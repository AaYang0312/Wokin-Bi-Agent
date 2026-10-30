# 库存来源验收（continuous-inventory 前置）— 当前判定：FAIL

日期：2026-09-16（2026-09-17 追加 §8：官方全量公开 API 目录核对、聚合探针与所有者 Option A 决定）
基线：`main@e760be6937bf1669149413128e6823966c33f557`（当时工作树无本记录之外的 tracked 改动，暂存区为空）；§8 追加基线：`main@c70aed417f9afcc2479ecb1590ca96038c17a39b`（本轮 tracked 改动只限本批四份文档：本文件、spec、plan 与新建的 2026-09-17 本地开发例外；暂存区仍为空，代码与运维文件零改动）

> **状态（先读这一段）**
>
> 1. 本文件**不构成**库存来源验收通过，**不构成** Task 11 发布门禁通过，**不构成**生产就绪结论，**不构成**启用持续库存通知或部署计划任务的许可。
> 2. 持续库存通知的**生产实现与启用预检仍为 FAIL 并保持停止**（plan「Execution Preflight」现在把结论拆成三条，生产启用属于其中永远未满足的那一条）。判定依据：plan 预检要求本文件对策略依赖的每个库存层级逐层写明 verified evidence；本文对 `physical` 与 `channel` 两层均给出 NOT MET。
> 3. `INVENTORY_MONITOR_ENABLED` 在 `.env.example` 与 `backend/bi_agent/config.py` 中均**不存在**（grep 为零命中）：监控能力尚未引入，等价于 enabled=false；不存在任何已启用路径。
> 4. 只有**新的日期化证据 + 所有者明确决定**能改变本判定；本文措辞或代码重构不改变任何结论。§8 就是一次这样的日期化追加。
> 5. 2026-09-17 所有者决定（§8.3）改变了两件事的**形状**，未改变任何一层的**判定**：(a) 来源门禁改为按层级判定（已核验的实物层可以在渠道层 `data_missing` 时继续出告警）；(b) 按 `docs/superpowers/research/2026-09-17-continuous-inventory-local-development-exception.md` 授权持续库存通知 Task 1–6 的**仅本机开发**。没有层级转为 MET，没有一层转为 verified，生产仍保持 disabled。

## 0. 本文件是什么 / 不是什么

- 是：continuous-inventory plan 预检（`docs/superpowers/plans/2026-09-14-continuous-inventory-notifications.md:35-55`，`rg -n 'physical|channel|freshness|scan_complete|reconciliation|enabled|data_missing|按层级' …`）所要求的日期化来源验收记录，按当前证据如实记 **FAIL**。
- 不是：任何形式的来源就绪声明、能力声明或生产放行。`2026-09-16-isolated-analysis-local-development-exception.md:33` 在其自身范围内仍然成立：那份例外**不允许**启动持续库存通知；本项目的本地开发许可来自另一份、日期更晚且专门针对本项目的 `2026-09-17-continuous-inventory-local-development-exception.md`，它只解除“不得写本地代码”的限制，不替本文件宣告任一层级就绪。
- 总设计约束（`docs/superpowers/specs/2026-09-14-post-task11-subprojects-design.md:348`）：库存通知的代码计划可以提前评审，**生产实现与实际启用必须等 §9.1 的逐层来源门禁与 §4 发布门禁满足**。

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

**逐层与“缺的是什么”的分类（2026-09-17 追加，不改判定）：** 上表八行全部仍为 **NOT MET**，但两类 NOT MET 的后续动作不同：`physical` 四行是**待取证**（端点已可用，缺的是所有者批准的时效策略、可重复 `scan_complete` 流程与生产对账）；`channel` 四行是**无源可登**（§8.1 的目录核对表明现有全量公开 API 中不存在权威逐店可售量读取端点，§8.2 的探针又表明虚拟库存里没有配置数据；叠上 `erp` 不是合法渠道通道）。这个分类不降低任何一层的取证要求，也不把渠道层变成 MET：它只意味着把渠道层当作一等 `data_missing`/unknown 是日期化结论（§8.3），而不是“本轮没读到就算没有”的静默降级。

