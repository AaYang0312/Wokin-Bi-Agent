# Task 11 后置子项目总设计

日期：2026-09-14。状态：设计方向已确认，等待书面规格复核后编写实施计划。

## 1. 目标与基线

本设计把[跨平台运营工作流计划](../plans/2026-09-11-data-and-query-closure.md)末尾的五个“后置子项目”拆成可以独立评审、测试和发布的工作包：

1. 语义目录与 Schema 检索；
2. 受控 SQL 探索；
3. approved 查询学习记忆；
4. 隔离分析 Agent；
5. 持续库存通知。

这些工作都不是当前 Task 11 的补丁。Task 11 仍负责 26 题与四工作流验收、真实来源与 provider 取证、部署恢复、一周试用及文档收口；五个后置子项目的**生产实现统一以 Task 11 验收通过为前置门禁**。在门禁通过前可以评审规格和计划，不合入后置功能代码，不借后置项目绕过尚未通过的验收。

截至本设计编写时，Task 6–10 已有实现提交；现有题库和 runner 仍是 20 题，四工作流专用集成测试与日期化验收报告尚未落地。上架价和库存来源注册表默认无已核验来源，所以真实部署会正确返回 `unsupported`。这些事实决定后置项目只能消费“已认证能力”，不能把新检索、Agent 或调度层当作数据来源。

## 2. 选定方案与替代方案

采用“一个总设计、五份独立实施计划、三条交付泳道”的方案。每个子项目有自己的公共接口、迁移、测试集、feature gate 和发布决定；共享现有授权、来源能力、运行版本、Artifact 与 deadline 契约，不建立第二套 Agent 平台。

交付泳道为：

```text
Task 11 发布门禁
├─ 语义目录 / Schema 检索 → 受控 SQL 探索 → approved 查询学习记忆
├─ 隔离分析 Agent
└─ 已核验库存来源 → 持续库存通知
```

未采用以下方案：

- **单一巨型计划：** 五个子系统的上线条件不同，合并后会让库存来源或通知渠道阻塞 Schema 检索，也无法独立回滚。
- **通用自治 Agent 优先：** 会让模型同时承担 Schema 选择、SQL 安全、数值计算和行动决策，破坏当前“金额由确定性代码产生”的边界。
- **先上向量库和外部工作流平台：** 当前元数据规模没有证明需要新基础设施；首版使用版本化代码目录、PostgreSQL 和现有运行层即可。

## 3. 全局约束

- Python 3.11+、Pydantic 2、psycopg 3、PostgreSQL 17、FastAPI、React 19 和现有 `unittest` / Vitest 技术栈保持不变。
- 每次实现前读取最新 HEAD 和实际迁移序号；从 019 以后使用当时下一个可用编号，不修改已经应用的迁移。
- 模型永远不能提供 `subject_id`、真实店铺 ID、库存池授权、DSN、来源认证或版本认证；这些只由服务端上下文提供。
- 底层 `bi.*` 表继续不授予 `bi_app`；查询只读获准的 `reporting.*` 视图，并在只读事务中执行。
- 金额、数量、比率、排名、趋势、价差、阈值和告警状态由确定性代码计算。模型只做候选选择、澄清、解释或假设生成。
- 所有新结果携带 schema/catalog/metric/policy/source/graph 版本和来源 Artifact 引用；版本不匹配时拒绝复用，不静默升级旧结果。
- 一次请求沿用现有 30 秒总 deadline、行数上限和 Store CAS；重试、检索、分析或 SQL 校验不能重置预算。
- 对外载荷只含稳定 opaque ref 和经授权的展示投影；原始提示、DSN、SQL 错误堆栈、ERP 主键和未授权名称不得进入模型载荷、普通日志或通知。
- 每个子项目默认关闭。只有自身验收通过并完成运维记录后才能启用；一个子项目关闭不影响现有固定业务 Tool。
- 用户当次目标价、临时阈值、临时假设和一次性纠错永远不自动写入长期目录、学习记忆或通知策略。

## 4. Task 11 统一发布门禁

后置实现开始前，仓库必须有一份日期化 `task-11-release-acceptance` 报告，并同时满足：

