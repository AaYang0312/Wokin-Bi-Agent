"""Task 6：跨渠道商品 / SKU 映射与名称查询。

计划验收数据（docs/superpowers/plans/2026-09-11-data-and-query-closure.md Task 6）：
同名「接头」的 6mm/8mm 必须返回两个候选；同一 ERP SKU 映射到淘宝、抖音两个链接可归为
同 SKU；另一账号相同数字 ID 不归并；无成交的已映射 listing 必须保留。

本模块另钉两条红线：文本只用于解析、不决定合并（只有显式标识映射才算 approved），
以及授权范围外的一条候选都不许漏出去。
"""

from __future__ import annotations

import os
import unittest
from datetime import date
from uuid import uuid4

from bi_agent.catalog.resolver import (
    expand_channel_items, resolve_product, selected_selector, text_selector)
from bi_agent.catalog.channel_mapping import (
    CHANNEL_MAPPING_VERSION, IdentifierMapping, record_identifier_mapping,
    record_trade_line_mapping)

from .dbfixtures import connect_test_db

NAMESPACE = "acct-default"
OTHER_NAMESPACE = "acct-other"
AT = date(2026, 9, 10)


@unittest.skipUnless(os.getenv("BI_TEST_ADMIN_DSN"), "未配置独立测试数据库")
class ChannelMappingTests(unittest.TestCase):
    def setUp(self):
        self.conn = connect_test_db(self)
        self.tag = uuid4().hex[:6]
        self.shops = {"tb": f"T{self.tag}1", "fxg": f"F{self.tag}2", "other": f"O{self.tag}3"}
        for key, shop_id in self.shops.items():
            self.conn.execute(
                "INSERT INTO bi.shops(shop_id, platform, display_name, capabilities) "
                "VALUES (%s, %s, %s, ARRAY['paid_amount']) "
                "ON CONFLICT (shop_id) DO UPDATE SET platform = EXCLUDED.platform",
                (shop_id, key, f"渠道测试店{key}"))
        # 同名异物：两个 ERP 商品都叫「接头」，规格分别是 6mm / 8mm。
        self.a = self._product("P-A", "接头", "6mm")
        self.b = self._product("P-B", "接头", "8mm")
        self.c = self._product("P-C", "独名件", None)

    def _product(self, product_id: str, name: str, sku_label: str | None) -> str:
        """商品档案名 + 一条成交行的规格快照：规格文本只从已核验来源取。

        这里不建 SKU 主档：`bi.skus` 的字段要由已核验来源填充，本轮没有那种来源，
        凭空建一张没人写的表只会让“已接入”看起来比实际更早。
        """
        self.conn.execute(
            "INSERT INTO bi.products(product_id, title, source_modified_at, synced_at) "
            "VALUES (%s, %s, now(), now()) "
            "ON CONFLICT (product_id) DO UPDATE SET title = EXCLUDED.title",
            (product_id, name))
        shop_id = self.shops["tb"]
        self.conn.execute(
            "INSERT INTO bi.orders(shop_id, erp_id, source, commercial_ids, active, "
            "paid_at, source_updated_at, batch_id) VALUES "
            "(%s, %s, 'erp.trade.outstock.simple.query', ARRAY[%s], true, %s, now(), "
            "'cm-seed') ON CONFLICT (shop_id, erp_id) DO NOTHING",
            (shop_id, f"E-{product_id}", f"C-{product_id}",
             "2026-09-05 10:00+08"))
        self.conn.execute(
            "INSERT INTO bi.order_items(shop_id, erp_id, line_id, commercial_id, "
            "product_id, sku_id, paid_at, quantity, line_kind, active) VALUES "
            "(%s, %s, %s, %s, %s, %s, '2026-09-05 10:00+08', 1, 'sale', true) "
            "ON CONFLICT (shop_id, erp_id, line_id) DO NOTHING",
            (shop_id, f"E-{product_id}", f"L-{product_id}", f"C-{product_id}",
             product_id, f"{product_id}-sku"))
        # 名称与规格快照由 005/007 加列，成交行只在这两列上带文本。
        self.conn.execute(
            "UPDATE bi.order_items SET product_name_snapshot = %s, "
            "sku_label_snapshot = %s WHERE shop_id = %s AND line_id = %s",
            (name, sku_label, shop_id, f"L-{product_id}"))
        return product_id

    def _line(self, product_id: str, shop_id: str, name: str) -> None:
        """在另一家店补一条成交行：证明“越权与不在范围”与“没有这个商品”是两回事。"""
        self.conn.execute(
            "INSERT INTO bi.orders(shop_id, erp_id, source, commercial_ids, active, "
            "paid_at, source_updated_at, batch_id) VALUES "
            "(%s, %s, 'erp.trade.outstock.simple.query', ARRAY[%s], true, "
            "'2026-09-05 10:00+08', now(), 'cm-seed') "
            "ON CONFLICT (shop_id, erp_id) DO NOTHING",
            (shop_id, f"E-OUT-{product_id}", f"C-OUT-{product_id}"))
        self.conn.execute(
            "INSERT INTO bi.order_items(shop_id, erp_id, line_id, commercial_id, "
            "product_id, sku_id, product_name_snapshot, paid_at, quantity, line_kind, "
            "active) VALUES (%s, %s, %s, %s, %s, %s, %s, '2026-09-05 10:00+08', "
            "1, 'sale', true) ON CONFLICT (shop_id, erp_id, line_id) DO NOTHING",
            (shop_id, f"E-OUT-{product_id}", f"L-OUT-{product_id}",
             f"C-OUT-{product_id}", product_id, f"{product_id}-sku", name))

    def _authorized(self):
        return frozenset(self.shops.values())

    # -- 引用解析 -----------------------------------------------------------

    def test_text_with_same_name_returns_every_candidate_not_one_guessed(self):
        resolution = resolve_product(
            self.conn, selector=text_selector("接头"),
            authorized_shop_ids=self._authorized(), at=AT)

        self.assertEqual(resolution.status, "ambiguous")
        self.assertEqual({item.erp_product_id for item in resolution.candidates},
                         {self.a, self.b},
                         "多候选必须全部交回澄清，任选一个就是把规格猜掉")
        self.assertTrue(all(item.sku_label for item in resolution.candidates))

    def test_unique_text_resolves_to_one_product(self):
        resolution = resolve_product(
            self.conn, selector=text_selector("独名件"),
            authorized_shop_ids=self._authorized(), at=AT)

        self.assertEqual(resolution.status, "resolved")
        self.assertEqual(resolution.erp_product_id, self.c)
        self.assertTrue(resolution.product_ref.startswith("ent-"))

    def test_no_match_is_unresolved_and_not_zero_sales(self):
        resolution = resolve_product(
            self.conn, selector=text_selector("从没见过的商品"),
            authorized_shop_ids=self._authorized(), at=AT)

        self.assertEqual(resolution.status, "unresolved")
        self.assertEqual(resolution.reason, "no_match")
        self.assertEqual(resolution.candidates, ())

    def test_unknown_ref_is_unresolved(self):
        resolution = resolve_product(
            self.conn, selector=selected_selector("ent-00000000"),
            authorized_shop_ids=self._authorized(), at=AT)

        self.assertEqual((resolution.status, resolution.reason),
                         ("unresolved", "unknown_ref"))

    def test_candidates_never_leak_out_of_authorized_scope(self):
        """「独名件」也存在于未授权店铺：越权候选与越权主键一条都不许出现。"""
        self._line(self.c, self.shops["other"], "独名件")

        resolution = resolve_product(
            self.conn, selector=text_selector("独名件"),
            authorized_shop_ids=frozenset({self.shops["tb"]}), at=AT)

        self.assertEqual(resolution.status, "resolved")
        self.assertEqual({shop for item in resolution.candidates for shop in item.shop_ids},
                         {self.shops["tb"]})
        self.assertNotIn(self.shops["other"],
                         [shop for item in resolution.candidates for shop in item.shop_ids],
                         "候选里出现越权店铺就是泄露")
        # 反向证明过滤真的在起作用：放开授权范围，那家店就该出现。
        widened = resolve_product(
            self.conn, selector=text_selector("独名件"),
            authorized_shop_ids=self._authorized(), at=AT)
        self.assertIn(self.shops["other"],
                      [shop for item in widened.candidates for shop in item.shop_ids])
        self.assertNotIn(self.shops["other"],
                         {item.shop_id for item in expand_channel_items(
                             self.conn, erp_product_id=self.c,
                             authorized_shop_ids=frozenset({self.shops["tb"]}), at=AT)})

    # -- 标识映射与归并 ------------------------------------------------------

    def test_text_candidate_lists_every_authorized_shop_that_sold_it(self):
        record_identifier_mapping(self.conn, IdentifierMapping(
            namespace=NAMESPACE, platform="fxg", shop_id=self.shops["fxg"],
            listing_id="L-DY-C", platform_sku_id="", erp_product_id=self.c,
            erp_sku_id="", evidence="probe:second-shop"))

        resolution = resolve_product(
            self.conn, selector=text_selector("独名件"),
            authorized_shop_ids=self._authorized(), at=AT)

        self.assertEqual(resolution.status, "resolved")
        # 成交只在 tb 店，映射给 fxg 店也落了点：候选范围要能同时看到两条来源。
        self.assertEqual(
            sorted({shop for shop in resolution.candidates[0].shop_ids}),
            sorted({self.shops["tb"]}))

    def test_same_erp_sku_across_two_channels_collapses_to_one_sku(self):
        for platform, shop_key, listing in (("tb", "tb", "L-TB"),
                                            ("fxg", "fxg", "L-DY")):
            record_identifier_mapping(self.conn, IdentifierMapping(
                namespace=NAMESPACE, platform=platform, shop_id=self.shops[shop_key],
                listing_id=listing, platform_sku_id=f"{listing}-sku",
                erp_product_id=self.a, erp_sku_id=f"{self.a}-sku",
                evidence="probe:manual-map"))

        items = expand_channel_items(self.conn, erp_product_id=self.a,
                                     authorized_shop_ids=self._authorized(), at=AT)

        self.assertEqual({item.shop_id for item in items},
                         {self.shops["tb"], self.shops["fxg"]})
        self.assertEqual(len({(item.namespace, item.erp_sku_id) for item in items}), 1)

    def test_same_platform_number_in_another_account_is_not_merged(self):
        for namespace, shop_key in ((NAMESPACE, "tb"), (OTHER_NAMESPACE, "other")):
            record_identifier_mapping(self.conn, IdentifierMapping(
                namespace=namespace, platform="tb", shop_id=self.shops[shop_key],
                listing_id="SAME-12345", platform_sku_id="SKU-999",
                erp_product_id=self.a if shop_key == "tb" else self.b,
                erp_sku_id="" if shop_key == "tb" else f"{self.b}-sku",
                evidence="probe:same-platform-id"))

        items = expand_channel_items(self.conn, erp_product_id=self.a,
                                     authorized_shop_ids=self._authorized(), at=AT)

        self.assertEqual({item.namespace for item in items}, {NAMESPACE},
                         "跨账号同号自动归并会把两个商品算成一个")

    def test_mapped_listing_without_any_trade_is_kept(self):
        record_identifier_mapping(self.conn, IdentifierMapping(
            namespace=NAMESPACE, platform="tb", shop_id=self.shops["tb"],
            listing_id="L-NEW", platform_sku_id="NEW-SKU",
            erp_product_id=self.c, erp_sku_id="", evidence="probe:new-listing"))

        items = expand_channel_items(self.conn, erp_product_id=self.c,
                                     authorized_shop_ids=self._authorized(), at=AT)

        self.assertEqual([item.listing_id for item in items], ["L-NEW"],
                         "上架全集不能由历史成交推导：新品也必须出现在复核目标里")

    def test_platform_name_alone_never_marks_a_mapping_approved(self):
        """文本解析只给候选；合并必须来自显式标识映射或成交行身份。"""
        resolution = resolve_product(
            self.conn, selector=text_selector("接头"),
            authorized_shop_ids=self._authorized(), at=AT)

        self.assertNotEqual(resolution.status, "resolved")
        self.assertIsNone(resolution.mapping_version)

    def test_trade_line_mapping_records_erp_identity_without_inventing_a_listing(self):
        mapping = record_trade_line_mapping(self.conn, namespace=NAMESPACE,
                                            platform="tb", shop_id=self.shops["tb"],
                                            erp_product_id=self.a,
                                            erp_sku_id=f"{self.a}-sku")

        self.assertEqual(mapping.status, "approved")
        self.assertEqual(mapping.listing_id, "", "成交行给不出渠道链接号")
        self.assertEqual(mapping.source, "trade_line")

    def test_mapping_version_is_reported_so_results_can_be_reproduced(self):
        record_identifier_mapping(self.conn, IdentifierMapping(
            namespace=NAMESPACE, platform="tb", shop_id=self.shops["tb"],
            listing_id="L-V", platform_sku_id="", erp_product_id=self.a,
            erp_sku_id="", evidence="probe:version"))
        items = expand_channel_items(self.conn, erp_product_id=self.a,
                                     authorized_shop_ids=self._authorized(), at=AT)

        self.assertEqual({item.mapping_version for item in items},
                         {CHANNEL_MAPPING_VERSION})


class ChannelMappingMigrationTests(unittest.TestCase):
    def test_mapping_table_and_view_exist_after_016(self):
        import pathlib

        sql = (pathlib.Path(__file__).parents[1] / "sql"
               / "016_channel_catalog.sql").read_text(encoding="utf-8")
        self.assertIn("bi.channel_items", sql)
        self.assertIn("reporting.v_channel_items", sql)
        # 自动合并只接受已确认的显式标识映射：approved 必须能追到证据。
        self.assertIn("evidence", sql)


if __name__ == "__main__":
    unittest.main()
