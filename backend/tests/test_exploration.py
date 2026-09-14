"""受控 SQL 探索测试（计划 Task 1 契约/依赖/门禁 + Task 2 固定 Tool 优先与编译器 +
Task 3 AST 策略与攻击语料 + Task 4 只读成本门禁与安全投影）。

`ExplorationContractTests` 钉形状：`ExplorationRequest` → `SqlDraft` →
`ValidatedQueryPlan` → `ExplorationResult`（加 `ExplorationColumn`）五个契约、
`sqlglot` 这一条锁定的依赖，以及 `AppSettings.controlled_sql_enabled` 门禁。
`ExplorationCompilerTests` 钉 Task 2 的两件纯服务端事：固定 Tool 优先
（`fixed_tool_for`）与确定性单基表 SELECT 编译（`compile_query`）。
`ExplorationPolicyTests` 钉 Task 3：`validate_exploration_plan` 对
`tests/exploration_attacks.jsonl` 里每一条攻击都在**任何数据库调用之前**给出稳定原因码，
而编译器产出的正例必须通过并拿到 64 位 statement fingerprint。
`ExplorationBudgetContractTests` 钉 Task 4 交到契约层的那一份：预算数字、
`ExplorationBudgetExceeded` 的两个原因码，以及"先 EXPLAIN、后只读执行、最后投影"这条序列。
`ExplorationProjectionTests` 是纯函数投影用例；`ExplorationRepositoryTests` 走**真库**：
EXPLAIN 成本、只读事务、`statement_timeout`、`limit+1` 溢出、列名逐项比对与数据库角色拒绝。
运行域与 Agent Tool（Task 5）仍不在本文件里。

为什么 256 KiB 在投影层判而不在执行层判：`execute_plan` 看到的是**未投影**的行，里面还有真
店号；把预算绑到那份表示上，要么逼执行层留下原始行，要么逼它偷偷调用投影。两者都不做，
所以执行层只管行数（`limit+1`）与列身份，投影层只管安全表示的字节数。

反恒真约定（开发流程 §4.1）：每一组拒绝用例都配一条只差那个角度的接受用例，并且断言
错误落在哪个字段/哪个原因码上；只扫整份序列化载荷里的随机子串不算护栏。策略夹具一律
用**目录全集**当本轮选择，因此攻击被拒只能来自 SQL 结构本身，不是来自窄选择集。
"""

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import inspect
import importlib
import importlib.metadata
import json
import os
import pathlib
import re
import time
import tomllib
import types
import typing
import unittest
from unittest import mock
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

import psycopg
from pydantic import BaseModel, ValidationError

from tests.dbfixtures import connect_test_db
from tests.test_core import valid_app_env

BACKEND_ROOT = pathlib.Path(__file__).resolve().parents[1]
SQLGLOT_SPECIFIER = ">=27.14,<28"
SQLGLOT_LOWER_BOUND = (27, 14)
SQLGLOT_UPPER_BOUND = (28,)
# 计划 Task 1 的 Produces 清单：五个契约，一个不多，一个不少。
EXPORTED_CONTRACTS = (
    "ExplorationColumn",
    "ExplorationRequest",
    "ExplorationResult",
    "SqlDraft",
    "ValidatedQueryPlan",
)
# 计划 Task 2 的 Produces 清单：两个入口 + 钉死的覆盖矩阵常量。
TASK_TWO_EXPORTS = ("FIXED_TOOL_METRICS", "compile_query", "fixed_tool_for")
# Task 4 之后本包有七个模块（`graph.py` / `tool.py` 属 Task 5）。
PACKAGE_MODULES = ["__init__.py", "compiler.py", "eligibility.py", "graph.py",
                   "models.py", "policy.py", "projection.py", "repository.py", "tool.py"]
# Task 5 才会交付的入口名字：本切片里它们必须不存在。
NOT_YET_IMPLEMENTED = ("execute_exploration_tool",)
# 计划 Task 3 的 Produces 清单只有一个入口，而且它的 Files 清单不含 `__init__.py`：
# 包级公共面仍归 Task 2，策略入口只住在 `exploration.policy` 里（Task 5 再接）。
TASK_THREE_ENTRY_POINT = "validate_exploration_plan"

# --- Task 3 攻击语料与策略夹具 -------------------------------------------------
ATTACK_CORPUS_PATH = BACKEND_ROOT / "tests" / "exploration_attacks.jsonl"
# 语料行数钉死（计划要求至少 20）：少一行就是有人删了用例，不是删了缺陷。
ATTACK_CORPUS_ROWS = 46
ATTACK_ROW_FIELDS = frozenset({"id", "sql", "reason"})
ATTACK_ID_RE = re.compile(r"^A[0-9]{2}$")
ATTACK_REASON_RE = re.compile(r"^[a-z][a-z0-9_]*$")
# 原因码只有一种拼法：`exploration_` 前缀 + 语料里的 reason。
POLICY_CODE_PREFIX = "exploration_"
# 计划 Task 3 Step 1 逐字给出的五条种子攻击：语料必须原样收着它们。
PLAN_SEED_ATTACKS = (
    ("A01", "SELECT * FROM reporting.v_shop_daily", "star_forbidden"),
    ("A02", "SELECT 1; DELETE FROM bi.orders", "multiple_statements"),
    ("A03", "WITH x AS (DELETE FROM bi.orders RETURNING *) SELECT * FROM x",
     "cte_forbidden"),
    ("A04", "SELECT pg_read_file('/etc/passwd')", "function_forbidden"),
    ("A05", "SELECT sum(p.amount) FROM reporting.v_payments p "
     "CROSS JOIN reporting.v_refunds r", "cross_join_forbidden"),
)
POLICY_NOW = datetime(2026, 9, 14, 12, tzinfo=timezone.utc)
# 带另一个时区的同一时刻：策略要求“带时区”，不要求“必须是 UTC”。
BEIJING = ZoneInfo("Asia/Shanghai")
POLICY_SHOP_IDS = frozenset({"S1", "S2"})
# 计划 Task 3 Step 4 的 fingerprint 输入清单：五项，不多不少。
FINGERPRINT_FIELDS = frozenset({"normalized_sql", "parameter_names", "selected_refs",
                                "scope_fingerprint", "catalog_version"})
SHA256_HEX = "a" * 64

# --- Task 2 夹具用的目录真 ref ---------------------------------------------------
# 计划 Task 2 的示例把分组简写成 `field-day` / `field-shop-id`、把 JOIN 侧写作
# `field-platform`；已发布目录按视图作用域命名重复列（见
# `semantic_catalog/registry.py` 模块注释 a)），所以这里用真 ref，语义不变。
DAY_COST = "field-product-cost-daily-day"
SHOP_COST = "field-product-cost-daily-shop-id"
FIELD_COST_TOTAL = "field-product-cost-daily-cost-total"
LINE_KIND_COST = "field-product-cost-daily-line-kind"
PRODUCT_COST = "field-product-cost-daily-product-id"
COST_VIEW = "view-product-cost-daily"
SHOPS_VIEW = "view-shops"
SHOPS_PLATFORM = "field-shops-platform"
SHOPS_CURRENCY = "field-shops-currency"
COST_SHOPS_JOIN = "join-product-cost-daily-shops"
DAY_SHOP_DAILY = "field-shop-daily-day"
SHOP_SHOP_DAILY = "field-shop-daily-shop-id"
PRODUCT_PRODUCT_DAILY = "field-product-daily-product-id"
DAY_PAYMENTS = "field-payments-paid-at"
STATUS_ERP = "field-erp-document-daily-normalization-status"
LISTING_CAPTURED_AT = "field-listing-items-captured-at"
POOL_PHYSICAL = "field-physical-stock-items-pool-id"
SHOP_CHANNEL = "field-channel-stock-items-shop-id"

# 计划 Task 2 Step 3 逐字固定的覆盖矩阵；产品代码里再一份，两者必须逐字相等。
PLAN_FIXED_TOOL_METRICS = {
    "query_business": frozenset({
        "metric-paid-amount", "metric-paid-orders", "metric-erp-documents",
        "metric-refund-amount", "metric-cash-difference", "metric-quantity",
        "metric-product-paid-amount"}),
    "analyze_product_performance": frozenset({
        "metric-sales-amount", "metric-quantity", "metric-cost-total",
        "metric-product-gross-profit-reference"}),
    "compare_performance": frozenset({
        "metric-sales-amount", "metric-quantity", "metric-cost-total",
        "metric-paid-amount", "metric-paid-orders",
        "metric-erp-gross-profit-reference"}),
    "audit_listing_prices": frozenset({"metric-listing-price"}),
    "inspect_inventory": frozenset({
        "metric-physical-available-quantity", "metric-channel-sellable-quantity"}),
}
# 计划里 Tool 的书写顺序就是重叠时的优先级（与 `agent._tool_schemas()` 一致）。
PLAN_TOOL_ORDER = ("query_business", "analyze_product_performance", "compare_performance",
                   "audit_listing_prices", "inspect_inventory")


def request_values(**overrides):
    """计划 Task 1 Step 1 的种子请求（`metric-cost-total` 那一组 ref）。"""
    values = {
        "question": "按店铺和日期看有成本销售额",
        "start": "2026-09-01",
        "end": "2026-09-08",
        "entity_refs": ["entity-shop"],
        "requested_metric_refs": ["metric-cost-total"],
        "group_by_field_refs": ["field-shop-id", "field-day"],
        "limit": 100,
    }
    values.update(overrides)
    return values


def make_request(**overrides):
    from bi_agent.exploration.models import ExplorationRequest

    return ExplorationRequest(**request_values(**overrides))


# --- 计划 Task 2 的夹具 ----------------------------------------------------------

def catalog_entries():
    """已发布目录的 ref 索引：Task 2 的夹具只从这一份真源反推 ref。"""
    from bi_agent.semantic_catalog.registry import CATALOG, catalog_indexes

    return catalog_indexes(CATALOG)


def as_list(value):
    return [value] if isinstance(value, str) else list(value)


def selection_for(metric_refs, *, groups=(), joins=(),
                  entities=("entity-shop",), selected_metrics=None,
                  missing_concepts=(), requires_clarification=False, **overrides):
    """造一份“检索已经把请求要的 ref 都选上”的选择（只给测试用）。

    `selected_metrics` 是故意单独开的参数：要把“本轮没选上这个指标”这种不匹配形状
    衣达出来时，不能靠重复传 `metric_refs` 的关键字参数（那是位置参数的名字）。
    """
    from bi_agent.semantic_catalog.models import SemanticSelection
    from bi_agent.semantic_catalog.registry import CATALOG

    entries = catalog_entries()
    refs = as_list(metric_refs)
    fields = set(groups)
    views = set()
    for ref in refs:
        if ref not in entries.metrics:
            # 计划矩阵里的 `metric-quantity` 在目录里没有条目：不伪造目录。
            continue
        fields.update(entries.metrics[ref].required_field_refs)
        views.add(entries.fields[entries.metrics[ref].required_field_refs[0]].view_ref)
    values = {
        "catalog_version": CATALOG.version,
        "entity_refs": tuple(sorted(entities)),
        "metric_refs": tuple(sorted(set(refs if selected_metrics is None
                                        else as_list(selected_metrics)))),
        "view_refs": tuple(sorted(views)),
        "field_refs": tuple(sorted(fields)),
        "join_path_refs": tuple(sorted(joins)),
        "missing_concepts": tuple(missing_concepts),
        "requires_clarification": requires_clarification,
    }
    values.update(overrides)
    return SemanticSelection(**values)


def request_for(metric_refs, groups=(), **overrides):
    """计划 Task 2 种子里的 `request_for(...)`：只有 ref 与业务值。"""
    from bi_agent.exploration.models import ExplorationRequest

    values = {
        "question": "按天看商品成本合计",
        "start": "2026-09-01",
        "end": "2026-09-08",
        "entity_refs": ["entity-shop"],
        "requested_metric_refs": as_list(metric_refs),
        "group_by_field_refs": list(groups),
        "limit": 100,
    }
    values.update(overrides)
    return ExplorationRequest(**values)


def compile_for(metric_refs, groups=(), *, selection=None, allowed_shop_ids=None,
                **overrides):
    """计划 Task 2 种子里的编译入口：选择集默认按请求自动补齐。"""
    from bi_agent.exploration.compiler import compile_query

    request = request_for(metric_refs, groups, **overrides)
    if selection is None:
        selection = selection_for(
            metric_refs, groups=groups, entities=request.entity_refs,
            joins=(COST_SHOPS_JOIN,) if SHOPS_PLATFORM in groups else ())
    return compile_query(request, selection=selection,
                         allowed_shop_ids=frozenset({"S1", "S2"})
                         if allowed_shop_ids is None else allowed_shop_ids)


def compile_error(metric_refs, groups=(), **kwargs):
    """要求编译失败并交出稳定原因码：能编出来就判红。"""
    try:
        draft = compile_for(metric_refs, groups, **kwargs)
    except ValueError as exc:
        return str(exc)
    raise AssertionError(f"编译器接下了这个请求：{draft.sql_text!r}")


def dequoted(sql_text):
    """只把双引号去掉：用来按字面复现计划种子里那两段未加引号的模板文本。"""
    return sql_text.replace('"', "")


# --- Task 3 夹具 ---------------------------------------------------------------

def attack_rows():
    """读 `tests/exploration_attacks.jsonl`：逐行 JSON，形状不合规当场判红。

    这里只校形状（三项、id 形状、行数），“攻击必须被拒”是策略用例的事；
    形状不钉住，语料被改少一行、多一个字段都能静默溜过。
    """
    text = ATTACK_CORPUS_PATH.read_text(encoding="utf-8")
    assert text.endswith("\n"), "攻击语料必须以 LF 结尾"
    assert "\r" not in text, "攻击语料不得含 CR"
    rows = []
    for number, line in enumerate(text.splitlines(), start=1):
        assert line.strip(), f"第 {number} 行为空行"
        row = json.loads(line)
        assert isinstance(row, dict), f"第 {number} 行不是对象"
        assert frozenset(row) == ATTACK_ROW_FIELDS, f"第 {number} 行字段不对：{sorted(row)}"
        assert isinstance(row["id"], str) and ATTACK_ID_RE.match(row["id"]), row["id"]
        assert isinstance(row["sql"], str) and row["sql"].strip(), row["id"]
        assert isinstance(row["reason"], str) and ATTACK_REASON_RE.match(row["reason"]), row
        rows.append(row)
    return rows


def policy_source():
    """策略模块源码：用来要每条语料 reason 都是策略真的会抛的那个码。"""
    import bi_agent.exploration.policy as policy

    return pathlib.Path(policy.__file__).read_text(encoding="utf-8")


def catalog_version() -> str:
    """当前发布目录版本：测试不拄第二份字面量。"""
    from bi_agent.semantic_catalog.registry import SEMANTIC_CATALOG_VERSION

    return SEMANTIC_CATALOG_VERSION


def parameter_keys() -> tuple[str, ...]:
    """编译器 owns 的四个参数 key：策略与编译器必须用同一套名字。"""
    from bi_agent.exploration.compiler import PARAMETER_KEYS

    return PARAMETER_KEYS


class RefusingConn:
    """任何属性访问都判红的“连接”替身：策略层碰一下库就失败。

    计划 Task 3 的验收是“20/20 攻击在数据库执行前拒”；只断“抛了错”不够，
    还得证明抛错之前根本没碰过 conn/store。
    """

    def __getattr__(self, name):
        raise AssertionError(f"exploration policy touched the database via {name}")


def full_selection(**overrides):
    """本轮选择 = 已发布目录的全部 ref。

    攻击语料拿它跑，是为了让“被拒”只能来自 SQL 结构本身：选择集宽到目录全集还有
    地方可拒，才是策略在干活。
    """
    from bi_agent.semantic_catalog.models import SemanticSelection
    from bi_agent.semantic_catalog.registry import CATALOG

    values = {
        "catalog_version": CATALOG.version,
        "entity_refs": tuple(sorted(entity.ref for entity in CATALOG.entities)),
        "metric_refs": tuple(sorted(metric.ref for metric in CATALOG.metrics)),
        "view_refs": tuple(sorted(view.ref for view in CATALOG.views)),
        "field_refs": tuple(sorted(field.ref for field in CATALOG.fields)),
        "join_path_refs": tuple(sorted(join.ref for join in CATALOG.joins)),
        "missing_concepts": (),
        "requires_clarification": False,
    }
    values.update(overrides)
    return SemanticSelection(**values)


def policy_context(**overrides):
    """计划 Task 3 消费的 `DomainContext`：真形状、假 conn、带时区时刻。

    `conn`/`store` 是 RefusingConn：策略不排包、不取数、不看成本，所以任何一次属性
    访问都是越界。
    """
    from bi_agent.commerce.models import DomainContext

    values = {
        "subject_id": "subject-one", "allowed_shop_ids": POLICY_SHOP_IDS,
        "shop_refs": {"S1": "ent-shop-one", "S2": "ent-shop-two"},
        "conn": RefusingConn(), "store": RefusingConn(),
        "chat_id": UUID(int=11), "user_message_id": UUID(int=12),
        "root_request_id": UUID(int=13), "now": POLICY_NOW,
        "deadline": time.monotonic() + 30.0, "attempt_no": 1,
    }
    values.update(overrides)
    return DomainContext(**values)


def shell_draft(**overrides):
    """攻击语料的草案壳子：编译器真正产出的单基表带分组查询。

    每条攻击只改 `sql_text`（parameters/selected_refs 保持不变），所以被拒原因
    只能来自 SQL 结构，不是来自“参数也没填对”。
    """
    from bi_agent.exploration.models import SqlDraft

    compiled = compile_for("metric-cost-total", groups=[DAY_COST])
    values = {"sql_text": compiled.sql_text,
              "parameters": dict(compiled.parameters),
              "selected_refs": list(compiled.selected_refs)}
    values.update(overrides)
    return SqlDraft(**values)


def attack_draft(row):
    """语料行 → 待校草案：只换 SQL 文本。"""
    return shell_draft(sql_text=row["sql"])


def ordered_sql(modifier=None):
    """草案壳子的 ORDER BY 写法：`modifier=None` 就是编译器原样。"""
    sql = shell_draft().sql_text
    if modifier is None:
        return sql
    mutated = sql.replace('ORDER BY fact."day"', f'ORDER BY fact."day" {modifier}')
    assert mutated != sql, modifier
    return mutated


def validate(draft, *, selection=None, context=None):
    """直接跑策略：不捕异常，只给“必须通过”的正例用。"""
    from bi_agent.exploration.policy import validate_exploration_plan

    return validate_exploration_plan(
        draft, selection=full_selection() if selection is None else selection,
        context=policy_context() if context is None else context)


def policy_reason(draft, *, selection=None, context=None):
    """要求策略拒接并交出稳定原因码；通过就判红。"""
    try:
        plan = validate(draft, selection=selection, context=context)
    except ValueError as exc:
        assert type(exc) is ValueError, f"不是裸 ValueError：{type(exc).__name__}"
        return str(exc)
    raise AssertionError(f"策略放行了这个草案：{plan.statement_fingerprint}")


