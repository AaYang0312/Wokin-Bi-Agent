"""字段规范化、窗口分页、事务、水位、补查与同步CLI。

本模块内的事务函数不自行提交；上层窗口同步统一提交整个窗口。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import sys
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from .catalog import EntityKind, bump_catalog_version, ensure_refs
from .config import load_sync_settings
from .data_quality import (
    ENTITY_SOURCES, QUALITY_RULE, reconcile_source_quality)
from .kuaimai import KuaimaiClient, KuaimaiError, parse_page
from .sources import (
    AFTERSALE_SOURCE, ORDERS_ENTITY, OUTSTOCK_SOURCE, ShopRecord, TRADE_LIST_SOURCE,
    capabilities_from_evidence, platform_order_sources, resolve_order_source)

logger = logging.getLogger(__name__)

BEIJING = ZoneInfo("Asia/Shanghai")
# 业务时间下限：早于此的支付/完成时间只可能是 ERP 占位值，不参与时间窗口归属。
BUSINESS_TIME_FLOOR = datetime(2010, 1, 1, tzinfo=BEIJING)

# 三个通道名只有一个真源：bi_agent/sources.py 的注册表。本模块继续从那里引用，
# 避免同步与查询两侧各自维护一份平台路由表。
ORDER_SOURCE = TRADE_LIST_SOURCE
# 官方 erp.trade.list.query 明确排除淘系、拼多多订单；两类平台改走交易模块销售
# 出库通道，只有非敏感字段（收件人/买家昵称/平台支付金额等不返回）。
ITEM_SOURCE = "item.list.query"

# 平台→订单源路由：直接取自注册表。sync_state 主键含 source，出库通道与交易通道的
# 覆盖/水位互不干扰；未命中的平台不再回退默认源（见 `_shop_order_source`）。
ORDER_SOURCE_BY_PLATFORM = platform_order_sources()

# ---------------------------------------------------------------------------
# PII 红线（2026-09-12 淘系接入约定）：下列字段在出库/售后响应中出现（部分
# 平台值已脱敏但仍非空），一律不规范化、不入库、不写日志。入库字段集维持
# sql/001_init.sql 现有列，一个都不加；未来扩列评审必须先复核本清单，
# tests/test_core.py 以本清单断言规范化键集与列白名单不漂移。
# 注意：platformPaymentAmount 不属于 PII，只是淘系不返回（现有列自然为 NULL）。
# ---------------------------------------------------------------------------
PII_FORBIDDEN_FIELDS = frozenset({
    "buyerNick", "buyerMessage", "buyerName", "buyerPhone",
    "receiverName", "receiverPhone", "receiverMobile", "receiverAddress",
    "receiverState", "receiverCity", "receiverDistrict", "receiverStreet",
    "receiverZip", "receiverCountry", "taobaoId", "ptConsignTime",
    "invoiceName", "invoiceRemark", "invoiceKind", "tradeInvoice",
    "shopName", "sellerNick", "openUid", "mobileTail",
})

# 单实例同步锁；锁放在整个CLI运行入口，sync_window内部仅负责单窗口事务
LOCK_ID = 7319041
# 增量重叠；回填开始前记录T0，完成后补拉[T0,固定T1)
SYNC_OVERLAP = timedelta(minutes=10)
PAGE_SIZE = 200
# 同批退款tids补查单次ID数量；执行4.7时按官方文档或小样本确认后固定
COHORT_TIDS_BATCH = 50
MAX_QUERY_DAYS = 366

_REQUIRED_SYNC_COLUMNS = {
    "orders": frozenset({"unified_status", "system_status"}),
    "order_items": frozenset({"source_type"}),
    "products": frozenset({"title", "source_modified_at"}),
}


# ---------------------------------------------------------------------------
# 基础解析：金额与时间
# ---------------------------------------------------------------------------


def parse_business_timestamp(value: Any) -> datetime | None:
    """业务时间（支付 / 退款完成）专用解析：明显占位值一律按“未取得”处理。

    快麦对未付款 / 已关闭单会回 `payTime = 2000-01-01 00:00` 这类占位值
    （真实账号实测：WAIT_BUYER_PAY 与 CLOSED 单各一）。当成真实支付时间入库，
    会让从未付款的单独出现在支付日指标里，也会让它撑出一个“已核验支付事实”。
    置 NULL 后该单不落入任何时间窗口，金额依旧原样保留在 raw 字段，不猜也不补 0。
    """
    parsed = parse_timestamp(value)
    if parsed is not None and parsed < BUSINESS_TIME_FLOOR:
        return None
    return parsed


def to_decimal(value: Any) -> Decimal | None:
    """解析金额；缺失/非法（NaN、Infinity、非数值）返回None，负值保留由语义判断。"""
    if value is None or isinstance(value, bool):
        return None
    try:
        if isinstance(value, Decimal):
            number = value
        elif isinstance(value, (int, float, str)):
            number = Decimal(str(value).strip())
        else:
            return None
    except (InvalidOperation, ValueError):
        return None
    if not number.is_finite():
        return None
    return number


def parse_timestamp(value: Any) -> datetime | None:
    """解析快麦时间：毫秒整数或字符串（北京时间）。"""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float, Decimal)):
        return datetime.fromtimestamp(float(value) / 1000.0, tz=BEIJING)
    text = str(value).strip()
    if not text:
        return None
    if text.isdigit():
        return datetime.fromtimestamp(float(text) / 1000.0, tz=BEIJING)
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=BEIJING)
        except ValueError:
            continue
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=BEIJING)
    return parsed


# ---------------------------------------------------------------------------
# 交易规范化
# ---------------------------------------------------------------------------


def _unique(values) -> list[str]:
    seen: dict[str, None] = {}
    for value in values:
        text = (value or "").strip()
        if text and text not in seen:
            seen[text] = None
    return list(seen)


def _commercial_ids(raw: dict[str, Any], items: list[dict[str, Any]]) -> list[str]:
    values = [str(raw.get("tid") or "")]
    tids = raw.get("tids")
    if isinstance(tids, str):
        values.extend(tids.split(","))
    elif isinstance(tids, list):
        values.extend(str(t) for t in tids)
    values.extend(str(item["commercial_id"] or "") for item in items)
    return _unique(values)


def _source_int(value: Any) -> int | None:
    """解析 API 枚举整数；非单一整数不猜测。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value.strip())
    return None


def _source_statuses(value: Any) -> list[int]:
    """解析逗号分隔的售后状态，保留其全部判定语义。"""
    if isinstance(value, int) and not isinstance(value, bool):
        return [value]
    if not isinstance(value, str):
        return []
    values = [_source_int(part) for part in value.split(",")]
    return [item for item in values if item is not None]


