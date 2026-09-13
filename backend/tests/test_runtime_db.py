"""Database checks for the query-run persistence migration.

Each test owns an administrator transaction and rolls it back so the
independent local ``*_test`` database remains unchanged.  The same guardrails
as ``test_db.py`` prevent this module from connecting to a production host.
"""

import json
import os
import traceback
import unittest
from contextlib import nullcontext
from datetime import datetime
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

BEIJING = ZoneInfo("Asia/Shanghai")

import psycopg

from .dbfixtures import connect_test_db
from .fakeconn import P1_REF, S1_REF, S2_REF
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
            store.save_artifact(uuid4(), NewArtifact(
                artifact_type="price_audit", payload={"status": "ok"}))
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
