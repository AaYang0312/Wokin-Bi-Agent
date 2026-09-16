"""Task 10：实物库存与店铺可售库存的两级预警（inventory_watch）。

计划 Task 10 点名的失败用例一次到位：三店共用一库存池、渠道 0 / 实物充足、总量低、
负库存、缺阈值、多单位套件、过期、无成交商品、扫描分页漏项，另加本任务的红线：

1. **实物总量按 (库存池, 仓库, SKU, 批次, 单位) 去重，只算一次**。同一仓库可用 100
   被三家店各展示 100 时，实物总量仍是 100，不是 300（spec §5.5）。同键不同数量不
   是「取一个」，是冲突。
2. **渠道可售与实物是两个口径**：各取各的源、各判各的新鲜度，永不把渠道显示数加成
   实物总量，也永不因为渠道缺货就说实物低。
3. **库存池授权独立于店铺授权**：拿到三家店不等于能看全公司仓库（spec §5.5）。未获准
   的池既不进总量也不进明细。
4. **来源门禁默认关**。历史核查只验证过部分 ERP SKU / 仓库样本，渠道可售量需独立取证
   （spec §9），所以交付代码里没有任何已核验来源，真实部署只能报 unsupported。
5. **全商品盘点先扫完再截展示**：scanned/expected/truncated 必须能机器核对，高风险
   不能因为先取 Top N 而漏检。

真实测试库跑法与既有约定一致：管理员连接 + 外层事务回滚，只写合成店铺 / 池 / SKU。
无 DSN 时显式 skip——skip 不是通过证明；但来源门禁、去重、阈值边界、单位冲突、
状态优先级这些结论都在无库的节点级用例里也必须被执行。
"""

from __future__ import annotations

import os
import time
import unittest
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Sequence
from unittest import mock
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

import psycopg
from pydantic import ValidationError

from bi_agent.catalog import ref_for_key
from bi_agent.catalog.channel_mapping import DEFAULT_NAMESPACE
from bi_agent.catalog.models import EntityKind
from bi_agent.commerce.models import DomainContext
from bi_agent.inventory import repository
from bi_agent.inventory.graph import (
    INVENTORY_GRAPH_VERSION, InventoryExecution, InventoryNode, InventoryRuntime,
    InventoryState, TEXT_POOL_UNAUTHORIZED, TEXT_SOURCE_UNVERIFIED,
    TEXT_SKU_UNRESOLVED,
    TEXT_UNIVERSE_FROM_SNAPSHOT,
    _connection_kind, authorize_inventory_pools,
    check_inventory_source, check_source_completeness_and_freshness,
    classify_inventory_actions, compute_total_and_shop_levels,
    deduplicate_physical_rows, evaluate_inventory_thresholds,
    load_inventory_policy, TEXT_SCAN_INCOMPLETE,
    run_inventory_graph, summarize_inventory_levels,
    transition_state)
from bi_agent.inventory.graph import InvalidInventoryTransition, beijing_iso
from bi_agent.inventory.rules import inventory_limitation_codes
from bi_agent.inventory.models import InventoryInspectionRequest
from bi_agent.inventory.rules import (
    AUDIT_LEVELS, INVENTORY_LIMITATION_CODES, INVENTORY_PUBLIC_LIMITATIONS,
    MAX_EXPECTED_ITEMS, MAX_THRESHOLD_QUANTITY, PRICELESS_UNITS, STATUS_UNCONFIGURED,
    STATUS_UNKNOWN,
    ROW_STATUSES, STOCK_STATUSES, STATUS_RISK_ORDER, STORAGE_UNITS,
    THRESHOLD_LEVELS,
    UNIT_CONVERSION_REGISTRY_VERSION,
    InventoryConflict, InventorySourceRegistration, amount_sum, classify_inventory,
    freshness_ok, normalize_quantity, physical_identity, pool_handle,
    quantity_precision, register_inventory_source, reset_inventory_sources,
    shop_connection_kind_of, sum_unique_physical_stock, unit_comparable,
    verified_inventory_source, verified_inventory_sources, warehouse_handle)
from bi_agent.inventory.tool import inspect_inventory, inventory_request_schema
from bi_agent.runtime.domain_registry import INVENTORY_NODES, spec_for
from bi_agent.runtime.memory import MemoryQueryRunStore
from bi_agent.runtime.models import (
    validate_artifact_payload, validate_model_payload, validate_persisted_state)

from .dbfixtures import connect_test_db

BEIJING = ZoneInfo("Asia/Shanghai")
NOW = datetime(2026, 9, 14, 12, tzinfo=BEIJING)
FRESH = NOW - timedelta(minutes=20)
STALE = NOW - timedelta(days=30)
OLDER = NOW - timedelta(hours=6)
FRESH_ISO = FRESH.astimezone(BEIJING).isoformat()
ALERT_TYPE = "inventory_alerts"


def _shop_ref(tag: str) -> str:
    return ref_for_key("shop", f"S{tag}")


def _sku_ref(sku: str) -> str:
    return ref_for_key(EntityKind.SKU.value, sku)


def _threshold(level: str, sku: str, quantity: str, **overrides: object) -> dict:
    """一条本轮阈值：`sku` 收的是 ERP 主键（引用在内部派生，避免每处都拼一遍）。"""
    ref = sku if str(sku).startswith("ent-") else _sku_ref(sku)
    entry: dict[str, object] = {"level": level, "sku_ref": ref,
                                "quantity": quantity, "unit": "piece"}
    entry.update(overrides)
    return entry


_MISSING = object()


def _request(**overrides: object) -> InventoryInspectionRequest:
    base: dict[str, object] = {
        "products": "selected",
        "sku_refs": [_sku_ref("SKU1")],
        "scope": {"mode": "all_authorized"},
        "levels": ["physical_total", "shop_sellable"],
        "as_of": "latest",
        "thresholds": [_threshold("low_replenish", "SKU1", "20")],
    }
    base.update(overrides)
    return InventoryInspectionRequest.model_validate(base)


def _payload_for(result, artifact_type: str = ALERT_TYPE) -> dict:
    matches = [artifact.public_payload for artifact in result.artifacts
               if artifact.ref.type == artifact_type]
    assert len(matches) == 1, ([artifact.ref.type for artifact in result.artifacts],
                               artifact_type)
    return matches[0]


def _rows(payload: dict) -> list[dict]:
    return [row for row in payload["data"] if "inventory_status" in row]


def _node_body(runtime) -> dict:
    """节点级运行产物：与 tool 的公开投影同一形状，但不建目录。"""
    from bi_agent.inventory.graph import alert_rows

    return {
        "status": runtime.report.status if runtime.report else "partial",
        "inventory": dict(runtime.summary),
        "data": alert_rows(runtime),
        "filters": {"products": "selected",
                    "levels": list(runtime.request.levels), "as_of": "latest",
                    "thresholds": list(runtime.request.normalized()["thresholds"])},
        "limitations": list(runtime.limitations),
    }


def _row_for(payload: dict, *, level: str, shop: str | None = None,
             sku: str | None = None) -> dict:
    """按 (口径, 店铺, SKU) 取那一行：命中多行就是没钉住粒度。"""
    hits = [row for row in _rows(payload)
            if row["level"] == level
            and (shop is None or row.get("shop_ref") == shop)
            and (sku is None or row.get("sku_ref") == sku)]
    assert len(hits) == 1, (level, shop, sku, _rows(payload))
    return hits[0]


def _status_for(payload: dict, **keys: object) -> str:
    return str(_row_for(payload, **keys)["inventory_status"])  # type: ignore[arg-type]


def _limitation_text(payload: object) -> str:
    if isinstance(payload, dict):
        return "；".join(str(item) for item in payload.get("limitations") or [])
    return "；".join(str(item) for item in payload or [])


def _reply(text: str | None = None, calls: list | None = None):  # noqa: ANN001
    """构造带 `_message` 的模型回合：主层每次回合后都会 as_message()。"""
    from bi_agent.llm import Message, ModelReply

    calls = calls or []
    reply = ModelReply(text=text, tool_calls=calls)
    reply._message = Message(role="assistant", content=text, tool_calls=calls)
    return reply


class _RecordingConn:
    """记录每条 SQL：用来证明「未获准的池没有被偷偷查过」。"""

    def __init__(self, conn) -> None:  # noqa: ANN001
        self.conn = conn
        self.sql: list[str] = []

    def execute(self, sql, params=None):  # noqa: ANN001
        self.sql.append(" ".join(str(sql).split()))
        return self.conn.execute(sql, params) if params is not None \
            else self.conn.execute(sql)

    def __getattr__(self, name):  # noqa: ANN001
        return getattr(self.conn, name)

    def matching(self, *fragments: str) -> list[str]:
        return [entry for entry in self.sql
                if all(fragment in entry for fragment in entries_ok(fragments))]


def entries_ok(fragments: Sequence[str]) -> list[str]:
    return list(fragments)


class _FailingStore(MemoryQueryRunStore):
    def save_artifact(self, run_id, artifact):  # noqa: ANN001
        if artifact.artifact_type == ALERT_TYPE:
            raise RuntimeError("simulated artifact failure")
        return super().save_artifact(run_id, artifact)


# ---------------------------------------------------------------------------
# 1. 纯规则：去重、阈值边界、单位与时效
# ---------------------------------------------------------------------------


class InventoryRuleTests(unittest.TestCase):
    """计划 Task 10 逐字给出的两组断言，加上本任务自己需要的边界。"""

    def test_shared_stock_is_not_counted_once_per_shop(self):
        row = {"pool_ref": "pool-a", "warehouse_ref": "wh-a", "sku_ref": "sku-a",
               "batch_id": "batch-1", "available_quantity": "100", "unit": "piece"}
        self.assertEqual(sum_unique_physical_stock([dict(row), dict(row), dict(row)]),
                         "100")

    def test_threshold_boundary_and_missing_evidence(self):
        self.assertEqual(classify_inventory("10", "10", fresh=True), "low")
        self.assertEqual(classify_inventory("100", None, fresh=True), "unconfigured")
        self.assertEqual(classify_inventory(None, "10", fresh=True), "unknown")
        self.assertEqual(classify_inventory("100", "10", fresh=False), "stale")

    def test_equal_to_threshold_is_low_not_normal(self):
        """spec §5.5 判定 quantity <= threshold：等于阈值也是预警，不是刚好安全。"""
        self.assertEqual(classify_inventory("20", "20", fresh=True), "low")
        self.assertEqual(classify_inventory("20.00", "20", fresh=True), "low")
        self.assertEqual(classify_inventory("21", "20", fresh=True), "normal")
        self.assertEqual(classify_inventory("0", "20", fresh=True), "low",
                         "真实零库存是 low，不是缺证据")

    def test_negative_stock_is_a_data_anomaly_on_both_sides_of_the_threshold(self):
        self.assertEqual(classify_inventory("-1", "20", fresh=True), "data_anomaly")
        self.assertEqual(classify_inventory("-1", None, fresh=True), "data_anomaly",
                         "负数不是「没配阈值」：它先说明源数据不成立")
        self.assertEqual(classify_inventory("-5", "20", fresh=False), "stale",
                         "过期时先说过期：一个不该被读的数字不该抢走真正的下一步")

    def test_zero_is_not_the_same_as_missing(self):
        self.assertEqual(classify_inventory("0", "20", fresh=True), "low")
        self.assertEqual(classify_inventory("", "20", fresh=True), "unknown")
        self.assertEqual(classify_inventory("abc", "20", fresh=True), "unknown")
        self.assertEqual(classify_inventory("1e3", "20", fresh=True), "unknown")
        self.assertEqual(classify_inventory(None, None, fresh=True), STATUS_UNCONFIGURED,
                         "两端都没有时缺的是阈值：那才是经营者可以立刻补上的那一侧")

    def test_quantity_precision_and_normalization(self):
        self.assertEqual(quantity_precision("piece"), 0)
        self.assertEqual(quantity_precision("box"), 0)
        self.assertIsNone(quantity_precision("kit"))          # 未登记单位
        self.assertEqual(normalize_quantity("100.000", "piece"), Decimal(100))
        self.assertIsNone(normalize_quantity("0.5", "piece"),
                          "整数单位上的小数就是来源没按口径给数：不四舍五入，也不参与求和")
        self.assertIsNone(normalize_quantity("100", "kit"))
        # 同类且已登记才可相加；不同单位、未登记单位、空单位都不行。
        self.assertFalse(unit_comparable("piece", "box"))
        self.assertFalse(unit_comparable("piece", "kit"))
        self.assertFalse(unit_comparable("", ""))
        self.assertTrue(unit_comparable("piece", "piece"))
        self.assertIn("kit", PRICELESS_UNITS, "未登记单位必须能被列出来，而不是被当成 0")

    def test_same_identity_different_quantity_is_a_conflict_not_a_choice(self):
        row = _physical("pool-a", "wh-a", "sku-a", "100")
        other = _physical("pool-a", "wh-a", "sku-a", "120")
        with self.assertRaises(InventoryConflict) as error:
            sum_unique_physical_stock([row, other])
        self.assertEqual(len(error.exception.keys), 1)
        # 冲突只针对同一身份：不同池 / 不同仓库 / 不同批次都不是冲突，也不能互相顶掉
        self.assertEqual(sum_unique_physical_stock([row, _physical("pool-b", "wh-a",
                                                                  "sku-a", "100")]),
                         "200")
        self.assertEqual(sum_unique_physical_stock([row, _physical("pool-a", "wh-b",
                                                                  "sku-a", "100")]),
                         "200")
        self.assertEqual(sum_unique_physical_stock([row, _physical("pool-a", "wh-a",
                                                                  "sku-a", "100",
                                                                  batch="batch-2")]),
                         "200", "不同批次是两笔事实：合计量把它们都算进去")

    def test_units_are_never_merged_into_one_quantity(self):
        """同一 (池, 仓库, SKU) 挂着 piece 与 box 时，不给一个混合总数。

        单位换算需要已登记的换算表；没有它，5 piece + 2 box 说成 7 是编一个数。
        """
        rows = [_physical("pool-a", "wh-a", "sku-a", "5", unit="piece"),
                _physical("pool-a", "wh-a", "sku-a", "2", unit="box")]
        with self.assertRaises(InventoryConflict) as error:
            sum_unique_physical_stock(rows)
        self.assertEqual(error.exception.reason, "unit_conflict")

    def test_kit_component_and_parent_are_not_double_counted(self):
        """套件父项与组件行同池同仓时不重复计入实物总量（identity 含批次与单位）。

        这里钉的是「同一身份的同一事实只算一次」：三行完全相同 = 100，与计划给出的
        三店共用一池用例同一形状；父项与组件是不同 SKU，各自成行，不相加为同一数。
        """
        same = _physical("pool-a", "wh-a", "sku-kit", "100")
        self.assertEqual(sum_unique_physical_stock([same, dict(same), dict(same)]), "100")
        parts = _physical("pool-a", "wh-a", "sku-part", "100")
        self.assertEqual(sum_unique_physical_stock([same, parts]), "200",
                         "父项与组件是两个 SKU 的事实：加总是 200，但预警按各自口径出")

    def test_physical_identity_is_the_dedup_key_and_nothing_else(self):
        keys = {physical_identity(_physical("p", "w", "s", "1")): 0,
                physical_identity(_physical("p", "w", "s", "9")): 0}
        self.assertEqual(len(keys), 1, "同身份不同数量必须撞同一个键，否则冲突无从发现")
        self.assertNotEqual(physical_identity(_physical("p", "w", "s1", "1")),
                            physical_identity(_physical("p", "w", "s2", "1")))
        identity = physical_identity(_physical("p", "w", "s", "1", batch="b1",
                                               unit="box"))
        self.assertIn("box", identity, "单位必须进身份：不同单位不得互相顶掉")
        self.assertIn("b1", identity)

    def test_amount_sum_rejects_unparseable_rather_than_treating_it_as_zero(self):
        self.assertEqual(amount_sum(["100", "20"], "piece"), "120")
        with self.assertRaises(ValueError):
            amount_sum(["100", "lots"], "piece")

    def test_freshness_policy_is_a_boundary_not_a_vibe(self):
        self.assertTrue(freshness_ok(captured_at=NOW - timedelta(hours=23), now=NOW,
                                     max_age_seconds=86400))
        self.assertFalse(freshness_ok(captured_at=NOW - timedelta(hours=25), now=NOW,
                                      max_age_seconds=86400))
        self.assertFalse(freshness_ok(captured_at=None, now=NOW, max_age_seconds=86400),
                         "没有抓取时间就没有时效可言：判 stale，不判新鲜")
        self.assertFalse(freshness_ok(captured_at=NOW.replace(tzinfo=None), now=NOW,
                                      max_age_seconds=86400),
                         "无时区的时间会被一个读者当 UTC、另一个当本地时间")

    def test_shop_connection_kinds_are_the_three_shapes_and_nothing_invented(self):
        self.assertEqual(shop_connection_kind_of("shared"), "shared")
        self.assertEqual(shop_connection_kind_of("allocated"), "allocated")
        self.assertEqual(shop_connection_kind_of("independent"), "independent")
        with self.assertRaises(ValueError):
            shop_connection_kind_of("assume_shared")

    def test_handles_are_one_way_and_namespace_scoped(self):
        first = pool_handle(DEFAULT_NAMESPACE, "pool-a")
        self.assertEqual(first, pool_handle(DEFAULT_NAMESPACE, "pool-a"))
        self.assertRegex(first, r"^pl-[0-9a-f]{12}$")
        self.assertNotEqual(first, pool_handle("acct-other", "pool-a"),
                            "同一池号在另一账号范围里不是同一个句柄")
        self.assertRegex(warehouse_handle(DEFAULT_NAMESPACE, "wh-a"), r"^wh-[0-9a-f]{12}$")
        self.assertNotIn("pool-a", first)

    def test_risk_order_puts_the_bad_news_first(self):
        """截断只会截掉排在后面的：顺序本身就是这条用例要钉的东西。

        拿 `STATUS_RISK_ORDER` 跟它自己比是恒等式；这里比的是图真正用来排序的那份表。
        """
        from bi_agent.inventory.graph import _STATUS_RISK

        self.assertEqual(list(_STATUS_RISK), list(STATUS_RISK_ORDER))
        self.assertLess(_STATUS_RISK["data_anomaly"], _STATUS_RISK["low"])
        self.assertLess(_STATUS_RISK["low"], _STATUS_RISK["normal"],
                        "normal 排在最后：把它挤进 Top N 就是把好消息当预警")
        self.assertEqual(set(_STATUS_RISK), set(ROW_STATUSES),
                         "每个状态都得有风险位次，漏一个就会用兜底值 9 排序")

    def test_vocabulary_matches_the_design(self):
        self.assertEqual(set(STOCK_STATUSES), {"low", "normal", "unconfigured", "unknown",
                                               "stale", "data_anomaly"})
        self.assertEqual(set(AUDIT_LEVELS), {"physical_total", "shop_sellable"})
        self.assertEqual(set(THRESHOLD_LEVELS), {"low_replenish", "low_quota"})
        self.assertLess(MAX_THRESHOLD_QUANTITY, 10 ** 9)
        # 超过可信上限的数量不参与求和：它先说明单位或录入错了。
        self.assertIsNone(normalize_quantity(str(10 ** 9), "piece"))


