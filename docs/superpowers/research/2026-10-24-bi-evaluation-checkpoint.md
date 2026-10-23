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

## 任务状态总览

| 任务 | 状态 |
| --- | --- |
| T0 | 完成 |
| T1 | 完成 |
| T2 | 完成 |
| T3 | 进行中（下一步） |
| T4–T8 | 未开始 |

下一条可执行命令：
`cd backend && uv run --locked python -m unittest tests.test_evaluation_runner tests.test_evaluation_reporting -v`（T3 红灯先行）。
