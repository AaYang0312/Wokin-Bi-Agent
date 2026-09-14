# Controlled SQL Exploration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为固定业务 Tool 无法表达、但来源能力与覆盖已满足的问题提供受控只读聚合查询，并把 SQL 安全、授权、预算、口径和结果持久化做成可审计闭环。

**Architecture:** Agent 不提交 SQL 标识符；服务端先用已发布语义目录检索稳定 ref，再把受控 `ExplorationRequest` 编译为参数化单条 SELECT。执行前依次校验固定 Tool 优先级、能力/覆盖、SQL AST、最小权限、EXPLAIN 成本和整轮 deadline；SQL 原文仅进入 `bi.query_diagnostics`，公开 Artifact 只保存安全结果和 statement fingerprint。

**Tech Stack:** Python 3.11+、Pydantic 2、psycopg 3、PostgreSQL 17、FastAPI、`sqlglot>=27.14,<28`（PostgreSQL AST，仅作为纵深防御之一）、`unittest`。

**Spec:** [Task 11 后置子项目总设计](../specs/2026-09-14-post-task11-subprojects-design.md) §3–4、§6、§10–12；前置计划：[Semantic Catalog and Schema Retrieval](2026-09-14-semantic-catalog-and-schema-retrieval.md)。

## Global Constraints

- Task 11 发布门禁和语义目录验收必须已经通过；找不到两份日期化验收记录时停止实现。
- `CONTROLLED_SQL_ENABLED=false` 为默认值；关闭时 Tool 列表、数据库读和聊天结果与当前版本一致。
- 固定 Tool 可表达的问题、缺能力、缺覆盖、来源未认证、写请求和未注册概念都不得降级到 SQL。
- 模型只提供语义 ref 和业务值；SQL schema/table/column/function、真实店铺 ID、授权集合与 limit 上限由服务端决定。
- 只允许单条非递归 SELECT；拒绝 `SELECT *`、CTE、子查询、未登记 JOIN、CROSS JOIN、DDL/DML/COPY/CALL/DO/锁和多语句。
- 查询运行在 `bi_app`、`READ ONLY` 事务和 `statement_timeout <= 5s` 下；结果最多 500 行、256 KiB，整轮仍为 30 秒 deadline。
- SQL 与内部参数只进入 `bi.query_diagnostics`；普通事件、模型消息、公开 Artifact 和应用日志不得出现 SQL 原文、DSN 或真实 ID。
- 任何必要 Artifact 或诊断记录写入失败都不能返回成功；历史 SQL 可查看但版本变化后不可复用。
- 迁移顺序已冻结：当前最高为 019，本计划只使用 `020_controlled_sql_exploration.sql`；执行前发现 020 已被其他变更占用时，本计划预检失败，须先单独修订并重新评审计划，不能在实现中临时改号。

## Execution Preflight

- [ ] 验证 Task 11、语义目录验收和依赖版本。

```powershell
rg --files docs/superpowers/research | rg 'task-11-release-acceptance|semantic-catalog-acceptance'
rg -n 'SEMANTIC_CATALOG_VERSION|retrieve_schema_candidates' backend/bi_agent/semantic_catalog
Get-ChildItem backend/sql -Filter '*.sql' | Sort-Object Name | Select-Object -Last 3 -ExpandProperty Name
git status --short --branch
```

Expected: 两份验收记录存在；语义目录接口已发布；最高已应用迁移为 019 且 020 尚不存在；无关工作树改动保持不动。

---

### Task 1: Exploration Contracts, Dependency, and Feature Gate

**Files:**
- Modify: `backend/pyproject.toml`
- Modify: `backend/uv.lock`
- Create: `backend/bi_agent/exploration/__init__.py`
- Create: `backend/bi_agent/exploration/models.py`
- Modify: `backend/bi_agent/config.py`
- Modify: `.env.example`
- Create: `backend/tests/test_exploration.py`
- Modify: `backend/tests/test_core.py`

**Interfaces:**
- Consumes: `SemanticSelection`、`VersionSet`。
- Produces: `ExplorationRequest`、`SqlDraft`、`ValidatedQueryPlan`、`ExplorationColumn`、`ExplorationResult`、`AppSettings.controlled_sql_enabled`。

- [ ] **Step 1: 写严格请求与配置依赖测试。**

