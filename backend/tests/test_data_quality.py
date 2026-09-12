"""Task 1：数据覆盖验收与版本来源。

四类的失败面来自 docs/superpowers/plans/2026-09-11-data-and-query-closure.md Task 1：
有店铺无事实、窗口有缺口、成功同步但业务截止未推进、对账失败。
本模块只读 reporting 视图与新增的就绪凭证表，不碰快麦接口。
"""

import os
import unittest
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import psycopg

BEIJING = ZoneInfo("Asia/Shanghai")

SOURCE = "erp.trade.list.query"


@unittest.skipUnless(os.getenv("BI_TEST_ADMIN_DSN"), "未配置独立测试数据库")
class CoverageAssessmentTests(unittest.TestCase):
    def setUp(self):
        from tests.dbfixtures import connect_test_db

        self.conn = connect_test_db(self)
        self.conn.execute(
            "INSERT INTO bi.shops(shop_id, platform, display_name) VALUES "
            "('DQ_S1','fxg','覆盖测试店A'), ('DQ_S2','pdd','覆盖测试店B') "
            "ON CONFLICT (shop_id) DO UPDATE SET display_name=EXCLUDED.display_name")

    def _state(self, shop_id: str, *, covered, data_as_of=None, last_success=None,
               entity: str = "orders", quality: str = "unknown"):
        """写一行同步状态；covered 用 [start,end) 北京时间时刻对表示。"""
        pieces = ", ".join("tstzrange(%s, %s, '[)')" for _ in covered)
        multirange = f"tstzmultirange({pieces})" if pieces else "tstzmultirange()"
        params: list[object] = [SOURCE, entity, shop_id]
        for window in covered:
            params.extend(window)
        params.extend([data_as_of, last_success, quality])
        self.conn.execute(
            "INSERT INTO bi.sync_state(source, entity, shop_id, covered, data_as_of, "
            f"last_success_at, quality_status) VALUES (%s, %s, %s, {multirange}, %s, %s, %s) "
            "ON CONFLICT (source, entity, shop_id) DO UPDATE SET "
            "covered = EXCLUDED.covered, data_as_of = EXCLUDED.data_as_of, "
            "last_success_at = EXCLUDED.last_success_at, "
            "quality_status = EXCLUDED.quality_status",
            tuple(params),
        )

    def _batch(self, batch_id: str, *, start: datetime, end: datetime,
               entity: str = "orders", shop_id: str = "DQ_S1", rows: int = 5):
        self.conn.execute(
            "INSERT INTO bi.sync_batches(source, entity, shop_id, batch_id, "
            "business_window, mode, row_count) VALUES (%s, %s, %s, %s, "
            "tstzrange(%s, %s, '[)'), 'backfill', %s)",
            (SOURCE, entity, shop_id, batch_id, start, end, rows))

    def _request(self, **overrides):
        from bi_agent.metrics import QueryRequest

        defaults = dict(start=date(2026, 9, 4), end=date(2026, 9, 11),
                        shop_ids=["DQ_S1"], metrics=["paid_amount"])
        defaults.update(overrides)
        return QueryRequest(**defaults)

    def _assess(self, request=None):
        from bi_agent.data_quality import assess_query_coverage

        return assess_query_coverage(self.conn, request or self._request())

    # -- 四类失败面 ----------------------------------------------------------

    def test_shop_without_facts_is_missing_not_empty_result(self):
        """有档案没事实：必须报成整窗缺失，不能算“这段时间确实没有交易”。"""
        assessment = self._assess()

        self.assertEqual(assessment.requested_window, ("2026-09-04", "2026-09-11"))
        self.assertEqual(assessment.status, "missing")
        self.assertEqual(assessment.missing_windows, (("2026-09-04", "2026-09-11"),))
        self.assertEqual(assessment.covered_windows, ())
        self.assertIsNone(assessment.data_as_of)

    def test_window_gap_is_reported_with_original_window_frozen(self):
        """缺口要精确到区间，且原请求窗口不得被建议窗口替换。"""
        self._state(
            "DQ_S1",
            covered=[(datetime(2026, 9, 4, tzinfo=BEIJING),
                      datetime(2026, 9, 9, tzinfo=BEIJING))],
            data_as_of=datetime(2026, 9, 9, tzinfo=BEIJING),
        )
        assessment = self._assess()

        self.assertEqual(assessment.status, "partial")
        self.assertEqual(assessment.missing_windows, (("2026-09-09", "2026-09-11"),))
        self.assertEqual(assessment.covered_windows, (("2026-09-04", "2026-09-09"),))
        self.assertEqual(assessment.requested_window, ("2026-09-04", "2026-09-11"),
                         "建议不能改写原窗口")
        self.assertEqual(assessment.suggested_window, ("2026-09-04", "2026-09-09"))

    def test_data_as_of_uses_business_cutoff_not_last_success(self):
        """同步任务成功不等于业务截止推进：截止只能取 data_as_of。"""
        self._state(
            "DQ_S1",
            covered=[(datetime(2026, 9, 4, tzinfo=BEIJING),
                      datetime(2026, 9, 11, tzinfo=BEIJING))],
            data_as_of=datetime(2026, 9, 9, 12, tzinfo=BEIJING),
            last_success=datetime(2026, 9, 11, 8, tzinfo=BEIJING),
        )
        assessment = self._assess()

        self.assertEqual(assessment.status, "complete")
        self.assertEqual(assessment.data_as_of, datetime(2026, 9, 9, 12, tzinfo=BEIJING))
        self.assertNotEqual(assessment.data_as_of, datetime(2026, 9, 11, 8, tzinfo=BEIJING),
                            "last_success_at 不是业务截止")

    def test_failed_reconciliation_blocks_numbers(self):
        """对账失败的范围禁止出数：状态必须是 failed，且不能被当成 passed。"""
        self._state(
            "DQ_S1",
            covered=[(datetime(2026, 9, 4, tzinfo=BEIJING),
                      datetime(2026, 9, 11, tzinfo=BEIJING))],
            data_as_of=datetime(2026, 9, 11, tzinfo=BEIJING),
            quality="failed",
        )
        assessment = self._assess()

        self.assertEqual(assessment.quality_status, "failed")
        self.assertNotEqual(assessment.quality_status, "passed")

    def test_unverified_history_is_unknown_not_invented_as_passed(self):
        """没有对账记录就是 unknown：既不声称已核验，也不当作已发现错误。"""
        self._state(
            "DQ_S1",
            covered=[(datetime(2026, 9, 4, tzinfo=BEIJING),
                      datetime(2026, 9, 11, tzinfo=BEIJING))],
            data_as_of=datetime(2026, 9, 11, tzinfo=BEIJING),
        )
        assessment = self._assess()

        self.assertEqual(assessment.quality_status, "unknown")
        self.assertEqual(assessment.status, "complete")

    def test_multiple_shops_merge_to_the_common_cutoff_and_strongest_block(self):
        self._state(
            "DQ_S1",
            covered=[(datetime(2026, 9, 4, tzinfo=BEIJING),
                      datetime(2026, 9, 11, tzinfo=BEIJING))],
            data_as_of=datetime(2026, 9, 11, tzinfo=BEIJING),
        )
        self._state(
            "DQ_S2",
            covered=[(datetime(2026, 9, 4, tzinfo=BEIJING),
                      datetime(2026, 9, 8, tzinfo=BEIJING))],
            data_as_of=datetime(2026, 9, 8, tzinfo=BEIJING),
        )
        assessment = self._assess(self._request(shop_ids=["DQ_S1", "DQ_S2"]))

        self.assertEqual(assessment.status, "partial")
        self.assertEqual(assessment.data_as_of, datetime(2026, 9, 8, tzinfo=BEIJING),
                         "多店必须取共同截止，不能用较新的那家宣布整体截止")
        self.assertEqual(assessment.missing_windows, (("2026-09-08", "2026-09-11"),))

    def test_aftersale_cohort_requirement_is_assessed_separately(self):
        """退款率依赖售后 cohort：订单覆盖完整时 cohort 缺失仍要报成带归因的缺口。"""
        self._state(
            "DQ_S1",
            covered=[(datetime(2026, 9, 4, tzinfo=BEIJING),
                      datetime(2026, 9, 11, tzinfo=BEIJING))],
            data_as_of=datetime(2026, 9, 11, tzinfo=BEIJING),
        )
        assessment = self._assess(
            self._request(metrics=["paid_amount", "cohort_refund_rate"]))

        self.assertEqual(assessment.status, "partial")
        self.assertEqual({(gap.entity, gap.shop_id) for gap in assessment.gaps},
                         {("aftersales_cohort", "DQ_S1")},
                         "缺口必须能归因到实体与店铺")

    # -- 来源批次凭证 --------------------------------------------------------

    def test_shop_without_onboarded_source_is_reported_as_unconfigured(self):
        """未开通来源的店与“有覆盖但窗口缺一天”是两回事。

        前者平台没登记来源（未授权），缩小日期范围永远拿不到数据；
        把两者都说成“请缩小范围”会误导经营者。
        """
        # 平台未登记 = 没有取数来源；能力标签不参与这一步（Task 5.1 能力门禁另行归因）。
        self.conn.execute(
            "UPDATE bi.shops SET platform = 'alibabac2m' WHERE shop_id='DQ_S2'")
        self._state(
            "DQ_S1",
            covered=[(datetime(2026, 9, 4, tzinfo=BEIJING),
                      datetime(2026, 9, 11, tzinfo=BEIJING))],
            data_as_of=datetime(2026, 9, 11, tzinfo=BEIJING),
        )
        assessment = self._assess(self._request(shop_ids=["DQ_S1", "DQ_S2"]))

        self.assertEqual(assessment.source_unconfigured, ("DQ_S2",),
                         "未登记来源的店必须单独归因")
        self.assertEqual(assessment.status, "partial")

    def test_configured_shop_is_not_reported_as_unconfigured(self):
        # 已登记来源就不报“未开通”：淘系与拼多多都有出库通道，能力缺失不混到这一步。
        for shop in ("DQ_S1", "DQ_S2"):
            self._state(
                shop,
                covered=[(datetime(2026, 9, 4, tzinfo=BEIJING),
                          datetime(2026, 9, 11, tzinfo=BEIJING))],
                data_as_of=datetime(2026, 9, 11, tzinfo=BEIJING),
            )
        assessment = self._assess(self._request(shop_ids=["DQ_S1", "DQ_S2"]))

        self.assertEqual(assessment.source_unconfigured, ())
        self.assertEqual(assessment.status, "complete")

    def test_assessment_names_the_batches_behind_the_numbers(self):
        """来源批次要能回答“这些数字是哪几批同步出来的”。"""
        self._state(
            "DQ_S1",
            covered=[(datetime(2026, 9, 4, tzinfo=BEIJING),
                      datetime(2026, 9, 11, tzinfo=BEIJING))],
            data_as_of=datetime(2026, 9, 11, tzinfo=BEIJING),
        )
        self._batch("batch-in-window", start=datetime(2026, 9, 5, tzinfo=BEIJING),
                    end=datetime(2026, 9, 6, tzinfo=BEIJING))
        self._batch("batch-out-of-window", start=datetime(2026, 8, 1, tzinfo=BEIJING),
                    end=datetime(2026, 8, 2, tzinfo=BEIJING))

        self.assertEqual(self._assess().source_batches, ("batch-in-window",),
                         "只算与请求窗口真正重叠的批次")

    def test_batch_row_count_keeps_missing_apart_from_true_zero(self):
        """未统计不能写成 0：0 是“确实没有数据”的断言，负数一律拒收。"""
        self.conn.execute(
            "INSERT INTO bi.sync_batches(source, entity, shop_id, batch_id, "
            "business_window, mode, row_count) VALUES "
            "(%s, 'orders', 'DQ_S1', 'batch-unknown', tstzrange(%s, %s, '[)'), "
            "'incremental', NULL)",
            (SOURCE, datetime(2026, 9, 4, tzinfo=BEIJING),
             datetime(2026, 9, 5, tzinfo=BEIJING)))

        counted = self.conn.execute(
            "SELECT row_count FROM bi.sync_batches WHERE batch_id='batch-unknown'").fetchone()
        self.assertIsNone(counted[0], "NULL 被当成了 0")

        # 断言放最后：语句报错会中止本用例事务，收尾只剩回滚。
        with self.assertRaises(psycopg.errors.CheckViolation):
            self.conn.execute(
                "INSERT INTO bi.sync_batches(source, entity, shop_id, batch_id, "
                "business_window, mode, row_count) VALUES "
                "(%s, 'orders', 'DQ_S1', 'batch-negative', tstzrange(%s, %s, '[)'), "
                "'incremental', -1)",
                (SOURCE, datetime(2026, 9, 4, tzinfo=BEIJING),
                 datetime(2026, 9, 5, tzinfo=BEIJING)))


