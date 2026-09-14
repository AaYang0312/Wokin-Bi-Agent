"""受控 SQL 探索测试（计划 Task 1 契约/依赖/门禁 + 计划 Task 2 固定 Tool 优先与编译器）。

`ExplorationContractTests` 钉形状：`ExplorationRequest` → `SqlDraft` →
`ValidatedQueryPlan` → `ExplorationResult`（加 `ExplorationColumn`）五个契约、
`sqlglot` 这一条锁定的依赖，以及 `AppSettings.controlled_sql_enabled` 门禁。
`ExplorationCompilerTests` 钉 Task 2 的两件纯服务端事：固定 Tool 优先
（`fixed_tool_for`）与确定性单基表 SELECT 编译（`compile_query`）。AST 策略（Task 3）、
只读执行与投影（Task 4）、运行域与 Agent Tool（Task 5）仍不在本文件里——所以本文件
不导入 `psycopg`，不连库，也不解析任何 SQL。

反恒真约定（开发流程 §4.1）：每一组拒绝用例都配一条只差那个字段的接受用例，并且断言
错误落在哪个字段上；只扫整份序列化载荷里的随机子串不算护栏。
"""

from datetime import date, timedelta
from decimal import Decimal
import inspect
import importlib
import importlib.metadata
import json
import pathlib
import re
import tomllib
import types
import typing
import unittest

from pydantic import BaseModel, ValidationError

from tests.test_core import valid_app_env

BACKEND_ROOT = pathlib.Path(__file__).resolve().parents[1]
SQLGLOT_SPECIFIER = ">=27.14,<28"
SQLGLOT_LOWER_BOUND = (27, 14)
SQLGLOT_UPPER_BOUND = (28,)
# 计划 Task 1 的 Produces 清单：五个契约，一个不多，一个不少。
EXPORTED_CONTRACTS = (
    "ExplorationColumn",
    "ExplorationRequest",
    "ExplorationResult",
    "SqlDraft",
    "ValidatedQueryPlan",
)
# 计划 Task 2 的 Produces 清单：两个入口 + 钉死的覆盖矩阵常量。
TASK_TWO_EXPORTS = ("FIXED_TOOL_METRICS", "compile_query", "fixed_tool_for")
# Task 2 之后本包仍然只有这四个模块（策略/执行/投影/工具属 Task 3-5）。
PACKAGE_MODULES = ["__init__.py", "compiler.py", "eligibility.py", "models.py"]
# Task 3-5 才会交付的入口名字：本切片里它们必须不存在。
NOT_YET_IMPLEMENTED = (
    "validate_exploration_plan", "estimate_plan", "execute_plan", "project_result",
    "execute_exploration_tool",
)
SHA256_HEX = "a" * 64

# --- Task 2 夹具用的目录真 ref ---------------------------------------------------
# 计划 Task 2 的示例把分组简写成 `field-day` / `field-shop-id`、把 JOIN 侧写作
# `field-platform`；已发布目录按视图作用域命名重复列（见
# `semantic_catalog/registry.py` 模块注释 a)），所以这里用真 ref，语义不变。
DAY_COST = "field-product-cost-daily-day"
SHOP_COST = "field-product-cost-daily-shop-id"
FIELD_COST_TOTAL = "field-product-cost-daily-cost-total"
LINE_KIND_COST = "field-product-cost-daily-line-kind"
PRODUCT_COST = "field-product-cost-daily-product-id"
COST_VIEW = "view-product-cost-daily"
SHOPS_VIEW = "view-shops"
SHOPS_PLATFORM = "field-shops-platform"
SHOPS_CURRENCY = "field-shops-currency"
COST_SHOPS_JOIN = "join-product-cost-daily-shops"
DAY_SHOP_DAILY = "field-shop-daily-day"
SHOP_SHOP_DAILY = "field-shop-daily-shop-id"
PRODUCT_PRODUCT_DAILY = "field-product-daily-product-id"
DAY_PAYMENTS = "field-payments-paid-at"
STATUS_ERP = "field-erp-document-daily-normalization-status"
LISTING_CAPTURED_AT = "field-listing-items-captured-at"
POOL_PHYSICAL = "field-physical-stock-items-pool-id"
SHOP_CHANNEL = "field-channel-stock-items-shop-id"

# 计划 Task 2 Step 3 逐字固定的覆盖矩阵；产品代码里再一份，两者必须逐字相等。
PLAN_FIXED_TOOL_METRICS = {
    "query_business": frozenset({
        "metric-paid-amount", "metric-paid-orders", "metric-erp-documents",
        "metric-refund-amount", "metric-cash-difference", "metric-quantity",
        "metric-product-paid-amount"}),
    "analyze_product_performance": frozenset({
        "metric-sales-amount", "metric-quantity", "metric-cost-total",
        "metric-product-gross-profit-reference"}),
    "compare_performance": frozenset({
        "metric-sales-amount", "metric-quantity", "metric-cost-total",
        "metric-paid-amount", "metric-paid-orders",
        "metric-erp-gross-profit-reference"}),
    "audit_listing_prices": frozenset({"metric-listing-price"}),
    "inspect_inventory": frozenset({
        "metric-physical-available-quantity", "metric-channel-sellable-quantity"}),
}
# 计划里 Tool 的书写顺序就是重叠时的优先级（与 `agent._tool_schemas()` 一致）。
PLAN_TOOL_ORDER = ("query_business", "analyze_product_performance", "compare_performance",
                   "audit_listing_prices", "inspect_inventory")


def request_values(**overrides):
    """计划 Task 1 Step 1 的种子请求（`metric-cost-total` 那一组 ref）。"""
    values = {
        "question": "按店铺和日期看有成本销售额",
        "start": "2026-09-01",
        "end": "2026-09-08",
        "entity_refs": ["entity-shop"],
        "requested_metric_refs": ["metric-cost-total"],
        "group_by_field_refs": ["field-shop-id", "field-day"],
        "limit": 100,
    }
    values.update(overrides)
    return values


def make_request(**overrides):
    from bi_agent.exploration.models import ExplorationRequest

    return ExplorationRequest(**request_values(**overrides))


# --- 计划 Task 2 的夹具 ----------------------------------------------------------

def catalog_entries():
    """已发布目录的 ref 索引：Task 2 的夹具只从这一份真源反推 ref。"""
    from bi_agent.semantic_catalog.registry import CATALOG, catalog_indexes

    return catalog_indexes(CATALOG)


def as_list(value):
    return [value] if isinstance(value, str) else list(value)


def selection_for(metric_refs, *, groups=(), joins=(),
                  entities=("entity-shop",), selected_metrics=None,
                  missing_concepts=(), requires_clarification=False, **overrides):
    """造一份“检索已经把请求要的 ref 都选上”的选择（只给测试用）。

    `selected_metrics` 是故意单独开的参数：要把“本轮没选上这个指标”这种不匹配形状
    衣达出来时，不能靠重复传 `metric_refs` 的关键字参数（那是位置参数的名字）。
    """
    from bi_agent.semantic_catalog.models import SemanticSelection
    from bi_agent.semantic_catalog.registry import CATALOG

    entries = catalog_entries()
    refs = as_list(metric_refs)
    fields = set(groups)
    views = set()
    for ref in refs:
        if ref not in entries.metrics:
            # 计划矩阵里的 `metric-quantity` 在目录里没有条目：不伪造目录。
            continue
        fields.update(entries.metrics[ref].required_field_refs)
        views.add(entries.fields[entries.metrics[ref].required_field_refs[0]].view_ref)
    values = {
        "catalog_version": CATALOG.version,
        "entity_refs": tuple(sorted(entities)),
        "metric_refs": tuple(sorted(set(refs if selected_metrics is None
                                        else as_list(selected_metrics)))),
        "view_refs": tuple(sorted(views)),
        "field_refs": tuple(sorted(fields)),
        "join_path_refs": tuple(sorted(joins)),
        "missing_concepts": tuple(missing_concepts),
        "requires_clarification": requires_clarification,
    }
    values.update(overrides)
    return SemanticSelection(**values)


def request_for(metric_refs, groups=(), **overrides):
    """计划 Task 2 种子里的 `request_for(...)`：只有 ref 与业务值。"""
    from bi_agent.exploration.models import ExplorationRequest

    values = {
        "question": "按天看商品成本合计",
        "start": "2026-09-01",
        "end": "2026-09-08",
        "entity_refs": ["entity-shop"],
        "requested_metric_refs": as_list(metric_refs),
        "group_by_field_refs": list(groups),
        "limit": 100,
    }
    values.update(overrides)
    return ExplorationRequest(**values)


def compile_for(metric_refs, groups=(), *, selection=None, allowed_shop_ids=None,
                **overrides):
    """计划 Task 2 种子里的编译入口：选择集默认按请求自动补齐。"""
    from bi_agent.exploration.compiler import compile_query

    request = request_for(metric_refs, groups, **overrides)
    if selection is None:
        selection = selection_for(
            metric_refs, groups=groups, entities=request.entity_refs,
            joins=(COST_SHOPS_JOIN,) if SHOPS_PLATFORM in groups else ())
    return compile_query(request, selection=selection,
                         allowed_shop_ids=frozenset({"S1", "S2"})
                         if allowed_shop_ids is None else allowed_shop_ids)


