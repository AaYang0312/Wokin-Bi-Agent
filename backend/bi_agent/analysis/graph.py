"""隔离分析的固定状态图（计划 2026-09-14-isolated-analysis-agent.md Task 5）。

六个节点就是六道推进位，顺序死在 `ANALYSIS_CHAIN`：

    load_source → validate_source → compute_findings → summarize_findings
    → persist_analysis → finalize

三条不可让的位置：

1. **绝对 deadline 先于一切读取。** `load_source` 的第一件事就是检查
   `context.deadline`（`time.monotonic()` 上的绝对时刻，与主层 30 秒总预算
   共用，不另起也不重置）：过期当场 `deadline_exceeded`，不读来源、不碰
   模型（计划 Step 1 用例钉住）。叙事侧的"剩余不足 2 秒不发调用"由
   Task 4 的 `run_isolated_analysis` 沿同一份 deadline 判。
2. **所有拒绝都在模型调用之前，公开位置只有稳定码。** loader 的授权/类型/
   版本/大小/覆盖失败映射到 `needs_input`/`unavailable` 两类稳定通道；异常
   原文不进公开载荷，也不进运行记录——归因只落到码表内的 `termination_reason`。
3. **持久化 fail closed，且只写一次。** `analysis_result` 恰好保存一份；
   保存失败把状态改成 failed、清空待发布结果，绝不重试第二次写入，来源
   Artifact 一个字节都不动。

与 Task 4 的分工：`run_isolated_analysis` 是"纯分析运行"的唯一组合入口
（确定性 findings + 受守卫叙事 + limitations 合并），图不复刻它内部的
合并规则；`compute_findings` 节点的独立预检只为了归因——量级越界必须在
`compute_findings` 节点停下，而不是带着模型节点一起失败。
"""

from __future__ import annotations

import contextlib
import time
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Mapping
from uuid import UUID, uuid4

from bi_agent.analysis.loader import SOURCE_ARTIFACT_TYPES, load_analysis_dataset
from bi_agent.analysis.models import AnalysisRequest, AnalysisResult
from bi_agent.analysis.summarizer import ANALYSIS_VERSION, run_isolated_analysis
from bi_agent.commerce.models import DomainContext
from bi_agent.llm import ChatModel
from bi_agent.runtime.models import (
    ArtifactRef, DomainArtifact, DomainResult, DomainStatus, ErrorEnvelope,
    NewArtifact, NewQueryRun, RunCompletion, RunStatus, RunTransition)

# 领域名与节点白名单只有一个真源：`runtime.domain_registry`。
ANALYSIS_DOMAIN = "isolated_analysis"
# 运行记录里 `tool_call_id` 的形状与其他领域一致。
TOOL_CALL_ID_PREFIX = "toolu_analysis_"


class AnalysisNode(StrEnum):
    """固定链上的六个节点；枚举顺序就是推进顺序。"""

    LOAD_SOURCE = "load_source"
    VALIDATE_SOURCE = "validate_source"
    COMPUTE_FINDINGS = "compute_findings"
    SUMMARIZE_FINDINGS = "summarize_findings"
    PERSIST_ANALYSIS = "persist_analysis"
    FINALIZE = "finalize"


ANALYSIS_CHAIN: tuple[AnalysisNode, ...] = tuple(AnalysisNode)


class _Refusal:
    """一个终止原因的五份说法：终态、运行状态、错误码、公开消息、终止原因。

    与 exploration/graph 的拒绝表同一做派：它们必须同时改，否则运行表、事件
    与模型消息就长成三套话。`code` 只取 ErrorCode 词表；`termination` 只取
    RequestIdentity 的终止原因码表（分析专属原因不在码表里，用语义最近的
    既有码归因，不现场发明第五个说法）。
    """

    __slots__ = ("status", "run_status", "code", "message", "termination")

    def __init__(self, status: DomainStatus, run_status: RunStatus, code: str,
                 message: str, termination: str) -> None:
        self.status = status
        self.run_status = run_status
        self.code = code
        self.message = message
        self.termination = termination


_NEEDS_INPUT = _Refusal(DomainStatus.NEEDS_INPUT, RunStatus.NEEDS_INPUT,
                        "invalid_parameters", "查询参数无效，请调整后重试。",
                        "invalid_parameters")

