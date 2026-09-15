"""独立测试数据库内的事务、权限和聚合检查。

单个用例在管理员连接的外层事务中准备数据，结束回滚；禁止连接生产库。
无测试DSN时显式skip——skip不是通过证明。
"""

import json
import os
import unittest
from datetime import datetime, timedelta
from decimal import Decimal
from unittest.mock import patch
from uuid import uuid4
from zoneinfo import ZoneInfo

import httpx
import psycopg

from .dbfixtures import (PRODUCT_DAILY_COLUMNS, PRODUCT_DAILY_COLUMNS_AFTER_005,
                       connect_test_db)

from .dbfixtures import connect_test_db

BEIJING = ZoneInfo("Asia/Shanghai")
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}

# 种子店默认“已逐来源核验并授予全部指标能力”；未授权/未取证对照店保持空数组。
ALL_CAPABILITIES = ("paid_amount", "paid_orders", "erp_documents", "aov", "quantity",
                    "product_paid_amount", "refund_amount", "cash_difference",
                    "cohort_refund_rate")


def set_capabilities(conn, shop_id: str, *capabilities: str) -> None:
    """写能力标签。它代表“已完成逐来源取证”这件事，只能由测试显式调用。"""
    conn.execute("UPDATE bi.shops SET capabilities=%s WHERE shop_id=%s",
                 (list(capabilities), shop_id))


def _ms(moment: datetime) -> int:
    """北京时间转快麦毫秒时间戳。"""
    return int(moment.timestamp() * 1000)


