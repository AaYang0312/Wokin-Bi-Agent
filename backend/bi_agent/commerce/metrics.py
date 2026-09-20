"""商品参考指标的纯函数层：只吃已验证的销售行 / 同质汇总组，不碰数据库。

计划 Task 7 把这一层单独钉住，因为最容易出错的两件事都在这里：

1. **均价不能按均价算**。跨店成交均价必须是「总分摊金额 ÷ 总件数」；拿两家店的
   均价求平均会得到一个谁的账单上都不存在的数字（spec §10 第二行：1 件 / 100 元
   与 9 件 / 450 元的正确答案是 55 元，不是 75 元）。
2. **成本缺口不能冒充整体**。任何一行拿不到成本，完整商品毛利参考就是 null；把
   已知成本的那几行加起来当整体，比给 null 危险得多（spec §5.2）。

因此算术只有 `_aggregate` 一份：`compute_reference_metrics` 是计划钉住的**逐行**入口
（单位成本 × 件数），`combine_reference_metrics` 是图上跑的**汇总组**入口（视图在 SQL
端按 (店铺, 日, 行性质) 聚合，按行取数会先撞上行数上限，截断后的汇总比 null 更危险）。
两者喂给同一个累加器，所以"缺证据就不给数"的规则不会在两处各自演化。

本层不读库、不判门禁，也不拿能力标签：数据库读取在 `repository.py`，来源与能力词表
在 `sources.py`，门禁与降级在 `graph.py`；本模块只借用 `data_quality.money_text`
把金额渲染成与固定指标一致的文本（同一个渲染函数，分项与合计才不会对不上）。
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from decimal import ROUND_HALF_UP, Decimal
from types import MappingProxyType
from typing import Literal, get_args

from bi_agent.data_quality import money_text
from bi_agent.sources import platform_codes

# 口径版本：参考指标是本轮新增的面，单独一个版本号参与血缘，
# 只改商品参考口径时不该让固定指标的血缘版本跟着动。
COMMERCE_METRIC_VERSION = "commerce-metrics/2026-09-13.1"
COMMERCE_GRAPH_VERSION = "commerce_graph/2026-09-13.1"
# 趋势窗口首版固定七天（计划 §校验：trend_days 首版固定 7）；它参与请求指纹。
TREND_DAYS = 7
# 比率保留 6 位小数：与 numeric(20,6) 同一尺度，舍入规则显式写在这里而不是隐式。
RATIO_SCALE = Decimal("0.000001")

# ---------------------------------------------------------------------------
# 指标词表与口径
# ---------------------------------------------------------------------------

CommerceMetric = Literal[
    "sold_quantity", "sales_amount", "weighted_avg_paid_price",
    "product_gross_profit_reference", "product_gross_margin_reference",
    "erp_gross_profit_reference",
]
COMMERCE_METRICS: frozenset[str] = frozenset(get_args(CommerceMetric))

# 两个面：商品父行面（销量 / 金额 / 均价 / 商品毛利）与 ERP 单据面（单据毛利）。
# 分面而不是一张表，是因为它们一旦同行，早晚会被相加或相除。
PRODUCT_FACET_METRICS: frozenset[str] = frozenset(
    ("sold_quantity", "sales_amount", "weighted_avg_paid_price",
     "product_gross_profit_reference", "product_gross_margin_reference"))
DOCUMENT_FACET_METRICS: frozenset[str] = frozenset(("erp_gross_profit_reference",))
METRIC_FACET: dict[str, str] = {
    **{name: "product_reference" for name in PRODUCT_FACET_METRICS},
    **{name: "erp_document" for name in DOCUMENT_FACET_METRICS},
}

# 口径文本是持久化契约的一部分：`runtime.models` 逐字比对，改一个字旧 Artifact 就读不回。
COMMERCE_METRIC_DEFINITIONS: dict[str, str] = {
    "sold_quantity": "有效非赠品销售父项件数（ERP 有效销售父项口径，按 paid_at 归属，"
                     "套件/组合/加工以父项计，不含其 SKU 子件）",
    "sales_amount": "有效非赠品销售父项的行级分摊支付金额合计（人民币，退款前，"
                    "未扣售后/平台费/运费/广告费）",
    "weighted_avg_paid_price": "成交均价=同一行集合的分摊支付金额÷件数"
                               "（件数为 0 时 null；不平均日均价或店铺均价）",
    "product_gross_profit_reference": "商品毛利参考=SUM(分摊支付金额-行单位成本×件数)，"
                                      "只含成本与分摊都已核验的普通销售行；"
                                      "覆盖不全时为 null；未扣售后/平台费/运费/广告费，"
                                      "不是净利润",
    "product_gross_margin_reference": "商品毛利参考率=同一行集合的商品毛利参考÷同集合收入"
                                      "（分母为 0 或成本覆盖不全时 null；不平均店铺利润率）",
    "erp_gross_profit_reference": "ERP 毛利参考=bi.orders.raw_gross_profit 按唯一 ERP 单据"
                                  "聚合（单据口径，与商品毛利分面展示，不相加也不分摊到商品）",
}

# 报告指标 → 来源注册表里已登记的指标能力标签。报告指标名是业务口径，能力标签是
# "这个来源能不能回答这个问题"，两者不是一回事，所以映射只写这一处。
COMMERCE_METRIC_CAPABILITIES: dict[str, tuple[str, ...]] = {
    "sold_quantity": ("quantity",),
    "sales_amount": ("product_paid_amount",),
    "weighted_avg_paid_price": ("quantity", "product_paid_amount"),
    "product_gross_profit_reference": ("product_paid_amount",),
    "product_gross_margin_reference": ("quantity", "product_paid_amount"),
    "erp_gross_profit_reference": ("erp_documents",),
}

# 单位：spec §3 要求结果自带单位，不能只靠口径文本让模型自己认。
MetricUnit = Literal["piece", "CNY", "ratio"]
COMMERCE_METRIC_UNITS: dict[str, MetricUnit] = {
    "sold_quantity": "piece",
    "sales_amount": "CNY",
    "weighted_avg_paid_price": "CNY",
    "product_gross_profit_reference": "CNY",
    "product_gross_margin_reference": "ratio",
    "erp_gross_profit_reference": "CNY",
}

# 图表轴上允许出现的列（spec §8 的 `x` / `y`）：与结果列同一归属，不在展示层重拄。
# `y` 只能是报告指标：`paid_amount` / `paid_orders` 那一面没有登记过单位契约，
# 不进轴——宁可少一张图，也不要一张要靠猜单位才能读的图。
CHART_Y_COLUMNS: frozenset[str] = frozenset(COMMERCE_METRICS)
CHART_METRIC_UNITS: Mapping[str, MetricUnit] = dict(COMMERCE_METRIC_UNITS)
# `x` / `series` 只能是分组键或日期：分组键就这两个（店铺、平台），没有自由维度。
CHART_X_COLUMNS: frozenset[str] = frozenset({"platform", "shop_ref", "day"})
CHART_SERIES_COLUMNS: frozenset[str] = frozenset({"platform", "shop_ref", "line_kind"})

# ---------------------------------------------------------------------------
# 投影契约（单一真源，与 promotion.py 同一形状）
# ---------------------------------------------------------------------------

# 列名 -> 校验类别。`shop_ref`/`day`/`line_kind`/`currency`/`erp_documents`/
# `paid_amount`/`paid_orders` 已在固定指标侧声明，这里只补商品参考面新增的列。
# `platform` 是**标签**列：取值只能是注册表登记过的平台码（`runtime.models` 按
# `PLATFORM_GROUP_CODES` 逐项校验），不给载荷留第二条自由文本通道。
COMMERCE_COLUMN_KINDS: Mapping[str, str] = {
    "sold_quantity": "decimal",
    "sales_amount": "decimal",
    "weighted_avg_paid_price": "decimal",
    "product_gross_profit_reference": "decimal",
    "product_gross_margin_reference": "decimal",
    "sales_share": "decimal",
    "erp_gross_profit_reference": "decimal",
    "erp_documents_with_gross_profit": "int",
    "platform": "label",
}
COMMERCE_RESULT_COLUMNS = frozenset(COMMERCE_COLUMN_KINDS)
COMMERCE_NUMERIC_RESULT_COLUMNS = frozenset(
    key for key, kind in COMMERCE_COLUMN_KINDS.items() if kind in ("decimal", "int"))
# 每一列都必须声明校验类别，不放开成"字符串列由调用方自己保证"：
# 非数值列只允许 `label`（枚举取值），新增形态必须先在这里登记再使用。
assert {kind for kind in COMMERCE_COLUMN_KINDS.values()} <= {"decimal", "int", "label"}
assert COMMERCE_RESULT_COLUMNS == (
    COMMERCE_NUMERIC_RESULT_COLUMNS | {"platform"})

# 商品面与单据面各自能出现在哪些列上：两面同表的写法一律当场拒绝。
COMMERCE_PRODUCT_COLUMNS: frozenset[str] = frozenset(
    ("sold_quantity", "sales_amount", "weighted_avg_paid_price",
     "product_gross_profit_reference", "product_gross_margin_reference", "sales_share"))
COMMERCE_DOCUMENT_COLUMNS: frozenset[str] = frozenset(
    ("erp_gross_profit_reference", "erp_documents_with_gross_profit"))
assert (COMMERCE_PRODUCT_COLUMNS | COMMERCE_DOCUMENT_COLUMNS | {"platform"}
        == COMMERCE_RESULT_COLUMNS)
# 与固定指标 / 推广的列名不得重名：重名就意味着同一个词在两个域里指两件事。
assert not (COMMERCE_RESULT_COLUMNS
            & {"paid_amount", "paid_orders", "erp_documents", "aov", "refund_amount",
               "cash_difference", "cohort_refund_rate", "quantity",
               "product_paid_amount", "day", "shop_ref", "product_ref", "line_kind",
               "currency", "notice", "gift_quantity", "allocation_verified",
               "sku_label", "product_name", "product_name_snapshot"})

# 结果行的固定列形（**行原文形态**：带 shop_id / product_id 与名称列）。
# 白名单投影会把主键换成引用、把名称列从数据行里挑出去（只留在授权展示层的
# entities 里），所以名称列永远到不了模型载荷——它们在这里出现只是为了让
# `build_catalog` 能在同一份行数据上解析展示名。
NAME_COLUMNS: tuple[str, ...] = ("product_name", "product_name_snapshot", "sku_label")

# 商品面：一行 = (店铺, 行性质)；跨店合计行不带 shop_id，避免被读成"某家店"。
PRODUCT_ROWS: tuple[str, ...] = (
    "shop_id", "product_id", "line_kind", "sold_quantity", "sales_amount",
    "weighted_avg_paid_price", "product_gross_profit_reference",
    "product_gross_margin_reference", "sales_share", *NAME_COLUMNS)
PRODUCT_TOTAL_ROWS: tuple[str, ...] = tuple(k for k in PRODUCT_ROWS if k != "shop_id")
# 七日趋势：一行 = (店铺, 日)。缺失日的数值列一律 null，真实零成交才是 0。
TREND_ROWS: tuple[str, ...] = (
    "shop_id", "product_id", "day", "sold_quantity", "sales_amount")
# ERP 单据面：一行一家店，单据数与带毛利的单据数一起给，覆盖不全时毛利为 null。
DOCUMENT_ROWS: tuple[str, ...] = (
    "shop_id", "erp_documents", "erp_documents_with_gross_profit",
    "erp_gross_profit_reference")
# 已验证支付面：独立一张表，不与单据毛利同行，也不与商品面相加。
PAYMENT_ROWS: tuple[str, ...] = ("shop_id", "paid_amount", "paid_orders")

# 对比面（Task 8）：一行 = 一个分组（一家店或一个平台）。
# 与商品面不同，对比行**不逐行性质拆**：一家店的对比值就是它窗口内全部行性质的合计
# （`_window_value` 同一规则），拆成多行会把"这一组是多少"变成"这几行里挑一行"。
# 合计行不带分组键，含义与商品面的合计行一致：已评估集合的合计。
# 具体列形由 `comparison_row_columns` 按本轮请求的指标算：只发被请求的指标列，
# 不补一份"反正都是 null"的列（一列写着 sales_amount 就会被读成"算过了它"）。
COMPARISON_METRIC_ORDER: tuple[str, ...] = (
    "sold_quantity", "sales_amount", "weighted_avg_paid_price",
    "product_gross_profit_reference", "product_gross_margin_reference",
    "erp_documents", "erp_documents_with_gross_profit", "erp_gross_profit_reference",
    "paid_amount", "paid_orders")# 对比趋势：一行 = (分组, 日)。缺失日的数值列一律 null，真实零成交才是 0
# （与商品面 TREND_ROWS 同一条规则，折线图的断口就来自这些 null）。
COMPARISON_TREND_SHOP_ROWS: tuple[str, ...] = (
    "shop_id", "day", "sold_quantity", "sales_amount")
COMPARISON_TREND_PLATFORM_ROWS: tuple[str, ...] = (
    "platform", "day", "sold_quantity", "sales_amount")

# 允许出现在原文行里的列：本域新增列 + 复用的既有列 + 主键与名称列。
COMMERCE_ROW_COLUMNS: frozenset[str] = (
    COMMERCE_RESULT_COLUMNS
    | frozenset({"shop_id", "product_id", "day", "line_kind", "erp_documents",
                 "paid_amount", "paid_orders", *NAME_COLUMNS}))

# `combine_reference_metrics` / `compute_reference_metrics` 的输出列：算术层只产这五列，
# 对比面与商品面共用。写在这里而不是让调用方手抄：少一列就会在投影处多一个 null。
COMBINED_VALUE_COLUMNS: tuple[str, ...] = (
    "sold_quantity", "sales_amount", "weighted_avg_paid_price",
    "product_gross_profit_reference", "product_gross_margin_reference")
# 这两列不能相加：均价与率都要从合并后的总额重算（spec §5.2）。
RATIO_VALUE_COLUMNS: frozenset[str] = frozenset(
    ("weighted_avg_paid_price", "product_gross_margin_reference"))
# 计数列保持整数：行里是 int，合计也必须是 int。一列两种类型，早晚有人拿字符串
# 去比大小或者把它渲染成 "6" 后再四舍五入。
COUNT_VALUE_COLUMNS: frozenset[str] = frozenset(
    ("erp_documents", "erp_documents_with_gross_profit", "paid_orders"))
# 证据列跟着哪个报告指标的口径走：它们不是报告指标，没有自己的口径凭证条目，
# 可比性只能跟着把它们带进本轮的那个指标。
COLUMN_GOVERNING_METRIC: Mapping[str, str] = MappingProxyType({
    "erp_documents": "erp_gross_profit_reference",
    "erp_documents_with_gross_profit": "erp_gross_profit_reference"})
# 支付面两列本身就是已登记的能力标签：口径直接按标签解，不借商品面的凭证。
PAYMENT_CAPABILITY_COLUMNS: frozenset[str] = frozenset({"paid_amount", "paid_orders"})
# 能进排名块的列：只有报告指标。单据面的两份计数进合计行也进行，但"谁第一"对
# 一张单据数没有意义，而且它们没有自己的口径凭证条目可比。
COMPARISON_RANKED_COLUMNS: frozenset[str] = frozenset(COMMERCE_METRICS)


def comparison_row_columns(metrics: Sequence[str], *, payments: bool,
                           documents: bool = False) -> tuple[str, ...]:
    """本轮对比行的指标列（不含分组键）：按 `COMPARISON_METRIC_ORDER` 定序。

    只发被请求的指标列；支付面两列只在 `sales_basis=verified_payment` 时跟着出
    （它们是另一份事实集合，不能顶替商品面的 `sales_amount`）。单据面被请求时，
    两份**计数**跟着一起出：毛利列能不能发布取决于"几张单据 / 几张带毛利字段"，
    只给一个 null 就把可核对的证据藏起来了（与商品报告 DOCUMENT_ROWS 同一形状）。
    """
    wanted = {str(metric) for metric in metrics}
    if payments:
        wanted.update(PAYMENT_ROWS[1:])
    if documents:
        wanted.update(key for key in DOCUMENT_ROWS[1:] if key != "erp_gross_profit_reference")
    columns = tuple(key for key in COMPARISON_METRIC_ORDER if key in wanted)
    unknown = set(columns) - COMMERCE_ROW_COLUMNS
    if unknown:
        raise ValueError(f"commerce_column_undeclared:{sorted(unknown)}")
    return columns


def project(declared: tuple[str, ...], values: Mapping[str, object]) -> dict[str, object]:
    """按声明列顺序产出结果行；缺列或多列都是开发期缺陷，当场失败。

    严格是有意的：新增一列却忘了改声明，应当在生产方这一侧炸掉，而不是让投影层
    静默少一个字段——后者会让一份报告看起来"就是没有毛利这一列"。
    """
    extra = set(values) - set(declared)
    missing = set(declared) - set(values)
    if extra or missing:
        raise ValueError(
            f"commerce_column_drift:{sorted(missing)}:{sorted(extra)}")
    unknown = set(declared) - COMMERCE_ROW_COLUMNS
    if unknown:
        raise ValueError(f"commerce_column_undeclared:{sorted(unknown)}")
    return {key: values[key] for key in declared}


# ---------------------------------------------------------------------------
# 披露文本（进 `runtime.models` 的公开限制白名单）
# ---------------------------------------------------------------------------

COMMERCE_PUBLIC_LIMITATIONS = frozenset({
    "商品毛利参考未扣售后、平台费、运费与广告费，不是净利润",
    "商品毛利参考不含套件/组合/加工父项：这些行的成本语义未核验",
    "分摊金额未核验的销售父项只发布件数，不发布销售金额",
    "商品毛利与 ERP 单据毛利是两个口径面，不能相加、相除或互相分摊",
    "已验证支付口径没有商品级事实，商品销量与金额不能按该口径给出",
    "低利润阈值与最小样本没有版本化规则，只提供排序候选，不推导投放回报或预算",
    "不含 shop_ref 的行是已评估店铺集合的合计，不等于全部获准店铺合计",
    "趋势窗口覆盖不足，缺失日按 null 单独留 gap，不滑到另一组七天",
    "授权范围内没有可分析的店铺",
    "商品未解析出来，不能当成销量为 0",
    # 对比面（Task 8）：分组规则、合计分母、支付面与图表降级各一句固定文本。
    "淘宝与天猫本轮没有已批准的版本化合并规则，按两个平台分列，不并成一个淘系组",
    "不含分组键的行是已发布分组的合计，不等于全部获准范围合计",
    "已验证支付口径下只能按支付面两列对比：报告指标的定义是 ERP 有效销售父项口径",
    "本轮没有可画的指标：图表只引用同口径的已落库数据集，没有可画集合时只发表格",
    "有分组的单元格算不出来（本轮没有可发布的事实行），原因见各面覆盖披露",
})

# 参数化披露：家数 / 行数可变，句式固定。金额片段与 `runtime.models._MONEY` 同源。
_MONEY = r"(?:0|[1-9][0-9]*)(?:\.[0-9]+)?"
# 分组键片段：平台码（`sources.platform_codes`）或 `ent-` 店铺引用都是这一类短标识，
# 同一个字符集不拄第二份；店铺主键与真实店名仍然进不了披露文本。
_GROUP_KEYS = r"[a-z0-9][a-z0-9_-]{0,15}(?:、[a-z0-9][a-z0-9_-]{0,15})*"
COMMERCE_LIMITATION_PATTERNS: tuple[re.Pattern[str], ...] = (
    # 范围收缩：家数与缺口原因数可变，句式固定；店铺主键不出现在文本里。
    re.compile(r"^[0-9]+ 家获准店铺未列入本次合计，原因见 excluded_scope$"),
    re.compile(r"^成本覆盖不全：" + _MONEY + r"/" + _MONEY
               + r" 行有行成本，未发布完整商品毛利参考$"),
    re.compile(r"^ERP 单据毛利覆盖不全：" + _MONEY + r"/" + _MONEY
               + r" 张单据带毛利字段，未发布单据毛利参考$"),
    re.compile(r"^ERP 单据集合含 " + _MONEY + r" 张拆单子单与 " + _MONEY
               + r" 张合单，与平台订单不是一对一$"),
    re.compile(r"^七日趋势含 " + _MONEY + r" 天真实零成交，与缺失日分列$"),
    re.compile(r"^[0-9]+ 家店铺在本轮窗口内没有该商品的成交行，按真实 0 计入$"),
    # 商品歧义：家数可变，句式固定；候选主键与匹配文本都不进披露。
    re.compile(r"^[0-9]+ 个候选商品命中同一文本，请改用商品引用后重试$"),
    # 对比面（Task 8）：家数与分组键可变，句式固定。
    re.compile(r"^[0-9]+ 个分组仍有获准店铺未被评估，不发布该分组数字："
               + _GROUP_KEYS + r"$"),
    re.compile(r"^[0-9]+ 个指标的合计已拒答（有分组拿不出该指标的值或口径不一致）$"),
)

COMMERCE_LIMITATION_CODES = frozenset({
    "commerce_scope_excluded",
    # 多个商品命中同一文本：缺的是一个确定选择器，不是能力也不是数据。
    "ambiguous_product",
    "cost_coverage_incomplete",
    "cost_semantics_unverified",
    "allocation_unverified",
    "gross_profit_not_net",
    "erp_document_coverage_incomplete",
    "erp_document_granularity",
    "facets_not_additive",
    "payment_product_attribution_unavailable",
    "opportunity_policy_unconfigured",
    "trend_coverage_incomplete",
    "trend_zero_days",
    "product_zero_rows",
    "product_not_resolved",
    "empty_scope",
    # 对比面（Task 8）：分组完整性、合计拒答、图表降级与平台分组规则各归一个码。
    # 跨组口径不一致沿用既有的 `basis_incompatible`，不另起第二个名字。
    "comparison_group_partial",
    "comparison_total_withheld",
    # 分组可答但本轮没有可发布的事实行：单元格留 null 的独立原因。
    "comparison_cell_withheld",
    "comparison_chart_unavailable",
    "platform_group_rule_unconfigured",
})

# ---------------------------------------------------------------------------
# 状态词表（spec §3、§4）
# ---------------------------------------------------------------------------

ScopeMode = Literal["all_authorized", "selected"]
SalesBasis = Literal["erp_effective_parent", "verified_payment"]
ProfitBasis = Literal["none", "existing_fields"]
ComparisonMode = Literal["none", "previous_period"]
MetricStatusValue = Literal["available", "missing", "unsupported", "incomparable"]
METRIC_STATUS_VALUES: frozenset[str] = frozenset(get_args(MetricStatusValue))
# 对比报告的分组状态（spec §8：无数据平台保留原因标签）。只登记真发得出的两种：
#   complete —— 本组全部获准店铺都被评估过，该组可以发布数字
#   partial  —— 本组仍有获准店铺未被评估 -> 不发该组数字，也不进合计与排名
# 组内口径不一致不是一种分组状态：它就是这一组里某些指标的单元格不可算，
# 已由 `metric_statuses` 与排名块的 `missing` 说过，不在这里再说一遍。
GroupStatusValue = Literal["complete", "partial"]
GROUP_STATUS_VALUES: frozenset[str] = frozenset(get_args(GroupStatusValue))
# 分组排名范围：只能取已评估集合，不拿它当全量排位。
RankingScope = Literal["evaluated_only"]
RANKING_SCOPES: frozenset[str] = frozenset(get_args(RankingScope))
# 一个指标的排名能走到哪一步（spec §3：“仅对同口径、同窗口、质量合格的子集排序，
# 并明确排名范围”）：
#   complete     —— 全部已发布分组同口径且都有值：排名与合计一起发
#   incomplete   —— 有分组拿不出该指标的值：值照发，名次一个都不给，缺的列在 missing 里
#   incomparable —— 分组间口径不一致：不排名也不合计，只按各分组自己的口径展示
RankingStatus = Literal["complete", "incomplete", "incomparable"]
RANKING_STATUSES: frozenset[str] = frozenset(get_args(RankingStatus))
ComparisonGroupBy = Literal["platform", "shop"]
COMPARISON_GROUP_BY: frozenset[str] = frozenset(get_args(ComparisonGroupBy))
# 分组维度 → 图表的分组键列：轴上的列名只在这一处与分组维度挂钩，
# 不两处各写一份词表。
COMPARISON_GROUP_COLUMN: dict[str, str] = {"platform": "platform", "shop": "shop_ref"}

# ---------------------------------------------------------------------------
# 平台分组规则（显式 tb / tm）
# ---------------------------------------------------------------------------
# 已登记的平台码（单一真源是来源注册表）：对比图的分组键取值只能来自这里。
PLATFORM_GROUP_CODES: frozenset[str] = frozenset(platform_codes())
# 本轮没有已批准的版本化合并规则：`{}` 就是“不合并”这个决定本身。
# 一旦配上（例如把 tb/tm 并成一个“淘系”组），这里换成 {"tb": "taoxi", "tm": "taoxi"}
# 并把 `PLATFORM_GROUP_RULE` 推进到已批准规则的版本号：分组一变，旧 Artifact 的
# “同一个组”就不再成立。
PLATFORM_GROUP_MERGES: Mapping[str, str] = MappingProxyType({})
PLATFORM_GROUP_RULE = "platform-groups/2026-09-14.1"
# 淘系两家（tb / tm）是这份规则要回答的那个具体问题：两者同时出现在一张对比表里时，
# 必须把“没合并”说在明面上，不让人自己猜“为什么没有淘系合计”。
TAOBAO_FAMILY_PLATFORMS: frozenset[str] = frozenset({"tb", "tm"})


def platform_group_of(platform: str) -> str:
    """平台码 → 分组键。本轮恒等：淘宝与天猫各成一组（spec §3）。"""
    return PLATFORM_GROUP_MERGES.get(platform, platform)


OpportunityStatus = Literal["configured", "unconfigured"]
OPPORTUNITY_STATUSES: frozenset[str] = frozenset(get_args(OpportunityStatus))

# 排除原因与固定指标查询同一批归因码：同一种缺口不能在两个领域里各起一个名字。
ExcludedReason = Literal[
    "source_unregistered", "capability_unavailable", "capability_ungranted",
    "coverage_time_basis_unverified", "coverage_incomplete", "data_as_of_unknown",
    "source_quality_failed", "basis_incompatible", "shop_disabled", "shop_not_synced",
    # 分区块超过行数上限：与结果侧的 `result_too_large` 同一名称，不另造一个词。
    "result_too_large",
]
EXCLUDED_REASONS: frozenset[str] = frozenset(get_args(ExcludedReason))


# ---------------------------------------------------------------------------
# 十进制入参
# ---------------------------------------------------------------------------


def to_decimal(value: object) -> Decimal | None:
    """把行值读成 Decimal：只接受十进制串 / 整数 / Decimal，不接受 float。

    float 一进金额，0.1+0.2 那类误差就会被当成"数据本身有问题"。上游 SQL 给的本来就是
    numeric，测试里传 float 属于写法错误：直接拒绝比静默转换诚实。
    """
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError("commerce_metric_input_unusable")
    if isinstance(value, Decimal):
        return value
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return Decimal(text)
        except ArithmeticError:
            raise ValueError("commerce_metric_input_unusable") from None
    raise ValueError("commerce_metric_input_unusable")


def money_of(value: Decimal | None) -> str | None:
    """金额 / 比率的对外渲染：与固定指标同一个 money_text（原值去尾零，不做舍入）。"""
    return None if value is None else money_text(value)


def decimal_of(payload: Mapping[str, object], key: str) -> Decimal | None:
    """从已渲染的结果行里取回 Decimal（渲染是字符串，比较必须按数值）。"""
    return to_decimal(payload.get(key))


def _ratio(numerator: Decimal | None, denominator: Decimal | None) -> Decimal | None:
    """比率只在分子分母都在且分母为正时计算：0 分母是 null，不是 0。"""
    if numerator is None or denominator is None or denominator <= 0:
        return None
    return (numerator / denominator).quantize(RATIO_SCALE, rounding=ROUND_HALF_UP)


# ---------------------------------------------------------------------------
# 参考指标：一份算术，两个入口
# ---------------------------------------------------------------------------

def _aggregate(triples: Sequence[tuple[Decimal | None, ...]]) -> dict[str, str | None]:
    """件数 / 金额 / 成本合计 → 参考指标。三个汇总各自独立判定完整性。

    缺成本不该连带吞掉销量与金额（spec §10「缺历史成本或费用：利润不可计算，
    销量 / 金额仍可独立展示」）；但任何一项缺数值，就不能拿剩下的部分冒充整体。
    """
    quantity_total: Decimal | None = None if not triples else Decimal(0)
    amount_total: Decimal | None = None if not triples else Decimal(0)
    profit_total: Decimal | None = None if not triples else Decimal(0)
    for quantity, amount, cost_total in triples:  # type: ignore[misc]
        if quantity is None or quantity_total is None:
            quantity_total = None
        else:
            quantity_total += quantity
        if amount is None or amount_total is None:
            amount_total = None
        else:
            amount_total += amount
        cost_ready = (cost_total is not None and quantity is not None
                      and amount is not None and profit_total is not None)
        if not cost_ready:
            profit_total = None
        else:
            profit_total += amount - cost_total
    return {
        "sold_quantity": money_of(quantity_total),
        "sales_amount": money_of(amount_total),
        "weighted_avg_paid_price": money_of(_ratio(amount_total, quantity_total)),
        "product_gross_profit_reference": money_of(profit_total),
        "product_gross_margin_reference": money_of(
            _ratio(profit_total, amount_total)),
    }


def _line_cost_total(quantity: Decimal | None, unit_cost: Decimal | None) -> Decimal | None:
    if unit_cost is None:
        return None
    if quantity is None:
        # 件数未知时单位成本乘不出成本：按 0 件算等于把成本抹掉。
        return None
    return unit_cost * quantity


def compute_reference_metrics(
        lines: Iterable[Mapping[str, object]]) -> dict[str, str | None]:
    """已验证普通销售行 → 商品参考指标（金额一律 Decimal 串，缺失是 null 不是 0）。

    入参前提（由调用方负责成立，本函数不复验）：同币种、同单位、`active`、
    `line_kind='sale'`、`allocation_verified`。成本按**单位成本 × 件数**入账；
    任何一行 `raw_unit_cost` 为 null，完整商品毛利就是 null。
    """
    parsed = []
    for line in lines:
        row = dict(line)
        quantity = to_decimal(row.get("quantity"))
        parsed.append((quantity, to_decimal(row.get("allocated_paid_amount")),
                       _line_cost_total(quantity, to_decimal(row.get("raw_unit_cost")))))
    return _aggregate(parsed)


def combine_reference_metrics(
        groups: Iterable[Mapping[str, object]]) -> dict[str, str | None]:
    """把若干**同质**汇总组并成更大的汇总（跨行性质、跨店、跨日）。

    每组给 `quantity` / `sales_amount` / `cost_total`，其中 `cost_total` 为 null 表示
    该组成本或分摊未核验。合并规则与逐行一致：均价与毛利率都从合并后的总额重算，
    绝不平均已算好的均价；任何一组未核验，整体毛利参考就是 null。
    """
    return _aggregate([
        (to_decimal(row.get("quantity")), to_decimal(row.get("sales_amount")),
         to_decimal(row.get("cost_total")))
        for row in groups])


# ---------------------------------------------------------------------------
# 排序与候选：确定性、不猜阈值
# ---------------------------------------------------------------------------


def rank_shops(rows: Iterable[Mapping[str, object]], metric: str,
               *, descending: bool = True) -> list[dict[str, object]]:
    """按同一指标排序；null 不参与排名（当 0 排会把它压到最低档，等于凭空定罪）。

    排名范围必须是"同口径、同窗口、覆盖完整的已评估集合"，所以调用方传进来的行集
    必须与合计用的行集是同一份——`excluded_scope` 的店铺混进来时这里不会发现。
    """
    materialised = [dict(row) for row in rows]
    keyed = [(decimal_of(row, metric), index, row)
             for index, row in enumerate(materialised)]
    # 升序 = 取负后降序：两个方向共用一个比较器，不养出第二份排序规则。
    # （取负而不是写两份 key：`descending` 与输出方必须一致，否则同名指标
    # 会在两个地方被说成"第一"。）
    sign = Decimal(-1) if descending else Decimal(1)
    present = sorted([item for item in keyed if item[0] is not None],
                     key=lambda item: (sign * item[0],
                                       str(item[2].get("shop_ref") or ""), item[1]))
    absent = sorted([item for item in keyed if item[0] is None],
                    key=lambda item: str(item[2].get("shop_ref") or ""))
    ordered: list[dict[str, object]] = []
    for rank, (_value, _index, row) in enumerate(present, start=1):
        ordered.append({**row, "rank": rank})
    for _value, _index, row in absent:
        ordered.append({**row, "rank": None})
    return ordered


def sales_shares(rows: Iterable[Mapping[str, object]], metric: str,
                 total: Decimal | None) -> list[dict[str, object]]:
    """逐店份额 = 该店该指标 ÷ 已评估集合合计；合计缺失或为 0 时份额是 null。

    份额不强制凑成 1：舍入误差不重新分配，宁可留一个可核对的差，也不要一个
    "看起来刚好 100%" 的合成数。
    """
    materialised = [dict(row) for row in rows]
    for row in materialised:
        value = decimal_of(row, metric) if total is not None else None
        row["sales_share"] = money_of(_ratio(value, total)) if value is not None else None
    return materialised


def rank_groups(rows: Iterable[Mapping[str, object]], metric: str,
                *, descending: bool = True) -> list[dict[str, object]]:
    """对比分组排名：与 `rank_shops` 同一份比较器，只多一个分组键回退。

    店铺分组行带 `shop_ref`，平台分组行带 `platform`；两者共用一个排序，
    就不会长出第二套"谁第一"的规则。null 仍然不参排（当 0 排会把缺口压到最低档）。
    """
    materialised = [{**dict(row), "shop_ref": row.get("shop_ref") or row.get("platform")}
                    for row in rows]
    return [{key: value for key, value in row.items() if key != "shop_ref"}
            if row.get("platform") is not None else row
            for row in rank_shops(materialised, metric, descending=descending)]


def _number(row: Mapping[str, object], key: str) -> tuple[int, Decimal]:
    """排序键：null 一律排到最后（缺证据不抢“最低利润”的首位）。"""
    value = decimal_of(row, key)
    return (1, Decimal(0)) if value is None else (0, value)


def low_profit_candidates(rows: Iterable[Mapping[str, object]],
                          *, threshold: Decimal | None,
                          min_sample: Decimal | None) -> dict[str, object]:
    """低利润候选：没有版本化阈值与最小样本就只给排序，不给"该停投"的结论。

    候选顺序是**商品毛利参考额升序**（最低利润的先看到），同额再按毛利率升序：
    只按率排会把一件没卖几件的小店排到大店前面，而“低利润”问的是哪里在真的
    少赚。利润与率两列都随候选发出，排序可逐项核对；阈值判定仍按毛利率。
    spec §5.2 明确首版不虚构阈值、不声称 ROAS 最优，也不自动下预算建议。
    """
    materialised = [dict(row) for row in rows]
    # 先按毛利率升序（同值定序与 null 排尾都交给 rank_shops 一份规则），
    # 再按毛利参考额升序作主键：`sorted` 是稳定排序，同额内部仍保持率序。
    by_margin = rank_shops(materialised, "product_gross_margin_reference",
                           descending=False)
    ranked = sorted(by_margin, key=lambda row: _number(row, "product_gross_profit_reference"))
    candidates = [{"shop_ref": row.get("shop_ref"),
                   "product_gross_profit_reference": row.get(
                       "product_gross_profit_reference"),
                   "product_gross_margin_reference": row.get(
                       "product_gross_margin_reference"),
                   "sales_amount": row.get("sales_amount"),
                   "sold_quantity": row.get("sold_quantity")}
                  for row in ranked]
    if threshold is None or min_sample is None:
        return {"status": "unconfigured", "ranking_scope": "evaluated_only",
                "candidates": candidates, "flagged": []}
    flagged: list[object] = []
    for row in ranked:
        margin = decimal_of(row, "product_gross_margin_reference")
        sample = decimal_of(row, "sold_quantity")
        if margin is None or sample is None:
            continue                      # 缺证据的店不进 flagged，也不当 0 参加
        if margin <= threshold and sample >= min_sample:
            flagged.append(row.get("shop_ref"))
    return {"status": "configured", "ranking_scope": "evaluated_only",
            "candidates": candidates, "flagged": flagged}
