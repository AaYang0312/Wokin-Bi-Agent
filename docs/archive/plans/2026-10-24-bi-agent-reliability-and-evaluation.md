# BI Agent 可用性与回归体系推进计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task if available. 若 zcode 没有该技能，直接按本文复现、实现、验证、记录的流程执行，不因缺技能停工。步骤使用 `- [ ]` 跟踪。默认单 agent 串行执行；无需等待本计划作者继续指挥。

**Goal:** 在约两天开发窗口内，复现并验证既有经营者实测问题，完成有证据支持的局部修复，建立可信、可重复运行的业务回归与评测报告，使项目具备更可靠的本地交付证据。

**Architecture:** 沿用 React → FastAPI → 受限 Agent → 查询状态图 → PostgreSQL 的现有架构。业务修复落在既有模块；评测辅助代码放在 `backend/tests/evaluation/`，复用现有种子、工具执行与结构化判定，不搭建第二套 Agent。测试使用独立 PostgreSQL 实例及纯合成数据。

**Tech Stack:** Python 3.11+、uv、unittest、Pydantic、psycopg、PostgreSQL 17.6、React 19、TypeScript、Vitest、Vite；以仓库锁文件为实际依赖依据，不顺便升级框架。

**Spec:** 本文第 2 节是本次推进范围与验收规格；业务事实以现有 `docs/metrics.md`、相关设计、最新验收和当前源码为准。第 3 节列出必须阅读的材料。

**目标仓库：** `D:\projects\bi-agent`。

**建议存放：** 执行 agent 在工作分支中将本文件复制到 `docs/superpowers/plans/2026-10-24-bi-agent-reliability-and-evaluation.md`，在该副本更新进度。以下仓库内文件路径均相对此仓库或其隔离工作副本。

**计划性质：** 可执行开发计划，不是验收通过声明。编写时仅检查了目录、文档和部分源码，没有重跑项目测试，也未取得可用 Git 状态。执行者必须先核实现状；历史记录不能代替本次结果。

## 1. 执行原则与优先级

### Global Constraints

- 额度只能在 zcode 客户端使用。本计划默认零外部模型 API、零快麦调用；可编写和用替身验证 live 评测路径，但不能把 zcode 额度当 API 余额。
- 默认完成本地开发，不部署、不推送、不修改现有生产或共享测试库，不启用线上功能门禁。
- 所有正常本地步骤连续推进；普通实现选择自主决定，环境阻塞只阻塞依赖它的任务。新业务口径、真实店铺映射、外部付费调用和部署不包含在本计划内。
- 不自动读取 `.env`、`.env.app`、`.env.sync`、现有 `.env.test` 的内容到模型上下文，不打印凭据；不复制真实经营数据到 fixtures。
- 现有 `bi_agent_test` 曾承载真实导入与同步数据。数据库名以 `_test` 结尾不能证明它是可随意重建的沙箱。本轮新建独立实例，不以现有库做 seed/reset/migration。
- 保持现有业务约束：缺失不补零、支付与出库口径不互换、毛利参考不冒充净利润、未核验来源不变成已核验；不新增拼多多能力，不自动合并淘宝与天猫。
- 毛利参考仍须用户明确确认；店铺/商品真实主键与名称按现有投影规则隔离；不得为改善模型选店把真实名称映射直接塞进提示词。
- 保持现有默认关闭门禁、每轮 4 次工具预算和 30 秒总 deadline。测试可在合成环境显式打开被测门禁，不改生产默认值。
- 共享数据库测试串行，禁止两个测试进程同时使用同一实例。若后续使用多个 agent，必须文件单写者、隔离 worktree 和隔离数据库；本计划不要求并行。
- 已实现且验证通过的任务标记“已有实现，本轮验证通过”，不要为了产生 diff 重写。新 bug 先写最小复现，再修根因，保留回归。
- 不更改既有金标准来迁就错误实现；如既有断言确实违背业务契约，提供契约与反例后单独记录修正理由。
- 不用增加测试数、代码行数或消耗量衡量进度。每项工作都必须关联经营者场景、明确缺陷或可复现验收能力。

### 两天的工作顺序

| 阶段 | 任务 | 建议时间盒 | 可交付结果 |
| --- | --- | --- | --- |
| 基线 | T0–T1 | 0–5 小时 | 当前差异清单、独立测试环境、基线命令结果 |
| 可信验收 | T2–T3 | 5–15 小时 | 判定器自检、可重复的扩展 runner、结构化报告 |
| 产品验证与修复 | T4–T5 | 15–30 小时 | 历史缺陷复现/验真、必要的局部修复 |
| 业务与界面回归 | T6–T7 | 30–40 小时 | 合成场景集、关键 UI 链路回归 |
| 收尾 | T8 | 40–48 小时 | 完整验收、问题分类、演示和接续记录 |

