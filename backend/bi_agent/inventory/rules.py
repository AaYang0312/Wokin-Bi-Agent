"""库存预警的确定性规则、词表与来源门禁（运营工作流计划 Task 10）。

本模块只做三件事，而且只做纯计算：

1. **去重与求和**：实物库存按 `(库存池, 仓库, SKU, 批次, 单位)` 这一条身份去重后各算
   一次（spec §5.5）。同一身份出现两个不同数量不是"取一个"，是冲突；同一身份挂两种
   单位也不是"换算一下"，是没登记换算表就是不能相加。
2. **阈值边界**：`classify_inventory` 实现 `quantity <= threshold`，等于也算预警；
   缺阈值 `unconfigured`、缺数量 `unknown`、过期 `stale`、负数 `data_anomaly`。
3. **来源门禁**：哪个口径的库存算"已核验来源"。默认**为空**。

## 为什么两个口径的门禁要分开登记

spec §9 把 `physical_stock_snapshots` 的当前证据写成「历史报告仅验证部分 ERP SKU /
仓库样本」，把 `channel_stock_snapshots` 写成「渠道可售量需独立取证」。所以实物取证
成功**不代表**渠道可售可用：注册表按 `level` 分别存，缺一个就那一格只能 `unsupported`。
拼多多按 2026-09-12 决定不接入，注册表也不给它留位置（来源种类白名单里没有它）。

`UNIT_CONVERSION_REGISTRY_VERSION` 与 `piece/box/...` 精度表放在同一处：单位换算表
一旦登记，就是"能不能把两行加成一个数"的依据，版本必须进血缘。
"""

from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Literal, Mapping

# ---------------------------------------------------------------------------
# 版本标识：参与血缘与请求指纹。
# ---------------------------------------------------------------------------
INVENTORY_GRAPH_VERSION = "inventory_watch-graph/2026-09-14.1"
INVENTORY_RULE_VERSION = "inventory-rules/2026-09-14.1"
INVENTORY_METRIC_VERSION = "inventory-stock/2026-09-14.1"
INVENTORY_SOURCE_REGISTRY_VERSION = "inventory-sources/2026-09-14.1"
# 单位换算表：本轮**没有**任何已登记的换算，所以跨单位相加一律拒绝。
UNIT_CONVERSION_REGISTRY_VERSION = "unit-conversion/none-2026-09-14"
INVENTORY_TEMPLATE_ID = "inventory_watch"
INVENTORY_TEMPLATE_VERSION = "1"
INVENTORY_SCHEMA_VERSION = "019"

# 全商品盘点的展示上限：**只截展示**。扫描事实与期望全集必须先进 summary，
# 否则 Top N 会被读成"整个目录就这 N 个"（spec §5.5）。
MAX_DISPLAY_ITEMS = 20
# 期望项上限：超了必须拒绝出数，不能静默截取（与 listing roster 上限同一理由）。
MAX_EXPECTED_ITEMS = 2000
# 阈值数量上限：挡住把"一个月的销量"或一位录入错误当阈值用。
MAX_THRESHOLD_QUANTITY = 10 ** 7
# 单个数量可信上限：与 019 的 numeric(18,4) CHECK 同一口径（那里允许负值进入，由
# 判定层报 data_anomaly；这里只挡明显不可能是库存的位数）。
MAX_STOCK_QUANTITY = 10 ** 8

# ---------------------------------------------------------------------------
# 词表
# ---------------------------------------------------------------------------

# spec §5.5 的六个判定态。`unsupported` 不属于它：那是来源门禁，不是库存状态。
StockStatus = Literal["low", "normal", "unconfigured", "unknown", "stale",
                      "data_anomaly"]
STOCK_STATUSES: tuple[str, ...] = ("low", "normal", "unconfigured", "unknown", "stale",
                                   "data_anomaly")
