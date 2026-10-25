# 2026-10-24 可用性与回归体系推进 —— 开发接续 checkpoint

计划：`docs/superpowers/plans/2026-10-24-bi-agent-reliability-and-evaluation.md`（T0–T8）。
执行分支：`reliability-evaluation-2026-10-24`（自 `main@dceaad8` 建出）。

## T0 — 核实现状与建立基线（2026-10-24，完成）

### 基线

- `baseline_revision`：`dceaad8afc703d989a676eb6091bcecfd2f29ac0`（main）。
- 用户 WIP（保留、不暂存、不提交，纯前端）：
  `frontend/src/App.tsx`、`components/ChatView.tsx`、`components/NotificationCenter.tsx`、
  `components/icons.tsx`、`styles.css` —— 把库存通知/审核记忆入口移入聊天顶栏并加图标的
  UI 改版（约 +200/-72 行）。本轮后端工作不碰这些文件；T7 若需改其行为，只新增测试或
  以最小方式叠加，绝不覆盖 WIP。
- `.zcode/plans/plan-sess_d154ea06…md` 是另一会话的快麦同步修复计划（涉 sync.py），
  与本轮无文件交集，记录不抢改。

### 10 月 9 日两项修复在开工源码中的状态（已实现，待 T4 验证）

1. 参考价快照筛选：`backend/bi_agent/commerce/repository.py` `_ITEM_PRICE_LATEST_SQL`
   —— 每店取最新一份 `scan_complete` 快照，只读商品级行（sys_sku_id=''）+ 调用方点名
   成交 SKU；目录行不参与归类、不顶 500 上限。既有测试：
   `tests/test_commerce.py:1142`（未售目录行多不把销售拒成 result_too_large）、
   `:1168`（成交 SKU 子集超限时参考价缺失、销售照发）。
2. 公开校验提示：`backend/bi_agent/commerce/tool.py:176` `_public_argument_hint` ——
   `profit_basis=existing_fields` 与 `platform_unregistered` 分别映射公开披露句，
   不进原始 Pydantic 输入。工具描述已写明毛利参考须先获用户口径确认。

### 环境能力

- uv 0.12.18 / Python（backend/.venv 既有）；`backend/uv.lock` 在。
- Node v22.23.2 / npm 10.9.8；`frontend/package-lock.json` 在。
- Docker CLI 29.7.2；守护进程开工时未运行，已启动 Docker Desktop 等待引擎就绪。
  若引擎不可用：T1 数据库验收标 blocked，纯函数/替身部分照常推进（计划 T1 预设）。
- 迁移：`backend/sql/` 001–033，编号有缺（006/010–013 作废），最新 033。

### 题数核对（开工源码）

- `backend/tests/acceptance.py:61` `EXPECTED_QUESTION_COUNT = 35`；`questions.jsonl`
  实测 35 行、ID 01–35 连续。旧文档（acceptance 首行 docstring「26题」、runbook
  「修订后的 26 题」）为历史文案，T8 更新文档时不沿用。
- `partial_answer_attacks.jsonl` 3 条；`exploration_attacks.jsonl` 46 条。
- 前端既有测试：`api.test.ts`、`partialAnswer.test.tsx`、
  `components/{ArtifactView,RemediationUpload,MessageView,ChartArtifact,…}.test.tsx` 等 13 个文件。

### 既有相关用例（后续任务优先复用）

| 主题 | 位置 |
| --- | --- |
| 参考价上限/无关快照 | `tests/test_commerce.py:1142,1168`、`test_product_ranking.py:301` |
| profit_basis 提示 | `tests/test_comparison.py`、`test_commerce.py`、`test_query_memory.py`、`test_operator_workflows.py` |
| 中文平台名反查 | `tests/test_channel_mapping.py`、`test_catalog.py`、`test_comparison.py`、`test_multi_source_metrics.py` 等 |
| 部分答案 | `tests/test_partial_answers.py`、`test_core.py`、`test_operator_workflows.py`、`test_sycm_metrics.py` |
| 上传/重查 | 后端 `tests/test_sycm_upload.py`、`test_sycm_import.py`；前端 `RemediationUpload.test.tsx` |

