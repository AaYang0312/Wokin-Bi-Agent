# 持续库存通知本地开发例外

日期：2026-09-17
基线：`main@c70aed417f9afcc2479ecb1590ca96038c17a39b`（本文件提交前后，工作树除本记录与同批文档外无 tracked 改动，暂存区为空）

状态：**仅授权本地开发；不构成来源验收通过、不构成发布门禁通过、不构成生产就绪或通知启用**

## 决定

项目所有者在 2026-09-17 明确两件事，本文件只记录第二件，第一件的证据链见
`2026-09-14-inventory-source-acceptance.md` §8：

1. **模型决定（Option A 保持，来源门禁改为按层级判定）。** 当前快麦公开 API 集视为完整：官方全量公开文档暴露公司/仓库级库存查询与身份映射，但**没有**任何返回权威逐店可售量的读取端点；`库存上传主动通知` 属于自建平台对接（把库存上传给快麦），不是现有店铺的权威读取来源。因此缺逐店/逐渠道数量是一等的 `data_missing`/unknown：**永远不是零**、**永远不从 `physical_total` 复制或扇出**、**永远不参与店铺级触发或解除**。所有者选择：**已核验的 `physical_total` 可以在 `shop_sellable` 缺失时继续告警**；不发送周期性“无法判断”通知，只记固定诊断与指标；一个曾经核验的渠道源本轮缺扫描**不得**解除 open/acknowledged 的渠道告警，能力或来源退场只用既有 `suppressed` 语义，永不 `resolved`。
2. **执行决定（本文件）。** 在库存来源验收仍为 **FAIL**、Task 11 统一发布门禁第 4–7 项仍 open 的前提下，允许在本地实施 `2026-09-14-continuous-inventory-notifications.md` 的 **Task 1–6**。

该决定只解除“不得写持续库存通知功能代码”的本地实施限制，并按所有者决定把 monitor 来源门禁的粒度改成层级级；它不修改总设计中的生产发布标准，不把任何未执行验证记为通过，也不让任何库存层级转为 verified。

同日追加的真实证据口径（只读、仅聚合，沿用现有可用凭据）：店铺 42 家、其中 21 家启用且非 PDD；`erp.virtual.warehouse.query` 分页取尽且 `total = 0`；`erp.item.virtual.stock.query` 分页取尽且 `total = 0`。这只证明端点可调用与**没有配置数据**，不证明生产对账、不证明来源验收、更不把 `total = 0` 读成“可售量为零”。

## 本例外同时确立的设计修订

- 来源门禁**逐层判定**：策略只能运行它请求的层级里已核验的那部分，runner 也只把这部分发给 graph——`InventoryInspectionRequest.levels` 与图上下文的 `DomainContext` 授权投影**同时**收窄（只核验实物 ⇒ 仅获准池 + 空店铺授权投影，graph 据此根本不读渠道快照、不产生店铺声明，渠道侧时点/扫描声明/独有 SKU 进不了物理轮的聚合与指纹；只核验渠道 ⇒ 仅店铺范围；两层都核验 ⇒ 两者都带；策略仍记录完整请求层级与全部 opaque refs，存储永不收窄）；缺已核验登记的层级不能触发也不能解除，在监控路径上无行、无去重键、不占 `expected_items`，从 `complete_levels` 省略，只以 gate 自己的闭集固定码 `monitor_level_unverified` 与固定计数出现（不再靠 graph 为它出 `unsupported` 占位行）；它的缺席与其任何诊断都不得使已核验层级变得不完整。
- 请求层级里**一个都没有**核验时，该策略以 disabled 成功退出：不跑图、不开事务、不写 outbox、不发通知。
- 保留的 fail-closed 与边界：缺数量不是零；不做层级替位；不允许假恢复；缺来源层级不产生任何面向用户的“无法判断”通知；`bi_monitor`/`bi_app`/`bi_sync` 权限分离、载荷去标识化、单事务原子提交与 dedupe/generation 语义全部不变。

## 允许范围

