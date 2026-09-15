# 受控 SQL 探索：本地验收记录

日期：2026-09-15（文件名按[实施计划](../plans/2026-09-14-controlled-sql-exploration.md) Task 6 的
`2026-09-14-controlled-sql-acceptance.md` 索引；实测完成于 2026-09-15）。
基线：`main`，HEAD `151ce4fd905e5bb19a1ea9af670ff2021555367b`（`feat: run controlled sql
explorations`，Task 1–5 已提交）。本轮文档与验收**不改动任何 `backend/**` 实现**：实现切片只
有一份通过评审的测试文件改动（见 §10）。
范围：**仅本机 `*_test` 数据库与离线/合成环境**。本文不构成发布就绪声明，也不改变 Task 11
统一发布门禁的状态（其 4–7 项仍 open）。

口径依据：[Task 11 后置子项目总设计](../specs/2026-09-14-post-task11-subprojects-design.md)
§3、§4、§6、§10–§12；[受控 SQL 探索实施计划](../plans/2026-09-14-controlled-sql-exploration.md)
Task 1–6；[受控 SQL 本地开发例外](2026-09-14-controlled-sql-local-development-exception.md)；
[Task 11 日期化发布验收](2026-09-14-task-11-release-acceptance.md)；
[语义目录与 Schema 检索本地验收](2026-09-14-semantic-catalog-acceptance.md)；
[bi-agent 开发流程](../../../.agents/skills/bi-agent-development-workflow/SKILL.md)。

## 0. 一句话结论

**受控聚合探索（计划 Task 1–6）在 HEAD `151ce4f` 上通过本地验收：Task 6 专项 24 项与现有
feature-off / 权限 / 迁移检查同跑 135 项零失败零跳过；全量后端 `discover` 1251 项零失败零跳过；
离线 26 题 26/26（两门默认关闭，固定 Tool 的调用数与结果不变）；前端 82 项与 `tsc -b && vite build`
零失败。十四行放行用例全部跑完真链路（编译 → AST → 真 EXPLAIN 成本门 → 只读执行 → 安全
投影），每一格同时等于“矩阵里那段手写基准 SQL”与“按 seed 手算的字面量”两份独立期望，并钉住
了 `Decimal` 标度（12 / 6 / 4 位）、真零与缺值不互代、分组不被合并、行序由分组键确定、真店号/
池号/SQL 原文不进公开载荷；46 行攻击全部在碰库之前被拒（31 个独立原因码），三行固定 Tool
可表达的问题在第二个节点就收口、探索语句数为 0；`R01` 上架快照时效行先人手算得出（2 组、
计数 `[2, 1]`）再拿到 `exploration_aggregate_not_permitted`，且 `estimate_plan` / `execute_plan`
调用数为 0。本轮没改一行产品代码、没升目录版本、没动锁文件，两个 feature gate 仍默认 `false`。**

缺的是**外部证据而不是代码路径**：真实 provider、目标环境迁移与授权、生产启用、部署与试用
一律记 `未执行`（§9）。Task 11 统一发布门禁的 4–7 项仍为 open（本项本地开发例外只解除
“允许写本地代码与本地取证”，见 §9 第 13 条）。

## 1. 环境（实测版本）

| 项 | 实测值 |
| --- | --- |
| 仓库 / 分支 / 基线 HEAD | `D:/Projects/bi-agent`、`main`、`151ce4fd905e5bb19a1ea9af670ff2021555367b` |
| Python | 3.11.16（uv 管理的 cpython-3.11-windows-x86_64） |
| 包管理 | uv 0.12.7（命令统一带 `--locked`；本轮未增删依赖、未改锁文件） |
| 其他锁定的直接依赖 | pydantic 2.13.5、psycopg 3.3.5 |
| sqlglot（锁定） | `pyproject.toml` 范围 `>=27.14,<28`；`uv.lock` 唯一解析 `27.29.0`（sdist + wheel 均带 sha256） |
| 语义目录版本 | `semantic/2026-09-14.1`（`SEMANTIC_CATALOG_VERSION` = `CATALOG.version`；本轮未升版本、未改目录） |
| 探索模板版本 | `exploration-sql/2026-09-14.1`（`bi_agent.exploration.policy.EXPLORATION_TEMPLATE_VERSION`，写进 `ValidatedQueryPlan.template_version` 与诊断的 `template_id`） |
| PostgreSQL 服务端 | `17.6 (Debian 17.6-2.pgdg13+1)`（本机 docker `postgres:17.6`，宿主端口 54329，`deploy/postgres` compose 工程；`SHOW server_version` 实测） |
| 测试库 | `bi_agent_test`（本机 `localhost`），DSN 只由 `backend/.env.test` 注入：`BI_TEST_ADMIN_DSN` / `BI_TEST_READER_DSN` / `BI_TEST_SYNC_DSN`（本文与任何命令输出都不打印 DSN） |
| 运行角色 | `bi_app`（成本门与只读执行在 `SET LOCAL ROLE bi_app` 下跑）与 `bi_reader`（自己的连接跑同一道门），见 §5 |
| Node / npm | v22.23.2 / 10.9.8（前端未改动，仅跑回归） |
| 两个 feature gate | `SEMANTIC_CATALOG_ENABLED=false`、`CONTROLLED_SQL_ENABLED=false`（`.env.example` 就是这两个值；本轮未翻任何一门） |

