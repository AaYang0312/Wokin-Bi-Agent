# BI Agent 运行手册

## 配置边界

| 文件 | 运行进程 | 必需内容 |
| --- | --- | --- |
| `.env.app` | FastAPI | `APP_*`、`BI_SHOP_IDS`、`BI_APP_DSN`、一个 provider 的模型配置 |
| `.env.sync` | 快麦同步 | `BI_WRITER_DSN`、`BI_SHOP_IDS`、`KUAI_MAI_*` |
| `.env.test` | 测试 | 独立 `*_test` 库的 DSN 与 fake 模型配置 |

三者都不提交。API 环境若出现 `BI_WRITER_DSN` 或快麦凭证会在启动时拒绝运行。

`APP_ENV=development` 时身份固定为 `local-development`。生产必须设置 HTTPS 的 `APP_PUBLIC_ORIGIN`、非空 `APP_ALLOWED_SUBJECTS`，并通过反向代理写入 `AUTH_SUBJECT_HEADER`（默认 `X-Auth-Request-Sub`）。代理必须先删除浏览器传来的同名头，再写入 OIDC 的真实 `sub`。

模型只支持 `LLM_PROVIDER=qwen|deepseek`。必须显式给出 `LLM_MODEL`，只填已选择 provider 的密钥；不做自动切换或重试。默认兼容地址是 Qwen 的 `https://dashscope.aliyuncs.com/compatible-mode/v1` 和 DeepSeek 的 `https://api.deepseek.com/v1`。真实联调前确认账号地域、型号和费用；当前两者均标记为“未实测”，直到在合成测试库跑完 smoke 和修订后的 26 题（旧 20 题的 14/20 只是历史基线，不代表多来源可用）。

## 本地开发

先由管理员在本地生产库或独立 `*_test` 库中按同一顺序初始化；每个库都必须完整执行`001 → 002 → 003 → 004 → 005 → 007 → 008 → 009 → 014 → 015 → 016 → 017 → 018 → 019 → 020 → 021 → 022`，不可跳过运行追踪迁移。编号 006 已作废（不补旧序号迁移），数据就绪能力落在 008，运行契约版本化落在 009，多来源契约与口径血缘落在 014/015，渠道映射落在 016，商品运营参考面与经营图所需的 reporting 视图落在 017，上架价复核的渠道在售快照与本轮目标价冻结落在 018，库存两级预警的实物 / 渠道可售快照、库存池连接关系与版本化阈值策略落在 019，受控聚合探索的运行契约（第四个领域 `controlled_sql_exploration`、Artifact 类型 `exploration_result`、四个终止原因，并把 `bi.query_diagnostics` 继续锁在应用/管理员身份之间）落在 020，approved 查询学习记忆的样例表、不可变事件表、`bi_approver` 审核角色与只含 approved 行的投影视图落在 021，隔离分析的第五个领域 `isolated_analysis` 与 Artifact 类型 `analysis_result` 的两份 CHECK 扩展落在 022（它不建任何事实表）。缺 019 时库存预警读不到快照视图，会按“来源未取证 / 暂不可用”降级，不会把读不到演成“0 件库存”。缺 018 时上架复核读不到快照视图，会按“来源未取证 / 暂不可用”降级，不会把读不到演成“都没上架”。缺 020 时探索结果写不进运行表（约束会拒），在持久化那一步失败为 `persistence_failed`，不会发出一份没有证据的结果。缺 021 时审核 API 的投影视图不存在，approved 查询学习记忆保持关闭（审核路由按未启用返回 404），不影响其它功能。缺 022 时隔离分析的 `analysis_result` 写不进 Artifact 表（CHECK 拒绝），图在持久化那一步失败为 `persistence_failed`，不会发出一份没有证据的分析结果；门禁关闭时 022 是否已应用都不影响聊天。005 / 007 / 008 / 009 都是代码硬依赖：少了它们，`reporting.v_product_daily` 没有名称与规格列、`v_coverage` 没有质量列、`v_source_batches` 不存在，或运行层无法写入血缘与请求身份，会直接报列/表不存在；缺 017 则商品运营图与商品解析在 `bi_app` 身份下直接读不到视图。

```powershell
psql -d bi_agent -f backend/sql/001_init.sql
psql -d bi_agent -f backend/sql/002_kuaimai_mapping_repair.sql
psql -d bi_agent -f backend/sql/003_kuaimai_metric_semantics.sql
psql -d bi_agent -f backend/sql/004_query_runtime.sql
psql -d bi_agent -f backend/sql/005_product_dimension.sql
psql -d bi_agent -f backend/sql/007_catalog_identity.sql
psql -d bi_agent -f backend/sql/008_data_readiness.sql
psql -d bi_agent -f backend/sql/009_query_provenance.sql
psql -d bi_agent -f backend/sql/014_multi_source_contract.sql
psql -d bi_agent -f backend/sql/015_provenance_basis.sql
psql -d bi_agent -f backend/sql/016_channel_catalog.sql
psql -d bi_agent -f backend/sql/017_commerce_views.sql
psql -d bi_agent -f backend/sql/018_listing_audit.sql
psql -d bi_agent -f backend/sql/019_inventory_snapshots.sql
psql -d bi_agent -f backend/sql/020_controlled_sql_exploration.sql
psql -d bi_agent -f backend/sql/021_approved_query_memory.sql
psql -d bi_agent -f backend/sql/022_isolated_analysis_artifacts.sql
```

单实例：全程持有数据库 advisory 锁，重复启动立即失败。

### 平台路由与来源注册表

当前开发基线为 main，旧分支的同步入口统一迁入 backend。淘系“入库完成”与“查询可用”分别验收：[多来源契约](superpowers/specs/2026-09-12-multi-source-metrics-design.md)及[任务5/11](superpowers/plans/2026-09-07-ecommerce-bi-agent.md)。任务 5.1–5.3 已交付：来源注册表与逐指标能力门禁（`backend/bi_agent/sources.py`）、覆盖按 (店铺×来源×实体) 取交集与付款时间口径三态认证、未匹配退款/未认证支付改为可量化披露（`coverage_time_basis_unverified` 等进入词表）。**basis 全链路与版本失效（5.4）仍未完成**；pdd 不得因档案或单据存在被宣称支付可用（2026-09-12 已决定不接入拼多多支付）。

