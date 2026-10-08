"""覆盖缺口部分答案的公开契约（计划 2026-10-08 T1）。

行为用例（聚合/比值/库存/关闸逐字一致）在分解函数与查询接线落地前不能绿；
这一层只钉载荷形状：服务端披露文本、模型可见但不可改写、门禁默认关闭。
"""

from __future__ import annotations

import pathlib
import unittest


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
