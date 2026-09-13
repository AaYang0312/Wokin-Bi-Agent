"""上架复核的输入契约与图内数据结构（模型可见部分只有业务字段与本轮引用）。

与经营图共用 `commerce.models` 里的 `DomainContext`、`ProductSelector` 与
`CommerceScope`：spec §3 把 scope / product 定义成**跨领域公共契约**，两处各写一份
就会在「什么叫 all_authorized」上长出两个答案。

本域专属的输入是 `expected_prices`，它整个契约的存在理由就是「本轮」：

- 必填且非空：缺目标价在入口就是 `needs_input`，不给任何回退机会；
- 金额是 Decimal 文本、币种与口径逐项声明：不替用户猜一个精度；
- `normalized()` 把它写进请求指纹：换价就是换问题，旧结果不许命中；
- **没有任何字段可以从上一轮、聊天历史或 `SessionState.filters` 里取**。

规则之间的冲突不在这里判：同一 (店, SKU) 被两条不同金额命中，是图上的
`capture_user_expected_prices` 要用 `needs_input` 回答的业务问题，不是 JSON 形状问题。

`RosterItem` 是期望复核项（目标上架全集里的一格），身份来自用户本轮选定的
授权店铺集合 × 商品 SKU / 渠道落点，不来自「抓到了哪些链接」。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field, model_validator

from bi_agent.catalog import REF_RE
from bi_agent.catalog.models import EntityKind, ref_for_key
from bi_agent.commerce.models import CommerceScope, DomainContext, ProductSelector

from .rules import (
    LISTING_SOURCE_KINDS, listing_ref_for, normalize_amount)

AsOf = Literal["latest"]
# 币种词表与运行契约（`runtime.models._CURRENCY_VALUES`）同源：本轮只登记了 CNY。
AUDIT_CURRENCIES: tuple[str, ...] = ("CNY",)
MAX_EXPECTATION_RULES = 200


def _dedupe_expectations(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """按整条内容去重，并保持先后顺序。

    去重只看"整条一样"，不看"目标项一样"：两条指向同一格但金额不同的规则必须都
    留着，让 `capture_user_expected_prices` 把它们判成冲突（needs_input）。在这里
    按目标项去重就等于由代码替用户选了一个价。
    """
    seen: set[tuple] = set()
    kept: list[dict[str, Any]] = []
    for entry in entries:
        key = tuple(sorted(entry.items()))
        if key in seen:
            continue
        seen.add(key)
        kept.append(entry)
    return kept


class ExpectedPrice(BaseModel):
    """用户在本轮明确指定的一条目标价（spec §4）。

    形状由 `applies_to` 决定，三个档位互斥：给 `sku` 档又带 `shop_ref` 就留下了
    「这条按 SKU 还是按店 SKU 生效」的第二义，不猜优先级，直接拒。
    """

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True,
                              allow_inf_nan=False)

    applies_to: Literal["all_selected", "sku", "shop_sku"]
    shop_ref: str | None = Field(default=None, pattern=REF_RE.pattern,
                                 description="ent- 形式的店铺引用（仅 shop_sku 档）")
    sku_ref: str | None = Field(default=None, pattern=REF_RE.pattern,
                                description="ent- 形式的 SKU 引用（sku / shop_sku 档）")
    expected_amount: str = Field(min_length=1, max_length=20)
    currency: Literal["CNY"] = "CNY"
    price_basis: Literal["list_price", "campaign_price"] | None = Field(
        default=None, description="省略即沿用请求的 price_basis；两者不一致会被拒")

    @model_validator(mode="after")
    def _shape_matches_variant(self) -> "ExpectedPrice":
        amount = str(self.expected_amount).strip()
        if normalize_amount(amount, self.currency) is None:
            # 不是纯十进制文本：让模型自己给容差或换算依据都是没依据的数。
            raise ValueError("expected_amount 必须是十进制金额文本")
        self.expected_amount = amount
        if self.applies_to == "all_selected" and (self.shop_ref or self.sku_ref):
            raise ValueError("all_selected 不该带 shop_ref / sku_ref")
        if self.applies_to == "sku" and (not self.sku_ref or self.shop_ref):
            raise ValueError("sku 档只带 sku_ref")
        if self.applies_to == "shop_sku" and not (self.shop_ref and self.sku_ref):
            raise ValueError("shop_sku 档必须同时给出 shop_ref 与 sku_ref")
        return self

    def as_rule(self, *, price_basis: str) -> dict[str, Any]:
        """换成规则层的纯数据结构（规则层不依赖请求模型，避免互相引用）。"""
        return {"applies_to": self.applies_to, "shop_ref": self.shop_ref,
                "sku_ref": self.sku_ref, "expected_amount": self.expected_amount,
                "currency": self.currency,
                "price_basis": self.price_basis or price_basis}

    def normalized(self, *, price_basis: str) -> dict[str, Any]:
        entry: dict[str, Any] = {"applies_to": self.applies_to,
                                 "expected_amount": self.expected_amount,
                                 "currency": self.currency,
                                 "price_basis": self.price_basis or price_basis}
        if self.shop_ref:
            entry["shop_ref"] = self.shop_ref
        if self.sku_ref:
            entry["sku_ref"] = self.sku_ref
        return entry


class ListingPriceAuditRequest(BaseModel):
    """`audit_listing_prices` 的公开输入（spec §4 Tool 目录逐项对应）。"""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True,
                              allow_inf_nan=False)

    product: ProductSelector
    scope: CommerceScope = Field(default_factory=CommerceScope)
    # 本轮只有"当前"这一档：历史时点必须有对应快照才接受（spec §3）。
    as_of: AsOf = "latest"
    price_basis: Literal["list_price", "campaign_price"] = "list_price"
    expected_prices: list[ExpectedPrice] = Field(min_length=1,
                                                 max_length=MAX_EXPECTATION_RULES)
    currency: Literal["CNY"] = "CNY"

    @model_validator(mode="after")
    def _check_bounds(self) -> "ListingPriceAuditRequest":
        if self.scope.mode != "selected" and self.scope.shop_refs:
            # all_authorized 里又带一份 shop_refs：范围到底听哪个？静默按 all_authorized
            # 展开而又在载荷里回显那两份引用，一份行数就是分母的表就会把“只同两家”
            # 说成“本轮问了这两家”。其他领域里这只是噪声，本领域里它就是答案本身。
            raise ValueError("shop_refs 只在 scope.mode=selected 时生效；"
                             "要缩小范围请选 selected，要全授权就不要带 shop_refs")
        for rule in self.expected_prices:
            if rule.currency != self.currency:
                # 一次复核只用一种币种：混币种要换算依据，本轮没有那个依据。
                raise ValueError("expected_prices 币种必须与请求币种一致")
            if rule.price_basis is not None and rule.price_basis != self.price_basis:
                raise ValueError("expected_prices 的 price_basis 必须与请求一致："
                                 "一次复核只回答一个口径的问题")
        return self

    def rules(self) -> list[dict[str, Any]]:
        return [rule.as_rule(price_basis=self.price_basis)
                for rule in self.expected_prices]

    def normalized(self) -> dict[str, object]:
        """可持久化、可指纹化的规范化请求：只含引用、业务码与本轮目标价。

        目标价金额必须进这里：它参与指纹，于是「同一家店换个目标价再问」不可能
        命中上一轮的结果。商品文本永远不进（spec §3）。
        """
        payload: dict[str, object] = {
            "scope_mode": self.scope.mode,
            "platforms": list(self.scope.platforms),
            "price_basis": self.price_basis,
            "as_of": self.as_of,
            "currency": self.currency,
            # 同一档、同一目标、同一金额的重复声明只留一条：那是同一句话说了两遍，
            # 不是冲突。金额不同的重复声明**不能**在这里合并——那是 needs_input 的
            # 材料，被去重抖掉就会把"用户说了两个价"演成"用户说过一个价"。
            "expected_prices": _dedupe_expectations(
                [rule.normalized(price_basis=self.price_basis)
                 for rule in sorted(
                     self.expected_prices,
                     key=lambda item: (item.applies_to,
                                       item.shop_ref or "",
                                       item.sku_ref or "",
                                       item.expected_amount))]),
        }
        if self.scope.shop_refs:
            payload["shop_refs"] = list(self.scope.shop_refs)
        if self.product.ref:
            payload["product_ref"] = self.product.ref
        if self.product.sku_refs:
            payload["sku_refs"] = sorted(set(self.product.sku_refs))
        return payload


@dataclass(frozen=True)
class RosterItem:
    """期望复核项：目标上架全集里的一格（店 × 链接 × 平台 SKU）。

    `listing_id` / `platform_sku_id` / `erp_sku_id` 是真实标识，只活在进程内；
    对外只有 `listing_ref`（单向句柄）与 `sku_ref`（目录引用）。
    """

    shop_id: str
    shop_ref: str = ""
    namespace: str = ""
    listing_id: str = ""
    platform_sku_id: str = ""
    erp_sku_id: str = ""
    # 用户在 product.sku_refs 里点名、本轮却没有渠道落点的 SKU：身份已知、落点未知。
    # 它带的是模型给的 ent- 引用本身，不反查主键（007 不把引用→主键映射给应用身份）。
    named_sku_ref: str = ""

    @property
    def listing_ref(self) -> str:
        return listing_ref_for(self.namespace, self.shop_id, self.listing_id,
                               self.platform_sku_id)

    @property
    def sku_ref(self) -> str | None:
        if self.erp_sku_id:
            return ref_for_key(EntityKind.SKU.value, self.erp_sku_id)
        return self.named_sku_ref or None

    @property
    def key(self) -> tuple[str, str | None]:
        """规则层的匹配键：(店铺引用, SKU 引用)。"""
        return (self.shop_ref or ref_for_key(EntityKind.SHOP.value, self.shop_id),
                self.sku_ref)

    @property
    def has_landing(self) -> bool:
        """是否有一个已确认的渠道落点（映射给出的链接号）。"""
        return bool(self.listing_id)


@dataclass(frozen=True)
class SnapshotHeader:
    """一次渠道在售快照的头：哪个账号范围、来源、抓取时点与完整性声明。

    `namespace` 与 `(shop_id, snapshot_id)` 一起才是快照的身份：同一批链接号在
    另一个账号里不是同一批（Task 6 同一条红线）。
    """

    snapshot_id: str
    namespace: str
    shop_id: str
    platform: str
    source: str
    captured_at: datetime | None
    enumeration_complete: bool
    batch_id: str | None = None

    def __post_init__(self) -> None:
        if self.source not in LISTING_SOURCE_KINDS:
            raise ValueError("listing_source_kind_unapproved")


@dataclass(frozen=True)
class SnapshotItem:
    """快照里的一条在售记录：真实链接号 + 该链接在本轮请求口径下的标价。"""

    snapshot_id: str
    shop_id: str
    listing_id: str
    platform_sku_id: str = ""
    erp_sku_id: str = ""
    erp_product_id: str = ""
    list_amount: str | None = None
    campaign_amount: str | None = None
    currency: str = "CNY"
    on_sale: bool = True
    captured_at: datetime | None = None

    def amount_for(self, price_basis: str) -> str | None:
        return (self.campaign_amount if price_basis == "campaign_price"
                else self.list_amount)


@dataclass(frozen=True)
class SnapshotBundle:
    """一家店本轮读到的那一次快照：头 + 明细。

    头与明细必须成对：只拿头会丢掉"这一家到底声明了几条链接"，只拿明细会丢掉时效与
    完整性声明，而两者都是逐格判定的依据。
    """

    header: SnapshotHeader
    items: tuple[SnapshotItem, ...] = ()


@dataclass(frozen=True)
class AuditItem:
    """一格的判定结果（图内形态：字段名与行列名故意不同，避免把两者当成一回事）。

    行列名在 `audit_rows` 里才决定：`expected_price` → `expected_amount` 这类换面是
    公开契约的一部分，不该让图内计算依赖它。
    """

    roster: RosterItem
    status: str
    expected_price: str | None = None
    actual_price: str | None = None
    difference: str | None = None
    snapshot_at: str | None = None
    currency: str = "CNY"
    price_basis: str = "list_price"

    def __post_init__(self) -> None:
        from .rules import AUDIT_STATUSES

        if self.status not in AUDIT_STATUSES:
            raise ValueError("audit_status_unsupported")
        # 非判定态不得带出差额：一个基于过期或不可比数据的差，会被当成当前差异
        # 转述给经营者。缺标准价那一格同样不许给差（它缺的是分母的一侧）。
        if self.status not in ("match", "mismatch") and self.difference is not None:
            raise ValueError("audit_difference_needs_a_judgement")


@dataclass(frozen=True)
class JoinedItem:
    """join 节点的输出：一格期望项 + 它归属的那一批快照 + 对到的记录 + 已成立的门禁。

    门禁结论随行传递，`compare_decimal_prices` 才能**只**做金额比较：让它在下游重新
    查一遍字典，就会有第二份判定顺序，两份迟早会漂移。

    为什么带 `header` 而不是带整批快照：一格的时效与"全集已枚举"声明必须由**它自己
    那一批**快照说。同一 shop_id 在两个账号范围各有一批时，拿另一批的完整枚举去证明
    这一家"未上架"，与拿别人家的价去比自己家的价是同一种错；无法归属时 `header` 就是
    None，那一格只能得到 `unknown`。
    """

    roster: RosterItem
    header: SnapshotHeader | None = None
    matched: SnapshotItem | None = None
    expected_amount: str | None = None
    source_ready: bool = False
    fresh: bool = False
    enumerated: bool = False


@dataclass(frozen=True)
class ListingAuditReport:
    """一次复核的报告：范围、判定、来源材料与恢复线索各自分列。"""

    status: Literal["ok", "partial", "missing_data", "needs_input", "forbidden",
                    "unavailable"]
    items: tuple[AuditItem, ...] = ()
    audit: Mapping[str, Any] = field(default_factory=dict)
    requested_scope: Mapping[str, object] = field(default_factory=dict)
    # 本轮真正被评估的店铺（引用形态）：契约 v2 的第三面。`evaluated_shop_ids` 是
    # 同一件事的主键形态，只给内部用（例如建目录时补齐每一家已评估店铺）。
    evaluated_scope: Mapping[str, object] = field(default_factory=dict)
    evaluated_shop_ids: tuple[str, ...] = ()
    excluded_scope: tuple[Mapping[str, object], ...] = ()
    limitations: tuple[str, ...] = ()
    candidates: tuple[Mapping[str, object], ...] = ()
    resolved_product: Mapping[str, object] = field(default_factory=dict)
    termination_reason: str | None = None
    data_as_of: datetime | None = None


__all__ = [
    "AsOf", "AuditItem", "AUDIT_CURRENCIES", "DomainContext", "ExpectedPrice",
    "JoinedItem", "ListingAuditReport", "ListingPriceAuditRequest", "PRICE_BASES",
    "RosterItem", "SnapshotBundle", "SnapshotHeader", "SnapshotItem",
]
