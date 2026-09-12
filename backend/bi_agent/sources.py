"""平台 → 来源 → 指标能力的唯一注册表。

这里只有**代码内的有限注册表**：不建动态插件系统，也不做通用配置中心。新增一个来源
必须同时带来方法名、账号授权、时间语义与金额对账证据，登记进本文件之后，同步、覆盖、
质量、能力与指标契约才能共同消费它。

口径依据：`docs/superpowers/specs/2026-09-12-multi-source-metrics-design.md` §3–§4。
范围决定：`docs/superpowers/research/2026-09-12-drop-pdd-onboarding.md`——2026-09-12
用户决定放弃拼多多方舟授权，因此注册表里**没有** pdd 支付分支，也不保留「开通后再登记」
的待办；pdd 的支付依赖永久解析为能力不足。

两条不可让步的规则：

1. **实体存在 ≠ 指标可用**。`bi.shops.capabilities` 里旧的 `orders` 一类实体标签不授予
   任何支付指标；能力标签与指标同名，只能由已核验的逐源对账证据推导。
2. **未知平台 fail closed**。没有登记就没有来源，更不允许「回退到交易源」之后把空响应
   标成完整覆盖。

`time_certified` 只表示该平台/通道有没有拿到「业务时间窗口完整」的对照证据。抖音通道实测
严格（0/9414 行越界）；出库接口实测不严格（83/8367 行 `paid_at` 早于窗口起点），所以淘系
为 False。未逐店测过的平台不沿用抖音的结论——认证不能跨平台复制。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Mapping

# ---------------------------------------------------------------------------
# 来源（快麦方法名）：只有这三条通道，且都以实测文档为准，不猜方法名。
# ---------------------------------------------------------------------------
TRADE_LIST_SOURCE = "erp.trade.list.query"
OUTSTOCK_SOURCE = "erp.trade.outstock.simple.query"
AFTERSALE_SOURCE = "erp.aftersale.list.query"

ORDERS_ENTITY = "orders"
AFTERSALE_ENTITY = "aftersales_occurrence"
AFTERSALE_COHORT_ENTITY = "aftersales_cohort"

# ---------------------------------------------------------------------------
# 口径版本。名字本身就是契约：换来源必须换 basis，不能沿用旧标签冒充同一口径。
# ---------------------------------------------------------------------------
PAYMENT_BASIS = "platform_payment/v1"
OUTSTOCK_BASIS = "erp_outstock_payment/v1"
DOCUMENT_BASIS = "erp_document/v1"
# 退款两端也是口径：发生额按平台完成时间，同批率需要 cohort 追溯证据。
# 目前没有任何平台拿到“与后台账单对照”的退款完整性证据，所以两个绑定的
# coverage_certified 都是 False；Task 5.2/5.3 不得把它当已认证窗口出数。
REFUND_BASIS = "platform_refund_occurrence/v1"
COHORT_BASIS = "matched_cohort/v1"

Capability = Literal[
    "paid_amount", "paid_orders", "erp_documents", "aov", "quantity",
    "product_paid_amount", "refund_amount", "cash_difference", "cohort_refund_rate",
]
# 能力标签与指标同名，一一对应：这既让「逐指标能力」可枚举，也排除了自造标签。
METRIC_CAPABILITIES: frozenset[str] = frozenset(
    ("paid_amount", "paid_orders", "erp_documents", "aov", "quantity",
     "product_paid_amount", "refund_amount", "cash_difference", "cohort_refund_rate"))

# 支付族：只有拿到已认证支付口径的来源才能授予。
PAYMENT_FAMILY: frozenset[str] = frozenset(
    ("paid_amount", "paid_orders", "aov", "product_paid_amount", "cash_difference",
     "cohort_refund_rate"))

# 指标 → 依赖的业务实体。覆盖门禁与来源解析共用这一份，两边不再各抄一遍。
ENTITY_REQUIREMENTS: dict[str, tuple[str, ...]] = {
    "paid_amount": (ORDERS_ENTITY,),
    "paid_orders": (ORDERS_ENTITY,),
    "erp_documents": (ORDERS_ENTITY,),
    "aov": (ORDERS_ENTITY,),
    "quantity": (ORDERS_ENTITY,),
    "product_paid_amount": (ORDERS_ENTITY,),
    "refund_amount": (ORDERS_ENTITY, AFTERSALE_ENTITY),
    "cash_difference": (ORDERS_ENTITY, AFTERSALE_ENTITY),
    "cohort_refund_rate": (ORDERS_ENTITY, AFTERSALE_COHORT_ENTITY),
}

TimeBasis = Literal["pay_time", "outstock_time", "aftersale_completion_time"]


@dataclass(frozen=True)
class PlatformRegistration:
    """一个平台在这份代码里能拿到什么来源、什么口径、什么上限。"""

    platform: str
    order_source: str
    order_time_basis: TimeBasis
    payment_basis: str | None
    # 该平台是否拿到「业务时间窗口完整」的对照证据；False 时不得声称完整支付窗口。
    time_certified: bool
    # 能力上限：平台上限之外的标签即使被误写进库也不放行。
    ceiling: frozenset[str] = field(default_factory=frozenset)


# 拼多多上限：仅已核验单据能力（行标识缺失、售后未逐店取证，其余一律关）。
PDD_CEILING: frozenset[str] = frozenset({"erp_documents"})

_REGISTRATIONS: dict[str, PlatformRegistration] = {
    # 抖音：交易通道排除淘系/拼多多，fxg 的支付口径与付款时间语义均已逐元对账。
    "fxg": PlatformRegistration("fxg", TRADE_LIST_SOURCE, "pay_time", PAYMENT_BASIS,
                                True, METRIC_CAPABILITIES),
    # 同一交易通道，但支付时间完整性未逐店取证：口径可同名，认证不复制。
    "jd": PlatformRegistration("jd", TRADE_LIST_SOURCE, "pay_time", PAYMENT_BASIS,
                               False, METRIC_CAPABILITIES),
    "kuaishou": PlatformRegistration("kuaishou", TRADE_LIST_SOURCE, "pay_time",
                                     PAYMENT_BASIS, False, METRIC_CAPABILITIES),
    "wxsph": PlatformRegistration("wxsph", TRADE_LIST_SOURCE, "pay_time",
                                  PAYMENT_BASIS, False, METRIC_CAPABILITIES),
    "wsxc": PlatformRegistration("wsxc", TRADE_LIST_SOURCE, "pay_time",
                                PAYMENT_BASIS, False, METRIC_CAPABILITIES),
    # 淘系唯一非敏感订单通道是销售出库；它按自身时间字段裁剪，不承诺支付窗口完整。
    "tb": PlatformRegistration("tb", OUTSTOCK_SOURCE, "outstock_time", OUTSTOCK_BASIS,
                              False, METRIC_CAPABILITIES),
    "tm": PlatformRegistration("tm", OUTSTOCK_SOURCE, "outstock_time", OUTSTOCK_BASIS,
                              False, METRIC_CAPABILITIES),
    # 拼多多：2026-09-12 决定不接入支付。单据口径保留，支付族永久解析不通。
    # 订单源只能给出库通道——官方 `erp.trade.list.query` 明确排除淘系与拼多多，
    # 拿交易源去同步会被空响应伪造成“完整覆盖”（设计 §3 禁止的正是这个回退）。
    "pdd": PlatformRegistration("pdd", OUTSTOCK_SOURCE, "outstock_time", None,
                                False, PDD_CEILING),
}

# 走交易通道的平台集合：供同步与用例遍历，不表示能力相同。拼多多不在里面——
# 它的订单实体只能从出库通道取。
TradeListPlatforms: tuple[str, ...] = tuple(
    sorted(item.platform for item in _REGISTRATIONS.values()
           if item.order_source == TRADE_LIST_SOURCE))
@dataclass(frozen=True)
class ShopRecord:
    """服务端加载的店铺记录。模型只能给 shop_ref，来源与能力一律由服务端解析。"""

    shop_id: str
    platform: str
    capabilities: frozenset[str] = frozenset()

    @classmethod
    def from_row(cls, shop_id: str, platform: str | None,
                 capabilities: object) -> "ShopRecord":
        return cls(shop_id=str(shop_id), platform=_normalise_platform(platform),
                   capabilities=_capability_set(capabilities))


@dataclass(frozen=True)
class SourceBinding:
    """一次「店铺 × 实体」取数依赖：来源、口径与时间认证三件事一起说清。"""

    shop_id: str
    platform: str
    entity: str
    source: str
    basis: str
    time_basis: str
    coverage_certified: bool


def _normalise_platform(platform: object) -> str:
    return str(platform or "").strip().lower()


def _capability_set(capabilities: object) -> frozenset[str]:
    if capabilities is None:
        return frozenset()
    if isinstance(capabilities, str):
        # psycopg 的 text[] 在部分替身里以 "{a,b}" 出现。
        items = [item for item in capabilities.strip("{}").split(",") if item]
    else:
        items = [str(item) for item in capabilities]
    return frozenset(items)


def platform_order_sources() -> dict[str, str]:
    """平台→订单源快照：同步路由与用例遍历共用，不在两处各抄一份。"""
    return {platform: item.order_source
            for platform, item in sorted(_REGISTRATIONS.items())}


def registration(platform: object) -> PlatformRegistration | None:
    """平台登记；未登记返回 None，调用方必须按 fail closed 处理。"""
    return _REGISTRATIONS.get(_normalise_platform(platform))


def _entity_source(reg: PlatformRegistration,
                   entity: str) -> tuple[str, str]:
    """实体 → (来源, 时间口径)。basis 要看指标，所以单独由 `_entity_basis` 定。"""
    if entity == ORDERS_ENTITY:
        return reg.order_source, reg.order_time_basis
    return AFTERSALE_SOURCE, "aftersale_completion_time"


def _entity_basis(reg: PlatformRegistration, metric: str,
                  entity: str) -> tuple[str, bool]:
    """指标在某实体上到底用哪个口径，以及该口径的时间窗口是否已认证。"""
    if entity != ORDERS_ENTITY:
        basis = COHORT_BASIS if entity == AFTERSALE_COHORT_ENTITY else REFUND_BASIS
        return basis, False
    if metric == "erp_documents" or reg.payment_basis is None:
        return DOCUMENT_BASIS, False
    return reg.payment_basis, reg.time_certified


def resolve_order_source(shop: ShopRecord) -> str | None:
    """这家店的订单来源；没有登记就没有来源，不回退。"""
    reg = registration(shop.platform)
    return None if reg is None else reg.order_source


def resolve_metric_sources(shop: ShopRecord,
                           metric: str) -> tuple[SourceBinding, ...]:
    """解析「这家店回答这个指标」需要读哪些来源。

    返回空元组就是能力不足：可能是没登记平台、没授予标签、平台上限不含它，
    或该指标需要的支付口径这个来源根本拿不到。调用方不得把空结果当「没有数据」。
    """
    reg = registration(shop.platform)
    if reg is None or metric not in METRIC_CAPABILITIES:
        return ()
    if metric not in shop.capabilities:
        return ()
    if metric not in reg.ceiling:
        return ()
    if metric in PAYMENT_FAMILY and reg.payment_basis is None:
        return ()

    bindings: list[SourceBinding] = []
    for entity in ENTITY_REQUIREMENTS[metric]:
        source, time_basis = _entity_source(reg, entity)
        basis, certified = _entity_basis(reg, metric, entity)
        bindings.append(SourceBinding(
            shop_id=shop.shop_id, platform=reg.platform, entity=entity, source=source,
            basis=basis, time_basis=time_basis, coverage_certified=certified))
    return tuple(bindings)


def unsupported_reason(shop: ShopRecord, metric: str) -> str | None:
    """为什么这家店回答不了这个指标；能回答时返回 None。

    原因码稳定，且与 `resolve_metric_sources` 同源，调用方不得自己猜归因：

    - `source_unregistered`：平台没有登记来源，禁止回退到别的通道。
    - `capability_ungranted`：来源在，但这家店没被授予该指标能力。
    - `capability_unavailable`：授予了标签，但该来源口径拿不到这个指标（拼多多支付族、
      未知指标名）。注册表上限优先于标签，误写进库的标签不会打开任何能力。

    归因顺序把「没有来源」放在最前：换指标救不了没登记的平台，两种缺口的用户建议不同。
    """
    reg = registration(shop.platform)
    if reg is None:
        return "source_unregistered"
    if resolve_metric_sources(shop, metric):
        return None
    if metric not in METRIC_CAPABILITIES:
        return "capability_unavailable"
    if metric in PAYMENT_FAMILY and reg.payment_basis is None:
        return "capability_unavailable"
    if metric not in reg.ceiling:
        return "capability_unavailable"
    if metric not in shop.capabilities:
        return "capability_ungranted"
    return "capability_unavailable"


def capabilities_from_evidence(
        platform: object,
        evidence: Mapping[tuple[str, str], str]) -> set[str]:
    """由逐来源核验证据推导可授予的能力标签。

    `evidence` 是 `(source, entity) -> quality_status`，只能来自对账（reconcile）写下的
    `bi.sync_state.quality_status`：同步成功不在这里出现，所以「同步过」永远不会开通能力。
    """
    reg = registration(platform)
    if reg is None:
        return set()
    granted: set[str] = set()
    for metric in sorted(reg.ceiling):
        if metric in PAYMENT_FAMILY and reg.payment_basis is None:
            continue
        bindings = [(reg.order_source if entity == ORDERS_ENTITY else AFTERSALE_SOURCE,
                     entity) for entity in ENTITY_REQUIREMENTS[metric]]
        if all(evidence.get(key) == "passed" for key in bindings):
            granted.add(metric)
    return granted
