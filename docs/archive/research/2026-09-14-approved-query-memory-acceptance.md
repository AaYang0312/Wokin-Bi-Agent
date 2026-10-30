# approved 查询学习记忆：本地验收记录

日期：2026-09-16（文件名按[实施计划](../plans/2026-09-14-approved-query-memory.md) Task 6 的
`2026-09-14-approved-query-memory-acceptance.md` 索引；验证矩阵实测于 2026-09-16）。
基线：`main`，HEAD `38bf73cdbc5218c91e16e70b4f87b9ee77928ba9`（`feat: use approved examples
for safe routing`，Task 1–5 已提交）。本轮只改测试与文档：新增 6 条撤销/版本失效用例并把
1 条前端既有用例补上 revoke 调用断言（见 §10），**不改动任何 `backend/bi_agent/**` 实现、
迁移、配置、API、前端产品代码或依赖**。
范围：**仅本机 `*_test` 数据库与离线/合成环境**。本文不构成发布就绪声明，也不改变 Task 11
统一发布门禁的状态（其第 4–7 项仍 open）。

口径依据：[Task 11 后置子项目总设计](../specs/2026-09-14-post-task11-subprojects-design.md)
§3、§4、§7、§10–§12；[approved 查询学习记忆实施计划](../plans/2026-09-14-approved-query-memory.md)
Task 1–6；[approved 查询学习记忆本地开发例外](2026-09-15-approved-query-memory-local-development-exception.md)；
[Task 11 日期化发布验收](2026-09-14-task-11-release-acceptance.md)；
[受控 SQL 探索本地验收](2026-09-14-controlled-sql-acceptance.md)；
[语义目录与 Schema 检索本地验收](2026-09-14-semantic-catalog-acceptance.md)。

## 0. 一句话结论

**approved 查询学习记忆（计划 Task 1–6）在 HEAD `38bf73c` 上通过本地验收：全量后端
`discover`（带测试库环境）1370 项零失败零跳过；`tests.test_db` 96 项、`tests.test_runtime_db`
55 项、`tests.test_api` 29 项串行零失败零跳过；离线 26 题 26/26（门禁关闭），门禁开启注入
后同样 26/26；前端 107 项与 `tsc -b && vite build` 零失败。Task 6 新增的撤销与版本失效
证据全部落地：经 repository 撤销的 approved 样例在下一条检索中立即可见地消失（离线替身
+ 真库投影视图两个变体）；`VersionSet` 七个维度任改其一都零召回，且存储例保持
`approved`、版本要求原封不动、检索路径零写入零事件（离线 + 真库两个变体）；经替换
（supersede）的旧例从此不再出现在任何检索结果里。门禁关闭时聊天回合与无记忆基线逐字
相同；门禁开启时至多追加一段独立 system 段（至多 3 条、每条恰好七个安全键），成功运行、
模型失败、用户纠错与兜底路径都不写任何记忆表。审核 API 维持非审核者 403、功能关闭 404、
载荷无敏感词。`APPROVED_QUERY_MEMORY_ENABLED` 保持默认 `false`，回滚 = 把它保持/改回
`false`。**

缺的是**外部证据而不是代码路径**：真实 provider、目标环境迁移与授权核对、HTTP 生产启用、
部署与试用一律记 `未执行`（§9）。已知限制逐条见 §8。

## 1. 五个实现提交（Task 1–5，均已独立评审并被接受）

| 提交 | 计划切片 | 接受基线说明 |
| --- | --- | --- |
| `b4a70d2` | Task 1：严格契约、默认关闭门禁、迁移 021 | 事件表 UPDATE 授权按“不可变事件”收窄为 SELECT/INSERT，评审通过并注明 |
| `a4f5a5d` | Task 2：草稿来源净化与人工生命周期仓库 | 两条 P2 遗留（replacement 行无锁读取的 TOCTOU、草稿期不强制 400 字模板上限）随验收接受并延期，见 §8 |
| `3e77a5e` | Task 3：授权与版本过滤检索 + 30 题金标准 | 零重叠不召回取更严口径（`overlap > 0`），评审通过并注明 |
| `5958449` | Task 4：审核者 API 与独立审核面板 | 错误码 → 404/409/422 的逐对映射为固定常量，评审通过并注明 |
| `38bf73c` | Task 5：路由接入且无自动学习 | 五域当前版本形状与探索域 Tool 联动按父级批准的 Option B 固定 |

