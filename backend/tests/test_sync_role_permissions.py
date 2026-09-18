"""sync 角色可操作性：bi_sync 的目录版本 UPSERT 与来源对账读取面。

复现两条本地事故（均由迁移 024 修复）：

  1) `sync shops` / `sync products` 在名称或档案变化后调用
     `bi_agent.catalog.repository.bump_catalog_version`，该函数用
     `INSERT ... ON CONFLICT (id) DO UPDATE` 递增 `bi.catalog_state.version`。
     007 只给了 bi_sync `SELECT, UPDATE`，缺 `INSERT`，于是
     `bi_agent.sync shops` 报 `permission denied for table catalog_state`。
  2) `reconcile --days N` 提交完窗口后调用
     `bi_agent.data_quality.reconcile_source_quality`，读
     `reporting.v_source_batches` 与 `reporting.v_refunds`。bi_sync 既没有
     `reporting` schema 的 `USAGE`，也没有这两条视图的 `SELECT`，于是对账在
     读质量证据那一步报 `permission denied for schema reporting`。

本模块用真实 `*_test` 库与真实 bi_sync 角色取证：先按 bi_sync 身份跑通
`bump_catalog_version`（rollback 隔离，不落版本）与对账读取路径，再反向钉住 024
只补了最小授权——reporting 上不给 `CREATE`/DML，无关 reporting 视图仍读不到，
PUBLIC/bi_app 对 `bi.catalog_state` 的收权未被动过。所有写入都落在 dbfixtures 的
外层回滚事务里，测试库不会留下业务行或版本变化。

真实测试库跑法与既有约定一致；无 `BI_TEST_ADMIN_DSN` 时显式 skip——skip 不是通过证明。
"""

from __future__ import annotations

import os
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import psycopg

from .dbfixtures import connect_test_db

BEIJING = ZoneInfo("Asia/Shanghai")

# 合成店铺：不会与测试库既有店铺相撞，对账只读空来源，返回确定的结果。
PROBE_SHOP = "SYNC-ROLE-PROBE"
PROBE_SOURCE = "erp.trade.list.query"

# 024 之后 bi_sync 应该能读的两条对账视图（与 reconcile_source_quality 的读路径一一对应）。
RECONCILE_VIEWS = ("reporting.v_source_batches", "reporting.v_refunds")

# 与 sync 无关的稳定 reporting 对象：由既有迁移（001/005/007/008/021/023）建立，
# bi_sync 从未获准，024 也不得放开其中任何一个。
UNRELATED_VIEWS = (
    "reporting.v_payments",
    "reporting.v_coverage",
    "reporting.v_product_daily",
    "reporting.v_inventory_notifications",
    "reporting.v_approved_query_examples",
)

MIGRATION = (Path(__file__).resolve().parents[1]
             / "sql" / "024_sync_role_operability.sql")


