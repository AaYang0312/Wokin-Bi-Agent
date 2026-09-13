"""Task 8：平台 / 店铺比较与图表契约。

计划 Task 8 点名的用例一次到位：五平台含一缺失、一口径不一致、平台下钻、
图表与数据集引用失配；另加本轮新增的边界：分组完整性（不完整分组不发布数字）、
合计与排名只覆盖完整同口径集合、缺失日在趋势里留 null、必需 Artifact 保存失败
与可选图表保存失败的不同处置、以及"一次集合查询服务全部分组"（不逐店循环）。

真实测试库跑法与既有约定一致：管理员连接 + 外层事务回滚，只写合成店铺。
无 DSN 时显式 skip——skip 不是通过证明。
"""

from __future__ import annotations

import json
import os
import time
import unittest
from datetime import date, datetime, timedelta
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

import psycopg

from bi_agent.catalog import ref_for_key
from bi_agent.commerce.graph import UnsupportedReportKind, run_commerce_graph
from bi_agent.commerce.models import (
    DomainContext, PerformanceComparisonRequest, ProductPerformanceRequest)
from bi_agent.commerce.tool import compare_performance
from bi_agent.data_quality import QUALITY_RULE
from bi_agent.presentation.charts import (
    ChartContractError, PersistedDataset, build_chart_spec)
from bi_agent.metrics import Coverage
from bi_agent.runtime.artifacts import ChartPairingError
from bi_agent.runtime.domain_registry import COMMERCE_NODES
from bi_agent.runtime.memory import MemoryQueryRunStore
from bi_agent.runtime.models import NewArtifact, NewQueryRun, validate_artifact_payload

from .dbfixtures import connect_test_db

BEIJING = ZoneInfo("Asia/Shanghai")
NOW = datetime(2026, 9, 11, 12, tzinfo=BEIJING)
ALL_CAPABILITIES = ("paid_amount", "paid_orders", "erp_documents", "aov", "quantity",
                    "product_paid_amount", "refund_amount", "cash_difference",
                    "cohort_refund_rate")
# 拼多多只保留已核验的单据能力：支付类指标永久解析不通（2026-09-12 决定，不接入）。
PDD_CAPABILITIES = ("erp_documents",)
WINDOW_START = datetime(2026, 8, 28, tzinfo=BEIJING)
WINDOW_END = datetime(2026, 9, 12, tzinfo=BEIJING)
# 这四个平台的支付口径签名完全相同（同一交易通道、未逐店对照），所以它们之间可以
# 比较与排名；抖音是 `certified`、淘系是 `disproved`，两者都会把集合判成不可比。
COMPARABLE_PLATFORMS = ("jd", "kuaishou", "wxsph", "wsxc")


def _request(**overrides: object) -> PerformanceComparisonRequest:
    base: dict[str, object] = {
        "scope": {"mode": "all_authorized"},
        "start": "2026-09-01", "end": "2026-09-08",
        "group_by": "platform",
        "metrics": ["sold_quantity", "sales_amount", "weighted_avg_paid_price"],
        "sales_basis": "erp_effective_parent", "profit_basis": "none",
        "trend_days": 7,
    }
    base.update(overrides)
    return PerformanceComparisonRequest.model_validate(base)


def _product_request(**overrides: object) -> ProductPerformanceRequest:
    base: dict[str, object] = {
        "product": {"text": "直钉枪"},
        "start": "2026-09-01", "end": "2026-09-08",
        "metrics": ["sales_amount"],
    }
    base.update(overrides)
    return ProductPerformanceRequest.model_validate(base)


def _payload_for(result, artifact_type: str) -> dict:
    """按 Artifact 类型取公开载荷：一次对比会发多份，断言必须指名要哪一份。"""
    matches = [artifact.public_payload for artifact in result.artifacts
               if artifact.ref.type == artifact_type]
    assert len(matches) == 1, ([artifact.ref.type for artifact in result.artifacts],
                               artifact_type)
    return matches[0]


def _charts(result) -> list[dict]:
    return [artifact.public_payload for artifact in result.artifacts
            if artifact.ref.type == "chart_spec"]


def _group_rows(dataset: dict) -> list[dict]:
    """带分组键的行（合计行不带，形状本身就把两者分开）。"""
    return [row for row in dataset["data"]
            if "shop_ref" in row or "platform" in row]


class _ChartStoreFails(MemoryQueryRunStore):
    """只让 chart_spec 写入失败：必需数据集仍要能发布，才能单独看这一条降级路径。"""

    def save_artifact(self, run_id, artifact):  # noqa: ANN001
        if artifact.artifact_type == "chart_spec":
            raise RuntimeError("simulated chart failure")
        return super().save_artifact(run_id, artifact)


class _CountingConn:
    """记录每条 SQL：跨店对比必须是一条集合查询，不是每店一条。"""

    def __init__(self, conn) -> None:  # noqa: ANN001
        self.conn = conn
        self.sql: list[str] = []

    def execute(self, sql, params=None):  # noqa: ANN001
        self.sql.append(" ".join(str(sql).split()))
        return self.conn.execute(sql, params) if params is not None \
            else self.conn.execute(sql)

    def __getattr__(self, name):  # noqa: ANN001
        return getattr(self.conn, name)


class ComparisonRequestContractTests(unittest.TestCase):
    """入参契约：分组维度和范围形状先成立，图上的完整性判定才有意义。"""

    def test_platform_grouping_defaults_to_all_authorized(self):
        request = _request()
        self.assertEqual(request.group_column, "platform")
        self.assertEqual(request.group_label, "platform")
        self.assertEqual(request.normalized()["report_kind"], "comparison")
        self.assertEqual(request.normalized()["group_by"], "platform")
        self.assertEqual(request.previous_window, None,
                         "本轮对比不做上期环比：没有取过上期数就不给差额")

    def test_shop_grouping_requires_exactly_one_platform(self):
        with self.assertRaises(Exception):
            _request(group_by="shop")            # 没选平台
        with self.assertRaises(Exception):
            _request(group_by="shop", scope={"platforms": ["jd", "fxg"]})
        _request(group_by="shop", scope={"platforms": ["jd"]})

    def test_platform_and_shop_refs_cannot_be_combined(self):
        """交集会把"不在该平台"的引用静默剔掉，而分组完整性依赖"这组本来有几家"。"""
        with self.assertRaises(Exception):
            _request(scope={"mode": "selected", "platforms": ["jd"],
                            "shop_refs": [ref_for_key("shop", "S1")]})

    def test_unknown_platform_metric_and_profit_basis_are_refused(self):
        for bad in ({"scope": {"platforms": ["taobao"]}},
                    {"metrics": ["gmv"]},
                    {"metrics": ["product_gross_profit_reference"]}):
            with self.subTest(bad=bad):
                with self.assertRaises(Exception):
                    _request(**bad)

    def test_trend_window_is_its_own_seven_days(self):
        request = _request(start="2026-09-06", end="2026-09-08")
        self.assertEqual(request.trend_window, (date(2026, 9, 1), date(2026, 9, 8)),
                         "主期间只有两天时趋势窗口仍然独立标七天")

    def test_comparison_report_kind_refuses_the_product_request(self):
        """报告种类与入参契约必须配对：拿商品请求跑对比会发出一份假对比表。"""
        context = DomainContext(
            subject_id="s", allowed_shop_ids=frozenset({"S1"}),
            shop_refs={ref_for_key("shop", "S1"): "S1"}, conn=None, store=None,
            chat_id=uuid4(), user_message_id=uuid4(), root_request_id=uuid4(),
            now=NOW, deadline=time.monotonic() + 30)
        with self.assertRaises(UnsupportedReportKind):
            run_commerce_graph(report_kind="comparison",
                               request=_product_request(), context=context,
                               tool_call_id="c")
        with self.assertRaises(UnsupportedReportKind):
            run_commerce_graph(report_kind="audit", request=_request(),
                               context=context, tool_call_id="c")


