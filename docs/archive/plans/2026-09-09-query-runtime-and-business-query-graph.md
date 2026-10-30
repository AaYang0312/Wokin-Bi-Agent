# 查询运行状态与确定性经营查询子图 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 建立可持久化、可审计的查询运行状态，并让所有 `query_business` 调用通过确定性 Python 子图执行，同时保持现有指标结果、Agent 限额和前端 SSE 契约不变。

**Architecture:** 阶段 1 新增 Pydantic 领域契约、内存 Store、PostgreSQL Store，以及 `query_runs/query_run_events/query_artifacts` 三张表，但不切换生产路径。阶段 2 将现有经营查询参数处理和执行迁入固定状态图；主 Agent 通过 Tool Adapter 调用子图，`metrics.query_business()` 仍是唯一指标执行入口。

**Tech Stack:** Python 3.11、Pydantic 2、PostgreSQL、psycopg 3、FastAPI、标准库 `unittest`；不增加运行时依赖。

**Spec:** [2026-09-09-query-runtime-and-business-query-graph-design.md](../specs/2026-09-09-query-runtime-and-business-query-graph-design.md)

## Global Constraints

- 开始执行每个任务前先运行 `git status --short` 和 `git log -3 --oneline`；不得覆盖其他 Agent 或用户的未提交修改。
- 当前基线包含 `002_kuaimai_mapping_repair.sql`；本项目新增迁移必须命名为 `backend/sql/004_query_runtime.sql`。
- 不修改 `backend/bi_agent/sync.py`、`backend/sql/001_init.sql`、`backend/sql/002_kuaimai_mapping_repair.sql`，也不改变 `backend/bi_agent/metrics.py` 的 SQL、指标口径或覆盖语义。
- 阶段 1 完成前不得切换生产查询路径；阶段 1 和阶段 2 分别执行完整回归并形成独立提交检查点。
- 不引入 LangGraph、自由 SQL、Schema 检索、向量库、Redis、队列或新服务。
- 原始问题只保留在 `bi.app_messages`；运行状态和事件不得保存模型隐藏推理、数据库错误原文、凭证、真实 ERP 店铺 ID 或商品 ID。
- `query_business` 仍受最多 4 次工具调用、一次模型参数纠正、最多 5 次模型回合和 30 秒总预算限制。
- `ToolResult`、固定指标数值、模型安全投影、公共安全投影、API 路径及 SSE 事件类型和顺序保持兼容。
- PostgreSQL 集成测试只允许本机、数据库名以 `_test` 结尾的 `BI_TEST_ADMIN_DSN`；没有测试 DSN 时明确 skip，不把 skip 报告为通过。
- 使用 TDD：每个行为先写失败测试、确认失败原因、写最小实现、确认通过，再提交；每次只暂存任务列出的文件。

---

## 文件结构与职责

| 文件 | 操作 | 单一职责 |
| --- | --- | --- |
| `backend/bi_agent/runtime/__init__.py` | 新建 | 导出稳定运行时类型 |
| `backend/bi_agent/runtime/models.py` | 新建 | DomainResult、错误、Artifact、Store 命令模型与协议 |
| `backend/bi_agent/runtime/memory.py` | 新建 | 单元测试使用的确定性内存 Store |
| `backend/bi_agent/runtime/repository.py` | 新建 | PostgreSQL Store 与持久化异常 |
| `backend/sql/004_query_runtime.sql` | 新建 | 三张运行表、约束、索引和 `bi_app` 最小权限 |
| `backend/bi_agent/business_query/__init__.py` | 新建 | 导出经营查询 Tool 入口 |
| `backend/bi_agent/business_query/state.py` | 新建 | 查询节点、持久化状态与仅内存 Context/Runtime |
| `backend/bi_agent/business_query/graph.py` | 新建 | 合法状态转移和固定调度 |
| `backend/bi_agent/business_query/nodes.py` | 新建 | 参数解析、校验、授权、执行、分类、Artifact 与终结节点 |
| `backend/bi_agent/business_query/tool.py` | 新建 | 安全投影、DomainResult 与旧 Agent 之间的 Adapter |
| `backend/bi_agent/agent.py` | 修改 | 移除经营查询内部实现，改为调用 Tool Adapter |
| `backend/bi_agent/chats.py` | 修改 | 将已保存用户消息 ID 传给调用方；现有返回类型不变 |
| `backend/tests/test_runtime.py` | 新建 | 通用契约与内存 Store 单元测试 |
| `backend/tests/test_runtime_db.py` | 新建 | 迁移、权限、PostgreSQL Store 和级联测试 |
| `backend/tests/test_business_query_graph.py` | 新建 | 节点顺序、错误分流、安全投影和 Artifact 测试 |
| `backend/tests/test_core.py` | 修改 | Agent 路由与兼容回归测试 |
| `backend/tests/test_api.py` | 修改 | SSE 不变和生产路径运行记录集成测试 |
| `backend/tests/acceptance.py` | 修改 | 直接调用 Agent 时注入内存 Store，保持 20 题接口稳定 |
| `README.md` | 修改 | 增加迁移顺序和测试模块 |
| `docs/runbook.md` | 修改 | 增加运行表迁移、诊断查询和故障说明 |

---

## 阶段 1：结构化运行状态

### Task 1: 运行时契约与内存 Store

**Files:**
- Create: `backend/bi_agent/runtime/__init__.py`
- Create: `backend/bi_agent/runtime/models.py`
- Create: `backend/bi_agent/runtime/memory.py`
- Create: `backend/tests/test_runtime.py`

**Interfaces:**
- Produces: `DomainStatus`、`RunStatus`、`RecoveryAction`、`RunEventType`、`ErrorEnvelope`、`ArtifactRef`、`DomainArtifact`、`DomainResult`、`TurnContext`。
- Produces: `NewQueryRun`、`RunTransition`、`NewArtifact`、`RunCompletion`、`QueryRunStore`。
- Produces: `MemoryQueryRunStore`，其可观察行为必须与 Task 3 的 PostgreSQL Store 一致。

- [ ] **Step 1: 写契约拒绝额外字段和 DomainResult 组合测试。**

在 `backend/tests/test_runtime.py` 使用以下 imports 和测试骨架：

