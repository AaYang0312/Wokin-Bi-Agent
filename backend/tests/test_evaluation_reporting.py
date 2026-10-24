"""T3（计划 2026-10-24）：报告层——分层、脱敏、不覆盖、失败不冒充成功。"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tests.evaluation.contracts import CaseResult, EvaluationSummary
from tests.evaluation.reporting import write_report


def _summary(**overrides) -> EvaluationSummary:
    base = dict(
        mode="offline",
        note="脚本模型下的协议与业务回归，不衡量自然语言理解。",
        dataset_sha256="0" * 64,
        source={"git_head": "deadbeef", "git_dirty": True,
                "diff_summary": None},
        frozen_time="2026-09-08T09:00:00+08:00",
        flags={"PARTIAL_ANSWERS_ENABLED": False},
        repeat=1,
        command="python -m tests.evaluation.runner --mode offline ...",
        totals={"attempted": 2, "executed": 1, "passed": 1, "failed": 0,
                "blocked": 1, "skipped": 0, "human_review_pending": 0,
                "pass_rate": 1.0},
        usage={"status": "unavailable", "totals": None, "known": None,
               "source": "synthetic"},
        cases=[
            CaseResult(id="case-001", repeat=1, status="pass", problems=[],
                       duration_ms=12.5, model_call_count=2,
                       tool_call_count=1, run_count=1, artifact_count=0,
                       error_class=None, usage_source="synthetic"),
            CaseResult(id="case-002", repeat=1, status="blocked",
                       problems=["未配置评测沙箱 DSN"], duration_ms=0.0,
                       model_call_count=0, tool_call_count=0, run_count=0,
                       artifact_count=0, error_class="EvaluationTargetError",
                       usage_source=None),
        ],
    )
    base.update(overrides)
    return EvaluationSummary(**base)


class WriteReportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.out = Path(self.tmp.name) / "run-001"

    def test_writes_three_layers(self):
        write_report(_summary(), self.out)
        cases = [json.loads(line) for line in
                 (self.out / "cases.jsonl").read_text("utf-8").splitlines()]
        self.assertEqual(len(cases), 2)
        summary = json.loads((self.out / "summary.json").read_text("utf-8"))
        self.assertEqual(summary["mode"], "offline")
        self.assertIn("不衡量自然语言理解", summary["note"])
        markdown = (self.out / "report.md").read_text("utf-8")
        self.assertIn("offline", markdown)
        self.assertIn("脚本模型", markdown)

    def test_refuses_overwrite_existing_run(self):
        write_report(_summary(), self.out)
        with self.assertRaises(ValueError):
            write_report(_summary(), self.out)

    def test_failed_case_problems_in_markdown(self):
        summary = _summary()
        summary.cases[0].status = "fail"
        summary.cases[0].problems = ["paid_amount期望1000，实际999"]
        write_report(summary, self.out)
        markdown = (self.out / "report.md").read_text("utf-8")
        self.assertIn("paid_amount期望1000", markdown)

    def test_exception_text_is_sanitized_not_serialized(self):
        summary = _summary()
        summary.cases[0].status = "error"
        summary.cases[0].problems = [
            "异常：OperationalError: 连接失败 "
            "postgresql://postgres:TopSecret@127.0.0.1:55439/x "
            "password=AlsoSecret"]
        write_report(summary, self.out)
        cases = (self.out / "cases.jsonl").read_text("utf-8")
        self.assertNotIn("TopSecret", cases)
        self.assertNotIn("AlsoSecret", cases)

    def test_zero_executed_pass_rate_is_null(self):
        summary = _summary(totals={"attempted": 1, "executed": 0, "passed": 0,
                                   "failed": 0, "blocked": 1, "skipped": 0,
                                   "human_review_pending": 0,
                                   "pass_rate": None})
        write_report(summary, self.out)
        summary_json = json.loads((self.out / "summary.json").read_text("utf-8"))
        self.assertIsNone(summary_json["totals"]["pass_rate"])
        markdown = (self.out / "report.md").read_text("utf-8")
        self.assertNotIn("100%", markdown)

    def test_human_review_pending_is_visible(self):
        summary = _summary(totals={"attempted": 1, "executed": 1, "passed": 1,
                                   "failed": 0, "blocked": 0, "skipped": 0,
                                   "human_review_pending": 1,
                                   "pass_rate": 1.0})
        write_report(summary, self.out)
        markdown = (self.out / "report.md").read_text("utf-8")
        self.assertIn("human_review_pending", markdown)

    def test_source_version_recorded(self):
        write_report(_summary(), self.out)
        summary = json.loads((self.out / "summary.json").read_text("utf-8"))
        self.assertIn("git_head", summary["source"])
        self.assertIn("git_dirty", summary["source"])
        self.assertNotIn("os_environ", summary)


if __name__ == "__main__":
    unittest.main()
