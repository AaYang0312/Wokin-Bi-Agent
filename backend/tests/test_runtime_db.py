"""Database checks for the query-run persistence migration.

Each test owns an administrator transaction and rolls it back so the
independent local ``*_test`` database remains unchanged.  The same guardrails
as ``test_db.py`` prevent this module from connecting to a production host.
"""

import json
import os
import re
import traceback
from decimal import Decimal
import unittest
from unittest import mock
from contextlib import nullcontext
from datetime import datetime, timedelta
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

BEIJING = ZoneInfo("Asia/Shanghai")

import psycopg

from .dbfixtures import connect_test_db
from .fakeconn import P1_REF, S1_REF, S2_REF, price_audit_payload
from .test_exploration import (
    BASELINE_ARTIFACT_TYPES, BASELINE_DOMAINS, BASELINE_TERMINATION_REASONS, COST_METRIC,
    DAY_COST, EXPLORATION_PAYLOAD_KEYS,
    EXPLORATION_ARTIFACT_TYPE, EXPLORATION_DOMAIN, EXPLORATION_NODES, GRAPH_QUESTION,
    EXPLORATION_TERMINATION_REASONS, SHOP_COST, SHOP_ID_COLUMN, SHOP_REF_COLUMN,
    ExplorationLivePlanFixture, exploration_payload, graph_context, graph_versions,
    project, ready_request, repository_entries, selection_for, server_plan,
    validate_exploration, DAY_SHOP_DAILY, SHOP_SHOP_DAILY)
from bi_agent.runtime import PostgresQueryRunStore
from bi_agent.runtime.models import (
    ArtifactPersistenceError,
    NewArtifact,
    NewQueryRun,
    RunCompletion,
    RunContextNotFound,
    RunEventType,
    RunNotFound,
    RunStatus,
    RunTransition,
    StaleRunRevision,
)


LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}
MIGRATION = Path(__file__).parents[1] / "sql" / "004_query_runtime.sql"
# 受控 SQL 探索的运行契约迁移（计划 Task 5 Step 3）：只在本机 `*_test` 库里重放。
MIGRATION_020 = "020_controlled_sql_exploration.sql"


class RuntimeStoreValidationTests(unittest.TestCase):
    """Ingestion checks that run without a configured PostgreSQL instance."""

    def setUp(self):
        self.store = PostgresQueryRunStore(None, forbidden_values={"S1", "ERP-P-9"})

    def test_create_run_revalidates_constructed_record_before_database_access(self):
        record = NewQueryRun.model_construct(
            chat_id=uuid4(),
            user_message_id=uuid4(),
            subject_id="u1",
            tool_call_id="call_1",
            domain="business_query",
            attempt_no=1,
            normalized_request={},
            state={"message": "请查询店铺 S1 的销售额"},
        )

        with self.assertRaisesRegex(ValueError, "^unsafe_persistence_payload$"):
            self.store.create_run(record)

    def test_transition_revalidates_constructed_command_before_database_access(self):
        transition = RunTransition.model_construct(
            expected_revision=0,
            node="resolve_parameters",
            event_type=RunEventType.TRANSITIONED,
            status=RunStatus.RUNNING,
            state={"node": "resolve_parameters", "revision": 1},
            payload={"reasoning_content": "opaque"},
            error_code=None,
        )

        with self.assertRaisesRegex(ValueError, "^unsafe_persistence_payload$"):
            self.store.transition(uuid4(), transition)

    def test_transition_revalidates_constructed_normalized_request_before_database_access(self):
        transition = RunTransition.model_construct(
            expected_revision=0,
            node="resolve_parameters",
            event_type=RunEventType.TRANSITIONED,
            status=RunStatus.RUNNING,
            state={
                "node": "resolve_parameters",
                "revision": 1,
                "normalized_request": {"shop_refs": ["S1"]},
            },
            normalized_request={"shop_refs": ["S1"]},
            payload={},
            error_code=None,
        )

        with self.assertRaisesRegex(ValueError, "^unsafe_persistence_payload$"):
            self.store.transition(uuid4(), transition)

    def test_transition_rejects_mismatched_normalized_request_before_database_access(self):
        transition = RunTransition.model_construct(
            expected_revision=0,
            node="validate_parameters",
            event_type=RunEventType.TRANSITIONED,
            status=RunStatus.RUNNING,
            state={
                "node": "validate_parameters",
                "revision": 1,
                "normalized_request": {"shop_refs": [S1_REF]},
            },
            normalized_request={"shop_refs": [S2_REF]},
            payload={},
            error_code=None,
        )

        with self.assertRaisesRegex(ValueError, "^normalized_request_mismatch$"):
            self.store.transition(uuid4(), transition)

    def test_transition_derives_top_level_normalized_request_from_state_without_io(self):
        normalized_request = {"shop_refs": [S2_REF]}
        transition = RunTransition(
            expected_revision=0,
            node="resolve_parameters",
            status=RunStatus.RUNNING,
            state={
                "node": "resolve_parameters",
                "revision": 1,
                "normalized_request": normalized_request,
            },
        )

        class Cursor:
            def fetchone(self):
                return (1,)

        class CapturingConnection:
            def __init__(self):
                self.calls = []

            def transaction(self):
                return nullcontext()

            def execute(self, statement, parameters):
                self.calls.append((statement, parameters))
                return Cursor()

        conn = CapturingConnection()
        store = PostgresQueryRunStore(conn, forbidden_values={"S1", "ERP-P-9"})

        store.transition(uuid4(), transition)

        update_parameters = conn.calls[0][1]
        self.assertEqual(update_parameters[2].obj, normalized_request)
        self.assertEqual(update_parameters[3].obj["normalized_request"], normalized_request)

    def test_transition_revalidates_malformed_constructed_state_before_database_access(self):
        transition = RunTransition.model_construct(
            expected_revision=0,
            node="resolve_parameters",
            event_type=RunEventType.TRANSITIONED,
            status=RunStatus.RUNNING,
            state=[],
            normalized_request={},
            payload={},
            error_code=None,
        )

        with self.assertRaisesRegex(ValueError, "^unsafe_persistence_payload$"):
            self.store.transition(uuid4(), transition)

    def test_save_artifact_revalidates_constructed_command_before_database_access(self):
        artifact = NewArtifact.model_construct(
            artifact_type="metric_result",
            payload={"status": "ok", "data": [{"product_id": "ERP-P-9"}]},
            data_as_of=None,
            coverage=None,
        )

        with self.assertRaisesRegex(ValueError, "^unsafe_persistence_payload$"):
            self.store.save_artifact(uuid4(), artifact)

    def test_finish_revalidates_constructed_running_status_before_database_access(self):
        completion = RunCompletion.model_construct(
            expected_revision=0,
            node="finalize",
            status="running",
            state={"node": "finalize", "status": "running", "revision": 1},
            payload={},
            error_code=None,
        )

        with self.assertRaisesRegex(ValueError, "^finish_requires_terminal_status$"):
            self.store.finish(uuid4(), completion)

    def test_store_requires_forbidden_values(self):
        with self.assertRaisesRegex(ValueError, "^forbidden_values_required$"):
            PostgresQueryRunStore(None, forbidden_values=set())

    def test_artifact_type_must_be_allowed_for_the_run_domain(self):
        """Postgres 侧同一道门：先看运行记录的领域，不匹配就不发 INSERT。

        只靠 009 的全局类型 CHECK 拦不住“领域对但类型不对”：business_query 发一张
        price_audit 在数据库看是完全合法的一行。
        """
        calls: list[str] = []

        class Row:
            @staticmethod
            def fetchone():
                return ("business_query",)

        class DomainConnection:
            def execute(self, statement, _parameters):
                calls.append(" ".join(str(statement).split()))
                return Row()

        store = PostgresQueryRunStore(DomainConnection(), forbidden_values={"S1"})

        with self.assertRaisesRegex(ValueError, "^unsafe_persistence_payload$"):
            # 载荷合法：这里唯一该被拦下的理由是"这条运行属于 business_query 领域"。
            store.save_artifact(uuid4(), NewArtifact(
                artifact_type="price_audit", payload=price_audit_payload()))
        self.assertEqual(len(calls), 1, "领域不匹配时不能走到 INSERT")
        self.assertIn("SELECT domain", calls[0])

        store.save_artifact(uuid4(), NewArtifact(payload={"status": "ok"}))
        self.assertEqual(len(calls), 3, "本领域允许的类型照旧写入")
        self.assertIn("INSERT INTO bi.query_artifacts", calls[2])

    def test_artifact_foreign_key_failure_is_a_safe_missing_run_error(self):
        marker = "repository-secret-marker"

        class MissingRunConnection:
            def execute(self, _statement, _parameters):
                raise psycopg.errors.ForeignKeyViolation(marker)

        store = PostgresQueryRunStore(MissingRunConnection(), forbidden_values={"S1"})

        with self.assertRaises(RunNotFound) as context:
            store.save_artifact(uuid4(), NewArtifact(payload={"status": "ok"}))

        self.assertEqual(str(context.exception), "run_not_found")
        self.assertIsNone(context.exception.__cause__)
        self.assertTrue(context.exception.__suppress_context__)
        self.assertNotIn(marker, "".join(traceback.format_exception(context.exception)))

    def test_artifact_database_failure_is_sanitized(self):
        marker = "repository-secret-marker"

        class FailingArtifactConnection:
            def execute(self, _statement, _parameters):
                raise psycopg.errors.SyntaxError(marker)

        store = PostgresQueryRunStore(FailingArtifactConnection(), forbidden_values={"S1"})

        with self.assertRaises(ArtifactPersistenceError) as context:
            store.save_artifact(uuid4(), NewArtifact(payload={"status": "ok"}))

        self.assertEqual(str(context.exception), "artifact_persistence_error")
        self.assertIsNone(context.exception.__cause__)
        self.assertTrue(context.exception.__suppress_context__)
        self.assertNotIn(marker, "".join(traceback.format_exception(context.exception)))


class RuntimeDatabaseFixture:
    def setUp(self):
        self.conn = connect_test_db(self)

    def _seed_user_message(self, subject: str = "u1"):
        chat_id = uuid4()
        message_id = uuid4()
        self.conn.execute(
            "INSERT INTO bi.app_chats(id, subject_id, title) VALUES (%s, %s, '查询')",
            (chat_id, subject),
        )
        self.conn.execute(
            "INSERT INTO bi.app_messages(id, chat_id, role, content, status) "
            "VALUES (%s, %s, 'user', '查询销售额', 'complete')",
            (message_id, chat_id),
        )
        return chat_id, message_id


