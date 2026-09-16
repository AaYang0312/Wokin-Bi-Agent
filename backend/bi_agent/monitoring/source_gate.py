"""按层级的监控来源门禁（计划 Task 1 Step 5；所有者 2026-09-17 Option A 决定）。

fail-closed 的粒度是**层级**：某层的登记、时效策略、完整扫描凭据或生产对账
任一缺失，只把那一层排除在可运行集合之外——不出数、不触发、不解除、不补零、
不替位；策略本身仍记录完整请求层级与全部 opaque scope refs。只有请求层级
**全部**未核验才回到策略级 disabled（``monitor_source_unverified``）。

返回集合有三个消费点，同一个值：runner 交给 graph 的 ``levels`` 与
``DomainContext`` 授权投影（计划 Task 4 Step 3，由同一次 gate 结果派生）与
状态机的 ``verified_levels``（计划 Task 3）。

按层级放宽只改"谁能出数"，不改"登记凭什么算核验"：``CHANNEL_SOURCE_KINDS``
与拼多多排除白名单（``inventory/rules.py``）不变；本模块只读登记表，从不写注册。
"""

from __future__ import annotations

from collections.abc import Mapping

from bi_agent.inventory.rules import InventorySourceRegistration
from bi_agent.monitoring.models import MonitorPolicy


def verified_monitor_levels(
    policy: MonitorPolicy,
    registrations: Mapping[str, InventorySourceRegistration],
) -> tuple[str, ...]:
    """策略请求层级里已核验的那些；其余层级是一等 data_missing，不是零。"""
    verified: list[str] = []
    for level in policy.levels:
        entry = registrations.get(level)
        if (entry is not None and entry.evidence and entry.max_age_seconds > 0
                and entry.scan_complete_supported
                and entry.production_reconciled_at is not None):
            verified.append(level)
    return tuple(verified)


def assert_monitor_sources_verified(
    policy: MonitorPolicy,
    registrations: Mapping[str, InventorySourceRegistration],
) -> tuple[str, ...]:
    """有已核验层时原样返回可运行集合；请求层级全空时策略 disabled 退出。"""
    verified = verified_monitor_levels(policy, registrations)
    if not verified:
        # 请求层级一个都没核验：整条策略 disabled 退出，不产生行也不产生通知。
        raise ValueError("monitor_source_unverified")
    return verified
