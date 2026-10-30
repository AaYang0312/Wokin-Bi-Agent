# 快麦返回字段 ↔ 数据表列 覆盖核查

核查时间：2026-09-09（北京时间 19:44—20:05）。目的：回答「快麦 API 每项数据是否都有对应表项」。

## 0. 方法

- 官方公开全文 `llms-full.txt` 重新拉取，SHA-256 `492673dfa5996837dd40a70efa5d39809677663d557bbdecb224bd3c49fe7882`，与 2026-09-06 报告记录的快照**逐字节一致**，字段定义未变。
- 用生产同一签名客户端（`bi_agent/kuaimai.py`）发 **约 25 次只读请求**，覆盖 3 个已接入方法与全部分页通道（`upd_time` cursor、`pay_time` 主/归档、售后 `startModified` 与 `startPlatformCompleteTime`、店铺列表）。
- 样本量：订单头 774（另一轮 680/382）、订单行 794、套件子件 93、订单标签 1777、合单快照 22、售后工单 746、售后行 852、店铺 42。
- 逐字段与 `bi_agent/sync.py` 的 `normalise_trade`/`_normalise_item`/`normalise_aftersale`/`sync_shops` 映射及 `bi_agent_test` 库 `information_schema` 实际列比对；分类强制「零未分类」（脚本对未归类字段直接报错）。
- 输出只含字段名、记录数、非空计数与数值正/零/负计数，不含订单号、金额绝对值、客户信息、凭证。核查期间另有同步进程在写该库，行数增长与本次核查无关（本核查未发起任何写操作）。

## 1. 结论

**不是每项都有表项，且已映射的部分有失效项。** 真实返回的 336 个「层级.字段」项中：

| 分类 | 项数 | 占比 |
| --- | ---: | ---: |
| 有对应表列（含 2 个派生列） | 37 | 11.0% |
| 按设计显式不入库（买家/收件人信息） | 15 | 4.5% |
| **无任何表项** | **283** | **84.2%** |
| 表列存在但来源字段从不返回（列恒 NULL/恒常量） | 6 列 / 13 个字段名 | — |

无表项的 283 项按性质分布：套件子件明细 56、状态与标记 43、商品标识与文本 31、合单快照 29、金额与费用 23、时间 21、物流与仓储 20、其他 45、嵌套结构 8、拆合单标记 2。

其中绝大多数属第一版范围外（物流、仓库、打印状态、备注、图片），**不必**为其建表；下表 A/B 两类是需要处理的真实问题。

## 2. A 类：有表项但映射失效（6 项，属实现缺陷）

| # | 表列 | 现状 | 证据 | 正确来源 |
| --- | --- | --- | --- | --- |
| A1 | `bi.orders.active` | **恒为 true** | 现规则比对 `status ∈ {cancel,trade_closed,...}`，实测 `status` 是平台码 `fxg_2/3/4/5`，命中 0/680；同期 `unifiedStatus=CLOSED` 占单量 12.6%、占支付额 20.0%，全部以 `active=true` 入库（核查结束时库内 9457/9457 active） | `unifiedStatus`/`sysStatus`（文档：CLOSED=交易关闭）；注意样本中 4 条 `sysStatus=CLOSED` 而 `unifiedStatus=FINISHED`，需先定优先级 |
| A2 | `bi.shops.enabled` | **恒为 true** | `state` 实测分布 3启用=33、1停用=8、4会话失效=1；现规则只排除字符串 `disable/deleted/0`，`"1"`/`"3"`/`"4"` 全部通过 → 库内 42/42 `enabled=true` | 应同时使用返回的 `active` 字段（实测 `state=1` 的 8 家 `active=0`，与 `state=3/4` 的 34 家 `active=1` 完全对应）；`metrics.py` 的停用店铺拦截当前形同虚设 |
| A3 | `bi.orders.split_parent_id` | 恒为 NULL | 读取 `splitParentId`，公开文档 0 处提及、实测 0/774 返回 | 真实字段是 `splitType`（-1未拆单/1拆单）+ `splitSid`（拆单时主单 sid）；本轮样本 100% 为 -1（未拆单），但字段可用 |
| A4 | `bi.order_items.platform_line_id` | 恒为 NULL | 读取 `platformOid`，文档 0 处、实测 0/794 | 平台子单号在 `orders[].oid`（实测 794/794 非空），现仅用作 `line_id` 兜底 |
| A5 | `bi.aftersales.system_completed_at` | 恒为 NULL | 读取 `systemCompleteTime`/`completeTime`，文档 0 处、实测 0/746；库内当前 1073 条售后 1073 条该列为 NULL | 真实字段 `finished`（毫秒时间戳，未结工单为 NULL；实测 340/393 非空） |
| A6 | `bi.order_items.line_kind` | 恒为 `'sale'` | 读取 `lineKind`，文档 0 处、实测 0/794；`giftNum` 实测 794/794 为 0 | 文档 `orders[].type`：0普通 1赠品 2套件 3组合 4加工，且明确「是否赠品看 `giftNum>0` 而非 `type`」→ 需同时落 `type` 与 `giftNum` 才能区分行性质 |

