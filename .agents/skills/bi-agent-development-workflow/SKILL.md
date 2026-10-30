---
name: bi-agent-development-workflow
description: Governs implementation, bug-fixing, review, progress checks, plan continuation, validation, and commits in the bi-agent repository. Use whenever developing D:/Projects/bi-agent, continuing a roadmap task, delegating work, reviewing a change, reporting progress, or reading a newly added plan.
compatibility: Pi coding agent in the trusted bi-agent Git repository with native pi-subagents, Git, uv, PostgreSQL test database, Node.js, and npm available.
metadata:
  project: bi-agent
  version: "1.1"
---

# BI Agent 开发流程

在 `D:/Projects/bi-agent` 内进行开发、修复、审查、验收、提交、进度检查或计划续作时，遵循本流程。除非用户在当前会话中明确覆盖某条可变规则，否则不要自行降级。

默认阅读入口为根 `AGENTS.md` 与 `docs/PROJECT_STATUS.md`。项目状态是进度、证据摘要和待办的唯一维护入口；运行方法、业务口径分别按需查 `docs/runbook.md`、`docs/metrics.md`。`docs/archive/` 默认不读、不搜索、不递归跟随链接；历史计划、验收、例外授权仅作追溯，不是本轮指令。

## 1. 不可变边界

1. **拼多多永久不接入**：不得新增 PDD 连接器、来源注册、支付能力、凭据、配置、同步、onboarding 或“即将可用”暗示。PDD 只能作为 `unsupported`、`excluded_scope`、`capability_unavailable` 等负向边界和回归用例出现。
2. **禁止未经明确授权的外部操作**：不得访问生产/预发布数据库，不得执行真实平台 API、真实同步/回填/对账、真实模型调用、部署、代理/OIDC 变更、生产迁移、备份恢复或计划任务变更。
3. **只使用隔离测试环境**：正常回归使用独立合成实例 `bi_agent_eval_test`，通过仓库根 `.env.evaluation` 加载。既有 `bi_agent_test` 承载过真实数据，不能仅凭 `_test` 后缀 seed/reset；不得把测试 DSN、stub key 或合成证据描述成生产就绪。
4. **SQL 可开发、不可生产执行**：允许新增版本化迁移并在本机测试库按顺序及幂等重放；禁止对真实环境执行 DDL。
5. **保护操作者文件**：除非用户明确要求，不得读取、修改、删除、暂存或提交：
   - `.cloudflared.*`
   - `.streamlit.*`
   - `.tools/`
   - `taoxi-probe-status.py`
   - `可参考/`
   - `.worktrees/`
   - `.env*` 中的密钥或凭据
6. 不得把离线测试、stub 模型、合成来源、静态检查或只读审查表述成真实来源、live 模型或发布门禁已通过。

## 2. 开工检查

开始一个 Task 或读取新计划时：

1. 确认仓库、分支、HEAD 和状态：
   ```bash
   git -C D:/Projects/bi-agent status --short
   git -C D:/Projects/bi-agent log -5 --oneline
   ```
2. 区分三类工作树内容：
   - 当前 Task 的受控改动；
   - 已存在且必须保留的用户/操作者改动；
   - 上述受保护的未跟踪运行文件。
3. 若存在来源不明的跟踪改动，先调查归属；不要覆盖、重置、清理或顺手提交。
4. 先读 `AGENTS.md` 和 `docs/PROJECT_STATUS.md`，按当前任务读取相关代码、测试及现行文档章节；不自动展开文档的全部引用。只有用户指定、需要追溯决策/原始验收证据，或当前契约存在无法解释的冲突时，才说明目的并读取对应归档文件的必要章节。
5. 用当前源码、相关测试和必要的提交历史核实交付状态，避免重复实现。历史 checkbox、旧任务编号和测试数字不代表当前状态；有冲突时核对相关实现与批准契约，并把结论更新到项目状态。
6. 对 3 步以上工作建立 `todo`；任何时刻只保留一个 `in_progress` 任务。
7. 在修改前记录基线验证结果；共享数据库测试必须串行运行。

## 3. 原生子代理规范

### 3.1 必须使用原生 subagent