@unittest.skipUnless(os.getenv("BI_TEST_ADMIN_DSN"), "未配置独立测试数据库")
class SyncRolePermissionTests(unittest.TestCase):
    """真实 bi_sync 身份的授权取证；连接与回滚由 dbfixtures 统一负责。"""

    def setUp(self):
        self.conn = connect_test_db(self)

    # -- bi_sync 正向：事故里真实失败的两个操作 ------------------------------

    # 切到 bi_sync 身份跑一段：保存点提交时角色在本次测试内生效，保存点回滚时
    # （包括权限失败把事务打成 aborted 的情况）角色与 aborted 一起撤销，失败信息
    # 保留原始的 InsufficientPrivilege 而不是被 InFailedSqlTransaction 盖掉。
    def _run_as_sync(self, body):
        with self.conn.transaction():
            self.conn.execute("SET LOCAL ROLE bi_sync")
            return body()

    def test_sync_role_can_bump_catalog_version_upsert(self):
        """bi_sync 必须能跑 bump_catalog_version 的 INSERT ... ON CONFLICT UPDATE。"""
        from bi_agent.catalog import bump_catalog_version

        before = self.conn.execute(
            "SELECT version FROM bi.catalog_state WHERE id = 1").fetchone()[0]

        def body():
            self.assertTrue(bump_catalog_version(self.conn))
            return self.conn.execute(
                "SELECT version FROM bi.catalog_state WHERE id = 1").fetchone()[0]

        after = self._run_as_sync(body)
        # 行已存在，upsert 必须命中 UPDATE 分支：版本恰好 +1（外层回滚后不落库）。
        self.assertEqual(after, before + 1)

    def test_sync_role_reads_reconcile_reporting_views(self):
        """bi_sync 必须能读 reconcile_source_quality 依赖的两条 reporting 视图。"""
        for view in RECONCILE_VIEWS:
            with self.subTest(view=view):
                self._run_as_sync(
                    lambda view=view: self.conn.execute(
                        f"SELECT count(*) FROM {view}").fetchone())

    def test_sync_role_runs_reconcile_quality_read_path(self):
        """按 bi_sync 身份跑通 reconcile_source_quality：有凭证时读到 v_refunds。

        没有凭证只读 v_source_batches 就返回 unknown，证明不了 v_refunds 可读，
        所以先在回滚事务里补一条本窗口的 reconcile 业务凭证，走到退款读取分支。
        """
        from bi_agent.data_quality import reconcile_source_quality

        start = datetime(2026, 8, 1, tzinfo=BEIJING)
        end = datetime(2026, 8, 8, tzinfo=BEIJING)
        self.conn.execute(
            "INSERT INTO bi.sync_batches(batch_id, source, entity, shop_id, "
            "business_window, window_kind, mode, row_count) "
            "VALUES (%s, %s, 'orders', %s, tstzrange(%s, %s), 'business', "
            "'reconcile', 0)",
            ("sync-role-probe", PROBE_SOURCE, PROBE_SHOP, start, end))
        status = self._run_as_sync(lambda: reconcile_source_quality(
            self.conn, shop_id=PROBE_SHOP, entity="orders",
            start=start, end=end, source=PROBE_SOURCE))
        # 有凭证、无未匹配退款：口径成立，返回 passed（写入同步状态也只是回滚事务内）。
        self.assertEqual(status, "passed")

    # -- 024 的精确授权矩阵 --------------------------------------------------

    def test_024_grants_exactly_the_missing_privileges(self):
        """只补 INSERT + reporting USAGE + 两视图 SELECT，不夹带任何多余授权。"""
        matrix = self.conn.execute(
            """SELECT
                 has_table_privilege('bi_sync', 'bi.catalog_state', 'INSERT'),
                 has_table_privilege('bi_sync', 'bi.catalog_state', 'SELECT'),
                 has_table_privilege('bi_sync', 'bi.catalog_state', 'UPDATE'),
                 has_table_privilege('bi_sync', 'bi.catalog_state', 'DELETE'),
                 has_schema_privilege('bi_sync', 'reporting', 'USAGE'),
                 has_schema_privilege('bi_sync', 'reporting', 'CREATE'),
                 has_table_privilege('bi_sync', 'reporting.v_source_batches', 'SELECT'),
                 has_table_privilege('bi_sync', 'reporting.v_refunds', 'SELECT'),
                 has_table_privilege('bi_sync', 'reporting.v_source_batches', 'INSERT'),
                 has_table_privilege('bi_sync', 'reporting.v_source_batches', 'UPDATE'),
                 has_table_privilege('bi_sync', 'reporting.v_source_batches', 'DELETE'),
                 has_table_privilege('bi_sync', 'reporting.v_refunds', 'INSERT'),
                 has_table_privilege('bi_sync', 'reporting.v_refunds', 'UPDATE'),
                 has_table_privilege('bi_sync', 'reporting.v_refunds', 'DELETE')
            """).fetchone()
        self.assertEqual(matrix, (
            True, True, True, False,    # catalog_state: 补 INSERT，保留 007 的 SELECT/UPDATE
            True, False,                # reporting schema: USAGE 可读，不给 CREATE
            True, True,                 # 两条对账视图: SELECT 可读
            False, False, False,        # v_source_batches 只读
            False, False, False,        # v_refunds 只读
        ), matrix)
        for view in UNRELATED_VIEWS:
            with self.subTest(view=view):
                self.assertFalse(self.conn.execute(
                    "SELECT has_table_privilege('bi_sync', %s, 'SELECT')",
                    (view,)).fetchone()[0])

    def test_024_adds_no_role_membership(self):
        """024 只做对象授权，不添加/继承任何角色成员关系。"""
        memberships = self.conn.execute(
            """SELECT member.rolname, granted.rolname
               FROM pg_auth_members a
               JOIN pg_roles member ON member.oid = a.member
               JOIN pg_roles granted ON granted.oid = a.roleid
               WHERE member.rolname = 'bi_sync' OR granted.rolname = 'bi_sync'"""
        ).fetchall()
        self.assertEqual(memberships, [], memberships)

    # -- 反向：未被放开的权限必须仍然被角色拦下 ------------------------------

    def test_sync_role_denials_are_enforced_by_the_role(self):
        """reporting 上没有 CREATE/DML，无关视图与聊天底表仍读不到。"""
        denials = (
            "SELECT count(*) FROM reporting.v_payments",
            "SELECT count(*) FROM reporting.v_coverage",
            "SELECT count(*) FROM reporting.v_approved_query_examples",
            "SELECT count(*) FROM reporting.v_inventory_notifications",
            "SELECT count(*) FROM bi.app_chats",
            "CREATE TABLE reporting.sync_role_probe (id integer)",
        )
        # 每条负向断言包在各自保存点里：否则一句权限失败会把整段事务打成
        # aborted，后面几条验的只是 InFailedSqlTransaction 而不是真权限。
        for statement in denials:
            with self.subTest(sql=statement[:48]), \
                    self.assertRaises(psycopg.errors.InsufficientPrivilege), \
                    self.conn.transaction():
                self.conn.execute("SET LOCAL ROLE bi_sync")
                self.conn.execute(statement)

        def allowed():
            # 获准的读取面：两条对账视图与目录版本确实可读。
            for view in RECONCILE_VIEWS:
                self.conn.execute(f"SELECT count(*) FROM {view}").fetchone()
            self.conn.execute(
                "SELECT version FROM bi.catalog_state WHERE id = 1").fetchone()

        self._run_as_sync(allowed)

    def test_public_and_app_still_cannot_read_catalog_state(self):
        """保留 007 的收权姿态：PUBLIC 与 bi_app 仍读不到 bi.catalog_state。"""
        public_privs = self.conn.execute(
            """SELECT has_table_privilege('public', 'bi.catalog_state', 'SELECT'),
                      has_table_privilege('public', 'bi.catalog_state', 'INSERT'),
                      has_table_privilege('public', 'bi.catalog_state', 'UPDATE')"""
        ).fetchone()
        self.assertEqual(public_privs, (False, False, False), public_privs)
        app_privs = self.conn.execute(
            """SELECT has_table_privilege('bi_app', 'bi.catalog_state', 'SELECT'),
                      has_table_privilege('bi_app', 'bi.catalog_state', 'INSERT'),
                      has_table_privilege('bi_app', 'bi.catalog_state', 'UPDATE')"""
        ).fetchone()
        self.assertEqual(app_privs, (False, False, False), app_privs)
        with self.assertRaises(psycopg.errors.InsufficientPrivilege), \
                self.conn.transaction():
            self.conn.execute("SET LOCAL ROLE bi_app")
            self.conn.execute("SELECT count(*) FROM bi.catalog_state").fetchone()

    # -- 迁移自身：前向且幂等 -------------------------------------------------

    def test_024_migration_replays_idempotently(self):
        """迁移文件必须存在、非空，且在同一事务里连跑两次不报错。"""
        sql = MIGRATION.read_text(encoding="utf-8")
        self.assertTrue(sql.strip(), f"迁移缺失或为空：{MIGRATION.name}")
        self.assertIn("bi.catalog_state", sql)
        with self.conn.transaction():
            self.conn.execute(sql)
            self.conn.execute(sql)
            # 重放后 bi_sync 的读取面在本次事务内即刻成立。
            self.conn.execute("SET LOCAL ROLE bi_sync")
            for view in RECONCILE_VIEWS:
                self.conn.execute(f"SELECT count(*) FROM {view}").fetchone()


if __name__ == "__main__":
    unittest.main()
