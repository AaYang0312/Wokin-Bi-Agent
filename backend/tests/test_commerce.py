"""Task 7：现有表项经营指标与商品运营图。

计划 Task 7 点名的用例一次到位：指定商品、多店、七日缺口、成本 null、混合赠品 /
套件、退款跨期、JOIN 放大；另加本轮新增的三条边界：授权（越权引用不静默剔除）、
能力 / 口径 / 覆盖三类 fail closed 归因、Artifact 保存失败不许冒充成功。

真实测试库跑法与既有约定一致：管理员连接 + 外层事务回滚，只写合成店铺。
无 DSN 时显式 skip——skip 不是通过证明。
"""

from __future__ import annotations

import json
import os
import time
import unittest
from datetime import date, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

import psycopg

from bi_agent.catalog import ref_for_key
from bi_agent.commerce.graph import (
    CommerceNode, CommerceState, InvalidCommerceTransition, UnsupportedReportKind,
    transition_state)
from bi_agent.commerce.metrics import (
    COMMERCE_METRIC_DEFINITIONS, PRODUCT_ROWS, combine_reference_metrics,
    compute_reference_metrics, project)
from bi_agent.commerce.models import DomainContext, ProductPerformanceRequest
from bi_agent.commerce.tool import analyze_product_performance
from bi_agent.data_quality import QUALITY_RULE
from bi_agent.metrics import MAX_SPAN_DAYS
from bi_agent.runtime.artifacts import TERMINATION_REASONS
from bi_agent.runtime.domain_registry import COMMERCE_NODES
from bi_agent.runtime.memory import MemoryQueryRunStore

from .dbfixtures import connect_test_db

BEIJING = ZoneInfo("Asia/Shanghai")
NOW = datetime(2026, 9, 11, 12, tzinfo=BEIJING)
ALL_CAPABILITIES = ("paid_amount", "paid_orders", "erp_documents", "aov", "quantity",
                    "product_paid_amount", "refund_amount", "cash_difference",
                    "cohort_refund_rate")
WINDOW_START = datetime(2026, 8, 28, tzinfo=BEIJING)
WINDOW_END = datetime(2026, 9, 12, tzinfo=BEIJING)


def _request(**overrides: object) -> ProductPerformanceRequest:
    base: dict[str, object] = {
        "product": {"text": "直钉枪"},
        "scope": {"mode": "all_authorized"},
        "start": "2026-09-01", "end": "2026-09-08",
        "metrics": ["sold_quantity", "sales_amount", "weighted_avg_paid_price"],
        "sales_basis": "erp_effective_parent", "profit_basis": "none",
        "trend_days": 7, "comparison": "none",
    }
    base.update(overrides)
    return ProductPerformanceRequest.model_validate(base)


class _ArtifactStoreFails(MemoryQueryRunStore):
    """只让 Artifact 写入失败：其它运行写入照旧，才能单独看这一条降级路径。"""

    def save_artifact(self, run_id, artifact):  # noqa: ANN001
        raise RuntimeError("simulated artifact failure")


def _payload_for(result, artifact_type: str) -> dict:
    """按 Artifact 类型取公开载荷：三份数据集分开发布，断言必须指名要哪一份。"""
    matches = [artifact.public_payload for artifact in result.artifacts
               if artifact.ref.type == artifact_type]
    assert len(matches) == 1, ([artifact.ref.type for artifact in result.artifacts],
                              artifact_type)
    return matches[0]