### 分类（T0 交付）

- **已实现待验证**：上表两项 10-9 修复；`llm.ModelReply.usage` 与 `ChatModel.complete`
  协议（T3 计量直接用）；acceptance 三入口（offline/provider-smoke/live）。
- **可复现缺陷（代码阅读已定位，待 T2 红灯证实）**：
  `acceptance.py:94 _quantize` 异常路径返回 `Decimal("0")`（坏值可被当 0 比对通过）；
  `acceptance.py:958 zip(turns, expected_list)` 在 expected 为错长列表时静默丢轮次；
  `acceptance.py:980 "tokens": "unknown"` 硬编码（usage 已可用）。
- **数据/口径阻塞**：无（本轮全合成）。live 模型评测按计划不跑（零外部 API），记
  not_run，不阻塞离线交付。
- **新增验收能力**：T1 沙箱库、T2 判定器自检、T3 扩展 runner/报告、T6 场景集、T7 前端回归。

### 基线命令结果

待 T1 新实例就绪后在其上串行跑（见 T1 记录）。不在既有 `bi_agent_test` 上跑本轮全量
（计划要求正常路径仅使用新实例）。

## T1 — 独立合成数据库与安全执行边界（2026-10-24，完成）

### 交付

- `deploy/evaluation/docker-compose.yml`：独立 project name `bi-agent-eval`、独立卷
  `bi_eval_pg_data`、只绑 `127.0.0.1`、密码认证（不沿用主库 trust）、默认端口
  55439（被占自动换）、`postgres:17.6`、TZ=Asia/Shanghai、locale 与主库对齐。
- `backend/tests/evaluation/database.py`：
  - `parse_evaluation_dsn`（纯函数，psycopg 解析 URL/kv 两种形式）：库名必须恰为
    `bi_agent_eval_test`（显式拒绝 `bi_agent_test`/`bi_agent` 等易混淆名）、只准本机
    地址；拒绝发生在任何连接之前，报错不含 DSN/密码。
  - `connect_evaluation_db`：连接后二次核验实际库名/主机 + 沙箱标记
    `bi.evaluation_sandbox`；标记缺失拒绝进入种子路径。
  - `initialize_evaluation_db`：按序重放 backend/sql 全部 28 个迁移文件
    （001–033，作废编号自然缺席）→ `bi_reader` 密码（sql.Literal）→ 写标记；
    已有标记拒绝重建；全程 try/finally 回滚关闭。
  - CLI：`--bootstrap`（生成仓库根 `.env.evaluation`（已忽略，仅 DSN/角色配置，
    无模型/快麦项）、起 compose、等就绪、初始化、提交 SB1 基线种子）、
    `--initialize`、`--seed-base`。
  - 基线种子只有 SB1（合成"评测基线成本店"，2026-08-20~09-08 逐日带 cost 成交）：
    受控探索套件真库 fixture 设计上要求库里有已提交数据（旧共享库靠真实同步数据
    满足）；曾试过同时提交 `seed_business_case`，但 test_db 的种子不是为叠加已提交
    状态设计的（11 失败 4 错误），回退为仅 SB1 后全绿。
- `backend/tests/test_evaluation_database.py`：15 用例（11 纯函数 + 4 活库）全过。
- `backend/tests/dbfixtures.py`：抽出 `assert_isolated_test_target`（本机 `*_test`
  双检查），`connect_test_db` 与 acceptance 共用。
- `backend/tests/acceptance.py`：offline/live 入口连接后过同一身份边界；每题
  savepoint 回滚后 `RELEASE`（长跑不积累）。
- `docs/runbook.md`：新增「评测沙箱实例」节（bootstrap、日常命令、清理）。

### 基线命令结果（全部在新实例 `bi_agent_eval_test` @127.0.0.1:55439 上串行）

