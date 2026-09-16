# 指标口径与来源对账

> 2026-09-12 修订：淘系 tb/tm 已通过非敏感出库通道接入统一 main，但查询侧单源覆盖、退款硬门禁与跨平台 basis 尚待原路线图 Task 5 实现。当前运行限制仍有效；目标合同以 [多来源指标设计](superpowers/specs/2026-09-12-multi-source-metrics-design.md) 为准。关闭但已付款单保留 active=false，独立认证支付事实并匹配退款；paid_amount 是退款前支付，不能叫净支付。未来退款未匹配降级为可答+比例声明，不能直接把本页历史拒答测试当新验收预期。

| 当前整合状态 | 查询发布条件 |
| --- | --- |
| fxg 等已核验交易源 | 保留现有认证与覆盖，逐店可用性取实际状态 |
| tb/tm 出库同步已合并 | 先完成来源路由门禁、时间语义核验、退款诊断与ERP口径标签；不能宣布平台账单GMV完整 |
| pdd 有单据但无支付金额 | **2026-09-12 决定不接入拼多多支付**（方舟授权成本过高）：任务5以capabilities长期阻止支付指标，不再是待批项 |

数据依据：[快麦复核报告](superpowers/research/2026-09-06-kuaimai-data-verification.md)、[脱敏实测统计](superpowers/research/2026-09-06-kuaimai-data-recheck.json)。本文是「可执行对账清单」：来源表记录接口、字段路径、单位、粒度、状态规则、抽样范围、启用状态和证据日期。**启用状态为「未启用/待对账」的项，代码与页面不得当作可用能力。**

## 1. 数据来源表

| 数据 | 官方方法 | 状态 | 证据日期 | 抽样范围 |
| --- | --- | --- | --- | --- |
| 店铺 | `erp.shop.list.query` | 待对账（任务4） | 2026-09-06 | 42条，33条启用，多平台 |
| 普通订单（非淘系/拼多多） | `erp.trade.list.query` | 待对账（任务4） | 2026-09-06 | 抖音一店第一页20条 |
| 售后工单 | `erp.aftersale.list.query` | 待对账（任务4） | 2026-09-06 | 抖音14条 |
| 商品档案 | `item.list.query` | 后续扩展 | 2026-09-06 | 3个启用商品 |
| 商品SKU | `erp.item.sku.list.get` | 后续扩展 | 2026-09-06 | 8个SKU |
| 当前库存 | `stock.api.status.query` 等 | 不在本版范围 | 2026-09-06 | 3条SKU样本 |
| 历史成本 | `erp.item.history.cost.price.query` | 禁用（成功空结果≠无成本） | 2026-09-06 | 成交SKU无记录 |
| 采购金额及采购明细 | `purchase.order.query` | 禁用（文档单位为分，本版不接入） | 2026-09-06 | 空 |
| 推广消耗 | 无公开方法 | **未证实，禁用** | 2026-09-06 | 增值报表需联系实施取得授权 |

## 2. 字段规范化规则（按完整路径）

| 字段 | 规范化规则 |
| --- | --- |
| 订单 `payAmount/payment/platformPaymentAmount` | 分别为买家已付/应付/平台支付，均独立保留；不得互换 |
| 订单 `unifiedStatus` / `sysStatus` | 两者原样保留；`unifiedStatus=CLOSED` 时订单不参与 ERP 有效单与商品父项统计，仅在它缺失时以 `sysStatus=CLOSED` 回退。平台 `status` 是平台码，不参与此判定；若已有有效支付时间和正支付额，支付事实仍保留，以便退款从已收款中抵扣 |
| 订单 `splitType` / `splitSid` | 仅 `splitType=1` 时，将 `splitSid` 写入 `split_parent_id` |
| 订单 `orders[].oid` / `orders[].type` / `orders[].giftNum` | `oid` 为平台行号；`type` 原样保留为 `source_type`。行性质：仅 `num=0` 且 `giftNum>0` 才整行判为 `gift`；`num>0` 且 `giftNum>0` 的混合行保留 `sale`（官方以 `giftNum` 判是否赠品，但销售数量与该行分摊金额不得因此从商品排行消失），赠品数量由 `gift_quantity` 单列承载。商品排行保留套件/组合/加工**父项**并输出 `line_kind`，不把它们表述为SKU子件排行 |
| 订单 `updTime` / `modified` | 前者为ERP数据更新时间，后者为平台修改时间；对 `upd_time` 增量先核对二者含义和样本，不直接把后者当ERP版本 |
| 订单 `cost` / `orders[].cost` / `orders[].suits[].cost` | 分别为总成本/普通行单位成本（需×num）/部分套件子结构总成本，分别标注；不统一乘数量 |
| 售后 `rawRefundMoney` / `items[].rawRefundMoney` | 单头是元，商品明细是分；首版退款聚合仅使用单头，商品退款暂不开放 |
| 售后 `onlineStatus=7` + `platformCompleteTime` | 平台退款成功候选条件；还需检查工单作废/合并、平台售后号去重。`status` 可为逗号分隔多值，含10/11时一律不计成功 |
| 售后 `finished` | 系统完成时间，映射为 `system_completed_at`；`systemCompleteTime`/`completeTime` 不作为来源 |
| 店铺 `active` | 映射为 `shops.enabled`；字段缺失才按已核验的 `state` 枚举（3/4启用）安全回退。查询混合范围时停用店铺被排除；若全部停用则拒绝返回指标 |
| 采购 `totalAmount/actualTotalAmount`、明细 `price/amount` | 文档为分；本版不接入，不得误复用订单转换函数 |
| `orders[].itemSysId/skuSysId` | 显式映射为商品查询的 `sysItemId/sysSkuId`；不按名字模糊匹配 |
| 订单 `grossProfit` | ERP毛利参考；实际运费/包材/平台扣费缺失，不得称净利润 |
| 拼多多 `payAmount/payment/modified` | 样本全部缺失；不得纳入支付指标，也不用其他字段反推。**2026-09-12 决定不再申请方舟授权，该限制为长期口径而非临时缺口**（见[范围决定](superpowers/research/2026-09-12-drop-pdd-onboarding.md)） |
| 合单（一张 ERP 单多个 `tid`/`tids`）的单头 `payAmount` | **只等于其中一个子单的金额**，不是整张合计（实测 287 张合单：单头 14.25 / 行级合计 386.05）。因此合单不得用单头与行级交叉咬合判核验：改按行级 `payAmount` 的 `tid` 归属取证（`basis='items_merged'`），且要求行全部带金额、行 tid 均在单头声明列表内、兄弟子单均有行，否则仍为 `undetermined` |
| `verified` / `allocation_verified` | 均为**本系统派生的交叉咬合结论，不是 ERP 字段**；含义、三态与升级规则见第 6 节 |

## 3. 指标口径（任务5实现后与人工答案对账）

业务时区 `Asia/Shanghai`；内部范围 `[start, end)` 排他。

| 指标 | 口径 | 来源依赖 |
| --- | --- | --- |
| `paid_amount` | 已验证商业订单支付金额之和（`order_payments.verified`，人民币） | orders/order_payments |
| `paid_orders` | 已验证商业订单数（一行一商业单） | order_payments |
| `erp_documents` | ERP单据数（拆合单粒度，仅作对账参考，不作客单价分母） | orders |
| `aov` | paid_amount / paid_orders（总口径，不平均每日） | 同上 |
| `refund_amount` | 平台退款成功（`platform_success` 且 `refund_canonical`）按完成时间归属的发生额 | aftersales_occurrence |
| `cash_difference` | paid_amount − refund_amount；**期间收支差额，不是净利润** | 上两者 |
| `cohort_refund_rate` | 同批：`[start,end)` 支付商业单在明确截止时刻前的累计退款 / 同批支付额 | aftersales_cohort + 原单匹配 |
| `quantity` / `product_paid_amount` | 有效非赠品父项数量与已核验行级分摊金额；结果以 `line_kind` 区分 sale/suite/combination/processing，套件不是子SKU排行 | order_items |
| 商品运营参考指标（`sold_quantity` 等 6 项） | 只由 `analyze_product_performance` 发布；口径、覆盖与拒发规则见 §4.2 | order_items / orders / order_payments |
| 平台 / 店铺对比值（同一批报告指标） | 只由 `compare_performance` 发布；分组、合计、排名与图表的拒发规则见 §4.3 | 与 §4.2 同一张视图（整店聚合粒度） |
| 推广费率/ROAS | **未启用**：无实耗来源；折扣/成本不得替代广告费 | 无 |

## 4. 已知功能门槛

