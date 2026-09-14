"""只读成本门禁与受控执行（计划 Task 4 Step 3/4）。

两道门，各有各的事务，各有各的预算：

1. `estimate_plan` 只发一条 `EXPLAIN (FORMAT JSON)`：在**自己的** `conn.transaction()` 里
   先 `SET TRANSACTION READ ONLY`，再 `SET LOCAL statement_timeout = '5000ms'`，然后才排包。
   读根 Plan 的 `Plan Rows` 与 `Total Cost`，任一超限抛 `ExplorationBudgetExceeded`
   （只有 `estimated_rows` 与 `total_cost` 两个原因）。返回的是**新**计划：原计划仍然是
   "未估算"，Task 5 因此能看出哪一步没走过。
2. `execute_plan` 只接已估算的计划，并且再比一道预算：有人手工 `model_copy` 填一个看起来
   安全的估算值也进不来。执行在另一个只读事务里，绑定计划自己的参数（真值永远不进 SQL
   文本），取 `limit + 1` 行——多出来的那一行是**截断证据**，一律整条拒而不是丢一行交数
   （与 `bi_agent.metrics._fetch_capped` 同一口径）。`cursor.description` 与从冻结的
   `plan.sql_text` 里逐项解出的输出列必须逐字相等：列数、列名、列序任一不符就拒。

三条不收口的线：

- **时间**：`deadline` 是 `time.monotonic()` 上的绝对时刻（与 `DomainContext.deadline`
  同一口径）。距 deadline 不足 `DB_IO_RESERVE_SECONDS`（0.1 秒）时**一次库都不碰**：
  宁可对用户说"这轮没算完"，也不留一条没人收结果的查询。语句自身的 5 秒上限由
  `SET LOCAL` 定，不随剩余预算收紧——那是计划 Step 3 钉死的一句话，整轮 30 秒的预算由
  Task 5 在节点之间 enforced。
- **身份**：本模块不认调用方给的 SQL，只认 `ValidatedQueryPlan`。执行前的四道形状检查
  （目录版本、四个服务端参数 key、`limit` 取值、语句文本：单条且以 `SELECT ` 开头、
  输出列别名可逐项解出）是**纵深防御**，不替代 Task 3 的策略：策略负责证明这句话是
  编译器那条模板，这里只保证"这句话至少是一条能安全发出去的单 SELECT"。
- **不外泄**：数据库自己报的错（`psycopg.Error`）原文里带着将要执行的语句，一律换成
  稳定原因码（超时单独一个码），并且 `raise ... from None` 把带语句的链一起断掉——SQL
  原文只能进 `bi.query_diagnostics`（总设计 §6.2）。

为什么不在这层做安全表示：这层看到的是数据库原始行，里面还有真店号；把结果字节预算绑到那份
表示上，要么逼这里留下原始行，要么逼这里偷跑下一步。两者都不做。所以：行数与列身份归
本模块（`MAX_ROWS` / `limit + 1` / 逐字比列名），字节数归下一层的投影入口。`_` 前缀的内部
列名**原样**交给那一步——把它换成 opaque 引用是那一层的唯一职责，本层没有那张映射表。
"""

from __future__ import annotations

import math
import re
import time
from decimal import Decimal, InvalidOperation
from typing import NoReturn

import psycopg

from bi_agent.exploration.compiler import INTERNAL_ALIAS_PREFIX, PARAMETER_KEYS
from bi_agent.exploration.models import (
    MAX_ESTIMATED_ROWS,
    MAX_ROWS,
    MAX_TOTAL_COST,
    STATEMENT_TIMEOUT_MS,
    DB_IO_RESERVE_SECONDS,
    ExplorationBudgetExceeded,
    ValidatedQueryPlan,
)
from bi_agent.semantic_catalog.registry import CATALOG, catalog_indexes

# 拒绝只有一个前缀：`_reject(原因)` 给 `ValueError("exploration_" + 原因)`，不回显任何入参。
CODE_PREFIX = "exploration_"
# 门禁事务的三句话（计划 Task 4 Step 3 的原文形状）。
READ_ONLY_STATEMENT = "SET TRANSACTION READ ONLY"
TIMEOUT_STATEMENT_TEMPLATE = f"SET LOCAL statement_timeout = '{{ms}}ms'"
EXPLAIN_STATEMENT_PREFIX = "EXPLAIN (FORMAT JSON) "
# 输出列别名：编译器只在列出处写 `AS "别名"`，`FROM ... AS fact` 与档案侧的 `AS shops`
# 都是裸名字（见 `compiler._bare`），所以带引号的那几处就是全部声明列。
DECLARED_ALIAS_RE = re.compile(r'AS "([a-z_][a-z0-9_]*)"')
SELECT_HEAD_RE = re.compile(r"^SELECT ")