## 2. 验收矩阵（Task 6 Step 1）

矩阵冻结在 `backend/tests/test_exploration.py`：`ACCEPTANCE_CASES` 共 18 行 = **14 行
`permitted`（P01–P14）+ 4 行负例**（`R01` 上架快照时效、`F01`–`F03` 固定 Tool 优先）。计划下限
是"至少 10 个允许问题、至少 20 个攻击问题"，实际 14 / 46。

每一行 `permitted` 都跑完整受控链路：`compile_query` → `validate_exploration_plan`（AST 策略）
→ `estimate_plan`（真 `EXPLAIN (FORMAT JSON)` 成本门）→ `execute_plan`（`SET TRANSACTION READ
ONLY` + `SET LOCAL statement_timeout = '5000ms'` + `limit + 1` 溢出判定 + 列身份逐字比对）→
`project_result`（内部列换 opaque 引用、封闭类型表、262144 字节预算）。结果与**两份独立写出的
期望**逐格比对：① 矩阵里那行人手写基准 SQL 的返回值；② 按 fixture seed 内容手算的字面量
（`MATRIX_FIXTURE_EXPECTATIONS` / `MATRIX_COLUMN_TYPES`）。期望值不取自编译器 SQL、不取自投影
结果，也不复用被测代码的算术。

### 2.1 十四行放行（全部在同一台机器、同一个 `bi_agent_test` 上执行）

| 用例 | 主题（覆盖面） | 事实视图 | 授权域 | 指标 | 分组维度 | 这行钉的是什么 |
| --- | --- | --- | --- | --- | --- | --- |
| P01 | product_cost_coverage | `view-product-cost-daily` | 店铺 | `metric-cost-total` + `metric-sales-amount` | 日、店铺 | 两个指标同一基表在**一条**语句里各聚一次（不逐指标往返）；`cost_total` 在视图里已 `coalesce(…,0)`，所以只对照数值与分组，不宣称成本覆盖完整 |
| P02 | product_cost_coverage | `view-product-cost-daily` | 店铺 | `metric-cost-total` | 日、行性质 | 行性质是维度列：父项与子件各成一组，不靠名字合并；同一天两组同时存在 |
| P03 | product_cost_coverage | `view-product-daily` | 店铺 | `metric-sold-quantity` + `metric-gift-quantity` | 日、店铺 | 成交件数与赠品件数是两份口径：赠品不并入销量，也不从销量里扣 |
| P04 | product_cost_coverage | `view-product-daily` | 店铺（两个值） | `metric-gift-quantity` | 平台（跨视图） | 唯一放开的跨视图粒度：目录登记的 `many_to_one` 店铺档案边，且本轮检索已选上它；两店各一组、数值不因 JOIN 放大 |
| P05 | erp_document_normalization | `view-erp-document-daily` | 店铺 | `metric-erp-document-cost` + `metric-erp-gross-profit-reference` | 归一化状态 | 整组无成本 ⇒ 那一格是 `null` 而不是 `0`；确有 0 成本的那组发 `0.000000`。`normalization_status` 列本身 NOT NULL，缺状态在库里构造不出来，也不拿占位字符串冒充空值组 |
| P06 | erp_document_normalization | `view-erp-document-daily` | 店铺 | `metric-erp-document-cost` | 归一化状态、店铺 | 店铺授权列参与分组时只能以 `shop-ref` 出列：真店号不进结果、不进日志 |
| P07 | payment_flow_breakdown | `view-payments` | 店铺 | `metric-payment-flow-amount` | 币种 | 明细面口径与 `v_shop_daily` 的已支付金额不是同一个数；`JPY` 组缺值发 `null`、`EUR` 组真零发 `0.000000` |
| P08 | payment_flow_breakdown | `view-payments` | 店铺 | `metric-payment-flow-amount` | 是否已核验 | `verified` 列 NOT NULL，可空的是 `amount`：未核验且金额未知的流水不并入有金额的组，整组无金额发 `null`——未核验不等于 0，也不等于已核验 |
| P09 | payment_flow_breakdown | `view-payments` | 店铺 | `metric-payment-flow-amount` | 支付时刻 | 时刻列也是半开区间的轴，窗口两端只走参数；`paid_at IS NULL` 的行不进任何组 |
| P10 | refund_match_breakdown | `view-refunds` | 店铺 | `metric-refund-record-amount` | 是否已匹配、平台是否成功 | 两个布尔维度各成一组（四组齐发），未匹配/未成功的金额不并进成功组；只聚平台原始面 |
| P11 | inventory_snapshot_completeness | `view-channel-stock-items` | 店铺 | `metric-channel-sellable-quantity` | 快照抓取时刻 | 渠道快照行只在回滚事务内 seed：这行只证明 sum 聚合链路，不证明来源就绪，也不证明快照完整 |
| P12 | inventory_snapshot_completeness | `view-channel-stock-items` | 店铺 | `metric-channel-sellable-quantity` | 店铺、计量单位 | 店铺列以 `shop-ref` 出列；`piece/box/set` 各一行，不做单位换算也不汇总成一行 |
| P13 | inventory_snapshot_completeness | `view-physical-stock-items` | **库存池** | `metric-physical-available-quantity` + `metric-inbound-quantity` + `metric-locked-quantity` | 快照抓取时刻 | 三个量各聚一次、不互相加减；`inbound` 整组无值发 `null`、`locked` 真零发 `0.0000`。按池授权只在编译→策略→执行→投影这条链上取证，不构成来源就绪 |
| P14 | inventory_snapshot_completeness | `view-physical-stock-items` | 库存池 | `metric-physical-available-quantity` | 计量单位 | 单位是维度而不是换算系数：不跨单位求总量 |