时间盒是优先级指引，不保证完成时间。到第 40 小时冻结新增范围。若时间不足，先交付 T0–T4 的可用闭环与 T8 的真实状态，不跳过验证去堆功能。

## 2. 本次规格与完成标准

### 2.1 为什么做这些工作

仓库同时存在不同日期、不同环境的记录：

- 2026-10-08 部分答案验收记录：35 道离线题、模拟模型消息序列；不能据此声称真实模型理解准确率。
- 2026-10-09 经营者记录：已经做过真实模型对话，平台对比、店铺下钻、上传后重查有可用路径；商品运营仍受口径与来源限制。
- 同一份 10 月 9 日记录的“P1 后续”又写明：参考价快照导致的行数误导、毛利参考校验提示已补代码，但未再跑现场对话。
- 当前源码也存在相关修复接缝：`commerce/repository.py` 的参考价筛选、`commerce/tool.py::_public_argument_hint`、现有工具描述。它们应先验证，不能当成尚未实现的需求。
- `tests/acceptance.py` 已有 offline/provider-smoke/live 三种入口、35 题合同和逐题 savepoint；当前报告把 tokens 写为 unknown，但 `llm.ModelReply.usage` 已存在，无需新造 provider 适配层。

### 2.2 本轮必交付

1. 保留既有 35 题合同，并以开工时的源码重新确认数量及新增情况。
2. 让判定器能拒绝被破坏的答案，尤其错误数值、错口径、缺披露、非法零值、漏执行轮次。
3. 建立分层报告：确定性业务结果、脚本模型协议回放、真实模型表现必须分开。
4. 验证 10 月 9 日两项已补修复；若还有缺陷，完成范围可控的修复。
5. 完成店铺范围与多轮授权隔离回归，修复当前确定性路径中可复现的范围丢失或错误继承。
6. 新增 30 个有独立意义的核心场景；在时间允许且确有新覆盖时扩到 60 个。按任务、输入条件、断言分类，不靠同义改写凑数。
7. 关键前端的部分答案、缺数上传重查、SSE 错误恢复契约得到验证。
8. 提供可重跑命令、机器报告、验收摘要、接续记录和真实限制清单。

### 2.3 本轮不扩展的产品方向

- 不新增商品级出库口径，不把未认证支付窗口放行，不通过删除来源门禁提高“可答率”。
- 不重做语义目录、受控 SQL、审核记忆、分析或库存监控；这些已有模块只做与本轮相关的回归。
- 不新增店铺别名映射、不补造渠道库存/在售价、不自动读取用户真实导出文件。
- 不新增通用 SQL 入口、模型自主写库、自动运营动作或复杂评测平台界面。
- 不承诺两天内生产就绪或真实模型准确率提升。

### Review Focus

| 风险输入/条件 | 用户应得到的行为 | 对应任务 |
| --- | --- | --- |
| 大量参考价快照、少量销售行 | 辅助价格不足不误报销售结果超限，销售真超限仍拒绝 | T4 |
| 参数少一项与平台未登记 | 能区分原因，不诱导用户确认不存在的口径或盲目换平台 | T4 |
| 多轮切换店铺、撤销授权 | 不继承越权范围，不泄露旧 Artifact 或名称 | T5 |
| 9 月导出上传后重查 10 月 | 仍显示缺口，不把上传成功当覆盖成功 | T6–T7 |
| 缺 usage、脚本回放、坏数字、未执行轮次 | 不记为零成本/模型准确率/通过；报告显式保留不完整状态 | T2–T3 |

## 3. 开工必读与文件地图

按需阅读，不每轮重读全仓库：

1. `README.md`、当前目录及祖先目录适用的 `AGENTS.md`；检查 `.agents/`、`.zcode/` 是否已有本项目活动任务，避免抢改。
2. `docs/metrics.md`、`docs/runbook.md` 中对应业务口径、测试库、迁移、默认门禁说明。
3. `docs/superpowers/research/2026-10-09-operator-cross-platform-live-test.md`，必须包含“P1 后续”一节。
4. `docs/superpowers/research/2026-10-08-operator-live-fixability.md`，区分已修/已扫描/未做，不重复执行旧建议。
5. `docs/superpowers/research/2026-10-08-partial-answers-acceptance.md`。
6. `docs/superpowers/plans/2026-10-08-partial-answers-on-coverage-gaps.md`。
7. `docs/superpowers/specs/2026-09-12-multi-source-metrics-design.md`、`2026-09-11-operator-workflows-design.md` 的相关段落。

