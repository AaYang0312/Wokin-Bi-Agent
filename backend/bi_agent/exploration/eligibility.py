"""固定 Tool 优先：能表达的就不许降级成 SQL（计划 Task 2 Step 3）。

判定只有一句话：**只要有一个固定 Tool 能同时覆盖请求要的全部指标和全部分组粒度，
就返回那个 Tool**，探索层没有第二次的机会。方向是故意单向的——固定 Tool 的可用性
赢过 capability / coverage：那两道门禁在下面几张图上会正确地拒答（来源未认证、
覆盖不全、口径未定），但如果这里因为"它大概也会拒"就放行 SQL，模型就得到了一条
绕过固定口径的路，同一个问题会有两个不同的数（总设计 §6.1、§6.4）。所以本模块
**不看** `selection.missing_concepts`，也**不看** `requires_clarification`。

三件事在这层是硬的：

1. `FIXED_TOOL_METRICS` 逐字取计划 Task 2 Step 3 那张表，不增不减：多写一个 ref
   就是凭空给某个 Tool 加能力（`metric-quantity` 是计划里的写法，目录里的"销量"
   ref 是 `metric-sold-quantity`；矩阵只按请求里的 ref 做集合判断，因此那个条目
   对今天的目录天然是空转——把它改写成 `metric-sold-quantity` 属于"新增 Tool 覆盖"，
   要改得先改计划）。
2. 分组粒度取各固定 Tool **真实契约**里能表达的那几种（下面每个条目都注了来源），
   并且是"整组匹配"：`QueryRequest.group_by` 与 `ComparisonGroupBy` 都是单值枚举，
   所以 `{day, shop}` 这种两维组合没有任何契约能表达，不能靠"每个维度各自能用"
   拼出来。
3. 重叠时的取舍是死的：按 `FIXED_TOOL_ORDER` 取第一个覆盖者。那个顺序就是计划里
   矩阵的书写顺序（也与 `agent._tool_schemas()` 的 Tool 顺序一致），所以既不是
   字母序也不是 dict 的偶然插入序。

`selection` 在本模块只有一个用途：它的 `catalog_version` 决定用哪一版目录把分组
ref 解成维度。目录版本不认识 ⇒ 解不出维度 ⇒ 不宣称任何 Tool 能表达（探索层往下走
会被编译器按 `exploration_catalog_version_mismatch` 拒掉），而不是猜一个当前目录。
"""

from __future__ import annotations

from types import MappingProxyType
from typing import Mapping

from bi_agent.exploration.models import ExplorationRequest
from bi_agent.semantic_catalog.models import SemanticSelection
from bi_agent.semantic_catalog.registry import catalog_indexes, catalog_for_version


FIXED_TOOL_METRICS: Mapping[str, frozenset[str]] = MappingProxyType({
    "query_business": frozenset({
        "metric-paid-amount", "metric-paid-orders", "metric-erp-documents",
        "metric-refund-amount", "metric-cash-difference", "metric-quantity",
        "metric-product-paid-amount"}),
    "analyze_product_performance": frozenset({
        "metric-sales-amount", "metric-quantity", "metric-cost-total",
        "metric-product-gross-profit-reference"}),
    "compare_performance": frozenset({
        "metric-sales-amount", "metric-quantity", "metric-cost-total",
        "metric-paid-amount", "metric-paid-orders",
        "metric-erp-gross-profit-reference"}),
    "audit_listing_prices": frozenset({"metric-listing-price"}),
    "inspect_inventory": frozenset({
        "metric-physical-available-quantity", "metric-channel-sellable-quantity"}),
})

# 重叠时的优先级 = 计划矩阵的书写顺序（也是 `_tool_schemas()` 里 Tool 的顺序）。
FIXED_TOOL_ORDER: tuple[str, ...] = (
    "query_business", "analyze_product_performance", "compare_performance",
    "audit_listing_prices", "inspect_inventory")

# 目录列名 → 粒度词（`SemanticView.grain` / `SemanticJoin.allowed_group_grains`
# 用的就是这一套词，不在这里发明第二个名字）。不在表里的列不是任何固定 Tool 的
# 分组维度（`normalization_status`、`captured_at`、`quality_status` 等），因此
# 按它们分组永远不会被宣称"固定 Tool 已能表达"。
GROUP_DIMENSION_BY_COLUMN: Mapping[str, str] = MappingProxyType({
    "shop_id": "shop",
    "pool_id": "pool",
    "warehouse_id": "warehouse",
    "listing_id": "listing",
    "product_id": "product",
    "line_kind": "line_kind",
    "day": "day",
    "platform": "platform",
    "erp_sku_id": "sku",
    "platform_sku_id": "sku",
})

_QUERY_BUSINESS_PRODUCT_METRICS = frozenset({
    "metric-quantity", "metric-product-paid-amount"})
_QUERY_BUSINESS_PERIOD_METRICS = FIXED_TOOL_METRICS["query_business"] \
    - _QUERY_BUSINESS_PRODUCT_METRICS

