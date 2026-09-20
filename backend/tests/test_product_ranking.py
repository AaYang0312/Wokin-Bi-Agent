"""真实测试库上的商品排行回归：日行先在 SQL 端聚成最终分组，再套行数上限。

FakeWarehouse 用例只回放聚合语义；这里用真实 reporting.v_product_daily 证明
>500 原始日行但 <500 最终分组时仍给出精确 Top N，而不是误触 MAX_ROWS 报不可用。
"""

import os
import time as time_module
import unittest
from datetime import date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from .dbfixtures import connect_test_db

BEIJING = ZoneInfo("Asia/Shanghai")


def _ms(moment: datetime) -> int:
    """北京时间转快麦毫秒时间戳。"""
    return int(moment.timestamp() * 1000)


def _at(day: int, hour: int = 12) -> datetime:
    return datetime(2026, 9, day, hour, tzinfo=BEIJING)


@unittest.skipUnless(os.getenv("BI_TEST_ADMIN_DSN"), "未配置独立测试数据库")
class ProductRankingDatabaseTests(unittest.TestCase):
    DAYS = 200
    START = date(2026, 9, 1)
    # 每个商品每天一条分摊支付额，P3 > P2 > P1；三者合计 600 行原始日行 > MAX_ROWS。
    PER_DAY = {"P1": "100", "P2": "300", "P3": "600"}

    def setUp(self):
        self.conn = connect_test_db(self)
        self.conn.execute(
            "INSERT INTO bi.shops(shop_id, platform, display_name) VALUES (%s, %s, %s) "
            "ON CONFLICT (shop_id) DO NOTHING",
            ("S1", "fxg", "店铺A"))
        # 档案名按 product_id 关联：SQL 聚合必须把它带回每个最终分组。
        for product in self.PER_DAY:
            self.conn.execute(
                "INSERT INTO bi.products(product_id, title, normalization_status) "
                "VALUES (%s, %s, 'normal') ON CONFLICT (product_id) DO UPDATE "
                "SET title=EXCLUDED.title",
                (product, f"档案{product}"))

    def _seed_days(self) -> None:
        from bi_agent.sync import apply_trade, normalise_trade

        for offset in range(self.DAYS):
            paid_at = datetime(2026, 9, 1, 12, tzinfo=BEIJING) + timedelta(days=offset)
            orders = [
                {"oid": f"L_{product}_{offset}", "tid": f"C_{product}", "itemSysId": product,
                 "type": 0, "num": "1", "payAmount": amount, "sysTitle": product,
                 "sysSkuPropertiesName": "成套"}
                for product, amount in self.PER_DAY.items()
            ]
            trade = normalise_trade({
                "sid": f"E_{offset}", "userId": "S1", "tid": f"CT_{offset}",
                "payAmount": "1000", "updTime": _ms(paid_at), "payTime": _ms(paid_at),
                "orders": orders,
            })
            with self.conn.transaction():
                self.assertTrue(apply_trade(self.conn, trade, batch_id="rank"))

    def test_more_raw_rows_than_the_cap_still_yields_the_exact_top_n(self):
        from bi_agent import metrics
        from bi_agent.metrics import MAX_ROWS, QueryRequest

        self._seed_days()
        end = self.START + timedelta(days=self.DAYS + 5)
        raw_rows = self.conn.execute(
            "SELECT count(*) FROM reporting.v_product_daily "
            "WHERE shop_id='S1' AND day >= %s AND day < %s",
            (self.START, end)).fetchone()[0]
        self.assertGreater(raw_rows, MAX_ROWS, "前提：原始日行必须超过行数上限")
        self.assertEqual(raw_rows, self.DAYS * len(self.PER_DAY))

        request = QueryRequest(start=self.START, end=end, shop_ids=["S1"],
                               metrics=["product_paid_amount"], group_by="product",
                               top_n=2)
        rows = metrics._product_rows(
            self.conn, request,
            start_ts=datetime(2026, 9, 1, tzinfo=BEIJING),
            end_ts=datetime.combine(end, datetime.min.time(), tzinfo=BEIJING),
            deadline=time_module.monotonic() + 30)

        ranked = [row for row in rows if "product_id" in row]
        self.assertEqual([row["product_id"] for row in ranked], ["P3", "P2"])
        self.assertEqual([Decimal(str(row["product_paid_amount"])) for row in ranked],
                         [Decimal(self.PER_DAY["P3"]) * self.DAYS,
                          Decimal(self.PER_DAY["P2"]) * self.DAYS])
        self.assertEqual([row["sku_label"] for row in ranked], ["成套", "成套"])
        self.assertEqual([row["product_name"] for row in ranked], ["档案P3", "档案P2"])
        self.assertEqual([row for row in rows if "notice" in row],
                         [{"notice": "仅返回Top 2，共3个商品"}])

    def test_more_final_groups_than_the_cap_fail_closed(self):
        """反恒真：最终分组真的超过上限时，必须拒输出数而不是给截断排名。"""
        from bi_agent import metrics
        from bi_agent.metrics import MAX_ROWS, QueryRequest, _RowsTruncated
        from bi_agent.sync import apply_trade, normalise_trade

        paid_at = datetime(2026, 9, 2, 12, tzinfo=BEIJING)
        orders = [
            {"oid": f"L_BIG_{index}", "tid": f"C_BIG_{index}",
             "itemSysId": f"P{index:04d}", "type": 0, "num": "1",
             "payAmount": "10", "sysTitle": "商品", "sysSkuPropertiesName": "成套"}
            for index in range(MAX_ROWS + 1)
        ]
        trade = normalise_trade({
            "sid": "E_BIG", "userId": "S1", "tid": "CT_BIG",
            "payAmount": "100", "updTime": _ms(paid_at), "payTime": _ms(paid_at),
            "orders": orders})
        with self.conn.transaction():
            self.assertTrue(apply_trade(self.conn, trade, batch_id="big"))

        request = QueryRequest(start=self.START, end=self.START + timedelta(days=5),
                               shop_ids=["S1"], metrics=["product_paid_amount"],
                               group_by="product")
        with self.assertRaises(_RowsTruncated):
            metrics._product_rows(
                self.conn, request,
                start_ts=datetime(2026, 9, 1, tzinfo=BEIJING),
                end_ts=datetime(2026, 9, 6, tzinfo=BEIJING),
                deadline=time_module.monotonic() + 30)