```python
import unittest
from uuid import uuid4

from pydantic import ValidationError

from bi_agent.runtime.models import (
    ArtifactRef,
    DomainArtifact,
    DomainResult,
    DomainStatus,
    ErrorEnvelope,
    RecoveryAction,
)


class RuntimeModelTests(unittest.TestCase):
    def test_error_envelope_forbids_extra_fields(self):
        with self.assertRaises(ValidationError):
            ErrorEnvelope(
                code="invalid_parameters",
                stage="validate_parameters",
                retryable=False,
                recovery=RecoveryAction.CORRECT_PARAMETERS,
                public_message="查询参数无效",
                secret="database detail",
            )

    def test_domain_result_keeps_ref_and_public_projection(self):
        artifact_id = uuid4()
        result = DomainResult(
            run_id=uuid4(),
            status=DomainStatus.SUCCESS,
            model_payload={"status": "ok", "data": [{"shop_id": "shop_1"}]},
            artifacts=[DomainArtifact(
                ref=ArtifactRef(id=artifact_id, type="metric_result"),
                public_payload={"status": "ok", "data": [{"shop_id": "店铺1"}]},
            )],
        )
        self.assertEqual(result.artifacts[0].ref.id, artifact_id)
        self.assertEqual(result.artifacts[0].public_payload["data"][0]["shop_id"], "店铺1")
```

- [ ] **Step 2: 从 `backend/` 运行契约测试并确认因模块不存在而失败。**

Run: `uv run python -m unittest tests.test_runtime.RuntimeModelTests -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'bi_agent.runtime'`。

- [ ] **Step 3: 在 `runtime/models.py` 实现明确枚举与 Pydantic 模型。**

使用 Python 3.11 `StrEnum`，所有边界模型设置 `ConfigDict(extra="forbid")`。模型字段固定为：

```python
class DomainStatus(StrEnum):
    SUCCESS = "success"
    NEEDS_INPUT = "needs_input"
    MISSING_DATA = "missing_data"
    PARTIAL = "partial"
    FAILED = "failed"


class RunStatus(StrEnum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    NEEDS_INPUT = "needs_input"
    MISSING_DATA = "missing_data"
    PARTIAL = "partial"
    FAILED = "failed"


class RecoveryAction(StrEnum):
    NONE = "none"
    ASK_USER = "ask_user"
    CORRECT_PARAMETERS = "correct_parameters"
    RETRY_LATER = "retry_later"


class RunEventType(StrEnum):
    ENTERED = "entered"
    COMPLETED = "completed"
    FAILED = "failed"
    TRANSITIONED = "transitioned"
```

`ErrorEnvelope`、`ArtifactRef`、`DomainArtifact`、`DomainResult` 精确采用 spec 第 5.1 节字段。Store 命令模型采用以下字段：

```python
class NewQueryRun(BaseModel):
    model_config = ConfigDict(extra="forbid")
    chat_id: UUID
    user_message_id: UUID
    subject_id: str
    tool_call_id: str
    domain: Literal["business_query"] = "business_query"
    attempt_no: int = Field(ge=1)
    normalized_request: dict[str, object] = Field(default_factory=dict)
    state: dict[str, object] = Field(default_factory=dict)


class RunTransition(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=0)
    node: str
    event_type: RunEventType = RunEventType.TRANSITIONED
    status: RunStatus
    state: dict[str, object]
    payload: dict[str, object] = Field(default_factory=dict)
    error_code: str | None = None


class NewArtifact(BaseModel):
    model_config = ConfigDict(extra="forbid")
    artifact_type: Literal["metric_result"] = "metric_result"
    payload: dict[str, object]
    data_as_of: datetime | None = None
    coverage: dict[str, object] | None = None


class RunCompletion(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=0)
    node: str
    status: RunStatus
    state: dict[str, object]
    payload: dict[str, object] = Field(default_factory=dict)
    error_code: str | None = None


class TurnContext(BaseModel):
    model_config = ConfigDict(extra="forbid")
    chat_id: UUID
    user_message_id: UUID
    subject_id: str
```

`QueryRunStore` 使用 `typing.Protocol` 暴露 spec 第 5.3 节四个方法，`transition()` 与 `finish()` 返回 `None`，`save_artifact()` 返回 `ArtifactRef`。

- [ ] **Step 4: 运行模型测试并确认通过。**

Run: `uv run python -m unittest tests.test_runtime.RuntimeModelTests -v`
Expected: PASS。

- [ ] **Step 5: 写内存 Store 的创建、revision、Artifact 和终结测试。**

```python
class MemoryQueryRunStoreTests(unittest.TestCase):
    def setUp(self):
        self.store = MemoryQueryRunStore(forbidden_values={"S1", "ERP-P-9"})
        self.record = NewQueryRun(
            chat_id=uuid4(), user_message_id=uuid4(), subject_id="u1",
            tool_call_id="call_1", attempt_no=1,
            normalized_request={"shop_aliases": ["shop_1"]},
            state={"node": "received"},
        )

    def test_transition_is_revision_checked_and_appends_one_event(self):
        run_id = self.store.create_run(self.record)
        transition = RunTransition(
            expected_revision=0, node="resolve_parameters",
            status=RunStatus.RUNNING,
            state={"node": "resolve_parameters", "revision": 1},
        )
        self.store.transition(run_id, transition)
        self.assertEqual(self.store.runs[run_id]["revision"], 1)
        self.assertEqual(self.store.events[run_id][0]["revision"], 1)
        with self.assertRaises(StaleRunRevision):
            self.store.transition(run_id, transition)

    def test_save_artifact_returns_reference_and_finish_is_terminal(self):
        run_id = self.store.create_run(self.record)
        ref = self.store.save_artifact(run_id, NewArtifact(
            payload={"status": "ok", "data": []},
            coverage={"status": "complete"},
        ))
        self.assertEqual(ref.type, "metric_result")
        self.store.finish(run_id, RunCompletion(
            expected_revision=0, node="finalize", status=RunStatus.SUCCEEDED,
            state={"node": "finalize", "revision": 1},
        ))
        self.assertEqual(self.store.runs[run_id]["status"], "succeeded")
        self.assertIsNotNone(self.store.runs[run_id]["completed_at"])
```

