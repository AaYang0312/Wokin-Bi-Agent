"""商品 / SKU 解析：文本只用于找候选，合并只认标识映射（Task 6）。

三条红线：
1. 多候选返回 ambiguous，交回澄清；任选一个就是把规格猜掉。
2. 零候选是 unresolved，不能当成“销量为 0”。
3. 授权范围外的一条候选、一个主键都不许漏出去；文本本身不进运行事件。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Iterable, Literal, Sequence

from .channel_mapping import CHANNEL_MAPPING_VERSION, ChannelItem, channel_items_for
from .models import is_safe_display_name, ref_for_key

# 范围内候选商品全集的扫描上限。拿它做“能不能断定引用不存在”的门槛：扫不完时
# 结果必须是 fail closed 的 `scope_too_large`，而不是“这个引用不认识”。
MAX_SCOPE_CANDIDATES = 2000

ResolutionStatus = Literal["resolved", "ambiguous", "unresolved"]

# 匹配用「档案名或成交快照任一命中」，但**取用哪个名字**回到 `pick_display_name`
# 一处决定：SQL 不按列序猜优先级，避免在解析层长出第二套规则。
# strpos 而非 LIKE：LIKE 的通配字面量会被 psycopg 认成占位符前缀。
#
# 读 `reporting.v_product_candidate_lines` 而非 `bi.order_items` + `bi.products`：
# 聊天 API 以 `bi_app` 身份连接，而 005 把 `bi.products` 从该角色收回（档案采购成本
# 不得外泄）。解析器以前只在管理员 DSN 的用例里跑过，换到真实部署就会抛
# permission denied 而不是安全的 `unresolved`。视图列形与原查询逐项对应，
# 纳入条件也一致（成交行全量，不按 active 筛：没成交过的商品由映射视图补）。
_CANDIDATE_SQL = """
SELECT product_id,
       max(archive_name) AS archive_name,
       array_agg(DISTINCT nullif(product_name_snapshot, '')) AS snapshot_names,
       array_agg(DISTINCT nullif(sku_label_snapshot, '')) AS sku_labels,
       array_agg(DISTINCT shop_id) AS shop_ids
FROM reporting.v_product_candidate_lines
WHERE shop_id = ANY(%s)
  AND (strpos(coalesce(nullif(archive_name, ''), ''), %s) > 0
       OR strpos(coalesce(nullif(product_name_snapshot, ''), ''), %s) > 0)
GROUP BY product_id
ORDER BY product_id
"""

_REF_LOOKUP_SQL = """
SELECT product_id,
       max(archive_name) AS archive_name,
       array_agg(DISTINCT nullif(product_name_snapshot, '')) AS snapshot_names,
       array_agg(DISTINCT nullif(sku_label_snapshot, '')) AS sku_labels,
       array_agg(DISTINCT shop_id) AS shop_ids
