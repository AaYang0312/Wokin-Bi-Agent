"""语义目录契约与登记表测试（计划 Task 1 + Task 2 + Task 3 + Task 4）。

契约的形状就是它的价值所在：这些类型是后续注册表、检索与受控 SQL 唯一的
词汇来源，所以"什么算一个合法 ref / 合法版本标识 / 合法基数"必须在这一层
一次定死，而不是等某个调用方顺手放宽。

Task 2 的部分再加一条：登记内容必须**闭包**（没有悬空/错归属/放大的边），而且
登记的列名要和已应用迁移的真实 schema 一致——后者是一条连本机测试库的对账，
缺 DSN 时按仓内现行约定正常 skip（不是 error）。

Task 3 的部分：检索必须可复现（同输入同输出、子句换序不变）、可回收（30 题
gold set 的 Top 5 召回 100%）、不泄密（输出里没有任何 SQL 标识符或真实 ID），
而且答不了时必须说答不了。

Task 4 的部分：启动预检必须只看元数据、只发计划钉下的那一条查询、不一致时给出
一个稳定的原因码（不带 SQL 标识符与库内原文），并且在只读角色下证明它没有扩大
业务事实权限。
"""

from dataclasses import FrozenInstanceError
import copy
import dataclasses
import json
import os
import pathlib
import re
import unittest

import psycopg

from pydantic import ValidationError

REF_RE = r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$"


def version_set(**overrides):
    from bi_agent.runtime.versions import VersionSet

    values = {
        "schema_version": "reporting/2026-09-14.1",
        "semantic_catalog_version": "semantic/2026-09-14.1",
        "data_catalog_version": 7,
        "metric_version": "metrics/2026-09-12.1",
        "policy_version": "multi-source-policy/2026-09-12.1",
        "source_registry_version": "sources/2026-09-12.1",
        "graph_version": "business_query-graph/2026-09-11.1",
    }
    values.update(overrides)
    return VersionSet(**values)


def entity(**overrides):
    from bi_agent.semantic_catalog import SemanticEntity

    values = {
        "ref": "entity-shop",
        "domains": ("business_query",),
        "aliases": ("店铺",),
        "description": "一家已授权的店铺",
    }
    values.update(overrides)
    return SemanticEntity(**values)


def field(**overrides):
    from bi_agent.semantic_catalog import SemanticField

    values = {
        "ref": "field-paid-amount",
        "view_ref": "view-shop-daily",
        "column": "paid_amount",
        "data_type": "decimal",
        "role": "measure",
        "aliases": ("支付金额",),
    }
    values.update(overrides)
    return SemanticField(**values)


def metric(**overrides):
    from bi_agent.semantic_catalog import SemanticMetric

    values = {
        "ref": "metric-paid-amount",
        "domains": ("business_query",),
        "aliases": ("支付金额",),
        "required_field_refs": ("field-paid-amount",),
        "allowed_aggregates": ("sum",),
        "default_aggregate": "sum",
        "basis_required": True,
    }
    values.update(overrides)
    return SemanticMetric(**values)


def view(**overrides):
    from bi_agent.semantic_catalog import SemanticView

    values = {
        "ref": "view-shop-daily",
        "schema": "reporting",
        "name": "v_shop_daily",
        "entity_ref": "entity-shop",
        "grain": ("shop", "day"),
        "authorization_field_ref": "field-shop-id",
        "field_refs": ("field-paid-amount",),
        "domains": ("business_query",),
    }
    values.update(overrides)
    return SemanticView(**values)


def join(**overrides):
    from bi_agent.semantic_catalog import SemanticJoin

    values = {
        "ref": "join-shop-daily-shops",
        "left_view_ref": "view-shop-daily",
        "right_view_ref": "view-shops",
        "left_field_ref": "field-shop-id",
        "right_field_ref": "field-shop-id",
        "cardinality": "many_to_one",
        "allowed_group_grains": ("shop",),
        "anti_amplification": "右视图每个 shop_id 只有一行，聚合前不得再 JOIN 一次",
    }
    values.update(overrides)
    return SemanticJoin(**values)


def catalog(**overrides):
    from bi_agent.semantic_catalog import SemanticCatalog

    values = {
        "version": "semantic/2026-09-14.1",
        "entities": (entity(),),
        "fields": (field(),),
        "metrics": (metric(),),
        "views": (view(),),
        "joins": (join(),),
    }
    values.update(overrides)
    return SemanticCatalog(**values)


def selection(**overrides):
    from bi_agent.semantic_catalog import SemanticSelection

    values = {
        "catalog_version": "semantic/2026-09-14.1",
        "entity_refs": ("entity-shop",),
        "metric_refs": ("metric-paid-amount",),
        "view_refs": ("view-shop-daily",),
        "field_refs": ("field-paid-amount",),
        "join_path_refs": ("join-shop-daily-shops",),
        "missing_concepts": (),
        "requires_clarification": False,
    }
    values.update(overrides)
    return SemanticSelection(**values)


# 每一个 ref / refs 字段都列在这里：加一个新 ref 字段而不走同一条规则，就是测试红。
# （版本标识另有规则，见 test_catalog_rejects_a_ref_shaped_version_and_duplicate_item_refs。）
REF_BEARING = (
    ("entity", "ref", lambda **kw: entity(**kw)),
    ("field", "ref", lambda **kw: field(**kw)),
    ("field", "view_ref", lambda **kw: field(**kw)),
    ("metric", "ref", lambda **kw: metric(**kw)),
    ("metric", "required_field_refs", lambda **kw: metric(**kw)),
    ("view", "ref", lambda **kw: view(**kw)),
    ("view", "entity_ref", lambda **kw: view(**kw)),
    ("view", "authorization_field_ref", lambda **kw: view(**kw)),
    ("view", "field_refs", lambda **kw: view(**kw)),
    ("join", "ref", lambda **kw: join(**kw)),
    ("join", "left_view_ref", lambda **kw: join(**kw)),
    ("join", "right_view_ref", lambda **kw: join(**kw)),
    ("join", "left_field_ref", lambda **kw: join(**kw)),
    ("join", "right_field_ref", lambda **kw: join(**kw)),
    ("selection", "entity_refs", lambda **kw: selection(**kw)),
    ("selection", "metric_refs", lambda **kw: selection(**kw)),
    ("selection", "view_refs", lambda **kw: selection(**kw)),
    ("selection", "field_refs", lambda **kw: selection(**kw)),
    ("selection", "join_path_refs", lambda **kw: selection(**kw)),
)

BAD_REFS = (
    "field-drop table",          # 空格：可断言的假绿来源
    "field-paid\n",              # 尾随换行：不得靠“局部匹配”混过去
    "field-paid ",
    "reporting.v_shop_daily",    # 原始 SQL 标识符不是 ref
    "v_shop_daily",              # 下划线形状不是 ref
    "View-Shop-Daily",           # 大写：同一概念只允许一种拼法
    "shop_id",
    "",
    "  ",
    "field-;",
    None,
    7,
)


