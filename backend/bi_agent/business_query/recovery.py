"""确定性恢复：原因码进，动作出，不读错误文案。

计划 Task 4 的核心要求是把"接下来怎么办"从模型自由循环里拿出来：同一个缺口如果
靠模型读文案决定，就会反复重发同一条查询直到耗尽工具次数，甚至悄悄把用户问的
日期改短。这里用固定映射消除这两种行为。

约束：
- 决策只给动作与原因，**不含替代请求**（不替用户改窗口、改指标）；
- 重试共享整轮 deadline，任何重试都不重置预算；
- 只有"已识别的临时连接故障"才允许自动重试，未知错误一律不猜类型。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

RecoveryAction = Literal[
    "ask_user",              # 缺参数或实体歧义：问，不跑 SQL
    "correct_parameters",    # 参数非法：允许模型修正后重试一次
    "retry_transient",       # 已识别临时连接故障：预算够才重试
    "report_gap",            # 缺覆盖 / 截止未知 / 来源未开通：如实报告缺口
    "terminate",             # 无权限、超时、预算耗尽：终止
    "reuse_result",          # 同指纹同数据版本已有结果：复用引用
    "fail_closed",           # Artifact 没存住：不发布成功结果
]

# 缺口类原因：重跑同一条查询不会补上数据，所以一次都不追加。
GAP_REASONS = frozenset({
    "coverage_incomplete", "data_as_of_unknown", "source_not_onboarded",
    "source_quality_failed", "comparison_coverage_incomplete",
    # 能力未开通同属缺口：重跑不会开通能力，只有完成逐源取证才行。
    "capability_unavailable",
})
TERMINAL_REASONS = frozenset({
    "forbidden", "deadline_exceeded", "query_timeout", "invalid_date_range",
})
# 临时故障重试需要的最小剩余预算：低于它重试只会把整轮拖死。
TRANSIENT_RETRY_MIN_SECONDS = 3.0
PARAMETER_RETRY_MIN_SECONDS = 2.0


@dataclass(frozen=True)
class RecoveryDecision:
    """一次确定性恢复决策。

    `suggested_window` 只是给用户看的建议；`decision` 里不携带任何改写过的请求。
    """

    action: RecoveryAction
    reason_code: str
    max_additional_attempts: int
    suggested_window: tuple[str, str] | None = None

    @property
    def consumes_tool_budget(self) -> bool:
        return self.max_additional_attempts > 0


def decide_recovery(error: object = None, coverage: object = None,
                    request_identity: object = None,
                    remaining_seconds: float = 0.0, *,
                    reason_code: str | None = None,
                    recovery_count: int | None = None,
                    reusable_artifact_ref: object | None = None) -> RecoveryDecision:
    """把已归因的原因码换成动作。原因码未知时不猜，按终止处理。"""
    code = _reason_of(error, reason_code)
    remaining = float(remaining_seconds)
    attempts = (recovery_count if recovery_count is not None
                else _identity_recovery_count(request_identity))
    suggested = _suggested_window(coverage)

    if reusable_artifact_ref is not None and code in (None, "succeeded"):
        return RecoveryDecision("reuse_result", "succeeded", 0, suggested)

    if code == "artifact_persistence_failed":
        # 结果没存住就不能对外算成功，也不靠重试掩盖持久化故障。
        return RecoveryDecision("fail_closed", code, 0, suggested)
    if code == "missing_parameters":
        return RecoveryDecision("ask_user", code, 0, suggested)
    if code in ("invalid_parameters", "invalid_metric", "invalid_group_by",
                "invalid_compare", "invalid_top_n", "invalid_shop"):
        allowed = 1 if (remaining > PARAMETER_RETRY_MIN_SECONDS and attempts < 1) else 0
        return RecoveryDecision("correct_parameters" if allowed else "terminate",
                                code, allowed, suggested)
    if code in GAP_REASONS:
        # 关键断言：缺覆盖绝不追加对同一请求的重复查询，也不动用户窗口。
        return RecoveryDecision("report_gap", code, 0, suggested)
    if code == "transient_source_failure":
        allowed = 1 if (remaining >= TRANSIENT_RETRY_MIN_SECONDS and attempts < 1) else 0
        return RecoveryDecision("retry_transient" if allowed else "terminate",
                                code, allowed, suggested)
    if code in TERMINAL_REASONS:
        return RecoveryDecision("terminate", code or "unavailable", 0, suggested)

    # 未知或不可归因的错误：保持 unavailable 语义终止，禁止按猜测重试。
    return RecoveryDecision("terminate", code or "unavailable", 0, suggested)


def _reason_of(error: object, explicit: str | None) -> str | None:
    if explicit:
        return str(explicit)
    if error is None:
        return None
    for attribute in ("termination_reason", "code", "reason_code"):
        value = getattr(error, attribute, None)
        if value is None and isinstance(error, dict):
            value = error.get(attribute)
        if value:
            return str(getattr(value, "value", value))
    return None


def _identity_recovery_count(identity: object) -> int:
    value = getattr(identity, "recovery_count", None)
    if value is None and isinstance(identity, dict):
        value = identity.get("recovery_count")
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _suggested_window(coverage: object) -> tuple[str, str] | None:
    value = getattr(coverage, "suggested_window", None)
    if value is None and isinstance(coverage, dict):
        value = coverage.get("suggested_window")
    if not value:
        return None
    parts = tuple(value)
    if len(parts) != 2:
        return None
    return (str(parts[0]), str(parts[1]))