- [ ] **Step 6: 运行内存 Store 测试并确认因实现缺失而失败。**

Run: `uv run python -m unittest tests.test_runtime.MemoryQueryRunStoreTests -v`
Expected: FAIL because `MemoryQueryRunStore` or `StaleRunRevision` is missing。

- [ ] **Step 7: 实现内存 Store。**

在 `runtime/models.py` 定义 `RunContextNotFound`、`RunNotFound`、`StaleRunRevision` 和 `ArtifactPersistenceError`，异常消息分别固定为同名 snake_case 错误码。在 `runtime/memory.py` 使用三个字典保存运行、事件和 Artifact；每次 `transition/finish` 检查 `expected_revision`，成功后 revision 加 1。`finish` 只接受非 `RUNNING` 状态并写入带时区的 `completed_at`；`save_artifact` 必须先确认 run 存在。

- [ ] **Step 8: 运行整个新单元测试模块。**

Run: `uv run python -m unittest tests.test_runtime -v`
Expected: PASS。

- [ ] **Step 9: 提交阶段 1 的契约切片。**

```bash
git add backend/bi_agent/runtime backend/tests/test_runtime.py
git commit -m "feat: add query runtime contracts"
```

### Task 2: 查询运行数据库迁移

**Files:**
- Create: `backend/sql/004_query_runtime.sql`
- Create: `backend/tests/test_runtime_db.py`

**Interfaces:**
- Consumes: Task 1 的状态字符串和 Artifact 类型。
- Produces: `bi.query_runs`、`bi.query_run_events`、`bi.query_artifacts`。
- Produces: `bi_app` 对运行表的最小写权限；不扩大经营事实表权限。

- [ ] **Step 1: 写数据库迁移结构、级联和权限测试。**

`backend/tests/test_runtime_db.py` 复用 `test_db.py` 的本机和 `_test` DSN 防护。测试自行建立管理员连接，并在每个用例结束时 rollback。关键断言：

```python
def test_runtime_tables_have_constraints_and_cascade_from_chat(self):
    chat_id, message_id = self._seed_user_message()
    run_id = uuid4()
    self.conn.execute(
        "INSERT INTO bi.query_runs "
        "(id, chat_id, user_message_id, subject_id, tool_call_id, attempt_no) "
        "VALUES (%s, %s, %s, 'u1', 'call_1', 1)",
        (run_id, chat_id, message_id),
    )
    self.conn.execute(
        "INSERT INTO bi.query_run_events "
        "(run_id, revision, node, event_type, status) "
        "VALUES (%s, 1, 'received', 'entered', 'running')", (run_id,),
    )
    self.conn.execute(
        "INSERT INTO bi.query_artifacts (id, run_id, artifact_type, payload) "
        "VALUES (%s, %s, 'metric_result', '{\"status\":\"ok\"}')",
        (uuid4(), run_id),
    )
    self.conn.execute("DELETE FROM bi.app_chats WHERE id=%s", (chat_id,))
    self.assertEqual(self.conn.execute(
        "SELECT count(*) FROM bi.query_runs WHERE id=%s", (run_id,)
    ).fetchone()[0], 0)


def test_app_role_can_manage_runtime_but_not_business_facts(self):
    chat_id, message_id = self._seed_user_message()
    self.conn.execute("SET LOCAL ROLE bi_app")
    self.conn.execute(
        "INSERT INTO bi.query_runs "
        "(id, chat_id, user_message_id, subject_id, tool_call_id, attempt_no) "
        "VALUES (%s, %s, %s, 'u1', 'call_1', 1)",
        (uuid4(), chat_id, message_id),
    )
    with self.assertRaises(psycopg.errors.InsufficientPrivilege):
        with self.conn.transaction():
            self.conn.execute("INSERT INTO bi.shops(shop_id) VALUES ('forbidden')")
```

另加一个测试读取 `004_query_runtime.sql` 并在管理员事务中执行两次，确认无 duplicate object 错误。

- [ ] **Step 2: 运行数据库测试并确认因运行表不存在而失败。**

Run from `backend/`: `uv run --env-file ../.env.test python -m unittest tests.test_runtime_db -v`
Expected: FAIL with `UndefinedTable` when `004_query_runtime.sql` 尚未应用。

- [ ] **Step 3: 创建幂等迁移。**

`004_query_runtime.sql` 必须使用 `CREATE TABLE IF NOT EXISTS` 和 `CREATE INDEX IF NOT EXISTS`，并包含以下约束：

```sql
CREATE TABLE IF NOT EXISTS bi.query_runs (
  id                 uuid PRIMARY KEY,
  chat_id            uuid NOT NULL REFERENCES bi.app_chats(id) ON DELETE CASCADE,
  user_message_id    uuid NOT NULL REFERENCES bi.app_messages(id) ON DELETE CASCADE,
  subject_id         text NOT NULL,
  tool_call_id       text NOT NULL,
  domain             text NOT NULL DEFAULT 'business_query'
                     CHECK (domain = 'business_query'),
  attempt_no         integer NOT NULL CHECK (attempt_no >= 1),
  status             text NOT NULL DEFAULT 'running'
                     CHECK (status IN ('running','succeeded','needs_input',
                                       'missing_data','partial','failed')),
  current_node       text NOT NULL DEFAULT 'received',
  revision           integer NOT NULL DEFAULT 0 CHECK (revision >= 0),
  normalized_request jsonb NOT NULL DEFAULT '{}',
  state              jsonb NOT NULL DEFAULT '{}',
  error_code         text,
  started_at         timestamptz NOT NULL DEFAULT now(),
  updated_at         timestamptz NOT NULL DEFAULT now(),
  completed_at       timestamptz,
  UNIQUE (user_message_id, domain, attempt_no)
);

CREATE TABLE IF NOT EXISTS bi.query_run_events (
  run_id      uuid NOT NULL REFERENCES bi.query_runs(id) ON DELETE CASCADE,
  revision    integer NOT NULL CHECK (revision >= 1),
  node        text NOT NULL,
  event_type  text NOT NULL CHECK (event_type IN
              ('entered','completed','failed','transitioned')),
  status      text NOT NULL CHECK (status IN
              ('running','succeeded','needs_input','missing_data','partial','failed')),
  payload     jsonb NOT NULL DEFAULT '{}',
  created_at  timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (run_id, revision)
);

CREATE TABLE IF NOT EXISTS bi.query_artifacts (
  id             uuid PRIMARY KEY,
  run_id         uuid NOT NULL REFERENCES bi.query_runs(id) ON DELETE CASCADE,
  artifact_type  text NOT NULL CHECK (artifact_type = 'metric_result'),
  payload        jsonb NOT NULL,
  data_as_of     timestamptz,
  coverage       jsonb,
  created_at     timestamptz NOT NULL DEFAULT now()
);
```