- 平台实退与系统实退分开；系统退款成功指标未对账前不发布。
- `platformPaymentAmount` 在当前已核验样本未返回，`raw_platform_payment` 不可作为指标来源；`order_payments.currency='CNY'` 是平台默认币种假设，尚无 `tradeExt.currency` 来源核验。
- 同批退款率只统计**已归属到本批支付**的退款；未匹配退款不猜配也不丢弃，另按披露给出。
- 「最近7天」= 最近7个完整自然日；「今天」未完成，返回缺数据而非0。
- 日期跨度最多366天。逐日分组最多500个（店铺,日）组，超出直接拒参数；`total`/`shop` 分组在SQL端按店铺聚合后再累加，看到的行数只取决于店铺数，不会被日行数上限压低。任何取数路径真的命中500行上限时返回 `unavailable` 并说明原因，绝不返回被截断的偏低汇总。
- 查询时间预算耗尽一律返回 `unavailable`，不与「确实没有数据」混同（后者才是 `ok` + 空行）。
- 真实推广实耗、成本贡献、淘系完整支付指标均有数据门槛，未满足时明确不可用；拼多多支付口径按 2026-09-12 决定**不接入**（长期不可用，非等待授权）。

## 4.1 推广预算情景测算口径（promotion.py）

- 输入只接受当前用户明确输入（页面表单或问题中明确假设）；模型不能把ERP成本/优惠解释为实耗，也不能沿用上一轮参数。
- `sales_cap`：上限 = 假设销售额 × 假设费用率（费用率≤100%）；预测销售不达预期时阈值需调整。
- `budget_scenario`：剩余 = max(0, 预算-已花)；超支单列 = max(0, 已花-预算)；日均 = 剩余/剩余天数（排他截止日起算），周期结束不除零。
- `actual_budget`/`contribution_cap`：一律 missing_data——真实费用率=同期实耗/同期有效支付（分母0不可计算），预算进度要求费用完整覆盖到昨日，贡献上限 max(0, C-P)；均未具备输入，不实现分支。
- 假设结果 `basis=用户输入假设`，`coverage.status=missing`（无费用实绩源）、`data_as_of=None`；不得写成“账号实际剩余额度”，也不得称店铺收入/费用为广告归因ROAS。

## 4.2 商品运营参考指标（`backend/bi_agent/commerce/`，计划 Task 7）

入口只有一个：`analyze_product_performance(request, context) -> DomainResult`（图：
`CommercePerformanceGraph`）。模型只能递业务字段与 `ent-` 引用；身份、授权范围、连接与
deadline 全部来自服务端 `DomainContext`。旧 `query_business` 不变，两者共用同一份
结果投影与白名单（`business_query.tool.safe_result_body`），不并存第二套数字出口。

口径文案的唯一真源是 `commerce/metrics.py:COMMERCE_METRIC_DEFINITIONS`（下表是人读版）：

| 指标 | 口径 | 不能是什么 |
| --- | --- | --- |
| `sold_quantity` | 有效非赠品销售父项件数（`active`、`line_kind<>'gift'`、按 `paid_at` 归属）；套件/组合/加工以父项计 | 不是子 SKU 件数之和 |
| `sales_amount` | 同一行集合的行级分摊支付金额合计（退款前）；`allocation_verified=false` 的行只计件数不计数额 | 不是平台账单 GMV，也不是净支付 |
| `weighted_avg_paid_price` | 成交均价 = 同一行集合的分摊金额 ÷ 件数；件数 0 时 null | 不是日均价或店铺均价的平均 |
| `product_gross_profit_reference` | `SUM(分摊金额 − 行单位成本 × 件数)`，只含成本与分摊都已核验的普通销售行；**任一行缺成本或行性质为套件/组合/加工 ⇒ 整体 null**；未扣售后、平台费、运费、广告费 | 不是净利润，也不是“已知成本子集的整体” |
| `product_gross_margin_reference` | 同一行集合的商品毛利参考 ÷ 同集合收入；分母 0 或成本覆盖不全 ⇒ null | 不平均店铺利润率，也不拿已知成本除全部收入 |
| `erp_gross_profit_reference` | `bi.orders.raw_gross_profit` 按**唯一 ERP 单据**聚合（拆合单不摊平）；一半单据无毛利字段 ⇒ null 并披露覆盖率 | 不摊到商品，不与商品毛利相加或相除 |

不可让步的七条规则（均有用例钉在 `backend/tests/test_commerce.py`）：

1. **两个口径面分开发布**。商品面是 `metric_result` 与 `trend_series`，ERP 单据面与
   已验证支付面是 `comparison_table`。两面一旦同列，早晚被相加或相除，所以行形本身不允许包
   含对面列（`project()` 当场拒）。
2. **JOIN 放大**：单据毛利永不连 `order_items`。`reporting.v_erp_document_daily` 主键即
   `(shop_id, erp_id)`，一张单三行不会把单头毛利变成三倍。
3. **缺证据是 null 不是 0**：成本、分摊、覆盖、能力四类缺口各自归因，不拿已知子集冒充整体；
   已覆盖但没成交的日才是真实 0，趋势缺口留 null 并单独披露（`trend_coverage_incomplete`）。
4. **销售父项口径与支付口径不混用**。`sales_basis=verified_payment` 下商品面不发布任何
   数字（已验证支付事实停在商业单粒度），只发支付面并标 `incomparable`；两者不得互除。
5. **范围三面分列 + 多指标独立判定**。`requested_scope` / `evaluated_scope` /
   `excluded_scope` 各自存；一家店回答不了报告主面就整店退出合计（原因码取自注册表词表：
   `capability_ungranted` / `coverage_time_basis_unverified` / `coverage_incomplete` 不互代），
   但能答的指标不会因为共一个请求里另一个指标缺数据而被收走。`partial` 的合计永不称为
   “所有店铺合计”，排名与份额只针对已评估集合。
6. **不自动推导投放结论**。低利润候选只按商品毛利参考额升序排队（率与额两列同发，可逐项
   核对）；没有版本化阈值与最小样本时 `opportunity.status=unconfigured`、`flagged=[]`，
   不声称 ROAS、不下预算建议。
7. **上期比较两侧同口径，且比率只在基期为正时成立**。`comparison.rows` 的本期与上期都按
   该店该窗口内**全部行性质**聚合（结果表本身仍逐行性质发行，不然“第一行”只是 sale 那一组，
   而上期是全口径，差额就成了两个集合之差）；`change_ratio` 与其余比率同一规则：基期 ≤ 0 就
   是 null。商品毛利参考允许为负（上期卖得比成本高），-50 → +60 的真实说法是“转亏为盈
   +110”，除以负基期得到的 -2.2 会把一次上涨说成下跌 220%；`change` 不要求正基期，照发。

取数与角度边界：商品销售/成本面与 `reporting.v_product_daily` 共用同一套纳入条件
（`sql/017_commerce_views.sql` 逐字沿用 007），两面数值必须相等；`bi_app` 只拿到四张新
视图的 SELECT，`bi.order_items` / `bi.orders` / `bi.products` / `bi.entity_refs` /
`bi.catalog_state` 依旧读不到（用例逐表断言 `InsufficientPrivilege`）。汇总、七日序列与
上期比较共用一个 REPEATABLE READ 只读快照，覆盖判定与取数不会来自两个数据版本。

本轮仍不得因代码存在就宣称的能力：商品成本覆盖率、上架实际价、库存与售后到商品行的
分配均未取证，所以只发布上述参考指标而不是净利或退货后商品利润；拼多多依旧只有
`erp_documents` 单据口径可用，支付族永久解析不通（见 §6 与 2026-09-12 决定）。

回归命令（`backend/`）：

```sh
.venv/bin/python -m unittest tests.test_commerce tests.test_core tests.test_business_query_graph -v
```

## 4.3 平台 / 店铺对比与图表契约（`compare_performance` + `presentation/charts.py`，计划 Task 8）

对比报告与商品报告跑**同一张** `CommercePerformanceGraph`（`report_kind=comparison`），只是分组
维度不同：`group_by=platform` 比平台，`group_by=shop` 比**一个**平台内的店铺。它不新增长期
投影、也不新建第二张图或第二套算术：整店汇总读的是同一张 `reporting.v_product_cost_daily`
（SQL 端按 `(shop_id, day, line_kind)` 聚合掉商品维度），算术仍是
`combine_reference_metrics`，可算性仍是 `_record_statuses` 留下的那份 `available` 集合。

