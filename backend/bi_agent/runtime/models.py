"""Public models and safe persistence contracts for deterministic query runs."""

from __future__ import annotations

import re
from datetime import date, datetime
from enum import StrEnum
from typing import Annotated, Literal, Protocol
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
from bi_agent.metrics import Coverage, METRIC_DEFINITIONS
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
    QueryProvenance,
    RequestIdentity,
    TERMINATION_REASONS,
)
from .domain_registry import ARTIFACT_TYPES, known_domain
PersistenceNode = Literal[
    "received", "resolve_parameters", "validate_parameters", "authorize_scope",
    "execute_fixed_query", "classify_result", "persist_artifact", "finalize",
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

_METRICS = frozenset(METRIC_DEFINITIONS)
# 口径文案字典：固定指标 + 推广口径（后者以 promotion.py 为真源）。
_METRIC_DEFINITION_TEXTS: dict[str, str] = {**METRIC_DEFINITIONS, **PROMOTION_METRIC_DEFINITIONS}
_TOOL_STATUSES = frozenset({
    "ok", "missing_data", "invalid_parameters", "forbidden", "unavailable",
})
_PUBLIC_PAYLOAD_STATUSES = _TOOL_STATUSES | frozenset({"needs_input", "partial", "failed"})
_RUN_STATUSES = frozenset({
    "running", "succeeded", "needs_input", "missing_data", "partial", "failed",
})
_DOMAIN_STATUSES = frozenset({"success", "needs_input", "missing_data", "partial", "failed"})
_GROUP_BY = frozenset({"total", "day", "shop", "product"})
_COMPARE = frozenset({"none", "previous_period"})
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
})
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
}) | PROMOTION_PUBLIC_LIMITATIONS
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
    # 商品归属披露：四个分项必现（缺项就是给猜测留空间），金额形式与 _DECIMAL_RE 同源。
    re.compile(
        r"^支付额中" + _MONEY + r"元未计入商品维度"
        r"（关闭订单行" + _MONEY + r"元；赠品行" + _MONEY + r"元；"
        r"无商品归属" + _MONEY + r"元；其他" + _MONEY + r"元）$"),
    *PROMOTION_LIMITATION_PATTERNS,
)
_STATE_KEYS = frozenset({
    "run_id", "node", "status", "revision", "normalized_request", "problems",
    "tool_status", "target_status", "coverage", "data_as_of", "limitations",
    "artifact_refs", "error",
})
_EVENT_KEYS = frozenset({
    "problem_codes", "tool_status", "target_status", "coverage_status", "data_as_of",
    "limitation_codes", "artifact_refs", "result_count",
})
_NORMALIZED_REQUEST_KEYS = frozenset({
    "shop_refs", "metrics", "start", "end", "group_by", "compare", "top_n", "currency",
})
_ARTIFACT_KEYS = frozenset({
    "status", "metric_definition", "coverage", "limitations", "data_as_of", "filters",
    "data", "entities", "catalog_version",
})
# 名称只在授权展示层出现：模型载荷带上这两项就是契约违规。
_PUBLIC_ONLY_ARTIFACT_KEYS = frozenset({"entities", "catalog_version"})
_FILTER_KEYS = frozenset({
    "start", "end", "shop_refs", "metrics", "group_by", "compare", "top_n", "currency",
    "mode",
})

_LINE_KINDS = frozenset({"sale", "gift", "suite", "combination", "processing"})
_CURRENCY_VALUES = frozenset({"CNY"})

# 结果列白名单以生产方为单一真源：推广列（含类型与可取集合）从 promotion.py 导出，
# 本模块只补充固定指标侧的列，不再手抄推广列名。
_METRIC_RESULT_COLUMNS = frozenset({
    "day", "shop_ref", "product_ref", "line_kind", "currency",
    "paid_amount", "paid_orders", "erp_documents", "aov", "refund_amount",
    "cash_difference", "cohort_refund_rate", "quantity", "product_paid_amount", "notice",
})
_REF_RESULT_COLUMNS = frozenset({"shop_ref", "product_ref"})
# 文本列只有上限、转义与长数字主键三道限制；不放开成“任意字符串都收”。
_TEXT_RESULT_COLUMNS = frozenset({"notice"})
_DATE_RESULT_COLUMNS = frozenset({"day"}) | PROMOTION_DATE_RESULT_COLUMNS
_LABEL_RESULT_VALUES: dict[str, frozenset[str]] = {
    "line_kind": _LINE_KINDS,
    "currency": _CURRENCY_VALUES,
    **PROMOTION_LABEL_VALUES,
}
_RESULT_COLUMNS = _METRIC_RESULT_COLUMNS | PROMOTION_RESULT_COLUMNS
# 剩下的列一律按十进制/整数严格校验；推广数值列集合作为交叉校验。
_NUMERIC_RESULT_COLUMNS = (_RESULT_COLUMNS - _REF_RESULT_COLUMNS - _DATE_RESULT_COLUMNS
                           - _TEXT_RESULT_COLUMNS
                           - frozenset(_LABEL_RESULT_VALUES))
assert _NUMERIC_RESULT_COLUMNS & PROMOTION_RESULT_COLUMNS == PROMOTION_NUMERIC_RESULT_COLUMNS
# 投影层（business_query/tool.py）复用同一份白名单，避免二次手抄漂移。
ARTIFACT_RESULT_COLUMNS = _RESULT_COLUMNS
ARTIFACT_FILTER_COLUMNS = _FILTER_KEYS
_NODES = frozenset({
    "received", "resolve_parameters", "validate_parameters", "authorize_scope",
    "execute_fixed_query", "classify_result", "persist_artifact", "finalize",
})
# 引用与展示名的形式规则只定义在 bi_agent.catalog 一处，这里复用不拄写。
_REF_RE = REF_RE
# 与 catalog 同一形式：平台码是短标识，不是文本。
_PLATFORM_CODE_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,15}$")
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
        if ref["type"] != "metric_result":
            _unsafe_payload()


def _normalized_request(value: object) -> dict[str, object]:
    request = _mapping(value, allowed=_NORMALIZED_REQUEST_KEYS)
    if "shop_refs" in request:
        _string_list(request["shop_refs"], _ref_or_invalid_shop)
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
    return payload


def validate_model_payload(value: object) -> dict[str, object]:
    return _public_metric_payload(value, public=False)


def validate_artifact_payload(value: object) -> dict[str, object]:
    return _public_metric_payload(value, public=True)


def validate_coverage_payload(value: object) -> dict[str, object]:
    return _coverage(value)


def validate_normalized_request(value: object) -> dict[str, object]:
    return _normalized_request(value)


NormalizedRequest = Annotated[dict[str, object], BeforeValidator(validate_normalized_request)]
PersistedState = Annotated[dict[str, object], BeforeValidator(validate_persisted_state)]
EventPayload = Annotated[dict[str, object], BeforeValidator(validate_event_payload)]
ModelPayload = Annotated[dict[str, object], BeforeValidator(validate_model_payload)]
ArtifactPayload = Annotated[dict[str, object], BeforeValidator(validate_artifact_payload)]
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
    public_payload: ArtifactPayload


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
    payload: ArtifactPayload
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
        elif self.dataset_ref is not None or self.chart_version is not None:
            raise ValueError("unexpected_dataset_reference")
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
