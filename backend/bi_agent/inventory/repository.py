"""库存预警的数据访问：池与连接关系、两批快照、阈值策略读取。

四条结构性约束：

1. **读走视图，写走授权**。预警图以 `bi_app` 身份连接，快照只能从 `reporting.v_*` 读
   （019 把底表从 bi_app 收回）。取证凭据 `evidence` / `scan_evidence` 不在视图里，
   所以模型路径上任何一层都拿不到它——不是"拿到但不展示"。
2. **每个口径只取本轮那一批快照**：实物一条按池集合、渠道一条按店铺集合。逐店 / 逐池
   循环会让一次预警在不同池上读到不同数据版本。
3. **池授权在 SQL 层就先收窄**（带 `pool_id = ANY(获准池)`），图里再判一次只是纵深防御。
4. **引用不能反查主键**：`ent-` 是 (kind, 主键) 的单向摘要，007 也不把引用→主键映射
   暴露给应用身份。所以点名 SKU 时，本轮在**授权范围内的落点表**上派生引用后比对
   （与 `catalog.resolver._resolve_by_ref` 同一做法），不猜、不反推。
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, NamedTuple, Sequence

from bi_agent.catalog.models import EntityKind, ref_for_key
from bi_agent.commerce.repository import ShopProfile, shop_profiles

from .models import ChannelRow, InventoryPool, PhysicalRow, Threshold
from .rules import (
    CHANNEL_SOURCE_KINDS, PHYSICAL_SOURCE_KINDS, THRESHOLD_LEVELS,
    normalize_quantity,
    pool_handle, quantity_precision, warehouse_handle)

MAX_POOLS_PER_QUERY = 200
MAX_SHOPS_PER_QUERY = 200
# 范围内 SKU 全集的扫描上限：扫不完时必须是 fail closed 的"范围过大"，
# 不能把"没扫到"说成"这个 SKU 没有库存记录"（与 catalog.resolver 同一口径）。
MAX_SKU_KEYS = 2000

DEFAULT_NAMESPACE = "acct-default"

__all__ = [
    "AuthorizedPools", "ChannelRow", "InventoryPool", "PhysicalRow", "ShopProfile",
    "Threshold", "authorized_pool_pairs", "authorized_pool_pairs_by_ids", "channel_snapshots",
    "insert_channel_snapshot", "insert_channel_snapshot_item",
    "insert_physical_snapshot", "insert_physical_snapshot_item",
    "connected_pairs", "insert_threshold_policy", "inventory_sku_keys", "load_thresholds", "physical_snapshots",
    "pool_connections", "shop_profiles", "sku_keys_in_scope",
]


class AuthorizedPools(NamedTuple):
    """本轮获准 / 未获准的池集合。

    未获准的池只带**句柄**回来：`excluded_scope` 要能说出"有一个池没算进来"，但不能
    因此把真实池号或标签交给一个没有该池权限的请求方。
    """

    granted: tuple[InventoryPool, ...]
    excluded: tuple[dict[str, str], ...]


_POOLS_BY_SHOPS_SQL = """
SELECT p.namespace, p.pool_id, p.label, p.connection_kind,
       coalesce(array_agg(cs.shop_id ORDER BY cs.shop_id)
                FILTER (WHERE cs.shop_id IS NOT NULL), '{}'::text[])
FROM reporting.v_inventory_pools p
LEFT JOIN reporting.v_inventory_pool_shops cs
       ON cs.namespace = p.namespace AND cs.pool_id = p.pool_id