# loader 的稳定原因码 → 拒绝通道（计划 Step 3：needs_input 或 unavailable）。
REFUSALS: Mapping[str, _Refusal] = MappingProxyType({
    # 读不到就是读不到：跨 owner 与不存在不可区分，也不告诉他人工件存在。
    # （仅指资格拒绝：Store 契约里 ValueError 只用于这四种资格失败。）
    "analysis_source_not_found": _NEEDS_INPUT,
    "analysis_source_type_unsupported": _NEEDS_INPUT,
    # 源库/读取故障（消毒后的 ArtifactPersistenceError、连接失败等）：基础设施
    # 问题，与资格拒绝不同类——retryable 的 unavailable，终止原因记
    # upstream_unavailable，异常原文不进任何公开位置，也不调模型、不写结果。
    "analysis_source_unavailable": _Refusal(
        DomainStatus.FAILED, RunStatus.FAILED, "unavailable",
        "查询暂不可用，请稍后重试。", "upstream_unavailable"),
    # 来源侧的问题换参数救不了：unavailable，归因到既有码表。
    "analysis_source_version_mismatch": _Refusal(
        DomainStatus.FAILED, RunStatus.FAILED, "unavailable",
        "查询暂不可用，请稍后重试。", "source_quality_failed"),
    "analysis_source_time_invalid": _Refusal(
        DomainStatus.FAILED, RunStatus.FAILED, "unavailable",
        "查询暂不可用，请稍后重试。", "source_quality_failed"),
    "analysis_source_invalid": _Refusal(
        DomainStatus.FAILED, RunStatus.FAILED, "unavailable",
        "查询结果异常。", "contract_violation"),
    "analysis_source_metric_missing": _Refusal(
        DomainStatus.FAILED, RunStatus.FAILED, "unavailable",
        "查询结果异常。", "contract_violation"),
    "analysis_row_ref_duplicate": _Refusal(
        DomainStatus.FAILED, RunStatus.FAILED, "unavailable",
        "查询结果异常。", "contract_violation"),
    "analysis_value_out_of_range": _Refusal(
        DomainStatus.FAILED, RunStatus.FAILED, "unavailable",
        "查询结果异常。", "contract_violation"),
    "analysis_source_coverage_incomplete": _Refusal(
        DomainStatus.FAILED, RunStatus.FAILED, "unavailable",
        "所查时间段的数据覆盖不足，可按建议窗口查询或等待回填完成。",
        "coverage_incomplete"),
    "analysis_source_too_large": _Refusal(
        DomainStatus.FAILED, RunStatus.FAILED, "unavailable",
        "查询结果异常。", "result_too_large"),
    # 来源数据带着未授权实体：范围问题，归因 forbidden，通道仍是 unavailable。
    "analysis_dimension_unauthorized": _Refusal(
        DomainStatus.FAILED, RunStatus.FAILED, "unavailable",
        "查询范围无权限。", "forbidden"),
    "deadline_exceeded": _Refusal(
        DomainStatus.FAILED, RunStatus.FAILED, "deadline_exceeded",
        "本次查询时间预算已耗尽，请缩小日期或店铺范围后重试。",
        "deadline_exceeded"),
    "persistence_failed": _Refusal(
        DomainStatus.FAILED, RunStatus.FAILED, "artifact_persistence_failed",
        "结果保存失败，请稍后重试。", "persistence_failed"),
})


@dataclass
class _Runtime:
    """一次分析的可变现场。"""

    request: AnalysisRequest
    context: DomainContext
    model: ChatModel | None
    run_id: UUID | None = None
    revision: int = 0
    # 最后一次成功落库的 revision 镜像：半途失败时的收尾要用它，不用预加值。
    persisted: int = 0
    finalized: bool = False
    node: AnalysisNode = AnalysisNode.LOAD_SOURCE
    stored: Any = None
    dataset: Any = None
    result: AnalysisResult | None = None
    persisted_payload: dict[str, object] | None = None
    published: list[ArtifactRef] = field(default_factory=list)
    refusal: str | None = None
    visited: list[AnalysisNode] = field(default_factory=list)