| 命令（backend/ 下） | 结果 | 退出码 |
| --- | --- | --- |
| `uv run --locked --env-file ../.env.evaluation python -m unittest discover -s tests -t .` | **2021 tests OK**（无 skip） | 0 |
| `uv run --locked --env-file ../.env.evaluation python -m tests.acceptance --offline` | **35/35 pass** | 0 |
| `uv run --locked --env-file ../.env.evaluation python -m unittest tests.test_evaluation_database -v` | 15/15 OK | 0 |

### 过程记录

- psycopg 顶层无 `UndefinedTable`（在 `psycopg.errors`）；探测 SQL 失败后必须先
  rollback 再重放迁移；`ALTER ROLE ... PASSWORD` 不收参数占位符（改 sql.Literal）。
- Docker Desktop 手动启动后引擎正常；`napcat`/`astrbot`/`bi-agent-postgres` 为既有
  容器，未触碰。沙箱重建过一次（`down -v` + bootstrap），期间零真实数据损失
  （沙箱内全为合成数据）。
- 主库 `bi_agent`、共享 `bi_agent_test` 本轮零连接、零写入。

## T2 — 判定器自检（2026-10-24，完成）

### 交付

- `backend/tests/evaluation/verification.py`（判定器单一实现，acceptance 与 T3
  runner 共用）：
  - `parse_finite_decimal`：None/空串/垃圾串/布尔/NaN/Infinity 一律
    `ValueError`；**没有任何"解析失败当 0"的路径**（旧 `_quantize` 的异常路径
    已随迁移删除，acceptance 中不再存在）。
  - 精度策略显式化：`MONEY_VALUE_KEYS` 白名单（与既有题库金额键一致）按 2 位
    小数比较；数量/单数/比值按精确数值比较（`7.00`==`7` 成立、`7.005`≠`7`）；
    未登记新键默认精确比较——只会更严不会更松。
  - `verify_expected(expected, observed)`：observed 为白名单投影
    （rows/limitations/basis/coverage/diagnostics/partial_answer/refused/
    remediation/text/calls）；期望侧 `calls` 保持既有"前 N 次调用参数核对"语义
    （Q21 依赖），总数断言用新增显式 `call_count`。
  - `verify_commerce_turn` / `verify_product_turn` / `param_problems` /
    `validate_case_shape` 从 acceptance 原样迁入（语义不变）。
- `backend/tests/test_evaluation_verification.py`：44 用例——非法值不等于 0
  （None/空串/"bad"/True/NaN/Infinity/期望本身非法/无数据行）、精度策略、
  13 项逐字段变异（金额/basis/time_basis/覆盖状态/覆盖区间/缺口披露/refused/
  not_codes/no_values/text/禁用词/调用次数/缺调用记录）、commerce 变异
  （excluded_scope/参数/行值/状态格/缺调用）、形状校验（空 turns/错长
  expected/非对象项）。
- `backend/tests/acceptance.py`：`_observation` 适配器把每轮执行投影给同一判定器；
  错形题（expected 列表与 turns 不等长）先判红再不执行，`zip` 静默丢轮次不可能；
  无工具结果时已声明的 values/refused 等照常核对（旧代码会静默跳过，属收紧）。
  迁出的 `_quantize/_value_problems/_structured_problems/_verify_commerce_turn/
  _verify_product_turn/_param_problems/_normalize_params` 已删除。

### 验证

| 命令 | 结果 |
| --- | --- |
| `uv run --locked python -m unittest tests.test_evaluation_verification -v` | 44/44 OK |
| `uv run --locked --env-file ../.env.evaluation python -m tests.acceptance --offline` | 35/35 pass（同一判定器下既有合同零回归） |
| `uv run --locked --env-file ../.env.evaluation python -m unittest discover -s tests -t .` | **2065/2065 OK**（2021 既有 + 44 新增） |

发现并修正的判定器缺口：变异测试暴露"期望 `calls` 列表被当总数"会误伤 Q21
（其 calls=前 2 次调用参数核对，实际脚本 3 次调用）——引入 `call_count` 分离语义。

## T3 — 扩展 runner、计量与结构化报告（2026-10-24，完成）

### 交付

