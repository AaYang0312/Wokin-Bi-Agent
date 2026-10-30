# BI Agent

面向电商经营分析的内部聊天助手。使用快麦同步事实与受控手工导入来源，在授权、覆盖和指标口径满足时回答问题；缺失或未认证数据保留原因，不补零。

**当前进度、能力边界、验证结果与待办统一见 [项目状态](docs/PROJECT_STATUS.md)。** Agent 先读 [AGENTS.md](AGENTS.md) 和项目状态，按任务定位代码；历史计划与验收资料已归档，默认无需阅读。

## 架构

```text
快麦 / 手工导出 → backend/ → PostgreSQL
                                 ↓
React/Vite → /api → FastAPI → 受限 Agent / 查询状态图
```

前端提供聊天、结果图表、缺数上传、审核与通知等受门禁控制的界面。服务端负责身份、实体引用、来源、覆盖、指标口径与预算；不提供通用 Text2SQL 或模型自主写库能力。

## 文档入口

| 需要什么 | 文档 |
| --- | --- |
| 项目现状、未完成事项、最近验证 | [docs/PROJECT_STATUS.md](docs/PROJECT_STATUS.md) |
| 启动、配置、隔离测试、迁移与运维 | [docs/runbook.md](docs/runbook.md)，仅查相关章节 |
| 指标定义、来源条件与拒答边界 | [docs/metrics.md](docs/metrics.md)，仅查相关章节 |
| 可复跑的合成演示 | [docs/demo.md](docs/demo.md) |
| 开发 agent 阅读与维护规则 | [AGENTS.md](AGENTS.md) |

## 本地开发

依赖 Python 3.11+、uv、Node.js 22 和 PostgreSQL 17；依赖版本以锁文件为准。应用使用独立 `.env.app`，同步使用 `.env.sync`，回归使用独立合成沙箱 `.env.evaluation`，不得混用身份或凭据。

[运行手册](docs/runbook.md) 提供完整步骤。开发前端地址为 `http://127.0.0.1:5175`，代理至后端 `127.0.0.1:8001`，`APP_PUBLIC_ORIGIN` 必须匹配。正常回归使用 `bi_agent_eval_test`；既有 `bi_agent_test` 承载过真实数据，不能作为可随意重建的测试沙箱。

新能力的仓库默认开关及启用前置统一列在项目状态。生产、真实模型、真实来源操作需要明确授权；离线测试通过不代表已上线。