class SemanticContractTests(unittest.TestCase):
    def test_versions_and_refs_reject_free_text(self):
        from bi_agent.runtime.versions import VersionSet
        from bi_agent.semantic_catalog.models import SemanticField

        versions = VersionSet(
            schema_version="reporting/2026-09-14.1",
            semantic_catalog_version="semantic/2026-09-14.1",
            data_catalog_version=7,
            metric_version="metrics/2026-09-12.1",
            policy_version="multi-source-policy/2026-09-12.1",
            source_registry_version="sources/2026-09-12.1",
            graph_version="business_query-graph/2026-09-11.1",
        )
        self.assertEqual(versions.data_catalog_version, 7)
        with self.assertRaises(ValidationError):
            SemanticField(
                ref="field-drop table",
                view_ref="view-shop-daily",
                column="paid_amount",
                data_type="decimal",
                role="measure",
                aliases=("销售额",),
            )

    def test_version_set_accepts_the_current_registry_versions(self):
        versions = version_set()
        self.assertEqual(versions.schema_version, "reporting/2026-09-14.1")
        self.assertEqual(versions.semantic_catalog_version, "semantic/2026-09-14.1")
        self.assertEqual(versions.data_catalog_version, 7)

    def test_version_set_rejects_free_text_negative_and_unknown_fields(self):
        for bad in ("", "  ", " reporting/2026-09-14.1", "reporting / x",
                    "a\nb", "not a version"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValidationError):
                    version_set(schema_version=bad)
        with self.assertRaises(ValidationError):
            version_set(semantic_catalog_version="semantic; DROP TABLE bi.orders")
        with self.assertRaises(ValidationError):
            version_set(data_catalog_version=-1)
        with self.assertRaises(ValidationError):
            version_set(registry_version="sources/2026-09-12.1")   # extra="forbid"

    def test_contracts_are_immutable(self):
        with self.assertRaises(ValidationError):
            version_set().graph_version = "business_query-graph/2026-09-12.1"
        with self.assertRaises(FrozenInstanceError):
            field().column = "paid_amount_v2"          # type: ignore[misc]
        with self.assertRaises(FrozenInstanceError):
            selection().requires_clarification = True  # type: ignore[misc]

    def test_every_ref_field_uses_the_one_ref_rule(self):
        for contract, attribute, build in REF_BEARING:
            for bad in BAD_REFS:
                with self.subTest(contract=contract, attribute=attribute, bad=bad):
                    value = (bad,) if attribute.endswith("_refs") else bad
                    with self.assertRaises(ValidationError):
                        build(**{attribute: value})

    def test_catalog_rejects_free_text_version_and_duplicate_item_refs(self):
        # 版本标识只拦空白与自由文本：`reporting/2026-09-14.1` 这种带斜杠/点号的写法
        # 是仓内现行形状（`metrics/2026-09-12.1`），不得当成 ref 去要求 kebab-case。
        self.assertEqual(
            catalog(version="semantic/2026-09-15.1").version, "semantic/2026-09-15.1")
        for bad in ("", "  ", "semantic 2026-09-14.1",
                    "semantic; DROP TABLE bi.orders", "a\nb", None, 7):
            with self.subTest(bad_version=bad):
                with self.assertRaises(ValidationError):
                    catalog(version=bad)
        with self.assertRaises(ValidationError):
            catalog(entities=(entity(), entity()))
        with self.assertRaises(ValidationError):
            catalog(fields=(field(), field(ref="field-paid-amount")))
        # 集合形状本身也是契约：目录不能装一个 dict 进去。
        with self.assertRaises(ValidationError):
            catalog(entities=[entity()])
        with self.assertRaises(ValidationError):
            catalog(entities=("entity-shop",))

    def test_domains_are_non_empty_stable_codes(self):
        # domain 为空的条目永远检索不到：那叫静默失效，不叫可选字段。
        for factory in (entity, metric, view):
            with self.subTest(contract=factory.__name__):
                with self.assertRaises(ValidationError):
                    factory(domains=())
                with self.assertRaises(ValidationError):
                    factory(domains=("Business Query",))
                with self.assertRaises(ValidationError):
                    factory(domains=("business query",))

    def test_views_are_reporting_only_and_identifiers_are_bare(self):
        self.assertEqual(view().schema, "reporting")
        for bad_schema in ("bi", "public", "REPORTING", "", None):
            with self.subTest(bad_schema=bad_schema):
                with self.assertRaises(ValidationError):
                    view(schema=bad_schema)
        for bad_name in ("public.v_shop_daily", "v shop daily", "V_SHOP_DAILY",
                         "v_shop_daily;", "v_shop_daily\n", ""):
            with self.subTest(bad_name=bad_name):
                with self.assertRaises(ValidationError):
                    view(name=bad_name)
        for bad_column in ("paid;amount", "paid amount", "a.b", "PAID",
                            "paid_amount\n", "paid_amount ", ""):
            with self.subTest(bad_column=bad_column):
                with self.assertRaises(ValidationError):
                    field(column=bad_column)

    def test_literal_vocabularies_are_closed(self):
        for bad in ("money", "DECIMAL", "text;", "", None):
            with self.subTest(bad_type=bad):
                with self.assertRaises(ValidationError):
                    field(data_type=bad)
        for bad in ("metric", "auth", "", None):
            with self.subTest(bad_role=bad):
                with self.assertRaises(ValidationError):
                    field(role=bad)
        # one_to_many 会在 JOIN 时放大金额：它根本不在词表里。
        for bad in ("one_to_many", "many_to_many", "", None):
            with self.subTest(bad_cardinality=bad):
                with self.assertRaises(ValidationError):
                    join(cardinality=bad)
        for good in ("one_to_one", "many_to_one"):
            with self.subTest(good=good):
                self.assertEqual(join(cardinality=good).cardinality, good)
        for bad in ("median", "SUM", "", None):
            with self.subTest(bad_aggregate=bad):
                with self.assertRaises(ValidationError):
                    metric(allowed_aggregates=("sum",), default_aggregate=bad)
        with self.assertRaises(ValidationError):
            metric(allowed_aggregates=("median",), default_aggregate="median")

    def test_default_aggregate_must_be_one_of_the_allowed_aggregates(self):
        with self.assertRaises(ValidationError):
            metric(allowed_aggregates=("sum", "count"), default_aggregate="avg")
        self.assertEqual(
            metric(allowed_aggregates=("sum", "count"), default_aggregate="count")
            .default_aggregate, "count")

    def test_collections_must_be_unique_non_blank_tuples(self):
        for bad in ("店铺", ["店铺"], None, 7):
            with self.subTest(bad_aliases=bad):
                with self.assertRaises(ValidationError):
                    entity(aliases=bad)
        with self.assertRaises(ValidationError):
            entity(aliases=("店铺", "店铺"))
        with self.assertRaises(ValidationError):
            entity(aliases=("店铺", "  "))
        with self.assertRaises(ValidationError):
            view(grain=["shop", "day"])
        with self.assertRaises(ValidationError):
            view(grain=("shop", "shop"))
        with self.assertRaises(ValidationError):
            view(field_refs=("field-paid-amount", "field-paid-amount"))
        with self.assertRaises(ValidationError):
            metric(allowed_aggregates=("sum", "sum"))
        # 别名可以为空（内部字段不需要对模型的叫法），但不能有空白项。
        self.assertEqual(field(aliases=()).aliases, ())

    def test_text_and_flag_fields_are_strictly_typed(self):
        for bad in ("", "   ", None, 7):
            with self.subTest(bad_description=bad):
                with self.assertRaises(ValidationError):
                    entity(description=bad)
            with self.subTest(bad_anti_amplification=bad):
                with self.assertRaises(ValidationError):
                    join(anti_amplification=bad)
        for bad in ("true", 1, None, 0):
            with self.subTest(bad_flag=bad):
                with self.assertRaises(ValidationError):
                    metric(basis_required=bad)
                with self.assertRaises(ValidationError):
                    selection(requires_clarification=bad)

    def test_missing_concepts_are_stable_codes_not_raw_tokens(self):
        # 概念码允许下划线（`profit_grain`），这与 ref 的词表故意不同：
        # 缺失概念要回给模型当澄清线索，但它绝不是可以解析成 SQL 的 ref。
        result = selection(missing_concepts=("profit_grain", "inventory_grain"),
                           requires_clarification=True, join_path_refs=())
        self.assertEqual(result.missing_concepts, ("profit_grain", "inventory_grain"))
        for bad in ("", "  ", "净 利润", None, 7):
            with self.subTest(bad_concept=bad):
                with self.assertRaises(ValidationError):
                    selection(missing_concepts=(bad,))
        with self.assertRaises(ValidationError):
            selection(missing_concepts=("attribution", "attribution"))

    def test_selection_can_be_empty_but_still_carries_its_catalog_version(self):
        result = selection(entity_refs=(), metric_refs=(), view_refs=(),
                           field_refs=(), join_path_refs=())
        self.assertEqual(result.view_refs, ())
        self.assertEqual(result.catalog_version, "semantic/2026-09-14.1")
        with self.assertRaises(ValidationError):
            selection(catalog_version="")

    def test_package_surface_is_exactly_the_published_contracts(self):
        import bi_agent.semantic_catalog as package

        self.assertEqual(
            sorted(package.__all__),
            sorted([
                "SemanticCatalog", "SemanticEntity", "SemanticField", "SemanticJoin",
                "SemanticMetric", "SemanticSelection", "SemanticView",
                # Task 2 的登记表与解析入口（计划 Task 2 Step 2 修改本文件）。
                "CATALOG", "CATALOGS_BY_VERSION", "SEMANTIC_CATALOG_VERSION",
                "catalog_for_version", "catalog_indexes", "resolve_sql_identifier",
                "validate_catalog",
                # Task 3 的确定性检索入口（计划 Task 3 Step 3 导出）。
                "normalize_terms", "retrieve_schema_candidates",
            ]))
        # Task 4 的启动预检只从 `bi_agent.semantic_catalog.schema_check` 模块限定导入，
        # feature gate 只是 `AppSettings` 的字段：包表面（=模型可见的语义词汇）因此
        # 一个名字都不许多加，下面三条仍然是红线。
        self.assertFalse(hasattr(package, "validate_catalog_schema"))
        self.assertFalse(hasattr(package, "SchemaMismatch"))
        self.assertFalse(hasattr(package, "semantic_catalog_enabled"))
        from bi_agent.semantic_catalog.models import REF_RE as ref_pattern

        self.assertEqual(ref_pattern, REF_RE)


# --- Task 2：登记表内容与闭包校验 -----------------------------------------------

# 计划表格里逐条批准的 reporting 视图：ref → 真实视图名。这里独立转录一份，
# 是为了让"注册了什么"由两处分别写出来再比对，而不是让实现自己宣布自己正确。
APPROVED_VIEWS = {
    "view-shops": "v_shops",
    "view-shop-daily": "v_shop_daily",
    "view-product-daily": "v_product_daily",
    "view-product-cost-daily": "v_product_cost_daily",
    "view-erp-document-daily": "v_erp_document_daily",
    "view-payments": "v_payments",
    "view-refunds": "v_refunds",
    "view-coverage": "v_coverage",
    "view-listing-items": "v_listing_snapshot_items",
    "view-physical-stock-items": "v_physical_stock_items",
    "view-channel-stock-items": "v_channel_stock_items",
}

# 计划 Task 3 的冲突表与总设计 §5.1/§6.3 点名的指标 ref：首批目录必须包含它们。
REQUIRED_METRICS = (
    "metric-paid-amount",
    "metric-product-gross-profit-reference",
    "metric-erp-gross-profit-reference",
    "metric-physical-available-quantity",
    "metric-channel-sellable-quantity",
    "metric-transaction-average-price",
    "metric-listing-price",
)


def closed_catalog(version="semantic/2026-09-14.1"):
    """一份**闭包干净**的最小目录：validate_catalog 的负向用例以它为底。"""
    return catalog(
        version=version,
        entities=(entity(),),
        fields=(
            field(ref="field-shop-daily-shop-id", view_ref="view-shop-daily",
                  column="shop_id", data_type="ref", role="authorization"),
            field(ref="field-shop-daily-paid-amount", view_ref="view-shop-daily",
                  column="paid_amount"),
            field(ref="field-shops-shop-id", view_ref="view-shops",
                  column="shop_id", data_type="ref", role="authorization"),
        ),
        metrics=(metric(ref="metric-paid-amount",
                        required_field_refs=("field-shop-daily-paid-amount",)),),
        views=(
            view(ref="view-shop-daily",
                 grain=("shop", "day", "currency"),
                 authorization_field_ref="field-shop-daily-shop-id",
                 field_refs=("field-shop-daily-shop-id", "field-shop-daily-paid-amount")),
            view(ref="view-shops", name="v_shops", entity_ref="entity-shop",
                 grain=("shop",), authorization_field_ref="field-shops-shop-id",
                 field_refs=("field-shops-shop-id",)),
        ),
        joins=(join(left_view_ref="view-shop-daily", right_view_ref="view-shops",
                    left_field_ref="field-shop-daily-shop-id",
                    right_field_ref="field-shops-shop-id"),),
    )


def patched(item, **overrides):
    """绕过冻结契约直接改字段：只用于证明 `validate_catalog` 的兑底规则不是死代码。"""
    clone = copy.copy(item)
    for name, value in overrides.items():
        object.__setattr__(clone, name, value)
    return clone


def replaced_item(base, kind, index=0, **overrides):
    items = list(getattr(base, kind))
    items[index] = dataclasses.replace(items[index], **overrides)
    return tuple(items)


def _platform_join_catalog():
    """闭包干净、但 JOIN 右键用了一个非授权列的目录：专门给授权列规则用。"""
    base = closed_catalog()
    platform = field(ref="field-shops-platform", view_ref="view-shops",
                     column="platform", data_type="text", role="dimension")
    views = (base.views[0], dataclasses.replace(
        base.views[1], field_refs=("field-shops-shop-id", platform.ref)))
    joins = replaced_item(
        dataclasses.replace(base, views=views), "joins", 0,
        right_field_ref=platform.ref)
    return dataclasses.replace(base, fields=base.fields + (platform,),
                               views=views, joins=joins)