@unittest.skipUnless(os.getenv("BI_TEST_ADMIN_DSN"), "未配置独立测试数据库")
class ProductRankingPartitionDatabaseTests(unittest.TestCase):
    """真实库上的分区商品排行：一次查询按口径分区各自出 Top-N。

    这里要证明的不是“替身里能分组”，而是真实 `reporting` 视图 + 真实同步状态上：
    认证分区正常出榜、未认证分区只作可观测样本、实测不成立的出库口径一行不出。
    """

    START = date(2026, 9, 1)
    END = date(2026, 9, 8)
    DATA_AS_OF = datetime(2026, 9, 8, tzinfo=BEIJING)
    PRODUCT_CAPS = "{product_paid_amount,quantity}"

    def setUp(self):
        self.conn = connect_test_db(self)
        for shop_id, platform in (("S1", "fxg"), ("JD1", "jd"), ("TB1", "tb")):
            self.conn.execute(
                "INSERT INTO bi.shops(shop_id, platform, display_name, capabilities) "
                "VALUES (%s, %s, %s, %s::text[]) "
                "ON CONFLICT (shop_id) DO UPDATE SET platform = EXCLUDED.platform, "
                "capabilities = EXCLUDED.capabilities",
                (shop_id, platform, f"分区店{shop_id}", self.PRODUCT_CAPS))

    def _seed_trade(self, shop_id, amounts, *, source, day=2):
        """一家店在一张单里带上它的全部商品行；金额就是行级支付额。"""
        from bi_agent.sync import apply_trade, normalise_trade

        total = sum(Decimal(amount) for amount in amounts.values())
        trade = normalise_trade({
            "sid": f"E-{shop_id}", "userId": shop_id, "tid": f"C-{shop_id}",
            "payAmount": format(total, "f"), "updTime": _ms(_at(day, 11)),
            "payTime": _ms(_at(day)),
            "orders": [{"oid": f"{shop_id}-{product}", "tid": f"C-{shop_id}",
                        "num": "1", "itemSysId": product, "payAmount": amount,
                        "sysTitle": product, "sysSkuPropertiesName": "成套"}
                       for product, amount in amounts.items()]},
            source=source)
        self.assertEqual(trade["normalization_status"], "normal")
        with self.conn.transaction():
            self.assertTrue(apply_trade(self.conn, trade, batch_id=f"rank-{shop_id}"))

    def _seed_coverage(self, shop_id, *, source):
        self.conn.execute(
            "INSERT INTO bi.sync_state(source, entity, shop_id, watermark, covered, "
            "data_as_of, quality_status, quality_checked_at, quality_rule) VALUES "
            "(%s, 'orders', %s, %s, "
            "tstzmultirange(tstzrange('2026-09-01','2026-09-08','[)')), %s, 'passed', "
            "now(), 'test-seed') ON CONFLICT (source, entity, shop_id) DO UPDATE SET "
            "covered = EXCLUDED.covered, data_as_of = EXCLUDED.data_as_of, "
            "quality_status = EXCLUDED.quality_status, "
            "quality_rule = EXCLUDED.quality_rule",
            (source, shop_id, self.DATA_AS_OF, self.DATA_AS_OF))

    def _query(self, shop_ids, *, top_n=10, metrics=("product_paid_amount",)):
        import time as time_module

        from bi_agent.metrics import QueryRequest, query_business

        return query_business(
            self.conn,
            QueryRequest(start=self.START, end=self.END, shop_ids=list(shop_ids),
                         metrics=list(metrics), group_by="product",
                         basis_policy="partitioned", top_n=top_n),
            allowed_shop_ids=frozenset(shop_ids), now=self.DATA_AS_OF,
            deadline=time_module.monotonic() + 30)

    def test_partitioned_ranking_on_real_tables_publishes_each_cohort(self):
        from bi_agent.sources import OUTSTOCK_SOURCE, TRADE_LIST_SOURCE

        self._seed_trade("S1", {"P_A": "300", "P_B": "100"},
                         source=TRADE_LIST_SOURCE)
        self._seed_trade("JD1", {"P_C": "500"}, source=TRADE_LIST_SOURCE)
        self._seed_trade("TB1", {"P_D": "900"}, source=OUTSTOCK_SOURCE)
        self._seed_coverage("S1", source=TRADE_LIST_SOURCE)
        self._seed_coverage("JD1", source=TRADE_LIST_SOURCE)
        self._seed_coverage("TB1", source=OUTSTOCK_SOURCE)

        result = self._query(["S1", "JD1", "TB1"], top_n=2)

        self.assertEqual(result.status, "ok", result.limitations)
        self.assertEqual([(group["group"], group["status"], group["shop_ids"])
                          for group in result.rank_groups],
                         [("g1", "certified", ["S1"]),
                          ("g2", "observable_sample", ["JD1"])])
        self.assertEqual(result.rank_exclusions,
                         [{"shop_id": "TB1",
                           "reason": "coverage_time_basis_unverified"}])
        self.assertEqual(result.coverage.status, "partial")
        ranked = [row for row in result.data if "product_id" in row]
        self.assertEqual([(row["shop_id"], row["rank_group"], row["rank"])
                          for row in ranked],
                         [("S1", "g1", 1), ("S1", "g1", 2), ("JD1", "g2", 1)])
        self.assertEqual([Decimal(str(row["product_paid_amount"]))
                          for row in ranked],
                         [Decimal("300"), Decimal("100"), Decimal("500")])
        # 口径凭证只覆盖已发布的分区：被排除的店不能借 basis 混进来。
        self.assertEqual(sorted({item["shop_id"] for item in result.basis}),
                         ["JD1", "S1"])

    def test_partitioned_ranking_keeps_the_final_group_cap_per_cohort(self):
        from bi_agent.metrics import MAX_ROWS
        from bi_agent.sources import TRADE_LIST_SOURCE

        self._seed_trade("S1", {f"P{index:04d}": "10"
                                 for index in range(MAX_ROWS + 1)},
                         source=TRADE_LIST_SOURCE)
        self._seed_trade("JD1", {"P_C": "500", "P_D": "200"},
                         source=TRADE_LIST_SOURCE)
        self._seed_coverage("S1", source=TRADE_LIST_SOURCE)
        self._seed_coverage("JD1", source=TRADE_LIST_SOURCE)

        result = self._query(["S1", "JD1"], top_n=2)

        self.assertEqual(result.status, "ok", result.limitations)
        self.assertEqual(result.rank_exclusions,
                         [{"shop_id": "S1", "reason": "result_too_large"}])
        self.assertEqual([group["group"] for group in result.rank_groups], ["g1"])
        ranked = [row for row in result.data if "product_id" in row]
        self.assertEqual([(row["product_id"], row["rank"]) for row in ranked],
                         [("P_C", 1), ("P_D", 2)])
        self.assertEqual([Decimal(str(row["product_paid_amount"]))
                          for row in ranked],
                         [Decimal("500"), Decimal("200")])


if __name__ == "__main__":
    unittest.main()