- 仅实施持续库存通知计划 Task 1–6（契约与按层级 source gate、023 与 `bi_monitor` 角色与 repository、按层级的确定性状态机、一次性 runner/CLI、outbox 与应用内通知中心、并发/崩溃与本地验收文档）。
- 一切执行都限定在本机：`*_test` 数据库、离线测试与 stub 模型；`TEST_DATABASE_URL` 必须指向本机 `bi_agent_test`。
- `INVENTORY_MONITOR_ENABLED=false` 保持默认关闭；`.env.example` 只放变量名，不放任何凭证值。
- 允许编写 `023_continuous_inventory_notifications.sql`，并**只**在本机 `*_test` 数据库顺序执行和幂等重放。
- 允许在本机测试库验证 `bi_monitor` 权限矩阵、单事务原子性、advisory lock 并发与 outbox 幂等。
- 允许按上述“本例外同时确立的设计修订”修订 spec §9/§11 与 plan 的接口、测试与最终检查条目（本轮已完成）。
- 每个计划 Task 继续执行独立审查、父级全量验证和单独提交；feature gate 关闭时，现有 Tool、数据库读取和用户可见行为必须保持不变。

## 仍然禁止

- 生产/预发布流量中启用持续库存通知；把它描述为“来源已就绪”“已上线”或“验收 PASS”。
- 任何新的真实快麦 API 调用、真实 provider smoke/live、生产或预发布数据库访问、迁移执行、来源同步、回填、生产对账、部署、OIDC、Origin、SSE、备份与恢复演练。
- 部署主机计划任务注册（Windows Task Scheduler / systemd timer）与任何常驻循环；本轮 runbook 只写文档，不执行。
- 把 `2026-09-14-inventory-source-acceptance.md` §8.2 那类聚合探针结果写进本机 `*_test` 数据库以外的任何库，或把它当作某层来源 verified 的证据。
- 用 `physical_total` 填充、复制或扇出成 `shop_sellable` 数量；把缺数据、缺扫描、stale、`unsupported` 或 `unknown` 当成零、当成安全或当成恢复。
- 为缺失层级生成周期性“无法判断”用户通知；缺失只能进固定诊断与固定计数指标。
- 把来源/能力退场写成 `resolved`；退场只能用 `suppressed`，且不写 outbox。
- PDD 连接器、来源、支付能力、凭据或 onboarding：`erp` 仍不是 `shop_sellable` 的合法来源种类（`inventory/rules.py` 的 `CHANNEL_SOURCE_KINDS`），来源白名单仍无任何拼多多通道。按层级放宽门禁只改“谁能出数”，不改“登记凭什么算核验”。
- 邮件、短信、企业微信、Slack、任意 webhook 或外部通知凭证；自动采购、调拨、改价或模型参与状态迁移。
- 修改或提交 `可参考/` 下的受保护参考文件（本轮只读 `可参考/llms.txt`）。

## 仍未满足的前置（逐条，均为“未执行”，不记为通过）

`2026-09-14-inventory-source-acceptance.md` §1 的判定矩阵：`physical` 与 `channel` 两层的 registration、freshness policy、`scan_complete` 凭据、reconciliation 共八行全部 **NOT MET**。本例外只授权本地写代码与本地测试，不解除其中任何一项；physical 层要转为 verified 仍需所有者批准的时效策略、可重复的 `scan_complete` 扫描流程与一次日期化的生产对账。

`2026-09-14-task-11-release-acceptance.md` 记录的统一发布门禁第 4–7 项仍为 open：

1. 获准真实 provider smoke 与 26 题 live；
2. 目标环境迁移、真实来源及能力核对；
3. 可信身份、Origin、SSE、同步、重核、脱敏日志、备份和真实恢复；
4. 一店一周试用与问题归档。

## 结论口径

本例外执行完成后的正确说法是：“持续库存通知的本地实现与本机/离线测试通过；来源验收仍 FAIL；生产监控保持 disabled。”

错误说法（任何一份文档都不得出现）：“库存来源已核验”“库存通知已就绪/已启用”“渠道层已按实物层补齐”“Task 11 门禁已通过”。

任何生产启用、真实来源取证或新的层级模型变更，都需要新的、明确的所有者决定与新的日期化证据。
