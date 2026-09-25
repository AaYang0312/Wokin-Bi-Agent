# S3 TRADE_CLOSED 口径决策落地验收记录（默认建议，待所有者追认）

日期：2026-09-25
计划：`docs/superpowers/plans/2026-09-25-first-two-workflows-launch.md` §2 S3
实现基线：`main@d699c71`（S1/S2 已提交）+ 当前未提交工作树（唯一写者，单切片）
关联：`docs/superpowers/research/2026-09-25-s2-acceptance.md`（S3 是其 §7.1「查询侧门禁翻转」的兑现）、
`docs/superpowers/specs/2026-09-12-multi-source-metrics-design.md` §2/§4/§5、
`docs/superpowers/research/2026-09-12-taoxi-onboarding.md` §1.3/§5.1

## 0. 结论

**S3 按计划默认建议实现并全部测试门禁通过。**口径内容（**待所有者追认**）：

1. **tb/tm（`local_filter`）支付口径在查询侧剔除「结构性证明的关闭后退款单」的已核验支付**。
   结构性证明的谓词（三条同时成立，宁可漏剔不可错剔）：全部含该商业号的已入库单据均已关闭
   （`bi.orders.active=false`）＋至少存在一张含该号的关闭单据＋存在同商业号、平台成功且
   canonical 的退款（与 `refund_amount` 分母同一谓词集合）。
2. **关闭未退款 ≠ 关闭已退款**：关闭但无成功 canonical 退款的单，其支付留在支付口径里；
   活动单据仍在的商业号不满足「已全额关闭」，不剔除（拆分退款工单只确认一笔 canonical，
   支付只剔除一次）。
3. **退款保持单独可见**：`refund_amount`（发生额、含未匹配披露）与 `cohort_refund_rate`
   （同批分母不减关闭后退款单）一律不动；未匹配退款披露照旧。
4. **`paid_amount − refund_amount` 只叫期间收支差额**：指标定义、披露文案与诊断都写明
   「不是净支付，不是净利润，也不是同批净收入」。fxg 的 `certified` 支付口径语义不变；
   拼多多永久排除不变；PII 白名单不变；missing-as-missing 不变；集合查询预算不变。

## 1. 形态决策：查询政策，而非事实改写（计划红线内优先项）

任务书要求「若 canonical facts 需要改活动语义，才提供同版本重述通道；能用查询政策作用于
存量历史且不改写事实的，优先查询政策，并写明 replay 是否仍必需」。核实结论：

- 2026-09-12 设计 §2 合并时已采用「保留 `active=false`，但 `paid_at` 非空且
  `raw_pay_amount>0` 的关闭单参与支付归集、行头交叉核验与退款匹配」——`bi.order_payments`
  里**已经**包含关闭单的支付事实，`reporting.v_shop_daily` 直读该表。即：canonical facts
  不需要改活动语义，S3 只需改变「口径怎么读这些事实」。
- 因此本切片选择**查询侧政策**：新增 026 取证视图 + 查询路径逐店按注册表剔除。存量事实
  零改写 ⇒ **不需要重述通道、不需要 replay**（`apply_trade` 严格 `>` 版本守卫、同版本幂等
  语义原样保留）。旧口径数字与新口径的差值由结果的 `closed_refunded_excluded` 诊断
  （笔数/金额/币种）逐项给出，满足计划「旧数字对比回归：差值必须逐项可解释」。
- 平台作用域：`sources.payment_caliber_excludes_closed_refunded()` 只对
  `time_certified == "local_filter"` 的登记平台为真（当前即 tb/tm）；fxg 连剔除查询都不发起。

## 2. 改动清单（smallest change）

- `backend/sql/026_closed_refunded_payment_caliber.sql`（新迁移，编号 026，001–025 零改动）：
  新建 `reporting.v_closed_refunded_payments`（shop_id, commercial_id, paid_at, day, amount,
  verified），字段集为 `v_payments` 子集加归属日，无 PII；授权 bi_reader/bi_app；
  `CREATE OR REPLACE` 写法可重复执行。必要性：查询身份（bi_app）只能读 reporting 视图，
  而关闭状态与退款取证事实只在 bi.* 底表——没有这条视图，查询政策无法执行。
- `backend/bi_agent/sources.py`：新增 `payment_caliber_excludes_closed_refunded()`；
  `METRIC_VERSION → metrics/2026-09-25.1`（口径变了，旧指纹的缓存/复用必须失效，
  旧 Artifact 原样可读并标旧口径）；注册表注释与模块文档同步。