# 门禁态与逐格状态在载荷里同列出现，所以把它们一起列进"行可取的状态"词表。
ROW_STATUSES: tuple[str, ...] = STOCK_STATUSES + ("unsupported",)
STATUS_LOW, STATUS_NORMAL = "low", "normal"
STATUS_UNCONFIGURED, STATUS_UNKNOWN = "unconfigured", "unknown"
STATUS_STALE, STATUS_ANOMALY = "stale", "data_anomaly"
STATUS_UNSUPPORTED = "unsupported"
# 只有这两类是"看过数量并按阈值判过"：其余都不许被算成已评估，也不许支撑 all_safe。
JUDGED_STATUSES: frozenset[str] = frozenset({STATUS_LOW, STATUS_NORMAL})
# 风险顺序：数据异常与缺货排最前。Top N 截断时这个顺序决定"被展示的是坏消息还是好消息"，
# 所以它属于词表而不是图上一个可以随意重排的排序细节。
STATUS_RISK_ORDER: tuple[str, ...] = (STATUS_ANOMALY, STATUS_LOW, STATUS_UNKNOWN,
                                      STATUS_STALE, STATUS_UNCONFIGURED,
                                      STATUS_UNSUPPORTED, STATUS_NORMAL)

InventoryLevel = Literal["physical_total", "shop_sellable"]
AUDIT_LEVELS: tuple[str, ...] = ("physical_total", "shop_sellable")
# 两种候选各自对应一个口径：混着给就是给错动作（spec §5.5 的例子）。
ThresholdLevel = Literal["low_replenish", "low_quota"]
THRESHOLD_LEVELS: tuple[str, ...] = ("low_replenish", "low_quota")
LEVEL_OF_THRESHOLD = {"low_replenish": "physical_total", "low_quota": "shop_sellable"}
# 阈值档位与库存口径必须成对出现：渠道可售那一格带补货阈值是问错了问题。
THRESHOLD_LEVEL_BY_LEVEL = {"physical_total": "low_replenish",
                            "shop_sellable": "low_quota"}

# 库存池与店铺的三种连接方式（计划 Task 10 第 2 条）。没有"未知即 shared"这一档：
# 把未声明的池当共享池，就等于替所有店把同一批库存重复数一遍。
ShopConnection = Literal["shared", "allocated", "independent"]
SHOP_CONNECTIONS: tuple[str, ...] = ("shared", "allocated", "independent")

# 获准的库存来源种类。注意**没有** `erp_suggested_price`，也没有任何拼多多通道。
PhysicalSourceKind = Literal["erp", "wms", "official_export", "manual_import"]
PHYSICAL_SOURCE_KINDS: tuple[str, ...] = ("erp", "wms", "official_export",
                                          "manual_import")
ChannelSourceKind = Literal["channel_api", "official_export", "manual_import"]
CHANNEL_SOURCE_KINDS: tuple[str, ...] = ("channel_api", "official_export",
                                         "manual_import")

# 单位精度表：本轮只登记已核验过的整数计数单位。**未登记单位不参与求和**，
# 也不被当成 0：套件/多件装的换算依据没有就是没有（spec §5.1）。
UNIT_PRECISIONS: Mapping[str, int] = {"piece": 0, "box": 0, "set": 0}
# 存储层允许出现的单位全集（与 019 的三处 unit CHECK 同一份）：`kit` 能存进来，所以
# 它也必须能出表——否则一个 kit 行就让整份预警发不出去，那是把一格的异常升级成全链失败。
STORAGE_UNITS: frozenset[str] = frozenset(UNIT_PRECISIONS) | frozenset({"kit"})
assert STORAGE_UNITS >= set(UNIT_PRECISIONS)
# 明确**没有**换算依据的单位：套件 / 多件装父项与组件不能相加（spec §5.1）。
# 它们不在 `UNIT_PRECISIONS` 里就已经不参与求和，这个名字存在的唯一理由是让"哪些
# 单位被有意留在外面"能被列出来，而不是让人去猜一个集合是不是漏写了。
PRICELESS_UNITS: frozenset[str] = frozenset({"kit"})
assert not (PRICELESS_UNITS & set(UNIT_PRECISIONS))
assert PRICELESS_UNITS <= STORAGE_UNITS
UNIT_CODE_RE = re.compile(r"^[a-z][a-z0-9_]{0,15}$")
_DECIMAL_RE = re.compile(r"^-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?$")
CNY_PRECISION = 0          # 件数是整数；与金额精度同源命名，避免两处各写一套


class InventoryConflict(Exception):
    """同一身份出现两个数量，或同一身份挂了两种单位。

    `reason` 区分两类冲突：`quantity_conflict` 要回去查哪一批录错了，
    `unit_conflict` 要的是换算表登记，两种下一步完全不同。
    """

    def __init__(self, reason: str, keys) -> None:  # noqa: ANN001
        super().__init__(reason)
        self.reason = reason
        self.keys = tuple(keys)


