"""Task 1：数据覆盖验收与版本来源。

四类的失败面来自 docs/superpowers/plans/2026-09-11-data-and-query-closure.md Task 1：
有店铺无事实、窗口有缺口、成功同步但业务截止未推进、对账失败。
本模块只读 reporting 视图与新增的就绪凭证表，不碰快麦接口。
"""

import os
import unittest
from datetime import date, datetime, timedelta
from decimal import Decimal
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
            "('DQ_S1','fxg','覆盖测试店A'), ('DQ_S2','jd','覆盖测试店B') "
            "ON CONFLICT (shop_id) DO UPDATE SET display_name=EXCLUDED.display_name")
        # 两家店都走交易通道，所以本类的 `_state` 默认能直接当它们的依赖来源。
        # 出库通道（淘系/拼多多）的依赖解析另由 MultiSourceCoverageIntersectionTests 覆盖。

    def _state(self, shop_id: str, *, covered, data_as_of=None, last_success=None,
               entity: str = "orders", quality: str = "unknown", source: str = SOURCE):
        """写一行同步状态；covered 用 [start,end) 北京时间时刻对表示。"""
        pieces = ", ".join("tstzrange(%s, %s, '[)')" for _ in covered)
        multirange = f"tstzmultirange({pieces})" if pieces else "tstzmultirange()"
        params: list[object] = [source, entity, shop_id]
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

        # 公共范围按“每一个必需依赖”取交集：cohort 整段都没有，本次请求就没有
        # 任何一家能同时回答两个指标的窗口。把订单已覆盖段当 partial 就是并集旧错。
        self.assertEqual(assessment.status, "missing")
        self.assertIsNone(assessment.suggested_window,
                          "拿不出的指标不能靠“建议窗口”渗回一半结果")
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
        # 未登记来源使本次请求没有任何公共可覆盖窗口，所以整体是 missing；
        # 与“部分窗口有洞”分开的依据是 source_unconfigured，不是 status 文字。
        self.assertEqual(assessment.status, "missing")

    def test_configured_shop_is_not_reported_as_unconfigured(self):
        # 已登记来源就不报“未开通”：两家都在交易通道上取过数，能力缺失不混到这一步。
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

    def test_unmatched_success_refund_is_recorded_but_does_not_fail(self):
        """计划 5.3d：未匹配是归属限制，不再独自把来源打成 failed。

        留着原因字段，是为了让“为什么这些退款还没归到原单”可查；换成另一道门禁
        继续拒答，等于换个理由不说真话（设计 §5）。
        """
        self._state("passed", rule="kuaimai-reconcile/1")
        self._evidence()
        self.conn.execute(
            "INSERT INTO bi.aftersales(shop_id, aftersale_id, commercial_id, "
            "platform_success, refund_canonical, matched, platform_completed_at, "
            "source_updated_at, batch_id) VALUES "
            "('DQ_Q1', 'A_ORPHAN', NULL, true, true, false, %s, now(), 'probe')",
            (datetime(2026, 9, 5, tzinfo=BEIJING),))

        self.assertEqual(self._reconcile(), "passed")
        status, _, _, reason = self._quality_row()
        self.assertEqual((status, reason), ("passed", "unmatched_success_refunds"),
                         "归属未确认要留下原因，但不能靠它拒答")

    def test_unmatched_count_and_amount_are_quantified_per_window(self):
        """设计 §5：披露未匹配条数/分母/金额/比例，0/0 时比例是未知而不是 0%。"""
        from bi_agent.data_quality import refund_attribution_gap

        self.conn.execute(
            "INSERT INTO bi.aftersales(shop_id, aftersale_id, commercial_id, "
            "platform_success, refund_canonical, matched, raw_platform_amount, "
            "platform_completed_at, source_updated_at, batch_id) VALUES "
            "('DQ_Q1', 'A_M1', 'C1', true, true, true, 30, %s, now(), 'probe'),"
            "('DQ_Q1', 'A_U1', NULL, true, true, false, 20, %s, now(), 'probe'),"
            "('DQ_Q1', 'A_FAIL', NULL, false, true, false, 999, %s, now(), 'probe')",
            (datetime(2026, 9, 5, tzinfo=BEIJING),) * 3)
        moment = (datetime(2026, 9, 4, tzinfo=BEIJING), datetime(2026, 9, 11, tzinfo=BEIJING))

        gap = refund_attribution_gap(self.conn, shop_ids=["DQ_Q1"],
                                     start_ts=moment[0], end_ts=moment[1])

        self.assertEqual((gap.unmatched, gap.total), (1, 2),
                         "分母只算 canonical 平台成功退款，失败工单不进分母")
        self.assertEqual(gap.unmatched_amount, Decimal("20"))
        self.assertEqual(gap.ratio_text, "50%")

        empty = refund_attribution_gap(self.conn, shop_ids=["DQ_Q1"],
                                       start_ts=datetime(2026, 1, 1, tzinfo=BEIJING),
                                       end_ts=datetime(2026, 1, 2, tzinfo=BEIJING))
        self.assertEqual((empty.unmatched, empty.total, empty.ratio_text),
                         (0, 0, "未知"))

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