@unittest.skipUnless(os.getenv("BI_TEST_ADMIN_DSN"), "未配置独立测试数据库")
class RuntimeDatabaseTests(RuntimeDatabaseFixture, unittest.TestCase):
    def test_runtime_tables_have_constraints_and_cascade_from_chat(self):
        """A run and every child record disappear when its chat is deleted."""
        chat_id, message_id = self._seed_user_message()
        run_id = uuid4()
        artifact_id = uuid4()
        self.conn.execute(
            "INSERT INTO bi.query_runs "
            "(id, chat_id, user_message_id, subject_id, tool_call_id, attempt_no) "
            "VALUES (%s, %s, %s, 'u1', 'call_1', 1)",
            (run_id, chat_id, message_id),
        )
        self.conn.execute(
            "INSERT INTO bi.query_run_events "
            "(run_id, revision, node, event_type, status) "
            "VALUES (%s, 1, 'received', 'entered', 'running')", (run_id,),
        )
        self.conn.execute(
            "INSERT INTO bi.query_artifacts (id, run_id, artifact_type, payload) "
            "VALUES (%s, %s, 'metric_result', '{\"status\":\"ok\"}')",
            (artifact_id, run_id),
        )

        for statement, parameters, error in (
            (
                "INSERT INTO bi.query_runs "
                "(id, chat_id, user_message_id, subject_id, tool_call_id, attempt_no) "
                "VALUES (%s, %s, %s, 'u1', 'call_2', 0)",
                (uuid4(), chat_id, message_id),
                psycopg.errors.CheckViolation,
            ),
            (
                "INSERT INTO bi.query_runs "
                "(id, chat_id, user_message_id, subject_id, tool_call_id, domain, attempt_no) "
                "VALUES (%s, %s, %s, 'u1', 'call_2', 'other_domain', 2)",
                (uuid4(), chat_id, message_id),
                psycopg.errors.CheckViolation,
            ),
            (
                "INSERT INTO bi.query_runs "
                "(id, chat_id, user_message_id, subject_id, tool_call_id, status, attempt_no) "
                "VALUES (%s, %s, %s, 'u1', 'call_2', 'unknown', 2)",
                (uuid4(), chat_id, message_id),
                psycopg.errors.CheckViolation,
            ),
            (
                "INSERT INTO bi.query_runs "
                "(id, chat_id, user_message_id, subject_id, tool_call_id, revision, attempt_no) "
                "VALUES (%s, %s, %s, 'u1', 'call_2', -1, 2)",
                (uuid4(), chat_id, message_id),
                psycopg.errors.CheckViolation,
            ),
            (
                "INSERT INTO bi.query_run_events "
                "(run_id, revision, node, event_type, status) "
                "VALUES (%s, 0, 'received', 'entered', 'running')",
                (run_id,),
                psycopg.errors.CheckViolation,
            ),
            (
                "INSERT INTO bi.query_run_events "
                "(run_id, revision, node, event_type, status) "
                "VALUES (%s, 2, 'received', 'unknown', 'running')",
                (run_id,),
                psycopg.errors.CheckViolation,
            ),
            (
                "INSERT INTO bi.query_run_events "
                "(run_id, revision, node, event_type, status) "
                "VALUES (%s, 2, 'received', 'entered', 'unknown')",
                (run_id,),
                psycopg.errors.CheckViolation,
            ),
            (
                "INSERT INTO bi.query_artifacts (id, run_id, artifact_type, payload) "
                "VALUES (%s, %s, 'unknown', '{}')",
                (uuid4(), run_id),
                psycopg.errors.CheckViolation,
            ),
        ):
            with self.subTest(statement=statement), self.assertRaises(error):
                with self.conn.transaction():
                    self.conn.execute(statement, parameters)

        with self.assertRaises(psycopg.errors.UniqueViolation):
            with self.conn.transaction():
                self.conn.execute(
                    "INSERT INTO bi.query_runs "
                    "(id, chat_id, user_message_id, subject_id, tool_call_id, attempt_no) "
                    "VALUES (%s, %s, %s, 'u1', 'call_duplicate', 1)",
                    (uuid4(), chat_id, message_id),
                )

        self.conn.execute("DELETE FROM bi.app_chats WHERE id=%s", (chat_id,))

        self.assertEqual(self.conn.execute(
            "SELECT count(*) FROM bi.query_runs WHERE id=%s", (run_id,)
        ).fetchone()[0], 0)
        self.assertEqual(self.conn.execute(
            "SELECT count(*) FROM bi.query_run_events WHERE run_id=%s", (run_id,)
        ).fetchone()[0], 0)
        self.assertEqual(self.conn.execute(
            "SELECT count(*) FROM bi.query_artifacts WHERE id=%s", (artifact_id,)
        ).fetchone()[0], 0)

    def test_app_role_can_manage_runtime_but_not_business_facts(self):
        """The API role writes its runtime rows but never fact-table rows."""
        chat_id, message_id = self._seed_user_message()
        run_id = uuid4()
        self.conn.execute("SET LOCAL ROLE bi_app")
        self.conn.execute(
            "INSERT INTO bi.query_runs "
            "(id, chat_id, user_message_id, subject_id, tool_call_id, attempt_no) "
            "VALUES (%s, %s, %s, 'u1', 'call_1', 1)",
            (run_id, chat_id, message_id),
        )
        self.conn.execute(
            "INSERT INTO bi.query_run_events "
            "(run_id, revision, node, event_type, status) "
            "VALUES (%s, 1, 'received', 'entered', 'running')", (run_id,),
        )
        self.conn.execute(
            "INSERT INTO bi.query_artifacts (id, run_id, artifact_type, payload) "
            "VALUES (%s, %s, 'metric_result', '{\"status\":\"ok\"}')",
            (uuid4(), run_id),
        )
        with self.assertRaises(psycopg.errors.InsufficientPrivilege):
            with self.conn.transaction():
                self.conn.execute("INSERT INTO bi.shops(shop_id) VALUES ('forbidden')")

    def test_app_role_cannot_mutate_runtime_events_or_artifacts(self):
        """Event and artifact history is append-only for the API role."""
        chat_id, message_id = self._seed_user_message()
        run_id = uuid4()
        artifact_id = uuid4()
        self.conn.execute(
            "INSERT INTO bi.query_runs "
            "(id, chat_id, user_message_id, subject_id, tool_call_id, attempt_no) "
            "VALUES (%s, %s, %s, 'u1', 'call_1', 1)",
            (run_id, chat_id, message_id),
        )
        self.conn.execute(
            "INSERT INTO bi.query_run_events "
            "(run_id, revision, node, event_type, status) "
            "VALUES (%s, 1, 'received', 'entered', 'running')", (run_id,),
        )
        self.conn.execute(
            "INSERT INTO bi.query_artifacts (id, run_id, artifact_type, payload) "
            "VALUES (%s, %s, 'metric_result', '{\"status\":\"ok\"}')",
            (artifact_id, run_id),
        )

        self.conn.execute("SET LOCAL ROLE bi_app")
        for statement, parameters in (
            ("UPDATE bi.query_run_events SET node='finalize' WHERE run_id=%s", (run_id,)),
            ("DELETE FROM bi.query_run_events WHERE run_id=%s", (run_id,)),
            ("UPDATE bi.query_artifacts SET payload='{}' WHERE id=%s", (artifact_id,)),
            ("DELETE FROM bi.query_artifacts WHERE id=%s", (artifact_id,)),
        ):
            with self.subTest(statement=statement), self.assertRaises(psycopg.errors.InsufficientPrivilege):
                with self.conn.transaction():
                    self.conn.execute(statement, parameters)

    def test_migration_is_idempotent_in_an_administrator_transaction(self):
        """Reapplying the migration does not create duplicate database objects."""
        migration = MIGRATION.read_text(encoding="utf-8")
        self.conn.execute(migration)
        self.conn.execute(migration)
        tables = self.conn.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema='bi' AND table_name IN "
            "('query_runs', 'query_run_events', 'query_artifacts') ORDER BY table_name"
        ).fetchall()
        self.assertEqual([row[0] for row in tables], [
            "query_artifacts", "query_run_events", "query_runs",
        ])
        indexes = self.conn.execute(
            "SELECT indexname FROM pg_indexes WHERE schemaname='bi' "
            "AND indexname IN ('query_runs_chat_started_idx', "
            "'query_runs_message_attempt_idx', 'query_artifacts_run_idx') "
            "ORDER BY indexname"
        ).fetchall()
        self.assertEqual([row[0] for row in indexes], [
            "query_artifacts_run_idx", "query_runs_chat_started_idx",
            "query_runs_message_attempt_idx",
        ])


