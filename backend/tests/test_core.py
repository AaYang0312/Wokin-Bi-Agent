"""核心离线检查：配置、快麦接口边界、指标输入、模型回合、预算假设与对话。"""

import hashlib
import hmac
import json
import logging
import unittest
from datetime import date, datetime, timezone, timedelta
from decimal import Decimal
from typing import Sequence
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

import httpx

from bi_agent.llm import ToolCall
from bi_agent.metrics import Coverage, ToolResult
from bi_agent.catalog import ref_for_key
from bi_agent.data_quality import QUALITY_RULE
from tests.fakeconn import S1_REF, ShopCatalogConn, catalog_rows


def valid_app_env() -> dict[str, str]:
    """一份最小合法的 API 环境：feature gate 用例只在它上面改一两个开关。

    计划 Task 1 的探索契约用例也复用这一份（`tests.test_exploration`），免得同一个
    “合法环境”在一堆文件里有五六种写法。
    """
    return {
        "APP_ENV": "development",
        "APP_PUBLIC_ORIGIN": "http://localhost:5173",
        "BI_SHOP_IDS": "S1",
        "BI_APP_DSN": "postgresql://bi_app:password@localhost/bi_agent",
    }


class ConfigTests(unittest.TestCase):
    def test_app_settings_use_the_chat_role_and_trusted_origin(self):
        from bi_agent.config import load_app_settings

        env = {
            "APP_ENV": "production",
            "APP_ALLOWED_SUBJECTS": "subject-a",
            "APP_PUBLIC_ORIGIN": "https://bi.example.com",
            "AUTH_SUBJECT_HEADER": "X-Auth-Request-Sub",
            "BI_SHOP_IDS": "S1",
            "BI_APP_DSN": "postgresql://bi_app:password@localhost/bi_agent",
        }
        settings = load_app_settings(env)
        self.assertEqual(settings.app_dsn.get_secret_value(), env["BI_APP_DSN"])
        self.assertEqual(settings.public_origin, "https://bi.example.com")
        self.assertEqual(settings.auth_subject_header, "X-Auth-Request-Sub")
        with self.assertRaises(ValueError):
            load_app_settings({**env, "APP_PUBLIC_ORIGIN": "http://bi.example.com"})

    def test_semantic_catalog_gate_defaults_off_and_accepts_only_true_false(self):
        """计划 Task 4：语义目录默认关闭，开关只认 `true`/`false` 两个写法。

        “看起来像真”的文本（`1` / `yes` / `True`）一律拒绝而不是猜：这一条门禁
        决定启动时要不要连库做预检，把它误读成“开”或“关”都是用户可见差异。
        """
        from bi_agent.config import AppSettings, load_app_settings

        env = valid_app_env()
        self.assertFalse(load_app_settings(env).semantic_catalog_enabled)
        for off in ("false", " false ", "", "   "):
            with self.subTest(off=off):
                settings = load_app_settings({**env, "SEMANTIC_CATALOG_ENABLED": off})
                self.assertFalse(settings.semantic_catalog_enabled)
        for on in ("true", " true "):
            with self.subTest(on=on):
                settings = load_app_settings({**env, "SEMANTIC_CATALOG_ENABLED": on})
                self.assertTrue(settings.semantic_catalog_enabled)
        for bad in ("TRUE", "True", "1", "0", "yes", "no", "on", "off", "enabled",
                    "truthy", "tru", "false 1", ";", "true; DROP TABLE bi.orders"):
            with self.subTest(bad=bad):
                with self.assertRaisesRegex(ValueError, "SEMANTIC_CATALOG_ENABLED"):
                    load_app_settings({**env, "SEMANTIC_CATALOG_ENABLED": bad})
        # 模型那一层也是默认关：直接构造 AppSettings 不会自己把门禁推开。
        self.assertIs(AppSettings.model_fields["semantic_catalog_enabled"].default, False)

    def test_env_example_ships_the_semantic_catalog_gate_closed(self):
        """`.env.example` 是部署方抄的那份：新门禁必须写在那儿且写=false。"""
        import pathlib

        text = (pathlib.Path(__file__).resolve().parents[2] / ".env.example"
                ).read_text(encoding="utf-8")
        self.assertIn("SEMANTIC_CATALOG_ENABLED=false\n", text)

    def test_controlled_sql_gate_defaults_off_and_accepts_only_true_false(self):
        """计划 Task 1：受控 SQL 默认关，开关沿用 `_flag` 的 true/false 严格解析。

        两个门禁的依赖关系（受控 SQL 需要语义目录）不能让“看起来像真”的文本提前过关：
        所以坏值报的仍是本变量自己的名字。
        """
        from bi_agent.config import load_app_settings

        env = valid_app_env()
        self.assertFalse(load_app_settings(env).controlled_sql_enabled)
        for off in ("false", " false ", "", "   "):
            with self.subTest(off=off):
                settings = load_app_settings({**env, "CONTROLLED_SQL_ENABLED": off})
                self.assertFalse(settings.controlled_sql_enabled)
        for on in ("true", " true "):
            with self.subTest(on=on):
                settings = load_app_settings({
                    **env,
                    "SEMANTIC_CATALOG_ENABLED": "true",
                    "CONTROLLED_SQL_ENABLED": on,
                })
                self.assertTrue(settings.controlled_sql_enabled)
        for bad in ("TRUE", "True", "1", "0", "yes", "no", "on", "off", "enabled",
                    "truthy", "tru", "false 1", ";", "true; DROP TABLE bi.orders"):
            with self.subTest(bad=bad):
                # 故意把目录开着：报错只能来自本变量的严格解析，不是依赖门禁。
                with self.assertRaisesRegex(ValueError, "CONTROLLED_SQL_ENABLED"):
                    load_app_settings({
                        **env,
                        "SEMANTIC_CATALOG_ENABLED": "true",
                        "CONTROLLED_SQL_ENABLED": bad,
                    })

    def test_controlled_sql_requires_the_semantic_catalog_gate(self):
        """受控 SQL 只能建在语义目录之上：目录关着开 SQL 就稳定报错，不静默降级。"""
        from bi_agent.config import load_app_settings

        env = valid_app_env()
        for catalog_off in (None, "", "false", " false "):
            with self.subTest(catalog_off=catalog_off):
                broken = {**env, "CONTROLLED_SQL_ENABLED": "true"}
                if catalog_off is not None:
                    broken["SEMANTIC_CATALOG_ENABLED"] = catalog_off
                with self.assertRaises(ValueError) as caught:
                    load_app_settings(broken)
                self.assertEqual(
                    str(caught.exception), "CONTROLLED_SQL_REQUIRES_SEMANTIC_CATALOG")
                self.assertNotIn("postgresql", str(caught.exception))
        both = load_app_settings({**env, "SEMANTIC_CATALOG_ENABLED": "true",
                                 "CONTROLLED_SQL_ENABLED": "true"})
        self.assertTrue(both.semantic_catalog_enabled)
        self.assertTrue(both.controlled_sql_enabled)
        # 反向不成立：目录可以单独开（它不依赖探索层）。
        only_catalog = load_app_settings({**env, "SEMANTIC_CATALOG_ENABLED": "true"})
        self.assertTrue(only_catalog.semantic_catalog_enabled)
        self.assertFalse(only_catalog.controlled_sql_enabled)
        # 依赖错不能抢在严格解析前面：坏值先报“只能是 true 或 false”。
        with self.assertRaises(ValueError) as caught:
            load_app_settings({**env, "CONTROLLED_SQL_ENABLED": "TRUE"})
        self.assertIn("CONTROLLED_SQL_ENABLED 只能是 true 或 false", str(caught.exception))

    def test_absent_and_false_controlled_sql_gates_give_identical_settings(self):
        """变量缺席与显式 false 必须给出同一份配置，且不新增其它设置项。"""
        from bi_agent.config import AppSettings, load_app_settings

        absent = load_app_settings(valid_app_env())
        explicit = load_app_settings({**valid_app_env(), "CONTROLLED_SQL_ENABLED": "false"})
        self.assertEqual(absent, explicit)
        self.assertEqual(
            set(AppSettings.model_fields),
            {"app_dsn", "shop_ids", "environment", "allowed_subjects", "public_origin",
             "auth_subject_header", "semantic_catalog_enabled", "controlled_sql_enabled",
             "approved_query_memory_enabled", "approver_subjects", "approver_dsn",
             "isolated_analysis_enabled"})
        self.assertIs(AppSettings.model_fields["controlled_sql_enabled"].default, False)

    def test_env_example_ships_the_controlled_sql_gate_closed(self):
        """`.env.example` 是部署方抄的那份：新门禁必须写在那儿且只写一次。"""
        import pathlib

        text = (pathlib.Path(__file__).resolve().parents[2] / ".env.example"
                ).read_text(encoding="utf-8")
        self.assertIn("CONTROLLED_SQL_ENABLED=false\n", text)
        for key in ("CONTROLLED_SQL_ENABLED", "SEMANTIC_CATALOG_ENABLED"):
            with self.subTest(key=key):
                assignments = [line for line in text.splitlines()
                               if line.startswith(f"{key}=")]
                self.assertEqual(assignments, [f"{key}=false"])

    def test_approved_query_memory_gate_defaults_off_and_accepts_only_true_false(self):
        """计划 Task 1：记忆门禁默认关，开关沿用 `_flag` 的严格 true/false 解析。

        审核者与审核 DSN 只在门禁开启时从环境装入：关着的记忆功能连审核身份都不
        进配置，关与缺席必须给出逐字同一份设置。
        """
        from bi_agent.config import AppSettings, load_app_settings

        env = valid_app_env()
        self.assertFalse(load_app_settings(env).approved_query_memory_enabled)
        for off in ("false", " false ", "", "   "):
            with self.subTest(off=off):
                settings = load_app_settings(
                    {**env, "APPROVED_QUERY_MEMORY_ENABLED": off})
                self.assertFalse(settings.approved_query_memory_enabled)
        enabled_env = {
            **env,
            "APP_APPROVER_SUBJECTS": "reviewer-a",
            "BI_APPROVER_DSN": "postgresql://bi_approver:pw@localhost/bi_agent",
        }
        for on in ("true", " true "):
            with self.subTest(on=on):
                settings = load_app_settings(
                    {**enabled_env, "APPROVED_QUERY_MEMORY_ENABLED": on})
                self.assertTrue(settings.approved_query_memory_enabled)
        for bad in ("TRUE", "True", "1", "0", "yes", "no", "on", "off", "enabled",
                    "truthy", "tru", "false 1", ";", "true; DROP TABLE bi.orders"):
            with self.subTest(bad=bad):
                with self.assertRaisesRegex(ValueError,
                                            "APPROVED_QUERY_MEMORY_ENABLED"):
                    load_app_settings(
                        {**env, "APPROVED_QUERY_MEMORY_ENABLED": bad})
        self.assertIs(
            AppSettings.model_fields["approved_query_memory_enabled"].default, False)
        self.assertEqual(AppSettings.model_fields["approver_subjects"].default,
                         frozenset())
        self.assertIsNone(AppSettings.model_fields["approver_dsn"].default)

    def test_enabling_memory_requires_reviewers_and_a_separate_dsn(self):
        """开启记忆必须有审核者、审核 DSN，且审核身份不能搭在聊天连接上。"""
        from bi_agent.config import load_app_settings

        env = valid_app_env()
        approver_dsn = "postgresql://bi_approver:pw@localhost/bi_agent"
        enabled = {**env, "APPROVED_QUERY_MEMORY_ENABLED": "true"}
        with self.assertRaises(ValueError) as caught:
            load_app_settings(enabled)
        self.assertEqual(str(caught.exception),
                         "APPROVED_QUERY_MEMORY_REQUIRES_APPROVER_SUBJECTS")
        with self.assertRaises(ValueError) as caught:
            load_app_settings({**enabled, "APP_APPROVER_SUBJECTS": "reviewer-a"})
        self.assertEqual(str(caught.exception),
                         "APPROVED_QUERY_MEMORY_REQUIRES_APPROVER_DSN")
        with self.assertRaises(ValueError) as caught:
            load_app_settings({**enabled, "APP_APPROVER_SUBJECTS": "reviewer-a",
                               "BI_APPROVER_DSN": env["BI_APP_DSN"]})
        self.assertEqual(str(caught.exception),
                         "APPROVED_QUERY_MEMORY_APPROVER_DSN_MUST_DIFFER")
        settings = load_app_settings({
            **enabled,
            "APP_APPROVER_SUBJECTS": "reviewer-a, reviewer-b",
            "BI_APPROVER_DSN": approver_dsn,
        })
        self.assertEqual(settings.approver_subjects,
                         frozenset({"reviewer-a", "reviewer-b"}))
        self.assertEqual(settings.approver_dsn.get_secret_value(), approver_dsn)
        # SecretStr 的 repr/str 不落 DSN：与 app_dsn 同一口径。
        self.assertNotIn(approver_dsn, repr(settings))
        self.assertNotIn(approver_dsn, str(settings))

    def test_disabled_memory_gate_leaves_approver_settings_unloaded(self):
        """门禁关着时审核者与审核 DSN 不进配置：功能关就是全关。"""
        from bi_agent.config import load_app_settings

        settings = load_app_settings({
            **valid_app_env(),
            "APP_APPROVER_SUBJECTS": "reviewer-a",
            "BI_APPROVER_DSN": "postgresql://bi_approver:pw@localhost/bi_agent",
        })
        self.assertFalse(settings.approved_query_memory_enabled)
        self.assertEqual(settings.approver_subjects, frozenset())
        self.assertIsNone(settings.approver_dsn)

    def test_env_example_ships_the_approved_query_memory_gate_closed(self):
        """`.env.example` 是部署方抄的那份：新门禁写=false，审核者留空示例。"""
        import pathlib

        text = (pathlib.Path(__file__).resolve().parents[2] / ".env.example"
                ).read_text(encoding="utf-8")
        self.assertIn("APPROVED_QUERY_MEMORY_ENABLED=false\n", text)
        assignments = [line for line in text.splitlines()
                       if line.startswith("APPROVED_QUERY_MEMORY_ENABLED=")]
        self.assertEqual(assignments, ["APPROVED_QUERY_MEMORY_ENABLED=false"])
        self.assertIn("APP_APPROVER_SUBJECTS=\n", text)
        self.assertIn("BI_APPROVER_DSN=\n", text)

    def test_isolated_analysis_gate_defaults_off_and_accepts_only_true_false(self):
        """隔离分析计划 Task 1：门禁默认关，开关沿用 `_flag` 严格 true/false。

        与记忆门禁不同，本切片的门禁开启不需要任何附加环境项（loader/graph 属
        后续 Task）；但关与缺席必须给出逐字同一份设置，非法文本必须当场报错，
        不能静默翻转。
        """
        import pathlib

        from bi_agent.config import AppSettings, load_app_settings

        env = valid_app_env()
        self.assertFalse(load_app_settings(env).isolated_analysis_enabled)
        for off in ("false", " false ", ""):
            with self.subTest(off=off):
                self.assertFalse(load_app_settings(
                    {**env, "ISOLATED_ANALYSIS_ENABLED": off}
                ).isolated_analysis_enabled)
        for on in ("true", " true "):
            with self.subTest(on=on):
                self.assertTrue(load_app_settings(
                    {**env, "ISOLATED_ANALYSIS_ENABLED": on}
                ).isolated_analysis_enabled)
        for bad in ("TRUE", "True", "1", "0", "yes", "no", "on", "off",
                    "enabled", "tru", "false 1", ";", "true; DROP TABLE bi.orders"):
            with self.subTest(bad=bad):
                with self.assertRaisesRegex(ValueError,
                                            "ISOLATED_ANALYSIS_ENABLED"):
                    load_app_settings(
                        {**env, "ISOLATED_ANALYSIS_ENABLED": bad})
        self.assertIs(
            AppSettings.model_fields["isolated_analysis_enabled"].default, False)
        text = (pathlib.Path(__file__).resolve().parents[2] / ".env.example"
                ).read_text(encoding="utf-8")
        assignments = [line for line in text.splitlines()
                       if line.startswith("ISOLATED_ANALYSIS_ENABLED=")]
        self.assertEqual(assignments, ["ISOLATED_ANALYSIS_ENABLED=false"])

    def test_env_example_ships_the_monitor_gate_closed(self):
        """`.env.example` 是部署方抄的那份：监控门禁写=false，身份三件套只留空名。

        持续库存监控（计划 Task 1）默认关闭；变量只出现一次且不带任何凭证值，
        独立 DSN / 服务主体 / 策略清单都只有名字没有示例秘密。
        """
        import pathlib

        text = (pathlib.Path(__file__).resolve().parents[2] / ".env.example"
                ).read_text(encoding="utf-8")
        assignments = [line for line in text.splitlines()
                       if line.startswith("INVENTORY_MONITOR_ENABLED=")]
        self.assertEqual(assignments, ["INVENTORY_MONITOR_ENABLED=false"])
        self.assertIn("BI_MONITOR_DSN=\n", text)
        self.assertIn("MONITOR_SERVICE_SUBJECT=\n", text)
        self.assertIn("MONITOR_POLICY_REFS=\n", text)

    def test_selected_provider_uses_its_own_key(self):
        from bi_agent.config import load_model_settings

        env = {
            "LLM_PROVIDER": "deepseek",
            "LLM_MODEL": "demo-model",
            "DEEPSEEK_API_KEY": "fake-deepseek-key",
            "QWEN_API_KEY": "fake-qwen-key",
        }
        settings = load_model_settings(env)
        self.assertEqual(settings.api_key.get_secret_value(), "fake-deepseek-key")
        with self.assertRaises(ValueError):
            load_model_settings({**env, "LLM_PROVIDER": "unknown"})
        self.assertNotIn("fake-deepseek-key", repr(settings))

    def test_missing_model_id_rejected(self):
        from bi_agent.config import load_model_settings

        env = {"LLM_PROVIDER": "qwen", "LLM_MODEL": "", "QWEN_API_KEY": "k"}
        with self.assertRaises(ValueError):
            load_model_settings(env)

    def test_base_url_must_be_https(self):
        from bi_agent.config import load_model_settings

        env = {
            "LLM_PROVIDER": "qwen",
            "LLM_MODEL": "m",
            "QWEN_API_KEY": "k",
            "LLM_BASE_URL": "http://insecure.example.com/v1",
        }
        with self.assertRaises(ValueError):
            load_model_settings(env)


class KuaimaiTests(unittest.TestCase):
    def test_sign_and_empty_are_explicit(self):
        from bi_agent.kuaimai import KuaimaiError, parse_page, sign

        expected = hmac.new(b"test-secret", b"a1b2", hashlib.sha256).hexdigest().upper()
        self.assertEqual(sign({"b": "2", "a": "1", "sign": "old"}, "test-secret"), expected)
        self.assertTrue(parse_page({"success": True, "total": 0}).verified_empty)
        # C-6：省略 list 的接口只能读出“未核验的空”，不能当已覆盖。
        live_empty = parse_page({"success": True}, allow_omitted_list=True)
        self.assertEqual(live_empty.rows, [])
        self.assertFalse(live_empty.verified_empty)
        for body in ({"success": True}, {"success": True, "total": 2},
                     {"success": False, "code": "25"}):
            with self.assertRaises(KuaimaiError):
                parse_page(body)

    def _client(self, responses):
        from bi_agent.kuaimai import KuaimaiClient
        from bi_agent.config import SyncSettings
        from pydantic import SecretStr

        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            response = responses[min(len(calls) - 1, len(responses) - 1)]
            if callable(response):
                return response(request)
            return response

        transport = httpx.MockTransport(handler)
        settings = SyncSettings(
            writer_dsn=SecretStr("postgresql://localhost/test"),
            shop_ids=frozenset({"S1"}),
            app_key=SecretStr("fake-app-key"),
            app_secret=SecretStr("fake-app-secret"),
            access_token=SecretStr("fake-session"),
            refresh_token=SecretStr("fake-refresh"),
        )
        return KuaimaiClient(settings, httpx.Client(transport=transport)), calls

    def test_call_signs_sent_params(self):
        from bi_agent.kuaimai import ROUTER_URL, sign

        def response(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"success": True, "total": 0})

        client, calls = self._client([response])
        payload = client.call("erp.trade.list.query", {"userIds": "S1"})
        self.assertEqual(payload["total"], 0)
        request = calls[0]
        self.assertEqual(str(request.url), ROUTER_URL)
        sent = dict(httpx.QueryParams(request.content.decode()))
        self.assertEqual(sent["appKey"], "fake-app-key")
        self.assertEqual(sent["session"], "fake-session")
        self.assertEqual(sent["method"], "erp.trade.list.query")
        self.assertEqual(sent["version"], "1.0")
        self.assertEqual(sent["sign_method"], "hmac-sha256")
        expected = sign(sent, "fake-app-secret")
        self.assertEqual(sent["sign"], expected)

    def test_retry_on_429_and_5xx(self):
        from bi_agent.kuaimai import KuaimaiClient

        responses = [httpx.Response(429), httpx.Response(503),
                     httpx.Response(200, json={"success": True, "total": 0})]
        client, calls = self._client(responses)
        with patch("bi_agent.kuaimai._sleep") as sleep:
            payload = client.call("m", {})
        self.assertEqual(payload["total"], 0)
        self.assertEqual(len(calls), 3)
        self.assertEqual(sleep.call_count, 2)

    def test_auth_error_no_retry(self):
        from bi_agent.kuaimai import KuaimaiError

        client, calls = self._client([
            httpx.Response(200, json={"success": False, "code": "7",
                                      "message": "session失效"})])
        with self.assertRaises(KuaimaiError) as ctx:
            client.call("m", {})
        self.assertEqual(ctx.exception.code, "authentication")
        self.assertEqual(len(calls), 1)

    def test_gives_up_after_three_attempts(self):
        from bi_agent.kuaimai import KuaimaiError

        client, calls = self._client([httpx.Response(500)])
        with patch("bi_agent.kuaimai._sleep"):
            with self.assertRaises(KuaimaiError) as ctx:
                client.call("m", {})
        self.assertEqual(ctx.exception.code, "upstream")
        self.assertEqual(len(calls), 3)

    def test_timeout_and_network_map(self):
        from bi_agent.kuaimai import KuaimaiError

        client, calls = self._client([httpx.Response(401)])
        with self.assertRaises(KuaimaiError) as ctx:
            client.call("m", {})
        self.assertEqual(ctx.exception.code, "authentication")

        client, calls = self._client([
            httpx.Response(200, content=b"not-json")])
        with self.assertRaises(KuaimaiError) as ctx:
            client.call("m", {})
        self.assertEqual(ctx.exception.code, "invalid_response")

    def test_refresh_session_keeps_tokens(self):
        client, calls = self._client([
            httpx.Response(200, json={"success": True,
                                      "expireTime": "2026-10-07 14:00:00"})])
        expires = client.refresh_session(now=datetime(2026, 9, 7, 12, 0,
                                                     tzinfo=ZoneInfo("Asia/Shanghai")))
        self.assertEqual(expires.year, 2026)
        self.assertEqual(expires.month, 10)

    def test_refresh_session_rejects_changed_token(self):
        from bi_agent.kuaimai import KuaimaiError

        client, calls = self._client([
            httpx.Response(200, json={"success": True, "accessToken": "other-token",
                                      "expireTime": "2026-10-07 14:00:00"})])
        with self.assertRaises(KuaimaiError):
            client.refresh_session()

    def test_no_secrets_in_logs(self):
        client, calls = self._client([
            httpx.Response(500)])
        records = []
        handler = logging.Handler()
        handler.emit = records.append
        logger = logging.getLogger("bi_agent.kuaimai")
        logger.addHandler(handler)
        try:
            with patch("bi_agent.kuaimai._sleep"):
                with self.assertRaises(Exception):
                    client.call("m", {})
        finally:
            logger.removeHandler(handler)
        text = "".join(str(r.getMessage()) for r in records)
        self.assertNotIn("fake-app-secret", text)
        self.assertNotIn("fake-session", text)


class KuaimaiPageTests(unittest.TestCase):
    def test_row_type_check(self):
        from bi_agent.kuaimai import KuaimaiError, parse_page

        with self.assertRaises(KuaimaiError):
            parse_page({"success": True, "total": 1, "list": ["not-a-dict"]})
        page = parse_page({"success": True, "total": 1, "list": [{"sid": "E1"}],
                           "hasNext": False})
        self.assertEqual(page.rows[0]["sid"], "E1")
        self.assertFalse(page.verified_empty)
        self.assertIs(page.has_next, False)


