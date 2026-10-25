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

## T5 — 多轮店铺范围与授权变化回归（2026-10-25，完成）

### 结论

**既有实现验证通过，本轮无需修复 agent.py。** 新增 14 个用例（8 纯函数 +
6 agent 级多轮）全绿；确定性路径（名称识别/引用派生/脱敏/授权裁剪）行为正确：

- `ShopScopeDetectionTests`（纯函数，无库）：已登记精确名命中；同名店铺
  标记歧义且同时命中两家（不替用户挑）；未登记名不匹配不扩大；伪造引用
  `ent-deadbeef` 不解析；撤销授权旧引用不再解析；脱敏把已登记名换引用、
  无引用名换"未授权店铺"；`_ensure_shop_refs` 撤销即删、重跑/重排不换号。
- `MultiTurnShopScopeTests`（真实图+脚本模型，断言规范化请求与完整消息对象）：
  - 三轮 全部授权→精确指定一家→改另一家：逐轮规范化 `shop_ids` 收窄/切换
    正确，授权集引用保留（模型每轮自选范围，服务端只保授权）；
  - 模型完整消息对象零真实店名/零真实店号；
  - 两家同名店→澄清且零查询零模型调用；
  - 撤销授权：会话状态引用即删，被撤销店铺不得再进任何查询；
  - 两个 subject 并行（串行执行）：B 的模型消息/查询里没有 A 的引用、
    名称、店号；
  - 时间+范围同时变化：换店不继承上轮店铺、换窗不继承上轮窗口。

### 记录（不修）

- `agent.py:714` 的 `matched_shops` 计算后未使用（范围由模型工具参数决定），
  属无害死代码，P2 记录不顺手改。
- "上次金额最高的那家"无确定性解析能力（模型侧看不到名称映射）：**待设计**，
  不为通过测试发明新能力；平台级收窄已由授权平台目录约束（既有测试覆盖）。

### 验证

| 命令 | 结果 |
| --- | --- |
| `uv run --locked --env-file ../.env.evaluation python -m unittest tests.test_core tests.test_runtime tests.test_operator_workflows -v` | **399 OK**（含 14 新增） |
| `uv run --locked --env-file ../.env.evaluation python -m unittest discover -s tests -t .` | **2120/2120 OK** |

## T6 — 业务核心场景集与稳定性检查（2026-10-26，完成）

### 交付

- `tests/evaluation/cases/operator_core.jsonl` 扩到 **34 个场景**（T3/T4 的 4 个
  + 本轮 30 个），10 类每类 ≥3 个独立条件；期望全部由种子算术手工推导
  （S1 值表：窗口 09-01..09-08 支付 1000/6 笔、P_A 7 件 600 元、P_B 4 件 400 元；
  拆单 E3/E4 记 2 笔支付——oc-009 首轮 3 笔预期被实际 4 笔纠正后按拆单语义定标）。
  金额期望 Decimal 可精确表达；0/null/字段不存在分立（oc-011 真实零
  status=ok 值'0' 带"未认证"披露 vs oc-008 无覆盖 missing_data）。
- 类别覆盖要点：排他结束日（单日 500 不含次日）、跨月 1500、窗口冲突不继承、
  aov 分母（4 笔 125）、加权价（产品工具 7 件 600）、部分答案（单段出数 50/
  多段整拒/指标拆分 refused）、多来源（未核验披露 basis_exact、pdd 整组排除
  payload missing_data、支付口径缺失格 missing 不补零）、参考价（快照在 90/
  指标子集不含价格列）、澄清（毛利确认 oc-004、未登记平台 1688、销售额口径）、
  授权（切店、越权引用被裁、全公司=授权集分列）、上传族（越窗仍缺、ROAS 同源、
  重查稳定）、故障（模型超时 ModelError 注入、非法参数一次修正/两次停止）、
  终态（fxg 真实 0 图表同源、退款披露保留、4 次工具预算后明确终态）。
- `tests/test_evaluation_cases.py`（9 用例）：≥30/类≥3/family 唯一（防同义改写）/
  split 不跨集/金额 Decimal 精确/部分答案语义必须显式开门禁；稳定性：串行×3
  与反序逐场景状态一致（题间零状态泄漏）；上传族补充事实层幂等（同窗种子写
  两遍合计仍 70——文件级幂等另由 test_sycm_import 钉住）。
