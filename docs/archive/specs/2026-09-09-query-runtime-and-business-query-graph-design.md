# 查询运行状态与确定性经营查询子图设计

**日期：** 2026-09-09
**状态：** 已确认，可进入实施计划
**范围：** 阶段 1「结构化运行状态」与阶段 2「现有固定查询的确定性子图」

## 1. 目标

在不改变现有经营指标口径、固定 SQL、前端 SSE 契约和问答结果的前提下，完成两项重构：

1. 建立与聊天消息分离、可持久化和可审计的领域运行状态。
2. 将 `query_business` 的参数处理、授权检查、固定查询执行和结果分类迁入确定性状态图，使主 Agent 只负责对话与工具路由。

阶段 1 和阶段 2 形成一个可独立交付的子项目。完成后，每次经营查询都能通过运行记录、状态事件和结果 Artifact 完整追踪，但系统仍使用现有固定指标查询，不引入自由 SQL。

## 2. 非目标

本次不实现：

- Schema Catalog、Schema 检索或 Schema 选择。
- Text2SQL、任意 SQL、Python 或 HTTP 工具。
- LangGraph、向量库、Redis、消息队列或新的后台服务。
- 复杂分析子 Agent、隔离计算环境或学习型记忆。
- 推广测算子图重构。
- ERP 数据库表项映射修复、同步规范化或指标口径调整。
- 前端页面、SSE 事件名称或 API 路径变更。

## 3. 选定方案

采用「Pydantic 契约 + PostgreSQL 运行记录 + 普通 Python 显式状态机」。

没有采用立即引入 LangGraph 的方案，因为当前查询路径节点少、转移固定，引入框架不会增加当前阶段的业务能力，反而会把依赖升级、检查点和序列化语义加入迁移范围。没有采用只定义内存状态的方案，因为无法满足错误、重试、查询结果与聊天消息分离以及可审计要求。

数据库变更使用新的 `backend/sql/004_query_runtime.sql`，顺接已经提交的 `002_kuaimai_mapping_repair.sql`。现有数据库映射修复由另一个 Agent 处理，本项目不修改 `sync.py`、既有业务表定义或 `metrics.py` 的查询语义。实施任务开始前必须检查工作区最新差异，避免覆盖并行工作。

## 4. 目标模块

```text
backend/bi_agent/
  runtime/
    __init__.py
    models.py       # 通用领域结果、错误、Artifact 和运行枚举
    repository.py   # 运行、事件和 Artifact 的持久化接口与 PostgreSQL 实现
    memory.py       # 供单元测试使用的内存 Store
  business_query/
    __init__.py
    state.py        # BusinessQueryInput、Context 和 State
    nodes.py        # 确定性节点
    graph.py        # 合法转移、节点调度和终态
    tool.py         # 对主 Agent 暴露统一经营查询 Tool
```

现有模块职责调整如下：

- `agent.py` 保留模型对话循环、店铺匿名化、工具路由、一次参数纠正、工具调用上限和总时间预算，不再直接处理经营查询参数或调用 `metrics.query_business()`。
- `metrics.py` 保持固定 SQL、指标定义、覆盖检查、权限二次校验和 `ToolResult` 契约。
- `chats.py` 只负责会话和用户可见消息；用户消息保存后提供 `user_message_id` 给运行上下文。
- `api.py` 继续输出现有 SSE 契约，不读取或展示内部节点事件。

## 5. 公共契约

### 5.1 领域结果

```python
class DomainStatus(StrEnum):
    SUCCESS = "success"
    NEEDS_INPUT = "needs_input"
    MISSING_DATA = "missing_data"
    PARTIAL = "partial"
    FAILED = "failed"


class RecoveryAction(StrEnum):
    NONE = "none"
    ASK_USER = "ask_user"
    CORRECT_PARAMETERS = "correct_parameters"
    RETRY_LATER = "retry_later"


class ErrorEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: str
    stage: str
    retryable: bool
    recovery: RecoveryAction
    public_message: str
    problems: list[str] = Field(default_factory=list)


class ArtifactRef(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: UUID
    type: Literal["metric_result"]


class DomainArtifact(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ref: ArtifactRef
    public_payload: dict[str, object]


class DomainResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    run_id: UUID
    status: DomainStatus
    model_payload: dict[str, object]
    artifacts: list[DomainArtifact] = Field(default_factory=list)
    data_as_of: datetime | None = None
    coverage: Coverage | None = None
    error: ErrorEnvelope | None = None
```