class SemanticRegistryTests(unittest.TestCase):
    def test_registry_contains_only_reporting_views_and_closed_refs(self):
        from bi_agent.semantic_catalog.registry import CATALOG, catalog_indexes

        indexes = catalog_indexes(CATALOG)
        self.assertGreaterEqual(len(CATALOG.views), 8)
        self.assertTrue(all(view.schema == "reporting" for view in CATALOG.views))
        self.assertNotIn("bi", {view.schema for view in CATALOG.views})
        for view_entry in CATALOG.views:
            self.assertIn(view_entry.authorization_field_ref, indexes.fields)
            self.assertTrue(set(view_entry.field_refs) <= set(indexes.fields))
        for join_entry in CATALOG.joins:
            self.assertIn(join_entry.cardinality, {"one_to_one", "many_to_one"})

    def test_model_facing_selection_never_resolves_to_sql_identifiers(self):
        from bi_agent.semantic_catalog.registry import CATALOG, resolve_sql_identifier

        self.assertEqual(
            resolve_sql_identifier(CATALOG, "view-shop-daily"),
            ("reporting", "v_shop_daily"),
        )
        with self.assertRaises(KeyError):
            resolve_sql_identifier(CATALOG, "reporting.v_shop_daily")

    def test_registered_views_are_exactly_the_approved_reporting_set(self):
        from bi_agent.semantic_catalog.registry import CATALOG

        self.assertEqual({view_entry.ref: view_entry.name for view_entry in CATALOG.views},
                         APPROVED_VIEWS)
        self.assertTrue(all(view_entry.schema == "reporting" for view_entry in CATALOG.views))

    def test_repeated_sql_columns_are_view_scoped_with_one_owner_each(self):
        """计划表格的 `field-shop-id` 简写不能照抄：一个 ref 只能解析到一个视图的列。

        如果十个视图共用一个 ref，按 ref 建的索引会静默保留最后一个，其余视图的
        授权字段就解到了别的表上——那是能发错金额的缺陷，不是命名偏好。
        """
        from bi_agent.semantic_catalog.registry import (CATALOG, catalog_indexes,
                                                        resolve_sql_identifier)

        indexes = catalog_indexes(CATALOG)
        shop_id_fields = sorted((f for f in CATALOG.fields if f.column == "shop_id"),
                                key=lambda item: item.ref)
        self.assertEqual(len(shop_id_fields), 10)          # 10 个视图以 shop_id 授权
        self.assertEqual({f.ref for f in shop_id_fields},
                         set(indexes.fields) & {f.ref for f in shop_id_fields})
        for field_entry in shop_id_fields:
            owner = indexes.views[field_entry.view_ref]
            self.assertIn(field_entry.ref, owner.field_refs)
            self.assertEqual(owner.authorization_field_ref, field_entry.ref)
            self.assertEqual(field_entry.role, "authorization")
            # 每个 ref 只解到**自己那张视图**的列。
            self.assertEqual(
                resolve_sql_identifier(CATALOG, field_entry.ref),
                ("reporting", owner.name, "shop_id"))
        # 实物库存按库存池授权，不拿着 shop_id 当授权列。
        physical = indexes.views["view-physical-stock-items"]
        self.assertEqual(
            physical.authorization_field_ref, "field-physical-stock-items-pool-id")
        self.assertEqual(
            resolve_sql_identifier(CATALOG, physical.authorization_field_ref),
            ("reporting", "v_physical_stock_items", "pool_id"))
        self.assertNotIn("field-shop-id", set(indexes.fields))

    def test_only_the_four_approved_shop_joins_are_registered(self):
        from bi_agent.semantic_catalog.registry import CATALOG, catalog_indexes

        indexes = catalog_indexes(CATALOG)
        edges = {(j.left_view_ref, j.right_view_ref) for j in CATALOG.joins}
        self.assertEqual(edges, {
            ("view-shop-daily", "view-shops"),
            ("view-product-daily", "view-shops"),
            ("view-product-cost-daily", "view-shops"),
            ("view-erp-document-daily", "view-shops"),
        })
        for join_entry in CATALOG.joins:
            self.assertEqual(join_entry.cardinality, "many_to_one")
            left = indexes.views[join_entry.left_view_ref]
            right = indexes.views[join_entry.right_view_ref]
            self.assertEqual(left.authorization_field_ref, join_entry.left_field_ref)
            self.assertEqual(right.authorization_field_ref, join_entry.right_field_ref)
            self.assertTrue(join_entry.anti_amplification.strip())
        # 三组故意不登记的边：它们会把金额/库存乘倍或把两种口径当同一件事。
        edges = {frozenset((j.left_view_ref, j.right_view_ref)) for j in CATALOG.joins}
        forbidden = {
            frozenset({"view-payments", "view-refunds"}),
            frozenset({"view-product-cost-daily", "view-erp-document-daily"}),
            frozenset({"view-physical-stock-items", "view-channel-stock-items"}),
        }
        self.assertFalse(edges & forbidden, str(edges & forbidden))

    def test_resolution_refuses_every_ref_kind_that_is_not_a_view_or_field(self):
        from bi_agent.semantic_catalog import registry

        resolve = registry.resolve_sql_identifier
        for ref in ("entity-shop", "metric-paid-amount", "join-shop-daily-shops",
                    "reporting.v_shops", "bi.orders", "v_shops", "", "not-a-ref"):
            with self.subTest(ref=ref):
                with self.assertRaises(KeyError):
                    resolve(registry.CATALOG, ref)
        # 解析失败不得把调用方传来的字符串原样回显到日志里。
        try:
            resolve(registry.CATALOG, "reporting.v_shops; DROP TABLE x")
        except KeyError as exc:
            self.assertNotIn("DROP", str(exc))

    def test_version_index_is_immutable_and_history_cannot_answer_new_versions(self):
        from bi_agent.semantic_catalog import registry

        self.assertEqual(registry.catalog_for_version(registry.SEMANTIC_CATALOG_VERSION),
                         registry.CATALOG)
        self.assertIsNone(registry.catalog_for_version("semantic/1999-01-01.1"))
        with self.assertRaises(TypeError):
            registry.CATALOGS_BY_VERSION["semantic/2099-01-01.1"] = registry.CATALOG
        with self.assertRaises(TypeError):
            registry.catalog_indexes(registry.CATALOG).views["view-shops"] = (
                registry.CATALOG.views[0])
        # 导入时已经跑过一次闭包校验（计划 Step 4）：这里只断言它成功且可重入。
        registry.validate_catalog(registry.CATALOG)

    def test_registered_grain_columns_are_present_and_array_columns_are_not_registered(self):
        """列名逐字来自定义它的迁移；数组/multirange 列首批不登记（见模块注释）。"""
        from bi_agent.semantic_catalog import registry

        resolve = registry.resolve_sql_identifier
        refs = {f.ref for f in registry.CATALOG.fields}
        self.assertEqual(
            resolve(registry.CATALOG, "field-shop-daily-cash-difference"),
            ("reporting", "v_shop_daily", "cash_difference"))
        self.assertEqual(
            resolve(registry.CATALOG, "field-product-cost-daily-cost-total"),
            ("reporting", "v_product_cost_daily", "cost_total"))
        self.assertEqual(
            resolve(registry.CATALOG, "field-channel-stock-items-sellable-quantity"),
            ("reporting", "v_channel_stock_items", "sellable_quantity"))
        # 计划表格提到的两个非标量列：词表里没有 array/multirange，伪装成 text 会让
        # Task 4 的启动校验在真实 schema 上永久失败，所以它们不进入目录。
        self.assertNotIn("field-shops-capabilities", refs)
        self.assertNotIn("field-coverage-covered", refs)
        self.assertNotIn("field-product-cost-daily-sku-ids", refs)

    def test_metric_required_fields_live_in_one_view_and_are_registered(self):
        from bi_agent.semantic_catalog import registry

        indexes = registry.catalog_indexes(registry.CATALOG)
        # 计划与总设计点名的 ref 必须存在（Task 3 的冲突表靠它们）；首批还可以
        # 给已批准量度列登记更多指标，但不得漏掉上面这份名单。
        self.assertTrue(set(REQUIRED_METRICS) <= {m.ref for m in registry.CATALOG.metrics})
        for metric_entry in registry.CATALOG.metrics:
            self.assertTrue(metric_entry.required_field_refs)
            owners = {indexes.fields[ref].view_ref
                      for ref in metric_entry.required_field_refs}
            self.assertEqual(len(owners), 1, metric_entry.ref)
            owner = indexes.views[next(iter(owners))]
            self.assertTrue(set(metric_entry.required_field_refs) <= set(owner.field_refs))
            self.assertTrue(set(owner.domains) & set(metric_entry.domains),
                            f"{metric_entry.ref} 没有它所在视图的 domain")

    def test_domains_are_the_registered_runtime_domains(self):
        """不发明第二套领域词表：目录里的每个 domain 必须是 runtime 已注册的那个。"""
        from bi_agent.runtime.domain_registry import known_domain
        from bi_agent.semantic_catalog import registry

        used = set()
        for items in (registry.CATALOG.entities, registry.CATALOG.metrics,
                      registry.CATALOG.views):
            for item in items:
                used.update(item.domains)
        self.assertTrue(used)
        for domain in sorted(used):
            with self.subTest(domain=domain):
                self.assertTrue(known_domain(domain))

    def test_module_validates_the_catalog_at_import(self):
        """计划 Step 4：目录不闭包时应用连启动都不该起来。

        这条用 AST 顶定：只删掉那一行调用就变红，不会变成“没人调的校验函数”。
        """
        import ast
        import inspect

        import bi_agent.semantic_catalog.registry as registry_module

        tree = ast.parse(inspect.getsource(registry_module))
        guards = [
            node for node in tree.body
            if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call)
            and getattr(node.value.func, "id", None) == "validate_catalog"
            and [getattr(arg, "id", None) for arg in node.value.args] == ["CATALOG"]
        ]
        self.assertEqual(len(guards), 1)

    def test_first_release_catalog_cannot_express_an_erp_outstock_amount(self):
        """首批目录里没有任何“ERP 出库金额”列：出库口径只在现有固定 Tool 里算。

        把 `paid_amount` 当成两种口径通用，会让检索“自信地”返回错视图；这里把
        “没有这种列”钉成断言，Task 3 只能回 `schema_ambiguous` / 缺失概念。
        """
        from bi_agent.semantic_catalog import registry

        columns = {f.column for f in registry.CATALOG.fields}
        self.assertFalse([c for c in columns if "outstock" in c or "ship_amount" in c])
        # 量度侧（指标与 measure 字段）不许出现"出库"字样：目录里没有任何出库金额列，
        # 一个这样的别名会让检索自信地把"按出库口径看销售额"送回支付列——那正是本仓
        # 花了一个任务分开的两种口径。实体侧保留"出库单"叫法（ERP 单据确实是出库单，
        # 而它没有任何金额列），所以这条只管量度。
        offenders = [
            f"{entry.ref}:{alias}"
            for entry in list(registry.CATALOG.metrics)
            + [f for f in registry.CATALOG.fields if f.role == "measure"]
            for alias in entry.aliases
            if "出库" in alias or "outstock" in alias.lower()
        ]
        self.assertEqual(offenders, [])
        for entry in registry.CATALOG.metrics:
            self.assertFalse(any("出库" in alias and "支付" in alias for alias in entry.aliases),
                             entry.ref)
        # 两种口径下会算出不同数的金额指标必须显式声明要口径。
        money = {m.ref: m for m in registry.CATALOG.metrics}
        for ref in ("metric-paid-amount", "metric-sales-amount", "metric-refund-amount",
                    "metric-cash-difference", "metric-transaction-average-price"):
            with self.subTest(metric=ref):
                self.assertTrue(money[ref].basis_required)

    def test_metric_aliases_are_unique_across_the_catalog(self):
        """一个词只指一个指标。

        三组“故意可冲突”的指标用的是不同的词（商品毛利 vs ERP单据毛利），所
        以不推翻这条；真正不能接受的是同一个词同时属于两个指标——那会让
        Task 3 的得分变成抛硬币，Top 5 里留哪个全看 ref 字母序。
        """
        from bi_agent.semantic_catalog import registry

        seen: dict[str, str] = {}
        for entry in registry.CATALOG.metrics:
            for alias in entry.aliases:
                self.assertNotIn(alias, seen,
                                 f"{alias} 同时属于 {seen.get(alias)} 与 {entry.ref}")
                seen[alias] = entry.ref

    def test_no_base_table_identifier_is_registered_anywhere(self):
        from bi_agent.semantic_catalog import registry

        indexes = registry.catalog_indexes(registry.CATALOG)
        tuples = []
        tuples.extend((v.schema, v.name) for v in registry.CATALOG.views)
        for ref, field_entry in indexes.fields.items():
            tuples.append(registry.resolve_sql_identifier(registry.CATALOG, ref))
        self.assertTrue(tuples)
        for parts in tuples:
            self.assertEqual(parts[0], "reporting")
        self.assertNotIn("bi", {parts[0] for parts in tuples})