为 `query_runs(chat_id, started_at DESC)`、`query_runs(user_message_id, attempt_no)` 和 `query_artifacts(run_id)` 建索引。权限仅授予：

```sql
GRANT SELECT, INSERT, UPDATE ON bi.query_runs TO bi_app;
GRANT SELECT, INSERT ON bi.query_run_events, bi.query_artifacts TO bi_app;
```

不要给 `bi_app` 新增经营事实表权限，也不要给运行事件直接 UPDATE/DELETE 权限。

- [ ] **Step 4: 在本地测试库依次应用迁移并运行测试。**

Run from repository root:

```bash
psql "$BI_TEST_ADMIN_DSN" -v ON_ERROR_STOP=1 -f backend/sql/004_query_runtime.sql
```

Run from `backend/`: `uv run --env-file ../.env.test python -m unittest tests.test_runtime_db -v`
Expected: PASS；无 DSN 时明确 SKIP。

- [ ] **Step 5: 提交迁移切片。**

```bash
git add backend/sql/004_query_runtime.sql backend/tests/test_runtime_db.py
git commit -m "feat: add query runtime persistence schema"
```

### Task 3: PostgreSQL QueryRunStore

**Files:**
- Create: `backend/bi_agent/runtime/repository.py`
- Modify: `backend/bi_agent/runtime/__init__.py`
- Modify: `backend/tests/test_runtime_db.py`

**Interfaces:**
- Consumes: Task 1 的 `QueryRunStore` 命令模型与 `RunContextNotFound`、`RunNotFound`、`StaleRunRevision`、`ArtifactPersistenceError` 脱敏异常。
- Produces: `PostgresQueryRunStore(conn, *, forbidden_values: Collection[str])`；`forbidden_values` 必须非空。

- [ ] **Step 1: 写 Store 创建运行时的归属验证测试。**

```python
def test_store_creates_run_only_for_matching_user_message(self):
    chat_id, message_id = self._seed_user_message(subject="u1")
    store = PostgresQueryRunStore(self.conn, forbidden_values={"S1", "ERP-P-9"})
    run_id = store.create_run(NewQueryRun(
        chat_id=chat_id, user_message_id=message_id, subject_id="u1",
        tool_call_id="call_1", attempt_no=1,
        normalized_request={"shop_aliases": ["shop_1"]},
        state={"node": "received"},
    ))
    row = self.conn.execute(
        "SELECT subject_id, revision, status FROM bi.query_runs WHERE id=%s",
        (run_id,),
    ).fetchone()
    self.assertEqual(row, ("u1", 0, "running"))
    with self.assertRaises(RunContextNotFound):
        store.create_run(NewQueryRun(
            chat_id=chat_id, user_message_id=message_id, subject_id="u2",
            tool_call_id="call_2", attempt_no=2,
        ))
```

- [ ] **Step 2: 运行单测并确认因 PostgreSQL Store 尚未实现而失败。**

Run: `uv run --env-file ../.env.test python -m unittest tests.test_runtime_db.RuntimeStoreDatabaseTests.test_store_creates_run_only_for_matching_user_message -v`
Expected: FAIL with import error。

- [ ] **Step 3: 实现 `create_run()`。**

使用以下单条 `INSERT SELECT` 联结 `bi.app_messages` 与 `bi.app_chats`，同时验证 message ID、chat ID、`role='user'` 和 subject。没有返回行时抛 `RunContextNotFound`，异常文本只包含固定错误码。

```sql
INSERT INTO bi.query_runs (
  id, chat_id, user_message_id, subject_id, tool_call_id, domain,
  attempt_no, normalized_request, state
)
SELECT %s, c.id, m.id, c.subject_id, %s, %s, %s, %s, %s
FROM bi.app_messages AS m
JOIN bi.app_chats AS c ON c.id = m.chat_id
WHERE m.id = %s AND c.id = %s AND m.role = 'user' AND c.subject_id = %s
RETURNING id
```

- [ ] **Step 4: 写原子 transition 与 stale revision 测试。**

测试第一次 `expected_revision=0` 后数据库 revision 和事件均为 1；再次使用 0 抛 `StaleRunRevision`，且事件数量仍为 1。

- [ ] **Step 5: 实现 `transition()` 和 `finish()` 的原子事务。**

两者都使用 `with self.conn.transaction()`：先执行带 `WHERE id=%s AND revision=%s` 的 UPDATE，并 `RETURNING revision`；无返回行时区分 run 不存在与 stale revision；随后插入相同 revision 的事件。`finish()` 同时设置 `completed_at=now()`，并拒绝 `RunStatus.RUNNING`。

- [ ] **Step 6: 写 Artifact 安全投影持久化测试。**

保存 `{"data":[{"shop_id":"店铺1","product_id":"商品A"}]}`，断言数据库 payload 完全一致；测试只把公共投影传给 Store，不允许 Store 接收 `ToolResult` 实例。

- [ ] **Step 7: 实现 `save_artifact()`。**

使用 `Jsonb(artifact.payload)`、`Jsonb(artifact.coverage)` 和新 UUID 插入；run 不存在时将外键异常转换为 `RunNotFound`，不把 psycopg 原文带出 repository。

- [ ] **Step 8: 运行阶段 1 的所有测试。**

Run from `backend/`:

```bash
uv run python -m unittest tests.test_runtime tests.test_core -v
uv run --env-file ../.env.test python -m unittest tests.test_runtime_db tests.test_db tests.test_api -v
```

Expected: 全部执行项 PASS；未配置 DSN 的数据库项显示 SKIP。

- [ ] **Step 9: 提交 PostgreSQL Store，并形成阶段 1 检查点。**