# ---------------------------------------------------------------------------
# 限制码与公开披露文本
# ---------------------------------------------------------------------------

# 一句文本对应一个码；词表由这张表派生（`LISTING` 同一做派），两边不可能漂移。
INVENTORY_CODE_BY_TEXT: Mapping[str, str] = {
    "库存来源尚未取证，本次两级预警不能判定": "inventory_source_unverified",
    "实物库存来源尚未取证，本次不能出补货候选": "inventory_physical_source_unverified",
    "渠道可售库存来源尚未取证，本次不能出配额候选": "inventory_channel_source_unverified",
    "本轮没有该库存池的实物快照，未判定项保持未知": "inventory_snapshot_missing",
    "本轮没有该店铺的渠道可售快照，未判定项保持未知": "inventory_channel_snapshot_missing",
    "快照已超过该来源的时效策略，按过期披露，不判安全": "inventory_snapshot_stale",
    "扫描分页未取尽，不能声称看完全目录": "inventory_scan_incomplete",
    "同一库存身份出现两个不同数量，按数据异常处理，不任选其一": "inventory_data_anomaly",
    "同一库存身份挂了两种单位且没有已登记的换算表，不合成一个总数": "inventory_unit_conflict",
    "负库存按数据异常处理，不生成补货候选": "inventory_negative_quantity",
    "缺少版本化阈值配置，本轮不作安全判定": "inventory_threshold_unconfigured",
    "本轮阈值与已配置策略同时给出，请先确认按哪一套": "inventory_threshold_conflict",
    "期望项超出可扫描上限，已拒绝出数以避免静默截断": "inventory_scan_truncated",
    "展示已按风险截断，未展示的高风险项仍在预警集合里": "inventory_display_truncated",
    "部分库存池不在本轮授权范围内，未计入总量": "inventory_pool_not_authorized",
    "授权范围内没有可检查的店铺": "inventory_scope_empty",
    "本轮授权范围内没有解析出该 SKU，不能当成 0 库存": "inventory_sku_unresolved",
    "全商品集合本轮只能由已授权快照批次派生，未覆盖无库存记录的 SKU":
        "inventory_universe_from_snapshot",
    "存在低于阈值的实物库存，给出补货候选": "inventory_replenish_candidate",
    "存在渠道配额缺口，给出店铺配额调整候选": "inventory_quota_candidate",
    "期望项未全部判定，不能声称全部安全": "inventory_audit_incomplete",
}

INVENTORY_PUBLIC_LIMITATIONS: frozenset[str] = frozenset(INVENTORY_CODE_BY_TEXT)
INVENTORY_LIMITATION_CODES: frozenset[str] = frozenset(INVENTORY_CODE_BY_TEXT.values())


def inventory_limitation_codes(limitations) -> list[str]:
    """披露文本 → 限制码：未登记的一句一律不进状态（宁可少记，不可自创码）。"""
    codes: list[str] = []
    for text in limitations:
        code = INVENTORY_CODE_BY_TEXT.get(str(text))
        if code is not None and code not in codes:
            codes.append(code)
    return codes


# ---------------------------------------------------------------------------
# 结果列白名单（生产方持有，`runtime.models` 只引用）
# ---------------------------------------------------------------------------

INVENTORY_COLUMN_KINDS: Mapping[str, str] = {
    "shop_ref": "ref",
    "sku_ref": "ref",
    "pool_ref": "pool_ref",
    "warehouse_ref": "warehouse_ref",
    "level": "label",
    "quantity": "quantity",
    "channel_quantity": "quantity",
    "threshold": "quantity",
    "unit": "unit",
    "batch_count": "int",
    "inventory_status": "label",
    "action": "label",
    "snapshot_at": "datetime",
}
INVENTORY_RESULT_COLUMNS: frozenset[str] = frozenset(INVENTORY_COLUMN_KINDS)
INVENTORY_COLUMN_CATEGORIES: frozenset[str] = frozenset(
    {"quantity", "int", "label", "ref", "pool_ref", "warehouse_ref", "unit",
     "datetime"})
assert set(INVENTORY_COLUMN_KINDS.values()) <= INVENTORY_COLUMN_CATEGORIES, \
    "未声明的列类别必须先在 runtime.models 里有一个校验函数"