## 2. 环境（实测版本）

| 项 | 实测值 |
| --- | --- |
| 仓库 / 分支 / 基线 HEAD | `D:/Projects/bi-agent`、`main`、`38bf73cdbc5218c91e16e70b4f87b9ee77928ba9` |
| Python / 包管理 | 3.11.16 / uv 0.12.7（命令统一带 `--locked`；本轮未增删依赖、未改锁文件） |
| PostgreSQL 服务端 | `17.6 (Debian 17.6-2.pgdg13+1)`（本机实例；`SHOW server_version` 实测） |
| 测试库 | 本机 `bi_agent_test`（021 的两张表与投影视图在场）；DSN 只由 `--env-file ../.env.test` 注入测试进程，本文与任何命令输出都不读取、不打印 DSN 值 |
| Node / npm | v22.23.2 / 10.9.8（前端只补一条既有用例的断言，其余为回归） |
| feature gate | `APPROVED_QUERY_MEMORY_ENABLED=false`（`.env.example` 即该值；本轮未在任何共享环境翻门，门禁开启的 26 题证据经测试进程内注入，见 §6） |

## 3. 迁移 021 与角色授权摘要

- **对象**：`bi.approved_query_examples`（样例主表，含 `status` CHECK 四态、`approval_revision >= 0`、
  `cardinality(authorization_refs) > 0`、顶层绑定业务值 CHECK 与 `schema_version='021'` 记录）、
  `bi.approved_query_events`（不可变事件，`UNIQUE (example_ref, revision)`、事件种类 CHECK、
  理由非空 CHECK）、索引 `approved_query_lookup_idx (status, domain, intent_signature)`、
  投影视图 `reporting.v_approved_query_examples`（只含 `status='approved'` 行，11 列固定，
  不含 `source_run_id/status/created_by`）。
- **角色**：`bi_approver`（NOLOGIN 组角色，经独立审核 DSN 的会话成员使用）：样例表
  SELECT/INSERT/UPDATE，事件表仅 SELECT/INSERT（无 UPDATE/DELETE——事件不可变，沿 Task 1
  评审通过的收窄），`bi.query_runs` / `bi.query_provenance` SELECT，事件序列 USAGE/SELECT，
  `bi` schema USAGE；事实表（如 `bi.orders` / `bi.shops`）零授权（privilege 探针逐项钉住）。
- **`bi_app`**：两张底表 `REVOKE ALL ... FROM PUBLIC, bi_app`；只有投影视图的 SELECT，
  且对视图的 INSERT 同样被拒。聊天检索只读这一层投影，底表与事件表对应用身份不可见。
- **幂等**：角色经 `pg_roles` 探测创建，表 `CREATE TABLE IF NOT EXISTS`，视图 `DROP IF EXISTS`
  + 重建，索引 `IF NOT EXISTS`；测试内事务重放断言在 `tests.test_db`（本轮 96 项中含这些用例）。
  本机 `bi_agent_test` 保留命令行显式应用的那份 021，与 020 先例一致；本轮未做任何新的
  命令行迁移操作，其它环境一律未触碰。

## 4. 验证矩阵（全部串行；真实数字）

所有命令在 `backend/`（前端两条在 `frontend/`）逐条串行执行；DB 套件共写同一个本机
`bi_agent_test`，串行是硬要求。每行都是本轮实测终态，不把未跑写成通过。