```python
class ExplorationContractTests(unittest.TestCase):
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

    def test_controlled_sql_requires_semantic_catalog(self):
        from bi_agent.config import load_app_settings
        env = valid_app_env() | {
            "SEMANTIC_CATALOG_ENABLED": "false",
            "CONTROLLED_SQL_ENABLED": "true",
        }
        with self.assertRaisesRegex(ValueError, "CONTROLLED_SQL_REQUIRES_SEMANTIC_CATALOG"):
            load_app_settings(env)
```

- [ ] **Step 2: 运行测试并确认缺少探索契约。**

Run: `uv run --locked python -m unittest tests.test_exploration.ExplorationContractTests -v`

Expected: FAIL with `ModuleNotFoundError: bi_agent.exploration`。

- [ ] **Step 3: 添加并锁定 sqlglot。**

```powershell
Set-Location backend
uv add "sqlglot>=27.14,<28"
uv lock --check
```

Expected: `pyproject.toml` 出现精确范围，`uv.lock` 包含唯一 sqlglot 解析结果；不升级无关直接依赖。

- [ ] **Step 4: 实现 Pydantic 契约。**

`models.py` 使用 `extra="forbid"`、`hide_input_in_errors=True`；字段固定如下：

```python
class ExplorationRequest(BaseModel):
    question: str = Field(min_length=1, max_length=4000)
    start: date | None = None
    end: date | None = None
    entity_refs: list[str] = Field(default_factory=list, max_length=5)
    requested_metric_refs: list[str] = Field(min_length=1, max_length=8)
    group_by_field_refs: list[str] = Field(default_factory=list, max_length=4)
    limit: int = Field(default=100, ge=1, le=500)

class SqlDraft(BaseModel):
    sql_text: str
    parameters: dict[str, object]
    selected_refs: list[str]

class ValidatedQueryPlan(BaseModel):
    template_version: str
    catalog_version: str
    statement_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    sql_text: str
    parameters: dict[str, object]
    selected_refs: list[str]
    estimated_rows: int | None = Field(default=None, ge=0)
    estimated_total_cost: Decimal | None = Field(default=None, ge=0)
    warnings: list[str] = Field(default_factory=list)

class ExplorationColumn(BaseModel):
    ref: str
    data_type: Literal["date", "datetime", "decimal", "integer", "boolean", "text", "ref"]

class ExplorationResult(BaseModel):
    template_version: str
    catalog_version: str
    statement_fingerprint: str
    columns: list[ExplorationColumn]
    rows: list[dict[str, object]] = Field(max_length=500)
    basis: list[dict[str, str]]
    coverage: dict[str, object]
    diagnostics: list[dict[str, object]]
    limitations: list[str]
```

所有 ref 复用语义目录 `_require_ref()`；请求校验 `start/end` 必须同时为空或同时存在且 `start < end <= start + 366 days`，列表去重后长度不变，否则拒绝。

- [ ] **Step 5: 实现配置门禁。**

`AppSettings` 增加 `controlled_sql_enabled: bool = False`；`load_app_settings()` 只接受 `true/false`，并在 true 且 `semantic_catalog_enabled` 为 false 时抛上面的稳定错误。`.env.example` 增加 `CONTROLLED_SQL_ENABLED=false`。

- [ ] **Step 6: 运行契约和配置测试。**

```powershell
uv run --locked python -m unittest tests.test_exploration.ExplorationContractTests tests.test_core.ConfigTests -v
```

Expected: PASS。

- [ ] **Step 7: 提交契约切片。**

```powershell
git add backend/pyproject.toml backend/uv.lock backend/bi_agent/exploration backend/bi_agent/config.py backend/tests/test_exploration.py backend/tests/test_core.py .env.example
git commit -m "feat: define controlled exploration contracts"
```

---

### Task 2: Fixed-Tool Eligibility and Deterministic SQL Compiler

**Files:**
- Create: `backend/bi_agent/exploration/eligibility.py`
- Create: `backend/bi_agent/exploration/compiler.py`
- Modify: `backend/bi_agent/exploration/__init__.py`
- Modify: `backend/tests/test_exploration.py`

**Interfaces:**
- Consumes: `ExplorationRequest`、`SemanticSelection`、`CATALOG`、`resolve_sql_identifier()`。
- Produces: `fixed_tool_for(selection, request) -> str | None`、`compile_query(request, *, selection, allowed_shop_ids) -> SqlDraft`。

