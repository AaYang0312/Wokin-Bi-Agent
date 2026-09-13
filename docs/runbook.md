# BI Agent 运行手册

## 配置边界

| 文件 | 运行进程 | 必需内容 |
| --- | --- | --- |
| `.env.app` | FastAPI | `APP_*`、`BI_SHOP_IDS`、`BI_APP_DSN`、一个 provider 的模型配置 |
| `.env.sync` | 快麦同步 | `BI_WRITER_DSN`、`BI_SHOP_IDS`、`KUAI_MAI_*` |
| `.env.test` | 测试 | 独立 `*_test` 库的 DSN 与 fake 模型配置 |

三者都不提交。API 环境若出现 `BI_WRITER_DSN` 或快麦凭证会在启动时拒绝运行。

`APP_ENV=development` 时身份固定为 `local-development`。生产必须设置 HTTPS 的 `APP_PUBLIC_ORIGIN`、非空 `APP_ALLOWED_SUBJECTS`，并通过反向代理写入 `AUTH_SUBJECT_HEADER`（默认 `X-Auth-Request-Sub`）。代理必须先删除浏览器传来的同名头，再写入 OIDC 的真实 `sub`。

模型只支持 `LLM_PROVIDER=qwen|deepseek`。必须显式给出 `LLM_MODEL`，只填已选择 provider 的密钥；不做自动切换或重试。默认兼容地址是 Qwen 的 `https://dashscope.aliyuncs.com/compatible-mode/v1` 和 DeepSeek 的 `https://api.deepseek.com/v1`。真实联调前确认账号地域、型号和费用；当前两者均标记为“未实测”，直到在合成测试库跑完 smoke 和 20 题。

## 本地开发

先由管理员在本地生产库或独立 `*_test` 库中按同一顺序初始化；每个库都必须完整执行 `001 → 002 → 003 → 004 → 005 → 007 → 008 → 009`，不可跳过运行追踪迁移。编号 006 已作废（不补旧序号迁移），数据就绪能力落在 008，运行契约版本化落在 009。005 / 007 / 008 / 009 都是代码硬依赖：少了它们，`reporting.v_product_daily` 没有名称与规格列、`v_coverage` 没有质量列、`v_source_batches` 不存在，或运行层无法写入血缘与请求身份，会直接报列/表不存在。

```powershell
psql -d bi_agent -f backend/sql/001_init.sql
psql -d bi_agent -f backend/sql/002_kuaimai_mapping_repair.sql
psql -d bi_agent -f backend/sql/003_kuaimai_metric_semantics.sql
psql -d bi_agent -f backend/sql/004_query_runtime.sql
psql -d bi_agent -f backend/sql/005_product_dimension.sql
psql -d bi_agent -f backend/sql/007_catalog_identity.sql
psql -d bi_agent -f backend/sql/008_data_readiness.sql
psql -d bi_agent -f backend/sql/009_query_provenance.sql
```

单实例：全程持有数据库 advisory 锁，重复启动立即失败。

### 平台路由与来源注册表

当前开发基线为 main，旧分支的同步入口统一迁入 backend。淘系“入库完成”与“查询可用”分别验收：[多来源契约](superpowers/specs/2026-09-12-multi-source-metrics-design.md)及[任务5/11](superpowers/plans/2026-09-07-ecommerce-bi-agent.md)。任务 5.1–5.3 已交付：来源注册表与逐指标能力门禁（`backend/bi_agent/sources.py`）、覆盖按 (店铺×来源×实体) 取交集与付款时间口径三态认证、未匹配退款/未认证支付改为可量化披露（`coverage_time_basis_unverified` 等进入词表）。**basis 全链路与版本失效（5.4）仍未完成**；pdd 不得因档案或单据存在被宣称支付可用（2026-09-12 已决定不接入拼多多支付）。