- `backend/tests/evaluation/contracts.py`：`EvaluationCase`（pydantic，未知字段
  拒绝；id/category/family_id/split/turns/expected/seed_names/allowed/
  feature_flags/scripted_replies/requires_human_review 全字段契约）、
  `load_cases`（ID 唯一、expected 与 turns 等长、种子步骤与 feature flag
  （`FEATURE_FLAG_REGISTRY={"PARTIAL_ANSWERS_ENABLED"}`）必须在注册表内——
  校验发生在任何数据库或模型调用之前）、`CaseResult`/`EvaluationSummary`、
  dataset SHA-256。
- `backend/tests/evaluation/telemetry.py`：`MeteredModel`（原协议
  `complete(messages, tools, *, timeout_s)` 透传，ModelReply 原样返回，采集
  次数/单调耗时/usage）、`ScriptedModel`（耗尽显式 `ScriptExhaustedError`）、
  `aggregate_usage`（complete/partial/unavailable；缺任一调用总量为 null、
  保留 known 部分）、`build_scripted_reply`。
- `backend/tests/evaluation/runner.py`：串行 `run_suite`（每题独立
  SessionState/授权集/savepoint（回滚后 RELEASE），多轮共享题内状态；判定
  复用 acceptance `_verify_turn`/`verify_commerce_turn` 同一实现）；种子与
  acceptance.run_offline 完全同一套合成基准；usage 从真实 `ModelReply.usage`
  累计；环境未就绪全部记 blocked 而非失败；live 默认拒绝（`--allow-live` 才
  放开，缺配置联网前退出 2，live 完全不发送 expected/scripted_replies——脚本
  在 live 下被忽略）；CLI 退出码 0/1/2（blocked/skipped/零 executed/人工待核
  →2）；`--order reverse` 支持（T6 反序验证用）。
- `backend/tests/evaluation/reporting.py`：`write_report` 写 cases.jsonl /
  summary.json / report.md 三层；已有 run 目录不覆盖；`sanitize` 把
  DSN/password= 形态的密码换成 ***；异常只落类别名+安全摘要（不序列化异常
  对象/模型请求/SQL 参数）；offline 报告固定标注"脚本模型下的协议与业务回归，
  不衡量自然语言理解"；零 executed 通过率 null。
- `backend/tests/evaluation/cases/operator_core.jsonl`：3 个初始场景
  （S1 窗口出数 / PDD1 能力拒答 / TB1 出库口径披露），T6 扩到 30+。
- 源码版本（git head + dirty 标记）、dataset SHA-256、冻结时间、门禁、重复
  次数、命令摘要入 summary；不导出环境变量。

### 验证

| 命令 | 结果 |
| --- | --- |
| `uv run --locked python -m unittest tests.test_evaluation_runner tests.test_evaluation_reporting -v` | OK（无环境 28 过 6 跳过） |
| `uv run --locked --env-file ../.env.evaluation python -m unittest tests.test_evaluation_runner tests.test_evaluation_reporting -v` | **34/34 OK** |
| `uv run --locked --env-file ../.env.evaluation python -m tests.evaluation.runner --mode offline --suite tests/evaluation/cases/operator_core.jsonl --repeat 1 --output ../.tmp/evaluation/run-001` | **3/3 pass，exit 0**，三层报告落盘（`.tmp/` 已忽略） |
| `uv run --locked --env-file ../.env.evaluation python -m unittest discover -s tests -t .` | **2099/2099 OK** |

过程记录：场景问题文本的日期窗口按服务端"含当日"语义解析（"1日至8日"=
[09-01,09-09)），首跑暴露 oc-003 越覆盖被正确拒答——改问题为"1日至7日"后
通过；这本身验证了窗口排他语义在 suite 里可被断言。

## T4 — 验真两项历史修复（2026-10-25，完成）

### 结论（按计划要求三类分开，不合并为"商品查询全面可用"）