def analyze_artifact(request: AnalysisRequest, *, context: DomainContext,
                     model: ChatModel | None = None) -> DomainResult:
    """按固定链执行一次隔离分析，并把每一格推进写进运行记录（CAS）。"""
    if not isinstance(request, AnalysisRequest):
        raise TypeError("analysis_request_contract_required")
    runtime = _Runtime(request=request, context=context, model=model)
    _create_run(runtime)
    steps: tuple[tuple[AnalysisNode, Any], ...] = (
        (AnalysisNode.LOAD_SOURCE, _load_source),
        (AnalysisNode.VALIDATE_SOURCE, _validate_source),
        (AnalysisNode.COMPUTE_FINDINGS, _compute_findings),
        (AnalysisNode.SUMMARIZE_FINDINGS, _summarize_findings),
        (AnalysisNode.PERSIST_ANALYSIS, _persist_analysis),
    )
    # 半死的运行不能留在 running：与其他领域图同一做派——先 best-effort 落一个
    # 终态，再把原异常上抛交给外层脱敏。
    try:
        for node, step in steps:
            if runtime.refusal is not None:
                break
            _enter(runtime, node)
            step(runtime)
        _finalize(runtime)
    except Exception:  # noqa: BLE001 - 收尾之后再上抛
        _finish_run_as_failed(runtime)
        raise
    return _domain_result(runtime)


# ---------------------------------------------------------------------------
# 驱动与记录
# ---------------------------------------------------------------------------

def _create_run(runtime: _Runtime) -> None:
    context = runtime.context
    runtime.run_id = context.store.create_run(NewQueryRun(
        chat_id=context.chat_id, user_message_id=context.user_message_id,
        subject_id=context.subject_id,
        tool_call_id=f"{TOOL_CALL_ID_PREFIX}{uuid4().hex[:16]}",
        domain=ANALYSIS_DOMAIN, attempt_no=context.attempt_no,
        normalized_request=_normalized(runtime),
        state={**_state(runtime, node=AnalysisNode.LOAD_SOURCE,
                        status=RunStatus.RUNNING), "revision": 0}))


def _enter(runtime: _Runtime, node: AnalysisNode) -> None:
    """推进一格：只写节点与状态，不写任何业务原文。"""
    runtime.node = node
    runtime.visited.append(node)
    runtime.revision += 1
    runtime.context.store.transition(runtime.run_id, RunTransition(
        expected_revision=runtime.revision - 1, node=node.value,
        status=RunStatus.RUNNING,
        state=_state(runtime, node=node, status=RunStatus.RUNNING)))
    runtime.persisted = runtime.revision


def _state(runtime: _Runtime, *, node: AnalysisNode, status: RunStatus,
           error: ErrorEnvelope | None = None) -> dict[str, object]:
    state: dict[str, object] = {
        "node": node.value, "status": status.value, "revision": runtime.revision,
        "normalized_request": _normalized(runtime)}
    if runtime.run_id is not None:
        state["run_id"] = str(runtime.run_id)
    if runtime.published:
        state["artifact_refs"] = [{"id": str(ref.id), "type": ref.type}
                                  for ref in runtime.published]
    if error is not None:
        state["error"] = error.model_dump(mode="json")
    return state


def _normalized(runtime: _Runtime) -> dict[str, object]:
    """规范化请求只存服务端授权引用：来源行、问题文本与授权主键都不进这一列。"""
    return {"shop_refs": sorted(runtime.context.shop_refs.values())}


def _refuse(runtime: _Runtime, reason: str) -> None:
    """记一次终止原因：原因只能取自映射表，图不在现场发明新说法。"""
    if reason not in REFUSALS:
        raise ValueError("analysis_termination_reason_unknown")
    runtime.refusal = reason


def _remaining(runtime: _Runtime) -> float:
    return runtime.context.deadline - time.monotonic()


def _finish_run_as_failed(runtime: _Runtime) -> None:
    """尽力把这轮收尾成 FAILED：写不进去就吞掉收尾自己的错误，不替换在飞异常。"""
    if runtime.run_id is None or runtime.finalized:
        return
    runtime.node = AnalysisNode.FINALIZE
    error = ErrorEnvelope(code="unavailable", stage=AnalysisNode.FINALIZE.value,
                          retryable=True, recovery="retry_later",
                          public_message="查询暂不可用，请稍后重试。",
                          problems=["unavailable"])
    with contextlib.suppress(Exception):
        runtime.context.store.finish(runtime.run_id, RunCompletion(
            expected_revision=runtime.persisted,
            node=AnalysisNode.FINALIZE.value, status=RunStatus.FAILED,
            state={**_state(runtime, node=AnalysisNode.FINALIZE,
                            status=RunStatus.FAILED, error=error),
                   "target_status": DomainStatus.FAILED.value},
            payload={"tool_status": "unavailable",
                     "target_status": DomainStatus.FAILED.value},
            error_code="unavailable", termination_reason="upstream_unavailable"))
    runtime.finalized = True


