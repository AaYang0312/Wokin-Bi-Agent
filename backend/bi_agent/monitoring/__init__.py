"""持续库存监控包（计划 2026-09-14-continuous-inventory-notifications.md）。

Task 1 交付严格模型（``models``）与按层级来源 gate（``source_gate``，所有者
2026-09-17 Option A）；Task 2 交付单事务持久化面（``repository``）：
``MonitorRepository`` 只经 023 的 SECURITY DEFINER 函数写库，不接受任意 SQL
或未验证 JSON；Task 3 交付确定性状态机（``state_machine``）：稳定去重键、事
件幂等键与纯函数决策表。runner / CLI 属计划 Task 4，尚不存在；本包不起任何
循环、不做任何外部调用。
"""

from bi_agent.monitoring.models import (
    MONITOR_DIAGNOSTIC_CODES, SCAN_DIAGNOSTIC_CODES, AlertDecision,
    AlertEventKind, AlertStatus, AlertTransition, DeliverySummary,
    MonitorAlertRow, MonitorPolicy, MonitorRunRequest, MonitorScan, StoredAlert)
from bi_agent.monitoring.repository import MonitorRepository
from bi_agent.monitoring.source_gate import (assert_monitor_sources_verified,
                                             verified_monitor_levels)
from bi_agent.monitoring.state_machine import (decide_alert_transitions,
                                               dedupe_key,
                                               event_idempotency_key)

__all__ = [
    "MONITOR_DIAGNOSTIC_CODES", "SCAN_DIAGNOSTIC_CODES", "AlertDecision",
    "AlertEventKind", "AlertStatus", "AlertTransition", "DeliverySummary",
    "MonitorAlertRow", "MonitorPolicy", "MonitorRepository",
    "MonitorRunRequest", "MonitorScan", "StoredAlert",
    "assert_monitor_sources_verified", "decide_alert_transitions",
    "dedupe_key", "event_idempotency_key", "verified_monitor_levels",
]
