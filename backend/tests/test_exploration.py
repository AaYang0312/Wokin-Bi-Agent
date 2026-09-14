"""受控 SQL 探索契约测试（计划 Task 1：契约、依赖、feature gate）。

这里钉的是"形状"，不是行为：本切片只交付 `ExplorationRequest` → `SqlDraft` →
`ValidatedQueryPlan` → `ExplorationResult`（加 `ExplorationColumn`）五个契约、
`sqlglot` 这一条锁定的依赖，以及 `AppSettings.controlled_sql_enabled` 门禁。编译器
（Task 2）、AST 策略（Task 3）、只读执行与投影（Task 4）、运行域与 Agent Tool（Task 5）
都不属于本文件当前应该测到的东西——所以本文件不导入 `psycopg`，也不连库。

反恒真约定（开发流程 §4.1）：每一组拒绝用例都配一条只差那个字段的接受用例，并且断言
错误落在哪个字段上；只扫整份序列化载荷里的随机子串不算护栏。
"""

from datetime import date, timedelta
from decimal import Decimal
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
# Task 2-5 才会交付的入口名字：本切片里它们必须不存在。
NOT_YET_IMPLEMENTED = (
    "fixed_tool_for", "compile_query", "validate_exploration_plan", "estimate_plan",
    "execute_plan", "project_result", "execute_exploration_tool",
)
SHA256_HEX = "a" * 64


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

    def test_package_exports_exactly_the_task_one_contracts(self):
        import bi_agent.exploration as exploration
        import bi_agent.exploration.models as models

        self.assertEqual(list(exploration.__all__), list(EXPORTED_CONTRACTS))
        namespace = vars(exploration)
        public = {name for name, value in namespace.items()
                  if not name.startswith("_")
                  and not isinstance(value, types.ModuleType)}
        self.assertEqual(public, set(EXPORTED_CONTRACTS))
        for name in EXPORTED_CONTRACTS:
            self.assertIs(namespace[name], getattr(models, name))
            self.assertTrue(issubclass(getattr(models, name), BaseModel), name)
        # models.py 里公开的可调用对象只有这五个契约类：没有藏编译/执行 helper。
        self.assertEqual(
            sorted(name for name, value in vars(models).items()
                   if callable(value)
                   and getattr(value, "__module__", "") == models.__name__
                   and not name.startswith("_")),
            sorted(EXPORTED_CONTRACTS))

    def test_this_slice_ships_no_compilation_or_execution_entry_point(self):
        """Task 2-5 的名字现在必须不存在：本切片不解析、不校验、不执行任何 SQL。"""
        import bi_agent.exploration as exploration
        import bi_agent.exploration.models as models

        for name in NOT_YET_IMPLEMENTED:
            with self.subTest(name=name):
                self.assertFalse(hasattr(exploration, name))
                self.assertFalse(hasattr(models, name))
        for module_name in ("compiler", "eligibility", "graph", "policy", "projection",
                            "repository", "tool"):
            with self.subTest(module=module_name):
                self.assertFalse(hasattr(exploration, module_name))
                self.assertFalse(hasattr(models, module_name))

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
