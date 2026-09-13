"""Public models and safe persistence contracts for deterministic query runs."""

from __future__ import annotations

import re
from datetime import date, datetime
from enum import StrEnum
from typing import Annotated, Literal, Protocol, get_args
from uuid import UUID

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    field_validator,
    ValidationError,
    model_validator,
)

from bi_agent.catalog import EntityKind, REF_RE, is_safe_display_name
from bi_agent.commerce.metrics import (
    CHART_SERIES_COLUMNS, CHART_X_COLUMNS, CHART_Y_COLUMNS,
    COMMERCE_LIMITATION_CODES, COMMERCE_LIMITATION_PATTERNS,
    COMMERCE_METRIC_DEFINITIONS, COMMERCE_METRIC_UNITS, COMMERCE_METRICS,
    COMMERCE_PUBLIC_LIMITATIONS, COMMERCE_RESULT_COLUMNS, EXCLUDED_REASONS,
    GROUP_STATUS_VALUES, METRIC_STATUS_VALUES, OPPORTUNITY_STATUSES,
    PLATFORM_GROUP_CODES, RANKING_SCOPES, RANKING_STATUSES, TREND_DAYS)
from bi_agent.listing_audit.rules import (
    APPLIES_TO_VALUES, AUDIT_STATUSES, LISTING_DATETIME_RESULT_COLUMNS,
    LISTING_LABEL_RESULT_VALUES,
    LISTING_LIMITATION_CODES, LISTING_LISTING_REF_RESULT_COLUMNS,
    LISTING_NUMERIC_RESULT_COLUMNS, LISTING_PUBLIC_LIMITATIONS,
    LISTING_REF_RE, LISTING_REF_RESULT_COLUMNS,
    LISTING_RESULT_COLUMNS, LISTING_SOURCE_KINDS, MAX_ROSTER_ITEMS, PRICE_BASES)  # noqa: E501
from bi_agent.metrics import Coverage, METRIC_DEFINITIONS
from bi_agent.presentation.charts import (
    CHART_BASELINES, CHART_KINDS, CHART_NULL_HANDLES, CHART_PAYLOAD_KEYS,
    CHART_REQUIRED_PAYLOAD_KEYS, CHART_SERIES_COLUMNS, CHART_SPEC_NAME,
    CHART_UNITS, CHART_X_COLUMNS, CHART_Y_COLUMNS)
from bi_agent.promotion import (
    PROMOTION_DATE_RESULT_COLUMNS,
    PROMOTION_LABEL_VALUES,
    PROMOTION_LIMITATION_PATTERNS,
    PROMOTION_METRIC_DEFINITIONS,
    PROMOTION_MODE_VALUES,
    PROMOTION_NUMERIC_RESULT_COLUMNS,
    PROMOTION_PUBLIC_LIMITATIONS,
    PROMOTION_RESULT_COLUMNS,
)

from .artifacts import (
    BASIS_SIGNATURE_RE,
    CHART_SPEC_VERSION,
    DATASET_ARTIFACT_TYPES,
    QueryProvenance,
    RequestIdentity,
    TERMINATION_REASONS,
)
from .domain_registry import (
    ARTIFACT_TYPES,
    COMMERCE_NODES,
    allows_artifact_type,
    known_domain,
    spec_for,
)
PersistenceNode = Literal[
    "received", "resolve_parameters", "validate_parameters", "authorize_scope",
    "execute_fixed_query", "classify_result", "persist_artifact", "finalize",
    # 运营图（spec §6）：商品经营报告走这一串节点。节点名与
    # `runtime.domain_registry` 里该领域的白名单由测试逐项比对，不两边各拄。
    "resolve_scope", "resolve_product_if_needed", "resolve_metric_basis",
    "check_capabilities_and_coverage", "freeze_versions", "plan_fixed_queries",
    "execute_aggregates", "compute_metrics", "build_comparison_and_trend",
    "classify_findings", "persist_artifacts",
    # 上架复核图（计划 Task 9、spec §6）：目标价只来自本轮，判定分母是期望 roster。
    # 链上每一格都对应一个可归因的证据问题，合并节点就等于合并归因。
    "resolve_scope_product_and_skus", "load_expected_listing_roster",
    "capture_user_expected_prices", "check_listing_source", "load_listing_snapshot",
    "verify_completeness_and_freshness", "join_expected_and_actual",
    "compare_decimal_prices", "classify_discrepancies", "persist_audit",
]
ErrorCode = Literal[
    "missing_parameters", "invalid_parameters", "forbidden", "deadline_exceeded",
    "unavailable", "result_contract_violation", "artifact_persistence_failed",
    "invalid_transition", "transient_source_failure",
]
ProblemCode = Literal[
    "missing_parameters", "invalid_parameters", "invalid_date_range", "invalid_metric",
    "invalid_group_by", "invalid_compare", "invalid_top_n", "invalid_shop", "forbidden",
    "deadline_exceeded", "unavailable", "result_contract_violation",
    "artifact_persistence_failed", "invalid_transition", "transient_source_failure",
]
PublicMessage = Literal[
    "本次查询时间预算已耗尽，请缩小日期或店铺范围后重试。",
    "所查时间段的数据覆盖不足，可按建议窗口查询或等待回填完成。",
    "该店铺的数据来源尚未开通，调整日期范围不会补上这段数据。",
    "本次查询的指标能力尚未开通，换成已开通的指标或先完成来源核验后再查。",
    "该来源的付款时间口径尚未完成对照取证，不能按完整支付窗口出数。",
    "这些范围的统计口径不兼容，不能汇总或比较；请按店铺分列后逐组查看。",
    "来源质量核验未通过，暂时不能出数。",
    "查询参数无效",
    "查询参数无效，请调整后重试。",
    "缺少查询参数，请补充后重试。",
    "查询范围无权限。",
    "查询暂不可用。",
    "查询暂不可用，请稍后重试。",
    "查询已超时，请稍后重试。",
    "查询结果异常。",
    "结果保存失败，请稍后重试。",
]

_METRICS = frozenset(METRIC_DEFINITIONS) | COMMERCE_METRICS
# 口径文案字典：固定指标 + 推广口径 + 商品运营参考指标（均以生产方模块为真源）。
_METRIC_DEFINITION_TEXTS: dict[str, str] = {
    **METRIC_DEFINITIONS, **PROMOTION_METRIC_DEFINITIONS,
    **COMMERCE_METRIC_DEFINITIONS}
_TOOL_STATUSES = frozenset({
    "ok", "missing_data", "invalid_parameters", "forbidden", "unavailable",
})
_PUBLIC_PAYLOAD_STATUSES = _TOOL_STATUSES | frozenset({"needs_input", "partial", "failed"})
_RUN_STATUSES = frozenset({
    "running", "succeeded", "needs_input", "missing_data", "partial", "failed",
})
_DOMAIN_STATUSES = frozenset({"success", "needs_input", "missing_data", "partial", "failed"})
_GROUP_BY = frozenset({"total", "day", "shop", "product", "platform"})
_COMPARE = frozenset({"none", "previous_period"})
# 商品运营（契约 v2）的枚举：范围、逐指标状态与口径选择。
# 取值集合由生产方（commerce.metrics）供给，这里只引用不再手抄。
_SCOPE_MODES = frozenset({"all_authorized", "selected"})   # 与 ProductScope.mode 同取值
_METRIC_STATUS_VALUES = METRIC_STATUS_VALUES
_METRIC_UNITS = frozenset(COMMERCE_METRIC_UNITS.values())
_SALES_BASES = frozenset({"erp_effective_parent", "verified_payment"})
_PROFIT_BASES = frozenset({"none", "existing_fields"})
_REPORT_KINDS = frozenset({"product", "comparison"})
_RANKING_SCOPES = RANKING_SCOPES
_OPPORTUNITY_STATUSES = OPPORTUNITY_STATUSES
# 排除原因取自注册表词表；逐指标状态的原因取自限制码表
# （见下方 _STATUS_REASONS），不新造第三种说法。
_EXCLUDED_REASONS = EXCLUDED_REASONS
_COVERAGE_STATUSES = frozenset({"complete", "partial", "missing"})
_PROBLEM_CODES = frozenset({
    "missing_parameters", "invalid_parameters", "invalid_date_range", "invalid_metric",
    "invalid_group_by", "invalid_compare", "invalid_top_n", "invalid_shop", "forbidden",
    "deadline_exceeded", "unavailable", "result_contract_violation",
    "artifact_persistence_failed", "invalid_transition",
})
# 009 的 SQL CHECK 必须同步这份码表；测试比对，不两边各写。
_LIMITATION_CODES = frozenset({
    "coverage_incomplete", "data_as_of_unknown", "shop_not_synced", "shops_inactive",
    "comparison_coverage_incomplete", "deadline_exceeded", "query_timeout", "forbidden",
    "result_too_large", "cohort_rate_not_computable",
    # 覆盖与质量是两件事：缺数据与对账未通过必须分开归因。
    "source_quality_failed", "source_quality_unverified",
    # 支付额未进商品维度：金额与成因由确定 SQL 产生，必须可归因不可自创。
    "revenue_not_attributed",
    "source_not_onboarded",
    # 指标能力门禁：来源存在但逐指标能力未授予，与缺覆盖不同类。
    "capability_unavailable",
    # 付款时间口径未认证：等回填不会解决，与缺覆盖分开归因。
    "coverage_time_basis_unverified",
    # 可量化限制（设计 §5）：披露而非拒答，三条各自归因。
    "unmatched_refunds", "matched_cohort_only", "unverified_payments",
    # 跨口径汇总/比较被拒：口径不兼容是参数范围问题。
    "basis_incompatible",
    # 商品运营面（Task 7）：范围、成本覆盖、单据口径与候选排序各自归因。
    *COMMERCE_LIMITATION_CODES,
    # 上架复核（Task 9）：来源 / 快照 / 时效 / 枚举 / 本轮目标价 / 业务发现各自归因。
    *LISTING_LIMITATION_CODES,
})
# 逐指标状态的原因只能用已登记的限制码：状态与限制共用一份词表，
# 不然同一个缺口会在两处各起一个名字。
_STATUS_REASONS = _LIMITATION_CODES | _EXCLUDED_REASONS
# 披露文本里的金额片段：与 _DECIMAL_RE 同一形式，不另加一套数字规则。
_MONEY = r"(?:0|[1-9][0-9]*)(?:\.[0-9]+)?"

