"""商品运营事实读取：只读获准 reporting 视图，一次集合查询取回全部面。

三条边界：

1. **不碰 `bi.*`**。聊天 API 以 `bi_app` 身份连接，它只有 reporting 视图的 SELECT；
   在这里写一条 `bi.order_items` 就会在真实部署里抛 permission denied，而不是给出
   安全的空结果。商品 / 成本 / 单据三张视图见 `sql/017_commerce_views.sql`。
2. **不在 SQL 里判定能不能发布**。视图只给出「几行有成本」「几张单据带毛利字段」这类
   覆盖率事实；是否把完整毛利置 null 由 `graph.py` 按门禁决定，避免同一规则在
   SQL 与 Python 各写一半。
3. **单据面永不连接订单行**。`v_erp_document_daily` 一行一张 ERP 单据，聚合只在这
   一层做；一张单三行把单头金额放大三倍是这类报告最典型的错法。
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any, NamedTuple, Sequence

from bi_agent.metrics import (  # noqa: F401  (预算与截断护栏只有一份实现)
    MAX_ROWS, _BudgetExhausted, _RowsTruncated, _SHOPS_SQL, _fetch_capped,
    _set_query_budget)

# 商品销售 + 成本面：一次取回 [窗口起, 窗口止) 内该商品在获准店铺的全部行。
# 主期间、趋势期间、上期比较共用这一条查询（调用方传入三段并集的窗口后在 Python 里
# 按天切分），这样"跨店合计"与"七日序列"来自同一个数据库快照。
_PRODUCT_LINES_SQL = """
SELECT shop_id, day, line_kind, quantity, gift_quantity, sales_amount,
       allocation_verified, line_count, cost_line_count, cost_quantity,
       cost_total, sku_ids
FROM reporting.v_product_cost_daily
WHERE shop_id = ANY(%s) AND product_id = %s AND day >= %s AND day < %s
ORDER BY shop_id, day, line_kind
LIMIT %s
"""

# ERP 单据毛利：唯一单据粒度聚合，不连接行表。
_ERP_DOCUMENTS_SQL = """
SELECT shop_id,
       count(*),
       count(*) FILTER (WHERE raw_gross_profit IS NOT NULL),
       sum(raw_gross_profit),
       count(*) FILTER (WHERE split_parent_id IS NOT NULL),
       count(*) FILTER (WHERE commercial_ids_count > 1)
FROM reporting.v_erp_document_daily
WHERE shop_id = ANY(%s) AND day >= %s AND day < %s
GROUP BY shop_id
ORDER BY shop_id
LIMIT %s
"""

# 已验证支付事实：一行一个商业支付事实，`verified` 门禁与固定指标同一列。
_PAYMENTS_SQL = """
SELECT shop_id,
       coalesce(sum(amount), 0),
       count(*)::bigint
FROM reporting.v_payments
WHERE shop_id = ANY(%s) AND verified AND paid_at >= %s AND paid_at < %s
GROUP BY shop_id
ORDER BY shop_id
LIMIT %s
"""

# S4 档案价参考事实：每家店**最新一份完整快照**里，该商品的商品级行
# （sys_sku_id=''）加上调用方点名的成交 SKU。未成交的目录 SKU 不参与参考价
# 归类，也不能因为目录行数把销售查询顶成 result_too_large。新鲜度仍由查询侧
# 按 captured_at 判定：SQL 只回答“最新一份里这些行是什么”。
_ITEM_PRICE_LATEST_SQL = """
WITH latest AS (
  SELECT DISTINCT ON (s.shop_id)
         s.shop_id, s.snapshot_id, s.captured_at
  FROM reporting.v_item_price_snapshots s
  WHERE s.scan_complete AND s.shop_id = ANY(%s)
  ORDER BY s.shop_id, s.captured_at DESC
)
SELECT i.shop_id, l.captured_at, i.sys_item_id, i.sys_sku_id,
       i.reference_price, i.purchase_price,
       i.item_active_status, i.sku_active_status
