# 淘系订单/售后接入复核（非敏感出库通道）

> 合并后注记（2026-09-12）：本文保留 taoxi 分支实测快照，不代表统一 main 或真实库修复后的结果。重构分支已有“关闭但已付款仍参与支付/退款匹配”的修复；main 合并保留该逻辑。§5.1 选择保留active=false、恢复经认证支付及匹配的C修正版，§1.6覆盖/退款/basis问题纳入[修订设计](../specs/2026-09-12-multi-source-metrics-design.md)及原计划任务5、11。报告中“只修匹配不改GMV”和固定旧列白名单不作为合并后契约。

日期：2026-09-12 ｜ 关联：`2026-09-06-kuaimai-data-verification.md` ｜ 分支：`taoxi/outstock-sync`

## 结论

1. **淘系（tb/tm）订单已经 `erp.trade.outstock.simple.query`（交易模块·销售出库查询）以非敏感字段口径接入 `bi.orders` / `bi.order_items` / `bi.order_payments`**，与抖音 `erp.trade.list.query` 通道并存，`sync_state` 按 `(source, entity, shop_id)` 主键隔离，互不干扰。
2. **淘系售后走既有 `erp.aftersale.list.query` 通道直接可用**，无需改动路由（实测见下表；`tid/sid/rawRefundMoney/refundMoney/platformCompleteTime` 非空率高）。
3. **口径限制（必须向使用者声明）**：淘系订单是 **ERP 销售出库口径，不是平台账单口径**。收件人姓名/手机/地址/省市区/街道/邮编、`buyerNick`、`buyerMessage`、发票、`taobaoId`、`platformPaymentAmount`、`ptConsignTime` 均不返回或为敏感字段；出库响应携带的 `shopName/sellerNick/openUid/mobileTail` **一律不入库**。实付、成本、毛利、佣金、邮费、状态、商品行齐全，可支撑经营分析；不宣称财务对账完成。
4. 平台→源路由为模块常量 `ORDER_SOURCE_BY_PLATFORM = {"tb": OUTSTOCK_SOURCE, "tm": OUTSTOCK_SOURCE}`（`_shop_order_source` 查 `bi.shops.platform` 解析，缺档案直接报错防假覆盖），其余平台（含未知）回退 `erp.trade.list.query`；同步前先跑 `sync shops`。
5. **重要口径后果（需口径负责人决策，§1.3/§5.1）**：出库通道会返回 `status=TRADE_CLOSED` 的已付款单，按任务书判定为 `active=false` 后，这批单（608 张、单头实付 ¥89,669.39）的支付事实退化为 orphan、664 笔 ¥91,290.42 成功退款无法回溯原单，淘系“退款匹配率 8.1%”对抖音同指标 95.8%。本轮未改判（属任务书规定的口径），但**淘系净支付/GMV 目现阶段不可与抖音直接相加比较**；同时它令 619 个商业单的售后补拉集合永不收敛（§1.5）。
6. **但“可用”尚未达成**：metrics 层的 `_ENTITY_SOURCES` 仍只登记 `erp.trade.list.query`，实测 12 家淘系店任何指标查询都返回 `missing_data`，与抖音店混合查询时还会连带拖死抖音部分（§1.6）。同步侧接入已完成，查询侧多源登记是下一步的第一优先。

## 1. 实测结果（测试库 bi_agent_test@127.0.0.1:54329）

- 环境：worktree `taoxi-sync`，`.env`（凭证）+ `BI_WRITER_DSN=bi_sync@…`，`BI_SHOP_IDS` 为 12 家淘系店。
- 命令序列：`sync shops`（42 店幂等）→ `probe 2026-08-15..16`（166520，38 单）→ `backfill --days 30`（12 店）→ `incremental`（12 店）→ `reconcile --days 3`（12 店）。
- 并发说明：本任务期间同一 worktree 未被第二会话提交（已核实）；若后续多会话共用 worktree，写入均为幂等 upsert + `covered` 区间并，无双写脏数据，人工复核可用 batch_id 区分轮次。
- 全部门店 `last_error_code` 为空；三实体 `covered` 均为单一连续区间（未出现破碎多段，说明回填+增量+对账链路在 30 天内完整相接）。

### 1.1 每店订单/支付/售后规模

