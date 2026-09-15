"""Chat API contract checks that do not require an ERP connection."""

import json
import unittest
import warnings
import os
from contextlib import contextmanager
from datetime import date, datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock, patch, sentinel
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

import psycopg

from starlette.exceptions import StarletteDeprecationWarning

warnings.filterwarnings("ignore", category=StarletteDeprecationWarning, module="fastapi.testclient")
from fastapi.testclient import TestClient
from pydantic import SecretStr

from tests.fakeconn import S1_REF, Rows, QueryMemoryConn, ShopCatalogConn, example_row, memory_run_row


class ApiTests(unittest.TestCase):
    def test_runtime_factory_loads_app_and_selected_model(self):
        from bi_agent.api import create_runtime_app

        env = {
            "APP_ENV": "development",
            "APP_PUBLIC_ORIGIN": "http://localhost:5173",
            "BI_APP_DSN": "postgresql://bi_app:password@localhost/bi_agent_test",
            "BI_SHOP_IDS": "S1",
            "LLM_PROVIDER": "qwen",
            "LLM_MODEL": "qwen-test",
            "QWEN_API_KEY": "test-key",
        }
        with patch.dict(os.environ, env, clear=True), patch(
            "bi_agent.llm.create_model", return_value=sentinel.model
        ) as create_model:
            app = create_runtime_app()

        self.assertEqual(app.state.settings.environment, "development")
        create_model.assert_called_once()

    def _runtime_env(self, **overrides):
        env = {
            "APP_ENV": "development",
            "APP_PUBLIC_ORIGIN": "http://localhost:5173",
            "BI_APP_DSN": "postgresql://bi_app:password@localhost/bi_agent_test",
            "BI_SHOP_IDS": "S1",
            "LLM_PROVIDER": "qwen",
            "LLM_MODEL": "qwen-test",
            "QWEN_API_KEY": "test-key",
        }
        env.update(overrides)
        return env

    def _run_runtime_factory(self, env):
        """跑一次 `create_runtime_app()`，把预检与模型创建换成可计数的替身。

        返回 `(app, calls, connect, validate)`；`calls` 是**有序**事件流，所以“只建
        一次连接”与“预检在模型与 FastAPI 之前”这两件事都能直接读出来。
        """
        from bi_agent.api import create_runtime_app

        calls = []

        class PreflightConn:
            def __enter__(self):
                calls.append("preflight-connection")
                return self

            def __exit__(self, *_details):
                calls.append("preflight-closed")
                return False

        def connect(dsn, **kwargs):
            calls.append("connect")
            self.assertEqual(dsn, env["BI_APP_DSN"])
            self.assertEqual(kwargs, {"autocommit": True})
            return PreflightConn()

        def validate(conn, catalog):
            calls.append("validate")
            self.assertIsInstance(conn, PreflightConn)
            return catalog

        with patch.dict(os.environ, env, clear=True), \
                patch("bi_agent.api.psycopg.connect", side_effect=connect) as connects, \
                patch("bi_agent.api.validate_catalog_schema",
                      side_effect=validate) as validates, \
                patch("bi_agent.llm.create_model",
                      side_effect=lambda _settings: calls.append("model")):
            app = create_runtime_app()
        return app, calls, connects, validates

    def test_runtime_factory_preflights_the_catalog_once_when_enabled(self):
        """计划 Task 4 Step 1：flag=true 时建立**一条** autocommit 连接并校验 CATALOG。"""
        from bi_agent.semantic_catalog import CATALOG

        app, calls, connect, validate = self._run_runtime_factory(
            self._runtime_env(SEMANTIC_CATALOG_ENABLED="true"))
        self.assertEqual(calls, ["connect", "preflight-connection", "validate",
                                 "preflight-closed", "model"])
        connect.assert_called_once()
        validate.assert_called_once()
        self.assertIs(validate.call_args.args[1], CATALOG)
        self.assertTrue(app.state.settings.semantic_catalog_enabled)

    def test_runtime_factory_makes_no_preflight_connection_when_disabled(self):
        """默认关闭时：不建连接、不校验、路由面不变——现有 Tool 与用户行为必须照旧。"""
        for value in (None, "false"):
            with self.subTest(env_value=value):
                env = (self._runtime_env() if value is None
                       else self._runtime_env(SEMANTIC_CATALOG_ENABLED=value))
                app, calls, connect, validate = self._run_runtime_factory(env)
                self.assertEqual(calls, ["model"])
                connect.assert_not_called()
                validate.assert_not_called()
                self.assertFalse(app.state.settings.semantic_catalog_enabled)

    def test_the_feature_gate_changes_no_route_or_user_visible_surface(self):
        """开关只改“要不要预检”，不改 HTTP 面：语义目录本计划不新增任何工具或路由。"""
        from bi_agent.semantic_catalog import CATALOG

        enabled_app, _, _, enabled_validate = self._run_runtime_factory(
            self._runtime_env(SEMANTIC_CATALOG_ENABLED="true"))
        disabled_app, _, _, _ = self._run_runtime_factory(self._runtime_env())
        self.assertEqual(set(enabled_app.openapi()["paths"]),
                         set(disabled_app.openapi()["paths"]))
        self.assertIn("/api/chats", set(disabled_app.openapi()["paths"]))
        self.assertIs(enabled_validate.call_args.args[1], CATALOG)

    def test_runtime_factory_fails_startup_when_the_preflight_mismatches(self):
        """目录与真实 schema 不一致 ⇒ 进程起不来，不降级为“目录为空”。"""
        from bi_agent.api import create_runtime_app
        from bi_agent.semantic_catalog.schema_check import SchemaMismatch

        events = []

        class PreflightConn:
            def __enter__(self):
                events.append("preflight-connection")
                return self

            def __exit__(self, *_details):
                events.append("preflight-closed")
                return False

        env = self._runtime_env(SEMANTIC_CATALOG_ENABLED="true")
        with patch.dict(os.environ, env, clear=True), \
                patch("bi_agent.api.psycopg.connect",
                      side_effect=lambda *_args, **_kwargs: PreflightConn()) as connect, \
                patch("bi_agent.api.validate_catalog_schema",
                      side_effect=SchemaMismatch(("field-shop-daily-paid-amount",))), \
                patch("bi_agent.llm.create_model",
                      side_effect=lambda _settings: events.append("model")) as create_model:
            with self.assertRaises(SchemaMismatch) as caught:
                create_runtime_app()

        self.assertEqual(str(caught.exception),
                         "semantic_schema_mismatch:field-shop-daily-paid-amount")
        self.assertNotIn("password", str(caught.exception))
        connect.assert_called_once()
        create_model.assert_not_called()
        # 失败路径也得把连接还回去：预检不在启动里留下挂着的连接。
        self.assertEqual(events, ["preflight-connection", "preflight-closed"])

    def _app(self):
        from bi_agent.api import create_app
        from bi_agent.config import AppSettings

        return create_app(AppSettings(
            app_dsn=SecretStr(os.environ["BI_TEST_ADMIN_DSN"]),
            shop_ids=frozenset({"S1"}),
            environment="production",
            allowed_subjects=frozenset({"user-a", "user-b"}),
            public_origin="https://bi.test",
            auth_subject_header="X-Auth-Request-Sub",
        ))

    @unittest.skipUnless(os.getenv("BI_TEST_ADMIN_DSN"), "未配置独立测试数据库")
    def test_chat_crud_is_owned_by_the_authenticated_subject(self):
        client = TestClient(self._app())
        write_headers = {
            "Content-Type": "application/json",
            "X-BI-Agent": "web",
            "Origin": "https://bi.test",
            "X-Auth-Request-Sub": "user-a",
        }
        created = client.post("/api/chats", headers=write_headers, json={})
        self.assertEqual(created.status_code, 201, created.text)
        chat_id = created.json()["id"]
        try:
            other = client.get(
                f"/api/chats/{chat_id}/messages",
                headers={"X-Auth-Request-Sub": "user-b"},
            )
            self.assertEqual(other.status_code, 404)
            renamed = client.patch(
                f"/api/chats/{chat_id}", headers=write_headers,
                json={"title": "九月复盘"},
            )
            self.assertEqual(renamed.status_code, 200, renamed.text)
            self.assertEqual(renamed.json()["title"], "九月复盘")
            denied = client.post(
                "/api/chats",
                headers={"X-Auth-Request-Sub": "user-a"}, json={},
            )
            self.assertEqual(denied.status_code, 403)
            self.assertEqual(denied.json()["code"], "forbidden")
        finally:
            client.delete(f"/api/chats/{chat_id}", headers=write_headers)

    @unittest.skipUnless(os.getenv("BI_TEST_ADMIN_DSN"), "未配置独立测试数据库")
    def test_message_stream_persists_a_completed_turn(self):
        from bi_agent.llm import Message, ModelReply

        class PlainModel:
            def complete(self, messages, tools, *, timeout_s):
                reply = ModelReply(text="已记录你的问题")
                reply._message = Message(role="assistant", content=reply.text)
                return reply

        client = TestClient(create_app := self._app_with_model(PlainModel()))
        headers = {
            "Content-Type": "application/json",
            "X-BI-Agent": "web",
            "Origin": "https://bi.test",
            "X-Auth-Request-Sub": "user-a",
        }
        created = client.post("/api/chats", headers=headers, json={})
        self.assertEqual(created.status_code, 201, created.text)
        chat_id = created.json()["id"]
        try:
            response = client.post(
                f"/api/chats/{chat_id}/messages", headers=headers,
                json={"content": "帮我看最近7天"},
            )
            self.assertEqual(response.status_code, 200, response.text)
            self.assertIn("event: status", response.text)
            self.assertIn("event: message", response.text)
            self.assertIn("event: done", response.text)
            messages = client.get(
                f"/api/chats/{chat_id}/messages",
                headers={"X-Auth-Request-Sub": "user-a"},
            ).json()
            self.assertEqual([message["role"] for message in messages], ["user", "assistant"])
            self.assertEqual(messages[-1]["content"], "已记录你的问题")
        finally:
            client.delete(f"/api/chats/{chat_id}", headers=headers)

    @unittest.skipUnless(os.getenv("BI_TEST_ADMIN_DSN"), "未配置独立测试数据库")
    def test_message_stream_reports_a_model_failure(self):
        from bi_agent.llm import ModelError

        class FailingModel:
            def complete(self, messages, tools, *, timeout_s):
                raise ModelError("timeout")

        client = TestClient(self._app_with_model(FailingModel()))
        headers = {
            "Content-Type": "application/json",
            "X-BI-Agent": "web",
            "Origin": "https://bi.test",
            "X-Auth-Request-Sub": "user-a",
        }
        created = client.post("/api/chats", headers=headers, json={})
        self.assertEqual(created.status_code, 201, created.text)
        chat_id = created.json()["id"]
        try:
            response = client.post(
                f"/api/chats/{chat_id}/messages", headers=headers,
                json={"content": "帮我看最近7天"},
            )
            self.assertEqual(response.status_code, 200, response.text)
            self.assertIn("event: error", response.text)
            self.assertIn('"status":"error"', response.text)
            messages = client.get(
                f"/api/chats/{chat_id}/messages",
                headers={"X-Auth-Request-Sub": "user-a"},
            ).json()
            self.assertEqual(messages[-1]["status"], "error")
        finally:
            client.delete(f"/api/chats/{chat_id}", headers=headers)

    def test_promotion_turn_streams_a_public_artifact_and_completes(self):
        """C-1 全链路：推广回合必须真发出 artifact 与 done，而不是被吞成笼统失败。

        推广结果列与持久化白名单漂移时，to_public_artifact() 抛 ValueError 会被
        run_chat_turn 的兜底 except 吞掉，前端只看到“本轮回答未完成”。
        """
        from bi_agent.agent import run_chat_turn
        from bi_agent.chats import ChatMessage
        from bi_agent.llm import Message, ModelReply, ToolCall
        from bi_agent.runtime import MemoryQueryRunStore

        class PromotionModel:
            def __init__(self):
                self.calls = 0

            def complete(self, messages, tools, *, timeout_s):
                self.calls += 1
                if self.calls == 1:
                    call = ToolCall(
                        id="call_1", name="evaluate_promotion", arguments={
                            "mode": "sales_cap", "start": "2026-10-01",
                            "end": "2026-11-01", "sales_estimate": "100000",
                            "target_ratio": "0.12",
                        },
                    )
                    reply = ModelReply(tool_calls=[call])
                    reply._message = Message(role="assistant", content=None,
                                             tool_calls=[call])
                    return reply
                reply = ModelReply(text="按假设最多可花 1.2 万元")
                reply._message = Message(role="assistant", content=reply.text)
                return reply

        conn = ShopCatalogConn()   # 同时服务 _fetch_shops 与目录投影两处读取
        model = PromotionModel()
        chat_id = uuid4()
        now = datetime(2026, 9, 8, 9, tzinfo=ZoneInfo("Asia/Shanghai"))
        saved_message = ChatMessage(id=uuid4(), role="assistant",
                                   content="按假设最多可花 1.2 万元", artifacts=[],
                                   status="complete", created_at=now)
        with patch(
            "bi_agent.agent.PostgresQueryRunStore",
            lambda _conn, *, forbidden_values: MemoryQueryRunStore(
                forbidden_values=frozenset({"S1", "ERP-P-9"})),
        ), patch("bi_agent.chats.load_chat_context", return_value=({}, [])), patch(
            "bi_agent.chats.save_user_message",
            return_value=SimpleNamespace(id=uuid4()),
        ), patch("bi_agent.chats.save_assistant_message",
                 return_value=saved_message) as save_assistant, patch(
            "bi_agent.chats.update_chat_filters",
        ):
            events = list(run_chat_turn(
                conn, chat_id, "user-a", "假设10月销售额10万元、推广费用率12%，最多花多少？",
                model=model, allowed_shop_ids=frozenset({"S1"}), now=now,
            ))

        self.assertEqual([event.event for event in events],
                         ["status", "status", "artifact", "status", "message", "done"])
        artifact = events[2].data
        self.assertEqual(artifact["status"], "ok")
        self.assertEqual(artifact["data"][0]["spend_cap"], "12000.00")
        self.assertEqual(artifact["filters"]["mode"], "sales_cap")
        self.assertEqual(events[-1].data, {"status": "complete"})
        stream = "\n".join(f"{event.event}:{event.data}" for event in events)
        self.assertNotIn("本轮回答未完成", stream)
        self.assertNotIn("unsafe_persistence_payload", stream)
        self.assertNotIn("S1", stream)
        saved_artifacts = save_assistant.call_args.args[4]
        self.assertEqual(len(saved_artifacts), 1)
        self.assertEqual(saved_artifacts[0]["data"][0]["sales_estimate"], "100000")

    def test_runtime_create_run_failure_ends_with_a_sanitized_sse_error(self):
        """A run-store failure must not leak database diagnostics to the browser."""
        from bi_agent.agent import run_chat_turn
        from bi_agent.llm import Message, ModelReply, ToolCall

        class QueryingModel:
            def __init__(self):
                self.calls = 0

            def complete(self, messages, tools, *, timeout_s):
                self.calls += 1
                call = ToolCall(
                    id="call_1", name="query_business", arguments={
                        "start": "2026-09-01", "end": "2026-09-08",
                        "shop_ids": [S1_REF], "metrics": ["paid_amount"],
                    },
                )
                reply = ModelReply(tool_calls=[call])
                reply._message = Message(role="assistant", content=None, tool_calls=[call])
                return reply

        class FailingRunStore:
            def __init__(self, _conn, *, forbidden_values):
                self.forbidden_values = forbidden_values

            def create_run(self, _record):
                raise RuntimeError(
                    "psycopg.OperationalError dsn=postgresql://secret "
                    "SELECT * FROM bi.query_runs\ntraceback\n"
                    "STACK_MARKER_RUNTIME_FAILURE\n"
                    'File "/srv/bi_agent/runtime/repository.py", line 42'
                )

        conn = ShopCatalogConn()   # 同时服务 _fetch_shops 与目录投影两处读取
        model = QueryingModel()
        with patch("bi_agent.agent.PostgresQueryRunStore", FailingRunStore), patch(
            "bi_agent.chats.load_chat_context", return_value=({}, [])
        ), patch(
            "bi_agent.chats.save_user_message",
            return_value=SimpleNamespace(id=uuid4()),
        ), patch("bi_agent.chats.save_assistant_message"), patch(
            "bi_agent.chats.update_chat_filters"
        ):
            events = list(run_chat_turn(
                conn, uuid4(), "user-a", "最近7天店铺A支付金额", model=model,
                allowed_shop_ids=frozenset({"S1"}),
                now=datetime(2026, 9, 8, 9, tzinfo=ZoneInfo("Asia/Shanghai")),
            ))

        self.assertEqual(model.calls, 1)
        self.assertEqual([event.event for event in events], ["status", "error", "done"])
        self.assertEqual(events[-2].data["code"], "unavailable")
        self.assertEqual(events[-1].data, {"status": "error"})
        browser_text = "\n".join(
            f"{event.event}:{event.data}" for event in events
        )
        self.assertNotIn("psycopg", browser_text)
        self.assertNotIn("SELECT", browser_text)
        self.assertNotIn("dsn=", browser_text)
        self.assertNotIn("traceback", browser_text)
        self.assertNotIn("STACK_MARKER_RUNTIME_FAILURE", browser_text)
        self.assertNotIn("/srv/bi_agent/runtime/repository.py", browser_text)

    def test_artifact_persistence_failure_has_no_artifact_sse_or_message_payload(self):
        """A failed audit write cannot produce a user-visible query result."""
        from bi_agent.agent import run_chat_turn
        from bi_agent.llm import Message, ModelReply, ToolCall
        from bi_agent.metrics import Coverage, ToolResult
        from bi_agent.runtime import ArtifactPersistenceError, MemoryQueryRunStore

        class QueryingModel:
            def __init__(self):
                self.calls = 0

            def complete(self, messages, tools, *, timeout_s):
                self.calls += 1
                call = ToolCall(
                    id="call_1", name="query_business", arguments={
                        "start": "2026-09-01", "end": "2026-09-08",
                        "shop_ids": [S1_REF], "metrics": ["paid_amount"],
                    },
                )
                reply = ModelReply(tool_calls=[call])
                reply._message = Message(role="assistant", content=None, tool_calls=[call])
                return reply

        class FailingArtifactStore(MemoryQueryRunStore):
            def __init__(self, _conn, *, forbidden_values):
                super().__init__(forbidden_values=forbidden_values)

            def save_artifact(self, run_id, artifact):  # type: ignore[no-untyped-def]
                raise ArtifactPersistenceError("database password=not-for-public-output")

        conn = ShopCatalogConn()   # 同时服务 _fetch_shops 与目录投影两处读取
        saved_user = SimpleNamespace(id=uuid4())
        chat_id = uuid4()
        model = QueryingModel()
        with patch("bi_agent.agent.PostgresQueryRunStore", FailingArtifactStore), patch(
            "bi_agent.business_query.nodes.metrics.query_business",
            return_value=ToolResult(
                status="ok", data=[{"paid_amount": "1000"}],
                coverage=Coverage(
                    status="complete", start=date(2026, 9, 1), end=date(2026, 9, 8),
                ),
            ),
        ), patch(
            "bi_agent.chats.load_chat_context", return_value=({}, [])
        ), patch(
            "bi_agent.chats.save_user_message", return_value=saved_user
        ), patch("bi_agent.chats.save_assistant_message") as save_assistant, patch(
            "bi_agent.chats.update_chat_filters"
        ) as update_filters:
            events = list(run_chat_turn(
                conn, chat_id, "user-a", "最近7天店铺A支付金额", model=model,
                allowed_shop_ids=frozenset({"S1"}),
                now=datetime(2026, 9, 8, 9, tzinfo=ZoneInfo("Asia/Shanghai")),
            ))

        self.assertEqual(model.calls, 1)
        self.assertEqual([event.event for event in events], ["status", "error", "done"])
        self.assertEqual(events[-2].data, {
            "code": "artifact_persistence_failed", "message": "结果保存失败，请稍后重试。",
        })
        self.assertEqual(events[-1].data, {"status": "error"})
        update_filters.assert_not_called()
        save_assistant.assert_called_once_with(
            conn, chat_id, "user-a", "结果保存失败，请稍后重试。", [], status="error",
        )
        browser_text = "\n".join(f"{event.event}:{event.data}" for event in events)
        self.assertNotIn("artifact:", browser_text)
        self.assertNotIn("1000", browser_text)
        self.assertNotIn("S1", browser_text)
        self.assertNotIn("database password=not-for-public-output", browser_text)

    @unittest.skipUnless(os.getenv("BI_TEST_ADMIN_DSN"), "未配置独立测试数据库")
    def test_message_stream_audits_a_completed_business_query_without_sensitive_json(self):
        """A completed business query links its run, events, and public artifact."""
        from bi_agent.llm import Message, ModelReply, ToolCall
        from tests.test_db import seed_business_case

        class BusinessQueryModel:
            def __init__(self):
                self.calls = 0

            def complete(self, messages, tools, *, timeout_s):
                self.calls += 1
                if self.calls == 1:
                    call = ToolCall(
                        id="call_1", name="query_business", arguments={
                            "start": "2026-09-01", "end": "2026-09-08",
                            "shop_ids": [S1_REF], "metrics": ["paid_amount"],
                        },
                    )
                    reply = ModelReply(tool_calls=[call])
                    reply._message = Message(
                        role="assistant", content=None, tool_calls=[call],
                        provider_context={"reasoning_content": "private reasoning"},
                    )
                    return reply
                reply = ModelReply(text="最近7天店铺A支付金额为1000元")
                reply._message = Message(role="assistant", content=reply.text)
                return reply

        class TestTransactionConnection:
            """Keep API requests inside this test's rollback-only transaction."""

            def __init__(self, connection):
                self._connection = connection

            def __enter__(self):
                return self

            def __exit__(self, *_details):
                return False

            def close(self):
                pass

            def __getattr__(self, name):
                return getattr(self._connection, name)

        admin_conn = psycopg.connect(os.environ["BI_TEST_ADMIN_DSN"])
        try:
            if not admin_conn.info.dbname.endswith("_test"):
                self.fail(f"测试必须连接 *_test 数据库，实际 {admin_conn.info.dbname}")
            if (admin_conn.info.host or "") not in {"localhost", "127.0.0.1", "::1"}:
                self.fail(f"测试必须连接本地测试实例，实际 {admin_conn.info.host}")
            seed_business_case(admin_conn)
            connection = TestTransactionConnection(admin_conn)
            client = TestClient(self._app_with_model(BusinessQueryModel()))
            headers = {
                "Content-Type": "application/json",
                "X-BI-Agent": "web",
                "Origin": "https://bi.test",
                "X-Auth-Request-Sub": "user-a",
            }
            with patch("bi_agent.api.psycopg.connect", return_value=connection):
                created = client.post("/api/chats", headers=headers, json={})
                self.assertEqual(created.status_code, 201, created.text)
                chat_id = created.json()["id"]
                try:
                    response = client.post(
                        f"/api/chats/{chat_id}/messages", headers=headers,
                        json={"content": "最近7天店铺A支付金额"},
                    )
                    self.assertEqual(response.status_code, 200, response.text)
                    self.assertIn("event: artifact", response.text)
                    self.assertIn("event: message", response.text)
                    self.assertLess(
                        response.text.index("event: artifact"),
                        response.text.index("event: message"),
                    )
                    self.assertLess(
                        response.text.index("event: message"),
                        response.text.rindex("event: done"),
                    )
                    self.assertTrue(response.text.rstrip().endswith(
                        'event: done\ndata: {"status":"complete"}'
                    ))
                    run = admin_conn.execute(
                        "SELECT id, status, current_node, revision FROM bi.query_runs "
                        "WHERE chat_id=%s ORDER BY started_at DESC LIMIT 1", (chat_id,),
                    ).fetchone()
                    self.assertIsNotNone(run)
                    self.assertEqual(run[1:3], ("succeeded", "finalize"))
                    self.assertGreater(run[3], 0)
                    events = admin_conn.execute(
                        "SELECT revision, node, event_type, status "
                        "FROM bi.query_run_events WHERE run_id=%s ORDER BY revision",
                        (run[0],),
                    ).fetchall()
                    self.assertEqual([event[0] for event in events], list(range(1, 8)))
                    self.assertEqual(events, [
                        (1, "resolve_parameters", "transitioned", "running"),
                        (2, "validate_parameters", "transitioned", "running"),
                        (3, "authorize_scope", "transitioned", "running"),
                        (4, "execute_fixed_query", "transitioned", "running"),
                        (5, "classify_result", "transitioned", "running"),
                        (6, "persist_artifact", "transitioned", "running"),
                        (7, "finalize", "completed", "succeeded"),
                    ])
                    self.assertEqual(admin_conn.execute(
                        "SELECT count(*) FROM bi.query_artifacts WHERE run_id=%s", (run[0],)
                    ).fetchone()[0], 1)
                    persisted_json = admin_conn.execute(
                        "SELECT concat(r.normalized_request::text, r.state::text, "
                        "coalesce((SELECT string_agg(e.payload::text, '') "
                        "FROM bi.query_run_events e WHERE e.run_id=r.id), ''), "
                        "coalesce((SELECT string_agg(a.payload::text, '') "
                        "FROM bi.query_artifacts a WHERE a.run_id=r.id), '')) "
                        "FROM bi.query_runs r WHERE r.id=%s", (run[0],),
                    ).fetchone()[0]
                    for forbidden in ("S1", "ERP-P-9", "reasoning_content"):
                        self.assertNotIn(forbidden, persisted_json)
                finally:
                    client.delete(f"/api/chats/{chat_id}", headers=headers)
        finally:
            admin_conn.rollback()
            admin_conn.close()

    def _app_with_model(self, model):
        from bi_agent.api import create_app
        from bi_agent.config import AppSettings

        return create_app(AppSettings(
            app_dsn=SecretStr(os.environ["BI_TEST_ADMIN_DSN"]),
            shop_ids=frozenset({"S1"}),
            environment="production",
            allowed_subjects=frozenset({"user-a", "user-b"}),
            public_origin="https://bi.test",
            auth_subject_header="X-Auth-Request-Sub",
        ), model=model)

    def test_health_and_chat_only_routes(self):
        from bi_agent.api import create_app
        from bi_agent.config import AppSettings

        app = create_app(AppSettings(
            app_dsn=SecretStr("postgresql://bi_app:password@localhost/bi_agent_test"),
            shop_ids=frozenset({"S1"}),
            environment="development",
            allowed_subjects=frozenset(),
            public_origin="http://localhost:5173",
            auth_subject_header="X-Auth-Request-Sub",
        ))
        client = TestClient(app)

        self.assertEqual(client.get("/api/health").json(), {"status": "ok"})
        paths = set(app.openapi()["paths"])
        self.assertIn("/api/chats", paths)
        self.assertNotIn("/api/query", paths)
        self.assertNotIn("/api/metrics", paths)
        self.assertNotIn("/api/sql", paths)