| 现有文件/目录 | 本轮作用 |
| --- | --- |
| `backend/tests/acceptance.py` | 现有加载器、种子、模型脚本、逐轮判定、三类执行入口 |
| `backend/tests/questions.jsonl` | 既有回归合同，不能直接用大批改写题替换 |
| `backend/tests/dbfixtures.py`、`test_db.py` | 测试连接回滚、合成种子、冻结时钟 |
| `backend/bi_agent/llm.py` | 已有 `ModelReply.usage` 与 `ChatModel.complete` 协议 |
| `backend/bi_agent/agent.py` | 授权范围、名称脱敏、多轮状态、工具预算、最终回答 |
| `backend/bi_agent/commerce/{models,repository,graph,tool}.py` | 商品/平台对比、参考价、校验提示与结果投影 |
| `backend/bi_agent/business_query/partial.py`、`metrics.py` | 部分窗口、指标拆分、口径 |
| `backend/bi_agent/sycm_upload.py`、`sycm_import.py` | 上传、映射与幂等导入 |
| `frontend/src/api.ts`、`api.test.ts` | SSE 解析与 API 处理 |
| `frontend/src/components/{ArtifactView,RemediationUpload,MessageView,ChartArtifact}*` | 结果、披露、上传和图表 |
| `backend/sql/` | 现有迁移；当前已看到 001–033 编号范围，编号有缺口，不能假设每个整数都有文件 |

新增文件统一归入下表。发现同职责代码时优先扩展已有模块，并在 checkpoint 记下实际路径，不平行造轮子。

| 新增路径 | 单一职责 |
| --- | --- |
| `backend/tests/evaluation/__init__.py` | 包标识 |
| `backend/tests/evaluation/database.py` | 合成沙箱连接、身份检查、事务清理 |
| `backend/tests/evaluation/contracts.py` | 场景、检查项、报告 schema |
| `backend/tests/evaluation/verification.py` | 可复用结构化判定与严格数字比较 |
| `backend/tests/evaluation/telemetry.py` | 包装模型调用并采集 usage/耗时，不改模型行为 |
| `backend/tests/evaluation/runner.py` | 执行、串行重复、退出码与失败分类 |
| `backend/tests/evaluation/reporting.py` | JSONL、summary.json、Markdown 汇总 |
| `backend/tests/evaluation/cases/operator_core.jsonl` | 扩展合成场景；不混入真实经营记录 |
| `backend/tests/test_evaluation_*.py` | 上述功能的独立验证 |
| `deploy/evaluation/docker-compose.yml` | 与现有数据库完全隔离的开发实例 |
| `docs/superpowers/research/2026-10-24-bi-evaluation-{checkpoint,acceptance}.md` | 开发接续与最终验收 |

新文件不是必须全部创建：若三五行逻辑能清楚地留在调用模块，不为凑分层拆文件；契约和职责保持一致。

## 4. 统一执行与记录方式

- 每个代码任务：定位已有实现 → 最小失败用例或已有修复验证 → 最小改动 → 定向检查 → 审视 diff → 记录证据 → 提交本任务文件。
- 测试要证明行为，不能只验证函数被调用或复制实现公式。
- Git 正常时每个完成任务单独提交；提交前列出 staged 文件，不 `git add .`。不夹带用户 WIP，不 reset/clean/stash 用户文件。
- 有工作树改动时先读差异，能分离则在独立分支/worktree 工作；若依赖现有 WIP，在隔离副本中保留它并标注基线，不能默默从旧 main 丢掉已有修复。
- 无法使用 Git 时允许在明确的隔离源码副本继续，记录前后 diff 和基线说明；不要把权限问题变成全项目停工理由。
- 每个任务结束更新 checkpoint：状态、基线、改动文件、命令/退出码、真实测试计数、未解决问题、下一条可执行命令。
- 同一个问题连续两轮没有新证据，记录最小复现和阻塞原因，转做独立任务，禁止无限自审循环。

## 5. 任务清单

### T0 — 核实现状与建立基线

**文件：** 阅读第 3 节；创建 checkpoint。此任务不修改业务逻辑。

**产物：** `baseline_revision`、WIP 清单、已有修复清单、环境能力、实际任务依赖。

- [ ] 检查 Git 根目录、分支、`git status --short`、相关 diff；记录当前源码是否已包含 10 月 9 日两项修复。
- [ ] 找到现有迁移清单与初始化方法，记录最新迁移；确认 Python/uv、Node/npm、Docker 可用性和锁文件。
- [ ] 核对 `_load_questions()` 的题数常量、`questions.jsonl` 实际题数，以及前端脚本。不要沿用文档里的旧 26 题文案。
- [ ] 列出已存在的相关用例，特别是参考价上限、profit_basis 提示、中文平台名、部分答案、上传重查；后续任务优先复用。
- [ ] 在 checkpoint 区分：已实现待验证、可复现缺陷、数据/口径阻塞、新增验收能力。历史“已通过”不得填入本次测试结果。