_PUBLIC_LIMITATIONS = frozenset({
    "店铺不在授权范围",
    "本次查询时间预算已耗尽",
    "查询超时",
    "店铺尚未同步，无法查询",
    "部分店铺已停用，仅返回剩余范围",
    "所选店铺均已停用，无法查询",
    "覆盖未完成，拒绝部分汇总；缺口见coverage.gaps",
    "数据截止未知（回填未完成）",
    "来源质量核验未通过，拒绝出数",
    "来源质量未核验（尚无对账记录）",
    "上期覆盖不足，无法比较，仅返回绝对值",
    "比较仅支持total/shop分组",
    "同批支付额为0或无支付，同批退款率不可计算",
}) | PROMOTION_PUBLIC_LIMITATIONS | COMMERCE_PUBLIC_LIMITATIONS \
    | LISTING_PUBLIC_LIMITATIONS
_PUBLIC_LIMITATION_PATTERNS = (
    re.compile(r"^存在[0-9]+条未匹配的平台成功退款，退款归属未确认$"),
    re.compile(r"^结果超过[0-9]+组，请缩小日期范围或店铺范围$"),
    # 行数上限是合法降级提醒，不登记就会被契约校验拒掉并误报成 result_contract_violation。
    re.compile(r"^结果行数达到[0-9]+上限，已拒绝出数以避免静默截断；"
               r"请缩小日期范围或店铺范围$"),
    # 来源未开通：家数可变，其余文字固定；与“覆盖有缺口”不同类，不能混因。
    re.compile(r"^[0-9]+ 家店铺的来源尚未开通（未授权或未同步），"
               r"缩小日期范围不会补上这段数据$"),
    # 能力门禁：家数与指标名可变（只能是已登记的指标名），其余文字固定。
    re.compile(r"^[0-9]+ 家店铺缺少 [a-z_、]+ 的已核验能力，未执行金额查询$"),
    # 口径不兼容：指标名可变（只能是已登记的指标名），其余文字固定。
    re.compile(r"^[0-9]+ 家店铺的上期与本期数据来源不同，不能按增长比较$"),
    re.compile(r"^这些指标在本次范围内口径互不兼容：[a-z_、]+；"
               r"请按店铺分列后逐组查看，不能汇总或比较$"),
    # 可量化限制：数字可变，句式固定（金额片段与 _MONEY 同源）。
    re.compile(r"^退款归属未确认：未匹配[0-9]+条/共[0-9]+条，金额" + _MONEY
               + r"元，比例(?:[0-9]+(?:\.[0-9]+)?%|未知)$"),
    re.compile(r"^同批退款率仅含已匹配退款（[0-9]+条未匹配退款无法归属，未计入）$"),
    re.compile(r"^未认证支付[0-9]+笔（金额未定[0-9]+笔），已知原始金额" + _MONEY
               + r"元（[0-9]+笔）$"),
    # 付款时间口径未认证：家数可变，两种后果（拒答 / 披露）各一句固定文本。
    re.compile(r"^[0-9]+ 家店铺的付款时间口径未经认证，未执行金额查询$"),
    re.compile(r"^[0-9]+ 家店铺的结果来自未认证付款时间口径，按可观测样本披露$"),
    # 商品归属披露：四个分项必现（缺项就是给猜测留空间），金额形式与 _DECIMAL_RE 同源。
    re.compile(
        r"^支付额中" + _MONEY + r"元未计入商品维度"
        r"（关闭订单行" + _MONEY + r"元；赠品行" + _MONEY + r"元；"
        r"无商品归属" + _MONEY + r"元；其他" + _MONEY + r"元）$"),
    *PROMOTION_LIMITATION_PATTERNS,
    *COMMERCE_LIMITATION_PATTERNS,
)
_STATE_KEYS = frozenset({
    "run_id", "node", "status", "revision", "normalized_request", "problems",
    "tool_status", "target_status", "coverage", "data_as_of", "limitations",
    "artifact_refs", "error",
})
_EVENT_KEYS = frozenset({
    "problem_codes", "tool_status", "target_status", "coverage_status", "data_as_of",
    "limitation_codes", "artifact_refs", "result_count", "basis_codes",
})
_NORMALIZED_REQUEST_KEYS = frozenset({
    "shop_refs", "metrics", "start", "end", "group_by", "compare", "top_n", "currency",
    "basis_policy",
    # 运营图新增：只进引用与业务码，商品文本不进这里（spec §3）。
    "product_ref", "sku_refs", "platforms", "scope_mode", "sales_basis",
    "profit_basis", "trend_days", "report_kind", "opportunity_policy_ref",
    # 对比报告的分组规则版本：分组规则换了就是另一个问题，旧结果不许命中。
    "platform_group_rule",
    # 上架复核（Task 9）：本轮目标价参与指纹，换价就是换问题；缺它就没有依据可言。
    "expected_prices", "price_basis", "as_of",
})
_ARTIFACT_KEYS = frozenset({
    "status", "metric_definition", "coverage", "limitations", "data_as_of", "filters",
    "data", "basis", "diagnostics", "entities", "catalog_version",
    # 契约 v2（spec §3）：范围三面、逐指标状态、趋势窗口、解析结果与恢复线索。
    # 旧 v1 记录不含这些键，仍必须可读，所以全部是可选键。
    "requested_scope", "evaluated_scope", "excluded_scope", "metric_statuses",
    "trend_window", "resolved_product", "comparison", "opportunity", "metric_units",
    "termination_reason", "candidates",
    # 对比报告（Task 8）：分组状态与“哪些分组进了排名 / 合计”。
    "group_statuses", "ranking",
    # 上架复核（Task 9）：期望项分母、判定计数、来源与时效声明。
    "audit",
})

# ---------------------------------------------------------------------------
# 图表载荷（chart_spec）的白名单：与数据集载荷是两个完全不相交的键集。
# 词表的所有者是 `presentation.charts`（它同时是唯一的构造入口），本模块只引用：
# 两边各自写一份，迟早会出现在这里能写、在那儿画不出来的列。
# ---------------------------------------------------------------------------

# UUID 串形式与 Artifact 引用同一规则（`str(UUID)` 的小写 8-4-4-4-12）。
_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
# `metric_basis` 的形状与血缘口径签名同一规则（`指标|口径|时间归属`）：
# 同一概念不拄第二份正则，也不给载荷留自由文本通道。
_METRIC_BASIS_RE = BASIS_SIGNATURE_RE
# 口径与诊断：名称形如 `platform_payment/v1`、`kuaimai-metrics/2`、`pay_time`。
# 版本段允许 `v1`、`2` 或日期式 `2026-09-12.1`：只允许纯数字会把已登记口径判成非法。
_BASIS_NAME_RE = re.compile(r"^[a-z0-9_-]+(?:/[a-z0-9][a-z0-9._-]*)?$")
_BASIS_ITEM_KEYS = frozenset({
    "shop_ref", "metric", "basis", "time_basis", "metric_version",
})
_BASIS_METRIC_KEYS = frozenset({"metric", "basis", "time_basis", "metric_version"})
_DIAGNOSIS_KEYS = frozenset({
    "unmatched_refunds", "matched_cohort_only", "unverified_payments",
    # 单据毛利的覆盖完整性：多少张单据、几张带毛利字段、能不能发布。
    "erp_document_coverage",
    # 对比报告请求了几个分组、发布了几家：让“缺了哪几个”能机器核对。
    "comparison_groups",
})
_DIAGNOSIS_FIELDS = {
    "unmatched_refunds": frozenset({
        "unmatched_count", "successful_count", "unmatched_amount", "ratio", "currency"}),
    "unverified_payments": frozenset({
        "total", "amount_undetermined", "amount_known", "known_amount"}),
    "matched_cohort_only": frozenset({"unmatched_count"}),
    "erp_document_coverage": frozenset({
        "documents", "documents_with_gross_profit", "publishable"}),
    "comparison_groups": frozenset({
        "groups_requested", "groups_published", "publishable"}),
}
# 名称只在授权展示层出现：模型载荷带上这两项就是契约违规。
_PUBLIC_ONLY_ARTIFACT_KEYS = frozenset({"entities", "catalog_version"})
_FILTER_KEYS = frozenset({
    "start", "end", "shop_refs", "metrics", "group_by", "compare", "top_n", "currency",
    "basis_policy", "mode", "sales_basis", "profit_basis", "platforms", "product_ref",
    "report_kind", "sales_share_basis",
    # 上架复核：一次复核回答的是「哪个口径、哪个时点、按哪些本轮目标价」。
    "price_basis", "as_of", "expected_prices",
})