# ======================================================================
# approved 查询记忆审核 API（计划 2026-09-14-approved-query-memory.md Task 4）
#
# 全部跑在替身上：bi_agent.query_memory.api 的 psycopg.connect 被换成假的
# approver 连接，于是「审核读写只建独立短连接、绝不碰 bi_app DSN」「被拒的
# 请求不建任何连接」都能直接断言。响应断言落在稳定 envelope（code/message）
# 与稳定 memory_* 错误码上；owner、授权域、created_by、事件理由、SQL、聊天、
# 结果、DSN、数据库异常文本一律不得出现在任何响应里。

_RUN_ID = UUID("550e8400-e29b-41d4-a716-446655440000")
_RUN_REF = f"run-{_RUN_ID}"
_TEMPLATE = "比较 {shop_scope} 在 {date_window} 的成本"
_SLOTS = [{"name": "shop_scope", "kind": "entity_scope"},
          {"name": "date_window", "kind": "date_window"}]
_APP_DSN = "postgresql://bi_app:app-secret@localhost/bi_agent_test"
_APPROVER_DSN = "postgresql://bi_approver:approver-secret@localhost/bi_agent_test"

_DRAFT_PROJECTION_FIELDS = {
    "example_ref", "domain", "intent_signature", "question_template", "slots",
    "normalized_request", "expected_tool", "version_requirements", "status",
    "approval_revision", "source_run_ref"}