**完成：** 执行者可以说明本轮改什么、哪些已经有、在哪个隔离环境跑；不把“没有真实模型 API”当本轮阻塞。

### T1 — 独立合成数据库与安全执行边界

**文件：** 新增 `deploy/evaluation/docker-compose.yml`、`backend/tests/evaluation/database.py`、`backend/tests/test_evaluation_database.py`；按需小改 `backend/tests/dbfixtures.py`、`acceptance.py`；更新 runbook 的开发测试段。

**接口：** `connect_evaluation_db(dsn: str) -> psycopg.Connection`，连接后先验证实例身份和数据库名，再允许种子或迁移；与现有连接清理规则兼容。不返回或记录 DSN。

- [ ] 先写失败用例：非本机地址、错误数据库名、缺少沙箱标记、现有 `bi_agent_test` 均不能进入种子路径；拒绝时不执行任何业务写 SQL。URL DSN 和 key/value DSN 使用 psycopg 的解析能力，不手写字符串匹配。
- [ ] 新 Compose 使用 `postgres:17.6`，独立 project name、独立卷，只绑定 `127.0.0.1`；默认候选宿主端口 `55439`，被占用则换空闲端口，不关停其他服务。使用本地生成密码，不照搬现有 trust 配置，不将密码入库。
- [ ] 新库固定命名 `bi_agent_eval_test`；初始化后创建仅用于识别沙箱的标记。必须确认容器和卷是本轮新建的；同名已有资源不自动重建。
- [ ] 按仓库实际依赖顺序重放迁移并准备最小角色；迁移可修改 cluster 级角色，因此必须独立 PostgreSQL 实例，不能只在现有实例另建数据库。
- [ ] 合成 DSN 写入仓库忽略的 `.env.evaluation`，仅包含测试所需 DSN/角色配置，不含模型/快麦配置。脚本从环境读密码，报告只记录脱敏环境标识。
- [ ] 新 runner 和现有 acceptance 的种子执行都经过环境检查；用 `try/finally` 确保初始化中途报错、执行报错也回滚并关闭连接。每题 savepoint 回滚后释放，避免长跑积累。
- [ ] 在新实例串行跑基线，记录 skips、failures、errors 和退出码。Docker 不可用则完成纯函数/替身验证，将 DB 验收标记 blocked，继续 T2/T3 非 DB 部分。

**命令（从工作副本 backend 目录）：**

```powershell
uv sync --locked
uv run --locked --env-file ../.env.evaluation python -m unittest discover -s tests -t .
uv run --locked --env-file ../.env.evaluation python -m tests.acceptance --offline
```

**完成：** 正常路径仅使用新实例；错误目标在 seed 前被拦截。既有测试缺少某角色/配置导致的 skip 必须列出，不把带 skip 的输出写成“全量验收通过”。

### T2 — 先验证“验收程序不会错判通过”

**文件：** `backend/tests/acceptance.py`、新增 `evaluation/verification.py`、`test_evaluation_verification.py`。优先提取有复用价值的判断，避免整体重构千行 runner。

**接口：** `parse_finite_decimal(value: object) -> Decimal`，非法/非有限值抛 `ValueError`；`verify_expected(expected: dict, observed: dict) -> list[str]` 返回稳定问题码，空列表仅表示全部要求满足。

- [ ] 写失败用例：期望数值 0 时，实际 `None`、空串、`"bad"`、布尔值、NaN、Infinity 不能等于 0；期望本身非法也要拒绝。特别核验现有 `_quantize()` 的异常返回 0 路径。
- [ ] 验证数值比较遵循指标精度：金额使用明确的金额精度，数量不得被统一按两位小数抹平，比例沿用现有 metric 契约；不要顺便改变业务层数值算法。
- [ ] 对每个正常观测，分别变异金额、basis/time_basis、覆盖区间、缺口披露、refused 集合、excluded_scope、调用次数、授权引用；每个变异应导致相应失败，不用全 JSON 数字子串扫描。
- [ ] 场景 schema 强制 `turns` 非空、逐轮 expected 数量相等；缺少已声明检查的数据即失败。防止 `zip` 静默丢轮次和缺字段默认通过。
- [ ] 保留原有 35 题结构及行为；若提取判定器，原 runner 调用同一实现。金额/比例策略有意不同的地方明确命名，不隐式猜测 `_is_money`。
- [ ] 跑自检并串行跑既有 acceptance；每一种注入错误都应有一条能真正变红的测试。

**验证：** `uv run --locked python -m unittest tests.test_evaluation_verification -v`；再运行 T1 的 offline 命令。

**完成：** 正常基准仍通过；故意篡改观测会失败；坏数值、遗漏轮次不再可能被视为成功。只在测试内构造变异，不改正式业务事实或现有题库期望。

