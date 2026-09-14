"""探索 Tool 的模型可见契约与服务端适配器（计划 Task 5 Step 6）。

两件事分别钉住：

1. `exploration_request_schema(selection)` 只把**当前这一轮服务端 selection 里的**
   entity / metric / field ref 作为 JSON Schema enum，并且 `additionalProperties=false`。
   模型因此既拿不到目录外的 ref，也拿不到 `question` / `sql` / `shop_id` /
   `allowed_shop_ids` 这几个键——提问文本由服务端注入，SQL 与授权集合由服务端决定
   （计划 Global Constraints：模型只提供语义 ref 与业务值）。
2. `execute_exploration_tool(call, context, *, question, versions)` 按顺序做四件事：
   用**当前用户原文**覆盖提问 → 用同一份原文重新检索 → 检查模型给的 ref 是服务端
   selection 的子集且目录版本一致 → 跑固定图。任何一步不过都在图之前停，并按
   既有运行契约建一条可审计的运行记录，而不是悄悄返回一个空结果。

为什么"模型已经看过 enum 了还要再检一次子集"：schema 是提示，不是凭据。替身模型、
旧客户端与被改写的 arguments 都能带着别的 ref 过来；只有服务端重新解一遍才算授权。
"""

from __future__ import annotations

from typing import Any, Mapping
from uuid import UUID

from pydantic import ValidationError

from bi_agent.commerce.models import DomainContext
from bi_agent.exploration.graph import (
    EXPLORATION_DOMAIN, ExplorationExecution, run_exploration_graph)
from bi_agent.exploration.models import MAX_ROWS, ExplorationRequest
from bi_agent.runtime.domain_registry import domains
from bi_agent.runtime.models import (
    DomainResult, DomainStatus, ErrorEnvelope, NewQueryRun, RunCompletion, RunStatus,
    validate_model_payload)
from bi_agent.runtime.versions import VersionSet
from bi_agent.semantic_catalog.models import SemanticSelection
from bi_agent.semantic_catalog.retrieval import retrieve_schema_candidates

TOOL_NAME = "explore_business_data"
# 运行记录里 `tool_call_id` 的形状与其他领域一致：模型没给 id 时服务端补一个。
TOOL_CALL_ID_PREFIX = "toolu_exploration_"

# 模型的提问通道只有一个：当前这轮用户原文。args 里出现这些键一律拒。
SERVER_OWNED_ARGUMENTS = frozenset({
    "question", "sql", "sql_text", "shop_id", "shop_ids", "allowed_shop_ids",
    "statement_fingerprint", "template_version", "catalog_version", "domain"})


# 门禁需要一句"哪一版 schema 登记了这个领域"的血缘说明：020 迁移。
EXPLORATION_SCHEMA_VERSION = "020"


def exploration_gate(question: str, *, allowed_shop_ids: frozenset[str],
                     shop_refs: Mapping[str, str]) -> tuple[dict[str, object] | None,
                                                            VersionSet | None]:
    """本轮能不能给模型 `explore_business_data`：不过就 `(None, None)`。

    计划 Task 5 Step 7 的三条（`fixed_tool_for` 为空、无未注册概念、不需澄清）在这里判，
    再加一条服务端作用域：授权集合非空且每个店铺都有不透明引用 —— 没有引用就没有能
    对外发的行（投影那一格会整条拒掉），把它公告出去只会换来一次失败的探索。

    来源能力 / 质量 / 覆盖那三条真库事实**不在这里**判：Tool 列表不是执行门禁，把库
    查询搬到列 Tool 上会让"没被调用的 Tool"也花钱，而拒答也不再留下可审计的运行记录。
    它们由图的 `assess_readiness` 节点判（计划 Task 5 Step 5）。

    fail-closed 的方向是**少给能力**：解不出、装不进请求契约、探测异常 —— 全部退回
    "不加这个 Tool"，固定 Tool 继续是唯一入口；固定 Tool 本轮就会返回 unavailable 时
    也不众开第二条路（不能拿它的拒答当"换个工具再试一次"）。
    """
    from bi_agent.exploration.eligibility import fixed_tool_for
    from bi_agent.runtime.domain_registry import domains

    if not allowed_shop_ids or any(shop not in shop_refs for shop in allowed_shop_ids):
        return None, None
    versions = exploration_versions()
    try:
        selection = retrieve_schema_candidates(question,
                                              allowed_domains=frozenset(domains()),
                                              current_versions=versions)
    except ValueError:
        return None, None
    if (selection.requires_clarification or selection.missing_concepts
            or not selection.view_refs or not selection.metric_refs):
        return None, None
    if (len(selection.metric_refs) > 8 or len(selection.field_refs) > 4
            or len(selection.entity_refs) > 5):
        # 装不进请求契约的探测既不能证明"固定 Tool 能表达"，也不该被公告成可探索。
        return None, None
    try:
        probe = ExplorationRequest(
            question=question, entity_refs=list(selection.entity_refs),
            requested_metric_refs=list(selection.metric_refs),
            group_by_field_refs=list(selection.field_refs))
    except (ValidationError, ValueError):
        return None, None
    try:
        expressed = fixed_tool_for(selection, probe)
    except (TypeError, ValueError):
        return None, None
    if expressed is not None:
        return None, None
    return ({"type": "function", "function": {
        "name": TOOL_NAME,
        "description": "只有前面那些固定工具表达不了的聚合问题才用：从下面给出的语义引用里"
                       "选指标与分组，日期 end 排他；不给 SQL、不给店铺主键，授权范围与"
                       "行数上限由服务端决定。缺能力、缺覆盖或口径未认证时它返回原因而不是数字",
        "parameters": exploration_request_schema(selection)}}, versions)


