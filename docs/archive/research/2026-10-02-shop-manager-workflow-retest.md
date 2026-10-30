# 店铺经营者日常工作流复测记录（2026-10-02，修复后）

## 1. 目的

在 `26a93e3`（fix: allow s3 closed-refunded disclosure through state and artifact
validation）合入后，以店铺经营者身份重跑一次 18 问日常工作流，做两件事：

1. **缺陷修复的真实数据复核**：首测（[2026-10-01 记录](2026-10-01-shop-manager-daily-workflow-offline-test.md)
   §5.1）中 W01 类"所有店铺最近7天支付金额"在真实 tb/tm 数据上必崩
   （`classify_result` 抛 `unsafe_persistence_payload` → FAILED/unavailable）；
   本轮同形问题应走完整链并成功出数，且 S3 披露文本落进 Artifact。
2. 重新覆盖四张已启用状态图的节点链与终态，并补上首测缺的
   commerce_performance **SUCCEEDED** 全链。

环境与边界与首测一致：本机隔离测试库 `bi_agent_test`（真实同步数据至
2026-10-02 凌晨）、`127.0.0.1:8001` 隔离实例、**离线脚本化桩模型**（未调用
真实模型）、授权 22 店（20 ERP + 2 拼多多负向边界）。未执行：真实模型、
真实来源/平台 API、生产操作。测试会话与 `expected_listing_rosters` /
`price_audit_expectations` 残渣已清理（两表回到 0 行）。

## 2. 结果总表（18 问，断言 18/18 通过）

| # | 问题 | 领域 | 终态 | 相对首测 |
| --- | --- | --- | --- | --- |
| R01 | 所有店铺最近7天的支付金额，按店铺分列 | business_query | **succeeded**（全链 7 事件 + Artifact） | 首测同形 W01 **failed** → 修复生效 |
| R02 | 沃金五金机电商城最近7天支付金额 | business_query | **succeeded** | tb 单店成功路径首次走通 |
| R03 | 沃金五金机电商城最近7天每天的支付金额走势 | business_query | **succeeded** | — |
| R04 | 沃金五金机电商城9月20日至27日比前7天 | business_query | **succeeded** | 覆盖上期披露文本变体 |
| R05 | 沃金五金机电商城2025年11月支付金额 | business_query | missing_data | 无覆盖不补零，一致 |
| R06 | 支付金额是多少？（新会话） | business_query | needs_input | 缺期间反问，一致 |
| R07 | 各店铺最近7天保温钉枪-元发的销量排行 | business_query | **succeeded**（3 分区 Top-N） | 见 §4.1 问法观察 |
| R08 | 查宝威德官方旗舰店支付金额 | business_query | failed（`forbidden`） | 越权一致 |
| R09 | 拼多多店铺最近7天支付金额 | business_query | missing_data | PDD 永久边界一致 |
| R10 | 保温钉枪-元发在抖音店铺近七天卖得怎么样 | commerce_performance | **succeeded**（12 节点全链 + metric_result + trend_series） | 首测 commerce 只有 partial，本轮补齐 SUCCEEDED |
| R11 | 钉枪最近七天卖得怎么样 | commerce_performance | needs_input | 多候选反问一致 |
| R12 | 比较一下抖音各店铺近七天支付金额 | commerce_performance | partial | 见 §4.2 |
| R13 | 淘宝各店铺近七天支付金额比较 | commerce_performance | missing_data | consigned_at 完整性门禁一致 |
| R14 | 保温钉枪-元发标价25.80 复核 | listing_price_audit | missing_data | 来源未核验边界一致（roster 残渣已清） |
| R15 | 全商品总库存和店铺可售 | inventory_watch | missing_data | 快照无行一致 |
| R16 | 假设10月销售10万、费率12%，最多花多少 | evaluate_promotion | ok（`spend_cap=12000.00`，basis=用户输入假设） | 一致 |
| R17 | 最近7天实际推广费率和ROAS | evaluate_promotion | missing_data | 无实耗拒答一致 |
| R18 | 元发钉枪五金工具支付金额（新会话） | 主层澄清拦截 | 无运行记录 | 同名店一致 |

