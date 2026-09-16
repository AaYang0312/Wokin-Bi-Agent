# BI Agent

面向电商经营分析的内部聊天助手。它从快麦同步到 PostgreSQL 的已核验数据中查询经营指标，并可按用户明确假设测算推广预算。

开发主线已统一到 `main`，包含 FastAPI/查询状态图和淘系非敏感出库同步。查询能力仍按已验证来源开放；淘系查询门禁、跨平台口径与拼多多能力限制按[修订计划任务 5、11](docs/superpowers/plans/2026-09-07-ecommerce-bi-agent.md)继续实施。广告实耗、ROAS、净利润和全平台汇总仍须可靠数据来源。

## 架构

```text
快麦开放平台 → backend/ 同步进程 → PostgreSQL
                                  ↓
React/Vite 前端 → /api 代理 → FastAPI → 受限 Agent
```

前端只调用会话 CRUD 与消息 SSE。`query_business` 和 `evaluate_promotion` 是后端 Agent 的内部工具；没有手动查询、CSV 下载或通用 Text2SQL API。

## 语义目录（默认关闭）

`backend/bi_agent/semantic_catalog/` 是一份版本化的显式目录（`semantic/2026-09-14.1`：11 张已批准的 `reporting.*` 视图、84 个字段、22 个指标、10 个实体、4 条合法 JOIN 边）。它**只**回答“哪些已批准的结构可能表达这个问题”，对经营问题返回至多 5 个视图候选，供**后续**受控 SQL 探索使用（计划 Task 11 后置子项目 B）。

- 只为将来的受控 SQL 提供候选：不执行 SQL、不读业务事实行、不新增 Agent Tool 或路由、不授予任何数据能力，也不改变现有固定 Tool 的路由与门禁。
- 默认关闭：`SEMANTIC_CATALOG_ENABLED=false`（`.env.example` 就是该值）。显式 `false` 与“变量缺席”两种写法的启动与聊天行为已逐项比对一致（工具名与 schema、发给查询层的规范化请求、运行状态、Artifact 数量与确定性结果载荷），且关闭时不建任何数据库连接；该包只被 `api.py` 的启动预检导入，不新增 Agent Tool 或 HTTP 路由。
- 打开时启动多做一件事：用 `BI_APP_DSN` 建立一条 autocommit 连接，执行**一条** `information_schema.columns` 只读查询核对声明与真实 schema。不一致就让进程起不来，错误只有 `semantic_schema_mismatch:<稳定 ref>`，不带 SQL 标识符、列名、数据库原文或 DSN；绝不降级成“目录为空”。
- 模型侧只见 kebab-case 语义 ref；SQL 标识符只由服务端 `resolve_sql_identifier()` 解析。底表 `bi.*` 永不进目录，也不因该功能获得任何新授权。
- 目前只完成本地开发与本地验收（[验收记录](docs/superpowers/research/2026-09-14-semantic-catalog-acceptance.md)）；启用步骤、前置检查与回退方式见[运行手册](docs/runbook.md)。生产启用仍以 Task 11 统一发布门禁为前置，该门禁尚未通过。

## 受控聚合探索（默认关闭）

`backend/bi_agent/exploration/` 交付的是**受控聚合探索**：只为固定业务 Tool 无法表达、但来源能力与覆盖已满足的问题，生成**一条参数化的单基表只读 `SELECT`**。它不是通用 Text2SQL，也不存在“用户或模型写 SQL”的入口：模型只给稳定语义 ref 与业务值，SQL 标识符、授权集合、时间窗口与行数上限全部由服务端从已发布语义目录（`semantic/2026-09-14.1`）解析并注入。

