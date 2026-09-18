"""逐日 AOV：group_by=day 必须按每个自然日自己的「支付金额 / 商业订单数」计算。

回归背景（live 复现）：同一家店同一窗口，group_by=total 返回 status=ok 且
AOV=58.109…，换成 group_by=day 时 7 行 paid_amount/paid_orders 都非零，但每行
aov=null——day 分支只把裸指标装进 entry 就调 _group_rows，漏掉了 aov 计算。

本文件只走公开入口 query_business / QueryRequest 的 day 路径（不直接调
_compute_aov），断言用合成数据独立重算，不依赖任何 live 业务值。SQL 语义替身复用
tests.test_core.FakeWarehouse（离线内存假库，连接名与真实 *_test 库无关），
本文件只负责构造逐日场景与断言。
"""

from __future__ import annotations

import time
import unittest
from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from bi_agent.metrics import QueryRequest, query_business
from tests.test_core import FakeWarehouse

TZ = ZoneInfo("Asia/Shanghai")

START = date(2026, 9, 10)
END = date(2026, 9, 17)          # 排他：[2026-09-10, 2026-09-17) 共 7 天
DATA_AS_OF = datetime(2026, 9, 17, 0, 0, tzinfo=TZ)

# 每天一行：(paid_amount, paid_orders, aov, erp_documents)。
# 09-12 是覆盖内的真实零日（有行、零支付）；09-13/15/16 无行，必须补真实零。
# erp_documents 刻意与 paid_orders 不同，用来证明 aov 的分母是商业订单数。
EXPECTED_DAYS: dict[str, tuple[str, int, str | None, int]] = {
    "2026-09-10": ("108", 3, "36", 5),   # 108/3=36；误用 ERP 单据数会得 108/5=21.6
    "2026-09-11": ("50", 2, "25", 2),
    "2026-09-12": ("0", 0, None, 0),     # 零单：不可除，必须 null 而不是 0
    "2026-09-13": ("0", 0, None, 0),     # 覆盖内缺交易日
    "2026-09-14": ("90", 3, "30", 1),
    "2026-09-15": ("0", 0, None, 0),
    "2026-09-16": ("0", 0, None, 0),
}


def _day_warehouse() -> FakeWarehouse:
    """专用逐日夹具：合成 3 个有支付日 + 1 个真实零日 + 3 个缺交易日。"""
    daily_rows = [
        # (shop, day, paid_amount, paid_orders, erp_documents, refund, cash_diff)
        ("S1", date(2026, 9, 10), Decimal("108"), 3, 5, Decimal("0"), Decimal("108")),
        ("S1", date(2026, 9, 11), Decimal("50"), 2, 2, Decimal("0"), Decimal("50")),
        ("S1", date(2026, 9, 12), Decimal("0"), 0, 0, Decimal("0"), Decimal("0")),
        ("S1", date(2026, 9, 14), Decimal("90"), 3, 1, Decimal("0"), Decimal("90")),
    ]
    return FakeWarehouse(
        daily_rows=daily_rows,
        shops=[("S1", True, "CNY")],
        shop_profiles=[("S1", "fxg", "档案店S1")],
        data_as_of=DATA_AS_OF,
    )