`model_payload` 必须使用现有模型安全投影，不含真实店铺或商品标识。`DomainArtifact.public_payload` 使用现有公共安全投影，`ref` 指向已经持久化的 Artifact；这样主 Agent 无需再次查询数据库即可维持现有 SSE 输出。`DomainResult` 不暴露 SQL、数据库诊断或内部节点输入。

### 5.2 子图入口

```python
def run_business_query(
    conn: psycopg.Connection,
    store: QueryRunStore,
    tool_input: BusinessQueryInput,
    context: BusinessQueryContext,
) -> DomainResult:
    ...
```

为兼容现有 `TurnResult.results: list[ToolResult]` 和真实店铺筛选状态，图内部可以使用不持久化的 `BusinessQueryExecution`，同时携带 `DomainResult`、原始 `ToolResult` 和 `session_filters`。公开领域入口仍只返回 `DomainResult`；兼容包装仅供当前主 Agent 迁移期间使用。

`BusinessQueryInput` 包含模型工具调用 ID 与解析后的参数。`BusinessQueryContext` 仅在进程内使用，包含原始问题、会话筛选、匿名店铺映射、授权店铺 ID、业务时刻、绝对 deadline、`chat_id`、`user_message_id` 和本轮 `attempt_no`。

`BusinessQueryState` 是可持久化的安全状态，仅包含：

- 当前节点、运行状态和 revision。
- 匿名化、规范化后的请求参数。
- 参数问题列表。
- 授权范围数量或稳定摘要，不保存真实店铺 ID。
- `ToolResult.status`、覆盖、共同截止时间和限制。
- Artifact 引用。
- 结构化错误。

原始问题、模型隐藏推理、真实 ERP 标识、数据库错误原文和凭证不得进入持久化状态或事件 payload。

### 5.3 持久化接口

```python
class QueryRunStore(Protocol):
    def create_run(self, record: NewQueryRun) -> UUID: ...
    def transition(self, run_id: UUID, transition: RunTransition) -> None: ...
    def save_artifact(self, run_id: UUID, artifact: NewArtifact) -> ArtifactRef: ...
    def finish(self, run_id: UUID, completion: RunCompletion) -> None: ...
```

生产使用 `PostgresQueryRunStore`，单元测试使用 `MemoryQueryRunStore`。业务子图只依赖此协议，不直接拼接运行记录 SQL。

## 6. 数据模型

### 6.1 `bi.query_runs`

每次 `query_business` 工具调用一行，保存最新安全状态。

| 字段 | 约束与用途 |
| --- | --- |
| `id` | UUID 主键 |
| `chat_id` | 外键关联 `bi.app_chats` |
| `user_message_id` | 外键关联触发运行的用户消息 |
| `subject_id` | 会话所有者，用于审计与归属查询 |
| `tool_call_id` | 当前模型工具调用 ID，只用于协议关联 |
| `domain` | 固定为 `business_query` |
| `attempt_no` | 同一用户消息下从 1 开始递增 |
| `status` | `running/succeeded/needs_input/missing_data/partial/failed` |
| `current_node` | 当前或最后完成节点 |
| `revision` | 每次状态迁移加 1 |
| `normalized_request` | 匿名化请求 JSON，不含真实 ERP ID |
| `state` | 最新 `BusinessQueryState` 安全快照 |
| `error_code` | 可为空的结构化错误码 |
| `started_at/updated_at/completed_at` | 运行时间 |