class SemanticCatalogValidationTests(unittest.TestCase):
    """`validate_catalog` 的每条规则都要能单独被打断。"""

    def setUp(self):
        from bi_agent.semantic_catalog.registry import validate_catalog

        self.validate = validate_catalog

    def test_accepts_a_closed_catalog(self):
        self.assertIsNone(self.validate(closed_catalog()))

    def test_rejects_empty_collections(self):
        base = closed_catalog()
        for kind in ("entities", "fields", "metrics", "views", "joins"):
            with self.subTest(kind=kind):
                with self.assertRaisesRegex(ValueError, "empty_collection"):
                    self.validate(dataclasses.replace(base, **{kind: ()}))

    def test_rejects_wrong_collection_item_kind(self):
        # Task 1 的契约层只看“有没有 ref”；具体类型在这里判。混进去时索引会
        # 把错的种类当成对的用，报错落在 Task 3/4 的调用方身上。
        base = closed_catalog()
        for kind, intruder in (("fields", entity()), ("views", field()),
                              ("metrics", view()), ("joins", metric()),
                              ("entities", join())):
            with self.subTest(kind=kind):
                items = list(getattr(base, kind)) + [intruder]
                with self.assertRaisesRegex(ValueError, "wrong_collection_type"):
                    self.validate(dataclasses.replace(base, **{kind: tuple(items)}))

    def test_rejects_a_ref_shared_across_collections(self):
        base = closed_catalog()
        items = list(base.metrics) + [metric(ref="view-shops",
                                            required_field_refs=("field-shops-shop-id",))]
        with self.assertRaisesRegex(ValueError, "duplicate_ref"):
            self.validate(dataclasses.replace(base, metrics=tuple(items)))

    def test_rejects_padded_grain_terms_but_not_padded_aliases(self):
        # grain / allowed_group_grains 是 Task 3 的匹配键：带空格只会“永不命中”。
        # 别名是自然语言，不在这条规则里（尾部空格不影响它能不能被读到）。
        base = closed_catalog()
        for kind, index, attribute in (("views", 0, "grain"),
                                       ("joins", 0, "allowed_group_grains")):
            with self.subTest(kind=kind, attribute=attribute):
                with self.assertRaisesRegex(ValueError, "padded_term"):
                    self.validate(dataclasses.replace(
                        base, **{kind: replaced_item(base, kind, index,
                                                     **{attribute: (" shop", "day")})}))
        self.assertIsNone(self.validate(dataclasses.replace(
            base, fields=replaced_item(base, "fields", 1, aliases=(" 支付金额 ",)))))

    def test_rejects_dangling_and_misowned_refs(self):
        base = closed_catalog()
        cases = (
            (lambda: dataclasses.replace(base, fields=replaced_item(
                base, "fields", 1, view_ref="view-nope")), "dangling_view_ref"),
            (lambda: dataclasses.replace(base, views=replaced_item(
                base, "views", 0, entity_ref="entity-nope")), "dangling_entity_ref"),
            (lambda: dataclasses.replace(base, views=replaced_item(
                base, "views", 0, field_refs=("field-shop-daily-shop-id",
                                             "field-shop-daily-paid-amount",
                                             "field-nope"))), "dangling_field_ref"),
            # field_refs 里放另一个视图的列：模型看上去能拿到 paid_amount，实际解到别的表。
            (lambda: dataclasses.replace(base, views=replaced_item(
                base, "views", 1,
                field_refs=("field-shops-shop-id", "field-shop-daily-paid-amount"),
            )), "misowned_field"),
            # 字段没被它的视图列入：永远检索不到的死目录项。
            (lambda: dataclasses.replace(base, fields=base.fields + (field(
                ref="field-shop-daily-day", view_ref="view-shop-daily", column="day",
                data_type="date", role="time"),)), "orphan_field"),
            (lambda: dataclasses.replace(base, metrics=replaced_item(
                base, "metrics", 0,
                required_field_refs=("field-nope",),
            )), "dangling_field_ref"),
            (lambda: dataclasses.replace(base, metrics=replaced_item(
                base, "metrics", 0, required_field_refs=(),
            )), "metric_without_required_fields"),
            # 一个指标需要两张互不 JOIN 的视图的列：首批目录无法表达，宁拒。
            (lambda: dataclasses.replace(base, metrics=replaced_item(
                base, "metrics", 0,
                required_field_refs=("field-shop-daily-paid-amount",
                                     "field-shops-shop-id"))), "metric_fields_span_views"),
            (lambda: dataclasses.replace(base, joins=replaced_item(
                base, "joins", 0, left_view_ref="view-nope")), "dangling_view_ref"),
            (lambda: dataclasses.replace(base, joins=replaced_item(
                base, "joins", 0, left_field_ref="field-nope")), "dangling_field_ref"),
            (lambda: dataclasses.replace(base, joins=replaced_item(
                base, "joins", 0, left_field_ref="field-shops-shop-id",
            )), "join_key_not_in_view"),
            # JOIN 键必须是两张视图各自的授权列，否则边能绕过作用域。
            (lambda: _platform_join_catalog(), "join_key_not_authorization"),
            (lambda: dataclasses.replace(base, joins=replaced_item(
                base, "joins", 0, allowed_group_grains=("warehouse",))), "grain_not_in_views"),
            (lambda: dataclasses.replace(base, joins=replaced_item(
                base, "joins", 0, left_view_ref="view-shops")), "join_same_view"),
            (lambda: dataclasses.replace(base, views=replaced_item(
                base, "views", 0, authorization_field_ref="field-shop-daily-paid-amount")),
             "authorization_role_required"),
            (lambda: dataclasses.replace(base, views=replaced_item(
                base, "views", 0, authorization_field_ref="field-shops-shop-id")),
             "authorization_field_not_in_view"),
            (lambda: dataclasses.replace(base, entities=replaced_item(
                base, "entities", 0, domains=("not_a_registered_domain",))), "unknown_domain"),
            # 一个对模型没有叫法的业务字段永远检索不到：那也是死目录项。
            (lambda: dataclasses.replace(base, fields=replaced_item(
                base, "fields", 1, aliases=())), "field_without_aliases"),
        )
        for build, reason in cases:
            with self.subTest(reason=reason):
                with self.assertRaisesRegex(ValueError, reason):
                    self.validate(build())

    def test_defensive_rules_are_live_even_if_the_contract_layer_is_bypassed(self):
        """契约层已经拦住的形状，这里再用绕过构造的对象证一遍不是死代码。"""
        base = closed_catalog()
        cases = (
            ("views", 0, patched(base.views[0], schema="bi"), "schema_not_reporting"),
            ("joins", 0, patched(base.joins[0], cardinality="one_to_many"),
             "cardinality_not_supported"),
            ("joins", 0, patched(base.joins[0], anti_amplification="   "),
             "anti_amplification_blank"),
            ("fields", 1, patched(base.fields[1], data_type="array"),
             "unrepresentable_data_type"),
        )
        for kind, index, item, reason in cases:
            with self.subTest(reason=reason):
                items = tuple(
                    item if position == index else existing
                    for position, existing in enumerate(getattr(base, kind)))
                with self.assertRaisesRegex(ValueError, reason):
                    self.validate(dataclasses.replace(base, **{kind: items}))

    def test_validate_reports_a_single_deterministic_violation(self):
        base = closed_catalog()
        broken = dataclasses.replace(base, views=replaced_item(
            base, "views", 0, field_refs=("field-shop-daily-shop-id", "field-nope"),
            entity_ref="entity-nope"))
        messages = set()
        for _ in range(5):
            with self.assertRaises(ValueError) as caught:
                self.validate(broken)
            messages.add(str(caught.exception))
        self.assertEqual(len(messages), 1)


class SemanticRegistrySchemaTests(unittest.TestCase):
    """登记内容 vs 已应用 001→019 的本机测试库：列名逐字对账。

    这是只读 introspection，不写任何东西；缺 DSN 时 skip（不是 error）。
    """

    @unittest.skipUnless(os.getenv("BI_TEST_ADMIN_DSN"), "未配置独立测试数据库")
    def test_every_registered_column_exists_with_the_declared_type_family(self):
        from tests import dbfixtures

        conn = dbfixtures.connect_test_db(self)
        rows = conn.execute(
            "SELECT table_name, column_name, data_type FROM information_schema.columns"
            " WHERE table_schema = 'reporting'").fetchall()
        actual = {(table, column): data_type for table, column, data_type in rows}

        from bi_agent.semantic_catalog import registry

        indexes = registry.catalog_indexes(registry.CATALOG)
        checked = 0
        for field_entry in registry.CATALOG.fields:
            schema, table, column = registry.resolve_sql_identifier(
                registry.CATALOG, field_entry.ref)
            self.assertEqual(schema, "reporting")
            self.assertIn((table, column), actual,
                          f"{field_entry.ref} 声明的列在测试库不存在：{table}.{column}")
            self.assertIn(actual[(table, column)],
                          registry.DATA_TYPE_SQL_FAMILIES[field_entry.data_type],
                          f"{field_entry.ref} 声明的类型族与实列不符：{actual[(table, column)]}")
            self.assertEqual(indexes.views[field_entry.view_ref].name, table)
            checked += 1
        self.assertGreaterEqual(checked, 60)
        for view_entry in registry.CATALOG.views:
            self.assertIn((view_entry.name, "shop_id") if view_entry.ref != (
                "view-physical-stock-items") else (view_entry.name, "pool_id"), actual)




# --- Task 3：确定性词项检索与 30 题 gold set -------------------------------------

GOLD_KEYS = ("clarify", "domains", "id", "join_paths", "missing_concepts", "question",
             "required_metrics", "required_views")
GOLD_FILE = "semantic_questions.jsonl"
KNOWN_DOMAINS = frozenset({"business_query", "commerce_performance", "listing_price_audit",
                           "inventory_watch"})
MISSING_CODES = frozenset({"profit_grain", "inventory_grain", "price_basis",
                           "promotion_spend", "traffic", "attribution", "net_profit"})


def gold_rows():
    """逐行读 30 题 gold set（形状由 `SemanticGoldSetTests` 先把关）。"""
    path = pathlib.Path(__file__).with_name(GOLD_FILE)
    lines = path.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()], lines


def retrieve(question, domains, *, limit=5, versions=None):
    from bi_agent.semantic_catalog import retrieve_schema_candidates

    return retrieve_schema_candidates(question, allowed_domains=frozenset(domains),
                                      current_versions=versions or version_set(),
                                      limit=limit)


def catalog_refs(catalog=None):
    from bi_agent.semantic_catalog import CATALOG, catalog_indexes

    indexes = catalog_indexes(catalog or CATALOG)
    return {kind: set(getattr(indexes, kind))
            for kind in ("entities", "fields", "metrics", "views", "joins")}


class SemanticTermTests(unittest.TestCase):
    """`normalize_terms` 是计分的入口：切块方式一变，30 题的排序就全变。"""

    def test_nfkc_lowercase_and_first_occurrence_order(self):
        from bi_agent.semantic_catalog import normalize_terms

        self.assertEqual(normalize_terms("Ａ类SKU 与 sku"), ("a", "类", "sku", "与"))

    def test_chinese_run_keeps_the_whole_segment_then_bigrams(self):
        from bi_agent.semantic_catalog import normalize_terms

        self.assertEqual(normalize_terms("支付金额"), ("支付金额", "支付", "付金", "金额"))
        self.assertEqual(normalize_terms("库存"), ("库存",))
        self.assertEqual(normalize_terms("GMV 销量"), ("gmv", "销量"))

    def test_non_string_is_refused_rather_than_stringified(self):
        from bi_agent.semantic_catalog import normalize_terms

        for value in (None, 7, ["支付金额"]):
            with self.subTest(kind=type(value).__name__):
                with self.assertRaises(ValueError):
                    normalize_terms(value)

    def test_scoring_weights_are_the_ones_the_plan_fixed(self):
        import bi_agent.semantic_catalog.retrieval as retrieval

        self.assertEqual((retrieval.METRIC_ALIAS_SCORE, retrieval.FIELD_ALIAS_SCORE,
                          retrieval.ENTITY_ALIAS_SCORE, retrieval.TOKEN_SCORE,
                          retrieval.DOMAIN_SCORE), (100, 40, 20, 5, 10))
        self.assertEqual(retrieval.MAX_VIEW_CANDIDATES, 5)

    def test_conflicts_and_missing_vocabulary_are_closed(self):
        from bi_agent.semantic_catalog.retrieval import CONFLICTS, KNOWN_MISSING_CONCEPTS

        self.assertEqual(dict(CONFLICTS), {
            frozenset({"metric-product-gross-profit-reference",
                       "metric-erp-gross-profit-reference"}): "profit_grain",
            frozenset({"metric-physical-available-quantity",
                       "metric-channel-sellable-quantity"}): "inventory_grain",
            frozenset({"metric-transaction-average-price",
                       "metric-listing-price"}): "price_basis",
        })
        self.assertEqual(set(KNOWN_MISSING_CONCEPTS),
                         {"promotion_spend", "traffic", "attribution", "net_profit"})
        for table in (CONFLICTS, KNOWN_MISSING_CONCEPTS):
            with self.assertRaises(TypeError):
                table["injected"] = "x"