@unittest.skipUnless(os.getenv("BI_TEST_ADMIN_DSN"), "未配置独立测试数据库")
class ProvenanceDatabaseTests(RuntimeDatabaseFixture, unittest.TestCase):
    """Task 3 落库契约：版本、身份与诊断记录都走真实约束。"""

    def _store(self):
        from bi_agent.runtime.repository import PostgresQueryRunStore

        return PostgresQueryRunStore(self.conn, forbidden_values={"S1", "ERP-P-9"})

    def _run(self, store, chat_id, message_id, *, domain="business_query"):
        from bi_agent.runtime.models import NewQueryRun

        return store.create_run(NewQueryRun(
            chat_id=chat_id, user_message_id=message_id, subject_id="u1",
            tool_call_id="call_1", domain=domain, attempt_no=1,
            normalized_request={"shop_refs": [S1_REF]}, state={"node": "received"},
        ))

    def test_provenance_and_identity_round_trip(self):
        from bi_agent.runtime.artifacts import (
            QueryProvenance, RequestIdentity, request_fingerprint)

        chat_id, message_id = self._seed_user_message(subject="u1")
        store = self._store()
        run_id = self._run(store, chat_id, message_id)
        provenance = QueryProvenance(catalog_version=4, source_batches=("b-2", "b-1"),
                                     data_as_of=datetime(2026, 9, 9, tzinfo=BEIJING))
        identity = RequestIdentity(
            root_request_id=run_id, attempt_no=1,
            request_fingerprint=request_fingerprint(
                subject_id="u1", allowed_shop_ids=frozenset({"S1"}),
                normalized_request={"metrics": ["paid_amount"]}, provenance=provenance))
        store.record_provenance(run_id, provenance=provenance, identity=identity)

        row = self.conn.execute(
            """SELECT root_request_id, request_fingerprint, recovery_count
               FROM bi.query_runs WHERE id=%s""", (run_id,)).fetchone()
        self.assertEqual(row[0], run_id)
        self.assertRegex(row[1], r"^[0-9a-f]{64}$")
        self.assertEqual(row[2], 0)
        stored = self.conn.execute(
            """SELECT metric_version, catalog_version, source_batches, data_as_of
               FROM bi.query_provenance WHERE run_id=%s""", (run_id,)).fetchone()
        self.assertEqual(stored[1], 4)
        self.assertEqual(sorted(stored[2]), ["b-1", "b-2"], "批次必须可按 run 反查")
        self.assertEqual(stored[3], datetime(2026, 9, 9, tzinfo=BEIJING))

    def test_same_fingerprint_not_reused_after_data_version_moves(self):
        """回填推进目录版本后，同一问题不能命中旧运行。"""
        from bi_agent.runtime.artifacts import QueryProvenance, request_fingerprint

        chat_id, message_id = self._seed_user_message(subject="u1")
        store = self._store()
        run_id = self._run(store, chat_id, message_id)
        request = {"metrics": ["paid_amount"], "start": "2026-09-01"}
        old = request_fingerprint(subject_id="u1", allowed_shop_ids=frozenset({"S1"}),
                                 normalized_request=request,
                                 provenance=QueryProvenance(catalog_version=0))
        store.record_provenance(run_id, provenance=QueryProvenance(catalog_version=0),
                               identity=None)
        self.conn.execute("UPDATE bi.query_runs SET request_fingerprint=%s, "
                          "status='succeeded' WHERE id=%s", (old, run_id))

        self.assertEqual(store.find_reusable_run(subject_id="u1", fingerprint=old), run_id)
        moved = request_fingerprint(subject_id="u1", allowed_shop_ids=frozenset({"S1"}),
                                    normalized_request=request,
                                    provenance=QueryProvenance(catalog_version=7))
        self.assertNotEqual(old, moved)
        self.assertIsNone(store.find_reusable_run(subject_id="u1", fingerprint=moved),
                          "目录版本一变就不许复用旧结果")

    def test_other_subject_cannot_reuse_the_same_request(self):
        from bi_agent.runtime.artifacts import QueryProvenance, request_fingerprint

        chat_id, message_id = self._seed_user_message(subject="u1")
        store = self._store()
        run_id = self._run(store, chat_id, message_id)
        fingerprint = request_fingerprint(
            subject_id="u1", allowed_shop_ids=frozenset({"S1"}),
            normalized_request={"metrics": ["paid_amount"]}, provenance=QueryProvenance())
        self.conn.execute("UPDATE bi.query_runs SET request_fingerprint=%s, "
                          "status='succeeded' WHERE id=%s", (fingerprint, run_id))

        self.assertIsNone(store.find_reusable_run(subject_id="u2", fingerprint=fingerprint))

    def test_database_still_refuses_unknown_domain_and_type(self):
        """应用层白名单之外，数据库 CHECK 必须仍然独立拦得住。"""
        chat_id, message_id = self._seed_user_message(subject="u1")
        with self.assertRaises(Exception):
            with self.conn.transaction():
                self.conn.execute(
                    """INSERT INTO bi.query_runs (id, chat_id, user_message_id, subject_id,
                           tool_call_id, domain, attempt_no, normalized_request, state,
                           status, revision)
                       VALUES (%s, %s, %s, 'u1', 'c', 'not_a_domain', 1, '{}', '{}',
                               'running', 0)""",
                    (uuid4(), chat_id, message_id))

    def test_termination_reason_rejects_free_text_and_accepts_code(self):
        chat_id, message_id = self._seed_user_message(subject="u1")
        store = self._store()
        run_id = self._run(store, chat_id, message_id)
        with self.assertRaises(Exception):
            with self.conn.transaction():
                self.conn.execute("UPDATE bi.query_runs SET termination_reason=%s WHERE id=%s",
                                  ("上游返回了一句很奇怪的话", run_id))
        self.conn.execute("UPDATE bi.query_runs SET termination_reason=%s WHERE id=%s",
                          ("source_quality_failed", run_id))
        self.assertEqual(self.conn.execute(
            "SELECT termination_reason FROM bi.query_runs WHERE id=%s", (run_id,)).fetchone()[0],
            "source_quality_failed")

    def test_diagnostics_are_referenced_and_never_reach_reporting(self):
        """诊断 SQL 只进受控记录：不得有 reporting 视图把它暴露出去。"""
        chat_id, message_id = self._seed_user_message(subject="u1")
        store = self._store()
        run_id = self._run(store, chat_id, message_id)
        diagnostic_id = store.record_diagnostic(
            run_id, template_id="fixed_metric_query",
            sql_text="SELECT sum(amount) FROM bi.order_payments WHERE shop_id = ANY(%s)",
            parameters={"shop_ids": ["S1"]})
        self.assertEqual(self.conn.execute(
            "SELECT template_id FROM bi.query_diagnostics WHERE id=%s",
            (diagnostic_id,)).fetchone()[0], "fixed_metric_query")
        exposed = self.conn.execute(
            "SELECT count(*) FROM information_schema.views WHERE table_schema='reporting' "
            "AND view_definition LIKE '%query_diagnostics%'").fetchone()[0]
        self.assertEqual(exposed, 0, "诊断记录不得经 reporting 暴露")


@unittest.skipUnless(os.getenv("BI_TEST_ADMIN_DSN"), "未配置独立测试数据库")
class RuntimeStoreDatabaseTests(RuntimeDatabaseFixture, unittest.TestCase):
    def test_store_creates_run_only_for_matching_user_message(self):
        chat_id, message_id = self._seed_user_message(subject="u1")
        store = PostgresQueryRunStore(self.conn, forbidden_values={"S1", "ERP-P-9"})
        run_id = store.create_run(NewQueryRun(
            chat_id=chat_id, user_message_id=message_id, subject_id="u1",
            tool_call_id="call_1", attempt_no=1,
            normalized_request={"shop_refs": [S1_REF]},
            state={"node": "received"},
        ))
        row = self.conn.execute(
            "SELECT subject_id, revision, status FROM bi.query_runs WHERE id=%s",
            (run_id,),
        ).fetchone()
        self.assertEqual(row, ("u1", 0, "running"))
        with self.assertRaises(RunContextNotFound):
            store.create_run(NewQueryRun(
                chat_id=chat_id, user_message_id=message_id, subject_id="u2",
                tool_call_id="call_2", attempt_no=2,
            ))

    def test_store_transitions_once_and_rejects_stale_revision(self):
        chat_id, message_id = self._seed_user_message()
        store = PostgresQueryRunStore(self.conn, forbidden_values={"S1", "ERP-P-9"})
        run_id = store.create_run(NewQueryRun(
            chat_id=chat_id, user_message_id=message_id, subject_id="u1",
            tool_call_id="call_1", attempt_no=1, state={"node": "received"},
        ))
        normalized_request = {"shop_refs": [S1_REF]}
        transition = RunTransition(
            expected_revision=0,
            node="resolve_parameters",
            status=RunStatus.RUNNING,
            normalized_request=normalized_request,
            state={
                "node": "resolve_parameters",
                "revision": 1,
                "normalized_request": normalized_request,
            },
        )

        store.transition(run_id, transition)

        self.assertEqual(self.conn.execute(
            "SELECT revision FROM bi.query_runs WHERE id=%s", (run_id,)
        ).fetchone()[0], 1)
        self.assertEqual(self.conn.execute(
            "SELECT revision FROM bi.query_run_events WHERE run_id=%s", (run_id,)
        ).fetchone()[0], 1)
        self.assertEqual(self.conn.execute(
            "SELECT normalized_request FROM bi.query_runs WHERE id=%s", (run_id,)
        ).fetchone()[0], normalized_request)
        with self.assertRaises(StaleRunRevision):
            store.transition(run_id, transition)
        self.assertEqual(self.conn.execute(
            "SELECT count(*) FROM bi.query_run_events WHERE run_id=%s", (run_id,)
        ).fetchone()[0], 1)

    def test_store_finishes_with_terminal_event(self):
        chat_id, message_id = self._seed_user_message()
        store = PostgresQueryRunStore(self.conn, forbidden_values={"S1", "ERP-P-9"})
        run_id = store.create_run(NewQueryRun(
            chat_id=chat_id, user_message_id=message_id, subject_id="u1",
            tool_call_id="call_1", attempt_no=1, state={"node": "received"},
        ))

        store.finish(run_id, RunCompletion(
            expected_revision=0,
            node="finalize",
            status=RunStatus.SUCCEEDED,
            state={"node": "finalize", "status": "succeeded", "revision": 1},
            payload={"result_count": 1},
        ))

        self.assertEqual(self.conn.execute(
            "SELECT revision, status, completed_at IS NOT NULL FROM bi.query_runs WHERE id=%s",
            (run_id,),
        ).fetchone(), (1, "succeeded", True))
        self.assertEqual(self.conn.execute(
            "SELECT revision, event_type FROM bi.query_run_events WHERE run_id=%s",
            (run_id,),
        ).fetchone(), (1, "completed"))

    def test_store_persists_only_public_artifact_projection(self):
        chat_id, message_id = self._seed_user_message()
        store = PostgresQueryRunStore(self.conn, forbidden_values={"S1", "ERP-P-9"})
        run_id = store.create_run(NewQueryRun(
            chat_id=chat_id, user_message_id=message_id, subject_id="u1",
            tool_call_id="call_1", attempt_no=1, state={"node": "received"},
        ))
        payload = {
            "status": "ok",
            "data": [{"shop_ref": S1_REF, "product_ref": P1_REF}],
            "entities": [{"ref": S1_REF, "kind": "shop", "display_name": "店铺A",
                          "name_source": "shop_profile"},
                         {"ref": P1_REF, "kind": "product", "display_name": "直钉枪",
                          "name_source": "archive"}],
            "catalog_version": 7,
        }

        artifact = store.save_artifact(run_id, NewArtifact(payload=payload))

        self.assertEqual(artifact.type, "metric_result")
        self.assertEqual(self.conn.execute(
            "SELECT payload FROM bi.query_artifacts WHERE id=%s", (artifact.id,)
        ).fetchone()[0], payload)
        self.assertNotIn("S1", json.dumps(payload, ensure_ascii=False))
        self.assertIn("店铺A", json.dumps(payload, ensure_ascii=False))

    def test_store_persists_chart_pairing_and_refuses_a_stale_or_foreign_dataset(self):
        """图表与数据集的配对要在**数据库行**上成立，不只是 Python 对象里对得上。

        009 给 `bi.query_artifacts` 加了 `dataset_ref` / `chart_version` 两列与一条 CHECK：
        不写这两列，chart_spec 那一行要么违反 CHECK，要么留下一张谁也不引用的图。
        另一条运行里的数据集也不能被本轮的图借去用：那会把别人的数据版本说成自己的。
        """
        from datetime import date, timedelta
        from zoneinfo import ZoneInfo

        from bi_agent.metrics import Coverage
        from bi_agent.presentation.charts import PersistedDataset, build_chart_spec
        from bi_agent.runtime.artifacts import CHART_SPEC_VERSION, ChartPairingError

        store = PostgresQueryRunStore(self.conn, forbidden_values={"S1", "ERP-P-9"})
        data_as_of = datetime(2026, 9, 8, 9, tzinfo=ZoneInfo("Asia/Shanghai"))
        coverage = Coverage(status="partial", start=date(2026, 9, 1),
                            end=date(2026, 9, 8),
                            gaps=["2026-09-04~2026-09-05"])

        def new_run(subject: str):
            chat_id, message_id = self._seed_user_message(subject=subject)
            return store.create_run(NewQueryRun(
                chat_id=chat_id, user_message_id=message_id, subject_id=subject,
                tool_call_id="call_c", domain="commerce_performance", attempt_no=1,
                state={"node": "resolve_scope"}))

        run_id = new_run("u1")
        table = store.save_artifact(run_id, NewArtifact(
            artifact_type="comparison_table", payload={"status": "ok"},
            data_as_of=data_as_of, coverage=coverage.model_dump(mode="json")))
        handle = PersistedDataset(artifact_id=table.id, artifact_type=table.type,
                                  data_as_of=data_as_of, coverage=coverage)
        payload = build_chart_spec(
            handle, kind="bar", x="platform", y="sales_amount",
            series=("platform",), unit="CNY",
            metric_basis="sales_amount|platform_payment/v1|pay_time",
            coverage_ref=handle).as_payload()

        def chart_of(**changes):
            fields = {"artifact_type": "chart_spec", "payload": dict(payload),
                      "data_as_of": data_as_of,
                      "coverage": coverage.model_dump(mode="json"),
                      "dataset_ref": table.id, "chart_version": CHART_SPEC_VERSION}
            fields.update(changes)
            return NewArtifact(**fields)

        chart = store.save_artifact(run_id, chart_of())
        row = self.conn.execute(
            "SELECT dataset_ref, chart_version FROM bi.query_artifacts WHERE id=%s",
            (chart.id,)).fetchone()
        self.assertEqual(row, (table.id, CHART_SPEC_VERSION),
                         "引用与版本必须落到列上，否则 009 的配对 CHECK 形同虚设")

        # 换一条运行去引用同一份数据集：图不能跨运行借数据版本。
        other_run = new_run("u2")
        with self.assertRaises(ChartPairingError) as refused:
            store.save_artifact(other_run, chart_of())
        self.assertEqual(refused.exception.reason, "chart_dataset_other_run")

        # 截止时刻变了就不是同一份数据集：旧引用不能给新数签名。
        with self.assertRaises(ChartPairingError) as refused:
            store.save_artifact(run_id, chart_of(
                data_as_of=data_as_of - timedelta(days=1)))
        self.assertEqual(refused.exception.reason, "chart_dataset_version_mismatch")
        with self.assertRaises(ChartPairingError) as refused:
            store.save_artifact(run_id, chart_of(coverage={
                "status": "complete", "start": "2026-09-01", "end": "2026-09-08",
                "gaps": []}))
        self.assertEqual(refused.exception.reason, "chart_dataset_version_mismatch")
        # 引用一张图表自己：009 只允许数据集被引用。
        with self.assertRaises(ChartPairingError) as refused:
            store.save_artifact(run_id, chart_of(dataset_ref=chart.id, payload={
                **payload, "dataset_ref": str(chart.id), "coverage_ref": str(chart.id)}))
        self.assertEqual(refused.exception.reason, "chart_dataset_type_invalid")

    def test_store_revalidates_constructed_commands_before_writing(self):
        chat_id, message_id = self._seed_user_message()
        store = PostgresQueryRunStore(self.conn, forbidden_values={"S1", "ERP-P-9"})
        unsafe_record = NewQueryRun.model_construct(
            chat_id=chat_id,
            user_message_id=message_id,
            subject_id="u1",
            tool_call_id="call_1",
            domain="business_query",
            attempt_no=1,
            normalized_request={},
            state={"message": "请查询店铺 S1 的销售额"},
        )

        with self.assertRaisesRegex(ValueError, "^unsafe_persistence_payload$"):
            store.create_run(unsafe_record)

        self.assertEqual(self.conn.execute(
            "SELECT count(*) FROM bi.query_runs WHERE user_message_id=%s", (message_id,)
        ).fetchone()[0], 0)