@unittest.skipUnless(os.getenv("BI_TEST_ADMIN_DSN"), "未配置独立测试数据库")
class CommerceGraphTests(unittest.TestCase):
    """真实库上的经营图：合成一到三家店、一个商品、若干成交行。"""

    def setUp(self):
        self.conn = connect_test_db(self)
        self.tag = uuid4().hex[:5].upper()
        self.shops: dict[str, str] = {}

    # -- 夹具 --------------------------------------------------------------

    def _shop(self, key: str, platform: str = "fxg",
              capabilities=ALL_CAPABILITIES) -> str:
        shop_id = f"S{self.tag}{key}"
        self.conn.execute(
            "INSERT INTO bi.shops(shop_id, platform, display_name, capabilities) "
            "VALUES (%s, %s, %s, %s) ON CONFLICT (shop_id) DO UPDATE SET "
            "platform = EXCLUDED.platform",
            (shop_id, platform, f"经营测试店{key}", list(capabilities)))
        self.shops[key] = shop_id
        return shop_id

    def _cover(self, shop_key: str, start: datetime, end: datetime,
               source: str = "erp.trade.list.query") -> None:
        self.conn.execute(
            "INSERT INTO bi.sync_state(source, entity, shop_id, watermark, covered, "
            "data_as_of, quality_status, quality_rule) "
            "VALUES (%s, 'orders', %s, %s, tstzmultirange(tstzrange(%s, %s, '[)')), %s, "
            "'passed', %s) ON CONFLICT (source, entity, shop_id) DO UPDATE SET "
            "covered = bi.sync_state.covered + EXCLUDED.covered, "
            "data_as_of = greatest(coalesce(bi.sync_state.data_as_of, "
            "  '-infinity'), EXCLUDED.data_as_of), "
            "quality_status = EXCLUDED.quality_status, "
            "quality_rule = EXCLUDED.quality_rule",
            (source, self.shops[shop_key], end, start, end, end, QUALITY_RULE))
        # 一次真实取数同时留下两条证据：覆盖区间（sync_state.covered）与批次本身
        # （sync_batches）。血缘里的 source_batches 只承认后者，所以这里也必须写：
        # 只填 covered 会造出一张“数据库里从未发生过的同步”。窗口名带日期段，
        # 同一家店多次 _cover 就是多个批次，不互相顶掉。
        self.conn.execute(
            "INSERT INTO bi.sync_batches(source, entity, shop_id, batch_id, "
            "business_window, window_kind, mode, row_count, business_end) "
            "VALUES (%s, 'orders', %s, %s, tstzrange(%s, %s, '[)'), 'business', "
            "'backfill', 1, %s) ON CONFLICT (source, entity, shop_id, batch_id) "
            "DO UPDATE SET business_window = EXCLUDED.business_window",
            (source, self.shops[shop_key],
             f"t7-{source}-{start:%m%d}-{end:%m%d}", start, end, end))

    def _archive(self, product: str, title: str) -> None:
        self.conn.execute(
            "INSERT INTO bi.products(product_id, title, source_modified_at, synced_at) "
            "VALUES (%s, %s, now(), now()) ON CONFLICT (product_id) "
            "DO UPDATE SET title = EXCLUDED.title", (product, title))

    def _sale(self, shop_key: str, erp_id: str, *, product: str, day: date,
              quantity: str, amount: str, cost: str | None = None,
              line_kind: str = "sale", verified: bool = True,
              document_cost: str | None = None,
              document_profit: str | None = None,
              sku: str | None = None, source: str = "erp.trade.list.query",
              commercial_ids: tuple[str, ...] = ()) -> None:
        """一张 ERP 单据 + 一行商品父行：单据头与行分别带自己的成本口径。"""
        shop_id = self.shops[shop_key]
        paid_at = datetime(day.year, day.month, day.day, 12, tzinfo=BEIJING)
        commercials = list(commercial_ids) or [f"C{erp_id}"]
        self.conn.execute(
            "INSERT INTO bi.orders(shop_id, erp_id, commercial_ids, source, "
            "source_updated_at, paid_at, raw_cost, raw_gross_profit, active, "
            "batch_id) VALUES (%s, %s, %s, %s, now(), %s, %s, %s, true, 't7-seed') "
            "ON CONFLICT (shop_id, erp_id) DO NOTHING",
            (shop_id, erp_id, commercials, source, paid_at, document_cost,
             document_profit))
        self.conn.execute(
            "INSERT INTO bi.order_items(shop_id, erp_id, line_id, commercial_id, "
            "product_id, sku_id, paid_at, quantity, raw_unit_cost, "
            "allocated_paid_amount, allocation_verified, line_kind, active, "
            "product_name_snapshot) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, true, %s) "
            "ON CONFLICT (shop_id, erp_id, line_id) DO NOTHING",
            (shop_id, erp_id, f"{erp_id}-L1", commercials[0], product,
             sku or f"{product}-sku", paid_at, quantity, cost, amount, verified,
             line_kind, product.title if False else f"档案-{product}"))

    def _context(self, *, allowed: frozenset[str] | None = None,
                 store: MemoryQueryRunStore | None = None) -> DomainContext:
        allowed = allowed or frozenset(self.shops.values())
        refs = {shop: ref_for_key("shop", shop) for shop in allowed}
        return DomainContext(
            subject_id=f"t7-{self.tag}", allowed_shop_ids=allowed, shop_refs=refs,
            conn=self.conn, store=store or MemoryQueryRunStore(
                forbidden_values=set(allowed)),
            chat_id=UUID(int=1), user_message_id=UUID(int=2),
            root_request_id=UUID(int=3), now=NOW,
            deadline=time.monotonic() + 30, attempt_no=1)

    def _run(self, *, store: MemoryQueryRunStore | None = None,
             allowed: frozenset[str] | None = None,
             **request_overrides: object):
        return analyze_product_performance(
            _request(**request_overrides), self._context(store=store, allowed=allowed))

    # -- 1. 指定商品跨店：均价、份额与合计 --------------------------------

    def test_product_report_aggregates_across_shops_with_one_average_price(self):
        self._shop("1")
        self._shop("2")
        self._archive("P1", "直钉枪")
        for key in self.shops:
            self._cover(key, WINDOW_START, WINDOW_END)
        self._sale("1", "E1", product="P1", day=date(2026, 9, 2),
                   quantity="1", amount="100", cost="40")
        self._sale("2", "E2", product="P1", day=date(2026, 9, 3),
                   quantity="9", amount="450", cost="30")

        result = self._run(
            profit_basis="existing_fields",
            metrics=["sold_quantity", "sales_amount", "weighted_avg_paid_price",
                     "product_gross_profit_reference",
                     "product_gross_margin_reference"])
        payload = result.model_payload
        self.assertEqual(result.status.value, "success", payload.get("limitations"))
        total = [row for row in payload["data"]
                 if "shop_ref" not in row and row.get("line_kind") == "sale"]
        self.assertEqual(len(total), 1, "跨店合计行必须唯一且不带 shop_ref")
        # 均价是 550/10=55，绝不是两家店均价 (100+50)/2=75。
        self.assertEqual(total[0]["weighted_avg_paid_price"], "55")
        self.assertEqual(total[0]["sales_amount"], "550")
        self.assertEqual(total[0]["sold_quantity"], "10")
        self.assertEqual(total[0]["product_gross_profit_reference"], "240")
        self.assertEqual(total[0]["product_gross_margin_reference"], "0.436364")
        shares = {row["shop_ref"]: row["sales_share"] for row in payload["data"]
                  if "shop_ref" in row}
        self.assertEqual(sorted(shares.values()), ["0.181818", "0.818182"],
                         "份额按金额算：100/550 与 450/550")
        self.assertEqual({row["shop_ref"]: row["weighted_avg_paid_price"]
                          for row in payload["data"] if "shop_ref" in row},
                         {ref: value for ref, value in
                          zip(shares, ("100", "50"))},
                         "逐店均价各自保留，合并只发生在合计行")
        opportunity = payload["opportunity"]
        self.assertEqual(opportunity["status"], "unconfigured",
                         "没有版本化阈值与最小样本就不许给低利润结论")
        self.assertEqual(opportunity["flagged"], [])
        self.assertEqual([item["sales_amount"] for item in opportunity["candidates"]],
                         ["100", "450"], "候选按毛利率升序，最低利润的先看到")
        self.assertEqual(payload["metric_units"]["sales_amount"], "CNY")
        self.assertEqual(payload["metric_units"]["sold_quantity"], "piece")

    def test_trend_series_and_sku_refs_are_published(self):
        self._shop("1")
        self._archive("P1", "直钉枪")
        self._cover("1", WINDOW_START, WINDOW_END)
        self._sale("1", "E1", product="P1", day=date(2026, 9, 5), quantity="1",
                   amount="100", cost="40", sku="SKU-A")
        self._sale("1", "E2", product="P1", day=date(2026, 9, 6), quantity="2",
                   amount="200", cost="30", sku="SKU-B")

        result = self._run(start="2026-09-05", end="2026-09-07",
                           metrics=["sold_quantity", "sales_amount"])
        payload = result.model_payload
        self.assertEqual(payload["trend_window"], ["2026-08-31", "2026-09-07"],
                         "趋势窗口独立标注为 [end-7天, end)，即使主期间只有两天")
        trend = _payload_for(result, "trend_series")
        rows = {row["day"]: row for row in trend["data"]}
        self.assertEqual(len(rows), 7, "七日序列必须七天齐全")
        self.assertEqual(rows["2026-09-05"]["sold_quantity"], "1")
        self.assertEqual(rows["2026-09-06"]["sold_quantity"], "2")
        # 已覆盖但没有成交的日是真实 0，不是 null。
        self.assertEqual(rows["2026-09-01"]["sold_quantity"], "0")
        self.assertEqual(rows["2026-09-01"]["sales_amount"], "0")
        self.assertEqual(trend["coverage"]["status"], "complete")
        skus = payload["resolved_product"]["sku_refs"]
        self.assertEqual(len(skus), 2, "两个 SKU 都要出现在明细引用里")
        self.assertTrue(all(str(ref).startswith("ent-") for ref in skus))
        self.assertNotIn("SKU-A", json.dumps(payload, ensure_ascii=False),
                         "ERP SKU 号不进模型载荷")

    # -- 2. 成本与行性质：null 就是 null ---------------------------------

    def test_missing_cost_nulls_profit_but_keeps_sales_and_quantity(self):
        self._shop("1")
        self._archive("P1", "直钉枪")
        self._cover("1", WINDOW_START, WINDOW_END)
        self._sale("1", "E1", product="P1", day=date(2026, 9, 2),
                   quantity="1", amount="100", cost="40")
        self._sale("1", "E2", product="P1", day=date(2026, 9, 3),
                   quantity="2", amount="200", cost=None)

        result = self._run(
            profit_basis="existing_fields",
            metrics=["sold_quantity", "sales_amount",
                     "product_gross_profit_reference",
                     "product_gross_margin_reference"])
        payload = result.model_payload
        total = [row for row in payload["data"] if "shop_ref" not in row][0]
        self.assertIsNone(total["product_gross_profit_reference"],
                          "已知成本子集不能冒充整行集合的毛利")
        self.assertIsNone(total["product_gross_margin_reference"])
        self.assertEqual(total["sales_amount"], "300")
        self.assertEqual(total["sold_quantity"], "3")
        self.assertTrue(any("成本覆盖不全" in text for text in payload["limitations"]),
                        payload["limitations"])
        self.assertEqual(payload["termination_reason"], "succeeded",
                         "缺成本只是这一个指标不可算，不改变整份报告的成功状态")

    def test_suite_and_gift_lines_never_enter_product_gross_profit(self):
        self._shop("1")
        self._archive("P1", "直钉枪")
        self._cover("1", WINDOW_START, WINDOW_END)
        self._sale("1", "E1", product="P1", day=date(2026, 9, 2),
                   quantity="1", amount="100", cost="40")
        self._sale("1", "E2", product="P1", day=date(2026, 9, 3), quantity="1",
                   amount="80", cost="50", line_kind="suite")
        # 赠品父行：与 v_product_daily 同一纳入条件，两边都不该出现。
        self._sale("1", "E3", product="P1", day=date(2026, 9, 3), quantity="1",
                   amount="0", cost="9", line_kind="gift")

        result = self._run(
            profit_basis="existing_fields",
            metrics=["sold_quantity", "sales_amount",
                     "product_gross_profit_reference"])
        payload = result.model_payload
        kinds = sorted({str(row.get("line_kind")) for row in payload["data"]})
        self.assertEqual(kinds, ["sale", "suite"],
                         "赠品父行不进商品面；套件父行按自身行性质单列")
        self.assertTrue(any("套件/组合/加工父项" in text
                            for text in payload["limitations"]), payload["limitations"])
        suite = [row for row in payload["data"]
                 if row.get("line_kind") == "suite" and "shop_ref" in row][0]
        self.assertIsNone(suite["product_gross_profit_reference"],
                          "套件父项成本语义未核验，不许发布它的毛利")
        suite_total = [row for row in payload["data"]
                       if row.get("line_kind") == "suite" and "shop_ref" not in row][0]
        self.assertIsNone(suite_total["product_gross_profit_reference"])
        self.assertEqual(suite["sold_quantity"], "1", "件数照常发布，只是不给毛利")

    def test_unverified_allocation_keeps_quantity_and_drops_amount(self):
        self._shop("1")
        self._archive("P1", "直钉枪")
        self._cover("1", WINDOW_START, WINDOW_END)
        self._sale("1", "E1", product="P1", day=date(2026, 9, 2), quantity="3",
                   amount="300", cost="10", verified=False)

        payload = self._run(
            profit_basis="existing_fields",
            metrics=["sold_quantity", "sales_amount",
                     "product_gross_profit_reference"]).model_payload
        total = [row for row in payload["data"] if "shop_ref" not in row][0]
        self.assertEqual(total["sold_quantity"], "3", "件数不依赖分摊结论")
        self.assertIsNone(total["sales_amount"], "分摊未核验的金额不能当已核验销售额")
        self.assertIsNone(total["product_gross_profit_reference"])
        self.assertTrue(any("分摊金额未核验" in text for text in payload["limitations"]),
                        payload["limitations"])

    # -- 3. JOIN 放大：单据毛利只按唯一 ERP 单据算一次 --------------------

    def test_erp_document_profit_is_counted_once_per_document(self):
        self._shop("1")
        self._archive("P1", "直钉枪")
        self._cover("1", WINDOW_START, WINDOW_END)
        # 一张单据三行商品：单头毛利 30 只能算一次，连接行表就会变成 90。
        shop_id = self.shops["1"]
        paid_at = datetime(2026, 9, 2, 12, tzinfo=BEIJING)
        self.conn.execute(
            "INSERT INTO bi.orders(shop_id, erp_id, commercial_ids, source, "
            "source_updated_at, paid_at, raw_gross_profit, active, batch_id) VALUES "
            "(%s, 'E_MULTI', ARRAY['C_MULTI'], 'erp.trade.list.query', now(), %s, 30, "
            "true, 't7-seed')", (shop_id, paid_at))
        for index in range(3):
            self.conn.execute(
                "INSERT INTO bi.order_items(shop_id, erp_id, line_id, commercial_id, "
                "product_id, paid_at, quantity, allocated_paid_amount, "
                "allocation_verified, line_kind, active) VALUES "
                "(%s, 'E_MULTI', %s, 'C_MULTI', 'P1', %s, 1, 100, true, 'sale', true)",
                (shop_id, f"L{index}", paid_at))
        self._sale("1", "E_ONE", product="P1", day=date(2026, 9, 3), quantity="1",
                   amount="50", cost="10", document_profit="15", document_cost="35")

        result = self._run(
            profit_basis="existing_fields",
            metrics=["sales_amount", "erp_gross_profit_reference"])
        facet = _payload_for(result, "comparison_table")
        rows = [row for row in facet["data"] if "shop_ref" in row]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["erp_documents"], 2, "两张 ERP 单据")
        self.assertEqual(rows[0]["erp_gross_profit_reference"], "45",
                         "30 + 15：单据口径，不按行数放大")
        self.assertNotIn("product_gross_profit_reference", rows[0],
                         "单据毛利与商品毛利不同行，不能被相加")
        self.assertTrue(any("两个口径面" in text for text in result.model_payload["limitations"]),
                        result.model_payload["limitations"])

    def test_incomplete_document_coverage_nulls_erp_profit(self):
        self._shop("1")
        self._archive("P1", "直钉枪")
        self._cover("1", WINDOW_START, WINDOW_END)
        self._sale("1", "E1", product="P1", day=date(2026, 9, 2), quantity="1",
                   amount="50", cost="10", document_profit="15")
        self._sale("1", "E2", product="P1", day=date(2026, 9, 3), quantity="1",
                   amount="50", cost="10", document_profit=None)

        facet = _payload_for(self._run(
            profit_basis="existing_fields",
            metrics=["sales_amount", "erp_gross_profit_reference"]),
            "comparison_table")
        row = [item for item in facet["data"] if "shop_ref" in item][0]
        self.assertEqual(row["erp_documents"], 2)
        self.assertEqual(row["erp_documents_with_gross_profit"], 1)
        self.assertIsNone(row["erp_gross_profit_reference"],
                          "一半单据没有毛利字段时不许拿有值子集冒充整体")
        self.assertTrue(any("单据毛利覆盖不全" in text for text in facet["limitations"]),
                        facet["limitations"])

    # -- 4. 退款跨期：售后不参与商品毛利 ---------------------------------

    def test_refunds_in_window_do_not_change_product_gross_profit(self):
        self._shop("1")
        self._archive("P1", "直钉枪")
        self._cover("1", WINDOW_START, WINDOW_END)
        self._sale("1", "E1", product="P1", day=date(2026, 9, 2), quantity="2",
                   amount="200", cost="50", document_profit="100")
        # 一笔跨期退款：本期完成、原单在上期且未匹配。
        self.conn.execute(
            "INSERT INTO bi.aftersales(shop_id, aftersale_id, commercial_id, "
            "raw_platform_amount, platform_completed_at, source_updated_at, "
            "platform_success, refund_canonical, matched, batch_id) VALUES "
            "(%s, 'A1', 'C-OLD', 90, '2026-09-04 10:00+08', now(), true, true, false, "
            "'t7-seed')", (self.shops["1"],))
        self._cover("1", datetime(2026, 8, 1, tzinfo=BEIJING),
                    datetime(2026, 9, 12, tzinfo=BEIJING),
                    source="erp.trade.list.query")

        payload = self._run(
            profit_basis="existing_fields",
            metrics=["sales_amount", "product_gross_profit_reference",
                     "erp_gross_profit_reference"]).model_payload
        total = [row for row in payload["data"] if "shop_ref" not in row][0]
        self.assertEqual(total["product_gross_profit_reference"], "100",
                         "200 - 50*2：退款不进商品毛利")
        self.assertTrue(any("未扣售后" in text for text in payload["limitations"]),
                        payload["limitations"])
        serialized = json.dumps(payload["data"], ensure_ascii=False)
        self.assertNotIn("refund_amount", serialized,
                         "本工具不发布退款指标，跨期退款更不许从毛利里扣")

    # -- 5. 能力、来源与口径：三类缺口分开归因 ----------------------------

    def test_platform_without_payment_source_is_excluded_not_zeroed(self):
        self._shop("1")
        self._shop("2", platform="pdd", capabilities=["erp_documents"])
        self._archive("P1", "直钉枪")
        self._cover("1", WINDOW_START, WINDOW_END)
        self._cover("2", WINDOW_START, WINDOW_END,
                    source="erp.trade.outstock.simple.query")
        self._sale("1", "E1", product="P1", day=date(2026, 9, 2), quantity="1",
                   amount="100", cost="40", document_profit="60")
        self._sale("2", "E2", product="P1", day=date(2026, 9, 2), quantity="5",
                   amount="500", cost="10", document_profit="55",
                   source="erp.trade.outstock.simple.query")

        payload = self._run(
            profit_basis="existing_fields",
            metrics=["sales_amount", "product_gross_profit_reference",
                     "erp_gross_profit_reference"]).model_payload
        excluded = {item["shop_ref"]: item["reason"] for item in payload["excluded_scope"]}
        self.assertEqual(len(excluded), 1, excluded)
        self.assertEqual(list(excluded.values()), ["coverage_time_basis_unverified"],
                         "拼多多商品销售额走的是出库通道：时间口径实测不成立，"
                         "归因到时间口径而不是缺数据")
        total = [row for row in payload["data"] if "shop_ref" not in row][0]
        self.assertEqual(total["sales_amount"], "100", "被排除店铺的行不参与合计")
        self.assertTrue(any("未列入本次合计" in text for text in payload["limitations"]),
                        payload["limitations"])
        self.assertEqual(payload["status"], "partial",
                         "两家店里有第三家的缺口：总结果只能是 partial")

    def test_grant_only_difference_is_reported_as_capability_gap(self):
        """来源在、但该店没被授予商品能力：只有对账能开通，缩范围救不了。"""
        self._shop("1")
        self._shop("2", capabilities=["paid_amount", "paid_orders", "erp_documents"])
        self._archive("P1", "直钉枪")
        self._cover("1", WINDOW_START, WINDOW_END)
        self._cover("2", WINDOW_START, WINDOW_END)
        self._sale("1", "E1", product="P1", day=date(2026, 9, 2), quantity="1",
                   amount="100", cost="40")
        self._sale("2", "E2", product="P1", day=date(2026, 9, 2), quantity="9",
                   amount="900", cost="10")

        result = self._run(metrics=["sales_amount"])
        payload = result.model_payload
        self.assertEqual(result.status.value, "partial")
        excluded = {item["reason"] for item in payload["excluded_scope"]}
        self.assertEqual(excluded, {"capability_ungranted"},
                         "已登记来源但未授予能力是另一种缺口，不能说成来源未开通")
        self.assertEqual([row["sales_amount"] for row in payload["data"]
                          if "shop_ref" not in row], ["100"])

    def test_tb_shop_cannot_answer_a_pay_time_product_window(self):
        self._shop("1")
        self._shop("2", platform="tb")
        self._archive("P1", "直钉枪")
        self._cover("1", WINDOW_START, WINDOW_END)
        self._cover("2", WINDOW_START, WINDOW_END,
                    source="erp.trade.outstock.simple.query")
        self._sale("1", "E1", product="P1", day=date(2026, 9, 2), quantity="1",
                   amount="100", cost="40")
        self._sale("2", "E2", product="P1", day=date(2026, 9, 2), quantity="9",
                   amount="900", cost="10", source="erp.trade.outstock.simple.query")

        payload = self._run(metrics=["sold_quantity", "sales_amount"]).model_payload
        excluded = {item["reason"] for item in payload["excluded_scope"]}
        self.assertIn("coverage_time_basis_unverified", excluded,
                      "出库通道的 paid_at 实测不成立，不能按支付窗口出数")
        self.assertTrue(any("付款时间口径未经认证" in text
                            for text in payload["limitations"]), payload["limitations"])
        total = [row for row in payload["data"] if "shop_ref" not in row][0]
        self.assertEqual(total["sold_quantity"], "1")

    def test_erp_documents_only_discloses_the_unverified_time_basis(self):
        """单据数与单据毛利不是支付窗口主张：同一家淘系店在这里可答，只披露。"""
        self._shop("1", platform="tb")
        self._archive("P1", "直钉枪")
        self._cover("1", WINDOW_START, WINDOW_END,
                    source="erp.trade.outstock.simple.query")
        self._sale("1", "E1", product="P1", day=date(2026, 9, 2), quantity="1",
                   amount="100", cost="40", document_profit="30",
                   source="erp.trade.outstock.simple.query")

        result = self._run(profit_basis="existing_fields",
                           metrics=["erp_gross_profit_reference"])
        payload = result.model_payload
        self.assertEqual(payload.get("excluded_scope", []), [])
        self.assertTrue(any("未认证付款时间口径" in text
                            for text in payload["limitations"]), payload["limitations"])
        facet = _payload_for(result, "comparison_table")
        self.assertEqual([row["erp_gross_profit_reference"] for row in facet["data"]
                          if "shop_ref" in row], ["30"])

    def test_verified_payment_basis_does_not_masquerade_as_product_sales(self):
        self._shop("1")
        self._archive("P1", "直钉枪")
        self._cover("1", WINDOW_START, WINDOW_END)
        self._sale("1", "E1", product="P1", day=date(2026, 9, 2), quantity="1",
                   amount="100", cost="40")
        self.conn.execute(
            "INSERT INTO bi.order_payments(shop_id, commercial_id, paid_at, amount, "
            "currency, basis, verified) VALUES "
            "(%s, 'C_E1', '2026-09-02 12:00+08', 100, 'CNY', 'items', true)",
            (self.shops["1"],))

        result = self._run(sales_basis="verified_payment", metrics=["sales_amount"])
        payload = result.model_payload
        self.assertTrue(any("已验证支付口径没有商品级事实" in text
                            for text in payload["limitations"]), payload["limitations"])
        self.assertEqual({item["status"] for item in payload["metric_statuses"]},
                         {"incomparable"})
        self.assertNotIn("product_id", json.dumps(payload["data"]),
                         "该口径下不发布任何商品面数字")
        facet = _payload_for(result, "comparison_table")
        self.assertEqual([row["paid_amount"] for row in facet["data"]], ["100"],
                         "商业支付事实单独一面，标签自证口径")

    # -- 6. 授权与解析 ----------------------------------------------------

    def test_out_of_scope_shop_ref_is_forbidden_not_dropped(self):
        self._shop("1")
        other = self._shop("2")
        allowed = frozenset({self.shops["1"]})
        result = analyze_product_performance(
            _request(scope={"mode": "selected",
                            "shop_refs": [ref_for_key("shop", other)]}),
            self._context(allowed=allowed))
        self.assertEqual(result.status.value, "failed")
        self.assertEqual(result.error.code, "forbidden")
        self.assertEqual(result.model_payload, {"status": "failed"},
                         "越权时不发任何数字，也不发部分结果")

    def test_unauthorized_product_ref_does_not_reveal_its_name(self):
        """别的店里卖过的商品：本轮授权范围内解析不出来，名称一个都不给。"""
        self._shop("1")
        other_shop = self._shop("2")
        self._archive("P_HIDDEN", "只在别店卖的货")
        self._cover("1", WINDOW_START, WINDOW_END)
        self._sale("2", "EH", product="P_HIDDEN", day=date(2026, 9, 2), quantity="1",
                   amount="10", cost="1")
        hidden_ref = ref_for_key("product", "P_HIDDEN")
        result = analyze_product_performance(
            _request(product={"ref": hidden_ref}),
            self._context(allowed=frozenset({self.shops["1"]})))
        self.assertEqual(result.status.value, "missing_data")
        self.assertNotIn("只在别店卖的货",
                         json.dumps(result.model_payload, ensure_ascii=False,
                                    default=str))

    def test_ambiguous_text_returns_needs_input_without_choosing(self):
        self._shop("1")
        self._cover("1", WINDOW_START, WINDOW_END)
        for product, label in (("PA", "6mm"), ("PB", "8mm")):
            self._archive(product, "接头")
            self._sale("1", f"E{product}", product=product, day=date(2026, 9, 2),
                       quantity="1", amount="10", cost="1")
            self.conn.execute(
                "UPDATE bi.order_items SET product_name_snapshot='接头' "
                "WHERE erp_id=%s", (f"E{product}",))

        result = self._run(product={"text": "接头"})
        self.assertEqual(result.status.value, "needs_input")
        self.assertEqual(result.error.recovery.value, "correct_parameters")
        self.assertEqual(result.model_payload["status"], "needs_input")
        self.assertNotIn("data", result.model_payload,
                         "歧义时不发任何数字，避免被当成已确定答案")
        self.assertEqual(result.artifacts, [], "needs_input 不发布数据集")
        self.assertTrue(any("候选商品命中同一文本" in text
                            for text in result.model_payload["limitations"]),
                        result.model_payload)

    def test_unresolved_product_is_not_reported_as_zero_sales(self):
        self._shop("1")
        self._cover("1", WINDOW_START, WINDOW_END)
        result = self._run(product={"text": "从没见过的商品"})
        self.assertEqual(result.status.value, "missing_data")
        self.assertTrue(any("商品未解析出来" in text
                            for text in result.model_payload["limitations"]),
                        result.model_payload)

    # -- 7. 覆盖、持久化与降级边界 ----------------------------------------

    def test_trend_gap_leaves_null_days_and_keeps_the_report_usable(self):
        self._shop("1")
        self._archive("P1", "直钉枪")
        # 覆盖只有主期间那一段（[09-04,09-06)）：趋势窗口 [08-30,09-06) 的前段是缺口，
        # 主期间完全落在已覆盖段内。
        self._cover("1", datetime(2026, 9, 4, tzinfo=BEIJING),
                    datetime(2026, 9, 6, tzinfo=BEIJING))
        self._sale("1", "E1", product="P1", day=date(2026, 9, 4), quantity="1",
                   amount="100", cost="40")

        result = self._run(start="2026-09-04", end="2026-09-06",
                           metrics=["sold_quantity", "sales_amount"])
        payload = result.model_payload
        self.assertEqual(payload["status"], "ok")
        trend = _payload_for(result, "trend_series")
        rows = {row["day"]: row for row in trend["data"]}
        self.assertEqual(len(rows), 7)
        # 缺口日必须是 null：写 0 就等于"那天确实没卖"，那是编出来的。
        self.assertIsNone(rows["2026-09-01"]["sold_quantity"])
        self.assertIsNone(rows["2026-08-31"]["sales_amount"])
        self.assertEqual(rows["2026-09-04"]["sold_quantity"], "1")
        self.assertEqual(rows["2026-09-05"]["sold_quantity"], "0",
                         "已覆盖但没成交才是真实 0")
        self.assertEqual(trend["coverage"]["status"], "partial")
        self.assertTrue(trend["coverage"]["gaps"])
        self.assertTrue(any("趋势窗口覆盖不足" in text for text in trend["limitations"]),
                        trend["limitations"])
        self.assertEqual(payload["status"], "ok",
                         "趋势缺口不牵动主报告：主期间覆盖是完整的")

    def test_coverage_gap_excludes_the_shop_instead_of_summing_a_partial_window(self):
        self._shop("1")
        self._shop("2")
        self._archive("P1", "直钉枪")
        self._cover("1", WINDOW_START, WINDOW_END)
        self._cover("2", datetime(2026, 9, 4, tzinfo=BEIJING), WINDOW_END)
        self._sale("1", "E1", product="P1", day=date(2026, 9, 2), quantity="1",
                   amount="100", cost="40")
        self._sale("2", "E2", product="P1", day=date(2026, 9, 5), quantity="9",
                   amount="900", cost="40")

        payload = self._run(metrics=["sales_amount"]).model_payload
        self.assertEqual(len(payload["excluded_scope"]), 1)
        self.assertEqual(payload["excluded_scope"][0]["reason"], "coverage_incomplete")
        self.assertTrue(payload["excluded_scope"][0]["windows"],
                        "缺口要说清是哪一段日期，而不是只说没覆盖")
        total = [row for row in payload["data"] if "shop_ref" not in row][0]
        self.assertEqual(total["sales_amount"], "100",
                         "拒绝部分汇总：缺覆盖的店不参与合计，也不冒充 0")
        self.assertEqual(payload["status"], "partial")

    def test_source_quality_failure_refuses_numbers(self):
        self._shop("1")
        self._archive("P1", "直钉枪")
        self._cover("1", WINDOW_START, WINDOW_END)
        self._sale("1", "E1", product="P1", day=date(2026, 9, 2), quantity="1",
                   amount="100", cost="40")
        self.conn.execute("UPDATE bi.sync_state SET quality_status='failed' "
                          "WHERE shop_id=%s", (self.shops["1"],))

        result = self._run(metrics=["sales_amount"])
        self.assertEqual(result.status.value, "failed")
        self.assertEqual(result.model_payload, {"status": "unavailable",
                                                "limitations": [
                                                    "来源质量核验未通过，拒绝出数"]})
        self.assertEqual(result.error.code, "unavailable")
        self.assertEqual(result.model_payload["limitations"],
                         ["来源质量核验未通过，拒绝出数"])

    def test_stale_quality_rule_is_disclosed_as_unverified_not_failed(self):
        self._shop("1")
        self._archive("P1", "直钉枪")
        self._cover("1", WINDOW_START, WINDOW_END)
        self._sale("1", "E1", product="P1", day=date(2026, 9, 2), quantity="1",
                   amount="100", cost="40")
        self.conn.execute("UPDATE bi.sync_state SET quality_rule='kuaimai-reconcile/1' "
                          "WHERE shop_id=%s", (self.shops["1"],))

        payload = self._run(metrics=["sales_amount"]).model_payload
        self.assertTrue(any("来源质量未核验" in text for text in payload["limitations"]),
                        payload["limitations"])
        self.assertEqual([row["sales_amount"] for row in payload["data"]
                          if "shop_ref" not in row], ["100"],
                         "从未核验不等于数据有错：可以出数，但必须说明")

    def test_deadline_exhaustion_is_unavailable_not_an_empty_answer(self):
        self._shop("1")
        self._archive("P1", "直钉枪")
        self._cover("1", WINDOW_START, WINDOW_END)
        self._sale("1", "E1", product="P1", day=date(2026, 9, 2), quantity="1",
                   amount="100", cost="40")
        context = self._context()
        context.deadline = time.monotonic() - 1
        result = analyze_product_performance(_request(metrics=["sales_amount"]), context)
        self.assertEqual(result.status.value, "failed")
        self.assertEqual(result.error.code, "deadline_exceeded")
        self.assertEqual(result.model_payload["status"], "unavailable")
        self.assertEqual(result.identity.request_fingerprint,
                         result.identity.request_fingerprint)

    def test_artifact_persistence_failure_is_not_reported_as_success(self):
        self._shop("1")
        self._archive("P1", "直钉枪")
        self._cover("1", WINDOW_START, WINDOW_END)
        self._sale("1", "E1", product="P1", day=date(2026, 9, 2), quantity="1",
                   amount="100", cost="40")
        failing = _ArtifactStoreFails(forbidden_values={self.shops["1"]})
        result = self._run(store=failing, metrics=["sales_amount"])
        self.assertEqual(result.status.value, "failed")
        self.assertEqual(result.error.code, "artifact_persistence_failed")
        self.assertEqual(result.model_payload, {"status": "failed"},
                         "保存失败时被投影的数字不得回流给模型")
        self.assertEqual(failing.runs[result.run_id]["termination_reason"],
                         "persistence_failed")

    def test_run_and_artifacts_persist_under_the_commerce_domain(self):
        self._shop("1")
        self._archive("P1", "直钉枪")
        self._cover("1", WINDOW_START, WINDOW_END)
        self._sale("1", "E1", product="P1", day=date(2026, 9, 2), quantity="1",
                   amount="100", cost="40", sku="SKU-A")
        store = MemoryQueryRunStore(forbidden_values={self.shops["1"]})
        context = self._context(store=store)
        result = analyze_product_performance(_request(metrics=["sales_amount"]),
                                            context)
        run = store.runs[result.run_id]
        self.assertEqual(run["domain"], "commerce_performance")
        self.assertEqual(run["status"], "succeeded")
        nodes = [event["node"] for event in store.events[result.run_id]]
        self.assertEqual(nodes[0], "resolve_scope")
        self.assertEqual(set(nodes) & COMMERCE_NODES, set(COMMERCE_NODES),
                         "完整节点链都要留痕，缺一个节点就是少一次审计")
        self.assertTrue(set(nodes) <= COMMERCE_NODES, nodes)
        self.assertTrue(run["request_fingerprint"])
        provenance = run["provenance"]
        self.assertEqual(provenance.metric_version, "commerce-metrics/2026-09-13.1")
        self.assertEqual(provenance.template_id, "commerce_product_report")
        self.assertTrue(provenance.basis_signature, "口径签名要参与指纹")
        self.assertTrue(provenance.source_batches, "来源批次要进血缘")
        kinds = sorted({artifact["artifact_type"]
                        for artifact in store.artifacts.values()})
        self.assertEqual(kinds, ["metric_result", "trend_series"])
        state = run["state"]
        self.assertEqual(state["normalized_request"]["product_ref"],
                         ref_for_key("product", "P1"),
                         "文本选择器也要落成引用，否则指纹对不上真实问题")
        self.assertNotIn("直钉枪", json.dumps(state, ensure_ascii=False, default=str),
                         "解析用的文本与商品主键都不进运行状态")
        self.assertNotIn(self.shops["1"], json.dumps(state, ensure_ascii=False))

    def test_fingerprint_moves_when_the_data_version_moves(self):
        self._shop("1")
        self._archive("P1", "直钉枪")
        self._cover("1", WINDOW_START, WINDOW_END)
        self._sale("1", "E1", product="P1", day=date(2026, 9, 2), quantity="1",
                   amount="100", cost="40")
        first_store = MemoryQueryRunStore(forbidden_values={self.shops["1"]})
        first = analyze_product_performance(_request(metrics=["sales_amount"]),
                                           self._context(store=first_store))
        # 回填推进了共同截止：同一问题不再是同一个请求。
        self.conn.execute("UPDATE bi.sync_state SET data_as_of=%s WHERE shop_id=%s",
                          (datetime(2026, 9, 12, 18, tzinfo=BEIJING), self.shops["1"]))
        second_store = MemoryQueryRunStore(forbidden_values={self.shops["1"]})
        second = analyze_product_performance(_request(metrics=["sales_amount"]),
                                            self._context(store=second_store))
        self.assertNotEqual(first.identity.request_fingerprint,
                            second.identity.request_fingerprint)

    # -- 8. 视图与角色边界（真实 SQL） -----------------------------------

    def test_commerce_reads_work_for_the_app_role(self):
        """经营图在真实部署里以 bi_app 身份连接：读不到的视图等于工具直接报错。"""
        self._shop("1")
        self._archive("P1", "直钉枪")
        self._cover("1", WINDOW_START, WINDOW_END)
        self._sale("1", "E1", product="P1", day=date(2026, 9, 2), quantity="1",
                   amount="100", cost="40", document_profit="20", document_cost="80")
        with self.conn.transaction():
            self.conn.execute("SET LOCAL ROLE bi_app")
            for view, needs_shop in (
                    ("v_product_cost_daily", True), ("v_erp_document_daily", True),
                    ("v_product_candidate_lines", True), ("v_product_refs", False)):
                sql = f"SELECT count(*) FROM reporting.{view}"
                params = ()
                if needs_shop:
                    sql += " WHERE shop_id = ANY(%s)"
                    params = ([self.shops["1"]],)
                self.conn.execute(sql, params).fetchone()
            # 新增视图没有扩大 bi_app 的范围：底层主档依旧读不到。
            # 每条负向断言包在自己的保存点里：一句权限失败会把整段事务打成
            # aborted，不分开的话只有第一条在真验权限，后四条验的是
            # InFailedSqlTransaction——那是空断言，不是最小权限证据。
            for table in ("bi.order_items", "bi.orders", "bi.products",
                          "bi.entity_refs", "bi.catalog_state"):
                with self.assertRaises(Exception, msg=table) as denied:
                    with self.conn.transaction():
                        self.conn.execute(f"SELECT count(*) FROM {table}").fetchone()
                self.assertIsInstance(
                    denied.exception, psycopg.errors.InsufficientPrivilege,
                    f"{table} 读不到必须是权限拒绝，而不是事务已中断后的空断言：{denied.exception!r}")

    def test_product_cost_daily_uses_the_same_rows_as_product_daily(self):
        """成本面与 v_product_daily 必须同纳入条件、同数值，否则两个面自相矛盾。"""
        self._shop("1")
        self._archive("P1", "直钉枪")
        self._sale("1", "E1", product="P1", day=date(2026, 9, 2), quantity="2",
                   amount="200", cost="40", sku="SKU-A")
        self._sale("1", "E2", product="P1", day=date(2026, 9, 2), quantity="1",
                   amount="0", cost="5", line_kind="gift")
        self._sale("1", "E3", product="P1", day=date(2026, 9, 3), quantity="1",
                   amount="90", cost="30", verified=False)
        self._sale("1", "E4", product="P1", day=date(2026, 9, 3), quantity="1",
                   amount="60", cost="20", line_kind="suite", sku="SKU-S")
        rows = self.conn.execute(
            "SELECT d.day, d.line_kind, d.quantity, d.product_paid_amount, "
            "c.quantity, c.sales_amount, c.line_count, c.cost_line_count, "
            "c.allocation_verified "
            "FROM reporting.v_product_daily d "
            "JOIN reporting.v_product_cost_daily c "
            "ON c.shop_id = d.shop_id AND c.day = d.day "
            "AND c.product_id = d.product_id AND c.line_kind = d.line_kind "
            "WHERE d.shop_id = %s AND d.product_id = 'P1' "
            "ORDER BY d.day, d.line_kind", (self.shops["1"],)).fetchall()
        self.assertEqual(
            [(str(row[0]), row[1], str(row[2]), str(row[3]), str(row[4]), str(row[5]))
             for row in rows],
            [("2026-09-02", "sale", "2.000000", "200.000000", "2.000000", "200.000000"),
             ("2026-09-03", "sale", "1.000000", "90.000000", "1.000000", "90.000000"),
             ("2026-09-03", "suite", "1.000000", "60.000000", "1.000000", "60.000000")],
            "两面的纳入条件与数值必须逐行相同（赠品父行两边都不出现）")
        self.assertEqual([str(row[6]) for row in rows], ["1", "1", "1"])
        self.assertEqual([str(row[7]) for row in rows], ["1", "1", "1"])
        self.assertEqual([bool(row[8]) for row in rows], [True, False, True])

    def test_cost_total_only_covers_costed_lines(self):
        self._shop("1")
        self._archive("P1", "直钉枪")
        self._sale("1", "E1", product="P1", day=date(2026, 9, 2), quantity="2",
                   amount="200", cost="40")
        self._sale("1", "E2", product="P1", day=date(2026, 9, 2), quantity="3",
                   amount="300", cost=None)
        row = self.conn.execute(
            "SELECT line_count, cost_line_count, quantity, cost_quantity, cost_total "
            "FROM reporting.v_product_cost_daily "
            "WHERE shop_id = %s AND product_id = 'P1' AND day = '2026-09-02'",
            (self.shops["1"],)).fetchone()
        self.assertEqual([str(item) for item in row[:2]], ["2", "1"],
                         "行数与带成本行数一起给，覆盖率不靠猜")
        self.assertEqual(Decimal(str(row[2])), Decimal("5"))
        self.assertEqual(Decimal(str(row[3])), Decimal("2"))
        self.assertEqual(Decimal(str(row[4])), Decimal("80"))

    def test_erp_document_view_has_one_row_per_document(self):
        self._shop("1")
        self._archive("P1", "直钉枪")
        for index in range(3):
            self._sale("1", f"E{index}", product="P1",
                       day=date(2026, 9, 2 + index % 2), quantity="1", amount="10",
                       cost="1", document_profit="5")
        counted = self.conn.execute(
            "SELECT count(*), count(DISTINCT erp_id) FROM reporting.v_erp_document_daily "
            "WHERE shop_id = %s", (self.shops["1"],)).fetchone()
        self.assertEqual(str(counted[0]), str(counted[1]),
                         "一行一张单据：否则聚合就会放大金额")
        self.conn.execute("UPDATE bi.orders SET active = false WHERE shop_id = %s "
                          "AND erp_id = 'E0'", (self.shops["1"],))
        remaining = self.conn.execute(
            "SELECT count(*) FROM reporting.v_erp_document_daily WHERE shop_id = %s",
            (self.shops["1"],)).fetchone()[0]
        self.assertEqual(int(remaining), 2, "关闭单不进单据面")

    # -- 9. 上期比较：两侧必须同一个行性质口径 ------------------------------

    def test_previous_period_compares_the_same_line_kinds_on_both_sides(self):
        """sale + suite 并存时，本期与上期都必须是“该店该窗口内全部行性质”的聚合。

        本期值若只取结果表第一行（排序后就是 sale 那一组），而上期取全行性质聚合，
        两侧集合不同，change / change_ratio 就成了两个不同口径之差——而这两个数正是
        模型拿去说“环比”的东西。结果表本身仍按行性质逐行发行（套件父行单列），比较只是
        把两侧合到同一个口径上。赠品不进商品面、套件成本语义未核验 ⇒ 毛利参考两期都是
        null，差额也是 null：修口径不能把这两个限定顺手抹掉。
        """
        self._shop("1")
        self._archive("P1", "直钉枪")
        self._cover("1", WINDOW_START, WINDOW_END)
        # 本期 [09-04,09-08)：sale 1件/100元，suite 1件/25元，另有一行赠品。
        self._sale("1", "E_NOW_SALE", product="P1", day=date(2026, 9, 4),
                   quantity="1", amount="100", cost="40")
        self._sale("1", "E_NOW_SUITE", product="P1", day=date(2026, 9, 5),
                   quantity="1", amount="25", cost="5", line_kind="suite")
        self._sale("1", "E_NOW_GIFT", product="P1", day=date(2026, 9, 5),
                   quantity="3", amount="0", cost="9", line_kind="gift")
        # 上期 [08-31,09-04)：sale 2件/200元，suite 2件/50元。
        self._sale("1", "E_PRE_SALE", product="P1", day=date(2026, 9, 1),
                   quantity="2", amount="200", cost="50")
        self._sale("1", "E_PRE_SUITE", product="P1", day=date(2026, 9, 2),
                   quantity="2", amount="50", cost="10", line_kind="suite")

        result = self._run(start="2026-09-04", end="2026-09-08",
                           comparison="previous_period",
                           profit_basis="existing_fields",
                           metrics=["sold_quantity", "sales_amount",
                                    "weighted_avg_paid_price",
                                    "product_gross_profit_reference"])
        payload = result.model_payload
        block = payload["comparison"]
        self.assertTrue(block["comparable"], payload.get("limitations"))
        self.assertEqual(block["window"], ["2026-08-31", "2026-09-04"],
                         "上期窗口 = [start-span, start)，独立标注")
        rows = {str(row["metric"]): row for row in block["rows"]}
        self.assertEqual({str(row["shop_ref"]) for row in rows.values()},
                         {ref_for_key("shop", self.shops["1"])})
        # 本期：件数 1+1=2，金额 100+25=125（不是只 sale 那组的 1 件/100 元）。
        self.assertEqual(rows["sold_quantity"]["current"], "2")
        self.assertEqual(rows["sold_quantity"]["previous"], "4")
        self.assertEqual(rows["sold_quantity"]["change"], "-2")
        self.assertEqual(rows["sold_quantity"]["change_ratio"], "-0.5",
                         "两侧同一口径才是 -50%；本期只取 sale 那组会算成 -75%")
        self.assertEqual(rows["sales_amount"]["current"], "125")
        self.assertEqual(rows["sales_amount"]["previous"], "250")
        self.assertEqual(rows["sales_amount"]["change"], "-125")
        self.assertEqual(rows["sales_amount"]["change_ratio"], "-0.5",
                         "本期只取 sale 那组会把它说成 -60%")
        # 均价是比出来的：125/2 与 250/4 都是 62.5，环比不变。
        self.assertEqual(rows["weighted_avg_paid_price"]["current"], "62.5",
                         "均价也从同一集合算：只取 sale 行会变成 100")
        self.assertEqual(rows["weighted_avg_paid_price"]["previous"], "62.5")
        self.assertEqual(rows["weighted_avg_paid_price"]["change"], "0")
        self.assertEqual(rows["weighted_avg_paid_price"]["change_ratio"], "0")
        # 套件父项成本语义未核验：两期都不发毛利，差额也不发（不是 0）。
        for key in ("current", "previous", "change", "change_ratio"):
            self.assertIsNone(rows["product_gross_profit_reference"][key],
                              "套件成本未核验时两期都不发毛利，差额也不许凭空出现")
        # 结果表仍按行性质逐行发行：合口径只发生在比较里，不把套件并回 sale 行。
        per_shop = [row for row in payload["data"] if "shop_ref" in row]
        self.assertEqual(sorted(str(row["line_kind"]) for row in per_shop),
                         ["sale", "suite"], "赠品不出现，套件单独成行")
        self.assertEqual(sum(Decimal(str(row["sold_quantity"])) for row in per_shop),
                         Decimal("2"),
                         "逐行性质行相加就是比较本期值：1(sale)+1(suite)，赠品行不计")


    # -- 10. 上期比较：基期不为正就不发增长率 --------------------------------

    def test_change_ratio_needs_a_positive_base(self):
        """增长率只在基期为正时发布；差额照发。

        商品毛利参考可以是真实的负数（上期卖得比成本高）。-50 → +60 是“转亏为盈”，
        除以负基期会得到 -2.2，把一次上涨说成跌 220%：符号被基期拧反，不是“涨得少”。
        基期为 0 时比率无定义；而基期为正时不能误杀正常增长率，所以同一条用例里
        三个位置一起钉。与 `commerce.metrics._ratio`（分母必须 > 0）同一规则。
        """
        self._shop("1")
        self._archive("P1", "直钉枪")
        self._cover("1", WINDOW_START, WINDOW_END)
        # 本期 [09-04,09-08)：1 件 / 100 元 / 成本 40 ⇒ 毛利参考 +60。
        self._sale("1", "E_NOW", product="P1", day=date(2026, 9, 4),
                   quantity="1", amount="100", cost="40")
        # 上期 [08-31,09-04)：1 件 / 0 元 / 成本 50 ⇒ 毛利参考 -50，收入基期为 0。
        self._sale("1", "E_PRE", product="P1", day=date(2026, 9, 1),
                   quantity="1", amount="0", cost="50")

        result = self._run(start="2026-09-04", end="2026-09-08",
                           comparison="previous_period",
                           profit_basis="existing_fields",
                           metrics=["sold_quantity", "sales_amount",
                                    "weighted_avg_paid_price",
                                    "product_gross_profit_reference",
                                    "product_gross_margin_reference"])
        payload = result.model_payload
        block = payload["comparison"]
        self.assertTrue(block["comparable"], payload.get("limitations"))
        self.assertEqual(block["window"], ["2026-08-31", "2026-09-04"])
        rows = {str(row["metric"]): row for row in block["rows"]}
        # 负基期：变化是转亏为盈 +110，不能反过来把方向拧成 -2.2。
        profit = rows["product_gross_profit_reference"]
        self.assertEqual(profit["current"], "60")
        self.assertEqual(profit["previous"], "-50")
        self.assertEqual(profit["change"], "110",
                         "差额不要求正基期：两期都在且同口径就该发")
        self.assertIsNone(profit["change_ratio"],
                          "负基期只能发 null：-2.2 会把上涨说成跌 220%")
        # 零基期：比率无定义。
        for metric in ("sales_amount", "weighted_avg_paid_price"):
            row = rows[metric]
            self.assertEqual(row["current"], "100")
            self.assertEqual(row["previous"], "0")
            self.assertEqual(row["change"], "100")
            self.assertIsNone(row["change_ratio"], f"{metric} 的基期是 0")
        # 基期为正：不能顺手把正常增长也杀成 null。
        quantity = rows["sold_quantity"]
        self.assertEqual(quantity["current"], "1")
        self.assertEqual(quantity["previous"], "1")
        self.assertEqual(quantity["change"], "0")
        self.assertEqual(quantity["change_ratio"], "0")
        # 上期毛利率本来就不成立（收入为 0）：基期缺失时全链都是 null，不是 0。
        margin = rows["product_gross_margin_reference"]
        self.assertEqual(margin["current"], "0.6")
        for key in ("previous", "change", "change_ratio"):
            self.assertIsNone(margin[key], f"product_gross_margin_reference.{key}")


