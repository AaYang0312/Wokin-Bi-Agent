"""一次性监控 runner（计划 2026-09-14 Task 4）：复用 InventoryWatchGraph。

一次 `run_inventory_monitor` = 一个策略 = 一次完整闭环：按层级来源门禁 →
服务端目录解析（opaque refs 只经枚举派生，不反查单向引用）→ 收窄的
`InventoryInspectionRequest` 与 `DomainContext` 授权投影 → MemoryQueryRunStore
上的真实图执行 → 从内存 Store 取回唯一来源 Artifact 并重验 → `MonitorScan`
（fail-closed 的完整性归属与行过滤）→ 纯状态机（verified_levels）→
`repository.commit_scan` 唯一一次事务提交。

固定边界：模型与工具路径零参与；请求层级与授权投影由同一次 gate 结果收窄；
策略存储永不收窄；已持久化载荷与 fingerprint 保持 graph 原样，不改写、不重
算；异常、deadline 与契约错误只记固定 reason，保持 active alerts 不变且零
outbox。本模块不起循环、不开服务器、不调用任何模型。
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping
from uuid import NAMESPACE_URL, UUID, uuid5

import psycopg

from bi_agent.catalog import ref_for_key
from bi_agent.commerce.models import CommerceScope, DomainContext
from bi_agent.inventory.graph import run_inventory_graph
from bi_agent.inventory.models import InventoryInspectionRequest
from bi_agent.inventory.rules import (
    INVENTORY_LIMITATION_CODES, InventorySourceRegistration,
    inventory_limitation_codes, pool_handle, verified_inventory_sources)
from bi_agent.monitoring.models import (
    MONITOR_DIAGNOSTIC_CODES, AlertTransition, MonitorAlertRow,
    MonitorPolicy, MonitorRunRequest, MonitorScan)
from bi_agent.monitoring.repository import MonitorRepository
from bi_agent.monitoring.source_gate import assert_monitor_sources_verified
from bi_agent.runtime.memory import MemoryQueryRunStore
from bi_agent.runtime.models import validate_artifact_payload

# 闭集固定码按位绑定：本模块只使用常量，不复述字面量。
(_MONITOR_SOURCE_UNVERIFIED, _MONITOR_LEVEL_UNVERIFIED,
 _MONITOR_UNVERIFIED_LEVEL_ROW, _MONITOR_ROW_SCOPE_MISSING) = MONITOR_DIAGNOSTIC_CODES

# 023 预置的固定 service chat/message：监控运行不属于任何真人会话。
SERVICE_CHAT_ID = UUID("00000000-0000-4000-8000-000000000023")
SERVICE_MESSAGE_ID = UUID("00000000-0000-4000-8000-000000000024")

_SOURCE_ARTIFACT_TYPE = "inventory_alerts"
# 只有这两类来源状态允许出决策；其余一律 fail-closed。
_DECIDABLE_SOURCE_STATUSES = ("ok", "partial")

# runner 自有的固定计数（labels 只取固定枚举值；不含 policy ref、SKU、主体）。
LEVEL_UNVERIFIED_TOTAL = "inventory_monitor_level_unverified_total"
ROWS_DROPPED_TOTAL = "inventory_monitor_rows_dropped_total"
SKIPPED_TOTAL = "inventory_monitor_skipped_total"

# 完整性归属的闭集（计划 Task 4 Step 4）。全部从 graph 的封闭词表按形状派生，
# 不在本模块复述任何字面量：词表单边改名会当场 NameError/空集，而不是悄悄漂移。
_SHOP_ONLY_CODES = frozenset(
    code for code in INVENTORY_LIMITATION_CODES if "channel_snapshot" in code)
_TRUNCATED_CODE = next(code for code in INVENTORY_LIMITATION_CODES
                       if "display" in code and "truncated" in code)
_SNAPSHOT_MISSING_CODE = next(code for code in INVENTORY_LIMITATION_CODES
                              if "snapshot_missing" in code
                              and "channel" not in code)
_BENIGN_CODES = frozenset(
    code for code in INVENTORY_LIMITATION_CODES
    if code.endswith("_candidate") or "universe" in code)
# 只说池口径的披露：它对店铺层什么都没说；空店铺投影下它没有合法产生点。
_POOL_SCOPE_CODES = frozenset(
    code for code in INVENTORY_LIMITATION_CODES if "pool_not_authorized" in code)
# 其余全部当聚合口径 fail closed（含未登记的码：见 _complete_levels 的兜底分支）。
_AGGREGATE_CODES = frozenset(INVENTORY_LIMITATION_CODES) - _SHOP_ONLY_CODES     - _BENIGN_CODES - _POOL_SCOPE_CODES     - {_TRUNCATED_CODE, _SNAPSHOT_MISSING_CODE}

_ENUMERATE_POOLS_SQL = "SELECT namespace, pool_id FROM reporting.v_inventory_pools"
_ENUMERATE_SHOPS_SQL = "SELECT shop_id FROM reporting.v_shops"

_COUNTERS: dict[str, int] = {}


def _count(name: str, **labels: str) -> None:
    key = name
    if labels:
        key += "{" + ",".join(f"{k}={v}" for k, v in sorted(labels.items())) + "}"
    _COUNTERS[key] = _COUNTERS.get(key, 0) + 1


def counter_total(name: str, **labels: str) -> int:
    key = name
    if labels:
        key += "{" + ",".join(f"{k}={v}" for k, v in sorted(labels.items())) + "}"
    return _COUNTERS.get(key, 0)


def reset_monitor_counters() -> None:
    _COUNTERS.clear()


@dataclass(frozen=True)
class ResolvedScope:
    """策略 opaque refs 经服务端目录枚举派生的落点（真实主键只在此中间态）。"""

    shop_ids: frozenset[str]
    shop_refs: dict[str, str]
    pool_ids: frozenset[str]


@dataclass(frozen=True)
class ContextProjection:
    """同一 gate 结果对 `DomainContext` 的授权投影（纯函数的输出）。"""

    shop_ids: frozenset[str]
    shop_refs: dict[str, str]
    pool_ids: frozenset[str]


def context_projection_for(policy: MonitorPolicy, verified_levels: tuple[str, ...],
                           resolved: ResolvedScope) -> ContextProjection:
    """verified_levels 的纯函数：实物 ⇒ 池 + 空店铺；渠道 ⇒ 店铺 + 空池；都核验 ⇒ 都带。

    投影与策略存储是两回事：`policy.shop_refs` / `inventory_pool_refs` /
    `levels` 原样保留，这里只派生本轮的上下文。
    """
    for level in verified_levels:
        if level not in policy.levels:
            raise ValueError("monitor_projection_level_outside_policy")
    physical = "physical_total" in verified_levels
    channel = "shop_sellable" in verified_levels
    return ContextProjection(
        shop_ids=frozenset(resolved.shop_ids) if channel else frozenset(),
        shop_refs=dict(resolved.shop_refs) if channel else {},
        pool_ids=frozenset(resolved.pool_ids) if physical else frozenset())


def _resolve_policy_scope(conn, policy: MonitorPolicy,
                          deadline: float) -> ResolvedScope:
    """opaque refs → 真实主键：枚举授权目录行、派生句柄后求交，不反查单向引用。"""
    if time.monotonic() > deadline:
        raise TimeoutError("monitor_deadline_exceeded")
    wanted_pools = {str(ref) for ref in policy.inventory_pool_refs}
    pool_ids: set[str] = set()
    if wanted_pools:
        for namespace, pool_id in conn.execute(_ENUMERATE_POOLS_SQL).fetchall():
            if pool_handle(str(namespace), str(pool_id)) in wanted_pools:
                pool_ids.add(str(pool_id))
    wanted_shops = {str(ref) for ref in policy.shop_refs}
    shop_ids: set[str] = set()
    shop_refs: dict[str, str] = {}
    if wanted_shops:
        for row in conn.execute(_ENUMERATE_SHOPS_SQL).fetchall():
            shop_id = str(row[0])
            ref = ref_for_key("shop", shop_id)
            if ref in wanted_shops:
                shop_ids.add(shop_id)
                shop_refs[shop_id] = ref
    return ResolvedScope(shop_ids=frozenset(shop_ids), shop_refs=shop_refs,
                         pool_ids=frozenset(pool_ids))


def _service_refs(policy_ref: str, now: datetime) -> tuple[UUID, str]:
    """root request 与 tool call 由 policy ref + 调度分钟的 UUIDv5 稳定生成。"""
    minute = now.astimezone(timezone.utc).strftime("%Y%m%dT%H%M")
    root = uuid5(NAMESPACE_URL, f"inventory-monitor/{policy_ref}/{minute}")
    return root, f"monitor-{policy_ref}-{minute}"


def _canonical_fingerprint(payload: Mapping[str, Any]) -> str:
    """canonical JSON 的 SHA-256：载荷字节不变，fingerprint 就不变。"""
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _parse_data_as_of(payload: Mapping[str, Any], now: datetime) -> tuple[datetime, bool]:
    """fresh 只在整份载荷拿不到可信 data_as_of 时才为 False（单格过期不改它）。"""
    raw = payload.get("data_as_of")
    if isinstance(raw, str) and raw.strip():
        try:
            stamp = datetime.fromisoformat(raw.strip())
            if stamp.tzinfo is not None:
                return stamp, True
        except ValueError:
            pass
    return now, False


def _source_codes(payload: Mapping[str, Any]) -> set[str]:
    return set(inventory_limitation_codes(list(payload.get("limitations") or [])))


def _complete_levels(policy: MonitorPolicy, verified: tuple[str, ...],
                     payload: Mapping[str, Any], diagnostics: list[str],
                     skipped: list[str]) -> tuple[str, ...]:
    """已核验层级的完整性归属（计划 Task 4 Step 4 的唯一规则表，只单向移除）。"""
    complete = list(verified)
    codes = _source_codes(payload)
    inventory = payload.get("inventory") or {}
    pool_claims = {str(entry.get("pool_ref"))
                   for entry in (inventory.get("pools") or [])}
    if "physical_total" in complete:
        # 实物侧判据是且只是池声明：策略在册池对不上 inventory.pools[].pool_ref
        # （graph 只为读到行的池出声明）⇒ 实物层不算完整。这一判定不依赖限制码
        # 是否出现——graph 自己无法知道策略范围里还有哪个池。
        missing = [ref for ref in policy.inventory_pool_refs
                   if ref not in pool_claims]
        if missing:
            complete.remove("physical_total")
            diagnostics.append(_SNAPSHOT_MISSING_CODE)
            codes = codes - {"inventory_snapshot_missing"}
    if bool(inventory.get("truncated")) or _TRUNCATED_CODE in codes:
        # 展示投影只是 top-20：排序跨层级混合，不能安全归属 ⇒ 全部不算完整。
        diagnostics.append(_TRUNCATED_CODE)
        skipped.append("display_projection_truncated")
        return ()
    shop_evidence = any(entry.get("reason") == "shop_not_synced"
                        for entry in (payload.get("excluded_scope") or []))
    for code in sorted(codes):
        if code in _BENIGN_CODES:
            continue
        if code in _SHOP_ONLY_CODES:
            # 渠道侧码只说店铺；空店铺投影的实物轮里它没有合法产生点 ⇒ 违约。
            if "shop_sellable" in complete:
                complete.remove("shop_sellable")
            else:
                skipped.append("monitor_projection_contract_violation")
                return ()
            continue
        if code == _SNAPSHOT_MISSING_CODE:
            # 双重来源：实物侧判据是且只是池声明；店铺侧证据只说店铺；空店铺
            # 投影下两种证据都不成立 ⇒ 按投影契约违约当聚合码 fail closed。
            gap = [ref for ref in policy.inventory_pool_refs
                   if ref not in pool_claims]
            if gap:
                if "physical_total" in complete:
                    complete.remove("physical_total")
                if shop_evidence and "shop_sellable" in complete:
                    complete.remove("shop_sellable")
                if complete:
                    continue
                return ()
            if shop_evidence and "shop_sellable" in complete:
                complete.remove("shop_sellable")
                continue
            skipped.append("monitor_projection_contract_violation")
            return ()
        if code in _POOL_SCOPE_CODES:
            # 只说池口径：实物层在跑 ⇒ 去掉实物；仅渠道轮里它是预期披露（池侧
            # 事实本来就不在范围），对渠道层什么都没说。
            if "physical_total" in complete:
                complete.remove("physical_total")
                continue
            continue
        if code in _AGGREGATE_CODES:
            # 聚合口径 = "整份行集里有格子没判过/没看全"，无法安全归属单层。
            skipped.append("monitor_aggregate_limitation_code")
            return ()
        # 未登记的码：fail closed，宁可整轮不改状态。
        diagnostics.append(code)
        skipped.append("monitor_unknown_limitation_code")
        return ()
    return tuple(sorted(set(complete)))


def _scan_rows(verified: tuple[str, ...], payload: Mapping[str, Any],
               diagnostics: list[str]) -> tuple[MonitorAlertRow, ...]:
    """第二道闸：只放行已核验层级、且带全本层级 opaque 作用域引用的行。"""
    rows: list[MonitorAlertRow] = []
    seen: set[tuple[str, str, str]] = set()
    for raw in payload.get("data") or []:
        if not isinstance(raw, Mapping):
            raise ValueError("monitor_payload_row_shape")
        level = str(raw.get("level"))
        if level not in verified:
            diagnostics.append(_MONITOR_UNVERIFIED_LEVEL_ROW)
            _count(ROWS_DROPPED_TOTAL, level=level or "unknown",
                   reason=_MONITOR_UNVERIFIED_LEVEL_ROW)
            continue
        if level == "physical_total":
            pool_ref = raw.get("pool_ref")
            warehouse_ref = raw.get("warehouse_ref")
            if not pool_ref or not warehouse_ref:
                diagnostics.append(_MONITOR_ROW_SCOPE_MISSING)
                _count(ROWS_DROPPED_TOTAL, level=level,
                       reason=_MONITOR_ROW_SCOPE_MISSING)
                continue
            scope_ref = f"{pool_ref}|{warehouse_ref}"
            quantity = raw.get("quantity")
        else:
            shop_ref = raw.get("shop_ref")
            if not shop_ref:
                diagnostics.append(_MONITOR_ROW_SCOPE_MISSING)
                _count(ROWS_DROPPED_TOTAL, level=level,
                       reason=_MONITOR_ROW_SCOPE_MISSING)
                continue
            scope_ref = str(shop_ref)
            quantity = raw.get("channel_quantity")
        cell = (level, str(raw.get("sku_ref")), scope_ref)
        if cell in seen:
            raise ValueError("monitor_duplicate_row")
        seen.add(cell)
        rows.append(MonitorAlertRow(
            level=level, status=str(raw.get("inventory_status")),
            sku_ref=str(raw.get("sku_ref")), scope_ref=scope_ref,
            quantity=None if quantity is None else str(quantity),
            threshold=None if raw.get("threshold") is None else str(raw.get("threshold")),
            unit=str(raw.get("unit"))))
    return tuple(rows)


def _connect(settings) -> psycopg.Connection:
    """监控身份的独立连接：bi_monitor 只见获准库存视图、策略与两个函数。"""
    return psycopg.connect(settings.monitor_dsn.get_secret_value())


def run_inventory_monitor(request: MonitorRunRequest, *, settings,
                          now: datetime, deadline: float,
                          registrations: Mapping[str, InventorySourceRegistration]
                          | None = None) -> tuple[AlertTransition, ...]:
    """一个策略的一次监控闭环；失败与缺证据路径全部 fail-closed。"""
    if not settings.enabled:
        return ()
    if registrations is None:
        registrations = verified_inventory_sources()
    if time.monotonic() > deadline:
        _count(SKIPPED_TOTAL, reason="monitor_deadline_exceeded")
        return ()
    conn = _connect(settings)
    try:
        repository = MonitorRepository(conn)
        policy = repository.load_policy(request.policy_ref)
        if not policy.enabled:
            # 记录在案的 policy-disabled/023 张力：commit 对 disabled 策略必然以
            # monitor_policy_disabled 失败。runner 在跑图前跳过，不做不可能的提交。
            _count(SKIPPED_TOTAL, reason="monitor_policy_disabled")
            return ()
        try:
            verified = assert_monitor_sources_verified(policy, registrations)
        except ValueError:
            # 请求层级全部未核验：不跑图、不开事务、不写 outbox、零通知。
            _count(SKIPPED_TOTAL, reason=_MONITOR_SOURCE_UNVERIFIED)
            return ()
        return _run_verified_round(request, policy=policy, verified=verified,
                                   repository=repository, conn=conn,
                                   now=now, deadline=deadline,
                                   service_subject_id=settings.service_subject_id)
    finally:
        conn.close()


def _run_verified_round(request: MonitorRunRequest, *, policy: MonitorPolicy,
                        verified: tuple[str, ...], repository: MonitorRepository,
                        conn, now: datetime, deadline: float,
                        service_subject_id: str) -> tuple[AlertTransition, ...]:
    diagnostics: list[str] = []
    if len(verified) < len(policy.levels):
        # 未核验层级只以 gate 自己的固定码与计数落地；它没有被问过，载荷里
        # 既没有它的行，也没有它的 unsupported 占位行。
        diagnostics.append(_MONITOR_LEVEL_UNVERIFIED)
        for level in policy.levels:
            if level not in verified:
                _count(LEVEL_UNVERIFIED_TOTAL, level=level)

    resolved = _resolve_policy_scope(conn, policy, deadline)
    projection = context_projection_for(policy, verified, resolved)
    store = MemoryQueryRunStore(forbidden_values=frozenset(
        set(resolved.shop_ids) | set(resolved.pool_ids)
        | {policy.owner_subject_id, service_subject_id}))
    root_request_id, tool_call_id = _service_refs(policy.policy_ref, now)
    inspection = InventoryInspectionRequest(
        products="all",
        scope=CommerceScope(),
        levels=list(verified),
        as_of="latest",
        threshold_policy_ref=policy.threshold_policy_ref)
    context = DomainContext(
        subject_id=policy.owner_subject_id,
        allowed_shop_ids=projection.shop_ids,
        shop_refs=projection.shop_refs,
        allowed_inventory_pool_ids=projection.pool_ids,
        conn=conn, store=store,
        chat_id=SERVICE_CHAT_ID, user_message_id=SERVICE_MESSAGE_ID,
        root_request_id=root_request_id,
        now=now, deadline=deadline, attempt_no=1)
    from bi_agent.metrics import _BudgetExhausted

    try:
        run_inventory_graph(request=inspection, context=context,
                            tool_call_id=tool_call_id)
    except (TimeoutError, _BudgetExhausted):
        _count(SKIPPED_TOTAL, reason="monitor_deadline_exceeded")
        return ()
    except Exception:  # noqa: BLE001 - 固定 reason，不带出任何原文
        _count(SKIPPED_TOTAL, reason="monitor_graph_exception")
        return ()

    artifacts = [entry for entry in store.artifacts.values()
                 if entry["artifact_type"] == _SOURCE_ARTIFACT_TYPE]
    if not artifacts:
        _count(SKIPPED_TOTAL, reason="monitor_artifact_missing")
        return ()
    if len(artifacts) > 1:
        _count(SKIPPED_TOTAL, reason="monitor_artifact_ambiguous")
        return ()
    payload = artifacts[0]["payload"]
    if payload.get("status") not in _DECIDABLE_SOURCE_STATUSES:
        _count(SKIPPED_TOTAL, reason="monitor_artifact_status_not_decidable")
        return ()
    try:
        validate_artifact_payload(payload, _SOURCE_ARTIFACT_TYPE)
    except Exception:  # noqa: BLE001 - 契约错误 fail closed，零决策
        _count(SKIPPED_TOTAL, reason="monitor_artifact_payload_invalid")
        return ()

    data_as_of, fresh = _parse_data_as_of(payload, now)
    skipped: list[str] = []
    complete = _complete_levels(policy, verified, payload, diagnostics, skipped)
    for reason in skipped:
        _count(SKIPPED_TOTAL, reason=reason)
    try:
        rows = _scan_rows(verified, payload, diagnostics)
        scan = MonitorScan(
            policy_ref=policy.policy_ref,
            source_artifact_payload=dict(payload),
            source_fingerprint=_canonical_fingerprint(payload),
            data_as_of=data_as_of, fresh=fresh,
            complete_levels=complete, rows=rows,
            diagnostics=tuple(dict.fromkeys(diagnostics)))
        history = repository.load_alert_history(policy.policy_ref)
        from bi_agent.monitoring.state_machine import decide_alert_transitions
        decisions = decide_alert_transitions(scan, policy=policy,
                                             active=history,
                                             verified_levels=verified, now=now)
    except ValueError:
        # 契约错误（重复行、形状不符、上游门禁被绕过）：fail closed——固定 reason、
        # 保持 active alerts 原状、零 outbox；本轮照旧不改写任何已持久化载荷。
        _count(SKIPPED_TOTAL, reason="monitor_contract_error")
        return ()
    return repository.commit_scan(scan, decisions, now=now)