- 订单源由 `sources.py` 注册表按 `bi.shops.platform` 解析：`tb`/`tm`/`pdd` 用 `erp.trade.outstock.simple.query`（销售出库·非敏感字段），`fxg`/`jd`/`kuaishou`/`wxsph`/`wsxc` 用 `erp.trade.list.query`。**未登记平台（1688、淘工厂等）没有默认回退**：`_shop_order_source` 直接报错退出，逐店循环里该店被跳过并打印原因，绝不把“拿不到”写成“没有”。`sync_state` 主键含 source，两通道水位/覆盖互不干扰。
- 新增一个来源必须先拿齐方法名、权限、时间语义与金额对账证据，再写进注册表；数据库里不维护第二套可自由配置的来源表。
- 先跑 `shops` 刷店铺档案再跑订单命令，缺档案的店会直接报错（防假覆盖）。
- 淘系口径为 **ERP 出库非敏感字段**，非平台账单口径；收件人/买家昵称/手机号等 PII 字段在规范化入口即丢弃并有守护用例，不得扩列。详见 `docs/superpowers/research/2026-09-12-taoxi-onboarding.md`。

分别启动后端和前端：

```powershell
Set-Location backend
uv sync --locked
uv run --env-file ../.env.app uvicorn bi_agent.api:create_runtime_app --factory --host 127.0.0.1 --port 8001 --reload
```

```powershell
Set-Location frontend
npm ci
npm run dev -- --host 127.0.0.1
```

Vite 将 `/api` 代理到 `http://127.0.0.1:8001`。开发页必须通过 `http://127.0.0.1:5175` 打开，并设置 `APP_PUBLIC_ORIGIN=http://127.0.0.1:5175`，否则写请求会被 Origin 检查拒绝。

## 数据库和同步

### 本机数据库容器

本机 PostgreSQL 跑在 Docker（`deploy/postgres/docker-compose.yml`，`postgres:17.6` + 命名卷
`bi-agent-pg_bi_pg_data`）。日常启停、备份还原、其他主机经 Tailscale 连接与安全边界见
[`deploy/postgres/README.md`](../deploy/postgres/README.md)：

```powershell
cd deploy\postgres
docker compose start        # 或 up -d；docker compose stop 不会删数据
```

宿主端口 `54329`、`postgres` 本机免密（trust），与迁移前的便携版集群一致，所以 `.env.app` / `.env.test` /
`.env.sync*` 的 DSN 无需修改；若把 `pg_hba.conf` 换成 `scram-sha-256`，所有 DSN 要同步加口令。
库里的 schema 版本以 `\dt bi.*` 和下面的迁移清单为准，缺哪个补哪个（迁移文件可重复执行）。
本机（Windows）当前两库都已跑到 **016**：`bi_agent` 拉取前停在 009，2026-09-13 补跑
014 → 015 → 016；`bi_agent_test` 停在 001，同批补跑 002 → 016（003 只重定义
`reporting.v_product_daily`，已被 007 的宽版本取代，`CREATE OR REPLACE VIEW` 没法减列，所以跳过）。
补跑前各自用
`pg_dump -Fc` 存了 schema 前快照（在仓库外的 `D:\Projects\pg17\migration-20260912\`），
补跑后逐表 `count(*)` 与全行 md5 与迁移前一致（差异只有 016 新建的空表 `bi.channel_items`
与 015 新加的三列——拿旧列重算 md5 仍是同一个值）。

DDL 只由管理员执行；`bi_sync` 拥有 `bi` schema 事实表读写权限；`bi_reader` 只有 `reporting` schema 指定视图的 SELECT 权限（默认只读、5 秒超时）。角色密码通过管理员 `\password` 或现有密钥设施设置，SQL 文件不含密码。

### 角色边界与迁移

`bi_sync` 写业务事实，`bi_reader` 只能读报表视图，`bi_app` 读取相同视图并读写 `bi.app_chats` 与 `bi.app_messages`。API 查询与消息写入都有五秒 SQL 超时；同一会话同时生成时返回 `409 chat_busy`。

已有数据库升级快麦字段映射与指标口径时，先由管理员执行前向迁移，再部署同步代码。迁移不会重写历史事实；为使修正后的状态、行号、行类型和完成时间生效，须对保留历史范围显式重放订单和售后，再刷新店铺档案。同步启动会检查 002 所需列；缺失时以 `schema_outdated` 拒绝写入。`replay` 仅会重规范化相同 `source_updated_at` 的版本，不会让较旧上游版本覆盖较新版本。

**部署顺序硬约束：014 与 015 必须在应用新代码之前跑完。**014 扩了终止原因 CHECK
（新增 `capability_unavailable`、`coverage_time_basis_unverified`，009 已应用不可改写）；
015 给血缘表加来源注册表版本、口径签名与当时生效的质量规则三列，运行层写血缘时按列名
写入。库停在前一版时，运行层会预检一次早报 `schema_outdated:<码>`，但那等于服务不可用
——先跑迁移，再部署。

```powershell
Set-Location backend
psql -d bi_agent -f sql/002_kuaimai_mapping_repair.sql
psql -d bi_agent -f sql/003_kuaimai_metric_semantics.sql
psql -d bi_agent -f sql/004_query_runtime.sql
psql -d bi_agent -f sql/005_product_dimension.sql
psql -d bi_agent -f sql/007_catalog_identity.sql
psql -d bi_agent -f sql/008_data_readiness.sql
psql -d bi_agent -f sql/009_query_provenance.sql
psql -d bi_agent -f sql/014_multi_source_contract.sql
psql -d bi_agent -f sql/015_provenance_basis.sql
psql -d bi_agent -f sql/016_channel_catalog.sql
uv run --env-file ../.env.sync python -m bi_agent.sync shops
uv run --env-file ../.env.sync python -m bi_agent.sync replay --entity orders --start <保留历史起日> --end <截止日的下一日>
uv run --env-file ../.env.sync python -m bi_agent.sync replay --entity aftersales_occurrence --start <保留历史起日> --end <截止日的下一日>
```

成交名称与规格快照（`product_name_snapshot` / `sku_label_snapshot`）只在新写入或显式重放时采集：执行完 007 的历史行仍是 NULL，名称回落到商品档案，规格则不展示，不从商品名猜。测试库实测：1488 行商品日数据全部有档案名、0 行有成交快照与规格，属预期而不是缺陷。

**部署合单取证修正（`basis='items_merged'`）后必须重算历史支付事实**：判定只影响新发生的
`rebuild_payments` 调用，已落库的合单仍会停在 `undetermined`（金额为 NULL，店铺侧看不见这笔收入）。
对历史范围跑一次 `replay --entity orders` 即可重算；完成后核对：

```powershell
psql -d bi_agent -c "SELECT basis, verified, count(*), coalesce(sum(amount),0) "
                "FROM bi.order_payments GROUP BY 1,2 ORDER BY 1;"