_LINE_KINDS = frozenset({"sale", "gift", "suite", "combination", "processing"})
_CURRENCY_VALUES = frozenset({"CNY"})
# 上架复核的词表由生产方（`listing_audit.rules`）持有：列名、类别与取值集合都在
# 那一处声明并用断言自校，这里只引用。「新增列必须先声明类型」这条规则才有唯一落点。
_LISTING_REF_RESULT_COLUMNS: frozenset[str] = LISTING_LISTING_REF_RESULT_COLUMNS
_DATETIME_RESULT_COLUMNS: frozenset[str] = LISTING_DATETIME_RESULT_COLUMNS
_PRICE_BASES = frozenset(PRICE_BASES)
_AUDIT_STATUSES = frozenset(AUDIT_STATUSES)

# 结果列白名单以生产方为单一真源：推广列（含类型与可取集合）从 promotion.py 导出，
# 本模块只补充固定指标侧的列，不再手抄推广列名。
_METRIC_RESULT_COLUMNS = frozenset({
    "day", "shop_ref", "product_ref", "line_kind", "currency",
    "paid_amount", "paid_orders", "erp_documents", "aov", "refund_amount",
    "cash_difference", "cohort_refund_rate", "quantity", "product_paid_amount", "notice",
})
_REF_RESULT_COLUMNS = (frozenset({"shop_ref", "product_ref"})
                       | LISTING_REF_RESULT_COLUMNS)
# 文本列只有上限、转义与长数字主键三道限制；不放开成“任意字符串都收”。
_TEXT_RESULT_COLUMNS = frozenset({"notice"})
_DATE_RESULT_COLUMNS = frozenset({"day"}) | PROMOTION_DATE_RESULT_COLUMNS
_LABEL_RESULT_VALUES: dict[str, frozenset[str]] = {
    "line_kind": _LINE_KINDS,
    "currency": _CURRENCY_VALUES,
    # 对比报告的平台分组键：取值只能是来源注册表里登记过的平台码。
    "platform": PLATFORM_GROUP_CODES,
    **PROMOTION_LABEL_VALUES,
    # 上架复核的判定状态与价格口径：只能取 spec §5.4 / §4 的那几个值。
    **LISTING_LABEL_RESULT_VALUES,
}
_RESULT_COLUMNS = (_METRIC_RESULT_COLUMNS | PROMOTION_RESULT_COLUMNS
                   | COMMERCE_RESULT_COLUMNS | LISTING_RESULT_COLUMNS)
# 剩下的列一律按十进制/整数严格校验；推广数值列集合作为交叉校验。
# 上架复核那一段的数值列**取声明集而不是取余集**：拿余集当数值兼容，新登记一个
# 未认识的列就会默默落进"只收十进制"那一档，把任意文本当金额收下来。
_NUMERIC_RESULT_COLUMNS = ((_RESULT_COLUMNS - _REF_RESULT_COLUMNS - _DATE_RESULT_COLUMNS
                            - _TEXT_RESULT_COLUMNS - _DATETIME_RESULT_COLUMNS
                            - _LISTING_REF_RESULT_COLUMNS - LISTING_RESULT_COLUMNS
                            - frozenset(_LABEL_RESULT_VALUES))
                           | LISTING_NUMERIC_RESULT_COLUMNS)
assert _NUMERIC_RESULT_COLUMNS & PROMOTION_RESULT_COLUMNS == PROMOTION_NUMERIC_RESULT_COLUMNS
# 投影层（business_query/tool.py）复用同一份白名单，避免二次手抄漂移。
ARTIFACT_RESULT_COLUMNS = _RESULT_COLUMNS
ARTIFACT_FILTER_COLUMNS = _FILTER_KEYS
# 节点白名单直接从 `PersistenceNode` 推导：再把同一享节点集手抄一遍，
# 总有一天两份会漂移（一份在校验处，一份在类型上）。
# 各领域取哪一段由 `domain_registry` 决定，Store 在写库前按领域复核。
_NODES = frozenset(get_args(PersistenceNode))
# 引用与展示名的形式规则只定义在 bi_agent.catalog 一处，这里复用不拄写。
_REF_RE = REF_RE
# 与 catalog 同一形式：平台码是短标识，不是文本。
_PLATFORM_CODE_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,15}$")
# 版本化策略引用（如 `low-margin/1`）：与口径名同一形式。
_POLICY_REF_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}/[0-9][0-9._-]{0,15}$")
# ---------------------------------------------------------------------------
# 上架复核（Task 9）的载荷词表。取值集合全部由生产方 `listing_audit.rules` 供给，
# 本模块只引用：两边各自拄一份，迟早会出现在这里能写、在图上判不出来的状态。
# ---------------------------------------------------------------------------
_APPLIES_TO_VALUES = frozenset(APPLIES_TO_VALUES)
_LISTING_SOURCE_KINDS = frozenset(LISTING_SOURCE_KINDS)
_AS_OF_VALUES = frozenset({"latest"})
_EXPECTATION_KEYS = frozenset({"applies_to", "shop_ref", "sku_ref", "expected_amount",
                               "currency", "price_basis"})
_AUDIT_SUMMARY_KEYS = frozenset({
    "expected_items", "evaluated_items", "matched_items", "all_correct", "counts",
    "sources", "freshness_policy_seconds", "rule_version"})
_AUDIT_SUMMARY_REQUIRED = frozenset({
    "expected_items", "evaluated_items", "matched_items", "all_correct", "counts",
    "sources"})
_AUDIT_SOURCE_KEYS = frozenset({"shop_ref", "source_kind", "snapshot_at",
                                "enumeration_complete", "fresh"})
_AUDIT_SOURCE_REQUIRED = frozenset({"shop_ref", "source_kind", "enumeration_complete",
                                     "fresh"})
_ENTITY_KINDS = frozenset(kind.value for kind in EntityKind)
_NAME_SOURCES = frozenset({"archive", "trade_snapshot", "shop_profile", "unresolved"})
_ENTITY_KEYS = frozenset({"ref", "kind", "display_name", "sku_label", "name_source",
                          "platform"})
_DATE_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
_GAP_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}~[0-9]{4}-[0-9]{2}-[0-9]{2}$")
_DECIMAL_RE = re.compile(r"^-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?$")


def _unsafe_payload() -> None:
    raise ValueError("unsafe_persistence_payload")


def _mapping(value: object, *, allowed: frozenset[str],
             required: frozenset[str] = frozenset()) -> dict[str, object]:
    if not isinstance(value, dict) or not required <= value.keys() or not value.keys() <= allowed:
        _unsafe_payload()
    return value


def _string_in(value: object, allowed: frozenset[str]) -> None:
    if not isinstance(value, str) or value not in allowed:
        _unsafe_payload()


def _non_negative_int(value: object) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        _unsafe_payload()


def _positive_int(value: object) -> None:
    _non_negative_int(value)
    if value == 0:
        _unsafe_payload()


def _date_string(value: object) -> None:
    if not isinstance(value, str) or not _DATE_RE.fullmatch(value):
        _unsafe_payload()
    try:
        date.fromisoformat(value)
    except ValueError:
        _unsafe_payload()


def _datetime_string(value: object) -> None:
    if not isinstance(value, str) or "T" not in value:
        _unsafe_payload()
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        _unsafe_payload()
    if parsed.tzinfo is None:
        _unsafe_payload()


def _ref(value: object) -> None:
    """模型与展示层只允许不透明引用；ERP 店铺号与商品号一律拒收。"""
    if not isinstance(value, str) or not _REF_RE.fullmatch(value):
        _unsafe_payload()


def _listing_ref(value: object) -> None:
    """链接句柄：`(账号范围, 店, 链接, 平台 SKU)` 的单向摘要。

    它故意不是 `ent-` 引用：目录不解析它，也不把它放进 `entities`，因为渠道链接号
    本身不是可读实体名；而它也不能退回成原文，那会把一个真实渠道主键送进展示路径。
    """
    if not isinstance(value, str) or not LISTING_REF_RE.fullmatch(value):
        _unsafe_payload()


def _entities(value: object) -> None:
    if not isinstance(value, list):
        _unsafe_payload()
    seen: set[str] = set()
    for item in value:
        entity = _mapping(item, allowed=_ENTITY_KEYS,
                         required=frozenset({"ref", "kind", "name_source"}))
        _ref(entity["ref"])
        if entity["ref"] in seen:
            _unsafe_payload()
        seen.add(entity["ref"])
        _string_in(entity["kind"], _ENTITY_KINDS)
        _string_in(entity["name_source"], _NAME_SOURCES)
        for key in ("display_name", "sku_label"):
            present = entity.get(key) is not None
            if present and not is_safe_display_name(entity[key]):
                _unsafe_payload()
        if entity["name_source"] != "unresolved" and entity.get("display_name") is None:
            _unsafe_payload()
        if entity["name_source"] == "unresolved" and entity.get("display_name") is not None:
            _unsafe_payload()
        platform = entity.get("platform")
        # platform 不在 required 里：旧 Artifact 没这个字段，必须继续可读。
        # 平台码只能是不带修饰的短标识，不能当第二条自由文本通道。
        if platform is not None and (not isinstance(platform, str)
                                     or not _PLATFORM_CODE_RE.fullmatch(platform)):
            _unsafe_payload()


def _ref_or_invalid_shop(value: object) -> None:
    if value == "invalid_shop":
        return
    _ref(value)


def _string_list(value: object, validator) -> None:
    if not isinstance(value, list):
        _unsafe_payload()
    for item in value:
        validator(item)


def _platform_code(value: object) -> None:
    """平台码只能是短标识：不放开成第二条自由文本通道。"""
    if not isinstance(value, str) or not _PLATFORM_CODE_RE.fullmatch(value):
        _unsafe_payload()


