"""独立合成评测库：连接边界与一次性初始化（计划 2026-10-24 T1）。

为什么是独立实例而不是换库名：迁移 001/021/023 会创建 cluster 级角色
（bi_sync/bi_reader/bi_app/bi_approver/bi_monitor），在共享实例上重放会改动
其他库依赖的角色；既有 ``bi_agent_test`` 承载过真实导入与同步数据，库名带
``_test`` 不能证明它是可随意重建的沙箱。

边界规则（先于任何业务 SQL）：

- 数据库名必须恰为 ``bi_agent_eval_test``；其他库名（含 ``bi_agent_test`` 与
  生产名 ``bi_agent``）一律拒绝。
- 主机必须是 localhost / 127.0.0.1 / ::1；DSN 解析交给 psycopg
  （URL 与 key/value 两种写法），不做手写字符串匹配。
- 目标库必须带本轮初始化写入的沙箱标记 ``bi.evaluation_sandbox``；标记在，
  才允许种子/评测路径。
- 拒绝信息不含 DSN 或密码；本模块不返回、不记录 DSN。

一次性初始化（``--bootstrap`` / ``--initialize``）按仓库顺序重放
``backend/sql`` 全部迁移、给 ``bi_reader`` 设登录密码、最后写沙箱标记。
已有标记的实例拒绝自动重建。
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import socket
import subprocess
import sys
import time
from pathlib import Path

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict

EVALUATION_DB_NAME = "bi_agent_eval_test"
SANDBOX_LABEL = "bi-agent-evaluation-sandbox"
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}
DEFAULT_PORT = 55439
SQL_DIR = Path(__file__).resolve().parents[2] / "sql"
REPO_ROOT = Path(__file__).resolve().parents[3]
ENV_FILE = REPO_ROOT / ".env.evaluation"
COMPOSE_FILE = REPO_ROOT / "deploy" / "evaluation" / "docker-compose.yml"


class EvaluationTargetError(RuntimeError):
    """目标不是本轮隔离评测沙箱；拒绝连接或操作，且不回显 DSN。"""


def parse_evaluation_dsn(dsn: str) -> dict:
    """解析并校验 DSN；非法目标在任何连接建立之前就被拒绝。

    返回 psycopg 的连接参数（host 缺省按 localhost、port 归一为 int），
    供 ``psycopg.connect(**params)`` 使用。
    """
    try:
        params = dict(conninfo_to_dict(dsn))
    except psycopg.Error as exc:
        raise EvaluationTargetError(
            f"DSN无法解析（{type(exc).__name__}）") from exc
    params.setdefault("host", "localhost")
    _require_isolated_params(params)
    if "port" in params:
        params["port"] = int(params["port"])
    return params


def _require_isolated_params(params: dict) -> None:
    if params.get("dbname") != EVALUATION_DB_NAME:
        raise EvaluationTargetError(
            f"评测沙箱库名必须恰为{EVALUATION_DB_NAME}；"
            "其他数据库（含既有共享测试库）不接受种子或迁移")
    if params.get("host") not in LOCAL_HOSTS:
        raise EvaluationTargetError(
            "评测沙箱必须连接本机地址（localhost/127.0.0.1/::1）")


def _marker_row(conn: psycopg.Connection) -> tuple | None:
    with conn.cursor() as cursor:
        cursor.execute("SELECT label, created_at FROM bi.evaluation_sandbox "
                       "LIMIT 1")
        return cursor.fetchone()


def sandbox_marker(conn: psycopg.Connection) -> dict:
    """读沙箱标记；缺失或不匹配即拒绝。只读，不写任何业务 SQL。"""
    try:
        row = _marker_row(conn)
    except psycopg.errors.UndefinedTable:
        raise EvaluationTargetError(
            "目标库缺少评测沙箱标记（bi.evaluation_sandbox），不允许进入种子路径")
    if row is None or row[0] != SANDBOX_LABEL:
        raise EvaluationTargetError("沙箱标记与本轮评测不符，拒绝使用该库")
    return {"label": row[0], "created_at": row[1]}


def connect_evaluation_db(dsn: str) -> psycopg.Connection:
    """连接评测沙箱并完成全部身份检查；返回可用的干净连接。"""
    parse_evaluation_dsn(dsn)
    conn = psycopg.connect(dsn)
    try:
        if conn.info.dbname != EVALUATION_DB_NAME:
            raise EvaluationTargetError("实际连接到的数据库不是评测沙箱")
        if (conn.info.host or "") not in LOCAL_HOSTS:
            raise EvaluationTargetError("实际连接到的不是本机实例")
        sandbox_marker(conn)
        conn.rollback()  # 标记检查开启的只读事务在此结束
    except Exception:
        conn.close()
        raise
    return conn


def initialize_evaluation_db(params: dict, *, reader_password: str | None = None) -> None:
    """一次性初始化：按序重放迁移 → bi_reader 密码 → 写沙箱标记。

    全部成功才提交；任一步失败回滚并关闭。已有标记的实例拒绝重建。
    """
    _require_isolated_params(params)
    conn = psycopg.connect(**params)
    try:
        try:
            row = _marker_row(conn)
        except psycopg.errors.UndefinedTable:
            row = None
        # 探测语句失败会把外层事务留在 aborted 状态，必须先回滚再重放迁移。
        conn.rollback()
        if row is not None:
            raise EvaluationTargetError(
                "目标库已有评测沙箱标记；同名已有资源不自动重建。"
                "确要重来请人工清理后重跑")
        for path in sorted(SQL_DIR.glob("*.sql")):
            conn.execute(path.read_text(encoding="utf-8"))
        if reader_password is not None:
            # ALTER ROLE 的 PASSWORD 只收字面量；用 sql.Literal 正确转义。
            conn.execute(sql.SQL("ALTER ROLE bi_reader LOGIN PASSWORD {}")
                         .format(sql.Literal(reader_password)))
        conn.execute(
            "CREATE TABLE IF NOT EXISTS bi.evaluation_sandbox ("
            "label text PRIMARY KEY, created_at timestamptz NOT NULL "
            "DEFAULT now())")
        conn.execute(
            "INSERT INTO bi.evaluation_sandbox(label) VALUES (%s) "
            "ON CONFLICT (label) DO NOTHING", (SANDBOX_LABEL,))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# bootstrap：生成本地密码与空闲端口、写 .env.evaluation、起独立 compose、初始化
# ---------------------------------------------------------------------------


def _port_is_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        try:
            probe.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def _pick_port() -> int:
    candidates = [DEFAULT_PORT] + list(range(DEFAULT_PORT + 1,
                                             DEFAULT_PORT + 21))
    for port in candidates:
        if _port_is_free(port):
            return port
    raise EvaluationTargetError("候选端口均被占用，请人工指定后重试")


def _admin_dsn(password: str, port: int) -> str:
    return (f"postgresql://postgres:{password}@127.0.0.1:{port}/"
            f"{EVALUATION_DB_NAME}")


def bootstrap() -> int:
    if ENV_FILE.exists():
        print(json.dumps({"result": "refused", "reason":
                          ".env.evaluation已存在；已有配置不覆盖，"
                          "重建需人工处理"}, ensure_ascii=False))
        return 2
    admin_password = secrets.token_urlsafe(24)
    reader_password = secrets.token_urlsafe(24)
    port = _pick_port()
    dsn = _admin_dsn(admin_password, port)
    ENV_FILE.write_text("\n".join([
        "# 评测沙箱（独立 PostgreSQL 实例，仅合成数据）。由 "
        "tests.evaluation.database --bootstrap 生成。",
        "# 不含模型或快麦配置；不要提交本文件。",
        f"BI_EVALUATION_POSTGRES_PASSWORD={admin_password}",
        f"BI_EVALUATION_PORT={port}",
        f"BI_EVALUATION_ADMIN_DSN={dsn}",
        f"BI_EVALUATION_READER_PASSWORD={reader_password}",
        f"BI_EVALUATION_READER_DSN=postgresql://bi_reader:{reader_password}"
        f"@127.0.0.1:{port}/{EVALUATION_DB_NAME}",
        "# 既有测试/验收入口读这两个名字；指向本沙箱后正常路径只使用新实例。",
        f"BI_TEST_ADMIN_DSN={dsn}",
        f"BI_TEST_READER_DSN=postgresql://bi_reader:{reader_password}"
        f"@127.0.0.1:{port}/{EVALUATION_DB_NAME}",
        "",
    ]), encoding="utf-8")
    print(json.dumps({"result": "env-written", "port": port,
                      "database": EVALUATION_DB_NAME,
                      "env_file": ENV_FILE.name}, ensure_ascii=False))
    subprocess.run(["docker", "compose", "-f", str(COMPOSE_FILE),
                    "--env-file", str(ENV_FILE), "up", "-d"],
                   check=True, cwd=REPO_ROOT)
    params = parse_evaluation_dsn(dsn)
    deadline = time.monotonic() + 120
    while True:
        try:
            ready = psycopg.connect(**params, connect_timeout=3)
            ready.close()
            break
        except psycopg.OperationalError:
            if time.monotonic() > deadline:
                print(json.dumps({"result": "timeout",
                                  "reason": "评测实例120秒内未就绪"},
                                 ensure_ascii=False))
                return 2
            time.sleep(2)
    initialize_evaluation_db(params, reader_password=reader_password)
    seed_base_dataset(dsn)
    print(json.dumps({"result": "initialized", "port": port,
                      "database": EVALUATION_DB_NAME,
                      "migrations": len(sorted(SQL_DIR.glob('*.sql')))},
                     ensure_ascii=False))
    return 0


def seed_base_dataset(dsn: str | None = None) -> int:
    """提交最小合成基线：仅 SB1（带档案成本的连续 20 日成交）。

    受控探索套件的真库 fixture（tests/test_exploration.py 的
    ExplorationLivePlanFixture）按设计要求共享测试库**已有已提交数据**才可取证
    （旧 bi_agent_test 靠真实同步数据满足）。沙箱只补 SB1 一家店满足该假设；
    **不**提交 seed_business_case——它不是为叠加已提交状态设计的，测试内部
    会在回滚事务里重放它，外层再有已提交的同名行会破坏 test_db 的种子语义。
    """
    dsn = dsn or os.getenv("BI_EVALUATION_ADMIN_DSN")
    if not dsn:
        print(json.dumps({"result": "skipped", "reason":
                          "未配置BI_EVALUATION_ADMIN_DSN"}, ensure_ascii=False))
        return 2
    conn = connect_evaluation_db(dsn)
    try:
        conn.execute("SAVEPOINT eval_seed_base")
        try:
            _seed_costed_shop(conn)
            conn.commit()
        except Exception:
            conn.execute("ROLLBACK TO eval_seed_base")
            conn.execute("RELEASE SAVEPOINT eval_seed_base")
            raise
    finally:
        conn.close()
    print(json.dumps({"result": "seeded",
                      "dataset": "sb1_costed"}, ensure_ascii=False))
    return 0


def _seed_costed_shop(conn: psycopg.Connection) -> None:
    """SB1：20 个连续自然日、逐日带档案成本（item.cost）的合成成交。

    seed_business_case 的 S1（约 15 个自然日）不带 cost，`v_product_cost_daily`
    的 cost_total 全是 0；探索 fixture 按「自然日数最多」选店，断言成本合计是
    十进制数（如 "100.0"），"0" 过不了。旧共享库靠真实同步的档案成本满足该
    前提，沙箱用天数更多、逐日带成本的 SB1 成为 fixture 的取证对象替代。
    日期压在 2026-08-20~09-08（冻结窗口内、远离真实经营数据）。
    """
    from datetime import datetime
    from zoneinfo import ZoneInfo

    beijing = ZoneInfo("Asia/Shanghai")
    conn.execute(
        "INSERT INTO bi.shops(shop_id, platform, display_name) "
        "VALUES ('SB1','fxg','评测基线成本店') "
        "ON CONFLICT (shop_id) DO UPDATE SET display_name = EXCLUDED.display_name")
    # 幂等：重复 seed-base 时先清掉本店的合成行再重建。
    conn.execute("DELETE FROM bi.order_items WHERE shop_id='SB1'")
    conn.execute("DELETE FROM bi.orders WHERE shop_id='SB1'")
    for index, day in enumerate(range(20, 32)):  # 08-20 ~ 08-31
        _apply_costed_trade(conn, beijing, index=index, month=8, day=day)
    for index, day in enumerate(range(1, 9), start=12):  # 09-01 ~ 09-08
        _apply_costed_trade(conn, beijing, index=index, month=9, day=day)


def _apply_costed_trade(conn: psycopg.Connection, beijing, *, index: int,
                        month: int, day: int) -> None:
    from datetime import datetime

    from bi_agent.sync import apply_trade, normalise_trade

    moment = datetime(2026, month, day, 10, tzinfo=beijing)
    raw = {
        "sid": f"EVAL-COST-{index:02d}", "userId": "SB1",
        "tid": f"C-EVAL-COST-{index:02d}",
        "payTime": int(moment.timestamp() * 1000),
        "updTime": int(moment.timestamp() * 1000) + 3600000,
        "orders": [{"oid": f"EVAL-COST-{index:02d}-1",
                    "tid": f"C-EVAL-COST-{index:02d}", "itemSysId": "P_A",
                    "num": "2", "payAmount": "200.00", "cost": "50"}],
    }
    trade = normalise_trade(raw)
    assert trade["normalization_status"] == "normal", trade
    assert apply_trade(conn, trade, batch_id="eval-base-seed") or True


def initialize_from_env() -> int:
    dsn = os.getenv("BI_EVALUATION_ADMIN_DSN")
    if not dsn:
        print(json.dumps({"result": "skipped", "reason":
                          "未配置BI_EVALUATION_ADMIN_DSN"}, ensure_ascii=False))
        return 2
    params = parse_evaluation_dsn(dsn)
    initialize_evaluation_db(
        params, reader_password=os.getenv("BI_EVALUATION_READER_PASSWORD"))
    print(json.dumps({"result": "initialized",
                      "database": EVALUATION_DB_NAME}, ensure_ascii=False))
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="tests.evaluation.database",
        description="评测沙箱的建立与初始化（只在独立实例上操作）")
    parser.add_argument("--bootstrap", action="store_true",
                        help="生成.env.evaluation、启动独立compose并初始化")
    parser.add_argument("--initialize", action="store_true",
                        help="对已启动的实例重放迁移并写沙箱标记")
    parser.add_argument("--seed-base", action="store_true",
                        help="提交合成基线种子（探索套件的真库取证前提）")
    args = parser.parse_args(argv)
    if args.bootstrap:
        return bootstrap()
    if args.initialize:
        return initialize_from_env()
    if args.seed_base:
        return seed_base_dataset()
    parser.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