逐格断言的形状：列声明 `(ref, data_type)` 逐项相等 + 行序敏感（列表相等而不是集合相等）+
`Decimal` 标度按数据库给的位数逐字比（12 / 6 / 4 位小数各自可判）+ 缺值为 `None` 而非 `"0"` +
时刻列只判"带时区的 ISO 文本且按分组键严格升序"。作用域换算只走测试自己按
`ref_for_key("shop", …)` 从**本轮真正授权的那些值**建出的映射，不写死句柄、不拿整份载荷搜子串。

夹具全部写在 `connect_test_db` 的外层回滚事务里（`bi.shops` / `bi.orders` / `bi.order_items` /
`bi.order_payments` / `bi.aftersales` / `bi.channel_stock_*` / `bi.inventory_pools` /
`bi.physical_stock_*` / `bi.listing_*`），窗口取数据库自己的当日往前七天（`[current_date -
window_days - 1, current_date - 1)`，让"未来抓取"那两条 018/019 CHECK 不会被跑在正午之前的用例
误伤），作用域是用例 id 导出的合成店/池 id。`test_seeded_rows_never_escape_the_rollback_transaction`
用第二条连接反证种子没有跨过本用例事务。

### 2.2 四行负例（不计入那十个允许问题）

| 用例 | 期望 | 实测 |
| --- | --- | --- |
| `R01` 按快照抓取时间看上架价（`view-listing-items`，`metric-listing-price`） | 编译层 `exploration_aggregate_not_permitted`，且在**任何探索执行层语句之前** | 先只跑那行人手写的反事实基准（`avg(list_amount)` 按 `captured_at` 分组，2 组、计数 `[2, 1]`，证明"人手能算出来"），再编译 → `exploration_aggregate_not_permitted`；`estimate_plan` / `execute_plan` 的替身地雷调用数为 0，该用例在库里只发过基准自己的三条语句（门禁 `SET` × 2 + 那条基准），没有任何 `EXPLAIN` |
| `F01` 按日期看买家已支付金额 | `fixed_tool_available`，固定 Tool = `query_business` | 图在第二个节点 `authorize_scope` 就收口，`EvidenceConn` 记录的语句数为 **0**，无 Artifact、无诊断 |
| `F02` 按店铺看上架价 | `fixed_tool_available`，固定 Tool = `audit_listing_prices`（上架价的固定路径不变） | 同上；该行只按固定 Tool 优先归因，不拿编译器替它归因（同一份选择丢给编译器也会因 `avg` 被拒，若混用就会让固定 Tool 优先这条规则静默失去覆盖） |
| `F03` 按库存池看实物可用量 | `fixed_tool_available`，固定 Tool = `inspect_inventory` | 同上（固定 Tool 那道门在池授权检查之前） |

`R01` 是**已批准的 fail-closed 映射**：目录给上架价/活动价登记的默认聚合是 `avg`（比值型的量，
真实值要分子分母各自求和再相除），而编译器只放 `sum/count/min/max`。本轮**没有**为此改编译器、
语义目录或目录版本；它作为独立负例行存在，不占那十个名额，`audit_listing_prices` 仍是上架价的
固定路径。库存快照的时效/完整性只通过真正放行的 `sum` 型渠道/实物指标覆盖（P11–P14），且只
证明聚合链路，不证明来源就绪。

### 2.3 攻击语料汇总