class SemanticRetrievalTests(unittest.TestCase):
    """检索的排序、歧义与脱敏（计划 Step 1 的两条起步用例逐字保留）。"""

    def versions(self):
        from bi_agent.runtime.versions import VersionSet
        from bi_agent.semantic_catalog.registry import SEMANTIC_CATALOG_VERSION
        return VersionSet(
            schema_version="reporting/2026-09-14.1",
            semantic_catalog_version=SEMANTIC_CATALOG_VERSION,
            data_catalog_version=7, metric_version="metrics/2026-09-12.1",
            policy_version="multi-source-policy/2026-09-12.1",
            source_registry_version="sources/2026-09-12.1",
            graph_version="business_query-graph/2026-09-11.1")

    def test_paid_amount_retrieves_shop_daily_without_identifiers(self):
        from bi_agent.semantic_catalog import retrieve_schema_candidates
        result = retrieve_schema_candidates(
            "按店铺看每日支付金额", allowed_domains=frozenset({"business_query"}),
            current_versions=self.versions(), limit=5)
        self.assertEqual(result.view_refs[0], "view-shop-daily")
        self.assertIn("metric-paid-amount", result.metric_refs)
        self.assertNotIn("v_shop_daily", repr(result))

    def test_conflicting_profit_grains_require_clarification(self):
        from bi_agent.semantic_catalog import retrieve_schema_candidates
        result = retrieve_schema_candidates(
            "比较商品毛利和ERP单据毛利", allowed_domains=frozenset({"commerce_performance"}),
            current_versions=self.versions())
        self.assertTrue(result.requires_clarification)
        self.assertIn("profit_grain", result.missing_concepts)

    def test_longer_alias_wins_so_two_bases_are_never_mixed(self):
        # `商品销售额` 里含 `销售额`：短命中必须作废，否则两种口径的金额一起被点亮。
        result = retrieve("商品销售额", ["business_query", "commerce_performance"])
        self.assertEqual(result.metric_refs, ("metric-sales-amount",))
        self.assertNotIn("metric-paid-amount", result.metric_refs)
        self.assertEqual(result.field_refs, ("field-product-cost-daily-sales-amount",))

    def test_a_disallowed_long_word_never_falls_back_to_its_shorter_alias(self):
        # `商品销售额` 只属于 commerce_performance；在 business_query 轮里既不能发它，
        # 也不能反过来用 `销售额` 冒充支付金额（静默降级就是发错数）。
        result = retrieve("商品销售额", ["business_query"])
        self.assertEqual(result.metric_refs, ())
        self.assertEqual(result.field_refs, ())
        self.assertTrue(result.requires_clarification)

    def test_ordering_is_score_then_ref_never_input_order(self):
        # `支付金额` 与 `收支差` 都只完整命中一次，但前者的长别名（`已支付金额` /
        # `买家已支付金额`）与问题共享词块 ⇒ 多拿 +5，排序看分数而不是字母序。
        first = retrieve("支付金额和收支差", ["business_query"])
        self.assertEqual(first.metric_refs,
                         ("metric-paid-amount", "metric-cash-difference"))
        self.assertEqual(retrieve("收支差和支付金额", ["business_query"]).metric_refs,
                         first.metric_refs)

    def test_equal_scores_break_on_ref(self):
        # 两个价格指标都只命中一次且没拿到词块分（分数相等）⇒ 只剩 ref 升序。
        self.assertEqual(retrieve("上架价与活动价", ["listing_price_audit"]).metric_refs,
                         ("metric-campaign-price", "metric-listing-price"))
        self.assertEqual(retrieve("活动价与上架价", ["listing_price_audit"]).metric_refs,
                         ("metric-campaign-price", "metric-listing-price"))

    def test_reordered_clauses_give_the_identical_selection(self):
        one = retrieve("按平台看支付金额和销量", ["business_query", "commerce_performance"])
        other = retrieve("销量与支付金额，按平台", ["commerce_performance", "business_query"])
        self.assertEqual(one, other)

    def test_inventory_measurements_are_ordered_by_score_not_by_mention_order(self):
        # 同一题里三个量分三档：库存池可用量（长别名 +2 个部分命中）> 入库量 > 锁定量。
        result = retrieve("按仓库和SKU看库存池可用量、入库量、锁定量与计量单位",
                          ["inventory_watch"])
        self.assertEqual(result.metric_refs,
                         ("metric-physical-available-quantity",
                          "metric-inbound-quantity", "metric-locked-quantity"))

    def test_selected_metric_carries_every_required_field(self):
        # 毛利参考的定义是"这一组每行都有成本"，六列证据少一列就不够下游算。
        result = retrieve("商品毛利", ["commerce_performance"])
        self.assertEqual(set(result.field_refs), {
            "field-product-cost-daily-sales-amount", "field-product-cost-daily-cost-total",
            "field-product-cost-daily-line-count", "field-product-cost-daily-cost-line-count",
            "field-product-cost-daily-cost-quantity", "field-product-cost-daily-quantity"})

    def test_same_named_columns_are_told_apart_by_their_view(self):
        # `quantity` 在两张视图里同名：只有成本视图那个叫"成本口径件数"。
        cost = retrieve("成本口径件数", ["commerce_performance"])
        self.assertIn("field-product-cost-daily-quantity", cost.field_refs)
        self.assertNotIn("field-product-daily-quantity", cost.field_refs)
        sold = retrieve("销量", ["business_query"])
        self.assertIn("field-product-daily-quantity", sold.field_refs)
        self.assertNotIn("field-product-cost-daily-quantity", sold.field_refs)

    def test_registered_join_is_returned_only_when_both_sides_are_needed(self):
        joined = retrieve("按店铺和平台看支付订单数", ["business_query"])
        self.assertEqual(joined.join_path_refs, ("join-shop-daily-shops",))
        self.assertFalse(joined.requires_clarification)
        # 只问事实表：不发边。实体命中（店铺）不把 view-shops 算成"需要"。
        plain = retrieve("按店铺看每日支付金额", ["business_query"])
        self.assertEqual(plain.join_path_refs, ())
        self.assertIn("view-shops", plain.view_refs)

    def test_two_fact_views_are_connected_by_their_own_edges(self):
        result = retrieve("按平台看支付金额和销量",
                          ["business_query", "commerce_performance"])
        self.assertEqual(result.join_path_refs,
                         ("join-product-daily-shops", "join-shop-daily-shops"))
        self.assertFalse(result.requires_clarification)

    def test_disconnected_views_are_never_half_joined(self):
        for question, domains in (
                ("各店的退款金额和平台原始退款金额分别看", ["business_query"]),
                ("按币种看支付金额", ["business_query"]),
                ("商品销售额和销量一起看", ["business_query", "commerce_performance"]),
                ("商品成本合计和单据成本哪个高", ["commerce_performance"]),
                ("按店铺看ERP单据数和商品销售额", ["business_query", "commerce_performance"])):
            with self.subTest(question=question):
                result = retrieve(question, domains)
                self.assertGreater(len(result.view_refs), 1)
                self.assertEqual(result.join_path_refs, ())
                self.assertTrue(result.requires_clarification)

    def test_no_unregistered_edge_can_ever_be_returned(self):
        registered = {"join-erp-document-daily-shops", "join-product-cost-daily-shops",
                      "join-product-daily-shops", "join-shop-daily-shops"}
        probes = [row["question"] for row in gold_rows()[0]] + [
            "把支付流水金额和平台原始退款金额对齐", "实物库存与渠道库存哪个低",
            "上架价与单据毛利", "支付金额、上架价、实物库存与商品销售额一起看"]
        for question in probes:
            for domains in (["business_query"], ["business_query", "commerce_performance"],
                            sorted(KNOWN_DOMAINS)):
                result = retrieve(question, domains)
                self.assertLessEqual(set(result.join_path_refs), registered, question)

    def test_outstock_basis_never_becomes_paid_amount(self):
        # 整句只要通用销售额：这是被批准的拒绝形状——整份候选不发，也不新增原因码。
        result = retrieve("按出库口径看销售额", ["business_query"])
        self.assertEqual(result.metric_refs, ())
        self.assertEqual(result.field_refs, ())
        self.assertEqual(result.view_refs, ())
        self.assertEqual(result.entity_refs, ())
        self.assertEqual(result.join_path_refs, ())
        self.assertEqual(result.missing_concepts, ())
        self.assertTrue(result.requires_clarification)
        self.assertNotIn("metric-paid-amount", repr(result))

    def test_outstock_basis_never_resolves_to_a_product_sales_amount(self):
        # `商品销售额` 不是通用词，但它同样是支付窗口的数（017 里的 sales_amount 按
        # paid_at 归日）：点名出库口径时不许拿它顶出库金额。
        result = retrieve("按出库口径看商品销售额",
                          ["business_query", "commerce_performance"])
        self.assertEqual(result.metric_refs, ())
        self.assertEqual(result.field_refs, ())
        self.assertEqual(result.view_refs, ())
        self.assertEqual(result.entity_refs, ())
        self.assertEqual(result.join_path_refs, ())
        self.assertEqual(result.missing_concepts, ())
        self.assertTrue(result.requires_clarification)
        self.assertNotIn("metric-sales-amount", repr(result))

    def test_outstock_basis_never_resolves_through_the_measure_field(self):
        # 守卫只能看指标 ref 是不够的：`metric-paid-amount` 只登记在 business_query 下，
        # 而 `v_shop_daily` 跨两种领域。只允许 commerce_performance 的一轮里，指标被领域
        # 门禁挡住、剩下的只有 `field-shop-daily-paid-amount`：把它发回去依旧是把支付窗口
        # 的数当作出库金额。
        for question in ("按出库口径看销售额", "按出库口径看支付金额"):
            with self.subTest(question=question):
                result = retrieve(question, ["commerce_performance"])
                self.assertEqual(result.metric_refs, ())
                self.assertEqual(result.field_refs, ())
                self.assertEqual(result.view_refs, ())
                self.assertEqual(result.entity_refs, ())
                self.assertEqual(result.join_path_refs, ())
                self.assertEqual(result.missing_concepts, ())
                self.assertTrue(result.requires_clarification)
                self.assertNotIn("field-shop-daily-paid-amount", repr(result))

    def test_generic_sales_measure_field_is_never_a_settled_basis(self):
        # 同一轮里没有出库说法、只用了通用词：候选可以留，但口径算未定。
        result = retrieve("销售额", ["commerce_performance"])
        self.assertEqual(result.field_refs, ("field-shop-daily-paid-amount",))
        self.assertTrue(result.requires_clarification)

    def test_outstock_measure_field_guard_leaves_independent_columns_alone(self):
        # 正向对照：不在那个闭包里的列不受影响。
        documents = retrieve("按出库口径看ERP单据数", ["business_query"])
        self.assertEqual(documents.metric_refs, ("metric-erp-documents",))
        self.assertIn("field-shop-daily-erp-documents", documents.field_refs)
        self.assertFalse(documents.requires_clarification)
        allocated = retrieve("商品分摊支付金额", ["commerce_performance"])
        self.assertEqual(allocated.metric_refs, ("metric-product-paid-amount",))
        self.assertIn("field-product-daily-product-paid-amount", allocated.field_refs)
        self.assertFalse(allocated.requires_clarification)

    def test_payment_basis_amount_field_closure_comes_from_the_catalog(self):
        from bi_agent.semantic_catalog import CATALOG, catalog_indexes
        import bi_agent.semantic_catalog.retrieval as retrieval

        indexes = catalog_indexes(CATALOG)
        self.assertEqual(retrieval._payment_basis_amount_field_refs(indexes), frozenset({
            "field-shop-daily-paid-amount", "field-product-cost-daily-sales-amount",
            "field-product-daily-product-paid-amount", "field-payments-amount"}))
        # 新增一个支付窗口金额指标，它的列自动进守卫：不鼓助第二份名单。
        self.assertTrue(all(ref in {field.ref for field in CATALOG.fields}
                            for ref in retrieval._payment_basis_amount_field_refs(indexes)))

    def test_outstock_basis_with_an_explicit_payment_flow_still_asks(self):
        # `支付流水金额` 自己就是支付侧说法：跟出库口径同时出现是自相矛盾的问题，
        # 候选可以留，但这一轮不能当成口径已确认。
        result = retrieve("按出库口径看支付流水金额", ["business_query"])
        self.assertTrue(result.requires_clarification)

    def test_payment_amount_metrics_still_retrieve_without_an_outstock_phrase(self):
        # 正向对照：没有出库说法时，两个支付窗口金额照常检索，不跟着报澄清。
        product = retrieve("商品销售额", ["business_query", "commerce_performance"])
        self.assertEqual(product.metric_refs, ("metric-sales-amount",))
        self.assertEqual(product.view_refs[0], "view-product-cost-daily")
        self.assertEqual(product.missing_concepts, ())
        self.assertFalse(product.requires_clarification)
        flow = retrieve("支付流水金额按支付时间看", ["business_query"])
        self.assertEqual(flow.metric_refs, ("metric-payment-flow-amount",))
        self.assertEqual(flow.view_refs, ("view-payments",))
        self.assertFalse(flow.requires_clarification)

    def test_payment_window_amount_set_is_closed_and_immutable(self):
        import bi_agent.semantic_catalog.retrieval as retrieval

        self.assertEqual(retrieval.PAYMENT_BASIS_AMOUNT_METRICS, frozenset({
            "metric-paid-amount", "metric-sales-amount",
            "metric-product-paid-amount", "metric-payment-flow-amount"}))
        with self.assertRaises(AttributeError):
            retrieval.PAYMENT_BASIS_AMOUNT_METRICS.add("metric-erp-documents")

    def test_outstock_guard_leaves_independent_concepts_alone(self):
        documents = retrieve("按出库口径看ERP单据数", ["business_query"])
        self.assertEqual(documents.metric_refs, ("metric-erp-documents",))
        self.assertEqual(documents.missing_concepts, ())
        self.assertFalse(documents.requires_clarification)
        quantity = retrieve("按出库单看销量", ["business_query"])
        self.assertEqual(quantity.metric_refs, ("metric-sold-quantity",))
        self.assertFalse(quantity.requires_clarification)
        # 混着问时：金额被摘走，独立概念必须留着，整轮仍然要澄清。
        mixed = retrieve("按出库口径看商品销售额和ERP单据数",
                         ["business_query", "commerce_performance"])
        self.assertEqual(mixed.metric_refs, ("metric-erp-documents",))
        self.assertNotIn("metric-sales-amount", mixed.metric_refs)
        self.assertIn("view-shop-daily", mixed.view_refs)
        self.assertTrue(mixed.requires_clarification)

    def test_explicit_payment_alias_with_an_outstock_phrase_still_asks(self):
        # 自相矛盾的问题：列点名了支付，口径又说出库——候选照发，但不能当成已确认。
        result = retrieve("按出库口径看支付金额", ["business_query"])
        self.assertEqual(result.metric_refs, ("metric-paid-amount",))
        self.assertTrue(result.requires_clarification)

    def test_generic_sales_word_alone_is_not_a_settled_basis(self):
        for question in ("本周销售额是多少", "本周GMV是多少"):
            with self.subTest(question=question):
                result = retrieve(question, ["business_query"])
                self.assertEqual(result.metric_refs, ("metric-paid-amount",))
                self.assertTrue(result.requires_clarification)
        settled = retrieve("按店铺看每日支付金额", ["business_query"])
        self.assertFalse(settled.requires_clarification)

    def test_all_three_conflicts_are_recognised(self):
        cases = {
            "profit_grain": ("比较商品毛利和ERP单据毛利", ["commerce_performance"]),
            "inventory_grain": ("实物库存与渠道库存一起看", ["inventory_watch"]),
            "price_basis": ("成交均价与上架价哪个更能说明问题",
                            ["commerce_performance", "listing_price_audit"]),
        }
        for code, (question, domains) in cases.items():
            with self.subTest(code=code):
                result = retrieve(question, domains)
                self.assertEqual(result.missing_concepts, (code,))
                self.assertTrue(result.requires_clarification)
        # 只点名一边就不算冲突。
        single = retrieve("商品毛利参考是多少", ["commerce_performance"])
        self.assertEqual(single.missing_concepts, ())
        self.assertFalse(single.requires_clarification)

    def test_known_missing_vocabulary_is_recognised_and_never_invented(self):
        for code, phrase in (("promotion_spend", "广告花费"), ("traffic", "流量"),
                             ("attribution", "归因"), ("net_profit", "净利润")):
            with self.subTest(code=code):
                result = retrieve(f"{phrase}这一轮的支付金额按店铺看", ["business_query"])
                self.assertEqual(result.missing_concepts, (code,))
                self.assertTrue(result.requires_clarification)
        noise = retrieve("zzqqx 库存赔付率 wertyu", ["business_query"])
        self.assertEqual(noise.missing_concepts, ())
        self.assertTrue(noise.requires_clarification)
        for token in ("zzqqx", "wertyu", "库存赔付率"):
            self.assertNotIn(token, repr(noise))

    def test_limit_is_a_hard_range_and_anchored_views_win_their_slots(self):
        question = "按平台看支付金额和销量"
        capped = retrieve(question, ["business_query", "commerce_performance"], limit=1)
        self.assertEqual(len(capped.view_refs), 1)
        self.assertIn(capped.view_refs[0],
                      ("view-shop-daily", "view-product-daily", "view-shops"))
        self.assertTrue(capped.requires_clarification, "点名了 3 张视图却只发 1 张")
        self.assertEqual(len(retrieve(question, ["business_query", "commerce_performance"],
                                      limit=3).view_refs), 3)
        for bad in (0, 6, -1, True, "5", None):
            with self.subTest(limit=bad):
                with self.assertRaises(ValueError):
                    retrieve("支付金额", ["business_query"], limit=bad)

    def test_anchored_views_come_before_entity_only_fill(self):
        # 只被实体点到的视图（`view-channel-stock-items` 因为 SKU）可以填充，但不能插到
        # 被指标/字段锚定的视图前面。
        result = retrieve("实物库存按SKU和仓库看", ["inventory_watch"])
        self.assertEqual(result.view_refs,
                         ("view-physical-stock-items", "view-channel-stock-items"))
        self.assertEqual(result.metric_refs, ("metric-physical-available-quantity",))
        self.assertFalse(result.requires_clarification)

    def test_invalid_arguments_are_refused_before_any_work(self):
        from bi_agent.semantic_catalog import retrieve_schema_candidates

        with self.assertRaises(ValueError):
            # 不是 frozenset：绕过 `retrieve()` 包一层，直接交给实现。
            retrieve_schema_candidates("支付金额", allowed_domains={"business_query"},
                                       current_versions=version_set())
        with self.assertRaises(ValueError):
            retrieve("支付金额", ["business_query", "commerce"])  # 未登记领域
        with self.assertRaises(ValueError):
            retrieve("   ", ["business_query"])
        with self.assertRaises(ValueError):
            retrieve(None, ["business_query"])
        with self.assertRaises(ValueError):
            retrieve("支付金额", ["business_query"], versions=object())

    def test_stale_or_unknown_catalog_version_is_refused(self):
        for version in ("semantic/2026-09-13.1", "semantic/9999-99-99.9"):
            with self.subTest(version=version):
                with self.assertRaisesRegex(ValueError, "semantic_catalog_version_mismatch"):
                    retrieve("支付金额", ["business_query"],
                             versions=version_set(semantic_catalog_version=version))

    def test_no_stale_catalog_can_be_searched_through_a_parameter(self):
        import inspect

        from bi_agent.semantic_catalog import retrieve_schema_candidates

        self.assertNotIn("catalog", inspect.signature(retrieve_schema_candidates).parameters)
        self.assertEqual(
            list(inspect.signature(retrieve_schema_candidates).parameters),
            ["question", "allowed_domains", "current_versions", "limit"])

    def test_empty_allowed_domains_offers_nothing(self):
        result = retrieve("按店铺看每日支付金额", [])
        self.assertEqual((result.view_refs, result.metric_refs, result.field_refs,
                          result.entity_refs, result.join_path_refs),
                         ((), (), (), (), ()))
        self.assertTrue(result.requires_clarification)

    def test_selection_is_immutable_and_leaks_no_identifiers(self):
        result = retrieve("按平台看支付金额和销量",
                          ["business_query", "commerce_performance"])
        with self.assertRaises(FrozenInstanceError):
            result.view_refs = ()
        refs = catalog_refs()
        for attribute, universe in (("entity_refs", refs["entities"]),
                                    ("metric_refs", refs["metrics"]),
                                    ("view_refs", refs["views"]),
                                    ("field_refs", refs["fields"]),
                                    ("join_path_refs", refs["joins"])):
            values = getattr(result, attribute)
            self.assertIsInstance(values, tuple, attribute)
            self.assertLessEqual(values and set(values) or set(), universe, attribute)
            self.assertEqual(len(set(values)), len(values), attribute)
            self.assertNotIn("_", "".join(values), f"{attribute} 带着 SQL 标识符的形状")
        self.assertIsInstance(result.missing_concepts, tuple)
        self.assertIsInstance(result.requires_clarification, bool)
        dumped = repr(result)
        for leak in ("reporting.", "v_", "bi.", "shop_id", "pool_id", "SELECT", "sku_id"):
            self.assertNotIn(leak, dumped)

    def test_same_question_retrieves_the_same_selection(self):
        first = retrieve("上架价与活动价按在售链接看", ["listing_price_audit"])
        self.assertEqual([retrieve("上架价与活动价按在售链接看", ["listing_price_audit"])
                          for _ in range(3)], [first] * 3)

    def test_pdd_payment_readiness_is_never_implied(self):
        result = retrieve("拼多多的支付金额和上架价", sorted(KNOWN_DOMAINS))
        dumped = repr(result)
        for token in ("pdd", "拼多多"):
            self.assertNotIn(token, dumped)
        self.assertLessEqual(set(result.metric_refs), catalog_refs()["metrics"])

    def test_retrieval_touches_no_database_network_model_or_tool_dispatch(self):
        import ast

        package = pathlib.Path(__file__).resolve().parents[1] / "bi_agent"
        tree = ast.parse((package / "semantic_catalog" / "retrieval.py")
                         .read_text(encoding="utf-8"))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
                imported.add(node.module.split(".")[0])
        self.assertLessEqual(imported, {"__future__", "dataclasses", "re", "types",
                                        "typing", "unicodedata", "bi_agent"}, sorted(imported))
        for banned in ("psycopg", "httpx", "subprocess", "open"):
            self.assertNotIn(banned, (package / "semantic_catalog" / "retrieval.py")
                             .read_text(encoding="utf-8"))
        # 检索不进固定 Tool 路由：agent / runtime 注册表里没有任何引用。
        for path in (package / "agent.py", package / "runtime" / "domain_registry.py",
                     package / "business_query" / "tool.py"):
            text = path.read_text(encoding="utf-8")
            self.assertNotIn("retrieve_schema_candidates", text, path.name)
            self.assertNotIn("semantic_catalog", text, path.name)


