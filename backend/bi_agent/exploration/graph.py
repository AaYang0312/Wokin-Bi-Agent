"""受控 SQL 探索的固定状态图（计划 Task 5 Step 5）。

九个节点就是九道门禁，顺序是死的：

    select_schema → authorize_scope → assess_readiness → compile_query → validate_ast
    → estimate_cost → execute_readonly → persist_artifact → finalize

三条不可让的位置：

1. **固定 Tool 优先在 `authorize_scope`，在任何 SQL 之前。** `eligibility` 只看
   「固定 Tool 能不能表达」，不看能力/覆盖：固定 Tool 会正确地拒答时也不许换成 SQL
   绕过（总设计 §6.1、§6.4）。
2. **能力 / 质量 / 覆盖在 `compile_query` 之前。** 编译出来的语句再干净也修不了
   "这家店的这个来源从没取证"；把 readiness 放到执行之后，等于先花钱再发现答案不能发。
3. **`record_diagnostic` 在 `save_artifact` 之前。** 诊断是这份结果唯一留存的 SQL 证据；
   证据留不下就不许发公开结果（计划 Task 5 Step 5：诊断写失败映射 `persistence_failed`
   并清空待发布结果）。公开 Artifact 写不进同样是 failed，而不是"少一张卡片"。

预算整轮只用 `context.deadline`（`time.monotonic()` 上的绝对时刻）：图不重置它，也不
给自己另开一份。两道 Task 4 的门各要 5 秒语句上限，所以**进库之前**要求剩余预算同时
容得下这两道门加余量：宁可当场判 `deadline_exceeded`，也不要跑到一半被数据库掐断，
留下一条谁也解释不了的运行记录。

授权只有店铺一条通道：`allowed_shop_ids` 只收服务端授权集合里的店铺主键；库存池 /
仓库的 id 绝不 substitute 进来，只能按池授权的视图当场 fail closed（`forbidden`），
而不是凑一个条件跑出一份空结果当作"这家店没数据"。
"""

from __future__ import annotations

import contextlib
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Mapping
from uuid import UUID, uuid4

from bi_agent.commerce.models import DomainContext
from bi_agent.exploration.compiler import compile_query
from bi_agent.exploration.eligibility import fixed_tool_for
from bi_agent.exploration.models import (
    DB_IO_RESERVE_SECONDS,
    STATEMENT_TIMEOUT_MS,
    ExplorationColumn,
    ExplorationRequest,
    ExplorationResult,
    ValidatedQueryPlan,
)
from bi_agent.exploration.policy import validate_exploration_plan
from bi_agent.exploration.projection import project_result
from bi_agent.exploration.repository import estimate_plan, execute_plan
from bi_agent.data_quality import assess_query_coverage
from bi_agent.metrics import PRODUCT_METRICS, QueryRequest
from bi_agent.runtime.domain_registry import domains
from bi_agent.sources import (METRIC_CAPABILITIES, METRIC_VERSION, ShopRecord,
                              binding_signature, resolve_metric_dependencies,
                              unsupported_reason)
from bi_agent.runtime.models import (
    ArtifactRef,
    DomainArtifact,
    DomainResult,
    DomainStatus,
    ErrorEnvelope,
    NewArtifact,
    NewQueryRun,
    RunCompletion,
    RunStatus,
    RunTransition,
)
from bi_agent.runtime.versions import VersionSet
from bi_agent.semantic_catalog.models import SemanticSelection
from bi_agent.semantic_catalog.registry import CATALOG, catalog_indexes
from bi_agent.semantic_catalog.retrieval import retrieve_schema_candidates

# 领域名与节点白名单只有一个真源：`runtime.domain_registry`。
EXPLORATION_DOMAIN = "controlled_sql_exploration"


class ExplorationNode(StrEnum):
    """固定链上的九个节点；枚举顺序就是推进顺序。"""

    SELECT_SCHEMA = "select_schema"
    AUTHORIZE_SCOPE = "authorize_scope"
    ASSESS_READINESS = "assess_readiness"
    COMPILE_QUERY = "compile_query"
    VALIDATE_AST = "validate_ast"
    ESTIMATE_COST = "estimate_cost"
    EXECUTE_READONLY = "execute_readonly"
    PERSIST_ARTIFACT = "persist_artifact"
    FINALIZE = "finalize"


EXPLORATION_CHAIN: tuple[ExplorationNode, ...] = tuple(ExplorationNode)
# 已进库的节点里，哪几格之后就不该再有任何数据库语句（用例按这条线判"提前终止"）。
PRE_DB_NODES = (ExplorationNode.SELECT_SCHEMA, ExplorationNode.AUTHORIZE_SCOPE,
                ExplorationNode.ASSESS_READINESS, ExplorationNode.COMPILE_QUERY,
                ExplorationNode.VALIDATE_AST)

# 两道 Task 4 的门各 5 秒 + 两次 0.1 秒 IO 余量 + 1 秒收尾：进库前的最低线。
EXECUTION_RESERVE_SECONDS = (2 * STATEMENT_TIMEOUT_MS / 1000.0
                             + 2 * DB_IO_RESERVE_SECONDS + 1.0)
# 只剩只读执行这一道门时的最低线。
FINAL_GATE_RESERVE_SECONDS = STATEMENT_TIMEOUT_MS / 1000.0 + DB_IO_RESERVE_SECONDS + 0.5