FROM latest l
JOIN reporting.v_item_price_snapshot_items i
  ON i.shop_id = l.shop_id AND i.snapshot_id = l.snapshot_id
WHERE i.sys_item_id = %s
  AND (i.sys_sku_id = '' OR i.sys_sku_id = ANY(%s))
ORDER BY i.shop_id, i.sys_sku_id
LIMIT %s
"""


# 出库口径分组面（S1）：`reporting.v_outstock_daily` 与 v_product_cost_daily 同一
# 套纳入条件与列形，唯独按发货时间（bi.orders.consigned_at）归日，且只含带发货
# 时间的单据。缺发货时间的行在视图里就不存在——出库口径不把它们当 0，也不借用
# paid_at 的归属；窗口完整性由查询侧的覆盖与完整性门禁判定。
_OUTSTOCK_GROUPS_SQL = """
SELECT shop_id, day, line_kind, sum(quantity), sum(gift_quantity),
       sum(sales_amount), bool_and(allocation_verified), sum(line_count),
       sum(cost_line_count), sum(cost_quantity), sum(cost_total)
FROM reporting.v_outstock_daily
WHERE shop_id = ANY(%s) AND day >= %s AND day < %s
GROUP BY shop_id, day, line_kind
ORDER BY shop_id, day, line_kind
LIMIT %s
"""

# 出库口径单据面：与 v_erp_document_daily 同列形，按发货时间归日。
_OUTSTOCK_DOCUMENTS_SQL = """
SELECT shop_id,
       count(*),
       count(*) FILTER (WHERE raw_gross_profit IS NOT NULL),
       sum(raw_gross_profit),
       count(*) FILTER (WHERE split_parent_id IS NOT NULL),
       count(*) FILTER (WHERE commercial_ids_count > 1)
FROM reporting.v_outstock_document_daily
WHERE shop_id = ANY(%s) AND day >= %s AND day < %s
GROUP BY shop_id
ORDER BY shop_id
LIMIT %s
"""

# 发货时间完整性探针：出库通道单据里缺 consigned_at 的数量（不分窗口——
# 缺发货时间的单据无法证明它落在请求窗口内还是外，存在即整店 fail closed）。
_CONSIGN_COMPLETENESS_SQL = """
SELECT shop_id, count(*)
FROM reporting.v_order_consign
WHERE shop_id = ANY(%s) AND source = ANY(%s) AND consigned_at IS NULL
GROUP BY shop_id
ORDER BY shop_id
LIMIT %s
"""

# 分组面（Task 8 对比报告）：与商品面**同一张视图、同一入条件**，只是不再按商品筛，
# 而在 SQL 端把同一 (店铺, 日, 行性质) 下的**全部商品**合起来。
# 两件原因：① 一家店的平台对比要的是整店经营量，不是某个商品；
#           ② 不先在 SQL 端聚合就会先撞 MAX_ROWS（一个中型店一天就能出几百个商品行），
#              而截断后的合计偏低，比不给数更危险。
# `line_count` / `cost_line_count` 是求和而不是取最大：成本覆盖判定跨商品仍然是
# “带成本的行 ÷ 全部行”，粒度变了规则不变。
_SHOP_GROUPS_SQL = """
SELECT shop_id, day, line_kind, sum(quantity), sum(gift_quantity),
       sum(sales_amount), bool_and(allocation_verified), sum(line_count),
       sum(cost_line_count), sum(cost_quantity), sum(cost_total)