FROM reporting.v_product_candidate_lines
WHERE shop_id = ANY(%s) AND product_id = %s
GROUP BY product_id
"""

# 范围内候选商品全集：引用是 (kind, 主键) 的哈希，不能反查，只能在本轮授权范围内
# 逐个派生后比对（与 `catalog/projection.py` 同源的做法，不把派生规则再抄进 SQL）。
# 多取一行就是截断证据：扫不完时不能把“没扫到”说成“这个引用不认识”。
_SCOPE_PRODUCT_IDS_SQL = """
SELECT DISTINCT product_id
FROM reporting.v_product_candidate_lines
WHERE shop_id = ANY(%s)
ORDER BY product_id
LIMIT %s
"""

# 引用存在性探针：只回答“是不是一个已知商品引用”，不回答它对应哪个主键。
_REF_EXISTS_SQL = """
SELECT 1 FROM reporting.v_product_refs WHERE ref = %s LIMIT 1
"""


@dataclass(frozen=True)
class Selector:
    """`ref` 与 `text` 二选一；两者都给或都不给都是选择器非法，不猜优先级。"""

    ref: str | None = None
    text: str | None = None
    sku_refs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if bool(self.ref) == bool(self.text):
            raise ValueError("selector_needs_exactly_one")


@dataclass(frozen=True)
class Candidate:
    # 候选带的是**授权范围内**出现过这个商品的店铺集合；范围外的店不在这里出现，
    # 也不在别处出现——不能拿“先取一个店”当形状，那会让越权检查看着像通过。
    shop_ids: tuple[str, ...]
    erp_product_id: str
    product_ref: str
    product_name: str | None
    name_source: Literal["archive", "trade_snapshot", "unresolved"]
    sku_label: str | None
    sku_refs: tuple[str, ...] = ()


@dataclass(frozen=True)
class ProductResolution:
    status: ResolutionStatus
    reason: str = ""
    product_ref: str | None = None
    erp_product_id: str | None = None
    sku_refs: tuple[str, ...] = ()
    candidates: tuple[Candidate, ...] = ()
    mapping_version: str | None = None
    catalog_version: int = 0


def text_selector(text: str, *, sku_refs: Iterable[str] = ()) -> Selector:
    return Selector(text=str(text).strip(), sku_refs=tuple(sku_refs))


def selected_selector(ref: str, *, sku_refs: Iterable[str] = ()) -> Selector:
    return Selector(ref=str(ref).strip(), sku_refs=tuple(sku_refs))


def _single(values: object) -> str | None:
    """多个不同取值时不选一个：名称/规格不一致就是“这条不能确定地说是什么”。"""
    items = [item for item in (values or []) if item is not None]
    return str(items[0]) if len(set(items)) == 1 else None


def _label(labels: object) -> str | None:
    """规格取用沿用 catalog 的规则：每一行都指同一个非空规格才展示。"""
    from .models import pick_sku_label

    values = [item for item in (labels or []) if item is not None]
    return pick_sku_label(values)


def _candidate(row) -> Candidate:
    """把一行聚合结果换成候选：名称与规格都沿用 catalog 的单点规则。"""
    product_id = str(row[0])
    archive_name = row[1]
    snapshot_name = _single(row[2])
    from .models import pick_display_name

    # 取用优先级只有一处实现：档案名 > 成交快照 > 未取得（不在这重排一遍规则）。
    name, source = pick_display_name(archive=archive_name, snapshot=snapshot_name)
    return Candidate(shop_ids=tuple(sorted(str(item) for item in (row[4] or []))),
                     erp_product_id=product_id,
                     product_ref=ref_for_key("product", product_id),
                     product_name=name, name_source=source,
                     sku_label=_label(row[3]))


def resolve_product(conn, *, selector: Selector, authorized_shop_ids: Iterable[str],
                    at: date) -> ProductResolution:
    """把 ref 或文本解析成授权范围内的一个 ERP 商品。

    `at` 是有效期时点：映射按段生效，解析也必须落在同一段上，否则“当前”会去用
    未来的映射。文本只在服务端用于找候选，不写进运行事件与提示词。
    """
    shops = sorted({str(item) for item in authorized_shop_ids})
    if not shops:
        return ProductResolution("unresolved", reason="no_authorized_scope")

    if selector.ref:
        return _resolve_by_ref(conn, ref=selector.ref, shops=shops, at=at)

    needle = (selector.text or "").strip()
    if not needle:
        return ProductResolution("unresolved", reason="empty_selector")
    rows = conn.execute(_CANDIDATE_SQL, (shops, needle, needle)).fetchall()
    candidates = tuple(_candidate(row) for row in rows)
    if not candidates:
        # 零候选是“没解析出来”，不是“这个商品没卖过”：后者要由成交事实回答。
        return ProductResolution("unresolved", reason="no_match")
    if len(candidates) > 1:
        # 同名异物（如“接头”的 6mm / 8mm）必须整批交回澄清；文本不足以决定合并。
        return ProductResolution("ambiguous", reason="multiple_products",
                                 candidates=candidates)
    return _resolved(conn, candidates[0], shops=shops, at=at)


def _resolve_by_ref(conn, *, ref: str, shops: Sequence[str],
                    at: date) -> ProductResolution:
    """按引用在**本轮授权范围**内定位 ERP 商品。

    引用是 (kind, 主键) 的哈希前缀，不能反推主键，所以在范围内逐个派生后比对；
    `bi.entity_refs` 不进 reporting 层，007 拒绝向应用身份暴露引用→主键映射。
    三种结果分得清：“这个引用不认识”、“引用认识但本轮看不到成交/档案行”、
    “范围内候选多到扫不完”（fail closed，不当成不认识）。
    """
    wanted = MAX_SCOPE_CANDIDATES + 1
    rows = conn.execute(_SCOPE_PRODUCT_IDS_SQL, (list(shops), wanted)).fetchall()
    if len(rows) >= wanted:
        # 扫不完就不能断“没这个引用”：宁可拒绝本次解析，也不把越权说成不存在。
        return ProductResolution("unresolved", reason="scope_too_large")
    for row in rows:
        product_id = str(row[0])
        if ref_for_key("product", product_id) != ref:
            continue
        return _resolved(conn, _candidate(conn.execute(
            _REF_LOOKUP_SQL, (list(shops), product_id)).fetchone()),
            shops=shops, at=at)
    if conn.execute(_REF_EXISTS_SQL, (ref,)).fetchone() is None:
        return ProductResolution("unresolved", reason="unknown_ref")
    # 引用存在但在本次授权范围里看不到成交/档案行：越权与“没有这个商品”必须分开。
    return ProductResolution("unresolved", reason="out_of_scope")


def _resolved(conn, candidate: Candidate, *, shops: Sequence[str],
              at: date) -> ProductResolution:
    items = expand_channel_items(conn, erp_product_id=candidate.erp_product_id,
                                 authorized_shop_ids=shops, at=at)
    mapping_version = CHANNEL_MAPPING_VERSION if items else None
    sku_refs = tuple(sorted({ref_for_key("sku", item.erp_sku_id)
                             for item in items if item.erp_sku_id}))
    return ProductResolution("resolved", product_ref=candidate.product_ref,
                             erp_product_id=candidate.erp_product_id,
                             sku_refs=sku_refs, candidates=(candidate,),
                             mapping_version=mapping_version)


def expand_channel_items(conn, *, erp_product_id: str,
                         authorized_shop_ids: Iterable[str],
                         at: date) -> tuple[ChannelItem, ...]:
    """某个 ERP 商品在授权范围内的渠道落点。

    全集来自映射表本身（含无成交的已映射 listing），**不从订单历史推导**：
    上架复核与库存预警都要能看到没卖过的链接。
    """
    return tuple(channel_items_for(conn, erp_product_id=erp_product_id,
                                   authorized_shop_ids=authorized_shop_ids, at=at))
