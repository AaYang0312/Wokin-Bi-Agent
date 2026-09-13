"""跨渠道商品 / SKU 标识映射（运营工作流计划 Task 6）。

只有**已确认的显式标识映射**才构成合并依据：平台商品名相同不是依据，成交行也只能证明
「这家店卖过这个 ERP 商品的这个 SKU」，不能证明渠道链接的存在。身份里必须带
`namespace`（账号/租户范围）——另一个账号里的相同数字 ID 不是同一个商品。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date
from typing import Iterable, Sequence

# 映射口径版本：参与结果血缘，版本一变旧结果不得复用。
CHANNEL_MAPPING_VERSION = "channel-map/2026-09-12.1"
DEFAULT_NAMESPACE = "acct-default"

_UPSERT_SQL = """
INSERT INTO bi.channel_items (
    namespace, platform, shop_id, listing_id, platform_sku_id,
    erp_product_id, erp_sku_id, status, source, evidence, mapping_version, valid_from
) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (namespace, shop_id, listing_id, platform_sku_id, valid_from) DO UPDATE SET
    platform = EXCLUDED.platform,
    erp_product_id = EXCLUDED.erp_product_id,
    erp_sku_id = EXCLUDED.erp_sku_id,
    status = EXCLUDED.status,
    source = EXCLUDED.source,
    evidence = EXCLUDED.evidence,
    mapping_version = EXCLUDED.mapping_version,
    valid_to = EXCLUDED.valid_to,
    recorded_at = now()
"""

_SELECT_SQL = """
SELECT namespace, platform, shop_id, listing_id, platform_sku_id,
       erp_product_id, erp_sku_id, status, source, mapping_version
FROM reporting.v_channel_items
WHERE erp_product_id = %s AND valid_from <= %s
  AND (valid_to IS NULL OR valid_to >= %s)
  AND shop_id = ANY(%s)
ORDER BY namespace, shop_id, listing_id, platform_sku_id
"""


@dataclass(frozen=True)
class IdentifierMapping:
    """一条标识映射。`evidence` 为空就不许落库：没有依据的合并等于猜。"""

    namespace: str
    platform: str
    shop_id: str
    erp_product_id: str
    listing_id: str = ""
    platform_sku_id: str = ""
    erp_sku_id: str = ""
    evidence: str = ""
    status: str = "approved"
    source: str = "manual_map"
    valid_from: date = date(1970, 1, 1)
    valid_to: date | None = None
    mapping_version: str = CHANNEL_MAPPING_VERSION


@dataclass(frozen=True)
class ChannelItem:
    """某个 ERP 商品在一家店 / 一个链接上的映射事实。"""

    namespace: str
    platform: str
    shop_id: str
    listing_id: str
    platform_sku_id: str
    erp_product_id: str
    erp_sku_id: str
    status: str
    source: str
    mapping_version: str


def record_identifier_mapping(conn, mapping: IdentifierMapping, *,
                              at: date | None = None) -> IdentifierMapping:
    """写入一条显式标识映射；缺证据或缺店铺/商品身份直接拒绝。"""
    if not mapping.evidence.strip():
        raise ValueError("mapping_evidence_required")
    if not mapping.shop_id or not mapping.erp_product_id:
        raise ValueError("mapping_identity_incomplete")
    if mapping.status == "approved" and mapping.source == "manual_map" \
            and not mapping.listing_id:
        # 人工映射的意义就是补上渠道链接号；空号应该走成交行那条路。
        raise ValueError("manual_map_needs_listing")
    valid_from = at or mapping.valid_from
    row = replace(mapping, valid_from=valid_from) if valid_from != mapping.valid_from \
        else mapping
    conn.execute(_UPSERT_SQL, (
        row.namespace, row.platform, row.shop_id, row.listing_id, row.platform_sku_id,
        row.erp_product_id, row.erp_sku_id or None, row.status, row.source,
        row.evidence, row.mapping_version, row.valid_from))
    return row


def record_trade_line_mapping(conn, *, namespace: str, platform: str, shop_id: str,
                              erp_product_id: str, erp_sku_id: str = "",
                              at: date | None = None) -> IdentifierMapping:
    """从成交行登记 ERP 侧身份。

    成交行给不出渠道链接号，所以 `listing_id` 留空——它只证明“这家店卖过这个
    ERP 商品的这个 SKU”，不代表渠道上有哪个链接；上架全集必须由已核验的渠道来源
    填充，不能从历史成交推导。
    """
    return record_identifier_mapping(conn, IdentifierMapping(
        namespace=namespace, platform=platform, shop_id=shop_id,
        erp_product_id=erp_product_id, erp_sku_id=erp_sku_id,
        evidence=f"trade_line:{shop_id}:{erp_product_id}:{erp_sku_id}",
        source="trade_line"), at=at)


def channel_items_for(conn, *, erp_product_id: str,
                      authorized_shop_ids: Iterable[str], at: date) -> list[ChannelItem]:
    """读取某个 ERP 商品在授权范围内的映射；范围外的行一条都不返回。"""
    shops = sorted({str(item) for item in authorized_shop_ids})
    if not erp_product_id or not shops:
        return []
    rows = conn.execute(_SELECT_SQL, (erp_product_id, at, at, shops)).fetchall()
    return [ChannelItem(*(str(value) if value is not None else "" for value in row))
            for row in rows]


def mapped_product_ids(conn, *, authorized_shop_ids: Sequence[str]) -> set[str]:
    """授权范围内有映射记录的 ERP 商品集合（供解析时判断引用是否可达）。"""
    shops = sorted({str(item) for item in authorized_shop_ids})
    if not shops:
        return set()
    return {str(row[0]) for row in conn.execute(
        "SELECT DISTINCT erp_product_id FROM reporting.v_channel_items "
        "WHERE shop_id = ANY(%s)", (shops,)).fetchall() if row[0]}