| 契约 | 规则 | 不能是什么 |
| --- | --- | --- |
| 分组键 | `platform` 取来源注册表登记过的平台码；`shop_ref` 取目录引用 | 不接受平台中文名，也不接受 ERP 店铺号 |
| tb / tm | 显式规则 `platform_group_of`：本轮**没有**已批准的版本化合并规则，淘宝与天猫各成一组，`platform_group_rule` 参与请求指纹与血缘 | 不静默并成"淘系"组；真要合并必须先登记版本化规则并推进规则版本 |
| `group_by=shop` | 范围里必须恰好一个平台（`shop_refs` 与 `platforms` 互斥，交集会拆散分组完整性） | 不允许"拿三个平台的店比店间差距" |
| 分组发布 | 一个分组里**任一**获准店铺未被评估 ⇒ 该组不发布数字，逐组原因写 `group_statuses`（`partial` + `shops_requested` / `shops_evaluated`），缺的店仍逐家在 `excluded_scope` 里；请求单据口径时两份计数（`erp_documents` / `erp_documents_with_gross_profit`）跟着毛利列一起出 | 不发布"两家店盖成整个平台"的组内合计；也不把缺失组当 0；不给一个 null 藏掉可核对的分母 |
| 单元格的两种 null | 组内成员店**没有 ERP 单据** = 真实 0 贡献，本列照常发布；有单据但部分不带毛利字段 ⇒ 毛利列 null 且原因是 `erp_document_coverage_incomplete`；整组没有可发布的事实 ⇒ null 且原因是 `comparison_cell_withheld` | 三种情况不写成同一句话：一个指向"该补取数字段"，一个只是"那天确实没卖" |
| 合计与排名 | 合计与名次共用同一个判定：只有**全部已发布分组都有值且同口径**（`complete`）才一起发；集合不完整时值照发、该列合计与名次全部留 null，缺的分组写进 `missing`（`incomplete`）；口径不一致时连值也不发（`incomparable`，且不再贴单一口径名） | 不平均分组均价或店铺利润率；不把两个口径的柱子排在同一根轴上 |
| `ChartSpec` | `kind=bar|line|scatter`、`dataset_ref`、`x`、`y`、`series`、`unit`、`metric_basis`、`coverage_ref`；字段全是枚举 / UUID / 已登记列名；`unit` 必须等于该指标登记的单位，`metric_basis` 前缀必须就是被画的指标；趋势图的 `series` 必须点名分组列（`platform` / `shop_ref`）而 `x=day`；scatter 在词表里但本轮**没有取数路径**，构造时按 `chart_kind_unsupported` 拒 | 不含 SVG / HTML / JavaScript 或任何自由文本；不复制行数据；不承诺模型生成图 |
| 图表配对 | 先落库数据集再落图表；`bi.query_artifacts.dataset_ref` / `chart_version` 真写进列，两个 Store 写库前都跑 `verify_chart_pairing`（同运行、数据集类型、`data_as_of` 与 `coverage` 逐字相同、版本是当前契约版本） | 图表不能引用另一张图，也不能跨运行引用别人的数据集版本 |
| 下钻 | 平台柱上的一次点击只交出 `DrilldownIntent`（平台码、`[start,end)`、指标、口径签名），外层用它发一次**新的** `compare_performance`；问句里同时写出 `platform=<已登记平台码>`，不只给人读名 | 客户端不缓存、不展开、不"顺手"改窗口或口径；授权由服务端每次重新算；只发中文名会让 `normalize_platform` fail closed，一次下钻退化成"查询参数无效" |

前端渲染侧另有两条自己的铁则：`series` 声明的列决定画几条线（分组趋势的行是"逐分组 × 逐日"，
不分列就会把甲组最后一天连到乙组第一天），轴的两端只用水里原样出现过的数（负毛利因此自己
占一段轴，柱形从基线向下长，不会被压成贴着轴底的一像素）；数据集版本按**时刻**比而不是按
序列化写法比（同一 UTC 瞬间有 `Z` 与 `+00:00` 两种合法写法）。

一次对比 = 一次图执行 = **一条**整店集合查询（`shop_id = ANY(%s)`）：查询条数不随店铺数增长，
主期间、分组行与七日序列共用同一个 REPEATABLE READ 只读快照。

仍不得因代码存在就宣称的能力：京东、快手、视频号、微购相册的支付窗口都还没有逐店账单对照
证据（`unmeasured`），抖音是 `certified`、淘系是 `disproved`，所以按现行可比性规则**跨平台**的
`sales_amount` 往往判为 `incomparable` —— 此时报告只分行给出各分组自己的数，合计与排名留空。
五平台总览里唯一能拿到完整总览的是"同为未认证支付口径"的那一组，其余一律按缺口披露；
拼多多继续作为显式缺失组出现（`excluded_scope`，不接入支付）。

回归命令（`backend/`）：

```sh
.venv/bin/python -m unittest tests.test_comparison tests.test_commerce tests.test_core -v
uv run --env-file ../.env.test python -m unittest tests.test_runtime_db -v
cd ../frontend && npm test -- src/components/ChartArtifact.test.tsx src/components/ArtifactView.test.tsx
```

## 4.4 四工作流端到端验收（计划 Task 11，2026-09-14）

入口仍是四个公开 Tool（`analyze_product_performance` / `compare_performance` /
`audit_listing_prices` / `inspect_inventory`）与旧 `query_business`；本节只记验收结论与口径红线，
逐条证据在 [Task 11 日期化发布验收报告](superpowers/research/2026-09-14-task-11-release-acceptance.md)。

| 口径 | 结论 | 不能是什么 |
| --- | --- | --- |
| 一次完整业务请求 | 一次业务 Tool 调用 = 一次图执行：商品跨店汇总、七日趋势、图表与候选在同一份冻结数据里生成；跳数不随店铺数增长（一条 `shop_id = ANY(%s)` 集合查询），30 秒不够就明说未完成 | 不主 Agent 逐店循环，也不静默缩短窗口 |
| 上架目标价 | 只取自用户**当前这一句**，冻结为本次审计依据（`bi.price_audit_expectations` 按 `run_id` 存）；本轮没给价 → `needs_input` 且零份依据落库 | 不从上一轮、会话筛选、历史成交均价或 ERP 档案建议价继承一个标准 |
| 差异表分母 | `expected_items` 固定为用户本轮选定的授权店铺集合 × SKU / 落点展开的期望项；`evaluated_items` 只数真判过的格，未判定逐格带原因（`unknown` / `unsupported` / `stale` / `missing_standard` / `unmapped`） | 不拿采到的项当分母，也不把缺失当 0 或未上架 |
| “全部正确 / 全部安全” | `all_correct` 要求每一格都新鲜、完整且匹配；`all_safe` 还要求扫描声明完整且未截断 | 缺一家、快照过期、来源未取证或只看 Top N 都不能说 |
| 两级库存 | 实物按 (池, 仓库, SKU, 批次, 单位) 去重只算一次，渠道可售逐店各一行 | 三个渠道各展示 100 不是 300；配额候选与补货候选不互换 |
| 真实就绪 | 价审与库存的**来源注册表默认为空**，真实部署只会报 `unsupported`；测试用例里的“正常路径”靠测试进程内登记的**合成**来源 | 合成通过不是真实来源就绪，代码存在不是平台已对账 |
| 拼多多 | 支付族永久解析不通，只以 `excluded_scope` / `capability_unavailable` 出现；本轮未新增连接器、来源、凭证或 onboarding 代码路径 | 不写“待授权 / 延后”，也不拿出库金额补支付数 |

验收题库从 20 题扩到修订后的 26 题（08 / 15 改预期，新增 21–26），结构化断言现在包含原因码、
逐店逐指标 basis、退款诊断与覆盖形状。一个刻意的取证映射：原路线图把 Q21 / Q23 / Q25 的示例数字
挂在淘系店上，但出库通道的付款时间口径已被实测判为 `disproved`（见 §6 与 Q26），所以混口径分列的
可执行证据改用两家都能答的 `erp_documents`（抖音 6 / 淘系 1，各带自己的时间归属），未匹配退款披露
留在淘系的 `refund_amount`（50 元，1/2 未匹配、20 元），而“关闭已付款单 100 / 30 / 70 与销量不回活”
放到具备合成时间口径认证的抖音基准店上。这一映射是为了不同时违背两条规则，不是宣布淘系支付可用。

「销售额」的澄清不再是一句固定的“支付还是出库”：它按本轮授权店铺的平台与来源注册表列出候选口径，
并同时要期间；`certified` / `unmeasured` / `disproved` 三种说法分开，出库通道明写“非平台账单 GMV”，
拼多多明写“无支付口径能力”。登记过口径仍然不等于结果可用。

## 4.5 语义目录首版登记面（`backend/bi_agent/semantic_catalog/`，计划 Task 11 后置子项目 A）

目录版本 `semantic/2026-09-14.1`，门禁 `SEMANTIC_CATALOG_ENABLED` **默认关闭**。它只回答“哪些已批准的结构可能表达这个问题”，为**后续**受控 SQL 探索提供至多 5 个候选视图；不执行 SQL、不读业务事实行、不新增 Agent Tool，也不改变下面任何一条口径与能力门禁。模型与检索只见 kebab-case ref；SQL 标识符只由服务端 `resolve_sql_identifier()` 解析，`bi.*` 底表永不进目录。