# ---------------------------------------------------------------------------
# 1) load_source：deadline 先于一切读取，授权投影只走 Task 2 loader
# ---------------------------------------------------------------------------

def _load_source(runtime: _Runtime) -> None:
    context = runtime.context
    if _remaining(runtime) <= 0:
        # 计划 Step 1：过期 deadline 在任何读取与模型调用之前拒绝。
        _refuse(runtime, "deadline_exceeded")
        return
    try:
        # 来源时点/覆盖列用于血缘回写：与 loader 同一入口、同一 owner 判据读一次。
        # loader 自己的授权读是分析准入的唯一权威，这里的快照只带元数据。
        runtime.stored = context.store.load_artifact_for_analysis(
            UUID(runtime.request.artifact_ref), subject_id=context.subject_id)
    except ValueError:
        # 稳定资格拒绝（Store 契约：资格失败只用这个 ValueError）。
        runtime.stored = None
        _refuse(runtime, "analysis_source_not_found")
        return
    except Exception:  # noqa: BLE001 - 源读取故障：unavailable 通道，不留原文
        runtime.stored = None
        _refuse(runtime, "analysis_source_unavailable")
        return
    try:
        runtime.dataset = load_analysis_dataset(runtime.request.artifact_ref,
                                                context=context)
    except ValueError as error:
        _refuse(runtime, _loader_reason(error))
        return
    except Exception:  # noqa: BLE001 - loader 内的源读取故障走同一 unavailable 通道
        _refuse(runtime, "analysis_source_unavailable")
        return


def _loader_reason(error: ValueError) -> str:
    """loader 的稳定原因码 → 拒绝通道；未知原因按最保守的 not_found 处理。"""
    text = str(error)
    return text if text in REFUSALS else "analysis_source_not_found"


# ---------------------------------------------------------------------------
# 2) validate_source：值对象与请求/快照逐项对上才进纯分析
# ---------------------------------------------------------------------------

def _validate_source(runtime: _Runtime) -> None:
    dataset = runtime.dataset
    stored = runtime.stored
    if dataset.source_artifact_ref != runtime.request.artifact_ref \
            or str(stored.ref.id) != dataset.source_artifact_ref:
        # 值对象与请求/快照错位：这不是可发布的结果，也不是可重试的失败。
        _refuse(runtime, "analysis_source_invalid")
        return
    if stored.subject_id != runtime.context.subject_id:
        _refuse(runtime, "analysis_source_not_found")
        return
    if stored.artifact_type not in SOURCE_ARTIFACT_TYPES:
        _refuse(runtime, "analysis_source_type_unsupported")
        return


# ---------------------------------------------------------------------------
# 3) compute_findings：量级越界在模型之前拒绝，归因落在计算节点
# ---------------------------------------------------------------------------

def _compute_findings(runtime: _Runtime) -> None:
    from .calculations import compute_findings

    try:
        # 预检与 summarize 节点的组合入口各自调用同一份纯函数：确定性计算
        # 两次结果逐字相同，预检只为了让失败停在正确的节点上。
        compute_findings(runtime.dataset,
                         tuple(runtime.request.analysis_kinds))
    except ValueError:
        _refuse(runtime, "analysis_value_out_of_range")
        return


# ---------------------------------------------------------------------------
# 4) summarize_findings：Task 4 的组合入口，deadline 沿用主请求
# ---------------------------------------------------------------------------

def _summarize_findings(runtime: _Runtime) -> None:
    try:
        runtime.result = run_isolated_analysis(
            runtime.dataset, kinds=tuple(runtime.request.analysis_kinds),
            model=runtime.model, deadline=runtime.context.deadline)
    except ValueError:
        # 组合入口自己消化模型失败；能逃出去的只剩确定性计算的不测失败。
        _refuse(runtime, "analysis_value_out_of_range")
        return


# ---------------------------------------------------------------------------
# 5) persist_analysis：恰好一份 artifact，失败即整体失败，绝不重试
# ---------------------------------------------------------------------------

def _persist_analysis(runtime: _Runtime) -> None:
    payload = runtime.result.model_dump(mode="json")
    try:
        ref = runtime.context.store.save_artifact(
            runtime.run_id, NewArtifact(
                artifact_type="analysis_result", payload=payload,
                data_as_of=runtime.stored.data_as_of,
                coverage=runtime.stored.coverage))
    except Exception:  # noqa: BLE001 - 必需结果存不下不是降级
        runtime.published = []
        runtime.result = None
        _refuse(runtime, "persistence_failed")
        return
    runtime.published = [ref]
    runtime.persisted_payload = payload