@unittest.skipUnless(os.getenv("BI_TEST_ADMIN_DSN"), "未配置独立测试数据库")
class DatabaseTests(unittest.TestCase):
    def setUp(self):
        # 外层事务必须在这儿开：被测同步函数自己开 transaction()，否则它们会提交假数据。
        self.conn = connect_test_db(self)

    def _seed_shop(self, shop_id: str = "S1", platform: str = "fxg",
                   capabilities=ALL_CAPABILITIES):
        """预置一家店；默认代表“已逐来源取证并授予全部指标能力”的健康店。

        能力标签不随“插了一行店铺档案”自动成立：需要未开通能力的场景时，
        调用方显式传 `capabilities=()` 或事后 `set_capabilities` 掉。
        """
        self.conn.execute(
            "INSERT INTO bi.shops(shop_id, platform, display_name) VALUES (%s, %s, %s) "
            "ON CONFLICT (shop_id) DO NOTHING",
            (shop_id, platform, "店铺A"),
        )
        set_capabilities(self.conn, shop_id, *capabilities)

    # -- 权限 ----------------------------------------------------------------

    def test_app_role_writes_chats_but_not_business_facts(self):
        self.conn.execute("SET LOCAL ROLE bi_app")
        self.conn.execute(
            "INSERT INTO bi.app_chats(id, subject_id, title) VALUES (%s, 'subject-a', '新对话')",
            (uuid4(),),
        )
        with self.assertRaises(psycopg.errors.InsufficientPrivilege):
            with self.conn.transaction():
                self.conn.execute("INSERT INTO bi.shops(shop_id) VALUES ('forbidden')")
        self.conn.execute("RESET ROLE")

    def test_chat_turn_lock_rejects_a_second_connection(self):
        from bi_agent.chats import (ChatBusy, claim_chat_turn, create_chat,
                                    delete_chat, release_chat_turn)

        dsn = os.environ["BI_TEST_ADMIN_DSN"]
        first = psycopg.connect(dsn, autocommit=True)
        second = psycopg.connect(dsn, autocommit=True)
        subject = f"lock-{uuid4()}"
        chat = create_chat(first, subject)
        first_locked = second_locked = False
        try:
            claim_chat_turn(first, chat.id, subject)
            first_locked = True
            with self.assertRaises(ChatBusy):
                claim_chat_turn(second, chat.id, subject)
            release_chat_turn(first, chat.id)
            first_locked = False
            claim_chat_turn(second, chat.id, subject)
            second_locked = True
        finally:
            if first_locked:
                release_chat_turn(first, chat.id)
            if second_locked:
                release_chat_turn(second, chat.id)
            delete_chat(first, subject, chat.id)
            first.close()
            second.close()

    def test_read_role_cannot_write(self):
        with psycopg.connect(os.environ["BI_TEST_READER_DSN"]) as conn:
            self.assertTrue(conn.info.dbname.endswith("_test"))
            with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                conn.execute("INSERT INTO bi.shops(shop_id) VALUES ('forbidden')")

    def test_read_role_cannot_read_base_tables(self):
        with psycopg.connect(os.environ["BI_TEST_READER_DSN"]) as conn:
            self.assertTrue(conn.info.dbname.endswith("_test"))
            with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                conn.execute("SELECT count(*) FROM bi.orders")

    def test_reader_can_read_reporting_views_only(self):
        with psycopg.connect(os.environ["BI_TEST_READER_DSN"]) as conn:
            self.assertTrue(conn.info.dbname.endswith("_test"))
            conn.execute("SELECT count(*) FROM reporting.v_payments").fetchone()
            conn.execute("SELECT count(*) FROM reporting.v_refunds").fetchone()
            conn.execute("SELECT count(*) FROM reporting.v_coverage").fetchone()
            conn.execute("SELECT count(*) FROM reporting.v_shops").fetchone()

    def test_statement_timeout_enforced_for_reader(self):
        with psycopg.connect(os.environ["BI_TEST_READER_DSN"]) as conn:
            value = conn.execute("SHOW statement_timeout").fetchone()[0]
            self.assertEqual(value, "5s")
            read_only = conn.execute("SHOW default_transaction_read_only").fetchone()[0]
        self.assertEqual(read_only, "on")

    def test_sync_role_can_write_fact_tables(self):
        self._seed_shop()
        with self.conn.transaction():
            self.conn.execute("SET LOCAL ROLE bi_sync")
            self.conn.execute(
                "INSERT INTO bi.orders(shop_id, erp_id, source, source_updated_at, batch_id) "
                "VALUES ('S1', 'EX', 'erp.trade.list.query', now(), 'b1')")
        self.conn.execute("RESET ROLE")

    def test_shop_sync_persists_the_documented_active_flag(self):
        from bi_agent.sync import sync_shops

        class Client:
            def call(self, method, parameters):
                return {"success": True, "total": 2, "hasNext": False, "list": [
                    {"userId": "S_DISABLED", "state": 1, "active": 0},
                    {"userId": "S_ACTIVE", "state": 4, "active": 1},
                ]}

        self.assertEqual(sync_shops(self.conn, Client()), 2)
        rows = self.conn.execute(
            "SELECT shop_id, enabled FROM bi.shops "
            "WHERE shop_id IN ('S_DISABLED', 'S_ACTIVE') ORDER BY shop_id").fetchall()
        self.assertEqual(rows, [("S_ACTIVE", True), ("S_DISABLED", False)])

    # -- 测试事务边界 ---------------------------------------------------

    def test_sync_helper_writes_are_not_committed(self):
        """sync_shops/sync_products 内部的 conn.transaction() 不得提交。

        假档案带着未来的 source_modified_at 一旦落库，版本守卫会永久拒收接口回来的
        真档案，测试库就再也刷不成真数据。
        """
        from bi_agent.sync import sync_shops

        class Client:
            def call(self, method, parameters):
                return {"success": True, "total": 1, "hasNext": False,
                        "list": [{"userId": "S_TX_GUARD", "state": 4, "active": 1}]}

        dsn = os.environ["BI_TEST_ADMIN_DSN"]
        try:
            self.assertEqual(sync_shops(self.conn, Client()), 1)
            with psycopg.connect(dsn) as other:
                visible = other.execute(
                    "SELECT count(*) FROM bi.shops WHERE shop_id='S_TX_GUARD'").fetchone()[0]
            self.assertEqual(visible, 0, "同步写入逃出了测试事务，已提交进共享测试库")
        finally:
            with psycopg.connect(dsn, autocommit=True) as cleaner:
                cleaner.execute("DELETE FROM bi.shops WHERE shop_id='S_TX_GUARD'")

    # -- 商品档案维表 -------------------------------------------------------

    @staticmethod
    def _goods_client(rows, *, total):
        class Client:
            def call(self, method, parameters):
                assert method == "item.list.query", method
                assert parameters["pageSize"] == "200", parameters
                return {"success": True, "total": total, "items": rows}
        return Client()

    # 合成商品号：不会与实测 435 条真档案相撞（真号是 15~16 位）。
    GOODS_ID = "900000000000000001"

    def _goods_row(self, **overrides):
        row = {"sysItemId": self.GOODS_ID, "title": "接头-元发", "outerId": "JT-01",
               "type": 0, "activeStatus": 1, "itemCategoryNames": "气动配件",
               "purchasePrice": 3.5, "modified": 1788166354000}
        row.update(overrides)
        return row

    def _product(self, columns="*"):
        return self.conn.execute(
            f"SELECT {columns} FROM bi.products WHERE product_id=%s",
            (self.GOODS_ID,)).fetchone()

    def test_product_sync_writes_archive_and_reports_the_change_kind(self):
        from bi_agent.sync import sync_products

        archive = [self._goods_row()]
        stats = sync_products(self.conn, self._goods_client(archive, total=1))
        self.assertEqual((stats["fetched"], stats["upserted"]), (1, 1))
        row = self._product("product_id, title, outer_id, item_type, category, active, "
                            "purchase_price, source_modified_at")
        self.assertEqual(row[0], self.GOODS_ID)
        self.assertEqual(row[1:6], ("接头-元发", "JT-01", "0", "气动配件", True))
        self.assertEqual(row[6], Decimal("3.5"))

        # 同一版本再跑一次：不写行，计入 skipped
        again = sync_products(self.conn, self._goods_client(archive, total=1))
        self.assertEqual((again["upserted"], again["skipped"]), (0, 1))

    def test_product_sync_keeps_the_newer_archive_version(self):
        """较旧的 source_modified_at 不得覆盖较新的档案（与 replay 同规则）。"""
        from bi_agent.sync import sync_products

        sync_products(self.conn, self._goods_client(
            [self._goods_row(title="接头-元发(新)")], total=1))
        sync_products(self.conn, self._goods_client(
            [self._goods_row(title="接头-元发(旧)", modified=1700000000000)], total=1))
        self.assertEqual(self._product("title")[0], "接头-元发(新)")
        self.assertEqual(self._product("source_modified_at")[0],
                         datetime.fromtimestamp(1788166354, tz=BEIJING))

    def test_sync_persists_stable_refs_for_reverse_lookup(self):
        """引用落表：反查与撞车检测依赖这张表，不能只靠读侧现算。"""
        from bi_agent.catalog import lookup_refs, ref_for_key
        from bi_agent.sync import sync_products, sync_shops

        class Shops:
            def call(self, method, parameters):
                return {"success": True, "total": 1, "hasNext": False,
                        "list": [{"userId": "S_REF", "state": 4, "active": 1,
                                  "title": "引用落表店"}]}

        sync_shops(self.conn, Shops())
        sync_products(self.conn, self._goods_client([self._goods_row()], total=1))

        shop_ref = ref_for_key("shop", "S_REF")
        product_ref = ref_for_key("product", self.GOODS_ID)
        self.assertEqual(lookup_refs(self.conn, [shop_ref, product_ref]),
                         {shop_ref: ("shop", "S_REF"),
                          product_ref: ("product", self.GOODS_ID)})

    def test_archive_name_join_leaves_totals_untouched(self):
        """补名称前后同范围数值必须一致：名称只能做展示，不能改变金额、销量与父项口径。"""
        from bi_agent.sync import apply_trade, normalise_trade

        self._seed_shop()
        paid_at = datetime(2026, 9, 2, 12, tzinfo=BEIJING)
        trade = normalise_trade({
            "sid": "E_JOIN", "userId": "S1", "tid": "C_JOIN", "payAmount": "100",
            "updTime": _ms(paid_at), "payTime": _ms(paid_at),
            "orders": [
                {"oid": "L_PARENT", "tid": "C_JOIN", "itemSysId": "P_JOIN", "type": 2,
                 "num": "1", "payAmount": "100", "sysTitle": "套件当时名"},
                {"oid": "L_CHILD", "tid": "C_JOIN", "itemSysId": "P_JOIN", "type": 1,
                 "num": "2", "payAmount": "0", "sysTitle": "子件当时名"},
            ],
        })
        totals = ("SELECT coalesce(sum(quantity), 0), coalesce(sum(gift_quantity), 0), "
                  "coalesce(sum(product_paid_amount), 0), count(*), "
                  "count(product_name) FROM reporting.v_product_daily "
                  "WHERE shop_id='S1' AND product_id='P_JOIN'")
        with self.conn.transaction():
            self.assertTrue(apply_trade(self.conn, trade, batch_id="join"))
            before = self.conn.execute(totals).fetchone()   # 档案还不存在：名称 JOIN 不中
            self.assertEqual(before[4], 0)
            self.conn.execute(
                "INSERT INTO bi.products(product_id, title, normalization_status) "
                "VALUES ('P_JOIN', '套件-元发', 'normal') "
                "ON CONFLICT (product_id) DO UPDATE SET title=EXCLUDED.title")
            after = self.conn.execute(totals).fetchone()

        self.assertEqual(after[:4], before[:4], "名称列不得改变销量、赠品量与支付额")
        self.assertEqual(after[4], after[3], "档案命中后每个聚合组都拿到名称")

    def test_trade_line_snapshots_are_persisted_and_survive_replay(self):
        """重放时上游没带名称，不得把已留住的成交快照洗成空。"""
        from bi_agent.sync import apply_trade, normalise_trade

        self._seed_shop()
        paid_at = datetime(2026, 9, 2, 12, tzinfo=BEIJING)

        def trade(orders, upd_time):
            return normalise_trade({
                "sid": "E_SNAP", "userId": "S1", "tid": "C_SNAP", "payAmount": "100",
                "updTime": _ms(upd_time), "payTime": _ms(paid_at), "orders": orders,
            })

        named = [{"oid": "L_SNAP", "tid": "C_SNAP", "itemSysId": "P_SNAP", "type": 0,
                  "num": "1", "payAmount": "100", "sysTitle": "接头-元发",
                  "sysSkuPropertiesName": "接头 20PP"}]
        unnamed = [{key: value for key, value in named[0].items()
                    if not key.startswith("sys")}]
        columns = ("SELECT product_name_snapshot, sku_label_snapshot FROM bi.order_items "
                   "WHERE shop_id='S1' AND erp_id='E_SNAP' AND line_id='L_SNAP'")
        with self.conn.transaction():
            self.assertTrue(apply_trade(self.conn, trade(named, paid_at), batch_id="snap"))
            self.assertEqual(self.conn.execute(columns).fetchone(),
                             ("接头-元发", "接头 20PP"))
            self.assertTrue(apply_trade(self.conn,
                                        trade(unnamed, paid_at + timedelta(hours=1)),
                                        batch_id="replay"))
            self.assertEqual(self.conn.execute(columns).fetchone(),
                             ("接头-元发", "接头 20PP"))

    def test_archive_and_shop_sync_bump_catalog_version_only_on_name_change(self):
        from bi_agent.catalog import catalog_version
        from bi_agent.sync import sync_products, sync_shops

        before = catalog_version(self.conn)
        sync_products(self.conn, self._goods_client([self._goods_row()], total=1))
        self.assertEqual(catalog_version(self.conn), before + 1)

        sync_products(self.conn, self._goods_client([self._goods_row()], total=1))
        self.assertEqual(catalog_version(self.conn), before + 1, "同一版本档案不推进目录版本")

        class Shops:
            def call(self, method, parameters):
                return {"success": True, "total": 1, "hasNext": False,
                        "list": [{"userId": "S_CAT_VERSION", "state": 4, "active": 1,
                                  "title": "目录版本店"}]}

        sync_shops(self.conn, Shops())
        self.assertEqual(catalog_version(self.conn), before + 2)
        sync_shops(self.conn, Shops())
        self.assertEqual(catalog_version(self.conn), before + 2, "名称未变不推进版本")

    def test_product_daily_view_gives_the_archive_name_without_the_cost(self):
        """成本价只能留在 bi.products，不能随商品名进任何 reporting 视图。"""
        columns = [row[0] for row in self.conn.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema='reporting' AND table_name='v_product_daily' "
            "ORDER BY ordinal_position").fetchall()]
        self.assertEqual(columns, PRODUCT_DAILY_COLUMNS)
        exposed = self.conn.execute(
            "SELECT table_name, view_definition FROM information_schema.views "
            "WHERE table_schema='reporting'").fetchall()
        self.assertTrue(exposed)
        for name, definition in exposed:
            self.assertNotIn("purchase_price", definition, f"{name} 暴露了成本列")

    def test_product_daily_marks_several_skus_instead_of_choosing_one(self):
        """同一天同商品多 SKU：视图只给空规格，不能任选一个规格当展示名。"""
        from bi_agent.sync import apply_trade, normalise_trade

        self._seed_shop()
        paid_at = datetime(2026, 9, 2, 12, tzinfo=BEIJING)
        trade = normalise_trade({
            "sid": "E_SKU", "userId": "S1", "tid": "C_SKU", "payAmount": "150",
            "updTime": _ms(paid_at), "payTime": _ms(paid_at),
            "orders": [
                {"oid": "L_M1", "tid": "C_SKU", "itemSysId": "P_MULTI", "type": 0,
                 "num": "1", "payAmount": "50", "sysTitle": "直钉枪",
                 "sysSkuPropertiesName": "30mm"},
                {"oid": "L_M2", "tid": "C_SKU", "itemSysId": "P_MULTI", "type": 0,
                 "num": "2", "payAmount": "50", "sysTitle": "直钉枪",
                 "sysSkuPropertiesName": "50mm"},
                {"oid": "L_S1", "tid": "C_SKU", "itemSysId": "P_SINGLE", "type": 0,
                 "num": "4", "payAmount": "50", "sysTitle": "撞针",
                 "sysSkuPropertiesName": "成套"},
            ],
        })
        with self.conn.transaction():
            self.assertTrue(apply_trade(self.conn, trade, batch_id="sku"))
            rows = self.conn.execute(
                "SELECT product_id, sku_label, quantity, "
                "product_paid_amount FROM reporting.v_product_daily "
                "WHERE shop_id='S1' ORDER BY product_id").fetchall()

        self.assertEqual([(str(row[0]), row[1]) for row in rows],
                         [("P_MULTI", None), ("P_SINGLE", "成套")],
                         "多 SKU 只能置空，不得挑一个规格展示")
        self.assertEqual([Decimal(str(row[2])) for row in rows],
                         [Decimal("3"), Decimal("4")], "规格列不得改变销量")
        self.assertEqual([Decimal(str(row[3])) for row in rows],
                         [Decimal("100"), Decimal("50")], "规格列不得改变支付额")

    def test_product_rows_drop_spec_when_skus_differ_across_days(self):
        """跨天聚合同样不得任选规格：任一成交行规格缺失或不一致就只留商品名。"""
        import time as time_module

        from bi_agent import metrics
        from bi_agent.metrics import QueryRequest
        from bi_agent.sync import apply_trade, normalise_trade

        self._seed_shop()

        def line(order_id: str, product: str, spec: str) -> dict:
            return {"oid": order_id, "tid": f"C_{product}", "itemSysId": product,
                    "type": 0, "num": "1", "payAmount": "50", "sysTitle": product,
                    "sysSkuPropertiesName": spec}

        for day, spec in ((2, "30mm"), (3, "50mm")):
            paid_at = datetime(2026, 9, day, 12, tzinfo=BEIJING)
            trade = normalise_trade({
                "sid": f"E_{day}", "userId": "S1", "tid": f"CT_{day}",
                "payAmount": "100", "updTime": _ms(paid_at), "payTime": _ms(paid_at),
                "orders": [line(f"L_DIFF_{day}", "P_DIFF", spec),
                           line(f"L_SAME_{day}", "P_SAME", "成套")],
            })
            with self.conn.transaction():
                self.assertTrue(apply_trade(self.conn, trade, batch_id="days"))

        request = QueryRequest(start="2026-09-01", end="2026-09-08", shop_ids=["S1"],
                               metrics=["product_paid_amount"], group_by="product")
        rows = metrics._product_rows(
            self.conn, request, start_ts=datetime(2026, 9, 1, tzinfo=BEIJING),
            end_ts=datetime(2026, 9, 8, tzinfo=BEIJING),
            deadline=time_module.monotonic() + 30)

        by_product = {row["product_id"]: row for row in rows if "product_id" in row}
        self.assertEqual(by_product["P_SAME"]["sku_label"], "成套",
                         "跨天规格一致时仍应展示")
        self.assertIsNone(by_product["P_DIFF"]["sku_label"],
                          "跨天规格不同时不得任选一个")
        self.assertEqual(Decimal(str(by_product["P_DIFF"]["product_paid_amount"])),
                         Decimal("100"), "规格列不得改变支付额")

    def _attribution_limitation(self, result) -> str | None:
        return next((item for item in result.limitations if "未计入商品维度" in item), None)

    def _cover_orders(self, start: datetime, end: datetime) -> None:
        """给 S1 建 orders 覆盖：没有覆盖时查询先被门禁拦下，测不到归属。"""
        self.conn.execute(
            "INSERT INTO bi.sync_state(source, entity, shop_id, watermark, covered, "
            "data_as_of, quality_status, quality_rule) VALUES "
            "('erp.trade.list.query', 'orders', 'S1', %s, "
            "tstzmultirange(tstzrange(%s, %s, '[)')), %s, 'passed', 'test-fixture') "
            "ON CONFLICT (source, entity, shop_id) DO UPDATE SET "
            "watermark = EXCLUDED.watermark, "
            "covered = bi.sync_state.covered + EXCLUDED.covered, "
            "data_as_of = greatest(coalesce(bi.sync_state.data_as_of, '-infinity'), "
            "                       EXCLUDED.data_as_of), "
            "quality_status = EXCLUDED.quality_status, "
            "quality_rule = EXCLUDED.quality_rule",
            (end, start, end, end))

    def _query_ok(self, start: str, end: str):
        import time as time_module

        from bi_agent.metrics import QueryRequest, query_business

        return query_business(
            self.conn, QueryRequest(start=start, end=end, shop_ids=["S1"],
                                    metrics=["paid_amount"]),
            allowed_shop_ids=frozenset({"S1"}), now=FROZEN_NOW,
            deadline=time_module.monotonic() + 30)

    def test_fully_attributed_revenue_adds_no_disclosure(self):
        """全都能归属时不得多插一句：避免把正常查询变成噪声。"""
        from bi_agent.sync import apply_trade

        self._seed_shop()
        pay_time = datetime(2026, 9, 1, 12, 0, tzinfo=BEIJING)
        self._cover_orders(datetime(2026, 9, 1, tzinfo=BEIJING), datetime(2026, 9, 2, tzinfo=BEIJING))
        apply_trade(self.conn, self._trade("E_AT1", ["C_AT1"], "100.00", pay_time,
                                           datetime(2026, 9, 1, 13, 0, tzinfo=BEIJING), [
                                               {"oid": "AT1-1", "tid": "C_AT1",
                                                "itemSysId": "P_A", "num": "1",
                                                "payAmount": "100.00"}]),
                    batch_id="attribution-ok")
        result = self._query_ok("2026-09-01", "2026-09-02")

        self.assertEqual(result.status, "ok", result.limitations)
        self.assertIsNone(self._attribution_limitation(result), result.limitations)

    def test_closed_line_revenue_is_disclosed_as_unattributed(self):
        """关闭单保留支付事实、商品视图只取有效行：差额必须逐元说清。

        模型拿不到归因就会自己编（实测它把差额归给了“赠品/非父项行”，
        而真因是关闭行），所以成因要随金额一起给出。
        """
        from bi_agent.sync import apply_trade, normalise_trade

        self._seed_shop()
        pay_time = datetime(2026, 9, 1, 12, 0, tzinfo=BEIJING)
        self._cover_orders(datetime(2026, 9, 1, tzinfo=BEIJING), datetime(2026, 9, 2, tzinfo=BEIJING))
        trade = normalise_trade({
            "sid": "E_AT2", "userId": "S1", "tid": "C_AT2", "payAmount": "12.80",
            "payTime": _ms(pay_time),
            "updTime": _ms(datetime(2026, 9, 1, 13, 0, tzinfo=BEIJING)),
            "unifiedStatus": "CLOSED", "sysStatus": "CLOSED",
            "orders": [{"oid": "AT2-1", "tid": "C_AT2", "itemSysId": "P_A",
                         "num": "1", "payAmount": "12.80"}],
        })
        self.assertFalse(trade["active"], "CLOSED 单据及其行应判为非有效")
        apply_trade(self.conn, trade, batch_id="attribution-closed")

        result = self._query_ok("2026-09-01", "2026-09-02")

        self.assertEqual(result.status, "ok", result.limitations)
        self.assertEqual(Decimal(str(result.data[0]["paid_amount"])), Decimal("12.80"),
                         "已收款的关闭单仍计入店铺支付额")
        self.assertEqual(self._attribution_limitation(result),
                         "支付额中12.8元未计入商品维度"
                         "（关闭订单行12.8元；赠品行0元；无商品归属0元；其他0元）")

    def test_gift_line_is_attributed_to_gift_not_closed(self):
        """赠品行不能当作关闭行披露：成因分类错了比不披更糟。"""
        from bi_agent.sync import apply_trade

        self._seed_shop()
        pay_time = datetime(2026, 9, 1, 12, 0, tzinfo=BEIJING)
        self._cover_orders(datetime(2026, 9, 1, tzinfo=BEIJING), datetime(2026, 9, 2, tzinfo=BEIJING))
        apply_trade(self.conn, self._trade("E_AT3", ["C_AT3"], "110.00", pay_time,
                                           datetime(2026, 9, 1, 13, 0, tzinfo=BEIJING), [
                                               {"oid": "AT3-1", "tid": "C_AT3",
                                                "itemSysId": "P_A", "num": "1",
                                                "payAmount": "100.00"},
                                               {"oid": "AT3-2", "tid": "C_AT3",
                                                "itemSysId": "P_B", "num": "0",
                                                "giftNum": "2", "payAmount": "10.00"},
                                           ]), batch_id="attribution-gift")

        result = self._query_ok("2026-09-01", "2026-09-02")

        self.assertEqual(self._attribution_limitation(result),
                         "支付额中10元未计入商品维度"
                         "（关闭订单行0元；赠品行10元；无商品归属0元；其他0元）")

    def test_migration_chain_005_then_007_is_forward_only(self):
        """005 → 007 必须能按顺序执行，且各自可重复执行。"""
        from pathlib import Path

        sql_dir = Path(__file__).parents[1] / "sql"
        # 回到 003 产出的视图形状（带 line_kind、不带名称列），再跑 005。
        self.conn.execute("DROP VIEW IF EXISTS reporting.v_product_daily")
        self.conn.execute("DROP TABLE IF EXISTS bi.products CASCADE")
        self.conn.execute((sql_dir / "003_kuaimai_metric_semantics.sql").read_text(encoding="utf-8"))

        fifth = (sql_dir / "005_product_dimension.sql").read_text(encoding="utf-8")
        self.conn.execute(fifth)
        self.conn.execute(fifth)
        self.assertEqual(self._view_columns("v_product_daily"), PRODUCT_DAILY_COLUMNS_AFTER_005)

        seventh = (sql_dir / "007_catalog_identity.sql").read_text(encoding="utf-8")
        self.conn.execute(seventh)
        self.conn.execute(seventh)
        self.assertEqual(self._view_columns("v_product_daily"), PRODUCT_DAILY_COLUMNS)
        self.assertEqual(self.conn.execute(
            "SELECT count(*) FROM bi.catalog_state WHERE id = 1").fetchone()[0], 1)

    def _view_columns(self, view: str) -> list[str]:
        return [row[0] for row in self.conn.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema='reporting' AND table_name=%s "
            "ORDER BY ordinal_position", (view,)).fetchall()]

    # -- 交易规范化与支付重建 -------------------------------------------------

    def _trade(self, erp_id: str, commercial_ids: list[str], pay_amount: str,
               pay_time: datetime, upd_time: datetime, items: list[dict]) -> dict:
        from bi_agent.sync import normalise_trade

        raw = {
            "sid": erp_id,
            "userId": "S1",
            "payAmount": pay_amount,
            "payTime": _ms(pay_time),
            "updTime": _ms(upd_time),
            "orders": items,
        }
        if len(commercial_ids) == 1:
            raw["tid"] = commercial_ids[0]
        elif len(commercial_ids) > 1:
            raw["tids"] = ",".join(commercial_ids)
            raw["tid"] = commercial_ids[0]
        trade = normalise_trade(raw)
        self.assertEqual(trade["normalization_status"], "normal")
        return trade

    def test_single_order_payment_uses_head(self):
        from bi_agent.sync import apply_trade

        self._seed_shop()
        pay_time = datetime(2026, 9, 1, 12, 0, tzinfo=BEIJING)
        upd_time = datetime(2026, 9, 1, 13, 0, tzinfo=BEIJING)
        trade = self._trade("E1", ["C1"], "300.00", pay_time, upd_time, [
            {"oid": "E1-1", "tid": "C1", "itemSysId": "P_A", "skuSysId": "S_A1",
             "num": "2", "payAmount": "200.00", "cost": "50"},
            {"oid": "E1-2", "tid": "C1", "itemSysId": "P_B", "skuSysId": "S_B1",
             "num": "1", "payAmount": "100.00", "cost": "40"},
        ])
        batch = "batch-head"
        with self.conn.transaction():
            self.assertTrue(apply_trade(self.conn, trade, batch_id=batch))
            row = self.conn.execute(
                "SELECT amount, paid_at, basis, verified FROM bi.order_payments "
                "WHERE shop_id='S1' AND commercial_id='C1'").fetchone()
        self.assertEqual(row[0], Decimal("300.00"))
        self.assertEqual(row[1], pay_time)
        self.assertEqual(row[2], "head")
        self.assertTrue(row[3])
        verified_items = self.conn.execute(
            "SELECT count(*) FROM bi.order_items WHERE shop_id='S1' AND allocation_verified"
        ).fetchone()[0]
        self.assertEqual(verified_items, 2)

    def test_split_orders_produce_one_payment(self):
        from bi_agent.sync import apply_trade

        self._seed_shop()
        pay_time = datetime(2026, 9, 2, 10, 0, tzinfo=BEIJING)
        upd_time = datetime(2026, 9, 2, 11, 0, tzinfo=BEIJING)
        batch = "batch-split"
        trade_e3 = self._trade("E3", ["C3"], "40.00", pay_time, upd_time, [
            {"oid": "E3-1", "tid": "C3", "itemSysId": "P_A", "num": "1", "payAmount": "40.00"},
        ])
        trade_e4 = self._trade("E4", ["C3"], "60.00", pay_time, upd_time, [
            {"oid": "E4-1", "tid": "C3", "itemSysId": "P_B", "num": "1", "payAmount": "60.00"},
        ])
        with self.conn.transaction():
            self.assertTrue(apply_trade(self.conn, trade_e3, batch_id=batch))
            self.assertTrue(apply_trade(self.conn, trade_e4, batch_id=batch))
            row = self.conn.execute(
                "SELECT amount, verified, basis FROM bi.order_payments "
                "WHERE shop_id='S1' AND commercial_id='C3'").fetchone()
        self.assertEqual(row[0], Decimal("100.00"))
        self.assertTrue(row[1])
        self.assertEqual(row[2], "items")

    def test_merged_orders_keep_each_payment(self):
        from bi_agent.sync import apply_trade

        self._seed_shop()
        pay_time = datetime(2026, 9, 3, 9, 0, tzinfo=BEIJING)
        upd_time = datetime(2026, 9, 3, 10, 0, tzinfo=BEIJING)
        batch = "batch-merge"
        trade = self._trade("E5", ["C4", "C5"], "200.00", pay_time, upd_time, [
            {"oid": "E5-1", "tid": "C4", "itemSysId": "P_A", "num": "1", "payAmount": "80.00"},
            {"oid": "E5-2", "tid": "C5", "itemSysId": "P_B", "num": "1", "payAmount": "120.00"},
        ])
        with self.conn.transaction():
            self.assertTrue(apply_trade(self.conn, trade, batch_id=batch))
            rows = self.conn.execute(
                "SELECT commercial_id, amount, verified FROM bi.order_payments "
                "WHERE shop_id='S1' AND commercial_id IN ('C4','C5') ORDER BY commercial_id"
            ).fetchall()
        self.assertEqual(rows[0][0], "C4")
        self.assertEqual(rows[0][1], Decimal("80.00"))
        self.assertTrue(rows[0][2])
        self.assertEqual(rows[1][0], "C5")
        self.assertEqual(rows[1][1], Decimal("120.00"))
        self.assertTrue(rows[1][2])

    def test_merged_order_certifies_from_line_amounts(self):
        """真实合单形态：单头 payAmount 只等于此中一个子单。

        实测快麦合单（如 ERP 单 6000726513644043）单头=14.25，行级合计=386.05，
        旧规则因单头与行对不上而整张丢章，店铺侧直接漏记这笔收入。
        行级 payAmount 每行自带 tid，能精确归属，所以按行取证。
        """
        from bi_agent.sync import apply_trade

        self._seed_shop()
        pay_time = datetime(2026, 9, 3, 9, 0, tzinfo=BEIJING)
        upd_time = datetime(2026, 9, 3, 10, 0, tzinfo=BEIJING)
        trade = self._trade("E_M", ["C_M1", "C_M2"], "14.25", pay_time, upd_time, [
            {"oid": "E-M1", "tid": "C_M1", "itemSysId": "P_A", "num": "1",
             "payAmount": "14.25"},
            {"oid": "E-M2", "tid": "C_M2", "itemSysId": "P_B", "num": "1",
             "payAmount": "371.80"},
        ])
        with self.conn.transaction():
            self.assertTrue(apply_trade(self.conn, trade, batch_id="merge-head-short"))
            rows = self.conn.execute(
                "SELECT commercial_id, amount, verified, basis FROM bi.order_payments "
                "WHERE shop_id='S1' AND commercial_id IN ('C_M1','C_M2') "
                "ORDER BY commercial_id").fetchall()

        self.assertEqual([(str(r[0]), str(r[1]), r[2], r[3]) for r in rows],
                         [("C_M1", "14.250000", True, "items_merged"),
                          ("C_M2", "371.800000", True, "items_merged")],
                         "合单要按行级 tid 归属取证，不能整张丢章")

    def test_merged_order_with_missing_child_lines_stays_undetermined(self):
        """子单声明了却没有行：数据未到齐，不能拿不完整证据发核验章。"""
        from bi_agent.sync import apply_trade

        self._seed_shop()
        pay_time = datetime(2026, 9, 3, 9, 0, tzinfo=BEIJING)
        upd_time = datetime(2026, 9, 3, 10, 0, tzinfo=BEIJING)
        trade = self._trade("E_MISS", ["C_MISS1", "C_MISS2"], "10.00", pay_time, upd_time, [
            {"oid": "E-MISS1", "tid": "C_MISS1", "itemSysId": "P_A", "num": "1",
             "payAmount": "10.00"},
        ])
        with self.conn.transaction():
            apply_trade(self.conn, trade, batch_id="merge-missing")
            rows = self.conn.execute(
                "SELECT commercial_id, amount, verified, basis FROM bi.order_payments "
                "WHERE shop_id='S1' AND commercial_id LIKE 'C_MISS%' ORDER BY 1").fetchall()

        self.assertEqual({r[3] for r in rows}, {"undetermined"},
                         "缺兄弟行仍要回到不岻证的本位")
        self.assertTrue(all(not r[2] for r in rows), rows)

    def test_merged_order_with_unpaid_line_stays_undetermined(self):
        """有一行不带金额：存在未归属余额，不能声称行级合计就是已付。"""
        from bi_agent.sync import apply_trade

        self._seed_shop()
        pay_time = datetime(2026, 9, 3, 9, 0, tzinfo=BEIJING)
        upd_time = datetime(2026, 9, 3, 10, 0, tzinfo=BEIJING)
        trade = self._trade("E_NOPAY", ["C_N1", "C_N2"], "10.00", pay_time, upd_time, [
            {"oid": "E-N1", "tid": "C_N1", "itemSysId": "P_A", "num": "1",
             "payAmount": "10.00"},
            {"oid": "E-N2", "tid": "C_N2", "itemSysId": "P_B", "num": "1"},
        ])
        with self.conn.transaction():
            apply_trade(self.conn, trade, batch_id="merge-nopay")
            bases = self.conn.execute(
                "SELECT DISTINCT basis FROM bi.order_payments "
                "WHERE shop_id='S1' AND commercial_id LIKE 'C_N%'").fetchall()

        self.assertEqual([r[0] for r in bases], ["undetermined"])

    def test_split_docs_with_mismatched_totals_stay_undetermined(self):
        """D 的牙齿：不是合单就不能走行级取证。

        拆单（两个单都只挂同一个商业号）时单头合计 100 与行级合计 90 对不上，
        这既不是合单也解释不了差额，必须继续不发证而不是“宽容一下”。
        """
        from bi_agent.sync import apply_trade

        self._seed_shop()
        pay_time = datetime(2026, 9, 3, 9, 0, tzinfo=BEIJING)
        upd_time = datetime(2026, 9, 3, 10, 0, tzinfo=BEIJING)
        batch = "split-mismatch"
        first = self._trade("E_A", ["C_SP1"], "40.00", pay_time, upd_time, [
            {"oid": "E-A1", "tid": "C_SP1", "itemSysId": "P_A", "num": "1",
             "payAmount": "40.00"},
        ])
        second = self._trade("E_B", ["C_SP1"], "60.00", pay_time,
                             datetime(2026, 9, 3, 12, 0, tzinfo=BEIJING), [
                                 {"oid": "E-B1", "tid": "C_SP1", "itemSysId": "P_B",
                                  "num": "1", "payAmount": "50.00"},
                             ])
        with self.conn.transaction():
            apply_trade(self.conn, first, batch_id=batch)
            apply_trade(self.conn, second, batch_id=batch)
            row = self.conn.execute(
                "SELECT amount, verified, basis FROM bi.order_payments "
                "WHERE shop_id='S1' AND commercial_id='C_SP1'").fetchone()

        self.assertEqual((row[0], row[2]), (None, "undetermined"))
        self.assertFalse(row[1])

    def test_single_commercial_document_prefers_the_head(self):
        """回新用为凭：只有一个商业号时单头直接取证，不走合单宽容。

        单头 300 / 行级 250 的差额不是 D 要解决的问题，而是“行未完全归属”
        （与窗内无行的已核验支付同族），已在发现记录里单独列出。
        """
        from bi_agent.sync import apply_trade

        self._seed_shop()
        pay_time = datetime(2026, 9, 3, 9, 0, tzinfo=BEIJING)
        trade = self._trade("E_ONE", ["C_ONE"], "300.00", pay_time,
                            datetime(2026, 9, 3, 10, 0, tzinfo=BEIJING), [
                                {"oid": "E-O1", "tid": "C_ONE", "itemSysId": "P_A",
                                 "num": "1", "payAmount": "250.00"},
                            ])
        with self.conn.transaction():
            apply_trade(self.conn, trade, batch_id="single-head")
            row = self.conn.execute(
                "SELECT amount, verified, basis FROM bi.order_payments "
                "WHERE shop_id='S1' AND commercial_id='C_ONE'").fetchone()

        self.assertEqual((row[0], row[1], row[2]),
                         (Decimal("300.00"), True, "head"),
                         "D 不得改变单头路径的现有行为")

    def test_replay_old_version_does_not_regress(self):
        from bi_agent.sync import apply_trade

        self._seed_shop()
        pay_time = datetime(2026, 9, 1, 12, 0, tzinfo=BEIJING)
        new_trade = self._trade("E1", ["C1"], "300.00", pay_time,
                                datetime(2026, 9, 2, 12, 0, tzinfo=BEIJING), [
                                    {"oid": "E1-1", "tid": "C1", "itemSysId": "P_A",
                                     "num": "2", "payAmount": "200.00"},
                                    {"oid": "E1-2", "tid": "C1", "itemSysId": "P_B",
                                     "num": "1", "payAmount": "100.00"},
                                ])
        old_trade = self._trade("E1", ["C1"], "280.00", pay_time,
                                datetime(2026, 9, 1, 12, 0, tzinfo=BEIJING), [
                                    {"oid": "E1-1", "tid": "C1", "itemSysId": "P_A",
                                     "num": "2", "payAmount": "180.00"},
                                ])
        batch = "batch-version"
        with self.conn.transaction():
            self.assertTrue(apply_trade(self.conn, new_trade, batch_id=batch))
            self.assertFalse(apply_trade(self.conn, old_trade, batch_id=batch))
            self.assertFalse(apply_trade(self.conn, old_trade, batch_id=batch, force=True))
            amount = self.conn.execute(
                "SELECT amount FROM bi.order_payments WHERE commercial_id='C1'").fetchone()[0]
            items = self.conn.execute(
                "SELECT count(*) FROM bi.order_items WHERE erp_id='E1'").fetchone()[0]
        self.assertEqual(amount, Decimal("300.00"))
        self.assertEqual(items, 2)

    def test_replay_same_version_is_idempotent(self):
        from bi_agent.sync import apply_trade

        self._seed_shop()
        pay_time = datetime(2026, 9, 1, 12, 0, tzinfo=BEIJING)
        trade = self._trade("E1", ["C1"], "300.00", pay_time,
                            datetime(2026, 9, 1, 13, 0, tzinfo=BEIJING), [
                                {"oid": "E1-1", "tid": "C1", "itemSysId": "P_A",
                                 "num": "2", "payAmount": "200.00"},
                            ])
        batch = "batch-idem"
        with self.conn.transaction():
            self.assertTrue(apply_trade(self.conn, trade, batch_id=batch))
            self.assertFalse(apply_trade(self.conn, trade, batch_id="batch-idem-2"))
            count = self.conn.execute(
                "SELECT count(*) FROM bi.order_payments WHERE commercial_id='C1'").fetchone()[0]
            items = self.conn.execute(
                "SELECT count(*) FROM bi.order_items WHERE erp_id='E1'").fetchone()[0]
        self.assertEqual(count, 1)
        self.assertEqual(items, 1)

    def test_replay_can_re_normalise_an_equal_source_version(self):
        """显式 replay 只允许同版本重规范化，供字段映射修复回填使用。"""
        from bi_agent.sync import apply_trade, normalise_trade

        self._seed_shop()
        updated_at = datetime(2026, 9, 1, 13, 0, tzinfo=BEIJING)
        base = {
            "sid": "E_REPLAY", "userId": "S1", "tid": "C_REPLAY",
            "updTime": _ms(updated_at), "orders": [{"oid": "L_REPLAY"}],
        }
        corrected = {
            **base, "unifiedStatus": "CLOSED", "sysStatus": "FINISHED",
            "orders": [{"oid": "L_REPLAY", "type": 2}],
        }
        with self.conn.transaction():
            self.assertTrue(apply_trade(
                self.conn, normalise_trade(base), batch_id="original"))
            self.assertTrue(apply_trade(
                self.conn, normalise_trade(corrected), batch_id="replay", force=True))
            order = self.conn.execute(
                "SELECT active, unified_status FROM bi.orders WHERE erp_id='E_REPLAY'").fetchone()
            item = self.conn.execute(
                "SELECT source_type, line_kind FROM bi.order_items WHERE erp_id='E_REPLAY'").fetchone()
        self.assertEqual(order, (False, "CLOSED"))
        self.assertEqual(item, (2, "suite"))

    def test_closed_order_has_no_product_daily_row(self):
        from bi_agent.sync import apply_trade, normalise_trade

        self._seed_shop()
        paid_at = datetime(2026, 9, 2, 12, tzinfo=BEIJING)
        trade = normalise_trade({
            "sid": "E_CLOSED", "userId": "S1", "tid": "C_CLOSED",
            "updTime": _ms(paid_at), "payTime": _ms(paid_at),
            "unifiedStatus": "CLOSED",
            "orders": [{"oid": "L_CLOSED", "itemSysId": "P_CLOSED", "type": 0,
                        "num": "1", "payAmount": "10"}],
        })
        with self.conn.transaction():
            self.assertTrue(apply_trade(self.conn, trade, batch_id="closed"))
            count = self.conn.execute(
                "SELECT count(*) FROM reporting.v_product_daily WHERE product_id='P_CLOSED'").fetchone()[0]
        self.assertEqual(count, 0)

    def test_closed_paid_order_keeps_cash_payment_and_refund_match(self):
        from bi_agent.sync import apply_aftersale, apply_trade, normalise_aftersale, normalise_trade

        self._seed_shop()
        paid_at = datetime(2026, 9, 2, 12, tzinfo=BEIJING)
        closed = normalise_trade({
            "sid": "E_CASH", "userId": "S1", "tid": "C_CASH",
            "updTime": _ms(paid_at), "payTime": _ms(paid_at), "payAmount": "100",
            "unifiedStatus": "CLOSED",
            "orders": [{"oid": "L_CASH", "tid": "C_CASH", "itemSysId": "P_CASH",
                        "type": 0, "num": "1", "payAmount": "100"}],
        })
        refund = normalise_aftersale({
            "aftersaleId": "A_CASH", "userId": "S1", "tid": "C_CASH",
            "onlineStatus": 7, "status": 9, "rawRefundMoney": "30",
            "platformCompleteTime": _ms(paid_at), "modified": _ms(paid_at),
        })
        with self.conn.transaction():
            self.assertTrue(apply_trade(self.conn, closed, batch_id="cash"))
            self.assertTrue(apply_aftersale(self.conn, refund, batch_id="cash"))
            payment = self.conn.execute(
                "SELECT amount, verified FROM bi.order_payments WHERE commercial_id='C_CASH'").fetchone()
            matched = self.conn.execute(
                "SELECT matched FROM bi.aftersales WHERE aftersale_id='A_CASH'").fetchone()[0]
        self.assertEqual(payment, (Decimal("100"), True))
        self.assertTrue(matched)

    def test_closed_paid_split_orders_keep_one_verified_payment(self):
        from bi_agent.sync import apply_trade, normalise_trade

        self._seed_shop()
        paid_at = datetime(2026, 9, 2, 12, tzinfo=BEIJING)

        def closed_trade(erp_id: str, line_id: str, amount: str):
            return normalise_trade({
                "sid": erp_id, "userId": "S1", "tid": "C_CLOSED_SPLIT",
                "updTime": _ms(paid_at), "payTime": _ms(paid_at), "payAmount": amount,
                "unifiedStatus": "CLOSED",
                "orders": [{"oid": line_id, "tid": "C_CLOSED_SPLIT", "itemSysId": "P_SPLIT",
                            "type": 0, "num": "1", "payAmount": amount}],
            })

        with self.conn.transaction():
            self.assertTrue(apply_trade(self.conn, closed_trade("E_SPLIT_1", "L_SPLIT_1", "40"),
                                        batch_id="closed-split"))
            self.assertTrue(apply_trade(self.conn, closed_trade("E_SPLIT_2", "L_SPLIT_2", "60"),
                                        batch_id="closed-split"))
            payment = self.conn.execute(
                "SELECT amount, verified, basis FROM bi.order_payments "
                "WHERE commercial_id='C_CLOSED_SPLIT'").fetchone()
        self.assertEqual(payment, (Decimal("100"), True, "items"))

    def test_outstock_closed_payment_refund_and_refetch_converge(self):
        """淘系关闭单仍保留收款，退款只扣一次，已到原单不再反复补拉。"""
        from bi_agent.sync import (OUTSTOCK_SOURCE, apply_aftersale, apply_trade,
                                   normalise_aftersale, normalise_trade,
                                   unmatched_commercials)

        self._seed_shop("TB_CLOSED", platform="tb")
        paid = datetime(2026, 9, 2, 12, tzinfo=BEIJING)
        trade = normalise_trade({
            "sid": "E_TB", "userId": "TB_CLOSED", "tid": "C_TB",
            "updTime": _ms(paid), "payTime": _ms(paid), "payAmount": "100",
            "status": "TRADE_CLOSED", "sysStatus": "SELLER_SEND_GOODS",
            "orders": [{"id": "L_TB", "tid": "C_TB", "itemSysId": "P_TB",
                        "num": "1", "payAmount": "100"}],
        }, source=OUTSTOCK_SOURCE)
        self.assertFalse(trade["active"])
        self.assertTrue(apply_trade(self.conn, trade, batch_id="tb-closed"))
        refund = normalise_aftersale({
            "aftersaleId": "A_TB", "userId": "TB_CLOSED", "tid": "C_TB",
            "platformRefundId": "R_TB",
            "onlineStatus": 7, "status": 9, "rawRefundMoney": "30",
            "platformCompleteTime": _ms(paid), "modified": _ms(paid),
        })
        self.assertTrue(apply_aftersale(self.conn, refund, batch_id="tb-refund"))
        self.assertEqual(self.conn.execute(
            "SELECT paid_amount, refund_amount, cash_difference "
            "FROM reporting.v_shop_daily WHERE shop_id='TB_CLOSED'"
        ).fetchone(), (Decimal("100"), Decimal("30"), Decimal("70")))
        self.assertTrue(self.conn.execute(
            "SELECT matched FROM bi.aftersales WHERE aftersale_id='A_TB'"
        ).fetchone()[0])
        self.assertEqual(unmatched_commercials(self.conn, "TB_CLOSED"), set())
        self.assertEqual(self.conn.execute(
            "SELECT count(*) FROM reporting.v_product_daily WHERE shop_id='TB_CLOSED'"
        ).fetchone()[0], 0)

    def test_outstock_reconcile_updates_only_its_source_quality(self):
        """重核不能读取或修改淘系店遗留的交易查询源状态。"""
        from bi_agent.sync import ORDER_SOURCE, OUTSTOCK_SOURCE, _reconcile_shop

        self._seed_shop("TB_QUALITY", platform="tb")
        base = datetime(2026, 9, 5, tzinfo=BEIJING)
        self.conn.execute(
            "INSERT INTO bi.sync_state(source, entity, shop_id) "
            "VALUES (%s, 'orders', 'TB_QUALITY')", (ORDER_SOURCE,))
        _reconcile_shop(self.conn, self._client(), shop_id="TB_QUALITY", days=1,
                        run_end=base + timedelta(days=1), order_source=OUTSTOCK_SOURCE)
        self.assertEqual(dict(self.conn.execute(
            "SELECT source, quality_status FROM bi.sync_state "
            "WHERE shop_id='TB_QUALITY' AND entity='orders'"
        ).fetchall()), {ORDER_SOURCE: "unknown", OUTSTOCK_SOURCE: "passed"})

    def test_metric_semantics_migration_appends_line_kind_to_legacy_view(self):
        from pathlib import Path

        self.conn.execute("DROP VIEW reporting.v_product_daily")
        self.conn.execute(
            "CREATE OR REPLACE VIEW reporting.v_product_daily AS "
            "SELECT shop_id, (paid_at AT TIME ZONE 'Asia/Shanghai')::date AS day, product_id, "
            "sum(quantity) AS quantity, sum(gift_quantity) AS gift_quantity, "
            "sum(allocated_paid_amount) AS product_paid_amount, bool_and(allocation_verified) "
            "AS allocation_verified FROM bi.order_items "
            "WHERE active AND line_kind = 'sale' AND product_id IS NOT NULL "
            "AND allocated_paid_amount IS NOT NULL GROUP BY shop_id, day, product_id")
        migration = Path(__file__).parents[1] / "sql" / "003_kuaimai_metric_semantics.sql"
        self.conn.execute(migration.read_text(encoding="utf-8"))
        columns = self.conn.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema='reporting' AND table_name='v_product_daily' "
            "ORDER BY ordinal_position").fetchall()
        self.assertEqual([row[0] for row in columns], [
            "shop_id", "day", "product_id", "quantity", "gift_quantity",
            "product_paid_amount", "allocation_verified", "line_kind",
        ])

    def test_product_daily_keeps_suite_parent_with_kind_label(self):
        from bi_agent.sync import apply_trade, normalise_trade

        self._seed_shop()
        paid_at = datetime(2026, 9, 2, 12, tzinfo=BEIJING)
        suite = normalise_trade({
            "sid": "E_SUITE", "userId": "S1", "tid": "C_SUITE",
            "updTime": _ms(paid_at), "payTime": _ms(paid_at), "payAmount": "100",
            "orders": [{"oid": "L_SUITE", "tid": "C_SUITE", "itemSysId": "P_SUITE",
                        "type": 2, "num": "1", "payAmount": "100"}],
        })
        with self.conn.transaction():
            self.assertTrue(apply_trade(self.conn, suite, batch_id="suite"))
            row = self.conn.execute(
                "SELECT line_kind, quantity, product_paid_amount "
                "FROM reporting.v_product_daily WHERE product_id='P_SUITE'").fetchone()
        self.assertEqual(row, ("suite", Decimal("1"), Decimal("100")))

    def test_status_only_trade_update_deactivates_existing_product_rows(self):
        from bi_agent.sync import apply_trade, normalise_trade

        self._seed_shop()
        paid_at = datetime(2026, 9, 2, 12, tzinfo=BEIJING)
        active = normalise_trade({
            "sid": "E_STATUS", "userId": "S1", "tid": "C_STATUS",
            "updTime": _ms(paid_at), "payTime": _ms(paid_at),
            "orders": [{"oid": "L_STATUS", "tid": "C_STATUS", "itemSysId": "P_STATUS",
                        "type": 0, "num": "1", "payAmount": "10"}],
        })
        closed = normalise_trade({
            "sid": "E_STATUS", "userId": "S1", "tid": "C_STATUS",
            "updTime": _ms(paid_at + timedelta(hours=1)), "payTime": _ms(paid_at),
            "unifiedStatus": "CLOSED",
        })
        with self.conn.transaction():
            self.assertTrue(apply_trade(self.conn, active, batch_id="active"))
            self.assertTrue(apply_trade(self.conn, closed, batch_id="closed"))
            count = self.conn.execute(
                "SELECT count(*) FROM reporting.v_product_daily WHERE product_id='P_STATUS'").fetchone()[0]
        self.assertEqual(count, 0)

    def test_orphan_payment_marked_unverified(self):
        from bi_agent.sync import apply_trade

        self._seed_shop()
        pay_time = datetime(2026, 9, 1, 12, 0, tzinfo=BEIJING)
        with_tid = self._trade("E9", ["C9"], "50.00", pay_time,
                               datetime(2026, 9, 1, 12, 0, tzinfo=BEIJING), [
                                   {"oid": "E9-1", "tid": "C9", "itemSysId": "P_A",
                                    "num": "1", "payAmount": "50.00"},
                               ])
        without_tid = self._trade("E9", [], "50.00", pay_time,
                                  datetime(2026, 9, 2, 12, 0, tzinfo=BEIJING), [
                                      {"oid": "E9-1", "itemSysId": "P_A",
                                       "num": "1", "payAmount": "50.00"},
                                  ])
        with self.conn.transaction():
            self.assertTrue(apply_trade(self.conn, with_tid, batch_id="b1"))
            self.assertTrue(apply_trade(self.conn, without_tid, batch_id="b2"))
            row = self.conn.execute(
                "SELECT amount, verified, basis FROM bi.order_payments "
                "WHERE commercial_id='C9'").fetchone()
        self.assertIsNone(row[0])
        self.assertFalse(row[1])

    def test_missing_items_field_keeps_old_details(self):
        from bi_agent.sync import apply_trade

        self._seed_shop()
        pay_time = datetime(2026, 9, 1, 12, 0, tzinfo=BEIJING)
        trade = self._trade("E1", ["C1"], "300.00", pay_time,
                            datetime(2026, 9, 1, 13, 0, tzinfo=BEIJING), [
                                {"oid": "E1-1", "tid": "C1", "itemSysId": "P_A",
                                 "num": "2", "payAmount": "200.00"},
                            ])
        batch = "batch-items"
        with self.conn.transaction():
            self.assertTrue(apply_trade(self.conn, trade, batch_id=batch))
            stripped = dict(trade)
            stripped["items_present"] = False
            stripped["source_updated_at"] = datetime(2026, 9, 2, 13, 0, tzinfo=BEIJING)
            self.assertTrue(apply_trade(self.conn, stripped, batch_id=batch))
            items = self.conn.execute(
                "SELECT count(*) FROM bi.order_items WHERE erp_id='E1'").fetchone()[0]
        self.assertEqual(items, 1)

    def test_documented_trade_mapping_fields_persist(self):
        """A1/A3/A4/A6：新字段及派生规则应一起写入事实表。"""
        from bi_agent.sync import apply_trade, normalise_trade

        self._seed_shop()
        trade = normalise_trade({
            "sid": "E_MAP", "userId": "S1", "tid": "C_MAP",
            "updTime": _ms(datetime(2026, 9, 2, 12, tzinfo=BEIJING)),
            "unifiedStatus": "CLOSED", "sysStatus": "FINISHED",
            "splitType": 1, "splitSid": "E_PARENT",
            "orders": [{"id": "L_MAP", "oid": "P_MAP", "type": 2,
                        "num": "1", "payAmount": "10"}],
        })
        with self.conn.transaction():
            self.assertTrue(apply_trade(self.conn, trade, batch_id="mapping"))
            order = self.conn.execute(
                "SELECT unified_status, system_status, split_parent_id, active "
                "FROM bi.orders WHERE erp_id='E_MAP'").fetchone()
            item = self.conn.execute(
                "SELECT platform_line_id, source_type, line_kind "
                "FROM bi.order_items WHERE erp_id='E_MAP'").fetchone()
        self.assertEqual(order, ("CLOSED", "FINISHED", "E_PARENT", False))
        self.assertEqual(item, ("P_MAP", 2, "suite"))

    # -- 售后规范化与去重 ------------------------------------------------------

    def test_aftersale_platform_success_candidate(self):
        from bi_agent.sync import apply_aftersale, normalise_aftersale

        self._seed_shop()
        self.conn.execute(
            "INSERT INTO bi.orders(shop_id, erp_id, commercial_ids, source, "
            "source_updated_at, batch_id) VALUES ('S1','E1',ARRAY['C1'],"
            "'erp.trade.list.query', now(), 'b')")

        def aftersale(aid: str, **overrides):
            raw = {
                "aftersaleId": aid, "userId": "S1", "tid": "C1",
                "refundId": f"PR_{aid}", "rawRefundMoney": "30.00",
                "onlineStatus": 7, "status": 9,
                "platformCompleteTime": _ms(datetime(2026, 9, 2, 8, 0, tzinfo=BEIJING)),
                "modified": _ms(datetime(2026, 9, 2, 9, 0, tzinfo=BEIJING)),
            }
            raw.update(overrides)
            return normalise_aftersale(raw)

        pending = aftersale("A_pending", onlineStatus=2)
        self.assertFalse(pending["platform_success"])
        voided = aftersale("A_voided", status=10)
        self.assertFalse(voided["platform_success"])
        closed = aftersale("A_closed", onlineStatus=6)
        self.assertFalse(closed["platform_success"])
        success = aftersale("A_ok")
        self.assertTrue(success["platform_success"])

        with self.conn.transaction():
            self.assertTrue(apply_aftersale(self.conn, success, batch_id="b"))
            row = self.conn.execute(
                "SELECT matched, refund_canonical FROM bi.aftersales "
                "WHERE aftersale_id='A_ok'").fetchone()
        self.assertTrue(row[0])
        self.assertTrue(row[1])

    def test_aftersale_finished_and_multi_status_are_persisted_safely(self):
        from bi_agent.sync import apply_aftersale, normalise_aftersale

        self._seed_shop()
        finished = datetime(2026, 9, 2, 8, 0, tzinfo=BEIJING)
        record = normalise_aftersale({
            "aftersaleId": "A_MAP", "userId": "S1", "status": "2,10",
            "onlineStatus": 7, "modified": _ms(finished),
            "finished": _ms(finished), "platformCompleteTime": _ms(finished),
        })
        with self.conn.transaction():
            self.assertTrue(apply_aftersale(self.conn, record, batch_id="mapping"))
            row = self.conn.execute(
                "SELECT work_status, system_completed_at, platform_success "
                "FROM bi.aftersales WHERE aftersale_id='A_MAP'").fetchone()
        self.assertEqual(row, (2, finished, False))

    def test_aftersale_replay_can_re_normalise_an_equal_source_version(self):
        from bi_agent.sync import apply_aftersale, normalise_aftersale

        self._seed_shop()
        modified = datetime(2026, 9, 2, 8, 0, tzinfo=BEIJING)
        base = {
            "aftersaleId": "A_REPLAY", "userId": "S1", "status": 9,
            "onlineStatus": 7, "modified": _ms(modified),
        }
        corrected = {**base, "finished": _ms(modified), "status": "2,10"}
        with self.conn.transaction():
            self.assertTrue(apply_aftersale(
                self.conn, normalise_aftersale(base), batch_id="original"))
            self.assertTrue(apply_aftersale(
                self.conn, normalise_aftersale(corrected), batch_id="replay", force=True))
            row = self.conn.execute(
                "SELECT system_completed_at, platform_success FROM bi.aftersales "
                "WHERE aftersale_id='A_REPLAY'").fetchone()
        self.assertEqual(row, (modified, False))

    def test_aftersale_replay_does_not_regress_an_older_source_version(self):
        from bi_agent.sync import apply_aftersale, normalise_aftersale

        self._seed_shop()
        newer = datetime(2026, 9, 3, 8, 0, tzinfo=BEIJING)
        older = datetime(2026, 9, 2, 8, 0, tzinfo=BEIJING)
        current = normalise_aftersale({
            "aftersaleId": "A_NEWER", "userId": "S1", "status": 9,
            "onlineStatus": 7, "modified": _ms(newer), "finished": _ms(newer),
        })
        stale = normalise_aftersale({
            "aftersaleId": "A_NEWER", "userId": "S1", "status": "2,10",
            "onlineStatus": 7, "modified": _ms(older), "finished": _ms(older),
        })
        with self.conn.transaction():
            self.assertTrue(apply_aftersale(self.conn, current, batch_id="newer"))
            self.assertFalse(apply_aftersale(self.conn, stale, batch_id="replay", force=True))
            finished = self.conn.execute(
                "SELECT system_completed_at FROM bi.aftersales WHERE aftersale_id='A_NEWER'").fetchone()[0]
        self.assertEqual(finished, newer)

    def test_all_disabled_shops_return_no_metric_scope(self):
        import time as time_module

        from bi_agent.metrics import QueryRequest, query_business

        self._seed_shop()
        self.conn.execute("UPDATE bi.shops SET enabled=false WHERE shop_id='S1'")
        result = query_business(
            self.conn,
            QueryRequest(start="2026-09-01", end="2026-09-02", shop_ids=["S1"],
                         metrics=["paid_amount"]),
            allowed_shop_ids=frozenset({"S1"}),
            now=datetime(2026, 9, 3, tzinfo=BEIJING),
            deadline=time_module.monotonic() + 30,
        )
        self.assertEqual(result.status, "missing_data")
        self.assertEqual(result.data, [])
        self.assertIn("所选店铺均已停用，无法查询", result.limitations)

    def test_duplicate_platform_refund_dedup(self):
        from bi_agent.sync import apply_aftersale, mark_refund_canonical, normalise_aftersale

        self._seed_shop()
        complete = _ms(datetime(2026, 9, 2, 8, 0, tzinfo=BEIJING))
        modified = _ms(datetime(2026, 9, 2, 9, 0, tzinfo=BEIJING))

        def raw(aid: str, amount: str, refund_id: str):
            return normalise_aftersale({
                "aftersaleId": aid, "userId": "S1", "tid": "C1",
                "refundId": refund_id, "rawRefundMoney": amount,
                "onlineStatus": 7, "status": 9,
                "platformCompleteTime": complete, "modified": modified,
            })

        with self.conn.transaction():
            # 同金额重复：只确认一次
            apply_aftersale(self.conn, raw("A1", "30.00", "PR_DUP"), batch_id="b")
            apply_aftersale(self.conn, raw("A2", "30.00", "PR_DUP"), batch_id="b")
            mark_refund_canonical(self.conn, "S1", {"PR_DUP"})
            rows = self.conn.execute(
                "SELECT aftersale_id, refund_canonical FROM bi.aftersales "
                "WHERE platform_refund_id='PR_DUP' ORDER BY aftersale_id").fetchall()
        self.assertEqual(rows[0][0], "A1")
        self.assertTrue(rows[0][1])
        self.assertFalse(rows[1][1])

        with self.conn.transaction():
            # 金额不一致组：未验证，禁止取最大值
            apply_aftersale(self.conn, raw("A3", "10.00", "PR_MIX"), batch_id="b")
            apply_aftersale(self.conn, raw("A4", "20.00", "PR_MIX"), batch_id="b")
            mark_refund_canonical(self.conn, "S1", {"PR_MIX"})
            rows = self.conn.execute(
                "SELECT refund_canonical FROM bi.aftersales "
                "WHERE platform_refund_id='PR_MIX'").fetchall()
        self.assertFalse(any(row[0] for row in rows))

    def test_aftersale_without_order_still_stored(self):
        from bi_agent.sync import apply_aftersale, normalise_aftersale

        self._seed_shop()
        record = normalise_aftersale({
            "aftersaleId": "A_X", "userId": "S1", "tid": "C_UNKNOWN",
            "refundId": "PR_X", "rawRefundMoney": "12.00",
            "onlineStatus": 7, "status": 9,
            "platformCompleteTime": _ms(datetime(2026, 9, 2, 8, 0, tzinfo=BEIJING)),
            "modified": _ms(datetime(2026, 9, 2, 9, 0, tzinfo=BEIJING)),
        })
        with self.conn.transaction():
            self.assertTrue(apply_aftersale(self.conn, record, batch_id="b"))
            row = self.conn.execute(
                "SELECT matched, raw_platform_amount FROM bi.aftersales "
                "WHERE aftersale_id='A_X'").fetchone()
        self.assertFalse(row[0])
        self.assertEqual(row[1], Decimal("12.00"))

    def test_invalid_records_rejected(self):
        from bi_agent.sync import apply_trade, normalise_trade

        self._seed_shop()
        trade = normalise_trade({"sid": "", "userId": "S1", "updTime": _ms(datetime(2026, 9, 1, tzinfo=BEIJING))})
        self.assertEqual(trade["normalization_status"], "invalid")
        with self.conn.transaction():
            self.assertFalse(apply_trade(self.conn, trade, batch_id="b"))


    # -- 窗口事务与水位（任务4） ----------------------------------------------

    def _client(self):
        from bi_agent.config import SyncSettings
        from bi_agent.kuaimai import KuaimaiClient
        from pydantic import SecretStr

        settings = SyncSettings(
            writer_dsn=SecretStr("postgresql://localhost/bi_agent_test"),
            shop_ids=frozenset({"S1"}),
            app_key=SecretStr("k"), app_secret=SecretStr("s"),
            access_token=SecretStr("t"), refresh_token=SecretStr("r"),
        )
        return KuaimaiClient(settings, httpx.Client(transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"success": True, "total": 0}))))

    def _state_row(self, entity: str, shop_id: str = "S1", source: str | None = None):
        if source is None:
            source = "erp.trade.list.query" if entity == "orders" else "erp.aftersale.list.query"
        return self.conn.execute(
            "SELECT watermark, covered, data_as_of, last_error_code FROM bi.sync_state "
            "WHERE source=%s AND entity=%s AND shop_id=%s",
            (source, entity, shop_id),
        ).fetchone()

    def test_interrupted_window_does_not_advance_watermark(self):
        """4.1：分页中断后水位不动、零业务；不能把失败变成零数据。"""
        from bi_agent.kuaimai import KuaimaiError
        from bi_agent.sync import Window, sync_window

        self._seed_shop()
        old_watermark = datetime(2026, 9, 5, 0, 0, tzinfo=BEIJING)
        self.conn.execute(
            "INSERT INTO bi.sync_state(source, entity, shop_id, watermark) "
            "VALUES ('erp.trade.list.query', 'orders', 'S1', %s)",
            (old_watermark,),
        )

        def interrupted_fetch(*args, **kwargs):
            yield {"sid": "E1", "userId": "S1", "updTime": 1788537600000,
                   "tid": "C1", "payAmount": "100.00", "orders": []}
            raise KuaimaiError("timeout")

        window = Window(datetime(2026, 9, 5, 0, 0, tzinfo=BEIJING),
                        datetime(2026, 9, 6, 0, 0, tzinfo=BEIJING))
        client = self._client()
        with patch("bi_agent.sync.fetch_window", side_effect=interrupted_fetch):
            with self.assertRaises(KuaimaiError):
                sync_window(self.conn, client, entity="orders", shop_id="S1",
                            window=window, mode="incremental")
        self.assertEqual(self._state_row("orders")[0], old_watermark)
        count = self.conn.execute(
            "SELECT count(*) FROM bi.orders WHERE shop_id='S1'").fetchone()[0]
        self.assertEqual(count, 0)
        self.assertEqual(self.conn.execute(
            "SELECT count(*) FROM bi.sync_batches WHERE shop_id='S1'").fetchone()[0], 0,
            "分页中断不得留下批次凭证")

    def test_sync_window_persists_batch_evidence_for_its_window(self):
        """成功窗口要留下批次凭证，而且必须标清自己是哪个时间口径。

        增量窗口是修改时间，回填/重放/对账才是业务时间；两者混在一列
        就会被当成“这段时间已覆盖”的假凭证。
        """
        from bi_agent.sync import Window, sync_window

        self._seed_shop()
        base = datetime(2026, 9, 5, 0, 0, tzinfo=BEIJING)
        records = [
            {"sid": "E1", "userId": "S1", "updTime": _ms(base + timedelta(hours=2)),
             "tid": "C1", "payAmount": "100.00",
             "payTime": _ms(base + timedelta(hours=1)),
             "orders": [{"oid": "E1-1", "tid": "C1", "itemSysId": "P_A",
                          "num": "1", "payAmount": "100.00"}]},
        ]
        window = Window(base, base + timedelta(days=1))

        def full_fetch(*args, **kwargs):
            yield from records

        with patch("bi_agent.sync.fetch_window", side_effect=full_fetch):
            accepted = sync_window(self.conn, self._client(), entity="orders",
                                   shop_id="S1", window=window, mode="incremental")
        self.assertEqual(accepted, 1)

        row = self.conn.execute(
            "SELECT mode, window_kind, row_count, lower(business_window), "
            "upper(business_window) FROM bi.sync_batches "
            "WHERE shop_id='S1' AND entity='orders'").fetchone()
        self.assertEqual((row[0], row[1], row[2]), ("incremental", "modified", 1))
        self.assertEqual((row[3], row[4]), (window.start, window.end))

    def test_backfill_batch_evidence_is_marked_business_time(self):
        from bi_agent.sync import Window, sync_window

        self._seed_shop()
        base = datetime(2026, 9, 5, 0, 0, tzinfo=BEIJING)
        records = [
            {"sid": "E1", "userId": "S1", "updTime": _ms(base + timedelta(hours=2)),
             "tid": "C1", "payAmount": "100.00",
             "payTime": _ms(base + timedelta(hours=1)),
             "orders": [{"oid": "E1-1", "tid": "C1", "itemSysId": "P_A",
                          "num": "1", "payAmount": "100.00"}]},
        ]
        window = Window(base, base + timedelta(days=1))

        def full_fetch(*args, **kwargs):
            yield from records

        with patch("bi_agent.sync.fetch_window", side_effect=full_fetch):
            sync_window(self.conn, self._client(), entity="orders",
                        shop_id="S1", window=window, mode="backfill")

        kind = self.conn.execute(
            "SELECT window_kind FROM bi.sync_batches "
            "WHERE shop_id='S1' AND entity='orders'").fetchone()[0]
        self.assertEqual(kind, "business", "回填窗口是业务时间口径")

    def test_reconcile_window_evidence_enables_quality_promotion(self):
        """完整链路：reconcile 先落业务凭证，质量才有资格升 passed。"""
        from bi_agent.data_quality import reconcile_source_quality
        from bi_agent.sync import Window, sync_window

        self._seed_shop()
        base = datetime(2026, 9, 5, 0, 0, tzinfo=BEIJING)
        records = [
            {"sid": "E1", "userId": "S1", "updTime": _ms(base + timedelta(hours=2)),
             "tid": "C1", "payAmount": "100.00",
             "payTime": _ms(base + timedelta(hours=1)),
             "orders": [{"oid": "E1-1", "tid": "C1", "itemSysId": "P_A",
                          "num": "1", "payAmount": "100.00"}]},
        ]
        window = Window(base, base + timedelta(days=1))

        def full_fetch(*args, **kwargs):
            yield from records

        with patch("bi_agent.sync.fetch_window", side_effect=full_fetch):
            sync_window(self.conn, self._client(), entity="orders",
                        shop_id="S1", window=window, mode="reconcile")

        self.assertEqual(reconcile_source_quality(
            self.conn, shop_id="S1", entity="orders",
            start=base, end=base + timedelta(days=1)), "passed")
        # 凭证必须在同一窗口内，否则整段窗口都要维持 unknown。
        self.assertEqual(reconcile_source_quality(
            self.conn, shop_id="S1", entity="orders",
            start=base + timedelta(days=3), end=base + timedelta(days=4)), "unknown")

    def test_successful_incremental_advances_watermark_and_covers(self):
        """4.6：补跑→覆盖缺口闭合；连续增量扩展业务覆盖终点。"""
        from bi_agent.sync import Window, sync_window

        self._seed_shop()
        base = datetime(2026, 9, 5, 0, 0, tzinfo=BEIJING)
        prior_end = base  # 已有覆盖终点与水位一致：连续增量
        self.conn.execute(
            "INSERT INTO bi.sync_state(source, entity, shop_id, watermark, covered) "
            "VALUES ('erp.trade.list.query', 'orders', 'S1', %s, "
            "tstzmultirange(tstzrange(%s, %s, '[)')))",
            (base, base - timedelta(days=5), prior_end))

        records = [
            {"sid": "E1", "userId": "S1", "updTime": _ms(base + timedelta(hours=2)),
             "tid": "C1", "payAmount": "100.00",
             "payTime": _ms(base + timedelta(hours=1)),
             "orders": [{"oid": "E1-1", "tid": "C1", "itemSysId": "P_A",
                          "num": "1", "payAmount": "100.00"}]},
        ]
        window = Window(base, base + timedelta(days=1))

        def full_fetch(*args, **kwargs):
            yield from records

        client = self._client()
        with patch("bi_agent.sync.fetch_window", side_effect=full_fetch):
            accepted = sync_window(self.conn, client, entity="orders", shop_id="S1",
                                   window=window, mode="incremental")
        self.assertEqual(accepted, 1)
        watermark, covered, _, _ = self._state_row("orders")
        self.assertEqual(watermark, window.end)
        contains = self.conn.execute(
            "SELECT covered @> tstzmultirange(tstzrange(%s, %s, '[)')) "
            "FROM bi.sync_state WHERE source='erp.trade.list.query' "
            "AND entity='orders' AND shop_id='S1'",
            (base, base + timedelta(hours=1, seconds=1)),
        ).fetchone()[0]
        self.assertTrue(contains)
        # 覆盖不能越过已观察到的业务时间盲目延伸整天
        beyond = self.conn.execute(
            "SELECT covered @> tstzmultirange(tstzrange(%s, %s, '[)')) "
            "FROM bi.sync_state WHERE source='erp.trade.list.query' "
            "AND entity='orders' AND shop_id='S1'",
            (base + timedelta(hours=2), window.end),
        ).fetchone()[0]
        self.assertFalse(beyond)

    def test_backfill_establishes_watermark_so_incremental_can_start(self):
        """C-2：backfill 结束必须留下非 epoch 水位，否则后续增量永远 SystemExit。"""
        from bi_agent.sync import _backfill_shop, _incremental_shop

        self._seed_shop()
        t0 = datetime(2026, 9, 5, 0, 0, tzinfo=BEIJING)
        t1 = datetime(2026, 9, 5, 6, 0, tzinfo=BEIJING)
        client = self._client()

        # 拉取本身不是本用例要验的：空页让 sync_window 只跑状态写入。
        with patch("bi_agent.sync.fetch_window",
                   side_effect=lambda *args, **kwargs: iter(())):
            _backfill_shop(self.conn, client, shop_id="S1", days=1, t0=t0,
                           now=lambda: t1)

        for entity in ("orders", "aftersales_occurrence"):
            watermark, _covered, data_as_of, error_code = self._state_row(entity)
            self.assertGreater(watermark, datetime(1970, 1, 2, tzinfo=BEIJING))
            self.assertEqual(watermark, t1)
            # data_as_of 必须是回填结束时刻，不是开工瞬间。
            self.assertEqual(data_as_of, t1)
            self.assertIsNone(error_code)

        # 关键回归：旧行为下水位停在 epoch，这里会直接 SystemExit。
        # 必须在两个实体的断言都跑完之后再推增量：_incremental_shop 会把两条水位
        # 一起推到 run_end，放进循环里会让第二个实体的 assertEqual(watermark, t1) 失真。
        stats = _incremental_shop(self.conn, client, shop_id="S1",
                                  run_end=t1 + timedelta(hours=1))
        self.assertEqual(stats["orders"], 0)
        self.assertEqual(self._state_row("orders")[0], t1 + timedelta(hours=1))

    def test_row_cap_does_not_silently_truncate_totals(self):
        """C-3：日行数远超 MAX_ROWS 时，真实SQL下 total/shop 仍必须是完整汇总。"""
        import time as time_module
        from datetime import date

        from bi_agent.metrics import MAX_ROWS, QueryRequest, query_business

        start = date(2025, 9, 1)
        end = start + timedelta(days=366)          # MAX_SPAN_DAYS 上限
        shops = ("S1", "S2", "S3")
        start_ts = datetime(start.year, start.month, start.day, tzinfo=BEIJING)
        end_ts = datetime(end.year, end.month, end.day, tzinfo=BEIJING)
        groups = 366 * len(shops)
        self.assertGreater(groups, MAX_ROWS)

        for shop_id in shops:
            self.conn.execute(
                "INSERT INTO bi.shops(shop_id, platform, display_name) "
                "VALUES (%s, 'fxg', %s) ON CONFLICT (shop_id) DO NOTHING",
                (shop_id, shop_id))
            # 行上限检查走在能力门禁之后：这些店必须被当成“已取证”，
            # 否则测到的是能力而不是截断。
            set_capabilities(self.conn, shop_id, *ALL_CAPABILITIES)
            self.conn.execute(
                "INSERT INTO bi.sync_state(source, entity, shop_id, watermark, covered, "
                "data_as_of) VALUES ('erp.trade.list.query', 'orders', %s, %s, "
                "tstzmultirange(tstzrange(%s, %s, '[)')), %s) "
                "ON CONFLICT (source, entity, shop_id) DO UPDATE SET "
                "covered = EXCLUDED.covered, data_as_of = EXCLUDED.data_as_of",
                (shop_id, end_ts, start_ts, end_ts, end_ts))
        self.conn.execute(
            """
            INSERT INTO bi.order_payments(shop_id, commercial_id, paid_at, amount,
                                          currency, basis, verified)
            SELECT s.shop_id,
                   'C-' || s.shop_id || '-' || d.n,
                   %s::timestamptz + d.n * interval '1 day' + interval '12 hours',
                   100.00, 'CNY', 'head', true
            FROM (VALUES ('S1'), ('S2'), ('S3')) AS s(shop_id),
                 generate_series(0, 365) AS d(n)
            """,
            (start_ts,))

        def run(group_by, metrics):
            request = QueryRequest(start=start, end=end, shop_ids=list(shops),
                                   metrics=list(metrics), group_by=group_by)
            return query_business(self.conn, request, allowed_shop_ids=frozenset(shops),
                                  now=end_ts, deadline=time_module.monotonic() + 30)

        total = run("total", ["paid_amount", "paid_orders", "aov"])
        self.assertEqual(total.status, "ok", total.limitations)
        # 只拿得到前 500 个日行时会被读成 50000（且标 status=ok）。
        self.assertEqual(Decimal(total.data[0]["paid_amount"]), Decimal(100) * groups)
        self.assertEqual(total.data[0]["paid_orders"], groups)
        self.assertEqual(Decimal(total.data[0]["aov"]), Decimal("100"))

        by_shop = run("shop", ["paid_amount"])
        self.assertEqual(by_shop.status, "ok", by_shop.limitations)
        self.assertEqual([row["shop_id"] for row in by_shop.data], list(shops))
        for row in by_shop.data:
            self.assertEqual(Decimal(row["paid_amount"]), Decimal(100) * 366)

        # 逐日分组确实超上限：宁可拒绝参数，也不给一个偏低的数。
        day = run("day", ["paid_amount"])
        self.assertEqual(day.status, "invalid_parameters")
        self.assertEqual(day.data, [])

    def _split_pair(self, *, sibling_upd: datetime):
        """已核验的单头支付 + 一条行明细未到齐的兄弟拆单（undetermined 来源）。

        返回 (支付时刻, 本次守卫拦截数)；计数是进程级全局量，用差值断言才不被其它用例干扰。
        """
        from bi_agent.sync import GUARD_STATS, apply_trade

        self._seed_shop()
        pay_time = datetime(2026, 9, 2, 10, 0, tzinfo=BEIJING)
        first = self._trade("E3", ["C3"], "40.00", pay_time,
                            datetime(2026, 9, 2, 11, 0, tzinfo=BEIJING), [
                                {"oid": "E3-1", "tid": "C3", "itemSysId": "P_A",
                                 "num": "1", "payAmount": "40.00"}])
        with self.conn.transaction():
            self.assertTrue(apply_trade(self.conn, first, batch_id="guard-a"))
        before = GUARD_STATS.payment_downgrade_blocked
        sibling = self._trade("E4", ["C3"], "60.00", pay_time, sibling_upd, [])
        with self.conn.transaction():
            self.assertTrue(apply_trade(self.conn, sibling, batch_id="guard-b"))
        return pay_time, GUARD_STATS.payment_downgrade_blocked - before

    def _payment_row(self):
        return self.conn.execute(
            "SELECT amount, verified, basis FROM bi.order_payments "
            "WHERE shop_id='S1' AND commercial_id='C3'").fetchone()

    def test_incomplete_sibling_cannot_wipe_a_verified_payment(self):
        """C-5：兄弟拆单未到齐产生的 undetermined 不得清零已核验收入。"""
        pay_time, blocked = self._split_pair(
            sibling_upd=datetime(2026, 9, 2, 10, 30, tzinfo=BEIJING))

        self.assertEqual(self._payment_row(), (Decimal("40.00"), True, "head"))
        self.assertEqual(blocked, 1)
        # 行级核验证据也必须保留（被拦下时不再抹掉）
        self.assertTrue(self.conn.execute(
            "SELECT allocation_verified FROM bi.order_items "
            "WHERE shop_id='S1' AND erp_id='E3'").fetchone()[0])
        # 全域收入（v_shop_daily 带 WHERE verified）没有无声消失
        self.assertEqual(self.conn.execute(
            "SELECT paid_amount FROM reporting.v_shop_daily "
            "WHERE shop_id='S1' AND day = %s", (pay_time.date(),)).fetchone()[0],
            Decimal("40.00"))

    def test_strictly_fresher_undetermined_evidence_still_downgrades(self):
        """守卫只拦“旧证据覆盖新事实”；更新的真证据仍应能推翻核验。"""
        _pay_time, blocked = self._split_pair(
            sibling_upd=datetime(2026, 9, 2, 12, 0, tzinfo=BEIJING))

        self.assertEqual(self._payment_row(), (None, False, "undetermined"))
        self.assertEqual(blocked, 0)

    def test_outstock_channel_persists_and_isolates_state(self):
        """淘系出库通道落库：PII字段不入库、状态行与交易查询通道互不干扰。"""
        from bi_agent.sync import (
            OUTSTOCK_SOURCE, PII_FORBIDDEN_FIELDS, Window, sync_window)

        self._seed_shop("TB1", platform="tb")
        base = datetime(2026, 8, 16, 0, 0, tzinfo=BEIJING)
        for source in (OUTSTOCK_SOURCE, "erp.trade.list.query"):
            self.conn.execute(
                "INSERT INTO bi.sync_state(source, entity, shop_id, watermark) "
                "VALUES (%s, 'orders', 'TB1', %s)", (source, base))
        raw = {
            "sid": 9001, "tid": "C-TB", "userId": "TB1",
            "payAmount": "16.90", "payment": "16.90", "cost": "10.00",
            "grossProfit": "6.90",
            "payTime": _ms(base + timedelta(hours=1)),
            "modified": _ms(base + timedelta(hours=2)),
            "updTime": _ms(base + timedelta(hours=2)),
            "status": "WAIT_BUYER_CONFIRM_GOODS", "sysStatus": "SELLER_SEND_GOODS",
            "splitSid": 9000,
            "buyerNick": "PII-buyerNick", "receiverName": "PII-receiverName",
            "receiverMobile": "PII-receiverMobile", "openUid": "PII-openUid",
            "shopName": "PII-shopName",
            "orders": [{"id": 1, "oid": "L1", "tid": "C-TB", "itemSysId": 10,
                         "skuSysId": 20, "num": "1", "giftNum": 0,
                         "payAmount": "16.90", "payment": "16.90", "cost": "10.00",
                         "buyerNick": "PII-buyerNick"}],
        }
        window = Window(base, base + timedelta(days=1))
        with patch("bi_agent.sync.fetch_window",
                   side_effect=lambda *a, **k: iter([raw])):
            accepted = sync_window(self.conn, self._client(), entity="orders",
                                   shop_id="TB1", window=window,
                                   mode="incremental", order_source=OUTSTOCK_SOURCE)
        self.assertEqual(accepted, 1)
        # 出库通道水位前进；同店交易查询通道状态行不动（sync_state 主键含 source）
        self.assertEqual(self._state_row("orders", "TB1", OUTSTOCK_SOURCE)[0], window.end)
        self.assertEqual(self._state_row("orders", "TB1", "erp.trade.list.query")[0], base)
        order = self.conn.execute(
            "SELECT source, split_parent_id, commercial_ids, raw_pay_amount "
            "FROM bi.orders WHERE shop_id='TB1' AND erp_id='9001'").fetchone()
        self.assertEqual(order[0], OUTSTOCK_SOURCE)
        self.assertEqual(order[1], "9000")
        self.assertEqual(order[2], ["C-TB"])
        self.assertEqual(order[3], Decimal("16.90"))
        payment = self.conn.execute(
            "SELECT basis, verified, amount FROM bi.order_payments "
            "WHERE shop_id='TB1' AND commercial_id='C-TB'").fetchone()
        self.assertEqual(payment[0], "head")
        self.assertTrue(payment[1])
        self.assertEqual(payment[2], Decimal("16.90"))
        # 表列集合红线：任何事实表都不存在 PII 列，落库路径无从引用
        pii_columns = self.conn.execute(
            "SELECT table_name, column_name FROM information_schema.columns "
            "WHERE table_schema='bi'").fetchall()
        leaked = {column for _table, column in pii_columns
                  if column in PII_FORBIDDEN_FIELDS}
        self.assertEqual(leaked, set())

    def test_fetch_window_cursor_pagination_contract(self):
        """4.2：首请求不传cursor；后续传上一页cursor；hasNext=true无游标报错。"""
        from bi_agent.kuaimai import KuaimaiError
        from bi_agent.sync import Window, fetch_window

        requests: list[dict] = []
        pages = [
            {"success": True, "total": 2, "hasNext": True, "cursor": "c1",
             "list": [{"sid": "E1", "userId": "S1", "updTime": 1, "payAmount": "1"}]},
            {"success": True, "total": 2, "hasNext": False,
             "list": [{"sid": "E2", "userId": "S1", "updTime": 2, "payAmount": "2"}]},
        ]

        class FakeClient:
            def call(self, method, params):
                requests.append(dict(params))
                return pages[len(requests) - 1]

        window = Window(datetime(2026, 9, 5, 0, 0, tzinfo=BEIJING),
                        datetime(2026, 9, 6, 0, 0, tzinfo=BEIJING))
        rows = list(fetch_window(FakeClient(), entity="orders", shop_id="S1",
                                 window=window, mode="incremental"))
        self.assertEqual(len(rows), 2)
        self.assertNotIn("cursor", requests[0])
        self.assertEqual(requests[1]["cursor"], "c1")
        self.assertEqual(requests[0]["timeType"], "upd_time")
        self.assertEqual(requests[0]["queryType"], "0")

        pages_stuck = [pages[0], pages[0]]

        class StuckClient:
            def __init__(self):
                self.n = 0

            def call(self, method, params):
                self.n += 1
                return pages_stuck[min(self.n - 1, 1)]

        with self.assertRaises(KuaimaiError):
            list(fetch_window(StuckClient(), entity="orders", shop_id="S1",
                              window=window, mode="incremental"))

    def test_fetch_window_aftersales_contract(self):
        """4.2：售后分页不附订单参数，按total判断末页。"""
        from bi_agent.sync import Window, fetch_window

        requests: list[dict] = []
        pages = [
            {"success": True, "total": 1,
             "list": [{"aftersaleId": "A1", "userId": "S1", "rawRefundMoney": "30"}]},
        ]

        class FakeClient:
            def call(self, method, params):
                requests.append(dict(params))
                return pages[0]

        window = Window(datetime(2026, 9, 5, 0, 0, tzinfo=BEIJING),
                        datetime(2026, 9, 6, 0, 0, tzinfo=BEIJING))
        rows = list(fetch_window(FakeClient(), entity="aftersales_occurrence",
                                 shop_id="S1", window=window, mode="backfill"))
        self.assertEqual(len(rows), 1)
        self.assertNotIn("timeType", requests[0])
        self.assertNotIn("useHasNext", requests[0])
        self.assertIn("startPlatformCompleteTime", requests[0])
        self.assertEqual(requests[0]["asVersion"], "2")