- 固定 Tool 优先：只要有一个固定 Tool 能同时覆盖本轮全部指标与全部分组粒度，就不开探索入口，返回 `fixed_tool_available`。缺能力、缺覆盖、来源未认证、需要澄清或写请求同样不许降级成 SQL——换一条语句绕过固定口径的拒答就是绕过门禁。
- 纵深防御：只允许单条非递归 `SELECT`；只能命中目录里 11 张 `reporting.*` 视图；聚合只放 `sum/count/min/max`（比值型的 `avg` 不授权）；跳视图分组只允许目录登记的 4 条 `many_to_one` 店铺档案边上的平台码。`SELECT *`、CTE、子查询、UNION、未登记对象、多语句与 DDL/DML/COPY/CALL/DO/锁在碰库之前被 AST 策略拒（`sql_policy_rejected`）。
- 预算与身份：运行在 `bi_app`、`READ ONLY` 事务与 `statement_timeout = 5s` 下；先跑受控 `EXPLAIN`，预计行数 ≤ 50000 且总成本 ≤ 100000 才进执行；结果至多 500 行、安全投影后 ≤ 262144 字节；整轮仍是 30 秒 deadline。超预算或行数溢出整条拒，不返回被截断的偏低汇总。
- 隐私：SQL 原文与参数只进 `bi.query_diagnostics`（`bi_reader` 拿不到任何新授权，也不为它建 reporting 视图）；模型消息、普通事件、公开 Artifact 与应用日志都不含 SQL、DSN 或真实店铺 ID，店铺只以 opaque `shop-ref` 出现。
- 默认关闭：`CONTROLLED_SQL_ENABLED=false`（`.env.example` 就是该值），且只有 `SEMANTIC_CATALOG_ENABLED=true` 时才能开——否则启动即 `CONTROLLED_SQL_REQUIRES_SEMANTIC_CATALOG`。关闭时 Tool 列表逐字不变、根本不进门禁，路由与聊天行为无差异。
- 目前只完成本地开发与本地验收（[验收记录](docs/superpowers/research/2026-09-14-controlled-sql-acceptance.md)）：它只证明“放行/拒答的形状与边界”，不证明任何平台真实来源已就绪，也不改变 Task 11 统一发布门禁的状态。部署顺序（含 `020`）、两个门禁、诊断与回退见[运行手册](docs/runbook.md)，允许面边界见[指标口径](docs/metrics.md)。生产启用仍以 Task 11 统一发布门禁为前置，该门禁第 4–7 项尚未通过。

## approved 查询学习记忆（默认关闭）

`backend/bi_agent/query_memory/` 提供的是**人工批准的查询样例记忆**：只有授权审核者在独立审核面板上明确批准过的规范化样例，才会在门禁开启且版本、领域、授权域全部精确匹配时，作为至多 3 条 few-shot 提供给路由与参数规范化。它不是聊天历史搜索，也不是自动训练：成功运行、用户点赞和自然语言纠错都不会自动改变长期记忆，模型没有任何创建、批准、撤销或替换样例的入口。

- 记忆**能**做：为“固定 Tool 表达不了”的经营问题提供已审核的 Tool 选择与槽位结构示范，减少同类问题的路由摇摆。
- 记忆**不能**做：覆盖服务端身份、授权、能力、coverage、basis、来源与固定 Tool 优先级；样例只示范结构，当前值必须从本轮问题提取，任何模型输出仍走既有 JSON schema、授权与 capability 校验。
- 不保存原始聊天、真实店铺/商品/SKU 名称、真实主键、SQL 原文、结果行、DSN 或密钥；目标价、阈值、预算、日期区间和店铺选择一律是槽位，不得存成长期常量；样例投影只有稳定 ref、槽位化模板、规范化请求与工具名。
- 生命周期只有 `draft → approved → superseded/revoked`：批准、撤销、替换都必须由审核者给出显式理由并留不可变事件；撤销在下一条检索中立即可见，版本升级后旧例零召回、进入重审，不自动迁移。写路径走独立审核 DSN（`bi_approver` 最小权限），`bi_app` 只能读 approved 投影视图。
- 默认关闭：`APPROVED_QUERY_MEMORY_ENABLED=false`（`.env.example` 就是该值），启用还要求非空 `APP_APPROVER_SUBJECTS` 与不同于 `BI_APP_DSN` 的 `BI_APPROVER_DSN`。关闭时零记忆读取，聊天回合与无记忆基线逐字相同；回滚 = 把开关保持/改回 `false`。HTTP 生产启用与受控探索一样被刻意延后，需要单独的所有者决定。
- 目前只完成本地开发与本地验收（[验收记录](docs/superpowers/research/2026-09-14-approved-query-memory-acceptance.md)）：不证明真实模型下的检索质量，也不改变 Task 11 统一发布门禁的状态。启用步骤、审核流程与回退见[运行手册](docs/runbook.md)，定名指标与观测口径见[指标口径](docs/metrics.md)。

## 隔离分析（默认关闭）

`backend/bi_agent/analysis/` 是 Task 11 后置子项目 D：对当前用户仍有权读取的不可变数据集 Artifact（`metric_result` / `comparison_table` / `trend_series`）做确定性贡献拆解、变化拆分与 MAD 异常候选分析。金额、数量、占比、得分全部由 Decimal 纯函数计算并与 gold set 逐项一致；可选模型只用 `complete(..., tools=[])`（字面空工具列表，AST 钉住）总结已验证 finding，失败时仍发布确定性结果。无证据的因果/行动结论只能进入 `unsupported_claims`，永不作为事实发布；分析运行本身没有数据库、文件、网络或业务 Tool 能力（import 守卫在测试里逐模块扫描）。