def exploration_versions() -> VersionSet:
    """本轮冻结的版本集合：语义目录版本一变，旧的探索结果不可复用（总设计 §5.4）。

    `data_catalog_version` 取 0 是有意的：门禁这一步不查库，而探索运行目前还没有
    `bi.query_provenance` 行可写。等探索补上血缘时，真实数据目录版本应在图里（手里有
    conn）读取并落库，而不是在列 Tool 的阶段补一个数。
    """
    from bi_agent.runtime.artifacts import GRAPH_VERSION
    from bi_agent.semantic_catalog.registry import CATALOG
    from bi_agent.sources import (METRIC_VERSION, POLICY_VERSION,
                                  SOURCE_REGISTRY_VERSION)

    return VersionSet(schema_version=EXPLORATION_SCHEMA_VERSION,
                      semantic_catalog_version=CATALOG.version,
                      data_catalog_version=0, metric_version=METRIC_VERSION,
                      policy_version=POLICY_VERSION,
                      source_registry_version=SOURCE_REGISTRY_VERSION,
                      graph_version=GRAPH_VERSION)


def exploration_request_schema(selection: SemanticSelection) -> dict[str, object]:
    """本轮 selection 能给出的 ref 就是模型能给的 ref：词表随检索结果收窄。"""
    if not isinstance(selection, SemanticSelection):
        raise TypeError("exploration_selection_contract_required")
    return {
        "type": "object",
        "properties": {
            "entity_refs": _ref_array(selection.entity_refs, max_items=5),
            "requested_metric_refs": _ref_array(selection.metric_refs, max_items=8),
            "group_by_field_refs": _ref_array(selection.field_refs, max_items=4),
            "start": {"anyOf": [{"type": "string", "format": "date"},
                                {"type": "null"}]},
            "end": {"anyOf": [{"type": "string", "format": "date"},
                              {"type": "null"}]},
            "limit": {"type": "integer", "minimum": 1, "maximum": MAX_ROWS},
        },
        "required": ["requested_metric_refs"],
        "additionalProperties": False,
    }


def _ref_array(refs: tuple[str, ...], *, max_items: int) -> dict[str, object]:
    return {"type": "array", "maxItems": max_items,
            "items": {"type": "string", "enum": sorted(refs)}}


def execute_exploration_tool(call: Any, context: DomainContext, *, question: str,
                             versions: VersionSet) -> ExplorationExecution:
    """跑一次探索 Tool 调用：提问由服务端注入，ref 由服务端重新解。"""
    arguments = _arguments_of(call)
    if isinstance(arguments, str):          # 已经是拒答原因
        return _refuse(context, reason="invalid_parameters", stage="select_schema",
                       detail=arguments, question=question, versions=versions)
    request = _request_of(arguments, question=question)
    if isinstance(request, str):
        return _refuse(context, reason="invalid_parameters", stage="select_schema",
                       detail=request, question=question, versions=versions)
    selection = _authoritative_selection(question, versions)
    if isinstance(selection, str):
        return _refuse(context, reason="schema_ambiguous", stage="select_schema",
                       detail=selection, question=question, versions=versions)
    violation = _subset_violation(request, selection)
    if violation is not None:
        return _refuse(context, reason="schema_ambiguous", stage="select_schema",
                       detail=violation, question=question, versions=versions)
    return run_exploration_graph(question=question, request=request, context=context,
                                 versions=versions)