首版登记的 11 张 `reporting.*` 视图（共 84 字段 / 22 指标 / 10 实体）：

| view ref | SQL 视图 | 粒度 | 授权列 ref | 登记指标数 | 领域 |
| --- | --- | --- | --- | --- | --- |
| `view-shops` | `reporting.v_shops` | 店铺 | `field-shops-shop-id` | 0（只档案列） | 全部四领域 |
| `view-shop-daily` | `reporting.v_shop_daily` | 店铺 / 日 / 币种 | `field-shop-daily-shop-id` | 5 | business_query, commerce_performance |
| `view-product-daily` | `reporting.v_product_daily` | 店铺 / 日 / 商品 / 行性质 | `field-product-daily-shop-id` | 3 | 同上 |
| `view-product-cost-daily` | `reporting.v_product_cost_daily` | 店铺 / 日 / 商品 / 行性质 | `field-product-cost-daily-shop-id` | 4 | commerce_performance |
| `view-erp-document-daily` | `reporting.v_erp_document_daily` | 店铺 / ERP 单据 | `field-erp-document-daily-shop-id` | 2 | commerce_performance |
| `view-payments` | `reporting.v_payments` | 店铺 / 商业单 | `field-payments-shop-id` | 1 | business_query, commerce_performance |
| `view-refunds` | `reporting.v_refunds` | 店铺 / 退款单 | `field-refunds-shop-id` | 1 | 同上 |
| `view-coverage` | `reporting.v_coverage` | 来源 / 实体 / 店铺 | `field-coverage-shop-id` | 0（只覆盖与质量列） | 全部四领域 |
| `view-listing-items` | `reporting.v_listing_snapshot_items` | 快照 / 链接 / SKU | `field-listing-items-shop-id` | 2 | listing_price_audit |
| `view-physical-stock-items` | `reporting.v_physical_stock_items` | 快照 / 库存池 / 仓库 / SKU / 单位 | `field-physical-stock-items-pool-id` | 3 | inventory_watch |
| `view-channel-stock-items` | `reporting.v_channel_stock_items` | 快照 / 店铺 / 链接 / SKU / 单位 | `field-channel-stock-items-shop-id` | 1 | inventory_watch |

首批只登记这四条边，全部是 N:1 到店铺档案、两侧各用自己的授权列、允许聚合粒度只有 `shop`：

| JOIN ref | 左 → 右 | 键 | 基数 |
| --- | --- | --- | --- |
| `join-shop-daily-shops` | `view-shop-daily` → `view-shops` | `field-shop-daily-shop-id` = `field-shops-shop-id` | `many_to_one` |
| `join-product-daily-shops` | `view-product-daily` → `view-shops` | `field-product-daily-shop-id` = `field-shops-shop-id` | `many_to_one` |
| `join-product-cost-daily-shops` | `view-product-cost-daily` → `view-shops` | `field-product-cost-daily-shop-id` = `field-shops-shop-id` | `many_to_one` |
| `join-erp-document-daily-shops` | `view-erp-document-daily` → `view-shops` | `field-erp-document-daily-shop-id` = `field-shops-shop-id` | `many_to_one` |

**故意没有直接 JOIN 的三对（不登记，登记就等于允许把不同粒度 / 不同口径的行乘在一起）**：

- `view-payments` ↔ `view-refunds`：支付与退款分属两个时间口径（`paid_at` 与 `platform_completed_at`），一行乘一行会造出不存在的“同单退款”。两者各自与 `view-shop-daily` 的聚合面也不得相加：一个是明细面、一个是日聚合面。
- 商品毛利 ↔ 单据毛利（`view-product-cost-daily` ↔ `view-erp-document-daily`）：商品面按 `(店铺, 日, 商品, 行性质)`、单据面按 `(店铺, ERP 单据)`；一张单多行会把单头毛利乘几倍（§4.2 第 2 条）。要对照只能像现有 Tool 那样两面分列各自展示：目录里这两张视图之间没有登记边，一起点名会被当作断开的图处理——不给半条路径，并要求澄清。
- 实物库存 ↔ 渠道库存（`view-physical-stock-items` ↔ `view-channel-stock-items`）：实物按库存池去重、渠道逐店各一行，三店共 100 件一边 JOIN 就变 300（§4.4 “两级库存”）。两侧连授权列都不同（`pool_id` 与 `shop_id`）。

三条红线的后果：“把支付流水和退款单逐笔对上”、“比较商品毛利和 ERP 单据毛利”、“实物库存与渠道库存一起看”均返回 `requires_clarification=true` 与空 `join_path_refs`（gold set S23 / S25 / S11）；目录不为了“给个候选”而退到一个看似的视图。

不可回退的门禁归属（目录不接管、也不稀释）：

- **能力与覆盖门禁仍是 `sources.py` + `bi.shops.capabilities` 的职权**：目录选中某个视图不代表该店该指标能出数，也不代表可以跑 SQL；`capability_unavailable` / `coverage_incomplete` / `coverage_time_basis_unverified` 全部在下游运行层判定。
- **basis 门禁照旧**：目录里只有支付口径的金额列（首批登记的 reporting 视图里没有任何“ERP 出库金额”列），所以“按出库口径看销售额”只能要求澄清（gold set S07），不能退回 `paid_amount`；`销售额` / `GMV` 这类通用词单独出现时也算口径未定（§3 与 §4.4 “「销售额」的澄清”）。
- **拼多多保持不支持**：目录不收录任何支付能力，也不因 `erp_documents` 单据列的存在而打开支付族；PDD 仍只以 `excluded_scope` / `capability_unavailable` 出现（§6 与 2026-09-12 决定）。本计划未新增任何 pdd 连接器、登记、凭证或 onboarding 路径。
- 未登记面：数组与 multirange 列（`v_shops.capabilities`、`v_product_cost_daily.sku_ids`、`v_coverage.covered`）首批不登记（契约里没有 array/multirange 类型，伪装成 `text` 会让启动预检在真实 schema 上永远失败）；ERP 主键类列一律标 `internal`，不进模型可见选择。
- 上架价与库存依旧“无已核验来源 ⇒ 真实部署只能 `unsupported`”：目录登记了 `view-listing-items` / `view-physical-stock-items` / `view-channel-stock-items` 只意味着“这些结构已批准可用于将来的候选”，不意味着来源就绪（§4.4 “真实就绪”与 §1 的启用状态列）。检索不读库（启动预检只读一条 `information_schema.columns`）。

回归命令（`backend/`）：

```sh
uv run --locked --env-file ../.env.test python -m unittest tests.test_semantic_catalog -v
```

30 题 gold set 的逐题结果、Top 5 召回率与“门禁关闭时行为不变”的对比证据见 [语义目录与 Schema 检索本地验收记录](superpowers/research/2026-09-14-semantic-catalog-acceptance.md)。

## 4.6 受控聚合探索的允许面（`backend/bi_agent/exploration/`，计划 Task 11 后置子项目 B）

入口是 `explore_business_data`：它只在门禁打开且本轮过得了探索门禁时，作为**第七个** Tool 追加在
现有六份固定 Tool 之后（固定 Tool 的顺序与描述逐字不变；关闭时根本不追加）。门禁
`CONTROLLED_SQL_ENABLED` **默认关闭**（且必须先开 `SEMANTIC_CATALOG_ENABLED`，否则启动即
`CONTROLLED_SQL_REQUIRES_SEMANTIC_CATALOG`）。本节只登记“受控聚合探索能表达什么”，**不改变上面
任何一条口径、能力或覆盖门禁**：它不是通用 Text2SQL，也不是“多一个算指标的地方”。同一个指标
走探索与走固定 Tool 必须是同一个数，否则就是口径事故。

### 固定 Tool 优先（先问“谁能表达”，再谈能不能发 SQL）

只要有一个固定 Tool 能同时覆盖本轮全部指标与全部分组粒度，就不开探索入口（运行层记
`fixed_tool_available`，发生在任何 SQL 之前）。重叠时按 Tool 声明顺序取第一个覆盖者：
`query_business` → `analyze_product_performance` → `compare_performance` →
`audit_listing_prices` → `inspect_inventory`。分组粒度取各固定 Tool **真实契约**能表达的那几种，
并且整组匹配（`QueryRequest.group_by` / `ComparisonGroupBy` 都是单值枚举，所以 `{day, shop}`
这种两维组合谁都不覆盖）：