MULTI_SOURCE = "erp.trade.outstock.simple.query"


@unittest.skipUnless(os.getenv("BI_TEST_ADMIN_DSN"), "未配置独立测试数据库")
class MultiSourceCoverageIntersectionTests(unittest.TestCase):
    """计划 Task 5.2a：公共覆盖是「店铺×来源×实体」的交集，不是各店已覆盖段的并集。

    并集会把“甲店有这两天、乙店有那两天”拼成一个谁都不完整的“建议窗口”，
    拿着它再查一次仍然缺数，等于把缺口藏进了建议里。
    """

    def setUp(self):
        from tests.dbfixtures import connect_test_db

        self.conn = connect_test_db(self)
        self.conn.execute(
            "INSERT INTO bi.shops(shop_id, platform, display_name) VALUES "
            "('MS_S1','fxg','交集测试抖音店'), ('MS_TB1','tb','交集测试淘宝店') "
            "ON CONFLICT (shop_id) DO UPDATE SET display_name = EXCLUDED.display_name")

    def _state(self, shop_id, source, *, covered, data_as_of, entity="orders"):
        pieces = ", ".join("tstzrange(%s, %s, '[)')" for _ in covered)
        self.conn.execute(
            "INSERT INTO bi.sync_state(source, entity, shop_id, watermark, covered, "
            f"data_as_of) VALUES (%s, %s, %s, '2026-09-08 00:00+08', {multirange_of(pieces)}, "
            "%s) ON CONFLICT (source, entity, shop_id) DO UPDATE SET "
            "covered = EXCLUDED.covered, data_as_of = EXCLUDED.data_as_of",
            (source, entity, shop_id, *flat(covered), data_as_of))

    def _request(self, **overrides):
        from bi_agent.metrics import QueryRequest

        defaults = dict(start="2026-09-01", end="2026-09-08",
                        shop_ids=["MS_S1", "MS_TB1"], metrics=["paid_amount"])
        defaults.update(overrides)
        return QueryRequest(**defaults)

    def _assess(self, request):
        from bi_agent.data_quality import assess_query_coverage

        return assess_query_coverage(self.conn, request)

    def test_holes_survive_as_holes_instead_of_becoming_a_continuous_suggestion(self):
        """计划固定反例：S1 全段，TB1 只有 [09-03,09-05) 与 [09-06,09-08)。"""
        self._state("MS_S1", SOURCE, covered=[(day(2026, 9, 1), day(2026, 9, 8))],
                    data_as_of=day(2026, 9, 8, 0, 0))
        self._state("MS_TB1", MULTI_SOURCE,
                    covered=[(day(2026, 9, 3), day(2026, 9, 5)),
                             (day(2026, 9, 6), day(2026, 9, 8))],
                    data_as_of=day(2026, 9, 8, 0, 0))

        assessment = self._assess(self._request())

        self.assertEqual(assessment.covered_windows,
                         (("2026-09-03", "2026-09-05"), ("2026-09-06", "2026-09-08")))
        self.assertEqual(assessment.missing_windows,
                         (("2026-09-01", "2026-09-03"), ("2026-09-05", "2026-09-06")))
        self.assertEqual(assessment.status, "partial")
        # 建议窗口必须同时在两家店都已覆盖：并集给出的 (09-01,09-08) 是假建议。
        self.assertEqual(assessment.suggested_window, ("2026-09-06", "2026-09-08"))
        self.assertEqual(assessment.requested_window, ("2026-09-01", "2026-09-08"))

    def test_stale_trade_channel_row_cannot_borrow_coverage(self):
        """淘系店即使在交易通道名下还留着旧状态行，也不能拿来拼覆盖。

        这正是旧实现的错：`ENTITY_SOURCES["orders"]` 是单源常量，淘系查不到行就整店
        报缺数，反向存在旧行时又会把不相干来源的区间当覆盖。
        """
        self._state("MS_S1", SOURCE, covered=[(day(2026, 9, 1), day(2026, 9, 8))],
                    data_as_of=day(2026, 9, 8, 0, 0))
        self._state("MS_TB1", MULTI_SOURCE,
                    covered=[(day(2026, 9, 3), day(2026, 9, 5)),
                             (day(2026, 9, 6), day(2026, 9, 8))],
                    data_as_of=day(2026, 9, 8, 0, 0))
        # 干扰项：同一淘系店在交易通道下的“完整”旧区间。
        self._state("MS_TB1", SOURCE, covered=[(day(2026, 9, 1), day(2026, 9, 8))],
                    data_as_of=day(2026, 9, 8, 0, 0))

        assessment = self._assess(self._request())

        self.assertEqual(assessment.missing_windows,
                         (("2026-09-01", "2026-09-03"), ("2026-09-05", "2026-09-06")))

    def test_outstock_only_shop_is_answered_from_its_own_channel(self):
        """只有出库通道状态的淘系店，单店查询不再被“查不到交易源行”打死。"""
        self._state("MS_TB1", MULTI_SOURCE, covered=[(day(2026, 9, 1), day(2026, 9, 8))],
                    data_as_of=day(2026, 9, 8, 0, 0))

        assessment = self._assess(self._request(shop_ids=["MS_TB1"]))

        self.assertEqual(assessment.status, "complete", assessment.missing_windows)
        self.assertEqual(assessment.gaps, ())

    def test_any_dependency_without_cutoff_leaves_the_common_cutoff_unknown(self):
        self._state("MS_S1", SOURCE, covered=[(day(2026, 9, 1), day(2026, 9, 8))],
                    data_as_of=day(2026, 9, 8, 0, 0))
        self._state("MS_TB1", MULTI_SOURCE, covered=[(day(2026, 9, 1), day(2026, 9, 8))],
                    data_as_of=None)

        assessment = self._assess(self._request())

        self.assertIsNone(assessment.data_as_of,
                          "任一依赖没推进截止，整体截止就是未知，不能拿别的店的截止当共同截止")

    def test_common_cutoff_is_the_earliest_dependency_cutoff(self):
        self._state("MS_S1", SOURCE, covered=[(day(2026, 9, 1), day(2026, 9, 8))],
                    data_as_of=day(2026, 9, 8, 0, 0))
        self._state("MS_TB1", MULTI_SOURCE, covered=[(day(2026, 9, 1), day(2026, 9, 8))],
                    data_as_of=day(2026, 9, 7, 0, 0))

        self.assertEqual(self._assess(self._request()).data_as_of, day(2026, 9, 7, 0, 0))

    def test_source_batches_only_carry_the_channels_actually_used(self):
        self._state("MS_S1", SOURCE, covered=[(day(2026, 9, 1), day(2026, 9, 8))],
                    data_as_of=day(2026, 9, 8, 0, 0))
        self._state("MS_TB1", MULTI_SOURCE, covered=[(day(2026, 9, 1), day(2026, 9, 8))],
                    data_as_of=day(2026, 9, 8, 0, 0))
        for batch_id, source in (("B_USED_OUT", MULTI_SOURCE), ("B_STALE_TRADE", SOURCE)):
            self.conn.execute(
                "INSERT INTO bi.sync_batches(source, entity, shop_id, batch_id, "
                "business_window, mode, row_count) VALUES (%s, 'orders', 'MS_TB1', %s, "
                "tstzrange('2026-09-01','2026-09-08','[)'), 'backfill', 3)",
                (source, batch_id))

        batches = self._assess(self._request(shop_ids=["MS_TB1"])).source_batches

        self.assertIn("B_USED_OUT", batches)
        self.assertNotIn("B_STALE_TRADE", batches,
                         "已经不当用的旧通道批次不能混进血缘")

    def test_gaps_are_attributed_to_the_channel_that_is_behind(self):
        self._state("MS_S1", SOURCE, covered=[(day(2026, 9, 1), day(2026, 9, 8))],
                    data_as_of=day(2026, 9, 8, 0, 0))
        self._state("MS_TB1", MULTI_SOURCE,
                    covered=[(day(2026, 9, 5), day(2026, 9, 8))],
                    data_as_of=day(2026, 9, 8, 0, 0))

        gaps = self._assess(self._request()).gaps

        self.assertTrue(gaps)
        self.assertTrue(all(gap.shop_id == "MS_TB1" for gap in gaps),
                        "缺口只能归因到落后的那条依赖")
        self.assertEqual({gap.source for gap in gaps}, {MULTI_SOURCE})

    def test_previous_period_is_assessed_on_the_same_channels(self):
        # 上一等长区间 [08-25,09-01)：S1 有、TB1 没有 → 公共范围空，不能拿本期覆盖充数。
        self._state("MS_S1", SOURCE,
                    covered=[(day(2026, 8, 25), day(2026, 9, 1)),
                             (day(2026, 9, 1), day(2026, 9, 8))],
                    data_as_of=day(2026, 9, 8, 0, 0))
        self._state("MS_TB1", MULTI_SOURCE, covered=[(day(2026, 9, 1), day(2026, 9, 8))],
                    data_as_of=day(2026, 9, 8, 0, 0))

        previous = self._assess(self._request(start="2026-08-25", end="2026-09-01"))

        self.assertEqual(previous.covered_windows, ())
        self.assertEqual(previous.missing_windows, (("2026-08-25", "2026-09-01"),))
        self.assertEqual(previous.status, "missing")