- 使用 Pi 原生 `subagent` 工作流，以便问题、结果、验收和 supervisor 请求自动回传主会话。
- **不要使用 Herdr 项目窗格**承载本项目开发，除非用户在当前会话明确要求；Herdr 同级会话的问题不会自动回传主会话。
- 启动前先调用：
  1. `subagent({ action: "list", capabilities: true })`
  2. `subagent({ action: "models" })`
- 子代理模型默认使用精确模型 `deepseek/deepseek-v4-flash`，并通过模型后缀或配置显式指定 **high** 思考强度：`deepseek/deepseek-v4-flash:high`。
- 若该模型不可用，不得静默替换；先报告并请求用户决定。

### 3.2 一个顶层工作流、一个写者

- 多步骤任务只发起 **一个**顶层异步 workflow；开发、审查和修复子步骤在该 workflow 内顺序运行。
- 同一工作树同时只能有一个写者。审查必须等写者结束后再开始。
- 工作树已有未提交改动时，不要擅自创建基于旧 HEAD 的隔离 worktree；保持共享工作树单写者。
- writer 负责实现和测试，但不得 `git add`、commit、push、merge 或清理用户文件。
- reviewer 使用 fresh、只读上下文，独立检查真实源码，不只复述 writer 报告。
- reviewer 重点报告 P0/P1；P2 记录为后续项，除非它会使本轮验收本身不可信，或用户明确要求修复。
- 发现 P0/P1 后，恢复同协议 writer 做最小修复，再进行 fresh 复审；在最终 `VERDICT: OK` 前不得提交。

### 3.3 子代理提示必须包含

- 仓库路径、分支、精确基线 HEAD；
- `docs/PROJECT_STATUS.md`、当前 Task 的目标与允许修改范围；仅附与切片直接相关的文档章节，不要求子代理重读整个历史计划；
- 明确验收标准、测试命令和输出文件；
- 单写者、禁止 stage/commit/push；
- PDD、生产、密钥、真实来源/模型/同步和受保护路径边界；
- 必须清理 scratch 文件并保持 LF；
- 报告改动文件、命令结果、未执行验证、残留风险及 `git diff --cached` 为空的证据。

### 3.4 异步与 supervisor

- 原生异步工作流会自动通知；不要用 sleep、循环 status 或反复读日志来等待。
- 只有用户要求状态、出现卡死证据、收到 `needs attention`，或需要终态输出细节时才做一次状态检查。
- 收到 supervisor 请求时，先通过 `subagent_supervisor` 回复 pending 请求，再 steer/resume。
- 工作流、扩展、验收输出或子工具基础设施失败时：停止并报告精确 run、cwd、分支、HEAD、工作树和已保留 diff；优先用同协议 resume/retry。不得静默切换到 Herdr、外部 CLI 或非治理模式。
- “验收报告格式失败”与“代码/测试失败”必须分开描述；前者不能抹掉已保留的代码和测试证据。

## 4. 实施纪律

1. 代码缺陷先写能证明问题的红灯测试，再写最小实现；对随机/脆弱测试使用反向突变或错误期望证明断言不是恒真。纯文档及注释引用整理直接验证链接、入口与内容保全，不为文案建立测试。
2. 断言结构化字段和值，不要在包含随机 UUID/哈希句柄的整份序列化 payload 中搜索数字子串。
3. 缺失、未知、未认证、不可比较不得当作 `0`；只有有完整覆盖证据的真实零才能发布为零。
4. 授权、能力、覆盖、时间口径、指标口径和 artifact lineage 必须 fail closed；不得由模型文字绕过服务端门禁。
5. 当前轮输入不得通过会话过滤器意外继承到下一轮，尤其是目标价、库存阈值和临时范围。
6. 保持集合查询和固定图执行预算；不得按店铺/商品逐项查询造成 N+1。
7. 不新增依赖，除非计划或用户明确批准；新增依赖时同时更新锁文件、说明理由并验证许可证/构建。
8. 使用仓库现有命名、原因码、数据集与 lineage 契约；不要创建平行实现或重复定义。
9. 保持 LF，删除探针、临时脚本、补丁副本和测试输出。
10. 不修改与当前 Task 无关的 P2 或历史债务；记录到报告或后续 todo。
11. 交付时直接更新 `docs/PROJECT_STATUS.md` 的相关状态、证据与待办，避免另建重复进度文档；只有用户要求或独立证据确需留存时才新增专题记录。归档前将仍有效的未完成事项同步回项目状态。