# 质量三态里 `unknown` 的那一句强制披露：文本与限制码都已在 `runtime.models` 登记
# （`_PUBLIC_LIMITATIONS` / `_LIMITATION_CODES`），与固定指标查询、运营图同一份。
QUALITY_UNVERIFIED_TEXT = "来源质量未核验（尚无对账记录）"
QUALITY_UNVERIFIED_CODE = "source_quality_unverified"

# 本切片唯一可用的授权列：别的（pool_id / warehouse_id）在这条链上没有授权通道。
SHOP_AUTHORIZATION_COLUMN = "shop_id"
# readiness 在服务端只读这一条事实：店 × 平台 × 已授予的能力标签（表名与 001 的视图逐字
# 相同）。覆盖、质量、时间口径与共同截止不在这里手写 SQL —— 那些判定与固定指标查询共用
# `data_quality.assess_query_coverage` 的同一份来源依赖表。
SHOP_STATEMENT = ("SELECT shop_id, platform, capabilities FROM reporting.v_shops "
                  "WHERE shop_id = ANY(%s)")



@dataclass(frozen=True)
class _Refusal:
    """一个终止原因的五份说法：终态、运行状态、错误码、公开消息、限制码。

    放在一张表里是因为它们必须同时改：同一个原因在运行表、事件与模型消息里长成两套
    话，恢复层就只能去猜。限制码为 `None` 读作"这不是可披露的限制，是失败"。
    """

    status: DomainStatus
    run_status: RunStatus
    code: str
    message: str
    limitation: str | None = None
    # 终止原因只能取自码表：需要区分"哪件事"而没有第五个码时，用同一句合法原因、
    # 换不同的终态与错误码归因（`concept_unregistered` 就是这一条）。
    termination: str | None = None


REFUSALS: Mapping[str, _Refusal] = MappingProxyType({
    # 固定 Tool 能表达：不是错误回答，而是"换条路问"。
    "fixed_tool_available": _Refusal(
        DomainStatus.NEEDS_INPUT, RunStatus.NEEDS_INPUT, "invalid_parameters",
        "查询参数无效，请调整后重试。"),
    # 检索无法唯一定位：缺的是澄清。
    "schema_ambiguous": _Refusal(
        DomainStatus.NEEDS_INPUT, RunStatus.NEEDS_INPUT, "invalid_parameters",
        "查询参数无效，请调整后重试。"),
    # 目录里没有这个概念：未注册概念不许降级成 SQL。
    # 目录里没有这个概念：不降级成 SQL。库里没有第五个新码，所以终止原因仍记在
    # `schema_ambiguous`（检索解不出语义）上，但终态是 missing_data 而不是 needs_input：
    # 换窗口救不了它，用户要改的是问法。
    "concept_unregistered": _Refusal(
        DomainStatus.MISSING_DATA, RunStatus.MISSING_DATA, "invalid_parameters",
        "查询参数无效，请调整后重试。", termination="schema_ambiguous"),
    # 授权集合为空、缺引用，或作用域只能按池授权。
    "forbidden": _Refusal(
        DomainStatus.FAILED, RunStatus.FAILED, "forbidden", "查询范围无权限。",
        "forbidden"),
    "capability_unavailable": _Refusal(
        DomainStatus.MISSING_DATA, RunStatus.MISSING_DATA, "unavailable",
        "本次查询的指标能力尚未开通，换成已开通的指标或先完成来源核验后再查。",
        "capability_unavailable"),
    "source_not_onboarded": _Refusal(
        DomainStatus.MISSING_DATA, RunStatus.MISSING_DATA, "unavailable",
        "该店铺的数据来源尚未开通，调整日期范围不会补上这段数据。",
        "source_not_onboarded"),
    "coverage_incomplete": _Refusal(
        DomainStatus.MISSING_DATA, RunStatus.MISSING_DATA, "unavailable",
        "所查时间段的数据覆盖不足，可按建议窗口查询或等待回填完成。",
        "coverage_incomplete"),
    "source_quality_failed": _Refusal(
        DomainStatus.FAILED, RunStatus.FAILED, "unavailable",
        "来源质量核验未通过，暂时不能出数。", "source_quality_failed"),
    # 付款时间口径只有"实测成立"这一档算证据：未测与实测不成立都不够格发聚合数。
    "coverage_time_basis_unverified": _Refusal(
        DomainStatus.MISSING_DATA, RunStatus.MISSING_DATA, "unavailable",
        "该来源的付款时间口径尚未完成对照取证，不能按完整支付窗口出数。",
        "coverage_time_basis_unverified"),
    # 共同截止未知：任一依赖没推进 data_as_of，就不能声称数据新到什么时候。
    "data_as_of_unknown": _Refusal(
        DomainStatus.MISSING_DATA, RunStatus.MISSING_DATA, "unavailable",
        "查询暂不可用，请稍后重试。", "data_as_of_unknown"),
    # 同一指标跨店来自不同口径/通道：两份数相加不是任何一个问题的答案。
    "basis_incompatible": _Refusal(
        DomainStatus.MISSING_DATA, RunStatus.MISSING_DATA, "invalid_parameters",
        "这些范围的统计口径不兼容，不能汇总或比较；请按店铺分列后逐组查看。",
        "basis_incompatible", termination="contract_violation"),
    # 编译器接不下这份选择：选择与目录不自洽，属契约违规，不是策略拒绝。
    "contract_violation": _Refusal(
        DomainStatus.FAILED, RunStatus.FAILED, "result_contract_violation",
        "查询结果异常。"),
    "sql_policy_rejected": _Refusal(
        DomainStatus.FAILED, RunStatus.FAILED, "unavailable", "查询结果异常。"),
    "query_cost_exceeded": _Refusal(
        DomainStatus.FAILED, RunStatus.FAILED, "unavailable",
        "本次查询时间预算已耗尽，请缩小日期或店铺范围后重试。", "deadline_exceeded"),
    "deadline_exceeded": _Refusal(
        DomainStatus.FAILED, RunStatus.FAILED, "deadline_exceeded",
        "本次查询时间预算已耗尽，请缩小日期或店铺范围后重试。", "deadline_exceeded"),
    "query_timeout": _Refusal(
        DomainStatus.FAILED, RunStatus.FAILED, "deadline_exceeded",
        "查询已超时，请稍后重试。", "query_timeout"),
    "result_too_large": _Refusal(
        DomainStatus.FAILED, RunStatus.FAILED, "unavailable", "查询结果异常。",
        "result_too_large"),
    "persistence_failed": _Refusal(
        DomainStatus.FAILED, RunStatus.FAILED, "artifact_persistence_failed",
        "结果保存失败，请稍后重试。"),
})