- [ ] **Step 1: 写固定 Tool 优先和服务端授权注入测试。**

```python
class ExplorationCompilerTests(unittest.TestCase):
    def test_fixed_paid_amount_query_is_not_eligible(self):
        from bi_agent.exploration.eligibility import fixed_tool_for
        self.assertEqual(
            fixed_tool_for(selection_for("metric-paid-amount"),
                           request_for("metric-paid-amount")),
            "query_business",
        )

    def test_compiler_injects_scope_and_never_accepts_identifier_input(self):
        from bi_agent.exploration.compiler import compile_query
        draft = compile_query(
            request_for("metric-cost-total", groups=["field-day"]),
            selection=selection_for("metric-cost-total"),
            allowed_shop_ids=frozenset({"S1", "S2"}))
        self.assertIn("reporting.v_product_cost_daily", draft.sql_text)
        self.assertIn("shop_id = ANY(%(allowed_shop_ids)s)", draft.sql_text)
        self.assertNotIn("S1", draft.sql_text)
        self.assertEqual(draft.parameters["allowed_shop_ids"], ["S1", "S2"])
```

- [ ] **Step 2: 运行测试并确认 compiler 不存在。**

Run: `uv run --locked python -m unittest tests.test_exploration.ExplorationCompilerTests -v`

Expected: FAIL with import error。

- [ ] **Step 3: 实现固定 Tool 覆盖矩阵。**

`FIXED_TOOL_METRICS` 固定为：

```python
FIXED_TOOL_METRICS = {
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
```

只要一个固定 Tool 能覆盖全部 requested metrics 和 group_by 粒度，就返回该工具名并拒绝探索。缺 capability/coverage 不影响此判断：不能因固定 Tool 正确拒答而换 SQL 绕过。

- [ ] **Step 4: 实现单基表、可选 many-to-one JOIN 的编译器。**

编译规则：所有 metric 必须来自同一个 base view、每个 metric 恰有一个 required field、使用其 `default_aggregate`；group field 必须属于 base view，唯一例外是通过登记的 many-to-one `view-shops` JOIN 取 `field-platform`。每个事实 view 必须有 time field 和 authorization field。

SQL 模板固定为：

```sql
SELECT {group_columns}, {aggregate_columns}
FROM {reporting_view} AS fact
{optional_many_to_one_join}
WHERE fact.{authorization_column} = ANY(%(allowed_shop_ids)s)
  AND fact.{time_column} >= %(start)s
  AND fact.{time_column} < %(end)s
GROUP BY {group_columns}
ORDER BY {group_columns}
LIMIT %(limit)s
```

标识符只能来自 `resolve_sql_identifier()` 并匹配 `^[a-z_][a-z0-9_]*$`，然后用双引号包裹；参数按 key 排序，`allowed_shop_ids` 排序后注入。没有日期或 view 无 time field 返回 `ValueError("exploration_window_required")`。禁止请求方覆盖任何参数 key。

- [ ] **Step 5: 测试 JOIN 放大和跨基表被拒。**

添加 product-cost + erp-document 两 metric 拒绝 `exploration_multiple_fact_grains`；请求未登记 product↔document JOIN 拒绝 `exploration_join_not_registered`；platform many-to-one JOIN 通过且 GROUP BY 只含 `shops.platform`。

- [ ] **Step 6: 运行 compiler 测试。**

Run: `uv run --locked python -m unittest tests.test_exploration.ExplorationCompilerTests -v`

Expected: PASS。

- [ ] **Step 7: 提交编译器切片。**

```powershell
git add backend/bi_agent/exploration backend/tests/test_exploration.py
git commit -m "feat: compile exploration queries from semantic refs"
```

---

### Task 3: AST Policy and Attack Corpus

**Files:**
- Create: `backend/bi_agent/exploration/policy.py`
- Create: `backend/tests/exploration_attacks.jsonl`
- Modify: `backend/tests/test_exploration.py`

**Interfaces:**
- Consumes: `SqlDraft`、`SemanticSelection`、`DomainContext`。
- Produces: `validate_exploration_plan(draft, *, selection, context) -> ValidatedQueryPlan`。

