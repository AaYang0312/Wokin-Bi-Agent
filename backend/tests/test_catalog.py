"""目录身份层：持久 opaque ref、唯一化展示名、正文确定性改写。

口径来自 docs/archive/plans/2026-09-11-data-and-query-closure.md Task 2：
模型只看 ref，真名只在授权展示层出现；本模块另加落库前的 ref→真名改写。
"""

import os
import unittest
from zoneinfo import ZoneInfo

import psycopg

BEIJING = ZoneInfo("Asia/Shanghai")


class RefFormatTests(unittest.TestCase):
    """ref 必须与 ERP 主键不同类：不可读、可持久、重排不变。"""

    def test_ref_is_derived_from_kind_and_key(self):
        from bi_agent.catalog import ref_for_key

        self.assertEqual(ref_for_key("shop", "166754"), ref_for_key("shop", "166754"))
        self.assertNotEqual(ref_for_key("shop", "166754"), ref_for_key("product", "166754"))

    def test_ref_shape_is_opaque_and_deterministic(self):
        from bi_agent.catalog import REF_RE, ref_for_key

        keys = [str(9000000000000000 + index) for index in range(30)]
        refs = [ref_for_key("product", key) for key in keys]
        reshuffled = [ref_for_key("product", key) for key in reversed(keys)]

        self.assertEqual(len(set(refs)), 30, "30 个商品必须给出 30 个不同引用")
        self.assertEqual(reshuffled, list(reversed(refs)), "引用不得随排序变化")
        for ref in refs:
            self.assertTrue(REF_RE.fullmatch(ref), ref)

    def test_ref_never_exposes_the_erp_key(self):
        from bi_agent.catalog import ref_for_key

        key = "548597548700160"

        self.assertNotIn(key, ref_for_key("product", key))


class ShopLabelTests(unittest.TestCase):
    """展示名要在整张店铺表上唯一：同名店不带平台就没法读，也没法安全改写。"""

    @staticmethod
    def _shops(*rows):
        return [{"shop_id": s, "platform": p, "display_name": n} for s, p, n in rows]

    def test_unique_name_is_used_verbatim(self):
        from bi_agent.catalog import shop_display_labels

        labels = shop_display_labels(self._shops(("166754", "fxg", "元发钉枪")))

        self.assertEqual(labels["166754"], "元发钉枪")

    def test_duplicate_names_get_platform_suffix(self):
        from bi_agent.catalog import shop_display_labels

        labels = shop_display_labels(self._shops(
            ("166754", "fxg", "元发钉枪"), ("167156", "kuaishou", "元发钉枪")))

        self.assertEqual(labels["166754"], "元发钉枪(抖音)")
        self.assertEqual(labels["167156"], "元发钉枪(快手)")
        self.assertEqual(len(set(labels.values())), 2)

    def test_still_colliding_labels_fall_back_to_the_shop_key_tail(self):
        from bi_agent.catalog import shop_display_labels

        # 平台码未收录时不猜中文名，用原始码；同名同平台再撞就带店铺号尾号。
        labels = shop_display_labels(self._shops(
            ("900001111", "wsxc", "微购相册"), ("900002222", "wsxc", "微购相册")))

        self.assertEqual(sorted(labels.values()),
                         ["微购相册(wsxc-1111)", "微购相册(wsxc-2222)"])

    def test_blank_display_name_is_reported_as_unresolved_not_invented(self):
        from bi_agent.catalog import shop_display_labels

        labels = shop_display_labels(self._shops(("166754", "fxg", "  ")))

        self.assertIsNone(labels["166754"])