class SemanticGoldSetTests(unittest.TestCase):
    """30 题 gold set：Top 5 召回 100%、非法 JOIN 0、形状与脱敏逐行把关。"""

    def check_row(self, row, result):
        """一题的全部期望；返回人可读的不符清单（空表 = 通过）。"""
        problems = []
        if not set(row["required_views"]) <= set(result.view_refs[:5]):
            problems.append(f"所需视图 {row['required_views']} 不在 Top 5 "
                            f"{list(result.view_refs)}")
        for attribute, key in (("metric_refs", "required_metrics"),
                               ("join_path_refs", "join_paths"),
                               ("missing_concepts", "missing_concepts")):
            actual = getattr(result, attribute)
            if sorted(actual) != sorted(row[key]):
                problems.append(f"{key}: 期望 {sorted(row[key])} 实得 {sorted(actual)}")
        if result.requires_clarification != row["clarify"]:
            problems.append(f"clarify: 期望 {row['clarify']} "
                            f"实得 {result.requires_clarification}")
        return problems

    def test_file_is_exactly_thirty_frozen_rows_with_the_fixed_shape(self):
        rows, lines = gold_rows()
        self.assertEqual(len(rows), 30)
        self.assertEqual(len(lines), 30, "gold 文件只准 30 行：不留空行或注释位")
        self.assertEqual([row["id"] for row in rows], [f"S{index:02d}" for index in range(1, 31)])
        self.assertEqual(len({row["question"] for row in rows}), 30)
        for row in rows:
            self.assertEqual(tuple(sorted(row)), GOLD_KEYS, row["id"])
            self.assertTrue(row["domains"], row["id"])
            self.assertLessEqual(set(row["domains"]), KNOWN_DOMAINS, row["id"])
            self.assertIsInstance(row["clarify"], bool, row["id"])
            for key in ("required_views", "required_metrics", "join_paths", "missing_concepts"):
                self.assertIsInstance(row[key], list, row["id"])
                self.assertEqual(len(set(row[key])), len(row[key]), f"{row['id']} 有重复项")

    def test_gold_rows_never_carry_identifiers_or_source_claims(self):
        rows = gold_rows()[0]
        for row in rows:
            # 题面与期望值都不准出现真实主键、底表名或来源就绪的说法（id 列除外）。
            text = json.dumps({key: value for key, value in row.items() if key != "id"},
                              ensure_ascii=False)
            for leak in ("bi.", "reporting.", "shop_id", "pool_id", "v_", "pdd", "拼多多",
                         "S1", "TB1", "FX1", "kuaimai", "certified"):
                self.assertNotIn(leak, text, row["id"])

    def test_every_row_matches_and_top_five_recall_is_complete(self):
        refs = catalog_refs()
        rows = gold_rows()[0]
        failures = []
        recall: dict[str, bool] = {}
        for row in rows:
            result = retrieve(row["question"], row["domains"])
            problems = self.check_row(row, result)
            if len(result.view_refs) > 5:
                problems.append(f"Top 5 被超出：{list(result.view_refs)}")
            for values, universe in ((row["required_views"], refs["views"]),
                                     (row["required_metrics"], refs["metrics"]),
                                     (row["join_paths"], refs["joins"])):
                if not set(values) <= universe:
                    problems.append(f"{row['id']} 期望了未登记的 ref")
            returned = (result.view_refs + result.metric_refs + result.field_refs
                        + result.entity_refs + result.join_path_refs)
            for value in returned:
                if not re.fullmatch(REF_RE, value):
                    problems.append(f"{row['id']} 返回了非法 ref：{value}")
            if not set(returned) <= set().union(*refs.values()):
                problems.append(f"{row['id']} 返回了目录之外的 ref")
            if not set(result.missing_concepts) <= MISSING_CODES:
                problems.append(f"{row['id']} 返回了固定表之外的缺失概念")
            dumped = repr(result)
            for leak in ("reporting.", "bi.", "v_", "shop_id", "pool_id"):
                if leak in dumped:
                    problems.append(f"{row['id']} 输出泄露了 {leak}")
            recall[row["id"]] = set(row["required_views"]) <= set(result.view_refs[:5])
            if problems:
                failures.append(f"{row['id']}: " + "；".join(problems))
        self.assertEqual(failures, [])
        needing = [row["id"] for row in rows if row["required_views"]]
        # 召回率 = 所需视图全部落进 Top 5 的题数 / 有所需视图的题数；必须 100%。
        # （上面 `failures == []` 已经逐题判过；这里再把它算成一个数，不让分母被动过手脚。）
        recalled = sum(1 for ref in needing if recall[ref])
        self.assertEqual(recalled, len(needing))
        self.assertEqual(recalled / len(needing), 1.0)
        self.assertEqual(len(needing), 27,
                         "只有整份拒绝的三题（S07 / S26 / S27）可以没有所需视图")

    def test_gold_set_covers_the_whole_catalog_it_claims_to_test(self):
        rows = gold_rows()[0]
        refs = catalog_refs()
        self.assertEqual({ref for row in rows for ref in row["required_views"]},
                         refs["views"], "有视图一次都没被要求召回")
        self.assertEqual({ref for row in rows for ref in row["required_metrics"]},
                         refs["metrics"], "有指标一次都没被点名")
        self.assertEqual({domain for row in rows for domain in row["domains"]},
                         set(KNOWN_DOMAINS))
        for domain in KNOWN_DOMAINS:
            # 每个工作流至少三题；更强的覆盖断言是上面那两条：11 张视图与 22 个指标
            # 必须各自被全部点名一次（不多不少）。
            self.assertGreaterEqual(sum(1 for row in rows if domain in row["domains"]), 3,
                                    f"{domain} 覆盖不足")
        self.assertGreaterEqual(sum(1 for row in rows if row["join_paths"]), 2)
        self.assertEqual({code for row in rows for code in row["missing_concepts"]},
                         set(MISSING_CODES))
        self.assertGreaterEqual(sum(1 for row in rows if row["clarify"]), 12)
        self.assertGreaterEqual(sum(1 for row in rows if not row["clarify"]), 12)
        # 三对故意不登记的边都必须真的被拒（join_paths 为空 + 要澄清）。
        forbidden = [row for row in rows
                     if row["required_metrics"] and not row["join_paths"] and row["clarify"]]
        self.assertGreaterEqual(len(forbidden), 5, "非法 JOIN 的用例不足")

    def test_runner_is_not_vacuous(self):
        rows = gold_rows()[0]
        result = retrieve(rows[0]["question"], rows[0]["domains"])
        self.assertEqual(self.check_row(rows[0], result), [])
        mutations = (
            ("required_views", lambda row: row.update(required_views=["view-coverage"])),
            ("required_metrics", lambda row: row.update(required_metrics=[])),
            ("required_metrics", lambda row: row.update(
                required_metrics=list(row["required_metrics"]) + ["metric-paid-orders"])),
            ("join_paths", lambda row: row.update(join_paths=["join-shop-daily-shops"])),
            ("clarify", lambda row: row.update(clarify=not row["clarify"])),
            ("missing_concepts", lambda row: row.update(missing_concepts=["traffic"])),
        )
        for key, mutate in mutations:
            with self.subTest(key=key):
                broken = dict(rows[0])
                mutate(broken)
                self.assertTrue(self.check_row(broken, result),
                                f"改掉 {key} 之后 runner 仍然通过：那条断言是恒真的")
        # 领域是另一回事：它换的是输入，不是期望值 ⇒ 结果必须跟着变。
        self.assertNotEqual(retrieve(rows[0]["question"], ["commerce_performance"]), result)

    def test_every_row_is_reproducible(self):
        for row in gold_rows()[0]:
            with self.subTest(row["id"]):
                first = retrieve(row["question"], row["domains"])
                self.assertEqual(self.check_row(row, retrieve(row["question"],
                                                              row["domains"])), [])
                self.assertEqual(first, retrieve(row["question"], row["domains"]))


