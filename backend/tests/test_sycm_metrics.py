"""生意参谋手工指标与查询层（计划 §3.M3，红先行）。

口径依据：docs/superpowers/plans/2026-10-02-manual-sycm-export-import.md
§2 设计决策 D1–D3、§3.M3、§4 工程约束。

锁七件事：
1) 来源注册：manual-export 来源族按 runbook 契约登记进 sources 注册表；版本
   决策钉死——SOURCE_REGISTRY_VERSION 不 bump（新增词表不能作废既有请求指纹），
   口径版本由 basis 名自身的 `manual.sycm.export/v1` 段携带；
2) 指标定义与逐店绑定：6 个新指标经 bi.shops.capabilities（M2 授予）解析到
   (manual.sycm.export, shop_daily) 绑定；未授予报能力缺口、pdd 永不解析；
3) 同源比值铁律（结构化测试）：sycm 查询路径的 SQL 只引用 sycm 视图列，
   ERP 的 SQL 与 paid_amount 永不出现在 sycm 路径；比值只由同源合计计算；
4) 覆盖缺口：窗口不足走既有 missing_data + coverage.gaps，不补零；
   suggested_window 与已覆盖段一致；
5) remediation（D3）：公开 Artifact 附 upload 结构，键与形状在 runtime 白名单
   校验；模型可见载荷不含 remediation；公开文案为服务端固定模板；
6) promotion actual_budget 两态：有 ad_spend 覆盖的店可答（实耗=手工导入，带
   披露），无覆盖店铺行为不变（missing_data）；
7) 词表一致性（S3 教训回环）：新披露文本→码→公开校验逐层可回环。

纯结构测试任何环境都跑；查询/写入面测试 skipUnless BI_TEST_ADMIN_DSN，
全部在外层回滚事务内进行，共享测试库零残留。
"""

from __future__ import annotations

import os
import re
import unittest
from datetime import date, datetime, time
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from bi_agent import metrics as metrics_module
from bi_agent import promotion as promotion_module
from bi_agent.metrics import METRIC_DEFINITIONS, QueryRequest
from bi_agent.sources import (
    ENTITY_REQUIREMENTS,
    MANUAL_EXPORT_BASIS,
    MANUAL_EXPORT_ENTITY,
    MANUAL_EXPORT_SOURCE,
    MANUAL_METRIC_CAPABILITIES,
    METRIC_CAPABILITIES,
    METRIC_VERSION,
    SOURCE_REGISTRY_VERSION,
    ShopRecord,
    resolve_metric_dependencies,
    resolve_metric_sources,
    unsupported_reason,
)

BEIJING = ZoneInfo("Asia/Shanghai")

MANUAL_METRICS = ("ad_spend", "ad_spend_ratio", "roas", "visitors",
                  "pay_conversion_rate", "cart_count")
RATIO_METRICS = ("roas", "ad_spend_ratio")


def _shop(shop_id: str = "SY-A", platform: str = "tb",
          *capabilities: str) -> ShopRecord:
    return ShopRecord(shop_id=shop_id, platform=platform,
                      capabilities=frozenset(capabilities))


# ---------------------------------------------------------------------------
# 1) 来源注册与版本决策（纯结构，不碰数据库）
# ---------------------------------------------------------------------------


class ManualSourceRegistryTests(unittest.TestCase):
    """manual-export 族是 sources 注册表的一等来源族；pdd 永不解析。"""

    def test_source_family_constants_align_with_m2_import(self):
        from bi_agent import sycm_import

        self.assertEqual(MANUAL_EXPORT_SOURCE, "manual.sycm.export")
        self.assertEqual(sycm_import.SYCM_MANUAL_SOURCE, MANUAL_EXPORT_SOURCE,
                         "M2 公开名必须继续指向注册表真源")
        self.assertEqual(MANUAL_EXPORT_ENTITY, "shop_daily")
        self.assertEqual(sycm_import.SHOP_DAILY_ENTITY, MANUAL_EXPORT_ENTITY)
        self.assertEqual(set(MANUAL_METRIC_CAPABILITIES), set(MANUAL_METRICS))
        self.assertEqual(set(sycm_import.SYCM_MANUAL_CAPABILITIES),
                         set(MANUAL_METRICS),
                         "M2 授予的能力词与 M3 指标族一一对应")

    def test_registry_version_is_not_bumped_by_adding_the_manual_family(self):
        # 版本决策：新增来源族不 bump SOURCE_REGISTRY_VERSION / METRIC_VERSION。
        # 既有 ERP 请求的绑定、口径与指纹一个都没变；bump 会无谓作废全部既有
        # 请求指纹。手工口径自身的版本由 basis 名携带（下一断言）。
        self.assertEqual(SOURCE_REGISTRY_VERSION, "sources/2026-09-12.1")
        self.assertEqual(METRIC_VERSION, "metrics/2026-09-25.1")

    def test_manual_basis_carries_its_own_version_segment(self):
        # 口径版本契约沿用既有形态：`名字/版本段`。换口径必须换 basis。
        # 口径标签不是来源名：它必须通过 runtime 载荷校验的口径名形状
        # （首段只允许 [a-z0-9_-]），带点的来源名不能直接当 basis。
        from bi_agent.runtime.models import _BASIS_NAME_RE

        self.assertEqual(MANUAL_EXPORT_BASIS, "manual_sycm_export/v1")
        self.assertIsNotNone(_BASIS_NAME_RE.fullmatch(MANUAL_EXPORT_BASIS),
                             "basis 必须能过公开载荷的口径名校验")

    def test_entity_requirements_bind_the_shop_daily_entity(self):
        for metric in MANUAL_METRICS:
            with self.subTest(metric=metric):
                self.assertEqual(ENTITY_REQUIREMENTS[metric],
                                 (MANUAL_EXPORT_ENTITY,))

    def test_granted_shop_binds_the_manual_source_with_stat_date_attribution(self):
        for metric in MANUAL_METRICS:
            with self.subTest(metric=metric):
                bindings = resolve_metric_sources(
                    _shop("SY-A", "tb", *MANUAL_METRICS), metric)
                self.assertEqual(len(bindings), 1)
                binding = bindings[0]
                self.assertEqual(binding.source, MANUAL_EXPORT_SOURCE)
                self.assertEqual(binding.entity, MANUAL_EXPORT_ENTITY)
                self.assertEqual(binding.basis, MANUAL_EXPORT_BASIS)
                self.assertEqual(binding.time_basis, "stat_date",
                                 "手工导出按统计日期归属")
                self.assertFalse(binding.coverage_certified)

    def test_dependencies_resolve_without_the_capability_tag(self):
        # resolve_metric_dependencies 只答“要读哪些来源”，不看能力标签；
        # 覆盖判定靠它，能力门禁靠 unsupported_reason，两者不同因。
        bindings = resolve_metric_dependencies(_shop("SY-A", "tb"), "ad_spend")
        self.assertEqual([item.source for item in bindings],
                         [MANUAL_EXPORT_SOURCE])

    def test_ungranted_shop_reports_a_capability_gap_not_coverage(self):
        for metric in MANUAL_METRICS:
            with self.subTest(metric=metric):
                self.assertEqual(unsupported_reason(_shop("SY-A", "tb"), metric),
                                 "capability_ungranted")

    def test_pdd_never_resolves_the_manual_family(self):
        # 三道守卫的第三道：即便登记面与授予面双双失守，注册表也不给 pdd 解析。
        shop = _shop("P1", "pdd", *MANUAL_METRICS)
        for metric in MANUAL_METRICS:
            with self.subTest(metric=metric):
                self.assertEqual(resolve_metric_sources(shop, metric), ())
                self.assertEqual(unsupported_reason(shop, metric),
                                 "capability_unavailable")

    def test_unregistered_platform_with_manual_grant_still_resolves(self):
        # 手工导出不依赖 ERP 平台注册：导入面按 display_name 映射并授予能力，
        # 绑定只认能力与覆盖，未登记 ERP 平台不是拒绝手工指标的理由。
        shop = _shop("SY-U", "alibabac2m", "ad_spend")
        bindings = resolve_metric_sources(shop, "ad_spend")
        self.assertEqual([item.source for item in bindings],
                         [MANUAL_EXPORT_SOURCE])
        self.assertIsNone(unsupported_reason(shop, "ad_spend"))

    def test_erp_reconcile_evidence_never_grants_the_manual_family(self):
        # 能力只能由逐源对账证据推导（ERP 面）——对账证据开不了手工族，
        # 手工族只能由导入面授予（sources.capabilities_from_evidence 只扫 ceiling）。
        from bi_agent.sources import capabilities_from_evidence

        evidence = {("erp.trade.outstock.simple.query", "orders"): "passed"}
        self.assertEqual(capabilities_from_evidence("tb", evidence)
                         & set(MANUAL_METRICS), set())


