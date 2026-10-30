# 技术方案：前两个工作流（商品跨店分析 + 平台对比）上线去限制化

日期：2026-09-25。基线：`main@675ac19`。
前置取证：`2026-09-25` 凌晨四工作流可用性结论（会话评估：商品运营/平台对比=受限可用，上架复核/库存预警=真实环境不可用）。
关联文档（必读）：
- `docs/superpowers/specs/2026-09-12-multi-source-metrics-design.md` §3–§6（多来源指标与口径契约）
- `docs/superpowers/specs/2026-09-11-operator-workflows-design.md` §10（运营工作流）
- `docs/superpowers/research/2026-09-12-taoxi-onboarding.md` §5.1（TRADE_CLOSED 三个候选口径）
- `docs/superpowers/research/2026-09-14-inventory-source-acceptance.md` §8（Option A：缺来源=一等 data_missing，不补零）
- `logs/kuaimai-llms-full-fresh.txt`（2026-09-12 官方文档快照，字段事实以它为准；开工前重新抓一次并记录 SHA-256）

## 0. 目标与原则

目标：让「商品跨店分析」「平台对比」两个功能在**不新增外部数据渠道**（仅快麦 API + 本地已有数据）的前提下达到可发布状态，把"受限可用"的每一项限制变成三态之一：**已解决 / 已降级为诚实展示 / 明确挂起并有路径**。

原则（全部沿用现有纪律，不重开）：
1. 口径诚实：任何指标名称与其数据来源语义必须一致；不可比的东西不放进同一个排名。
2. Fail-closed + 一等 data_missing：无认证来源的格子显示"不可用+原因"，永远不补零、不替位、不外推（Option A 决定）。
3. PII 白名单不变：whitelist-only normaliser、守卫测试继续有效。
4. 拼多多永久排除口径不变。
5. 一切真实调用仅只读探针 + `bi_agent_test` 库；不碰生产。

## 1. 限制清单 → 处置矩阵

| # | 现状限制 | 根因 | 处置 | 本方案切片 |
|---|---|---|---|---|
| L1 | 五平台同口径总排名不可靠 | 支付时间口径仅抖音认证；淘系窗口实测不成立 | **换基座**：平台对比提供双口径——全平台「ERP 出库口径」排名（出库时间所有平台同源同语义）+ 「支付口径」仅标已认证平台，其余格 data_missing | S1 |
| L2 | 淘系付款窗口不成立 | `timeType=pay_time` 过滤语义随渠道漂移（~1% 行落在窗外） | **宽窗拉取 + 本地 payTime 过滤 + 差集证据实验**；收敛后淘系支付口径转 certified | S2 |
| L3 | TRADE_CLOSED 净额虚低、退款匹配 8.1% | 已付后退关闭单成 orphan | **所有者口径决策落地**：支付口径剔除 closed-refund 单、退款单列展示；写进指标定义与 disclosure | S3 |
| L4 | 「售价」实为成交均价 | 渠道在售价无源 | **接 ERP 档案价快照**：`erp.item.list.get` 的 `priceOutput`（销售价）/`purchasePrice`（成本价），标注"ERP 档案价，非促销后渠道实价" | S4 |
| L5 | 「利润」未扣广告/平台费 | 快麦订单通道无推广实耗 | **参数化估算口径**：店铺级配置"平台综合费率%"→ 输出 `estimated_net_margin`，UI 永久带「估算」标签；推广实耗增值接口另线推动 | S5 |
| L6 | 京东/快手 observable 未 certified | 缺逐店账单对照 | **对账工具 + 滚动认证清单**（运营取证任务，格式由系统产出） | S6 |

## 2. 切片任务书

### S1 双口径平台对比（核心，先行）
- 契约：`platform_comparison` 响应增加 `caliber` 维度（`erp_outstock` | `platform_payment`），每个 (platform, metric, caliber) 格走现有 capabilities 门禁；排名图默认出库口径并显式徽标；支付口径 tab 只聚合 `certified` 平台组，未认证平台展示原因而非缺席。
- 数据：出库口径指标（`erp_documents`、出库金额、出库销量）复用现有 ERP 单据事实；**不新建指标语义**，只允许在 reporting view 层组合。若出库金额字段当前不存在，从出库单的 items 行金额聚合，聚合规则写入 spec 并测试锁定。
- 验收：五平台（除 PDD 支付口径外）出库口径总排名可复现且通过既有确定性校验；支付口径 tab 下 tb/tm/jd/ks 返回 data_missing + 原因码；26/26 离线业务题扩展覆盖两口径问法（≥2 题）。