窗口：`backfill --days 30`（覆盖 2026-08-13 → 2026-09-12）+ `incremental` + `reconcile --days 3`；三实体（orders / aftersales_occurrence / aftersales_cohort）每店 `covered` 均为**单一连续区间** [08-13, 09-12]，`last_error_code` 全空，水位推进到增量运行终点。

| shop_id | 平台 | 店铺 | orders | payments | verified | verified% | 售后行 | 售后matched | 售后canonical | canonical金额(¥) |
|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|
| 166517 | tb | 宝威德滤纸直销 | 3437 | 3470 | 3211 | 92.5% | 259 | 40 | 226 | 10436.24 |
| 166520 | tb | 元发钉枪五金工具 | 1303 | 1379 | 1101 | 79.8% | 199 | 46 | 172 | 30109.93 |
| 166647 | tb | 沃金五金机电商城 | 617 | 657 | 539 | 82.0% | 84 | 24 | 73 | 27674.08 |
| 166650 | tb | 滤纸滤布工厂 | 424 | 429 | 396 | 92.3% | 28 | 2 | 27 | 1282.69 |
| 166684 | tb | 宝威德滤纸 | 474 | 478 | 439 | 91.8% | 38 | 3 | 37 | 4060.77 |
| 166685 | tb | 木工钉枪批发 | 246 | 254 | 218 | 85.8% | 33 | 5 | 29 | 3446.71 |
| 166693 | tb | 元发工具工厂 | 230 | 236 | 193 | 81.8% | 38 | 5 | 36 | 4555.18 |
| 167166 | tb | 装修吊顶工具 | 136 | 139 | 108 | 77.7% | 32 | 4 | 27 | 3487.39 |
| 186607 | tb | 沃金劳保用品企业店 | 898 | 899 | 897 | 99.8% | 0 | 0 | 0 | 0 |
| 900007148 | tb | 沃金数码商城 | 15 | 15 | 9 | 60.0% | 7 | 1 | 7 | 1386.80 |
| 166687 | tm | 沃金五金专营店 | 400 | 423 | 331 | 78.3% | 64 | 7 | 61 | 12211.77 |
| 900453539 | tm | 元发旗舰店 | 187 | 187 | 155 | 82.9% | 39 | 6 | 34 | 7576.94 |
| **合计** | | | **8367** | **8566** | **7597** | **88.7%**（¥810,511.40） | **821** | **143** | **729** | **¥106,228.50** |

注：`paid_at` 最小值早于窗口起点（166647 至 2026-07-02）——实测两个通道对 `timeType=pay_time` 的语义不一致：出库通道有 **83/8367（1.0%）** 行的 `paid_at` 早于 `lower(covered)`（最早早 42 天），而抖音 `erp.trade.list.query` 同口径为 **0 行**（见 §3、§5.3）。记录本身完整，但它们的支付事实落在 `covered` 之外。

### 1.2 状态与规范化分布

| 指标 | 值 |
|---|---|
| normalization_status | 全部 `normal`（8367/8367）；`needs_review`=0，`invalid`=0 |
| active | 7759（92.7%）；非活跃（TRADE_CLOSED 等）608（7.3%） |
| split_parent_id | 本窗口 0 行（实测该批无拆单命中；逻辑已由单测覆盖） |
| 逐日断层（08-14→09-11，29 天） | 11/12 店零订单天数 = 0（29/29 天有单）；900007148 沃金数码商城 9/29 天有单（全窗口仅 15 单的小店，最大相邻间隔 222.9h，非断档） |
| 其余 11 店最大相邻出库间隔 | 3.6h～26.0h（166517 3.6h / 166520 8.5h / 167166 26.0h 为最大值） |
| 抽查 5 单（166520，08-15） | 金额链路 payAmount=payment、cost、grossProfit=payment−cost−postFee 自洽；closed→active=false ✓ |
| quality_ok | 全部 false —— 与存量抖音店一致，`sync_state.quality_ok` 为人工质检占位列，同步代码从不写入，非新通道缺陷 |

注：§1.1 的「售后matched」列为**全部状态的 matched**（143）；若只看"退款成功且金额口径可用"（`platform_success AND refund_canonical`，729 条）中已匹配的子集，只有 **59 条（8.1%）**，详见 §1.3。

### 1.3 不活跃单（TRADE_CLOSED）对支付归集与退款匹配的实测影响

任务书假设 4 与 E 节要求 `status=TRADE_CLOSED → active=false`（已由 `tests/test_core.py` 固化）；本轮**未改判**，但实测出该判定在出库通道上的下游后果，必须留档：