- `backend/bi_agent/data_quality.py`：**门禁翻转**——`local_filter` 不再与 `disproved`
  同层拒答支付窗口类指标，降为披露层；出数前提是其后既有的覆盖完整/截止/质量门禁
  全部成立（没有真实 local_filter 覆盖就在覆盖门禁处照旧 fail closed）。`disproved`
  的拒答分支原样保留（patch 注入合成登记的用例钉住）。新增剔除事实读取
  （`CLOSED_REFUNDED_PAYMENTS_SQL` + `ClosedRefundedPayments` + `closed_refunded_payments()`
  + `describe_closed_refunded_exclusion()` + `describe_previous_closed_refunded_exclusion()`）。
- `backend/bi_agent/metrics.py`：`_query_in_transaction` 对 local_filter 店铺在金额 SQL 之前
  读取剔除事实（走 `_set_query_budget` + `_fetch_capped`，超限降 unavailable）；支付口径
  `paid_amount/paid_orders/cash_difference` 按 (店铺, 支付日) 逐项扣减，`aov` 调整后重算；
  披露文案（固定句式 → 码 `closed_refunded_excluded`）+ 同名诊断；
  `METRIC_DEFINITIONS` 更新（paid_amount / paid_orders / cash_difference / cohort_refund_rate）。
  **独立复审 P1 修复**：`compare=previous_period` 分支原先调用 `_period_rows` 时未传上期剔除
  事实，同店两期会混用两种口径、把剔除量伪造成环比下滑；现为本期做过剔除的店铺另取
  **上期窗口**的剔除事实（同样 `_set_query_budget` + `MAX_ROWS+1` 截断护栏，超限抛
  `_BudgetExhausted`/`_RowsTruncated` 降级 unavailable），传入上期 `_period_rows`，
  上期被剔的量由「上期支付口径已剔除…」（同一稳定码）单独披露。commerce 侧无同类问题：
  其上期比较只作用于商品面取值，支付口径没有上期对比路径，且支付口径 tab 仅
  `certified` 平台可进（本剔不适用）。
- `backend/bi_agent/commerce/graph.py`：支付面读取后应用同一剔除政策
  （`_apply_closed_refunded_exclusion`，同一文案/诊断）；`_attributed_reason` 的
  `coverage_time_basis_unverified` 重映射扩到 `local_filter`（S2 复审遗留 P2-③）。
- `backend/bi_agent/commerce/metrics.py`：`COMMERCE_METRIC_VERSION → commerce-metrics/2026-09-25.1`。
- `backend/bi_agent/runtime/models.py`：`_TIME_CERTIFICATIONS` 闭集补 `local_filter`
  （S2 复审遗留 P2-②；S3 让该状态可达，载荷校验必须认识它），`_DIAGNOSIS_KEYS/_FIELDS`
  登记 `closed_refunded_excluded`。
- `backend/bi_agent/agent.py`：`_TIME_CERTIFICATION_NOTE["local_filter"]` 口径澄清文案
  加入 S3 剔除与「完整支付窗口仍不宣称」。
- `backend/bi_agent/business_query/nodes.py`：剔除披露文本 → 稳定原因码映射。
- 文档：`docs/metrics.md`（§3 指标表、§4.3 NOT MET 边界、§4.4 验收映射沿革、§6 注册表与
  四态说明）、`docs/runbook.md`（付款时间口径三档后果）。

## 3. 测试（红先行）

新增 25 个用例，先红后绿（实现前运行确认 FAIL/ERROR，实现后 GREEN；复审 P1 修复另做
 revert 验证：临时移除上期剔除传参后新用例变红 `200 != 130`，恢复后转绿）：

- `tests/test_db.py::ClosedRefundedPaymentCaliberTests`（5）：026 谓词矩阵（活动单+canonical
  退款不剔、未成功退款不剔、关闭未退款不剔）、拆分工单只剔一次、活动单据在场的商业号不剔
  （「已全额关闭」未被证明）、视图列形=无 PII 闭集且 bi_app 可读、迁移可重复执行。
- `tests/test_multi_source_metrics.py::S3ClosedRefundedCaliberTests`（10）：真实库 + reader
  身份的查询行为——剔除后 paid=200−70=130 且关闭未退款的 30 留在口径、paid_orders=2、
  refund=70 单列、cash=60、诊断 `{excluded_count:1, excluded_amount:"70"}` 与旧数字差逐项
  可解释；`period difference` 定义点名「不是净支付/净利润」；day 分组按支付日扣减
  （09-02:100 / 09-03:30；现金差 100 / −40）；aov=65（130/2）；同批率 0.35=70/200
  （分母不减）；fxg 同形状种子不做剔除（150）且无诊断；可答+披露码；**无覆盖 fail closed**
  （coverage_incomplete，不冒充零）；**部分覆盖拒绝整窗**；**上期对比同口径**
  （本期 50 / 上期 200−70=130 / 差额 −80，上期剔除量单独披露； reverted 修复时该用例变红：
  paid_amount_previous 退回未剔除的 200，钉死复审 P1）。