- runner/判定器配套：`ScriptedReply.error`（ModelError 注入）；commerce
  拒答形态通用键 `payload_status`（needs_input/missing_data/failed 不再被判
  "无已发布载荷"）；`_verify_turn` 的 tool 键缺失守卫与调用数观测修复
  （空 commerce 列表不得顶替 query 调用数——oc-035 首轮 0 次误报的根因）。

### 验证

| 命令 | 结果 |
| --- | --- |
| `uv run --locked --env-file ../.env.evaluation python -m tests.evaluation.runner --mode offline --suite tests/evaluation/cases/operator_core.jsonl --repeat 1 --output ../.tmp/evaluation/run-003` | **34/34 pass，exit 0** |
| 同上 `--repeat 3` | **102/102 pass，exit 0** |
| 同上 `--order reverse` | **34/34 pass，exit 0** |
| `uv run --locked --env-file ../.env.evaluation python -m unittest tests.test_evaluation_cases -v` | **9/9 OK** |
| `uv run --locked python -m unittest tests.test_evaluation_runner tests.test_evaluation_reporting tests.test_evaluation_verification tests.test_evaluation_cases -v` | 90 用例 OK（无环境 9 skip：DB 稳定性组） |
| `uv run --locked --env-file ../.env.evaluation python -m unittest discover -s tests -t .` | **2129/2129 OK** |
| `uv run --locked --env-file ../.env.evaluation python -m tests.acceptance --offline` | **35/35 pass** |

首轮 12 失败全部定位为**期望侧错误或判定器缺口**（非业务缺陷）：拆单笔数、
商品档案需种子、`reference_price` 非请求指标名（应为 product_gross_margin_
reference）、含当日窗口语义、needs_input 由 reference 指标触发、退款口径
basis 为发生口径/完成时间、文本子串笔误；判定器缺口 3 处已修（见上）。
未放宽任何门禁、未改业务代码来迎合期望。

## T7 — 前端关键链路与 API 契约回归（2026-10-26，完成）

### 交付与验证

- **基线（用户 WIP 在场）**：`npm test` 217/217 通过——既有 13 个测试文件已覆盖
  T7 清单的大部分：部分答案（服务端披露/窗口校验/拒答枚举/中文原因码）、
  ArtifactView **各类 null 各归其位、绝不渲染为 0**、上传卡（结构化块才出按钮/
  拒绝不出现成功入口/七种原因码/404 功能关/重查文本取最近用户问题原文）、
  图表（**图表表格逐项相等、缺值断线真实 0 落基线、两口径徽标不同**、载荷文本
  不当标记）、消息渲染（不输出模型文本里的标记）。
- **本轮新增 6 用例**（`src/api.test.ts`，未触碰用户 WIP 文件）：
  - 逐字节喂入的**任意切点帧重组**；**CRLF 与混合分隔符**；**非法 JSON** 抛
    `SSE JSON不合法` 且零事件发出；
  - `sendMessage` 流契约：干净流按序 status→message→done 正常结束；
    **流在帧中间被掐断 → 拒绝（"不完整"）且绝不发出 done**（不完整回复不允许
    标 complete，fail closed）；非 2xx 在解析 body 前转 `ApiError`。
- **验证（frontend/）**：`npm ci` → `npm test` **223/223** → `npm run build` ✓
  （用户 WIP 原样在场并通过构建）。
- **如实记录**：本轮只完成组件/API 两层，**未做浏览器端到端**（不安装浏览器体系
  掩盖缺口）；上传"重复点击"的 busy 交互在 App 层（用户 WIP 文件），未加用例——
  事实层幂等已由后端（同窗重传合计不变）与 api 层（非 2xx→ApiError）钉住。

## T8 — 最终验收与接续交付（2026-10-26，完成）

- 最终验收文档：[2026-10-24-bi-evaluation-acceptance.md](2026-10-24-bi-evaluation-acceptance.md)
  （四栏结论、五条合成演示路径、证据摘要、待办清单、未执行边界）。
