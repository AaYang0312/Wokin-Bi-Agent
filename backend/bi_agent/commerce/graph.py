"""CommercePerformanceGraph：spec §6 的固定节点链（运营工作流计划 Task 7 / Task 8）。

    resolve_scope → resolve_product_if_needed → resolve_metric_basis
    → check_capabilities_and_coverage → freeze_versions → plan_fixed_queries
    → execute_aggregates → compute_metrics → build_comparison_and_trend
    → classify_findings → persist_artifacts → finalize

两个公开 Tool 跑同一张图（`report_kind=product|comparison`）：商品报告按
(店铺, 行性质) 发行，对比报告按 (平台 | 店铺) 分组发行，两者共用同一套授权、
能力 / 口径 / 覆盖门禁、同一份只读快照与同一套发布与降级规则。

四条结构性约束：

1. **门禁在聚合之前，缺口分组不并入合计**。能力、时间口径、覆盖三类缺口各自归因；
   一家店回答不了就进 `excluded_scope`，它的数字既不出现也不参与合计与排名，缺数据
   不会被读成 0。多指标独立判定：能答销量就先给销量，不要求全部指标同时可算。
2. **一次只读快照**。覆盖判定、跨店合计、分组行、七日序列与上期比较都在同一个
   REPEATABLE READ 事务里读出：中途插进一次回填就会把两个数据版本拼成一份报告。
   该事务是只读的，所以这一段的状态写入按原顺序延后到快照退出之后再落库。
3. **降级有边界**。单指标 / 单来源不可用可以是 partial；授权失败、契约违规与
   必需 Artifact 保存失败一律 failed，不许冒充成功。
4. **对比只统计完整且同口径的分组集合**（Task 8）。一个分组里有任一家获准店铺未被
   评估，就不发布该分组的合计；跨分组的合计与排名只覆盖同口径且每个分组都有值的
   集合，否则只分行展示。分组不完整的店仍然在 `excluded_scope` 里逐项可查。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Any, Callable, Mapping, NamedTuple, Sequence

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
    Coverage, MAX_ROWS, METRIC_DEFINITIONS, QueryRequest, ToolResult,
    _BudgetExhausted, _RowsTruncated, _capability_gap, _window_range,
    read_only_snapshot)
from bi_agent.runtime.artifacts import (
    CHART_SPEC_VERSION, QueryProvenance, RequestIdentity, basis_signature_of,
    request_fingerprint)
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
from bi_agent.presentation.charts import PersistedDataset, build_chart_spec
from bi_agent.sources import (
    AFTERSALE_COHORT_ENTITY, AFTERSALE_ENTITY, ORDERS_ENTITY, PAYMENT_WINDOW_METRICS,
    ShopRecord, binding_signature, registration, resolve_metric_dependencies,
    unsupported_reason)

from . import repository
from .metrics import (
    CHART_METRIC_UNITS,
    COMBINED_VALUE_COLUMNS,
    COLUMN_GOVERNING_METRIC,
    COMMERCE_METRICS,
    COMPARISON_RANKED_COLUMNS,
    COUNT_VALUE_COLUMNS,
    COMMERCE_GRAPH_VERSION,
    COMMERCE_METRIC_CAPABILITIES,
    COMMERCE_METRIC_DEFINITIONS,
    COMMERCE_METRIC_VERSION,
    COMPARISON_GROUP_COLUMN,
    COMPARISON_TREND_PLATFORM_ROWS,
    COMPARISON_TREND_SHOP_ROWS,
    DOCUMENT_ROWS,
    METRIC_FACET,
    PAYMENT_CAPABILITY_COLUMNS,
    PAYMENT_ROWS,
    PLATFORM_GROUP_RULE,
    PRODUCT_FACET_METRICS,
    PRODUCT_ROWS,
    PRODUCT_TOTAL_ROWS,
    RATIO_VALUE_COLUMNS,
    TAOBAO_FAMILY_PLATFORMS,
    TREND_ROWS,
    combine_reference_metrics,
    comparison_row_columns,
    low_profit_candidates,
    money_of,
    platform_group_of,
    project,
    rank_groups,
    sales_shares,
    to_decimal,
)
from .models import (
    CommerceDataset,
    CommerceReport,
    DomainContext,
    PerformanceComparisonRequest,
    ProductPerformanceRequest,
)

# 两个公开 Tool 的入参契约：字段集合不同，但图上用的到的都是同一组属性
# （start/end/metrics/sales_basis/profit_basis/trend_window/normalized/scope）。
CommerceRequest = ProductPerformanceRequest | PerformanceComparisonRequest

DOMAIN = "commerce_performance"
TEMPLATE_ID = "commerce_product_report"
# 对比报告走同一张图，但模板是另一份：固定模板 ID / 版本要能区分“哪个报形跑了”，
# 否则同一个 template_id 下会出现两种行形，回看血缘时分不开。
COMPARISON_TEMPLATE_ID = "commerce_comparison_report"
TEMPLATE_VERSION = "1"
# 歧义候选卡片的张数上限：候选全集可能很大，一次性发给模型只会让它自己在长列表里猜。
# 披露文本里给的是**全量**家数，这里只是可核对的前几张。
MAX_CANDIDATE_CARDS = 20
# 图表存不下去时补进数据集的那句披露：与 `commerce.metrics` 的公开文本同一句。
CHART_UNAVAILABLE_TEXT = (
    "本轮没有可画的指标：图表只引用同口径的已落库数据集，没有可画集合时只发表格")
# 分组可答但本轮没有可发布事实（零行、覆盖不全）时的那句披露：同样只说一遍。
CELL_WITHHELD_TEXT = (
    "有分组的单元格算不出来（本轮没有可发布的事实行），原因见各面覆盖披露")
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
    """本轮真正要跑的固定模板：读窗口与各面开关。"""

    read_window: tuple[date, date]
    product_facet: bool
    document_facet: bool
    payment_facet: bool
    # 对比报告读的是同一张视图的另一粒度（整店汇总），不是“再跑一次商品面查询”。
    group_facet: bool = False


@dataclass
class ChartPlan:
    """一张待生成的图表：到发布节点才拿得到被引用数据集的真实落库引用。"""

    dataset_type: str
    kind: str
    x: str
    y: str
    series: tuple[str, ...]
    basis_entry: Mapping[str, str]


@dataclass
class CommerceRuntime:
    """图内可变状态：真实主键只活在这里，不进 Store、不进事件、不进模型载荷。"""

    state: CommerceState
    context: DomainContext
    tool_call_id: str
    report_kind: str = "product"
    request: CommerceRequest | None = None
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
    # 对比报告的整店汇总行（与 `lines` 同一类型，只是不按商品筛）：两面各自取数，
    # 不会在同一次执行里既读商品面又读分组面。
    groups: tuple[repository.ProductLine, ...] = ()
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
    # 对比报告（Task 8）：分组集合、分组行与由它们导出的排名 / 图表计划。
    # requested_groups 在**门禁与剪枝之前**采下：一个分组“有几家获准店”不能被
    # 缺口改变，否则排除一家店同时会让剩下那几家看着像“整个平台”。
    requested_groups: dict[str, tuple[str, ...]] = field(default_factory=dict)
    group_column: str = "platform"
    published_groups: dict[str, tuple[str, ...]] = field(default_factory=dict)
    available: dict[str, set[str]] = field(default_factory=dict)
    group_rows: list[dict[str, Any]] = field(default_factory=list)
    group_trend_rows: list[dict[str, Any]] = field(default_factory=list)
    group_statuses: list[dict[str, Any]] = field(default_factory=list)
    ranking: list[dict[str, Any]] = field(default_factory=list)
    chart_plans: list[ChartPlan] = field(default_factory=list)


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
        "report_kind": runtime.report_kind,
        # 份额只有一个合法分母：已评估集合。留着这个键让"分母是谁"可被核对。
        "sales_share_basis": "evaluated_only",
    }
    if isinstance(request, PerformanceComparisonRequest):
        # 分组维度是合计与排名能不能做的直接依据：商品报告不带这个键
        # （它的分行由行性质决定）。
        filters["group_by"] = request.group_by
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
    ("已发布分组的合计", "commerce_scope_excluded"),
    ("不发布该分组数字", "comparison_group_partial"),
    ("个指标的合计已拒答", "comparison_total_withheld"),
    ("单元格算不出来", "comparison_cell_withheld"),
    ("没有可画的指标", "comparison_chart_unavailable"),
    ("没有已批准的版本化合并规则", "platform_group_rule_unconfigured"),
    ("只能按支付面两列对比", "payment_product_attribution_unavailable"),
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
        template_id=(COMPARISON_TEMPLATE_ID if runtime.report_kind == "comparison"
                     else TEMPLATE_ID),
        template_version=TEMPLATE_VERSION,
        metric_version=COMMERCE_METRIC_VERSION, graph_version=COMMERCE_GRAPH_VERSION,
        # 目录版本只取已冻结在图上的值：提前终止时不补一次 SQL，那已超过预算。
        catalog_version=runtime.catalog_version or 0,
        # 商品报告的映射版本来自已解析的渠道映射；对比报告不解析商品，这里落的是
        # **平台分组规则版本**：分组口径一变，旧结果就不再是同一个问题的答案。
        mapping_version=(mapping or (PLATFORM_GROUP_RULE
                                     if runtime.report_kind == "comparison"
                                     else defaults.mapping_version)),
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
        trend_window=(_iso_pair(runtime.trend_window)
                      if runtime.trend_rows or runtime.group_trend_rows else None),
        termination_reason=_termination_reason(runtime, resolved),
        limitations=tuple(runtime.limitations),
        candidates=runtime.candidates,
        # 对比面专属：没发布哪些分组、哪些分组进了排名 / 合计、能进哪几张图。
        group_statuses=tuple(runtime.group_statuses),
        ranking=tuple(runtime.ranking),
        evaluated_platforms=tuple(
            sorted(runtime.published_groups) if runtime.group_column == "platform" else ()),
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
    # 对比报告要先采下“本轮要回答哪几个分组”：采在门禁与剪枝**之前**，否则排除一家
    # 店会同时把剩下的那几家说成“整个平台”。
    _plan_groups(runtime)

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


def _plan_groups(runtime: CommerceRuntime) -> None:
    """把已展开的获准店铺映射到分组键（平台码或店铺引用）。

    平台分组走 `platform_group_of`：本轮**没有**已批准的版本化合并规则，所以
    淘宝与天猫各自成组（spec §3）。“淘系”不是一个可以被静默引入的默认组。
    """
    request = runtime.request
    if runtime.report_kind != "comparison" or not isinstance(
            request, PerformanceComparisonRequest):
        return
    runtime.group_column = request.group_column
    for shop in runtime.candidate_shop_ids:
        profile = runtime.profiles.get(shop)
        # 店铺分组的键就是店铺本身：行里带 `shop_id`，到投影处再换成引用。
        # 直接把引用当分组键留着，下游会拿"ent-xxxx"当真实主键去查目录。
        key = (shop if request.group_by == "shop" else
               platform_group_of(profile.platform) if profile is not None else None)
        if key is None:
            continue          # 没有店铺档案 -> 下一句就会被当成未同步排除
        runtime.requested_groups.setdefault(key, []).append(shop)
    runtime.requested_groups = {
        key: tuple(sorted(shops)) for key, shops in sorted(runtime.requested_groups.items())}
    if (request.group_by == "platform"
            and TAOBAO_FAMILY_PLATFORMS <= set(runtime.requested_groups)):
        runtime.limitations.append(
            "淘宝与天猫本轮没有已批准的版本化合并规则，按两个平台分列，不并成一个淘系组")


def runtime_group_by(runtime: CommerceRuntime) -> str:
    """本轮分组维度：行里的列名与对外标签列都由它一处定，不在三处各判一次。"""
    return "shop" if runtime.group_column == "shop_id" else "platform"


def _group_label(runtime: CommerceRuntime, key: str) -> str:
    """分组键的对外形态：店铺分组换引用，平台分组原样就是平台码。"""
    return _ref_of(runtime, key) if runtime.group_column == "shop_id" else key




# ---------------------------------------------------------------------------
# 节点 2：商品解析（复用 Task 6 解析器，不长第二份）
# ---------------------------------------------------------------------------


def resolve_product_if_needed(runtime: CommerceRuntime) -> None:
    """把 ref 或文本换成一个 ERP 商品；歧义交回澄清，零候选不是零销量。

    节点名里的 if_needed 在对比报告上就是“不需要”：平台 / 店铺对比不筛商品，
    这里直接过去（仍然是同一个固定节点链，不在图外开第二条路径）。
    """
    if runtime.report_kind == "comparison":
        return
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


def _capability_gap_text(request: CommerceRequest) -> str:
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


def _decisive_metrics(request: CommerceRequest) -> frozenset[str]:
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
            bindings = runtime.bindings.get((shop, str(metric)), ())
            # 单元格级原因：只回答“这一家店、这一个指标，本窗口里算不算得出来”。
            cell_reason: str | None = blockers.get(shop, {}).get(str(metric))
            if (cell_reason is None and basis_only
                    and METRIC_FACET.get(str(metric)) == "product_reference"):
                cell_reason = ("basis_incompatible"
                               if runtime.report_kind == "comparison"
                               else "payment_product_attribution_unavailable")
            if cell_reason is None and not bindings:
                # 没有取数依赖就是“这个口径答不了这个指标”：不拿“没报错”当“可回答”。
                cell_reason = "capability_unavailable"
            if cell_reason is None:
                # “可算”集合只在这里记一次：分组单元格、合计、排名与图表都只读这一份，
                # 不在下游各自重新推断“这家店能不能答”。
                runtime.available.setdefault(shop, set()).add(str(metric))
            # 状态级原因再叠上“本次范围内同名不同口径”：它不动摇单店数字，
            # 但让它们不能再被汇总、排名或互相比较（Task 7 就是这样判合计行的）。
            reason = cell_reason
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
            "已验证支付口径没有商品级事实，商品销量与金额不能按该口径给出"
            if runtime.report_kind == "product" else
            "已验证支付口径下只能按支付面两列对比：报告指标的定义是 ERP 有效销售父项口径")
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
        product_facet=(runtime.report_kind == "product"
                       and request.sales_basis == "erp_effective_parent"
                       and any(METRIC_FACET.get(str(metric)) == "product_reference"
                               for metric in request.metrics)),
        # 对比报告读同一张视图的**整店聚合**（不按商品筛）：同一只读快照、同一套
        # 入条件，只是一条集合查询换了一个分组粒度，不是把商品面再跑一遍。
        group_facet=(runtime.report_kind == "comparison"
                     and request.sales_basis == "erp_effective_parent"
                     and any(METRIC_FACET.get(str(metric)) == "product_reference"
                             for metric in request.metrics)),
        document_facet=bool(document_tags),
        payment_facet=request.sales_basis == "verified_payment")


# ---------------------------------------------------------------------------
# 节点 7：一次集合查询取回全部面
# ---------------------------------------------------------------------------


def execute_aggregates(runtime: CommerceRuntime) -> None:
    """读商品父行面（或对比面的整店汇总）、ERP 单据面与（需要时）商业支付面。"""
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
    if plan.group_facet:
        runtime.groups = tuple(repository.load_shop_day_groups(
            context.conn, shop_ids=shops,
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
    if plan.product_facet or plan.document_facet or plan.group_facet:
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
    if runtime.report_kind == "comparison":
        _comparison_rows(runtime)
        return
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
# 对比面（Task 8）：分组行、合计、排名与图表计划
#
# 只多一个分组维度，不多一套算术：取值仍走 `combine_reference_metrics`，
# 可算性仍读 `_record_statuses` 留下的 `runtime.available`，口径签名仍出自
# `sources.binding_signature`。这里新增的只有一件事：**分组粒度**——
# 一个分组要么全量发布，要么不发布（缺一就不发），合计与排名要么覆盖全部
# 已发布分组且同口径，要么拒答。
# ---------------------------------------------------------------------------


def _source_rows(runtime: CommerceRuntime) -> Sequence[repository.ProductLine]:
    """本轮要聚合的销售父行：商品面按 (店, 日, 性质, 商品)，对比面按 (店, 日, 性质)。"""
    return runtime.groups if runtime.report_kind == "comparison" else runtime.lines


def _complete_groups(runtime: CommerceRuntime) -> dict[str, tuple[str, ...]]:
    """只留下“本组全部获准店铺都被评估过”的分组。

    缺一就不发：一个只盖了两家店的“淘宝合计”会被读成整个淘宝的数，而缺口在行里
    看不见（它只在 excluded_scope 里）。不发布的分组在 `group_statuses` 里逐组带原因，
    下钻到 `group_by=shop` 仍能看到已评估的那几家。
    """
    evaluated = set(runtime.candidate_shop_ids)
    return {key: shops for key, shops in runtime.requested_groups.items()
            if shops and all(shop in evaluated for shop in shops)}


class ColumnFacts(NamedTuple):
    """一个分组一个指标列的可发布事实：值、能不能答、按哪个口径、不可算的原因。

    把"值"与"能不能给这个值"放在一起，是为了让下游（行、合计、排名、图表）不可能
    只用其中一个：拿一个没有口径凭证的数去排名，正是这类报告最典型的错法。
    """

    value: Any
    answerable: bool
    basis: Mapping[str, str] | None
    signature: tuple | None
    reason: str | None


def _basis_of_bindings(bindings: Sequence) -> tuple[Mapping[str, str], tuple] | None:
    """一组取数依赖 → (口径凭证, 可比性签名)；没有依赖就是"这个口径答不了"。"""
    if not bindings:
        return None
    return ({"basis": str(bindings[0].basis), "time_basis": str(bindings[0].time_basis)},
            binding_signature(bindings))


def _group_basis(runtime: CommerceRuntime, shops: Sequence[str],
                 metric: str) -> tuple[Mapping[str, str], tuple] | None:
    """本分组本指标的口径凭证与可比性签名；算不出来或组内口径不一致时返回 None。

    可算性看 `runtime.available`（能力 / 覆盖 / 时间口径三道门禁共同的结论），而不是看
    "注册表能不能解出这条依赖"：后者对未授予能力的店铺照样能解出绑定，拿它当可算就是把
    "未接入"说成"已接入但没数据"。

    "同名不同口径"在这里只判**组内**：跨分组的口径差异不抹掉单组自己的数（spec §8 要的
    是分面或标不可比），它抹掉的是跨分组的合计与排名。
    """
    answers = []
    for shop in shops:
        if metric not in runtime.available.get(shop, set()):
            return None
        answers.append(_basis_of_bindings(runtime.bindings.get((shop, metric), ())))
    if any(answer is None for answer in answers) or not answers:
        return None
    signatures = {answer[1] for answer in answers}       # type: ignore[index]
    if len(signatures) != 1:
        return None
    return answers[0][0], answers[0][1]                   # type: ignore[index]


def _column_facts(runtime: CommerceRuntime, shops: Sequence[str], column: str,
                  value: Any) -> ColumnFacts:
    """一列的可发布性：报告指标看自己的凭证，证据列看把它带进来的那个指标。

    证据列（单据数、带毛利单据数、支付两列）不是报告指标，没有自己的口径凭证条目；
    它们跟着谁被请求进来，就跟着谁的口径判可比性。
    """
    governing = (column if column in COMMERCE_METRICS
                 else str(COLUMN_GOVERNING_METRIC.get(column) or column))
    basis: tuple[Mapping[str, str], tuple] | None = None
    if column in PAYMENT_CAPABILITY_COLUMNS:
        # 支付面两列本身就是已登记的能力标签：先看这家店被授予没有，再按标签解口径。
        # 只解依赖不看授予会把"未接入支付"的店也算出一个支付数，那正是能力门禁要挡的。
        per_shop = [None if unsupported_reason(_record_of(runtime, shop), column) is not None
                    else _basis_of_bindings(resolve_metric_dependencies(
                        _record_of(runtime, shop), column))
                    for shop in shops]
        if all(item is not None for item in per_shop) and per_shop:
            signatures = {item[1] for item in per_shop if item is not None}
            if len(signatures) == 1:
                basis = (per_shop[0][0], per_shop[0][1])      # type: ignore[index]
    else:
        basis = _group_basis(runtime, shops, governing)
    if basis is None:
        return ColumnFacts(value=None, answerable=False, basis=None, signature=None,
                           reason=_column_reason(runtime, shops, governing))
    # 能答这一问、但本轮这一格没有可发布的数（零行、成本或单据覆盖不全）：
    # 留 null 并给原因，不把它混进"这家店答不了"那一类，也不混进"没有事实"那一类。
    return ColumnFacts(value=value, answerable=True, basis=basis[0], signature=basis[1],
                       reason=_cell_reason(runtime, shops, governing, value))


def _column_reason(runtime: CommerceRuntime, shops: Sequence[str],
                   metric: str) -> str:
    """为什么这一列在本组不可算：取成员店铺里最说不动的那个原因，不自己编。

    分组没有独立的门禁判定：它的缺口一定来自某家成员店铺，把那条原因搬过来才
    能和 `metric_statuses` 里的逐店条目对上。
    """
    own_refs = {_ref_of(runtime, shop) for shop in shops}
    reasons = [str(item.get("reason")) for item in runtime.statuses
               if item.get("metric") == metric and str(item.get("shop_ref")) in own_refs]
    if not reasons:
        # 证据列（支付面两列）不是报告指标，逐店状态里没有它的条目：那就直接问注册表。
        # 不这么办就只能把"没授予这个能力"写成"未列入合计"，那是两种不同的缺口。
        reasons = [found for shop in shops
                  if (found := unsupported_reason(_record_of(runtime, shop), metric))
                  is not None]
    for preferred in ("source_unregistered", "coverage_time_basis_unverified",
                      "capability_unavailable", "capability_ungranted",
                      "coverage_incomplete", "basis_incompatible"):
        if preferred in reasons:
            return preferred
    return reasons[0] if reasons else "commerce_scope_excluded"


def _cell_reason(runtime: CommerceRuntime, shops: Sequence[str],
                 metric: str, value: Any) -> str | None:
    """可答但值为 null 时的原因码：两种"没有数"是三件事，不能说成一件。

    单据毛利缺的是**覆盖率**（有单据、部分单据不带毛利字段），商品面缺的是**本轮事实**；
    混成一个码就会让一句"没有可发布的事实行"盖住一条本该去补的取数缺口。
    """
    if value is not None:
        return None
    if metric == "erp_gross_profit_reference" and any(
            shop in runtime.documents
            and runtime.documents[shop].documents_with_gross_profit
            < runtime.documents[shop].documents
            for shop in shops):
        return "erp_document_coverage_incomplete"
    return "comparison_cell_withheld"


def _group_product_values(runtime: CommerceRuntime, shops: Sequence[str],
                         window: tuple[date, date]) -> dict[str, Any]:
    """分组在窗口内的商品面取值：与 `_window_value` 共用一份合并规则。

    窗口内没有行不等于"缺数"：能进这里的分组窗口都完整（缺覆盖的店已整店退出），
    所以那是真的没卖 —— 件数与金额给真实 0。均价与两个毛利列仍留 null：0÷0 无定义，
    没有成本行也没有可发布的毛利。"有行但成本没覆盖齐"与"零行"这两种 null 都由同一份
    `combine_reference_metrics` 输出，不在这里分叉。
    """
    wanted = set(shops)
    rows = [row for row in _source_rows(runtime)
            if row.shop_id in wanted and _in(window, row.day)]
    if not rows:
        # 能进这里的分组窗口都完整（缺覆盖的店已整店退出），所以"没有行"就是
        # 真的没卖：件数与金额给真实 0。均价与两个毛利列仍留 null ——
        # 0÷0 无定义，没有成本行也就没有可发布的毛利，补 0 是凭空造数。
        values = {key: None for key in COMBINED_VALUE_COLUMNS}
        values["sold_quantity"] = "0"
        values["sales_amount"] = "0"
        return values
    combined = combine_reference_metrics([_group_input(row) for row in rows])
    return {key: combined.get(key) for key in COMBINED_VALUE_COLUMNS}


def _group_document_values(runtime: CommerceRuntime,
                          shops: Sequence[str]) -> dict[str, Any]:
    """分组单据面：单据数与带毛利单据数逐店求和，毛利覆盖不全时留 null。

    成员店在取数结果里没有条目 = 它本轮窗口内**确实没有 ERP 单据**（覆盖门禁已经把它
    按缺口整店排除过了，能进这里的分组窗口都完整），所以它对两个计数与毛利都贡献真实
    的 0，本列照常发布。null 只出现在两种情况下：整组一张单据都没有（没有可加的事实），
    或有单据的店里有单据不带毛利字段（`with_profit < documents`）—— 后者是覆盖率问题，
    原因由 `_cell_reason` 单独立成 `erp_document_coverage_incomplete`，不混进"没有事实"。
    """
    facts = [runtime.documents[shop] for shop in shops if shop in runtime.documents]
    if not facts:
        return {"erp_documents": None, "erp_documents_with_gross_profit": None,
                "erp_gross_profit_reference": None}
    documents = sum(item.documents for item in facts)
    with_profit = sum(item.documents_with_gross_profit for item in facts)
    total: Decimal | None = None
    if all(item.gross_profit is not None for item in facts):
        total = sum((item.gross_profit for item in facts
                     if item.gross_profit is not None), Decimal(0))
    whole = with_profit == documents and total is not None
    return {"erp_documents": documents, "erp_documents_with_gross_profit": with_profit,
            "erp_gross_profit_reference": money_of(total) if whole else None}


def _group_payment_values(runtime: CommerceRuntime,
                          shops: Sequence[str]) -> dict[str, Any]:
    """分组已验证支付面：另一份事实集合，单独两列，不顶替商品面的 sales_amount。"""
    facts = [runtime.payments[shop] for shop in shops if shop in runtime.payments]
    if not facts:
        return {"paid_amount": None, "paid_orders": None}
    return {"paid_amount": money_of(sum(item.paid_amount for item in facts)),
            "paid_orders": sum(item.paid_orders for item in facts)}


def _group_values(runtime: CommerceRuntime, shops: Sequence[str],
                  window: tuple[date, date]) -> dict[str, Any]:
    """一个分组在某个窗口的全部可算取值（三个面各自独立，缺的面自然缺位）。"""
    plan = runtime.plan
    assert plan is not None
    values: dict[str, Any] = {}
    if plan.group_facet:
        values.update(_group_product_values(runtime, shops, window))
    if plan.document_facet:
        values.update(_group_document_values(runtime, shops))
    if plan.payment_facet:
        values.update(_group_payment_values(runtime, shops))
    return values


def _comparison_rows(runtime: CommerceRuntime) -> None:
    """发布分组行：一个分组一行，列形 = 分组键 + 本轮被请求的指标列。

    与商品面同一个铁则：行里只出现已评估的分组；一个分组的某列算不出来就是 null，
    不是 0。分组不完整的店仍在 excluded_scope 里逐家可查，缺哪个分组则逐组写在
    `group_statuses` 里（spec §8：无数据平台保留原因标签）。
    """
    request = runtime.request
    plan = runtime.plan
    assert isinstance(request, PerformanceComparisonRequest) and plan is not None
    main = (request.start, request.end)
    runtime.published_groups = _complete_groups(runtime)
    columns = comparison_row_columns(request.metrics, payments=plan.payment_facet,
                                     documents=plan.document_facet)
    declared = (runtime.group_column, *columns)
    facts: dict[str, dict[str, ColumnFacts]] = {}
    for key, shops in sorted(runtime.published_groups.items()):
        values = _group_values(runtime, shops, main)
        row_facts = {column: _column_facts(runtime, shops, column, values.get(column))
                     for column in columns}
        facts[key] = row_facts
        runtime.rows.append(project(
            declared, {**{runtime.group_column: key},
                       **{column: row_facts[column].value for column in columns}}))
    _group_statuses(runtime)
    _group_metric_statuses(runtime, facts)
    _comparison_totals(runtime, columns, facts)


def _group_metric_statuses(runtime: CommerceRuntime,
                           facts: Mapping[str, Mapping[str, ColumnFacts]]) -> None:
    """逐分组逐指标状态（spec §3："按店铺 / 平台 / 指标列出"）。

    逐店状态说的是"这家店答不答得了"，分组状态说的是"这一组本轮报不报得出数"：
    平台对比里用户读的是后者，只给前者就会让一个 null 单元格看起来像没写。
    """
    if runtime.group_column == "shop_id":
        # 店铺分组时"一组"就是一家店：逐店状态已经在 `_record_statuses` 里发过了，
        # 再发一遍就是同一个 (店铺, 指标) 两个说法——载荷校验会直接拒掉重复键。
        return
    label = COMPARISON_GROUP_COLUMN[runtime_group_by(runtime)]
    for key, columns in sorted(facts.items()):
        for column, item in columns.items():
            if column not in COMMERCE_METRICS:
                continue     # 证据列不是报告指标：它的原因跟着带它进来的那个指标走
            entry: dict[str, Any] = {label: _group_label(runtime, key),
                                     "metric": column}
            reason = item.reason
            if item.answerable and item.value is not None:
                entry["status"] = "available"
            elif reason is not None and (reason.startswith("capability")
                                         or reason == "source_unregistered"):
                entry.update(status="unsupported", reason=reason)
            elif reason == "basis_incompatible":
                entry.update(status="incomparable", reason=reason)
            else:
                entry.update(status="missing",
                             reason=reason or "comparison_cell_withheld")
            runtime.statuses.append(entry)


def _group_statuses(runtime: CommerceRuntime) -> None:
    """逐分组状态：没发布的分组为什么没数，与逐店缺口分开说。"""
    evaluated = set(runtime.candidate_shop_ids)
    label = COMPARISON_GROUP_COLUMN[runtime_group_by(runtime)]
    for key, shops in sorted(runtime.requested_groups.items()):
        kept = [shop for shop in shops if shop in evaluated]
        entry: dict[str, Any] = {label: _group_label(runtime, key),
                                 "shops_requested": len(shops),
                                 "shops_evaluated": len(kept),
                                 "status": "complete" if len(kept) == len(shops)
                                 else "partial"}
        if entry["status"] != "complete":
            entry["reason"] = "commerce_scope_excluded"
        runtime.group_statuses.append(entry)
    dropped = sorted(key for key, shops in runtime.requested_groups.items()
                     if key not in runtime.published_groups
                     and any(shop in evaluated for shop in shops))
    if dropped:
        # 只点"一组里评估了几家"这种部分发布：整组都没评估的店已经在 excluded_scope
        # 与上一句家数披露里说过一遍，再列一次就是同一件事说两遍。
        runtime.limitations.append(
            f"{len(dropped)} 个分组仍有获准店铺未被评估，不发布该分组数字："
            + "、".join(dropped))


def _comparison_totals(runtime: CommerceRuntime, columns: tuple[str, ...],
                       facts: Mapping[str, Mapping[str, ColumnFacts]]) -> None:
    """合计行与逐指标排名：两者共用同一个"完整且同口径"判定。

    名次与合计必须一起成立或一起不成立：一处给"第一"、另一处说"集合不完整"，
    模型就会把那个第一抄进正文。
    """
    request = runtime.request
    assert isinstance(request, PerformanceComparisonRequest)
    runtime.diagnostics["comparison_groups"] = {
        "groups_requested": len(runtime.requested_groups),
        "groups_published": len(runtime.published_groups),
        "publishable": "true" if runtime.published_groups and len(
            runtime.published_groups) == len(runtime.requested_groups) else "false"}
    if not runtime.published_groups:
        return
    totals: dict[str, Any] = {}
    withheld = 0
    for column in columns:
        per_group = {key: item[column] for key, item in facts.items()}
        answers = {key: item for key, item in per_group.items() if item.answerable}
        signatures = {item.signature for item in answers.values()}
        valued = {key: item for key, item in answers.items() if item.value is not None}
        complete = (len(answers) == len(runtime.published_groups)
                    and len(signatures) == 1
                    and len(valued) == len(runtime.published_groups))
        incomparable = bool(answers) and len(signatures) > 1
        totals[column] = (_total_value(runtime, column, per_group) if complete else None)
        if not complete:
            withheld += 1
        if column in COMPARISON_RANKED_COLUMNS:
            # 两份单据计数不是"可比较的指标"：它们进合计行，不进排名块
            # （排名块的 metric 必须是已登记报告指标，见 runtime.models._ranking）。
            runtime.ranking.append(_ranking_block(runtime, column, per_group,
                                                 complete=complete,
                                                 incomparable=incomparable))
    runtime.totals.append(project(columns, totals))
    if withheld:
        runtime.limitations.append(
            f"{withheld} 个指标的合计已拒答（有分组拿不出该指标的值或口径不一致）")


def _total_value(runtime: CommerceRuntime, column: str,
                 per_group: Mapping[str, ColumnFacts]) -> Any:
    """合计列取值：可加的就加；均价与毛利率从**全部已发布分组**的原始行重算。

    绝不平均分组均价，也绝不在这里重算单据与支付面之外的东西（spec §5.2）。
    """
    request = runtime.request
    assert request is not None
    if column in RATIO_VALUE_COLUMNS:
        shops = [shop for group in runtime.published_groups.values() for shop in group]
        return _group_values(runtime, shops, (request.start, request.end)).get(column)
    values = [to_decimal(item.value) for item in per_group.values()]
    if not values or any(value is None for value in values):
        return None
    total = sum(value for value in values if value is not None)
    return int(total) if column in COUNT_VALUE_COLUMNS else money_of(total)


def _ranking_block(runtime: CommerceRuntime, column: str,
                   per_group: Mapping[str, ColumnFacts], *, complete: bool,
                   incomparable: bool) -> dict[str, Any]:
    """一个指标的排名块。

    `complete` 才给名次；口径不一致时连值也不发（那些值分属两个口径，把它们排在
    一起本身就是一种汇总）；集合不完整时值照发、名次留 null，缺的分组列在
    `missing` 里带原因，不从排名里静默消失。
    """
    label = COMPARISON_GROUP_COLUMN[runtime_group_by(runtime)]
    block: dict[str, Any] = {"metric": column, "ranking_scope": "evaluated_only",
                             "rows": [], "missing": []}
    if incomparable:
        block["status"] = "incomparable"
        block["reason"] = "basis_incompatible"
        block["rows"] = [{label: _group_label(runtime, key), "value": None, "rank": None}
                         for key in sorted(per_group)]
        return block
    answers = [key for key, item in per_group.items() if item.answerable]
    block["status"] = "complete" if complete else "incomplete"
    if complete:
        basis = {key: getattr(per_group[key], "basis") for key in answers}
        entry = next(iter(basis.values()))
        block["basis"] = entry["basis"]
        block["time_basis"] = entry["time_basis"]
    rows = [{label: _group_label(runtime, key), "value": per_group[key].value,
             "rank": None} for key in answers]
    if complete:
        rows = rank_groups(rows, "value")
    block["rows"] = rows
    missing = [{label: _group_label(runtime, key),
                "reason": per_group[key].reason}
               for key in sorted(per_group)
               if not per_group[key].answerable or per_group[key].value is None]
    withheld_cells = any(item["reason"] == "comparison_cell_withheld" for item in missing)
    if withheld_cells and CELL_WITHHELD_TEXT not in runtime.limitations:
        # 每个指标都会各自判一次缺格；这句说的是同一件事，只说一遍。
        runtime.limitations.append(CELL_WITHHELD_TEXT)
    block["missing"] = missing
    if not block["missing"]:
        block.pop("missing")
    return block


def build_comparison_and_trend(runtime: CommerceRuntime) -> None:
    plan = runtime.plan
    request = runtime.request
    assert plan is not None and request is not None
    if runtime.report_kind == "comparison":
        _build_group_trend(runtime)
        return
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


def _build_group_trend(runtime: CommerceRuntime) -> None:
    """对比面的七日序列：一行 = (分组, 日)，与商品面同一条 null 规则。

    分组那一天只要有一家成员店未被覆盖，整组那天就是 null —— 给一个"部分店铺的
    当日合计"会被读成整组那天只卖了这么多；折线图因此在那里断开，而不是连过去。
    """
    request = runtime.request
    plan = runtime.plan
    assert isinstance(request, PerformanceComparisonRequest) and plan is not None
    if not plan.group_facet:
        return
    window = runtime.trend_window
    gaps = _window_gaps(runtime, window)
    # 两份列形只是分组键不同（`shop_id` / `platform`），其余同形：选一份，不各写一遍。
    declared = (COMPARISON_TREND_SHOP_ROWS if runtime.group_column == "shop_id"
                else COMPARISON_TREND_PLATFORM_ROWS)
    uncovered_groups: set[str] = set()
    zeros = 0
    for key, shops in sorted(runtime.published_groups.items()):
        for day in _days(window):
            covered = all(_is_covered(shop, day, gaps) for shop in shops)
            if not covered:
                uncovered_groups.add(key)
                values = {"sold_quantity": None, "sales_amount": None}
            else:
                # 覆盖成立那天没有行 = 真实零成交：`_group_product_values` 同一规则给 0，
                # 不在这里再写第二份"什么算 0"。
                values = _group_values(runtime, shops, (day, day + timedelta(days=1)))
                values = {name: values.get(name) for name in ("sold_quantity",
                                                              "sales_amount")}
                if values["sold_quantity"] == "0":
                    zeros += 1
            runtime.group_trend_rows.append(project(
                declared, {runtime.group_column: key, "day": day.isoformat(), **values}))
    if uncovered_groups:
        runtime.limitations.append("趋势窗口覆盖不足，缺失日按 null 单独留 gap，"
                                   "不滑到另一组七天")
    if zeros:
        runtime.limitations.append(f"七日趋势含 {zeros} 天真实零成交，与缺失日分列")


def _plan_charts(runtime: CommerceRuntime) -> None:
    """把"哪些指标该画图"定下来；引用与版本要到发布节点拿真实落库引用。

    只为**排名完整**（同口径、每个已发布分组都有值）的指标画柱状图：一张把不可比
    分组画在一起的柱状图，就是把 spec §8 明令禁止的那次汇总画给用户看。
    折线图为趋势里的每个指标画一张，系列 = 分组键；缺日断口由 null 承担。
    """
    request = runtime.request
    assert isinstance(request, PerformanceComparisonRequest)
    group_column = COMPARISON_GROUP_COLUMN[request.group_by]
    for block in runtime.ranking:
        metric = str(block["metric"])
        if block.get("status") != "complete" or metric not in COMMERCE_METRICS:
            continue
        if metric not in {str(item) for item in request.metrics}:
            continue      # 证据列（单据数等）不是报告指标：不进轴
        runtime.chart_plans.append(ChartPlan(
            dataset_type="comparison_table", kind="bar", x=group_column, y=metric,
            series=(group_column,),
            basis_entry=f"{metric}|{block['basis']}|{block['time_basis']}"))
    trend_metrics = [key for key in ("sold_quantity", "sales_amount")
                     if runtime.group_trend_rows
                     and key in {str(metric) for metric in request.metrics}]
    for metric in trend_metrics:
        basis = next((block for block in runtime.ranking
                      if str(block.get("metric")) == metric), None)
        if basis is None or basis.get("status") not in {"complete", "incomplete"}:
            continue
        # 趋势图的口径取该指标本轮唯一导出过的签名：不一致时前面已经判成
        # incomparable 并跳过，所以这里只会拿到一份一致的签名。
        signature = _trend_chart_basis(runtime, metric)
        if signature is None:
            continue
        runtime.chart_plans.append(ChartPlan(
            dataset_type="trend_series", kind="line", x="day", y=metric,
            series=(group_column,), basis_entry=signature))
    if not runtime.chart_plans:
        runtime.limitations.append(CHART_UNAVAILABLE_TEXT)


def _trend_chart_basis(runtime: CommerceRuntime, metric: str) -> str | None:
    """趋势图那一列的口径签名：成员店铺的签名必须处处一致。"""
    signatures: set[tuple] = set()
    entry: Mapping[str, str] | None = None
    for shops in runtime.published_groups.values():
        basis = _group_basis(runtime, shops, metric)
        if basis is None:
            return None
        signatures.add(basis[1])
        entry = basis[0]
    if len(signatures) != 1 or entry is None:
        return None
    return f"{metric}|{entry['basis']}|{entry['time_basis']}"


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
    """分类节点：商品面出份额与候选，对比面出排名与图表计划。

    两条分支最后都汇到同一个发布判定上：能发多少、是不是 partial，不该由报告种类
    决定两套标准。
    """
    if runtime.report_kind == "comparison":
        _classify_comparison(runtime)
    else:
        _classify_product(runtime)
    _classify_publication(runtime)


def _classify_product(runtime: CommerceRuntime) -> None:
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


def _classify_comparison(runtime: CommerceRuntime) -> None:
    """对比面的发现：排名已在聚合节点按"完整且同口径"判过，这里只补两件事。

    1. 份额本轮不算：它的分母就是那张合计行，合计没发布时去凑一个
       "占已发布分组的百分比"就是一个新造出来的口径；
    2. 图表计划：只给同口径且每个已发布分组都有值的指标出图。

    （“淘系不合并”那句分组规则披露在 `_plan_groups` 里已经说过，不在这里重复一遍。）
    """
    request = runtime.request
    assert isinstance(request, PerformanceComparisonRequest)
    if runtime.rows and runtime.excluded:
        runtime.limitations.append(
            "不含分组键的行是已发布分组的合计，不等于全部获准范围合计")
    _plan_charts(runtime)


def _classify_publication(runtime: CommerceRuntime) -> None:
    datasets = _datasets(runtime)
    runtime.result = datasets[0].result if datasets else None
    partial = bool(runtime.excluded or runtime.incomparable_metrics
                   or _statuses_all_missing(runtime)
                   or any(item.get("status") != "complete"
                          for item in runtime.group_statuses))
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
    """商品报告三份数据集：商品面、七日序列、店铺级单据 / 支付面。

    每份都自带覆盖与口径凭证；趋势那份的覆盖是**趋势窗口**的覆盖，不与主期间混用。
    """
    request = runtime.request
    plan = runtime.plan
    if request is None or plan is None:
        return []
    if runtime.report_kind == "comparison":
        return _comparison_datasets(runtime)
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


def _comparison_datasets(runtime: CommerceRuntime) -> list[CommerceDataset]:
    """对比报告两份数据集：分组对比表（含合计行）与分组七日序列。

    对比表是本轮的**必需**数据集，图表与兜底摘要都从它出发；趋势那份的覆盖是趋势
    窗口的覆盖，与主期间不混用（与商品面同一条规则）。
    """
    request = runtime.request
    assert isinstance(request, PerformanceComparisonRequest)
    coverage = Coverage(status="complete", start=request.start, end=request.end,
                        gaps=[])
    datasets: list[CommerceDataset] = []
    if runtime.rows or runtime.totals:
        datasets.append(CommerceDataset(
            artifact_type="comparison_table",
            result=_tool_result(runtime, [*runtime.rows, *runtime.totals], coverage,
                                _dataset_limitations(runtime, "comparison_table"),
                                _definitions_of([*runtime.rows, *runtime.totals]))))
    if runtime.group_trend_rows:
        gaps = sorted({f"{start.isoformat()}~{end.isoformat()}"
                       for windows in _window_gaps(runtime, runtime.trend_window).values()
                       for start, end in windows})
        datasets.append(CommerceDataset(
            artifact_type="trend_series",
            result=_tool_result(
                runtime, runtime.group_trend_rows,
                Coverage(status="complete" if not gaps else "partial",
                         start=runtime.trend_window[0], end=runtime.trend_window[1],
                         gaps=gaps),
                _dataset_limitations(runtime, "trend_series"),
                _definitions_of(runtime.group_trend_rows))))
    return datasets


def _definitions_of(rows: Sequence[Mapping[str, Any]]) -> dict[str, str]:
    """本轮行里出现过的列 → 已登记口径文本：没登记过定义的列不写进 metric_definition。

    口径文本是持久化契约的一部分（`runtime.models` 逐字比对），所以这里只能从
    两张定义字典里取，不能为新增的证据列现编一句说明。
    """
    names = sorted({key for row in rows for key in row
                    if key in COMMERCE_METRIC_DEFINITIONS
                    or key in METRIC_DEFINITIONS})
    return {name: {**METRIC_DEFINITIONS, **COMMERCE_METRIC_DEFINITIONS}[name]
            for name in names}


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
    """建一次目录：引用与展示名的唯一换面处，越权店铺在这里直接失败。

    对比报告的平台行不带 `shop_id`（行里只有平台码），但范围与缺口仍然是按店说的：
    所以每一家已评估店铺都要进目录，不然 `evaluated_scope` 换引用时会当场
    `shop_not_authorized`——那不是授权问题，而是投影材抖少了。
    """
    rows = list(runtime.rows)
    seen = {str(row.get("shop_id")) for row in rows}
    names = _name_columns(runtime)
    for shop in runtime.candidate_shop_ids:
        if shop in seen:
            continue
        row: dict[str, Any] = {"shop_id": shop}
        if runtime.erp_product_id is not None:
            row["product_id"] = runtime.erp_product_id
            row.update(names)
        rows.append(row)
    if not rows and runtime.erp_product_id is not None:
        rows.append({"product_id": runtime.erp_product_id, **names})
    source = ToolResult(status="ok", data=rows, filters=_filters(runtime),
                        coverage=runtime.state.coverage
                        or Coverage(status="complete", start=date(1970, 1, 1),
                                    end=date(1970, 1, 2), gaps=[]))
    runtime.catalog = build_catalog(runtime.context.conn, source,
                                    allowed_shop_ids=runtime.context.allowed_shop_ids)


def persist_artifacts(runtime: CommerceRuntime, store: Any) -> None:
    """先存数据集、再存图表；必需数据集存不下就是 failed，图表存不下只降级。

    顺度不是风格：图表只能引用**已经落库**的数据集，反过来先存图表就会需要引用
    一个还不存在的 Artifact id。spec §7 对两种失败给的不是同一个处置：
    必需结果存不下就不发布成功，可选的 chart_spec 存不下仍保留已存下的表格。
    """
    if runtime.catalog is None or runtime.report is None:
        _stop(runtime, kind="unavailable", code="result_contract_violation",
              problems=["result_contract_violation"],
              stage=CommerceNode.PERSIST_ARTIFACTS, message="查询结果异常。")
        return
    from .tool import project_dataset

    refs: list[Any] = []
    published: list[tuple[Any, dict[str, Any]]] = []
    persisted: dict[str, PersistedDataset] = {}
    try:
        for dataset in runtime.report.datasets:
            payload = project_dataset(dataset, runtime.catalog, runtime.report)
            coverage = dataset.result.coverage
            ref = store.save_artifact(runtime.state.run_id, NewArtifact(
                artifact_type=dataset.artifact_type, payload=payload,
                data_as_of=dataset.result.data_as_of,
                coverage=coverage.model_dump(mode="json")))
            refs.append(ref)
            published.append((ref, payload))
            if dataset.result.data_as_of is not None:
                persisted[dataset.artifact_type] = PersistedDataset(
                    artifact_id=ref.id, artifact_type=ref.type,
                    data_as_of=dataset.result.data_as_of, coverage=coverage)
    except Exception:  # noqa: BLE001 - 保存失败与契约违规都不许变成成功，也不许带出原文
        runtime.published = []
        _stop(runtime, kind="unavailable", code="artifact_persistence_failed",
              problems=["artifact_persistence_failed"],
              stage=CommerceNode.PERSIST_ARTIFACTS,
              message="结果保存失败，请稍后重试。")
        return
    saved = _persist_charts(runtime, store, persisted)
    for ref, payload in saved:
        refs.append(ref)
        published.append((ref, payload))
    runtime.published = published
    if len(saved) < len(runtime.chart_plans):
        # 有图表没存住：这句降级要补进每一份数据集。已落库的 Artifact 载荷**不改**（它们
        # 存的是当时按当时证据发布的那一份），但模型与展示层必须看见"图没出来，只剩表格"
        # ——否则缺图会被读成"本轮没有可画的数"，那是另一件没发生过的事。
        for dataset in runtime.report.datasets:
            if CHART_UNAVAILABLE_TEXT not in dataset.result.limitations:
                dataset.result.limitations.append(CHART_UNAVAILABLE_TEXT)
    runtime.state = runtime.state.model_copy(update={"artifact_refs": refs})


def _persist_charts(runtime: CommerceRuntime, store: Any,
                    persisted: Mapping[str, PersistedDataset]
                    ) -> list[tuple[Any, dict[str, Any]]]:
    """可选图表：一张存不下只拿掉那一张，不连带抖掉已经存下的表格。

    为什么允许部分失败：图表不增加任何业务事实，它只是同一份已落库数据的另一种读法；
    而表格是必需结果，它存不下已经是上面那条 `artifact_persistence_failed` 分支了。
    """
    saved: list[tuple[Any, dict[str, Any], int]] = []
    for plan in runtime.chart_plans:
        dataset = persisted.get(plan.dataset_type)
        if dataset is None:
            continue        # 被引用的那一份本轮没发布（也没落库）：无引用可建
        try:
            spec = build_chart_spec(
                dataset, kind=plan.kind, x=plan.x, y=plan.y, series=plan.series,
                unit=CHART_METRIC_UNITS[plan.y], metric_basis=plan.basis_entry,
                coverage_ref=dataset)
            payload = spec.as_payload()
            ref = store.save_artifact(runtime.state.run_id, NewArtifact(
                artifact_type="chart_spec", payload=payload,
                data_as_of=dataset.data_as_of, coverage=payload_coverage(dataset),
                dataset_ref=dataset.artifact_id, chart_version=CHART_SPEC_VERSION))
        except Exception:  # noqa: BLE001 - 契约不过或存不下去都只拿掉这一张图
            # 图表不可渲染是 warning：保留已验证表格，不得抖掉业务结果（spec §7）。
            # 不区分异常种类也不带原文：原因码表里没有"图表为什么没成"这一类，
            # 把原文发出去就会变成给用户看的第二份说法。
            if CHART_UNAVAILABLE_TEXT not in runtime.limitations:
                runtime.limitations.append(CHART_UNAVAILABLE_TEXT)
            continue
        saved.append((ref, payload))
    return saved


def payload_coverage(dataset: PersistedDataset) -> dict[str, Any]:
    """图表 Artifact 的覆盖列：逐字拿被引用数据集那一份，不重新算一遍。"""
    return dataset.coverage.model_dump(mode="json")


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


def run_commerce_graph(*, report_kind: str, request: CommerceRequest,
                       context: DomainContext, tool_call_id: str,
                       arguments: Mapping[str, Any] | None = None,
                       arguments_error: str | None = None) -> CommerceExecution:
    """执行一次经营图：`report_kind` = `product`（商品报告）| `comparison`（对比报告）。

    入参与报告种类必须匹配：拿商品请求去跑对比报告会发出一份“看起来是对比”的
    商品表，所以这里直接拒绝，不让它退化。
    """
    expected = {"product": ProductPerformanceRequest,
                "comparison": PerformanceComparisonRequest}.get(report_kind)
    # `request is None` 是参数解析失败那一途：它仍要走图（needs_input 要留下运行记录与
    # 终止原因），所以只有"带来了错类型的请求"或"没登记的报告种类"才在这里拒掉。
    if expected is None or (request is not None and not isinstance(request, expected)):
        raise UnsupportedReportKind(report_kind)
    return _execute(context, report_kind=report_kind, request=request,
                    tool_call_id=tool_call_id, arguments=arguments,
                    arguments_error=arguments_error)


def _execute(context: DomainContext, *, report_kind: str,
             request: CommerceRequest | None,
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
        tool_call_id=tool_call_id, request=request, report_kind=report_kind)
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