WHERE cs.shop_id = ANY(%s)
GROUP BY p.namespace, p.pool_id, p.label, p.connection_kind
ORDER BY p.namespace, p.pool_id
"""


def connected_pairs(conn, *, shop_ids: Sequence[str]) -> list[tuple[str, str]]:
    """与这些店铺有连接关系的全部池（**不做**授权过滤）。

    只给"哪些池存在、服务谁"这一件事用：让未授权排除能说出"有个池没算进来"。
    这里读不到任何数量、标签以外的东西，句柄也不是凭证。
    """
    shops = sorted({str(shop) for shop in shop_ids if str(shop or "").strip()})
    if not shops:
        return []
    rows = conn.execute(_POOLS_BY_SHOPS_SQL, (shops,)).fetchall()
    return [(str(row[0]), str(row[1])) for row in rows]


def authorized_pool_pairs(conn, *, shop_ids: Sequence[str],
                          allowed_pool_ids: Sequence[str]) -> list[tuple[str, str]]:
    """与本轮授权店铺有连接关系、且在这个主体被授权的 (账号范围, 池号)。

    两个集合是**交集**：店铺授权推导不出池授权（spec §5.5），池授权也不会凭空带出
    没获准的店铺。
    """
    shops = sorted({str(shop) for shop in shop_ids if str(shop or "").strip()})
    allowed = {str(pool) for pool in allowed_pool_ids if str(pool or "").strip()}
    if not shops or not allowed:
        return []
    rows = conn.execute(_POOLS_BY_SHOPS_SQL, (shops,)).fetchall()
    return [(str(row[0]), str(row[1])) for row in rows if str(row[1]) in allowed]


_POOLS_BY_IDS_SQL = """
SELECT namespace, pool_id
FROM reporting.v_inventory_pools
WHERE pool_id = ANY(%s)
ORDER BY namespace, pool_id
"""


def authorized_pool_pairs_by_ids(conn, *, allowed_pool_ids: Sequence[str],
                                 deadline: float) -> list[tuple[str, str]]:
    """按池授权集直接派生 (账号范围, 池号)：monitor 空店铺投影的读取面。

    与 `pool_connections` 同一授权面（SQL 侧 `pool_id = ANY(获准池)`），只读池
    身份列，不读任何数量、标签或快照证据。与 `authorized_pool_pairs` 的差别只
    在推导方向：那一版经店铺连接交集（`shop_ids` 入参），这一版由服务端已收窄
    的池授权集直接给出——monitor 的空店铺投影下店铺侧交集恒为空。调用方仍要
    经过 `pool_connections` 的逐池授权复核（第二道闸）。
    """
    allowed = sorted({str(pool) for pool in allowed_pool_ids
                      if str(pool or "").strip()})
    if not allowed:
        return []
    _check_budget(conn, deadline)
    rows = conn.execute(_POOLS_BY_IDS_SQL, (allowed,)).fetchall()
    return [(str(row[0]), str(row[1])) for row in rows]


_POOL_SHOP_LINKS_SQL = """
SELECT namespace, pool_id, shop_id
FROM reporting.v_inventory_pool_shops
WHERE namespace = ANY(%s) AND pool_id = ANY(%s)
ORDER BY namespace, pool_id, shop_id
"""


def pool_connections(conn, *, namespace_pool_ids: Sequence[tuple[str, str]],
                     allowed_pool_ids: frozenset[str]) -> AuthorizedPools:
    """取池声明与连接关系；未获准的只回句柄。"""
    wanted = sorted({(str(ns), str(pool)) for ns, pool in namespace_pool_ids})
    if not wanted:
        return AuthorizedPools((), ())
    if len(wanted) > MAX_POOLS_PER_QUERY:
        raise ValueError("inventory_pool_scope_too_large")
    namespaces = sorted({ns for ns, _pool in wanted})
    pool_ids = sorted({pool for _ns, pool in wanted})
    rows = conn.execute(
        """SELECT namespace, pool_id, label, connection_kind
           FROM reporting.v_inventory_pools
           WHERE namespace = ANY(%s) AND pool_id = ANY(%s)
           ORDER BY namespace, pool_id""", (namespaces, pool_ids)).fetchall()
    # 两个独立的 ANY() 会把 (A,P1) 与 (B,P2) 扩成 A,P2 / B,P1 四格：授权是成对事实，
    # 所以在这里按请求的精确配对过滤掉多出来的三格。今天只有一个 namespace，扩不出
    # 来；接进多账号授权的那天，这一行过滤就是唯一的边界。
    wanted_pairs = set(wanted)
    rows = [row for row in rows
            if (str(row[0]), str(row[1])) in wanted_pairs]
    links = conn.execute(_POOL_SHOP_LINKS_SQL, (namespaces, pool_ids)).fetchall()
    shops_of: dict[tuple[str, str], list[str]] = {}
    for row in links:
        shops_of.setdefault((str(row[0]), str(row[1])), []).append(str(row[2]))
    granted: list[InventoryPool] = []
    excluded: list[dict[str, str]] = []
    seen: set[str] = set()
    for row in rows:
        namespace, pool_id = str(row[0]), str(row[1])
        handle = pool_handle(namespace, pool_id)
        # 授权集目前是裸池号（服务端给的就是这一层）；句柄里带 namespace，所以同一
        # 池号在另一个账号范围里会算成另一个句柄。这里按池号比对，配对过滤在上面已经
        # 把不属于本轮请求的行去掉了。
        if pool_id not in allowed_pool_ids:
            if handle not in seen:
                seen.add(handle)
                excluded.append({"pool_ref": handle, "reason": "pool_not_authorized"})
            continue
        granted.append(InventoryPool(
            pool_id=pool_id, pool_ref=handle, namespace=namespace,
            label=str(row[2] or ""), connection_kind=str(row[3] or "shared"),
            shop_ids=tuple(shops_of.get((namespace, pool_id), ()))))
    return AuthorizedPools(tuple(granted), tuple(excluded))


# 每个 (池, 仓库) 只取**当前那一次**盘点，然后把那一次的明细全部读回来。
# 比它更早的那些不读：把两次扫描的数加成总量，就是报一个从没盘出来的库存数（spec §6）。
_PHYSICAL_LATEST_SQL = """
SELECT namespace, pool_id, warehouse_id, snapshot_id
FROM (
    SELECT namespace, pool_id, warehouse_id, snapshot_id,
           rank() OVER (PARTITION BY namespace, pool_id, warehouse_id
                        ORDER BY captured_at DESC) AS current_scan
    FROM reporting.v_physical_stock_snapshots
    WHERE namespace = ANY(%s) AND pool_id = ANY(%s) AND captured_at <= %s
) scan
WHERE current_scan = 1
ORDER BY namespace, pool_id, warehouse_id, snapshot_id
"""

# `{tuples}` 由 `_values_tuples` 按"当前那一次"的行数展开：占位符个数必须与参数个数
# 一致，所以它在运行期拼，而不是提前固定一个上限。
_PHYSICAL_ITEMS_SQL = """
SELECT i.namespace, i.pool_id, i.warehouse_id, i.snapshot_id, i.erp_sku_id,
       i.available_quantity, i.inbound_quantity, i.locked_quantity, i.unit, i.batch_id,
       s.source, s.captured_at, s.scan_complete
