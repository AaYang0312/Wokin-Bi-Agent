"""多来源唯一注册表与指标能力门禁（原路线图 Task 5.1）。

口径依据：`docs/superpowers/specs/2026-09-12-multi-source-metrics-design.md` §3–§4。
范围决定：`docs/superpowers/research/2026-09-12-drop-pdd-onboarding.md`——拼多多不接入
支付，注册表里没有 pdd 支付分支，也不保留「开通后再登记」的待办。

分两层：

1. 注册表是纯代码解析，不碰数据库，任何环境都必须跑（skip 不是通过证明）。
2. 请求门禁必须走在金额 SQL 之前：用真实测试库 + reader 身份证明
   「数据齐、覆盖全」的店铺在缺能力标签时一个数字都拿不到。
"""

from __future__ import annotations

import os
import time
import unittest
from types import SimpleNamespace
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

BEIJING = ZoneInfo("Asia/Shanghai")

from bi_agent.sources import (
    AFTERSALE_COHORT_ENTITY, AFTERSALE_ENTITY, AFTERSALE_SOURCE, COHORT_BASIS,
    DOCUMENT_BASIS, METRIC_CAPABILITIES, ORDERS_ENTITY, OUTSTOCK_BASIS,
    OUTSTOCK_SOURCE, PAYMENT_BASIS, REFUND_BASIS, TRADE_LIST_SOURCE,
    ShopRecord, TradeListPlatforms, capabilities_from_evidence,
    resolve_metric_sources, resolve_order_source, unsupported_reason,
)

from .dbfixtures import connect_test_db
from .test_db import FROZEN_NOW, seed_business_case

# 拼多多永久不获得的支付族能力。
PAYMENT_FAMILY = ("paid_amount", "paid_orders", "aov", "product_paid_amount",
                  "cash_difference", "cohort_refund_rate")


def _shop(shop_id: str = "S1", platform: str = "fxg",
          *capabilities: str) -> ShopRecord:
    """服务端加载的店铺记录：测试里显式写出这家店被授予了哪些指标能力。"""
    return ShopRecord(shop_id=shop_id, platform=platform,
                      capabilities=frozenset(capabilities))


class OrderSourceRoutingTests(unittest.TestCase):
    """订单来源只由平台解析；未知平台不得回退交易源。"""

    def test_taoxi_orders_route_to_the_outstock_channel(self):
        for platform in ("tb", "tm"):
            with self.subTest(platform=platform):
                self.assertEqual(resolve_order_source(_shop(platform=platform)),
                                 OUTSTOCK_SOURCE)

    def test_trade_list_platforms_route_to_the_trade_channel(self):
        for platform in TradeListPlatforms:
            with self.subTest(platform=platform):
                self.assertEqual(
                    resolve_order_source(_shop(platform=platform)), TRADE_LIST_SOURCE)

    def test_routing_ignores_case_and_surrounding_space(self):
        self.assertEqual(resolve_order_source(_shop(platform=" TM ")), OUTSTOCK_SOURCE)

    def test_unregistered_platform_fails_closed_without_fallback(self):
        # 1688 / 淘工厂 / 任何没登记的写法：没有来源，也就没有「回退到交易源」。
        for platform in ("1688", "alibabac2m", "unknown", "", None):
            with self.subTest(platform=platform):
                shop = _shop(platform=platform)
                self.assertIsNone(resolve_order_source(shop))
                self.assertEqual(resolve_metric_sources(shop, "erp_documents"), ())
                self.assertEqual(unsupported_reason(shop, "erp_documents"),
                                 "source_unregistered")


class MetricBindingTests(unittest.TestCase):
    """逐店逐指标的来源、口径与时间认证由注册表决定，模型无权指定 source。"""

    def test_taoxi_payment_uses_the_erp_outstock_basis(self):
        bindings = resolve_metric_sources(_shop("S1", "tb", "paid_amount"),
                                          "paid_amount")
        self.assertEqual(len(bindings), 1)
        binding = bindings[0]
        self.assertEqual(binding.source, OUTSTOCK_SOURCE)
        self.assertEqual(binding.basis, OUTSTOCK_BASIS)
        # 出库接口只证明采集了哪段出库范围；没有对照证据就不算支付窗口完整。
        self.assertEqual(binding.time_basis, "outstock_time")
        self.assertFalse(binding.coverage_certified)

    def test_douyin_payment_keeps_its_certified_pay_time(self):
        binding = resolve_metric_sources(_shop("S1", "fxg", "paid_amount"),
                                         "paid_amount")[0]
        self.assertEqual(binding.source, TRADE_LIST_SOURCE)
        self.assertEqual(binding.basis, PAYMENT_BASIS)
        self.assertEqual(binding.time_basis, "pay_time")
        self.assertTrue(binding.coverage_certified)

    def test_other_trade_list_platforms_do_not_inherit_douyin_certification(self):
        # 「不能把 fxg 的认证复制给其他平台」：口径同名可以，时间完整性各自取证。
        for platform in ("jd", "kuaishou", "wxsph", "wsxc"):
            with self.subTest(platform=platform):
                binding = resolve_metric_sources(
                    _shop("S1", platform, "paid_amount"), "paid_amount")[0]
                self.assertEqual(binding.basis, PAYMENT_BASIS)
                self.assertFalse(binding.coverage_certified)

    def test_refund_amount_binds_only_the_after_sale_source(self):
        """设计 §4：退款发生额只需退款发生源。

        订单还没取到的退款也是真实发生的退款；把 orders 也列进依赖，就会拿“订单没覆盖”
        去打死一个本可回答的问题（计划 5.3b）。
        """
        bindings = resolve_metric_sources(_shop("S1", "fxg", "refund_amount"),
                                          "refund_amount")
        self.assertEqual({item.source for item in bindings}, {AFTERSALE_SOURCE})
        self.assertEqual({item.entity for item in bindings}, {AFTERSALE_ENTITY})

    def test_cash_difference_binds_both_the_payment_and_refund_sources(self):
        # 现金差 = 支付 − 期间退款发生：两端都要，所以两条依赖都在。
        bindings = resolve_metric_sources(_shop("S1", "fxg", "cash_difference"),
                                          "cash_difference")
        self.assertEqual({item.source for item in bindings},
                         {TRADE_LIST_SOURCE, AFTERSALE_SOURCE})
        self.assertEqual({item.entity for item in bindings},
                         {ORDERS_ENTITY, AFTERSALE_ENTITY})

    def test_cohort_binds_the_cohort_entity_not_the_occurrence_entity(self):
        entities = {item.entity for item in resolve_metric_sources(
            _shop("S1", "fxg", "cohort_refund_rate"), "cohort_refund_rate")}
        self.assertIn(AFTERSALE_COHORT_ENTITY, entities)
        self.assertNotIn(AFTERSALE_ENTITY, entities)

    def test_refund_bindings_carry_their_own_basis_and_are_not_certified(self):
        # 退款两端各自有口径，不能拿单据口径冒充退款口径。
        bindings = {item.entity: item for item in resolve_metric_sources(
            _shop("S1", "fxg", "refund_amount"), "refund_amount")}
        self.assertEqual(bindings[AFTERSALE_ENTITY].basis, REFUND_BASIS)
        # 没有任何平台拿退款完成时间与后台账单对过完整性：这里必须是 False。
        self.assertFalse(bindings[AFTERSALE_ENTITY].coverage_certified)
        cohort = {item.entity: item for item in resolve_metric_sources(
            _shop("S1", "fxg", "cohort_refund_rate"), "cohort_refund_rate")}
        self.assertEqual(cohort[AFTERSALE_COHORT_ENTITY].basis, COHORT_BASIS)

    def test_one_shop_resolves_exactly_one_order_source(self):
        # 同店同指标同期间只有一个订单来源，禁止新旧来源重叠相加。
        order_bindings = [item for item in resolve_metric_sources(
            _shop("S1", "tb", "paid_amount"), "paid_amount")
            if item.entity == ORDERS_ENTITY]
        self.assertEqual(len(order_bindings), 1)

    def test_binding_carries_the_shop_id_not_a_shared_placeholder(self):
        bindings = resolve_metric_sources(_shop("77", "tm", "quantity"), "quantity")
        self.assertEqual({item.shop_id for item in bindings}, {"77"})