```bash
git add backend/bi_agent/runtime backend/tests/test_runtime_db.py
git commit -m "feat: persist structured query runs"
```

---

## 阶段 2：确定性经营查询子图

### Task 4: 查询状态、临时运行上下文与合法转移

**Files:**
- Create: `backend/bi_agent/business_query/__init__.py`
- Create: `backend/bi_agent/business_query/state.py`
- Create: `backend/bi_agent/business_query/graph.py`
- Create: `backend/tests/test_business_query_graph.py`

**Interfaces:**
- Consumes: Task 1 的 `RunStatus`、`ErrorEnvelope`、`ArtifactRef`。
- Produces: `BusinessQueryNode`、`BusinessQueryInput`、`BusinessQueryContext`、`BusinessQueryState`、`BusinessQueryRuntime`、`BusinessQueryExecution`。
- Produces: `transition_state(state, next_node) -> BusinessQueryState`。

- [ ] **Step 1: 写状态序列和非法跳转测试。**

```python
class BusinessQueryTransitionTests(unittest.TestCase):
    def test_normal_path_is_fixed(self):
        state = BusinessQueryState(run_id=uuid4())
        for node in (
            BusinessQueryNode.RESOLVE_PARAMETERS,
            BusinessQueryNode.VALIDATE_PARAMETERS,
            BusinessQueryNode.AUTHORIZE_SCOPE,
            BusinessQueryNode.EXECUTE_FIXED_QUERY,
            BusinessQueryNode.CLASSIFY_RESULT,
            BusinessQueryNode.PERSIST_ARTIFACT,
            BusinessQueryNode.FINALIZE,
        ):
            state = transition_state(state, node)
        self.assertEqual(state.node, BusinessQueryNode.FINALIZE)

    def test_execute_cannot_skip_validation(self):
        state = BusinessQueryState(run_id=uuid4())
        with self.assertRaises(InvalidBusinessQueryTransition):
            transition_state(state, BusinessQueryNode.EXECUTE_FIXED_QUERY)
```

- [ ] **Step 2: 运行测试并确认因模块不存在而失败。**

Run: `uv run python -m unittest tests.test_business_query_graph.BusinessQueryTransitionTests -v`
Expected: FAIL with module import error。

- [ ] **Step 3: 实现状态类型。**

`BusinessQueryNode` 定义：`received`、`resolve_parameters`、`validate_parameters`、`authorize_scope`、`execute_fixed_query`、`classify_result`、`persist_artifact`、`finalize`。`BusinessQueryInput` 精确定义 `tool_call_id: str`、`arguments: dict[str, object] | None` 和 `arguments_error: str | None`，使 JSON 解析失败也能建立可审计运行。`BusinessQueryState` 只允许安全字段：

```python
class BusinessQueryState(BaseModel):
    model_config = ConfigDict(extra="forbid")
    run_id: UUID
    node: BusinessQueryNode = BusinessQueryNode.RECEIVED
    status: RunStatus = RunStatus.RUNNING
    revision: int = Field(default=0, ge=0)
    normalized_request: dict[str, object] = Field(default_factory=dict)
    problems: list[ProblemCode] = Field(default_factory=list)
    tool_status: str | None = None
    target_status: DomainStatus | None = None
    coverage: Coverage | None = None
    data_as_of: datetime | None = None
    limitations: list[str] = Field(default_factory=list)
    artifact_refs: list[ArtifactRef] = Field(default_factory=list)
    error: ErrorEnvelope | None = None
```

`BusinessQueryContext` 包含真实授权信息，但不得嵌入 State：`chat_id`、`user_message_id`、`subject_id`、`question`、`previous_filters`、`shop_aliases`、`allowed_shop_ids`、`now`、`deadline`、`attempt_no`。`BusinessQueryRuntime` 使用 `dataclass` 保存 `state/context/resolved_args/request/result`；后四项不被序列化到数据库。`BusinessQueryExecution` 使用 `dataclass` 保存 `domain_result: DomainResult`、`tool_result: ToolResult | None` 和只在进程内使用的 `session_filters: dict[str, object]`。

- [ ] **Step 4: 实现白名单转移表。**

```python
_NEXT_NODE = {
    BusinessQueryNode.RECEIVED: BusinessQueryNode.RESOLVE_PARAMETERS,
    BusinessQueryNode.RESOLVE_PARAMETERS: BusinessQueryNode.VALIDATE_PARAMETERS,
    BusinessQueryNode.VALIDATE_PARAMETERS: BusinessQueryNode.AUTHORIZE_SCOPE,
    BusinessQueryNode.AUTHORIZE_SCOPE: BusinessQueryNode.EXECUTE_FIXED_QUERY,
    BusinessQueryNode.EXECUTE_FIXED_QUERY: BusinessQueryNode.CLASSIFY_RESULT,
    BusinessQueryNode.CLASSIFY_RESULT: BusinessQueryNode.PERSIST_ARTIFACT,
    BusinessQueryNode.PERSIST_ARTIFACT: BusinessQueryNode.FINALIZE,
}
```

`transition_state` 只接受表中唯一后继，返回 `model_copy(update={"node": next_node})`；任何跳转抛固定文本 `invalid_transition`。

- [ ] **Step 5: 运行状态测试并提交。**

Run: `uv run python -m unittest tests.test_business_query_graph.BusinessQueryTransitionTests -v`
Expected: PASS。

```bash
git add backend/bi_agent/business_query backend/tests/test_business_query_graph.py
git commit -m "feat: define deterministic business query graph"
```

### Task 5: 参数解析、校验与授权节点

**Files:**
- Create: `backend/bi_agent/business_query/nodes.py`
- Modify: `backend/tests/test_business_query_graph.py`

**Interfaces:**
- Consumes: `QueryRequest`、`resolve_period()` 和 Task 4 的 Runtime。
- Produces: `resolve_parameters(runtime)`、`validate_parameters(runtime)`、`authorize_scope(runtime)`。
- Guarantee: 只有通过授权节点后 Runtime 才能进入执行节点。

- [ ] **Step 1: 写继承筛选、日期补齐和匿名持久化测试。**