### T3 — 扩展 runner、计量与结构化报告

**文件：** 新增 `evaluation/{contracts,telemetry,runner,reporting}.py`、`test_evaluation_runner.py`、`test_evaluation_reporting.py`；按需提取 acceptance 的种子/逐轮执行助手。不要新增 provider 客户端。

**接口约定：**

- `load_cases(path: Path) -> list[EvaluationCase]`：严格验证 ID 唯一、轮数一致、seed 在注册白名单中，未知字段拒绝。
- `run_suite(cases: list[EvaluationCase], *, mode: str, repeat: int, output_dir: Path) -> EvaluationSummary`：串行运行；CLI 模式为 `offline` 或 `live`，默认必须显式选模式。
- `MeteredModel` 包装现有 `ChatModel`，保持 `complete(messages, tools, *, timeout_s)` 原协议，原样返回 `ModelReply`，不改变 timeout/模型配置。
- `write_report(summary: EvaluationSummary, output_dir: Path) -> None`：写 `cases.jsonl`、`summary.json`、`report.md`；不覆盖已有 run 目录。

**场景最小字段：** `id`、`category`、`family_id`、`split`（regression/holdout）、`turns`、`expected`、`seed_names`、`allowed`、`feature_flags`、`scripted_replies`、`requires_human_review`。冻结时钟复用现有 FROZEN_NOW；只有登记过的 feature flag 可设置。

**判定兼容：** `expected` 固定为与 turns 等长的列表；每项复用当前 `_verify_turn` / `_verify_commerce_turn` 已支持的键和语义，例如 status、parameters、values、basis、coverage、codes、calls、no_values、no_rows、no_total。开工时从这两个函数整理准确键表，不把上述示例误当完整表；新检查项须同时注册 schema、判定函数和反例。`observed` 是执行器从实际 ToolResult、运行记录、公开 Artifact 与最终文本采集的白名单投影，不是另一份 expected。生产响应无需为适配评测新增字段。

**模型脚本：** 显式声明每次回复的 text/tool_calls/合成 usage；使用现有 ToolCall/ModelReply 构造。不得根据本次实际结果生成期望，也不得从 expected 自动复制最终答案来声称验证了回答质量。

- [ ] 先写 runner 验证：未知 seed/重复 ID/错误 expected 长度在接触数据库或模型前失败；脚本耗尽明确失败；未使用脚本回复要按场景合同检查，不能默默忽略。
- [ ] 既有 `questions.jsonl` 的 ID 分支脚本保留兼容；扩展场景用显式脚本，不继续往 `_model_script` 增加几十个 ID 特例。只提取必要接缝。
- [ ] 每题独立会话、授权集合、运行存储和 savepoint；多轮场景共享该题的 SessionState；题间不共享。顺序颠倒、重复运行仍得到相同结构化结果；随机 ID/计时字段单独处理。
- [ ] 采集 `duration_ms`、model_call_count、tool_call_count、run/artifact 数量、错误分类、每项 assertion 状态。耗时用单调时钟；端到端与模型耗时分开。
- [ ] usage 从真实 `ModelReply.usage` 累计：完整时给总量，缺任一调用时总量为 null，同时保留 known 部分和 complete/partial/unavailable 状态。离线 usage 来源标 synthetic，不能计成真实成本。无价格表不估算费用。
- [ ] summary 分列 attempted/executed/passed/failed/blocked/skipped、human_review_pending；assertion_pass 不代表人工复核通过。零 executed 时通过率 null，不能输出 100%。
- [ ] 离线报告固定标注“脚本模型下的协议与业务回归，不衡量自然语言理解”。live CLI 另需默认 false 的 `--allow-live` 开关；只有用户另行明确授权 API 调用范围后才能传入，本轮不传。缺开关/配置时在联网前退出 2，不因 `.env` 存在自动调用。为防脚本答案成为 live 提示，live 模式完全不向模型发送 expected/scripted_replies。
- [ ] CLI 退出码：完整要求满足为 0；断言失败/执行错误为 1；环境、未授权 live 或必需人工复核等未完成状态为 2。已有 legacy CLI 若保留旧行为，明确差异，新报告不据旧 exit=0 推断通过。
- [ ] 报告用检查项 ID、错误类别和安全摘要，不直接序列化异常对象、DSN、模型请求、SQL 参数。用含伪秘密标记的异常测试报告不会泄露；已知合成 case 内容可落盘。
- [ ] 保存源码版本（包含 dirty/diff 标识）、dataset SHA-256、模式、冻结时间、配置开关、重复次数、运行命令摘要；不得把环境变量全量导出。

**实现后的命令：**

```powershell
uv run --locked python -m unittest tests.test_evaluation_runner tests.test_evaluation_reporting -v
uv run --locked --env-file ../.env.evaluation python -m tests.evaluation.runner --mode offline --suite tests/evaluation/cases/operator_core.jsonl --repeat 1 --output ../.tmp/evaluation/run-001
```