class ManualMetricDefinitionTests(unittest.TestCase):
    """指标定义、请求词表与工具 schema 的登记。"""

    def test_query_request_accepts_each_manual_metric(self):
        for metric in MANUAL_METRICS:
            with self.subTest(metric=metric):
                request = QueryRequest(start=date(2026, 9, 1), end=date(2026, 9, 8),
                                       shop_ids=["ent-0a1b2c3d"], metrics=[metric])
                self.assertEqual(request.metrics, [metric])

    def test_product_grouping_rejects_manual_metrics(self):
        with self.assertRaises(ValueError):
            QueryRequest(start=date(2026, 9, 1), end=date(2026, 9, 8),
                         shop_ids=["ent-0a1b2c3d"], metrics=["ad_spend"],
                         group_by="product")

    def test_definitions_state_manual_provenance_and_stat_date_attribution(self):
        for metric in MANUAL_METRICS:
            with self.subTest(metric=metric):
                definition = METRIC_DEFINITIONS[metric]
                self.assertIn("手工导入", definition)
                self.assertIn("统计日期", definition)
        for metric in RATIO_METRICS:
            with self.subTest(metric=metric):
                # D2：同源比值必须在口径声明里点名分子分母同源、与 ERP 支付额无关。
                self.assertIn("同源", METRIC_DEFINITIONS[metric])
                self.assertIn("生意参谋支付金额", METRIC_DEFINITIONS[metric])
                self.assertIn("推广花费", METRIC_DEFINITIONS[metric])

    def test_tool_schema_statically_lists_the_new_metrics(self):
        # 已批准偏差（主会话决定）：工具 schema 的指标词表静态列出这 6 个名字。
        # “逐字一致”只对回答行为成立：gate 关闭时无法导入数据，任何相关提问
        # 都会走能力/覆盖 fail-closed 拒答。本断言把偏差本身钉进测试。
        schema = QueryRequest.model_json_schema()
        offered = set(schema["properties"]["metrics"]["items"]["enum"])
        self.assertTrue(set(MANUAL_METRICS) <= offered)

    def test_capability_vocabulary_covers_every_published_metric(self):
        # ERP 族 + 手工族 = 全部已发布指标：同一份 METRIC_DEFINITIONS 不能有
        # 任何一个指标不在两个能力词表之并集里（自造标签没有落点）。
        self.assertEqual(set(METRIC_CAPABILITIES) | set(MANUAL_METRIC_CAPABILITIES),
                         set(METRIC_DEFINITIONS))


# ---------------------------------------------------------------------------
# 2) 同源比值铁律（结构化测试，D2）
# ---------------------------------------------------------------------------


