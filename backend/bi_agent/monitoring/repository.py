"""监控 repository：单事务提交与最小读取面（计划 Task 2）。

这里只有四个方法：``load_policy``、``load_alert_history``（全生命周期，状态机
输入面）、``load_active_alerts``（旧名，只 open/acknowledged）、
``commit_scan``。
``commit_scan`` 先用 ``NewArtifact`` / ``validate_artifact_payload`` /
``allows_artifact_type`` / ``AlertDecision`` 重验输入（不接受的 JSON 一律在碰
库之前被拒），再在一个 ``conn.transaction()`` 里只调用唯一一条固定 SQL——
``SELECT bi.commit_inventory_monitor_scan(...)``（023 的 SECURITY DEFINER
函数）：run/artifact/告警/事件/outbox 的原子性与 advisory 锁全部在数据库一侧。
数据库失败映射成固定非秘密错误码（``monitor_*``），错误原文、参数与 DSN 不进
异常消息。状态机、runner、投递与 API 属计划 Task 3–5，不在这里。
"""

from __future__ import annotations

import re

import psycopg
from pydantic import BaseModel
from psycopg.types.json import Jsonb

from bi_agent.monitoring.models import (AlertDecision, AlertTransition,
                                        MonitorPolicy, MonitorScan, StoredAlert)
from bi_agent.runtime.domain_registry import allows_artifact_type
from bi_agent.runtime.models import NewArtifact, validate_artifact_payload

# monitor 运行只属于 inventory_watch 领域，来源 Artifact 只能是 inventory_alerts。
_MONITOR_DOMAIN = "inventory_watch"
_SOURCE_ARTIFACT_TYPE = "inventory_alerts"
_DECISION_BUDGET = 500

# 与计划 Task 3 Step 3 同一形状：决策键必须已是 64 位十六进制去重键。
_DEDUPE_KEY_RE = re.compile(r"^[0-9a-f]{64}$")

# 提交函数的全部固定失败码；不在表里的任何数据库错误都折叠成
# monitor_commit_failed——错误原文永不外泄。
STABLE_COMMIT_FAILURES: frozenset[str] = frozenset({
    "monitor_policy_not_found", "monitor_policy_disabled",
    "monitor_service_chat_missing", "monitor_source_payload_unsafe",
    "monitor_invalid_decision", "monitor_decision_budget_exceeded",
    "monitor_invalid_transition", "monitor_lock_unavailable",
    "monitor_run_write_failed", "monitor_artifact_write_failed",
    "monitor_alert_write_failed", "monitor_event_write_failed",
    "monitor_outbox_write_failed",
})

_POLICY_SQL = (
    "SELECT policy_ref, threshold_policy_ref, owner_subject_id, shop_refs, "
    "inventory_pool_refs, levels, cooldown_seconds, enabled "
    "FROM bi.inventory_monitor_policies WHERE policy_ref = %s")
_ACTIVE_ALERTS_SQL = (
    "SELECT alert_ref, dedupe_key, generation, status, level, sku_ref, scope_ref, "
    "last_observed_at, last_notified_at FROM bi.inventory_alert_instances "
    "WHERE policy_ref = %s AND status IN ('open','acknowledged') "
    "ORDER BY alert_ref")
_ALERT_HISTORY_SQL = (
    "SELECT alert_ref, dedupe_key, generation, status, level, sku_ref, scope_ref, "
    "last_observed_at, last_notified_at FROM bi.inventory_alert_instances "
    "WHERE policy_ref = %s ORDER BY alert_ref")
_COMMIT_SQL = "SELECT bi.commit_inventory_monitor_scan(%s, %s, %s, %s, %s, %s)"


def _stable_failure(error: Exception, fallback: str) -> RuntimeError:
    """数据库错误 → 固定非秘密错误码；diagnostic 原文不进异常消息。"""
    message = getattr(getattr(error, "diag", None), "message_primary", None) \
        or str(error)
    if message in STABLE_COMMIT_FAILURES:
        return RuntimeError(message)
    return RuntimeError(fallback)


def _validated_source(scan: MonitorScan) -> dict[str, object]:
    """来源载荷重新走一遍公开契约：类型、领域白名单与载荷形状各挡一次。"""
    try:
        validate_artifact_payload(scan.source_artifact_payload,
                                  _SOURCE_ARTIFACT_TYPE)
        artifact = NewArtifact(artifact_type=_SOURCE_ARTIFACT_TYPE,
                               payload=scan.source_artifact_payload,
                               data_as_of=scan.data_as_of)
    except Exception as error:   # noqa: BLE001 - pydantic/契约错误统一折叠
        raise ValueError("monitor_source_payload_unsafe") from error
    if not allows_artifact_type(_MONITOR_DOMAIN, artifact.artifact_type):
        raise ValueError("monitor_artifact_type_not_allowed")
    return artifact.payload