**完成：** 无外部 API 也可跑完整的合成回归与报告；故障、跳过、人工待核、usage 缺失都不会冒充成功。该任务先用 3 个小型场景跑通，T6 再扩展。

### T4 — 验真两项历史修复，修掉仍能复现的问题

**文件：** `commerce/repository.py`、`commerce/graph.py`、`commerce/tool.py`、必要时 `agent.py`；`tests/test_commerce.py`、`test_product_ranking.py`、`test_comparison.py`、`test_core.py` 中相关测试。

**接口：** 保持 `load_item_prices`、`execute_commerce_tool(call, context)`、`_public_argument_hint(error)` 现有签名；不扩展商品查询业务口径。

- [ ] 用合成的 20 店 × 42 SKU 档案快照和少量已成交 SKU 复现原行数场景；所有店铺/商品使用合成 ID。确认当前筛选实现是否已经避开无关快照。
- [ ] 覆盖：无关价格行很多不导致销售超限；关联参考价子集超限时参考价缺失但合格销售值可出；销售数据真超限时仍拒绝；未认证支付仍拒绝；无权店铺价格不参与结果。
- [ ] 对行数上限 499/500/501 分别测试，先从现有查询的 LIMIT/哨兵策略确认 500 边界约定，明确写入用例；不靠随意提高上限让测试通过。
- [ ] 检查错误建议：参考价不足不得再建议缩日期；终止原因和限制码必须反映实际失败来源，不把辅助价格缺失提升成销售硬失败。
- [ ] 分别复现缺 profit_basis、未登记平台、两者同时错误、未知校验错误。模型只接收公开白名单提示，不接收 Pydantic 原始输入和堆栈；毛利参考提示不能自动补 `existing_fields` 代用户确认。
- [ ] 录制一个脚本模型多轮回放：请求毛利参考 → 明确缺少确认 → 用户明确确认 → 合成来源允许时进入执行。单独验证模型换平台重试不能跳过确认。
- [ ] 若现有实现已经满足，保留已有修复并补缺失证据；若失败，先保存复现再做最小修复。不要借此更改来源、成本或支付认证。

**验证：** 串行运行 `uv run --locked --env-file ../.env.evaluation python -m unittest tests.test_commerce tests.test_product_ranking tests.test_comparison tests.test_core -v`，再跑相关扩展场景。

**完成：** 两类问题分别有“当前已修且通过/本轮修复且通过/仍受业务来源限制”的结论，不能将三者混为一个“商品查询已全面可用”。

### T5 — 多轮店铺范围与授权变化回归

**文件：** `agent.py` 的 `_detect_shop_ids`、`_ensure_shop_refs`、`_anonymize_question` 与 answer 状态接缝；`tests/test_core.py`、`test_runtime.py`、`test_operator_workflows.py`。只改确认存在问题的路径。

**接口：** 保持既有 `answer(...) -> TurnResult` 与 SessionState；不新增名称搜索 API，不向模型提供真实名称字典。

- [ ] 覆盖“全部授权店铺 → 用户精确指定一家 → 改另一家”的三轮输入；检查最终规范化 shop_refs，而非只断言自然语言里出现店名。
- [ ] 覆盖已登记精确名称、两个同名店铺、未知店名、伪造引用、已撤销授权旧引用；重复名称应澄清，未知名称不能扩大为全部店铺。
- [ ] 覆盖两个不同 subject 并行存在的会话，单个题仍串行执行；A 的 refs/Artifact 不得进入 B 的模型消息或公开结果。
- [ ] 覆盖时间变化与范围变化同时发生；不能把“只看上个月店铺 B”保留成上一轮 A/本月。
- [ ] 对现有确定性名称识别/脱敏路径存在的 bug 做局部修复，模型消息只能包含当前授权引用。测试检查完整发送消息对象，确保名称和真实 ID 未外泄。
- [ ] 平台级收窄只能依赖已有授权平台目录；“上次金额最高的那家”若无明确现有解析能力，记为待设计，不凭模型猜一个 ref，也不为了通过测试发明新能力。

**验证：** `uv run --locked --env-file ../.env.evaluation python -m unittest tests.test_core tests.test_runtime tests.test_operator_workflows -v`。

**完成：** 用户已经支持的精确选店和多轮切换路径有可验证正确性；P2 的所有指代问题不必在本轮全部解决，剩余能力必须列明。

### T6 — 建立业务核心场景集与稳定性检查

**文件：** `evaluation/cases/operator_core.jsonl`、必要的合成 fixture 注册模块、`test_evaluation_cases.py`；复用既有 tests 内 seed，不将 fixture 放进生产包。