def _source_bool(value: Any) -> bool | None:
    """解析店铺 API 的 active 标志；未知值交由调用方安全回退。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "enabled"}:
            return True
        if normalized in {"0", "false", "no", "disabled"}:
            return False
    return None


def assert_sync_schema(conn) -> None:
    """在任何同步写入前确认前向迁移已完成。"""
    table_names = sorted(_REQUIRED_SYNC_COLUMNS)
    column_names = sorted({column for columns in _REQUIRED_SYNC_COLUMNS.values()
                           for column in columns})
    rows = conn.execute(
        "SELECT table_name, column_name FROM information_schema.columns "
        "WHERE table_schema='bi' AND table_name = ANY(%s) AND column_name = ANY(%s)",
        (table_names, column_names),
    ).fetchall()
    present = {(str(table), str(column)) for table, column in rows}
    required = {(table, column) for table, columns in _REQUIRED_SYNC_COLUMNS.items()
                for column in columns}
    if not required <= present:
        raise KuaimaiError("schema_outdated")


def _normalise_item(raw_item: dict[str, Any], erp_id: str, index: int,
                    fallback_paid_at: datetime | None) -> dict[str, Any]:
    quantity = to_decimal(raw_item.get("num")) or Decimal(0)
    gift_quantity = to_decimal(raw_item.get("giftNum")) or Decimal(0)
    source_type = _source_int(raw_item.get("type"))
    # 只有纯赠品行（无销售数量）才整行判为赠品；混合行保留销售口径，
    # 赠品数量由 gift_quantity 单列承载，否则该行分摊金额会被商品排行误排除。
    if gift_quantity > 0 and quantity == 0:
        line_kind = "gift"
    else:
        line_kind = {
            0: "sale",
            2: "suite",
            3: "combination",
            4: "processing",
        }.get(source_type, "sale")
    allocated = to_decimal(raw_item.get("payAmount"))
    return {
        "line_id": str(raw_item.get("id") or raw_item.get("oid")
                       or f"{erp_id}#{index}"),
        "commercial_id": (str(raw_item.get("tid") or "").strip() or None),
        "platform_line_id": (str(raw_item.get("oid") or "").strip() or None),
        "source_type": source_type,
        "product_id": (str(raw_item.get("itemSysId") or "").strip() or None),
        "sku_id": (str(raw_item.get("skuSysId") or "").strip() or None),
        # 成交名称快照：ERP 侧商品名与 SKU 规格名原样留存。档案会改名，
        # 快照只用于留住“当时成交叫什么”；平台标题 title 不当商品名。
        "product_name_snapshot": (str(raw_item.get("sysTitle") or "").strip() or None),
        "sku_label_snapshot": (str(raw_item.get("sysSkuPropertiesName") or "").strip() or None),
        "paid_at": parse_timestamp(raw_item.get("payTime")) or fallback_paid_at,
        "quantity": quantity,
        "gift_quantity": gift_quantity,
        "raw_paid_amount": to_decimal(raw_item.get("payAmount")),
        "raw_payment": to_decimal(raw_item.get("payment")),
        "raw_unit_cost": to_decimal(raw_item.get("cost")),
        "allocated_paid_amount": allocated,
        "allocation_verified": False,
        "line_kind": line_kind,
        "active": True,
    }


def _split_parent_id(raw: dict[str, Any]) -> str | None:
    """拆单父单映射：交易查询用 splitParentId；出库通道为 splitSid
    （官方文档：splitType=1 时为拆单主单 sid，否则 -1）。-1/空视为无拆单。"""
    for key in ("splitParentId", "splitSid"):
        text = str(raw.get(key) or "").strip()
        if text and text != "-1":
            return text
    return None


def normalise_trade(raw: dict[str, Any], *, source: str = ORDER_SOURCE) -> dict[str, Any]:
    """白名单规范化一单ERP交易；缺少orders字段与合法空列表不同。"""
    erp_id = str(raw.get("sid") or "").strip()
    shop_id = str(raw.get("userId") or "").strip()
    source_updated_at = parse_timestamp(raw.get("updTime")) or parse_timestamp(raw.get("modified"))
    paid_at = parse_business_timestamp(raw.get("payTime"))
    raw_pay_amount = to_decimal(raw.get("payAmount"))
    status = "normal"
    if not erp_id or not shop_id or source_updated_at is None:
        status = "invalid"
    elif raw_pay_amount is not None and raw_pay_amount < 0:
        status = "needs_review"
    items_present = "orders" in raw
    raw_items = raw.get("orders") if isinstance(raw.get("orders"), list) else []
    items = [_normalise_item(item, erp_id, index, paid_at)
             for index, item in enumerate(raw_items) if isinstance(item, dict)]
    split_parent = (_split_parent_id(raw) if source == OUTSTOCK_SOURCE else
                    (str(raw.get("splitSid") or "").strip() or None
                     if _source_int(raw.get("splitType")) == 1 else None))
    unified_status = str(raw.get("unifiedStatus") or "").strip() or None
    system_status = str(raw.get("sysStatus") or "").strip() or None
    effective_status = unified_status if unified_status is not None else system_status
    active = (effective_status or "").upper() != "CLOSED"
    if source == OUTSTOCK_SOURCE:
        active = active and str(raw.get("status") or "").upper() not in {
            "TRADE_CLOSED", "CLOSED", "CANCELLED", "CANCELED", "CANCEL"}
        active = active and (system_status or "").upper() not in {"CANCEL", "CANCELLED", "CANCELED"}
    for item in items:
        item["active"] = active
    trade: dict[str, Any] = {
        "shop_id": shop_id,
        "erp_id": erp_id,
        "commercial_ids": _commercial_ids(raw, items),
        "split_parent_id": split_parent,
        "source": source,
        "source_updated_at": source_updated_at,
        "platform_modified_at": parse_timestamp(raw.get("modified")),
        "paid_at": paid_at,
        "raw_pay_amount": raw_pay_amount,
        "raw_payment": to_decimal(raw.get("payment")),
        "raw_platform_payment": to_decimal(raw.get("platformPaymentAmount")),
        "raw_cost": to_decimal(raw.get("cost")),
        "raw_gross_profit": to_decimal(raw.get("grossProfit")),
        "unified_status": unified_status,
        "system_status": system_status,
        "active": active,
        "normalization_status": status,
        "items_present": items_present,
        "items": items,
    }
    return trade


# ---------------------------------------------------------------------------
# 商业订单支付重建：单头或已核验行级分摊，禁止猜测
# ---------------------------------------------------------------------------


def _load_orders(conn, shop_id: str, commercial_id: str) -> list[tuple]:
    return conn.execute(
        "SELECT erp_id, commercial_ids, paid_at, raw_pay_amount, source_updated_at "
        "FROM bi.orders WHERE shop_id=%s AND commercial_ids @> %s "
        "AND (active OR (paid_at IS NOT NULL AND raw_pay_amount > 0))",
        (shop_id, [commercial_id]),
    ).fetchall()


def _merged_certifiable(conn, shop_id: str, all_orders: list[tuple]) -> bool:
    """快麦合单专项取证的成立条件。

    实测合单（一张 ERP 单挂多个 tid）的单头 payAmount 只等于其中**一个**子单
    （真实样本：单头 14.25，行级合计 386.05），所以单头永远与行对不上，
    行级金额反而才是完整证据。但只有三条全成立才能发证：

    1. 涉及的单据里确实有“一单多商业号”（否则就是拆单或真异常，不走这条路）；
    2. 这些单据的有效行全部带金额：存在无金额行就有未归属余额，不能声称行合计等于已付；
    3. 每行归属的商业号都在所属单据声明的 tid 列表内：不承认来路不明的行。
    """
    if not any(len(order[1] or []) >= 2 for order in all_orders):
        return False
    erp_ids = [order[0] for order in all_orders]
    if not erp_ids:
        return False
    row = conn.execute(
        "SELECT count(*) FILTER (WHERE i.allocated_paid_amount IS NULL), "
        "       count(*) FILTER (WHERE NOT (i.commercial_id = ANY(o.commercial_ids))) "
        "FROM bi.order_items i JOIN bi.orders o "
        "  ON o.shop_id = i.shop_id AND o.erp_id = i.erp_id "
        "WHERE i.shop_id=%s AND i.active AND i.erp_id = ANY(%s)",
        (shop_id, erp_ids),
    ).fetchone()
    return row[0] == 0 and row[1] == 0


def _determine_payment(conn, shop_id: str, commercial_id: str,
                       orders: list[tuple]) -> tuple[Decimal | None, datetime | None, str, bool, datetime | None]:
    """返回 (amount, paid_at, basis, verified, source_updated_at)。"""
    source_updated_at = max(order[4] for order in orders) if orders else None
    if not orders:
        return None, None, "orphan", False, source_updated_at
    if len(orders) == 1 and len(orders[0][1] or []) == 1:
        paid_at, head = orders[0][2], orders[0][3]
        if head is not None and head >= 0 and paid_at is not None:
            return head, paid_at, "head", True, source_updated_at
    # 行级分摊路径：涉及拆合单或单头不可用
    involved = _unique(
        cid for order in orders for cid in (order[1] or []))
    if not involved:
        return None, None, "undetermined", False, source_updated_at
    # 所有引用涉及商业单的有效订单都要参与交叉核对
    all_orders = conn.execute(
        "SELECT erp_id, commercial_ids, paid_at, raw_pay_amount, source_updated_at "
        "FROM bi.orders WHERE shop_id=%s AND commercial_ids && %s "
        "AND (active OR (paid_at IS NOT NULL AND raw_pay_amount > 0))",
        (shop_id, involved),
    ).fetchall()
    source_updated_at = max(order[4] for order in all_orders) if all_orders else source_updated_at
    heads = [order[3] for order in all_orders]
    total_head: Decimal | None
    if all(head is not None for head in heads):
        total_head = sum(heads, Decimal(0))
    else:
        total_head = None
    item_rows: dict[str, list[tuple[Decimal, datetime | None]]] = {}
    for cid in involved:
        item_rows[cid] = conn.execute(
            "SELECT i.allocated_paid_amount, i.paid_at FROM bi.order_items i "
            "JOIN bi.orders o ON o.shop_id=i.shop_id AND o.erp_id=i.erp_id "
            "WHERE i.shop_id=%s AND i.commercial_id=%s "
            "AND (i.active OR (NOT o.active AND o.paid_at IS NOT NULL "
            "                 AND o.raw_pay_amount > 0)) "
            "AND i.allocated_paid_amount IS NOT NULL",
            (shop_id, cid),
        ).fetchall()
        if not item_rows[cid]:
            return None, None, "undetermined", False, source_updated_at
    total_items = sum((amount for rows in item_rows.values() for amount, _ in rows), Decimal(0))
    basis = "items"
    if total_head is None or total_head != total_items:
        # 单头与行对不上：只有“完整合单”才能改用行级取证，否则继续不发证。
        if not _merged_certifiable(conn, shop_id, all_orders):
            return None, None, "undetermined", False, source_updated_at
        basis = "items_merged"
    target = item_rows[commercial_id]
    amount = sum((row[0] for row in target), Decimal(0))
    pay_times = {row[1] for row in target if row[1] is not None}
    paid_at = pay_times.pop() if len(pay_times) == 1 else None
    # 行级路径与单头路径同口径：负数分摊不能拿核验章（单头路径有 head >= 0 守卫）。
    verified = paid_at is not None and amount is not None and amount >= 0
    return amount, paid_at, basis, verified, source_updated_at


# 支付事实降级守卫：已核验的行只能被“不更弱”的证据覆盖。
# 拆合单/兄弟行尚未同步齐时 _determine_payment 会返回 undetermined(None/false)，
# 无条件覆盖会把已核验的100元清成 NULL；reporting.v_shop_daily 带 WHERE verified，
# 这部分收入就在全域报表里无声消失（C-5）。允许覆盖的方向：
#   1) 新证据本身已核验（升级或同级修正）；
#   2) 旧行本就未核验；
#   3) 已无任何订单支撑该商业单（orphan：source_updated_at 只能为 NULL，
#      因 bi.orders.source_updated_at 是 NOT NULL）；
#   4) 降级方向仅当新证据的 source_updated_at 严格更新才放行。
_PAYMENT_UPSERT_SQL = """
INSERT INTO bi.order_payments
    (shop_id, commercial_id, paid_at, amount, currency, basis, verified, source_updated_at)
VALUES (%s, %s, %s, %s, 'CNY', %s, %s, %s)
ON CONFLICT (shop_id, commercial_id) DO UPDATE SET
    paid_at = EXCLUDED.paid_at,
    amount = EXCLUDED.amount,
    currency = 'CNY',
    basis = EXCLUDED.basis,
    verified = EXCLUDED.verified,
    source_updated_at = EXCLUDED.source_updated_at
WHERE EXCLUDED.verified
   OR NOT bi.order_payments.verified
   OR EXCLUDED.source_updated_at IS NULL
   OR EXCLUDED.source_updated_at > bi.order_payments.source_updated_at