- [ ] **Step 1: 写允许 SQL 与攻击语料 runner。**

`exploration_attacks.jsonl` 至少写 20 行，固定形状：

```json
{"id":"A01","sql":"SELECT * FROM reporting.v_shop_daily","reason":"star_forbidden"}
{"id":"A02","sql":"SELECT 1; DELETE FROM bi.orders","reason":"multiple_statements"}
{"id":"A03","sql":"WITH x AS (DELETE FROM bi.orders RETURNING *) SELECT * FROM x","reason":"cte_forbidden"}
{"id":"A04","sql":"SELECT pg_read_file('/etc/passwd')","reason":"function_forbidden"}
{"id":"A05","sql":"SELECT sum(p.amount) FROM reporting.v_payments p CROSS JOIN reporting.v_refunds r","reason":"cross_join_forbidden"}
```

继续覆盖 INSERT/UPDATE/DELETE/DDL/COPY/CALL/DO/LOCK、注释多语句、相关子查询、未登记 schema/view/column/function、未登记 JOIN、UNION 和无授权谓词。

- [ ] **Step 2: 运行 policy 测试并确认模块不存在。**

Run: `uv run --locked python -m unittest tests.test_exploration.ExplorationPolicyTests -v`

Expected: FAIL with import error。

- [ ] **Step 3: 实现 placeholder 归一化和单 SELECT 检查。**

只允许 `%(<lower_snake_case>)s`；placeholder 集必须与 `draft.parameters` key 完全相同。解析前将每个 placeholder 替换为 SQL `NULL`，保留原 SQL 用于 fingerprint 和执行。调用 `sqlglot.parse(..., read="postgres")`，长度必须为 1，根节点必须为 `exp.Select`，且 AST 不含 `With/Subquery/Union/Intersect/Except/Command/Insert/Update/Delete/Create/Drop/Alter/Copy/Lock`。

- [ ] **Step 4: 实现白名单 AST 遍历。**

允许函数仅 `sum/count/avg/min/max` 和授权谓词中的 `any`；拒绝 Star、匿名函数、未登记 table/column、CROSS/NATURAL join。AST 中的 tables、columns 和 join pairs 必须与 `selection.view_refs/field_refs/join_path_refs` 解析后的集合完全相容。WHERE 必须存在 authorization column `= ANY(NULL)` 和 `[start,end)` 两个时间谓词；limit 必须存在且参数是 `limit`。

`statement_fingerprint` 为以下 JSON 的 SHA-256：规范化 SQL、排序参数名、selection refs、`context.allowed_shop_ids` 的 SHA-256、semantic catalog version；不得把真实 ID 写进 fingerprint 输入以外的持久化载荷。

- [ ] **Step 5: 运行攻击语料和正常 compiler 输出。**

```powershell
uv run --locked python -m unittest tests.test_exploration.ExplorationPolicyTests -v
```

Expected: 20/20 攻击在数据库执行前拒绝；compiler 生成的 product-cost query 通过并得到 64 位 fingerprint。

- [ ] **Step 6: 提交策略切片。**

```powershell
git add backend/bi_agent/exploration/policy.py backend/tests/test_exploration.py backend/tests/exploration_attacks.jsonl
git commit -m "feat: enforce exploration sql ast policy"
```

---

### Task 4: Read-Only Cost Gate and Safe Result Projection

**Files:**
- Create: `backend/bi_agent/exploration/repository.py`
- Create: `backend/bi_agent/exploration/projection.py`
- Modify: `backend/bi_agent/exploration/models.py`
- Modify: `backend/tests/test_exploration.py`
- Modify: `backend/tests/test_runtime_db.py`

**Interfaces:**
- Consumes: `ValidatedQueryPlan`、`DomainContext.shop_refs`。
- Produces: `estimate_plan(conn, plan, *, deadline) -> ValidatedQueryPlan`、`execute_plan(conn, plan, *, deadline) -> tuple[list[str], list[tuple[object, ...]]]`、`project_result(columns, rows, *, shop_refs) -> tuple[list[ExplorationColumn], list[dict[str, object]]]`。

- [ ] **Step 1: 写超成本、只读和投影脱敏测试。**