| Tool | 覆盖的指标 ref | 可表达的分组 |
| --- | --- | --- |
| `query_business` | `metric-paid-amount`、`metric-paid-orders`、`metric-erp-documents`、`metric-refund-amount`、`metric-cash-difference`、`metric-quantity`、`metric-product-paid-amount` | 合计、`day`、`shop`、`product`（商品面指标只能配 `product`） |
| `analyze_product_performance` | `metric-sales-amount`、`metric-quantity`、`metric-cost-total`、`metric-product-gross-profit-reference` | 合计、`product`、`shop`、`line_kind`（固定报告行形；七日窗口固定，所以 `day` 不算它能表达） |
| `compare_performance` | `metric-sales-amount`、`metric-quantity`、`metric-cost-total`、`metric-paid-amount`、`metric-paid-orders`、`metric-erp-gross-profit-reference` | `platform`、`shop`（`group_by` 必填，无“合计”这一档） |
| `audit_listing_prices` | `metric-listing-price` | 合计、`shop`、`listing`、`sku` |
| `inspect_inventory` | `metric-physical-available-quantity`、`metric-channel-sellable-quantity` | 合计、`shop`、`pool`、`warehouse`、`listing`、`sku`、`{pool, warehouse}`、`{shop, listing}` |

两点不得误读：`metric-quantity` 是计划矩阵里的写法，在已发布目录里没有条目（“销量”的 ref 是
`metric-sold-quantity`），所以该条目对今天的目录天然空转——把它改写成 `metric-sold-quantity`
属于“新增 Tool 覆盖”，要改得先改计划；而**固定 Tool 的拒答不会被降级绕过**：它因缺能力、
缺覆盖或口径未定而正确拒答时，探索入口仍然不开（这一层刻意不看 `missing_concepts` 与
`requires_clarification`，也不拿“它大概也会拒”当放行 SQL 的理由）。

### 允许的视图、聚合与分组列

编译只仍从当前发布的语义目录（`semantic/2026-09-14.1`）解标识符；目录关着就没有解析路径。

| 视图 | 可探索的指标 ref（默认聚合） | 可当分组维度的列 | 备注 |
| --- | --- | --- | --- |
| `view-shop-daily` | `metric-paid-amount`、`metric-paid-orders`、`metric-erp-documents`、`metric-refund-amount`、`metric-cash-difference`（均 `sum`） | `day`、`currency`、`shop_id`（授权列） | 与固定指标查询共用同一口径定义 |
| `view-product-daily` | `metric-sold-quantity`、`metric-gift-quantity`、`metric-product-paid-amount`（均 `sum`） | `day`、`line_kind`、`allocation_verified`、`shop_id` | 父项口径，不是子 SKU 排行 |
| `view-product-cost-daily` | `metric-sales-amount`、`metric-cost-total`（均 `sum`） | `day`、`line_kind`、`shop_id` | 商品毛利参考与成交均价不可探索（下表） |
| `view-erp-document-daily` | `metric-erp-document-cost`、`metric-erp-gross-profit-reference`（均 `sum`） | `day`、`normalization_status`、`shop_id` | 单据面永不连 `order_items`（§4.2 第 2 条） |
| `view-payments` | `metric-payment-flow-amount`（`sum`） | `currency`、`verified`、`paid_at`、`shop_id` | 明细面；与 `v_shop_daily` 的已支付金额不同数 |
| `view-refunds` | `metric-refund-record-amount`（`sum`） | `platform_success`、`refund_canonical`、`matched`、`platform_completed_at`、`shop_id` | 平台原始金额，不等于已归一的 `refund_amount` |
| `view-listing-items` | **无**（两个价指标都不可探索） | `captured_at`、`currency`、`on_sale`、`shop_id`（只供固定 Tool） | 上架价仍是 `audit_listing_prices` 的固定路径 |
| `view-physical-stock-items` | `metric-physical-available-quantity`、`metric-inbound-quantity`、`metric-locked-quantity`（均 `sum`） | `unit`、`captured_at`、`pool_id` | 实物按 (池, 仓库, SKU, 批次, 单位) 去重，不跟渠道数相加 |
| `view-channel-stock-items` | `metric-channel-sellable-quantity`（`sum`） | `unit`、`captured_at`、`shop_id` | 渠道逐店各一行；单位不换算也不相加 |
| `view-shops` | 无（只档案列） | 只能作 JOIN 右侧的 `platform` | 它是档案侧，不是事实粒度 |
| `view-coverage` | 无（只覆盖与质量列） | — | 覆盖与质量仍由 `data_quality` / `sources` 判，不拿 SQL 重算 |

不可探索的四个指标（编译器当场拒，不回落成“换个聚合试试”）：

| 指标 ref | 拒因 |
| --- | --- |
| `metric-product-gross-profit-reference` | 多字段比值：要分子分母在同一行集合上各自求和再相除，单列一个聚合发不出那个数（`exploration_metric_field_ambiguous`） |
| `metric-transaction-average-price` | 同上，且默认聚合是未授权的 `avg` |
| `metric-listing-price` | 目录登记默认聚合 `avg`，而编译器只放 `sum/count/min/max`（`exploration_aggregate_not_permitted`） |
| `metric-campaign-price` | 同上 |

允许的聚合只有 `sum`、`count`、`min`、`max`。允许当分组维度的列角色只有 `dimension`、
`time`、`authorization`；`measure`（未聚合的度量）与 `internal`（ERP 主键）一律不可分组也不可
公开出列——授权列参与分组时只能以 opaque `shop-ref` 出现，`_pool_id` 等其他 `_` 前缀列在投影
层直接拒。

### 允许的 JOIN

只有目录里那 4 条 `many_to_one` 店铺档案边（`join-shop-daily-shops`、
`join-product-daily-shops`、`join-product-cost-daily-shops`、`join-erp-document-daily-shops`），
而且跳视图分组**只准取档案侧的 `field-shops-platform` 这一列**（本轮检索还必须已经把这条边
选进 `join_path_refs`）。两侧都是各自的授权列，所以不放大行数；金额只在事实侧聚一次。

§4.5 里“故意没有登记”的三对边在探索层同样不放开：支付 ↔ 退款、商品毛利 ↔ 单据毛利、
实物库存 ↔ 渠道库存。未登记边返回 `exploration_join_not_registered`，跳事实粒度返回
`exploration_multiple_fact_grains`；这跟 §4.2 / §4.4 的“两面分列、不相加不相除”是同一条红线。

### 预算与结果契约

| 契约 | 取值 | 超限行为 |
| --- | --- | --- |
| 窗口 | `[start, end)` 半开，至多 366 天；无窗口不编译 | `exploration_window_required` |
| 行数 | `limit ≤ 500`，实取 `limit + 1` | 多出的那一行是截断证据 ⇒ 整条拒，不交偏低的汇总 |
| 结果体积 | 安全投影后的紧凑 JSON ≤ **262144** 字节 | `exploration_result_too_large` |
| 预计行数 / 总成本 | 真 `EXPLAIN` 的 `Plan Rows ≤ 50000` 且 `Total Cost ≤ 100000` | `exploration_budget_exceeded(estimated_rows\|total_cost)` → 运行层 `query_cost_exceeded` |
| 语句超时 | `SET LOCAL statement_timeout = '5000ms'`（两道门各自一次） | `exploration_statement_timeout` → `query_timeout` |
| 整轮预算 | 沿用 `context.deadline`（30 秒），图不重置也不另开一份 | `deadline_exceeded` |
| 身份 | 只跑 `bi_app` 的 `READ ONLY` 事务；底表依旧读不到，`pg_read_file` 一类同样被拒 | 数据库报的错一律换成稳定码，不带语句原文 |
| 隐私 | SQL 与参数只进 `bi.query_diagnostics`（`bi_reader` 无授权、不建 reporting 视图）；公开 Artifact 只存安全结果与 `statement_fingerprint` | 诊断或 Artifact 写不进 ⇒ `persistence_failed`，不发结果 |

### 不得从本功能读出的结论

- **时效与完整性**：“按抓取时刻看上架价”是已批准的 fail-closed 映射——它作为 `R01` 负例行存在，
  拒在聚合授权（`avg`），不改编译器、不改目录、也不升目录版本；上架价的固定路径仍是
  `audit_listing_prices`。库存快照的时效/完整性只通过真正放行的 `sum` 型渠道/实物指标（可售、
  可用、在途、锁定）覆盖，这**不**证明来源就绪，也不证明快照完整。
- 能力与覆盖门禁仍属 `sources.py` + `bi.shops.capabilities`；“能编出一条干净的语句”永远不会
  把 `capability_unavailable` / `coverage_incomplete` / `coverage_time_basis_unverified` 洗成可出数。
- basis 仍照 §3 / §4.4 / §4.5：目录里只有支付口径的金额列，探索不发明“出库口径的销售额”。
- 拼多多保持不支持：本功能未新增任何连接器、来源登记、支付能力、凭证或 onboarding 路径，
  也不因 `erp_documents` 单据列的存在而打开支付族。
