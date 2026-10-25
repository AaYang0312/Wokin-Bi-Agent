"""T6（计划 2026-10-24）：核心场景集的结构与稳定性检查。

结构：≥30 个场景、10 类每类 ≥3、family 唯一（改写必须同 family）、
split 不跨集。稳定性：串行 ×3 与反序各一遍，结果全过且结构化结果一致
（时间/UUID 等不作逐字比较）。上传类另有一条事实层幂等用例。
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from tests.evaluation.contracts import load_cases

SUITE_PATH = Path(__file__).with_name("evaluation") / "cases" / "operator_core.jsonl"

EXPECTED_CATEGORIES = {
    "时间与覆盖": 3, "口径与比率": 3, "部分答案": 3, "多来源": 3,
    "商品与参考价": 3, "澄清": 3, "授权与多轮": 3, "上传后重查": 3,
    "故障与终态": 3, "图表与终态": 3,
}


def _suite_cases():
    return load_cases(SUITE_PATH)


class SuiteStructureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cases = _suite_cases()

    def test_at_least_thirty_core_cases(self):
        self.assertGreaterEqual(len(self.cases), 30,
                                "核心场景集必须≥30个，不靠同义改写凑数")

    def test_every_category_has_three_independent_conditions(self):
        counts: dict[str, int] = {}
        for case in self.cases:
            counts[case.category] = counts.get(case.category, 0) + 1
        for category, minimum in EXPECTED_CATEGORIES.items():
            self.assertGreaterEqual(
                counts.get(category, 0), minimum,
                f"类别「{category}」至少{minimum}个独立条件，实际"
                f"{counts.get(category, 0)}")
        unexpected = set(counts) - set(EXPECTED_CATEGORIES)
        self.assertEqual(unexpected, set(), f"未登记的类别：{unexpected}")

    def test_family_ids_are_unique(self):
        families = [case.family_id for case in self.cases]
        duplicates = {family for family in families
                      if families.count(family) > 1}
        self.assertEqual(duplicates, set(),
                         "family 重复即同义改写：改写题必须与原题同 family 而不是新开")

    def test_families_do_not_cross_splits(self):
        by_family: dict[str, set[str]] = {}
        for case in self.cases:
            by_family.setdefault(case.family_id, set()).add(case.split)
        crossed = {family: splits for family, splits in by_family.items()
                   if len(splits) > 1}
        self.assertEqual(crossed, {}, "同一 family 不允许跨 regression/holdout")

    def test_money_expectations_are_decimal_exact(self):
        """金额期望必须可被 Decimal 精确表达（不含千分位/单位字）。"""
        for case in self.cases:
            for expected in case.expected:
                for key, value in (expected.get("values") or {}).items():
                    if isinstance(value, str):
                        Decimal(value)  # 非法即失败
                        self.assertNotIn(",", value)
                        self.assertNotIn("元", value)

    def test_partial_answer_cases_declare_the_gate(self):
        for case in self.cases:
            needs_gate = any(
                "refused" in expected
                or expected.get("domain_status") == "partial"
                for expected in case.expected)
            if needs_gate:
                self.assertEqual(
                    case.feature_flags.get("PARTIAL_ANSWERS_ENABLED"), True,
                    f"{case.id} 的期望依赖部分答案语义，必须显式打开该门禁")


@unittest.skipUnless(
    os.getenv("BI_EVALUATION_ADMIN_DSN")
    or (os.getenv("BI_TEST_ADMIN_DSN") or "").endswith("bi_agent_eval_test"),
    "未配置评测沙箱 DSN")
class SuiteStabilityTests(unittest.TestCase):
    """串行 ×3 + 反序：同一数据集、同一配置，结果必须全过且逐场景一致。"""

    @classmethod
    def setUpClass(cls):
        from tests.evaluation.runner import run_suite

        cls.cases = _suite_cases()
        cls.runs = {
            "repeat3": run_suite(cls.cases, mode="offline", repeat=3),
            "reverse": run_suite(list(reversed(cls.cases)), mode="offline",
                                 repeat=1, suite_path=SUITE_PATH),
        }

    def test_all_runs_fully_pass(self):
        for label, summary in self.runs.items():
            self.assertEqual(summary.totals["failed"], 0,
                             f"{label} 存在失败："
                             f"{[c.problems for c in summary.cases if c.problems]}")
            self.assertEqual(summary.totals["blocked"], 0, label)
            self.assertEqual(summary.totals["executed"],
                             summary.totals["attempted"], label)

    def test_status_is_identical_across_orders_and_repeats(self):
        def flat(summary, *, repeat=1):
            return {case.id: case.status for case in summary.cases
                    if case.repeat == repeat}

        first_pass = flat(self.runs["repeat3"])
        for repeat in (2, 3):
            self.assertEqual(flat(self.runs["repeat3"], repeat=repeat),
                             first_pass, f"第{repeat}遍串行结果与第1遍不一致")
        self.assertEqual(flat(self.runs["reverse"]), first_pass,
                         "反序结果与正序不一致：题间存在状态泄漏")

    def test_sycm_facts_survive_double_seed_without_double_counting(self):
        """上传族补充：同一份合成事实写两遍（同窗重传语义），合计不变。

        文件级幂等（同一文件导两遍）由 tests.test_sycm_import 钉住；这里
        钉事实层：种子按导入器同一 upsert 语义执行两遍后，窗口合计仍为 70。
        """
        from tests.acceptance import seed_sycm_manual_case
        from tests.evaluation.database import connect_evaluation_db

        conn = connect_evaluation_db(os.environ["BI_EVALUATION_ADMIN_DSN"])
        try:
            import time

            from tests.test_db import FROZEN_NOW

            from bi_agent.metrics import QueryRequest, query_business

            seed_sycm_manual_case(conn)
            seed_sycm_manual_case(conn)
            result = query_business(
                conn,
                QueryRequest(start=FROZEN_NOW.date().replace(day=1),
                             end=FROZEN_NOW.date().replace(day=8),
                             shop_ids=["SY1"], metrics=["ad_spend"]),
                allowed_shop_ids=frozenset({"SY1"}),
                deadline=time.monotonic() + 30, now=FROZEN_NOW)
            self.assertEqual(result.status, "ok", result.limitations)
            self.assertEqual([Decimal(str(row.get("ad_spend")))
                              for row in result.data],
                             [Decimal("70")],
                             "同窗事实写两遍后合计必须仍是70，不是140")
        finally:
            conn.rollback()
            conn.close()


@unittest.skipUnless(
    os.getenv("BI_EVALUATION_ADMIN_DSN")
    or (os.getenv("BI_TEST_ADMIN_DSN") or "").endswith("bi_agent_eval_test"),
    "未配置评测沙箱 DSN")
class ReviewMutationTests(unittest.TestCase):
    """复核报告（2026-10-26）坏例回归：删步骤/拼错键必须翻红，不允许假通过。

    这些用例固定住复核中证明过的两条假通过路径：
    F1（oc-004 删掉首次 all_authorized 调用仍通过）与 F2（expected 键拼错
    仍通过）。任何一条变绿都表示判定器又退回到"声明了但不检查"。
    """

    @classmethod
    def setUpClass(cls):
        cls.cases = {case.id: case for case in _suite_cases()}

    def _mutant(self, case_id, mutate):
        payload = self.cases[case_id].model_dump()
        mutate(payload)
        return payload

    def test_f1_removing_the_first_required_call_from_oc004_fails(self):
        """删掉"首次请求"脚本步骤后，剩余单次换平台调用不满足两步确认合同。"""
        from tests.evaluation.runner import run_suite

        def drop_first_call(payload):
            # 首个脚本回复是 all_authorized 的毛利请求；删掉后第一轮只剩
            # 换平台重试一次。期望保持 call_count=2 不变。
            assert payload["scripted_replies"][0]["tool_calls"]
            payload["scripted_replies"].pop(0)

        summary = run_suite(
            [self._mutant("oc-004", drop_first_call)],
            mode="offline", repeat=1)
        result = summary.cases[0]
        self.assertNotEqual(result.status, "pass",
                             "缺少首次调用的场景不允许通过（复核 F1 假通过）")
        self.assertTrue(any("调用" in problem or "needs_input" in problem
                            for problem in result.problems),
                        result.problems)

    def test_f1_oc004_asserts_every_call_was_needs_input(self):
        """正例锁：原始 oc-004 的两次门禁拒答都被逐次断言。"""
        case = self.cases["oc-004"]
        self.assertEqual(case.expected[0].get("call_count"), 2)
        self.assertEqual(case.expected[0].get("call_statuses"),
                         ["needs_input", "needs_input"])

    def test_f2_misspelled_expected_key_is_rejected_at_load(self):
        """valuez 拼写错误在 load_cases 就拒绝，轮不到数据库或模型。"""
        def misspell(payload):
            payload["expected"][0]["valuez"] = {"paid_amount": "0"}
            payload["expected"][0].pop("values", None)

        bad = self._mutant("oc-001", misspell)
        with tempfile.TemporaryDirectory() as tmp:
            suite = Path(tmp) / "bad.jsonl"
            suite.write_text(json.dumps(bad, ensure_ascii=False) + "\n",
                             encoding="utf-8")
            with self.assertRaises(ValueError) as caught:
                load_cases(suite)
            self.assertIn("valuez", str(caught.exception))

    def test_f2_verifier_rejects_unconsumed_expected_keys(self):
        """直接调用判定器时，未登记的期望键也是问题（第二道防线）。"""
        from tests.evaluation.verification import verify_expected

        problems = verify_expected(
            {"valuez": {"paid_amount": "0"}}, {"rows": []})
        self.assertTrue(problems, "拼错的期望键不能被静默忽略")

    def test_f2_registered_keys_cover_the_legacy_question_contract(self):
        """键注册表必须覆盖既有 35 题用到的全部期望键（不因登记不全误伤）。"""
        from tests.evaluation.verification import EXPECTED_KEYS

        legacy = Path(__file__).with_name("questions.jsonl")
        used: set[str] = set()
        for line in legacy.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            question = json.loads(line)
            expected_list = (question["expected"]
                             if isinstance(question["expected"], list)
                             else [question["expected"]])
            for expected in expected_list:
                used.update(expected)
        unknown = used - EXPECTED_KEYS
        self.assertEqual(unknown, set(),
                         f"35题使用了未登记期望键：{sorted(unknown)}")


if __name__ == "__main__":
    unittest.main()