| # | 命令 | 实测结果 |
| --- | --- | --- |
| G1 | `uv sync --locked` | `Resolved 27 packages in 1ms` / `Checked 26 packages in 1ms`：环境与锁文件一致，零变更 |
| G2 | `uv run --locked --env-file ../.env.test python -m unittest discover -s tests -t .` | `Ran 1370 tests in ~21s` → **OK**（1370 passed / 0 failed / 0 error / **0 skip**）。基线 1364 + 本轮 6 条新用例 = 1370。见 §10 的首次调用异常披露：共 9 次全量运行，第 1 次出现 1 个未能定位的单项失败，其后连续 8 次全部干净；本文引用的数字来自干净串行重跑 |
| G3 | `uv run --locked --env-file ../.env.test python -m tests.acceptance --offline` | `{"mode":"offline","total":26,"passed":26,"failures":0,"result":"pass"}`，exit 0：门禁默认关闭下 26/26 |
| G4 | `uv run --locked --env-file ../.env.test python -c "import functools; import tests.acceptance as a; a.answer = functools.partial(a.answer, approved_query_memory_enabled=True); raise SystemExit(a.main(['--offline']))"` | 同上 26/26，exit 0：门禁开启注入后 26/26（runner 文件零改动；空投影视图即无记忆基线，与 Task 5 的做法一致——测试不写记忆） |
| G5 | `uv run --locked --env-file ../.env.test python -m unittest tests.test_db` | `Ran 96 tests in 3.024s` → **OK**（94 + 本轮 2 条真库撤销/版本用例；0 skip） |
| G6 | `uv run --locked --env-file ../.env.test python -m unittest tests.test_runtime_db` | `Ran 55 tests in 1.479s` → **OK**（0 skip；生命周期真库用例不受影响） |
| G7 | `uv run --locked --env-file ../.env.test python -m unittest tests.test_api` | `Ran 29 tests in 0.764s` → **OK**（28 + 本轮 1 条 API 撤销用例；0 skip） |
| G8 | `npm test -- --run` | `Test Files 9 passed (9)`、`Tests 107 passed (107)`，duration ~400 ms，零失败零跳过（与 Task 4/5 基线同数：本轮只延长 1 条既有用例的断言，未新增用例） |
| G9 | `npm run build` | `tsc -b && vite build` 成功（`index` js 243.34 kB / css 27.63 kB，`✓ built in 433ms`），类型检查与生产构建零失败 |

聚焦面（本轮新用例的单项终态，均已含在上表总数内）：

| 命令（`backend/`） | 结果 |
| --- | --- |
| `uv run --locked python -m unittest tests.test_query_memory.QueryMemoryRevocationInvalidationTests -v` | 3/3 OK |
| `uv run --locked python -m unittest tests.test_api.QueryMemoryApiTests.test_revocation_via_the_api_is_immediate_and_terminal -v` | 1/1 OK |
| `uv run --locked --env-file ../.env.test python -m unittest tests.test_db.ApprovedQueryMemoryMigrationTests.test_repository_revocation_leaves_the_projection_before_the_next_retrieval tests.test_db.ApprovedQueryMemoryMigrationTests.test_single_version_dimension_change_stops_the_retrieval_match -v` | 2/2 OK |
| `uv run --locked python -m unittest tests.test_query_memory` | `Ran 78 tests` → OK（75 + 3） |

## 5. 检索边界证据（Task 6 Step 1）

### 5.1 撤销即时生效（离线替身 + 真库两个变体）

- **离线**（`QueryMemoryRevocationInvalidationTests.test_revocation_is_invisible_on_next_retrieval`）：
  写路径（`QueryMemoryRepository.transition`，生命周期替身）与读路径（`RetrievalConn` 重放
  `status='approved'` 投影视图语义）**共享同一批行对象**——repository 把 `mem-current-cost`
  改为 `revoked` 后，紧接着的下一次 `retrieve_approved_examples` 立即返回空：没有缓存宽限。
  撤销恰好追加一条不可变事件 `(revision=2, reviewer-a, revoked, 显式理由)`。
- **真库**（`test_db.test_repository_revocation_leaves_the_projection_before_the_next_retrieval`）：
  真实 `bi_agent_test` 上先证明 approved 行在 `reporting.v_approved_query_examples` 可见，
  经 `QueryMemoryRepository.transition` 撤销后同一条视图查询立即为空，底表行
  `(revoked, revision=2)`，事件表恰有 `(2, reviewer-a, revoked, 理由)` 一条。
- **API 面**（`test_api.test_revocation_via_the_api_is_immediate_and_terminal`）：撤销动作
  必带显式理由（`min_length=3`），响应即时反映 `(revoked, revision=2)`，列表立即返回撤销后
  状态；撤销是终态——再批准或再撤销都返回 409 `memory_transition_invalid`，且不追加事件。

### 5.2 七维版本单项失配零召回，且无自动升级

- **离线**（`test_each_version_dimension_invalidates_without_auto_upgrade`）：对 `VersionSet`
  的全部七个字段（schema / semantic catalog / data catalog / metric / policy / source registry
  / graph）逐个注入探针值，其余字段不动：每次检索都零召回。测试同时断言字段集合与
  `VersionSet.model_fields` 完全相等——将来加版本字段时这条断言当场红，不许静默漏维度。
  失配之后：存储例仍是 `approved`、`approval_revision=1`、版本要求逐键原封不动，检索路径
  零写入、零事件——**不存在自动升级或自动迁移**，版本兼容只能由人工重新审核恢复。