_CANDIDATE_PROJECTION_FIELDS = {
    "source_run_ref", "domain", "normalized_request", "expected_tool",
    "version_requirements", "created_at"}

_DRAFT_LIST_COLUMNS = (
    "example_ref", "source_run_id", "domain", "intent_signature",
    "question_template", "slots", "normalized_request", "expected_tool",
    "version_requirements", "status", "approval_revision")


class ReviewerApiConn(QueryMemoryConn):
    """生命周期替身 + 审核 API 的三条读取：候选列表、草稿列表、来源 owner 解析。

    白名单之外的 SQL（聊天、Artifact、诊断……）由 QueryMemoryConn 显式报错，
    所以「审核 API 不读聊天与结果」同样是替身保证的性质。
    """

    def __init__(self, *, candidate_rows=None, runs=None, **overrides):
        super().__init__(**overrides)
        self.candidate_rows = list(candidate_rows or [])
        self.runs = dict(runs or {})
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *_details):
        self.closed = True
        return False

    def close(self):
        self.closed = True

    def execute(self, sql, params=None):
        text = " ".join(sql.split())
        values = list(params or [])
        self.sql_log.append(text)
        if text.startswith("SELECT r.id, r.domain, r.normalized_request"):
            return Rows(self.candidate_rows)
        if text.startswith("SELECT example_ref, source_run_id, domain"):
            ordered = sorted(self.examples.values(), key=lambda row: row["example_ref"])
            return Rows([[row[column] for column in _DRAFT_LIST_COLUMNS] for row in ordered])
        if text.startswith("SELECT subject_id FROM bi.query_runs WHERE id"):
            return Rows([(self.runs[values[0]],)]) if values[0] in self.runs else Rows([])
        return super().execute(sql, params)