1. `questions.jsonl` 和 runner 已升级到 26 题，离线结构化验收 26/26；basis、diagnostics、capability 和原因码参与断言。
2. 四工作流 11 个业务场景已进入 `test_operator_workflows.py`；正常、部分来源、无权限和 Artifact 持久化失败均有覆盖。
3. 项目锁定环境可复现；DB-enabled 后端套件、API 测试、前端测试和生产构建均为零失败，不能以缺依赖或整组 skip 代替通过。
4. 至少一个获准真实 provider 完成 smoke 和 26 题 live 验收；未测试的 provider 明确记为未实测，不能宣称双 provider 通过。
5. 001 到最新迁移已在目标环境核对；每个来源/能力明确记录为 enabled、unsupported 或 failed。上架价或库存没有证据时允许保持 disabled，但不能伪装成零或安全。
6. 可信身份、Origin、SSE、小时同步、每日重核、脱敏日志、备份和一次真实恢复检查均有日期化证据。
7. 一店一周试用完成，问题、数据缺口、模型错误和处理决定已归档；README、runbook、metrics 和 demo 与实际状态一致。

门禁通过表示当前产品可以诚实发布，不表示所有平台、上架价、库存或广告数据都已开通。

## 5. 子项目 A：语义目录与 Schema 检索

### 5.1 职责

建立供服务端使用的版本化语义目录，描述已获准 reporting 视图、字段、指标、实体和合法 JOIN 粒度，并从用户问题中检索一个小而完整的候选集合。它只回答“哪些已批准的数据结构可能表达这个问题”，不执行 SQL，不授予数据能力，不读取未授权行。

首版不引入向量数据库。采用代码中的显式目录、受控别名和确定性词项检索；模型如参与，只能在服务端给出的候选内排序或请求澄清。数据库 introspection 仅用于启动时验证声明与实际 schema 一致，不能自动把新表加入可查询范围。

### 5.2 公共契约

```python
@dataclass(frozen=True)
class SemanticSelection:
    catalog_version: str
    entity_refs: tuple[str, ...]
    metric_refs: tuple[str, ...]
    view_refs: tuple[str, ...]
    field_refs: tuple[str, ...]
    join_path_refs: tuple[str, ...]
    missing_concepts: tuple[str, ...]
    requires_clarification: bool

def retrieve_schema_candidates(
    question: str,
    *,
    allowed_domains: frozenset[str],
    current_versions: "VersionSet",
    limit: int = 5,
) -> SemanticSelection: ...
```

目录项使用稳定 ref，不把 SQL 标识符直接交给模型；服务端在后续受控 SQL 阶段把 ref 解析成白名单标识符。JOIN 边明确记录左右实体、键、基数、允许的聚合粒度和防重复规则。

### 5.3 数据流与失败语义

用户问题先走现有固定 Tool 路由。只有问题属于经营数据查询、且没有固定模板能表达时，才进入语义检索。检索顺序是领域过滤 → 别名/词项匹配 → JOIN 连通性校验 → 候选裁剪 → 版本冻结。没有完整 JOIN 路径、命中多个冲突粒度或出现未注册概念时返回 `needs_input` / `schema_ambiguous`，不得猜一个视图。

### 5.4 验收

- 至少 30 道冻结的 schema 选择题覆盖现有指标、四工作流实体、同名字段、错误 JOIN 和未支持概念。
- 每题所需视图在 Top 5 候选内的召回率为 100%，禁止视图召回为 0；合法 JOIN 路径和粒度判断与人工 gold set 完全一致。
- 目录声明与 PostgreSQL 实际视图不一致时启动预检失败；旧目录版本仍可解释历史 Artifact，但不能用于新 SQL。
- 候选结果不含真实店铺 ID、底表名或未经授权的显示名称。

## 6. 子项目 B：受控 SQL 探索

### 6.1 职责与入口门槛

只承接“数据能力和覆盖已满足，但固定模板不能表达”的只读聚合问题。`capability_unavailable`、缺覆盖、来源未认证、需要写操作或存在固定 Tool 的请求都不能降级到 SQL 探索。

流程使用语义目录选出的视图、字段与 JOIN 边生成单条 PostgreSQL `SELECT` 草案，再通过 AST、参数、权限、代价和结果契约五层校验。首版选择一个支持 PostgreSQL 方言并能遍历 AST 的锁定解析库；安全性同时依赖 `bi_app` 最小权限、`READ ONLY` 事务和 `statement_timeout`，不把 parser 当唯一沙箱。

### 6.2 公共契约

