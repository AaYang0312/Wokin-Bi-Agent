"""隔离分析的严格契约：请求、观测、数据集、finding 与结果。

计划 2026-09-14-isolated-analysis-agent.md Task 1。所有模型共用一条纪律：
`extra="forbid"` 拒绝拼错或临时发明的键，`frozen=True` 禁止就地改写，
`hide_input_in_errors` 不把绑定业务值回显进校验错误文本。

隔离边界（计划 Global Constraints）在本模块内成立：这里只 import 标准库与
pydantic，不 import psycopg、repository、HTTP client、文件 API、业务 Tool 或
同步代码。分析运行只接收 `AnalysisDataset` 值对象；从 Artifact 到数据集的
授权投影属可信服务层（Task 2 的 loader），数值 finding 一律由 Task 3 的
Decimal 纯函数产生，模型只做无工具总结（Task 4）。

安全形状：
- dimension/metric 键：`^[a-z][a-z0-9_]{0,47}$`；
- dimension 值只能是 stable ref（kebab token）、ISO date 或登记枚举 token——
  原始 ERP 主键、带空格的自由文本、数字开头句柄一律拒收；
- 指标值只收精确十进制：拒 float、布尔、NaN/Infinity 与指数文本，不给
  浮点通道；输出 Decimal 由后续 Task 量化为字符串，这里先守住输入端。
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

AnalysisKind = Literal["contribution", "change_decomposition",
                       "anomaly_candidates", "followups"]

_KEY_RE = r"^[a-z][a-z0-9_]{0,47}$"
_ARTIFACT_REF_RE = (r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-"
                    r"[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
_FINGERPRINT_RE = r"^[0-9a-f]{64}$"
_ROW_REF_RE = r"^row-[a-z0-9-]{1,60}$"
_FINDING_REF_RE = r"^finding-[a-z0-9-]{1,60}$"
_VERSION_RE = r"^[a-z0-9]+(-[a-z0-9]+)*/[0-9]{4}-[0-9]{2}-[0-9]{2}\.[0-9]+$"
_DIMENSION_VALUE_RE = re.compile(r"^[a-z][a-z0-9]*(-[a-z0-9]+)*$")
_ISO_DATE_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
_PLAIN_DECIMAL_RE = re.compile(r"^-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?$")
_KEY_COMPILED = re.compile(_KEY_RE)
_ROW_COMPILED = re.compile(_ROW_REF_RE)
# 计划 Task 1：`sql/prompt/tool_calls/raw_rows` 键在任意层级都拒——values 映射的
# 键也是键，不能因为值是自由文本就放过查询通道词汇。与 runtime/models.py 的
# `_ANALYSIS_FORBIDDEN_KEYS` 同一词表（两侧独立编译，理由同版本正则）。
_FORBIDDEN_KEYS = frozenset({"sql", "prompt", "tool_calls", "raw_rows"})
# 指标文本上限：足够容纳"极大 Decimal"gold case，又不给超长数字串留通道。
_METRIC_TEXT_LIMIT = 64
_TEXT_LIMIT = 500
_CODE_LIMIT = 48
_LIST_LIMIT = 20


def _bounded_text(value: object, *, limit: int) -> str:
    """非空、无首尾空白、不超长的单段文本。"""
    if (not isinstance(value, str) or not value or value != value.strip()
            or len(value) > limit):
        raise ValueError("analysis_text_invalid")
    return value


def _safe_decimal(value: object) -> Decimal:
    """指标值只收精确十进制：float/布尔/NaN/Infinity/指数文本一律拒收。"""
    if isinstance(value, Decimal):
        # 直构 Decimal 只出现在可信生产方（Task 2 loader 从 JSON 字符串投影）。
        # 位宽/指数界只对 str/int 输入强制；Decimal 分支仅验 finite，超大值由
        # Task 3 的 quantize 确定性失败兜底（InvalidOperation），仍是 fail closed。
        if not value.is_finite():
            raise ValueError("metric_value_must_be_finite")
        return value
    if isinstance(value, bool):
        raise ValueError("metric_value_must_be_decimal")
    if isinstance(value, float):
        raise ValueError("metric_value_must_not_be_float")
    if isinstance(value, int):
        if len(str(abs(value))) > _METRIC_TEXT_LIMIT:
            raise ValueError("metric_value_too_large")
        return Decimal(value)
    if not isinstance(value, str):
        raise ValueError("metric_value_must_be_decimal")
    text = value.strip()
    if len(text) > _METRIC_TEXT_LIMIT or not _PLAIN_DECIMAL_RE.fullmatch(text):
        raise ValueError("metric_value_must_be_plain_decimal")
    try:
        parsed = Decimal(text)
    except InvalidOperation:
        raise ValueError("metric_value_must_be_decimal") from None
    if not parsed.is_finite():
        raise ValueError("metric_value_must_be_finite")
    return parsed


class AnalysisRequest(BaseModel):
    """一次分析请求：只指向已持久化的 Artifact，声明要跑的分析 kinds。"""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    artifact_ref: str = Field(pattern=_ARTIFACT_REF_RE)
    analysis_kinds: list[AnalysisKind] = Field(min_length=1, max_length=8)

    @field_validator("analysis_kinds")
    @classmethod
    def kinds_are_unique(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("analysis_kind_duplicate")
        return value


class AnalysisObservation(BaseModel):
    """一行安全投影：opaque row_ref + 有界 dimension + 精确十进制指标。"""

    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True,
                              allow_inf_nan=False)

    row_ref: str = Field(pattern=_ROW_REF_RE)
    dimensions: dict[str, str] = Field(max_length=10)
    metrics: dict[str, Decimal] = Field(max_length=10)
    previous_metrics: dict[str, Decimal] = Field(default_factory=dict,
                                                 max_length=10)

    @field_validator("metrics", "previous_metrics", mode="before")
    @classmethod
    def decimal_values_only(cls, value: object) -> object:
        if not isinstance(value, dict):
            return value
        for key, item in value.items():
            if not isinstance(key, str) or not _KEY_COMPILED.fullmatch(key):
                raise ValueError("analysis_key_invalid")
            _safe_decimal(item)
        return value

    @field_validator("dimensions")
    @classmethod
    def dimension_values_are_safe(cls, value: dict[str, str]) -> dict[str, str]:
        for key, item in value.items():
            if not isinstance(key, str) or not _KEY_COMPILED.fullmatch(key):
                raise ValueError("analysis_key_invalid")
            if (not isinstance(item, str)
                    or not (_ISO_DATE_RE.fullmatch(item)
                            or _DIMENSION_VALUE_RE.fullmatch(item))):
                raise ValueError("dimension_value_must_be_ref_date_or_enum")
        return value


class AnalysisDataset(BaseModel):
    """分析运行的唯一输入：服务层投影好的不可变值对象。"""

    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    source_artifact_ref: str = Field(pattern=_ARTIFACT_REF_RE)
    source_fingerprint: str = Field(pattern=_FINGERPRINT_RE)
    metric_version: str = Field(pattern=_VERSION_RE)
    observations: tuple[AnalysisObservation, ...] = Field(max_length=500)
    limitations: tuple[str, ...] = ()

    @field_validator("limitations")
    @classmethod
    def limitations_are_bounded_codes(cls, value: tuple[str, ...]):
        if len(value) > _LIST_LIMIT:
            raise ValueError("analysis_limitations_too_many")
        for item in value:
            _bounded_text(item, limit=200)
        return value


class Finding(BaseModel):
    """一条确定性 finding：数值全部在 `values` 里以字符串出现。"""

    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    finding_ref: str = Field(pattern=_FINDING_REF_RE)
    kind: AnalysisKind
    metric: str = Field(pattern=_KEY_RE)
    row_refs: tuple[str, ...] = Field(min_length=1, max_length=500)
    values: dict[str, str] = Field(max_length=10)
    statement_code: str = Field(pattern=_KEY_RE)

    @field_validator("row_refs")
    @classmethod
    def row_refs_are_row_refs(cls, value: tuple[str, ...]):
        for item in value:
            if not isinstance(item, str) or not _ROW_COMPILED.fullmatch(item):
                raise ValueError("analysis_row_ref_invalid")
        return value

    @field_validator("values")
    @classmethod
    def values_are_bounded_text(cls, value: dict[str, str]) -> dict[str, str]:
        for key, item in value.items():
            if (not isinstance(key, str) or key in _FORBIDDEN_KEYS
                    or not _KEY_COMPILED.fullmatch(key)):
                raise ValueError("analysis_key_invalid")
            _bounded_text(item, limit=200)
        return value


class AnalysisResult(BaseModel):
    """持久化为 `analysis_result` Artifact 的完整结果。"""

    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    source_artifact_ref: str = Field(pattern=_ARTIFACT_REF_RE)
    source_fingerprint: str = Field(pattern=_FINGERPRINT_RE)
    analysis_version: str = Field(pattern=_VERSION_RE)
    findings: tuple[Finding, ...] = Field(max_length=500)
    narrative: tuple[dict[str, object], ...] = Field(default=(),
                                                     max_length=_LIST_LIMIT)
    hypotheses: tuple[str, ...] = Field(default=(), max_length=_LIST_LIMIT)
    unsupported_claims: tuple[str, ...] = Field(default=(),
                                                max_length=_LIST_LIMIT)
    limitations: tuple[str, ...] = Field(default=(), max_length=_LIST_LIMIT)

    @field_validator("hypotheses", "unsupported_claims", "limitations")
    @classmethod
    def text_lists_are_bounded(cls, value: tuple[str, ...]):
        for item in value:
            _bounded_text(item, limit=_TEXT_LIMIT)
        return value