RETURNING commercial_id
"""


@dataclass
class GuardStats:
    """运行期守卫计数；单进程CLI（入口有advisory锁）下足够。"""

    payment_downgrade_blocked: int = 0


GUARD_STATS = GuardStats()


def _commercial_ref(commercial_id: str) -> str:
    """日志不落订单号明文（与 _probe “不输出订单号”一致），只留可反查的短摘要。"""
    return hashlib.sha256(commercial_id.encode()).hexdigest()[:12]


def rebuild_payments(conn, shop_id: str, commercial_ids: set[str]) -> int:
    """重建受影响商业订单的支付事实；不确定金额或时间留NULL并令verified=false。

    返回被降级守卫拦下的次数：DO UPDATE 的 WHERE 谓词为假时整条语句影响0行，
    原已核验事实保留，order_items 的 allocation_verified 也不再被抹掉。
    """
    cids = {cid for cid in commercial_ids if cid}
    blocked = 0
    for commercial_id in sorted(cids):
        orders = _load_orders(conn, shop_id, commercial_id)
        amount, paid_at, basis, verified, source_updated_at = _determine_payment(
            conn, shop_id, commercial_id, orders)
        applied = conn.execute(
            _PAYMENT_UPSERT_SQL,
            (shop_id, commercial_id, paid_at, amount, basis, verified, source_updated_at),
        ).fetchone()
        if applied is None:
            blocked += 1
            logger.warning(
                "payment downgrade blocked: kept verified payment shop=%s basis=%s",
                shop_id, basis,
                extra={"shop_id": shop_id, "basis": basis,
                       "commercial_ref": _commercial_ref(commercial_id),
                       "error_code": "payment_downgrade_blocked"})
            continue
        if verified:
            conn.execute(
                "UPDATE bi.order_items SET allocation_verified = true "
                "WHERE shop_id=%s AND commercial_id=%s AND active AND allocated_paid_amount IS NOT NULL",
                (shop_id, commercial_id),
            )
        else:
            conn.execute(
                "UPDATE bi.order_items SET allocation_verified = false "
                "WHERE shop_id=%s AND commercial_id=%s",
                (shop_id, commercial_id),
            )
    GUARD_STATS.payment_downgrade_blocked += blocked
    return blocked


# ---------------------------------------------------------------------------
# 版本化入库
# ---------------------------------------------------------------------------

_ORDER_COLUMNS = (
    "shop_id, erp_id, commercial_ids, split_parent_id, source, source_updated_at, "
    "platform_modified_at, paid_at, raw_pay_amount, raw_payment, raw_platform_payment, "
    "raw_cost, raw_gross_profit, unified_status, system_status, active, "
    "normalization_status, batch_id"
)


def _trade_row(trade: dict[str, Any], batch_id: str) -> tuple:
    return (
        trade["shop_id"], trade["erp_id"], trade["commercial_ids"], trade["split_parent_id"],
        trade["source"], trade["source_updated_at"], trade["platform_modified_at"],
        trade["paid_at"], trade["raw_pay_amount"], trade["raw_payment"],
        trade["raw_platform_payment"], trade["raw_cost"], trade["raw_gross_profit"],
        trade["unified_status"], trade["system_status"], trade["active"],
        trade["normalization_status"], batch_id,
    )


def apply_trade(conn, trade: dict[str, Any], *, batch_id: str, force: bool = False) -> bool:
    """版本保护入库；replay 仅可重规范化时间戳相等的同版本记录。"""
    if trade["normalization_status"] == "invalid":
        return False
    existing = conn.execute(
        "SELECT commercial_ids FROM bi.orders WHERE shop_id=%s AND erp_id=%s",
        (trade["shop_id"], trade["erp_id"]),
    ).fetchone()
    old_ids = set(existing[0] or []) if existing else set()
    row = _trade_row(trade, batch_id)
    updated = conn.execute(
        f"""
        INSERT INTO bi.orders ({_ORDER_COLUMNS})
        VALUES ({", ".join(["%s"] * 18)})
        ON CONFLICT (shop_id, erp_id) DO UPDATE SET
            commercial_ids = EXCLUDED.commercial_ids,
            split_parent_id = EXCLUDED.split_parent_id,
            source = EXCLUDED.source,
            source_updated_at = EXCLUDED.source_updated_at,
            platform_modified_at = EXCLUDED.platform_modified_at,
            paid_at = EXCLUDED.paid_at,
            raw_pay_amount = EXCLUDED.raw_pay_amount,
            raw_payment = EXCLUDED.raw_payment,
            raw_platform_payment = EXCLUDED.raw_platform_payment,
            raw_cost = EXCLUDED.raw_cost,
            raw_gross_profit = EXCLUDED.raw_gross_profit,
            unified_status = EXCLUDED.unified_status,
            system_status = EXCLUDED.system_status,
            active = EXCLUDED.active,
            normalization_status = EXCLUDED.normalization_status,
            batch_id = EXCLUDED.batch_id
        WHERE EXCLUDED.source_updated_at > bi.orders.source_updated_at
           OR (%s AND EXCLUDED.source_updated_at = bi.orders.source_updated_at)
        RETURNING erp_id
        """,
        (*row, force),
    ).fetchone()
    if updated is None:
        existing = conn.execute(
            "SELECT source_updated_at FROM bi.orders WHERE shop_id=%s AND erp_id=%s",
            (trade["shop_id"], trade["erp_id"]),
        ).fetchone()
        if existing is None or existing[0] >= trade["source_updated_at"]:
            return False
        # 仅当数据库版本严格更旧才可能走到这里；同版本不视为冲突
        conn.execute(
            "UPDATE bi.orders SET normalization_status='version_conflict' "
            "WHERE shop_id=%s AND erp_id=%s",
            (trade["shop_id"], trade["erp_id"]),
        )
        return False
    conn.execute(
        "UPDATE bi.order_items SET active=%s WHERE shop_id=%s AND erp_id=%s",
        (trade["active"], trade["shop_id"], trade["erp_id"]),
    )
    if trade["items_present"]:
        # 先删后插是热路径；用 DELETE ... RETURNING 拿回旧快照，
        # 上游本次没带名称时不得把已留住的成交名称洗成空。
        removed = conn.execute(
            "DELETE FROM bi.order_items WHERE shop_id=%s AND erp_id=%s "
            "RETURNING line_id, product_name_snapshot, sku_label_snapshot",
            (trade["shop_id"], trade["erp_id"]),
        ).fetchall()
        kept_snapshots = {str(row[0]): (row[1], row[2]) for row in removed}
        for item in trade["items"]:
            previous = kept_snapshots.get(item["line_id"], (None, None))
            conn.execute(
                """
                INSERT INTO bi.order_items
                    (shop_id, erp_id, line_id, commercial_id, platform_line_id, product_id,
                     source_type, sku_id, paid_at, quantity, gift_quantity, raw_paid_amount,
                     raw_payment, raw_unit_cost, allocated_paid_amount, allocation_verified,
                     line_kind, active, product_name_snapshot, sku_label_snapshot)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    trade["shop_id"], trade["erp_id"], item["line_id"], item["commercial_id"],
                    item["platform_line_id"], item["product_id"], item["source_type"],
                    item["sku_id"], item["paid_at"], item["quantity"], item["gift_quantity"],
                    item["raw_paid_amount"], item["raw_payment"], item["raw_unit_cost"],
                    item["allocated_paid_amount"], item["allocation_verified"],
                    item["line_kind"], item["active"],
                    item["product_name_snapshot"] or previous[0],
                    item["sku_label_snapshot"] or previous[1],
                ),
            )
    old_ids |= set(trade["commercial_ids"])
    rebuild_payments(conn, str(trade["shop_id"]), old_ids)
    return True


# ---------------------------------------------------------------------------
# 售后规范化与去重
# ---------------------------------------------------------------------------


def normalise_aftersale(raw: dict[str, Any], *,
                        source: str = AFTERSALE_SOURCE) -> dict[str, Any]:
    """售后单头规范化；platform_success只是候选条件，canonical由去重决定。"""
    aftersale_id = str(raw.get("aftersaleId") or raw.get("id") or "").strip()
    shop_id = str(raw.get("userId") or "").strip()
    source_updated_at = (parse_timestamp(raw.get("modified"))
                         or parse_timestamp(raw.get("modifiedTime"))
                         or parse_timestamp(raw.get("updTime")))
    platform_completed_at = parse_business_timestamp(raw.get("platformCompleteTime"))
    system_completed_at = parse_business_timestamp(raw.get("finished"))
    online_status = raw.get("onlineStatus")
    work_status = raw.get("status")
    online_value = _source_int(online_status)
    work_values = _source_statuses(work_status)
    work_value = work_values[0] if work_values else None
    platform_success = bool(
        online_value == 7
        and platform_completed_at is not None
        and bool(work_values)
        and not any(value in (10, 11) for value in work_values)
    )
    return {
        "shop_id": shop_id,
        "aftersale_id": aftersale_id,
        "platform_refund_id": (str(raw.get("platformId") or raw.get("refundId")
                                   or raw.get("platformRefundId") or "").strip() or None),
        "commercial_id": (str(raw.get("tid") or "").strip() or None),
        "erp_id": (str(raw.get("sid") or "").strip() or None),
        "raw_platform_amount": to_decimal(raw.get("rawRefundMoney")),
        "raw_system_amount": to_decimal(raw.get("refundMoney")),
        "online_status": online_value,
        "work_status": work_value,
        "platform_completed_at": platform_completed_at,
        "system_completed_at": system_completed_at,
        "source_updated_at": source_updated_at,
        "platform_success": platform_success,
        "valid": bool(aftersale_id and shop_id and source_updated_at is not None),
    }


def apply_aftersale(conn, aftersale: dict[str, Any], *, batch_id: str,
                    force: bool = False) -> bool:
    """售后版本化UPSERT；replay 仅可重规范化时间戳相等的同版本记录。"""
    if not aftersale.get("valid"):
        return False
    commercial_id = aftersale["commercial_id"]
    matched = False
    if commercial_id:
        matched = conn.execute(
            "SELECT 1 FROM bi.orders WHERE shop_id=%s AND commercial_ids @> %s LIMIT 1",
            (aftersale["shop_id"], [commercial_id]),
        ).fetchone() is not None
    updated = conn.execute(
        """
        INSERT INTO bi.aftersales
            (shop_id, aftersale_id, platform_refund_id, commercial_id, erp_id,
             raw_platform_amount, raw_system_amount, online_status, work_status,
             platform_completed_at, system_completed_at, source_updated_at,
             platform_success, matched, batch_id)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (shop_id, aftersale_id) DO UPDATE SET
            platform_refund_id = EXCLUDED.platform_refund_id,
            commercial_id = EXCLUDED.commercial_id,
            erp_id = EXCLUDED.erp_id,
            raw_platform_amount = EXCLUDED.raw_platform_amount,
            raw_system_amount = EXCLUDED.raw_system_amount,
            online_status = EXCLUDED.online_status,
            work_status = EXCLUDED.work_status,
            platform_completed_at = EXCLUDED.platform_completed_at,
            system_completed_at = EXCLUDED.system_completed_at,
            source_updated_at = EXCLUDED.source_updated_at,
            platform_success = EXCLUDED.platform_success,
            batch_id = EXCLUDED.batch_id
        WHERE EXCLUDED.source_updated_at > bi.aftersales.source_updated_at
           OR (%s AND EXCLUDED.source_updated_at = bi.aftersales.source_updated_at)
        RETURNING aftersale_id
        """,
        (
            aftersale["shop_id"], aftersale["aftersale_id"], aftersale["platform_refund_id"],
            commercial_id, aftersale["erp_id"], aftersale["raw_platform_amount"],
            aftersale["raw_system_amount"], aftersale["online_status"], aftersale["work_status"],
            aftersale["platform_completed_at"], aftersale["system_completed_at"],
            aftersale["source_updated_at"], aftersale["platform_success"], matched, batch_id, force,
        ),
    ).fetchone()
    if updated is None:
        # 版本护栏拦截：历史行仍补齐缺失的平台退款号，并维持canonical判定
        if aftersale["platform_refund_id"]:
            conn.execute(
                "UPDATE bi.aftersales SET platform_refund_id=%s "
                "WHERE shop_id=%s AND aftersale_id=%s AND platform_refund_id IS NULL",
                (aftersale["platform_refund_id"], aftersale["shop_id"],
                 aftersale["aftersale_id"]),
            )
            mark_refund_canonical(conn, aftersale["shop_id"],
                                  {aftersale["platform_refund_id"]})
        return False
    if aftersale["platform_refund_id"]:
        mark_refund_canonical(conn, aftersale["shop_id"], {aftersale["platform_refund_id"]})
    if commercial_id:
        refresh_aftersale_matched(conn, aftersale["shop_id"], {commercial_id})
    return True