class SemanticRetrievalSafetyTests(unittest.TestCase):
    """候选闭包、拒绝形状与"不共享状态"：Task 3 最容易在后续 Task 里被改坏的地方。
    """

    def test_every_returned_ref_is_allowed_by_this_rounds_domains(self):
        from bi_agent.semantic_catalog import CATALOG, catalog_indexes

        cases = (
            ("按SKU看上架价", ["business_query"]),
            ("按SKU看上架价", ["listing_price_audit"]),
            ("按仓库看实物库存与店铺可售库存", ["inventory_watch"]),
            ("支付金额、上架价与实物库存一起看", sorted(KNOWN_DOMAINS)),
            ("商品分摊支付金额与销量", ["commerce_performance"]),
        )
        indexes = catalog_indexes(CATALOG)
        for question, domains in cases:
            with self.subTest(question=question, domains=domains):
                result = retrieve(question, domains)
                allowed = frozenset(domains)
                self.assertTrue(allowed or not result.view_refs)
                for ref in result.view_refs:
                    self.assertTrue(set(indexes.views[ref].domains) & allowed, ref)
                for ref in result.field_refs:
                    owner = indexes.views[indexes.fields[ref].view_ref]
                    self.assertTrue(set(owner.domains) & allowed, ref)
                for ref in result.metric_refs:
                    self.assertTrue(set(indexes.metrics[ref].domains) & allowed, ref)
                for ref in result.entity_refs:
                    self.assertTrue(set(indexes.entities[ref].domains) & allowed, ref)

    def test_refusal_still_names_the_catalog_it_refused_from(self):
        result = retrieve("按出库口径看销售额", ["business_query"])
        from bi_agent.semantic_catalog import CATALOG

        self.assertEqual(result.catalog_version, CATALOG.version)
        self.assertFalse(any((result.entity_refs, result.metric_refs, result.view_refs,
                              result.field_refs, result.join_path_refs,
                              result.missing_concepts)))

    def test_two_calls_do_not_share_state(self):
        # 口径判定会就地摘除候选：必须只影响当轮那份字典。
        refused = retrieve("按出库口径看销售额", ["business_query"])
        normal = retrieve("销售额", ["business_query"])
        later = retrieve("按出库口径看销售额", ["business_query"])
        self.assertEqual(refused, later)
        self.assertEqual(normal.metric_refs, ("metric-paid-amount",))
        self.assertTrue(normal.requires_clarification)
        from bi_agent.semantic_catalog import CATALOG

        self.assertEqual(len(CATALOG.metrics), 22)

    def test_a_path_through_the_shop_archive_does_not_legitimise_two_fact_views(self):
        # 两张事实表都能 N:1 连到店铺档案，但它们之间没有登记边：穿过档案连起来不等于
        # 有合法 JOIN 路径（那正是首批目录故意不登记 `商品成本 ↔ 单据毛利` 的原因）。
        result = retrieve("按店铺看支付金额和销量", ["business_query", "commerce_performance"])
        self.assertEqual(result.join_path_refs, ())
        self.assertTrue(result.requires_clarification)
        # 点名平台之后 view-shops 自己成了锚点，这两条边才算真的被需要。
        joined = retrieve("按店铺和平台看支付金额和销量",
                          ["business_query", "commerce_performance"])
        self.assertEqual(joined.join_path_refs,
                         ("join-product-daily-shops", "join-shop-daily-shops"))
        self.assertFalse(joined.requires_clarification)


# --- Task 4：数据库 schema 预检与只读边界 ----------------------------------------

# 计划 Task 4 Step 3 钉下的唯一 introspection 查询：按空白归一化比对，实现既不许改
# SELECT 列 / WHERE 过滤 / ORDER BY，也不许在这条之外顺手发别的语句。
INTROSPECTION_SQL = ("SELECT table_schema, table_name, column_name, data_type "
                     "FROM information_schema.columns "
                     "WHERE table_schema = 'reporting' "
                     "ORDER BY table_name, ordinal_position")


def normalized(sql):
    return " ".join(str(sql).split())


def real_columns(catalog=None):
    """把目录“读成”一份与它完全匹配的库内形状。

    类型族里挑第一个成员当实际类型（`integer` 声明可以是 `bigint`）：这样通过用例
    证明的是“比的是族”，而不是“比的是目录里那个字面量”。
    """
    from bi_agent.semantic_catalog import CATALOG, catalog_indexes
    from bi_agent.semantic_catalog.registry import DATA_TYPE_SQL_FAMILIES

    target = CATALOG if catalog is None else catalog
    indexes = catalog_indexes(target)
    columns = {f"{view_entry.schema}.{view_entry.name}": {} for view_entry in target.views}
    for field_entry in target.fields:
        owner = indexes.views[field_entry.view_ref]
        family = sorted(DATA_TYPE_SQL_FAMILIES[field_entry.data_type])
        columns[f"{owner.schema}.{owner.name}"][field_entry.column] = family[0]
    return columns


class IntrospectionRows:
    """psycopg 结果对象的最小替身。"""

    def __init__(self, rows):
        self._rows = list(rows)

    def fetchall(self):
        return list(self._rows)