**缺失的注册字段**：`InventorySourceRegistration`（`inventory/rules.py:472-500`）今天只有 `level/channel/evidence/max_age_seconds`；continuous-inventory plan Task 1 Step 5（plan `:256-284`）要求新增必填 `scan_complete_supported: bool` 与 `production_reconciled_at: datetime | None`，并由 `verified_monitor_levels` / `assert_monitor_sources_verified`（`monitor_source_unverified`）**按层级**给出可运行集合——fail closed 的粒度是层级，全部请求层级未核验时才回到策略级 disabled。这两个字段**尚不存在**，因此即便今天有人登记来源，也无法通过未来门禁；Task 10 现有夹具（`backend/tests/test_inventory.py:925-933,1723-1731` 等）在 Task 1 落地时需显式补值。

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

**证据局限（如实声明）**：该探针是仓库外的一次性脚本，**不属于 `tests/**` 任何断言，未被仓库测试独立复核**；其结果未持久化，不满足 `sql/019` 对每批快照的 `scan_complete`+`scan_evidence` 契约，更不构成生产对账（reconciliation）。本文件不把它记为任何一层的通过证据。 2026-09-17 的同类聚合探针见 §8.2，局限完全相同。

## 3. 与 Task 11 门禁的关系

Task 11 统一发布门禁第 4–7 项仍为 open（`docs/superpowers/research/2026-09-14-task-11-release-acceptance.md:177-190`：真实 provider smoke/live、目标环境迁移与来源核对、部署/备份/恢复、一店一周试用均"未执行"）。当前 `.env.test` 仍为 `LLM_MODEL=demo-model` + 占位 provider key（父会话核验）。即使未来本验收转为 PASS，也不解除门禁 4–7；反之亦然。两者是独立的前置。

## 4. 迁移 023 状态

`backend/sql/` 现有 001–022，`023_continuous_inventory_notifications.sql` **编号未被占用**（plan 预检第 3 条满足，2026-09-17 复核仍成立）。但这只是编号空间事实：预检的生产启用结论（第 1 条）因本 FAIL 而不满足，**023 的可用性既不推进生产启用，也不构成来源就绪证据**；它只能按 2026-09-17 例外在本机 `*_test` 库顺序执行与幂等重放。

## 5. 未执行 / 需所有者决定的事项

1. **physical 层**：所有者批准该层 freshness policy（基于实测的 `stockModifiedTime` 行为给出 `max_age_seconds`）；提供可重复执行的完整扫描流程并把 `scan_complete`/`scan_evidence` 凭据接入同步/监控链路（不是一次性脚本）；执行并日期化一次对仓库实际数量的生产 reconciliation；之后方可在 `inventory/rules.py`（唯一合法登记点）登记。
2. **channel 层：所有者已于 2026-09-17 决定（见 §8.3）。** 原开选项是 (a) 提供平台授权 channel API / 逐店带时点与完整性声明的官方导出证据，或 (b) 把首版策略缩到只剩 `physical_total`。所有者两个都没选：**保留 Option A 的两层模型，把当前 API 集视为完整，把缺逐店/逐渠道数量当作一等 `data_missing`/unknown，并把来源门禁改为按层级判定**。因此：已核验的 `physical_total` 可以在 `shop_sellable` 缺失时继续告警；渠道层永远不因实物层数据而被填零、复制或扇出，也不参与店铺级触发或解除。这一决定**不解除**第 1 条对 physical 层的四项要求，也不把渠道行改为 MET。
3. **plan Task 1 落地时**：`InventorySourceRegistration` 需按 plan `:256-284` 增加 `scan_complete_supported` 与 `production_reconciled_at`，并同步 Task 10 夹具——这是代码侧前提，但不改变本 FAIL。
4. **Task 11 门禁 4–7**：独立推进，与本验收互不替代。

## 6. 敏感数据纪律