# ---------------------------------------------------------------------------
# 任务5合成数据集（人工答案基准，无PII），供指标测试与验收脚本共用
# ---------------------------------------------------------------------------

FROZEN_NOW = datetime(2026, 9, 8, 9, 0, tzinfo=BEIJING)
FROZEN_CUTOFF = datetime(2026, 9, 8, 0, 0, tzinfo=BEIJING)
COVERAGE_START = datetime(2026, 8, 25, 0, 0, tzinfo=BEIJING)
COVERAGE_END = datetime(2026, 9, 8, 0, 0, tzinfo=BEIJING)


def seed_business_case(conn) -> None:
    """冻结时刻2026-09-08 09:00+08；覆盖2026-08-25至2026-09-08；金额均为元。"""
    # 种子代表“已逐源取证并授予能力的参考场景”：S1 拿到全部指标能力，
    # S2 是未授权对照店（能力为空）。能力标签与指标同名，旧的 orders 一类实体标签
    # 不授予任何指标（设计 §4），所以种子必须显式写标签。
    conn.execute(
        "INSERT INTO bi.shops(shop_id, platform, display_name) VALUES "
        "('S1','fxg','店铺A'), ('S2','fxg','店铺B') "
        "ON CONFLICT (shop_id) DO UPDATE SET platform = EXCLUDED.platform, "
        "display_name = EXCLUDED.display_name")
    set_capabilities(conn, "S1", *ALL_CAPABILITIES)
    set_capabilities(conn, "S2")

    def pay_time(day: int, hour: int) -> datetime:
        return datetime(2026, 9, day, hour, tzinfo=BEIJING)

    def aug(day: int, hour: int) -> datetime:
        return datetime(2026, 8, day, hour, tzinfo=BEIJING)

    trades = [
        # C0 08-31 500：A 1件500
        {"sid": "E0", "userId": "S1", "tid": "C0", "payAmount": "500.00",
         "payTime": _ms(aug(31, 12)), "updTime": _ms(aug(31, 13)),
         "orders": [{"oid": "E0-1", "tid": "C0", "itemSysId": "P_A", "num": "1",
                      "payAmount": "500.00"}]},
        # C1 09-01 300：A 2件200 + B 1件100
        {"sid": "E1", "userId": "S1", "tid": "C1", "payAmount": "300.00",
         "payTime": _ms(pay_time(1, 10)), "updTime": _ms(pay_time(1, 11)),
         "orders": [{"oid": "E1-1", "tid": "C1", "itemSysId": "P_A", "num": "2",
                      "payAmount": "200.00"},
                     {"oid": "E1-2", "tid": "C1", "itemSysId": "P_B", "num": "1",
                      "payAmount": "100.00"}]},
        # C2 09-01 200：A 2件200
        {"sid": "E2", "userId": "S1", "tid": "C2", "payAmount": "200.00",
         "payTime": _ms(pay_time(1, 15)), "updTime": _ms(pay_time(1, 16)),
         "orders": [{"oid": "E2-1", "tid": "C2", "itemSysId": "P_A", "num": "2",
                      "payAmount": "200.00"}]},
        # C3 09-02 100：拆为E3/E4
        {"sid": "E3", "userId": "S1", "tid": "C3", "payAmount": "40.00",
         "payTime": _ms(pay_time(2, 11)), "updTime": _ms(pay_time(2, 12)),
         "orders": [{"oid": "E3-1", "tid": "C3", "itemSysId": "P_A", "num": "1",
                      "payAmount": "40.00"}]},
        {"sid": "E4", "userId": "S1", "tid": "C3", "payAmount": "60.00",
         "payTime": _ms(pay_time(2, 11)), "updTime": _ms(pay_time(2, 12)),
         "orders": [{"oid": "E4-1", "tid": "C3", "itemSysId": "P_B", "num": "1",
                      "payAmount": "60.00"}]},
        # C4/C5 09-03：合入E5
        {"sid": "E5", "userId": "S1", "tid": "C4", "tids": "C4,C5",
         "payAmount": "200.00", "payTime": _ms(pay_time(3, 9)),
         "updTime": _ms(pay_time(3, 10)),
         "orders": [{"oid": "E5-1", "tid": "C4", "itemSysId": "P_A", "num": "1",
                      "payAmount": "80.00"},
                     {"oid": "E5-2", "tid": "C5", "itemSysId": "P_B", "num": "1",
                      "payAmount": "120.00"}]},
        # C6 09-05 200：A 1件80 + B 1件120
        {"sid": "E6", "userId": "S1", "tid": "C6", "payAmount": "200.00",
         "payTime": _ms(pay_time(5, 20)), "updTime": _ms(pay_time(5, 21)),
         "orders": [{"oid": "E6-1", "tid": "C6", "itemSysId": "P_A", "num": "1",
                      "payAmount": "80.00"},
                     {"oid": "E6-2", "tid": "C6", "itemSysId": "P_B", "num": "1",
                      "payAmount": "120.00"}]},
    ]
    from bi_agent.sync import apply_trade, normalise_trade

    for raw in trades:
        trade = normalise_trade(raw)
        assert trade["normalization_status"] == "normal", raw
        if not apply_trade(conn, trade, batch_id="seed"):
            # 同版本幂等重放：确认记录已存在
            assert conn.execute(
                "SELECT 1 FROM bi.orders WHERE shop_id=%s AND erp_id=%s",
                (trade["shop_id"], trade["erp_id"])).fetchone()

    from bi_agent.sync import apply_aftersale, normalise_aftersale

    def refund(aid: str, tid: str | None, refund_id: str, amount: str,
               complete: datetime | None, online_status: int, status: int,
               modified: datetime) -> None:
        raw = {"aftersaleId": aid, "userId": "S1", "rawRefundMoney": amount,
               "onlineStatus": online_status, "status": status, "modified": _ms(modified)}
        if tid:
            raw["tid"] = tid
        if refund_id:
            raw["refundId"] = refund_id
        if complete is not None:
            raw["platformCompleteTime"] = _ms(complete)
        record = normalise_aftersale(raw)
        if not apply_aftersale(conn, record, batch_id="seed"):
            assert conn.execute(
                "SELECT 1 FROM bi.aftersales WHERE shop_id='S1' AND aftersale_id=%s",
                (record["aftersale_id"],)).fetchone()

    refund("R1", "C1", "PR1", "30.00", pay_time(2, 8), 7, 9, pay_time(2, 8))
    refund("R2", "C1", "PR2", "20.00", pay_time(4, 8), 7, 9, pay_time(4, 8))
    refund("R3", "C0", "PR3", "50.00", pay_time(3, 8), 7, 9, pay_time(3, 8))
    # R4 09-09超过本次截止，不能计入
    refund("R4", "C2", "PR4", "40.00",
           datetime(2026, 9, 9, 8, tzinfo=BEIJING), 7, 9,
           datetime(2026, 9, 9, 8, tzinfo=BEIJING))
    # R5 待处理退款10；R6 工单已解决但线上退款关闭20：均不计
    refund("R5", "C3", "PR5", "10.00", None, 2, 2, pay_time(5, 8))
    refund("R6", "C2", "PR6", "20.00", None, 6, 9, pay_time(6, 8))

    for entity, source in (("orders", "erp.trade.list.query"),
                           ("aftersales_occurrence", "erp.aftersale.list.query"),
                           ("aftersales_cohort", "erp.aftersale.list.query")):
        # 种子代表“已核验的参考场景”，所质状态显式写成 passed；
        # 旧 quality_ok 列已由 008 退役，不得再写。
        conn.execute(
            "INSERT INTO bi.sync_state(source, entity, shop_id, watermark, covered, "
            "data_as_of, quality_status, quality_checked_at, quality_rule) "
            "VALUES (%s, %s, 'S1', %s, "
            "tstzmultirange(tstzrange(%s, %s, '[)')), %s, 'passed', %s, 'test-seed') "
            "ON CONFLICT (source, entity, shop_id) DO UPDATE SET covered = "
            "EXCLUDED.covered, data_as_of = EXCLUDED.data_as_of, "
            "quality_status = 'passed', quality_checked_at = EXCLUDED.quality_checked_at, "
            "quality_rule = EXCLUDED.quality_rule",
            (source, entity, COVERAGE_END, COVERAGE_START, COVERAGE_END, FROZEN_CUTOFF,
             FROZEN_CUTOFF))