class CommerceContractTests(unittest.TestCase):
    """不需要数据库的契约用例：白名单、词表与算术自锁。"""

    def test_node_chain_matches_the_domain_registry_exactly(self):
        self.assertEqual({node.value for node in CommerceNode}, set(COMMERCE_NODES))

    def test_node_chain_is_linear_and_rejects_jumps(self):
        state = CommerceState(run_id=uuid4())
        advanced = transition_state(state, CommerceNode.RESOLVE_PRODUCT)
        self.assertEqual(advanced.node, CommerceNode.RESOLVE_PRODUCT)
        with self.assertRaises(InvalidCommerceTransition):
            transition_state(state, CommerceNode.FINALIZE)
        with self.assertRaises(InvalidCommerceTransition):
            transition_state(advanced, CommerceNode.RESOLVE_SCOPE)

    def test_state_rejects_nodes_from_another_domain(self):
        from pydantic import ValidationError

        with self.assertRaises(ValidationError):
            CommerceState(run_id=uuid4(), node="execute_fixed_query")
        CommerceState(run_id=uuid4(), node="freeze_versions")

    def test_row_projection_refuses_silent_column_changes(self):
        row = {key: "1" for key in PRODUCT_ROWS}
        self.assertEqual(set(project(PRODUCT_ROWS, row)), set(PRODUCT_ROWS))
        with self.assertRaises(ValueError):
            project(PRODUCT_ROWS, {**row, "erp_shop_id": "S1"})
        with self.assertRaises(ValueError):
            project(tuple(PRODUCT_ROWS) + ("purchase_price",),
                    {**row, "purchase_price": "1"})

    def test_combining_groups_matches_the_line_arithmetic(self):
        """两个入口必须给出同一个答案：一份算术，两种形状。"""
        per_line = compute_reference_metrics([
            {"quantity": "1", "allocated_paid_amount": "100", "raw_unit_cost": "40"},
            {"quantity": "9", "allocated_paid_amount": "450", "raw_unit_cost": "30"}])
        per_group = combine_reference_metrics([
            {"quantity": "1", "sales_amount": "100", "cost_total": "40"},
            {"quantity": "9", "sales_amount": "450", "cost_total": "270"}])
        self.assertEqual(per_line, per_group)

    def test_commerce_metric_vocabulary_is_complete(self):
        self.assertEqual(set(COMMERCE_METRIC_DEFINITIONS),
                         {"sold_quantity", "sales_amount", "weighted_avg_paid_price",
                          "product_gross_profit_reference",
                          "product_gross_margin_reference",
                          "erp_gross_profit_reference"})

    def test_request_rejects_out_of_range_windows_and_unknown_metrics(self):
        from pydantic import ValidationError

        with self.assertRaises(ValidationError):
            _request(end="2020-01-01")
        with self.assertRaises(ValidationError):
            _request(start="2020-01-01",
                     end=(date(2020, 1, 1) + timedelta(days=MAX_SPAN_DAYS + 2)).isoformat())
        with self.assertRaises(ValidationError):
            _request(metrics=["net_profit"])
        with self.assertRaises(ValidationError):
            _request(metrics=[])

    def test_request_requires_explicit_profit_basis_and_one_selector(self):
        from pydantic import ValidationError

        with self.assertRaises(ValidationError):
            _request(metrics=["product_gross_profit_reference"])
        with self.assertRaises(ValidationError):
            _request(product={"text": "A", "ref": "ent-00000000"})
        with self.assertRaises(ValidationError):
            _request(product={})
        with self.assertRaises(ValidationError):
            _request(scope={"mode": "selected"})
        with self.assertRaises(ValidationError):
            _request(scope={"platforms": ["taobao"]},
                     profit_basis="existing_fields",
                     metrics=["erp_gross_profit_reference"])
        with self.assertRaises(ValidationError):
            _request(scope={"shop_refs": ["S1"]})
        with self.assertRaises(ValidationError):
            _request(opportunity_policy_ref="drop table")
        _request(scope={"platforms": ["fxg"]})

    def test_report_kind_pairing_is_refused_not_downgraded(self):
        """Task 8 之后 comparison 已受支持，但**入参契约必须配对**。

        拿商品请求去跑对比报告会发出一份"看起来是对比表"的商品表；未登记的报告种类
        也一样：两者都是显式拒绝，不是降级成商品报告。
        """
        with self.assertRaises(UnsupportedReportKind):
            analyze_as_comparison()
        with self.assertRaises(UnsupportedReportKind):
            run_with_kind("listing_price_audit")

    def test_termination_codes_stay_inside_the_existing_table(self):
        """本轮不新增终止原因码：新增就要重声明 SQL CHECK 并推部署顺序硬约束。"""
        from bi_agent.commerce.graph import _TERMINATION_BY_CODE

        self.assertTrue(set(_TERMINATION_BY_CODE.values()) <= set(TERMINATION_REASONS))

    def test_commerce_columns_do_not_collide_with_fixed_metrics(self):
        from bi_agent.commerce.metrics import COMMERCE_RESULT_COLUMNS
        from bi_agent.metrics import METRIC_DEFINITIONS

        self.assertFalse(COMMERCE_RESULT_COLUMNS & set(METRIC_DEFINITIONS))

    def test_public_limitations_and_patterns_are_registered(self):
        """披露文本要么在固定集合里，要么有模式：否则真实报告会在校验处被拒。"""
        from bi_agent.commerce.graph import _COMMERCE_CODE_BY_FRAGMENT
        from bi_agent.commerce.metrics import (
            COMMERCE_LIMITATION_CODES, COMMERCE_LIMITATION_PATTERNS,
            COMMERCE_PUBLIC_LIMITATIONS)
        from bi_agent.runtime import models

        self.assertTrue(COMMERCE_LIMITATION_CODES <= models._LIMITATION_CODES)
        self.assertTrue(COMMERCE_PUBLIC_LIMITATIONS <= models._PUBLIC_LIMITATIONS)
        self.assertTrue(set(COMMERCE_LIMITATION_CODES)
                        <= {code for _fragment, code in _COMMERCE_CODE_BY_FRAGMENT})
        for fragment, code in _COMMERCE_CODE_BY_FRAGMENT:
            self.assertIn(code, COMMERCE_LIMITATION_CODES, fragment)
        # 每条参数化披露都要有对应模式，逐条试一句真实文本。
        samples = {
            "commerce_scope_excluded": "2 家获准店铺未列入本次合计，原因见 excluded_scope",
            "cost_coverage_incomplete": "成本覆盖不全：1/2 行有行成本，未发布完整商品毛利参考",
            "erp_document_coverage_incomplete":
                "ERP 单据毛利覆盖不全：1/2 张单据带毛利字段，未发布单据毛利参考",
            "erp_document_granularity":
                "ERP 单据集合含 3 张拆单子单与 1 张合单，与平台订单不是一对一",
            "trend_zero_days": "七日趋势含 2 天真实零成交，与缺失日分列",
            "product_zero_rows": "1 家店铺在本轮窗口内没有该商品的成交行，按真实 0 计入",
        }
        for code, text in samples.items():
            self.assertTrue(
                any(pattern.fullmatch(text)
                    for pattern in models._PUBLIC_LIMITATION_PATTERNS), text)
            self.assertIn(code, COMMERCE_LIMITATION_CODES)


def analyze_as_comparison() -> None:
    """把商品请求送进对比报告种类：报告种类与入参不配对，应当被显式拒绝。"""
    run_with_kind("comparison")


def run_with_kind(report_kind: str) -> None:
    from bi_agent.commerce.graph import run_commerce_graph

    context = DomainContext(
        subject_id="s", allowed_shop_ids=frozenset({"S1"}),
        shop_refs={ref_for_key("shop", "S1"): "S1"}, conn=None, store=None,
        chat_id=uuid4(), user_message_id=uuid4(), root_request_id=uuid4(),
        now=NOW, deadline=time.monotonic() + 30)
    run_commerce_graph(report_kind=report_kind, request=_request(),
                       context=context, tool_call_id="c")


if __name__ == "__main__":
    unittest.main()