构造 Context：上一轮真实筛选为 `shop_ids=["S1"]`、`metrics=["paid_amount"]`，匿名映射为 `{"S1":"shop_1"}`，问题为“那上个月呢”。调用解析节点后断言：

```python
self.assertEqual(runtime.resolved_args["shop_ids"], ["S1"])
self.assertEqual(runtime.resolved_args["metrics"], ["paid_amount"])
self.assertEqual(runtime.resolved_args["start"], "2026-08-01")
self.assertEqual(runtime.resolved_args["end"], "2026-09-01")
self.assertEqual(runtime.state.normalized_request["shop_aliases"], ["shop_1"])
self.assertNotIn("S1", json.dumps(runtime.state.model_dump(mode="json")))
```

- [ ] **Step 2: 写缺店铺、Pydantic 失败和未授权测试。**

分别断言：缺店铺产生 `missing_parameters/NEEDS_INPUT`；非法日期产生 `invalid_parameters/NEEDS_INPUT`；`shop_2` 或注入字符串产生 `forbidden/FAILED`。`problems` 只保存 Task 1 允许的 `ProblemCode`，不得保存 Pydantic 原始错误文本。给 `metrics.query_business` 设置 Mock，并断言这三类路径都没有调用它。

- [ ] **Step 3: 运行节点测试并确认失败。**

Run: `uv run python -m unittest tests.test_business_query_graph.BusinessQueryInputNodeTests -v`
Expected: FAIL because node functions are missing。

- [ ] **Step 4: 实现参数解析。**

将现有 `_handle_query_business()` 中的日期、上一轮筛选和默认指标逻辑迁入 `resolve_parameters`。真实店铺 ID 只写入 `runtime.resolved_args`。持久化请求使用 `shop_aliases`，未知值统一记为 `invalid_shop`，不得把模型提供的原值回显进 state 或问题列表。

- [ ] **Step 5: 实现参数校验。**

用 `QueryRequest.model_validate(runtime.resolved_args)`；成功写入 `runtime.request`。失败将 Pydantic 的 `loc/type` 确定性映射为 Task 1 的安全问题码：`start/end -> invalid_date_range`、`metrics -> invalid_metric`、`group_by -> invalid_group_by`、`compare -> invalid_compare`、`top_n -> invalid_top_n`、`shop_ids -> invalid_shop`，其余为 `invalid_parameters`。不得持久化 Pydantic 原始错误文本。生成：

```python
ErrorEnvelope(
    code="invalid_parameters",
    stage="validate_parameters",
    retryable=False,
    recovery=RecoveryAction.CORRECT_PARAMETERS,
    public_message="查询参数无效，请调整后重试。",
    problems=[problem_code],
)
```

- [ ] **Step 6: 实现授权节点。**

比较 `set(runtime.request.shop_ids)` 与 `context.allowed_shop_ids`。失败不回显店铺值，返回 `forbidden`；成功才允许状态机进入 `EXECUTE_FIXED_QUERY`。保留 `metrics.query_business()` 内的二次授权检查。

- [ ] **Step 7: 运行输入节点测试并提交。**

Run: `uv run python -m unittest tests.test_business_query_graph.BusinessQueryInputNodeTests -v`
Expected: PASS。

```bash
git add backend/bi_agent/business_query/nodes.py backend/tests/test_business_query_graph.py
git commit -m "feat: validate and authorize business queries"
```

### Task 6: 固定查询执行、结果分类、Artifact 与完整图调度

**Files:**
- Modify: `backend/bi_agent/business_query/nodes.py`
- Modify: `backend/bi_agent/business_query/graph.py`
- Create: `backend/bi_agent/business_query/tool.py`
- Modify: `backend/bi_agent/agent.py`
- Modify: `backend/tests/test_business_query_graph.py`

**Interfaces:**
- Produces: `execute_fixed_query()`、`classify_result()`、`persist_artifact()`、`finalize_run()`。
- Produces: `_execute_business_query_graph(conn, store, tool_input, context) -> BusinessQueryExecution` 的内部图执行能力；公开入口在 Task 7 完成。
- Calls: `metrics.query_business(conn, request, allowed_shop_ids, now, deadline)` exactly once on an authorized success path。

- [ ] **Step 1: 写完整成功节点顺序测试。**

使用 `MemoryQueryRunStore(forbidden_values={"S1", "ERP-P-9"})` 和已知 `ToolResult(status="ok", data=[{"paid_amount":"1000"}], coverage=complete)`；patch `bi_agent.metrics.query_business`。断言调用一次、事件 node 顺序与设计一致、运行终态为 `succeeded`、Artifact 数为 1。

- [ ] **Step 2: 写 `missing_data`、真实零、partial 和查询失败分类测试。**

最少覆盖：

```python
cases = [
    (ToolResult(status="ok", data=[{"paid_amount": "0"}],
                coverage=Coverage(status="complete", start=START, end=END)),
     DomainStatus.SUCCESS),
    (ToolResult(status="missing_data", data=[],
                coverage=Coverage(status="missing", start=START, end=END)),
     DomainStatus.MISSING_DATA),
    (ToolResult(status="missing_data", data=[],
                coverage=Coverage(status="partial", start=START, end=END,
                                  gaps=["2026-09-05~2026-09-08"])),
     DomainStatus.MISSING_DATA),
    (ToolResult(status="unavailable", data=[],
                coverage=Coverage(status="missing", start=None, end=None)),
     DomainStatus.FAILED),
]
```

`PARTIAL` 仅用于未来存在可展示部分结果且覆盖为 partial 的合法 `ToolResult`；当前 `metrics.py` 对部分覆盖返回 `missing_data`，测试不得伪造系统当前不会生成的部分汇总。

- [ ] **Step 3: 写 Artifact Store 失败覆盖原分类的测试。**

创建继承 `MemoryQueryRunStore` 的测试 Store，让 `save_artifact()` 抛 `ArtifactPersistenceError("artifact_persistence_failed")`。断言 `DomainStatus.FAILED`、错误码固定、结果没有 Artifact，且没有向外暴露异常文本。

- [ ] **Step 4: 运行完整图测试并确认失败。**

Run: `uv run python -m unittest tests.test_business_query_graph.BusinessQueryExecutionTests -v`
Expected: FAIL because execution and graph runner are incomplete。

