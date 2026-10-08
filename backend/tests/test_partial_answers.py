"""覆盖缺口部分答案（计划 2026-10-08）。

T1 钉载荷形状。T2 钉不连库的可分解性：空覆盖、未知截止、混合指标、
同源比值的公共窗口。T3 钉单段窗口收窄。T4 钉指标拆分：可答子集出数，
拒答子集进 refused。
"""

from __future__ import annotations

import copy
import json
import pathlib
import re
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

    def test_prompt_requires_the_actual_window_and_forbids_a_completeness_claim(self):
        from bi_agent.agent import _SYSTEM_PROMPT

        self.assertIn("partial_answer", _SYSTEM_PROMPT)
        self.assertIn("必须原样复述其中的实际覆盖窗口与缺口起点", _SYSTEM_PROMPT)
        self.assertIn("不得把这部分数字说成完整窗口", _SYSTEM_PROMPT)
        self.assertIn("refused", _SYSTEM_PROMPT)
        self.assertIn("不得把已经出数的指标说成整组指标都有数", _SYSTEM_PROMPT)


class PartialAnswerTamperTests(unittest.TestCase):
    """模型载荷上的删改不能换成服务端披露。

    这些攻击不放进 exploration_attacks.jsonl：那份语料的每一行都是 SQL，
    字段只有 id/sql/reason，策略层在碰库之前拒绝。部分答案的攻击对象是
    已经投影好的载荷，不是一条查询。
    """

    def test_corpus_cannot_replace_the_server_disclosure(self):
        from bi_agent.metrics import partial_answer_disclosure
        from bi_agent.runtime.models import (
            validate_artifact_payload, validate_model_payload)

        path = pathlib.Path(__file__).with_name("partial_answer_attacks.jsonl")
        text = path.read_text(encoding="utf-8")
        self.assertTrue(text.endswith("\n"))
        self.assertNotIn("\r", text)
        rows = [json.loads(line) for line in text.splitlines()]
        self.assertEqual([row["id"] for row in rows], ["P01", "P02", "P03"])
        server = validate_artifact_payload(_artifact(_block()))
        disclosure = server["partial_answer"]["disclosure"]
        self.assertEqual(disclosure, DISCLOSURE)
        for row in rows:
            tampered = copy.deepcopy(server)
            block = tampered["partial_answer"]
            if row["op"] == "drop":
                del tampered["partial_answer"]
                self.assertNotIn("partial_answer", validate_model_payload(tampered))
            elif row["op"] == "rewrite_disclosure":
                block["disclosure"] = row["disclosure"]
                with self.assertRaises(ValueError):
                    validate_model_payload(tampered)
            elif row["op"] == "rewrite_windows":
                block["answered_windows"] = row["answered_windows"]
                block["missing_windows"] = row["missing_windows"]
                block["disclosure"] = partial_answer_disclosure(
                    [tuple(pair) for pair in row["answered_windows"]],
                    [tuple(pair) for pair in row["missing_windows"]])
                forged = validate_model_payload(tampered)
                self.assertNotEqual(forged["partial_answer"], server["partial_answer"])
            else:
                self.fail(row["op"])
            self.assertEqual(server["partial_answer"]["disclosure"], DISCLOSURE)


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


def _assessment(**overrides):
    from bi_agent.data_quality import CoverageAssessment

    fields = dict(
        status="partial",
        requested_window=("2026-09-01", "2026-10-01"),
        covered_windows=(("2026-09-01", "2026-09-20"),),
        missing_windows=(("2026-09-20", "2026-10-01"),),
        data_as_of=datetime(2026, 9, 20, tzinfo=ZoneInfo("Asia/Shanghai")),
        quality_status="passed",
        source_batches=(),
        gaps=(),
        suggested_window=("2026-09-01", "2026-09-20"),
    )
    fields.update(overrides)
    return CoverageAssessment(**fields)


def _request(metrics):
    from bi_agent.metrics import QueryRequest

    return QueryRequest(start="2026-09-01", end="2026-10-01",
                        shop_ids=["S1"], metrics=list(metrics))