`backend/tests/exploration_attacks.jsonl` 当前 **46 行**（下限 20），逐行 `{id, sql, reason}`，
覆盖星号投影、多语句、CTE 写、服务端函数、CROSS JOIN、DML/DDL/COPY/CALL/DO/锁、注释混淆、
相关与派生子查询、UNION/INTERSECT/EXCEPT、未登记 schema/view/column/function、未登记 JOIN、
越权谓词、缺 limit、参数改写与不安全文本等。汇总 runner 逐行断言被拒原因码逐字等于
`exploration_<reason>`，且策略的 `conn` / `store` 是"碰一下就判红"的替身——**每一条都在数据库
执行之前被拒**；独立原因码 **31** 个（下限 20，防止一个万能码收完整个语料）。

## 3. 允许面边界（供 `docs/metrics.md` 引用）

- **视图**：只有已发布语义目录里的 11 张 `reporting.*` 视图（`semantic/2026-09-14.1`）；
  `bi.*` 底表、`information_schema` 里出现的新对象都不进目录，也没有"自动发现即登记"这条路。
- **聚合**：编译器只放行 `sum`、`count`、`min`、`max`（`PERMITTED_AGGREGATES`）；
  `avg` 不授权——比值型量必须分子分母各自求和再相除，那是单列一个聚合发不出的形状。指标能
  用哪个聚合还受目录 `allowed_aggregates` / `default_aggregate` 双重约束。
- **分组列**：角色必须是 `dimension` / `time` / `authorization`；`measure` 与 `internal`
  （ERP 主键）一律不可分组，也不作为公开列出。
- **JOIN**：只有目录登记的 4 条 `many_to_one` 店铺档案边，且跨视图分组只允许取
  `field-shops-platform` 这一列；`view-payments` ↔ `view-refunds`、商品毛利 ↔ 单据毛利、
  实物库存 ↔ 渠道库存故意没有边。
- **形状**：单条非递归 `SELECT`；一个事实粒度（跨粒度 `exploration_multiple_fact_grains`）；
  每个指标恰有一个必需字段；必须有窗口（`exploration_window_required`）；授权集合非空
  （`exploration_scope_empty`）且只能进参数。
- **固定 Tool 优先**：只要有一个固定 Tool 能同时覆盖本轮全部指标与全部分组粒度，就不开探索
  入口（`fixed_tool_for` 在 `authorize_scope`、任何 SQL 之前）。该判定**不看**能力/覆盖/澄清：
  固定 Tool 会正确拒答的问题，不许换一条 SQL 绕过同一个门禁。

## 4. 预算与执行门（代码常量 ↔ 计划数字）

| 门 | 取值 | 超限的码 |
| --- | --- | --- |
| 预计行数 | `MAX_ESTIMATED_ROWS = 50_000` | `ExplorationBudgetExceeded("estimated_rows")` → 运行层 `query_cost_exceeded` |
| 总成本 | `MAX_TOTAL_COST = Decimal("100000")` | `ExplorationBudgetExceeded("total_cost")` → 同上 |
| 语句超时 | `STATEMENT_TIMEOUT_MS = 5_000`（`SET LOCAL statement_timeout = '5000ms'`） | `exploration_statement_timeout` → `query_timeout` |
| 行数 | `MAX_ROWS = 500`（取 `limit + 1`，多一行即截断证据） | `exploration_row_limit_exceeded`（不返回被截断的偏低汇总） |
| 结果字节 | `MAX_RESULT_BYTES = 262_144`（投影**之后**的紧凑 JSON，UTF-8） | `exploration_result_too_large` |
| 整轮预算 | `context.deadline`（`time.monotonic()` 绝对时刻，30 秒），进库前要求容得下两道 5 秒门 + 两次 `DB_IO_RESERVE_SECONDS = 0.1` 的 IO 余量 + 1 秒收尾（`EXECUTION_RESERVE_SECONDS = 11.2s`） | `exploration_deadline_exceeded` → `deadline_exceeded` |
| 窗口 | `[start, end)`，至多 366 天（`MAX_WINDOW_DAYS`） | 请求契约拒绝 |
| 文本 | 单元格至多 4000 字符（`MAX_TEXT_CHARS`） | `exploration_text_too_large` |

## 5. 迁移 020、角色与隐私

- **部署顺序**：`020_controlled_sql_exploration.sql` 必须在 `004 → 009 → 014 → 015 → 016 →
  017 → 018 → 019` 之后执行（它重建 009/014 声明的三份 CHECK，读不到既有定义就
  `constraint_missing:<名>` 早失败），完整顺序见 `docs/runbook.md`。编号已冻结：`sql/` 下以
  `020` 开头的只有一份，`006` 仍作废。