def estimate_plan(conn, plan: ValidatedQueryPlan, *, deadline: float) -> ValidatedQueryPlan:
    """花一次 EXPLAIN 换两个估算数，超预算就不给可执行计划。

    返回新计划（`estimated_rows` / `estimated_total_cost` 已填）；入参计划不被改动。
    """
    _require_call(conn, plan, deadline)
    with conn.transaction():
        try:
            _open_gate(conn)
            row = conn.execute(EXPLAIN_STATEMENT_PREFIX + plan.sql_text,
                               plan.parameters).fetchone()
        except psycopg.errors.QueryCanceled:
            _reject("statement_timeout")
        except psycopg.Error:
            _reject("query_rejected")
    estimated_rows, total_cost = _root_estimate(row)
    if estimated_rows > MAX_ESTIMATED_ROWS:
        raise ExplorationBudgetExceeded("estimated_rows")
    if total_cost > MAX_TOTAL_COST:
        raise ExplorationBudgetExceeded("total_cost")
    return _with_estimate(plan, estimated_rows, total_cost)


def execute_plan(conn, plan: ValidatedQueryPlan, *,
                 deadline: float) -> tuple[list[str], list[tuple[object, ...]]]:
    """执行一条已过成本门的计划：回列身份与原始行，不截断、不改列。

    列身份：输出列别名解回稳定 ref；`_` 前缀的内部列名原样交出去（内部列只能由
    下一层带着服务端映射表换名，本层没有那张表）。
    """
    declared = _require_call(conn, plan, deadline)
    _require_estimated(plan)
    limit = _limit_value(plan.parameters)
    with conn.transaction():
        try:
            _open_gate(conn)
            result = conn.execute(plan.sql_text, plan.parameters)
            names = [column.name for column in result.description]
            if names != declared:
                # 列数、列名、列序逐项相等才继续：别名就是这一列的身份，近似相等不算通过。
                _reject("column_mismatch")
            rows = result.fetchmany(limit + 1)
            if len(rows) > limit:
                # 多出来的那一行就是截断证据：丢一行交数会把汇总额压低冒充正常。
                _reject("row_limit_exceeded")
        except psycopg.errors.QueryCanceled:
            _reject("statement_timeout")
        except psycopg.Error:
            _reject("query_rejected")
    return [_column_identity(alias, plan) for alias in declared], \
        [tuple(row) for row in rows]


# --- 入参与预算：一次库都不碰的那几道检查 ----------------------------------------

def _require_call(conn, plan, deadline: object) -> list[str]:
    """五道前置检查（计划契约、deadline、目录版本、参数与语句形状）都在碰库之前。

    回的就是与 `cursor.description` 逐项比的那份声明输出列：列身份解不出来的计划，
    连 EXPLAIN 都不值得花——不知道它会回哪几列，就不知道拿到的数是哪几列的。
    """
    if not isinstance(plan, ValidatedQueryPlan):
        raise TypeError(CODE_PREFIX + "plan_contract_required")
    if (isinstance(deadline, bool) or not isinstance(deadline, (int, float))
            or not math.isfinite(deadline)):
        raise TypeError(CODE_PREFIX + "deadline_contract_required")
    if time.monotonic() + DB_IO_RESERVE_SECONDS > deadline:
        _reject("deadline_exceeded")
    if plan.catalog_version != CATALOG.version:
        # 标识符与列身份只能从**当前发布**的目录解：旧快照用来解释历史结果，不用来发新查询。
        _reject("catalog_version_mismatch")
    _require_server_parameters(plan.parameters)
    _require_executable_text(plan.sql_text)
    return _declared_aliases(plan.sql_text)


def _require_estimated(plan: ValidatedQueryPlan) -> None:
    """执行只接"过成本门的计划"：两个估算数都在，并且都还在预算内。"""
    if plan.estimated_rows is None or plan.estimated_total_cost is None:
        _reject("plan_not_estimated")
    if plan.estimated_rows > MAX_ESTIMATED_ROWS:
        raise ExplorationBudgetExceeded("estimated_rows")
    if plan.estimated_total_cost > MAX_TOTAL_COST:
        raise ExplorationBudgetExceeded("total_cost")


def _require_server_parameters(parameters: dict[str, object]) -> None:
    """四个服务端参数 key 一个不多一个不少；`limit` 的取值本模块自己要拿来当预算。

    窗口与授权集合的取值属 Task 2/3 的口径（那里有请求侧的唯一真源），这里不重判第二份
    规则；psycopg 绑定失败会被换成稳定原因码，不会把语句原文带出去。
    """
    if sorted(parameters) != sorted(PARAMETER_KEYS):
        _reject("parameter_mismatch")
    _limit_value(parameters)


def _limit_value(parameters: dict[str, object]) -> int:
    limit = parameters["limit"]
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_ROWS:
        _reject("parameter_invalid")
    return limit