@dataclass(frozen=True)
class ExplorationExecution:
    """计划 Task 5 固定的返回形状。

    `plan` 在编译/策略之前失败时是 `None`：没有计划就没有 fingerprint，硬造一个空串
    会让"这次到底跑没跑 SQL"看不出来。
    """

    domain_result: DomainResult
    plan: ValidatedQueryPlan | None


@dataclass
class _Runtime:
    """一次探索的可变现场。"""

    question: str
    context: DomainContext
    request: ExplorationRequest
    versions: VersionSet
    run_id: UUID | None = None
    revision: int = 0
    # 最后一次成功落库的 revision 镜像：半途失败时的收尾要用它，不用预加值。
    persisted: int = 0
    finalized: bool = False
    node: ExplorationNode = ExplorationNode.SELECT_SCHEMA
    selection: SemanticSelection | None = None
    shop_ids: tuple[str, ...] = ()
    draft: Any = None
    plan: ValidatedQueryPlan | None = None
    columns: list[ExplorationColumn] = field(default_factory=list)
    rows: list[dict[str, object]] = field(default_factory=list)
    result: ExplorationResult | None = None
    published: list[ArtifactRef] = field(default_factory=list)
    diagnostic: UUID | None = None
    basis: list[dict[str, str]] = field(default_factory=list)
    # 三态之一：`unknown` 可出数但必须披露（`data_quality` 的口径），`failed` 已在门禁拒。
    quality_status: str = "passed"
    source_batches: tuple[str, ...] = ()
    refusal: str | None = None
    data_as_of: datetime | None = None
    coverage: dict[str, object] | None = None
    visited: list[ExplorationNode] = field(default_factory=list)


# ---------------------------------------------------------------------------
# 驱动
# ---------------------------------------------------------------------------

def run_exploration_graph(*, question: str, request: ExplorationRequest,
                          context: DomainContext,
                          versions: VersionSet) -> ExplorationExecution:
    """按固定链执行一次受控探索，并把每一格的推进写进运行记录。

    `question` 是**当前这轮**用户原文：检索只按它重做一次，不接受调用方递进来的
    selection（那等于让模型自选的 ref 变成服务端的事实）。
    """
    if not isinstance(request, ExplorationRequest):
        raise TypeError("exploration_request_contract_required")
    if not isinstance(question, str) or not question.strip():
        raise ValueError("exploration_question_required")
    runtime = _Runtime(question=question, context=context, request=request,
                       versions=versions)
    _create_run(runtime)
    steps: tuple[tuple[ExplorationNode, Any], ...] = (
        (ExplorationNode.SELECT_SCHEMA, _select_schema),
        (ExplorationNode.AUTHORIZE_SCOPE, _authorize_scope),
        (ExplorationNode.ASSESS_READINESS, _assess_readiness),
        (ExplorationNode.COMPILE_QUERY, _compile_query),
        (ExplorationNode.VALIDATE_AST, _validate_ast),
        (ExplorationNode.ESTIMATE_COST, _estimate_cost),
        (ExplorationNode.EXECUTE_READONLY, _execute_readonly),
        (ExplorationNode.PERSIST_ARTIFACT, _persist_artifact),
    )
    # 半死的运行不能留在 `running`：唯一键 (user_message_id, domain, attempt_no) 会占住
    # 同一次尝试的重放，而会话侧没有回收器。与经营/运营/价审/库存四张图同一做派 ——
    # 先 best-effort 落一个终态，再把原异常原样上抛交给外层脱敏。
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
    return ExplorationExecution(domain_result=_domain_result(runtime), plan=runtime.plan)