class CaliberIsolationTests(unittest.TestCase):
    """sycm 查询路径只读 sycm 视图；ERP 路径永不引用 sycm 列。"""

    _WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

    def _sql_constants(self):
        # 扫描面 = metrics + promotion（M3 遗留收口：ACTUAL_SPEND_SQL 曾在
        # 扫描面外，promotion 实耗路径同样不得混口径）。
        constants: dict[str, str] = {}
        for module in (metrics_module, promotion_module):
            constants.update({
                name: getattr(module, name) for name in dir(module)
                if name.isupper() and name.endswith("_SQL")
                and isinstance(getattr(module, name), str)})
        return constants

    def _words(self, sql: str) -> set[str]:
        return set(self._WORD.findall(sql))

    def test_sycm_sql_reads_only_the_sycm_view_and_sycm_columns(self):
        sycm_sqls = {name: sql for name, sql in self._sql_constants().items()
                     if "v_sycm_shop_daily" in sql}
        self.assertTrue(sycm_sqls, "sycm 查询路径必须有固定 SQL")
        # 扫描面必须覆盖 promotion 的实耗读取（M3 遗留：它曾漏在扫描外）。
        self.assertIn("ACTUAL_SPEND_SQL", sycm_sqls)
        for name, sql in sycm_sqls.items():
            with self.subTest(sql=name):
                self.assertNotIn("paid_amount",
                                 self._words(sql) - {"paid_amount_sycm"},
                                 "sycm 路径不得引用 ERP paid_amount 列")
                self.assertNotIn("v_shop_daily", self._words(sql),
                                 "sycm 路径不得读 ERP 日报视图")

    def test_erp_sql_never_references_sycm_columns_or_table(self):
        erp_sqls = {name: sql for name, sql in self._sql_constants().items()
                    if "v_sycm_shop_daily" not in sql}
        self.assertTrue(erp_sqls)
        for name, sql in erp_sqls.items():
            with self.subTest(sql=name):
                words = self._words(sql)
                self.assertNotIn("paid_amount_sycm", words)
                self.assertNotIn("sycm_shop_daily", words)

    def test_ratio_helpers_compute_from_sycm_totals_only(self):
        # 比值只在两个同源合计之间计算；分母为 0（零花费/零支付）不可计算，
        # 返回 None 绝不补 0。
        self.assertEqual(metrics_module.compute_roas(Decimal("270"), Decimal("90")),
                         Decimal("3"))
        self.assertIsNone(metrics_module.compute_roas(Decimal("270"), Decimal("0")))
        self.assertEqual(
            metrics_module.compute_ad_spend_ratio(Decimal("90"), Decimal("270")),
            Decimal("90") / Decimal("270"))
        self.assertIsNone(
            metrics_module.compute_ad_spend_ratio(Decimal("90"), Decimal("0")))

    def test_conversion_helper_never_divides_by_zero(self):
        self.assertEqual(metrics_module.compute_pay_conversion_rate(18, 900),
                         Decimal("18") / Decimal("900"))
        self.assertIsNone(metrics_module.compute_pay_conversion_rate(0, 0))

    def test_basis_of_manual_metrics_names_only_the_manual_source(self):
        records = [_shop("SY-A", "tb", *MANUAL_METRICS)]
        bindings_by_shop_metric = {
            (record.shop_id, metric): resolve_metric_sources(record, metric)
            for record in records for metric in MANUAL_METRICS}
        entries = metrics_module._basis_evidence(records, list(MANUAL_METRICS),
                                                 bindings_by_shop_metric)
        self.assertEqual(len(entries), len(MANUAL_METRICS))
        for entry in entries:
            with self.subTest(metric=entry["metric"]):
                self.assertEqual(entry["source"], MANUAL_EXPORT_SOURCE)
                self.assertEqual(entry["basis"], MANUAL_EXPORT_BASIS)
                self.assertEqual(entry["time_basis"], "stat_date")

    def test_shop_daily_entity_has_no_second_writer_source(self):
        # shop_daily 实体的覆盖只能由 manual.sycm.export 写：注册表里没有任何
        # ERP 通道把实体解析到 shop_daily，换来源比较的检查因此依然成立。
        for platform in ("fxg", "tb", "tm", "jd", "kuaishou", "wxsph", "wsxc",
                         "pdd"):
            record = _shop("X", platform, *METRIC_CAPABILITIES)
            for metric in sorted(METRIC_CAPABILITIES):
                bindings = resolve_metric_dependencies(record, metric)
                self.assertNotIn(MANUAL_EXPORT_ENTITY,
                                 {item.entity for item in bindings})


# ---------------------------------------------------------------------------
# 3) 词表一致性（S3 教训：新文本→码→公开校验逐层回环）
# ---------------------------------------------------------------------------


class SycmVocabularyConsistencyTests(unittest.TestCase):
    """新披露文本、新限制码与 remediation 载荷的登记回环。"""

    def _payload(self, body):
        payload = {"status": "ok", "metric_definition": {}, "filters": {},
                   "data": [], "limitations": []}
        payload.update(body)
        return {key: value for key, value in payload.items() if value is not None}

    def test_fixed_texts_round_trip_through_codes_and_public_check(self):
        from bi_agent.business_query.nodes import _limitation_codes
        from bi_agent.runtime import models

        corpus = (
            metrics_module.SYCM_MIXED_CALIBER_LIMITATION,
            metrics_module.SYCM_UPLOAD_LIMITATION,
            metrics_module.SYCM_RATIO_NOT_COMPUTABLE_LIMITATION,
        )
        for text in corpus:
            with self.subTest(text=text[:18]):
                codes = _limitation_codes([text])
                self.assertTrue(codes, "固定文本必须能归因到至少一个码")
                self.assertLessEqual(set(codes), models._LIMITATION_CODES)
                models._public_limitation(text)

    def test_parameterized_texts_round_trip_through_codes_and_public_check(self):
        from bi_agent.business_query.nodes import _limitation_codes
        from bi_agent.runtime import models

        # business_query 面发射的参数化文本：文本→码→公开校验逐层回环。
        corpus = (
            "1 家店铺缺少 ad_spend 的手工导入数据，未执行查询",
            "2 家店铺缺少 roas、visitors 的手工导入数据，未执行查询",
            "窗口内 visitors 缺 1 天，拒绝部分汇总",
            "窗口内 visitors 缺 2 天、paid_buyers 缺 1 天，拒绝部分汇总",
        )
        for text in corpus:
            with self.subTest(text=text[:18]):
                codes = _limitation_codes([text])
                self.assertTrue(codes, "参数化文本必须能归因到至少一个码")
                self.assertLessEqual(set(codes), models._LIMITATION_CODES)
                models._public_limitation(text)

    def test_promotion_excluded_shops_text_passes_the_public_check(self):
        # promotion 面的排除计数披露不经过 business_query 的文本→码映射
        # （推广结果不走 classify），只要求能过公开载荷校验。
        from bi_agent.promotion import MANUAL_SPEND_EXCLUDED_PATTERN
        from bi_agent.runtime import models

        text = "1 家店铺缺少该窗口的手工实耗数据"
        self.assertIsNotNone(MANUAL_SPEND_EXCLUDED_PATTERN.fullmatch(text))
        models._public_limitation(text)

    def test_ratio_code_is_registered(self):
        from bi_agent.runtime import models

        self.assertIn("ratio_not_computable", models._LIMITATION_CODES)

    def test_manual_result_columns_are_registered_with_numeric_rules(self):
        from bi_agent.runtime.models import (
            ARTIFACT_RESULT_COLUMNS,
            validate_artifact_payload,
        )

        self.assertTrue(set(MANUAL_METRICS) <= ARTIFACT_RESULT_COLUMNS)
        payload = self._payload({
            "status": "ok",
            "data": [{"shop_ref": "ent-0a1b2c3d", "ad_spend": "90.00",
                      "visitors": 900, "cart_count": 45,
                      "pay_conversion_rate": "0.02", "roas": "3.00"}]})
        validated = validate_artifact_payload(payload)
        self.assertEqual(validated["status"], "ok")

    def _remediation_block(self, **overrides):
        block = {"kind": "upload", "file_kind": "店铺维度",
                 "shop_refs": ["ent-0a1b2c3d"],
                 "covered_window": ["2026-09-01", "2026-09-10"],
                 "missing_range": ["2026-09-10", "2026-09-20"]}
        block.update(overrides)
        return {key: value for key, value in block.items() if value is not None}

    def _artifact_with_remediation(self, remediation):
        return self._payload({"status": "missing_data", "remediation": remediation})

    def test_remediation_block_is_a_registered_public_payload_key(self):
        from bi_agent.runtime.models import validate_artifact_payload

        payload = validate_artifact_payload(
            self._artifact_with_remediation(self._remediation_block()))
        self.assertEqual(payload["remediation"]["kind"], "upload")

    def test_remediation_shape_is_validated_field_by_field(self):
        from bi_agent.runtime.models import validate_artifact_payload

        bad_blocks = (
            ("unknown_key", self._remediation_block(extra="x")),
            ("bad_kind", self._remediation_block(kind="magic")),
            ("bad_file_kind", self._remediation_block(file_kind="商品维度")),
            ("bad_shop_ref", self._remediation_block(shop_refs=["shop-1"])),
            ("bad_missing_range",
             self._remediation_block(missing_range=["2026-09-10", "明天"])),
            ("bad_covered_window",
             self._remediation_block(covered_window=["2026-09-01"])),
            ("missing_missing_range", {
                "kind": "upload", "file_kind": "店铺维度",
                "shop_refs": ["ent-0a1b2c3d"]}),
        )
        for name, block in bad_blocks:
            with self.subTest(case=name):
                with self.assertRaises(ValueError):
                    validate_artifact_payload(
                        self._artifact_with_remediation(block))

    def test_model_payload_must_not_carry_remediation(self):
        # 防模型伪造上传提示：remediation 只进公开 Artifact，模型载荷带它即违约。
        from bi_agent.runtime.models import validate_model_payload

        with self.assertRaises(ValueError):
            validate_model_payload(
                self._artifact_with_remediation(self._remediation_block()))