- 结果以新 `analysis_result` Artifact 持久化并引用来源 fingerprint；持久化失败整次失败，不发布任何分析结果，来源 Artifact 一个字节都不动。
- 前端按白名单渲染：载荷先整体验形（键集、正则、边界与后端同一纪律），验不过整卡拒绝；数值原样展示不重算；假设标“待验证”，证据不足的说法单独折叠，限制常显；“查看来源数据”按钮只在同一条消息里按引用找到唯一匹配的数据集时可用，不 fetch、不读缓存。
- 默认关闭：`ISOLATED_ANALYSIS_ENABLED=false`（`.env.example` 就是该值）。关闭时不注册分析 Tool、不读取任何 Artifact、聊天与 Task 11 基线逐字相同；回滚 = 把开关保持/改回 `false`。迁移 `022` 只扩 domain/artifact 两份 CHECK，不建事实表。
- 目前只完成本地开发与本地验收（[验收记录](docs/superpowers/research/2026-09-14-isolated-analysis-acceptance.md)）：不证明真实模型下的叙述质量，也不改变 Task 11 统一发布门禁的状态（第 4–7 项仍 open）。HTTP 生产启用与受控探索一样被刻意延后。启用前置与回退见[运行手册](docs/runbook.md)，观测口径见[指标口径](docs/metrics.md)。

## 本地启动

需要 Python 3.11、Node.js 22、PostgreSQL 17。复制 `.env.example` 为 `.env.app`，只填写 API 所需的 `BI_APP_DSN`、店铺范围和一个模型 provider 配置；不要在其中放同步 DSN 或快麦凭证。

本机 PostgreSQL 由 `deploy/postgres` 的 compose 工程提供（`postgres:17.6`，宿主端口 54329，数据在命名卷）：

```powershell
cd deploy\postgres
docker compose up -d
```

备份还原、其他主机经 Tailscale 连接与安全边界见 [deploy/postgres/README.md](deploy/postgres/README.md)。

```powershell
Set-Location backend
uv sync --locked
uv run --env-file ../.env.app uvicorn bi_agent.api:create_runtime_app --factory --host 127.0.0.1 --port 8001 --reload
```

另开终端：

```powershell
Set-Location frontend
npm ci
npm run dev -- --host 127.0.0.1
```

访问 `http://127.0.0.1:5175`。Vite 会将 `/api` 转发到 `127.0.0.1:8001`，因此 `.env.app` 的 `APP_PUBLIC_ORIGIN` 必须为 `http://127.0.0.1:5175`。

数据库管理员必须在生产库和独立测试库中分别、按以下顺序执行迁移（测试库同样不能跳过运行追踪迁移）：

```powershell
psql -d bi_agent -f backend/sql/001_init.sql
psql -d bi_agent -f backend/sql/002_kuaimai_mapping_repair.sql
psql -d bi_agent -f backend/sql/003_kuaimai_metric_semantics.sql
psql -d bi_agent -f backend/sql/004_query_runtime.sql
```

它创建 `bi_sync`、报表只读身份 `bi_reader` 与 API 身份 `bi_app`，并建立可审计的查询运行记录。API 使用 `bi_app`，只能读取 `reporting` 视图和读写聊天与查询运行表。

本机两个库（`bi_agent`、`bi_agent_test`）已经建好并随命名卷保留，重建容器不需要重跑 DDL；新克隆时按[运行手册](docs/runbook.md)的完整顺序在两个库各跑一遍（001 → 022）。

## 数据同步

同步使用单独的 `.env.sync`：

```powershell
Set-Location backend
uv run --env-file ../.env.sync python -m bi_agent.sync incremental
uv run --env-file ../.env.sync python -m bi_agent.sync reconcile --days 7
```

同步与 API 不能共用环境变量文件。同步窗口失败会回滚并保留成功水位；没有完整覆盖的时间段不会伪装成零业务。

## 质量检查

```powershell
Set-Location backend
uv run python -m unittest tests.test_core -v
uv run --env-file ../.env.test python -m unittest tests.test_db tests.test_api -v
uv run --env-file ../.env.test python -m tests.acceptance --offline
Set-Location ../frontend
npm test
npm run build
```

离线验收使用合成数据，验证 20 个业务问题的工具参数、指标和边界；不代表真实模型理解已验收。Qwen 和 DeepSeek 的真实联调状态在 [运行手册](docs/runbook.md) 中单独记录。

指标口径见 [docs/metrics.md](docs/metrics.md)，合成数据演示及面试讲解路径见 [docs/demo.md](docs/demo.md)。