### S2 淘系支付口径取证实验（纯离线，可能翻案）
- 方法：选 1 家 tb + 1 家 tm 店，对同一半月窗口做三路拉取：① API `timeType=pay_time`；② API 出库时间宽窗（±15 天）+ 本地 `payTime ∈ [start,end)` 过滤；③ ①②差集逐单归因（漏/多各属哪类：跨窗支付、TRADE_CLOSED、时间字段语义）。
- 判定：若差集全部可归因且解释覆盖率 100%，产出日期化取证文档，把 tb/tm 支付口径标记为「本地过滤认证」，同步链路改为宽窗+本地过滤（含幂等回补）；否则维持 NOT MET 并记录反例。
- 红线：只读探针，聚合输出，无订单号/买家信息落盘。

### S3 TRADE_CLOSED 口径决策落地
- 依赖：S2 的归因数据。决策候选见 taoxi-onboarding §5.1；**默认建议（待所有者确认）**：支付口径 = 剔除 closed-then-refunded；净额视图 = 支付额 − 退款额单列。
- 实现：metrics 定义、gate membership（注意既有教训：`paid_amount` 曾不受 unmatched-refund gate 保护）、disclosure 文案、旧数字对比回归（差值必须逐项可解释）。

### S4 商品档案价快照
- 新表 `bi.item_price_snapshots`（shop_id, sys_sku_id/outer_id+sku_outer_id, price_output, purchase_price, captured_at, source='kuaimai_item_api', scan_complete 凭据同 019 契约精神）。
- 同步：`erp.item.list.get` 按店全量扫描（分页+限流退避），日频快照；商品分析新增 `reference_price`（档案售价）与 `margin_ref`（(均价−档案成本)/均价，**只到 SKU 级参考，不进排名**）。
- 语义护栏：档案价与成交均价并列展示、不可互相冒充；`activeStatus=0` 商品显式"已停用"。
- 验收：全店扫描可重复执行且 `scan_complete` 有据；快照过期（>max_age）时指标降级为 data_missing。

### S5 净利估算口径（可选项，但 UI/契约先行）
- 店铺配置新增 `platform_fee_rate`（运营手填，带 `estimated=true` 标记）；`estimated_net_margin = gross_profit_ref − 销售额×fee_rate`，输出名、tooltip、导出列全部带「估算」。
- 该字段永久不进 certified 门禁与排名指标；推广实耗接口开通后替换为实测值（留 TODO 锚点）。

### S6 京东/快手认证工具
- 产出 `docs/superpowers/research/` 用的对账模板：每店每月「ERP 出库/支付聚合 vs 平台账单导出」差集表 + 判定行（certified/observable/failed）；工具接受 CSV 导入，输出日期化证据文件。
- 认证状态驱动 capabilities：认证一家店，排名里该平台的支付口径格即时点亮。

## 3. 工程与流程约束（写给执行 agent）

- 单一写者：每个切片一次只有一个 writer；切片顺序 S1 → S2 → S3 → S4 → S6，S5 可并行末位。
- 每切片交付：代码 + 迁移（如需）+ 测试 + 日期化验收/取证文档；`docs: authorize/verify <slice>` 提交信息惯例不变；不 push、不 merge main 之外分支（提交即停，等人工复核）。
- 测试基线不得回退：后端 1701、DB 套件 289（0 skip）、离线业务题 26、前端 172 + build 绿。
- 真实 API 探针一律复用 `logs/probe_*.py` 模式（KuaimaiClient、聚合输出、脚本产物脱敏）。
- 快麦文档快照每日新鲜度要求：开工时重抓 `open.kuaimai.com/llms-full.txt` → `logs/`，记录 SHA-256，与 9-12 快照 diff 出字段变化再动手。

## 4. 风险与开放问题

1. S2 若差集不可完全归因（时间字段语义本身漂移），淘系支付口径本季度维持 data_missing——发布形态仍成立（出库口径为基座）。
2. 出库金额聚合可能与 `paid_amount` 数值有系统性差（运费、优惠摊行规则）；S1 必须先用一家认证店（抖音）做双口径对账，差率>阈值则先修聚合规则再上图。
3. 档案价质量取决于运营维护（可能长期不更新）；S4 的 max_age 降级策略需所有者给默认值（建议 7 天）。
4. `platform_fee_rate` 手填真实性无校验，只影响带「估算」标签的列，不影响任何 certified 指标。
5. 推广实耗接口、拼多多方舟通道：外部依赖，保持挂起，不阻塞本方案。

## 5. 发布形态（本方案完成时）

- 平台对比：出库口径全平台（含 tb/tm/jd/ks/fxg）同图排名 + 支付口径（fxg 起，tb/tm 视 S2 取证，jd/ks 视 S6 逐店认证）；PDD 显式排除声明。
- 商品分析：期间/趋势/排名/销量/销售额 + 成交均价 + 档案参考价 + 参考毛利（+ 可选估算净利）；每列语义徽标。
- 上架复核、库存预警：维持现状（后两个功能，不在本方案范围）。