```

预期 `undetermined` 归零（或仅剩确实证据不全的单），并出现 `items_merged` 行。本地实测：
166754 单店 685 个 undetermined 全部转为已核验，救回 68,617.84 元，且降级守卫拦截 0 次
（没有任何已核验收入被清零）。

```powershell
Set-Location backend
uv run --env-file ../.env.sync python -m bi_agent.sync shops
uv run --env-file ../.env.sync python -m bi_agent.sync backfill --days 90
uv run --env-file ../.env.sync python -m bi_agent.sync incremental
uv run --env-file ../.env.sync python -m bi_agent.sync reconcile --days 7
# 对账凭证落下后才能开通指标能力：先只报告，确认后再接着跑 --apply
uv run --env-file ../.env.sync python -m bi_agent.sync capabilities
uv run --env-file ../.env.sync python -m bi_agent.sync capabilities --apply
```

同步在完整分页、校验和事务提交后才推进水位。分页、权限或上游错误不会成为零业务数据；每日重核最近七天以处理晚到退款。

空返回与支付核验的额外约束：

- 「成功却没有结果列表」且没有 `total=0` / `hasNext=false` 正向完成证据时按**不可信空**处理：报 `unknown_empty`，整窗口不写 covered。只有实测会省略列表的接口（见 `docs/superpowers/research/2026-09-06-kuaimai-data-recheck.json`：`erp.item.history.cost.price.query`、`erp.item.sku.list.get`、`stock.api.status.query`、`erp.item.warehouse.list.get`、`erp.wave.logistics.order.query`、`erp.aftersale.refund.warehouse.query`、`purchase.order.query`）才容许把缺列表解析为「未核验的空」；同步链在用的 `erp.trade.list.query` / `erp.aftersale.list.query` / `erp.shop.list.query` 均实测返回列表，一处也不放开。
- 已核验的支付事实不会被更旧或尚未齐平的证据静默清零。`rebuild_payments` 的降级守卫保留原事实（也不再抹掉 `order_items.allocation_verified`），拦截数记在各同步命令输出的 `payment_downgrade_blocked` 字段，日志字段 `error_code=payment_downgrade_blocked`（只带店铺与订单号摘要，不带订单号明文）。该计数持续上升说明有拆合单兄弟行未到齐，需对相应窗口执行 `replay`。

部署时可注册每小时同步任务，工作目录固定为 `backend`：

```powershell
$taskUv = (Get-Command uv).Source
$syncAction = New-ScheduledTaskAction -Execute $taskUv -Argument 'run --locked --env-file ../.env.sync python -m bi_agent.sync incremental' -WorkingDirectory 'D:\Projects\bi-agent\backend'
$syncTrigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Hours 1)
$syncSettings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName 'BI Agent Hourly Sync' -Action $syncAction -Trigger $syncTrigger -Settings $syncSettings
```

另建每日低峰的 `reconcile --days 7` 任务。运行账户只读取同步配置；命令参数不含密码。

## 数据就绪与质量核对

覆盖、业务截止与质量是三件事（口径见 `docs/metrics.md` 第 6 节）。核对与推进：

```powershell
# 看当前就绪情况：覆盖区间、业务截止、质量状态与所用口径
psql -d bi_agent -c "SELECT entity, shop_id, data_as_of, quality_status, quality_rule, "
                "       last_error_code FROM reporting.v_coverage ORDER BY entity, shop_id;"