def _finish_run_as_failed(runtime: _Runtime) -> None:
    """尽力把这轮收尾成 FAILED：写不进去就吞掉收尾自己的错误，不替换在飞的异常。

    `expected_revision` 用**最后一次成功写入**的镜像值（不是 `_enter` 预加的那个），
    否则半途失败时这里会再撞一次 stale，终态就永远落不下去。
    """
    if runtime.run_id is None or runtime.finalized:
        return
    runtime.node = ExplorationNode.FINALIZE
    error = ErrorEnvelope(code="unavailable", stage=ExplorationNode.FINALIZE.value,
                          retryable=True, recovery="retry_later",
                          public_message="查询暂不可用，请稍后重试。",
                          problems=["unavailable"])
    with contextlib.suppress(Exception):
        runtime.context.store.finish(runtime.run_id, RunCompletion(
            expected_revision=runtime.persisted, node=ExplorationNode.FINALIZE.value,
            status=RunStatus.FAILED,
            state={**_state(runtime, node=ExplorationNode.FINALIZE,
                            status=RunStatus.FAILED, error=error),
                   "target_status": DomainStatus.FAILED.value},
            payload={"tool_status": "unavailable",
                     "target_status": DomainStatus.FAILED.value},
            error_code="unavailable", termination_reason="upstream_unavailable"))
    runtime.finalized = True


def _create_run(runtime: _Runtime) -> None:
    context = runtime.context
    normalized = _normalized_request(runtime)
    runtime.run_id = context.store.create_run(NewQueryRun(
        chat_id=context.chat_id, user_message_id=context.user_message_id,
        subject_id=context.subject_id, tool_call_id=f"toolu_exploration_{uuid4().hex[:16]}",
        domain=EXPLORATION_DOMAIN, attempt_no=context.attempt_no,
        normalized_request=normalized,
        state={**_state(runtime, node=ExplorationNode.SELECT_SCHEMA,
                        status=RunStatus.RUNNING), "revision": 0}))


def _enter(runtime: _Runtime, node: ExplorationNode) -> None:
    """推进一格：只写节点与状态，不写任何业务原文。"""
    runtime.node = node
    runtime.visited.append(node)
    runtime.revision += 1
    runtime.context.store.transition(runtime.run_id, RunTransition(
        expected_revision=runtime.revision - 1, node=node.value,
        status=RunStatus.RUNNING,
        state=_state(runtime, node=node, status=RunStatus.RUNNING)))
    runtime.persisted = runtime.revision


def _state(runtime: _Runtime, *, node: ExplorationNode, status: RunStatus,
           error: ErrorEnvelope | None = None) -> dict[str, object]:
    state: dict[str, object] = {
        "node": node.value, "status": status.value, "revision": runtime.revision,
        "normalized_request": _normalized_request(runtime)}
    if runtime.run_id is not None:
        state["run_id"] = str(runtime.run_id)
    if runtime.published:
        state["artifact_refs"] = [{"id": str(ref.id), "type": ref.type}
                                  for ref in runtime.published]
    if runtime.coverage is not None:
        state["coverage"] = runtime.coverage
    if runtime.data_as_of is not None:
        state["data_as_of"] = runtime.data_as_of.isoformat()
    codes = _limitation_codes(runtime)
    if codes:
        state["limitations"] = codes
    if error is not None:
        state["error"] = error.model_dump(mode="json")
    return state


def _normalized_request(runtime: _Runtime) -> dict[str, object]:
    """规范化请求只存引用与窗口：语义 ref 与 SQL 都不进这一列。"""
    context = runtime.context
    request = runtime.request
    normalized: dict[str, object] = {
        "shop_refs": sorted(context.shop_refs[shop] for shop in runtime.shop_ids)
        or sorted(context.shop_refs.values())}
    if request.start is not None and request.end is not None:
        normalized["start"] = request.start.isoformat()
        normalized["end"] = request.end.isoformat()
    return normalized


def _limitation_codes(runtime: _Runtime) -> list[str]:
    """成功路径也能带限制码：`quality_status="unknown"` 的强制披露就走这里。

    `data_quality` 的口径是三态：`passed` 干净、`unknown` 可出数但必须披露、`failed` 禁止
    出数（ readiness 已拒）。把中间那态折进"干净"就是丢掉一句必须的披露。
    """
    if runtime.refusal is not None:
        limitation = REFUSALS[runtime.refusal].limitation
        return [limitation] if limitation else []
    return [QUALITY_UNVERIFIED_CODE] if runtime.quality_status == "unknown" else []


def _published_limitations(runtime: _Runtime) -> list[str]:
    """公开载荷里的披露文本：取自已登记的 `PublicMessage`/限制文本词表，不自造句子。"""
    if runtime.quality_status == "unknown":
        return [QUALITY_UNVERIFIED_TEXT]
    return []


def _event_payload(runtime: _Runtime, *, status: RunStatus) -> dict[str, object]:
    """事件载荷只带码表里的状态：`target_status` 是领域终态，不是运行表那一列的值。"""
    payload: dict[str, object] = {"tool_status": _tool_status(status),
                                  "target_status": _terminal(runtime)[0].value}
    codes = _limitation_codes(runtime)
    if codes:
        payload["limitation_codes"] = codes
    if runtime.coverage is not None:
        payload["coverage_status"] = runtime.coverage["status"]
    if runtime.data_as_of is not None:
        payload["data_as_of"] = runtime.data_as_of.isoformat()
    if runtime.published:
        payload["artifact_refs"] = [{"id": str(ref.id), "type": ref.type}
                                    for ref in runtime.published]
        payload["result_count"] = len(runtime.rows)
    return payload


