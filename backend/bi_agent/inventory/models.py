"""库存预警的输入契约与图内数据结构（模型可见部分只有业务字段与引用）。

与经营图 / 价审图共用 `commerce.models` 的 `DomainContext`、`CommerceScope`：跨领域
公共契约写两份，就会在「什么叫 all_authorized」上长出两个答案。

本域新增的服务端上下文只有一件：`allowed_inventory_pool_ids`。它**必须**由服务端给
（spec §5.5「总池包含用户未获准的仓库或其他主体数据时，需要独立 inventory-pool
授权；不能由店铺授权推导」），所以：

- 它不在模型可见的入参里，schema 里连字段名都不出现；
- 默认值是空集：本轮没有任何池授权配置时，实物那一格只能是未判定，不会凭空可看。

`levels` 两个口径是**两个问题**：`physical_total` 回答要不要补货，`shop_sellable`
回答要不要调配额。它们的来源、批次、时效、阈值档位都各自一套，永不相加。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field, model_validator

from bi_agent.catalog import REF_RE
from bi_agent.catalog.models import EntityKind, ref_for_key
from bi_agent.commerce.models import CommerceScope, DomainContext

from .rules import (
    MAX_THRESHOLD_QUANTITY, POOL_REF_RE, UNIT_CODE_RE, UNIT_PRECISIONS,
    normalize_quantity, quantity_precision, shop_connection_kind_of)

AsOf = Literal["latest"]
ProductsMode = Literal["all", "selected"]
_POLICY_REF_RE = None  # 复用 rules 里的版本引用形式（见 _policy_shape）


def _policy_ref_valid(value: str) -> bool:
    """策略引用形如 `sku-default/1`：与口径名同一规则，不开自由文本通道。"""
    import re

    return bool(re.fullmatch(r"^[a-z][a-z0-9_-]{0,31}/[0-9][0-9._-]{0,15}$", value))


class ThresholdInput(BaseModel):
    """本轮用户明确给出的一个阈值（spec §4）。

    档位与口径必须配对：`low_quota`（店铺可售）只能配 `shop_ref`，`low_replenish`
    （实物）只能配 `pool_ref` 或按 SKU 全局给。跨着给就是问错了问题——渠道缺货与
    仓库补货是两种动作。
    """

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True,
                              allow_inf_nan=False)

    level: Literal["low_replenish", "low_quota"]
    sku_ref: str = Field(pattern=REF_RE.pattern)
    quantity: str = Field(min_length=1, max_length=20)
    unit: str = Field(min_length=1, max_length=16)
    shop_ref: str | None = Field(default=None, pattern=REF_RE.pattern)
    pool_ref: str | None = None

    @model_validator(mode="after")
    def _check_shape(self) -> "ThresholdInput":
        if not UNIT_CODE_RE.fullmatch(self.unit):
            raise ValueError("unit 必须是已登记的小写单位码")
        if self.unit not in UNIT_PRECISIONS:
            raise ValueError("未登记单位没有阈值精度可言")
        if quantity_precision(self.unit) is None:
            raise ValueError("unit 未登记")
        parsed = normalize_quantity(self.quantity, self.unit)
        if parsed is None:
            raise ValueError("quantity 必须与单位精度一致（整数单位不收小数）")
        if parsed < 0:
            raise ValueError("quantity 不能为负")
        if parsed > MAX_THRESHOLD_QUANTITY:
            raise ValueError("quantity 超出可配置上限")
        if self.shop_ref is not None and self.level != "low_quota":
            raise ValueError("shop_ref 只对 low_quota 档位有意义")
        if self.level == "low_quota" and self.pool_ref is not None:
            raise ValueError("low_quota 按店铺给，不带库存池")
        if self.pool_ref is not None and not POOL_REF_RE.fullmatch(str(self.pool_ref)):
            raise ValueError("pool_ref 必须是 pl- 句柄")
        return self

    def normalized(self) -> dict[str, Any]:
        entry: dict[str, Any] = {"level": self.level, "sku_ref": self.sku_ref,
                                "quantity": self.quantity, "unit": self.unit}
        if self.shop_ref:
            entry["shop_ref"] = self.shop_ref
        if self.pool_ref:
            entry["pool_ref"] = self.pool_ref
        return entry


class InventoryInspectionRequest(BaseModel):
    """`inspect_inventory` 的公开输入（spec §4 Tool 目录逐项对应）。"""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True,
                              allow_inf_nan=False)

    products: ProductsMode = "selected"
    product_refs: list[str] = Field(default_factory=list, max_length=50)
    sku_refs: list[str] = Field(default_factory=list, max_length=200)
    scope: CommerceScope = Field(default_factory=CommerceScope)
    levels: list[Literal["physical_total", "shop_sellable"]] = Field(min_length=1,
                                                                     max_length=2)
    as_of: AsOf = "latest"
    # 两者互斥：同时给就是两套分母，取哪一个都没依据（spec §4）。
    threshold_policy_ref: str | None = None
    thresholds: list[ThresholdInput] | None = Field(default=None, min_length=1,
                                                    max_length=200)

    @model_validator(mode="after")
    def _check_bounds(self) -> "InventoryInspectionRequest":
        for ref in self.product_refs:
            if not REF_RE.fullmatch(str(ref)):
                raise ValueError("product_refs 只能是 ent- 引用")
        for ref in self.sku_refs:
            if not REF_RE.fullmatch(str(ref)):
                raise ValueError("sku_refs 只能是 ent- 引用")
        if len(set(self.levels)) != len(self.levels):
            raise ValueError("levels 不能重复：同一口径出两行会被读成两个口径")
        if self.products == "selected" and not (self.sku_refs or self.product_refs):
            raise ValueError("selected 必须给出 product_refs 或 sku_refs")
        if self.scope.mode != "selected" and self.scope.shop_refs:
            # 全部授权又带一份引用清单：范围按哪个听？静默按 all_authorized 展开、
            # 却又在载荷里回显那份清单，就是把"只问了这两家"说成"问了所有家"。
            raise ValueError("shop_refs 只在 scope.mode=selected 时生效；要缩小范围就选 "
                             "selected，要全授权就不要带 shop_refs")
        if self.products == "all" and (self.sku_refs or self.product_refs):
            # all 又要点名：那是两个全集定义。让模型选一个就是把范围猜掉。
            raise ValueError("products=all 时不带 product_refs / sku_refs")
        if self.thresholds and self.threshold_policy_ref:
            raise ValueError("thresholds 与 threshold_policy_ref 互斥")
        if self.threshold_policy_ref is not None and not _policy_ref_valid(
                self.threshold_policy_ref):
            raise ValueError("threshold_policy_ref 形如 scope/1")
        if self.thresholds:
            levels = set(self.levels)
            for rule in self.thresholds:
                wanted = "physical_total" if rule.level == "low_replenish" \
                    else "shop_sellable"
                if wanted not in levels:
                    raise ValueError(
                        f"{rule.level} 只对 levels 里的 {wanted} 有意义")
            # 同一目标项给两个不同数量：在**本轮内部**就已经矛盾，不进图。
            seen: dict[tuple, str] = {}
            for rule in self.thresholds:
                key = (rule.level, rule.sku_ref, rule.shop_ref or "", rule.pool_ref or "")
                if key in seen and seen[key] != rule.quantity:
                    raise ValueError("同一目标项给了两个不同阈值")
                seen[key] = rule.quantity
        return self

    @property
    def threshold_rules(self) -> list[dict[str, Any]]:
        return [rule.normalized() for rule in (self.thresholds or [])]

    def normalized(self) -> dict[str, object]:
        """可持久化、可指纹化的规范化请求：只含引用、业务码与本轮阈值。

        阈值必须进这里：它参与指纹，于是「同一家店换个阈值再问」不可能命中上一轮
        的结果。`thresholds=None`（按已配置策略）与 `thresholds=[...]` 也是两个不同
        的问题。
        """
        payload: dict[str, object] = {
            "products": self.products,
            "scope_mode": self.scope.mode,
            "levels": sorted(set(self.levels)),
            "as_of": self.as_of,
        }
        if self.sku_refs:
            payload["sku_refs"] = sorted(set(self.sku_refs))
        if self.product_refs:
            payload["product_refs"] = sorted(set(self.product_refs))
        if self.scope.shop_refs:
            payload["shop_refs"] = list(self.scope.shop_refs)
        if self.scope.platforms:
            payload["platforms"] = list(self.scope.platforms)
        if self.threshold_policy_ref:
            payload["threshold_policy_ref"] = self.threshold_policy_ref
        # 完全相同的重复阈值只留一条；不同数量的那一例已在入口被拒。
        payload["thresholds"] = _dedupe([rule.normalized()
                                         for rule in (self.thresholds or [])])
        return payload


def _dedupe(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple] = set()
    kept: list[dict[str, Any]] = []
    for entry in entries:
        key = tuple(sorted(entry.items()))
        if key in seen:
            continue
        seen.add(key)
        kept.append(entry)
    return kept


@dataclass(frozen=True)
class InventoryPool:
    """一个库存池：身份、连接方式与它服务哪些店铺。

    这里**没有**快照字段：池与店的关系是配置，快照是事实。把两者放进同一个对象，
    就会出现"读到了池、没读到快照"时把缺事实说成缺配置——那是两种不同的下一步。
    连接方式也不构成读取授权（spec §5.5）。
    """

    pool_id: str
    pool_ref: str
    namespace: str = ""
    label: str = ""
    connection_kind: str = "shared"
    shop_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        shop_connection_kind_of(self.connection_kind)


@dataclass(frozen=True)
class PhysicalRow:
    """一条实物库存事实：`(池, 仓库, SKU, 批次, 单位)` 上的一次读数。"""

    snapshot_id: str
    namespace: str
    pool_id: str
    pool_ref: str
    warehouse_id: str
    warehouse_ref: str
    erp_sku_id: str
    sku_ref: str
    batch_id: str
    unit: str
    available_quantity: str | None
    inbound_quantity: str | None = None
    locked_quantity: str | None = None
    source: str = "erp"
    captured_at: datetime | None = None
    scan_complete: bool = False

    @property
    def group_key(self) -> tuple[str, str, str]:
        """汇总身份：`(池句柄, 仓库句柄, SKU)`。

        批次与单位**不在**这里：它们参与去重键，但不是两个不同的预警格。账号范围也不
        单独进键——池与仓库的句柄本身就是 `(账号范围, 号)` 的摘要，再放一次只会让写入侧
        与读取侧各拼出一个键，而两边都对不上。
        """
        return (self.pool_ref, self.warehouse_ref, self.erp_sku_id)

    @property
    def identity(self) -> str:
        from .rules import physical_identity

        return physical_identity({"namespace": self.namespace,
                                  "pool_ref": self.pool_ref,
                                  "warehouse_ref": self.warehouse_ref,
                                  "sku_ref": self.sku_ref,
                                  "batch_id": self.batch_id, "unit": self.unit})


@dataclass(frozen=True)
class ChannelRow:
    """一条店铺可售事实：`(平台, 店铺, 链接, SKU, 快照)` 上的一次读数。"""

    snapshot_id: str
    namespace: str
    shop_id: str
    platform: str
    listing_id: str
    platform_sku_id: str
    erp_sku_id: str
    sku_ref: str
    sellable_quantity: str | None
    unit: str
    captured_at: datetime | None = None
    source: str = "official_export"
    scan_complete: bool = False


@dataclass(frozen=True)
class Threshold:
    """一份阈值：配置版或本轮 inline，两者在图上走同一条判定。"""

    level: str
    sku_ref: str
    quantity: str
    unit: str
    shop_ref: str = ""
    pool_ref: str = ""
    version: str = "inline"


@dataclass(frozen=True)
class StockAlertRow:
    """一格的预警结果（行原文形态：真实主键只活在这里，投影层负责换引用）。"""

    level: str
    sku_ref: str
    erp_sku_id: str = ""
    shop_id: str = ""
    shop_ref_value: str = ""
    pool_ref: str | None = None
    warehouse_ref: str | None = None
    quantity: str | None = None
    channel_quantity: str | None = None
    threshold: str | None = None
    unit: str = "piece"
    batch_count: int = 0
    status: str = "unknown"
    action: str = ""
    reason: str = ""
    snapshot_at: str | None = None

    @property
    def shop_ref(self) -> str:
        return self.shop_ref_value or (ref_for_key(EntityKind.SHOP.value, self.shop_id)
                                       if self.shop_id else "")

    def as_payload(self) -> dict[str, Any]:
        """公开行：只有引用、句柄与数值，没有任何真实主键。"""
        row: dict[str, Any] = {"level": self.level, "sku_ref": self.sku_ref,
                               "inventory_status": self.status,
                               "quantity": self.quantity,
                               "channel_quantity": self.channel_quantity,
                               "threshold": self.threshold, "unit": self.unit,
                               "batch_count": self.batch_count}
        if self.shop_ref:
            row["shop_ref"] = self.shop_ref
        if self.pool_ref:
            row["pool_ref"] = self.pool_ref
        if self.warehouse_ref:
            row["warehouse_ref"] = self.warehouse_ref
        if self.snapshot_at is not None:
            row["snapshot_at"] = self.snapshot_at
        if self.action:
            row["action"] = self.action
        return row


@dataclass(frozen=True)
class InventoryAlertReport:
    """一次两级预警的报告：范围、判定、池声明与恢复线索各自分列。"""

    status: Literal["ok", "partial", "missing_data", "needs_input", "forbidden",
                    "unavailable"]
    rows: tuple[StockAlertRow, ...] = ()
    summary: Mapping[str, Any] = field(default_factory=dict)
    requested_scope: Mapping[str, object] = field(default_factory=dict)
    evaluated_scope: Mapping[str, object] = field(default_factory=dict)
    excluded_scope: tuple[Mapping[str, object], ...] = ()
    limitations: tuple[str, ...] = ()
    resolved_product: Mapping[str, object] = field(default_factory=dict)
    termination_reason: str | None = None
    data_as_of: datetime | None = None


__all__ = [
    "AsOf", "ChannelRow", "DomainContext", "InventoryAlertReport",
    "InventoryInspectionRequest", "PhysicalRow", "StockAlertRow", "Threshold",
    "ThresholdInput",
]