# ---------------------------------------------------------------------------
# 6) finalize 与公开结果
# ---------------------------------------------------------------------------

def _finalize(runtime: _Runtime) -> None:
    context = runtime.context
    status, run_status, code, _message, termination = _terminal(runtime)
    runtime.node = AnalysisNode.FINALIZE
    runtime.visited.append(AnalysisNode.FINALIZE)
    runtime.revision += 1
    error = _error_envelope(runtime)
    context.store.finish(runtime.run_id, RunCompletion(
        expected_revision=runtime.persisted, node=AnalysisNode.FINALIZE.value,
        status=run_status,
        state=_state(runtime, node=AnalysisNode.FINALIZE, status=run_status,
                     error=error),
        payload=_event_payload(runtime, status=run_status),
        error_code=code if error is not None else None,
        termination_reason=termination))
    # 只有写成功才算收尾完成：失败时留给 `_finish_run_as_failed` 补一个终态。
    runtime.persisted = runtime.revision
    runtime.finalized = True


def _terminal(runtime: _Runtime) -> tuple[DomainStatus, RunStatus, str, str, str]:
    if runtime.refusal is None:
        return (DomainStatus.SUCCESS, RunStatus.SUCCEEDED, "", "", "succeeded")
    refusal = REFUSALS[runtime.refusal]
    return (refusal.status, refusal.run_status, refusal.code, refusal.message,
            refusal.termination)


def _error_envelope(runtime: _Runtime) -> ErrorEnvelope | None:
    if runtime.refusal is None:
        return None
    refusal = REFUSALS[runtime.refusal]
    return ErrorEnvelope(
        code=refusal.code, stage=runtime.node.value,
        retryable=refusal.code in {"deadline_exceeded",
                                   "artifact_persistence_failed",
                                   "unavailable"},
        recovery=_recovery(refusal.status), public_message=refusal.message,
        problems=[refusal.code])


def _recovery(status: DomainStatus) -> str:
    return {DomainStatus.NEEDS_INPUT: "correct_parameters",
            DomainStatus.FAILED: "retry_later"}[status]


def _event_payload(runtime: _Runtime, *, status: RunStatus) -> dict[str, object]:
    payload: dict[str, object] = {"tool_status": _tool_status(status),
                                  "target_status": _terminal(runtime)[0].value}
    if runtime.published:
        payload["artifact_refs"] = [{"id": str(ref.id), "type": ref.type}
                                    for ref in runtime.published]
        payload["result_count"] = (len(runtime.result.findings)
                                   if runtime.result is not None else 0)
    return payload


def _tool_status(status: RunStatus) -> str:
    return {RunStatus.SUCCEEDED: "ok", RunStatus.NEEDS_INPUT: "invalid_parameters",
            RunStatus.MISSING_DATA: "missing_data", RunStatus.FAILED: "unavailable",
            RunStatus.PARTIAL: "partial", RunStatus.RUNNING: "running"}[status]


def _domain_result(runtime: _Runtime) -> DomainResult:
    status, _run_status, _code, _message, termination = _terminal(runtime)
    artifacts: list[DomainArtifact] = []
    if runtime.result is not None and runtime.published:
        artifacts = [DomainArtifact(ref=runtime.published[0],
                                    public_payload=runtime.persisted_payload)]
    stored = runtime.stored
    return DomainResult(
        run_id=runtime.run_id, status=status,
        model_payload=_model_payload(runtime, status=status,
                                     termination=termination),
        artifacts=artifacts,
        data_as_of=getattr(stored, "data_as_of", None),
        coverage=getattr(stored, "coverage", None),
        error=_error_envelope(runtime))


def _model_payload(runtime: _Runtime, *, status: DomainStatus,
                   termination: str) -> dict[str, object]:
    """拒答发最小形状：没有可发的结果就别装出一副有数的样子。

    成功时 findings/narrative 走 Artifact 公开载荷（runtime 契约：分析载荷
    给模型与公开发的是同一份形状），这里只带通道状态。
    """
    if status is DomainStatus.SUCCESS:
        return {"status": "ok", "limitations": []}
    return {"status": _tool_status(_terminal(runtime)[1]),
            "termination_reason": termination,
            "limitations": []}


__all__ = ["ANALYSIS_CHAIN", "ANALYSIS_DOMAIN", "ANALYSIS_VERSION",
           "AnalysisNode", "analyze_artifact"]