def _tool_status(status: RunStatus) -> str:
    return {RunStatus.SUCCEEDED: "ok", RunStatus.NEEDS_INPUT: "invalid_parameters",
            RunStatus.MISSING_DATA: "missing_data", RunStatus.FAILED: "unavailable",
            RunStatus.PARTIAL: "partial", RunStatus.RUNNING: "running"}[status]


def _refuse(runtime: _Runtime, reason: str) -> None:
    """记一次终止原因：原因只能取自码表，图不在现场发明新说法。"""
    if reason not in REFUSALS:
        raise ValueError("exploration_termination_reason_unknown")
    runtime.refusal = reason


# ---------------------------------------------------------------------------
# 1) select_schema
# ---------------------------------------------------------------------------

def _select_schema(runtime: _Runtime) -> None:
    """服务端按当前问题检索：模型给的 ref 到这一步只会被重新解一次。"""
    try:
        selection = retrieve_schema_candidates(
            runtime.question, allowed_domains=frozenset(domains()),
            current_versions=runtime.versions)
    except ValueError:
        # 目录版本不认识这份 VersionSet：没有合法的解析路径，只能当场停。
        _refuse(runtime, "schema_ambiguous")
        return
    runtime.selection = selection
    if selection.requires_clarification:
        # 检索自己说「这一轮没定下来」：猜一个视图就是把未确认的问题当成已确认的。
        _refuse(runtime, "schema_ambiguous")
        return
    if selection.missing_concepts:
        # 未注册概念不降级成 SQL（计划 Global Constraints）。
        _refuse(runtime, "concept_unregistered")
        return
    if not selection.view_refs:
        # 一个候选视图都没有：没有解析路径。编译器当然也编不出东西，但原因要说对。
        _refuse(runtime, "schema_ambiguous")
        return


# ---------------------------------------------------------------------------
# 2) authorize_scope
# ---------------------------------------------------------------------------

def _authorize_scope(runtime: _Runtime) -> None:
    """先问"固定 Tool 能不能表达"，再谈服务端授权与授权列形状。"""
    selection = runtime.selection
    assert selection is not None
    if fixed_tool_for(selection, runtime.request) is not None:
        _refuse(runtime, "fixed_tool_available")
        return
    context = runtime.context
    shops = tuple(sorted(str(shop) for shop in context.allowed_shop_ids))
    if not shops or any(shop not in context.shop_refs for shop in shops):
        # 没有 opaque 引用可发：投影那一格会把"不知道属于哪家店"的行整条拒掉。
        _refuse(runtime, "forbidden")
        return
    if any(shop in context.allowed_inventory_pool_ids for shop in shops):
        # 池 id 混进店铺集合：店铺授权与池授权相互独立，绝不互相 substitute。
        _refuse(runtime, "forbidden")
        return
    if not _shop_authorized_views(selection):
        _refuse(runtime, "forbidden")
        return
    runtime.shop_ids = shops


def _shop_authorized_views(selection: SemanticSelection) -> bool:
    """本轮选中的每个视图都必须按店铺授权，否则这条链没有授权通道。"""
    catalog = CATALOG if selection.catalog_version == CATALOG.version else None
    if catalog is None:
        return False
    indexes = catalog_indexes(catalog)
    for view_ref in selection.view_refs:
        view = indexes.views.get(view_ref)
        if view is None:
            return False
        field = indexes.fields.get(view.authorization_field_ref)
        if field is None or field.column != SHOP_AUTHORIZATION_COLUMN:
            return False
    return True


# ---------------------------------------------------------------------------
# 3) assess_readiness
# ---------------------------------------------------------------------------

def _attested_metric_codes(runtime: _Runtime) -> list[tuple[str, str]] | None:
    """把请求里的指标 ref 换成**已登记**的能力标签；有一条换不出就返回 `None`。

    014 的口径是"能力标签与指标同名"，所以目录里那条字段的**列名**就是它在来源注册表
    里的标签（`sources.METRIC_CAPABILITIES`）。除此之外本层不认任何映射：比值型指标有
    好几个必需字段（编译器也不接），`cost_total` / `raw_cost` / `list_amount` /
    `available_quantity` 这类列没有已登记的来源依赖表 —— 猜一条 view→来源 映射就是
    平行实现，fail-closed 的方向是**拒答**（计划 Global Constraints：缺能力、缺覆盖、
    来源未认证都不得降级到 SQL）。
    """
    entries = catalog_indexes(CATALOG)
    resolved: list[tuple[str, str]] = []
    for ref in runtime.request.requested_metric_refs:
        metric = entries.metrics.get(ref)
        if metric is None or len(metric.required_field_refs) != 1:
            return None
        field = entries.fields.get(metric.required_field_refs[0])
        if field is None or field.column not in METRIC_CAPABILITIES:
            return None
        resolved.append((ref, field.column))
    return resolved