同一 `user_message_id + domain + attempt_no` 唯一。删除聊天时运行、事件和 Artifact 随聊天级联删除；删除单条消息不是当前 API 能力。

### 6.2 `bi.query_run_events`

状态事件只追加。

| 字段 | 约束与用途 |
| --- | --- |
| `run_id` | 外键关联运行，级联删除 |
| `revision` | 与 `query_runs.revision` 对应 |
| `node` | 发生事件的节点 |
| `event_type` | `entered/completed/failed/transitioned` |
| `status` | 事件后的运行状态 |
| `payload` | 脱敏节点摘要 |
| `created_at` | 事件时刻 |

`run_id + revision` 唯一。事件没有 UPDATE 或 DELETE 应用接口。

### 6.3 `bi.query_artifacts`

| 字段 | 约束与用途 |
| --- | --- |
| `id` | UUID 主键 |
| `run_id` | 外键关联运行，级联删除 |
| `artifact_type` | 当前固定为 `metric_result` |
| `payload` | 使用公共安全投影后的结果 |
| `data_as_of` | 查询共同截止时间 |
| `coverage` | 覆盖状态 JSON |
| `created_at` | 创建时刻 |

现有 `app_messages.artifacts` 在本阶段继续保存公共展示投影以兼容前端。`query_artifacts` 是机器侧权威记录，消息附件是面向现有 API 的展示副本。

## 7. 确定性状态图

正常路径固定为：

```text
RECEIVED
  -> RESOLVE_PARAMETERS
  -> VALIDATE_PARAMETERS
  -> AUTHORIZE_SCOPE
  -> EXECUTE_FIXED_QUERY
  -> CLASSIFY_RESULT
  -> PERSIST_ARTIFACT
  -> FINALIZE
  -> SUCCEEDED | MISSING_DATA | PARTIAL | FAILED
```

终态包括：

```text
SUCCEEDED
NEEDS_INPUT
MISSING_DATA
PARTIAL
FAILED
```

节点职责：

1. `RESOLVE_PARAMETERS`：将匿名店铺映射到内存中的真实授权 ID，合并上一轮筛选，使用现有日期解析补齐范围，并提供默认指标。持久化版本只记录匿名参数。
2. `VALIDATE_PARAMETERS`：使用现有 `QueryRequest` 校验日期、指标、维度、比较和 Top-N。
3. `AUTHORIZE_SCOPE`：确认全部店铺均在授权集合内；失败后禁止执行查询。
4. `EXECUTE_FIXED_QUERY`：调用现有 `metrics.query_business()`，继续使用其权限二次校验、覆盖检查、绝对 deadline 和固定 SQL。
5. `CLASSIFY_RESULT`：把 `ToolResult.status` 与覆盖状态映射成待应用的领域终态。
6. `PERSIST_ARTIFACT`：对所有形状合法的 `ToolResult` 保存公共安全投影，包括 `missing_data`、`partial` 和查询层失败结果；成功保存后才能返回该结果。
7. `FINALIZE`：应用已分类的领域终态。Artifact 保存失败时覆盖原分类并进入 `FAILED`。

状态机不允许自由选择下一节点。非法转移产生 `invalid_transition`，运行进入 `FAILED`。

## 8. 错误与恢复

| 条件 | 终态 | 错误码 | 恢复动作 |
| --- | --- | --- | --- |
| 缺少必要参数 | `NEEDS_INPUT` | `missing_parameters` | `ASK_USER` 或 `CORRECT_PARAMETERS` |
| 参数类型或范围非法 | `NEEDS_INPUT` | `invalid_parameters` | `CORRECT_PARAMETERS` |
| 未授权店铺或注入式标识 | `FAILED` | `forbidden` | `NONE`，禁止重试 |
| 数据完全无覆盖 | `MISSING_DATA` | 无系统错误 | `NONE`，返回覆盖缺口 |
| 数据部分覆盖 | `PARTIAL` | 无系统错误 | `NONE`，保留结果及限制 |
| 总预算耗尽 | `FAILED` | `deadline_exceeded` | `RETRY_LATER` |
| 数据库不可用 | `FAILED` | `unavailable` | `RETRY_LATER` |
| ToolResult 契约异常 | `FAILED` | `result_contract_violation` | `NONE` |
| Artifact 持久化失败 | `FAILED` | `artifact_persistence_failed` | `RETRY_LATER` |

