"""T3（计划 2026-10-24）：扩展 runner、计量与结构化报告。

DB-free 部分任何时候都跑；连库部分只在配置了 ``BI_EVALUATION_ADMIN_DSN``（或
指向沙箱的 ``BI_TEST_ADMIN_DSN``）时跑。所有断言针对结构化字段，不做整份
payload 的子串扫描。
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from tests.evaluation.contracts import (
    FEATURE_FLAG_REGISTRY,
    EvaluationCase,
    load_cases,
)


def _case_line(**overrides) -> dict:
    from bi_agent.catalog import ref_for_key

    s1_ref = ref_for_key("shop", "S1")
    case = {
        "id": "case-001",
        "category": "时间与覆盖",
        "family_id": "f-window",
        "split": "regression",
        "turns": [f"店铺{s1_ref}最近7天支付金额是多少？"],
        "expected": [{"tool": "query_business",
                      "status": "ok",
                      "values": {"paid_amount": "1000"}}],
        "seed_names": ["multi_source"],
        "allowed": ["S1"],
        "feature_flags": {},
        "scripted_replies": [
            {"tool_calls": [{"name": "query_business",
                             "arguments": {"shop_ids": [s1_ref],
                                           "metrics": ["paid_amount"]},
                             "call_id": "call_1"}]},
            {"text": "最近7天支付1000元。",
             "usage": {"prompt_tokens": 10, "completion_tokens": 5,
                       "total_tokens": 15}},
        ],
        "requires_human_review": False,
    }
    case.update(overrides)
    return case


def _write_suite(path: Path, lines: list[dict]) -> Path:
    path.write_text("\n".join(json.dumps(line, ensure_ascii=False)
                              for line in lines) + "\n", encoding="utf-8")
    return path


class LoadCasesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "suite.jsonl"

    def test_valid_case_loads(self):
        load_cases(_write_suite(self.path, [_case_line()]))
        cases = load_cases(self.path)
        self.assertEqual(len(cases), 1)
        self.assertIsInstance(cases[0], EvaluationCase)
        self.assertEqual(cases[0].id, "case-001")

    def test_duplicate_ids_rejected(self):
        suite = _write_suite(self.path, [_case_line(), _case_line()])
        with self.assertRaises(ValueError):
            load_cases(suite)

    def test_unknown_seed_rejected_before_any_execution(self):
        suite = _write_suite(self.path, [_case_line(seed_names=["not_a_seed"])])
        with self.assertRaises(ValueError) as caught:
            load_cases(suite)
        self.assertIn("not_a_seed", str(caught.exception))

    def test_unknown_field_rejected(self):
        suite = _write_suite(self.path, [_case_line(extra_field=1)])
        with self.assertRaises(ValueError):
            load_cases(suite)

    def test_expected_length_mismatch_rejected(self):
        suite = _write_suite(self.path, [_case_line(
            turns=["问一", "问二"],
            expected=[{"tool": "query_business", "status": "ok"}])])
        with self.assertRaises(ValueError) as caught:
            load_cases(suite)
        self.assertIn("turns长度", str(caught.exception))

    def test_empty_turns_rejected(self):
        suite = _write_suite(self.path, [_case_line(turns=[], expected=[])])
        with self.assertRaises(ValueError):
            load_cases(suite)

    def test_bad_split_rejected(self):
        suite = _write_suite(self.path, [_case_line(split="benchmark")])
        with self.assertRaises(ValueError):
            load_cases(suite)

    def test_unregistered_feature_flag_rejected(self):
        suite = _write_suite(self.path, [_case_line(
            feature_flags={"NOT_A_REGISTERED_GATE": True})])
        with self.assertRaises(ValueError) as caught:
            load_cases(suite)
        self.assertIn("NOT_A_REGISTERED_GATE", str(caught.exception))
        self.assertIn("PARTIAL_ANSWERS_ENABLED", FEATURE_FLAG_REGISTRY)

    def test_case_id_required(self):
        line = _case_line()
        del line["id"]
        suite = _write_suite(self.path, [line])
        with self.assertRaises(ValueError):
            load_cases(suite)


class MeteredModelTests(unittest.TestCase):
    def _reply(self, usage=None):
        from tests.evaluation.telemetry import build_scripted_reply

        return build_scripted_reply({"text": "完成", "usage": usage})

    def test_protocol_passthrough(self):
        from tests.evaluation.telemetry import MeteredModel, ScriptedModel

        scripted = ScriptedModel([self._reply()])
        metered = MeteredModel(scripted)
        from bi_agent.llm import Message

        reply = metered.complete([Message(role="user", content="问")],
                                 [], timeout_s=1.5)
        self.assertEqual(reply.text, "完成")
        self.assertEqual(metered.call_count, 1)
        self.assertGreaterEqual(metered.model_duration_ms, 0.0)

    def test_script_exhaustion_raises(self):
        from tests.evaluation.telemetry import MeteredModel, ScriptedModel

        metered = MeteredModel(ScriptedModel([]))
        from bi_agent.llm import Message

        with self.assertRaises(Exception):
            metered.complete([Message(role="user", content="问")],
                             [], timeout_s=1.0)

    def test_reply_message_is_usable(self):
        from tests.evaluation.telemetry import build_scripted_reply

        reply = build_scripted_reply({
            "tool_calls": [{"name": "query_business",
                            "arguments": {"metrics": ["paid_amount"]},
                            "call_id": "c1"}]})
        message = reply.as_message()
        self.assertEqual(message.role, "assistant")
        self.assertEqual(message.tool_calls[0].name, "query_business")

    def test_usage_aggregation_complete(self):
        from tests.evaluation.telemetry import aggregate_usage

        agg = aggregate_usage([
            {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            {"prompt_tokens": 20, "completion_tokens": 7, "total_tokens": 27},
        ])
        self.assertEqual(agg["status"], "complete")
        self.assertEqual(agg["totals"],
                         {"prompt_tokens": 30, "completion_tokens": 12,
                          "total_tokens": 42})

    def test_usage_aggregation_partial_keeps_known_and_nulls_total(self):
        from tests.evaluation.telemetry import aggregate_usage

        agg = aggregate_usage([
            {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            None,
            {"prompt_tokens": 3, "completion_tokens": None,
             "total_tokens": None},
        ])
        self.assertEqual(agg["status"], "partial")
        self.assertIsNone(agg["totals"])
        self.assertEqual(agg["known"]["prompt_tokens"], 13)
        self.assertEqual(agg["known"]["completion_tokens"], 5)

    def test_usage_aggregation_unavailable(self):
        from tests.evaluation.telemetry import aggregate_usage

        agg = aggregate_usage([None, None])
        self.assertEqual(agg["status"], "unavailable")
        self.assertIsNone(agg["totals"])
        self.assertIsNone(agg["known"])

    def test_failed_model_call_is_counted_as_missing_usage(self):
        """复核 F5：调用抛异常时 usage 必须记为缺失，不能从聚合里消失。"""
        from tests.evaluation.telemetry import (
            MeteredModel,
            ScriptedModel,
            aggregate_usage,
        )
        from tests.evaluation.contracts import ScriptedReply

        from bi_agent.llm import Message

        metered = MeteredModel(ScriptedModel([
            ScriptedReply(error="timeout"),
            ScriptedReply(text="好了",
                          usage={"prompt_tokens": 10,
                                 "completion_tokens": 5,
                                 "total_tokens": 15}),
        ]))
        messages = [Message(role="user", content="问")]
        with self.assertRaises(Exception):
            metered.complete(messages, [], timeout_s=1.0)
        metered.complete(messages, [], timeout_s=1.0)
        self.assertEqual(metered.call_count, 2)
        self.assertEqual(len(metered.usage_records), 2,
                         "两次调用都要留下记录（失败=缺失）")
        agg = aggregate_usage(metered.usage_records)
        self.assertEqual(agg["status"], "partial")
        self.assertIsNone(agg["totals"])
        self.assertEqual(agg["known"],
                         {"prompt_tokens": 10, "completion_tokens": 5,
                          "total_tokens": 15})


class SanitizeTests(unittest.TestCase):
    def test_dsn_password_never_survives(self):
        from tests.evaluation.reporting import sanitize

        dirty = ("连接失败：postgresql://postgres:SuPerSecret@127.0.0.1:55439/x "
                 "password=AnotherSecret")
        clean = sanitize(dirty)
        self.assertNotIn("SuPerSecret", clean)
        self.assertNotIn("AnotherSecret", clean)
        self.assertIn("postgresql://", clean)

    def test_quoted_password_with_spaces_is_fully_masked(self):
        """复核 F8：libpq 合法的带引号含空格密码必须整段脱敏。"""
        from tests.evaluation.reporting import sanitize

        dirty = "host=localhost password='Alpha SecretTail' dbname=demo"
        clean = sanitize(dirty)
        self.assertNotIn("SecretTail", clean)
        self.assertNotIn("Alpha", clean)
        self.assertIn("password=", clean)
        dirty_double = 'host=localhost password="Beta OtherTail" dbname=demo'
        clean_double = sanitize(dirty_double)
        self.assertNotIn("OtherTail", clean_double)
        self.assertNotIn("Beta", clean_double)

    def test_plain_text_untouched(self):
        from tests.evaluation.reporting import sanitize

        self.assertEqual(sanitize("支付金额1000元"), "支付金额1000元")


class BlockedPathReportTests(unittest.TestCase):
    """复核 F9：环境阻塞也要落报告，attempted/blocked 按 repeat 统一。"""

    def test_blocked_run_writes_report_and_counts_repeats(self):
        import json
        from unittest import mock

        from tests.evaluation import runner as runner_module
        from tests.evaluation.runner import evaluate_exit_code, run_suite

        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "blocked-run"
            with mock.patch.object(runner_module, "_suite_dsn", return_value=None):
                summary = run_suite([_case_line()], mode="offline", repeat=3,
                                    output_dir=output)
            self.assertEqual(summary.totals["attempted"], 3)
            self.assertEqual(summary.totals["blocked"], 3)
            self.assertEqual(summary.totals["executed"], 0)
            self.assertIsNone(summary.totals["pass_rate"])
            self.assertEqual(evaluate_exit_code(summary), 2)
            for name in ("cases.jsonl", "summary.json", "report.md"):
                self.assertTrue((output / name).exists(), name)
            body = json.loads((output / "summary.json").read_text("utf-8"))
            self.assertEqual(body["totals"]["blocked"], 3)


class ExitCodeTests(unittest.TestCase):
    """环境/授权未就绪必须退出 2，不能当失败跑一半。"""

    def test_missing_mode_is_rejected(self):
        from tests.evaluation.runner import main

        with self.assertRaises(SystemExit):
            main(["--suite", "x"])

    def test_live_without_allow_live_exits_2(self):
        from tests.evaluation.runner import main

        with tempfile.TemporaryDirectory() as tmp:
            code = main(["--mode", "live", "--suite", str(_stub_suite(Path(tmp))),
                         "--output", str(Path(tmp) / "run")])
        self.assertEqual(code, 2)

    def test_unknown_mode_rejected(self):
        from tests.evaluation.runner import main

        with tempfile.TemporaryDirectory() as tmp:
            code = main(["--mode", "benchmark", "--suite",
                         str(_stub_suite(Path(tmp))), "--output",
                         str(Path(tmp) / "run")])
        self.assertEqual(code, 2)

    def test_bad_suite_path_exits_2(self):
        from tests.evaluation.runner import main

        with tempfile.TemporaryDirectory() as tmp:
            code = main(["--mode", "offline", "--suite",
                         str(Path(tmp) / "missing.jsonl"), "--output",
                         str(Path(tmp) / "run")])
        self.assertEqual(code, 2)


def _stub_suite(directory: Path) -> Path:
    return _write_suite(directory / "stub.jsonl", [_case_line()])


@unittest.skipUnless(
    os.getenv("BI_EVALUATION_ADMIN_DSN")
    or (os.getenv("BI_TEST_ADMIN_DSN") or "").endswith("bi_agent_eval_test"),
    "未配置评测沙箱 DSN")
class FailureAccountingTests(unittest.TestCase):
    """复核 F6/F7：错误计入分母；准备失败保留原异常并清理。"""

    @classmethod
    def setUpClass(cls):
        from tests.evaluation.runner import run_suite

        cls.tmp = tempfile.TemporaryDirectory()
        # 一条正常场景 + 一条脚本耗尽（第二个模型回合无回复）的场景
        exhausted = _case_line(id="case-exhaust")
        exhausted["scripted_replies"] = exhausted["scripted_replies"][:1]
        cls.summary = run_suite([_case_line(), exhausted], mode="offline",
                                repeat=1, output_dir=Path(cls.tmp.name) / "run")

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_f6_errors_count_in_the_executed_denominator(self):
        totals = self.summary.totals
        self.assertEqual(totals["attempted"], 2)
        self.assertEqual(totals["executed"], 2,
                         "执行错误也是已执行的尝试，不得从分母里消失")
        self.assertEqual(totals["passed"], 1)
        self.assertEqual(totals["failed"], 1)
        self.assertAlmostEqual(totals["pass_rate"], 0.5,
                               msg="含错误的运行不得呈现为100%通过")

    def test_f6_exit_code_still_signals_failure(self):
        from tests.evaluation.runner import evaluate_exit_code

        self.assertEqual(evaluate_exit_code(self.summary), 1)


@unittest.skipUnless(
    os.getenv("BI_EVALUATION_ADMIN_DSN")
    or (os.getenv("BI_TEST_ADMIN_DSN") or "").endswith("bi_agent_eval_test"),
    "未配置评测沙箱 DSN")
class SeedFailureHandlingTests(unittest.TestCase):
    """复核 F7：seed/模型工厂抛错要保留原异常并完成逐题清理。"""

    def test_seed_failure_keeps_original_error_and_releases_savepoint(self):
        from unittest import mock

        from tests import acceptance as acceptance_module
        from tests.evaluation.runner import run_suite

        def broken_seed(_conn):
            raise RuntimeError("合成种子爆炸")

        case = _case_line()
        case["seed_names"] = ["multi_source"]
        with mock.patch.dict(acceptance_module.SEED_STEPS,
                             {"multi_source": broken_seed}):
            summary = run_suite([case], mode="offline", repeat=1)
        result = summary.cases[0]
        self.assertEqual(result.status, "error")
        self.assertEqual(result.error_class, "RuntimeError",
                         "原始异常类别必须保留，不得被 UnboundLocalError 覆盖")
        self.assertTrue(any("合成种子爆炸" in problem
                            for problem in result.problems), result.problems)

        # 同一连接上紧接着再跑一次正常场景：savepoint 已释放，库可用
        with mock.patch.dict(acceptance_module.SEED_STEPS):
            del acceptance_module.SEED_STEPS["multi_source"]
            acceptance_module.SEED_STEPS["multi_source"] = (
                acceptance_module.seed_multi_source_case)
            replay = run_suite([_case_line()], mode="offline", repeat=1)
        self.assertEqual(replay.cases[0].status, "pass",
                         "前题清理失败会拖垮后续场景的连接状态")


@unittest.skipUnless(
    os.getenv("BI_EVALUATION_ADMIN_DSN")
    or (os.getenv("BI_TEST_ADMIN_DSN") or "").endswith("bi_agent_eval_test"),
    "未配置评测沙箱 DSN")
class RunSuiteDatabaseTests(unittest.TestCase):
    """三个小型场景跑通 offline 全链路（合成库上串行执行）。"""

    @classmethod
    def setUpClass(cls):
        from tests.evaluation.runner import run_suite

        from bi_agent.catalog import ref_for_key

        cls.tmp = tempfile.TemporaryDirectory()
        s1_ref = ref_for_key("shop", "S1")
        cases = [
            _case_line(),
            _case_line(id="case-002", family_id="f-window",
                       expected=[{"tool": "query_business", "status": "ok",
                                  "values": {"paid_amount": "999"}}]),
            _case_line(id="case-003", family_id="f-refuse",
                       expected=[{"tool": "query_business",
                                  "status": "missing_data"}],
                       scripted_replies=[
                           {"tool_calls": [{"name": "query_business",
                                            "arguments": {
                                                "shop_ids": [s1_ref],
                                                "metrics": ["paid_amount"],
                                                "start": "2026-10-01",
                                                "end": "2026-10-08"},
                                            "call_id": "call_1"}]},
                           {"text": "该窗口没有数据。"}],
                       requires_human_review=False),
        ]
        cls.summary = run_suite(cases, mode="offline", repeat=1,
                                output_dir=Path(cls.tmp.name) / "run")

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_summary_counts(self):
        summary = self.summary.model_dump()
        self.assertEqual(summary["totals"]["attempted"], 3)
        self.assertEqual(summary["totals"]["executed"], 3)
        # case-001（数值正确）与 case-003（越窗正确拒答）通过；
        # case-002 期望值故意写错，必须失败。
        self.assertEqual(summary["totals"]["passed"], 2)
        self.assertEqual(summary["totals"]["failed"], 1)
        self.assertEqual(summary["totals"]["blocked"], 0)

    def test_pass_rate_reflects_failures(self):
        totals = self.summary.model_dump()["totals"]
        self.assertAlmostEqual(totals["pass_rate"], 2 / 3)

    def test_case_results_carry_telemetry(self):
        first = next(case for case in self.summary.cases
                     if case.id == "case-001")
        self.assertEqual(first.status, "pass")
        self.assertGreaterEqual(first.model_call_count, 2)
        self.assertGreaterEqual(first.tool_call_count, 1)
        self.assertGreaterEqual(first.duration_ms, 0.0)
        self.assertIsNotNone(first.usage_source)

    def test_wrong_expectation_fails_with_problem(self):
        wrong = next(case for case in self.summary.cases
                     if case.id == "case-002")
        self.assertEqual(wrong.status, "fail")
        self.assertTrue(any("paid_amount" in problem
                            for problem in wrong.problems))

    def test_usage_totals_synthetic(self):
        usage = self.summary.model_dump()["usage"]
        self.assertEqual(usage["source"], "synthetic")
        self.assertEqual(usage["status"], "partial")  # 首条回复未声明 usage

    def test_reports_written(self):
        from tests.evaluation.reporting import write_report

        out = Path(self.tmp.name) / "reports"
        write_report(self.summary, out)
        self.assertTrue((out / "cases.jsonl").exists())
        self.assertTrue((out / "summary.json").exists())
        self.assertTrue((out / "report.md").exists())
        summary_json = json.loads((out / "summary.json").read_text("utf-8"))
        self.assertIn("dataset_sha256", summary_json)
        self.assertIn("source", summary_json)
        with self.assertRaises(ValueError):
            write_report(self.summary, out)  # 已有 run 目录不覆盖


if __name__ == "__main__":
    unittest.main()