def compile_error(metric_refs, groups=(), **kwargs):
    """要求编译失败并交出稳定原因码：能编出来就判红。"""
    try:
        draft = compile_for(metric_refs, groups, **kwargs)
    except ValueError as exc:
        return str(exc)
    raise AssertionError(f"编译器接下了这个请求：{draft.sql_text!r}")


def dequoted(sql_text):
    """只把双引号去掉：用来按字面复现计划种子里那两段未加引号的模板文本。"""
    return sql_text.replace('"', "")


def draft_values(**overrides):
    values = {
        "sql_text": 'SELECT "day", sum("cost_total") FROM "reporting"."v_product_cost_daily"'
                    " AS fact GROUP BY \"day\"",
        "parameters": {"allowed_shop_ids": ["S1", "S2"], "limit": 100},
        "selected_refs": ["view-product-cost-daily", "metric-cost-total", "field-day"],
    }
    values.update(overrides)
    return values


def plan_values(**overrides):
    values = {
        "template_version": "exploration-sql/2026-09-14.1",
        "catalog_version": "semantic/2026-09-14.1",
        "statement_fingerprint": SHA256_HEX,
        "sql_text": draft_values()["sql_text"],
        "parameters": {"allowed_shop_ids": ["S1"], "limit": 100},
        "selected_refs": ["metric-cost-total", "field-day"],
    }
    values.update(overrides)
    return values


def column_values(**overrides):
    values = {"ref": "metric-cost-total", "data_type": "decimal"}
    values.update(overrides)
    return values


def result_values(**overrides):
    values = {
        "template_version": "exploration-sql/2026-09-14.1",
        "catalog_version": "semantic/2026-09-14.1",
        "statement_fingerprint": SHA256_HEX,
        "columns": [
            {"ref": "field-day", "data_type": "date"},
            {"ref": "metric-cost-total", "data_type": "decimal"},
        ],
        "rows": [{"field-day": "2026-09-01", "metric-cost-total": "12.30"}],
        "basis": [{"metric": "metric-cost-total", "basis": "cost/2026-09-12.1"}],
        "coverage": {"status": "complete", "gaps": []},
        "diagnostics": [{"code": "cost_coverage", "rows": 1}],
        "limitations": ["来源成本覆盖率未核验"],
    }
    values.update(overrides)
    return values


def error_of(factory, values):
    """要求构造失败并交出错误列表：没有任何错误时直接判红。"""
    try:
        factory(**values)
    except ValidationError as exc:
        return exc.errors(include_url=False)
    raise AssertionError(f"{factory.__name__} accepted {sorted(values)}")


def locs_of(factory, values):
    return [tuple(error["loc"]) for error in error_of(factory, values)]


def request_locs(**overrides):
    """只改请求的一个角度，报错就该落在那个字段上。"""
    from bi_agent.exploration.models import ExplorationRequest

    return locs_of(ExplorationRequest, request_values(**overrides))