| 项 | 淘系（出库通道，30 天窗口） | 抖音（`erp.trade.list.query`，同库对照） |
|---|---|---|
| 订单行 / 不活跃行 | 8367 / **608（7.27%）** | 9414 / **0（0.00%）** |
| 不活跃单的单头实付合计 | **¥89,669.39**（608 单全部 `paid_at` 非空且 `raw_pay_amount>0`） | — |
| 支付事实 | 8566 条：head+verified 7597（¥810,511.40）、**orphan 621（金额 NULL）**、undetermined 348 | 9787 条：head 9097（92.9%）、**orphan 0**、undetermined 690 |
| orphan 归因 | **621/621 全部**是"该商业单只存在于不活跃出库单中"（`_load_orders`/`refresh_aftersale_matched` 均带 `AND active`） | 0 |
| 成功+canonical 退款匹配率 | **59/729 = 8.1%**（未匹配 670 条，¥98,711.51） | 881/920 = **95.8%** |
| 未匹配退款归因 | 664 条（¥91,290.42）原单存在但只有不活跃出库单；6 条（¥7,421.09）窗口内查无出库单 | — |

口径后果（分三层，已实测，详见 §1.6）：对"付款后退款成功→交易自动关闭"这批单，现状是**支付事实被排除、退款事实照常计入** `reporting.v_shop_daily.refund_amount`（该 CTE 只过 `platform_success AND refund_canonical`，**不要求 `matched`**），于是 `cash_difference = 净支付 − 退款` 对该批订单单向扣减（少计 ¥89,669.39 支付、多计 ¥91,290.42 退款）。metrics 层则不会给出这个错数，而是直接拒答（未匹配退款硬门禁）。抖音通道因不产生不活跃行，历史上从未暴露此问题（09-06 复核亦未覆盖）。

同时：`verified` 比例 88.7%（淘系）对 92.9%（抖音）的差距中，约 10 个百分点由上述 621 条 orphan 造成；剩余为 348 条 `undetermined`——实测 162 张活跃出库单的行级 `raw_paid_amount` 合计**大于**单头 `raw_pay_amount`（无一例小于），按"禁止猜测"规则不写金额，属既有设计（抖音同口径 690 条）。

### 1.4 完整性与一致性检查（全 0 为通过）

| 检查 | 结果 |
|---|---|
| 淘系店被 `erp.trade.list.query` 双写（同店两通道） | 0 行（源路由隔离生效） |
| `bi.order_items` 淘系行数 / 无主明细行 | 9177 / 0 |
| 支付行无任何订单 `commercial_ids` 引用 | 0 |
| 同一 `commercial_id` 跨店重复 | 0 |
| 负金额支付 / `paid_at` 为空的订单 / 成功退款金额为空 / 退款缺 `commercial_id` | 0 / 0 / 0 / 0 |
| `split_parent_id` 非空行 | 0（本窗口无拆单命中） |
| `sync_state.covered` 分段数（38 行：12 店出库 orders + 13 店×2 售后实体） | **全部 1 段**（回填→增量→对账首尾相接，无破碎区间） |
| `last_error_code` | 全空 |
| PII 只读探针（166520，08-20→08-21，31 单/8 售后） | 原始出库响应非空 PII 键 5 个（`buyerNick/mobileTail/openUid/sellerNick/shopName`）、售后原始响应 3 个（`buyerName/buyerPhone/shopName`）；规范化结果与商品行、售后行中命中数**均为 0**，键集无漂移（`logs/pii_probe_taoxi.out`） |

### 1.5 衍生问题：`unmatched_commercials` 补拉集合永不收敛

`_incremental_shop`（L1147）与 `_reconcile_shop`（L1172）结尾都会调 `unmatched_commercials()` 找“售后已到、原单未到”的 cid，再逐个 `refetch_orders_for_commercials(tid=…)` 补拉。实测当前存量：

| 组 | 每轮补拉 cid | 其中“只有不活跃出库单”（永不可解析） | 其 `source_updated_at` 在 366 天补拉窗内 | 完全无单（可重试） |
|---|---:|---:|---:|---:|
| 淘系（11 店有售后） | **625** | **619（99.0%）** | 619（100%） | 6 |
| 抖音 | 45 | **0** | 0 | 45 |