- 本节全部放行证据来自本机 `*_test` 与事务内 seed 的合成行，逐条见
  [受控 SQL 探索本地验收记录](../research/2026-09-14-controlled-sql-acceptance.md)；真实模型与
  真实来源的验证一律 `未执行`。

回归命令（`backend/`）：

```sh
uv run --locked --env-file ../.env.test python -m unittest tests.test_exploration -v
```

## 4.7 approved 查询学习记忆的观测口径（`backend/bi_agent/query_memory/`，计划 Task 11 后置子项目 C）

本功能默认关闭（`APPROVED_QUERY_MEMORY_ENABLED=false`），且仓库没有指标后端：以下是三个定名指标的**真实现状**与数据库侧的只读观测口径，不虚构未接入的计数器。

### 三个定名指标

| 名称 | 现状 | 含义 |
| --- | --- | --- |
| `query_memory_retrieval_total` | 当前**没有独立计数器**：门禁开启的每回合至多发 4 条投影视图 SELECT + 2 条目录版本读取（开放探索 Tool 的回合第 5 域再加一），剩余预算不足 2 秒整段跳过且零 SQL；读取量可由该固定上界与数据库侧对 `reporting.v_approved_query_examples` 的查询计数推出。接入指标后端时应以该名称登记“门禁开启的检索尝试次数” | 衡量记忆面被使用的频度 |
| `query_memory_candidate_invalid_total` | **进程内计数器**（`bi_agent.query_memory.retrieval` 模块内的定名整数；读取方先快照再取差值） | 检索层因反序列化失败、引用未登记、槽位重复、revision < 1 等缺陷被整条丢弃的候选数；每次读取都重新解析，坏行每次都计 |
| `query_memory_retrieval_failed_total` | **进程内计数器**（`bi_agent.query_memory.prompt` 模块内；同一读取方式） | 路由期整段记忆检索失败（连接、解析、序列化）的次数；失败即 fail open 为空记忆，回合照常完成 |

两个进程内计数器只在所属进程内有效，进程重启即归零；它们不记异常文本、候选内容或问题文本。

### 按状态计数与审批冲突（数据库侧只读观测）

用 `bi_approver`（或管理员）身份的只读连接；不查聊天、Artifact 与底表载荷，输出只有状态、事件种类与 ref 形状：

```sql
-- 生命周期存量：draft / approved / superseded / revoked 各多少
SELECT status, count(*) FROM bi.approved_query_examples GROUP BY status ORDER BY status;
-- 不可变事件分布：drafted / approved / superseded / revoked 各多少
SELECT event_kind, count(*) FROM bi.approved_query_events GROUP BY event_kind ORDER BY event_kind;
-- 当前仍可被检索的样例（投影只含 approved）
SELECT count(*) FROM reporting.v_approved_query_examples;
```

- **审批冲突数**：`memory_revision_conflict`（HTTP 409）即并发审批的 CAS 冲突；它是审核 API 的稳定响应码，没有独立计数器，需要趋势时从访问日志按响应码统计。冲突不追加事件、不重放审批，属正常并发防护而不是故障。
- **label 边界**：一切指标的维度只允许状态、事件种类、domain 与稳定 ref 形状；**任何问题文本、模板内容、真实店铺/商品名称或用户 subject 都不得作为 label、tag 或样本**。
- 计数器异常的处置：`query_memory_candidate_invalid_total` 持续增长说明有形状不合法的存量样例混进了投影（应核查写入路径与 021 CHECK）；`query_memory_retrieval_failed_total` 增长说明数据库或解析层故障，记忆自动降级为无记忆，路由行为回到固定 Tool 基线，优先排查审核 DSN 连通性。

### 不得从本功能读出的结论

- 门禁关闭时零记忆读取，聊天回合与无记忆基线逐字相同；门禁开启的 26 题离线证据在空投影视图上取得，**不证明**真实模型下的检索质量。
- 检索只能改善 Tool/参数选择：身份、授权、能力、coverage、basis、来源与固定 Tool 优先级永远由服务端判定，样例不能覆盖它们。
- 版本零召回是设计行为：七个版本维度任一失配即不可见，进入人工重审，不自动迁移；撤销在下一条检索立即生效。
- 本节全部证据来自本机 `*_test` 与离线/合成环境，逐条见 [approved 查询学习记忆本地验收记录](../research/2026-09-14-approved-query-memory-acceptance.md)；HTTP 生产启用被刻意延后（与 `CONTROLLED_SQL_ENABLED` 先例一致），Task 11 统一发布门禁第 4–7 项仍 open。

## 4.8 隔离分析的观测口径（`backend/bi_agent/analysis/`，计划 Task 11 后置子项目 D）

本功能默认关闭（`ISOLATED_ANALYSIS_ENABLED=false`），且仓库没有指标后端：以下是四个定名指标的**真实现状**与观测口径，不虚构未接入的计数器。当前每次门禁开启的分析都在 `bi.query_runs` / `bi.query_artifacts` 留有完整运行与 Artifact 记录；今天能直接派生的只有**已持久化的终态与码计数**（运行终态 status、`error_code`、`termination_reason` 与 Artifact 载荷内已登记行），分析 kind 与 loader 内部细分原因**不可派生**——须按下表逐项现状区分「可派生」与「目标插桩」。接入指标后端时必须使用这些名称与标签词表，不得另起名字。

### 四个定名指标

| 名称 | 现状 | 含义 |
| --- | --- | --- |
| `analysis_requests_total{status,kind}` | **目标插桩，当前不可从现有记录派生**：status 可由 `bi.query_runs`（domain=`isolated_analysis`）按运行终态统计，但 **kind 维度当前不可派生**——请求声明的 analysis kinds 不落库，且产出零 finding 的 kind 不留任何痕迹。接入指标后端时必须新增请求级插桩（按请求声明的每个 kind 各计一次）后才能按 kind 出数；在那之前只能提供按终态派生的 `analysis_requests_total{status}` | 衡量分析面被使用的频度与结果分布 |
| `analysis_source_rejected_total{reason}` | 无独立计数器；今天能从 `bi.query_runs.error_code / termination_reason` 派生的只有**持久化后的粗粒度词表**（`error_code`：`invalid_parameters` / `unavailable` / `deadline_exceeded` / `artifact_persistence_failed`；`termination_reason` 另有 `source_quality_failed` / `contract_violation` / `coverage_incomplete` / `result_too_large` / `forbidden` / `upstream_unavailable` / `persistence_failed`）。loader 内部的细分原因（不存在/类型/版本/时间/覆盖/大小/未授权维度等）在落库前被翻译成上述通道，**不会以原词持久化**；要按 loader 原因粒度出数必须新增插桩 | 衡量模型前拒绝的分布；全部拒绝都发生在模型调用与任何写入之前 |
| `analysis_narrative_failed_total` | 无独立计数器：由成功运行 Artifact 载荷 `limitations` 中的 `narrative_unavailable` 行派生（模型超时/超 token/格式不合法/异常时记录，确定性 findings 照常发布） | 衡量无工具叙述的降级频度 |
| `analysis_unsupported_claim_total` | 无独立计数器：由成功运行 Artifact 载荷 `unsupported_claims` 数组长度派生（无证据因果/行动说法被守卫拦截的次数） | 衡量证据不足说法的拦截量 |

- **label 边界**：维度只允许运行终态、分析 kind、拒绝码与载荷内已登记码；**任何问题文本、叙述文本、row_ref 内容、真实店铺/商品名称、用户 subject 或来源 Artifact 载荷都不得作为 label、tag 或样本**。
- 计数异常的处置：集中在 `source_quality_failed` / `contract_violation` 时优先核对来源数据质量与版本血缘；集中在 `invalid_parameters` 时优先核对来源归属与类型（注意跨属主与不存在同码，不据此断言“存在但无权”）；`analysis_narrative_failed_total` 持续增长优先核对模型 provider 健康与剩余 deadline；两者都不影响确定性 findings 的正确性。

### 不得从本功能读出的结论

- 门禁关闭时零分析入口与零 Artifact 读取，聊天与 Task 11 基线逐字相同；本节全部证据来自本机 `*_test` 与离线/合成环境，逐条见 [隔离分析本地验收记录](../research/2026-09-14-isolated-analysis-acceptance.md)。
- 数值结论只来自 Decimal 纯函数与 gold set 对账；模型叙述只是已验证 finding 的总结，不能产生、改写或补充任何数字。
- 分析不创造新数据能力：它只能消费当前用户已被授权读取的既有 Artifact，来源失权后下一次分析即拒绝。
- HTTP 生产启用被刻意延后（与 `CONTROLLED_SQL_ENABLED` 先例一致），Task 11 统一发布门禁第 4–7 项仍 open。