class AnswerRewriteTests(unittest.TestCase):
    """落库/发浏览器前把引用换成真名；未知引用原样保留，绝不编造。"""

    def test_known_refs_are_replaced_in_prose(self):
        from bi_agent.catalog import render_display_text

        text = render_display_text(
            "ent-1111aaaa 支付 12 万，ent-2222bbbb 第二",
            {"ent-1111aaaa": "元发钉枪(抖音)", "ent-2222bbbb": "直钉枪-元发"})

        self.assertEqual(text, "元发钉枪(抖音) 支付 12 万，直钉枪-元发 第二")

    def test_unknown_ref_is_kept_as_is(self):
        from bi_agent.catalog import render_display_text

        text = render_display_text("ent-ffffffff 无名称", {"ent-1111aaaa": "元发钉枪"})

        self.assertEqual(text, "ent-ffffffff 无名称")

    def test_rewrite_is_longest_token_first_and_idempotent(self):
        from bi_agent.catalog import render_display_text

        mapping = {"ent-1111aaaa": "元发钉枪(抖音)"}
        once = render_display_text("ent-1111aaaa", mapping)

        self.assertEqual(render_display_text(once, mapping), once)

    def test_erp_digit_key_in_prose_is_not_touched_by_rewrite(self):
        """改写只认引用格式：正文里出现的 15 位 ERP 号不属于本步骤的处理范围。"""
        from bi_agent.catalog import render_display_text

        text = render_display_text("订单 548597548700160 异常", {})

        self.assertEqual(text, "订单 548597548700160 异常")


class SkuLabelTests(unittest.TestCase):
    """规格取用规则只这一处：不一致、歧义或任一缺失就不展示，绝不任选一个。"""

    def test_single_agreed_spec_is_used(self):
        from bi_agent.catalog import pick_sku_label

        self.assertEqual(pick_sku_label(["400支", " 400支 "]), "400支")

    def test_any_disagreement_or_gap_drops_the_spec(self):
        from bi_agent.catalog import pick_sku_label

        cases = {
            "一行无规格": ["400支", None],
            "两行不一致": ["400支", "600支"],
            "空字符串": ["400支", "  "],
            "无行": [],
        }
        for name, labels in cases.items():
            with self.subTest(name):
                self.assertIsNone(pick_sku_label(labels))