- 文档按实际变化更新：`README.md`（沙箱回归路径指针）、`docs/runbook.md`
  （T1 已加「评测沙箱实例」节）、`docs/demo.md`（五条合成演示路径）；
  `docs/metrics.md` 未动（指标语义零变化）。
- 最终门禁（同一基线、同一实例、串行）：后端全量 **2129/2129 OK（0 skip）**、
  acceptance offline **35/35**、扩展套件 **repeat 3 = 102/102**（dataset SHA-256
  与源文件逐字节一致）、反序 **34/34**、前端 `npm ci→test→build`
  **223/223 + build ✓**、`git diff --check` 干净。
- 审查确认：8 个提交不含用户 WIP（前端 5 文件仍为未提交修改）、无凭据、
  无真实数据、无生产开关变化；`.env.evaluation` 被忽略未入库；
  `.tmp/evaluation/` 报告目录被忽略。
- 未推送、未合并、未部署。

## 任务状态总览（最终）

| 任务 | 状态 | 关键证据 |
| --- | --- | --- |
| T0 基线 | 完成 | WIP/修复清单/环境/题数（35）核实 |
| T1 沙箱 | 完成 | 15 用例 + 2021 基线 |
| T2 判定器 | 完成 | 47 自检（后 3 项并入 T4/T6 修复） |
| T3 runner/报告 | 完成 | 34 用例 + CLI 端到端 |
| T4 历史修复验真 | 完成 | 20 店×42 行 + 499/500/501 + oc-004 |
| T5 多轮授权 | 完成 | 14 用例，agent.py 零改动 |
| T6 场景集 | 完成 | 34 场景 + 3×串行/反序一致 |
| T7 前端契约 | 完成 | 223/223 + 流中断不发 done |
| T8 验收交付 | 完成 | 本节 + 验收文档 |

## R — 2026-10-26 复核返工（F1–F9，完成）

2026-10-26 交付复核报告（F1–F9）逐项复现并修复；逐项根因/修复位置/红→绿
证据/坏例演示见[验收文档 §6](2026-10-24-bi-evaluation-acceptance.md)。
提交：`3cafadc`（F1/F2 假通过）、`50432b4`（F3 SSE 终态）、`059da8a`
（F4 加权金标准）、`6a3a4cb`（F5–F9 计量/分母/清理/脱敏/阻塞报告）。

关键更正：

- 撤回"判定器可信""无遗留未修复的代码缺陷"的表述——可信范围以已注册、
  已判定、有反例的断言键为限；已知限制列于验收 §2.3。
- T0–T8 共 **9 项**任务（此前文档误写 8 个）。
- 复跑数字（2026-10-26）：后端 **2142/2142**（含 13 条复核坏例回归）、
  旧 35 题 **35/35**、扩展套件 **×3 = 102/102**（34 场景重复运行，非 102 个
  独立场景）与反序 **34/34**、前端 **225/225 + build**（用户 WIP 在场）、
  `git diff --check` 干净。
- 坏例演示（沙箱实跑）：F1 删必需调用 fail×4 问题、F2 拼错键加载即拒、
  F3 空流/完整帧后 EOF 必拒、F4 陷阱 150 必败、F5 partial/null、F6 0.5 非
  100%、F7 RuntimeError 保留且连接可复用、F8 引号密码零残留、F9 阻塞三件
  报告落盘且退出码 2。
- 仍未执行（partial/not_run）：live 模型评测 not_run；浏览器端到端 not_run；
  App 层（WIP 文件）busy 交互未测；完整授权安全审计未做。

### 恢复位置与下一条可执行命令

分支 `reliability-evaluation-2026-10-24`（未推送、未合并、未部署）。沙箱容器
`bi-agent-eval-postgres`（restart=no，宿主休眠后会退出；恢复：
`docker start bi-agent-eval-postgres`）。接续第一步：

```bash
cd backend && uv run --locked --env-file ../.env.evaluation python -m unittest discover -s tests -t .
```

期望 2142/2142 OK。其后按验收文档 §2.4 待办逐项决策（每项均为所有者决定，
不阻塞本分支合并评估）。