- 订单源由 `sources.py` 注册表按 `bi.shops.platform` 解析：`tb`/`tm`/`pdd` 用 `erp.trade.outstock.simple.query`（销售出库·非敏感字段），`fxg`/`jd`/`kuaishou`/`wxsph`/`wsxc` 用 `erp.trade.list.query`。**未登记平台（1688、淘工厂等）没有默认回退**：`_shop_order_source` 直接报错退出，逐店循环里该店被跳过并打印原因，绝不把“拿不到”写成“没有”。`sync_state` 主键含 source，两通道水位/覆盖互不干扰。
- 新增一个来源必须先拿齐方法名、权限、时间语义与金额对账证据，再写进注册表；数据库里不维护第二套可自由配置的来源表。
- 先跑 `shops` 刷店铺档案再跑订单命令，缺档案的店会直接报错（防假覆盖）。
- 淘系口径为 **ERP 出库非敏感字段**，非平台账单口径；收件人/买家昵称/手机号等 PII 字段在规范化入口即丢弃并有守护用例，不得扩列。详见 `docs/superpowers/research/2026-09-12-taoxi-onboarding.md`。
- 淘系（tb/tm）支付窗口回填/replay/reconcile 自 2026-09-25（S2 取证）起改走**宽出库窗 + 本地 payTime 过滤**：按 `timeType=consign_time` 请求 `[start-15d, end+15d)`，**逐日切片（≤1 天/片，官方建议跨度）且每片先在线（cursor/hasNext）后归档（total 证据）双通道**，任何一片分页不完整即整窗失败；本地只保留 `payTime ∈ [start, end)` 的行后入库，覆盖区间仍是请求窗口本身（宽窗只是取数范围，永远不写成覆盖）。代价是查询放大：每个 1 天支付窗口的宽窗取数为 31 个日切片 × 2 通道 = 每支付日至少 62 次 stream start（切片超 200 行另有分页请求；原 pay_time 路径每支付日仅 2 次 stream start），调度预算由运维控制；未发货的已付单在该路径不可见（出库人群上限，来源口径已披露），由 upd_time 增量与后续发货后的重放补齐。其余平台（fxg/jd/kuaishou/wxsph/wsxc/pdd）支付窗口仍按 API `pay_time` 过滤。

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
psql -d bi_agent -f sql/017_commerce_views.sql
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
# 历史深度自 2026-09-28 起为 180 天（分段扩历史的做法见下一节）
uv run --env-file ../.env.sync python -m bi_agent.sync backfill --days 180
uv run --env-file ../.env.sync python -m bi_agent.sync incremental
uv run --env-file ../.env.sync python -m bi_agent.sync reconcile --days 7
# 对账凭证落下后才能开通指标能力：先只报告，确认后再接着跑 --apply
uv run --env-file ../.env.sync python -m bi_agent.sync capabilities
uv run --env-file ../.env.sync python -m bi_agent.sync capabilities --apply
```

同步在完整分页、校验和事务提交后才推进水位。分页、权限或上游错误不会成为零业务数据；每日重核最近七天以处理晚到退款。

重试与窗口容错（2026-09-28 起）：

- 单次调用：限流类错误（HTTP 429 与业务信封「频繁/限流」）最多尝试 5 次，服务端 `Retry-After` 在 30 秒上限内完整尊重；其余错误（超时/5xx）仍为 3 次指数退避；authentication/permission 不重试。
- 窗口级：回填/重核的每个日窗口对瞬态错误（rate_limit/timeout/upstream）冷却 30 秒后重试 2 次，最终失败记入该店输出 JSON 的 `failed_windows` 清单后**继续后续窗口**，不再中断整店；失败窗口覆盖不推进（fail-closed 语义不变），由下次运行或 `reconcile --missing-only` 补上。增量（incremental）保持失败即停：水位不前移，下一小时从原水位自愈。
- `reconcile --missing-only`：逐实体（orders/aftersales_occurrence/aftersales_cohort）只拉 `bi.sync_state.covered` 在请求区间内的缺口窗口，已覆盖窗口零请求（淘系全量重核每店每天 434+ 次流式请求，missing-only 稳态接近零）。适合安排每小时/更高频的自愈任务；没有任何状态行时等价于全量重核，可直接用于新店首拉。

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

另建每日低峰的 `reconcile --days 7` 任务，以及可选的每小时 `reconcile --days 2 --missing-only` 自愈任务（缺口零成本重拉，持续保持近期窗口无洞）。运行账户只读取同步配置；命令参数不含密码。

长时间回填与小时任务的锁竞争属预期：同步 CLI 全程持有 advisory 锁，长任务运行期间小时任务以「已有同步任务运行」快速失败，不做排队；回填结束后下一次整点自动恢复。需要缩短连续持锁时间时，用 `replay` 按月分段补历史（见下）。

### 历史深度扩展（180 天，2026-09-28）

覆盖目标区间 `[今天-180天, 今天)`。已覆盖近段的店不要整体重跑 `backfill --days 180`，用 `replay` 只补缺口段（覆盖按窗口累加，重放已覆盖区间是纯浪费）：

```powershell
Set-Location backend
# 已接入店：先追平停摆缺口（backfill 会重建 data_as_of 并补水位扫描段）
uv run --env-file ../.env.sync python -m bi_agent.sync backfill --days <停摆天数>
# 再按月分段把历史从已覆盖起点扩到 180 天（orders → 售后 → cohort 三实体都要）
uv run --env-file ../.env.sync python -m bi_agent.sync replay --entity orders --start 2026-04-01 --end 2026-05-01
uv run --env-file ../.env.sync python -m bi_agent.sync replay --entity aftersales_occurrence --start 2026-04-01 --end 2026-05-01
uv run --env-file ../.env.sync python -m bi_agent.sync replay --entity aftersales_cohort --start 2026-04-01 --end 2026-05-01
# 逐月推进到既有覆盖起点；新接入店直接：
uv run --env-file ../.env.sync python -m bi_agent.sync backfill --days 180
```

淘系（tb/tm）历史回填的请求量提醒：宽出库窗路径每个支付日 ≥62 次流式请求，180 天约 1.1 万次/店，单店连续持锁可达数小时——放在低峰段、按店分段执行（临时用环境变量覆盖 `BI_SHOP_IDS` 逐店跑，不改 `.env.sync`）。回填输出里的 `failed_windows` 清单在自愈任务 `--days` 跨度内的条目由 `reconcile --missing-only` 自动补；更早的失败窗口需要 `replay` 或加大 `--days` 重跑。

## 平台对比的双口径（S1，P1 修正后）

「平台对比」自 S1 起按两个口径发布，读数前先认口径徽标：

- **ERP出库口径（默认排名与图表）**：单据数、出库件数、出库金额（行级分摊金额）取自 ERP
  出库单据事实，**按发货时间 `consignTime` 归属**（迁移 025 的 `bi.orders.consigned_at`，
  与付款时间 `paid_at`=`payTime` 是两个独立字段，永不互替）；读取视图为
  `reporting.v_outstock_daily` / `reporting.v_outstock_document_daily`。**覆盖只认
  `bi.sync_state(entity='orders_consign')` 凭证**，即 `erp.trade.outstock.simple.query`
  请求 `timeType=consign_time` 建立的扫描；付款时间扫描的覆盖不可借用。出库路径与支付
  来源绑定无关：`resolve_consign_source` 把全部已登记非 PDD 平台都绑定到这条专项通道。
  因此**扫描前（无 `orders_consign` 凭证）所有平台 fail closed**；**扫描完成后，任一拥有
  完整 `orders_consign` 证据的已登记非 PDD 平台都可回答**。存在缺发货时间的单据时整范围
  fail closed（`coverage_incomplete`）。拼多多不接入，永远只以 `excluded_scope`
  缺口出现。
- **平台支付口径**：只聚合已认证支付平台（当前仅抖音 fxg）。淘宝/天猫/京东/快手等未认证
  平台的支付格显示 `data_missing` + 原因 `coverage_time_basis_unverified`，不补零、不缺席；
  逐店账单对照（认证）完成后平台才会进入该口径。

机器可查：每个对比 Artifact 的 `filters.caliber`、每个排名块与每张图表的 `caliber` 字段。
字段/来源/覆盖契约见 `docs/metrics.md` §4.3.1。

**出库口径开通命令**（运营在采集机执行；未执行前出库口径对所有平台 NOT MET 为预期状态）：

```sh
cd backend
uv run --env-file ../.env.test python -m bi_agent.sync consign-scan --days 90
# 有界回放指定窗口（覆盖凭证只按完整窗口推进）：
uv run --env-file ../.env.test python -m bi_agent.sync consign-scan   --start 2026-09-01 --end 2026-10-01 --mode replay
```

命令语义：逐个 `BI_SHOP_IDS` 店铺、按 ≤1 天窗口调用 `erp.trade.outstock.simple.query`
（请求 `timeType=consign_time`，分页完整性复用订单通道同一套机器）；单据规范化入库时
**不重建/不新建任何支付事实**（`order_payments` 不经此路径）。只有整个窗口扫描完整且全部
单据带发货时间、发货时间都落在窗口内，才把该窗口计入 `bi.sync_state`
（source=`erp.trade.outstock.simple.query`，entity=`orders_consign`）；任何一页不完整、
单据缺 `consignTime` 或发货时间越窗 → 整窗回滚，仅留 `last_error_code=
consign_scan_incomplete` 等失败凭证（fail closed，不补零、不写部分覆盖）。拼多多店铺
自动跳过（永久排除）。首次运行后需按既有对账流程核验来源质量
（`reconcile_source_quality`），出库口径排名才会随认证状态亮起。

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
- 付款时间口径未认证（Task 5.2c）分三档：`disproved`（销售出库接口实测按自身时间裁剪）
  不得给支付窗口类结果，返回 `coverage_time_basis_unverified`；`local_filter`（S2 取证后
  淘系同步改宽出库窗+本地 payTime 过滤，窗口归属已按构造精确）在 S3 关闭单口径决策落地
  后降为**可答+披露**：前提是完整且新鲜的 local_filter 覆盖与全部既有门禁成立，支付口径
  在查询侧剔除结构性证明的关闭后退款单支付（026 视图；关闭未退款单不剔除，同批退款率
  分母不减），剔除量由 `closed_refunded_excluded` 诊断/披露逐项给出；上期对比
  （compare=previous_period）另取上期窗口的剔除事实同步扣减，上期被剔量单独披露，
  环比差额不得把口径漂移当增长；没有真实覆盖时照旧
  在覆盖门禁处 fail closed，「完整支付窗口」主张仍不开放；`unmeasured`（同通道但未逐店
  对照）可出数但必须披露为可观测样本。`erp_documents` 是单据计数、不是支付窗口主张，
  各档都只披露不拒答。等回填不会改变 `disproved` 的结论，要的是与后台账单/业务日期的对照登记。

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

### 渠道在售价与库存的证据采集（Task 9 / 10 的真实开关）

上架价复核与库存预警的**来源门禁不在数据库里**，而在代码内的有限注册表：
`bi_agent/listing_audit/rules.py` 的 `register_listing_source` 与
`bi_agent/inventory/rules.py` 的 `register_inventory_source`。默认一条都没有，
所以真实部署里两个 Tool 只会给出 `unsupported`（而不是“都没上架”或“0 件库存”）。

开一格的完整前置（缺一就保持关闭）：

1. 来源形态：平台授权 API，或带抓取时点与完整性声明的官方导出（ERP 档案建议价 `priceOutput` 不能替代实际在售价）；
2. 字段与单位：实际 listing / SKU 售价、上架状态、抓取时间、快照完成标志；库存还要可用 / 锁定 / 在途逐个核；
3. 时效：一个已批准的 freshness policy（超过它就判 `stale`，不判“正确 / 安全”）；
4. 分页完整性：全量枚举凭据才能判 `not_listed`，扫描声明不完整只能给 `unknown`；
5. 对账：与渠道后台逐元（价格）/ 逐件（库存）比对结果与差异记录。

取得证据后由服务端登记（不开放给模型入参、不读配置文件），再跑一次只读复核确认逐格状态与
期望/已评估计数能对上；同步更新 `docs/metrics.md` 的就绪清单与验收报告的逐平台表。
库存那一侧还需一条**服务端**的库存池授权来源（与店铺授权相互独立）：现在主 Agent 递进去的
池授权集永远是空集，所以聊天路径只能给店铺可售预警，实物总量恒为未判定。

## 语义目录与 Schema 检索（默认关闭）

`backend/bi_agent/semantic_catalog/` 是 Task 11 后置子项目 A：版本化目录 + 确定性 Top 5 检索。它只为**后续**受控 SQL 探索提供候选结构，本身不执行 SQL、不读业务事实行、不新增 Agent Tool 或 HTTP 路由、不授予任何数据能力；逐店逐指标的能力门禁（`backend/bi_agent/sources.py`）与口径（basis）契约（`docs/metrics.md` 第 3 节与第 6 节）仍是唯一权威，权限判定也仍由服务端上下文给出，模型递不进身份。拼多多继续按 2026-09-12 决定不接入，目录不为它开任何口。

### 门禁与取值

| `SEMANTIC_CATALOG_ENABLED` | 行为 |
| --- | --- |
| 缺省 / 空串 / 纯空白 / `false` | 关。不建预检连接、不发那条查询、不校验目录；“显式 false”与“变量缺席”两种写法的启动与聊天行为已逐项比对一致（见[验收记录](superpowers/research/2026-09-14-semantic-catalog-acceptance.md) §6），且本包在 Agent / 查询层没有任何调用路径 |
| `true` | 开。`create_runtime_app()` 先建**一条** `BI_APP_DSN` 的 autocommit 连接，跑完预检再建模型与 FastAPI app |
| 其它写法（`1` / `0` / `yes` / `True` / `on` / 带分号等） | 启动即失败：`SEMANTIC_CATALOG_ENABLED 只能是 true 或 false` |

严格性在 loader（`config.load_app_settings`）里；直接构造 `AppSettings` 只是 `semantic_catalog_enabled` 默认 `False`，loader 不是它的校验入口，所以部署侧只该用环境变量这一条路径。`.env.example` 带的是 `SEMANTIC_CATALOG_ENABLED=false`。

### 启用前置（本机与目标环境同一顺序）

1. **迁移必须齐**：本机 `*_test` 与目标库都按完整顺序 `001 → 002 → 003 → 004 → 005 → 007 → 008 → 009 → 014 → 015 → 016 → 017 → 018 → 019`（编号 006 已作废；受控聚合探索另需 020，见下一节）。目录声明对应的最新定义迁移：

| 登记视图 | 列定义来源（最新） |
| --- | --- |
| `reporting.v_shops`、`v_shop_daily`、`v_payments`、`v_refunds` | `001_init.sql` |
| `reporting.v_product_daily` | `007_catalog_identity.sql`（003 的窄版本已被取代） |
| `reporting.v_coverage` | `008_data_readiness.sql` |
| `reporting.v_product_cost_daily`、`v_erp_document_daily` | `017_commerce_views.sql` |
| `reporting.v_listing_snapshot_items` | `018_listing_audit.sql` |
| `reporting.v_physical_stock_items`、`v_channel_stock_items` | `019_inventory_snapshots.sql` |

   预检是“缺哪报哪”的：库停在旧版时启动直接失败并报缺的 view / field ref，不会带着一份骗人的目录起来。本计划**不新增迁移**，也不改任何已应用 SQL。
2. **授权核对**（只读，管理员执行）：11 张登记视图对 `bi_app` 与 `bi_reader` 只有 SELECT，且 `bi_app` 对 `bi.*` 事实表仍无 SELECT。

```powershell
psql -d bi_agent -c "SELECT table_name, grantee, privilege_type " \
                   "FROM information_schema.table_privileges " \
                   "WHERE table_schema='reporting' AND grantee IN ('bi_app','bi_reader') " \
                   "ORDER BY table_name, grantee;"
