"""首版语义目录：显式登记的 reporting 视图、字段、指标与合法 JOIN 边（计划 Task 2）。

这一层只回答"哪些已批准的结构可能表达这个问题"：它不查业务事实、不生成 SQL、
不授予任何能力（总设计 §5.1）。因此三条边界在代码里是硬的：

1. 只登记逐一批准的 `reporting.*` 视图与其列；`bi.*` 底表、`information_schema`
   里出现的新对象都不进目录——没有"自动发现即登记"这条路。
2. 模型与检索只看 ref；SQL 标识符只能由 `resolve_sql_identifier` 在服务端解析。
3. JOIN 边只准 N:1 / 1:1，且必须写明防放大理由。

## 与计划表格的两处有意差异（都是 fail-closed，不是遗漏）

**a) `field-shop-id` 这个简写不能照抄。** `SemanticField.view_ref` 决定一个字段 ref
只属于一个视图，而 `shop_id` 在十个视图里重复出现。若十个视图共用同一个 ref，
按 ref 建的索引会静默保留最后一个视图，其余视图的授权列就解析到别的表上——那是
能发错金额的缺陷，不是命名偏好。因此重复列一律按**视图作用域**命名
（`field-shop-daily-shop-id`、`field-shops-shop-id`……），不保留多归属的规范 ref。
计划表格里那些"authorization = field-shop-id"读作"该视图以 shop_id 授权"。

**b) 数组与 multirange 列首批不登记。** `reporting.v_shops.capabilities`（数组）与
`reporting.v_coverage.covered`（tstzmultirange）在计划表格里被提到，但 Task 1 的
`DataType` 词表是封闭的，没有 array/multirange 两种取值；把它们伪装成 `text` 会让
Task 4 的启动一致性校验在真实 schema 上永久失败。列仍在库里，只是不可被检索选中；
要开放必须先扩词表并升目录版本。

## 首批目录只能表达支付口径的金额

已批准的 reporting 视图里没有任何“ERP 出库金额”列：出库口径只在现有固定 Tool 与 Python 里算（`sources.OUTSTOCK_BASIS`
是 `erp_outstock_payment/v1`），不落在批准视图的列上。
所以问“按出库口径看销售额”时，检索只能报 `schema_ambiguous` / 缺失概念，不能退回
`paid_amount`。支付与出库在这里故意不共用一列：`sources.PAYMENT_BASIS` 与
`sources.OUTSTOCK_BASIS` 是两个口径（docs/metrics.md 的 basis 契约），同一个
“销售额”按两种口径会算出不同的数。

## 粒度键也登记为字段

计划表格的"允许字段"一列没有重复写 `day`、`product_id` 之类，但同一行的 grain 提到了
它们。grain 若没有对应字段就是无法 group by 的装饰，所以每张视图登记
「授权列 ∪ grain 键列 ∪ 表格允许列」。ERP 主键（`product_id`、`erp_id`、`commercial_id`、
`snapshot_id`、`warehouse_id`、`aftersale_id`、`listing_id`、`platform_sku_id`、
`erp_sku_id`）一律标 `role="internal"`：它们只为让粒度可解析而存在，不得进入模型可见
选择，也不得作为分组维度直接暴露——对外仍是既有 Tool 的 opaque ref。
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping, NoReturn

from bi_agent.runtime.domain_registry import known_domain

from .models import (
    SemanticCatalog,
    SemanticEntity,
    SemanticField,
    SemanticJoin,
    SemanticMetric,
    SemanticView,
)

SEMANTIC_CATALOG_VERSION = "semantic/2026-09-14.1"

# 领域词表只有 runtime 那一套：目录不发明第二个名字。
ALL_DOMAINS = ("business_query", "commerce_performance", "listing_price_audit",
               "inventory_watch")
TRADE_DOMAINS = ("business_query", "commerce_performance")

_VIEW_SHOPS = "view-shops"
_VIEW_SHOP_DAILY = "view-shop-daily"
_VIEW_PRODUCT_DAILY = "view-product-daily"
_VIEW_PRODUCT_COST_DAILY = "view-product-cost-daily"
_VIEW_ERP_DOCUMENT_DAILY = "view-erp-document-daily"
_VIEW_PAYMENTS = "view-payments"
_VIEW_REFUNDS = "view-refunds"
_VIEW_COVERAGE = "view-coverage"
_VIEW_LISTING_ITEMS = "view-listing-items"
_VIEW_PHYSICAL_STOCK_ITEMS = "view-physical-stock-items"
_VIEW_CHANNEL_STOCK_ITEMS = "view-channel-stock-items"

_ENTITY_SHOP = "entity-shop"
_ENTITY_PRODUCT = "entity-product"
_ENTITY_SKU = "entity-sku"
_ENTITY_LISTING = "entity-listing"
_ENTITY_INVENTORY_POOL = "entity-inventory-pool"
_ENTITY_WAREHOUSE = "entity-warehouse"
_ENTITY_ERP_DOCUMENT = "entity-erp-document"
_ENTITY_PAYMENT = "entity-payment"
_ENTITY_REFUND = "entity-refund"
_ENTITY_SOURCE = "entity-source"

# 声明的 data_type → PostgreSQL `information_schema.columns.data_type` 可能的取值。
# Task 4 的启动校验按这份映射比"类型族"，所以这里必须是目录里唯一的类型↔SQL 知识。
# `ref` 表示"服务端持有的不透明标识列"，在库里就是 text。
DATA_TYPE_SQL_FAMILIES: Mapping[str, frozenset[str]] = MappingProxyType({
    "date": frozenset({"date"}),
    "datetime": frozenset({"timestamp with time zone"}),
    "decimal": frozenset({"numeric"}),
    "integer": frozenset({"bigint", "integer", "smallint"}),
    "boolean": frozenset({"boolean"}),
    "text": frozenset({"text"}),
    "ref": frozenset({"text"}),
})


class SemanticCatalogViolation(ValueError):
    """目录不闭包：宁可在导入时炸，也不让下游拿着半套 ref 去编译 SQL。

    原因码固定 `semantic_catalog_*` 前缀；附带的 ref 会先消毒，避免以后目录改成
    由外部输入构造时，错误消息变成日志注入的口子。
    """


def _fail(reason: str, detail: object = "") -> NoReturn:
    safe = "".join(ch if ch.isalnum() or ch in "-_." else "?" for ch in str(detail))[:64]
    raise SemanticCatalogViolation(f"semantic_catalog_{reason}" + (f":{safe}" if safe else ""))


def _field_ref(view_ref: str, column: str) -> str:
    """字段 ref 由「视图名 + 列名」唯一决定：重复列因此必然按视图作用域分开。"""
    return f"field-{view_ref[len('view-'):]}-{column.replace('_', '-')}"


# --- 目录内容 -------------------------------------------------------------------

def _entity(ref: str, aliases: tuple[str, ...], description: str,
            domains: tuple[str, ...]) -> SemanticEntity:
    return SemanticEntity(ref=ref, domains=domains, aliases=aliases,
                          description=description)


ENTITIES: tuple[SemanticEntity, ...] = (
    _entity(_ENTITY_SHOP, ("店铺", "门店"),
            "一家已授权店铺：跨渠道比较、授权与作用域都落在这个实体上。", ALL_DOMAINS),
    _entity(_ENTITY_PRODUCT, ("商品", "货品", "产品"),
            "ERP 商品（父项/子件在同一视图里按 line_kind 分开，不靠名字合并）。",
            TRADE_DOMAINS),
    _entity(_ENTITY_SKU, ("SKU", "规格", "款式"),
            "可下单的具体规格；渠道侧与 ERP 侧的 SKU 通过目录映射相连。",
            ("listing_price_audit", "inventory_watch")),
    _entity(_ENTITY_LISTING, ("在售链接", "商品链接", "listing"),
            "某个店铺平台上的一条在售链接（价审的作用对象）。",
            ("listing_price_audit", "inventory_watch")),
    _entity(_ENTITY_INVENTORY_POOL, ("库存池", "实物库存池"),
            "共享实物库存的池子：多店可以指向同一个池，因此按池而不是按店去重。",
            ("inventory_watch",)),
    _entity(_ENTITY_WAREHOUSE, ("仓库",),
            "实物库存的仓库维度（实物粒度的组成键之一）。", ("inventory_watch",)),
    _entity(_ENTITY_ERP_DOCUMENT, ("ERP单据", "单据", "出库单"),
            "ERP 的一张单据：ERP 毛利参考就按这个粒度记账。", TRADE_DOMAINS),
    _entity(_ENTITY_PAYMENT, ("支付流水", "支付单"),
            "一笔支付记录（明细面，聚合面在 v_shop_daily）。", TRADE_DOMAINS),
    _entity(_ENTITY_REFUND, ("退款单", "售后单"),
            "一笔退款/售后记录（明细面，聚合面在 v_shop_daily）。", TRADE_DOMAINS),
    _entity(_ENTITY_SOURCE, ("数据源", "来源"),
            "一个来源绑定：覆盖、水位与数据质量都按来源记账。", ALL_DOMAINS),
)


# (view_ref, column, data_type, role, aliases)；列名逐字取自定义它的迁移，
# 并由 `SemanticRegistrySchemaTests` 与本机测试库的 information_schema 对账。
_FIELD_ROWS: tuple[tuple[str, str, str, str, tuple[str, ...]], ...] = (
    # reporting.v_shops（001_init.sql）。capabilities 是数组列，首批不登记（见模块注释）。
    (_VIEW_SHOPS, "shop_id", "ref", "authorization", ()),
    (_VIEW_SHOPS, "platform", "text", "dimension", ("平台", "渠道")),
    (_VIEW_SHOPS, "display_name", "text", "internal", ()),
    (_VIEW_SHOPS, "currency", "text", "dimension", ("币种",)),
    (_VIEW_SHOPS, "enabled", "boolean", "dimension", ("店铺已启用",)),

    # reporting.v_shop_daily（001_init.sql）
    (_VIEW_SHOP_DAILY, "shop_id", "ref", "authorization", ()),
    (_VIEW_SHOP_DAILY, "day", "date", "time", ("日期", "按天", "每天")),
    (_VIEW_SHOP_DAILY, "currency", "text", "dimension", ("币种",)),
    (_VIEW_SHOP_DAILY, "paid_amount", "decimal", "measure",
     ("支付金额", "已支付金额", "销售额")),
    (_VIEW_SHOP_DAILY, "paid_orders", "integer", "measure", ("支付订单数", "订单数")),
    (_VIEW_SHOP_DAILY, "erp_documents", "integer", "measure", ("ERP单据数", "单据数")),
    (_VIEW_SHOP_DAILY, "refund_amount", "decimal", "measure", ("退款金额",)),
    (_VIEW_SHOP_DAILY, "cash_difference", "decimal", "measure", ("收支差", "现金差")),

    # reporting.v_product_daily（007_catalog_identity.sql 为最新定义）
    (_VIEW_PRODUCT_DAILY, "shop_id", "ref", "authorization", ()),
    (_VIEW_PRODUCT_DAILY, "day", "date", "time", ("日期", "按天", "每天")),
    (_VIEW_PRODUCT_DAILY, "product_id", "ref", "internal", ()),
    (_VIEW_PRODUCT_DAILY, "line_kind", "text", "dimension", ("商品行类型", "行类型")),
    (_VIEW_PRODUCT_DAILY, "quantity", "decimal", "measure", ("销量", "成交件数")),
    (_VIEW_PRODUCT_DAILY, "gift_quantity", "decimal", "measure", ("赠品件数", "赠品数量")),
    (_VIEW_PRODUCT_DAILY, "product_paid_amount", "decimal", "measure",
     ("商品分摊支付金额",)),
    (_VIEW_PRODUCT_DAILY, "allocation_verified", "boolean", "dimension",
     ("分摊已核验", "成本分摊已核验")),

    # reporting.v_product_cost_daily（017_commerce_views.sql）。sku_ids 是数组列，不登记。
    (_VIEW_PRODUCT_COST_DAILY, "shop_id", "ref", "authorization", ()),
    (_VIEW_PRODUCT_COST_DAILY, "day", "date", "time", ("日期", "按天", "每天")),
    (_VIEW_PRODUCT_COST_DAILY, "product_id", "ref", "internal", ()),
    (_VIEW_PRODUCT_COST_DAILY, "line_kind", "text", "dimension", ("商品行类型", "行类型")),
    (_VIEW_PRODUCT_COST_DAILY, "quantity", "decimal", "measure", ("成本口径件数",)),
    (_VIEW_PRODUCT_COST_DAILY, "sales_amount", "decimal", "measure",
     ("商品销售额", "商品成交金额")),
    (_VIEW_PRODUCT_COST_DAILY, "line_count", "integer", "measure", ("商品行数",)),
    (_VIEW_PRODUCT_COST_DAILY, "cost_line_count", "integer", "measure",
     ("带成本的行数", "成本行数")),
    (_VIEW_PRODUCT_COST_DAILY, "cost_quantity", "decimal", "measure",
     ("带成本的件数", "成本件数")),
    (_VIEW_PRODUCT_COST_DAILY, "cost_total", "decimal", "measure", ("商品成本合计", "成本合计")),

    # reporting.v_erp_document_daily（017_commerce_views.sql）
    (_VIEW_ERP_DOCUMENT_DAILY, "shop_id", "ref", "authorization", ()),
    (_VIEW_ERP_DOCUMENT_DAILY, "erp_id", "ref", "internal", ()),
    (_VIEW_ERP_DOCUMENT_DAILY, "day", "date", "time", ("日期", "按天", "每天")),
    (_VIEW_ERP_DOCUMENT_DAILY, "raw_cost", "decimal", "measure", ("单据成本", "ERP原始成本")),
    (_VIEW_ERP_DOCUMENT_DAILY, "raw_gross_profit", "decimal", "measure",
     ("单据毛利", "ERP单据毛利")),
    (_VIEW_ERP_DOCUMENT_DAILY, "commercial_ids_count", "integer", "measure",
     ("关联商业单数", "商业单数")),
    (_VIEW_ERP_DOCUMENT_DAILY, "normalization_status", "text", "dimension",
     ("归一化状态", "单据归一状态")),

    # reporting.v_payments（001_init.sql）
    (_VIEW_PAYMENTS, "shop_id", "ref", "authorization", ()),
    (_VIEW_PAYMENTS, "commercial_id", "ref", "internal", ()),
    (_VIEW_PAYMENTS, "paid_at", "datetime", "time", ("支付时间",)),
    (_VIEW_PAYMENTS, "amount", "decimal", "measure", ("支付流水金额",)),
    (_VIEW_PAYMENTS, "currency", "text", "dimension", ("币种",)),
    (_VIEW_PAYMENTS, "verified", "boolean", "dimension", ("支付已核验",)),

    # reporting.v_refunds（001_init.sql）
    (_VIEW_REFUNDS, "shop_id", "ref", "authorization", ()),
    (_VIEW_REFUNDS, "aftersale_id", "ref", "internal", ()),
    (_VIEW_REFUNDS, "platform_completed_at", "datetime", "time", ("退款完成时间",)),
    (_VIEW_REFUNDS, "raw_platform_amount", "decimal", "measure", ("平台原始退款金额",)),
    (_VIEW_REFUNDS, "platform_success", "boolean", "dimension", ("平台退款成功",)),
    (_VIEW_REFUNDS, "refund_canonical", "boolean", "dimension", ("退款已归一",)),
    (_VIEW_REFUNDS, "matched", "boolean", "dimension", ("退款已匹配",)),

    # reporting.v_coverage（008_data_readiness.sql）。covered 是 multirange 列，不登记。
    (_VIEW_COVERAGE, "shop_id", "ref", "authorization", ()),
    (_VIEW_COVERAGE, "source", "text", "dimension", ("数据源", "来源")),
    (_VIEW_COVERAGE, "entity", "text", "dimension", ("实体类型", "对象类型")),
    (_VIEW_COVERAGE, "data_as_of", "datetime", "time", ("数据截至", "数据更新时间")),
    (_VIEW_COVERAGE, "quality_status", "text", "dimension", ("数据质量状态", "质量状态")),
    (_VIEW_COVERAGE, "quality_rule", "text", "dimension", ("数据质量规则", "质量规则")),

    # reporting.v_listing_snapshot_items（018_listing_audit.sql）
    (_VIEW_LISTING_ITEMS, "shop_id", "ref", "authorization", ()),
    (_VIEW_LISTING_ITEMS, "snapshot_id", "ref", "internal", ()),
    (_VIEW_LISTING_ITEMS, "listing_id", "ref", "internal", ()),
    (_VIEW_LISTING_ITEMS, "platform_sku_id", "ref", "internal", ()),
    (_VIEW_LISTING_ITEMS, "erp_sku_id", "ref", "internal", ()),
    (_VIEW_LISTING_ITEMS, "list_amount", "decimal", "measure", ("上架价", "标价")),
    (_VIEW_LISTING_ITEMS, "campaign_amount", "decimal", "measure", ("活动价", "促销价")),
    (_VIEW_LISTING_ITEMS, "currency", "text", "dimension", ("币种",)),
    (_VIEW_LISTING_ITEMS, "on_sale", "boolean", "dimension", ("在售",)),
    (_VIEW_LISTING_ITEMS, "captured_at", "datetime", "time", ("快照抓取时间", "抓取时间")),

    # reporting.v_physical_stock_items（019_inventory_snapshots.sql）：按库存池授权。
    (_VIEW_PHYSICAL_STOCK_ITEMS, "pool_id", "ref", "authorization", ()),
    (_VIEW_PHYSICAL_STOCK_ITEMS, "snapshot_id", "ref", "internal", ()),
    (_VIEW_PHYSICAL_STOCK_ITEMS, "warehouse_id", "ref", "internal", ()),
    (_VIEW_PHYSICAL_STOCK_ITEMS, "erp_sku_id", "ref", "internal", ()),
    (_VIEW_PHYSICAL_STOCK_ITEMS, "available_quantity", "decimal", "measure",
     ("实物可用量", "实物库存", "库存池可用量")),
    (_VIEW_PHYSICAL_STOCK_ITEMS, "inbound_quantity", "decimal", "measure",
     ("入库在途量", "入库量")),
    (_VIEW_PHYSICAL_STOCK_ITEMS, "locked_quantity", "decimal", "measure",
     ("锁定量", "占用量")),
    (_VIEW_PHYSICAL_STOCK_ITEMS, "unit", "text", "dimension", ("计量单位", "单位")),
    (_VIEW_PHYSICAL_STOCK_ITEMS, "captured_at", "datetime", "time", ("快照抓取时间", "抓取时间")),

    # reporting.v_channel_stock_items（019_inventory_snapshots.sql）
    (_VIEW_CHANNEL_STOCK_ITEMS, "shop_id", "ref", "authorization", ()),
    (_VIEW_CHANNEL_STOCK_ITEMS, "snapshot_id", "ref", "internal", ()),
    (_VIEW_CHANNEL_STOCK_ITEMS, "listing_id", "ref", "internal", ()),
    (_VIEW_CHANNEL_STOCK_ITEMS, "platform_sku_id", "ref", "internal", ()),
    (_VIEW_CHANNEL_STOCK_ITEMS, "erp_sku_id", "ref", "internal", ()),
    (_VIEW_CHANNEL_STOCK_ITEMS, "sellable_quantity", "decimal", "measure",
     ("店铺可售库存", "渠道库存", "可售库存")),
    (_VIEW_CHANNEL_STOCK_ITEMS, "unit", "text", "dimension", ("计量单位", "单位")),
    (_VIEW_CHANNEL_STOCK_ITEMS, "captured_at", "datetime", "time", ("快照抓取时间", "抓取时间")),
)

FIELDS: tuple[SemanticField, ...] = tuple(
    SemanticField(ref=_field_ref(view_ref, column), view_ref=view_ref, column=column,
                  data_type=data_type, role=role, aliases=aliases)
    for view_ref, column, data_type, role, aliases in _FIELD_ROWS
)


def _view(ref: str, name: str, entity_ref: str, grain: tuple[str, ...],
          domains: tuple[str, ...], authorization_column: str = "shop_id") -> SemanticView:
    return SemanticView(
        ref=ref, schema="reporting", name=name, entity_ref=entity_ref, grain=grain,
        # 每张视图的授权列：默认 shop_id，实物库存池按 pool_id 授权。
        authorization_field_ref=_field_ref(ref, authorization_column),
        # 视图的字段集合由字段表反推：这样"注册了列却没被视图列入"（或反过来）
        # 在这份目录里根本构造不出来，闭包校验仍然照跑。
        field_refs=tuple(f.ref for f in FIELDS if f.view_ref == ref),
        domains=domains,
    )


VIEWS: tuple[SemanticView, ...] = (
    _view(_VIEW_SHOPS, "v_shops", _ENTITY_SHOP, ("shop",), ALL_DOMAINS),
    _view(_VIEW_SHOP_DAILY, "v_shop_daily", _ENTITY_SHOP,
          ("shop", "day", "currency"), TRADE_DOMAINS),
    _view(_VIEW_PRODUCT_DAILY, "v_product_daily", _ENTITY_PRODUCT,
          ("shop", "day", "product", "line_kind"), TRADE_DOMAINS),
    _view(_VIEW_PRODUCT_COST_DAILY, "v_product_cost_daily", _ENTITY_PRODUCT,
          ("shop", "day", "product", "line_kind"), ("commerce_performance",)),
    _view(_VIEW_ERP_DOCUMENT_DAILY, "v_erp_document_daily", _ENTITY_ERP_DOCUMENT,
          ("shop", "erp_document"), ("commerce_performance",)),
    _view(_VIEW_PAYMENTS, "v_payments", _ENTITY_PAYMENT,
          ("shop", "commercial_order"), TRADE_DOMAINS),
    _view(_VIEW_REFUNDS, "v_refunds", _ENTITY_REFUND,
          ("shop", "refund"), TRADE_DOMAINS),
    _view(_VIEW_COVERAGE, "v_coverage", _ENTITY_SOURCE,
          ("source", "entity", "shop"), ALL_DOMAINS),
    _view(_VIEW_LISTING_ITEMS, "v_listing_snapshot_items", _ENTITY_LISTING,
          ("snapshot", "listing", "sku"), ("listing_price_audit",)),
    _view(_VIEW_PHYSICAL_STOCK_ITEMS, "v_physical_stock_items", _ENTITY_INVENTORY_POOL,
          ("snapshot", "pool", "warehouse", "sku", "unit"), ("inventory_watch",),
          authorization_column="pool_id"),
    _view(_VIEW_CHANNEL_STOCK_ITEMS, "v_channel_stock_items", _ENTITY_SKU,
          ("snapshot", "shop", "listing", "sku", "unit"), ("inventory_watch",)),
)


_ANTI_AMPLIFICATION_SHOPS = (
    "reporting.v_shops 直接投影 bi.shops（shop_id 是主键），每个 shop_id 只有一行，"
    "JOIN 不增加行数；金额只在左侧事实视图上聚合一次，档案侧不得再展开一次"
)


def _shop_join(left_view_ref: str) -> SemanticJoin:
    """唯一登记的边：事实视图 N:1 店铺档案，两侧都是各自的授权列。"""
    return SemanticJoin(
        ref=f"join-{left_view_ref[len('view-'):]}-shops",
        left_view_ref=left_view_ref,
        right_view_ref=_VIEW_SHOPS,
        left_field_ref=_field_ref(left_view_ref, "shop_id"),
        right_field_ref=_field_ref(_VIEW_SHOPS, "shop_id"),
        cardinality="many_to_one",
        allowed_group_grains=("shop",),
        anti_amplification=_ANTI_AMPLIFICATION_SHOPS,
    )


JOINS: tuple[SemanticJoin, ...] = tuple(_shop_join(ref) for ref in (
    _VIEW_SHOP_DAILY, _VIEW_PRODUCT_DAILY, _VIEW_PRODUCT_COST_DAILY,
    _VIEW_ERP_DOCUMENT_DAILY,
))
# 故意没有的边（计划 Step 3）：payments↔refunds、商品成本↔单据毛利、
# 实物库存↔渠道库存。三条都会把不同口径或不同粒度的行乘到一起，
# 一旦登记，Task 3 就无法靠"没有合法边"把它们请求澄清。


def _metric(ref: str, view_ref: str, aliases: tuple[str, ...], columns: tuple[str, ...],
            allowed: tuple[str, ...], default: str, basis_required: bool,
            domains: tuple[str, ...]) -> SemanticMetric:
    return SemanticMetric(
        ref=ref, domains=domains, aliases=aliases,
        required_field_refs=tuple(_field_ref(view_ref, column) for column in columns),
        allowed_aggregates=allowed, default_aggregate=default,
        basis_required=basis_required,
    )


# 聚合语义的两条约定（契约里没有放说明的位置，写在这里给 Task 3/4 读）：
# * `("avg",)` 的指标是**比值型**指标（成交均价、上架价）：值必须由分子分母在同一
#   行集合上各自求和后再相除得到，不是对逐行比值取平均——这里登记 avg 只表示
#   "它是一个平均意义上的量"，不授权 `AVG(col)`。
# * 上架价/活动价只在**单个快照**内有意义：跨快照聚合必须先按 snapshot 过滤，
#   这条约束无法用现有字段表达，靠 Task 3/4 的粒度与新鲜度检查兜住。
METRICS: tuple[SemanticMetric, ...] = (
    _metric("metric-paid-amount", _VIEW_SHOP_DAILY,
            ("支付金额", "已支付金额", "买家已支付金额", "销售额", "GMV"),
            ("paid_amount",), ("sum",), "sum", True, ("business_query",)),
    _metric("metric-paid-orders", _VIEW_SHOP_DAILY,
            ("支付订单数", "订单数"), ("paid_orders",),
            ("sum",), "sum", True, ("business_query",)),
    _metric("metric-erp-documents", _VIEW_SHOP_DAILY,
            ("ERP单据数", "单据数"), ("erp_documents",),
            ("sum",), "sum", False, ("business_query",)),
    _metric("metric-refund-amount", _VIEW_SHOP_DAILY,
            ("退款金额",), ("refund_amount",), ("sum",), "sum", True, ("business_query",)),
    _metric("metric-cash-difference", _VIEW_SHOP_DAILY,
            ("收支差", "现金差"), ("cash_difference",),
            ("sum",), "sum", True, ("business_query",)),
    _metric("metric-sold-quantity", _VIEW_PRODUCT_DAILY,
            ("销量", "成交件数"), ("quantity",), ("sum",), "sum", True, TRADE_DOMAINS),
    _metric("metric-gift-quantity", _VIEW_PRODUCT_DAILY,
            ("赠品件数", "赠品数量"), ("gift_quantity",),
            ("sum",), "sum", False, TRADE_DOMAINS),
    _metric("metric-product-paid-amount", _VIEW_PRODUCT_DAILY,
            ("商品分摊支付金额",), ("product_paid_amount",),
            ("sum",), "sum", True, TRADE_DOMAINS),
    _metric("metric-sales-amount", _VIEW_PRODUCT_COST_DAILY,
            ("商品销售额", "商品成交金额"), ("sales_amount",),
            ("sum",), "sum", True, ("commerce_performance",)),
    # 两个 quantity 列（v_product_daily 与 v_product_cost_daily）在各自迁移里是同一条
    # WHERE + 同一条 GROUP BY（同一个数），所以"销量"只留一个 ref：
    # `metric-sold-quantity`。成本视图那列仍作为必需字段登记（成交均价与
    # 毛利参考的分母），但不另起一个指标名——同一概念不得有两种拼法。
    # 毛利参考本身必须是"这一组里每行都有成本"才成立，完整性靠 line_count /
    # cost_line_count / cost_quantity 三列证据判断，因此它们一起登记为必需字段。
    _metric("metric-product-gross-profit-reference", _VIEW_PRODUCT_COST_DAILY,
            ("商品毛利", "商品毛利参考", "商品预估毛利"),
            ("sales_amount", "cost_total", "line_count", "cost_line_count",
             "cost_quantity", "quantity"),
            ("sum",), "sum", True, ("commerce_performance",)),
    _metric("metric-cost-total", _VIEW_PRODUCT_COST_DAILY,
            ("成本合计", "商品成本合计"), ("cost_total",),
            ("sum",), "sum", True, ("commerce_performance",)),
    _metric("metric-erp-gross-profit-reference", _VIEW_ERP_DOCUMENT_DAILY,
            ("ERP单据毛利", "单据毛利", "ERP毛利参考"), ("raw_gross_profit",),
            ("sum",), "sum", True, ("commerce_performance",)),
    _metric("metric-erp-document-cost", _VIEW_ERP_DOCUMENT_DAILY,
            ("单据成本", "ERP原始成本"), ("raw_cost",),
            ("sum",), "sum", False, ("commerce_performance",)),
    _metric("metric-transaction-average-price", _VIEW_PRODUCT_COST_DAILY,
            ("成交均价", "加权成交均价"), ("sales_amount", "quantity"),
            ("avg",), "avg", True, ("commerce_performance",)),
    _metric("metric-listing-price", _VIEW_LISTING_ITEMS,
            ("上架价", "标价"), ("list_amount",),
            ("avg", "min", "max"), "avg", False, ("listing_price_audit",)),
    _metric("metric-campaign-price", _VIEW_LISTING_ITEMS,
            ("活动价", "促销价"), ("campaign_amount",),
            ("avg", "min", "max"), "avg", False, ("listing_price_audit",)),
    _metric("metric-physical-available-quantity", _VIEW_PHYSICAL_STOCK_ITEMS,
            ("实物可用量", "实物库存", "库存池可用量"), ("available_quantity",),
            ("sum", "min", "max"), "sum", False, ("inventory_watch",)),
    _metric("metric-inbound-quantity", _VIEW_PHYSICAL_STOCK_ITEMS,
            ("入库在途量", "入库量"), ("inbound_quantity",),
            ("sum",), "sum", False, ("inventory_watch",)),
    _metric("metric-locked-quantity", _VIEW_PHYSICAL_STOCK_ITEMS,
            ("锁定量", "占用量"), ("locked_quantity",),
            ("sum",), "sum", False, ("inventory_watch",)),
    _metric("metric-channel-sellable-quantity", _VIEW_CHANNEL_STOCK_ITEMS,
            ("店铺可售库存", "渠道库存", "可售库存"), ("sellable_quantity",),
            ("sum", "min", "max"), "sum", False, ("inventory_watch",)),
    _metric("metric-payment-flow-amount", _VIEW_PAYMENTS,
            ("支付流水金额",), ("amount",), ("sum",), "sum", True, TRADE_DOMAINS),
    _metric("metric-refund-record-amount", _VIEW_REFUNDS,
            ("平台原始退款金额",), ("raw_platform_amount",),
            ("sum",), "sum", True, TRADE_DOMAINS),
)


CATALOG = SemanticCatalog(
    version=SEMANTIC_CATALOG_VERSION,
    entities=ENTITIES,
    fields=FIELDS,
    metrics=METRICS,
    views=VIEWS,
    joins=JOINS,
)


# --- 索引与版本快照 -------------------------------------------------------------

@dataclass(frozen=True)
class CatalogIndexes:
    """按 ref 建的五张只读索引。键都是 ref，值都是契约对象本身。"""

    entities: Mapping[str, SemanticEntity]
    fields: Mapping[str, SemanticField]
    metrics: Mapping[str, SemanticMetric]
    views: Mapping[str, SemanticView]
    joins: Mapping[str, SemanticJoin]


# 集合名 → 该集合唯一允许的元素类型：Task 1 的契约层只看"有没有 ref"，
# 具体类型在这一层判（把 view 塞进 fields 会让索引静默错用）。
_COLLECTION_KINDS: tuple[tuple[str, type], ...] = (
    ("entities", SemanticEntity),
    ("fields", SemanticField),
    ("metrics", SemanticMetric),
    ("views", SemanticView),
    ("joins", SemanticJoin),
)


def _build_indexes(catalog: SemanticCatalog) -> CatalogIndexes:
    proxies: dict[str, Mapping[str, object]] = {}
    for kind, item_type in _COLLECTION_KINDS:
        entries: dict[str, object] = {}
        for item in getattr(catalog, kind):
            if not isinstance(item, item_type):
                _fail("wrong_collection_type", f"{kind}:{getattr(item, 'ref', '?')}")
            if item.ref in entries:
                _fail("duplicate_ref", item.ref)
            entries[item.ref] = item
        proxies[kind] = MappingProxyType(entries)
    return CatalogIndexes(**proxies)


def catalog_indexes(catalog: SemanticCatalog) -> CatalogIndexes:
    """建（或不建）一份只读索引。

    每次都新建：入参是普通 frozen dataclass，缓存会诱导调用方在构造完之后
    再改集合（绕过契约层就能做到），那时索引与目录就永久分叉了。校验只在
    导入时对真目录跑一次，索引本身不缓存。
    """
    return _build_indexes(catalog)


# 目录是"版本 → 快照"的映射，不是一行可改写的常量。推进版本时必须把旧快照留在这里
# 供历史 Artifact 解释，而检索侧只认 current_versions 里那一版（Task 3）。
CATALOGS_BY_VERSION: Mapping[str, SemanticCatalog] = MappingProxyType(
    {CATALOG.version: CATALOG})


def catalog_for_version(version: str) -> SemanticCatalog | None:
    """取某版本的目录快照；未知版本返回 None，不给默认目录。"""
    try:
        return CATALOGS_BY_VERSION[version]
    except (KeyError, TypeError):
        return None


# --- 闭包校验 -------------------------------------------------------------------

def _check_terms(kind: str, ref: str, attribute: str, terms: tuple[str, ...]) -> None:
    # grain / allowed_group_grains 是 Task 3 的匹配键：带空格的项永远不会命中，
    # 那是"看起来登记了、实际答不了"的静默失效，因此这里（而不是自然语言别名上）拦。
    for term in terms:
        if term != term.strip():
            _fail("padded_term", f"{kind}:{ref}.{attribute}")


def _validate_fields(catalog: SemanticCatalog, indexes: CatalogIndexes) -> None:
    listed: set[str] = set()
    for view in catalog.views:
        listed.update(view.field_refs)
    for field in catalog.fields:
        if field.data_type not in DATA_TYPE_SQL_FAMILIES:
            _fail("unrepresentable_data_type", field.ref)
        owner = indexes.views.get(field.view_ref)
        if owner is None:
            _fail("dangling_view_ref", field.ref)
        if field.ref not in listed:
            _fail("orphan_field", field.ref)
        # 授权列与内部列不需要"对模型的叫法"；其它字段一个别名都没有就是检索不到。
        if field.role in {"dimension", "measure", "time"} and not field.aliases:
            _fail("field_without_aliases", field.ref)


def _validate_views(catalog: SemanticCatalog, indexes: CatalogIndexes) -> None:
    for view in catalog.views:
        if view.schema != "reporting":
            _fail("schema_not_reporting", view.ref)
        if view.entity_ref not in indexes.entities:
            _fail("dangling_entity_ref", view.ref)
        for ref in view.field_refs:
            field = indexes.fields.get(ref)
            if field is None:
                _fail("dangling_field_ref", ref)
            if field.view_ref != view.ref:
                _fail("misowned_field", f"{view.ref}:{ref}")
        authorization = indexes.fields.get(view.authorization_field_ref)
        if authorization is None:
            _fail("dangling_field_ref", view.authorization_field_ref)
        if view.authorization_field_ref not in view.field_refs:
            _fail("authorization_field_not_in_view", view.ref)
        if authorization.role != "authorization":
            _fail("authorization_role_required", view.authorization_field_ref)
        _check_terms("view", view.ref, "grain", view.grain)


def _validate_joins(catalog: SemanticCatalog, indexes: CatalogIndexes) -> None:
    for edge in catalog.joins:
        left = indexes.views.get(edge.left_view_ref)
        right = indexes.views.get(edge.right_view_ref)
        if left is None or right is None:
            _fail("dangling_view_ref", edge.ref)
        if left.ref == right.ref:
            _fail("join_same_view", edge.ref)
        for view, ref, side in ((left, edge.left_field_ref, "left"),
                                (right, edge.right_field_ref, "right")):
            key = indexes.fields.get(ref)
            if key is None:
                _fail("dangling_field_ref", f"{edge.ref}:{side}")
            if key.view_ref != view.ref:
                _fail("join_key_not_in_view", f"{edge.ref}:{side}")
            # 边的两侧必须就是两张视图各自的授权列：否则这条边能绕过作用域，
            # 把没获准的店铺行 JOIN 进来。
            if view.authorization_field_ref != ref:
                _fail("join_key_not_authorization", f"{edge.ref}:{side}")
        if edge.cardinality not in {"one_to_one", "many_to_one"}:
            # 会放大金额的基数在这一版根本不登记（Task 1 的词表也只有这两种）。
            _fail("cardinality_not_supported", edge.ref)
        if not edge.anti_amplification.strip():
            _fail("anti_amplification_blank", edge.ref)
        _check_terms("join", edge.ref, "allowed_group_grains", edge.allowed_group_grains)
        for grain in edge.allowed_group_grains:
            if grain not in left.grain and grain not in right.grain:
                _fail("join_grain_not_in_views", f"{edge.ref}:{grain}")


def _validate_metrics(catalog: SemanticCatalog, indexes: CatalogIndexes) -> None:
    for entry in catalog.metrics:
        if not entry.required_field_refs:
            _fail("metric_without_required_fields", entry.ref)
        owners: set[str] = set()
        for ref in entry.required_field_refs:
            field = indexes.fields.get(ref)
            if field is None:
                _fail("dangling_field_ref", f"{entry.ref}:{ref}")
            owners.add(field.view_ref)
        if len(owners) > 1:
            # 首批目录里这些视图之间没有合法 JOIN 边，跨视图的指标无法表达；
            # 要支持必须先登记一条经核对的 N:1 边，而不是让检索先猜。
            _fail("metric_fields_span_views", entry.ref)


def _validate_domains(catalog: SemanticCatalog) -> None:
    entries = tuple((f"{kind}:{item.ref}", item.domains)
                    for kind, collection in (("entity", catalog.entities),
                                             ("metric", catalog.metrics),
                                             ("view", catalog.views))
                    for item in collection)
    for where, domains in entries:
        for domain in domains:
            if not known_domain(domain):
                _fail("unknown_domain", f"{where}:{domain}")


def _validate_namespace(indexes: CatalogIndexes) -> None:
    # 同一份目录里 ref 是唯一的寻址空间：两个 kind 撞名时，索引会静默保留一个，
    # 而 Task 3 只看到"这个 ref 存在"。
    seen: dict[str, str] = {}
    for kind, collection in _COLLECTION_KINDS:
        for ref in getattr(indexes, kind):
            if ref in seen:
                _fail("duplicate_ref", f"{seen[ref]}:{kind}")
            seen[ref] = kind


def validate_catalog(catalog: SemanticCatalog) -> None:
    """整份目录必须闭包：任何悬空、错归属、可放大的边都在这里拒绝，不留给下游。

    失败即抛 `SemanticCatalogViolation`（`ValueError` 子类），消息以
    `semantic_catalog_*` 开头且可稳定复现（扫描顺序固定：集合形状 → 命名空间 →
    字段 → 视图 → 边 → 指标 → domain）。
    """
    for kind, _item_type in _COLLECTION_KINDS:
        if not getattr(catalog, kind):
            _fail("empty_collection", kind)
    indexes = _build_indexes(catalog)
    _validate_namespace(indexes)
    _validate_fields(catalog, indexes)
    _validate_views(catalog, indexes)
    _validate_joins(catalog, indexes)
    _validate_metrics(catalog, indexes)
    _validate_domains(catalog)


# --- 服务端解析：ref → SQL 标识符 -----------------------------------------------

def resolve_sql_identifier(catalog: SemanticCatalog, ref: str) -> tuple[str, ...]:
    """把已登记的 view/field ref 解析成 SQL 标识符元组。

    只接受 ref：带点号的原始 SQL 名（`reporting.v_shops`）、未登记对象、实体/指标/边
    一律 `KeyError`，并且不回显调用方给的字符串（那可能带着 SQL 片段）。
    """
    indexes = catalog_indexes(catalog)
    view = indexes.views.get(ref)
    if view is not None:
        return (view.schema, view.name)
    field = indexes.fields.get(ref)
    if field is not None:
        owner = indexes.views.get(field.view_ref)
        if owner is None:                       # validate_catalog 之后不可达，仍不放开
            raise KeyError("semantic_identifier_unresolved")
        return (owner.schema, owner.name, field.column)
    raise KeyError("semantic_identifier_unresolved")


# 导入即校验：目录不闭包时应用连启动都起不来，而不是等到某个问题检索到半个 ref。
validate_catalog(CATALOG)