def _assess_readiness(runtime: _Runtime) -> None:
    """逐指标判能力、来源、质量、覆盖、时间口径与共同截止：全部复用既有已登记契约。

    1. 能力（`sources.unsupported_reason`）：按**店 × 指标**判，不再拿"这家店有任何
       标签"当"这个指标已开通"——014 说清了标签是逐指标的。
    2. 覆盖 / 质量 / 时间口径 / 共同截止（`data_quality.assess_query_coverage`）：与固定
       指标查询同一个引擎、同一份依赖表（`sources.resolve_metric_dependencies`），
       按 (店铺 × 来源 × 实体) 分组一次查完，不逐店往返。
    3. 探索层比固定路径只严不宽：时间口径必须**已认证**（`blocking` 与 `disclosure` 都
       拒），共同截止必须存在（缺任何一条依赖的 `data_as_of` 就拒），跨店口径签名必须
       一致（不一致就是"两个问题的答案相加"）。
    4. 质量仍是三态，不折成两态：`failed` 拒答，`unknown` 可出数但必须带既有那句披露
       （`limitations` 进公开载荷，`source_quality_unverified` 进运行状态与事件）。
    """
    request = runtime.request
    pairs = _attested_metric_codes(runtime)
    if pairs is None:
        _refuse(runtime, "capability_unavailable")
        return
    if request.start is None or request.end is None:
        # 没有窗口就没有可证明的覆盖：不猜"整表都算完整"。
        _refuse(runtime, "coverage_incomplete")
        return
    codes = [code for _ref, code in pairs]
    context = runtime.context
    shops = list(runtime.shop_ids)
    try:
        rows = list(context.conn.execute(SHOP_STATEMENT, (shops,)).fetchall())
    except Exception:  # noqa: BLE001 - 证据读不到就是没证据，不带出数据库原文
        _refuse(runtime, "source_not_onboarded")
        return
    records = {str(row[0]): ShopRecord.from_row(row[0], row[1], row[2]) for row in rows}
    if any(shop not in records for shop in shops):
        _refuse(runtime, "source_not_onboarded")
        return
    for shop in shops:
        for code in codes:
            reason = unsupported_reason(records[shop], code)
            if reason is None:
                continue
            _refuse(runtime, "source_not_onboarded" if reason == "source_unregistered"
                    else "capability_unavailable")
            return
    try:
        probe = QueryRequest(start=request.start.isoformat(), end=request.end.isoformat(),
                             shop_ids=shops, metrics=codes,
                             group_by="product" if all(code in PRODUCT_METRICS
                                                       for code in codes) else "total")
        assessment = assess_query_coverage(context.conn, probe)
    except Exception:  # noqa: BLE001 - 依赖解不出就没有可发数的证据
        _refuse(runtime, "contract_violation")
        return
    runtime.coverage = _coverage_from(assessment)
    if assessment.source_unconfigured:
        _refuse(runtime, "source_not_onboarded")
        return
    if assessment.quality_status == "failed":
        _refuse(runtime, "source_quality_failed")
        return
    if assessment.status != "complete":
        # 部分覆盖也不发：跨缺口的聚合数不是"少一点的全量"，而是另一个问题的答案。
        _refuse(runtime, "coverage_incomplete")
        return
    if assessment.time_basis_blocking or assessment.time_basis_disclosure:
        _refuse(runtime, "coverage_time_basis_unverified")
        return
    if assessment.data_as_of is None:
        # 共同截止未知：`max()` 一个别处的截止当"数据新到什么时候"就是编。
        _refuse(runtime, "data_as_of_unknown")
        return
    basis = _basis_entries(runtime, pairs, records)
    if basis is None:
        _refuse(runtime, "basis_incompatible")
        return
    runtime.basis = basis
    runtime.quality_status = assessment.quality_status
    runtime.data_as_of = assessment.data_as_of
    runtime.source_batches = assessment.source_batches


def _coverage_from(assessment) -> dict[str, object]:
    """覆盖载荷用全仓已有的 `Coverage` 形状：缺口逐条可查，不为探索另立一份口径。"""
    # `missing_windows` 是引擎算好的 (ISO 起, ISO 止) 日期对：与固定指标查询对外披露的
    # 缺口同一形状（`CoverageGap` 里的 shop_id/source 只留在服务端，不进公开载荷）。
    start, end = assessment.requested_window
    return {"status": assessment.status, "start": start, "end": end,
            "gaps": [f"{gap_start}~{gap_end}" for gap_start, gap_end
                     in assessment.missing_windows]}