子图内部不自动重试。现有模型参数纠正仍允许一次，但第一次非法调用以 `NEEDS_INPUT` 结束，修正后的模型调用创建新的 `query_run`，两者使用相同 `user_message_id` 和递增 `attempt_no` 关联。

## 9. 事务和故障语义

每次 `transition()` 在一个数据库事务中：

1. 锁定对应 `query_runs` 行并核对 revision。
2. 将 revision 加 1，更新最新状态、节点和时间。
3. 插入相同 revision 的 `query_run_events`。
4. 一并提交或一并回滚。

创建运行记录失败时不执行经营查询。查询已经完成但 Artifact 无法持久化时，运行进入 `FAILED`，不得向模型或用户声称查询成功。API 使用现有脱敏错误和 `error -> done` SSE 结束方式，不暴露数据库诊断。

现有会话锁继续保证同一聊天一次只有一个活动回答。一次回答中的多个工具调用分别创建运行记录，不共享可变子图状态。

## 10. 调用链

```text
run_chat_turn
  -> 保存用户消息并取得 user_message_id
  -> 构造 TurnContext
  -> answer
  -> business_query tool
  -> 创建 query_run
  -> 执行确定性子图并记录事件
  -> 保存 query_artifact
  -> 返回 DomainResult
  -> 转换为模型 Tool Message 与公共 Artifact
  -> 保存用户可见回答
```

`agent.py` 继续负责匿名化用户问题、模型消息历史、最多四次工具调用、一次参数纠正、最多五次模型回合和 30 秒总预算。推广测算继续使用原路径。

## 11. 测试策略

### 11.1 阶段 1

- 所有 Pydantic 边界契约拒绝额外字段。
- `ErrorEnvelope`、运行状态和事件 payload 拒绝数据库原文、真实 ERP ID 和模型隐藏推理。
- `004_query_runtime.sql` 可重复执行。
- 一次迁移的运行更新和事件插入原子提交。
- revision 与事件顺序一致，过期 revision 更新失败。
- 删除聊天时运行、事件和 Artifact 级联删除。
- `MemoryQueryRunStore` 与 PostgreSQL Store 具有相同可观察行为。
- 阶段 1 完成时生产查询路径尚未切换，全部原有测试继续通过。

### 11.2 阶段 2

- 成功查询严格经过预定节点顺序。
- 缺参数和非法参数不调用 `metrics.query_business()`。
- 未授权范围不调用 `metrics.query_business()`。
- `missing_data` 与真实零值产生不同终态。
- `partial` 保存结果、覆盖缺口和限制。
- 超时或不可用错误不会在子图内重试。
- 一次模型参数纠正产生两个运行记录。
- Artifact 持久化失败不能返回成功。
- 模型安全投影和公共安全投影不包含真实 ERP 标识。
- 现有工具调用上限、时间预算、会话筛选和匿名映射测试继续通过。
- `backend/tests/questions.jsonl` 的 20 道问答验收结果不变。
- 前端 SSE 事件类型和顺序不变。

## 12. 完成标准

阶段 1 完成标准：

- 通用运行契约、三个新增运行表、PostgreSQL Store 和内存 Store 完成。
- 尚未切换生产查询路径。
- 原有测试及新增持久化测试通过。

阶段 2 完成标准：

- 所有 `query_business` 调用均经过确定性子图。
- `agent.py` 不再包含经营查询参数处理、授权判断和直接执行逻辑。
- 每次经营查询可以通过 `query_runs -> query_run_events -> query_artifacts` 完整追踪。
- 固定指标口径、数据库结果、模型可见结果和用户可见结果与重构前一致。
- 未引入本设计非目标中的能力或依赖。
