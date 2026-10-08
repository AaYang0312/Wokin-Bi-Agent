"""覆盖缺口部分答案（计划 2026-10-08）。

T1 钉载荷形状。T2 钉不连库的可分解性：空覆盖、未知截止、混合指标、
同源比值的公共窗口。查询接线与关闸逐字一致留到 T3。
"""

from __future__ import annotations

import pathlib
import unittest
from datetime import datetime
from zoneinfo import ZoneInfo


ANSWERED = [["2026-09-01", "2026-09-29"]]
MISSING = [["2026-09-29", "2026-10-01"]]
DISCLOSURE = "仅覆盖 2026-09-01~2026-09-29，2026-09-29 起缺口"


def _block(**overrides):
    block = {
        "answered_windows": ANSWERED,
        "missing_windows": MISSING,
        "metric_refs": ["ad_spend"],
        "disclosure": DISCLOSURE,
    }
    block.update(overrides)
    return block


def _artifact(block):
    return {"status": "ok", "partial_answer": block}


class PartialAnswerContractTests(unittest.TestCase):
    def test_disclosure_text_is_the_server_template(self):
        from bi_agent.metrics import partial_answer_disclosure

        self.assertEqual(
            partial_answer_disclosure(
                [("2026-09-01", "2026-09-29")],
                [("2026-09-29", "2026-10-01")]),
            DISCLOSURE)
        self.assertEqual(
            partial_answer_disclosure(
                [("2026-09-01", "2026-09-10"), ("2026-09-20", "2026-09-29")],
                [("2026-09-10", "2026-09-20"), ("2026-09-29", "2026-10-01")]),
            "仅覆盖 2026-09-01~2026-09-10、2026-09-20~2026-09-29，"
            "2026-09-10、2026-09-29 起缺口")

    def test_public_and_model_payloads_accept_the_server_block(self):
        from bi_agent.runtime.models import (
            validate_artifact_payload, validate_model_payload)

        public = validate_artifact_payload(_artifact(_block()))
        model = validate_model_payload(_artifact(_block()))
        self.assertEqual(public["partial_answer"]["disclosure"], DISCLOSURE)
        self.assertEqual(model["partial_answer"]["metric_refs"], ["ad_spend"])

    def test_rewritten_or_malformed_blocks_are_rejected(self):
        from bi_agent.runtime.models import validate_artifact_payload

        cases = (
            ("rewritten", _block(disclosure="9月基本完整")),
            ("unknown_key", _block(note="完整")),
            ("empty_answered", _block(answered_windows=[])),
            ("empty_missing", _block(missing_windows=[])),
            ("reversed_window", _block(
                answered_windows=[["2026-09-29", "2026-09-01"]])),
            ("unknown_metric", _block(metric_refs=["net_profit"])),
            ("duplicate_metric", _block(metric_refs=["ad_spend", "ad_spend"])),
            ("missing_disclosure", {
                "answered_windows": ANSWERED,
                "missing_windows": MISSING,
                "metric_refs": ["ad_spend"],
            }),
        )
        for name, block in cases:
            with self.subTest(case=name):
                with self.assertRaises(ValueError):
                    validate_artifact_payload(_artifact(block))

    def test_model_cannot_rewrite_the_disclosure(self):
        from bi_agent.runtime.models import validate_model_payload

        with self.assertRaises(ValueError):
            validate_model_payload(_artifact(_block(disclosure="覆盖已经完整")))


class PartialAnswerGateTests(unittest.TestCase):
    def test_gate_defaults_off_and_rejects_loose_booleans(self):
        from bi_agent.config import AppSettings, load_app_settings
        from tests.test_core import valid_app_env

        absent = load_app_settings(valid_app_env())
        explicit = load_app_settings({
            **valid_app_env(), "PARTIAL_ANSWERS_ENABLED": "false"})
        self.assertEqual(absent, explicit)
        self.assertFalse(absent.partial_answers_enabled)
        self.assertIs(AppSettings.model_fields["partial_answers_enabled"].default, False)
        enabled = load_app_settings({
            **valid_app_env(), "PARTIAL_ANSWERS_ENABLED": "true"})
        self.assertTrue(enabled.partial_answers_enabled)
        with self.assertRaises(ValueError) as caught:
            load_app_settings({**valid_app_env(), "PARTIAL_ANSWERS_ENABLED": "yes"})
        self.assertIn("PARTIAL_ANSWERS_ENABLED 只能是 true 或 false",
                      str(caught.exception))

    def test_env_example_ships_the_gate_closed(self):
        text = (pathlib.Path(__file__).resolve().parents[2] / ".env.example"
                ).read_text(encoding="utf-8")
        assignments = [line for line in text.splitlines()
                       if line.startswith("PARTIAL_ANSWERS_ENABLED=")]
        self.assertEqual(assignments, ["PARTIAL_ANSWERS_ENABLED=false"])