# 每个 Tool 的公开契约真正能表达的分组组合；`frozenset()` 读作"不带分期的合计"。
# * `query_business`：`QueryRequest.group_by` 的 Literal 是 total / day / shop / product。
# * `analyze_product_performance`：`ProductPerformanceRequest` 没有 group_by 旋钮；
#   它的报告行形就是 (商品, 店铺, 行性质) 加一份合计（`commerce.models.PRODUCT_ROWS`
#   与 `PRODUCT_TOTAL_ROWS`）。七日趋势的窗口是固定的 7 天，所以**按天分组不算它能
#   表达**：一个 90 天的按天问题不是那份报告。
# * `compare_performance`：`PerformanceComparisonRequest.group_by` 是必填的
#   `ComparisonGroupBy`（platform / shop），所以没有"合计"这一档。
# * `audit_listing_prices` / `inspect_inventory`：两个 Tool 的公开输入是
#   `applies_to`（all_selected / sku / shop_sku）与 `levels`（physical_total /
#   shop_sellable），各自的结果行形就是它们能给出的分组；两者都只有
#   `as_of="latest"`，没有历史窗口概念，所以也不含 `day`。
FIXED_TOOL_GROUPINGS: Mapping[str, frozenset[frozenset[str]]] = MappingProxyType({
    "query_business": frozenset({
        frozenset(), frozenset({"day"}), frozenset({"shop"}), frozenset({"product"})}),
    "analyze_product_performance": frozenset({
        frozenset(), frozenset({"product"}), frozenset({"shop"}), frozenset({"line_kind"})}),
    "compare_performance": frozenset({frozenset({"platform"}), frozenset({"shop"})}),
    "audit_listing_prices": frozenset({
        frozenset(), frozenset({"shop"}), frozenset({"listing"}), frozenset({"sku"})}),
    "inspect_inventory": frozenset({
        frozenset(), frozenset({"shop"}), frozenset({"pool"}), frozenset({"warehouse"}),
        frozenset({"listing"}), frozenset({"sku"}),
        frozenset({"pool", "warehouse"}), frozenset({"shop", "listing"})}),
})

# (Tool, 分组组合) → 该组合下契约真正能承载的指标。只有一处需要收窄：
# `QueryRequest._check_bounds` + `metrics.PRODUCT_METRICS` —— 商品面指标只能用
# `group_by=product`，反过来其他分组不许带商品指标。表里没有的组合用该 Tool 的全集。
FIXED_TOOL_GROUPING_METRICS: Mapping[tuple[str, frozenset[str]], frozenset[str]] = \
    MappingProxyType({
        ("query_business", frozenset()): _QUERY_BUSINESS_PERIOD_METRICS,
        ("query_business", frozenset({"day"})): _QUERY_BUSINESS_PERIOD_METRICS,
        ("query_business", frozenset({"shop"})): _QUERY_BUSINESS_PERIOD_METRICS,
        ("query_business", frozenset({"product"})): _QUERY_BUSINESS_PRODUCT_METRICS,
    })


def fixed_tool_for(selection: SemanticSelection, request: ExplorationRequest) -> str | None:
    """返回能表达这份请求的固定 Tool 名；没有任何 Tool 能表达时返回 `None`。

    `None` 只表示"固定 Tool 这条路走不通"，不表示可以查库：能力、覆盖、AST、
    成本与授权门禁一层都还在前面（计划 Task 5 的图按顺序跑）。
    """
    if not isinstance(selection, SemanticSelection):
        raise TypeError("exploration_selection_contract_required")
    if not isinstance(request, ExplorationRequest):
        raise TypeError("exploration_request_contract_required")

    grouping = _request_grouping(request, selection.catalog_version)
    if grouping is None:
        # 分组解不出来（目录版本不认识或 ref 不在目录里）：不猜覆盖。
        return None
    metrics = frozenset(request.requested_metric_refs)
    for tool in FIXED_TOOL_ORDER:
        if grouping not in FIXED_TOOL_GROUPINGS[tool]:
            continue
        carried = FIXED_TOOL_GROUPING_METRICS.get((tool, grouping), FIXED_TOOL_METRICS[tool])
        if metrics <= carried:
            return tool
    return None


def _request_grouping(request: ExplorationRequest, catalog_version: str):
    """请求的分组粒度 → 粒度词集合；任何一个维度解不出来就返回 `None`。

    没有分组就是"合计"，不需要目录也能判，因此返回空的 `frozenset`。反过来，目录
    版本拿不到字段索引时一律 `None`：宁可不宣称覆盖，也不拿当前目录去解释一份旧
    选择里的 ref（总设计 §5.4：旧版本只能用来读历史 Artifact）。
    """
    if not request.group_by_field_refs:
        return frozenset()
    catalog = catalog_for_version(catalog_version)
    if catalog is None:
        return None
    fields = catalog_indexes(catalog).fields
    dimensions = set()
    for ref in request.group_by_field_refs:
        field = fields.get(ref)
        if field is None:
            return None
        dimension = GROUP_DIMENSION_BY_COLUMN.get(field.column)
        if dimension is None:
            return None
        dimensions.add(dimension)
    return frozenset(dimensions)