- [ ] **Step 5: 实现执行与分类节点。**

`execute_fixed_query` 在 deadline 已耗尽时不调用指标函数，构造 `deadline_exceeded`。否则调用现有指标函数一次。`classify_result` 使用固定映射：`ok+complete -> SUCCESS`、`missing_data -> MISSING_DATA`、`invalid_parameters -> NEEDS_INPUT`、`forbidden/unavailable -> FAILED`；保留 coverage 和 data_as_of。原始 `ToolResult.limitations` 只进入 Task 1 验证过的公共 Artifact；持久化 State 只保存确定性映射后的 limitation codes，未知文本不得写入 State/Event。

- [ ] **Step 6: 实现安全投影、Artifact 和终结节点。**

将现有 `_PUBLIC_RESULT_COLUMNS` 和安全投影算法从 `agent.py` 移入 `business_query/tool.py`，改为接收 `shop_aliases: dict[str, str]`，不要导入 `SessionState`，避免循环依赖。`agent.py` 在查询路由尚未切换的情况下改为导入兼容的 `to_model_result` 和 `to_public_artifact` 包装，确保中间提交没有重复逻辑且原测试继续通过。对所有合法 `ToolResult` 生成模型安全投影与公共安全投影，再把公共投影交给 Store。保存成功后将 ref 加入 State；失败生成 `artifact_persistence_failed`。`finalize_run` 使用已分类 target status 映射到 RunStatus，并调用 `store.finish()`。

- [ ] **Step 7: 实现固定调度器。**

调度器只按白名单顺序调用节点。`NEEDS_INPUT/FAILED` 若发生在执行前，立即 `finish()`；已有合法 ToolResult 时必须经过 `CLASSIFY_RESULT -> PERSIST_ARTIFACT -> FINALIZE`。每次节点完成后调用 `store.transition()` 并使内存 revision 与数据库 revision 同步。

- [ ] **Step 8: 运行子图模块全部测试并提交。**

Run: `uv run python -m unittest tests.test_business_query_graph -v`
Expected: PASS。

```bash
git add backend/bi_agent/business_query backend/tests/test_business_query_graph.py
git commit -m "feat: execute fixed queries through state graph"
```

### Task 7: Tool Adapter 与主 Agent 接入

**Files:**
- Modify: `backend/bi_agent/business_query/tool.py`
- Modify: `backend/bi_agent/business_query/__init__.py`
- Modify: `backend/bi_agent/agent.py`
- Modify: `backend/bi_agent/chats.py`
- Modify: `backend/tests/test_core.py`
- Modify: `backend/tests/acceptance.py`

**Interfaces:**
- Consumes: Task 4 的 `BusinessQueryExecution(domain_result, tool_result, session_filters)`。
- Produces: `run_business_query(conn, store, tool_input, context) -> DomainResult` 与 `execute_business_query_tool(call, session_state, context, conn, store) -> BusinessQueryExecution`。
- Changes: `answer(question: str, state: SessionState, *, model: ChatModel, conn: object, allowed_shop_ids: frozenset[str], now: datetime, run_store: QueryRunStore | None = None, turn_context: TurnContext | None = None) -> TurnResult`。
- Preserves: `TurnResult.results: list[ToolResult]` for current tests and acceptance reporting。

- [ ] **Step 1: 把现有 Agent 查询回归改为注入内存 Store。**

在 `AgentTests.setUp()` 创建 `MemoryQueryRunStore(forbidden_values={"S1", "ERP-P-9"})`，现有 `answer()` 调用传入 `run_store=self.run_store`。新增断言：一次成功查询产生一条 run 和一个 Artifact；非法参数修正产生两条相同 `user_message_id`、attempt_no 为 1/2 的运行。

- [ ] **Step 2: 新增 Agent 不直接执行指标查询的边界测试。**

patch `bi_agent.business_query.nodes.query_business` 或节点使用的准确 import 点，不能再 patch `bi_agent.agent._run_query_business`。同时断言 `agent.py` 不再导出 `_handle_query_business` 和 `_run_query_business`。

- [ ] **Step 3: 运行 Agent 测试并确认接入前失败。**

Run: `uv run python -m unittest tests.test_core.AgentTests -v`
Expected: 新增运行审计断言 FAIL。

- [ ] **Step 4: 实现 Tool Adapter。**

在 Task 6 已迁移的安全投影基础上实现 Adapter，保持输出逐字段兼容。Adapter 使用 Task 4 的 `BusinessQueryExecution`：公开 `run_business_query()` 只返回其中的 `domain_result`；兼容现有 Agent 的 `execute_business_query_tool()` 返回完整的内部 Execution。`session_filters` 由已验证 `QueryRequest` 生成，包含真实店铺 ID，但只存在于进程内和 `SessionState.filters`，不得写入运行状态、事件、模型 payload 或 Artifact。

- [ ] **Step 5: 接入 `answer()`。**

删除 `_handle_query_business()`、`_run_query_business()` 和 `_resolved_shops()`。先按工具名分支：`query_business` 即使 `arguments_error` 非空也必须调用 Adapter 并产生运行记录；推广测算继续使用现有通用参数错误路径。经营查询的 `NEEDS_INPUT` 生成现有 correction tool message；有合法 `tool_result` 时继续追加到 `TurnResult.results`；工具消息内容直接使用 `domain_result.model_payload`；筛选只从 `session_filters` 更新。

直接调用 `answer()` 且未提供 Store 时，以 `allowed_shop_ids` 作为 deny set 建立一个 `MemoryQueryRunStore(forbidden_values=allowed_shop_ids)` 和本轮固定的 synthetic `chat_id/user_message_id`，保证单元测试和离线验收仍经过状态图，但不要求数据库会话记录。生产 `run_chat_turn()` 必须显式提供 PostgreSQL Store，不允许走此 fallback。

- [ ] **Step 6: 让生产聊天路径传递真实用户消息 ID。**

`save_user_message()` 的返回值已经是 `ChatMessage`，保持函数不变；在 `run_chat_turn()` 中改为：