class PddScopeTests(unittest.TestCase):
    """2026-09-12 决定：拼多多只保留已核验单据能力，支付族永久解析不通。"""

    def test_pdd_resolves_documents_only(self):
        bindings = resolve_metric_sources(_shop("P1", "pdd", "erp_documents"),
                                          "erp_documents")
        self.assertEqual(len(bindings), 1)
        self.assertEqual(bindings[0].basis, DOCUMENT_BASIS)

    def test_pdd_payment_metrics_never_resolve(self):
        for metric in PAYMENT_FAMILY:
            with self.subTest(metric=metric):
                # 即使有人误把标签写进库，注册表也不放行。
                shop = _shop("P1", "pdd", metric)
                self.assertEqual(resolve_metric_sources(shop, metric), ())
                self.assertEqual(unsupported_reason(shop, metric),
                                 "capability_unavailable")

    def test_pdd_documents_come_from_the_outstock_channel(self):
        # 官方交易接口排除拼多多；用交易源“验证为空”会伪造完整覆盖，所以单据源也是出库。
        binding = resolve_metric_sources(_shop("P1", "pdd", "erp_documents"),
                                         "erp_documents")[0]
        self.assertEqual(binding.source, OUTSTOCK_SOURCE)

    def test_pdd_quantity_and_refund_stay_closed(self):
        # 行标识缺失、售后未逐店取证：都不在「仅单据能力」之内。
        for metric in ("quantity", "refund_amount"):
            with self.subTest(metric=metric):
                self.assertEqual(
                    resolve_metric_sources(_shop("P1", "pdd", metric), metric), ())


class CapabilityTagTests(unittest.TestCase):
    """能力标签就是指标名；实体存在不等于指标可用。"""

    def test_legacy_entity_tags_grant_no_metric(self):
        legacy = _shop("S1", "fxg", "orders", "aftersales_occurrence",
                       "aftersales_cohort")
        for metric in sorted(METRIC_CAPABILITIES):
            with self.subTest(metric=metric):
                self.assertEqual(resolve_metric_sources(legacy, metric), ())

    def test_empty_capabilities_grant_nothing(self):
        shop = _shop("S1", "fxg")
        for metric in sorted(METRIC_CAPABILITIES):
            with self.subTest(metric=metric):
                self.assertEqual(resolve_metric_sources(shop, metric), ())
                self.assertEqual(unsupported_reason(shop, metric),
                                 "capability_ungranted")

    def test_capability_vocabulary_covers_every_published_metric(self):
        from bi_agent.metrics import METRIC_DEFINITIONS
        from bi_agent.sources import MANUAL_METRIC_CAPABILITIES

        # ERP 族（注册表 ceiling 词表，对账证据授予）+ 手工导出族（导入面授予）
        # = 全部已发布指标：自造标签在这两份词表之外没有落点。
        self.assertEqual(set(METRIC_CAPABILITIES) | set(MANUAL_METRIC_CAPABILITIES),
                         set(METRIC_DEFINITIONS))

    def test_answerable_metric_reports_no_reason(self):
        # 能回答时必须是 None，不能拿一个看起来像原因码的字符串占位。
        self.assertIsNone(unsupported_reason(_shop("S1", "fxg", "paid_amount"),
                                             "paid_amount"))

    def test_uncertified_platform_is_grantable_but_flagged(self):
        """能力开通与时间窗口认证是两件事，后者必须留着给 Task 5.2 消费。

        没有这个旗标，5.2c “完整支付窗口”就无法拒绝把出库样本当全窗口；
        现在它没有任何生产者，本用例钉住它不会因为“暂时没人读”被删。
        """
        granted = capabilities_from_evidence("tm", {(OUTSTOCK_SOURCE, ORDERS_ENTITY):
                                                    "passed"})
        self.assertIn("paid_amount", granted)
        binding = resolve_metric_sources(_shop("S1", "tm", "paid_amount"),
                                         "paid_amount")[0]
        self.assertFalse(binding.coverage_certified)
        # 已认证的抖音则相反：同一能力，时间口径可声称完整。
        certified = resolve_metric_sources(_shop("S1", "fxg", "paid_amount"),
                                           "paid_amount")[0]
        self.assertTrue(certified.coverage_certified)


class EvidenceGrantTests(unittest.TestCase):
    """能力只能由逐源核验证据推导，并且永远不超过该平台上限。"""

    EVIDENCE = {(TRADE_LIST_SOURCE, ORDERS_ENTITY): "passed",
                (AFTERSALE_SOURCE, AFTERSALE_ENTITY): "passed",
                (AFTERSALE_SOURCE, AFTERSALE_COHORT_ENTITY): "passed"}

    def test_full_passed_evidence_grants_the_whole_ceiling(self):
        self.assertEqual(capabilities_from_evidence("fxg", self.EVIDENCE),
                         set(METRIC_CAPABILITIES))

    def test_unknown_cohort_withholds_only_the_cohort_metric(self):
        evidence = dict(self.EVIDENCE)
        evidence[(AFTERSALE_SOURCE, AFTERSALE_COHORT_ENTITY)] = "unknown"
        granted = capabilities_from_evidence("fxg", evidence)
        self.assertNotIn("cohort_refund_rate", granted)
        self.assertIn("refund_amount", granted)

    def test_failed_or_missing_order_evidence_withholds_payment_metrics(self):
        for status in ("failed", "unknown"):
            with self.subTest(status=status):
                evidence = dict(self.EVIDENCE)
                evidence[(TRADE_LIST_SOURCE, ORDERS_ENTITY)] = status
                granted = capabilities_from_evidence("fxg", evidence)
                self.assertFalse({"paid_amount", "aov", "cash_difference"} & granted)

    def test_pdd_evidence_cannot_open_payment(self):
        self.assertEqual(capabilities_from_evidence(
            "pdd", {(OUTSTOCK_SOURCE, ORDERS_ENTITY): "passed"}),
            {"erp_documents"})

    def test_unregistered_platform_grants_nothing(self):
        self.assertEqual(capabilities_from_evidence("alibabac2m", self.EVIDENCE), set())

    def test_evidence_must_come_from_the_registered_source(self):
        # 淘系只有交易源状态时不能当作出库源的证据。
        self.assertNotIn("paid_amount", capabilities_from_evidence("tb", self.EVIDENCE))

    def test_taoxi_outstock_evidence_opens_payment_but_not_cohort(self):
        granted = capabilities_from_evidence("tm", {
            (OUTSTOCK_SOURCE, ORDERS_ENTITY): "passed",
            (AFTERSALE_SOURCE, AFTERSALE_ENTITY): "passed",
            (AFTERSALE_SOURCE, AFTERSALE_COHORT_ENTITY): "unknown"})
        self.assertIn("paid_amount", granted)
        self.assertIn("refund_amount", granted)
        self.assertNotIn("cohort_refund_rate", granted)