另有一处**潜在**（未在样本中发生，但规则不成立）：文档写明售后 `status`「2:未解决 9:已解决 10:已作废 11:已合并 12:解决中，**多个逗号隔开**」，而 `normalise_aftersale` 用 `isdigit()` 解析——出现 `"2,10"` 这类值时 `work_status` 会静默变 NULL，`platform_success` 的作废/合并排除随之失效；`12 解决中` 也未纳入判断。本轮样本 393 条全为单值（9/2/10），故尚未发生。`online_status`/`work_status` 本身入库正常（实测 7 种/3 种取值，与文档枚举一致）。

`bi.orders.raw_platform_payment` 是第 7 种情况：`platformPaymentAmount` **文档有**（且注明淘系/拼多多不返回）但本账号本样本 0/774 返回。该列在可见期内没有数据来源，应在 `docs/metrics.md` 标注「来源未证实」或暂不保留，而不是留一个恒空列。

## 3. B 类：整层数据被丢弃（结构性无表）

| 层 | 样本 | 字段数 | 丢弃后可见后果 |
| --- | ---: | ---: | --- |
| `aftersale.items` | 852 行 | 30 | 无法回答「退的是哪个商品、退几件、实收几件、行级退了多少」。`itemRealQty`/`receivableCount`/`goodItemCount`/`badItemCount`/`rawRefundMoney`/`refundMoney`/`sysItemId`/`sysSkuId` 全无表项。`metrics.md` 已声明「商品退款暂不开放」，但连承载列都没有 |
| `trade.orders.suits` | 93 行 | 56 | 套件子件成本/数量/金额全无表项。设计上「父行与子件不重复累加」是对的，但子件明细等于 0 留存；且父行 `itemSysId` 指向套件而非实际 SKU，`v_product_daily` 会把套件当单商品排行 |
| `trade.tradeTags` | 1777 行 | 4 | 订单标签（`tagName`/`remark`/`id`）无表项，无法按标签维度分析 |
| `trade.messageMemos` / `aftersale.messageMemos` | 22 / 191 | 27 / 2 | 文档标注为**合单信息**（含 `isMergeMain`）。实测 `trade.type` 含 `7`=合并订单 占 10/374；合单关系目前完全不可还原 |
| `trade.tradeExt` / `trade.tradeInvoice` | 382 / 382 | 0 / 0 | 本账号返回**空对象**。`tradeExt.currency` 是唯一币种来源 → 缺失使 `order_payments.currency='CNY'` 只是写死默认值，无来源核验 |
| 商品/库存/成本/采购/推广 | — | — | `item.list.query`、`erp.item.sku.list.get`、`stock.api.status.query`、历史成本、采购按范围未接入，无表；因此 `order_items.product_id`/`sku_id` 只有数字 ID，`v_product_daily` 与聊天回答无法给出商品名称（`orders[].title`/`sysTitle` 也未入库） |

## 4. C 类：有真实数值、无表项、且与既有口径直接相关

按 `docs/metrics.md` 已声明的口径，以下几项值得优先决定是否建列（其余归入范围外）：

- `trade.acPayment`（实测 774/774 正数）、`trade.totalFee`（774/774 正数）、`trade.discountFee`（774/774 当前为 0）、`trade.postFee`（5/774 正）、`trade.theoryPostFee`（296/774 正）、`trade.actualPostFee`（仅 1/774 返回）、`trade.packmaCost`/`saleFee`/`salePrice`（样本全 0）。这些正是报告里「实际运费/包材/平台扣费缺失，故 `grossProfit` 不能称净利润」的字段载体，现在既不入库也无从复算。
- `trade.status` 系（`sysStatus`、`unifiedStatus`、`stockStatus`、`isRefund`、`refundStatus`、`isExcep`/`excep`）：只留一个派生布尔，无法回答「多少单待发货/已关闭/异常」，也让 A1 的错误无法在库内被发现。
- 售后维度：`afterSaleType`（1/2/3/4/5，区分仅退款/退货退款/换货——影响退款率解释）、`goodStatus`（货物状态）、`reason`、`refundPostFee`（25/746 正数，运费退款）、`refundWarehouse*`。
- 行级金额：`orders[].price`、`discountFee`、`divideOrderFee`、`totalFee`（均 794/794 有正数）——`allocated_paid_amount` 的交叉核验目前只能拿父行 `payAmount` 与单头 `payAmount` 比，没有第三条独立路径。

