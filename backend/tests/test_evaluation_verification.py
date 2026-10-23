"""T2（计划 2026-10-24）：判定器自检——坏观测不允许被判为通过。

要证明的负面清单（每条都有用例钉住）：

- 期望数值为 0 时，实际 ``None`` / 空串 / ``"bad"`` / 布尔 / NaN / Infinity
  都不能等于 0（旧 ``_quantize`` 把异常吞成 ``Decimal("0")`` 的路径不允许复活）；
  期望本身非法同样拒绝。
- 数量不被金额精度抹平：期望 7 件不匹配 7.005；金额按明确 2 位小数比较。
- 每个正常观测的逐字段变异（数值、basis/time_basis、覆盖、缺口披露、
  refused、remediation、no_values、text、调用次数、excluded_scope、
  compare 参数）都必须导致至少一条问题码。
- 场景形状：turns 非空、逐轮 expected 数量相等（zip 静默丢轮次不允许）。
"""

from __future__ import annotations

import copy
import unittest
from decimal import Decimal

from tests.evaluation.verification import (
    parse_finite_decimal,
    validate_case_shape,
    verify_commerce_turn,
    verify_expected,
)


class ParseFiniteDecimalTests(unittest.TestCase):
    def test_rejects_none_and_empty(self):
        for bad in (None, ""):
            with self.assertRaises(ValueError):
                parse_finite_decimal(bad)

    def test_rejects_garbage_strings(self):
        for bad in ("bad", "7件", "1,000", " ", "-"):
            with self.assertRaises(ValueError):
                parse_finite_decimal(bad)

    def test_rejects_booleans(self):
        # bool 是 int 子类：True 曾被 str() 成 "True" 再吞成 0。
        with self.assertRaises(ValueError):
            parse_finite_decimal(True)
        with self.assertRaises(ValueError):
            parse_finite_decimal(False)

    def test_rejects_non_finite(self):
        for bad in ("NaN", "Infinity", "-Infinity", "nan", "inf",
                    Decimal("NaN"), Decimal("Infinity"), float("nan"),
                    float("inf")):
            with self.assertRaises(ValueError):
                parse_finite_decimal(bad)

    def test_accepts_finite_values(self):
        self.assertEqual(parse_finite_decimal(7), Decimal(7))
        self.assertEqual(parse_finite_decimal("7.00"), Decimal("7.00"))
        self.assertEqual(parse_finite_decimal(Decimal("3.3")), Decimal("3.3"))
        self.assertEqual(parse_finite_decimal(0), Decimal(0))
        self.assertEqual(parse_finite_decimal("0"), Decimal(0))


class PrecisionPolicyTests(unittest.TestCase):
    """金额按 2 位小数比较；数量与比值按精确数值比较（不被金额精度抹平）。"""

    ROWS = [{"paid_amount": Decimal("33.333333")}]

    def _problems(self, expected, rows):
        return verify_expected(expected, {"rows": rows})

    def test_money_compares_at_two_decimals(self):
        self.assertEqual(self._problems(
            {"values": {"paid_amount": "33.33"}}, self.ROWS), [])

    def test_money_mismatch_is_caught(self):
        self.assertTrue(self._problems(
            {"values": {"paid_amount": "33.34"}}, self.ROWS))

    def test_quantity_is_not_flattened_to_money_precision(self):
        rows = [{"quantity": Decimal("7.005")}]
        self.assertTrue(self._problems({"values": {"quantity": "7"}}, rows))

    def test_quantity_exact_with_trailing_zeros(self):
        rows = [{"quantity": Decimal("7.00")}]
        self.assertEqual(self._problems({"values": {"quantity": "7"}}, rows), [])

    def test_ratio_exact_numeric_equality(self):
        rows = [{"roas": Decimal("3.0")}]
        self.assertEqual(self._problems({"values": {"roas": "3"}}, rows), [])


class BrokenValueCannotPassAsZeroTests(unittest.TestCase):
    """期望 0 时，一切非法观测必须是问题，不允许静默等于 0。"""

    def test_zero_expects_zero_passes(self):
        rows = [{"refund_amount": Decimal("0.00")}]
        self.assertEqual(verify_expected(
            {"values": {"refund_amount": "0"}}, {"rows": rows}), [])

    def test_none_value_fails(self):
        rows = [{"refund_amount": None}]
        self.assertTrue(verify_expected(
            {"values": {"refund_amount": "0"}}, {"rows": rows}))

    def test_empty_string_fails(self):
        rows = [{"refund_amount": ""}]
        self.assertTrue(verify_expected(
            {"values": {"refund_amount": "0"}}, {"rows": rows}))

    def test_garbage_string_fails(self):
        rows = [{"refund_amount": "bad"}]
        self.assertTrue(verify_expected(
            {"values": {"refund_amount": "0"}}, {"rows": rows}))

    def test_boolean_fails(self):
        rows = [{"refund_amount": True}]
        self.assertTrue(verify_expected(
            {"values": {"refund_amount": "0"}}, {"rows": rows}))

    def test_nan_fails(self):
        rows = [{"refund_amount": "NaN"}]
        self.assertTrue(verify_expected(
            {"values": {"refund_amount": "0"}}, {"rows": rows}))

    def test_infinity_fails(self):
        rows = [{"refund_amount": "Infinity"}]
        self.assertTrue(verify_expected(
            {"values": {"refund_amount": "0"}}, {"rows": rows}))

    def test_invalid_expected_value_fails(self):
        rows = [{"refund_amount": Decimal("5")}]
        self.assertTrue(verify_expected(
            {"values": {"refund_amount": "bad"}}, {"rows": rows}))

    def test_no_rows_when_values_expected_fails(self):
        observed = {"rows": None}
        self.assertTrue(verify_expected(
            {"values": {"refund_amount": "0"}}, observed))