## 5. 真实对账结果摘要

### 合成基准（已通过，冻结时刻 2026-09-08 09:00+08）

`tests/test_db.py` 的 `seed_business_case` 覆盖 2026-08-25至2026-09-08，含拆合单、跨期退款、部分退款、待处理/关闭工单；人工答案与系统输出一致：

| 断言 | 人工答案 | 结果 |
| --- | --- | --- |
| `[09-01,09-08)` 支付/商业单/ERP单 | 1000元 / 6 / 6 | ✅ 一致 |
| 客单价（总口径） | 166.67（1000/6） | ✅ 一致 |
| 退款发生 / 期间收支差 | 100元 / 900元 | ✅ 一致 |
| 同批退款率（截至09-08 00:00） | 50/1000 = 5% | ✅ 一致 |
| 商品A/B 金额/数量 | 600元/7件，400元/4件 | ✅ 一致 |
| 每日趋势（含真实0） | 500/100/200/0/200/0/0 | ✅ 一致 |
| 上期 `[08-25,09-01)` 与增长 | 500元，+500（+100%） | ✅ 一致 |
| 09-02粒度 | 商业单1、ERP单2 | ✅ 一致 |
| 未匹配成功退款 | missing_data + 数量提示 | ✅ 符合 |
| 未授权店铺/注入串 | forbidden，无副作用 | ✅ 符合 |
| 09-04真实0 vs 超覆盖日期 | 0 与 missing_data 区分 | ✅ 符合 |
| 0分母 | NULL + 不可计算说明 | ✅ 符合 |

### 真实店铺对账（待任务4.7执行）

（待试点一店一天真实 probe 与后台报表核对后填写：差异表与口径确认。）

## 6. 覆盖、业务截止与质量状态

三者是三个不同的事实，任何一个都不能拿另一个替代（`bi_agent/data_quality.py`）：

| 概念 | 存在哪 | 不能用什么替代 |
| --- | --- | --- |
| 覆盖 `covered` | `bi.sync_state.covered`（tstzmultirange） | 不能拿“同步任务成功”宣布已覆盖 |
| 业务截止 `data_as_of` | `bi.sync_state.data_as_of` | 禁止用 `last_success_at` 顶替 |
| 来源质量 `quality_status` | `bi.sync_state.quality_status` + `quality_rule` + `quality_checked_at` | 不能因请求成功自动置 passed |

质量三态的口径：

- `unknown`：从未对账。仍可以出数，但结果必须带「来源质量未核验（尚无对账记录）」。
- `passed`：本窗口内确有 `mode='reconcile'` 且 `window_kind='business'` 的批次凭证，
  且无归属未确认的平台成功退款。凭证只来自 `bi.sync_batches`，不看“任务成功过”。
- `failed`：对账发现差异（当前规则是 `unmatched_success_refunds`）。**禁止出数**。
- 历史数据的 `quality_ok=false` 在 008 迁移中统一记为 `unknown`，不是 `failed`：
  “没查过”不等于“已查出问题”。
- 口径版本 `quality_rule` 变更后，旧 `passed` 自动降级为 `unknown`，必须重跑对账才能恢复。

查询前的结构化缺口：`assess_query_coverage(conn, request)` 返回
`requested_window` / `covered_windows` / `missing_windows` / `data_as_of` /
`quality_status` / `source_batches` / `gaps` / `suggested_window`。
原请求窗口在此冻结；`suggested_window` 只是给用户的下一步建议，
系统不得代用户缩短日期。缺口的实体与店铺归因只留在服务端，
对外 `coverage.gaps` 仍只是日期段，避免缺口反成主键泄露面。

批次口径：`bi.sync_batches.window_kind` 区分 `business`（回填/重放/对账）与
`modified`（增量的修改时间窗口）。只有 business 凭证参与覆盖与质量判定；
`row_count` 为 NULL 表示当时未统计，与“确实 0 行”不同，不得合并解释。

支付归属披露：003 口径保留“关闭但已支付”的支付事实，而 `v_product_daily` 只取有效行，
所以店铺支付额可以大于商品合计。这部分差额不得静默：`attribution_gap()` 按请求窗口
算出未进商品维度的金额，并拆成关闭订单行 / 赠品行 / 无商品归属 / 其他四个**必现**分项，
随 `limitations` 一起给出（事件码 `revenue_not_attributed`）。
分项必须全列：实测模型拿不到归因时会自己编原因（把关闭行造成的差额说成
“赠品/非父项行未计入”），给出显式 0 才能排除错猜。
真实数据核对（166754，`[2026-09-01,2026-09-10)`）：披露为
「支付额中3130.51元未计入商品维度（关闭订单行3130.51元；赠品行0元；无商品归属0元；其他0元）」，
与用 `bi_reader` 独立重算的结果逐元一致。

### 指标能力门禁与来源注册表（任务 5.1，2026-09-12）

来源与能力只有一个真源：`backend/bi_agent/sources.py` 的代码内有限注册表。查询按
「先授权 → 解析逐店逐指标来源与能力 → 核对时间口径 → 读覆盖/质量 → 才跑金额 SQL」
的顺序执行，任何一步不过就不出数，也不拿 0 或空结果冒充“确实没有”。

| 平台 | 订单来源 | 支付口径 | 业务时间认证 | 能力上限 |
| --- | --- | --- | --- | --- |
| `fxg` | `erp.trade.list.query` | `platform_payment/v1` | `certified`（逐元对账 + 0/9414 行越界） | 全部 9 项 |
| `jd` `kuaishou` `wxsph` `wsxc` | `erp.trade.list.query` | `platform_payment/v1` | `unmeasured`：同通道同参数但未逐店对照 | 全部 9 项（仍需逐店取证） |
| `tb` `tm` | `erp.trade.outstock.simple.query` | `erp_outstock_payment/v1` | **`disproved`**（实测 83/8367 行 `paid_at` 早于窗口起点） | 全部 9 项（出库口径，非平台账单） |
| `pdd` | 出库通道（官方交易接口按文档排除拼多多） | 无（不接入） | `disproved` | **仅 `erp_documents`** |
| `1688` `alibabac2m` 及未登记平台 | 无 | 无 | — | 无：不回退交易源 |

业务时间认证是三态，因为“没测过”与“测了不成立”后果不同（Task 5.2c 已消费）：
`disproved` 的店不得给支付窗口类结果，返回 `coverage_time_basis_unverified` 并拒答；
`unmeasured` 的店照常出数但必须披露为可观测样本。`erp_documents` 是单据计数、
不是支付窗口主张，所以两种情况都只披露不拒答（设计 §4：拼多多单据数仍可查）。
认证目前是按通道登记的代码事实；逐店与后台账单对照的证据到齐前不先建“无人写入”的
登记表。金额口径与逐指标能力仍按店由对账证据开通，不拿抖音的结论复制给别的平台。

能力标签取值与指标同名，只能由 `sync capabilities` 从 `bi.sync_state` 的**逐来源对账证据**
推导（同步成功、档案存在、上游回空均不算证据；`quality_rule` 过期视同未核验）。
表中“能力上限”只是该平台的天花板，不等于已开通：上限 × 逐源证据 × 覆盖三者同时成立才出数。
三类缺口分开归因，不得互相冒充：

| 情况 | 结果 | 恢复策略 |
| --- | --- | --- |
| 平台未登记来源 | `source_not_onboarded` →「N 家店铺的来源尚未开通」 | 0 次追加：重跑不会开通来源，换指标也不会 |
| 来源已登记但指标未授予能力 | `capability_unavailable` →「N 家店铺缺少 X 的已核验能力，未执行金额查询」 | 0 次追加：只有对账 + `capabilities --apply` 能开通 |
| 付款时间口径未认证（拒答或只披露） | `coverage_time_basis_unverified` | 0 次追加：需要与后台账单/业务日期对照登记 |
| 能力已授予但窗口有缺口 | `coverage_incomplete` / `data_as_of_unknown` | 0 次追加，给建议窗口 |

指标名只出现能力名（固定词表），店铺主键不进口径文本；能力缺失时覆盖保持「未评估」
（`coverage.start` 为 NULL），不伪装成“缺覆盖”。混合范围不悄悄删店后冒充全量成功：
只要一家缺能力，整个请求就报缺失组，原请求店铺与窗口不变。拼多多按 2026-09-12
决定永久处于能力未开通状态，全平台汇总必须把它列成缺失组而不是 0。

### 公共覆盖按「店铺 × 来源 × 实体」取交集（Task 5.2b）

请求公共可覆盖范围 = 请求窗口 ∩ 每一个必需依赖的已覆盖区间；`missing_windows` =
请求窗口 − 公共范围；`suggested_window` 只能取自公共范围。旧实现把各店已覆盖段取
**并集**，会把“甲店有这两天、乙店有那两天”拼成一个谁都不完整的建议窗口，拿着它
再查仍缺数。配套约束：