def refresh_aftersale_matched(conn, shop_id: str, commercial_ids: set[str]) -> None:
    """原单到达后刷新售后匹配标记。"""
    for commercial_id in sorted(c for c in commercial_ids if c):
        conn.execute(
            """
            UPDATE bi.aftersales a SET matched = EXISTS (
                SELECT 1 FROM bi.orders o
                WHERE o.shop_id = a.shop_id
                  AND (o.active OR (o.paid_at IS NOT NULL AND o.raw_pay_amount > 0))
                  AND o.commercial_ids @> ARRAY[a.commercial_id])
            WHERE a.shop_id=%s AND a.commercial_id=%s
            """,
            (shop_id, commercial_id),
        )


def mark_refund_canonical(conn, shop_id: str, platform_refund_ids: set[str]) -> None:
    """平台售后号相同的拆分工单只确认一次实际退款。

    金额一致的组确定唯一canonical；金额不一致的组设未验证，禁止取最大值。
    """
    for refund_id in sorted(r for r in platform_refund_ids if r):
        rows = conn.execute(
            "SELECT aftersale_id, raw_platform_amount, platform_success FROM bi.aftersales "
            "WHERE shop_id=%s AND platform_refund_id=%s ORDER BY aftersale_id",
            (shop_id, refund_id),
        ).fetchall()
        if not rows:
            continue
        if len(rows) == 1:
            conn.execute(
                "UPDATE bi.aftersales SET refund_canonical=%s "
                "WHERE shop_id=%s AND platform_refund_id=%s",
                (bool(rows[0][2]), shop_id, refund_id),
            )
            continue
        amounts = {row[1] for row in rows}
        if len(amounts) == 1:
            canonical_id = rows[0][0]
            conn.execute(
                "UPDATE bi.aftersales SET refund_canonical=(aftersale_id=%s AND platform_success) "
                "WHERE shop_id=%s AND platform_refund_id=%s",
                (canonical_id, shop_id, refund_id),
            )
        else:
            conn.execute(
                "UPDATE bi.aftersales SET refund_canonical=false "
                "WHERE shop_id=%s AND platform_refund_id=%s",
                (shop_id, refund_id),
            )


# ---------------------------------------------------------------------------
# 窗口与分页拉取
# ---------------------------------------------------------------------------


# 实测会“成功却省略 list”的接口清单（docs/superpowers/research/
# 2026-09-06-kuaimai-data-recheck.json）：erp.item.history.cost.price.query、
# erp.item.sku.list.get、stock.api.status.query、erp.item.warehouse.list.get、
# erp.wave.logistics.order.query、erp.aftersale.refund.warehouse.query、
# purchase.order.query（初查 status=unexpected_list_shape，复查 success_no_records）。
# 同步链在用的 erp.aftersale.list.query / erp.shop.list.query 与订单的**在线通道**
# （queryType=0，空集时带 total=0）都返回 list，依旧不传宽容位（C-6）。
# 例外是订单**归档通道**（queryType=1）：2026-09-11 真实账号实测，窗口内没有归档单时
# 它既不回 list 也不回 total，只回 {"success": true, "traceId": ...}，所以该调用点
# 单独开宽容位。省略仍不构成完成证据：覆盖证据只由在线通道的 total/hasNext 提供，
# 在线通道拿不到证据时仍抛 unknown_empty。


@dataclass(frozen=True)
class Window:
    """业务或修改时间窗口，内部归属一律 [start, end)。"""

    start: datetime
    end: datetime


def day_windows(start: datetime, end: datetime) -> Iterator[Window]:
    """把范围切成每窗口不超过一天的小窗口。"""
    cursor = start
    while cursor < end:
        chunk_end = min(cursor + timedelta(days=1), end)
        yield Window(cursor, chunk_end)
        cursor = chunk_end


def _fmt(moment: datetime) -> str:
    return moment.astimezone(BEIJING).strftime("%Y-%m-%d %H:%M:%S")


def _fetch_orders_cursor(client: KuaimaiClient, *, shop_id: str, window: Window,
                         time_type: str, query_type: str,
                         method: str = ORDER_SOURCE) -> Iterator[dict[str, Any]]:
    """非归档订单：官方cursor+hasNext分页；不能用'本页少于200'作为唯一结束条件。

    method 参数化订单源：交易查询与出库通道的 cursor/queryType 参数形状一致，复用同一实现。"""
    cursor: str | None = None
    while True:
        params: dict[str, str] = {
            "userIds": shop_id,
            "timeType": time_type,
            "startTime": _fmt(window.start),
            "endTime": _fmt(window.end),
            "pageSize": str(PAGE_SIZE),
            "queryType": query_type,
            "useHasNext": "true",
            "useCursor": "true",
        }
        if cursor is not None:
            params["cursor"] = cursor
        page = parse_page(client.call(method, params))
        if not page.rows:
            return
        yield from page.rows
        if page.verified_empty:
            return
        if page.has_next is False:
            return
        if page.cursor is None:
            # hasNext=true却无数据/游标：无结束证据
            raise KuaimaiError("invalid_response")
        if cursor is not None and page.cursor == cursor:
            raise KuaimaiError("invalid_response")
        cursor = page.cursor


def _fetch_orders_paged(client: KuaimaiClient, *, shop_id: str, window: Window,
                        time_type: str | None, query_type: str,
                        method: str = ORDER_SOURCE,
                        allow_omitted_list: bool = False) -> Iterator[dict[str, Any]]:
    """归档通道：页码分页；按total判断末页并检查计数一致性。

    allow_omitted_list 只为归档通道开（实测它无数据时省略 list/total）。
    省略 list 永远不等于完成证据（C-6）：调用方依旧需要 total 或 hasNext。
    """
    page_no = 1
    collected = 0
    total: int | None = None
    while True:
        params: dict[str, str] = {
            "userIds": shop_id,
            "pageNo": str(page_no),
            "pageSize": str(PAGE_SIZE),
            "queryType": query_type,
            "startTime": _fmt(window.start),
            "endTime": _fmt(window.end),
        }
        if time_type:
            params["timeType"] = time_type
        page = parse_page(client.call(method, params),
                          allow_omitted_list=allow_omitted_list)
        if page.verified_empty:
            return
        if total is None and page.total is not None:
            total = page.total
        if page.total is not None and total is not None and page.total != total:
            # 数据漂移：重跑由上层决定，不确认覆盖
            raise KuaimaiError("upstream")
        yield from page.rows
        collected += len(page.rows)
        if total is not None:
            if collected >= total:
                return
            if len(page.rows) < PAGE_SIZE:
                raise KuaimaiError("invalid_response")
        else:
            # 归档通道不返回total：以不足一页作为末页证据
            if len(page.rows) < PAGE_SIZE:
                return
        page_no += 1


def _fetch_aftersales_paged(client: KuaimaiClient, *, shop_id: str, window: Window,
                            start_param: str, end_param: str,
                            extra_params: dict[str, str] | None = None) -> Iterator[dict[str, Any]]:
    """售后：userIds/pageNo/pageSize=200/asVersion=2；按total判断末页。"""
    page_no = 1
    collected = 0
    total: int | None = None
    while True:
        params: dict[str, str] = {
            "userIds": shop_id,
            "pageNo": str(page_no),
            "pageSize": str(PAGE_SIZE),
            "asVersion": "2",
            start_param: _fmt(window.start),
            end_param: _fmt(window.end),
        }
        if extra_params:
            params.update(extra_params)
        page = parse_page(client.call(AFTERSALE_SOURCE, params))
        if page.verified_empty:
            return
        if page.total is None:
            raise KuaimaiError("invalid_response")
        if total is not None and page.total != total:
            raise KuaimaiError("upstream")
        total = page.total
        yield from page.rows
        collected += len(page.rows)
        if collected >= total:
            return
        if len(page.rows) < PAGE_SIZE:
            raise KuaimaiError("invalid_response")
        page_no += 1


def fetch_window(client: KuaimaiClient, *, entity: str, shop_id: str,
                 window: Window, mode: str,
                 order_source: str = ORDER_SOURCE) -> Iterator[dict[str, Any]]:
    """拉取一个窗口；结束前必须证明分页完整，否则抛KuaimaiError。

    初始订单回填按pay_time建立支付业务覆盖；归档边界附近分别核对
    queryType=0/1，使用两通道覆盖且依主键幂等去重。
    order_source 决定订单实体请求的接口方法（淘系走出库通道）；售后不分平台。
    """
    if entity == "orders":
        if mode in ("incremental", "scan"):
            yield from _fetch_orders_cursor(client, shop_id=shop_id, window=window,
                                            time_type="upd_time", query_type="0",
                                            method=order_source)
        elif mode in ("backfill", "replay", "reconcile", "probe"):
            yield from _fetch_orders_cursor(client, shop_id=shop_id, window=window,
                                            time_type="pay_time", query_type="0",
                                            method=order_source)
            # 归档通道实测会省略 list/total（见上方 C-6 例外说明）：按空页解析，
            # 但不得拿它当完成证据；整窗证据仍由上面的在线通道负责。
            yield from _fetch_orders_paged(client, shop_id=shop_id, window=window,
                                           time_type="pay_time", query_type="1",
                                           method=order_source, allow_omitted_list=True)
        else:
            raise ValueError(f"未知模式 {mode}")
    elif entity == "aftersales_occurrence":
        if mode in ("incremental", "scan"):
            yield from _fetch_aftersales_paged(client, shop_id=shop_id, window=window,
                                               start_param="startModified",
                                               end_param="endModified")
        elif mode in ("backfill", "replay", "reconcile", "probe"):
            # 售后发生额用startPlatformCompleteTime/endPlatformCompleteTime建立覆盖
            yield from _fetch_aftersales_paged(client, shop_id=shop_id, window=window,
                                               start_param="startPlatformCompleteTime",
                                               end_param="endPlatformCompleteTime")
        else:
            raise ValueError(f"未知模式 {mode}")
    elif entity == "aftersales_cohort":
        raise ValueError("aftersales_cohort由check_cohort_window按已回填商业单补查")
    else:
        raise ValueError(f"未知实体 {entity}")


# ---------------------------------------------------------------------------
# 窗口事务与覆盖/水位维护
# ---------------------------------------------------------------------------


def _ensure_state(conn, source: str, entity: str, shop_id: str) -> None:
    conn.execute(
        "INSERT INTO bi.sync_state(source, entity, shop_id) VALUES (%s, %s, %s) "
        "ON CONFLICT (source, entity, shop_id) DO NOTHING",
        (source, entity, shop_id),
    )


