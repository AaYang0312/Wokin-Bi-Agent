"""outbox 的 at-least-once 投递 wrapper（计划 Task 5 Step 3）。

这里只有一条固定 SQL——``SELECT bi.deliver_inventory_outbox(%(now)s,
%(limit)s)``（023 的 SECURITY DEFINER 投递函数）：选中到期行、插入应用内
通知（幂等键 ``ON CONFLICT DO NOTHING``）、推进 delivered_at/attempts、单行
失败退避与 20 次 dead-letter 全部在数据库一侧。wrapper 只负责调用纪律：

- ``limit`` 必须落在 1..100（计划 Task 2 的 outbox batch 上限）；
- ``now`` 必须带时区（投递时点不许依赖服务器本地时区）；
- 整个调用在一个事务里，失败即整体回滚（通知与 outbox 状态一起消失）；
- 函数结果逐字验证进 :class:`DeliverySummary`，缺键/多键/越界一律拒绝；
- 数据库失败折叠成固定非秘密错误码，原文、参数与 DSN 不进异常消息。

至少一次语义、恰好一份通知与崩溃重试由 023 的函数与幂等键承载
（tests.test_db 另证主矩阵；tests.test_monitoring 证明 wrapper 入口）。
本模块没有第二条 SQL 通路，也不接任何外部 sink——邮件、短信、webhook 一律
不存在；Task 4 的 ``bi-inventory-outbox`` CLI 保持一次性、默认关闭。
"""

from __future__ import annotations

from datetime import datetime

import psycopg
from pydantic import ValidationError

from bi_agent.monitoring.models import DeliverySummary

# 与计划 Task 5 Step 3 的固定调用逐字一致：没有第二条 SQL 形状。
_DELIVER_SQL = "SELECT bi.deliver_inventory_outbox(%(now)s, %(limit)s)"

_LIMIT_BOUNDS = (1, 100)


def deliver_inventory_outbox(conn: psycopg.Connection, *, now: datetime,
                             limit: int = 100) -> DeliverySummary:
    """一个事务内调用一次固定投递函数，返回严格验证过的计数结果。

    调用方输入问题（limit 越界、naive now）以 ``ValueError`` 的固定码拒绝，
    且不碰数据库；数据库与结果形状问题折叠成 ``RuntimeError`` 的固定码。
    """
    if isinstance(limit, bool) or not isinstance(limit, int) \
            or not _LIMIT_BOUNDS[0] <= limit <= _LIMIT_BOUNDS[1]:
        raise ValueError("monitor_delivery_limit_out_of_range")
    if not isinstance(now, datetime) or now.tzinfo is None \
            or now.tzinfo.utcoffset(now) is None:
        raise ValueError("monitor_delivery_now_naive")
    with conn.transaction():
        try:
            row = conn.execute(_DELIVER_SQL, {"now": now, "limit": limit}).fetchone()
        except psycopg.errors.Error as error:
            # 固定码，不带数据库原文；原始异常保留在 __cause__ 供日志侧使用。
            raise RuntimeError("monitor_delivery_failed") from error
    if row is None:
        raise RuntimeError("monitor_delivery_result_invalid")
    try:
        return DeliverySummary.model_validate(row[0])
    except ValidationError as error:
        raise RuntimeError("monitor_delivery_result_invalid") from error
