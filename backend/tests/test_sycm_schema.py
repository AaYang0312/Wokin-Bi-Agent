"""028：生意参谋手工导出导入的数据层契约（计划 §3.M1）。

锁四件事：
1) 四张新表存在且列集**精确等于**白名单——多一列都是口径/PII 泄漏的开口；
2) PK 锁日聚合身份，金额/计数非负、比率 [0,1] 的 CHECK 真的会拒行；
3) 迁移文件连续重放两遍不报错（幂等）；
4) 本切片不新增授权：四张表 relacl 为空，bi_sync/bi_app 对事件表无任何写读权，
   只追加由权限面保证（后续授权切片对事件表也只能追加 SELECT/INSERT）。

所有对象操作都在外层回滚事务内进行，共享测试库零残留；无测试 DSN 时显式
skip——skip 不是通过证明。
"""

import os
import unittest
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import psycopg
from psycopg.types.json import Jsonb

from .dbfixtures import connect_test_db

TABLES = ("sycm_shop_daily", "sycm_customer_daily",
          "manual_uploads", "manual_upload_events")

# 店铺维度 17 个指标列 + 身份/批次列；表头映射见 028 迁移注释（2026-10-01 导出实测）。
SHOP_DAILY_COLUMNS = (
    "shop_id", "stat_date",
    "visitors", "item_visitors", "views",
    "paid_amount_sycm", "paid_buyers", "paid_items", "order_amount", "refund_amount",
    "ad_spend_all", "ad_spend_keyword", "ad_spend_crowd", "ad_spend_smart",
    "ke_commission", "pay_conversion_rate", "aov", "cart_count", "favorite_count",
    "batch_id", "recorded_at",
)
# 客户维度只收 8 个日聚合列（用户数/已购用户数/新访客户数/父订单数/复购父订单数/
# 180天复购率/店铺粉丝数/店铺会员数）；任何个体级明细列永不入库。
CUSTOMER_DAILY_COLUMNS = (
    "shop_id", "stat_date",
    "users", "paid_users", "new_customers", "parent_orders", "repeat_parent_orders",
    "repeat_rate_180d", "shop_fans", "shop_members",
    "batch_id", "recorded_at",
)
UPLOAD_COLUMNS = (
    "id", "subject_id", "filename", "content_sha256", "kind", "status", "reason_code",
    "rows_read", "rows_written", "shops", "created_at", "completed_at",
)
EVENT_COLUMNS = ("id", "upload_id", "event_type", "payload", "created_at")


def _columns(conn: psycopg.Connection, table: str) -> list[str]:
    return [row[0] for row in conn.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema='bi' AND table_name=%s "
        "ORDER BY ordinal_position", (table,)).fetchall()]


def _primary_key(conn: psycopg.Connection, table: str) -> list[str]:
    return [row[0] for row in conn.execute(
        "SELECT kcu.column_name "
        "FROM information_schema.table_constraints tc "
        "JOIN information_schema.key_column_usage kcu "
        "  ON tc.constraint_name = kcu.constraint_name "
        " AND tc.constraint_schema = kcu.constraint_schema "
        "WHERE tc.table_schema='bi' AND tc.table_name=%s "
        "  AND tc.constraint_type='PRIMARY KEY' "
        "ORDER BY kcu.ordinal_position", (table,)).fetchall()]