@unittest.skipUnless(os.getenv("BI_TEST_ADMIN_DSN"), "未配置独立测试数据库")
class ExplorationRoleDatabaseTests(ExplorationLivePlanFixture, unittest.TestCase):
    """计划 Task 4：只读门禁必须在**运行角色**下成立，而不是只在超级用户下。

    上面那些类都以 `postgres` 连接做断言；真实读取按 `bi_app`（API）与 `bi_reader`
    （只读消费方）跑。只证明超级用户能过门等于什么都没证明：底表拒绝、视图授权与
    写拒绝全是角色决定的。每条用例各持一个管理员事务并按 Rollback 协议退出。
    """

    def setUp(self):
        super().setUp()                                  # 连接、店铺与窗口都来自真库
        self.conn.execute("SET LOCAL ROLE bi_app")       # 与生产 API 同一身份
        self.assertEqual(str(self.conn.execute("SELECT current_user").fetchone()[0]),
                         "bi_app")

    def test_the_gate_reads_reporting_views_as_the_app_role(self):
        """成本门与只读执行在 bi_app 下过；投影后没有真店号。"""
        estimate_plan, execute_plan = repository_entries()
        plan = self.plan_for()
        estimated = estimate_plan(self.conn, plan, deadline=self.deadline())
        self.assertIsNotNone(estimated.estimated_rows)
        columns, rows = execute_plan(self.conn, estimated, deadline=self.deadline())
        self.assertTrue(rows)
        declared, safe_rows = project(columns, rows,
                                      shop_refs={self.shop_id: self.shop_ref})
        self.assertEqual([column.ref for column in declared],
                         [DAY_COST, SHOP_REF_COLUMN, "metric-cost-total"])
        self.assertEqual({row[SHOP_REF_COLUMN] for row in safe_rows}, {self.shop_ref})
        self.assertNotIn(self.shop_id,
                         [value for row in safe_rows for value in row.values()])

    def test_base_tables_stay_closed_to_the_gate_and_the_message_stays_clean(self):
        """`bi_app` 读不到业务底表：门禁把它换成稳定原因码，不外泄语句原文。"""
        estimate_plan, execute_plan = repository_entries()
        plan = server_plan('SELECT count(*) AS "n" FROM bi.orders')
        with self.assertRaises(ValueError) as caught:
            # EXPLAIN 就要权限：哪一步先拒不重要，重要的是两步都不会把数发出去。
            execute_plan(self.conn, estimate_plan(self.conn, plan,
                                                  deadline=self.deadline()),
                         deadline=self.deadline())
        self.assertEqual(str(caught.exception), "exploration_query_rejected")
        self.assertIs(caught.exception.__cause__, None)
        self.assertTrue(caught.exception.__suppress_context__)
        for leak in ("SELECT", "FROM", "bi.orders", "count", "%("):
            self.assertNotIn(leak, str(caught.exception))

    def test_server_files_stay_closed_to_the_gate(self):
        """`pg_read_file` 一类服务端读取同样只剩稳定码。"""
        estimate_plan, _ = repository_entries()
        plan = server_plan("SELECT pg_read_file('/etc/passwd') AS \"n\"")
        with self.assertRaises(ValueError) as caught:
            estimate_plan(self.conn, plan, deadline=self.deadline())
        self.assertEqual(str(caught.exception), "exploration_query_rejected")

    @unittest.skipUnless(os.getenv("BI_TEST_READER_DSN"), "未配置测试库的只读角色 DSN")
    def test_the_reader_dsn_passes_the_gate_and_still_refuses_writes(self):
        """同一道门在 `bi_reader` 自己的连接上过；写底表仍被角色拒。

        写断言是这条链上唯一必须**真执行**的拒绝：它单独占一个连接，而且后面不再发
        语句——库里报错会把当前事务置为 aborted，同一事务里再问一句拿到的就不再是
        "角色被拒"的证据。
        """
        estimate_plan, execute_plan = repository_entries()
        plan = self.plan_for()
        with psycopg.connect(os.environ["BI_TEST_READER_DSN"]) as conn:
            self.assertEqual(conn.info.user, "bi_reader")
            self.assertTrue(conn.info.dbname.endswith("_test"))
            estimated = estimate_plan(conn, plan, deadline=self.deadline())
            columns, rows = execute_plan(conn, estimated, deadline=self.deadline())
        self.assertEqual(columns, [DAY_COST, SHOP_ID_COLUMN, "metric-cost-total"])
        self.assertTrue(rows)
        with psycopg.connect(os.environ["BI_TEST_READER_DSN"]) as conn:
            with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                conn.execute("DELETE FROM bi.orders")


