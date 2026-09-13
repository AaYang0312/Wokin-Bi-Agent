"""上架复核的确定性规则、词表与来源门禁（运营工作流计划 Task 9）。

本模块只做三件事，而且只做纯计算：

1. **金额比较**：`compare_price` 只在币种、规格、时点、完整性都已经被外层图判过
   之后才调用（计划 Task 9 契约）。它精确比较，不引入任何容差 —— 目标价是用户
   自己给的数，"差一分钱也算对"这种判断没有依据（spec §4）。
2. **词表**：九个判定状态、两种价格口径、三种获准来源、限制码与披露文本。所有
   面向模型与展示层的字符串都从这一处出，不给自由文本留通道。
3. **来源门禁**：哪个平台的渠道在售价算"已核验"。默认**为空**。

## 为什么来源注册表默认是空的

spec §9 把 `listing_snapshots` 的当前证据写成「尚无已验证渠道在售价来源；ERP 档案
priceOutput 不能替代」；Task 6 的交付记录也写明本轮没有任何 `channel_api` 生产者。
所以真实部署里本工具只能报 `unsupported`，不能声称线上全店复核可用。

门禁放在代码里而不是入参里，理由很直接：能被模型或用户传进来的开关不叫门禁。
开通一个平台必须在这里登记一条**带证据**的注册项（探针编号 / 对账单号），于是它
必然带着证据进代码评审，也必然要重新过 `tests/test_listing_audit.py` 的行为用例。
拼多多按 2026-09-12 的决定不接入，注册表里也不给它留位置。
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
# 版本标识：参与血缘与请求指纹，版本一变旧结果就不是同一个问题的答案。
# ---------------------------------------------------------------------------
LISTING_GRAPH_VERSION = "listing_price_audit-graph/2026-09-13.1"
LISTING_RULE_VERSION = "listing-rules/2026-09-13.1"
LISTING_METRIC_VERSION = "listing-price/2026-09-13.1"
LISTING_SOURCE_REGISTRY_VERSION = "listing-sources/2026-09-13.1"
LISTING_TEMPLATE_ID = "listing_price_audit"
LISTING_TEMPLATE_VERSION = "1"
LISTING_SCHEMA_VERSION = "018"
# 本轮一个已确认落点都没读到时的映射版本：它参与指纹，所以不能与任何真实映射版本
# 同名，也不能是空串（空串会被读成"没算过"，而不是"算过且没有"）。
LISTING_NO_MAPPING = "channel-map/none"

# roster 上限：全商品盘点必须"扫完再说扫不完"，不能先取 Top N 再报复核完成。
MAX_ROSTER_ITEMS = 500

# ---------------------------------------------------------------------------
# 词表
# ---------------------------------------------------------------------------

# spec §5.4 的九个状态。外层图负责 not_listed / not_on_sale / stale 这类需要
# 快照证据的判断，`compare_price` 只负责 match / mismatch / missing_standard / unknown。
AuditStatus = Literal["match", "mismatch", "not_listed", "not_on_sale",
                      "missing_standard", "unmapped", "stale", "unsupported", "unknown"]
AUDIT_STATUSES: tuple[str, ...] = (
    "match", "mismatch", "not_listed", "not_on_sale", "missing_standard", "unmapped",
    "unsupported", "stale", "unknown")
# 只有这两类算"本轮实际比过价"：其余状态都是证据不足，不进 evaluated_items 分子。
JUDGED_STATUSES: frozenset[str] = frozenset({"match", "mismatch"})
# 「这一格还没定下来」：下一步是去补证据。`not_listed` 与 `not_on_sale` **不在**
# 这一类里：它们是关于货架的确定结论（全量枚举证明它不在架 / 它当前不在售），只是
# 不是"价格一致"。把确定结论归入"没定下来"，一份完整的"这家店确实没上"的报告就会被
# 说成"证据不足"，那是两种完全不同的下一步。
UNDECIDED_STATUSES: frozenset[str] = frozenset(
    {"unsupported", "unknown", "unmapped", "stale", "missing_standard"})
assert not (UNDECIDED_STATUSES & JUDGED_STATUSES)
assert set(UNDECIDED_STATUSES) | JUDGED_STATUSES | {"not_listed", "not_on_sale"}     == set(AUDIT_STATUSES)

# 价格口径（spec §4）。会员价 / 券后价不在这里：它们不能用无条件标价替代。
PriceBasis = Literal["list_price", "campaign_price"]
PRICE_BASES: tuple[str, ...] = ("list_price", "campaign_price")

# 获准的渠道在售价来源种类。注意**没有** `erp_suggested_price`：ERP 档案建议价
# 不是渠道在售价（spec §9），它连候选都不算。
ListingSourceKind = Literal["channel_api", "official_export", "manual_import"]
LISTING_SOURCE_KINDS: tuple[str, ...] = ("channel_api", "official_export", "manual_import")

# 目标价规则种类（spec §4）。没有"按店铺统一价"这一档：一家店所有 SKU 同价
# 这种说法本身就没被用户明确表达过，不预备一个没人能正确使用的档位。
AppliesTo = Literal["all_selected", "sku", "shop_sku"]
APPLIES_TO_VALUES: tuple[str, ...] = ("all_selected", "sku", "shop_sku")

# 已核验的货币精度：只有拿到精度证据的币种才允许被规范化。
# 「按已核验的货币精度规范化」（spec §4）意味着未登记币种**不许**凭常识量化。
CNY_PRECISION = 2
CURRENCY_PRECISIONS: Mapping[str, int] = {"CNY": CNY_PRECISION}
_DECIMAL_RE = re.compile(r"^-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?$")

# ---------------------------------------------------------------------------
# 限制码与公开披露文本
# ---------------------------------------------------------------------------

# 一句文本对应一个码：披露文本与归因码同源，就不会出现"这句话是哪个码"的第二次判断。
# 词表就从这张表里派生（见下方 `LISTING_LIMITATION_CODES`）：手拄一份码列表，
# 迟早会写出一个"有文本、没码"或"有码、没文本"的说法，而两种都会以契约违规的形式
# 在运行现场爆掉。`tests/test_listing_audit.py` 拿运行契约的词表再比一次。
LISTING_CODE_BY_TEXT: Mapping[str, str] = {
    "渠道在售价来源尚未取证，本次上架复核不能判定": "listing_source_unverified",
    "本轮没有该店铺的渠道在售快照，未判定项保持未知": "listing_snapshot_missing",
    "快照已超过该来源的时效策略，按过期披露，不判正确": "listing_snapshot_stale",
    "缺少店铺全量上架枚举证据，不能判定未上架": "listing_enumeration_unproven",
    "快照币种与本轮目标价币种不一致，不能比较": "listing_currency_incomparable",
    "快照未声明活动价，按请求的活动价口径无法判定": "listing_price_basis_undeclared",
    "部分期望项没有本轮目标价，不判通过": "listing_expectation_missing",
    "目标价规则相互冲突，请先确认每个规格的目标价": "listing_expectation_conflict",
    "该 SKU 没有已确认的渠道落点映射，未判定": "listing_sku_unmapped",
    "该商品命中多个候选，请指定要复核哪一个": "listing_product_ambiguous",
    # 上限是常量，所以这一句也是固定文本：不把数字拼进披露，就不需要一条模式去兼容它。
    "期望复核项超过上限，已拒绝出数以避免静默截断；请缩小店铺或规格范围":
        "listing_roster_truncated",
    "存在目标价与实际价不一致的项，这是业务发现，不重试到匹配为止":
        "listing_price_mismatch_found",
    "存在经完整上架枚举确认的未上架项": "listing_not_listed_found",
    "存在当前不在售的链接": "listing_not_on_sale_found",
    "期望项未全部判定，不能声称全部正确": "listing_audit_incomplete",
    "授权范围内没有可复核的店铺": "listing_scope_empty",
    "本轮授权范围内没有解析出该商品，不能当成没有上架项": "listing_scope_empty",
}

LISTING_PUBLIC_LIMITATIONS: frozenset[str] = frozenset(LISTING_CODE_BY_TEXT)
# 码表从文本表派生：两者同源于同一处定义，就不可能一个里有“缺依据”而另一个里没它。
LISTING_LIMITATION_CODES: frozenset[str] = frozenset(LISTING_CODE_BY_TEXT.values())


def listing_limitation_codes(limitations) -> list[str]:
    """披露文本 → 限制码：未登记的一句一律不进状态（宁可少记，不可自创码）。"""
    codes: list[str] = []
    for text in limitations:
        code = LISTING_CODE_BY_TEXT.get(str(text))
        if code is not None and code not in codes:
            codes.append(code)
    return codes


# ---------------------------------------------------------------------------
# 结果列白名单（生产方持有，`runtime.models` 只引用）
# ---------------------------------------------------------------------------

# 列的校验类别由这一处声明；`runtime.models` 按类别派发到具体校验函数。
# 下面三条导入期断言是「新增列必须先声明类型」能成立的全部理由：不认识的类别、或某一列
# 没有任何校验函数可归，都在 import 时报——而不是让那一列落到数值兜底里，把任意文本当金额收。
LISTING_COLUMN_KINDS: Mapping[str, str] = {
    "shop_ref": "ref",
    "sku_ref": "ref",
    "listing_ref": "listing_ref",
    "expected_amount": "decimal",
    "actual_amount": "decimal",
    "amount_difference": "decimal",
    "audit_status": "label",
    "price_basis": "label",
    "snapshot_at": "datetime",
}
LISTING_RESULT_COLUMNS: frozenset[str] = frozenset(LISTING_COLUMN_KINDS)
LISTING_COLUMN_CATEGORIES: frozenset[str] = frozenset(
    {"decimal", "int", "label", "ref", "listing_ref", "datetime"})
assert set(LISTING_COLUMN_KINDS.values()) <= LISTING_COLUMN_CATEGORIES,     "未声明的列类别必须先在 runtime.models 里有一个校验函数"
LISTING_LABEL_RESULT_VALUES: Mapping[str, frozenset[str]] = {
    "audit_status": frozenset(AUDIT_STATUSES),
    "price_basis": frozenset(PRICE_BASES),
}
# 一个类别一个集合，`runtime.models` 直接按它派发：列归类不再由"剩下的就是数值"那种
# 兼容推导决定——那种写法下新登记一个不认识的类别，它会默默落进数值兼容。
LISTING_NUMERIC_RESULT_COLUMNS: frozenset[str] = frozenset(
    key for key, kind in LISTING_COLUMN_KINDS.items() if kind in ("decimal", "int"))
LISTING_REF_RESULT_COLUMNS: frozenset[str] = frozenset(
    key for key, kind in LISTING_COLUMN_KINDS.items() if kind == "ref")
LISTING_LISTING_REF_RESULT_COLUMNS: frozenset[str] = frozenset(
    key for key, kind in LISTING_COLUMN_KINDS.items() if kind == "listing_ref")
LISTING_DATETIME_RESULT_COLUMNS: frozenset[str] = frozenset(
    key for key, kind in LISTING_COLUMN_KINDS.items() if kind == "datetime")
assert (LISTING_NUMERIC_RESULT_COLUMNS | LISTING_REF_RESULT_COLUMNS
        | LISTING_LISTING_REF_RESULT_COLUMNS | LISTING_DATETIME_RESULT_COLUMNS
        | frozenset(LISTING_LABEL_RESULT_VALUES)
        ) == LISTING_RESULT_COLUMNS, "每一列都必须有一个校验类别可归"
# 链接句柄：`(namespace, 店, 链接, 平台 SKU)` 的单向摘要，不是 ERP 主键的别名。
# 用独立前缀而不是 `ent-`：`ent-` 引用由目录解析成展示名，而链接号本身
# 不是可读实体名，混进同一命名空间只会被展示层当成"名称未取得"藏起来。
LISTING_REF_RE = re.compile(r"^lst-[0-9a-f]{12}$")


class RosterConflict(Exception):
    """同一期望项被两条不同目标价命中：这是缺一个明确选择，不是可以取平均的噪声。"""

    def __init__(self, items) -> None:  # noqa: ANN001
        super().__init__("listing_expectation_conflict")
        self.items = tuple(items)


# ---------------------------------------------------------------------------
# 金额规则
# ---------------------------------------------------------------------------


def currency_precision(currency: object) -> int | None:
    """已核验的货币小数位；未登记币种返回 None，不猜一个精度。"""
    code = str(currency or "").strip().upper()
    return CURRENCY_PRECISIONS.get(code)


def normalize_amount(value: object, currency: object) -> Decimal | None:
    """把金额文本读成精确值；不合法、格式不正或币种未登记返回 None。

    只接受纯十进制文本（与运行契约 `_DECIMAL_RE` 同一形状）：带单位、千分位或
    科学计数法的输入都说明来源没按口径给数，这里不替它猜。

    **不做任何舍入**：「按已核验的货币精度规范化」在这里只干两件事——拿币种当
    可比性门禁，以及拿精度当展示下限（见 `price_text`）。把 19.899 舍成 19.90 会把
    一分钱的差异说成"没差"，而目标价比较的全部意义就在那一分钱上。
    """
    if value is None:
        return None
    text = str(value).strip()
    if not _DECIMAL_RE.fullmatch(text):
        return None
    if currency_precision(currency) is None:
        # 未登记币种没有已核验的精度依据：不拿它参与比较，也不拿它参与展示。
        return None
    try:
        return Decimal(text)
    except InvalidOperation:
        return None


def compare_price(actual: str | None, expected: str | None,
                  *, currency: str = "CNY") -> str:
    """仅在币种、规格、时点、完整性都已通过后使用（计划 Task 9 契约）。

    返回 `match` / `mismatch` / `missing_standard` / `unknown`：

    - 缺标准（expected 为 None）是 `missing_standard`：缺的是用户那一侧，
      这是可执行的下一步（去问目标价），而"价格未知"不是；
    - 缺实际价、金额不可解析、币种未登记 → `unknown`：不编一个数，也不当作 0。
    """
    if expected is None:
        return "missing_standard"
    if actual is None:
        return "unknown"
    expected_value = normalize_amount(expected, currency)
    actual_value = normalize_amount(actual, currency)
    if expected_value is None or actual_value is None:
        return "unknown"
    return "match" if expected_value == actual_value else "mismatch"


def amount_difference(actual: str | None, expected: str | None,
                      *, currency: str = "CNY") -> str | None:
    """实际价 − 目标价（带符号文本）；任何一侧不可解析就是 null，不是 0。"""
    if actual is None or expected is None:
        return None
    actual_value = normalize_amount(actual, currency)
    expected_value = normalize_amount(expected, currency)
    if actual_value is None or expected_value is None:
        return None
    difference = (actual_value - expected_value).normalize()
    exponent = difference.as_tuple().exponent
    if isinstance(exponent, int) and exponent > 0:
        # normalize 会把 20 写成 2E+1：出表要的是人读的形，不是指数的形。
        difference = difference.quantize(Decimal(1))
    return str(difference) if difference else "0"


def price_text(value: object, currency: object) -> str | None:
    """标价出表：按已核验的货币精度作为**小数下限**，不多不少地保留来源给的值。

    - `39.90` 与 `39.9000`（numeric(18,4) 读回来的形状）都写成 `39.90`：去尾零再按
      币种精度补齐，不改变数值；
    - 来源给了超过币种精度的位数（`19.899`）就**原样写出去**，不向下取整：拿一个
      被舍掉的数去对后台是错的，拿它去比目标价也是错的；
    - 与 `amount_difference` 故意不同：单价是一行报价，差额是一个导出的量。
    """
    amount = normalize_amount(value, currency)
    if amount is None:
        return None
    precision = currency_precision(currency) or 0
    stripped = amount.normalize()
    exponent = stripped.as_tuple().exponent
    fraction = -exponent if isinstance(exponent, int) and exponent < 0 else 0
    scale = max(precision, fraction)
    return str(stripped.quantize(Decimal(1).scaleb(-scale)))


# ---------------------------------------------------------------------------
# 时效规则
# ---------------------------------------------------------------------------


def freshness_ok(*, captured_at: datetime | None, now: datetime,
                 max_age_seconds: int) -> bool:
    """快照是否仍在该来源的时效策略内。

    缺抓取时间就是"无从判断新鲜"，按不新鲜处理：一个没有时点的价格证明不了
    "现在"的标价（spec §5.4「最新快照超过该来源 freshness policy 时标 stale」）。
    """
    if captured_at is None or max_age_seconds <= 0:
        return False
    if captured_at.tzinfo is None or now.tzinfo is None:
        # 无时区的时间会被当成 UTC 或本地时间，两种猜测都能把过期说成新鲜。
        return False
    age = (now - captured_at).total_seconds()
    # 允许一个浮点误差位：跨机器时钟抖动不该把一次复核翻成过期。
    return age <= max_age_seconds + 1e-6


def beijing_iso(value: datetime | None) -> str | None:
    """快照时点出表：统一换成北京时区 ISO 文本（与 `data_as_of` 同一渲染）。"""
    if value is None:
        return None
    from zoneinfo import ZoneInfo

    return value.astimezone(ZoneInfo("Asia/Shanghai")).isoformat()


# ---------------------------------------------------------------------------
# 链接句柄
# ---------------------------------------------------------------------------


def listing_ref_for(namespace: str, shop_id: str, listing_id: str,
                    platform_sku_id: str) -> str:
    """`(账号范围, 店铺, 链接, 平台 SKU)` → 稳定句柄。

    namespace 必须在摘要里：另一个账号里的相同链接号不是同一条链接（Task 6 红线）。
    截断到 12 位十六进制（约 2^48）：句柄只用于在同一份结果里区分链接，
    不承担授权凭证职责，撞车也不会把数据并到别的店上。
    """
    material = "\x1f".join((str(namespace), str(shop_id), str(listing_id),
                            str(platform_sku_id)))
    return "lst-" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:12]


# ---------------------------------------------------------------------------
# 本轮目标价 → 期望项
# ---------------------------------------------------------------------------


def resolve_price_rules(rules, items):  # noqa: ANN001
    """把本轮目标价规则摊到期望项上，返回 {(shop_ref, sku_ref): 金额文本}。

    没被任何规则命中的项取值 None（→ `missing_standard`），**不**回退到别的规则：
    缺标准价是"这一项还没被问到"，不是"按别的规格的价格算"。

    两条规则命中同一项且金额不同 → `RosterConflict`（spec §4：冲突返回 needs_input，
    不取平均、不先来的赢）。金额完全相同的重复规则不是冲突。
    """
    resolved: dict[tuple, str] = {}
    conflicts: list[tuple] = []
    for item in items:
        amounts = {str(rule["expected_amount"]).strip()
                   for rule in _applicable(rules, item)}
        amounts.discard("")
        if len(amounts) > 1:
            conflicts.append(item)
        elif amounts:
            resolved[item] = sorted(amounts)[0]
    if conflicts:
        raise RosterConflict(conflicts)
    return resolved


def _applicable(rules, item):  # noqa: ANN001
    shop_ref, sku_ref = item
    for rule in rules:
        applies_to = str(rule.get("applies_to"))
        if applies_to == "all_selected":
            yield rule
        elif applies_to == "sku" and sku_ref is not None \
                and str(rule.get("sku_ref") or "") == str(sku_ref):
            yield rule
        elif applies_to == "shop_sku" and sku_ref is not None \
                and str(rule.get("shop_ref") or "") == str(shop_ref) \
                and str(rule.get("sku_ref") or "") == str(sku_ref):
            yield rule


# ---------------------------------------------------------------------------
# 来源门禁
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ListingSourceRegistration:
    """一个平台的渠道在售价来源：证据、时效与非敏感通道。

    `evidence` 必须是可核对的东西（探针编号、对账单号、渠道接口批次），
    `max_age_seconds` 是该来源已批准的时效策略。空证据、零时效都是"其实没取证"。
    """

    platform: str
    source_kind: str
    evidence: str
    max_age_seconds: int

    def __post_init__(self) -> None:
        code = str(self.platform or "").strip().lower()
        if not code:
            raise ValueError("listing_source_platform_required")
        if self.source_kind not in LISTING_SOURCE_KINDS:
            # 未登记的通道（含 ERP 建议价）不能成为已核验来源。
            raise ValueError("listing_source_kind_unapproved")
        if not str(self.evidence or "").strip():
            raise ValueError("listing_source_evidence_required")
        if int(self.max_age_seconds) <= 0 or math.isnan(self.max_age_seconds):
            raise ValueError("listing_source_freshness_policy_required")


_REGISTRY: dict[str, ListingSourceRegistration] = {}


def register_listing_source(entry: ListingSourceRegistration) -> None:
    """登记一条已核验来源（服务端专用：不暴露给模型入参，也不读配置文件）。"""
    code = str(entry.platform or "").strip().lower()
    _REGISTRY[code] = entry


def reset_listing_sources() -> None:
    """清空注册表：测试夹具与真实部署都从"一条都没有"起步。"""
    _REGISTRY.clear()


def verified_listing_source(platform: object) -> ListingSourceRegistration | None:
    """该平台是否已有已核验的渠道在售价来源；没有登记就没有，不回退。"""
    return _REGISTRY.get(str(platform or "").strip().lower())


def verified_listing_sources() -> Mapping[str, ListingSourceRegistration]:
    return dict(_REGISTRY)


__all__ = [
    "APPLIES_TO_VALUES", "AUDIT_STATUSES", "CNY_PRECISION", "CURRENCY_PRECISIONS",
    "JUDGED_STATUSES", "LISTING_CODE_BY_TEXT",
    "LISTING_COLUMN_KINDS", "LISTING_DATETIME_RESULT_COLUMNS",
    "LISTING_GRAPH_VERSION", "LISTING_LABEL_RESULT_VALUES",
    "LISTING_LIMITATION_CODES", "LISTING_LISTING_REF_RESULT_COLUMNS",
    "LISTING_METRIC_VERSION",
    "LISTING_NO_MAPPING", "LISTING_PUBLIC_LIMITATIONS", "LISTING_REF_RE","LISTING_RESULT_COLUMNS", "LISTING_RULE_VERSION",
    "LISTING_SCHEMA_VERSION", "LISTING_SOURCE_KINDS",
    "LISTING_SOURCE_REGISTRY_VERSION", "LISTING_TEMPLATE_ID",
    "LISTING_TEMPLATE_VERSION", "MAX_ROSTER_ITEMS", "PRICE_BASES",
    "RosterConflict", "amount_difference", "beijing_iso", "compare_price",
    "currency_precision", "freshness_ok", "listing_limitation_codes",
    "listing_ref_for", "normalize_amount", "price_text", "register_listing_source",
    "reset_listing_sources", "resolve_price_rules", "verified_listing_source",
    "verified_listing_sources", "ListingSourceRegistration",
]