## 5. D 类：显式不入库（设计正确，无需建表）

`buyerNick`、`openUid`、`taobaoId`、`buyerMessage`、`receiverName/Mobile/Address/City/District/State/Street`、售后的 `buyerName`/`buyerPhone`/`wangwangNum`、发票子结构（本账号未返回）。共 15 项，与 `sql/001_init.sql` 中「买家字段一律不入库」一致。

## 6. 建议处置顺序

1. 修 A1/A2（影响已发布指标的口径与店铺范围校验），并把 `status`/`unifiedStatus`/`sysStatus` 原样入库以便复核派生规则。核查时库内 9457 条订单 `active` 全为 true、42 家店铺 `enabled` 全为 true。
2. 修 A3/A4/A5/A6 的字段名，或删列并在 `metrics.md` 标注不可用——不要保留恒空列。
3. 就 B 类中「售后行」「套件子件」决定是否建表；不建则在 `metrics.md` 的来源表里显式写「整层丢弃」，避免后续误认为已留存。
4. C 类金额字段先补 `trade.acPayment`/`totalFee`/运费族，为「ERP 毛利参考 → 更接近净利润」的对账留路径。
5. 明确币种来源缺失：在取得 `tradeExt.currency` 之前，`currency` 只能作为「按平台默认 CNY 的假设」标注。

## 7. 逐字段附表

字段名后不带数值；`金额/费用` 组中**加粗**表示该字段在样本里出现过非零值。

#### `trade`（样本 382 条，标量字段 87 项）

- 有表项：`cost` → bi.orders.raw_cost；`grossProfit` → bi.orders.raw_gross_profit；`modified` → bi.orders.platform_modified_at；`payAmount` → bi.orders.raw_pay_amount；`payTime` → bi.orders.paid_at；`payment` → bi.orders.raw_payment；`sid` → bi.orders.erp_id；`status` → bi.orders.active（派生）；`tid` → bi.orders.commercial_ids[]；`updTime` → bi.orders.source_updated_at；`userId` → bi.orders.shop_id
- 按设计不入库（买家信息）：`buyerMessage`、`buyerNick`、`openUid`、`receiverAddress`、`receiverCity`、`receiverDistrict`、`receiverMobile`、`receiverName`、`receiverState`、`receiverStreet`、`taobaoId`
- 无表项（状态/标记，18）：`deliverStatus`、`excep`、`expressStatus`、`isCancel`、`isExcep`、`isHalt`、`isHandlerMemo`、`isHandlerMessage`、`isPackage`、`isPresell`、`isRefund`、`isTmallDelivery`、`isUrgent`、`scalping`、`sellerFlag`、`stockStatus`、`sysStatus`、`unifiedStatus`
- 无表项（其他，12）：`belongType`、`convertType`、`itemKindNum`、`itemNum`、`netWeight`、`sellerMemo`、`shopName`、`shortId`、`source`、`sourceId`、`volume`、`weight`
- 无表项（物流/仓储，11）：`destId`、`expressCode`、`expressCompanyId`、`expressCompanyName`、`logisticsCompanyId`、`outSid`、`templateId`、`templateName`、`warehouseId`、`warehouseName`、`wlbTemplateType`
- 无表项（金额/费用，9）：**`acPayment`**、`actualPostFee`、`discountFee`、`packmaCost`、**`postFee`**、`saleFee`、`salePrice`、**`theoryPostFee`**、**`totalFee`**
- 无表项（时间，9）：`auditTime`、`consignTime`、`created`、`deliverPrintTime`、`endTime`、`expressPrintTime`、`ptConsignTime`、`threePlTiming`、`timeoutActionTime`
- 无表项（商品文本/标识，3）：`shortTitle`、`sysOuterId`、`type`
- 无表项（拆合单标记，2）：`splitSid`、`splitType`
- 无表项（嵌套结构，1）：`companyId`

#### `trade.orders`（样本 395 条，标量字段 64 项）