class SpanAlgebraTests(unittest.TestCase):
    """交集/裁剪是纯函数：不靠数据库也能把边界钉住。"""

    def test_intersection_keeps_holes_and_ordering(self):
        from bi_agent.data_quality import intersect_spans

        left = [(_d(9, 1), _d(9, 8))]
        right = [(_d(9, 3), _d(9, 5)), (_d(9, 6), _d(9, 8))]
        self.assertEqual(intersect_spans(left, right),
                         [(_d(9, 3), _d(9, 5)), (_d(9, 6), _d(9, 8))])

    def test_empty_operand_gives_empty_result(self):
        from bi_agent.data_quality import intersect_spans

        self.assertEqual(intersect_spans([(_d(9, 1), _d(9, 8))], []), [])
        self.assertEqual(intersect_spans([], []), [])

    def test_subtract_reports_what_is_left_out(self):
        from bi_agent.data_quality import subtract_spans

        whole = [(_d(9, 1), _d(9, 8))]
        covered = [(_d(9, 3), _d(9, 5)), (_d(9, 6), _d(9, 8))]
        self.assertEqual(subtract_spans(whole, covered),
                         [(_d(9, 1), _d(9, 3)), (_d(9, 5), _d(9, 6))])


def _d(month: int, day: int) -> date:
    return date(2026, month, day)


def day(year: int, month: int, day_: int, hour: int = 0, minute: int = 0) -> datetime:
    return datetime(year, month, day_, hour, minute, tzinfo=BEIJING)


def multirange_of(pieces: str) -> str:
    return f"tstzmultirange({pieces})" if pieces else "tstzmultirange()"


def flat(spans) -> list:
    items: list = []
    for span in spans:
        items.extend(span)
    return items


if __name__ == "__main__":
    unittest.main()