- **真库**（`test_db.test_single_version_dimension_change_stops_the_retrieval_match`）：用检索
  模块自己的 SQL（`reporting.v_approved_query_examples` 上 owner/domain/授权子集/
  `version_requirements = %(versions)s::jsonb` 四条过滤）在真库上重放：基线版本命中 1 行，
  七个探针各自零行；底表行保持 `(approved, 1, 原版本 JSON)`。

### 5.3 被替换（superseded）的样例永不召回

`test_superseded_examples_never_retrieve`：经 repository 以同域已批准 replacement 执行
supersede 后，旧例立即离开投影（后续检索为空），状态 `(superseded, revision=2)`，事件恰一条
`superseded` 且记录 replacement ref。加上 Task 3 既有覆盖（30 题金标准中的 draft / revoked /
superseded / 跨 owner / 跨授权域 / 七维失配各负例），“非 approved 状态零召回”在状态机驱动
与静态种子两个来源上都成立。

### 5.4 30 题金标准检索结果

`tests/fixtures/approved_memory_gold.jsonl` 共 30 题（六固定键），在两套确定性洗牌顺序
（seed 20260914 / 914）下 60 组子断言全部逐条复现期望 ref（`test_gold_cases_reproduce_expected_refs_in_two_store_orders`），
第三套顺序的集合一致性另有断言。覆盖：approved 命中 ×5（含跨 owner 正控、授权子集、
全角 NFKC、陈旧店铺行排除）、draft/revoked/superseded ×3、跨 subject ×1、跨授权域 ×2、
空授权域（零 SQL）×1、**七个版本维度单项失配 ×7**、domain 失配 ×1、零命中 ×1、注入 ×2、
坏候选 ×1（5 条毒行）、LIMIT 100 窗口 ×1、revision 对 ×1、空问题 ×1。夹具扫描断言无
`://`、postgres、password、secret、token、DSN 变量名等标记，全部内容为合成 ref 与槽位化
模板。

## 6. 门禁证据：关闭逐字不变，开启单段安全

- **关闭 = 无记忆基线**：`test_gate_off_never_reads_memory`（候选在场仍零记忆 SQL）与
  `test_gate_off_is_byte_identical_to_the_baseline_turn`（完整消息元组、工具 JSON、SQL 日志
  与“变量缺席”基线逐字相同）；工作流层 `test_gate_off_and_absent_flag_run_identical_workflow_turns`
  在真库上复证。G3 的 26/26 即关闭门禁的题集证据。
- **开启 = 至多一段独立 system 段**：`test_gate_on_places_one_memory_segment_between_system_and_user`
  （消息面恰为 `[system, 记忆段, user]`，基础系统提示在前，至多 3 条、每条恰好七个安全键：
  example_ref / intent_signature / question_template / slots / normalized_request / expected_tool /
  approval_revision）；`test_gate_on_keeps_the_fixed_tool_snapshot_byte_identical`（六 Tool
  快照逐字不变，固定 Tool 永远优先，`test_fixed_tool_wins_and_a_disabled_tool_is_never_referenced`
  钉住未开放的探索 Tool 既不被引用也不被检索）；记忆段不进会话历史（后续回合只拿到一段
  新段，绝无陈旧副本）。G4 的 26/26 即开启门禁的题集证据（空投影 = 无记忆基线）。
- **预算**：检索共享本轮 30 秒绝对 deadline，剩余不足 2 秒整段跳过且零 SQL，绝不重置或
  延长预算（`test_retrieval_shares_the_turn_deadline_and_cannot_extend_it` + 毒连接零 SQL 用例）。

## 7. 无自动写入矩阵、API 门禁与载荷扫描

- **没有自动写入**：`test_no_chat_outcome_writes_memory` 结构性覆盖成功回答、模型失败
  （`ModelError`）、用户纠错（参数纠正）与确定性兜底四种结局——每一条触及 `approved_query`
  的语句都是对投影视图的 `SELECT`，无 `approved_query_events`，全程无 `INSERT/UPDATE/DELETE`；
  生命周期写入口只有审核 API → repository。真库工作流用例对门禁开启回合的每条语句做
  动词扫描：恰 4 条投影视图 SELECT、零写动词。点赞/纠错没有任何记忆写接口（Task 5 面内
  无新 Tool、无回调）。