def _policy_ref(value: object) -> None:
    """版本化策略引用（如 `low-margin/1`）：不认识的写法不进载荷。"""
    if not isinstance(value, str) or not _POLICY_REF_RE.fullmatch(value):
        _unsafe_payload()


def _bool_flag(value: object) -> None:
    if not isinstance(value, bool):
        _unsafe_payload()


def _decimal_or_null(value: object) -> None:
    _numeric_result(value)


def _date_pair(value: object) -> None:
    """[start,end) 的文本对：与 suggested_window 同一形状，两个 ISO 日期。"""
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        _unsafe_payload()
    for item in value:
        if not isinstance(item, str):
            _unsafe_payload()
        _date_string(item)


def _requested_scope(value: object) -> None:
    scope = _mapping(value, allowed=frozenset({"mode", "shop_refs", "platforms"}),
                     required=frozenset({"mode"}))
    _string_in(scope["mode"], _SCOPE_MODES)
    if "shop_refs" in scope:
        _string_list(scope["shop_refs"], _ref)
    if "platforms" in scope:
        _string_list(scope["platforms"], _platform_code)


def _evaluated_scope(value: object) -> None:
    scope = _mapping(value, allowed=frozenset({"shop_refs", "platforms"}),
                     required=frozenset({"shop_refs"}))
    _string_list(scope["shop_refs"], _ref)
    if "platforms" in scope:
        _string_list(scope["platforms"], _platform_code)


def _excluded_scope(value: object) -> None:
    """获准但本轮没评估的店铺：每店一个原因，缺口日期段可附。

    这里出现的一定是本轮已获准的店铺引用：越权店不会因为被排除而露出来
    （它们根本不进 evaluated/excluded 两个集合）。
    """
    if not isinstance(value, list):
        _unsafe_payload()
    seen: set[str] = set()
    for item in value:
        entry = _mapping(item, allowed=frozenset({"shop_ref", "platform", "reason",
                                                  "windows"}),
                         required=frozenset({"shop_ref", "reason"}))
        _ref(entry["shop_ref"])
        if entry["shop_ref"] in seen:
            _unsafe_payload()      # 同一家店两个说法就有一个是错的
        seen.add(str(entry["shop_ref"]))
        if "platform" in entry:
            _platform_code(entry["platform"])
        _string_in(entry["reason"], _EXCLUDED_REASONS)
        if "windows" in entry:
            if not isinstance(entry["windows"], list):
                _unsafe_payload()
            for window in entry["windows"]:
                if not isinstance(window, str) or not _GAP_RE.fullmatch(window):
                    _unsafe_payload()


def _metric_statuses(value: object) -> None:
    """逐店 / 逐平台逐指标的可评估性：可用 / 缺数据 / 不支持 / 口径不可比。

    分组键只能是 `shop_ref` 或 `platform`，事必给此其一：spec §3 把“按店铺 / 平台 /
  指标列出”当成契约要求，不带分组键的状态条目会被读成“整个报告都这样”，而它
    其实只说的是某一家店。
    """
    if not isinstance(value, list):
        _unsafe_payload()
    seen: set[tuple[str, str, str]] = set()
    for item in value:
        entry = _mapping(item, allowed=frozenset({"shop_ref", "platform", "metric",
                                                 "status", "reason"}),
                         required=frozenset({"metric", "status"}))
        if "shop_ref" in entry:
            _ref(entry["shop_ref"])
        if "platform" in entry:
            _platform_code(entry["platform"])
        if ("shop_ref" in entry) == ("platform" in entry):
            _unsafe_payload()      # 两个都给或都不给：同一个状态被说了两件不同的事
        key = (str(entry.get("shop_ref")), str(entry.get("platform")),
               str(entry["metric"]))
        if key in seen:
            _unsafe_payload()
        seen.add(key)
        if entry["metric"] not in _METRICS:
            _unsafe_payload()
        _string_in(entry["status"], _METRIC_STATUS_VALUES)
        if "reason" in entry:
            _string_in(entry["reason"], _STATUS_REASONS)


def _group_statuses(value: object) -> None:
    """对比报告的分组完整性（spec §8：无数据平台保留原因标签）。

    一个分组只能以店铺引用或平台码为键，而且只出现一次：同一个组给两个说法，
    其中一个是错的，而读者无从知道是哪一个。
    """
    if not isinstance(value, list):
        _unsafe_payload()
    seen: set[str] = set()
    for item in value:
        entry = _mapping(item, allowed=frozenset({"shop_ref", "platform", "status",
                                                 "reason", "shops_requested",
                                                 "shops_evaluated"}),
                         required=frozenset({"status"}))
        if ("shop_ref" in entry) == ("platform" in entry):
            _unsafe_payload()
        key = str(entry.get("shop_ref") or entry.get("platform"))
        if key in seen:
            _unsafe_payload()
        seen.add(key)
        if "shop_ref" in entry:
            _ref(entry["shop_ref"])
        if "platform" in entry:
            _platform_code(entry["platform"])
        _string_in(entry["status"], GROUP_STATUS_VALUES)
        if "reason" in entry:
            _string_in(entry["reason"], _STATUS_REASONS)
        for column in ("shops_requested", "shops_evaluated"):
            if column in entry:
                _non_negative_int(entry[column])
        if (entry.get("shops_evaluated") is not None
                and entry.get("shops_requested") is not None
                and entry["shops_evaluated"] > entry["shops_requested"]):
            # 评估了比获准还多家：这个分组里混进了不获准的店铺。
            _unsafe_payload()


def _ranking(value: object) -> None:
    """排名块：合计与名次只覆盖完整且同口径的分组集合（spec §3、§8）。

    三条一起成立才能叫 `complete`：同口径、每个已发布分组都有值、分组粒度单一。
    `missing` 里每家分组都带原因，不会从排名里静默消失，也不会被当成 0。
    """
    if not isinstance(value, list):
        _unsafe_payload()
    seen: set[str] = set()
    for item in value:
        block = _mapping(item, allowed=frozenset({"metric", "status", "reason",
                                                 "basis", "time_basis",
                                                 "ranking_scope", "rows", "missing"}),
                         required=frozenset({"metric", "status", "ranking_scope",
                                             "rows"}))
        if block["metric"] not in _METRICS:
            _unsafe_payload()
        if str(block["metric"]) in seen:
            _unsafe_payload()      # 一个指标两份排名就有一份是多余的说法
        seen.add(str(block["metric"]))
        _string_in(block["status"], RANKING_STATUSES)
        _string_in(block["ranking_scope"], RANKING_SCOPES)
        if "reason" in block:
            _string_in(block["reason"], _STATUS_REASONS)
        for column in ("basis", "time_basis"):
            if column in block and not (isinstance(block[column], str)
                                        and _BASIS_NAME_RE.fullmatch(block[column])):
                _unsafe_payload()
        if block["status"] == "incomparable" and "basis" in block:
            # 口径不一致时不能再贴一个口径名：那会把“多个口径”说成“一个口径”。
            _unsafe_payload()
        rows = block["rows"]
        if not isinstance(rows, list):
            _unsafe_payload()
        grouped: set[str] = set()
        for row in rows:
            entry = _mapping(row, allowed=frozenset({"shop_ref", "platform", "value",
                                                   "rank"}),
                             required=frozenset({"value", "rank"}))
            if ("shop_ref" in entry) == ("platform" in entry):
                _unsafe_payload()
            key = str(entry.get("shop_ref") or entry.get("platform"))
            if key in grouped:
                _unsafe_payload()
            grouped.add(key)
            if "shop_ref" in entry:
                _ref(entry["shop_ref"])
            if "platform" in entry:
                _platform_code(entry["platform"])
            _decimal_or_null(entry["value"])
            if entry["rank"] is not None:
                _positive_int(entry["rank"])
            if block["status"] != "complete" and entry["rank"] is not None:
                # 集合不完整 / 口径不一致却给了名次：那一个“第一”没有依据。
                _unsafe_payload()
            if entry["value"] is None and entry["rank"] is not None:
                _unsafe_payload()  # 缺值不当 0 参加排名，也不得占一个名次
        missing = block.get("missing")
        if missing is not None:
            if not isinstance(missing, list):
                _unsafe_payload()
            for item_value in missing:
                entry = _mapping(item_value,
                                 allowed=frozenset({"shop_ref", "platform", "reason"}),
                                 required=frozenset({"reason"}))
                if ("shop_ref" in entry) == ("platform" in entry):
                    _unsafe_payload()
                if "shop_ref" in entry:
                    _ref(entry["shop_ref"])
                if "platform" in entry:
                    _platform_code(entry["platform"])
                # 缺口的原因只能说一次：同时带 shop_ref 与 platform 就是把一件事
                # 报给两个读者，而两份说法早晚会对不上。
                _string_in(entry["reason"], _STATUS_REASONS)
        if block["status"] == "complete" and missing is not None:
            _unsafe_payload()      # 自称完整却又列出缺项