class MetricsTests(unittest.TestCase):
    """5.2/5.6：人工金额断言与业务风险检查，reader角色只读执行。"""

    def setUp(self):
        if not os.getenv("BI_TEST_ADMIN_DSN"):
            self.skipTest("未配置独立测试数据库")
        self.conn = connect_test_db(self)
        seed_business_case(self.conn)
        self.conn.execute("SET LOCAL ROLE bi_reader")

    def _query(self, **overrides):
        import time as time_module

        from bi_agent.metrics import QueryRequest, query_business

        defaults = dict(start="2026-09-01", end="2026-09-08", shop_ids=["S1"],
                        metrics=["paid_amount"])
        defaults.update(overrides)
        request = QueryRequest(**defaults)
        return query_business(self.conn, request, allowed_shop_ids=frozenset({"S1"}),
                              now=FROZEN_NOW, deadline=time_module.monotonic() + 30)

    def _as_admin(self, sql: str, params: tuple = ()) -> None:
        """本类默认以 bi_reader 跑查询，改预置数据时必须临时切回属主再切回。"""
        self.conn.execute("RESET ROLE")
        self.conn.execute(sql, params)
        self.conn.execute("SET LOCAL ROLE bi_reader")

    def _quality(self, status: str, *, entity: str = "orders") -> None:
        self._as_admin(
            "UPDATE bi.sync_state SET quality_status=%s WHERE entity=%s AND shop_id='S1'",
            (status, entity))

    def test_unverified_source_answers_but_discloses_quality(self):
        """从未对账不等于数据有错：可以出数，但必须把未核验说出来。"""
        self._quality("unknown")
        result = self._query()

        self.assertEqual(result.status, "ok", result.limitations)
        self.assertTrue(any("未核验" in item for item in result.limitations),
                        result.limitations)

    def test_failed_reconciliation_refuses_to_emit_numbers(self):
        """对账失败的范围禁止出数：不能拿已知有错的数据继续给金额。"""
        self._quality("failed")
        result = self._query()

        self.assertNotEqual(result.status, "ok")
        self.assertEqual(result.data, [])
        self.assertTrue(any("质量核验未通过" in item for item in result.limitations),
                        result.limitations)

    def test_coverage_gap_returns_suggestion_and_keeps_the_requested_window(self):
        """缺覆盖时只给建议，原窗口必须原封不动返回。"""
        self._as_admin(
            "UPDATE bi.sync_state SET covered = tstzmultirange(tstzrange(%s, %s, '[)')), "
            "data_as_of=%s WHERE entity='orders' AND shop_id='S1'",
            (datetime(2026, 9, 1, tzinfo=BEIJING), datetime(2026, 9, 6, tzinfo=BEIJING),
             datetime(2026, 9, 6, tzinfo=BEIJING)))
        result = self._query(metrics=["paid_amount", "paid_orders"])

        self.assertEqual(result.status, "missing_data", result.limitations)
        self.assertEqual(result.filters["start"], "2026-09-01")
        self.assertEqual(result.filters["end"], "2026-09-08")
        self.assertEqual(result.coverage.gaps, ["2026-09-06~2026-09-08"])
        self.assertEqual(result.coverage.suggested_window, ("2026-09-01", "2026-09-06"))

    def test_period_totals_match_manual_answers(self):
        result = self._query(metrics=["paid_amount", "paid_orders", "refund_amount",
                                      "cash_difference", "cohort_refund_rate"])
        self.assertEqual(result.status, "ok", result.limitations)
        self.assertEqual(result.coverage.status, "complete")
        row = result.data[0]
        self.assertEqual(Decimal(row["paid_amount"]), Decimal("1000"))
        self.assertEqual(row["paid_orders"], 6)
        self.assertEqual(Decimal(row["refund_amount"]), Decimal("100"))
        self.assertEqual(Decimal(row["cash_difference"]), Decimal("900"))
        self.assertEqual(Decimal(row["cohort_refund_rate"]), Decimal("0.05"))
        self.assertEqual(result.data_as_of, FROZEN_CUTOFF)

    def test_aov_uses_commercial_orders(self):
        result = self._query(metrics=["paid_amount", "paid_orders", "aov"])
        row = result.data[0]
        self.assertEqual(Decimal(row["aov"]), (Decimal("1000") / Decimal("6")))
        # ERP单据数同样是6（E1..E6），不能以此替代商业单分母检验
        self.assertNotIn("erp_documents", row)

    def test_sep02_granularity_distinct(self):
        """09-02单独看：ERP单2、商业单1，证明没有混淆粒度。"""
        result = self._query(start="2026-09-02", end="2026-09-03",
                             metrics=["paid_amount", "paid_orders", "erp_documents"],
                             group_by="day")
        row = result.data[0]
        self.assertEqual(Decimal(row["paid_amount"]), Decimal("100"))
        self.assertEqual(row["paid_orders"], 1)
        self.assertEqual(row["erp_documents"], 2)

    def test_day_trend_zero_fills_only_covered_days(self):
        result = self._query(metrics=["paid_amount"], group_by="day")
        self.assertEqual(result.status, "ok", result.limitations)
        by_day = {row["day"]: Decimal(row["paid_amount"]) for row in result.data}
        expected = {"2026-09-01": "500", "2026-09-02": "100", "2026-09-03": "200",
                    "2026-09-04": "0", "2026-09-05": "200", "2026-09-06": "0",
                    "2026-09-07": "0"}
        self.assertEqual(len(result.data), 7)
        for day, value in expected.items():
            self.assertEqual(by_day[day], Decimal(value))

    def test_product_ranking(self):
        result = self._query(metrics=["product_paid_amount", "quantity"],
                             group_by="product", top_n=2)
        self.assertEqual(result.status, "ok", result.limitations)
        rows = {row["product_id"]: row for row in result.data}
        self.assertEqual(Decimal(rows["P_A"]["product_paid_amount"]), Decimal("600"))
        self.assertEqual(Decimal(rows["P_A"]["quantity"]), Decimal("7"))
        self.assertEqual(Decimal(rows["P_B"]["product_paid_amount"]), Decimal("400"))
        self.assertEqual(Decimal(rows["P_B"]["quantity"]), Decimal("4"))
        self.assertTrue(all(row["allocation_verified"] == 1 for row in rows.values()))

    def test_compare_previous_period(self):
        result = self._query(metrics=["paid_amount"], compare="previous_period")
        row = result.data[0]
        self.assertEqual(Decimal(row["paid_amount"]), Decimal("1000"))
        self.assertEqual(Decimal(row["paid_amount_previous"]), Decimal("500"))
        self.assertEqual(Decimal(row["paid_amount_change"]), Decimal("500"))
        self.assertEqual(Decimal(row["paid_amount_change_ratio"]), Decimal("1"))

    def test_cross_period_refund_and_partial_refunds_counted(self):
        """R3跨期退款计入期间退款发生；C1两次部分退款都计入。"""
        result = self._query(metrics=["refund_amount"])
        self.assertEqual(Decimal(result.data[0]["refund_amount"]), Decimal("100"))

    def test_refund_after_cutoff_excluded(self):
        """R4于09-09退款：窗口与截止都不含。"""
        result = self._query(start="2026-09-01", end="2026-09-10",
                             metrics=["refund_amount"])
        self.assertEqual(result.status, "missing_data")

    def test_pending_and_closed_refunds_not_counted(self):
        result = self._query(metrics=["refund_amount", "cash_difference"])
        self.assertEqual(Decimal(result.data[0]["refund_amount"]), Decimal("100"))

    def test_unmatched_refunds_are_answered_with_a_quantified_disclosure(self):
        """计划 5.3a：未匹配退款不再拒答，改为逐结果披露条数/分母/金额/比例。

        种子窗口内 canonical 平台成功退款共 4 条（R1/R2/R3 + 新加的 R7），
        其中 R7 没有原单：退款发生额 125 含它，同批率不含它。
        """
        from bi_agent.sync import apply_aftersale, normalise_aftersale

        self.conn.execute("RESET ROLE")
        record = normalise_aftersale({
            "aftersaleId": "R7", "userId": "S1", "refundId": "PR7",
            "rawRefundMoney": "25.00", "onlineStatus": 7, "status": 9,
            "platformCompleteTime": _ms(datetime(2026, 9, 5, 8, tzinfo=BEIJING)),
            "modified": _ms(datetime(2026, 9, 5, 8, tzinfo=BEIJING)),
        })
        self.assertTrue(apply_aftersale(self.conn, record, batch_id="seed2"))
        self.conn.execute("SET LOCAL ROLE bi_reader")
        result = self._query(metrics=["refund_amount", "cash_difference",
                                      "cohort_refund_rate"])

        self.assertEqual(result.status, "ok", result.limitations)
        self.assertEqual(Decimal(result.data[0]["refund_amount"]), Decimal("125"),
                         "matched=false 的 canonical 成功退款同样是已发生的退款")
        self.assertEqual(Decimal(result.data[0]["cash_difference"]), Decimal("875"))
        self.assertTrue(any("未匹配1条/共4条" in item and "金额25元" in item
                            and "比例25%" in item for item in result.limitations),
                        result.limitations)
        self.assertTrue(any("仅含已匹配退款" in item for item in result.limitations),
                        "同批率不能把所有未匹配退款猜配到本期")
        self.assertEqual(Decimal(result.data[0]["cohort_refund_rate"]), Decimal("0.05"),
                         "同批只算能归属到本期支付原单的退款：R1+R2=50 / 1000")

        # 支付指标不受退款归属问题影响，但要把未认证支付说清楚。
        paid = self._query(metrics=["paid_amount"])
        self.assertEqual(paid.status, "ok")
        self.assertEqual(Decimal(paid.data[0]["paid_amount"]), Decimal("1000"))

    def test_unauthorized_shop_forbidden(self):
        result = self._query(shop_ids=["S1", "S2"], metrics=["paid_amount"])
        self.assertEqual(result.status, "forbidden")

    def test_injection_style_shop_id_rejected(self):
        result = self._query(shop_ids=["S1; DROP TABLE bi.orders; --"],
                             metrics=["paid_amount"])
        self.assertEqual(result.status, "forbidden")
        self.conn.execute("RESET ROLE")
        remaining = self.conn.execute(
            "SELECT count(*) FROM bi.orders").fetchone()[0]
        self.assertGreater(remaining, 0)

    def test_true_zero_vs_missing_day(self):
        """09-04覆盖完整且无支付：真实0；超出覆盖的日期：missing_data。"""
        covered = self._query(start="2026-09-04", end="2026-09-05",
                              metrics=["paid_amount"], group_by="day")
        self.assertEqual(covered.status, "ok")
        self.assertEqual(Decimal(covered.data[0]["paid_amount"]), Decimal("0"))
        beyond = self._query(start="2026-09-09", end="2026-09-10",
                             metrics=["paid_amount"])
        self.assertEqual(beyond.status, "missing_data")
        self.assertEqual(beyond.coverage.status, "missing")

    def test_zero_denominator_not_computable(self):
        result = self._query(start="2026-09-06", end="2026-09-07",
                             metrics=["aov", "cohort_refund_rate"])
        self.assertEqual(result.status, "ok")
        self.assertIsNone(result.data[0]["aov"])
        self.assertIsNone(result.data[0]["cohort_refund_rate"])
        self.assertTrue(any("不可计算" in item for item in result.limitations))

    def test_fan_out_guard_amounts_not_inflated(self):
        """多商品行+多笔退款+同日多单：各自聚合，金额不被连接放大。"""
        result = self._query(metrics=["paid_amount", "paid_orders", "refund_amount",
                                      "cohort_refund_rate", "erp_documents"])
        row = result.data[0]
        self.assertEqual(Decimal(row["paid_amount"]), Decimal("1000"))
        self.assertEqual(row["paid_orders"], 6)
        self.assertEqual(Decimal(row["refund_amount"]), Decimal("100"))
        self.assertEqual(row["erp_documents"], 6)