psql -d bi_agent -c "SELECT table_name, privilege_type " \
                   "FROM information_schema.table_privileges " \
                   "WHERE table_schema='bi' AND grantee='bi_app' ORDER BY table_name;"
```

第二条只应看到聊天与运行追踪表（`app_chats`、`app_messages`、`query_runs`、`query_run_events`、`query_artifacts`、`query_diagnostics`、`query_provenance`、`expected_listing_rosters`、`price_audit_expectations`）；出现 `orders` / `order_items` / `shops` / `sync_state` 等事实表就是权限漂移，先撤销再谈启用。本机 `bi_agent_test` 实测：11 张登记视图均为 `bi_app=SELECT, bi_reader=SELECT`，`bi_app` 在 `bi.*` 上只有上述聊天/运行表。
3. **用只读角色自证预检不扩权**（本机测试库）：

```powershell
Set-Location backend
uv run --locked --env-file ../.env.test python -m unittest tests.test_semantic_catalog.SemanticSchemaCheckDatabaseTests -v
uv run --locked --env-file ../.env.test python -m unittest tests.test_semantic_catalog -v
```

前者在 `bi_reader` 下跑完整 `validate_catalog_schema(conn, CATALOG)` 并要求 `SELECT 1 FROM bi.orders LIMIT 1` 抛 `InsufficientPrivilege`；后者含目录闭包、检索与 30 题 gold set（107 项，需 `*_test` 的两项在有 DSN 时跑）。

### 启用步骤

本机：`.env.app` 写 `SEMANTIC_CATALOG_ENABLED=true` → 按原命令启动 `uv run --env-file ../.env.app uvicorn bi_agent.api:create_runtime_app --factory --host 127.0.0.1 --port 8001` → 确认预检通过（不打印任何 SQL 原文，只有成功启动或一个稳定错码）→ 跑 26 题离线验收与一次聊天冒烟。

目标环境：先逐条完成上面 1–3 的核对（DDL 与 GRANT 只由管理员执行），再按现有单实例发布流程改配置并重启一个实例；预检失败即发布失败，不得绕过、不得重试到启动成功。本轮**未在任何非本机环境启用过该门禁**（见[验收记录](superpowers/research/2026-09-14-semantic-catalog-acceptance.md)的未执行清单）。

打开后仍不变的：`/api` 路由集合（7 条）与 Agent Tool 清单（6 个）不变；语义目录没有对外入口、没有后台任务、不写任何表。

### 启动失败与回退（安全错误契约）

| 观察到的错误文本 | 含义 | 处置 |
| --- | --- | --- |
| `semantic_schema_mismatch:<第一个排序后的稳定 ref>` | 目录声明与库里 `reporting` 真实结构不一致：整张视图缺失报视图 ref（如 `semantic_schema_mismatch:view-shop-daily`），缺列或类型族不符报字段 ref（如 `semantic_schema_mismatch:field-shop-daily-paid-amount`）；完整清单在异常的 `refs` 属性上，同样只含 ref | 库落后 → 补跑迁移；登记列真的被改/删 → 由实现方修正目录声明并升 `SEMANTIC_CATALOG_VERSION`。**不**手工改库去迁就声明，也**不**删登记让启动通过 |
| `semantic_schema_mismatch:catalog-entry` | 传进来的目录条目 ref 形状不合法（只有绕过契约层构造目录才会出现）；原文绝不回显 | 当代码缺陷处理：排查构造目录的代码，而不是改环境变量 |
| `semantic_catalog_*`（例：`semantic_catalog_duplicate_ref:view-shops`） | 目录自身不闭包（悬空 ref、字段错归属、可放大基数、未写防放大理由等），导入时就失败 | 回到登记层修，修完重跑 `tests.test_semantic_catalog` |
| `SEMANTIC_CATALOG_ENABLED 只能是 true 或 false` | 配置写法不被接受 | 改成 `true` / `false`，或删除该变量（缺省即关） |
| `semantic_catalog_version_mismatch` | 递进来的 `current_versions.semantic_catalog_version` 不是当前目录版本 | 不是启动失败而是**检索时**拒绝（本轮无生产调用方）：旧目录快照只用于解释历史 Artifact，不能用来为新问题选候选 |

这些文本之外不会有任何其他细节：消息里永不出现 SQL 标识符、列名、数据库返回原文、DSN、密码或请求体（`AppSettings.app_dsn` 是 `SecretStr`）。失败时进程不监听端口，也不会降级为“目录为空 / 这次先不检索”。

回退 = 把 `SEMANTIC_CATALOG_ENABLED` 改回 `false`（或删掉该变量）后重启：不建预检连接、不发那条查询、不校验目录。已实测“显式 false”与“变量缺席”逐项相同（工具名与 schema 摘要、发给查询层的规范化请求、运行状态、事件数、Artifact 数量与类型、确定性结果载荷、聊天正文）；回退不需要跑迁移、不需要清库，因为本功能不写任何表。

不得做的“恢复”动作：不要为了让启动成功而 `CREATE VIEW` / 改列名去凑声明；不要把门禁关掉后就宣布问题已解决（先分清是“库落后”还是“目录写错”）；不要拿离线 26/26 或本地预检通过当作可发布给运营用户的依据——Task 11 统一发布门禁 4–7 项仍是 open。

## 受控聚合探索（默认关闭）

`backend/bi_agent/exploration/` 是 Task 11 后置子项目 B：把固定业务 Tool 无法表达、但来源能力与覆盖已满足的只读聚合问题，编译成**一条**受控语句执行。它叫受控聚合探索，不是通用 Text2SQL：模型与前端都没有提交 SQL、标识符、排序、limit 或授权条件的入口，SQL 标识符只由服务端从语义目录的稳定 ref 解出，真值全部走参数。拼多多继续按 2026-09-12 决定不接入，本功能不为它开任何口。

### 两个门禁与依赖

| 变量 | 默认 | 含义 |
| --- | --- | --- |
| `SEMANTIC_CATALOG_ENABLED` | `false` | 语义目录与启动预检（上一节）。受控探索的标识符解析依赖它 |
| `CONTROLLED_SQL_ENABLED` | `false` | 受控聚合探索：`explore_business_data` 是否出现在 Tool 列表、探索图是否可被调用 |

- `CONTROLLED_SQL_ENABLED=true` 而 `SEMANTIC_CATALOG_ENABLED=false` → 启动即失败 `CONTROLLED_SQL_REQUIRES_SEMANTIC_CATALOG`：目录关着就没有 SQL 标识符的解析路径，不许“退化成不带目录的 SQL”。
- 两个变量都只接受 `true` / `false`（`1` / `0` / `yes` / `TRUE` / `on` 一律当场拒绝）；`.env.example` 给的是两个 `false`，并由用例钉住“示例文件带的是关闭值”。
- 关闭时（默认）：根本不进探索门禁，发给模型的 Tool 列表 JSON 逐字等于关闭基线（6 个 Tool，无 `explore_business_data`），`/api` 路由集合与用户可见面不变。打开时也只在“固定 Tool 表达不了、不需要澄清、无未注册概念、服务端作用域就绪”四项都过的那一轮才多一项；固定 Tool 本轮即便会返回 `unavailable` / `missing_data` 也不因此开放探索。
- 一次探索 Tool 调用 = 一个 `DomainContext` = 一次图执行：不逐店循环，提问由服务端注入，回到模型的那一句里没有 SQL 也没有真店号。

### 020 的部署、幂等与回退

`020_controlled_sql_exploration.sql` 必须在 `004 → 009 → 014 → 015 → 016 → 017 → 018 → 019` 之后执行（它重建 009/014 声明的三份 CHECK，读不到既有定义就 `constraint_missing:<名>` 早失败），只做三件事：

1. 三份白名单**只重建不收缩**：逐字重声明 009/014 已登记的每一个取值，并保留库里当前已存在的额外取值，再参加本计划的 `controlled_sql_exploration` 领域、`exploration_result` 类型与 `fixed_tool_available` / `schema_ambiguous` / `sql_policy_rejected` / `query_cost_exceeded` 四个终止原因。因此它在独立泳道的 `022_isolated_analysis_artifacts.sql` 之前或之后执行都成立，谁后跑谁负责合并，不靠顺序运气。
2. `bi.query_diagnostics` 保持私有：不建任何 reporting 视图，`REVOKE ALL … FROM PUBLIC / bi_reader`，只给 `bi_app` `SELECT, INSERT, UPDATE`。
3. 幂等：全部是 `DROP … IF EXISTS` + 重建，可重复执行。本机 `bi_agent_test` 已应用该迁移（探索图能以该领域写真库为证），测试里还在管理员外层事务内重放两次并逐字比对约束定义（`ExplorationMigrationTests`，跑完按 Rollback 协议退出）。

```powershell
Set-Location backend
psql -d bi_agent -v ON_ERROR_STOP=1 -f sql/020_controlled_sql_exploration.sql
```

回退 = 把 `CONTROLLED_SQL_ENABLED` 改回 `false`（或删掉该变量）后重启：不新增 Tool、不进门禁、不发任何语句。回退**不需要**回滚 020：白名单只增不减，已有的探索运行记录与诊断仍按当版可读；也不要为了让启动成功去改约束、删运行记录或把门禁“先关掉当已解决”。

### 诊断：一次受控聚合探索为什么没出数

| 观察到的稳定码 | 发生在哪一步 | 含义与处置 |
| --- | --- | --- |
| `fixed_tool_available` | `authorize_scope`（编译之前） | 固定 Tool 已能表达这个问题：改用它。“换个工具再试一次”不是绕过固定口径的路 |
| `schema_ambiguous` | `select_schema` | 检索无法唯一定位语义 ref：该澄清，不是猜一个就发 SQL |
| 编译层的 `exploration_*`（运行层记 `contract_violation`） | `compile_query` | 本轮选择与目录不自洽、缺窗口、跳事实粒度、聚合未授权（如上架价默认的 `avg`）、未登记 JOIN 边、授权集合为空等：属契约/选择问题，不是数据库问题 |
| `sql_policy_rejected` | `validate_ast` | AST 白名单在碰库之前拒掉这条语句（多语句、CTE、子查询、UNION、未登记对象、函数逃逸、缺授权或时间谓词等）。语句原文只进诊断表，错误文本只剩一个码 |
| `query_cost_exceeded` | `estimate_cost` | 真 `EXPLAIN` 的预计行数 > 50000 或总成本 > 100000：缩小窗口或店铺范围，不为了过门抬阈值 |
| `query_timeout` | 执行 / 投影 | 5 秒 `statement_timeout` 掐断了这条语句 |
| `deadline_exceeded` | 任一进库步骤之前 | 整轮 30 秒预算不足（进库前要求容得下两道 5 秒门 + 两道 0.1 秒 IO 余量 + 1 秒收尾）：宁可当场判未完成，也不留半截查询 |
| `result_too_large` | 执行 / 投影 | 命中 500 行上限（取 `limit + 1`，多一行就是截断证据）或安全投影后 > 262144 字节：整条拒，不返回被截断的偏低汇总 |
| `persistence_failed` | `persist_artifact` | 诊断或 Artifact 写不进：清空待发布结果。先恢复运行记录的写入能力，再由用户重发原问题 |
| `forbidden` | `authorize_scope` | 授权集合为空、拿不到 opaque 引用，或本轮选中的视图只能按库存池授权：探索链只开店铺一条授权通道，池 id 绝不 substitute 进来 |

排查时只读 `bi.query_diagnostics`（`bi_app` 与管理员身份读得到，`bi_reader` 无授权）：SQL 原文与参数只在这里，通过 `run_id` 引用；事件、模型消息与本页示例都不含 SQL、DSN 或真实店铺标识。历史 SQL 只可查看，目录版本或模板版本一变就必须重新生成并重新验证（`exploration_catalog_version_mismatch`），旧计划不得复用。

### 启用前置（本机与目标环境同一顺序）

1. 迁移齐：按上面的完整顺序含 `020`；预检失败即发布失败，不得绕过。
2. 权限核对（只读，管理员执行）：用上一节那两条 `information_schema.table_privileges` 查询确认 11 张登记视图对 `bi_app` / `bi_reader` 仍只有 `SELECT`，且 `bi_app` 对 `bi.*` 事实表仍无 `SELECT`——受控探索不扩权：它读的还是目录里那 11 张视图，底表权限不因本功能变化。
3. 回归（本机 `*_test`）：

```powershell
Set-Location backend
uv run --locked --env-file ../.env.test python -m unittest tests.test_exploration tests.test_runtime tests.test_runtime_db -v
uv run --locked --env-file ../.env.test python -m tests.acceptance --offline
```

4. 本轮**未在任何环境启用过该门禁**：真实 provider smoke 与 26 题 live、目标环境迁移与授权实测、生产启用、部署与一周试用全部记 `未执行`，逐条见 [受控 SQL 探索本地验收记录](superpowers/research/2026-09-14-controlled-sql-acceptance.md) §9。放行矩阵里的库存行只在回滚事务内 seed，不意味着库存或上架价来源已就绪；真实部署里 `listing_audit` / `inventory` 注册表仍为空，两个固定 Tool 依旧只报 `unsupported`。

## approved 查询学习记忆（默认关闭）

`backend/bi_agent/query_memory/` 是 Task 11 后置子项目 C：把**人工审核批准、已脱敏且版本兼容**的规范化查询样例，在门禁开启时作为至多 3 条 few-shot 提供给路由与参数规范化。模型与成功运行都不能自动写入记忆：唯一的写入口是审核 API → repository，由独立审核 DSN（`bi_approver` 身份）执行。样例不保存原始聊天、真实名称/主键、SQL 原文、结果行、DSN 或密钥；目标价、阈值、预算、日期区间与店铺选择一律是槽位。

### 门禁与启用前置

| 变量 | 默认 | 含义 |
| --- | --- | --- |
| `APPROVED_QUERY_MEMORY_ENABLED` | `false` | 记忆检索与审核 API 的总开关；关闭时审核路由按未启用返回 404，聊天零记忆读取 |
| `APP_APPROVER_SUBJECTS` | 空 | 授权审核者的 subject 集合；开启时必须非空，非审核者一律 403 |
| `BI_APPROVER_DSN` | 空 | 独立审核连接串（`bi_approver` 会话成员）；开启时必须存在且**不等于** `BI_APP_DSN` |

- 启用 = 三个变量一起配置后重启：`APPROVED_QUERY_MEMORY_ENABLED=true` + 非空 `APP_APPROVER_SUBJECTS` + 合格 `BI_APPROVER_DSN`；缺任一项启动即失败，绝不带病降级。取值只接受 `true` / `false`（其它写法当场拒绝）。
- 021 必须已按完整顺序应用：`bi.approved_query_examples`、不可变的 `bi.approved_query_events`、`bi_approver`（NOLOGIN 组角色，经独立 DSN 的会话成员使用；只有样例表读写与事件追加权，事件不可改写，事实表零授权）与 `reporting.v_approved_query_examples`（只含 approved 行）。`bi_app` 对两张底表零授权，只读投影视图。
- 前置回归（本机 `*_test`，串行）：

```powershell
Set-Location backend
uv run --locked --env-file ../.env.test python -m unittest tests.test_query_memory tests.test_db tests.test_runtime_db tests.test_api -v
uv run --locked --env-file ../.env.test python -m tests.acceptance --offline
```

### 审核流程（全部人工，全部显式理由）

1. **从候选列表建草稿**：审核面板的候选列表只显示最近 100 个 `succeeded`、血缘完整且尚无任何记忆行的运行（含脱敏后的规范化请求、domain、工具与冻结版本）。审核者选中一条来源、把问题改写成槽位化模板（如“比较 {shop_scope} 在 {date_window} 的成本”）并为每个槽位选类型后提交；草稿的 owner 固定取来源运行自己的 subject，与服务端复核，不由请求体指定。
2. **批准 / 撤销 / 替换都要显式理由**：三个动作的请求体都必须带非空审核理由（至少 3 个字符），与操作者、时间、revision、replacement ref 一起写进不可变事件；理由空或过短一律 422。生命周期只有 `draft → approved → superseded/revoked`、`draft → revoked`；终态不可逆，重新启用必须新建草稿。
3. **建议双人复核**：系统强制的是“只有人工能批准”，不强制两人；运维上建议批准者与建草稿者不是同一人，替换（supersede）前由第二人核对 replacement 同领域且已批准。
4. **撤销立即生效**：撤销后的**下一条**检索就看不到该样例（投影视图只含 approved 行），没有缓存宽限期。
5. **版本升级后旧例进重审，不自动迁移**：schema / 语义目录 / 数据目录 / 指标 / 策略 / 来源注册表 / 图七个版本维度任一变化，旧例即零召回；要让新版本继续可用，必须用新版运行重新走“候选 → 草稿 → 批准”，并把旧例 supersede 到新例上。系统不做任何自动升级或自动迁移。
- 面板可见性：功能关闭时后端返回 404，审核入口整体隐藏；非审核者 403 显示“无审核权限”；审核面板是独立入口，不是聊天界面的延伸。

### 回退

回退 = 把 `APPROVED_QUERY_MEMORY_ENABLED` 改回 `false`（或删掉该变量）后重启：聊天回到无记忆基线，回合的消息、工具与 SQL 逐字等于关闭基线（测试钉住），审核面板随 404 消失。回退**不需要**回滚 021：样例、事件与视图留在库里仍可审计，已批准样例在下一次开启时按当时的版本精确匹配重新生效。不要为了让启动成功去删表、改授权或把门禁“先关掉当已解决”。

## 隔离分析（默认关闭）

`backend/bi_agent/analysis/` 是 Task 11 后置子项目 D：对当前用户仍有权读取的不可变数据集 Artifact（`metric_result` / `comparison_table` / `trend_series`）做确定性分析，数值全部由 Decimal 纯函数产生，可选模型只用字面空工具列表总结已验证 finding，失败时仍发布确定性结果。分析入口是一个只读 Tool（`analyze_artifact`），参数只有 `artifact_ref` 与 `analysis_kinds`；没有 SQL、店铺 ID、数据行或工具列表通道，同轮最多调用一次。

### 门禁与启用前置

| 变量 | 默认 | 含义 |
| --- | --- | --- |
| `ISOLATED_ANALYSIS_ENABLED` | `false` | 隔离分析总开关；关闭时不注册 Tool、不读 Artifact、聊天与 Task 11 基线逐字相同 |

- 取值只接受 `true` / `false`（其它写法启动即拒）；启用 = 置 `true` 后重启，无需其它变量。
- 022 必须已应用（它只把 `isolated_analysis` 领域与 `analysis_result` 类型加进两份 CHECK，独立泳道，与 020/021 谁先谁后都幂等）；未应用时门禁开启的分析会在持久化那一步失败为 `persistence_failed`，不会发出没有证据的结果。
- 每次分析前服务端都会重新检查：来源 run 的 `subject_id` 归属、当前授权实体集合、Artifact 类型（只认三种数据集）、schema/metric/policy/source/graph 版本、覆盖状态、大小（≤500 行 / 20 列 / 256 KiB 安全投影）与 fingerprint。任何一项不过都在**模型调用之前**拒绝；loader 内部的细分原因（不存在/类型/版本/时间/覆盖/大小/未授权维度等）在落库前统一翻译，**不会以原词持久化**。运行记录里持久化的稳定词表只有 `error_code`（`invalid_parameters` / `unavailable` / `deadline_exceeded` / `artifact_persistence_failed`）与 `termination_reason`（`invalid_parameters`、`upstream_unavailable`、`source_quality_failed`、`contract_violation`、`coverage_incomplete`、`result_too_large`、`forbidden`、`deadline_exceeded`、`persistence_failed`）：资格拒绝（不存在/类型/归属）走 `invalid_parameters`，其中**跨属主与不存在刻意不可区分**（不告诉他人工件存在）；来源质量问题走 `unavailable`，按 `source_quality_failed` / `contract_violation` / `coverage_incomplete` / `result_too_large` / `forbidden` 细分终止原因；源库/读取故障走 `unavailable` + `upstream_unavailable`。不自动重查数据库，也不把异常原文写进公开载荷或运行记录。
- 前置回归（本机 `*_test`，串行）：

```powershell
Set-Location backend
uv run --locked --env-file ../.env.test python -m unittest tests.test_analysis tests.test_core tests.test_operator_workflows -v
uv run --locked --env-file ../.env.test python -m tests.acceptance --offline
```

### 版本推进与来源失权

- 分析载荷冻结 `analysis_version = isolated-analysis/2026-09-14.1` 与来源 fingerprint；来源数据集不可变，分析结果引用它而从不改写或重查它。
- 来源失权（撤销授权、能力降级、覆盖缺口）不影响已落库的分析结果（它是当时的确定性事实），但**下一次**分析会在模型调用前按当前授权重新拒绝；不存在“用旧授权继续分析”的路径。
- 计算或版本规则升级 = 新的 `ANALYSIS_VERSION` + 新 gold set，旧 Artifact 按原版本原样保留可审计；系统不做自动重算或迁移。

### 回退与诊断

- 回退 = 把 `ISOLATED_ANALYSIS_ENABLED` 改回 `false`（或删掉该变量）后重启：Tool 列表回到 Task 11 基线，聊天逐字不变（测试钉住）。回退**不需要**回滚 022：两份 CHECK 只多出两个合法取值，已有 `analysis_result` 行仍可读可审计。
- 模型叙述失败（超时、超 token、格式不合法、模型异常）不丢数：确定性 findings 照常发布，限制里记 `narrative_unavailable`；剩余 deadline 不足 2 秒整段跳过模型调用。持久化失败才是硬失败：状态 failed、零 Artifact、模型侧拿不到任何 findings。
- 前端对 `analysis_result` 载荷整体验形后才渲染：验不过整卡拒绝（“未通过展示侧校验”），不猜形状、不补数字；“查看来源数据”按钮只在同一条消息里找到唯一匹配的数据集 Artifact 时可用。

## 持续库存通知（默认关闭）

`backend/bi_agent/monitoring/` 是 Task 11 后置子项目 E：部署主机周期性调用一次性 CLI（`bi-inventory-monitor` 生成告警与 outbox 行，`bi-inventory-outbox` 幂等投递到应用内通知中心），复用 `InventoryWatchGraph` 与确定性问题状态机；模型与工具路径零参与。来源门禁**按层级判定**：只核验实物时渠道层是一等 `data_missing`/unknown——不补零、不替位、不触发、不解除、不发“无法判断”通知。

### 当前状态（先读这一段）

**默认关闭、未部署、未注册调度。** `INVENTORY_MONITOR_ENABLED=false` 是仓库与文档的默认值；本轮没有在任何机器注册计划任务，没有创建 systemd timer / Windows 任务，没有部署。启用前置是逐层库存来源验收（[验收记录](superpowers/research/2026-09-14-inventory-source-acceptance.md)，仍为 FAIL：`physical` 四维 NOT MET、`channel` 一等 `data_missing`）与 Task 11 统一发布门禁第 4–7 项（仍 open）——两者都未满足前，本节调度与回滚只是文档，不是操作记录。迁移 `023` 只在本机 `*_test` 库执行过；`bi_monitor` 是 NOLOGIN 最小权限身份，生产启用前必须另建独立登录与服务主体。

### 门禁与变量

| 变量 | 默认 | 含义 |
| --- | --- | --- |
| `INVENTORY_MONITOR_ENABLED` | `false` | 总开关；关闭时两条 CLI 打印一行安全文案、退出 0、不连任何数据库、不装载任何秘密 |
| `BI_MONITOR_DSN` | 空 | `bi_monitor` 身份的独立连接串；开启时必填，且不得等于 app/writer/approver 任一 DSN（`MONITOR_DSN_MUST_BE_SEPARATE`） |
| `MONITOR_SERVICE_SUBJECT` | 空 | 服务主体；开启时必填 |
| `MONITOR_POLICY_REFS` | 空 | 逗号分隔的已批准策略引用；开启时必填、去重、至多 100 条 |

策略本身存于 `bi.inventory_monitor_policies`（默认无行，`enabled=false` 起步）；批准一条策略时的显式检查：其作用域内期望项必须 ≤20——来源 Artifact 只是 top-20 展示投影，超过 20 的作用域本轮永远零告警。

### 部署主机调度（仅文档；本轮未注册）

两种受控配置，调度器只反复调用两条一次性命令，命令不接受任何参数；**不创建 Codex automation**，不在 API 进程建循环：

- **Windows Task Scheduler**（每 15 分钟，工作目录 `backend\`，非交互服务账号，单实例，30 秒超时，失败退出码告警）：

```powershell
# 注册模板（本轮未执行；仅当启用前置全部满足后由所有者决定使用）
# run-inventory-monitor.ps1 依次执行 monitor、outbox，并把任一非零退出码交给既有告警通道。
schtasks /Create /TN bi-inventory-monitor /SC MINUTE /MO 15 /RU <SERVICE_ACCOUNT> /RP * ^
  /TR "powershell.exe -NoProfile -NonInteractive -File D:\bi-agent\run-inventory-monitor.ps1"