class ExplorationContractTests(unittest.TestCase):
    # --- 计划 Task 1 Step 1 的种子用例 -------------------------------------------

    def test_controlled_sql_requires_semantic_catalog(self):
        """计划 Task 1 Step 1 种子用例：目录关着不许开受控 SQL。"""
        from bi_agent.config import load_app_settings

        env = valid_app_env() | {
            "SEMANTIC_CATALOG_ENABLED": "false",
            "CONTROLLED_SQL_ENABLED": "true",
        }
        with self.assertRaisesRegex(ValueError, "CONTROLLED_SQL_REQUIRES_SEMANTIC_CATALOG"):
            load_app_settings(env)
        # 稳定原因码本身：不回显 DSN，不夹带别的文字。
        with self.assertRaises(ValueError) as caught:
            load_app_settings(env)
        self.assertEqual(str(caught.exception), "CONTROLLED_SQL_REQUIRES_SEMANTIC_CATALOG")
        self.assertNotIn("postgresql", str(caught.exception))

    def test_request_accepts_refs_and_rejects_sql_identifiers(self):
        from bi_agent.exploration.models import ExplorationRequest

        request = ExplorationRequest(
            question="按店铺和日期看有成本销售额",
            start="2026-09-01", end="2026-09-08",
            entity_refs=["entity-shop"],
            requested_metric_refs=["metric-cost-total"],
            group_by_field_refs=["field-shop-id", "field-day"], limit=100)
        self.assertEqual(request.limit, 100)
        with self.assertRaises(ValueError):
            ExplorationRequest(
                question="x", start="2026-09-01", end="2026-09-08",
                entity_refs=["reporting.v_shop_daily"],
                requested_metric_refs=["SUM(paid_amount)"],
                group_by_field_refs=[], limit=501)
        # 种子请求被接受的那部分要按值钉住，"没抛异常"不是断言。
        self.assertEqual(request.question, "按店铺和日期看有成本销售额")
        self.assertEqual((request.start, request.end), (date(2026, 9, 1), date(2026, 9, 8)))
        self.assertEqual(request.entity_refs, ["entity-shop"])
        self.assertEqual(request.requested_metric_refs, ["metric-cost-total"])
        self.assertEqual(request.group_by_field_refs, ["field-shop-id", "field-day"])
        # 上面那次整体失败里，三个字段各自都被拒了一次（错误定位精确到列表下标）。
        self.assertEqual(
            sorted(locs_of(ExplorationRequest, request_values(
                question="x", entity_refs=["reporting.v_shop_daily"],
                requested_metric_refs=["SUM(paid_amount)"],
                group_by_field_refs=[], limit=501))),
            [("entity_refs", 0), ("limit",), ("requested_metric_refs", 0)])

    def test_canonical_contracts_accept_safe_values(self):
        """五个契约各有一份能过的安全输入：后面的拒绝用例因此不是"永远红"。"""
        from bi_agent.exploration.models import (
            ExplorationColumn, ExplorationRequest, ExplorationResult, SqlDraft,
            ValidatedQueryPlan)

        self.assertEqual(make_request().model_dump()["limit"], 100)
        draft = SqlDraft(**draft_values())
        self.assertEqual(sorted(draft.parameters), ["allowed_shop_ids", "limit"])
        self.assertEqual(len(draft.selected_refs), 3)
        plan = ValidatedQueryPlan(**plan_values())
        self.assertEqual(plan.statement_fingerprint, SHA256_HEX)
        self.assertIsNone(plan.estimated_rows)
        self.assertIsNone(plan.estimated_total_cost)
        self.assertEqual(plan.warnings, [])
        column = ExplorationColumn(**column_values())
        self.assertEqual((column.ref, column.data_type), ("metric-cost-total", "decimal"))
        result = ExplorationResult(**result_values())
        self.assertEqual([item.ref for item in result.columns],
                         ["field-day", "metric-cost-total"])
        self.assertEqual(result.rows, [{"field-day": "2026-09-01",
                                        "metric-cost-total": "12.30"}])
        self.assertTrue(issubclass(ExplorationRequest, BaseModel))

    def test_model_json_round_trip_keeps_the_contract_shape(self):
        """Task 5 要从模型 JSON 校验请求：JSON 数字合法，字符串数字不合法。"""
        from bi_agent.exploration.models import ExplorationRequest

        parsed = ExplorationRequest.model_validate_json(json.dumps(request_values()))
        self.assertEqual(parsed, make_request())
        self.assertEqual(locs_of(ExplorationRequest, {**request_values(), "limit": "100"}),
                         [("limit",)])
        self.assertEqual(
            [tuple(err["loc"]) for err in error_of(
                ExplorationRequest, {**request_values(), "question": 2026})],
            [("question",)])

    # --- ExplorationRequest 边界 -------------------------------------------------

    def test_question_must_be_nonblank_and_bounded(self):
        self.assertEqual(make_request(question="按店铺看成本").question, "按店铺看成本")
        self.assertEqual(make_request(question="问" * 4000).question, "问" * 4000)
        # 只判非空白：不静默改用户原文（改了就对不上诊断里的提问原文）。
        self.assertEqual(make_request(question=" 按店铺看成本 ").question,
                         " 按店铺看成本 ")
        for bad in ("", " ", "  \t\n ", "\u3000", "问" * 4001, 20260901, None, ["问"]):
            with self.subTest(bad=repr(bad)):
                self._assert_request_rejected("question", bad)

    def test_start_and_end_are_required_together(self):
        self.assertEqual(self._window_rejection(start="2026-09-01", end=None),
                         "exploration_window_incomplete")
        self.assertEqual(self._window_rejection(start=None, end="2026-09-08"),
                         "exploration_window_incomplete")
        both_none = make_request(start=None, end=None)
        self.assertEqual((both_none.start, both_none.end), (None, None))

    def test_window_is_half_open_and_bounded_to_366_days(self):
        start = date(2026, 1, 1)
        for ok_days in (1, 2, 365, 366):
            with self.subTest(ok_days=ok_days):
                self.assertEqual(make_request(start=start,
                                              end=start + timedelta(days=ok_days)).end,
                                 start + timedelta(days=ok_days))
        self.assertEqual(self._window_rejection(start=start, end=start),
                         "exploration_window_ordered")
        self.assertEqual(self._window_rejection(start=start,
                                                end=start - timedelta(days=1)),
                         "exploration_window_ordered")
        self.assertEqual(self._window_rejection(start=start,
                                                end=start + timedelta(days=367)),
                         "exploration_window_too_large")
        self.assertEqual(self._window_rejection(start="2026-09-08", end="2026-09-01"),
                         "exploration_window_ordered")
        self.assertEqual(self._window_rejection(start="2026-01-01", end="2027-01-03"),
                         "exploration_window_too_large")
        # 日期本身仍是字段级校验：坏日期不会伪装成"窗口不对"。
        self.assertEqual(request_locs(start="not-a-date"), [("start",)])

    def test_limit_is_an_integer_from_one_to_five_hundred(self):
        for ok in (1, 2, 499, 500):
            with self.subTest(ok=ok):
                self.assertEqual(make_request(limit=ok).limit, ok)
        self.assertEqual(make_request().limit, 100)            # 计划默认值
        for bad in (0, -1, 501, 10_000, True, False, "100", "1e2", 100.0, 99.5, None,
                    [], {"limit": 1}):
            with self.subTest(bad=repr(bad)):
                self._assert_request_rejected("limit", bad)

    def test_list_sizes_are_bounded_per_field(self):
        def refs(stem: str, count: int) -> list[str]:
            return [f"{stem}-{n}" for n in range(count)]

        self.assertEqual(make_request(entity_refs=refs("entity", 5)).entity_refs,
                         refs("entity", 5))
        self.assertEqual(make_request(requested_metric_refs=refs("metric", 8)
                                      ).requested_metric_refs, refs("metric", 8))
        self.assertEqual(make_request(group_by_field_refs=refs("field", 4)
                                      ).group_by_field_refs, refs("field", 4))
        self.assertEqual(make_request(entity_refs=[]).entity_refs, [])
        self.assertEqual(make_request(group_by_field_refs=[]).group_by_field_refs, [])
        # 指标非空：没有指标的探索不是一个查询。
        self._assert_request_rejected("requested_metric_refs", [])
        for field, stem, count in (("entity_refs", "entity", 6),
                                   ("requested_metric_refs", "metric", 9),
                                   ("group_by_field_refs", "field", 5)):
            with self.subTest(field=field):
                self._assert_request_rejected(field, refs(stem, count))

    def test_duplicate_refs_are_rejected_not_silently_deduplicated(self):
        """去重后长度变了就是"两个指标变成一个指标"：当场拒，不悄悄少查一项。"""
        unique = ["metric-cost-total", "metric-quantity"]
        self.assertEqual(make_request(requested_metric_refs=unique).requested_metric_refs,
                         unique)                     # 顺序与长度原样保留
        self.assertEqual(make_request(entity_refs=["entity-shop", "entity-order"]
                                      ).entity_refs, ["entity-shop", "entity-order"])
        self.assertEqual(make_request(group_by_field_refs=["field-day", "field-shop-id"]
                                      ).group_by_field_refs, ["field-day", "field-shop-id"])
        for field, duplicate in (
                ("entity_refs", ["entity-shop", "entity-shop"]),
                ("requested_metric_refs", ["metric-cost-total", "metric-cost-total"]),
                ("group_by_field_refs", ["field-day", "field-day"])):
            with self.subTest(field=field):
                self._assert_request_rejected(field, duplicate)

    def test_only_stable_semantic_refs_are_accepted(self):
        """ref 规则的唯一真源是语义目录：SQL 标识符、表达式、下划线拼法都进不来。"""
        from bi_agent.semantic_catalog.models import _require_ref

        for ref in ("entity-shop", "metric-cost-total", "field-shop-id", "field-day",
                    "view-product-cost-daily", "join-shop-daily-shops"):
            with self.subTest(ref=ref):
                _require_ref(ref)          # 规则本身认它，探索层就必须认它
                self.assertEqual(make_request(entity_refs=[ref]).entity_refs, [ref])
        for bad in ("reporting.v_shop_daily", "v_shop_daily", "SUM(paid_amount)",
                    "field_shop_id", "cost_total", "Entity-shop", "entity shop",
                    " entity-shop", "entity-shop ", "", "1", "entity-shop;x",
                    "DROP TABLE bi.orders", "metric-$sum", None, 7):
            with self.subTest(bad=repr(bad)):
                self._assert_request_rejected("requested_metric_refs", [bad])
        # 下标进 loc：才知道列表里哪一项被拒。
        self.assertEqual(request_locs(entity_refs=["entity-shop", "shop_id"]),
                         [("entity_refs", 1)])

    def test_request_rejects_sql_authorization_and_invented_fields(self):
        """模型不许递 SQL、店号或授权集合进来：那些只由服务端决定（总设计 §3）。"""
        from bi_agent.exploration.models import ExplorationRequest

        for key, value in (
                ("sql", "SELECT 1"),
                ("sql_text", draft_values()["sql_text"]),
                ("shop_ids", ["S1"]),
                ("allowed_shop_ids", ["S1"]),
                ("authorization_column", "shop_id"),
                ("view_ref", "view-product-cost-daily"),
                ("requested_metrics", ["metric-cost-total"]),
                ("group_by_refs", ["field-day"]),
                ("max_limit", 500),
                ("aggregate", "sum")):
            with self.subTest(key=key):
                errors = error_of(ExplorationRequest, {**request_values(), key: value})
                self.assertEqual([tuple(err["loc"]) for err in errors], [(key,)])
                self.assertEqual(errors[0]["type"], "extra_forbidden")

    def test_contracts_are_frozen_and_hide_inputs(self):
        from bi_agent.exploration.models import (
            ExplorationColumn, ExplorationRequest, ExplorationResult, SqlDraft,
            ValidatedQueryPlan)

        instances = [
            ExplorationRequest(**request_values()),
            SqlDraft(**draft_values()),
            ValidatedQueryPlan(**plan_values()),
            ExplorationColumn(**column_values()),
            ExplorationResult(**result_values()),
        ]
        self.assertEqual(len(instances), len(EXPORTED_CONTRACTS))
        for instance in instances:
            with self.subTest(model=type(instance).__name__):
                config = type(instance).model_config
                self.assertEqual(config.get("extra"), "forbid")
                self.assertIs(config.get("frozen"), True)
                self.assertIs(config.get("hide_input_in_errors"), True)
                field = next(iter(type(instance).model_fields))
                with self.assertRaises(ValidationError):
                    setattr(instance, field, "mutated")

    # --- SqlDraft / ValidatedQueryPlan ------------------------------------------

    def test_sql_draft_keeps_text_and_parameters_apart(self):
        """草案是"文本 + 具名参数"两段：真值不许混进 SQL 文本。"""
        from bi_agent.exploration.models import SqlDraft

        draft = SqlDraft(**draft_values())
        self.assertEqual(draft.parameters["allowed_shop_ids"], ["S1", "S2"])
        self.assertEqual(draft.parameters["limit"], 100)
        self.assertEqual(SqlDraft(**draft_values(
            parameters={"nested": {"a": [1, Decimal("2")]}, "limit": 1}
        )).parameters["nested"], {"a": [1, Decimal("2")]})
        for bad in (["S1"], "limit=1", None, 100):
            with self.subTest(bad=repr(bad)):
                self.assertEqual(locs_of(SqlDraft, draft_values(parameters=bad)),
                                 [("parameters",)])
        for bad in ("", "   ", "\n\t "):
            with self.subTest(bad=repr(bad)):
                self.assertEqual(locs_of(SqlDraft, draft_values(sql_text=bad)),
                                 [("sql_text",)])
        for bad in (["shop_id"], ["field-day", ""], ["field-day", 7], "field-day", None):
            with self.subTest(bad=repr(bad)):
                self.assertTrue(all(loc[0] == "selected_refs" for loc in locs_of(
                    SqlDraft, draft_values(selected_refs=bad))),
                    str(locs_of(SqlDraft, draft_values(selected_refs=bad))))
        for field in ("sql_text", "parameters", "selected_refs"):
            with self.subTest(missing=field):
                values = draft_values()
                del values[field]
                self.assertIn((field,), locs_of(SqlDraft, values))

    def test_plan_requires_a_sha256_fingerprint_and_version_identifiers(self):
        from bi_agent.exploration.models import ValidatedQueryPlan

        self.assertEqual(ValidatedQueryPlan(**plan_values(
            statement_fingerprint="0123456789abcdef" * 4)).statement_fingerprint,
            "0123456789abcdef" * 4)
        for bad in ("", "a" * 63, "a" * 65, "A" * 64, "SELECT", "g" * 64,
                    f"sha256:{SHA256_HEX}", None, 7):
            with self.subTest(fingerprint=repr(bad)):
                self.assertEqual(locs_of(
                    ValidatedQueryPlan,
                    plan_values(statement_fingerprint=bad)),
                    [("statement_fingerprint",)])
        for field in ("template_version", "catalog_version"):
            for bad in ("", "  ", "exploration sql/2026-09-14.1",
                        " exploration-sql/2026-09-14.1", "exploration-sql/2026-09-14.1 ",
                        "\n", 7, None):
                with self.subTest(field=field, bad=repr(bad)):
                    self.assertEqual(
                        locs_of(ValidatedQueryPlan, plan_values(**{field: bad})),
                        [(field,)])

    def test_plan_estimate_fields_are_bounded_and_finite(self):
        from bi_agent.exploration.models import ValidatedQueryPlan

        plan = ValidatedQueryPlan(**plan_values(
            estimated_rows=50_000, estimated_total_cost=Decimal("1234.500000"),
            warnings=["cost_above_advisor"]))
        self.assertEqual(plan.estimated_rows, 50_000)
        self.assertEqual(plan.estimated_total_cost, Decimal("1234.500000"))
        self.assertEqual(plan.warnings, ["cost_above_advisor"])
        self.assertEqual(ValidatedQueryPlan(**plan_values(
            estimated_rows=0)).estimated_rows, 0)
        self.assertEqual(ValidatedQueryPlan(**plan_values(
            estimated_total_cost=Decimal("0"))).estimated_total_cost, Decimal("0"))
        self.assertIsNone(ValidatedQueryPlan(**plan_values()).estimated_total_cost)
        for bad in (-1, True, False, "5", 5.0, 1.5, [], object()):
            with self.subTest(estimated_rows=repr(bad)):
                self.assertEqual(
                    locs_of(ValidatedQueryPlan, plan_values(estimated_rows=bad)),
                    [("estimated_rows",)])
        for bad in (Decimal("-0.000001"), Decimal("NaN"), Decimal("sNaN"),
                    Decimal("Infinity"), Decimal("-Infinity"), float("nan"),
                    float("inf"), "NaN", "Infinity", "abc", True, -1, []):
            with self.subTest(estimated_total_cost=repr(bad)):
                self.assertEqual(
                    locs_of(ValidatedQueryPlan,
                            plan_values(estimated_total_cost=bad)),
                    [("estimated_total_cost",)])
        self.assertEqual(locs_of(ValidatedQueryPlan, plan_values(warnings="cost_high")),
                         [("warnings",)])
        self.assertTrue(all(loc[0] == "warnings" for loc in locs_of(
            ValidatedQueryPlan, plan_values(warnings=[1]))))
        self.assertEqual(ValidatedQueryPlan(**plan_values(
            estimated_total_cost="100000")).estimated_total_cost, Decimal("100000"))

    # --- ExplorationColumn / ExplorationResult ----------------------------------

    def test_result_columns_use_the_catalog_data_type_vocabulary(self):
        from bi_agent.exploration.models import ExplorationColumn
        from bi_agent.semantic_catalog.models import DataType

        self.assertEqual(sorted(typing.get_args(DataType)),
                         ["boolean", "date", "datetime", "decimal", "integer", "ref",
                          "text"])
        for data_type in sorted(typing.get_args(DataType)):
            with self.subTest(data_type=data_type):
                self.assertEqual(ExplorationColumn(**column_values(
                    data_type=data_type)).data_type, data_type)
        for bad in ("DECIMAL", "", "money", "array", "varchar(1040)", None, 3):
            with self.subTest(bad=repr(bad)):
                self.assertEqual(
                    locs_of(ExplorationColumn, column_values(data_type=bad)),
                    [("data_type",)])
        for bad in ("cost_total", "reporting.v_product_cost_daily.cost_total", "", None):
            with self.subTest(ref=repr(bad)):
                self.assertEqual(locs_of(ExplorationColumn, column_values(ref=bad)),
                                 [("ref",)])

    def test_result_rows_are_capped_and_values_stay_structured(self):
        from bi_agent.exploration.models import ExplorationResult

        self.assertEqual(len(ExplorationResult(**result_values(
            rows=[{"field-day": "2026-09-01"}] * 500)).rows), 500)
        self.assertEqual(ExplorationResult(**result_values(rows=[])).rows, [])
        self.assertEqual(
            locs_of(ExplorationResult,
                    result_values(rows=[{"field-day": "x"}] * 501)),
            [("rows",)])
        rows = [{"field-day": date(2026, 9, 1), "metric-cost-total": Decimal("12.30"),
                 "flag": True, "nothing": None}]
        result = ExplorationResult(**result_values(rows=rows))
        self.assertEqual(result.rows[0]["metric-cost-total"], Decimal("12.30"))
        self.assertIs(result.rows[0]["flag"], True)
        self.assertIsNone(result.rows[0]["nothing"])
        for bad in (["field-day"], "rows", None, 1):
            with self.subTest(bad=repr(bad)):
                errors = locs_of(ExplorationResult, result_values(rows=bad))
                self.assertTrue(errors)
                self.assertTrue(all(loc[0] == "rows" for loc in errors), str(errors))
        for field in ("basis", "coverage", "diagnostics", "limitations"):
            with self.subTest(field=field):
                self.assertEqual(locs_of(ExplorationResult, result_values(**{field: None})),
                                 [(field,)])

    def test_result_required_fields_and_nested_shapes(self):
        from bi_agent.exploration.models import ExplorationResult

        for field in ("template_version", "catalog_version", "statement_fingerprint",
                      "columns", "rows", "basis", "coverage", "diagnostics",
                      "limitations"):
            with self.subTest(missing=field):
                values = result_values()
                del values[field]
                self.assertIn((field,), locs_of(ExplorationResult, values))
        self.assertEqual(
            [loc for loc in locs_of(ExplorationResult, result_values(
                basis=[{"metric": "metric-cost-total", "basis": 7.5}]))],
            [("basis", 0, "basis")])
        self.assertEqual(locs_of(ExplorationResult, result_values(limitations=[None])),
                         [("limitations", 0)])
        self.assertEqual(
            locs_of(ExplorationResult,
                    result_values(columns=["metric-cost-total"])),
            [("columns", 0)])
        self.assertEqual(locs_of(ExplorationResult,
                                 result_values(coverage=["status"])),
                         [("coverage",)])
        self.assertEqual(locs_of(ExplorationResult, result_values(
            statement_fingerprint="zz")), [("statement_fingerprint",)])
        self.assertEqual(locs_of(ExplorationResult,
                                 result_values(catalog_version="semantic 2026-09-14")),
                         [("catalog_version",)])
        self.assertEqual(ExplorationResult(**result_values(
            coverage={"status": "partial", "gaps": [{"start": "2026-09-03"}],
                      "estimated": 1})).coverage["gaps"], [{"start": "2026-09-03"}])

    # --- 包边界 ------------------------------------------------------------------

    def test_package_exports_the_task_one_and_two_surface(self):
        import bi_agent.exploration as exploration
        import bi_agent.exploration.compiler as compiler
        import bi_agent.exploration.eligibility as eligibility
        import bi_agent.exploration.models as models

        expected = set(EXPORTED_CONTRACTS) | set(TASK_TWO_EXPORTS)
        self.assertEqual(list(exploration.__all__), sorted(expected))
        namespace = vars(exploration)
        public = {name for name, value in namespace.items()
                  if not name.startswith("_")
                  and not isinstance(value, types.ModuleType)}
        self.assertEqual(public, expected)
        for name in EXPORTED_CONTRACTS:
            self.assertIs(namespace[name], getattr(models, name))
            self.assertTrue(issubclass(getattr(models, name), BaseModel), name)
        # Task 2 的两个入口只能住在自己的模块里：包不拄第二份实现。
        self.assertIs(namespace["compile_query"], compiler.compile_query)
        self.assertIs(namespace["fixed_tool_for"], eligibility.fixed_tool_for)
        self.assertIs(namespace["FIXED_TOOL_METRICS"], eligibility.FIXED_TOOL_METRICS)
        # models.py 里公开的可调用对象仍然只有那五个契约类。
        self.assertEqual(
            sorted(name for name, value in vars(models).items()
                   if callable(value)
                   and getattr(value, "__module__", "") == models.__name__
                   and not name.startswith("_")),
            sorted(EXPORTED_CONTRACTS))

    def test_package_ships_only_the_task_one_and_two_modules(self):
        """目录列表钉住：Task 3-5 的策略/执行/工具文件不能提前偷渡。"""
        import bi_agent.exploration as exploration

        shipped = sorted(path.name for path in
                         pathlib.Path(exploration.__file__).parent.glob("*.py"))
        self.assertEqual(shipped, sorted(PACKAGE_MODULES))

    def test_this_slice_ships_no_policy_or_execution_entry_point(self):
        """Task 3-5 的名字现在必须不存在：本切片不校验 AST、不连库、不执行 SQL。"""
        import bi_agent.exploration as exploration
        import bi_agent.exploration.compiler as compiler
        import bi_agent.exploration.eligibility as eligibility
        import bi_agent.exploration.models as models

        for name in NOT_YET_IMPLEMENTED:
            with self.subTest(name=name):
                for holder in (exploration, models, compiler, eligibility):
                    self.assertFalse(hasattr(holder, name), f"{holder.__name__}.{name}")
        # 编译入口的形状：没有 conn / parameters / 超时参数可用。
        parameters = inspect.signature(compiler.compile_query).parameters
        self.assertEqual(list(parameters), ["request", "selection", "allowed_shop_ids"])
        self.assertTrue(parameters["selection"].kind is inspect.Parameter.KEYWORD_ONLY)
        self.assertTrue(parameters["allowed_shop_ids"].kind
                        is inspect.Parameter.KEYWORD_ONLY)
        self.assertFalse(hasattr(compiler, "execute"))
        self.assertFalse(hasattr(compiler, "policy"))

    # --- helpers ----------------------------------------------------------------

    def _assert_request_rejected(self, field, value):
        from bi_agent.exploration.models import ExplorationRequest

        errors = error_of(ExplorationRequest, request_values(**{field: value}))
        self.assertTrue(errors, f"{field}={value!r} was accepted")
        for error in errors:
            self.assertEqual(error["loc"][:1], (field,),
                             f"{field}={value!r} rejected elsewhere: {error}")

    def _window_rejection(self, **overrides):
        """跨字段规则报在模型根上：返回稳定原因码，不回显输入。"""
        from bi_agent.exploration.models import ExplorationRequest

        errors = error_of(ExplorationRequest, request_values(**overrides))
        self.assertEqual([tuple(err["loc"]) for err in errors], [()])
        self.assertEqual(errors[0]["type"], "value_error")
        self.assertNotIn("2026-", errors[0]["msg"])   # 不把窗口取值写进错误
        return errors[0]["msg"].removeprefix("Value error, ")