- **只重建、不收缩**：三份白名单逐字重声明 009/014 的全部取值 + 本轮新增，并**保留库里当前已
  存在的额外取值**，因此独立泳道的 `022_isolated_analysis_artifacts.sql` 先跑也不会被 020 缩窄
  （`test_020_keeps_values_another_lane_already_added`）。新增取值：领域
  `controlled_sql_exploration`、Artifact `exploration_result`、终止原因
  `fixed_tool_available` / `schema_ambiguous` / `sql_policy_rejected` / `query_cost_exceeded`。
- **幂等**：全部是 `DROP … IF EXISTS` + 重建；`test_replaying_020_twice_never_narrows_the_whitelists`
  在本机 `bi_agent_test` 的管理员外层事务里重放两次并逐字比对约束定义（跑完按 Rollback 协议
  退出，共享库只保留已提交的那一份 020）。
- **角色**：`bi_app` 与 `bi_reader` 在 11 张登记视图上仍只有 `SELECT`；`bi_app` 对 `bi.*` 事实表
  仍无 `SELECT`；`pg_read_file` 一类服务端读取同样只剩稳定原因码。`ExplorationRoleDatabaseTests`
  不拿超级用户自证：成本门与只读执行在 `SET LOCAL ROLE bi_app` 后跑（并逐项断言
  `current_user = bi_app`、投影后不含真店号），同一道门另在 `bi_reader` **自己的连接**上过，
  且 `DELETE FROM bi.orders` 仍抛 `InsufficientPrivilege`。
- **SQL 隐私**：`sql_text` 与参数只进 009 建的 `bi.query_diagnostics`，通过 `run_id` 引用；020
  为它**不建任何 reporting 视图、不给 `bi_reader` 任何新授权**（`REVOKE ALL … FROM PUBLIC /
  bi_reader`，只 `GRANT SELECT, INSERT, UPDATE … TO bi_app`）。公开 Artifact 只保存安全结果与
  `statement_fingerprint`（SHA-256 五项：规范化 SQL、排序参数名、selected refs、授权集合摘要、
  目录版本）；模型消息、普通事件、应用日志与文档示例都不含 SQL 原文、DSN 或真实店铺 ID
  （矩阵的每行结果都断言原始店/池 id、`_shop_id`、`SELECT`、`reporting.` 与 `sql_text` 不出现在
  公开载荷里）。
- **写入失败即失败**：`record_diagnostic` 在 `save_artifact` **之前**；诊断写不进映射
  `persistence_failed` 并清空待发布结果，Artifact 写不进同样不返回成功。历史 SQL 只可查看，
  目录/模板版本一变就必须重新生成与验证（`exploration_catalog_version_mismatch`），旧结果
  不复用。

## 6. 两个 feature gate 与关闭面证据

| 门禁 | 默认 | 关闭时的可证行为 |
| --- | --- | --- |
| `SEMANTIC_CATALOG_ENABLED` | `false` | 不建预检连接、不发那条 `information_schema.columns` 查询、不校验目录（`tests.test_api.ApiTests.test_runtime_factory_makes_no_preflight_connection_when_disabled`） |
| `CONTROLLED_SQL_ENABLED` | `false` | `_exploration_gate` 根本不被调用，发给模型的 Tool 列表 JSON **逐字**等于关闭基线（6 个 Tool，无 `explore_business_data`）；`/api` 路由集合与用户可见面不变（`tests.test_api.ApiTests.test_the_feature_gate_changes_no_route_or_user_visible_surface`）；"变量缺席"与"显式 false"得到的设置逐项相等 |
| 依赖关系 | — | `CONTROLLED_SQL_ENABLED=true` 且 `SEMANTIC_CATALOG_ENABLED=false` → 启动即 `CONTROLLED_SQL_REQUIRES_SEMANTIC_CATALOG`；两个变量都只接受 `true`/`false`，其它写法（`TRUE` / `1` / `yes` / `on`）当场失败；`.env.example` 两份都是 `false`，并由用例钉住"示例文件带的是关闭值" |

本轮**没有**在任何环境把任一门禁打开；§7 的每一项都在两门关闭的默认配置下执行（探索层自身的
真库用例通过被测函数直接调用，不依赖聊天入口的门禁）。

## 7. 串行门禁命令与结果（`backend/` 与 `frontend/`，Windows）

所有命令逐条串行跑（这些套件共用 `bi_agent_test`：外层事务回滚，但种子行、advisory 锁与共享
表是全局态）。每行得到终态后立即回填，不把未跑写成通过。