- 有表项：`cost` → bi.order_items.raw_unit_cost；`giftNum` → bi.order_items.gift_quantity；`id` → bi.order_items.line_id；`itemSysId` → bi.order_items.product_id；`num` → bi.order_items.quantity；`payAmount` → bi.order_items.raw_paid_amount + allocated_paid_amount；`payTime` → bi.order_items.paid_at；`payment` → bi.order_items.raw_payment；`skuSysId` → bi.order_items.sku_id；`tid` → bi.order_items.commercial_id
- 字段名不存在（列失效）：`oid` → bi.order_items.line_id（兜底，非平台行号）
- 按设计不入库（买家信息）：`taobaoId`
- 无表项（商品文本/标识，15）：`authorId`、`authorName`、`numIid`、`outerSkuId`、`picPath`、`shortTitle`、`skuId`、`skuPropertiesName`、`sysItemOuterId`、`sysOuterId`、`sysPicPath`、`sysSkuPropertiesName`、`sysTitle`、`title`、`type`
- 无表项（金额/费用，10）：**`acPayment`**、**`discountFee`**、**`discountRate`**、**`divideOrderFee`**、`postFee`、**`price`**、**`priceDouble`**、`saleFee`、`salePrice`、**`totalFee`**
- 无表项（状态/标记，10）：`insufficientCanceled`、`isCancel`、`isPresell`、`isVirtual`、`nonConsign`、`refundStatus`、`stockStatus`、`sysConsigned`、`sysStatus`、`unifiedStatus`
- 无表项（其他，6）：`forcePackNum`、`netWeight`、`refundId`、`source`、`stockNum`、`volume`
- 无表项（时间，5）：`consignTime`、`created`、`endTime`、`estimateConTime`、`ptConsignTime`
- 无表项（拆合单/嵌套明细，4）：`combineId`、`modified`、`status`、`updTime`
- 无表项（嵌套结构，2）：`companyId`、`sid`

#### `trade.orders.suits`（样本 49 条，标量字段 56 项）

- 无表项（套件子件明细，56）：`combineId`、`companyId`、`consignTime`、`cost`、`created`、`discountFee`、`discountRate`、`endTime`、`estimateConTime`、`forcePackNum`、`giftNum`、`id`、`insufficientCanceled`、`isCancel`、`isPresell`、`isVirtual`、`itemSysId`、`modified`、`netWeight`、`nonConsign`、`num`、`numIid`、`oid`、`outerSkuId`、`payAmount`、`payTime`、`payment`、`picPath`、`price`、`priceDouble`、`ptConsignTime`、`refundStatus`、`saleFee`、`salePrice`、`sid`、`skuId`、`skuPropertiesName`、`skuSysId`、`source`、`status`、`stockNum`、`stockStatus`、`sysConsigned`、`sysItemOuterId`、`sysOuterId`、`sysPicPath`、`sysSkuPropertiesName`、`sysStatus`、`sysTitle`、`taobaoId`、`tid`、`title`、`totalFee`、`type`、`updTime`、`volume`

#### `trade.orders.orderExt`（样本 395 条，标量字段 3 项）

- 无表项（嵌套结构，2）：`sid`、`tid`
- 无表项（时间，1）：`promiseAcceptTime`

#### `trade.tradeTags`（样本 1777 条，标量字段 4 项）

- 无表项（其他，3）：`id`、`remark`、`tagName`
- 无表项（商品文本/标识，1）：`type`

#### `trade.messageMemos`（样本 22 条，标量字段 27 项）

- 无表项（合单快照，27）：`acPayment`、`actualPostFee`、`addressChanged`、`consignTime`、`created`、`discountFee`、`grossProfit`、`isHandlerMemo`、`isHandlerMessage`、`isMergeMain`、`isUpload`、`payAmount`、`payTime`、`payment`、`postFee`、`ptConsignTime`、`saleFee`、`sellerFlag`、`sellerMemo`、`sid`、`sysConsigned`、`sysStatus`、`theoryPostFee`、`tid`、`totalFee`、`unifiedStatus`、`userId`

#### `aftersale`（样本 200 条，标量字段 48 项）

- 有表项：`id` → bi.aftersales.aftersale_id；`modified` → bi.aftersales.source_updated_at；`onlineStatus` → bi.aftersales.online_status；`platformCompleteTime` → bi.aftersales.platform_completed_at；`platformId` → bi.aftersales.platform_refund_id；`rawRefundMoney` → bi.aftersales.raw_platform_amount；`refundMoney` → bi.aftersales.raw_system_amount；`sid` → bi.aftersales.erp_id；`status` → bi.aftersales.work_status；`tid` → bi.aftersales.commercial_id；`userId` → bi.aftersales.shop_id
- 按设计不入库（买家信息）：`buyerName`、`buyerPhone`、`wangwangNum`
- 无表项（状态/标记，12）：`advanceStatus`、`advanceStatusText`、`afterSaleType`、`destWorkOrderStatus`、`extraWarehouseStatus`、`goodStatus`、`handlerStatus`、`handlerStatusText`、`onlineStatusText`、`orderType`、`platformStatus`、`storageProgress`
- 无表项（物流/仓储，9）：`refundExpressCompany`、`refundExpressId`、`refundWarehouseCode`、`refundWarehouseId`、`refundWarehouseName`、`tradeOutSid`、`tradeWarehouseCode`、`tradeWarehouseId`、`tradeWarehouseName`
- 无表项（其他，6）：`dealResult`、`reason`、`shopName`、`shortId`、`source`、`sourceId`
- 无表项（时间，4）：`afterSaleApplicationTime`、`applyDate`、`created`、`finished`
- 无表项（嵌套结构，2）：`companyId`、`explains`
- 无表项（金额/费用，1）：**`refundPostFee`**