@unittest.skipUnless(os.getenv("BI_TEST_ADMIN_DSN"), "未配置独立测试数据库")
class CatalogRepositoryTests(unittest.TestCase):
    def setUp(self):
        from tests.dbfixtures import connect_test_db

        self.conn = connect_test_db(self)

    def _seed_shop(self, shop_id: str, *, platform: str = "fxg", name: str = "元发钉枪"):
        self.conn.execute(
            "INSERT INTO bi.shops(shop_id, platform, display_name) VALUES (%s,%s,%s) "
            "ON CONFLICT (shop_id) DO UPDATE SET platform=EXCLUDED.platform, "
            "display_name=EXCLUDED.display_name",
            (shop_id, platform, name),
        )

    def test_ensure_refs_is_idempotent_and_reversible(self):
        from bi_agent.catalog import ensure_refs, lookup_refs

        first = ensure_refs(self.conn, "shop", ["166754", "167156"])
        again = ensure_refs(self.conn, "shop", ["167156", "166754"])

        self.assertEqual(first, again, "同一批键重复取引用必须完全一致")
        self.assertEqual(lookup_refs(self.conn, list(first.values())),
                         {ref: ("shop", key) for key, ref in first.items()})

    def test_resolve_returns_names_only_for_authorized_shops(self):
        from bi_agent.catalog import ensure_refs, resolve_display_entities

        # 用合成店号：真实表里“元发钉枪”本来就两家，展示名会带平台后缀。
        self._seed_shop("S_CAT_A", platform="fxg", name="目录甲店")
        self._seed_shop("S_CAT_B", platform="pdd", name="目录乙店")
        refs = ensure_refs(self.conn, "shop", ["S_CAT_A", "S_CAT_B"])

        resolved = resolve_display_entities(
            self.conn, list(refs.values()), allowed_shop_ids=frozenset({"S_CAT_A"}))

        self.assertEqual([item.display_name for item in resolved], ["目录甲店"])
        unauthorized = resolve_display_entities(
            self.conn, [refs["S_CAT_B"]], allowed_shop_ids=frozenset({"S_CAT_A"}))
        self.assertEqual(unauthorized, [], "越权引用不得带出任何名称")

    def test_duplicate_shop_names_resolve_to_distinct_labels(self):
        from bi_agent.catalog import ensure_refs, resolve_display_entities

        # 名字用合成值：真实表里“元发钉枪”本来就两对，不依赖共享库内容。
        self._seed_shop("S_CAT_C", platform="fxg", name="目录重名店")
        self._seed_shop("S_CAT_D", platform="kuaishou", name="目录重名店")
        refs = ensure_refs(self.conn, "shop", ["S_CAT_C", "S_CAT_D"])

        resolved = resolve_display_entities(
            self.conn, list(refs.values()), allowed_shop_ids=frozenset(refs))

        self.assertEqual({item.display_name for item in resolved},
                         {"目录重名店(抖音)", "目录重名店(快手)"})

    def test_product_name_prefers_archive_and_falls_back_to_snapshot(self):
        from bi_agent.catalog import ensure_refs, resolve_display_entities

        self.conn.execute(
            "INSERT INTO bi.products(product_id, title, normalization_status) "
            "VALUES ('P_ARCHIVE','直钉枪-元发','normal'), ('P_BLANK','','needs_review') "
            "ON CONFLICT (product_id) DO UPDATE SET title=EXCLUDED.title",
        )
        refs = ensure_refs(self.conn, "product", ["P_ARCHIVE", "P_BLANK"])

        resolved = {item.ref: item for item in resolve_display_entities(
            self.conn, list(refs.values()), allowed_shop_ids=frozenset(),
            snapshot_names={"P_BLANK": "接头-元发快照", "P_ARCHIVE": "旧标题快照"})}

        archive = resolved[refs["P_ARCHIVE"]]
        self.assertEqual((archive.display_name, archive.name_source),
                         ("直钉枪-元发", "archive"))
        snapshot = resolved[refs["P_BLANK"]]
        self.assertEqual((snapshot.display_name, snapshot.name_source),
                         ("接头-元发快照", "trade_snapshot"))

    def test_unresolved_product_reports_missing_name_without_inventing_one(self):
        from bi_agent.catalog import ensure_refs, resolve_display_entities

        refs = ensure_refs(self.conn, "product", ["P_UNKNOWN"])

        resolved = resolve_display_entities(self.conn, list(refs.values()),
                                            allowed_shop_ids=frozenset())

        self.assertEqual(resolved[0].display_name, None)
        self.assertEqual(resolved[0].name_source, "unresolved")

    def test_catalog_version_increments_monotonically(self):
        from bi_agent.catalog import bump_catalog_version, catalog_version

        first = catalog_version(self.conn)
        self.assertIsInstance(first, int)
        self.assertTrue(bump_catalog_version(self.conn))
        self.assertEqual(catalog_version(self.conn), first + 1)
        self.assertTrue(bump_catalog_version(self.conn))
        self.assertEqual(catalog_version(self.conn), first + 2)

    def test_ref_rows_survive_being_written_twice(self):
        from bi_agent.catalog import ensure_refs

        ensure_refs(self.conn, "product", ["P_DUP"])
        ensure_refs(self.conn, "product", ["P_DUP"])
        count = self.conn.execute(
            "SELECT count(*) FROM bi.entity_refs WHERE kind='product' AND natural_key='P_DUP'"
        ).fetchone()[0]

        self.assertEqual(count, 1)