| # | 命令 | 结果 |
| --- | --- | --- |
| G1 | `uv run --locked --env-file ../.env.test python -m unittest tests.test_exploration.ExplorationAcceptanceMatrixTests tests.test_exploration.ExplorationAcceptanceDatabaseTests tests.test_exploration.ExplorationAcceptanceSummaryTests tests.test_core.ConfigTests tests.test_core.ControlledSqlAgentIntegrationTests tests.test_api tests.test_runtime tests.test_runtime_db.ExplorationRoleDatabaseTests tests.test_runtime_db.ExplorationMigrationTests tests.test_runtime_db.ExplorationGraphDatabaseTests` | 已完成：`Ran 135 tests in 2.147s` → **OK**（135 passed / 0 failed / 0 error / **0 skip**）。分组：Task 6 专项 24（矩阵 12 + 真库链 9 + 汇总 3）、`ConfigTests` 10（两个门禁与 `.env.example` 默认关闭值）、`ControlledSqlAgentIntegrationTests` 13（关闭时 Tool 快照逐字不变）、`test_api` 13（路由面与关闭时不建预检连接）、`test_runtime` 54（运行契约与注册表/码表不漂移）、`ExplorationRoleDatabaseTests` 4、`ExplorationMigrationTests` 7（020 双次重放不收缩）、`ExplorationGraphDatabaseTests` 10（九节点链与真库持久化） |
| G2 | `uv run --locked --env-file ../.env.test python -m unittest discover -s tests -t .` | 已完成：`Ran 1251 tests in 20.428s` → **OK**（1251 passed / 0 failed / 0 error / **0 skip**）。相对语义目录验收记录的 984 项，差额全部是受控 SQL Task 1–6 的测试增长（`tests.test_exploration` 当前 228 项，另加那几个 Task 在 `test_core` / `test_api` / `test_runtime` / `test_runtime_db` 里补的接线与角色用例），不重新归因到旧记录。stderr 里那一行 `payment downgrade blocked… shop=S1` 是现有支付降级守卫用例自己预期的日志输出（合成夹具 id，不是真实店号），与本轮改动无关 |
| G3 | `uv run --locked --env-file ../.env.test python -m tests.acceptance --offline` | 已完成：`{"mode":"offline","total":26,"passed":26,"failures":0,"result":"pass"}`（Q01–Q26 逐题 `pass`，exit=0）。当前基线仍是修订后的 **26 题**（`backend/tests/questions.jsonl` 行数 26、id `01`…`26`，文件最后一次变更在受控 SQL 之前的 `e7d4298`）：本 Task 未扩题、未改任何预期。两门默认关闭下的固定 Tool 调用数、结果与原因码未因受控聚合探索发生任何变化（Tool 列表逐字不变由 G1 的 `ControlledSqlAgentIntegrationTests` 钉住）。runner 自己的注记同样成立：离线模式不证明模型理解准确率，也不构成任何平台真实来源就绪的证据 |
| G4 | `cd ../frontend && npm test -- --run` | 已完成：`Test Files 8 passed (8)`、`Tests 82 passed (82)`，duration 416 ms，零失败零跳过。前端未改动：受控聚合探索不新增任何前端卡片、Artifact 类型展示或路由（本轮与上一轮同样只跑回归） |
| G5 | `cd ../frontend && npm run build` | 已完成：`tsc -b && vite build` 成功，`✓ 38 modules transformed`、`✓ built in 456ms`，产物 `index.html` + CSS + JS 各一份；类型检查与生产构建零失败。构建产物落在已被 `.gitignore` 覆盖的 `frontend/dist`，不进气泡差异（见 §10 的 `git status`） |

跳过数核对（证明"跳过是环境门禁而不是掩盖失败"）：

| # | 命令 | 结果 |
| --- | --- | --- |
| G6 | G1 去掉 `--env-file`（并显式 unset `BI_TEST_ADMIN_DSN` / `BI_TEST_READER_DSN` / `BI_TEST_SYNC_DSN`） | 已完成：`Ran 135 tests in 0.244s` → **OK (skipped=34)**，0 failure / 0 error。逐条归因（`-v`）：skip 全部带原因 `未配置独立测试数据库`，落在 `ExplorationAcceptanceDatabaseTests` 9 + `ApiTests` 4 + `ExplorationRoleDatabaseTests` 4 + `ExplorationMigrationTests` 7 + `ExplorationGraphDatabaseTests` 10 = 34；其余 101 项仍跑，包括矩阵 12 项、两个门禁的配置面与 Agent Tool 快照面、策略层 46 行攻击与固定 Tool 拒答汇总。**缺 DSN 是干净 skip，不是 error**；反之带 `backend/.env.test` 时（G1）这 34 项全部跑，零 skip。 |

## 8. 已知偏差与交接（不静默改代码）