| 项 | 结论 |
| --- | --- |
| 参考价快照筛选（10-09 P1 后续修复 1） | **当前已修且通过**。既有用例 `test_commerce.py::test_unsold_catalog_rows_above_the_answer_cap_do_not_block_sales` / `test_item_price_cap_withholds_reference_without_rejecting_sales` 保持绿；本轮补 20 店 × 42 行（840 行）复现用例 `test_twenty_shop_snapshot_catalog_replication`：无关快照不把销售拒成 result_too_large、成交 SKU 参考价保留、未授权店铺（m21）店号与价格零出现、无"缩小日期"建议 |
| 公开校验提示（10-09 P1 后续修复 2） | **当前已修且通过**。既有 `test_profit_reference_needs_input_names_profit_basis`（缺 profit_basis→公开披露句；未登记平台"抖店"→另一句；互不串扰）保持绿；本轮补三轮回放场景 `oc-004`（见下） |
| 判定器/回合接缝（本轮发现） | **本轮修复且通过**。commerce needs_input 回合曾被告定器误判"无已发布载荷"、`status/limitations_contain/call_count` 在回合级与 commerce 载荷级同键双义、commerce 调用数不进观测——修复于 `verification.py`（needs_input 分支）与 `acceptance._verify_turn`（键剥离+commerce_calls 通道），47 项判定器自检全绿 |
| 行数上限 500 边界 | **已验证**。哨兵策略=LIMIT MAX_ROWS+1 超限抛 `_RowsTruncated`；新增 `test_sales_parent_rows_at_499/500/501_publish/refuse`：499/500 出数（20 店行、合计 500 件）、501 拒出数（中文披露句、零数据集、status failed/unavailable），未靠调上限过测试 |
| 商品级出库口径 / 淘系支付认证 | **仍受业务来源限制**（产品决策，不属本轮）：付款口径未认证的 fail-closed 行为不变；未改来源/成本/支付认证 |

### 新增证据

- `tests/test_commerce.py::ItemPriceReferenceTests` 4 个新用例（20 店复现 + 499/500/501 边界）。
- `tests/evaluation/cases/operator_core.jsonl::oc-004`：三轮毛利确认回放——
  轮1 无 profit_basis 调用→needs_input（披露句含 `profit_basis=existing_fields`），
  模型换平台（scope=fxg）重试→**仍 needs_input**（确认不可跳过）；
  轮2 用户明确确认→带 `profit_basis=existing_fields` 执行成功，数值与 Q27 金标准
  同源（tb 1件/100元、fxg 真实 0、pdd 分组 partial、排除原因
  coverage_time_basis_unverified）。suite 4/4 pass、exit 0。
- 过程发现（记入行为理解）：commerce 的 needs_input 不硬停 agent 循环——模型
  读披露后可自行修正参数重试；服务端只认参数里的显式 `profit_basis`，用户确认
  语义由工具描述约束（live 模型行为契约，离线脚本可控复现）。

### 验证

| 命令 | 结果 |
| --- | --- |
| `uv run --locked --env-file ../.env.evaluation python -m unittest tests.test_commerce tests.test_product_ranking tests.test_comparison tests.test_core -v` | **348 OK** |
| `uv run --locked python -m unittest tests.test_evaluation_verification -v` | **47 OK** |
| `uv run --locked --env-file ../.env.evaluation python -m tests.evaluation.runner --mode offline --suite tests/evaluation/cases/operator_core.jsonl --repeat 1 --output ../.tmp/evaluation/run-002` | **4/4 pass，exit 0** |
| `uv run --locked --env-file ../.env.evaluation python -m unittest discover -s tests -t .` | **2106/2106 OK** |
| `uv run --locked --env-file ../.env.evaluation python -m tests.acceptance --offline` | **35/35 pass** |

## 任务状态总览

| 任务 | 状态 |
| --- | --- |
| T0 | 完成 |
| T1 | 完成 |
| T2 | 完成 |
| T3 | 完成 |
| T4 | 完成 |
| T5 | 进行中（下一步） |
| T6–T8 | 未开始 |

下一条可执行命令：
`cd backend && uv run --locked --env-file ../.env.evaluation python -m unittest tests.test_core tests.test_runtime tests.test_operator_workflows -v`（T5）。
