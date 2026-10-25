"""扩展评测 runner（计划 2026-10-24 T3）。

串行执行 ``EvaluationCase``：每题独立会话、授权集、运行存储与 savepoint，
多轮场景共享该题的 SessionState，题间不共享。判定复用 acceptance 的
``_verify_turn`` / ``verify_commerce_turn``（同一实现，无第二套语义）。

退出码：全部满足为 0；断言失败/执行错误为 1；环境未就绪、未授权 live、
必需人工复核等未完成状态为 2。live 模式默认关闭（``--allow-live`` 才放开，
且完全不向模型发送 expected/scripted_replies——脚本在 live 下被忽略）。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from tests.evaluation.contracts import (
    CaseResult,
    EvaluationCase,
    EvaluationSummary,
    load_cases,
)
from tests.evaluation.database import EvaluationTargetError
from tests.evaluation.reporting import sanitize, write_report
from tests.evaluation.telemetry import (
    MeteredModel,
    ScriptExhaustedError,
    ScriptedModel,
    aggregate_usage,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
OFFLINE_NOTE = ("脚本模型下的协议与业务回归，不衡量自然语言理解；"
                "不构成任何平台真实来源就绪的证据。")
LIVE_NOTE = ("真实模型在合成测试库上的表现；澄清与因果边界仍需人工核看，"
             "不与其他模式混算。")


def _suite_dsn() -> str | None:
    evaluation = os.getenv("BI_EVALUATION_ADMIN_DSN")
    if evaluation:
        return evaluation
    shared = os.getenv("BI_TEST_ADMIN_DSN") or ""
    # 只有明确指向评测沙箱的共享变量名才接受；bi_agent_test 不进本 runner。
    if shared.endswith("bi_agent_eval_test"):
        return shared
    return None


def _git_source() -> dict:
    def run(*args: str) -> str:
        try:
            result = subprocess.run(
                ["git", "-C", str(REPO_ROOT), *args], capture_output=True,
                text=True, timeout=10, check=True)
            return result.stdout.strip()
        except (OSError, subprocess.SubprocessError):
            return "unknown"

    dirty = False
    status = run("status", "--porcelain")
    if status not in ("", "unknown"):
        dirty = True
    return {"git_head": run("rev-parse", "HEAD"), "git_dirty": dirty,
            "diff_summary": None}


def _dataset_sha256(cases: list[EvaluationCase], suite_path: Path | None) -> str:
    if suite_path is not None:
        return hashlib.sha256(suite_path.read_bytes()).hexdigest()
    canonical = json.dumps([case.model_dump() for case in cases],
                           ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _blocked_summary(cases: list[EvaluationCase], *, mode: str, repeat: int,
                     reason: str, suite_path: Path | None,
                     command: str) -> EvaluationSummary:
    note = OFFLINE_NOTE if mode == "offline" else LIVE_NOTE
    # attempted/blocked 与正常路径同一口径：场景数 × 重复次数（复核 F9）。
    total = len(cases) * repeat
    return EvaluationSummary(
        mode=mode, note=note,
        dataset_sha256=_dataset_sha256(cases, suite_path),
        source=_git_source(), frozen_time=_frozen_time_iso(),
        flags={}, repeat=repeat, command=sanitize(command),
        totals={"attempted": total, "executed": 0, "passed": 0,
                "failed": 0, "blocked": total, "skipped": 0,
                "human_review_pending": 0, "pass_rate": None},
        usage={"status": "unavailable", "totals": None, "known": None,
               "source": "synthetic" if mode == "offline" else "model"},
        cases=[CaseResult(
            id=case.id, repeat=repetition, status="blocked",
            problems=[sanitize(reason)], duration_ms=0.0,
            model_call_count=0, tool_call_count=0, run_count=0,
            artifact_count=0, error_class="EvaluationTargetError",
            usage_source=None)
            for repetition in range(1, repeat + 1) for case in cases])


def _frozen_time_iso() -> str:
    from tests.test_db import FROZEN_NOW

    return FROZEN_NOW.isoformat()


def _seed_acceptance_base(conn) -> None:
    """与 acceptance.run_offline 完全同一套合成基准种子。"""
    from tests.acceptance import seed_multi_source_case
    from tests.test_db import ALL_CAPABILITIES, seed_business_case, set_capabilities

    seed_business_case(conn)
    seed_multi_source_case(conn)
    conn.execute(
        "INSERT INTO bi.shops(shop_id, platform, display_name) "
        "VALUES ('S9','fxg','店铺A') ON CONFLICT (shop_id) DO NOTHING")
    set_capabilities(conn, "S9", *ALL_CAPABILITIES)


def _run_case(conn, case: EvaluationCase, repetition: int,
              model_factory) -> tuple[CaseResult, list[dict | None]]:
    from unittest.mock import patch

    from tests.acceptance import (SEED_STEPS, _memory_store, _run_turn,
                                  _verify_commerce_turn, _verify_turn)
    from tests.test_db import FROZEN_NOW

    import bi_agent.metrics as metrics_module
    from bi_agent.agent import SessionState
    from bi_agent.agent import execute_commerce_tool as real_commerce

    real_query = metrics_module.query_business
    started = time.monotonic()
    recorded: list = []
    recorded_commerce: list = []
    run_count = 0
    artifact_count = 0
    problems: list[str] = []
    status = "pass"
    error_class: str | None = None
    metered: MeteredModel | None = None
    conn.execute("SAVEPOINT evaluation_case")

    def spy(connection, request, **kwargs):
        recorded.append(request)
        return real_query(connection, request, **kwargs)

    def commerce_spy(call, context):
        execution = real_commerce(call, context)
        recorded_commerce.append({
            "arguments": dict(call.arguments or {}),
            "payload": execution.domain_result.model_payload,
        })
        return execution

    try:
        # seed 与模型工厂都在清理边界内：任何一步抛错都保留原始异常类别，
        # 并按题回滚/释放存点（复核 F7：不再被 UnboundLocalError 覆盖）。
        for step in case.seed_names:
            SEED_STEPS[step](conn)
        state = SessionState(subject=f"evaluation-{case.id}-{repetition}")
        allowed = frozenset(case.allowed or ["S1"])
        metered = model_factory(case)
        with patch("bi_agent.business_query.nodes.metrics.query_business",
                   side_effect=spy),                     patch("bi_agent.agent.execute_commerce_tool",
                      side_effect=commerce_spy):
            for turn_text, expected in zip(case.turns, case.expected):
                store = _memory_store(allowed)
                recorded_start = len(recorded)
                commerce_start = len(recorded_commerce)
                turn = _run_turn(
                    turn_text, state, metered, conn, allowed, FROZEN_NOW,
                    store,
                    partial_answers_enabled=bool(
                        case.feature_flags.get("PARTIAL_ANSWERS_ENABLED",
                                               False)))
                state = turn.state
                run_count += len(store.runs)
                artifact_count += len(store.artifacts)
                problems.extend(_verify_turn(
                    expected, turn, recorded, store, recorded_start, allowed,
                    commerce_calls=recorded_commerce[commerce_start:]))
                problems.extend(_verify_commerce_turn(
                    expected, recorded_commerce[commerce_start:]))
                if problems:
                    break
        inner = metered.inner
        if isinstance(inner, ScriptedModel) and inner.remaining:
            problems.append(
                f"脚本回复未用完（剩{inner.remaining}条）——场景合同与实际"
                "执行不一致")
    except ScriptExhaustedError as exc:
        status, error_class = "error", "ScriptExhaustedError"
        problems.append(sanitize(str(exc)))
    except Exception as exc:  # noqa: BLE001
        status, error_class = "error", type(exc).__name__
        problems.append(sanitize(f"异常：{type(exc).__name__}: {exc}"[:300]))
    finally:
        # 逐题清理在一切路径上执行：seed 失败、模型工厂失败、执行失败 alike。
        conn.execute("ROLLBACK TO evaluation_case")
        conn.execute("RELEASE SAVEPOINT evaluation_case")
    if status == "pass" and problems:
        status = "fail"
    duration_ms = (time.monotonic() - started) * 1000.0
    usage_source = None
    if metered is not None:
        usage_source = ("synthetic" if isinstance(metered.inner, ScriptedModel)
                        else "model")
    result = CaseResult(
        id=case.id, repeat=repetition, status=status,
        problems=[sanitize(problem) for problem in problems],
        duration_ms=round(duration_ms, 3),
        model_call_count=metered.call_count if metered else 0,
        tool_call_count=len(recorded) + len(recorded_commerce),
        run_count=run_count, artifact_count=artifact_count,
        error_class=error_class,
        usage_source=usage_source)
    return result, list(metered.usage_records) if metered else []


def run_suite(cases: list[EvaluationCase], *, mode: str, repeat: int,
              output_dir: Path | None = None,
              suite_path: Path | None = None,
              command: str = "") -> EvaluationSummary:
    """串行运行场景；环境未就绪时全部记 blocked（不是失败）。

    ``cases`` 也接受未打包的 dict（入口统一按契约校验后转 EvaluationCase）。
    """
    import psycopg

    from tests.evaluation.database import connect_evaluation_db

    if mode not in ("offline", "live"):
        raise ValueError(f"未知模式：{mode}")
    cases = [case if isinstance(case, EvaluationCase) else EvaluationCase(**case)
             for case in cases]
    def blocked(reason: str) -> EvaluationSummary:
        # 阻塞也要落报告（复核 F9）：退出码 2 的同时保留可查的结构化结果。
        summary = _blocked_summary(
            cases, mode=mode, repeat=repeat, reason=reason,
            suite_path=suite_path, command=command)
        if output_dir is not None:
            write_report(summary, Path(output_dir))
        return summary

    dsn = _suite_dsn()
    if not dsn:
        return blocked("未配置评测沙箱DSN（BI_EVALUATION_ADMIN_DSN）")

    live_model = None
    if mode == "live":
        try:
            from bi_agent.config import load_model_settings
            from bi_agent.llm import create_model

            live_model = create_model(load_model_settings(os.environ))
        except (OSError, ValueError) as exc:
            return blocked(f"live模型配置缺失：{sanitize(str(exc)[:200])}")

    def model_factory(case: EvaluationCase) -> MeteredModel:
        if mode == "live":
            return MeteredModel(live_model)
        return MeteredModel(ScriptedModel(case.scripted_replies,
                                          case_id=case.id))

    try:
        conn = connect_evaluation_db(dsn)
    except EvaluationTargetError as exc:
        return blocked(str(exc))
    results: list[CaseResult] = []
    usage_records: list[dict | None] = []
    try:
        try:
            _seed_acceptance_base(conn)
            order = list(cases)
            for repetition in range(1, repeat + 1):
                for case in order:
                    result, records = _run_case(conn, case, repetition,
                                                model_factory)
                    results.append(result)
                    usage_records.extend(records)
        finally:
            conn.rollback()
            conn.close()
    except psycopg.Error as exc:
        remaining = max(len(cases) * repeat - len(results), 0)
        results.extend([CaseResult(
            id=f"environment-{index}", repeat=0, status="blocked",
            problems=[sanitize(f"数据库错误：{type(exc).__name__}")],
            duration_ms=0.0, model_call_count=0, tool_call_count=0,
            run_count=0, artifact_count=0, error_class=type(exc).__name__,
            usage_source=None) for index in range(remaining)])
    summary = _summarize(cases, results, mode=mode, repeat=repeat,
                         suite_path=suite_path, command=command,
                         usage_records=usage_records)
    if output_dir is not None:
        write_report(summary, Path(output_dir))
    return summary


def _summarize(cases, results: list[CaseResult], *, mode: str, repeat: int,
               suite_path: Path | None, command: str,
               usage_records: list[dict | None]) -> EvaluationSummary:
    # 执行错误也是已执行的尝试：计入分母，不得让含错运行呈现 100%（复核 F6）。
    executed = [case for case in results
                if case.status in ("pass", "fail", "error")]
    passed = sum(1 for case in executed if case.status == "pass")
    failed = sum(1 for case in results if case.status == "fail")
    blocked = sum(1 for case in results if case.status == "blocked")
    skipped = sum(1 for case in results if case.status == "skipped")
    errored = sum(1 for case in results if case.status == "error")
    human_pending = sum(
        1 for case in executed if case.status == "pass" and any(
            source.requires_human_review
            for source in cases if source.id == case.id))
    pass_rate = (passed / len(executed)) if executed else None
    usage = aggregate_usage(usage_records or [])
    usage["source"] = "synthetic" if mode == "offline" else "model"
    note = OFFLINE_NOTE if mode == "offline" else LIVE_NOTE
    return EvaluationSummary(
        mode=mode, note=note,
        dataset_sha256=_dataset_sha256(cases, suite_path),
        source=_git_source(), frozen_time=_frozen_time_iso(),
        flags={"PARTIAL_ANSWERS_ENABLED": False}, repeat=repeat,
        command=sanitize(command),
        totals={"attempted": len(cases) * repeat, "executed": len(executed),
                "passed": passed, "failed": failed + errored,
                "blocked": blocked, "skipped": skipped,
                "human_review_pending": human_pending,
                "pass_rate": pass_rate},
        usage=usage, cases=results)


def evaluate_exit_code(summary: EvaluationSummary) -> int:
    totals = summary.totals
    if totals["blocked"] or totals["skipped"] or totals["executed"] == 0:
        return 2
    if totals["human_review_pending"]:
        return 2
    if totals["failed"]:
        return 1
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="tests.evaluation.runner",
        description="串行执行合成评测场景并产出分层报告")
    parser.add_argument("--mode", required=True,
                        help="offline（脚本模型）或 live（真实模型，需--allow-live）")
    parser.add_argument("--suite", required=True, help="场景 JSONL 路径")
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--output", required=True, help="报告目录（不覆盖已有）")
    parser.add_argument("--allow-live", action="store_true",
                        help="显式授权真实模型调用；默认拒绝")
    parser.add_argument("--order", choices=("normal", "reverse"),
                        default="normal")
    args = parser.parse_args(argv)
    if args.mode not in ("offline", "live"):
        print(json.dumps({"result": "refused",
                          "reason": f"未知模式：{args.mode}"}, ensure_ascii=False))
        return 2
    if args.mode == "live" and not args.allow_live:
        print(json.dumps({"result": "refused", "reason":
                          "live需要--allow-live显式授权；本轮默认不调用外部模型"},
                         ensure_ascii=False))
        return 2
    suite_path = Path(args.suite)
    try:
        cases = load_cases(suite_path)
    except (OSError, ValueError) as exc:
        print(json.dumps({"result": "refused",
                          "reason": sanitize(str(exc))}, ensure_ascii=False))
        return 2
    if args.order == "reverse":
        cases = list(reversed(cases))
    command = "python -m tests.evaluation.runner " + " ".join(argv or sys.argv[1:])
    summary = run_suite(cases, mode=args.mode, repeat=args.repeat,
                        output_dir=Path(args.output), suite_path=suite_path,
                        command=command)
    code = evaluate_exit_code(summary)
    print(json.dumps({"mode": summary.mode, "totals": summary.totals,
                      "usage": summary.usage, "exit_code": code},
                     ensure_ascii=False))
    return code


if __name__ == "__main__":
    sys.exit(main())