INVENTORY_LABEL_RESULT_VALUES: Mapping[str, frozenset[str]] = {
    "inventory_status": frozenset(ROW_STATUSES),
    "level": frozenset(AUDIT_LEVELS),
    "action": frozenset({"", "replenish", "quota_adjust"}),
}
INVENTORY_QUANTITY_RESULT_COLUMNS: frozenset[str] = frozenset(
    key for key, kind in INVENTORY_COLUMN_KINDS.items() if kind == "quantity")
INVENTORY_INT_RESULT_COLUMNS: frozenset[str] = frozenset(
    key for key, kind in INVENTORY_COLUMN_KINDS.items() if kind == "int")
INVENTORY_REF_RESULT_COLUMNS: frozenset[str] = frozenset(
    key for key, kind in INVENTORY_COLUMN_KINDS.items() if kind == "ref")
INVENTORY_POOL_REF_RESULT_COLUMNS: frozenset[str] = frozenset(
    key for key, kind in INVENTORY_COLUMN_KINDS.items() if kind == "pool_ref")
INVENTORY_WAREHOUSE_REF_RESULT_COLUMNS: frozenset[str] = frozenset(
    key for key, kind in INVENTORY_COLUMN_KINDS.items() if kind == "warehouse_ref")
INVENTORY_UNIT_RESULT_COLUMNS: frozenset[str] = frozenset(
    key for key, kind in INVENTORY_COLUMN_KINDS.items() if kind == "unit")
INVENTORY_DATETIME_RESULT_COLUMNS: frozenset[str] = frozenset(
    key for key, kind in INVENTORY_COLUMN_KINDS.items() if kind == "datetime")
# 每个类别各一个集合，`runtime.models` 直接按它派发：列归类不由"剩下的就是数值"决定。
_covered = (INVENTORY_QUANTITY_RESULT_COLUMNS | INVENTORY_INT_RESULT_COLUMNS
            | INVENTORY_REF_RESULT_COLUMNS | INVENTORY_POOL_REF_RESULT_COLUMNS
            | INVENTORY_WAREHOUSE_REF_RESULT_COLUMNS | INVENTORY_UNIT_RESULT_COLUMNS
            | INVENTORY_DATETIME_RESULT_COLUMNS
            | frozenset(INVENTORY_LABEL_RESULT_VALUES) | {"snapshot_at"})
assert _covered >= INVENTORY_RESULT_COLUMNS, "每一列都必须有一个校验类别可归"

# 池 / 仓库句柄：`(账号范围, 池号)` 与 `(账号范围, 仓库号)` 的单向摘要。
# 用独立前缀而不是 `ent-`：它们不是目录实体，混进同一命名空间只会被展示层当成
# "名称未取得"藏起来，而句柄本身是要给经营者对着后台核的。
POOL_REF_RE = re.compile(r"^pl-[0-9a-f]{12}$")
WAREHOUSE_REF_RE = re.compile(r"^wh-[0-9a-f]{12}$")


# ---------------------------------------------------------------------------
# 数量规则
# ---------------------------------------------------------------------------


def quantity_precision(unit: object) -> int | None:
    """已核验的单位小数位；未登记单位返回 None（不参与求和，也不参与比较）。"""
    return UNIT_PRECISIONS.get(str(unit or "").strip().lower())


def normalize_quantity(value: object, unit: object) -> Decimal | None:
    """把数量文本读成精确值；格式不正、单位未登记或与单位精度不符都返回 None。

    只接受纯十进制文本（与运行契约 `_DECIMAL_RE` 同一形状）。**不做任何舍入**：
    整数单位上的 `0.5` 说明来源没按口径给数，把它舍成 0 或 1 都是在编一个数。
    """
    if value is None:
        return None
    text = str(value).strip()
    if not _DECIMAL_RE.fullmatch(text):
        return None
    precision = quantity_precision(unit)
    if precision is None:
        return None
    try:
        parsed = Decimal(text)
    except InvalidOperation:
        return None
    # 精度=0 的单位（件 / 箱）只收**整数值**，但收尾零：`numeric(18,4)` 读回来的
    # 本来就是 `100.0000`。区分"尾零"与"真小数"要看值本身，看写法会把正常读数
    # 全部判成坏数据，总量就永远出不来。
    if precision == 0 and parsed != parsed.to_integral_value():
        return None
    if parsed.copy_abs() > MAX_STOCK_QUANTITY:
        # 超出可信上限的数量说明来源给错了单位或录错了，不配参与任何求和。
        return None
    return parsed