class ChartSpecContractTests(unittest.TestCase):
    """图表契约：只引用已落库数据集，且任何字段都不携带可执行内容。"""

    def setUp(self):
        self.coverage = Coverage(status="partial", start=date(2026, 9, 1),
                                 end=date(2026, 9, 8),
                                 gaps=["2026-09-03~2026-09-04"])
        self.dataset = PersistedDataset(
            artifact_id=uuid4(), artifact_type="comparison_table",
            data_as_of=datetime(2026, 9, 8, tzinfo=BEIJING), coverage=self.coverage)

    def _spec(self, **overrides):
        arguments = dict(kind="bar", x="platform", y="sales_amount",
                         series=("platform",), unit="CNY",
                         metric_basis="sales_amount|platform_payment/v1|pay_time",
                         coverage_ref=self.dataset)
        arguments.update(overrides)
        return build_chart_spec(self.dataset, **arguments)

    def test_bar_chart_declares_zero_baseline_and_persisted_dataset(self):
        spec = self._spec()
        payload = spec.as_payload()
        self.assertEqual(payload["baseline"], "zero",
                         "条形图必须零基线：非零基线是在拿轴长编故事")
        self.assertEqual(payload["null_values"], "blank")
        self.assertEqual(payload["dataset_ref"], str(self.dataset.artifact_id))
        self.assertEqual(payload["coverage_ref"], str(self.dataset.artifact_id))
        self.assertEqual(payload["dataset_type"], "comparison_table")
        self.assertEqual(payload["coverage_status"], "partial")
        self.assertEqual(payload["coverage_gaps"], ["2026-09-03~2026-09-04"])
        self.assertEqual(validate_artifact_payload(payload, "chart_spec"), payload)

    def test_line_chart_declares_break_and_requires_a_time_axis(self):
        spec = self._spec(kind="line", x="day", unit="piece", y="sold_quantity",
                          metric_basis="sold_quantity|platform_payment/v1|pay_time")
        self.assertEqual(spec.null_values, "break", "缺失日必须断开，不连成一条直线")
        with self.assertRaises(ChartContractError) as error:
            self._spec(kind="line", x="platform")
        self.assertEqual(error.exception.reason, "chart_kind_axis_mismatch")
        with self.assertRaises(ChartContractError) as error:
            self._spec(kind="bar", x="day")
        self.assertEqual(error.exception.reason, "chart_kind_axis_mismatch")

    def test_unit_and_basis_must_match_the_plotted_metric(self):
        for bad, reason in (
                (dict(unit="piece"), "chart_unit_mismatch"),
                (dict(metric_basis="sold_quantity|platform_payment/v1|pay_time"),
                 "chart_basis_metric_mismatch"),
                (dict(metric_basis="drop table"), "chart_basis_invalid"),
                (dict(y="gmv"), "chart_axis_column_invalid"),
                (dict(x="display_name"), "chart_axis_column_invalid"),
                (dict(series=("paid_amount",)), "chart_series_column_invalid"),
                (dict(kind="pie"), "chart_kind_unsupported")):
            with self.subTest(reason=reason):
                with self.assertRaises(ChartContractError) as error:
                    self._spec(**bad)
                self.assertEqual(error.exception.reason, reason)

    def test_coverage_reference_must_be_the_same_persisted_version(self):
        other = PersistedDataset(artifact_id=uuid4(),
                                 artifact_type="comparison_table",
                                 data_as_of=self.dataset.data_as_of,
                                 coverage=self.dataset.coverage)
        with self.assertRaises(ChartContractError) as error:
            self._spec(coverage_ref=other)
        self.assertEqual(error.exception.reason, "chart_coverage_version_mismatch")
        stale = PersistedDataset(artifact_id=self.dataset.artifact_id,
                                 artifact_type="comparison_table",
                                 data_as_of=self.dataset.data_as_of - timedelta(days=1),
                                 coverage=self.dataset.coverage)
        with self.assertRaises(ChartContractError) as error:
            self._spec(coverage_ref=stale)
        self.assertEqual(error.exception.reason, "chart_coverage_version_mismatch")

    def test_chart_cannot_reference_a_non_dataset_artifact(self):
        with self.assertRaises(ChartContractError) as error:
            PersistedDataset(artifact_id=uuid4(), artifact_type="chart_spec",
                             data_as_of=self.dataset.data_as_of,
                             coverage=self.dataset.coverage)
        self.assertEqual(error.exception.reason, "chart_dataset_type_invalid")

    def test_no_field_accepts_executable_text(self):
        payload = self._spec().as_payload()
        for key, value in list(payload.items()):
            if isinstance(value, str):
                self.assertNotIn("<", value, key)
                self.assertNotIn("(", value, key)
                self.assertNotIn(";", value, key)
        for injection in ("<script>alert(1)</script>", "javascript:alert(1)",
                           "() => import(1)"):
            with self.subTest(value=injection[:12]):
                with self.assertRaises(ChartContractError):
                    self._spec(metric_basis=injection)
                with self.assertRaises(Exception):
                    validate_artifact_payload({**payload, "x": injection}, "chart_spec")