def _basis_entries(runtime: _Runtime, pairs: list[tuple[str, str]],
                   records: Mapping[str, ShopRecord]) -> list[dict[str, str]] | None:
    """口径凭证由来源注册表解析：一条依赖一行 `basis`/`time_basis`，都是注册表里的实测值。

    兼容性判据照抄既有契约（`metrics._incompatible_metrics`）：同一个指标在**各店之间**的
    整条依赖签名必须一致，否则返回 `None` ——两份不同口径的数相加不是任何一个问题的答案。
    一个指标自身有几条依赖不在这里判：`cash_difference` 合法地同时要订单与退款发生两个来源，
    逐条比会把它永久拒掉。注册表版本一并带上，口径版本一变旧结果就不可复用。
    """
    entries: list[dict[str, str]] = []
    seen: set[tuple[str, str, str, str]] = set()
    # 签名按**整个 (店 × 指标) 依赖表**算一次，再跨店比：与 `metrics._incompatible_metrics`
    # 同一条规则。逐条 binding 各算一份会把"一个指标本来就要两个来源"（`cash_difference`
    # = 订单 + 退款发生）读成口径互不兼容，那是把可答的问题永久拒掉。
    signatures: dict[str, set[tuple[tuple[str, str, str], ...]]] = {}
    for ref, code in pairs:
        for shop in runtime.shop_ids:
            bindings = resolve_metric_dependencies(records[shop], code)
            if not bindings:
                return None
            signatures.setdefault(ref, set()).add(binding_signature(bindings))
            for binding in bindings:
                item = {"metric": ref, "basis": binding.basis,
                        "time_basis": binding.time_basis,
                        "metric_version": METRIC_VERSION,
                        "shop_ref": runtime.context.shop_refs[shop]}
                if tuple(sorted(item.items())) in seen:
                    continue
                seen.add(tuple(sorted(item.items())))
                entries.append(item)
    if any(len(values) > 1 for values in signatures.values()):
        return None
    return sorted(entries, key=lambda item: (item["metric"], item["shop_ref"],
                                             item["basis"], item["time_basis"]))



# ---------------------------------------------------------------------------
# 4) compile_query
# ---------------------------------------------------------------------------

def _compile_query(runtime: _Runtime) -> None:
    try:
        runtime.draft = compile_query(runtime.request, selection=runtime.selection,
                                      allowed_shop_ids=frozenset(runtime.shop_ids))
    except ValueError:
        _refuse(runtime, "contract_violation")
        return


# ---------------------------------------------------------------------------
# 5) validate_ast
# ---------------------------------------------------------------------------

def _validate_ast(runtime: _Runtime) -> None:
    try:
        runtime.plan = validate_exploration_plan(
            runtime.draft, selection=runtime.selection, context=runtime.context)
    except ValueError:
        _refuse(runtime, "sql_policy_rejected")
        return


# ---------------------------------------------------------------------------
# 6) estimate_cost / 7) execute_readonly
# ---------------------------------------------------------------------------

def _estimate_cost(runtime: _Runtime) -> None:
    plan = runtime.plan
    assert plan is not None
    if _remaining(runtime) < EXECUTION_RESERVE_SECONDS:
        # 两道门都跑不完：不碰数据库，也不留半截查询。
        _refuse(runtime, "deadline_exceeded")
        return
    try:
        runtime.plan = estimate_plan(runtime.context.conn, plan,
                                     deadline=runtime.context.deadline)
    except Exception as error:  # noqa: BLE001 - 只留原因码，不留原文
        _refuse(runtime, _execution_reason(error))
        return


def _execute_readonly(runtime: _Runtime) -> None:
    plan = runtime.plan
    assert plan is not None
    if _remaining(runtime) < FINAL_GATE_RESERVE_SECONDS:
        _refuse(runtime, "deadline_exceeded")
        return
    try:
        columns, rows = execute_plan(runtime.context.conn, plan,
                                     deadline=runtime.context.deadline)
        declared, projected = project_result(columns, rows,
                                             shop_refs=runtime.context.shop_refs)
    except Exception as error:  # noqa: BLE001
        _refuse(runtime, _execution_reason(error))
        return
    runtime.columns = declared
    runtime.rows = projected
    # 指纹之后不许改：动了就是"发出去的结果与留证的语句不是同一条"。
    assert runtime.plan is not None and runtime.plan.statement_fingerprint == \
        plan.statement_fingerprint


def _execution_reason(error: object) -> str:
    """执行/投影层的稳定原因码 → 终止原因：按码分流，不读文字里的数字。"""
    text = str(error)
    if text.startswith("exploration_budget_exceeded"):
        return "query_cost_exceeded"
    if "deadline" in text:
        return "deadline_exceeded"
    if "timeout" in text:
        return "query_timeout"
    if "result_too_large" in text or "row_limit_exceeded" in text:
        return "result_too_large"
    return "sql_policy_rejected"


def _remaining(runtime: _Runtime) -> float:
    return runtime.context.deadline - time.monotonic()


# ---------------------------------------------------------------------------
# 8) persist_artifact
# ---------------------------------------------------------------------------

def _persist_artifact(runtime: _Runtime) -> None:
    plan = runtime.plan
    assert plan is not None and runtime.selection is not None
    payload = _result_payload(runtime, plan=plan)
    try:
        runtime.diagnostic = runtime.context.store.record_diagnostic(
            runtime.run_id, template_id=plan.template_version, sql_text=plan.sql_text,
            parameters={**_jsonable_parameters(plan.parameters),
                        "selected_refs": list(plan.selected_refs)})
    except Exception:  # noqa: BLE001 - 证据留不下就不发结论
        runtime.result = None
        _refuse(runtime, "persistence_failed")
        return
    try:
        ref = runtime.context.store.save_artifact(
            runtime.run_id, NewArtifact(artifact_type="exploration_result",
                                        payload=payload.model_dump(mode="json"),
                                        data_as_of=runtime.data_as_of,
                                        coverage=payload.coverage))
    except Exception:  # noqa: BLE001 - 必需结果存不下不是降级
        runtime.published = []
        runtime.result = None
        _refuse(runtime, "persistence_failed")
        return
    runtime.published = [ref]
    runtime.result = payload


