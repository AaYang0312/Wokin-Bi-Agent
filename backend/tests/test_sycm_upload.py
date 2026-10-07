"""M4 上传接口契约（计划 §3.M4，docs/superpowers/plans/2026-10-02-manual-sycm-export-import.md）。

锁六件事：
1) **gate**：SYCM_MANUAL_IMPORT_ENABLED 关（缺省）时路由 404（复用 query_memory
   的"路由常驻、依赖里判"门禁模式），且不建任何数据库连接；
2) **写面边界适配字节体**：X-BI-Agent: web、Origin == public_origin、认证主体
   （development 沿用 local-development 先例）；任一缺失 403 + 稳定原因码，
   且被拒请求不建连接、不产生任何数据库语句；
3) **请求形状**：application/octet-stream 原始字节体；文件名经查询参数或
   X-File-Name 头传递（查询参数优先），服务端消毒（长度上限、字符白名单、
   路径分隔/控制字符/Windows 保留设备名），不合格 422 稳定码；
4) **处理契约**：响应 JSON 恰好 6 键 {upload_id, status, reason_code, rows_read,
   rows_written, shops}；status/reason_code 用 M2 集中枚举（ReasonCode）；成功
   幂等（同窗重传台账按次追加、事实不变；不同内容同窗后批替换、batch lineage
   随内容哈希变化）；拒绝分支（非 xlsx/超限/未映射店铺/PII 列/损坏 zip）都返回
   同一契约形状并留台账（received/rejected 事件），拒绝路径不写任何事实表；
5) **写面身份 fail-closed**：gate 开启但数据库写面身份不可用
   （InsufficientPrivilege，例如 031 未应用）时给稳定 503 码，不泄漏数据库文本；
6) **031 迁移**：status/reason_code DB 级词表 CHECK（词表与 M2 枚举一致）反向
   突变；幂等重放；role_table_grants/role_column_grants 断言 bi_app 授权集精确
   （主会话批准偏差：任务书清单 + bi.shops SELECT + bi.sync_state
   SELECT/INSERT/UPDATE，其中 sync_state 以 RLS 限定 manual 来源族）；
   RLS 正反向突变（bi_app 只见/只写 manual.sycm.export 行，bi_sync 全通，
   bi_reader 经 v_coverage 的读取不回归）。

数据库测试全部在外层回滚事务内进行（connect_test_db 协议），共享测试库零
残留；无 BI_TEST_ADMIN_DSN 时显式 skip——skip 不是通过证明。迁移 031 在事务
内应用（GRANT/RLS 均事务性），重放两遍验证幂等。
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import io
import os
import unittest
import zipfile
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

import psycopg
from fastapi.testclient import TestClient
from pydantic import SecretStr

from tests.dbfixtures import connect_test_db
from tests.test_sycm_parser import (
    SHOP_HEADER,
    build_xlsx,
    crc_flip_first_member_byte,
    shop_rows,
)

APP_DSN = "postgresql://bi_app:app-secret@localhost/bi_agent_test"
UPLOAD_PATH = "/api/uploads/sycm"
ORIGIN = "https://bi.test"
SUBJECT = "local-development"

SHOP_ID = "SY-U"
SHOP_NAME = "上传测试淘店"
UNREGISTERED_SHOP = "元发美特钉枪直销店"
FILENAME = f"淘店-{SHOP_NAME}-店铺维度_20261001_ab12cd34.xlsx"
PII_COLUMN = "手机号"
FIVE_MB = 5 * 1024 * 1024

# 契约形状（M5 前端将按它渲染，键集精确）：
CONTRACT_FIELDS = {"upload_id", "status", "reason_code", "rows_read",
                   "rows_written", "shops"}

# 031 授予 bi_app 的精确授权集（含主会话批准偏差：任务书清单 + shops SELECT +
# sync_state SELECT/INSERT/UPDATE，sync_state 由 RLS 限定 manual 来源族）。
APP_TABLE_GRANTS = {
    "shops": {"SELECT"},
    "sycm_shop_daily": {"INSERT", "UPDATE"},
    "sycm_customer_daily": {"INSERT", "UPDATE"},
    "manual_uploads": {"INSERT"},
    "manual_upload_events": {"INSERT"},
    "sync_state": {"SELECT", "INSERT", "UPDATE"},
}
UPLOAD_TABLES = ("sycm_shop_daily", "sycm_customer_daily", "manual_uploads",
                 "manual_upload_events")


def _settings(**overrides):
    from bi_agent.config import AppSettings

    values = dict(
        app_dsn=SecretStr(APP_DSN),
        shop_ids=frozenset({"S1"}),
        environment="development",
        allowed_subjects=frozenset({"user-a"}),
        public_origin=ORIGIN,
        auth_subject_header="X-Auth-Request-Sub",
        sycm_manual_import_enabled=True,
    )
    values.update(overrides)
    return AppSettings(**values)


def patch_connect(side_effect):
    return patch("bi_agent.api.psycopg.connect", side_effect=side_effect)


class _NoStatementsConn:
    """替身连接：记录每条 SQL；任何语句执行都让测试当场失败。"""

    def __init__(self):
        self.executed: list[str] = []

    def __enter__(self):
        return self

    def __exit__(self, *_details):
        return False

    def close(self):
        pass

    def execute(self, sql, params=None):
        self.executed.append(" ".join(sql.split()))
        raise AssertionError(f"被拒的请求不得产生数据库语句：{sql[:80]}")


class _BrokenWriteConn:
    """写面身份不可用的替身：第一条语句就报 InsufficientPrivilege（031 缺席）。"""

    def __enter__(self):
        return self

    def __exit__(self, *_details):
        return False

    def execute(self, sql, params=None):
        raise psycopg.errors.InsufficientPrivilege(
            "permission denied for table shops")

    def transaction(self):
        return contextlib.nullcontext()


class _Rows:
    def __init__(self, rows):
        self._rows = list(rows)

    def fetchall(self):
        return list(self._rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None


class _RecordingConn:
    """替身连接：回答店铺登记读取，记录台账 INSERT 参数；白名单外报错。

    _insert_upload_ledger 的参数序：(upload_id, subject_id, filename,
    content_sha256, kind, status, reason_code, rows_read, rows_written,
    shops)——接受与拒绝两条路径共用这一列序。
    """

    def __init__(self):
        self.executed: list[str] = []
        self.uploads: list[dict] = []

    def __enter__(self):
        return self

    def __exit__(self, *_details):
        return False

    def close(self):
        pass

    def transaction(self):
        return contextlib.nullcontext()

    def execute(self, sql, params=None):
        text = " ".join(sql.split())
        self.executed.append(text)
        if text.startswith(
                "SELECT display_name, shop_id, platform FROM bi.shops"):
            return _Rows([(SHOP_NAME, SHOP_ID, "tb")])
        if text.startswith("INSERT INTO bi.manual_uploads"):
            self.uploads.append({
                "filename": params[2],
                "content_sha256": params[3],
                "kind": params[4],
                "status": params[5],
                "reason_code": params[6],
            })
            return _Rows([])
        if text.startswith("INSERT INTO bi.manual_upload_events"):
            return _Rows([])
        raise AssertionError(f"未预期的SQL：{text[:120]}")


def valid_shop_xlsx() -> bytes:
    return build_xlsx(shop_rows(1, shop=SHOP_NAME))


def not_xlsx_bytes() -> bytes:
    return b"this is not a zip file"


def crc_corrupted_xlsx() -> bytes:
    """真实损坏样本：翻转工作表成员压缩数据首字节（CRC/解压层损坏）。"""
    return crc_flip_first_member_byte(valid_shop_xlsx())


def truncated_xlsx() -> bytes:
    """真实损坏样本：截掉 zip 尾部（EOCD 缺失/流截断）。"""
    return valid_shop_xlsx()[:-40]


def pii_xlsx() -> bytes:
    rows = shop_rows(1, shop=SHOP_NAME)
    rows[0] = list(rows[0]) + [PII_COLUMN]
    rows[1] = list(rows[1]) + ["13800000000"]
    return build_xlsx(rows)


def unregistered_shop_xlsx() -> bytes:
    return build_xlsx(shop_rows(1, shop=UNREGISTERED_SHOP))


class TestTransactionConnection:
    """把 API 的每请求连接绑到本测试的外层回滚事务上（test_api 先例）。"""

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


class SycmUploadRouteTests(unittest.TestCase):
    """离线路由契约：gate、写面边界、文件名消毒、写面身份 fail-closed。"""

    @contextlib.contextmanager
    def _client(self, conn, *, settings=None):
        from bi_agent.api import create_app

        app = create_app(settings or _settings())
        calls = []

        def connect(dsn, **kwargs):
            calls.append((dsn, kwargs))
            return conn

        with patch_connect(connect):
            # create_app 时路由才挂上；patch 须覆盖整个请求生命周期。
            yield TestClient(app), calls

    @staticmethod
    def _headers(**extra):
        values = {"X-BI-Agent": "web", "Origin": ORIGIN,
                  "Content-Type": "application/octet-stream"}
        values.update(extra)
        return values

    def _post(self, client, *, filename=FILENAME, body=b"irrelevant",
              headers=None):
        params = {"filename": filename} if filename is not None else {}
        return client.post(UPLOAD_PATH, params=params, content=body,
                           headers=headers if headers is not None
                           else self._headers())

    # -- gate -----------------------------------------------------------------

    def test_gate_off_returns_stable_404_and_touches_no_database(self):
        conn = _NoStatementsConn()
        with self._client(conn, settings=_settings(
                sycm_manual_import_enabled=False)) as (client, calls):
            response = self._post(client, body=valid_shop_xlsx())
        self.assertEqual(response.status_code, 404, response.text)
        self.assertEqual(response.json(),
                         {"code": "not_found", "message": "功能未启用"})
        self.assertEqual(calls, [], "gate 关闭时连应用连接都不建")
        self.assertEqual(conn.executed, [])

    # -- 写面边界（字节体适配） ------------------------------------------------

    def test_production_upload_without_subject_is_unauthenticated(self):
        conn = _NoStatementsConn()
        with self._client(
                conn,
                settings=_settings(environment="production")) as (client,
                                                                  calls):
            response = client.post(
                UPLOAD_PATH, params={"filename": FILENAME},
                content=valid_shop_xlsx(),
                headers={"X-BI-Agent": "web", "Origin": ORIGIN,
                         "Content-Type": "application/octet-stream"})
        self.assertEqual(response.status_code, 401, response.text)
        self.assertEqual(response.json()["code"], "unauthenticated")
        self.assertEqual(calls, [])
        self.assertEqual(conn.executed, [])

    def test_boundary_rejections_are_403_with_stable_reason_codes(self):
        without_web_header = {key: value
                              for key, value in self._headers().items()
                              if key != "X-BI-Agent"}
        cases = (
            ("web_header_missing", without_web_header),
            ("origin_mismatch", self._headers(Origin="https://evil.test")),
            ("unsupported_media_type",
             self._headers(**{"Content-Type": "application/json"})),
        )
        for expected_code, headers in cases:
            with self.subTest(code=expected_code):
                conn = _NoStatementsConn()
                with self._client(conn) as (client, calls):
                    response = self._post(client, body=valid_shop_xlsx(),
                                          headers=headers)
                self.assertEqual(response.status_code, 403, response.text)
                body = response.json()
                self.assertEqual(body["code"], expected_code)
                self.assertEqual(calls, [], "被拒请求不建连接")
                self.assertEqual(conn.executed, [])

    # -- 文件名消毒 -----------------------------------------------------------

    def test_invalid_filenames_are_422_with_stable_code(self):
        bad_names = (
            None,                                   # 缺失
            "",                                     # 空白
            "../淘店.xlsx",                          # 路径分隔
            "a\\b.xlsx",                            # 反斜杠
            "店铺\n维度.xlsx",                        # 控制字符
            "店 维度.xlsx",                          # 白名单外字符（空格）
            "x.xlsx" + "a" * 300,                   # 超长（>255 字节）
            "CON.xlsx",                             # Windows 保留设备名
            "报表.",                                # 结尾点号（Windows 剥离歧义）
        )
        for filename in bad_names:
            with self.subTest(filename=filename):
                conn = _NoStatementsConn()
                with self._client(conn) as (client, calls):
                    response = self._post(client, filename=filename,
                                          body=valid_shop_xlsx())
                self.assertEqual(response.status_code, 422, response.text)
                self.assertEqual(response.json()["code"], "invalid_filename")
                self.assertEqual(conn.executed, [])

    def test_header_filename_is_accepted_and_query_param_wins(self):
        """X-File-Name 头可作为文件名来源；两者同给时查询参数优先。

        头部只能承载 ASCII（HTTP 头编码限制），中文文件名走查询参数
        （UTF-8 百分号编码）——这正是"查询参数优先"的实现理由。
        """
        conn = _RecordingConn()
        with self._client(conn) as (client, calls):
            response = client.post(
                UPLOAD_PATH, params={"filename": FILENAME},
                content=not_xlsx_bytes(),
                headers=self._headers(**{"X-File-Name": "ascii-name.xlsx"}))
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["status"], "rejected")
        self.assertEqual(conn.uploads[-1]["filename"], FILENAME,
                         "查询参数优先于头部")
        self.assertTrue(calls, "处理路径使用应用连接")

    # -- 写面身份 fail-closed ---------------------------------------------------

    def test_unusable_write_identity_fails_closed_with_stable_code(self):
        """gate 开启但 031 写面授权缺席（InsufficientPrivilege）⇒ 稳定 503 码。"""
        with self._client(_BrokenWriteConn()) as (client, calls):
            response = self._post(client, body=valid_shop_xlsx())
        self.assertEqual(response.status_code, 503, response.text)
        body = response.json()
        self.assertEqual(body["code"], "sycm_upload_write_path_unavailable")
        self.assertNotIn("permission denied", response.text,
                         "数据库异常文本不得外泄")


# ---------------------------------------------------------------------------
# 数据库路径：处理契约、幂等、拒绝台账、大小上限（外层回滚事务）
# ---------------------------------------------------------------------------


@unittest.skipUnless(os.getenv("BI_TEST_ADMIN_DSN"), "未配置独立测试数据库")
class SycmUploadDatabaseTests(unittest.TestCase):
    """处理契约与台账走真实测试库；请求内写入随外层事务回滚。"""

    def setUp(self):
        self.conn = connect_test_db(self)
        self._seed_shop()

    # -- 夹具 ------------------------------------------------------------------

    def _seed_shop(self, shop_id: str = SHOP_ID, name: str = SHOP_NAME,
                   platform: str = "tb") -> None:
        self.conn.execute(
            "INSERT INTO bi.shops(shop_id, platform, display_name, capabilities) "
            "VALUES (%s, %s, %s, '{}') ON CONFLICT (shop_id) DO UPDATE SET "
            "platform = EXCLUDED.platform, display_name = EXCLUDED.display_name, "
            "capabilities = '{}'", (shop_id, platform, name))

    def _client(self):
        from bi_agent.api import create_app

        return TestClient(create_app(_settings()))

    @contextlib.contextmanager
    def _patched(self):
        conn = TestTransactionConnection(self.conn)
        with patch_connect(lambda dsn, **kwargs: conn):
            yield

    def _post(self, client, *, filename=FILENAME, body):
        return client.post(UPLOAD_PATH, params={"filename": filename},
                           content=body,
                           headers={"X-BI-Agent": "web", "Origin": ORIGIN,
                                    "Content-Type": "application/octet-stream"})

    def _uploads(self, filename: str):
        return self.conn.execute(
            "SELECT id, kind, status, reason_code, rows_read, rows_written, "
            "shops, content_sha256, subject_id FROM bi.manual_uploads "
            "WHERE filename = %s ORDER BY created_at", (filename,)).fetchall()

    def _events(self, upload_id) -> list[tuple[str, dict]]:
        return [(row[0], row[1]) for row in self.conn.execute(
            "SELECT event_type, payload FROM bi.manual_upload_events "
            "WHERE upload_id = %s ORDER BY created_at, id", (upload_id,))]

    def _fact_count(self) -> int:
        return self.conn.execute(
            "SELECT count(*) FROM bi.sycm_shop_daily WHERE shop_id=%s",
            (SHOP_ID,)).fetchone()[0]

    # -- 成功路径：契约 + 幂等 + lineage ----------------------------------------

    def test_upload_success_returns_exact_contract_and_writes_ledger(self):
        client = self._client()
        with self._patched():
            response = self._post(client, body=valid_shop_xlsx())
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(set(body), CONTRACT_FIELDS,
                         "响应契约键集精确（M5 按它渲染）")
        self.assertEqual(body["status"], "accepted")
        self.assertIsNone(body["reason_code"])
        self.assertEqual(body["shops"], [SHOP_ID])
        self.assertEqual((body["rows_read"], body["rows_written"]), (1, 1))
        self.assertEqual(str(UUID(body["upload_id"])), body["upload_id"],
                         "upload_id 是台账行的 uuid")
        # 台账：一行 + received/accepted 事件，subject 是写面主体。
        rows = self._uploads(FILENAME)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][1:4], ("shop", "accepted", None))
        self.assertEqual(rows[0][6], [SHOP_ID])
        self.assertEqual(rows[0][8], SUBJECT)
        self.assertEqual(str(rows[0][0]), body["upload_id"])
        self.assertEqual({name for name, _ in self._events(rows[0][0])},
                         {"received", "accepted"})
        # 事实与覆盖凭证：D1 立即可用+披露。
        self.assertEqual(self._fact_count(), 1)
        self.assertEqual(self.conn.execute(
            "SELECT count(*) FROM bi.sync_state "
            "WHERE source='manual.sycm.export' AND shop_id=%s",
            (SHOP_ID,)).fetchone()[0], 1)

    def test_same_window_reuploads_are_idempotent_and_lineage_follows_content(
            self):
        client = self._client()
        with self._patched():
            first = self._post(client, body=valid_shop_xlsx())
            second = self._post(client, body=valid_shop_xlsx())
        self.assertEqual((first.status_code, second.status_code), (200, 200))
        first_body, second_body = first.json(), second.json()
        self.assertEqual((first_body["status"], second_body["status"]),
                         ("accepted", "accepted"))
        self.assertEqual(first_body["shops"], second_body["shops"])
        self.assertNotEqual(first_body["upload_id"], second_body["upload_id"],
                            "每次上传各留一行台账（追加历史）")
        expected_sha = hashlib.sha256(valid_shop_xlsx()).hexdigest()
        self.assertEqual(self.conn.execute(
            "SELECT count(*) FROM bi.sycm_shop_daily WHERE shop_id=%s",
            (SHOP_ID,)).fetchone()[0], 1, "同 (店,日) 幂等，只有一行事实")
        self.assertEqual(self.conn.execute(
            "SELECT DISTINCT batch_id FROM bi.sycm_shop_daily WHERE shop_id=%s",
            (SHOP_ID,)).fetchall(), [(expected_sha,)],
            "同内容批次的 lineage 就是内容哈希")
        self.assertEqual(len(self._uploads(FILENAME)), 2, "台账按次追加")

        # 不同内容、同一窗口：后批替换前批，lineage 随内容哈希变化。
        other_rows = shop_rows(1, shop=SHOP_NAME)
        other_rows[1][SHOP_HEADER.index("支付金额")] = "1,111.11"
        other_bytes = build_xlsx(other_rows)
        other_sha = hashlib.sha256(other_bytes).hexdigest()
        with self._patched():
            third = self._post(client,
                               filename="淘店-改-店铺维度_20261001_ffee0011.xlsx",
                               body=other_bytes)
        self.assertEqual(third.json()["status"], "accepted")
        batch_id, paid = self.conn.execute(
            "SELECT batch_id, paid_amount_sycm FROM bi.sycm_shop_daily "
            "WHERE shop_id=%s", (SHOP_ID,)).fetchone()
        self.assertEqual(batch_id, other_sha, "lineage 切换到后批内容哈希")
        self.assertEqual(paid, Decimal("1111.11"), "后批内容覆盖前批")

    # -- 拒绝分支：同一契约 + 台账 + 不写事实 -------------------------------------

    def test_rejection_branches_return_contract_and_ledger_without_facts(self):
        cases = (
            ("not_xlsx", not_xlsx_bytes()),
            ("crc_corrupted", crc_corrupted_xlsx()),
            ("truncated_zip", truncated_xlsx()),
            ("unregistered_shop", unregistered_shop_xlsx()),
            ("pii_columns", pii_xlsx()),
        )
        for label, body_bytes in cases:
            with self.subTest(case=label):
                client = self._client()
                with self._patched():
                    response = self._post(client, body=body_bytes)
                self.assertEqual(response.status_code, 200, response.text)
                payload = response.json()
                self.assertEqual(set(payload), CONTRACT_FIELDS)
                self.assertEqual(payload["status"], "rejected")
                self.assertEqual(payload["shops"], [], "拒绝不留任何 shop")
                self.assertIn(payload["reason_code"],
                              {"parse_failed", "shop_not_registered",
                               "pii_columns_rejected"}, payload)
                rows = self._uploads(FILENAME)
                self.assertEqual(len(rows), 1, "拒绝也要留台账")
                self.assertEqual(rows[0][2], "rejected")
                self.assertEqual(rows[0][3], payload["reason_code"])
                events = self._events(rows[0][0])
                self.assertEqual({name for name, _ in events},
                                 {"received", "rejected"})
                rejected = next(payload_ for name, payload_ in events
                                if name == "rejected")
                self.assertEqual(rejected["reason_code"],
                                 payload["reason_code"])
                self.assertEqual(self._fact_count(), 0, "拒绝路径不写事实表")
                self.assertEqual(self.conn.execute(
                    "SELECT count(*) FROM bi.sync_state "
                    "WHERE source='manual.sycm.export' AND shop_id=%s",
                    (SHOP_ID,)).fetchone()[0], 0, "拒绝不写覆盖凭证")
                # 用例间互不残留（外层事务内清理台账行，事件级联删除）。
                self.conn.execute(
                    "DELETE FROM bi.manual_uploads WHERE filename=%s",
                    (FILENAME,))

    # -- 大小上限 ---------------------------------------------------------------

    def test_oversize_body_is_rejected_with_ledger_event_and_no_facts(self):
        client = self._client()
        oversize = b"PK" + b"\x00" * (FIVE_MB + 1 - 2)
        with self._patched():
            response = self._post(client, body=oversize)
        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(set(payload), CONTRACT_FIELDS)
        self.assertEqual(payload["status"], "rejected")
        self.assertEqual(payload["reason_code"], "parse_failed",
                         "超限用 M2 集中枚举（防护上限归 parse_failed）")
        rows = self._uploads(FILENAME)
        self.assertEqual(len(rows), 1, "超限拒绝也留台账")
        events = self._events(rows[0][0])
        rejected = next(payload_ for name, payload_ in events
                        if name == "rejected")
        # 事件 payload 摊平 detail：file_too_large 上限与实际字节数都点名。
        self.assertEqual(rejected["detail"], "file_too_large")
        self.assertEqual(rejected["limit_bytes"], FIVE_MB)
        self.assertEqual(rejected["bytes"], len(oversize))
        self.assertEqual(rejected["content_scope"], "retained_bytes_only",
                         "台账哈希只覆盖保留内容，不冒充整文件哈希")
        self.assertEqual(self._fact_count(), 0)

    def test_capped_stream_rejects_bodies_larger_than_declared(self):
        """Content-Length 谎报（小于实际流）时流式封顶兜底（单元级）。"""
        from bi_agent.sycm_upload import _read_capped_body

        class FakeRequest:
            def __init__(self, headers, chunks):
                self.headers = headers
                self._chunks = chunks

            def stream(self):
                async def chunks():
                    for chunk in self._chunks:
                        yield chunk
                return chunks()

        body = b"a" * (FIVE_MB + 1)
        retained, oversized = asyncio.run(_read_capped_body(FakeRequest(
            {"content-length": "1024"}, [body[:1024], body[1024:]])))
        self.assertEqual(oversized, len(body))
        self.assertEqual(len(retained), 1024, "只保留封顶前的前缀")
        # 声明即超限：不读流，保留零字节。
        retained, oversized = asyncio.run(_read_capped_body(FakeRequest(
            {"content-length": str(len(body))}, [body])))
        self.assertEqual(oversized, len(body))
        self.assertEqual(retained, b"")

    # -- gate 与拒绝路径的台账隔离 -----------------------------------------------

    def test_gate_off_leaves_no_ledger(self):
        from bi_agent.api import create_app

        client = TestClient(create_app(
            _settings(sycm_manual_import_enabled=False)))
        with self._patched():
            response = self._post(client, body=valid_shop_xlsx())
        self.assertEqual(response.status_code, 404)
        self.assertEqual(self._uploads(FILENAME), [], "404 不留台账")


# ---------------------------------------------------------------------------
# 031：词表 CHECK、授权集精确、RLS 范围（事务内应用 + 重放）
# ---------------------------------------------------------------------------


def _migration_sql() -> str:
    path = Path(__file__).parents[1] / "sql" / "031_sycm_upload_write_path.sql"
    return path.read_text(encoding="utf-8")


@unittest.skipUnless(os.getenv("BI_TEST_ADMIN_DSN"), "未配置独立测试数据库")
class SycmUploadWritePathMigrationTests(unittest.TestCase):
    """031 在事务内应用并重放：词表反向突变、授权精确、RLS 范围。"""

    REASON_CODES = ("missing_columns", "bad_date", "shop_not_registered",
                    "row_overflow", "pii_columns_rejected", "parse_failed",
                    "not_imported_this_round")

    def setUp(self):
        self.conn = connect_test_db(self)
        self.conn.execute(_migration_sql())
        self.conn.execute(_migration_sql())  # 幂等重放

    def _upload(self, *, status="accepted", reason_code=None,
                sha256="a" * 64):
        self.conn.execute(
            "INSERT INTO bi.manual_uploads(id, subject_id, filename, "
            "content_sha256, status, reason_code) VALUES (%s, 'subject-a', "
            "'x.xlsx', %s, %s, %s)",
            (uuid4(), sha256, status, reason_code))

    def test_status_vocabulary_check_rejects_unknown_words(self):
        self._upload(status="accepted")  # 合法基线
        for bad_status in ("weird", "ACCEPTED", "pending", ""):
            with self.subTest(status=bad_status):
                with self.assertRaises(psycopg.errors.CheckViolation), \
                        self.conn.transaction():
                    self._upload(status=bad_status)

    def test_reason_code_vocabulary_matches_the_m2_enum(self):
        for code in self.REASON_CODES:
            with self.subTest(code=code):
                self._upload(status="rejected", reason_code=code)
        for bad_code in ("shop_not_found", "unknown", "PARSE_FAILED"):
            with self.subTest(code=bad_code):
                with self.assertRaises(psycopg.errors.CheckViolation), \
                        self.conn.transaction():
                    self._upload(status="rejected", reason_code=bad_code)

    def test_grant_set_is_exactly_the_approved_matrix(self):
        """role_table_grants / role_column_grants 断言授权集精确（含批准偏差）。

        表级：role_table_grants 的 (table → 权限集) 必须精确等于批准矩阵。
        列级：role_column_grants 会把表级授权展开成逐列行，所以对写面六表
        断言"每列可见的权限 = 表级授权 ∪ (shops.capabilities 的列级 UPDATE)"
        ——多出任何一列/一种权限都算夹带。
        """
        grants: dict[str, set[str]] = {}
        for table, privilege in self.conn.execute(
                "SELECT table_name, privilege_type FROM information_schema."
                "role_table_grants WHERE grantee = 'bi_app' AND "
                "table_schema='bi' AND table_name = ANY(%s)",
                (sorted(APP_TABLE_GRANTS),)).fetchall():
            grants.setdefault(table, set()).add(privilege)
        self.assertEqual(grants, APP_TABLE_GRANTS, grants)

        expected_columns: dict[tuple[str, str], set[str]] = {}
        for table, privileges in APP_TABLE_GRANTS.items():
            for (column,) in self.conn.execute(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema='bi' AND table_name=%s "
                    "ORDER BY ordinal_position", (table,)).fetchall():
                expected = set(privileges)
                if (table, column) == ("shops", "capabilities"):
                    expected.add("UPDATE")  # 031 的列级授权
                expected_columns[(table, column)] = expected
        actual_columns: dict[tuple[str, str], set[str]] = {}
        for table, column, privilege in self.conn.execute(
                "SELECT table_name, column_name, privilege_type FROM "
                "information_schema.role_column_grants WHERE grantee='bi_app' "
                "AND table_schema='bi' AND table_name = ANY(%s)",
                (sorted(APP_TABLE_GRANTS),)).fetchall():
            actual_columns.setdefault((table, column), set()).add(privilege)
        self.assertEqual(actual_columns, expected_columns,
                         "列级授权面必须精确等于表级授权 + capabilities 列级 UPDATE")
        # bi_sync 对四张 sycm 表没有任何授权（001 的 ON ALL TABLES 不覆盖后建表）。
        sync_grants = self.conn.execute(
            "SELECT count(*) FROM information_schema.role_table_grants "
            "WHERE grantee='bi_sync' AND table_schema='bi' "
            "AND table_name = ANY(%s)", (list(UPLOAD_TABLES),)).fetchone()[0]
        self.assertEqual(sync_grants, 0)
        # PUBLIC 对写面全部收权。
        public_grants = self.conn.execute(
            "SELECT count(*) FROM information_schema.role_table_grants "
            "WHERE grantee='PUBLIC' AND table_schema='bi' "
            "AND table_name = ANY(%s)",
            (sorted((*UPLOAD_TABLES, "sync_state")),)).fetchone()[0]
        self.assertEqual(public_grants, 0)

    def test_rls_scopes_bi_app_to_the_manual_source_family(self):
        """bi_app 只见/只写 manual.sycm.export 行；erp 行不可见、改不动、插不进。"""
        self.conn.execute(
            "INSERT INTO bi.sync_state(source, entity, shop_id, watermark) "
            "VALUES ('erp.trade.outstock.simple.query', 'orders', 'RLS-ERP', "
            "'2026-09-01') ON CONFLICT (source, entity, shop_id) DO NOTHING")

        def as_bi_app(body):
            with self.conn.transaction():
                self.conn.execute("SET LOCAL ROLE bi_app")
                try:
                    return body()
                finally:
                    self.conn.execute("RESET ROLE")

        # erp 行不可见、UPDATE 静默零行（fail-closed 不篡改）。
        visible = as_bi_app(lambda: self.conn.execute(
            "SELECT count(*) FROM bi.sync_state WHERE shop_id='RLS-ERP'"
        ).fetchone()[0])
        self.assertEqual(visible, 0)
        tampered = as_bi_app(lambda: self.conn.execute(
            "UPDATE bi.sync_state SET quality_status='passed' "
            "WHERE shop_id='RLS-ERP'").rowcount)
        self.assertEqual(tampered, 0)
        # 插入 manual 行 OK；插入 erp 行被 WITH CHECK 拒（RLS 违规是
        # 42501 insufficient_privilege 族，带稳定文案，不是普通 CHECK）。
        as_bi_app(lambda: self.conn.execute(
            "INSERT INTO bi.sync_state(source, entity, shop_id, covered) "
            "VALUES ('manual.sycm.export', 'shop_daily', 'RLS-MANUAL', "
            "tstzmultirange(tstzrange('2026-09-01','2026-09-02','[)')))"))
        with self.assertRaises(psycopg.errors.InsufficientPrivilege) as caught, \
                self.conn.transaction():
            # 失败的语句把事务打成 aborted：RESET ROLE 交给保存点回滚顺带撤销
            # （SET LOCAL 的作用域就是这一个保存点），不再手动恢复。
            self.conn.execute("SET LOCAL ROLE bi_app")
            self.conn.execute(
                "INSERT INTO bi.sync_state(source, entity, shop_id) "
                "VALUES ('erp.trade.list.query', 'orders', 'RLS-FORGE')")
        self.assertIn("row-level security", str(caught.exception))
        # manual 行的幂等覆盖 upsert（读旧 covered 合并）照常工作。
        as_bi_app(lambda: self.conn.execute(
            "INSERT INTO bi.sync_state(source, entity, shop_id, covered, "
            "quality_status, quality_rule) VALUES ('manual.sycm.export', "
            "'shop_daily', 'RLS-MANUAL', tstzmultirange(tstzrange("
            "'2026-09-02','2026-09-03','[)')), 'unknown', "
            "'manual-export/unverified') ON CONFLICT (source, entity, "
            "shop_id) DO UPDATE SET covered = bi.sync_state.covered + "
            "EXCLUDED.covered"))
        merged = self.conn.execute(
            "SELECT covered FROM bi.sync_state WHERE shop_id='RLS-MANUAL'"
        ).fetchone()[0]
        self.assertEqual(len(list(merged)), 1, "两段相邻覆盖归并成一段")
        only = list(merged)[0]
        self.assertEqual(
            (only.lower, only.upper),
            (datetime(2026, 9, 1, tzinfo=ZoneInfo("Asia/Shanghai")),
             datetime(2026, 9, 3, tzinfo=ZoneInfo("Asia/Shanghai"))),
            "upsert 读旧值合并（SELECT 授权是 upsert 表达式所需）")
        # bi_sync（同步写者保底策略）全通：erp 行可插可改，全表可见。
        with self.conn.transaction():
            self.conn.execute("SET LOCAL ROLE bi_sync")
            try:
                self.conn.execute(
                    "INSERT INTO bi.sync_state(source, entity, shop_id) "
                    "VALUES ('erp.trade.list.query', 'orders', 'RLS-SYNC') "
                    "ON CONFLICT (source, entity, shop_id) DO UPDATE SET "
                    "watermark='2026-09-02'")
                total = self.conn.execute(
                    "SELECT count(*) FROM bi.sync_state WHERE shop_id "
                    "LIKE 'RLS-%'").fetchone()[0]
            finally:
                self.conn.execute("RESET ROLE")
        self.assertEqual(total, 3)

    def test_reader_coverage_view_is_unaffected_by_rls(self):
        """RLS 不得误伤 bi_reader 经 v_coverage 的既有读取（视图以属主权限运行）。"""
        with self.conn.transaction():
            self.conn.execute("SET LOCAL ROLE bi_reader")
            try:
                self.conn.execute("SELECT count(*) FROM reporting.v_coverage")
            finally:
                self.conn.execute("RESET ROLE")


if __name__ == "__main__":
    unittest.main()