def amount_sum(values, unit: object) -> str:  # noqa: ANN001
    """同单位求和；任何一个值不可解析就报错，不当它不存在。

    少加一行会得到一个**偏小**的总数，而那正是会被拿去说"库存还够"的那类错。
    """
    precision = quantity_precision(unit)
    if precision is None:
        raise ValueError("inventory_unit_unregistered")
    total = Decimal(0)
    for value in values:
        parsed = normalize_quantity(value, unit)
        if parsed is None:
            raise ValueError("inventory_quantity_unparseable")
        total += parsed
    return str(total.quantize(Decimal(1).scaleb(-precision)))


def unit_comparable(left: object, right: object) -> bool:
    """两个单位是否可以直接相加：必须是同一个**已登记**单位。

    返回 False 不代表"不能相加所以要报错"——不同单位本来就是不同的键，各自成行；
    只有同一身份上出现两种单位才由 `sum_unique_physical_stock` 判成冲突。
    """
    left_unit = str(left or "").strip().lower()
    right_unit = str(right or "").strip().lower()
    return bool(left_unit) and left_unit == right_unit \
        and quantity_precision(left_unit) is not None


def physical_identity(row: Mapping[str, object]) -> str:
    """实物行的去重身份：`(企业/账号范围, 池, 仓库, SKU, 批次, 单位)`。

    身份里**没有店铺**：spec §5.5 的三店共享一池例子说明，只要店铺进了键，
    同一批货就会被数三遍。店铺关系是连接方式（shared / allocated / independent），
    不是事实的身份。
    """
    parts = [str(row.get(key) or "") for key in
             ("namespace", "pool_ref", "warehouse_ref", "sku_ref", "batch_id", "unit")]
    return "\x1f".join(parts)


def sum_unique_physical_stock(rows) -> str:  # noqa: ANN001
    """已授权实物行 → 一个去重后的总量。

    三条规则（计划 Task 10 契约）：

    - 同一身份重复出现且数量一致 → 只算一次（三店共享一池就是这一条）；
    - 同一身份数量不一致 → `InventoryConflict`，不取第一个、不取平均、不取最大；
    - 同一身份单位不一致 → 冲突（缺换算表时相加就是编数）。

    不同批次是**两笔事实**，合计都算进去，但 `batch_count` 要能说出它是几批拼出来的。
    """
    seen: dict[str, tuple[str, str]] = {}
    unit_conflicts: list[str] = []
    quantity_conflicts: list[str] = []
    totals: dict[str, list[str]] = {}
    for row in rows:
        identity = physical_identity(row)
        base = "\x1f".join(identity.split("\x1f")[:5])
        unit = str(row.get("unit") or "")
        quantity = str(row.get("available_quantity") or "")
        previous = seen.get(base)
        if previous is not None:
            previous_unit, previous_quantity = previous
            if not unit_comparable(previous_unit, unit):
                unit_conflicts.append(base)
                continue
            if normalize_quantity(previous_quantity, previous_unit) != \
                    normalize_quantity(quantity, unit):
                quantity_conflicts.append(base)
                continue
        # 同一身份同一数量：只登记一次，按 (base, unit) 归到该单位的小计里
        key = f"{base}\x1f{unit}"
        if key not in totals:
            totals[key] = [quantity]
            seen[base] = (unit, quantity)
    if unit_conflicts:
        raise InventoryConflict("unit_conflict", sorted(set(unit_conflicts)))
    if quantity_conflicts:
        raise InventoryConflict("quantity_conflict", sorted(set(quantity_conflicts)))
    per_unit: dict[str, list[str]] = {}
    for key, values in totals.items():
        unit = key.split("\x1f")[-1]
        per_unit.setdefault(unit, []).extend(values)
    if len(per_unit) > 1:
        # 每个身份都只有一种单位，但不同身份之间单位不同：那也不能加成一个总数。
        raise InventoryConflict("unit_conflict", sorted(per_unit))
    if not per_unit:
        return "0"
    (unit, values), = per_unit.items()
    return amount_sum(values, unit)