- 一个依赖去重后只查一次，按 `(source, entity)` 分组批量取状态，不按店铺逐条往返；
  区间边界仍在 SQL 里裁剪，Python 只做交集与求差。
- 不相干的旧通道状态行不再参与判定：出库通道平台的依赖只看出库源，
  交易通道下残留的“完整区间”既不能补覆盖也不能混进 `source_batches`。
- 任一依赖没有推进 `data_as_of`，整体共同截止就是未知；缺口按实体、店铺与**具体来源**
  归因（`source` 与 `shop_id` 一样只留在服务端，不进口型文本）。
- 本指标集合内若某店某指标没有任何可用依赖（平台拿不到该口径），整段窗口按未知处理，
  不会因为“没有依赖”而算出一个假“完整覆盖”。

> 未定事项：2026-09-11 拼多多的 46/46 无金额样本未曾标明出自哪个通道。逐店开启
> `erp_documents` 前必须重跑一次取证，确认来源、覆盖与时间口径，不得拿本表推定。
> 旧的 pdd 交易通道 `sync_state` 行已被 pdd→出库的路由抛成孤儿：它既不参与覆盖判定
> 也不删除（删就是抹掉取过数的痕迹），台账处置归 Task 5.3/发布清单。

### 口径凭证全链路与混口径禁合并（Task 5.4）

每个结果都携带逐店逐指标的口径凭证：内部形状是
`{shop_id, metric, source, basis, time_basis, metric_version}`，公开投影只保留
`{shop_ref, metric, basis, time_basis, metric_version}` ——**接口方法名与 ERP 主键都不外发**。
模型因此看得见“这些数是什么口径”，但拿不到标识符。

| 规则 | 结果 |
| --- | --- |
| 同一请求范围内出现互不兼容的 `(basis, time_basis)`，分组是 `total`/`day`/`product` | `invalid_parameters` + `basis_incompatible`，提示按店铺分列；不产出任何数字 |
| 同上不兼容但 `group_by=shop` 且 `basis_policy=separate` | 每店一行、各带自己的口径；**永不产出跨口径合计、增长率或排名** |
| `basis_policy=strict`（默认） | 连分列也要先确认口径：模型必须显式改用 `separate` 才能拿到分列结果 |
| 上期与本期由**不同来源**覆盖（换过通道） | 拒答比较：两期差额是口径变化，不是经营增长 |
| 单店结果 | 同样附口径；否则“淘系的单据数”会被当成按付款时间的全平台口径 |

指纹与血缘纳入 `source_registry_version`、`basis_signature`（`指标\|口径\|时间归属`
去重签名，不含主键）、`quality_rule`：登记新来源、改通道口径或升级对账规则后，旧结果
不允许被复用，旧 Artifact 仍按当版口径原样可读。系统提示词同步要求：销售额先确认期间与
口径、平台对比只展示可比结果、同名指标不得自动同义化。

### 可量化限制代替整次拒答（Task 5.3）

以前“窗口里只要有一条未匹配的平台成功退款”就把整次查询打成 `missing_data`，
实测淘系是 659/717 未匹配——等于拿另一个门禁继续拒答。现在它们都是**披露**：

| 叞制 | 行为 | 不得越过的线 |
| --- | --- | --- |
| 未匹配退款 | `refund_amount` 照算（canonical 平台成功退款无论 matched 全部计入）+ 披露「未匹配 N 条/共 T 条，金额 X 元，比例 R」；0/0 时比例是**未知**不是 0% | 不得说这些退款全属于本期支付；分母固定为同窗口 canonical 成功退款，失败/待处理/重复工单排除；不拿各店比例求平均 |
| 同批退款率 | 返回已匹配部分可算的值，并标「仅含已匹配退款（N 条未匹配未计入）」 | 不得报作“完整同批退款率”，也不得把未匹配退款猜配本期 |
| 未认证支付 | `paid_amount` 不因退款问题拒答，但必须报笔数：总额、金额未定笔数、有原始金额笔数与已知合计 | orphan / undetermined 不能无声消失；金额未知保持 NULL，不当 0 |
| 商品归属差额 | 保留 `revenue_not_attributed` 四分项披露 | 同上：分项必现，不给模型自己猜原因的空间 |

质量规则升到 `kuaimai-reconcile/2`：未匹配不再独自把来源置 `failed`，但仍写
`quality_reason = unmatched_success_refunds` 便于取证。**旧规则因 unmatched 被标
`failed` 的店不会自动解禁**：必须重跑 `reconcile` 逐源取证后才能恢复，不批量清空。
金额冲突、分页损坏等真实失败仍是硬门禁。

### 多平台来源就绪清单（2026-09-11 历史快照）

原则：没有实测的接口不填“可用”。下表于 2026-09-11 由真实账号逐店取证，
取证方法：逐店调 `erp.trade.list.query` 比对单头与行级 `payAmount` 存在率，
同步后比对 `bi.sync_state.quality_status` 与覆盖区间。

| 平台 | 店数 | 订单 | 成本 | 上架实际价 | 实物库存 | 渠道库存 |
| --- | --- | --- | --- | --- | --- | --- |
| 抖音 `fxg` | 3/4 | **已对账通过** | 未执行 | 未执行 | 未执行 | 未执行 |
| 京东 `jd` | 1/1 | **已对账通过** | 未执行 | 未执行 | 未执行 | 未执行 |
| 快手 `kuaishou` | 1/1 | **已对账通过** | 未执行 | 未执行 | 未执行 | 未执行 |
| 视频号 `wxsph` | 1/1 | **已对账通过** | 未执行 | 未执行 | 未执行 | 未执行 |
| 微购相册 `wsxc` | 1/1 | **已对账通过**（量极小） | 未执行 | 未执行 | 未执行 | 未执行 |
| 拼多多 `pdd` | 0/13 | **不接入**：交易接口可查但单头与行级均无 `payAmount`/`payment`（实测 46/46 单无金额、行 `oid` 也缺失），不得纳入支付指标。2026-09-12 用户决定放弃方舟授权，该状态为最经定性；仅 `erp_documents` 单据口径在逐店覆盖/质量成立时可答 | 未执行 | 未执行 | 未执行 | 未执行 |
| 淘宝 `tb` | 0/10 | **不可用**：`erp.trade.list.query` 回 `code=33 非法店铺编码`（userId 与 shopId 均被拒），本 appKey 未开通淘系交易查询授权 | 未执行 | 未执行 | 未执行 | 未执行 |
| 天猫 `tm` | 0/2 | **不可用**：同上（`非法店铺编码`） | 未执行 | 未执行 | 未执行 | 未执行 |
| 淘工厂 `alibabac2m` | 0/1 | **不可用**：同上 | 未执行 | 未执行 | 未执行 | 未执行 |
| 1688 | 0/1（未启用） | **不可用**：`deadline=2025-01-21` 已过期且未启用 | 未执行 | 未执行 | 未执行 | 未执行 |

已接入的 7 家店均已回款逐元核对（见
`docs/superpowers/research/2026-09-11-production-deploy-and-sync.md`）。
所有店的 `aftersales_cohort` 仍为 `unknown`（同批 cohort 窗口不写批次凭证）；
库存与上架价、跨渠道 SKU 映射属计划 Task 6 / 9 / 10，本表不因代码存在而改为“可用”。

## 7. 版本化运行契约与复现等级

`revision` 只表示状态推进，**不承担数据版本语义**（计划 Task 3）。哪一版口径、
哪一版名称目录、哪几批来源数据支撑了这次结果，由 `bi.query_provenance` 显式记录：
`template_id/template_version`、`metric_version`、`schema_version`、`catalog_version`、
`mapping_version`、`policy_version`、`graph_version`、`source_batches`、`data_as_of`。

请求身份落在 `bi.query_runs`：`root_request_id`、`request_fingerprint`、`recovery_count`、
`termination_reason`。指纹 = 授权主体 + 授权店铺范围 + 规范化参数 + 上述版本的 sha256，
因此**目录版本或数据截止一变，同一问题不会再命中旧结果**；换一个主体或授权范围也不会命中
（否则等于越权读取他人结果）。

需要留证的 SQL 与参数只进 `bi.query_diagnostics`，通过 `run_id` 引用；
不写进模型消息，也不写进普通事件文本，且**不建 reporting 视图**。

复现等级（如实声明）：本契约保证**已存 Artifact 可复现读取**——给定 run 与 artifact 引用，
能取回当时发布的载荷及其版本与来源批次。在没有版本化事实库之前，
**不承诺任意历史 SQL 重跑一致**：`source_batches` 只能指回当时的同步批次，
不能重建当时的上游状态。