@unittest.skipUnless(os.getenv("BI_TEST_ADMIN_DSN"), "未配置独立测试数据库")
class RefetchConvergenceTests(unittest.TestCase):
    """计划 5.3e：补拉集合必须收敛，失败不能留下半成品。

    2026-09-12 淘系实测记录指出这两条路径此前**零用例覆盖**，于是“集合永不收敛”
    每轮白跑 625 次上游调用都没被发现。这里把它钉住。
    """

    def setUp(self):
        self.conn = connect_test_db(self)

    def _shop(self, shop_id="S1", platform="fxg"):
        self.conn.execute(
            "INSERT INTO bi.shops(shop_id, platform, display_name) VALUES (%s,%s,'店') "
            "ON CONFLICT (shop_id) DO UPDATE SET platform = EXCLUDED.platform",
            (shop_id, platform))

    def _order(self, *, shop_id="S1", erp_id="E1", commercial="C1", active=False,
               paid="100.00", paid_at=None, source="erp.trade.list.query"):
        self.conn.execute(
            "INSERT INTO bi.orders(shop_id, erp_id, source, commercial_ids, active, "
            "raw_pay_amount, paid_at, source_updated_at, batch_id) "
            "VALUES (%s, %s, %s, ARRAY[%s], %s, %s, %s, now(), 'probe') "
            "ON CONFLICT (shop_id, erp_id) DO NOTHING",
            (shop_id, erp_id, source, commercial, active, Decimal(paid),
             paid_at or datetime(2026, 9, 2, 10, tzinfo=BEIJING)))

    def _refund(self, *, shop_id="S1", aftersale_id="A1", commercial="C1"):
        self.conn.execute(
            "INSERT INTO bi.aftersales(shop_id, aftersale_id, commercial_id, "
            "platform_success, refund_canonical, matched, raw_platform_amount, "
            "platform_completed_at, source_updated_at, batch_id) VALUES "
            "(%s, %s, %s, true, true, false, 30, %s, now(), 'probe') "
            "ON CONFLICT (shop_id, aftersale_id) DO NOTHING",
            (shop_id, aftersale_id, commercial,
             datetime(2026, 9, 3, 8, tzinfo=BEIJING)))

    def _pending(self, shop_id="S1"):
        from bi_agent.sync import unmatched_commercials

        return unmatched_commercials(self.conn, shop_id)

    def test_paid_but_closed_order_leaves_the_refetch_set(self):
        """已付款关闭单已能参与匹配：它不得再留在补拉集合里（否则每轮白跑）。"""
        self._shop()
        self._order(active=False, paid="100.00")
        self._refund()

        self.assertEqual(self._pending(), set(),
                         "关闭但已付款的原单同样是原单，不能继续要求补拉")

    def test_genuinely_missing_order_stays_in_the_set(self):
        self._shop()
        self._refund(commercial="C_MISSING")

        self.assertEqual(self._pending(), {"C_MISSING"},
                         "原单真的没到时必须继续等待补拉，不能当作已解决")

    def test_unmatched_refund_without_commercial_id_is_not_a_refetch_target(self):
        self._shop()
        self.conn.execute(
            "INSERT INTO bi.aftersales(shop_id, aftersale_id, commercial_id, "
            "platform_success, refund_canonical, matched, raw_platform_amount, "
            "platform_completed_at, source_updated_at, batch_id) VALUES "
            "('S1', 'A_NO_CID', NULL, true, true, false, 20, %s, now(), 'probe')",
            (datetime(2026, 9, 4, 8, tzinfo=BEIJING),))

        self.assertEqual(self._pending(), set(),
                         "没有原单号就没有可补拉的 tid：这类退款归披露，不归补拉")

    def test_refetch_uses_the_platform_channel_and_converges(self):
        """补拉按店铺平台走实际通道，取回原单后集合收敛。"""
        from bi_agent.sync import refetch_orders_for_commercials

        self._shop(platform="tb")
        self._refund(commercial="C9")

        class FakeClient:
            def __init__(self):
                self.methods: list[str] = []

            def call(self, method, params):
                self.methods.append(method)
                return {"success": True, "total": 1, "hasNext": False,
                        "list": [{"sid": "E9", "userId": "S1", "tid": "C9",
                                  "payAmount": "60.00",
                                  "payTime": _ms(datetime(2026, 9, 1, 9, tzinfo=BEIJING)),
                                  "updTime": _ms(datetime(2026, 9, 1, 10, tzinfo=BEIJING)),
                                  "status": "TRADE_CLOSED", "sysStatus": 0,
                                  "orders": [{"oid": "E9-1", "tid": "C9",
                                               "itemSysId": "P_A", "num": "1",
                                               "payAmount": "60.00"}]}]}

        client = FakeClient()
        accepted = refetch_orders_for_commercials(
            self.conn, client, shop_id="S1", commercial_ids={"C9"},
            order_source="erp.trade.outstock.simple.query")

        self.assertEqual(client.methods, ["erp.trade.outstock.simple.query"])
        self.assertGreaterEqual(accepted, 1)
        self.assertEqual(self._pending(), set(), "补拉回来的原单必须让集合收敛")

    def test_failed_refetch_leaves_no_half_written_state(self):
        """上游失败时不能留下支付/批次半成品：一次补拉的窗口要么整事务成立要么不成立。"""
        from bi_agent.kuaimai import KuaimaiError
        from bi_agent.sync import refetch_orders_for_commercials

        self._shop()
        self._refund(commercial="C9")

        class FailingClient:
            def call(self, method, params):
                raise KuaimaiError("upstream")

        with self.assertRaises(KuaimaiError):
            refetch_orders_for_commercials(
                self.conn, FailingClient(), shop_id="S1", commercial_ids={"C9"})

        self.assertEqual(self.conn.execute(
            "SELECT count(*) FROM bi.orders WHERE erp_id='E9'").fetchone()[0], 0)
        self.assertEqual(self.conn.execute(
            "SELECT count(*) FROM bi.sync_batches WHERE mode='incremental'"
        ).fetchone()[0], 0, "失败的上游页不能留下批次凭证")
        self.assertEqual(self._pending(), {"C9"})