FROM reporting.v_physical_stock_items i
JOIN reporting.v_physical_stock_snapshots s
  ON s.namespace = i.namespace AND s.snapshot_id = i.snapshot_id
 AND s.pool_id = i.pool_id AND s.warehouse_id = i.warehouse_id
JOIN (VALUES {tuples}) AS current_scan (namespace, pool_id, warehouse_id, snapshot_id)
  ON current_scan.namespace = i.namespace AND current_scan.pool_id = i.pool_id
 AND current_scan.warehouse_id = i.warehouse_id
 AND current_scan.snapshot_id = i.snapshot_id
{sku_filter}ORDER BY i.namespace, i.pool_id, i.warehouse_id, i.erp_sku_id, i.unit,
       i.batch_id
"""

# SKU 过滤子句单独命名：`products=all` 那一途要把整段拿掉，而不是传一个空列表进去
# （空列表在 SQL 里是 `= ANY('{}')`，返回零行，会被读成"这些池都没有库存记录"）。
_SKU_FILTER = "WHERE i.erp_sku_id = ANY(%s)\n"
_CHANNEL_SKU_CLAUSE = "AND i.erp_sku_id = ANY(%s)"

# 一家店只取**当前那一次抓取**：与实物同一批规则。把两次抓取的显示数加成"现在还剩
# 多少"，报出来的是一个从没成立过的数——而它正好决定要不要给这家店调配额。
_CHANNEL_LATEST_SQL = """
SELECT namespace, shop_id, snapshot_id
FROM (
    SELECT namespace, shop_id, snapshot_id,
           rank() OVER (PARTITION BY namespace, shop_id
                        ORDER BY captured_at DESC) AS current_scan
    FROM reporting.v_channel_stock_snapshots
    WHERE namespace = ANY(%s) AND shop_id = ANY(%s) AND captured_at <= %s
) scan
WHERE current_scan = 1
ORDER BY namespace, shop_id, snapshot_id
"""

_CHANNEL_SNAPSHOTS_SQL = """
SELECT i.snapshot_id, i.namespace, i.shop_id, s.platform, s.source, s.captured_at,
       s.scan_complete, i.listing_id, i.platform_sku_id, i.erp_sku_id,
       i.sellable_quantity, i.unit
FROM reporting.v_channel_stock_items i
-- 连接键必须与快照主键一致 (namespace, shop_id, snapshot_id)：三家店共用一个
-- snapshot_id 时，只按 snapshot_id 连接会把每家的一行放大成三行，可售数就被加成 300。
JOIN reporting.v_channel_stock_snapshots s
  ON s.namespace = i.namespace AND s.shop_id = i.shop_id
 AND s.snapshot_id = i.snapshot_id
JOIN (VALUES {tuples}) AS current_scan (namespace, shop_id, snapshot_id)
  ON current_scan.namespace = i.namespace AND current_scan.shop_id = i.shop_id
 AND current_scan.snapshot_id = i.snapshot_id
WHERE i.namespace = ANY(%s) AND i.shop_id = ANY(%s)
  {sku_clause}