@unittest.skipUnless(os.getenv("BI_TEST_ADMIN_DSN"), "未配置独立测试数据库")
class ExplorationMigrationTests(RuntimeDatabaseFixture, unittest.TestCase):
    """计划 Task 5 Step 3：020 重建三份 CHECK、幂等，并且不给诊断开第二条读取通道。

    全部 DDL 都在本类的管理员外层事务里跑，结束按 Rollback 协议退出：共享测试库只保留
    已提交的那一份 020，不保留仿真用的 022。
    """

    CONSTRAINTS = ("query_runs_domain_check", "query_artifacts_artifact_type_check",
                   "query_runs_termination_reason")

    def setUp(self):
        super().setUp()
        self.migration_path = Path(__file__).parents[1] / "sql" / MIGRATION_020
        self.migration = self.migration_path.read_text(encoding="utf-8")

    def _definitions(self) -> dict[str, str]:
        rows = self.conn.execute(
            """SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint
               WHERE conname = ANY(%s)""", (list(self.CONSTRAINTS),)).fetchall()
        self.assertEqual(len(rows), len(self.CONSTRAINTS),
                         f"三份 CHECK 必须都在：{[row[0] for row in rows]}")
        return {str(row[0]): str(row[1]) for row in rows}

    def _reporting_views(self):
        return self.conn.execute(
            "SELECT table_name FROM information_schema.views "
            "WHERE table_schema='reporting' ORDER BY table_name").fetchall()

    def test_020_is_the_next_numbered_migration_after_019(self):
        """迁移编号已冻结：020 只属于本计划，而且排在 019 之后。"""
        files = sorted(path.name for path in self.migration_path.parent.glob("*.sql"))
        self.assertIn(MIGRATION_020, files)
        self.assertGreater(files.index(MIGRATION_020),
                           files.index("019_inventory_snapshots.sql"))
        self.assertEqual([name for name in files if name.startswith("020")], [MIGRATION_020],
                         "同一编号不能有两份迁移：那意味着 020 已被占用")

    def test_replaying_020_twice_never_narrows_the_whitelists(self):
        from bi_agent.runtime.artifacts import TERMINATION_REASONS
        from bi_agent.runtime.domain_registry import ARTIFACT_TYPES, domains

        self.conn.execute(self.migration)
        once = self._definitions()
        self.conn.execute(self.migration)
        self.assertEqual(self._definitions(), once, "重放不得改变约束定义")
        domain_def = once["query_runs_domain_check"]
        artifact_def = once["query_artifacts_artifact_type_check"]
        reason_def = once["query_runs_termination_reason"]
        for value in sorted(BASELINE_DOMAINS | {EXPLORATION_DOMAIN} | set(domains())):
            self.assertIn(f"'{value}'", domain_def, f"库里的领域名单缺 {value}")
        for value in sorted(BASELINE_ARTIFACT_TYPES | {EXPLORATION_ARTIFACT_TYPE}
                            | set(ARTIFACT_TYPES)):
            self.assertIn(f"'{value}'", artifact_def, f"库里的类型名单缺 {value}")
        for value in sorted(TERMINATION_REASONS | BASELINE_TERMINATION_REASONS):
            self.assertIn(f"'{value}'", reason_def, f"库里的码表缺 {value}")
        # 多一个就算宽：库里的领域取值必须逐项等于注册表，不能变成自由文本列。
        self.assertEqual(sorted(re.findall(r"'([a-z_]+)'", domain_def)), sorted(domains()))

    def test_020_keeps_values_another_lane_already_added(self):
        """020 必须能在 022 之后执行而不收缩：库里已存在的额外取值全部保留。"""
        for statement in (
            """ALTER TABLE bi.query_runs DROP CONSTRAINT IF EXISTS query_runs_domain_check;
               ALTER TABLE bi.query_runs ADD CONSTRAINT query_runs_domain_check CHECK (
                 domain IN ('business_query', 'commerce_performance',
                            'controlled_sql_exploration', 'inventory_watch',
                            'isolated_analysis', 'listing_price_audit'))""",
            """ALTER TABLE bi.query_artifacts DROP CONSTRAINT IF EXISTS
                     query_artifacts_artifact_type_check;
               ALTER TABLE bi.query_artifacts ADD CONSTRAINT
                     query_artifacts_artifact_type_check CHECK (
                 artifact_type IN ('analysis_result', 'chart_spec', 'comparison_table',
                                   'exploration_result', 'inventory_alerts',
                                   'metric_result', 'price_audit', 'trend_series'))""",
        ):
            self.conn.execute(statement)
        self.conn.execute(self.migration)
        self.conn.execute(self.migration)
        definitions = self._definitions()
        for value in ("isolated_analysis", "controlled_sql_exploration",
                      "business_query", "inventory_watch"):
            self.assertIn(f"'{value}'", definitions["query_runs_domain_check"], value)
        for value in ("analysis_result", "exploration_result", "chart_spec",
                      "inventory_alerts"):
            self.assertIn(f"'{value}'", definitions["query_artifacts_artifact_type_check"],
                          value)

    def test_020_accepts_the_new_values_and_still_rejects_unknown_ones(self):
        chat_id, message_id = self._seed_user_message(subject="u1")
        run_id = uuid4()
        self.conn.execute(
            """INSERT INTO bi.query_runs (id, chat_id, user_message_id, subject_id,
                   tool_call_id, domain, attempt_no, normalized_request, state)
               VALUES (%s, %s, %s, 'u1', 'call_1', %s, 1, '{}', '{}')""",
            (run_id, chat_id, message_id, EXPLORATION_DOMAIN))
        self.conn.execute(
            """INSERT INTO bi.query_artifacts (id, run_id, artifact_type, payload)
               VALUES (%s, %s, %s, %s::jsonb)""",
            (uuid4(), run_id, EXPLORATION_ARTIFACT_TYPE,
             json.dumps(exploration_payload(), ensure_ascii=False, default=str)))
        self.conn.execute("UPDATE bi.query_runs SET termination_reason=%s WHERE id=%s",
                          ("query_cost_exceeded", run_id))
        for reason in sorted(EXPLORATION_TERMINATION_REASONS):
            self.conn.execute("UPDATE bi.query_runs SET termination_reason=%s WHERE id=%s",
                              (reason, run_id))
        for statement, parameters in (
                ("""INSERT INTO bi.query_runs (id, chat_id, user_message_id, subject_id,
                        tool_call_id, domain, attempt_no, normalized_request, state)
                    VALUES (%s, %s, %s, 'u1', 'c', %s, 2, '{}', '{}')""",
                 (uuid4(), chat_id, message_id, "arbitary_sql")),
                ("UPDATE bi.query_runs SET termination_reason=%s WHERE id=%s",
                 ("query_was_too_slow", run_id))):
            with self.subTest(parameters=str(parameters)[-24:]), \
                    self.assertRaises(psycopg.errors.CheckViolation):
                with self.conn.transaction():
                    self.conn.execute(statement, parameters)

    def test_020_leaves_the_diagnostics_channel_private(self):
        """不新增 reporting 视图，也不给 bi_reader 任何新授权。"""
        views_before = self._reporting_views()
        self.conn.execute(self.migration)
        self.assertEqual(self._reporting_views(), views_before, "020 不得建视图")
        exposed = self.conn.execute(
            "SELECT count(*) FROM information_schema.views "
            "WHERE view_definition ILIKE '%query_diagnostics%'").fetchone()[0]
        self.assertEqual(exposed, 0, "诊断记录不得经任何视图暴露")
        privileges = self.conn.execute(
            """SELECT has_table_privilege('bi_reader', 'bi.query_diagnostics', 'SELECT'),
                       has_table_privilege('bi_reader', 'bi.query_diagnostics', 'INSERT'),
                       has_table_privilege('bi_app', 'bi.query_diagnostics', 'INSERT'),
                       has_table_privilege('public', 'bi.query_diagnostics', 'SELECT')
                   """).fetchone()
        self.assertEqual(privileges, (False, False, True, False), privileges)

    def test_exploration_run_and_artifact_round_trip_through_the_postgres_store(self):
        """Store 与库同一口径：能写进去的载荷读回来仍然没有 SQL 与真店号。"""
        chat_id, message_id = self._seed_user_message(subject="u1")
        store = PostgresQueryRunStore(self.conn, forbidden_values={"S1", "ERP-P-9"})
        run_id = store.create_run(NewQueryRun(
            chat_id=chat_id, user_message_id=message_id, subject_id="u1",
            tool_call_id="call_1", domain=EXPLORATION_DOMAIN, attempt_no=1,
            state={"node": EXPLORATION_NODES[0]}))
        ref = store.save_artifact(run_id, NewArtifact(
            artifact_type=EXPLORATION_ARTIFACT_TYPE, payload=exploration_payload()))
        row = self.conn.execute(
            "SELECT artifact_type, payload FROM bi.query_artifacts WHERE id=%s",
            (ref.id,)).fetchone()
        self.assertEqual(row[0], EXPLORATION_ARTIFACT_TYPE)
        stored = row[1] if isinstance(row[1], dict) else json.loads(row[1])
        self.assertEqual(validate_exploration(stored), stored)
        self.assertNotIn("SELECT", json.dumps(stored, ensure_ascii=False))
        diagnostic_id = store.record_diagnostic(
            run_id, template_id="exploration_sql",
            sql_text='SELECT fact."day" FROM reporting.v_product_cost_daily AS fact',
            parameters={"allowed_shop_ids": ["S1"], "limit": 100})
        self.assertEqual(self.conn.execute(
            "SELECT parameters->>'limit' FROM bi.query_diagnostics WHERE id=%s",
            (diagnostic_id,)).fetchone()[0], "100")
        store.finish(run_id, RunCompletion(
            expected_revision=0, node="finalize", status=RunStatus.SUCCEEDED, state={},
            termination_reason="succeeded"))
        self.assertEqual(self.conn.execute(
            "SELECT current_node, termination_reason FROM bi.query_runs WHERE id=%s",
            (run_id,)).fetchone(), ("finalize", "succeeded"))

    def test_record_diagnostic_reports_the_same_failures_as_the_memory_store(self):
        """运行行不存在与约束违反：两个 Store 必须长成同一个异常。"""
        store = PostgresQueryRunStore(self.conn, forbidden_values={"S1"})
        with self.assertRaises(RunNotFound):
            with self.conn.transaction():      # 外键违反会中止事务：用保存点收回去
                store.record_diagnostic(uuid4(), template_id="exploration_sql",
                                        sql_text="SELECT 1", parameters={})
        chat_id, message_id = self._seed_user_message(subject="u1")
        run_id = store.create_run(NewQueryRun(
            chat_id=chat_id, user_message_id=message_id, subject_id="u1",
            tool_call_id="call_1", domain=EXPLORATION_DOMAIN, attempt_no=1,
            state={"node": "select_schema"}))
        with self.assertRaises(ArtifactPersistenceError):
            with self.conn.transaction():
                store.record_diagnostic(run_id, template_id=" padded ",
                                       sql_text="SELECT 1", parameters={})