**接口：** 使用 T3 的 EvaluationCase；所有 expected 以手工可算的小型数据及现有契约独立制定，不由被测函数生成。

- [ ] 建 30 个核心场景：以下 10 类每类至少 3 个独立条件。可达 60 个但不得用同义词改写代替边界覆盖。

| 类别 | 最少覆盖 |
| --- | --- |
| 时间与覆盖 | 结束日期排他、跨月、明确窗口与上一轮冲突 |
| 口径与比率 | 支付/出库隔离、加权均价非店均价平均、分母零 |
| 部分答案 | 单段可答、多段仍拒绝、可答与不可答指标拆分 |
| 多来源 | 未核验的披露、缺 consignTime 的排除、缺失组非零 |
| 商品与参考价 | T4 的无关快照、参考价不足、销售行真正超限 |
| 澄清 | 毛利确认、未知平台、商品多候选 |
| 授权与多轮 | 切店、撤权、跨 subject 引用 |
| 上传后重查 | 覆盖窗口可答、越窗仍缺、重复导入不重复计数 |
| 故障与持久化 | 超时、模型非法参数、Artifact 写入失败不发布成功 |
| 图表与终态 | 图表表格数据一致、限制披露保留、工具预算到达仍有明确终态 |

- [ ] 金额用 Decimal 可精确计算的小数；明确 0、null、字段不存在、部分结果的不同含义。用合成 upload bytes，不引用真实导出目录。
- [ ] 优先复用已有 `partial_answer_attacks.jsonl`、`exploration_attacks.jsonl` 中相关攻击类型，不整体重复拷贝。
- [ ] 按 family_id 去重；改写题必须与原题同 family。如果预留 holdout，同 family 不跨开发/holdout；全部来自同一作者的合成集只能称回归集，不能宣称独立业务 benchmark。
- [ ] 先跑一次，修复可复现问题；最终对已修改的共享状态路径跑三次串行和一次反序，验证隔离性。非确定性的时间、UUID 不作逐字相等比较。
- [ ] 同一失败一次修复、一次定向复验；无变化时不反复全量重跑。保留 before/after 使用同一 dataset hash 和配置的证据。

**验证：** `uv run --locked python -m unittest tests.test_evaluation_cases -v`，并用 T3 runner 执行 suite；增加 `--order reverse` 参数或独立单测实现反序验证，不需要随机测试框架。

**完成：** 所有核心场景明确 expected，回归结果稳定；报告中的“通过率”标题含 mode 和数据类型。未运行 live 时不写模型能力提升百分比。

### T7 — 前端关键链路与 API 契约回归

**文件：** `frontend/src/api.test.ts`、`partialAnswer.test.tsx`、`components/{ArtifactView,RemediationUpload,MessageView,ChartArtifact}.test.tsx`；必要时对应实现文件；后端已有上传/API 测试。

**接口：** 沿用现有 SSE 事件与上传、重新查询交互。上传成功后继续由用户点击重查，不擅自改成自动重跑。

- [ ] 部分答案：实际窗口、缺口、拒绝指标原因常显；空值不渲染为 0；非法载荷整卡拒绝；图表不得丢掉限制或把 excluded 组补零。
- [ ] 上传：成功显示导入状态但不宣称原问题已解决；点击重新查询发送原始问题；被拒绝不出现成功入口；重复点击发送是否沿用既有 busy/幂等保护需用用例核对。
- [ ] SSE：中文 UTF-8 跨 chunk、帧跨 chunk、CRLF、未知事件、错误 JSON、流在 done 前结束；不能静默把不完整回复标 complete。只要求解析现有服务端合法协议，不顺便实现全规格 EventSource。
- [ ] 后端异常或持久化失败，前端仍显示可理解错误且不会保留虚假的“成功”状态。重试不得串到另一会话。
- [ ] 用现有 Vitest 和 fetch/stream 替身完成上述验证；另用后端 API 测试证明对应事件/上传响应真实契约。若现有工具可用，再启动仅连合成实例的页面做一轮浏览器联调。
- [ ] 浏览器联调使用本地脚本模型或 fixture 服务，不调用真实模型。若只完成组件/API 两层测试，就如实写“未做浏览器端到端”，不安装大型浏览器体系来掩盖缺口。

**验证（从 frontend）：**

```powershell
npm ci
npm test
npm run build
```

**完成：** 上述行为有证据；不做整站视觉改版，不增加无关组件。

### T8 — 最终验收与接续交付

**文件：** `docs/superpowers/research/2026-10-24-bi-evaluation-acceptance.md`、checkpoint；按实际变化更新 `README.md`、`docs/runbook.md`、`docs/demo.md`，指标语义确有变化才更新 `docs/metrics.md`。