```python
class ExplorationRequest(BaseModel):
    question: str
    start: date | None
    end: date | None
    entity_refs: list[str]
    requested_metrics: list[str]
    group_by_refs: list[str]
    limit: int = 100

class ValidatedQueryPlan(BaseModel):
    template_version: str
    catalog_version: str
    statement_fingerprint: str
    sql_text: str
    parameters: dict[str, object]
    selected_refs: list[str]
    estimated_rows: int | None
    warnings: list[str]

def validate_exploration_plan(
    draft: "SqlDraft",
    *,
    selection: SemanticSelection,
    context: "DomainContext",
) -> ValidatedQueryPlan: ...
```

`sql_text` 只进入受限诊断记录和审计表，不进入普通聊天消息。公开 Artifact 保存问题、已选语义 ref、参数、结果、口径和 statement fingerprint；历史 SQL 只可查看，版本变化后必须重新生成和验证。

### 6.3 强制拒绝规则

- 只允许一条非递归 `SELECT`；拒绝 DDL、DML、COPY、CALL、DO、事务控制、锁、临时对象、扩展和多语句。
- 拒绝 `SELECT *`、未登记 schema/table/column/function/operator、隐式 CROSS JOIN、未登记 JOIN 边、相关子查询和会突破授权粒度的聚合。
- 日期、ref 和用户值必须参数化；模型不能提供标识符、排序表达式、limit 上限或权限条件。
- 服务端强制 `limit <= 500`、`statement_timeout <= 5s`、30 秒整轮 deadline 和结果字节上限；先执行受控 `EXPLAIN`，代价或预计行数超限则拒绝。
- 结果必须经过字段、类型、Decimal、basis、coverage、diagnostics 和 Artifact 白名单校验；任何必要 Artifact 写入失败都不能返回成功。

### 6.4 验收

- 固定业务问题与手工 SQL 的 Decimal 结果逐项一致，JOIN 放大、空值、零和部分覆盖分别验证。
- 攻击语料中的写操作、注释混淆、多语句、函数逃逸、越权视图、笛卡尔积和超预算查询全部在执行前拒绝。
- 使用 `bi_app` 的真实数据库测试证明底表与未授权视图不可读；数据库侧即使 validator 失误也拒绝写操作。
- 固定 Tool 可表达的问题仍只调用固定 Tool；SQL 探索关闭时原产品行为不变。

## 7. 子项目 C：approved 查询学习记忆

### 7.1 职责

把人工审核通过、版本仍匹配的规范化查询示例提供给路由、参数规范化或语义选择作为 few-shot。记忆不是聊天历史搜索，也不是自动训练；一次成功查询不会自动变成规则。

记忆生命周期固定为 `draft → approved → superseded/revoked`。只有 `approved` 且授权域、domain、schema/catalog/metric/policy 版本全部兼容的记录能进入检索。批准、撤销和替换必须记录操作者、时间、理由及来源运行 ID。

### 7.2 公共契约

```python
@dataclass(frozen=True)
class ApprovedExample:
    example_ref: str
    domain: str
    intent_signature: str
    normalized_request: dict[str, object]
    expected_tool: str
    version_requirements: "VersionSet"
    approval_revision: int

def retrieve_approved_examples(
    question: str,
    *,
    subject_scope: "AuthorizationScope",
    domain: str,
    current_versions: "VersionSet",
    limit: int = 3,
) -> tuple[ApprovedExample, ...]: ...
```

记录只保存去标识化问题模板、规范化请求和稳定 ref，不复制原始聊天、真实名称、SQL 结果或密钥。目标价、阈值、预算、日期和店铺选择默认是槽位，不作为长期常量保存。

### 7.3 数据流与防污染

候选来源是已有成功运行，但必须由授权操作者在独立审核界面明确批准。写入时先脱敏、抽取槽位、冻结版本；读取时先做授权和版本过滤，再做确定性检索。模型不能批准自己的输出，线上反馈也不能直接改 approved 状态。

撤销立即阻止后续检索；版本升级使旧例进入 `superseded` 候选队列，必须重新审核，不自动迁移。检索结果只能改善工具/参数选择，不能覆盖服务端能力、basis、来源或授权判断。

### 7.4 验收

- draft、revoked、superseded、跨授权域和版本不匹配示例的召回数均为 0。
- approved 当前版本示例可稳定复现工具和规范化参数；不因排序变化泄露真实实体。
- 注入式问题、错误 SQL、越权 ref 和一次性目标价即使对应运行成功也不能自动进入记忆。
- 启用记忆后的冻结题集不得降低金额、权限和拒答边界；关闭 feature gate 后回到无记忆基线。