@unittest.skipUnless(os.getenv("BI_TEST_ADMIN_DSN"), "未配置独立测试数据库")
class ComparisonGraphTests(unittest.TestCase):
    """真实库上的对比图：合成多平台店铺，一店一条集合查询。"""

    def setUp(self):
        self.conn = connect_test_db(self)
        self.tag = uuid4().hex[:5].upper()
        self.shops: dict[str, str] = {}

    # -- 夹具 --------------------------------------------------------------

    def _shop(self, key: str, platform: str = "jd",
              capabilities=ALL_CAPABILITIES) -> str:
        shop_id = f"S{self.tag}{key}"
        self.conn.execute(
            "INSERT INTO bi.shops(shop_id, platform, display_name, capabilities) "
            "VALUES (%s, %s, %s, %s) ON CONFLICT (shop_id) DO UPDATE SET "
            "platform = EXCLUDED.platform",
            (shop_id, platform, f"对比测试店{key}", list(capabilities)))
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
        self.conn.execute(
            "INSERT INTO bi.sync_batches(source, entity, shop_id, batch_id, "
            "business_window, window_kind, mode, row_count, business_end) "
            "VALUES (%s, 'orders', %s, %s, tstzrange(%s, %s, '[)'), 'business', "
            "'backfill', 1, %s) ON CONFLICT (source, entity, shop_id, batch_id) "
            "DO UPDATE SET business_window = EXCLUDED.business_window",
            (source, self.shops[shop_key],
             f"t8-{source}-{start:%m%d}-{end:%m%d}", start, end, end))

    def _sale(self, shop_key: str, erp_id: str, *, day: date, quantity: str,
              amount: str, cost: str | None = None, product: str = "P8",
              line_kind: str = "sale", verified: bool = True,
              document_profit: str | None = None,
              source: str = "erp.trade.list.query") -> None:
        """一张 ERP 单据 + 一行商品父行：与 Task 7 同一形状，只是商品号无关紧要。"""
        shop_id = self.shops[shop_key]
        paid_at = datetime(day.year, day.month, day.day, 12, tzinfo=BEIJING)
        self.conn.execute(
            "INSERT INTO bi.orders(shop_id, erp_id, commercial_ids, source, "
            "source_updated_at, paid_at, raw_gross_profit, active, batch_id) "
            "VALUES (%s, %s, %s, %s, now(), %s, %s, true, 't8-seed') "
            "ON CONFLICT (shop_id, erp_id) DO NOTHING",
            (shop_id, erp_id, [f"C{erp_id}"], source, paid_at, document_profit))
        self.conn.execute(
            "INSERT INTO bi.order_items(shop_id, erp_id, line_id, commercial_id, "
            "product_id, sku_id, paid_at, quantity, raw_unit_cost, "
            "allocated_paid_amount, allocation_verified, line_kind, active) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, true) "
            "ON CONFLICT (shop_id, erp_id, line_id) DO NOTHING",
            (shop_id, erp_id, f"{erp_id}-L1", f"C{erp_id}",
             f"{self.tag}{product}", f"{self.tag}{product}-sku", paid_at, quantity,
             cost, amount, verified, line_kind))

    def _context(self, *, allowed: frozenset[str] | None = None,
                 store: MemoryQueryRunStore | None = None,
                 conn: object = None) -> DomainContext:
        allowed = allowed or frozenset(self.shops.values())
        refs = {shop: ref_for_key("shop", shop) for shop in allowed}
        return DomainContext(
            subject_id=f"t8-{self.tag}", allowed_shop_ids=allowed, shop_refs=refs,
            conn=conn if conn is not None else self.conn,
            store=store or MemoryQueryRunStore(forbidden_values=set(allowed)),
            chat_id=UUID(int=1), user_message_id=UUID(int=2),
            root_request_id=UUID(int=3), now=NOW,
            deadline=time.monotonic() + 30, attempt_no=1)

    def _run(self, *, store: MemoryQueryRunStore | None = None,
             allowed: frozenset[str] | None = None, conn: object = None,
             **request_overrides: object):
        return compare_performance(_request(**request_overrides),
                                   self._context(store=store, allowed=allowed,
                                                 conn=conn))

    def _five_platforms(self, missing: str = "pdd") -> None:
        """四个可比平台 + 一个回答不了支付口径的平台。"""
        for index, platform in enumerate(COMPARABLE_PLATFORMS):
            key = str(index + 1)
            self._shop(key, platform=platform)
            self._cover(key, WINDOW_START, WINDOW_END)
            self._sale(key, f"E{index}", day=date(2026, 9, 2),
                       quantity=str(index + 1), amount=str(100 * (index + 1)))
        self._shop("5", platform=missing,
                   capabilities=PDD_CAPABILITIES if missing == "pdd"
                   else ALL_CAPABILITIES)
        self._cover("5", WINDOW_START, WINDOW_END,
                    source="erp.trade.outstock.simple.query")
        self._sale("5", "E9", day=date(2026, 9, 2), quantity="7", amount="700",
                   source="erp.trade.outstock.simple.query")

    # -- 1. 五平台含一缺失 -------------------------------------------------

    def test_five_platform_overview_publishes_only_complete_comparable_groups(self):
        self._five_platforms()
        result = self._run()
        payload = result.model_payload
        self.assertEqual(result.status.value, "partial", payload.get("limitations"))
        table = _payload_for(result, "comparison_table")
        rows = {row["platform"]: row for row in _group_rows(table)}
        self.assertEqual(sorted(rows), sorted(COMPARABLE_PLATFORMS),
                         "回答不了本口径的平台整组不发布，不给它一个 0")
        self.assertEqual(rows["jd"]["sold_quantity"], "1")
        self.assertEqual(rows["wsxc"]["sold_quantity"], "4")
        self.assertEqual(rows["wsxc"]["sales_amount"], "400")
        # 均价从合并后的总额重算：(100+200+300+400)/(1+2+3+4)=100，不是四个均价的平均。
        total = [row for row in table["data"]
                 if "platform" not in row and "shop_ref" not in row]
        self.assertEqual(len(total), 1, "合计行唯一且不带分组键")
        self.assertEqual(total[0]["sold_quantity"], "10")
        self.assertEqual(total[0]["sales_amount"], "1000")
        self.assertEqual(total[0]["weighted_avg_paid_price"], "100")
        missing = {item["shop_ref"]: item["reason"]
                   for item in payload["excluded_scope"]}
        # 拼多多的支付缺口按注册表归因到"出库通道的付款时间口径实测不成立"：
        # 说成"去开通能力"会把人引向一条永远不会完成的对账路（Task 7 同一判法）。
        self.assertEqual(missing, {ref_for_key("shop", self.shops["5"]):
                                   "coverage_time_basis_unverified"})
        groups = {item["platform"]: item for item in payload["group_statuses"]
                  if "platform" in item}
        self.assertEqual(groups["pdd"]["status"], "partial")
        self.assertEqual(groups["pdd"]["shops_evaluated"], 0)
        self.assertEqual(payload["evaluated_scope"]["platforms"],
                         sorted(COMPARABLE_PLATFORMS),
                         "已发布完整分组按平台码定序：与 rows 的分组顺序同一规则")
        self.assertNotIn(self.shops["5"], json.dumps(payload, ensure_ascii=False),
                         "被排除店铺的引用不进 evaluated 侧的任何清单")

    def test_ranking_covers_exactly_the_published_groups(self):
        self._five_platforms()
        payload = self._run().model_payload
        blocks = {block["metric"]: block for block in payload["ranking"]}
        sales = blocks["sales_amount"]
        self.assertEqual(sales["status"], "complete")
        self.assertEqual(sales["ranking_scope"], "evaluated_only")
        self.assertEqual(sales["basis"], "platform_payment/v1")
        self.assertEqual([(row["rank"], row["platform"], row["value"])
                          for row in sales["rows"]],
                         [(1, "wsxc", "400"), (2, "wxsph", "300"),
                          (3, "kuaishou", "200"), (4, "jd", "100")],
                         "名次只给同口径且有值的分组，缺值不占位也不当 0 排")
        self.assertNotIn("pdd", json.dumps(sales, ensure_ascii=False),
                         "缺失分组不进排名集合，它在 excluded_scope 与 group_statuses 里")

    def test_incomplete_platform_group_publishes_no_group_number(self):
        """一个平台三家店、一家缺覆盖：整组不发布，缺的那家店仍按原因列出。"""
        for key in ("1", "2", "3"):
            self._shop(key, platform="jd")
            self._cover(key, WINDOW_START, WINDOW_END)
            self._sale(key, f"E{key}", day=date(2026, 9, 2), quantity="1",
                       amount="100")
        self.conn.execute(
            "UPDATE bi.sync_state SET covered = "
            "tstzmultirange(tstzrange('2026-08-28 00:00+08','2026-09-02 00:00+08','[)'))"
            " WHERE shop_id = %s AND entity = 'orders'",
            (self.shops["3"],))

        result = self._run(metrics=["sales_amount"])
        payload = result.model_payload
        self.assertEqual(result.status.value, "missing_data", payload)
        self.assertEqual(result.artifacts, [],
                         "组内缺一家就不发这个平台的数：两家的和被读成整个平台是错话")
        self.assertTrue(any("不发布该分组数字：jd" in text
                            for text in payload["limitations"]), payload["limitations"])
        self.assertTrue(any("未列入本次合计" in text
                            for text in payload["limitations"]), payload["limitations"],
                        )
        # 下钻到店铺粒度：已评估的两家店照给，缺的那家照缺。
        drilled = self._run(group_by="shop", metrics=["sales_amount"],
                            scope={"mode": "all_authorized", "platforms": ["jd"]})
        rows = {row["shop_ref"] for row in _group_rows(
            _payload_for(drilled, "comparison_table"))}
        self.assertEqual(rows, {ref_for_key("shop", self.shops["1"]),
                                ref_for_key("shop", self.shops["2"])})
        self.assertEqual({item["shop_ref"]: item["reason"]
                          for item in drilled.model_payload["excluded_scope"]},
                         {ref_for_key("shop", self.shops["3"]): "coverage_incomplete"})

    # -- 2. 一口径不一致 ---------------------------------------------------

    def test_mixed_basis_groups_stay_visible_but_share_no_total_or_ranking(self):
        """抖音（已认证支付窗口）与未逐店对照的平台同名不同认证：不给合计也不排名。"""
        for index, platform in enumerate(COMPARABLE_PLATFORMS):
            key = str(index + 1)
            self._shop(key, platform=platform)
            self._cover(key, WINDOW_START, WINDOW_END)
            self._sale(key, f"E{index}", day=date(2026, 9, 2), quantity="1",
                       amount=str(100 * (index + 1)))
        self._shop("9", platform="fxg")
        self._cover("9", WINDOW_START, WINDOW_END)
        self._sale("9", "E9", day=date(2026, 9, 2), quantity="5", amount="900")

        result = self._run(metrics=["sales_amount"])
        payload = result.model_payload
        table = _payload_for(result, "comparison_table")
        rows = {row["platform"]: row for row in _group_rows(table)}
        self.assertEqual(sorted(rows), sorted([*COMPARABLE_PLATFORMS, "fxg"]))
        self.assertEqual(rows["fxg"]["sales_amount"], "900",
                         "单组自己的数字照发：不可比抹掉的是汇总与排名，不是各行")
        total = [row for row in table["data"] if "platform" not in row][0]
        self.assertIsNone(total["sales_amount"], "混口径的集合没有合计")
        block = {item["metric"]: item for item in payload["ranking"]}["sales_amount"]
        self.assertEqual(block["status"], "incomparable")
        self.assertEqual(block["reason"], "basis_incompatible")
        self.assertTrue(all(row["rank"] is None for row in block["rows"]))
        self.assertNotIn("basis", block, "不可比时不再贴一个口径名，那会把多口径说成一口径")
        self.assertTrue(any("口径互不兼容" in text
                            for text in payload["limitations"]), payload["limitations"])
        self.assertEqual(result.status.value, "partial")

    def test_tb_and_tm_stay_two_groups_without_a_merge_rule(self):
        """显式分组规则：本轮没有版本化合并规则，淘宝与天猫不并成一个"淘系"组。"""
        for key, platform in (("1", "tb"), ("2", "tm")):
            self._shop(key, platform=platform)
            self._cover(key, WINDOW_START, WINDOW_END,
                        source="erp.trade.outstock.simple.query")
            self._sale(key, f"E{key}", day=date(2026, 9, 2), quantity="1",
                       amount="100", document_profit="30",
                       source="erp.trade.outstock.simple.query")

        result = self._run(metrics=["erp_gross_profit_reference"],
                           profit_basis="existing_fields")
        payload = result.model_payload
        table = _payload_for(result, "comparison_table")
        rows = {row["platform"]: row for row in _group_rows(table)}
        self.assertEqual(sorted(rows), ["tb", "tm"])
        self.assertEqual([rows[key]["erp_gross_profit_reference"]
                          for key in ("tb", "tm")], ["30", "30"])
        total = [row for row in table["data"] if "platform" not in row][0]
        self.assertEqual(total["erp_gross_profit_reference"], "60",
                         "同一注册表通道内仍可合计：单据口径两边签名一致")
        self.assertTrue(any("不并成一个淘系组" in text
                            for text in payload["limitations"]), payload["limitations"])
        self.assertNotIn("taoxi", json.dumps(payload, ensure_ascii=False))

    # -- 3. 平台下钻：单平台多店 ------------------------------------------

    def test_one_platform_shop_drilldown_keeps_window_and_basis(self):
        self._shop("1", platform="fxg")
        self._shop("2", platform="fxg")
        self._shop("3", platform="jd")
        for key in ("1", "2", "3"):
            self._cover(key, WINDOW_START, WINDOW_END)
        self._sale("1", "E1", day=date(2026, 9, 2), quantity="1", amount="100",
                   cost="40")
        self._sale("2", "E2", day=date(2026, 9, 3), quantity="9", amount="450",
                   cost="30")
        self._sale("3", "E3", day=date(2026, 9, 2), quantity="8", amount="800")

        result = self._run(
            group_by="shop", scope={"mode": "all_authorized", "platforms": ["fxg"]},
            start="2026-09-01", end="2026-09-05",
            metrics=["sold_quantity", "sales_amount", "weighted_avg_paid_price"])
        payload = result.model_payload
        table = _payload_for(result, "comparison_table")
        refs = {row["shop_ref"] for row in _group_rows(table)}
        self.assertEqual(refs, {ref_for_key("shop", self.shops["1"]),
                                ref_for_key("shop", self.shops["2"])},
                         "下钻只回到本平台的两家店：第三个平台的数字不进本轮")
        # 请求的窗口与口径原样保留：下钻不是"顺手改成近 30 天"。
        self.assertEqual(payload["filters"]["start"], "2026-09-01")
        self.assertEqual(payload["filters"]["end"], "2026-09-05")
        self.assertEqual(payload["filters"]["sales_basis"], "erp_effective_parent")
        self.assertEqual(payload["filters"]["group_by"], "shop")
        self.assertEqual(payload["requested_scope"], {"mode": "all_authorized",
                                                     "platforms": ["fxg"]})
        total = [row for row in table["data"] if "shop_ref" not in row][0]
        self.assertEqual(total["weighted_avg_paid_price"], "55",
                         "两店均价 100/50 不能被平均成 75")

    def test_drilldown_rereads_authorization_and_refuses_out_of_scope(self):
        self._shop("1", platform="fxg")
        other = self._shop("2", platform="fxg")
        self._cover("1", WINDOW_START, WINDOW_END)
        self._cover("2", WINDOW_START, WINDOW_END)
        self._sale("1", "E1", day=date(2026, 9, 2), quantity="1", amount="100")
        self._sale("2", "E2", day=date(2026, 9, 2), quantity="2", amount="200")

        allowed = frozenset({self.shops["1"]})
        result = compare_performance(
            _request(group_by="shop", scope={"mode": "all_authorized",
                                             "platforms": ["fxg"]}),
            self._context(allowed=allowed))
        payload = result.model_payload
        self.assertEqual([row["shop_ref"] for row in _group_rows(_payload_for(
            result, "comparison_table"))], [ref_for_key("shop", self.shops["1"])])
        self.assertNotIn(other, json.dumps(payload, ensure_ascii=False))
        self.assertNotIn(ref_for_key("shop", other), json.dumps(payload),
                         "不获准的店铺连引用都不出现在下钻结果里")

        forbidden = compare_performance(
            _request(group_by="platform",
                     scope={"mode": "selected",
                           "shop_refs": [ref_for_key("shop", other)]}),
            self._context(allowed=allowed))
        self.assertEqual(forbidden.status.value, "failed")
        self.assertEqual(forbidden.error.code, "forbidden")
        self.assertEqual(forbidden.model_payload, {"status": "failed"})

    def test_grouping_uses_one_set_query_no_matter_how_many_shops(self):
        """一次集合查询服务全部分组：查询条数不随店铺数线性增长（spec §2）。"""
        self._five_platforms()
        counting = _CountingConn(self.conn)
        store = MemoryQueryRunStore(forbidden_values=set(self.shops.values()))
        single = compare_performance(_request(metrics=["sales_amount"]),
                                     self._context(conn=counting, store=store))
        self.assertIn(single.status.value, {"success", "partial"},
                      single.model_payload.get("limitations"))
        group_queries = [sql for sql in counting.sql if "v_product_cost_daily" in sql]
        self.assertEqual(len(group_queries), 1,
                         f"分组面必须只有一条集合查询：{group_queries}")
        self.assertIn("GROUP BY shop_id, day, line_kind", group_queries[0])
        self.assertIn("shop_id = ANY(", group_queries[0],
                      "店铺集合只能作为一条数组参数进去，不能逐店拼 SQL")

    # -- 4. 趋势缺口与图表 -------------------------------------------------

    def test_trend_null_gaps_break_the_line_and_zero_days_stay_real(self):
        self._shop("1", platform="jd")
        self._shop("2", platform="kuaishou")
        for key in ("1", "2"):
            self._cover(key, WINDOW_START, WINDOW_END)
        self._sale("1", "E1", day=date(2026, 9, 6), quantity="1", amount="100")
        self._sale("2", "E2", day=date(2026, 9, 6), quantity="2", amount="200")
        # 只在趋势窗口内、主期间外挖一个洞：主期间仍完整，缺的那天必须留 null。
        self.conn.execute(
            "UPDATE bi.sync_state SET covered = "
            "tstzmultirange(tstzrange('2026-08-28 00:00+08','2026-09-04 00:00+08','[)'),"
            " tstzrange('2026-09-05 00:00+08','2026-09-12 00:00+08','[)')) "
            "WHERE shop_id = %s AND entity = 'orders'",
            (self.shops["2"],))

        result = self._run(start="2026-09-05", end="2026-09-08",
                           metrics=["sold_quantity", "sales_amount"])
        trend = _payload_for(result, "trend_series")
        rows = {(row["platform"], row["day"]): row for row in trend["data"]}
        self.assertEqual(trend["coverage"]["status"], "partial")
        self.assertIn("2026-09-04~2026-09-05", trend["coverage"]["gaps"])
        self.assertIsNone(rows[("kuaishou", "2026-09-04")]["sold_quantity"],
                          "覆盖缺的一天必须是 null：给 0 就等于说那天没卖")
        self.assertEqual(rows[("jd", "2026-09-04")]["sold_quantity"], "0",
                         "覆盖成立而当天无成交：那是真实 0")
        self.assertEqual(rows[("jd", "2026-09-06")]["sold_quantity"], "1")
        self.assertTrue(any("缺失日按 null" in text for text in trend["limitations"]),
                        trend["limitations"])
        # 行必须按"逐分组 × 逐日"发：前端按 series 分列画多条线，靠的就是这一形状。
        self.assertEqual([row["platform"] for row in trend["data"]],
                         ["jd"] * 7 + ["kuaishou"] * 7)
        self.assertEqual({row["day"] for row in trend["data"]},
                         {f"2026-09-0{day}" for day in range(1, 8)})
        line = [chart for chart in _charts(result) if chart["kind"] == "line"]
        self.assertTrue(line, "趋势数据集要有折线图引用")
        self.assertEqual(line[0]["null_values"], "break")
        self.assertEqual(line[0]["dataset_type"], "trend_series")
        # series 必须点名分组列：只声明 x=day 的话，两个平台会被连成一条假线。
        self.assertEqual(line[0]["x"], "day")
        self.assertEqual(line[0]["series"], ["platform"])

    def test_chart_artifacts_reference_the_persisted_table_and_match_its_rows(self):
        self._five_platforms()
        store = MemoryQueryRunStore(forbidden_values=set(self.shops.values()))
        result = self._run(store=store, metrics=["sales_amount"])
        table_ref = next(artifact.ref for artifact in result.artifacts
                         if artifact.ref.type == "comparison_table")
        charts = _charts(result)
        self.assertEqual(sorted(chart["kind"] for chart in charts), ["bar", "line"],
                         "同一指标的柱图与折图各一张：轴、数据集与缺值处理都不同")
        chart = next(item for item in charts if item["kind"] == "bar")
        self.assertEqual(chart["kind"], "bar")
        self.assertEqual(chart["x"], "platform")
        self.assertEqual(chart["baseline"], "zero")
        self.assertEqual(chart["unit"], "CNY")
        self.assertEqual(chart["currency"], "CNY")
        self.assertEqual(chart["dataset_ref"], str(table_ref.id),
                         "图表只能引用已经落库的那份数据集")
        saved = store.artifacts[table_ref.id]
        self.assertEqual(chart["dataset_data_as_of"],
                         saved["data_as_of"].isoformat(),
                         "图表声明的数据版本必须就是被引用那份的截止时刻")
        table = _payload_for(result, "comparison_table")
        self.assertEqual({row["platform"]: row["sales_amount"]
                          for row in _group_rows(table)},
                         {row["platform"]: row["value"]
                          for row in next(block for block in
                                          result.model_payload["ranking"]
                                          if block["metric"] == "sales_amount")["rows"]},
                         "表、排名与图表引用的是同一批数：三处必须逐项相等")
        pair = next(artifact for artifact in store.artifacts.values()
                    if artifact["artifact_type"] == "chart_spec")
        self.assertEqual(pair["dataset_ref"], table_ref.id)
        self.assertEqual(pair["chart_version"], chart["chart_version"])

    def test_chart_persistence_failure_keeps_the_table(self):
        """spec §7：可选图表存不下只降级成表格，必需结果仍在。"""
        self._five_platforms()
        store = _ChartStoreFails(forbidden_values=set(self.shops.values()))
        result = self._run(store=store, metrics=["sales_amount"])
        self.assertEqual(result.status.value, "partial",
                         "拼多多那一组仍然缺失：降级的是图表，不是范围")
        kinds = sorted(artifact.ref.type for artifact in result.artifacts)
        self.assertEqual(kinds, ["comparison_table", "trend_series"],
                         "图表存不下只拿掉那一张，两份表格都在")
        self.assertTrue(any("只发表格" in text
                            for text in result.model_payload["limitations"]))

    def test_required_dataset_persistence_failure_is_not_reported_as_success(self):
        self._five_platforms()

        class _AllFails(MemoryQueryRunStore):
            def save_artifact(self, run_id, artifact):  # noqa: ANN001
                raise RuntimeError("simulated artifact failure")

        result = self._run(store=_AllFails(
            forbidden_values=set(self.shops.values())), metrics=["sales_amount"])
        self.assertEqual(result.status.value, "failed")
        self.assertEqual(result.error.code, "artifact_persistence_failed")
        self.assertEqual(result.model_payload, {"status": "failed"},
                         "保存失败时连投影好的数字都不还给模型")

    def test_chart_pairing_is_verified_against_persisted_rows(self):
        """引用失配在写库这一侧被拒：换运行、换类型、换版本都过不了。"""
        self._five_platforms()
        store = MemoryQueryRunStore(forbidden_values=set(self.shops.values()))
        result = self._run(store=store, metrics=["sales_amount"])
        run_id = result.run_id
        table = next(artifact for artifact in store.artifacts.values()
                     if artifact["artifact_type"] == "comparison_table")
        coverage = table["coverage"]
        payload_of = next(artifact.public_payload for artifact in result.artifacts
                          if artifact.ref.type == "chart_spec")
        data_as_of = table["data_as_of"]

        def build(**changes):
            fields = {"artifact_type": "chart_spec",
                      "payload": {**payload_of},
                      "data_as_of": data_as_of, "coverage": coverage,
                      "dataset_ref": table["id"], "chart_version": 1}
            fields.update(changes)
            return NewArtifact(**fields)

        with self.assertRaises(ValueError):
            store.save_artifact(run_id, build(
                payload={**payload_of, "dataset_ref": str(uuid4())}),
            )
        other_run = store.create_run(NewQueryRun(**_new_run_fields()))
        with self.assertRaises(ChartPairingError) as error:
            store.save_artifact(other_run, build())
        self.assertEqual(error.exception.reason, "chart_dataset_other_run")
        unknown = uuid4()
        with self.assertRaises(ChartPairingError) as error:
            store.save_artifact(run_id, build(
                dataset_ref=unknown,
                payload={**payload_of, "dataset_ref": str(unknown),
                         "coverage_ref": str(unknown)}))
        self.assertEqual(error.exception.reason, "chart_dataset_unresolved")
        with self.assertRaises(ChartPairingError) as error:
            store.save_artifact(run_id, build(data_as_of=data_as_of
                                              - timedelta(days=1)))
        self.assertEqual(error.exception.reason, "chart_dataset_version_mismatch")
        chart_row = next(artifact for artifact in store.artifacts.values()
                         if artifact["artifact_type"] == "chart_spec")
        # 载荷自称是数据集不算：Store 只认它**查到的那一行**的类型。
        with self.assertRaises(ChartPairingError) as error:
            store.save_artifact(run_id, build(
                dataset_ref=chart_row["id"],
                payload={**payload_of, "dataset_ref": str(chart_row["id"]),
                         "coverage_ref": str(chart_row["id"])}))
        self.assertEqual(error.exception.reason, "chart_dataset_type_invalid")

    # -- 5. 其余边界 -------------------------------------------------------

    def test_pdd_stays_an_explicit_missing_group_without_any_payment_number(self):
        """拼多多：本轮不接入支付，它只作为显式缺失组出现，也不给任何支付数字。"""
        self._shop("1", platform="jd")
        self._shop("2", platform="pdd", capabilities=PDD_CAPABILITIES)
        self._cover("1", WINDOW_START, WINDOW_END)
        self._cover("2", WINDOW_START, WINDOW_END,
                    source="erp.trade.outstock.simple.query")
        self._sale("1", "E1", day=date(2026, 9, 2), quantity="1", amount="100")
        self._sale("2", "E2", day=date(2026, 9, 2), quantity="9", amount="900",
                   source="erp.trade.outstock.simple.query")

        payload = self._run(metrics=["sales_amount"]).model_payload
        excluded = {item["shop_ref"]: item["reason"]
                    for item in payload["excluded_scope"]}
        self.assertEqual(excluded,
                         {ref_for_key("shop", self.shops["2"]):
                          "coverage_time_basis_unverified"})
        self.assertNotIn("900", json.dumps(payload, ensure_ascii=False),
                         "出库金额不能被补成支付销售额参与比较")
        self.assertEqual([row["platform"] for row in
                          _group_rows(_payload_for(
                              self._run(metrics=["sales_amount"]),
                              "comparison_table"))], ["jd"])

    def test_secondary_metric_gap_stays_a_cell_not_a_dropped_group(self):
        """主面可答、次要指标缺能力的分组照发布，缺的那一列留 null 并带原因。

        这正是"多指标能力独立判断"在对比面上的形状：不能答单据毛利的平台仍然
        回答销量，而合计与排名不会因为一个空单元格就说成整套都有数。
        """
        self._shop("1", platform="jd")
        self._shop("2", platform="kuaishou",
                   capabilities=("quantity", "product_paid_amount"))
        self._cover("1", WINDOW_START, WINDOW_END)
        self._cover("2", WINDOW_START, WINDOW_END)
        self._sale("1", "E1", day=date(2026, 9, 2), quantity="1", amount="100",
                   document_profit="30")
        self._sale("2", "E2", day=date(2026, 9, 2), quantity="5", amount="500")

        result = self._run(metrics=["sold_quantity", "erp_gross_profit_reference"],
                           profit_basis="existing_fields")
        payload = result.model_payload
        self.assertEqual(payload.get("excluded_scope", []), [],
                         "有别的指标可答就不整店退出：多指标独立判定")
        table = _payload_for(result, "comparison_table")
        rows = {row["platform"]: row for row in _group_rows(table)}
        self.assertEqual([rows[key]["sold_quantity"] for key in ("jd", "kuaishou")],
                         ["1", "5"])
        self.assertEqual(rows["jd"]["erp_gross_profit_reference"], "30")
        self.assertIsNone(rows["kuaishou"]["erp_gross_profit_reference"],
                          "缺能力的分组不给 0：那一格就是没算出来")
        statuses = {(item.get("platform"), item["metric"]): item["status"]
                    for item in payload["metric_statuses"]}
        self.assertEqual(statuses[("kuaishou", "erp_gross_profit_reference")],
                         "unsupported")
        self.assertEqual(statuses[("kuaishou", "sold_quantity")], "available")
        blocks = {block["metric"]: block for block in payload["ranking"]}
        self.assertEqual(blocks["sold_quantity"]["status"], "complete")
        self.assertEqual(blocks["erp_gross_profit_reference"]["status"], "incomplete")
        total = [row for row in table["data"] if "platform" not in row][0]
        self.assertEqual(total["sold_quantity"], "6")
        self.assertIsNone(total["erp_gross_profit_reference"],
                          "有分组缺值时这一列不发合计：6 与 30 不是同一个集合的答案")

    def test_invalid_comparison_arguments_still_leave_a_needs_input_run(self):
        """参数解析失败也要走图：needs_input 必须留下运行记录与终止原因。

        否则恢复层只能去猜聊天文本（旧 query_business 就是这条契约），而对比 Tool
        与商品 Tool 共用同一个适配器，这条不能只在一种报告上成立。
        """
        from bi_agent.commerce.tool import execute_commerce_tool
        from bi_agent.llm import ToolCall

        self._shop("1", platform="jd")
        store = MemoryQueryRunStore(forbidden_values=set(self.shops.values()))
        execution = execute_commerce_tool(
            ToolCall(id="call_bad", name="compare_performance", arguments=None,
                     arguments_error="arguments不是对象"),
            self._context(store=store))
        result = execution.domain_result
        self.assertEqual(result.status.value, "needs_input")
        self.assertEqual(result.model_payload, {"status": "needs_input"})
        self.assertEqual(result.error.code, "invalid_parameters")
        run = store.runs[result.run_id]
        self.assertEqual(run["termination_reason"], "invalid_parameters")
        self.assertEqual(run["domain"], "commerce_performance")
        self.assertEqual(store.artifacts, {}, "参数没解析出来就没有可发布的结果")

    def test_incomplete_document_coverage_is_its_own_cell_reason(self):
        """单据毛利覆盖不全是**覆盖率**缺口，不能混写成"本轮没有可发布的事实行"。

        两个原因指向完全不同的下一步：一个要回去补单据毛利字段，一个只是那天确实没卖。
        """
        self._shop("1", platform="jd")
        self._shop("2", platform="kuaishou")
        for key in ("1", "2"):
            self._cover(key, WINDOW_START, WINDOW_END)
        self._sale("1", "E1", day=date(2026, 9, 2), quantity="1", amount="100",
                   document_profit=None)
        self._sale("1", "E2", day=date(2026, 9, 3), quantity="1", amount="100",
                   document_profit="30")
        self._sale("2", "E3", day=date(2026, 9, 2), quantity="1", amount="100",
                   document_profit="40")

        result = self._run(metrics=["sold_quantity", "erp_gross_profit_reference"],
                           profit_basis="existing_fields")
        payload = result.model_payload
        table = _payload_for(result, "comparison_table")
        rows = {row["platform"]: row for row in _group_rows(table)}
        self.assertIsNone(rows["jd"]["erp_gross_profit_reference"],
                          "两张单据只有一张带毛利字段：不给这一组的毛利")
        self.assertEqual(rows["kuaishou"]["erp_gross_profit_reference"], "40")
        self.assertEqual(rows["jd"]["erp_documents"], 2, "两份计数照常给，缺口自己可数")
        self.assertEqual(rows["jd"]["erp_documents_with_gross_profit"], 1)
        statuses = {(item.get("platform"), item["metric"]): item
                    for item in payload["metric_statuses"] if "platform" in item}
        jd = statuses[("jd", "erp_gross_profit_reference")]
        self.assertEqual(jd["status"], "missing")
        self.assertEqual(jd["reason"], "erp_document_coverage_incomplete",
                         "原因要指回该补的取数字段，而不是笼统说没有事实")
        block = {item["metric"]: item for item in payload["ranking"]}
        self.assertEqual(block["erp_gross_profit_reference"]["status"], "incomplete")
        self.assertEqual(block["erp_gross_profit_reference"]["missing"],
                         [{"platform": "jd", "reason": "erp_document_coverage_incomplete"}])
        self.assertTrue(all(row["rank"] is None
                            for row in block["erp_gross_profit_reference"]["rows"]))
        total = [row for row in table["data"] if "platform" not in row][0]
        self.assertIsNone(total["erp_gross_profit_reference"])
        self.assertEqual(total["sold_quantity"], "3",
                         "同一份事实里能算的列不受影响：三件销量照合计")
        self.assertEqual(total["erp_documents"], 3, "两份计数也进合计行：缺口的分母看得见")
        self.assertEqual(total["erp_documents_with_gross_profit"], 2,
                         "两家的计数各自完整：合计行给的是 3 张里带毛利的 2 张")

    def test_verified_payment_basis_publishes_the_payment_facet_only(self):
        """`sales_basis=verified_payment` 的对比：支付面单独两列，报告指标不冒充。

        报告指标 `sales_amount` 的定义就是 ERP 有效销售父项口径；把商业支付额填进
        那一列，等于用另一个事实集合顶替同一个名字（spec §5.2）。因此本轮：支付面
        按自己的列发布，请求的指标一律标 `incomparable`，也不出图。
        """
        self._shop("1", platform="jd")
        self._shop("2", platform="kuaishou")
        for key in ("1", "2"):
            self._cover(key, WINDOW_START, WINDOW_END)
        self._sale("1", "E1", day=date(2026, 9, 2), quantity="1", amount="100")
        self._sale("2", "E2", day=date(2026, 9, 2), quantity="2", amount="200")
        for key, commercial, amount in (("1", "C_E1", "100"), ("2", "C_E2", "200")):
            self.conn.execute(
                "INSERT INTO bi.order_payments(shop_id, commercial_id, paid_at, amount,"
                " currency, basis, verified) VALUES (%s, %s, %s, %s, 'CNY', 'items', true)",
                (self.shops[key], commercial, datetime(2026, 9, 2, 12, tzinfo=BEIJING),
                 amount))

        result = self._run(sales_basis="verified_payment", metrics=["sales_amount"])
        payload = result.model_payload
        statuses = {(item.get("shop_ref"), item["metric"]): item["status"]
                    for item in payload["metric_statuses"]}
        self.assertEqual(set(statuses.values()), {"incomparable"},
                         "报告指标在该口径下不可算：不能拿支付额冒充 sales_amount")
        self.assertTrue(all(row.get("sales_amount") is None
                            for row in _payload_for(result, "comparison_table")["data"]),
                        "该口径下商品面一列都不给数：有列而无值，才是不可算的正确形状")
        table = _payload_for(result, "comparison_table")
        payments = {row["platform"]: row.get("paid_amount") for row in _group_rows(table)
                    if "paid_amount" in row}
        self.assertEqual(payments, {"jd": "100", "kuaishou": "200"},
                         "支付面单独一列，列名自己说清口径")
        self.assertEqual(_charts(result), [],
                         "支付面没有登记过单位契约，不进图表轴")
        self.assertTrue(any("本轮没有可画的指标" in text
                            for text in payload["limitations"]), payload["limitations"])

    def test_comparison_reads_work_for_the_app_role(self):
        """整店聚合面也要能在 bi_app 身份下读：新查询不能只管理员跑得通。"""
        self._shop("1", platform="jd")
        self._cover("1", WINDOW_START, WINDOW_END)
        self._sale("1", "E1", day=date(2026, 9, 2), quantity="1", amount="100")
        with self.conn.transaction():
            self.conn.execute("SET LOCAL ROLE bi_app")
            rows = self.conn.execute(
                "SELECT shop_id, day, line_kind, sum(quantity), sum(gift_quantity), "
                "sum(sales_amount), bool_and(allocation_verified), sum(line_count), "
                "sum(cost_line_count), sum(cost_quantity), sum(cost_total) "
                "FROM reporting.v_product_cost_daily "
                "WHERE shop_id = ANY(%s) AND day >= %s AND day < %s "
                "GROUP BY shop_id, day, line_kind",
                ([self.shops["1"]], date(2026, 9, 1), date(2026, 9, 8))).fetchall()
            self.assertEqual(len(rows), 1)
            # 底层表权限不因视图而扩大：每条都要在自己的保存点里失败，
            # 否则一次权限异常会把后面几条都变成 InFailedSqlTransaction 的空断言。
            for table in ("bi.order_items", "bi.orders"):
                with self.assertRaises(Exception, msg=table) as denied:
                    with self.conn.transaction():
                        self.conn.execute(f"SELECT count(*) FROM {table}").fetchone()
                self.assertIsInstance(
                    denied.exception, psycopg.errors.InsufficientPrivilege,
                    f"{table} 必须是以权限拒绝报错，不是以中断判断伪证 {denied.exception!r}")

    def test_comparison_run_persists_with_its_own_template_and_full_chain(self):
        self._five_platforms()
        store = MemoryQueryRunStore(forbidden_values=set(self.shops.values()))
        result = self._run(store=store, metrics=["sales_amount"])
        run = store.runs[result.run_id]
        self.assertEqual(run["domain"], "commerce_performance")
        self.assertEqual(run["status"], "partial", "拼多多那一组缺失必须留痕")
        nodes = [event["node"] for event in store.events[result.run_id]]
        self.assertEqual(set(nodes) & COMMERCE_NODES, set(COMMERCE_NODES),
                         "对比报告走同一条固定节点链，不另开第二条路径")
        provenance = run["provenance"]
        self.assertEqual(provenance.template_id, "commerce_comparison_report")
        self.assertEqual(provenance.mapping_version, "platform-groups/2026-09-14.1",
                         "平台分组规则要进血缘：换合并规则就是另一个问题")
        normalized = run["state"]["normalized_request"]
        self.assertEqual(normalized["report_kind"], "comparison")
        self.assertEqual(normalized["group_by"], "platform")
        self.assertNotIn(self.shops["1"], json.dumps(run["state"], ensure_ascii=False,
                                                    default=str))
        self.assertNotIn("对比测试店", json.dumps(run["state"], ensure_ascii=False,
                                                default=str),
                         "真实店名不进状态")

    def test_fingerprint_separates_platform_and_shop_groupings(self):
        self._five_platforms()
        first = MemoryQueryRunStore(forbidden_values=set(self.shops.values()))
        second = MemoryQueryRunStore(forbidden_values=set(self.shops.values()))
        self._run(store=first, metrics=["sales_amount"])
        self._run(store=second, metrics=["sales_amount"], group_by="platform",
                  scope={"mode": "all_authorized", "platforms": ["jd"]})
        fingerprints = {store.runs[list(store.runs)[0]]["request_fingerprint"]
                        for store in (first, second)}
        self.assertEqual(len(fingerprints), 2,
                         "同一份参数换一个平台范围就不是同一个请求，不许命中旧结果")


def _new_run_fields() -> dict:
    """另一次运行的最小字段：配对检查要求 dataset_ref 属于同一次运行。"""
    return {
        "chat_id": UUID(int=7), "user_message_id": UUID(int=8),
        "subject_id": "t8-other", "tool_call_id": "other",
        "domain": "commerce_performance", "attempt_no": 1,
        "normalized_request": {},
        "state": {"node": "resolve_scope", "status": "running", "revision": 0}}


if __name__ == "__main__":
    unittest.main()
