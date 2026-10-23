"""测试库连接：显式外层事务 + 硬性安全边界。

被测代码（sync_shops / sync_products / sync_window 等）内部会自己开
`conn.transaction()`。psycopg3 只有在**已存在显式事务**时才把嵌套调用降级成
SAVEPOINT；在隐式事务下它会真的 BEGIN/COMMIT，于是测试假数据永久留在共享测试库里。
这不只是脏：假商品档案带着未来的 `source_modified_at` 落库后，版本守卫会一直拒收
真接口的刷新，真数据再也进不来。

所以测试库连接统一从这里开，外层事务在 case 结束时按 Rollback 协议退出。
"""

from __future__ import annotations

import os
from typing import Any

import psycopg

LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}

# reporting.v_product_daily 的列序是契约（视图带名称列，列序一变投影就可能错装）。
# 三处断言共用这一份，避免基线 / 005 / 007 形状各抄一遍后互相漂移。
PRODUCT_DAILY_BASE_COLUMNS = [
    "shop_id", "day", "product_id", "quantity", "gift_quantity",
    "product_paid_amount", "allocation_verified", "line_kind",
]
PRODUCT_DAILY_COLUMNS_AFTER_005 = PRODUCT_DAILY_BASE_COLUMNS + ["product_name"]
PRODUCT_DAILY_COLUMNS = PRODUCT_DAILY_BASE_COLUMNS + [
    "product_name", "product_name_snapshot", "sku_label",
]


def assert_isolated_test_target(conn: psycopg.Connection) -> None:
    """连接身份硬边界：只认本机 ``*_test`` 库；不满足立即断言失败。

    ``connect_test_db`` 与 acceptance/评测 runner 的自建连接共用同一份规则，
    避免两条路径的边界漂移。
    """
    if not conn.info.dbname.endswith("_test"):
        raise AssertionError(
            f"测试必须连接 *_test 数据库，实际 {conn.info.dbname}")
    if (conn.info.host or "") not in LOCAL_HOSTS:
        raise AssertionError(
            f"测试必须连接本地测试实例，实际 {conn.info.host}")


def connect_test_db(case: Any, dsn_env: str = "BI_TEST_ADMIN_DSN") -> psycopg.Connection:
    """连测试库并开启外层回滚事务；不在 *_test 或不在本机就直接失败。"""
    conn = psycopg.connect(os.environ[dsn_env])
    try:
        assert_isolated_test_target(conn)
    except AssertionError:
        conn.close()
        raise
    transaction = conn.transaction()
    transaction.__enter__()
    # tearDown 先跑，再跑 cleanups；所以回滚登记成 cleanup 时要用逆序保证外层先退出。
    case.addCleanup(conn.close)
    case.addCleanup(_rollback, conn, transaction)
    return conn


def _rollback(conn: psycopg.Connection, transaction: Any) -> None:
    """按 psycopg3 协议以 Rollback() 退出外层事务，禁止把测试写入提交掉。"""
    transaction.__exit__(psycopg.Rollback, psycopg.Rollback(), None)
    if conn.info.transaction_status != psycopg.pq.TransactionStatus.IDLE:
        conn.rollback()