FROM reporting.v_product_cost_daily
WHERE shop_id = ANY(%s) AND day >= %s AND day < %s
GROUP BY shop_id, day, line_kind
ORDER BY shop_id, day, line_kind
LIMIT %s
"""


class ProductLine(NamedTuple):
    """`(店铺, 日, 行性质)` 上的商品事实与成本覆盖证据。"""

    shop_id: str
    day: date
    line_kind: str
    quantity: Decimal
    gift_quantity: Decimal
    sales_amount: Decimal
    allocation_verified: bool
    line_count: int
    cost_line_count: int
    cost_quantity: Decimal
    cost_total: Decimal | None
    sku_ids: tuple[str, ...]


class DocumentFacts(NamedTuple):
    """一家店的 ERP 单据口径事实（单据数、带毛利单据数、合计与拆合单规模）。"""

    shop_id: str
    documents: int
    documents_with_gross_profit: int
    gross_profit: Decimal | None
    split_documents: int
    merged_documents: int


class PaymentFacts(NamedTuple):
    """一家店的已验证支付事实。"""

    shop_id: str
    paid_amount: Decimal
    paid_orders: int


class ShopProfile(NamedTuple):
    """服务端店铺档案：授权展开与来源 / 能力解析所需的全部字段。"""

    shop_id: str
    enabled: bool
    currency: str | None
    platform: str
    capabilities: frozenset[str]


class ItemPriceRow(NamedTuple):
    """一条档案价快照明细（商品级 sys_sku_id='' 或 SKU 级）。

    缺价格/成本是 NULL：data_missing 不是 0；active_status 取值 0/1，
    0=已停用档案，必须显式披露。
    """

    shop_id: str
    sys_item_id: str
    sys_sku_id: str
    reference_price: Decimal | None
    purchase_price: Decimal | None
    item_active_status: int
    sku_active_status: int | None


class ItemPriceSnapshot(NamedTuple):
    """一家店最新一份完整快照的档案价行与抓取时刻（新鲜度由查询侧判定）。"""

    shop_id: str
    captured_at: Any
    rows: dict[str, ItemPriceRow]


def shop_profiles(conn, shop_ids: list[str], *, deadline: float) -> list[ShopProfile]:
    """按 id 取店铺档案。

    SQL 与固定指标查询共用 `_SHOPS_SQL`：平台码与能力标签只能有一个来源，
    两处各查一次迟早会查出不一致的"这家店能不能回答"。
    """
    if not shop_ids:
        return []
    if not _set_query_budget(conn, deadline):
        raise _BudgetExhausted
    rows = conn.execute(_SHOPS_SQL, (shop_ids,)).fetchall()
    return [ShopProfile(shop_id=str(row[0]), enabled=bool(row[1]),
                        currency=row[2], platform=str(row[3] or "").strip().lower(),
                        capabilities=frozenset(str(item).strip()
                                               for item in (row[4] or [])
                                               if str(item or "").strip()))
            for row in rows]


def load_product_lines(conn, *, shop_ids: list[str], erp_product_id: str,
                       start: date, end: date, deadline: float) -> list[ProductLine]:
    """取该商品在获准店铺、`[start,end)` 内的全部销售父行（含成本覆盖证据）。

    返回行按 `(shop, day, line_kind)` 有序；`fetchall()` 前先设 statement 超时，
    命中 MAX_ROWS 一律抛 `_RowsTruncated`——截断后的合计偏低，不能冒充 ok。
    """
    if not shop_ids or not erp_product_id:
        return []
    if not _set_query_budget(conn, deadline):
        raise _BudgetExhausted
    raw = _fetch_capped(conn, _PRODUCT_LINES_SQL,
                        (shop_ids, erp_product_id, start, end))
    return [ProductLine(shop_id=str(row[0]), day=row[1], line_kind=str(row[2]),
                        quantity=row[3], gift_quantity=row[4], sales_amount=row[5],
                        allocation_verified=bool(row[6]), line_count=int(row[7]),
                        cost_line_count=int(row[8]), cost_quantity=row[9],
                        cost_total=row[10],
                        sku_ids=tuple(str(item) for item in (row[11] or [])
                                      if item is not None))
            for row in raw]


def load_shop_day_groups(conn, *, shop_ids: list[str], start: date, end: date,
                         deadline: float) -> list[ProductLine]:
    """取获准店铺在 `[start,end)` 内的**整店**销售与成本汇总行（不按商品筛）。

    返回行与 `load_product_lines` 完整同形（同一个 `ProductLine`），所以商品面与
    对比面共用同一套算术与同一套成本覆盖判定，不养出第二份“什叫成本齐了”。
    `sku_ids` 恒为空：SKU 明细属于商品面，对比面不回答“哪个规格”。

    一家店仍是**一条集合查询**：按店循环会把 N 家店变成 N 条查询，既耗尽预算
    也无法保证他们落在同一个快照上（spec §2）。
    """
    if not shop_ids:
        return []
    if not _set_query_budget(conn, deadline):
        raise _BudgetExhausted
    raw = _fetch_capped(conn, _SHOP_GROUPS_SQL, (shop_ids, start, end))
    return [ProductLine(shop_id=str(row[0]), day=row[1], line_kind=str(row[2]),
                        quantity=row[3], gift_quantity=row[4], sales_amount=row[5],
                        allocation_verified=bool(row[6]), line_count=int(row[7]),
                        cost_line_count=int(row[8]), cost_quantity=row[9],
                        cost_total=row[10], sku_ids=())
            for row in raw]


def load_document_facts(conn, *, shop_ids: list[str], start: date, end: date,
                        deadline: float) -> dict[str, DocumentFacts]:
    """按唯一 ERP 单据聚合的毛利参考事实（每家店一行）。"""
    if not shop_ids:
        return {}
    if not _set_query_budget(conn, deadline):
        raise _BudgetExhausted
    raw = _fetch_capped(conn, _ERP_DOCUMENTS_SQL, (shop_ids, start, end))
    return {str(row[0]): DocumentFacts(shop_id=str(row[0]), documents=int(row[1]),
                                       documents_with_gross_profit=int(row[2]),
                                       gross_profit=row[3],
                                       split_documents=int(row[4]),
                                       merged_documents=int(row[5]))
            for row in raw}


def load_outstock_shop_day_groups(conn, *, shop_ids: list[str], start: date,
                                  end: date, deadline: float) -> list[ProductLine]:
    """取获准店铺 `[start,end)` 内按**发货时间**归日的整店出库聚合行。

    与 `load_shop_day_group` 完整同形（同一 `ProductLine`、同一套算术与成本覆盖
    判定）；差别只有归属时间（consigned_at）与“缺发货时间的行不存在”。仍是一条
    集合查询：按店循环会把 N 家店变成 N 条查询。
    """
    if not shop_ids:
        return []
    if not _set_query_budget(conn, deadline):
        raise _BudgetExhausted
    raw = _fetch_capped(conn, _OUTSTOCK_GROUPS_SQL, (shop_ids, start, end))
    return [ProductLine(shop_id=str(row[0]), day=row[1], line_kind=str(row[2]),
                        quantity=row[3], gift_quantity=row[4], sales_amount=row[5],
                        allocation_verified=bool(row[6]), line_count=int(row[7]),
                        cost_line_count=int(row[8]), cost_quantity=row[9],
                        cost_total=row[10], sku_ids=())
            for row in raw]


def load_outstock_document_facts(conn, *, shop_ids: list[str], start: date,
                                 end: date, deadline: float) -> dict[str, DocumentFacts]:
    """出库口径的单据事实：按发货时间归日的唯一单据粒度聚合（每家店一行）。"""
    if not shop_ids:
        return {}
    if not _set_query_budget(conn, deadline):
        raise _BudgetExhausted
    raw = _fetch_capped(conn, _OUTSTOCK_DOCUMENTS_SQL, (shop_ids, start, end))
    return {str(row[0]): DocumentFacts(shop_id=str(row[0]), documents=int(row[1]),
                                       documents_with_gross_profit=int(row[2]),
                                       gross_profit=row[3],
                                       split_documents=int(row[4]),
                                       merged_documents=int(row[5]))
            for row in raw}


def load_consign_completeness(conn, *, shop_ids: list[str], sources: list[str],
                              deadline: float) -> dict[str, int]:
    """逐店统计已登记来源单据中缺发货时间（consigned_at IS NULL）的数量。

    一张单缺发货时间就无法证明它落在请求的出库窗口内还是外：count>0 的店整店
    fail closed，不补零、不拿 paid_at 顶替。一次集合查询，不逐店往返。
    """
    if not shop_ids or not sources:
        return {}
    if not _set_query_budget(conn, deadline):
        raise _BudgetExhausted
    raw = _fetch_capped(conn, _CONSIGN_COMPLETENESS_SQL, (shop_ids, sources))
    return {str(row[0]): int(row[1]) for row in raw}


def load_payment_facts(conn, *, shop_ids: list[str], start_ts: Any, end_ts: Any,
                       deadline: float) -> dict[str, PaymentFacts]:
    """按支付时间归属的已验证支付事实（每家店一行）。"""
    if not shop_ids:
        return {}
    if not _set_query_budget(conn, deadline):
        raise _BudgetExhausted
    raw = _fetch_capped(conn, _PAYMENTS_SQL, (shop_ids, start_ts, end_ts))
    return {str(row[0]): PaymentFacts(shop_id=str(row[0]), paid_amount=row[1],
                                      paid_orders=int(row[2]))
            for row in raw}


def load_item_prices(conn, *, shop_ids: list[str], erp_product_id: str,
                     deadline: float,
                     sku_ids: Sequence[str] = ()) -> dict[str, ItemPriceSnapshot]:
    """取获准店铺各自**最新一份完整**档案价快照中该商品的参考价行。

    一次集合查询：按店取最新 scan_complete 快照（DISTINCT ON + captured_at
    降序），再取商品级行，以及 `sku_ids` 里实际成交的 SKU。未成交目录行不读。
    快照过不过期由查询侧用 context.now 判定，这里不做时效决策；拼多多店铺
    永远没有快照行，天然 data_missing。成交 SKU 子集仍命中 MAX_ROWS 时抛
    `_RowsTruncated`：截断的参考面比缺失更危险，调用方应改记参考价缺失，
    而不是拿这张无日期快照去拒绝销售数字。
    """
    if not shop_ids or not erp_product_id:
        return {}
    if not _set_query_budget(conn, deadline):
        raise _BudgetExhausted
    # 空列表推断不出数组类型；没有成交 SKU 时用空串占位，SQL 仍会留下商品级行。
    wanted = sorted({str(sku) for sku in sku_ids if str(sku or "").strip()}) or [""]
    raw = _fetch_capped(conn, _ITEM_PRICE_LATEST_SQL,
                        (shop_ids, erp_product_id, wanted))
    snapshots: dict[str, ItemPriceSnapshot] = {}
    for row in raw:
        shop_id = str(row[0])
        captured_at = row[1]
        item = ItemPriceRow(
            shop_id=shop_id, sys_item_id=str(row[2]), sys_sku_id=str(row[3] or ""),
            reference_price=row[4], purchase_price=row[5],
            item_active_status=int(row[6]),
            sku_active_status=(int(row[7]) if row[7] is not None else None))
        existing = snapshots.get(shop_id)
        if existing is None or existing.captured_at == captured_at:
            rows = dict(existing.rows) if existing is not None else {}
            rows[item.sys_sku_id] = item
            snapshots[shop_id] = ItemPriceSnapshot(shop_id=shop_id,
                                                   captured_at=captured_at,
                                                   rows=rows)
    return snapshots


__all__ = [
    "MAX_ROWS",
    "DocumentFacts",
    "ItemPriceRow",
    "ItemPriceSnapshot",
    "PaymentFacts",
    "ProductLine",
    "ShopProfile",
    "load_consign_completeness",
    "load_document_facts",
    "load_item_prices",
    "load_outstock_document_facts",
    "load_outstock_shop_day_groups",
    "load_payment_facts",
    "load_product_lines",
    "load_shop_day_groups",
    "shop_profiles",
]
