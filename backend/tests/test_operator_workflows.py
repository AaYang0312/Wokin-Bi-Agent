"""Task 11：四工作流验收与首次用户交付（spec §10 的 11 个场景 + 主层路由）。

这份文件只做一件事：把 [运营工作流设计](../../docs/superpowers/specs/
2026-09-11-operator-workflows-design.md) 第 10 节那 11 行验收样例写成可执行的集成
用例，并按计划要求为**每个领域**跑齐四类路径：正常、部分来源、无权限、Artifact
持久化失败。它不重做 Task 7–10 的单元用例：那些文件钉的是节点与契约，这里钉的是
"运营用户问一句话，系统给什么"。

本轮取证边界（写在这里，免得把用例跑绿当成上线证明）：

1. **全部数据是合成的**，只写独立 `*_test` 库里的合成店铺 / 池 / 快照；没有任何真实
   账号、真实来源接口、真实同步、部署或真实模型被调用。缺集成环境的项一律标
   "未执行"（见 `docs/superpowers/research/2026-09-14-task-11-release-acceptance.md`）。
2. **渠道在售价与库存的来源门禁在代码里默认关闭**（Task 9 / Task 10 的交付状态）。
   本文件的"正常路径"是在**测试进程内**临时登记一条合成来源，跑完立刻 reset；
   这不构成任何平台"线上复核可用"的证据。
3. **路由用例的模型是脚本替身**：它们证明主层把一次工具调用换成一次图执行、
   参数契约成立、越权与降级路径成立；**不**证明真实模型会选对工具。真实模型
   26 题与 provider 联调本轮**未执行**。
4. 拼多多按 2026-09-12 决定**不接入**：本文件只断言显式 `excluded_scope` /
   `unsupported` / 能力解析为空，不出现任何"待授权""延后"的说法，也不新增连接器、
   来源、凭证或 onboarding 代码路径。

真实测试库跑法与既有约定一致：管理员连接 + 外层事务回滚（`tests.dbfixtures`）。
无 DSN 时显式 skip——skip 不是通过证明。
"""

from __future__ import annotations

import ast
import json
import os
import time
import unittest
from datetime import date, datetime, timedelta
from typing import Any, Mapping, Sequence
from unittest import mock
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from bi_agent.agent import SessionState, answer
from bi_agent.catalog import ref_for_key
from bi_agent.catalog.channel_mapping import (
    CHANNEL_MAPPING_VERSION, DEFAULT_NAMESPACE, IdentifierMapping,
    record_identifier_mapping)
from bi_agent.commerce.metrics import COMMERCE_GRAPH_VERSION
from bi_agent.commerce.models import (
    DomainContext, PerformanceComparisonRequest, ProductPerformanceRequest)
from bi_agent.commerce.tool import analyze_product_performance, compare_performance
from bi_agent.data_quality import QUALITY_RULE
from bi_agent.inventory import repository as inventory_repository
from bi_agent.inventory.graph import TEXT_SOURCE_UNVERIFIED as INVENTORY_SOURCE_TEXT
from bi_agent.inventory.models import InventoryInspectionRequest
from bi_agent.inventory.rules import (
    INVENTORY_RULE_VERSION, UNIT_CONVERSION_REGISTRY_VERSION,
    InventorySourceRegistration, pool_handle, register_inventory_source,
    reset_inventory_sources)
from bi_agent.inventory.tool import inspect_inventory
from bi_agent.listing_audit import repository as listing_repository
from bi_agent.listing_audit.graph import TEXT_SOURCE_UNVERIFIED as LISTING_SOURCE_TEXT
from bi_agent.listing_audit.models import ListingPriceAuditRequest
from bi_agent.listing_audit.rules import (
    LISTING_RULE_VERSION, LISTING_SOURCE_REGISTRY_VERSION,
    ListingSourceRegistration, register_listing_source, reset_listing_sources)
from bi_agent.listing_audit.tool import audit_listing_prices
from bi_agent.llm import Message, ModelReply, ToolCall
from bi_agent.runtime.memory import MemoryQueryRunStore
from bi_agent.sources import (
    PAYMENT_FAMILY, PDD_CEILING, SOURCE_REGISTRY_VERSION, TRADE_LIST_SOURCE,
    OUTSTOCK_SOURCE, TradeListPlatforms, ShopRecord, registration,
    resolve_metric_sources, unsupported_reason)

from .dbfixtures import connect_test_db

BEIJING = ZoneInfo("Asia/Shanghai")
# 冻结时刻晚于所有成交与快照：主层与四张图共用同一个"现在"。
NOW = datetime(2026, 9, 14, 12, tzinfo=BEIJING)
FRESH = NOW - timedelta(minutes=20)
STALE = NOW - timedelta(days=30)
WINDOW_START = datetime(2026, 8, 25, tzinfo=BEIJING)
WINDOW_END = datetime(2026, 9, 8, tzinfo=BEIJING)
START = date(2026, 9, 1)
END = date(2026, 9, 8)
ALL_CAPABILITIES = ("paid_amount", "paid_orders", "erp_documents", "aov", "quantity",
                    "product_paid_amount", "refund_amount", "cash_difference",
                    "cohort_refund_rate")
PDD_CAPABILITIES = ("erp_documents",)
COMPARABLE_PLATFORMS = ("jd", "kuaishou", "wxsph", "wsxc")
PRODUCT = "P11"
SKU_A = "SKU-A"
SKU_B = "SKU-B"


# ---------------------------------------------------------------------------
# 请求构造：与 Task 7–10 的入参契约同一形状
# ---------------------------------------------------------------------------


def product_request(**overrides: Any) -> ProductPerformanceRequest:
    base: dict[str, Any] = {
        "product": {"text": "直钉枪"},
        "scope": {"mode": "all_authorized"},
        "start": START.isoformat(), "end": END.isoformat(),
        "metrics": ["sold_quantity", "sales_amount", "weighted_avg_paid_price"],
        "sales_basis": "erp_effective_parent", "profit_basis": "none",
        "trend_days": 7, "comparison": "none",
    }
    base.update(overrides)
    return ProductPerformanceRequest.model_validate(base)


def comparison_request(**overrides: Any) -> PerformanceComparisonRequest:
    base: dict[str, Any] = {
        "scope": {"mode": "all_authorized"},
        "start": START.isoformat(), "end": END.isoformat(),
        "group_by": "platform",
        "metrics": ["sold_quantity", "sales_amount", "weighted_avg_paid_price"],
        "sales_basis": "erp_effective_parent", "profit_basis": "none",
        "trend_days": 7,
    }
    base.update(overrides)
    return PerformanceComparisonRequest.model_validate(base)


def price_rule(amount: str, **overrides: Any) -> dict[str, Any]:
    entry: dict[str, Any] = {"applies_to": "all_selected", "expected_amount": amount,
                             "currency": "CNY"}
    entry.update(overrides)
    return entry


def audit_request(**overrides: Any) -> ListingPriceAuditRequest:
    base: dict[str, Any] = {
        "product": {"text": "直钉枪"},
        "scope": {"mode": "all_authorized"},
        "as_of": "latest", "price_basis": "list_price",
        "expected_prices": [price_rule("19.90")],
    }
    base.update(overrides)
    return ListingPriceAuditRequest.model_validate(base)


def inventory_request(**overrides: Any) -> InventoryInspectionRequest:
    base: dict[str, Any] = {
        "products": "selected",
        "scope": {"mode": "all_authorized"},
        "levels": ["physical_total", "shop_sellable"],
        "as_of": "latest",
    }
    base.update(overrides)
    return InventoryInspectionRequest.model_validate(base)


def threshold(level: str, sku: str, quantity: str, **overrides: Any) -> dict[str, Any]:
    entry: dict[str, Any] = {"level": level, "sku_ref": ref_for_key("sku", sku),
                             "quantity": quantity, "unit": "piece"}
    entry.update(overrides)
    return entry


# ---------------------------------------------------------------------------
# 载荷读取：Artifact 类型 → 行 / 汇总
# ---------------------------------------------------------------------------


def payload_for(result: Any, artifact_type: str) -> dict[str, Any]:
    """按 Artifact 类型取公开载荷：命中 0 份或多份都直接失败。"""
    matches = [artifact.public_payload for artifact in result.artifacts
               if artifact.ref.type == artifact_type]
    assert len(matches) == 1, ([artifact.ref.type for artifact in result.artifacts],
                               artifact_type)
    return matches[0]


def artifact_of(artifacts: Sequence[Mapping[str, Any]], artifact_type: str) -> Mapping:
    """从主层已投影的展示载荷里取某类型的那一份（`answer()` 之后拿到的形状）。"""
    matches = [item for item in artifacts if item.get("artifact_type") == artifact_type]
    assert len(matches) == 1, ([item.get("artifact_type") for item in artifacts],
                               artifact_type)
    return matches[0]