@unittest.skipUnless(os.getenv("BI_TEST_ADMIN_DSN"), "未配置独立测试数据库")
class CapabilityGateDatabaseTests(unittest.TestCase):
    """门禁走在金额 SQL 之前：数据齐、覆盖全，也不算「有能力」。"""

    def setUp(self):
        self.conn = connect_test_db(self)
        seed_business_case(self.conn)
        self.conn.execute("SET LOCAL ROLE bi_reader")

    def _query(self, metrics, shop_ids=("S1",)):
        from bi_agent.metrics import QueryRequest, query_business

        request = QueryRequest(start="2026-09-01", end="2026-09-08",
                               shop_ids=list(shop_ids), metrics=list(metrics))
        return query_business(self.conn, request,
                              allowed_shop_ids=frozenset({"S1", "S2"}),
                              now=FROZEN_NOW, deadline=time.monotonic() + 30)

    def _grant(self, capabilities: str, *, shop_id: str = "S1",
               platform: str | None = None) -> None:
        self.conn.execute("RESET ROLE")
        if platform is None:
            self.conn.execute(
                "UPDATE bi.shops SET capabilities=%s::text[] WHERE shop_id=%s",
                (capabilities, shop_id))
        else:
            self.conn.execute(
                "UPDATE bi.shops SET capabilities=%s::text[], platform=%s "
                "WHERE shop_id=%s", (capabilities, platform, shop_id))
            if platform in ("pdd", "tb", "tm"):
                # 出库通道平台的依赖在出库源上：把种子留在交易通道下的状态行
                # 原样照一份过去。旧行保留着，正好证明它不再参与覆盖判定。
                self.conn.execute(
                    "INSERT INTO bi.sync_state(source, entity, shop_id, watermark, "
                    "covered, data_as_of, quality_status, quality_rule) "
                    "SELECT %s, entity, shop_id, watermark, covered, data_as_of, "
                    "quality_status, quality_rule FROM bi.sync_state "
                    "WHERE source='erp.trade.list.query' AND entity='orders' AND shop_id=%s",
                    (OUTSTOCK_SOURCE, shop_id))
        self.conn.execute("SET LOCAL ROLE bi_reader")

    def test_unregistered_platform_gets_the_onboarding_gap_not_the_capability_one(self):
        # 平台没登记来源时，“换个已开通的指标”是错建议：只能先完成接入取证。
        self._grant("{paid_amount,erp_documents}", platform="alibabac2m")
        result = self._query(["paid_amount"])
        self.assertEqual(result.status, "missing_data", result.limitations)
        self.assertIn("source_not_onboarded", self._codes(result))
        self.assertNotIn("capability_unavailable", self._codes(result))

    def test_two_shops_with_two_gaps_get_two_distinct_disclosures(self):
        # 未登记来源 + 有来源但未授予能力：同一请求里分开归因，不写成一回事。
        self._grant("{paid_amount}", shop_id="S1", platform="alibabac2m")
        self._grant("{erp_documents}", shop_id="S2")
        result = self._query(["paid_amount"], shop_ids=("S1", "S2"))
        codes = self._codes(result)
        self.assertIn("source_not_onboarded", codes)
        self.assertIn("capability_unavailable", codes)
        self.assertEqual([item for item in result.limitations if "店铺" in item],
                         ["1 家店铺的来源尚未开通（未授权或未同步），缩小日期范围不会补上这段数据",
                          "1 家店铺缺少 paid_amount 的已核验能力，未执行金额查询"])

    def _codes(self, result) -> list[str]:
        from bi_agent.business_query.nodes import _limitation_codes

        return _limitation_codes(result.limitations)

    def test_missing_capability_refuses_an_otherwise_answerable_query(self):
        # 种子数据本来完整可答（支付 1000）；撤掉能力标签后必须一个数字都不给。
        self._grant("{}")
        result = self._query(["paid_amount"])
        self.assertEqual(result.status, "missing_data", result.limitations)
        self.assertEqual(result.data, [])
        self.assertIn("capability_unavailable", self._codes(result))

    def test_legacy_entity_tags_do_not_answer_money(self):
        self._grant("{orders,aftersales_occurrence,aftersales_cohort}")
        result = self._query(["paid_amount"])
        self.assertEqual(result.status, "missing_data", result.limitations)
        self.assertEqual(result.data, [])

    def test_granted_capability_still_answers_the_same_query(self):
        self._grant("{paid_amount}")
        result = self._query(["paid_amount"])
        self.assertEqual(result.status, "ok", result.limitations)
        # numeric 求和带尺度（1000.000000），比对按数值而不是按字符串长度。
        self.assertEqual(Decimal(result.data[0]["paid_amount"]), Decimal("1000"))

    def test_documents_only_answers_documents_but_not_money(self):
        self._grant("{erp_documents}", platform="pdd")
        documents = self._query(["erp_documents"])
        self.assertEqual(documents.status, "ok", documents.limitations)
        self.assertEqual(documents.data[0]["erp_documents"], 6)

        money = self._query(["paid_amount"])
        self.assertEqual(money.status, "missing_data", money.limitations)
        self.assertEqual(money.data, [])

    def test_local_filter_channel_answers_with_disclosure_when_covered(self):
        """S3 落地（默认建议，待所有者追认）：local_filter 平台在完整/新鲜覆盖与
        全部既有门禁下可答+披露；支付口径剔除关闭后退款单（另有专类覆盖）。
        数据齐、覆盖全时不再因时间口径拒答，但也不宣称完整支付窗口。"""
        self._grant("{paid_amount}", platform="tb")
        result = self._query(["paid_amount"])
        self.assertEqual(result.status, "ok", result.limitations)
        self.assertIn("coverage_time_basis_unverified", self._codes(result),
                      "可答+披露：按可观测样本披露，不冒充已认证")
        self.assertEqual(sorted(result.filters["shop_ids"]), ["S1"],
                         "原请求不得被改写")

    def test_document_count_is_answered_but_disclosed_as_a_sample(self):
        """设计 §4：单据数仍可在覆盖成立时查询，但必须披露它是出库来源样本。"""
        self._grant("{erp_documents}", platform="tb")
        result = self._query(["erp_documents"])
        self.assertEqual(result.status, "ok", result.limitations)
        self.assertIn("coverage_time_basis_unverified", self._codes(result))
        self.assertEqual(result.coverage.status, "complete",
                         "披露时间口径不等于缺覆盖：两件事分开说")

    def test_certified_channel_neither_refuses_nor_discloses(self):
        self._grant("{paid_amount}", platform="fxg")
        result = self._query(["paid_amount"])
        self.assertEqual(result.status, "ok", result.limitations)
        self.assertNotIn("coverage_time_basis_unverified", self._codes(result))

    def test_unmeasured_trade_channel_answers_with_a_disclosure(self):
        """同通道同参数但没逐店对照过：出数 + 披露，不把没测过说成不成立。"""
        self._grant("{paid_amount}", platform="kuaishou")
        result = self._query(["paid_amount"])
        self.assertEqual(result.status, "ok", result.limitations)
        self.assertIn("coverage_time_basis_unverified", self._codes(result))

    def test_mixed_scope_keeps_the_request_and_names_the_missing_group(self):
        # 一家有能力、一家没有：不得悄悄删店后冒充全量成功。
        self._grant("{paid_amount}", shop_id="S1")
        self._grant("{erp_documents}", shop_id="S2", platform="pdd")
        result = self._query(["paid_amount"], shop_ids=("S1", "S2"))
        self.assertEqual(result.status, "missing_data", result.limitations)
        self.assertIn("capability_unavailable", self._codes(result))
        self.assertEqual(sorted(result.filters["shop_ids"]), ["S1", "S2"])
        # 缺能力不能伪装成缺覆盖：能力门禁走在覆盖读取之前，覆盖保持“未评估”。
        self.assertIsNone(result.coverage.start)
        self.assertEqual(result.coverage.status, "missing")