class ExplorationCompilerTests(unittest.TestCase):
    """计划 Task 2 Step 1/3/5：固定 Tool 优先与服务端确定性编译器。"""

    # --- 计划 Task 2 Step 1 的两条种子用例 ---------------------------------------

    def test_fixed_paid_amount_query_is_not_eligible(self):
        from bi_agent.exploration.eligibility import fixed_tool_for

        self.assertEqual(
            fixed_tool_for(selection_for("metric-paid-amount"),
                           request_for("metric-paid-amount")),
            "query_business",
        )
        # 反向护栏：没有任何固定 Tool 的契约能同时表达这个指标与这个粒度时，不许谎报覆盖
        # （`analyze_product_performance` 能拆行类型，但它的指标集里没有支付金额）。
        self.assertIsNone(fixed_tool_for(
            selection_for("metric-paid-amount", groups=[LINE_KIND_COST]),
            request_for("metric-paid-amount", groups=[LINE_KIND_COST])))

    def test_compiler_injects_scope_and_never_accepts_identifier_input(self):
        draft = compile_for("metric-cost-total", groups=[DAY_COST])
        # 计划种子按未加引号的模板文本写；只去掉双引号再逐字比。
        plain = dequoted(draft.sql_text)
        self.assertIn("reporting.v_product_cost_daily", plain)
        self.assertIn("shop_id = ANY(%(allowed_shop_ids)s)", plain)
        self.assertNotIn("S1", draft.sql_text)
        self.assertEqual(draft.parameters["allowed_shop_ids"], ["S1", "S2"])
        # 计划 Step 4：真形状逐字带双引号，去引号只看模板词序。
        self.assertIn('FROM "reporting"."v_product_cost_daily" AS fact', draft.sql_text)
        self.assertIn('WHERE fact."shop_id" = ANY(%(allowed_shop_ids)s)', draft.sql_text)

    # --- 固定 Tool 覆盖矩阵 ------------------------------------------------------

    def test_fixed_tool_metrics_are_exactly_the_plan_table(self):
        from bi_agent.exploration.eligibility import FIXED_TOOL_METRICS

        self.assertEqual({tool: set(refs) for tool, refs in FIXED_TOOL_METRICS.items()},
                         {tool: set(refs) for tool, refs in PLAN_FIXED_TOOL_METRICS.items()})
        # 重叠时的优先级 = 计划里 Tool 的书写顺序：既不是字母序也不是偶然的插入序。
        self.assertEqual(list(FIXED_TOOL_METRICS), list(PLAN_TOOL_ORDER))
        self.assertNotIn("evaluate_promotion", FIXED_TOOL_METRICS)     # 不新搭一个 Tool 覆盖
        with self.assertRaises(TypeError):
            FIXED_TOOL_METRICS["query_business"] = frozenset()        # 运行时改不了覆盖表

    # 分组维度词 → 目录里一个真属于该维度的字段 ref（只给表驱动测试用）。
    DIMENSION_REFS = {
        "day": DAY_SHOP_DAILY,
        "shop": SHOP_SHOP_DAILY,
        "product": PRODUCT_PRODUCT_DAILY,
        "platform": SHOPS_PLATFORM,
        "line_kind": LINE_KIND_COST,
        "pool": POOL_PHYSICAL,
    }

    def test_every_matrix_entry_is_claimed_by_the_tool_that_lists_it(self):
        """矩阵逐条过：每个 (Tool, 指标) 都要在该 Tool 能表达的一个粒度上被认出来。"""
        from bi_agent.exploration.eligibility import fixed_tool_for

        period = ["metric-paid-amount", "metric-paid-orders", "metric-erp-documents",
                  "metric-refund-amount", "metric-cash-difference"]
        probes = [("query_business", metric, []) for metric in period]
        probes += [("query_business", metric, ["product"]) for metric in
                   ("metric-quantity", "metric-product-paid-amount")]
        probes += [("analyze_product_performance", metric, [])
                   for metric in sorted(PLAN_FIXED_TOOL_METRICS["analyze_product_performance"])]
        probes += [("compare_performance", metric, ["platform"])
                   for metric in sorted(PLAN_FIXED_TOOL_METRICS["compare_performance"])]
        probes += [("audit_listing_prices", "metric-listing-price", ["shop"])]
        probes += [("inspect_inventory", "metric-physical-available-quantity", ["pool"]),
                   ("inspect_inventory", "metric-channel-sellable-quantity", ["shop"])]
        self.assertEqual({(tool, metric) for tool, metric, _dims in probes},
                         {(tool, metric) for tool, metrics in PLAN_FIXED_TOOL_METRICS.items()
                          for metric in metrics})
        for tool, metric, dimensions in probes:
            with self.subTest(tool=tool, metric=metric, dimensions=dimensions):
                groups = [self.DIMENSION_REFS[dimension] for dimension in dimensions]
                self.assertEqual(
                    fixed_tool_for(selection_for(metric, groups=groups),
                                   request_for(metric, groups=groups)), tool)

    def test_fixed_tool_group_support_comes_from_the_real_contracts(self):
        from bi_agent.exploration.eligibility import fixed_tool_for

        cases = (
            # (分组维度, 预期 Tool) —— `QueryRequest.group_by` 与 `ComparisonGroupBy` 的词表
            ([], "query_business"),
            (["day"], "query_business"),
            (["shop"], "query_business"),           # 重叠：矩阵顺序里 query_business 在前
            (["platform"], "compare_performance"),   # 只有它能按平台分组
            (["product"], None),                      # 支付金额是期间指标，不能按商品分组
            (["line_kind"], None),                    # 能拆行类型的 Tool 不持支付金额
            (["day", "shop"], None),                  # 两个 group_by 旋钮都是单值
        )
        for dimensions, expected in cases:
            with self.subTest(dimensions=dimensions):
                groups = [self.DIMENSION_REFS[dimension] for dimension in dimensions]
                self.assertEqual(
                    fixed_tool_for(selection_for("metric-paid-amount", groups=groups),
                                   request_for("metric-paid-amount", groups=groups)),
                    expected)
        # `PerformanceComparisonRequest.group_by` 是必填项：这个 Tool 没有"合计"那一档。
        # 拿只属于它的指标去探，无分组时必须递 None（带上分组则能认出来）。
        self.assertIsNone(fixed_tool_for(
            selection_for("metric-erp-gross-profit-reference"),
            request_for("metric-erp-gross-profit-reference")))
        self.assertEqual(fixed_tool_for(
            selection_for("metric-erp-gross-profit-reference", groups=[SHOP_SHOP_DAILY]),
            request_for("metric-erp-gross-profit-reference", groups=[SHOP_SHOP_DAILY])),
            "compare_performance")

    def test_overlapping_tools_are_decided_deterministically(self):
        from bi_agent.exploration.eligibility import fixed_tool_for

        metrics = ["metric-sales-amount", "metric-cost-total"]
        groups = [self.DIMENSION_REFS["shop"]]
        selection = selection_for(metrics, groups=groups)
        request = request_for(metrics, groups)
        first = fixed_tool_for(selection, request)
        self.assertEqual(first, "analyze_product_performance")     # 名字序里它在 compare 之前
        for _ in range(5):
            self.assertEqual(fixed_tool_for(selection, request), first)
        # 请求里指标的书写顺序不得改变结果。
        self.assertEqual(fixed_tool_for(selection, request_for(metrics[::-1], groups)), first)
        # 不重叠时也不能漏：平台分组只有 compare_performance 能表达。
        self.assertEqual(fixed_tool_for(
            selection_for(metrics, groups=[SHOPS_PLATFORM]),
            request_for(metrics, groups=[SHOPS_PLATFORM])), "compare_performance")

    def test_fixed_tool_wins_over_capability_coverage_and_selection_gaps(self):
        """缺能力 / 缺覆盖 / 要澄清都不是降级 SQL 的理由（计划 Task 2 Step 3）。"""
        from bi_agent.exploration.eligibility import fixed_tool_for

        blocked = selection_for("metric-paid-amount", missing_concepts=("promotion_spend",
                                                                        "net_profit"),
                               requires_clarification=True, view_refs=(), field_refs=(),
                               selected_metrics=())
        self.assertEqual(fixed_tool_for(blocked, request_for("metric-paid-amount")),
                         "query_business")
        # 同一个判断重复跑不得飘移（Task 5 的图会拿它做拒答依据）。
        self.assertEqual(fixed_tool_for(blocked, request_for("metric-paid-amount")),
                         "query_business")

    def test_fixed_tool_does_not_invent_coverage_for_unknown_refs(self):
        from bi_agent.exploration.eligibility import fixed_tool_for

        # 目录里没有的分组列：解不出维度，就不能宣称某个固定 Tool 能表达。
        self.assertIsNone(fixed_tool_for(
            selection_for("metric-paid-amount", groups=["field-shop-daily-nonexistent"]),
            request_for("metric-paid-amount", groups=["field-shop-daily-nonexistent"])))
        # 目录版本不认识时不猜分组维度；无分组不依赖目录，仍然按矩阵拒探索。
        stale = selection_for("metric-paid-amount", catalog_version="semantic/2020-01-01.1")
        self.assertIsNone(fixed_tool_for(stale, request_for("metric-paid-amount",
                                                           groups=[DAY_SHOP_DAILY])))
        self.assertEqual(fixed_tool_for(stale, request_for("metric-paid-amount")),
                         "query_business")

    def test_fixed_tool_rejects_foreign_input_shapes(self):
        from bi_agent.exploration.eligibility import fixed_tool_for

        with self.assertRaises(TypeError):
            fixed_tool_for(None, request_for("metric-paid-amount"))
        with self.assertRaises(TypeError):
            fixed_tool_for(selection_for("metric-paid-amount"),
                           {"requested_metric_refs": ["metric-paid-amount"]})

    # --- 编译器形状 --------------------------------------------------------------

    def test_grouped_query_matches_the_fixed_template(self):
        draft = compile_for("metric-cost-total", groups=[DAY_COST, SHOP_COST])
        self.assertEqual(draft.sql_text, "\n".join((
            'SELECT fact."day" AS "field_product_cost_daily_day", '
            'fact."shop_id" AS "_shop_id", '
            'sum(fact."cost_total") AS "metric_cost_total"',
            'FROM "reporting"."v_product_cost_daily" AS fact',
            'WHERE fact."shop_id" = ANY(%(allowed_shop_ids)s)',
            '  AND fact."day" >= %(start)s',
            '  AND fact."day" < %(end)s',
            'GROUP BY fact."day", fact."shop_id"',
            'ORDER BY fact."day", fact."shop_id"',
            'LIMIT %(limit)s',
        )))
        self.assertEqual(draft.selected_refs, sorted([
            COST_VIEW, "metric-cost-total", DAY_COST, SHOP_COST, FIELD_COST_TOTAL]))

    def test_total_query_omits_group_by_and_order_by(self):
        draft = compile_for("metric-cost-total")
        self.assertNotIn("GROUP BY", draft.sql_text)
        self.assertNotIn("ORDER BY", draft.sql_text)
        self.assertIn('SELECT sum(fact."cost_total") AS "metric_cost_total"', draft.sql_text)
        self.assertIn("LIMIT %(limit)s", draft.sql_text)
        # 只差分组：加上分组就两条子句都出现，不是模板里从来没有。
        grouped = compile_for("metric-cost-total", groups=[DAY_COST])
        self.assertIn("GROUP BY fact.\"day\"", grouped.sql_text)
        self.assertIn("ORDER BY fact.\"day\"", grouped.sql_text)

    def test_metrics_on_one_view_are_aggregated_in_a_single_pass(self):
        metrics = ["metric-cost-total", "metric-sales-amount"]
        draft = compile_for(metrics, groups=[DAY_COST])
        self.assertIn('sum(fact."cost_total") AS "metric_cost_total"', draft.sql_text)
        self.assertIn('sum(fact."sales_amount") AS "metric_sales_amount"', draft.sql_text)
        self.assertEqual(draft.sql_text.count("FROM "), 1)
        self.assertEqual(draft.sql_text.count("JOIN"), 0)
        self.assertEqual(draft.sql_text.count("SELECT"), 1)

    def test_output_aliases_are_identifier_safe(self):
        draft = compile_for("metric-cost-total", groups=[DAY_COST, SHOP_COST])
        aliases = re.findall(r'AS "([^"]+)"', draft.sql_text)
        self.assertEqual(aliases, ["field_product_cost_daily_day", "_shop_id",
                                   "metric_cost_total"])
        for alias in aliases:
            self.assertRegex(alias, r"^[a-z_][a-z0-9_]*$")
        # 授权列只能以下划线前缀输出，让投影层把它换成 opaque ref。
        self.assertNotIn('AS "field_product_cost_daily_shop_id"', draft.sql_text)

    def test_only_catalog_identifiers_are_quoted_in_the_sql(self):
        draft = compile_for("metric-cost-total", groups=[DAY_COST, SHOPS_PLATFORM])
        self.assertEqual(set(re.findall(r'"([^"]+)"', draft.sql_text)), {
            "reporting", "v_product_cost_daily", "v_shops", "day", "shop_id", "cost_total",
            "platform", "metric_cost_total", "field_product_cost_daily_day",
            "field_shops_platform"})
        # 稳定 ref 永远不进 SQL 文本：它们只被解成目录里的标识符。
        for ref in ("metric-cost-total", DAY_COST, SHOPS_PLATFORM):
            self.assertNotIn(ref, draft.sql_text)

    def test_compilation_is_deterministic_and_order_independent(self):
        forward = compile_for(["metric-sales-amount", "metric-cost-total"],
                              groups=[SHOP_COST, DAY_COST])
        backward = compile_for(["metric-cost-total", "metric-sales-amount"],
                               groups=[DAY_COST, SHOP_COST])
        self.assertEqual(forward.sql_text, backward.sql_text)
        self.assertEqual(forward.selected_refs, backward.selected_refs)
        self.assertEqual(forward.parameters, backward.parameters)
        again = compile_for(["metric-sales-amount", "metric-cost-total"],
                            groups=[SHOP_COST, DAY_COST])
        self.assertEqual(again.sql_text, forward.sql_text)

    # --- 编译器拒绝面 ------------------------------------------------------------

    def test_metrics_from_two_fact_grains_are_rejected(self):
        self.assertEqual(
            compile_error(["metric-cost-total", "metric-erp-gross-profit-reference"]),
            "exploration_multiple_fact_grains")
        # 只差一个指标：同基表的两个指标可以过（上面已测形状）。
        self.assertIn("metric_cost_total", compile_for(["metric-cost-total",
                                                       "metric-sales-amount"]).sql_text)

    def test_unregistered_join_is_rejected(self):
        # 商品成本 ↔ 单据毛利：目录里故意没有这条边。
        self.assertEqual(compile_error("metric-cost-total", groups=[STATUS_ERP]),
                         "exploration_join_not_registered")
        # 支付明细面上也没有到店铺档案的边：不能为了平台分组自己拼一条。
        self.assertEqual(compile_error("metric-payment-flow-amount", groups=[SHOPS_PLATFORM]),
                         "exploration_join_not_registered")
        # 只差视图：同一列在成本视图上走的是登记过的 N:1 边。
        self.assertIn("JOIN", compile_for("metric-cost-total",
                                         groups=[SHOPS_PLATFORM]).sql_text)

    def test_platform_join_uses_the_registered_edge_and_groups_only_platform(self):
        draft = compile_for("metric-cost-total", groups=[SHOPS_PLATFORM])
        self.assertEqual(draft.sql_text, "\n".join((
            'SELECT shops."platform" AS "field_shops_platform", '
            'sum(fact."cost_total") AS "metric_cost_total"',
            'FROM "reporting"."v_product_cost_daily" AS fact',
            'INNER JOIN "reporting"."v_shops" AS shops '
            'ON shops."shop_id" = fact."shop_id"',
            'WHERE fact."shop_id" = ANY(%(allowed_shop_ids)s)',
            '  AND fact."day" >= %(start)s',
            '  AND fact."day" < %(end)s',
            'GROUP BY shops."platform"',
            'ORDER BY shops."platform"',
            'LIMIT %(limit)s',
        )))
        # 一条 N:1 边、一次 JOIN、金额只在事实侧聚一次：不放大也不 N+1。
        self.assertEqual(draft.sql_text.count("JOIN"), 1)
        self.assertEqual(draft.sql_text.count("FROM "), 1)
        self.assertNotIn("CROSS", draft.sql_text)
        self.assertEqual(draft.selected_refs, sorted([
            COST_VIEW, SHOPS_VIEW, COST_SHOPS_JOIN, "metric-cost-total", SHOPS_PLATFORM,
            SHOP_COST, DAY_COST, FIELD_COST_TOTAL, "field-shops-shop-id"]))
        # 同基表上既按天又按平台：仍然只一条 JOIN。
        both = compile_for("metric-cost-total", groups=[SHOPS_PLATFORM, DAY_COST])
        self.assertIn('GROUP BY fact."day", shops."platform"', both.sql_text)
        self.assertEqual(both.sql_text.count("JOIN"), 1)

    def test_other_columns_of_the_joined_view_are_not_groupable(self):
        self.assertEqual(compile_error("metric-cost-total", groups=[SHOPS_CURRENCY]),
                         "exploration_join_not_registered")
        # 只差列：同一个视图上的平台列能走那条登记过的边。
        self.assertIn('GROUP BY shops."platform"', compile_for(
            "metric-cost-total", groups=[SHOPS_PLATFORM]).sql_text)

    def test_platform_grouping_needs_the_selection_to_have_picked_the_edge(self):
        """目录里有这条边、但本轮检索没选上：不自己补路径，直接拒。"""
        self.assertEqual(
            compile_error("metric-cost-total", groups=[SHOPS_PLATFORM],
                          selection=selection_for("metric-cost-total",
                                                 groups=[SHOPS_PLATFORM], joins=())),
            "exploration_join_not_selected")
        # 只差那一条边：选上了就能编。
        self.assertIn("INNER JOIN", compile_for("metric-cost-total",
                                                groups=[SHOPS_PLATFORM]).sql_text)

    def test_only_the_currently_published_catalog_is_compiled(self):
        """旧目录版本可以解释历史 Artifact，不能用来编新 SQL（总设计 §5.4）。"""
        stale = selection_for("metric-cost-total", catalog_version="semantic/2020-01-01.1")
        self.assertEqual(compile_error("metric-cost-total", selection=stale),
                         "exploration_catalog_version_mismatch")
        self.assertIn("FROM", compile_for("metric-cost-total").sql_text)

    def test_internal_and_measure_columns_cannot_be_group_dimensions(self):
        for group in (PRODUCT_COST, FIELD_COST_TOTAL):
            with self.subTest(group=group):
                self.assertEqual(compile_error("metric-cost-total", groups=[group]),
                                 "exploration_group_not_permitted")
        # 只差角色：同表的时间/维度/授权列都能分组。
        for group in (DAY_COST, LINE_KIND_COST, SHOP_COST):
            with self.subTest(group=group):
                self.assertIn("GROUP BY", compile_for("metric-cost-total",
                                                      groups=[group]).sql_text)

    def test_a_datetime_time_column_is_also_a_half_open_window(self):
        """时间列可以是 `timestamptz`：模板只要求它存在，区间仍然是 [start,end)。"""
        draft = compile_for("metric-payment-flow-amount", groups=[DAY_PAYMENTS])
        self.assertIn('WHERE fact."shop_id" = ANY(%(allowed_shop_ids)s)', draft.sql_text)
        self.assertIn('AND fact."paid_at" >= %(start)s', draft.sql_text)
        self.assertIn('AND fact."paid_at" < %(end)s', draft.sql_text)
        self.assertIn('GROUP BY fact."paid_at"', draft.sql_text)
        self.assertEqual(draft.sql_text.count("JOIN"), 0)

    def test_the_authorization_column_is_always_the_view_own_and_only_in_parameters(self):
        """按库存池授权的视图上谓词落在 `pool_id`：列名来自目录，值永远走参数。"""
        shop_ids = frozenset({"pool-a", "pool-b"})
        draft = compile_for("metric-physical-available-quantity", allowed_shop_ids=shop_ids)
        self.assertIn('WHERE fact."pool_id" = ANY(%(allowed_shop_ids)s)', draft.sql_text)
        self.assertIn('FROM "reporting"."v_physical_stock_items" AS fact', draft.sql_text)
        self.assertEqual(draft.parameters["allowed_shop_ids"], ["pool-a", "pool-b"])
        for shop_id in shop_ids:
            self.assertNotIn(shop_id, draft.sql_text)
        # 库存快照的时效分组也不是任何价审/库存报告的形状。
        from bi_agent.exploration.eligibility import fixed_tool_for

        self.assertIsNone(fixed_tool_for(
            selection_for("metric-listing-price", groups=[LISTING_CAPTURED_AT]),
            request_for("metric-listing-price", groups=[LISTING_CAPTURED_AT])))
        # 渠道库存按自己的店授权列分组：基表自己的列，不需要 JOIN。
        channel = compile_for("metric-channel-sellable-quantity", groups=[SHOP_CHANNEL])
        self.assertIn('GROUP BY fact."shop_id"', channel.sql_text)
        self.assertIn('SELECT fact."shop_id" AS "_shop_id"', channel.sql_text)
        self.assertEqual(channel.sql_text.count("JOIN"), 0)

    def test_metrics_need_one_field_and_a_permitted_aggregate(self):
        # 两个比值型指标都要分子分母各自求和再相除，不是一个 SUM/AVG 能发的数。
        self.assertEqual(compile_error("metric-transaction-average-price"),
                         "exploration_metric_field_ambiguous")
        self.assertEqual(compile_error("metric-product-gross-profit-reference"),
                         "exploration_metric_field_ambiguous")
        self.assertEqual(compile_error("metric-listing-price"),
                         "exploration_aggregate_not_permitted")
        # 只差形状：单字段 sum 指标能过。
        self.assertIn("sum(", compile_for("metric-cost-total").sql_text)

    def test_missing_dates_are_window_required(self):
        self.assertEqual(compile_error("metric-cost-total", start=None, end=None),
                         "exploration_window_required")
        self.assertIn("FROM", compile_for("metric-cost-total").sql_text)

    def test_the_366_day_boundary_is_inherited_from_the_request_contract(self):
        """窗口上界只在 `ExplorationRequest` 里判一次：编译器不拄第二份 366。"""
        draft = compile_for("metric-cost-total", start="2026-01-01", end="2026-12-31")
        self.assertEqual((draft.parameters["start"], draft.parameters["end"]),
                         (date(2026, 1, 1), date(2026, 12, 31)))
        with self.assertRaises(ValidationError) as caught:
            request_for("metric-cost-total", start="2026-01-01", end="2027-01-03")
        self.assertIn("exploration_window_too_large", str(caught.exception))

    def test_server_scope_must_be_a_non_empty_set_of_opaque_ids(self):
        for scope, expected in (
                (frozenset(), "exploration_scope_empty"),
                (["S1"], "exploration_scope_invalid"),                 # 可变异容器不收
                ("S1", "exploration_scope_invalid"),                   # 字符串不是 id 集合
                (frozenset({"S1", ""}), "exploration_scope_invalid"),
                (frozenset({"S1", "   "}), "exploration_scope_invalid"),
                (frozenset({"S1", None}), "exploration_scope_invalid"),
                (frozenset({"S1", True}), "exploration_scope_invalid")):
            with self.subTest(scope=repr(scope)):
                self.assertEqual(compile_error("metric-cost-total", allowed_shop_ids=scope),
                                 expected)
        # 只差容器类型：frozenset 与 set 都是服务端授权集合的形状。
        self.assertIn("ANY", compile_for("metric-cost-total",
                                         allowed_shop_ids={"S1"}).sql_text)

    def test_scope_is_sorted_into_parameters_and_never_into_sql_text(self):
        draft = compile_for("metric-cost-total",
                            allowed_shop_ids=frozenset({"S22", "S1", "S10"}))
        self.assertEqual(draft.parameters["allowed_shop_ids"], ["S1", "S10", "S22"])
        for shop_id in ("S1", "S10", "S22"):
            self.assertNotIn(shop_id, draft.sql_text)
            self.assertNotIn(shop_id, " ".join(draft.selected_refs))
        # 一条 ANY 谓词，不是逐店一圈：200 家店的 SQL 与 3 家店的逐字相同。
        big = frozenset(f"S{i}" for i in range(200))
        self.assertEqual(compile_for("metric-cost-total", allowed_shop_ids=big).sql_text,
                         draft.sql_text)
        self.assertEqual(draft.sql_text.count("= ANY(%(allowed_shop_ids)s)"), 1)

    def test_parameters_are_server_controlled_typed_and_key_sorted(self):
        draft = compile_for("metric-cost-total", groups=[DAY_COST], limit=25)
        self.assertEqual(list(draft.parameters), ["allowed_shop_ids", "end", "limit", "start"])
        self.assertEqual(draft.parameters["limit"], 25)
        self.assertIs(type(draft.parameters["limit"]), int)
        self.assertIs(type(draft.parameters["start"]), date)
        self.assertIs(type(draft.parameters["end"]), date)
        self.assertIs(type(draft.parameters["allowed_shop_ids"]), list)
        for forbidden in ("sql", "shop_ids", "order_by", "aggregate", "view_ref"):
            self.assertNotIn(forbidden, draft.parameters)

    def test_a_request_cannot_override_a_server_parameter(self):
        """形状契约之外，编译器自己复查服务端预算与参数类型。"""
        from bi_agent.exploration.compiler import compile_query
        from bi_agent.exploration.models import ExplorationRequest

        smuggled = ExplorationRequest.model_construct(
            question="x", start=date(2026, 9, 1), end=date(2026, 9, 8), entity_refs=[],
            requested_metric_refs=["metric-cost-total"], group_by_field_refs=[],
            limit=999_999)
        self.assertEqual(smuggled.limit, 999_999)          # 确实绕过了字段校验
        with self.assertRaises(ValueError) as caught:
            compile_query(smuggled, selection=selection_for("metric-cost-total"),
                          allowed_shop_ids=frozenset({"S1"}))
        self.assertEqual(str(caught.exception), "exploration_parameter_override")

        text_window = ExplorationRequest.model_construct(
            question="x", start="2026-09-01) OR 1=1 --", end=date(2026, 9, 8),
            entity_refs=[], requested_metric_refs=["metric-cost-total"],
            group_by_field_refs=[], limit=100)
        with self.assertRaises(ValueError) as caught:
            compile_query(text_window, selection=selection_for("metric-cost-total"),
                          allowed_shop_ids=frozenset({"S1"}))
        self.assertEqual(str(caught.exception), "exploration_parameter_override")

    def test_request_refs_must_be_selected_and_registered(self):
        cases = (
            ("exploration_ref_not_selected",
             dict(selection=selection_for("metric-cost-total", selected_metrics=()))),
            ("exploration_ref_not_selected",
             dict(selection=selection_for("metric-cost-total", field_refs=(
                 FIELD_COST_TOTAL,)))),
            ("exploration_ref_not_selected",
             dict(selection=selection_for("metric-cost-total", entities=()))),
            ("exploration_ref_not_selected",
             dict(selection=selection_for("metric-cost-total", view_refs=()))),
            ("exploration_ref_unregistered",
             dict(selection=selection_for("metric-quantity"))),
        )
        for expected, kwargs in cases:
            with self.subTest(expected=expected, **{k: repr(v) for k, v in kwargs.items()}):
                metric = ("metric-quantity" if expected == "exploration_ref_unregistered"
                          else "metric-cost-total")
                self.assertEqual(compile_error(metric, [DAY_COST], **kwargs), expected)
        # 只差那一项：配套齐了就能编。
        self.assertIn("FROM", compile_for("metric-cost-total", groups=[DAY_COST]).sql_text)

    def test_request_must_be_the_exploration_contract(self):
        from bi_agent.exploration.compiler import compile_query

        with self.assertRaises(TypeError):
            compile_query({"requested_metric_refs": ["metric-cost-total"]},
                          selection=selection_for("metric-cost-total"),
                          allowed_shop_ids=frozenset({"S1"}))
        with self.assertRaises(TypeError):
            compile_query(request_for("metric-cost-total"), selection={"metric_refs": ()},
                          allowed_shop_ids=frozenset({"S1"}))
        self.assertIn("FROM", compile_for("metric-cost-total").sql_text)

    def test_free_text_never_reaches_the_sql(self):
        draft = compile_for("metric-cost-total",
                            question='"; DROP TABLE bi.orders; -- reporting.v_shop_daily')
        self.assertNotIn("DROP", draft.sql_text)
        self.assertNotIn("bi.orders", draft.sql_text)
        self.assertNotIn("v_shop_daily", draft.sql_text)
        self.assertNotIn(";", draft.sql_text)
        self.assertNotIn("--", draft.sql_text)
        self.assertEqual(draft.sql_text.count("SELECT"), 1)
        self.assertNotIn("DROP", str(draft.parameters))

    def test_this_slice_neither_executes_sql_nor_checks_the_ast(self):
        """Task 2 不连库、不跑 EXPLAIN、不做 sqlglot 策略（那分属 Task 3/4）。"""
        import bi_agent.exploration.compiler as compiler
        import bi_agent.exploration.eligibility as eligibility

        for module in (compiler, eligibility):
            source = pathlib.Path(module.__file__).read_text(encoding="utf-8")
            for token in ("psycopg", "sqlglot", "EXPLAIN", "execute", "cursor", "connect",
                          "statement_timeout"):
                with self.subTest(module=module.__name__, token=token):
                    self.assertNotIn(token, source)