ORDER BY i.namespace, i.shop_id, i.erp_sku_id, i.listing_id, i.platform_sku_id
"""


class PhysicalRead(NamedTuple):
    """实物读数。

    曾经这里还带一个"同刻并列批次"的歧义标记，但那会误伤一个 SKU 合法分在两批的
    情形：批次本来就是去重身份的一部分，同身份出现两个数量由 `sum_unique_physical_stock`
    直接判冲突。两套信号表达同一件事，就会有两套互相矛盾的结论。
    """

    rows: tuple[PhysicalRow, ...] = ()


class ChannelRead(NamedTuple):
    """渠道可售读数（只含每家店当前那一次抓取）。"""

    rows: tuple[ChannelRow, ...] = ()


def _values_tuples(arity: int, count: int) -> str:
    """`VALUES` 行：每一列都显式 CAST。

    `VALUES` 里的裸参数没有类型可推，Postgres 会直接报 could not determine data type
    of parameter；占位符个数也必须与参数个数一致，所以它在运行期按行数展开。
    """
    one = ", ".join("CAST(%s AS text)" for _ in range(arity))
    return ", ".join("({one})".format(one=one) for _ in range(count))


def physical_snapshots(conn, *, namespace_pool_ids: Sequence[tuple[str, str]],
                       erp_sku_ids: Sequence[str] | None, as_of: datetime,
                       deadline: float) -> PhysicalRead:
    """各 (池, 仓库) 当前那一次盘点的明细（不跨扫描时刻混合，也不按行去重）。

    两次查询跑在同一只读快照里（由调用方的 `read_only_snapshot` 提供）：分开跑会让
    "该用哪一次"与"这一次里有什么"来自两个数据版本，那正是混批合计换了个形式回来。
    """
    _check_budget(conn, deadline)
    pairs = sorted({(str(ns), str(pool)) for ns, pool in namespace_pool_ids})
    if not pairs:
        return PhysicalRead()
    if erp_sku_ids is None:
        skus: list[str] | None = None
    else:
        skus = sorted({str(sku) for sku in erp_sku_ids if str(sku or "").strip()})
        if not skus:
            return PhysicalRead()
    latest = conn.execute(_PHYSICAL_LATEST_SQL, (
        sorted({ns for ns, _pool in pairs}),
        sorted({pool for _ns, pool in pairs}), as_of)).fetchall()
    if not latest:
        return PhysicalRead()
    params: list[Any] = []
    for row in latest:
        params.extend([str(row[0]), str(row[1]), str(row[2]), str(row[3])])
    sql = _PHYSICAL_ITEMS_SQL.replace("{tuples}", _values_tuples(4, len(latest)))
    # 两个方向都要替换：只处理"不筛"那一头，会把 `{sku_filter}` 原样留在 SQL 里，
    # 而参数个数与占位符个数不一致时 psycopg 报的是"占位符少了参数"，看起来像上游
    # 传错了列表，其实是一个占位符从来没被填过。
    if skus is None:
        sql = sql.replace("{sku_filter}", "")
    else:
        sql = sql.replace("{sku_filter}", _SKU_FILTER)
        params.append(skus)
    rows = conn.execute(sql, tuple(params)).fetchall()
    out: list[PhysicalRow] = []
    for row in rows:
        namespace, pool_id, warehouse_id = str(row[0]), str(row[1]), str(row[2])
        out.append(PhysicalRow(
            snapshot_id=str(row[3]), namespace=namespace, pool_id=pool_id,
            pool_ref=pool_handle(namespace, pool_id), warehouse_id=warehouse_id,
            warehouse_ref=warehouse_handle(namespace, warehouse_id),
            erp_sku_id=str(row[4] or ""),
            sku_ref=ref_for_key(EntityKind.SKU.value, str(row[4] or "")),
            batch_id=str(row[9] or ""), unit=str(row[8] or ""),
            available_quantity=_text(row[5]), inbound_quantity=_text(row[6]),
            locked_quantity=_text(row[7]), source=str(row[10] or "erp"),
            captured_at=row[11], scan_complete=bool(row[12])))
    return PhysicalRead(tuple(out))


def channel_snapshots(conn, *, namespaces: Sequence[str], shop_ids: Sequence[str],
                      erp_sku_ids: Sequence[str] | None, as_of: datetime,
                      deadline: float) -> ChannelRead:
    """各店**当前那一次**抓取的渠道可售读数（与实物分开取源，永不参与实物总量）。"""
    _check_budget(conn, deadline)
    shops = sorted({str(shop) for shop in shop_ids if str(shop or "").strip()})
    if not shops:
        return ChannelRead()
    if len(shops) > MAX_SHOPS_PER_QUERY:
        raise ValueError("inventory_shop_scope_too_large")
    ns_list = sorted({str(ns) for ns in namespaces})
    current = conn.execute(_CHANNEL_LATEST_SQL, (ns_list, shops, as_of)).fetchall()
    if not current:
        return ChannelRead()
    if erp_sku_ids is None:
        skus: list[str] | None = None
    else:
        skus = sorted({str(sku) for sku in erp_sku_ids if str(sku or "").strip()})
        if not skus:
            return ChannelRead()
    params: list[Any] = []
    for row in current:
        params.extend([str(row[0]), str(row[1]), str(row[2])])
    # 参数顺序必须与 SQL 里的占位符顺序一致：不筛 SKU 时那个参数也一起消失。
    params += [ns_list, shops]
    sql = _CHANNEL_SNAPSHOTS_SQL.replace("{tuples}", _values_tuples(3, len(current)))
    if skus is None:
        sql = sql.replace("{sku_clause}", "")
    else:
        sql = sql.replace("{sku_clause}", _CHANNEL_SKU_CLAUSE)
        params.append(skus)
    rows = conn.execute(sql, tuple(params)).fetchall()
    return ChannelRead(tuple(ChannelRow(
            snapshot_id=str(row[0]), namespace=str(row[1]), shop_id=str(row[2]),
            platform=str(row[3] or ""), listing_id=str(row[7] or ""),
            platform_sku_id=str(row[8] or ""), erp_sku_id=str(row[9] or ""),
            sku_ref=ref_for_key(EntityKind.SKU.value, str(row[9] or "")),
            sellable_quantity=_text(row[10]), unit=str(row[11] or ""),
            captured_at=row[5], source=str(row[4] or ""),
                scan_complete=bool(row[6])) for row in rows))


_THRESHOLDS_SQL = """
SELECT t.policy_version, t.level, t.erp_sku_id, t.pool_id, t.shop_id, t.quantity,
       t.unit, coalesce(p.namespace, %s)