def expected_fingerprint(sql_text, parameters, selected_refs, allowed_shop_ids,
                         catalog_version):
    """独立重现计划 Task 3 Step 4 的 fingerprint：JSON 五项 + SHA-256。

    在测试里再算一遍而不是从策略里取：两者一致才能说明持久化载荷里没有真店号
    （授权集合只以摘要出现），以及指纹形状就是计划钉的那个形状。
    """
    scope = hashlib.sha256(json.dumps(sorted(allowed_shop_ids),
                                      separators=(",", ":")).encode("utf-8")).hexdigest()
    payload = {"normalized_sql": " ".join(sql_text.split()),
               "parameter_names": sorted(parameters),          # 只取 key 名
               "selected_refs": list(selected_refs),
               "scope_fingerprint": scope,
               "catalog_version": catalog_version}
    assert frozenset(payload) == FINGERPRINT_FIELDS
    return hashlib.sha256(json.dumps(payload, sort_keys=True,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()


def draft_values(**overrides):
    values = {
        "sql_text": 'SELECT "day", sum("cost_total") FROM "reporting"."v_product_cost_daily"'
                    " AS fact GROUP BY \"day\"",
        "parameters": {"allowed_shop_ids": ["S1", "S2"], "limit": 100},
        "selected_refs": ["view-product-cost-daily", "metric-cost-total", "field-day"],
    }
    values.update(overrides)
    return values


def plan_values(**overrides):
    values = {
        "template_version": "exploration-sql/2026-09-14.1",
        "catalog_version": "semantic/2026-09-14.1",
        "statement_fingerprint": SHA256_HEX,
        "sql_text": draft_values()["sql_text"],
        "parameters": {"allowed_shop_ids": ["S1"], "limit": 100},
        "selected_refs": ["metric-cost-total", "field-day"],
    }
    values.update(overrides)
    return values


def column_values(**overrides):
    values = {"ref": "metric-cost-total", "data_type": "decimal"}
    values.update(overrides)
    return values


def result_values(**overrides):
    values = {
        "template_version": "exploration-sql/2026-09-14.1",
        "catalog_version": "semantic/2026-09-14.1",
        "statement_fingerprint": SHA256_HEX,
        "columns": [
            {"ref": "field-day", "data_type": "date"},
            {"ref": "metric-cost-total", "data_type": "decimal"},
        ],
        "rows": [{"field-day": "2026-09-01", "metric-cost-total": "12.30"}],
        "basis": [{"metric": "metric-cost-total", "basis": "cost/2026-09-12.1"}],
        "coverage": {"status": "complete", "gaps": []},
        "diagnostics": [{"code": "cost_coverage", "rows": 1}],
        "limitations": ["来源成本覆盖率未核验"],
    }
    values.update(overrides)
    return values


def error_of(factory, values):
    """要求构造失败并交出错误列表：没有任何错误时直接判红。"""
    try:
        factory(**values)
    except ValidationError as exc:
        return exc.errors(include_url=False)
    raise AssertionError(f"{factory.__name__} accepted {sorted(values)}")


def locs_of(factory, values):
    return [tuple(error["loc"]) for error in error_of(factory, values)]


def request_locs(**overrides):
    """只改请求的一个角度，报错就该落在那个字段上。"""
    from bi_agent.exploration.models import ExplorationRequest

    return locs_of(ExplorationRequest, request_values(**overrides))


# --- Task 4 夹具：预算数字、门禁语句、真库计划与投影输入 -------------------------

BUDGET_EXCEPTION = "ExplorationBudgetExceeded"
TASK_FOUR_MODULE_NAMES = ("repository", "projection")
# Task 4 的三个入口与它们所属的模块（计划 Task 4 的 Interfaces/Produces）。
TASK_FOUR_ENTRY_POINTS = {"estimate_plan": "repository", "execute_plan": "repository",
                          "project_result": "projection"}
# 计划 Task 4 Step 3/4/5 逐字钉死的数字：实现里再一份，两者必须逐字相等。
PLAN_MAX_ESTIMATED_ROWS = 50_000
PLAN_MAX_TOTAL_COST = Decimal("100000")
PLAN_STATEMENT_TIMEOUT_MS = 5_000
PLAN_MAX_RESULT_BYTES = 262_144
PLAN_DEADLINE_RESERVE_SECONDS = 0.1
PLAN_MAX_TEXT_CHARS = 4_000
# 门禁事务里的三句话（计划 Step 3 的原文形状）。
READ_ONLY_STATEMENT = "SET TRANSACTION READ ONLY"
STATEMENT_TIMEOUT_STATEMENT = "SET LOCAL statement_timeout = '5000ms'"
EXPLAIN_STATEMENT_PREFIX = "EXPLAIN (FORMAT JSON) "
# 投影对外的唯一店铺列名，与编译器给授权列的输出别名。
SHOP_ID_COLUMN = "_shop_id"
SHOP_REF_COLUMN = "shop-ref"
# 成本门只许说这两个原因（计划 Step 3）。
BUDGET_REASONS = frozenset({"estimated_rows", "total_cost"})
# 执行层与投影层的稳定原因码全集：多一个、少一个都判红（Task 5 按码分流，不看文字）。
EXECUTION_REASONS = frozenset({
    "alias_unbound", "catalog_version_mismatch", "column_mismatch", "deadline_exceeded",
    "estimate_unavailable", "multiple_statements", "parameter_invalid",
    "parameter_mismatch", "plan_not_estimated", "query_rejected", "row_limit_exceeded",
    "statement_not_select", "statement_timeout",
})
PROJECTION_REASONS = frozenset({
    "column_duplicate", "column_mismatch", "column_not_public", "datetime_naive",
    "internal_column_forbidden", "raw_identifier", "ref_unregistered", "result_too_large",
    "row_limit_exceeded", "shop_not_registered", "shop_refs_invalid", "text_too_large",
    "value_not_finite", "value_type_unsupported",
})
# 纯投影用例的输入：真店号只出现在这里（与 `policy_context()` 的夹具店号一致）。
SHOP_REFS = {"S1": "ent-shop-one", "S2": "ent-shop-two"}
# 服务端 owns 的四个参数：预算用例里的重查询不带占位符，psycopg 允许多余 key。
SERVER_PARAMETERS = {"allowed_shop_ids": ["S1"], "end": date(2026, 9, 8), "limit": 100,
                     "start": date(2026, 9, 1)}
# 两条“重到必须被成本门拦下”的语句：只碰目录表 `pg_class`，不读任何业务事实行。
# 余量是三个数量级（Plan Rows≈2×10^8、Total Cost≈2.5×10^6 vs 阈值 5×10^4 / 1×10^5），
# 所以用例不依赖统计信息的细微波动。带 `count(*)` 的那条根节点只有一行，于是
# “超行数”与“超成本”两条分支各自可判。
OVER_ROWS_SQL = ('SELECT a.relname AS "relname" FROM pg_catalog.pg_class a, '
                 'pg_catalog.pg_class b, pg_catalog.pg_class c')
OVER_COST_SQL = ('SELECT count(*) AS "n" FROM pg_catalog.pg_class a, '
                 'pg_catalog.pg_class b, pg_catalog.pg_class c')


def server_plan(sql_text, **overrides):
    """给预算用例造一份形状合法的计划：不进策略，只交给执行层。"""
    from bi_agent.exploration.models import ValidatedQueryPlan

    values = {"template_version": "exploration-sql/2026-09-14.1",
              "catalog_version": catalog_version(),
              "statement_fingerprint": SHA256_HEX,
              "sql_text": sql_text,
              "parameters": dict(SERVER_PARAMETERS),
              "selected_refs": ["metric-cost-total"]}
    values.update(overrides)
    return ValidatedQueryPlan(**values)


def stub_plan():
    """纯函数得到的未估算计划：编译器 + 策略，不碰库（`policy_context` 的 conn 一碰就判红）。"""
    return validate(shell_draft())


def project(columns, rows, *, shop_refs=SHOP_REFS):
    from bi_agent.exploration.projection import project_result

    return project_result(columns, rows, shop_refs=shop_refs)


def projection_reason(columns, rows, *, shop_refs=SHOP_REFS):
    """要求投影拒绍并交出稳定原因码；投影成功就判红。"""
    try:
        declared, projected = project(columns, rows, shop_refs=shop_refs)
    except ValueError as exc:
        assert type(exc) is ValueError, f"不是裸 ValueError：{type(exc).__name__}"
        return str(exc)
    raise AssertionError(f"投影接下了这个结果：{declared} {projected}")


def refusal(action):
    """跑一逐必须被拒的调用，交出稳定原因码文字（预算类异常走不到这里）。"""
    try:
        action()
    except ValueError as exc:
        assert type(exc) is ValueError, f"不是裸 ValueError：{type(exc).__name__}"
        return str(exc)
    raise AssertionError("调用被接下了，本应被拒")


def module_source(name):
    """Task 4 模块源码：用来钉语句序列与“谁不得调谁”。"""
    module = importlib.import_module(f"bi_agent.exploration.{name}")
    return pathlib.Path(module.__file__).read_text(encoding="utf-8")


class GateScriptTransaction:
    """计数用的事务上下文：证明每次调用自开自关一个。"""

    def __init__(self, conn):
        self._conn = conn

    def __enter__(self):
        self._conn.entered += 1
        return self

    def __exit__(self, exc_type, exc, traceback):
        self._conn.exited += 1
        return False


class GateScriptResult:
    """假结果：EXPLAIN 给一行 JSON，SELECT 给 description 与 fetchmany。"""

    def __init__(self, *, explain=None, description=(), rows=()):
        self._explain = explain
        self.description = [types.SimpleNamespace(name=name) for name in description]
        self._rows = list(rows)
        self.sizes = []

    def fetchone(self):
        return None if self._explain is None else (self._explain,)

    def fetchmany(self, size):
        self.sizes.append(size)
        return self._rows[:size]


class GateScriptConn:
    """只钉“门禁发了哪几句、事务开合几次”的假连接。

    真 EXPLAIN 数字、真只读、真权限一律走 `ExplorationRepositoryTests` 的真库用例；
    本替身只用来把语句序列与 `limit+1` 形状钉成字面量。
    """

    def __init__(self, *, explain_rows=18, explain_cost=647.04, description=(), rows=(),
                 fail_with=None):
        self.explain = [{"Plan": {"Plan Rows": explain_rows, "Total Cost": explain_cost}}]
        self.description = list(description)
        self.rows = list(rows)
        self.fail_with = fail_with
        self.statements = []
        self.parameters = []
        self.entered = 0
        self.exited = 0
        self.result = None

    def transaction(self):
        return GateScriptTransaction(self)

    def execute(self, sql, params=None):
        self.statements.append(sql)
        self.parameters.append(params)
        if self.fail_with is not None:
            raise self.fail_with
        explained = sql.startswith(EXPLAIN_STATEMENT_PREFIX)
        self.result = GateScriptResult(explain=self.explain if explained else None,
                                       description=() if explained else self.description,
                                       rows=() if explained else self.rows)
        return self.result


class GateProbeConn:
    """把**真库**连接包一层：在门禁自己的事务里反问一句“现在真的只读吗、超时多少”。

    不替被测代码执行任何它自己要发的语句：`transaction()` 与三条语句都原样落到真库，
    探针只是同一事务里额外两句 `SHOW` 与一次必须被拒的写入。
    """

    def __init__(self, conn):
        self.conn = conn
        self.statements = []
        self.read_only = None
        self.timeout = None
        self.write_refused = False
        self._probed = False

    def transaction(self):
        return self.conn.transaction()

    def execute(self, sql, params=None):
        self.statements.append(sql)
        first_real = (sql.startswith(EXPLAIN_STATEMENT_PREFIX) or sql.startswith("SELECT"))
        if first_real and not self._probed:
            self._probed = True
            self.read_only = str(self.conn.execute(
                "SHOW transaction_read_only").fetchone()[0])
            self.timeout = str(self.conn.execute("SHOW statement_timeout").fetchone()[0])
            try:
                with self.conn.transaction():
                    self.conn.execute(
                        "INSERT INTO bi.app_chats(id, subject_id, title) "
                        "VALUES (%s, 'probe', '探针')", (uuid4(),))
            except psycopg.errors.ReadOnlySqlTransaction:
                self.write_refused = True
            else:
                raise AssertionError("门禁事务里的写入被接受了：READ ONLY 没生效")
        return self.conn.execute(sql, params)


class ExplorationLivePlanFixture:
    """真库上的正例计划：店铺与窗口都从 `reporting.v_product_cost_daily` 现有数据里取。

    不写死店号与日期：本层要证明的是“门禁按真实 EXPLAIN 数字放行/拦下”，不是“某个特定
    店在某个特定周恰好有数据”。没数据就直接判红，不 skip（共享测试库必须已 seeding）。
    """

    def setUp(self):
        super().setUp()
        self.conn = connect_test_db(self)
        row = self.conn.execute(
            "SELECT shop_id, min(day), max(day) FROM reporting.v_product_cost_daily"
            " GROUP BY shop_id ORDER BY count(distinct day) DESC, shop_id LIMIT 1"
        ).fetchone()
        assert row is not None, "测试库里没有 v_product_cost_daily 数据：执行层无法取证"
        self.shop_id, first, last = str(row[0]), row[1], row[2]
        assert (last - first).days + 1 <= 300, f"窗口超出预算：{first}..{last}"
        self.start, self.end = first, last + timedelta(days=1)
        from bi_agent.catalog import ref_for_key

        self.shop_ref = ref_for_key("shop", self.shop_id)

    def deadline(self) -> float:
        """与固定指标查询同一形状：`time.monotonic()` 上的绝对时刻。"""
        return time.monotonic() + 30.0

    def plan_for(self, metrics=("metric-cost-total",), groups=(DAY_COST, SHOP_COST),
                 limit=100):
        """编译→策略→可执行计划：与 Task 5 将要接的序列一致。"""
        draft = compile_for(metrics, groups=list(groups),
                            allowed_shop_ids=frozenset({self.shop_id}),
                            start=self.start, end=self.end, limit=limit)
        return validate(draft, context=policy_context(
            allowed_shop_ids=frozenset({self.shop_id})))

    def explain_root(self, plan):
        """独立重现一次 EXPLAIN：门禁报的数必须与它逐项相等，而不是自说自话。"""
        with self.conn.transaction():
            self.conn.execute(READ_ONLY_STATEMENT)
            self.conn.execute(STATEMENT_TIMEOUT_STATEMENT)
            row = self.conn.execute(EXPLAIN_STATEMENT_PREFIX + plan.sql_text,
                                    plan.parameters).fetchone()
        return row[0][0]["Plan"]


class ExplorationContractTests(unittest.TestCase):
    # --- 计划 Task 1 Step 1 的种子用例 -------------------------------------------

    def test_controlled_sql_requires_semantic_catalog(self):
        """计划 Task 1 Step 1 种子用例：目录关着不许开受控 SQL。"""
        from bi_agent.config import load_app_settings

        env = valid_app_env() | {
            "SEMANTIC_CATALOG_ENABLED": "false",
            "CONTROLLED_SQL_ENABLED": "true",
        }
        with self.assertRaisesRegex(ValueError, "CONTROLLED_SQL_REQUIRES_SEMANTIC_CATALOG"):
            load_app_settings(env)
        # 稳定原因码本身：不回显 DSN，不夹带别的文字。
        with self.assertRaises(ValueError) as caught:
            load_app_settings(env)
        self.assertEqual(str(caught.exception), "CONTROLLED_SQL_REQUIRES_SEMANTIC_CATALOG")
        self.assertNotIn("postgresql", str(caught.exception))

    def test_request_accepts_refs_and_rejects_sql_identifiers(self):
        from bi_agent.exploration.models import ExplorationRequest

        request = ExplorationRequest(
            question="按店铺和日期看有成本销售额",
            start="2026-09-01", end="2026-09-08",
            entity_refs=["entity-shop"],
            requested_metric_refs=["metric-cost-total"],
            group_by_field_refs=["field-shop-id", "field-day"], limit=100)
        self.assertEqual(request.limit, 100)
        with self.assertRaises(ValueError):
            ExplorationRequest(
                question="x", start="2026-09-01", end="2026-09-08",
                entity_refs=["reporting.v_shop_daily"],
                requested_metric_refs=["SUM(paid_amount)"],
                group_by_field_refs=[], limit=501)
        # 种子请求被接受的那部分要按值钉住，"没抛异常"不是断言。
        self.assertEqual(request.question, "按店铺和日期看有成本销售额")
        self.assertEqual((request.start, request.end), (date(2026, 9, 1), date(2026, 9, 8)))
        self.assertEqual(request.entity_refs, ["entity-shop"])
        self.assertEqual(request.requested_metric_refs, ["metric-cost-total"])
        self.assertEqual(request.group_by_field_refs, ["field-shop-id", "field-day"])
        # 上面那次整体失败里，三个字段各自都被拒了一次（错误定位精确到列表下标）。
        self.assertEqual(
            sorted(locs_of(ExplorationRequest, request_values(
                question="x", entity_refs=["reporting.v_shop_daily"],
                requested_metric_refs=["SUM(paid_amount)"],
                group_by_field_refs=[], limit=501))),
            [("entity_refs", 0), ("limit",), ("requested_metric_refs", 0)])

    def test_canonical_contracts_accept_safe_values(self):
        """五个契约各有一份能过的安全输入：后面的拒绝用例因此不是"永远红"。"""
        from bi_agent.exploration.models import (
            ExplorationColumn, ExplorationRequest, ExplorationResult, SqlDraft,
            ValidatedQueryPlan)

        self.assertEqual(make_request().model_dump()["limit"], 100)
        draft = SqlDraft(**draft_values())
        self.assertEqual(sorted(draft.parameters), ["allowed_shop_ids", "limit"])
        self.assertEqual(len(draft.selected_refs), 3)
        plan = ValidatedQueryPlan(**plan_values())
        self.assertEqual(plan.statement_fingerprint, SHA256_HEX)
        self.assertIsNone(plan.estimated_rows)
        self.assertIsNone(plan.estimated_total_cost)
        self.assertEqual(plan.warnings, [])
        column = ExplorationColumn(**column_values())
        self.assertEqual((column.ref, column.data_type), ("metric-cost-total", "decimal"))
        result = ExplorationResult(**result_values())
        self.assertEqual([item.ref for item in result.columns],
                         ["field-day", "metric-cost-total"])
        self.assertEqual(result.rows, [{"field-day": "2026-09-01",
                                        "metric-cost-total": "12.30"}])
        self.assertTrue(issubclass(ExplorationRequest, BaseModel))

    def test_model_json_round_trip_keeps_the_contract_shape(self):
        """Task 5 要从模型 JSON 校验请求：JSON 数字合法，字符串数字不合法。"""
        from bi_agent.exploration.models import ExplorationRequest

        parsed = ExplorationRequest.model_validate_json(json.dumps(request_values()))
        self.assertEqual(parsed, make_request())
        self.assertEqual(locs_of(ExplorationRequest, {**request_values(), "limit": "100"}),
                         [("limit",)])
        self.assertEqual(
            [tuple(err["loc"]) for err in error_of(
                ExplorationRequest, {**request_values(), "question": 2026})],
            [("question",)])

    # --- ExplorationRequest 边界 -------------------------------------------------

    def test_question_must_be_nonblank_and_bounded(self):
        self.assertEqual(make_request(question="按店铺看成本").question, "按店铺看成本")
        self.assertEqual(make_request(question="问" * 4000).question, "问" * 4000)
        # 只判非空白：不静默改用户原文（改了就对不上诊断里的提问原文）。
        self.assertEqual(make_request(question=" 按店铺看成本 ").question,
                         " 按店铺看成本 ")
        for bad in ("", " ", "  \t\n ", "\u3000", "问" * 4001, 20260901, None, ["问"]):
            with self.subTest(bad=repr(bad)):
                self._assert_request_rejected("question", bad)

    def test_start_and_end_are_required_together(self):
        self.assertEqual(self._window_rejection(start="2026-09-01", end=None),
                         "exploration_window_incomplete")
        self.assertEqual(self._window_rejection(start=None, end="2026-09-08"),
                         "exploration_window_incomplete")
        both_none = make_request(start=None, end=None)
        self.assertEqual((both_none.start, both_none.end), (None, None))

    def test_window_is_half_open_and_bounded_to_366_days(self):
        start = date(2026, 1, 1)
        for ok_days in (1, 2, 365, 366):
            with self.subTest(ok_days=ok_days):
                self.assertEqual(make_request(start=start,
                                              end=start + timedelta(days=ok_days)).end,
                                 start + timedelta(days=ok_days))
        self.assertEqual(self._window_rejection(start=start, end=start),
                         "exploration_window_ordered")
        self.assertEqual(self._window_rejection(start=start,
                                                end=start - timedelta(days=1)),
                         "exploration_window_ordered")
        self.assertEqual(self._window_rejection(start=start,
                                                end=start + timedelta(days=367)),
                         "exploration_window_too_large")
        self.assertEqual(self._window_rejection(start="2026-09-08", end="2026-09-01"),
                         "exploration_window_ordered")
        self.assertEqual(self._window_rejection(start="2026-01-01", end="2027-01-03"),
                         "exploration_window_too_large")
        # 日期本身仍是字段级校验：坏日期不会伪装成"窗口不对"。
        self.assertEqual(request_locs(start="not-a-date"), [("start",)])

    def test_limit_is_an_integer_from_one_to_five_hundred(self):
        for ok in (1, 2, 499, 500):
            with self.subTest(ok=ok):
                self.assertEqual(make_request(limit=ok).limit, ok)
        self.assertEqual(make_request().limit, 100)            # 计划默认值
        for bad in (0, -1, 501, 10_000, True, False, "100", "1e2", 100.0, 99.5, None,
                    [], {"limit": 1}):
            with self.subTest(bad=repr(bad)):
                self._assert_request_rejected("limit", bad)

    def test_list_sizes_are_bounded_per_field(self):
        def refs(stem: str, count: int) -> list[str]:
            return [f"{stem}-{n}" for n in range(count)]

        self.assertEqual(make_request(entity_refs=refs("entity", 5)).entity_refs,
                         refs("entity", 5))
        self.assertEqual(make_request(requested_metric_refs=refs("metric", 8)
                                      ).requested_metric_refs, refs("metric", 8))
        self.assertEqual(make_request(group_by_field_refs=refs("field", 4)
                                      ).group_by_field_refs, refs("field", 4))
        self.assertEqual(make_request(entity_refs=[]).entity_refs, [])
        self.assertEqual(make_request(group_by_field_refs=[]).group_by_field_refs, [])
        # 指标非空：没有指标的探索不是一个查询。
        self._assert_request_rejected("requested_metric_refs", [])
        for field, stem, count in (("entity_refs", "entity", 6),
                                   ("requested_metric_refs", "metric", 9),
                                   ("group_by_field_refs", "field", 5)):
            with self.subTest(field=field):
                self._assert_request_rejected(field, refs(stem, count))

    def test_duplicate_refs_are_rejected_not_silently_deduplicated(self):
        """去重后长度变了就是"两个指标变成一个指标"：当场拒，不悄悄少查一项。"""
        unique = ["metric-cost-total", "metric-quantity"]
        self.assertEqual(make_request(requested_metric_refs=unique).requested_metric_refs,
                         unique)                     # 顺序与长度原样保留
        self.assertEqual(make_request(entity_refs=["entity-shop", "entity-order"]
                                      ).entity_refs, ["entity-shop", "entity-order"])
        self.assertEqual(make_request(group_by_field_refs=["field-day", "field-shop-id"]
                                      ).group_by_field_refs, ["field-day", "field-shop-id"])
        for field, duplicate in (
                ("entity_refs", ["entity-shop", "entity-shop"]),
                ("requested_metric_refs", ["metric-cost-total", "metric-cost-total"]),
                ("group_by_field_refs", ["field-day", "field-day"])):
            with self.subTest(field=field):
                self._assert_request_rejected(field, duplicate)

    def test_only_stable_semantic_refs_are_accepted(self):
        """ref 规则的唯一真源是语义目录：SQL 标识符、表达式、下划线拼法都进不来。"""
        from bi_agent.semantic_catalog.models import _require_ref

        for ref in ("entity-shop", "metric-cost-total", "field-shop-id", "field-day",
                    "view-product-cost-daily", "join-shop-daily-shops"):
            with self.subTest(ref=ref):
                _require_ref(ref)          # 规则本身认它，探索层就必须认它
                self.assertEqual(make_request(entity_refs=[ref]).entity_refs, [ref])
        for bad in ("reporting.v_shop_daily", "v_shop_daily", "SUM(paid_amount)",
                    "field_shop_id", "cost_total", "Entity-shop", "entity shop",
                    " entity-shop", "entity-shop ", "", "1", "entity-shop;x",
                    "DROP TABLE bi.orders", "metric-$sum", None, 7):
            with self.subTest(bad=repr(bad)):
                self._assert_request_rejected("requested_metric_refs", [bad])
        # 下标进 loc：才知道列表里哪一项被拒。
        self.assertEqual(request_locs(entity_refs=["entity-shop", "shop_id"]),
                         [("entity_refs", 1)])

    def test_request_rejects_sql_authorization_and_invented_fields(self):
        """模型不许递 SQL、店号或授权集合进来：那些只由服务端决定（总设计 §3）。"""
        from bi_agent.exploration.models import ExplorationRequest

        for key, value in (
                ("sql", "SELECT 1"),
                ("sql_text", draft_values()["sql_text"]),
                ("shop_ids", ["S1"]),
                ("allowed_shop_ids", ["S1"]),
                ("authorization_column", "shop_id"),
                ("view_ref", "view-product-cost-daily"),
                ("requested_metrics", ["metric-cost-total"]),
                ("group_by_refs", ["field-day"]),
                ("max_limit", 500),
                ("aggregate", "sum")):
            with self.subTest(key=key):
                errors = error_of(ExplorationRequest, {**request_values(), key: value})
                self.assertEqual([tuple(err["loc"]) for err in errors], [(key,)])
                self.assertEqual(errors[0]["type"], "extra_forbidden")

    def test_contracts_are_frozen_and_hide_inputs(self):
        from bi_agent.exploration.models import (
            ExplorationColumn, ExplorationRequest, ExplorationResult, SqlDraft,
            ValidatedQueryPlan)

        instances = [
            ExplorationRequest(**request_values()),
            SqlDraft(**draft_values()),
            ValidatedQueryPlan(**plan_values()),
            ExplorationColumn(**column_values()),
            ExplorationResult(**result_values()),
        ]
        self.assertEqual(len(instances), len(EXPORTED_CONTRACTS))
        for instance in instances:
            with self.subTest(model=type(instance).__name__):
                config = type(instance).model_config
                self.assertEqual(config.get("extra"), "forbid")
                self.assertIs(config.get("frozen"), True)
                self.assertIs(config.get("hide_input_in_errors"), True)
                field = next(iter(type(instance).model_fields))
                with self.assertRaises(ValidationError):
                    setattr(instance, field, "mutated")

    # --- SqlDraft / ValidatedQueryPlan ------------------------------------------

    def test_sql_draft_keeps_text_and_parameters_apart(self):
        """草案是"文本 + 具名参数"两段：真值不许混进 SQL 文本。"""
        from bi_agent.exploration.models import SqlDraft

        draft = SqlDraft(**draft_values())
        self.assertEqual(draft.parameters["allowed_shop_ids"], ["S1", "S2"])
        self.assertEqual(draft.parameters["limit"], 100)
        self.assertEqual(SqlDraft(**draft_values(
            parameters={"nested": {"a": [1, Decimal("2")]}, "limit": 1}
        )).parameters["nested"], {"a": [1, Decimal("2")]})
        for bad in (["S1"], "limit=1", None, 100):
            with self.subTest(bad=repr(bad)):
                self.assertEqual(locs_of(SqlDraft, draft_values(parameters=bad)),
                                 [("parameters",)])
        for bad in ("", "   ", "\n\t "):
            with self.subTest(bad=repr(bad)):
                self.assertEqual(locs_of(SqlDraft, draft_values(sql_text=bad)),
                                 [("sql_text",)])
        for bad in (["shop_id"], ["field-day", ""], ["field-day", 7], "field-day", None):
            with self.subTest(bad=repr(bad)):
                self.assertTrue(all(loc[0] == "selected_refs" for loc in locs_of(
                    SqlDraft, draft_values(selected_refs=bad))),
                    str(locs_of(SqlDraft, draft_values(selected_refs=bad))))
        for field in ("sql_text", "parameters", "selected_refs"):
            with self.subTest(missing=field):
                values = draft_values()
                del values[field]
                self.assertIn((field,), locs_of(SqlDraft, values))

    def test_plan_requires_a_sha256_fingerprint_and_version_identifiers(self):
        from bi_agent.exploration.models import ValidatedQueryPlan

        self.assertEqual(ValidatedQueryPlan(**plan_values(
            statement_fingerprint="0123456789abcdef" * 4)).statement_fingerprint,
            "0123456789abcdef" * 4)
        for bad in ("", "a" * 63, "a" * 65, "A" * 64, "SELECT", "g" * 64,
                    f"sha256:{SHA256_HEX}", None, 7):
            with self.subTest(fingerprint=repr(bad)):
                self.assertEqual(locs_of(
                    ValidatedQueryPlan,
                    plan_values(statement_fingerprint=bad)),
                    [("statement_fingerprint",)])
        for field in ("template_version", "catalog_version"):
            for bad in ("", "  ", "exploration sql/2026-09-14.1",
                        " exploration-sql/2026-09-14.1", "exploration-sql/2026-09-14.1 ",
                        "\n", 7, None):
                with self.subTest(field=field, bad=repr(bad)):
                    self.assertEqual(
                        locs_of(ValidatedQueryPlan, plan_values(**{field: bad})),
                        [(field,)])

    def test_plan_estimate_fields_are_bounded_and_finite(self):
        from bi_agent.exploration.models import ValidatedQueryPlan

        plan = ValidatedQueryPlan(**plan_values(
            estimated_rows=50_000, estimated_total_cost=Decimal("1234.500000"),
            warnings=["cost_above_advisor"]))
        self.assertEqual(plan.estimated_rows, 50_000)
        self.assertEqual(plan.estimated_total_cost, Decimal("1234.500000"))
        self.assertEqual(plan.warnings, ["cost_above_advisor"])
        self.assertEqual(ValidatedQueryPlan(**plan_values(
            estimated_rows=0)).estimated_rows, 0)
        self.assertEqual(ValidatedQueryPlan(**plan_values(
            estimated_total_cost=Decimal("0"))).estimated_total_cost, Decimal("0"))
        self.assertIsNone(ValidatedQueryPlan(**plan_values()).estimated_total_cost)
        for bad in (-1, True, False, "5", 5.0, 1.5, [], object()):
            with self.subTest(estimated_rows=repr(bad)):
                self.assertEqual(
                    locs_of(ValidatedQueryPlan, plan_values(estimated_rows=bad)),
                    [("estimated_rows",)])
        for bad in (Decimal("-0.000001"), Decimal("NaN"), Decimal("sNaN"),
                    Decimal("Infinity"), Decimal("-Infinity"), float("nan"),
                    float("inf"), "NaN", "Infinity", "abc", True, -1, []):
            with self.subTest(estimated_total_cost=repr(bad)):
                self.assertEqual(
                    locs_of(ValidatedQueryPlan,
                            plan_values(estimated_total_cost=bad)),
                    [("estimated_total_cost",)])
        self.assertEqual(locs_of(ValidatedQueryPlan, plan_values(warnings="cost_high")),
                         [("warnings",)])
        self.assertTrue(all(loc[0] == "warnings" for loc in locs_of(
            ValidatedQueryPlan, plan_values(warnings=[1]))))
        self.assertEqual(ValidatedQueryPlan(**plan_values(
            estimated_total_cost="100000")).estimated_total_cost, Decimal("100000"))

    # --- ExplorationColumn / ExplorationResult ----------------------------------

    def test_result_columns_use_the_catalog_data_type_vocabulary(self):
        from bi_agent.exploration.models import ExplorationColumn
        from bi_agent.semantic_catalog.models import DataType

        self.assertEqual(sorted(typing.get_args(DataType)),
                         ["boolean", "date", "datetime", "decimal", "integer", "ref",
                          "text"])
        for data_type in sorted(typing.get_args(DataType)):
            with self.subTest(data_type=data_type):
                self.assertEqual(ExplorationColumn(**column_values(
                    data_type=data_type)).data_type, data_type)
        for bad in ("DECIMAL", "", "money", "array", "varchar(1040)", None, 3):
            with self.subTest(bad=repr(bad)):
                self.assertEqual(
                    locs_of(ExplorationColumn, column_values(data_type=bad)),
                    [("data_type",)])
        for bad in ("cost_total", "reporting.v_product_cost_daily.cost_total", "", None):
            with self.subTest(ref=repr(bad)):
                self.assertEqual(locs_of(ExplorationColumn, column_values(ref=bad)),
                                 [("ref",)])

    def test_result_rows_are_capped_and_values_stay_structured(self):
        from bi_agent.exploration.models import ExplorationResult

        self.assertEqual(len(ExplorationResult(**result_values(
            rows=[{"field-day": "2026-09-01"}] * 500)).rows), 500)
        self.assertEqual(ExplorationResult(**result_values(rows=[])).rows, [])
        self.assertEqual(
            locs_of(ExplorationResult,
                    result_values(rows=[{"field-day": "x"}] * 501)),
            [("rows",)])
        rows = [{"field-day": date(2026, 9, 1), "metric-cost-total": Decimal("12.30"),
                 "flag": True, "nothing": None}]
        result = ExplorationResult(**result_values(rows=rows))
        self.assertEqual(result.rows[0]["metric-cost-total"], Decimal("12.30"))
        self.assertIs(result.rows[0]["flag"], True)
        self.assertIsNone(result.rows[0]["nothing"])
        for bad in (["field-day"], "rows", None, 1):
            with self.subTest(bad=repr(bad)):
                errors = locs_of(ExplorationResult, result_values(rows=bad))
                self.assertTrue(errors)
                self.assertTrue(all(loc[0] == "rows" for loc in errors), str(errors))
        for field in ("basis", "coverage", "diagnostics", "limitations"):
            with self.subTest(field=field):
                self.assertEqual(locs_of(ExplorationResult, result_values(**{field: None})),
                                 [(field,)])

    def test_result_required_fields_and_nested_shapes(self):
        from bi_agent.exploration.models import ExplorationResult

        for field in ("template_version", "catalog_version", "statement_fingerprint",
                      "columns", "rows", "basis", "coverage", "diagnostics",
                      "limitations"):
            with self.subTest(missing=field):
                values = result_values()
                del values[field]
                self.assertIn((field,), locs_of(ExplorationResult, values))
        self.assertEqual(
            [loc for loc in locs_of(ExplorationResult, result_values(
                basis=[{"metric": "metric-cost-total", "basis": 7.5}]))],
            [("basis", 0, "basis")])
        self.assertEqual(locs_of(ExplorationResult, result_values(limitations=[None])),
                         [("limitations", 0)])
        self.assertEqual(
            locs_of(ExplorationResult,
                    result_values(columns=["metric-cost-total"])),
            [("columns", 0)])
        self.assertEqual(locs_of(ExplorationResult,
                                 result_values(coverage=["status"])),
                         [("coverage",)])
        self.assertEqual(locs_of(ExplorationResult, result_values(
            statement_fingerprint="zz")), [("statement_fingerprint",)])
        self.assertEqual(locs_of(ExplorationResult,
                                 result_values(catalog_version="semantic 2026-09-14")),
                         [("catalog_version",)])
        self.assertEqual(ExplorationResult(**result_values(
            coverage={"status": "partial", "gaps": [{"start": "2026-09-03"}],
                      "estimated": 1})).coverage["gaps"], [{"start": "2026-09-03"}])

    # --- 包边界 ------------------------------------------------------------------

    def test_package_exports_the_task_one_and_two_surface(self):
        import bi_agent.exploration as exploration
        import bi_agent.exploration.compiler as compiler
        import bi_agent.exploration.eligibility as eligibility
        import bi_agent.exploration.models as models

        expected = set(EXPORTED_CONTRACTS) | set(TASK_TWO_EXPORTS)
        self.assertEqual(list(exploration.__all__), sorted(expected))
        namespace = vars(exploration)
        public = {name for name, value in namespace.items()
                  if not name.startswith("_")
                  and not isinstance(value, types.ModuleType)}
        self.assertEqual(public, expected)
        for name in EXPORTED_CONTRACTS:
            self.assertIs(namespace[name], getattr(models, name))
            self.assertTrue(issubclass(getattr(models, name), BaseModel), name)
        # Task 2 的两个入口只能住在自己的模块里：包不拄第二份实现。
        self.assertIs(namespace["compile_query"], compiler.compile_query)
        self.assertIs(namespace["fixed_tool_for"], eligibility.fixed_tool_for)
        self.assertIs(namespace["FIXED_TOOL_METRICS"], eligibility.FIXED_TOOL_METRICS)
        # models.py 里公开的可调用对象：五个契约类 + Task 4 的那一个预算异常。
        public_models_callables = sorted(
            name for name, value in vars(models).items()
            if callable(value)
            and getattr(value, "__module__", "") == models.__name__
            and not name.startswith("_"))
        self.assertEqual(public_models_callables,
                         sorted(set(EXPORTED_CONTRACTS) | {BUDGET_EXCEPTION}))
        budget_exception = getattr(models, BUDGET_EXCEPTION)
        self.assertTrue(issubclass(budget_exception, ValueError))
        self.assertFalse(issubclass(budget_exception, BaseModel))
        # Task 4 的执行/投影入口不得进包面：计划的 Files 清单里没有 `__init__.py`。
        for module_name in TASK_FOUR_MODULE_NAMES:
            module = importlib.import_module(f"bi_agent.exploration.{module_name}")
            for name, owner in TASK_FOUR_ENTRY_POINTS.items():
                self.assertFalse(hasattr(exploration, name), name)
                self.assertIs(hasattr(module, name), owner == module_name, f"{name}@{owner}")

    def test_package_ships_only_the_task_one_to_four_modules(self):
        """目录列表钉住：Task 5 的运行域/工具文件不能提前偷渡。"""
        import bi_agent.exploration as exploration

        shipped = sorted(path.name for path in
                         pathlib.Path(exploration.__file__).parent.glob("*.py"))
        self.assertEqual(shipped, sorted(PACKAGE_MODULES))

    def test_this_slice_ships_the_policy_entry_point_and_nothing_else(self):
        """策略入口只到 `validate_exploration_plan`；执行/投影已交，工具仍属 Task 5。

        计划在 Task 3 的 Files 清单里没有 `exploration/__init__.py`，所以策略入口**不得**
        出现在包级公共面上（Task 5 接运行域时再统一接）；本用例同时钉住这两件事。
        """
        import bi_agent.exploration as exploration
        import bi_agent.exploration.compiler as compiler
        import bi_agent.exploration.eligibility as eligibility
        import bi_agent.exploration.models as models
        import bi_agent.exploration.policy as policy

        holders = (exploration, models, compiler, eligibility, policy)
        for name in NOT_YET_IMPLEMENTED:
            with self.subTest(name=name):
                for holder in holders:
                    self.assertFalse(hasattr(holder, name), f"{holder.__name__}.{name}")
        # 入口只住在 policy 里，包面逐字不变。
        self.assertTrue(callable(getattr(policy, TASK_THREE_ENTRY_POINT, None)))
        self.assertFalse(hasattr(exploration, TASK_THREE_ENTRY_POINT))
        self.assertFalse(hasattr(models, TASK_THREE_ENTRY_POINT))
        self.assertFalse(hasattr(compiler, TASK_THREE_ENTRY_POINT))
        self.assertFalse(hasattr(eligibility, TASK_THREE_ENTRY_POINT))
        # 入口的形状：`validate_exploration_plan(draft, *, selection, context)`。
        # 模块带 `from __future__ import annotations`，所以这里比注解名字而不是对象：
        # 计划 Interfaces 里写的消费方就是这三个契约，换一个都得改测试。
        parameters = inspect.signature(policy.validate_exploration_plan).parameters
        self.assertEqual(list(parameters), ["draft", "selection", "context"])
        self.assertTrue(parameters["draft"].kind is inspect.Parameter.POSITIONAL_OR_KEYWORD)
        self.assertTrue(parameters["selection"].kind is inspect.Parameter.KEYWORD_ONLY)
        self.assertTrue(parameters["context"].kind is inspect.Parameter.KEYWORD_ONLY)
        self.assertEqual({name: str(parameters[name].annotation) for name in parameters},
                         {"draft": "SqlDraft", "selection": "SemanticSelection",
                          "context": "DomainContext"})
        self.assertEqual(policy.validate_exploration_plan.__annotations__["return"],
                         "ValidatedQueryPlan")
        # 本切片不连库、不执行：入口只有一个，`conn` / `store` 从头到尾不被访问
        # （见 RefusingConn：碰一下就判红）。
        self.assertFalse(hasattr(policy, "estimate_plan"))
        self.assertFalse(hasattr(policy, "execute"))
        self.assertFalse(hasattr(policy, "execute_plan"))

    # --- helpers ----------------------------------------------------------------

    def _assert_request_rejected(self, field, value):
        from bi_agent.exploration.models import ExplorationRequest

        errors = error_of(ExplorationRequest, request_values(**{field: value}))
        self.assertTrue(errors, f"{field}={value!r} was accepted")
        for error in errors:
            self.assertEqual(error["loc"][:1], (field,),
                             f"{field}={value!r} rejected elsewhere: {error}")

    def _window_rejection(self, **overrides):
        """跨字段规则报在模型根上：返回稳定原因码，不回显输入。"""
        from bi_agent.exploration.models import ExplorationRequest

        errors = error_of(ExplorationRequest, request_values(**overrides))
        self.assertEqual([tuple(err["loc"]) for err in errors], [()])
        self.assertEqual(errors[0]["type"], "value_error")
        self.assertNotIn("2026-", errors[0]["msg"])   # 不把窗口取值写进错误
        return errors[0]["msg"].removeprefix("Value error, ")


class ExplorationCompilerTests(unittest.TestCase):
    """计划 Task 2 Step 1/3/5：固定 Tool 优先与服务端确定性编译器。"""

    # --- 计划 Task 2 Step 1 的两条种子用例 ---------------------------------------

    def test_fixed_paid_amount_query_is_not_eligible(self):
        from bi_agent.exploration.eligibility import fixed_tool_for

        self.assertEqual(
            fixed_tool_for(selection_for("metric-paid-amount"),
                           request_for("metric-paid-amount")),
            "query_business",
        )
        # 反向护栏：没有任何固定 Tool 的契约能同时表达这个指标与这个粒度时，不许谎报覆盖
        # （`analyze_product_performance` 能拆行类型，但它的指标集里没有支付金额）。
        self.assertIsNone(fixed_tool_for(
            selection_for("metric-paid-amount", groups=[LINE_KIND_COST]),
            request_for("metric-paid-amount", groups=[LINE_KIND_COST])))

    def test_compiler_injects_scope_and_never_accepts_identifier_input(self):
        draft = compile_for("metric-cost-total", groups=[DAY_COST])
        # 计划种子按未加引号的模板文本写；只去掉双引号再逐字比。
        plain = dequoted(draft.sql_text)
        self.assertIn("reporting.v_product_cost_daily", plain)
        self.assertIn("shop_id = ANY(%(allowed_shop_ids)s)", plain)
        self.assertNotIn("S1", draft.sql_text)
        self.assertEqual(draft.parameters["allowed_shop_ids"], ["S1", "S2"])
        # 计划 Step 4：真形状逐字带双引号，去引号只看模板词序。
        self.assertIn('FROM "reporting"."v_product_cost_daily" AS fact', draft.sql_text)
        self.assertIn('WHERE fact."shop_id" = ANY(%(allowed_shop_ids)s)', draft.sql_text)

    # --- 固定 Tool 覆盖矩阵 ------------------------------------------------------

    def test_fixed_tool_metrics_are_exactly_the_plan_table(self):
        from bi_agent.exploration.eligibility import FIXED_TOOL_METRICS

        self.assertEqual({tool: set(refs) for tool, refs in FIXED_TOOL_METRICS.items()},
                         {tool: set(refs) for tool, refs in PLAN_FIXED_TOOL_METRICS.items()})
        # 重叠时的优先级 = 计划里 Tool 的书写顺序：既不是字母序也不是偶然的插入序。
        self.assertEqual(list(FIXED_TOOL_METRICS), list(PLAN_TOOL_ORDER))
        self.assertNotIn("evaluate_promotion", FIXED_TOOL_METRICS)     # 不新搭一个 Tool 覆盖
        with self.assertRaises(TypeError):
            FIXED_TOOL_METRICS["query_business"] = frozenset()        # 运行时改不了覆盖表

    # 分组维度词 → 目录里一个真属于该维度的字段 ref（只给表驱动测试用）。
    DIMENSION_REFS = {
        "day": DAY_SHOP_DAILY,
        "shop": SHOP_SHOP_DAILY,
        "product": PRODUCT_PRODUCT_DAILY,
        "platform": SHOPS_PLATFORM,
        "line_kind": LINE_KIND_COST,
        "pool": POOL_PHYSICAL,
    }

    def test_every_matrix_entry_is_claimed_by_the_tool_that_lists_it(self):
        """矩阵逐条过：每个 (Tool, 指标) 都要在该 Tool 能表达的一个粒度上被认出来。"""
        from bi_agent.exploration.eligibility import fixed_tool_for

        period = ["metric-paid-amount", "metric-paid-orders", "metric-erp-documents",
                  "metric-refund-amount", "metric-cash-difference"]
        probes = [("query_business", metric, []) for metric in period]
        probes += [("query_business", metric, ["product"]) for metric in
                   ("metric-quantity", "metric-product-paid-amount")]
        probes += [("analyze_product_performance", metric, [])
                   for metric in sorted(PLAN_FIXED_TOOL_METRICS["analyze_product_performance"])]
        probes += [("compare_performance", metric, ["platform"])
                   for metric in sorted(PLAN_FIXED_TOOL_METRICS["compare_performance"])]
        probes += [("audit_listing_prices", "metric-listing-price", ["shop"])]
        probes += [("inspect_inventory", "metric-physical-available-quantity", ["pool"]),
                   ("inspect_inventory", "metric-channel-sellable-quantity", ["shop"])]
        self.assertEqual({(tool, metric) for tool, metric, _dims in probes},
                         {(tool, metric) for tool, metrics in PLAN_FIXED_TOOL_METRICS.items()
                          for metric in metrics})
        for tool, metric, dimensions in probes:
            with self.subTest(tool=tool, metric=metric, dimensions=dimensions):
                groups = [self.DIMENSION_REFS[dimension] for dimension in dimensions]
                self.assertEqual(
                    fixed_tool_for(selection_for(metric, groups=groups),
                                   request_for(metric, groups=groups)), tool)

    def test_fixed_tool_group_support_comes_from_the_real_contracts(self):
        from bi_agent.exploration.eligibility import fixed_tool_for

        cases = (
            # (分组维度, 预期 Tool) —— `QueryRequest.group_by` 与 `ComparisonGroupBy` 的词表
            ([], "query_business"),
            (["day"], "query_business"),
            (["shop"], "query_business"),           # 重叠：矩阵顺序里 query_business 在前
            (["platform"], "compare_performance"),   # 只有它能按平台分组
            (["product"], None),                      # 支付金额是期间指标，不能按商品分组
            (["line_kind"], None),                    # 能拆行类型的 Tool 不持支付金额
            (["day", "shop"], None),                  # 两个 group_by 旋钮都是单值
        )
        for dimensions, expected in cases:
            with self.subTest(dimensions=dimensions):
                groups = [self.DIMENSION_REFS[dimension] for dimension in dimensions]
                self.assertEqual(
                    fixed_tool_for(selection_for("metric-paid-amount", groups=groups),
                                   request_for("metric-paid-amount", groups=groups)),
                    expected)
        # `PerformanceComparisonRequest.group_by` 是必填项：这个 Tool 没有"合计"那一档。
        # 拿只属于它的指标去探，无分组时必须递 None（带上分组则能认出来）。
        self.assertIsNone(fixed_tool_for(
            selection_for("metric-erp-gross-profit-reference"),
            request_for("metric-erp-gross-profit-reference")))
        self.assertEqual(fixed_tool_for(
            selection_for("metric-erp-gross-profit-reference", groups=[SHOP_SHOP_DAILY]),
            request_for("metric-erp-gross-profit-reference", groups=[SHOP_SHOP_DAILY])),
            "compare_performance")

    def test_overlapping_tools_are_decided_deterministically(self):
        from bi_agent.exploration.eligibility import fixed_tool_for

        metrics = ["metric-sales-amount", "metric-cost-total"]
        groups = [self.DIMENSION_REFS["shop"]]
        selection = selection_for(metrics, groups=groups)
        request = request_for(metrics, groups)
        first = fixed_tool_for(selection, request)
        self.assertEqual(first, "analyze_product_performance")     # 名字序里它在 compare 之前
        for _ in range(5):
            self.assertEqual(fixed_tool_for(selection, request), first)
        # 请求里指标的书写顺序不得改变结果。
        self.assertEqual(fixed_tool_for(selection, request_for(metrics[::-1], groups)), first)
        # 不重叠时也不能漏：平台分组只有 compare_performance 能表达。
        self.assertEqual(fixed_tool_for(
            selection_for(metrics, groups=[SHOPS_PLATFORM]),
            request_for(metrics, groups=[SHOPS_PLATFORM])), "compare_performance")

    def test_fixed_tool_wins_over_capability_coverage_and_selection_gaps(self):
        """缺能力 / 缺覆盖 / 要澄清都不是降级 SQL 的理由（计划 Task 2 Step 3）。"""
        from bi_agent.exploration.eligibility import fixed_tool_for

        blocked = selection_for("metric-paid-amount", missing_concepts=("promotion_spend",
                                                                        "net_profit"),
                               requires_clarification=True, view_refs=(), field_refs=(),
                               selected_metrics=())
        self.assertEqual(fixed_tool_for(blocked, request_for("metric-paid-amount")),
                         "query_business")
        # 同一个判断重复跑不得飘移（Task 5 的图会拿它做拒答依据）。
        self.assertEqual(fixed_tool_for(blocked, request_for("metric-paid-amount")),
                         "query_business")

    def test_fixed_tool_does_not_invent_coverage_for_unknown_refs(self):
        from bi_agent.exploration.eligibility import fixed_tool_for

        # 目录里没有的分组列：解不出维度，就不能宣称某个固定 Tool 能表达。
        self.assertIsNone(fixed_tool_for(
            selection_for("metric-paid-amount", groups=["field-shop-daily-nonexistent"]),
            request_for("metric-paid-amount", groups=["field-shop-daily-nonexistent"])))
        # 目录版本不认识时不猜分组维度；无分组不依赖目录，仍然按矩阵拒探索。
        stale = selection_for("metric-paid-amount", catalog_version="semantic/2020-01-01.1")
        self.assertIsNone(fixed_tool_for(stale, request_for("metric-paid-amount",
                                                           groups=[DAY_SHOP_DAILY])))
        self.assertEqual(fixed_tool_for(stale, request_for("metric-paid-amount")),
                         "query_business")

    def test_fixed_tool_rejects_foreign_input_shapes(self):
        from bi_agent.exploration.eligibility import fixed_tool_for

        with self.assertRaises(TypeError):
            fixed_tool_for(None, request_for("metric-paid-amount"))
        with self.assertRaises(TypeError):
            fixed_tool_for(selection_for("metric-paid-amount"),
                           {"requested_metric_refs": ["metric-paid-amount"]})

    # --- 编译器形状 --------------------------------------------------------------

    def test_grouped_query_matches_the_fixed_template(self):
        draft = compile_for("metric-cost-total", groups=[DAY_COST, SHOP_COST])
        self.assertEqual(draft.sql_text, "\n".join((
            'SELECT fact."day" AS "field_product_cost_daily_day", '
            'fact."shop_id" AS "_shop_id", '
            'sum(fact."cost_total") AS "metric_cost_total"',
            'FROM "reporting"."v_product_cost_daily" AS fact',
            'WHERE fact."shop_id" = ANY(%(allowed_shop_ids)s)',
            '  AND fact."day" >= %(start)s',
            '  AND fact."day" < %(end)s',
            'GROUP BY fact."day", fact."shop_id"',
            'ORDER BY fact."day", fact."shop_id"',
            'LIMIT %(limit)s',
        )))
        self.assertEqual(draft.selected_refs, sorted([
            COST_VIEW, "metric-cost-total", DAY_COST, SHOP_COST, FIELD_COST_TOTAL]))

    def test_total_query_omits_group_by_and_order_by(self):
        draft = compile_for("metric-cost-total")
        self.assertNotIn("GROUP BY", draft.sql_text)
        self.assertNotIn("ORDER BY", draft.sql_text)
        self.assertIn('SELECT sum(fact."cost_total") AS "metric_cost_total"', draft.sql_text)
        self.assertIn("LIMIT %(limit)s", draft.sql_text)
        # 只差分组：加上分组就两条子句都出现，不是模板里从来没有。
        grouped = compile_for("metric-cost-total", groups=[DAY_COST])
        self.assertIn("GROUP BY fact.\"day\"", grouped.sql_text)
        self.assertIn("ORDER BY fact.\"day\"", grouped.sql_text)

    def test_metrics_on_one_view_are_aggregated_in_a_single_pass(self):
        metrics = ["metric-cost-total", "metric-sales-amount"]
        draft = compile_for(metrics, groups=[DAY_COST])
        self.assertIn('sum(fact."cost_total") AS "metric_cost_total"', draft.sql_text)
        self.assertIn('sum(fact."sales_amount") AS "metric_sales_amount"', draft.sql_text)
        self.assertEqual(draft.sql_text.count("FROM "), 1)
        self.assertEqual(draft.sql_text.count("JOIN"), 0)
        self.assertEqual(draft.sql_text.count("SELECT"), 1)

    def test_output_aliases_are_identifier_safe(self):
        draft = compile_for("metric-cost-total", groups=[DAY_COST, SHOP_COST])
        aliases = re.findall(r'AS "([^"]+)"', draft.sql_text)
        self.assertEqual(aliases, ["field_product_cost_daily_day", "_shop_id",
                                   "metric_cost_total"])
        for alias in aliases:
            self.assertRegex(alias, r"^[a-z_][a-z0-9_]*$")
        # 授权列只能以下划线前缀输出，让投影层把它换成 opaque ref。
        self.assertNotIn('AS "field_product_cost_daily_shop_id"', draft.sql_text)

    def test_only_catalog_identifiers_are_quoted_in_the_sql(self):
        draft = compile_for("metric-cost-total", groups=[DAY_COST, SHOPS_PLATFORM])
        self.assertEqual(set(re.findall(r'"([^"]+)"', draft.sql_text)), {
            "reporting", "v_product_cost_daily", "v_shops", "day", "shop_id", "cost_total",
            "platform", "metric_cost_total", "field_product_cost_daily_day",
            "field_shops_platform"})
        # 稳定 ref 永远不进 SQL 文本：它们只被解成目录里的标识符。
        for ref in ("metric-cost-total", DAY_COST, SHOPS_PLATFORM):
            self.assertNotIn(ref, draft.sql_text)

    def test_compilation_is_deterministic_and_order_independent(self):
        forward = compile_for(["metric-sales-amount", "metric-cost-total"],
                              groups=[SHOP_COST, DAY_COST])
        backward = compile_for(["metric-cost-total", "metric-sales-amount"],
                               groups=[DAY_COST, SHOP_COST])
        self.assertEqual(forward.sql_text, backward.sql_text)
        self.assertEqual(forward.selected_refs, backward.selected_refs)
        self.assertEqual(forward.parameters, backward.parameters)
        again = compile_for(["metric-sales-amount", "metric-cost-total"],
                            groups=[SHOP_COST, DAY_COST])
        self.assertEqual(again.sql_text, forward.sql_text)

    # --- 编译器拒绝面 ------------------------------------------------------------

    def test_metrics_from_two_fact_grains_are_rejected(self):
        self.assertEqual(
            compile_error(["metric-cost-total", "metric-erp-gross-profit-reference"]),
            "exploration_multiple_fact_grains")
        # 只差一个指标：同基表的两个指标可以过（上面已测形状）。
        self.assertIn("metric_cost_total", compile_for(["metric-cost-total",
                                                       "metric-sales-amount"]).sql_text)

    def test_unregistered_join_is_rejected(self):
        # 商品成本 ↔ 单据毛利：目录里故意没有这条边。
        self.assertEqual(compile_error("metric-cost-total", groups=[STATUS_ERP]),
                         "exploration_join_not_registered")
        # 支付明细面上也没有到店铺档案的边：不能为了平台分组自己拼一条。
        self.assertEqual(compile_error("metric-payment-flow-amount", groups=[SHOPS_PLATFORM]),
                         "exploration_join_not_registered")
        # 只差视图：同一列在成本视图上走的是登记过的 N:1 边。
        self.assertIn("JOIN", compile_for("metric-cost-total",
                                         groups=[SHOPS_PLATFORM]).sql_text)

    def test_platform_join_uses_the_registered_edge_and_groups_only_platform(self):
        draft = compile_for("metric-cost-total", groups=[SHOPS_PLATFORM])
        self.assertEqual(draft.sql_text, "\n".join((
            'SELECT shops."platform" AS "field_shops_platform", '
            'sum(fact."cost_total") AS "metric_cost_total"',
            'FROM "reporting"."v_product_cost_daily" AS fact',
            'INNER JOIN "reporting"."v_shops" AS shops '
            'ON shops."shop_id" = fact."shop_id"',
            'WHERE fact."shop_id" = ANY(%(allowed_shop_ids)s)',
            '  AND fact."day" >= %(start)s',
            '  AND fact."day" < %(end)s',
            'GROUP BY shops."platform"',
            'ORDER BY shops."platform"',
            'LIMIT %(limit)s',
        )))
        # 一条 N:1 边、一次 JOIN、金额只在事实侧聚一次：不放大也不 N+1。
        self.assertEqual(draft.sql_text.count("JOIN"), 1)
        self.assertEqual(draft.sql_text.count("FROM "), 1)
        self.assertNotIn("CROSS", draft.sql_text)
        self.assertEqual(draft.selected_refs, sorted([
            COST_VIEW, SHOPS_VIEW, COST_SHOPS_JOIN, "metric-cost-total", SHOPS_PLATFORM,
            SHOP_COST, DAY_COST, FIELD_COST_TOTAL, "field-shops-shop-id"]))
        # 同基表上既按天又按平台：仍然只一条 JOIN。
        both = compile_for("metric-cost-total", groups=[SHOPS_PLATFORM, DAY_COST])
        self.assertIn('GROUP BY fact."day", shops."platform"', both.sql_text)
        self.assertEqual(both.sql_text.count("JOIN"), 1)

    def test_other_columns_of_the_joined_view_are_not_groupable(self):
        self.assertEqual(compile_error("metric-cost-total", groups=[SHOPS_CURRENCY]),
                         "exploration_join_not_registered")
        # 只差列：同一个视图上的平台列能走那条登记过的边。
        self.assertIn('GROUP BY shops."platform"', compile_for(
            "metric-cost-total", groups=[SHOPS_PLATFORM]).sql_text)

    def test_platform_grouping_needs_the_selection_to_have_picked_the_edge(self):
        """目录里有这条边、但本轮检索没选上：不自己补路径，直接拒。"""
        self.assertEqual(
            compile_error("metric-cost-total", groups=[SHOPS_PLATFORM],
                          selection=selection_for("metric-cost-total",
                                                 groups=[SHOPS_PLATFORM], joins=())),
            "exploration_join_not_selected")
        # 只差那一条边：选上了就能编。
        self.assertIn("INNER JOIN", compile_for("metric-cost-total",
                                                groups=[SHOPS_PLATFORM]).sql_text)

    def test_only_the_currently_published_catalog_is_compiled(self):
        """旧目录版本可以解释历史 Artifact，不能用来编新 SQL（总设计 §5.4）。"""
        stale = selection_for("metric-cost-total", catalog_version="semantic/2020-01-01.1")
        self.assertEqual(compile_error("metric-cost-total", selection=stale),
                         "exploration_catalog_version_mismatch")
        self.assertIn("FROM", compile_for("metric-cost-total").sql_text)

    def test_internal_and_measure_columns_cannot_be_group_dimensions(self):
        for group in (PRODUCT_COST, FIELD_COST_TOTAL):
            with self.subTest(group=group):
                self.assertEqual(compile_error("metric-cost-total", groups=[group]),
                                 "exploration_group_not_permitted")
        # 只差角色：同表的时间/维度/授权列都能分组。
        for group in (DAY_COST, LINE_KIND_COST, SHOP_COST):
            with self.subTest(group=group):
                self.assertIn("GROUP BY", compile_for("metric-cost-total",
                                                      groups=[group]).sql_text)

    def test_a_datetime_time_column_is_also_a_half_open_window(self):
        """时间列可以是 `timestamptz`：模板只要求它存在，区间仍然是 [start,end)。"""
        draft = compile_for("metric-payment-flow-amount", groups=[DAY_PAYMENTS])
        self.assertIn('WHERE fact."shop_id" = ANY(%(allowed_shop_ids)s)', draft.sql_text)
        self.assertIn('AND fact."paid_at" >= %(start)s', draft.sql_text)
        self.assertIn('AND fact."paid_at" < %(end)s', draft.sql_text)
        self.assertIn('GROUP BY fact."paid_at"', draft.sql_text)
        self.assertEqual(draft.sql_text.count("JOIN"), 0)

    def test_the_authorization_column_is_always_the_view_own_and_only_in_parameters(self):
        """按库存池授权的视图上谓词落在 `pool_id`：列名来自目录，值永远走参数。"""
        shop_ids = frozenset({"pool-a", "pool-b"})
        draft = compile_for("metric-physical-available-quantity", allowed_shop_ids=shop_ids)
        self.assertIn('WHERE fact."pool_id" = ANY(%(allowed_shop_ids)s)', draft.sql_text)
        self.assertIn('FROM "reporting"."v_physical_stock_items" AS fact', draft.sql_text)
        self.assertEqual(draft.parameters["allowed_shop_ids"], ["pool-a", "pool-b"])
        for shop_id in shop_ids:
            self.assertNotIn(shop_id, draft.sql_text)
        # 库存快照的时效分组也不是任何价审/库存报告的形状。
        from bi_agent.exploration.eligibility import fixed_tool_for

        self.assertIsNone(fixed_tool_for(
            selection_for("metric-listing-price", groups=[LISTING_CAPTURED_AT]),
            request_for("metric-listing-price", groups=[LISTING_CAPTURED_AT])))
        # 渠道库存按自己的店授权列分组：基表自己的列，不需要 JOIN。
        channel = compile_for("metric-channel-sellable-quantity", groups=[SHOP_CHANNEL])
        self.assertIn('GROUP BY fact."shop_id"', channel.sql_text)
        self.assertIn('SELECT fact."shop_id" AS "_shop_id"', channel.sql_text)
        self.assertEqual(channel.sql_text.count("JOIN"), 0)

    def test_metrics_need_one_field_and_a_permitted_aggregate(self):
        # 两个比值型指标都要分子分母各自求和再相除，不是一个 SUM/AVG 能发的数。
        self.assertEqual(compile_error("metric-transaction-average-price"),
                         "exploration_metric_field_ambiguous")
        self.assertEqual(compile_error("metric-product-gross-profit-reference"),
                         "exploration_metric_field_ambiguous")
        self.assertEqual(compile_error("metric-listing-price"),
                         "exploration_aggregate_not_permitted")
        # 只差形状：单字段 sum 指标能过。
        self.assertIn("sum(", compile_for("metric-cost-total").sql_text)

    def test_missing_dates_are_window_required(self):
        self.assertEqual(compile_error("metric-cost-total", start=None, end=None),
                         "exploration_window_required")
        self.assertIn("FROM", compile_for("metric-cost-total").sql_text)

    def test_the_366_day_boundary_is_inherited_from_the_request_contract(self):
        """窗口上界只在 `ExplorationRequest` 里判一次：编译器不拄第二份 366。"""
        draft = compile_for("metric-cost-total", start="2026-01-01", end="2026-12-31")
        self.assertEqual((draft.parameters["start"], draft.parameters["end"]),
                         (date(2026, 1, 1), date(2026, 12, 31)))
        with self.assertRaises(ValidationError) as caught:
            request_for("metric-cost-total", start="2026-01-01", end="2027-01-03")
        self.assertIn("exploration_window_too_large", str(caught.exception))

    def test_server_scope_must_be_a_non_empty_set_of_opaque_ids(self):
        for scope, expected in (
                (frozenset(), "exploration_scope_empty"),
                (["S1"], "exploration_scope_invalid"),                 # 可变异容器不收
                ("S1", "exploration_scope_invalid"),                   # 字符串不是 id 集合
                (frozenset({"S1", ""}), "exploration_scope_invalid"),
                (frozenset({"S1", "   "}), "exploration_scope_invalid"),
                (frozenset({"S1", None}), "exploration_scope_invalid"),
                (frozenset({"S1", True}), "exploration_scope_invalid")):
            with self.subTest(scope=repr(scope)):
                self.assertEqual(compile_error("metric-cost-total", allowed_shop_ids=scope),
                                 expected)
        # 只差容器类型：frozenset 与 set 都是服务端授权集合的形状。
        self.assertIn("ANY", compile_for("metric-cost-total",
                                         allowed_shop_ids={"S1"}).sql_text)

    def test_scope_is_sorted_into_parameters_and_never_into_sql_text(self):
        draft = compile_for("metric-cost-total",
                            allowed_shop_ids=frozenset({"S22", "S1", "S10"}))
        self.assertEqual(draft.parameters["allowed_shop_ids"], ["S1", "S10", "S22"])
        for shop_id in ("S1", "S10", "S22"):
            self.assertNotIn(shop_id, draft.sql_text)
            self.assertNotIn(shop_id, " ".join(draft.selected_refs))
        # 一条 ANY 谓词，不是逐店一圈：200 家店的 SQL 与 3 家店的逐字相同。
        big = frozenset(f"S{i}" for i in range(200))
        self.assertEqual(compile_for("metric-cost-total", allowed_shop_ids=big).sql_text,
                         draft.sql_text)
        self.assertEqual(draft.sql_text.count("= ANY(%(allowed_shop_ids)s)"), 1)

    def test_parameters_are_server_controlled_typed_and_key_sorted(self):
        draft = compile_for("metric-cost-total", groups=[DAY_COST], limit=25)
        self.assertEqual(list(draft.parameters), ["allowed_shop_ids", "end", "limit", "start"])
        self.assertEqual(draft.parameters["limit"], 25)
        self.assertIs(type(draft.parameters["limit"]), int)
        self.assertIs(type(draft.parameters["start"]), date)
        self.assertIs(type(draft.parameters["end"]), date)
        self.assertIs(type(draft.parameters["allowed_shop_ids"]), list)
        for forbidden in ("sql", "shop_ids", "order_by", "aggregate", "view_ref"):
            self.assertNotIn(forbidden, draft.parameters)

    def test_a_request_cannot_override_a_server_parameter(self):
        """形状契约之外，编译器自己复查服务端预算与参数类型。"""
        from bi_agent.exploration.compiler import compile_query
        from bi_agent.exploration.models import ExplorationRequest

        smuggled = ExplorationRequest.model_construct(
            question="x", start=date(2026, 9, 1), end=date(2026, 9, 8), entity_refs=[],
            requested_metric_refs=["metric-cost-total"], group_by_field_refs=[],
            limit=999_999)
        self.assertEqual(smuggled.limit, 999_999)          # 确实绕过了字段校验
        with self.assertRaises(ValueError) as caught:
            compile_query(smuggled, selection=selection_for("metric-cost-total"),
                          allowed_shop_ids=frozenset({"S1"}))
        self.assertEqual(str(caught.exception), "exploration_parameter_override")

        text_window = ExplorationRequest.model_construct(
            question="x", start="2026-09-01) OR 1=1 --", end=date(2026, 9, 8),
            entity_refs=[], requested_metric_refs=["metric-cost-total"],
            group_by_field_refs=[], limit=100)
        with self.assertRaises(ValueError) as caught:
            compile_query(text_window, selection=selection_for("metric-cost-total"),
                          allowed_shop_ids=frozenset({"S1"}))
        self.assertEqual(str(caught.exception), "exploration_parameter_override")

    def test_request_refs_must_be_selected_and_registered(self):
        cases = (
            ("exploration_ref_not_selected",
             dict(selection=selection_for("metric-cost-total", selected_metrics=()))),
            ("exploration_ref_not_selected",
             dict(selection=selection_for("metric-cost-total", field_refs=(
                 FIELD_COST_TOTAL,)))),
            ("exploration_ref_not_selected",
             dict(selection=selection_for("metric-cost-total", entities=()))),
            ("exploration_ref_not_selected",
             dict(selection=selection_for("metric-cost-total", view_refs=()))),
            ("exploration_ref_unregistered",
             dict(selection=selection_for("metric-quantity"))),
        )
        for expected, kwargs in cases:
            with self.subTest(expected=expected, **{k: repr(v) for k, v in kwargs.items()}):
                metric = ("metric-quantity" if expected == "exploration_ref_unregistered"
                          else "metric-cost-total")
                self.assertEqual(compile_error(metric, [DAY_COST], **kwargs), expected)
        # 只差那一项：配套齐了就能编。
        self.assertIn("FROM", compile_for("metric-cost-total", groups=[DAY_COST]).sql_text)

    def test_request_must_be_the_exploration_contract(self):
        from bi_agent.exploration.compiler import compile_query

        with self.assertRaises(TypeError):
            compile_query({"requested_metric_refs": ["metric-cost-total"]},
                          selection=selection_for("metric-cost-total"),
                          allowed_shop_ids=frozenset({"S1"}))
        with self.assertRaises(TypeError):
            compile_query(request_for("metric-cost-total"), selection={"metric_refs": ()},
                          allowed_shop_ids=frozenset({"S1"}))
        self.assertIn("FROM", compile_for("metric-cost-total").sql_text)

    def test_free_text_never_reaches_the_sql(self):
        draft = compile_for("metric-cost-total",
                            question='"; DROP TABLE bi.orders; -- reporting.v_shop_daily')
        self.assertNotIn("DROP", draft.sql_text)
        self.assertNotIn("bi.orders", draft.sql_text)
        self.assertNotIn("v_shop_daily", draft.sql_text)
        self.assertNotIn(";", draft.sql_text)
        self.assertNotIn("--", draft.sql_text)
        self.assertEqual(draft.sql_text.count("SELECT"), 1)
        self.assertNotIn("DROP", str(draft.parameters))

    def test_this_slice_neither_executes_sql_nor_checks_the_ast(self):
        """Task 2 不连库、不跑 EXPLAIN、不做 sqlglot 策略（那分属 Task 3/4）。"""
        import bi_agent.exploration.compiler as compiler
        import bi_agent.exploration.eligibility as eligibility

        for module in (compiler, eligibility):
            source = pathlib.Path(module.__file__).read_text(encoding="utf-8")
            for token in ("psycopg", "sqlglot", "EXPLAIN", "execute", "cursor", "connect",
                          "statement_timeout"):
                with self.subTest(module=module.__name__, token=token):
                    self.assertNotIn(token, source)


class ExplorationPolicyTests(unittest.TestCase):
    """计划 Task 3 Step 1/5：攻击语料 runner + 编译器正例 + 稳定错误与指纹。"""

    # --- 语料形状 ---------------------------------------------------------------

    def test_attack_corpus_has_the_exact_frozen_shape(self):
        rows = attack_rows()
        self.assertEqual(len(rows), ATTACK_CORPUS_ROWS)
        self.assertGreaterEqual(len(rows), 20)          # 计划的硬下限
        self.assertEqual([row["id"] for row in rows],
                         [f"A{number:02d}" for number in range(1, len(rows) + 1)])
        self.assertEqual(len({row["id"] for row in rows}), len(rows))
        for row in rows:
            with self.subTest(row=row["id"]):
                self.assertEqual(frozenset(row), ATTACK_ROW_FIELDS)
                self.assertTrue(ATTACK_ID_RE.match(row["id"]))
                self.assertTrue(ATTACK_REASON_RE.match(row["reason"]))
                self.assertNotIn(POLICY_CODE_PREFIX, row["reason"])   # 前缀只写一次
        # 计划的覆盖面：写操作、注释混淆、多语句、函数逃逸、越权视图、笛卡尔积、
        # 未登记 JOIN、集合运算、无授权谓词都必须有至少一行。
        reasons = {row["reason"] for row in rows}
        for reason in ("dml_forbidden", "ddl_forbidden", "copy_forbidden",
                       "command_forbidden", "lock_forbidden", "sql_text_unsafe",
                       "multiple_statements", "function_forbidden", "table_forbidden",
                       "cross_join_forbidden", "join_not_registered",
                       "set_operation_forbidden", "subquery_forbidden", "cte_forbidden",
                       "star_forbidden", "authorization_predicate_missing",
                       "window_predicate_missing", "limit_required", "column_forbidden"):
            self.assertIn(reason, reasons)

    def test_attack_corpus_keeps_the_plan_seed_rows_verbatim(self):
        """计划 Step 1 那五行逐字入库：后来的实现不得把它们改得更弱。"""
        self.assertEqual([(row["id"], row["sql"], row["reason"])
                          for row in attack_rows()[:len(PLAN_SEED_ATTACKS)]],
                         list(PLAN_SEED_ATTACKS))

    def test_every_corpus_reason_is_a_code_the_policy_actually_raises(self):
        """语料的 reason 必须能在策略源码里找到同名码：防两个文件一起写错。"""
        source = policy_source()
        for row in attack_rows():
            with self.subTest(reason=row["reason"]):
                self.assertIn(f'"{row["reason"]}"', source)
        self.assertGreaterEqual(len({row["reason"] for row in attack_rows()}), 25)

    # --- 攻击必须全部在碰库之前被拒 --------------------------------------------

    def test_the_policy_fixture_selection_is_the_whole_catalog(self):
        """护住上面那个“宽选择”前提：它不宽，后面的拒绝就说明不了问题。"""
        from bi_agent.semantic_catalog.registry import CATALOG

        selection = full_selection()
        self.assertEqual(set(selection.view_refs), {view.ref for view in CATALOG.views})
        self.assertEqual(set(selection.field_refs), {field.ref for field in CATALOG.fields})
        self.assertEqual(set(selection.metric_refs),
                         {metric.ref for metric in CATALOG.metrics})
        self.assertEqual(set(selection.join_path_refs),
                         {join.ref for join in CATALOG.joins})
        self.assertEqual(selection.catalog_version, CATALOG.version)
        self.assertFalse(selection.requires_clarification)
        self.assertEqual(selection.missing_concepts, ())

    def test_the_shell_draft_itself_passes_the_policy(self):
        """反恒真：语料用的草案壳子本身必须能过，否则“全拒”可以是假拒。"""
        plan = validate(shell_draft())
        self.assertEqual(plan.sql_text, shell_draft().sql_text)
        self.assertRegex(plan.statement_fingerprint, r"^[0-9a-f]{64}$")

    def test_every_attack_is_rejected_before_any_database_call(self):
        for row in attack_rows():
            with self.subTest(id=row["id"], reason=row["reason"]):
                code = policy_reason(attack_draft(row))
                self.assertEqual(code, POLICY_CODE_PREFIX + row["reason"])

    def test_rejection_reasons_are_distinct_per_attack_class(self):
        """每条语料只钉一个原因：不能靠一个万能码收完 46 行。"""
        codes = {row["id"]: policy_reason(attack_draft(row)) for row in attack_rows()}
        self.assertEqual(len(set(codes.values())), len({row["reason"] for row in attack_rows()}))
        self.assertEqual(codes["A01"], "exploration_star_forbidden")
        self.assertEqual(codes["A02"], "exploration_multiple_statements")
        self.assertEqual(codes["A03"], "exploration_cte_forbidden")
        self.assertEqual(codes["A04"], "exploration_function_forbidden")
        self.assertEqual(codes["A05"], "exploration_cross_join_forbidden")

    def test_rejections_never_echo_sql_refs_or_shop_ids(self):
        """错误只有一个码：不回显 SQL 片段、ref、店号或目录名。"""
        haystacks = ("SELECT", "select", "DELETE", "DROP", "pg_read_file", "S1", "S2",
                     "reporting", "bi.orders", "metric-cost-total", DAY_COST, "%(limit)s",
                     "allowed_shop_ids", "//", "/*")
        cases = [attack_draft(row) for row in attack_rows()]
        cases += [
            shell_draft(sql_text='SELECT 1 -- probe\n; DELETE FROM bi.orders'),
            shell_draft(sql_text=shell_draft().sql_text.replace('%(limit)s', '%s')),
            shell_draft(selected_refs=sorted(set(shell_draft().selected_refs)
                                             | {"view-coverage"})),
            shell_draft(parameters={**shell_draft().parameters, "limit": 999_999}),
        ]
        for index, draft in enumerate(cases):
            with self.subTest(case=index):
                code = policy_reason(draft)
                self.assertEqual(code, POLICY_CODE_PREFIX + code.removeprefix(POLICY_CODE_PREFIX))
                self.assertTrue(ATTACK_REASON_RE.match(code.removeprefix(POLICY_CODE_PREFIX)))
                self.assertLessEqual(len(code), 64)
                for text in (code, repr(ValueError(code))):
                    for needle in haystacks:
                        self.assertNotIn(needle, text)

    # --- 正例：编译器输出 ------------------------------------------------------

    def test_compiled_queries_pass_the_policy(self):
        """计划 Step 5：compiler 生成的 product-cost 查询与平台 JOIN 都是正例。"""
        cases = {
            "grouped": compile_for("metric-cost-total", groups=[DAY_COST]),
            "platform-join": compile_for("metric-cost-total", groups=[SHOPS_PLATFORM]),
            "total": compile_for("metric-cost-total"),
            "two-metrics": compile_for(["metric-cost-total", "metric-sales-amount"],
                                       groups=[DAY_COST, SHOP_COST]),
            "datetime-window": compile_for("metric-payment-flow-amount",
                                           groups=[DAY_PAYMENTS]),
        }
        fingerprints = {}
        for name, draft in cases.items():
            with self.subTest(case=name):
                plan = validate(draft)
                self.assertEqual(plan.sql_text, draft.sql_text)
                self.assertEqual(plan.parameters, draft.parameters)
                self.assertEqual(plan.selected_refs, draft.selected_refs)
                self.assertEqual(plan.catalog_version, catalog_version())
                self.assertRegex(plan.statement_fingerprint, r"^[0-9a-f]{64}$")
                self.assertEqual(plan.statement_fingerprint, expected_fingerprint(
                    draft.sql_text, draft.parameters, draft.selected_refs,
                    POLICY_SHOP_IDS, plan.catalog_version))
                self.assertIsNone(plan.estimated_rows)
                self.assertIsNone(plan.estimated_total_cost)
                self.assertEqual(plan.warnings, [])
                fingerprints[name] = plan.statement_fingerprint
        # 形状不同的语句不得共享指纹（除了只换参数值）。
        self.assertEqual(len(set(fingerprints.values())), len(fingerprints))
        joined = cases["platform-join"].sql_text
        self.assertIn("INNER JOIN", joined)
        self.assertIn("JOIN", validate(cases["platform-join"]).sql_text)

    def test_pool_authorized_view_passes_with_its_own_scope_values(self):
        """授权列来自目录：库存池视图上用池集合才能过。"""
        pools = frozenset({"pool-a", "pool-b"})
        draft = compile_for("metric-physical-available-quantity", allowed_shop_ids=pools)
        self.assertIn('WHERE fact."pool_id" = ANY(%(allowed_shop_ids)s)', draft.sql_text)
        plan = validate(draft, context=policy_context(allowed_shop_ids=pools))
        self.assertEqual(plan.parameters["allowed_shop_ids"], ["pool-a", "pool-b"])
        self.assertEqual(plan.statement_fingerprint, expected_fingerprint(
            draft.sql_text, draft.parameters, draft.selected_refs, pools,
            plan.catalog_version))
        # 只差授权集合：同一形状必须给出不同指纹（不能拿窄授权计划当宽授权重跑）。
        narrow = frozenset({"pool-a"})
        other_draft = compile_for("metric-physical-available-quantity",
                                 allowed_shop_ids=narrow)
        other = validate(other_draft, context=policy_context(allowed_shop_ids=narrow))
        self.assertNotEqual(other.statement_fingerprint, plan.statement_fingerprint)
        # 而“参数里的集合与服务端授权集合不等”本身就是拒理由。
        self.assertEqual(policy_reason(draft, context=policy_context(allowed_shop_ids=narrow)),
                         "exploration_scope_mismatch")

    def test_the_plan_keeps_the_original_sql_for_execution(self):
        """NULL 只用于解析：执行/指纹拿到的仍是带占位符的原文。"""
        draft = shell_draft()
        plan = validate(draft)
        self.assertNotIn("NULL", plan.sql_text)
        for name in ("allowed_shop_ids", "start", "end", "limit"):
            self.assertIn(f"%({name})s", plan.sql_text)
        self.assertEqual(plan.sql_text, draft.sql_text)
        self.assertIs(type(plan.parameters["start"]), date)
        self.assertIs(type(plan.parameters["limit"]), int)

    # --- 指纹 ------------------------------------------------------------------

    def test_fingerprint_is_the_planned_json_sha256_payload(self):
        import bi_agent.exploration.policy as policy

        draft = shell_draft()
        plan = validate(draft)
        self.assertEqual(plan.statement_fingerprint, expected_fingerprint(
            draft.sql_text, draft.parameters, draft.selected_refs, POLICY_SHOP_IDS,
            catalog_version()))
        self.assertEqual(policy.EXPLORATION_TEMPLATE_VERSION, "exploration-sql/2026-09-14.1")
        self.assertEqual(plan.template_version, policy.EXPLORATION_TEMPLATE_VERSION)
        self.assertEqual(tuple(policy.PLACEHOLDER_ORDER), ("allowed_shop_ids", "start",
                                                           "end", "limit"))
        self.assertEqual(sorted(parameter_keys()), sorted(policy.PLACEHOLDER_ORDER))
        # 真店号不进指纹输入以外的持久载荷：摘要之外的字段都是 ref/文本/版本。
        self.assertNotIn("S1", plan.statement_fingerprint)
        self.assertNotIn("S2", plan.statement_fingerprint)
        # 指纹不是“裸 SQL 的哈希”：它同时钉住参数名、选择集、授权集合摘要与目录版本。
        self.assertNotEqual(plan.statement_fingerprint,
                            hashlib.sha256(draft.sql_text.encode("utf-8")).hexdigest())
        self.assertNotEqual(plan.statement_fingerprint,
                            hashlib.sha256(json.dumps(
                                sorted(POLICY_SHOP_IDS), separators=(",", ":")
                            ).encode("utf-8")).hexdigest())

    def test_fingerprint_is_a_statement_identity_not_an_invocation_identity(self):
        """同一形状不同参数值→同一指纹；换授权集合/换形状→不同一。"""
        from bi_agent.exploration.models import ExplorationRequest

        base = validate(shell_draft())
        moved = validate(compile_for("metric-cost-total", groups=[DAY_COST],
                                     start="2026-10-01", end="2026-10-02", limit=7))
        self.assertEqual(base.statement_fingerprint, moved.statement_fingerprint)
        self.assertEqual(moved.parameters["limit"], 7)
        wider_ids = frozenset({"S1", "S2", "S3"})
        wider = validate(compile_for("metric-cost-total", groups=[DAY_COST],
                                    allowed_shop_ids=wider_ids),
                         context=policy_context(allowed_shop_ids=wider_ids))
        self.assertNotEqual(base.statement_fingerprint, wider.statement_fingerprint)
        shaped = validate(compile_for("metric-cost-total"))
        self.assertNotEqual(base.statement_fingerprint, shaped.statement_fingerprint)
        # 运行时刻、重试次数、会话身份都不参与指纹：指纹必须可重算。
        for overrides in ({"now": datetime(2030, 1, 1, tzinfo=timezone.utc)},
                          {"now": datetime(2026, 9, 14, 20, tzinfo=BEIJING)},
                          {"deadline": time.monotonic() + 0.5},
                          {"attempt_no": 9}, {"subject_id": "other"},
                          {"chat_id": UUID(int=99)}):
            with self.subTest(**{key: type(value).__name__
                                 for key, value in overrides.items()}):
                self.assertEqual(validate(shell_draft(),
                                          context=policy_context(**overrides)
                                          ).statement_fingerprint,
                                 base.statement_fingerprint)
        self.assertIsInstance(ExplorationRequest, type)

    # --- 目录/选择/上下文不匹配 ------------------------------------------------

    def test_only_the_currently_published_catalog_is_accepted(self):
        from bi_agent.semantic_catalog.registry import CATALOG

        stale = full_selection(catalog_version="semantic/2020-01-01.1")
        self.assertEqual(policy_reason(shell_draft(), selection=stale),
                         "exploration_catalog_version_mismatch")
        self.assertEqual(validate(shell_draft(),
                                  selection=full_selection()).catalog_version, CATALOG.version)

    def test_tables_columns_and_joins_must_be_in_this_rounds_selection(self):
        draft = compile_for("metric-cost-total", groups=[SHOPS_PLATFORM])
        without_edge = full_selection(join_path_refs=())
        self.assertEqual(policy_reason(draft, selection=without_edge,
                                       context=policy_context()),
                         "exploration_join_not_selected")
        self.assertEqual(validate(draft, selection=full_selection(),
                                  context=policy_context()).selected_refs,
                         draft.selected_refs)
        without_view = full_selection(view_refs=tuple(
            ref for ref in full_selection().view_refs if ref != COST_VIEW))
        self.assertEqual(policy_reason(draft, selection=without_view),
                         "exploration_table_forbidden")
        without_field = full_selection(field_refs=tuple(
            ref for ref in full_selection().field_refs if ref != SHOPS_PLATFORM))
        self.assertEqual(policy_reason(draft, selection=without_field),
                         "exploration_column_forbidden")
        without_metric = full_selection(metric_refs=tuple(
            ref for ref in full_selection().metric_refs if ref != "metric-cost-total"))
        self.assertEqual(policy_reason(draft, selection=without_metric),
                         "exploration_ref_not_selected")

    def test_selected_refs_must_be_exactly_the_closure_of_the_statement(self):
        """多一个、少一个、重复、没排序：都不再是“这句 SQL 到底说了什么”。"""
        closure = shell_draft().selected_refs
        cases = (
            ("extra", sorted(set(closure) | {"view-coverage"})),
            ("duplicate", sorted(set(closure)) + [closure[0]]),
            ("unsorted", list(reversed(sorted(closure)))),
        )
        for name, refs in cases:
            with self.subTest(case=name):
                self.assertEqual(policy_reason(shell_draft(selected_refs=refs)),
                                 "exploration_ref_unbound")
        self.assertEqual(validate(shell_draft(selected_refs=closure)).selected_refs, closure)

    def test_a_foreign_or_unbound_ref_in_the_draft_is_refused(self):
        """ref 进不了草案契约，以及契约内的 ref 在本轮/在本句里都得有交代。"""
        for refs in (["metric-$sum"], ["reporting.v_shop_daily"]):
            with self.subTest(refs=refs):
                with self.assertRaises(ValidationError):
                    shell_draft(selected_refs=refs)
        # ref 形状合法但本轮没选它：目录能解，选择集里没有。
        shell = shell_draft()
        foreign = "field-coverage-data-as-of"
        refs = sorted(set(shell.selected_refs) | {foreign})
        narrow = full_selection(field_refs=tuple(
            ref for ref in full_selection().field_refs if ref != foreign))
        self.assertEqual(policy_reason(shell_draft(selected_refs=refs), selection=narrow),
                         "exploration_ref_not_selected")
        # ref 合法、本轮也选了，但这句 SQL 里没有它的落点：还是不对。
        self.assertEqual(policy_reason(shell_draft(
            selected_refs=sorted(set(shell.selected_refs) | {"entity-shop"}))),
            "exploration_ref_unbound")

    # --- 参数与授权集合 --------------------------------------------------------

    def test_parameter_keys_must_be_exactly_the_server_set(self):
        base = dict(shell_draft().parameters)
        cases = (
            ("extra", {**base, "offset": 10}),
            ("missing", {key: value for key, value in base.items() if key != "limit"}),
            ("renamed", {key: value for key, value in base.items() if key != "start"}
                        | {"from": base["start"]}),
        )
        for name, parameters in cases:
            with self.subTest(case=name):
                self.assertEqual(policy_reason(shell_draft(parameters=parameters)),
                                 "exploration_parameter_mismatch")
        self.assertEqual(validate(shell_draft(parameters=base)).parameters, base)

    def test_placeholder_names_and_positions_are_bound_together(self):
        sql = shell_draft().sql_text
        cases = (
            ("bare positional", sql.replace("%(allowed_shop_ids)s", "%s"),
             "exploration_placeholder_invalid"),
            ("uppercase name", sql.replace("%(allowed_shop_ids)s", "%(AllowedShopIds)s"),
             "exploration_placeholder_invalid"),
            ("wrong conversion", sql.replace("%(limit)s", "%(limit)d"),
             "exploration_placeholder_invalid"),
            ("swapped window", sql.replace(">= %(start)s", ">= %(end)s")
                               .replace("< %(end)s", "< %(start)s"),
             "exploration_parameter_mismatch"),
            ("limit in ANY", sql.replace("ANY(%(allowed_shop_ids)s)", "ANY(%(limit)s)")
                             .replace("LIMIT %(limit)s", "LIMIT %(allowed_shop_ids)s"),
             "exploration_parameter_mismatch"),
            ("doubled parameter", sql.replace("< %(end)s", "< %(start)s"),
             "exploration_parameter_mismatch"),
        )
        for name, sql_text, expected in cases:
            with self.subTest(case=name):
                self.assertNotEqual(sql_text, sql)
                self.assertEqual(policy_reason(shell_draft(sql_text=sql_text)), expected)

    def test_parameter_values_are_rechecked_server_side(self):
        from datetime import date as date_type

        base = dict(shell_draft().parameters)
        cases = (
            ("limit zero", {**base, "limit": 0}),
            ("limit above budget", {**base, "limit": 501}),
            ("limit as text", {**base, "limit": "100"}),
            ("limit as bool", {**base, "limit": True}),
            ("start as text", {**base, "start": "2026-09-01) OR 1=1 --"}),
            ("end as datetime", {**base, "end": datetime(2026, 9, 8, tzinfo=timezone.utc)}),
            ("window not ordered", {**base, "start": base["end"], "end": base["start"]}),
            ("window too wide", {**base, "start": date_type(2020, 1, 1),
                                 "end": date_type(2026, 1, 1)}),
            ("scope unsorted", {**base, "allowed_shop_ids": ["S2", "S1"]}),
            ("scope blank member", {**base, "allowed_shop_ids": ["S1", "  "]}),
            ("scope empty list", {**base, "allowed_shop_ids": []}),
            ("scope as text", {**base, "allowed_shop_ids": "S1,S2"}),
            ("scope member not text", {**base, "allowed_shop_ids": ["S1", 7]}),
        )
        for name, parameters in cases:
            with self.subTest(case=name):
                self.assertEqual(policy_reason(shell_draft(parameters=parameters)),
                                 "exploration_parameter_invalid")
        self.assertEqual(validate(shell_draft(parameters=base)).parameters, base)

    def test_scope_must_match_the_server_authorization(self):
        for context, expected in (
                (policy_context(allowed_shop_ids=frozenset({"S1"})),
                 "exploration_scope_mismatch"),
                (policy_context(allowed_shop_ids=frozenset()), "exploration_scope_empty"),
                (policy_context(allowed_shop_ids=["S1", "S2"]), "exploration_scope_invalid"),
                (policy_context(allowed_shop_ids=frozenset({"S1", " "})),
                 "exploration_scope_invalid"),
                (policy_context(allowed_shop_ids=frozenset({"S1", None})),
                 "exploration_scope_invalid")):
            with self.subTest(scope=repr(context.allowed_shop_ids)):
                self.assertEqual(policy_reason(shell_draft(), context=context), expected)
        self.assertEqual(validate(shell_draft(), context=policy_context(
            allowed_shop_ids=POLICY_SHOP_IDS)).parameters["allowed_shop_ids"], ["S1", "S2"])

    # --- 时间角度 --------------------------------------------------------------

    def test_context_time_must_be_timezone_aware_and_the_budget_a_number(self):
        for context, expected in (
                (policy_context(now=datetime(2026, 9, 14, 12)),
                 "exploration_context_time_naive"),
                (policy_context(now="2026-09-14T12:00:00Z"), "exploration_context_time_naive"),
                (policy_context(deadline="30"), "exploration_context_deadline_invalid"),
                (policy_context(deadline=True), "exploration_context_deadline_invalid"),
                (policy_context(deadline=float("nan")), "exploration_context_deadline_invalid"),
                (policy_context(deadline=None), "exploration_context_deadline_invalid")):
            with self.subTest(now=repr(context.now), deadline=repr(context.deadline)):
                self.assertEqual(policy_reason(shell_draft(), context=context), expected)
        # 带时区即可（不要求 UTC）：仓库里既有 BEIJING 的 DomainContext 也是合法上下文。
        self.assertEqual(validate(shell_draft(), context=policy_context(
            now=datetime(2026, 9, 14, 20, tzinfo=BEIJING))).statement_fingerprint,
            validate(shell_draft()).statement_fingerprint)

    # --- 输入契约与不变异 ------------------------------------------------------

    def test_inputs_must_be_the_committed_contracts(self):
        """三个入参都是已交付契约：递字典/递别的对象不当“形状不对”而当编程错误。"""
        from bi_agent.exploration.models import ValidatedQueryPlan

        draft = shell_draft()
        with self.assertRaises(TypeError):
            validate({"sql_text": draft.sql_text, "parameters": draft.parameters,
                      "selected_refs": draft.selected_refs})
        with self.assertRaises(TypeError):
            validate(draft, selection={"metric_refs": ()})
        with self.assertRaises(TypeError):
            validate(draft, selection=full_selection(), context=object())
        with self.assertRaises(TypeError):
            validate(request_for("metric-cost-total"))
        self.assertIsInstance(validate(draft), ValidatedQueryPlan)

    def test_single_statement_shape_rules_are_what_they_say(self):
        """尾部分号不是第二条语句；多余的字面 NULL 是多开的一条真实值通道。"""
        from bi_agent.exploration.models import SqlDraft

        sql = shell_draft().sql_text
        with self.assertRaises(ValidationError):
            SqlDraft(**{**draft_values(), "sql_text": ""})      # 空文本进不了草案
        self.assertIn(";", validate(shell_draft(sql_text=f"{sql};")).sql_text)
        self.assertEqual(policy_reason(shell_draft(sql_text=f"{sql}; {sql}")),
                         "exploration_multiple_statements")
        # 输出列里多一个写死的 NULL：四个参数位以外不容得下第五个 NULL。
        self.assertEqual(policy_reason(shell_draft(
            sql_text=sql.replace('fact."day" AS "field_product_cost_daily_day"', "NULL"))),
            "exploration_null_literal_forbidden")
        # 两个基表直接笛卡尔积：没有 ON 的 JOIN 在目录配边之前就被拒。
        self.assertEqual(policy_reason(shell_draft(
            sql_text=sql.replace('WHERE fact."shop_id" = ANY(%(allowed_shop_ids)s)',
                                 'CROSS JOIN "reporting"."v_shops" AS shops '
                                 'WHERE fact."shop_id" = ANY(%(allowed_shop_ids)s)'))),
            "exploration_cross_join_forbidden")
        self.assertIn("JOIN", validate(
            compile_for("metric-cost-total", groups=[SHOPS_PLATFORM])).sql_text)

    def test_an_aggregate_free_statement_is_not_an_exploration(self):
        """受控探索只会发聚合行：只回明细的 SELECT 不在此列（也不该在此列）。"""
        sql = shell_draft().sql_text
        bare = sql.replace('fact."day" AS "field_product_cost_daily_day", '
                           'sum(fact."cost_total") AS "metric_cost_total"',
                           'fact."day" AS "field_product_cost_daily_day"')
        self.assertNotEqual(bare, sql)
        self.assertEqual(policy_reason(shell_draft(sql_text=bare)),
                         "exploration_aggregate_required")
        # 只差那一个聚合：把它加回去就是一条合法的聚合探索。
        self.assertIn("sum(", validate(shell_draft()).sql_text)

    def test_ordering_modifiers_are_not_the_fixed_template(self):
        """排序旋钮由服务端定：`DESC` 与 `NULLS FIRST` 都不是模板能发的形状。

        `NULLS FIRST` 是这条护栏的回归用例：它曾因为策略读了 `Ordered` 上并不存在的
        `"nulls"` 键而直接过关（有方向的空值位置会改掉 `LIMIT` 截断后留下哪几组）。
        postgres 的 `ASC` 默认就是空值在后，所以下面两条等价写法仍在模板语义之内。
        """
        sql = shell_draft().sql_text
        for modifier in ("DESC", "NULLS FIRST"):
            with self.subTest(modifier=modifier):
                self.assertEqual(policy_reason(shell_draft(sql_text=ordered_sql(modifier))),
                                 "exploration_expression_forbidden")
        self.assertEqual(policy_reason(shell_draft(
            sql_text=sql.replace('ORDER BY fact."day"', 'ORDER BY 2'))),
            "exploration_expression_forbidden")
        for equivalent in ("ASC", "NULLS LAST"):
            with self.subTest(equivalent=equivalent):
                mutated = ordered_sql(equivalent)
                self.assertEqual(validate(shell_draft(sql_text=mutated)).sql_text, mutated)
        self.assertIn('ORDER BY fact."day"', validate(shell_draft()).sql_text)

    def test_the_order_modifier_guard_reads_the_keys_sqlglot_actually_sets(self):
        """钉住修饰键名单与 `exp.Ordered.arg_types` 同步：恒假分句就是没有护栏。

        上一轮的缺陷不是“判错了键”而是“判了一个从来不存在的键”：那种分句永远不命中，
        代码看起来仍然在拦。这里把两个方向都钉住：名单必须覆盖 `arg_types` 里除 `this`
        以外的全部键；被拦的写法必须真的把名单里的某个键置真；而编译器真正输出的那个
        `Ordered` 必须一个修饰都不带（否则把判法改成 `is not None` 一类的错法会把正例全误拒）。
        """
        import sqlglot
        from sqlglot import expressions as exp

        import bi_agent.exploration.policy as policy

        self.assertEqual(set(policy.ORDER_MODIFIER_ARGS),
                         set(exp.Ordered.arg_types) - {"this"})
        for clause, truthy in (("NULLS FIRST", {"nulls_first"}),
                               ("DESC", {"desc", "nulls_first"})):
            with self.subTest(clause=clause):
                ordered = self._ordered_item(ordered_sql(clause))
                self.assertEqual({key for key in policy.ORDER_MODIFIER_ARGS
                                  if ordered.args.get(key)}, truthy)
                self.assertEqual(policy_reason(shell_draft(sql_text=ordered_sql(clause))),
                                 "exploration_expression_forbidden")
        plain = self._ordered_item(shell_draft().sql_text)
        self.assertEqual([key for key in policy.ORDER_MODIFIER_ARGS if plain.args.get(key)], [])
        self.assertIsInstance(plain, exp.Ordered)
        self.assertIsInstance(plain.this, exp.Column)
        self.assertIsNotNone(sqlglot.__version__)

    @staticmethod
    def _ordered_item(sql_text):
        import sqlglot

        normalized = sql_text
        for name in ("allowed_shop_ids", "start", "end", "limit"):
            normalized = normalized.replace(f"%({name})s", "NULL")
        return sqlglot.parse(normalized, read="postgres")[0].args["order"].expressions[0]

    @staticmethod
    def _ordered_item(sql_text):
        import sqlglot

        normalized = sql_text
        for name in ("allowed_shop_ids", "start", "end", "limit"):
            normalized = normalized.replace(f"%({name})s", "NULL")
        return sqlglot.parse(normalized, read="postgres")[0].args["order"].expressions[0]

    def test_validation_does_not_mutate_its_inputs(self):
        """策略是纯函数：不改入参，也不与入参共享可变容器。"""
        from bi_agent.exploration.models import ValidatedQueryPlan

        draft = shell_draft()
        before_sql = draft.sql_text
        before_parameters = {key: str(value)
                             for key, value in sorted(draft.parameters.items())}
        before_refs = list(draft.selected_refs)
        context = policy_context()
        before_scope = context.allowed_shop_ids
        plan = validate(draft, context=context)
        self.assertIsInstance(plan, ValidatedQueryPlan)
        self.assertEqual(draft.sql_text, before_sql)
        self.assertEqual({key: str(value) for key, value in sorted(draft.parameters.items())},
                         before_parameters)
        self.assertEqual(draft.selected_refs, before_refs)
        self.assertIs(context.allowed_shop_ids, before_scope)
        # 计划里的参数与草案不共享容器：事后改草案不得改已发计划。
        self.assertIsNot(plan.parameters, draft.parameters)
        self.assertIsNot(plan.parameters["allowed_shop_ids"],
                         draft.parameters["allowed_shop_ids"])
        self.assertIsNot(plan.selected_refs, draft.selected_refs)
        draft.parameters["allowed_shop_ids"].append("S3")
        draft.parameters["limit"] = 40_000
        self.assertEqual(plan.parameters["allowed_shop_ids"], ["S1", "S2"])
        self.assertEqual(plan.parameters["limit"], 100)

    def test_a_mutated_draft_cannot_smuggle_server_values_past_the_policy(self):
        """参数字典是可变的：策略必须重校取值，不能只信 Task 1 的形状。"""
        for key, value, expected in (
                ("limit", 999_999, "exploration_parameter_invalid"),
                ("allowed_shop_ids", ["S1", "S2", "S9"], "exploration_scope_mismatch"),
                ("start", "2026-09-01) OR 1=1 --", "exploration_parameter_invalid"),
                ("end", datetime(2026, 9, 8, tzinfo=timezone.utc),
                 "exploration_parameter_invalid")):
            with self.subTest(key=key):
                draft = shell_draft()
                draft.parameters[key] = value
                self.assertEqual(policy_reason(draft), expected)

    # --- 策略不越界 ------------------------------------------------------------

    def test_policy_source_neither_connects_nor_executes(self):
        """Task 3 只做结构校验：连库、EXPLAIN、超时都是 Task 4/5 的事。

        import 清单用 AST 取（注释里提到 psycopg/EXPLAIN 不算依赖），代码形状则按
        字面量查：“没拿到 conn/store”比“源码里没有某个词”更难造假。
        """
        import ast
        import bi_agent.exploration.policy as policy

        source = pathlib.Path(policy.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.add(str(node.module))
        self.assertTrue({"sqlglot", "hashlib", "json"} <= imported)
        for forbidden in ("psycopg", "socket", "subprocess", "threading", "http",
                         "bi_agent.runtime.repository", "bi_agent.exploration.repository"):
            with self.subTest(module=forbidden):
                self.assertNotIn(forbidden, imported)
        # “依赖只有解析库”已由上面的 import 清单证明；下面只能拦代码形状（注释里
        # 提到 psycopg 的占位符规则不是依赖）。
        for token in ("import psycopg", "connect(", "cursor", "conn.execute", "transaction",
                      "statement_timeout", "EXPLAIN (", "context.conn", "context.store",
                      "context.shop_refs"):
            with self.subTest(token=token):
                self.assertNotIn(token, source)


class ExplorationDependencyTests(unittest.TestCase):
    """`sqlglot` 是本轮唯一的新依赖（开发流程 §4.7：计划已明确批准）。"""

    def test_pyproject_declares_sqlglot_and_nothing_else_new(self):
        pyproject = tomllib.loads((BACKEND_ROOT / "pyproject.toml")
                                  .read_text(encoding="utf-8"))
        declared = pyproject["project"]["dependencies"]
        self.assertIn(f"sqlglot{SQLGLOT_SPECIFIER}", declared)
        self.assertEqual(
            sorted(declared),
            sorted(["fastapi>=0.116,<1", "httpx>=0.27,<1", "psycopg[binary]>=3.2,<4",
                    "pydantic>=2,<3", "sqlglot>=27.14,<28", "tzdata>=2024.1",
                    "uvicorn>=0.35,<1"]))
        self.assertEqual(len([item for item in declared if item.startswith("sqlglot")]),
                         1)

    def test_uv_lock_resolves_exactly_one_sqlglot_in_range(self):
        lock = tomllib.loads((BACKEND_ROOT / "uv.lock").read_text(encoding="utf-8"))
        resolved = [package for package in lock["package"]
                    if package["name"] == "sqlglot"]
        self.assertEqual(len(resolved), 1)
        version = resolved[0]["version"]
        self.assertTrue(self._in_locked_range(version), f"locked {version}")
        root = next(package for package in lock["package"]
                    if package["name"] == "bi-agent")
        self.assertIn({"name": "sqlglot"}, root["dependencies"])
        requires_dist = [item for item in root["metadata"]["requires-dist"]
                         if item["name"] == "sqlglot"]
        self.assertEqual(len(requires_dist), 1)
        self.assertEqual(requires_dist[0]["specifier"], SQLGLOT_SPECIFIER)

    def test_installed_sqlglot_satisfies_the_locked_range(self):
        version = importlib.metadata.version("sqlglot")
        lock = tomllib.loads((BACKEND_ROOT / "uv.lock").read_text(encoding="utf-8"))
        locked = next(package["version"] for package in lock["package"]
                      if package["name"] == "sqlglot")
        self.assertEqual(self._version_tuple(version), self._version_tuple(locked))
        self.assertTrue(self._in_locked_range(version), f"installed {version}")
        module = importlib.import_module("sqlglot")
        self.assertEqual(self._version_tuple(str(getattr(module, "__version__", ""))),
                         self._version_tuple(version))
        # 只证明"依赖可用"：本切片不用它解析或校验任何 SQL。
        self.assertTrue(callable(getattr(module, "parse", None)))

    def test_the_range_guard_itself_rejects_out_of_range_versions(self):
        """护栏自检：低于/高于区间的版本号必须判假，否则上面两条断言是空的。"""
        for too_low in ("26.3.0", "27.13.1", "27.0.0"):
            with self.subTest(version=too_low):
                self.assertFalse(self._in_locked_range(too_low))
        for too_high in ("28.0.0", "28.1.0", "29.0.0"):
            with self.subTest(version=too_high):
                self.assertFalse(self._in_locked_range(too_high))
        for ok in ("27.14", "27.14.0", "27.15.1", "27.99.0"):
            with self.subTest(version=ok):
                self.assertTrue(self._in_locked_range(ok))
        for malformed in ("", "latest", "sqlglot"):
            with self.subTest(version=repr(malformed)):
                with self.assertRaises(ValueError):
                    self._version_tuple(malformed)

    @staticmethod
    def _version_tuple(text: str) -> tuple[int, ...]:
        match = re.match(r"^(\d+)\.(\d+)(?:\.(\d+))?$", text.strip())
        if match is None:
            raise ValueError(f"unparsed_version:{text!r}")
        return tuple(int(part) for part in match.groups() if part is not None)

    @classmethod
    def _in_locked_range(cls, text: str) -> bool:
        version = cls._version_tuple(text)
        return SQLGLOT_LOWER_BOUND <= version < SQLGLOT_UPPER_BOUND


def repository_entries():
    """Task 4 执行层入口（延迟导入：纯契约用例不应该依赖 `psycopg` 导入成功）。"""
    from bi_agent.exploration.repository import estimate_plan, execute_plan

    return estimate_plan, execute_plan


def budget_exception():
    """成本异常类（延迟导入）：`refusal()` 只认裸 `ValueError`，预算类得单独抱。"""
    from bi_agent.exploration.models import ExplorationBudgetExceeded

    return ExplorationBudgetExceeded


class ExplorationBudgetContractTests(unittest.TestCase):
    """计划 Task 4 交到契约层的那一份：预算数字、两个原因码与三步调用序列。"""

    def test_task_four_budget_numbers_are_the_planned_ones(self):
        from bi_agent.exploration import models

        self.assertEqual(models.MAX_ESTIMATED_ROWS, PLAN_MAX_ESTIMATED_ROWS)
        self.assertIsInstance(models.MAX_TOTAL_COST, Decimal)   # 浮点迚不了成本比较
        self.assertEqual(models.MAX_TOTAL_COST, PLAN_MAX_TOTAL_COST)
        self.assertEqual(models.STATEMENT_TIMEOUT_MS, PLAN_STATEMENT_TIMEOUT_MS)
        self.assertEqual(models.MAX_RESULT_BYTES, PLAN_MAX_RESULT_BYTES)
        self.assertEqual(models.DB_IO_RESERVE_SECONDS, PLAN_DEADLINE_RESERVE_SECONDS)
        self.assertEqual(models.MAX_TEXT_CHARS, PLAN_MAX_TEXT_CHARS)
        self.assertEqual((models.SHOP_ID_COLUMN, models.SHOP_REF_COLUMN),
                         (SHOP_ID_COLUMN, SHOP_REF_COLUMN))
        # 行数上限不另开一份：请求、执行、投影用的是同一个预算（Task 1 已定）。
        self.assertLessEqual(models.MAX_ROWS, PLAN_MAX_ESTIMATED_ROWS)

    def test_budget_exception_publishes_exactly_two_reasons(self):
        from bi_agent.exploration.models import ExplorationBudgetExceeded

        self.assertEqual(BUDGET_REASONS, {"estimated_rows", "total_cost"})
        for reason in sorted(BUDGET_REASONS):
            with self.subTest(reason=reason):
                error = ExplorationBudgetExceeded(reason)
                self.assertEqual(error.reason, reason)
                self.assertEqual(str(error), f"exploration_budget_exceeded:{reason}")
                self.assertIsInstance(error, ValueError)   # Task 5 一道 except ValueError 就够
        # 没批过的原因码进不来：否则“稳定”只是口头承诺。
        self.assertEqual(refusal(lambda: ExplorationBudgetExceeded("too_slow")),
                         "exploration_budget_reason_unknown")

    def test_entry_points_have_the_published_signatures(self):
        """签名即契约：Task 5 只能按 `estimate → execute → project` 接线。"""
        import bi_agent.exploration.projection as projection
        import bi_agent.exploration.repository as repository

        shapes = {"estimate_plan": (["conn", "plan", "deadline"], "ValidatedQueryPlan"),
                  "execute_plan": (["conn", "plan", "deadline"],
                                   "tuple[list[str], list[tuple[object, ...]]]"),
                  "project_result": (["columns", "rows", "shop_refs"],
                                     "tuple[list[ExplorationColumn], "
                                     "list[dict[str, object]]]")}
        for name, (parameters, returned) in shapes.items():
            holder = repository if name in ("estimate_plan", "execute_plan") else projection
            with self.subTest(entry=name):
                signature = inspect.signature(getattr(holder, name))
                self.assertEqual(list(signature.parameters), parameters)
                self.assertEqual(signature.return_annotation, returned)
                # 预算与映射都是关键字参数：调用点不能靠位置传个数字进去。
                self.assertTrue(signature.parameters[parameters[-1]].kind
                                is inspect.Parameter.KEYWORD_ONLY, name)
                if name != "project_result":
                    # 执行层只认 Task 1 的计划契约；投影层的入参是列与行，不是计划。
                    self.assertEqual(signature.parameters["plan"].annotation,
                                     "ValidatedQueryPlan")
                else:
                    self.assertEqual(signature.parameters["shop_refs"].annotation,
                                     "Mapping[str, str]")

    def test_result_budget_lives_in_the_projection_not_the_executor(self):
        """字节预算只判安全表示：执行层不投影、不调负投影，投影不碰库。"""
        repository_source = module_source("repository")
        projection_source = module_source("projection")
        # 执行层看不到真店号的映射表，也不自己偷跑投影：否则原始行就有了第二个去处。
        for token in ("project_result", "projection", "shop_refs", "MAX_RESULT_BYTES"):
            with self.subTest(repository_token=token):
                self.assertNotIn(token, repository_source)
        for token in ("psycopg", "EXPLAIN", "conn", "transaction", "execute", "time."):
            with self.subTest(projection_token=token):
                self.assertNotIn(token, projection_source)
        # 行数与列身份在执行层，字节数在投影层：两边各自只认一个常量。
        self.assertIn("MAX_ROWS", repository_source)
        self.assertIn("row_limit_exceeded", repository_source)
        self.assertIn("MAX_RESULT_BYTES", projection_source)

    def test_rejection_reason_sets_are_the_published_ones(self):
        """`_reject("x")` 与预算异常的字面量就是公用的全部原因码。"""
        repository_source = module_source("repository")
        self.assertEqual(set(re.findall(r'_reject\("([a-z_]+)"\)', repository_source)),
                         set(EXECUTION_REASONS))
        self.assertEqual(
            set(re.findall(r'ExplorationBudgetExceeded\("([a-z_]+)"\)', repository_source)),
            set(BUDGET_REASONS))
        self.assertEqual(
            set(re.findall(r'_reject\("([a-z_]+)"\)', module_source("projection"))),
            set(PROJECTION_REASONS))

    def test_the_authorization_alias_the_projection_honours_is_the_compilers(self):
        """投影只认编译器给授权列的那个别名：别名改了就得在这里判红。"""
        from bi_agent.exploration.compiler import INTERNAL_ALIAS_PREFIX
        from bi_agent.semantic_catalog.registry import CATALOG, catalog_indexes

        draft = compile_for("metric-cost-total", groups=[DAY_COST, SHOP_COST])
        self.assertIn(f'AS "{SHOP_ID_COLUMN}"', draft.sql_text)
        self.assertEqual(f"{INTERNAL_ALIAS_PREFIX}shop_id", SHOP_ID_COLUMN)
        # 目录里另一条授权列（库存池）不拿着同一张映射表混进来。
        pool = catalog_indexes(CATALOG).fields[POOL_PHYSICAL]
        self.assertEqual(f"{INTERNAL_ALIAS_PREFIX}{pool.column}", "_pool_id")
        self.assertEqual(
            projection_reason(["_pool_id"], [("POOL-1",)]),
            "exploration_internal_column_forbidden")


class ExplorationGateScriptTests(unittest.TestCase):
    """门禁的语句形状与错误映射：真 EXPLAIN、真只读、真权限在下一个类里。"""

    SCRIPT_DESCRIPTION = ["field_product_cost_daily_day", "metric_cost_total"]

    def setUp(self):
        self.estimate_plan, self.execute_plan = repository_entries()
        self.plan = stub_plan()
        self.deadline = time.monotonic() + 30.0

    # --- helpers ----------------------------------------------------------------

    def script_conn(self, **overrides):
        defaults = {"description": self.SCRIPT_DESCRIPTION,
                    "rows": [(date(2026, 9, 1), Decimal("12.30"))]}
        defaults.update(overrides)
        return GateScriptConn(**defaults)

    def estimated(self, conn):
        """先过成本门：拿到唯一能被执行的那份计划。"""
        return self.estimate_plan(conn, self.plan, deadline=self.deadline)

    # --- 语句序列 --------------------------------------------------------------

    def test_estimate_opens_its_own_read_only_transaction_and_explains_once(self):
        conn = self.script_conn()
        estimated = self.estimated(conn)
        self.assertEqual(conn.statements, [
            READ_ONLY_STATEMENT, STATEMENT_TIMEOUT_STATEMENT,
            EXPLAIN_STATEMENT_PREFIX + self.plan.sql_text])
        self.assertEqual(conn.parameters[2], self.plan.parameters)
        self.assertEqual(conn.entered, 1)
        self.assertEqual(conn.exited, 1)
        self.assertEqual(estimated.estimated_rows, 18)
        self.assertEqual(estimated.estimated_total_cost, Decimal("647.04"))
        # 计划不被就地改写：返回的是新对象，原计划仍然是“未估算”。
        self.assertIsNone(self.plan.estimated_rows)

    def test_execute_opens_its_own_read_only_transaction_and_runs_the_plan(self):
        gate = self.script_conn()
        estimated = self.estimated(gate)
        conn = self.script_conn()
        columns, rows = self.execute_plan(conn, estimated, deadline=self.deadline)
        self.assertEqual(conn.statements, [
            READ_ONLY_STATEMENT, STATEMENT_TIMEOUT_STATEMENT, self.plan.sql_text])
        self.assertEqual(conn.parameters[2], self.plan.parameters)
        self.assertEqual(columns, [DAY_COST, "metric-cost-total"])
        self.assertEqual(rows, [(date(2026, 9, 1), Decimal("12.30"))])
        # 两次调用各自开、各自关一个事务：不共用门禁的只读上下文。
        self.assertEqual((gate.entered, gate.exited, conn.entered, conn.exited),
                         (1, 1, 1, 1))

    def test_execution_fetches_limit_plus_one_and_refuses_overflow(self):
        conn = self.script_conn(rows=[(date(2026, 9, 1), Decimal("1.00"))] * 3)
        estimated = self.estimated(conn)
        self.execute_plan(conn, estimated, deadline=self.deadline)
        self.assertEqual(conn.result.sizes, [101])             # limit=100 → 取 101
        # 取到 limit+1 行就是截断事故：不丢行、不部分返回，整条拒。
        narrow = estimated.model_copy(update={"parameters": {**estimated.parameters,
                                                             "limit": 2}})
        overflow = self.script_conn(rows=[(date(2026, 9, 1), Decimal("1.00"))] * 3)
        self.assertEqual(refusal(lambda: self.execute_plan(overflow, narrow,
                                                           deadline=self.deadline)),
                         "exploration_row_limit_exceeded")
        self.assertEqual(overflow.result.sizes, [3])           # 拒前确实多取了那一行
        # 只差那一行：limit 回到 3 就正常出数（断言不是恒真）。
        wide = narrow.model_copy(update={"parameters": {**narrow.parameters,
                                                        "limit": 3}})
        enough = self.script_conn(rows=[(date(2026, 9, 1), Decimal("1.00"))] * 3)
        self.assertEqual(len(self.execute_plan(enough, wide, deadline=self.deadline)[1]), 3)

    def test_internal_aliases_are_passed_through_untouched(self):
        """授权列不在这层换成 ref：那是投影层唯一的职责（带 shop_refs 才能做）。"""
        draft = compile_for("metric-cost-total", groups=[DAY_COST, SHOP_COST])
        plan = validate(draft, context=policy_context())
        conn = self.script_conn(
            description=["field_product_cost_daily_day", SHOP_ID_COLUMN, "metric_cost_total"],
            rows=[(date(2026, 9, 1), "S1", Decimal("12.30"))])
        columns, rows = self.execute_plan(
            conn, self.estimate_plan(conn, plan, deadline=self.deadline),
            deadline=self.deadline)
        self.assertEqual(columns, [DAY_COST, SHOP_ID_COLUMN, "metric-cost-total"])
        self.assertEqual(rows[0][1], "S1")     # 未投影的行仍带真店号（投影存在的理由）

    # --- 预算与拒绝 ------------------------------------------------------------

    def test_the_gate_reads_its_budget_from_the_published_constants(self):
        """阈值改动必须真的被读到：否则常量只是注释。"""
        budget = budget_exception()
        over_rows = GateScriptConn(explain_rows=1, explain_cost=1.0)
        with mock.patch("bi_agent.exploration.repository.MAX_ESTIMATED_ROWS", 0):
            with self.assertRaises(budget) as caught:
                self.estimated(over_rows)
            self.assertEqual(caught.exception.reason, "estimated_rows")
        over_cost = GateScriptConn(explain_rows=1, explain_cost=1.0)
        with mock.patch("bi_agent.exploration.repository.MAX_TOTAL_COST", Decimal("0")):
            with self.assertRaises(budget) as caught:
                self.estimated(over_cost)
            self.assertEqual(caught.exception.reason, "total_cost")
        # 只差阈值：同一个结果不收紧时照旧通过（断言不是恒假）。
        self.assertIsNotNone(self.estimated(GateScriptConn(explain_rows=1,
                                                           explain_cost=1.0)))

    def test_explain_output_without_the_two_numbers_is_unavailable(self):
        for payload in ([{"Plan": {"Plan Rows": 1}}], [], [{"nope": {}}],
                        [{"Plan": {"Plan Rows": 1, "Total Cost": float("nan")}}],
                        [{"Plan": {"Plan Rows": True, "Total Cost": 1.0}}]):
            with self.subTest(payload=payload):
                conn = GateScriptConn()
                conn.explain = payload
                self.assertEqual(refusal(lambda: self.estimated(conn)),
                                 "exploration_estimate_unavailable")
                self.assertEqual(conn.entered, 1)
                self.assertEqual(conn.exited, 1)      # 被拒的事务照样自关

    def test_database_failures_are_mapped_to_stable_codes(self):
        for failure, expected in (
                (psycopg.errors.QueryCanceled("canceling statement due to statement timeout"),
                 "exploration_statement_timeout"),
                (psycopg.errors.UndefinedTable('relation "reporting.v_x" does not exist'),
                 "exploration_query_rejected")):
            with self.subTest(expected=expected):
                conn = GateScriptConn(fail_with=failure)
                with self.assertRaises(ValueError) as caught:
                    self.estimated(conn)
                self.assertEqual(str(caught.exception), expected)
                self.assertIs(caught.exception.__cause__, None)   # 带 SQL 的原文不外泄
                self.assertTrue(caught.exception.__suppress_context__)

    def test_deadline_reserve_touches_no_database(self):
        """剩余不足 0.1 秒：不排包、不取数、不 BEGIN。"""
        estimated = self.estimated(self.script_conn())
        for name, action in (("estimate_plan", lambda conn: self.estimate_plan(
                                 conn, self.plan, deadline=time.monotonic() + 0.05)),
                             ("execute_plan", lambda conn: self.execute_plan(
                                 conn, estimated, deadline=time.monotonic() + 0.05))):
            with self.subTest(entry=name):
                self.assertEqual(refusal(lambda: action(RefusingConn())),
                                 "exploration_deadline_exceeded")
        # 只差预留：预算足够时同一对入口真的会碰库（否则上面那条是恒假）。
        conn = self.script_conn()
        self.estimate_plan(conn, self.plan, deadline=time.monotonic() + 5.0)
        self.assertEqual(conn.entered, 1)

    def test_deadline_must_be_a_monotonic_number(self):
        estimate_plan, execute_plan = self.estimate_plan, self.execute_plan
        for bad in (None, "30", True, float("inf"), float("nan"), date(2026, 9, 1)):
            with self.subTest(deadline=repr(bad)):
                with self.assertRaises(TypeError) as caught:
                    estimate_plan(RefusingConn(), self.plan, deadline=bad)
                self.assertEqual(str(caught.exception),
                                 "exploration_deadline_contract_required")
                with self.assertRaises(TypeError):
                    execute_plan(RefusingConn(), self.plan, deadline=bad)
        # 只差预留量：给到 0.2 秒就真的去碰库（否则上面那五条拒绝是恒假）。
        proceed = self.script_conn()
        self.assertIsNotNone(estimate_plan(proceed, self.plan,
                                           deadline=time.monotonic() + 0.2))
        self.assertEqual(proceed.entered, 1)

    def test_unestimated_plans_never_reach_the_database(self):
        self.assertEqual(refusal(lambda: self.execute_plan(
            RefusingConn(), self.plan, deadline=self.deadline)),
            "exploration_plan_not_estimated")

    def test_over_budget_estimates_cannot_be_forged_past_execution(self):
        """执行前再比一道：有人 `model_copy` 一个伪估计也迭不过去。"""
        forged = [
            ({"estimated_rows": 10 ** 9, "estimated_total_cost": Decimal("1")},
             "exploration_budget_exceeded:estimated_rows"),
            ({"estimated_rows": 1, "estimated_total_cost": Decimal("100000.01")},
             "exploration_budget_exceeded:total_cost"),
        ]
        for update, expected in forged:
            with self.subTest(expected=expected):
                plan = self.plan.model_copy(update=update)
                with self.assertRaises(budget_exception()) as caught:
                    self.execute_plan(RefusingConn(), plan, deadline=self.deadline)
                self.assertEqual(str(caught.exception), expected)

    def test_plan_shape_guards_run_before_any_database_call(self):
        """六道前置门都在 `conn` 之前：RefusingConn 一碰就判红。"""
        from bi_agent.exploration.models import ValidatedQueryPlan

        cases = [
            ("exploration_statement_not_select",
             {"sql_text": 'UPDATE bi.orders SET x = 1 AS "n" WHERE 1 = NULL'}),
            ("exploration_multiple_statements",
             {"sql_text": 'SELECT 1 AS "n"; DELETE FROM bi.orders'}),
            ("exploration_alias_unbound", {"sql_text": "SELECT 1"}),
            ("exploration_alias_unbound",
             {"sql_text": 'SELECT a AS "one", b AS "one"'}),
            ("exploration_parameter_mismatch", {"parameters": {"limit": 100}}),
            ("exploration_parameter_invalid", {"parameters": {**SERVER_PARAMETERS,
                                                              "limit": 5_000}}),
            ("exploration_parameter_invalid", {"parameters": {**SERVER_PARAMETERS,
                                                              "limit": True}}),
            ("exploration_catalog_version_mismatch",
             {"catalog_version": "semantic/2020-01-01.1"}),
        ]
        for expected, overrides in cases:
            with self.subTest(reason=expected):
                plan = self.plan.model_copy(
                    update={"estimated_rows": None, "estimated_total_cost": None,
                            **overrides})
                self.assertIsInstance(plan, ValidatedQueryPlan)
                for entry in (self.estimate_plan, self.execute_plan):
                    self.assertEqual(refusal(lambda: entry(RefusingConn(), plan,
                                                           deadline=self.deadline)),
                                     expected)

    def test_column_description_mismatch_is_refused(self):
        conn = self.script_conn()
        estimated = self.estimated(conn)
        other = self.script_conn(description=["totally_different", "metric_cost_total"])
        self.assertEqual(refusal(lambda: self.execute_plan(
            other, estimated, deadline=self.deadline)), "exploration_column_mismatch")
        self.assertEqual(other.entered, 1)
        self.assertEqual(other.exited, 1)          # 失败的事务同样自关
        self.assertEqual(other.result.sizes, [])   # 列名不对就连一行都不取

    def test_entry_points_reject_foreign_input_objects(self):
        for bad_plan in (None, "SELECT 1", {}, compile_for("metric-cost-total")):
            with self.subTest(plan=type(bad_plan).__name__):
                with self.assertRaises(TypeError) as caught:
                    self.estimate_plan(RefusingConn(), bad_plan, deadline=self.deadline)
                self.assertEqual(str(caught.exception),
                                 "exploration_plan_contract_required")


class ExplorationProjectionTests(unittest.TestCase):
    """计划 Task 4 Step 1/5：内部列换 opaque ref、值按目录声明类型出、公开载荷有上限。"""

    DECIMAL_COLUMN = "metric-cost-total"
    INTEGER_COLUMN = "metric-paid-orders"
    DATE_COLUMN = DAY_COST
    DATETIME_COLUMN = "field-listing-items-captured-at"
    TEXT_COLUMN = "field-product-cost-daily-line-kind"
    BOOLEAN_COLUMN = "field-shops-enabled"

    # --- 计划 Step 1 的种子用例 --------------------------------------------------

    def test_internal_shop_id_becomes_ref_and_decimal_becomes_string(self):
        from bi_agent.exploration.models import ExplorationColumn

        columns, rows = project([SHOP_ID_COLUMN, self.DECIMAL_COLUMN],
                                [("S1", Decimal("12.30"))], shop_refs=SHOP_REFS)
        self.assertEqual(rows, [{SHOP_REF_COLUMN: "ent-shop-one",
                                 self.DECIMAL_COLUMN: "12.30"}])
        self.assertNotIn("S1", repr(rows))
        self.assertEqual([(column.ref, column.data_type) for column in columns],
                         [(SHOP_REF_COLUMN, "ref"), (self.DECIMAL_COLUMN, "decimal")])
        for column in columns:
            self.assertIsInstance(column, ExplorationColumn)
        # 只差映射：没有 shop_refs 就不允许把真店号发出去。
        self.assertEqual(projection_reason([SHOP_ID_COLUMN, self.DECIMAL_COLUMN],
                                           [("S9", Decimal("12.30"))]),
                         "exploration_shop_not_registered")

    # --- 值的表示 --------------------------------------------------------------

    def test_decimal_is_plain_text_and_keeps_its_scale(self):
        for value, expected in ((Decimal("12.30"), "12.30"), (Decimal("0"), "0"),
                                (Decimal("-0.005"), "-0.005"), (Decimal("1E+3"), "1000"),
                                (Decimal("12345678901234567890.12"),
                                 "12345678901234567890.12")):
            with self.subTest(value=str(value)):
                _, rows = project([self.DECIMAL_COLUMN], [(value,)])
                self.assertEqual(rows, [{self.DECIMAL_COLUMN: expected}])

    def test_non_finite_decimals_are_refused(self):
        for value in (Decimal("NaN"), Decimal("Infinity"), Decimal("-Infinity")):
            with self.subTest(value=str(value)):
                self.assertEqual(projection_reason([self.DECIMAL_COLUMN], [(value,)]),
                                 "exploration_value_not_finite")
        # 只差有限性：同列同形状的一个有限值照旧通过。
        self.assertEqual(project([self.DECIMAL_COLUMN], [(Decimal("0.00"),)])[1],
                         [{self.DECIMAL_COLUMN: "0.00"}])

    def test_dates_and_aware_datetimes_become_iso_text(self):
        _, rows = project([self.DATE_COLUMN], [(date(2026, 9, 1),)])
        self.assertEqual(rows, [{self.DATE_COLUMN: "2026-09-01"}])
        _, rows = project([self.DATETIME_COLUMN],
                          [(datetime(2026, 9, 12, 8, 30, tzinfo=BEIJING),)])
        self.assertEqual(rows, [{self.DATETIME_COLUMN: "2026-09-12T08:30:00+08:00"}])
        # 不换算时区：换了就不是那一句话的口径。
        moment = datetime(2026, 9, 12, 8, 30, tzinfo=timezone.utc)
        _, rows = project([self.DATETIME_COLUMN], [(moment,)])
        self.assertEqual(rows, [{self.DATETIME_COLUMN: "2026-09-12T08:30:00+00:00"}])

    def test_naive_datetimes_are_refused(self):
        self.assertEqual(projection_reason([self.DATETIME_COLUMN],
                                           [(datetime(2026, 9, 12, 8, 30),)]),
                         "exploration_datetime_naive")
        # 同一列、只差时区：带上就过（不是“整列都拒”）。
        self.assertEqual(project([self.DATETIME_COLUMN],
                                 [(datetime(2026, 9, 12, 8, 30, tzinfo=BEIJING),)])[1],
                         [{self.DATETIME_COLUMN: "2026-09-12T08:30:00+08:00"}])

    def test_temporal_types_cannot_trade_places(self):
        self.assertEqual(projection_reason([self.DATE_COLUMN],
                                           [(datetime(2026, 9, 12, 8, 30, tzinfo=BEIJING),)]),
                         "exploration_value_type_unsupported")
        self.assertEqual(projection_reason([self.DATETIME_COLUMN], [(date(2026, 9, 1),)]),
                         "exploration_value_type_unsupported")

    def test_integer_columns_take_integers_and_integral_decimals_only(self):
        for value, expected in ((7, 7), (Decimal("7"), 7), (Decimal("7E+1"), 70),
                                (0, 0), (-12, -12)):
            with self.subTest(value=str(value)):
                _, rows = project([self.INTEGER_COLUMN], [(value,)])
                self.assertEqual(rows, [{self.INTEGER_COLUMN: expected}])
        for value in (Decimal("7.5"), 7.0, True, "7", b"7"):
            with self.subTest(value=repr(value)):
                self.assertEqual(projection_reason([self.INTEGER_COLUMN], [(value,)]),
                                 "exploration_value_type_unsupported")

    def test_boolean_columns_take_only_booleans(self):
        _, rows = project([self.BOOLEAN_COLUMN], [(True,), (False,)])
        self.assertEqual(rows, [{self.BOOLEAN_COLUMN: True}, {self.BOOLEAN_COLUMN: False}])
        for value in (1, 0, Decimal("1"), "true"):
            with self.subTest(value=repr(value)):
                self.assertEqual(projection_reason([self.BOOLEAN_COLUMN], [(value,)]),
                                 "exploration_value_type_unsupported")

    def test_text_columns_are_bounded_and_never_carry_raw_ids(self):
        _, rows = project([self.TEXT_COLUMN], [("gift",)])
        self.assertEqual(rows, [{self.TEXT_COLUMN: "gift"}])
        project([self.TEXT_COLUMN], [("x" * PLAN_MAX_TEXT_CHARS,)])      # 上界本身合法
        self.assertEqual(projection_reason([self.TEXT_COLUMN], [("x" * (PLAN_MAX_TEXT_CHARS + 1),)]),
                         "exploration_text_too_large")
        # 真店号从任何一列回都算泄露：shop_refs 的 key 就是服务端的已知集。
        self.assertEqual(projection_reason([self.TEXT_COLUMN], [("S1",)]),
                         "exploration_raw_identifier")

    def test_unsupported_value_types_are_refused(self):
        for value in (1.5, b"bytes", [1], {"a": 1}, {1, 2}, Decimal, object()):
            with self.subTest(value=type(value).__name__):
                self.assertEqual(projection_reason([self.DECIMAL_COLUMN], [(value,)]),
                                 "exploration_value_type_unsupported")

    def test_null_is_the_only_value_that_projects_to_null(self):
        columns, rows = project([self.DATE_COLUMN, self.DECIMAL_COLUMN,
                                 self.TEXT_COLUMN, self.BOOLEAN_COLUMN],
                                [(None, None, None, None)])
        self.assertEqual(rows, [{self.DATE_COLUMN: None, self.DECIMAL_COLUMN: None,
                                 self.TEXT_COLUMN: None, self.BOOLEAN_COLUMN: None}])
        self.assertEqual([column.data_type for column in columns],
                         ["date", "decimal", "text", "boolean"])   # 空列不推 type

    # --- 列身份 --------------------------------------------------------------

    def test_public_columns_must_be_projectable_catalog_refs(self):
        _, rows = project([self.DECIMAL_COLUMN], [(Decimal("1.00"),)])
        self.assertEqual(rows, [{self.DECIMAL_COLUMN: "1.00"}])
        for ref in ("metric-cost-total-typo", "field-day"):
            with self.subTest(ref=ref):
                self.assertEqual(projection_reason([ref], [(Decimal("1.00"),)]),
                                 "exploration_ref_unregistered")
        # 已知但不是输出列的 ref（视图/实体/JOIN）不能当输出列。
        for ref in (COST_VIEW, "entity-shop", COST_SHOPS_JOIN):
            with self.subTest(ref=ref):
                self.assertEqual(projection_reason([ref], [("x",)]),
                                 "exploration_column_not_public")
        for ref in ("metric-not-registered", "field-not-registered", "whatever-else",
                    "catalog"):
            with self.subTest(ref=ref):
                self.assertEqual(projection_reason([ref], [("x",)]),
                                 "exploration_ref_unregistered")

    def test_raw_identifier_columns_cannot_be_published_under_their_own_ref(self):
        """授权列与 ERP 主键列只能走 `_shop_id`，不然真号就是一条看起来正常的列。"""
        for ref, value in ((SHOP_COST, "S1"), (PRODUCT_COST, "ERP-9"),
                           ("field-shop-daily-paid-orders", 3),
                           (POOL_PHYSICAL, "POOL-1")):
            with self.subTest(ref=ref):
                self.assertEqual(projection_reason([ref], [(value,)]),
                                 "exploration_column_not_public")
        # 只差写法：同一个店号走 `_shop_id` 就变成 opaque ref。
        _, rows = project([SHOP_ID_COLUMN], [("S1",)])
        self.assertEqual(rows, [{SHOP_REF_COLUMN: "ent-shop-one"}])

    def test_underscore_columns_are_all_refused_except_the_authorized_one(self):
        for column in ("_pool_id", "_erp_id", "_product_id", "_shop_id_2", "_", "_S1",
                       "_shop_id "):
            with self.subTest(column=column):
                self.assertEqual(projection_reason([column], [("anything",)]),
                                 "exploration_internal_column_forbidden")
        self.assertEqual(project([SHOP_ID_COLUMN], [("S1",)])[1],
                         [{SHOP_REF_COLUMN: "ent-shop-one"}])

    def test_duplicate_columns_are_refused(self):
        cases = [
            ([self.DECIMAL_COLUMN, self.DECIMAL_COLUMN],
             [(Decimal("1.00"), Decimal("2.00"))]),
            ([SHOP_ID_COLUMN, SHOP_ID_COLUMN], [("S1", "S2")]),
            ([self.DATE_COLUMN, self.DATE_COLUMN],
             [(date(2026, 9, 1), date(2026, 9, 2))]),
        ]
        for columns, rows in cases:
            with self.subTest(columns=columns):
                self.assertEqual(projection_reason(columns, rows),
                                 "exploration_column_duplicate")

    # --- 形状与预算 ----------------------------------------------------------

    def test_row_width_must_match_the_declared_columns(self):
        self.assertEqual(projection_reason([self.DECIMAL_COLUMN, self.DATE_COLUMN],
                                           [(Decimal("1.00"),)]),
                         "exploration_column_mismatch")
        self.assertEqual(projection_reason([self.DECIMAL_COLUMN],
                                           [(Decimal("1.00"), date(2026, 9, 1))]),
                         "exploration_column_mismatch")
        self.assertEqual(project([self.DECIMAL_COLUMN], [(Decimal("1.00"),)])[1],
                         [{self.DECIMAL_COLUMN: "1.00"}])

    def test_a_result_without_columns_is_not_a_result(self):
        self.assertEqual(projection_reason([], []), "exploration_column_mismatch")
        self.assertEqual(projection_reason([], [("x",)]), "exploration_column_mismatch")

    def test_row_count_stays_within_the_published_budget(self):
        from bi_agent.exploration.models import MAX_ROWS

        boundary = [(Decimal("1.00"),)] * MAX_ROWS
        self.assertEqual(len(project([self.DECIMAL_COLUMN], boundary)[1]), MAX_ROWS)
        self.assertEqual(
            projection_reason([self.DECIMAL_COLUMN], boundary + [(Decimal("1.00"),)]),
            "exploration_row_limit_exceeded")

    def test_the_result_budget_is_utf8_bytes_of_the_compact_projection(self):
        text = "领" * 1000                       # 非 ASCII：字节数 > 字符数，正是预算要看的东西
        under = [(text,) for _ in range(60)]      # 60 × 3000 B ≈ 180 KB
        over = [(text,) for _ in range(100)]      # 100 × 3000 B ≈ 300 KB
        declared, rows = project([self.TEXT_COLUMN], under)
        measured = len(json.dumps({"columns": [column.model_dump() for column in declared],
                                   "rows": rows}, ensure_ascii=False,
                                  separators=(",", ":")).encode("utf-8"))
        self.assertLessEqual(measured, PLAN_MAX_RESULT_BYTES)
        self.assertGreater(measured, 100_000)      # 确实是个大载荷，不是一句空话
        self.assertEqual(projection_reason([self.TEXT_COLUMN], over),
                         "exploration_result_too_large")

    def test_the_shop_reference_table_must_be_a_reference_table(self):
        for shop_refs in ({"S1": "ent shop"}, {"S1": "S1"}, {"": "ent-shop-one"},
                          {"S1": 1}, {"S1": None}, {"S1": "ent-shop-one", "S1 ": "x"}):
            with self.subTest(shop_refs=repr(shop_refs)):
                self.assertEqual(
                    projection_reason([SHOP_ID_COLUMN], [("S1",)], shop_refs=shop_refs),
                    "exploration_shop_refs_invalid")
        self.assertEqual(project([SHOP_ID_COLUMN], [("S1",)],
                                 shop_refs={"S1": "ent-shop-one"})[1],
                         [{SHOP_REF_COLUMN: "ent-shop-one"}])
        with self.assertRaises(TypeError):
            project([SHOP_ID_COLUMN], [("S1",)], shop_refs=None)

    def test_rejections_never_echo_values_refs_or_shop_ids(self):
        """错误只说原因：投影入参里的任何一个字面量都不许出现在消息里。"""
        cases = [
            ([SHOP_ID_COLUMN], [("S1",)], {"OTHER": "ent-shop-one"}),
            (["metric-not-registered"], [("x",)], SHOP_REFS),
            ([self.DATE_COLUMN], [(datetime(2026, 9, 12, 8, 30),)], SHOP_REFS),
            ([self.TEXT_COLUMN], [("秘密提问" * 2_000,)], SHOP_REFS),
            ([self.DECIMAL_COLUMN], [(123456.78,)], SHOP_REFS),
            ([self.DECIMAL_COLUMN], [(float("nan"),)], SHOP_REFS),
        ]
        for columns, rows, shop_refs in cases:
            with self.subTest(columns=columns):
                reason = projection_reason(columns, rows, shop_refs=shop_refs)
                self.assertTrue(reason.startswith("exploration_"), reason)
                for leak in ("S1", "OTHER", "metric-not-registered", "秘密", "123456",
                             "nan", "2026", "ent-shop-one"):
                    self.assertNotIn(leak, reason)

    def test_projection_input_must_be_sequences_of_rows(self):
        for columns, rows in ((None, []), ("metric-cost-total", []),
                              ([self.DECIMAL_COLUMN], {Decimal("1.00")}),
                              ([self.DECIMAL_COLUMN], ["S1"])):
            with self.subTest(columns=type(columns).__name__, rows=repr(rows)[:24]):
                with self.assertRaises(TypeError):
                    project(columns, rows)
        self.assertEqual(project([self.DECIMAL_COLUMN], ())[1], [])

    def test_empty_rows_still_publish_declared_columns(self):
        columns, rows = project([SHOP_ID_COLUMN, self.DECIMAL_COLUMN], [])
        self.assertEqual([(column.ref, column.data_type) for column in columns],
                         [(SHOP_REF_COLUMN, "ref"), (self.DECIMAL_COLUMN, "decimal")])
        self.assertEqual(rows, [])


@unittest.skipUnless(os.getenv("BI_TEST_ADMIN_DSN"), "未配置独立测试数据库")
class ExplorationRepositoryTests(ExplorationLivePlanFixture, unittest.TestCase):
    """计划 Task 4 Step 1/3/4/6：真库上的成本门、只读执行、列比对与数据库角色。"""

    # --- EXPLAIN 成本门 --------------------------------------------------------

    def test_estimate_plan_reports_the_explain_numbers_of_the_real_plan(self):
        from bi_agent.exploration.models import ValidatedQueryPlan

        estimate_plan, _ = repository_entries()
        plan = self.plan_for()
        estimated = estimate_plan(self.conn, plan, deadline=self.deadline())
        root = self.explain_root(plan)                     # 独立重跑一次 EXPLAIN
        self.assertIsInstance(estimated, ValidatedQueryPlan)
        self.assertEqual(estimated.estimated_rows, root["Plan Rows"])
        self.assertEqual(estimated.estimated_total_cost, Decimal(str(root["Total Cost"])))
        self.assertLessEqual(estimated.estimated_rows, PLAN_MAX_ESTIMATED_ROWS)
        self.assertLessEqual(estimated.estimated_total_cost, PLAN_MAX_TOTAL_COST)
        # 同一句话、同一份参数、同一个指纹：门只贴两个估算值。
        self.assertEqual(estimated.statement_fingerprint, plan.statement_fingerprint)
        self.assertEqual(estimated.sql_text, plan.sql_text)
        self.assertEqual(estimated.parameters, plan.parameters)
        self.assertEqual(estimated.selected_refs, plan.selected_refs)
        self.assertIsNot(estimated.parameters, plan.parameters)
        self.assertIsNone(plan.estimated_rows)

    def test_estimated_rows_over_budget_are_refused(self):
        estimate_plan, _ = repository_entries()
        plan = server_plan(OVER_ROWS_SQL)
        root = self.explain_root(plan)
        self.assertGreater(root["Plan Rows"], PLAN_MAX_ESTIMATED_ROWS)
        with self.assertRaises(budget_exception()) as caught:
            estimate_plan(self.conn, plan, deadline=self.deadline())
        self.assertEqual(caught.exception.reason, "estimated_rows")
        self.assertEqual(str(caught.exception), "exploration_budget_exceeded:estimated_rows")
        self.assertNotIn("pg_class", str(caught.exception))    # 不回显语句

    def test_total_cost_over_budget_is_refused(self):
        estimate_plan, _ = repository_entries()
        plan = server_plan(OVER_COST_SQL)
        root = self.explain_root(plan)
        # 带 `count(*)` 的根节点只有一行：能走到 total_cost 分支，而不是被行数先拦下。
        self.assertLessEqual(root["Plan Rows"], PLAN_MAX_ESTIMATED_ROWS)
        self.assertGreater(root["Total Cost"], float(PLAN_MAX_TOTAL_COST))
        with self.assertRaises(budget_exception()) as caught:
            estimate_plan(self.conn, plan, deadline=self.deadline())
        self.assertEqual(caught.exception.reason, "total_cost")

    def test_the_gate_consults_the_published_budget_constants(self):
        """真库、真 EXPLAIN，只改阈值：两个分支各自可判。"""
        estimate_plan, _ = repository_entries()
        budget = budget_exception()
        plan = self.plan_for()
        with mock.patch("bi_agent.exploration.repository.MAX_ESTIMATED_ROWS", 0):
            with self.assertRaises(budget) as caught:
                estimate_plan(self.conn, plan, deadline=self.deadline())
            self.assertEqual(caught.exception.reason, "estimated_rows")
        with mock.patch("bi_agent.exploration.repository.MAX_TOTAL_COST", Decimal("0")):
            with self.assertRaises(budget) as caught:
                estimate_plan(self.conn, plan, deadline=self.deadline())
            self.assertEqual(caught.exception.reason, "total_cost")
        # 只差阈值：不收紧时同一句话照旧通过。
        self.assertIsNotNone(estimate_plan(self.conn, plan, deadline=self.deadline()))

    def test_gate_opens_a_read_only_timeout_bounded_transaction(self):
        """真库上反问一句：门禁自己的事务真的只读、真的带 5 秒上限。"""
        estimate_plan, execute_plan = repository_entries()
        plan = self.plan_for()
        for entry in (estimate_plan, execute_plan):
            with self.subTest(entry=entry.__name__):
                estimated = estimate_plan(self.conn, plan, deadline=self.deadline())
                probe = GateProbeConn(self.conn)
                if entry is estimate_plan:
                    entry(probe, plan, deadline=self.deadline())
                else:
                    entry(probe, estimated, deadline=self.deadline())
                self.assertEqual(probe.read_only, "on")
                self.assertEqual(probe.timeout, "5s")
                self.assertTrue(probe.write_refused)
                self.assertEqual(probe.statements, [
                    READ_ONLY_STATEMENT, STATEMENT_TIMEOUT_STATEMENT,
                    EXPLAIN_STATEMENT_PREFIX + plan.sql_text if entry is estimate_plan
                    else plan.sql_text])

    def test_statement_timeout_really_cancels_a_slow_query(self):
        """真取消：5 秒到点，而不只是一个映别。"""
        estimate_plan, execute_plan = repository_entries()
        slow = server_plan('SELECT pg_sleep(6) AS "n"')
        estimated = estimate_plan(self.conn, slow, deadline=self.deadline())
        self.assertLessEqual(estimated.estimated_rows, PLAN_MAX_ESTIMATED_ROWS)
        started = time.monotonic()
        with self.assertRaises(ValueError) as caught:
            execute_plan(self.conn, estimated, deadline=self.deadline() + 30.0)
        elapsed = time.monotonic() - started
        self.assertEqual(str(caught.exception), "exploration_statement_timeout")
        self.assertGreaterEqual(elapsed, PLAN_STATEMENT_TIMEOUT_MS / 1000.0 - 0.5)
        self.assertLess(elapsed, PLAN_STATEMENT_TIMEOUT_MS / 1000.0 + 5.0)
        self.assertEqual(self.conn.execute("SELECT 1").fetchone(), (1,))   # 连接仍可用

    # --- 受控执行 --------------------------------------------------------------

    def test_execution_returns_declared_columns_and_their_rows(self):
        estimate_plan, execute_plan = repository_entries()
        plan = self.plan_for()
        columns, rows = execute_plan(self.conn, estimate_plan(self.conn, plan,
                                                              deadline=self.deadline()),
                                     deadline=self.deadline())
        self.assertEqual(columns, [DAY_COST, SHOP_ID_COLUMN, "metric-cost-total"])
        self.assertGreaterEqual(len(rows), 2)                # 溢出用例需要 limit-1 以上的行数
        for row in rows:
            self.assertIsInstance(row, tuple)
            self.assertEqual(len(row), len(columns))
            self.assertEqual(row[1], self.shop_id)           # 未投影：真店号仍在行里
        first = rows[0]
        self.assertIs(type(first[0]), date)                  # 列序就是声明序：日、店、金额
        self.assertIs(type(first[2]), Decimal)
        _, singles = execute_plan(self.conn, estimate_plan(
            self.conn, self.plan_for(groups=(DAY_COST,)), deadline=self.deadline()),
            deadline=self.deadline())
        self.assertEqual([row[0] for row in singles][0], first[0])   # 同一天的两条查询一致

    def test_execution_requires_an_estimated_plan(self):
        _, execute_plan = repository_entries()
        self.assertEqual(refusal(lambda: execute_plan(self.conn, self.plan_for(),
                                                      deadline=self.deadline())),
                         "exploration_plan_not_estimated")

    def test_row_overflow_is_refused_without_truncation(self):
        """文本里的字面 LIMIT 与参数不一致：取到 limit+1 行就整条拒，不丢行交数。"""
        estimate_plan, execute_plan = repository_entries()
        estimated = estimate_plan(self.conn, self.plan_for(), deadline=self.deadline())
        _, rows = execute_plan(self.conn, estimated, deadline=self.deadline())
        overflowing = estimated.model_copy(update={
            "sql_text": estimated.sql_text.replace("LIMIT %(limit)s", f"LIMIT {len(rows)}"),
            "parameters": {**estimated.parameters, "limit": len(rows) - 1}})
        self.assertEqual(refusal(lambda: execute_plan(self.conn, overflowing,
                                                      deadline=self.deadline())),
                         "exploration_row_limit_exceeded")
        # 只差那一行：limit 回到真实行数就正常出数（断言不是恒真）。
        restored = overflowing.model_copy(update={"parameters": {**overflowing.parameters,
                                                                "limit": len(rows)}})
        self.assertEqual(len(execute_plan(self.conn, restored,
                                          deadline=self.deadline())[1]), len(rows))

    def test_column_description_mismatch_is_refused_on_the_real_database(self):
        """`CAST(x AS "numeric")` 里的 `AS` 不是输出列：声明与回列逐项比才能发现。"""
        estimate_plan, execute_plan = repository_entries()
        plan = self.plan_for(groups=())
        mutated = plan.model_copy(update={"sql_text": plan.sql_text.replace(
            'sum(fact."cost_total")', 'cast(sum(fact."cost_total") AS "numeric")')})
        self.assertIn('AS "numeric"', mutated.sql_text)
        estimated = estimate_plan(self.conn, mutated, deadline=self.deadline())
        self.assertEqual(refusal(lambda: execute_plan(self.conn, estimated,
                                                      deadline=self.deadline())),
                         "exploration_column_mismatch")
        # 只差那个 cast：原计划同一入口正常回列。
        clean = estimate_plan(self.conn, plan, deadline=self.deadline())
        self.assertEqual(execute_plan(self.conn, clean, deadline=self.deadline())[0],
                         ["metric-cost-total"])

    def test_database_failures_never_echo_the_statement(self):
        """缺表之类的事实失败只给稳定码：SQL 原文只能进诊断表。"""
        _, execute_plan = repository_entries()
        missing = self.plan_for().model_copy(update={
            "estimated_rows": 1, "estimated_total_cost": Decimal("1")})
        missing = missing.model_copy(update={"sql_text": missing.sql_text.replace(
            '"reporting"."v_product_cost_daily"', '"reporting"."v_not_there"')})
        with self.assertRaises(ValueError) as caught:
            execute_plan(self.conn, missing, deadline=self.deadline())
        self.assertEqual(str(caught.exception), "exploration_query_rejected")
        self.assertIs(caught.exception.__cause__, None)
        for leak in ("SELECT", "FROM", "v_product_cost_daily", "v_not_there",
                     "cost_total", self.shop_id, "%(limit)s"):
            self.assertNotIn(leak, str(caught.exception))

    # --- 投影接线 --------------------------------------------------------------

    def test_end_to_end_result_publishes_shop_refs_not_raw_ids(self):
        estimate_plan, execute_plan = repository_entries()
        estimated = estimate_plan(self.conn, self.plan_for(), deadline=self.deadline())
        columns, rows = execute_plan(self.conn, estimated, deadline=self.deadline())
        declared, safe_rows = project(columns, rows, shop_refs={self.shop_id: self.shop_ref})
        self.assertEqual([(column.ref, column.data_type) for column in declared],
                         [(DAY_COST, "date"), (SHOP_REF_COLUMN, "ref"),
                          ("metric-cost-total", "decimal")])
        self.assertEqual({key for row in safe_rows for key in row},
                         {DAY_COST, SHOP_REF_COLUMN, "metric-cost-total"})
        self.assertEqual({row[SHOP_REF_COLUMN] for row in safe_rows}, {self.shop_ref})
        self.assertNotIn(self.shop_id,
                         [value for row in safe_rows for value in row.values()])
        for row, raw in zip(safe_rows, rows):
            self.assertEqual(row[DAY_COST], raw[0].isoformat())
            self.assertRegex(row["metric-cost-total"], r"^-?\d+\.\d+$")
            self.assertEqual(Decimal(row["metric-cost-total"]), raw[2])
        measured = json.dumps({"columns": [c.model_dump() for c in declared], "rows": safe_rows},
                              ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.assertLessEqual(len(measured), PLAN_MAX_RESULT_BYTES)

    # --- 数据库角色（计划 Step 1 的种子用例）--------------------------------- --

    @unittest.skipUnless(os.getenv("BI_TEST_READER_DSN"), "未配置测试库的只读角色 DSN")
    def test_write_statement_is_refused_by_database_role(self):
        """只读角色的拒绍必须真的在库里跑过：不 mock、不比空、不降级成文字检查。

        每一条拒绍各用一个连接：库里报错会把当前事务置为 aborted，同一事务里再发一句
        拿到的是 `InFailedSqlTransaction`，那就不再是“角色被拒”的证据。
        """
        with psycopg.connect(os.environ["BI_TEST_READER_DSN"]) as conn:
            self.assertEqual(conn.info.user, "bi_reader")
            self.assertTrue(conn.info.dbname.endswith("_test"))
            with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                conn.execute("DELETE FROM bi.orders")
        for statement in ("SELECT count(*) FROM bi.orders",
                          "INSERT INTO bi.shops(shop_id) VALUES ('forbidden')",
                          "SELECT pg_read_file('/etc/passwd')"):
            with self.subTest(statement=statement), \
                    psycopg.connect(os.environ["BI_TEST_READER_DSN"]) as conn:
                with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                    conn.execute(statement)
        # 只差对象：获准的 reporting 视图照旧能读（拒绍不是“这个连接什么都查不了”）。
        with psycopg.connect(os.environ["BI_TEST_READER_DSN"]) as conn:
            conn.execute("SELECT count(*) FROM reporting.v_product_cost_daily").fetchone()


@unittest.skipUnless(os.getenv("BI_TEST_ADMIN_DSN"), "未配置独立测试数据库")
class ExplorationMultiMetricTests(ExplorationLivePlanFixture, unittest.TestCase):
    """两个指标、同一基表：一条只读语句里聚完，不逐指标一圈（N+1）。"""

    def test_a_plan_within_both_budgets_executes_in_one_statement(self):
        estimate_plan, execute_plan = repository_entries()
        plan = self.plan_for(metrics=("metric-cost-total", "metric-sales-amount"),
                             groups=(DAY_COST,))
        probe = GateProbeConn(self.conn)
        estimated = estimate_plan(probe, plan, deadline=self.deadline())
        columns, rows = execute_plan(self.conn, estimated, deadline=self.deadline())
        self.assertEqual(columns, [DAY_COST, "metric-cost-total", "metric-sales-amount"])
        self.assertTrue(rows)
        self.assertEqual(probe.statements.count(READ_ONLY_STATEMENT), 1)


# --- Task 5（阶段一）运行域契约：九节点链、Artifact、原因码与私有诊断 ----------------
#
# 本切片只交付「迁移 + 运行层契约」：`graph.py`、`tool.py` 与 Agent 接线属阶段二，
# 所以这里钉的是领域注册表、`exploration_result` 载荷校验与两个 Store 的持久化契约，
# 不钉推进行为。断言一律落在结构化字段与稳定原因码上（开发流程 §4.2）。

EXPLORATION_DOMAIN = "controlled_sql_exploration"
EXPLORATION_ARTIFACT_TYPE = "exploration_result"
# 计划 Task 5 Step 1 的固定九节点链。顺序就是阶段二图上的推进顺序；注册表只回答
# 「这个领域能不能往运行记录里写这个节点」，所以两处都钉：集合逐项相等 + 逐个可写。
EXPLORATION_NODES = (
    "select_schema", "authorize_scope", "assess_readiness", "compile_query",
    "validate_ast", "estimate_cost", "execute_readonly", "persist_artifact", "finalize")
# 计划 Task 5 新增的四个终止原因：四个都发生在 SQL 执行之前或预算线上，与既有
# 「缺参数 / 缺覆盖 / 缺能力」不同类，不能拿现成的码含糊带过。
EXPLORATION_TERMINATION_REASONS = frozenset({
    "fixed_tool_available", "schema_ambiguous", "sql_policy_rejected",
    "query_cost_exceeded"})
# 009/014 已登记进库的值：020 重建 CHECK 时一个都不许掉（重建不是收缩）。
BASELINE_DOMAINS = frozenset({"business_query", "commerce_performance",
                             "listing_price_audit", "inventory_watch"})
BASELINE_ARTIFACT_TYPES = frozenset({
    "metric_result", "comparison_table", "trend_series", "chart_spec", "price_audit",
    "inventory_alerts"})
BASELINE_TERMINATION_REASONS = frozenset({
    "succeeded", "missing_parameters", "invalid_parameters", "forbidden",
    "coverage_incomplete", "data_as_of_unknown", "source_quality_failed",
    "source_not_onboarded", "revenue_not_attributed", "result_too_large",
    "comparison_coverage_incomplete", "deadline_exceeded", "query_timeout",
    "persistence_failed", "contract_violation", "upstream_unavailable",
    "transient_source_failure", "recovery_exhausted", "capability_unavailable",
    "coverage_time_basis_unverified"})
# 计划 Task 5 Step 4 的载荷字段全集：九个键，全部必填，多一个少一个都判红。
EXPLORATION_PAYLOAD_KEYS = frozenset({
    "template_version", "catalog_version", "statement_fingerprint", "columns", "rows",
    "basis", "coverage", "diagnostics", "limitations"})
# 阶段二才交付的模块与入口：本阶段它们必须还不存在。
TASK_FIVE_MODULES = ("graph", "tool")
TASK_FIVE_ENTRY_POINTS = ("execute_exploration_tool", "explore_business_data")
# 载荷被拒的原因与两个 Store 同一份：只有一个码，不许现场发明新说法。
PAYLOAD_REJECTION = "unsafe_persistence_payload"
COST_METRIC = "metric-cost-total"   # 结果里唯一的金额列
# 文本列用例：`line_kind` 是目录里已登记的维度列，也是唯一能装自由文本的那一格。
TEXT_COLUMN = LINE_KIND_COST


def exploration_payload(**overrides):
    """一份合法的 `exploration_result` 载荷：与 Task 4 投影的产出同形状。"""
    values = {
        "template_version": "exploration-sql/2026-09-14.1",
        "catalog_version": "semantic/2026-09-14.1",
        "statement_fingerprint": SHA256_HEX,
        "columns": [
            {"ref": DAY_COST, "data_type": "date"},
            {"ref": SHOP_REF_COLUMN, "data_type": "ref"},
            {"ref": COST_METRIC, "data_type": "decimal"},
        ],
        "rows": [
            {DAY_COST: "2026-09-01", SHOP_REF_COLUMN: "ent-shop-one",
             COST_METRIC: "12.30"},
            {DAY_COST: "2026-09-02", SHOP_REF_COLUMN: "ent-shop-two",
             COST_METRIC: "0.00"},
        ],
        "basis": [{"metric": COST_METRIC, "basis": "cost/2026-09-12.1"}],
        "coverage": {"status": "complete", "start": "2026-09-01", "end": "2026-09-08",
                     "gaps": []},
        "diagnostics": [{"code": "erp_document_coverage", "documents": 2}],
        "limitations": ["来源质量未核验（尚无对账记录）"],
    }
    values.update(overrides)
    return values


def text_column_payload(**overrides):
    """只有一个文本列的结果：给「自由文本里能不能藏 SQL」那组用例当底座。"""
    values = {"columns": [{"ref": TEXT_COLUMN, "data_type": "text"}],
              "rows": [{TEXT_COLUMN: "sale"}]}
    values.update(overrides)
    return exploration_payload(**values)


def validate_exploration(payload, *, as_model: bool = False):
    from bi_agent.runtime.models import validate_artifact_payload, validate_model_payload

    validate = validate_model_payload if as_model else validate_artifact_payload
    return validate(payload, EXPLORATION_ARTIFACT_TYPE)


def rejection_for(payload) -> str:
    """要求运行层拒掉这份载荷并交出稳定原因码；接下了就判红。"""
    try:
        validate_exploration(payload)
    except ValueError as exc:
        assert type(exc) is ValueError, f"不是裸 ValueError：{type(exc).__name__}"
        return str(exc)
    raise AssertionError("运行层接下了这份探索载荷")


def payload_rejection(**overrides) -> str:
    return rejection_for(exploration_payload(**overrides))


def without(payload, key):
    return {name: value for name, value in payload.items() if name != key}


def row_with(**cells):
    """改第一行的某几格：其余格保持合法，只差被测的那一个角度。"""
    row = dict(exploration_payload()["rows"][0])
    row.update(cells)
    return row


class ExplorationDomainContractTests(unittest.TestCase):
    """领域注册表：九个节点、一种 Artifact、四个原因码，一个都不能多。"""

    def test_registry_declares_exactly_the_nine_fixed_nodes(self):
        from typing import get_args

        from bi_agent.runtime.domain_registry import spec_for
        from bi_agent.runtime.models import PersistenceNode

        spec = spec_for(EXPLORATION_DOMAIN)
        self.assertEqual(spec.name, EXPLORATION_DOMAIN)
        self.assertEqual(spec.nodes, frozenset(EXPLORATION_NODES))
        self.assertEqual(len(set(EXPLORATION_NODES)), 9, "固定链就是九格")
        # 领域白名单必须是运行层全局节点表的子集：两份名单不能各写一半。
        self.assertTrue(spec.nodes <= frozenset(get_args(PersistenceNode)))

    def test_registry_declares_only_the_exploration_result_artifact(self):
        from bi_agent.runtime.domain_registry import (
            ARTIFACT_TYPES, allows_artifact_type, domains, spec_for)

        self.assertIn(EXPLORATION_ARTIFACT_TYPE, ARTIFACT_TYPES)
        self.assertEqual(spec_for(EXPLORATION_DOMAIN).artifact_types,
                         frozenset({EXPLORATION_ARTIFACT_TYPE}))
        self.assertTrue(allows_artifact_type(EXPLORATION_DOMAIN, EXPLORATION_ARTIFACT_TYPE))
        # 探索领域不产数据集，也不产别的领域的类型。
        for other in sorted(BASELINE_ARTIFACT_TYPES):
            self.assertFalse(allows_artifact_type(EXPLORATION_DOMAIN, other), other)
        # 既有领域也不许借道发探索结果。
        for domain in sorted(BASELINE_DOMAINS):
            self.assertFalse(allows_artifact_type(domain, EXPLORATION_ARTIFACT_TYPE), domain)
        self.assertEqual(domains(), tuple(sorted(BASELINE_DOMAINS | {EXPLORATION_DOMAIN})),
                         "登记新领域不得改掉既有清单")

    def test_exploration_nodes_are_writable_and_unlisted_nodes_are_not(self):
        from bi_agent.runtime.domain_registry import allows_node
        from bi_agent.runtime.models import validate_persisted_state

        for node in EXPLORATION_NODES:
            with self.subTest(node=node):
                self.assertTrue(allows_node(EXPLORATION_DOMAIN, node))
                self.assertEqual(validate_persisted_state({"node": node}), {"node": node})
        # 图上没有的格子：既不在本领域白名单，也不在全局节点表里。
        for node in ("execute_arbitrary_sql", "run_sql", "record_diagnostic"):
            self.assertFalse(allows_node(EXPLORATION_DOMAIN, node), node)
            with self.assertRaises(ValueError):
                validate_persisted_state({"node": node})
        # 节点可以全局合法，但不能借给别的领域：固定指标查询没有编译格。
        self.assertTrue(allows_node("business_query", "execute_fixed_query"))
        self.assertFalse(allows_node("business_query", "compile_query"))
        self.assertFalse(allows_node(EXPLORATION_DOMAIN, "execute_fixed_query"))

    def test_four_new_termination_reasons_join_the_existing_code_table(self):
        from bi_agent.runtime.artifacts import TERMINATION_REASONS
        from bi_agent.runtime.models import RunCompletion, RunStatus

        self.assertTrue(EXPLORATION_TERMINATION_REASONS <= TERMINATION_REASONS)
        # 追加不是替换：014 之前那份码表一项都不许少。
        self.assertTrue(BASELINE_TERMINATION_REASONS <= TERMINATION_REASONS)
        self.assertEqual(TERMINATION_REASONS - BASELINE_TERMINATION_REASONS,
                         EXPLORATION_TERMINATION_REASONS)
        for reason in sorted(EXPLORATION_TERMINATION_REASONS):
            with self.subTest(reason=reason):
                completion = RunCompletion(expected_revision=1, node="finalize",
                                           status=RunStatus.FAILED, state={},
                                           termination_reason=reason)
                self.assertEqual(completion.termination_reason, reason)
        with self.assertRaises(ValueError):
            RunCompletion(expected_revision=1, node="finalize", status=RunStatus.FAILED,
                          state={}, termination_reason="query_was_too_slow")

    def test_exploration_result_is_a_declared_artifact_type_everywhere(self):
        from bi_agent.runtime.artifacts import ArtifactEnvelope
        from bi_agent.runtime.models import ArtifactRef, NewArtifact

        ArtifactEnvelope(domain=EXPLORATION_DOMAIN, artifact_type=EXPLORATION_ARTIFACT_TYPE,
                         payload=exploration_payload())
        with self.assertRaises(ValidationError):
            ArtifactEnvelope(domain="business_query",
                             artifact_type=EXPLORATION_ARTIFACT_TYPE,
                             payload=exploration_payload())
        self.assertEqual(ArtifactRef(id=uuid4(), type=EXPLORATION_ARTIFACT_TYPE).type,
                         EXPLORATION_ARTIFACT_TYPE)
        # 载荷按类型判到探索那一支：`NewArtifact` 在建对象时已经跑过同一份校验。
        artifact = NewArtifact(artifact_type=EXPLORATION_ARTIFACT_TYPE,
                               payload=exploration_payload())
        self.assertEqual(set(artifact.payload), EXPLORATION_PAYLOAD_KEYS)
        self.assertIsNone(artifact.dataset_ref)
        self.assertIsNone(artifact.chart_version)

    def test_exploration_result_is_not_a_chart_dataset(self):
        """探索结果不是数据集：图表不得引用它，它也不得带数据集配对字段。"""
        from bi_agent.runtime.domain_registry import DATASET_ARTIFACT_TYPES
        from bi_agent.runtime.models import NewArtifact

        self.assertNotIn(EXPLORATION_ARTIFACT_TYPE, DATASET_ARTIFACT_TYPES)
        with self.assertRaises(ValidationError):
            NewArtifact(artifact_type=EXPLORATION_ARTIFACT_TYPE,
                        payload=exploration_payload(), dataset_ref=uuid4(),
                        chart_version=1)




class ExplorationArtifactPayloadTests(unittest.TestCase):
    """`exploration_result` 的严格载荷契约（计划 Task 5 Step 4）。"""

    def test_the_nine_fields_are_all_required_and_nothing_else_is_accepted(self):
        self.assertEqual(set(validate_exploration(exploration_payload())),
                         set(EXPLORATION_PAYLOAD_KEYS))
        for key in sorted(EXPLORATION_PAYLOAD_KEYS):
            with self.subTest(missing=key):
                self.assertEqual(rejection_for(without(exploration_payload(), key)),
                                 PAYLOAD_REJECTION)
        for extra, value in (("sql_text", "SELECT 1"),
                             ("parameters", {"limit": 100}),
                             ("question", "按店铺看成本"),
                             ("selected_refs", [COST_METRIC]),
                             ("shop_ids", ["S1"]),
                             ("entities", [{"ref": "ent-shop-one"}])):
            with self.subTest(extra=extra):
                self.assertEqual(rejection_for(exploration_payload(**{extra: value})),
                                 PAYLOAD_REJECTION)

    def test_versions_and_fingerprint_keep_the_exploration_contract(self):
        for bad_version in ("", " exploration-sql/2026-09-14.1", "two words", 1):
            with self.subTest(template_version=repr(bad_version)[:12]):
                self.assertEqual(payload_rejection(template_version=bad_version),
                                 PAYLOAD_REJECTION)
        for bad_fingerprint in ("a" * 63, "A" * 64, "zz" * 32, None):
            with self.subTest(fingerprint=str(bad_fingerprint)[:8]):
                self.assertEqual(payload_rejection(statement_fingerprint=bad_fingerprint),
                                 PAYLOAD_REJECTION)

    def test_rows_must_carry_exactly_the_declared_columns(self):
        for label, bad_rows in (("不是列表", "2026-09-01"),
                               ("行不是对象", [DAY_COST]),
                               ("少一列", [without(row_with(), DAY_COST)]),
                               ("多一列", [row_with(**{PRODUCT_COST: "P1"})]),
                               ("用 SQL 列名", [{"day": "2026-09-01",
                                                SHOP_REF_COLUMN: "ent-shop-one",
                                                "cost_total": "12.30"}])):
            with self.subTest(case=label):
                self.assertEqual(payload_rejection(rows=bad_rows), PAYLOAD_REJECTION)

    def test_row_count_uses_the_same_budget_as_the_request(self):
        from bi_agent.exploration.models import MAX_ROWS

        row = row_with()
        self.assertEqual(len(validate_exploration(
            exploration_payload(rows=[dict(row) for _ in range(MAX_ROWS)]))["rows"]),
            MAX_ROWS)
        self.assertEqual(payload_rejection(
            rows=[dict(row) for _ in range(MAX_ROWS + 1)]), PAYLOAD_REJECTION)

    def test_columns_must_be_unique_refs_with_a_declared_type(self):
        for label, bad_columns in (
                ("没有列", []),
                ("不是列表", DAY_COST),
                ("重复列", [{"ref": DAY_COST, "data_type": "date"},
                          {"ref": DAY_COST, "data_type": "date"},
                          {"ref": SHOP_REF_COLUMN, "data_type": "ref"},
                          {"ref": COST_METRIC, "data_type": "decimal"}]),
                ("缺类型", [{"ref": DAY_COST}]),
                ("多键", [{"ref": DAY_COST, "data_type": "date", "column": "day"}]),
                ("未登记类型", [{"ref": DAY_COST, "data_type": "money"}]),
                ("SQL 列名", [{"ref": "cost_total", "data_type": "decimal"}]),
                ("表达式冒充 ref", [{"ref": "SUM(cost_total)", "data_type": "decimal"}]),
                ("店铺列换了类型", [{"ref": SHOP_REF_COLUMN, "data_type": "text"}]),
                ("普通列冒充 ref", [{"ref": DAY_COST, "data_type": "ref"}])):
            with self.subTest(case=label):
                self.assertEqual(payload_rejection(columns=bad_columns), PAYLOAD_REJECTION)

    def test_values_must_match_the_declared_column_type(self):
        for column, bad in ((DAY_COST, "2026/09/01"), (DAY_COST, "09-01-2026"),
                            (SHOP_REF_COLUMN, "S1"), (SHOP_REF_COLUMN, "shop 1"),
                            (COST_METRIC, 12.30), (COST_METRIC, "12,30"),
                            (COST_METRIC, True)):
            with self.subTest(column=column, value=repr(bad)[:16]):
                self.assertEqual(payload_rejection(rows=[row_with(**{column: bad})]),
                                 PAYLOAD_REJECTION)

    def test_integer_boolean_and_moment_columns_honour_their_declaration(self):
        for columns, row in (
                ([{"ref": TEXT_COLUMN, "data_type": "text"},
                  {"ref": COST_METRIC, "data_type": "integer"}],
                 {TEXT_COLUMN: "sale", COST_METRIC: True}),
                ([{"ref": COST_METRIC, "data_type": "boolean"},
                  {"ref": DAY_COST, "data_type": "date"}],
                 {COST_METRIC: 1, DAY_COST: "2026-09-01"}),
                ([{"ref": STATUS_ERP, "data_type": "datetime"}],
                 {STATUS_ERP: "2026-09-01 12:00:00"}),
                ([{"ref": STATUS_ERP, "data_type": "datetime"}],
                 {STATUS_ERP: "2026-09-01T12:00:00"})):
            with self.subTest(row=repr(row)[:44]):
                self.assertEqual(payload_rejection(columns=columns, rows=[row]),
                                 PAYLOAD_REJECTION)
        # 反例：按声明发就得接受（差一个角度而不是全拒）
        self.assertEqual(validate_exploration(text_column_payload(
            columns=[{"ref": TEXT_COLUMN, "data_type": "text"},
                     {"ref": COST_METRIC, "data_type": "integer"}],
            rows=[{TEXT_COLUMN: "sale", COST_METRIC: 3}]))["rows"],
            [{TEXT_COLUMN: "sale", COST_METRIC: 3}])
        self.assertEqual(validate_exploration(text_column_payload(
            columns=[{"ref": STATUS_ERP, "data_type": "datetime"}],
            rows=[{STATUS_ERP: "2026-09-01T12:00:00+08:00"}]))["rows"],
            [{STATUS_ERP: "2026-09-01T12:00:00+08:00"}])

    def test_nulls_are_allowed_only_where_the_projection_emits_them(self):
        """空值不是 0：允许出现在除 `shop-ref` 以外的列上。"""
        self.assertEqual(validate_exploration(exploration_payload(
            rows=[row_with(**{DAY_COST: None, COST_METRIC: None})]))["rows"],
            [row_with(**{DAY_COST: None, COST_METRIC: None})])
        self.assertEqual(payload_rejection(rows=[row_with(**{SHOP_REF_COLUMN: None})]),
                         PAYLOAD_REJECTION)

    def test_no_sql_shape_reaches_the_public_payload(self):
        """载荷里任何一处字符串都得过 SQL 形状检查，不只检查键名。"""
        for value in ("SELECT sum(cost_total) FROM reporting.v_product_cost_daily",
                      "fact.\"shop_id\" = ANY(%(allowed_shop_ids)s)",
                      "DROP TABLE bi.orders",
                      "1; DELETE FROM bi.orders",
                      "cost -- 注释"):
            with self.subTest(value=value[:24]):
                self.assertEqual(rejection_for(text_column_payload(
                    rows=[{TEXT_COLUMN: value}])), PAYLOAD_REJECTION)
        for field, value in (
                ("limitations", ["成本口径见 SELECT 语句"]),
                ("basis", [{"metric": COST_METRIC,
                           "basis": "cost/2026-09-12.1; DROP TABLE bi.orders"}]),
                ("diagnostics", [{"code": "erp_document_coverage",
                                 "documents": "DELETE FROM bi.orders"}])):
            with self.subTest(field=field):
                self.assertEqual(payload_rejection(**{field: value}), PAYLOAD_REJECTION)

    def test_coverage_basis_and_diagnostics_reuse_the_registered_vocabularies(self):
        """不另起一套平行契约：覆盖走 `Coverage`，诊断走已登记码表。"""
        self.assertEqual(payload_rejection(coverage={"status": "complete", "gaps": []}),
                         PAYLOAD_REJECTION)                       # 缺 start/end
        self.assertEqual(payload_rejection(
            coverage={"status": "whole", "start": "2026-09-01", "end": "2026-09-08",
                      "gaps": []}), PAYLOAD_REJECTION)
        self.assertEqual(payload_rejection(
            basis=[{"metric": "cost_total", "basis": "cost/2026-09-12.1"}]),
            PAYLOAD_REJECTION)                                    # 指标必须是稳定 ref
        self.assertEqual(payload_rejection(basis=[{"metric": COST_METRIC}]),
                         PAYLOAD_REJECTION)                       # 缺口径名
        self.assertEqual(payload_rejection(
            basis=[{"metric": COST_METRIC, "basis": "cost/2026-09-12.1",
                   "shop_id": "S1"}]), PAYLOAD_REJECTION)         # 未登记键
        self.assertEqual(payload_rejection(
            diagnostics=[{"code": "cost_coverage", "rows": 1}]), PAYLOAD_REJECTION)
        self.assertEqual(payload_rejection(
            diagnostics=[{"code": "erp_document_coverage", "rows": 1}]),
            PAYLOAD_REJECTION)                                    # 未登记字段
        self.assertEqual(payload_rejection(
            diagnostics=[{"code": "erp_document_coverage", "documents": 1.5}]),
            PAYLOAD_REJECTION)                                    # 浮点冒充计数
        self.assertEqual(payload_rejection(diagnostics=["erp_document_coverage"]),
                         PAYLOAD_REJECTION)
        self.assertEqual(payload_rejection(limitations=["成本口径没核验"]),
                         PAYLOAD_REJECTION)                       # 未登记披露文本
        self.assertEqual(payload_rejection(basis=[]), PAYLOAD_REJECTION)
        # 反例：一次干净的结果本来就没有披露与诊断，清空这两项必须照旧可发。
        clean = validate_exploration(exploration_payload(limitations=[], diagnostics=[]))
        self.assertEqual(clean["limitations"], [])
        self.assertEqual(clean["diagnostics"], [])

    def test_model_payload_and_artifact_payload_are_the_same_shape(self):
        """探索结果里没有展示名，所以给模型与公开发的是同一份形状。"""
        payload = exploration_payload()
        self.assertEqual(validate_exploration(payload, as_model=True),
                         validate_exploration(payload))
        # 同一护栏对两支都生效：少任何必填键，两边都不收。
        for as_model in (False, True):
            with self.subTest(as_model=as_model):
                try:
                    validate_exploration(without(payload, "statement_fingerprint"),
                                         as_model=as_model)
                except ValueError as exc:
                    self.assertEqual(str(exc), PAYLOAD_REJECTION)
                else:
                    self.fail("运行层接下了缺字段的探索载荷")

    def test_the_runtime_validator_and_the_task_one_contract_agree(self):
        """计划 Task 1 的 `ExplorationResult` 与运行层载荷校验必须判同一件事。"""
        from bi_agent.exploration.models import ExplorationResult

        payload = exploration_payload()
        result = ExplorationResult(**payload)
        self.assertEqual(validate_exploration(result.model_dump(mode="json")),
                         validate_exploration(payload))
        # 运行层是单向收紧：Task 1 收下的形状仍要过已登记词表。
        ExplorationResult(**dict(payload, diagnostics=[{"code": "cost_coverage"}]))
        self.assertEqual(payload_rejection(diagnostics=[{"code": "cost_coverage"}]),
                         PAYLOAD_REJECTION)
        with self.assertRaises(ValueError):
            ExplorationResult(**dict(payload, sql_text=result.statement_fingerprint))


class ExplorationStoreContractTests(unittest.TestCase):
    """两个 Store 复用同一份载荷校验，并且只有一处私有诊断通道（不碰库的那一半）。"""

    def _record(self, **overrides):
        from bi_agent.catalog import ref_for_key
        from bi_agent.runtime.models import NewQueryRun

        values = dict(
            chat_id=uuid4(), user_message_id=uuid4(), subject_id="u1",
            tool_call_id="call_1", domain=EXPLORATION_DOMAIN, attempt_no=1,
            normalized_request={"shop_refs": [ref_for_key("shop", "S1")]},
            state={"node": "select_schema"})
        values.update(overrides)
        return NewQueryRun(**values)

    def _memory_store(self):
        from bi_agent.runtime.memory import MemoryQueryRunStore

        return MemoryQueryRunStore(forbidden_values={"S1", "ERP-P-9"})

    def _memory_store_run(self, store) -> UUID:
        """内存 Store 要先有运行行：`save_artifact` / `record_diagnostic` 都以它为前提。"""
        return store.create_run(self._record())

    def _postgres_store(self):
        from bi_agent.runtime.repository import PostgresQueryRunStore

        # 载荷被拒必须发生在碰库之前：连接替身是 None，越界就是 AttributeError。
        return PostgresQueryRunStore(None, forbidden_values={"S1", "ERP-P-9"})

    def _construct_artifact(self, payload):
        """跳过构造期校验：这里要证明的是 Store 自己会拒，不是 Pydantic 先拦。"""
        from bi_agent.runtime.models import NewArtifact

        return NewArtifact.model_construct(artifact_type=EXPLORATION_ARTIFACT_TYPE,
                                           payload=payload, data_as_of=None,
                                           coverage=None, dataset_ref=None,
                                           chart_version=None)

    def test_both_stores_refuse_a_node_the_domain_does_not_own(self):
        """节点名全局合法不等于本领域可写：两个 Store 都在碰库之前拒。"""
        for store in (self._memory_store(), self._postgres_store()):
            with self.subTest(store=type(store).__name__):
                with self.assertRaises(ValueError) as caught:
                    store.create_run(self._record(state={"node": "execute_fixed_query"}))
                self.assertEqual(str(caught.exception), PAYLOAD_REJECTION)

    def test_both_stores_reject_the_same_exploration_payloads(self):
        bad = (exploration_payload() | {"sql_text": "SELECT 1"},
               without(exploration_payload(), "statement_fingerprint"),
               exploration_payload(rows=[{"day": "2026-09-01"}]),
               exploration_payload(limitations=["成本口径没核验"]),
               exploration_payload(diagnostics=[{"code": "cost_coverage"}]))
        for index, payload in enumerate(bad):
            memory = self._memory_store()
            for label, store, run_id in (("memory", memory, self._memory_store_run(memory)),
                                         ("postgres", self._postgres_store(), uuid4())):
                with self.subTest(case=index, store=label):
                    with self.assertRaises(ValueError) as caught:
                        store.save_artifact(run_id, self._construct_artifact(payload))
                    self.assertEqual(str(caught.exception), PAYLOAD_REJECTION)

    def test_both_stores_reject_a_real_shop_id_in_the_exploration_payload(self):
        """真店号进不了公开载荷：形状再合法也没用，两个 Store 同一条规则。"""
        payload = text_column_payload(rows=[{TEXT_COLUMN: "S1"}])
        validate_exploration(payload)          # 形状这一道是过的
        memory = self._memory_store()
        for label, store, run_id in (("memory", memory, self._memory_store_run(memory)),
                                     ("postgres", self._postgres_store(), uuid4())):
            with self.subTest(store=label):
                with self.assertRaises(ValueError) as caught:
                    store.save_artifact(run_id, self._construct_artifact(payload))
                self.assertEqual(str(caught.exception), PAYLOAD_REJECTION)

    def test_memory_store_keeps_diagnostics_out_of_every_public_record(self):
        from bi_agent.runtime.models import NewArtifact

        store = self._memory_store()
        run_id = self._memory_store_run(store)
        diagnostic_id = store.record_diagnostic(
            run_id, template_id="exploration_sql",
            sql_text='SELECT fact."day" FROM "reporting"."v_product_cost_daily" AS fact'
                     ' WHERE fact."shop_id" = ANY(%(allowed_shop_ids)s)',
            parameters={"allowed_shop_ids": ["S1"], "limit": 100})
        self.assertIsInstance(diagnostic_id, UUID)
        self.assertEqual(store.diagnostic_count, 1)
        # 私有通道按设计带着 SQL 与真店号：这里不许被公开载荷那套护栏误伤。
        self.assertEqual(store.diagnostics[run_id][0]["parameters"]["allowed_shop_ids"],
                         ["S1"])
        for published in (store.runs, store.events, store.artifacts):
            self.assertNotIn("SELECT", repr(published))
            self.assertNotIn("allowed_shop_ids", repr(published))
            self.assertNotIn("'S1'", repr(published))
        ref = store.save_artifact(run_id, NewArtifact(
            artifact_type=EXPLORATION_ARTIFACT_TYPE, payload=exploration_payload()))
        self.assertEqual(ref.type, EXPLORATION_ARTIFACT_TYPE)
        self.assertEqual([item["artifact_type"] for item in store.artifacts.values()],
                         [EXPLORATION_ARTIFACT_TYPE])
        self.assertNotIn("SELECT", repr(store.artifacts))
        # 一次成功的探索：一份公开结果 + 一份私有证据，不多不少。
        self.assertEqual((len(store.artifacts), store.diagnostic_count), (1, 1))

    def test_memory_record_diagnostic_requires_an_existing_run(self):
        from bi_agent.runtime.models import RunNotFound

        store = self._memory_store()
        with self.assertRaises(RunNotFound):
            store.record_diagnostic(uuid4(), template_id="exploration_sql",
                                   sql_text="SELECT 1", parameters={})


# --- Task 5（阶段二）固定图与 Tool 适配器 ---------------------------------------
#
# 反恒真约定（开发流程 §4.1）：每个拒答用例都同时钉「原因码 + 走到哪一格 + 有没有碰库
# + 发了几份公开结果」。只断「招了」不算护栏：`EvidenceConn` 对第三条语句直接报错，
# 所以「提前终止」是可证的而不是推测的。

GRAPH_NOW = POLICY_NOW
GRAPH_START, GRAPH_END = date(2026, 9, 1), date(2026, 9, 8)
# 探索夹具只能用**已登记能力标签同名**的指标（014 的口径）：`metric-cost-total` 的列
# `cost_total` 没有任何已登记的来源依赖表，探索层必须当场拒它（见
# `ExplorationReadinessRegressionTests`）。
GRAPH_QUESTION = "按店铺和日期看支付金额与订单数"
READY_METRICS = ("metric-paid-amount",)
READY_GROUPS = (DAY_SHOP_DAILY, SHOP_SHOP_DAILY)
PAID_METRIC = "metric-paid-amount"
# 未投影的原始行：带真店号，用来证明投影与公开载荷那一跑是必需的而不是装饰。
STUB_ROWS = [(GRAPH_START, "S1", Decimal("1000.00")), (GRAPH_START, "S2", Decimal("0.00"))]
STUB_COLUMNS = [DAY_SHOP_DAILY, SHOP_ID_COLUMN, PAID_METRIC]
# `normalized_request.shop_refs` 走全仓那条实体引用规则（`ent-` + 8 位），不是投影夹具里
# 那种可读假名：这里必须用同源生成的真引用，否则运行契约先判红。
from bi_agent.catalog import ref_for_key as _ref_for_key

GRAPH_SHOP_REFS = {shop: _ref_for_key("shop", shop) for shop in sorted(POLICY_SHOP_IDS)}
# readiness 只该读这一条事实（店 × 平台 × 能力标签）；覆盖/质量/截止由
# `data_quality.assess_query_coverage` 判，用例把它的结论贴进来当输入。


class RecordingStore:
    """只记录写序列的 Store 代身：节点推进、诊断与 Artifact 的先后就是契约。"""

    def __init__(self, *, fail_diagnostic=False, fail_artifact=False,
                 fail_transition_after=None, fail_finish=False):
        self.log: list[tuple] = []
        self.artifacts: dict = {}
        self.diagnostics: dict = {}
        self.fail_diagnostic = fail_diagnostic
        self.fail_artifact = fail_artifact
        # 第 N 次 transition 写完就报错：模拟"图跑到一半死了"。
        self.fail_transition_after = fail_transition_after
        self.fail_finish = fail_finish
        self.transitions = 0
        self.finishes = 0
        self._run_id = uuid4()

    def create_run(self, record):
        self.log.append(("create_run", record.domain, record.state.get("node")))
        return self._run_id

    def transition(self, run_id, transition):
        self.transitions += 1
        self.log.append(("transition", transition.node, transition.status.value))
        if self.fail_transition_after == self.transitions:
            raise RuntimeError("connection lost mid-chain")

    def record_diagnostic(self, run_id, *, template_id, sql_text, parameters):
        self.log.append(("diagnostic", template_id, sql_text, parameters))
        if self.fail_diagnostic:
            raise RuntimeError("diagnostic write failed")
        self.diagnostics.setdefault(run_id, []).append(sql_text)
        return uuid4()

    def save_artifact(self, run_id, artifact):
        self.log.append(("artifact", artifact.artifact_type, artifact.payload,
                         artifact.coverage, artifact.data_as_of))
        if self.fail_artifact:
            raise RuntimeError("artifact write failed")
        from bi_agent.runtime.models import ArtifactRef

        ref = ArtifactRef(id=uuid4(), type=artifact.artifact_type)
        self.artifacts[ref.id] = artifact.payload
        return ref

    def finish(self, run_id, completion):
        self.finishes += 1
        self.log.append(("finish", completion.node, completion.status.value,
                         completion.termination_reason, completion.error_code,
                         completion.state, completion.payload))
        if self.fail_finish:
            raise RuntimeError("finish write failed")

    @property
    def kinds(self) -> list[str]:
        return [entry[0] for entry in self.log]

    @property
    def nodes(self) -> list[str]:
        return [entry[1] for entry in self.log if entry[0] == "transition"]

    @property
    def diagnostic_count(self) -> int:
        return sum(len(items) for items in self.diagnostics.values())

    @property
    def finish_record(self) -> tuple:
        return next(entry for entry in reversed(self.log) if entry[0] == "finish")


class Rows:
    """psycopg 结果对象的最小代身。"""

    def __init__(self, rows):
        self._rows = list(rows)

    def fetchall(self):
        return list(self._rows)


class EvidenceConn:
    """只回答 readiness 那一条店铺事实的假连接。

    覆盖、质量、时间口径与共同截止由 `assess_query_coverage` 判（用例贴它的结论），所以
    这里出现**任何**别的语句（覆盖引擎的 SQL、EXPLAIN、真执行）都是越界：这让"提前终止"
    是可证的而不是推测的。
    """

    def __init__(self, *, shops=None, broken=False):
        default = [("S1", "fxg", ["paid_amount", "paid_orders"]),
                   ("S2", "fxg", ["paid_amount", "paid_orders"])]
        self.shops = default if shops is None else shops
        self.broken = broken
        self.statements: list[str] = []

    def execute(self, sql, parameters=None):
        self.statements.append(sql)
        if self.broken:
            raise RuntimeError("database unavailable")
        if "reporting.v_shops" in sql:
            return Rows(self.shops)
        raise AssertionError(f"exploration issued a statement it must not: {sql[:40]}")


def assessment(*, status="complete", quality="passed", data_as_of=GRAPH_NOW,
               missing=(), blocking=(), disclosure=(), unconfigured=(),
               batches=("batch-orders-1",), raise_error=None):
    """脚本化的就绪结论：readiness 只能按它的字段判，不在图里重拄一套规则。"""
    from bi_agent.data_quality import CoverageAssessment

    if raise_error is not None:
        return raise_error          # 直接交异常实例：`graph_patches` 用 side_effect 贴
    return CoverageAssessment(
        status=status,
        requested_window=(GRAPH_START.isoformat(), GRAPH_END.isoformat()),
        covered_windows=((GRAPH_START.isoformat(), GRAPH_END.isoformat()),) * int(
            status == "complete"),
        missing_windows=tuple(missing),
        data_as_of=data_as_of,
        quality_status=quality,
        source_batches=tuple(batches),
        gaps=(),
        suggested_window=None,
        source_unconfigured=tuple(unconfigured),
        time_basis_blocking=tuple(blocking),
        time_basis_disclosure=tuple(disclosure))


def graph_context(store, conn, **overrides):
    values = {"subject_id": "subject-one", "allowed_shop_ids": POLICY_SHOP_IDS,
              "shop_refs": dict(GRAPH_SHOP_REFS), "conn": conn, "store": store,
              "chat_id": UUID(int=11), "user_message_id": UUID(int=12),
              "root_request_id": UUID(int=13), "now": GRAPH_NOW,
              "deadline": time.monotonic() + 30.0, "attempt_no": 1}
    values.update(overrides)
    return policy_context(**values)


def graph_versions():
    from bi_agent.runtime.artifacts import GRAPH_VERSION
    from bi_agent.runtime.versions import VersionSet
    from bi_agent.semantic_catalog.registry import CATALOG
    from bi_agent.sources import METRIC_VERSION, POLICY_VERSION, SOURCE_REGISTRY_VERSION

    return VersionSet(schema_version="020", semantic_catalog_version=CATALOG.version,
                      data_catalog_version=0, metric_version=METRIC_VERSION,
                      policy_version=POLICY_VERSION,
                      source_registry_version=SOURCE_REGISTRY_VERSION,
                      graph_version=GRAPH_VERSION)


def ready_request(**overrides):
    values = {"start": GRAPH_START, "end": GRAPH_END}
    values.update(overrides)
    return request_for(READY_METRICS, list(READY_GROUPS), **values)


def stub_estimate(conn, plan, *, deadline):
    """贴估算值的新计划：与 Task 4 真库拿到的形状一致，但不进库。"""
    return plan.model_copy(update={"estimated_rows": 18,
                                   "estimated_total_cost": Decimal("647.04")})


def stub_execute(conn, plan, *, deadline):
    """返回**未投影**的原始行（带真店号）：投影仍由真 `project_result` 做。"""
    return list(STUB_COLUMNS), [tuple(row) for row in STUB_ROWS]


def graph_patches(selection=None, *, estimate=stub_estimate, execute=stub_execute,
                  ready=None):
    """图外面那几道会进库/查目录的口：检索贴 selection，覆盖结论贴脚本。

    `ready` 给 `CoverageAssessment` 就是"引擎这么判"，给异常实例就是"引擎自己跳了"。
    """
    selection = selection_for(READY_METRICS, groups=READY_GROUPS)         if selection is None else selection
    verdict = assessment() if ready is None else ready
    coverage_kwargs = ({"side_effect": verdict} if isinstance(verdict, Exception)
                       else {"return_value": verdict})
    return [
        mock.patch("bi_agent.exploration.graph.retrieve_schema_candidates",
                   return_value=selection),
        mock.patch("bi_agent.exploration.graph.assess_query_coverage",
                   **coverage_kwargs),
        mock.patch("bi_agent.exploration.graph.estimate_plan", side_effect=estimate),
        mock.patch("bi_agent.exploration.graph.execute_plan", side_effect=execute),
    ]


def run_graph(request=None, *, selection=None, store=None, conn=None, versions=None,
              question=None, patches=None, ready=None, **context_overrides):
    """跑图：默认贴好那几道口，所以本文件的图用例不需要数据库也能跑完九格。"""
    from contextlib import ExitStack

    from bi_agent.exploration.graph import run_exploration_graph

    request = ready_request() if request is None else request
    store = RecordingStore() if store is None else store
    conn = EvidenceConn() if conn is None else conn
    context = graph_context(store, conn, **context_overrides)
    with ExitStack() as stack:
        for patcher in (graph_patches(selection, ready=ready) if patches is None
                        else patches):
            stack.enter_context(patcher)
        execution = run_exploration_graph(
            question=GRAPH_QUESTION if question is None else question,
            request=request, context=context, versions=versions or graph_versions())
    return execution, store, context


def refusal_reason_of(store) -> str:
    return store.finish_record[3]


def refusal_state(store) -> dict:
    return store.finish_record[5]


class ExplorationGraphChainTests(unittest.TestCase):
    """九节点链、写顺序与「在碰库之前就能停」。"""

    def test_chain_visits_the_nine_nodes_and_publishes_one_of_each(self):
        execution, store, _context = run_graph()
        self.assertEqual(store.nodes, list(EXPLORATION_NODES[:-1]))
        self.assertEqual(store.finish_record[1], EXPLORATION_NODES[-1])
        self.assertEqual(execution.domain_result.status.value, "success",
                         execution.domain_result.error)
        self.assertEqual(store.kinds.count("diagnostic"), 1)
        self.assertEqual(store.kinds.count("artifact"), 1)
        # 先留证据再发结果：顺序反了就是「结果没有可追溯的语句」。
        self.assertLess(store.kinds.index("diagnostic"), store.kinds.index("artifact"))
        self.assertEqual(refusal_reason_of(store), "succeeded")
        self.assertIsNotNone(execution.plan.estimated_rows)
        self.assertIsNotNone(execution.plan.estimated_total_cost)

    def test_success_publishes_one_artifact_and_keeps_sql_private(self):
        execution, store, _context = run_graph()
        artifacts = execution.domain_result.artifacts
        self.assertEqual([item.ref.type for item in artifacts], [EXPLORATION_ARTIFACT_TYPE])
        payload = artifacts[0].public_payload
        self.assertEqual(set(payload), EXPLORATION_PAYLOAD_KEYS)
        self.assertEqual([row[SHOP_REF_COLUMN] for row in payload["rows"]],
                         [GRAPH_SHOP_REFS["S1"], GRAPH_SHOP_REFS["S2"]])
        self.assertEqual([row[PAID_METRIC] for row in payload["rows"]],
                         ["1000.00", "0.00"])
        self.assertEqual(payload["coverage"]["status"], "complete")
        self.assertEqual(payload["diagnostics"], [])
        self.assertEqual(execution.domain_result.model_payload, payload)
        sql = store.log[store.kinds.index("diagnostic")][2]
        self.assertIn("SELECT", sql)
        # SQL 与真店号只活在那一条私有记录里。
        for surface, text in (("artifact", repr(artifacts)),
                              ("model", repr(execution.domain_result.model_payload)),
                              ("run", repr(store.finish_record))):
            with self.subTest(surface=surface):
                for leak in ("SELECT", "ANY(", "'S1'", "statement_timeout"):
                    self.assertNotIn(leak, text, leak)

    def test_fixed_tool_question_is_refused_before_any_statement(self):
        """计划 Task 5 种子用例：固定 Tool 能表达的就不许花一钱数据库。"""
        # 不带分组：`analyze_product_performance` 能表达成本合计。
        execution, store, _context = run_graph(request_for("metric-cost-total"))
        self.assertEqual(execution.domain_result.status.value, "needs_input")
        self.assertEqual(execution.domain_result.error.code, "invalid_parameters")
        self.assertIsNone(execution.plan)
        self.assertEqual(refusal_reason_of(store), "fixed_tool_available")
        self.assertEqual(store.nodes, ["select_schema", "authorize_scope"])
        self.assertEqual(store.kinds.count("artifact"), 0)
        self.assertEqual(store.kinds.count("diagnostic"), 0)
        self.assertEqual(execution.domain_result.artifacts, [])

    def test_clarification_missing_concept_and_empty_candidates_stop_at_selection(self):
        # 三格都停在 select_schema，但归因不同：该澄清的要问用户，没注册概念的要说
        # "换问法"（库里没有第五个新码，所以终止原因同为 schema_ambiguous，靠终态区分）。
        cases = (("clarify", selection_for(READY_METRICS, groups=READY_GROUPS,
                                          requires_clarification=True),
                  "schema_ambiguous", "needs_input"),
                 ("missing", selection_for(READY_METRICS, groups=READY_GROUPS,
                                           missing_concepts=("profit_grain",)),
                  "schema_ambiguous", "missing_data"),
                 ("无候选", selection_for(READY_METRICS, groups=READY_GROUPS,
                                         view_refs=(), field_refs=()),
                  "schema_ambiguous", "needs_input"))
        for label, selection, reason, status in cases:
            with self.subTest(case=label):
                conn = EvidenceConn()
                execution, store, _context = run_graph(selection=selection, conn=conn)
                self.assertEqual(store.nodes, ["select_schema"])
                self.assertEqual(refusal_reason_of(store), reason)
                self.assertEqual(execution.domain_result.status.value, status)
                self.assertEqual(store.kinds.count("artifact"), 0)
                self.assertEqual(conn.statements, [], "选择没定下来就不该读证据")

    def test_readiness_failures_stop_before_the_database_gates(self):
        """能力 / 质量 / 覆盖 / 时间口径 / 截止各说各的，并且都走不到 EXPLAIN。

        P1-1 回归（red→green）：旧实现只看"这家店有没有任何能力标签"，所以一个
        拿不到 `paid_orders` 授权的店也能被 `paid_amount` 的探索顺手带出去；现在逐
        店逐指标走 `sources.unsupported_reason`，标签不对就该当场拒。同理，覆盖不再
        拿订单脊柱推定、`data_as_of` 不再取 max。
        """
        # 请求要两个指标，其中 `paid_orders` 没被授权：旧实现只看"有没有任何标签"，
        # 这一格会直接放行；新实现逐店逐指标问 `sources.unsupported_reason`。
        both = request_for([PAID_METRIC, "metric-paid-orders"],
                           [DAY_SHOP_DAILY, SHOP_SHOP_DAILY],
                           start=GRAPH_START, end=GRAPH_END)
        cases = (
            ("标签存在但不是这个指标", {"shops": [("S1", "fxg", ["paid_amount"]),
                                            ("S2", "fxg", ["paid_amount"])]},
             "capability_unavailable"),
            ("完全没标签", {"shops": [("S1", "fxg", []), ("S2", "fxg", ["paid_amount"])]},
             "capability_unavailable"),
            ("档案不在库里", {"shops": [("S1", "fxg", ["paid_amount"])]},
             "source_not_onboarded"),
            ("平台未登记来源", {"shops": [("S1", "nosuch", ["paid_amount", "paid_orders"]),
                                     ("S2", "nosuch", ["paid_amount", "paid_orders"])]},
             "source_not_onboarded"),
            ("库读不到", {"broken": True}, "source_not_onboarded"))
        for label, kwargs, reason in cases:
            with self.subTest(case=label):
                conn = EvidenceConn(**kwargs)
                selection = selection_for([PAID_METRIC, "metric-paid-orders"],
                                          groups=[DAY_SHOP_DAILY, SHOP_SHOP_DAILY])
                _execution, store, _context = run_graph(both, selection=selection,
                                                        conn=conn)
                self.assertEqual(refusal_reason_of(store), reason)
                self.assertEqual(store.nodes, ["select_schema", "authorize_scope",
                                               "assess_readiness"])
                self.assertEqual(store.kinds.count("artifact"), 0)
                self.assertEqual(store.kinds.count("diagnostic"), 0)
                # 只读那一条店铺事实：没有 EXPLAIN、没有真执行、也没有手写 multirange。
                self.assertLessEqual(len(conn.statements), 1)
                for sql in conn.statements:
                    self.assertIn("reporting.v_shops", sql)

    def test_one_metric_with_two_dependencies_publishes_both_basis_rows(self):
        """P1-1a 回归（red→green）：`cash_difference` 合法地同时要订单与退款发生两个来源。

        旧写法逐条 binding 各算一份签名再比"只许一份"，于是这个**每家店都一致**的指标被
        永久判成"口径不兼容"（termination `contract_violation`）。兼容性只能按整条
        (店 × 指标) 依赖表跨店比，与 `metrics._incompatible_metrics` 同一条规则。
        """
        cash = "metric-cash-difference"
        selection = selection_for([cash], groups=[DAY_SHOP_DAILY, SHOP_SHOP_DAILY])
        columns = [DAY_SHOP_DAILY, SHOP_ID_COLUMN, cash]
        rows = [(GRAPH_START, "S1", Decimal("700.00")),
                (GRAPH_START, "S2", Decimal("-30.00"))]

        def execute(conn, plan, *, deadline):
            return list(columns), [tuple(row) for row in rows]

        conn = EvidenceConn(shops=[("S1", "fxg", ["cash_difference"]),
                                   ("S2", "fxg", ["cash_difference"])])
        execution, store, _context = run_graph(
            request_for([cash], [DAY_SHOP_DAILY, SHOP_SHOP_DAILY],
                        start=GRAPH_START, end=GRAPH_END),
            selection=selection, conn=conn,
            patches=graph_patches(selection, execute=execute))
        self.assertEqual(execution.domain_result.status.value, "success",
                         execution.domain_result.error)
        payload = execution.domain_result.artifacts[0].public_payload
        self.assertEqual({(item["basis"], item["time_basis"]) for item in payload["basis"]},
                         {("platform_payment/v1", "pay_time"),
                          ("platform_refund_occurrence/v1",
                           "aftersale_completion_time")})
        self.assertEqual({item["metric"] for item in payload["basis"]}, {cash})
        self.assertEqual([row[cash] for row in payload["rows"]], ["700.00", "-30.00"])
        self.assertEqual(refusal_reason_of(store), "succeeded")
        self.assertEqual(store.kinds.count("artifact"), 1)
        self.assertEqual(store.kinds.count("diagnostic"), 1)

    def test_unknown_quality_must_publish_the_registered_disclosure(self):
        """P1-1b 回归：质量三态不能被掰成两态。`unknown` 可出数但必须披露。

        文本与限制码都是仓库里已登记的那一份（`_PUBLIC_LIMITATIONS` /
        `_LIMITATION_CODES`，与固定指标查询、运营图同句），不是为探索新造的口径。
        """
        text = "来源质量未核验（尚无对账记录）"
        for label, ready, expect_text, expect_code in (
                ("未核验", assessment(quality="unknown"), [text],
                 ["source_quality_unverified"]),
                ("已核验", assessment(quality="passed"), [], [])):
            with self.subTest(case=label):
                execution, store, _context = run_graph(ready=ready)
                self.assertEqual(execution.domain_result.status.value, "success",
                                 execution.domain_result.error)
                payload = execution.domain_result.artifacts[0].public_payload
                self.assertEqual(payload["limitations"], expect_text)
                self.assertEqual(execution.domain_result.model_payload["limitations"],
                                 expect_text)
                self.assertEqual(refusal_reason_of(store), "succeeded")
                self.assertEqual(refusal_state(store).get("limitations", []), expect_code)
                self.assertEqual(store.finish_record[6].get("limitation_codes", []),
                                 expect_code)

    def test_quality_failed_still_refuses_although_disclosure_exists(self):
        """反向护栏：`failed` 不得被新的披露通道救成"可出数"。"""
        _execution, store, _context = run_graph(ready=assessment(quality="failed"))
        self.assertEqual(refusal_reason_of(store), "source_quality_failed")
        self.assertEqual(store.kinds.count("artifact"), 0)

    def test_coverage_engine_verdicts_are_mapped_one_by_one(self):
        """覆盖引擎的四种结论必须是四个不同的原因码，不能合 fire。"""
        cases = (("对账未通过", assessment(quality="failed"), "source_quality_failed"),
                 ("部分覆盖", assessment(status="partial",
                                       missing=[("2026-09-01", "2026-09-04")]),
                  "coverage_incomplete"),
                 ("完全没覆盖", assessment(status="missing",
                                        missing=[("2026-09-01", "2026-09-08")]),
                  "coverage_incomplete"),
                 ("时间口径实测不成立", assessment(blocking=("S1",)),
                  "coverage_time_basis_unverified"),
                 ("时间口径未取证", assessment(disclosure=("S1",)),
                  "coverage_time_basis_unverified"),
                 ("共同截止未知", assessment(data_as_of=None), "data_as_of_unknown"),
                 ("来源未开通的店", assessment(unconfigured=("S9",)), "source_not_onboarded"),
                 ("引擎自己跳了", assessment(raise_error=RuntimeError("no sync_state")),
                  "contract_violation"))
        for label, ready, reason in cases:
            with self.subTest(case=label):
                conn = EvidenceConn()
                _execution, store, _context = run_graph(ready=ready, conn=conn)
                self.assertEqual(refusal_reason_of(store), reason)
                self.assertEqual(store.nodes, ["select_schema", "authorize_scope",
                                               "assess_readiness"])
                self.assertEqual(store.kinds.count("artifact"), 0)
                self.assertEqual(store.kinds.count("diagnostic"), 0)
                self.assertEqual(len(conn.statements), 1)

    def test_unattested_metric_is_refused_before_any_database_read(self):
        """目录里拿不到已登记能力标签的指标（如 `cost_total`）：一句库也不发。"""
        request = request_for("metric-cost-total", [DAY_COST, SHOP_COST],
                              start=GRAPH_START, end=GRAPH_END)
        conn = EvidenceConn()
        _execution, store, _context = run_graph(
            request, selection=selection_for(["metric-cost-total"],
                                             groups=[DAY_COST, SHOP_COST]), conn=conn)
        self.assertEqual(refusal_reason_of(store), "capability_unavailable")
        self.assertEqual(store.nodes, ["select_schema", "authorize_scope",
                                       "assess_readiness"])
        self.assertEqual(conn.statements, [])

    def test_mixed_basis_across_shops_is_not_summed(self):
        """同一指标在两家店来自不同口径：汇成一个数就是回答了一个没人问过的问题。"""
        conn = EvidenceConn(shops=[("S1", "fxg", ["paid_amount"]),
                                   ("S2", "jd", ["paid_amount"])])
        _execution, store, _context = run_graph(conn=conn)
        # 库里没有第五个码能叫 basis_incompatible：终止原因记在 contract_violation 上，
        # 但限制码与公开消息仍然是口径不兼容那一套，归因不丢。
        self.assertEqual(refusal_reason_of(store), "contract_violation")
        self.assertEqual(refusal_state(store)["limitations"], ["basis_incompatible"])
        self.assertEqual(store.kinds.count("artifact"), 0)

    def test_basis_entries_are_resolved_evidence_not_the_catalog_version(self):
        """公开载荷的 `basis` 必须来自来源注册表：目录版本不是口径凭证。"""
        from bi_agent.sources import METRIC_VERSION, PAYMENT_BASIS

        execution, _store, _context = run_graph()
        basis = execution.domain_result.artifacts[0].public_payload["basis"]
        self.assertEqual({item["metric"] for item in basis}, {PAID_METRIC})
        self.assertEqual({item["basis"] for item in basis}, {PAYMENT_BASIS})
        self.assertEqual({item["time_basis"] for item in basis}, {"pay_time"})
        self.assertEqual({item["metric_version"] for item in basis}, {METRIC_VERSION})
        self.assertEqual({item["shop_ref"] for item in basis},
                         {GRAPH_SHOP_REFS["S1"], GRAPH_SHOP_REFS["S2"]})
        for item in basis:
            self.assertNotIn("semantic/", item.values().__str__())
        self.assertEqual(execution.domain_result.model_payload["basis"], basis)

    def test_coverage_refusal_records_the_gap_and_the_limitation(self):
        _execution, store, _context = run_graph(
            ready=assessment(status="partial",
                             missing=[("2026-09-01", "2026-09-04")]))
        state = refusal_state(store)
        self.assertEqual(state["coverage"]["gaps"], ["2026-09-01~2026-09-04"])
        self.assertEqual(state["coverage"]["status"], "partial")
        self.assertEqual(state["limitations"], ["coverage_incomplete"])
        self.assertEqual(store.finish_record[2], "missing_data")

    def test_pool_only_authorization_is_refused_without_substituting_ids(self):
        """只能按池授权的视图：当场 `forbidden`，不拿池 id 去填店铺集合。"""
        selection = selection_for("metric-physical-available-quantity",
                                 groups=[POOL_PHYSICAL])
        execution, store, _context = run_graph(
            selection=selection, allowed_inventory_pool_ids=frozenset({"S1"}))
        self.assertEqual(store.nodes, ["select_schema", "authorize_scope"])
        self.assertEqual(refusal_reason_of(store), "forbidden")
        self.assertEqual(execution.domain_result.error.code, "forbidden")
        self.assertEqual(store.kinds.count("diagnostic"), 0)
        self.assertEqual(execution.domain_result.model_payload["status"], "unavailable")

    def test_empty_or_unmapped_scope_is_forbidden(self):
        for label, overrides in (("空集合", {"allowed_shop_ids": frozenset()}),
                                ("没有引用", {"shop_refs": {}})):
            with self.subTest(case=label):
                _execution, store, _context = run_graph(**overrides)
                self.assertEqual(refusal_reason_of(store), "forbidden")
                self.assertEqual(store.nodes, ["select_schema", "authorize_scope"])

    def test_exhausted_budget_never_reaches_the_gates(self):
        """剩不够两道 5 秒门加余量时直接停：宁可当场 deadline，也不留半截查询。"""
        conn = EvidenceConn()
        _execution, store, _context = run_graph(conn=conn,
                                                deadline=time.monotonic() + 3.0)
        self.assertEqual(refusal_reason_of(store), "deadline_exceeded")
        self.assertEqual(store.nodes, ["select_schema", "authorize_scope",
                                       "assess_readiness", "compile_query",
                                       "validate_ast", "estimate_cost"])
        self.assertEqual(len(conn.statements), 1, "只读了店铺事实，没有 EXPLAIN")
        for sql in conn.statements:
            self.assertTrue("reporting.v_shops" in sql or "reporting.v_coverage" in sql, sql)
        self.assertEqual(store.kinds.count("diagnostic"), 0)

    def test_second_gate_also_refuses_when_the_budget_is_short(self):
        """估算用掉大半预算后，只读执行这一道门也不跑：预算不能被烧成半截执行。"""
        from bi_agent.exploration.graph import FINAL_GATE_RESERVE_SECONDS, run_exploration_graph

        store = RecordingStore()
        context = graph_context(store, EvidenceConn(),
                                deadline=time.monotonic() + 16.0)
        executed = []

        def spending_estimate(conn, plan, *, deadline):
            # 模拟"EXPLAIN 花了 8 秒"：把剩余预算压到第二道门的最低线以下。
            context.deadline = time.monotonic() + FINAL_GATE_RESERVE_SECONDS - 1.0
            return stub_estimate(conn, plan, deadline=deadline)

        patches = graph_patches(estimate=spending_estimate,
                                execute=mock.Mock(side_effect=executed.append))
        from contextlib import ExitStack

        with ExitStack() as stack:
            for patcher in patches:
                stack.enter_context(patcher)
            execution = run_exploration_graph(
                question=GRAPH_QUESTION, request=ready_request(), context=context,
                versions=graph_versions())
        self.assertEqual(refusal_reason_of(store), "deadline_exceeded")
        # 第二道门已经进格（归因要指到它），但一句库语句都没发。
        self.assertEqual(store.nodes, list(EXPLORATION_NODES[:7]))
        self.assertEqual(executed, [], "第二道门不该发起任何语句")
        # 估算过的那份计划仍然交回（指纹与预算数字都是事实），但它一次也没被执行。
        self.assertIsNotNone(execution.plan.estimated_rows)
        self.assertEqual(store.kinds.count("diagnostic"), 0)
        self.assertEqual(store.kinds.count("artifact"), 0)

    def test_plan_is_never_mutated_after_fingerprinting(self):
        """指纹之后不许改：改了就是「发出去的结果与留证的语句不是同一条」。"""
        from bi_agent.exploration.graph import run_exploration_graph

        seen: list[tuple] = []
        store = RecordingStore()

        def capture_execute(conn, plan, *, deadline):
            seen.append((plan.statement_fingerprint, plan.sql_text,
                         dict(plan.parameters), tuple(plan.selected_refs)))
            return stub_execute(conn, plan, deadline=deadline)

        patches = graph_patches(execute=capture_execute)
        from contextlib import ExitStack

        with ExitStack() as stack:
            for patcher in patches:
                stack.enter_context(patcher)
            execution = run_exploration_graph(
                question=GRAPH_QUESTION, request=ready_request(),
                context=graph_context(store, EvidenceConn()),
                versions=graph_versions())
        fingerprint, sql_text, parameters, refs = seen[0]
        self.assertEqual(execution.plan.statement_fingerprint, fingerprint)
        self.assertEqual(execution.plan.sql_text, sql_text)
        self.assertEqual(dict(execution.plan.parameters), parameters)
        self.assertEqual(tuple(execution.plan.selected_refs), refs)
        artifact_log = store.log[store.kinds.index("artifact")]
        self.assertEqual(artifact_log[2]["statement_fingerprint"], fingerprint)
        self.assertEqual(store.log[store.kinds.index("diagnostic")][2], sql_text)

    def test_compile_rejection_is_attributed_to_the_selection(self):
        """编译器接不下这份选择：归因到契约违规，不是策略拒绝。"""
        selection = selection_for(READY_METRICS, groups=READY_GROUPS, selected_metrics=())
        _execution, store, _context = run_graph(selection=selection)
        self.assertEqual(store.nodes, ["select_schema", "authorize_scope",
                                       "assess_readiness", "compile_query"])
        self.assertEqual(refusal_reason_of(store), "contract_violation")
        self.assertEqual(store.kinds.count("diagnostic"), 0)


class ExplorationGraphFailureMappingTests(unittest.TestCase):
    """策略 / 成本 / 持久化三类失败：原因码分开，载荷与事件不沾 SQL。"""

    def test_policy_rejection_is_attributable_before_estimation(self):
        from bi_agent.exploration.graph import run_exploration_graph

        tampered = shell_draft(sql_text="SELECT * FROM reporting.v_shop_daily")
        store = RecordingStore()
        patches = graph_patches() + [mock.patch("bi_agent.exploration.graph.compile_query",
                                                return_value=tampered)]
        from contextlib import ExitStack

        with ExitStack() as stack:
            for patcher in patches:
                stack.enter_context(patcher)
            execution = run_exploration_graph(
                question=GRAPH_QUESTION, request=ready_request(),
                context=graph_context(store, EvidenceConn()),
                versions=graph_versions())
        self.assertEqual(refusal_reason_of(store), "sql_policy_rejected")
        self.assertEqual(store.nodes, list(EXPLORATION_NODES[:5]))
        self.assertEqual(store.kinds.count("diagnostic"), 0,
                         "被策略拒掉的语句不该进诊断表")
        self.assertIsNone(execution.plan)
        self.assertNotIn("SELECT", repr(store.log))

    def test_cost_over_budget_is_reported_as_cost_not_as_timeout(self):
        from bi_agent.exploration.graph import run_exploration_graph
        from bi_agent.exploration.models import ExplorationBudgetExceeded

        store = RecordingStore()
        patches = graph_patches(estimate=mock.Mock(
            side_effect=ExplorationBudgetExceeded("estimated_rows")))
        from contextlib import ExitStack

        with ExitStack() as stack:
            for patcher in patches:
                stack.enter_context(patcher)
            execution = run_exploration_graph(
                question=GRAPH_QUESTION, request=ready_request(),
                context=graph_context(store, EvidenceConn()),
                versions=graph_versions())
        self.assertEqual(refusal_reason_of(store), "query_cost_exceeded")
        self.assertEqual(store.kinds.count("artifact"), 0)
        self.assertEqual(execution.domain_result.status.value, "failed")
        self.assertNotEqual(refusal_reason_of(store), "query_timeout")

    def test_projection_and_statement_refusals_publish_nothing(self):
        from bi_agent.exploration.graph import run_exploration_graph

        for label, error in (("投影拒收", ValueError("exploration_shop_not_registered")),
                             ("语句被库拒", ValueError("exploration_query_rejected")),
                             ("结果过大", ValueError("exploration_result_too_large"))):
            with self.subTest(case=label):
                store = RecordingStore()
                patches = graph_patches(execute=mock.Mock(side_effect=error))
                from contextlib import ExitStack

                with ExitStack() as stack:
                    for patcher in patches:
                        stack.enter_context(patcher)
                    execution = run_exploration_graph(
                        question=GRAPH_QUESTION, request=ready_request(),
                        context=graph_context(store, EvidenceConn()),
                        versions=graph_versions())
                self.assertEqual(execution.domain_result.status.value, "failed")
                self.assertEqual(store.kinds.count("artifact"), 0)
                self.assertEqual(store.kinds.count("diagnostic"), 0)
                self.assertNotIn("SELECT", repr(execution.domain_result.model_payload))
                self.assertNotIn("S1", repr(execution.domain_result.model_payload))
                self.assertEqual(store.finish_record[1], "finalize")

    def test_write_failures_clear_the_public_result(self):
        """证据留不下 → 不发结果；结果存不下 → 也不算成功。"""
        from bi_agent.exploration.graph import run_exploration_graph

        for label, store in (("diagnostic", RecordingStore(fail_diagnostic=True)),
                             ("artifact", RecordingStore(fail_artifact=True))):
            with self.subTest(case=label):
                from contextlib import ExitStack

                with ExitStack() as stack:
                    for patcher in graph_patches():
                        stack.enter_context(patcher)
                    execution = run_exploration_graph(
                        question=GRAPH_QUESTION, request=ready_request(),
                        context=graph_context(store, EvidenceConn()),
                        versions=graph_versions())
                self.assertEqual(execution.domain_result.status.value, "failed")
                self.assertEqual(execution.domain_result.error.code,
                                 "artifact_persistence_failed")
                self.assertEqual(refusal_reason_of(store), "persistence_failed")
                self.assertEqual(execution.domain_result.artifacts, [])
                self.assertEqual(store.artifacts, {})
                # 计划仍然交了：指纹在，只是结果没发出去。
                self.assertIsNotNone(execution.plan)


class ExplorationToolAdapterTests(unittest.TestCase):
    """Tool 面：模型只能给 ref 与业务值，提问由服务端注入。"""

    def _selection(self, **overrides):
        return selection_for(READY_METRICS, groups=READY_GROUPS, **overrides)

    def _call(self, arguments, error=None):
        from bi_agent.llm import ToolCall

        return ToolCall(id="call_1", name="explore_business_data", arguments=arguments,
                        arguments_error=error)

    def test_schema_offers_only_this_rounds_refs_and_no_server_keys(self):
        from bi_agent.exploration.tool import exploration_request_schema

        selection = self._selection()
        schema = exploration_request_schema(selection)
        self.assertEqual(set(schema["properties"]),
                         {"entity_refs", "requested_metric_refs", "group_by_field_refs",
                          "start", "end", "limit"})
        self.assertIs(schema["additionalProperties"], False)
        self.assertEqual(schema["required"], ["requested_metric_refs"])
        for name, allowed in (("entity_refs", selection.entity_refs),
                              ("requested_metric_refs", selection.metric_refs),
                              ("group_by_field_refs", selection.field_refs)):
            with self.subTest(field=name):
                self.assertEqual(schema["properties"][name]["items"]["enum"],
                                 sorted(allowed))
        for banned in ("question", "sql", "sql_text", "shop_id", "shop_ids",
                      "allowed_shop_ids", "statement_fingerprint", "catalog_version",
                      "selected_refs", "domain", "parameters"):
            self.assertNotIn(banned, schema["properties"], banned)
        # 换一个 selection 就换一个 enum：词表不是写死的。
        # 换一个 selection 就换一个 enum：词表不是写死的，而是跟着本轮检索走。
        narrow = selection_for(["metric-paid-amount"])
        other = exploration_request_schema(narrow)
        self.assertEqual(other["properties"]["requested_metric_refs"]["items"]["enum"],
                         ["metric-paid-amount"])
        self.assertEqual(other["properties"]["group_by_field_refs"]["items"]["enum"],
                         sorted(narrow.field_refs))
        with self.assertRaises(TypeError):
            exploration_request_schema({})

    def test_server_question_replaces_anything_the_model_might_offer(self):
        from bi_agent.exploration.tool import execute_exploration_tool

        captured: dict = {}
        placeholder = _ToolPassThrough()

        def fake_graph(**kwargs):
            captured.update(kwargs)
            return placeholder

        store = RecordingStore()
        conn = EvidenceConn()
        with mock.patch("bi_agent.exploration.tool.run_exploration_graph",
                        side_effect=fake_graph), \
                mock.patch("bi_agent.exploration.tool.retrieve_schema_candidates",
                           return_value=self._selection()):
            returned = execute_exploration_tool(
                self._call({"requested_metric_refs": [PAID_METRIC],
                            "group_by_field_refs": [DAY_SHOP_DAILY], "limit": 20}),
                graph_context(store, conn), question=GRAPH_QUESTION,
                versions=graph_versions())
        self.assertIs(returned, placeholder)
        self.assertEqual(captured["question"], GRAPH_QUESTION)
        self.assertEqual(captured["request"].question, GRAPH_QUESTION)
        self.assertEqual(captured["versions"], graph_versions())
        self.assertEqual(captured["request"].limit, 20)
        self.assertEqual(store.log, [], "图接管了运行记录，Tool 不再补写")

    def test_server_owned_and_unparsable_arguments_are_refused_before_sql(self):
        from bi_agent.exploration.tool import execute_exploration_tool

        cases = (("带 SQL", {"requested_metric_refs": [PAID_METRIC],
                           "sql": "SELECT * FROM bi.orders"}, None),
                 ("带提问", {"requested_metric_refs": [PAID_METRIC],
                           "question": "自己写提问"}, None),
                 ("带店号", {"requested_metric_refs": [PAID_METRIC],
                           "shop_ids": ["S1"]}, None),
                 ("带授权", {"requested_metric_refs": [PAID_METRIC],
                           "allowed_shop_ids": ["S1"]}, None),
                 ("缺指标", {"group_by_field_refs": [DAY_SHOP_DAILY]}, None),
                 ("limit 越界", {"requested_metric_refs": [PAID_METRIC],
                             "limit": 5000}, None),
                 ("窗口不完整", {"requested_metric_refs": [PAID_METRIC],
                             "start": "2026-09-01"}, None),
                 ("ref 不是 ref", {"requested_metric_refs": ["SUM(cost_total)"]}, None),
                 ("不可解析", None, "json truncated"),
                 ("参数缺席", None, None))
        for label, arguments, error in cases:
            with self.subTest(case=label):
                store = RecordingStore()
                conn = EvidenceConn()
                with mock.patch("bi_agent.exploration.tool.run_exploration_graph") as graph:
                    execution = execute_exploration_tool(
                        self._call(arguments, error), graph_context(store, conn),
                        question=GRAPH_QUESTION, versions=graph_versions())
                graph.assert_not_called()
                self.assertIsNone(execution.plan)
                self.assertEqual(execution.domain_result.status.value, "needs_input")
                self.assertEqual(execution.domain_result.error.code, "invalid_parameters")
                self.assertEqual(execution.domain_result.artifacts, [])
                self.assertEqual(store.kinds.count("artifact"), 0)
                self.assertEqual(store.kinds.count("diagnostic"), 0)
                self.assertEqual(conn.statements, [])
                # 原因码可归因，而且留下一条可审计的运行记录。
                self.assertEqual(store.kinds[0], "create_run")
                self.assertEqual(store.kinds[-1], "finish")
                self.assertEqual(store.finish_record[3], "invalid_parameters")
                self.assertEqual(store.finish_record[1], "finalize")
                self.assertNotIn("SELECT", repr(store.log))
                self.assertNotIn("S1", repr(store.log))

    def test_refs_outside_the_servers_selection_are_refused(self):
        """枚举是提示，不是凭据：服务端重检一遍才算授权。

        三个用例都给的是**目录里真实存在**的 ref（不是乱写的字符串），只是本轮检索没
        选中它们：形状合法而集合外，才是最需要子集检查的那一类。
        """
        from bi_agent.exploration.tool import execute_exploration_tool

        cases = (("指标越界", {"requested_metric_refs": ["metric-paid-orders"],
                            "group_by_field_refs": [DAY_SHOP_DAILY]}),
                 ("分组越界", {"requested_metric_refs": [PAID_METRIC],
                            "group_by_field_refs": [SHOPS_PLATFORM]}),
                 ("实体越界", {"requested_metric_refs": [PAID_METRIC],
                            "entity_refs": ["entity-product"],
                            "group_by_field_refs": [DAY_SHOP_DAILY]}))
        for label, arguments in cases:
            with self.subTest(case=label):
                store = RecordingStore()
                conn = EvidenceConn()
                with mock.patch("bi_agent.exploration.tool.retrieve_schema_candidates",
                                return_value=self._selection()),                         mock.patch("bi_agent.exploration.tool.run_exploration_graph") as graph:
                    execution = execute_exploration_tool(self._call(arguments),
                                                        graph_context(store, conn),
                                                        question=GRAPH_QUESTION,
                                                        versions=graph_versions())
                graph.assert_not_called()
                self.assertIsNone(execution.plan)
                self.assertEqual(store.finish_record[3], "schema_ambiguous")
                self.assertEqual(store.kinds.count("artifact"), 0)
                self.assertEqual(conn.statements, [])

    def test_stale_catalog_version_is_refused_without_running_the_graph(self):
        from bi_agent.exploration.tool import execute_exploration_tool

        store = RecordingStore()
        conn = EvidenceConn()
        stale = graph_versions().model_copy(
            update={"semantic_catalog_version": "semantic/1970-01-01.1"})
        with mock.patch("bi_agent.exploration.tool.run_exploration_graph") as graph:
            execution = execute_exploration_tool(
                self._call({"requested_metric_refs": [PAID_METRIC],
                            "group_by_field_refs": [DAY_SHOP_DAILY]}),
                graph_context(store, conn), question=GRAPH_QUESTION, versions=stale)
        graph.assert_not_called()
        self.assertIsNone(execution.plan)
        self.assertEqual(store.finish_record[3], "schema_ambiguous")

    def test_tool_refusal_payload_still_passes_the_runtime_validator(self):
        """拒答载荷也是公开载荷：形状不合法就是第二份 bug。"""
        from bi_agent.exploration.tool import execute_exploration_tool
        from bi_agent.runtime.models import validate_model_payload

        store = RecordingStore()
        conn = EvidenceConn()
        with mock.patch("bi_agent.exploration.tool.run_exploration_graph"):
            execution = execute_exploration_tool(self._call({"sql": "SELECT 1"}),
                                                graph_context(store, conn),
                                                question=GRAPH_QUESTION,
                                                versions=graph_versions())
        payload = execution.domain_result.model_payload
        self.assertEqual(validate_model_payload(payload), payload)
        self.assertEqual(payload["termination_reason"], "invalid_parameters")


class _ToolPassThrough:
    """Tool 用例里替图接住的返回值：只证明 Tool 原样转交。"""

    plan = None
    domain_result = None


class ExplorationGraphCrashFinalizationTests(unittest.TestCase):
    """P1-2 回归（red→green）：意外异常不能让运行记录停在 `running`。

    唯一键 `(user_message_id, domain, attempt_no)` 会占住同一次尝试的重放（`sql/004`），
    而会话侧没有回收器：一条半死的运行如果不落终态，就既查不出为什么失败，也重不了。
    与经营/运营/价审/库存四张图同一契约：best-effort 收尾，然后**原样上抛**。
    """

    def test_mid_chain_store_failure_still_finalizes_and_reraises(self):
        from bi_agent.exploration.graph import run_exploration_graph

        for label, kwargs in (("第二条推进失败", {"fail_transition_after": 2}),
                              ("最后一条推进失败", {"fail_transition_after": 6})):
            with self.subTest(case=label):
                store = RecordingStore(**kwargs)
                patches = graph_patches()
                from contextlib import ExitStack

                with ExitStack() as stack:
                    for patcher in patches:
                        stack.enter_context(patcher)
                    with self.assertRaises(RuntimeError):
                        run_exploration_graph(
                            question=GRAPH_QUESTION, request=ready_request(),
                            context=graph_context(store, EvidenceConn()),
                            versions=graph_versions())
                # 收尾补上了一个终态，而不是留在 running。
                self.assertEqual(store.finishes, 1)
                self.assertEqual(store.finish_record[1], "finalize")
                self.assertEqual(store.finish_record[2], "failed")
                self.assertEqual(store.finish_record[3], "upstream_unavailable")
                self.assertEqual(store.finish_record[4], "unavailable")
                self.assertEqual(store.kinds.count("artifact"), 0)
                self.assertEqual(store.kinds.count("diagnostic"), 0)

    def test_unexpected_step_failure_finalizes_once(self):
        """节点自已在收尾之外爆炸（不是可归因的招拒）：也不能留 running。"""
        from bi_agent.exploration.graph import run_exploration_graph

        store = RecordingStore()
        patches = graph_patches() + [
            mock.patch("bi_agent.exploration.graph.compile_query",
                       side_effect=KeyError("programmer error"))]
        from contextlib import ExitStack

        with ExitStack() as stack:
            for patcher in patches:
                stack.enter_context(patcher)
            with self.assertRaises(KeyError):
                run_exploration_graph(question=GRAPH_QUESTION, request=ready_request(),
                                      context=graph_context(store, EvidenceConn()),
                                      versions=graph_versions())
        self.assertEqual(store.finishes, 1)
        self.assertEqual(store.finish_record[2], "failed")
        self.assertEqual(store.finish_record[3], "upstream_unavailable")
        # 不能把本来已归因的拒答劫成另一个原因：这一步压根没跑到收尾。
        self.assertNotEqual(store.finish_record[3], "contract_violation")

    def test_finalize_failure_is_not_replaced_by_the_cleanup_write(self):
        """收尾本身也写了：不掩盖原异常，也不无限重试（二次写失败必须吞掉）。"""
        from bi_agent.exploration.graph import run_exploration_graph

        store = RecordingStore(fail_finish=True)
        from contextlib import ExitStack

        with ExitStack() as stack:
            for patcher in graph_patches():
                stack.enter_context(patcher)
            with self.assertRaises(RuntimeError) as caught:
                run_exploration_graph(question=GRAPH_QUESTION, request=ready_request(),
                                      context=graph_context(store, EvidenceConn()),
                                      versions=graph_versions())
        self.assertIn("finish write failed", str(caught.exception))
        self.assertEqual(store.finishes, 2, "一次收尾 + 一次 best-effort 补写")

    def test_happy_path_does_not_write_a_second_completion(self):
        _execution, store, _context = run_graph()
        self.assertEqual(store.finishes, 1)
        self.assertEqual(store.finish_record[2], "succeeded")
        self.assertNotEqual(store.finish_record[3], "upstream_unavailable")


if __name__ == "__main__":
    unittest.main()