## 8. 子项目 D：隔离分析 Agent

### 8.1 职责

对已经持久化、版本一致且当前用户仍有权读取的数据集 Artifact 做额外解释，例如贡献拆分、异常候选、变化摘要和可验证的后续问题。它不直接连接数据库，不生成或执行 SQL，不调用同步、价审、库存调整或通知工具。

趋势、排名、金额、比率、价差和阈值仍由确定性分析函数计算。Agent 只消费这些函数的结构化输出，组织解释并区分事实、观察和假设；没有流量、广告或实验数据时不能输出因果结论。

### 8.2 公共契约

```python
class AnalysisRequest(BaseModel):
    artifact_ref: str
    analysis_kinds: list[Literal[
        "contribution", "change_decomposition", "anomaly_candidates", "followups"
    ]]

class AnalysisResult(BaseModel):
    source_artifact_ref: str
    source_fingerprint: str
    analysis_version: str
    findings: list[dict[str, object]]
    hypotheses: list[str]
    unsupported_claims: list[str]
    limitations: list[str]

def analyze_artifact(
    request: AnalysisRequest,
    *,
    context: "DomainContext",
) -> AnalysisResult: ...
```

### 8.3 隔离和失败语义

服务端先重新检查 Artifact 归属、当前授权、schema 版本和数据集/图表配对，再把安全数据集投影交给分析运行。运行上下文不含数据库连接和写工具；最大行数、列数、token 和 deadline 固定。来源 Artifact 过期、缺列、版本不一致或已失去权限时返回稳定原因码，不尝试重查数据库。

分析结果作为新 Artifact 保存并引用不可变来源 fingerprint。可选自然语言总结失败时仍可展示确定性 findings；必要结果持久化失败时整次失败，不修改来源 Artifact。

### 8.4 验收

- 所有数值 findings 与来源数据集逐项相等，顺序变化不改变 fingerprint 或结论。
- 无权、版本过期、图表/数据集错配和过大 Artifact 在调用模型前拒绝。
- 测试替身证明分析运行没有数据库、文件、网络或现有业务 Tool 的可调用入口。
- “销量下降因为广告”“库存低所以立即采购”等无证据因果/行动结论进入 `unsupported_claims`，不会作为事实发布。

## 9. 子项目 E：持续库存通知

### 9.1 前置与首版渠道

除 Task 11 外，本子项目还要求 InventoryWatchGraph 对通知策略所依赖的库存层级已有真实来源登记、freshness policy、完整扫描证据和生产对账。**该门禁按层级分别判定**：

- 已核验登记只替自己那一层说话。`physical_total` 已取证而 `shop_sellable` 无来源时，策略仍可就实物层出告警；这种组合是所有者 2026-09-17 选定的目标形态（Option A），不是待修缺陷。反过来，策略请求的层级里一个都没核验时该策略以 disabled 退出；没有任何可用层级时调度器整体保持 disabled。两种情形下都不周期性发送“无法判断”通知。
- 缺已核验登记的层级既不能触发也不能解除：runner 把交给 graph 的**请求层级与 `DomainContext` 授权投影**同时收窄到已核验子集（只核验实物 ⇒ 仅获准池 + 空店铺授权投影；只核验渠道 ⇒ 仅店铺范围，池授权为空；两层都核验 ⇒ 两者都带），因此这一层在监控路径上根本没有行、没有去重键，也不占 `expected_items`，它的快照与店铺事实根本不被读取：graph 的渠道快照读以店铺授权非空为闸（`inventory/graph.py:613-620`），渠道侧的新鲜/扫描声明、时点并集与独有 SKU 都进不了物理轮的聚合（`inventory/graph.py:671`、`:676-683`、`:1033-1039`）；它的缺失由 gate 自己的**闭集**固定码与计数（`monitor_level_unverified` + `inventory_monitor_level_unverified_total{level}`）表达，而不是靠向 graph 多问一次、拿它出的 `unsupported` 占位行当表达。聊天路径不变：那里仍由 graph 自己报 `inventory_physical_source_unverified`、`inventory_channel_source_unverified`、`inventory_channel_snapshot_missing`。任何未核验层级的缺席或诊断，都不得使已在跑的已核验层级变得不完整。
- 缺数量永远不是零，也不做层级替位：不得把 `physical_total` 复制、扇出或换算成逐店可售量，也不得拿渠道缺口当实物补货依据，反之亦然；店铺级触发与解除只能由已核验的店铺级来源支撑。
- 缺失层级不产生面向用户的周期性提醒；只有固定诊断与固定计数。
- 一格要进入状态迁移，先得能被唯一识别：`scope_ref` 只能来自该行自己层级的 opaque 引用（实物 = 池 + 仓库一对，渠道 = 店铺）。预警载荷契约本身允许这些引用键缺席（只强制层级/SKU/状态），而 graph 在**已核验**的实物层也会出这种行：点名了某 SKU 但本轮没有快照时是一格无池无仓库的 `unknown` 占位行。它由 runner 排除在状态机输入之外，只记固定诊断与固定计数——不借另一层级的引用、不拼一个默认作用域、不补零、不生成去重键，因此既不触发也不解除。
- 来源 Artifact 是 graph 的**展示投影**（最多 `MAX_DISPLAY_ITEMS = 20` 行），不是全量行集：期望项超过这个上限时投影必被截断，而截断在 graph 里是“业务发现”不是失败。投影被截断的这一轮对任何已核验层级都不算完整：既不首发也不解除，也不得声称 500 行来源已全量持久化，或没出现的格子都安全。扩大决策投影是需要所有者另行决定的变更。