class DailyAovTests(unittest.TestCase):
    """新问题回归：day 路径逐日 AOV。"""

    def _query(self, *, group_by: str = "day",
               metrics: tuple[str, ...] = ("paid_amount", "paid_orders",
                                           "erp_documents", "aov")):
        request = QueryRequest(start=START, end=END, shop_ids=["S1"],
                               metrics=list(metrics), group_by=group_by)
        return query_business(_day_warehouse(), request,
                              allowed_shop_ids=frozenset({"S1"}),
                              now=DATA_AS_OF, deadline=time.monotonic() + 30)

    def _by_day(self, result) -> dict[str, dict]:
        return {row["day"]: row for row in result.data}

    def test_each_day_aov_equals_its_own_amount_over_commercial_orders(self):
        result = self._query()

        self.assertEqual(result.status, "ok", result.limitations)
        self.assertEqual(len(result.data), 7)
        by_day = self._by_day(result)
        self.assertEqual(sorted(by_day), sorted(EXPECTED_DAYS))
        for day, (amount, orders, aov, erp) in EXPECTED_DAYS.items():
            row = by_day[day]
            self.assertEqual(row["paid_amount"], amount, day)
            self.assertEqual(row["paid_orders"], orders, day)
            self.assertEqual(row["erp_documents"], erp, day)
            if aov is None:
                self.assertIsNone(row["aov"], day)
                continue
            # 独立于被测实现按本行自己的数重算，不是整段总额广播给每一天。
            self.assertEqual(
                Decimal(row["aov"]),
                Decimal(row["paid_amount"]) / Decimal(row["paid_orders"]), day)
            self.assertEqual(Decimal(row["aov"]), Decimal(aov), day)

    def test_day_aov_denominator_is_commercial_orders_not_erp_documents(self):
        result = self._query()

        row = self._by_day(result)["2026-09-10"]
        self.assertEqual(row["paid_orders"], 3)
        self.assertEqual(row["erp_documents"], 5)
        self.assertEqual(Decimal(row["aov"]), Decimal("36"))

    def test_multiple_days_produce_distinct_ratios(self):
        result = self._query()

        ratios = {Decimal(row["aov"]) for row in result.data
                  if row["aov"] is not None}
        self.assertEqual(ratios, {Decimal("36"), Decimal("25"), Decimal("30")})
        self.assertGreater(len(ratios), 1, "每天不能广播同一个 AOV")

    def test_zero_order_day_returns_null_not_zero(self):
        result = self._query()

        row = self._by_day(result)["2026-09-12"]
        self.assertEqual(row["paid_amount"], "0")
        self.assertEqual(row["paid_orders"], 0)
        self.assertIsNone(row["aov"])
        self.assertNotEqual(row["aov"], 0)
        self.assertNotEqual(row["aov"], "0")
        self.assertNotIsInstance(row["aov"], Decimal)

    def test_missing_transaction_day_is_zero_filled_without_aov(self):
        result = self._query()

        row = self._by_day(result)["2026-09-13"]
        self.assertEqual(row["paid_amount"], "0")
        self.assertEqual(row["paid_orders"], 0)
        self.assertIsNone(row["aov"])

    def test_aov_key_is_not_added_when_not_requested(self):
        result = self._query(metrics=("paid_amount", "paid_orders"))

        self.assertEqual(result.status, "ok", result.limitations)
        self.assertTrue(result.data)
        for row in result.data:
            self.assertNotIn("aov", row, row["day"])

    def test_total_and_shop_aov_stay_whole_period_and_not_day_averages(self):
        total = self._query(group_by="total")
        self.assertEqual(total.status, "ok", total.limitations)
        self.assertEqual(len(total.data), 1)
        self.assertEqual(total.data[0]["paid_amount"], "248")   # 108+50+0+90
        self.assertEqual(total.data[0]["paid_orders"], 8)       # 3+2+0+3
        self.assertEqual(Decimal(total.data[0]["aov"]),
                         Decimal("248") / Decimal("8"))
        self.assertEqual(Decimal(total.data[0]["aov"]), Decimal("31"))

        # AOV 口径是总额/总单数，不是逐日 AOV 的平均：日均 = 91/3 ≈ 30.33
        day = self._query(group_by="day")
        daily = [Decimal(row["aov"]) for row in day.data
                 if row["aov"] is not None]
        self.assertEqual(sum(daily) / len(daily), Decimal(91) / Decimal(3))
        self.assertNotEqual(Decimal(total.data[0]["aov"]),
                            sum(daily) / len(daily))

        shop = self._query(group_by="shop")
        self.assertEqual([row["shop_id"] for row in shop.data], ["S1"])
        self.assertEqual(Decimal(shop.data[0]["aov"]), Decimal("31"))


if __name__ == "__main__":
    unittest.main()