- **API 门禁**：功能关闭时六条路由全部稳定 404 且零连接（`test_feature_gate_off_returns_stable_404_on_every_memory_route`）；
  非审核者读写在建立任何连接之前 403（`test_non_reviewer_cannot_list_create_or_decide`）；
  无身份 401 先行；所有写请求过既有 WebWrite 边界；审核读写只用独立 `approver_dsn` 短连接
  （每请求一建一关，与 `BI_APP_DSN` 不同由配置层强制，测试以两个不同 DSN 常量钉住调用次数）。
- **载荷敏感词扫描**：API 侧每个载荷用例断言渲染文本不含 `sql_text / rows / subject_id /
  shop_id / raw_prompt / owner_subject_id / authorization_refs / created_by / actor / reason /
  error_code / chat_messages / query_artifacts / dsn=` 与两个测试 DSN 常量；候选投影是剥离
  日期与 shop_refs 后的价值自由形状。前端渲染用例断言无绑定业务值（`2026-09-01`、`99.00`
  不出现）与同一组禁词。契约层另有 `hide_input_in_errors`（被拒输入不出现在错误文本）与
  数据库侧顶层绑定值 CHECK 的逐键探针（Task 1 既有证据，本轮 96 项 `test_db` 继续覆盖）。

## 8. 已知限制（逐条，不作缺陷隐瞒也不虚构成春）

1. **HTTP 生产启用被刻意延后**：`api.py` 的聊天入口不传 `approved_query_memory_enabled`，
   与已接受的 `CONTROLLED_SQL_ENABLED` 先例完全一致——门禁在 `run_chat_turn`/`answer` 层
   完整可达并被测试覆盖，但 HTTP 生产启用需要未来的所有者决定 + `api.py` 一行接线，
   本轮按接受口径不做。
2. **进程内计数器，无指标后端**：`query_memory_candidate_invalid_total`（检索层丢弃的坏候选数）
   与 `query_memory_retrieval_failed_total`（路由期整段检索失败次数）都是进程内定名整数，
   读取方先快照再取差值；仓库没有指标后端，本项也未新增依赖。`query_memory_retrieval_total`
   （门禁开启的检索尝试次数）当前没有独立计数器：每次开启门禁的回合至多发 4 条投影视图
   SELECT + 2 条目录版本读取（开放探索 Tool 时第 5 域再加一），读取量可由该固定上界与
   数据库侧视图查询计数推出；接入指标后端时应以该名称登记。详见 [指标口径](../../metrics.md)。
3. **探索运行在写入血缘前不合格**：`build_draft_from_run` 要求完整 provenance，而探索运行
   目前不写 `bi.query_provenance`，因此探索成功运行暂时不能成为草稿来源（Task 2 验收时
   批准的口径）。
4. **Task 2 两条已接受的 P2**（随验收延期，未在本轮扩大范围）：supersede 的 replacement 行
   读取未加 `FOR UPDATE`（与并发撤销 replacement 之间存在 TOCTOU，只影响审计 cosmetic；
   最小修法是 replacement 读取加锁）；草稿期不强制 400 字模板上限（超长模板可被批准但在
   检索层 fail-closed 丢弃，属质量缺陷不是安全缺陷）。
5. **catalog-0 早停运行按设计不可达**：business_query / commerce 的当前版本形状含现读的
   数据目录版本，早停运行冻结的 catalog 0 永远追不上，正是 §7.3 “版本变更须重审”的精确
   匹配语义，不是缺陷。
6. **本地开发例外范围**：本计划全部实现与证据都在
   [2026-09-15 本地开发例外](2026-09-15-approved-query-memory-local-development-exception.md)
   授权范围内；[Task 11 统一发布门禁](2026-09-14-task-11-release-acceptance.md) 第 4–7 项
   （真实 provider smoke 与 26 题 live、目标环境迁移与能力核对、可信身份/Origin/SSE/同步/
   脱敏日志/备份/真实恢复、一店一周试用）**仍为 open**，本文不改变其状态。

## 9. 未执行清单（逐条 `未执行`，不写“预计通过”）

1. 真实 provider smoke 与 26 题 live、真实模型下的记忆注入观察：`未执行`（门禁开启 26 题是
   离线桩模型证据）。