def _require_executable_text(sql_text: str) -> None:
    """单条、以 `SELECT ` 开头：这两条是廉价的纵深防御，不是策略的替代。

    分号在编译模板里永远不出现，出现就是有人往一句话后面接了第二句；非 `SELECT` 开头同理。
    真正的结构校验（占位符位置、表/列/JOIN 闭环、授权与时间谓词）在 `exploration.policy`。
    """
    if ";" in sql_text:
        _reject("multiple_statements")
    if not SELECT_HEAD_RE.match(sql_text):
        _reject("statement_not_select")


def _declared_aliases(sql_text: str) -> list[str]:
    """从冻结的计划文本里按序解出输出列别名：与 `cursor.description` 逐项比的那一份。

    别名不是显示名：它把这一列钉回稳定 ref。一个都解不出来，或者同一个别名出现两次，
    都意味着"这句话的输出列没有唯一身份"，不能执行。
    """
    aliases = DECLARED_ALIAS_RE.findall(sql_text)
    if not aliases or len(aliases) != len(set(aliases)):
        _reject("alias_unbound")
    return aliases


# --- 事务形状 ---------------------------------------------------------------------

def _open_gate(conn) -> None:
    """门禁事务的两句话：先只读，再限时。顺序不能反——`SET TRANSACTION` 只在本事务
    还没有任何查询之前有效，而 `SET LOCAL` 是语句级的。
    """
    conn.execute(READ_ONLY_STATEMENT)
    conn.execute(TIMEOUT_STATEMENT_TEMPLATE.format(ms=STATEMENT_TIMEOUT_MS))


# --- EXPLAIN 结果 ----------------------------------------------------------------

def _root_estimate(row) -> tuple[int, Decimal]:
    """取根 Plan 的两个数：形状不对就是"没拿到估算"，不是"估算很小"。"""
    plan_node = _first_plan_node(row)
    if plan_node is None:
        _reject("estimate_unavailable")
    return _estimate_rows(plan_node), _estimate_cost(plan_node)


def _first_plan_node(row):
    payload = row[0] if isinstance(row, (list, tuple)) and row else None
    if not isinstance(payload, (list, tuple)) or not payload:
        return None
    entry = payload[0]
    plan_node = entry.get("Plan") if isinstance(entry, dict) else None
    return plan_node if isinstance(plan_node, dict) else None


def _estimate_rows(plan_node: dict) -> int:
    value = plan_node.get("Plan Rows")
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        _reject("estimate_unavailable")
    return value


def _estimate_cost(plan_node: dict) -> Decimal:
    value = plan_node.get("Total Cost")
    if isinstance(value, bool) or not isinstance(value, (int, float, str, Decimal)):
        _reject("estimate_unavailable")
    try:
        cost = Decimal(str(value))
    except InvalidOperation:
        _reject("estimate_unavailable")
    if not cost.is_finite() or cost < 0:
        # 拿不到可信成本就是"不可用"：把 NaN 当成 0 会让任何语句都过门。
        _reject("estimate_unavailable")
    return cost


def _with_estimate(plan: ValidatedQueryPlan, rows: int, cost: Decimal) -> ValidatedQueryPlan:
    """重新构造而不是 `model_copy`：估算数也得过一遍 Task 1 的形状检查。"""
    return ValidatedQueryPlan(
        template_version=plan.template_version,
        catalog_version=plan.catalog_version,
        statement_fingerprint=plan.statement_fingerprint,
        sql_text=plan.sql_text,
        parameters=dict(plan.parameters),
        selected_refs=list(plan.selected_refs),
        estimated_rows=rows,
        estimated_total_cost=cost,
        warnings=list(plan.warnings),
    )


# --- 列身份 -----------------------------------------------------------------------

def _column_identity(alias: str, plan: ValidatedQueryPlan) -> str:
    """`SQL 别名 → 稳定 ref`，与编译器/策略同一套推导（授权列不按 ref 输出）。

    `_` 前缀的内部列**不**换成 ref：真店号只能由下一层带着服务端映射表换成 opaque 引用。
    """
    if alias.startswith(INTERNAL_ALIAS_PREFIX):
        return alias
    return _ref_for_alias(alias, plan)


def _ref_for_alias(alias: str, plan: ValidatedQueryPlan) -> str:
    """在计划的 ref 闭环里找这个别名对应的那个 ref。

    别名与 ref 的对应关系只有一条：普通列是 ref 的 `-` → `_`，授权列是 `_列名`（编译器
    与策略都是这一套）。解不出来就说明这一列没有已选 ref 兜着，不能发出去。
    """
    index = catalog_indexes(CATALOG)
    for ref in plan.selected_refs:
        field = index.fields.get(ref)
        if field is not None:
            candidate = (f"{INTERNAL_ALIAS_PREFIX}{field.column}"
                         if field.role == "authorization" else ref.replace("-", "_"))
        elif ref in index.metrics:
            candidate = ref.replace("-", "_")
        else:
            continue
        if candidate == alias:
            return ref
    _reject("alias_unbound")


def _reject(reason: str) -> NoReturn:
    # `from None`：在 `except` 里转码时不把带语句原文的链带给日志与诊断。
    raise ValueError(CODE_PREFIX + reason) from None
