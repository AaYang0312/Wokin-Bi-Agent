"""语义目录契约测试（计划 Task 1）。

契约的形状就是它的价值所在：这些类型是后续注册表、检索与受控 SQL 唯一的
词汇来源，所以"什么算一个合法 ref / 合法版本标识 / 合法基数"必须在这一层
一次定死，而不是等某个调用方顺手放宽。
"""

from dataclasses import FrozenInstanceError
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
            ]))
        # 目录内容与检索属于 Task 2/3：此刻它们必须还不存在。
        self.assertFalse(hasattr(package, "CATALOG"))
        self.assertFalse(hasattr(package, "retrieve_schema_candidates"))
        from bi_agent.semantic_catalog.models import REF_RE as ref_pattern

        self.assertEqual(ref_pattern, REF_RE)