class SyncNormalisationTests(unittest.TestCase):
    def test_trade_uses_documented_status_split_and_line_fields(self):
        """A1/A3/A4/A6：API字段应产生可复核的规范化结果。"""
        from bi_agent.sync import normalise_trade

        trade = normalise_trade({
            "sid": "E1", "userId": "S1", "updTime": 1788537600000,
            "unifiedStatus": "CLOSED", "sysStatus": "FINISHED",
            "splitType": 1, "splitSid": "E_PARENT",
            "orders": [{"id": "L1", "oid": "PLATFORM-L1", "type": 2,
                        "giftNum": "0"}],
        })

        self.assertFalse(trade["active"])
        self.assertEqual(trade["unified_status"], "CLOSED")
        self.assertEqual(trade["system_status"], "FINISHED")
        self.assertEqual(trade["split_parent_id"], "E_PARENT")
        self.assertEqual(trade["items"][0]["platform_line_id"], "PLATFORM-L1")
        self.assertEqual(trade["items"][0]["source_type"], 2)
        self.assertEqual(trade["items"][0]["line_kind"], "suite")
        self.assertFalse(trade["items"][0]["active"])

    def test_trade_uses_system_status_only_when_unified_status_is_missing(self):
        from bi_agent.sync import normalise_trade

        base = {"sid": "E1", "userId": "S1", "updTime": 1788537600000}
        self.assertFalse(normalise_trade({**base, "sysStatus": "CLOSED"})["active"])
        self.assertTrue(normalise_trade({
            **base, "unifiedStatus": "FINISHED", "sysStatus": "CLOSED",
        })["active"])

    def test_mixed_gift_line_keeps_sale_kind_and_reports_gift_quantity(self):
        """回归：`num>0` 且 `giftNum>0` 的混合行不得整行判为赠品。否则
        `line_kind <> 'gift'` 过滤会连同销售数量与该行分摊金额一起从商品排行消失。"""
        from bi_agent.sync import normalise_trade

        trade = normalise_trade({
            "sid": "E1", "userId": "S1", "updTime": 1788537600000,
            "orders": [
                {"oid": "L_MIX", "type": 0, "num": "2", "giftNum": "1",
                 "payAmount": "30"},
                {"oid": "L_PURE_GIFT", "type": 0, "num": "0", "giftNum": "3",
                 "payAmount": "0"},
            ],
        })

        mixed, pure = trade["items"]
        self.assertEqual(mixed["line_kind"], "sale")
        self.assertEqual(mixed["quantity"], Decimal("2"))
        self.assertEqual(mixed["gift_quantity"], Decimal("1"))
        self.assertEqual(mixed["allocated_paid_amount"], Decimal("30"))
        self.assertEqual(pure["line_kind"], "gift")
        self.assertEqual(pure["gift_quantity"], Decimal("3"))
        self.assertEqual(pure["quantity"], Decimal("0"))

    def test_aftersale_uses_finished_and_excludes_multi_value_void_status(self):
        from bi_agent.sync import normalise_aftersale

        record = normalise_aftersale({
            "aftersaleId": "A1", "userId": "S1", "modified": 1788537600000,
            "onlineStatus": 7, "status": "2,10", "finished": 1788624000000,
            "platformCompleteTime": 1788624000000,
        })

        self.assertEqual(record["work_status"], 2)
        self.assertEqual(record["system_completed_at"],
                         datetime.fromtimestamp(1788624000, tz=ZoneInfo("Asia/Shanghai")))
        self.assertFalse(record["platform_success"])

    def test_shop_sync_uses_active_flag(self):
        from bi_agent.sync import sync_shops

        class Transaction:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, traceback):
                return False

        class Connection:
            def __init__(self):
                self.parameters = []

            def transaction(self):
                return Transaction()

            def execute(self, sql, parameters):
                self.parameters.append(parameters)
                if "FROM bi.entity_refs" in sql:
                    # 引用落表的回读：按同一派生规则给出，别把断言带偏。
                    kind, keys = parameters
                    return _FakeResult([(key, ref_for_key(kind, key)) for key in keys])
                return _FakeResult([])   # 未命中变更：fetchone() 返回 None

        class Client:
            def call(self, method, parameters):
                return {"success": True, "total": 2, "hasNext": False, "list": [
                    {"userId": "S_DISABLED", "state": 1, "active": 0},
                    {"userId": "S_ACTIVE", "state": 4, "active": 1},
                ]}

        conn = Connection()
        self.assertEqual(sync_shops(conn, Client()), 2)
        self.assertFalse(conn.parameters[0][-1])
        self.assertTrue(conn.parameters[1][-1])


    def test_normalise_trade_treats_placeholder_pay_time_as_missing(self):
        """未付款/已关闭单的 payTime=2000-01-01 是占位值，不是真实支付时间。

        真实数据实测：抖音与快手各一单（WAIT_BUYER_PAY / CLOSED）带 946656000000。
        当成已支付时间入库会让未付的单独进支付日指标，也会让它拿到已核验支付事实。
        """
        from bi_agent.sync import normalise_trade

        trade = normalise_trade({
            "sid": "E_PLACE", "userId": "S1", "tid": "C_PLACE",
            "payTime": 946656000000,                       # 2000-01-01 00:00 +08
            "updTime": 1788825531000, "payAmount": "4.30",
            "unifiedStatus": "WAIT_BUYER_PAY",
            "orders": [{"oid": "L_P1", "tid": "C_PLACE", "itemSysId": "P_A",
                         "num": "1", "payAmount": "4.30"}],
        })

        self.assertIsNone(trade["paid_at"], "占位支付时间应判为未取得")
        self.assertEqual(trade["raw_pay_amount"], Decimal("4.30"), "金额仍要原样留住")
        self.assertEqual(trade["normalization_status"], "normal")
        self.assertIsNone(trade["items"][0]["paid_at"])

    def test_normalise_aftersale_treats_placeholder_completion_time_as_missing(self):
        from bi_agent.sync import normalise_aftersale

        record = normalise_aftersale({
            "aftersaleId": "A_PLACE", "userId": "S1", "tid": "C_P1",
            "rawRefundMoney": "10.00", "onlineStatus": 7, "status": 9,
            "modified": 1788825531000,
            "platformCompleteTime": 946656000000, "finished": 946656000000,
        })

        self.assertIsNone(record["platform_completed_at"])
        self.assertIsNone(record["system_completed_at"])
        self.assertFalse(record["platform_success"],
                         "没有真实完成时间就不能宣布平台退款已完成")

    def test_real_business_time_is_not_rejected_by_the_floor(self):
        """下限不能误伤真数据：2026 年的支付时间必须原样保留。"""
        from bi_agent.sync import normalise_trade

        trade = normalise_trade({
            "sid": "E_REAL", "userId": "S1", "tid": "C_REAL",
            "payTime": 1788797057000, "updTime": 1788825531000,
            "payAmount": "10.00",
            "orders": [{"oid": "L_R1", "tid": "C_REAL", "itemSysId": "P_A",
                         "num": "1", "payAmount": "10.00"}],
        })

        self.assertIsNotNone(trade["paid_at"])
        self.assertEqual(trade["paid_at"].year, 2026)


class SyncSchemaTests(unittest.TestCase):
    def test_sync_schema_rejects_missing_mapping_columns(self):
        from bi_agent.kuaimai import KuaimaiError
        from bi_agent.sync import assert_sync_schema

        class Rows:
            def __init__(self, rows):
                self.rows = rows

            def fetchall(self):
                return self.rows

        class Connection:
            def __init__(self, rows):
                self.rows = rows

            def execute(self, sql, parameters):
                return Rows(self.rows)

        assert_sync_schema(Connection([
            ("orders", "unified_status"), ("orders", "system_status"),
            ("order_items", "source_type"),
            ("products", "title"), ("products", "source_modified_at"),
        ]))
        with self.assertRaisesRegex(KuaimaiError, "schema_outdated"):
            assert_sync_schema(Connection([("orders", "unified_status")]))
        # 005 未执行时同步必须整体拒写，不能让商品名默默一直缺失。
        with self.assertRaisesRegex(KuaimaiError, "schema_outdated"):
            assert_sync_schema(Connection([
                ("orders", "unified_status"), ("orders", "system_status"),
                ("order_items", "source_type"),
            ]))


class SyncBackfillTests(unittest.TestCase):
    """C-2：回填必须重新取结束时刻，否则扫描段恒空、水位建不起来。"""

    T0 = datetime(2026, 9, 8, 9, 0, tzinfo=ZoneInfo("Asia/Shanghai"))

    def _backfill(self, *, backfill_seconds: int):
        from bi_agent import sync

        class Transaction:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, traceback):
                return False

        class Connection:
            def __init__(self):
                self.statements: list[tuple[str, object]] = []

            def transaction(self):
                return Transaction()

            def execute(self, sql, parameters=None):
                self.statements.append((sql, parameters))

        conn = Connection()
        windows: list[tuple[str, str, object]] = []

        def run_window(_conn, _client, **kwargs):
            windows.append((kwargs["mode"], kwargs["entity"], kwargs["window"]))
            return 0

        t1 = self.T0 + timedelta(seconds=backfill_seconds)
        with patch.object(sync, "_run_window", side_effect=run_window), patch.object(
            sync, "check_cohort_window", return_value=0
        ):
            stats = sync._backfill_shop(conn, Mock(), shop_id="S1", days=2,
                                        t0=self.T0, now=lambda: t1)
        return conn, windows, stats, t1

    def test_scan_segment_covers_the_whole_backfill_period(self):
        conn, windows, stats, t1 = self._backfill(backfill_seconds=36 * 3600)

        # 回填本体照旧按业务时间跑，水位扫描段覆盖 [t0, 回填结束时刻)。
        self.assertEqual({mode for mode, _entity, _w in windows}, {"backfill", "scan"})
        for entity in ("orders", "aftersales_occurrence"):
            scanned = [window for mode, item, window in windows
                       if mode == "scan" and item == entity]
            self.assertTrue(scanned)  # 旧行为：t1==t0 使这里恒空
            self.assertEqual(scanned[0].start, self.T0)
            self.assertEqual(scanned[-1].end, t1)
            for previous, following in zip(scanned, scanned[1:]):
                self.assertEqual(previous.end, following.start)
        backfilled = [window for mode, entity, window in windows
                      if mode == "backfill" and entity == "orders"]
        self.assertEqual(backfilled[0].start, self.T0 - timedelta(days=2))
        self.assertEqual(backfilled[-1].end, self.T0)
        # data_as_of 公布的是回填结束时刻，不是开工瞬间。
        published = [parameters[0] for sql, parameters in conn.statements
                     if "SET data_as_of" in sql]
        self.assertEqual(published, [t1] * 3)
        self.assertGreater(t1, self.T0)
        self.assertEqual(stats["cohort_windows"], 2)

    def test_frozen_clock_still_advances_a_scan_window(self):
        """时钟不动或回拨时，也要留下能建立水位的扫描窗口。"""
        from bi_agent.sync import SYNC_OVERLAP

        _conn, windows, _stats, _t1 = self._backfill(backfill_seconds=0)

        scanned = [window for mode, _entity, window in windows if mode == "scan"]
        self.assertTrue(scanned)
        self.assertEqual(scanned[-1].end, self.T0 + SYNC_OVERLAP)

    def test_backfill_entrypoint_no_longer_pins_t1_to_t0(self):
        """main() 不能再把 t1 当参数传进去（C-2 的错源）。"""
        import inspect

        from bi_agent.sync import _backfill_shop

        parameters = inspect.signature(_backfill_shop).parameters
        self.assertNotIn("t1", parameters)
        self.assertIn("now", parameters)


class ProductMasterNormalisationTests(unittest.TestCase):
    """商品档案只读入库：名称用于展示，不能臆造；成本只当内部参考列。"""

    TZ = ZoneInfo("Asia/Shanghai")

    @staticmethod
    def _raw(**overrides):
        raw = {"sysItemId": 548597548708352, "title": "接头-元发", "outerId": "JT-01",
               "type": 0, "activeStatus": 1, "itemCategoryNames": "气动配件",
               "purchasePrice": 3.5, "modified": 1788166354000}
        raw.update(overrides)
        return raw

    def test_documented_fields_are_mapped_by_explicit_name(self):
        from bi_agent.sync import normalise_item_master

        item = normalise_item_master(self._raw())

        self.assertEqual(item["normalization_status"], "normal")
        self.assertEqual(item["product_id"], "548597548708352")
        self.assertEqual(item["title"], "接头-元发")
        self.assertEqual(item["outer_id"], "JT-01")
        self.assertEqual(item["item_type"], "0")
        self.assertEqual(item["category"], "气动配件")
        self.assertTrue(item["active"])
        self.assertEqual(item["purchase_price"], Decimal("3.5"))
        self.assertEqual(item["source_modified_at"].tzinfo, self.TZ)

    def test_missing_sys_item_id_is_invalid_and_never_guessed(self):
        from bi_agent.sync import normalise_item_master

        for raw in ({"title": "无号商品"}, self._raw(sysItemId=None), self._raw(sysItemId=" ")):
            with self.subTest(raw=raw):
                item = normalise_item_master(raw)
                self.assertEqual(item["normalization_status"], "invalid")
                self.assertIsNone(item["product_id"])

    def test_blank_title_stays_blank_but_needs_review(self):
        """档案没名字就是没名字：不得拿 outerId 拼一个看起来像名字的值。"""
        from bi_agent.sync import normalise_item_master

        item = normalise_item_master(self._raw(title="  "))

        self.assertEqual(item["title"], "")
        self.assertEqual(item["normalization_status"], "needs_review")

    def test_disabled_archive_is_marked_inactive(self):
        from bi_agent.sync import normalise_item_master

        for raw in (self._raw(activeStatus=0), self._raw(activeStatus=None)):
            with self.subTest(raw=raw):
                self.assertFalse(normalise_item_master(raw)["active"])

    def test_non_numeric_cost_becomes_null_instead_of_zero(self):
        """成本缺失不能当 0 入库，否则后续毛利会把无成本当成零成本。"""
        from bi_agent.sync import normalise_item_master

        for raw in (self._raw(purchasePrice=""), self._raw(purchasePrice="abc"),
                    self._raw(purchasePrice=True), self._raw(purchasePrice=None)):
            with self.subTest(raw=raw):
                self.assertIsNone(normalise_item_master(raw)["purchase_price"])

    def test_unparseable_modified_is_null_not_now(self):
        from bi_agent.sync import normalise_item_master

        item = normalise_item_master(self._raw(modified="不是时间"))

        self.assertIsNone(item["source_modified_at"])


class ItemNameSnapshotTests(unittest.TestCase):
    """成交名称快照：订单响应实测带 sysTitle / sysSkuPropertiesName，不额外调接口。"""

    @staticmethod
    def _item(**overrides):
        raw = {"id": "L1", "oid": "PL1", "tid": "C1", "itemSysId": "P1", "skuSysId": "S1",
               "num": "1", "payAmount": "10", "type": 0,
               "sysTitle": "接头-元发", "sysSkuPropertiesName": "接头-元发适五20PP"}
        raw.update(overrides)
        return raw

    def _normalised(self, **overrides):
        from bi_agent.sync import normalise_trade

        trade = normalise_trade({
            "sid": "E1", "userId": "S1", "tid": "C1", "payAmount": "10",
            "updTime": 1788166354000, "payTime": 1788166354000,
            "orders": [self._item(**overrides)],
        })
        return trade["items"][0]

    def test_documented_line_text_fields_are_kept_as_snapshots(self):
        item = self._normalised()

        self.assertEqual(item["product_name_snapshot"], "接头-元发")
        self.assertEqual(item["sku_label_snapshot"], "接头-元发适五20PP")

    def test_blank_line_text_stays_null_instead_of_borrowing_the_platform_title(self):
        item = self._normalised(sysTitle="  ", sysSkuPropertiesName=None,
                               title="平台长标题不算商品名")

        self.assertIsNone(item["product_name_snapshot"])
        self.assertIsNone(item["sku_label_snapshot"])


class PageCompletionEvidenceTests(unittest.TestCase):
    """C-6：“没拿到 list”不等于“确定没有记录”；covered 只能由正向完成证据支撑。"""

    TZ = ZoneInfo("Asia/Shanghai")

    def test_missing_list_without_evidence_is_untrusted_even_when_allowed(self):
        from bi_agent.kuaimai import parse_page

        page = parse_page({"success": True}, allow_omitted_list=True)

        self.assertEqual(page.rows, [])
        self.assertFalse(page.verified_empty)

    def test_missing_list_without_the_allowance_is_unknown_empty(self):
        from bi_agent.kuaimai import KuaimaiError, parse_page

        for body in ({"success": True}, {"success": True, "cursor": "c1"}):
            with self.subTest(body=body):
                with self.assertRaises(KuaimaiError) as ctx:
                    parse_page(body)
                self.assertEqual(ctx.exception.code, "unknown_empty")

    def test_gateway_error_envelope_is_not_an_empty_page(self):
        """网关错误页（无 success、无 list）必须报错，而不是发布成空窗口。"""
        from bi_agent.kuaimai import KuaimaiError, parse_page

        with self.assertRaises(KuaimaiError) as ctx:
            parse_page({"error_code": "15", "error_msg": "Remote service error",
                        "request_id": "abc"})
        self.assertEqual(ctx.exception.code, "unknown_empty")

    def test_nonzero_total_without_list_is_invalid(self):
        from bi_agent.kuaimai import KuaimaiError, parse_page

        with self.assertRaises(KuaimaiError) as ctx:
            parse_page({"success": True, "total": 3})
        self.assertEqual(ctx.exception.code, "invalid_response")

    def test_positive_evidence_makes_the_empty_page_verified(self):
        from bi_agent.kuaimai import parse_page

        for body in ({"success": True, "total": 0},
                     {"success": True, "total": 0, "list": []},
                     {"success": True, "hasNext": False},
                     {"success": True, "hasNext": False, "list": []}):
            with self.subTest(body=body):
                self.assertTrue(parse_page(body).verified_empty)

    def test_string_total_zero_is_not_completion_evidence(self):
        from bi_agent.kuaimai import KuaimaiError, parse_page

        with self.assertRaises(KuaimaiError):
            parse_page({"success": True, "total": "0"})

    def test_goods_envelope_rows_are_read_from_the_items_key(self):
        """实测 `item.list.query` 返回 `{"items": [...], "total": 435}`，不是 `list`。"""
        from bi_agent.kuaimai import parse_page

        page = parse_page(
            {"success": True, "total": 2,
             "items": [{"sysItemId": 548597548708352}, {"sysItemId": 548597548708353}]},
            list_key="items")

        self.assertEqual(len(page.rows), 2)
        self.assertEqual(page.total, 2)
        self.assertFalse(page.verified_empty)

    def test_goods_envelope_empty_list_with_total_zero_is_verified(self):
        from bi_agent.kuaimai import parse_page

        page = parse_page({"success": True, "total": 0, "items": []}, list_key="items")

        self.assertEqual(page.rows, [])
        self.assertTrue(page.verified_empty)

    def test_alternate_list_key_never_weakens_completion_evidence(self):
        """换分页键不得把「有总数却没列表」读成空页。"""
        from bi_agent.kuaimai import KuaimaiError, parse_page

        with self.assertRaises(KuaimaiError) as ctx:
            parse_page({"success": True, "total": 3}, list_key="items")
        self.assertEqual(ctx.exception.code, "invalid_response")

    def test_default_list_key_stays_list_for_the_trade_channel(self):
        from bi_agent.kuaimai import parse_page

        page = parse_page({"success": True, "total": 1, "list": [{"sid": "E1"}]})

        self.assertEqual(page.rows, [{"sid": "E1"}])

    def test_no_sync_call_site_opens_the_allowance(self):
        """C-6：宽容只允许给“不承载覆盖证据”的归档通道，在线通道永远严格。

        2026-09-11 真实账号实测推翻旧前提：`erp.trade.list.query` 的归档通道
        （queryType=1）在窗口内无归档单时既不回 `list` 也不回 `total`，
        只会回 `{"success": true, "traceId": ...}`。
        但“省略 list 永远不是完成证据”依旧成立，所以约束改成：
        允许位只能出现在归档分页调用上，并且全库只出现一次；
        在线游标通道（建立覆盖的那一条）不得拿它当退路。
        """
        import inspect

        from bi_agent import sync

        source = inspect.getsource(sync)

        self.assertIn("C-6", source)
        self.assertEqual(source.count("allow_omitted_list=True"), 1,
                         "宽容位不得扩散到其他调用点")
        cursor_calls = [line for line in source.splitlines()
                        if "_fetch_orders_cursor(" in line or "time_type=\"pay_time\"" in line]
        self.assertTrue(any("query_type=\"0\"" in line for line in cursor_calls),
                        "在线通道调用应存在且保持严格解析")
        for line in source.splitlines():
            if "_fetch_orders_cursor(" in line and "allow_omitted_list" in line:
                self.fail("在线订单通道不得省略不可信空")

    def test_backfill_accepts_archive_channel_omitting_list(self):
        """真实形状回放：在线通道给出 total，归档通道只回 success 空信封。"""
        from bi_agent import sync

        class Client:
            def call(self, method, params):
                if params.get("useCursor") == "true":
                    # 在线通道：实测带 total，空集时是 total=0
                    return {"success": True, "total": 1,
                            "list": [{"sid": "E1", "userId": "S1", "tid": "C1",
                                       "payAmount": "10.00",
                                       "orders": [{"oid": "L1", "itemSysId": "P1",
                                                    "num": "1", "payAmount": "10.00"}]}],
                            "hasNext": False}
                # 归档通道（queryType=1）：实测省略 list 与 total
                return {"success": True, "traceId": "t-1"}

        rows = list(sync.fetch_window(Client(), entity="orders", shop_id="S1",
                                      window=sync.Window(
                                          datetime(2026, 9, 9, tzinfo=self.TZ),
                                          datetime(2026, 9, 10, tzinfo=self.TZ)),
                                      mode="backfill"))

        self.assertEqual([row["sid"] for row in rows], ["E1"],
                         "在线通道数据必须拉到，归档空页不得抛不可信空")

    def test_backfill_still_raises_when_online_channel_gives_no_evidence(self):
        """归档宽容不得变成覆盖退路：在线通道无证据仍要报错。"""
        from bi_agent import sync
        from bi_agent.kuaimai import KuaimaiError

        class Client:
            def call(self, method, params):
                return {"success": True, "traceId": "t-1"}

        with self.assertRaises(KuaimaiError) as ctx:
            list(sync.fetch_window(Client(), entity="orders", shop_id="S1",
                                   window=sync.Window(
                                       datetime(2026, 9, 9, tzinfo=self.TZ),
                                       datetime(2026, 9, 10, tzinfo=self.TZ)),
                                   mode="backfill"))

        self.assertEqual(ctx.exception.code, "unknown_empty")

    class _Connection:
        def __init__(self):
            self.statements: list[str] = []

        def transaction(self):
            return _NullTransaction()

        def execute(self, sql, parameters=None):
            self.statements.append(" ".join(sql.split()))
            return _FakeResult([])

    def _sync_window(self, conn, payload: dict) -> int:
        from bi_agent import sync

        class Client:
            def call(self, method, params):
                return dict(payload)

        window = sync.Window(datetime(2026, 9, 5, tzinfo=self.TZ),
                             datetime(2026, 9, 6, tzinfo=self.TZ))
        return sync.sync_window(conn, Client(), entity="orders", shop_id="S1",
                                window=window, mode="backfill")

    @staticmethod
    def _covered(conn) -> bool:
        return any("covered = covered +" in sql for sql in conn.statements)

    def test_window_without_list_and_without_evidence_is_not_covered(self):
        from bi_agent.kuaimai import KuaimaiError

        conn = self._Connection()
        with self.assertRaises(KuaimaiError) as ctx:
            self._sync_window(conn, {"success": True})

        self.assertEqual(ctx.exception.code, "unknown_empty")
        self.assertFalse(self._covered(conn))

    def test_total_zero_window_is_covered(self):
        conn = self._Connection()
        accepted = self._sync_window(conn, {"success": True, "total": 0})

        self.assertEqual(accepted, 0)
        self.assertTrue(self._covered(conn))

    def test_has_next_false_window_is_covered(self):
        conn = self._Connection()
        self._sync_window(conn, {"success": True, "list": [], "hasNext": False})

        self.assertTrue(self._covered(conn))


class PaymentStubConnection:
    """rebuild_payments / _determine_payment 的最小连接替身。

    故意把拆合单的行级交叉核对造干“行缺失/行额为负”，验证降级守卫。
    """

    def __init__(self, *, orders, items_by_commercial=None, upsert_applies=True):
        self.orders = [tuple(row) for row in orders]
        self.items_by_commercial = items_by_commercial or {}
        self.upsert_applies = upsert_applies
        self.statements: list[tuple[str, tuple]] = []

    def execute(self, sql, parameters=()):
        text = " ".join(sql.split())
        params = tuple(parameters)
        self.statements.append((text, params))
        if "INSERT INTO bi.order_payments" in text:
            return _FakeResult([("C1",)] if self.upsert_applies else [])
        if "FROM bi.order_items i" in text:
            return _FakeResult(self.items_by_commercial.get(params[1], []))
        if "FROM bi.orders" in text:
            return _FakeResult(self.orders)
        return _FakeResult([])


class PaymentDowngradeGuardTests(unittest.TestCase):
    """C-5：undetermined 重放不得把已核验支付清零。"""

    TZ = ZoneInfo("Asia/Shanghai")
    PAID_AT = datetime(2026, 9, 2, 10, 0, tzinfo=TZ)
    UPDATED_AT = datetime(2026, 9, 2, 11, 0, tzinfo=TZ)
    # 单ERP单跳两商业单：跳过单头捷径，走行级交叉核对
    ORDERS = [("E1", ["C1", "C2"], PAID_AT, Decimal("100.00"), UPDATED_AT)]

    def setUp(self):
        from bi_agent.sync import GUARD_STATS

        GUARD_STATS.payment_downgrade_blocked = 0

    def tearDown(self):
        from bi_agent.sync import GUARD_STATS

        GUARD_STATS.payment_downgrade_blocked = 0

    def test_undetermined_replay_is_blocked_and_keeps_the_verified_row(self):
        from bi_agent import sync

        # upsert_applies=False == DO UPDATE 的 WHERE 谓词为假（整条语句影响 0 行）
        conn = PaymentStubConnection(orders=self.ORDERS, upsert_applies=False)
        with self.assertLogs("bi_agent.sync", level="WARNING") as logs:
            blocked = sync.rebuild_payments(conn, "S1", {"C1"})
            self.assertTrue(any("payment downgrade blocked" in line
                                for line in logs.output), logs.output)
            # 告警不带订单号明文
            self.assertFalse(any(" C1" in line for line in logs.output), logs.output)

        self.assertEqual(blocked, 1)
        self.assertEqual(sync.GUARD_STATS.payment_downgrade_blocked, 1)
        upserts = [sql for sql, _ in conn.statements
                   if "INSERT INTO bi.order_payments" in sql]
        self.assertEqual(len(upserts), 1)
        # 被拦下时不能再抹掉行级核验证据（否则下一轮重建也没依据）
        self.assertFalse([sql for sql, _ in conn.statements
                          if "allocation_verified = false" in sql])

    def test_guard_predicate_is_part_of_the_upsert(self):
        from bi_agent.sync import GUARD_STATS, _PAYMENT_UPSERT_SQL

        guard = " ".join(_PAYMENT_UPSERT_SQL.split())
        self.assertIn("ON CONFLICT (shop_id, commercial_id) DO UPDATE SET", guard)
        self.assertIn("WHERE EXCLUDED.verified", guard)
        self.assertIn("NOT bi.order_payments.verified", guard)
        self.assertIn("EXCLUDED.source_updated_at IS NULL", guard)   # orphan 允许撤销
        self.assertIn("EXCLUDED.source_updated_at > bi.order_payments.source_updated_at",
                      guard)
        self.assertIn("RETURNING", guard)
        self.assertEqual(GUARD_STATS.payment_downgrade_blocked, 0)

    def test_upsert_that_applies_still_writes_the_unverified_row(self):
        """旧行本就未核验时不拦截：正常降级仍要写下去。"""
        from bi_agent import sync

        conn = PaymentStubConnection(orders=self.ORDERS, upsert_applies=True)
        blocked = sync.rebuild_payments(conn, "S1", {"C1"})

        self.assertEqual(blocked, 0)
        self.assertTrue([sql for sql, _ in conn.statements
                         if "allocation_verified = false" in sql])

    def test_negative_item_allocation_never_gets_a_verified_stamp(self):
        """行级路径缺的正是单头路径那个 >= 0 守卫。"""
        from bi_agent.sync import _determine_payment

        conn = PaymentStubConnection(
            orders=self.ORDERS,
            items_by_commercial={"C1": [(Decimal("-5.00"), self.PAID_AT)],
                                 "C2": [(Decimal("105.00"), self.PAID_AT)]})
        amount, paid_at, basis, verified, _updated = _determine_payment(
            conn, "S1", "C1", self.ORDERS)

        self.assertEqual(amount, Decimal("-5.00"))
        self.assertIsNotNone(paid_at)
        self.assertFalse(verified)
        self.assertEqual(basis, "items")