机制：补拉能查到该单（服务端按 `upd_time` 命中），但 `apply_trade` 版本守卫严格 `>` → 同版本不写（`accepted` 不计数，jsonl 里看不出来）→ `refresh_aftersale_matched` 因 `o.active` 仍算出 `matched=false` → 下一轮再次入选。该集合**单调增长、永不排空**：每轮 incremental、每轮 reconcile 各至少 625 次单 tid 分页调用；淘系店越多、历史越长成本越高。抖音通道无此现象（45 个全部是“单未到”型，原单到达即收敛）。注：该集合含全部售后状态，不仅 §1.3 的 670 条成功退款（对应售后行：淘系 678 / 抖音 52）。

### 1.6 metrics 层实测：淘系店当前完全不可查，修好后还有两道门禁

用真实 reader 角色 + `query_business` 直查测试库（`logs/probe_metrics_taoxi.py`，窗口 08-13→09-06）：

| 查询（指标 paid/refund/cash） | 结果 |
|---|---|
| 对照：抖音店 166754 | **status=ok**，cov=complete，paid ¥138,089.05，refund ¥27,511.53 |
| 12 家淘系店 | **missing_data**，cov=partial，limitations：覆盖未完成 + 数据截止未知（回填未完成） |
| 混合（166754 + 12 家淘系） | **missing_data** —— 抖音那部分也被连带拖死 |

原因（代码定位）：`metrics.py` L56-60 `_ENTITY_SOURCES["orders"] = "erp.trade.list.query"` 是**单源常量**，而淘系店只有 `erp.trade.outstock.simple.query` 的 `sync_state` 行（`logs/q12.sql`：12 店 has_tradelist_state 全为 f）→ `_coverage_for` 查不到行 → status=missing、data_as_of=None。因此：

1. **前置必做（否则本轮接入对 Agent/页面零收益）**：`_ENTITY_SOURCES` 需改为每实体可多源（按平台解析，或取各源 covered 交集），并处理混合查询语义。本轮红线为"不改 metrics"，未动。
2. **第二道门禁（第 1 点修好后才暴露）**：`_UNMATCHED_SQL`（L302-307）对 `refund_amount` / `cash_difference` / `cohort_refund_rate` 是**硬拒答**——窗口内只要存在 1 条未匹配的平台成功退款就整体 missing_data。实测同口径计数：淘系 **659/717 未匹配**，抖音 **0/255** → 不先解 §1.3 的口径，淘系退款类指标几乎必然不可查。
3. **`paid_amount` 不在门禁清单里** → 它是唯一会**静默给出偏小数字**的指标（621 条 orphan 对应 ¥89,669.39 不计入）；直接查 `reporting.v_shop_daily` 的下游同理，视图本身不要求 `matched`。

## 2. 实施要点与踩坑记录

1. **分页复用**：出库通道直接复用 `_fetch_orders_cursor` / `_fetch_orders_paged`，仅把 method 参数化；响应形状 `{pageNo, pageSize, total, list}` 与交易查询一致。
2. **增量语义**：`timeType=upd_time + queryType=0`；回填 `timeType=pay_time` + queryType 0/1 双通道（3 个月内外），窗口 ≤1 天，与抖音通道相同逻辑。
3. **`split_parent_id` ← `splitSid`**：出库接口无 `splitParentId` 字段；`splitSid` 为"拆单主单 sid"，`-1`/空视为无拆单（已实测本窗口全部无值，逻辑按文档处理）。
4. **回填水位（本次修正）**：旧实现在回填开始前取 `t1 = now`，且 12 店共用同一时间戳，回填结果不落水位（data_as_of 有值、watermark 保持 epoch）。改为每店回填写完后取 `t1 = now`：回填窗口以 `pay_time` 分片不产生 upd_time 口径的连续覆盖，水位不动；随后的 ≤1 天修改时间扫描（queryType 0/1）落入 `covered` 并把水位推进到扫描窗口终点（实测水位 = 扫描窗口终点，见 §1.1 注）。
5. **出库 `orders[]` 无 `skuOrderId`**：商品行回退使用 `skuId` 作为 `sku_order_id`，保证抖音"同 skuSysId 多行"的邮费分摊判别对淘系同样成立（淘系该键常空 → 单组 → 全额邮费记主组）。
6. **出库响应 `created` 可能为 null**：缺 `payTime` 时按缺时间判 invalid，不依赖 created。
7. **PII 红线（守护用例固化）**：`PII_FORBIDDEN_FIELDS` 共 24 项：收件人 10（receiverName/Phone/Mobile/Address/State/City/District/Street/Zip/Country）+ 买家 4（buyerNick/buyerMessage/buyerName/buyerPhone）+ 发票 4（invoiceName/Remark/Kind/tradeInvoice）+ taobaoId/ptConsignTime + 出库响应实际携带的 shopName/sellerNick/openUid/mobileTail；规范化入口命中即整单 invalid 且不写日志；表结构本身无这些列，测试防止未来扩列违约。**入库字段集合 = 现有列，一个未加**。
8. **金额单位为元**（探针 `payAmount="16.90"` 这类字符串），`to_decimal` 直接兼容；分/元换算仅适用于推广费用接口，与订单无关。
9. **抖音 `erp.trade.list.query` 排除淘系订单**（文档明示），因此淘系唯一非敏感订单通道是出库接口；奇门/方舟敏感通道不在范围内。