def _extend_incremental_coverage(conn, *, source: str, entity: str, shop_id: str,
                                 batch_id: str, window: Window) -> None:
    """确认期间新增支付/退款的收录后才扩展已建立的业务覆盖终点。"""
    if entity == "orders":
        business_end = conn.execute(
            "SELECT max(paid_at) FROM bi.orders WHERE shop_id=%s AND batch_id=%s",
            (shop_id, batch_id),
        ).fetchone()[0]
    else:
        business_end = conn.execute(
            "SELECT max(platform_completed_at) FROM bi.aftersales "
            "WHERE shop_id=%s AND batch_id=%s AND platform_success",
            (shop_id, batch_id),
        ).fetchone()[0]
    if business_end is None:
        return
    state = conn.execute(
        "SELECT covered FROM bi.sync_state WHERE source=%s AND entity=%s AND shop_id=%s FOR UPDATE",
        (source, entity, shop_id),
    ).fetchone()
    if state is None:
        return
    uppers = [rng.upper for rng in state[0]
              if rng.upper is not None and rng.upper != datetime.max.replace(tzinfo=BEIJING)]
    if not uppers:
        return  # 未建立业务覆盖，不靠增量直接填平历史缺口
    prev_end = max(uppers)
    if business_end <= prev_end:
        return
    if window.start - prev_end > SYNC_OVERLAP:
        return  # 覆盖不连续
    # 终点含边界时刻的已收录支付（[start,end)归属，多加1秒覆盖边界）
    conn.execute(
        "UPDATE bi.sync_state SET covered = covered + tstzmultirange(tstzrange(%s, %s, '[)')) "
        "WHERE source=%s AND entity=%s AND shop_id=%s",
        (prev_end, business_end + timedelta(seconds=1), source, entity, shop_id),
    )




def _record_batch(conn, *, source: str, entity: str, shop_id: str, window: Window,
                  mode: str, batch_id: str, row_count: int) -> None:
    """落批次凭证：回答"这些数字是哪几批同步出来的"。

    窗口口径必须标清楚：增量/扫描扫的是修改时间，回填/重放/对账才是业务时间。
    两者混成一列，一次增量就会被当成"该业务窗口已覆盖"的假凭证。
    row_count 是本次真实写入数，0 表示确实没有数据，与"没统计"不是一回事。
    """
    conn.execute(
        "INSERT INTO bi.sync_batches(source, entity, shop_id, batch_id, business_window, "
        "window_kind, mode, row_count) VALUES (%s, %s, %s, %s, tstzrange(%s, %s, '[)'), "
        "%s, %s, %s) ON CONFLICT (source, entity, shop_id, batch_id) DO NOTHING",
        (source, entity, shop_id, batch_id, window.start, window.end,
         "modified" if mode in ("incremental", "scan") else "business",
         mode, row_count),
    )


def _record_window_success(conn, *, source: str, entity: str, shop_id: str,
                           window: Window, mode: str, batch_id: str,
                           row_count: int) -> None:
    _ensure_state(conn, source, entity, shop_id)
    if mode in ("incremental", "scan"):
        conn.execute(
            "UPDATE bi.sync_state SET watermark=%s, last_success_at=now(), last_error_code=NULL "
            "WHERE source=%s AND entity=%s AND shop_id=%s",
            (window.end, source, entity, shop_id),
        )
        _extend_incremental_coverage(conn, source=source, entity=entity, shop_id=shop_id,
                                     batch_id=batch_id, window=window)
    else:
        # 仅完成对应业务时间回填/replay的窗口可加入覆盖；
        # upd_time成功本身不能证明该修改窗口就是支付覆盖
        conn.execute(
            "UPDATE bi.sync_state SET "
            "covered = covered + tstzmultirange(tstzrange(%s, %s, '[)')), "
            "last_success_at = now(), last_error_code = NULL "
            "WHERE source=%s AND entity=%s AND shop_id=%s",
            (window.start, window.end, source, entity, shop_id),
        )
    # 两条分支都要留凭证；窗口口径由 _record_batch 按 mode 标清。
    _record_batch(conn, source=source, entity=entity, shop_id=shop_id, window=window,
                  mode=mode, batch_id=batch_id, row_count=row_count)


def record_failure(conn, *, source: str, entity: str, shop_id: str, code: str) -> None:
    """异常回滚后另开短事务记录；保留旧成功水位和已完成窗口。"""
    _ensure_state(conn, source, entity, shop_id)
    conn.execute(
        "UPDATE bi.sync_state SET last_attempt_at=now(), last_error_code=%s "
        "WHERE source=%s AND entity=%s AND shop_id=%s",
        (code, source, entity, shop_id),
    )


def sync_window(conn, client: KuaimaiClient, *, entity: str, shop_id: str,
                window: Window, mode: str,
                order_source: str = ORDER_SOURCE) -> int:
    """单个窗口事务：拉取、规范化、入库、去重；成功后推进状态。成功返回写入记录数。"""
    batch_id = uuid.uuid4().hex
    source = order_source if entity == "orders" else AFTERSALE_SOURCE
    accepted = 0
    refund_ids: set[str] = set()
    touched_commercials: set[str] = set()
    with conn.transaction():
        for raw in fetch_window(client, entity=entity, shop_id=shop_id, window=window,
                                mode=mode, order_source=order_source):
            if entity == "orders":
                trade = normalise_trade(raw, source=order_source)
                if trade["normalization_status"] == "invalid":
                    continue
                apply_trade(conn, trade, batch_id=batch_id, force=(mode == "replay"))
                accepted += 1
                touched_commercials.update(trade["commercial_ids"])
            else:
                aftersale = normalise_aftersale(raw)
                if apply_aftersale(conn, aftersale, batch_id=batch_id, force=(mode == "replay")):
                    accepted += 1
                if aftersale["platform_refund_id"]:
                    refund_ids.add(aftersale["platform_refund_id"])
                if aftersale["commercial_id"]:
                    touched_commercials.add(aftersale["commercial_id"])
        if entity != "orders":
            refresh_aftersale_matched(conn, shop_id, touched_commercials)
        mark_refund_canonical(conn, shop_id, refund_ids)
        _record_window_success(conn, source=source, entity=entity, shop_id=shop_id,
                               window=window, mode=mode, batch_id=batch_id,
                               row_count=accepted)
    return accepted


def check_cohort_window(conn, client: KuaimaiClient, *, shop_id: str,
                        window: Window) -> int:
    """同批退款：用已回填商业单的tids分批补查，并建立cohort覆盖。

    另取status=2,12未结工单属于增量/reconcile的modified扫描，不在此处。
    """
    batch_id = uuid.uuid4().hex
    refund_ids: set[str] = set()
    touched: set[str] = set()
    with conn.transaction():
        commercials = [
            row[0] for row in conn.execute(
                "SELECT commercial_id FROM bi.order_payments "
                "WHERE shop_id=%s AND paid_at >= %s AND paid_at < %s AND verified",
                (shop_id, window.start, window.end),
            ).fetchall()
        ]
        for offset in range(0, len(commercials), COHORT_TIDS_BATCH):
            chunk = commercials[offset:offset + COHORT_TIDS_BATCH]
            page_no = 1
            collected = 0
            total: int | None = None
            while True:
                params = {
                    "userIds": shop_id,
                    "pageNo": str(page_no),
                    "pageSize": str(PAGE_SIZE),
                    "asVersion": "2",
                    "tids": ",".join(chunk),
                }
                page = parse_page(client.call(AFTERSALE_SOURCE, params))
                if page.verified_empty:
                    break
                if page.total is None:
                    raise KuaimaiError("invalid_response")
                if total is not None and page.total != total:
                    raise KuaimaiError("upstream")
                total = page.total
                for raw in page.rows:
                    aftersale = normalise_aftersale(raw)
                    apply_aftersale(conn, aftersale, batch_id=batch_id)
                    if aftersale["platform_refund_id"]:
                        refund_ids.add(aftersale["platform_refund_id"])
                    if aftersale["commercial_id"]:
                        touched.add(aftersale["commercial_id"])
                collected += len(page.rows)
                if collected >= total:
                    break
                if len(page.rows) < PAGE_SIZE:
                    raise KuaimaiError("invalid_response")
                page_no += 1
        refresh_aftersale_matched(conn, shop_id, touched)
        mark_refund_canonical(conn, shop_id, refund_ids)
        _ensure_state(conn, AFTERSALE_SOURCE, "aftersales_cohort", shop_id)
        conn.execute(
            "UPDATE bi.sync_state SET "
            "covered = covered + tstzmultirange(tstzrange(%s, %s, '[)')), "
            "last_success_at = now(), last_error_code = NULL, "
            "data_as_of = greatest(coalesce(data_as_of, '1970-01-01 00:00+00'), now()) "
            "WHERE source=%s AND entity='aftersales_cohort' AND shop_id=%s",
            (window.start, window.end, AFTERSALE_SOURCE, shop_id),
        )
    return len(commercials)


def refetch_orders_for_commercials(conn, client: KuaimaiClient, *, shop_id: str,
                                   commercial_ids: set[str],
                                   order_source: str = ORDER_SOURCE) -> int:
    """增量收到更早商业单退款时按已发布的tid条件补拉原单。

    单次tid查询参数及数量上限需在4.7真实核验后固定；当前按单tid逐个补拉。
    """
    accepted = 0
    for commercial_id in sorted(commercial_ids):
        if not commercial_id:
            continue
        cursor: str | None = None
        while True:
            params: dict[str, str] = {
                "userIds": shop_id,
                "timeType": "upd_time",
                "startTime": _fmt(datetime.now(BEIJING) - timedelta(days=MAX_QUERY_DAYS)),
                "endTime": _fmt(datetime.now(BEIJING)),
                "pageSize": str(PAGE_SIZE),
                "queryType": "0",
                "useHasNext": "true",
                "useCursor": "true",
                "tid": commercial_id,
            }
            if cursor is not None:
                params["cursor"] = cursor
            page = parse_page(client.call(order_source, params))
            batch_id = uuid.uuid4().hex
            with conn.transaction():
                for raw in page.rows:
                    trade = normalise_trade(raw, source=order_source)
                    if trade["normalization_status"] == "invalid":
                        continue
                    if apply_trade(conn, trade, batch_id=batch_id):
                        accepted += 1
            refresh_aftersale_matched(conn, shop_id, {commercial_id})
            if page.verified_empty or page.has_next is False:
                break
            if page.cursor is None or (cursor is not None and page.cursor == cursor):
                raise KuaimaiError("invalid_response")
            cursor = page.cursor
    return accepted