@unittest.skipUnless(os.getenv("BI_TEST_ADMIN_DSN"), "未配置独立测试数据库")
class BasisContractDatabaseTests(unittest.TestCase):
    """设计 §6：口径（basis）必须跟着结果走，不能把不同口径汇成一个数再比较。

    抖音的 `platform_payment/v1` 与淘系出库的 `erp_outstock_payment/v1` 同名不同义：
    合起来当“全平台销售额”，或者拿它算增长率/排名，都是把口径差当成经营差异。
    """

    def setUp(self):
        self.conn = connect_test_db(self)
        seed_business_case(self.conn)          # S1 = fxg，支付 1000
        self.conn.execute("RESET ROLE")
        self._seed_tb_shop()
        self.conn.execute("SET LOCAL ROLE bi_reader")

    def _ms(self, moment):
        return int(moment.timestamp() * 1000)

    def _seed_tb_shop(self) -> None:
        from bi_agent.sync import apply_trade, normalise_trade

        self.conn.execute(
            "INSERT INTO bi.shops(shop_id, platform, display_name, capabilities) "
            "VALUES ('TB1','tb','淘系店','{paid_amount,erp_documents}') "
            "ON CONFLICT (shop_id) DO UPDATE SET capabilities = EXCLUDED.capabilities")
        trade = normalise_trade({
            "sid": "E-TB1", "userId": "TB1", "tid": "C-TB1", "payAmount": "100.00",
            "payTime": self._ms(datetime(2026, 9, 2, 10, tzinfo=BEIJING)),
            "updTime": self._ms(datetime(2026, 9, 2, 11, tzinfo=BEIJING)),
            "status": "TRADE_NO_CREATED_EVALUATION", "sysStatus": 0,
            "orders": [{"skuId": "S1", "num": "1", "payAmount": "100.00",
                        "itemSysId": "P_A", "oid": "E-TB1-1"}],
        }, source=OUTSTOCK_SOURCE)
        assert trade["normalization_status"] == "normal", trade
        self.assertTrue(apply_trade(self.conn, trade, batch_id="tb-seed"))
        self.conn.execute(
            "INSERT INTO bi.sync_state(source, entity, shop_id, watermark, covered, "
            "data_as_of, quality_status, quality_rule) VALUES "
            "(%s, 'orders', 'TB1', '2026-09-08 00:00+08', "
            "tstzmultirange(tstzrange('2026-09-01','2026-09-08','[)')), "
            "'2026-09-08 00:00+08', 'passed', 'test-seed') "
            "ON CONFLICT (source, entity, shop_id) DO UPDATE SET "
            "covered = EXCLUDED.covered, data_as_of = EXCLUDED.data_as_of",
            (OUTSTOCK_SOURCE,))

    def _query(self, metrics, shop_ids, **overrides):
        import time as time_module

        from bi_agent.metrics import QueryRequest, query_business

        defaults = dict(start="2026-09-01", end="2026-09-08",
                        shop_ids=list(shop_ids), metrics=list(metrics))
        defaults.update(overrides)
        return query_business(self.conn, QueryRequest(**defaults),
                              allowed_shop_ids=frozenset({"S1", "S2", "TB1"}),
                              now=FROZEN_NOW, deadline=time_module.monotonic() + 30)

    def test_mixed_time_basis_cannot_be_merged_into_one_total(self):
        """两家都能答的 `erp_documents`，一个是 pay_time、一个是 outstock_time。

        兼容性看 (口径, 时间归属) 两件事，不能只看指标同名——“ERP单据数”在两个通道
        上是两个不同问题的答案，加起来不是“全平台单据数”。
        """
        result = self._query(["erp_documents"], ["S1", "TB1"])
        self.assertEqual(result.status, "invalid_parameters", result.limitations)
        self.assertIn("basis_incompatible", self._codes(result))
        self.assertEqual(result.data, [])

    def test_product_and_day_grouping_are_equally_incompatible(self):
        day = self._query(["erp_documents"], ["S1", "TB1"], group_by="day")
        self.assertEqual(day.status, "invalid_parameters")
        self.assertIn("basis_incompatible", self._codes(day))

    def test_separate_policy_answers_per_shop_rows_with_their_own_basis(self):
        result = self._query(["erp_documents"], ["S1", "TB1"], group_by="shop",
                             basis_policy="separate")

        self.assertEqual(result.status, "ok", result.limitations)
        rows = {row["shop_id"]: row for row in result.data}
        self.assertEqual(set(rows), {"S1", "TB1"})
        self.assertEqual(rows["S1"]["erp_documents"], 6)
        self.assertEqual(rows["TB1"]["erp_documents"], 1)
        keys = {(entry["shop_id"], entry["time_basis"]) for entry in result.basis}
        self.assertEqual(keys, {("S1", "pay_time"), ("TB1", "outstock_time")})

    def test_strict_policy_still_refuses_per_shop_mixing(self):
        """strict 的语义就是“不合并也不行，先确认口径”；separate 才是显式分列。"""
        result = self._query(["erp_documents"], ["S1", "TB1"], group_by="shop",
                             basis_policy="separate")
        strict = self._query(["erp_documents"], ["S1", "TB1"], group_by="shop")
        self.assertEqual(result.status, "ok", result.limitations)
        self.assertEqual(strict.status, "invalid_parameters")
        self.assertIn("basis_incompatible", self._codes(strict))

    def test_single_shop_result_still_carries_its_basis(self):
        """单店也必须附 basis：否则“淘系的单据数”会被当成按付款时间的全平台口径。"""
        result = self._query(["erp_documents"], ["TB1"])
        self.assertEqual(result.status, "ok", result.limitations)
        self.assertEqual([(item["shop_id"], item["metric"], item["basis"],
                           item["time_basis"]) for item in result.basis],
                         [("TB1", "erp_documents", DOCUMENT_BASIS, "outstock_time")])
        # 内部项保留来源，公开投影只给 shop_ref：口径不能变成主键的泄露面。
        self.assertTrue(all(item["source"] == OUTSTOCK_SOURCE for item in result.basis))

    def test_comparison_across_a_source_switch_is_refused_not_explained_as_growth(self):
        """同店换来源时，两期差额不能被解释成增长：那只是口径变了。"""
        self.conn.execute("RESET ROLE")
        # TB1 的上期落在交易通道（旧来源），本期落在出库通道：两期口径不同。
        self.conn.execute(
            "INSERT INTO bi.sync_state(source, entity, shop_id, watermark, covered, "
            "data_as_of, quality_status, quality_rule) VALUES "
            "(%s, 'orders', 'TB1', '2026-09-01 00:00+08', "
            "tstzmultirange(tstzrange('2026-08-25','2026-09-01','[)')), "
            "'2026-09-01 00:00+08', 'passed', 'test-seed')",
            (TRADE_LIST_SOURCE,))
        self.conn.execute(
            "INSERT INTO bi.orders(shop_id, erp_id, source, commercial_ids, active, "
            "raw_pay_amount, paid_at, source_updated_at, batch_id) VALUES "
            "('TB1','E-TB0',%s,ARRAY['C-TB0'],true,50,'2026-08-28 10:00+08',now(),'tb-seed')",
            (TRADE_LIST_SOURCE,))
        self.conn.execute(
            "INSERT INTO bi.order_payments(shop_id, commercial_id, paid_at, amount, "
            "currency, basis, verified, source_updated_at) VALUES "
            "('TB1','C-TB0','2026-08-28 10:00+08',50,'CNY','head',true,now())")
        self.conn.execute("SET LOCAL ROLE bi_reader")

        result = self._query(["erp_documents"], ["TB1"], start="2026-09-01",
                             compare="previous_period")
        self.assertEqual(result.status, "invalid_parameters", result.limitations)
        self.assertIn("basis_incompatible", self._codes(result))

    def test_projection_keeps_basis_but_strips_identifiers_and_channels(self):
        """口径要发给模型，主键与接口方法名不行。"""
        import json

        from bi_agent.business_query.tool import to_model_result, to_public_artifact
        from bi_agent.catalog import build_catalog

        result = self._query(["erp_documents"], ["S1", "TB1"], group_by="shop",
                             basis_policy="separate")
        catalog = build_catalog(self.conn, result,
                               allowed_shop_ids=frozenset({"S1", "S2", "TB1"}))

        model_payload = to_model_result(result, catalog)
        artifact_payload = to_public_artifact(result, catalog)
        text = json.dumps(model_payload, ensure_ascii=False)

        self.assertTrue(model_payload["basis"], "口径必须发给模型")
        for item in model_payload["basis"]:
            self.assertNotIn("shop_id", item)
            self.assertNotIn("source", item)
            self.assertIn("shop_ref", item)
        self.assertNotIn("TB1", text)
        self.assertNotIn("erp.trade", text)
        self.assertTrue(artifact_payload["basis"])

    def test_basis_carrying_an_erp_key_is_rejected(self):
        from bi_agent.business_query.tool import to_public_artifact
        from bi_agent.catalog import build_catalog

        result = self._query(["erp_documents"], ["TB1"])
        catalog = build_catalog(self.conn, result,
                                allowed_shop_ids=frozenset({"S1", "S2", "TB1"}))
        # 内部形状带真实主键与来源；投影必须先转换再校验，不能原样转发。
        self.assertTrue(all("shop_id" in item for item in result.basis))
        payload = to_public_artifact(result, catalog)
        self.assertTrue(all("shop_id" not in item for item in payload["basis"]))

    def test_deterministic_summary_states_the_basis(self):
        """兜底摘要不能比正常结果少说口径。"""
        from bi_agent.business_query.tool import to_model_result
        from bi_agent.catalog import build_catalog
        from bi_agent.response_summary import render_result_summary

        result = self._query(["erp_documents"], ["S1", "TB1"], group_by="shop",
                             basis_policy="separate")
        catalog = build_catalog(self.conn, result,
                                allowed_shop_ids=frozenset({"S1", "S2", "TB1"}))
        summary = render_result_summary(SimpleNamespace(
            model_payload=to_model_result(result, catalog)))

        self.assertIn("统计口径", summary)
        self.assertIn("erp_document/v1", summary)
        self.assertIn("outstock_time", summary)

    def test_projection_keeps_basis_but_strips_identifiers_and_channels(self):
        """口径要发给模型，主键与接口方法名不行。"""
        import json

        from bi_agent.business_query.tool import to_model_result, to_public_artifact
        from bi_agent.catalog import build_catalog

        result = self._query(["erp_documents"], ["S1", "TB1"], group_by="shop",
                             basis_policy="separate")
        catalog = build_catalog(self.conn, result,
                               allowed_shop_ids=frozenset({"S1", "S2", "TB1"}))

        model_payload = to_model_result(result, catalog)
        artifact_payload = to_public_artifact(result, catalog)
        text = json.dumps(model_payload, ensure_ascii=False)

        self.assertTrue(model_payload["basis"], "口径必须发给模型")
        for item in model_payload["basis"]:
            self.assertNotIn("shop_id", item)
            self.assertNotIn("source", item)
            self.assertIn("shop_ref", item)
        self.assertNotIn("TB1", text)
        self.assertNotIn("erp.trade", text)
        self.assertTrue(artifact_payload["basis"])

    def test_basis_carrying_an_erp_key_is_rejected(self):
        from bi_agent.business_query.tool import to_public_artifact
        from bi_agent.catalog import build_catalog

        result = self._query(["erp_documents"], ["TB1"])
        catalog = build_catalog(self.conn, result,
                                allowed_shop_ids=frozenset({"S1", "S2", "TB1"}))
        # 内部形状带真实主键与来源；投影必须先转换再校验，不能原样转发。
        self.assertTrue(all("shop_id" in item for item in result.basis))
        payload = to_public_artifact(result, catalog)
        self.assertTrue(all("shop_id" not in item for item in payload["basis"]))

    def test_deterministic_summary_states_the_basis(self):
        """兜底摘要不能比正常结果少说口径。"""
        from bi_agent.business_query.tool import to_model_result
        from bi_agent.catalog import build_catalog
        from bi_agent.response_summary import render_result_summary

        result = self._query(["erp_documents"], ["S1", "TB1"], group_by="shop",
                             basis_policy="separate")
        catalog = build_catalog(self.conn, result,
                                allowed_shop_ids=frozenset({"S1", "S2", "TB1"}))
        summary = render_result_summary(SimpleNamespace(
            model_payload=to_model_result(result, catalog)))

        self.assertIn("统计口径", summary)
        self.assertIn("erp_document/v1", summary)
        self.assertIn("outstock_time", summary)

    def _codes(self, result) -> list[str]:
        from bi_agent.business_query.nodes import _limitation_codes

        return _limitation_codes(result.limitations)