class ExplorationDependencyTests(unittest.TestCase):
    """`sqlglot` 是本轮唯一的新依赖（开发流程 §4.7：计划已明确批准）。"""

    def test_pyproject_declares_sqlglot_and_nothing_else_new(self):
        pyproject = tomllib.loads((BACKEND_ROOT / "pyproject.toml")
                                  .read_text(encoding="utf-8"))
        declared = pyproject["project"]["dependencies"]
        self.assertIn(f"sqlglot{SQLGLOT_SPECIFIER}", declared)
        self.assertEqual(
            sorted(declared),
            sorted(["fastapi>=0.116,<1", "httpx>=0.27,<1", "psycopg[binary]>=3.2,<4",
                    "pydantic>=2,<3", "sqlglot>=27.14,<28", "tzdata>=2024.1",
                    "uvicorn>=0.35,<1"]))
        self.assertEqual(len([item for item in declared if item.startswith("sqlglot")]),
                         1)

    def test_uv_lock_resolves_exactly_one_sqlglot_in_range(self):
        lock = tomllib.loads((BACKEND_ROOT / "uv.lock").read_text(encoding="utf-8"))
        resolved = [package for package in lock["package"]
                    if package["name"] == "sqlglot"]
        self.assertEqual(len(resolved), 1)
        version = resolved[0]["version"]
        self.assertTrue(self._in_locked_range(version), f"locked {version}")
        root = next(package for package in lock["package"]
                    if package["name"] == "bi-agent")
        self.assertIn({"name": "sqlglot"}, root["dependencies"])
        requires_dist = [item for item in root["metadata"]["requires-dist"]
                         if item["name"] == "sqlglot"]
        self.assertEqual(len(requires_dist), 1)
        self.assertEqual(requires_dist[0]["specifier"], SQLGLOT_SPECIFIER)

    def test_installed_sqlglot_satisfies_the_locked_range(self):
        version = importlib.metadata.version("sqlglot")
        lock = tomllib.loads((BACKEND_ROOT / "uv.lock").read_text(encoding="utf-8"))
        locked = next(package["version"] for package in lock["package"]
                      if package["name"] == "sqlglot")
        self.assertEqual(self._version_tuple(version), self._version_tuple(locked))
        self.assertTrue(self._in_locked_range(version), f"installed {version}")
        module = importlib.import_module("sqlglot")
        self.assertEqual(self._version_tuple(str(getattr(module, "__version__", ""))),
                         self._version_tuple(version))
        # 只证明"依赖可用"：本切片不用它解析或校验任何 SQL。
        self.assertTrue(callable(getattr(module, "parse", None)))

    def test_the_range_guard_itself_rejects_out_of_range_versions(self):
        """护栏自检：低于/高于区间的版本号必须判假，否则上面两条断言是空的。"""
        for too_low in ("26.3.0", "27.13.1", "27.0.0"):
            with self.subTest(version=too_low):
                self.assertFalse(self._in_locked_range(too_low))
        for too_high in ("28.0.0", "28.1.0", "29.0.0"):
            with self.subTest(version=too_high):
                self.assertFalse(self._in_locked_range(too_high))
        for ok in ("27.14", "27.14.0", "27.15.1", "27.99.0"):
            with self.subTest(version=ok):
                self.assertTrue(self._in_locked_range(ok))
        for malformed in ("", "latest", "sqlglot"):
            with self.subTest(version=repr(malformed)):
                with self.assertRaises(ValueError):
                    self._version_tuple(malformed)

    @staticmethod
    def _version_tuple(text: str) -> tuple[int, ...]:
        match = re.match(r"^(\d+)\.(\d+)(?:\.(\d+))?$", text.strip())
        if match is None:
            raise ValueError(f"unparsed_version:{text!r}")
        return tuple(int(part) for part in match.groups() if part is not None)

    @classmethod
    def _in_locked_range(cls, text: str) -> bool:
        version = cls._version_tuple(text)
        return SQLGLOT_LOWER_BOUND <= version < SQLGLOT_UPPER_BOUND


if __name__ == "__main__":
    unittest.main()