# ---------------------------------------------------------------------------
# 4) 查询面（共享测试库，外层回滚事务）
# ---------------------------------------------------------------------------


def _day_start(day: date) -> datetime:
    return datetime.combine(day, time.min, tzinfo=BEIJING)


@unittest.skipUnless(os.getenv("BI_TEST_ADMIN_DSN"), "未配置独立测试数据库")
class SycmMetricQueryDatabaseTests(unittest.TestCase):
    """覆盖门禁、比值口径、披露与 remediation 走真实测试库与 reader 身份。"""

    SHOP_A = "SY-A"
    SHOP_B = "SY-B"

    def setUp(self):
        from .dbfixtures import connect_test_db

        self.conn = connect_test_db(self)
        migration = (Path(__file__).parents[1] / "sql"
                     / "030_sycm_shop_daily_reader_view.sql")
        self.conn.execute(migration.read_text(encoding="utf-8"))
        # 18 天（09-01..09-18，覆盖 [09-01,09-19)）：留出完整本期+上期窗口。
        self._shop(self.SHOP_A, "同步淘店A", days=18)
        self._shop(self.SHOP_B, "同步淘店B", days=4)

    # -- 夹具 ------------------------------------------------------------------

    def _shop(self, shop_id, name, *, days,
              capabilities=("paid_amount",)):
        words = sorted(set((*capabilities, *MANUAL_METRICS)))
        self.conn.execute(
            "INSERT INTO bi.shops(shop_id, platform, display_name, capabilities) "
            "VALUES (%s, 'tb', %s, %s) ON CONFLICT (shop_id) DO UPDATE SET "
            "capabilities = EXCLUDED.capabilities, display_name = EXCLUDED.display_name",
            (shop_id, name, words))
        for offset in range(days):
            day = date(2026, 9, 1 + offset)
            self.conn.execute(
                "INSERT INTO bi.sycm_shop_daily(shop_id, stat_date, visitors, "
                "paid_amount_sycm, paid_buyers, ad_spend_all, cart_count, batch_id) "
                "VALUES (%s, %s, 100, 30, 2, 10, 5, 'sycm-m3-seed') "
                "ON CONFLICT (shop_id, stat_date) DO UPDATE SET visitors = 100, "
                "paid_amount_sycm = 30, paid_buyers = 2, ad_spend_all = 10, "
                "cart_count = 5, batch_id = 'sycm-m3-seed'", (shop_id, day))
        self._coverage(shop_id, date(2026, 9, 1), date(2026, 9, 1 + days))

    def _coverage(self, shop_id, start, end_exclusive):
        self.conn.execute(
            "INSERT INTO bi.sync_state(source, entity, shop_id, watermark, covered, "
            "data_as_of, quality_status, quality_rule) VALUES "
            "(%s, %s, %s, %s, tstzmultirange(tstzrange(%s, %s, '[)')), %s, "
            "'unknown', 'manual-export/unverified') "
            "ON CONFLICT (source, entity, shop_id) DO UPDATE SET "
            "covered = EXCLUDED.covered, data_as_of = EXCLUDED.data_as_of",
            (MANUAL_EXPORT_SOURCE, MANUAL_EXPORT_ENTITY, shop_id,
             _day_start(end_exclusive), _day_start(start),
             _day_start(end_exclusive), _day_start(end_exclusive)))

    def _query(self, metrics_, shop_ids=(SHOP_A,), allowed=None, *,
               partial_answers_enabled=False, **overrides):
        import time as time_module

        defaults = dict(start="2026-09-01", end="2026-09-10",
                        shop_ids=list(shop_ids), metrics=list(metrics_))
        defaults.update(overrides)
        self.conn.execute("RESET ROLE")
        self.conn.execute("SET LOCAL ROLE bi_reader")
        return metrics_module.query_business(
            self.conn, QueryRequest(**defaults),
            allowed_shop_ids=frozenset(allowed) if allowed is not None
            else frozenset({self.SHOP_A, self.SHOP_B}),
            now=datetime(2026, 9, 10, 9, tzinfo=BEIJING),
            deadline=time_module.monotonic() + 30,
            partial_answers_enabled=partial_answers_enabled)

    def _codes(self, result) -> list[str]:
        from bi_agent.business_query.nodes import _limitation_codes

        return _limitation_codes(result.limitations)

    def _catalog(self, result):
        from bi_agent.catalog import build_catalog

        return build_catalog(self.conn, result,
                             allowed_shop_ids=frozenset({self.SHOP_A, self.SHOP_B}))

    # -- 正向：可答 + 披露 + basis ----------------------------------------------

    def test_manual_window_answers_with_values_disclosure_and_basis(self):
        result = self._query(["ad_spend", "visitors", "cart_count",
                              "pay_conversion_rate", "roas", "ad_spend_ratio"])
        self.assertEqual(result.status, "ok", result.limitations)
        row = result.data[0]
        self.assertEqual(Decimal(row["ad_spend"]), Decimal("90"))
        self.assertEqual(row["visitors"], 900)
        self.assertEqual(row["cart_count"], 45)
        self.assertEqual(Decimal(row["pay_conversion_rate"]),
                         Decimal("18") / Decimal("900"))
        self.assertEqual(Decimal(row["roas"]), Decimal("3"),
                         "ROAS = 生意参谋支付 270 ÷ 推广花费 90（同源）")
        self.assertEqual(Decimal(row["ad_spend_ratio"]),
                         Decimal("90") / Decimal("270"))
        self.assertEqual(result.coverage.status, "complete")
        self.assertEqual(list(result.coverage.suggested_window or ()),
                         ["2026-09-01", "2026-09-10"])
        self.assertIn("来源质量未核验（尚无对账记录）", result.limitations)
        self.assertIn("source_quality_unverified", self._codes(result))
        self.assertEqual({(entry["metric"], entry["source"], entry["basis"],
                           entry["time_basis"]) for entry in result.basis},
                         {(metric, MANUAL_EXPORT_SOURCE, MANUAL_EXPORT_BASIS,
                           "stat_date") for metric in MANUAL_METRICS})

    def test_roas_stays_the_sycm_ratio_when_erp_payment_exists(self):
        # 口径隔离的动态证据：同店同窗口有 ERP 支付（paid_amount=100）时，
        # ROAS 仍只用生意参谋两侧合计，ERP 金额不进分子分母。
        from bi_agent.sync import apply_trade, normalise_trade

        self.conn.execute("RESET ROLE")
        trade = normalise_trade({
            "sid": "E-SY-A", "userId": self.SHOP_A, "tid": "C-SY-A",
            "payAmount": "100.00",
            "payTime": int(datetime(2026, 9, 2, 10, tzinfo=BEIJING).timestamp() * 1000),
            "updTime": int(datetime(2026, 9, 2, 11, tzinfo=BEIJING).timestamp() * 1000),
            "status": "TRADE_NO_CREATED_EVALUATION", "sysStatus": 0,
            "orders": [{"skuId": "S1", "num": "1", "payAmount": "100.00",
                        "itemSysId": "P_A", "oid": "E-SY-A-1"}],
        }, source="erp.trade.outstock.simple.query")
        self.assertTrue(apply_trade(self.conn, trade, batch_id="sycm-m3-erp"))
        self.conn.execute(
            "INSERT INTO bi.sync_state(source, entity, shop_id, watermark, covered, "
            "data_as_of, quality_status, quality_rule) VALUES "
            "('erp.trade.outstock.simple.query', 'orders', %s, "
            "'2026-09-10 00:00+08', tstzmultirange(tstzrange('2026-09-01',"
            "'2026-09-10','[)')), '2026-09-10 00:00+08', 'passed', 'test-seed') "
            "ON CONFLICT (source, entity, shop_id) DO UPDATE SET "
            "covered = EXCLUDED.covered, data_as_of = EXCLUDED.data_as_of",
            (self.SHOP_A,))

        roas = self._query(["roas"])
        self.assertEqual(roas.status, "ok", roas.limitations)
        self.assertEqual(Decimal(roas.data[0]["roas"]), Decimal("3"))
        erp = self._query(["paid_amount"])
        self.assertEqual(erp.status, "ok", erp.limitations)
        self.assertEqual(Decimal(erp.data[0]["paid_amount"]), Decimal("100"),
                         "ERP 支付口径走自己的来源与数值")

    # -- 覆盖缺口与 remediation -------------------------------------------------

    def test_short_window_refuses_with_gaps_suggested_window_and_remediation(self):
        from bi_agent.business_query.tool import to_model_result, to_public_artifact

        # SHOP_B 只覆盖 [09-01,09-05)：请求 [09-01,09-20) 缺 [09-05,09-20)。
        result = self._query(["ad_spend"], shop_ids=(self.SHOP_B,),
                             start="2026-09-01", end="2026-09-20")
        self.assertEqual(result.status, "missing_data", result.limitations)
        self.assertEqual(result.data, [])
        self.assertEqual(result.coverage.gaps, ["2026-09-05~2026-09-20"])
        self.assertEqual(list(result.coverage.suggested_window or ()),
                         ["2026-09-01", "2026-09-05"])
        self.assertEqual(result.remediation, {
            "kind": "upload", "file_kind": "店铺维度",
            "shop_ids": [self.SHOP_B],
            "covered_window": ["2026-09-01", "2026-09-05"],
            "missing_range": ["2026-09-05", "2026-09-20"]})
        self.assertIn(metrics_module.SYCM_UPLOAD_LIMITATION, result.limitations)
        self.assertIn("coverage_incomplete", self._codes(result))

        catalog = self._catalog(result)
        public_payload = to_public_artifact(result, catalog)
        self.assertEqual(public_payload["remediation"], {
            "kind": "upload", "file_kind": "店铺维度",
            "shop_refs": [catalog.shop_ref(self.SHOP_B)],
            "covered_window": ["2026-09-01", "2026-09-05"],
            "missing_range": ["2026-09-05", "2026-09-20"]})
        model_payload = to_model_result(result, catalog)
        self.assertNotIn("remediation", model_payload)
        self.assertIsNone(result.partial_answer)
        self.assertNotIn("partial_answer", model_payload)

    def test_partial_gate_answers_one_covered_span_and_keeps_the_request_window(self):
        """闸开时，单段已覆盖窗口出数；原请求窗口留在 coverage，缺口仍可上传。"""
        from bi_agent.business_query.tool import to_model_result, to_public_artifact

        closed = self._query(["ad_spend"], shop_ids=(self.SHOP_B,),
                             start="2026-09-01", end="2026-09-20",
                             partial_answers_enabled=False)
        self.assertEqual(closed.status, "missing_data", closed.limitations)
        self.assertEqual(closed.data, [])
        self.assertIsNone(closed.partial_answer)

        result = self._query(["ad_spend"], shop_ids=(self.SHOP_B,),
                             start="2026-09-01", end="2026-09-20",
                             partial_answers_enabled=True)
        self.assertEqual(result.status, "ok", result.limitations)
        self.assertEqual(Decimal(result.data[0]["ad_spend"]), Decimal("40"))
        self.assertEqual(result.coverage.status, "partial")
        self.assertEqual(result.coverage.start, date(2026, 9, 1))
        self.assertEqual(result.coverage.end, date(2026, 9, 20))
        self.assertEqual(result.coverage.gaps, ["2026-09-05~2026-09-20"])
        self.assertEqual(list(result.coverage.suggested_window or ()),
                         ["2026-09-01", "2026-09-05"])
        self.assertEqual(result.filters["start"], "2026-09-01")
        self.assertEqual(result.filters["end"], "2026-09-20")
        self.assertEqual(result.partial_answer, {
            "answered_windows": [["2026-09-01", "2026-09-05"]],
            "missing_windows": [["2026-09-05", "2026-09-20"]],
            "metric_refs": ["ad_spend"],
            "disclosure": "仅覆盖 2026-09-01~2026-09-05，2026-09-05 起缺口",
        })
        self.assertNotIn("拒绝部分汇总", "".join(result.limitations))
        self.assertIn(metrics_module.SYCM_UPLOAD_LIMITATION, result.limitations)
        self.assertIn(metrics_module.SOURCE_QUALITY_UNVERIFIED_LIMITATION,
                      result.limitations)
        self.assertEqual(result.remediation, {
            "kind": "upload", "file_kind": "店铺维度",
            "shop_ids": [self.SHOP_B],
            "covered_window": ["2026-09-01", "2026-09-05"],
            "missing_range": ["2026-09-05", "2026-09-20"]})

        catalog = self._catalog(result)
        public_payload = to_public_artifact(result, catalog)
        model_payload = to_model_result(result, catalog)
        self.assertEqual(public_payload["partial_answer"], result.partial_answer)
        self.assertEqual(model_payload["partial_answer"], result.partial_answer)
        self.assertIn("remediation", public_payload)
        self.assertNotIn("remediation", model_payload)

    def test_partial_gate_answers_a_same_source_ratio_on_the_covered_span(self):
        result = self._query(["roas"], shop_ids=(self.SHOP_B,),
                             start="2026-09-01", end="2026-09-20",
                             partial_answers_enabled=True)
        self.assertEqual(result.status, "ok", result.limitations)
        self.assertEqual(Decimal(result.data[0]["roas"]), Decimal("3"))
        self.assertEqual(result.coverage.status, "partial")
        self.assertEqual(result.partial_answer["metric_refs"], ["roas"])
        self.assertEqual(result.partial_answer["disclosure"],
                         "仅覆盖 2026-09-01~2026-09-05，2026-09-05 起缺口")

    def test_never_imported_shop_gets_upload_remediation_without_covered_window(
            self):
        """M3 遗留收口：从未导入（整窗缺失）时 covered_window 必须是 null 且
        公开载荷校验接受它——不能编造一个“已覆盖窗口”。"""
        from bi_agent.business_query.tool import to_public_artifact
        from bi_agent.catalog import build_catalog

        shop_c = "SY-NEVER"
        # 能力已授予（可解析到手工来源绑定），但无任何 sync_state 覆盖与事实行。
        self.conn.execute(
            "INSERT INTO bi.shops(shop_id, platform, display_name, capabilities) "
            "VALUES (%s, 'tb', '从未导入店', %s) ON CONFLICT (shop_id) DO UPDATE "
            "SET capabilities = EXCLUDED.capabilities, "
            "display_name = EXCLUDED.display_name",
            (shop_c, sorted(MANUAL_METRICS)))
        result = self._query(["ad_spend"], shop_ids=(shop_c,),
                             allowed=(self.SHOP_A, self.SHOP_B, shop_c))
        self.assertEqual(result.status, "missing_data", result.limitations)
        self.assertEqual(result.data, [])
        self.assertIsNotNone(result.remediation, result.limitations)
        self.assertIsNone(result.remediation["covered_window"],
                          "从未导入 ⇒ 无任何已覆盖段，covered_window 是 null")
        self.assertEqual(result.remediation["missing_range"],
                         ["2026-09-01", "2026-09-10"])
        self.assertEqual(result.remediation["shop_ids"], [shop_c])

        catalog = build_catalog(self.conn, result, allowed_shop_ids=frozenset(
            {self.SHOP_A, self.SHOP_B, shop_c}))
        public_payload = to_public_artifact(result, catalog)
        self.assertIsNone(public_payload["remediation"]["covered_window"],
                          "公开形状必须接受 covered_window=null（前端据此隐藏窗口）")
        self.assertEqual(public_payload["remediation"]["shop_refs"],
                         [catalog.shop_ref(shop_c)])

    def test_capability_gap_uses_manual_wording_and_stays_fail_closed(self):
        self.conn.execute("RESET ROLE")
        self.conn.execute(
            "UPDATE bi.shops SET capabilities = '{paid_amount}' WHERE shop_id = %s",
            (self.SHOP_A,))
        result = self._query(["ad_spend"])
        self.assertEqual(result.status, "missing_data", result.limitations)
        self.assertEqual(result.data, [])
        self.assertIn("1 家店铺缺少 ad_spend 的手工导入数据，未执行查询",
                      result.limitations)
        self.assertIn("capability_unavailable", self._codes(result))

    def test_pdd_shop_never_answers_manual_metrics_even_with_facts(self):
        # 第三道守卫的动态面：手工事实/覆盖/能力标签全部伪造在场，查询仍拒。
        self.conn.execute("RESET ROLE")
        self.conn.execute(
            "INSERT INTO bi.shops(shop_id, platform, display_name, capabilities) "
            "VALUES ('SY-PDD', 'pdd', '同步拼多店', %s) "
            "ON CONFLICT (shop_id) DO UPDATE SET "
            "capabilities = EXCLUDED.capabilities",
            (sorted(MANUAL_METRICS),))
        self.conn.execute(
            "INSERT INTO bi.sycm_shop_daily(shop_id, stat_date, visitors, "
            "paid_amount_sycm, paid_buyers, ad_spend_all, cart_count, batch_id) "
            "VALUES ('SY-PDD', '2026-09-02', 100, 30, 2, 10, 5, 'forged')")
        self._coverage("SY-PDD", date(2026, 9, 1), date(2026, 9, 10))
        result = self._query(["ad_spend"], shop_ids=("SY-PDD",),
                             allowed=("SY-PDD", self.SHOP_A, self.SHOP_B))
        self.assertEqual(result.status, "missing_data", result.limitations)
        self.assertEqual(result.data, [])
        self.assertIn("capability_unavailable", self._codes(result))

    # -- 口径与数值诚实 ---------------------------------------------------------

    def test_mixed_erp_and_manual_metrics_refuse_in_one_query(self):
        result = self._query(["paid_amount", "ad_spend"])
        self.assertEqual(result.status, "invalid_parameters", result.limitations)
        self.assertEqual(result.data, [])
        self.assertIn(metrics_module.SYCM_MIXED_CALIBER_LIMITATION,
                      result.limitations)
        self.assertIn("basis_incompatible", self._codes(result))

    def test_zero_denominator_ratio_publishes_null_with_disclosure(self):
        self.conn.execute("RESET ROLE")
        self.conn.execute(
            "UPDATE bi.sycm_shop_daily SET ad_spend_all = 0 WHERE shop_id = %s",
            (self.SHOP_A,))
        result = self._query(["roas"])
        self.assertEqual(result.status, "ok", result.limitations)
        self.assertIsNone(result.data[0]["roas"], "零花费不得算出 0 倍 ROAS")
        self.assertIn(metrics_module.SYCM_RATIO_NOT_COMPUTABLE_LIMITATION,
                      result.limitations)
        self.assertIn("ratio_not_computable", self._codes(result))

    def test_missing_base_value_refuses_partial_aggregation_for_that_metric(self):
        self.conn.execute("RESET ROLE")
        self.conn.execute(
            "UPDATE bi.sycm_shop_daily SET visitors = NULL "
            "WHERE shop_id = %s AND stat_date = '2026-09-03'", (self.SHOP_A,))
        visitors = self._query(["visitors"])
        self.assertEqual(visitors.status, "missing_data", visitors.limitations)
        self.assertEqual(visitors.data, [])
        self.assertIn("窗口内 visitors 缺 1 天，拒绝部分汇总", visitors.limitations)

        spend = self._query(["ad_spend"])
        self.assertEqual(spend.status, "ok",
                         f"{spend.limitations}（不受缺值影响的指标照常可答）")
        self.assertEqual(Decimal(spend.data[0]["ad_spend"]), Decimal("90"))

        daily = self._query(["visitors"], group_by="day")
        self.assertEqual(daily.status, "ok", daily.limitations)
        by_day = {row["day"]: row for row in daily.data}
        self.assertIsNone(by_day["2026-09-03"]["visitors"],
                          "日行缺值发布 null，不补零")
        self.assertEqual(by_day["2026-09-02"]["visitors"], 100)

    def test_previous_period_comparison_uses_the_same_manual_caliber(self):
        self.conn.execute("RESET ROLE")
        # 本期 [09-11,09-16) 有一个高价日：change 必须来自同源两侧，而不是口径漂移。
        self.conn.execute(
            "UPDATE bi.sycm_shop_daily SET ad_spend_all = 30 "
            "WHERE shop_id = %s AND stat_date = '2026-09-11'", (self.SHOP_A,))
        result = self._query(["ad_spend"], start="2026-09-11", end="2026-09-16",
                             compare="previous_period")
        self.assertEqual(result.status, "ok", result.limitations)
        row = result.data[0]
        self.assertEqual(Decimal(row["ad_spend"]), Decimal("70"))
        self.assertEqual(Decimal(row["ad_spend_previous"]), Decimal("50"))
        self.assertEqual(Decimal(row["ad_spend_change"]), Decimal("20"))

    def test_previous_period_coverage_gap_downgrades_to_absolute_values(self):
        # 把覆盖收窄到 [09-05,09-16)：本期 [09-06,09-11) 仍完整，上期缺 [09-01,09-05)。
        self.conn.execute("RESET ROLE")
        self.conn.execute(
            "UPDATE bi.sync_state SET covered = "
            "tstzmultirange(tstzrange('2026-09-05','2026-09-16','[)')) "
            "WHERE source = %s AND entity = %s AND shop_id = %s",
            (MANUAL_EXPORT_SOURCE, MANUAL_EXPORT_ENTITY, self.SHOP_A))
        downgraded = self._query(["ad_spend"], start="2026-09-06", end="2026-09-11",
                                 compare="previous_period")
        self.assertEqual(downgraded.status, "ok", downgraded.limitations)
        self.assertIn("上期覆盖不足，无法比较，仅返回绝对值",
                      downgraded.limitations)
        self.assertIsNone(downgraded.data[0].get("ad_spend_previous"))

    def test_projection_round_trip_keeps_refs_and_drops_internal_names(self):
        import json

        from bi_agent.business_query.tool import to_model_result, to_public_artifact

        result = self._query(["ad_spend", "roas"])
        catalog = self._catalog(result)
        model_payload = to_model_result(result, catalog)
        public_payload = to_public_artifact(result, catalog)
        text = json.dumps(model_payload, ensure_ascii=False)
        self.assertNotIn(self.SHOP_A, text, "ERP 店铺主键不得进模型载荷")
        for entry in model_payload["basis"]:
            self.assertNotIn("shop_id", entry)
            self.assertIn("shop_ref", entry)
        self.assertEqual(Decimal(public_payload["data"][0]["ad_spend"]),
                         Decimal("90"),
                         "公开行与内部行同一合计（数值比较，不比字符串尺度）")

    def test_erp_capability_recompute_preserves_manual_tags(self):
        # ERP 对账重算是“唯一开通入口”，但它管不着导入面授予的手工词：
        # --apply 不得把手工能力静默回收。
        from bi_agent.sync import recompute_shop_capabilities

        self.conn.execute("RESET ROLE")
        grants = recompute_shop_capabilities(self.conn, [self.SHOP_A], apply=True)
        self.assertEqual(grants[0].granted & set(MANUAL_METRICS),
                         set(MANUAL_METRICS))
        stored = self.conn.execute(
            "SELECT capabilities FROM bi.shops WHERE shop_id = %s",
            (self.SHOP_A,)).fetchone()[0]
        self.assertTrue(set(MANUAL_METRICS) <= set(stored))


