"""隔离分析 Tool 的模型可见契约与服务端适配器（计划 Task 5 Step 5）。

两件事分别钉住：

1. `analysis_request_schema()` 直接取 `AnalysisRequest.model_json_schema()`：
   参数只有 `artifact_ref` 与 `analysis_kinds`，`extra="forbid"` 生成的
   `additionalProperties=false` 拒掉一切附加键——`question`、`sql`、店铺主键、
   数据行、tool list 都没有入口。契约只有一份，schema 与校验不会漂移。
2. `execute_analysis_tool(call, context, *, model)` 在图之前只做参数解析：
   `arguments_error`、非对象参数、服务端专有键与请求校验失败都按稳定码拒绝，
   并按既有运行契约留下一条可审计的运行记录（与 exploration/tool 同一做派），
   而不是悄悄返回一个空结果。授权不来自模型给的任何字段：图内从当前
   `DomainContext` 重建（subject/shops/deadline 全部服务端注入）。
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from pydantic import ValidationError

from bi_agent.analysis.graph import ANALYSIS_DOMAIN, analyze_artifact
from bi_agent.analysis.models import AnalysisRequest
from bi_agent.commerce.models import DomainContext
from bi_agent.llm import ChatModel
from bi_agent.runtime.models import (
    DomainResult, DomainStatus, ErrorEnvelope, NewQueryRun, RunCompletion,
    RunStatus, validate_model_payload)

TOOL_NAME = "analyze_artifact"

# 模型永远不许提供的键：提问文本、查询通道、授权主键。schema 的
# additionalProperties=false 是第一道，这里是替身/旧客户端的第二道。
SERVER_OWNED_ARGUMENTS = frozenset({
    "question", "sql", "sql_text", "shop_id", "shop_ids", "allowed_shop_ids",
    "data", "rows", "tools", "domain"})


def analysis_request_schema() -> dict[str, object]:
    """模型的 JSON Schema：就是请求契约本身，不另写第二份词表。"""
    return AnalysisRequest.model_json_schema()


def execute_analysis_tool(call: Any, context: DomainContext, *,
                          model: ChatModel | None) -> DomainResult:
    """跑一次分析 Tool 调用：参数由服务端重解，授权只来自当前上下文。"""
    arguments = _arguments_of(call)
    if isinstance(arguments, str):
        return _refuse(context, detail=arguments)
    try:
        request = AnalysisRequest.model_validate(arguments)
    except (ValidationError, ValueError):
        return _refuse(context, detail="tool_arguments_invalid")
    return analyze_artifact(request, context=context, model=model)


def _arguments_of(call: Any) -> dict[str, object] | str:
    """取模型给的参数：`arguments_error` 与非对象 arguments 都变成稳定原因码。"""
    arguments = getattr(call, "arguments", None)
    error = getattr(call, "arguments_error", None)
    if isinstance(error, str) and error:
        return "tool_arguments_unparsable"
    if arguments is None or not isinstance(arguments, dict):
        return "tool_arguments_required"
    if any(key in SERVER_OWNED_ARGUMENTS for key in arguments):
        # 不回显键名也不回显值：值可能就是那段 SQL 或那个真店号。
        return "tool_arguments_server_owned_keys"
    return dict(arguments)


def _refuse(context: DomainContext, *, detail: str) -> DomainResult:
    """参数层的拒绝也要留审计记录：不建"看起来成功"的空结果。

    `detail` 只是稳定码，不进任何公开位置：`termination_reason` 已经足够归因。
    """
    del detail
    store = context.store
    normalized = {"shop_refs": sorted(context.shop_refs.values())}
    run_id = store.create_run(NewQueryRun(
        chat_id=context.chat_id, user_message_id=context.user_message_id,
        subject_id=context.subject_id,
        tool_call_id=f"toolu_analysis_{uuid4().hex[:16]}",
        domain=ANALYSIS_DOMAIN, attempt_no=context.attempt_no,
        normalized_request=normalized,
        state={"node": "load_source", "status": RunStatus.RUNNING.value,
               "revision": 0, "normalized_request": normalized}))
    error = ErrorEnvelope(code="invalid_parameters", stage="load_source",
                          retryable=False, recovery="correct_parameters",
                          public_message="查询参数无效，请调整后重试。",
                          problems=["invalid_parameters"])
    store.finish(run_id, RunCompletion(
        expected_revision=0, node="finalize", status=RunStatus.NEEDS_INPUT,
        state={"node": "finalize", "status": RunStatus.NEEDS_INPUT.value,
               "revision": 1, "run_id": str(run_id),
               "normalized_request": normalized,
               "error": error.model_dump(mode="json")},
        payload={"tool_status": "invalid_parameters",
                 "target_status": DomainStatus.NEEDS_INPUT.value},
        error_code="invalid_parameters", termination_reason="invalid_parameters"))
    return DomainResult(
        run_id=run_id, status=DomainStatus.NEEDS_INPUT,
        model_payload=validate_model_payload(
            {"status": "invalid_parameters",
             "termination_reason": "invalid_parameters", "limitations": []}),
        artifacts=[], error=error)


__all__ = ["SERVER_OWNED_ARGUMENTS", "TOOL_NAME", "analysis_request_schema",
           "execute_analysis_tool"]
