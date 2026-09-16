"""确定性告警状态机与稳定去重键（计划 2026-09-14-continuous-inventory-notifications.md
Task 3）。

纯函数：不读数据库、不碰网络、不读文件、无全局可变状态、无随机性；同一输入
永远得到同一决策序列，且与行/告警的输入顺序无关。层级门禁
（``verified_levels``）是必需关键字输入，不是调用方的约定：
runnable = ``policy.levels ∩ verified_levels``。

决策表（层级为外层循环，fail-closed 的粒度是层级）。``active`` 是该策略的
**全生命周期**告警历史（含 resolved/suppressed；Task 4 经
``MonitorRepository.load_alert_history`` 读出）：终端行只参与
``max(generation)+1`` 与含糊历史检测，任何决策（抑制、保留、匹配、冷却、
策略停用）都只落在 open/acknowledged 上——终端历史自己绝不拿决策：

- ``policy.enabled=False``：只对 active 告警产出 suppressed/updated/notify=false/
  observed=false，不处理任何行、不触发、不解除。
- 层级不在 ``verified_levels``：曾经核验、本轮退场——该层 active 告警走
  suppressed（updated、notify=false、observed=false，永不 resolved）。该层的行
  即使被人为塞进 ``scan.rows``（第二道闸被绕过）也只被丢弃：不处理、不补零、
  不解除，也永不牵连其它已核验层级（丢弃整轮才是错的方向）。
- 层级已核验但 ``not scan.fresh`` 或不在 ``complete_levels``：缺页/过期/本轮无
  行——该层 active 告警保留原状态（updated、notify=false、observed=false），不
  推进 ``last_observed_at``，永不 resolved；该层的行不参与触发。
- 层级已核验且完整：逐行决策。``low`` 且 Decimal 复核 ``quantity <= threshold``
  成立才触发/冷却重触发/持续更新；``normal`` 且同层同格有 active 告警才
  resolved；其余状态（unknown/unconfigured/stale/data_anomaly/unsupported）永
  不触发也永不解除，有 active 告警时保留原状态且 observed=false——"没读到"
  不是"刚看到"，不能把下一轮 stale 周期变成永不报警。

dedupe key 只由 ``[policy_version, level, sku_ref, scope_ref, rule_code]`` 的
canonical JSON 计算；数量、阈值、时间与显示名一律不参与。``rule_code`` 是状态
机的固定常量（首版只有阈值规则，按层级取 ``THRESHOLD_LEVEL_BY_LEVEL``），不从
行里读。事件幂等键 = ``sha256(key:generation:event_kind:source_fingerprint)``，
与 023 ``commit_inventory_monitor_scan`` 同一公式。

失败模式全部 fail closed 且与顺序无关：重复扫描行、重复 active 键、同一
``(dedupe_key, generation)`` 的含糊历史、naive 时间、scan/policy 引用不一致都
以固定 ``monitor_`` 错误码当场拒绝，绝不按"先到先得"解释输入。
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta

from bi_agent.inventory.rules import (AUDIT_LEVELS, POOL_REF_RE, STATUS_LOW,
                                      STATUS_NORMAL, THRESHOLD_LEVEL_BY_LEVEL,
                                      WAREHOUSE_REF_RE, normalize_quantity)
from bi_agent.monitoring.models import (AlertDecision, AlertEventKind, AlertStatus,
                                        MonitorAlertRow, MonitorPolicy, MonitorScan,
                                        StoredAlert)

# 与 023 的决策校验同一份闭集形状：键输入先验形状，再进哈希。
_POLICY_VERSION_RE = re.compile(r"^inventory-monitor/[0-9][0-9._-]{0,31}$")
# SKU/店铺 ref：目录单向引用 `ent-` + 8 位小写十六进制（SQL 侧 d_sku 同一形状）。
_ENTITY_REF_RE = re.compile(r"^ent-[0-9a-z]{8}$")
_RULE_CODE_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
_FINGERPRINT_RE = re.compile(r"^[0-9a-f]{64}$")
# 作用域引用只能来自该行自己的层级：实物 = pool_ref|warehouse_ref 的 canonical 对
# （graph 真实物格本来就两个都带），渠道 = shop_ref。
_PHYSICAL_SCOPE_RE = re.compile(
    rf"{POOL_REF_RE.pattern.strip('^$')}\|{WAREHOUSE_REF_RE.pattern.strip('^$')}")

_EVENT_KINDS: tuple[str, ...] = ("triggered", "retriggered", "updated", "resolved")
# 决策只能落在活跃状态上；resolved/suppressed 历史行绝不拿决策。
_ACTIVE_STATUSES: frozenset[str] = frozenset({"open", "acknowledged"})


def _scope_ref_is_valid(level: str, scope_ref: str) -> bool:
    if level == "physical_total":
        return _PHYSICAL_SCOPE_RE.fullmatch(scope_ref) is not None
    return _ENTITY_REF_RE.fullmatch(scope_ref) is not None


def dedupe_key(*, policy_version: str, level: str, sku_ref: str,
               scope_ref: str, rule_code: str) -> str:
    """稳定去重键：canonical JSON 五元组的 SHA-256；输入形状不对当场拒绝。

    只由策略版本、库存层级、SKU ref、opaque 作用域 ref 和规则码组成；真实 ID、
    显示名、数量、阈值与时间都不在这五个入参里，因此结构上进不了键。
    """
    if _POLICY_VERSION_RE.fullmatch(policy_version) is None:
        raise ValueError("monitor_dedupe_policy_version_invalid")
    if level not in AUDIT_LEVELS:
        raise ValueError("monitor_dedupe_level_invalid")
    if _ENTITY_REF_RE.fullmatch(sku_ref) is None:
        raise ValueError("monitor_dedupe_sku_ref_invalid")
    if not scope_ref or not _scope_ref_is_valid(level, scope_ref):
        raise ValueError("monitor_dedupe_scope_ref_invalid")
    if _RULE_CODE_RE.fullmatch(rule_code) is None:
        raise ValueError("monitor_dedupe_rule_code_invalid")
    payload = [policy_version, level, sku_ref, scope_ref, rule_code]
    encoded = json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def event_idempotency_key(key: str, generation: int, event_kind: str,
                          source_fingerprint: str) -> str:
    """事件幂等键：与 023 `sha256(d_key:generation:event_kind:fingerprint)` 同一公式。

    同一次扫描的崩溃重放算出同一个键：事件、首发与 outbox 都只落一次。
    """
    if _FINGERPRINT_RE.fullmatch(key) is None:
        raise ValueError("monitor_event_key_invalid")
    if not isinstance(generation, int) or isinstance(generation, bool) or generation < 1:
        raise ValueError("monitor_event_generation_invalid")
    if event_kind not in _EVENT_KINDS:
        raise ValueError("monitor_event_kind_invalid")
    if _FINGERPRINT_RE.fullmatch(source_fingerprint) is None:
        raise ValueError("monitor_event_fingerprint_invalid")
    material = f"{key}:{generation}:{event_kind}:{source_fingerprint}"
    return hashlib.sha256(material.encode()).hexdigest()


def _row_key(policy: MonitorPolicy, row: MonitorAlertRow) -> str:
    return dedupe_key(policy_version=policy.policy_ref, level=row.level,
                      sku_ref=row.sku_ref, scope_ref=row.scope_ref,
                      rule_code=THRESHOLD_LEVEL_BY_LEVEL[row.level])


def _row_is_low(row: MonitorAlertRow) -> bool:
    """复用 inventory 的 Decimal 规则：`quantity <= threshold` 才是低。

    行的 ``status`` 是 graph 的判定；状态机仍用 Decimal 独立复核一遍——复核不
    成立（数量不可解析、单位未登记、数量为负）就不触发：误触发比漏发更不可逆，
    且这些格在规则层本来就是 unknown/data_anomaly，不是"低"。
    """
    if row.status != STATUS_LOW:
        return False
    quantity = normalize_quantity(row.quantity, row.unit)
    threshold = normalize_quantity(row.threshold, row.unit)
    if quantity is None or threshold is None or quantity < 0:
        return False
    return quantity <= threshold


def _reject_ambiguous_inputs(scan: MonitorScan,
                             active: tuple[StoredAlert, ...]) -> None:
    """重复行 / 重复 active 键 / 含糊历史 / naive 时间：一律 fail closed。"""
    seen_cells: set[tuple[str, str, str]] = set()
    for row in scan.rows:
        cell = (row.level, row.sku_ref, row.scope_ref)
        if cell in seen_cells:
            # 同一格两行不是"取一个"，是冲突；任选其一就是顺序依赖。
            raise ValueError("monitor_duplicate_row")
        seen_cells.add(cell)
    seen_active: set[str] = set()
    seen_history: set[tuple[str, int]] = set()
    for alert in active:
        if alert.status in _ACTIVE_STATUSES:
            # 023 的 partial unique index 保证一格最多一条 active；看到两条
            # 意味着输入已坏，不能决定听谁的。
            if alert.dedupe_key in seen_active:
                raise ValueError("monitor_duplicate_active_alert")
            seen_active.add(alert.dedupe_key)
        history = (alert.dedupe_key, alert.generation)
        if history in seen_history:
            # UNIQUE(dedupe_key, generation) 的违例形状：历史含糊，拒绝判定。
            raise ValueError("monitor_ambiguous_history")
        seen_history.add(history)
        for stamp in (alert.last_observed_at, alert.last_notified_at):
            if stamp is not None and stamp.tzinfo is None:
                raise ValueError("monitor_alert_timestamp_naive")


def _history_generations(active: tuple[StoredAlert, ...]) -> dict[str, int]:
    """每格已知的最大 generation（含 resolved/suppressed 历史）：新首发 = max+1。

    与 023 的 ``history_generation + 1`` 同一口径，"恢复后再次下降"因此落在新
    generation 上，而不是复活旧告警。
    """
    generations: dict[str, int] = {}
    for alert in active:
        generations[alert.dedupe_key] = max(generations.get(alert.dedupe_key, 0),
                                            alert.generation)
    return generations


def _cooldown_elapsed(current: StoredAlert, policy: MonitorPolicy,
                      now: datetime) -> bool:
    if current.last_notified_at is None:
        # 从未通知过（理论上有审计路径）：冷却视为已过，retrigger 是有信息量的动作。
        return True
    return now - current.last_notified_at >= timedelta(seconds=policy.cooldown_seconds)


def _decision(key: str, generation: int, previous_status: AlertStatus | None,
              next_status: AlertStatus, event_kind: AlertEventKind, *, row: MonitorAlertRow,
              notify: bool, observed: bool) -> AlertDecision:
    return AlertDecision(
        dedupe_key=key, generation=generation, previous_status=previous_status,
        next_status=next_status, event_kind=event_kind, notify=notify,
        observed=observed, rule_code=THRESHOLD_LEVEL_BY_LEVEL[row.level],
        level=row.level, sku_ref=row.sku_ref, scope_ref=row.scope_ref)


def _suppress(alert: StoredAlert) -> AlertDecision:
    """策略停用/层级退场：suppressed、updated、notify=false、observed=false。

    身份逐字来自 StoredAlert（023 行本就带 level/sku_ref/scope_ref），退场绝不
    写成 resolved，也绝不写 outbox（updated+notify=false 只落审计事件）。
    """
    return AlertDecision(
        dedupe_key=alert.dedupe_key, generation=alert.generation,
        previous_status=alert.status, next_status="suppressed", event_kind="updated",
        notify=False, observed=False,
        rule_code=THRESHOLD_LEVEL_BY_LEVEL[alert.level], level=alert.level,
        sku_ref=alert.sku_ref, scope_ref=alert.scope_ref)


def _retain(alert: StoredAlert) -> AlertDecision:
    """缺页/过期/截断的保留：updated、notify=false、observed=false（不推进观测）。"""
    return AlertDecision(
        dedupe_key=alert.dedupe_key, generation=alert.generation,
        previous_status=alert.status, next_status=alert.status, event_kind="updated",
        notify=False, observed=False,
        rule_code=THRESHOLD_LEVEL_BY_LEVEL[alert.level], level=alert.level,
        sku_ref=alert.sku_ref, scope_ref=alert.scope_ref)


def _decide_row(policy: MonitorPolicy, row: MonitorAlertRow,
                open_by_key: dict[str, StoredAlert],
                history_generation: dict[str, int], now: datetime) -> AlertDecision | None:
    key = _row_key(policy, row)
    current = open_by_key.get(key)   # key 内含 level：同键即同层，无需再比
    if _row_is_low(row):
        if current is None:
            # 首发或恢复后再次下降：generation = 已知历史最大代 + 1。
            generation = history_generation.get(key, 0) + 1
            return _decision(key, generation, None, "open", "triggered", row=row,
                             notify=True, observed=True)
        if _cooldown_elapsed(current, policy, now):
            # 冷却边界（含恰好等于）之后的持续异常：retriggered，重新通知一次。
            return _decision(key, current.generation, current.status, current.status,
                             "retriggered", row=row, notify=True, observed=True)
        # 相同事实重复扫描：只更新 last_observed_at，不重复通知。
        return _decision(key, current.generation, current.status, current.status,
                         "updated", row=row, notify=False, observed=True)
    if current is None:
        # 无 active 告警且不低：安全格不需要任何决策，也不给"都安全"发通知。
        return None
    if row.status == STATUS_NORMAL:
        # 解除必须同层、已核验、fresh 且完整，且这一格真的读到了 normal。
        return _decision(key, current.generation, current.status, "resolved",
                         "resolved", row=row, notify=True, observed=True)
    # unknown/unconfigured/stale/data_anomaly/unsupported：没读到数据——不触发、
    # 不解除、不推进 last_observed_at。
    return _decision(key, current.generation, current.status, current.status,
                     "updated", row=row, notify=False, observed=False)


def decide_alert_transitions(
        scan: MonitorScan, *, policy: MonitorPolicy,
        active: tuple[StoredAlert, ...], verified_levels: tuple[str, ...],
        now: datetime) -> tuple[AlertDecision, ...]:
    """一次扫描 → 一组彼此不重复的确定性决策（与输入顺序无关）。

    ``active`` 是该策略的全生命周期告警历史（``MonitorRepository.load_alert_history``
    的返回值）：resolved/suppressed 行只给 ``max(generation)+1`` 与含糊历史用，
    决策本身只落在 open/acknowledged 上。``verified_levels`` 是必需关键字：层级
    门禁是状态机的输入。输出按 ``policy.levels`` 顺序（层内按 ref/键排序）拼接，
    每格至多一条决策，键互不重复——023 与 repository 对重复键的拒绝因此永远
    不会被状态机触发。
    """
    if now.tzinfo is None:
        raise ValueError("monitor_now_naive")
    if scan.policy_ref != policy.policy_ref:
        # dedupe key 的 policy_version 成员来自 policy；两者不一致时键没有意义。
        raise ValueError("monitor_policy_ref_mismatch")
    for level in verified_levels:
        if level not in AUDIT_LEVELS:
            raise ValueError("monitor_verified_level_unrecognized")
    _reject_ambiguous_inputs(scan, active)

    if not policy.enabled:
        # 策略停用/撤销：只对仍活跃（open/acknowledged）的告警产出退场审计，不
        # 处理行、不触发、不解除；resolved/suppressed 历史行只参与代际，绝不拿决策。
        return tuple(
            _suppress(alert) for alert in sorted(
                (alert for alert in active if alert.status in _ACTIVE_STATUSES),
                key=lambda alert: alert.dedupe_key))

    runnable = set(verified_levels) & set(policy.levels)
    open_by_key = {alert.dedupe_key: alert for alert in active
                   if alert.status in _ACTIVE_STATUSES}
    history_generation = _history_generations(active)
    rows_by_level: dict[str, list[MonitorAlertRow]] = {level: [] for level in policy.levels}
    for row in scan.rows:
        # 第二道闸：正常路径上 runner 已把 scan.rows 收窄到 verified_levels；
        # 仍有 runnable 之外的行到达 ⇒ 上游门禁被绕过——只丢那一层的行。
        if row.level in rows_by_level and row.level in runnable:
            rows_by_level[row.level].append(row)

    decisions: list[AlertDecision] = []
    for level in policy.levels:
        level_alerts = sorted(
            (alert for alert in active
             if alert.level == level and alert.status in _ACTIVE_STATUSES),
            key=lambda alert: alert.dedupe_key)
        if level not in verified_levels:
            # 该层没有已核验来源：曾经核验过的 active 告警只能解释为退场，走
            # suppressed；未核验过的层根本不会有行可触发。
            decisions.extend(_suppress(alert) for alert in level_alerts)
            continue
        if not scan.fresh or level not in scan.complete_levels:
            # 缺页、过期、本轮无行：保留原状态与 last_observed_at，永不 resolved。
            decisions.extend(_retain(alert) for alert in level_alerts)
            continue
        for row in sorted(rows_by_level[level],
                          key=lambda item: (item.sku_ref, item.scope_ref)):
            decision = _decide_row(policy, row, open_by_key, history_generation, now)
            if decision is not None:
                decisions.append(decision)
    return tuple(decisions)


__all__ = ["decide_alert_transitions", "dedupe_key", "event_idempotency_key"]