def _normal_observation() -> dict:
    """一个手工可核的正常观测：所有白名单键都有值。"""
    return {
        "rows": [{"shop_id": "S1", "paid_amount": Decimal("1000.00")}],
        "limitations": ["来源质量未核验（尚无对账记录）"],
        "basis": [("S1", "paid_amount", "payment/head", "paid_time")],
        "coverage": {"status": "complete", "start": "2026-09-01T00:00:00+08:00",
                     "end": "2026-09-08T00:00:00+08:00", "gaps": [],
                     "suggested_window": None},
        "diagnostics": {"payments": {"row_count": 6}},
        "partial_answer": None,
        "refused": None,
        "remediation": None,
        "text": "最近7天支付1000元。",
        "calls": [{"tool": "query_business"}],
    }


def _normal_expected() -> dict:
    return {
        "values": {"paid_amount": "1000"},
        "codes": ["source_quality_unverified"],
        "not_codes": ["result_too_large"],
        "basis": [("S1", "paid_amount", "payment/head", "paid_time")],
        "limitations_contain": ["来源质量未核验"],
        "coverage": {"status": "complete", "end": "2026-09-08T00:00:00+08:00",
                     "gaps": []},
        "diagnostics": {"payments": {"row_count": 6}},
        "text_contains": ["1000"],
        "text_absent": ["下跌"],
        "calls": [{"tool": "query_business"}],
    }


class StructuredMutationTests(unittest.TestCase):
    """正常观测改任何被声明检查的字段都必须翻红。"""

    def setUp(self):
        self.expected = _normal_expected()
        self.observed = _normal_observation()

    def test_baseline_passes(self):
        self.assertEqual(verify_expected(self.expected, self.observed), [])

    def _mutated_fails(self, mutate_expected=None, mutate_observed=None, label=""):
        expected = copy.deepcopy(self.expected)
        observed = copy.deepcopy(self.observed)
        if mutate_expected:
            mutate_expected(expected)
        if mutate_observed:
            mutate_observed(observed)
        problems = verify_expected(expected, observed)
        self.assertTrue(problems, f"变异{label}未被判定器发现")

    def test_amount_mutation_fails(self):
        self._mutated_fails(
            mutate_observed=lambda o: o["rows"][0].__setitem__(
                "paid_amount", Decimal("999.00")), label="金额")

    def test_basis_mutation_fails(self):
        self._mutated_fails(
            mutate_observed=lambda o: o["basis"].__setitem__(
                0, ("S1", "paid_amount", "outstock/line", "paid_time")),
            label="basis")

    def test_time_basis_mutation_fails(self):
        self._mutated_fails(
            mutate_observed=lambda o: o["basis"].__setitem__(
                0, ("S1", "paid_amount", "payment/head", "consign_time")),
            label="time_basis")

    def test_coverage_status_mutation_fails(self):
        self._mutated_fails(
            mutate_observed=lambda o: o["coverage"].__setitem__(
                "status", "partial"), label="覆盖状态")

    def test_coverage_window_mutation_fails(self):
        self._mutated_fails(
            mutate_observed=lambda o: o["coverage"].__setitem__(
                "end", "2026-09-07T00:00:00+08:00"), label="覆盖区间")

    def test_gap_disclosure_mutation_fails(self):
        expected = _normal_expected()
        expected["partial_answer"] = {
            "answered_windows": ["2026-09-01/2026-09-05"],
            "missing_windows": ["2026-09-05/2026-09-08"],
            "disclosure": "仅覆盖 2026-09-01~2026-09-05，2026-09-05 起缺口。"}
        observed = _normal_observation()
        observed["partial_answer"] = {
            "answered_windows": ["2026-09-01/2026-09-05"],
            "missing_windows": ["2026-09-05/2026-09-08"],
            "disclosure": "仅覆盖 2026-09-01~2026-09-05，2026-09-05 起缺口。"}
        self.assertEqual(verify_expected(expected, observed), [])
        observed["partial_answer"]["missing_windows"] = []
        self.assertTrue(verify_expected(expected, observed))

    def test_refused_set_mutation_fails(self):
        expected = _normal_expected()
        expected["refused"] = [{"metric": "ad_spend",
                                "reason": "capability_unavailable"}]
        observed = _normal_observation()
        observed["refused"] = [{"metric": "ad_spend",
                                "reason": "capability_unavailable"}]
        self.assertEqual(verify_expected(expected, observed), [])
        observed["refused"][0]["reason"] = "missing_data"
        self.assertTrue(verify_expected(expected, observed))

    def test_not_codes_mutation_fails(self):
        self._mutated_fails(
            mutate_observed=lambda o: o["limitations"].append(
                "结果行数达到500上限，已拒绝出数以避免静默截断"),
            label="不该出现的原因码")

    def test_no_values_mutation_fails(self):
        expected = {"no_values": True}
        observed = {"rows": [{"paid_amount": 1}]}
        self.assertTrue(verify_expected(expected, observed))

    def test_text_mutation_fails(self):
        self._mutated_fails(
            mutate_observed=lambda o: o.__setitem__(
                "text", "最近7天支付2000元。"), label="回答文本")

    def test_forbidden_text_mutation_fails(self):
        self._mutated_fails(
            mutate_observed=lambda o: o.__setitem__(
                "text", "最近7天支付1000元，有所下跌。"), label="禁用词")

    def test_call_count_mutation_fails(self):
        self._mutated_fails(
            mutate_expected=lambda e: e.__setitem__("call_count", 2),
            label="调用次数")

    def test_missing_call_records_fail(self):
        expected = {"call_count": 1}
        observed = {"rows": None, "calls": None}
        self.assertTrue(verify_expected(expected, observed))