- `tests/test_core.py::S3ClosedRefundedCaliberTests`（10）：注册表谓词（tb/tm 真，fxg/jd/
  pdd/未登记假）、口径澄清文案、门禁翻转（fakeconn：local_filter→披露不阻断）、
  disproved 仍阻断（patch 注入合成登记，防回归）、剔除算术（total 行+诊断+文案）、
  零剔除不出披露、fxg 不发剔除查询、运行时载荷闭集接受 `local_filter` 且禁止冒充
  `certified`、`_attributed_reason` local_filter 归因、披露→稳定码映射（business_query 与
  commerce 两侧）。

既有行为变化用例（S3 授权范围内的语义翻转，逐条按原意图改写，非放松）：
`test_multi_source_metrics`（local_filter 覆盖齐全时从拒答改为可答+披露）、
`test_core::PartitionedProductRankingTests`（淘系分区以 `observable_sample` 发布而非整组
排除；空商品两分区照发且无排除→complete）、`test_product_ranking`（三分区发布；前移窗口
用例中淘系店因**覆盖缺口**而非口径原因留场外，缺口窗口逐项可查）、`test_commerce`（淘系店
样本发布、跨口径合计照旧拒发；版本字面量更新）、`test_operator_workflows`（五店场景：淘系
样本发布、pdd 归因不变、合计行三指标仍 null）、`test_runtime`（指标定义字面量改引常量）。

## 4. 门禁结果（串行执行，真实调用仅测试库）

| 门禁 | 结果 |
| --- | --- |
| 新增用例红→绿（25 个在实现前 FAIL/ERROR，实现后 GREEN；P1 修复另做 revert 验证） | PASS |
| `uv run --locked --env-file ../.env.test python -m unittest discover -s tests -t .` | **1757 tests OK**（S2 基线 1732 + 25） |
| `python -m unittest tests.test_db tests.test_runtime_db tests.test_monitoring` | **307 tests OK，0 skip** |
| `python -m tests.acceptance --offline` | **28/28 pass**（Q21/23/26 按本切片更新为可答+披露；Q22/24/25/27/28 拒答边界原样通过；Q06/Q20 上期对比仅 fxg，不触发剔除路径） |
| `cd frontend && npm test` / `npm run build`（里程碑证据；本轮未改前端） | 11 files / 177 tests passed；build PASS |
| `git diff --check`；`git diff --cached --name-only` 为空 | PASS / 空 |
| 测试库 schema：026 已应用到 `bi_agent_test`（仅测试库，幂等脚本，未触碰生产/预生产） | PASS |

## 5. 红线核对

- fxg 语义不变（certified 口径无剔除，连查询都不发起——fakeconn 用例钉住）。
- 未匹配退款披露、`unmatched_refunds`/`matched_cohort_only` 诊断原样（Q23 通过）。
- 时间窗口规则不变：覆盖交集、multirange 缺口、`data_as_of` 共同截止、上期换来源拒比
  全部未动；无覆盖/部分覆盖的淘系支付查询在覆盖门禁处 fail closed（用途例钉住）。
- missing-as-missing：剔除只减已存在的已核验事实，无补零、无替位。
- PDD 永久排除不变：`disproved` 拒答分支与 `capability_unavailable` 归因路径均有用例。
- PII 白名单不变：026 视图字段集为 `v_payments` 子集+归属日，information_schema 用例钉住。
- 固定集合查询预算：剔除读取走既有 `_set_query_budget` + `_fetch_capped`，超限降 unavailable。
- 受控探索（exploration）对 time-basis 披露/阻断的拒答路径未放宽——查询政策不开绕过口子。
- 未改任何既有迁移（001–025 零改动）；无依赖变更；未 stage/commit/push/merge；
  未读取/打印任何 `.env*`/`auth.json`；未执行任何真实 API/sync/replay/模型调用；
  数据库操作仅限本机 `bi_agent_test`。

## 6. 诚实边界（本文件证明了什么、没证明什么）

- 证明了：在合成测试库上，剔除谓词的结构正确性、查询政策的逐日/逐店算术、门禁翻转后的
  可答+披露形状、无覆盖 fail closed、幂等迁移、旧新口径差值可解释性，以及全量回归绿。