#### `aftersale.items`（样本 228 条，标量字段 30 项）

- 无表项（商品文本/标识，11）：`mainOuterId`、`numIid`、`outerId`、`picPath`、`propertiesName`、`skuId`、`suite`、`sysItemId`、`sysSkuId`、`title`、`type`
- 无表项（其他，10）：`badItemCount`、`goodItemCount`、`id`、`itemRealQty`、`itemSnapShotId`、`rawRefundMoney`、`receivableCount`、`refundMoney`、`suiteRatio`、`suiteType`
- 无表项（状态/标记，3）：`isGift`、`isMatch`、`suiteSingle`
- 无表项（金额/费用，3）：**`payment`**、**`price`**、**`refundableMoney`**
- 无表项（拆合单/嵌套明细，1）：`combineId`
- 无表项（时间，1）：`receiveGoodsTime`
- 无表项（嵌套结构，1）：`tid`

#### `aftersale.messageMemos`（样本 191 条，标量字段 2 项）

- 无表项（合单快照，2）：`sellerFlag`、`sellerMemo`

#### `shop`（样本 42 条，标量字段 15 项）

- 有表项：`nick` → bi.shops.display_name（兜底）；`source` → bi.shops.platform；`state` → bi.shops.enabled（派生）；`title` → bi.shops.display_name；`userId` → bi.shops.shop_id
- 无表项（其他，8）：`active`、`deadline`、`externalName`、`name`、`remark`、`sendContactId`、`shopId`、`shopLabel`
- 无表项（时间，1）：`created`
- 无表项（商品文本/标识，1）：`shortTitle`


---

# 复验：修复提交 `888f04e fix: repair kuaimai field mappings`（2026-09-09 20:35 之后）

复验时间：2026-09-09 21:20—21:45（北京时间）。方法与首轮一致：官方文档快照未变（同一 SHA-256）；用生产签名客户端发只读请求（订单 1200 单/近 7 天修改时间、售后 392、店铺 42）；把真实样本送进修复后的 `normalise_trade`/`_normalise_item`/`normalise_aftersale`/`_source_bool` 看派生结果；对**一次性空库** `bi_agent_check_test`（`001_init.sql` 新建，验毕已 `DROP`）跑 `tests.test_db`/`tests.test_api`；对真实库 `bi_agent_test` 只做事务内验证并回滚。真实库复验后仍为 9457 订单 / 9936 行 / 1073 售后 / 42 店铺，无 `CHK*` 残留、无新列，未被改动。

## 8.1 首轮 A 类 6 项的复验结果（代码层全部成立）

| # | 复验证据 | 判定 |
| --- | --- | --- |
| A1 `active` | 681 单真实样本 → inactive 87 单（12.8%）；`unifiedStatus`/`sysStatus` 原样入库；两状态不一致 4 条按 unified 优先（规则确定） | 已修 |
| A2 `shops.enabled` | 42 家 → `active=0/state=1` 的 8 家 `enabled=false`，34 家 true；`metrics.py` 增加「部分停用缩小范围、全部停用拒绝出数」 | 已修 |
| A3 `split_parent_id` | 改读 `splitType==1` 时的 `splitSid`；但真实样本 **681/681 均 `splitType=-1`**，无一条真实拆单可验，仅合成用例通过 | 代码已修，真实数据未验证 |
| A4 `platform_line_id` | `orders[].oid` → 705/705 非空（首轮为 0/794） | 已修 |
| A5 `system_completed_at` | 改读 `finished` → 338/392 非空（首轮 0/746） | 已修 |
| A6 `line_kind` | `orders[].type` → `source_type` 入库；行性质 sale 1205 / suite 42；`giftNum>0` 判赠品（本批 0 条） | 已修 |
| 潜在雷（多值 `status`） | 新增 `_source_statuses` 按逗号拆分，含 10/11 一律不判成功；`work_values` 为空也不再判成功 | 已修 |

覆盖率随之变化：真实返回 336 个「层级.字段」项中，有表项 **37 → 45**（11.0% → 13.4%），无表项 283 → 276；「响应字段名不存在导致列失效」由 1 项降为 **0**；恒 NULL 列只剩 `bi.orders.raw_platform_payment`（来源 `platformPaymentAmount` 文档有、本账号从不返回），已在 `docs/metrics.md` 显式标注，处理正确。