这一区分不降低任何一层的取证要求：本节的“可按层跑”只指已核验层级可以先出告警；把某一层定为“当前无权威来源、按 `data_missing`/unknown 处理”必须是日期化来源验收加所有者明确决定的结论（`2026-09-14-inventory-source-acceptance.md` 的 2026-09-17 追加节就是这份记录），而不是把“本轮没读到数据”当成默认的静默降级。2026-09-17 所有者决定与它的授权边界见 `docs/superpowers/research/2026-09-17-continuous-inventory-local-development-exception.md`，逐层取证台账见 `docs/superpowers/research/2026-09-14-inventory-source-acceptance.md`。

首版通知渠道确定为**应用内通知中心 + PostgreSQL outbox**。不在本项目内接入邮件、短信、企业微信、Slack 或任意 webhook；将来增加外部渠道时实现独立 sink 并单独评审凭证、收件范围和重试政策。调度使用部署主机的受控计划任务调用 CLI，不创建 Codex 自动化。

### 9.2 状态与公共契约

告警生命周期为 `open → acknowledged → resolved`，另有 `suppressed`。唯一去重键由策略版本、库存层级、商品/SKU、店铺或库存池作用域和规则码组成；作用域引用只能由该行自身层级给出（实物=池 + 仓库、渠道=店铺），缺任一成员就不是一格可去重的告警；同一事实重复扫描只更新 `last_observed_at`，不创建第二条 open 告警。

策略里的 `levels` 是**请求**层级，不是已核验层级：运行时已核验集合只能从来源注册表派生，`complete_levels` 只能是“已核验且本轮扫描完整”的子集。未核验层级的行不进入状态迁移、不生成去重键：在监控路径上 runner 只把已核验子集发给 graph——同时把图上下文的授权投影收窄到同样的范围（策略存储里的请求层级与 opaque refs 原样保留，永不收窄，两者不是一个字段）——所以 graph 不会为它出 `unsupported` + null 数量的占位行，也就没有可供它计算的去重键；作为第二道闸，runner 仍按已核验集合过滤一次载荷里的行，但已持久化载荷与 fingerprint 不因过滤而改变。聊天路径仍可同时请求两层并由 graph 自己报未核验限制码。

```python
class MonitorRunRequest(BaseModel):
    policy_ref: str
    as_of: Literal["latest"] = "latest"

@dataclass(frozen=True)
class AlertTransition:
    alert_ref: str
    previous_status: str | None
    next_status: Literal["open", "acknowledged", "resolved", "suppressed"]
    event_kind: Literal["triggered", "retriggered", "updated", "resolved"]
    dedupe_key: str
    source_artifact_ref: str

def run_inventory_monitor(
    request: MonitorRunRequest,
    *,
    service_scope: "AuthorizationScope",
    now: datetime,
    deadline: float,
) -> tuple[AlertTransition, ...]: ...
```

### 9.3 调度、解除与投递