```python
class ExplorationRepositoryTests(unittest.TestCase):
    def test_internal_shop_id_becomes_ref_and_decimal_becomes_string(self):
        from decimal import Decimal
        from bi_agent.exploration.projection import project_result
        columns, rows = project_result(
            ["_shop_id", "metric-cost-total"], [("S1", Decimal("12.30"))],
            shop_refs={"S1": "ent-shop-one"})
        self.assertEqual(rows, [{"shop-ref": "ent-shop-one", "metric-cost-total": "12.30"}])
        self.assertNotIn("S1", repr(rows))

    def test_write_statement_is_refused_by_database_role(self):
        with reader_connection() as conn:
            with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                conn.execute("DELETE FROM bi.orders")
```

- [ ] **Step 2: 运行 repository 测试并确认实现缺失。**

Run: `uv run --locked --env-file ../.env.test python -m unittest tests.test_exploration.ExplorationRepositoryTests -v`

Expected: FAIL with import error；数据库权限断言必须实际执行。

- [ ] **Step 3: 实现 EXPLAIN 成本门禁。**

常量固定：`MAX_ESTIMATED_ROWS=50_000`、`MAX_TOTAL_COST=Decimal("100000")`、`STATEMENT_TIMEOUT_MS=5000`。在 `conn.transaction()` 内先执行 `SET TRANSACTION READ ONLY` 和 `SET LOCAL statement_timeout = '5000ms'`，再执行 `EXPLAIN (FORMAT JSON) {sql}`。读取根 Plan 的 `Plan Rows` 与 `Total Cost`；任一超限抛稳定 `ExplorationBudgetExceeded("estimated_rows"|"total_cost")`。deadline 剩余不足 0.1 秒时不碰数据库。

- [ ] **Step 4: 实现受控执行和 256 KiB 上限。**

通过成本门禁后在新的只读事务执行原 SQL；fetch `limit + 1` 行，超过 limit 拒绝而不截断。把 cursor.description 列名与编译器声明逐项比较。对投影后的 JSON 使用 `ensure_ascii=False,separators=(",", ":")` 编码，超过 262144 bytes 抛 `exploration_result_too_large`。

- [ ] **Step 5: 实现安全投影。**

允许值类型仅 `None/bool/int/Decimal/date/datetime/str`；Decimal 输出普通十进制字符串，datetime 必须有时区并输出 ISO。内部列 `_shop_id` 必须在 `shop_refs` 中并改成 `shop-ref`；其他以下划线开头的列全部拒绝。列名必须是已选 field/metric ref；重复列、未知 ref、NaN/Infinity 和超过 4000 字符文本拒绝。

- [ ] **Step 6: 运行真实 DB 测试。**

```powershell
uv run --locked --env-file ../.env.test python -m unittest tests.test_exploration.ExplorationRepositoryTests tests.test_runtime_db -v
```

Expected: PASS，0 skip；EXPLAIN、只读执行、权限拒绝与投影断言均实际运行。

- [ ] **Step 7: 提交执行层。**

```powershell
git add backend/bi_agent/exploration backend/tests/test_exploration.py backend/tests/test_runtime_db.py
git commit -m "feat: execute bounded read-only explorations"
```

---

### Task 5: Runtime Domain, Persistence, and Agent Tool

**Files:**
- Create: `backend/sql/020_controlled_sql_exploration.sql`
- Create: `backend/bi_agent/exploration/graph.py`
- Create: `backend/bi_agent/exploration/tool.py`
- Modify: `backend/bi_agent/runtime/domain_registry.py`
- Modify: `backend/bi_agent/runtime/models.py`
- Modify: `backend/bi_agent/runtime/artifacts.py`
- Modify: `backend/bi_agent/runtime/memory.py`
- Modify: `backend/bi_agent/runtime/repository.py`
- Modify: `backend/bi_agent/agent.py`
- Modify: `backend/tests/test_exploration.py`
- Modify: `backend/tests/test_runtime.py`
- Modify: `backend/tests/test_runtime_db.py`
- Modify: `backend/tests/test_core.py`

**Interfaces:**
- Consumes: Tasks 1–4、`DomainContext`、`QueryRunStore.record_diagnostic()`、`retrieve_schema_candidates()`。
- Produces: domain `controlled_sql_exploration`、artifact `exploration_result`、Tool `explore_business_data`、`execute_exploration_tool(call, context, *, question, versions) -> ExplorationExecution`。