终态覆盖：business_query 4/5（succeeded×5、missing_data×2、needs_input、failed；
PARTIAL 仅可来自分区排行的 ok+partial，本轮分区完整未触发）、commerce 4/5
（**succeeded**、partial、needs_input、missing_data）、listing 与 inventory 各自
missing_data 全链。五种终态在四张图上整体齐备。

## 3. 修复复核证据（真实数据）

- R01/R02/R04 终态 `succeeded`，事件链
  `resolve_parameters→validate→authorize→execute→classify→persist→finalize` 完整，
  `metric_result` Artifact 落盘；运行状态 `limitations` 含 `closed_refunded_excluded`
  （修复前正是这个码让状态校验整体拒绝）。
- Artifact 披露文本逐字出现，两条新正则都在真实数据上命中：
  - 本期（R02）：`支付口径已剔除关闭后退款单的已核验支付15笔/2984.36元；退款单独列示，支付额−退款额为期间收支差额（不是净支付，也不是净利润）`
  - 上期（R04）：`上期支付口径已剔除关闭后退款单的已核验支付18笔/4390.22元（本期与上期对比按同一剔除口径）`
- R01 全店口径：20 店按店铺分列各带口径，披露"16 家店铺的结果来自未认证付款时间口径，
  按可观测样本披露"+ 收入未归属 + S3 剔除三件套齐备；覆盖 complete 无缺口。

## 4. 本轮观察

### 4.1 P2：口语"近七天"不在日期解析词典，静默继承上一轮窗口

R07 首轮用口语"**近七天**"提问，`resolve_period_detail` 返回 None（词典收
"最近7天"），于是 `RESOLVE_PARAMETERS` 按设计回退到会话筛选——继承了 R05 的
2025 年 11 月窗口，20 店全部"覆盖不足"被排除成 missing_data。改用"最近7天"
后正常解析为 [09-25, 10-02) 并成功。直接调用实证：

```
'各店铺近七天保温钉枪-元发的…'  -> None
'各店铺最近7天保温钉枪-元发的…' -> ('relative', 2026-09-25, 2026-10-02)
```

这不是回归（首测 W18 同文本"成功"只是继承了恰好同值的上一轮窗口，被巧合掩盖），
属确定性日期词典的收词缺口：建议后续把"近N天/近半月"等口语变体补进词典，或在
继承窗口而非本轮明示时于回答中显式提示"统计期间沿用上一轮 X–Y"。**不动代码**，
留待所有者决策。

### 4.2 R12 partial 的原因（合规输出）

fxg 支付口径的店铺对比里，`sales_amount` 的定义是 ERP 有效销售父项口径，支付口径
下只能按支付面出列 → 该指标合计拒答（`comparison_total_withheld`）、图表无可画
集合（`comparison_chart_unavailable`）→ 终态 partial、表格逐格带原因。双口径
契约（S1）按设计运行。

### 4.3 库内残渣处理

R14 上架复核会在真库写 `expected_listing_rosters` / `price_audit_expectations`
各 22 行，而 `test_operator_workflows` 的"本轮不写依据/不落库"用例以两表 0 行为
前提（首测残渣曾让提交前门禁红了 2 例）。本轮结束后已把两表清回 0 行、删除测试
会话（复测会话 ID：main `98314601-b861-4988-a402-506cad5d9ef1`、fresh
`45907f9e-1727-4b0f-999f-e70d667f473a`，已删）。

## 5. 结论

- **修复在真实数据上验证通过**：首测发现的 P1（成功支付查询崩溃）不复现，
  成功链路完整、披露码与两条披露文本按契约落盘；后端 1815 全量与离线验收 29/29
  已在提交 `26a93e3` 前跑绿（见提交记录）。
- 四张状态图节点链、事件顺序、终态分类与各类 fail-closed 边界（PDD、越权、
  同名澄清、来源未取证、覆盖缺口不补零）复测一致。
- 新增一条 P2 观察（§4.1 口语日期词典），未改代码，待所有者决策。
