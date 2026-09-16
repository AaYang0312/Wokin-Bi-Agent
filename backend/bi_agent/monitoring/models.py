"""持续库存监控的严格输入面模型（计划 2026-09-14-continuous-inventory-notifications.md
Task 1 Step 3）。

这里只有状态机与 runner 之后要消费的**数据契约**：全部 ``frozen``、
``extra="forbid"``，ref 排序去重非空，诊断码与行词表都收闭集，数量文本与
runtime 载荷契约（``runtime/models.py`` 的 ``_quantity_or_null``）同一形状。
模型不参与调度、状态迁移、文案、投递或 acknowledge；``MonitorPolicy`` 带
``hide_input_in_errors``：owner subject 与 opaque scope refs 不出现在
校验错误回显里。

``MonitorPolicy.levels`` 是**请求**层级，不是已核验层级；已核验集合只能由
``monitoring.source_gate`` 从来源注册表派生。Task 1 只交付契约；
repository / 状态机 / runner 属计划 Task 2–4。
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from bi_agent.inventory.rules import (AUDIT_LEVELS, INVENTORY_LIMITATION_CODES,
                                      ROW_STATUSES, STORAGE_UNITS)

AlertStatus = Literal["open", "acknowledged", "resolved", "suppressed"]
AlertEventKind = Literal["triggered", "retriggered", "updated", "resolved"]

# monitor 自有的闭集诊断码：只允许本计划命名的这四个，不自创文案、不新增码。
# graph 侧的固定限制码直接复用 `INVENTORY_LIMITATION_CODES`，两份词表不漂移。
MONITOR_DIAGNOSTIC_CODES: tuple[str, ...] = (
    "monitor_source_unverified", "monitor_level_unverified",
    "monitor_unverified_level_row", "monitor_row_scope_missing")
SCAN_DIAGNOSTIC_CODES: frozenset[str] = (
    frozenset(MONITOR_DIAGNOSTIC_CODES) | INVENTORY_LIMITATION_CODES)

# 数量/阈值文本：与 runtime 载荷契约同一形状——不带指数、不带千分位的十进制
# 文本，``None`` 表示"没读到"（缺数量永远不是零）。
_QUANTITY_TEXT_PATTERN = r"^-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?$"
# 监控策略引用：与计划 Task 1 Step 3 的形状逐字一致。
_POLICY_REF_PATTERN = r"^inventory-monitor/[0-9][0-9._-]{0,31}$"


class MonitorRunRequest(BaseModel):
    """一次监控 CLI 运行的请求面：只有策略引用与时点，没有任何真实 ID。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    policy_ref: str = Field(pattern=_POLICY_REF_PATTERN)
    as_of: Literal["latest"] = "latest"