def classify_inventory(quantity: str | None, threshold: str | None, *,
                       fresh: bool, unit: str = "piece") -> str:
    """一格库存对一份阈值的判定（spec §5.5 的 `quantity <= threshold`）。

    顺序本身就是结论的一部分，不可调换：

    1. `fresh=False` → `stale`：一个过期数字既不能判低也不能判安全，它要先去重扫；
    2. 数量本身不成立（负数或不可解析的负值形态）→ `data_anomaly`：先说源数据坏了；
    3. 缺阈值 → `unconfigured`：缺的是经营者的那一侧，是可以立刻补上的下一步；
    4. 缺数量 → `unknown`；
    5. 其余才比大小。
    """
    if not fresh:
        return STATUS_STALE
    parsed = normalize_quantity(quantity, unit)
    if quantity is not None and str(quantity).strip().startswith("-"):
        # 负库存是"这一条源数据不成立"，比"没配阈值"更该先说；单位不认识的负数同样。
        return STATUS_ANOMALY
    if parsed is None and quantity is not None:
        return STATUS_UNKNOWN
    if threshold is None:
        return STATUS_UNCONFIGURED
    if parsed is None:
        return STATUS_UNKNOWN
    parsed_threshold = normalize_quantity(threshold, unit)
    if parsed_threshold is None:
        return STATUS_UNCONFIGURED
    if parsed < 0:
        return STATUS_ANOMALY
    return STATUS_LOW if parsed <= parsed_threshold else STATUS_NORMAL


def freshness_ok(*, captured_at: datetime | None, now: datetime,
                 max_age_seconds: int) -> bool:
    """快照是否仍在该来源的时效策略内。

    缺抓取时间就是"无从判断新鲜"，按不新鲜处理（spec §5.5：过期快照返回 stale）。
    无时区的时间同样按不新鲜处理：一个被读成 UTC、另一个被读成本地时间，
    差的就是八小时，而那正好能把过期说成新鲜。
    """
    if captured_at is None or max_age_seconds <= 0:
        return False
    if captured_at.tzinfo is None or now.tzinfo is None:
        return False
    age = (now - captured_at).total_seconds()
    return age <= max_age_seconds + 1e-6


def quantity_text(value: object, unit: object) -> str | None:
    """数量出表：按单位精度保留，不做四舍五入；不可解析返回 None。"""
    parsed = normalize_quantity(value, unit)
    if parsed is None:
        return None
    precision = quantity_precision(unit) or 0
    stripped = parsed.normalize()
    exponent = stripped.as_tuple().exponent
    fraction = -exponent if isinstance(exponent, int) and exponent < 0 else 0
    scale = max(precision, fraction)
    return str(stripped.quantize(Decimal(1).scaleb(-scale)))


def beijing_iso(value: datetime | None) -> str | None:
    """快照时点出表：统一换成北京时区 ISO 文本（与 `data_as_of` 同一渲染）。"""
    if value is None:
        return None
    from zoneinfo import ZoneInfo

    return value.astimezone(ZoneInfo("Asia/Shanghai")).isoformat()


# ---------------------------------------------------------------------------
# 句柄
# ---------------------------------------------------------------------------


def _handle(prefix: str, namespace: str, natural_key: str) -> str:
    material = "\x1f".join((str(namespace or ""), str(natural_key or "")))
    return f"{prefix}-" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:12]


def pool_handle(namespace: str, pool_id: str) -> str:
    """`(账号范围, 池号)` → 稳定句柄；另一账号里的相同池号不是同一个池。"""
    return _handle("pl", namespace, pool_id)


def warehouse_handle(namespace: str, warehouse_id: str) -> str:
    return _handle("wh", namespace, warehouse_id)


def shop_connection_kind_of(value: object) -> str:
    """池与店的连接方式：三种都必须在快照里被显式声明过。"""
    kind = str(value or "").strip().lower()
    if kind not in SHOP_CONNECTIONS:
        raise ValueError("inventory_connection_kind_unsupported")
    return kind


