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

本机两个库（`bi_agent`、`bi_agent_test`）已经建好并随命名卷保留，重建容器不需要重跑 DDL；新克隆时按上面顺序在两个库各跑一遍。

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