@unittest.skipUnless(os.getenv("BI_TEST_ADMIN_DSN"), "未配置独立测试数据库")
class CapabilityMaintenanceTests(unittest.TestCase):
    """能力的唯一开通路径：逐来源对账证据，不是“同步跑成功了”。"""

    def setUp(self):
        from bi_agent.data_quality import QUALITY_RULE

        self.rule = QUALITY_RULE
        self.conn = connect_test_db(self)
        self.conn.execute(
            "INSERT INTO bi.shops(shop_id, platform, display_name) VALUES "
            "('CM_FXG','fxg','取证店A'), ('CM_PDD','pdd','取证店B') "
            "ON CONFLICT (shop_id) DO UPDATE SET display_name = EXCLUDED.display_name")

    def _state(self, shop_id: str, source: str, entity: str,
               status: str, rule: str | None = None) -> None:
        self.conn.execute(
            "INSERT INTO bi.sync_state(source, entity, shop_id, watermark, quality_status, "
            "quality_rule) VALUES (%s, %s, %s, '2026-09-08 00:00+08', %s, %s) "
            "ON CONFLICT (source, entity, shop_id) DO UPDATE SET "
            "quality_status = EXCLUDED.quality_status, quality_rule = EXCLUDED.quality_rule",
            (source, entity, shop_id, status, rule or self.rule))

    def _current(self, shop_id: str) -> set[str]:
        row = self.conn.execute(
            "SELECT capabilities FROM bi.shops WHERE shop_id=%s", (shop_id,)).fetchone()
        return set(row[0] or [])

    def test_reconciled_evidence_grants_and_is_written_on_apply(self):
        from bi_agent.sync import recompute_shop_capabilities

        for entity in (ORDERS_ENTITY, AFTERSALE_ENTITY, AFTERSALE_COHORT_ENTITY):
            source = TRADE_LIST_SOURCE if entity == ORDERS_ENTITY else AFTERSALE_SOURCE
            self._state("CM_FXG", source, entity, "passed")

        grants = recompute_shop_capabilities(self.conn, ["CM_FXG"], apply=True)

        self.assertEqual(grants[0].granted, set(METRIC_CAPABILITIES))
        self.assertEqual(self._current("CM_FXG"), set(METRIC_CAPABILITIES))

    def test_sync_success_without_reconciliation_grants_nothing(self):
        # 有同步状态行、甚至写了 watermark，但没有对账凭证 → 一个能力也不开通。
        from bi_agent.sync import recompute_shop_capabilities

        self._state("CM_FXG", TRADE_LIST_SOURCE, ORDERS_ENTITY, "unknown")
        self.conn.execute("UPDATE bi.shops SET capabilities='{paid_amount}' "
                          "WHERE shop_id='CM_FXG'")

        grants = recompute_shop_capabilities(self.conn, ["CM_FXG"], apply=True)

        self.assertEqual(grants[0].granted, set())
        self.assertEqual(self._current("CM_FXG"), set(), "证据消失后能力必须回收")

    def test_stale_quality_rule_grants_nothing(self):
        # 口径升级后旧 passed 不得沿用：质量规则版本不一致就是“从未核验”。
        from bi_agent.sync import recompute_shop_capabilities

        self._state("CM_FXG", TRADE_LIST_SOURCE, ORDERS_ENTITY, "passed",
                    rule="legacy:quality_ok")
        self.assertEqual(
            recompute_shop_capabilities(self.conn, ["CM_FXG"])[0].granted, set())

    def test_pdd_evidence_only_opens_documents(self):
        from bi_agent.sync import recompute_shop_capabilities

        self._state("CM_PDD", OUTSTOCK_SOURCE, ORDERS_ENTITY, "passed")
        self.assertEqual(
            recompute_shop_capabilities(self.conn, ["CM_PDD"])[0].granted,
            {"erp_documents"})

    def test_unregistered_platform_is_reported_not_silently_skipped(self):
        from bi_agent.sync import recompute_shop_capabilities

        self.conn.execute("INSERT INTO bi.shops(shop_id, platform, display_name) "
                          "VALUES ('CM_UNK','alibabac2m','未登记店') "
                          "ON CONFLICT (shop_id) DO NOTHING")
        grants = recompute_shop_capabilities(self.conn, ["CM_UNK"])
        self.assertEqual(grants[0].granted, set())
        self.assertEqual(unsupported_reason(
            ShopRecord.from_row("CM_UNK", "alibabac2m", ()), "erp_documents"),
            "source_unregistered")

    def test_apply_writes_every_change_in_one_transaction(self):
        from bi_agent.sync import recompute_shop_capabilities

        self._state("CM_FXG", TRADE_LIST_SOURCE, ORDERS_ENTITY, "passed")
        self.conn.execute("UPDATE bi.shops SET capabilities='{paid_amount}' "
                          "WHERE shop_id='CM_FXG'")
        grants = recompute_shop_capabilities(self.conn, ["CM_FXG", "CM_PDD"],
                                             apply=True)
        self.assertEqual(self._current("CM_FXG"), set(grants[0].granted))
        self.assertIn("paid_amount", grants[0].granted)
        # 只有订单证据时，退款类不该被开通：一次事务写的是完整集合，不是逐项累加。
        self.assertNotIn("refund_amount", grants[0].granted)

    def test_targets_default_to_the_authorized_scope(self):
        from bi_agent.sync import capability_target_shops

        self.assertEqual(capability_target_shops(self.conn, {"CM_FXG"}), ["CM_FXG"])
        every = capability_target_shops(self.conn, {"CM_FXG"}, all_shops=True)
        self.assertIn("CM_PDD", every, "回收必须能碰到授权范围外还在挂旧标签的店")