2. 任何共享/生产/预发布环境把 `APPROVED_QUERY_MEMORY_ENABLED` 置 `true`：`未执行`。
3. 生产或预发布库的 021 迁移执行与 `bi_approver` / `bi_app` 授权实测：`未执行`（本文 §3 的
   角色与权限结论来自本机 `bi_agent_test` 与 021 文本 + 测试探针）。
4. 真实来源接口、同步、回填、对账、部署、身份、备份、恢复、调度、凭据操作：`未执行`。
5. 独立审核 DSN 的真实 LOGIN 角色接线（本地测试用 privilege 探针而非切换会话身份）：`未执行`。
6. 一店一周试用与问题归档：`未执行`。
7. 跨子项目：隔离分析 Agent、持续库存通知：`未执行`（本地例外不覆盖它们）。

## 10. 范围、偏差与本轮改动

| 文件 | 本轮改了什么 |
| --- | --- |
| `backend/tests/test_query_memory.py` | 新增 `QueryMemoryRevocationInvalidationTests`（3 条）：撤销即时生效（共享对象的结构性证明）、七维版本单项失配零召回 + 无自动升级、supersede 后旧例永不召回 |
| `backend/tests/test_db.py` | `ApprovedQueryMemoryMigrationTests` 新增 2 条真库变体：repository 撤销后投影视图立即不可见；检索 SQL 七维单项失配零命中且行保持 approved |
| `backend/tests/test_api.py` | `QueryMemoryApiTests` 新增 1 条：API 撤销必带理由、立即反映、终态不可逆（重放 409 且零事件） |
| `frontend/src/components/QueryMemoryReview.test.tsx` | 既有 decide 用例补上 revoke 调用断言（URL 与 reason-only 请求体）；未改面板、未新增用例 |
| `README.md` | 新增“approved 查询学习记忆（默认关闭）”一节；新克隆迁移范围说明 001 → 021 |
| `docs/runbook.md` | 迁移顺序与本地初始化清单补 021；新增“approved 查询学习记忆（默认关闭）”运维一节（启用前置、草稿创建、显式理由、双人复核建议、撤销时效、版本重审、回滚）；检查命令补 `tests.test_query_memory` |
| `docs/metrics.md` | 新增 §4.7：三个定名指标的真实现状（两个进程内计数器 + 检索尝试的固定上界推导）、按状态/事件计数与审批冲突的只读观测查询、“不用问题文本作 label”与“不得从本功能读出的结论” |
| 本文 | 新建：提交清单、环境、021 与角色、验证矩阵实测数字、检索/门禁/无写入证据、已知限制与未执行清单 |

- **偏差披露（唯一一项）**：全量 `discover` 共运行 9 次；第 1 次出现 1 个单项失败，因输出
  截断未能定位（其后连续 8 次 1370 项全部干净，含父级在 `38bf73c` 的 1364 项基线验证）。
  按[运行手册](../../runbook.md)的串行重跑纪律，本文引用的是干净串行重跑数字；该一次性
  异常无法归因到本轮改动（本轮 6 条新用例全部零时序依赖、零共享全局态），作为残余风险
  记录在案。
- 计划里的实现 checkbox **不勾选**：Task 1–6 的完成状态以本文与 Git 历史为准。
- 本轮无 stage、无 commit、无 push（提交由主会话在父级验证后决定）；`git diff --check` 干净；
  受保护路径（`.cloudflared*`、`.streamlit*`、`.tools/`、`taoxi-probe-status.py`、`可参考/`、
  `.worktrees/`）全程未读取、未修改。
- 本文不含任何 DSN、真实聊天、真实店铺/商品名称、真实主键或密钥值；所有示例 ref 与理由
  均为合成值。

## 11. 建议的下一步（顺序即依赖）

1. 主会话按 §4 复跑并决定是否按单个逻辑提交收口本轮（子代理不提交：
   `docs: accept approved query memory`）。
2. 生产启用前必须先有：HTTP 接线决定、目标环境 021 与 `bi_approver` 会话成员接线、
   独立审核 DSN 供给，以及 Task 11 门禁 4–7 的完成——任何一项缺失都维持默认关闭。
3. 若要补 §8.4 的两条 P2，改动都收敛在 `repository.py` 一处（replacement 读取加锁、草稿期
   长度校验），应作为独立小切片走评审，不混入本文档轮次。