`ExplorationExecution` 固定为：

```python
@dataclass(frozen=True)
class ExplorationExecution:
    domain_result: DomainResult
    plan: ValidatedQueryPlan | None
```

- [ ] **Step 1: 写固定节点链、Artifact 和 Tool 门禁测试。**

```python
EXPLORATION_NODES = (
    "select_schema", "authorize_scope", "assess_readiness", "compile_query",
    "validate_ast", "estimate_cost", "execute_readonly", "persist_artifact", "finalize")

class ExplorationGraphTests(unittest.TestCase):
    def test_graph_persists_one_safe_result_and_one_private_diagnostic(self):
        execution = run_ready_exploration()
        self.assertEqual(execution.domain_result.status, "ok")
        self.assertEqual([a.ref.type for a in execution.domain_result.artifacts],
                         ["exploration_result"])
        self.assertNotIn("SELECT", repr(execution.domain_result.model_payload))
        self.assertEqual(execution.store.diagnostic_count, 1)

    def test_fixed_tool_query_is_refused_before_sql(self):
        execution = run_exploration_for_paid_amount()
        self.assertEqual(execution.domain_result.status, "needs_input")
        self.assertEqual(execution.domain_result.error.code, "invalid_arguments")
        self.assertEqual(execution.repository.execute_count, 0)
```

- [ ] **Step 2: 运行图测试并确认 domain 未登记。**

Run: `uv run --locked python -m unittest tests.test_exploration.ExplorationGraphTests -v`

Expected: FAIL with unknown domain/artifact。

- [ ] **Step 3: 编写 020 迁移。**

迁移必须完整重建而非移除白名单：

- `bi.query_runs.domain` 增加 `controlled_sql_exploration`；
- `bi.query_artifacts.artifact_type` 增加 `exploration_result`；
- termination reason 增加 `fixed_tool_available/schema_ambiguous/sql_policy_rejected/query_cost_exceeded`；
- 保留 014/015 现有所有 domain、artifact 和 reason；同时保留独立泳道可能已登记的 `isolated_analysis` / `analysis_result`，使 020 在 022 已应用后执行也不会缩窄约束；
- `bi.query_diagnostics` 不新增 reporting view、不授权 `bi_reader`；
- 幂等执行两次通过。

- [ ] **Step 4: 登记 runtime 契约。**

`domain_registry.py` 登记上面九个节点和 `exploration_result`。`runtime.models` 为 `exploration_result` 增加严格 payload validator：字段仅 `template_version/catalog_version/statement_fingerprint/columns/rows/basis/coverage/diagnostics/limitations`；行数<=500，列 ref 与每行 key 一致，字符串不匹配真实 ID forbidden set。Memory/Postgres Store 都复用该 validator。

- [ ] **Step 5: 实现固定状态图。**

`graph.py` 逐节点执行：检索 → 固定 Tool eligibility → 服务端授权 → `assess_readiness` → 编译 → AST → EXPLAIN → 只读执行 → 投影 → 先 `record_diagnostic` 后 `save_artifact` → finalize。任何 capability/coverage/quality 失败在 compile 前终止；deadline 全程使用 `context.deadline`。diagnostic 写失败映射 `persistence_failed`，清空待发布结果。

- [ ] **Step 6: 实现动态 Tool schema 和适配器。**

`exploration_request_schema(selection)` 只把 selection 中的 entity/metric/field ref 作为 JSON Schema enum；不含 `question/sql/shop_id/allowed_shop_ids`。`execute_exploration_tool()` 从当前用户问题注入 question，重新检索并验证模型 ref 是服务端 selection 子集，再运行图。

- [ ] **Step 7: 接入主 Agent 且保持固定 Tool 优先。**

`agent.py` 在 feature enabled 时为当前问题获取 selection；仅当 `fixed_tool_for(...) is None`、无 missing concept 且不需澄清时追加 `explore_business_data` schema。处理调用时构造一次 `DomainContext`；不逐店循环，不把 SQL 放进 tool message。feature off 时 `_tool_schemas()` snapshot 必须逐字不变。

- [ ] **Step 8: 运行迁移、runtime、Agent 与图测试。**