## 3. 与 09-06 复核的差异/冲突

- 09-06 快照中出库响应 `list[]` 仅示例性列了 8 个头字段；本次实测单头 82 键，`status/sysStatus/userId/shopName/orders[]` 均非空，以实测为准。
- **假设 4「TRADE_CLOSED→active=false 已兼容」实测不成立**：判定本身按任务书实现且有单测，但在出库通道上它使 608 张已付款出库单的支付事实退化为 orphan、并使 664 笔成功退款无法回溯原单（见 §1.3）。属口径决策而非缺陷，故本轮不改判，转为人工复核点（§5）。
- **`timeType=pay_time` 语义两通道不一致（实测）**：回填/对账同一段代码、同一参数，抖音通道返回行的 `paid_at` 全部落在 `covered` 内（0/9414）；出库通道有 83/8367（1.0%）行 `paid_at` 早于 `covered` 起点（最早 2026-07-02，早 42 天），即该接口对付款时间参数实际按其自身时间字段（出库/发货）裁剪，文档声称的“按付款时间”不成立。未改代码（无法从响应内证明哪条时间字段主导，猜口径会污染覆盖区间），只在 §5.3 标出口径后果。抽样时预期行数应按 `paid_at` 分布而非窗口天数估计。
- 其余（淘系敏感字段缺失、售后可用、店铺/商品/库存不分平台）与 09-06 结论一致。

## 4. 证据与追溯

- 代码：`bi_agent/sync.py`（`ORDER_SOURCE_BY_PLATFORM`、`_shop_order_source`、`normalise_trade(..., source=)`、`_fetch_orders_cursor/_fetch_orders_paged(method=)`、PII_FORBIDDEN_FIELDS 红线、回填水位修正），提交 `1e2b119` / `03d7713` / `161b3aa`。
- 测试：`tests/test_core.py`（源路由、出库规范化、PII 守护）、`tests/test_db.py`（出库落库 + 双通道状态隔离），`uv run --with pytest --env-file .env.test python -m pytest tests/ -q` 全绿（104 passed, 12 subtests passed）。已知空白：`unmatched_commercials` / `refetch_orders_for_commercials` 无任何用例（§1.5 因此长期未被发现）。
- 官方文档快照：`D:\Projects\bi-agent\logs\kuaimai-llms-full-fresh.txt` §销售出库查询（L11182 起）、§售后工单查询（L15575 起）。
- 只读探针（2026-09-12）：`D:\Projects\bi-agent\logs\probe_tb.py` / `probe_tb2.py` / `probe_tb3.py`；PII 白名单探针 `logs/probe_taoxi_pii.py` → `logs/pii_probe_taoxi.out`。
- 运行日志（本 worktree `logs/`，git 忽略）：`backfill_taoxi_30d.jsonl`（12 店逐店 orders/aftersales/cohort 计数与 `order_source` 路由证据）、`incr_taoxi.jsonl`、`recon_taoxi.jsonl`、`sync.log`（4356 次上游调用）。注：`backfill_taoxi_30d.err` 记录的是一次 `BI_SHOP_IDS` 缺失导致的启动失败（未配置环境，立即退出、未写库），成功重跑即上述 jsonl。
- 验证 SQL：`logs/verify_taoxi.sql`、`logs/taoxi_v1.sql`（§1.1～§1.2 与覆盖/断层）、`logs/taoxi_v2.sql`（§1.4 完整性）、`logs/taoxi_v3.sql`（§1.3/§1.4 归因与覆盖分段）、`logs/taoxi_v4.sql`（§1.5 补拉集合）、`logs/q10.sql`（timeType 通道对照）、`logs/q12.sql`（source 行存在性与未匹配计数）、`logs/q13.sql`（对照店覆盖区间），输出同名 `.out`；`logs/probe_metrics_taoxi.py` 为 §1.6 的 metrics 只读探针。

