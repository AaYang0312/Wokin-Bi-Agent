"""版本化运行契约：数据血缘、请求身份与跨领域 Artifact 判别。

计划 Task 3 要解决的真实风险：`revision` 只表示状态推进，一旦被当成"数据版本"，
同一 revision 在数据回填后就会继续命中旧结果，经营者看到的是过期数字。
这里把"哪一版口径、哪一批数据"显式记下来，并让请求指纹依赖它们。

复现等级（如实声明）：本契约保证**已存 Artifact 可复现读取**——给定 run 与
artifact 引用，能读出当时发布给用户的载荷与其版本。没有版本化事实库之前，
不承诺任意历史 SQL 重跑得到同一结果；`source_batches` 只能指回当时的同步批次。
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .domain_registry import (
    ARTIFACT_TYPES,
    DATASET_ARTIFACT_TYPES,
    allows_artifact_type,
    known_domain,
)

# 口径版本：改指标定义、改名称目录口径、改映射规则都必须推进对应版本，
# 否则旧的已存结果会被当成新版本数据复用。
METRIC_VERSION = "metrics/2026-09-11.1"
POLICY_VERSION = "quality-policy/2026-09-11.1"
GRAPH_VERSION = "business_query-graph/2026-09-11.1"
QUERY_TEMPLATE_ID = "fixed_metric_query"
QUERY_TEMPLATE_VERSION = "1"


def _reject_text(value: object) -> object:
    """版本字段是标识符，不是自由文本。

    只拦能携带语句或跳出短标识的写法：首尾空白、引号、分号、反斜杠、控制字符、
    路径上跳。斜杠是合法版本字符（口径版本形如 metrics/2026-09-11.1），不得误拦。
    """
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped or stripped != value or len(stripped) > 80:
            raise ValueError("unsafe_provenance_value")
        if any(ch in value for ch in (";", "'", '"', "\\", "..")):
            raise ValueError("unsafe_provenance_value")
        if any(ord(ch) < 0x20 or ord(ch) == 0x7f for ch in value):
            raise ValueError("unsafe_provenance_value")
    return value


class QueryProvenance(BaseModel):
    """一次查询用了哪一版口径、哪一版目录、哪几批数据。"""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    template_id: str = QUERY_TEMPLATE_ID
    template_version: str = QUERY_TEMPLATE_VERSION
    metric_version: str = METRIC_VERSION
    schema_version: str = "008"
    catalog_version: int = Field(default=0, ge=0)
    mapping_version: str = "identity/2026-09-11.1"
    policy_version: str = POLICY_VERSION
    graph_version: str = GRAPH_VERSION
    # 来源批次：服务端可信标识；不进模型载荷，只进 Artifact 与诊断记录。
    source_batches: tuple[str, ...] = ()
    data_as_of: datetime | None = None

    _clean = field_validator(
        "template_id", "template_version", "metric_version", "schema_version",
        "mapping_version", "policy_version", "graph_version")(
        classmethod(lambda cls, value: _reject_text(value)))

    @field_validator("source_batches")
    @classmethod
    def _batches_are_identifiers(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for item in value:
            if not isinstance(item, str) or not item or item.strip() != item:
                raise ValueError("unsafe_provenance_value")
        return tuple(sorted(set(value)))

    def as_dict(self) -> dict[str, object]:
        """列存形态：批次以数组保存，截止时刻原样保存。"""
        return self.model_dump(mode="python")

    def fingerprint_parts(self) -> dict[str, object]:
        """参与请求指纹的部分：版本一变，旧结果就不允许命中。

        `data_as_of` 与 `source_batches` 也计入——回填推进了截止时刻，
        同一份参数就不再是同一个问题。
        """
        payload = self.as_dict()
        return {key: payload[key] for key in (
            "template_id", "template_version", "metric_version", "schema_version",
            "catalog_version", "mapping_version", "policy_version", "graph_version",
            "source_batches", "data_as_of")}


class RequestIdentity(BaseModel):
    """跨重试稳定的请求身份：`revision` 只表示推进，不承担任何数据版本语义。"""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    root_request_id: UUID
    request_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    attempt_no: int = Field(ge=1)
    recovery_count: int = Field(default=0, ge=0)
    # 终止原因只能取自固定码表：不能把任意错误文本写进运行身份。
    termination_reason: Literal[
        "succeeded", "missing_parameters", "invalid_parameters", "forbidden",
        "coverage_incomplete", "data_as_of_unknown", "source_quality_failed",
        "source_not_onboarded", "revenue_not_attributed", "result_too_large",
        # 逐指标能力未授予：与来源未开通、缺覆盖分别归因。
        "capability_unavailable",
        "comparison_coverage_incomplete", "deadline_exceeded", "query_timeout",
        "persistence_failed", "contract_violation", "upstream_unavailable",
        "transient_source_failure", "recovery_exhausted",
    ] | None = None


def _termination_reasons() -> frozenset[str]:
    """终止原因码表从模型字段推导，不再手抄第二份；SQL CHECK 由测试比对它。"""
    from typing import get_args

    members: set[str] = set()
    for item in get_args(RequestIdentity.model_fields["termination_reason"].annotation):
        nested = get_args(item)
        if nested:                      # Literal[...] 的成员才是码表
            members.update(str(value) for value in nested)
    return frozenset(members)


TERMINATION_REASONS = _termination_reasons()


def request_fingerprint(*, subject_id: str, allowed_shop_ids: frozenset[str],
                        normalized_request: dict[str, object],
                        provenance: QueryProvenance) -> str:
    """规范化请求 + 授权范围 + 数据版本 → 稳定指纹。

    授权范围必须参与：同一条查询换一个主体就不是同一个请求，命中他人结果
    等于越权读取。
    """
    import hashlib
    import json

    material = json.dumps(
        {
            "subject": subject_id,
            "scope": sorted(str(shop) for shop in allowed_shop_ids),
            "request": normalized_request,
            "provenance": provenance.fingerprint_parts(),
        },
        sort_keys=True, ensure_ascii=False, default=str, separators=(",", ":"),
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


class ArtifactEnvelope(BaseModel):
    """跨领域 Artifact 的判别外壳：类型决定 payload 的校验方式。"""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    artifact_type: Literal[
        "metric_result", "comparison_table", "trend_series", "chart_spec",
        "price_audit", "inventory_alerts"]
    payload: dict[str, object]
    data_as_of: datetime | None = None
    coverage: dict[str, object] | None = None
    domain: str = "business_query"
    provenance: QueryProvenance | None = None
    # chart_spec 与它引用的数据集必须是同一次运行的同一版本。
    dataset_ref: UUID | None = None
    chart_version: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def _type_must_match_domain_and_dataset(self) -> "ArtifactEnvelope":
        if self.artifact_type not in ARTIFACT_TYPES:
            raise ValueError("unknown_artifact_type")
        if not known_domain(self.domain):
            raise ValueError("unknown_domain")
        if not allows_artifact_type(self.domain, self.artifact_type):
            raise ValueError("artifact_type_not_allowed_for_domain")
        if self.artifact_type == "chart_spec":
            if self.dataset_ref is None or self.chart_version is None:
                raise ValueError("chart_requires_dataset_version")
        elif self.dataset_ref is not None or self.chart_version is not None:
            # 只有图表需要指回数据集；其他类型带这两个字段说明装配错了。
            raise ValueError("unexpected_dataset_reference")
        return self

    @property
    def is_dataset(self) -> bool:
        return self.artifact_type in DATASET_ARTIFACT_TYPES