- [ ] 停止扩大范围，审查 diff，确认无用户 WIP、真实数据、凭据或生产开关变化被纳入提交。
- [ ] 在同一已记录基线与合成实例上，串行跑后端全量、既有 acceptance offline、扩展 suite、前端测试与构建；保存命令、退出码、计数和跳过明细。
- [ ] 阅读新报告，核对“缺环境/live未跑/人工待核/业务来源不足”不会记 pass；检查报告中的 dataset hash、版本和实际执行一致。
- [ ] 最终修复后只补受影响定向检查；若改到共享 runner/授权/数值核心，再重跑受影响的全量门禁。所有最终结论对应最终源码。
- [ ] 报告分四栏：已验证可用、正确拒答、尚未解决的代码问题、真实数据/产品决策/外部调用待办。不得只有一个笼统完成百分比。
- [ ] 提供 5 条合成演示路径：平台可比结果、精确店铺下钻、部分答案披露、毛利确认、上传后原题重查；每条包含输入、预期结果和限制。
- [ ] 生成项目证据摘要：做了什么、为什么、复现方式、修复证据。性能比较仅限同数据/环境/配置；分开报告冷/热、离线/真实模型耗时。没有对照实验就不写“提升 X%”。
- [ ] 整理剩余产品决定：商品级出库口径是否扩展、真实店铺映射、真实来源认证、live 模型评测预算与范围。每项写清阻塞了哪个用户场景，不替所有者作答。
- [ ] checkpoint 写下下一条可执行命令和恢复位置。提交本轮文件，不推送、不合并、不发布。

**最终建议命令（backend）：**

```powershell
uv run --locked --env-file ../.env.evaluation python -m unittest discover -s tests -t .
uv run --locked --env-file ../.env.evaluation python -m tests.acceptance --offline
uv run --locked --env-file ../.env.evaluation python -m tests.evaluation.runner --mode offline --suite tests/evaluation/cases/operator_core.jsonl --repeat 3 --output ../.tmp/evaluation/final-001
```

随后从 frontend 执行 `npm test` 与 `npm run build`，从仓库根执行 `git diff --check`。若路径/命令在实施中有合理调整，以最终文档里的已验证命令为准。

## 6. 失败分类与停止条件

| 类别 | 判断依据 | 下一步 |
| --- | --- | --- |
| 代码缺陷 | 小型合成数据能稳定复现且违反现有契约 | 最小修复 + 回归 |
| 测试/runner 缺陷 | 观测正确却判错，或明显错误仍判 pass | 修判定器并做变异自检，不改业务迎合测试 |
| 数据不足 | 来源/窗口/字段本身缺失 | 保持正确拒答，列数据待办 |
| 业务决策 | 需要新口径、映射、阈值或授权 | 留具体方案及影响，继续无关任务 |
| 环境阻塞 | 本地依赖/隔离库不可用 | 完成可运行层，DB/E2E 标 blocked |
| 外部调用未执行 | 无本轮 API 授权与预算 | live 标 not_run；本轮离线交付继续 |

禁止用扩大行数上限、放宽授权、提高工具次数、补零、改 gold set、跳过断言来消除失败。必须有明确证据才能把问题归为上述任何一类。

## 7. 可直接粘贴到 zcode 的启动指令

```text
请在 D:\projects\bi-agent 执行这份《BI Agent 可用性与回归体系推进计划》，从 T0 开始连续推进到 T8。

我希望得到可运行的改进、可信回归和验收证据。请先读计划与项目约定，核实当前源码和已有修改。历史已修项先验证，不重复实现；普通本地开发步骤自主完成，不每一步询问我。

默认零外部模型 API、零快麦调用，不部署、不推送、不启用生产开关。现有 bi_agent_test 可能有真实数据，必须使用本计划规定的独立合成实例，禁止清空或重建现有库。保留用户未提交修改。

按任务记录 checkpoint，关键改动先最小复现再修复，串行运行数据库测试。环境或业务决策阻塞时记录具体原因并继续独立任务。每个阶段提供改动、测试结果、未解决问题和下一步，不用文档数量或测试数量代替交付。结束时给出实际运行证据、剩余阻塞和下一条可执行命令。
```

## 8. 编写时自审记录

- 已区分 10 月 8 日离线验收与 10 月 9 日真实模型实测，没有沿用“从未跑 live”的错误概括。
- 已将同日报告后续修复列为先验证，避免重新实现参考价筛选和公开校验提示。
- 已识别测试库可能包含真实经营数据，采用独立实例而非只换库名；具体沙箱防误连与清理由 T1 验证。
- 已利用现有 ModelReply.usage，不重复开发模型客户端；缺失计量不记零。
- 已将脚本回放与模型理解分开，预留 live 路径但不依赖外部额度。
- 五项 Review Focus 均有明确任务和用例；最终完成以真实执行证据为准。