def unmatched_commercials(conn, shop_id: str) -> set[str]:
    """售后已到、原单未到的商业订单号，等待按tid补拉。"""
    rows = conn.execute(
        "SELECT DISTINCT a.commercial_id FROM bi.aftersales a "
        "WHERE a.shop_id=%s AND a.commercial_id IS NOT NULL "
        "AND NOT EXISTS (SELECT 1 FROM bi.orders o WHERE o.shop_id=a.shop_id "
        "                AND (o.active OR (o.paid_at IS NOT NULL AND o.raw_pay_amount > 0)) "
        "                AND o.commercial_ids @> ARRAY[a.commercial_id])",
        (shop_id,),
    ).fetchall()
    return {row[0] for row in rows}


# ---------------------------------------------------------------------------
# 店铺同步
# ---------------------------------------------------------------------------


def sync_shops(conn, client: KuaimaiClient) -> int:
    """拉取店铺档案并更新bi.shops；不输出店铺名称到控制台。"""
    page_no = 1
    collected = 0
    label_changes = 0
    seen_shop_ids: list[str] = []
    with conn.transaction():
        while True:
            page = parse_page(client.call("erp.shop.list.query", {
                "pageNo": str(page_no), "pageSize": str(PAGE_SIZE)}))
            if not page.rows:
                break
            for raw in page.rows:
                shop_id = str(raw.get("userId") or "").strip()
                if not shop_id:
                    continue
                state = str(raw.get("state") or "").strip().lower()
                enabled = _source_bool(raw.get("active"))
                if enabled is None:
                    enabled = state in {"3", "4", "enable", "enabled"}
                changed = conn.execute(
                    "INSERT INTO bi.shops(shop_id, platform, display_name, enabled) "
                    "VALUES (%s, %s, %s, %s) "
                    "ON CONFLICT (shop_id) DO UPDATE SET platform=EXCLUDED.platform, "
                    "display_name=EXCLUDED.display_name, enabled=EXCLUDED.enabled "
                    "WHERE (bi.shops.platform, bi.shops.display_name, bi.shops.enabled) "
                    "IS DISTINCT FROM (EXCLUDED.platform, EXCLUDED.display_name, "
                    "EXCLUDED.enabled) "
                    "RETURNING shop_id",
                    (shop_id, str(raw.get("source") or "unknown"),
                     str(raw.get("title") or raw.get("nick") or raw.get("shopName") or ""),
                     enabled),
                ).fetchone()
                if changed is not None:
                    # 平台/停用状态会改变同名店的展示后缀，因此一并算目录变更。
                    label_changes += 1
                seen_shop_ids.append(shop_id)
                collected += 1
            if len(page.rows) < PAGE_SIZE:
                break
            page_no += 1
        # 引用落表：展示层靠纯派生，反查与撞车检校靠这张表。
        ensure_refs(conn, EntityKind.SHOP.value, seen_shop_ids)
        if label_changes:
            bump_catalog_version(conn)
    return collected


# ---------------------------------------------------------------------------
# 商品档案同步：把订单行里的 itemSysId 对应到可读名称
# ---------------------------------------------------------------------------

# 版本守卫 + 变更判定：较旧的档案修改时间不覆盖较新的，内容全同则不写行。
# synced_at 不参与比较，否则每次都算“有变更”。
_PRODUCT_UPSERT_SQL = """
INSERT INTO bi.products(product_id, title, outer_id, item_type, category, active,
                        purchase_price, normalization_status, source_modified_at, synced_at)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, now())
ON CONFLICT (product_id) DO UPDATE SET
    title = EXCLUDED.title, outer_id = EXCLUDED.outer_id, item_type = EXCLUDED.item_type,
    category = EXCLUDED.category, active = EXCLUDED.active,
    purchase_price = EXCLUDED.purchase_price,
    normalization_status = EXCLUDED.normalization_status,
    source_modified_at = EXCLUDED.source_modified_at, synced_at = EXCLUDED.synced_at
WHERE (bi.products.source_modified_at IS NULL
       OR EXCLUDED.source_modified_at IS NULL
       OR bi.products.source_modified_at <= EXCLUDED.source_modified_at)
  AND ROW(bi.products.title, bi.products.outer_id, bi.products.item_type,
          bi.products.category, bi.products.active, bi.products.purchase_price,
          bi.products.normalization_status, bi.products.source_modified_at)
      IS DISTINCT FROM
      ROW(EXCLUDED.title, EXCLUDED.outer_id, EXCLUDED.item_type,
          EXCLUDED.category, EXCLUDED.active, EXCLUDED.purchase_price,
          EXCLUDED.normalization_status, EXCLUDED.source_modified_at)
RETURNING bi.products.product_id
"""


def normalise_item_master(raw: dict[str, Any]) -> dict[str, Any]:
    """白名单规范化一条商品档案；无 sysItemId 不猜主键，无名称不拼展示名。"""
    product_id = (str(raw.get("sysItemId") or "").strip() or None)
    title = str(raw.get("title") or "").strip()
    status = "normal"
    if not product_id:
        status = "invalid"
    elif not title:
        status = "needs_review"
    item_type = raw.get("type")
    return {
        "product_id": product_id,
        "title": title,
        "outer_id": (str(raw.get("outerId") or "").strip() or None),
        "item_type": (str(item_type).strip()
                      if item_type not in (None, "") else None),
        "category": (str(raw.get("itemCategoryNames") or "").strip() or None),
        "active": _source_int(raw.get("activeStatus")) == 1,
        "purchase_price": to_decimal(raw.get("purchasePrice")),
        "normalization_status": status,
        "source_modified_at": parse_timestamp(raw.get("modified")),
    }


def sync_products(conn, client: KuaimaiClient) -> dict[str, int]:
    """全量翻页拉商品档案（实测 435 条/3 页）。

    接口实测忽略 sysItemIds/sysItemId 过滤参数（带与不带都返回同一 total），
    所以只能整表取回后在 reporting 层 JOIN，不能按成交商品点查。
    完成证据只认 total：不足一页却没拉满视为上游不一致，不发布成功。
    """
    stats = {"fetched": 0, "upserted": 0, "skipped": 0, "invalid": 0}
    page_no = 1
    total: int | None = None
    synced_product_ids: list[str] = []
    with conn.transaction():
        while True:
            page = parse_page(client.call(ITEM_SOURCE, {
                "pageNo": str(page_no), "pageSize": str(PAGE_SIZE)}), list_key="items")
            if page.total is None:
                raise KuaimaiError("invalid_response")
            if total is None:
                total = page.total
            elif page.total != total:
                raise KuaimaiError("upstream")
            stats["fetched"] += len(page.rows)
            for raw in page.rows:
                item = normalise_item_master(raw)
                if item["normalization_status"] == "invalid":
                    stats["invalid"] += 1
                    continue
                written = conn.execute(
                    _PRODUCT_UPSERT_SQL,
                    (item["product_id"], item["title"], item["outer_id"],
                     item["item_type"], item["category"], item["active"],
                     item["purchase_price"], item["normalization_status"],
                     item["source_modified_at"]),
                ).fetchone()
                stats["upserted" if written else "skipped"] += 1
                synced_product_ids.append(str(item["product_id"]))
            assert total is not None
            if stats["fetched"] >= total:
                break
            if len(page.rows) < PAGE_SIZE:
                raise KuaimaiError("invalid_response")
            page_no += 1
        ensure_refs(conn, EntityKind.PRODUCT.value, synced_product_ids)
        if stats["upserted"]:
            # 档案名称真的变过才推进目录版本，供 Artifact 记录它解析时用的版本。
            bump_catalog_version(conn)
    return stats


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _connect(dsn: str):
    # 每个conn.transaction()都是独立提交，不能让默认外层事务拖到CLI结束才提交
    return __import__("psycopg").connect(dsn, autocommit=True)


def _require_single_shop(settings) -> str:
    if len(settings.shop_ids) != 1:
        raise SystemExit("probe要求仅配置一个店铺（BI_SHOP_IDS）")
    return next(iter(settings.shop_ids))


def _shop_order_source(conn, shop_id: str) -> str:
    """按 bi.shops.platform 路由订单源；平台未登记就报错退出，不回退默认源。

    两个失败模式都踩过，因此这里宁可停机：
    - 店铺档案缺失时静默回退，淘系店会被 `trade.list.query` “验证为空”造成假覆盖；
    - 未登记平台回退交易源，同样会把拿不到的数据当成“确实没有”。
    """
    row = conn.execute(
        "SELECT platform FROM bi.shops WHERE shop_id=%s", (shop_id,)).fetchone()
    if row is None:
        raise SystemExit(f"店铺 {shop_id} 不在 bi.shops，请先运行 shops 同步")
    source = resolve_order_source(ShopRecord.from_row(shop_id, row[0], ()))
    if source is None:
        raise SystemExit(f"店铺 {shop_id} 的平台 {row[0]!r} 未在 sources 注册表登记来源，"
                         "拒绝回退交易通道")
    return source


def _shop_error(exc: BaseException) -> str:
    """CLI 逐店隔离时输出的脱敏错误标识：KuaimaiError 取 code，其余取消息文本。"""
    if isinstance(exc, KuaimaiError):
        return exc.code
    return str(exc) or exc.__class__.__name__


def _run_window(conn, client: KuaimaiClient, *, entity: str, shop_id: str,
                window: Window, mode: str,
                order_source: str = ORDER_SOURCE) -> int:
    try:
        return sync_window(conn, client, entity=entity, shop_id=shop_id,
                           window=window, mode=mode, order_source=order_source)
    except KuaimaiError as exc:
        record_failure(conn,
                       source=order_source if entity == "orders" else AFTERSALE_SOURCE,
                       entity=entity, shop_id=shop_id, code=exc.code)
        logger.warning("sync window failed entity=%s shop=%s code=%s", entity, shop_id, exc.code)
        raise


def _beijing_now() -> datetime:
    """北京时间当前时刻；回填结束时刻单独取，不能沿用开工瞬间。"""
    return datetime.now(BEIJING)


