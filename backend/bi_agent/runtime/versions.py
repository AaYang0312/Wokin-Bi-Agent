"""跨领域共用的版本标识契约。

版本字段是"哪一版口径"的身份证：它们要进请求指纹、进 Artifact lineage，
因此只准是短标识符。形状沿用 `runtime.artifacts` 对 provenance 的同一条约束
（首尾空白、含空白的自由文本、空串一律拒绝），但这条规则只在
`check_version_identifier` 一处定义：`VersionSet` 与语义目录的版本字段共用它，
免得两处对同一个 `metrics/2026-09-12.1` 各判一次、判法还不一样。
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator

INVALID_VERSION_IDENTIFIER = "invalid_version_identifier"


def check_version_identifier(value: object) -> str:
    """版本标识必须非空、不带首尾空白、整体不含任何空白字符。

    不拦斜杠与点：合法版本形如 `reporting/2026-09-14.1`。
    """
    if (not isinstance(value, str) or not value or value != value.strip()
            or any(ch.isspace() for ch in value)):
        raise ValueError(INVALID_VERSION_IDENTIFIER)
    return value


class VersionSet(BaseModel):
    """一次运行要冻结的全部版本：缺一项就分不清"这份结果是按哪套口径算的"。

    `frozen=True` + `extra="forbid"`：版本集合不能就地改，也不接受调用方临时发明的
    新键——新增一个版本字段必须是这里的显式契约，否则拼错的键会被静默丢掉。
    """

    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    schema_version: str
    semantic_catalog_version: str
    data_catalog_version: int = Field(ge=0)
    metric_version: str
    policy_version: str
    source_registry_version: str
    graph_version: str

    @field_validator("schema_version", "semantic_catalog_version", "metric_version",
                     "policy_version", "source_registry_version", "graph_version")
    @classmethod
    def stable_identifier(cls, value: str) -> str:
        return check_version_identifier(value)
