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
from typing import Any, NamedTuple

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


__all__ = [
    "MAX_ROWS",
    "DocumentFacts",
    "PaymentFacts",
    "ProductLine",
    "ShopProfile",
    "load_document_facts",
    "load_payment_facts",
    "load_product_lines",
    "shop_profiles",
]