测试：`tests.test_core` 62 全过；`tests.test_db` 46 全过、`tests.test_api` 5 全过（需 `BI_TEST_ADMIN_DSN` + `BI_TEST_READER_DSN`）。**注意**：本机没有 `.env.test`，直接跑会得到 `49 skipped`——新增的 8 个映射用例在默认环境下其实一条都没执行，skip 不是通过证明。

## 8.2 阻塞项：迁移没有执行，同步现在会直接失败

真实库 `bi.orders` 没有 `unified_status`/`system_status`，`bi.order_items` 没有 `source_type`。在同一库上以事务方式调用修复后的 `apply_trade`+`apply_aftersale`，实测：

```
[a) 未迁移库] 失败 UndefinedColumn: column "unified_status" of relation "orders" does not exist
[b) 事务内临时执行 002 后] apply_trade=True apply_aftersale=True
    orders  : active=False unified_status=CLOSED split_parent_id=CHK-ERP-PARENT
    items   : platform_line_id=7001 source_type=2 line_kind=suite active=False
    aftersale: system_completed_at非空=True platform_success=True
[回滚确认] unified_status 列仍存在？False；CHK 行残留？0
```

即：**下一次 `incremental`/`backfill`/`replay` 一跑就会窗口失败回滚**（保留旧水位，不会污染数据，但同步事实上停摆）。`002_kuaimai_mapping_repair.sql` 无任何代码或测试引用，也没有启动期 schema 校验，只靠 `docs/runbook.md` 的一句「先迁移再部署」。建议补一条断言（如启动时检查 `information_schema` 必需列，缺失即拒绝同步并报 `schema_outdated`），否则「代码已合并、库未迁移」这种状态会静默存在到下次同步。

## 8.3 修复引入的两处口径副作用（需要决策，不是代码 bug）

**(1) 关闭单被排除出支付、其退款仍计入 → 双重扣减。** 一次性库实测同一笔业务（100 元支付、30 元退款成功）：

| unifiedStatus | v_shop_daily | order_payments |
| --- | --- | --- |
| FINISHED | paid=100 单数=1 退款=30 收支差=**70** | amount=100 verified=true basis=head |
| CLOSED | paid=0 单数=0 退款=30 收支差=**−30** | amount=None verified=false basis=orphan |

`rebuild_payments`/`_load_orders` 都带 `AND active`，所以关闭单的支付被整体剔除，而退款侧仍按 `platform_success` 聚合。真实样本量级：近 7 天 1200 单中 CLOSED 116 单、占支付额 **16.47%**（另一轮 681 单占 20.0%）。replay 之后 `paid_amount`、`paid_orders`、`aov` 会一次性掉约 16%，`cash_difference` 再重复扣一次同方向的退款。二选一：甲、支付口径保留已付款关闭单（`active` 只用于「有效单量」类），乙、排除关闭单的同时排除其对应退款。当前实现是两者混合。同批退款率不受影响（分母只用 verified 支付）。

**(2) 套件/组合/加工行从商品日聚合消失。** `line_kind` 一旦产出 `suite`/`combination`/`processing`，`reporting.v_product_daily` 的 `WHERE line_kind='sale'` 就把它们全部排除，而 `orders[].suits` 子件表按本期决定不建。真实样本：42/1247 行，但占**行级金额 15.41%**；`test_closed_order_has_no_product_daily_row` 也把「无行」当成了期望行为。结果修复前这些商品参与排行（口径混浊但可见），修复后在商品排行里彻底不存在。需要决定：视图纳入非 sale 行并单列标注，或先建子件表再排除父行；否则 `docs/metrics.md` 应显式写「套件/组合/加工行的金额不进商品排行」。

## 8.4 首轮 B/C 类保持原状（属显式不做什么）

`aftersale.items`(30) / `orders[].suits`(56) / `tradeTags`(4) / `messageMemos`(27+2) / 商品名称与档案 / 库存 / 历史成本 / 采购 / 推广仍无表项；`acPayment`、`totalFee`、`discountFee`、`postFee`、`theoryPostFee`、`packmaCost`、`saleFee` 等 23 项金额/费用仍无列，「ERP 毛利 → 更接近净利润」的对账路径仍未铺。提交说明显式「不创建售后行或套件子件等第二期明细表」，与首轮建议第 3 条一致，但 `metrics.md` 尚未把「整层丢弃」写清。

## 8.5 复验后待办（按优先级）