def dataset_rows(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    """数据集里带分组键的行（平台 / 店铺 / 商品行），不含合计行。"""
    return [row for row in payload.get("data") or []
            if any(key in row for key in ("platform", "shop_ref"))]


def total_row(payload: Mapping[str, Any]) -> dict[str, Any]:
    """不带店铺 / 平台分组键的那一行：它才是"已评估集合的合计"。"""
    totals = [row for row in payload.get("data") or []
              if "platform" not in row and "shop_ref" not in row]
    assert len(totals) == 1, payload.get("data")
    return totals[0]


def audit_rows(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [row for row in payload.get("data") or [] if "audit_status" in row]


def alert_rows(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [row for row in payload.get("data") or [] if "inventory_status" in row]


def _reply(text: str | None = None, calls: list[ToolCall] | None = None) -> ModelReply:
    """脚本替身的一回合：`ModelReply` + 可回放进历史的 assistant 消息。"""
    message = Message(role="assistant", content=text, tool_calls=calls or [])
    reply = ModelReply(text=text, tool_calls=calls or [])
    reply._message = message
    return reply


def _call(name: str, arguments: dict[str, Any], call_id: str = "call_1") -> ToolCall:
    return ToolCall(id=call_id, name=name, arguments=arguments)


class _ArtifactStoreFails(MemoryQueryRunStore):
    """只让必需 Artifact 写入失败：其它运行写入照旧，才能单独看这一条降级路径。"""

    def save_artifact(self, run_id, artifact):  # noqa: ANN001
        raise RuntimeError("simulated artifact failure")


class _ChartStoreFails(MemoryQueryRunStore):
    """只让可选的 `chart_spec` 落库失败：表格必须保留（spec §7 的"图表不可渲染"分支）。"""

    def save_artifact(self, run_id, artifact):  # noqa: ANN001
        if getattr(artifact, "artifact_type", None) == "chart_spec":
            raise RuntimeError("simulated chart failure")
        return super().save_artifact(run_id, artifact)


class _CountingConn:
    """包一层真连接，只数经营图发了几条 SQL（"不随店铺数线性增长"的证据）。"""

    def __init__(self, inner: Any) -> None:
        self.inner = inner
        self.statements: list[str] = []

    def execute(self, sql: str, params: object = None):  # noqa: ANN001
        self.statements.append(" ".join(str(sql).split()))
        return self.inner.execute(sql, params)

    def aggregate_reads(self) -> int:
        """本回合发了几条事实取数 SQL（“不随店数增长”要钉的就是这个）。"""
        facts = ("reporting.v_product_cost_daily", "reporting.v_product_daily",
                 "reporting.v_erp_document_daily", "reporting.v_payments")
        return len([text for text in self.statements if any(view in text
                                                             for view in facts)])

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)


# ---------------------------------------------------------------------------
# 共用夹具：一套合成店 / 商品 / 快照，四个领域都从这儿取数
# ---------------------------------------------------------------------------


class OperatorFixture(unittest.TestCase):
    """四个领域共用的合成夹具。

    夹具只造"形状"，不造结论：结论全部来自各领域的图与确定性规则。任何一条来源
    登记都在 `addCleanup` 里退回"一条都没有"，不留进程内的隐性就绪状态。
    """

    def setUp(self) -> None:
        self.conn = connect_test_db(self)
        self.tag = uuid4().hex[:5].upper()
        self.shops: dict[str, str] = {}
        self.pools: dict[str, str] = {}
        self.chat_id = UUID(int=1)
        self.message_id = UUID(int=2)
        self._db_subject: str | None = None
        self.addCleanup(reset_listing_sources)
        self.addCleanup(reset_inventory_sources)

    # -- 店铺 / 成交 / 覆盖 -------------------------------------------------

    def _shop(self, key: str, platform: str = "fxg",
              capabilities: Sequence[str] = ALL_CAPABILITIES) -> str:
        shop_id = f"S{self.tag}{key}"
        self.conn.execute(
            "INSERT INTO bi.shops(shop_id, platform, display_name, capabilities) "
            "VALUES (%s, %s, %s, %s) ON CONFLICT (shop_id) DO UPDATE SET "
            "platform = EXCLUDED.platform, display_name = EXCLUDED.display_name, "
            "capabilities = EXCLUDED.capabilities",
            (shop_id, platform, f"验收店{key}{self.tag}", list(capabilities)))
        self.shops[key] = shop_id
        return shop_id

    def _order_source(self, key: str) -> str:
        """这家店按注册表应该走哪个订单通道（夹具不自己猜方法名）。"""
        row = self.conn.execute(
            "SELECT platform FROM bi.shops WHERE shop_id=%s", (self.shops[key],)).fetchone()
        platform = str(row[0])
        return OUTSTOCK_SOURCE if platform in ("tb", "tm", "pdd") else TRADE_LIST_SOURCE

    def _cover(self, key: str, start: datetime = WINDOW_START,
               end: datetime = WINDOW_END, source: str | None = None,
               entity: str = "orders") -> None:
        channel = source or self._order_source(key)
        self.conn.execute(
            "INSERT INTO bi.sync_state(source, entity, shop_id, watermark, covered, "
            "data_as_of, quality_status, quality_rule) "
            "VALUES (%s, %s, %s, %s, tstzmultirange(tstzrange(%s, %s, '[)')), %s, "
            "'passed', %s) ON CONFLICT (source, entity, shop_id) DO UPDATE SET "
            "covered = bi.sync_state.covered + EXCLUDED.covered, "
            "data_as_of = greatest(coalesce(bi.sync_state.data_as_of, "
            "  '-infinity'), EXCLUDED.data_as_of), "
            "quality_status = EXCLUDED.quality_status, "
            "quality_rule = EXCLUDED.quality_rule",
            (channel, entity, self.shops[key], end, start, end, end, QUALITY_RULE))
        self.conn.execute(
            "INSERT INTO bi.sync_batches(source, entity, shop_id, batch_id, "
            "business_window, window_kind, mode, row_count, business_end) "
            "VALUES (%s, %s, %s, %s, tstzrange(%s, %s, '[)'), 'business', "
            "'backfill', 1, %s) ON CONFLICT (source, entity, shop_id, batch_id) "
            "DO UPDATE SET business_window = EXCLUDED.business_window",
            (channel, entity, self.shops[key],
             f"t11-{channel}-{entity}-{start:%m%d}-{end:%m%d}", start, end, end))

    def _cover_holes(self, key: str, spans: Sequence[tuple[datetime, datetime]],
                     source: str | None = None, entity: str = "orders") -> None:
        """把某店的覆盖换成"有洞"的那几段（Q24 那类公共交集场景）。"""
        channel = source or self._order_source(key)
        self.conn.execute(
            "DELETE FROM bi.sync_state WHERE shop_id=%s AND source=%s AND entity=%s",
            (self.shops[key], channel, entity))
        for start, end in spans:
            self._cover(key, start, end, source=channel, entity=entity)
        self.conn.execute(
            "UPDATE bi.sync_state SET data_as_of=%s WHERE shop_id=%s AND source=%s "
            "AND entity=%s", (max(end for _start, end in spans), self.shops[key],
                              channel, entity))

    def _archive(self, product: str, title: str) -> None:
        self.conn.execute(
            "INSERT INTO bi.products(product_id, title, source_modified_at, synced_at) "
            "VALUES (%s, %s, now(), now()) ON CONFLICT (product_id) "
            "DO UPDATE SET title = EXCLUDED.title", (product, title))

    def _product(self, suffix: str = PRODUCT) -> str:
        return f"{self.tag}{suffix}"

    def _sku(self, suffix: str) -> str:
        return f"{self.tag}{suffix}"

    def _sale(self, key: str, erp_id: str, *, product: str | None = None,
              sku: str = SKU_A, day: date = date(2026, 9, 2), quantity: str,
              amount: str, cost: str | None = None, line_kind: str = "sale",
              verified: bool = True, document_profit: str | None = None,
              source: str | None = None, active: bool = True) -> None:
        """一张 ERP 单据 + 一行商品父行（与 Task 7 / 8 的夹具同一形状）。"""
        shop_id = self.shops[key]
        channel = source or self._order_source(key)
        paid_at = datetime(day.year, day.month, day.day, 12, tzinfo=BEIJING)
        product_id = self._product(product or PRODUCT)
        self.conn.execute(
            "INSERT INTO bi.orders(shop_id, erp_id, commercial_ids, source, "
            "source_updated_at, paid_at, raw_gross_profit, active, batch_id) "
            "VALUES (%s, %s, %s, %s, now(), %s, %s, %s, 't11-seed') "
            "ON CONFLICT (shop_id, erp_id) DO NOTHING",
            (shop_id, erp_id, [f"C{erp_id}"], channel, paid_at, document_profit, active))
        self.conn.execute(
            "INSERT INTO bi.order_items(shop_id, erp_id, line_id, commercial_id, "
            "product_id, sku_id, paid_at, quantity, raw_unit_cost, "
            "allocated_paid_amount, allocation_verified, line_kind, active, "
            "product_name_snapshot, sku_label_snapshot) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (shop_id, erp_id, line_id) DO NOTHING",
            (shop_id, erp_id, f"{erp_id}-L1", f"C{erp_id}", product_id,
             self._sku(sku), paid_at, quantity, cost, amount, verified, line_kind,
             active, "直钉枪", "6mm" if sku == SKU_A else "8mm"))

    # -- 渠道映射 / 上架快照 ------------------------------------------------

    def _map(self, key: str, *, listing: str, sku: str = SKU_A,
             platform: str = "tb", platform_sku: str | None = None,
             product: str | None = None) -> None:
        record_identifier_mapping(self.conn, IdentifierMapping(
            namespace=DEFAULT_NAMESPACE, platform=platform, shop_id=self.shops[key],
            listing_id=listing, platform_sku_id=platform_sku or f"PS-{listing}-{sku}",
            erp_product_id=self._product(product or PRODUCT),
            erp_sku_id=self._sku(sku),
            evidence=f"probe-t11-{self.tag}-{key}-{listing}", source="manual_map"),
            at=date(2026, 9, 1))

    def _register_listing_source(self, platform: str = "tb") -> None:
        """测试进程内登记一条**合成**渠道在售价来源（不是任何平台的就绪证明）。"""
        register_listing_source(ListingSourceRegistration(
            platform=platform, source_kind="official_export",
            evidence=f"probe-t11-{self.tag}-{platform}", max_age_seconds=86400))

    def _register_inventory_sources(self, *levels: str) -> None:
        for level in levels or ("physical_total", "shop_sellable"):
            register_inventory_source(InventorySourceRegistration(
                level=level,
                channel="erp" if level == "physical_total" else "official_export",
                evidence=f"probe-t11-{self.tag}-{level}", max_age_seconds=86400,
                scan_complete_supported=False, production_reconciled_at=None))

    def _snapshot(self, key: str, *, items: Sequence[dict[str, Any]],
                  platform: str = "tb", captured_at: datetime = FRESH,
                  enumeration_complete: bool = True,
                  source: str = "official_export",
                  evidence: str | None = None) -> str:
        sid = f"snap-{self.tag}-{key}-{uuid4().hex[:6]}"
        declared = evidence or f"probe-t11-{self.tag}-{key}"
        listing_repository.insert_listing_snapshot(
            self.conn, snapshot_id=sid, shop_id=self.shops[key], platform=platform,
            namespace=DEFAULT_NAMESPACE, source=source, evidence=declared,
            captured_at=captured_at, enumeration_complete=enumeration_complete,
            enumeration_evidence=declared if enumeration_complete else None,
            batch_id=f"batch-{sid}")
        for index, item in enumerate(items):
            listing_repository.insert_listing_snapshot_item(
                self.conn, snapshot_id=sid, shop_id=self.shops[key],
                namespace=DEFAULT_NAMESPACE,
                listing_id=item.get("listing_id", f"L{index}"),
                platform_sku_id=item.get("platform_sku_id", ""),
                erp_sku_id=item.get("erp_sku_id", ""),
                erp_product_id=item.get("erp_product_id"),
                list_amount=item.get("list_amount"),
                campaign_amount=item.get("campaign_amount"),
                currency=item.get("currency", "CNY"),
                on_sale=item.get("on_sale", True),
                captured_at=item.get("captured_at", captured_at))
        return sid

    # -- 库存池 / 快照 / 阈值策略 -------------------------------------------

    def _pool(self, key: str, *, connection: str = "shared",
              shops: tuple[str, ...] = ()) -> str:
        pool_id = f"pool-{self.tag}-{key}"
        self.conn.execute(
            "INSERT INTO bi.inventory_pools(pool_id, namespace, label, connection_kind, "
            "evidence) VALUES (%s, %s, %s, %s, %s) ON CONFLICT (namespace, pool_id) "
            "DO NOTHING",
            (pool_id, DEFAULT_NAMESPACE, f"库存池{key}", connection,
             f"probe-t11-{self.tag}-{key}"))
        for shop in shops:
            self.conn.execute(
                "INSERT INTO bi.inventory_pool_shops(namespace, pool_id, shop_id) "
                "VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
                (DEFAULT_NAMESPACE, pool_id, self.shops[shop]))
        self.pools[key] = pool_id
        return pool_id

    def _physical(self, pool_key: str, *, warehouse: str, sku: str, quantity: str,
                  unit: str = "piece", batch: str = "batch-1",
                  captured_at: datetime = FRESH,
                  scan_complete: bool = True) -> None:
        sid = f"ph-{self.tag}-{warehouse}-{batch}"
        inventory_repository.insert_physical_snapshot(
            self.conn, snapshot_id=sid, pool_id=self.pools[pool_key],
            warehouse_id=f"{self.tag}-{warehouse}", namespace=DEFAULT_NAMESPACE,
            platform=None, source="erp", evidence=f"probe-t11-{self.tag}-{pool_key}",
            captured_at=captured_at, scan_complete=scan_complete,
            scan_evidence=f"pages-t11-{self.tag}" if scan_complete else None,
            batch_id=batch)
        inventory_repository.insert_physical_snapshot_item(
            self.conn, snapshot_id=sid, pool_id=self.pools[pool_key],
            warehouse_id=f"{self.tag}-{warehouse}", namespace=DEFAULT_NAMESPACE,
            erp_sku_id=self._sku(sku), available_quantity=quantity, unit=unit,
            batch_id=batch, captured_at=captured_at)

    def _channel_stock(self, key: str, *, sku: str, quantity: str | None,
                       listing: str = "L1", platform: str = "tb",
                       captured_at: datetime = FRESH,
                       snapshot_id: str | None = None) -> None:
        sid = snapshot_id or f"ch-{self.tag}-{key}"
        inventory_repository.insert_channel_snapshot(
            self.conn, snapshot_id=sid, shop_id=self.shops[key], platform=platform,
            namespace=DEFAULT_NAMESPACE, source="official_export",
            evidence=f"probe-t11-{self.tag}-{key}", captured_at=captured_at,
            scan_complete=True, scan_evidence=f"pages-t11-{self.tag}-{key}")
        inventory_repository.insert_channel_snapshot_item(
            self.conn, snapshot_id=sid, shop_id=self.shops[key],
            namespace=DEFAULT_NAMESPACE, listing_id=listing,
            platform_sku_id=f"PS-{listing}-{sku}", erp_sku_id=self._sku(sku),
            sellable_quantity=quantity, unit="piece", captured_at=captured_at)

    def _threshold_policy(self, *, sku: str, level: str, quantity: str,
                          pool_key: str | None = None, shop_key: str | None = None,
                          version: str = "operator-default/1") -> None:
        # 生效日同样要显式钉在本轮冻结时钟上：那一列不给就落库 `current_date`，真实时间
        # 一跨过 NOW 后图上按 `at=context.now.date()` 就再也取不到策略（与 test_inventory
        # 的 _threshold 同一约束）。
        inventory_repository.insert_threshold_policy(
            self.conn, policy_id=f"pol-{self.tag}-{sku}-{level}-{version}",
            policy_version=version, level=level, erp_sku_id=self._sku(sku),
            pool_id=pool_key and self.pools[pool_key],
            shop_id=shop_key and self.shops[shop_key],
            quantity=quantity, unit="piece", effective_at=NOW.date(),
            evidence=f"policy-import-t11-{self.tag}")

    # -- 上下文 -------------------------------------------------------------

    def _allow(self, *keys: str) -> frozenset[str]:
        return frozenset(self.shops[key] for key in (keys or tuple(self.shops)))

    def _db_store(self) -> Any:
        """一个真的 `PostgresQueryRunStore`：要断言"本轮依据真的落了库"时才用它。

        运行行对 `bi.app_messages` 有外键，所以聊天与用户消息也得先造出来：
        这三张表一起写才能复现真实部署里"一句提问 → 一份审计依据"的链路。
        """
        from bi_agent.runtime.repository import PostgresQueryRunStore

        self.chat_id, self.message_id = uuid4(), uuid4()
        subject = f"t11-db-{self.tag}"
        self.conn.execute(
            "INSERT INTO bi.app_chats(id, subject_id, title) VALUES (%s, %s, '运营验收')",
            (self.chat_id, subject))
        self.conn.execute(
            "INSERT INTO bi.app_messages(id, chat_id, role, content, status) "
            "VALUES (%s, %s, 'user', '标价是否正确', 'complete')",
            (self.message_id, self.chat_id))
        self._db_subject = subject
        return PostgresQueryRunStore(self.conn,
                                     forbidden_values=set(self.shops.values()))

    def _pools(self, *keys: str) -> frozenset[str]:
        return frozenset(self.pools[key] for key in (keys or tuple(self.pools)))

    def _context(self, *, allowed: frozenset[str] | None = None,
                 pools: frozenset[str] | None = None,
                 store: MemoryQueryRunStore | None = None,
                 conn: Any = None, deadline: float | None = None,
                 subject: str | None = None) -> DomainContext:
        allowed = self._allow() if allowed is None else allowed
        return DomainContext(
            subject_id=subject or f"t11-{self.tag}", allowed_shop_ids=allowed,
            shop_refs={shop: ref_for_key("shop", shop) for shop in allowed},
            allowed_inventory_pool_ids=self._pools() if pools is None else pools,
            conn=self.conn if conn is None else conn,
            store=store or MemoryQueryRunStore(
                forbidden_values=set(self.shops.values()) or {"unused"}),
            chat_id=self.chat_id, user_message_id=self.message_id,
            root_request_id=UUID(int=3), now=NOW,
            deadline=time.monotonic() + 30 if deadline is None else deadline,
            attempt_no=1)

    # -- 主层（脚本替身） ---------------------------------------------------

    def _turn(self, question: str, replies: Sequence[ModelReply], *,
              allowed: frozenset[str] | None = None,
              state: SessionState | None = None,
              store: MemoryQueryRunStore | None = None,
              conn: Any = None,
              deadline_patch: float | None = None,
              approved_query_memory_enabled: bool = False,
              isolated_analysis_enabled: bool = False):
        """一句话过一遍主层：真实图、真实库、脚本模型。

        这是“离线集成”而不是“真实模型验收”：模型侧只提供预制回合，因此这些用例
        能钉住分发、参数契约、降级与持久化路径，钉不住模型的理解准确率。
        """
        model = mock.Mock()
        model.complete.side_effect = list(replies)
        self.last_model = model
        allowed = self._allow() if allowed is None else allowed
        state = state or SessionState(subject=f"t11-agent-{self.tag}")
        patches = []
        if deadline_patch is not None:
            patches.append(mock.patch("bi_agent.agent.TOTAL_BUDGET_SECONDS",
                                      new=deadline_patch))
        store = store or MemoryQueryRunStore(forbidden_values=set(self.shops.values())
                                             or {"unused"})
        for patch in patches:
            patch.start()
        self.addCleanup(lambda: [patch.stop() for patch in patches])
        return answer(question, state, model=model, conn=self.conn if conn is None
                      else conn, allowed_shop_ids=allowed, now=NOW, run_store=store,
                      approved_query_memory_enabled=approved_query_memory_enabled,
                      isolated_analysis_enabled=isolated_analysis_enabled)


def _is_database_class(node: ast.AST) -> bool:
    """这个类是不是“要读真实测试库”的用例类（以 `OperatorFixture` 为基类）。

    判据取基类名而不是真跑一次 setUp：DSN gate 本身必须能在没库的环境里被检查。
    """
    return (isinstance(node, ast.ClassDef)
            and any(isinstance(base, ast.Name) and base.id == "OperatorFixture"
                    for base in node.bases))


def _module_tree() -> ast.Module:
    from pathlib import Path

    return ast.parse(Path(__file__).read_text(encoding="utf-8"))


class OperatorTestDatabaseGatingTests(unittest.TestCase):
    """没有测试库 DSN 时，本文件的库用例必须整组 skip，不能变成 error。

    `OperatorFixture.setUp` 走 `dbfixtures.connect_test_db`，那里第一句就是
    `os.environ["BI_TEST_ADMIN_DSN"]`：少了类上的 gate，计划里第一条**不带**
    `--env-file` 的验收命令在任何没配库的环境上都会报 KeyError。skip 不是通过证明，
    但把"这台机器没测试库"写成"代码坏了"，就把两件不同的事说成了同一件。
    """

    def test_every_database_class_declares_the_dsn_gate(self):
        """结构守护：类名清单从源码 AST 取，不手写。

        以后新增一个库用例类却忘了 gate，这里会一起拓出来，而不是等到某台没库的
        机器上报 error 才发现。
        """
        unguarded = [node.name for node in _module_tree().body
                     if _is_database_class(node)
                     and not any("BI_TEST_ADMIN_DSN" in ast.dump(dec)
                                 for dec in node.decorator_list)]
        self.assertEqual(
            unguarded, [],
            f"这些库用例类没有 DSN gate，没配库的环境会 error 而不是 skip：{unguarded}")

    def test_database_classes_skip_without_the_dsn(self):
        """行为守护：拿掉 DSN 重跑一遍库用例，只能看到 skip。"""
        import subprocess
        import sys
        from pathlib import Path

        classes = [node.name for node in _module_tree().body
                   if _is_database_class(node)]
        self.assertTrue(classes, "本文件应该已经有库用例类")
        env = {key: value for key, value in os.environ.items()
               if key != "BI_TEST_ADMIN_DSN"}
        # 子进程的输出里有中文 skip 理由：不给定 UTF-8 两侧，Windows 会拿系统码页
        # 去解，解失败时 stdout/stderr 直接变 None，守护就看不见了。
        env["PYTHONIOENCODING"] = "utf-8"
        # 也不让子进程写 __pycache__：父子同时刷新同一个模块的缓存字节码是假错误的
        # 现成来源，而这条守护要能在任何机器上一次过。
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        # 只跑库用例类：gate 自己不在子进程里再跑一次，不会递归。
        command = [sys.executable, "-B", "-m", "unittest",
                   *(f"tests.test_operator_workflows.{name}" for name in classes)]
        try:
            completed = subprocess.run(
                command, cwd=str(Path(__file__).resolve().parents[1]), env=env,
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=600)
        except subprocess.TimeoutExpired as exc:  # pragma: no cover - 只在机器卡死时走到
            self.fail(f"子进程没在 600 秒内跑完：{command}\n{exc}")
        output = completed.stdout + completed.stderr
        self.assertIsNotNone(completed.stdout, "子进程没给出可解码的标准输出")
        self.assertNotIn("ERROR:", output, output[-2500:])
        self.assertNotIn("Traceback", output, output[-2500:])
        self.assertIn("skipped=", output, output[-2500:])
        self.assertEqual(completed.returncode, 0, output[-2500:])


@unittest.skipUnless(os.getenv("BI_TEST_ADMIN_DSN"), "未配置独立测试数据库")
class OperatorProductWorkflowTests(OperatorFixture):
    """商品运营域（`analyze_product_performance`）的四类路径与相关场景。"""

    def _run(self, store: MemoryQueryRunStore | None = None,
             allowed: frozenset[str] | None = None, **overrides: Any):
        return analyze_product_performance(
            product_request(**overrides),
            self._context(store=store, allowed=allowed))

    def _three_platform_five_shops(self, *, cost: str | None = "40") -> None:
        """三平台五店：fxg 两家、jd 一家能答；tb 一家时间口径不成立；pdd 无支付能力。"""
        self._archive(PRODUCT, "直钉枪")
        for key, platform in (("1", "fxg"), ("2", "fxg"), ("3", "jd"), ("4", "tb")):
            self._shop(key, platform=platform)
            self._cover(key)
        self._shop("5", platform="pdd", capabilities=PDD_CAPABILITIES)
        self._cover("5")
        # 两个 SKU 分五店：同一次映射链要能追到两个规格，不混价。
        self._sale("1", "E1", sku=SKU_A, quantity="1", amount="100", cost=cost,
                   day=date(2026, 9, 2))
        self._sale("2", "E2", sku=SKU_B, quantity="9", amount="450", cost=cost,
                   day=date(2026, 9, 3))
        self._sale("3", "E3", sku=SKU_A, quantity="2", amount="200", cost=cost,
                   day=date(2026, 9, 4))
        self._sale("4", "E4", sku=SKU_A, quantity="7", amount="700", cost=cost,
                   day=date(2026, 9, 5), source=OUTSTOCK_SOURCE)
        self._sale("5", "E5", sku=SKU_A, quantity="5", amount="500", cost=cost,
                   day=date(2026, 9, 6), source=OUTSTOCK_SOURCE)
        # 已确认的标识映射：跨平台的同一 ERP SKU 才能被说成"同款"。
        self._map("1", listing="L1", sku=SKU_A, platform="fxg")
        self._map("2", listing="L2", sku=SKU_B, platform="fxg")
        self._map("3", listing="L3", sku=SKU_A, platform="jd")

    # -- 正常路径：场景 1（两 SKU 跨三平台五店）--------------------------

    def test_product_report_tracks_two_skus_and_keeps_incomparable_shops_apart(self):
        """场景 1：A 商品两种 SKU 跨三平台五店。

        要拿到的是"同款映射可追踪 + SKU 不混价 + 七日趋势独立成集"。已认证支付口径
        （fxg）与未逐店对照的口径（jd）同名不同认证，所以跨店合计按契约**不发**；
        各店自己的数照给，缺的两家逐家归因。已评估集合内部的排名属于
        `compare_performance`（商品报告本来不发 `ranking` 块，拿它的缺席当护栏会是假断言），
        那边由本文件的比较域用例钉住。
        """
        self._three_platform_five_shops()
        result = self._run()
        payload = result.model_payload
        self.assertEqual(result.status.value, "partial", payload.get("limitations"))
        # 同款映射可追踪：商品引用、两个 SKU 引用与版本化的映射版本一起给。
        resolved = payload["resolved_product"]
        self.assertEqual(resolved["product_ref"], ref_for_key("product", self._product()))
        self.assertEqual(sorted(resolved["sku_refs"]),
                         sorted([ref_for_key("sku", self._sku(SKU_A)),
                                 ref_for_key("sku", self._sku(SKU_B))]))
        self.assertEqual(resolved["mapping_version"], CHANNEL_MAPPING_VERSION)
        # SKU 不混价：逐店行各自保留自己的均价，合并只发生在合计行那一格。
        per_shop = {row["shop_ref"]: row for row in payload["data"] if "shop_ref" in row}
        self.assertEqual(sorted(str(row["weighted_avg_paid_price"])
                                for row in per_shop.values()), ["100", "100", "50"])
        for raw_sku in (self._sku(SKU_A), self._sku(SKU_B), self._product()):
            self.assertNotIn(raw_sku, json.dumps(payload, ensure_ascii=False,
                                                 default=str),
                             "ERP 商品 / SKU 主键不进模型载荷")
        # 跨口径不汇总：合计行的这三个指标都是 null，而不是一个混合数。
        total = total_row(payload)
        for metric in ("sold_quantity", "sales_amount", "weighted_avg_paid_price"):
            self.assertIsNone(total[metric], payload["limitations"])
        statuses = payload["metric_statuses"]
        self.assertEqual({(str(item["status"]), str(item["reason"]))
                          for item in statuses},
                         {("incomparable", "basis_incompatible")})
        # 已评估集合里同时挂着已认证支付与未逐店对照的支付：份额只在这些店之间算，
        # 缺的两家不进分母。“不排名”在本域不能拿 `ranking` 缺席当证据：商品报告本来
        # 就不发排名块（排名属于 compare_performance），真正的护栏是上面那两个 null。
        shares = [str(row.get("sales_share")) for row in per_shop.values()]
        self.assertEqual(shares, ["None", "None", "None"],
                         "口径不可比时逐店份额也不发：否则三个百分比会被读成一个集合的占比")
        # 七日趋势独立成一份数据集，缺日留 gap 而不是抹平。
        trend = payload_for(result, "trend_series")
        self.assertEqual(len({row["day"] for row in trend["data"]}), 7)
        # 答不了本口径的两家店整店退出合计，原因逐家列出，不给 0。
        excluded = {item["shop_ref"]: item["reason"] for item in payload["excluded_scope"]}
        self.assertEqual(excluded,
                         {ref_for_key("shop", self.shops["4"]):
                          "coverage_time_basis_unverified",
                          ref_for_key("shop", self.shops["5"]):
                          "coverage_time_basis_unverified"},
                         "出库通道的两家（淘系与拼多多）都归因到实测不成立的付款时间口径；"
                         "拼多多另外永远解析不出支付依赖，那条断言在 PDD 边界用例里钉")
        dumped = json.dumps(payload, ensure_ascii=False, default=str)
        self.assertNotIn(self.shops["5"], dumped)
        self.assertEqual(payload["status"], "partial",
                         "两家可算一家缺：只能是 partial，不能当全量合计")

    def test_product_workflow_publishes_persisted_datasets_with_lineage(self):
        """正常路径还要看见“结果真的落库了”：三份数据集同版本、血缘带来源批次。"""
        self._three_platform_five_shops()
        store = MemoryQueryRunStore(forbidden_values=set(self.shops.values()))
        result = self._run(store=store)
        types = sorted(artifact.ref.type for artifact in result.artifacts)
        self.assertEqual(types, ["metric_result", "trend_series"])
        run = store.runs[result.run_id]
        provenance = run["provenance"]
        self.assertEqual(provenance.graph_version, COMMERCE_GRAPH_VERSION)
        self.assertTrue(provenance.source_batches, "血缘必须指回真实的合成批次")
        self.assertEqual(run["domain"], "commerce_performance")
        self.assertEqual(run["status"], "partial")
        self.assertIsNotNone(run["request_fingerprint"])

    # -- 部分来源路径 ------------------------------------------------------

    def test_product_report_with_one_source_missing_is_partial_not_silently_complete(self):
        """部分来源：三家店里一家只有单据能力——能答的照给，缺的逐家归因。"""
        self._archive(PRODUCT, "直钉枪")
        for key in ("1", "2"):
            self._shop(key)
            self._cover(key)
            self._sale(key, f"E{key}", quantity="1", amount="100", cost="40")
        self._shop("3", capabilities=["erp_documents"])
        self._cover("3")
        self._sale("3", "E3", quantity="9", amount="900", cost="1")

        payload = self._run().model_payload
        self.assertEqual(payload["status"], "partial")
        excluded = {item["reason"] for item in payload["excluded_scope"]}
        self.assertEqual(excluded, {"capability_ungranted"})
        # 能答的指标不会因为同一请求里另一个指标缺数据而被收走：这家店的单据行仍在。
        statuses = payload["metric_statuses"]
        self.assertTrue(statuses, "逐店逐指标的状态列不得缺席")
        self.assertEqual({str(item["shop_ref"]) for item in statuses},
                         {ref_for_key("shop", self.shops["1"]),
                          ref_for_key("shop", self.shops["2"])},
                         "被整店剪掉的那家不进 metric_statuses：它已在 excluded_scope 里")
        self.assertEqual(total_row(payload)["sales_amount"], "200")

    def test_product_workflow_refuses_unverified_time_basis_without_zeroing_the_shop(self):
        """部分来源的另一副面孔：出库通道的店不当 0，也不拉低已有销量。"""
        self._archive(PRODUCT, "直钉枪")
        self._shop("1")
        self._shop("2", platform="tm")
        self._cover("1")
        self._cover("2")
        self._sale("1", "E1", quantity="1", amount="100", cost="40")
        self._sale("2", "E2", quantity="8", amount="800", cost="40",
                   source=OUTSTOCK_SOURCE)
        payload = self._run().model_payload
        self.assertEqual([row["shop_ref"] for row in dataset_rows(payload)
                          if "sales_amount" in row],
                         [ref_for_key("shop", self.shops["1"])])
        self.assertEqual(total_row(payload)["sold_quantity"], "1",
                         "不能把未认证那家店的 8 件算进来，也不能把它当 0 件")

    # -- 无权限路径 --------------------------------------------------------

    def test_product_workflow_forbids_an_out_of_scope_shop_ref(self):
        self._archive(PRODUCT, "直钉枪")
        self._shop("1")
        hidden = self._shop("2")
        self._cover("1")
        self._sale("1", "E1", quantity="1", amount="100", cost="40")
        self._cover("2")
        self._sale("2", "E2", quantity="9", amount="900", cost="1")

        result = analyze_product_performance(
            product_request(scope={"mode": "selected",
                                   "shop_refs": [ref_for_key("shop", hidden)]}),
            self._context(allowed=frozenset({self.shops["1"]})))
        self.assertEqual(result.status.value, "failed")
        self.assertEqual(result.error.code, "forbidden")
        self.assertEqual(result.model_payload, {"status": "failed"})
        self.assertEqual(result.artifacts, [], "越权不发布任何数据")

    def test_product_workflow_never_sees_shops_outside_the_authorized_set(self):
        """场景 11（商品面）：只能看两家店时，all_authorized 也翻不出第三家。"""
        self._three_platform_five_shops()
        allowed = self._allow("1", "2")
        payload = self._run(allowed=allowed).model_payload
        refs = {row["shop_ref"] for row in payload["data"]
                if isinstance(row.get("shop_ref"), str)}
        self.assertEqual(refs, {ref_for_key("shop", self.shops["1"]),
                                ref_for_key("shop", self.shops["2"])})
        self.assertEqual(set(payload["evaluated_scope"]["shop_refs"]),
                         {ref_for_key("shop", self.shops["1"]),
                          ref_for_key("shop", self.shops["2"])})
        dumped = json.dumps(payload, ensure_ascii=False, default=str)
        for key in ("3", "4", "5"):
            self.assertNotIn(self.shops[key], dumped,
                             "未授权店的 ERP 主键既不进数据也不进缺口清单")

    # -- 持久化失败路径 ----------------------------------------------------

    def test_product_workflow_persistence_failure_is_not_reported_as_success(self):
        self._three_platform_five_shops()
        result = self._run(store=_ArtifactStoreFails(
            forbidden_values=set(self.shops.values())))
        self.assertEqual(result.status.value, "failed")
        self.assertEqual(result.error.code, "artifact_persistence_failed")
        self.assertEqual(result.artifacts, [])
        self.assertNotIn("data", result.model_payload)
        self.assertNotIn("sales_amount", json.dumps(result.model_payload,
                                                    ensure_ascii=False, default=str))

    def test_agent_turn_reports_persistence_failure_instead_of_a_partial_answer(self):
        """主层同样不能“卡片没发出去但正文说查到了”。"""
        self._three_platform_five_shops()
        calls = [_reply(calls=[_call("analyze_product_performance", {
            "product": {"text": "直钉枪"}, "scope": {"mode": "all_authorized"},
            "start": START.isoformat(), "end": END.isoformat(),
            "metrics": ["sales_amount", "sold_quantity"],
            "sales_basis": "erp_effective_parent", "profit_basis": "none"})]),
            _reply(text="查到了一些数字")]
        turn = self._turn("直钉枪各店近七天卖得怎么样", calls,
                          store=_ArtifactStoreFails(
                              forbidden_values=set(self.shops.values())))
        self.assertEqual(turn.error_code, "artifact_persistence_failed")
        self.assertEqual(turn.artifacts, [])
        self.assertNotIn("查到了一些数字", turn.text)

    # -- 场景 2 与场景 10：均价与缺成本 ------------------------------------

    def test_two_shop_weighted_price_is_not_the_average_of_shop_averages(self):
        """spec §10 第 2 行：1 件 / 100 元与 9 件 / 450 元的总均价是 55 元，不是 75 元。"""
        self._archive(PRODUCT, "直钉枪")
        for key in ("1", "2"):
            self._shop(key)
            self._cover(key)
        self._sale("1", "E1", quantity="1", amount="100", cost="40")
        self._sale("2", "E2", quantity="9", amount="450", cost="30",
                   day=date(2026, 9, 3))
        payload = self._run(
            profit_basis="existing_fields",
            metrics=["sold_quantity", "sales_amount", "weighted_avg_paid_price",
                     "product_gross_profit_reference",
                     "product_gross_margin_reference"]).model_payload
        self.assertEqual(total_row(payload)["weighted_avg_paid_price"], "55")
        # “不是 75”要按值钉：拿整个载荷扫子串会撞进 UUID 或其它数字，既可能假红
        # 也可能假绿，看不出均价到底算错在哪。
        prices = [str(row["weighted_avg_paid_price"]) for row in payload["data"]]
        self.assertEqual(sorted(prices), ["100", "50", "55"],
                         "逐店均价各自保留，合计行才是 55")
        self.assertNotIn("75", prices, "两店均价的平均不是总均价")
        self.assertEqual(sorted(str(row["sales_share"])
                                for row in payload["data"] if "shop_ref" in row),
                         ["0.181818", "0.818182"],
                         "份额按金额算：100/550 与 450/550，不是按件数也不是平均")
        self.assertEqual(total_row(payload)["product_gross_profit_reference"], "240")
        self.assertEqual(total_row(payload)["sales_amount"], "550")
        self.assertEqual(total_row(payload)["sold_quantity"], "10")

    def test_missing_cost_nulls_profit_but_keeps_quantity_and_sales(self):
        """spec §10 第 10 行：缺成本只让利润不可算，销量与金额照给。"""
        self._archive(PRODUCT, "直钉枪")
        self._shop("1")
        self._cover("1")
        self._sale("1", "E1", quantity="1", amount="100", cost=None)
        self._sale("1", "E2", sku=SKU_B, quantity="2", amount="200", cost="30",
                   day=date(2026, 9, 3))
        payload = self._run(
            profit_basis="existing_fields",
            metrics=["sold_quantity", "sales_amount", "weighted_avg_paid_price",
                     "product_gross_profit_reference"]).model_payload
        total = total_row(payload)
        self.assertIsNone(total["product_gross_profit_reference"],
                          "一行缺成本就不能拿已知子集冒充整体")
        self.assertEqual(total["sales_amount"], "300")
        self.assertEqual(total["sold_quantity"], "3")
        self.assertEqual(payload["status"], "ok")

    def test_profit_basis_must_be_confirmed_before_a_reference_metric_is_published(self):
        """“利润”按已有表项：没显式选 profit_basis 就不发毛利列，也不默认猜一个。"""
        with self.assertRaises(ValueError) as raised:
            product_request(metrics=["sales_amount", "product_gross_profit_reference"])
        self.assertIn("profit_basis", str(raised.exception))


@unittest.skipUnless(os.getenv("BI_TEST_ADMIN_DSN"), "未配置独立测试数据库")
class OperatorComparisonWorkflowTests(OperatorFixture):
    """平台 / 店铺比较域（`compare_performance`）：四类路径与图表逐项相等。"""

    def _run(self, store: MemoryQueryRunStore | None = None,
             allowed: frozenset[str] | None = None, conn: Any = None,
             **overrides: Any):
        return compare_performance(comparison_request(**overrides),
                                   self._context(store=store, allowed=allowed,
                                                 conn=conn))

    def _five_platforms(self, *, missing: str = "pdd") -> None:
        """四个同口径平台 + 一个回答不了本口径的平台（Task 8 的总览形状）。"""
        for index, platform in enumerate(COMPARABLE_PLATFORMS):
            key = str(index + 1)
            self._shop(key, platform=platform)
            self._cover(key)
            self._sale(key, f"E{index}", quantity=str(index + 1),
                       amount=str(100 * (index + 1)), day=date(2026, 9, 1 + index))
        self._shop("5", platform=missing,
                   capabilities=PDD_CAPABILITIES if missing == "pdd"
                   else ALL_CAPABILITIES)
        self._cover("5")
        self._sale("5", "E9", quantity="7", amount="700", day=date(2026, 9, 2),
                   source=OUTSTOCK_SOURCE)

    # -- 正常路径与场景 3 -------------------------------------------------

    def test_platform_without_payment_source_stays_an_explicit_missing_group(self):
        """场景 3：三平台里一家拿不到完整支付来源 → 两平台可比 + 第三家原因。"""
        for index, platform in enumerate(("jd", "kuaishou")):
            key = str(index + 1)
            self._shop(key, platform=platform)
            self._cover(key)
            self._sale(key, f"E{index}", quantity="1", amount="100")
        self._shop("3", platform="pdd", capabilities=PDD_CAPABILITIES)
        self._cover("3")
        self._sale("3", "E3", quantity="9", amount="900", source=OUTSTOCK_SOURCE)

        result = self._run()
        payload = result.model_payload
        self.assertEqual(result.status.value, "partial", payload["limitations"])
        table = payload_for(result, "comparison_table")
        rows = {row["platform"]: row for row in dataset_rows(table)}
        self.assertEqual(sorted(rows), ["jd", "kuaishou"],
                         "答不了本口径的平台整组不发布，也不给它一个 0")
        self.assertEqual(total_row(table)["sales_amount"], "200")
        self.assertEqual(total_row(table)["weighted_avg_paid_price"], "100")
        ranking = {block["metric"]: block for block in payload["ranking"]}
        self.assertEqual(ranking["sales_amount"]["status"], "complete")
        self.assertNotIn("pdd", json.dumps(ranking, ensure_ascii=False),
                         "缺失平台不占名次，也不进合计")
        self.assertEqual({item["reason"] for item in payload["excluded_scope"]},
                         {"coverage_time_basis_unverified"})
        groups = {item["platform"]: item for item in payload["group_statuses"]
                  if "platform" in item}
        self.assertEqual(groups["pdd"]["shops_evaluated"], 0)
        self.assertEqual(groups["pdd"]["shops_requested"], 1)

    def test_five_platform_chart_and_table_agree_row_by_row(self):
        """五平台总览：一根指标一根柱，图 / 表 / 排名逐行相等（Task 11 验收点）。"""
        self._five_platforms()
        result = self._run(store=MemoryQueryRunStore(
            forbidden_values=set(self.shops.values())))
        table = payload_for(result, "comparison_table")
        table_ref = next(artifact.ref for artifact in result.artifacts
                         if artifact.ref.type == "comparison_table")
        charts = [artifact.public_payload for artifact in result.artifacts
                  if artifact.ref.type == "chart_spec"]
        bars = {chart["y"]: chart for chart in charts if chart["kind"] == "bar"}
        self.assertEqual(sorted(bars),
                         ["sales_amount", "sold_quantity", "weighted_avg_paid_price"],
                         "每个指标独立一张柱图，不共用一根轴")
        table_rows = {row["platform"]: row for row in dataset_rows(table)}
        ranking = {block["metric"]: block for block in
                   result.model_payload["ranking"]}
        for metric, spec in bars.items():
            self.assertEqual(spec["x"], "platform")
            self.assertEqual(spec["baseline"], "zero", "柱状图零基线（spec §8）")
            self.assertEqual(spec["dataset_ref"], str(table_ref.id),
                             "图表只引用已落库的那份表格")
            self.assertEqual(spec["coverage_ref"], spec["dataset_ref"],
                             "覆盖与数据同源：否则图上的洞与数字来自两个版本")
            self.assertEqual(spec["metric_basis"].split("|", 1)[0], metric)
            plotted = {row["platform"]: row["value"]
                       for row in ranking[metric]["rows"]}
            expected = {platform: str(row[metric])
                        for platform, row in table_rows.items()
                        if row.get(metric) is not None}
            self.assertEqual(plotted, expected,
                             f"{metric} 图上会画的点与表格行逐项相等，缺值不画成 0")
        self.assertNotIn(self.shops["5"], json.dumps(charts, ensure_ascii=False,
                                                     default=str))

    def test_single_platform_shop_drilldown_keeps_window_basis_and_scope(self):
        """「抖音各店比较」：shop 分组只认恰好一个平台，窗口与口径按本轮重算。"""
        for key, platform in (("1", "fxg"), ("2", "fxg"), ("3", "jd")):
            self._shop(key, platform=platform)
            self._cover(key)
        self._sale("1", "E1", quantity="1", amount="100")
        self._sale("2", "E2", quantity="4", amount="400", day=date(2026, 9, 3))
        self._sale("3", "E3", quantity="9", amount="900", day=date(2026, 9, 4))

        result = self._run(group_by="shop",
                           scope={"mode": "all_authorized", "platforms": ["fxg"]})
        payload = result.model_payload
        table = payload_for(result, "comparison_table")
        self.assertEqual({row["shop_ref"] for row in dataset_rows(table)},
                         {ref_for_key("shop", self.shops["1"]),
                          ref_for_key("shop", self.shops["2"])},
                         "另一个平台的店不进店铺分组：它根本不在本轮范围里")
        self.assertEqual(table["filters"]["start"], START.isoformat())
        self.assertEqual(table["filters"]["end"], END.isoformat())
        self.assertEqual({(str(item["metric"]), str(item["basis"]),
                           str(item["time_basis"]))
                          for item in table["basis"] if "shop_ref" in item},
                         {("sales_amount", "platform_payment/v1", "pay_time"),
                          ("sold_quantity", "platform_payment/v1", "pay_time"),
                          ("weighted_avg_paid_price", "platform_payment/v1",
                           "pay_time")},
                         "下钻沿用的是本轮已认证口径，不拿上一轮结果里的店当范围")
        self.assertNotIn(self.shops["3"], json.dumps(payload, ensure_ascii=False,
                                                     default=str))
        self.assertEqual(total_row(table)["weighted_avg_paid_price"], "100",
                         "(100+400)/5 才是两店总均价")

    # -- 部分来源 / 无权限 / 持久化失败 ---------------------------------

    def test_group_with_a_coverage_gap_publishes_no_group_number(self):
        """部分来源：同平台三家店一家缺覆盖 → 整组不发数，缺的店按原因列出。"""
        for key in ("1", "2", "3"):
            self._shop(key, platform="jd")
            self._cover(key)
            self._sale(key, f"E{key}", quantity="1", amount="100")
        self.conn.execute(
            "UPDATE bi.sync_state SET covered = tstzmultirange(tstzrange("
            "'2026-08-25 00:00+08','2026-09-02 00:00+08','[)')) "
            "WHERE shop_id=%s AND entity='orders'", (self.shops["3"],))
        result = self._run(metrics=["sales_amount"])
        payload = result.model_payload
        self.assertEqual(result.status.value, "missing_data", payload)
        self.assertEqual(result.artifacts, [],
                         "组内缺一家就不发这个平台的数：两家的和会被读成整个平台")
        self.assertTrue(any("不发布该分组数字" in text
                            for text in payload["limitations"]), payload["limitations"])
        # 下钻到店铺粒度：已评估的两家店照给，缺的那家逐家带原因。
        drilled = self._run(group_by="shop", metrics=["sales_amount"],
                            scope={"mode": "all_authorized", "platforms": ["jd"]})
        self.assertEqual({item["shop_ref"]: item["reason"]
                          for item in drilled.model_payload["excluded_scope"]},
                         {ref_for_key("shop", self.shops["3"]): "coverage_incomplete"})
        self.assertEqual({row["shop_ref"]
                          for row in dataset_rows(payload_for(drilled,
                                                              "comparison_table"))},
                         {ref_for_key("shop", self.shops["1"]),
                          ref_for_key("shop", self.shops["2"])})

    def test_comparison_forbids_a_shop_outside_the_authorized_set(self):
        self._five_platforms()
        outside = self._shop("9", platform="fxg")
        result = compare_performance(
            comparison_request(scope={"mode": "selected",
                                      "shop_refs": [ref_for_key("shop", outside)]}),
            self._context(allowed=self._allow("1", "2")))
        self.assertEqual(result.status.value, "failed")
        self.assertEqual(result.error.code, "forbidden")
        self.assertEqual(result.artifacts, [])
        self.assertEqual(result.model_payload, {"status": "failed"})

    def test_required_dataset_failure_fails_the_run_while_chart_failure_keeps_the_table(self):
        self._five_platforms()
        failed = self._run(store=_ArtifactStoreFails(
            forbidden_values=set(self.shops.values())), metrics=["sales_amount"])
        self.assertEqual(failed.status.value, "failed")
        self.assertEqual(failed.error.code, "artifact_persistence_failed")
        self.assertEqual(failed.artifacts, [])

        kept = self._run(store=_ChartStoreFails(
            forbidden_values=set(self.shops.values())), metrics=["sales_amount"])
        kinds = sorted(artifact.ref.type for artifact in kept.artifacts)
        self.assertEqual(kinds, ["comparison_table", "trend_series"],
                         "图表存不下只拿掉那一张，两份表格都在")
        self.assertTrue(
            any("只发表格" in text for text in kept.model_payload["limitations"]),
            kept.model_payload["limitations"])


@unittest.skipUnless(os.getenv("BI_TEST_ADMIN_DSN"), "未配置独立测试数据库")
class OperatorListingWorkflowTests(OperatorFixture):
    """上架复核域（`audit_listing_prices`）：四类路径与场景 4 / 5 / 6 / 9。

    本类的“正常路径”靠 `_register_listing_source` 在**测试进程内**登记一条合成来源；
    真实交付里没有任何已核验的渠道在售价来源，所以这些用例不构成任何平台的就绪证明。
    """

    def _run(self, store: MemoryQueryRunStore | None = None,
             allowed: frozenset[str] | None = None, **overrides: Any):
        return audit_listing_prices(audit_request(**overrides),
                                    self._context(store=store, allowed=allowed))

    def _shop_with_listing(self, key: str, *, platform: str = "tb",
                           skus: Sequence[str] = (SKU_A,),
                           listings: Sequence[str] | None = None,
                           prices: Mapping[str, str] | None = None,
                           captured_at: datetime = FRESH,
                           enumeration_complete: bool = True,
                           snapshot: bool = True,
                           with_trade: bool = True) -> str:
        """一家店 + 已映射链接（+ 一行成交让商品可解析）+ 一份在售快照。"""
        self._shop(key, platform=platform)
        self._cover(key)
        labels = list(listings or [f"L{key}-{index + 1}"
                                   for index in range(len(skus))])
        for index, sku in enumerate(skus):
            if with_trade:
                self._sale(key, f"E{key}{index}", sku=sku, quantity="1",
                           amount="19.90", cost="8")
            self._map(key, listing=labels[index], sku=sku, platform=platform)
        if not snapshot:
            return
        items = [{"listing_id": labels[index], "erp_sku_id": self._sku(sku),
                  "list_amount": (prices or {}).get(sku, "19.90")}
                 for index, sku in enumerate(skus)]
        self._snapshot(key, items=items, platform=platform,
                       captured_at=captured_at,
                       enumeration_complete=enumeration_complete)
        return self.shops[key]

    # -- 正常路径：全部匹配 ------------------------------------------------

    def test_price_audit_normal_path_publishes_a_complete_difference_table(self):
        for key in ("1", "2"):
            self._shop_with_listing(key)
        self._register_listing_source("tb")
        store = MemoryQueryRunStore(forbidden_values=set(self.shops.values()))
        result = self._run(store=store)
        payload = result.model_payload
        self.assertEqual(result.status.value, "success", payload["limitations"])
        audit = payload["audit"]
        self.assertEqual((audit["expected_items"], audit["evaluated_items"],
                          audit["matched_items"]), (2, 2, 2))
        self.assertTrue(audit["all_correct"],
                        "两格都有新鲜、完整且匹配的证据：这才是允许说“全部正确”的那一种情况")
        rows = audit_rows(payload)
        self.assertEqual({row["audit_status"] for row in rows}, {"match"})
        self.assertEqual({row["expected_amount"] for row in rows}, {"19.90"})
        # 来源、时点与规则版本跟着结果走：旧结果不能在新来源上复用。
        self.assertEqual(audit["rule_version"], LISTING_RULE_VERSION)
        self.assertTrue(audit["sources"], audit)
        provenance = store.runs[result.run_id]["provenance"]
        self.assertEqual(provenance.policy_version, LISTING_SOURCE_REGISTRY_VERSION)
        self.assertTrue(provenance.source_batches, "快照批次要进血缘")
        self.assertIsNotNone(result.data_as_of)

    # -- 场景 4：新上架无成交仍要进复核 ---------------------------------

    def test_new_sku_without_any_sale_still_enters_the_audit(self):
        """spec §10 第 4 行：没成交的新链接不从分母里静默消失。

        新 SKU 一行成交都没有，但有已确认的标识映射与全量枚举凭据：它必须出现在
        差异表上并被判成 `not_listed`，而不是"本轮没问这个规格"。
        """
        self._shop_with_listing("1", skus=(SKU_A,))
        # 第二个 SKU 只有一条已映射链接，一行成交都没有。
        self._map("1", listing="L1-NEW", sku=SKU_B, platform="tb")
        self._register_listing_source("tb")
        payload = self._run().model_payload
        by_sku = {str(row.get("sku_ref")): row["audit_status"]
                  for row in audit_rows(payload)}
        self.assertEqual(by_sku, {ref_for_key("sku", self._sku(SKU_A)): "match",
                                  ref_for_key("sku", self._sku(SKU_B)): "not_listed"})
        audit = payload["audit"]
        self.assertEqual(audit["expected_items"], 2)
        self.assertEqual(audit["counts"], {"match": 1, "not_listed": 1})
        self.assertFalse(audit["all_correct"])

    def test_conflicting_targets_for_one_sku_ask_instead_of_choosing(self):
        """同一格被两条不同金额命中：needs_input，代码不替用户选一个价。"""
        self._shop_with_listing("1", skus=(SKU_A,))
        self._register_listing_source("tb")
        result = self._run(expected_prices=[
            price_rule("19.90"),
            price_rule("29.90", applies_to="sku",
                       sku_ref=ref_for_key("sku", self._sku(SKU_A)))])
        self.assertEqual(result.model_payload["status"], "needs_input")
        self.assertNotIn("data", result.model_payload)
        self.assertEqual(
            self.conn.execute("SELECT count(*) FROM bi.expected_listing_rosters"
                              ).fetchone()[0], 0,
            "没定下标准之前连期望 roster 都不该落库")

    # -- 场景 5：目标 5 店只采到 4 店 -------------------------------------

    def test_missing_snapshot_for_one_shop_blocks_the_all_correct_claim(self):
        """spec §10 第 5 行：第五店标 unknown，不输出“全部正确”。"""
        for key in ("1", "2", "3", "4"):
            self._shop_with_listing(key)
        self._shop_with_listing("5", snapshot=False)
        self._register_listing_source("tb")
        result = self._run()
        payload = result.model_payload
        audit = payload["audit"]
        self.assertEqual(audit["expected_items"], 5, "分母是期望项，不是采到的项")
        self.assertEqual(audit["evaluated_items"], 4)
        self.assertFalse(audit["all_correct"])
        self.assertEqual(audit["counts"].get("unknown"), 1, audit["counts"])
        self.assertEqual(result.status.value, "partial", payload["limitations"])
        self.assertTrue(any("未判定项保持未知" in text
                            for text in payload["limitations"]), payload["limitations"])
        self.assertNotIn(LISTING_SOURCE_TEXT, payload["limitations"],
                         "这一格缺的是快照不是来源：两个原因不能互代")

    def test_complete_enumeration_without_the_item_is_not_listed_not_unknown(self):
        """同一格的另一副面孔：有全量枚举凭据时才能判 not_listed。"""
        for key in ("1", "2"):
            self._shop_with_listing(key)
        # 第三家有完整枚举但货架上没这个商品：它是确定结论，不是"没采到"。
        self._shop_with_listing("3", snapshot=False)
        self._snapshot("3", items=[], platform="tb", enumeration_complete=True)
        self._register_listing_source("tb")
        payload = self._run().model_payload
        rows = {row["shop_ref"]: row["audit_status"] for row in audit_rows(payload)}
        self.assertEqual(rows[ref_for_key("shop", self.shops["3"])], "not_listed")
        audit = payload["audit"]
        self.assertEqual(audit["counts"].get("not_listed"), 1)
        self.assertFalse(audit["all_correct"])

    # -- 场景 6：两 SKU 不同目标价 + 同 SKU 两链接 -----------------------

    def test_per_sku_targets_and_multiple_listings_keep_every_mismatch(self):
        """spec §10 第 6 行：逐 SKU / 逐链接比较，不取最便宜那条，也不合并。"""
        self._shop_with_listing("1", skus=(SKU_A, SKU_B),
                                listings=("LA", "LB"),
                                prices={SKU_A: "29.90", SKU_B: "19.90"})
        # 同一个 SKU 的第二条链接：价格不一致也得逐条出。
        self._map("1", listing="LC", sku=SKU_A, platform="tb")
        first_snapshot = self.conn.execute(
            "SELECT snapshot_id FROM bi.listing_snapshots WHERE shop_id=%s "
            "ORDER BY captured_at DESC LIMIT 1", (self.shops["1"],)).fetchone()[0]
        listing_repository.insert_listing_snapshot_item(
            self.conn, snapshot_id=first_snapshot, shop_id=self.shops["1"],
            namespace=DEFAULT_NAMESPACE, listing_id="LC",
            platform_sku_id="PS-LC-" + SKU_A, erp_sku_id=self._sku(SKU_A),
            list_amount="39.90", currency="CNY", on_sale=True, captured_at=FRESH)
        self._register_listing_source("tb")
        result = self._run(
            expected_prices=[price_rule("19.90", applies_to="sku",
                                        sku_ref=ref_for_key("sku", self._sku(SKU_A))),
                             price_rule("19.90", applies_to="sku",
                                        sku_ref=ref_for_key("sku", self._sku(SKU_B)))])
        payload = result.model_payload
        rows = audit_rows(payload)
        self.assertEqual(len(rows), 3, "两链接 + 一 SKU：三格都得在表上")
        by_listing = {row["listing_ref"]: row for row in rows}
        self.assertEqual(len(by_listing), 3, "同 SKU 的两条链接不能合并成一行")
        audit = payload["audit"]
        self.assertEqual(audit["expected_items"], 3)
        self.assertEqual(audit["counts"].get("mismatch"), 2, audit["counts"])
        self.assertEqual(audit["matched_items"], 1)
        self.assertFalse(audit["all_correct"])
        differences = sorted(str(row["amount_difference"])
                             for row in rows if row["audit_status"] == "mismatch")
        self.assertEqual(differences, ["10", "20"],
                         "两条不匹配各留自己的差额，不取最便宜那条也不平均")
        self.assertEqual(result.status.value, "success",
                         "每格都判完了：差异是发现，不是没做完（域状态 success）")
        self.assertEqual(payload["status"], "ok")

    # -- 场景 9：快照过期 -----------------------------------------------

    def test_stale_snapshot_is_reported_stale_not_correct(self):
        """spec §10 第 9 行（价审面）：过期只说 stale 与读取时刻，不判“正确”。"""
        self._shop_with_listing("1")
        self._shop_with_listing("2", captured_at=STALE)
        self._register_listing_source("tb")
        result = self._run()
        payload = result.model_payload
        rows = {row["shop_ref"]: row for row in audit_rows(payload)}
        self.assertEqual(rows[ref_for_key("shop", self.shops["2"])]["audit_status"],
                         "stale")
        self.assertIsNone(rows[ref_for_key("shop", self.shops["2"])]["actual_amount"],
                         "过期快照的采集价不能当本轮结论金额发出去")
        audit = payload["audit"]
        self.assertEqual(audit["expected_items"], 2)
        self.assertEqual(audit["evaluated_items"], 1)
        self.assertFalse(audit["all_correct"])
        self.assertIsNotNone(result.data_as_of, "读取时刻必须被披露")
        self.assertEqual(result.status.value, "partial", payload["limitations"])

    # -- 部分来源 / 无权限 / 持久化失败 ---------------------------------

    def test_unverified_source_for_one_platform_marks_cells_unsupported(self):
        """部分来源：只给 tb 登记了合成来源，fxg 那家只能报未取证。"""
        self._shop_with_listing("1", platform="tb")
        self._shop_with_listing("2", platform="fxg")
        self._register_listing_source("tb")
        result = self._run()
        payload = result.model_payload
        rows = {row["shop_ref"]: row["audit_status"] for row in audit_rows(payload)}
        self.assertEqual(rows[ref_for_key("shop", self.shops["1"])], "match")
        self.assertEqual(rows[ref_for_key("shop", self.shops["2"])], "unsupported")
        audit = payload["audit"]
        self.assertEqual((audit["expected_items"], audit["evaluated_items"]), (2, 1))
        self.assertFalse(audit["all_correct"])
        self.assertEqual(result.status.value, "partial", payload["limitations"])

    def test_without_any_verified_source_the_audit_reports_unsupported_only(self):
        """交付态：一条来源都没登记时，本域一个判定也不发（不把读不到演成未上架）。"""
        for key in ("1", "2"):
            self._shop_with_listing(key)
        result = self._run()
        payload = result.model_payload
        self.assertEqual(payload["audit"]["evaluated_items"], 0)
        self.assertEqual({row["audit_status"] for row in audit_rows(payload)},
                         {"unsupported"})
        self.assertFalse(payload["audit"]["all_correct"])
        self.assertEqual(result.status.value, "missing_data", payload["limitations"])
        self.assertIn(LISTING_SOURCE_TEXT, "；".join(payload["limitations"]))

    def test_forbidden_scope_reveals_nothing_about_the_other_shop(self):
        self._shop_with_listing("1")
        outside = self._shop_with_listing("2")
        self._register_listing_source("tb")
        result = audit_listing_prices(
            audit_request(scope={"mode": "selected",
                                 "shop_refs": [ref_for_key("shop", outside)]}),
            self._context(allowed=frozenset({self.shops["1"]})))
        self.assertEqual(result.status.value, "failed")
        self.assertEqual(result.error.code, "forbidden")
        self.assertEqual(result.model_payload, {"status": "failed"})
        self.assertEqual(result.artifacts, [])

    def test_audit_persistence_failure_is_not_reported_as_a_finished_review(self):
        self._shop_with_listing("1")
        self._register_listing_source("tb")
        result = self._run(store=_ArtifactStoreFails(
            forbidden_values=set(self.shops.values())))
        self.assertEqual(result.status.value, "failed")
        self.assertEqual(result.error.code, "artifact_persistence_failed")
        self.assertEqual(result.artifacts, [])
        self.assertNotIn("audit", result.model_payload)

    def test_missing_this_round_target_price_leaves_no_inherited_standard(self):
        """缺本轮目标价：needs_input 且**不写任何一份依据**（没继承就没有标准）。

        目标价只能来自当前这句话（计划 Task 9、spec §5.4）。上一轮的价既不会被存进
        会话（主层不回写），也不会从 `bi.price_audit_expectations` 的旧运行里摸回来：
        本用例在真库上看这两行分别属于谁。
        """
        from bi_agent.listing_audit.tool import execute_listing_audit_tool

        self._shop_with_listing("1")
        self._register_listing_source("tb")
        store = self._db_store()
        subject = self._db_subject

        def count(run: UUID) -> int:
            return int(self.conn.execute(
                "SELECT count(*) FROM bi.price_audit_expectations WHERE run_id=%s",
                (run,)).fetchone()[0])

        # 上一轮：用户当前这句话里给了价 → 依据冻结在那一次运行上。
        prior = ToolCall(id="call_prior", name="audit_listing_prices",
                         arguments=audit_request().model_dump(mode="json"))
        prior_run = execute_listing_audit_tool(
            prior, self._context(store=store, subject=subject)).domain_result
        self.assertEqual(prior_run.status.value, "success", prior_run.model_payload)
        self.assertEqual(count(prior_run.run_id), 1)

        # 这一轮：新一条用户消息，里没有价。系统不拿上一轮的 19.90 补一个标准。
        self.message_id = uuid4()
        self.conn.execute(
            "INSERT INTO bi.app_messages(id, chat_id, role, content, status) "
            "VALUES (%s, %s, 'user', '那现在都对吗', 'complete')",
            (self.message_id, self.chat_id))
        later = ToolCall(id="call_later", name="audit_listing_prices",
                         arguments={"product": {"text": "直钉枪"},
                                    "scope": {"mode": "all_authorized"}})
        later_result = execute_listing_audit_tool(
            later, self._context(store=store, subject=subject)).domain_result
        self.assertEqual(later_result.status.value, "needs_input")
        self.assertEqual(later_result.artifacts, [])
        self.assertEqual(count(later_result.run_id), 0,
                         "本轮没给价就不该有任何一份审计依据落库")


@unittest.skipUnless(os.getenv("BI_TEST_ADMIN_DSN"), "未配置独立测试数据库")
class OperatorInventoryWorkflowTests(OperatorFixture):
    """库存预警域（`inspect_inventory`）：四类路径与场景 7 / 8 / 9 / 11。

    与价审域同一取证边界：交付代码里没有任何已核验的库存来源，本类的“正常”靠
    测试进程内登记一条合成来源，不构成任何渠道 / ERP 库存已接入的证据。
    """

    def _run(self, store: MemoryQueryRunStore | None = None,
             allowed: frozenset[str] | None = None,
             pools: frozenset[str] | None = None, **overrides: Any):
        if "thresholds" not in overrides and "threshold_policy_ref" not in overrides:
            # 两个口径都要有自己的阈值（否则另一格只能报 unconfigured，测试意图就进了
            # "缺配置"那一途）：实物按 SKU 给，配额逐店给。
            overrides["thresholds"] = [
                threshold("low_replenish", self._sku(SKU_A), "20")]
            overrides["thresholds"] += [
                threshold("low_quota", self._sku(SKU_A), "20",
                          shop_ref=ref_for_key("shop", shop))
                for shop in sorted(self.shops.values())]
        overrides.setdefault("sku_refs", [ref_for_key("sku", self._sku(SKU_A))])
        return inspect_inventory(inventory_request(**overrides),
                                 self._context(store=store, allowed=allowed,
                                               pools=pools))

    def _shared_pool(self, *, physical: str = "100", channel: str = "100",
                     captured_at: datetime = FRESH, pool_key: str = "a",
                     skus: Sequence[str] = (SKU_A,)) -> None:
        """三店共用一个库存池：池里一份实物，三家渠道各展示一份。"""
        for key in ("1", "2", "3"):
            self._shop(key, platform="tb")
            self._cover(key)
            for sku in skus:
                self._sale(key, f"E{key}{sku}", sku=sku, quantity="1", amount="19.90",
                           cost="8")
        self._pool(pool_key, connection="shared", shops=("1", "2", "3"))
        for sku in skus:
            self._physical(pool_key, warehouse="wh-a", sku=sku, quantity=physical,
                           captured_at=captured_at)
        for key in ("1", "2", "3"):
            for sku in skus:
                self._channel_stock(key, sku=sku, quantity=channel,
                                    captured_at=captured_at)
        self._register_inventory_sources()

    # -- 正常路径与场景 7 -------------------------------------------------

    def test_shared_pool_is_counted_once_and_never_summed_per_shop(self):
        """spec §10 第 7 行：三店共仓 100 件、各显示 100 → 实物总量仍是 100。"""
        self._shared_pool()
        store = MemoryQueryRunStore(forbidden_values=set(self.shops.values()))
        result = self._run(store=store)
        payload = result.model_payload
        rows = alert_rows(payload)
        physical = [row for row in rows if row["level"] == "physical_total"]
        self.assertEqual(len(physical), 1, "实物那一格按池去重后只能有一行")
        self.assertEqual(physical[0]["quantity"], "100")
        self.assertEqual(physical[0]["batch_count"], 1)
        self.assertIn("pool_ref", physical[0])
        self.assertIn("warehouse_ref", physical[0])
        channels = [row for row in rows if row["level"] == "shop_sellable"]
        self.assertEqual(len(channels), 3, "渠道可售逐店各一行")
        self.assertEqual(sorted(str(row["channel_quantity"]) for row in channels),
                         ["100", "100", "100"])
        # 去重要按**值**钉：拿整个载荷扫"300"会撞进 Artifact 的 UUID（随机红），
        # 也不证明任何东西。实物那一格只能是自己那一份。
        self.assertEqual([str(row["quantity"]) for row in physical], ["100"],
                         "三家各展示 100 时实物总量仍是 100，不是 300（spec §5.5）")
        self.assertNotIn("300", [str(row["quantity"]) for row in rows]
                         + [str(row["channel_quantity"]) for row in rows],
                         "没有任何一格能把共享池按店加成三个 100")
        self.assertEqual({str(row["pool_ref"]) for row in physical},
                         {pool_handle(DEFAULT_NAMESPACE, self.pools["a"])},
                         "实物那一格只指向共享池自己：不逐店复制，也不拿另一个池作证")
        self.assertEqual(payload["inventory"]["expected_items"], 4)
        self.assertEqual(payload["inventory"]["evaluated_items"], 4)
        self.assertEqual(payload["inventory"]["threshold_source"], "this_turn")
        self.assertEqual(payload["inventory"]["rule_version"], INVENTORY_RULE_VERSION)
        # 正常路径还要看见结果真的落库了，并继承单版本换算表与快照批次：
        # 实物去重不能混批次，也不能拿上一版的换算规则读新的单位。
        types = sorted(artifact.ref.type for artifact in result.artifacts)
        self.assertEqual(types, ["inventory_alerts"])
        provenance = store.runs[result.run_id]["provenance"]
        self.assertEqual(provenance.policy_version, UNIT_CONVERSION_REGISTRY_VERSION)
        self.assertEqual(sorted(provenance.source_batches),
                         sorted(set(provenance.source_batches)),
                         "快照批次不得重复声明，也不得混进不属于本池的那一批")
        self.assertTrue(provenance.source_batches, "库存快照批次要进血缘")

    def test_all_safe_needs_every_cell_judged_normal_with_complete_evidence(self):
        """“全部安全”只给完整证据：全部 normal + 扫完 + 未截断。"""
        self._shared_pool(physical="500", channel="500")
        payload = self._run().model_payload
        inventory = payload["inventory"]
        self.assertTrue(inventory["all_safe"], inventory)
        self.assertFalse(inventory["truncated"])
        self.assertEqual(inventory["counts"].get("normal"), 4)

    def test_missing_threshold_returns_unconfigured_not_normal(self):
        """缺阈值就是 unconfigured：不替经营者设一个数，也不说“安全”。"""
        self._shared_pool()
        payload = self._run(thresholds=None).model_payload
        inventory = payload["inventory"]
        self.assertEqual(inventory["threshold_source"], "none")
        self.assertEqual(set(inventory["counts"]), {"unconfigured"})
        self.assertFalse(inventory["all_safe"])
        self.assertNotIn("都安全", json.dumps(payload, ensure_ascii=False, default=str))

    # -- 场景 8：渠道 0 / 实物充足 --------------------------------------

    def test_shop_zero_with_ample_shared_stock_is_a_quota_action_not_a_purchase(self):
        """spec §10 第 8 行：店铺配额候选，不能误判成必须采购。"""
        self._shared_pool(physical="100", channel="0")
        payload = self._run().model_payload
        rows = alert_rows(payload)
        actions = {(row["level"], str(row.get("action"))) for row in rows}
        self.assertIn(("shop_sellable", "quota_adjust"), actions)
        self.assertNotIn(("physical_total", "replenish"), actions,
                         "实物 100 高于阈值 20：这一格不是补货候选")
        limitations = "；".join(payload["limitations"])
        self.assertIn("店铺配额调整候选", limitations)
        self.assertNotIn("给出补货候选", limitations,
                         "配额缺口不能被说成采购动作：两个候选各自对应一个口径")

    def test_low_physical_stock_is_a_replenishment_candidate(self):
        self._shared_pool(physical="5", channel="100")
        payload = self._run().model_payload
        physical = next(row for row in alert_rows(payload)
                        if row["level"] == "physical_total")
        self.assertEqual(physical["inventory_status"], "low")
        self.assertEqual(physical["action"], "replenish")
        self.assertEqual(physical["quantity"], "5")
        self.assertFalse(payload["inventory"]["all_safe"])

    # -- 场景 4 / 11 与场景 9（库存面）---------------------------------

    def test_sku_without_any_sale_still_enters_the_inventory_check(self):
        """没成交的新 SKU 仍要能被盘点：目录/快照里有它，就不能从分母里消失。"""
        self._shared_pool(skus=(SKU_A,))
        # SKU_B：一行成交都没有，只有库存快照里的数量。
        self._physical("a", warehouse="wh-a", sku=SKU_B, quantity="3")
        self._channel_stock("1", sku=SKU_B, quantity="0")
        payload = self._run(sku_refs=[ref_for_key("sku", self._sku(SKU_A)),
                                      ref_for_key("sku", self._sku(SKU_B))],
                            thresholds=[
                                threshold("low_replenish", self._sku(SKU_A), "20"),
                                threshold("low_replenish", self._sku(SKU_B), "20"),
                                threshold("low_quota", self._sku(SKU_B), "20",
                                          shop_ref=ref_for_key("shop", self.shops["1"]))]
                            ).model_payload
        skus = {str(row["sku_ref"]) for row in alert_rows(payload)}
        self.assertIn(ref_for_key("sku", self._sku(SKU_B)), skus)
        b_rows = [row for row in alert_rows(payload)
                  if row["sku_ref"] == ref_for_key("sku", self._sku(SKU_B))]
        self.assertEqual({row["level"] for row in b_rows},
                         {"physical_total", "shop_sellable"},
                         "两个口径各自一行：实物低与渠道缺不是一个问题")

    def test_stale_inventory_snapshot_is_stale_not_safe(self):
        """spec §10 第 9 行（库存面）：过期只说 stale 与快照时刻。"""
        self._shared_pool(captured_at=STALE)
        result = self._run()
        payload = result.model_payload
        inventory = payload["inventory"]
        self.assertEqual(inventory["counts"].get("stale"), 4, inventory["counts"])
        self.assertEqual(inventory["evaluated_items"], 0,
                         "过期那一格不算已评估：分母仍是期望项")
        self.assertEqual(inventory["expected_items"], 4)
        self.assertFalse(inventory["all_safe"])
        self.assertTrue(all("snapshot_at" in row for row in alert_rows(payload)))
        self.assertEqual(result.status.value, "missing_data", payload["limitations"])

    def test_unauthorized_pool_is_excluded_without_leaking_its_contents(self):
        """spec §10 第 11 行（库存面）：拿到三家店不等于能看全公司仓库。"""
        self._shared_pool()
        payload = self._run(pools=frozenset()).model_payload
        inventory = payload["inventory"]
        self.assertEqual(inventory["evaluated_items"], 3,
                         "只剩渠道可售那一半：实物那一格因缺池授权不判")
        self.assertFalse(inventory["all_safe"])
        self.assertTrue(inventory.get("excluded_pools"),
                        "未获准的池要能被数出来，不然“少了一格”没有去处")
        self.assertEqual(inventory["excluded_pools"][0]["reason"], "pool_not_authorized")
        quantities = [row.get("quantity") for row in alert_rows(payload)
                      if row["level"] == "physical_total"]
        self.assertTrue(all(value is None for value in quantities), quantities)

    # -- 部分来源 / 无权限 / 持久化失败 ---------------------------------

    def test_only_one_level_verified_leaves_the_other_level_unsupported(self):
        """部分来源：只登记渠道可售的取证时，实物那一格仍不能判。"""
        self._shared_pool()
        reset_inventory_sources()
        self._register_inventory_sources("shop_sellable")
        payload = self._run().model_payload
        inventory = payload["inventory"]
        self.assertEqual(inventory["counts"].get("unsupported"), 1, inventory["counts"])
        self.assertEqual(inventory["evaluated_items"], 3)
        self.assertEqual(inventory["expected_items"], 4)
        self.assertEqual(inventory["levels"], ["physical_total", "shop_sellable"])
        self.assertFalse(inventory["all_safe"])
        self.assertEqual(payload["status"], "partial")

    def test_without_any_verified_source_inventory_reports_unsupported_only(self):
        """交付态：两条来源都没登记时，本域一个判定也不发。"""
        self._shared_pool()
        reset_inventory_sources()
        result = self._run()
        payload = result.model_payload
        self.assertEqual(payload["inventory"]["evaluated_items"], 0)
        self.assertEqual({row["inventory_status"] for row in alert_rows(payload)},
                         {"unsupported"})
        self.assertIn(INVENTORY_SOURCE_TEXT, "；".join(payload["limitations"]))
        self.assertEqual(result.status.value, "missing_data", payload["limitations"])

    def test_inventory_forbids_a_shop_outside_the_authorized_set(self):
        self._shared_pool()
        outside = self._shop("9", platform="tb")
        result = inspect_inventory(
            inventory_request(sku_refs=[ref_for_key("sku", self._sku(SKU_A))],
                             scope={"mode": "selected",
                                    "shop_refs": [ref_for_key("shop", outside)]}),
            self._context(allowed=self._allow("1")))
        self.assertEqual(result.status.value, "failed")
        self.assertEqual(result.error.code, "forbidden")
        self.assertEqual(result.artifacts, [])
        self.assertEqual(result.model_payload, {"status": "failed"})

    def test_inventory_alert_persistence_failure_is_not_reported_as_safe(self):
        self._shared_pool(physical="500", channel="500")
        result = self._run(store=_ArtifactStoreFails(
            forbidden_values=set(self.shops.values())))
        self.assertEqual(result.status.value, "failed")
        self.assertEqual(result.error.code, "artifact_persistence_failed")
        self.assertEqual(result.artifacts, [])
        self.assertNotIn("inventory", result.model_payload)

    def test_configured_threshold_policy_is_used_when_no_inline_input(self):
        """没有本轮 inline 阈值时用已配置策略：两者同给就是两套分母，入口就拒。"""
        self._shared_pool(physical="500", channel="500")
        self._threshold_policy(sku=SKU_A, level="low_replenish", quantity="999",
                              pool_key="a")
        payload = self._run(thresholds=None,
                            threshold_policy_ref="operator-default/1").model_payload
        inventory = payload["inventory"]
        self.assertEqual(inventory["threshold_source"], "configured")
        physical = next(row for row in alert_rows(payload)
                        if row["level"] == "physical_total")
        self.assertEqual(physical["inventory_status"], "low",
                         "500 < 999：策略阈值真被用上了，不是当没配置")
        with self.assertRaises(ValueError):
            inventory_request(sku_refs=[ref_for_key("sku", self._sku(SKU_A))],
                              thresholds=[threshold("low_replenish",
                                                    self._sku(SKU_A), "20")],
                              threshold_policy_ref="operator-default/1")


@unittest.skipUnless(os.getenv("BI_TEST_ADMIN_DSN"), "未配置独立测试数据库")
class OperatorAgentRoutingTests(OperatorFixture):
    """主层路由：一句运营提问 → 一次业务 Tool → 一份完整报告。

    模型侧是脚本替身：它们证明主层的公告 / 分发 / 上下文注入 / 降级契约成立，
    **不**证明真实模型会选对工具（真实模型 26 题本轮未执行）。
    """

    def _shop_refs(self, *keys: str) -> list[str]:
        return [ref_for_key("shop", self.shops[key]) for key in keys]

    # -- 五个路由场景 -----------------------------------------------------

    def test_product_question_over_all_shops_uses_one_product_tool_call(self):
        self._archive(PRODUCT, "直钉枪")
        for key in ("1", "2", "3"):
            self._shop(key)
            self._cover(key)
            self._sale(key, f"E{key}", quantity="1", amount="100", cost="40")
        calls = [
            _reply(calls=[_call("analyze_product_performance", {
                "product": {"text": "直钉枪"},
                "scope": {"mode": "all_authorized"},
                "start": START.isoformat(), "end": END.isoformat(),
                "metrics": ["sold_quantity", "sales_amount"],
                "sales_basis": "erp_effective_parent", "profit_basis": "none"})]),
            _reply(text="三家店的销量见下方表格")]
        turn = self._turn("直钉枪在所有店铺近七天卖得怎么样？", calls)
        self.assertEqual(len(turn.artifacts), 2, "一次调用就拿到汇总 + 七日趋势")
        self.assertEqual(sorted(item["artifact_type"] for item in turn.artifacts),
                         ["metric_result", "trend_series"])
        self.assertIn("验收店", json.dumps(turn.artifacts[0]["entities"],
                                          ensure_ascii=False),
                      "展示侧已换真名，模型侧不会看到")
        self.assertNotIn("直钉枪", str(turn.state.turns[1].content))
        self.assertIsNone(turn.error_code)

    def test_five_platform_question_routes_to_platform_grouping_with_charts(self):
        for index, platform in enumerate(COMPARABLE_PLATFORMS):
            key = str(index + 1)
            self._shop(key, platform=platform)
            self._cover(key)
            self._sale(key, f"E{index}", quantity="1", amount="100")
        calls = [
            _reply(calls=[_call("compare_performance", {
                "scope": {"mode": "all_authorized"},
                "start": START.isoformat(), "end": END.isoformat(),
                "group_by": "platform",
                "metrics": ["sales_amount", "sold_quantity"],
                "sales_basis": "erp_effective_parent", "profit_basis": "none"})]),
            _reply(text="四个平台见下方对比图")]
        turn = self._turn("五个平台近七天哪个卖得好？", calls)
        types = sorted(item["artifact_type"] for item in turn.artifacts)
        self.assertIn("chart_spec", types)
        self.assertIn("comparison_table", types)
        table = artifact_of(turn.artifacts, "comparison_table")
        chart = next(item for item in turn.artifacts
                     if item["artifact_type"] == "chart_spec" and item["kind"] == "bar")
        self.assertEqual(chart["dataset_ref"], table["artifact_id"],
                         "同一条消息里图表引用的就是这张表")
        self.assertEqual(chart["x"], "platform")
        rows = {row["platform"]: row["sales_amount"] for row in dataset_rows(table)}
        self.assertEqual(rows, {platform: "100" for platform in COMPARABLE_PLATFORMS})

    def test_one_platform_shop_question_routes_to_shop_grouping(self):
        for key in ("1", "2"):
            self._shop(key)
            self._cover(key)
        for key, amount in (("1", "100"), ("2", "400")):
            self._sale(key, f"E{key}", quantity="1" if key == "1" else "4",
                       amount=amount, day=date(2026, 9, 2) if key == "1" else
                       date(2026, 9, 3))
        self._shop("3", platform="jd")
        self._cover("3")
        self._sale("3", "E3", quantity="9", amount="900", day=date(2026, 9, 4))
        calls = [
            _reply(calls=[_call("compare_performance", {
                "scope": {"mode": "all_authorized", "platforms": ["fxg"]},
                "start": START.isoformat(), "end": END.isoformat(),
                "group_by": "shop", "metrics": ["sales_amount"],
                "sales_basis": "erp_effective_parent", "profit_basis": "none"})]),
            _reply(text="两店对比见表")]
        turn = self._turn("比较一下抖音各店铺的支付金额", calls)
        table = artifact_of(turn.artifacts, "comparison_table")
        self.assertEqual({row["shop_ref"] for row in dataset_rows(table)},
                         set(self._shop_refs("1", "2")),
                         "单平台下钻不会把另一个平台的店拉进来")
        self.assertEqual(total_row(table)["sales_amount"], "500")

    def test_listing_target_price_comes_from_the_current_message_only(self):
        """「A 的该 SKU 全店标价 19.90 是否正确」：本轮有价才能审，下轮没价就问。"""
        self._shop("1", platform="tb")
        self._cover("1")
        self._sale("1", "E1", sku=SKU_A, quantity="1", amount="19.90", cost="8")
        self._map("1", listing="L1", sku=SKU_A, platform="tb")
        self._snapshot("1", items=[{"listing_id": "L1",
                                     "erp_sku_id": self._sku(SKU_A),
                                     "list_amount": "19.90"}], platform="tb")
        self._register_listing_source("tb")
        first = self._turn(
            "直钉枪 6mm 在获准店铺的标价是 19.90，都对不对？",
            [_reply(calls=[_call("audit_listing_prices", {
                "product": {"text": "直钉枪"},
                "scope": {"mode": "all_authorized"},
                "expected_prices": [price_rule("19.90")],
                "price_basis": "list_price", "as_of": "latest"})]),
             _reply(text="这一格一致")])
        audit = artifact_of(first.artifacts, "price_audit")
        self.assertEqual(audit["audit"]["all_correct"], True, audit["audit"])
        self.assertEqual(audit["filters"]["expected_prices"][0]["expected_amount"],
                         "19.90")
        # 本轮没给价：主层不会把上一轮的 19.90 当本轮标准（不回写、不继承）。
        self.assertNotIn("expected_prices", str(first.state.filters))
        second = self._turn(
            "那现在都还对吗？",
            [_reply(calls=[_call("audit_listing_prices", {
                "product": {"text": "直钉枪"},
                "scope": {"mode": "all_authorized"}}),
            ]), _reply(text="目标价需要你确认")],
            state=first.state)
        self.assertEqual(second.artifacts, [],
                         "缺本轮目标价不发差异表：上一轮的价不会被隐式继承")
        tool_messages = [message for message in second.state.turns
                         if message.role == "tool"]
        self.assertTrue(any("needs_input" in str(message.content)
                            for message in tool_messages),
                        "工具必须把缺价说成 needs_input，而不是静默沿用上一轮")
        self.assertNotIn("19.90", str(second.state.filters))
        self.assertEqual(
            self.conn.execute(
                "SELECT count(*) FROM bi.price_audit_expectations").fetchone()[0], 0,
            "内存 Store 不落库；真库的『本轮不写依据』在域内用例里钉")

    def test_stock_question_routes_to_inventory_alerts(self):
        """「全商品总库存和店铺预警」：一次调用同时拿两级，且不拿池当已授权。"""
        for key in ("1", "2"):
            self._shop(key, platform="tb")
            self._cover(key)
            self._sale(key, f"E{key}", sku=SKU_A, quantity="1", amount="19.90")
        self._pool("a", connection="shared", shops=("1", "2"))
        self._physical("a", warehouse="wh-a", sku=SKU_A, quantity="100")
        for key in ("1", "2"):
            self._channel_stock(key, sku=SKU_A, quantity="0" if key == "1" else "50")
        self._register_inventory_sources()
        calls = [
            _reply(calls=[_call("inspect_inventory", {
                "products": "all", "scope": {"mode": "all_authorized"},
                "levels": ["physical_total", "shop_sellable"], "as_of": "latest"})]),
            _reply(text="预警见下表")]
        turn = self._turn("看一下全商品总库存和店铺预警", calls)
        alerts = artifact_of(turn.artifacts, "inventory_alerts")
        self.assertEqual(alerts["inventory"]["levels"],
                         ["physical_total", "shop_sellable"])
        physical = [row for row in alert_rows(alerts) if row["level"] == "physical_total"]
        channels = [row for row in alert_rows(alerts)
                    if row["level"] == "shop_sellable"]
        # 主层本轮没有任何池授权配置：实物那一格只能未知，不能被当成 0 或缺货。
        self.assertEqual([row["inventory_status"] for row in physical],
                         ["unknown"] * len(physical) if physical else [])
        self.assertTrue(channels, "店铺可售不依赖池授权，本轮仍可判")
        self.assertFalse(alerts["inventory"]["all_safe"])

    # -- 调用次数与预算 ---------------------------------------------------

    def _comparison_calls(self, shop_count: int) -> tuple[int, int, int]:
        """返回 (业务 Tool 调用次数, 经营取数 SQL 条数, 分组行数)。"""
        for index in range(shop_count):
            key = str(index + 1)
            self._shop(key, platform="jd")
            self._cover(key)
            self._sale(key, f"E{index}", quantity="1", amount="100")
        counting = _CountingConn(self.conn)
        calls = [
            _reply(calls=[_call("compare_performance", {
                "scope": {"mode": "all_authorized", "platforms": ["jd"]},
                "start": START.isoformat(), "end": END.isoformat(),
                "group_by": "shop", "metrics": ["sales_amount"],
                "sales_basis": "erp_effective_parent", "profit_basis": "none"})]),
            _reply(text="各店对比见表")]
        # `group_by=shop` 要求恰好一个平台：这句就是图上点一下京东柱子发的那一句。
        turn = self._turn("比较一下京东各店的支付金额", calls, conn=counting)
        table = artifact_of(turn.artifacts, "comparison_table")
        return (len(turn.artifacts), counting.aggregate_reads(),
                len(dataset_rows(table)))

    def test_one_business_tool_call_per_request_regardless_of_shop_count(self):
        """计划 Task 11：每个完整业务请求通常只需一次业务 Tool，且不随店数增长。

        两条分别钉两件事：主层只递了一次工具调用；图内取数条数与店铺数无关
        （一条 `shop_id = ANY(%s)` 集合查询）。
        """
        small_artifacts, small_reads, small_rows = self._comparison_calls(2)
        large_artifacts, large_reads, large_rows = self._comparison_calls(7)
        self.assertGreater(small_reads, 0, "先确保这条计数不是在比两个 0")
        self.assertEqual(small_rows, 2)
        self.assertEqual(large_rows, 7, "七家店一次就都到了：主 Agent 不逐店循环")
        self.assertEqual(small_artifacts, large_artifacts,
                         "店数变了不多发卡片：一次工具调用 = 一次图执行")
        self.assertEqual(small_reads, large_reads,
                         "事实取数 SQL 条数不随店铺数线性增长")

    def test_budget_exhaustion_discloses_the_unfinished_scope_without_numbers(self):
        """30 秒预算不够时说“没完成”，不缩范围、不改窗口、不把 partial 演成 ok。"""
        self._archive(PRODUCT, "直钉枪")
        self._shop("1")
        self._cover("1")
        self._sale("1", "E1", quantity="1", amount="100", cost="40")
        calls = [_reply(calls=[_call("analyze_product_performance", {
            "product": {"text": "直钉枪"}, "scope": {"mode": "all_authorized"},
            "start": START.isoformat(), "end": END.isoformat(),
            "metrics": ["sales_amount"], "sales_basis": "erp_effective_parent",
            "profit_basis": "none"})])]
        turn = self._turn("直钉枪近七天卖得怎么样", calls, deadline_patch=0.0)
        self.assertIn("预算", turn.text)
        self.assertEqual(turn.artifacts, [])
        self.assertNotIn("sales_amount", json.dumps(turn.artifacts, ensure_ascii=False))

    def test_model_sees_refs_while_the_display_side_sees_names(self):
        """同一份数字的两个出口：模型只拿引用，展示层才拿真名。"""
        self._archive(PRODUCT, "直钉枪")
        self._shop("1")
        self._cover("1")
        self._sale("1", "E1", quantity="1", amount="100", cost="40")
        calls = [_reply(calls=[_call("analyze_product_performance", {
            "product": {"text": "直钉枪"}, "scope": {"mode": "all_authorized"},
            "start": START.isoformat(), "end": END.isoformat(),
            "metrics": ["sales_amount"], "sales_basis": "erp_effective_parent",
            "profit_basis": "none"})]),
            _reply(text="已查询")]
        turn = self._turn("直钉枪卖得如何", calls)
        tool_messages = [message for message in turn.state.turns
                         if message.role == "tool"]
        self.assertTrue(tool_messages)
        model_side = tool_messages[-1].content or ""
        self.assertNotIn("验收店", model_side)
        self.assertNotIn(self.shops["1"], model_side, "ERP 店铺主键不进模型历史")
        self.assertIn("验收店", json.dumps(turn.artifacts[0]["entities"],
                                          ensure_ascii=False))


@unittest.skipUnless(os.getenv("BI_TEST_ADMIN_DSN"), "未配置独立测试数据库")
class OperatorAnalysisWorkflowTests(OperatorFixture):
    """隔离分析 Task 5 Step 6 的四工作流回归：分析门禁开着时，四个既有运营
    工作流的分发与产物逐字不变。

    每个工作流跑两遍（门禁关 / 开），脚本模型都只调固定 Tool：两遍的 Artifact
    类型与错误状态必须完全一致；开着时唯一差异是模型多看到一个只读
    `analyze_artifact`。分析图自身的门禁与持久化由 tests.test_analysis 钉。
    """

    def _assert_gate_on_matches_baseline(self, question: str, replies,
                                         expected_types: list[str]):
        baseline = self._turn(question, replies, isolated_analysis_enabled=False)
        enabled = self._turn(question, replies, isolated_analysis_enabled=True)
        self.assertEqual([item["artifact_type"] for item in enabled.artifacts],
                         expected_types)
        self.assertEqual([item["artifact_type"] for item in baseline.artifacts],
                         expected_types,
                         "先确认基线本身拿到预期产物，再比对门禁差异")
        self.assertEqual((baseline.error_code, baseline.text),
                         (enabled.error_code, enabled.text),
                         "分析门禁不得改变既有工作流的回答")
        offered = [item["function"]["name"] for item in
                   self.last_model.complete.call_args_list[0].args[1]]
        self.assertEqual(offered[-1], "analyze_artifact", "开着时只追加这一个只读 Tool")
        self.assertNotIn("analysis_result",
                         json.dumps(enabled.artifacts, ensure_ascii=False),
                         "脚本模型没调分析 Tool，就不该有分析产物")

    def test_product_workflow_is_unchanged_with_the_gate_on(self):
        self._archive(PRODUCT, "直钉枪")
        for key in ("1", "2"):
            self._shop(key)
            self._cover(key)
            self._sale(key, f"E{key}", quantity="1", amount="100", cost="40")
        replies = [
            _reply(calls=[_call("analyze_product_performance", {
                "product": {"text": "直钉枪"},
                "scope": {"mode": "all_authorized"},
                "start": START.isoformat(), "end": END.isoformat(),
                "metrics": ["sold_quantity", "sales_amount"],
                "sales_basis": "erp_effective_parent", "profit_basis": "none"})]),
            _reply(text="两家店的销量见下方表格")]
        self._assert_gate_on_matches_baseline(
            "直钉枪在所有店铺近七天卖得怎么样？", replies,
            ["metric_result", "trend_series"])

    def test_comparison_workflow_is_unchanged_with_the_gate_on(self):
        for key, amount in (("1", "100"), ("2", "400")):
            self._shop(key)
            self._cover(key)
            self._sale(key, f"E{key}", quantity="1" if key == "1" else "4",
                       amount=amount)
        replies = [
            _reply(calls=[_call("compare_performance", {
                "scope": {"mode": "all_authorized"},
                "start": START.isoformat(), "end": END.isoformat(),
                "group_by": "platform",
                "metrics": ["sales_amount"],
                "sales_basis": "erp_effective_parent", "profit_basis": "none"})]),
            _reply(text="对比见下表")]
        self._assert_gate_on_matches_baseline(
            "各平台支付金额对比", replies,
            ["comparison_table", "trend_series", "chart_spec", "chart_spec"])

    def test_listing_workflow_is_unchanged_with_the_gate_on(self):
        self._shop("1", platform="tb")
        self._cover("1")
        self._sale("1", "E1", sku=SKU_A, quantity="1", amount="19.90", cost="8")
        self._map("1", listing="L1", sku=SKU_A, platform="tb")
        self._snapshot("1", items=[{"listing_id": "L1",
                                     "erp_sku_id": self._sku(SKU_A),
                                     "list_amount": "19.90"}], platform="tb")
        self._register_listing_source("tb")
        replies = [
            _reply(calls=[_call("audit_listing_prices", {
                "product": {"text": "直钉枪"},
                "scope": {"mode": "all_authorized"},
                "expected_prices": [price_rule("19.90")],
                "price_basis": "list_price", "as_of": "latest"})]),
            _reply(text="这一格一致")]
        self._assert_gate_on_matches_baseline(
            "直钉枪 6mm 在获准店铺的标价是 19.90，都对不对？", replies,
            ["price_audit"])

    def test_inventory_workflow_is_unchanged_with_the_gate_on(self):
        for key in ("1", "2"):
            self._shop(key, platform="tb")
            self._cover(key)
            self._sale(key, f"E{key}", sku=SKU_A, quantity="1", amount="19.90")
        self._pool("a", connection="shared", shops=("1", "2"))
        self._physical("a", warehouse="wh-a", sku=SKU_A, quantity="100")
        for key in ("1", "2"):
            self._channel_stock(key, sku=SKU_A,
                                quantity="0" if key == "1" else "50")
        self._register_inventory_sources()
        replies = [
            _reply(calls=[_call("inspect_inventory", {
                "products": "all", "scope": {"mode": "all_authorized"},
                "levels": ["physical_total", "shop_sellable"],
                "as_of": "latest"})]),
            _reply(text="预警见下表")]
        self._assert_gate_on_matches_baseline(
            "看一下全商品总库存和店铺预警", replies, ["inventory_alerts"])


@unittest.skipUnless(os.getenv("BI_TEST_ADMIN_DSN"), "未配置独立测试数据库")
class OperatorMultiSourceGuardrailTests(OperatorFixture):
    """计划 Task 11 的“验收口径补充”：逐店 basis、混口径不合并、换源不复用、拼多多边界。

    口径依据：[多来源指标设计](../../docs/superpowers/specs/
    2026-09-12-multi-source-metrics-design.md) §3–§6 与 2026-09-12 不接入拼多多的决定。
    """

    def test_certified_and_unverified_payment_windows_share_no_total_or_ranking(self):
        """同为支付口径但认证状态不同（抖音已逐元对照、京东未逐店对照）：不合计也不排名。"""
        self._shop("1", platform="fxg")
        self._shop("2", platform="jd")
        for key in ("1", "2"):
            self._cover(key)
            self._sale(key, f"E{key}", quantity="1", amount="100")
        result = compare_performance(
            comparison_request(metrics=["sales_amount"]), self._context())
        payload = result.model_payload
        table = payload_for(result, "comparison_table")
        rows = {row["platform"]: row["sales_amount"] for row in dataset_rows(table)}
        self.assertEqual(rows, {"fxg": "100", "jd": "100"},
                         "各分组自己的数照发：不可比不等于没数据")
        self.assertIsNone(total_row(table)["sales_amount"],
                          "合计那一格发 null：两口径的和不是“全平台支付额”")
        ranking = {block["metric"]: block for block in payload["ranking"]}
        sales = ranking["sales_amount"]
        self.assertEqual(sales["status"], "incomparable",
                         "排名块保留分母与原因，但一个名次也不给")
        self.assertEqual([row["rank"] for row in sales["rows"]], [None, None],
                         "认证状态不同的两根柱子不同一排序")
        self.assertEqual([row["value"] for row in sales["rows"]], [None, None])
        self.assertEqual({str(item["status"]) for item in payload["metric_statuses"]
                          if "shop_ref" in item}, {"incomparable"},
                         payload["metric_statuses"])
        self.assertTrue(any("口径互不兼容" in text or "不能汇总" in text
                            for text in payload["limitations"]), payload["limitations"])

    def test_taoxi_outstock_is_excluded_from_a_platform_payment_total_not_zeroed(self):
        """fxg 支付 + tb 出库：tb 整组因时间口径不成立被排除，不拿出库金额补支付数。"""
        self._shop("1", platform="fxg")
        self._shop("2", platform="tb")
        for key in ("1", "2"):
            self._cover(key)
            self._sale(key, f"E{key}", quantity="1", amount="100")
        result = compare_performance(
            comparison_request(metrics=["sales_amount"], group_by="platform"),
            self._context())
        payload = result.model_payload
        table = payload_for(result, "comparison_table")
        self.assertEqual({row["platform"] for row in dataset_rows(table)}, {"fxg"})
        self.assertEqual(total_row(table)["sales_amount"], "100",
                         "合计只覆盖已评估分组：它不是“所有平台合计”")
        self.assertEqual({str(item["reason"]) for item in payload["excluded_scope"]},
                         {"coverage_time_basis_unverified"})
        groups = {str(item["platform"]): item for item in payload["group_statuses"]
                  if "platform" in item}
        self.assertEqual(groups["tb"]["shops_evaluated"], 0)
        self.assertNotEqual(groups["tb"]["status"], "complete")
        bases = {(str(item["shop_ref"]), str(item["basis"]), str(item["time_basis"]))
                 for item in table["basis"] if "shop_ref" in item}
        self.assertEqual(bases,
                         {(ref_for_key("shop", self.shops["1"]),
                           "platform_payment/v1", "pay_time")},
                         "每一条已发结果都带着自己的 basis 与时间归属")

    def test_commerce_lineage_inherits_source_time_basis_and_capability_version(self):
        """跳领域 Artifact 继承来源 / 时间口径 / 能力版本：旧结果不得在换源后复用。"""
        self._archive(PRODUCT, "直钉枪")
        self._shop("1")
        self._cover("1")
        self._sale("1", "E1", quantity="1", amount="100", cost="40")
        store = MemoryQueryRunStore(forbidden_values=set(self.shops.values()))
        first = analyze_product_performance(
            product_request(metrics=["sales_amount"]), self._context(store=store))
        provenance = store.runs[first.run_id]["provenance"].fingerprint_parts()
        self.assertEqual(provenance["source_registry_version"], SOURCE_REGISTRY_VERSION)
        self.assertEqual(provenance["basis_signature"],
                         ("sales_amount|platform_payment/v1|pay_time",),
                         "签名只留 指标|口径|时间归属：主键与接口方法名进不了血缘表")
        self.assertTrue(provenance["source_batches"])
        first_fingerprint = store.runs[first.run_id]["request_fingerprint"]

        # 换源：把这家店改到出库通道（真实发生过通道切换的形状）。
        self.conn.execute("UPDATE bi.shops SET platform='tb' WHERE shop_id=%s",
                          (self.shops["1"],))
        self.conn.execute(
            "INSERT INTO bi.sync_state(source, entity, shop_id, watermark, covered, "
            "data_as_of, quality_status, quality_rule) "
            "SELECT %s, entity, shop_id, watermark, covered, data_as_of, "
            "'passed', %s FROM bi.sync_state WHERE shop_id=%s "
            "ON CONFLICT (source, entity, shop_id) DO NOTHING",
            (OUTSTOCK_SOURCE, QUALITY_RULE, self.shops["1"]))
        self.conn.execute(
            "UPDATE bi.orders SET source=%s WHERE shop_id=%s", (OUTSTOCK_SOURCE,
                                                                self.shops["1"]))
        second_store = MemoryQueryRunStore(forbidden_values=set(self.shops.values()))
        second = analyze_product_performance(
            product_request(metrics=["sales_amount"]),
            self._context(store=second_store))
        self.assertNotEqual(first_fingerprint,
                            second_store.runs[second.run_id]["request_fingerprint"],
                            "换源后同一句话不再是同一个请求：旧结果不得命中")

    def test_pdd_has_no_payment_path_and_only_appears_as_an_explicit_gap(self):
        """拼多多的永久口径：不接入支付，只以显式缺失组 / unsupported 出现。"""
        pdd = ShopRecord.from_row("PDD1", "pdd", ["paid_amount", "erp_documents",
                                                  "quantity", "refund_amount"])
        self.assertEqual(registration("pdd").payment_basis, None)
        for metric in sorted(PAYMENT_FAMILY):
            self.assertEqual(resolve_metric_sources(pdd, metric), (),
                             f"pdd 的 {metric} 永远解析不出依赖（不接入，非延后）")
        self.assertTrue(resolve_metric_sources(pdd, "erp_documents"),
                        "单据口径仍可在逐店取证成立时回答")
        self.assertEqual(unsupported_reason(pdd, "paid_amount"), "capability_unavailable")

        self._shop("1")
        self._cover("1")
        self._sale("1", "E1", quantity="1", amount="100")
        self._shop("2", platform="pdd", capabilities=PDD_CAPABILITIES)
        self._cover("2")
        self._sale("2", "E2", quantity="9", amount="900", source=OUTSTOCK_SOURCE)
        payload = compare_performance(
            comparison_request(metrics=["sales_amount"]), self._context()).model_payload
        excluded = {str(item["platform"]): str(item["reason"])
                    for item in payload["excluded_scope"]}
        self.assertEqual(excluded, {"pdd": "coverage_time_basis_unverified"})
        self.assertNotIn("900", [str(row.get("sales_amount"))
                                 for row in dataset_rows(payload)],
                         "拼多多的行不得被补成一个支付数")
        self.assertEqual(payload["evaluated_scope"]["shop_refs"],
                         [ref_for_key("shop", self.shops["1"])],
                         "已评估集合只到能答的这一家：被排除的店不进数也不进合计")

    def test_no_pdd_connector_credential_or_onboarding_path_exists(self):
        """范围决定的代码面守护：注册表里没 pdd 支付分支，也没"等授权"的说法。

        拼多多的不接入是一个**结论**而不是一个待办：本轮不新增连接器、来源、凭证或
        onboarding 代码路径，文档与代码都只能写"不可用/已排除"。
        """
        import inspect

        import bi_agent.sources as sources_module

        reg = registration("pdd")
        self.assertEqual(reg.ceiling, PDD_CEILING)
        self.assertEqual(PDD_CEILING, frozenset({"erp_documents"}))
        self.assertIsNone(reg.payment_basis)
        self.assertEqual(reg.order_source, OUTSTOCK_SOURCE,
                         "订单实体只能从出库通道取：不拿交易源回退后把空响应伪造成完整覆盖")
        self.assertNotIn("pdd", TradeListPlatforms)
        source_text = inspect.getsource(sources_module)
        self.assertNotIn("erp.pdd", source_text,
                         "不得为拼多多猜一个接口方法名（设计 §3）")
        # 凭证侧也不得出现任何 pdd 专用配置入口。
        import bi_agent.config as config_module

        config_text = inspect.getsource(config_module)
        for token in ("PDD", "pdd"):
            self.assertNotIn(token, config_text,
                             "应用配置里没有拼多多凭证入口（不接入、不 onboarding）")


@unittest.skipUnless(os.getenv("BI_TEST_ADMIN_DSN"), "未配置独立测试数据库")
class QueryMemoryOperatorGateTests(OperatorFixture):
    """记忆门禁开着的一回合走真库（计划 approved-query-memory Task 5）。

    测试库的记忆投影视图是空的：这里要钉的不是“样例改变了什么”，而是三件
    不变式——(1) 开着时真的只对 reporting.v_approved_query_examples 发只读
    SELECT，每域一条，不碰底表也不写任何东西；(2) 同一工作流在门禁开/关下
    得到同型的结果（空记忆 = 无记忆基线）；(3) 会话历史里不留记忆段。
    """

    def _product_workflow_replies(self):
        return [_reply(calls=[_call("analyze_product_performance", {
            "product": {"text": "直钉枪"},
            "scope": {"mode": "all_authorized"},
            "start": START.isoformat(), "end": END.isoformat(),
            "metrics": ["sold_quantity", "sales_amount"],
            "sales_basis": "erp_effective_parent", "profit_basis": "none"})]),
            _reply(text="销量与销售额见下方表格")]

    def _seed_one_shop(self) -> None:
        self._archive(PRODUCT, "直钉枪")
        self._shop("1")
        self._cover("1")
        self._sale("1", "E1", quantity="1", amount="100", cost="40")

    def test_gate_on_turn_reads_only_the_projection_and_keeps_the_workflow(self):
        import re as re_module

        self._seed_one_shop()
        question = "直钉枪在获准店铺近七天卖得怎么样？"
        off = self._turn(question, self._product_workflow_replies())
        counted = _CountingConn(self.conn)
        on = self._turn(question, self._product_workflow_replies(), state=off.state,
                        conn=counted, approved_query_memory_enabled=True)
        # 工作流不因记忆改变：同型 Artifact、同一份数值（视图为空 = 无记忆基线）。
        self.assertEqual([item["artifact_type"] for item in on.artifacts],
                         [item["artifact_type"] for item in off.artifacts])
        self.assertEqual(on.results[-1].data, off.results[-1].data)
        # 只读投影：business_query / commerce / listing / inventory 各一条
        # SELECT（探索 Tool 没开放，探索域一条都不发）。
        view_reads = [text for text in counted.statements
                      if "v_approved_query_examples" in text]
        self.assertEqual(len(view_reads), 4)
        for statement in counted.statements:
            if "approved_query" in statement:
                self.assertIn("FROM reporting.v_approved_query_examples", statement)
                self.assertTrue(statement.upper().startswith("SELECT"), statement)
                self.assertNotIn("BI.APPROVED_QUERY_EXAMPLES", statement.upper())
            self.assertNotIn("APPROVED_QUERY_EVENTS", statement.upper())
            self.assertIsNone(re_module.search(
                r"\b(INSERT|UPDATE|DELETE)\b", statement.upper()),
                f"聊天路径不得发出写入：{statement[:80]}")
        # 记忆段不进会话历史：第二回合的历史里没有 system 消息。
        for message in on.state.turns:
            self.assertNotEqual(message.role, "system")

    def test_gate_off_and_absent_flag_run_identical_workflow_turns(self):
        self._seed_one_shop()
        question = "直钉枪在获准店铺近七天卖得怎么样？"
        baseline = self._turn(question, self._product_workflow_replies())
        off = self._turn(question, self._product_workflow_replies(),
                         approved_query_memory_enabled=False)
        self.assertEqual([item["artifact_type"] for item in off.artifacts],
                         [item["artifact_type"] for item in baseline.artifacts])
        self.assertEqual(off.text, baseline.text)


class OperatorAcceptanceSetTests(unittest.TestCase):
    """修订后的 26 题合同：题库形状与逐题守护字段。

    这里不跑模型也不跑库（那些在 `python -m tests.acceptance --offline` 里），
    只钉住合同本身：题数、逐题的原因码 / 口径凭证 / 诊断 / 覆盖形状不得默默退回去。
    尤其钉住一件事：**没有任一道题能拿合成通过当真实就绪的证据**。
    """

    @classmethod
    def setUpClass(cls) -> None:
        from tests import acceptance

        cls.module = acceptance
        cls.questions = {item["id"]: item for item in acceptance._load_questions()}

    def _expected(self, qid: str, index: int = 0) -> dict:
        expected = self.questions[qid]["expected"]
        return expected[index] if isinstance(expected, list) else expected

    def test_the_set_is_26_questions_and_ids_are_contiguous(self):
        self.assertEqual(len(self.questions), 26)
        self.assertEqual(sorted(self.questions),
                         [f"{index:02d}" for index in range(1, 27)])
        self.assertEqual(self.module.EXPECTED_QUESTION_COUNT, 26)

    def test_every_answer_question_compares_structured_facts_not_only_words(self):
        """设计 §6 / 计划 Task 11：不能只用“最终正文含某个词”判通过。

        三条可执行的规则：报成功的题必须比数值（或比“确实零行”）；报缺数据的题
        必须比原因码或覆盖形状；只有因果纠错题可以用正文包含词，但它也得同时比数值。
        """
        guarded = 0
        exempt: list[str] = []
        for qid, question in self.questions.items():
            expectations = (question["expected"]
                            if isinstance(question["expected"], list)
                            else [question["expected"]])
            for expected in expectations:
                if expected.get("clarify") or expected.get("status") == "forbidden":
                    exempt.append(qid)
                    continue
                status = expected.get("status")
                if status == "ok":
                    self.assertTrue(expected.get("values") or expected.get("no_rows"),
                                    f"{qid} 报成功却没比数值：{sorted(expected)}")
                if status in ("missing_data", "invalid_parameters"):
                    self.assertTrue(expected.get("codes")
                                    or expected.get("coverage")
                                    or expected.get("no_values"),
                                    f"{qid} 报缺数据却没比原因码/覆盖：{sorted(expected)}")
                if expected.get("text_contains"):
                    self.assertTrue(expected.get("values"),
                                    f"{qid} 只能同比正文与数值，不能只看措辞")
                guarded += 1
        self.assertGreaterEqual(guarded, 28, "逐题断言不能只靠几道新题撑场面")
        self.assertEqual(sorted(set(exempt)), ["08", "09", "19"],
                         "只有“先澄清”与“越权”两类可以不比数值")
        # 多来源守护字段真的在题集里：口径 / 诊断 / 覆盖 / 原因码。[26:51]
        dumped = json.dumps([self.questions[key] for key in sorted(self.questions)],
                            ensure_ascii=False)
        for field in ("basis", "diagnostics", "coverage", "codes", "no_total"):
            self.assertIn(field, dumped)

    def test_q08_asks_period_and_names_registered_bases(self):
        expected = self._expected("08")
        self.assertTrue(expected["clarify"])
        self.assertIn("统计期间", expected["clarify_contains"])
        self.assertIn("erp_outstock_payment/v1", expected["clarify_contains"])
        self.assertIn("非平台账单", expected["clarify_contains"])
        self.assertIn("TB1", self.questions["08"]["allowed"],
                      "不拿淘系店问就试不出“必须说出出库来源”这一条")

    def test_q15_is_dynamic_capability_not_a_fixed_platform_answer(self):
        expected = self._expected("15")
        self.assertEqual(sorted(self.questions["15"]["allowed"]),
                         ["PDD1", "S1", "TB1"])
        self.assertEqual(sorted(expected["parameters"]["shop_ids"]),
                         ["PDD1", "S1", "TB1"], "原授权范围不得被悄悄删小")
        self.assertIn("capability_unavailable", expected["codes"])
        self.assertTrue(expected["no_values"], "不给全公司总额")
        self.assertTrue(expected["scope_only_authorized"])

    def test_q21_keeps_mixed_bases_apart_and_proves_the_separate_path(self):
        refused, separate = (self._expected("21", 0), self._expected("21", 1))
        self.assertEqual(len(refused["calls"]), 2,
                         "一次提问里要先看到混口径被拒，再看到分列那一次")
        self.assertEqual(refused["calls"][0]["parameters"]["basis_policy"], "strict")
        self.assertEqual(refused["calls"][1]["parameters"]["basis_policy"], "separate")
        self.assertIn("coverage_time_basis_unverified", refused["codes"])
        self.assertTrue(refused["no_values"])
        self.assertTrue(separate["no_total"], "分列结果里不得藏一个跳店合计")
        self.assertEqual({entry[3] for entry in separate["basis"]},
                         {"pay_time", "outstock_time"},
                         "两个时间归属都要逐项看见")

    def test_q22_pdd_payment_stays_unavailable_documents_are_not_paid_orders(self):
        payment, documents = self._expected("22", 0), self._expected("22", 1)
        self.assertIn("capability_unavailable", payment["codes"])
        self.assertTrue(payment["no_values"])
        self.assertEqual(documents["values"], {"erp_documents": "3"})
        self.assertEqual(documents["basis"][0][2:],
                         ["erp_document/v1", "outstock_time"],
                         "单据口径不能伪装成支付订单数")

    def test_q23_discloses_unmatched_refunds_and_q26_refuses_the_window(self):
        refund = self._expected("23", 0)
        self.assertEqual(refund["values"], {"refund_amount": "50"})
        self.assertEqual(refund["diagnostics"]["unmatched_refunds"]["ratio"], "50%")
        self.assertIn("unmatched_refunds", refund["codes"])
        refused = self._expected("23", 1)
        self.assertIn("coverage_time_basis_unverified", refused["codes"])
        self.assertTrue(refused["no_values"])
        q26 = self._expected("26")
        self.assertIn("coverage_time_basis_unverified", q26["codes"])
        self.assertTrue(q26["no_values"])

    def test_q24_intersection_keeps_gaps_and_refuses_a_union(self):
        expected = self._expected("24")
        self.assertEqual(self.questions["24"]["seeds"], ["coverage_holes"])
        self.assertEqual(expected["coverage"]["gaps"],
                         ["2026-09-01~2026-09-03", "2026-09-05~2026-09-06"])
        self.assertEqual(expected["coverage"]["start"], "2026-09-01")
        self.assertEqual(expected["coverage"]["end"], "2026-09-08",
                         "原查询窗口不得被自动缩短")
        self.assertTrue(expected["no_values"], "有洞就不给部分汇总")

    def test_q25_closed_order_math_lives_on_the_certified_shop(self):
        """关闭单 100/30/70 与 matched_cohort_only 放在 fxg；tb 拿不到这些数。"""
        self.assertEqual(self.questions["25"]["allowed"], ["FX1"])
        first, cohort, quantity = (self._expected("25", 0), self._expected("25", 1),
                                  self._expected("25", 2))
        self.assertEqual(first["values"], {"paid_amount": "100",
                                           "refund_amount": "30",
                                           "cash_difference": "70"})
        self.assertIn("revenue_not_attributed", first["codes"],
                      "关闭行的支付没进商品维度：差额必须被说出来")
        self.assertIn("matched_cohort_only", cohort["codes"],
                      "有未匹配退款时同批率只能标已匹配口径")
        self.assertTrue(quantity["no_rows"], "商品有效销量不回活")

    def test_no_question_claims_live_or_provider_readiness(self):
        """合成通过不是 live 通过：runner 的总结本身要把这句话带在机器可读字段里。"""
        import inspect

        text = inspect.getsource(self.module)
        self.assertIn("不能证明模型理解准确率", text)
        self.assertIn("不构成任何平台真实来源就绪的证据", text)
        self.assertIn("未实测", text)
        for question in self.questions.values():
            self.assertNotIn("live", question)
            self.assertNotIn("provider", question)

    def test_the_dated_acceptance_report_exists_with_its_disclaimers(self):
        """验收报告是本轮交付物之一：它必须存并明写"哪些没跑"。"""
        from pathlib import Path

        report = (Path(__file__).resolve().parents[2]
                  / "docs" / "superpowers" / "research"
                  / "2026-09-14-task-11-release-acceptance.md")
        self.assertTrue(report.exists(), report)
        text = report.read_text(encoding="utf-8")
        self.assertIn("26", text)
        self.assertIn("未执行", text)
        self.assertIn("不接入", text)
        self.assertIn("合成", text)


__all__ = ["OperatorFixture", "OperatorTestDatabaseGatingTests",
           "OperatorProductWorkflowTests",
           "OperatorComparisonWorkflowTests", "OperatorListingWorkflowTests",
           "OperatorInventoryWorkflowTests", "OperatorAgentRoutingTests",
           "OperatorMultiSourceGuardrailTests", "OperatorAcceptanceSetTests",
           "QueryMemoryOperatorGateTests"]
