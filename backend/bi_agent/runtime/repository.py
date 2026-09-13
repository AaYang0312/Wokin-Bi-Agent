"""PostgreSQL implementation of the safe query-run persistence contract."""

from __future__ import annotations

from collections.abc import Collection
from typing import Any
from uuid import UUID, uuid4

from psycopg import errors
from pydantic import ValidationError
from psycopg.types.json import Jsonb

from .artifacts import TERMINATION_REASONS, verify_chart_pairing
from .domain_registry import allows_artifact_type, allows_node
from .models import (
    ArtifactPersistenceError,
    ArtifactRef,
    NewArtifact,
    NewQueryRun,
    RunCompletion,
    RunContextNotFound,
    RunEventType,
    RunNotFound,
    RunStatus,
    RunTransition,
    SchemaOutdated,
    StaleRunRevision,
    validate_artifact_payload,
    validate_coverage_payload,
    validate_event_payload,
    validate_normalized_request,
    validate_persisted_state,
    transition_normalized_request,
)


class PostgresQueryRunStore:
    """Persist the public, validated projection of a query run."""

    def __init__(self, conn: Any, *, forbidden_values: Collection[str]) -> None:
        self.conn = conn
        self._forbidden_values = frozenset(value for value in forbidden_values if value)
        self._vocabulary_checked = False
        if not self._forbidden_values:
            raise ValueError("forbidden_values_required")

    def _ensure_termination_vocabulary(self) -> None:
        """确认库里的终止原因 CHECK 已经认识本进程的码表。

        009 不能改写（已应用的迁移），新增原因码由 014 重新声明完整 CHECK。库没跑 014
        就跑新代码时，一次正常的“能力未开通”查询会在收尾写入上撞 CHECK，被包成看不出
        原因的失败。这里提前一次、按进程缓存地把它报成 schema_outdated。

        读不到 `pg_constraint`（权限或对象缺失）时不额外拦路：那时仍由数据库自己的
        CHECK 兜底，行为与今天一致。
        """
        if self._vocabulary_checked:
            return
        self._vocabulary_checked = True
        try:
            row = self.conn.execute(
                "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                "WHERE conrelid = 'bi.query_runs'::regclass "
                "  AND conname = 'query_runs_termination_reason'").fetchone()
        except errors.Error:
            return
        definition = str(row[0]) if row is not None else ""
        missing = [code for code in sorted(TERMINATION_REASONS)
                   if definition and f"'{code}'" not in definition]
        if missing:
            raise SchemaOutdated(f"schema_outdated:{missing[0]}")

    def create_run(self, record: NewQueryRun) -> UUID:
        record = self._revalidate_new_run(record)
        self._validate_normalized_request(record.normalized_request)
        self._validate_state(record.state)
        self._check_domain_node(record.domain, record.state.get("node"))
        # 契约校验先行，然后才碰库：带着非法载荷去探测 schema 只是把错因搅浑。
        self._ensure_termination_vocabulary()
        run_id = uuid4()
        try:
            row = self.conn.execute(
                """INSERT INTO bi.query_runs (
                       id, chat_id, user_message_id, subject_id, tool_call_id, domain,
                       attempt_no, normalized_request, state,
                       root_request_id, request_fingerprint, recovery_count
                   )
                   SELECT %s, c.id, m.id, c.subject_id, %s, %s, %s, %s, %s, %s, %s, %s
                   FROM bi.app_messages AS m
                   JOIN bi.app_chats AS c ON c.id = m.chat_id
                   WHERE m.id = %s AND c.id = %s AND m.role = 'user' AND c.subject_id = %s
                   RETURNING id""",
                (
                    run_id,
                    record.tool_call_id,
                    record.domain,
                    record.attempt_no,
                    Jsonb(record.normalized_request),
                    Jsonb(record.state),
                    # 身份缺失时退化成"本次运行就是根请求"，旧写入路径不受影响。
                    (record.identity.root_request_id if record.identity else run_id),
                    (record.identity.request_fingerprint if record.identity else None),
                    (record.identity.recovery_count if record.identity else 0),
                    record.user_message_id,
                    record.chat_id,
                    record.subject_id,
                ),
            ).fetchone()
        except errors.UniqueViolation as error:
            raise ValueError("duplicate_query_run") from error
        if row is None:
            raise RunContextNotFound()
        if record.provenance is not None:
            self._insert_provenance(row[0], record.provenance)
        return row[0]

    def _insert_provenance(self, run_id: UUID, provenance) -> None:
        """血缘单独一行：版本与来源批次不混进状态 jsonb，也不进模型载荷。"""
        self.conn.execute(
            """INSERT INTO bi.query_provenance (
                   run_id, template_id, template_version, metric_version, schema_version,
                   catalog_version, mapping_version, policy_version, graph_version,
                   source_batches, data_as_of, source_registry_version,
                   basis_signature, quality_rule
               ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (run_id) DO UPDATE SET
                   template_id = EXCLUDED.template_id,
                   template_version = EXCLUDED.template_version,
                   metric_version = EXCLUDED.metric_version,
                   schema_version = EXCLUDED.schema_version,
                   catalog_version = EXCLUDED.catalog_version,
                   mapping_version = EXCLUDED.mapping_version,
                   policy_version = EXCLUDED.policy_version,
                   graph_version = EXCLUDED.graph_version,
                   source_batches = EXCLUDED.source_batches,
                   data_as_of = EXCLUDED.data_as_of,
                   source_registry_version = EXCLUDED.source_registry_version,
                   basis_signature = EXCLUDED.basis_signature,
                   quality_rule = EXCLUDED.quality_rule""",
            (run_id, provenance.template_id, provenance.template_version,
             provenance.metric_version, provenance.schema_version,
             provenance.catalog_version, provenance.mapping_version,
             provenance.policy_version, provenance.graph_version,
             list(provenance.source_batches), provenance.data_as_of,
             provenance.source_registry_version, list(provenance.basis_signature),
             provenance.quality_rule or None),
        )

    def record_diagnostic(self, run_id: UUID, *, template_id: str, sql_text: str,
                          parameters: dict[str, object]) -> UUID:
        """受控诊断记录：SQL 与参数只进这里，绝不写进模型消息或事件文本。"""
        diagnostic_id = uuid4()
        self.conn.execute(
            "INSERT INTO bi.query_diagnostics (id, run_id, template_id, sql_text, parameters) "
            "VALUES (%s, %s, %s, %s, %s)",
            (diagnostic_id, run_id, template_id, sql_text, Jsonb(parameters)),
        )
        return diagnostic_id

    def record_provenance(self, run_id: UUID, *, provenance, identity) -> None:
        """请求解析完成后再落血缘与身份：创建时还没有规范化请求可指纹化。"""
        if provenance is not None:
            self._insert_provenance(run_id, provenance)
        if identity is not None:
            self.conn.execute(
                """UPDATE bi.query_runs
                   SET root_request_id = %s, request_fingerprint = %s,
                       recovery_count = %s
                   WHERE id = %s""",
                (identity.root_request_id, identity.request_fingerprint,
                 identity.recovery_count, run_id),
            )

    def record_listing_audit_basis(self, run_id: UUID, *, subject_id: str,
                                   fingerprint: str | None, roster, expectations,
                                   price_basis: str, currency: str) -> None:
        """上架复核（Task 9）的本轮依据：roster + 用户本轮目标价，同一事务落库。

        为什么这条写路径在 Store 而不在复核图里：两张表的 `run_id` 外键指向
        `bi.query_runs`，而运行记录只由本 Store 写。图绕过 Store 直接写，内存 Store
        下就会 FK 违反（真实部署反而不会报，因为它用的是本类）——一个只在测试里
        通过的写入路径不是写入路径。形式校验走 `listing_audit.repository` 那一份，
        与内存 Store 共用，两种实现不会在"能不能写"上漂移。
        """
        from bi_agent.listing_audit.repository import write_audit_basis

        # 运行行的存在性不在这里重查：`run_id` 的外键就是凭据，与 `save_artifact` 同一
        # 处置——违反时报 RunNotFound，而不是给一个不存在的运行记上依据。
        try:
            write_audit_basis(self.conn, run_id=run_id, subject_id=subject_id,
                              fingerprint=fingerprint, roster=roster,
                              expectations=expectations, price_basis=price_basis,
                              currency=currency)
        except errors.ForeignKeyViolation:
            raise RunNotFound() from None
        except errors.Error:
            raise ArtifactPersistenceError() from None

    def find_reusable_run(self, *, subject_id: str, fingerprint: str) -> UUID | None:
        """只有同一指纹的成功运行才可复用：指纹已含授权范围与数据版本。"""
        row = self.conn.execute(
            """SELECT id FROM bi.query_runs
               WHERE subject_id = %s AND request_fingerprint = %s
                 AND status = 'succeeded'
               ORDER BY started_at DESC LIMIT 1""",
            (subject_id, fingerprint),
        ).fetchone()
        return row[0] if row is not None else None

    def transition(self, run_id: UUID, transition: RunTransition) -> None:
        transition = self._revalidate_transition(transition)
        self._validate_state(transition.state)
        normalized_request = transition_normalized_request(transition)
        if normalized_request is not None:
            self._validate_normalized_request(normalized_request)
        self._validate_event(transition.payload)
        with self.conn.transaction():
            row = self.conn.execute(
                """UPDATE bi.query_runs
                   SET status = %s, current_node = %s, revision = revision + 1,
                       normalized_request = COALESCE(%s, normalized_request), state = %s,
                       error_code = %s, updated_at = now()
                   WHERE id = %s AND revision = %s
                   RETURNING revision""",
                (
                    transition.status.value,
                    transition.node,
                    Jsonb(normalized_request) if normalized_request is not None else None,
                    Jsonb(transition.state),
                    transition.error_code,
                    run_id,
                    transition.expected_revision,
                ),
            ).fetchone()
            if row is None:
                self._raise_missing_or_stale(run_id)
            self._insert_event(
                run_id=run_id,
                revision=row[0],
                node=transition.node,
                event_type=transition.event_type,
                status=transition.status,
                payload=transition.payload,
            )

    def save_artifact(self, run_id: UUID, artifact: NewArtifact) -> ArtifactRef:
        artifact = self._revalidate_artifact(artifact)
        self._validate_artifact(artifact.payload, artifact.artifact_type)
        if artifact.coverage is not None:
            self._validate_coverage(artifact.coverage)
        artifact_id = uuid4()
        try:
            # 领域能产出哪种 Artifact 由 `domain_registry` 一处定：只校全局类型白名单的话，
            # business_query 就能写出 price_audit，“按领域白名单”只剩一句注释。这条读与
            # 下面的 INSERT 共用同一套错误映射：读失败不能变成另一种故噪。
            row = self.conn.execute(
                "SELECT domain FROM bi.query_runs WHERE id = %s", (run_id,)).fetchone()
            if row is None:
                raise RunNotFound()
            if not allows_artifact_type(str(row[0]), artifact.artifact_type):
                raise ValueError("unsafe_persistence_payload")
            if artifact.artifact_type == "chart_spec":
                # “图表与数据集同版本”要能从已落库的行里核出来，不能只信调用方：
                # 与内存 Store 跑同一个 `verify_chart_pairing`，否则内存测试比真实
                # 部署宽，而数据库那份 CHECK 只能验证两个列非空。
                dataset = self.conn.execute(
                    """SELECT id, run_id, artifact_type, data_as_of, coverage
                       FROM bi.query_artifacts WHERE id = %s""",
                    (artifact.dataset_ref,)).fetchone()
                verify_chart_pairing(
                    None if dataset is None else {
                        "id": dataset[0], "run_id": dataset[1],
                        "artifact_type": dataset[2], "data_as_of": dataset[3],
                        "coverage": dataset[4]},
                    run_id=run_id, chart_version=int(artifact.chart_version or 0),
                    data_as_of=artifact.data_as_of, coverage=artifact.coverage)
            self.conn.execute(
                """INSERT INTO bi.query_artifacts (
                       id, run_id, artifact_type, payload, data_as_of, coverage,
                       dataset_ref, chart_version
                   ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
                (
                    artifact_id,
                    run_id,
                    artifact.artifact_type,
                    Jsonb(artifact.payload),
                    artifact.data_as_of,
                    Jsonb(artifact.coverage),
                    artifact.dataset_ref,
                    artifact.chart_version,
                ),
            )
        except errors.ForeignKeyViolation:
            raise RunNotFound() from None
        except errors.Error:
            raise ArtifactPersistenceError() from None
        return ArtifactRef(id=artifact_id, type=artifact.artifact_type)

    def finish(self, run_id: UUID, completion: RunCompletion) -> None:
        completion = self._revalidate_completion(completion)
        if completion.status is RunStatus.RUNNING:
            raise ValueError("finish_requires_terminal_status")
        self._validate_state(completion.state)
        self._validate_event(completion.payload)
        with self.conn.transaction():
            row = self.conn.execute(
                """UPDATE bi.query_runs
                   SET status = %s, current_node = %s, revision = revision + 1,
                       state = %s, error_code = %s, updated_at = now(), completed_at = now(),
                       termination_reason = coalesce(%s, termination_reason)
                   WHERE id = %s AND revision = %s
                   RETURNING revision""",
                (
                    completion.status.value,
                    completion.node,
                    Jsonb(completion.state),
                    completion.error_code,
                    completion.termination_reason,
                    run_id,
                    completion.expected_revision,
                ),
            ).fetchone()
            if row is None:
                self._raise_missing_or_stale(run_id)
            self._insert_event(
                run_id=run_id,
                revision=row[0],
                node=completion.node,
                event_type=(RunEventType.FAILED if completion.status is RunStatus.FAILED
                            else RunEventType.COMPLETED),
                status=completion.status,
                payload=completion.payload,
            )

    def _check_domain_node(self, domain: str, node: object) -> None:
        """未登记领域能写的节点：创建时就拒，不等数据库。

        004 的 `current_node` 是无 CHECK 的文本列，领域与节点的配对只能在
        进入 Store 的这一处核。`transition` / `finish` 不做同样的反查：
        既有契约明确要求它们不追加 IO，后续节点由各领域图在建链时
        自己对照注册表（见 commerce.graph 的固定链与对应用例）。
        """
        if node is not None and not allows_node(domain, node):
            raise ValueError("unsafe_persistence_payload")

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
        # 载荷形状由类型判到哪一个 schema：图表与数据集是两份独立契约。
        validate_artifact_payload(value, artifact_type)
        self._reject_forbidden_values(value)

    def _validate_coverage(self, value: object) -> None:
        validate_coverage_payload(value)
        self._reject_forbidden_values(value)

    def _raise_missing_or_stale(self, run_id: UUID) -> None:
        row = self.conn.execute(
            "SELECT 1 FROM bi.query_runs WHERE id = %s", (run_id,)
        ).fetchone()
        if row is None:
            raise RunNotFound()
        raise StaleRunRevision()

    def _insert_event(self, *, run_id: UUID, revision: int, node: str,
                      event_type: RunEventType, status: RunStatus,
                      payload: dict[str, object]) -> None:
        self.conn.execute(
            """INSERT INTO bi.query_run_events (
                   run_id, revision, node, event_type, status, payload
               ) VALUES (%s, %s, %s, %s, %s, %s)""",
            (run_id, revision, node, event_type.value, status.value, Jsonb(payload)),
        )

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