1. 执行 `002_kuaimai_mapping_repair.sql` → `sync shops` → `replay --entity orders` → `replay --entity aftersales_occurrence`；并加启动期 schema 断言，防止「代码新库旧」静默停摆。
2. 就 8.3(1) 定支付口径，并把结论写进 `docs/metrics.md` 与相应用例（当前用例锁住的是「关闭单不进聚合」这一选择，未处理退款侧）。
3. 就 8.3(2) 定商品聚合口径（纳入非 sale 行并标注，或建 `suits` 子表）。
4. 找一个真实含拆单/合单的日期窗口复验 A3（当前只有合成用例，生产样本 0 条）。
5. 把 `tests.test_db`/`test_api` 需要的 `BI_TEST_ADMIN_DSN`、`BI_TEST_READER_DSN` 固化进 `.env.test`（或 CI），否则 46 条 DB 用例在默认环境恒为 skip。
6. 小项：`_commercial_ids` 仍读永不存在的 `tids`（死代码）；`parse_timestamp(0)` 会把未付款单写成 `1970-01-01` 的 `paid_at`，建议 0/空视为 None（本轮按 `status=WAIT_BUYER_PAY` 过滤只回到 2 条且 `payTime` 为正，风险未证实也未设防）。

---

# 第三轮复验：`f112040 fix: align kuaimai cash and product metrics`（2026-09-10 01:47 之后）

复验时间：2026-09-10 02:00—02:20（北京时间）。范围：迁移是否真执行、同步是否真重跑、上一轮 §8.2/§8.3 三项是否落地、是否引入新问题。验证手段：真实库只读查询；一次性库（`001_init.sql` 新建，跑完 `DROP`）跑 DB 测试与验收；旧 schema 一次性库实测守卫（跑完 `DROP`）；只读拉取样比对字段漂移。**真实库未被写。**

## 9.1 上一轮三项全部落地并有真实数据证据

| 上轮问题 | 处置 | 复验证据 |
| --- | --- | --- |
| §8.2 迁移未执行，同步会炸 | `002`+`003` 已执行，且新增 `assert_sync_schema()` 启动守卫 | 真实库列齐备；在按 `888f04e~1` 旧 `001_init.sql` 造的库上跑 `sync shops`/`sync incremental`，均 `exit=1 stderr=schema_outdated`，且在发 API 请求之前就退出 |
| §8.3(1) 关闭单双重扣减 | `_load_orders`/`_determine_payment`/行级交叉核对/`matched`/`unmatched_commercials` 全部改为 `active OR (paid_at IS NOT NULL AND raw_pay_amount > 0)` | 真实库 750 条 `unified_status=CLOSED` 全部 `raw_pay_amount>0`；其中 722 条支付事实 `basis=head verified=true`，仍按已收款计入 `paid_amount`，退款侧照旧 → 不再双扣。关闭单贡献已验证支付额 94,001.68 / 708,656.91 = **13.3%**（即不修就会虚掉这个比例） |
| §8.3(2) 套件行退出商品排行 | 视图改 `line_kind <> 'gift'` 并按 `line_kind` 分组，`metrics.py`/`agent.py` 输出该列，指标文案改为「非赠品父项，套件不是子SKU排行」 | `reporting.v_product_daily`：`sale` 1242 行/43 商品，`suite` 246 行/38 商品，均可见且带行性质标注 |

## 9.2 真实库当前状态（同步已于 01:52/01:56 重跑）

- `bi.orders` 9522：`FINISHED` 7963、`SELLER_SEND_GOODS` 780、**`CLOSED` 750 全部 `active=false`**、`WAIT_SEND_GOODS` 28、`unified_status` 缺失 1 条（按 `sys_status=FINISHED` 回退为有效，回退路径生效）。
- `bi.order_items` 10002：`platform_line_id` 空值 **0**、`source_type` 空值 **0**（`sale` 9592 / `suite` 410）。
- `bi.aftersales` 1078：`system_completed_at` 空值 **117**（上轮为 1073/1073 全空），`platform_success` 936，`matched` 1034。
- `bi.shops` 42：**`enabled=false` 8 家**、true 34 家。
- 支付事实 9892 条，verified 9207（93.1%）。
- 仍为空的列：`split_parent_id` 0 条、`raw_platform_payment` 0 条（后者已在 `docs/metrics.md` 标注来源不返回，属已知）。

## 9.3 测试与漂移

- `tests.test_core` 63 全过；`tests.test_db` **50 全过**（含 `closed_paid_order_keeps_cash_payment_and_refund_match`、`closed_paid_split_orders_keep_one_verified_payment`、`product_daily_keeps_suite_parent_with_kind_label`、`metric_semantics_migration_appends_line_kind_to_legacy_view`）；`tests.test_api` 5 过；`tests.acceptance --offline` 20/20 过。前提是显式给出 `BI_TEST_ADMIN_DSN`+`BI_TEST_READER_DSN`——默认环境仍是全 skip（本轮还顺手验证了 `*_test` 库名守卫：给一个不合规库名时 50 条全部按预期拒绝）。
- 只读重新拉样比对：12 个层级的字段集合与昨晚**完全一致，无漂移**（新样本订单头 400、订单行 412、售后 200、店铺 42）；覆盖率维持 45/336 有表项、276 无表项、15 显式不入库。