class MetricInputTests(unittest.TestCase):
    def test_date_defaults_and_bounds(self):
        from bi_agent.metrics import QueryRequest, resolve_period

        now = datetime(2026, 9, 8, 9, tzinfo=ZoneInfo("Asia/Shanghai"))
        self.assertEqual(resolve_period("最近7天", now=now),
                         (date(2026, 9, 1), date(2026, 9, 8)))
        with self.assertRaises(ValueError):
            QueryRequest(start="2025-01-01", end="2026-09-08", shop_ids=["S1"],
                         metrics=["paid_amount"])

    def test_same_bounds_rejected(self):
        from bi_agent.metrics import QueryRequest

        with self.assertRaises(ValueError):
            QueryRequest(start="2026-09-01", end="2026-09-01", shop_ids=["S1"],
                         metrics=["paid_amount"])

    def test_unknown_metric_rejected(self):
        from bi_agent.metrics import QueryRequest

        with self.assertRaises(ValueError):
            QueryRequest(start="2026-09-01", end="2026-09-08", shop_ids=["S1"],
                         metrics=["widget_refund_rate"])

    def test_non_cny_rejected(self):
        from bi_agent.metrics import QueryRequest

        with self.assertRaises(ValueError):
            QueryRequest(start="2026-09-01", end="2026-09-08", shop_ids=["S1"],
                         metrics=["paid_amount"], currency="USD")

    def test_product_group_rejects_non_product_metrics(self):
        from bi_agent.metrics import QueryRequest

        with self.assertRaises(ValueError):
            QueryRequest(start="2026-09-01", end="2026-09-08", shop_ids=["S1"],
                         metrics=["paid_amount"], group_by="product")
        request = QueryRequest(start="2026-09-01", end="2026-09-08", shop_ids=["S1"],
                               metrics=["quantity"], group_by="product")
        self.assertEqual(request.group_by, "product")

    def test_partitioned_policy_only_combines_with_product_grouping(self):
        """分区口径只在商品分组下成立：别的分组要么用 strict，要么用 separate。"""
        from bi_agent.metrics import QueryRequest

        with self.assertRaises(ValueError):
            QueryRequest(start="2026-09-01", end="2026-09-08", shop_ids=["S1"],
                         metrics=["paid_amount"], group_by="shop",
                         basis_policy="partitioned")
        request = QueryRequest(start="2026-09-01", end="2026-09-08", shop_ids=["S1"],
                               metrics=["product_paid_amount"], group_by="product",
                               basis_policy="partitioned")
        self.assertEqual(request.basis_policy, "partitioned")

    def test_half_month_resolves_to_the_last_fifteen_completed_days(self):
        """「近半个月」= 最近 15 个完整自然日（含今天的窗口会把没跑完的一天算进排名）。"""
        from bi_agent.metrics import resolve_period, resolve_period_detail

        now = datetime(2026, 9, 8, 9, tzinfo=ZoneInfo("Asia/Shanghai"))
        self.assertEqual(resolve_period("近半个月", now=now),
                         (date(2026, 8, 24), date(2026, 9, 8)))
        self.assertEqual(resolve_period("最近半个月最好的10个商品", now=now),
                         (date(2026, 8, 24), date(2026, 9, 8)))
        self.assertEqual(resolve_period_detail("近半个月", now=now).kind, "half_month")
        # 反恒真：既有词表的返回值形状与窗口不变（二元组，且今天仍算相对词）。
        for text, expected in (
                ("最近7天", (date(2026, 9, 1), date(2026, 9, 8))),
                ("9月1日至7日支付金额", (date(2026, 9, 1), date(2026, 9, 8))),
                ("那上个月呢", (date(2026, 8, 1), date(2026, 9, 1))),
                ("今天的支付额", (date(2026, 9, 8), date(2026, 9, 9))),
                ("照上次那样", None)):
            with self.subTest(text=text):
                self.assertEqual(resolve_period(text, now=now), expected)
        # 解析种类分开报：显式日期与相对日期对窗口边界的权威程度不同。
        self.assertEqual(resolve_period_detail("9月1日至7日支付金额", now=now).kind,
                         "explicit_range")
        self.assertEqual(resolve_period_detail("2026-09-01的支付额", now=now).kind,
                         "explicit_range")
        self.assertEqual(resolve_period_detail("那上个月呢", now=now).kind, "relative")
        self.assertIsNone(resolve_period_detail("照上次那样", now=now))

    def test_spoken_dates(self):
        from bi_agent.metrics import resolve_period

        now = datetime(2026, 9, 8, 9, tzinfo=ZoneInfo("Asia/Shanghai"))
        self.assertEqual(resolve_period("9月1日至7日支付金额", now=now),
                         (date(2026, 9, 1), date(2026, 9, 8)))
        self.assertEqual(resolve_period("2026-09-01的支付额", now=now),
                         (date(2026, 9, 1), date(2026, 9, 2)))
        self.assertEqual(resolve_period("那上个月呢", now=now),
                         (date(2026, 8, 1), date(2026, 9, 1)))
        self.assertEqual(resolve_period("今天的支付额", now=now),
                         (date(2026, 9, 8), date(2026, 9, 9)))
        self.assertIsNone(resolve_period("照上次那样", now=now))


def _model_settings(provider: str):
    from bi_agent.config import load_model_settings

    key = f"{provider.upper()}_API_KEY"
    return load_model_settings({
        "LLM_PROVIDER": provider, "LLM_MODEL": "demo-model",
        key: f"fake-{provider}-key",
    })


class _FakeResult:
    def __init__(self, rows):
        self.rows = [tuple(row) for row in rows]

    def fetchall(self):
        return list(self.rows)

    def fetchone(self):
        return self.rows[0] if self.rows else None


class _NullTransaction:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False


from psycopg.types.range import Range   # 替身要返回带 lower/upper 的真区间对象


class FakeWarehouse:
    """reporting 视图的内存替身：按真实 SQL 的过滤/聚合/排序/LIMIT 语义回放。

    只服务离线用例（验证“截断后给什么状态”这类控制流）；真实 SQL 行为在
    tests.test_db 的一次性库里跑。
    """

    def __init__(self, *, daily_rows=(), product_rows=(), shops=(), data_as_of=None,
                 cohort=(None, None), refund_gap=(0, 0, 0),
                 unverified_payments=(0, 0, 0, 0), shop_profiles=(),
                 catalog_version=7,
                 quality_status="passed", quality_rule=QUALITY_RULE,
                 capabilities=("paid_amount", "paid_orders", "erp_documents", "aov",
                               "quantity", "product_paid_amount", "refund_amount",
                               "cash_difference", "cohort_refund_rate")):
        self.daily_rows = [tuple(row) for row in daily_rows]
        # 视图列形以 tests.dbfixtures.PRODUCT_DAILY_COLUMNS 为单一真源，这里不再手抄列数：
        # 测试行没给末尾的名称 / 成交快照 / 规格列就按契约补上。
        from tests.dbfixtures import PRODUCT_DAILY_COLUMNS

        width = len(PRODUCT_DAILY_COLUMNS)
        self.product_rows = [tuple(row) if len(row) == width
                             else tuple(row) + (f"档案-{row[2]}",)
                             + (None,) * (width - len(row) - 1)
                             for row in product_rows]
        self.shop_profiles = [tuple(row) for row in shop_profiles]
        self.catalog_version = catalog_version
        self.capabilities = tuple(capabilities)
        # v_shops 现在返回五列（带平台与能力标签）：用例仍按 (店, 开关, 币种) 写，
        # 健康替身默认“已登记平台 + 全部指标已授予能力”，不把契约变化注入每个离线用例。
        self.shops = [self._shop_row(row) for row in shops]
        self.data_as_of = data_as_of
        # 替身默认代表“已对账通过的健康库”；真实库默认是 unknown，两边不同。
        self.quality_status = quality_status
        self.quality_rule = quality_rule
        self.cohort = tuple(cohort)
        # 退款归属与未认证支付诊断：默认“没有这类限制”，与真实健康库一致。
        self.refund_gap = tuple(refund_gap)
        self.unverified_payments = tuple(unverified_payments)
        self.statements: list[tuple[str, tuple]] = []

    def _shop_row(self, row: tuple) -> tuple:
        """把用例写的 (店, 开关, 币种) 补齐成 v_shops 的五列形状。"""
        row = tuple(row)
        if len(row) == 3:
            return row + ("fxg", list(self.capabilities))
        if len(row) == 4:
            return row + (list(self.capabilities),)
        return row

    @property
    def info(self):
        import psycopg

        class _Info:
            transaction_status = psycopg.pq.TransactionStatus.IDLE

        return _Info()

    def transaction(self):
        return _NullTransaction()

    def fetches(self, view: str) -> list[tuple[str, tuple]]:
        return [(sql, params) for sql, params in self.statements if view in sql]

    def execute(self, sql, parameters=()):
        text = " ".join(sql.split())
        params = tuple(parameters)
        self.statements.append((text, params))
        if "set_config(" in text or text.startswith("SET TRANSACTION"):
            return _FakeResult([])
        catalog = catalog_rows(text, shops=self.shop_profiles,
                               version=self.catalog_version)
        if catalog is not None:
            return _FakeResult(catalog.fetchall())
        if "FROM reporting.v_shops" in text:
            if text.startswith("SELECT shop_id, platform"):
                # 覆盖门禁问的是“这个平台有没有登记来源”，与指标能力分开归因。
                return _FakeResult([(row[0], row[3]) for row in self.shops
                                    if row[0] in params[0]])
            return _FakeResult([row for row in self.shops if row[0] in params[0]])
        if "FROM reporting.v_coverage" in text:
            # 生产代码一条 SQL 判完整覆盖：(店铺, 已覆盖段, 缺口段, 截止, 质量)。
            if self.data_as_of is None:
                return _FakeResult([])                    # 没有状态行 = 未知
            start_ts, end_ts = params[0], params[1]
            covered = [Range(start_ts, end_ts, "[)")]
            return _FakeResult([(shop_id, covered, [], self.data_as_of,
                                self.quality_status, self.quality_rule)
                                for shop_id in params[6]])
        if "FROM reporting.v_source_batches" in text:
            return _FakeResult([])                        # 离线用例不伪造批次血缘
        if "FROM reporting.v_payment_attribution" in text:
            # 健康替身：total, closed, gift, no_product, other 全零 = 完全归属。
            return _FakeResult([(0, 0, 0, 0, 0)])
        if text.startswith("SELECT count(*) FILTER (WHERE commercial_id IS NULL"):
            # 退款归属诊断：(未匹配条数, canonical 成功总数, 未匹配金额)。
            # 按语句前缀认，不按视图名认：cohort 的 CTE 也读 v_refunds/v_payments。
            return _FakeResult([self.refund_gap])
        if text.startswith("SELECT count(*), count(*) FILTER (WHERE amount IS NULL)"):
            # 未认证支付诊断：(总笔数, 金额未定, 有原始金额, 已知金额合计)。
            return _FakeResult([self.unverified_payments])
        if text.startswith("WITH cohort"):
            return _FakeResult([self.cohort])
        if "FROM reporting.v_product_daily" in text:
            rows = self._select(self.product_rows, params)
            # _PRODUCT_SQL：日行先按 (店铺, 商品, line_kind) 在 SQL 端聚合掉，LIMIT 只约束
            # 最终分组数。列序与生产 SQL 逐一对应，替身不得比生产少聚合一层。
            if "GROUP BY shop_id, product_id, line_kind" in text:
                rows = self._aggregate_products(rows)
            return _FakeResult(rows[:params[-1]])
        if "FROM reporting.v_shop_daily" in text:
            rows = self._select(self.daily_rows, params)
            if "SELECT DISTINCT shop_id, day" in text:
                return _FakeResult([(len({(row[0], row[1]) for row in rows}),)])
            if "GROUP BY shop_id" in text:      # _AGG_SQL：日行在SQL端聚合掉
                aggregated: dict[str, list] = {}
                for row in rows:
                    entry = aggregated.setdefault(
                        row[0], [Decimal(0), 0, 0, Decimal(0), Decimal(0)])
                    entry[0] += row[2]
                    entry[1] += row[3]
                    entry[2] += row[4]
                    entry[3] += row[5]
                    entry[4] += row[6]
                rows = [(shop_id, *values)
                        for shop_id, values in sorted(aggregated.items())]
            else:
                rows = sorted(rows, key=lambda row: (row[1], row[0]))  # ORDER BY day
            return _FakeResult(rows[:params[-1]])
        raise AssertionError(f"未预期的SQL：{text}")

    @staticmethod
    def _select(rows, params):
        shop_ids, start, end = params[0], params[1], params[2]
        return [row for row in rows
                if row[0] in shop_ids and start <= row[1] < end]

    @staticmethod
    def _aggregate_products(rows):
        """回放 _PRODUCT_SQL 的分组/求和/数组聚合语义（名称与规格按 day 排序）。"""
        groups: dict[tuple, dict] = {}
        for row in rows:
            entry = groups.setdefault((row[0], row[2], row[7]), {
                "quantity": Decimal(0), "gift_quantity": Decimal(0),
                "product_paid_amount": Decimal(0), "allocation_verified": True,
                "names": [], "snapshots": [], "sku_labels": []})
            entry["quantity"] += row[3]
            entry["gift_quantity"] += row[4]
            entry["product_paid_amount"] += row[5]
            entry["allocation_verified"] = bool(
                entry["allocation_verified"] and row[6])
            if row[8] is not None:
                entry["names"].append((row[1], str(row[8])))
            if row[9] is not None:
                entry["snapshots"].append((row[1], str(row[9])))
            entry["sku_labels"].append((row[1], row[10]))
        output = []
        for (shop_id, product_id, line_kind), entry in sorted(groups.items()):
            names = [value for _, value in sorted(entry["names"], key=lambda item: item[0])]
            snapshots = [value for _, value
                         in sorted(entry["snapshots"], key=lambda item: item[0])]
            sku_labels = [value for _, value
                          in sorted(entry["sku_labels"], key=lambda item: item[0])]
            name_day = min((day for day, _ in entry["names"]), default=None)
            snapshot_day = min((day for day, _ in entry["snapshots"]), default=None)
            output.append((shop_id, product_id, line_kind, entry["quantity"],
                           entry["gift_quantity"], entry["product_paid_amount"],
                           entry["allocation_verified"], name_day, names or None,
                           snapshot_day, snapshots or None, sku_labels))
        return output


class StepClock:
    """每次读表推进1秒：把“第几次预算检查”变成可直接指定的整数。"""

    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        self.now += 1.0
        return self.now


class MetricBudgetTests(unittest.TestCase):
    """C-4：时间预算耗尽不能冒充“确定没有数据”。"""

    TZ = ZoneInfo("Asia/Shanghai")
    START = date(2026, 9, 1)
    END = date(2026, 9, 8)
    DATA_AS_OF = datetime(2026, 9, 8, 0, 0, tzinfo=TZ)

    def _warehouse(self):
        return FakeWarehouse(
            daily_rows=[("S1", self.START + timedelta(days=day), Decimal("100"), 1, 1,
                         Decimal("0"), Decimal("100")) for day in range(7)],
            product_rows=[("S1", self.START, "P1", Decimal("2"), Decimal("0"),
                           Decimal("200"), True, "sale") for _ in range(3)],
            shops=[("S1", True, "CNY")],
            shop_profiles=[("S1", "fxg", "档案店S1")],
            data_as_of=self.DATA_AS_OF,
            cohort=(Decimal("700"), Decimal("70")),
        )

    def _query(self, *, failing_check: int | None = None, group_by: str = "total",
               metrics=("paid_amount", "aov"), warehouse=None):
        """预算在第 failing_check+1 次检查处耗尽（None=预算充足）。

        StepClock 每次读表推 1 秒，deadline=failing_check+0.5 就恰好卡在
        第 failing_check 次检查之后；检查顺序：1入口 → 2覆盖 → 3取行 → 4同批退款率。
        """
        import time as time_module

        from bi_agent import metrics as metrics_module

        request = metrics_module.QueryRequest(
            start=self.START, end=self.END, shop_ids=["S1"],
            metrics=list(metrics), group_by=group_by)
        warehouse = self._warehouse() if warehouse is None else warehouse
        if failing_check is None:
            return metrics_module.query_business(
                warehouse, request, allowed_shop_ids=frozenset({"S1"}),
                now=self.DATA_AS_OF, deadline=time_module.monotonic() + 30)
        with patch.object(metrics_module, "time", StepClock()):
            return metrics_module.query_business(
                warehouse, request, allowed_shop_ids=frozenset({"S1"}),
                now=self.DATA_AS_OF, deadline=float(failing_check) + 0.5)

    def test_generous_budget_still_returns_the_rows(self):
        """正向控制：同一桩数据在预算充足时仍应 return ok。"""
        result = self._query()

        self.assertEqual(result.status, "ok", result.limitations)
        self.assertEqual(Decimal(result.data[0]["paid_amount"]), Decimal("700"))

    def test_budget_exhausted_at_period_fetch_is_unavailable(self):
        # 共 4 次检查：入口→覆盖→行数预检→取行；旧代码在第 4 次 return [] 并照发 ok
        result = self._query(failing_check=3)

        self.assertEqual(result.status, "unavailable")
        self.assertNotEqual(result.status, "ok")
        self.assertIn("本次查询时间预算已耗尽", result.limitations)
        self.assertEqual(result.data, [])

    def test_budget_exhausted_at_product_fetch_is_unavailable(self):
        result = self._query(failing_check=3, group_by="product",
                             metrics=("product_paid_amount", "quantity"))

        self.assertEqual(result.status, "unavailable")
        self.assertEqual(result.data, [])
        self.assertIn("本次查询时间预算已耗尽", result.limitations)

    def test_budget_exhausted_at_cohort_query_is_unavailable(self):
        # 带同批退款率时多一次检查（第 5 次），旧代码同样 return []
        result = self._query(failing_check=4,
                             metrics=("paid_amount", "cohort_refund_rate"))

        self.assertEqual(result.status, "unavailable")
        self.assertEqual(result.data, [])

    def test_no_budget_left_at_any_stage_never_reports_ok(self):
        """任意一个预算检查点耗尽，都只能得到 unavailable。"""
        for group_by, metrics in (("total", ("paid_amount", "aov")),
                                  ("shop", ("paid_amount",)),
                                  ("day", ("paid_amount",)),
                                  ("total", ("paid_amount", "cohort_refund_rate")),
                                  ("product", ("quantity",))):
            warehouse = self._warehouse()
            control = self._query(group_by=group_by, metrics=metrics,
                                  warehouse=warehouse)
            self.assertEqual(control.status, "ok", control.limitations)
            checks = sum(1 for sql, _ in warehouse.statements
                         if "statement_timeout" in sql)
            self.assertGreaterEqual(checks, 3)   # 至少：入口、覆盖、取行
            for failing_check in range(checks):
                with self.subTest(group_by=group_by, metrics=metrics,
                                  failing_check=failing_check):
                    result = self._query(failing_check=failing_check,
                                         group_by=group_by, metrics=metrics)
                    self.assertNotEqual(result.status, "ok")
                    self.assertEqual(result.status, "unavailable", result.limitations)
                    self.assertEqual(result.data, [])
                    self.assertIn("本次查询时间预算已耗尽", result.limitations)


class MetricRowCapTests(unittest.TestCase):
    """C-3：MAX_ROWS 截断不得压低汇总额后冒充 ok。"""

    TZ = ZoneInfo("Asia/Shanghai")
    SHOPS = ("S1", "S2", "S3")
    START = date(2025, 9, 1)
    DAYS = 366
    DATA_AS_OF = datetime(2026, 9, 8, 0, 0, tzinfo=TZ)

    def _warehouse(self, *, shops=SHOPS, days=DAYS, products=("P1", "P2")):
        from datetime import date as _date

        daily_rows = []
        for offset in range(days):
            day = _date.fromordinal(self.START.toordinal() + offset)
            for shop_id in shops:
                # (shop, day, paid_amount, paid_orders, erp_documents, refund, cash_diff)
                daily_rows.append((shop_id, day, Decimal("100"), 1, 1, Decimal("0"),
                                   Decimal("100")))
        product_rows = []
        for offset in range(days):
            day = _date.fromordinal(self.START.toordinal() + offset)
            for product_id in products:
                product_rows.append((shops[0], day, product_id, Decimal("1"),
                                     Decimal("0"), Decimal("100"), True, "sale"))
        return FakeWarehouse(daily_rows=daily_rows, product_rows=product_rows,
                             shops=[(shop_id, True, "CNY") for shop_id in shops],
                             shop_profiles=[(shop_id, "fxg", f"档案店{shop_id}")
                                            for shop_id in shops],
                             data_as_of=self.DATA_AS_OF)

    def _query(self, warehouse, *, group_by: str, metrics, shop_ids=None, top_n=10):
        import time as time_module

        from bi_agent import metrics as metrics_module

        if shop_ids is None:
            shop_ids = sorted({row[0] for row in warehouse.daily_rows})
        request = metrics_module.QueryRequest(
            start=self.START, end=self.START + timedelta(days=self.DAYS),
            shop_ids=list(shop_ids), metrics=list(metrics), group_by=group_by,
            top_n=top_n)
        return metrics_module.query_business(
            warehouse, request, allowed_shop_ids=frozenset(shop_ids),
            now=self.DATA_AS_OF, deadline=time_module.monotonic() + 30)

    def test_total_grouping_aggregates_in_sql_and_keeps_the_full_sum(self):
        from bi_agent.metrics import MAX_ROWS

        warehouse = self._warehouse()
        result = self._query(warehouse, group_by="total",
                             metrics=["paid_amount", "paid_orders", "aov"])

        # 1098 个（店，日）组：旧代码只拿得到 ORDER BY day 的前 500 行，
        # 会报出 50000 并标 status=ok；现在必须是完整的 109800。
        self.assertEqual(result.status, "ok", result.limitations)
        row = result.data[0]
        self.assertEqual(Decimal(row["paid_amount"]),
                         Decimal(100) * len(self.SHOPS) * self.DAYS)
        self.assertNotEqual(Decimal(row["paid_amount"]), Decimal(100) * MAX_ROWS)
        self.assertEqual(row["paid_orders"], len(self.SHOPS) * self.DAYS)
        self.assertEqual(Decimal(row["aov"]), Decimal("100"))
        fetches = [sql for sql, _ in warehouse.fetches("v_shop_daily")]
        self.assertTrue(any("GROUP BY shop_id" in sql for sql in fetches))
        self.assertFalse(any("ORDER BY day, shop_id" in sql for sql in fetches))

    def test_shop_grouping_reports_every_shop_day(self):
        warehouse = self._warehouse()
        result = self._query(warehouse, group_by="shop", metrics=["paid_amount"])

        self.assertEqual(result.status, "ok", result.limitations)
        self.assertEqual([row["shop_id"] for row in result.data], list(self.SHOPS))
        for row in result.data:
            self.assertEqual(Decimal(row["paid_amount"]), Decimal(100) * self.DAYS)

    def test_day_grouping_is_refused_before_any_truncation(self):
        warehouse = self._warehouse()
        result = self._query(warehouse, group_by="day", metrics=["paid_amount"])

        self.assertEqual(result.status, "invalid_parameters")
        self.assertEqual(result.data, [])

    def test_product_groups_over_the_cap_are_unavailable_not_a_partial_top_n(self):
        """最终分组 >500 时仍必须 fail-closed：LIMIT 探测不能冒充 ok 的截断排名。"""
        from bi_agent.metrics import MAX_ROWS

        rows = [("S1", self.START, f"P{index:04d}", Decimal("1"), Decimal("0"),
                 Decimal("10"), True, "sale") for index in range(MAX_ROWS + 1)]
        warehouse = FakeWarehouse(product_rows=rows, shops=[("S1", True, "CNY")],
                                  shop_profiles=[("S1", "fxg", "档案店S1")],
                                  data_as_of=self.DATA_AS_OF)
        result = self._query(warehouse, group_by="product",
                             metrics=["product_paid_amount"], shop_ids=["S1"])

        self.assertEqual(result.status, "unavailable")
        self.assertEqual(result.data, [])
        self.assertTrue(any("上限" in item for item in result.limitations),
                        result.limitations)

    def test_product_rows_aggregate_daily_rows_in_sql_before_the_cap(self):
        """>500 行日行但最终分组 <500：必须先在 SQL 端聚合再排名，不得误报超限。"""
        from bi_agent.metrics import MAX_ROWS

        amounts = {"P1": Decimal("2"), "P2": Decimal("3"), "P3": Decimal("5")}
        rows = []
        for offset in range(self.DAYS):
            day = date.fromordinal(self.START.toordinal() + offset)
            for product_id, amount in amounts.items():
                rows.append(("S1", day, product_id, Decimal("1"), Decimal("0"),
                             amount, True, "sale"))
        warehouse = FakeWarehouse(product_rows=rows, shops=[("S1", True, "CNY")],
                                  shop_profiles=[("S1", "fxg", "档案店S1")],
                                  data_as_of=self.DATA_AS_OF)
        self.assertGreater(len(warehouse.product_rows), MAX_ROWS)

        result = self._query(warehouse, group_by="product", shop_ids=["S1"],
                             metrics=["product_paid_amount"], top_n=2)

        self.assertEqual(result.status, "ok", result.limitations)
        ranked = [row for row in result.data if "product_id" in row]
        self.assertEqual([row["product_id"] for row in ranked], ["P3", "P2"])
        self.assertEqual([Decimal(str(row["product_paid_amount"])) for row in ranked],
                         [amounts["P3"] * self.DAYS, amounts["P2"] * self.DAYS])
        self.assertEqual([row for row in result.data if "notice" in row],
                         [{"notice": "仅返回Top 2，共3个商品"}])
        # 反恒真：SQL 必须自己聚合日行，而不是把 1098 行日行 LIMIT 掉后靠 Python 拼。
        statements = [statement for statement, _ in warehouse.fetches("v_product_daily")]
        self.assertTrue(any("GROUP BY shop_id, product_id, line_kind" in statement
                            for statement in statements), statements)
        self.assertFalse(any("ORDER BY day, shop_id, product_id" in statement
                             for statement in statements), statements)

    def test_more_shops_than_the_cap_is_unavailable_not_a_partial_sum(self):
        shop_ids = tuple(f"S{index}" for index in range(501))
        warehouse = self._warehouse(shops=shop_ids, days=1)
        result = self._query(warehouse, group_by="total", metrics=["paid_amount"],
                            shop_ids=list(shop_ids))

        self.assertEqual(result.status, "unavailable")
        self.assertEqual(result.data, [])

    def test_fetch_probe_asks_for_one_row_beyond_the_cap(self):
        from bi_agent.metrics import MAX_ROWS

        warehouse = self._warehouse(shops=("S1",), days=3)
        result = self._query(warehouse, group_by="total", metrics=["paid_amount"])

        self.assertEqual(result.status, "ok", result.limitations)
        limits = [params[-1] for sql, params in warehouse.fetches("v_shop_daily")
                  if "GROUP BY shop_id" in sql]
        self.assertEqual(limits, [MAX_ROWS + 1])


