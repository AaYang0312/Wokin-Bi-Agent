"""语义目录契约与登记表测试（计划 Task 1 + Task 2）。

契约的形状就是它的价值所在：这些类型是后续注册表、检索与受控 SQL 唯一的
词汇来源，所以"什么算一个合法 ref / 合法版本标识 / 合法基数"必须在这一层
一次定死，而不是等某个调用方顺手放宽。

Task 2 的部分再加一条：登记内容必须**闭包**（没有悬空/错归属/放大的边），而且
登记的列名要和已应用迁移的真实 schema 一致——后者是一条连本机测试库的对账，
缺 DSN 时按仓内现行约定正常 skip（不是 error）。
"""

from dataclasses import FrozenInstanceError
import copy
import dataclasses
import os
import unittest

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

    def test_task_one_surface_exports_models_only(self):
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
            ]))
        # 检索与启动校验属于 Task 3/4：它们必须还不存在。
        self.assertFalse(hasattr(package, "retrieve_schema_candidates"))
        self.assertFalse(hasattr(package, "validate_catalog_schema"))
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