def _decision_entries(scan: MonitorScan,
                      decisions: tuple[AlertDecision, ...]) -> list[dict[str, object]]:
    """决策重验并补齐本行展示字段：键集与 023 的 SQL 侧校验精确对齐。"""
    rows_by_cell = {(row.level, row.sku_ref, row.scope_ref): row
                    for row in scan.rows}
    entries: list[dict[str, object]] = []
    seen: set[str] = set()
    for decision in decisions:
        # 经 model_dump 重建后再验：pydantic 默认不重验模型实例，绕过构造器的
        # 坏实例（事件码被改等）也要在这一步被拒，而不是带进数据库。
        raw = decision.model_dump(warnings=False) if isinstance(decision, BaseModel) \
            else decision
        try:
            validated = AlertDecision.model_validate(raw)
        except Exception as error:   # noqa: BLE001
            raise ValueError("monitor_invalid_decision") from error
        if not _DEDUPE_KEY_RE.fullmatch(validated.dedupe_key) \
                or validated.dedupe_key in seen \
                or not validated.sku_ref or not validated.scope_ref:
            raise ValueError("monitor_invalid_decision")
        seen.add(validated.dedupe_key)
        row = rows_by_cell.get(
            (validated.level, validated.sku_ref, validated.scope_ref))
        entries.append({
            "dedupe_key": validated.dedupe_key,
            "generation": validated.generation,
            "previous_status": validated.previous_status,
            "next_status": validated.next_status,
            "event_kind": validated.event_kind,
            "notify": validated.notify,
            "observed": validated.observed,
            "rule_code": validated.rule_code,
            "level": validated.level,
            "sku_ref": validated.sku_ref,
            "scope_ref": validated.scope_ref,
            # 行上没有的展示字段保持 null：缺扫描/退场保留的 updated 不补数。
            "quantity": row.quantity if row is not None else None,
            "threshold": row.threshold if row is not None else None,
            "unit": row.unit if row is not None else None,
        })
    if len(entries) > _DECISION_BUDGET:
        raise ValueError("monitor_decision_budget_exceeded")
    return entries


class MonitorRepository:
    """bi_monitor 连接上的监控持久化面：没有任何任意 SQL 或自由 JSON 入口。"""

    def __init__(self, conn: psycopg.Connection) -> None:
        self.conn = conn

    def load_policy(self, policy_ref: str) -> MonitorPolicy:
        """已批准策略原样读出（含 enabled=False）：收窄是 gate 的事，不是存储的。"""
        try:
            row = self.conn.execute(_POLICY_SQL, (policy_ref,)).fetchone()
        except psycopg.errors.Error as error:
            raise _stable_failure(error, "monitor_policy_read_failed") from error
        if row is None:
            raise ValueError("monitor_policy_not_found")
        try:
            return MonitorPolicy(
                policy_ref=row[0], threshold_policy_ref=row[1],
                owner_subject_id=row[2], shop_refs=tuple(row[3]),
                inventory_pool_refs=tuple(row[4]), levels=tuple(row[5]),
                cooldown_seconds=row[6], enabled=row[7])
        except Exception as error:   # noqa: BLE001
            raise ValueError("monitor_policy_row_invalid") from error

    def load_alert_history(self, policy_ref: str) -> tuple[StoredAlert, ...]:
        """该策略的**全生命周期**告警（含 resolved/suppressed）：状态机输入面。

        023 的首发校验要求 ``generation = max(全部历史)+1``（``history_generation
        + 1``），所以恢复后再次下降必须让状态机看到终端历史行，否则会算出
        generation 1 而整个提交以 ``monitor_invalid_transition`` 失败。终端行
        只参与代际与含糊历史；抑制/保留/匹配/冷却与策略停用决策由状态机自行
        收窄到 open/acknowledged（``decide_alert_transitions``）。
        """
        return self._load_alert_rows(_ALERT_HISTORY_SQL, policy_ref)

    def load_active_alerts(self, policy_ref: str) -> tuple[StoredAlert, ...]:
        """旧名保留：只回 open/acknowledged（Task 2 契约形状不变）。

        它不是状态机的输入面——全历史请用 ``load_alert_history``（计划 Task 4
        的 runner 必须把那里的返回值交给 ``decide_alert_transitions``），否则
        resolved→再次下降的首发永远停在 generation 1，提交会被 SQL 侧拒绝。
        """
        return self._load_alert_rows(_ACTIVE_ALERTS_SQL, policy_ref)

    def _load_alert_rows(self, sql: str,
                         policy_ref: str) -> tuple[StoredAlert, ...]:
        try:
            rows = self.conn.execute(sql, (policy_ref,)).fetchall()
        except psycopg.errors.Error as error:
            raise _stable_failure(error, "monitor_alerts_read_failed") from error
        alerts: list[StoredAlert] = []
        for row in rows:
            try:
                alerts.append(StoredAlert(
                    alert_ref=row[0], dedupe_key=row[1], generation=row[2],
                    status=row[3], level=row[4], sku_ref=row[5], scope_ref=row[6],
                    last_observed_at=row[7], last_notified_at=row[8]))
            except Exception as error:   # noqa: BLE001
                raise ValueError("monitor_alert_row_invalid") from error
        return tuple(alerts)

    def commit_scan(self, scan: MonitorScan,
                    decisions: tuple[AlertDecision, ...], *,
                    now) -> tuple[AlertTransition, ...]:
        """一次扫描 = 一个事务 = 一次函数调用；任一步失败五类行整批回滚。"""
        payload = _validated_source(scan)
        entries = _decision_entries(scan, decisions)
        with self.conn.transaction():
            try:
                row = self.conn.execute(
                    _COMMIT_SQL,
                    (scan.policy_ref, Jsonb(payload), scan.source_fingerprint,
                     scan.data_as_of, Jsonb(entries), now)).fetchone()
            except psycopg.errors.Error as error:
                raise _stable_failure(error, "monitor_commit_failed") from error
        if row is None:
            raise RuntimeError("monitor_commit_failed")
        result = row[0]
        artifact_ref = str(result["artifact_id"])
        return tuple(
            AlertTransition(
                alert_ref=transition["alert_ref"],
                previous_status=transition["previous_status"],
                next_status=transition["next_status"],
                event_kind=transition["event_kind"],
                dedupe_key=transition["dedupe_key"],
                source_artifact_ref=artifact_ref)
            for transition in result["transitions"])