# ---------------------------------------------------------------------------
# 来源门禁
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class InventorySourceRegistration:
    """一个库存口径的已核验来源：证据、时效与通道。

    `level` 只能是两个口径之一：一次取证只回答"这个口径能不能信"。
    """

    level: str
    channel: str
    evidence: str
    max_age_seconds: int

    def __post_init__(self) -> None:
        level = str(self.level or "").strip().lower()
        if level not in AUDIT_LEVELS:
            raise ValueError("inventory_source_level_required")
        allowed = (PHYSICAL_SOURCE_KINDS if level == "physical_total"
                   else CHANNEL_SOURCE_KINDS)
        if str(self.channel or "").strip().lower() not in allowed:
            # 未登记的通道（含任何拼多多通道、ERP 建议价）不能成为已核验来源。
            raise ValueError("inventory_source_channel_unapproved")
        if not str(self.evidence or "").strip():
            raise ValueError("inventory_source_evidence_required")
        try:
            seconds = int(self.max_age_seconds)
        except (TypeError, ValueError):
            raise ValueError("inventory_source_freshness_policy_required") from None
        if seconds <= 0 or math.isnan(seconds):
            raise ValueError("inventory_source_freshness_policy_required")
        object.__setattr__(self, "level", level)
        object.__setattr__(self, "channel", str(self.channel).strip().lower())


_REGISTRY: dict[str, InventorySourceRegistration] = {}


def register_inventory_source(entry: InventorySourceRegistration) -> None:
    """登记一条已核验来源（服务端专用：不暴露给模型入参，也不读配置文件）。"""
    _REGISTRY[str(entry.level)] = entry


def reset_inventory_sources() -> None:
    """清空注册表：测试夹具与真实部署都从"一条都没有"起步。"""
    _REGISTRY.clear()


def verified_inventory_source(level: object) -> InventorySourceRegistration | None:
    """这个口径是否已有已核验来源；没有登记就没有，也不从另一个口径推。"""
    return _REGISTRY.get(str(level or "").strip().lower())


def verified_inventory_sources() -> Mapping[str, InventorySourceRegistration]:
    return dict(_REGISTRY)


__all__ = [
    "AUDIT_LEVELS", "CHANNEL_SOURCE_KINDS", "INVENTORY_CODE_BY_TEXT",
    "INVENTORY_COLUMN_CATEGORIES", "INVENTORY_COLUMN_KINDS",
    "INVENTORY_DATETIME_RESULT_COLUMNS", "INVENTORY_GRAPH_VERSION",
    "INVENTORY_INT_RESULT_COLUMNS", "INVENTORY_LABEL_RESULT_VALUES",
    "INVENTORY_LIMITATION_CODES", "INVENTORY_METRIC_VERSION",
    "INVENTORY_POOL_REF_RESULT_COLUMNS", "INVENTORY_PUBLIC_LIMITATIONS",
    "INVENTORY_QUANTITY_RESULT_COLUMNS", "INVENTORY_REF_RESULT_COLUMNS",
    "INVENTORY_RESULT_COLUMNS", "INVENTORY_RULE_VERSION",
    "INVENTORY_SCHEMA_VERSION", "INVENTORY_SOURCE_REGISTRY_VERSION",
    "INVENTORY_TEMPLATE_ID", "INVENTORY_TEMPLATE_VERSION",
    "INVENTORY_UNIT_RESULT_COLUMNS", "INVENTORY_WAREHOUSE_REF_RESULT_COLUMNS",
    "JUDGED_STATUSES", "LEVEL_OF_THRESHOLD", "MAX_DISPLAY_ITEMS",
    "MAX_EXPECTED_ITEMS", "MAX_STOCK_QUANTITY", "MAX_THRESHOLD_QUANTITY", "PHYSICAL_SOURCE_KINDS",
    "POOL_REF_RE", "PRICELESS_UNITS", "SHOP_CONNECTIONS", "STATUS_ANOMALY",
    "STATUS_LOW", "STATUS_NORMAL", "STATUS_STALE", "STATUS_UNCONFIGURED",
    "STATUS_UNKNOWN", "STATUS_UNSUPPORTED", "STOCK_STATUSES",
    "THRESHOLD_LEVELS", "UNIT_CONVERSION_REGISTRY_VERSION", "UNIT_PRECISIONS",
    "WAREHOUSE_REF_RE", "InventoryConflict", "InventorySourceRegistration",
    "ROW_STATUSES", "amount_sum", "beijing_iso", "classify_inventory",
    "freshness_ok", "inventory_limitation_codes", "normalize_quantity",
    "physical_identity", "pool_handle", "quantity_precision", "quantity_text",
    "register_inventory_source", "reset_inventory_sources",
    "shop_connection_kind_of", "sum_unique_physical_stock", "unit_comparable",
    "verified_inventory_source", "verified_inventory_sources",
]