1. **计划示例 ↔ 已交付契约的映射**（延续 Task 2/3 已批准的口径，不改产品代码）：
   - 计划 Task 2 示例里的简写 `field-day` / `field-shop-id` / `field-platform`，在已发布目录里
     是视图作用域命名（`field-product-cost-daily-day` 等），`field-shops-platform` 才是平台列：
     一个 ref 只能属于一个视图，否则按 ref 建的索引会静默保留最后一个视图。
   - 计划矩阵里的 `metric-quantity` 在目录里没有条目（"销量"的 ref 是 `metric-sold-quantity`）：
     覆盖矩阵按请求 ref 做集合判断，因此该条目对今天的目录天然空转。把它改写成
     `metric-sold-quantity` 属于"新增 Tool 覆盖"，要改得先改计划——本轮零改动。
   - 计划 Task 6 Step 1 写的"listing snapshot 时效分组"是**覆盖面要求**而不是放行要求：它落在
     §2.2 的 `R01` 负例行（见已批准映射）。
   - 计划 Step 1 的"至少 10 个允许问题"实测 14 个；"至少 20 个攻击问题"实测 46 行 / 31 个
     独立原因码。
2. **阶段 1A → 1B 的夹具锚点修正（测试片内，已记录）**：阶段 1A 曾打算"从现有数据里挑行最多的
   作用域 + 尾部七天窗口"。真跑时发现它撑不起 Task 6 要的断言：两个库存快照视图在本库无行
   （锚点必然为空），而整组无值 / 真零 / 跨单位这些形状在现有数据里并不能保证存在——拿它当
   期望就是把没被证明的约定当基线。因此窗口改成纯时钟、作用域改成用例导出的合成店/池，并在
   同一个回滚事务里 seed 确定性行。冻结的 18 行契约（问题、指标、分组、JOIN、手算基准 SQL、
   列序规则）没有为此改动。
3. **`R01` 的归因唯一性**：同一份选择丢给编译器也会被 `avg` 拒。所以 `F02`（按店铺看上架价）
   只按固定 Tool 优先归因（那道门在编译之前），`R01` 只按聚合授权归因；两行不共用一个结论。
4. **共享库竞争是一个真实运维风险，不是用例缺陷**：本轮出现过一次"上一条被中途 kill 的用例
   留在 `bi_agent_test` 里一个 idle-in-transaction 后端，导致同一批专项命令从亚秒级排队到分钟
   级"的现场；跑完后 `pg_stat_activity`（`*_test`）无遗留后端、`pg_locks WHERE NOT granted`
   为 0。任何被中止的 DB 用例运行都应先确认没有遗留事务再跑下一条。
5. 沿用 Task 11 / 语义目录记录的未闭环项：模型消息持久化的字段级契约、来源绑定签名精确匹配、
   同步凭证校验位置、陈旧措辞（`docs/metrics.md` 首段与"四个领域"）等——本轮**不**顺手扫
   这些 P2。

## 9. 未执行清单（逐条，全部为 `未执行`，不写"预计通过"）

1. 真实 provider 的 `--provider-smoke` 与 26 题 `--live`（含模型版本、调用数、错误分类）：
   `未执行`。本轮模型侧只有 `.env.test` 里的桩配置，不构成真实模型证据；受控探索在真实模型
   下的"该不该选探索"判断也没有证据。
2. `CONTROLLED_SQL_ENABLED=true`（以及 `SEMANTIC_CATALOG_ENABLED=true`）在任何环境的启用与
   观察：`未执行`。两门默认关闭，本轮未翻门。
3. 生产 / 预发布环境的迁移核对（001 → 020）与 `bi_app` / `bi_reader` 授权实测：`未执行`
   （§5 的角色与幂等数字全部来自本机 `bi_agent_test`）。
4. 真实来源接口调用、历史回填、增量同步、`reconcile`、`capabilities --apply`：`未执行`。
5. 部署、反向代理 + OIDC 身份边界、Origin、SSE 断线恢复、每小时同步与每日重核任务注册：
   `未执行`。
6. 备份与一次真实恢复检查：`未执行`。
7. 首位运营用户一店一周试用与问题归档：`未执行`。
8. 真实上架价 / 实物库存 / 渠道库存的来源取证与逐元逐件对账：`未执行`——`listing_audit` 与
   `inventory` 来源注册表默认为空，真实部署仍只能报 `unsupported`。P11–P14 的放行**不**改变
   这一条：它们只证明 `sum` 型快照指标能被受控链路透传，不证明任何来源就绪。
9. 逐店付款时间口径与后台账单对照（`unmeasured` / 逐店 `time_basis` 登记）：`未执行`。
10. 拼多多任何形态的接入（连接器、来源登记、支付能力、凭证、onboarding）：永久不接入
    （2026-09-12 决定），本轮零改动，也不写"以后再说"。
11. 跨子项目/跨泳道：approved 查询学习记忆、隔离分析 Agent、持续库存通知：`未执行`（本地例外
    只解除受控 SQL 的本地实施限制）。
12. Task 11 统一发布门禁 4–7 项（真实 provider live、目标环境核对、部署/备份/恢复与一周试用）：
    **open**，本轮没有通过，也不因本文改变。