@unittest.skipUnless(os.getenv("BI_TEST_ADMIN_DSN"), "未配置独立测试数据库")
class ExplorationGraphDatabaseTests(ExplorationLivePlanFixture, unittest.TestCase):
    """真库上的成功路径与四类拒答：九格全走，一条真 SELECT，一份诊断，一份公开结果。

    店铺按 `platform='fxg'` 与 `reporting.v_shop_daily` 现有数据挑（`fxg` 是注册表里唯一
    “付款时间口径实测成立”的交易通道），能力标签与来源覆盖由本用例在回滚事务里造：
    readiness 走的是 `sources` + `data_quality` 那两份**已登记**契约，不是替身字面量。
    """

    PAID = "metric-paid-amount"

    def setUp(self):
        super().setUp()
        from bi_agent.data_quality import QUALITY_RULE
        from bi_agent.sources import TRADE_LIST_SOURCE

        self.quality_rule = QUALITY_RULE
        self.source = TRADE_LIST_SOURCE
        row = self.conn.execute(
            "SELECT s.shop_id, min(v.day), max(v.day) FROM reporting.v_shop_daily v "
            "JOIN bi.shops s ON s.shop_id = v.shop_id WHERE s.platform = 'fxg' "
            "  AND s.enabled GROUP BY s.shop_id "
            "ORDER BY count(DISTINCT v.day) DESC, s.shop_id LIMIT 1").fetchone()
        assert row is not None, "测试库里没有 fxg 店的 v_shop_daily 数据：无法取证"
        self.shop_id, first, last = str(row[0]), row[1], row[2]
        assert (last - first).days + 1 <= 300, f"窗口超出预算：{first}..{last}"
        self.start, self.end = first, last + timedelta(days=1)
        from bi_agent.catalog import ref_for_key

        self.shop_ref = ref_for_key("shop", self.shop_id)
        self.chat_id, self.user_message_id = uuid4(), uuid4()
        self.conn.execute("INSERT INTO bi.app_chats(id, subject_id, title) "
                          "VALUES (%s, 'exploration-subject', '探索')", (self.chat_id,))
        self.conn.execute(
            "INSERT INTO bi.app_messages(id, chat_id, role, content, status) "
            "VALUES (%s, %s, 'user', %s, 'complete')",
            (self.user_message_id, self.chat_id, GRAPH_QUESTION))
        self.conn.execute("UPDATE bi.shops SET capabilities=%s WHERE shop_id=%s",
                          (["paid_amount", "paid_orders"], self.shop_id))
        self.conn.execute(
            """INSERT INTO bi.sync_state(source, entity, shop_id, watermark, covered,
                       data_as_of, quality_status, quality_rule)
               VALUES (%s, 'orders', %s, %s,
                       tstzmultirange(tstzrange(%s, %s, '[)')), %s, 'passed', %s)
               ON CONFLICT (source, entity, shop_id) DO UPDATE SET
                       covered = bi.sync_state.covered + EXCLUDED.covered,
                       quality_status = 'passed', quality_rule = EXCLUDED.quality_rule,
                       data_as_of = EXCLUDED.data_as_of""",
            (self.source, self.shop_id, self.end, self.start - timedelta(days=1),
             self.end + timedelta(days=1), self.end, self.quality_rule))

    def _new_turn(self):
        """再开一条用户消息：`query_runs` 的唯一键按 (消息, 领域, 尝试) 占位。"""
        self.chat_id, self.user_message_id = uuid4(), uuid4()
        self.conn.execute("INSERT INTO bi.app_chats(id, subject_id, title) "
                          "VALUES (%s, 'exploration-subject', '探索')", (self.chat_id,))
        self.conn.execute(
            "INSERT INTO bi.app_messages(id, chat_id, role, content, status) "
            "VALUES (%s, %s, 'user', %s, 'complete')",
            (self.user_message_id, self.chat_id, GRAPH_QUESTION))

    def _store(self):
        return PostgresQueryRunStore(self.conn, forbidden_values={self.shop_id})

    def _context(self, store):
        return graph_context(store, self.conn,
                             allowed_shop_ids=frozenset({self.shop_id}),
                             shop_refs={self.shop_id: self.shop_ref},
                             subject_id="exploration-subject", chat_id=self.chat_id,
                             user_message_id=self.user_message_id)

    def _run(self, store, metrics=None, groups=None, **request_overrides):
        from bi_agent.exploration.graph import run_exploration_graph

        values = {"start": self.start, "end": self.end}
        values.update(request_overrides)
        metrics = [self.PAID] if metrics is None else list(metrics)
        groups = ([DAY_SHOP_DAILY, SHOP_SHOP_DAILY] if groups is None
                  else list(groups))
        # 指标与分组同时送进"本轮选择"和"探索请求"：只改一处测的就不是 readiness，
        # 而是后面那格编译失败。
        values.setdefault("requested_metric_refs", list(metrics))
        values.setdefault("group_by_field_refs", list(groups))
        with mock.patch("bi_agent.exploration.graph.retrieve_schema_candidates",
                        return_value=selection_for(metrics, groups=groups)):
            return run_exploration_graph(question=GRAPH_QUESTION,
                                         request=ready_request(**values),
                                         context=self._context(store),
                                         versions=graph_versions())

    def _termination_reason(self, execution) -> str:
        return self.conn.execute(
            "SELECT termination_reason FROM bi.query_runs WHERE id=%s",
            (execution.domain_result.run_id,)).fetchone()[0]

    def _diagnostic_count(self, execution) -> int:
        return self.conn.execute(
            "SELECT count(*) FROM bi.query_diagnostics WHERE run_id=%s",
            (execution.domain_result.run_id,)).fetchone()[0]

    def test_ready_exploration_persists_one_artifact_and_one_diagnostic(self):
        execution = self._run(self._store())
        self.assertEqual(execution.domain_result.status.value, "success",
                         execution.domain_result.error)
        self.assertIsNotNone(execution.plan.estimated_rows)
        payload = execution.domain_result.artifacts[0].public_payload
        self.assertEqual(set(payload), EXPLORATION_PAYLOAD_KEYS)
        self.assertTrue(payload["rows"])
        self.assertEqual({row[SHOP_REF_COLUMN] for row in payload["rows"]},
                         {self.shop_ref})
        for row in payload["rows"]:
            self.assertRegex(row[self.PAID], r"^-?\d+(\.\d+)?$")
        # 覆盖/口径/截止都必须来自真证据：complete + 已解析的 basis + 共同截止。
        self.assertEqual(payload["coverage"]["status"], "complete")
        self.assertEqual({item["metric"] for item in payload["basis"]}, {self.PAID})
        self.assertEqual({item["basis"] for item in payload["basis"]},
                         {"platform_payment/v1"})
        self.assertEqual({item["time_basis"] for item in payload["basis"]}, {"pay_time"})
        for item in payload["basis"]:
            self.assertNotIn("semantic/", str(list(item.values())))
        self.assertEqual(execution.domain_result.data_as_of, self.conn.execute(
            "SELECT data_as_of FROM bi.sync_state WHERE shop_id=%s AND entity='orders'",
            (self.shop_id,)).fetchone()[0])
        for leak in ("SELECT", "ANY(", self.shop_id, "statement_timeout"):
            self.assertNotIn(leak, json.dumps(payload, ensure_ascii=False), leak)
        run_id = execution.domain_result.run_id
        self.assertEqual(self.conn.execute(
            "SELECT count(*) FROM bi.query_artifacts WHERE run_id=%s "
            "AND artifact_type='exploration_result'", (run_id,)).fetchone()[0], 1)
        sql_text = self.conn.execute(
            "SELECT sql_text FROM bi.query_diagnostics WHERE run_id=%s",
            (run_id,)).fetchone()[0]
        self.assertIn("SELECT", sql_text)
        self.assertEqual(sql_text, execution.plan.sql_text)
        self.assertEqual(self.conn.execute(
            "SELECT status, termination_reason FROM bi.query_runs WHERE id=%s",
            (run_id,)).fetchone(), ("succeeded", "succeeded"))
        self.assertEqual(self.conn.execute(
            "SELECT domain, current_node FROM bi.query_runs WHERE id=%s",
            (run_id,)).fetchone(), (EXPLORATION_DOMAIN, "finalize"))

    def test_wrong_capability_tag_refuses_before_any_statement(self):
        """P1-1 回归：标签集合里没这个指标的标签，旧实现只看"非空"就当已开通。"""
        self.conn.execute("UPDATE bi.shops SET capabilities=%s WHERE shop_id=%s",
                          (["paid_orders"], self.shop_id))
        execution = self._run(self._store())
        self.assertEqual(execution.domain_result.status.value, "missing_data")
        self.assertIsNone(execution.plan)
        self.assertEqual(self._diagnostic_count(execution), 0)
        self.assertEqual(self._termination_reason(execution), "capability_unavailable")

    def test_metric_without_registered_capability_refuses_without_reading_facts(self):
        """`cost_total` 没有已登记的能力标签：一句库都不该问。

        这条同时钉住"没有把订单脊柱当万能覆盖"：旧实现会拿 orders 覆盖替它宣布
        `complete`，所以只断"拒答"不够，还要断诊断/事实读取都没发生。
        """
        execution = self._run(self._store(), metrics=["metric-cost-total"],
                              groups=[DAY_COST, SHOP_COST])
        self.assertIsNone(execution.plan)
        self.assertEqual(execution.domain_result.status.value, "missing_data")
        self.assertEqual(self._diagnostic_count(execution), 0)
        self.assertEqual(self._termination_reason(execution), "capability_unavailable")

    def test_unverified_time_basis_refuses_although_coverage_is_complete(self):
        """覆盖与质量都对，但付款时间口径没逐店对照取证：不拿它出聚合数。"""
        self.conn.execute("UPDATE bi.shops SET platform='jd' WHERE shop_id=%s",
                          (self.shop_id,))
        execution = self._run(self._store())
        self.assertIsNone(execution.plan)
        self.assertEqual(self._diagnostic_count(execution), 0)
        self.assertEqual(self._termination_reason(execution),
                         "coverage_time_basis_unverified")

    def test_missing_common_cutoff_refuses_although_coverage_is_complete(self):
        """覆盖完整但 `data_as_of` 没推进：不能声称数据新到什么时候。"""
        self.conn.execute("UPDATE bi.sync_state SET data_as_of = NULL WHERE shop_id=%s "
                          "AND entity='orders'", (self.shop_id,))
        execution = self._run(self._store())
        self.assertIsNone(execution.plan)
        self.assertEqual(self._termination_reason(execution), "data_as_of_unknown")

    def test_stale_quality_rule_publishes_only_with_the_mandatory_disclosure(self):
        """P1-1b 回归（真库、真引擎）：对账口径版本一变，旧的 passed 降为 unknown。

        `data_quality` 的口径是三态：unknown **可出数但必须披露**。把中间那态当"干净"
        就是丢一句必要的披露；当"失败"拒答又是另一回事（那是固定路径也不做的加严）。
        """
        self.conn.execute("UPDATE bi.sync_state SET quality_rule='older-rule' "
                          "WHERE shop_id=%s AND entity='orders'", (self.shop_id,))
        execution = self._run(self._store())
        self.assertEqual(execution.domain_result.status.value, "success",
                         execution.domain_result.error)
        payload = execution.domain_result.artifacts[0].public_payload
        self.assertEqual(payload["limitations"], ["来源质量未核验（尚无对账记录）"])
        self.assertEqual(payload["coverage"]["status"], "complete")
        self.assertEqual(execution.domain_result.model_payload["limitations"],
                         payload["limitations"])
        state = self.conn.execute(
            "SELECT state, termination_reason FROM bi.query_runs WHERE id=%s",
            (execution.domain_result.run_id,)).fetchone()
        run_state = state[0] if isinstance(state[0], dict) else json.loads(state[0])
        self.assertEqual(run_state["limitations"], ["source_quality_unverified"])
        self.assertEqual(state[1], "succeeded")

    def test_verified_quality_keeps_the_payload_free_of_borrowed_disclosure(self):
        """反例（反恒真）：质控已取证时不得附那句披露。"""
        execution = self._run(self._store())
        payload = execution.domain_result.artifacts[0].public_payload
        self.assertEqual(payload["limitations"], [])
        self.assertEqual(self._run_state(execution).get("limitations", []), [])

    def test_metric_with_two_dependencies_publishes_both_over_the_real_engine(self):
        """P1-1a 回归（真库）：`cash_difference` 要两个来源，不是"口径不兼容"。

        这里同时证两件相反的事：两个来源都取证才能出数（只补订单行就拒），而且
        两家店都同意时不得拒（逐条签名比会把它永久拒掉）。
        """
        from bi_agent.sources import AFTERSALE_ENTITY, AFTERSALE_SOURCE

        cash = "metric-cash-difference"
        self.conn.execute("UPDATE bi.shops SET capabilities=%s WHERE shop_id=%s",
                          (["cash_difference"], self.shop_id))
        self.conn.execute(
            """INSERT INTO bi.sync_state(source, entity, shop_id, watermark, covered,
                       data_as_of, quality_status, quality_rule)
               VALUES (%s, %s, %s, %s, tstzmultirange(tstzrange(%s, %s, '[)')),
                       %s, 'passed', %s)
               ON CONFLICT (source, entity, shop_id) DO UPDATE SET
                       covered = bi.sync_state.covered + EXCLUDED.covered,
                       quality_status = 'passed', quality_rule = EXCLUDED.quality_rule,
                       data_as_of = EXCLUDED.data_as_of""",
            (AFTERSALE_SOURCE, AFTERSALE_ENTITY, self.shop_id, self.end,
             self.start - timedelta(days=1), self.end + timedelta(days=1), self.end,
             self.quality_rule))
        execution = self._run(self._store(), metrics=[cash],
                              groups=[DAY_SHOP_DAILY, SHOP_SHOP_DAILY])
        self.assertEqual(execution.domain_result.status.value, "success",
                         execution.domain_result.error)
        basis = execution.domain_result.artifacts[0].public_payload["basis"]
        self.assertEqual({(item["basis"], item["time_basis"]) for item in basis},
                         {("platform_payment/v1", "pay_time"),
                          ("platform_refund_occurrence/v1",
                           "aftersale_completion_time")})
        self.assertEqual(self._termination_reason(execution), "succeeded")

        # 只补一个来源就不得出数：另一个来源从没取过数，不是"零"。
        self.conn.execute("DELETE FROM bi.sync_state WHERE shop_id=%s AND entity=%s",
                          (self.shop_id, AFTERSALE_ENTITY))
        self._new_turn()      # 一条用户消息就是一个运行上下文：第二次问要换新的那一条
        second = self._run(self._store(), metrics=[cash],
                           groups=[DAY_SHOP_DAILY, SHOP_SHOP_DAILY])
        self.assertIsNone(second.plan)
        self.assertEqual(self._termination_reason(second), "coverage_incomplete")

    def _run_state(self, execution) -> dict:
        state = self.conn.execute(
            "SELECT state FROM bi.query_runs WHERE id=%s",
            (execution.domain_result.run_id,)).fetchone()[0]
        return state if isinstance(state, dict) else json.loads(state)

    def test_uncovered_window_refuses_and_records_the_gap(self):
        self.conn.execute(
            "UPDATE bi.sync_state SET covered = tstzmultirange() WHERE shop_id=%s "
            "AND entity='orders'", (self.shop_id,))
        execution = self._run(self._store())
        self.assertIsNone(execution.plan)
        self.assertEqual(self._termination_reason(execution), "coverage_incomplete")
        state = self.conn.execute(
            "SELECT state FROM bi.query_runs WHERE id=%s",
            (execution.domain_result.run_id,)).fetchone()[0]
        state = state if isinstance(state, dict) else json.loads(state)
        self.assertEqual(state["limitations"], ["coverage_incomplete"])
        self.assertTrue(state["coverage"]["gaps"])

    def test_agent_turn_end_to_end_offers_only_the_tool_and_leaks_no_sql(self):
        """主层→适配器→图→真库的一条完整路径：模型拿到的那一句里没有 SQL 也没有真店号。

        只贴检索那一句（让本轮选择确定）：能力、覆盖、编译、AST、EXPLAIN、只读执行、
        投影与两条写入都是真的。这才能证明"门禁在列 Tool 阶段不查库"与"回到模型的
        载荷形状合法"不是推论。
        """
        import json

        from bi_agent.agent import SessionState, answer
        from bi_agent.llm import Message, ModelReply, ToolCall
        from bi_agent.runtime import TurnContext
        from bi_agent.runtime.models import RunNotFound

        selection = selection_for([self.PAID], groups=[DAY_SHOP_DAILY, SHOP_SHOP_DAILY])
        call = ToolCall(id="call_e1", name="explore_business_data",
                        arguments={"requested_metric_refs": [self.PAID],
                                   "group_by_field_refs": [DAY_SHOP_DAILY,
                                                           SHOP_SHOP_DAILY],
                                   "start": self.start.isoformat(),
                                   "end": self.end.isoformat()})

        def scripted(text, calls):
            reply = ModelReply(text=text, tool_calls=calls)
            reply._message = Message(role="assistant", content=text, tool_calls=calls)
            return reply

        reply_calls = [scripted(None, [call]), scripted("按引用给出了结果", [])]
        sent: list = []

        class ScriptedModel:
            def complete(self, messages, tools, timeout_s=None):
                sent.append((list(messages), list(tools)))
                return reply_calls[len(sent) - 1]

        with mock.patch("bi_agent.exploration.tool.retrieve_schema_candidates",
                        return_value=selection), \
                mock.patch("bi_agent.exploration.graph.retrieve_schema_candidates",
                           return_value=selection):
            turn = answer("按店铺和日期看支付金额与订单数", SessionState(subject="u1"),
                          model=ScriptedModel(), conn=self.conn,
                          allowed_shop_ids=frozenset({self.shop_id}),
                          now=datetime(2026, 9, 8, 9, tzinfo=BEIJING),
                          run_store=self._store(),
                          turn_context=TurnContext(chat_id=self.chat_id,
                                                   user_message_id=self.user_message_id,
                                                   subject_id="exploration-subject"),
                          controlled_sql_enabled=True)
        self.assertEqual(sent[0][1][-1]["function"]["name"], "explore_business_data",
                         "本轮选择不可由固定 Tool 表达 → 必须公告探索入口")
        tool_messages = [message for message in sent[1][0]
                         if isinstance(message, Message) and message.role == "tool"]
        self.assertEqual(len(tool_messages), 1)
        payload = json.loads(tool_messages[0].content)
        self.assertEqual(set(payload), EXPLORATION_PAYLOAD_KEYS)
        self.assertTrue(payload["rows"])
        self.assertEqual({item["basis"] for item in payload["basis"]},
                         {"platform_payment/v1"})
        for leak in ("SELECT", "ANY(", "%(limit)s", "statement_timeout", self.shop_id):
            self.assertNotIn(leak, tool_messages[0].content, leak)
        self.assertEqual([item["artifact_type"] for item in turn.artifacts],
                         [EXPLORATION_ARTIFACT_TYPE])
        run_id = self.conn.execute(
            "SELECT id FROM bi.query_runs WHERE domain=%s",
            (EXPLORATION_DOMAIN,)).fetchone()[0]
        self.assertEqual(self.conn.execute(
            "SELECT status, termination_reason FROM bi.query_runs WHERE id=%s",
            (run_id,)).fetchone(), ("succeeded", "succeeded"))
        # 图与 Tool 只开了一个运行行、一条诊断：不存在逐店循环的额外写入。
        self.assertEqual(self.conn.execute(
            "SELECT count(*) FROM bi.query_runs WHERE domain=%s",
            (EXPLORATION_DOMAIN,)).fetchone()[0], 1)
        self.assertEqual(self.conn.execute(
            "SELECT count(*) FROM bi.query_diagnostics WHERE run_id=%s",
            (run_id,)).fetchone()[0], 1)
        with self.assertRaises(RunNotFound):
            self._store().record_diagnostic(uuid4(), template_id="exploration_sql",
                                           sql_text="SELECT 1", parameters={})