@unittest.skipUnless(os.getenv("BI_TEST_ADMIN_DSN"), "未配置独立测试数据库")
class S3ClosedRefundedCaliberTests(unittest.TestCase):
    """S3 关闭单口径决策落地（默认建议，待所有者追认）：

    tb/tm（`local_filter`）的支付口径在查询侧剔除**结构性证明的关闭后退款单**的
    已核验支付；退款仍单独列示；`paid_amount − refund_amount` 只叫期间收支差额。
    关闭**未**退款单的支付必须留在口径里；fxg 语义不变；覆盖不完整照旧 fail closed；
    存量事实不改写（查询政策，无需重述/replay）。
    """

    WINDOW = ("2026-09-01", "2026-09-08")

    def setUp(self):
        from tests.dbfixtures import connect_test_db

        self.conn = connect_test_db(self)
        self.conn.execute(self._migration_sql())

    @staticmethod
    def _migration_sql() -> str:
        from pathlib import Path

        path = Path(__file__).parents[1] / "sql" / "026_closed_refunded_payment_caliber.sql"
        return path.read_text(encoding="utf-8")

    def _shop(self, shop_id: str, platform: str) -> None:
        from bi_agent.data_quality import QUALITY_RULE

        self.conn.execute(
            "INSERT INTO bi.shops(shop_id, platform, display_name, capabilities) "
            "VALUES (%s, %s, %s, '{paid_amount, paid_orders, aov, cash_difference, "
            "refund_amount, cohort_refund_rate, erp_documents}') "
            "ON CONFLICT (shop_id) DO UPDATE SET platform = EXCLUDED.platform",
            (shop_id, platform, f"S3测试店{shop_id}"))
        # 业务覆盖（local_filter 路径写入的 orders 实体）+ 退款发生/同批覆盖。
        # 订单实体跟平台走（淘系=出库源）；售后实体一律是售后通道。
        order_source = ("erp.trade.outstock.simple.query" if platform == "tb"
                        else "erp.trade.list.query")
        for entity, source in (("orders", order_source),
                               ("aftersales_occurrence", "erp.aftersale.list.query"),
                               ("aftersales_cohort", "erp.aftersale.list.query")):
            self.conn.execute(
                "INSERT INTO bi.sync_state(source, entity, shop_id, watermark, "
                "covered, data_as_of, quality_status, quality_rule) VALUES "
                "(%s, %s, %s, '2026-09-08 00:00+08', "
                "tstzmultirange(tstzrange('2026-09-01','2026-09-08','[)')), "
                "'2026-09-08 00:00+08', 'passed', %s) "
                "ON CONFLICT (source, entity, shop_id) DO UPDATE SET "
                "covered = EXCLUDED.covered, data_as_of = EXCLUDED.data_as_of, "
                "quality_status = EXCLUDED.quality_status, "
                "quality_rule = EXCLUDED.quality_rule",
                (source, entity, shop_id, QUALITY_RULE))

    def _fact(self, shop_id: str, *, erp_id: str, commercial: str, day: int,
              amount: str, active: bool = True) -> None:
        paid_at = datetime(2026, 9, day, 12, tzinfo=BEIJING)
        self.conn.execute(
            "INSERT INTO bi.orders(shop_id, erp_id, commercial_ids, source, "
            "source_updated_at, paid_at, raw_pay_amount, active, batch_id) "
            "VALUES (%s, %s, %s, 'erp.trade.outstock.simple.query', now(), %s, "
            "%s, %s, 's3-seed') ON CONFLICT (shop_id, erp_id) DO NOTHING",
            (shop_id, erp_id, [commercial], paid_at, Decimal(amount), active))
        self.conn.execute(
            "INSERT INTO bi.order_payments(shop_id, commercial_id, paid_at, amount, "
            "currency, basis, verified) VALUES "
            "(%s, %s, %s, %s, 'CNY', 'head', true) "
            "ON CONFLICT (shop_id, commercial_id) DO NOTHING",
            (shop_id, commercial, paid_at, Decimal(amount)))

    def _refund(self, shop_id: str, *, commercial: str, day: int) -> None:
        self.conn.execute(
            "INSERT INTO bi.aftersales(shop_id, aftersale_id, commercial_id, "
            "raw_platform_amount, platform_completed_at, source_updated_at, "
            "platform_success, refund_canonical, matched, batch_id) VALUES "
            "(%s, %s, %s, %s, %s, now(), true, true, true, 's3-seed')",
            (shop_id, f"A-{commercial}", commercial, "70",
             datetime(2026, 9, day, 18, tzinfo=BEIJING)))

    def _seed_tb(self) -> None:
        """TB1：活动单100；关闭后退款单70；关闭未退款单30。"""
        self._shop("TB1", "tb")
        self._fact("TB1", erp_id="E-A", commercial="C-A", day=2, amount="100")
        self._fact("TB1", erp_id="E-R", commercial="C-R", day=2, amount="70",
                   active=False)
        self._refund("TB1", commercial="C-R", day=3)
        self._fact("TB1", erp_id="E-U", commercial="C-U", day=3, amount="30",
                   active=False)

    def _query(self, metrics, shop_ids=("TB1",), **overrides):
        import time as time_module

        from bi_agent.metrics import QueryRequest, query_business

        # 种子以管理员身份写入；金额查询按真实查询身份（只读 reporting）执行。
        self.conn.execute("RESET ROLE")
        self.conn.execute("SET LOCAL ROLE bi_reader")
        defaults = dict(start=self.WINDOW[0], end=self.WINDOW[1],
                        shop_ids=list(shop_ids), metrics=list(metrics))
        defaults.update(overrides)
        return query_business(self.conn, QueryRequest(**defaults),
                              allowed_shop_ids=frozenset({"TB1", "FX1"}),
                              now=FROZEN_NOW, deadline=time_module.monotonic() + 30)

    def _codes(self, result) -> list[str]:
        from bi_agent.business_query.nodes import _limitation_codes

        return _limitation_codes(result.limitations)

    def test_closed_then_refunded_payment_leaves_the_payment_caliber(self):
        self._seed_tb()
        result = self._query(["paid_amount", "paid_orders", "refund_amount",
                              "cash_difference"])
        self.assertEqual(result.status, "ok", result.limitations)
        row = result.data[0]
        self.assertEqual(Decimal(row["paid_amount"]), Decimal("130"),
                         "200 − 70：关闭后退款单的支付退出口径，关闭未退款的30留在口径内")
        self.assertEqual(row["paid_orders"], 2)
        self.assertEqual(Decimal(row["refund_amount"]), Decimal("70"),
                         "退款仍单独列示：口径剔除不吞掉退款发生额")
        self.assertEqual(Decimal(row["cash_difference"]), Decimal("60"),
                         "现金差额 = 剔除后支付130 − 退款70")
        # 旧数字对比回归：差值必须逐项可解释——旧口径 200 与新口径 130 的差
        # 恰等于被剔除的那一笔（诊断给出同额同笔数）。
        self.assertEqual(result.diagnostics.get("closed_refunded_excluded"),
                         {"excluded_count": 1, "excluded_amount": "70",
                          "currency": "CNY"})
        self.assertTrue(any("支付口径已剔除关闭后退款单" in text
                            for text in result.limitations), result.limitations)
        self.assertIn("closed_refunded_excluded", self._codes(result))

    def test_period_difference_is_labeled_not_net_payment_or_profit(self):
        self._seed_tb()
        result = self._query(["cash_difference"])
        self.assertEqual(result.status, "ok", result.limitations)
        self.assertIn("不是净利润", result.metric_definition["cash_difference"])
        self.assertIn("净支付", result.metric_definition["cash_difference"],
                      "指标定义必须点名它不是净支付")

    def test_day_grouping_applies_the_exclusion_per_payment_day(self):
        self._seed_tb()
        result = self._query(["paid_amount", "cash_difference"], group_by="day")
        self.assertEqual(result.status, "ok", result.limitations)
        rows = {(row["shop_id"], row["day"]): row for row in result.data}
        self.assertEqual(Decimal(rows[("TB1", "2026-09-02")]["paid_amount"]),
                         Decimal("100"), "09-02 剔除 C-R 的 70")
        self.assertEqual(Decimal(rows[("TB1", "2026-09-03")]["paid_amount"]),
                         Decimal("30"))
        self.assertEqual(Decimal(rows[("TB1", "2026-09-02")]["cash_difference"]),
                         Decimal("100"))
        self.assertEqual(Decimal(rows[("TB1", "2026-09-03")]["cash_difference"]),
                         Decimal("-40"), "当日支付30 − 当日完成退款70")

    def test_aov_follows_the_excluded_caliber(self):
        self._seed_tb()
        result = self._query(["aov"])
        self.assertEqual(result.status, "ok", result.limitations)
        self.assertEqual(Decimal(result.data[0]["aov"]), Decimal("65"),
                         "130 / 2 单：客单价用剔除后的口径")

    def test_cohort_refund_rate_keeps_the_full_cohort(self):
        """同批退款率不是支付口径金额：同批分母不减关闭后退款单，退款保持可见。"""
        self._seed_tb()
        result = self._query(["cohort_refund_rate"])
        self.assertEqual(result.status, "ok", result.limitations)
        self.assertAlmostEqual(float(result.data[0]["cohort_refund_rate"]), 0.35,
                               places=6,
                               msg="70 / 200：同批率仍以全部同批支付为分母")

    def test_fxg_caliber_keeps_closed_refunded_payments(self):
        """fxg 的支付口径语义不变：关闭后退款单的支付留在 certified 口径里。"""
        self._shop("FX1", "fxg")
        self._fact("FX1", erp_id="E-X", commercial="C-X", day=2, amount="90")
        self._fact("FX1", erp_id="E-Y", commercial="C-Y", day=2, amount="60",
                   active=False)
        self._refund("FX1", commercial="C-Y", day=3)
        result = self._query(["paid_amount"], shop_ids=("FX1",))
        self.assertEqual(result.status, "ok", result.limitations)
        self.assertEqual(Decimal(result.data[0]["paid_amount"]), Decimal("150"),
                         "fxg 不做 S3 剔除：两笔支付都在口径里")
        self.assertNotIn("closed_refunded_excluded", result.diagnostics)

    def test_local_filter_answers_with_disclosure_but_never_claims_certified(self):
        self._seed_tb()
        result = self._query(["paid_amount"])
        self.assertEqual(result.status, "ok", result.limitations)
        self.assertIn("coverage_time_basis_unverified", self._codes(result),
                      "可答+披露：不宣称完整支付窗口，也不假装已认证")
        self.assertTrue(any("按可观测样本披露" in text
                            for text in result.limitations), result.limitations)

    def test_local_filter_without_coverage_still_fails_closed(self):
        self._seed_tb()
        self.conn.execute("RESET ROLE")
        self.conn.execute(
            "UPDATE bi.sync_state SET covered = tstzmultirange() "
            "WHERE shop_id='TB1'")
        result = self._query(["paid_amount"])
        self.assertEqual(result.status, "missing_data", result.limitations)
        self.assertEqual(result.data, [])
        self.assertIn("coverage_incomplete", self._codes(result),
                      "没有真实 local_filter 覆盖就在覆盖门禁处拒答，不冒充零")

    def test_previous_period_comparison_applies_the_same_exclusion(self):
        """P1 回归钉：上期行与本期同用 S3 剔除口径，change 不得把口径漂移当增长。

        本期（09-08~09-15）只有一笔活动支付 50；上期（09-01~09-08）被剔 70。
        若上期行不剔除，paid_amount_previous 会是 200，差额 −150 会把口径切换
        伪造成业务下滑；修复后上期 = 130，差额 −80 逐项可解释。
        """
        self._seed_tb()
        self._fact("TB1", erp_id="E-N", commercial="C-N", day=9, amount="50")
        self.conn.execute("RESET ROLE")
        self.conn.execute(
            "UPDATE bi.sync_state SET covered = "
            "tstzmultirange(tstzrange('2026-09-01','2026-09-15','[)')) "
            "WHERE shop_id='TB1' AND entity='orders'")
        result = self._query(["paid_amount"], start="2026-09-08", end="2026-09-15",
                             compare="previous_period")
        self.assertEqual(result.status, "ok", result.limitations)
        row = result.data[0]
        self.assertEqual(Decimal(row["paid_amount"]), Decimal("50"))
        self.assertEqual(Decimal(row["paid_amount_previous"]), Decimal("130"),
                         "上期同口径：200 − 70；不得拿未剔除的 200 冒充上期")
        self.assertEqual(Decimal(row["paid_amount_change"]), Decimal("-80"),
                         "差额 = 50 − 130：剔除量不再被伪造成下滑")
        self.assertIsNotNone(row["paid_amount_change_ratio"])
        self.assertTrue(any("上期支付口径已剔除关闭后退款单" in text
                            and "1笔/70元" in text for text in result.limitations),
                        result.limitations)
        self.assertIn("closed_refunded_excluded", self._codes(result))
        # 本期窗口没有剔除事实：诊断只描述本期，不为上期凭空出现。
        self.assertNotIn("closed_refunded_excluded", result.diagnostics)

    def test_partial_local_filter_coverage_refuses_the_whole_window(self):
        self._seed_tb()
        self.conn.execute("RESET ROLE")
        self.conn.execute(
            "UPDATE bi.sync_state SET covered = "
            "tstzmultirange(tstzrange('2026-09-01','2026-09-04','[)')) "
            "WHERE shop_id='TB1' AND entity='orders'")
        result = self._query(["paid_amount"])
        self.assertEqual(result.status, "missing_data", result.limitations)
        self.assertEqual(result.coverage.gaps, ["2026-09-04~2026-09-08"])


if __name__ == "__main__":
    unittest.main()