# ---------------------------------------------------------------------------
# 5) promotion actual_budget 两态（共享测试库）
# ---------------------------------------------------------------------------


@unittest.skipUnless(os.getenv("BI_TEST_ADMIN_DSN"), "未配置独立测试数据库")
class PromotionActualBudgetDatabaseTests(unittest.TestCase):
    """有 ad_spend 覆盖的店可答（实耗=手工导入，带披露）；无覆盖行为不变。"""

    SHOP_A = "SY-A"
    SHOP_B = "SY-B"

    def setUp(self):
        from .dbfixtures import connect_test_db

        self.conn = connect_test_db(self)
        migration = (Path(__file__).parents[1] / "sql"
                     / "030_sycm_shop_daily_reader_view.sql")
        self.conn.execute(migration.read_text(encoding="utf-8"))
        self.conn.execute(
            "INSERT INTO bi.shops(shop_id, platform, display_name, capabilities) "
            "VALUES (%s, 'tb', '实耗淘店A', %s), (%s, 'tb', '实耗淘店B', '{aov}') "
            "ON CONFLICT (shop_id) DO UPDATE SET "
            "capabilities = EXCLUDED.capabilities",
            (self.SHOP_A, sorted(MANUAL_METRICS), self.SHOP_B))
        for offset in range(9):
            self.conn.execute(
                "INSERT INTO bi.sycm_shop_daily(shop_id, stat_date, ad_spend_all, "
                "batch_id) VALUES (%s, %s, 10, 'sycm-promo-seed') "
                "ON CONFLICT (shop_id, stat_date) DO UPDATE SET ad_spend_all = 10",
                (self.SHOP_A, date(2026, 9, 1 + offset)))
        self.conn.execute(
            "INSERT INTO bi.sync_state(source, entity, shop_id, watermark, covered, "
            "data_as_of, quality_status, quality_rule) VALUES "
            "(%s, %s, %s, '2026-09-10 00:00+08', "
            "tstzmultirange(tstzrange('2026-09-01','2026-09-10','[)')), "
            "'2026-09-10 00:00+08', 'unknown', 'manual-export/unverified') "
            "ON CONFLICT (source, entity, shop_id) DO UPDATE SET "
            "covered = EXCLUDED.covered, data_as_of = EXCLUDED.data_as_of",
            (MANUAL_EXPORT_SOURCE, MANUAL_EXPORT_ENTITY, self.SHOP_A))

    def _evaluate(self, shop_ids, **overrides):
        from bi_agent.promotion import PromotionRequest, evaluate_promotion

        arguments = {"mode": "actual_budget", "start": "2026-09-01",
                     "end": "2026-09-10", "shop_ids": list(shop_ids)}
        arguments.update(overrides)
        request = PromotionRequest.model_validate(arguments)
        return evaluate_promotion(
            request, confirmed_inputs={},
            now=datetime(2026, 9, 10, 9, tzinfo=BEIJING),
            conn=self.conn, allowed_shop_ids=frozenset({self.SHOP_A, self.SHOP_B}))

    def test_covered_shop_answers_with_manual_spend_and_disclosure(self):
        result = self._evaluate([self.SHOP_A])
        self.assertEqual(result.status, "ok", result.limitations)
        row = result.data[0]
        self.assertEqual(row["mode"], "actual_budget")
        self.assertEqual(row["shop_id"], self.SHOP_A)
        self.assertEqual(Decimal(row["actual_spend"]), Decimal("90"))
        self.assertEqual(row["spent_through"], "2026-09-10")
        self.assertEqual(row["basis"], "手工导入生意参谋实耗")
        self.assertIn("实耗来自手工导入的生意参谋导出（质量未核验），"
                      "不代表广告账户实际扣费", result.limitations)
        self.assertIn("来源质量未核验（尚无对账记录）", result.limitations)
        self.assertIn("统计日期", result.metric_definition["actual_spend"])

    def test_uncovered_shop_keeps_the_legacy_missing_data(self):
        result = self._evaluate([self.SHOP_B])
        self.assertEqual(result.status, "missing_data", result.limitations)
        self.assertEqual(result.data, [])
        self.assertIn("尚未取得推广实耗及完整同口径成本，当前只能进行明确假设的预算测算",
                      result.limitations)

    def test_mixed_scope_answers_covered_shops_and_counts_the_rest(self):
        result = self._evaluate([self.SHOP_A, self.SHOP_B])
        self.assertEqual(result.status, "ok", result.limitations)
        self.assertEqual([row["shop_id"] for row in result.data], [self.SHOP_A])
        self.assertIn("1 家店铺缺少该窗口的手工实耗数据", result.limitations)

    def test_partially_covered_window_counts_as_uncovered(self):
        # 覆盖不包含整个请求窗口：该店拿不出“该窗口的实耗”，不当部分合计。
        result = self._evaluate([self.SHOP_A], start="2026-09-05", end="2026-09-20")
        self.assertEqual(result.status, "missing_data", result.limitations)
        self.assertEqual(result.data, [])

    def test_out_of_scope_shop_is_forbidden(self):
        result = self._evaluate(["ent-not-real"])
        self.assertEqual(result.status, "forbidden", result.limitations)
        self.assertIn("店铺不在授权范围", result.limitations)

    def test_legacy_call_without_conn_stays_missing_data(self):
        from bi_agent.promotion import PromotionRequest, evaluate_promotion

        request = PromotionRequest(mode="actual_budget", start="2026-09-01",
                                   end="2026-09-10")
        result = evaluate_promotion(request, confirmed_inputs={},
                                    now=datetime(2026, 9, 10, 9, tzinfo=BEIJING))
        self.assertEqual(result.status, "missing_data")

    def test_shop_ids_are_only_accepted_for_actual_budget(self):
        from bi_agent.promotion import PromotionRequest

        with self.assertRaises(ValueError):
            PromotionRequest(mode="sales_cap", start="2026-09-01", end="2026-09-10",
                             sales_estimate="100", target_ratio="0.1",
                             shop_ids=["S1"])
        with self.assertRaises(ValueError):
            PromotionRequest(mode="contribution_cap", start="2026-09-01",
                             end="2026-09-10", shop_ids=["S1"])

    def test_projection_round_trip_accepts_actual_budget_shape(self):
        from bi_agent.business_query.tool import to_model_result, to_public_artifact
        from bi_agent.catalog import build_catalog

        result = self._evaluate([self.SHOP_A])
        catalog = build_catalog(self.conn, result,
                                allowed_shop_ids=frozenset({self.SHOP_A,
                                                            self.SHOP_B}))
        model_payload = to_model_result(result, catalog)
        public_payload = to_public_artifact(result, catalog)
        self.assertEqual(model_payload["data"][0]["shop_ref"],
                         catalog.shop_ref(self.SHOP_A))
        self.assertNotIn("shop_id", model_payload["data"][0])
        self.assertEqual(public_payload["metric_definition"]["actual_spend"],
                         result.metric_definition["actual_spend"])


if __name__ == "__main__":
    unittest.main()