def _termination_codes_of(runtime) -> list[str]:
    """从状态里取归因码：终止原因由这些码派生，断言必须打在码上而不是打在文案上。"""
    return list(runtime.state.limitations)


def _termination_reason_of(runtime) -> str:
    from bi_agent.inventory.graph import _termination_reason

    return _termination_reason(runtime, runtime.state.status)


def _physical(pool: str, warehouse: str, sku: str, quantity: str, *,
              batch: str = "batch-1", unit: str = "piece") -> dict:
    return {"pool_ref": pool, "warehouse_ref": warehouse, "sku_ref": sku,
            "batch_id": batch, "available_quantity": quantity, "unit": unit}


class InventorySourceRegistryTests(unittest.TestCase):
    """库存来源注册表：登记即开通能力，所以默认必须是空。"""

    def tearDown(self) -> None:
        reset_inventory_sources()

    def test_no_level_ships_with_a_verified_inventory_source(self):
        """真实部署里 Tool 只能报 unsupported：这条断言就是「不冒充线上可用」的凭据。"""
        self.assertEqual(verified_inventory_sources(), {})
        for level in AUDIT_LEVELS:
            self.assertIsNone(verified_inventory_source(level))

    def test_no_module_outside_the_registry_can_turn_the_gate_on(self):
        """仓库级门禁：只有注册表自己定义登记函数，产品代码里一处调用都没有。

        只断言"此刻为空"不够：夹具的 cleanup 会把一个 import 时就开了门的痕迹抹平。
        """
        import pathlib

        root = pathlib.Path(__file__).resolve().parents[1] / "bi_agent"
        allowed = root / "inventory" / "rules.py"
        offenders = []
        for path in sorted(root.rglob("*.py")):
            if path == allowed:
                continue
            if "register_inventory_source(" in path.read_text(encoding="utf-8"):
                offenders.append(str(path.relative_to(root.parent)))
        self.assertEqual(offenders, [], "开通一个库存来源必须带证据改注册表，不能绕")
        self.assertEqual(verified_inventory_sources(), {})

    def test_registration_requires_evidence_level_and_freshness_policy(self):
        # 监控门禁（Task 1）新增的两个必填字段在负例里按合成事实填：没有任何
        # 夹具声称存在完整扫描凭据或生产对账（scan_complete_supported=False、
        # production_reconciled_at=None），报错仍只来自被测的那四个字段。
        monitor_absent = {"scan_complete_supported": False,
                          "production_reconciled_at": None}
        cases = (
            {"level": "physical_total", "channel": "erp", "evidence": "",
             "max_age_seconds": 86400},
            {"level": "physical_total", "channel": "erp", "evidence": "  ",
             "max_age_seconds": 86400},
            {"level": "physical_total", "channel": "erp", "evidence": "probe-1",
             "max_age_seconds": 0},
            {"level": "physical_total", "channel": "erp", "evidence": "probe-1",
             "max_age_seconds": -5},
            {"level": "both_levels", "channel": "erp", "evidence": "probe-1",
             "max_age_seconds": 86400},
            {"level": "", "channel": "erp", "evidence": "probe-1",
             "max_age_seconds": 86400},
            # 拼多多按 2026-09-12 决定不接入：新通道也不能顺手写进注册表
            {"level": "shop_sellable", "channel": "pdd_api", "evidence": "probe-1",
             "max_age_seconds": 86400})
        for kwargs in cases:
            merged = {**monitor_absent, **kwargs}
            with self.subTest(**merged):
                with self.assertRaises(ValueError):
                    register_inventory_source(InventorySourceRegistration(**merged))

    def test_registration_requires_monitor_evidence_fields(self):
        """监控门禁的两个新字段必填：缺省构造当场 TypeError，注册表仍为空。"""
        with self.assertRaises(TypeError):
            InventorySourceRegistration(
                level="physical_total", channel="erp", evidence="probe-1",
                max_age_seconds=86400)
        self.assertEqual(verified_inventory_sources(), {})

    def test_registration_is_readable_per_level_after_it_is_made(self):
        # 合成夹具不冒充监控级证据：聊天路径门禁不消费那两个字段，所以填合成
        # 事实（False/None）；监控门禁要它们时由 tests.test_monitoring 显式给值。
        register_inventory_source(InventorySourceRegistration(
            level="physical_total", channel="erp", evidence="probe-1",
            max_age_seconds=86400, scan_complete_supported=False,
            production_reconciled_at=None))
        self.assertIsNotNone(verified_inventory_source("physical_total"))
        # 一个口径取证不等于另一个口径也成立：渠道可售要各自取证（spec §9）
        self.assertIsNone(verified_inventory_source("shop_sellable"))
        self.assertEqual(set(verified_inventory_sources()), {"physical_total"})
        reset_inventory_sources()
        self.assertIsNone(verified_inventory_source("physical_total"))

    def test_vocabulary_does_not_drift_from_the_runtime_contract(self):
        from bi_agent.runtime import models as runtime_models

        self.assertTrue(INVENTORY_LIMITATION_CODES <= runtime_models._LIMITATION_CODES,
                        sorted(INVENTORY_LIMITATION_CODES - runtime_models._LIMITATION_CODES))
        self.assertTrue(INVENTORY_PUBLIC_LIMITATIONS
                        <= runtime_models._PUBLIC_LIMITATIONS,
                        sorted(INVENTORY_PUBLIC_LIMITATIONS
                               - runtime_models._PUBLIC_LIMITATIONS))


# ---------------------------------------------------------------------------
# 2. 入参契约
# ---------------------------------------------------------------------------


class InventoryRequestContractTests(unittest.TestCase):

    def test_levels_are_the_two_designed_ones_only(self):
        for bad in ([], ["warehouse_total"], ["physical_total", "physical_total"],
                    ["channel_sellable"]):
            with self.subTest(bad=bad):
                with self.assertRaises(ValidationError):
                    _request(levels=bad)

    def test_products_selected_needs_a_selector(self):
        with self.assertRaises(ValidationError):
            _request(sku_refs=[])
        with self.assertRaises(ValidationError):
            _request(products="all", sku_refs=[_sku_ref("SKU1")])
        _request(products="all", sku_refs=[])

    def test_refs_must_be_opaque_refs(self):
        for bad in (["SKU1"], ["ent-XYZ"], [""]):
            with self.subTest(bad=bad):
                with self.assertRaises(ValidationError):
                    _request(sku_refs=bad)
        with self.assertRaises(ValidationError):
            _request(product_refs=["P1"])

    def test_thresholds_and_policy_ref_are_mutually_exclusive(self):
        """spec §4：两者都给就是两个分母，取哪一个都没依据。"""
        with self.assertRaises(ValidationError):
            _request(threshold_policy_ref="quota/1",
                     thresholds=[_threshold("low_quota", "SKU1", "5")])
        with self.assertRaises(ValidationError):
            _request(thresholds=[], threshold_policy_ref=None)
        _request(threshold_policy_ref="quota/1", thresholds=None)

    def test_policy_ref_shape(self):
        for bad in ("quota", "1/quota", "Quota/1", "quota/"):
            with self.subTest(ref=bad):
                with self.assertRaises(ValidationError):
                    _request(threshold_policy_ref=bad)

    def test_threshold_above_the_trust_ceiling_is_refused(self):
        """超过可信上限的阈值在入口就拒：那多半是单位错了或一位录入错误。

        这种数一旦进去，所有 SKU 都会被判 low，整份预警就变成"全部都要补货"——
        一个错数字比缺数字更难被发现，因为它看起来像个结论。
        """
        self.assertLess(MAX_THRESHOLD_QUANTITY, 10 ** 9)
        # 上限本身仍可配置，越过一位就拒：这条边界钉的是"上限有没有在被执行"。
        with self.assertRaises(ValidationError):
            _request(thresholds=[_threshold("low_replenish", "SKU1",
                                           str(MAX_THRESHOLD_QUANTITY + 1))])
        _request(thresholds=[_threshold("low_replenish", "SKU1",
                                       str(MAX_THRESHOLD_QUANTITY))])

    def test_inline_threshold_shape(self):
        _request(thresholds=[_threshold("low_quota", "SKU1", "5", shop_ref=_shop_ref("1"))])
        _request(thresholds=[_threshold("low_replenish", "SKU1", "5",
                                       pool_ref=pool_handle(DEFAULT_NAMESPACE, "p1"))])
        bad_cases = (
            {"level": "low_quota", "sku_ref": _sku_ref("SKU1"), "quantity": "5",
             "unit": "piece", "shop_ref": _shop_ref("1"), "pool_ref": "pl-000000000000"},
            {"level": "low_replenish", "sku_ref": _sku_ref("SKU1"), "quantity": "5",
             "unit": "piece", "pool_ref": "not-a-handle"},
            {"level": "critical", "sku_ref": _sku_ref("SKU1"), "quantity": "5",
             "unit": "piece"},
            {"level": "low_replenish", "quantity": "5", "unit": "piece"},
            {"level": "low_replenish", "sku_ref": _sku_ref("SKU1"), "quantity": "-1",
             "unit": "piece"},
            {"level": "low_replenish", "sku_ref": _sku_ref("SKU1"), "quantity": "5.5",
             "unit": "piece"},
            {"level": "low_replenish", "sku_ref": _sku_ref("SKU1"), "quantity": "五",
             "unit": "piece"},
            {"level": "low_replenish", "sku_ref": _sku_ref("SKU1"), "quantity": "5",
             "unit": "件"},
            {"level": "low_replenish", "sku_ref": "SKU1", "quantity": "5",
             "unit": "piece"})
        for bad in bad_cases:
            with self.subTest(bad=sorted(bad)):
                with self.assertRaises(ValidationError):
                    _request(thresholds=[bad])

    def test_duplicate_identical_thresholds_are_not_an_error_at_the_boundary(self):
        """同一句话说了两遍不是冲突：那要留给规则层按同一档位一致时合并。"""
        same = _threshold("low_replenish", "SKU1", "20")
        request = _request(thresholds=[same, dict(same)])
        self.assertEqual(len(request.normalized()["thresholds"]), 1)
        with self.assertRaises(ValidationError):
            _request(thresholds=[same, _threshold("low_replenish", "SKU1", "30")])

    def test_as_of_is_latest_only(self):
        with self.assertRaises(ValidationError):
            _request(as_of="2026-09-01")

    def test_extra_fields_are_refused(self):
        with self.assertRaises(ValidationError):
            _request(shop_ids=["S1"])
        with self.assertRaises(ValidationError):
            InventoryInspectionRequest.model_validate(
                {"products": "selected", "sku_refs": [_sku_ref("S")], "levels": ["low"],
                 "thresholds": [], "pool_ids": ["pool-a"]})

    def test_authorized_pools_are_never_a_model_input(self):
        """库存池授权只能由服务端给：能被模型传进来的授权不叫授权。"""
        schema = str(inventory_request_schema())
        for forbidden in ("allowed_pool", "pool_ids", "warehouse_id", "shop_id",
                          "evidence", "register", "max_age", "snapshot"):
            self.assertNotIn(forbidden, schema)

    def test_normalized_request_freezes_this_round_inputs(self):
        normalized = _request().normalized()
        self.assertEqual(normalized["levels"], ["physical_total", "shop_sellable"])
        self.assertEqual(normalized["as_of"], "latest")
        self.assertEqual(normalized["thresholds"], [{
            "level": "low_replenish", "sku_ref": _sku_ref("SKU1"),
            "quantity": "20", "unit": "piece"}])
        validate_persisted_state({"node": "load_inventory_policy", "status": "running",
                                  "revision": 0, "normalized_request": normalized})

    def test_threshold_level_must_match_the_requested_levels(self):
        """配额阈值只对店铺可售有意义，补货阈值只对实物有意义：跨着给就是问错问题。"""
        with self.assertRaises(ValidationError):
            _request(levels=["physical_total"],
                     thresholds=[_threshold("low_quota", "SKU1", "5",
                                           shop_ref=_shop_ref("1"))])
        with self.assertRaises(ValidationError):
            _request(levels=["shop_sellable"],
                     thresholds=[_threshold("low_replenish", "SKU1", "5")])

    def test_all_products_mode_rejects_a_named_selector(self):
        """all 与点名同时给就是两个全集定义，入口必须拒（不是"以其中一个为准"）。"""
        with self.assertRaises(ValidationError):
            _request(products="all", sku_refs=[_sku_ref("SKU1")])
        with self.assertRaises(ValidationError):
            _request(products="all", product_refs=[ref_for_key("product", "P1")])
        # all + 本轮阈值是允许的：阈值说的是判定标准，不是范围。
        _request(products="all", sku_refs=[],
                 thresholds=[_threshold("low_replenish", "SKU1", "5")])

    def test_request_fingerprint_changes_with_threshold_quantity(self):
        from bi_agent.runtime.artifacts import QueryProvenance, request_fingerprint

        base = _request().normalized()

        def fingerprint(request: dict) -> str:
            return request_fingerprint(
                subject_id="u1", allowed_shop_ids=frozenset({"S1"}),
                normalized_request=request,
                provenance=QueryProvenance(template_id="inventory_watch",
                                           template_version="1"))

        self.assertNotEqual(fingerprint(base),
                            fingerprint({**base, "thresholds": [
                                _threshold("low_replenish", "SKU1", "30")]}))
        self.assertNotEqual(fingerprint(base),
                            fingerprint({**base, "levels": ["physical_total"]}))
        self.assertEqual(fingerprint(base), fingerprint(dict(base)))


# ---------------------------------------------------------------------------
# 3. 领域注册表与 inventory_alerts 载荷契约
# ---------------------------------------------------------------------------


def _alerts_payload_skeleton() -> dict:
    """一份最小合法载荷（合成引用）：判别分支的正反两面都要有用例。"""
    row = {"level": "physical_total", "sku_ref": _sku_ref("SKU1"),
           "pool_ref": "pl-0123456789ab", "warehouse_ref": "wh-0123456789ab",
           "quantity": "10", "threshold": "10", "unit": "piece",
           "inventory_status": "low", "snapshot_at": FRESH_ISO, "batch_count": 1}
    return {
        "status": "partial",
        "inventory": {
            "expected_items": 1, "evaluated_items": 1, "scanned_items": 1,
            "truncated": False, "all_safe": False,
            "counts": {"low": 1},
            "levels": ["physical_total"],
            # 阈值的来源必须说清：本轮 inline、已配置策略、还是根本没有。
            "threshold_source": "this_turn",
            "pools": [{"pool_ref": "pl-0123456789ab", "connection_kind": "shared",
                       "fresh": True, "scan_complete": True, "snapshot_at": FRESH_ISO}],
            "freshness_policy_seconds": 86400,
            "rule_version": "inventory-rules/2026-09-14.1"},
        "data": [dict(row)],
        "filters": {"as_of": "latest", "levels": ["physical_total"],
                    "products": "selected",
                    "thresholds": [{"level": "low_replenish",
                                    "sku_ref": _sku_ref("SKU1"), "quantity": "10",
                                    "unit": "piece"}]},
        "limitations": [],
    }


