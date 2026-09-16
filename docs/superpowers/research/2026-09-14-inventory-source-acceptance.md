# 库存来源验收（continuous-inventory 前置）— 当前判定：FAIL

日期：2026-09-16
基线：`main@e760be6937bf1669149413128e6823966c33f557`（工作树无本记录之外的 tracked 改动，暂存区为空）

> **状态（先读这一段）**
>
> 1. 本文件**不构成**库存来源验收通过，**不构成** Task 11 发布门禁通过，**不构成**生产就绪结论，**不构成**启动 `2026-09-14-continuous-inventory-notifications.md` Task 1 的许可。
> 2. 持续库存通知的执行预检（plan「Execution Preflight」）判定为 **FAIL**，Task 1 实现保持**停止**。判定依据：plan 预检要求本文件对策略依赖的每个库存层级逐层写明 verified evidence；本文对 `physical` 与 `channel` 两层均给出 NOT MET。
> 3. `INVENTORY_MONITOR_ENABLED` 在 `.env.example` 与 `backend/bi_agent/config.py` 中均**不存在**（grep 为零命中）：监控能力尚未引入，等价于 enabled=false；不存在任何已启用路径。
> 4. 只有**新的日期化证据 + 所有者明确决定**能改变本判定；本文措辞或代码重构不改变任何结论。

## 0. 本文件是什么 / 不是什么

- 是：continuous-inventory plan 预检（`docs/superpowers/plans/2026-09-14-continuous-inventory-notifications.md:30-41`，`rg -n 'physical|channel|freshness|scan_complete|reconciliation|enabled' …`）所要求的日期化来源验收记录，按当前证据如实记 **FAIL**。
- 不是：任何形式的放行、例外或能力声明。`2026-09-16-isolated-analysis-local-development-exception.md:33` 明文规定：该例外**不允许**启动持续库存通知；后者必须先取得本文件并通过逐层证明。
- 总设计约束（`docs/superpowers/specs/2026-09-14-post-task11-subprojects-design.md:335`）：库存通知的代码计划可以提前评审，**实际实现必须等真实库存来源门禁满足**。

## 1. 判定矩阵（grep 友好；逐层逐维度）

| 层级 | 维度 | 判定 | 依据（摘要） |
| --- | --- | --- | --- |
| physical（physical_total） | registration enabled | **NOT MET** | 生产注册表按设计为空：`inventory/rules.py:504-523`（`_REGISTRY` 初始为空、`reset_inventory_sources` 清空、`verified_inventory_source` 返回 None）；守卫测试 `backend/tests/test_inventory.py:411-419` 禁止 `rules.py` 之外出现任何 `register_inventory_source(`；`inventory/graph.py:566-586` `check_inventory_source` 因此把每一格判为 `unsupported` 且零数量放行。 |
| physical（physical_total） | freshness policy | **NOT MET** | 无所有者批准的 `max_age_seconds`；探针只见原始毫秒时间戳（`stockModifiedTime`），其语义未经策略核验；`freshness_ok`（`inventory/rules.py:401-415`）要求已登记的正时效策略 + 可信 `captured_at`，二者皆无。 |
| physical（physical_total） | scan_complete credential | **NOT MET** | 快照契约要求每批带 `scan_complete` + `scan_evidence` 分页凭据（`backend/sql/019_inventory_snapshots.sql:73,88-89,108`）；这两列的唯一写入方是 `inventory/repository.py:518,582`（目前仅测试夹具调用）。2026-09-16 的一次性探针脚本在运行后已删除，不是可重复的扫描凭据；单次时点扫描 ≠ repeatable scan_complete。 |
| physical（physical_total） | reconciliation（生产对账） | **NOT MET** | 从未执行过对仓库实际数量的生产对账；`erp.warehouse.list.query` 返回 1 个启用自有仓、分仓样本仅 1 SKU / 1 仓——样本不是对账；交易侧 reconcile（`sync.py`）从不触碰库存表。 |
| channel（shop_sellable） | registration enabled | **NOT MET** | 同上注册表为空；且 `CHANNEL_SOURCE_KINDS`（`inventory/rules.py:100-101`）只允许 `channel_api` / `official_export` / `manual_import`——**`erp` 不是渠道层合法来源**，ERP 派生数据流无法登记为 `shop_sellable`。 |
| channel（shop_sellable） | freshness policy | **NOT MET** | 无渠道层数据流，自然无时效策略。 |
| channel（shop_sellable） | scan_complete credential | **NOT MET** | `bi.channel_stock_snapshots` 主键为 `(namespace, shop_id, snapshot_id)`（`backend/sql/019_inventory_snapshots.sql:170`），`platform`/`source` 是非键列、仅受 CHECK 白名单约束（列定义 `:163-164`；`source` 白名单 `:171-172`、`platform` 格式 `:180`），并同样强制 `scan_complete`/`scan_evidence`（约束在 178-179）；现有任何 API 证据都不提供逐店身份，更无完整扫描凭据。 |
| channel（shop_sellable） | reconciliation | **NOT MET** | 无任何渠道层数据可比对。 |

**缺失的注册字段**：`InventorySourceRegistration`（`inventory/rules.py:472-500`）今天只有 `level/channel/evidence/max_age_seconds`；continuous-inventory plan Task 1 Step 5（plan `:199-209`）要求新增必填 `scan_complete_supported: bool` 与 `production_reconciled_at`，并用 `assert_monitor_sources_verified`（`monitor_source_unverified`）逐层 fail closed——这两个字段**尚不存在**，因此即便今天有人登记来源，也无法通过未来门禁；Task 10 现有夹具（`backend/tests/test_inventory.py:925-933,1723-1731` 等）在 Task 1 落地时需显式补值。