class FakeIntrospectionConn:
    """`reporting` schema 的替身：只回答计划指定的那条查询。

    `tables` 形状是 `{"reporting.v_shop_daily": {"shop_id": "text", ...}}`：少一张视图
    =库里没有（或当前角色读不到）它，少一列=列不在了，类型不同=类型族变了。任何其它
    SQL 直接报错，所以“预检顺手多发一条语句”这种退化一定变红，不会静默通过。
    """

    def __init__(self, tables):
        self.tables = {key: dict(value) for key, value in tables.items()}
        self.executed = []

    def rows(self):
        rows = []
        for key in sorted(self.tables):
            schema, _, name = key.partition(".")
            for column, data_type in self.tables[key].items():
                rows.append((schema, name, column, data_type))
        return rows

    def execute(self, sql, params=None, **kwargs):
        text = normalized(sql)
        self.executed.append(text)
        if text != INTROSPECTION_SQL:
            raise AssertionError(f"预检只准执行计划指定的那条查询，实际：{text}")
        if params is not None or kwargs:
            raise AssertionError("计划指定的那条查询不带参数")
        return IntrospectionRows(self.rows())


class SemanticSchemaCheckTests(unittest.TestCase):
    """`validate_catalog_schema`：一条元数据查询、稳定原因码、失败即不降级。"""

    def validate(self, conn, catalog=None):
        from bi_agent.semantic_catalog import CATALOG
        from bi_agent.semantic_catalog.schema_check import validate_catalog_schema

        return validate_catalog_schema(conn, CATALOG if catalog is None else catalog)

    def mismatch(self, conn, catalog=None):
        from bi_agent.semantic_catalog.schema_check import SchemaMismatch

        with self.assertRaises(SchemaMismatch) as caught:
            self.validate(conn, catalog)
        return caught.exception

    # --- 计划 Step 1 的三条形状 ------------------------------------------------

    def test_missing_registered_column_fails_closed(self):
        """计划 Task 4 Step 1 起步用例：登记的列在库里没了 ⇒ 只报稳定 ref。"""
        conn = FakeIntrospectionConn({
            "reporting.v_shop_daily": {"shop_id": "text", "day": "date"},
            "reporting.v_shops": {"shop_id": "text"},
        })
        error = self.mismatch(conn, closed_catalog())
        self.assertEqual(error.refs, ("field-shop-daily-paid-amount",))
        self.assertEqual(str(error), "semantic_schema_mismatch:field-shop-daily-paid-amount")

    def test_missing_registered_view_fails_closed(self):
        conn = FakeIntrospectionConn({"reporting.v_shops": {"shop_id": "text"}})
        error = self.mismatch(conn, closed_catalog())
        # 整张视图不在就报视图 ref：把它下面每一列再念一遍只是噪音。
        self.assertEqual(error.refs, ("view-shop-daily",))
        self.assertEqual(str(error), "semantic_schema_mismatch:view-shop-daily")

    def test_declared_type_family_must_match_the_real_column(self):
        tables = real_columns(closed_catalog())
        tables["reporting.v_shop_daily"]["paid_amount"] = "text"      # 目录声明 decimal
        error = self.mismatch(FakeIntrospectionConn(tables), closed_catalog())
        self.assertEqual(error.refs, ("field-shop-daily-paid-amount",))
        self.assertEqual(str(error), "semantic_schema_mismatch:field-shop-daily-paid-amount")

    def test_the_matching_declaration_passes_with_no_side_effects(self):
        conn = FakeIntrospectionConn(real_columns())
        self.assertIsNone(self.validate(conn))
        # 只读元数据：一次调用恰好一条 SQL，且就是计划钉下的那条。
        self.assertEqual(conn.executed, [INTROSPECTION_SQL])

    def test_a_type_from_the_declared_family_is_accepted(self):
        # `integer` 的族里有 bigint/integer/smallint：换成员不是漂移，改族才是。
        tables = real_columns()
        tables["reporting.v_shop_daily"]["paid_orders"] = "integer"
        self.assertIsNone(self.validate(FakeIntrospectionConn(tables)))

    # --- 逐列/逐视图覆盖：84 条声明不许被抽样代替 ------------------------------

    def test_every_registered_field_is_checked_against_the_database(self):
        from bi_agent.semantic_catalog import CATALOG

        self.assertGreaterEqual(len(CATALOG.fields), 60)
        for field_entry in CATALOG.fields:
            with self.subTest(field=field_entry.ref):
                tables = real_columns()
                owner = next(view_entry for view_entry in CATALOG.views
                             if view_entry.ref == field_entry.view_ref)
                del tables[f"{owner.schema}.{owner.name}"][field_entry.column]
                error = self.mismatch(FakeIntrospectionConn(tables))
                self.assertEqual(error.refs, (field_entry.ref,))

    def test_every_registered_view_is_checked_against_the_database(self):
        from bi_agent.semantic_catalog import CATALOG

        self.assertGreaterEqual(len(CATALOG.views), 8)
        for view_entry in CATALOG.views:
            with self.subTest(view=view_entry.ref):
                tables = real_columns()
                del tables[f"{view_entry.schema}.{view_entry.name}"]
                error = self.mismatch(FakeIntrospectionConn(tables))
                self.assertEqual(error.refs, (view_entry.ref,))

    # --- 映射形状：列名不属于“任意一张视图” ------------------------------------

    def test_a_column_is_credited_only_in_its_own_view(self):
        # 同名列在别的视图里存在，不能替 `v_shop_daily.paid_amount` 交差。
        conn = FakeIntrospectionConn({
            "reporting.v_shop_daily": {"shop_id": "text", "day": "date"},
            "reporting.v_shops": {"shop_id": "text", "paid_amount": "numeric"},
        })
        error = self.mismatch(conn, closed_catalog())
        self.assertEqual(error.refs, ("field-shop-daily-paid-amount",))

    def test_rows_from_another_schema_never_count(self):
        # 真实查询带 `WHERE table_schema = 'reporting'`；替身故意回一条 public 的行，
        # 实现必须按 (schema, view, column) 三元组建映射，而不是只看视图名。
        conn = FakeIntrospectionConn({
            "public.v_shop_daily": {"shop_id": "text", "day": "date",
                                    "paid_amount": "numeric"},
            "reporting.v_shops": {"shop_id": "text"},
        })
        error = self.mismatch(conn, closed_catalog())
        self.assertEqual(error.refs, ("view-shop-daily",))

    def test_unregistered_views_and_columns_are_not_reported(self):
        # 库里可以有很多没登记的对象：预检既不能因此失败，也不能因此“顺手登记”。
        tables = real_columns()
        tables["reporting.v_channel_items"] = {"shop_id": "text", "quantity": "numeric"}
        tables["reporting.v_shop_daily"]["capabilities"] = "ARRAY"
        self.assertIsNone(self.validate(FakeIntrospectionConn(tables)))

    def test_the_query_is_the_plan_one_and_nothing_else_is_executed(self):
        conn = FakeIntrospectionConn(real_columns())
        self.validate(conn)
        self.assertEqual(conn.executed, [INTROSPECTION_SQL])
        self.assertNotIn("bi.", conn.executed[0])
        self.assertNotIn(";", conn.executed[0])
        # 替身本身不是恒真的：任何别的 SQL 都会被它拒绝。
        with self.assertRaises(AssertionError):
            conn.execute("SELECT 1")

    def test_an_unclosed_catalog_is_refused_before_any_sql_runs(self):
        base = closed_catalog()
        broken = dataclasses.replace(base, views=replaced_item(
            base, "views", 0, field_refs=("field-shop-daily-shop-id", "field-nope")))
        conn = FakeIntrospectionConn(real_columns(base))
        with self.assertRaisesRegex(ValueError, "semantic_catalog_"):
            self.validate(conn, broken)
        self.assertEqual(conn.executed, [])

    # --- 消息形状：稳定、可排序、可脱敏 ----------------------------------------

    def test_refs_are_sorted_and_only_the_first_is_in_the_message(self):
        tables = real_columns()
        del tables["reporting.v_coverage"]["data_as_of"]              # field-coverage-*
        del tables["reporting.v_erp_document_daily"]["raw_cost"]      # field-erp-*
        del tables["reporting.v_shops"]                               # view-shops
        error = self.mismatch(FakeIntrospectionConn(tables))
        self.assertEqual(error.refs, ("field-coverage-data-as-of",
                                      "field-erp-document-daily-raw-cost",
                                      "view-shops"))
        self.assertEqual(str(error), "semantic_schema_mismatch:field-coverage-data-as-of")
        # 同一个缺陷集合必须给出同一个字符串：诊断才能被 grep、被计数、被比较。
        self.assertEqual(str(self.mismatch(FakeIntrospectionConn(tables))), str(error))
        self.assertIsInstance(error, ValueError)

    def test_the_message_never_carries_sql_identifiers_or_database_text(self):
        conn = FakeIntrospectionConn({"reporting.v_shops": {"shop_id": "text"}})
        message = str(self.mismatch(conn, closed_catalog()))
        for leak in ("reporting.", "v_shop_daily", "paid_amount", "shop_id", "SELECT",
                     "information_schema", "password", "postgresql://"):
            self.assertNotIn(leak, message)
        self.assertTrue(re.fullmatch(r"semantic_schema_mismatch:[a-z][a-z0-9-]*", message))

    def test_a_ref_built_around_the_contract_still_cannot_reach_the_message(self):
        """兜底规则不是死代码：形状不符的“ref”一律换成固定码，原文绝不回显。"""
        base = closed_catalog()
        smuggled = "v_shop_daily; -- postgresql://app:secret"
        fields = tuple(
            patched(item, ref=smuggled) if item.ref == "field-shop-daily-paid-amount"
            else item for item in base.fields)
        views = tuple(
            patched(item, field_refs=tuple(
                smuggled if ref == "field-shop-daily-paid-amount" else ref
                for ref in item.field_refs))
            if item.ref == "view-shop-daily" else item for item in base.views)
        metrics = tuple(
            patched(item, required_field_refs=tuple(
                smuggled if ref == "field-shop-daily-paid-amount" else ref
                for ref in item.required_field_refs))
            for item in base.metrics)
        broken = dataclasses.replace(base, fields=fields, views=views, metrics=metrics)
        conn = FakeIntrospectionConn({
            "reporting.v_shop_daily": {"shop_id": "text", "day": "date"},
            "reporting.v_shops": {"shop_id": "text"},
        })
        error = self.mismatch(conn, broken)
        for leak in ("postgresql", "--", "v_shop_daily", "paid_amount", "secret"):
            self.assertNotIn(leak, str(error))
        self.assertEqual(error.refs, ("catalog-entry",))
        self.assertEqual(str(error), "semantic_schema_mismatch:catalog-entry")


@unittest.skipUnless(os.getenv("BI_TEST_READER_DSN"), "未配置测试库的只读角色 DSN")
class SemanticSchemaCheckDatabaseTests(unittest.TestCase):
    """计划 Task 4 Step 5：只读角色下预检通过，而 `bi.orders` 依然读不到。

    连接统一走 `tests.dbfixtures.connect_test_db`：库名必须以 `_test` 结尾、主机必须在本机，
    整段包在显式回滚事务里——预检本身只读元数据，但数据库门禁不能因为“这次不写”就松开。
    """

    def test_reader_preflight_passes_while_base_tables_stay_closed(self):
        from tests import dbfixtures

        conn = dbfixtures.connect_test_db(self, "BI_TEST_READER_DSN")
        self.assertEqual(conn.info.user, "bi_reader")
        self.assertTrue(conn.info.dbname.endswith("_test"))

        # 1) 声明与真实 schema 一致：整份 CATALOG 通过（启动门禁做的正是这件事）。
        self.assertIsNone(self.validate(conn))
        # 2) 预检用的权限没有超出“读获准视图”这条线：视图照旧能查。
        conn.execute("SELECT count(*) FROM reporting.v_shop_daily").fetchone()
        # 3) 业务事实底表仍然被拒——预检没有扩大任何事实读取面。
        with self.assertRaises(psycopg.errors.InsufficientPrivilege):
            conn.execute("SELECT 1 FROM bi.orders LIMIT 1")

    def validate(self, conn):
        from bi_agent.semantic_catalog import CATALOG
        from bi_agent.semantic_catalog.schema_check import validate_catalog_schema

        return validate_catalog_schema(conn, CATALOG)
