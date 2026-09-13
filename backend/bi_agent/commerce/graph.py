"""CommercePerformanceGraph：spec §6 的固定节点链（运营工作流计划 Task 7）。

    resolve_scope → resolve_product_if_needed → resolve_metric_basis
    → check_capabilities_and_coverage → freeze_versions → plan_fixed_queries
    → execute_aggregates → compute_metrics → build_comparison_and_trend
    → classify_findings → persist_artifacts → finalize

三条结构性约束：

1. **门禁在聚合之前，缺口分组不并入合计**。能力、时间口径、覆盖三类缺口各自归因；
   一家店回答不了就进 `excluded_scope`，它的数字既不出现也不参与合计与排名，缺数据
   不会被读成 0。多指标独立判定：能答销量就先给销量，不要求全部指标同时可算。
2. **一次只读快照**。覆盖判定、跨店合计、七日序列、上期比较都在同一个
   REPEATABLE READ 事务里读出：中途插进一次回填就会把两个数据版本拼成一份报告。
   该事务是只读的，所以这一段的状态写入按原顺序延后到快照退出之后再落库。
3. **降级有边界**。单指标 / 单来源不可用可以是 partial；授权失败、契约违规与
   必需 Artifact 保存失败一律 failed，不许冒充成功。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Any, Callable, Mapping, Sequence

import psycopg

from bi_agent.business_query.graph import _finish_run_as_failed, _persist_transition
from bi_agent.business_query.nodes import _event_payload, _run_status
from bi_agent.business_query.state import BusinessQueryState
from bi_agent.catalog import build_catalog
from bi_agent.catalog.models import ref_for_key
from bi_agent.catalog.resolver import ProductResolution, Selector, resolve_product
from bi_agent.data_quality import (
    QUALITY_RULE, CoverageAssessment, assess_query_coverage, attribution_gap,
    describe_attribution_gap, money_text, switched_sources_between, unverified_payments)
from bi_agent.metrics import (
    Coverage, MAX_ROWS, QueryRequest, ToolResult, _BudgetExhausted, _RowsTruncated,
    _capability_gap, _window_range, read_only_snapshot)
from bi_agent.runtime.artifacts import (
    QueryProvenance, RequestIdentity, basis_signature_of, request_fingerprint)
from bi_agent.runtime.domain_registry import COMMERCE_NODES, spec_for
from bi_agent.runtime.models import (
    DomainArtifact,
    DomainResult,
    DomainStatus,
    ErrorEnvelope,
    NewArtifact,
    NewQueryRun,
    RecoveryAction,
    RunCompletion,
    RunStatus,
)
from bi_agent.sources import (
    AFTERSALE_COHORT_ENTITY, AFTERSALE_ENTITY, ORDERS_ENTITY, PAYMENT_WINDOW_METRICS,
    ShopRecord, binding_signature, registration, resolve_metric_dependencies,
    unsupported_reason)

from . import repository
from .metrics import (
    COMMERCE_GRAPH_VERSION,
    COMMERCE_METRIC_CAPABILITIES,
    COMMERCE_METRIC_DEFINITIONS,
    COMMERCE_METRIC_VERSION,
    DOCUMENT_ROWS,
    METRIC_FACET,
    PAYMENT_ROWS,
    PRODUCT_FACET_METRICS,
    PRODUCT_ROWS,
    PRODUCT_TOTAL_ROWS,
    TREND_ROWS,
    combine_reference_metrics,
    low_profit_candidates,
    money_of,
    project,
    sales_shares,
    to_decimal,
)
from .models import CommerceDataset, CommerceReport, DomainContext, ProductPerformanceRequest

DOMAIN = "commerce_performance"
TEMPLATE_ID = "commerce_product_report"
TEMPLATE_VERSION = "1"
# 歧义候选卡片的张数上限：候选全集可能很大，一次性发给模型只会让它自己在长列表里猜。
# 披露文本里给的是**全量**家数，这里只是可核对的前几张。
MAX_CANDIDATE_CARDS = 20
# 套件 / 组合 / 加工父项的成本语义未核验：销量与金额照常计入，但不参与商品毛利
# （spec §5.2 点名这三类"成本语义不清"）。
COST_SEMANTIC_UNVERIFIED_KINDS = frozenset({"suite", "combination", "processing"})
# 覆盖缺口按业务实体归因；`entity == 指标名` 的那一类是"这个平台的来源拿不到该口径"，
# 属于能力缺口，已由能力门禁处理，不能再算成缺覆盖。
_REAL_ENTITIES = frozenset({ORDERS_ENTITY, AFTERSALE_ENTITY, AFTERSALE_COHORT_ENTITY})
# 能力标签 → 覆盖判定所用的分组形态（商品标签与固定指标的 product 分组共用依赖）。
_TAG_GROUP_BY = {"quantity": "product", "product_paid_amount": "product"}


class CommerceNode(StrEnum):
    RESOLVE_SCOPE = "resolve_scope"
    RESOLVE_PRODUCT = "resolve_product_if_needed"
    RESOLVE_METRIC_BASIS = "resolve_metric_basis"
    CHECK_CAPABILITIES_AND_COVERAGE = "check_capabilities_and_coverage"
    FREEZE_VERSIONS = "freeze_versions"
    PLAN_FIXED_QUERIES = "plan_fixed_queries"
    EXECUTE_AGGREGATES = "execute_aggregates"
    COMPUTE_METRICS = "compute_metrics"
    BUILD_COMPARISON_AND_TREND = "build_comparison_and_trend"
    CLASSIFY_FINDINGS = "classify_findings"
    PERSIST_ARTIFACTS = "persist_artifacts"
    FINALIZE = "finalize"


# 建链时就对照领域注册表：004 的 current_node 是无 CHECK 的文本列，领域与节点的配对
# 只有这一处说了算，漂移必须在导入期炸掉，而不是悄悄写进运行记录。
assert COMMERCE_NODES == frozenset(node.value for node in CommerceNode), \
    "经营图节点链必须与 runtime.domain_registry 登记的集合逐项一致"
assert spec_for(DOMAIN).nodes is COMMERCE_NODES


_NEXT_NODE: dict[CommerceNode | None, CommerceNode] = {
    None: CommerceNode.RESOLVE_SCOPE,
    CommerceNode.RESOLVE_SCOPE: CommerceNode.RESOLVE_PRODUCT,
    CommerceNode.RESOLVE_PRODUCT: CommerceNode.RESOLVE_METRIC_BASIS,
    CommerceNode.RESOLVE_METRIC_BASIS: CommerceNode.CHECK_CAPABILITIES_AND_COVERAGE,
    CommerceNode.CHECK_CAPABILITIES_AND_COVERAGE: CommerceNode.FREEZE_VERSIONS,
    CommerceNode.FREEZE_VERSIONS: CommerceNode.PLAN_FIXED_QUERIES,
    CommerceNode.PLAN_FIXED_QUERIES: CommerceNode.EXECUTE_AGGREGATES,
    CommerceNode.EXECUTE_AGGREGATES: CommerceNode.COMPUTE_METRICS,
    CommerceNode.COMPUTE_METRICS: CommerceNode.BUILD_COMPARISON_AND_TREND,
    CommerceNode.BUILD_COMPARISON_AND_TREND: CommerceNode.CLASSIFY_FINDINGS,
    CommerceNode.CLASSIFY_FINDINGS: CommerceNode.PERSIST_ARTIFACTS,
    CommerceNode.PERSIST_ARTIFACTS: CommerceNode.FINALIZE,
}


class InvalidCommerceTransition(Exception):
    def __init__(self) -> None:
        super().__init__("invalid_transition")


class UnsupportedReportKind(ValueError):
    """报告种类尚未实现：调用方必须显式处置，不许悄悄降级成商品报告。"""

    def __init__(self, report_kind: object) -> None:
        super().__init__(f"report_kind_unsupported:{report_kind}")


class CommerceState(BusinessQueryState):
    """同一份可持久化状态契约，只换节点类型。

    状态能带哪些键、限制能取哪些码、引用长什么样，全部由 `runtime.models` 一处决定；
    这里不复制第二份白名单。
    """

    node: CommerceNode = CommerceNode.RESOLVE_SCOPE


@dataclass
class QueryPlan:
    """本轮真正要跑的固定模板：读窗口与三个面各自的开关。"""

    read_window: tuple[date, date]
    product_facet: bool
    document_facet: bool
    payment_facet: bool


@dataclass
class CommerceRuntime:
    """图内可变状态：真实主键只活在这里，不进 Store、不进事件、不进模型载荷。"""

    state: CommerceState
    context: DomainContext
    tool_call_id: str
    request: ProductPerformanceRequest | None = None
    report: CommerceReport | None = None
    # `result` 是主数据集的 ToolResult：事件载荷与目录投影都按它取数。
    result: ToolResult | None = None
    catalog: Any = None
    provenance: QueryProvenance | None = None
    identity: RequestIdentity | None = None
    # 范围与商品解析
    profiles: dict[str, repository.ShopProfile] = field(default_factory=dict)
    candidate_shop_ids: tuple[str, ...] = ()
    resolution: ProductResolution | None = None
    # needs_input 时的候选卡片（只带引用，最多 MAX_CANDIDATE_CARDS 张）。
    candidates: tuple[dict[str, str], ...] = ()
    erp_product_id: str | None = None
    excluded: list[dict[str, Any]] = field(default_factory=list)
    # excluded 里只有引用，这里留真实主键：全部被排除时还要能把原因说成带家数的那句。
    excluded_ids: set[str] = field(default_factory=set)
    # 口径与门禁
    tags: tuple[str, ...] = ()
    bindings: dict[tuple[str, str], tuple] = field(default_factory=dict)
    main_gaps: dict[str, list[tuple[date, date]]] = field(default_factory=dict)
    blocking: dict[str, frozenset[str]] = field(default_factory=dict)
    disclosure: dict[str, frozenset[str]] = field(default_factory=dict)
    statuses: list[dict[str, Any]] = field(default_factory=list)
    basis: list[dict[str, str]] = field(default_factory=list)
    limitations: list[str] = field(default_factory=list)
    data_as_of: datetime | None = None
    source_batches: tuple[str, ...] = ()
    quality_status: str = "unknown"
    incomparable_metrics: frozenset[str] = frozenset()
    # 分类节点定下来的报告终态：它不是“提前终止”，而是“报告已成立，还等发布”。
    final_status: RunStatus | None = None
    # 窗口与聚合
    trend_window: tuple[date, date] = (date(1970, 1, 1), date(1970, 1, 2))
    previous_window: tuple[date, date] | None = None
    plan: QueryPlan | None = None
    lines: tuple[repository.ProductLine, ...] = ()
    documents: Mapping[str, repository.DocumentFacts] = field(default_factory=dict)
    payments: Mapping[str, repository.PaymentFacts] = field(default_factory=dict)
    rows: list[dict[str, Any]] = field(default_factory=list)
    totals: list[dict[str, Any]] = field(default_factory=list)
    trend_rows: list[dict[str, Any]] = field(default_factory=list)
    facet_rows: list[dict[str, Any]] = field(default_factory=list)
    comparison: dict[str, Any] | None = None
    opportunity: dict[str, Any] | None = None
    diagnostics: dict[str, Any] = field(default_factory=dict)
    # 目录版本是内部缓存，不进 diagnostics：diagnostics 会随结果发布，词表是固定的。
    catalog_version: int | None = None
    # 只读快照内的状态写入延后落库：快照只读，但节点顺序必须照原样留痕。
    pending: list[tuple[CommerceState, dict[str, object]]] = field(default_factory=list)
    # 已保存 Artifact 的 (引用, 公开载荷)：DomainResult 要带载荷，不让展示层二次投影。
    published: list[tuple[Any, dict[str, Any]]] = field(default_factory=list)
    # 覆盖判定按 (能力标签, 店铺集合, 窗口) 记忆：多个指标共用同一次集合查询。
    assessments: dict[tuple, CoverageAssessment] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# 推进、终止与投影材料
# ---------------------------------------------------------------------------


def transition_state(state: CommerceState, next_node: CommerceNode) -> CommerceState:
    """按固定链推进一步；跳步与回头一律拒绝。"""
    if _NEXT_NODE.get(state.node) is not next_node:
        raise InvalidCommerceTransition()
    return state.model_copy(update={"node": next_node})


def _record_of(runtime: CommerceRuntime, shop_id: str) -> ShopRecord:
    profile = runtime.profiles[shop_id]
    return ShopRecord.from_row(shop_id, profile.platform, profile.capabilities)


def _ref_of(runtime: CommerceRuntime, shop_id: str) -> str:
    return runtime.context.shop_refs.get(shop_id, ref_for_key("shop", shop_id))


def _exclude(runtime: CommerceRuntime, shop_id: str, reason: str,
             windows: Sequence[tuple[date, date]] = ()) -> None:
    """把一家获准店铺列入缺失组：原因取自注册表词表，缺口只给日期段。"""
    entry: dict[str, Any] = {"shop_ref": _ref_of(runtime, shop_id), "reason": reason}
    profile = runtime.profiles.get(shop_id)
    if profile is not None and profile.platform:
        entry["platform"] = profile.platform
    if windows:
        entry["windows"] = [f"{start.isoformat()}~{end.isoformat()}"
                            for start, end in windows]
    runtime.excluded.append(entry)
    runtime.excluded_ids.add(shop_id)
    dropped = {str(item["shop_ref"]) for item in runtime.excluded}
    runtime.candidate_shop_ids = tuple(
        shop for shop in runtime.candidate_shop_ids if _ref_of(runtime, shop) not in dropped)


def _requested_scope(runtime: CommerceRuntime) -> dict[str, Any]:
    request = runtime.request
    scope: dict[str, Any] = {"mode": "all_authorized"}
    if request is None:
        return scope
    scope["mode"] = request.scope.mode
    if request.scope.platforms:
        scope["platforms"] = list(request.scope.platforms)
    if request.scope.shop_refs:
        scope["shop_refs"] = list(request.scope.shop_refs)
    return scope


def _filters(runtime: CommerceRuntime) -> dict[str, Any]:
    request = runtime.request
    if request is None:
        return {}
    filters: dict[str, Any] = {
        "start": request.start.isoformat(),
        "end": request.end.isoformat(),
        "shop_ids": sorted(runtime.candidate_shop_ids),
        "metrics": list(request.metrics),
        "currency": request.currency,
        "sales_basis": request.sales_basis,
        "profit_basis": request.profit_basis,
        "report_kind": "product",
        # 份额只有一个合法分母：已评估集合。留着这个键让"分母是谁"可被核对。
        "sales_share_basis": "evaluated_only",
    }
    if request.scope.platforms:
        filters["platforms"] = list(request.scope.platforms)
    if runtime.erp_product_id is not None:
        filters["product_ref"] = ref_for_key("product", runtime.erp_product_id)
    return filters


def _normalized_request(runtime: CommerceRuntime) -> dict[str, Any]:
    """可持久化、可指纹化的请求：解析出来的商品引用一并写入。

    文本选择器必须落成引用，否则两个不同文本的问题会得到同一个指纹；一旦启用结果复用
    就会把别人的答案发给这个人。真实商品主键仍然不出现。
    """
    request = runtime.request
    if request is None:
        return dict(runtime.state.normalized_request)
    normalized = request.normalized()
    if runtime.erp_product_id is not None:
        normalized["product_ref"] = ref_for_key("product", runtime.erp_product_id)
    normalized["shop_refs"] = ([_ref_of(runtime, shop)
                                for shop in runtime.candidate_shop_ids]
                               or ["invalid_shop"])
    return normalized


# 披露文本 → 限制码。固定指标那批文本沿用同一份归因（同一种缺口不许在两个领域里各起
# 一个名字），经营面新增的披露自己登记。
_FIXED_LIMITATION_CODES = {
    "店铺不在授权范围": "forbidden",
    "本次查询时间预算已耗尽": "deadline_exceeded",
    "查询超时": "query_timeout",
    "店铺尚未同步，无法查询": "shop_not_synced",
    "部分店铺已停用，仅返回剩余范围": "shops_inactive",
    "来源质量核验未通过，拒绝出数": "source_quality_failed",
    "来源质量未核验（尚无对账记录）": "source_quality_unverified",
    "数据截止未知（回填未完成）": "data_as_of_unknown",
    "覆盖未完成，拒绝部分汇总；缺口见coverage.gaps": "coverage_incomplete",
}
_FIXED_CODE_BY_FRAGMENT = (
    (" 家店铺的来源尚未开通", "source_not_onboarded"),
    (" 家店铺缺少 ", "capability_unavailable"),
    (" 家店铺的付款时间口径未经认证", "coverage_time_basis_unverified"),
    (" 家店铺的结果来自未认证付款时间口径", "coverage_time_basis_unverified"),
    ("口径互不兼容", "basis_incompatible"),
    ("上期与本期数据来源不同", "basis_incompatible"),
    ("支付额中", "revenue_not_attributed"),
    ("未认证支付", "unverified_payments"),
    ("结果行数达到", "result_too_large"),
)
_COMMERCE_CODE_BY_FRAGMENT = (
    ("未列入本次合计", "commerce_scope_excluded"),
    ("已评估店铺集合的合计", "commerce_scope_excluded"),
    ("成本覆盖不全", "cost_coverage_incomplete"),
    ("套件/组合/加工父项", "cost_semantics_unverified"),
    ("分摊金额未核验", "allocation_unverified"),
    ("未扣售后", "gross_profit_not_net"),
    ("单据毛利覆盖不全", "erp_document_coverage_incomplete"),
    ("拆单", "erp_document_granularity"),
    ("两个口径面", "facets_not_additive"),
    ("已验证支付口径没有商品级事实", "payment_product_attribution_unavailable"),
    ("低利润阈值", "opportunity_policy_unconfigured"),
    ("趋势窗口覆盖不足", "trend_coverage_incomplete"),
    ("真实零成交", "trend_zero_days"),
    ("授权范围内没有可分析的店铺", "empty_scope"),
    ("商品未解析出来", "product_not_resolved"),
    ("没有该商品的成交行", "product_zero_rows"),
    ("候选商品命中同一文本", "ambiguous_product"),
)


def _limitation_codes(limitations: Sequence[str]) -> list[str]:
    """把披露文本换成码：未登记的文本一律不进状态（宁可少记，不可自创码）。"""
    codes: list[str] = []
    for limitation in limitations:
        code = _FIXED_LIMITATION_CODES.get(limitation)
        if code is None:
            for fragment, candidate in (_FIXED_CODE_BY_FRAGMENT
                                        + _COMMERCE_CODE_BY_FRAGMENT):
                if fragment in limitation:
                    code = candidate
                    break
        if code is not None and code not in codes:
            codes.append(code)
    return codes


# 限制码 / 错误码 → 终止原因：与 `runtime.artifacts.TERMINATION_REASONS` 以及 009+014 的
# SQL CHECK 同一词表。本轮**不新增**原因码：新增就要重新声明 CHECK 并推部署顺序硬约束。
_TERMINATION_BY_CODE = {
    "missing_parameters": "missing_parameters",
    "invalid_parameters": "invalid_parameters",
    "forbidden": "forbidden",
    "coverage_incomplete": "coverage_incomplete",
    "data_as_of_unknown": "data_as_of_unknown",
    "source_quality_failed": "source_quality_failed",
    "source_not_onboarded": "source_not_onboarded",
    "coverage_time_basis_unverified": "coverage_time_basis_unverified",
    "capability_unavailable": "capability_unavailable",
    "basis_incompatible": "invalid_parameters",
    "revenue_not_attributed": "revenue_not_attributed",
    "result_too_large": "result_too_large",
    "comparison_coverage_incomplete": "comparison_coverage_incomplete",
    "deadline_exceeded": "deadline_exceeded",
    "query_timeout": "query_timeout",
    "artifact_persistence_failed": "persistence_failed",
    "result_contract_violation": "contract_violation",
    "unavailable": "upstream_unavailable",
    "empty_scope": "missing_parameters",
    "product_not_resolved": "missing_parameters",
    # 商品歧义只是缺一个确定选择器：与“能力尚未开通”是两种不同的建议。
    "ambiguous_product": "missing_parameters",
    "commerce_scope_excluded": "capability_unavailable",
    "shop_not_synced": "missing_parameters",
}


def _termination_reason(runtime: CommerceRuntime, status: RunStatus) -> str:
    if status is RunStatus.SUCCEEDED:
        return "succeeded"
    for code in _limitation_codes(runtime.limitations):
        if code in _TERMINATION_BY_CODE:
            return _TERMINATION_BY_CODE[code]
    error = runtime.state.error
    if error is not None:
        code = str(getattr(error.code, "value", error.code))
        return _TERMINATION_BY_CODE.get(code, "contract_violation")
    return "recovery_exhausted"


# `kind` 决定状态四元组（RunStatus / DomainStatus / 状态机 tool_status / 报告 status）：
# 四者必须一起换。`tool_status` 沿用固定指标的词汇表（`runtime.models._TOOL_STATUSES`），
# 报告状态才是契约 v2 给展示层看的形状：`needs_input` 在两者里是两个词，不能合并。
_OUTCOMES = {
    "needs_input": (RunStatus.NEEDS_INPUT, DomainStatus.NEEDS_INPUT,
                    "invalid_parameters", "needs_input"),
    "missing_data": (RunStatus.MISSING_DATA, DomainStatus.MISSING_DATA,
                     "missing_data", "missing_data"),
    "forbidden": (RunStatus.FAILED, DomainStatus.FAILED, "forbidden", "forbidden"),
    "unavailable": (RunStatus.FAILED, DomainStatus.FAILED, "unavailable", "unavailable"),
}

# 归因码 → ErrorEnvelope：`ErrorCode` 与 009 的 `error_code` 同一张固定码表，本轮不扩列。
# 细粒度归因（缺覆盖 / 缺能力 / 时间口径 / 成本覆盖）留在限制码与终止原因里，
# 机器读得到，但不靠发明新错误码来表达。
_ERROR_BY_FINDING: dict[str, tuple[str, RecoveryAction]] = {
    "missing_parameters": ("missing_parameters", RecoveryAction.CORRECT_PARAMETERS),
    "invalid_parameters": ("invalid_parameters", RecoveryAction.CORRECT_PARAMETERS),
    "forbidden": ("forbidden", RecoveryAction.NONE),
    "deadline_exceeded": ("deadline_exceeded", RecoveryAction.RETRY_LATER),
    "query_timeout": ("deadline_exceeded", RecoveryAction.RETRY_LATER),
    "result_too_large": ("unavailable", RecoveryAction.NONE),
    "source_quality_failed": ("unavailable", RecoveryAction.RETRY_LATER),
    "unavailable": ("unavailable", RecoveryAction.RETRY_LATER),
    "artifact_persistence_failed": ("artifact_persistence_failed",
                                    RecoveryAction.RETRY_LATER),
    "result_contract_violation": ("result_contract_violation", RecoveryAction.NONE),
}


def _stop(runtime: CommerceRuntime, *, kind: str, code: str, problems: Sequence[str],
          stage: CommerceNode, message: str,
          limitations: Sequence[str] = ()) -> None:
    """提前终止：状态、血缘与报告一起定下来，后面的节点不再执行。

    `code` 是**归因码**（与 `_TERMINATION_BY_CODE` 同一词表），不是错误码：
    `missing_data` 不发 ErrorEnvelope（与固定指标同一处置：缺数据不是故障），
    其余种类按固定码表换行一个 `ErrorCode`，恢复动作跟着错误码走。
    """
    run_status, domain_status, tool_status, report_status = _OUTCOMES[kind]
    for limitation in limitations:
        if limitation not in runtime.limitations:
            runtime.limitations.append(limitation)
    error: ErrorEnvelope | None = None
    if kind != "missing_data":
        envelope_code, recovery = _ERROR_BY_FINDING.get(
            code, ("unavailable", RecoveryAction.NONE))
        error = ErrorEnvelope(
            code=envelope_code, stage=stage.value,  # type: ignore[arg-type]
            retryable=recovery is RecoveryAction.RETRY_LATER,
            recovery=recovery, public_message=message,  # type: ignore[arg-type]
            problems=list(problems))  # type: ignore[arg-type]
    runtime.state = runtime.state.model_copy(update={
        "status": run_status, "target_status": domain_status,
        "tool_status": tool_status, "problems": list(problems),
        "limitations": _limitation_codes(runtime.limitations),
        "data_as_of": runtime.data_as_of,
        "normalized_request": _normalized_request(runtime),
        "error": error,
    })
    # 终止路径也要有请求身份：主层拿不到指纹就没办法判断“同一个问题重试过没有”。
    _ensure_lineage(runtime)
    runtime.report = _report(runtime, status=report_status, run_status=run_status)


def _money(value: Decimal | None) -> str:
    """金额文本：与固定指标同一渲染（原值去尾零、不舍入），分项与合计才对得上。"""
    return money_text(value) if value is not None else "0"


def _iso_pair(window: tuple[date, date]) -> tuple[str, str]:
    return (window[0].isoformat(), window[1].isoformat())


def _provenance_of(runtime: CommerceRuntime) -> QueryProvenance:
    """把图上已有的材料装成血缘：还没读到的部分留空，不编。"""
    resolution = runtime.resolution
    mapping = (resolution.mapping_version
               if resolution is not None and resolution.mapping_version else None)
    defaults = QueryProvenance()
    return QueryProvenance(
        template_id=TEMPLATE_ID, template_version=TEMPLATE_VERSION,
        metric_version=COMMERCE_METRIC_VERSION, graph_version=COMMERCE_GRAPH_VERSION,
        # 目录版本只取已冻结在图上的值：提前终止时不补一次 SQL，那已超过预算。
        catalog_version=runtime.catalog_version or 0,
        mapping_version=mapping or defaults.mapping_version,
        source_batches=runtime.source_batches, data_as_of=runtime.data_as_of,
        basis_signature=basis_signature_of(runtime.basis), quality_rule=QUALITY_RULE)


def _identity_of(runtime: CommerceRuntime) -> RequestIdentity:
    return RequestIdentity(
        root_request_id=runtime.context.root_request_id,
        request_fingerprint=request_fingerprint(
            subject_id=runtime.context.subject_id,
            allowed_shop_ids=runtime.context.allowed_shop_ids,
            normalized_request=dict(runtime.state.normalized_request),
            provenance=runtime.provenance or _provenance_of(runtime)),
        attempt_no=runtime.context.attempt_no)


def _ensure_lineage(runtime: CommerceRuntime) -> None:
    """提前终止也要有血缘与请求身份：没有指纹，恢复层只能去猜聊天文本。"""
    if runtime.provenance is None:
        runtime.provenance = _provenance_of(runtime)
    if runtime.identity is None:
        runtime.identity = _identity_of(runtime)


def _resolved_product(runtime: CommerceRuntime) -> dict[str, Any]:
    if runtime.erp_product_id is None:
        return {}
    resolved: dict[str, Any] = {"product_ref": ref_for_key("product",
                                                           runtime.erp_product_id)}
    skus = sorted({ref_for_key("sku", sku) for line in runtime.lines
                   for sku in line.sku_ids})
    resolution = runtime.resolution
    if resolution is not None:
        skus = sorted(set(skus) | set(resolution.sku_refs))
        if resolution.mapping_version:
            resolved["mapping_version"] = resolution.mapping_version
    if skus:
        resolved["sku_refs"] = skus
    version = _catalog_version_of(runtime)
    if version is not None:
        resolved["catalog_version"] = version
    return resolved


def _report(runtime: CommerceRuntime, *, status: str,
            run_status: RunStatus | None = None) -> CommerceReport:
    """把图上的当前结论装成一次报告输出。

    `run_status` 缺当前状态：还在跑的图只能拿“已成功”去算终止原因，提前终止
    路径则传入真正的终态。
    """
    resolved = run_status or (runtime.state.status
                              if runtime.state.status is not RunStatus.RUNNING
                              else RunStatus.SUCCEEDED)
    return CommerceReport(
        status=status,
        datasets=tuple(_datasets(runtime)),
        requested_scope=_requested_scope(runtime),
        evaluated_shop_ids=runtime.candidate_shop_ids,
        excluded_scope=tuple(runtime.excluded),
        metric_statuses=tuple(runtime.statuses),
        resolved_product=_resolved_product(runtime),
        comparison=runtime.comparison,
        opportunity=runtime.opportunity,
        trend_window=(_iso_pair(runtime.trend_window) if runtime.trend_rows else None),
        termination_reason=_termination_reason(runtime, resolved),
        limitations=tuple(runtime.limitations),
        candidates=runtime.candidates,
    )


# ---------------------------------------------------------------------------
# 节点 1：授权范围
# ---------------------------------------------------------------------------


def resolve_scope(runtime: CommerceRuntime) -> None:
    """展开获准店铺并按平台过滤；显式越权引用直接 forbidden，不静默剔除。"""
    context = runtime.context
    request = runtime.request
    assert request is not None
    try:
        profiles = repository.shop_profiles(context.conn,
                                            sorted(context.allowed_shop_ids),
                                            deadline=context.deadline)
    except _BudgetExhausted:
        _stop(runtime, kind="unavailable", code="deadline_exceeded", problems=[],
              stage=CommerceNode.RESOLVE_SCOPE,
              message="本次查询时间预算已耗尽，请缩小日期或店铺范围后重试。",
              limitations=["本次查询时间预算已耗尽"])
        return
    runtime.profiles = {profile.shop_id: profile for profile in profiles}
    if request.scope.mode == "selected" and request.scope.shop_refs:
        wanted: list[str] = []
        for ref in request.scope.shop_refs:
            shop_id = context.ref_to_shop_id.get(ref)
            if shop_id is None or shop_id not in context.allowed_shop_ids:
                # 明确点名的引用不在授权范围内：整次请求拒绝，不删掉这家店继续算。
                _stop(runtime, kind="forbidden", code="forbidden",
                      problems=["forbidden"], stage=CommerceNode.RESOLVE_SCOPE,
                      message="查询范围无权限。", limitations=["店铺不在授权范围"])
                return
            wanted.append(shop_id)
    else:
        # all_authorized 的全集是"获准的店铺"，不以有成交记录的店铺为准。
        wanted = sorted(context.allowed_shop_ids)
    if request.scope.platforms:
        platforms = set(request.scope.platforms)
        wanted = [shop for shop in wanted
                  if runtime.profiles.get(shop) is not None
                  and runtime.profiles[shop].platform in platforms]
    runtime.candidate_shop_ids = tuple(sorted(set(wanted)))

    missing = [shop for shop in runtime.candidate_shop_ids if shop not in runtime.profiles]
    for shop in missing:
        _exclude(runtime, shop, "shop_not_synced")
    if missing:
        runtime.limitations.append("店铺尚未同步，无法查询")
    disabled = [shop for shop in runtime.candidate_shop_ids
                if not runtime.profiles[shop].enabled]
    for shop in disabled:
        _exclude(runtime, shop, "shop_disabled")
    if disabled:
        runtime.limitations.append("部分店铺已停用，仅返回剩余范围")
    # 未登记平台没有来源，换日期换指标都救不了：沿用固定指标同一句披露与同一归因。
    unregistered = [shop for shop in runtime.candidate_shop_ids
                    if unsupported_reason(_record_of(runtime, shop), "paid_amount")
                    == "source_unregistered"]
    for shop in unregistered:
        _exclude(runtime, shop, "source_unregistered")
    if unregistered:
        runtime.limitations.extend(_capability_gap(
            [_record_of(runtime, shop) for shop in unregistered], ["paid_amount"]))
    runtime.state = runtime.state.model_copy(
        update={"normalized_request": _normalized_request(runtime)})
    if not runtime.candidate_shop_ids:
        _stop(runtime, kind="missing_data", code="missing_parameters", problems=[],
              stage=CommerceNode.RESOLVE_SCOPE,
              message="所查时间段的数据覆盖不足，可按建议窗口查询或等待回填完成。",
              limitations=["授权范围内没有可分析的店铺"])


# ---------------------------------------------------------------------------
# 节点 2：商品解析（复用 Task 6 解析器，不长第二份）
# ---------------------------------------------------------------------------


def resolve_product_if_needed(runtime: CommerceRuntime) -> None:
    """把 ref 或文本换成一个 ERP 商品；歧义交回澄清，零候选不是零销量。"""
    request = runtime.request
    assert request is not None
    selector = (Selector(ref=request.product.ref) if request.product.ref
                else Selector(text=(request.product.text or "").strip()))
    # 解析按**授权全集**查，不按本轮范围查：范围外的同名商品既不该造成歧义，
    # 也不该因为"这轮没选它"就被说成不存在。
    resolution = resolve_product(runtime.context.conn, selector=selector,
                                 authorized_shop_ids=sorted(
                                     runtime.context.allowed_shop_ids),
                                 at=runtime.context.now.date())
    runtime.resolution = resolution
    if resolution.status == "resolved" and resolution.erp_product_id:
        runtime.erp_product_id = str(resolution.erp_product_id)
        runtime.state = runtime.state.model_copy(
            update={"normalized_request": _normalized_request(runtime)})
        return
    if resolution.status == "ambiguous":
        # 候选已按**授权全集**查出来：能进这里的就是本轮有权看的，不需要再筛一道。
        runtime.candidates = tuple(
            {"ref": candidate.product_ref} for candidate
            in resolution.candidates[:MAX_CANDIDATE_CARDS])
        _stop(runtime, kind="needs_input", code="missing_parameters",
              problems=["missing_parameters"], stage=CommerceNode.RESOLVE_PRODUCT,
              message="缺少查询参数，请补充后重试。",
              limitations=[f"{len(resolution.candidates)} 个候选商品命中同一文本，"
                           "请改用商品引用后重试"])
        return
    _stop(runtime, kind="missing_data", code="product_not_resolved", problems=[],
          stage=CommerceNode.RESOLVE_PRODUCT,
          message="所查时间段的数据覆盖不足，可按建议窗口查询或等待回填完成。",
          limitations=["商品未解析出来，不能当成销量为 0"])


# ---------------------------------------------------------------------------
# 节点 3：口径
# ---------------------------------------------------------------------------


def resolve_metric_basis(runtime: CommerceRuntime) -> None:
    """确定要读哪些能力标签，并给每个 (店铺, 报告指标) 生成口径凭证。

    口径只能由注册表推导：模型不许指定口径，也不许把报告指标名当能力标签用。
    `sales_basis=verified_payment` 换的是事实集合（商业支付），商品父行面在那个口径下没有
    对应事实，解析为空并标成 incomparable —— 不拿支付额冒充商品销售额。
    """
    request = runtime.request
    assert request is not None
    runtime.trend_window = request.trend_window
    runtime.previous_window = request.previous_window
    tags: set[str] = set()
    for metric in request.metrics:
        tags.update(COMMERCE_METRIC_CAPABILITIES.get(str(metric), ()))
    if request.sales_basis == "verified_payment":
        tags.update({"paid_amount", "paid_orders"})
    runtime.tags = tuple(sorted(tags))
    for shop in runtime.candidate_shop_ids:
        record = _record_of(runtime, shop)
        for metric in request.metrics:
            bindings = tuple(_dependencies(record, str(metric), request.sales_basis))
            runtime.bindings[(shop, str(metric))] = bindings
            if not bindings:
                continue
            # 凭证按报告指标名给出：模型问的是 `sales_amount`，就该看见它自己的口径；
            # 底层能力标签仍然只由注册表决定值。
            runtime.basis.append({
                "shop_id": shop, "metric": str(metric),
                "source": bindings[0].source, "basis": bindings[0].basis,
                "time_basis": bindings[0].time_basis,
                "metric_version": COMMERCE_METRIC_VERSION,
            })


def _dependencies(record: ShopRecord, metric: str, sales_basis: str) -> tuple:
    """报告指标在这家店的取数依赖；解析为空就是"这个来源答不了这个口径"。"""
    if (sales_basis == "verified_payment"
            and METRIC_FACET.get(metric) == "product_reference"):
        # 已验证支付事实停在商业单粒度，没有商品级归属；硬接过去只会把支付额说成商品额。
        return ()
    return tuple(binding
                 for tag in COMMERCE_METRIC_CAPABILITIES.get(metric, ())
                 for binding in resolve_metric_dependencies(record, tag))


# ---------------------------------------------------------------------------
# 节点 4：能力 / 时间口径 / 覆盖 门禁
# ---------------------------------------------------------------------------


def _assessment(runtime: CommerceRuntime, tag: str,
                window: tuple[date, date]) -> CoverageAssessment | None:
    """一次覆盖判定，按 (标签, 有该标签依赖的店铺集合, 窗口) 记忆。

    只把"这个来源能回答该口径"的店铺送进去：一家平台拿不到该口径的店铺会让整段窗口
    被判成未知（`assess_query_coverage` 对无依赖店铺就是 fail closed 的），但这个后果
    不该扩到本来可答的店铺身上——那等于让一个缺口拖垮整份报告。
    """
    shops = tuple(sorted(shop for shop in runtime.candidate_shop_ids
                         if resolve_metric_dependencies(_record_of(runtime, shop), tag)))
    if not shops:
        return None
    key = (tag, shops, window)
    cached = runtime.assessments.get(key)
    if cached is None:
        probe = QueryRequest(start=window[0], end=window[1], shop_ids=list(shops),
                             metrics=[tag], group_by=_TAG_GROUP_BY.get(tag, "total"),
                             compare="none", currency="CNY")
        cached = assess_query_coverage(runtime.context.conn, probe)
        runtime.assessments[key] = cached
    return cached


def _gap_windows(assessment: CoverageAssessment, shop_id: str,
                 window: tuple[date, date]) -> list[tuple[date, date]]:
    """这一店在这一窗口内的未覆盖段（只算真实实体缺口）。"""
    out: list[tuple[date, date]] = []
    for gap in assessment.gaps:
        if gap.shop_id != shop_id or gap.entity not in _REAL_ENTITIES:
            continue
        start, end = max(gap.start, window[0]), min(gap.end, window[1])
        if start < end:
            out.append((start, end))
    return out


def check_capabilities_and_coverage(runtime: CommerceRuntime) -> None:
    """逐店逐指标判能力、时间口径与覆盖；三类原因各说各的，不互相冒充。"""
    request = runtime.request
    assert request is not None
    main = (request.start, request.end)

    # 1) 能力（不查库）：来源没登记、标签没授予、该口径拿不到，分开归因。
    #    一家店答不上报告的主面就整店退出：商品报告里混一家“只有单据面”的店，
    #    会被读成“这家店这个商品没卖动”，那是错话；它的缺口在 excluded_scope 里逐项可查。
    capability_reason = _capability_blockers(runtime)
    for shop, reasons in sorted(capability_reason.items()):
        if _decisive_metrics(request) <= set(reasons):
            _exclude(runtime, shop, _dominant_reason(reasons.values()))
    if not runtime.candidate_shop_ids:
        _stop(runtime, kind="missing_data", code="capability_unavailable", problems=[],
              stage=CommerceNode.CHECK_CAPABILITIES_AND_COVERAGE,
              message="本次查询的指标能力尚未开通，换成已开通的指标或先完成来源核验后再查。",
              limitations=_capability_gap(
                  [_record_of(runtime, shop) for shop in sorted(runtime.excluded_ids)
                   if shop in runtime.profiles], list(runtime.tags))
              or [_capability_gap_text(request)])
        return

    # 2) 主窗口覆盖：一家店有缺口就整店退出合计，不拿已覆盖段冒充全量汇总。
    metrics_by_tag: dict[str, set[str]] = {}
    for metric in request.metrics:
        for tag in COMMERCE_METRIC_CAPABILITIES.get(str(metric), ()):
            metrics_by_tag.setdefault(tag, set()).add(str(metric))
    if request.sales_basis == "verified_payment":
        # 支付面自己也要过覆盖门禁：它不是“默认完整”，只是换了事实集合。
        for tag in ("paid_amount", "paid_orders"):
            metrics_by_tag.setdefault(tag, set()).update(
                str(metric) for metric in request.metrics
                if METRIC_FACET.get(str(metric)) != "product_reference")
    coverage_blockers: dict[str, dict[str, str]] = {}
    runtime.blocking, runtime.disclosure = {}, {}
    qualities: list[str] = []
    for tag, affected in sorted(metrics_by_tag.items()):
        assessment = _assessment(runtime, tag, main)
        if assessment is None:
            continue
        runtime.blocking[tag] = frozenset(assessment.time_basis_blocking)
        runtime.disclosure[tag] = frozenset(assessment.time_basis_disclosure)
        # 质量是三态：failed 拒答，unknown 出数但披露，passed 才什么都不加。
        # 多指标多标签时取最差那个：一个来源核验不过就是整体不过。
        qualities.append(assessment.quality_status)
        for shop in runtime.candidate_shop_ids:
            if shop in assessment.time_basis_blocking:
                for metric in affected:
                    coverage_blockers.setdefault(shop, {})[metric] = \
                        "coverage_time_basis_unverified"
                continue
            windows = _gap_windows(assessment, shop, main)
            if windows:
                runtime.main_gaps.setdefault(shop, []).extend(windows)
                for metric in affected:
                    coverage_blockers.setdefault(shop, {}).setdefault(
                        metric, "coverage_incomplete")

    runtime.quality_status = _worst_quality(qualities)
    if runtime.quality_status == "failed":
        _stop(runtime, kind="unavailable", code="source_quality_failed", problems=[],
              stage=CommerceNode.CHECK_CAPABILITIES_AND_COVERAGE,
              message="来源质量核验未通过，暂时不能出数。",
              limitations=["来源质量核验未通过，拒绝出数"])
        return

    # 3) 共同截止：任一依赖没推进 data_as_of，整体截止就是未知。
    cutoffs = [item.data_as_of for item in runtime.assessments.values()]
    if not cutoffs or any(item is None for item in cutoffs):
        _stop(runtime, kind="missing_data", code="data_as_of_unknown", problems=[],
              stage=CommerceNode.CHECK_CAPABILITIES_AND_COVERAGE,
              message="所查时间段的数据覆盖不足，可按建议窗口查询或等待回填完成。",
              limitations=["数据截止未知（回填未完成）",
                           "覆盖未完成，拒绝部分汇总；缺口见coverage.gaps"])
        return
    runtime.data_as_of = min(item for item in cutoffs if item is not None)
    runtime.source_batches = tuple(sorted({batch
                                           for item in runtime.assessments.values()
                                           for batch in item.source_batches}))
    if runtime.quality_status != "passed":
        runtime.limitations.append("来源质量未核验（尚无对账记录）")

    # 4) 未认证付款时间口径：实测不成立的店不出数，没逐店对照过的出数但披露为样本。
    blocking = {shop for shops in runtime.blocking.values() for shop in shops}
    for shop in sorted(blocking):
        _exclude(runtime, shop, "coverage_time_basis_unverified")
    if blocking:
        runtime.limitations.append(
            f"{len(blocking)} 家店铺的付款时间口径未经认证，未执行金额查询")
    disclosed = ({shop for shops in runtime.disclosure.values() for shop in shops}
                 - blocking) & set(runtime.candidate_shop_ids)
    if disclosed:
        runtime.limitations.append(
            f"{len(disclosed)} 家店铺的结果来自未认证付款时间口径，按可观测样本披露")

    # 5) 覆盖缺口店铺退出。
    for shop in sorted(runtime.main_gaps):
        if shop in runtime.candidate_shop_ids:
            _exclude(runtime, shop, "coverage_incomplete", runtime.main_gaps[shop])
    if not runtime.candidate_shop_ids:
        _stop(runtime, kind="missing_data", code="coverage_incomplete", problems=[],
              stage=CommerceNode.CHECK_CAPABILITIES_AND_COVERAGE,
              message="所查时间段的数据覆盖不足，可按建议窗口查询或等待回填完成。",
              limitations=["覆盖未完成，拒绝部分汇总；缺口见coverage.gaps"])
        return

    blockers = {shop: {**capability_reason.get(shop, {}),
                       **coverage_blockers.get(shop, {})}
                for shop in set(capability_reason) | set(coverage_blockers)}
    _record_statuses(runtime, blockers)


def _capability_gap_text(request: ProductPerformanceRequest) -> str:
    return (f"{len(request.metrics)} 个指标在本次范围内均不可回答，"
            "未执行金额查询")


def _worst_quality(qualities: Sequence[str]) -> str:
    """多个标签的质量判定取最差：failed > unknown > passed。

    默认不能是 unknown：把“已经对过账”的报告说成“尚无对账记录”，与把未核验的说成
    已核验同样错。
    """
    if "failed" in qualities:
        return "failed"
    if not qualities or "unknown" in qualities:
        return "unknown"
    return "passed"


def _decisive_metrics(request: ProductPerformanceRequest) -> frozenset[str]:
    """哪些指标答不上就会把一家店逐出本报告：报告的主面。

    商品运营图的主面是商品父行面（spec §5.2）；只请了单据参考指标时才是单据面。
    不能要求“所有指标同时可算”才留一家店：那会把能答销量的店错杀。
    """
    product = frozenset(str(metric) for metric in request.metrics
                        if METRIC_FACET.get(str(metric)) == "product_reference")
    return product or frozenset(str(metric) for metric in request.metrics)


def _capability_blockers(runtime: CommerceRuntime) -> dict[str, dict[str, str]]:
    request = runtime.request
    assert request is not None
    out: dict[str, dict[str, str]] = {}
    for shop in runtime.candidate_shop_ids:
        record = _record_of(runtime, shop)
        reasons: dict[str, str] = {}
        for metric in request.metrics:
            for tag in COMMERCE_METRIC_CAPABILITIES.get(str(metric), ()):
                reason = unsupported_reason(record, tag)
                if reason is None:
                    continue
                reasons[str(metric)] = _attributed_reason(record, tag, reason)
        # `verified_payment` 下面向商品的指标不是"能力未开通"，而是"这个口径没有这个
        # 事实"：归因留给 _record_statuses，别把口径问题说成接入问题。
        if request.sales_basis == "verified_payment":
            for metric in request.metrics:
                if METRIC_FACET.get(str(metric)) == "product_reference":
                    reasons.pop(str(metric), None)
        if reasons:
            out[shop] = reasons
    return out


def _attributed_reason(record: ShopRecord, tag: str, reason: str) -> str:
    """把注册表的“能力拿不到”换成真正的原因。

    拼多多是个已登记但只有出库通道的平台：注册表把支付族指标一律解成空，
    `unsupported_reason` 因此报 capability_unavailable。但真正拦住它的不是“没开通”，
    而是那条通道的付款时间口径实测不成立（spec §5.3、sources 三态认证）：
    说成“去开通能力”会把人引向一条永远不会完成的对账路。其余额外保持原归因；
    “平台未登记”与“标签未授予”换个说法也救不了，不能混进这一类。
    """
    if reason != "capability_unavailable" or tag not in PAYMENT_WINDOW_METRICS:
        return reason
    registered = registration(record.platform)
    if (registered is not None and registered.order_time_basis != "pay_time"
            and registered.time_certified == "disproved"):
        return "coverage_time_basis_unverified"
    return reason


def _dominant_reason(reasons: Sequence[str]) -> str:
    """一家店多个指标都答不了时取一个稳定归因：先说修不动的那个。

    来源没登记 > 时间口径实测不成立 > 能力未开通 > 标签未授予：前三者“换个时间范围”
    都救不了，最后一句至少还能指到对账开通那条路。
    """
    values = set(reasons)
    for reason in ("source_unregistered", "coverage_time_basis_unverified",
                   "capability_unavailable", "capability_ungranted"):
        if reason in values:
            return reason
    return sorted(values)[0] if values else "capability_unavailable"


def _record_statuses(runtime: CommerceRuntime,
                     blockers: Mapping[str, Mapping[str, str]]) -> None:
    """逐店逐指标状态 + 跨指标口径签名：同名不同口径不许并入同一个合计。"""
    request = runtime.request
    assert request is not None
    signatures: dict[str, set[tuple]] = {}
    for shop in runtime.candidate_shop_ids:
        for metric in request.metrics:
            bindings = runtime.bindings.get((shop, str(metric)), ())
            if bindings:
                signatures.setdefault(str(metric), set()).add(
                    binding_signature(bindings))
    incomparable = {metric for metric, items in signatures.items() if len(items) > 1}
    runtime.incomparable_metrics = frozenset(incomparable)
    if incomparable:
        runtime.limitations.append(
            f"这些指标在本次范围内口径互不兼容：{'、'.join(sorted(incomparable))}；"
            "请按店铺分列后逐组查看，不能汇总或比较")
    basis_only = request.sales_basis == "verified_payment"
    for shop in sorted(runtime.candidate_shop_ids):
        for metric in request.metrics:
            reason: str | None = blockers.get(shop, {}).get(str(metric))
            if (reason is None and basis_only
                    and METRIC_FACET.get(str(metric)) == "product_reference"):
                reason = "payment_product_attribution_unavailable"
            if reason is None and str(metric) in incomparable:
                reason = "basis_incompatible"
            entry: dict[str, Any] = {"shop_ref": _ref_of(runtime, shop),
                                     "metric": str(metric)}
            if reason is None:
                entry["status"] = "available"
            elif reason in {"basis_incompatible",
                            "payment_product_attribution_unavailable"}:
                entry.update(status="incomparable", reason=reason)
            elif reason.startswith("capability") or reason == "source_unregistered":
                entry.update(status="unsupported", reason=reason)
            else:
                entry.update(status="missing", reason=reason)
            runtime.statuses.append(entry)
    if basis_only and any(METRIC_FACET.get(str(metric)) == "product_reference"
                          for metric in request.metrics):
        runtime.limitations.append(
            "已验证支付口径没有商品级事实，商品销量与金额不能按该口径给出")
    if runtime.excluded:
        runtime.limitations.append(
            f"{len(runtime.excluded)} 家获准店铺未列入本次合计，"
            "原因见 excluded_scope")


# ---------------------------------------------------------------------------
# 节点 5：版本与血缘
# ---------------------------------------------------------------------------

_CATALOG_VERSION: dict[int, int] = {}


def freeze_versions(runtime: CommerceRuntime) -> None:
    """把"哪一版口径、哪一批数据、哪一版目录与映射"冻进状态与血缘。

    这一节点之后图上的版本就不再变；只读快照与本节点同一时刻开始，所以“冻结的
    批次”与“读到的行”是同一个数据库版本。
    """
    request = runtime.request
    assert request is not None
    runtime.state = runtime.state.model_copy(update={
        "normalized_request": _normalized_request(runtime),
        "coverage": Coverage(status="complete", start=request.start, end=request.end,
                             gaps=[]),
        "data_as_of": runtime.data_as_of,
    })
    # 目录版本要进血缘，所以在这里读一次并留在图上；后续节点复用同一个值。
    _catalog_version_of(runtime)
    runtime.provenance = _provenance_of(runtime)
    runtime.identity = _identity_of(runtime)


def _catalog_version_of(runtime: CommerceRuntime) -> int | None:
    """读一次目录版本并留在图上：后续节点复用同一个值，不各读各的。"""
    if runtime.catalog_version is not None:
        return runtime.catalog_version
    row = runtime.context.conn.execute(
        "SELECT version FROM reporting.v_catalog_version").fetchone()
    runtime.catalog_version = int(row[0]) if row is not None else 0
    return runtime.catalog_version


# ---------------------------------------------------------------------------
# 节点 6：固定模板成形
# ---------------------------------------------------------------------------


def plan_fixed_queries(runtime: CommerceRuntime) -> None:
    """把本轮真正要跑的固定模板与读窗口定下来。

    读窗口取「主期间 ∪ 七日趋势 ∪ 上期比较」的并集：合计、七日序列与上期数值来自
    **同一条集合查询、同一个数据库快照**，不会一半新一半旧。
    """
    request = runtime.request
    assert request is not None
    starts = [request.start, runtime.trend_window[0]]
    ends = [request.end, runtime.trend_window[1]]
    if runtime.previous_window is not None:
        starts.append(runtime.previous_window[0])
        ends.append(runtime.previous_window[1])
    document_tags = {tag for metric in request.metrics
                     if METRIC_FACET.get(str(metric)) == "erp_document"
                     for tag in COMMERCE_METRIC_CAPABILITIES.get(str(metric), ())}
    runtime.plan = QueryPlan(
        read_window=(min(starts), max(ends)),
        # 商品父行面只在 ERP 有效销售父项口径下有事实；换到已验证支付口径就不许
        # 继续发布商品数字，否则同一份报告里会出现两套"销售额"。
        product_facet=(request.sales_basis == "erp_effective_parent"
                       and any(METRIC_FACET.get(str(metric)) == "product_reference"
                               for metric in request.metrics)),
        document_facet=bool(document_tags),
        payment_facet=request.sales_basis == "verified_payment")


# ---------------------------------------------------------------------------
# 节点 7：一次集合查询取回全部面
# ---------------------------------------------------------------------------


def execute_aggregates(runtime: CommerceRuntime) -> None:
    """读商品父行面、ERP 单据面与（需要时）商业支付面。"""
    request = runtime.request
    plan = runtime.plan
    assert request is not None and plan is not None
    context = runtime.context
    shops = list(runtime.candidate_shop_ids)
    if plan.product_facet:
        runtime.lines = tuple(repository.load_product_lines(
            context.conn, shop_ids=shops,
            erp_product_id=str(runtime.erp_product_id),
            start=plan.read_window[0], end=plan.read_window[1],
            deadline=context.deadline))
    if plan.document_facet:
        runtime.documents = repository.load_document_facts(
            context.conn, shop_ids=shops, start=request.start, end=request.end,
            deadline=context.deadline)
    if plan.payment_facet:
        window = _window_range(request.start, request.end)
        runtime.payments = repository.load_payment_facts(
            context.conn, shop_ids=shops, start_ts=window[0], end_ts=window[1],
            deadline=context.deadline)
    _attribution_disclosures(runtime)


def _attribution_disclosures(runtime: CommerceRuntime) -> None:
    """把"钱里有多少没进商品维度"与"有多少支付没拿到核验章"一起说清。

    这两条是 5.3 定下的**可量化限制**：披露而不是拒答，也不许让它们无声消失。
    """
    request = runtime.request
    plan = runtime.plan
    assert request is not None and plan is not None
    if not runtime.candidate_shop_ids:
        return
    window = _window_range(request.start, request.end)
    shops = list(runtime.candidate_shop_ids)
    if plan.product_facet or plan.document_facet:
        text = describe_attribution_gap(attribution_gap(
            runtime.context.conn, shop_ids=shops, start_ts=window[0], end_ts=window[1]))
        if text:
            runtime.limitations.append(text)
    if plan.payment_facet:
        gap = unverified_payments(runtime.context.conn, shop_ids=shops,
                                  start_ts=window[0], end_ts=window[1])
        if gap.material:
            runtime.limitations.append(
                f"未认证支付{gap.total}笔（金额未定{gap.amount_undetermined}笔），"
                f"已知原始金额{_money(gap.known_amount)}元（{gap.amount_known}笔）")
            runtime.diagnostics["unverified_payments"] = {
                "total": gap.total, "amount_undetermined": gap.amount_undetermined,
                "amount_known": gap.amount_known,
                "known_amount": _money(gap.known_amount)}


# ---------------------------------------------------------------------------
# 节点 8：指标算术
# ---------------------------------------------------------------------------


def _group_input(line: repository.ProductLine) -> dict[str, Any]:
    """一行 (店铺, 日, 行性质) 聚合 → 累加器入参。

    成本不完整、分摊未核验、成本语义不清（套件 / 组合 / 加工）三种情况都把
    `cost_total` 置 null：整体毛利就不许发布。`allocation_verified` 为 false 时连销售
    金额也不发布，只发件数——件数不依赖分摊结论。
    """
    cost_ready = (line.cost_line_count == line.line_count
                  and line.allocation_verified
                  and line.line_kind not in COST_SEMANTIC_UNVERIFIED_KINDS)
    return {"quantity": line.quantity,
            "sales_amount": line.sales_amount if line.allocation_verified else None,
            "cost_total": line.cost_total if cost_ready else None}


def _in(window: tuple[date, date], day: date) -> bool:
    return window[0] <= day < window[1]


def compute_metrics(runtime: CommerceRuntime) -> None:
    """按 (店铺, 行性质) 逐行与跨店合计分别聚合；行性质不混面，合计不越集合。

    三个面各自独立算：只请了单据参考指标时，商品面没行可算也必须把单据面发出去
    （“多指标能力独立判断”不能停在聚合节点上）。口径互不兼容的指标只留在各店自己的
    行上：合计行里给它一个数，就是把两个口径的答案说成一个集合的答案。
    """
    request = runtime.request
    plan = runtime.plan
    assert request is not None and plan is not None
    main = (request.start, request.end)
    lines = [line for line in runtime.lines if _in(main, line.day)] if plan.product_facet else []
    names = _name_columns(runtime)
    if plan.product_facet:
        for shop in sorted(runtime.candidate_shop_ids):
            for kind in sorted({line.line_kind for line in lines if line.shop_id == shop}):
                values = combine_reference_metrics(
                    [_group_input(line) for line in lines
                     if line.shop_id == shop and line.line_kind == kind])
                runtime.rows.append(project(PRODUCT_ROWS, {
                    "shop_id": shop, "product_id": str(runtime.erp_product_id),
                    "line_kind": kind, "sales_share": None, **values, **names}))
        for kind in sorted({line.line_kind for line in lines}):
            values = combine_reference_metrics(
                [_group_input(line) for line in lines if line.line_kind == kind])
            values["sales_share"] = None      # 份额在节点 10 里按已评估集合算
            for metric in runtime.incomparable_metrics:
                if metric in values:
                    values[metric] = None
            runtime.totals.append(project(PRODUCT_TOTAL_ROWS, {
                "product_id": str(runtime.erp_product_id), "line_kind": kind,
                **values, **names}))
        _cost_disclosures(runtime, lines)
    _document_rows(runtime)
    _payment_rows(runtime)


def _name_columns(runtime: CommerceRuntime) -> dict[str, Any]:
    """名称与规格列只喂目录投影：它们不在结果列白名单里，投影后不会到模型手上。"""
    candidate = (runtime.resolution.candidates[0]
                 if runtime.resolution is not None and runtime.resolution.candidates
                 else None)
    return {"product_name": getattr(candidate, "product_name", None),
            "product_name_snapshot": getattr(candidate, "product_name_snapshot", None),
            "sku_label": getattr(candidate, "sku_label", None)}


def _cost_disclosures(runtime: CommerceRuntime,
                      lines: Sequence[repository.ProductLine]) -> None:
    request = runtime.request
    assert request is not None
    profit_requested = any("gross" in str(metric) for metric in request.metrics)
    if not profit_requested:
        return
    runtime.limitations.append("商品毛利参考未扣售后、平台费、运费与广告费，不是净利润")
    costed = sum(line.cost_line_count for line in lines)
    total = sum(line.line_count for line in lines)
    if lines and costed < total:
        runtime.limitations.append(
            f"成本覆盖不全：{costed}/{total} 行有行成本，未发布完整商品毛利参考")
    if any(line.line_kind in COST_SEMANTIC_UNVERIFIED_KINDS for line in lines):
        runtime.limitations.append(
            "商品毛利参考不含套件/组合/加工父项：这些行的成本语义未核验")
    if any(not line.allocation_verified for line in lines):
        runtime.limitations.append("分摊金额未核验的销售父项只发布件数，不发布销售金额")


def _document_rows(runtime: CommerceRuntime) -> None:
    """ERP 单据毛利：每家店一行，覆盖不全的店毛利留 null，不拿有值子集冒充整体。"""
    plan = runtime.plan
    assert plan is not None
    if not plan.document_facet:
        return
    facts = [runtime.documents[shop] for shop in runtime.candidate_shop_ids
             if shop in runtime.documents]
    for item in facts:
        whole = item.documents_with_gross_profit == item.documents
        runtime.facet_rows.append(project(DOCUMENT_ROWS, {
            "shop_id": item.shop_id, "erp_documents": item.documents,
            "erp_documents_with_gross_profit": item.documents_with_gross_profit,
            "erp_gross_profit_reference": (
                money_of(item.gross_profit)
                if whole and item.gross_profit is not None else None)}))
    documents = sum(item.documents for item in facts)
    with_profit = sum(item.documents_with_gross_profit for item in facts)
    runtime.diagnostics["erp_document_coverage"] = {
        "documents": documents,
        "documents_with_gross_profit": with_profit,
        "publishable": "true" if facts and with_profit == documents else "false"}
    if facts and with_profit < documents:
        runtime.limitations.append(
            f"ERP 单据毛利覆盖不全：{with_profit}/{documents} 张单据带毛利字段，"
            "未发布单据毛利参考")
    split = sum(item.split_documents for item in facts)
    merged = sum(item.merged_documents for item in facts)
    if split or merged:
        runtime.limitations.append(
            f"ERP 单据集合含 {split} 张拆单子单与 {merged} 张合单，与平台订单不是一对一")


def _payment_rows(runtime: CommerceRuntime) -> None:
    """已验证支付面：单独一型行，不与单据毛利同行，也不与商品面相加。"""
    plan = runtime.plan
    assert plan is not None
    if not plan.payment_facet:
        return
    for shop in sorted(runtime.payments):
        facts = runtime.payments[shop]
        runtime.facet_rows.append(project(PAYMENT_ROWS, {
            "shop_id": shop, "paid_amount": money_of(facts.paid_amount),
            "paid_orders": facts.paid_orders}))


# ---------------------------------------------------------------------------
# 节点 9：七日趋势与上期比较
# ---------------------------------------------------------------------------


def build_comparison_and_trend(runtime: CommerceRuntime) -> None:
    plan = runtime.plan
    request = runtime.request
    assert plan is not None and request is not None
    if plan.product_facet:
        _build_trend(runtime)
    if request.comparison == "previous_period":
        _build_comparison(runtime)


def _window_gaps(runtime: CommerceRuntime,
                 window: tuple[date, date]) -> dict[str, list[tuple[date, date]]]:
    """一个窗口内逐店未覆盖段：趋势与上期各自单独检查，缺口不挪到另一组七天。"""
    request = runtime.request
    out: dict[str, list[tuple[date, date]]] = {}
    if request is None:
        return out
    tags = sorted({tag for metric in request.metrics
                   for tag in COMMERCE_METRIC_CAPABILITIES.get(str(metric), ())})
    for tag in tags:
        assessment = _assessment(runtime, tag, window)
        if assessment is None:
            continue
        for shop in runtime.candidate_shop_ids:
            windows = _gap_windows(assessment, shop, window)
            if windows:
                out.setdefault(shop, []).extend(windows)
    return out


def _is_covered(shop: str, day: date,
                gaps: Mapping[str, list[tuple[date, date]]]) -> bool:
    return not any(start <= day < end for start, end in gaps.get(shop, ()))


def _days(window: tuple[date, date]) -> list[date]:
    out: list[date] = []
    day = window[0]
    while day < window[1]:
        out.append(day)
        day += timedelta(days=1)
    return out


def _build_trend(runtime: CommerceRuntime) -> None:
    """七日序列逐日发布：覆盖内的缺交易日补真实 0，覆盖外留 null。

    两者必须分得开：把缺失日当 0 会让"没取到数"看着像"那天没卖"。
    """
    window = runtime.trend_window
    gaps = _window_gaps(runtime, window)
    uncovered: set[str] = set()
    zeros = 0
    for shop in sorted(runtime.candidate_shop_ids):
        for day in _days(window):
            lines = [line for line in runtime.lines
                     if line.shop_id == shop and line.day == day]
            covered = _is_covered(shop, day, gaps)
            if not covered:
                uncovered.add(shop)
            elif not lines:
                zeros += 1
            values = (combine_reference_metrics([_group_input(line) for line in lines])
                      if covered and lines else None)
            quantity = values["sold_quantity"] if values else ("0" if covered else None)
            amount = values["sales_amount"] if values else ("0" if covered else None)
            runtime.trend_rows.append(project(TREND_ROWS, {
                "shop_id": shop, "product_id": str(runtime.erp_product_id),
                "day": day.isoformat(), "sold_quantity": quantity,
                "sales_amount": amount}))
    if uncovered:
        runtime.limitations.append("趋势窗口覆盖不足，缺失日按 null 单独留 gap，"
                                   "不滑到另一组七天")
    if zeros:
        runtime.limitations.append(f"七日趋势含 {zeros} 天真实零成交，与缺失日分列")


def _build_comparison(runtime: CommerceRuntime) -> None:
    """上期比较：先确认两期同源且两期覆盖都完整，才给差额与增长率。"""
    request = runtime.request
    assert request is not None
    previous = runtime.previous_window
    assert previous is not None
    rows: list[dict[str, Any]] = []
    comparable = True
    reason: str | None = None
    previous_range = _window_range(previous[0], previous[1])
    bindings = [binding for values in runtime.bindings.values() for binding in values]
    switched = switched_sources_between(
        runtime.context.conn, shop_ids=list(runtime.candidate_shop_ids),
        entities=sorted({binding.entity for binding in bindings}),
        current={(binding.shop_id, binding.entity, binding.source)
                 for binding in bindings},
        start_ts=previous_range[0], end_ts=previous_range[1])
    if switched:
        comparable, reason = False, "comparison_coverage_incomplete"
        runtime.limitations.append(
            f"{len(switched)} 家店铺的上期与本期数据来源不同，不能按增长比较")
    if _window_gaps(runtime, previous) or _window_gaps(runtime, (request.start,
                                                                 request.end)):
        comparable, reason = False, reason or "coverage_incomplete"
    for shop in sorted(runtime.candidate_shop_ids):
        for metric in request.metrics:
            current = _current_value(runtime, shop, str(metric))
            entry: dict[str, Any] = {"shop_ref": _ref_of(runtime, shop),
                                     "metric": str(metric), "current": current}
            if comparable:
                before = _previous_value(runtime, shop, str(metric))
                entry["previous"] = before
                now_value, was_value = to_decimal(current), to_decimal(before)
                entry["change"] = (money_of(now_value - was_value)
                                   if now_value is not None and was_value is not None
                                   else None)
                # 增长率只在**基期为正**时发布：与 `commerce.metrics._ratio` 同一规则。
                # 基期可以是真实的负数（商品毛利参考亏损、上期负毛利），-50 → +60 的
                # 真实变化是“转亏为盈 +110”；除以负基期会得到 -2.2，把一次上涨说成
                # 跌 220%——符号被基期拧反，不是“增长很小”。基期为 0 时比率无定义。
                # 差额照发：它是两期都在且同口径的绝对变化，不需要正基期才有意义。
                entry["change_ratio"] = (
                    money_of((now_value - was_value) / was_value)
                    if now_value is not None and was_value is not None
                    and was_value > 0 else None)
            rows.append(entry)
    block: dict[str, Any] = {"window": _iso_pair(previous), "comparable": comparable,
                             "rows": rows}
    if reason is not None:
        block["reason"] = reason
    runtime.comparison = block


def _document_value(runtime: CommerceRuntime, shop: str, metric: str) -> Any:
    """单据面：一家店一行（已按唯一 ERP 单据聚过），不查行表也不互相冒充。"""
    for row in runtime.facet_rows:
        if row.get("shop_id") == shop and metric in row:
            return row[metric]
    return None


def _window_value(runtime: CommerceRuntime, shop: str, metric: str,
                  window: tuple[date, date]) -> Any:
    """一家店一个商品面指标在某个窗口内的值：该店该窗口内**全部行性质**一次聚合。

    拿 (店铺, 行性质) 结果表的第一行当本期值是错的：一家店同时有普通销售行与套件/组合
    父行时，“第一行”只是 sale 那一组，而上期按全行性质聚合——两侧集合不同，差额与
    增长率就变成两个不同口径之差，而这两个数正是模型拿去说“环比”的东西。
    结果表本身逐行性质发行是对的（商品面与套件面不许混），比较两侧则必须共用本函数。
    赠品父行在视图层已被排除；分摊未核验与套件/组合/加工的成本语义由 `_group_input`
    统一处理，所以两期的 null 形状也是一致的。
    """
    lines = [line for line in runtime.lines
             if line.shop_id == shop and _in(window, line.day)]
    if not lines:
        return None
    return combine_reference_metrics([_group_input(line)
                                      for line in lines]).get(metric)


def _current_value(runtime: CommerceRuntime, shop: str, metric: str) -> Any:
    """本期已发布值（主窗口）。"""
    request = runtime.request
    assert request is not None
    if METRIC_FACET.get(metric) == "erp_document":
        return _document_value(runtime, shop, metric)
    return _window_value(runtime, shop, metric, (request.start, request.end))


def _previous_value(runtime: CommerceRuntime, shop: str, metric: str) -> Any:
    """上期同店同指标值：只有商品父行面能按行重算，其它面本轮不单独取上期数。

    单据面与支付面的上期值需要各自取数与各自的能力判定；这一版没有取。
    与其给一个口径不明的差额，不如不给。
    """
    if METRIC_FACET.get(metric) != "product_reference":
        return None
    assert runtime.previous_window is not None
    return _window_value(runtime, shop, metric, runtime.previous_window)


# ---------------------------------------------------------------------------
# 节点 10：份额、候选与状态
# ---------------------------------------------------------------------------


def classify_findings(runtime: CommerceRuntime) -> None:
    request = runtime.request
    assert request is not None
    _apply_shares(runtime)
    sale_rows = [{"shop_ref": _ref_of(runtime, str(row["shop_id"])),
                  "sold_quantity": row.get("sold_quantity"),
                  "sales_amount": row.get("sales_amount"),
                  "sales_share": row.get("sales_share"),
                  "product_gross_profit_reference":
                      row.get("product_gross_profit_reference"),
                  "product_gross_margin_reference":
                      row.get("product_gross_margin_reference")}
                 for row in runtime.rows if str(row.get("line_kind")) == "sale"]
    runtime.opportunity = low_profit_candidates(sale_rows, threshold=None,
                                               min_sample=None)
    runtime.limitations.append(
        "低利润阈值与最小样本没有版本化规则，只提供排序候选，不推导投放回报或预算")
    if runtime.rows and runtime.facet_rows:
        runtime.limitations.append(
            "商品毛利与 ERP 单据毛利是两个口径面，不能相加、相除或互相分摊")
    if runtime.totals and runtime.excluded:
        runtime.limitations.append(
            "不含 shop_ref 的行是已评估店铺集合的合计，不等于全部获准店铺合计")
    datasets = _datasets(runtime)
    runtime.result = datasets[0].result if datasets else None
    partial = bool(runtime.excluded or runtime.incomparable_metrics
                   or _statuses_all_missing(runtime))
    # 本节点不能把 `status` 换成终态：驱动里“非 running”等价于提前终止，那样
    # Artifact 就不会落库，一份算对了的报告会变成“没有任何数据可发”。终态由
    # `finalize_run` 按 `final_status` 落；只有“一面可发的数据都没有”才是真停。
    runtime.state = runtime.state.model_copy(update={
        "target_status": DomainStatus.PARTIAL if partial else DomainStatus.SUCCESS,
        "tool_status": "ok", "limitations": _limitation_codes(runtime.limitations),
        "data_as_of": runtime.data_as_of})
    runtime.final_status = RunStatus.PARTIAL if partial else RunStatus.SUCCEEDED
    runtime.report = _report(runtime, status="partial" if partial else "ok",
                             run_status=runtime.final_status)
    if not datasets:
        # 一面都没有可发布的数据（例如只有缺失组）：不发布成功。
        _stop(runtime, kind="missing_data", code="coverage_incomplete", problems=[],
              stage=CommerceNode.CLASSIFY_FINDINGS,
              message="所查时间段的数据覆盖不足，可按建议窗口查询或等待回填完成。",
              limitations=["覆盖未完成，拒绝部分汇总；缺口见coverage.gaps"])


def _apply_shares(runtime: CommerceRuntime) -> None:
    """逐店份额：分母固定为**已评估集合**的同行性质合计。"""
    for kind in {str(row.get("line_kind")) for row in runtime.rows}:
        rows = [row for row in runtime.rows if str(row.get("line_kind")) == kind]
        total_row = next((item for item in runtime.totals
                          if str(item.get("line_kind")) == kind), None)
        for metric in ("sold_quantity", "sales_amount"):
            total = to_decimal((total_row or {}).get(metric))
            shared = sales_shares([{**row, "shop_ref": _ref_of(
                runtime, str(row["shop_id"]))} for row in rows], metric, total)
            for source, result in zip(rows, shared):
                source["sales_share"] = result["sales_share"]


def _statuses_all_missing(runtime: CommerceRuntime) -> bool:
    """一个可评估的指标都没有：这份报告没有数字可发，不能算成功。"""
    return bool(runtime.statuses) and all(
        item.get("status") != "available" for item in runtime.statuses)


# ---------------------------------------------------------------------------
# 数据集
# ---------------------------------------------------------------------------


# 哪些披露只约束某一份数据集。默认（不在这张表里）是“每一份都该听见”：范围、覆盖、
# 质量、时间口径这类缺口同时动摇所有数字，宁可重复说一句，也不能让某一份表格看着干净。
# 反过来，成本覆盖率只动摇商品面，单据覆盖率只动摇单据面：把它们发给不相干的面，
# 等于让一份完整的数字背上一个它没有的限定。
_FACET_LIMITATIONS: dict[str, tuple[str, ...]] = {
    "cost_coverage_incomplete": ("metric_result",),
    "cost_semantics_unverified": ("metric_result",),
    "allocation_unverified": ("metric_result",),
    "gross_profit_not_net": ("metric_result",),
    "product_zero_rows": ("metric_result",),
    "opportunity_policy_unconfigured": ("metric_result",),
    "trend_coverage_incomplete": ("trend_series",),
    "trend_zero_days": ("trend_series",),
    "erp_document_coverage_incomplete": ("comparison_table",),
    "erp_document_granularity": ("comparison_table",),
    "unverified_payments": ("comparison_table",),
    "payment_product_attribution_unavailable": ("metric_result", "comparison_table"),
}


def _dataset_limitations(runtime: CommerceRuntime, artifact_type: str) -> list[str]:
    """按限制码把披露分到它真正约束的那一份数据集上。"""
    selected: list[str] = []
    for text in runtime.limitations:
        codes = _limitation_codes([text])
        narrowed = [_FACET_LIMITATIONS[code] for code in _FACET_LIMITATIONS
                    if code in codes]
        if not narrowed or any(artifact_type in facet for facet in narrowed):
            selected.append(text)
    return selected


def _datasets(runtime: CommerceRuntime) -> list[CommerceDataset]:
    """三份数据集：商品面、七日序列、店铺级单据 / 支付面。

    每份都自带覆盖与口径凭证；趋势那份的覆盖是**趋势窗口**的覆盖，不与主期间混用。
    """
    request = runtime.request
    plan = runtime.plan
    if request is None or plan is None:
        return []
    coverage = Coverage(status="complete", start=request.start, end=request.end, gaps=[])
    datasets: list[CommerceDataset] = []
    if plan.product_facet and (runtime.rows or runtime.totals):
        names = sorted({str(metric) for metric in request.metrics
                        if metric in PRODUCT_FACET_METRICS})
        datasets.append(CommerceDataset(
            artifact_type="metric_result",
            result=_tool_result(runtime, [*runtime.rows, *runtime.totals], coverage,
                                _dataset_limitations(runtime, "metric_result"),
                                {name: COMMERCE_METRIC_DEFINITIONS[name]
                                 for name in names})))
    if plan.product_facet and runtime.trend_rows:
        gaps = sorted({f"{start.isoformat()}~{end.isoformat()}"
                       for windows in _window_gaps(runtime, runtime.trend_window).values()
                       for start, end in windows})
        trend_coverage = Coverage(
            status="complete" if not gaps else "partial", start=runtime.trend_window[0],
            end=runtime.trend_window[1], gaps=gaps)
        datasets.append(CommerceDataset(
            artifact_type="trend_series",
            result=_tool_result(
                runtime, runtime.trend_rows, trend_coverage,
                _dataset_limitations(runtime, "trend_series"),
                {name: COMMERCE_METRIC_DEFINITIONS[name]
                 for name in ("sold_quantity", "sales_amount")})))
    if runtime.facet_rows:
        names = sorted({key for row in runtime.facet_rows for key in row
                        if key in COMMERCE_METRIC_DEFINITIONS})
        datasets.append(CommerceDataset(
            artifact_type="comparison_table",
            result=_tool_result(
                runtime, runtime.facet_rows, coverage,
                _dataset_limitations(runtime, "comparison_table"),
                {name: COMMERCE_METRIC_DEFINITIONS[name] for name in names})))
    return datasets


def _tool_result(runtime: CommerceRuntime, rows: Sequence[Mapping[str, Any]],
                 coverage: Coverage, limitations: Sequence[str],
                 definitions: Mapping[str, str]) -> ToolResult:
    evaluated = set(runtime.candidate_shop_ids)
    return ToolResult(
        status="ok", data=[dict(row) for row in rows],
        metric_definition=dict(definitions), filters=_filters(runtime),
        coverage=coverage, limitations=list(limitations),
        data_as_of=runtime.data_as_of, source_batches=runtime.source_batches,
        # 口径凭证只留已评估店铺：被排除的店已经在 excluded_scope 里说了不算，
        # 再给它发一张“本结果按此口径得出”的凭证就是两份互相矛盾的出处。
        basis=[entry for entry in runtime.basis if entry["shop_id"] in evaluated],
        diagnostics=dict(runtime.diagnostics))


# ---------------------------------------------------------------------------
# 持久化与收尾
# ---------------------------------------------------------------------------


def build_commerce_catalog(runtime: CommerceRuntime) -> None:
    """建一次目录：引用与展示名的唯一换面处，越权店铺在这里直接失败。"""
    rows = list(runtime.rows)
    seen = {str(row.get("shop_id")) for row in rows}
    names = _name_columns(runtime)
    for shop in runtime.candidate_shop_ids:
        if shop not in seen and runtime.erp_product_id is not None:
            rows.append({"shop_id": shop, "product_id": runtime.erp_product_id,
                         **names})
    if not rows and runtime.erp_product_id is not None:
        rows.append({"product_id": runtime.erp_product_id, **names})
    source = ToolResult(status="ok", data=rows, filters=_filters(runtime),
                        coverage=runtime.state.coverage
                        or Coverage(status="complete", start=date(1970, 1, 1),
                                    end=date(1970, 1, 2), gaps=[]))
    runtime.catalog = build_catalog(runtime.context.conn, source,
                                    allowed_shop_ids=runtime.context.allowed_shop_ids)


def persist_artifacts(runtime: CommerceRuntime, store: Any) -> None:
    """保存公开投影；必需数据集保存失败就是 failed，不许发布成功。"""
    if runtime.catalog is None or runtime.report is None:
        _stop(runtime, kind="unavailable", code="result_contract_violation",
              problems=["result_contract_violation"],
              stage=CommerceNode.PERSIST_ARTIFACTS, message="查询结果异常。")
        return
    from .tool import project_dataset

    refs: list[Any] = []
    published: list[tuple[Any, dict[str, Any]]] = []
    try:
        for dataset in runtime.report.datasets:
            payload = project_dataset(dataset, runtime.catalog, runtime.report)
            ref = store.save_artifact(runtime.state.run_id, NewArtifact(
                artifact_type=dataset.artifact_type, payload=payload,
                data_as_of=dataset.result.data_as_of,
                coverage=dataset.result.coverage.model_dump(mode="json")))
            refs.append(ref)
            published.append((ref, payload))
    except Exception:  # noqa: BLE001 - 保存失败与契约违规都不许变成成功，也不许带出原文
        runtime.published = []
        _stop(runtime, kind="unavailable", code="artifact_persistence_failed",
              problems=["artifact_persistence_failed"],
              stage=CommerceNode.PERSIST_ARTIFACTS,
              message="结果保存失败，请稍后重试。")
        return
    runtime.published = published
    runtime.state = runtime.state.model_copy(update={"artifact_refs": refs})


def finalize_run(runtime: CommerceRuntime, store: Any) -> None:
    """收尾：终态由分类节点定下来，不在这里重新推断。

    分类节点不会把 `status` 换成终态（否则驱动会当成提前终止，Artifact 就永远不落库），
    所以这里才是运行记录唯一的一次终态写入。
    """
    status = runtime.final_status or _run_status(
        runtime.state.target_status or DomainStatus.FAILED)
    runtime.state = runtime.state.model_copy(update={
        "status": status, "revision": runtime.state.revision + 1})
    store.finish(runtime.state.run_id, _completion(runtime, status))


def _finish(runtime: CommerceRuntime, store: Any) -> None:
    """提前收尾：终止原因与血缘必须跟终态一起落库。

    沿用业务查询图的 `_finish_early` 会少一个 `termination_reason`：“保存失败”与
    “参数不完整”在运行表上变成同一个空白，恢复层就只能去查聊天文本。
    血缘同理：缺了指纹的提前终止在运行表上就是一条“查不出是谁问过什么”的记录，
    而恢复与复用都按指纹判定。
    """
    previous = runtime.state
    if runtime.provenance is not None or runtime.identity is not None:
        store.record_provenance(previous.run_id, provenance=runtime.provenance,
                                identity=runtime.identity)
    finished = previous.model_copy(update={"revision": previous.revision + 1})
    store.finish(finished.run_id, RunCompletion(
        expected_revision=previous.revision, node=finished.node.value,
        status=finished.status, state=finished.model_dump(mode="json"),
        payload=_event_payload(runtime),
        error_code=finished.error.code if finished.error else None,
        termination_reason=_termination_reason(runtime, finished.status)))
    runtime.state = finished


def _completion(runtime: CommerceRuntime, status: RunStatus) -> Any:
    from bi_agent.runtime.models import RunCompletion

    return RunCompletion(
        expected_revision=runtime.state.revision - 1,
        node=CommerceNode.FINALIZE.value, status=status,
        state=runtime.state.model_dump(mode="json"),
        payload=_event_payload(runtime),
        error_code=runtime.state.error.code if runtime.state.error else None,
        termination_reason=_termination_reason(runtime, status))


# ---------------------------------------------------------------------------
# 驱动
# ---------------------------------------------------------------------------

_PRE_NODES: tuple[tuple[CommerceNode, Callable[[CommerceRuntime], None]], ...] = (
    (CommerceNode.RESOLVE_SCOPE, resolve_scope),
    (CommerceNode.RESOLVE_PRODUCT, resolve_product_if_needed),
    (CommerceNode.RESOLVE_METRIC_BASIS, resolve_metric_basis),
)
_SNAPSHOT_NODES: tuple[tuple[CommerceNode, Callable[[CommerceRuntime], None]], ...] = (
    (CommerceNode.CHECK_CAPABILITIES_AND_COVERAGE, check_capabilities_and_coverage),
    (CommerceNode.FREEZE_VERSIONS, freeze_versions),
    (CommerceNode.PLAN_FIXED_QUERIES, plan_fixed_queries),
    (CommerceNode.EXECUTE_AGGREGATES, execute_aggregates),
    (CommerceNode.COMPUTE_METRICS, compute_metrics),
    (CommerceNode.BUILD_COMPARISON_AND_TREND, build_comparison_and_trend),
    # 份额与候选也要读一次趋势覆盖，所以它仍在同一个只读快照里：快照退出后再算就会
    # 出现"数字来自旧快照、缺口判断来自新快照"的错配。
    (CommerceNode.CLASSIFY_FINDINGS, classify_findings),
)


@dataclass
class CommerceExecution:
    """内部兼容结果：绝不直接持久化。"""

    domain_result: DomainResult
    report: CommerceReport | None = None
    session_filters: Mapping[str, object] = field(default_factory=dict)


def run_commerce_graph(*, report_kind: str, request: ProductPerformanceRequest,
                       context: DomainContext, tool_call_id: str,
                       arguments: Mapping[str, Any] | None = None,
                       arguments_error: str | None = None) -> CommerceExecution:
    """执行一次经营图。`report_kind` 目前只接受 `product`。

    `comparison` 属于 compare_performance（计划 Task 8）：本轮没有平台 / 店铺分组契约,
    也没有图表规范，所以它在这里是**显式不支持**，不是"先拿商品报告顶着"。
    """
    if report_kind != "product":
        raise UnsupportedReportKind(report_kind)
    return _execute(context, request=request, tool_call_id=tool_call_id,
                    arguments=arguments, arguments_error=arguments_error)


def _execute(context: DomainContext, *, request: ProductPerformanceRequest | None,
             tool_call_id: str, arguments: Mapping[str, Any] | None,
             arguments_error: str | None) -> CommerceExecution:
    """建一条 commerce 运行记录并按固定链推进。

    任何意外失败都先尽力收尾再上抛：一条永远 running 的记录会占住
    `(user_message_id, domain, attempt_no)` 这个唯一上下文，而会话没有回收器。
    """
    store = context.store
    run_id = store.create_run(NewQueryRun(
        chat_id=context.chat_id, user_message_id=context.user_message_id,
        subject_id=context.subject_id, tool_call_id=tool_call_id, domain=DOMAIN,
        attempt_no=context.attempt_no, normalized_request={},
        state={"node": CommerceNode.RESOLVE_SCOPE.value,
               "status": RunStatus.RUNNING.value, "revision": 0}))
    runtime = CommerceRuntime(
        state=CommerceState(run_id=run_id, normalized_request={}), context=context,
        tool_call_id=tool_call_id, request=request)
    try:
        return _run_nodes(runtime, store, arguments_error=arguments_error)
    except Exception:  # noqa: BLE001 - 收尾后原样上抛，由外层做脱敏
        _finish_run_as_failed(runtime, store)
        raise


def _run_nodes(runtime: CommerceRuntime, store: Any, *,
               arguments_error: str | None) -> CommerceExecution:
    if arguments_error is not None or runtime.request is None:
        _stop(runtime, kind="needs_input", code="invalid_parameters",
              problems=["invalid_parameters"], stage=CommerceNode.RESOLVE_SCOPE,
              message="查询参数无效，请调整后重试。")
        runtime.state = runtime.state.model_copy(update={"status": RunStatus.NEEDS_INPUT})
        _persist_transition(runtime, store, _event_payload(runtime))
        _finish(runtime, store)
        return _execution_result(runtime)

    for node, action in _PRE_NODES:
        if runtime.state.node is not node:
            runtime.state = transition_state(runtime.state, node)
        action(runtime)
        _persist_transition(runtime, store, _event_payload(runtime))
        if runtime.state.status is not RunStatus.RUNNING:
            _finish(runtime, store)
            return _execution_result(runtime)

    try:
        with read_only_snapshot(runtime.context.conn):
            for node, action in _SNAPSHOT_NODES:
                runtime.state = transition_state(runtime.state, node)
                action(runtime)
                runtime.pending.append((runtime.state, _event_payload(runtime)))
                if runtime.state.status is not RunStatus.RUNNING:
                    break
    except _BudgetExhausted:
        _stop(runtime, kind="unavailable", code="deadline_exceeded", problems=[],
              stage=CommerceNode.EXECUTE_AGGREGATES,
              message="本次查询时间预算已耗尽，请缩小日期或店铺范围后重试。",
              limitations=["本次查询时间预算已耗尽"])
    except _RowsTruncated:
        _stop(runtime, kind="unavailable", code="result_too_large", problems=[],
              stage=CommerceNode.EXECUTE_AGGREGATES, message="查询暂不可用，请稍后重试。",
              limitations=[f"结果行数达到{MAX_ROWS}上限，已拒绝出数以避免静默截断；"
                           "请缩小日期范围或店铺范围"])
    except psycopg.errors.QueryCanceled:
        _stop(runtime, kind="unavailable", code="query_timeout", problems=[],
              stage=CommerceNode.EXECUTE_AGGREGATES, message="查询已超时，请稍后重试。",
              limitations=["查询超时"])
    except (psycopg.OperationalError, psycopg.InterfaceError):
        # 连接类故障：不猜类型也不自动追加查询（预算与恢复决策归外层图）。
        _stop(runtime, kind="unavailable", code="unavailable", problems=[],
              stage=CommerceNode.EXECUTE_AGGREGATES, message="查询暂不可用，请稍后重试。")
    if not runtime.pending or runtime.pending[-1][0] is not runtime.state:
        runtime.pending.append((runtime.state, _event_payload(runtime)))
    # 快照内不写库：退出只读事务后按节点原顺序补写状态。
    for captured, payload in runtime.pending:
        runtime.state = captured.model_copy(update={"revision": runtime.state.revision})
        _persist_transition(runtime, store, payload)
    if runtime.state.status is not RunStatus.RUNNING:
        _finish(runtime, store)
        return _execution_result(runtime)

    try:
        build_commerce_catalog(runtime)
    except Exception:  # noqa: BLE001 - 名称解析失败就不能把未核验的行发出去
        _stop(runtime, kind="unavailable", code="result_contract_violation",
              problems=["result_contract_violation"],
              stage=CommerceNode.PERSIST_ARTIFACTS, message="查询结果异常。")
        _persist_transition(runtime, store, _event_payload(runtime))
        _finish(runtime, store)
        return _execution_result(runtime)

    assert runtime.provenance is not None and runtime.identity is not None
    store.record_provenance(runtime.state.run_id, provenance=runtime.provenance,
                            identity=runtime.identity)
    runtime.state = transition_state(runtime.state, CommerceNode.PERSIST_ARTIFACTS)
    persist_artifacts(runtime, store)
    _persist_transition(runtime, store, _event_payload(runtime))
    if runtime.state.status is not RunStatus.RUNNING:
        _finish(runtime, store)
        return _execution_result(runtime)
    runtime.state = transition_state(runtime.state, CommerceNode.FINALIZE)
    finalize_run(runtime, store)
    return _execution_result(runtime)


def _execution_result(runtime: CommerceRuntime) -> CommerceExecution:
    from .tool import model_payload

    state = runtime.state
    status = state.target_status or DomainStatus.FAILED
    report = runtime.report or _report(runtime, status="unavailable")
    safe = not (state.error is not None and state.error.code
                in {"artifact_persistence_failed", "result_contract_violation"})
    model: dict[str, object]
    artifacts: list[DomainArtifact] = []
    if report.datasets and runtime.catalog is not None and safe:
        try:
            model = model_payload(report, runtime.catalog)
            artifacts = [DomainArtifact(ref=ref, public_payload=payload)
                         for ref, payload in runtime.published]
        except ValueError:
            # 投影失败说明结果不符合安全契约：三个出口一起关闭，被拒数字不得回流。
            status = DomainStatus.FAILED
            model = {"status": "failed"}
            artifacts = []
            safe = False
    else:
        model = _silent_payload(report, safe=safe)
    return CommerceExecution(
        domain_result=DomainResult(
            run_id=state.run_id, status=status, model_payload=model,
            artifacts=artifacts, data_as_of=state.data_as_of,
            coverage=state.coverage, error=state.error,
            provenance=runtime.provenance, identity=runtime.identity),
        report=report if safe else None,
        session_filters={})


def _silent_payload(report: CommerceReport, *, safe: bool) -> dict[str, object]:
    """没有可发数据集时的模型载荷：只给状态与已登记的披露，数字一个都不给。

    授权失败连原因都不发：一句“这家店不在授权范围”本身就在证实那家店存在，
    而越权引用该得到的只是一个否。其余三种情态（要参数、缺数据、不可用）
    必须带披露：经营者要能分清“没解析出商品”与“今天没销量”是两个不同的答案。
    """
    if not safe or report.status == "forbidden":
        return {"status": "failed"}
    payload: dict[str, object] = {"status": report.status}
    if report.limitations:
        # 披露文本要么在公开白名单里，要么有模式：拿不准就不发，不给模型留自由文本。
        payload["limitations"] = list(report.limitations)
    if report.candidates:
        # 歧义要把选择交回用户：不给候选引用，用户就只能重新猜一个同名词。
        payload["candidates"] = [dict(item) for item in report.candidates]
    return payload