@unittest.skipUnless(os.getenv("BI_TEST_ADMIN_DSN"), "未配置独立测试数据库")
class SycmManualExportSchemaTests(unittest.TestCase):
    """bi.sycm_shop_daily / bi.sycm_customer_daily / bi.manual_uploads / _events。"""

    def setUp(self):
        self.conn = connect_test_db(self)

    @staticmethod
    def _migration_sql() -> str:
        path = Path(__file__).parents[1] / "sql" / "028_manual_sycm_export.sql"
        return path.read_text(encoding="utf-8")

    def _apply_fresh(self) -> None:
        """撤掉旧对象、从零应用迁移，模拟"从未迁移"的库。"""
        self.conn.execute(
            "DROP TABLE IF EXISTS bi.manual_upload_events, bi.manual_uploads, "
            "bi.sycm_customer_daily, bi.sycm_shop_daily CASCADE")
        self.conn.execute(self._migration_sql())

    def _shop(self, shop_id: str = "SY1", platform: str = "fxg") -> None:
        self.conn.execute(
            "INSERT INTO bi.shops(shop_id, platform, display_name) VALUES "
            "(%s, %s, '生意参谋手工导入测试店') ON CONFLICT (shop_id) DO NOTHING",
            (shop_id, platform))

    def _shop_row(self, shop_id: str = "SY1", stat_date: str = "2026-09-30",
                  **overrides) -> None:
        """一行全合法的店铺维度日聚合；overrides 用来构造违例行。"""
        values = {
            "visitors": 100, "item_visitors": 100, "views": 120,
            "paid_amount_sycm": "4693.78", "paid_buyers": 20, "paid_items": 43,
            "order_amount": "7449.10", "refund_amount": "518.00",
            "ad_spend_all": "377.44", "ad_spend_keyword": "106.14",
            "ad_spend_crowd": "48.53", "ad_spend_smart": "0",
            "ke_commission": "0", "pay_conversion_rate": "0.0014",
            "aov": "234.69", "cart_count": 31, "favorite_count": 19,
            "batch_id": "d09c215888e9a3ef3805810fe2a83c11",
        }
        values.update(overrides)
        self.conn.execute(
            "INSERT INTO bi.sycm_shop_daily(shop_id, stat_date, visitors, "
            "item_visitors, views, paid_amount_sycm, paid_buyers, paid_items, "
            "order_amount, refund_amount, ad_spend_all, ad_spend_keyword, "
            "ad_spend_crowd, ad_spend_smart, ke_commission, pay_conversion_rate, "
            "aov, cart_count, favorite_count, batch_id) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, "
            "%s, %s, %s, %s, %s)",
            (shop_id, stat_date, values["visitors"], values["item_visitors"],
             values["views"], values["paid_amount_sycm"], values["paid_buyers"],
             values["paid_items"], values["order_amount"], values["refund_amount"],
             values["ad_spend_all"], values["ad_spend_keyword"],
             values["ad_spend_crowd"], values["ad_spend_smart"],
             values["ke_commission"], values["pay_conversion_rate"], values["aov"],
             values["cart_count"], values["favorite_count"], values["batch_id"]))

    def _customer_row(self, shop_id: str = "SY1", stat_date: str = "2026-09-30",
                      **overrides) -> None:
        values = {
            "users": 13975, "paid_users": 20, "new_customers": 11870,
            "parent_orders": 24, "repeat_parent_orders": 11,
            "repeat_rate_180d": "0.1576", "shop_fans": 8, "shop_members": 0,
            "batch_id": "7bc8996eb85b631c642438c0ef473ac6",
        }
        values.update(overrides)
        self.conn.execute(
            "INSERT INTO bi.sycm_customer_daily(shop_id, stat_date, users, "
            "paid_users, new_customers, parent_orders, repeat_parent_orders, "
            "repeat_rate_180d, shop_fans, shop_members, batch_id) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (shop_id, stat_date, values["users"], values["paid_users"],
             values["new_customers"], values["parent_orders"],
             values["repeat_parent_orders"], values["repeat_rate_180d"],
             values["shop_fans"], values["shop_members"], values["batch_id"]))

    def _upload(self, upload_id=None, kind: str | None = "shop",
                sha256: str = "a" * 64) -> object:
        upload_id = upload_id or uuid4()
        self.conn.execute(
            "INSERT INTO bi.manual_uploads(id, subject_id, filename, "
            "content_sha256, kind, status) VALUES (%s, 'subject-a', "
            "'淘店-沃金五金机电商城-店铺维度_20261001_x.xlsx', %s, %s, 'accepted')",
            (upload_id, sha256, kind))
        return upload_id

    # -- 幂等与形状 -----------------------------------------------------------

    def test_migration_replays_twice_idempotently(self):
        """真实执行迁移脚本两遍：只读文本证明不了幂等。"""
        self._apply_fresh()
        self.conn.execute(self._migration_sql())  # 重复执行不得报错（幂等）
        for table in TABLES:
            count = self.conn.execute(
                "SELECT count(*) FROM information_schema.tables "
                "WHERE table_schema='bi' AND table_name=%s", (table,)).fetchone()[0]
            self.assertEqual(count, 1, table)

    def test_shop_daily_columns_are_exactly_the_whitelist(self):
        self._apply_fresh()
        self.assertEqual(sorted(_columns(self.conn, "sycm_shop_daily")),
                         sorted(SHOP_DAILY_COLUMNS),
                         "店铺维度列集必须精确等于计划白名单，多一列都是泄漏开口")

    def test_customer_daily_columns_are_exactly_the_whitelist(self):
        self._apply_fresh()
        self.assertEqual(sorted(_columns(self.conn, "sycm_customer_daily")),
                         sorted(CUSTOMER_DAILY_COLUMNS),
                         "客户维度只收日聚合白名单列，个体级明细列无库内载体")

    def test_manual_uploads_columns_are_exactly_the_whitelist(self):
        self._apply_fresh()
        self.assertEqual(sorted(_columns(self.conn, "manual_uploads")),
                         sorted(UPLOAD_COLUMNS))

    def test_manual_upload_events_columns_are_exactly_the_whitelist(self):
        self._apply_fresh()
        self.assertEqual(sorted(_columns(self.conn, "manual_upload_events")),
                         sorted(EVENT_COLUMNS))

    def test_primary_keys_and_identity_column_types(self):
        self._apply_fresh()
        self.assertEqual(_primary_key(self.conn, "sycm_shop_daily"),
                         ["shop_id", "stat_date"], "日聚合身份 = (店, 北京时间日)")
        self.assertEqual(_primary_key(self.conn, "sycm_customer_daily"),
                         ["shop_id", "stat_date"])
        self.assertEqual(_primary_key(self.conn, "manual_uploads"), ["id"])
        self.assertEqual(_primary_key(self.conn, "manual_upload_events"), ["id"])
        types = {(row[0], row[1]): row[2] for row in self.conn.execute(
            "SELECT table_name, column_name, data_type FROM information_schema.columns "
            "WHERE table_schema='bi' AND table_name = ANY(%s)",
            (list(TABLES),)).fetchall()}
        expected = {
            ("sycm_shop_daily", "stat_date"): "date",
            ("sycm_shop_daily", "paid_amount_sycm"): "numeric",
            ("sycm_shop_daily", "visitors"): "bigint",
            ("sycm_shop_daily", "recorded_at"): "timestamp with time zone",
            ("sycm_customer_daily", "repeat_rate_180d"): "numeric",
            ("sycm_customer_daily", "users"): "bigint",
            ("manual_uploads", "id"): "uuid",
            ("manual_uploads", "rows_read"): "integer",
            ("manual_uploads", "rows_written"): "integer",
            ("manual_uploads", "shops"): "ARRAY",
            ("manual_uploads", "created_at"): "timestamp with time zone",
            ("manual_uploads", "completed_at"): "timestamp with time zone",
            ("manual_upload_events", "payload"): "jsonb",
            ("manual_upload_events", "created_at"): "timestamp with time zone",
        }
        for key, data_type in expected.items():
            self.assertEqual(types.get(key), data_type, key)

    # -- CHECK / FK / NOT NULL 生效 --------------------------------------------

    def test_shop_daily_rejects_negative_amounts_and_unscaled_rates(self):
        self._apply_fresh()
        self._shop()
        bad_values = [
            ("visitors", -1), ("item_visitors", -1), ("views", -1),
            ("paid_amount_sycm", "-0.01"), ("paid_buyers", -1), ("paid_items", -1),
            ("order_amount", "-0.01"), ("refund_amount", "-0.01"),
            ("ad_spend_all", "-1"), ("ad_spend_keyword", "-1"),
            ("ad_spend_crowd", "-1"), ("ad_spend_smart", "-1"),
            ("ke_commission", "-0.50"), ("aov", "-2"),
            ("cart_count", -1), ("favorite_count", -1),
            # 导出是百分数（如 0.14%）；忘除 100 的解析回归必须被拒。
            ("pay_conversion_rate", "1.5"), ("pay_conversion_rate", "-0.001"),
        ]
        for column, value in bad_values:
            with self.subTest(column=column, value=value):
                with self.assertRaises(psycopg.errors.CheckViolation), \
                        self.conn.transaction():
                    self._shop_row(**{column: value})
        self._shop_row()
        stored = self.conn.execute(
            "SELECT pay_conversion_rate, recorded_at FROM bi.sycm_shop_daily "
            "WHERE shop_id='SY1'").fetchone()
        self.assertEqual(stored[0], Decimal("0.0014"), "比率列存除以 100 后的分数")
        self.assertIsNotNone(stored[1], "recorded_at 由 DEFAULT now() 填充")

    def test_shop_daily_rejects_future_dates_and_broken_batch_identity(self):
        self._apply_fresh()
        self._shop()
        with self.assertRaises(psycopg.errors.CheckViolation), self.conn.transaction():
            self._shop_row(stat_date="9999-12-31")
        with self.assertRaises(psycopg.errors.CheckViolation), self.conn.transaction():
            self._shop_row(batch_id="   ")
        with self.assertRaises(psycopg.errors.NotNullViolation), \
                self.conn.transaction():
            self._shop_row(batch_id=None)
        with self.assertRaises(psycopg.errors.ForeignKeyViolation), \
                self.conn.transaction():
            self._shop_row(shop_id="未登记店铺")

    def test_customer_daily_rejects_negative_counts_and_out_of_range_rates(self):
        self._apply_fresh()
        self._shop()
        bad_values = [
            ("users", -1), ("paid_users", -1), ("new_customers", -1),
            ("parent_orders", -1), ("repeat_parent_orders", -1),
            ("shop_fans", -1), ("shop_members", -1),
            ("repeat_rate_180d", "1.5"), ("repeat_rate_180d", "-0.1"),
        ]
        for column, value in bad_values:
            with self.subTest(column=column, value=value):
                with self.assertRaises(psycopg.errors.CheckViolation), \
                        self.conn.transaction():
                    self._customer_row(**{column: value})
        self._customer_row()
        stored = self.conn.execute(
            "SELECT repeat_rate_180d FROM bi.sycm_customer_daily "
            "WHERE shop_id='SY1'").fetchone()
        self.assertEqual(stored[0], Decimal("0.1576"))

    def test_manual_uploads_defaults_kind_vocabulary_and_sha256_shape(self):
        self._apply_fresh()
        upload_id = uuid4()
        self.conn.execute(
            "INSERT INTO bi.manual_uploads(id, subject_id, filename, content_sha256, "
            "status) VALUES (%s, 'subject-a', 'x.xlsx', %s, 'accepted')",
            (upload_id, "b" * 64))
        row = self.conn.execute(
            "SELECT kind, reason_code, rows_read, rows_written, shops, created_at, "
            "completed_at FROM bi.manual_uploads WHERE id=%s", (upload_id,)).fetchone()
        self.assertIsNone(row[0], "kind 允许暂未知（分类失败也要留台账）")
        self.assertIsNone(row[1])
        self.assertEqual((row[2], row[3], row[4]), (0, 0, []), "计数与 shops 有默认值")
        self.assertIsNotNone(row[5], "created_at 由 DEFAULT now() 填充")
        self.assertIsNone(row[6], "completed_at 留给完成时刻")
        with self.assertRaises(psycopg.errors.CheckViolation), self.conn.transaction():
            self._upload(kind="item")
        with self.assertRaises(psycopg.errors.CheckViolation), self.conn.transaction():
            self._upload(sha256="nothex")
        self._upload(kind="customer")

    def test_events_cascade_with_their_upload(self):
        self._apply_fresh()
        upload_id = self._upload()
        for event_type in ("received", "rejected"):
            self.conn.execute(
                "INSERT INTO bi.manual_upload_events(id, upload_id, event_type, "
                "payload) VALUES (%s, %s, %s, %s)",
                (uuid4(), upload_id, event_type,
                 Jsonb({"reason_code": "shop_not_registered"})))
        self.conn.execute("DELETE FROM bi.manual_uploads WHERE id=%s", (upload_id,))
        count = self.conn.execute(
            "SELECT count(*) FROM bi.manual_upload_events").fetchone()[0]
        self.assertEqual(count, 0, "删除台账必须级联删除其事件")

    def test_no_new_grants_and_events_stay_append_only(self):
        """本切片不新增授权：四张新表无任何显式授权，事件表对谁都不开 UPDATE/DELETE。"""
        self._apply_fresh()
        for table in TABLES:
            relacl = self.conn.execute(
                "SELECT relacl FROM pg_class WHERE oid=%s::regclass",
                (f"bi.{table}",)).fetchone()[0]
            self.assertIsNone(relacl, f"{table} 不应带任何显式授权")
        for role, statement in (
                ("bi_sync", "INSERT INTO bi.manual_upload_events(id, upload_id, "
                            "event_type) VALUES (%s, %s, 'forged')"),
                ("bi_app", "SELECT count(*) FROM bi.manual_upload_events")):
            self.conn.execute(f"SET LOCAL ROLE {role}")
            with self.assertRaises(psycopg.errors.InsufficientPrivilege), \
                    self.conn.transaction():
                self.conn.execute(statement, (uuid4(), uuid4()) if role == "bi_sync"
                                  else ())
            self.conn.execute("RESET ROLE")


if __name__ == "__main__":
    unittest.main()