AS_OF = datetime(2026, 9, 30, 23, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
REQUESTED = ("2026-09-01", "2026-10-01")


def _decompose(metrics, coverages):
    from bi_agent.business_query.partial import decompose_partial_answer

    return decompose_partial_answer(REQUESTED, metrics, coverages)


def _covered(*windows, as_of=AS_OF):
    from bi_agent.business_query.partial import MetricCoverage

    return MetricCoverage(covered=tuple(windows), data_as_of=as_of)


class PartialDecompositionTests(unittest.TestCase):
    """T2：覆盖能否拆开出数，只由窗口与指标种类决定，不连库。"""

    def test_empty_coverage_stays_missing_data(self):
        decision = _decompose(("ad_spend",), {"ad_spend": _covered()})
        self.assertEqual(decision.status, "missing_data")
        self.assertEqual(decision.groups, ())
        self.assertEqual(decision.refused, (("ad_spend", "coverage_incomplete"),))

    def test_unknown_data_as_of_stays_missing_data(self):
        decision = _decompose(
            ("visitors",),
            {"visitors": _covered(("2026-09-01", "2026-09-20"), as_of=None)})
        self.assertEqual(decision.status, "missing_data")
        self.assertEqual(decision.refused, (("visitors", "data_as_of_unknown"),))

    def test_single_aggregate_uses_its_covered_spans(self):
        decision = _decompose(
            ("ad_spend",),
            {"ad_spend": _covered(("2026-09-01", "2026-09-20"))})
        self.assertEqual(decision.status, "partial")
        self.assertEqual(decision.refused, ())
        group = decision.groups[0]
        self.assertEqual(group.metrics, ("ad_spend",))
        self.assertEqual(group.answered_windows, (("2026-09-01", "2026-09-20"),))
        self.assertEqual(group.missing_windows, (("2026-09-20", "2026-10-01"),))

    def test_complete_aggregate_does_not_become_partial(self):
        decision = _decompose(
            ("paid_amount",),
            {"paid_amount": _covered(("2026-09-01", "2026-10-01"))})
        self.assertEqual(decision.status, "complete")
        self.assertEqual(decision.groups[0].missing_windows, ())
        self.assertEqual(decision.refused, ())

    def test_manual_and_erp_metrics_do_not_share_a_refusal(self):
        decision = _decompose(
            ("paid_amount", "ad_spend"),
            {
                "paid_amount": _covered(("2026-09-01", "2026-10-01")),
                "ad_spend": _covered(("2026-09-01", "2026-09-20")),
            })
        self.assertEqual(decision.status, "partial")
        self.assertEqual(decision.refused, ())
        by_metric = {group.metrics: group.answered_windows for group in decision.groups}
        self.assertEqual(by_metric[("paid_amount",)], (("2026-09-01", "2026-10-01"),))
        self.assertEqual(by_metric[("ad_spend",)], (("2026-09-01", "2026-09-20"),))

    def test_same_source_ratio_uses_the_intersection(self):
        decision = _decompose(
            ("roas",),
            {
                "paid_amount_sycm": _covered(("2026-09-01", "2026-09-25")),
                "ad_spend": _covered(("2026-09-10", "2026-09-30")),
            })
        self.assertEqual(decision.status, "partial")
        self.assertEqual(decision.refused, ())
        group = decision.groups[0]
        self.assertEqual(group.metrics, ("roas",))
        self.assertEqual(group.answered_windows, (("2026-09-10", "2026-09-25"),))
        self.assertEqual(group.missing_windows, (
            ("2026-09-01", "2026-09-10"), ("2026-09-25", "2026-10-01")))

    def test_ratio_without_a_common_span_is_refused(self):
        decision = _decompose(
            ("aov",),
            {
                "paid_amount": _covered(("2026-09-01", "2026-09-10")),
                "paid_orders": _covered(("2026-09-10", "2026-09-20")),
            })
        self.assertEqual(decision.status, "missing_data")
        self.assertEqual(decision.refused, (("aov", "coverage_incomplete"),))

    def test_cross_source_ratio_and_snapshot_stay_missing_data(self):
        decision = _decompose(
            ("cash_difference", "physical_total"),
            {
                "cash_difference": _covered(("2026-09-01", "2026-09-20")),
                "physical_total": _covered(("2026-09-01", "2026-09-20")),
            })
        self.assertEqual(decision.status, "missing_data")
        self.assertEqual(decision.groups, ())
        self.assertEqual(decision.refused, (
            ("cash_difference", "basis_incompatible"),
            ("physical_total", "basis_incompatible"),
        ))