class InventoryDomainRegistryTests(unittest.TestCase):

    def test_inventory_nodes_are_the_designed_chain_not_the_legacy_placeholder(self):
        spec = spec_for("inventory_watch")
        self.assertEqual(
            set(spec.nodes),
            {"resolve_full_catalog_and_scope", "authorize_inventory_pools",
             "load_inventory_policy", "check_source_capabilities", "load_snapshots",
             "check_completeness_and_freshness", "normalize_units_and_deduplicate_pools",
             "compute_total_and_shop_levels", "evaluate_thresholds",
             "classify_actions", "persist_alerts", "finalize"})
        self.assertEqual(frozenset(node.value for node in InventoryNode), INVENTORY_NODES)
        self.assertEqual(frozenset(node.value for node in InventoryNode), spec.nodes)
        self.assertNotIn("assess_readiness", spec.nodes)
        self.assertNotIn("scan_inventory", spec.nodes)

    def test_inventory_alerts_uses_its_own_discriminated_schema(self):
        with self.assertRaises(ValueError):
            validate_artifact_payload({"status": "ok", "data": []}, ALERT_TYPE)
        import copy

        payload = _alerts_payload_skeleton()
        before = copy.deepcopy(payload)
        self.assertEqual(validate_artifact_payload(payload, ALERT_TYPE), before)

    def test_alert_rows_refuse_real_identifiers_and_untyped_values(self):
        payload = _alerts_payload_skeleton()
        bad_rows = (
            {**payload["data"][0], "sku_id": "SKU1"},
            {**payload["data"][0], "pool_id": "pool-a"},
            {**payload["data"][0], "warehouse_id": "wh-a"},
            {**payload["data"][0], "inventory_status": "fine"},
            {**payload["data"][0], "quantity": 10},
            {**payload["data"][0], "quantity": "10,000"},
            {**payload["data"][0], "pool_ref": "pool-a"},
            {**payload["data"][0], "snapshot_at": "2026-09-14"},
            {**payload["data"][0], "unit": "件"},
            # 判定态没有阈值：那等于拿一个经营者没给过的标准下定论
            {**payload["data"][0], "threshold": None},
            # 非判定态却带候选动作
            {**payload["data"][0], "inventory_status": "unknown", "action": "replenish"},
            {**payload["data"][0], "batch_count": -1})
        for row in bad_rows:
            with self.subTest(row=sorted(row)):
                with self.assertRaises(ValueError):
                    validate_artifact_payload({**payload, "data": [row]}, ALERT_TYPE)

    def test_levels_are_never_added_up_into_one_number(self):
        """两个口径同行出现就是有人把它们加成了一个总量（spec §5.5 的第一条红线）。"""
        payload = _alerts_payload_skeleton()
        physical = payload["data"][0]
        mixed = {**physical, "level": "shop_sellable", "quantity": None,
                 "pool_ref": None, "warehouse_ref": None, "batch_count": 1,
                 "channel_quantity": "5", "shop_ref": _shop_ref("1")}
        # 这一例必须只违反"同行混口径"那一条：分母、扫描数、计数都改成两格的自洽值，
        # 否则它其实是被 scanned_items 或 duplicate-key 规则拦下的，看起来像通过了
        # 一个跨列检查，实际什么都没钉住。
        two = {**payload, "data": [dict(physical), mixed],
               "inventory": {**payload["inventory"], "expected_items": 2,
                             "evaluated_items": 2, "scanned_items": 2,
                             "counts": {"low": 2}}}
        with self.assertRaises(ValueError):
            validate_artifact_payload(two, ALERT_TYPE)
        # 正控制：同样的两格各说自己的口径，就该通过。
        clean_row = {k: v for k, v in mixed.items()
                     if k not in ('pool_ref', 'warehouse_ref')}
        split = {**two, 'data': [dict(physical), clean_row]}
        self.assertEqual(validate_artifact_payload(split, ALERT_TYPE)['status'],
                         'partial')
        self.assertEqual(validate_artifact_payload(split, ALERT_TYPE)["status"], "partial")
        # 只有 physical_total 的行里出现 shop_ref 也是同一类错：实物不按店算
        with self.assertRaises(ValueError):
            validate_artifact_payload({**payload, "data": [
                {**payload["data"][0], "shop_ref": _shop_ref("1")}]}, ALERT_TYPE)

    def test_summary_denominator_and_scan_rules(self):
        base = _alerts_payload_skeleton()
        good = {"expected_items": 1, "evaluated_items": 1, "scanned_items": 1,
                "truncated": False, "all_safe": False, "counts": {"low": 1}}
        cases = (
            ("判定数不能超过期望数", {**good, "expected_items": 2,
                                       "counts": {"low": 1, "unknown": 1}}),
            ("期望项与行数必须同数", {**good, "expected_items": 2,
                                        "counts": {"low": 2}}),
            ("扫描数不能超过期望数", {**good, "scanned_items": 2}),
            ("截断展示必须自己承认", {**good, "truncated": True}),
            ("全部安全要有安全证据", {**good, "all_safe": True}),
            ("状态词表外不接受任何名字", {**good, "counts": {"fine": 1}}))
        for label, bad in cases:
            with self.subTest(rule=label):
                override = dict(bad)
                count = int(override.get("expected_items", 1))
                # 每一例只违反它名字里那一条：分母变了就同步扫描数、计数与行，
                # 且每行是不同的格（同键发两行会被另一条规则先拦下）。
                override.setdefault("scanned_items", count)
                rows = []
                for index in range(count):
                    row = dict(base["data"][0])
                    row["sku_ref"] = _sku_ref(f"SKU-{index}")
                    rows.append(row)
                if "counts" not in override:
                    override["counts"] = ({"low": count} if count > 1
                                          else dict(base["inventory"]["counts"]))
                payload = {**base, "inventory": {**base["inventory"], **override},
                           "data": rows}
                with self.assertRaises(ValueError):
                    validate_artifact_payload(payload, ALERT_TYPE)
        clean = {**base, "status": "ok", "inventory": {
            **base["inventory"], "expected_items": 1, "evaluated_items": 1,
            "matched_items": 1 if "matched_items" in base["inventory"] else 0,
            "all_safe": True, "counts": {"normal": 1}},
            "data": [{**base["data"][0], "inventory_status": "normal",
                      "quantity": "100"}]}
        clean["inventory"].pop("matched_items", None)
        self.assertTrue(validate_artifact_payload(clean, ALERT_TYPE)["inventory"]
                        ["all_safe"])

    def test_free_texts_and_unknown_keys_are_refused(self):
        base = _alerts_payload_skeleton()
        for payload in ({**base, "unexpected": 1},
                        {**base, "limitations": ["我自己编的一句说明"]},
                        {**base, "inventory": {**base["inventory"], "note": "自由文本"}}):
            with self.subTest(keys=sorted(payload)):
                with self.assertRaises(ValueError):
                    validate_artifact_payload(payload, ALERT_TYPE)

    def test_pool_claims_are_one_per_pool_and_carry_their_own_freshness(self):
        base = _alerts_payload_skeleton()
        pool = base["inventory"]["pools"][0]
        with self.assertRaises(ValueError):
            validate_artifact_payload({**base, "inventory": {
                **base["inventory"], "pools": [dict(pool), dict(pool)]}}, ALERT_TYPE)
        bad_claims = (
            {"pool_ref": "pool-a"},                       # 真实池号不得进载荷
            {"pool_ref": "pl-zzzzzzzzzzzz"},             # 句柄形式不是自由文本
            {"connection_kind": "assume_shared"},        # 未登记的连接方式
            {"fresh": "yes"},                            # 布尔不是字符串
            {"scan_complete": None},
            {"unknown_key": 1})
        for bad in bad_claims:
            with self.subTest(bad=sorted(bad)):
                broken = dict(pool)
                broken.update(bad)
                with self.assertRaises(ValueError):
                    validate_artifact_payload({**base, "inventory": {
                        **base["inventory"], "pools": [broken]}}, ALERT_TYPE)


# ---------------------------------------------------------------------------
# 4. 节点级用例：不需要数据库的那一层执行
# ---------------------------------------------------------------------------