## 5. 未尽事项 / 人工复核点

1. **【需决策】出库通道 `TRADE_CLOSED` 的活动性口径（§1.3）**：现口径使 ¥89,669.39 支付事实退化为 orphan、670 笔 ¥98,711.51 成功退款不可回溯，淘系净支付被单向扣减。三个候选：
   - A 保持现状，在视图/页面口径说明中声明"淘系净支付不含付款后退款关闭单，且退款仍全额扣减"（零改动，但指标有系统性偏差）；
   - B 出库通道改判"`paid_at` 非空且 `raw_pay_amount>0` 即活跃"（`sysStatus=CANCEL` 仍不活跃），抖音通道不变；
   - C 保留 `active=false`，但让 `refresh_aftersale_matched`（及支付归集）允许关联不活跃单——只修匹配率，不改 GMV。
   **任一改动都需要历史重述通道**：`apply_trade` 的版本守卫是严格 `>`（`test_replay_same_version_is_idempotent` 固化），CLI 幂等重放/`replay` 都不会重写同版本行，故改判后既有 608 行不会自愈，而 `DELETE`/手工 `UPDATE` 均越出本轮红线。建议由口径负责人决定后再排"同版本重述"专项。
   另：§1.5 的补拉集合与此决策绑定——B/C 任选一项都会使 619 个 cid 自动收敛（matched 可算出 true）；**选项 A 不修复该集合**，需额外给 `unmatched_commercials` 加终止条件（例如“已存在同 cid 行即视为已解析”），否则每轮白跑 ≥625 次上游调用。
2. **淘系 `verified` 88.7% 的剩余缺口**：348 条 `undetermined` 源于 162 张单"行级实付合计 > 单头实付"（优惠/运费分摊口径差），需与业务确认应以单头还是行级为准。
3. **`timeType=pay_time` 在两个通道语义不一致（§3）**：回填/对账用 `timeType=pay_time` 分片。抖音通道严格（0/9414 行越界），出库通道不严格（83/8367 行 `paid_at` 早于 `covered` 起点，最早 2026-07-02，早 42 天）。后果：这些行的支付事实落在 `covered` 外，查询该更早区间时 metrics 会按覆盖率返回 partial/missing（设计行为，非数据丢失），但回填行数预估不能按窗口天数线性推。需确认是否补一段 `covered_from = min(paid_at)` 的存量重述（同类：需重述通道）。
4. **【前置必做】metrics 层不识别出库通道，12 家淘系店当前完整不可查（§1.6）**：`_ENTITY_SOURCES` 单源常量使所有淘系查询返回 missing_data，且与抖音店混合查询时**连带拖死抖音部分**。需改为每实体多源（按平台解析或取 covered 交集）并补测；同时注意修好后第二道门禁会立刻把淘系退款类指标打成 missing_data（现窗 659 条），所以这一项必须与 §5.1 的口径决策同批排期。另：`tests/test_db.py` 对 `unmatched_commercials` / `refetch_orders_for_commercials` **零覆盖**（只有 `mark_refund_canonical` 有独立用例），改该路径时需一并补收敛用例（`_seed_shop(platform="tb")` 可直接复用）。
5. ~~拼多多订单需方舟 appkey，未接。~~ **2026-09-12 已决定不接入拼多多支付（授权成本过高，不再作为待办前置）**，见 [范围决定](2026-09-12-drop-pdd-onboarding.md)；pdd 仅保留单据口径。1688（`1688`/`alibabac2b`→`alibabac2m`）本次范围外。
- 页面/Agent 侧对 12 家淘系店的开放与否是后续人工决定（前提：先完成 §5.4 的 metrics 多源登记）。
- 长尾回填：出库 queryType=0 仅覆盖近 3 个月，更早订单需要 queryType=1 归档窗口回填（本次 30 天范围内未受影响）。
- 建议排期：每日 `incremental`（orders×2 通道 + aftersales occurrence/cohort），每周 `reconcile --days 3`，每月 `replay --start <月末-40d> --end <月末>` 清理退款迟到/补发/换货。