每次调度以专用服务身份加载已批准策略和授权范围，调用现有 InventoryWatchGraph 生成不可变来源 Artifact（只请求已核验的那部分层级，并把图上下文的授权投影收窄到同样范围——物理轮空店铺投影下不读渠道快照、不产生店铺声明），再在单事务中计算状态迁移并写 outbox。通知页面按用户授权过滤，显示数量、单位、阈值、来源快照时间、作用域和原因。

- 首次低于或等于阈值产生 `triggered`。
- 状态持续但数值未发生有意义变化时只更新观测时间，不重复通知。
- 经过配置的冷却期后仍异常可产生 `retriggered`；冷却期属于版本化策略。
- 只有新的、完整且 fresh、且属于**该告警自身层级**的扫描证明已恢复到阈值以上才能 `resolved`；缺数据、过期、扫描不完整或该层级已不再核验都保持原状态并记录诊断。
- 一个曾经核验的渠道源停止供数（本轮没有它的快照）不能把 open/acknowledged 的渠道告警报成恢复；能力或来源退场只能走 `suppressed`，永远不是 `resolved`。
- 展示投影被截断（`inventory.truncated` 为 true 或限制码里出现 `inventory_display_truncated`）的这一轮，所有已核验请求层级都不算完整：既不 triggered 也不 resolved，保持原状态并记固定诊断与计数。投影截断不是“库存都安全”，也不是“已恢复”。
- acknowledge 不等于 resolved；策略删除或来源/能力撤销把告警置 `suppressed`，不能伪装成库存恢复。
- outbox 采用 at-least-once 投递和稳定幂等键；页面消费成功后记录 delivery，不因页面刷新重复创建事件。

### 9.4 验收

- 相同快照重复运行、调度并发和进程崩溃恢复都只保留一个 open 告警与一个首次触发事件。
- 低库存、恰好阈值、恢复、再次下降、stale、扫描缺页、无阈值、单位冲突和共享库存池均有状态机测试。
- 三店共享 100 件仍只按实物库存池计 100；渠道 0/实物充足只产生配额候选，不误发采购告警。
- 策略请求两层而只有 `physical_total` 核验时：runner 递给 graph 的 `levels` 只有 `physical_total`，且图上下文的店铺授权投影为空、池授权只含策略在册池——底层仓库里即使存在过期/不完整的渠道快照、渠道独有 SKU 与无档案店铺，本轮对渠道快照零查询，载荷（行、`data_as_of`、`source_batches`、去重全集）与 fingerprint 里零出现；实物层照常 triggered/updated/resolved，渠道层零告警、零 outbox 行、零通知，固定诊断与固定计数里能看到该层未核验（不是靠 graph 的渠道占位行）；在载荷行集里给出一条缺 `pool_ref`/`warehouse_ref` 的实物行时，实物层其余格子仍可首发。只核验 `shop_sellable` 时对称成立：池授权投影为空、对实物快照零查询，实物侧事实进不了载荷与指纹。
- 已核验层级被撤销登记后：该层 open/acknowledged 告警走 `suppressed`，`resolved` 计数保持 0；恢复事件必须等到新的、同层级、fresh 且完整的扫描。
- 不出现任何“因缺数据而发”的用户通知：缺失层级只增加诊断与指标，不写 outbox。
- `inventory_snapshot_missing` 按本轮真正在跑的范围双向用例：实物侧用例是在册池缺 `inventory/pools[]` 声明 ⇒ 去掉 `physical_total`；店铺侧用例只在 `shop_sellable` 本轮在跑时成立——`shop_not_synced` 伴随的同码只去掉 `shop_sellable`；物理轮（空店铺投影）里店铺侧产生点结构上不存在，同码出现即投影契约违约、当聚合码本轮去掉全部已核验层级，不得默认归给实物一层，也不得因此把已核验实物层永久弄哑，更不得据此把请求或投影改宽。
- 缺 `pool_ref`/`warehouse_ref`（实物）或 `shop_ref`（渠道）的 graph 行：不进入状态机输入、不生成去重键、零 triggered、零 resolved、零 outbox，只出现固定诊断码与固定计数；用例必须盖住已核验实物层里的 `unknown` 占位行。
- 构造一份 `status=partial`（截断时 `all_safe` 必为 false，所以不会是 `ok`）、带 500 个期望项与 20 行展示投影的来源 Artifact：本轮零 triggered、零 resolved、零 outbox，open/acknowledged 保持原状态，且固定计数能看到这一轮。
- 越权用户看不到告警或关联名称；日志和 outbox 不含真实底层 ID、DSN 或快照证据原文。
- monitor 图契约修订（空店铺投影下节点 1 不再以 `inventory_scope_empty` 终止、授权池对按池授权集直接派生）只覆盖「上下文无店铺授权且请求不含 `shop_sellable`」的形状；聊天路径行为逐字不变（原库存测试零回退）。
- 调度关闭、来源被撤销或策略失效后不再产生新告警，现有历史仍可审计。