@unittest.skipUnless(os.getenv("BI_TEST_ADMIN_DSN"), "未配置独立测试数据库")
class ApprovedQueryMemoryMigrationTests(unittest.TestCase):
    """计划 Task 1 Step 4/6：021 幂等、bi_app 无底表权限、只有 approved 投影可读。

    全部 DDL 在管理员外层事务里跑（结束按 Rollback 协议退出），共享测试库只保留
    由命令行显式提交的那份 021。privilege 探针沿用 020 诊断用例的
    `has_*_privilege` 写法：bi_approver 是 NOLOGIN，不切会话角色。
    """

    MIGRATION = "021_approved_query_memory.sql"
    SLOTS_JSON = ('[{"name": "shop_scope", "kind": "entity_scope"}, '
                  '{"name": "date_window", "kind": "date_window"}]')
    REQUEST_JSON = '{"requested_metric_refs": ["metric-cost-total"]}'
    VERSIONS_JSON = ('{"schema_version": "reporting/2026-09-14.1", '
                     '"semantic_catalog_version": "semantic/2026-09-14.1", '
                     '"data_catalog_version": 7, '
                     '"metric_version": "metrics/2026-09-12.1", '
                     '"policy_version": "multi-source-policy/2026-09-12.1", '
                     '"source_registry_version": "sources/2026-09-12.1", '
                     '"graph_version": "business_query-graph/2026-09-11.1"}')

    def setUp(self):
        self.conn = connect_test_db(self)

    def _migration_sql(self) -> str:
        from pathlib import Path

        path = Path(__file__).parents[1] / "sql" / self.MIGRATION
        return path.read_text(encoding="utf-8")

    def _seed_run(self) -> str:
        """预置一条可被样例引用的 query_run（样例行 FK 指向它）。"""
        chat_id, message_id = uuid4(), uuid4()
        run_id = uuid4()
        self.conn.execute(
            "INSERT INTO bi.app_chats(id, subject_id, title) VALUES (%s, 'subject-a', '查询')",
            (chat_id,))
        self.conn.execute(
            "INSERT INTO bi.app_messages(id, chat_id, role, content, status) "
            "VALUES (%s, %s, 'user', '查询销售额', 'complete')",
            (message_id, chat_id))
        self.conn.execute(
            "INSERT INTO bi.query_runs(id, chat_id, user_message_id, subject_id, "
            "tool_call_id, domain, attempt_no, normalized_request, state) "
            "VALUES (%s, %s, %s, 'subject-a', 'call_1', 'business_query', 1, '{}', '{}')",
            (run_id, chat_id, message_id))
        return str(run_id)

    def _insert_example(self, run_id: str, ref: str, *, status: str,
                        request_json: str = REQUEST_JSON,
                        revision: int = 1) -> None:
        self.conn.execute(
            """INSERT INTO bi.approved_query_examples(
                   example_ref, source_run_id, owner_subject_id, domain,
                   intent_signature, question_template, slots, normalized_request,
                   expected_tool, version_requirements, authorization_refs,
                   status, approval_revision, created_by)
               VALUES (%s, %s, 'subject-a', 'controlled_exploration',
                       'cost-by-shop-and-window',
                       '比较 {shop_scope} 在 {date_window} 的成本',
                       %s::jsonb, %s::jsonb, 'explore_business_data',
                       %s::jsonb, ARRAY['ent-1a2b3c4d'], %s, %s, 'reviewer-a')""",
            (ref, run_id, self.SLOTS_JSON, request_json, self.VERSIONS_JSON,
             status, revision))

    def test_021_is_the_next_numbered_migration_after_020(self):
        """迁移编号已冻结：021 只属于本计划，而且排在 020 之后。"""
        from pathlib import Path

        sql_dir = Path(__file__).parents[1] / "sql"
        files = sorted(path.name for path in sql_dir.glob("*.sql"))
        self.assertIn(self.MIGRATION, files)
        self.assertGreater(files.index(self.MIGRATION),
                           files.index("020_controlled_sql_exploration.sql"))
        self.assertEqual([name for name in files if name.startswith("021")],
                         [self.MIGRATION], "同一编号不能有两份迁移")

    def test_replaying_021_is_idempotent(self):
        self.conn.execute(self._migration_sql())
        self.conn.execute(self._migration_sql())
        objects = self.conn.execute(
            """SELECT table_name FROM information_schema.tables
               WHERE table_schema='bi' AND table_name IN
                     ('approved_query_examples', 'approved_query_events')
               UNION ALL
               SELECT table_name FROM information_schema.views
               WHERE table_schema='reporting'
                     AND table_name='v_approved_query_examples'
               ORDER BY 1""").fetchall()
        self.assertEqual([row[0] for row in objects],
                         ["approved_query_events", "approved_query_examples",
                          "v_approved_query_examples"])
        self.assertFalse(self.conn.execute(
            "SELECT rolcanlogin FROM pg_roles WHERE rolname='bi_approver'"
        ).fetchone()[0], "审核身份必须是 NOLOGIN：它只能经独立 DSN 的会话成员使用")

    def test_bi_app_cannot_read_or_write_memory_base_tables(self):
        self.conn.execute(self._migration_sql())
        self.conn.execute("SET LOCAL ROLE bi_app")
        denials = (
            "SELECT count(*) FROM bi.approved_query_examples",
            "SELECT count(*) FROM bi.approved_query_events",
            "INSERT INTO bi.approved_query_events(example_ref, revision, "
            "actor_subject_id, event_kind, reason) "
            "VALUES ('mem-x', 0, 's', 'drafted', 'r')",
        )
        for statement in denials:
            with self.subTest(sql=statement[:36]), \
                    self.assertRaises(psycopg.errors.InsufficientPrivilege), \
                    self.conn.transaction():
                self.conn.execute(statement)
        # 投影视图可读，但只读：INSERT 的授权从未给出。
        self.assertEqual(self.conn.execute(
            "SELECT count(*) FROM reporting.v_approved_query_examples").fetchone()[0], 0)
        with self.assertRaises(psycopg.errors.InsufficientPrivilege), \
                self.conn.transaction():
            self.conn.execute(
                "INSERT INTO reporting.v_approved_query_examples(example_ref) "
                "VALUES ('mem-nope')")

    def test_view_exposes_only_approved_rows_and_never_the_source_columns(self):
        self.conn.execute(self._migration_sql())
        run_id = self._seed_run()
        self._insert_example(run_id, "mem-draft-001", status="draft")
        self._insert_example(run_id, "mem-ok-0001", status="approved")
        rows = self.conn.execute(
            "SELECT example_ref FROM reporting.v_approved_query_examples "
            "ORDER BY example_ref").fetchall()
        self.assertEqual([row[0] for row in rows], ["mem-ok-0001"],
                         "draft 行不得进检索投影")
        columns = [row[0] for row in self.conn.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema='reporting' "
            "AND table_name='v_approved_query_examples' "
            "ORDER BY ordinal_position").fetchall()]
        self.assertEqual(columns, ["example_ref", "owner_subject_id", "domain",
                                   "intent_signature", "question_template", "slots",
                                   "normalized_request", "expected_tool",
                                   "version_requirements", "authorization_refs",
                                   "approval_revision"])
        for leaked in ("source_run_id", "status", "created_by"):
            self.assertNotIn(leaked, columns, f"投影不得暴露 {leaked}")

    def test_repository_revocation_leaves_the_projection_before_the_next_retrieval(self):
        """计划 Task 6 Step 1 的真库变体：经 repository 撤销后，检索视角的下
        一次读取立即看不到该行——投影视图只含 approved，撤销即时生效。"""
        from bi_agent.query_memory.models import ApprovalCommand
        from bi_agent.query_memory.repository import QueryMemoryRepository

        self.conn.execute(self._migration_sql())
        run_id = self._seed_run()
        self._insert_example(run_id, "mem-live-0001", status="approved")
        self.assertEqual(self.conn.execute(
            "SELECT example_ref FROM reporting.v_approved_query_examples"
        ).fetchall(), [("mem-live-0001",)])
        repository = QueryMemoryRepository(self.conn)
        repository.transition(
            "mem-live-0001",
            command=ApprovalCommand(action="revoke", reason="证据失效，立即撤销"),
            actor_subject_id="reviewer-a")
        self.assertEqual(self.conn.execute(
            "SELECT example_ref FROM reporting.v_approved_query_examples"
        ).fetchall(), [])
        self.assertEqual(self.conn.execute(
            "SELECT status, approval_revision FROM bi.approved_query_examples"
            " WHERE example_ref = 'mem-live-0001'").fetchone(),
            ("revoked", 2))
        self.assertEqual(self.conn.execute(
            "SELECT revision, actor_subject_id, event_kind, reason"
            " FROM bi.approved_query_events ORDER BY revision").fetchall(),
            [(2, "reviewer-a", "revoked", "证据失效，立即撤销")])

    def test_single_version_dimension_change_stops_the_retrieval_match(self):
        """计划 Task 6 Step 1 的真库变体：VersionSet 七个维度任改其一，检索
        SQL 的完整 jsonb 精确相等就不再命中；存储行保持 approved，不被自动
        升级或迁移。"""
        from psycopg.types.json import Jsonb

        from bi_agent.query_memory.retrieval import _RETRIEVAL_SQL
        from bi_agent.runtime.versions import VersionSet

        self.conn.execute(self._migration_sql())
        run_id = self._seed_run()
        self._insert_example(run_id, "mem-live-0001", status="approved")
        current = json.loads(self.VERSIONS_JSON)
        params = {"domain": "controlled_exploration",
                  "subject_id": "subject-a",
                  "allowed_refs": ["ent-1a2b3c4d"]}
        rows = self.conn.execute(_RETRIEVAL_SQL, params | {
            "versions": Jsonb(current)}).fetchall()
        self.assertEqual([row[0] for row in rows], ["mem-live-0001"])
        probes = {
            "schema_version": "reporting/2099-01-01.probe",
            "semantic_catalog_version": "semantic/2099-01-01.probe",
            "data_catalog_version": current["data_catalog_version"] + 1,
            "metric_version": "metrics/2099-01-01.probe",
            "policy_version": "multi-source-policy/2099-01-01.probe",
            "source_registry_version": "sources/2099-01-01.probe",
            "graph_version": "business_query-graph/2099-01-01.probe",
        }
        self.assertEqual(frozenset(probes), frozenset(VersionSet.model_fields))
        for field, value in probes.items():
            with self.subTest(field=field):
                rows = self.conn.execute(_RETRIEVAL_SQL, params | {
                    "versions": Jsonb(current | {field: value})}).fetchall()
                self.assertEqual(rows, [], f"{field} 单项失配仍命中了检索")
        # jsonb 读回就是 Python 对象：版本要求逐键原封不动，没有任何自动升级。
        self.assertEqual(self.conn.execute(
            "SELECT status, approval_revision, version_requirements"
            " FROM bi.approved_query_examples"
            " WHERE example_ref = 'mem-live-0001'").fetchone(),
            ("approved", 1, current))

    def test_base_tables_reject_every_bound_value_key(self):
        """021 的 no_bound_values CHECK 与 models.FORBIDDEN_VALUE_KEYS 同一词表。"""
        from bi_agent.query_memory.models import FORBIDDEN_VALUE_KEYS

        self.conn.execute(self._migration_sql())
        run_id = self._seed_run()
        for key in sorted(FORBIDDEN_VALUE_KEYS):
            poisoned = json.dumps({"requested_metric_refs": ["metric-cost-total"],
                                   key: "x"})
            with self.subTest(key=key), \
                    self.assertRaises(psycopg.errors.CheckViolation), \
                    self.conn.transaction():
                self._insert_example(run_id, f"mem-bound-{key[:8].replace('_','')}",
                                     status="approved", request_json=poisoned)

    def test_base_tables_reject_unknown_status_and_negative_revision(self):
        self.conn.execute(self._migration_sql())
        run_id = self._seed_run()
        for field, value in (("status", "learning"), ("approval_revision", -1)):
            with self.subTest(field=field), \
                    self.assertRaises(psycopg.errors.CheckViolation), \
                    self.conn.transaction():
                self.conn.execute(
                    f"INSERT INTO bi.approved_query_examples(example_ref, source_run_id, "
                    f"owner_subject_id, domain, intent_signature, question_template, "
                    f"slots, normalized_request, expected_tool, version_requirements, "
                    f"authorization_refs, {field}, created_by) "
                    f"VALUES ('mem-bad-x', %s, 's', 'd', 'i', 't', '{{}}', '{{}}', "
                    f"'t', '{{}}', ARRAY['ent-1a2b3c4d'], %s, 'r')",
                    (run_id, value))

    def test_bi_approver_has_least_privilege(self):
        """bi_approver 能写样例与追加事件，但没有事件改写权，也没有任何事实表权限。"""
        self.conn.execute(self._migration_sql())
        privileges = self.conn.execute(
            """SELECT
                   has_table_privilege('bi_approver', 'bi.approved_query_examples',
                                       'SELECT'),
                   has_table_privilege('bi_approver', 'bi.approved_query_examples',
                                       'INSERT'),
                   has_table_privilege('bi_approver', 'bi.approved_query_examples',
                                       'UPDATE'),
                   has_table_privilege('bi_approver', 'bi.approved_query_examples',
                                       'DELETE'),
                   has_table_privilege('bi_approver', 'bi.approved_query_events',
                                       'SELECT'),
                   has_table_privilege('bi_approver', 'bi.approved_query_events',
                                       'INSERT'),
                   has_table_privilege('bi_approver', 'bi.approved_query_events',
                                       'UPDATE'),
                   has_table_privilege('bi_approver', 'bi.approved_query_events',
                                       'DELETE'),
                   has_table_privilege('bi_approver', 'bi.query_runs', 'SELECT'),
                   has_table_privilege('bi_approver', 'bi.query_provenance', 'SELECT'),
                   has_table_privilege('bi_approver', 'bi.orders', 'SELECT'),
                   has_table_privilege('bi_approver', 'bi.shops', 'SELECT'),
                   has_sequence_privilege('bi_approver',
                                          'bi.approved_query_events_id_seq',
                                          'USAGE, SELECT'),
                   has_schema_privilege('bi_approver', 'bi', 'USAGE')
               """).fetchone()
        self.assertEqual(privileges, (True, True, True, False,
                                      True, True, False, False,
                                      True, True, False, False,
                                      True, True), privileges)


if __name__ == "__main__":
    unittest.main()