def _chart_payload(value: object) -> dict[str, object]:
    """图表声明载荷：键集与数据集载荷不相交，所以判别是唯一下一行代码。"""
    payload = _mapping(value, allowed=CHART_PAYLOAD_KEYS,
                       required=CHART_REQUIRED_PAYLOAD_KEYS)
    _positive_int(payload["chart_version"])
    if payload["chart_version"] != CHART_SPEC_VERSION:
        # 旧形状的载荷不在新列上复用：读回时字段含义已经变了。
        _unsafe_payload()
    if payload["spec_version"] != CHART_SPEC_NAME:
        _unsafe_payload()
    _string_in(payload["kind"], CHART_KINDS)
    _string_in(payload["x"], CHART_X_COLUMNS)
    _string_in(payload["y"], CHART_Y_COLUMNS)
    _string_in(payload["unit"], CHART_UNITS)
    _string_in(payload["baseline"], CHART_BASELINES)
    _string_in(payload["null_values"], CHART_NULL_HANDLES)
    _string_in(payload["dataset_type"], DATASET_ARTIFACT_TYPES)
    _string_in(payload["coverage_status"], _COVERAGE_STATUSES)
    for column in ("dataset_ref", "coverage_ref"):
        if not isinstance(payload[column], str) \
                or not _UUID_RE.fullmatch(payload[column]):
            # 必须是 `str(UUID)` 原样：拿任意文本当引用就是给“指错了数据集”留门。
            _unsafe_payload()
    if payload["dataset_ref"] != payload["coverage_ref"]:
        # 本轮一张图只从一份数据集出数：分开两个引用就是允许“拿 A 的覆盖说 B 的数”。
        _unsafe_payload()
    if not isinstance(payload["series"], list):
        _unsafe_payload()
    for column in payload["series"]:
        _string_in(column, CHART_SERIES_COLUMNS)
    basis = payload["metric_basis"]
    if not isinstance(basis, str) or not _METRIC_BASIS_RE.fullmatch(basis):
        # `指标|口径|时间归属`：与血缘签名同一个形状，不开自由文本通道。
        _unsafe_payload()
    if basis.split("|", 1)[0] != str(payload["y"]):
        _unsafe_payload()
    if payload["unit"] != COMMERCE_METRIC_UNITS[str(payload["y"])]:
        # 单位由指标决定：允许自填单就能把件、元、率三样东西画到同一根轴上。
        _unsafe_payload()
    if payload["kind"] == "bar" and payload["baseline"] != "zero":
        _unsafe_payload()   # 条形图不零基线就是拿轴长编故事
    if payload["kind"] == "bar" and payload["x"] == "day":
        _unsafe_payload()   # 按日的条形图会把缺失日画成“没有这根柱子”
    if payload["kind"] == "line" and payload["x"] != "day":
        _unsafe_payload()
    if payload["kind"] == "scatter":
        _unsafe_payload()   # 本轮没有 scatter 的生产者：不接受，也不预备
    for boundary in ("dataset_data_as_of",):
        _datetime_string(payload[boundary])
    for boundary in ("coverage_start", "coverage_end"):
        _date_string(payload[boundary])
    if not isinstance(payload["coverage_gaps"], list):
        _unsafe_payload()
    for gap in payload["coverage_gaps"]:
        if not isinstance(gap, str) or not _GAP_RE.fullmatch(gap):
            _unsafe_payload()
    if "currency" in payload:
        _string_in(payload["currency"], _CURRENCY_VALUES)
        if payload["unit"] != "CNY":
            _unsafe_payload()   # 不系金额的指标不带币种
    elif payload["unit"] == "CNY":
        _unsafe_payload()
    return payload


def _resolved_product(value: object) -> None:
    """解析结果只留引用与版本：ERP 商品号与匹配用的文本都不在这里。"""
    product = _mapping(value, allowed=frozenset({"product_ref", "sku_refs",
                                                 "mapping_version",
                                                 "catalog_version"}),
                       required=frozenset({"product_ref"}))
    _ref(product["product_ref"])
    if "sku_refs" in product:
        _string_list(product["sku_refs"], _ref)
    if "mapping_version" in product:
        version = product["mapping_version"]
        if not isinstance(version, str) or not _BASIS_NAME_RE.fullmatch(version):
            _unsafe_payload()
    if "catalog_version" in product:
        _non_negative_int(product["catalog_version"])


def _comparison(value: object) -> None:
    """上期比较：每一格都自带“能不能比”，不可比时差额一律 null。"""
    block = _mapping(value,
                     allowed=frozenset({"window", "comparable", "reason", "rows"}),
                     required=frozenset({"window", "comparable", "rows"}))
    _date_pair(block["window"])
    _bool_flag(block["comparable"])
    if "reason" in block:
        _string_in(block["reason"], _STATUS_REASONS)
    if not isinstance(block["rows"], list):
        _unsafe_payload()
    for row in block["rows"]:
        entry = _mapping(row, allowed=frozenset({"shop_ref", "metric", "current",
                                                "previous", "change",
                                                "change_ratio"}),
                         required=frozenset({"metric"}))
        if "shop_ref" in entry:
            _ref(entry["shop_ref"])
        if entry["metric"] not in _METRICS:
            _unsafe_payload()
        for key in ("current", "previous", "change", "change_ratio"):
            if key in entry:
                _decimal_or_null(entry[key])
        if not block["comparable"] and any(
                entry.get(key) is not None
                for key in ("previous", "change", "change_ratio")):
            # 不可比却给了差额：把口径变化说成经营变化，正是这条规则要挡的。
            _unsafe_payload()


def _opportunity(value: object) -> None:
    """重点投放候选：只有排序与可逐项核对的候选，不含阈值结论。"""
    block = _mapping(value, allowed=frozenset({"status", "ranking_scope",
                                              "candidates", "flagged"}),
                     required=frozenset({"status", "ranking_scope"}))
    _string_in(block["status"], _OPPORTUNITY_STATUSES)
    _string_in(block["ranking_scope"], _RANKING_SCOPES)
    if "candidates" in block:
        if not isinstance(block["candidates"], list):
            _unsafe_payload()
        for row in block["candidates"]:
            entry = _mapping(row, allowed=frozenset({"shop_ref", "sales_amount",
                                                     "sales_share",
                                                     "product_gross_profit_reference",
                                                     "product_gross_margin_reference",
                                                     "sold_quantity"}),
                             required=frozenset({"shop_ref"}))
            _ref(entry["shop_ref"])
            for key, key_value in entry.items():
                if key != "shop_ref":
                    _decimal_or_null(key_value)
    if "flagged" in block:
        _string_list(block["flagged"], _ref)


def _candidate_cards(value: object) -> None:
    """needs_input 的候选卡片：只能是一张引用，不能带名字也不能带自由文本。

    名字在这条路径上无法安全解析（本路径不建目录、不发 Artifact），所以只发引用；
    重复引用会把它说成两个商品，同一个引用只计一次。
    """
    if not isinstance(value, list):
        _unsafe_payload()
    seen: set[str] = set()
    for item in value:
        entry = _mapping(item, allowed=frozenset({"ref"}), required=frozenset({"ref"}))
        _ref(entry["ref"])
        if entry["ref"] in seen:
            _unsafe_payload()
        seen.add(str(entry["ref"]))


def _metric_units(value: object) -> None:
    """每个指标的计量单位：模型不能靠口径文本自己认单位。"""
    units = _mapping(value, allowed=frozenset(_METRICS))
    for metric, unit in units.items():
        if metric not in COMMERCE_METRICS:
            _unsafe_payload()
        _string_in(unit, _METRIC_UNITS)


def _termination_code(value: object) -> None:
    if not isinstance(value, str) or value not in TERMINATION_REASONS:
        _unsafe_payload()


def _coverage(value: object) -> dict[str, object]:
    # suggested_window 不在 required 里：旧 Artifact 没这个字段，必须继续可读。
    coverage = _mapping(
        value,
        allowed=frozenset({"status", "start", "end", "gaps", "suggested_window"}),
        required=frozenset({"status", "start", "end", "gaps"}),
    )
    _string_in(coverage["status"], _COVERAGE_STATUSES)
    for boundary in ("start", "end"):
        if coverage[boundary] is not None:
            _date_string(coverage[boundary])
    if not isinstance(coverage["gaps"], list):
        _unsafe_payload()
    for gap in coverage["gaps"]:
        if not isinstance(gap, str) or not _GAP_RE.fullmatch(gap):
            _unsafe_payload()
    suggested = coverage.get("suggested_window")
    if suggested is not None:
        # 建议窗口只能是两个 ISO 日期；它是建议，不是被改写过的原请求。
        if (not isinstance(suggested, (list, tuple)) or len(suggested) != 2
                or any(not isinstance(item, str) for item in suggested)):
            _unsafe_payload()
        for item in suggested:
            _date_string(item)
    return coverage


def _artifact_refs(value: object) -> None:
    if not isinstance(value, list):
        _unsafe_payload()
    for item in value:
        ref = _mapping(item, allowed=frozenset({"id", "type"}),
                       required=frozenset({"id", "type"}))
        if not isinstance(ref["id"], str):
            _unsafe_payload()
        try:
            UUID(ref["id"])
        except ValueError:
            _unsafe_payload()
        # 类型白名单与 `ArtifactRef` 同源（六类可枚举）：运营图要发布趋势与对比
        # 数据集，把它们挡在状态之外只会让状态记录少掉真实存在的引用。
        # 能不能属于本领域由 Store 在写 Artifact 时按 `allows_artifact_type` 复核。
        if ref["type"] not in ARTIFACT_TYPES:
            _unsafe_payload()