class PartitionedProductRankingTests(unittest.TestCase):
    """多店商品排行按完整指标签名分区：各出各自 Top-N，缺证据的分区只排除自己。

    这批用例钉住的是“一次查询、服务端分组”，不是“让模型多试几次”：
    认证组正常出榜、未认证组只能当可观测样本、实测不成立的口径不出一行，
    且任何跨分区合计/排名/静默截断都不会出现。
    """

    TZ = ZoneInfo("Asia/Shanghai")
    START = date(2026, 9, 1)
    END = date(2026, 9, 8)
    DATA_AS_OF = datetime(2026, 9, 8, 0, 0, tzinfo=TZ)
    PRODUCT_CAPS = ("product_paid_amount", "quantity")

    def _shop(self, shop_id, platform, capabilities=PRODUCT_CAPS):
        # v_shops 五列：店、开关、币种、平台、能力标签。
        return (shop_id, True, "CNY", platform, list(capabilities))

    def _products(self, shop_id, amounts, *, day=None):
        return [(shop_id, day or self.START, product_id, Decimal("1"), Decimal("0"),
                 Decimal(amount), True, "sale")
                for product_id, amount in amounts.items()]

    def _warehouse(self, shops, product_rows, *, unverified_payments=(0, 0, 0, 0)):
        return FakeWarehouse(
            product_rows=list(product_rows), shops=list(shops),
            shop_profiles=[(row[0], row[3], f"档案店{row[0]}") for row in shops],
            data_as_of=self.DATA_AS_OF, unverified_payments=unverified_payments)

    def _query(self, warehouse, shop_ids, *, metrics=("product_paid_amount",),
               basis_policy="partitioned", top_n=10):
        import time as time_module

        from bi_agent import metrics as metrics_module

        request = metrics_module.QueryRequest(
            start=self.START, end=self.END, shop_ids=list(shop_ids),
            metrics=list(metrics), group_by="product", basis_policy=basis_policy,
            top_n=top_n)
        return metrics_module.query_business(
            warehouse, request, allowed_shop_ids=frozenset(shop_ids),
            now=self.DATA_AS_OF, deadline=time_module.monotonic() + 30)

    @staticmethod
    def _rows_by_group(result):
        grouped: dict[str, list[dict]] = {}
        for row in result.data:
            if "product_id" in row:
                grouped.setdefault(str(row["rank_group"]), []).append(row)
        return grouped

    def test_partitioned_ranking_splits_certified_and_unmeasured_cohorts(self):
        warehouse = self._warehouse(
            [self._shop("FX1", "fxg"), self._shop("JD1", "jd")],
            self._products("FX1", {"P1": "300", "P2": "200"})
            + self._products("JD1", {"P9": "500"}))

        result = self._query(warehouse, ["FX1", "JD1"])

        self.assertEqual(result.status, "ok", result.limitations)
        self.assertEqual([group["group"] for group in result.rank_groups], ["g1", "g2"])
        self.assertEqual([group["status"] for group in result.rank_groups],
                         ["certified", "observable_sample"])
        self.assertEqual([group["shop_ids"] for group in result.rank_groups],
                         [["FX1"], ["JD1"]])
        self.assertEqual(result.rank_exclusions, [])
        self.assertEqual(result.coverage.status, "complete")
        self.assertIn("1 家店铺的结果来自未认证付款时间口径，按可观测样本披露",
                      result.limitations)
        self.assertIn("本次结果按口径分为 2 个分区，分区之间不得汇总、比较或排名",
                      result.limitations)
        self.assertEqual({group: [row["rank"] for row in rows]
                          for group, rows in self._rows_by_group(result).items()},
                         {"g1": [1, 2], "g2": [1]})

    def test_partitioned_ranking_discloses_unverified_payments_like_strict(self):
        """分区路径对同一指标必须照搬 strict 的未认证支付披露，不能因多店而吞掉。"""
        from bi_agent.business_query.nodes import _limitation_codes

        warehouse = self._warehouse(
            [self._shop("FX1", "fxg"), self._shop("JD1", "jd")],
            self._products("FX1", {"P1": "300"})
            + self._products("JD1", {"P9": "500"}),
            unverified_payments=(3, 1, 2, Decimal("500")))

        result = self._query(warehouse, ["FX1", "JD1"])

        self.assertEqual(result.status, "ok", result.limitations)
        self.assertIn("未认证支付3笔（金额未定1笔），已知原始金额500元（2笔）",
                      result.limitations)
        self.assertIn("unverified_payments", _limitation_codes(result.limitations))

        # 正向控制：诊断为零时不得凭空多出这句话。
        clean = self._warehouse(
            [self._shop("FX1", "fxg"), self._shop("JD1", "jd")],
            self._products("FX1", {"P1": "300"})
            + self._products("JD1", {"P9": "500"}))
        clean_result = self._query(clean, ["FX1", "JD1"])
        self.assertFalse(any(item.startswith("未认证支付")
                             for item in clean_result.limitations))

    def test_disproved_cohort_publishes_no_rows_and_is_excluded(self):
        """实测不成立的付款时间口径：一行都不出，且原因是口径未认证而不是“指标冲突”。"""
        warehouse = self._warehouse(
            [self._shop("FX1", "fxg"), self._shop("TB1", "tb")],
            self._products("FX1", {"P1": "300"})
            + self._products("TB1", {"P9": "999"}))

        result = self._query(warehouse, ["FX1", "TB1"])

        self.assertEqual(result.status, "ok", result.limitations)
        self.assertEqual([row["shop_id"] for row in result.data], ["FX1"])
        self.assertEqual([row["rank"] for row in result.data], [1])
        self.assertEqual(result.rank_exclusions,
                         [{"shop_id": "TB1",
                           "reason": "coverage_time_basis_unverified"}])
        self.assertIn("1 家店铺的付款时间口径未经认证（实测不成立），未列入本次排行",
                      result.limitations)
        self.assertEqual(result.coverage.status, "partial")
        # 反恒真：不得把口径缺口说成“指标互不兼容”，也不得建议换个指标再试。
        self.assertFalse(any("互不兼容" in item for item in result.limitations))
        self.assertFalse(any("quantity" in item for item in result.limitations))

        # 公开投影只带引用与分区陈述：真实店铺主键不得经新字段泄出去。
        from bi_agent.business_query.tool import to_model_result
        from bi_agent.catalog import build_catalog
        from tests.fakeconn import ShopCatalogConn

        catalog = build_catalog(
            ShopCatalogConn([("FX1", "店铺A"), ("TB1", "店铺B")]), result,
            allowed_shop_ids=frozenset({"FX1", "TB1"}))
        model_payload = to_model_result(result, catalog)
        rendered = json.dumps(model_payload, ensure_ascii=False)
        self.assertNotIn("FX1", rendered)
        self.assertNotIn("TB1", rendered)
        self.assertEqual(model_payload["excluded_scope"],
                         [{"shop_ref": catalog.shop_ref("TB1"),
                           "reason": "coverage_time_basis_unverified"}])
        self.assertEqual([group["shop_refs"] for group in model_payload["rank_groups"]],
                         [[catalog.shop_ref("FX1")]])

    def test_unsupported_cohort_is_excluded_not_a_whole_request_refusal(self):
        """能力/来源缺口只缩到分区粒度：一家店答不了不得打死整次排行。"""
        warehouse = self._warehouse(
            [self._shop("FX1", "fxg"), self._shop("PD1", "pdd")],
            self._products("FX1", {"P1": "300"}))

        result = self._query(warehouse, ["FX1", "PD1"])

        self.assertEqual(result.status, "ok", result.limitations)
        self.assertEqual([row["shop_id"] for row in result.data], ["FX1"])
        self.assertEqual(result.rank_exclusions,
                         [{"shop_id": "PD1", "reason": "capability_unavailable"}])
        self.assertIn("1 家店铺缺少 product_paid_amount 的已核验能力，未列入本次排行",
                      result.limitations)
        self.assertEqual(result.coverage.status, "partial")

    def test_cohort_over_the_final_group_cap_is_excluded_while_others_publish(self):
        """分区内最终分组 >500：该分区拒输出数，其他分区照常发布，绝不静默截断。"""
        from bi_agent.metrics import MAX_ROWS

        warehouse = self._warehouse(
            [self._shop("FX1", "fxg"), self._shop("JD1", "jd")],
            self._products("FX1", {f"P{index:04d}": "10"
                                    for index in range(MAX_ROWS + 1)})
            + self._products("JD1", {"P9": "500"}))

        result = self._query(warehouse, ["FX1", "JD1"])

        self.assertEqual(result.status, "ok", result.limitations)
        self.assertEqual(result.rank_exclusions,
                         [{"shop_id": "FX1", "reason": "result_too_large"}])
        self.assertIn("1 家店铺的商品分组数超过 500 上限，未列入本次排行",
                      result.limitations)
        self.assertEqual([row["shop_id"] for row in self.data_rows(result)], ["JD1"])

    @staticmethod
    def data_rows(result):
        return [row for row in result.data if "product_id" in row]

    def test_composite_output_cap_refuses_without_publishing_a_prefix(self):
        """组合输出上限：超限时一行都不发（发布前几个分区就是静默截断）。"""
        wide = {f"P{index:03d}": "10" for index in range(300)}
        warehouse = self._warehouse(
            [self._shop("FX1", "fxg"), self._shop("JD1", "jd")],
            self._products("FX1", wide) + self._products("JD1", wide))

        refused = self._query(warehouse, ["FX1", "JD1"], top_n=300)

        self.assertEqual(refused.status, "invalid_parameters", refused.limitations)
        self.assertEqual(refused.data, [])
        self.assertEqual(refused.rank_groups, [])
        self.assertIn("分区结果共需 600 行，超过 500 行上限；请缩小 top_n 后重试",
                      refused.limitations)

        # 正向控制：把 top_n 缩到上限以内，两个分区都能发布（各带一条截断提醒行）。
        control = self._query(warehouse, ["FX1", "JD1"], top_n=200)
        self.assertEqual(control.status, "ok", control.limitations)
        self.assertEqual([group["truncated"] for group in control.rank_groups],
                         [True, True])
        self.assertEqual(len(control.data), 402)

    def test_partitioned_ranking_never_merges_ranks_across_cohorts(self):
        """反恒真：名次只在分区内计数；两个分区不能共享一个全局计数器。"""
        warehouse = self._warehouse(
            [self._shop("FX1", "fxg"), self._shop("JD1", "jd")],
            self._products("FX1", {"P1": "300", "P2": "200"})
            + self._products("JD1", {"P9": "500", "P10": "100"}))

        grouped = self._rows_by_group(self._query(warehouse, ["FX1", "JD1"]))

        self.assertEqual(sorted(grouped), ["g1", "g2"])
        self.assertEqual([row["rank"] for row in grouped["g1"]], [1, 2])
        self.assertEqual([row["rank"] for row in grouped["g2"]], [1, 2])
        # 未认证分区的第一名金额高于认证分区的第二名：全局计数器会把它记成 rank 3。
        self.assertLess(Decimal(str(grouped["g1"][1]["product_paid_amount"])),
                        Decimal(str(grouped["g2"][0]["product_paid_amount"])))

    def test_strict_multi_basis_product_ranking_still_refuses_and_names_the_policy(self):
        """显式 strict 行为不变，只是补一句告诉我们怎么改成分区排行。"""
        from bi_agent.business_query.nodes import _limitation_codes

        warehouse = self._warehouse(
            [self._shop("FX1", "fxg"), self._shop("TB1", "tb")],
            self._products("FX1", {"P1": "300"})
            + self._products("TB1", {"P9": "999"}))

        result = self._query(warehouse, ["FX1", "TB1"], basis_policy="strict")

        self.assertEqual(result.status, "invalid_parameters")
        self.assertEqual(result.data, [])
        self.assertEqual(_limitation_codes(result.limitations),
                         ["basis_incompatible"])
        self.assertTrue(any("basis_policy=partitioned" in item
                            for item in result.limitations), result.limitations)

    def test_product_metric_signatures_stay_within_the_partition_bound(self):
        """分区数由注册表决定而不是由店铺数决定：这条不变量要能被钉住。"""
        from bi_agent.metrics import MAX_RANK_GROUPS, PRODUCT_METRICS
        from bi_agent.sources import (METRIC_CAPABILITIES, ShopRecord, _REGISTRATIONS,
                                      binding_signature, resolve_metric_dependencies)

        signatures = set()
        for platform in _REGISTRATIONS:
            record = ShopRecord(shop_id="S1", platform=platform,
                                capabilities=frozenset(METRIC_CAPABILITIES))
            signatures.add(tuple(sorted(
                (metric, binding_signature(resolve_metric_dependencies(record, metric)))
                for metric in sorted(PRODUCT_METRICS))))

        self.assertLessEqual(len(signatures), MAX_RANK_GROUPS)

    def test_partitioned_queries_are_bounded_by_cohorts_not_shops(self):
        """查询次数上界是分区数：不得按店铺或商品逐项发查询（N+1）。"""
        warehouse = self._warehouse(
            [self._shop("FX1", "fxg"), self._shop("FX2", "fxg"),
             self._shop("JD1", "jd")],
            self._products("FX1", {"P1": "300"})
            + self._products("FX2", {"P2": "200"})
            + self._products("JD1", {"P9": "500"}))

        result = self._query(warehouse, ["FX1", "FX2", "JD1"])

        self.assertEqual(result.status, "ok", result.limitations)
        fetches = warehouse.fetches("v_product_daily")
        # 商品分组查询（而不是归属披露等读同一视图的其他语句）每分区恰好一次。
        product_fetches = [(sql, params) for sql, params in fetches
                           if "GROUP BY shop_id, product_id, line_kind" in sql]
        self.assertEqual(len(product_fetches), len(result.rank_groups))
        self.assertEqual({tuple(params[0]) for _sql, params in product_fetches},
                         {("FX1", "FX2"), ("JD1",)})

    def test_partitioned_ranking_with_empty_published_cohorts_is_partial(self):
        """已评估但确实没有商品也是结果：它不能被读成契约违规。"""
        from bi_agent.business_query.nodes import _classify
        from bi_agent.runtime.models import DomainStatus

        warehouse = self._warehouse(
            [self._shop("FX1", "fxg"), self._shop("TB1", "tb")], [])

        result = self._query(warehouse, ["FX1", "TB1"])

        self.assertEqual(result.status, "ok", result.limitations)
        self.assertEqual(result.data, [])
        self.assertEqual(result.coverage.status, "partial")
        self.assertEqual(result.rank_groups[0]["groups_total"], 0)
        target_status, error = _classify(result)
        self.assertEqual(target_status, DomainStatus.PARTIAL)
        self.assertIsNone(error)