def _jsonable_parameters(parameters: Mapping[str, Any]) -> dict[str, object]:
    """诊断表的 jsonb 只收 JSON 安全值：日期按 ISO 文本落，真值本身不变。

    不在这里过公开载荷那套护栏：这一条记录**就是**给审计看 SQL 与参数用的，把它
    洗成空对象等于没留证（计划 Task 5：SQL 原文只进 `bi.query_diagnostics`）。
    """
    clean: dict[str, object] = {}
    for key, value in parameters.items():
        if isinstance(value, (datetime, date)):
            clean[key] = value.isoformat()
        elif isinstance(value, (list, tuple)):
            clean[key] = [_jsonable_value(item) for item in value]
        else:
            clean[key] = _jsonable_value(value)
    return clean


def _jsonable_value(value: Any) -> object:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return str(value)


def _result_payload(runtime: _Runtime, *, plan: ValidatedQueryPlan) -> ExplorationResult:
    """公开载荷：结果 + 口径 + 覆盖 + 限制。**永远没有 SQL 与参数真值。**"""
    request = runtime.request
    return ExplorationResult(
        template_version=plan.template_version, catalog_version=plan.catalog_version,
        statement_fingerprint=plan.statement_fingerprint, columns=runtime.columns,
        rows=runtime.rows,
        # 口径凭证是 readiness 从来源注册表解析出的实测值（basis + time_basis +
        # metric_version + shop_ref），不是目录版本字符串：把版本当口径等于没给凭据。
        basis=list(runtime.basis),
        coverage=dict(runtime.coverage or {}),
        diagnostics=[], limitations=_published_limitations(runtime))


# ---------------------------------------------------------------------------
# 9) finalize 与公开结果
# ---------------------------------------------------------------------------

def _finalize(runtime: _Runtime) -> None:
    context = runtime.context
    _status, run_status, code, _message, _limitation = _terminal(runtime)
    runtime.node = ExplorationNode.FINALIZE
    runtime.visited.append(ExplorationNode.FINALIZE)
    runtime.revision += 1
    error = _error_envelope(runtime)
    context.store.finish(runtime.run_id, RunCompletion(
        expected_revision=runtime.persisted, node=ExplorationNode.FINALIZE.value,
        status=run_status,
        state=_state(runtime, node=ExplorationNode.FINALIZE, status=run_status,
                     error=error),
        payload=_event_payload(runtime, status=run_status),
        error_code=code if error is not None else None,
        termination_reason=_termination_reason(runtime)))
    # 只有写成功才算收尾完成：失败时留给 `_finish_run_as_failed` 补一个终态。
    runtime.persisted = runtime.revision
    runtime.finalized = True


def _terminal(runtime: _Runtime) -> tuple[DomainStatus, RunStatus, str, str, str | None]:
    if runtime.refusal is None:
        return (DomainStatus.SUCCESS, RunStatus.SUCCEEDED, "", "", None)
    refusal = REFUSALS[runtime.refusal]
    return (refusal.status, refusal.run_status, refusal.code, refusal.message,
            refusal.limitation)


def _termination_reason(runtime: _Runtime) -> str:
    if runtime.refusal is None:
        return "succeeded"
    return REFUSALS[runtime.refusal].termination or runtime.refusal


def _error_envelope(runtime: _Runtime) -> ErrorEnvelope | None:
    if runtime.refusal is None:
        return None
    refusal = REFUSALS[runtime.refusal]
    return ErrorEnvelope(
        code=refusal.code, stage=runtime.node.value,
        retryable=refusal.code in {"deadline_exceeded", "artifact_persistence_failed",
                                   "unavailable"},
        recovery=_recovery(refusal.status), public_message=refusal.message,
        problems=[refusal.code])


def _recovery(status: DomainStatus) -> str:
    return {DomainStatus.NEEDS_INPUT: "correct_parameters",
            DomainStatus.MISSING_DATA: "ask_user",
            DomainStatus.FAILED: "retry_later"}[status]


def _domain_result(runtime: _Runtime) -> DomainResult:
    status, _run_status, _code, _message, _limitation = _terminal(runtime)
    artifacts: list[DomainArtifact] = []
    if runtime.result is not None and runtime.published:
        artifacts = [DomainArtifact(
            ref=runtime.published[0],
            public_payload=runtime.result.model_dump(mode="json"))]
    return DomainResult(run_id=runtime.run_id, status=status,
                        model_payload=_model_payload(runtime, status=status),
                        artifacts=artifacts, data_as_of=runtime.data_as_of,
                        error=_error_envelope(runtime))


def _model_payload(runtime: _Runtime, *, status: DomainStatus) -> dict[str, object]:
    """成功发探索载荷本身；拒答发最小形状：没有可发的结果就别装出一副有数的样子。"""
    if status is DomainStatus.SUCCESS and runtime.result is not None:
        return runtime.result.model_dump(mode="json")
    return {"status": _tool_status(_terminal(runtime)[1]),
            "termination_reason": _termination_reason(runtime),
            "limitations": []}


__all__ = ["EXPLORATION_CHAIN", "EXPLORATION_DOMAIN", "ExplorationExecution",
           "ExplorationNode", "run_exploration_graph"]