class PartialExecutionTests(unittest.TestCase):
    def test_one_covered_span_is_the_execution_window(self):
        from bi_agent.metrics import _single_covered_span

        start, end, block = _single_covered_span(
            _request(["ad_spend"]), _assessment())
        self.assertEqual((start.isoformat(), end.isoformat()),
                         ("2026-09-01", "2026-09-20"))
        self.assertEqual(block["metric_refs"], ["ad_spend"])
        self.assertEqual(block["disclosure"],
                         "仅覆盖 2026-09-01~2026-09-20，2026-09-20 起缺口")

    def test_disjoint_spans_are_not_narrowed(self):
        from bi_agent.metrics import _single_covered_span

        assessment = _assessment(
            covered_windows=(("2026-09-01", "2026-09-05"),
                             ("2026-09-10", "2026-09-15")),
            missing_windows=(("2026-09-05", "2026-09-10"),
                             ("2026-09-15", "2026-10-01")))
        self.assertIsNone(_single_covered_span(_request(["ad_spend"]), assessment))

    def test_cross_source_metric_is_not_narrowed(self):
        from bi_agent.metrics import _single_covered_span

        self.assertIsNone(_single_covered_span(
            _request(["cash_difference"]), _assessment()))

    def test_unknown_cutoff_is_not_narrowed(self):
        from bi_agent.metrics import _single_covered_span

        self.assertIsNone(_single_covered_span(
            _request(["ad_spend"]), _assessment(data_as_of=None)))

    def test_non_partition_ok_partial_classifies_as_partial(self):
        from datetime import date

        from bi_agent.business_query.nodes import _classify
        from bi_agent.metrics import Coverage, ToolResult
        from bi_agent.runtime.models import DomainStatus

        result = ToolResult(
            status="ok",
            data=[{"ad_spend": "40"}],
            coverage=Coverage(status="partial", start=date(2026, 9, 1),
                              end=date(2026, 9, 20),
                              gaps=["2026-09-05~2026-09-20"]),
            partial_answer=_block(
                answered_windows=[["2026-09-01", "2026-09-05"]],
                missing_windows=[["2026-09-05", "2026-09-20"]],
                disclosure="仅覆盖 2026-09-01~2026-09-05，2026-09-05 起缺口",
            ),
        )
        status, error = _classify(result)
        self.assertEqual(status, DomainStatus.PARTIAL)
        self.assertIsNone(error)
        self.assertEqual(result.rank_groups, [])

    def test_partial_answers_do_not_add_sql_templates(self):
        text = (pathlib.Path(__file__).parents[1] / "bi_agent" / "metrics.py"
                ).read_text(encoding="utf-8")
        names = re.findall(r"^(_[A-Z0-9_]+SQL) = ", text, re.MULTILINE)
        self.assertEqual(names, [
            "_DAILY_SQL", "_AGG_SQL", "_PRODUCT_SQL", "_GROUP_COUNT_SQL",
            "_COHORT_SQL", "_SHOPS_SQL", "_SYCM_AGG_SQL", "_SYCM_DAILY_SQL",
            "_SYCM_GROUP_COUNT_SQL",
        ])


def _refused_item(**overrides):
    item = {"metric": "ad_spend", "reason": "capability_unavailable"}
    item.update(overrides)
    return item


class RefusedMetricContractTests(unittest.TestCase):
    def test_public_payload_keeps_per_metric_remediation(self):
        from bi_agent.runtime.models import validate_artifact_payload

        payload = validate_artifact_payload({
            "status": "ok",
            "refused": [_refused_item(remediation={
                "kind": "upload", "file_kind": "店铺维度",
                "shop_refs": ["ent-0a1b2c3d"],
                "covered_window": None,
                "missing_range": ["2026-09-01", "2026-09-10"],
            })],
        })
        self.assertEqual(payload["refused"][0]["reason"], "capability_unavailable")
        self.assertEqual(payload["refused"][0]["remediation"]["file_kind"], "店铺维度")

    def test_model_payload_sees_reasons_without_remediation(self):
        from bi_agent.runtime.models import validate_model_payload

        payload = validate_model_payload({
            "status": "missing_data",
            "refused": [
                _refused_item(),
                _refused_item(metric="visitors"),
            ],
        })
        self.assertEqual([item["metric"] for item in payload["refused"]],
                         ["ad_spend", "visitors"])
        with self.assertRaises(ValueError):
            validate_model_payload({
                "status": "missing_data",
                "refused": [_refused_item(remediation={
                    "kind": "upload", "file_kind": "店铺维度",
                    "shop_refs": ["ent-0a1b2c3d"],
                    "missing_range": ["2026-09-01", "2026-09-10"],
                })],
            })

    def test_malformed_refusals_are_rejected(self):
        from bi_agent.runtime.models import validate_artifact_payload

        cases = (
            [],
            [_refused_item(reason="please_upload")],
            [_refused_item(metric="net_profit")],
            [_refused_item(), _refused_item()],
            [_refused_item(note="完整")],
            "ad_spend",
        )
        for case in cases:
            with self.subTest(case=case):
                with self.assertRaises(ValueError):
                    validate_artifact_payload({"status": "missing_data", "refused": case})