## 9.4 本轮新增/仍存留的观察（均不阻塞）

1. **`erp_documents` 与 `paid_orders` 从此可双向倒挂**：前者只数有效单，后者含关闭但已付单。实测 09-05 `docs=100 / paid=106`，09-06 `docs=104 / paid=99`。口径各自成立（`metrics.md` 已写明 `erp_documents` 仅作对账参考、不作客单价分母），但页面与提示词不得把两者差异当异常报警。
2. **赠品判定被放宽成回归风险**：`888f04e` 把 `gift_quantity > 0 and quantity == 0` 改成 `gift_quantity > 0`。若出现「既卖又赠」的混合行（`num>0` 且 `giftNum>0`），整行会判为 `gift`，`line_kind <> 'gift'` 过滤后**连同售出数量与该行分摊金额一起**从商品排行消失（修复前它以 `sale` 身份整行计入）。真实库 10002 行里 `gift_quantity>0` 与 `quantity=0` 均为 0 条，属未验证路径；建议恢复 `quantity == 0` 条件，赠品数量已由 `gift_quantity` 单列承载。
3. **A3 拆单映射仍只有合成证据**：生产 `split_parent_id` 0 条非空，样本 681/681 `splitType=-1`。需要等一个真实含拆单/合单的窗口复验，或在 `docs/metrics.md` 标注「该列尚无真实数据验证」。
4. `parse_timestamp` 仍无 0 值防护（`payTime=0` 会写成 `1970-01-01`）；真实库 `paid_at < 2000` 为 0 条，风险未证实也未设防。
5. 死读与无效兜底仍在：`_commercial_ids` 读永不出现的 `tids`；售后 `aftersaleId`/`modifiedTime`/`updTime`/`refundId`/`platformRefundId`、店铺 `shopName` 五个兜底名在文档与响应中都不存在（不影响正确性，但会误导后续维护者以为它们是有效来源）。
6. 首轮 B/C 类维持不变：`aftersale.items`(30)、`orders[].suits`(56)、`tradeTags`(4)、`messageMemos`(27+2)、23 项金额/费用族、商品名称与档案、库存/历史成本/采购/推广均仍无表项；`order_items.product_id`/`sku_id` 依旧只有数字 ID，商品排行给不出名称。

## 9.5 结论

首轮 6 项映射缺陷、迁移执行、schema 守卫、两处口径副作用，**本轮全部闭环**；快麦字段的入库覆盖从 37/336 升到 45/336，恒 NULL 列只剩设计上已标注的 `raw_platform_payment`。剩余项是「要不要为明细层与金额族建表」的产品范围决策，加上第 9.4 条的 5 个小项（其中第 2 条建议尽快回滚那半个条件）。

---

# 10. 处置记录：赠品混合行（§9.4 第 2 条）

决策：恢复 `gift_quantity > 0 and quantity == 0` 作为整行判赠品的条件。混合行（`num>0` 且 `giftNum>0`）保留 `sale`，销售数量与该行分摊金额进商品排行，赠品数量由 `gift_quantity` 单列展示——按 `gift_quantity > 0` 判赠品会让混合行连同售出部分一起被 `line_kind <> 'gift'` 排除，属潜在收入低估。

改动：
- `backend/bi_agent/sync.py` `_normalise_item`：恢复 `quantity == 0` 条件并注明原因。
- `backend/tests/test_core.py`：原 `test_positive_gift_quantity_overrides_line_type_for_metric_filtering` 断言的正是被放宽后的行为（`num=1`、`giftNum=1` 期望 `gift`），替换为 `test_mixed_gift_line_keeps_sale_kind_and_reports_gift_quantity`，同时覆盖混合行（`sale`，quantity 2、gift_quantity 1、分摊金额 30 保留）与纯赠品行（`num=0`、`giftNum=3` → `gift`）。
- `docs/metrics.md` 字段规范化表同步该口径，避免文档与代码再次分叉。

复验：`test_core` 63、`test_db` 50、`test_api` 5、`tests.acceptance --offline` 20/20 全过（DB 用例跑在一次性库 `bi_agent_check_test` 上，验毕 `DROP`）。真实库现有 `bi.order_items` 中 `gift_quantity>0` 与 `quantity=0` 均为 0 条，**已入库数据不含混合行，部署后无需为此单独 replay**；后续含赠品的窗口由 `incremental`/`reconcile` 自然生效。