- 未证明（全部 FAIL/挂起，如实记录）：
  1. **所有者追认**（下节 #1）——当前口径是计划默认建议，未获所有者确认前不得当作最终口径。
  2. **live 覆盖**——真实淘系店铺还没有任何 `local_filter` 支付窗口回填覆盖；真实库上
     淘系支付查询在覆盖门禁处保持 fail closed（这正是「无真实覆盖=live 保持 fail closed」
     的设计行为）。等真实回填+对账完成后再评估披露形态的实际数字。
  3. **live 重述**——本切片不需要重述（查询政策、零改写），因此也未在任何真实库上执行
     重述/回填/replay；真实库的历史数字语义随本代码上线的切换点，应由部署方在发布说明里
     标注（上线前后的同一窗口数字会按 `closed_refunded_excluded` 差一个可解释的量）。
  4. 未调用真实模型；`--offline` 不证明模型理解；未触碰平台对比支付口径 tab 的
     `certified`-only 成员资格（S2 §7.1 的「tab 成员资格讨论」仍挂起）。

## 7. 待所有者追认 / 后续

1. **【所有者追认】S3 口径默认建议**：tb/tm 支付口径剔除「结构性证明的关闭后退款单」、
   退款单列、`paid−refund` 只叫期间收支差额；同批退款率分母不减。若所有者改选
   taoxi-onboarding §5.1 的 A（保持现状+声明）或 B（改判活动性），需重开口径评审并
   另排改判重述专项（B 需要同版本重述通道+回滚测试，本切片未实现——A/B 未被选用）。
2. **【live 覆盖挂起】**：真实淘系店铺完成 `local_filter` 支付窗口回填与逐源对账、
   `capabilities --apply` 开通后，淘系支付指标才会在真实环境可答；届时应复跑 S2 探针
   确认闭集类别仍解释全部差集，再评估披露数字。
3. **【live 重述挂起】**：本形态无需重述/replay；若所有者改选 A/B 口径，则按 §2 的
   设计契约补同版本重述通道并测回滚（本切片范围外）。
4. 平台对比支付口径 tab 成员资格（淘系是否进 tab）仍按 S2 §7.1 挂起，待所有者讨论；
   当前 tab 仍只收 `certified` 平台（Q28 原样通过）。
5. 遗留（非本切片引入）：runbook 顶部迁移清单停在 022，023–025 未回填进该清单
   （026 同样未列）；建议后续统一补一次文档链。

## 8. 独立复审与修复记录（2026-09-25，复审 P1 已修复）

- **P1（已修复）**：`metrics.py` 的 `compare=previous_period` 分支原先调用 `_period_rows`
  时未传上期剔除事实——本期行新口径、上期行旧口径，`paid_amount_change/change_ratio`
  会把口径漂移伪造成业务下滑且无披露。修复：为本期做过剔除的店铺另取**上期窗口**的
  剔除事实（同样 `_set_query_budget` + `MAX_ROWS+1` 截断护栏，超限抛
  `_BudgetExhausted`/`_RowsTruncated` 降级 unavailable），传入上期 `_period_rows`；
  上期被剔的量由「上期支付口径已剔除…」披露单独给出（同一稳定码
  `closed_refunded_excluded`，business_query 与 commerce 两侧映射均可归因）。新增回归
  用例 `test_previous_period_comparison_applies_the_same_exclusion`（本期 50 / 上期
  200−70=130 / 差额 −80），并做 revert 验证：临时移除上期剔除传参后该用例变红
  （`paid_amount_previous` 退回未剔除的 200），恢复后转绿。commerce 侧核实无同类问题：
  其上期比较只作用于商品面取值（`_window_value` 读行级聚合），支付口径没有上期对比路径，
  且支付口径 tab 仅 `certified` 平台可进。
- **P2（仅记录，不在本轮修复范围）**：① 商品面（`product_paid_amount`/分区排行、
  `attribution_gap` 读原始 `v_payments`）对 local_filter 店铺仍含关闭后退款单支付且无
  专项标记——支付面与商品面本就是两个事实集合（同一报表内不同列、不相加），后续如需
  对齐口径应作为独立小切片，先定披露形状；② `cash_difference` 的「期间现金差额」措辞
  可再精确（退款侧不随剔除减少，公式与三重否定已在定义与披露中）；③ 复审把工作树里
  `.agents/skills/.../SKILL.md` 的子代理模型默认值改动列为「无关编辑」——该改动是本切片
  开工前已存在的操作者未提交修改，任务书明令保留不回滚，非本写入者所为，提交时应按
  既有惯例单独处理（不并入 S3 提交）。
- 复审为只读（无 shell），本节门禁数字由写入者重跑：全量 1757 OK、DB 套件 307 OK
  （0 skip）、离线验收 28/28、前端 177+build PASS、`git diff --check` 干净、无暂存。