13. 生产库上的任何 DDL/DML、`REVOKE`/`GRANT` 变更：`未执行`（DDL 只由管理员在目标环境执行；
    本轮只在 `*_test` 上跑测试内的事务内重放）。
14. 手工 `psql` 双次重放 020：本轮 `未重跑`——同一断言由
    `ExplorationMigrationTests.test_replaying_020_twice_never_narrows_the_whitelists` 在测试库
    内自动执行（见 §5）；发布前仍应由管理员按 runbook 在目标库顺序执行并留证。

## 10. 范围、红线与本轮改动

- 允许并实际修改：`README.md`、`docs/runbook.md`、`docs/metrics.md`、本文、
  `docs/superpowers/plans/2026-09-11-data-and-query-closure.md` 的“受控 SQL 探索”索引条目一处。

| 文件 | 本轮改了什么 |
| --- | --- |
| `README.md` | 新增“受控聚合探索（默认关闭）”一节（固定 Tool 优先、纵深防御、预算与身份、隐私、默认关闭与依赖、只完成本地验收）；并把新克隆的迁移范围说明从“001 → 016”更正为“001 → 020”（与运行手册同一条顺序，属本 Task 加入 020 后的必要一致性） |
| `docs/runbook.md` | 迁移清单与完整顺序补上 `020`（含“缺 020 则持久化失败为 `persistence_failed`”的降级行为）；新增“受控聚合探索（默认关闭）”一节：两个门禁与依赖、`020` 的部署/幂等/回退、逐步稳定的诊断码表（`fixed_tool_available` / `schema_ambiguous` / 编译层 `exploration_*` / `sql_policy_rejected` / `query_cost_exceeded` / `query_timeout` / `deadline_exceeded` / `result_too_large` / `persistence_failed` / `forbidden`）、启用前置与回归命令；语义目录那一节的“完整顺序”加了一句“受控聚合探索另需 020”；“检查、故障与恢复”补上 `tests.test_exploration` 一条 |
| `docs/metrics.md` | 新增 §4.6：固定 Tool 优先矩阵（哪个 Tool 能覆盖哪些 ref 与哪几档分组）、逐视图的可探索指标与可分组列、四个不可探索指标与拒因、允许的 4 条 `many_to_one` 边与“只准取平台码”、预算与结果契约（500 行 / 262144 字节 / 50000 行 / 100000 成本 / 5s / 30s）、以及“不得从本功能读出的结论”（时效与完整性、能力/覆盖/basis 门禁归属、拼多多、本地不等于就绪） |
| `docs/superpowers/plans/2026-09-11-data-and-query-closure.md` | 只改“受控 SQL 探索”一条索引：补上本地实现与本地验收已完成、生产启用与真实模型未执行、验收记录链接、默认关闭与“受控聚合探索不是通用 Text2SQL”的措辞 |
| 本文 | 新建：环境、14 行放行矩阵逐行结果、4 行负例、46 行攻击汇总、允许面、预算与执行门、020 与角色/隐私、两个门禁的关闭面证据、§7 串行命令与终态、偏差与交接、未执行清单 |

- 本轮**未新增一次性探针**：§6 的“关闭面逐项相等”与 §7 的每一行都对应仓库内已有或将有的用例（`ConfigTests`、`ControlledSqlAgentIntegrationTests`、`tests.test_api` 的两条门禁用例，与 Task 6 的三个验收类），不依赖跑完即删的脚本。
- 测试切片（Task 6 唯一实现侧改动）：`backend/tests/test_exploration.py`（只增不改产品行为）。
- 未改动：任何 `backend/bi_agent/**` 实现、`backend/sql/**`、`pyproject.toml` / `uv.lock`、
  `frontend/**`、`.env*`、受保护路径（`.cloudflared.*`、`.streamlit.*`、`.tools/`、
  `taoxi-probe-status.py`、`可参考/`、`.worktrees/`）。
- 计划里的实现 checkbox **不勾选**：Task 1–6 的完成状态以本文与 Git 历史为准。
- 数据库侧只跑本机 `bi_agent_test`：全部写在显式回滚事务里，无写入残留、无 DSN 输出。
- 无 stage、无 commit、无 push（提交由主会话在父级验证后决定）；`git diff --check` 干净。
- 本文的数字只证明"默认关闭的受控聚合探索在本地正确、且不改变现有固定 Tool 行为"。**不宣布
  发布就绪、不宣布任何平台真实来源就绪、不宣布 Task 11 门禁 4–7 已通过。**

## 11. 建议的下一步（顺序即依赖）

1. 主会话按 §7 复跑并决定是否按单个逻辑提交收口本轮（子代理不提交）。
2. 若要把 §2 的"14 行放行"扩到新的粒度，先改计划的覆盖面再改矩阵——矩阵不接受现场加用例。
3. 下一个子项目（approved 查询学习记忆）需要**新的**所有者决定：本地例外不覆盖它。