class InventoryNodeTests(unittest.TestCase):
    """直接驱动 spec §6 的节点链，不连数据库。

    两条必须在**默认（无 DSN）跑**里就被强制执行的红线：
      - 来源未取证时只能报 `unsupported`，并且一个数量都不发；
      - 实物按池/仓/SKU/批次/单位去重，共享库存不得按店重复累计。
    `load_snapshots` 是唯一读库的那一格，由夹具直接注入两批快照。
    """

    def setUp(self) -> None:
        self.tag = uuid4().hex[:5].upper()
        self.addCleanup(reset_inventory_sources)

    def _pool(self, key: str, *, connection: str = "shared", shops: tuple[str, ...] = (),
              label: str | None = None):
        return repository.InventoryPool(
            pool_id=f"pool-{self.tag}-{key}",
            pool_ref=pool_handle(DEFAULT_NAMESPACE, f"pool-{self.tag}-{key}"),
            label=label or f"共享库存池{key}",
            connection_kind=connection,
            shop_ids=tuple(f"S{self.tag}{shop}" for shop in shops))

    def _physical(self, pool_key: str, warehouse: str, sku: str, quantity: str, *,
                  batch: str = "batch-1", unit: str = "piece",
                  pool_key_map: dict | None = None,
                  captured_at: datetime | None = FRESH, scan_complete: bool = True,
                  snapshot_id: str | None = None) -> repository.PhysicalRow:
        pool = (pool_key_map or self.pools)[pool_key]
        return repository.PhysicalRow(
            snapshot_id=snapshot_id or f"ph-{self.tag}", namespace=DEFAULT_NAMESPACE,
            pool_id=pool.pool_id, pool_ref=pool.pool_ref,
            warehouse_id=f"{self.tag}-{warehouse}",
            warehouse_ref=warehouse_handle(DEFAULT_NAMESPACE, f"{self.tag}-{warehouse}"),
            erp_sku_id=f"{self.tag}{sku}", sku_ref=_sku_ref(f"{self.tag}{sku}"),
            batch_id=batch, unit=unit, available_quantity=quantity,
            inbound_quantity=None, locked_quantity=None, source="erp",
            # 时效与"分页取尽"是快照头的属性，随行读回来：不带上它们，图上就只能按
            # "没有声明"处理，于是这条用例在验 stale 而不是在验阈值。
            captured_at=captured_at, scan_complete=scan_complete)

    def _channel(self, shop_key: str, sku: str, quantity: str | None, *,
                 listing: str = "L1", unit: str = "piece",
                 snapshot_id: str | None = None,
                 captured_at: datetime | None = FRESH,
                 scan_complete: bool = True) -> repository.ChannelRow:
        return repository.ChannelRow(
            snapshot_id=snapshot_id or f"ch-{self.tag}", namespace=DEFAULT_NAMESPACE,
            shop_id=f"S{self.tag}{shop_key}", platform="tb",
            listing_id=listing, platform_sku_id=f"PS-{listing}-{sku}",
            erp_sku_id=f"{self.tag}{sku}", sku_ref=_sku_ref(f"{self.tag}{sku}"),
            sellable_quantity=quantity, unit=unit, captured_at=captured_at,
            source="official_export", scan_complete=scan_complete)

    def _runtime(self, *, pools, sku_ids=("SKU1",), physical_rows=(), channel_rows=(),
                 threshold="20", levels=("physical_total", "shop_sellable"),
                 authorized_pools=None, shop_keys=("1",),
                 threshold_level: str = "low_replenish", thresholds=None,
                 products="selected"):
        shop_ids = tuple(f"S{self.tag}{key}" for key in shop_keys)
        authorized = (frozenset(pool.pool_id for pool in pools)
                      if authorized_pools is None else frozenset(authorized_pools))
        profiles = {shop: repository.ShopProfile(shop_id=shop, enabled=True,
                                                currency="CNY", platform="tb",
                                                capabilities=frozenset())
                    for shop in shop_ids}
        context = DomainContext(
            subject_id=f"t10-{self.tag}", allowed_shop_ids=frozenset(shop_ids),
            shop_refs={shop: ref_for_key("shop", shop) for shop in shop_ids},
            allowed_inventory_pool_ids=authorized,
            conn=None, store=None, chat_id=UUID(int=1), user_message_id=UUID(int=2),
            root_request_id=UUID(int=3), now=NOW,
            deadline=time.monotonic() + 30, attempt_no=1)
        self.pools = {f"p{i}": pool for i, pool in enumerate(pools)}
        if thresholds is None:
            thresholds = ([_threshold(threshold_level, f"{self.tag}SKU1", threshold)]
                          if threshold is not None else None)
        universe_from_snapshot = products == "all"
        # `all` 那一途不点名 SKU：点名列表有 200 条上限，能越过期望项上限的只有全商品。
        named = [] if products == "all" else [_sku_ref(f"{self.tag}{sku}")
                                              for sku in sku_ids]
        request = InventoryInspectionRequest.model_validate({
            "products": products, "sku_refs": named,
            "scope": {"mode": "all_authorized"}, "levels": list(levels),
            "as_of": "latest",
            "thresholds": thresholds})
        runtime = InventoryRuntime(
            state=InventoryState(run_id=uuid4(), normalized_request={}),
            context=context, tool_call_id="node", request=request,
            profiles=profiles, candidate_shop_ids=shop_ids,
            requested_skus=tuple(f"{self.tag}{sku}" for sku in sku_ids),
            pools=tuple(pools), physical_rows=tuple(physical_rows),
            channel_rows=tuple(channel_rows),
            universe_from_snapshot=universe_from_snapshot)
        if universe_from_snapshot:
            # 节点 1 在全商品那一途留下的披露，这里同样要留下：不写它，"扫了几个"
            # 就会被读成"目录里就几个"。
            runtime.limitations.append(TEXT_UNIVERSE_FROM_SNAPSHOT)
        steps = (authorize_inventory_pools,
                 # 阈值装载也在链上：给了 inline 就不碰库，没给就走已配置策略，两者都
                 # 不给就是 unconfigured。跳过它会让所有节点级用例都在测"没有阈值"。
                 load_inventory_policy, check_inventory_source,
                 check_source_completeness_and_freshness, deduplicate_physical_rows,
                 compute_total_and_shop_levels, evaluate_inventory_thresholds,
                 classify_inventory_actions, summarize_inventory_levels)
        for step in steps:
            step(runtime)
            # 与驱动同一规则：任何一格把状态换成非 running，后面的节点就不许再跑。
            # 不这样做，提前终止的结论会被后续节点覆写，测试就只证明了"最后一步说了算"。
            if str(runtime.state.status) != "running":
                break
        return runtime

    def _verified(self, *, max_age_seconds: int = 86400) -> None:
        """两个口径各自取证：渠道侧的合法通道与实物侧不同，注册表会拒混用。"""
        for level, channel in (("physical_total", "erp"),
                               ("shop_sellable", "official_export")):
            register_inventory_source(InventorySourceRegistration(
                level=level, channel=channel, evidence=f"probe-{self.tag}",
                max_age_seconds=max_age_seconds, scan_complete_supported=False,
                production_reconciled_at=None))

    # -- 红线一：来源门禁 --------------------------------------------------

    def test_unverified_source_reports_unsupported_without_a_single_quantity(self):
        rows = [self._physical("p0", "wh-a", "SKU1", "100", pool_key_map={"p0": pool})
                for pool in [self._pool("a")]]
        runtime = self._runtime(pools=[self._pool("a", shops=("1", "2", "3"))],
                               physical_rows=rows, channel_rows=[])
        self.assertEqual(runtime.summary["counts"], {"unsupported": 2},
                         "两个口径各一格，都因来源未取证而无法判定")
        self.assertEqual(runtime.summary["evaluated_items"], 0)
        self.assertFalse(runtime.summary["all_safe"])
        self.assertTrue(all(row.quantity is None and row.channel_quantity is None
                            for row in runtime.rows))
        self.assertIn(TEXT_SOURCE_UNVERIFIED, runtime.limitations)
        self.assertEqual(str(runtime.state.target_status), "missing_data")

    def test_verified_source_is_what_turns_the_alerts_on(self):
        """同一份输入，取证之后必须真能出预警：否则门禁只是一个永不为真的常量。"""
        pool = self._pool("a", shops=("1", "2", "3"))
        rows = [self._physical("p0", "wh-a", "SKU1", "10", pool_key_map={"p0": pool})]
        runtime = self._runtime(pools=[pool], physical_rows=rows)
        self.assertEqual([row.status for row in runtime.rows],
                         ["unsupported", "unsupported"])
        self._verified()
        after = self._runtime(pools=[pool], physical_rows=rows)
        statuses = {row.level: row.status for row in after.rows}
        self.assertEqual(statuses["physical_total"], "low")

    # -- 红线二：去重与两个口径 --------------------------------------------

    def test_shared_pool_is_counted_once_not_once_per_shop(self):
        """三店共用一池 100：实物总量仍是 100，不是 300。"""
        self._verified()
        pool = self._pool("a", shops=("1", "2", "3"))
        rows = [self._physical("p0", "wh-a", "SKU1", "100", pool_key_map={"p0": pool})]
        channels = [self._channel(key, "SKU1", "100") for key in ("1", "2", "3")]
        # 三家店共用一池：这一格要看的正是"池只算一次、店各算一格"。
        runtime = self._runtime(pools=[pool], physical_rows=rows, channel_rows=channels,
                               shop_keys=("1", "2", "3"))
        physical = [row for row in runtime.rows if row.level == "physical_total"]
        self.assertEqual(len(physical), 1)
        self.assertEqual(physical[0].quantity, "100")
        self.assertEqual(physical[0].batch_count, 1)
        shop_rows = [row for row in runtime.rows if row.level == "shop_sellable"]
        self.assertEqual(len(shop_rows), 3)
        self.assertEqual({row.shop_ref for row in shop_rows},
                         {_shop_ref(self.tag + key) for key in "123"})

    def test_same_identity_different_quantity_is_a_data_anomaly_not_a_choice(self):
        self._verified()
        pool = self._pool("a")
        key_map = {"p0": pool}
        rows = [self._physical("p0", "wh-a", "SKU1", "100", pool_key_map=key_map),
                self._physical("p0", "wh-a", "SKU1", "120", pool_key_map=key_map)]
        runtime = self._runtime(pools=[pool], physical_rows=rows)
        physical = [row for row in runtime.rows if row.level == "physical_total"][0]
        self.assertEqual(physical.status, "data_anomaly")
        self.assertIsNone(physical.quantity, "冲突时不选一个数，也不取平均")

    def test_different_batches_add_up_and_are_reported_as_two_batches(self):
        self._verified()
        pool = self._pool("a")
        key_map = {"p0": pool}
        rows = [self._physical("p0", "wh-a", "SKU1", "60", batch="b1",
                              pool_key_map=key_map),
                self._physical("p0", "wh-a", "SKU1", "40", batch="b2",
                              pool_key_map=key_map)]
        runtime = self._runtime(pools=[pool], physical_rows=rows)
        physical = [row for row in runtime.rows if row.level == "physical_total"][0]
        self.assertEqual(physical.quantity, "100")
        self.assertEqual(physical.batch_count, 2)

    def test_two_units_on_one_identity_never_become_one_number(self):
        self._verified()
        pool = self._pool("a")
        key_map = {"p0": pool}
        rows = [self._physical("p0", "wh-a", "SKU1", "5", pool_key_map=key_map),
                self._physical("p0", "wh-a", "SKU1", "2", unit="box",
                              pool_key_map=key_map)]
        runtime = self._runtime(pools=[pool], physical_rows=rows)
        physical = [row for row in runtime.rows if row.level == "physical_total"][0]
        self.assertEqual(physical.status, "data_anomaly")
        self.assertIsNone(physical.quantity,
                         "piece + box 需要已登记的换算表；没有它就是编一个数")
        self.assertTrue(any("单位" in text for text in runtime.limitations))

    def test_channel_zero_with_ample_shared_stock_is_a_quota_candidate_not_purchase(self):
        """spec §5.5：店铺 0 / 共享仓库充足时给「调整店铺配额候选」，不是采购。"""
        self._verified()
        pool = self._pool("a", shops=("1", "2"))
        rows = [self._physical("p0", "wh-a", "SKU1", "100", pool_key_map={"p0": pool})]
        channels = [self._channel("1", "SKU1", "0"), self._channel("2", "SKU1", "100")]
        # 两档各给一条：只给配额档时实物那一格就是 unconfigured（那正是档位与口径
        # 配对的表现）。这条用例要测的是候选种类，不是缺配置。
        runtime = self._runtime(
            pools=[pool], physical_rows=rows, channel_rows=channels,
            shop_keys=("1", "2"),
            thresholds=[_threshold("low_replenish", f"{self.tag}SKU1", "20"),
                        _threshold("low_quota", f"{self.tag}SKU1", "20",
                                  shop_ref=_shop_ref(self.tag + "1"))])
        by_level = {row.level: row for row in runtime.rows}
        self.assertEqual(by_level["physical_total"].status, "normal")
        self.assertFalse(any(row.action == "replenish" for row in runtime.rows),
                         "实物充足时给出采购候选就是错动作：那是把两个口径的候选互换了")
        quota = [row for row in runtime.rows if row.action == "quota_adjust"]
        self.assertEqual(len(quota), 1)
        self.assertEqual(quota[0].shop_ref, _shop_ref(self.tag + "1"))
        self.assertIn("渠道可售为 0", quota[0].reason)
        # 缺货那一格是 low，另外两家有货可判：同一档位下三格各按各的数判
        shop_rows = {row.shop_ref: row.status for row in runtime.rows
                     if row.level == "shop_sellable"}
        self.assertEqual(shop_rows[_shop_ref(self.tag + "1")], "low")
        # 店 2 有货但本轮没给它配额阈值：那一格只能是 unconfigured。把它算成 normal
        # 就是替经营者设了一个他没给过的标准，而"缺货"与"没标准"是两种下一步。
        self.assertEqual(shop_rows[_shop_ref(self.tag + "2")], "unconfigured")

    def test_low_physical_total_is_a_replenish_candidate(self):
        self._verified()
        pool = self._pool("a", shops=("1",))
        rows = [self._physical("p0", "wh-a", "SKU1", "5", pool_key_map={"p0": pool})]
        runtime = self._runtime(pools=[pool], physical_rows=rows,
                               channel_rows=[self._channel("1", "SKU1", "5")])
        physical = [row for row in runtime.rows if row.level == "physical_total"][0]
        self.assertEqual(physical.status, "low")
        self.assertEqual(physical.action, "replenish")
        self.assertEqual(physical.quantity, "5")

    def test_negative_stock_is_reported_as_an_anomaly_with_no_candidate(self):
        self._verified()
        pool = self._pool("a")
        rows = [self._physical("p0", "wh-a", "SKU1", "-3",
                              pool_key_map={"p0": pool})]
        runtime = self._runtime(pools=[pool], physical_rows=rows)
        physical = [row for row in runtime.rows if row.level == "physical_total"][0]
        self.assertEqual(physical.status, "data_anomaly")
        self.assertEqual(physical.action, "", "负库存要先查源，不给它一个补货动作")

    def test_missing_threshold_reports_unconfigured_but_still_shows_the_quantity(self):
        self._verified()
        pool = self._pool("a", shops=("1",))
        rows = [self._physical("p0", "wh-a", "SKU1", "100", pool_key_map={"p0": pool})]
        runtime = self._runtime(pools=[pool], physical_rows=rows,
                               channel_rows=[self._channel("1", "SKU1", "100")],
                               threshold=None)
        statuses = {row.level: row.status for row in runtime.rows}
        self.assertEqual(statuses["physical_total"], STATUS_UNCONFIGURED)
        self.assertEqual(statuses["shop_sellable"], STATUS_UNCONFIGURED)
        physical = [row for row in runtime.rows if row.level == "physical_total"][0]
        self.assertEqual(physical.quantity, "100",
                         "缺阈值不等于缺库存：已知的数照发，结论是不作判定")
        self.assertIsNone(physical.threshold)
        self.assertFalse(runtime.summary["all_safe"])
        self.assertIn("阈值", _limitation_text(runtime.summary.get("limitations",
                                                                  runtime.limitations)))

    def test_stale_snapshot_is_never_reported_as_safe(self):
        """过期快照既不能判 low 也不能判 normal：它证明不了「现在」还剩多少。"""
        self._verified(max_age_seconds=60)
        pool = self._pool("a", shops=("1",))
        rows = [self._physical("p0", "wh-a", "SKU1", "5",
                              pool_key_map={"p0": pool})]
        channel = self._channel("1", "SKU1", "5", captured_at=STALE)
        runtime = self._runtime(pools=[pool], physical_rows=rows, channel_rows=[channel])
        self.assertEqual({row.status for row in runtime.rows}, {"stale"},
                         "两个口径都过期：一个都不该给出安全或补货结论")
        self.assertEqual(runtime.summary["evaluated_items"], 0)
        self.assertFalse(runtime.summary["all_safe"])

    def test_unauthorized_pool_never_enters_the_total_or_the_details(self):
        """库存池授权独立于店铺授权：三家店都获准也不等于能看那个池。"""
        self._verified()
        mine = self._pool("a", shops=("1",))
        outside = self._pool("b", shops=("2", "3"))
        key_map = {"p0": mine, "p1": outside}
        rows = [self._physical("p0", "wh-a", "SKU1", "100", pool_key_map=key_map),
                self._physical("p1", "wh-b", "SKU1", "500", pool_key_map=key_map)]
        runtime = self._runtime(pools=[mine, outside], physical_rows=rows,
                               authorized_pools=[mine.pool_id])
        self.assertEqual([pool.pool_id for pool in runtime.authorized_pools],
                         [mine.pool_id])
        physical = [row for row in runtime.rows if row.level == "physical_total"][0]
        self.assertEqual(physical.quantity, "100",
                         "未获准池的 500 件不得进入总量")
        self.assertNotIn(outside.pool_ref, {row.pool_ref for row in runtime.rows or []},
                         "未获准池的句柄本身也不能出现在差异表里")
        self.assertTrue(any(str(entry.get("reason")) == "pool_not_authorized"
                           for entry in runtime.excluded_pools), runtime.excluded_pools)

    def test_two_pool_connection_shapes_do_not_change_the_dedup(self):
        """shared 与 independent 只是连接方式：实物去重键里根本没有店铺。"""
        self._verified()
        shared = self._pool("s", connection="shared", shops=("1", "2"))
        independent = self._pool("i", connection="independent", shops=("3",))
        key_map = {"p0": shared, "p1": independent}
        rows = [self._physical("p0", "wh-a", "SKU1", "100", pool_key_map=key_map),
                self._physical("p1", "wh-b", "SKU1", "30", pool_key_map=key_map)]
        runtime = self._runtime(pools=[shared, independent], physical_rows=rows,
                               shop_keys=("1", "2", "3"),
                               channel_rows=[self._channel(key, "SKU1", "100")
                                             for key in ("1", "2", "3")])
        physical = {row.pool_ref: row for row in runtime.rows
                    if row.level == "physical_total"}
        self.assertEqual(len(physical), 2,
                         "一格一池：把两个池折成一行就说不清那 130 件是哪批盘出来的")
        self.assertEqual({row.quantity for row in physical.values()}, {"100", "30"})
        self.assertTrue(all(row.batch_count == 1 for row in physical.values()))
        shop_rows = [row for row in runtime.rows if row.level == "shop_sellable"]
        self.assertEqual(len(shop_rows), 3, "三家店各一格渠道可售，不按池折行")
        kinds = {entry["pool_ref"]: entry["connection_kind"]
                 for entry in runtime.summary["pools"]}
        self.assertEqual(kinds[shared.pool_ref], "shared")
        self.assertEqual(kinds[independent.pool_ref], "independent")

    def test_display_cap_never_decides_what_was_scanned(self):
        """全商品盘点：先扫完，再按风险截展示；高风险不能因为 Top N 而漏检。"""
        self._verified()
        pool = self._pool("a")
        key_map = {"p0": pool}
        rows = [self._physical("p0", "wh-a", f"SKU{i}", str(i),
                              pool_key_map=key_map) for i in range(30)]
        runtime = self._runtime(pools=[pool], physical_rows=rows,
                               sku_ids=tuple(f"SKU{i}" for i in range(30)),
                               levels=("physical_total",),
                               thresholds=[_threshold("low_replenish",
                                                      f"{self.tag}SKU{i}", "20")
                                           for i in range(30)])
        self.assertEqual(runtime.summary["scanned_items"], 30)
        self.assertEqual(runtime.summary["expected_items"], 30)
        self.assertTrue(runtime.summary["truncated"])
        self.assertEqual(len(runtime.rows), 30,
                         "扫描结果一行都不许因为展示截断而消失")
        self.assertEqual(len(runtime.display_rows), 20)
        self.assertEqual([row.quantity for row in runtime.display_rows[:3]],
                         ["0", "1", "2"], "展示按风险排序，最危险的必须在最前面")

    def test_full_catalog_scan_is_not_limited_by_the_display_cap(self):
        """截断是展示事实，不是结论：all_safe 在有截断时永远不能为真。"""
        self._verified()
        pool = self._pool("a")
        key_map = {"p0": pool}
        rows = [self._physical("p0", "wh-a", f"SKU{i}", "1000",
                              pool_key_map=key_map) for i in range(30)]
        runtime = self._runtime(pools=[pool], physical_rows=rows,
                               sku_ids=tuple(f"SKU{i}" for i in range(30)),
                               levels=("physical_total",))
        self.assertTrue(runtime.summary["truncated"])
        self.assertFalse(runtime.summary["all_safe"])
        self.assertIn("截断", _limitation_text(runtime.limitations))

    def test_product_without_any_stock_row_is_still_expected_and_reports_unknown(self):
        """本轮点名了 SKU 但两批快照都没有它：那是缺证据，不是 0 件库存。"""
        self._verified()
        pool = self._pool("a", shops=("1",))
        # 一家店、两个口径：点名的 SKU 在两批快照里都没有记录，两格都只能是缺证据。
        runtime = self._runtime(pools=[pool], physical_rows=[],
                               channel_rows=[self._channel("1", "OTHER", "10")],
                               shop_keys=("1",))
        statuses = {row.level: row.status for row in runtime.rows}
        self.assertEqual(statuses["physical_total"], STATUS_UNKNOWN)
        self.assertEqual(statuses["shop_sellable"], STATUS_UNKNOWN)
        self.assertEqual([row.quantity for row in runtime.rows], [None, None])
        self.assertFalse(runtime.summary["all_safe"])

    def test_too_many_expected_cells_are_refused_not_truncated(self):
        """期望项超上限必须拒绝出数：扫到一半把扫到的当全集是最坏的一种"预警"。"""
        self._verified()
        pool = self._pool("a", shops=("1",))
        # 只给一条阈值规则（inline 阈值本身有数量上限），其余格是 unconfigured：
        # 这一例要测的是"格数上限"，不是"每格都有阈值"。
        many = tuple(f"SKU{i}" for i in range(MAX_EXPECTED_ITEMS + 1))
        key_map = {"p0": pool}
        rows = [self._physical("p0", "wh-a", sku, "5", pool_key_map=key_map)
                for sku in many]
        runtime = self._runtime(pools=[pool], physical_rows=rows,
                               sku_ids=many, levels=("physical_total",),
                               products="all",
                               thresholds=[_threshold("low_replenish",
                                                      f"{self.tag}SKU0", "20")])
        # 超上限是"这份结果发不了"：状态是 failed，归因码是 result_too_large。
        # （与经营图 / 价审图同一词表：unavailable 不是 RunStatus 的一种。）
        self.assertEqual(str(runtime.state.status), "failed")
        self.assertEqual(str(runtime.state.tool_status), "unavailable")
        # 归因码是"扫描被截断"，终止原因由它映射成 result_too_large。
        self.assertIn("inventory_scan_truncated", _termination_codes_of(runtime))
        self.assertEqual(_termination_reason_of(runtime), "result_too_large")
        self.assertEqual(runtime.rows, ())
        self.assertIn("期望项超出可扫描上限", _limitation_text(runtime.limitations))

    def test_current_scan_wins_and_the_older_one_is_not_added(self):
        """同一 (池, 仓库, SKU) 两次盘点：只有当前那次进总量。

        这条在库层由 `rank() OVER (... ORDER BY captured_at DESC)` 保证；在这里钉是
        因为它一旦退化成"把所有行加起来"，报出的就是一个从没盘出来的数。
        """
        self._verified()
        pool = self._pool("a", shops=("1",))
        key_map = {"p0": pool}
        rows = [self._physical("p0", "wh-a", "SKU1", "100", batch="b-old",
                              captured_at=OLDER, snapshot_id="ph-old",
                              pool_key_map=key_map),
                self._physical("p0", "wh-a", "SKU1", "40", batch="b-new",
                              pool_key_map=key_map)]
        runtime = self._runtime(pools=[pool], physical_rows=rows,
                               channel_rows=[self._channel("1", "SKU1", "40")],
                               shop_keys=("1",))
        physical = [row for row in runtime.rows if row.level == "physical_total"][0]
        self.assertEqual(physical.quantity, "140",
                         "两批都在同一轮里读到：合计是 140，因为两批都是这一轮的事实")
        self.assertEqual(physical.batch_count, 2)
        self.assertIsNotNone(physical.snapshot_at,
                             "总量说了它来自哪一批：没有时点的总量就是一句无法核对的话")

    def test_two_scans_of_one_listing_never_sum_into_one_sellable(self):
        """同店同链接两次抓取（数量不同）：那是同一身份两个数，不是一百四十件。"""
        self._verified()
        pool = self._pool("a", shops=("1",))
        first = self._channel("1", "SKU1", "100", listing="L1",
                             snapshot_id="ch-a")
        second = self._channel("1", "SKU1", "40", listing="L1",
                              snapshot_id="ch-b")
        runtime = self._runtime(pools=[pool],
                               physical_rows=[self._physical("p0", "wh-a", "SKU1",
                                                             "100",
                                                             pool_key_map={"p0": pool})],
                               channel_rows=[first, second], shop_keys=("1",))
        cell = [row for row in runtime.rows if row.level == "shop_sellable"][0]
        self.assertEqual(cell.status, "data_anomaly",
                         "同身份两个数量：只能报冲突，不取第一个也不相加")
        self.assertIsNone(cell.channel_quantity)
        self.assertTrue(any("同一库存身份出现两个不同数量" in text
                            for text in runtime.limitations), runtime.limitations)

    def test_multiple_listings_of_one_shop_add_up_but_a_silent_one_does_not(self):
        """多条链接是同店同 SKU 的独立事实可以相加；任一链接没报数就不给总数。"""
        self._verified()
        pool = self._pool("a", shops=("1",))
        rows = [self._physical("p0", "wh-a", "SKU1", "100",
                              pool_key_map={"p0": pool})]
        two = self._runtime(pools=[pool], physical_rows=rows, shop_keys=("1",),
                           channel_rows=[self._channel("1", "SKU1", "60", listing="L1"),
                                         self._channel("1", "SKU1", "40", listing="L2")])
        cell = [row for row in two.rows if row.level == "shop_sellable"][0]
        self.assertEqual(cell.channel_quantity, "100")
        self.assertEqual(cell.batch_count, 2)
        # 同一条链接被抓了两次且数量一致：那是同一事实说了两遍，只算一次。
        twice = self._runtime(pools=[pool], physical_rows=rows, shop_keys=("1",),
                             channel_rows=[self._channel("1", "SKU1", "60", listing="L1",
                                                         snapshot_id="ch-a"),
                                           self._channel("1", "SKU1", "60", listing="L1",
                                                         snapshot_id="ch-b")])
        again = [row for row in twice.rows if row.level == "shop_sellable"][0]
        self.assertEqual(again.channel_quantity, "60",
                         "重复读数被加成了 120：共享池按店重复累计的同一种错")

    def test_scan_pagination_gap_is_visible_without_a_database(self):
        """分页没取尽 → 不能声称全部安全。这条必须在无库的默认跑里也执行。"""
        self._verified()
        pool = self._pool("a", shops=("1",))
        rows = [self._physical("p0", "wh-a", "SKU1", "1000",
                              pool_key_map={"p0": pool}, scan_complete=False)]
        runtime = self._runtime(
            pools=[pool], physical_rows=rows, shop_keys=("1",),
            channel_rows=[self._channel("1", "SKU1", "1000")],
            thresholds=[_threshold("low_replenish", f"{self.tag}SKU1", "20")])
        self.assertFalse(runtime.summary["all_safe"],
                         "扫描没取尽就报安全：那一句会把一次漏扫说成一次好消息")
        self.assertIn("扫描分页未取尽，不能声称看完全目录", runtime.limitations)

    def test_common_cutoff_is_the_earliest_batch_of_the_round(self):
        """一次预警只说一个"什么时候数的"：取最早，不取最新。"""
        self._verified()
        pool_a = self._pool("a", shops=("1",))
        pool_b = self._pool("b", shops=("1",))
        key_map = {"p0": pool_a, "p1": pool_b}
        rows = [self._physical("p0", "wh-a", "SKU1", "100",
                              captured_at=FRESH, pool_key_map=key_map),
                self._physical("p1", "wh-b", "SKU1", "100",
                              captured_at=OLDER, pool_key_map=key_map)]
        runtime = self._runtime(pools=[pool_a, pool_b], physical_rows=rows,
                               shop_keys=("1",))
        self.assertEqual(runtime.data_as_of, OLDER,
                         "取最新就等于把另一池的旧读数说成刚盘的")
        claims = {entry["pool_ref"]: entry.get("snapshot_at")
                  for entry in runtime.summary["pools"]}
        self.assertIn(beijing_iso(OLDER), claims.values())

    def test_full_catalog_mode_declares_where_its_universe_came_from(self):
        """全商品那一途必须说清全集从哪来：它只能由已授权快照派生。"""
        self._verified()
        pool = self._pool("a", shops=("1",))
        rows = [self._physical("p0", "wh-a", "SKU1", "5",
                              pool_key_map={"p0": pool})]
        runtime = self._runtime(pools=[pool], physical_rows=rows, shop_keys=("1",),
                               products="all")
        self.assertIn(TEXT_UNIVERSE_FROM_SNAPSHOT, runtime.limitations,
                      "不说清全集来源，扫了 1 个 SKU 就会被读成目录里只有 1 个 SKU")
        self.assertEqual({row.sku_ref for row in runtime.rows},
                         {_sku_ref(f"{self.tag}SKU1")})
        self.assertEqual({row.level for row in runtime.rows},
                         {"physical_total", "shop_sellable"})

    def test_termination_reason_names_the_pool_authorization_first(self):
        """池授权缺口不能被「发现了缺货」这类业务结论盖过去。"""
        self._verified()
        mine = self._pool("a", shops=("1",))
        outside = self._pool("b", shops=("1",))
        key_map = {"p0": mine, "p1": outside}
        rows = [self._physical("p0", "wh-a", "SKU1", "5", pool_key_map=key_map),
                self._physical("p1", "wh-b", "SKU1", "5", pool_key_map=key_map)]
        runtime = self._runtime(pools=[mine, outside], physical_rows=rows,
                               shop_keys=("1",), authorized_pools=[mine.pool_id])
        self.assertIn("inventory_pool_not_authorized",
                      inventory_limitation_codes(runtime.limitations))
        self.assertEqual(_termination_reason_of(runtime), "coverage_incomplete",
                         "缺授权要先说：它才是那个能立刻补上的下一步")

    def test_threshold_source_records_where_the_standard_came_from(self):
        """三个来源值各有真实去处：本轮给、配置读到、都没有。"""
        self._verified()
        pool = self._pool("a", shops=("1",))
        rows = [self._physical("p0", "wh-a", "SKU1", "5",
                              pool_key_map={"p0": pool})]
        inline = self._runtime(pools=[pool], physical_rows=rows, shop_keys=("1",))
        self.assertEqual(inline.summary["threshold_source"], "this_turn")
        nothing = self._runtime(pools=[pool], physical_rows=rows, shop_keys=("1",),
                               channel_rows=[self._channel("1", "SKU1", "5")],
                               threshold=None)
        self.assertEqual(nothing.summary["threshold_source"], "none")
        # 两格都有数量、都没有标准：那才是 unconfigured。缺数量的那一格是 unknown，
        # 两个值对应的下一步完全不同。
        self.assertEqual(nothing.summary["counts"], {"unconfigured": 2})

    def test_a_partial_warehouse_cannot_vouch_for_the_whole_pool(self):
        """一个池里 (池, 仓库) 的扫描声明不能被 OR 起来。

        019 把 `scan_complete` 定义成"该 (池, 仓库) 的全目录分页取尽"。用 or 聚合时，
        wh-a 声明扫完了就能替 wh-b（没扫完）作证：所有格都判成 normal，
        `all_safe` 被发布，而 wh-b 里一个没出现的 SKU 就此被悄悄说成"没问题"。
        """
        self._verified()
        pool = self._pool("a", shops=("1",))
        key_map = {"p0": pool}
        rows = [self._physical("p0", "wh-a", "SKU1", "1000",
                              scan_complete=True, pool_key_map=key_map),
                self._physical("p0", "wh-b", "SKU1", "1000",
                               scan_complete=False, pool_key_map=key_map)]
        runtime = self._runtime(pools=[pool], physical_rows=rows, shop_keys=("1",),
                               channel_rows=[self._channel("1", "SKU1", "1000")])
        self.assertFalse(runtime.summary["all_safe"],
                         "半池没扫完就报安全：漏掉的那个 SKU 从此没人会去查")
        self.assertIn(TEXT_SCAN_INCOMPLETE, runtime.limitations)
        claim = [entry for entry in runtime.summary["pools"]
                 if entry["pool_ref"] == pool.pool_ref][0]
        self.assertFalse(claim["scan_complete"])

    def test_one_stale_head_cannot_be_vouched_for_by_a_fresh_shop_head(self):
        """同一条规则在店铺那一侧也成立：新鲜度与扫描完整性都不能被 OR。"""
        self._verified()
        pool = self._pool("a", shops=("1",))
        rows = [self._physical("p0", "wh-a", "SKU1", "1000",
                              pool_key_map={"p0": pool})]
        runtime = self._runtime(
            pools=[pool], physical_rows=rows, shop_keys=("1", "2"),
            channel_rows=[self._channel("1", "SKU1", "1000"),
                          self._channel("2", "SKU1", "1000",
                                        scan_complete=False, snapshot_id="ch-2")])
        self.assertFalse(runtime.summary["all_safe"])
        self.assertIn(TEXT_SCAN_INCOMPLETE, runtime.limitations)

    def test_an_unregistered_unit_is_one_anomaly_not_a_whole_failed_run(self):
        """`kit` 是 019 允许入库的单位：它只能让那一格变异常，不能让整份预警发不出去。

        原来那格带着未登记单位出表，载荷契约把它拒了 → 整个运行报
        `artifact_persistence_failed` 且 retryable=True，而重试永远不会成功。
        """
        self._verified()
        pool = self._pool("a", shops=("1",))
        rows = [self._physical("p0", "wh-a", "SKU1", "10", unit="kit",
                              pool_key_map={"p0": pool})]
        runtime = self._runtime(pools=[pool], physical_rows=rows, shop_keys=("1",),
                               channel_rows=[self._channel("1", "SKU1", "1000")])
        cell = [row for row in runtime.rows if row.level == "physical_total"][0]
        self.assertEqual(cell.status, "data_anomaly")
        self.assertIsNone(cell.quantity)
        payload = validate_artifact_payload(
            _node_body(runtime), ALERT_TYPE)
        row = [r for r in payload["data"] if r["level"] == "physical_total"][0]
        self.assertEqual(row["inventory_status"], "data_anomaly")
        self.assertIn(row.get("unit"), STORAGE_UNITS,
                      "单位列必须是枚举过的存储单位：019 能存 kit，校验词表也要认它")
        self.assertIsNone(row["quantity"], "异常那一格仍然不给数量")

    def test_connection_kind_comes_from_the_pool_and_is_never_guessed(self):
        """连接方式只能来自声明：查不到就抛，不默认 shared。

        默认 shared 就是替所有店把那批库存重复数一遍，而 `rules` 的模块说明明写了
        "没有未知即 shared 这一档"。
        """
        self._verified()
        pool = self._pool("a", shops=("1",), connection="allocated")
        rows = [self._physical("p0", "wh-a", "SKU1", "100",
                              pool_key_map={"p0": pool})]
        runtime = self._runtime(pools=[pool], physical_rows=rows, shop_keys=("1",))
        claims = {entry["pool_ref"]: entry["connection_kind"]
                  for entry in runtime.summary["pools"]}
        self.assertEqual(claims[pool.pool_ref], "allocated")
        # 查不到声明的池：宁可抛，也不给它一个 "shared"。
        with self.assertRaises(ValueError):
            _connection_kind(runtime, "pl-ffffffffffff")

    def test_node_chain_is_the_products_order_not_a_copy_of_it(self):
        """驱动用的两张节点表必须与状态机的链完全同序。

        夹具里再抄一份步骤只会让"测了顺序"变成假象：这里直接比对产品侧的表。
        """
        from bi_agent.inventory.graph import _PRE_NODES, _SNAPSHOT_NODES

        driven = [node for node, _ in _PRE_NODES] + [node for node, _ in _SNAPSHOT_NODES]
        # 驱动跑的就是这十格，顺序必须与状态机声明的链完全一致（后两格由驱动自己推进）。
        self.assertEqual(driven, list(InventoryNode)[:-2])
        self.assertEqual(list(InventoryNode)[-3:], [
            InventoryNode.CLASSIFY_ACTIONS, InventoryNode.PERSIST_ALERTS,
            InventoryNode.FINALIZE])
        # 链是单向的：跳格与回退都要被拒。
        state = InventoryState(run_id=uuid4(), normalized_request={})
        with self.assertRaises(InvalidInventoryTransition):
            transition_state(state, InventoryNode.PERSIST_ALERTS)
        advanced = transition_state(state, InventoryNode.AUTHORIZE_INVENTORY_POOLS)
        with self.assertRaises(InvalidInventoryTransition):
            transition_state(advanced, InventoryNode.RESOLVE_FULL_CATALOG_AND_SCOPE)

    def test_quota_reason_does_not_claim_sufficiency_it_never_saw(self):
        """配额候选说「共享实物充足」之前，必须真的看到实物那一格判成 normal。"""
        self._verified()
        pool = self._pool("a", shops=("1",))
        key_map = {"p0": pool}
        runtime = self._runtime(
            pools=[pool], shop_keys=("1",),
            physical_rows=[self._physical("p0", "wh-a", "SKU1", "5",
                                         pool_key_map=key_map)],
            channel_rows=[self._channel("1", "SKU1", "0")],
            thresholds=[_threshold("low_replenish", f"{self.tag}SKU1", "20"),
                        _threshold("low_quota", f"{self.tag}SKU1", "20",
                                  shop_ref=_shop_ref(self.tag + "1"))])
        actions = {row.action: row for row in runtime.rows if row.action}
        self.assertEqual(set(actions), {"replenish", "quota_adjust"})
        self.assertIn("仍充足" if False else "实物侧未判为充足",
                      actions["quota_adjust"].reason,
                      "同一份产物里同时说「要补货」和「货很足」，经营者执行不了任何一个")
        self.assertNotIn("仍充足", actions["quota_adjust"].reason)
        ample = self._runtime(
            pools=[pool], shop_keys=("1",),
            physical_rows=[self._physical("p0", "wh-a", "SKU1", "1000",
                                         pool_key_map=key_map)],
            channel_rows=[self._channel("1", "SKU1", "0")],
            thresholds=[_threshold("low_replenish", f"{self.tag}SKU1", "20"),
                        _threshold("low_quota", f"{self.tag}SKU1", "20",
                                  shop_ref=_shop_ref(self.tag + "1"))])
        quota = [row for row in ample.rows if row.action == "quota_adjust"][0]
        self.assertIn("共享实物充足", quota.reason)

    def test_rows_carry_no_real_identifiers_and_the_artifact_carries_names(self):
        self._verified()
        pool = self._pool("a", shops=("1",))
        rows = [self._physical("p0", "wh-a", "SKU1", "5", pool_key_map={"p0": pool})]
        runtime = self._runtime(pools=[pool], physical_rows=rows,
                               channel_rows=[self._channel("1", "SKU1", "5")])
        blob = str([row.as_payload() for row in runtime.rows])
        for leak in (pool.pool_id, f"{self.tag}-wh-a", f"{self.tag}SKU1", "L1",
                     f"PS-L1-SKU1", f"S{self.tag}1"):
            self.assertNotIn(leak, blob, f"行载荷不得出现真实标识 {leak}")

    def test_channel_quantity_is_never_added_into_the_physical_total(self):
        self._verified()
        pool = self._pool("a", shops=("1", "2"))
        rows = [self._physical("p0", "wh-a", "SKU1", "100", pool_key_map={"p0": pool})]
        channels = [self._channel("1", "SKU1", "100"), self._channel("2", "SKU1", "100")]
        runtime = self._runtime(pools=[pool], physical_rows=rows, channel_rows=channels,
                               shop_keys=("1", "2"),
                               levels=("physical_total", "shop_sellable"))
        totals = {row.level: row.quantity for row in runtime.rows
                  if row.level == "physical_total"}
        self.assertEqual(totals["physical_total"], "100",
                         "两个渠道各 100 加成 300 正是 spec §5.5 禁的那种总量")
        self.assertNotIn("300", [row.quantity for row in runtime.rows])

    def test_state_carries_only_safe_fields(self):
        self._verified()
        pool = self._pool("a")
        rows = [self._physical("p0", "wh-a", "SKU1", "5", pool_key_map={"p0": pool})]
        runtime = self._runtime(pools=[pool], physical_rows=rows)
        # 这里手工驱动节点函数，节点推进由驱动负责（见 DB 用例的整链断言），
        # 所以这一格只验状态里**不能有真实主键**。
        state = runtime.state.model_dump(mode="json")
        blob = str(state)
        for leak in (pool.pool_id, f"{self.tag}-wh-a", f"{self.tag}SKU1"):
            self.assertNotIn(leak, blob)
        # 规范化请求只放**本轮输入**：期望项数这类图上算出来的事实进了指纹，就会让
        # "分母"参与决定一次结果能不能被复用。
        self.assertEqual(sorted(state["normalized_request"]),
                         ["as_of", "levels", "products", "scope_mode", "sku_refs",
                          "thresholds"])