本文件及 2026-09-16 探针全程：仅聚合统计；无 DSN、API 密钥、Token、真实业务主键、客户信息或未脱敏错误原文；无任何数据库/生产写入；PDD 永久排除口径不变（`inventory/rules.py:17,95,490`：来源白名单无任何拼多多通道）。2026-09-17 追加部分同样：只读公开文档索引与聚合口径计数（店铺总数 42、启用非 PDD 21），无逐店名称/编号、无数量业务值、无凭据、无写操作；`可参考/llms.txt` 为受保护的未跟踪参考文件，本轮**只读**，未修改、未暂存、未提交。

## 7. 结论

截至 2026-09-16 / `main@e760be6`：`physical` 与 `channel` 两层的 registration、freshness、scan_complete、reconciliation 四维全部 NOT MET，监控**生产启用**预检 **FAIL**，`INVENTORY_MONITOR_ENABLED` 等价 enabled=false。

更新到 2026-09-17 / `main@c70aed4`（§8）：判定不变——没有任何层级转为 MET，生产仍保持 disabled，Task 11 第 4–7 项仍 open。发生变化的只有两点：门禁粒度从策略级改为**按层级**（已核验层可跑，缺来源层为一等 `data_missing`，不补零、不替位、不触发、不解除、不向用户播报），以及 Task 1–6 获得一纸**仅本机开发**例外。本判定仍然只能被**新的日期化证据 + 所有者明确决定**改变；对本文的文字修改或与证据无关的代码改动均不改变判定。

## 8. 2026-09-17 追加：官方全量公开目录核对、聚合探针与所有者决定（Option A）

本节按所有者指示追加，基线 `main@c70aed417f9afcc2479ecb1590ca96038c17a39b`。它**不改变 §1 的任何判定**（没有一行转为 MET），只 (a) 记录两条新事实、(b) 记录所有者的决定、(c) 把 monitor 的来源门禁粒度从策略级改为层级级。本轮只改文档：未改代码、未跑迁移、未建任何数据库连接、未调用模型、除 §8.2 引用的既有聚合口径外未发起任何真实 API 调用。

### 8.1 官方全量公开 API 目录核对（文档证据）

按所有者授权，本轮只读快麦公开文档索引 `可参考/llms.txt`（受保护的未跟踪参考文件，只读、未修改、未暂存），并只沿库存相关链接核对能力形状。结论：

- 公开的**读取**面覆盖公司/货品级与仓库级库存以及身份映射：`查询库存状态`（货品/公司级）、`查询仓库及商品库存信息`、`仓库查询`、`店铺查询`（店号与平台身份映射）、`货位库存查询列表`、`平台商品批次效期库存查询列表`。
- 虚拟库存一侧只有**写**接口（`修改虚拟库存`、`批量修改虚拟库存`）加 `查询虚拟仓`；目录里**没有**任何一个能返回"某店某 SKU 权威可售量"的读取端点。
- `库存上传主动通知` 挂在「API场景说明 / 自建平台」分组下，与 `库存业务对接`、`订单业务对接`、`商品业务对接` 同组：它的语义是**自建平台把库存上传给快麦**的对接说明，不是给现有平台店铺提供权威可售量的读取来源。把它的回调当读取源，等于把自己写回去的数再读回来当第三方证据。
- 因此：**官方全量公开 API 集不含权威的逐店/逐渠道可售量读取端点**。这与 §1 里"`erp` 不是 `CHANNEL_SOURCE_KINDS` 合法通道"的结构结论相互独立，两条同时成立。

### 8.2 聚合口径探针（能力性事实，仍非验收证据）

沿用现有可用凭据的一次聚合口径探针（与 §2 同一纪律：只出计数、无业务标识、无写库）：

- 店铺：42 家，其中 21 家启用且非 PDD（PDD 永久排除口径不变）。
- `erp.virtual.warehouse.query`：分页取尽，`complete_by_reported_total = true`，`total = 0`。
- `erp.item.virtual.stock.query`：分页取尽，`complete_by_reported_total = true`，`total = 0`。