```powershell
psql "$env:BI_TEST_ADMIN_DSN" -v ON_ERROR_STOP=1 -f sql/020_controlled_sql_exploration.sql
psql "$env:BI_TEST_ADMIN_DSN" -v ON_ERROR_STOP=1 -f sql/020_controlled_sql_exploration.sql
uv run --locked --env-file ../.env.test python -m unittest tests.test_exploration tests.test_runtime tests.test_runtime_db tests.test_core -v
```

Expected: PASS，迁移幂等，0 skip；模型/Artifact 载荷中没有 SQL 或真实 ID。

- [ ] **Step 9: 提交运行接入。**

```powershell
git add backend/sql/020_controlled_sql_exploration.sql backend/bi_agent/exploration backend/bi_agent/runtime backend/bi_agent/agent.py backend/tests/test_exploration.py backend/tests/test_runtime.py backend/tests/test_runtime_db.py backend/tests/test_core.py
git commit -m "feat: run controlled sql explorations"
```

---

### Task 6: Full Acceptance and Operating Documentation

**Files:**
- Modify: `README.md`
- Modify: `docs/runbook.md`
- Modify: `docs/metrics.md`
- Create: `docs/superpowers/research/2026-09-14-controlled-sql-acceptance.md`
- Modify: `docs/superpowers/plans/2026-09-11-data-and-query-closure.md`

**Interfaces:**
- Consumes: 完整探索领域。
- Produces: 安全攻击、权限、数值、feature-off 与性能验收记录。

- [ ] **Step 1: 建立冻结验收矩阵。**

在 `test_exploration.py` 增加至少 10 个允许问题和 20 个攻击问题的汇总 runner。允许问题人工 SQL 只在测试 fixture 中运行，Decimal/分组/空值逐项比较；至少覆盖 product cost coverage、ERP document normalization 状态计数、listing snapshot 时效分组和库存 snapshot 完整性计数。任何现有固定 Tool 可回答的输入期望 `fixed_tool_available`。

- [ ] **Step 2: 运行完整回归。**

```powershell
Set-Location backend
uv run --locked --env-file ../.env.test python -m unittest discover -s tests -t .
uv run --locked --env-file ../.env.test python -m tests.acceptance --offline
Set-Location ../frontend
npm test -- --run
npm run build
```

Expected: 零失败、DB-enabled 0 skip、26/26；现有四工作流的工具调用数与结果不变。

- [ ] **Step 3: 做 feature-off 与权限对比。**

在 flag off/缺省两种配置下运行 tool schema snapshot、Q01、Q15 和四工作流路由，输出逐字相同；在 flag on 下使用 `bi_app` 验证允许 query 成功，`DELETE bi.orders`、`SELECT bi.orders` 和 `pg_read_file` 分别被 validator 或数据库拒绝。

- [ ] **Step 4: 更新文档。**

README 只把能力称为“受控聚合探索”；runbook 写 020 部署顺序、两个 feature flag、schema mismatch/AST/cost/timeout 的诊断和回滚；metrics 写允许 view/aggregate/JOIN 清单与 fixed-tool-first 规则。不得出现“任意 SQL”或“通用 Text2SQL”。

- [ ] **Step 5: 写日期化验收记录。**

记录 HEAD、sqlglot 锁定版本、catalog version、10 个允许问题结果、20 个攻击拒绝码、EXPLAIN 阈值、最大结果字节、DB 角色、完整测试和 feature-off diff。真实模型未跑则明确 `未执行`。

- [ ] **Step 6: 更新后置索引并提交。**

```powershell
git add README.md docs/runbook.md docs/metrics.md docs/superpowers/research/2026-09-14-controlled-sql-acceptance.md docs/superpowers/plans/2026-09-11-data-and-query-closure.md
git commit -m "docs: verify controlled sql exploration"
```

## Final Verification

- [ ] 20/20 攻击在 SQL 执行前拒绝；数据库角色另行拒绝底表读写。
- [ ] 允许问题与人工基准 Decimal、分组和空值逐项一致。
- [ ] 模型消息、事件、Artifact、普通日志不含 `SELECT`、真实店铺 ID 或 DSN。
- [ ] SQL 原文只存在于 `bi.query_diagnostics`，且 `bi_reader` 无权读取。
- [ ] `CONTROLLED_SQL_ENABLED=false` 时没有新增 Tool、数据库读取或用户可见差异。
- [ ] `git diff --check` 无输出，提交只含本计划文件。