## 10. 文件和数据边界

五份实施计划必须保持以下模块边界，具体文件名和迁移号在写计划时根据最新 HEAD 固定：

| 子项目 | 主要责任边界 | 允许消费 | 禁止依赖 |
| --- | --- | --- | --- |
| 语义目录 | metadata registry、检索、schema 预检 | reporting 契约、metric/source 版本 | 业务事实行、动态开表权限 |
| 受控 SQL | AST 校验、只读执行、探索 Artifact | SemanticSelection、DomainContext | 底表权限、任意模型 SQL 直通 |
| 学习记忆 | 审核生命周期、版本过滤、few-shot 检索 | 成功运行引用、规范化请求 | 原始聊天自动学习、临时业务值 |
| 隔离分析 | Artifact 校验、确定性分析、解释 | 已持久化安全数据集 | DB 连接、同步与行动工具 |
| 库存通知 | 调度、告警状态机、outbox、应用内展示 | InventoryWatchGraph、已批准策略 | 未认证来源、外部通知凭证 |

共享类型优先放在现有 `runtime` 中真正被多个领域消费的位置；领域私有类型留在自己的包，不新增笼统 `common.py` 或第二套 Store。跨项目只通过本设计列出的稳定接口和 Artifact ref 交互。

## 11. 测试与发布策略

每份实施计划按 TDD 拆成可独立提交的任务，至少包含：

1. 纯契约和边界单元测试；
2. 启用测试 DB 的权限、迁移和持久化测试；
3. Agent/tool 路由与 deadline 集成测试；
4. 脱敏、越权、版本失效和 Artifact 兼容回归；
5. feature gate 关闭时的原行为回归；
6. 日期化验收报告和 runbook 更新。

语义目录 → SQL → 记忆严格按依赖顺序实施。隔离分析在 Task 11 门禁通过后可以作为独立泳道实施，不等待语义目录，也不共享未发布接口。库存通知的代码计划可以提前评审，**生产实现与实际启用必须等 §9.1 的逐层来源门禁与 §4 发布门禁满足**。

2026-09-17 所有者决定：在库存来源验收仍为 FAIL、生产保持 disabled 的前提下，只解除“不得写后置功能代码”的本地实施限制，允许按 `docs/superpowers/research/2026-09-17-continuous-inventory-local-development-exception.md` 实施库存通知计划 Task 1–6：只跑本机 `*_test` 库、离线与 stub 测试，`INVENTORY_MONITOR_ENABLED` 默认 false，迁移 023 只在本机测试库执行。该例外不修改本节任何发布标准，不把未执行验证记为通过，也不替任何层级宣告来源就绪。

计划文件固定为：

- `docs/superpowers/plans/2026-09-14-semantic-catalog-and-schema-retrieval.md`
- `docs/superpowers/plans/2026-09-14-controlled-sql-exploration.md`
- `docs/superpowers/plans/2026-09-14-approved-query-memory.md`
- `docs/superpowers/plans/2026-09-14-isolated-analysis-agent.md`
- `docs/superpowers/plans/2026-09-14-continuous-inventory-notifications.md`

当前运营工作流计划的“后置子项目”在五份计划完成后改为链接索引，并保留 Task 11 前置门禁；不把后置计划的存在写成已经交付。

## 12. 明确不在本轮计划内

- 通用 Text2SQL、任意表问答、模型直连数据库或自动创建视图；
- 向量数据库、自动 embedding 管道或跨公司共享记忆；
- 自动批准学习样例、在线微调或从用户纠错直接修改生产规则；
- 任意 Python/JavaScript 执行、联网研究型 Agent 或自动业务决策；
- 自动采购、调拨、改价、投放、邮件、短信、IM 或 webhook 通知；
- 为使验收通过而虚构上架价、库存、广告实耗、归因、净利润或平台支付来源。
