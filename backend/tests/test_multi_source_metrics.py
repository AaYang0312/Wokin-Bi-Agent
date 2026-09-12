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
from decimal import Decimal

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

        self.assertEqual(set(METRIC_CAPABILITIES), set(METRIC_DEFINITIONS))

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

    def test_disproved_pay_time_channel_refuses_a_payment_window_result(self):
        """出库接口实测按自身时间裁剪（83/8367 行越界）：它给不出完整支付窗口。

        数据齐、覆盖全也不能出数——把可观测样本当完整窗口，就是拿偏小的数字冒充总额。
        """
        self._grant("{paid_amount}", platform="tb")
        result = self._query(["paid_amount"])
        self.assertEqual(result.status, "missing_data", result.limitations)
        self.assertEqual(result.data, [])
        self.assertIn("coverage_time_basis_unverified", self._codes(result))
        self.assertEqual(sorted(result.filters["shop_ids"]), ["S1"], "原请求不得被改写")

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


if __name__ == "__main__":
    unittest.main()
