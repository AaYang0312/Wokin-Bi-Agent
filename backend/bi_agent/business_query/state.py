"""Safe persisted state and in-process data for business-query runs."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, Mapping
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from bi_agent.catalog import Catalog
from bi_agent.metrics import Coverage, QueryRequest, ToolResult
from bi_agent.runtime.models import (
    ArtifactRef,
    DomainResult,
    DomainStatus,
    ErrorEnvelope,
    ProblemCode,
    RunStatus,
    validate_persisted_state,
)


class BusinessQueryNode(StrEnum):
    RECEIVED = "received"
    RESOLVE_PARAMETERS = "resolve_parameters"
    VALIDATE_PARAMETERS = "validate_parameters"
    AUTHORIZE_SCOPE = "authorize_scope"
    EXECUTE_FIXED_QUERY = "execute_fixed_query"
    CLASSIFY_RESULT = "classify_result"
    PERSIST_ARTIFACT = "persist_artifact"
    FINALIZE = "finalize"


class BusinessQueryInput(BaseModel):
    """One model tool call, including a JSON parse failure when present."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    tool_call_id: str
    arguments: dict[str, object] | None
    arguments_error: str | None = None


class BusinessQueryState(BaseModel):
    """The complete allowlisted snapshot permitted to cross the Store boundary."""

    model_config = ConfigDict(
        extra="forbid", hide_input_in_errors=True, validate_assignment=True
    )

    run_id: UUID
    node: BusinessQueryNode = BusinessQueryNode.RECEIVED
    status: RunStatus = RunStatus.RUNNING
    revision: int = Field(default=0, ge=0)
    normalized_request: dict[str, object] = Field(default_factory=dict)
    problems: list[ProblemCode] = Field(default_factory=list)
    tool_status: str | None = None
    target_status: DomainStatus | None = None
    coverage: Coverage | None = None
    data_as_of: datetime | None = None
    limitations: list[str] = Field(default_factory=list)
    artifact_refs: list[ArtifactRef] = Field(default_factory=list)
    error: ErrorEnvelope | None = None

    @model_validator(mode="before")
    @classmethod
    def _validate_persisted_input(cls, value: object) -> object:
        if isinstance(value, dict):
            candidate = cls.model_construct(**value)
            validate_persisted_state(BaseModel.model_dump(candidate, mode="json"))
        return value

    @model_validator(mode="after")
    def _validate_persisted_contract(self) -> "BusinessQueryState":
        self._validate_persisted_snapshot()
        return self

    def _validate_persisted_snapshot(self) -> dict[str, object]:
        return validate_persisted_state(super().model_dump(mode="json"))

    def model_copy(
        self, *, update: Mapping[str, object] | None = None, deep: bool = False
    ) -> "BusinessQueryState":
        copied = super().model_copy(update=update, deep=deep)
        return type(self).model_validate(copied.__dict__)

    def model_dump(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        self._validate_persisted_snapshot()
        return super().model_dump(*args, **kwargs)

    def model_dump_json(self, *args: Any, **kwargs: Any) -> str:
        self._validate_persisted_snapshot()
        return super().model_dump_json(*args, **kwargs)


@dataclass
class BusinessQueryContext:
    """Request-scoped data that can contain real identifiers and raw text."""

    chat_id: UUID
    user_message_id: UUID
    subject_id: str
    question: str
    previous_filters: dict[str, object]
    shop_refs: dict[str, str]
    allowed_shop_ids: frozenset[str]
    now: datetime
    deadline: float
    attempt_no: int
    # Only the server may set this after observing a real coverage gap. It never
    # enters the model schema or the persisted graph state.
    trusted_window_override: tuple[str, str] | None = None


@dataclass
class BusinessQueryRuntime:
    """Mutable graph inputs and results which must remain process-local."""

    state: BusinessQueryState
    context: BusinessQueryContext
    resolved_args: dict[str, object] = field(default_factory=dict)
    request: QueryRequest | None = None
    result: ToolResult | None = None
    catalog: Catalog | None = None
    # 已识别的临时连接故障标记与已重试标记：只在本进程内使用，不入库。
    transient_failure: bool = False
    attempted_retry: bool = False
    # 血缘与请求身份：进程内持有，落库同时随 DomainResult 一起返回。
    provenance: object | None = None
    identity: object | None = None


@dataclass
class BusinessQueryExecution:
    """Internal compatibility result; it is never persisted directly."""

    domain_result: DomainResult
    tool_result: ToolResult | None = None
    session_filters: dict[str, object] = field(default_factory=dict)