```python
saved_user = save_user_message(conn, chat_id, subject, content)
turn = answer(
    content,
    state,
    model=model,
    conn=conn,
    allowed_shop_ids=allowed_shop_ids,
    now=now,
    run_store=PostgresQueryRunStore(conn, forbidden_values=allowed_shop_ids),
    turn_context=TurnContext(
        chat_id=chat_id,
        user_message_id=saved_user.id,
        subject_id=subject,
    ),
)
```

- [ ] **Step 7: 修改离线验收注入。**

`tests/acceptance.py::_run_turn()` 为每个用户 turn 创建一个固定 `TurnContext` 和 `MemoryQueryRunStore(forbidden_values=allowed)`；Spy 改为 patch 新节点的指标调用点。验收仍从 `TurnResult.results` 检查原始 `ToolResult`，不改 20 道题的期望值。

- [ ] **Step 8: 运行核心和离线验收测试。**

Run from `backend/`:

```bash
uv run python -m unittest tests.test_runtime tests.test_business_query_graph tests.test_core -v
uv run --env-file ../.env.test python -m tests.acceptance --offline
```

Expected: 单元测试 PASS；离线验收 20 题全部通过。没有测试 DSN 时 acceptance 明确返回 skipped，不可报告为通过。

- [ ] **Step 9: 提交 Agent 接入。**

```bash
git add backend/bi_agent/agent.py backend/bi_agent/chats.py backend/bi_agent/business_query backend/tests/test_core.py backend/tests/acceptance.py
git commit -m "refactor: route business queries through state graph"
```

### Task 8: 生产路径追踪、SSE 回归与运维文档

**Files:**
- Modify: `backend/tests/test_api.py`
- Modify: `backend/tests/test_runtime_db.py`
- Modify: `README.md`
- Modify: `docs/runbook.md`

**Interfaces:**
- Verifies: 一次 API 经营查询产生完整 `query_runs -> query_run_events -> query_artifacts` 链。
- Preserves: API 路径和 SSE `status/artifact/message/error/done` 契约。

- [ ] **Step 1: 写 API 查询追踪集成测试。**

构造模型先返回 `query_business` ToolCall，再返回最终文本。通过 API 创建聊天并发送“最近7天店铺A支付金额”；测试数据库预置现有合成业务数据与覆盖。断言：

```python
self.assertIn("event: artifact", response.text)
self.assertIn("event: message", response.text)
self.assertTrue(response.text.rstrip().endswith(
    'event: done\ndata: {"status":"complete"}'
))
run = admin_conn.execute(
    "SELECT id, status, current_node, revision FROM bi.query_runs "
    "WHERE chat_id=%s ORDER BY started_at DESC LIMIT 1", (chat_id,),
).fetchone()
self.assertEqual(run[1:3], ("succeeded", "finalize"))
self.assertGreater(run[3], 0)
self.assertGreater(admin_conn.execute(
    "SELECT count(*) FROM bi.query_run_events WHERE run_id=%s", (run[0],)
).fetchone()[0], 0)
self.assertEqual(admin_conn.execute(
    "SELECT count(*) FROM bi.query_artifacts WHERE run_id=%s", (run[0],)
).fetchone()[0], 1)
```

查询持久化 JSON 文本并断言不含测试真实 ID `S1`、`ERP-P-9` 或 `reasoning_content`。

- [ ] **Step 2: 写 SSE 错误兼容测试。**

让 Store 创建运行失败，断言响应仍是 `event: error` 后接 `event: done`，浏览器消息不包含 psycopg 类型、SQL 或 DSN。保留已有模型失败 SSE 测试。

- [ ] **Step 3: 运行 API 测试并确认新增测试在接入完成前失败。**

Run: `uv run --env-file ../.env.test python -m unittest tests.test_api -v`
Expected: 新查询追踪测试在 Task 7 完成前 FAIL；Task 7 完成后 PASS。

- [ ] **Step 4: 补充迁移与诊断文档。**

README 和 runbook 明确生产/测试数据库均按 `001 -> 002 -> 003` 顺序执行。runbook 增加只读诊断查询：按 chat 查最近 run、按 run 查事件顺序、按 run 查 Artifact 元数据；示例不得查询 Artifact payload 或记录真实店铺 ID。写明运行持久化失败会阻止查询，恢复后由用户重发问题。

- [ ] **Step 5: 执行完整验证。**

Run from `backend/`:

```bash
uv run python -m unittest tests.test_runtime tests.test_business_query_graph tests.test_core -v
uv run --env-file ../.env.test python -m unittest tests.test_runtime_db tests.test_db tests.test_api -v
uv run --env-file ../.env.test python -m tests.acceptance --offline
```

Run from `frontend/`:

```bash
npm test
npm run build
```

Expected: 所有执行项 PASS；离线 acceptance 报告 20/20；前端测试和构建成功。若数据库或 provider 环境缺失，逐项记录 SKIP/未实测，不能写成通过。

- [ ] **Step 6: 检查范围和敏感数据。**

Run from repository root:

```bash
git diff --check
git diff --name-only HEAD
rg -n "reasoning_content|BI_TEST_ADMIN_DSN|S1|ERP-P-9" backend/bi_agent/runtime backend/bi_agent/business_query backend/sql/004_query_runtime.sql
```

Expected: `git diff --check` 无输出；修改文件均属于本计划；源代码没有硬编码凭证，测试 fixture 出现 `S1/ERP-P-9` 仅限测试文件且不会进入持久化状态。

- [ ] **Step 7: 提交文档与端到端验证切片。**

```bash
git add backend/tests/test_api.py backend/tests/test_runtime_db.py README.md docs/runbook.md
git commit -m "test: verify audited business query flow"
```

---

## 最终检查点

- 阶段 1 结束时，运行表和 Store 已可独立使用，但生产查询尚未接入。
- 阶段 2 结束时，所有生产 `query_business` 调用都经过固定状态图。
- `agent.py` 不再拥有经营查询参数解析、授权判断或固定查询执行实现。
- `metrics.py` 仍是唯一指标 SQL 执行层，映射修复提交不被回退或改写。
- 运行状态、事件和 Artifact 与聊天消息分离，且可按 `chat_id/user_message_id/run_id` 追踪。
- `missing_data`、真实零、参数错误、禁止访问、超时和持久化失败拥有不同终态及恢复动作。
- 20 道离线验收、后端完整测试、前端测试和构建均有明确执行结果。