def _normalized_request(value: object) -> dict[str, object]:
    request = _mapping(value, allowed=_NORMALIZED_REQUEST_KEYS)
    if "shop_refs" in request:
        _string_list(request["shop_refs"], _ref_or_invalid_shop)
    if "sku_refs" in request:
        _string_list(request["sku_refs"], _ref)
    if "platforms" in request:
        _string_list(request["platforms"], _platform_code)
    for key, allowed in (("scope_mode", _SCOPE_MODES), ("sales_basis", _SALES_BASES),
                         ("profit_basis", _PROFIT_BASES), ("report_kind", _REPORT_KINDS)):
        if key in request:
            _string_in(request[key], allowed)
    if "product_ref" in request:
        _ref(request["product_ref"])
    if "opportunity_policy_ref" in request:
        _policy_ref(request["opportunity_policy_ref"])
    if "expected_prices" in request:
        _expected_price_entries(request["expected_prices"])
    if "price_basis" in request:
        _string_in(request["price_basis"], _PRICE_BASES)
    if "as_of" in request:
        _string_in(request["as_of"], _AS_OF_VALUES)
    if "platform_group_rule" in request:
        # 分组规则版本是短标识，不是自由文本：与口径名同一规则。
        if not isinstance(request["platform_group_rule"], str) \
                or not _BASIS_NAME_RE.fullmatch(request["platform_group_rule"]):
            _unsafe_payload()
    if "trend_days" in request:
        _positive_int(request["trend_days"])
        if request["trend_days"] != TREND_DAYS:
            _unsafe_payload()
    if "metrics" in request:
        _string_list(request["metrics"], lambda item: _string_in(item, _METRICS))
    for boundary in ("start", "end"):
        if boundary in request:
            _date_string(request[boundary])
    if "group_by" in request:
        _string_in(request["group_by"], _GROUP_BY)
    if "compare" in request:
        _string_in(request["compare"], _COMPARE)
    if "top_n" in request:
        _positive_int(request["top_n"])
        if request["top_n"] > 500:
            _unsafe_payload()
    if "currency" in request and request["currency"] != "CNY":
        _unsafe_payload()
    return request


def _state_error(value: object) -> None:
    if not isinstance(value, dict):
        _unsafe_payload()
    try:
        ErrorEnvelope.model_validate(value)
    except ValidationError as error:
        raise ValueError("unsafe_persistence_payload") from error


def validate_persisted_state(value: object) -> dict[str, object]:
    state = _mapping(value, allowed=_STATE_KEYS)
    if state and "node" not in state:
        _unsafe_payload()
    if "run_id" in state:
        if not isinstance(state["run_id"], str):
            _unsafe_payload()
        try:
            UUID(state["run_id"])
        except ValueError:
            _unsafe_payload()
    if "node" in state:
        _string_in(state["node"], _NODES)
    if "status" in state:
        _string_in(state["status"], _RUN_STATUSES)
    if "revision" in state:
        _non_negative_int(state["revision"])
    if "normalized_request" in state:
        _normalized_request(state["normalized_request"])
    if "problems" in state:
        _string_list(state["problems"], lambda item: _string_in(item, _PROBLEM_CODES))
    if "tool_status" in state and state["tool_status"] is not None:
        _string_in(state["tool_status"], _TOOL_STATUSES)
    if "target_status" in state and state["target_status"] is not None:
        _string_in(state["target_status"], _DOMAIN_STATUSES)
    if "coverage" in state and state["coverage"] is not None:
        _coverage(state["coverage"])
    if "data_as_of" in state and state["data_as_of"] is not None:
        _datetime_string(state["data_as_of"])
    if "limitations" in state:
        _string_list(state["limitations"], lambda item: _string_in(item, _LIMITATION_CODES))
    if "artifact_refs" in state:
        _artifact_refs(state["artifact_refs"])
    if "error" in state and state["error"] is not None:
        _state_error(state["error"])
    return state


def validate_event_payload(value: object) -> dict[str, object]:
    payload = _mapping(value, allowed=_EVENT_KEYS)
    if "problem_codes" in payload:
        _string_list(payload["problem_codes"], lambda item: _string_in(item, _PROBLEM_CODES))
    if "tool_status" in payload:
        _string_in(payload["tool_status"], _TOOL_STATUSES)
    if "target_status" in payload:
        _string_in(payload["target_status"], _DOMAIN_STATUSES)
    if "coverage_status" in payload:
        _string_in(payload["coverage_status"], _COVERAGE_STATUSES)
    if "data_as_of" in payload:
        _datetime_string(payload["data_as_of"])
    if "limitation_codes" in payload:
        _string_list(payload["limitation_codes"], lambda item: _string_in(item, _LIMITATION_CODES))
    if "artifact_refs" in payload:
        _artifact_refs(payload["artifact_refs"])
    if "result_count" in payload:
        _non_negative_int(payload["result_count"])
    return payload


def _public_limitation(value: object) -> None:
    if not isinstance(value, str) or (
        value not in _PUBLIC_LIMITATIONS
        and not any(pattern.fullmatch(value) for pattern in _PUBLIC_LIMITATION_PATTERNS)
    ):
        _unsafe_payload()


def _numeric_result(value: object) -> None:
    if value is None:
        return
    if isinstance(value, int) and not isinstance(value, bool):
        return
    if isinstance(value, str) and _DECIMAL_RE.fullmatch(value):
        return
    _unsafe_payload()


def _result_rows(value: object, *, public: bool) -> None:
    if not isinstance(value, list):
        _unsafe_payload()
    for item in value:
        row = _mapping(item, allowed=_RESULT_COLUMNS)
        if not row:
            _unsafe_payload()
        for key, result_value in row.items():
            if key in _NUMERIC_RESULT_COLUMNS:
                _numeric_result(result_value)
            elif key in _DATE_RESULT_COLUMNS:
                _date_string(result_value)
            elif key in _DATETIME_RESULT_COLUMNS:
                # 快照时点是「什么时候的价格」：与 `data_as_of` 同一形状。只到日的
                # 文本不收：那会把一个日期当成一次抓取时刻，时效就没法算了。
                _datetime_string(result_value)
            elif key in _LISTING_REF_RESULT_COLUMNS:
                _listing_ref(result_value)
            elif key in _LABEL_RESULT_VALUES:
                _string_in(result_value, _LABEL_RESULT_VALUES[key])
            elif key in _REF_RESULT_COLUMNS:
                _ref(result_value)
            elif key in _TEXT_RESULT_COLUMNS:
                if not is_safe_display_name(result_value):
                    _unsafe_payload()
            else:
                # 白名单内但没有校验规则的列一律拒绝：新增列必须同步声明类型。
                _unsafe_payload()


def _filters(value: object, *, public: bool) -> None:
    filters = _mapping(value, allowed=_FILTER_KEYS)
    for boundary in ("start", "end"):
        if boundary in filters:
            _date_string(filters[boundary])
    if "shop_refs" in filters:
        _string_list(filters["shop_refs"], _ref)
    if "metrics" in filters:
        _string_list(filters["metrics"], lambda item: _string_in(item, _METRICS))
    if "group_by" in filters:
        _string_in(filters["group_by"], _GROUP_BY)
    if "compare" in filters:
        _string_in(filters["compare"], _COMPARE)
    if "top_n" in filters:
        _positive_int(filters["top_n"])
        if filters["top_n"] > 500:
            _unsafe_payload()
    if "currency" in filters and filters["currency"] != "CNY":
        _unsafe_payload()
    if "mode" in filters:
        _string_in(filters["mode"], PROMOTION_MODE_VALUES)
    if "platforms" in filters:
        _string_list(filters["platforms"], _platform_code)
    if "product_ref" in filters:
        _ref(filters["product_ref"])
    if "expected_prices" in filters:
        _expected_price_entries(filters["expected_prices"])
    if "price_basis" in filters:
        _string_in(filters["price_basis"], _PRICE_BASES)
    if "as_of" in filters:
        _string_in(filters["as_of"], _AS_OF_VALUES)
    for key, allowed in (("sales_basis", _SALES_BASES), ("profit_basis", _PROFIT_BASES),
                         ("report_kind", _REPORT_KINDS)):
        if key in filters:
            _string_in(filters[key], allowed)
    if filters.get("sales_share_basis") != "evaluated_only":
        # 份额只有"对已评估集合"这一种分母；出现别的取值就是有人偷偷换了分母。
        if "sales_share_basis" in filters:
            _unsafe_payload()


def _metric_definitions(value: object) -> None:
    definitions = _mapping(value, allowed=frozenset(_METRIC_DEFINITION_TEXTS))
    for metric, definition in definitions.items():
        if definition != _METRIC_DEFINITION_TEXTS[metric]:
            _unsafe_payload()


def _public_metric_payload(value: object, *, public: bool) -> dict[str, object]:
    payload = _mapping(value, allowed=_ARTIFACT_KEYS, required=frozenset({"status"}))
    if not public and _PUBLIC_ONLY_ARTIFACT_KEYS & payload.keys():
        # 展示名与目录版本不得出现在给模型的载荷里。
        _unsafe_payload()
    if "entities" in payload:
        _entities(payload["entities"])
    if "catalog_version" in payload:
        _non_negative_int(payload["catalog_version"])
    _string_in(payload["status"], _PUBLIC_PAYLOAD_STATUSES)
    if "metric_definition" in payload:
        _metric_definitions(payload["metric_definition"])
    if "coverage" in payload:
        _coverage(payload["coverage"])
    if "limitations" in payload:
        _string_list(payload["limitations"], _public_limitation)
    if "data_as_of" in payload and payload["data_as_of"] is not None:
        _datetime_string(payload["data_as_of"])
    if "filters" in payload:
        _filters(payload["filters"], public=public)
    if "data" in payload:
        _result_rows(payload["data"], public=public)
    if "basis" in payload:
        _basis_items(payload["basis"])
    if "diagnostics" in payload:
        _diagnostics(payload["diagnostics"])
    # 契约 v2（spec §3）：范围三面分开，被排除的店铺与原因逐条可核对。
    if "requested_scope" in payload:
        _requested_scope(payload["requested_scope"])
    if "evaluated_scope" in payload:
        _evaluated_scope(payload["evaluated_scope"])
    if "excluded_scope" in payload:
        _excluded_scope(payload["excluded_scope"])
    if "metric_statuses" in payload:
        _metric_statuses(payload["metric_statuses"])
    if "group_statuses" in payload:
        _group_statuses(payload["group_statuses"])
    if "ranking" in payload:
        _ranking(payload["ranking"])
    if "trend_window" in payload:
        _date_pair(payload["trend_window"])
    if "resolved_product" in payload:
        _resolved_product(payload["resolved_product"])
    if "comparison" in payload and payload["comparison"] is not None:
        _comparison(payload["comparison"])
    if "opportunity" in payload and payload["opportunity"] is not None:
        _opportunity(payload["opportunity"])
    if "metric_units" in payload:
        _metric_units(payload["metric_units"])
    if "candidates" in payload:
        _candidate_cards(payload["candidates"])
    if "audit" in payload:
        # 判别位在 `validate_artifact_payload`：数据集类型带了 audit 块也照样在这里
        # 被逐字段校验，不给任何一支留出"未登记的键先放行"的口子。
        _audit_summary(payload["audit"])
    if "termination_reason" in payload:
        _termination_code(payload["termination_reason"])
    return payload