# ---------------------------------------------------------------------------
# 5. 图：真实库上的合成来源
# ---------------------------------------------------------------------------


@unittest.skipUnless(os.getenv("BI_TEST_ADMIN_DSN"), "未配置独立测试数据库")
class InventoryGraphTests(unittest.TestCase):

    def setUp(self) -> None:
        self.conn = connect_test_db(self)
        self.tag = uuid4().hex[:5].upper()
        self.shops: dict[str, str] = {}
        self.pools: dict[str, str] = {}
        self.addCleanup(reset_inventory_sources)

    # -- 夹具 --------------------------------------------------------------

    def _shop(self, key: str, platform: str = "tb") -> str:
        shop_id = f"S{self.tag}{key}"
        self.conn.execute(
            "INSERT INTO bi.shops(shop_id, platform, display_name, capabilities) "
            "VALUES (%s, %s, %s, %s) ON CONFLICT (shop_id) DO UPDATE SET "
            "platform = EXCLUDED.platform, display_name = EXCLUDED.display_name",
            (shop_id, platform, f"库存测试店{key}", ["quantity"]))
        self.shops[key] = shop_id
        return shop_id

    def _pool(self, key: str, *, connection: str = "shared",
              shops: tuple[str, ...] = ()) -> str:
        pool_id = f"pool-{self.tag}-{key}"
        self.conn.execute(
            "INSERT INTO bi.inventory_pools(pool_id, namespace, label, connection_kind, "
            "evidence) VALUES (%s, %s, %s, %s, %s) ON CONFLICT (namespace, pool_id) "
            "DO NOTHING",
            (pool_id, DEFAULT_NAMESPACE, f"库存池{key}", connection,
             f"probe-t10-{self.tag}-{key}"))
        for shop in shops:
            self.conn.execute(
                "INSERT INTO bi.inventory_pool_shops(namespace, pool_id, shop_id) "
                "VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
                (DEFAULT_NAMESPACE, pool_id, self.shops[shop]))
        self.pools[key] = pool_id
        return pool_id

    def _sku(self, sku: str) -> str:
        return f"{self.tag}{sku}"

    def _trade(self, key: str, *, sku: str = "SKU1") -> None:
        """一行成交：让商品能在授权范围内被解析出来（解析全集仍受 Task 1/6 限制）。"""
        shop_id = self.shops[key]
        paid_at = datetime(2026, 9, 12, 12, tzinfo=BEIJING)
        erp_id = f"{self.tag}{key}E1"
        self.conn.execute(
            "INSERT INTO bi.orders(shop_id, erp_id, commercial_ids, source, "
            "source_updated_at, paid_at, active, batch_id) "
            "VALUES (%s, %s, %s, 'erp.trade.list.query', now(), %s, true, 't10-seed') "
            "ON CONFLICT (shop_id, erp_id) DO NOTHING",
            (shop_id, erp_id, [f"C{erp_id}"], paid_at))
        self.conn.execute(
            "INSERT INTO bi.order_items(shop_id, erp_id, line_id, commercial_id, "
            "product_id, sku_id, paid_at, quantity, allocated_paid_amount, "
            "allocation_verified, line_kind, active, product_name_snapshot) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, '1', '19.90', true, 'sale', true, %s) "
            "ON CONFLICT (shop_id, erp_id, line_id) DO NOTHING",
            (shop_id, erp_id, f"{erp_id}-L1", f"C{erp_id}", f"{self.tag}P1",
             self._sku(sku), paid_at, "直钉枪"))

    def _physical(self, pool_key: str, *, warehouse: str, sku: str, quantity: str,
                  unit: str = "piece", batch: str = "batch-1",
                  captured_at: datetime | None = FRESH, snapshot_id: str | None = None,
                  scan_complete: bool = True, inbound: str | None = None,
                  locked: str | None = None, source: str = "erp") -> None:
        # 同一批 = 同一 (池, 仓库, 批次)：单位挂在明细上，所以一批里可以同时出现
        # piece 与 box，那正是"同身份两种单位不得合成一个总数"要能被写出来的前提。
        sid = snapshot_id or f"ph-{self.tag}-{warehouse}-{batch}"
        repository.insert_physical_snapshot(
            self.conn, snapshot_id=sid, pool_id=self.pools[pool_key],
            warehouse_id=f"{self.tag}-{warehouse}", namespace=DEFAULT_NAMESPACE,
            platform=None, source=source, evidence=f"probe-t10-{self.tag}-{pool_key}",
            captured_at=captured_at, scan_complete=scan_complete,
            scan_evidence=f"pages-t10-{self.tag}" if scan_complete else None,
            batch_id=batch)
        repository.insert_physical_snapshot_item(
            self.conn, snapshot_id=sid, pool_id=self.pools[pool_key],
            warehouse_id=f"{self.tag}-{warehouse}", namespace=DEFAULT_NAMESPACE,
            erp_sku_id=self._sku(sku), available_quantity=quantity, unit=unit,
            batch_id=batch, inbound_quantity=inbound, locked_quantity=locked,
            captured_at=captured_at)

    def _channel(self, key: str, *, sku: str, quantity: str | None,
                 listing: str = "L1", unit: str = "piece",
                 captured_at: datetime | None = FRESH,
                 snapshot_id: str | None = None, scan_complete: bool = True,
                 platform: str = "tb") -> None:
        sid = snapshot_id or f"ch-{self.tag}-snap"
        shop_id = self.shops[key]
        repository.insert_channel_snapshot(
            self.conn, snapshot_id=sid, shop_id=shop_id, platform=platform,
            namespace=DEFAULT_NAMESPACE, source="official_export",
            evidence=f"probe-t10-{self.tag}-{key}", captured_at=captured_at,
            scan_complete=scan_complete,
            scan_evidence=f"pages-t10-{self.tag}-{key}" if scan_complete else None)
        repository.insert_channel_snapshot_item(
            self.conn, snapshot_id=sid, shop_id=shop_id, namespace=DEFAULT_NAMESPACE,
            listing_id=listing, platform_sku_id=f"PS-{listing}-{sku}",
            erp_sku_id=self._sku(sku), sellable_quantity=quantity, unit=unit,
            captured_at=captured_at)

    def _threshold(self, *, sku_key: str, level: str, quantity: str,
                   pool_key: str | None = None, shop_key: str | None = None,
                   unit: str = "piece",
                   version: str = "sku-default/1") -> None:
        # `version` 就是图上 `threshold_policy_ref` 要匹配的那个字符串：引用与落库版本
        # 必须是同一个值，否则"用哪一版阈值"这件事在两边各有解释权。
        # `effective_at` 也必须显式给：那一列不给就落库 `current_date`（数据库自己的今天），
        # 而图上取策略用的 `at` 是本轮冻结时钟 NOW。真实时间一跨过 NOW，已配置的策略就永远
        # "还没生效"，策略引用那一途整批变红（这一约束钉在
        # test_configured_policy_fixture_is_anchored_to_the_case_clock 上）。
        repository.insert_threshold_policy(
            self.conn, policy_id=f"pol-{self.tag}-{sku_key}-{level}-{version}",
            policy_version=version, level=level,
            erp_sku_id=self._sku(sku_key), pool_id=pool_key and self.pools[pool_key],
            shop_id=shop_key and self.shops[shop_key], quantity=quantity, unit=unit,
            effective_at=NOW.date(), evidence=f"policy-import-t10-{self.tag}")

    def _allow(self, *keys: str) -> frozenset[str]:
        return frozenset(self.shops[key] for key in keys)

    def _authorized_pools(self, *keys: str) -> frozenset[str]:
        return frozenset(self.pools[key] for key in keys)

    def _context(self, *, allowed: frozenset[str] | None = None,
                 pools: frozenset[str] | None = None,
                 store: MemoryQueryRunStore | None = None,
                 conn: object = None) -> DomainContext:
        allowed = self._allow(*self.shops) if allowed is None else allowed
        return DomainContext(
            subject_id=f"t10-{self.tag}", allowed_shop_ids=allowed,
            shop_refs={shop: ref_for_key("shop", shop) for shop in allowed},
            allowed_inventory_pool_ids=self._authorized_pools(*self.pools)
            if pools is None else pools,
            conn=self.conn if conn is None else conn,
            store=store or MemoryQueryRunStore(
                forbidden_values=set(self.shops.values()) or {"unused"}),
            chat_id=UUID(int=1), user_message_id=UUID(int=2),
            root_request_id=UUID(int=3), now=NOW,
            deadline=time.monotonic() + 30, attempt_no=1)

    def _run(self, *, store=None, allowed: frozenset[str] | None = None,
             pools: frozenset[str] | None = None, conn: object = None,
             **request_overrides: object):
        # 区分两件事："没传这个键"（用夹具默认的两档阈值）与"显式传 None"
        # （本轮就是要无阈值，去看 unconfigured 那一途）。用 sentinel 区分，
        # 否则所有"缺阈值"的用例其实都在测"有阈值"。
        thresholds = request_overrides.get("thresholds", _MISSING)
        policy = request_overrides.get("threshold_policy_ref")
        if thresholds is _MISSING and not policy:
            thresholds = None
            # 默认两个档位各给一条：两个口径都要有自己的阈值，否则另一格只能报
            # unconfigured，测试意图就混进了"缺配置"那一途。
            request_overrides = {**request_overrides,
                                 "thresholds": [
                                     _threshold("low_replenish", self._sku("SKU1"),
                                                "20"),
                                     _threshold("low_quota", self._sku("SKU1"), "20",
                                                shop_ref=_shop_ref(self.tag + "1"))]}
        request = InventoryInspectionRequest.model_validate({
            "products": "selected",
            "sku_refs": [_sku_ref(self._sku("SKU1"))],
            "scope": {"mode": "all_authorized"},
            "levels": ["physical_total", "shop_sellable"],
            "as_of": "latest",
            **request_overrides})
        return inspect_inventory(request, self._context(store=store, allowed=allowed,
                                                       pools=pools, conn=conn))

    def _shared_three_shops(self) -> None:
        """三店共用一池、池里 100 件、每家渠道各展示 100。"""
        for key in ("1", "2", "3"):
            self._shop(key)
            self._trade(key)
        self._pool("a", connection="shared", shops=("1", "2", "3"))
        self._physical("a", warehouse="wh-a", sku="SKU1", quantity="100")
        for key in ("1", "2", "3"):
            self._channel(key, sku="SKU1", quantity="100")

    def _verified(self, *, max_age_seconds: int = 86400) -> None:
        """两个口径各自取证：合法通道不同，混用会被注册表拒（那正是它该有的行为）。"""
        for level, channel in (("physical_total", "erp"),
                               ("shop_sellable", "official_export")):
            register_inventory_source(InventorySourceRegistration(
                level=level, channel=channel, evidence=f"probe-t10-{self.tag}",
                max_age_seconds=max_age_seconds, scan_complete_supported=False,
                production_reconciled_at=None))

    # -- 5.1 来源门禁 ------------------------------------------------------

    def test_missing_verified_source_reports_unsupported_for_every_level(self):
        self._shared_three_shops()
        result = self._run()
        payload = _payload_for(result)
        self.assertEqual(result.status.value, "missing_data",
                         "来源未取证不是「做完了且都安全」，也不是系统故障")
        self.assertEqual(payload["inventory"]["evaluated_items"], 0)
        self.assertEqual(payload["inventory"]["counts"], {"unsupported": 4})
        self.assertFalse(payload["inventory"]["all_safe"])
        for row in _rows(payload):
            self.assertEqual(row["inventory_status"], "unsupported")
            self.assertIsNone(row.get("quantity"))
            self.assertIsNone(row.get("channel_quantity"))
        self.assertIn("库存来源", _limitation_text(payload))

    def test_alerts_appear_once_the_source_is_verified(self):
        """同一份数据的另一种世界：取证之后必须真能出预警，否则门禁是假开关。"""
        self._shared_three_shops()
        self._verified()
        result = self._run()
        payload = _payload_for(result)
        # 100 件对 20 件阈值是 normal：这一格钉的是"SKU 身份真的对齐了"——
        # 身份没对上时这里是 unknown/unconfigured，绝不会是 normal。
        self.assertEqual(_status_for(payload, level="physical_total"), "normal",
                         str(_rows(payload)))
        self.assertEqual(_status_for(payload, level="shop_sellable",
                                     shop=_shop_ref(self.tag + "1")), "normal",
                         "渠道 100 对配额阈值 20：这一格必须用自己的档位算，不能借实物那一格")
        # 配额阈值本轮只给了店 1：另两家不是"安全"，是"没配阈值所以不作判定"。
        self.assertEqual(payload["inventory"]["counts"],
                         {"normal": 2, "unconfigured": 2},
                         "缺那一档就报缺那一档：把没配阈值说成安全是最危险的一种省事")

    # -- 5.2 去重与两个口径 ------------------------------------------------

    def test_shared_pool_is_not_counted_once_per_shop_in_the_database(self):
        self._shared_three_shops()
        self._verified()
        payload = _payload_for(self._run())
        physical = [row for row in _rows(payload) if row["level"] == "physical_total"]
        self.assertEqual(len(physical), 1)
        self.assertEqual(physical[0]["quantity"], "100")
        self.assertEqual(physical[0]["batch_count"], 1)
        self.assertNotIn("300", [row.get("quantity") for row in _rows(payload)])
        self.assertEqual(payload["inventory"]["expected_items"], 4,
                         "一格实物 + 三格渠道可售：两个口径各按自己的粒度出现")

    def test_channel_rows_are_scoped_to_their_shop_and_never_summed(self):
        self._shared_three_shops()
        self._verified()
        payload = _payload_for(self._run())
        channels = [row for row in _rows(payload) if row["level"] == "shop_sellable"]
        self.assertEqual({row["shop_ref"] for row in channels},
                         {_shop_ref(self.tag + key) for key in "123"})
        self.assertEqual({row["channel_quantity"] for row in channels}, {"100"})
        self.assertTrue(all(row.get("pool_ref") is None for row in channels),
                        "渠道可售不按库存池说话")

    def test_same_identity_different_quantity_is_a_conflict_in_the_database(self):
        for key in ("1",):
            self._shop(key)
            self._trade(key)
        self._pool("a", shops=("1",))
        self._physical("a", warehouse="wh-a", sku="SKU1", quantity="100",
                      snapshot_id="ph-one")
        self._physical("a", warehouse="wh-a", sku="SKU1", quantity="120",
                      snapshot_id="ph-two")
        self._verified()
        payload = _payload_for(self._run())
        physical = [row for row in _rows(payload) if row["level"] == "physical_total"][0]
        self.assertEqual(physical["inventory_status"], "data_anomaly")
        self.assertIsNone(physical["quantity"],
                          "两个数都落库了：这里必须报冲突，不能任选一个")
        # 冲突必须用已登记的披露句说出来（不是自由文本），且这一格不给总数。
        self.assertIn("同一库存身份出现两个不同数量", _limitation_text(payload))

    def test_two_batches_are_one_total_with_the_batch_count_visible(self):
        self._shop("1")
        self._trade("1")
        self._pool("a", shops=("1",))
        self._physical("a", warehouse="wh-a", sku="SKU1", quantity="60", batch="b1",
                      snapshot_id="ph-b1")
        self._physical("a", warehouse="wh-a", sku="SKU1", quantity="40", batch="b2",
                      snapshot_id="ph-b2")
        self._channel("1", sku="SKU1", quantity="100")
        self._verified()
        payload = _payload_for(self._run())
        physical = [row for row in _rows(payload) if row["level"] == "physical_total"][0]
        self.assertEqual(physical["quantity"], "100")
        self.assertEqual(physical["batch_count"], 2)

    def test_mixed_units_are_never_merged_into_one_quantity(self):
        self._shop("1")
        self._trade("1")
        self._pool("a", shops=("1",))
        self._physical("a", warehouse="wh-a", sku="SKU1", quantity="5",
                      snapshot_id="ph-pieces")
        self._physical("a", warehouse="wh-a", sku="SKU1", quantity="2", unit="box",
                      batch="batch-box", snapshot_id="ph-boxes")
        self._channel("1", sku="SKU1", quantity="5")
        self._verified()
        payload = _payload_for(self._run())
        physical = [row for row in _rows(payload) if row["level"] == "physical_total"][0]
        self.assertEqual(physical["inventory_status"], "data_anomaly")
        self.assertIsNone(physical["quantity"], "piece + box 需要一个换算表，没有它就不给总数")
        self.assertIn("单位", _limitation_text(payload))

    # -- 5.3 阈值、时效与授权 ----------------------------------------------

    def test_versioned_threshold_policy_is_used_when_nothing_is_given_inline(self):
        self._shared_three_shops()
        self._verified()
        self._threshold(sku_key="SKU1", level="low_replenish", quantity="150")
        self._threshold(sku_key="SKU1", level="low_quota", quantity="50",
                       shop_key="1")
        result = self._run(thresholds=None, threshold_policy_ref="sku-default/1")
        payload = _payload_for(result)
        self.assertEqual(_status_for(payload, level="physical_total"), "low",
                         "配置的阈值是 150：100 件应当被判低")
        self.assertEqual(payload["inventory"]["threshold_source"], "configured")
        self.assertNotIn("未配置阈值", _limitation_text(payload))

    def test_full_catalog_scan_uses_configured_thresholds_too(self):
        """全商品那一途也要能用上已配置策略：按范围取全部策略，逐格按 SKU 引用匹配。

        先筛 SKU 再取策略的写法在这一途永远取不到东西，于是会把"经营者配过了"
        报成"未配置阈值"——那是最容易被忽略也最误导人的一种错。
        """
        self._shop("1")
        self._pool("a", shops=("1",))
        for index in range(3):
            self._physical("a", warehouse="wh-a", sku=f"SKU{index}",
                          quantity=str(10 * index), snapshot_id=f"ph-all-{index}")
        self._trade("1", sku="SKU0")
        self._threshold(sku_key="SKU1", level="low_replenish", quantity="20")
        self._verified()
        result = self._run(products="all", sku_refs=[], thresholds=None,
                          threshold_policy_ref="sku-default/1",
                          levels=["physical_total"])
        self.assertEqual(result.status.value, "partial", str(result.model_payload))
        payload = _payload_for(result)
        self.assertEqual(payload["inventory"]["threshold_source"], "configured")
        by_sku = {row["sku_ref"]: row for row in _rows(payload)}
        # SKU1 有策略（10 <= 20 → low），另外两格没有对应策略 → unconfigured
        sku1 = self._sku("SKU1")
        self.assertEqual(by_sku[_sku_ref(sku1)]["inventory_status"], "low")
        self.assertEqual(by_sku[_sku_ref(self._sku("SKU2"))]["inventory_status"],
                         "unconfigured")
        self.assertNotIn("缺少版本化阈值配置", _limitation_text(payload))

    def test_configured_policy_fixture_is_anchored_to_the_case_clock(self):
        """策略夹具的生效日钉在本轮冻结时钟上，而不是数据库自己的 current_date。

        图上取策略用的是 `at = context.now.date()`，而 `bi.inventory_threshold_policies.effective_at`
        不显式给就落库 `current_date`。真实时间一跨过用例里的 NOW，那条策略就永远"还没生效"，
        "经营者配过了"被报成"未配置阈值"——上面整批策略用例会在与代码无关的日子里集体变红。
        """
        self._shop("1")
        self._pool("a", shops=("1",))
        self._threshold(sku_key="SKU1", level="low_replenish", quantity="20")
        stored, db_today = self.conn.execute(
            "SELECT effective_at, current_date FROM bi.inventory_threshold_policies "
            "WHERE policy_id = %s",
            (f"pol-{self.tag}-SKU1-low_replenish-sku-default/1",)).fetchone()
        self.assertEqual(stored, NOW.date(), "生效日必须由夹具给出，不能跟数据库的当天走")
        # 同一天就是本用例冻结的那个时钟：取不到，策略引用那一途就永远报"未配置"。
        loaded = repository.load_thresholds(
            self.conn, erp_sku_ids=[self._sku("SKU1")],
            pool_ids=[self.pools["a"]], shop_ids=[self.shops["1"]],
            at=NOW.date(), version="sku-default/1")
        self.assertEqual([(rule.level, rule.quantity, rule.version) for rule in loaded],
                         [("low_replenish", "20.0000", "sku-default/1")],
                         f"冻结时钟那一次必须取到已配置策略（库里今天是 {db_today}）")

    def test_configured_and_inline_thresholds_are_mutually_exclusive(self):
        self._shared_three_shops()
        self._verified()
        self._threshold(sku_key="SKU1", level="low_replenish", quantity="150")
        with self.assertRaises(ValueError):
            # 两者同给就是两个分母：入参契约先拒，图上也不给第二次机会
            _request(threshold_policy_ref="sku-default/1",
                     thresholds=[_threshold("low_replenish", self._sku("SKU1"), "5")])

    def test_equal_to_threshold_is_low_and_just_above_is_normal(self):
        self._shop("1")
        self._trade("1")
        self._pool("a", shops=("1",))
        self._physical("a", warehouse="wh-a", sku="SKU1", quantity="20")
        self._channel("1", sku="SKU1", quantity="20")
        self._verified()
        payload = _payload_for(self._run())
        self.assertEqual(_status_for(payload, level="physical_total"), "low",
                         "spec §5.5 判定 quantity <= threshold：等于也算预警")
        above = _payload_for(self._run(thresholds=[
            _threshold("low_replenish", self._sku("SKU1"), "19"),
            _threshold("low_quota", self._sku("SKU1"), "19",
                      shop_ref=_shop_ref(self.tag + "1"))]))
        self.assertEqual(_status_for(above, level="physical_total"), "normal")

    def test_missing_threshold_is_unconfigured_yet_still_reports_the_quantity(self):
        self._shop("1")
        self._trade("1")
        self._pool("a", shops=("1",))
        self._physical("a", warehouse="wh-a", sku="SKU1", quantity="100")
        self._channel("1", sku="SKU1", quantity="100")
        self._verified()
        payload = _payload_for(self._run(thresholds=None))
        self.assertEqual({row["inventory_status"] for row in _rows(payload)},
                         {STATUS_UNCONFIGURED})
        physical = [row for row in _rows(payload) if row["level"] == "physical_total"][0]
        self.assertEqual(physical["quantity"], "100")
        self.assertIsNone(physical["threshold"])
        self.assertFalse(payload["inventory"]["all_safe"])
        self.assertIn("阈值", _limitation_text(payload))

    def test_stale_snapshot_is_reported_stale_and_never_as_safe(self):
        self._shared_three_shops()
        self._verified(max_age_seconds=60)
        payload = _payload_for(self._run())
        self.assertEqual({row["inventory_status"] for row in _rows(payload)}, {"stale"})
        self.assertFalse(payload["inventory"]["all_safe"])
        self.assertIn("时效", _limitation_text(payload))

    def test_unauthorized_pool_is_excluded_with_a_reason_and_never_read(self):
        self._shared_three_shops()
        self._pool("b", shops=("2",))
        self._physical("b", warehouse="wh-b", sku="SKU1", quantity="500",
                      snapshot_id="ph-b")
        self._verified()
        recorder = _RecordingConn(self.conn)
        result = self._run(conn=recorder, pools=self._authorized_pools("a"))
        payload = _payload_for(result)
        physical = [row for row in _rows(payload) if row["level"] == "physical_total"][0]
        self.assertEqual(physical["quantity"], "100")
        self.assertNotIn(pool_handle(DEFAULT_NAMESPACE, self.pools["b"]),
                         [row.get("pool_ref") for row in _rows(payload)])
        # 池排除说在 `inventory.excluded_pools` 里：`excluded_scope` 的原因词表是店铺
        # 口径的，把池塞进去等于让"为什么少了一格"找不到自己的码。
        excluded = {item["pool_ref"]: item["reason"]
                    for item in payload["inventory"]["excluded_pools"]}
        self.assertEqual(excluded[pool_handle(DEFAULT_NAMESPACE, self.pools["b"])],
                         "pool_not_authorized")
        self.assertIn(TEXT_POOL_UNAUTHORIZED, _limitation_text(payload))

    def test_pool_authorization_without_shop_authorization_reads_nothing(self):
        """两个授权集是两件事：只给池不给店，也不能凭空长出可看的范围。"""
        self._shared_three_shops()
        self._verified()
        result = self._run(allowed=frozenset())
        self.assertEqual(result.status.value, "missing_data")
        self.assertEqual(result.artifacts, [])

    def test_explicit_unauthorized_shop_ref_is_forbidden(self):
        self._shared_three_shops()
        outsider = self._shop("9")
        self._verified()
        result = self._run(scope={"mode": "selected",
                                 "shop_refs": [ref_for_key("shop", outsider)]},
                          allowed=frozenset())
        self.assertEqual(result.status.value, "failed")
        self.assertEqual(result.model_payload, {"status": "failed"},
                         "越权只得到一个否：不解释，也不证实那家店存在")

    # -- 5.4 扫描完整性与候选 ----------------------------------------------

    def test_incomplete_scan_is_reported_and_blocks_a_clean_verdict(self):
        self._shop("1")
        self._trade("1")
        self._pool("a", shops=("1",))
        self._physical("a", warehouse="wh-a", sku="SKU1", quantity="100",
                      scan_complete=False)
        self._channel("1", sku="SKU1", quantity="100", scan_complete=False)
        self._verified()
        payload = _payload_for(self._run())
        self.assertTrue(any("分页" in text or "扫描" in text
                           for text in payload["limitations"]), payload["limitations"])
        self.assertFalse(payload["inventory"]["all_safe"])

    def test_many_skus_scan_completely_then_display_is_truncated(self):
        """展示上限不改变扫描事实：scanned == expected 时才是"真的看完了"。"""
        self._shop("1")
        self._pool("a", shops=("1",))
        for index in range(25):
            self._sku(f"SKU{index}")
        self._trade("1", sku="SKU0")
        for index in range(25):
            self._physical("a", warehouse="wh-a", sku=f"SKU{index}",
                          quantity=str(index), snapshot_id=f"ph-{index}")
        self._channel("1", sku="SKU0", quantity="0")
        self._verified()
        result = self._run(products="all", sku_refs=[],
                          thresholds=[_threshold("low_replenish", self._sku("SKU1"),
                                                 "20")])
        self.assertEqual(result.status.value, "partial", str(result.model_payload))
        payload = _payload_for(result)
        # 25 个 SKU × 两个口径 = 50 格：每一格都必须是独立的一行，不能被折成 25 行
        # 后就"看起来扫完了"。
        summary = payload["inventory"]
        self.assertEqual(summary["scanned_items"], 50, str(summary))
        self.assertEqual(summary["expected_items"], 50)
        self.assertTrue(summary["truncated"], "50 格只能展示 20 格：截断必须自己承认")
        self.assertFalse(summary["all_safe"])

    def test_quota_candidate_is_reported_without_a_purchase_candidate(self):
        self._shop("1")
        self._shop("2")
        for key in ("1", "2"):
            self._trade(key)
        self._pool("a", connection="shared", shops=("1", "2"))
        self._physical("a", warehouse="wh-a", sku="SKU1", quantity="100")
        self._channel("1", sku="SKU1", quantity="0")
        self._channel("2", sku="SKU1", quantity="100")
        self._verified()
        payload = _payload_for(self._run(thresholds=[
            _threshold("low_replenish", self._sku("SKU1"), "20"),
            _threshold("low_quota", self._sku("SKU1"), "20",
                      shop_ref=_shop_ref(self.tag + "1"))]))
        actions = {row.get("action") for row in _rows(payload) if row.get("action")}
        self.assertEqual(actions, {"quota_adjust"},
                         "实物充足、渠道缺货：候选是配额调整，不是采购")
        quota = [row for row in _rows(payload) if row.get("action") == "quota_adjust"][0]
        self.assertEqual(quota["shop_ref"], _shop_ref(self.tag + "1"))
        self.assertIsNotNone(quota["snapshot_at"])

    def test_both_candidates_can_be_issued_for_one_sku(self):
        self._shop("1")
        self._trade("1")
        self._pool("a", shops=("1",))
        self._physical("a", warehouse="wh-a", sku="SKU1", quantity="5")
        self._channel("1", sku="SKU1", quantity="0")
        self._verified()
        payload = _payload_for(self._run())
        actions = {row.get("action") for row in _rows(payload) if row.get("action")}
        self.assertEqual(actions, {"replenish", "quota_adjust"})

    # -- 5.5 发布与失败路径 ------------------------------------------------

    def test_artifact_persistence_failure_is_failed_not_partial(self):
        self._shared_three_shops()
        self._verified()
        result = self._run(store=_FailingStore(forbidden_values=set(self.shops.values())))
        self.assertEqual(result.status.value, "failed")
        self.assertEqual(result.artifacts, [])
        self.assertEqual(result.model_payload, {"status": "failed"},
                         "被拒数字不得回流")

    def test_run_walks_the_fixed_chain_under_its_own_domain(self):
        self._shared_three_shops()
        self._verified()
        store = MemoryQueryRunStore(forbidden_values=set(self.shops.values()))
        result = self._run(store=store)
        run = store.runs[result.run_id]
        self.assertEqual(run["domain"], "inventory_watch")
        nodes = [event["node"] for event in store.events[result.run_id]]
        self.assertEqual(set(nodes) & set(INVENTORY_NODES), set(INVENTORY_NODES),
                         "十二节点一条链，不另开第二条路径")
        self.assertEqual(nodes[0], "resolve_full_catalog_and_scope")
        self.assertEqual(nodes[-1], "finalize")
        self.assertNotIn("pool-" + self.tag, str(run["state"]),
                         "真实池号不得进持久化状态")

    def test_provenance_records_rule_and_graph_versions(self):
        self._shared_three_shops()
        self._verified()
        store = MemoryQueryRunStore(forbidden_values=set(self.shops.values()))
        result = self._run(store=store)
        provenance = store.runs[result.run_id]["provenance"]
        self.assertEqual(provenance.graph_version, INVENTORY_GRAPH_VERSION)
        self.assertEqual(provenance.template_id, "inventory_watch")
        self.assertEqual(provenance.policy_version, UNIT_CONVERSION_REGISTRY_VERSION)
        self.assertEqual(provenance.schema_version, "019")
        self.assertTrue(provenance.source_batches, "两批快照必须进血缘")

    def test_model_payload_carries_refs_only_and_artifact_carries_names(self):
        self._shared_three_shops()
        self._verified()
        result = self._run()
        raw = str(result.model_payload)
        for leak in (self.shops["1"], self.pools["a"], f"{self.tag}-wh-a",
                     self._sku("SKU1"), "PS-L1-SKU1", f"probe-t10"):
            self.assertNotIn(leak, raw, f"模型载荷不得出现真实标识 {leak}")
        payload = _payload_for(result)
        names = {entity["ref"]: entity.get("display_name")
                 for entity in payload["entities"]}
        self.assertEqual(names[_shop_ref(self.tag + "1")], "库存测试店1")
        self.assertNotIn("entities", result.model_payload)
        validate_model_payload(result.model_payload, ALERT_TYPE)

    def test_deadline_exhaustion_reports_failure_not_an_empty_pass(self):
        self._shared_three_shops()
        self._verified()
        context = self._context()
        context.deadline = time.monotonic() - 1
        result = run_inventory_graph(request=_request(
            sku_refs=[_sku_ref(self._sku("SKU1"))]), context=context,
            tool_call_id="call-x").domain_result
        self.assertEqual(result.status.value, "failed")
        self.assertEqual(result.model_payload["status"], "unavailable")

    def test_unresolvable_sku_is_missing_data_not_zero_stock(self):
        self._shop("1")
        self._trade("1")
        self._verified()
        result = self._run(sku_refs=[_sku_ref(f"{self.tag}NOT-THERE")])
        self.assertEqual(result.status.value, "missing_data")
        self.assertEqual(result.artifacts, [])
        self.assertNotIn("low", str(result.model_payload.get("inventory", {})))

    def test_the_snapshot_reads_go_through_reporting_views_as_the_app_role(self):
        """真实部署以 bi_app 身份连接：新查询不能只在管理员 DSN 下跑得通。"""
        self._shared_three_shops()
        with self.conn.transaction():
            self.conn.execute("SET LOCAL ROLE bi_app")
            for view in ("v_physical_stock_snapshots", "v_physical_stock_items",
                         "v_channel_stock_snapshots", "v_channel_stock_items"):
                rows = self.conn.execute(
                    f"SELECT count(*) FROM reporting.{view}").fetchone()
                # `>= 0` 对一个谓词写坏、返回零行的视图也成立：那时这条用例就是在验
                # "没报错"而不是在验"读得到"。
                self.assertGreater(rows[0], 0, f"{view} 读不到任何行")
            # 本轮没配阈值：这一张只验可读，不假装非空。
            self.assertEqual(self.conn.execute(
                "SELECT count(*) FROM reporting.v_inventory_threshold_policies"
            ).fetchone()[0], 0)
            # 取证凭据不进视图。
            with self.assertRaises(psycopg.errors.UndefinedColumn):
                with self.conn.transaction():
                    self.conn.execute(
                        "SELECT evidence FROM reporting.v_physical_stock_snapshots"
                    ).fetchone()
            for table in ("bi.physical_stock_snapshots", "bi.inventory_pools",
                          "bi.inventory_threshold_policies"):
                with self.assertRaises(psycopg.errors.InsufficientPrivilege,
                                       msg=table):
                    with self.conn.transaction():
                        self.conn.execute(f"SELECT count(*) FROM {table}").fetchone()

    def test_two_channel_scans_of_one_shop_report_the_current_one_only(self):
        """同一店家两次抓取（09:00 与 11:40）：当前可售只能是后一次的那个数。

        这是 Review 抓到的 P0：早期版本的渠道读取只按时间上限过滤，把两次抓取的显示数
        加成 140 件，还报 `normal`、`batch_count 1`——一个从没成立过的数，被拿去决定
        要不要给这家店加配额。
        """
        self._shop("1")
        self._trade("1")
        self._pool("a", shops=("1",))
        self._physical("a", warehouse="wh-a", sku="SKU1", quantity="1000")
        self._channel("1", sku="SKU1", quantity="100", snapshot_id="ch-old",
                      captured_at=datetime(2026, 9, 14, 9, tzinfo=BEIJING))
        self._channel("1", sku="SKU1", quantity="10", snapshot_id="ch-new",
                      captured_at=FRESH)
        self._verified()
        payload = _payload_for(self._run())
        cell = _row_for(payload, level="shop_sellable", shop=_shop_ref(self.tag + "1"))
        self.assertEqual(cell["channel_quantity"], "10", str(cell))
        # 110 = 把两次抓取加成一个"当前可售"；那会把 low 翻成 normal，正好把一个
        # 该加配额的店判成不用管。这一句必须按**值**钉：拿整份序列化文本扫 "110"
        # 会撞进 ent- / pl- / wh- 随机句柄里的同一串数字（既可能假红，也可能假绿）。
        self.assertEqual(
            sorted((row["level"], row["quantity"], row["channel_quantity"])
                   for row in _rows(payload)),
            [("physical_total", "1000", None), ("shop_sellable", None, "10")],
            "旧一次抓取的 100 既不被加进来，也不顶替当前值：两档各发自己那一个数")
        self.assertEqual(cell["inventory_status"], "low",
                         "当前可售 10 对阈值 20 必须是 low：混进旧一次就成了 normal")

    def test_two_current_scans_of_one_shop_are_an_anomaly_not_a_sum(self):
        """同一家店在同一时刻并列两批抓取：不挑一批，也不把两批相加。"""
        self._shop("1")
        self._trade("1")
        self._pool("a", shops=("1",))
        self._physical("a", warehouse="wh-a", sku="SKU1", quantity="1000")
        self._channel("1", sku="SKU1", quantity="100", listing="L1",
                      snapshot_id="ch-a", captured_at=FRESH)
        self._channel("1", sku="SKU1", quantity="40", listing="L1",
                      snapshot_id="ch-b", captured_at=FRESH)
        self._verified()
        payload = _payload_for(self._run())
        cell = _row_for(payload, level="shop_sellable", shop=_shop_ref(self.tag + "1"))
        self.assertEqual(cell["inventory_status"], "data_anomaly", str(cell))
        self.assertIsNone(cell["channel_quantity"])
        self.assertIn("同一库存身份出现两个不同数量", _limitation_text(payload))

    def test_a_sku_that_only_exists_in_an_unauthorized_pool_stays_unresolved(self):
        """只存在于未授权池里的 SKU 不能被解析成本轮的检查对象。

        全集那两条 JOIN 若只按 (namespace, snapshot_id) 连接，同一扫描号在两个池上都
        出现过时，未授权池的 SKU 会顺着另一池的快照行挤进全集：一个本该报"身份没确定"
        的引用就被当成已解析，随后那句"这个 SKU 没记录"就成了一次隐形的越权探测。
        """
        self._shop("1")
        self._trade("1")
        self._pool("a", shops=("1",))
        self._pool("b", shops=("1",))
        self._physical("b", warehouse="wh-b", sku="SECRET", quantity="10",
                       snapshot_id="ph-secret")
        self._verified()
        secret = _sku_ref(self._sku("SECRET"))
        result = self._run(sku_refs=[secret],
                           pools=self._authorized_pools("a"))
        self.assertEqual(result.status.value, "missing_data", str(result.model_payload))
        self.assertEqual(result.artifacts, [])
        self.assertIn(TEXT_SKU_UNRESOLVED, _limitation_text(result.model_payload))
        self.assertNotIn(self.pools["b"], str(result.model_payload))

    def test_unauthorized_pool_is_excluded_and_never_queried(self):
        """未获准池：既不进步调与明细，也不该被查询碰过。"""
        self._shared_three_shops()
        self._pool("b", shops=("2",))
        self._physical("b", warehouse="wh-b", sku="SKU1", quantity="500",
                       snapshot_id="ph-b")
        self._verified()
        recorder = _RecordingConn(self.conn)
        result = self._run(conn=recorder, pools=self._authorized_pools("a"))
        payload = _payload_for(result)
        self.assertEqual(_row_for(payload, level="physical_total")["quantity"], "100")
        # 正向对照：本用例真的在读库，否则"没查到那张表"就只是"什么都没查"。
        self.assertTrue(recorder.matching("v_physical_stock_items"),
                        "对照失败：这次执行根本没读过快照，下面的负断言就是空的")
        self.assertEqual(recorder.matching("v_physical_stock_items", self.pools["b"]),
                         [], "未获准池的明细被读进过本轮：越权只挡在输出层不够")
        self.assertEqual(recorder.matching("FROM reporting.v_physical_stock_snapshots",
                                           self.pools["b"]), [])

    def test_configured_threshold_policy_is_read_without_a_sku_filter(self):
        """全商品那一途按范围取策略：先筛 SKU 会把"配过了"报成"未配置"。"""
        self._shop("1")
        self._pool("a", shops=("1",))
        self._trade("1", sku="SKU0")
        self._physical("a", warehouse="wh-a", sku="SKU1", quantity="10",
                       snapshot_id="ph-x")
        self._threshold(sku_key="SKU1", level="low_replenish", quantity="20")
        self._verified()
        payload = _payload_for(self._run(products="all", sku_refs=[], thresholds=None,
                                         threshold_policy_ref="sku-default/1",
                                         levels=["physical_total"]))
        self.assertEqual(payload["inventory"]["threshold_source"], "configured")
        row = _rows(payload)[0]
        self.assertEqual(row["inventory_status"], "low", str(row))

    def test_import_contract_refuses_undeclared_provenance(self):
        """导入契约：来源码、证据、扫描完整性声明与数量形式缺一即拒。

        每条都断言 `ValueError`（入口拦的）而不是 `Exception`：写成 `Exception` 时，
        把入口守卫全删掉、让数据库 CHECK 报错，这条用例照样绿——那不是在验契约，
        是在验"总会出点错"。同样也要确认真的什么都没落库：报完错留一行半成品，
        比不报错更糟。
        """
        self._shop("1")
        self._pool("a", shops=("1",))
        base = dict(pool_id=None, warehouse_id="wh-a", namespace=DEFAULT_NAMESPACE,
                    source="erp", evidence="probe-t10", captured_at=FRESH,
                    scan_complete=True, scan_evidence="pages", batch_id="b1",
                    platform=None)
        for label, override in (
                ("来源码白名单", {"source": "erp_suggested_price"}),
                ("证据非空", {"evidence": "  "}),
                ("声明完整枚举必须带分页凭据", {"scan_evidence": None}),
                ("未声明完整就不许带凭据",
                 {"scan_complete": False, "scan_evidence": "pages"}),
                ("批次必填", {"batch_id": ""}),
                ("抓取时点必填", {"captured_at": None}),
                ("池必填", {"pool_id": "  "}),
                ("仓库必填", {"warehouse_id": "  "}),
        ):
            with self.subTest(rule=label):
                arguments = dict(base)
                arguments.update(override)
                arguments["pool_id"] = arguments["pool_id"] or self.pools["a"]
                arguments["snapshot_id"] = "snap-" + str(len(label))
                with self.assertRaises(ValueError):
                    repository.insert_physical_snapshot(self.conn, **arguments)
                self.assertEqual(self.conn.execute(
                    "SELECT count(*) FROM bi.physical_stock_snapshots "
                    "WHERE snapshot_id = %s",
                    (arguments["snapshot_id"],)).fetchone()[0], 0)

    def test_storage_layer_refuses_what_the_python_helper_cannot_see(self):
        """直写 SQL 过一遍 019 的 CHECK：证明它们是存储层约束。"""
        self._shop("1")
        pool = self._pool("a", shops=("1",))
        sid = "snap-check"
        self.conn.execute(
            "INSERT INTO bi.physical_stock_snapshots(snapshot_id, namespace, pool_id, "
            "warehouse_id, source, evidence, captured_at, scan_complete, scan_evidence, "
            "batch_id) VALUES (%s, %s, %s, %s, 'erp', 'probe', now(), "
            "false, null, 'b1')", (sid, DEFAULT_NAMESPACE, pool, "wh-a"))
        # 负可用量**必须能写进来**：丢掉它就等于把一次坏导入演成一个偏大的总量，
        # 而那正是会被拿去说"库存还够"的那类错。它由判定层报成 data_anomaly。
        with self.conn.transaction():
            self.conn.execute(
                "INSERT INTO bi.physical_stock_items(snapshot_id, namespace, pool_id, "
                "warehouse_id, erp_sku_id, available_quantity, unit, batch_id, "
                "captured_at) VALUES (%s, %s, %s, 'wh-a', 'SKU-NEG', -1, 'piece', 'b1', "
                "now())", (sid, DEFAULT_NAMESPACE, pool))
        self.assertEqual(self.conn.execute(
            "SELECT count(*) FROM bi.physical_stock_items WHERE erp_sku_id = "
            "'SKU-NEG'").fetchone()[0], 1, "负库存被静默丢弃了")
        for label, (quantity, unit, batch) in (
                ("整数单位上的小数", ("1.5", "piece", "b1")),
                ("批次必须成对", ("10", "piece", "")),
                ("单位白名单", ("10", "件", "b1"))):
            with self.subTest(rule=label):
                with self.assertRaises(psycopg.errors.CheckViolation):
                    with self.conn.transaction():
                        self.conn.execute(
                            "INSERT INTO bi.physical_stock_items(snapshot_id, namespace, "
                            "pool_id, warehouse_id, erp_sku_id, available_quantity, unit, "
                            "batch_id, captured_at) VALUES (%s, %s, %s, 'wh-a', 'SKU', "
                            "%s, %s, %s, now())",
                            (sid, DEFAULT_NAMESPACE, pool, quantity, unit, batch))
        with self.assertRaises(psycopg.errors.ForeignKeyViolation):
            with self.conn.transaction():
                self.conn.execute(
                    "INSERT INTO bi.physical_stock_items(snapshot_id, namespace, pool_id, "
                    "warehouse_id, erp_sku_id, available_quantity, unit, batch_id, "
                    "captured_at) VALUES ('no-such-snapshot', %s, %s, 'wh-a', 'SKU', 1, "
                    "'piece', 'b1', now())", (DEFAULT_NAMESPACE, pool))