# schtasks.exe 不能设置下列两项；注册后必须通过受控 XML 或 Task Scheduler 设置并复核：
#   MultipleInstancesPolicy = IgnoreNew（单实例，不重叠）
#   ExecutionTimeLimit = PT30S（30 秒超时）
# 任务还必须设置为“无论用户是否登录都运行”；不得使用 /IT（它只允许交互登录会话）。
# 服务账号密码只在受控注册提示中输入，不写入脚本、命令记录或仓库。
```

- **systemd timer**（同两条 ExecStart，`OnCalendar=*:0/15`，单实例由 `flock` 保证，`TimeoutStartSec=30s`，专用系统用户，失败进 `OnFailure` 告警单元，`WorkingDirectory` 指向 `backend/`）：

```ini
# bi-inventory-monitor.service（模板；本轮未安装）
[Service]
Type=oneshot
User=bi-monitor
WorkingDirectory=/opt/bi-agent/backend
ExecStart=/usr/bin/flock -n /run/bi-inventory-monitor.lock uv run --locked bi-inventory-monitor
ExecStartPost=/usr/bin/flock -n /run/bi-inventory-monitor.lock uv run --locked bi-inventory-outbox
TimeoutStartSec=30s
EnvironmentFile=/etc/bi-agent/monitor.env
# environment 文件由运维以受控权限维护；仓库内不放任何真实值