## 5. 父级验收门禁

子代理报告不能替代主会话复验。所有命令串行运行，长输出用 context-mode 过滤出结论和失败名称。

纯文档/注释路径改动只需验证本地链接、默认阅读路径、归档原文与用户改动保全；不触发以下应用全量测试、数据库迁移或真实模型调用。业务行为、schema 或配置语义改动仍执行对应门禁。

### 5.1 后端

在 `backend/`：

```bash
uv run --locked --env-file ../.env.evaluation python -m unittest discover -s tests -t .
uv run --locked --env-file ../.env.evaluation python -m tests.acceptance --offline
```

同时运行当前 Task 的专项模块。若改动数据库测试门禁，再在移除 `BI_TEST_ADMIN_DSN` 的环境中运行对应套件，要求正常 skip 而不是 error。

### 5.2 前端

若 Task 修改前端，或作为里程碑/提交前全量门禁，在 `frontend/`：

```bash
npm test
npm run build
```

### 5.3 迁移与数据库

若新增或修改迁移：

- 只在已验证身份的独立合成实例 `bi_agent_eval_test` 运行；
- 从 `001` 到最新版本顺序执行；
- 重放最新迁移验证幂等；
- 验证测试角色权限和 fail-closed 行为；
- 不执行生产或预发布迁移。

### 5.4 Git 与范围

提交前必须通过：

```bash
git diff --check
git diff --cached --name-only
git status --short
```

并确认：

- 测试全部通过且没有未解释 skip；
- staged 区在父级明确暂存前为空；
- 无受保护路径、密钥、依赖、PDD 接入或无关文件；
- 未执行的 live、生产、来源和部署验证已明确列出；
- 审查最终无 P0/P1。

任一必需测试失败、实现不完整或错误未解决时，不得把 todo 标为 completed，也不得提交。

## 6. 提交规范

1. 子代理不提交；主会话在最终复审和父级验证后提交。
2. 每个 Task 一个逻辑提交。多个 Task 的改动混在工作树时，按精确文件/hunk 拆分，并对每个快照独立验证。
3. 只暂存白名单路径；比较实际 staged 清单与预期清单，不一致时撤销暂存并停止。
4. 提交前运行 `git diff --cached --check`；提交信息使用计划建议的祈使式 Conventional Commit。
5. 提交后报告 commit hash、测试计数、未执行验证和残留风险。
6. 提交后工作树只允许保留开工时已存在的受保护运行文件；不得顺手清理它们。

## 7. 进度汇报

用户询问进度时，简洁报告：

- 已完成、正在进行、尚未开始的 Task；
- workflow 和当前 child run ID；
- 当前阶段（开发、修复、审查、父级验证、提交）；
- 最近一次专项/全量/离线/前端测试计数；
- 真实 blocker 或待用户决策；
- 是否保持无 stage、无生产操作、无 PDD 接入。

不要把“子代理仍在思考”“报告格式失败”或“未运行外部验证”说成代码失败，也不要在没有证据时说“已卡死”。

## 8. 新计划续作

仅在用户指定新目标，或 `docs/PROJECT_STATUS.md` 登记了可继续的活动任务时续作：

1. 先查项目状态中的已完成、待办、边界与接续条件，不按文件日期扫描历史目录寻找“下一个计划”。没有已授权活动任务时，不自动激活旧路线图。
2. 对照当前 HEAD、相关源码与测试确定本次范围。新活动计划先在项目状态登记；只读该任务必需的规格、契约章节与验收标准，不递归展开引用。
3. 若确需历史追溯，先说明具体问题，再按文件/章节读取 `docs/archive/`，提取仍有效的结论回项目状态。归档内旧“必读”、执行命令、临时授权不继承到本轮。
4. 真实模型、真实来源、生产/预发布、部署、凭据或业务口径决定按当前授权边界处理，不能因旧计划文字自行执行；拼多多永久不接入。
5. 已授权代码任务继续执行本技能的开发、审查、验证与提交流程；普通进展直接更新项目状态，不再生成平行 checkpoint。