def _basis_items(value: object) -> None:
    """口径凭证校验：指标取自固定词表，口径名带版本，主键只能以 shop_ref 出现。

    模型必须看得见口径（否则它会把同名指标当同义词汇总、比较、排名），
    但它看到的只能是不透明引用与口径标签，不能是 ERP 主键或接口方法名。
    """
    if not isinstance(value, list):
        raise ValueError("basis_invalid")
    for item in value:
        if not isinstance(item, dict):
            raise ValueError("basis_invalid")
        if set(item) - _BASIS_ITEM_KEYS:
            raise ValueError("basis_invalid")
        for key in ("metric", "basis", "time_basis"):
            if key not in item:
                raise ValueError("basis_invalid")
        if item["metric"] not in _METRIC_DEFINITION_TEXTS:
            raise ValueError("basis_invalid")
        for key in ("basis", "time_basis", "metric_version"):
            if key in item and not (isinstance(item[key], str)
                                    and _BASIS_NAME_RE.fullmatch(item[key])):
                raise ValueError("basis_invalid")
        if "shop_ref" in item and not (isinstance(item["shop_ref"], str)
                                       and REF_RE.fullmatch(item["shop_ref"])):
            raise ValueError("basis_invalid")


def _diagnostics(value: object) -> None:
    """可量化限制的结构化形状：与披露文本同一来源，只允许已登记的诊断与字段。"""
    if not isinstance(value, dict):
        raise ValueError("diagnostics_invalid")
    for key, fields in value.items():
        if key not in _DIAGNOSIS_KEYS or not isinstance(fields, dict):
            raise ValueError("diagnostics_invalid")
        if set(fields) - _DIAGNOSIS_FIELDS[key]:
            raise ValueError("diagnostics_invalid")
        for item in fields.values():
            if not isinstance(item, (int, str)) or isinstance(item, bool):
                raise ValueError("diagnostics_invalid")


def _expected_price_entries(value: object) -> None:
    """本轮目标价条目：引用 + 金额 + 口径，一个字段都不能多。

    这些条目同时进 `normalized_request`（参与指纹）与 Artifact 的 `filters`（参与展示）：
    两处共用一份校验，否则同一句话在两个地方会长出两个形状。
    """
    if not isinstance(value, list) or not value:
        _unsafe_payload()
    seen: set[tuple] = set()
    for item in value:
        entry = _mapping(item, allowed=_EXPECTATION_KEYS,
                         required=frozenset({"applies_to", "expected_amount",
                                             "currency", "price_basis"}))
        _string_in(entry["applies_to"], _APPLIES_TO_VALUES)
        amount = entry["expected_amount"]
        if not isinstance(amount, str) or not _DECIMAL_RE.fullmatch(amount):
            _unsafe_payload()
        _string_in(entry["currency"], _CURRENCY_VALUES)
        _string_in(entry["price_basis"], _PRICE_BASES)
        for key in ("shop_ref", "sku_ref"):
            if entry.get(key) is not None:
                _ref(entry[key])
        if entry["applies_to"] == "all_selected" and any(
                entry.get(key) for key in ("shop_ref", "sku_ref")):
            # 统一价又带引用就是同一句话说了两个范围：写回时没人能分清哪个生效。
            _unsafe_payload()
        key = (entry["applies_to"], str(entry.get("shop_ref") or ""),
               str(entry.get("sku_ref") or ""))
        if key in seen:
            _unsafe_payload()      # 同一目标项重复声明：那是冲突，不是重复强调
        seen.add(key)


def _audit_summary(value: object) -> None:
    """复核汇总：分母固定为期望项，判定数不得超过它。

    三条结构不变量：

    1. `counts` 取值只能是 spec §5.4 的九个状态，且总和等于 `expected_items`：
       每一格都必须有一个状态，「没扫到」不能被抖成「不存在」；
    2. `evaluated_items` 只算真正比过价的格（match / mismatch），其余都是证据不足；
    3. `all_correct` 只有在每一格都新鲜、完整且匹配时才能为 true（spec §6）。
    """
    summary = _mapping(value, allowed=_AUDIT_SUMMARY_KEYS,
                       required=_AUDIT_SUMMARY_REQUIRED)
    for key in ("expected_items", "evaluated_items", "matched_items"):
        _non_negative_int(summary[key])
    expected = int(summary["expected_items"])
    evaluated = int(summary["evaluated_items"])
    matched = int(summary["matched_items"])
    if expected > MAX_ROSTER_ITEMS:
        # 分母超过上限就是有人截过 roster：宁可拒发，也不让人拿一份 Top N 当全量复核。
        _unsafe_payload()
    if evaluated > expected or matched > evaluated:
        _unsafe_payload()
    counts = _mapping(summary["counts"], allowed=_AUDIT_STATUSES)
    for status, count in counts.items():
        _string_in(status, _AUDIT_STATUSES)
        _non_negative_int(count)
    if sum(int(count) for count in counts.values()) != expected:
        _unsafe_payload()
    if matched != int(counts.get("match", 0)):
        _unsafe_payload()
    if evaluated != matched + int(counts.get("mismatch", 0)):
        _unsafe_payload()
    _bool_flag(summary["all_correct"])
    if summary["all_correct"] and not (expected > 0 and evaluated == expected
                                       and matched == expected):
        _unsafe_payload()
    if "freshness_policy_seconds" in summary:
        _positive_int(summary["freshness_policy_seconds"])
    if "rule_version" in summary:
        version = summary["rule_version"]
        if not isinstance(version, str) or not _BASIS_NAME_RE.fullmatch(version):
            _unsafe_payload()
    sources = summary["sources"]
    if not isinstance(sources, list):
        _unsafe_payload()
    seen: set[str] = set()
    for entry_value in sources:
        entry = _mapping(entry_value, allowed=_AUDIT_SOURCE_KEYS,
                         required=_AUDIT_SOURCE_REQUIRED)
        _ref(entry["shop_ref"])
        if entry["shop_ref"] in seen:
            _unsafe_payload()      # 一家店只引用一次快照：两次就是两个时点被当成一个
        seen.add(str(entry["shop_ref"]))
        _string_in(entry["source_kind"], _LISTING_SOURCE_KINDS)
        _bool_flag(entry["enumeration_complete"])
        _bool_flag(entry["fresh"])
        if entry.get("snapshot_at") is not None:
            _datetime_string(entry["snapshot_at"])


def _price_audit_payload(value: object, *, public: bool) -> dict[str, object]:
    """price_audit 载荷：数据集形状 + 自己的 `audit` 块与差异表行规则。

    行数必须等于 `expected_items`：拿「抓到的链接数」当行数，一份只覆盖四家的结果
    就会看起来像「四家都对了」，而第五家根本没进这张表。
    """
    payload = _public_metric_payload(value, public=public)
    if "audit" not in payload:
        _unsafe_payload()
    rows = payload.get("data")
    if not isinstance(rows, list):
        _unsafe_payload()
    summary = payload["audit"]
    assert isinstance(summary, dict)
    if len(rows) != int(summary["expected_items"]):
        _unsafe_payload()
    seen: set[tuple] = set()
    for row_value in rows:
        row = _mapping(row_value, allowed=_RESULT_COLUMNS,
                       required=frozenset({"shop_ref", "audit_status"}))
        _ref(row["shop_ref"])
        key = (str(row["shop_ref"]), str(row.get("listing_ref") or ""),
               str(row.get("sku_ref") or ""))
        if key in seen:
            _unsafe_payload()      # 同一格发两行：差额会被读成两个不同的链接
        seen.add(key)
        if (row["audit_status"] not in ("match", "mismatch")
                and row.get("amount_difference") is not None):
            # 过期 / 不可比 / 缺标准那一格给一个差，就会被当成「当前差异」转述出去。
            _unsafe_payload()
        if row["audit_status"] == "match" and row.get("actual_amount") is None:
            _unsafe_payload()      # 没有实际价的 match 是一个没依据的结论
        if row["audit_status"] in ("match", "mismatch") and not _prices_agree(
                row.get("actual_amount"), row.get("expected_amount"),
                str(row["audit_status"])):
            # 标签与数字必须相互印证：一个带"0.00 差额、两边同价"的 mismatch 行，
            # 没人在运行现场能看出它是错的，而模型会拿它去说"对不上"。
            _unsafe_payload()
    return payload