## 2. [LIVE] 能力性事实（2026-09-16 真实快麦只读探针；仅聚合，非验收证据）

以下事实由一次真实只读调用产生（`KuaimaiClient`，官方 `https://gw.superboss.cc/router` 签名通道），**只证明接口/凭据/参数形状可用**，不构成上表任何一维的 MET：

- `stock.api.status.query`：官方文档 `pageSize` 上限 100（错误码 20019「页数不能超过100」）。按 100 分页实际扫完全目录：22 页（21×100 + 49），`total = records = 2149`，`complete_by_reported_total = true`。
- 唯一性：2149 条 `(sysItemId, sysSkuId)` 身份全部唯一，重复 0。
- 数值字段覆盖 2149/2149：`totalAvailableStockSum`、`totalLockStock`、`totalAvailableStock`、`totalDefectiveStock`、`sellableNum`、`allocateNum`、`purchaseNum`、`onTheWayNum`、`refundStock`、`purchaseStock`；`publicStock` 0/2149（该关联字段未请求，全量缺席）。
- `stockModifiedTime` 2149/2149 存在，原始毫秒纪元 min=1689847179000、max=1789568590000；**语义未对照任何时效策略核验**。
- 状态分布：3（无货）=611、4（超卖）=52、6（有货）=1486。
- 渠道作用域字段 `shopId/userId/platform/source/channel/channelId`：**0/2149** ——该接口是货品/公司级，无逐店身份。
- 仓库：`erp.warehouse.list.query` 返回 1 条（type=0 自有仓，status=1 正常）；`erp.item.warehouse.list.get` 按 1 个 `outerId` 抽样得 1 SKU 行 / 1 分仓行（type=0，status=1），五个库存数值字段 1/1。
- 过程中出现平台限流（客户端按既有重试策略退避后成功），说明该通道有真实速率约束——未来任何周期扫描必须把限流与退避纳入扫描凭据设计。
- 脱敏与边界：输出仅聚合统计，无订单号、无客户信息、无店铺/仓库/商品业务标识、无 DSN 或凭据；未向任何数据库写入；临时探针脚本运行后已删除。

**证据局限（如实声明）**：该探针是仓库外的一次性脚本，**不属于 `tests/**` 任何断言，未被仓库测试独立复核**；其结果未持久化，不满足 `sql/019` 对每批快照的 `scan_complete`+`scan_evidence` 契约，更不构成生产对账（reconciliation）。本文件不把它记为任何一层的通过证据。

## 3. 与 Task 11 门禁的关系

Task 11 统一发布门禁第 4–7 项仍为 open（`docs/superpowers/research/2026-09-14-task-11-release-acceptance.md:177-190`：真实 provider smoke/live、目标环境迁移与来源核对、部署/备份/恢复、一店一周试用均"未执行"）。当前 `.env.test` 仍为 `LLM_MODEL=demo-model` + 占位 provider key（父会话核验）。即使未来本验收转为 PASS，也不解除门禁 4–7；反之亦然。两者是独立的前置。

## 4. 迁移 023 状态

`backend/sql/` 现有 001–022，`023_continuous_inventory_notifications.sql` **编号未被占用**（plan 预检第 3 条满足）。但这只是编号空间事实：预检第 1–2 条（两份验收文件存在且逐层 verified evidence）因本 FAIL 而不满足，**023 的可用性不能推进 Task 1**。

## 5. 未执行 / 需所有者决定的事项

1. **physical 层**：所有者批准该层 freshness policy（基于实测的 `stockModifiedTime` 行为给出 `max_age_seconds`）；提供可重复执行的完整扫描流程并把 `scan_complete`/`scan_evidence` 凭据接入同步/监控链路（不是一次性脚本）；执行并日期化一次对仓库实际数量的生产 reconciliation；之后方可在 `inventory/rules.py`（唯一合法登记点）登记。
2. **channel 层**：现有 ERP 通道证据**在结构上不可能**满足 `shop_sellable`（`rules.py:100-101` 拒绝 `erp` 通道）。所有者必须二选一：(a) 提供平台授权 channel API 或逐店带时点/完整性声明的官方导出证据；(b) 明确决定首版 monitor 策略仅含 `physical_total` 层，并修订 plan 与 spec §9 相应措辞。任一决定都需要新的日期化证据。
3. **plan Task 1 落地时**：`InventorySourceRegistration` 需按 plan `:199-209` 增加 `scan_complete_supported` 与 `production_reconciled_at`，并同步 Task 10 夹具——这是代码侧前提，但不改变本 FAIL。
4. **Task 11 门禁 4–7**：独立推进，与本验收互不替代。

## 6. 敏感数据纪律

本文件及 2026-09-16 探针全程：仅聚合统计；无 DSN、API 密钥、Token、真实业务主键、客户信息或未脱敏错误原文；无任何数据库/生产写入；PDD 永久排除口径不变（`inventory/rules.py:17,95,490`：来源白名单无任何拼多多通道）。

## 7. 结论

截至 2026-09-16 / `main@e760be6`：`physical` 与 `channel` 两层的 registration、freshness、scan_complete、reconciliation 四维全部 NOT MET，监控执行预检 **FAIL**，continuous-inventory Task 1 保持停止，`INVENTORY_MONITOR_ENABLED` 等价 enabled=false。本判定只能被**新的日期化证据 + 所有者明确决定**改变；对本文的文字修改或与证据无关的代码改动均不改变判定。