class CommerceMutationTests(unittest.TestCase):
    """compare_performance 回合：excluded_scope 与参数变异必须翻红。"""

    CALL = [{
        "arguments": {"caliber": "erp_outstock", "metrics": ["sales_amount"],
                      "scope": {"mode": "all_authorized"}},
        "payload": {
            "filters": {"caliber": "erp_outstock"},
            "data": [{"platform": "fxg", "sales_amount": "100.00"},
                     {"sales_amount": "100.00"}],
            "metric_statuses": [{"platform": "fxg", "metric": "sales_amount",
                                 "status": "ok", "reason": None}],
            "ranking": [], "group_statuses": [],
            "excluded_scope": [{"platform": "tb", "reason": "coverage_incomplete"}],
        },
    }]
    EXPECTED = {
        "tool": "compare_performance",
        "parameters": {"caliber": "erp_outstock"},
        "rows": {"fxg": {"sales_amount": "100.00"},
                 "total": {"sales_amount": "100.00"}},
        "cells": [("fxg", "sales_amount", "ok", None)],
        "excluded_reasons": ["coverage_incomplete"],
    }

    def test_baseline_passes(self):
        self.assertEqual(verify_commerce_turn(self.EXPECTED, self.CALL), [])

    def test_excluded_scope_mutation_fails(self):
        calls = copy.deepcopy(self.CALL)
        calls[0]["payload"]["excluded_scope"] = []
        self.assertTrue(verify_commerce_turn(self.EXPECTED, calls))

    def test_parameter_mutation_fails(self):
        calls = copy.deepcopy(self.CALL)
        calls[0]["arguments"]["caliber"] = "platform_payment"
        self.assertTrue(verify_commerce_turn(self.EXPECTED, calls))

    def test_row_value_mutation_fails(self):
        calls = copy.deepcopy(self.CALL)
        calls[0]["payload"]["data"][0]["sales_amount"] = "99.00"
        self.assertTrue(verify_commerce_turn(self.EXPECTED, calls))

    def test_cell_status_mutation_fails(self):
        calls = copy.deepcopy(self.CALL)
        calls[0]["payload"]["metric_statuses"][0]["status"] = "data_missing"
        self.assertTrue(verify_commerce_turn(self.EXPECTED, calls))

    def test_missing_call_fails(self):
        self.assertTrue(verify_commerce_turn(self.EXPECTED, []))


class CaseShapeTests(unittest.TestCase):
    def test_turns_must_be_non_empty(self):
        problems = validate_case_shape({"id": "01", "turns": [],
                                        "expected": {}})
        self.assertTrue(problems)

    def test_expected_list_length_must_match_turns(self):
        problems = validate_case_shape(
            {"id": "02", "turns": ["问一", "问二"],
             "expected": [{"status": "ok"}]})
        self.assertTrue(problems)

    def test_scalar_expected_repeats_for_all_turns(self):
        problems = validate_case_shape(
            {"id": "03", "turns": ["问一", "问二"],
             "expected": {"status": "ok"}})
        self.assertEqual(problems, [])

    def test_expected_items_must_be_dicts(self):
        problems = validate_case_shape(
            {"id": "04", "turns": ["问一"], "expected": ["ok"]})
        self.assertTrue(problems)

    def test_valid_multi_turn_shape_passes(self):
        problems = validate_case_shape(
            {"id": "05", "turns": ["问一", "问二"],
             "expected": [{"status": "ok"}, {"status": "missing_data"}]})
        self.assertEqual(problems, [])


if __name__ == "__main__":
    unittest.main()