class ModelTests(unittest.TestCase):
    PROVIDER_RESPONSE = {
        "choices": [{"message": {
            "role": "assistant", "content": None,
            "reasoning_content": "synthetic-private-context",
            "tool_calls": [{"id": "call_1", "type": "function", "function": {
                "name": "query_business", "arguments": '{"start":"2026-09-01"}'}}]
        }}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    }

    def _model(self, provider: str, handler):
        from bi_agent.llm import CompatibleChatModel

        return CompatibleChatModel(_model_settings(provider),
                                   transport=httpx.MockTransport(handler))

    def test_provider_round_trip_for_both(self):
        from bi_agent.llm import Message

        def provider_response(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=self.PROVIDER_RESPONSE)

        for provider in ("qwen", "deepseek"):
            with self.subTest(provider=provider):
                model = self._model(provider, provider_response)
                reply = model.complete([Message(role="user", content="查看经营")],
                                       [], timeout_s=2)
                self.assertEqual(reply.tool_calls[0].id, "call_1")
                self.assertEqual(reply.tool_calls[0].arguments, {"start": "2026-09-01"})
                self.assertNotIn("synthetic-private-context", repr(reply.as_message()))
                self.assertNotIn("synthetic-private-context", repr(reply))

    def test_second_request_preserves_reasoning_and_tool_id(self):
        from bi_agent.llm import Message, ModelReply, ToolCall

        requests: list[dict] = []

        def provider_response(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            requests.append(body)
            if len(requests) == 1:
                return httpx.Response(200, json=self.PROVIDER_RESPONSE)
            return httpx.Response(200, json={
                "choices": [{"message": {"role": "assistant",
                                          "content": "已查询"}}],
            })

        model = self._model("deepseek", provider_response)
        first = model.complete([Message(role="user", content="查看经营")],
                               [], timeout_s=2)
        tool_result = Message(role="tool", tool_call_id="call_1", content="{}")
        second = model.complete(
            [Message(role="user", content="查看经营"), first.as_message(), tool_result],
            [], timeout_s=2)
        self.assertEqual(second.tool_calls, [])
        assistant_encoded = requests[1]["messages"][1]
        self.assertEqual(assistant_encoded["reasoning_content"],
                         "synthetic-private-context")
        self.assertEqual(assistant_encoded["tool_calls"][0]["id"], "call_1")
        self.assertEqual(assistant_encoded["tool_calls"][0]["function"]["arguments"],
                         '{"start":"2026-09-01"}')
        tool_encoded = requests[1]["messages"][2]
        self.assertEqual(tool_encoded["tool_call_id"], "call_1")

    def test_invalid_json_arguments_preserve_id(self):
        from bi_agent.llm import Message

        def provider_response(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={
                "choices": [{"message": {"role": "assistant", "content": None,
                                          "tool_calls": [{"id": "call_9",
                                                           "type": "function",
                                                           "function": {
                                                               "name": "query_business",
                                                               "arguments": '{bad json'}}]}}],
            })

        model = self._model("qwen", provider_response)
        reply = model.complete([Message(role="user", content="q")], [], timeout_s=2)
        self.assertIsNone(reply.tool_calls[0].arguments)
        self.assertEqual(reply.tool_calls[0].arguments_error, "invalid_json")
        self.assertEqual(reply.tool_calls[0].id, "call_9")

    def test_usage_missing_and_partial(self):
        from bi_agent.llm import Message

        def missing_usage(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={
                "choices": [{"message": {"role": "assistant", "content": "ok"}}]})

        model = self._model("qwen", missing_usage)
        reply = model.complete([Message(role="user", content="q")], [], timeout_s=2)
        self.assertIsNone(reply.usage)

        def partial_usage(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={
                "choices": [{"message": {"role": "assistant", "content": "ok"}}],
                "usage": {"prompt_tokens": 7}})

        model = self._model("qwen", partial_usage)
        reply = model.complete([Message(role="user", content="q")], [], timeout_s=2)
        self.assertEqual(reply.usage, {"prompt_tokens": 7, "completion_tokens": None,
                                       "total_tokens": None})

    def test_error_mapping(self):
        from bi_agent.llm import Message, ModelError

        for status, code in ((401, "authentication"), (403, "authentication"),
                             (429, "rate_limit"), (500, "unavailable"), (503, "unavailable")):
            with self.subTest(status=status):
                model = self._model("deepseek",
                                    lambda request, s=status: httpx.Response(s))
                with self.assertRaises(ModelError) as ctx:
                    model.complete([Message(role="user", content="q")], [], timeout_s=2)
                self.assertEqual(ctx.exception.code, code)

    def test_timeout_budget_exhausted(self):
        import asyncio as asyncio_module

        from bi_agent.llm import Message, ModelError

        async def slow(request: httpx.Request) -> httpx.Response:
            await asyncio_module.sleep(1)
            return httpx.Response(200, json={
                "choices": [{"message": {"role": "assistant", "content": "ok"}}]})

        model = self._model("qwen", slow)
        with self.assertRaises(ModelError) as ctx:
            model.complete([Message(role="user", content="q")], [], timeout_s=0.05)
        self.assertEqual(ctx.exception.code, "timeout")

    def test_duplicate_and_missing_tool_ids_rejected(self):
        from bi_agent.llm import Message, ModelError

        duplicate = {
            "choices": [{"message": {"role": "assistant", "content": None,
                                      "tool_calls": [
                                          {"id": "c1", "type": "function",
                                           "function": {"name": "a", "arguments": "{}"}},
                                          {"id": "c1", "type": "function",
                                           "function": {"name": "b", "arguments": "{}"}}]}}]}
        missing = {
            "choices": [{"message": {"role": "assistant", "content": None,
                                      "tool_calls": [
                                          {"type": "function",
                                           "function": {"name": "a", "arguments": "{}"}}]}}]}
        for payload in (duplicate, missing):
            with self.subTest():
                model = self._model("qwen",
                                    lambda request, p=payload: httpx.Response(200, json=p))
                with self.assertRaises(ModelError) as ctx:
                    model.complete([Message(role="user", content="q")], [], timeout_s=2)
                self.assertEqual(ctx.exception.code, "invalid_response")

    def test_tools_and_endpoint_passthrough(self):
        from bi_agent.llm import DEFAULT_BASE_URLS, Message

        bodies: list[dict] = []

        def provider_response(request: httpx.Request) -> httpx.Response:
            bodies.append({"url": str(request.url),
                           "body": json.loads(request.content)})
            return httpx.Response(200, json={
                "choices": [{"message": {"role": "assistant", "content": "ok"}}]})

        tools = [{"type": "function", "function": {"name": "query_business",
                                                    "parameters": {"type": "object"}}}]
        model = self._model("qwen", provider_response)
        model.complete([Message(role="user", content="q")], tools, timeout_s=2)
        self.assertEqual(bodies[0]["url"],
                         DEFAULT_BASE_URLS["qwen"] + "/chat/completions")
        self.assertEqual(bodies[0]["body"]["tools"], tools)
        self.assertEqual(bodies[0]["body"]["model"], "demo-model")
        self.assertNotIn("response_format", bodies[0]["body"])

    def test_create_model_mapping(self):
        from bi_agent.llm import CompatibleChatModel, create_model

        for provider in ("qwen", "deepseek"):
            model = create_model(_model_settings(provider))
            self.assertIsInstance(model, CompatibleChatModel)


class PromotionTests(unittest.TestCase):
    NOW = datetime(2026, 9, 8, 9, tzinfo=ZoneInfo("Asia/Shanghai"))

    def test_cap_is_exact_and_missing_actual_is_not_zero(self):
        from bi_agent.metrics import ToolResult
        from bi_agent.promotion import PromotionRequest, evaluate_promotion

        request = PromotionRequest(mode="sales_cap", start="2026-10-01", end="2026-11-01",
                                   sales_estimate="100000", target_ratio="0.12")
        result = evaluate_promotion(request, confirmed_inputs={
            "sales_estimate": Decimal("100000"), "target_ratio": Decimal("0.12")},
            now=self.NOW)
        self.assertEqual(Decimal(result.data[0]["spend_cap"]), Decimal("12000"))
        actual = PromotionRequest(mode="actual_budget", start="2026-09-01", end="2026-10-01")
        outcome = evaluate_promotion(actual, confirmed_inputs={}, now=self.NOW)
        self.assertEqual(outcome.status, "missing_data")

    def test_budget_scenario_overrun(self):
        from bi_agent.promotion import PromotionRequest, evaluate_promotion

        request = PromotionRequest(mode="budget_scenario", start="2026-09-01",
                                   end="2026-09-08", budget="100", assumed_spend="120",
                                   spent_through="2026-09-06")
        result = evaluate_promotion(request, confirmed_inputs={
            "budget": Decimal("100"), "assumed_spend": Decimal("120")}, now=self.NOW)
        row = result.data[0]
        self.assertEqual(Decimal(row["remaining_budget"]), Decimal("0"))
        self.assertEqual(Decimal(row["overrun"]), Decimal("20"))
        self.assertEqual(row["remaining_days"], 2)
        self.assertEqual(Decimal(row["daily_allowance"]), Decimal("0"))
        self.assertEqual(row["basis"], "用户输入假设")

    def test_zero_budget(self):
        from bi_agent.promotion import PromotionRequest, evaluate_promotion

        request = PromotionRequest(mode="budget_scenario", start="2026-09-01",
                                   end="2026-09-08", budget="0", assumed_spend="0",
                                   spent_through="2026-09-06")
        result = evaluate_promotion(request, confirmed_inputs={
            "budget": Decimal("0"), "assumed_spend": Decimal("0")}, now=self.NOW)
        row = result.data[0]
        self.assertEqual(Decimal(row["remaining_budget"]), Decimal("0"))
        self.assertEqual(Decimal(row["daily_allowance"]), Decimal("0"))

    def test_period_ended_no_division_by_zero(self):
        from bi_agent.promotion import PromotionRequest, evaluate_promotion

        request = PromotionRequest(mode="budget_scenario", start="2026-09-01",
                                   end="2026-09-08", budget="100", assumed_spend="60",
                                   spent_through="2026-09-08")
        result = evaluate_promotion(request, confirmed_inputs={
            "budget": Decimal("100"), "assumed_spend": Decimal("60")}, now=self.NOW)
        row = result.data[0]
        self.assertIsNone(row["daily_allowance"])
        self.assertTrue(any("周期已结束" in item for item in result.limitations))

    def test_ratio_over_100_percent_rejected(self):
        from bi_agent.promotion import PromotionRequest

        with self.assertRaises(ValueError):
            PromotionRequest(mode="sales_cap", start="2026-10-01", end="2026-11-01",
                             sales_estimate="100000", target_ratio="1.2")

    def test_negative_and_nan_rejected(self):
        from bi_agent.promotion import PromotionRequest

        for kwargs in ({"sales_estimate": "-1", "target_ratio": "0.1"},
                       {"sales_estimate": "NaN", "target_ratio": "0.1"},
                       {"sales_estimate": "Infinity", "target_ratio": "0.1"}):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ValueError):
                    PromotionRequest(mode="sales_cap", start="2026-10-01",
                                     end="2026-11-01", **kwargs)

    def test_currency_restricted(self):
        from bi_agent.promotion import PromotionRequest

        with self.assertRaises(ValueError):
            PromotionRequest(mode="sales_cap", start="2026-10-01", end="2026-11-01",
                             sales_estimate="100000", target_ratio="0.1", currency="USD")

    def test_unconfirmed_amounts_rejected(self):
        from bi_agent.promotion import PromotionRequest, evaluate_promotion

        request = PromotionRequest(mode="sales_cap", start="2026-10-01", end="2026-11-01",
                                   sales_estimate="100000", target_ratio="0.12")
        result = evaluate_promotion(request, confirmed_inputs={}, now=self.NOW)
        self.assertEqual(result.status, "invalid_parameters")

    def test_mismatched_confirmed_inputs_rejected(self):
        from bi_agent.promotion import PromotionRequest, evaluate_promotion

        request = PromotionRequest(mode="budget_scenario", start="2026-09-01",
                                   end="2026-09-08", budget="100", assumed_spend="120",
                                   spent_through="2026-09-06")
        result = evaluate_promotion(request, confirmed_inputs={
            "budget": Decimal("999"), "assumed_spend": Decimal("120")}, now=self.NOW)
        self.assertEqual(result.status, "invalid_parameters")
        self.assertTrue(any("不一致" in item for item in result.limitations))

    def test_extra_mode_fields_rejected(self):
        from bi_agent.promotion import PromotionRequest

        with self.assertRaises(ValueError):
            PromotionRequest(mode="sales_cap", start="2026-10-01", end="2026-11-01",
                             sales_estimate="100000", target_ratio="0.1",
                             budget="500")

    def test_contribution_cap_missing_data(self):
        from bi_agent.promotion import PromotionRequest, evaluate_promotion

        request = PromotionRequest(mode="contribution_cap", start="2026-09-01",
                                   end="2026-10-01")
        result = evaluate_promotion(request, confirmed_inputs={}, now=self.NOW)
        self.assertEqual(result.status, "missing_data")
        self.assertTrue(any("推广实耗" in item for item in result.limitations))

    def test_span_limit(self):
        from bi_agent.promotion import PromotionRequest

        with self.assertRaises(ValueError):
            PromotionRequest(mode="sales_cap", start="2025-01-01", end="2026-09-08",
                             sales_estimate="1", target_ratio="0.1")

    # --- C-1 回归：投影契约以生产方为单一真源 --------------------------------
    @staticmethod
    def promotion_cases():
        """四种模式 × ok / 缺明确确认 / 确认不一致 / 周期已结束。"""
        return (
            ({"mode": "sales_cap", "start": "2026-10-01", "end": "2026-11-01",
              "sales_estimate": "100000", "target_ratio": "0.12"},
             {"sales_estimate": Decimal("100000"), "target_ratio": Decimal("0.12")}),
            ({"mode": "sales_cap", "start": "2026-10-01", "end": "2026-11-01",
              "sales_estimate": "100000", "target_ratio": "0.12"}, {}),
            ({"mode": "budget_scenario", "start": "2026-09-01", "end": "2026-09-08",
              "budget": "100", "assumed_spend": "120", "spent_through": "2026-09-06"},
             {"budget": Decimal("100"), "assumed_spend": Decimal("120")}),
            ({"mode": "budget_scenario", "start": "2026-09-01", "end": "2026-09-08",
              "budget": "100", "assumed_spend": "60", "spent_through": "2026-09-08"},
             {"budget": Decimal("100"), "assumed_spend": Decimal("60")}),
            ({"mode": "budget_scenario", "start": "2026-09-01", "end": "2026-09-08",
              "budget": "100", "assumed_spend": "60", "spent_through": "2026-09-06"},
             {"budget": Decimal("100"), "assumed_spend": Decimal("999")}),
            ({"mode": "actual_budget", "start": "2026-09-01", "end": "2026-10-01"}, {}),
            ({"mode": "contribution_cap", "start": "2026-09-01", "end": "2026-10-01"}, {}),
        )

    def test_projection_accepts_every_promotion_shape(self):
        """model 投影与 public artifact 投影对四种模式都不能拒绝自己的生产结果。"""
        from bi_agent.business_query.tool import to_model_result, to_public_artifact
        from bi_agent.catalog import build_catalog
        from bi_agent.promotion import PromotionRequest, evaluate_promotion
        from tests.fakeconn import ShopCatalogConn

        for args, confirmed in self.promotion_cases():
            result = evaluate_promotion(PromotionRequest.model_validate(args),
                                        confirmed_inputs=confirmed, now=self.NOW)
            with self.subTest(mode=args["mode"], status=result.status):
                catalog = build_catalog(ShopCatalogConn(), result,
                                        allowed_shop_ids=frozenset({"S1"}))
                model_payload = to_model_result(result, catalog)
                public_payload = to_public_artifact(result, catalog)
                self.assertEqual(model_payload["status"], result.status)
                self.assertEqual(public_payload["status"], result.status)
                self.assertEqual(public_payload["filters"]["mode"], args["mode"])
                self.assertEqual(public_payload["metric_definition"],
                                 dict(result.metric_definition))
                self.assertEqual(public_payload["limitations"], result.limitations)
                if result.status == "ok":
                    self.assertTrue(public_payload["data"])
                rendered = json.dumps(public_payload, ensure_ascii=False)
                self.assertNotIn("S1", rendered)

    def test_promotion_rows_equal_declared_contract(self):
        """实际行键 == 声明列集；白名单既不漏列也不残留手抄的幻影列。"""
        from bi_agent.promotion import (
            BUDGET_SCENARIO_COLUMNS,
            PROMOTION_METRIC_DEFINITIONS,
            PROMOTION_RESULT_COLUMNS,
            SPEND_CAP_COLUMNS,
            PromotionRequest,
            evaluate_promotion,
        )
        from bi_agent.runtime.models import (
            ARTIFACT_FILTER_COLUMNS,
            ARTIFACT_RESULT_COLUMNS,
        )

        declared = {"sales_cap": SPEND_CAP_COLUMNS,
                    "budget_scenario": BUDGET_SCENARIO_COLUMNS}
        for args, confirmed in self.promotion_cases():
            result = evaluate_promotion(PromotionRequest.model_validate(args),
                                        confirmed_inputs=confirmed, now=self.NOW)
            with self.subTest(mode=args["mode"], status=result.status):
                for row in result.data:
                    self.assertEqual(set(row), set(declared[args["mode"]]))
                self.assertLessEqual(set(result.metric_definition),
                                     set(PROMOTION_METRIC_DEFINITIONS))
        self.assertEqual(PROMOTION_RESULT_COLUMNS & ARTIFACT_RESULT_COLUMNS,
                         PROMOTION_RESULT_COLUMNS)
        self.assertIn("mode", ARTIFACT_FILTER_COLUMNS)
        for phantom in ("actual_spend", "over_budget", "daily_cap", "contribution_cap"):
            self.assertNotIn(phantom, ARTIFACT_RESULT_COLUMNS)

    def test_column_drift_fails_at_the_producer(self):
        """新增列忘改契约时先在 promotion 一侧失败，而不是漂到投影处。"""
        from bi_agent.promotion import SPEND_CAP_COLUMNS, _project

        row = {key: "1" for key in SPEND_CAP_COLUMNS}
        self.assertEqual(set(_project(SPEND_CAP_COLUMNS, row)), set(SPEND_CAP_COLUMNS))
        with self.assertRaises(ValueError):
            _project(SPEND_CAP_COLUMNS, {**row, "new_column": "2"})
        with self.assertRaises(ValueError):
            _project(SPEND_CAP_COLUMNS, {k: v for k, v in row.items() if k != "basis"})
        with self.assertRaises(ValueError):
            _project(SPEND_CAP_COLUMNS + ("erp_shop_id",), {**row, "erp_shop_id": "S1"})

    def test_whitelist_columns_all_have_validation_rules(self):
        """白名单里不允许出现无校验规则的列（否则新列会绕过类型校验）。"""
        from bi_agent.runtime import models

        covered = (models._NUMERIC_RESULT_COLUMNS | models._DATE_RESULT_COLUMNS
                   | models._DATETIME_RESULT_COLUMNS
                   | models._LISTING_REF_RESULT_COLUMNS
                   | models.INVENTORY_POOL_REF_RESULT_COLUMNS
                   | models.INVENTORY_WAREHOUSE_REF_RESULT_COLUMNS
                   | models.INVENTORY_UNIT_RESULT_COLUMNS
                   | models.INVENTORY_INT_RESULT_COLUMNS
                   | models._METRIC_INT_RESULT_COLUMNS
                   | frozenset(models._METRIC_LABEL_PATTERN_RESULT_COLUMNS)
                   | frozenset(models._LABEL_RESULT_VALUES)
                   | models._REF_RESULT_COLUMNS | models._TEXT_RESULT_COLUMNS)
        self.assertEqual(covered, models.ARTIFACT_RESULT_COLUMNS)


def _reply(text=None, calls=None, reasoning=None):
    from bi_agent.llm import Message, ModelReply, ToolCall

    calls = calls or []
    assistant = Message(role="assistant", content=text, tool_calls=calls,
                        provider_context=({"reasoning_content": reasoning}
                                          if reasoning else {}))
    reply = ModelReply(text=text, tool_calls=calls)
    reply._message = assistant
    return reply


class AgentTests(unittest.TestCase):
    NOW = datetime(2026, 9, 8, 9, tzinfo=ZoneInfo("Asia/Shanghai"))
    KNOWN = ToolResult(status="ok", data=[{"paid_amount": "1000"}],
                       coverage=Coverage(status="complete", start=date(2026, 9, 1),
                                         end=date(2026, 9, 8)))

    def setUp(self):
        from bi_agent.runtime.memory import MemoryQueryRunStore

        self.run_store = MemoryQueryRunStore(
            forbidden_values={"S1", "ERP-P-9"},
        )

    def _conn(self, shops=(("S1", "店铺A"),)):
        # 同一次连接要服务两处读取：Agent 的两列档案与目录投影的三列档案。
        return ShopCatalogConn(shops)

    def _call(self, **overrides):
        from bi_agent.llm import ToolCall

        args = {"start": "2026-09-01", "end": "2026-09-08",
                "shop_ids": [S1_REF], "metrics": ["paid_amount"]}
        args.update(overrides)
        return ToolCall(id="call_1", name="query_business", arguments=args)

    def test_first_turn_maps_ref_and_calls_query(self):
        from bi_agent.agent import SessionState, answer

        model = Mock()
        model.complete.side_effect = [
            _reply(calls=[self._call()]),
            _reply(text="最近7天支付金额1000元")]
        with patch("bi_agent.business_query.nodes.metrics.query_business",
                   return_value=self.KNOWN) as query:
            turn = answer("最近7天店铺A的支付金额", SessionState(subject="u1"),
                          model=model, conn=self._conn(),
                          allowed_shop_ids=frozenset({"S1"}), now=self.NOW,
                          run_store=self.run_store)
            self.assertEqual(query.call_args.args[1].shop_ids, ["S1"])
            self.assertEqual(query.call_args.args[1].start, date(2026, 9, 1))
            self.assertEqual(turn.results[0].data, self.KNOWN.data)
        self.assertEqual(len(self.run_store.runs), 1)
        self.assertEqual(len(self.run_store.artifacts), 1)
        first_model_messages = model.complete.call_args_list[0].args[0]
        model_question = [message.content for message in first_model_messages
                          if message.role == "user"][-1]
        self.assertIn(S1_REF, model_question)
        self.assertNotIn("S1", model_question)
        self.assertNotIn("店铺A", model_question, "真实店名不得进模型")
        self.assertEqual(turn.text, "最近7天支付金额1000元")

    def test_agent_routes_business_queries_only_through_the_graph_adapter(self):
        import bi_agent.agent as agent

        self.assertFalse(hasattr(agent, "_handle_query_business"))
        self.assertFalse(hasattr(agent, "_run_query_business"))

    def test_commerce_tool_is_registered_and_routed_as_one_call(self):
        """商品 Tool 已注册：一次模型调用 = 一次图执行，主层不逐店循环也不自己算钱。

        本用例只钉住主 Agent 侧的接线：工具已公告、参数原样送达、服务端上下文里
        拿到的是真实授权集与共享 deadline、图投出的引用载荷原样回到模型，
        而公开载荷只进展示层。图本身的口径与门禁由 tests.test_commerce 钉。
        """
        import bi_agent.agent as agent
        from bi_agent.agent import SessionState, answer
        from bi_agent.commerce.graph import CommerceExecution
        from bi_agent.commerce.models import CommerceDataset, CommerceReport
        from bi_agent.llm import ToolCall
        from bi_agent.runtime.models import (ArtifactRef, DomainArtifact, DomainResult,
                                             DomainStatus)
        from uuid import uuid4

        self.assertEqual([item["function"]["name"] for item in agent._tool_schemas()],
                         ["query_business", "analyze_product_performance",
                          "compare_performance", "audit_listing_prices",
                          "inspect_inventory", "evaluate_promotion"])
        captured: dict[str, object] = {}
        model_payload = {"status": "ok", "data": [{"sales_amount": "550"}],
                         "excluded_scope": [{"shop_ref": S1_REF,
                                             "reason": "coverage_incomplete"}]}

        def fake_graph(request, context, **kwargs):
            captured["request"] = request
            captured["context"] = context
            dataset = CommerceDataset(artifact_type="metric_result", result=self.KNOWN)
            report = CommerceReport(status="ok", datasets=(dataset,))
            return CommerceExecution(
                domain_result=DomainResult(
                    run_id=uuid4(), status=DomainStatus.SUCCESS,
                    model_payload=model_payload,
                    artifacts=[DomainArtifact(
                        ref=ArtifactRef(id=uuid4(), type="metric_result"),
                        public_payload=model_payload)],
                    coverage=self.KNOWN.coverage),
                report=report)

        model = Mock()
        model.complete.side_effect = [
            _reply(calls=[ToolCall(id="call_c", name="analyze_product_performance",
                                   arguments={
                                       "product": {"text": "直钉枪"},
                                       "start": "2026-09-01", "end": "2026-09-08",
                                       "metrics": ["sales_amount"]})]),
            _reply(text="该商品在售7天里卖了550元")]
        with patch("bi_agent.commerce.tool.run_commerce_graph",
                   side_effect=fake_graph) as routed:
            turn = answer("直钉枪最近7天卖得怎么样", SessionState(subject="u1"),
                          model=model, conn=self._conn(),
                          allowed_shop_ids=frozenset({"S1"}), now=self.NOW,
                          run_store=self.run_store)
            self.assertEqual(routed.call_count, 1, "一次工具调用只能跑一次图")
        self.assertEqual(captured["request"].product.text, "直钉枪")
        context = captured["context"]
        self.assertEqual(context.allowed_shop_ids, frozenset({"S1"}),
                         "真实授权集只能由服务端注入")
        self.assertEqual(dict(context.shop_refs), {"S1": S1_REF})
        # 图沿用本回合的 30 秒总预算：不是另起一份，也不是把已用掉的时间重置。
        import time as time_module

        self.assertGreater(context.deadline, time_module.monotonic())
        self.assertLessEqual(context.deadline,
                             time_module.monotonic() + agent.TOTAL_BUDGET_SECONDS)
        self.assertEqual([artifact["status"] for artifact in turn.artifacts], ["ok"])
        tool_messages = [m for m in turn.state.turns if m.role == "tool"]
        self.assertIn("550", tool_messages[-1].content or "")
        self.assertIn("excluded_scope", tool_messages[-1].content or "",
                      "范围缺口必须随载荷回到模型，不然它会把 partial 说成全量")
        self.assertNotIn("S1", tool_messages[-1].content or "")
        self.assertEqual(turn.results[0].data, self.KNOWN.data)

    def test_comparison_tool_is_routed_to_the_same_graph_once(self):
        """对比 Tool 的接线：主层只调一次图，不逐店循环，也不自己拼下钻范围。

        图本身的分组、排名与图表契约由 tests.test_comparison 钉；这里只钉住
        `compare_performance` 在提示词与路由里的形状（计划 Task 8）。
        """
        import bi_agent.agent as agent
        from bi_agent.agent import SessionState, answer
        from bi_agent.commerce.graph import CommerceExecution
        from bi_agent.commerce.models import CommerceDataset, CommerceReport
        from bi_agent.llm import ToolCall
        from bi_agent.runtime.models import (ArtifactRef, DomainArtifact, DomainResult,
                                             DomainStatus)
        from uuid import uuid4

        captured: dict[str, object] = {}
        model_payload = {"status": "ok", "data": [{"platform": "fxg",
                                                   "sales_amount": "900"}],
                         "group_statuses": [{"platform": "pdd", "status": "partial",
                                             "shops_requested": 1, "shops_evaluated": 0,
                                             "reason": "commerce_scope_excluded"}]}

        def fake_graph(request, context, **kwargs):
            captured["request"] = request
            captured["context"] = context
            captured["report_kind"] = kwargs.get("report_kind")
            dataset = CommerceDataset(artifact_type="comparison_table",
                                      result=self.KNOWN)
            return CommerceExecution(
                domain_result=DomainResult(
                    run_id=uuid4(), status=DomainStatus.PARTIAL,
                    model_payload=model_payload,
                    artifacts=[DomainArtifact(
                        ref=ArtifactRef(id=uuid4(), type="comparison_table"),
                        public_payload=model_payload)],
                    coverage=self.KNOWN.coverage),
                report=CommerceReport(status="partial", datasets=(dataset,)))

        model = Mock()
        model.complete.side_effect = [
            _reply(calls=[ToolCall(id="call_p", name="compare_performance",
                                   arguments={"scope": {"platforms": ["fxg"]},
                                              "start": "2026-09-01",
                                              "end": "2026-09-08",
                                              "group_by": "shop",
                                              "metrics": ["sales_amount"]})]),
            _reply(text="抖音两家店的销售额见下方对比表")]
        with patch("bi_agent.commerce.tool.run_commerce_graph",
                   side_effect=fake_graph) as routed:
            turn = answer("抖音各店支付金额对比", SessionState(subject="u1"),
                          model=model, conn=self._conn(),
                          allowed_shop_ids=frozenset({"S1"}), now=self.NOW,
                          run_store=self.run_store)
            self.assertEqual(routed.call_count, 1, "一次工具调用只能跑一次图")
        self.assertEqual(captured["report_kind"], "comparison")
        self.assertEqual(captured["request"].group_by, "shop",
                         "下钻范围按本次入参重新解，不沿用上一轮的已评估集合")
        self.assertEqual(captured["request"].start, date(2026, 9, 1),
                         "窗口与口径由服务端上下文原样带到图里")
        self.assertEqual(captured["context"].allowed_shop_ids, frozenset({"S1"}))
        tool_messages = [m for m in turn.state.turns if m.role == "tool"]
        self.assertIn("group_statuses", tool_messages[-1].content or "",
                      "缺失分组必须随载荷回到模型，不然它会把四个平台说成五个")
        self.assertNotIn("S1", tool_messages[-1].content or "")

    def test_follow_up_keeps_filters_only_dates_change(self):
        from bi_agent.agent import SessionState, answer

        model = Mock()
        model.complete.side_effect = [
            _reply(calls=[self._call()]),
            _reply(text="最近7天支付金额1000元")]
        with patch("bi_agent.business_query.nodes.metrics.query_business", return_value=self.KNOWN):
            turn1 = answer("最近7天店铺A的支付金额", SessionState(subject="u1"),
                           model=model, conn=self._conn(),
                           allowed_shop_ids=frozenset({"S1"}), now=self.NOW,
                           run_store=self.run_store)
        model.complete.side_effect = [
            _reply(calls=[ToolCall(id="call_2", name="query_business",
                                   arguments={"start": "2026-08-01",
                                              "end": "2026-09-01"})]),
            _reply(text="上个月支付500元")]
        with patch("bi_agent.business_query.nodes.metrics.query_business", return_value=self.KNOWN) as query:
            turn2 = answer("那上个月呢", turn1.state, model=model,
                           conn=self._conn(), allowed_shop_ids=frozenset({"S1"}),
                           now=self.NOW, run_store=self.run_store)
            request = query.call_args.args[1]
            self.assertEqual(request.shop_ids, ["S1"])
            self.assertEqual(request.metrics, ["paid_amount"])
            self.assertEqual(request.start, date(2026, 8, 1))
            self.assertEqual(request.end, date(2026, 9, 1))
        self.assertEqual(turn2.text, "上个月支付500元")

    def test_sales_ambiguity_clarifies_without_model(self):
        from bi_agent.agent import SessionState, answer

        model = Mock()
        turn = answer("我店里销售额怎么样？", SessionState(subject="u1"), model=model,
                      conn=self._conn(), allowed_shop_ids=frozenset({"S1"}), now=self.NOW,
                      run_store=self.run_store)
        self.assertIsNotNone(turn.clarification)
        self.assertIn("口径", turn.clarification)
        self.assertEqual(turn.results, [])
        model.complete.assert_not_called()

    def test_same_name_shop_clarifies(self):
        from bi_agent.agent import SessionState, answer

        model = Mock()
        turn = answer("店铺A上周业绩", SessionState(subject="u1"), model=model,
                      conn=self._conn(shops=(("S1", "店铺A"), ("S3", "店铺A"))),
                      allowed_shop_ids=frozenset({"S1", "S3"}), now=self.NOW,
                      run_store=self.run_store)
        self.assertIsNotNone(turn.clarification)
        self.assertIn("同名", turn.clarification)
        model.complete.assert_not_called()

    def test_unknown_tool_rejected(self):
        from bi_agent.agent import SessionState, answer
        from bi_agent.llm import ToolCall

        model = Mock()
        model.complete.side_effect = [
            _reply(calls=[ToolCall(id="call_1", name="run_sql",
                                   arguments={"sql": "SELECT 1"})]),
            _reply(text="只能使用两个工具")]
        with patch("bi_agent.business_query.nodes.metrics.query_business", return_value=self.KNOWN) as query:
            turn = answer("查点什么", SessionState(subject="u1"), model=model,
                          conn=self._conn(), allowed_shop_ids=frozenset({"S1"}),
                          now=self.NOW, run_store=self.run_store)
            query.assert_not_called()
        self.assertEqual(turn.results, [])
        tool_messages = [m for m in turn.state.turns if m.role == "tool"]
        self.assertTrue(any("unknown_tool" in (m.content or "") for m in tool_messages))

    def test_unauthorized_alias_and_injection_rejected(self):
        from bi_agent.agent import SessionState, answer

        model = Mock()
        model.complete.side_effect = [
            _reply(calls=[self._call(shop_ids=["shop_2"])]),
            _reply(text="好的")]
        with patch("bi_agent.business_query.nodes.metrics.query_business", return_value=self.KNOWN) as query:
            answer("查询一下", SessionState(subject="u1"), model=model,
                   conn=self._conn(), allowed_shop_ids=frozenset({"S1"}), now=self.NOW,
                   run_store=self.run_store)
            query.assert_not_called()

        model = Mock()
        model.complete.side_effect = [
            _reply(calls=[self._call(shop_ids=["S1; DROP TABLE bi.orders; --"])]),
            _reply(text="好的")]
        with patch("bi_agent.business_query.nodes.metrics.query_business", return_value=self.KNOWN) as query:
            turn = answer("查询一下", SessionState(subject="u1"), model=model,
                          conn=self._conn(), allowed_shop_ids=frozenset({"S1"}),
                          now=self.NOW, run_store=self.run_store)
            query.assert_not_called()
        self.assertEqual(turn.results, [])

    def test_corrected_query_reuses_message_id_and_advances_attempt_number(self):
        from bi_agent.agent import SessionState, answer
        from bi_agent.llm import ToolCall

        model = Mock()
        model.complete.side_effect = [
            _reply(calls=[ToolCall(id="call_1", name="query_business",
                                   arguments=None, arguments_error="invalid_json")]),
            _reply(calls=[ToolCall(
                id="call_2",
                name="query_business",
                arguments={
                    "start": "2026-09-01",
                    "end": "2026-09-08",
                    "shop_ids": [S1_REF],
                    "metrics": ["paid_amount"],
                },
            )]),
            _reply(text="已修正并完成查询"),
        ]
        with patch("bi_agent.business_query.nodes.metrics.query_business", return_value=self.KNOWN) as query:
            turn = answer("最近7天店铺A的支付金额", SessionState(subject="u1"),
                          model=model, conn=self._conn(),
                          allowed_shop_ids=frozenset({"S1"}), now=self.NOW,
                          run_store=self.run_store)
            query.assert_called_once()
        runs = list(self.run_store.runs.values())
        self.assertEqual(len(runs), 2)
        self.assertEqual({run["attempt_no"] for run in runs}, {1, 2})
        self.assertEqual(len({run["user_message_id"] for run in runs}), 1)
        self.assertEqual(len(turn.results), 1)
        self.assertEqual(model.complete.call_count, 3)

    def _basis_incompatible_result(self):
        """确定性引擎对跨口径范围的真实拒答：schema 合法、已执行、已可落盘。"""
        return ToolResult(
            status="invalid_parameters", data=[],
            coverage=Coverage(status="complete", start=date(2026, 9, 1),
                              end=date(2026, 9, 8)),
            filters={"start": "2026-09-01", "end": "2026-09-08",
                     "shop_ids": ["S1"], "metrics": ["product_paid_amount"],
                     "group_by": "product", "compare": "none", "currency": "CNY",
                     "basis_policy": "strict"},
            data_as_of=self.NOW,
            limitations=["这些指标在本次范围内口径互不兼容：product_paid_amount；"
                         "请按店铺分列后逐组查看，不能汇总或比较"],
            basis=[{"shop_id": "S1", "metric": "product_paid_amount",
                    "basis": "verified_payment/1", "time_basis": "pay_time",
                    "metric_version": "1"}])

    def test_basis_incompatible_result_is_delivered_not_retried_as_arguments(self):
        """basis_incompatible 是确定性结果，不是模型参数非法：不得消耗修正名额。"""
        from bi_agent.agent import SessionState, answer

        model = Mock()
        model.complete.side_effect = [
            _reply(calls=[self._call(metrics=["product_paid_amount"],
                                     group_by="product")]),
            _reply(text="两组口径不兼容，不能汇总或排名，请按店铺分列后再查。"),
        ]
        with patch("bi_agent.metrics.query_business",
                   return_value=self._basis_incompatible_result()) as query:
            turn = answer("近半月最好的10个商品", SessionState(subject="u1"),
                          model=model, conn=self._conn(),
                          allowed_shop_ids=frozenset({"S1"}), now=self.NOW,
                          run_store=self.run_store)
            query.assert_called_once()

        # 结果与 Artifact 被交付（bug 下两者都为空，且模型只收到修正消息）。
        self.assertEqual(len(turn.results), 1)
        self.assertEqual(turn.results[0].status, "invalid_parameters")
        self.assertEqual(len(turn.artifacts), 1)
        self.assertEqual(turn.artifacts[0]["status"], "invalid_parameters")
        self.assertEqual(model.complete.call_count, 2)
        self.assertNotIn("参数两次非法", turn.text or "")
        tool_messages = [m for m in turn.state.turns if m.role == "tool"]
        self.assertEqual(len(tool_messages), 1)
        content = tool_messages[-1].content or ""
        self.assertIn("口径互不兼容", content)
        self.assertIn("按店铺分列", content)
        self.assertNotIn('"error"', content, "不得退化成 invalid_parameters 修正通道")
        # 确定性兜底链也要能复述已交付的拒答，而不是只说“换一种问法”。
        from bi_agent.agent import _deterministic_summary
        summary = _deterministic_summary(list(turn.results))
        self.assertIsNotNone(summary)
        self.assertIn("口径互不兼容", summary)

    def test_malformed_arguments_still_get_one_correction_then_stop(self):
        """反恒真：真正的模型参数非法仍只给一次修正，第二次就停。"""
        from bi_agent.agent import SessionState, answer
        from bi_agent.llm import ToolCall

        bad = ToolCall(id="call_1", name="query_business", arguments=None,
                       arguments_error="invalid_json")
        model = Mock()
        model.complete.side_effect = [
            _reply(calls=[bad]),
            _reply(calls=[ToolCall(id="call_2", name="query_business",
                                   arguments=None, arguments_error="invalid_json")]),
        ]
        with patch("bi_agent.metrics.query_business") as query:
            turn = answer("查一下", SessionState(subject="u1"), model=model,
                          conn=self._conn(), allowed_shop_ids=frozenset({"S1"}),
                          now=self.NOW, run_store=self.run_store)
            query.assert_not_called()

        self.assertEqual(turn.results, [])
        self.assertEqual(turn.artifacts, [])
        self.assertEqual(model.complete.call_count, 2)
        self.assertIn("参数两次非法", turn.text or "")

    def test_partitioned_product_ranking_result_is_delivered_in_one_call(self):
        """复现句「近半月最好的10个商品」：一次调用拿到分区榜，不再四次试错到预算耗尽。"""
        from bi_agent.agent import SessionState, answer

        model = Mock()
        model.complete.side_effect = [
            _reply(calls=[self._call(metrics=["product_paid_amount"],
                                     group_by="product",
                                     basis_policy="partitioned",
                                     start="2026-09-01", end="2026-09-19")]),
            _reply(text="已认证口径的 Top 2 见结果。"),
        ]
        with patch("bi_agent.metrics.query_business",
                   return_value=self._partitioned_result()) as query:
            turn = answer("近半月最好的10个商品", SessionState(subject="u1"),
                          model=model, conn=self._conn(),
                          allowed_shop_ids=frozenset({"S1"}), now=self.NOW,
                          run_store=self.run_store)
            query.assert_called_once()
        request = query.call_args.args[1]
        self.assertEqual(request.basis_policy, "partitioned")
        # 模型给的 09-19 是没跑完的一天：确定性解析器把两侧边界都改成最近15个完整日。
        self.assertEqual(request.start, date(2026, 8, 24))
        self.assertEqual(request.end, date(2026, 9, 8))
        self.assertEqual(model.complete.call_count, 2)
        tool_messages = [m for m in turn.state.turns if m.role == "tool"]
        self.assertEqual(len(tool_messages), 1)
        content = tool_messages[-1].content or ""
        self.assertIn("rank_groups", content)
        self.assertIn("rank_group", content)
        self.assertNotIn("budget_exhausted", content)
        self.assertEqual(len(turn.results), 1)
        self.assertEqual(turn.results[0].status, "ok")
        self.assertEqual(len(turn.artifacts), 1)

    def test_system_prompt_and_tool_doc_direct_partitioned_product_ranking(self):
        """路由不靠运气：提示与工具说明必须直接让模型选对政策与指标。"""
        from bi_agent import agent

        prompt = agent._SYSTEM_PROMPT
        self.assertIn("basis_policy=partitioned", prompt)
        self.assertIn("rank_group", prompt)
        self.assertIn("不能跨分区汇总、比较或排名", prompt)
        description = next(item["function"]["description"] for item
                           in agent._tool_schemas()
                           if item["function"]["name"] == "query_business")
        self.assertIn("partitioned", description)
        enum = agent._tool_schemas()[0]["function"]["parameters"]["properties"][
            "basis_policy"]["enum"]
        self.assertEqual(sorted(enum), ["partitioned", "separate", "strict"])

    def _partitioned_result(self):
        """生产形状的分区结果：两个分区各自 Top-N，未认证分区按样本披露。"""
        return ToolResult(
            status="ok",
            data=[{"shop_id": "S1", "product_id": "P1", "line_kind": "sale",
                   "product_paid_amount": "300", "rank_group": "g1", "rank": 1},
                  {"shop_id": "S1", "product_id": "P2", "line_kind": "sale",
                   "product_paid_amount": "200", "rank_group": "g1", "rank": 2}],
            metric_definition={"product_paid_amount":
                               "已核验的非赠品父项行级分摊支付金额（按line_kind标注）"},
            filters={"start": "2026-08-24", "end": "2026-09-08", "shop_ids": ["S1"],
                     "metrics": ["product_paid_amount"], "group_by": "product",
                     "compare": "none", "currency": "CNY",
                     "basis_policy": "partitioned"},
            data_as_of=self.NOW,
            coverage=Coverage(status="partial", start=date(2026, 8, 24),
                              end=date(2026, 9, 8)),
            limitations=["本次结果按口径分为 1 个分区，分区之间不得汇总、比较或排名"],
            rank_groups=[{"group": "g1", "status": "certified", "shop_ids": ["S1"],
                          "basis": [{"metric": "product_paid_amount",
                                     "basis": "platform_payment/v1",
                                     "time_basis": "pay_time",
                                     "time_certification": "certified"}],
                          "groups_published": 2, "groups_total": 2,
                          "truncated": False}],
            rank_exclusions=[])

    def test_batch_of_five_executes_four(self):
        from bi_agent.agent import SessionState, answer
        from bi_agent.llm import ToolCall

        calls = [ToolCall(id=f"call_{i}", name="query_business",
                          arguments={"start": "2026-09-01", "end": "2026-09-08",
                                     "shop_ids": [S1_REF],
                                     "metrics": ["paid_amount"]})
                 for i in range(1, 6)]
        model = Mock()
        model.complete.side_effect = [_reply(calls=calls)]
        with patch("bi_agent.business_query.nodes.metrics.query_business", return_value=self.KNOWN) as query:
            turn = answer("多查询几个", SessionState(subject="u1"), model=model,
                          conn=self._conn(), allowed_shop_ids=frozenset({"S1"}),
                          now=self.NOW, run_store=self.run_store)
            self.assertEqual(query.call_count, 4)
        self.assertEqual(len(turn.results), 4)
        tool_messages = [m for m in turn.state.turns if m.role == "tool"]
        self.assertTrue(any("budget_exhausted" in (m.content or "")
                            for m in tool_messages))

    def _query_call(self, call_id: str):
        from bi_agent.llm import ToolCall

        return ToolCall(id=call_id, name="query_business",
                        arguments={"start": "2026-09-01", "end": "2026-09-08",
                                   "shop_ids": [S1_REF], "metrics": ["paid_amount"]})

    def test_tool_budget_exhaustion_asks_model_for_final_answer(self):
        """工具预算耗尽后必须补一次纯文本回合，不能把内部占位文案给用户。"""
        from bi_agent.agent import SessionState, answer

        calls = [self._query_call(f"call_{index}") for index in range(1, 6)]
        model = Mock()
        model.complete.side_effect = [
            _reply(calls=calls),
            _reply(text="四个窗口的支付金额见下方数据。"),
        ]
        with patch("bi_agent.metrics.query_business", return_value=self.KNOWN):
            turn = answer("多查询几个", SessionState(subject="u1"), model=model,
                          conn=self._conn(), allowed_shop_ids=frozenset({"S1"}),
                          now=self.NOW)
        self.assertEqual(len(turn.results), 4)
        self.assertEqual(turn.text, "四个窗口的支付金额见下方数据。")
        self.assertIsNone(turn.error_code)
        # 补答不得再带工具，否则模型会继续要求调用
        self.assertEqual(model.complete.call_args_list[-1].args[1], [])

    def test_model_turn_cap_asks_model_for_final_answer(self):
        """模型回合用尽同样要补一次纯文本回合。"""
        from bi_agent.agent import MAX_MODEL_TURNS, SessionState, answer
        from bi_agent.llm import ToolCall

        model = Mock()
        model.complete.side_effect = [
            _reply(calls=[ToolCall(id=f"call_{index}", name="run_sql", arguments={})])
            for index in range(MAX_MODEL_TURNS)
        ] + [_reply(text="只允许两个工具，已按现有结果作答。")]
        with patch("bi_agent.metrics.query_business", return_value=self.KNOWN):
            turn = answer("随便查", SessionState(subject="u1"), model=model,
                          conn=self._conn(), allowed_shop_ids=frozenset({"S1"}),
                          now=self.NOW)
        self.assertEqual(model.complete.call_count, MAX_MODEL_TURNS + 1)
        self.assertEqual(turn.text, "只允许两个工具，已按现有结果作答。")

    def test_final_answer_failure_keeps_results_without_placeholder(self):
        """补答再次失败时保留确定性结果，且不泄露内部占位文案。"""
        from bi_agent.agent import SessionState, answer

        calls = [self._query_call(f"call_{index}") for index in range(1, 6)]
        model = Mock()
        model.complete.side_effect = [_reply(calls=calls), _reply(text="")]
        with patch("bi_agent.metrics.query_business", return_value=self.KNOWN):
            turn = answer("多查询几个", SessionState(subject="u1"), model=model,
                          conn=self._conn(), allowed_shop_ids=frozenset({"S1"}),
                          now=self.NOW)
        self.assertEqual(len(turn.results), 4)
        self.assertIsNone(turn.error_code)
        self.assertNotIn("未取得模型回答", turn.text)
        self.assertTrue(turn.text.strip())

    def test_final_answer_call_failure_is_not_fatal(self):
        """补答调用自身抛错也不能毁掉已取得的确定性结果。"""
        from bi_agent.agent import SessionState, answer

        calls = [self._query_call(f"call_{index}") for index in range(1, 6)]
        model = Mock()
        model.complete.side_effect = [_reply(calls=calls), RuntimeError("boom")]
        with patch("bi_agent.metrics.query_business", return_value=self.KNOWN):
            turn = answer("多查询几个", SessionState(subject="u1"), model=model,
                          conn=self._conn(), allowed_shop_ids=frozenset({"S1"}),
                          now=self.NOW)
        self.assertEqual(len(turn.results), 4)
        self.assertNotIn("未取得模型回答", turn.text)

    def test_mixed_text_and_tool_calls_text_used_as_fallback(self):
        """模型同时给出正文和工具调用时，正文要留作兜底。"""
        from bi_agent.agent import SessionState, answer

        calls = [self._query_call(f"call_{index}") for index in range(1, 6)]
        model = Mock()
        model.complete.side_effect = [_reply(text="已查完四个窗口。", calls=calls)]
        with patch("bi_agent.metrics.query_business", return_value=self.KNOWN):
            turn = answer("多查询几个", SessionState(subject="u1"), model=model,
                          conn=self._conn(), allowed_shop_ids=frozenset({"S1"}),
                          now=self.NOW)
        self.assertEqual(turn.text, "已查完四个窗口。")

    def test_model_error_keeps_results(self):
        from bi_agent.agent import SessionState, answer
        from bi_agent.llm import ModelError

        model = Mock()
        model.complete.side_effect = [_reply(calls=[self._call()]),
                                      ModelError("timeout")]
        with patch("bi_agent.business_query.nodes.metrics.query_business", return_value=self.KNOWN):
            turn = answer("最近7天店铺A的支付金额", SessionState(subject="u1"),
                          model=model, conn=self._conn(),
                          allowed_shop_ids=frozenset({"S1"}), now=self.NOW,
                          run_store=self.run_store)
        self.assertEqual(len(turn.results), 1)
        self.assertIn("固定查询入口仍可用", turn.text)

    def test_artifact_persistence_failure_stops_without_results_or_filter_updates(self):
        from bi_agent.agent import SessionState, answer
        from bi_agent.runtime import ArtifactPersistenceError, MemoryQueryRunStore

        class FailingArtifactStore(MemoryQueryRunStore):
            def save_artifact(self, run_id, artifact):  # type: ignore[no-untyped-def]
                raise ArtifactPersistenceError("database password=not-for-public-output")

        initial_filters = {
            "start": "2026-08-01",
            "end": "2026-09-01",
            "shop_ids": ["S1"],
            "metrics": ["paid_amount"],
        }
        model = Mock()
        model.complete.side_effect = [
            _reply(calls=[self._call()]),
            _reply(text="this answer must not be generated"),
        ]
        with patch(
            "bi_agent.business_query.nodes.metrics.query_business",
            return_value=self.KNOWN,
        ):
            turn = answer(
                "最近7天店铺A的支付金额",
                SessionState(subject="u1", filters=initial_filters),
                model=model,
                conn=self._conn(),
                allowed_shop_ids=frozenset({"S1"}),
                now=self.NOW,
                run_store=FailingArtifactStore(forbidden_values={"S1", "ERP-P-9"}),
            )

        self.assertEqual(turn.results, [])
        self.assertEqual(turn.state.filters, initial_filters)
        self.assertEqual(turn.error_code, "artifact_persistence_failed")
        self.assertEqual(turn.text, "结果保存失败，请稍后重试。")
        self.assertEqual(model.complete.call_count, 1)
        self.assertEqual([message for message in turn.state.turns if message.role == "tool"], [])

    def test_time_budget_not_rewaited(self):
        from bi_agent.agent import SessionState, answer

        model = Mock()
        model.complete.side_effect = [_reply(calls=[self._call()]),
                                      _reply(text="应该不会到达")]
        with patch("bi_agent.business_query.nodes.metrics.query_business", return_value=self.KNOWN):
            with patch("bi_agent.agent.time_module.monotonic",
                       side_effect=[0.0, 0.0, 100.0]), \
                 patch("bi_agent.business_query.nodes.monotonic", return_value=0.0):
                turn = answer("最近7天店铺A的支付金额", SessionState(subject="u1"),
                              model=model, conn=self._conn(),
                              allowed_shop_ids=frozenset({"S1"}), now=self.NOW,
                              run_store=self.run_store)
        self.assertEqual(len(turn.results), 1)
        self.assertEqual(model.complete.call_count, 1)
        self.assertIn("预算已耗尽", turn.text)

    def test_tool_id_and_provider_context_preserved(self):
        from bi_agent.agent import SessionState, answer

        model = Mock()
        model.complete.side_effect = [
            _reply(calls=[self._call()], reasoning="synthetic-private-context"),
            _reply(text="完成")]
        with patch("bi_agent.business_query.nodes.metrics.query_business", return_value=self.KNOWN):
            turn = answer("最近7天店铺A的支付金额", SessionState(subject="u1"),
                          model=model, conn=self._conn(),
                          allowed_shop_ids=frozenset({"S1"}), now=self.NOW,
                          run_store=self.run_store)
        second_call_messages = model.complete.call_args_list[1][0][0]
        assistant = [m for m in second_call_messages if m.role == "assistant"][0]
        self.assertEqual(assistant.provider_context.get("reasoning_content"),
                         "synthetic-private-context")
        tool_messages = [m for m in second_call_messages if m.role == "tool"]
        self.assertEqual(tool_messages[0].tool_call_id, "call_1")
        # 引用映射：发给模型的结果不含真实店铺ID与店名
        self.assertNotIn("S1", tool_messages[0].content)

    def test_model_result_hides_erp_identifiers(self):
        from bi_agent.agent import to_model_result
        from bi_agent.catalog import build_catalog, ref_for_key

        result = ToolResult(
            status="ok",
            data=[{"shop_id": "S1", "product_id": "ERP-P-9", "paid_amount": "1000"}],
            filters={"shop_ids": ["S1"]},
            coverage=Coverage(status="complete", start=date(2026, 9, 1),
                              end=date(2026, 9, 8)),
        )
        catalog = build_catalog(self._conn(), result,
                                allowed_shop_ids=frozenset({"S1"}))
        payload = to_model_result(result, catalog)
        self.assertEqual(payload["data"][0]["shop_ref"], S1_REF)
        self.assertEqual(payload["data"][0]["product_ref"],
                         ref_for_key("product", "ERP-P-9"))
        self.assertEqual(payload["filters"]["shop_refs"], [S1_REF])
        self.assertNotIn("entities", payload, "展示名不得进模型载荷")
        rendered = json.dumps(payload, ensure_ascii=False)
        self.assertNotIn("S1", rendered)
        self.assertNotIn("ERP-P-9", rendered)

    def test_final_text_is_rewritten_from_refs_to_real_names(self):
        """方案 C：模型手里只有引用，用户读到的是已核验的展示名。"""
        from bi_agent.agent import SessionState, answer

        known = ToolResult(
            status="ok", data=[{"shop_id": "S1", "paid_amount": "1000"}],
            filters={"shop_ids": ["S1"]},
            coverage=Coverage(status="complete", start=date(2026, 9, 1),
                              end=date(2026, 9, 8)),
        )
        model = Mock()
        model.complete.side_effect = [
            _reply(calls=[self._call()]),
            _reply(text=f"{S1_REF} 支付金额 1000 元")]
        with patch("bi_agent.business_query.nodes.metrics.query_business",
                   return_value=known):
            turn = answer("最近7天店铺A的支付金额", SessionState(subject="u1"),
                          model=model, conn=self._conn(),
                          allowed_shop_ids=frozenset({"S1"}), now=self.NOW,
                          run_store=self.run_store)

        self.assertEqual(turn.text, "店铺A 支付金额 1000 元")
        self.assertNotIn(S1_REF, turn.text)
        # 模型载荷：只拿得到引用，拿不到真名。
        tool_messages = [message for message in turn.state.turns if message.role == "tool"]
        self.assertIn(S1_REF, tool_messages[0].content)
        self.assertNotIn("店铺A", tool_messages[0].content)
        # 公开附件：带展示名与目录版本。
        self.assertEqual(turn.artifacts[0]["entities"][0]["display_name"], "店铺A")
        self.assertEqual(turn.artifacts[0]["catalog_version"], 7)

    def test_unresolved_name_keeps_the_reference_instead_of_losing_the_answer(self):
        """档案没名字时：正文保留引用并照旧给出数字，不编名也不丢答案。"""
        from bi_agent.agent import SessionState, answer

        known = ToolResult(
            status="ok", data=[{"shop_id": "S1", "paid_amount": "1000"}],
            filters={"shop_ids": ["S1"]},
            coverage=Coverage(status="complete", start=date(2026, 9, 1),
                              end=date(2026, 9, 8)),
        )
        model = Mock()
        model.complete.side_effect = [
            _reply(calls=[self._call()]),
            _reply(text=f"{S1_REF} 支付金额 1000 元")]
        with patch("bi_agent.business_query.nodes.metrics.query_business",
                   return_value=known):
            turn = answer("最近7天支付金额", SessionState(subject="u1"),
                          model=model, conn=self._conn(shops=(("S1", ""),)),
                          allowed_shop_ids=frozenset({"S1"}), now=self.NOW,
                          run_store=self.run_store)

        self.assertIn(S1_REF, turn.text)
        self.assertIn("1000", turn.text)
        self.assertIsNone(turn.artifacts[0]["entities"][0].get("display_name"))
        self.assertEqual(turn.artifacts[0]["entities"][0]["name_source"], "unresolved")

    def test_state_isolation_between_users(self):
        from bi_agent.agent import SessionState, answer

        model = Mock()
        model.complete.side_effect = [_reply(calls=[self._call()]), _reply(text="ok")]
        with patch("bi_agent.business_query.nodes.metrics.query_business", return_value=self.KNOWN):
            turn_a = answer("最近7天店铺A的支付金额", SessionState(subject="A"),
                            model=model, conn=self._conn(),
                            allowed_shop_ids=frozenset({"S1"}), now=self.NOW,
                            run_store=self.run_store)
        state_b = SessionState(subject="B")
        self.assertEqual(state_b.filters, {})
        self.assertNotEqual(turn_a.state.filters, {})
        self.assertNotEqual(turn_a.state.subject, state_b.subject)

    def test_pii_branch(self):
        from bi_agent.agent import SessionState, answer

        model = Mock()
        phone_like_text = "138" + "1234" + "5678"
        turn = answer(f"订单{phone_like_text}退款到账了吗", SessionState(subject="u1"),
                      model=model, conn=self._conn(),
                      allowed_shop_ids=frozenset({"S1"}), now=self.NOW,
                      run_store=self.run_store)
        self.assertIsNotNone(turn.clarification)
        model.complete.assert_not_called()

    def test_explicit_assumptions_parsing(self):
        from bi_agent.agent import explicit_assumptions

        values = explicit_assumptions("假设10月销售额10万元、推广费用率12%，最多花多少？")
        self.assertEqual(values["sales_estimate"], Decimal("100000"))
        self.assertEqual(values["target_ratio"], Decimal("0.12"))
        values = explicit_assumptions(
            "假设9月1日至7日预算100元、已花120元，实耗统计到9月5日结束")
        self.assertEqual(values["budget"], Decimal("100"))
        self.assertEqual(values["assumed_spend"], Decimal("120"))
        self.assertEqual(values["_spent_through_md"], (9, 5))
        self.assertEqual(explicit_assumptions("照上次预算"), {})

    def test_promotion_tool_round(self):
        from bi_agent.agent import SessionState, answer
        from bi_agent.llm import ToolCall
        from bi_agent.metrics import Coverage, ToolResult

        promo_result = ToolResult(
            status="ok", data=[{"spend_cap": "12000", "basis": "用户输入假设"}],
            coverage=Coverage(status="missing", start=None, end=None))
        call = ToolCall(id="call_1", name="evaluate_promotion", arguments={
            "mode": "sales_cap", "start": "2026-10-01", "end": "2026-11-01",
            "sales_estimate": "100000", "target_ratio": "0.12"})
        model = Mock()
        model.complete.side_effect = [_reply(calls=[call]), _reply(text="上限12000元")]
        with patch("bi_agent.agent.evaluate_promotion", return_value=promo_result) as promo:
            turn = answer("假设10月销售额10万元、推广费用率12%，最多花多少？",
                          SessionState(subject="u1"), model=model, conn=self._conn(),
                          allowed_shop_ids=frozenset({"S1"}), now=self.NOW,
                          run_store=self.run_store)
            confirmed = promo.call_args.kwargs["confirmed_inputs"]
            self.assertEqual(confirmed["sales_estimate"], Decimal("100000"))
            self.assertEqual(confirmed["target_ratio"], Decimal("0.12"))
        self.assertEqual(turn.results[0].data[0]["spend_cap"], "12000")

    def test_promotion_turn_projects_through_the_real_producer(self):
        """C-1 全链路回归：不桩化 evaluate_promotion，真实结果走完 answer() 两侧投影。

        旧用例只把一手工造 ToolResult 塞进 answer()，恰好避开白名单漂移；
        本用例同时走 to_model_result()（answer 内部）与 to_public_artifact()。
        """
        from bi_agent.agent import SessionState, answer, to_public_artifact
        from bi_agent.llm import ToolCall

        call = ToolCall(id="call_1", name="evaluate_promotion", arguments={
            "mode": "sales_cap", "start": "2026-10-01", "end": "2026-11-01",
            "sales_estimate": "100000", "target_ratio": "0.12"})
        model = Mock()
        model.complete.side_effect = [_reply(calls=[call]), _reply(text="上限1.2万元")]
        turn = answer("假设10月销售额10万元、推广费用率12%，最多花多少？",
                      SessionState(subject="u1"), model=model, conn=self._conn(),
                      allowed_shop_ids=frozenset({"S1"}), now=self.NOW,
                      run_store=self.run_store)
        self.assertIsNone(turn.error_code)
        self.assertEqual(turn.text, "上限1.2万元")
        self.assertEqual(turn.results[0].status, "ok")
        tool_messages = [message for message in turn.state.turns if message.role == "tool"]
        model_payload = json.loads(tool_messages[0].content)
        self.assertEqual(model_payload["data"][0]["spend_cap"], "12000.00")
        self.assertEqual(model_payload["filters"]["mode"], "sales_cap")
        public_payload = turn.artifacts[0]
        self.assertEqual(public_payload["data"][0]["basis"], "用户输入假设")
        self.assertNotIn("S1", json.dumps(public_payload, ensure_ascii=False))


class PlatformProfilesConn:
    """只回答「授权店的平台档案」这一条读取（`data_quality.SHOP_PLATFORMS_SQL`）。

    口径候选只能从服务端档案派生，所以替身也只给 (shop_id, platform) 两列：
    多给一列展示名就会被读成平台名，那条 fail closed 用例当场失真。
    """

    def __init__(self, rows: Sequence[tuple[str, str]] = ()) -> None:
        self.rows = [tuple(row) for row in rows]
        self.queries: list[tuple[str, tuple]] = []

    def execute(self, sql: str, params: object = None) -> _FakeResult:
        text = " ".join(str(sql).split())
        self.queries.append((text, tuple(params)))
        if not text.startswith("SELECT shop_id, platform FROM reporting.v_shops"):
            raise AssertionError(f"口径澄清不该读别的表：{text[:120]}")
        wanted = set(params[0])
        return _FakeResult([row for row in self.rows if row[0] in wanted])


class SalesBasisClarificationTests(unittest.TestCase):
    """Q08（修订版）：「销售额」澄清先说授权店铺拿得到哪些口径，再要期间。

    候选由服务端授权档案 + 来源注册表派生：不写真实店号、不猜口径、不把「已登记」
    说成「已就绪」，也不把可用性固定成某一句平台名（计划 Task 11、设计 §6）。
    """

    def _clarify(self, rows: Sequence[tuple[str, str]]) -> str:
        from bi_agent.agent import sales_basis_clarification

        return sales_basis_clarification(
            PlatformProfilesConn(rows), frozenset(shop for shop, _ in rows))

    def test_single_certified_platform_names_its_basis_and_asks_the_period(self):
        text = self._clarify([("S1", "fxg")])
        self.assertIn("统计期间", text)
        self.assertIn("fxg", text)
        self.assertIn("platform_payment/v1", text)
        self.assertIn("付款时间窗口已认证", text)
        self.assertNotIn("S1", text, "授权集只能决定候选，不能把店号带进澄清文案")
        self.assertNotIn("可用的销售额是", text, "登记了口径不等于宣布结果可用")

    def test_mixed_scope_lists_every_registered_platform_without_picking_one(self):
        text = self._clarify([("S1", "fxg"), ("T1", "tb"), ("J1", "jd"),
                              ("P1", "pdd")])
        self.assertIn("tb：erp_outstock_payment/v1", text)
        self.assertIn("非平台账单 GMV", text)
        self.assertIn("实测不成立", text)
        self.assertIn("jd", text)
        self.assertIn("未逐店对照", text)
        # 顺序只由平台码决定：同一个授权集两次问不能给出两份文案，否则模型会把
        # 上一次看到的顺序当成事实。
        self.assertEqual(text, self._clarify([("J1", "jd"), ("P1", "pdd"),
                                              ("T1", "tb"), ("S1", "fxg")]))

    def test_pdd_stays_unavailable_not_pending(self):
        """拼多多只说「无支付口径能力」：不写「等授权」「待开通」，也不给它一个金额口径。"""
        text = self._clarify([("P1", "pdd")])
        self.assertIn("无支付口径能力", text)
        self.assertIn("erp_document/v1", text)
        for wording in ("等待", "即将", "待开通", "待授权", "已可用"):
            self.assertNotIn(wording, text)

    def test_unregistered_platform_fails_closed_without_fallback(self):
        text = self._clarify([("X1", "alibabac2m")])
        self.assertIn("来源未登记，没有可用口径", text)
        self.assertIn("不回退到其它通道", text)

    def test_empty_authorization_still_asks_instead_of_defaulting(self):
        text = self._clarify([])
        self.assertIn("当前授权范围内没有已登记来源的店铺", text)
        self.assertIn("统计期间", text)

    def test_clarification_happens_before_any_model_turn(self):
        from bi_agent.agent import SessionState, answer

        model = Mock()
        turn = answer("我店里销售额怎么样？", SessionState(subject="u1"), model=model,
                      conn=ShopCatalogConn([("S1", "店铺A")], platform="fxg"),
                      allowed_shop_ids=frozenset({"S1"}),
                      now=datetime(2026, 9, 8, 9, tzinfo=ZoneInfo("Asia/Shanghai")))
        model.complete.assert_not_called()
        self.assertEqual(turn.results, [])
        self.assertIn("fxg", turn.clarification or "")
        self.assertIn("统计期间", turn.clarification or "")


class OutstockSourceTests(unittest.TestCase):
    """淘系出库通道（erp.trade.outstock.simple.query）：源路由、规范化、PII 红线。"""

    TZ = timezone(timedelta(hours=8))

    @staticmethod
    def _ms(dt: datetime) -> int:
        return int(dt.timestamp() * 1000)

    def _raw(self, **overrides):
        paid = datetime(2026, 8, 16, 10, 30, tzinfo=self.TZ)
        modified = datetime(2026, 8, 20, 12, 0, tzinfo=self.TZ)
        raw = {
            "sid": 123, "tid": "T-1", "userId": 166520,
            "payAmount": "16.90", "payment": "16.90", "cost": "10.00",
            "grossProfit": "6.90", "postFee": "0.00",
            "payTime": self._ms(paid), "modified": self._ms(modified),
            "updTime": self._ms(modified),
            "status": "WAIT_BUYER_CONFIRM_GOODS", "sysStatus": "SELLER_SEND_GOODS",
            "orders": [{
                "id": 9001, "oid": "O1", "tid": "T-1",
                "itemSysId": 1350787, "skuSysId": 2204282, "skuId": "S1",
                "num": "1", "giftNum": 0,
                "payAmount": "16.90", "payment": "16.90", "cost": "10.00",
                "status": "WAIT_BUYER_CONFIRM_GOODS",
                "created": self._ms(paid), "modified": self._ms(modified),
            }],
        }
        raw.update(overrides)
        return raw

    def test_outstock_normalise_field_mapping(self):
        from bi_agent.sync import OUTSTOCK_SOURCE, normalise_trade
        modified = datetime(2026, 8, 20, 12, 0, tzinfo=self.TZ)
        paid = datetime(2026, 8, 16, 10, 30, tzinfo=self.TZ)
        trade = normalise_trade(self._raw(), source=OUTSTOCK_SOURCE)
        self.assertEqual(trade["source"], OUTSTOCK_SOURCE)
        self.assertEqual(trade["erp_id"], "123")
        self.assertEqual(trade["shop_id"], "166520")
        self.assertEqual(trade["commercial_ids"], ["T-1"])
        self.assertIsNone(trade["split_parent_id"])
        self.assertEqual(trade["source_updated_at"], modified)
        self.assertEqual(trade["paid_at"], paid)
        self.assertEqual(trade["raw_pay_amount"], Decimal("16.90"))
        self.assertEqual(trade["raw_payment"], Decimal("16.90"))
        # 淘系不返回 platformPaymentAmount：列自然保持 NULL，verified 不依赖它
        self.assertIsNone(trade["raw_platform_payment"])
        self.assertEqual(trade["raw_cost"], Decimal("10.00"))
        self.assertEqual(trade["raw_gross_profit"], Decimal("6.90"))
        self.assertTrue(trade["active"])
        self.assertEqual(trade["normalization_status"], "normal")
        self.assertTrue(trade["items_present"])
        item = trade["items"][0]
        self.assertEqual(item["line_id"], "9001")
        self.assertEqual(item["commercial_id"], "T-1")
        self.assertEqual(item["product_id"], "1350787")
        self.assertEqual(item["sku_id"], "2204282")
        self.assertEqual(item["quantity"], Decimal("1"))
        self.assertEqual(item["gift_quantity"], Decimal("0"))
        self.assertEqual(item["raw_paid_amount"], Decimal("16.90"))
        self.assertEqual(item["raw_payment"], Decimal("16.90"))
        self.assertEqual(item["raw_unit_cost"], Decimal("10.00"))

    def test_outstock_status_activity(self):
        from bi_agent.sync import OUTSTOCK_SOURCE, normalise_trade
        closed = normalise_trade(self._raw(status="TRADE_CLOSED",
                                           sysStatus="CLOSED"), source=OUTSTOCK_SOURCE)
        self.assertFalse(closed["active"])
        sending = normalise_trade(self._raw(status="WAIT_SELLER_SEND_GOODS"),
                                  source=OUTSTOCK_SOURCE)
        self.assertTrue(sending["active"])
        # 交易查询通道的 ERP 取消态仍按原规则判不活跃
        cancelled = normalise_trade(self._raw(status="CANCELLED"), source=OUTSTOCK_SOURCE)
        self.assertFalse(cancelled["active"])

    def test_split_sid_maps_split_parent(self):
        from bi_agent.sync import OUTSTOCK_SOURCE, normalise_trade
        split = normalise_trade(self._raw(splitSid=999, splitType=1),
                                source=OUTSTOCK_SOURCE)
        self.assertEqual(split["split_parent_id"], "999")
        for empty in (-1, "-1", "", None, "  "):
            plain = normalise_trade(self._raw(splitSid=empty), source=OUTSTOCK_SOURCE)
            self.assertIsNone(plain["split_parent_id"])
        # 交易查询通道仍优先用 splitParentId
        legacy = normalise_trade(self._raw(splitParentId="P1"), source=OUTSTOCK_SOURCE)
        self.assertEqual(legacy["split_parent_id"], "P1")

    def test_missing_user_id_is_invalid(self):
        from bi_agent.sync import OUTSTOCK_SOURCE, normalise_trade
        raw = self._raw()
        raw.pop("userId")
        trade = normalise_trade(raw, source=OUTSTOCK_SOURCE)
        self.assertEqual(trade["normalization_status"], "invalid")

    def test_platform_routing_table(self):
        from bi_agent.sync import (
            AFTERSALE_SOURCE, ORDER_SOURCE, ORDER_SOURCE_BY_PLATFORM, OUTSTOCK_SOURCE)
        # 路由表只有一个真源（sources 注册表）：未登记平台不在表里，也没有默认回退。
        self.assertEqual({platform for platform, source in ORDER_SOURCE_BY_PLATFORM.items()
                          if source == OUTSTOCK_SOURCE}, {"tb", "tm", "pdd"})
        self.assertEqual(ORDER_SOURCE_BY_PLATFORM["fxg"], ORDER_SOURCE)
        self.assertNotIn(AFTERSALE_SOURCE, ORDER_SOURCE_BY_PLATFORM.values())
        self.assertNotIn("alibabac2m", ORDER_SOURCE_BY_PLATFORM)

    def test_shop_order_source_lookup(self):
        from bi_agent.sync import (
            ORDER_SOURCE, OUTSTOCK_SOURCE, _shop_order_source)

        class Result:
            def __init__(self, row):
                self._row = row

            def fetchone(self):
                return self._row

        class Conn:
            def __init__(self, row):
                self.row = row

            def execute(self, sql, params=None):
                assert "bi.shops" in sql
                return Result(self.row)

        self.assertEqual(_shop_order_source(Conn(("tb",)), "166520"), OUTSTOCK_SOURCE)
        self.assertEqual(_shop_order_source(Conn(("TM",)), "166687"), OUTSTOCK_SOURCE)
        self.assertEqual(_shop_order_source(Conn(("pdd",)), "166712"), OUTSTOCK_SOURCE)
        self.assertEqual(_shop_order_source(Conn(("fxg",)), "166754"), ORDER_SOURCE)
        # 未登记平台不再回退默认源：宁可不跑同步，也不能把空响应写成完整覆盖。
        for platform in ("", "1688", "alibabac2m"):
            with self.subTest(platform=platform):
                with self.assertRaises(SystemExit):
                    _shop_order_source(Conn((platform,)), "1")
        with self.assertRaises(SystemExit):
            _shop_order_source(Conn(None), "404")

    def test_fetch_window_routes_method(self):
        from bi_agent.sync import (
            ORDER_SOURCE, OUTSTOCK_SOURCE, Window, fetch_window)
        window = Window(datetime(2026, 8, 16, tzinfo=self.TZ),
                        datetime(2026, 8, 17, tzinfo=self.TZ))

        class Client:
            def __init__(self):
                self.calls = []

            def call(self, method, params):
                self.calls.append((method, dict(params)))
                return {"success": True, "list": [], "total": 0}

        client = Client()
        list(fetch_window(client, entity="orders", shop_id="166520",
                          window=window, mode="incremental",
                          order_source=OUTSTOCK_SOURCE))
        self.assertEqual([m for m, _ in client.calls], [OUTSTOCK_SOURCE])
        self.assertEqual(client.calls[0][1]["timeType"], "upd_time")
        self.assertEqual(client.calls[0][1]["queryType"], "0")

        client = Client()
        list(fetch_window(client, entity="orders", shop_id="166520",
                          window=window, mode="backfill",
                          order_source=OUTSTOCK_SOURCE))
        self.assertEqual({m for m, _ in client.calls}, {OUTSTOCK_SOURCE})
        self.assertEqual([p["queryType"] for _, p in client.calls], ["0", "1"])
        self.assertEqual({p["timeType"] for _, p in client.calls}, {"pay_time"})

        # 默认回退：不传 order_source 时仍走交易查询
        client = Client()
        list(fetch_window(client, entity="orders", shop_id="166754",
                          window=window, mode="incremental"))
        self.assertEqual([m for m, _ in client.calls], [ORDER_SOURCE])

        # 售后不受路由影响
        client = Client()
        list(fetch_window(client, entity="aftersales_occurrence", shop_id="166520",
                          window=window, mode="incremental",
                          order_source=OUTSTOCK_SOURCE))
        self.assertEqual([m for m, _ in client.calls], ["erp.aftersale.list.query"])

    def test_normalised_key_set_frozen(self):
        from bi_agent.sync import normalise_aftersale
        trade = self._normalised_trade()
        self.assertEqual(set(trade), {
            "shop_id", "erp_id", "commercial_ids", "split_parent_id", "source",
            "source_updated_at", "platform_modified_at", "paid_at", "raw_pay_amount",
            "raw_payment", "raw_platform_payment", "raw_cost", "raw_gross_profit",
            "unified_status", "system_status",
            "active", "normalization_status", "items_present", "items"})
        self.assertEqual(set(trade["items"][0]), {
            "line_id", "commercial_id", "platform_line_id", "product_id", "sku_id",
            "source_type", "product_name_snapshot", "sku_label_snapshot",
            "paid_at", "quantity", "gift_quantity", "raw_paid_amount", "raw_payment",
            "raw_unit_cost", "allocated_paid_amount", "allocation_verified",
            "line_kind", "active"})
        aftersale = normalise_aftersale({"id": "R1", "tid": "T-1"})
        self.assertNotIn("buyerName", aftersale)
        self.assertNotIn("buyerPhone", aftersale)

    def _normalised_trade(self):
        from bi_agent.sync import OUTSTOCK_SOURCE, normalise_trade
        return normalise_trade(self._raw(), source=OUTSTOCK_SOURCE)

    def test_pii_never_normalised_or_stored(self):
        """PII 红线守护：出库/售后样本中塞满非空敏感字段，规范化结果不得出现。

        出库接口响应含 buyerNick/收件人信息/openUid/shopName 等（部分脱敏
        仍非空）；现表无对应列，本用例防的是未来扩列/改白名单时遗忘约定。"""
        from bi_agent.sync import (
            PII_FORBIDDEN_FIELDS, normalise_aftersale, normalise_trade)
        sentinels = {key: f"PII-LEAK-{key}" for key in sorted(PII_FORBIDDEN_FIELDS)}
        head = {**self._raw(**sentinels)}
        head["orders"] = [{**self._raw()["orders"][0], **sentinels}]
        trade = normalise_trade(head, source="erp.trade.outstock.simple.query")
        blob = repr(trade)
        self.assertNotIn("PII-LEAK-", blob)
        for key in PII_FORBIDDEN_FIELDS:
            self.assertNotIn(key, trade)
            self.assertNotIn(key, trade["items"][0])
        after_raw = {"id": "R1", "tid": "T-1", "sid": "123", **sentinels}
        aftersale = normalise_aftersale(after_raw)
        self.assertNotIn("PII-LEAK-", repr(aftersale))
        for key in PII_FORBIDDEN_FIELDS:
            self.assertNotIn(key, aftersale)

    def test_order_columns_whitelist_frozen(self):
        """入库列集合 = 现有列，一个不加；且与 PII 禁存清单不相交。"""
        from bi_agent.sync import PII_FORBIDDEN_FIELDS, _ORDER_COLUMNS
        columns = {part.strip() for part in _ORDER_COLUMNS.split(",")}
        self.assertEqual(columns, {
            "shop_id", "erp_id", "commercial_ids", "split_parent_id", "source",
            "source_updated_at", "platform_modified_at", "paid_at", "raw_pay_amount",
            "raw_payment", "raw_platform_payment", "raw_cost", "raw_gross_profit",
            "unified_status", "system_status",
            "active", "normalization_status", "batch_id"})
        self.assertFalse(columns & PII_FORBIDDEN_FIELDS)


class ControlledSqlAgentIntegrationTests(unittest.TestCase):
    """主 Agent 的受控探索接线（计划 Task 5 Step 7）。

    这里只钉主层该钉的四件事：
      1. `CONTROLLED_SQL_ENABLED` 关着（默认）时 Tool 列表逐字不变，而且根本不进门禁；
      2. 开着时只在「固定 Tool 表达不了、不澄清、无未注册概念、服务端作用域就绪」
         四项都过的那一轮才多一项；
      3. 一次 Tool 调用 = 一个 `DomainContext` = 一次图执行：不逐店循环（开发流程 §4.6），
         提问由服务端注入，回到模型的那一句里没有 SQL 也没有真店号；
      4. 固定 Tool 仍然是权威入口：它本轮就会返回 unavailable/missing_data，也不因此
         众开探索入入口（不能拿固定口径的拒答当“换个工具再试一次”）。

    图自己的九道门与真库执行由 `tests.test_exploration` / `tests.test_runtime_db` 钉。
    """

    NOW = AgentTests.NOW
    EXPLORATION_NAMES = ["query_business", "analyze_product_performance",
                         "compare_performance", "audit_listing_prices",
                         "inspect_inventory", "evaluate_promotion"]
    # 已发布目录下的真实行为（不靠 mock 检索）："按天看退款金额" 的指标与分组粒度
    # 恰好是 query_business 能表达的 → 不开放探索；"各平台销量对比" 没有一份固定契约
    # 能同时承载这些指标与粒度 → 才开放。提问刻意避开主层的销售额口径澄清分支。
    FIXED_QUESTION = "按天看退款金额"
    EXPLORABLE_QUESTION = "各平台销量对比"

    def setUp(self):
        from bi_agent.catalog import ref_for_key
        from bi_agent.runtime.memory import MemoryQueryRunStore

        self.shop_id = "S1"
        self.shop_ref = ref_for_key("shop", "S1")
        self.run_store = MemoryQueryRunStore(forbidden_values={"S1", "ERP-P-9"})

    def _conn(self):
        return ShopCatalogConn([(self.shop_id, "店铺A")])

    def _tools_sent(self, model):
        return model.complete.call_args_list[0].args[1]

    def _turn(self, question, *, enabled, model=None, calls=None, allowed=None,
              run_store=None):
        from bi_agent.agent import SessionState, answer

        model = Mock() if model is None else model
        model.complete.side_effect = calls or [_reply(text="好")]
        turn = answer(question, SessionState(subject="u1"), model=model,
                      conn=self._conn(),
                      allowed_shop_ids=(frozenset({self.shop_id})
                                        if allowed is None else allowed),
                      now=self.NOW, run_store=self.run_store if run_store is None else run_store,
                      controlled_sql_enabled=enabled)
        return turn, model

    # --- 1) 门禁关着：逐字不变 ---------------------------------------------------

    def test_disabled_gate_leaves_the_tool_snapshot_byte_identical(self):
        import json

        from bi_agent.agent import _tool_schemas

        baseline = json.dumps(_tool_schemas(), ensure_ascii=False, sort_keys=True)
        with patch("bi_agent.agent._exploration_gate") as gate:
            _turn, model = self._turn(self.EXPLORABLE_QUESTION, enabled=False,
                                      calls=[_reply(text="不用查")])
        gate.assert_not_called()
        sent = json.dumps(self._tools_sent(model), ensure_ascii=False, sort_keys=True)
        self.assertEqual(sent, baseline, "feature off 时送给模型的 Tool 列表必须逐字相同")
        self.assertEqual([item["function"]["name"] for item in self._tools_sent(model)],
                         self.EXPLORATION_NAMES)

    def test_disabled_gate_also_keeps_unknown_tool_text_unchanged(self):
        from bi_agent.llm import ToolCall

        _turn, model = self._turn("查点什么", enabled=False, calls=[
            _reply(calls=[ToolCall(id="c1", name="run_sql",
                                   arguments={"sql": "SELECT 1"})]),
            _reply(text="只能用已公工具")])
        detail = next(message.content for message in
                      [m for m in (turn.content for turn in [])] if False) \
            if False else model.complete.call_args_list[1]
        sent_messages = detail.args[0]
        tool_message = next(message for message in sent_messages if message.role == "tool")
        self.assertIn("unknown_tool", tool_message.content)
        self.assertNotIn("explore_business_data", tool_message.content)
        for name in self.EXPLORATION_NAMES:
            self.assertIn(name, tool_message.content)

    # --- 2) 门禁开着：四项都过才多一项 -----------------------------------------

    def test_enabled_gate_adds_the_tool_only_when_no_fixed_tool_expresses_it(self):
        from bi_agent.agent import _exploration_gate

        entry, versions = _exploration_gate(
            self.EXPLORABLE_QUESTION, allowed_shop_ids=frozenset({self.shop_id}),
            shop_refs={self.shop_id: self.shop_ref})
        self.assertIsNotNone(entry)
        self.assertEqual(entry["function"]["name"], "explore_business_data")
        self.assertEqual(versions.data_catalog_version, 0)
        refused, _none = _exploration_gate(
            self.FIXED_QUESTION, allowed_shop_ids=frozenset({self.shop_id}),
            shop_refs={self.shop_id: self.shop_ref})
        self.assertIsNone(refused, "固定 Tool 能表达的问题不得改走 SQL")

    def test_enabled_gate_needs_a_ready_server_scope(self):
        from bi_agent.agent import _exploration_gate

        for label, kwargs in (("空授权", {"allowed_shop_ids": frozenset(),
                                        "shop_refs": {}}),
                             ("缺引用", {"allowed_shop_ids": frozenset({"S1", "S9"}),
                                       "shop_refs": {"S1": self.shop_ref}})):
            with self.subTest(case=label):
                entry, versions = _exploration_gate(self.EXPLORABLE_QUESTION, **kwargs)
                self.assertIsNone(entry)
                self.assertIsNone(versions)

    def test_enabled_gate_refuses_clarification_and_missing_concepts(self):
        from bi_agent.agent import _exploration_gate
        from tests.test_exploration import selection_for

        for label, kwargs in (("要澄清", {"requires_clarification": True}),
                             ("未注册概念", {"missing_concepts": ("profit_grain",)}),
                             ("没指标", {"selected_metrics": []}),
                             ("没候选视图", {"view_refs": ()})):
            with self.subTest(case=label):
                selection = selection_for(["metric-sales-amount"], **kwargs)
                with patch("bi_agent.exploration.tool.retrieve_schema_candidates",
                           return_value=selection) as retrieve:
                    entry, versions = _exploration_gate(
                        self.EXPLORABLE_QUESTION,
                        allowed_shop_ids=frozenset({self.shop_id}),
                        shop_refs={self.shop_id: self.shop_ref})
                self.assertIsNone(entry)
                self.assertIsNone(versions)
                retrieve.assert_called_once()

    def test_enabled_turn_appends_the_exploration_schema_last(self):
        _turn, model = self._turn(self.EXPLORABLE_QUESTION, enabled=True,
                                  calls=[_reply(text="先不查")])
        names = [item["function"]["name"] for item in self._tools_sent(model)]
        self.assertEqual(names, self.EXPLORATION_NAMES + ["explore_business_data"])
        self.assertEqual([item["function"]["name"] for item in self._tools_sent(model)[:6]],
                         self.EXPLORATION_NAMES, "前六项顺序与描述不得变")

    def test_enabled_turn_keeps_the_six_tools_when_exploration_is_refused(self):
        _turn, model = self._turn(self.FIXED_QUESTION, enabled=True,
                                  calls=[_reply(text="先不查")])
        self.assertEqual([item["function"]["name"] for item in self._tools_sent(model)],
                         self.EXPLORATION_NAMES)

    # --- 3) Tool 调用：一个上下文、一次图、不泄露 SQL ---------------------

    def test_exploration_call_uses_one_context_one_graph_and_leaks_nothing(self):
        from bi_agent.exploration.graph import ExplorationExecution
        from bi_agent.llm import ToolCall
        from uuid import uuid4
        from bi_agent.runtime.models import (ArtifactRef, DomainArtifact, DomainResult,
                                             DomainStatus)
        from tests.test_exploration import (EXPLORATION_PAYLOAD_KEYS, exploration_payload)

        payload = exploration_payload()
        artifact_id = uuid4()
        result = DomainResult(
            run_id=uuid4(), status=DomainStatus.SUCCESS,
            model_payload=payload,
            artifacts=[DomainArtifact(
                ref=ArtifactRef(id=artifact_id, type="exploration_result"),
                public_payload=payload)])
        captured: list = []

        def fake_tool(call, context, **kwargs):
            captured.append((call, context, kwargs))
            return ExplorationExecution(domain_result=result, plan=None)

        with patch("bi_agent.exploration.tool.execute_exploration_tool",
                   side_effect=fake_tool):
            turn, model = self._turn(
                self.EXPLORABLE_QUESTION, enabled=True, calls=[
                    _reply(calls=[ToolCall(
                        id="call_x", name="explore_business_data",
                        arguments={"requested_metric_refs": ["metric-sales-amount"],
                                   "question": "自己写的提问"})]),
                    _reply(text="按引用给了结果")])
        self.assertEqual(len(captured), 1, "一次 Tool 调用只能跑一次图")
        _call, context, kwargs = captured[0]
        self.assertEqual(kwargs["question"], self.EXPLORABLE_QUESTION,
                         "提问只能取服务端当前那句")
        self.assertEqual(context.allowed_shop_ids, frozenset({self.shop_id}))
        self.assertEqual(context.shop_refs, {self.shop_id: self.shop_ref})
        self.assertEqual(len(turn.artifacts), 1)
        self.assertEqual(set(turn.artifacts[0]), EXPLORATION_PAYLOAD_KEYS | {"artifact_id",
                                                                          "artifact_type"})
        tool_message = next(message for message in
                            model.complete.call_args_list[1].args[0]
                            if message.role == "tool")
        for leak in ("SELECT", "ANY(", '"S1"', "parameters", "sql_text"):
            self.assertNotIn(leak, tool_message.content, leak)
        self.assertNotIn(self.shop_id, tool_message.content)
        # 回给模型的只有稳定引用与列 ref：内容非空才算"这条通道通了"（反恒真）。
        self.assertIn("metric-cost-total", tool_message.content)
        self.assertIn("shop-ref", tool_message.content)
        self.assertEqual(turn.error_code, None)

    def test_invented_exploration_call_when_not_offered_stays_unknown(self):
        from bi_agent.llm import ToolCall

        with patch("bi_agent.exploration.tool.execute_exploration_tool") as tool:
            _turn, model = self._turn(self.FIXED_QUESTION, enabled=True, calls=[
                _reply(calls=[ToolCall(id="c1", name="explore_business_data",
                                       arguments={"requested_metric_refs": []})]),
                _reply(text="换固定工具回答")])
        tool.assert_not_called()
        detail = next(message.content for message in model.complete.call_args_list[1].args[0]
                      if message.role == "tool")
        self.assertIn("unknown_tool", detail)
        self.assertNotIn("SELECT", detail)

    def test_exploration_needs_input_gets_one_correction_then_stops(self):
        from bi_agent.agent import SessionState, answer
        from bi_agent.llm import ToolCall
        from uuid import uuid4
        from bi_agent.runtime.models import DomainResult, DomainStatus, ErrorEnvelope
        from bi_agent.exploration.graph import ExplorationExecution

        refusal = DomainResult(
            run_id=uuid4(), status=DomainStatus.NEEDS_INPUT,
            model_payload={"status": "invalid_parameters", "termination_reason":
                           "schema_ambiguous", "limitations": []},
            artifacts=[], error=ErrorEnvelope(
                code="invalid_parameters", stage="select_schema", retryable=False,
                recovery="correct_parameters", public_message="查询参数无效，请调整后重试。",
                problems=["invalid_parameters"]))
        calls_seen: list = []

        with patch("bi_agent.exploration.tool.execute_exploration_tool",
                   return_value=ExplorationExecution(domain_result=refusal,
                                                     plan=None)) as tool:
            model = Mock()
            bad = ToolCall(id="c1", name="explore_business_data",
                           arguments={"requested_metric_refs": ["SUM(x)"]})
            model.complete.side_effect = [
                _reply(calls=[bad]), _reply(calls=[bad]), _reply(text="已问清")]
            turn = answer(self.EXPLORABLE_QUESTION, SessionState(subject="u1"), model=model,
                          conn=self._conn(), allowed_shop_ids=frozenset({self.shop_id}),
                          now=self.NOW, run_store=self.run_store,
                          controlled_sql_enabled=True)
            calls_seen = tool.call_args_list
        self.assertEqual(len(calls_seen), 2, "第二次非法参数就该停，不是无限重试")
        self.assertEqual(turn.results, [])
        self.assertEqual(turn.artifacts, [])
        self.assertIn("参数两次非法", turn.text or "")

    def test_exploration_persistence_failure_voids_the_turn(self):
        from bi_agent.exploration.graph import ExplorationExecution
        from uuid import uuid4
        from bi_agent.llm import ToolCall
        from bi_agent.runtime.models import DomainResult, DomainStatus, ErrorEnvelope

        failed = DomainResult(
            run_id=uuid4(), status=DomainStatus.FAILED,
            model_payload={"status": "unavailable", "termination_reason":
                           "persistence_failed", "limitations": []},
            artifacts=[], error=ErrorEnvelope(
                code="artifact_persistence_failed", stage="finalize", retryable=True,
                recovery="retry_later", public_message="结果保存失败，请稍后重试。",
                problems=["artifact_persistence_failed"]))
        with patch("bi_agent.exploration.tool.execute_exploration_tool",
                   return_value=ExplorationExecution(domain_result=failed, plan=None)), \
                patch("bi_agent.business_query.nodes.metrics.query_business",
                      return_value=AgentTests.KNOWN):
            turn, model = self._turn(self.EXPLORABLE_QUESTION, enabled=True, calls=[
                _reply(calls=[ToolCall(id="c1", name="query_business",
                                       arguments={"start": "2026-09-01", "end": "2026-09-08",
                                                  "shop_ids": [self.shop_ref],
                                                  "metrics": ["paid_amount"]})]),
                _reply(calls=[ToolCall(id="c2", name="explore_business_data",
                                       arguments={"requested_metric_refs":
                                                   ["metric-sales-amount"]})]),
                _reply(text="收尾")])
        self.assertEqual(turn.results, [], "存不下的探索不得与已有结果共存")
        self.assertEqual(turn.artifacts, [])
        self.assertEqual(turn.error_code, "artifact_persistence_failed")
        self.assertNotIn("SELECT", turn.text or "")

    # --- 4) 固定 Tool 仍然权威 -------------------------------------------------

    def test_fixed_tool_stays_authoritative_even_when_it_returns_unavailable(self):
        from bi_agent.llm import ToolCall
        from bi_agent.metrics import ToolResult
        from bi_agent.runtime.models import Coverage as _RuntimeCoverage

        unavailable = ToolResult(
            status="unavailable", data=[], limitations=["来源质量核验未通过，拒绍出数"],
            coverage=_RuntimeCoverage(status="missing", start=self.NOW.date(),
                                     end=self.NOW.date(), gaps=[]))
        with patch("bi_agent.business_query.nodes.metrics.query_business",
                   return_value=unavailable) as query:
            turn, model = self._turn(self.FIXED_QUESTION, enabled=True, calls=[
                _reply(calls=[ToolCall(id="c1", name="query_business",
                                       arguments={"start": "2026-09-01",
                                                  "end": "2026-09-08",
                                                  "shop_ids": [self.shop_ref],
                                                  "metrics": ["refund_amount"]})]),
                _reply(text="今天不能出数")])
            names = [item["function"]["name"] for item in self._tools_sent(model)]
        query.assert_called_once()
        self.assertEqual(names, self.EXPLORATION_NAMES,
                         "固定 Tool 拒答不得换一条 SQL 重问")
        self.assertNotIn("explore_business_data", str(turn.state.turns))

    def test_non_exploration_paths_are_unchanged_when_the_gate_is_open(self):
        from tests.test_exploration import EXPLORATION_ARTIFACT_TYPE  # noqa: F401
        from bi_agent.llm import ToolCall

        with patch("bi_agent.business_query.nodes.metrics.query_business",
                   return_value=AgentTests.KNOWN) as query:
            turn, model = self._turn("最近7天店铺A的支付金额", enabled=True, calls=[
                _reply(calls=[ToolCall(id="c1", name="query_business",
                                       arguments={"start": "2026-09-01",
                                                  "end": "2026-09-08",
                                                  "shop_ids": [self.shop_ref],
                                                  "metrics": ["paid_amount"]})]),
                _reply(text="支付金额1000元")])
        query.assert_called_once()
        self.assertEqual(len(turn.results), 1)
        self.assertEqual(len(self.run_store.runs), 1)
        self.assertEqual(len(self.run_store.artifacts), 1)
        self.assertNotIn(EXPLORATION_ARTIFACT_TYPE, str(turn.artifacts))


class QueryMemoryAgentIntegrationTests(unittest.TestCase):
    """approved 记忆路由接入（计划 approved-query-memory Task 5）：主层消息面、
    预算与历史契约的集成面。样例投影/检索合并/零写入的结构性证据在
    tests.test_query_memory.QueryMemoryRoutingTests，这里只钉主层编排。"""

    NOW = AgentTests.NOW

    def setUp(self):
        from bi_agent.runtime.memory import MemoryQueryRunStore

        self.run_store = MemoryQueryRunStore(forbidden_values={"S1"})

    def _memory_example(self, ref):
        from bi_agent.query_memory.models import ApprovedExample, QuerySlot
        from bi_agent.runtime.versions import VersionSet
        from tests.fakeconn import memory_versions_dict

        return ApprovedExample(
            example_ref=ref, domain="business_query",
            intent_signature="paid-amount-summary",
            question_template="汇总 {shop_scope} 的支付金额",
            slots=(QuerySlot(name="shop_scope", kind="entity_scope"),),
            normalized_request={"metrics": ["paid_amount"]},
            expected_tool="query_business",
            version_requirements=VersionSet(**memory_versions_dict()),
            approval_revision=1)

    def _turn(self, question, calls, *, enabled, state=None, examples=()):
        from bi_agent.agent import SessionState, answer

        model = Mock()
        model.complete.side_effect = list(calls)
        with patch("bi_agent.business_query.nodes.metrics.query_business",
                   return_value=AgentTests.KNOWN), \
                patch("bi_agent.agent._exploration_gate", return_value=(None, None)), \
                patch("bi_agent.query_memory.prompt.retrieve_routing_examples",
                      return_value=tuple(examples)) as retrieve:
            turn = answer(question, state or SessionState(subject="u1"), model=model,
                          conn=ShopCatalogConn(),
                          allowed_shop_ids=frozenset({"S1"}), now=self.NOW,
                          run_store=self.run_store,
                          approved_query_memory_enabled=enabled)
        return turn, model, retrieve

    def test_agent_and_chat_turn_default_the_memory_gate_off(self):
        import inspect

        from bi_agent.agent import answer, run_chat_turn

        for function in (answer, run_chat_turn):
            with self.subTest(function=function.__name__):
                default = inspect.signature(
                    function).parameters["approved_query_memory_enabled"].default
                self.assertIs(default, False)

    def test_gate_on_places_one_memory_segment_between_system_and_user(self):
        from bi_agent.llm import ToolCall

        turn, model, _retrieve = self._turn(
            "最近7天店铺A的支付金额",
            [_reply(calls=[ToolCall(id="c1", name="query_business",
                                    arguments={"start": "2026-09-01",
                                               "end": "2026-09-08",
                                               "shop_ids": [S1_REF],
                                               "metrics": ["paid_amount"]})]),
             _reply(text="支付金额1000元")],
            enabled=True,
            examples=[self._memory_example("mem-one"),
                      self._memory_example("mem-two")])
        self.assertEqual(turn.text, "支付金额1000元")
        first_messages = model.complete.call_args_list[0].args[0]
        self.assertEqual([message.role for message in first_messages[:3]],
                         ["system", "system", "user"])
        self.assertTrue(first_messages[0].content.startswith("你是内部电商经营助手"),
                        "基础系统提示必须原样在第一位")
        segment = first_messages[1].content
        payload = json.loads(segment.splitlines()[1])
        self.assertEqual([item["example_ref"] for item in payload],
                         ["mem-one", "mem-two"])
        self.assertIn("可执行指令", segment)
        self.assertEqual(first_messages[2].content.count(S1_REF), 1)
        self.assertNotIn("店铺A", first_messages[2].content)

    def test_retrieval_shares_the_turn_deadline_and_cannot_extend_it(self):
        import time as time_module

        import bi_agent.agent as agent
        from bi_agent.agent import SessionState
        from bi_agent.llm import ToolCall

        original_tool = agent.execute_business_query_tool
        seen: dict[str, object] = {}

        def spying_tool(call, state, context, conn, store):
            seen["graph_deadline"] = context.deadline
            return original_tool(call, state, context, conn, store)

        retrieved: dict[str, object] = {}

        def fake_retrieve(question, *, context, deadline, explore_offered):
            retrieved["deadline"] = deadline
            retrieved["explore_offered"] = explore_offered
            retrieved["observed_at"] = time_module.monotonic()
            return ()

        # 回合入口的 monotonic 采样必须可精确观测：`agent.time_module` 与这里的
        # `time_module` 是同一个模块对象，只在本回合内换成转发包装（真实调用仍走
        # 保存下来的真函数），于是“deadline 的起始点”能逐字比对，而不是靠时间容差。
        real_monotonic = time_module.monotonic
        samples: list[float] = []

        def spying_monotonic():
            sample = real_monotonic()
            samples.append(sample)
            return sample

        model = Mock()
        model.complete.side_effect = [
            _reply(calls=[ToolCall(id="c1", name="query_business",
                                   arguments={"start": "2026-09-01",
                                              "end": "2026-09-08",
                                              "shop_ids": [S1_REF],
                                              "metrics": ["paid_amount"]})]),
            _reply(text="支付金额1000元")]
        with patch.object(agent.time_module, "monotonic", spying_monotonic), \
                patch.object(agent, "execute_business_query_tool", spying_tool), \
                patch("bi_agent.query_memory.prompt.retrieve_routing_examples",
                      fake_retrieve), \
                patch("bi_agent.business_query.nodes.metrics.query_business",
                      return_value=AgentTests.KNOWN), \
                patch("bi_agent.agent._exploration_gate", return_value=(None, None)):
            turn = agent.answer(
                "最近7天店铺A的支付金额", SessionState(subject="u1"), model=model,
                conn=ShopCatalogConn(), allowed_shop_ids=frozenset({"S1"}),
                now=self.NOW, run_store=self.run_store,
                approved_query_memory_enabled=True)
            returned_at = spying_monotonic()
        self.assertEqual(turn.text, "支付金额1000元")
        self.assertIs(retrieved["explore_offered"], False)
        # 检索看到的就是回合入口那份 30 秒预算：deadline 精确等于回合入口第一个
        # monotonic 采样 + 30 秒。重置换算或延长都会让这个等式不成立。
        self.assertEqual(retrieved["deadline"],
                         samples[0] + agent.TOTAL_BUDGET_SECONDS)
        # 仍然落在未来：检索当下与回合返回时都不能是过去/零值。
        self.assertGreater(retrieved["deadline"], retrieved["observed_at"])
        self.assertGreater(retrieved["deadline"], returned_at)
        # 图拿到的是同一个绝对时刻：float 相等，不是“差不多”。
        self.assertEqual(seen["graph_deadline"], retrieved["deadline"])

    def test_follow_up_turn_gets_one_fresh_segment_not_a_stale_copy(self):
        from bi_agent.llm import ToolCall

        tool_call = _reply(calls=[ToolCall(
            id="c1", name="query_business",
            arguments={"start": "2026-09-01", "end": "2026-09-08",
                       "shop_ids": [S1_REF], "metrics": ["paid_amount"]})])
        first, _model, _retrieve = self._turn(
            "最近7天店铺A的支付金额", [tool_call, _reply(text="支付金额1000元")],
            enabled=True, examples=[self._memory_example("mem-first")])
        second, model, _retrieve = self._turn(
            "最近7天店铺A的支付金额", [tool_call, _reply(text="支付金额1000元")],
            enabled=True, state=first.state,
            examples=[self._memory_example("mem-second")])
        self.assertEqual(second.text, "支付金额1000元")
        first_messages = model.complete.call_args_list[0].args[0]
        system_messages = [message for message in first_messages
                           if message.role == "system"]
        self.assertEqual(len(system_messages), 2,
                         "新回合只带一段新鲜记忆段，不带上一轮的旧段")
        payload = json.loads(system_messages[1].content.splitlines()[1])
        self.assertEqual([item["example_ref"] for item in payload], ["mem-second"])
        self.assertNotIn("mem-first", system_messages[1].content)


class IsolatedAnalysisAgentIntegrationTests(unittest.TestCase):
    """主 Agent 的隔离分析接线（计划 isolated-analysis Task 5 Step 5–6）。

    这里只钉主层该钉的五件事：
      1. `ISOLATED_ANALYSIS_ENABLED` 关着（默认）时 Tool 列表逐字不变；
      2. 开着时只追加一个只读 `analyze_artifact`，六份固定 Tool 的顺序与描述不变；
      3. Tool schema 只有 artifact_ref 与 analysis_kinds，问题/SQL/店铺主键/数据行
         都没有入口；
      4. 一次 Tool 调用 = 一次图执行，同轮第二次分析被拒；上下文里的授权与
         deadline 全部由服务端注入；
      5. 分析 Artifact 的载荷（findings/narrative/limitations）回到模型与展示层，
         模型侧永远没有 ERP 主键。

    图内部的六道门、授权拒绝与 fail-closed 持久化由 tests.test_analysis 钉；
    Postgres 侧的读取语义由 tests.test_runtime_db 钉。
    """

    NOW = AgentTests.NOW

    def setUp(self):
        from bi_agent.runtime.memory import MemoryQueryRunStore

        self.run_store = MemoryQueryRunStore(forbidden_values={"S1"})

    def _conn(self):
        return ShopCatalogConn([("S1", "店铺A")])

    def _seed_source_artifact(self):
        """在内存 Store 里落一份可分析的来源 Artifact（成功运行 + 真血缘）。"""
        from bi_agent.analysis.loader import load_analysis_dataset
        from bi_agent.commerce.models import DomainContext
        from bi_agent.runtime.artifacts import QueryProvenance
        from bi_agent.runtime.models import (NewArtifact, NewQueryRun,
                                             RunCompletion, RunStatus)
        from uuid import uuid4
        payload = {"status": "ok", "metric_definition": {},
                   "coverage": {"status": "complete", "start": "2026-09-01",
                                "end": "2026-09-08", "gaps": []},
                   "limitations": [], "data_as_of": None, "filters": {},
                   "data": [{"shop_ref": S1_REF, "day": "2026-09-01",
                             "paid_amount": "12.30", "paid_orders": 3}]}
        run_id = self.run_store.create_run(NewQueryRun(
            chat_id=uuid4(), user_message_id=uuid4(), subject_id="u1",
            tool_call_id="seed", domain="business_query", attempt_no=1,
            normalized_request={"shop_refs": [S1_REF]},
            provenance=QueryProvenance(quality_rule=QUALITY_RULE),
            state={"node": "persist_artifact", "status": "running",
                   "revision": 0,
                   "normalized_request": {"shop_refs": [S1_REF]}}))
        ref = self.run_store.save_artifact(run_id, NewArtifact(
            artifact_type="metric_result", payload=payload,
            coverage=payload["coverage"]))
        self.run_store.finish(run_id, RunCompletion(
            expected_revision=0, node="finalize", status=RunStatus.SUCCEEDED,
            state={"node": "finalize", "status": "succeeded", "revision": 1,
                   "run_id": str(run_id),
                   "normalized_request": {"shop_refs": [S1_REF]}},
            payload={"tool_status": "ok", "target_status": "success"},
            termination_reason="succeeded"))
        return ref

    def _tools_sent(self, model):
        return model.complete.call_args_list[0].args[1]

    def _turn(self, question, calls, *, enabled, allowed=None):
        from bi_agent.agent import SessionState, answer

        model = Mock()
        model.complete.side_effect = list(calls)
        turn = answer(question, SessionState(subject="u1"), model=model,
                      conn=self._conn(),
                      allowed_shop_ids=(frozenset({"S1"}) if allowed is None
                                        else allowed),
                      now=self.NOW, run_store=self.run_store,
                      isolated_analysis_enabled=enabled)
        return turn, model

    # -- 1) 门禁关着：逐字不变 ---------------------------------------------

    def test_disabled_gate_leaves_the_tool_snapshot_byte_identical(self):
        from bi_agent.agent import _tool_schemas

        baseline = json.dumps(_tool_schemas(), ensure_ascii=False, sort_keys=True)
        _turn, model = self._turn("最近7天店铺A的支付金额",
                                  [_reply(text="直接回答")], enabled=False)
        sent = json.dumps(self._tools_sent(model), ensure_ascii=False,
                          sort_keys=True)
        self.assertEqual(sent, baseline, "feature off 时送给模型的 Tool 列表必须逐字相同")

    def test_disabled_gate_keeps_a_forged_analysis_call_unknown(self):
        from bi_agent.agent import _tool_schemas

        _turn, model = self._turn("分析一下", [
            _reply(calls=[ToolCall(id="c1", name="analyze_artifact",
                                   arguments={"artifact_ref": "0" * 8,
                                              "analysis_kinds": ["contribution"]})]),
            _reply(text="没有这个工具")], enabled=False)
        sent_messages = model.complete.call_args_list[1].args[0]
        tool_message = next(message for message in sent_messages
                            if message.role == "tool")
        self.assertIn("unknown_tool", tool_message.content,
                      "没公告的 Tool 不跑：伪造的名字拿不到入口")
        self.assertEqual([run for run in self.run_store.runs.values()
                          if run["domain"] == "isolated_analysis"], [],
                         "门禁关着时不建任何分析运行")

    # -- 2) 门禁开着：只追加一个只读 Tool ---------------------------------

    def test_enabled_gate_appends_exactly_one_readonly_tool_last(self):
        from bi_agent.agent import _tool_schemas

        fixed_names = [item["function"]["name"] for item in _tool_schemas()]
        _turn, model = self._turn("最近7天店铺A的支付金额",
                                  [_reply(text="先不分析")], enabled=True)
        tools = self._tools_sent(model)
        self.assertEqual([item["function"]["name"] for item in tools],
                         fixed_names + ["analyze_artifact"])
        schema = tools[-1]["function"]["parameters"]
        self.assertEqual(schema["properties"].keys(),
                         {"artifact_ref", "analysis_kinds"})
        self.assertIs(schema["additionalProperties"], False,
                      "schema 不给任何附加键留入口")
        forbidden = ("question", "sql", "shop_id", "shop_ids",
                     "allowed_shop_ids", "data", "rows", "tools")
        blob = json.dumps(tools[-1], ensure_ascii=False)
        for key in forbidden:
            self.assertNotIn(f'"{key}"', blob)

    def test_analysis_kinds_enum_matches_the_contract(self):
        from bi_agent.analysis.tool import analysis_request_schema

        schema = analysis_request_schema()
        self.assertEqual(schema["properties"]["analysis_kinds"]["items"]["enum"],
                         ["contribution", "change_decomposition",
                          "anomaly_candidates", "followups"])
        self.assertEqual(schema["properties"]["analysis_kinds"]["maxItems"], 8)

    def _finding_ref_of_source(self, ref) -> str:
        """用真 loader + 真计算学出确定性 finding_ref，叙事才能真的发布。"""
        import time as time_module
        from uuid import uuid4

        from bi_agent.analysis.calculations import compute_findings
        from bi_agent.analysis.loader import load_analysis_dataset
        from bi_agent.commerce.models import DomainContext

        dataset = load_analysis_dataset(str(ref.id), context=DomainContext(
            subject_id="u1", allowed_shop_ids=frozenset({"S1"}),
            shop_refs={"S1": S1_REF}, conn=self._conn(), store=self.run_store,
            chat_id=uuid4(), user_message_id=uuid4(), root_request_id=uuid4(),
            now=self.NOW, deadline=time_module.monotonic() + 30))
        return compute_findings(dataset, ("contribution",))[0].finding_ref

    # -- 3) Tool 调用：一个上下文、一次图、同轮一次 -----------------------

    def _analysis_call(self, ref, call_id="c1"):
        return ToolCall(id=call_id, name="analyze_artifact",
                        arguments={"artifact_ref": str(ref),
                                   "analysis_kinds": ["contribution"]})

    def test_one_analysis_call_runs_the_graph_once_and_persists_one_artifact(self):
        ref = self._seed_source_artifact()
        finding_ref = self._finding_ref_of_source(ref)
        narrative = json.dumps({"narrative": [{
            "text": "paid_amount 的值为 12.30，贡献占比 1.000000。",
            "finding_refs": [finding_ref], "claim_kind": "observation"}]},
            ensure_ascii=False)
        turn, model = self._turn("分析一下这份结果", [
            _reply(calls=[self._analysis_call(ref.id)]),
            _reply(text=narrative),          # 主层模型兼任叙事总结：同一份契约
            _reply(text="分析见下")], enabled=True)
        self.assertIsNone(turn.error_code)
        analysis_runs = [run for run in self.run_store.runs.values()
                         if run["domain"] == "isolated_analysis"]
        self.assertEqual(len(analysis_runs), 1, "一次工具调用只建一条运行")
        self.assertEqual(analysis_runs[0]["status"], "succeeded")
        artifacts = [item for item in turn.artifacts
                     if item["artifact_type"] == "analysis_result"]
        self.assertEqual(len(artifacts), 1)
        self.assertTrue(artifacts[0]["findings"])
        self.assertEqual(artifacts[0]["narrative"][0]["claim_kind"], "observation")
        self.assertNotIn("narrative_unavailable", artifacts[0]["limitations"])
        # 叙事那次模型调用的 tools 恒为空列表（与主层调用区分开）。
        narrative_tools = model.complete.call_args_list[1].args[1]
        self.assertEqual(narrative_tools, [])
        # 载荷回到模型：findings 在工具消息里，ERP 主键不在。
        tool_messages = [message for message in turn.state.turns
                         if message.role == "tool"]
        model_side = tool_messages[-1].content or ""
        self.assertIn("finding_ref", model_side)
        self.assertNotIn("S1", model_side, "ERP 主键不进模型历史")

    def test_second_analysis_call_in_the_same_turn_is_refused(self):
        ref = self._seed_source_artifact()
        turn, _model = self._turn("分析两次", [
            _reply(calls=[self._analysis_call(ref.id, "c1"),
                          self._analysis_call(ref.id, "c2")]),
            _reply(text="{\"narrative\": []}"),
            _reply(text="分析见下")], enabled=True)
        analysis_runs = [run for run in self.run_store.runs.values()
                         if run["domain"] == "isolated_analysis"]
        self.assertEqual(len(analysis_runs), 1, "同轮第二次分析不建运行")
        tool_messages = [message for message in turn.state.turns
                         if message.role == "tool"]
        self.assertIn("duplicate_analysis", tool_messages[-1].content or "")

    def test_analysis_arguments_are_server_resolved_not_model_trusted(self):
        from bi_agent.analysis import tool as analysis_tool
        from bi_agent.llm import ToolCall

        ref = self._seed_source_artifact()
        forged = ToolCall(id="c1", name="analyze_artifact", arguments={
            "artifact_ref": str(ref.id), "analysis_kinds": ["contribution"],
            "shop_ids": ["S1"]})
        turn, _model = self._turn("带着越权参数分析", [
            _reply(calls=[forged]), _reply(text="参数无效")], enabled=True)
        analysis_runs = [run for run in self.run_store.runs.values()
                         if run["domain"] == "isolated_analysis"]
        self.assertEqual(len(analysis_runs), 1)
        self.assertEqual(analysis_runs[0]["status"], "needs_input",
                         "服务端专有键出现即 needs_input，不执行图")
        self.assertEqual(analysis_runs[0]["termination_reason"],
                         "invalid_parameters")
        self.assertEqual([item for item in turn.artifacts
                          if item["artifact_type"] == "analysis_result"], [])

    def test_unknown_artifact_ref_asks_instead_of_leaking(self):
        from uuid import uuid4

        missing = uuid4()
        turn, _model = self._turn("分析不存在的结果", [
            _reply(calls=[self._analysis_call(missing)]),
            _reply(text="没有找到")], enabled=True)
        self.assertIsNone(turn.error_code)
        analysis_runs = [run for run in self.run_store.runs.values()
                         if run["domain"] == "isolated_analysis"]
        self.assertEqual(analysis_runs[0]["status"], "needs_input")
        tool_messages = [message for message in turn.state.turns
                         if message.role == "tool"]
        self.assertIn("invalid_parameters", tool_messages[-1].content or "")
        self.assertNotIn("analysis_source_not_found",
                         tool_messages[-1].content or "",
                         "映射后的稳定码之外不泄露 loader 原因")
        self.assertEqual(turn.artifacts, [])

    def test_model_failure_still_publishes_findings_artifact(self):
        from bi_agent.llm import ModelError

        ref = self._seed_source_artifact()
        turn, _model = self._turn("分析一下这份结果", [
            _reply(calls=[self._analysis_call(ref.id)]),
            ModelError("timeout"),                  # 叙事阶段失败：findings 保留
            _reply(text="分析见下")], enabled=True)
        artifacts = [item for item in turn.artifacts
                     if item["artifact_type"] == "analysis_result"]
        self.assertEqual(len(artifacts), 1)
        self.assertTrue(artifacts[0]["findings"])
        self.assertEqual(artifacts[0]["narrative"], [])
        self.assertIn("narrative_unavailable", artifacts[0]["limitations"])


if __name__ == "__main__":
    unittest.main()