这组数字只证明两件事：**端点可调用、分页/完整性形状可用**；以及**虚拟仓与虚拟库存侧没有配置数据**。它不证明逐店可售量可获取，不构成任何层级的 registration/freshness/scan_complete/reconciliation MET，也不是生产对账；与 §2 的局限声明同样，它是一次仓库外脚本产出的结果，不属于 `tests/**` 的断言，也未持久化进 `sql/019` 的 `scan_complete`+`scan_evidence` 契约。

`total = 0` 更是"**没有数据**"而不是"数据等于 0"：按 §8.3 第 2 条，它记为 `data_missing`/unknown，绝不允许被写成"这些店铺的可售量为零"或据此触发/解除任何告警。

### 8.3 所有者决定（2026-09-17）

1. **当前 API 集视为完整**：本项目不再把"等一个新读取端点"当作渠道层的前置。
2. **Option A 仍是目标模型**：`physical_total` 与 `shop_sellable` 两个层级都保留。缺逐店/逐渠道数量是一等的 `data_missing`/unknown——**永远不是零**，**永远不从 `physical_total` 复制或扇出**，**永远不参与店铺级触发或解除**。
3. **来源门禁按层级判定**：已核验的 `physical_total` 可以在 `shop_sellable` 缺失时继续出告警；缺已核验登记的层级既不能触发也不能解除，在监控路径上 runner 只把已核验子集发给 graph——`InventoryInspectionRequest.levels` 与图上下文的 `DomainContext` 授权投影**同时**收窄（只核验实物 ⇒ 仅获准池 + 空店铺授权投影，graph 据此根本不读渠道快照、不产生店铺声明；只核验渠道 ⇒ 仅店铺范围；两层都核验 ⇒ 两者都带；策略仍记录完整请求层级与全部 opaque refs），所以这一层无行、无去重键、不撑 `expected_items`，它的快照与店铺事实根本不被读取，并从 `complete_levels` 中省略；它自己的缺席不得使已核验层级变得不完整。策略请求的层级里一个都没有核验时，该策略以 disabled 退出（不跑图）。设计落点：spec §9.1–§9.4；实现落点：plan Task 1 Step 5（`verified_monitor_levels` / `assert_monitor_sources_verified`）、Task 3 Step 4 决策表与 Task 4 Step 3 的 graph 请求与授权投影收窄（含两个最小 graph 契约修订：空店铺投影下节点 1 不以 `inventory_scope_empty` 终止、授权池对按池授权集直接派生）。
4. **不发周期性“无法判断”通知**：缺失层级只记固定诊断与固定计数指标——监控路径上用 gate 自己的闭集码 `monitor_level_unverified` 加 `inventory_monitor_level_unverified_total{level}`（因为这一层从未被询问、其授权投影也被收窄，所以不会、也不应该出现 graph 的 `inventory_channel_source_unverified` 或任何渠道快照读取；后者仍属于聊天路径），不写 outbox、不进通知中心。
5. **缺扫描不等于恢复**：一个曾经核验的渠道源本轮没有扫描，不得把该层 open/acknowledged 告警解除；能力或来源退场只用既有 `suppressed` 语义，永不 `resolved`。
6. **生产今天仍然关闭**：`physical` 的来源登记、freshness policy、可重复 `scan_complete` 凭据与生产对账全部未落地；Task 11 门禁第 4–7 项、部署与一店一周试用同样 open。本节改变的是门禁粒度与本地开发授权，**不构成** PASS、生产就绪或通知启用。

执行授权只有一份：`docs/superpowers/research/2026-09-17-continuous-inventory-local-development-exception.md`（Task 1–6 仅限本机 `*_test` 库、离线/stub 测试，`INVENTORY_MONITOR_ENABLED=false` 默认，迁移 023 只在本机测试库）。该例外不解除 §5 第 1 条对 physical 层的四项取证要求，也不改变 §1 判定矩阵。
