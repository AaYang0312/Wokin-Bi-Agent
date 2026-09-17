"""一次性监控 CLI（计划 Task 4 Step 5）：`bi-inventory-monitor` / `bi-inventory-outbox`。

两条入口都只做一次就退出：没有 daemon、loop、HTTP server 或任何常驻参数——
命令不接受自由参数，调度只由部署主机的计划任务反复调用本进程（本轮不注册）。
`INVENTORY_MONITOR_ENABLED=false`（默认）时打印一行安全文案并以 0 退出，不连
任何数据库、不装载任何秘密；enabled 时逐 policy 共享 30 秒总 deadline，任一
policy 失败以退出码 1 结束，输出只含 policy ref 与固定错误码（不带 DSN、不
带证据、不带数量）。投递入口只调用 023 的固定 SECURITY DEFINER 投递函数。
"""

from __future__ import annotations

import os
import sys
import time
from datetime import datetime, timezone
from typing import Sequence

from bi_agent.config import load_monitor_settings
from bi_agent.monitoring.models import DeliverySummary, MonitorRunRequest
from bi_agent.monitoring.runner import _connect, run_inventory_monitor

_DISABLED_LINE = "inventory_monitor disabled"
_OUTBOX_DISABLED_LINE = "inventory_outbox disabled"

# 023 的固定投递函数：at-least-once、幂等键去重、批量上限 100。
_DELIVER_SQL = "SELECT bi.deliver_inventory_outbox(%s, %s)"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _emit(line: str) -> None:
    print(line, flush=True)


def run_main(argv: Sequence[str] | None = None) -> int:
    """一次性监控入口：disabled 单行退出；enabled 逐策略、共享总 deadline。"""
    del argv,  # 命令不接受任何参数：没有 loop/server 的入口形状可以构造。
    try:
        settings = load_monitor_settings(dict(os.environ))
    except ValueError as error:
        # loader 的错误全部是固定码；不带 DSN、不带证据（测试已钉）。
        _emit(f"inventory_monitor config_error code={error}")
        return 1
    if not settings.enabled:
        _emit(_DISABLED_LINE)
        return 0
    deadline = time.monotonic() + settings.run_deadline_seconds
    now = _utc_now()
    exit_code = 0
    for policy_ref in settings.policy_refs[:settings.max_policies]:
        try:
            request = MonitorRunRequest(policy_ref=policy_ref)
            result = run_inventory_monitor(request, settings=settings, now=now,
                                           deadline=deadline)
        except (ValueError, RuntimeError) as error:
            # 只输出 policy ref 与固定错误码：DSN、证据与数量永不进输出。
            _emit(f"inventory_monitor failed policy_ref={policy_ref} "
                  f"code={error}")
            exit_code = 1
            continue
        except Exception as error:  # noqa: BLE001 - 任一 policy 异常都折叠成
            # 固定码单行（计划 Step 5）：monitor DB 不可达的 psycopg 错误、
            # deadline 的 TimeoutError 等非稳定错误只带异常类型名，错误原文
            # （可能含本地细节）一律不带出；失败不中断剩余策略。
            _emit(f"inventory_monitor failed policy_ref={policy_ref} "
                  f"code=monitor_run_failed {type(error).__name__}")
            exit_code = 1
            continue
        _emit(f"inventory_monitor policy_ref={policy_ref} ok "
              f"transitions={len(result)}")
    return exit_code


def deliver_main(argv: Sequence[str] | None = None) -> int:
    """一次性投递入口：只调用 023 的固定投递函数，幂等且限量 100。"""
    del argv,  # 同上：无参数、无常驻形态。
    try:
        settings = load_monitor_settings(dict(os.environ))
    except ValueError as error:
        _emit(f"inventory_outbox config_error code={error}")
        return 1
    if not settings.enabled:
        _emit(_OUTBOX_DISABLED_LINE)
        return 0
    try:
        conn = _connect(settings)
    except Exception as error:  # noqa: BLE001 - 折叠成固定码，不带出 DSN
        _emit(f"inventory_outbox failed code=monitor_connect_failed {type(error).__name__}")
        return 1
    try:
        row = conn.execute(_DELIVER_SQL, (_utc_now(), 100)).fetchone()
        summary = DeliverySummary.model_validate(dict(row[0]))
    except (ValueError, RuntimeError) as error:
        conn.close()
        _emit(f"inventory_outbox failed code={error}")
        return 1
    except Exception as error:  # noqa: BLE001 - 折叠成固定码
        conn.close()
        _emit(f"inventory_outbox failed code=monitor_delivery_failed "
              f"{type(error).__name__}")
        return 1
    conn.close()
    _emit(f"inventory_outbox selected={summary.selected} "
          f"delivered={summary.delivered} retried={summary.retried} "
          f"dead_lettered={summary.dead_lettered}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(run_main())