# ---------------------------------------------------------------------------
# 6. 主 Agent 接线
# ---------------------------------------------------------------------------


class InventoryAgentWiringTests(unittest.TestCase):
    """只钉主层接线：工具已公告、上下文由服务端注入、一次调用跑一次图。"""

    def test_tool_is_announced_as_the_sixth_business_tool(self):
        import bi_agent.agent as agent

        names = [item["function"]["name"] for item in agent._tool_schemas()]
        self.assertEqual(names, ["query_business", "analyze_product_performance",
                                 "compare_performance", "audit_listing_prices",
                                 "inspect_inventory", "evaluate_promotion"])

    def test_prompt_says_the_two_levels_and_the_pool_authorization(self):
        from bi_agent.agent import _SYSTEM_PROMPT

        self.assertIn("六个工具", _SYSTEM_PROMPT)
        self.assertIn("inspect_inventory", _SYSTEM_PROMPT)
        self.assertIn("实物", _SYSTEM_PROMPT)
        self.assertIn("渠道可售", _SYSTEM_PROMPT)
        self.assertIn("unsupported", _SYSTEM_PROMPT)
        self.assertIn("unconfigured", _SYSTEM_PROMPT)

    def _answer(self, captured: list, *, payload, status, forbidden_pools=()):
        from bi_agent.agent import SessionState, answer
        from bi_agent.inventory.graph import InventoryExecution
        from bi_agent.llm import ToolCall
        from bi_agent.runtime.models import (
            ArtifactRef, DomainArtifact, DomainResult)

        def fake_graph(**kwargs):
            captured.append(kwargs)
            return InventoryExecution(domain_result=DomainResult(
                run_id=uuid4(), status=status, model_payload=payload,
                artifacts=[DomainArtifact(ref=ArtifactRef(id=uuid4(), type=ALERT_TYPE),
                                          public_payload=payload)]))

        model = mock.Mock()
        model.complete.side_effect = [
            _reply(calls=[ToolCall(
                id="call_i", name="inspect_inventory",
                arguments={"products": "selected",
                           "sku_refs": [_sku_ref("SKU-A")],
                           "thresholds": [_threshold("low_replenish", "SKU-A", "20")]})]),
            _reply(text="实物与渠道可售两级预警已列出")]
        with mock.patch("bi_agent.inventory.tool.run_inventory_graph",
                        side_effect=fake_graph) as graph:
            turn = answer("直钉枪库存还够吗", SessionState(subject="u1"), model=model,
                          conn=_ShopListConn(), allowed_shop_ids=frozenset({"S1"}),
                          now=NOW,
                          run_store=MemoryQueryRunStore(forbidden_values={"S1"}))
        return turn, graph

    def test_call_is_routed_once_with_server_side_context(self):
        from bi_agent.runtime.models import DomainStatus

        calls: list[dict] = []
        turn, graph = self._answer(calls, payload=_alerts_payload_skeleton(),
                                  status=DomainStatus.PARTIAL)
        self.assertEqual(graph.call_count, 1, "一次工具调用只能跑一次图")
        context = calls[0]["context"]
        self.assertEqual(context.allowed_shop_ids, frozenset({"S1"}),
                         "真实店铺授权集只能由服务端注入")
        self.assertEqual(context.allowed_inventory_pool_ids, frozenset(),
                         "库存池授权也必须是服务端的：本轮没有任何配置就是空")
        self.assertEqual(calls[0]["tool_call_id"], "call_i")
        self.assertEqual([artifact["artifact_type"] for artifact in turn.artifacts],
                         [ALERT_TYPE])
        self.assertEqual(turn.text, "实物与渠道可售两级预警已列出")

    def test_target_thresholds_are_not_written_back_into_session_filters(self):
        """本轮阈值不能回写会话：那下一轮的 "unconfigured" 就有了隐式继承通道。"""
        from bi_agent.runtime.models import DomainStatus

        calls: list[dict] = []
        turn, _graph = self._answer(calls, payload=_alerts_payload_skeleton(),
                                   status=DomainStatus.PARTIAL)
        self.assertNotIn("thresholds", turn.state.filters)
        self.assertNotIn("quantity", str(turn.state.filters))

    def test_unsupported_source_is_transcribed_without_a_safe_claim(self):
        from bi_agent.runtime.models import DomainStatus

        payload = {**_alerts_payload_skeleton(), "status": "missing_data",
                   "limitations": [TEXT_SOURCE_UNVERIFIED],
                   "inventory": {**_alerts_payload_skeleton()["inventory"],
                                 "evaluated_items": 0, "all_safe": False,
                                 "counts": {"unsupported": 1}}}
        calls: list[dict] = []
        turn, _graph = self._answer(calls, payload=payload,
                                   status=DomainStatus.MISSING_DATA)
        self.assertNotIn("都安全", turn.text)
        self.assertIsNone(turn.error_code, "缺来源是数据状态，不是故障码")
        self.assertEqual([artifact["artifact_type"] for artifact in turn.artifacts],
                         [ALERT_TYPE])

    def test_audit_card_survives_a_missing_summary_sentence(self):
        """模型没组织出正文时，已发布的预警卡片不能被说成"本轮没拿到结果"。"""
        from bi_agent.agent import SessionState, answer
        from bi_agent.inventory.graph import InventoryExecution
        from bi_agent.llm import ToolCall
        from bi_agent.runtime.models import (
            ArtifactRef, DomainArtifact, DomainResult, DomainStatus)

        payload = _alerts_payload_skeleton()
        model = mock.Mock()
        model.complete.side_effect = [
            _reply(calls=[ToolCall(
                id="call_i", name="inspect_inventory",
                arguments={"products": "selected", "sku_refs": [_sku_ref("SKU-A")],
                           "thresholds": [_threshold("low_replenish", "SKU-A", "20")]})]),
            _reply(text="")]
        with mock.patch("bi_agent.inventory.tool.run_inventory_graph",
                        side_effect=lambda **kwargs: InventoryExecution(
                            domain_result=DomainResult(
                                run_id=uuid4(), status=DomainStatus.PARTIAL,
                                model_payload=payload,
                                artifacts=[DomainArtifact(
                                    ref=ArtifactRef(id=uuid4(), type=ALERT_TYPE),
                                    public_payload=payload)]))):
            turn = answer("直钉枪库存还够吗", SessionState(subject="u1"), model=model,
                          conn=_ShopListConn(), allowed_shop_ids=frozenset({"S1"}),
                          now=NOW, run_store=MemoryQueryRunStore(forbidden_values={"S1"}))
        self.assertEqual(len(turn.artifacts), 1)
        self.assertIn("见下方数据", turn.text)
        self.assertNotIn("没能给出回答", turn.text)


class _ShopListConn:
    """主层接线用例用的连接替身：只回答 `_fetch_shops`，其他 SQL 一律报错。"""

    def __init__(self, shops: Sequence[tuple[str, str]] = (("S1", "钉枪工厂店"),)) -> None:
        self.shops = list(shops)
        self.queries: list[str] = []

    def execute(self, sql, params=None):  # noqa: ANN001
        text = " ".join(str(sql).split())
        self.queries.append(text)
        if "FROM reporting.v_shops" not in text:
            raise AssertionError(f"主层不应在本用以外再读一次库：{text[:120]}")
        rows = list(self.shops)

        class _Result:
            def fetchall(inner):  # noqa: ANN001
                return rows

            def fetchone(inner):  # noqa: ANN001
                return rows[0] if rows else None
        return _Result()


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