def _backfill_shop(conn, client: KuaimaiClient, *, shop_id: str, days: int,
                   t0: datetime, order_source: str = ORDER_SOURCE,
                   now: Callable[[], datetime] = _beijing_now) -> dict[str, int]:
    """回填最近N天并补拉[t0,t1)；t1 是回填完成瞬间，补齐后才发布data_as_of。

    t1 必须等回填循环全部跑完再重新取：回填本身可能跑几小时，沿用开工瞬间（旧代码
    的 t1=t0）会让 day_windows(t0, t1) 恒空，scan 一段也不执行，水位就建不起来，
    后续增量永远 SystemExit；data_as_of 也会宣布一个回填期间变更未入库的覆盖。
    """
    if days > MAX_QUERY_DAYS:
        raise SystemExit(f"回填跨度最多{MAX_QUERY_DAYS}天")
    start = t0 - timedelta(days=days)
    stats = {"orders": 0, "aftersales_occurrence": 0, "cohort_windows": 0}
    for window in day_windows(start, t0):
        stats["orders"] += _run_window(conn, client, entity="orders", shop_id=shop_id,
                                       window=window, mode="backfill",
                                       order_source=order_source)
    for window in day_windows(start, t0):
        stats["aftersales_occurrence"] += _run_window(
            conn, client, entity="aftersales_occurrence", shop_id=shop_id,
            window=window, mode="backfill")
        check_cohort_window(conn, client, shop_id=shop_id, window=window)
        stats["cohort_windows"] += 1
    # 补拉回填期间的变化：修改时间扫描推进水位
    t1 = now()
    if t1 <= t0:
        # 时钟不动或回拨时至少推进一步重叠量，保证首次回填能把水位建立起来。
        t1 = t0 + SYNC_OVERLAP
    for window in day_windows(t0, t1):
        _run_window(conn, client, entity="orders", shop_id=shop_id,
                    window=window, mode="scan", order_source=order_source)
        _run_window(conn, client, entity="aftersales_occurrence", shop_id=shop_id,
                    window=window, mode="scan")
    with conn.transaction():
        for entity in ("orders", "aftersales_occurrence", "aftersales_cohort"):
            state_source = order_source if entity == "orders" else AFTERSALE_SOURCE
            _ensure_state(conn, state_source, entity, shop_id)
            conn.execute(
                "UPDATE bi.sync_state SET data_as_of=%s "
                "WHERE source=%s AND entity=%s AND shop_id=%s",
                (t1, state_source, entity, shop_id),
            )
    return stats


def _incremental_shop(conn, client: KuaimaiClient, *, shop_id: str,
                      run_end: datetime,
                      order_source: str = ORDER_SOURCE) -> dict[str, int]:
    """增量：从watermark-10分钟到本次固定run_end，逐日窗口推进。"""
    stats = {"orders": 0, "aftersales_occurrence": 0}
    for entity, source in (("orders", order_source),
                           ("aftersales_occurrence", AFTERSALE_SOURCE)):
        state = conn.execute(
            "SELECT watermark FROM bi.sync_state WHERE source=%s AND entity=%s AND shop_id=%s",
            (source, entity, shop_id),
        ).fetchone()
        if state is None or state[0] <= datetime(1970, 1, 2, tzinfo=BEIJING):
            raise SystemExit(f"{entity} 增量前必须先完成backfill建立水位")
        start = state[0] - SYNC_OVERLAP
        for window in day_windows(start, run_end):
            stats[entity] += _run_window(conn, client, entity=entity, shop_id=shop_id,
                                         window=window, mode="incremental",
                                         order_source=order_source)
    # 增量收到更早商业单退款时，按已发布的tid条件补拉原单
    missing = unmatched_commercials(conn, shop_id)
    if missing:
        refetch_orders_for_commercials(conn, client, shop_id=shop_id,
                                       commercial_ids=missing, order_source=order_source)
    return stats


def _reconcile_shop(conn, client: KuaimaiClient, *, shop_id: str, days: int,
                    run_end: datetime,
                    order_source: str = ORDER_SOURCE) -> dict[str, object]:
    """按支付日/退款完成日重核最近N天，刷新cohort覆盖与data_as_of，并按凭证推进质量状态。"""
    if days > MAX_QUERY_DAYS:
        raise SystemExit(f"重核跨度最多{MAX_QUERY_DAYS}天")
    start = run_end - timedelta(days=days)
    stats: dict[str, object] = {"orders": 0, "aftersales_occurrence": 0, "cohort_windows": 0}
    for window in day_windows(start, run_end):
        stats["orders"] += _run_window(conn, client, entity="orders", shop_id=shop_id,
                                       window=window, mode="reconcile",
                                       order_source=order_source)
        stats["aftersales_occurrence"] += _run_window(
            conn, client, entity="aftersales_occurrence", shop_id=shop_id,
            window=window, mode="reconcile")
        check_cohort_window(conn, client, shop_id=shop_id, window=window)
        stats["cohort_windows"] += 1
    missing = unmatched_commercials(conn, shop_id)
    if missing:
        refetch_orders_for_commercials(conn, client, shop_id=shop_id, commercial_ids=missing,
                                       order_source=order_source)
    # 补拉完成后才判质量：没有落在本窗口的 reconcile 凭证就维持 unknown，
    # 有凭证但存在归属未确认的成功退款则降为 failed（该范围之后禁止出数）。
    stats["quality"] = {
        entity: reconcile_source_quality(conn, shop_id=shop_id, entity=entity,
                                         start=start, end=run_end,
                                         source=order_source if entity == "orders" else AFTERSALE_SOURCE)
        for entity in ("orders", "aftersales_occurrence", "aftersales_cohort")
    }
    return stats


def _replay_entity(conn, client: KuaimaiClient, *, shop_id: str, entity: str,
                   start: datetime, end: datetime,
                   order_source: str = ORDER_SOURCE) -> int:
    """历史范围replay：业务时间窗口重跑并加入覆盖。"""
    count = 0
    if entity == "orders":
        for window in day_windows(start, end):
            count += _run_window(conn, client, entity="orders", shop_id=shop_id,
                                 window=window, mode="replay",
                                 order_source=order_source)
    elif entity == "aftersales_occurrence":
        for window in day_windows(start, end):
            count += _run_window(conn, client, entity="aftersales_occurrence",
                                 shop_id=shop_id, window=window, mode="replay")
    elif entity == "aftersales_cohort":
        for window in day_windows(start, end):
            check_cohort_window(conn, client, shop_id=shop_id, window=window)
    else:
        raise SystemExit(f"未知实体 {entity}")
    return count


def _probe(client: KuaimaiClient, *, shop_id: str, start: datetime,
           end: datetime, order_source: str = ORDER_SOURCE) -> dict[str, object]:
    """拉全页但只输出数量、金额字段覆盖和质量统计，不输出客户/订单号。"""
    window = Window(start, end)
    pay_total = Decimal(0)
    pay_present = 0
    pay_negative = 0
    order_count = 0
    commercial_ids: set[str] = set()
    tid_counts: dict[str, int] = {}
    for raw in fetch_window(client, entity="orders", shop_id=shop_id,
                            window=window, mode="probe",
                            order_source=order_source):
        order_count += 1
        amount = to_decimal(raw.get("payAmount"))
        if amount is not None:
            pay_present += 1
            if amount < 0:
                pay_negative += 1
            else:
                pay_total += amount
        tid = str(raw.get("tid") or "").strip()
        if tid:
            tid_counts[tid] = tid_counts.get(tid, 0) + 1
    commercial_ids = set(tid_counts)
    refund_count = 0
    refund_success = 0
    refund_amount = Decimal(0)
    for raw in fetch_window(client, entity="aftersales_occurrence", shop_id=shop_id,
                            window=window, mode="probe", order_source=order_source):
        aftersale = normalise_aftersale(raw)
        refund_count += 1
        if aftersale["platform_success"]:
            refund_success += 1
            if aftersale["raw_platform_amount"] is not None:
                refund_amount += aftersale["raw_platform_amount"]
    split_or_merge = sum(1 for c in commercial_ids if tid_counts[c] > 1)
    return {
        "window": {"start": _fmt(start), "end": _fmt(end)},
        "orders": order_count,
        "pay_amount_present": pay_present,
        "pay_amount_negative": pay_negative,
        "pay_amount_sum": str(pay_total),
        "commercials": len(commercial_ids),
        "split_or_merge_commercials": split_or_merge,
        "aftersales": refund_count,
        "platform_success": refund_success,
        "platform_success_amount": str(refund_amount),
    }


def _refresh_session(conn, client: KuaimaiClient, *, shop_id: str = "__company__") -> None:
    """同一锁内检查到期窗口、距上次调用至少一小时；只记录期限和成功时刻。"""
    source = "open.token.refresh"
    _ensure_state(conn, source, "session", shop_id)
    state = conn.execute(
        "SELECT token_expires_at, last_refresh_at FROM bi.sync_state "
        "WHERE source=%s AND entity='session' AND shop_id=%s",
        (source, shop_id),
    ).fetchone()
    now = datetime.now(BEIJING)
    expires_at, last_refresh = state
    if expires_at is not None and expires_at - now > timedelta(days=7):
        print(json.dumps({"action": "refresh-session", "skipped": "not_in_window"}))
        return
    if last_refresh is not None and now - last_refresh < timedelta(hours=1):
        print(json.dumps({"action": "refresh-session", "skipped": "rate_limited"}))
        return
    expires = client.refresh_session(now=now)
    conn.execute(
        "UPDATE bi.sync_state SET token_expires_at=%s, last_refresh_at=%s, "
        "last_success_at=%s, last_error_code=NULL "
        "WHERE source=%s AND entity='session' AND shop_id=%s",
        (expires, now, now, source, shop_id),
    )
    print(json.dumps({"action": "refresh-session", "expires_at": expires.isoformat()}))


def _parse_date(text: str) -> datetime:
    parsed = datetime.strptime(text, "%Y-%m-%d").replace(tzinfo=BEIJING)
    return parsed