class MonitorPolicy(BaseModel):
    """版本化的监控策略存储形状：请求层级与 opaque scope refs 的**完整**记录。

    ``levels`` 是请求层级；``shop_refs`` / ``inventory_pool_refs`` 是策略在册
    作用域。两者都按原样存储、永不收窄——运行时可运行集合是 gate 的派生结果
    （``source_gate.verified_monitor_levels``），不是这份存储的字段。
    ``physical_total`` 必须带池 ref、``shop_sellable`` 必须带店 ref：一个没有
    作用域的层级根本无法唯一识别一格告警。
    """

    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    policy_ref: str = Field(pattern=_POLICY_REF_PATTERN)
    threshold_policy_ref: str = Field(min_length=1)
    owner_subject_id: str = Field(min_length=1)
    shop_refs: tuple[str, ...]
    inventory_pool_refs: tuple[str, ...]
    levels: tuple[Literal["physical_total", "shop_sellable"], ...]
    cooldown_seconds: int = Field(ge=3600, le=604800)
    enabled: bool

    @field_validator("shop_refs", "inventory_pool_refs")
    @classmethod
    def _refs_are_nonempty_text(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for ref in value:
            if not isinstance(ref, str) or not ref.strip():
                raise ValueError("monitor_policy_ref_empty")
        return value

    @model_validator(mode="after")
    def _sorted_unique_and_level_scoped(self) -> "MonitorPolicy":
        if not self.levels:
            raise ValueError("monitor_policy_levels_required")
        for field in ("shop_refs", "inventory_pool_refs", "levels"):
            items = tuple(getattr(self, field))
            if len(set(items)) != len(items):
                raise ValueError(f"monitor_policy_{field}_must_be_unique")
            if list(items) != sorted(items):
                raise ValueError(f"monitor_policy_{field}_must_be_sorted")
        if "physical_total" in self.levels and not self.inventory_pool_refs:
            raise ValueError("monitor_policy_physical_requires_pool_refs")
        if "shop_sellable" in self.levels and not self.shop_refs:
            raise ValueError("monitor_policy_channel_requires_shop_refs")
        return self


class MonitorAlertRow(BaseModel):
    """状态机可见的 graph 投影行：只有引用、句柄与数值，没有任何真实主键。

    ``quantity`` 取本层级的判定数量（``physical_total``→quantity、
    ``shop_sellable``→channel_quantity），永不从另一层级取数；``status`` 就是
    graph 投影里的 ``inventory_status``（与 runtime 载荷同一份闭集词表）。
    ``scope_ref`` 必填且非空，只能由本层级的 opaque 引用给出（实物 = ``pool_ref``
    + ``warehouse_ref`` 的 canonical 对，渠道 = ``shop_ref``）。载荷里缺任一成员
    的 graph 行（如已核验实物层的无池无仓库 ``unknown`` 占位行）**不得被构造成
    ``MonitorAlertRow``**：runner 直接不把它放进 ``rows``，只记固定码与固定计数
    （计划 Task 4 Step 4）。行上不携 ``rule_code``：首版只有阈值规则，
    ``rule_code`` 由状态机以固定常量参与 dedupe key，不从行里读。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    level: Literal["physical_total", "shop_sellable"]
    status: str
    sku_ref: str = Field(min_length=1)
    scope_ref: str = Field(min_length=1)   # 缺作用域引用的行根本拼不出这个模型
    quantity: str | None = Field(pattern=_QUANTITY_TEXT_PATTERN)
    threshold: str | None = Field(pattern=_QUANTITY_TEXT_PATTERN)
    unit: str

    @field_validator("scope_ref")
    @classmethod
    def _scope_ref_is_present(cls, value: str) -> str:
        # "非空"是 strip 后非空：一个全空白的引用等于没有作用域，拼不出可去重的格。
        if not value.strip():
            raise ValueError("monitor_row_scope_ref_required")
        return value

    @field_validator("status")
    @classmethod
    def _status_in_runtime_vocabulary(cls, value: str) -> str:
        # 与 runtime 载荷契约的 `_string_in(row["inventory_status"], _INVENTORY_STATUSES)`
        # 同一份词表：monitor 行不发明第五、第六个判定态。
        if value not in ROW_STATUSES:
            raise ValueError("monitor_row_status_unrecognized")
        return value

    @field_validator("unit")
    @classmethod
    def _unit_in_storage_vocabulary(cls, value: str) -> str:
        # 与 019 / `_inventory_unit` 同一份存储单位集合：存得进来的单位才能出现在
        # 状态机输入里，`kit` 这类"存得进、无精度"的单位照常在场。
        if value not in STORAGE_UNITS:
            raise ValueError("monitor_row_unit_unregistered")
        return value


class MonitorScan(BaseModel):
    """一次扫描交给状态机的不可变输入面。

    ``complete_levels`` 只能是「已核验 ∩ 本轮该层级扫描完整」的子集：未核验层级
    永远不在其中，因此它既不能被当成"缺页"撑住解除，也不能被补成零数量行。
    ``rows`` 是状态机唯一的行输入：正常路径上它只包含已核验层级的行（runner 把
    graph 请求与授权投影同时收窄到 verified_levels，计划 Task 4 Step 3），作为
    第二道闸 runner 仍按 ``verified_levels`` 过滤一次；已持久化的
    ``source_artifact_payload`` 与 ``source_fingerprint`` 保持 graph 原样，不改写、
    不重算。``diagnostics`` 只收闭集：graph 既有固定限制码 + 本模块的
    ``monitor_`` 前缀固定码。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    policy_ref: str = Field(pattern=_POLICY_REF_PATTERN)
    source_artifact_payload: dict[str, object]
    source_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    data_as_of: datetime
    fresh: bool
    complete_levels: tuple[str, ...]
    rows: tuple[MonitorAlertRow, ...]
    diagnostics: tuple[str, ...]

    @model_validator(mode="after")
    def _closed_level_and_diagnostic_sets(self) -> "MonitorScan":
        seen: list[str] = []
        for level in self.complete_levels:
            if level not in AUDIT_LEVELS:
                raise ValueError("monitor_complete_level_unrecognized")
            if level in seen:
                raise ValueError("monitor_complete_levels_must_be_unique")
            seen.append(level)
        if seen != sorted(seen):
            raise ValueError("monitor_complete_levels_must_be_sorted")
        for code in self.diagnostics:
            if code not in SCAN_DIAGNOSTIC_CODES:
                raise ValueError("monitor_diagnostic_code_unrecognized")
        return self


class AlertTransition(BaseModel):
    """一次状态迁移的持久化意图：ref 与 dedupe key 都是闭集形状。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    alert_ref: str = Field(pattern=r"^alert-[a-z0-9-]{1,60}$")
    previous_status: AlertStatus | None
    next_status: AlertStatus
    event_kind: AlertEventKind
    dedupe_key: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_artifact_ref: str


class StoredAlert(BaseModel):
    """已持久化告警的状态机视图：generation 从 1 起。

    ``level`` / ``sku_ref`` / ``scope_ref`` 是 2026-09-17 批准的身份桥
    （计划 Task 3）：保留/退场抑制的 ``AlertDecision`` 必须携带真实身份，而
    dedupe key 是单向哈希，纯状态机不能从它反推身份；读取面因此把 023
    ``bi.inventory_alert_instances`` 既有的这三列一并带回。行上仍不携
    ``rule_code``：首版只有阈值规则，规则码由状态机以固定常量参与 dedupe key
    （``state_machine``），不从存储读。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    alert_ref: str
    dedupe_key: str
    generation: int = Field(ge=1)
    status: AlertStatus
    level: Literal["physical_total", "shop_sellable"]
    sku_ref: str = Field(min_length=1)
    scope_ref: str = Field(min_length=1)
    last_observed_at: datetime
    last_notified_at: datetime | None

    @field_validator("sku_ref", "scope_ref")
    @classmethod
    def _identity_is_present(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("monitor_alert_identity_required")
        return value


class AlertDecision(BaseModel):
    """状态机的单格决策。``observed`` 区分两种 ``event_kind=updated``：本轮真看到
    那一格的异常（``observed=True``，推进 ``last_observed_at``）与本轮没有该层新
    观测、只是不能解除（``observed=False``，不推进）。两者都不写 outbox，都不变
    ``resolved``；缺来源与缺扫描的保留决策永远取 ``observed=False``。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    dedupe_key: str
    generation: int = Field(ge=1)
    previous_status: AlertStatus | None
    next_status: AlertStatus
    event_kind: AlertEventKind
    notify: bool
    observed: bool
    rule_code: str
    level: Literal["physical_total", "shop_sellable"]
    sku_ref: str
    scope_ref: str


class DeliverySummary(BaseModel):
    """outbox 投递步的计数结果：上限与计划 Task 2 的 batch 100 一致。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    selected: int = Field(ge=0, le=100)
    delivered: int = Field(ge=0, le=100)
    retried: int = Field(ge=0, le=100)
    dead_lettered: int = Field(ge=0, le=100)