@unittest.skipUnless(os.getenv("BI_TEST_ADMIN_DSN"), "未配置独立测试数据库")
class QualityPromotionTests(unittest.TestCase):
    """质量升级只能靠已留凭证的对账，不能拿“同步成功了”顶替。"""

    def setUp(self):
        from tests.dbfixtures import connect_test_db

        self.conn = connect_test_db(self)
        self.conn.execute(
            "INSERT INTO bi.shops(shop_id, platform, display_name) VALUES "
            "('DQ_Q1','fxg','对账测试店') ON CONFLICT (shop_id) DO NOTHING")

    def _state(self, quality="unknown", *, rule=None):
        self.conn.execute(
            "INSERT INTO bi.sync_state(source, entity, shop_id, covered, data_as_of, "
            "quality_status, quality_rule) VALUES (%s, 'orders', 'DQ_Q1', "
            "tstzmultirange(tstzrange(%s, %s, '[)')), %s, %s, %s) "
            "ON CONFLICT (source, entity, shop_id) DO UPDATE SET "
            "quality_status = EXCLUDED.quality_status, quality_rule = EXCLUDED.quality_rule",
            (SOURCE, datetime(2026, 9, 4, tzinfo=BEIJING),
             datetime(2026, 9, 11, tzinfo=BEIJING),
             datetime(2026, 9, 11, tzinfo=BEIJING), quality, rule))

    def _evidence(self):
        self.conn.execute(
            "INSERT INTO bi.sync_batches(source, entity, shop_id, batch_id, "
            "business_window, mode, row_count) VALUES "
            "(%s, 'orders', 'DQ_Q1', 'batch-reconciled', tstzrange(%s, %s, '[)'), "
            "'reconcile', 5)",
            (SOURCE, datetime(2026, 9, 5, tzinfo=BEIJING),
             datetime(2026, 9, 6, tzinfo=BEIJING)))

    def _reconcile(self):
        from bi_agent.data_quality import reconcile_source_quality

        return reconcile_source_quality(
            self.conn, shop_id="DQ_Q1", entity="orders",
            start=datetime(2026, 9, 4, tzinfo=BEIJING),
            end=datetime(2026, 9, 11, tzinfo=BEIJING))

    def _quality_row(self):
        return self.conn.execute(
            "SELECT quality_status, quality_rule, quality_checked_at, quality_reason "
            "FROM bi.sync_state WHERE source=%s AND entity='orders' AND shop_id='DQ_Q1'",
            (SOURCE,)).fetchone()

    def _assess(self):
        from bi_agent.data_quality import assess_query_coverage
        from bi_agent.metrics import QueryRequest

        return assess_query_coverage(self.conn, QueryRequest(
            start=date(2026, 9, 4), end=date(2026, 9, 11),
            shop_ids=["DQ_Q1"], metrics=["paid_amount"]))

    def test_without_batch_evidence_reconcile_cannot_claim_passed(self):
        """没有任何对账批次落在这段窗口里，就没资格说核验过。"""
        self._state()

        self.assertEqual(self._reconcile(), "unknown")
        self.assertEqual(self._quality_row()[0], "unknown")

    def test_clean_reconciliation_promotes_to_passed_with_rule_version(self):
        from bi_agent.data_quality import QUALITY_RULE

        self._state()
        self._evidence()

        self.assertEqual(self._reconcile(), "passed")
        status, rule, checked_at, reason = self._quality_row()
        self.assertEqual((status, rule), ("passed", QUALITY_RULE))
        self.assertIsNotNone(checked_at, "对账时间要留下取证时刻")
        self.assertIsNone(reason)

    def test_unmatched_success_refund_demotes_to_failed_with_reason(self):
        self._state("passed", rule="kuaimai-reconcile/1")
        self._evidence()
        self.conn.execute(
            "INSERT INTO bi.aftersales(shop_id, aftersale_id, commercial_id, "
            "platform_success, refund_canonical, matched, platform_completed_at, "
            "source_updated_at, batch_id) VALUES "
            "('DQ_Q1', 'A_ORPHAN', NULL, true, true, false, %s, now(), 'probe')",
            (datetime(2026, 9, 5, tzinfo=BEIJING),))

        self.assertEqual(self._reconcile(), "failed")
        status, _, _, reason = self._quality_row()
        self.assertEqual((status, reason), ("failed", "unmatched_success_refunds"),
                         "已知有错要显式降为 failed 并留下原因")

    def test_stale_rule_version_is_reported_as_unknown_not_passed(self):
        """口径升级后旧的 passed 不能自动沿用。"""
        self._state("passed", rule="kuaimai-reconcile/0")

        self.assertEqual(self._assess().quality_status, "unknown")

    def test_failed_quality_is_reported_even_with_complete_coverage(self):
        """覆盖完整也盖不住质量结论：两者分开返回，由门禁分别归因。

        “拒绝出数”本身在 tests.test_db.MetricsTests 里跑过一道：
        那里覆盖本就完整，状态改成 failed 后指标必须不出数。
        """
        self._state("failed")
        assessment = self._assess()

        self.assertEqual(assessment.status, "complete")
        self.assertEqual(assessment.quality_status, "failed")


if __name__ == "__main__":
    unittest.main()