FROM reporting.v_inventory_threshold_policies t
LEFT JOIN reporting.v_inventory_pools p ON p.pool_id = t.pool_id
WHERE {sku_filter}t.effective_at <= %s
  AND (t.expires_at IS NULL OR t.expires_at > %s)
  AND (t.pool_id IS NULL OR t.pool_id = ANY(%s))
  AND (t.shop_id IS NULL OR t.shop_id = ANY(%s))
  AND (%s::text IS NULL OR t.policy_version = %s::text)
ORDER BY t.erp_sku_id, t.level, t.pool_id NULLS FIRST, t.shop_id NULLS FIRST
"""


def load_thresholds(conn, *, erp_sku_ids: Sequence[str] | None, pool_ids: Sequence[str],
                    shop_ids: Sequence[str], at: date, version: str | None,
                    namespace: str = DEFAULT_NAMESPACE) -> tuple[Threshold, ...]:
    """读已配置的版本化阈值（给了策略引用时只读那一版）。

    阈值是**配置**，不是本轮用户输入：它的适用范围同样受池 / 店铺授权约束——越权的
    行不进判定。
    """
    # `erp_sku_ids=None`：全商品那一途在本节点还看不到 SKU 全集（快照要到下一格才读），
    # 所以按范围读全部策略、由 `_threshold_for` 按 SKU 引用逐格匹配。留一个"先筛 SKU"
    # 的读法，会让 all 模式配了策略也读不到，把"有配置"错报成 unconfigured。
    skus = None if erp_sku_ids is None else sorted(
        {str(sku) for sku in erp_sku_ids if str(sku or "").strip()})
    if skus == []:
        return ()
    params: list[Any] = [namespace]
    sql = _THRESHOLDS_SQL
    if skus is not None:
        params.append(skus)
        sql = sql.replace("{sku_filter}", "t.erp_sku_id = ANY(%s) AND ")
    else:
        sql = sql.replace("{sku_filter}", "")
    params.extend([at, at,
                   sorted({str(pool) for pool in pool_ids}),
                   sorted({str(shop) for shop in shop_ids}), version, version])
    rows = conn.execute(sql, tuple(params)).fetchall()
    out: list[Threshold] = []
    for row in rows:
        pool_id = str(row[3]) if row[3] else ""
        shop_id = str(row[4]) if row[4] else ""
        out.append(Threshold(
            level=str(row[1]), sku_ref=ref_for_key(EntityKind.SKU.value, str(row[2] or "")),
            quantity=_text(row[5]) or "", unit=str(row[6] or "piece"),
            shop_ref=ref_for_key(EntityKind.SHOP.value, shop_id) if shop_id else "",
            pool_ref=pool_handle(str(row[7] or namespace), pool_id) if pool_id else "",
            version=str(row[0] or "")))
    return tuple(out)


# 库存侧的 SKU 身份：两张明细表里出现过的 ERP SKU。
#
# 为什么不能只用渠道映射表（Task 6 的 `v_channel_items`）当全集：库存是 ERP 侧事实，
# 一个还没建渠道映射的 SKU 照样能有货。把映射当身份前提，"没有映射的 SKU 有库存"
# 这一格就会被从分母里抖掉——那正是"漏检"最安静的发生方式。
_INVENTORY_SKU_KEYS_SQL = """
SELECT DISTINCT erp_sku_id FROM (
    SELECT i.erp_sku_id
    FROM reporting.v_physical_stock_items i
    -- 连接键必须是完整快照主键：只按 (namespace, snapshot_id) 连接时，同一个扫描号
    -- 在两个池上都出现过，未授权池的 SKU 就会顺着另一池的快照行挤进全集。那样一个
    -- 本该报"身份没解析出来"的引用会被当成已解析——这是最安静的一种越权读。
    JOIN reporting.v_physical_stock_snapshots s
      ON s.namespace = i.namespace AND s.snapshot_id = i.snapshot_id
     AND s.pool_id = i.pool_id AND s.warehouse_id = i.warehouse_id
    WHERE s.namespace = ANY(%s) AND s.pool_id = ANY(%s)
    UNION
    SELECT i.erp_sku_id
    FROM reporting.v_channel_stock_items i
    JOIN reporting.v_channel_stock_snapshots s
      ON s.namespace = i.namespace AND s.shop_id = i.shop_id
     AND s.snapshot_id = i.snapshot_id
    WHERE i.shop_id = ANY(%s)
) known
ORDER BY erp_sku_id
LIMIT %s
"""


def inventory_sku_keys(conn, *, namespace_pool_ids: Sequence[tuple[str, str]],
                       shop_ids: Sequence[str]) -> tuple[tuple[str, ...], list[str]]:
    """已授权快照里出现过的 ERP SKU 集合；扫不完时返回阻塞原因（fail closed）。"""
    pairs = sorted({(str(ns), str(pool)) for ns, pool in namespace_pool_ids})
    shops = sorted({str(shop) for shop in shop_ids if str(shop or "").strip()})
    if not pairs and not shops:
        return (), []
    wanted = MAX_SKU_KEYS + 1
    rows = conn.execute(_INVENTORY_SKU_KEYS_SQL, (
        sorted({ns for ns, _p in pairs}), sorted({p for _ns, p in pairs}), shops,
        wanted)).fetchall()
    if len(rows) >= wanted:
        return (), ["sku_scope_too_large"]
    return tuple(str(row[0]) for row in rows), []


_SKU_KEYS_SQL = """
SELECT DISTINCT erp_sku_id
FROM reporting.v_channel_items
WHERE shop_id = ANY(%s) AND status = 'approved' AND erp_sku_id <> ''
ORDER BY erp_sku_id
LIMIT %s
"""


def sku_keys_in_scope(conn, *, shop_ids: Sequence[str],
                      extra: Sequence[str] = ()) -> tuple[tuple[str, ...], list[str]]:
    """本轮授权范围内的 ERP SKU 全集（渠道落点表 + 快照里出现的 SKU）。

    返回 `(keys, truncated_reason)`：扫不完时第二项非空，调用方必须 fail closed，
    不能把"没扫到"说成"这个 SKU 没有库存记录"。
    """
    shops = sorted({str(shop) for shop in shop_ids if str(shop or "").strip()})
    wanted = MAX_SKU_KEYS + 1
    keys: list[str] = []
    if shops:
        rows = conn.execute(_SKU_KEYS_SQL, (shops, wanted)).fetchall()
        if len(rows) >= wanted:
            return [], ["sku_scope_too_large"]
        keys.extend(str(row[0]) for row in rows)
    keys.extend(str(item) for item in extra if str(item or "").strip())
    return tuple(sorted(set(keys))), []


def resolve_sku_refs(conn, *, wanted_refs: Sequence[str], shop_ids: Sequence[str],
                     extra_keys: Sequence[str] = ()) -> tuple[tuple[str, ...],
                                                              tuple[str, ...]]:
    """点名的 SKU 引用 → 授权范围内的 ERP 主键（派生后比对，不反查）。

    返回 `(matched_ids, unresolved_refs)`：解析不出的引用不是"0 库存"，是身份还没
    确定，调用方必须把它说成未解析，不能让它进总量。
    """
    keys, blocked = sku_keys_in_scope(conn, shop_ids=shop_ids, extra=extra_keys)
    if blocked:
        return (), tuple(sorted({str(ref) for ref in wanted_refs}))
    derived = {ref_for_key(EntityKind.SKU.value, key): key for key in keys}
    matched = tuple(sorted({derived[ref] for ref in wanted_refs if ref in derived}))
    unresolved = tuple(sorted({str(ref) for ref in wanted_refs if ref not in derived}))
    return matched, unresolved


# ---------------------------------------------------------------------------
# 离线导入 / 测试夹具用的写入（预警图本身一条都不写）
# ---------------------------------------------------------------------------


def insert_physical_snapshot(conn, *, snapshot_id: str, pool_id: str, warehouse_id: str,
                              namespace: str, source: str, evidence: str,
                              captured_at: datetime | None, scan_complete: bool,
                              scan_evidence: str | None, batch_id: str,
                              platform: str | None = None) -> None:
    """登记一次实物盘点（一批 = 一个 (池, 仓库, 批次, 单位) 的完整读数）。"""
    if source not in PHYSICAL_SOURCE_KINDS:
        raise ValueError("inventory_physical_source_unapproved")
    # 快照号 / 池 / 仓库都是主键的一部分：留空会让数据库报一个约束名，而不是在这里
    # 说清"哪一格没填"。入口先拒，也让上面那条契约用例能断言 ValueError。
    _require(snapshot_id, "inventory_snapshot_id_required")
    _require(pool_id, "inventory_snapshot_pool_required")
    _require(warehouse_id, "inventory_snapshot_warehouse_required")
    _require(namespace, "inventory_snapshot_namespace_required")
    _require(evidence, "inventory_snapshot_evidence_required")
    if captured_at is None:
        raise ValueError("inventory_snapshot_captured_at_required")
    _require(batch_id, "inventory_snapshot_batch_required")
    if scan_complete:
        _require(scan_evidence, "inventory_snapshot_scan_evidence_required")
    elif scan_evidence:
        raise ValueError("inventory_snapshot_scan_evidence_without_claim")
    conn.execute(
        """INSERT INTO bi.physical_stock_snapshots (
               snapshot_id, namespace, pool_id, warehouse_id, platform, source,
               evidence, captured_at, scan_complete, scan_evidence, batch_id
           ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
           ON CONFLICT (namespace, snapshot_id, pool_id, warehouse_id) DO NOTHING""",
        (snapshot_id, namespace, pool_id, warehouse_id, platform, source,
         str(evidence).strip(), captured_at, bool(scan_complete),
         str(scan_evidence).strip() if scan_complete else None, batch_id))


def insert_physical_snapshot_item(conn, *, snapshot_id: str, pool_id: str,
                                   warehouse_id: str, namespace: str, erp_sku_id: str,
                                   available_quantity: Any, unit: str,
                                   batch_id: str = "", inbound_quantity: Any = None,
                                   locked_quantity: Any = None,
                                   captured_at: datetime | None = None) -> None:
    """登记一次盘点里某个 SKU 的读数。

    三个数量字段（可用 / 在途 / 锁定）各自存原值：本函数**不做**「可用 = 总量 − 锁定」
    的推导——源字段已经是可用量时再减一次锁定量，凭空少掉一批能卖的货。
    """
    _require(erp_sku_id, "inventory_item_sku_required")
    _require(batch_id, "inventory_item_batch_required")
    if available_quantity is None:
        raise ValueError("inventory_item_declares_no_quantity")
    if normalize_quantity(available_quantity, unit) is None:
        raise ValueError("inventory_item_quantity_form")
    conn.execute(
        """INSERT INTO bi.physical_stock_items (
               snapshot_id, namespace, pool_id, warehouse_id, erp_sku_id,
               available_quantity, inbound_quantity, locked_quantity, unit, batch_id,
               captured_at
           ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
           ON CONFLICT (namespace, snapshot_id, pool_id, warehouse_id, erp_sku_id, unit,
                        batch_id)
           DO UPDATE SET available_quantity = EXCLUDED.available_quantity,
                         inbound_quantity = EXCLUDED.inbound_quantity,
                         locked_quantity = EXCLUDED.locked_quantity,
                         captured_at = EXCLUDED.captured_at""",
        (snapshot_id, namespace, pool_id, warehouse_id, erp_sku_id, available_quantity,
         inbound_quantity, locked_quantity, unit, batch_id,
         captured_at or datetime.now().astimezone()))


def insert_channel_snapshot(conn, *, snapshot_id: str, shop_id: str, platform: str,
                             namespace: str, source: str, evidence: str,
                             captured_at: datetime | None, scan_complete: bool,
                             scan_evidence: str | None) -> None:
    """登记一次渠道可售抓取（与实物盘点完全分开）。"""
    # 白名单里没有任何拼多多通道（2026-09-12 决定不接入），所以不需要为它单独写一条
    # 特例：特例只会让人以为白名单之外还有别的口子。
    if str(source) not in _CHANNEL_SOURCES:
        raise ValueError("inventory_channel_source_unapproved")
    _require(snapshot_id, "inventory_snapshot_id_required")
    _require(shop_id, "inventory_snapshot_shop_required")
    _require(namespace, "inventory_snapshot_namespace_required")
    _require(evidence, "inventory_snapshot_evidence_required")
    if captured_at is None:
        raise ValueError("inventory_snapshot_captured_at_required")
    if scan_complete:
        _require(scan_evidence, "inventory_snapshot_scan_evidence_required")
    elif scan_evidence:
        raise ValueError("inventory_snapshot_scan_evidence_without_claim")
    conn.execute(
        """INSERT INTO bi.channel_stock_snapshots (
               snapshot_id, namespace, shop_id, platform, source, evidence,
               captured_at, scan_complete, scan_evidence
           ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
           ON CONFLICT (namespace, shop_id, snapshot_id) DO NOTHING""",
        (snapshot_id, namespace, shop_id, str(platform or "").strip().lower(), source,
         str(evidence).strip(), captured_at, bool(scan_complete),
         str(scan_evidence).strip() if scan_complete else None))


def insert_channel_snapshot_item(conn, *, snapshot_id: str, shop_id: str,
                                  namespace: str, listing_id: str,
                                  platform_sku_id: str, erp_sku_id: str,
                                  sellable_quantity: Any, unit: str,
                                  captured_at: datetime | None = None) -> None:
    _require(listing_id, "inventory_channel_item_listing_required")
    _require(erp_sku_id, "inventory_item_sku_required")
    if sellable_quantity is None:
        raise ValueError("inventory_channel_item_declares_no_quantity")
    if normalize_quantity(sellable_quantity, unit) is None:
        raise ValueError("inventory_item_quantity_form")
    conn.execute(
        """INSERT INTO bi.channel_stock_items (
               snapshot_id, namespace, shop_id, listing_id, platform_sku_id,
               erp_sku_id, sellable_quantity, unit, captured_at
           ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
           ON CONFLICT (namespace, snapshot_id, shop_id, listing_id, platform_sku_id)
           DO UPDATE SET sellable_quantity = EXCLUDED.sellable_quantity,
                         erp_sku_id = EXCLUDED.erp_sku_id,
                         captured_at = EXCLUDED.captured_at""",
        (snapshot_id, namespace, shop_id, listing_id, platform_sku_id, erp_sku_id,
         sellable_quantity, unit, captured_at or datetime.now().astimezone()))


def insert_threshold_policy(conn, *, policy_id: str, policy_version: str, level: str,
                             erp_sku_id: str, quantity: str, unit: str,
                             pool_id: str | None = None, shop_id: str | None = None,
                             evidence: str, effective_at: date | None = None,
                             expires_at: date | None = None) -> None:
    """登记一版阈值策略。

    阈值是经营者的配置，本轮没有配置入口，所以只有离线导入与测试会写它。图上唯一
    能做的是"读到了就用"或"没读到就 unconfigured"，绝不自己设一个数。
    """
    # 证据是必填的：给它一个默认值，就等于让"这条策略是谁批的"这一格永远不为空，
    # 而 019 的 threshold_evidence CHECK 也就永远不会被触发。
    _require(evidence, "inventory_threshold_evidence_required")
    _require(erp_sku_id, "inventory_item_sku_required")
    if level not in THRESHOLD_LEVELS:
        raise ValueError("inventory_threshold_level_unsupported")
    if quantity_precision(unit) is None:
        raise ValueError("inventory_threshold_unit_unregistered")
    if normalize_quantity(quantity, unit) is None:
        raise ValueError("inventory_threshold_quantity_form")
    if level == "low_replenish" and shop_id:
        raise ValueError("inventory_threshold_level_scope_mismatch")
    if level == "low_quota" and not shop_id:
        raise ValueError("inventory_threshold_level_scope_mismatch")
    conn.execute(
        """INSERT INTO bi.inventory_threshold_policies (
               policy_id, policy_version, level, erp_sku_id, pool_id, shop_id,
               quantity, unit, effective_at, expires_at, evidence
           ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, coalesce(%s, current_date), %s, %s)
           ON CONFLICT (policy_id) DO NOTHING""",
        (policy_id, policy_version, level, erp_sku_id, pool_id, shop_id, quantity, unit,
         effective_at, expires_at, str(evidence or "").strip()))


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------

# 渠道侧允许的来源种类直接取注册表那份词表（不抄第二份）：两份清单迟早会有一边多一个
# 通道，而那时门禁与导入会给出两个不同的答案。拼多多通道不在其中（2026-09-12 决定不接入）。
_CHANNEL_SOURCES = CHANNEL_SOURCE_KINDS


def _require(value: object, code: str) -> None:
    if not str(value or "").strip():
        raise ValueError(code)


def _text(value: Any) -> str | None:
    return None if value is None else str(value)


def _check_budget(conn, deadline: float) -> None:
    """与经营图 / 价审图同一套预算。

    预算耗尽抛 `_BudgetExhausted`：绝不能让一次超时查询返回空行，空行会被读成
    "这些池都没有库存记录"，那是一份看起来像结论的假答案。
    """
    from bi_agent.metrics import _BudgetExhausted, _set_query_budget

    if not _set_query_budget(conn, deadline):
        raise _BudgetExhausted