def _arguments_of(call: Any) -> dict[str, object] | str:
    """取模型给的参数：`arguments_error` 与非对象 arguments 都在这里变成稳定原因码。"""
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


def _request_of(arguments: Mapping[str, object], *, question: str) -> ExplorationRequest | str:
    """构造请求：提问原样换成服务端注入的那一份，模型给的提问文本一律不用。"""
    clean = {key: value for key, value in arguments.items()
             if key not in SERVER_OWNED_ARGUMENTS}
    try:
        return ExplorationRequest.model_validate(dict(clean, question=question))
    except ValidationError:
        return "tool_arguments_invalid"
    except ValueError:
        return "tool_arguments_invalid"


def _authoritative_selection(question: str, versions: VersionSet) -> SemanticSelection | str:
    """按当前问题重新检索：这是“模型给的 ref 属不属于本轮 selection”的那份判据。

    检索范国内当前已注册的全部领域：目录还没有把 `controlled_sql_exploration` 挂到
    任何视图上（那是目录变更，不属本 Task 的文件清单），所以按它检索只会拿到空候选。
    拿“全部已注册领域”不是放开：能跑什么仍然由图里的作用域/编译/策略/预算四道门定。
    """
    try:
        return retrieve_schema_candidates(question,
                                          allowed_domains=frozenset(domains()),
                                          current_versions=versions)
    except ValueError:
        return "semantic_retrieval_unavailable"


def _subset_violation(request: ExplorationRequest,
                      selection: SemanticSelection) -> str | None:
    """三类 ref 逐项必须是服务端 selection 的子集：不认就一个都不跑。"""
    for refs, allowed, name in (
            (request.entity_refs, selection.entity_refs, "entity"),
            (request.requested_metric_refs, selection.metric_refs, "metric"),
            (request.group_by_field_refs, selection.field_refs, "field")):
        if not set(refs) <= set(allowed):
            return f"exploration_{name}_ref_out_of_selection"
    return None


# ---------------------------------------------------------------------------
# 图之前的拒答：仍然要留下一条可审计的运行记录
# ---------------------------------------------------------------------------

def _refuse(context: DomainContext, *, reason: str, stage: str, detail: str,
            question: str, versions: VersionSet) -> ExplorationExecution:
    """不建"看起来成功"的空结果：状态、原因码与终止原因一起落运行记录。

    `detail` 只是稳定码（不含模型原文、SQL 或真店号），因此它可以安全地待在
    `normalized_request` 之外的任何位置——这里干脆不落它，公开位置连原因文字都不需要：
    `termination_reason` 已经足够归因，多的那句只会增加泄露面。
    """
    del detail, question, versions           # 只进稳定码，不进任何公开载荷
    store = context.store
    shop_refs = sorted(context.shop_refs.values())
    normalized: dict[str, object] = {"shop_refs": shop_refs}
    run_id: UUID = store.create_run(NewQueryRun(
        chat_id=context.chat_id, user_message_id=context.user_message_id,
        subject_id=context.subject_id,
        tool_call_id=f"{TOOL_CALL_ID_PREFIX}{run_suffix()}",
        domain=EXPLORATION_DOMAIN, attempt_no=context.attempt_no,
        normalized_request=normalized,
        state={"node": stage, "status": RunStatus.RUNNING.value, "revision": 0,
               "normalized_request": normalized}))
    status = DomainStatus.NEEDS_INPUT
    run_status = RunStatus.NEEDS_INPUT
    error = ErrorEnvelope(code="invalid_parameters", stage=stage, retryable=False,
                          recovery="correct_parameters",
                          public_message="查询参数无效，请调整后重试。",
                          problems=["invalid_parameters"])
    store.finish(run_id, RunCompletion(
        expected_revision=0, node="finalize", status=run_status,
        state={"node": "finalize", "status": run_status.value, "revision": 1,
               "run_id": str(run_id), "normalized_request": normalized,
               "error": error.model_dump(mode="json")},
        payload={"tool_status": "invalid_parameters", "target_status": status.value},
        error_code="invalid_parameters", termination_reason=reason))
    return ExplorationExecution(
        domain_result=DomainResult(
            run_id=run_id, status=status,
            model_payload=validate_model_payload(
                {"status": "invalid_parameters", "termination_reason": reason,
                 "limitations": []}),
            artifacts=[], error=error),
        plan=None)


def run_suffix() -> str:
    from uuid import uuid4

    return uuid4().hex[:16]


__all__ = ["TOOL_NAME", "execute_exploration_tool", "exploration_gate",
           "exploration_request_schema", "exploration_versions"]