# 看某段时间是哪几批同步出来的（window_kind='business' 才能当覆盖凭证）
psql -d bi_agent -c "SELECT shop_id, entity, batch_id, window_kind, mode, row_count "
                "FROM reporting.v_source_batches ORDER BY recorded_at DESC LIMIT 20;"
# 对账：只有本窗口确有 reconcile 批次，才能把 unknown 升为 passed
uv run --env-file ../.env.sync python -m bi_agent.sync reconcile --days 7
```

推进规则与限制：

- 覆盖、截止与质量按 `(店铺 × 来源 × 实体)` 取**交集**判定（计划 Task 5.2b）：
  `covered_windows` 与 `suggested_window` 都是“每家、每个依赖都齐”的区间，不再拿各店
  已覆盖段的并集当建议窗口；缺口按实体、店铺与具体来源归因，旧通道残留的状态行不再参与。
- 付款时间口径未认证（Task 5.2c）分两档：`disproved`（销售出库接口实测按自身时间裁剪）
  不得给支付窗口类结果，返回 `coverage_time_basis_unverified`；`unmeasured`（同通道但未
  逐店对照）可出数但必须披露为可观测样本。`erp_documents` 是单据计数、不是支付窗口主张，
  两档都只披露不拒答。等回填不会改变这个结论，要的是与后台账单/业务日期的对照登记。

- 历史遗留的“从未核验”统一是 `unknown`，仍可出数但会带「来源质量未核验」说明；不得直接当数据有错。
- 对账发现归属未确认的平台成功退款时，**不再**把来源降为 `failed`：它是一道可量化限制，
  逐结果披露条数/分母/金额/比例，同时把 `quality_reason` 记为 `unmatched_success_refunds`。
  金额冲突、分页或覆盖凭证损坏等真实核验失败仍为硬门禁。
- 质量规则升到 `kuaimai-reconcile/2` 后：旧 `passed` 视同未核验（自动降为 `unknown`），
  而**旧规则因 unmatched 被标 `failed` 的店不会自动解禁**——必须重跑一次能落在窗口内
  留下批次凭证的 `reconcile`，再跑 `capabilities --apply`。禁止批量清空 `failed`。
- `quality_rule` 变更后旧 `passed` 自动失效，必须重跑对账。
- **指标能力标签（`bi.shops.capabilities`）只能由对账证据开通**：先 `reconcile`，再跑能力重算，最后才可能出数。

```powershell
# 先报告差异（默认不写库）：逐店显示当前标签、证据推导出的标签、是否有变化与警告
uv run --env-file ../.env.sync python -m bi_agent.sync capabilities
# 确认无误后回写；证据消失时该标签同样会被回收
uv run --env-file ../.env.sync python -m bi_agent.sync capabilities --apply
# 授权范围（BI_SHOP_IDS）之外的店也可能挂着旧标签：回收时加上这个开关
uv run --env-file ../.env.sync python -m bi_agent.sync capabilities --all-shops --apply
```

输出逐店携带 `quality_rule` 与当前/推导出的标签：能力回收与开通走同一条命令，日志里能
直接回答“这几家店的数是按哪版取证口径开的”。

能力标签取值与指标同名（`paid_amount`/`paid_orders`/`erp_documents`/`aov`/`quantity`/
`product_paid_amount`/`refund_amount`/`cash_difference`/`cohort_refund_rate`）。旧的
`orders`、`aftersales_*` 实体标签不再授予任何指标；空数组就是“该店全部指标能力未开通”，
金额查询会在跑 SQL 之前返回 `capability_unavailable`。上线本迁移后未跑 `capabilities --apply`
前，聊天查询会全部报能力未开通——这是 fail closed 的预期，不是回归。
- `row_count` 为 NULL 是“当时未统计”，不等于 0 行；分页中断的窗口不会留下批次凭证，覆盖也不会推进。

执行历史回填、增量或核对时，按下表逐格记录实际结果，**没跑过就写未执行**，
不得拿代码存在或店铺档案数当完成：

| 平台 / 店铺 | 实体 | 回填 | 增量 | 核对 | 质量状态 | 未完成或失败原因 |
| --- | --- | --- | --- | --- | --- | --- |
| 抖音 166754 | orders | 已完成至 2026-09-09 | 未部署定时 | 未执行 | unknown | 未跑过 reconcile，无凭证可升 passed |
| 其余 41 家店铺 | 全部 | 未执行 | 未执行 | 未执行 | unknown | 未进入授权范围，不能拿档案数当覆盖 |

## 同源部署

发布 `frontend/dist` 静态文件。反向代理将 `/api/*` 转发到 `127.0.0.1:8000`，其余路径提供 SPA 回退；关闭 SSE 路径的响应缓冲。FastAPI 仅运行于回环地址且使用单 worker：

```powershell
Set-Location backend
uv run --env-file ../.env.app uvicorn bi_agent.api:create_runtime_app --factory --host 127.0.0.1 --port 8000 --workers 1
```

共享试用前验证：未登录被代理拦截；身份 A 不能读取或修改 B 的会话；伪造身份头无效；错误 Origin 返回 403；SSE 首个 `status` 及时到达且刷新可以恢复最后消息。

## 检查、故障与恢复

```powershell
Set-Location backend
uv run python -m unittest tests.test_core -v
uv run --env-file ../.env.test python -m unittest tests.test_db tests.test_api -v
uv run --env-file ../.env.test python -m tests.acceptance --offline
Set-Location ../frontend
npm test
npm run build
```

模型、数据库或工具失败时，消息 SSE 返回 `error` 后再返回 `done`；它不会包含调用栈、DSN、请求体或 ERP 标识。会话仅保存用户可见文本和脱敏的聚合附件。

运行记录创建或持久化失败会阻止该次经营查询完成并返回结果，浏览器同样只会收到安全的 `error`、`done` 终态。先恢复数据库的运行记录写入能力，再由用户重新发送原问题；不要从失败的 SSE 或日志内容中复制真实店铺、商品或 ERP 标识。

### 查询运行只读诊断

使用受控管理员只读连接排查运行追踪。以下查询只读取运行元数据、事件顺序和 Artifact 元数据；不得查询或导出 Artifact `payload`，示例也不记录真实店铺标识。

```sql
-- 某会话最近一次经营查询
SELECT id, status, current_node, revision, started_at, completed_at
FROM bi.query_runs
WHERE chat_id = '<chat-uuid>'
ORDER BY started_at DESC
LIMIT 1;

-- 某次查询的状态变化顺序
SELECT revision, node, event_type, status, created_at
FROM bi.query_run_events
WHERE run_id = '<run-uuid>'
ORDER BY revision;

-- 某次查询产出的 Artifact 元数据（刻意不读取 payload）
SELECT id, artifact_type, data_as_of, coverage, created_at
FROM bi.query_artifacts
WHERE run_id = '<run-uuid>'
ORDER BY created_at;
```

备份使用受控的 PostgreSQL 服务名，并只恢复到预建的独立库：

```powershell
pg_dump --dbname="service=bi_backup" --format=custom --file=backups/restore-check.dump
psql "service=bi_restore_check" -c "SELECT current_database();"
pg_restore --dbname="service=bi_restore_check" --no-owner --no-privileges backups/restore-check.dump
```

首次真实恢复前先记录事实表行数、金额摘要、覆盖状态和会话数，恢复后比较同一摘要。备份文件放在受限 ACL 的加密磁盘，保留七天。