def _prices_agree(actual: object, expected: object, status: str) -> bool:
    """判定标签与两侧金额是否互相印证（`compare_price` 的那条规则在出表处再核一次）。

    不拿生产方当凭据：载荷会被读、会被存档、会被别的实现写进来。同一句规则在两处
    成立，比只在一处成立难被绕过。
    """
    from decimal import Decimal, InvalidOperation

    try:
        left = Decimal(str(actual))
        right = Decimal(str(expected))
    except (InvalidOperation, TypeError, ValueError):
        return False
    equal = left == right
    return equal if status == "match" else not equal


def validate_model_payload(value: object,
                           artifact_type: str = "metric_result") -> dict[str, object]:
    """给模型的载荷：与公开载荷共用同一判别位，只是不含展示名。"""
    if artifact_type == "price_audit":
        return _price_audit_payload(value, public=False)
    return _public_metric_payload(value, public=False)


def validate_artifact_payload(value: object,
                             artifact_type: str = "metric_result") -> dict[str, object]:
    """公开 Artifact 载荷校验：按类型判到哪一个独立 schema（spec §3 判别联合）。

    `chart_spec` 与数据集载荷的键集完全不相交（图表不带 `status`/`data`），所以
    类型就是唯一的判别位；`price_audit` 带自己的 `audit` 块并要求每一行都有判定
    状态，一份 metric_result 形状不能冒充复核结果。未知类型与不属于本领域的
    类型仍由各 Store 按注册表拒掉。
    """
    if artifact_type == "chart_spec":
        return _chart_payload(value)
    if artifact_type == "price_audit":
        return _price_audit_payload(value, public=True)
    return _public_metric_payload(value, public=True)


def validate_coverage_payload(value: object) -> dict[str, object]:
    return _coverage(value)


def validate_normalized_request(value: object) -> dict[str, object]:
    return _normalized_request(value)


NormalizedRequest = Annotated[dict[str, object], BeforeValidator(validate_normalized_request)]
PersistedState = Annotated[dict[str, object], BeforeValidator(validate_persisted_state)]
EventPayload = Annotated[dict[str, object], BeforeValidator(validate_event_payload)]
ModelPayload = Annotated[dict[str, object], BeforeValidator(validate_model_payload)]
CoveragePayload = Annotated[dict[str, object], BeforeValidator(validate_coverage_payload)]


class DomainStatus(StrEnum):
    SUCCESS = "success"
    NEEDS_INPUT = "needs_input"
    MISSING_DATA = "missing_data"
    PARTIAL = "partial"
    FAILED = "failed"


class RunStatus(StrEnum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    NEEDS_INPUT = "needs_input"
    MISSING_DATA = "missing_data"
    PARTIAL = "partial"
    FAILED = "failed"


class RecoveryAction(StrEnum):
    NONE = "none"
    ASK_USER = "ask_user"
    CORRECT_PARAMETERS = "correct_parameters"
    RETRY_LATER = "retry_later"


class RunEventType(StrEnum):
    ENTERED = "entered"
    COMPLETED = "completed"
    FAILED = "failed"
    TRANSITIONED = "transitioned"


class ErrorEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    code: ErrorCode
    stage: PersistenceNode
    retryable: bool
    recovery: RecoveryAction
    public_message: PublicMessage
    problems: list[ProblemCode] = Field(default_factory=list)


class ArtifactRef(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    id: UUID
    type: str

    @field_validator("type")
    @classmethod
    def _known_type(cls, value: str) -> str:
        if value not in ARTIFACT_TYPES:
            raise ValueError("unknown_artifact_type")
        return value


class DomainArtifact(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    ref: ArtifactRef
    # 载荷按 `ref.type` 判到哪个 schema：图表载荷与数据集载荷是两份独立契约，
    # 所以下面用 after-validator 拿类型去分发，而不是在字段上固定一份校验器。
    public_payload: dict[str, object]

    @model_validator(mode="after")
    def _payload_matches_artifact_type(self) -> "DomainArtifact":
        self.public_payload = validate_artifact_payload(self.public_payload,
                                                       self.ref.type)
        return self


class DomainResult(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    run_id: UUID
    status: DomainStatus
    model_payload: ModelPayload
    artifacts: list[DomainArtifact] = Field(default_factory=list)
    data_as_of: datetime | None = None
    coverage: Coverage | None = None
    error: ErrorEnvelope | None = None
    # v2 追加：没有它们的结果仍合法（旧聊天空着读），有它们就必须合法。
    provenance: QueryProvenance | None = None
    identity: RequestIdentity | None = None


class NewQueryRun(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    chat_id: UUID
    user_message_id: UUID
    subject_id: str
    tool_call_id: str
    domain: str = "business_query"
    attempt_no: int = Field(ge=1)
    normalized_request: NormalizedRequest = Field(default_factory=dict)
    state: PersistedState = Field(default_factory=dict)
    provenance: QueryProvenance | None = None
    identity: RequestIdentity | None = None

    @field_validator("domain")
    @classmethod
    def _known_domain(cls, value: str) -> str:
        # 未登记领域在写库之前就被拒，不依赖数据库 CHECK 兜底。
        if not known_domain(value):
            raise ValueError("unknown_domain")
        return value

    @model_validator(mode="after")
    def _identity_matches_attempt(self) -> "NewQueryRun":
        if self.identity is not None and self.identity.attempt_no != self.attempt_no:
            raise ValueError("identity_attempt_mismatch")
        return self


class RunTransition(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    expected_revision: int = Field(ge=0)
    node: PersistenceNode
    event_type: RunEventType = RunEventType.TRANSITIONED
    status: RunStatus
    state: PersistedState
    normalized_request: NormalizedRequest | None = None
    payload: EventPayload = Field(default_factory=dict)
    error_code: ErrorCode | None = None

    @model_validator(mode="after")
    def _normalized_request_must_match_state(self) -> "RunTransition":
        transition_normalized_request(self)
        return self


def transition_normalized_request(
    transition: RunTransition,
) -> dict[str, object] | None:
    """Return the state-owned normalized request or reject divergent copies."""
    if not isinstance(transition.state, dict):
        raise ValueError("unsafe_persistence_payload")
    state_normalized_request = transition.state.get("normalized_request")
    if (
        transition.normalized_request is not None
        and state_normalized_request != transition.normalized_request
    ):
        raise ValueError("normalized_request_mismatch")
    if state_normalized_request is None:
        return None
    if not isinstance(state_normalized_request, dict):
        raise ValueError("unsafe_persistence_payload")
    return state_normalized_request


class NewArtifact(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    artifact_type: str = "metric_result"
    # 与 `DomainArtifact` 同一规则：载荷形状由类型决定，不在字段上写死一份校验器。
    payload: dict[str, object]
    data_as_of: datetime | None = None
    coverage: CoveragePayload | None = None
    # 只有 chart_spec 需要这两项；由数据库 CHECK 与本校验双重守住。
    dataset_ref: UUID | None = None
    chart_version: int | None = Field(default=None, ge=1)

    @field_validator("artifact_type")
    @classmethod
    def _known_type(cls, value: str) -> str:
        if value not in ARTIFACT_TYPES:
            raise ValueError("unknown_artifact_type")
        return value

    @model_validator(mode="after")
    def _chart_pairing(self) -> "NewArtifact":
        if self.artifact_type == "chart_spec":
            if self.dataset_ref is None or self.chart_version is None:
                raise ValueError("chart_requires_dataset_version")
            # 载荷与列必须说同一件事：引用、覆盖引用与版本三对都逐字相同，
            # 否则“列指向 A、载荷里写 B”就是两份互相矛盾的出处面。
            # 被引用的数据集是不是**真的存在且同版本**由各 Store 在写库前核
            # （`runtime.artifacts.verify_chart_pairing`）。
            if (self.payload.get("dataset_ref") != str(self.dataset_ref)
                    or self.payload.get("coverage_ref") != str(self.dataset_ref)
                    or self.payload.get("chart_version") != self.chart_version):
                raise ValueError("chart_reference_mismatch")
        elif self.dataset_ref is not None or self.chart_version is not None:
            raise ValueError("unexpected_dataset_reference")
        self.payload = validate_artifact_payload(self.payload, self.artifact_type)
        return self


class RunCompletion(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    expected_revision: int = Field(ge=0)
    node: PersistenceNode
    status: RunStatus
    state: PersistedState
    payload: EventPayload = Field(default_factory=dict)
    termination_reason: str | None = None

    @field_validator("termination_reason")
    @classmethod
    def _known_reason(cls, value: str | None) -> str | None:
        # 码表与 009 的 SQL CHECK 同源，任意错误文本不许进运行记录。
        if value is not None and value not in TERMINATION_REASONS:
            raise ValueError("unknown_termination_reason")
        return value
    error_code: ErrorCode | None = None


class TurnContext(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    chat_id: UUID
    user_message_id: UUID
    subject_id: str


class RunContextNotFound(Exception):
    def __init__(self) -> None:
        super().__init__("run_context_not_found")


class RunNotFound(Exception):
    def __init__(self) -> None:
        super().__init__("run_not_found")


class SchemaOutdated(Exception):
    """数据库还没应用本进程依赖的迁移：早失败并说清是哪一版，不留“莫名 500”。"""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)


class StaleRunRevision(Exception):
    def __init__(self) -> None:
        super().__init__("stale_run_revision")


class ArtifactPersistenceError(Exception):
    def __init__(self, _reason: str | None = None) -> None:
        super().__init__("artifact_persistence_error")


class QueryRunStore(Protocol):
    def create_run(self, record: NewQueryRun) -> UUID: ...

    def transition(self, run_id: UUID, transition: RunTransition) -> None: ...

    def save_artifact(self, run_id: UUID, artifact: NewArtifact) -> ArtifactRef: ...

    def finish(self, run_id: UUID, completion: RunCompletion) -> None: ...