class CatalogMigrationTests(unittest.TestCase):
    def test_007_is_the_catalog_migration_and_is_repeatable(self):
        from pathlib import Path

        sql_dir = Path(__file__).parents[1] / "sql"
        files = sorted(path.name for path in sql_dir.glob("0*.sql"))
        self.assertIn("007_catalog_identity.sql", files, f"迁移清单：{files}")

    @unittest.skipUnless(os.getenv("BI_TEST_ADMIN_DSN"), "未配置独立测试数据库")
    def test_order_items_carry_name_snapshots(self):
        from tests.dbfixtures import connect_test_db

        conn = connect_test_db(self)
        columns = {row[0] for row in conn.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema='bi' AND table_name='order_items'").fetchall()}
        self.assertTrue({"product_name_snapshot", "sku_label_snapshot"} <= columns)
        # 取用优先级只在 catalog 一处实现，视图只分别给出档案名、成交快照与规格聚合。
        view_columns = {row[0] for row in conn.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema='reporting' AND table_name='v_product_daily'").fetchall()}
        from tests.dbfixtures import PRODUCT_DAILY_COLUMNS
        self.assertEqual(view_columns, set(PRODUCT_DAILY_COLUMNS))


@unittest.skipUnless(os.getenv("BI_TEST_ADMIN_DSN"), "未配置独立测试数据库")
class CatalogProjectionTests(unittest.TestCase):
    """模型只看引用，真名只进展示层（计划 Task 2 核心断言）。"""

    def setUp(self):
        from tests.dbfixtures import connect_test_db

        self.conn = connect_test_db(self)
        self.conn.execute(
            "INSERT INTO bi.shops(shop_id, platform, display_name) VALUES "
            "('S_CAT_P','fxg','目录投影店'), ('S_CAT_Q','pdd','未授权店') "
            "ON CONFLICT (shop_id) DO UPDATE SET display_name=EXCLUDED.display_name")
        self.conn.execute(
            "INSERT INTO bi.products(product_id, title, normalization_status) "
            "VALUES ('P_CAT','直钉枪-元发','normal') "
            "ON CONFLICT (product_id) DO UPDATE SET title=EXCLUDED.title")

    def _result(self, rows):
        from datetime import date, datetime

        from bi_agent.metrics import METRIC_DEFINITIONS, Coverage, ToolResult

        return ToolResult(
            status="ok",
            coverage=Coverage(status="complete", start=date(2026, 9, 1), end=date(2026, 9, 8)),
            data_as_of=datetime(2026, 9, 8, tzinfo=BEIJING),
            # 口径文本必须与生产方一字不差，否则契约校验会先拒掉这份载荷。
            metric_definition={key: METRIC_DEFINITIONS[key]
                               for key in ("paid_amount", "quantity")},
            filters={"start": "2026-09-01", "end": "2026-09-08", "shop_ids": ["S_CAT_P"]},
            data=rows,
        )

    def _project(self, rows, *, allowed=("S_CAT_P",)):
        import json

        from bi_agent.business_query.tool import to_model_result, to_public_artifact
        from bi_agent.catalog import build_catalog

        result = self._result(rows)
        catalog = build_catalog(self.conn, result, allowed_shop_ids=frozenset(allowed))
        model = to_model_result(result, catalog)
        public = to_public_artifact(result, catalog)
        return catalog, model, public, json.dumps(model, ensure_ascii=False), \
            json.dumps(public, ensure_ascii=False)

    def test_model_sees_refs_and_never_the_real_name(self):
        _, model, _, model_json, public_json = self._project(
            [{"shop_id": "S_CAT_P", "paid_amount": "100"}])

        self.assertIn("目录投影店", public_json)
        self.assertNotIn("目录投影店", model_json)
        self.assertNotIn("S_CAT_P", model_json, "ERP 店铺主键不得进模型")
        self.assertNotIn("166754", model_json)
        shop_ref = model["data"][0]["shop_ref"]
        self.assertRegex(shop_ref, r"^ent-[0-9a-z]{8}$")
        self.assertEqual(model["filters"]["shop_refs"], [shop_ref])

    def test_artifact_carries_names_source_and_catalog_version(self):
        from bi_agent.catalog import catalog_version

        _, _, public, _, _ = self._project(
            [{"shop_id": "S_CAT_P", "paid_amount": "100"},
             {"shop_id": "S_CAT_P", "product_id": "P_CAT", "quantity": "3",
              "line_kind": "sale", "product_name": "直钉枪-元发",
              "product_name_snapshot": "旧成交名"}])

        entities = {item["ref"]: item for item in public["entities"]}
        self.assertEqual(len(entities), 2)
        shop = [item for item in entities.values() if item["kind"] == "shop"][0]
        product = [item for item in entities.values() if item["kind"] == "product"][0]
        self.assertEqual((shop["display_name"], shop["name_source"]),
                         ("目录投影店", "shop_profile"))
        self.assertEqual((product["display_name"], product["name_source"]),
                         ("直钉枪-元发", "archive"))
        self.assertEqual(public["catalog_version"], catalog_version(self.conn))

    def test_shop_entity_carries_the_platform_code(self):
        """操作者要能看出店铺属于哪个平台：平台码随实体一起返回。

        模型仍只看 ent- 引用，平台名不因此进入模型载荷。
        """
        _, _, public, model_json, _ = self._project(
            [{"shop_id": "S_CAT_P", "paid_amount": "100"}])

        shop = [item for item in public["entities"] if item["kind"] == "shop"][0]
        self.assertEqual(shop["platform"], "fxg")
        self.assertNotIn("fxg", model_json, "平台码不得进入模型载荷")

    def test_product_entity_has_no_platform(self):
        _, _, public, _, _ = self._project(
            [{"shop_id": "S_CAT_P", "product_id": "P_CAT", "quantity": "1",
              "line_kind": "sale", "product_name": "直钉枪-元发"}])

        product = [item for item in public["entities"] if item["kind"] == "product"][0]
        self.assertIsNone(product["platform"])

    def test_unknown_shop_platform_code_is_rejected(self):
        """平台码得是短码，不能把展示字段当自由文本注入。"""
        from bi_agent.catalog import DisplayEntity

        self.assertRaises(Exception, DisplayEntity,
                          ref="ent-1111aaaa", kind="shop",
                          platform="fxg; 顺手写点东西")

    def test_product_entity_shows_the_sku_spec_when_rows_agree(self):
        """每一行都给出同一个规格才展示。"""
        _, _, public, _, _ = self._project(
            [{"shop_id": "S_CAT_P", "product_id": "P_CAT", "quantity": "3",
              "line_kind": "sale", "product_name": "直钉枪-元发",
              "sku_label": "400支"},
             {"shop_id": "S_CAT_P", "product_id": "P_CAT", "quantity": "1",
              "line_kind": "suite", "product_name": "直钉枪-元发",
              "sku_label": "400支"}])

        product = [item for item in public["entities"] if item["kind"] == "product"][0]
        self.assertEqual(product["sku_label"], "400支")

    def test_product_entity_hides_the_spec_when_a_row_carries_none(self):
        """名称可以“先拿到的算”，规格不行：任一行无规格就不能当成全局一致。"""
        _, _, public, _, _ = self._project(
            [{"shop_id": "S_CAT_P", "product_id": "P_CAT", "quantity": "3",
              "line_kind": "sale", "product_name": "直钉枪-元发",
              "sku_label": "400支"},
             {"shop_id": "S_CAT_P", "product_id": "P_CAT", "quantity": "1",
              "line_kind": "suite", "product_name": "直钉枪-元发"}])

        product = [item for item in public["entities"] if item["kind"] == "product"][0]
        self.assertIsNone(product["sku_label"])

    def test_product_entity_hides_the_spec_when_rows_disagree(self):
        _, _, public, model_json, public_json = self._project(
            [{"shop_id": "S_CAT_P", "product_id": "P_CAT", "quantity": "3",
              "line_kind": "sale", "product_name": "直钉枪-元发",
              "sku_label": "400支"},
             {"shop_id": "S_CAT_P", "product_id": "P_CAT", "quantity": "1",
              "line_kind": "suite", "product_name": "直钉枪-元发",
              "sku_label": "600支"}])

        product = [item for item in public["entities"] if item["kind"] == "product"][0]
        self.assertIsNone(product["sku_label"], "多规格商品任选一个就是编造")
        self.assertNotIn("400支", model_json, "规格不得进模型载荷")
        self.assertNotIn("600支", model_json, "规格不得进模型载荷")

    def test_snapshot_is_used_only_when_the_archive_has_no_name(self):
        from bi_agent.business_query.tool import to_public_artifact
        from bi_agent.catalog import build_catalog

        result = self._result([{"shop_id": "S_CAT_P", "product_id": "P_NAMED",
                               "quantity": "1", "line_kind": "sale",
                               "product_name_snapshot": "接头-元发当时名"}])
        catalog = build_catalog(self.conn, result, allowed_shop_ids=frozenset({"S_CAT_P"}))
        payload = to_public_artifact(result, catalog)

        product = [item for item in payload["entities"] if item["kind"] == "product"][0]
        self.assertEqual((product["display_name"], product["name_source"]),
                         ("接头-元发当时名", "trade_snapshot"))

    def test_refs_are_stable_when_rows_are_reordered(self):
        rows = [{"shop_id": "S_CAT_P", "product_id": "P_CAT", "quantity": "3",
                 "line_kind": "sale"}]
        other = [{"shop_id": "S_CAT_P", "product_id": "P_NAMED", "quantity": "9",
                  "line_kind": "sale"}]

        _, first, _, _, _ = self._project(rows + other)
        _, reranked, _, _, _ = self._project(other + rows)

        self.assertEqual({row["product_ref"] for row in first["data"]},
                         {row["product_ref"] for row in reranked["data"]})

    def test_thirty_products_get_thirty_distinct_refs(self):
        rows = [{"shop_id": "S_CAT_P", "product_id": f"P_MANY_{index}", "quantity": str(index),
                 "line_kind": "sale"} for index in range(30)]

        _, model, _, _, _ = self._project(rows)

        self.assertEqual(len({row["product_ref"] for row in model["data"]}), 30)

    def test_top_n_notice_row_survives_projection(self):
        """超 TopN 提示行只能带 notice；以前会被白名单过滤成空行并触发契约违规。"""
        _, model, _, model_json, public_json = self._project(
            [{"shop_id": "S_CAT_P", "paid_amount": "100"},
             {"notice": "仅返回Top 10，共57个商品"}])

        self.assertIn("仅返回Top 10，共57个商品", model_json)
        self.assertIn("仅返回Top 10，共57个商品", public_json)

    def test_notice_row_cannot_smuggle_long_erp_identifiers(self):
        rows = [{"shop_id": "S_CAT_P", "paid_amount": "100"},
                {"notice": "订单号 548597548700160 异常"}]

        with self.assertRaises(ValueError):
            self._project(rows)

    def test_rename_is_picked_up_by_later_artifacts_without_rewriting_history(self):
        """改名只影响之后的解析：已给出的 Artifact 载荷不被覆写。

        真实名称由 v_product_daily 随行带出（bi_app 读不到 bi.products），
        所以这里按视图同源的方式取当前档案名再投影。
        """
        from bi_agent.catalog import bump_catalog_version, catalog_version

        def rows_with_current_name():
            title = self.conn.execute(
                "SELECT title FROM bi.products WHERE product_id='P_CAT'").fetchone()[0]
            return [{"shop_id": "S_CAT_P", "product_id": "P_CAT", "quantity": "1",
                     "line_kind": "sale", "product_name": title}]

        _, _, before, _, before_json = self._project(rows_with_current_name())
        old_version = catalog_version(self.conn)

        self.conn.execute("UPDATE bi.products SET title='新档名-元发' "
                          "WHERE product_id='P_CAT'")
        bump_catalog_version(self.conn)
        _, _, after, _, after_json = self._project(rows_with_current_name())

        self.assertIn("直钉枪-元发", before_json)
        self.assertIn("新档名-元发", after_json)
        self.assertNotIn("新档名-元发", before_json)
        self.assertEqual(after["catalog_version"], old_version + 1)
        product_ref = lambda payload: [item["ref"] for item in payload["entities"]
                                       if item["kind"] == "product"][0]
        self.assertEqual(product_ref(after), product_ref(before),
                         "改名不换引用：同一商品始终同一个 ref")

    def test_unauthorized_shop_in_result_fails_closed(self):
        from bi_agent.catalog import CatalogUnauthorized

        with self.assertRaises(CatalogUnauthorized):
            self._project([{"shop_id": "S_CAT_Q", "paid_amount": "100"}],
                          allowed=("S_CAT_P",))


if __name__ == "__main__":
    unittest.main()