def _setup_logging(log_dir: str = "logs") -> None:
    """一行JSON、字段白名单；文件轮转10MiB×5；不记录凭证/请求体/DSN。"""
    import json as json_module
    import logging.handlers
    import os as os_module

    class JsonFormatter(logging.Formatter):
        _ALLOWED = ("request_id", "tool", "entity", "shop_id", "window",
                    "rows", "duration_ms", "data_as_of", "error_code", "attempt",
                    "basis", "commercial_ref")

        def format(self, record: logging.LogRecord) -> str:
            payload = {"ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
                       "level": record.levelname, "logger": record.name}
            for key in self._ALLOWED:
                if hasattr(record, key):
                    payload[key] = getattr(record, key)
            if record.exc_text:
                payload["error_code"] = "exception"
            return json_module.dumps(payload, ensure_ascii=False)

    os_module.makedirs(log_dir, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(
        os_module.path.join(log_dir, "sync.log"), maxBytes=10 * 1024 * 1024,
        backupCount=5, encoding="utf-8")
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(logging.INFO)


@dataclass(frozen=True)
class CapabilityGrant:
    """一家店的指标能力重算结果。`granted` 只能来自对账证据，不是“同步跑过了”。"""

    shop_id: str
    platform: str
    current: frozenset[str]
    granted: frozenset[str]
    # 开通后仍不能拿数的问题（如覆盖层还只读交易源）：不能藏在“写入成功”后面。
    warnings: tuple[str, ...] = ()

    @property
    def changed(self) -> bool:
        return self.current != self.granted


_SHOP_PROFILES_SQL = """
SELECT shop_id, platform, capabilities FROM bi.shops WHERE shop_id = ANY(%s)
ORDER BY shop_id
"""

_SYNC_STATE_SQL = """
SELECT shop_id, source, entity, quality_status, quality_rule
FROM bi.sync_state WHERE shop_id = ANY(%s)
"""


def read_quality_evidence(conn, shop_ids) -> dict[str, dict[tuple[str, str], str]]:
    """逐店读回 `(source, entity) -> quality_status`。

    只认当前口径版本写下的结论：`quality_rule` 不是 `QUALITY_RULE` 的 passed 降级成
    unknown（与 `data_quality._effective_quality` 同一规则），否则口径升级后旧对账
    会长期挂在能力标签上。没有状态行就是未知，不是“确实没数据”。
    """
    evidence: dict[str, dict[tuple[str, str], str]] = {str(item): {} for item in shop_ids}
    for shop_id, source, entity, status, rule in conn.execute(
            _SYNC_STATE_SQL, ([str(shop_id) for shop_id in shop_ids],)).fetchall():
        effective = (status if rule == QUALITY_RULE else "unknown")
        evidence.setdefault(str(shop_id), {})[(str(source), str(entity))] = str(effective)
    return evidence


def capability_target_shops(conn, authorized, *, all_shops: bool = False) -> list[str]:
    """能力重算该动哪些店。

    默认只动授权范围（`BI_SHOP_IDS`）。但能力标签是长在 `bi.shops` 上的：范围外一家店
    曾经开通的能力不会随证据消失而回收，只能靠 `--all-shops` 扫全集。默认不这么做，
    是为了不让一次运维命令隐式改动本次部署不管的店铺。
    """
    if not all_shops:
        return sorted(str(shop_id) for shop_id in authorized)
    return [str(row[0]) for row in conn.execute(
        "SELECT shop_id FROM bi.shops ORDER BY shop_id").fetchall()]


def recompute_shop_capabilities(conn, shop_ids, *,
                                apply: bool = False) -> list[CapabilityGrant]:
    """按已登记的来源与逐源对账证据重算能力标签（默认只报告，不写库）。

    这是 capabilities 的唯一开通入口：同步成功、店铺档案存在、上游返回空都不算证据。
    证据消失时标签会被回收（包括回收为空白），否则一次意外对账就能永久开门。
    未登记平台报空白，不静默跳过：运维必须看得到“为什么这家店开不了”。
    """
    ids = [str(shop_id) for shop_id in shop_ids]
    if not ids:
        return []
    evidence = read_quality_evidence(conn, ids)
    grants: list[CapabilityGrant] = []
    for shop_id, platform, capabilities in conn.execute(
            _SHOP_PROFILES_SQL, (ids,)).fetchall():
        record = ShopRecord.from_row(shop_id, platform, capabilities)
        shop_evidence = evidence.get(str(shop_id), {})
        granted = capabilities_from_evidence(record.platform, shop_evidence)
        warnings: list[str] = []
        if granted and resolve_order_source(record) != ENTITY_SOURCES[ORDERS_ENTITY]:
            # 出库通道平台：能力已经开通，但覆盖门禁还只读交易源（Task 5.2 未交付）。
            # 不报出来，运维会看到“写了标签仍然缺数据”，然归因到覆盖上去。
            warnings.append("coverage_source_mismatch")
        grants.append(CapabilityGrant(
            shop_id=str(shop_id), platform=record.platform,
            current=frozenset(record.capabilities), granted=frozenset(granted),
            warnings=tuple(warnings)))
    if apply:
        # 一批写完：中途崩溃不能留下“一半店已回收、一半店还挂着旧标签”。
        with conn.transaction():
            for grant in grants:
                if not grant.changed:
                    continue
                conn.execute("UPDATE bi.shops SET capabilities=%s WHERE shop_id=%s",
                             (sorted(grant.granted), grant.shop_id))
                logger.info("capabilities updated shop=%s granted=%s rule=%s",
                            grant.shop_id, sorted(grant.granted), QUALITY_RULE)
    return grants


def main(argv: list[str] | None = None) -> int:
    _setup_logging()
    parser = argparse.ArgumentParser(prog="bi_agent.sync")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("shops")
    sub.add_parser("products", help="全量拉商品档案，为订单行补可读名称")
    probe = sub.add_parser("probe")
    probe.add_argument("--start", required=True, help="含，YYYY-MM-DD（北京时间）")
    probe.add_argument("--end", required=True, help="排他，YYYY-MM-DD（北京时间）")
    backfill = sub.add_parser("backfill")
    backfill.add_argument("--days", type=int, default=90)
    sub.add_parser("incremental")
    reconcile = sub.add_parser("reconcile")
    reconcile.add_argument("--days", type=int, default=7)
    replay = sub.add_parser("replay")
    replay.add_argument("--entity", required=True,
                        choices=["orders", "aftersales_occurrence", "aftersales_cohort"])
    replay.add_argument("--start", required=True)
    replay.add_argument("--end", required=True)
    sub.add_parser("refresh-session")
    capabilities = sub.add_parser(
        "capabilities",
        help="按逐来源对账证据重算指标能力标签（默认只报告差异，--apply 才写库）")
    capabilities.add_argument("--apply", action="store_true",
                              help="确实回写 bi.shops.capabilities")
    capabilities.add_argument("--all-shops", action="store_true",
                              help="覆盖 bi.shops 全集（回收授权范围外的旧标签），默认只动 BI_SHOP_IDS")
    args = parser.parse_args(argv)

    settings = load_sync_settings(os.environ)
    conn = _connect(settings.writer_dsn.get_secret_value())
    locked = False
    try:
        locked = conn.execute("SELECT pg_try_advisory_lock(%s)", (LOCK_ID,)).fetchone()[0]
        if not locked:
            raise RuntimeError("已有同步任务运行")
        try:
            assert_sync_schema(conn)
        except KuaimaiError as exc:
            raise SystemExit(exc.code) from None
        http = httpx.Client(timeout=30.0)
        client = KuaimaiClient(settings, http)
        try:
            if args.command == "shops":
                count = sync_shops(conn, client)
                print(json.dumps({"action": "shops", "updated": count}))
            elif args.command == "products":
                stats = sync_products(conn, client)
                print(json.dumps({"action": "products", "stats": stats}))
            elif args.command == "probe":
                shop_id = _require_single_shop(settings)
                summary = _probe(client, shop_id=shop_id,
                                 start=_parse_date(args.start), end=_parse_date(args.end),
                                 order_source=_shop_order_source(conn, shop_id))
                print(json.dumps(summary, ensure_ascii=False))
            elif args.command == "backfill":
                t0 = datetime.now(BEIJING)  # 回填开始前记录T0；完成后在_backfill_shop内补拉[T0,T1)
                for shop_id in sorted(settings.shop_ids):
                    before = GUARD_STATS.payment_downgrade_blocked
                    try:
                        order_source = _shop_order_source(conn, shop_id)
                        stats = _backfill_shop(conn, client, shop_id=shop_id,
                                               days=args.days, t0=t0,
                                               order_source=order_source)
                    except (KuaimaiError, SystemExit) as exc:
                        print(json.dumps({"action": "backfill", "shop_id": shop_id,
                                          "error": _shop_error(exc)}, ensure_ascii=False))
                        continue
                    stats["payment_downgrade_blocked"] = (
                        GUARD_STATS.payment_downgrade_blocked - before)
                    print(json.dumps({"action": "backfill", "shop_id": shop_id,
                                      "order_source": order_source, "stats": stats}))
            elif args.command == "incremental":
                run_end = datetime.now(BEIJING)
                for shop_id in sorted(settings.shop_ids):
                    before = GUARD_STATS.payment_downgrade_blocked
                    try:
                        order_source = _shop_order_source(conn, shop_id)
                        stats = _incremental_shop(conn, client, shop_id=shop_id,
                                                  run_end=run_end,
                                                  order_source=order_source)
                    except (KuaimaiError, SystemExit) as exc:
                        print(json.dumps({"action": "incremental", "shop_id": shop_id,
                                          "error": _shop_error(exc)}, ensure_ascii=False))
                        continue
                    stats["payment_downgrade_blocked"] = (
                        GUARD_STATS.payment_downgrade_blocked - before)
                    print(json.dumps({"action": "incremental", "shop_id": shop_id,
                                      "stats": stats}))
            elif args.command == "reconcile":
                run_end = datetime.now(BEIJING)
                for shop_id in sorted(settings.shop_ids):
                    before = GUARD_STATS.payment_downgrade_blocked
                    try:
                        order_source = _shop_order_source(conn, shop_id)
                        stats = _reconcile_shop(conn, client, shop_id=shop_id,
                                                days=args.days, run_end=run_end,
                                                order_source=order_source)
                    except (KuaimaiError, SystemExit) as exc:
                        print(json.dumps({"action": "reconcile", "shop_id": shop_id,
                                          "error": _shop_error(exc)}, ensure_ascii=False))
                        continue
                    stats["payment_downgrade_blocked"] = (
                        GUARD_STATS.payment_downgrade_blocked - before)
                    print(json.dumps({"action": "reconcile", "shop_id": shop_id,
                                      "stats": stats}))
            elif args.command == "replay":
                start = _parse_date(args.start)
                end = _parse_date(args.end)
                if (end - start).days > MAX_QUERY_DAYS:
                    raise SystemExit(f"replay跨度最多{MAX_QUERY_DAYS}天")
                for shop_id in sorted(settings.shop_ids):
                    before = GUARD_STATS.payment_downgrade_blocked
                    try:
                        order_source = _shop_order_source(conn, shop_id)
                        count = _replay_entity(conn, client, shop_id=shop_id,
                                               entity=args.entity, start=start, end=end,
                                               order_source=order_source)
                    except (KuaimaiError, SystemExit) as exc:
                        print(json.dumps({"action": "replay", "shop_id": shop_id,
                                          "error": _shop_error(exc)}, ensure_ascii=False))
                        continue
                    print(json.dumps({"action": "replay", "shop_id": shop_id,
                                      "entity": args.entity, "accepted": count,
                                      "payment_downgrade_blocked":
                                          GUARD_STATS.payment_downgrade_blocked - before}))
            elif args.command == "capabilities":
                targets = capability_target_shops(
                    conn, settings.shop_ids, all_shops=args.all_shops)
                grants = recompute_shop_capabilities(conn, targets, apply=args.apply)
                for grant in grants:
                    print(json.dumps({
                        "action": "capabilities", "shop_id": grant.shop_id,
                        "platform": grant.platform, "mode": "apply" if args.apply else "report",
                        "quality_rule": QUALITY_RULE,
                        "current": sorted(grant.current), "granted": sorted(grant.granted),
                        "changed": grant.changed, "warnings": list(grant.warnings)},
                        ensure_ascii=False))
            elif args.command == "refresh-session":
                _refresh_session(conn, client)
        finally:
            http.close()
    finally:
        if locked:
            conn.execute("SELECT pg_advisory_unlock(%s)", (LOCK_ID,))
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
