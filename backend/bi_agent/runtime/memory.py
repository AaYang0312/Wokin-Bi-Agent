"""In-memory implementation of the query-run persistence contract for tests."""

from __future__ import annotations

from collections.abc import Collection
from copy import deepcopy
from datetime import datetime, timezone
from uuid import UUID, uuid4

from pydantic import ValidationError

from .artifacts import verify_chart_pairing
from .domain_registry import allows_artifact_type, allows_node
from .models import (
    ArtifactRef,
    NewArtifact,
    NewQueryRun,
    RunCompletion,
    RunEventType,
    RunNotFound,
    RunStatus,
    RunTransition,
    StaleRunRevision,
    validate_artifact_payload,
    validate_coverage_payload,
    validate_event_payload,
    validate_normalized_request,
    validate_persisted_state,
    transition_normalized_request,
)


class MemoryQueryRunStore:
    """Store query-run records in dictionaries with repository-equivalent semantics."""

    def __init__(self, *, forbidden_values: Collection[str]) -> None:
        self._forbidden_values = frozenset(value for value in forbidden_values if value)
        if not self._forbidden_values:
            raise ValueError("forbidden_values_required")
        self.runs: dict[UUID, dict[str, object]] = {}
        self.events: dict[UUID, list[dict[str, object]]] = {}
        self.artifacts: dict[UUID, dict[str, object]] = {}
        # 私有诊断：与 Postgres 的 `bi.query_diagnostics` 同一个位置——按设计带着 SQL 原文
        # 与真店铺主键，所以不进 runs/events/artifacts 中任何一份公开记录。
        self.diagnostics: dict[UUID, list[dict[str, object]]] = {}

    def create_run(self, record: NewQueryRun) -> UUID:
        record = self._revalidate_new_run(record)
        self._validate_normalized_request(record.normalized_request)
        self._validate_state(record.state)
        self._check_domain_node(record.domain, record.state.get("node"))
        if self._has_run_context(record):
            raise ValueError("duplicate_query_run")
        run_id = uuid4()
        now = _now()
        self.runs[run_id] = {
            "id": run_id,
            "chat_id": record.chat_id,
            "user_message_id": record.user_message_id,
            "subject_id": record.subject_id,
            "tool_call_id": record.tool_call_id,
            "domain": record.domain,
            "attempt_no": record.attempt_no,
            "status": RunStatus.RUNNING.value,
            "current_node": record.state.get("node"),
            "revision": 0,
            "normalized_request": deepcopy(record.normalized_request),
            "state": deepcopy(record.state),
            "error_code": None,
            "root_request_id": (record.identity.root_request_id
                               if record.identity else run_id),
            "request_fingerprint": (record.identity.request_fingerprint
                                    if record.identity else None),
            "recovery_count": (record.identity.recovery_count if record.identity else 0),
            "provenance": (record.provenance.model_copy(deep=True)
                           if record.provenance is not None else None),
            "started_at": now,
            "updated_at": now,
            "completed_at": None,
        }
        self.events[run_id] = []
        return run_id

    def transition(self, run_id: UUID, transition: RunTransition) -> None:
        transition = self._revalidate_transition(transition)
        self._validate_state(transition.state)
        normalized_request = transition_normalized_request(transition)
        if normalized_request is not None:
            self._validate_normalized_request(normalized_request)
        self._validate_event(transition.payload)
        run = self._require_current_revision(run_id, transition.expected_revision)
        revision = transition.expected_revision + 1
        now = _now()
        update = {
            "status": transition.status.value,
            "current_node": transition.node,
            "revision": revision,
            "state": deepcopy(transition.state),
            "error_code": transition.error_code,
            "updated_at": now,
        }
        if normalized_request is not None:
            update["normalized_request"] = deepcopy(normalized_request)
        run.update(update)
        self.events[run_id].append(_event(
            run_id=run_id,
            revision=revision,
            node=transition.node,
            event_type=transition.event_type,
            status=transition.status,
            payload=deepcopy(transition.payload),
            created_at=now,
        ))

    def save_artifact(self, run_id: UUID, artifact: NewArtifact) -> ArtifactRef:
        run = self._require_run(run_id)
        artifact = self._revalidate_artifact(artifact)
        self._validate_artifact(artifact.payload, artifact.artifact_type)
        if artifact.coverage is not None:
            self._validate_coverage(artifact.coverage)
        # 领域能产出哪种 Artifact 由 `domain_registry` 一处说了算：只校全局类型白名单的话，
        # business_query 就能写出 price_audit，“按领域白名单”只剩一句注释。
        if not allows_artifact_type(str(run["domain"]), artifact.artifact_type):
            raise ValueError("unsafe_persistence_payload")
        if artifact.artifact_type == "chart_spec":
            # 图表只能引用**已经存过**的同版本数据集：本 Store 与 Postgres Store
            # 跑同一个 `verify_chart_pairing`，否则内存测试会比真实部署宽。
            verify_chart_pairing(
                self.artifacts.get(artifact.dataset_ref), run_id=run_id,
                chart_version=int(artifact.chart_version or 0),
                data_as_of=artifact.data_as_of, coverage=artifact.coverage)
        artifact_id = uuid4()
        self.artifacts[artifact_id] = {
            "id": artifact_id,
            "run_id": run_id,
            "artifact_type": artifact.artifact_type,
            "payload": deepcopy(artifact.payload),
            "data_as_of": artifact.data_as_of,
            "coverage": deepcopy(artifact.coverage) if artifact.coverage is not None else None,
            "dataset_ref": artifact.dataset_ref,
            "chart_version": artifact.chart_version,
            "created_at": _now(),
        }
        return ArtifactRef(id=artifact_id, type=artifact.artifact_type)

    @property
    def diagnostic_count(self) -> int:
        """已记录的私有诊断总数：阶段二的图用它证明“一次执行只留一份证据”。"""
        return sum(len(items) for items in self.diagnostics.values())

    def record_diagnostic(self, run_id: UUID, *, template_id: str, sql_text: str,
                          parameters: dict[str, object]) -> UUID:
        """内存版本的私有诊断记录：与 Postgres 同一契约，只记在公开三条记录之外。

        这条通道按设计带着 SQL 原文与真店铺主键（Postgres 那边写的是
        `bi.query_diagnostics`），所以**不**跑 `_reject_forbidden_values`：那道护栏是给
        公开载荷的，套到这里只会让门禁无法留证。运行行不存在同样报 `RunNotFound`：
        两个实现不能在“写失败长成什么样”上分叉。
        """
        self._require_run(run_id)
        diagnostic_id = uuid4()
        self.diagnostics.setdefault(run_id, []).append({
            "id": diagnostic_id,
            "run_id": run_id,
            "template_id": template_id,
            "sql_text": sql_text,
            "parameters": deepcopy(parameters),
            "recorded_at": _now(),
        })
        return diagnostic_id

    def record_provenance(self, run_id: UUID, *, provenance, identity) -> None:
        run = self._require_run(run_id)
        if provenance is not None:
            run["provenance"] = provenance.model_copy(deep=True)
        if identity is not None:
            run["root_request_id"] = identity.root_request_id
            run["request_fingerprint"] = identity.request_fingerprint
            run["recovery_count"] = identity.recovery_count

    def record_listing_audit_basis(self, run_id: UUID, *, subject_id: str,
                                   fingerprint: str | None, roster, expectations,
                                   price_basis: str, currency: str) -> None:
        """内存版本轮依据冻结：过**同一份**形式校验，再把元组记在运行上。

        不跟 Postgres 版各写一份校验：那会让内存测试比真实部署宽（戒得掉的形状
        在这里能存进去，到数据库那边才撞 CHECK），也不会两份词表慢慢漂移。
        """
        from bi_agent.listing_audit.repository import validate_audit_basis

        run = self._require_run(run_id)
        clean_roster, clean_expectations = validate_audit_basis(
            subject_id=subject_id, fingerprint=fingerprint, roster=roster,
            expectations=expectations, price_basis=price_basis, currency=currency)
        run["listing_audit_basis"] = {
            "subject_id": subject_id, "fingerprint": fingerprint,
            "roster": clean_roster, "expectations": clean_expectations,
            "price_basis": price_basis, "currency": currency}

    def find_reusable_run(self, *, subject_id: str, fingerprint: str):
        for run in self.runs.values():
            if (run.get("subject_id") == subject_id
                    and run.get("request_fingerprint") == fingerprint
                    and run.get("status") == RunStatus.SUCCEEDED.value):
                return run["id"]
        return None

    def finish(self, run_id: UUID, completion: RunCompletion) -> None:
        completion = self._revalidate_completion(completion)
        if completion.status is RunStatus.RUNNING:
            raise ValueError("finish_requires_terminal_status")
        self._validate_state(completion.state)
        self._validate_event(completion.payload)
        run = self._require_current_revision(run_id, completion.expected_revision)
        revision = completion.expected_revision + 1
        now = _now()
        run.update({
            "status": completion.status.value,
            "current_node": completion.node,
            "revision": revision,
            "state": deepcopy(completion.state),
            "error_code": completion.error_code,
            # 终止原因只在给出时覆盖，与 Postgres 的 coalesce 同语义。
            "termination_reason": (completion.termination_reason
                                   or run.get("termination_reason")),
            "updated_at": now,
            "completed_at": now,
        })
        self.events[run_id].append(_event(
            run_id=run_id,
            revision=revision,
            node=completion.node,
            event_type=(RunEventType.FAILED if completion.status is RunStatus.FAILED
                        else RunEventType.COMPLETED),
            status=completion.status,
            payload=deepcopy(completion.payload),
            created_at=now,
        ))

    def _check_domain_node(self, domain: str, node: object) -> None:
        """未登记领域能写的节点：创建时就拒，不等数据库。

        004 的 `current_node` 是无 CHECK 的文本列，领域与节点的配对只能在
        进入 Store 的这一处核。`transition` / `finish` 不做同样的反查：
        既有契约明确要求它们不追加 IO，后续节点由各领域图在建链时
        自己对照注册表（见 commerce.graph 的固定链与对应用例）。
        """
        if node is not None and not allows_node(domain, node):
            raise ValueError("unsafe_persistence_payload")

    def _require_run(self, run_id: UUID) -> dict[str, object]:
        try:
            return self.runs[run_id]
        except KeyError as error:
            raise RunNotFound() from error

    def _require_current_revision(
        self, run_id: UUID, expected_revision: int,
    ) -> dict[str, object]:
        run = self._require_run(run_id)
        if run["revision"] != expected_revision:
            raise StaleRunRevision()
        return run

    def _has_run_context(self, record: NewQueryRun) -> bool:
        return any(
            run["user_message_id"] == record.user_message_id
            and run["domain"] == record.domain
            and run["attempt_no"] == record.attempt_no
            for run in self.runs.values()
        )

    def _validate_normalized_request(self, value: object) -> None:
        validate_normalized_request(value)
        self._reject_forbidden_values(value)

    def _validate_state(self, value: object) -> None:
        validate_persisted_state(value)
        self._reject_forbidden_values(value)

    def _validate_event(self, value: object) -> None:
        validate_event_payload(value)
        self._reject_forbidden_values(value)

    def _validate_artifact(self, value: object, artifact_type: str) -> None:
        validate_artifact_payload(value, artifact_type)
        self._reject_forbidden_values(value)

    def _validate_coverage(self, value: object) -> None:
        validate_coverage_payload(value)
        self._reject_forbidden_values(value)

    def _reject_forbidden_values(self, value: object) -> None:
        if isinstance(value, dict):
            for nested in value.values():
                self._reject_forbidden_values(nested)
        elif isinstance(value, list):
            for nested in value:
                self._reject_forbidden_values(nested)
        elif isinstance(value, str) and value in self._forbidden_values:
            raise ValueError("unsafe_persistence_payload")

    @staticmethod
    def _revalidate_new_run(record: NewQueryRun) -> NewQueryRun:
        try:
            return NewQueryRun.model_validate(record.model_dump(warnings=False))
        except ValidationError as error:
            raise ValueError("unsafe_persistence_payload") from error

    @staticmethod
    def _revalidate_transition(transition: RunTransition) -> RunTransition:
        try:
            return RunTransition.model_validate(transition.model_dump(warnings=False))
        except ValidationError as error:
            if "normalized_request_mismatch" in str(error):
                raise ValueError("normalized_request_mismatch") from error
            raise ValueError("unsafe_persistence_payload") from error

    @staticmethod
    def _revalidate_artifact(artifact: NewArtifact) -> NewArtifact:
        try:
            return NewArtifact.model_validate(artifact.model_dump(warnings=False))
        except ValidationError as error:
            raise ValueError("unsafe_persistence_payload") from error

    @staticmethod
    def _revalidate_completion(completion: RunCompletion) -> RunCompletion:
        try:
            return RunCompletion.model_validate(completion.model_dump(warnings=False))
        except ValidationError as error:
            raise ValueError("unsafe_persistence_payload") from error


def _event(*, run_id: UUID, revision: int, node: str, event_type: RunEventType,
           status: RunStatus, payload: dict[str, object],
           created_at: datetime) -> dict[str, object]:
    return {
        "run_id": run_id,
        "revision": revision,
        "node": node,
        "event_type": event_type.value,
        "status": status.value,
        "payload": deepcopy(payload),
        "created_at": created_at,
    }


def _now() -> datetime:
    return datetime.now(timezone.utc)