# ---------------------------------------------------------------------------
# approved 查询学习记忆的生命周期（计划 Task 2 Step 5）：真库上的草稿来源边界、
# 人工状态机、CAS 与「恰好一条不可变事件」。全部写入发生在管理员外层事务里，
# 结束按 Rollback 协议丢弃，共享 *_test 库不留任何草稿或事件。
# ---------------------------------------------------------------------------

MEMORY_REQUEST = {"shop_refs": [S1_REF], "metrics": ["paid_amount"]}
MEMORY_PROVENANCE = ("fixed_metric_query", "1", "metrics/2026-09-12.1", "008", 7,
                     "identity/2026-09-11.1", "multi-source-policy/2026-09-12.1",
                     "business_query-graph/2026-09-11.1", "sources/2026-09-12.1")
MEMORY_TEMPLATE = "比较 {shop_scope} 在 {date_window} 的成本"


@unittest.skipUnless(os.getenv("BI_TEST_ADMIN_DSN"), "未配置独立测试数据库")
class QueryMemoryLifecycleDatabaseTests(unittest.TestCase):
    """计划 Task 2 Step 5：非成功/跨 owner 运行为 0 草稿；状态变化恰好一条事件。"""

    def setUp(self):
        self.conn = connect_test_db(self)

    def _command(self, action: str, reason: str, replacement: str | None = None):
        from bi_agent.query_memory.models import ApprovalCommand

        return ApprovalCommand(action=action, reason=reason,
                               replacement_ref=replacement)

    def _seed_run(self, *, subject: str = "subject-a", status: str = "succeeded",
                  domain: str = "business_query", with_provenance: bool = True,
                  request: dict | None = None):
        from psycopg.types.json import Jsonb

        chat_id, message_id, run_id = uuid4(), uuid4(), uuid4()
        self.conn.execute(
            "INSERT INTO bi.app_chats(id, subject_id, title) VALUES (%s, %s, '查询')",
            (chat_id, subject))
        self.conn.execute(
            "INSERT INTO bi.app_messages(id, chat_id, role, content, status) "
            "VALUES (%s, %s, 'user', '查询销售额', 'complete')",
            (message_id, chat_id))
        self.conn.execute(
            "INSERT INTO bi.query_runs(id, chat_id, user_message_id, subject_id, "
            "tool_call_id, domain, attempt_no, status, normalized_request, state) "
            "VALUES (%s, %s, %s, %s, 'call_1', %s, 1, %s, %s, '{}')",
            (run_id, chat_id, message_id, subject, domain, status,
             Jsonb(request if request is not None else MEMORY_REQUEST)))
        if with_provenance:
            self.conn.execute(
                """INSERT INTO bi.query_provenance (
                       run_id, template_id, template_version, metric_version,
                       schema_version, catalog_version, mapping_version,
                       policy_version, graph_version, source_registry_version)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                (run_id, *MEMORY_PROVENANCE))
        return run_id

    def _slots(self):
        from bi_agent.query_memory.models import QuerySlot

        return (QuerySlot(name="shop_scope", kind="entity_scope"),
                QuerySlot(name="date_window", kind="date_window"))

    def _build(self, run_id, *, owner: str = "subject-a"):
        from bi_agent.query_memory.repository import build_draft_from_run

        return build_draft_from_run(
            self.conn, run_id=run_id, owner_subject_id=owner,
            question_template=MEMORY_TEMPLATE, slots=self._slots(),
            created_by="reviewer-a")

    def _repository(self):
        from bi_agent.query_memory.repository import QueryMemoryRepository

        return QueryMemoryRepository(self.conn)

    def _event_count(self, example_ref: str) -> int:
        return self.conn.execute(
            "SELECT count(*) FROM bi.approved_query_events WHERE example_ref=%s",
            (example_ref,)).fetchone()[0]

    def _history(self, example_ref: str) -> list[tuple]:
        """按 revision 排好的 (revision, event_kind) 历史：不可变、只增。"""
        return self.conn.execute(
            "SELECT revision, event_kind FROM bi.approved_query_events "
            "WHERE example_ref=%s ORDER BY revision",
            (example_ref,)).fetchall()

    def test_succeeded_owned_run_builds_a_sanitized_draft_with_one_event(self):
        from bi_agent.semantic_catalog.registry import CATALOG

        run_id = self._seed_run()
        record = self._build(run_id)
        self.assertEqual(record.example_ref, "mem-" + run_id.hex)
        self.assertEqual((record.status, record.approval_revision), ("draft", 0))
        row = self.conn.execute(
            """SELECT status, approval_revision, expected_tool, normalized_request,
                      authorization_refs, slots
               FROM bi.approved_query_examples WHERE example_ref=%s""",
            (record.example_ref,)).fetchone()
        status, revision, tool, request, auth_refs, slots = row
        self.assertEqual((status, revision, tool), ("draft", 0, "query_business"))
        # 一次性业务值已剥离；授权域来自运行自身的 shop_refs；版本集合已冻结。
        self.assertEqual(request, {"metrics": ["paid_amount"]})
        self.assertEqual(list(auth_refs), [S1_REF])
        self.assertEqual([item["name"] for item in slots],
                         ["shop_scope", "date_window"])
        versions = self.conn.execute(
            "SELECT version_requirements FROM bi.approved_query_examples "
            "WHERE example_ref=%s", (record.example_ref,)).fetchone()[0]
        self.assertEqual(versions["semantic_catalog_version"], CATALOG.version)
        self.assertEqual(versions["data_catalog_version"], 7)
        # 恰好一条 drafted 事件：revision 0、操作者与固定理由留痕。
        self.assertEqual(self._event_count(record.example_ref), 1)
        event = self.conn.execute(
            """SELECT revision, actor_subject_id, event_kind, reason, replacement_ref
               FROM bi.approved_query_events WHERE example_ref=%s""",
            (record.example_ref,)).fetchone()
        self.assertEqual(event, (0, "reviewer-a", "drafted", "drafted", None))

    def test_ineligible_runs_leave_zero_drafts_and_zero_events(self):
        run_id = self._seed_run(status="failed")
        with self.assertRaisesRegex(ValueError, "memory_source_run_not_eligible"):
            self._build(run_id, owner="subject-a")
        cross = self._seed_run(subject="subject-b")
        with self.assertRaisesRegex(ValueError, "memory_source_run_not_eligible"):
            self._build(cross, owner="subject-a")
        orphan = self._seed_run(with_provenance=False)
        with self.assertRaisesRegex(ValueError, "memory_source_run_not_eligible"):
            self._build(orphan)
        scoped = self._seed_run(request={"shop_refs": ["invalid_shop"],
                                         "metrics": ["paid_amount"]})
        with self.assertRaisesRegex(ValueError, "memory_source_run_not_eligible"):
            self._build(scoped)
        self.assertEqual(self.conn.execute(
            "SELECT count(*) FROM bi.approved_query_examples").fetchone()[0], 0)
        self.assertEqual(self.conn.execute(
            "SELECT count(*) FROM bi.approved_query_events").fetchone()[0], 0)

    def test_double_draft_of_same_run_conflicts_on_the_primary_key(self):
        run_id = self._seed_run()
        first = self._build(run_id)
        with self.assertRaisesRegex(ValueError, "memory_draft_exists"):
            self._build(run_id)
        self.assertEqual(self._event_count(first.example_ref), 1)

    def test_lifecycle_transitions_append_exactly_one_event_each(self):
        run_id = self._seed_run()
        draft = self._build(run_id)
        repository = self._repository()
        approved = repository.transition(
            draft.example_ref, command=self._command("approve", "人工复核通过"),
            actor_subject_id="reviewer-a")
        self.assertEqual((approved.status, approved.approval_revision),
                         ("approved", 1))
        self.assertEqual(self._history(draft.example_ref),
                         [(0, "drafted"), (1, "approved")])
        # 已批准的样例不能再批准：非法迁移失败收场，事件不再增加。
        with self.assertRaisesRegex(ValueError, "memory_transition_invalid"):
            repository.transition(
                draft.example_ref,
                command=self._command("approve", "cannot approve twice"),
                actor_subject_id="reviewer-a")
        self.assertEqual(self._history(draft.example_ref),
                         [(0, "drafted"), (1, "approved")])
        # 撤销：approved → revoked，第二次状态变化恰一条新事件，状态进入终态。
        revoked = repository.transition(
            draft.example_ref, command=self._command("revoke", "证据失效，立即撤销"),
            actor_subject_id="reviewer-a")
        self.assertEqual((revoked.status, revoked.approval_revision),
                         ("revoked", 2))
        self.assertEqual(self._history(draft.example_ref),
                         [(0, "drafted"), (1, "approved"), (2, "revoked")])

    def test_supersede_needs_an_approved_same_domain_replacement(self):
        old_run = self._seed_run()
        old = self._build(old_run)
        new_run = self._seed_run()
        new = self._build(new_run)
        repository = self._repository()
        repository.transition(
            old.example_ref, command=self._command("approve", "人工复核通过"),
            actor_subject_id="reviewer-a")
        repository.transition(
            new.example_ref, command=self._command("approve", "人工复核通过"),
            actor_subject_id="reviewer-a")
        # 替换目标未批准（还是 draft）→ 无效，且不追加事件。
        draft_two = self._build(self._seed_run())
        with self.assertRaisesRegex(ValueError, "memory_replacement_invalid"):
            repository.transition(
                old.example_ref,
                command=self._command("supersede", "版本升级，换用新样例",
                                      draft_two.example_ref),
                actor_subject_id="reviewer-a")
        self.assertEqual(self._history(old.example_ref),
                         [(0, "drafted"), (1, "approved")])
        # 自替换同样无效。
        with self.assertRaisesRegex(ValueError, "memory_replacement_invalid"):
            repository.transition(
                old.example_ref,
                command=self._command("supersede", "版本升级，换用新样例",
                                      old.example_ref),
                actor_subject_id="reviewer-a")
        superseded = repository.transition(
            old.example_ref,
            command=self._command("supersede", "版本升级，换用新样例",
                                  new.example_ref),
            actor_subject_id="reviewer-a")
        self.assertEqual((superseded.status, superseded.approval_revision),
                         ("superseded", 2))
        event = self.conn.execute(
            """SELECT revision, event_kind, replacement_ref
               FROM bi.approved_query_events
               WHERE example_ref=%s ORDER BY revision DESC LIMIT 1""",
            (old.example_ref,)).fetchone()
        self.assertEqual(event, (2, "superseded", new.example_ref))

    def test_draft_row_satisfies_the_database_bound_value_check(self):
        """净化器剥掉 start/shop_refs 后，底表 CHECK 也不该再见到任何绑定值键。"""
        from bi_agent.query_memory.models import FORBIDDEN_VALUE_KEYS

        run_id = self._seed_run()
        record = self._build(run_id)
        stored = self.conn.execute(
            "SELECT normalized_request FROM bi.approved_query_examples "
            "WHERE example_ref=%s", (record.example_ref,)).fetchone()[0]
        self.assertFalse(set(stored) & FORBIDDEN_VALUE_KEYS)


@unittest.skipUnless(os.getenv("BI_TEST_ADMIN_DSN"), "未配置独立测试数据库")
class AnalysisArtifactDatabaseTests(unittest.TestCase):
    """计划 Task 2 Step 5：Postgres 侧的分析读取与内存 Store 语义逐字一致。"""

    def setUp(self):
        self.conn = connect_test_db(self)

    def _seed(self, *, subject: str = "u1", status: str = "succeeded",
              with_provenance: bool = True):
        from psycopg.types.json import Jsonb

        from bi_agent.runtime.artifacts import QueryProvenance

        provenance = QueryProvenance()
        chat_id, message_id, run_id = uuid4(), uuid4(), uuid4()
        self.conn.execute(
            "INSERT INTO bi.app_chats(id, subject_id, title) VALUES (%s, %s, '查询')",
            (chat_id, subject))
        self.conn.execute(
            "INSERT INTO bi.app_messages(id, chat_id, role, content, status) "
            "VALUES (%s, %s, 'user', '查询销售额', 'complete')",
            (message_id, chat_id))
        self.conn.execute(
            "INSERT INTO bi.query_runs(id, chat_id, user_message_id, subject_id, "
            "tool_call_id, domain, attempt_no, status, normalized_request, state) "
            "VALUES (%s, %s, %s, %s, 'call_1', 'business_query', 1, %s, '{}', '{}')",
            (run_id, chat_id, message_id, subject, status))
        if with_provenance:
            self.conn.execute(
                """INSERT INTO bi.query_provenance (
                       run_id, template_id, template_version, metric_version,
                       schema_version, catalog_version, mapping_version,
                       policy_version, graph_version, source_registry_version)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                (run_id, provenance.template_id, provenance.template_version,
                 provenance.metric_version, provenance.schema_version,
                 provenance.catalog_version, provenance.mapping_version,
                 provenance.policy_version, provenance.graph_version,
                 provenance.source_registry_version))
        payload = {"status": "ok", "metric_definition": {},
                   "coverage": {"status": "complete", "start": "2026-09-01",
                                "end": "2026-09-08", "gaps": []},
                   "limitations": [], "data_as_of": None, "filters": {},
                   "data": [{"shop_ref": S1_REF, "day": "2026-09-01",
                             "paid_amount": "12.30", "paid_orders": 3}]}
        store = PostgresQueryRunStore(self.conn, forbidden_values={"S1"})
        artifact_id = store.save_artifact(run_id, NewArtifact(
            artifact_type="metric_result", payload=payload)).id
        return run_id, artifact_id, payload

    def _store(self):
        return PostgresQueryRunStore(self.conn, forbidden_values={"S1"})

    def test_succeeded_owned_artifact_loads_and_projects_exactly(self):
        from datetime import datetime, timezone

        from bi_agent.analysis.loader import load_analysis_dataset
        from bi_agent.commerce.models import DomainContext

        run_id, artifact_id, payload = self._seed()
        stored = self._store().load_artifact_for_analysis(artifact_id, subject_id="u1")
        self.assertEqual((stored.run_id, stored.subject_id,
                          stored.artifact_type), (run_id, "u1", "metric_result"))
        self.assertEqual(stored.payload, payload)

        context = DomainContext(
            subject_id="u1", allowed_shop_ids=frozenset({S1_REF, S2_REF}),
            shop_refs={"S1": S1_REF, "S2": S2_REF},
            conn=self.conn, store=self._store(),
            chat_id=uuid4(), user_message_id=uuid4(), root_request_id=uuid4(),
            now=datetime(2026, 9, 15, tzinfo=timezone.utc), deadline=1e12)
        dataset = load_analysis_dataset(str(artifact_id), context=context)
        self.assertEqual(dataset.observations[0].metrics["paid_amount"],
                         Decimal("12.30"))
        self.assertEqual(dataset.observations[0].dimensions["shop_ref"], S1_REF)

    def test_ineligible_sources_are_indistinguishably_rejected(self):
        _, artifact_id, _ = self._seed()
        _, failed_artifact, _ = self._seed(status="failed")
        _, cross_artifact, _ = self._seed(subject="subject-b")
        _, orphan_artifact, _ = self._seed(with_provenance=False)
        store = self._store()
        for bad, subject in ((uuid4(), "u1"), (failed_artifact, "u1"),
                             (cross_artifact, "u1"), (orphan_artifact, "u1"),
                             (artifact_id, "subject-b")):
            with self.subTest(subject=subject), \
                    self.assertRaisesRegex(ValueError,
                                           "^analysis_source_not_found$"):
                store.load_artifact_for_analysis(bad, subject_id=subject)

    def test_memory_and_postgres_readers_agree_on_the_same_artifact(self):
        from bi_agent.runtime.memory import MemoryQueryRunStore

        _, artifact_id, payload = self._seed()
        pg_stored = self._store().load_artifact_for_analysis(
            artifact_id, subject_id="u1")

        memory = MemoryQueryRunStore(forbidden_values={"S1"})
        memory.runs[pg_stored.run_id] = {
            "id": pg_stored.run_id, "chat_id": uuid4(), "user_message_id": uuid4(),
            "subject_id": "u1", "tool_call_id": "call_1", "domain": "business_query",
            "attempt_no": 1, "status": "succeeded", "current_node": "finalize",
            "revision": 1, "normalized_request": {}, "state": {"node": "finalize"},
            "error_code": None, "root_request_id": pg_stored.run_id,
            "request_fingerprint": None, "recovery_count": 0,
            "provenance": pg_stored.provenance, "started_at": None,
            "updated_at": None, "completed_at": None,
        }
        memory.artifacts[artifact_id] = {
            "id": artifact_id, "run_id": pg_stored.run_id,
            "artifact_type": "metric_result", "payload": payload,
            "data_as_of": None, "coverage": None,
            "dataset_ref": None, "chart_version": None, "created_at": None,
        }
        memory_stored = memory.load_artifact_for_analysis(artifact_id, subject_id="u1")
        self.assertEqual(memory_stored.payload, pg_stored.payload)
        self.assertEqual(memory_stored.provenance, pg_stored.provenance)