# bi-inventory-monitor.timer
[Timer]
OnCalendar=*:0/15
Persistent=false
[Install]
WantedBy=timers.target
```

每轮 30 秒总 deadline 在进程内强制（逐策略共享）；调度器超时只是同一约束的第二道闸。日志只有 policy ref、run ref、固定状态码与计数——不含 DSN、证据原文、数量或真实 ID。

### 零告警不等于安全（三类合法原因）

运维**不得**把“没告警”读成“库存安全”。至少三类本轮合法的零告警，只能靠固定诊断码与固定计数区分：

1. **请求层级未核验**：策略请求的层级缺已核验登记时，该层根本不会被发给 graph——无行、无去重键、不撑 expected_items，信号只有固定码 `monitor_level_unverified` 与计数 `inventory_monitor_level_unverified_total{level}`。只核验实物时**不存在**任何渠道侧信号：空店铺授权投影下 graph 结构上不读渠道快照，运维不会在物理轮看到 `inventory_channel_source_unverified`，也不该看到 `inventory_channel_snapshot_missing` 或未同步店铺一类店铺侧信号——载荷里出现即投影契约违约（按聚合码 fail closed，本轮去掉全部已核验层级并照常持久化）；两层都核验的运行里，`shop_not_synced` 伴随的 `inventory_snapshot_missing` 只去掉 `shop_sellable`，不要去查实物层。
2. **展示投影截断**：来源 Artifact 只是 ≤20 行的展示投影；`inventory.truncated=true` 或出现 `inventory_display_truncated` 的轮次，所有已核验层级都不算完整——零首发、零解除，保持原状态。这不是“没出现的那几百格都安全”。
3. **缺作用域引用的行被丢弃**：缺 `pool_ref`/`warehouse_ref`（实物）或 `shop_ref`（渠道）的格子不成为告警，只记 `monitor_row_scope_missing` 与 `inventory_monitor_rows_dropped_total{level,reason}`；不借另一层级引用、不拼默认作用域、不补零。

对应计数：`inventory_monitor_skipped_total{reason}`（reason 是闭集固定码）、`inventory_monitor_level_unverified_total{level}`、`inventory_monitor_rows_dropped_total{level,reason}`；口径见[指标口径](metrics.md) §4.9。

### 回滚

顺序固定：① 把 `INVENTORY_MONITOR_ENABLED` 置回 `false`（或移除该变量）并停止调度任务（`schtasks /End` + `/Change /DISABLE`，或 `systemctl disable --now bi-inventory-monitor.timer`）；② 确认两条 CLI 在 disabled 下打印单行安全文案退出 0；③ **历史表全部保留审计**——`bi.inventory_alert_instances` / `bi.inventory_alert_events` / `bi.notification_outbox` / `bi.in_app_notifications` 不清空、不删除 open 告警、不伪造 `resolved`。迁移 `023` 不回滚：表结构与权限收窄保留，已有行仍可审计；重新启用只是把开关改回 `true`。来源被撤销或策略失效后的存量 open/acknowledged 告警由 runner 置 `suppressed`（永不 resolved）。

### 本地回归

```powershell
Set-Location backend
uv run --locked --env-file ../.env.test python -m unittest tests.test_monitoring -v
uv run --locked --env-file ../.env.test python -m unittest tests.test_db tests.test_runtime_db tests.test_monitoring -v
```

共享库套件请逐条顺序跑（同一 `bi_agent_test`，外层回滚但 advisory 锁与共享表是全局态）。

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
# 四工作流与运营场景回归（计划 Task 7–11）
uv run --env-file ../.env.test python -m unittest tests.test_channel_mapping tests.test_commerce tests.test_comparison tests.test_listing_audit tests.test_inventory tests.test_operator_workflows -v
# 受控聚合探索（计划 Task 1–6：契约、固定 Tool 优先与编译器、AST 策略与攻击语料、
# 成本门与投影、运行图，以及 Task 6 的 14 行放行矩阵 / 4 行负例 / 46 行攻击汇总）
uv run --locked --env-file ../.env.test python -m unittest tests.test_exploration -v
# approved 查询学习记忆（后置子项目 C：契约与生命周期、授权/版本过滤检索与撤销时效、
# 路由接入与无自动写入；真库用例在 test_db / test_runtime_db / test_api 内）
uv run --locked --env-file ../.env.test python -m unittest tests.test_query_memory -v
# 持续库存通知（后置子项目 E：契约与逐层门禁、状态机与 gold、runner 投影收窄、
# CLI/投递；真库并发/崩溃矩阵在 test_db，通知 API 在 test_api）
uv run --locked --env-file ../.env.test python -m unittest tests.test_monitoring -v
# 底座回归
uv run python -m unittest tests.test_core -v
uv run --env-file ../.env.test python -m unittest tests.test_db tests.test_api tests.test_runtime_db -v
# 修订后的 26 题离线验收（题库与 runner 已不再是旧 20 题）
uv run --env-file ../.env.test python -m tests.acceptance --offline
Set-Location ../frontend
npm test
npm run build
```

离线验收只能证明协议、口径与确定性执行；它不证明模型理解准确率，也不构成任何平台真实来源已就绪的证据。
这些套件都写同一个 `bi_agent_test`（外层事务回滚，但种子行、advisory 锁与共享表仍是全局态），
所以请**逐条顺序跑**。本轮出现过一次“两个套件并发跑时其中一份多一个错误、串行重跑两次均干净”的
现场，未能稳定复现也不归因；验收报告里引用的数字均来自串行重跑。
本轮验收结果、逐能力就绪清单与**未执行项**（真实 provider smoke/live、真实接口取证、生产迁移与恢复、
一周试用）逐条记在 [Task 11 日期化发布验收](superpowers/research/2026-09-14-task-11-release-acceptance.md)；
门禁七项中只有前三项已完成，因此不得拿本页的命令通过当作“可发布给运营用户试用”。
上架价复核与库存预警在真实部署里只能返回 `unsupported`：两个领域的来源注册表默认为空，
只有测试进程内的合成登记会暂时打开它们（跑完即恢复）。

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