def _candidate_row(*, run_id=_RUN_ID, domain="controlled_sql_exploration",
                   request_extra=None, provenance=True):
    """候选列表 SELECT 的一行：(id, domain, request, started_at, 血缘九列)。"""
    row = memory_run_row(domain=domain, request_extra=request_extra, provenance=provenance)
    started_at = datetime(2026, 9, 14, 12, tzinfo=timezone.utc)
    return (run_id, row[2], row[3], started_at, *row[4:])


class QueryMemoryApiTests(unittest.TestCase):
    """计划 Task 4 Step 1：非审核者拒绝、载荷脱敏与来源保护的 API 契约。"""

    maxDiff = None

    @contextmanager
    def reviewer_client(self, conn, *, enabled=True, loud=True):
        from bi_agent.api import create_app
        from bi_agent.config import AppSettings

        app = create_app(AppSettings(
            app_dsn=SecretStr(_APP_DSN),
            shop_ids=frozenset({"S1"}),
            environment="production",
            allowed_subjects=frozenset({"subject-a", "reviewer-a"}),
            public_origin="https://bi.test",
            auth_subject_header="X-Auth-Request-Sub",
            approved_query_memory_enabled=enabled,
            approver_subjects=frozenset({"reviewer-a"}) if enabled else frozenset(),
            approver_dsn=SecretStr(_APPROVER_DSN) if enabled else None,
        ))
        calls = []

        def connect(dsn, **kwargs):
            calls.append((dsn, kwargs))
            return conn

        with patch("bi_agent.query_memory.api.psycopg.connect", side_effect=connect):
            yield TestClient(app, raise_server_exceptions=loud), calls

    @staticmethod
    def _read(sub="reviewer-a"):
        return {"X-Auth-Request-Sub": sub}

    @staticmethod
    def _write(sub="reviewer-a"):
        return {"Content-Type": "application/json", "X-BI-Agent": "web",
                "Origin": "https://bi.test", "X-Auth-Request-Sub": sub}

    @staticmethod
    def _draft_payload():
        return {"source_run_ref": _RUN_REF, "question_template": _TEMPLATE,
                "slots": _SLOTS}

    def _forbidden_words_absent(self, rendered, extra=()):
        for word in ("sql_text", "rows", "subject_id", "shop_id", "raw_prompt",
                     "owner_subject_id", "authorization_refs", "created_by",
                     "actor", "error_code", "chat_messages", "query_artifacts",
                     "dsn=", "app-secret", "approver-secret", *extra):
            self.assertNotIn(word, rendered, word)

    # ---- feature gate：关闭时整条审核面都是 404，且不建任何连接 ------------

    def test_feature_gate_off_returns_stable_404_on_every_memory_route(self):
        conn = ReviewerApiConn(runs={_RUN_ID: "subject-a"})
        with self.reviewer_client(conn, enabled=False) as (client, calls):
            requests = (
                lambda: client.get("/api/query-memory/candidates", headers=self._read()),
                lambda: client.get("/api/query-memory/drafts", headers=self._read()),
                lambda: client.post("/api/query-memory/drafts",
                                    headers=self._write(), json=self._draft_payload()),
                lambda: client.post("/api/query-memory/drafts/mem-target/approve",
                                    headers=self._write(), json={"reason": "血缘与模板复核通过"}),
                lambda: client.post("/api/query-memory/drafts/mem-target/revoke",
                                    headers=self._write(), json={"reason": "血缘与模板复核通过"}),
                lambda: client.post("/api/query-memory/drafts/mem-target/supersede",
                                    headers=self._write(),
                                    json={"reason": "口径已升级", "replacement_ref": "mem-other"}),
            )
            for request in requests:
                with self.subTest(route=request):
                    response = request()
                    self.assertEqual(response.status_code, 404, response.text)
                    body = response.json()
                    self.assertEqual(body["code"], "not_found")
                    self.assertEqual(body["message"], "功能未启用")
        # 关闭时连审核 DSN 都不能碰：一条连接都不建。
        self.assertEqual(calls, [])
        self.assertEqual(conn.writes, [])

    def test_missing_subject_is_unauthenticated_before_anything_else(self):
        conn = ReviewerApiConn()
        with self.reviewer_client(conn) as (client, calls):
            response = client.get("/api/query-memory/drafts")
            self.assertEqual(response.status_code, 401)
            self.assertEqual(response.json()["code"], "unauthenticated")
        self.assertEqual(calls, [])

    # ---- 非审核者 403；被拒请求不建审核连接 --------------------------------

    def test_non_reviewer_cannot_list_create_or_decide(self):
        conn = ReviewerApiConn(runs={_RUN_ID: "subject-a"})
        with self.reviewer_client(conn) as (client, calls):
            requests = (
                lambda: client.get("/api/query-memory/candidates", headers=self._read("subject-a")),
                lambda: client.get("/api/query-memory/drafts", headers=self._read("subject-a")),
                lambda: client.post("/api/query-memory/drafts",
                                    headers=self._write("subject-a"), json=self._draft_payload()),
                lambda: client.post("/api/query-memory/drafts/mem-target/approve",
                                    headers=self._write("subject-a"), json={"reason": "血缘与模板复核通过"}),
            )
            for request in requests:
                with self.subTest(route=request):
                    response = request()
                    self.assertEqual(response.status_code, 403, response.text)
                    body = response.json()
                    self.assertEqual(body["code"], "forbidden")
                    self.assertEqual(body["message"], "无审核权限")
        self.assertEqual(calls, [])
        self.assertEqual(conn.writes, [])

    # ---- 写请求必须过既有 WebWrite 边界 ------------------------------------

    def test_writes_require_the_existing_web_write_boundary(self):
        conn = ReviewerApiConn(runs={_RUN_ID: "subject-a"})
        bad = {"Content-Type": "application/json", "X-Auth-Request-Sub": "reviewer-a"}
        with self.reviewer_client(conn) as (client, calls):
            for path, payload in (
                ("/api/query-memory/drafts", self._draft_payload()),
                ("/api/query-memory/drafts/mem-target/approve", {"reason": "血缘与模板复核通过"}),
                ("/api/query-memory/drafts/mem-target/revoke", {"reason": "血缘与模板复核通过"}),
                ("/api/query-memory/drafts/mem-target/supersede",
                 {"reason": "口径已升级", "replacement_ref": "mem-other"}),
            ):
                with self.subTest(path=path):
                    response = client.post(path, headers=bad, json=payload)
                    self.assertEqual(response.status_code, 403, response.text)
                    self.assertEqual(response.json()["code"], "forbidden")
        # 来源不可信的写请求在建立审核连接之前就被拒。
        self.assertEqual(calls, [])
        self.assertEqual(conn.writes, [])

    # ---- 审核读写只走独立 approver DSN 的短连接 ----------------------------

    def test_reads_use_one_short_lived_separate_approver_connection(self):
        conn = ReviewerApiConn(examples={
            "mem-a": example_row("mem-a", run_id=_RUN_ID)})
        with self.reviewer_client(conn) as (client, calls):
            response = client.get("/api/query-memory/drafts", headers=self._read())
            self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(calls, [(_APPROVER_DSN, {"autocommit": True})])
        self.assertNotEqual(calls[0][0], _APP_DSN)
        # 请求结束连接即归还：审核路径不留挂着的连接。
        self.assertTrue(conn.closed)

    # ---- 投影脱敏：候选与草稿只暴露计划字段，绝无来源/授权/聊天/SQL --------

    def test_draft_listing_exposes_only_the_safe_projection(self):
        conn = ReviewerApiConn(examples={
            "mem-b": example_row("mem-b", run_id=UUID(int=2),
                                 status="approved", revision=1),
            "mem-a": example_row("mem-a", run_id=_RUN_ID),
        })
        with self.reviewer_client(conn) as (client, calls):
            response = client.get("/api/query-memory/drafts", headers=self._read())
            self.assertEqual(response.status_code, 200, response.text)
            body = response.json()
        self.assertEqual([item["example_ref"] for item in body], ["mem-a", "mem-b"])
        for item in body:
            self.assertEqual(set(item), _DRAFT_PROJECTION_FIELDS)
        self.assertEqual(body[0]["source_run_ref"], _RUN_REF)
        self.assertEqual(body[1]["source_run_ref"],
                         "run-00000000-0000-0000-0000-000000000002")
        rendered = json.dumps(body, ensure_ascii=False)
        self._forbidden_words_absent(rendered, extra=("reason", "state"))

    def test_candidates_expose_the_value_free_projection_only(self):
        good = _candidate_row()
        poisoned = _candidate_row(run_id=UUID(int=2), request_extra={"top_n": 5})
        unknown_domain = _candidate_row(run_id=UUID(int=3), domain="not_registered")
        no_provenance = _candidate_row(run_id=UUID(int=4), provenance=False)
        conn = ReviewerApiConn(
            candidate_rows=[poisoned, good, unknown_domain, no_provenance])
        with self.reviewer_client(conn) as (client, calls):
            response = client.get("/api/query-memory/candidates", headers=self._read())
            self.assertEqual(response.status_code, 200, response.text)
            body = response.json()
        # 不合格的候选整条 fail-closed：不出现在列表里，也不带原因文本。
        self.assertEqual(len(body), 1)
        candidate = body[0]
        self.assertEqual(set(candidate), _CANDIDATE_PROJECTION_FIELDS)
        self.assertEqual(candidate["source_run_ref"], _RUN_REF)
        self.assertEqual(candidate["domain"], "controlled_sql_exploration")
        # 净化投影：日期区间与店铺选择被剥掉，只剩稳定引用与业务码——绝不是
        # 原始存储请求。
        self.assertEqual(candidate["normalized_request"], {"metrics": ["paid_amount"]})
        self.assertNotIn("start", candidate["normalized_request"])
        self.assertNotIn("shop_refs", candidate["normalized_request"])
        self.assertEqual(candidate["expected_tool"], "explore_business_data")
        rendered = json.dumps(body, ensure_ascii=False)
        self._forbidden_words_absent(rendered)
        # 列表 SQL 只读安全列：成功状态 + 完整血缘 + 排除已有草稿的运行，
        # 硬上限 100；聊天/Artifact/诊断/state/error_code 一个都不碰。
        self.assertEqual(len(conn.sql_log), 1)
        sql = " ".join(conn.sql_log[0].split())
        self.assertIn("bi.query_runs", sql)
        self.assertIn("bi.query_provenance", sql)
        self.assertIn("NOT EXISTS", sql)
        self.assertIn("bi.approved_query_examples", sql)
        self.assertIn("'succeeded'", sql)
        self.assertIn("LIMIT 100", sql)
        for forbidden in ("chat_messages", "query_artifacts", "query_diagnostics",
                          "r.state", "error_code", "payload"):
            self.assertNotIn(forbidden, sql)

    # ---- 草稿创建：opaque ref 服务端解析，owner 与审核者分离 ---------------

    def test_draft_creation_resolves_the_opaque_ref_and_keeps_owner_distinct(self):
        conn = ReviewerApiConn(runs={_RUN_ID: "subject-a"},
                               run_row=memory_run_row(subject="subject-a"))
        with self.reviewer_client(conn) as (client, calls):
            response = client.post("/api/query-memory/drafts", headers=self._write(),
                                   json=self._draft_payload())
            self.assertEqual(response.status_code, 201, response.text)
            body = response.json()
        self.assertEqual(body["example_ref"], "mem-" + _RUN_ID.hex)
        self.assertEqual(body["source_run_ref"], _RUN_REF)
        self.assertEqual(body["status"], "draft")
        self.assertEqual(body["approval_revision"], 0)
        self.assertEqual(set(body), _DRAFT_PROJECTION_FIELDS)
        rendered = json.dumps(body, ensure_ascii=False)
        self._forbidden_words_absent(rendered, extra=("reason", "state"))
        # owner 是来源运行本人，created_by/actor 是审核者：两者必须不同。
        stored = conn.examples["mem-" + _RUN_ID.hex]
        self.assertEqual(stored["owner_subject_id"], "subject-a")
        event = conn.events[0]
        self.assertEqual(event[3], "reviewer-a")
        self.assertEqual(event[4], "drafted")
        # 草稿请求是净化投影：一次性业务值不进记忆。
        self.assertNotIn("start", stored["normalized_request"])
        for sql in conn.sql_log:
            self.assertNotIn("chat_messages", sql)
            self.assertNotIn("query_artifacts", sql)
        self.assertTrue(conn.closed)

    def test_draft_owner_comes_from_the_run_not_from_the_reviewer(self):
        conn = ReviewerApiConn(runs={_RUN_ID: "subject-b"},
                               run_row=memory_run_row(subject="subject-b"))
        with self.reviewer_client(conn) as (client, calls):
            response = client.post("/api/query-memory/drafts", headers=self._write(),
                                   json=self._draft_payload())
            self.assertEqual(response.status_code, 201, response.text)
        stored = conn.examples["mem-" + _RUN_ID.hex]
        self.assertEqual(stored["owner_subject_id"], "subject-b")
        self.assertEqual(conn.events[0][3], "reviewer-a")

    def test_draft_creation_rejects_fields_beyond_the_three_allowed(self):
        for smuggled in (
            {"subject": "someone-else"},
            {"owner_subject_id": "someone-else"},
            {"status": "approved"},
            {"domain": "business_query"},
            {"normalized_request": {"metrics": ["paid_amount"]}},
            {"version_requirements": {}},
            {"authorization_refs": [S1_REF]},
            {"example_ref": "mem-forged"},
        ):
            with self.subTest(field=sorted(smuggled)):
                conn = ReviewerApiConn(runs={_RUN_ID: "subject-a"},
                                       run_row=memory_run_row(subject="subject-a"))
                with self.reviewer_client(conn) as (client, calls):
                    response = client.post("/api/query-memory/drafts",
                                           headers=self._write(),
                                           json=self._draft_payload() | smuggled)
                    self.assertEqual(response.status_code, 422, response.text)
                self.assertEqual(calls[0][0], _APPROVER_DSN)

    def test_draft_creation_revalidates_the_source_run(self):
        cases = (
            ("absent", ReviewerApiConn()),
            ("failed", ReviewerApiConn(runs={_RUN_ID: "subject-a"},
                                       run_row=memory_run_row(status="failed"))),
            ("no_provenance", ReviewerApiConn(
                runs={_RUN_ID: "subject-a"},
                run_row=memory_run_row(provenance=False))),
        )
        for name, conn in cases:
            with self.subTest(source=name):
                with self.reviewer_client(conn) as (client, calls):
                    response = client.post("/api/query-memory/drafts",
                                           headers=self._write(),
                                           json=self._draft_payload())
                    self.assertEqual(response.status_code, 422, response.text)
                    body = response.json()
                    self.assertEqual(body["code"], "memory_source_run_not_eligible")
                self.assertEqual(conn.writes, [])

    def test_draft_creation_rejects_malformed_source_refs(self):
        conn = ReviewerApiConn()
        for bad_ref in ("run-not-a-uuid", _RUN_ID.hex,
                        "mem-" + _RUN_ID.hex, "run-"):
            with self.subTest(ref=bad_ref):
                with self.reviewer_client(conn) as (client, calls):
                    response = client.post("/api/query-memory/drafts",
                                           headers=self._write(),
                                           json=self._draft_payload()
                                           | {"source_run_ref": bad_ref})
                    self.assertEqual(response.status_code, 422, response.text)
        # 形状不合格的 ref 从未到达解析 SQL：bi.query_runs 一次都没被读。
        for sql in conn.sql_log:
            self.assertNotIn("bi.query_runs", sql)
        self.assertEqual(conn.writes, [])

    # ---- 状态迁移：稳定错误码 → 固定 404/409/422 ---------------------------

    def _examples_with(self, *, status="draft", revision=0, extra=None):
        examples = {"mem-target": example_row("mem-target", run_id=_RUN_ID,
                                              status=status, revision=revision)}
        examples.update(extra or {})
        return examples

    def test_transition_errors_map_to_fixed_statuses(self):
        with self.subTest(case="unknown-ref"):
            conn = ReviewerApiConn()
            with self.reviewer_client(conn) as (client, calls):
                response = client.post("/api/query-memory/drafts/mem-target/approve",
                                       headers=self._write(),
                                       json={"reason": "血缘与模板复核通过"})
                self.assertEqual(response.status_code, 404, response.text)
                self.assertEqual(response.json()["code"], "memory_example_not_found")
            self.assertEqual(conn.events, [])
        with self.subTest(case="approve-twice"):
            conn = ReviewerApiConn(examples=self._examples_with(
                status="approved", revision=1))
            with self.reviewer_client(conn) as (client, calls):
                response = client.post("/api/query-memory/drafts/mem-target/approve",
                                       headers=self._write(),
                                       json={"reason": "血缘与模板复核通过"})
                self.assertEqual(response.status_code, 409, response.text)
                self.assertEqual(response.json()["code"], "memory_transition_invalid")
            self.assertEqual(conn.events, [])
        with self.subTest(case="revision-conflict"):
            conn = ReviewerApiConn(examples=self._examples_with())
            conn.cas_fail = True
            with self.reviewer_client(conn) as (client, calls):
                response = client.post("/api/query-memory/drafts/mem-target/approve",
                                       headers=self._write(),
                                       json={"reason": "血缘与模板复核通过"})
                self.assertEqual(response.status_code, 409, response.text)
                self.assertEqual(response.json()["code"], "memory_revision_conflict")
            # CAS 冲突绝不追加事件，也不重放审批。
            self.assertEqual(conn.events, [])
        with self.subTest(case="replacement-not-approved"):
            # supersede 只能从 approved 发起：目标本身必须是已批准记录。
            conn = ReviewerApiConn(examples=self._examples_with(
                status="approved", revision=1,
                extra={"mem-other": example_row("mem-other", run_id=UUID(int=9))}))
            with self.reviewer_client(conn) as (client, calls):
                response = client.post("/api/query-memory/drafts/mem-target/supersede",
                                       headers=self._write(),
                                       json={"reason": "口径已升级",
                                             "replacement_ref": "mem-other"})
                self.assertEqual(response.status_code, 422, response.text)
                self.assertEqual(response.json()["code"], "memory_replacement_invalid")
            self.assertEqual(conn.events, [])
        with self.subTest(case="supersede-without-replacement-field"):
            conn = ReviewerApiConn(examples=self._examples_with())
            with self.reviewer_client(conn) as (client, calls):
                response = client.post("/api/query-memory/drafts/mem-target/supersede",
                                       headers=self._write(), json={"reason": "口径已升级"})
                self.assertEqual(response.status_code, 422, response.text)
            self.assertEqual(conn.events, [])
        with self.subTest(case="blank-reason"):
            conn = ReviewerApiConn(examples=self._examples_with())
            with self.reviewer_client(conn) as (client, calls):
                response = client.post("/api/query-memory/drafts/mem-target/approve",
                                       headers=self._write(), json={"reason": "   "})
                self.assertEqual(response.status_code, 422, response.text)
            self.assertEqual(conn.events, [])
        with self.subTest(case="approve-must-not-carry-replacement"):
            conn = ReviewerApiConn(examples=self._examples_with())
            with self.reviewer_client(conn) as (client, calls):
                response = client.post("/api/query-memory/drafts/mem-target/approve",
                                       headers=self._write(),
                                       json={"reason": "血缘与模板复核通过",
                                             "replacement_ref": "mem-other"})
                self.assertEqual(response.status_code, 422, response.text)
            self.assertEqual(conn.events, [])

    def test_approval_updates_the_projection_and_appends_one_event(self):
        conn = ReviewerApiConn(examples=self._examples_with())
        with self.reviewer_client(conn) as (client, calls):
            response = client.post("/api/query-memory/drafts/mem-target/approve",
                                   headers=self._write(),
                                   json={"reason": "血缘与模板复核通过"})
            self.assertEqual(response.status_code, 200, response.text)
            body = response.json()
        self.assertEqual(body["status"], "approved")
        self.assertEqual(body["approval_revision"], 1)
        self.assertEqual(set(body), _DRAFT_PROJECTION_FIELDS)
        self.assertEqual(len(conn.events), 1)
        _depth, ref, revision, actor, kind, reason, replacement = conn.events[0]
        self.assertEqual((ref, revision, actor, kind, reason, replacement),
                         ("mem-target", 1, "reviewer-a", "approved",
                          "血缘与模板复核通过", None))

    # ---- 数据库失败：500 泛化响应，不外泄 DSN 或异常文本 -------------------

    def test_database_failure_leaks_no_dsn_or_exception_text(self):
        class BrokenConn(ReviewerApiConn):
            def execute(self, sql, params=None):
                raise psycopg.OperationalError(
                    "connection to server failed dsn="
                    f"\"{_APPROVER_DSN}\" SELECT * FROM bi.approved_query_examples")

        with self.reviewer_client(BrokenConn(), loud=False) as (client, calls):
            response = client.get("/api/query-memory/drafts", headers=self._read())
        self.assertEqual(response.status_code, 500)
        self._forbidden_words_absent(response.text, extra=("OperationalError", "SELECT"))
