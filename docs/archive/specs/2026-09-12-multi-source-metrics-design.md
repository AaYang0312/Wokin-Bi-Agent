# 多来源指标与口径契约修订

日期：2026-09-12。范围：统一开发主线，修订原路线图 Task 5 与 Task 11。会话 API、LLM provider 适配、推广测算、前端布局及部署路线不变。

本文件是后续实现契约，**不代表下述查询能力已经上线**。对应 [原实现计划](../plans/2026-09-07-ecommerce-bi-agent.md)；运营工作流计划继续沿用，并依赖本契约。历史证据见 [淘系实测报告 §1.6](../research/2026-09-12-taoxi-onboarding.md)。

## 1. 合并基线与证据边界

合并顺序：`main@06a5cc2 → codex/chat-fastapi-refactor@aa89bfa → taoxi/outstock-sync@f814a47`。`codex/query-runtime-graph@ffd8844` 已经由 `9a4cce9` 合入重构分支，随之进入 main，禁止再次搬运同一实现。

保留 `backend/` 目录和 React/FastAPI 架构；淘系的 `sync.py`、测试及 pytest 开发依赖合入对应 backend 文件，不恢复根目录 Python 项目。保留双方完整提交历史、重构中的回填时钟注入、分页校验、支付防降级、商品身份、运行版本与恢复机制。后续变更从更新后的 main 派生短分支，不继续在旧淘系和重构分支各自修改同步逻辑。

报告来自淘系分支及测试库，不能直接当作合并后真实库的结果：

- §1.6 查询窗口 `[08-13,09-06)`：12 家淘系订单源覆盖查不到；未匹配成功退款 659/717，抖音对照 0/255。
- §1.3 的 30 天数据：608 张关闭单、621 条 orphan 支付、头金额 89,669.39 元；这是另一窗口，不和 §1.6 的退款统计混算。
- 重构分支已在 `_load_orders`、`_determine_payment`、`refresh_aftersale_matched`、`unmatched_commercials` 使用 `active OR (paid_at IS NOT NULL AND raw_pay_amount > 0)`，不能在合并时退回只看 active。
- 新架构仍有单源 `ENTITY_SOURCES`、未匹配退款硬门禁、未匹配即质量 failed、实体级而非指标级 capabilities；四项风险仍须 Task 5 完成。
- §3 的出库时间参数实测异常属于覆盖语义风险：有单不代表完整支付时间窗口；不得仅换一个 source 字符串就宣布完成。

## 2. 关闭已付款单决策

采用“保留业务活动性，独立认证支付事实”的方案：保留 `active=false`，但有 `paid_at` 且 `raw_pay_amount > 0` 的关闭单可参与支付归集、行头交叉核验和退款匹配。**允许参与认证不等于自动 verified**；缺金额、金额冲突、拆合单重复和防降级规则继续生效。

这是报告候选 C 的明确修正版：支付事实也恢复，不能保留“只修匹配率，不改 GMV”的表述。拒绝候选 A 的系统性少计；不采用候选 B 把所有已付款关闭单改 active，避免改变销量、ERP 有效单和商品行口径。取消/关闭且未付款的单仍不能凭空产生支付。

定义保持：`paid_amount` 是退款前的已验证支付额；`refund_amount` 是按退款发生时间的退款额；`cash_difference = paid_amount - refund_amount`。三者不能统一叫“净支付”。商品仅有效父行，关闭支付未进商品合计的差额继续通过 `revenue_not_attributed` 披露。

历史修复必须重放订单规范化、重建支付事实、刷新退款匹配及重新核验质量；普通增量遇同版本不会自动修复。沿用现有 replay 允许同版本重新规范化的入口，禁止降低全局版本守卫。按店/来源记录修复前后关闭单数、orphan 数及金额、支付总额、未匹配退款数及金额、补拉 cid 数与批次。先独立测试库验证，再按部署流程处理真实库；本次合并不声称已修复历史库。

## 3. 唯一来源注册表

新增 `backend/bi_agent/sources.py`，同步、覆盖、质量、能力、指标契约共同消费。采用代码中的有限注册表，不建动态插件系统或通用配置中心。

| 平台/阶段 | 订单来源 | 支付口径 | 能力边界 |
| --- | --- | --- | --- |
| fxg 与已验证的 jd/kuaishou/wxsph/wsxc | erp.trade.list.query | 按平台核验的支付口径，fxg 使用 `platform_payment/v1` | 逐店证据决定；不能把 fxg 的认证复制给其他平台 |
| tb/tm | erp.trade.outstock.simple.query | `erp_outstock_payment/v1` | ERP 出库来源已验证支付；不是平台账单，不承诺支付时间窗口完整 |
| pdd（**决定不接入支付**） | 已核验的单据来源 | `erp_document/v1` | 仅 `erp_documents`，不得授予 paid_amount/paid_orders/aov/product_paid_amount/cash_difference/cohort_refund_rate |
| 未知平台、无店铺档案、未登记来源 | 无 | 无 | fail closed；禁止回退交易源后把空响应标为完整覆盖 |

不猜方舟方法名；未登记、未授权时解析支付依赖直接返回能力不足。2026-09-12 用户决定放弃拼多多方舟授权（见
[范围决定](../research/2026-09-12-drop-pdd-onboarding.md)）：注册表**不再有 pdd 支付分支**，也不保留
「开通后再登记」的待办；拼多多支付依赖永久解析为 `capability_unavailable`，全平台汇总必须把它列进
`excluded_scope`，不得算进覆盖分母。所有售后目前使用 `erp.aftersale.list.query`；退款发生与 cohort 仍是不同 entity。

接口契约（服务端对象；不得由模型指定 source）：

```python
@dataclass(frozen=True)
class SourceBinding:
    shop_id: str
    platform: str
    entity: str
    source: str
    basis: str
    time_basis: str
    coverage_certified: bool

# 查询只读 reporting.v_shops；同步可读 bi.shops。
def resolve_metric_sources(shop, metric: str) -> tuple[SourceBinding, ...]: ...
def resolve_order_source(shop) -> str: ...
```

`shop` 由服务端加载，包含 platform、capabilities 和已审核的来源/时间口径登记。同店同一指标同一期间只选确定的有效来源；来源切换按有效期间分段，禁止新旧来源重叠相加。现有 `(shop_id, erp_id)` 订单主键与 `(source, entity, shop_id)` 水位主键保留。新增源先规定回放/替代策略，不把 source 列存在误解为事实可以双写累加。

## 4. 覆盖与 capabilities 真门禁

先授权，再解析逐店逐指标依赖，再核对能力和时间口径，之后读取覆盖/质量，最后执行金额 SQL。同一 REPEATABLE READ 事务内完成，沿用 deadline 与行数约束。

- `erp_documents` 依赖已核验的单据时间覆盖；`paid_amount/paid_orders/aov` 依赖支付时间覆盖；商品数量/金额分别消费数量/分摊能力。
- `refund_amount` 只需退款发生源，不强制依赖订单匹配；`cash_difference` 需支付和退款发生；cohort 需支付及 cohort 追溯证据。
- 按 `(source, entity, time_basis)` 分组批量取状态，每店只读取该指标实际依赖的源，不要求淘系还存在交易源状态。
- 请求公共可覆盖范围 = 请求范围与**每个必需的店铺×来源×实体区间的交集**。保留 multirange 孔洞，不取各店区间并集，也不取 min(start)/max(end) 凑成连续覆盖。
- `missing_windows` = 请求范围减公共覆盖；`data_as_of` = 每个依赖项已完成截止的最小值，任一缺失则未知；`source_batches` 只收实际使用的来源批次。
- 当前及对比期间都核对上述条件。缺源/缺日不能补零、不能自动删店或缩短期间；可给出明确的分组结果或建议下一次查询的范围。
- 出库 `pay_time` 不足以证明支付日完整：`coverage_certified=false` 时不开放“完整支付窗口”结果。保留入库事实，披露为出库来源可观测样本；完整窗口能力须以与后台账单/业务日期对照的证据登记。禁止用 observed min/max 推断完整覆盖。
- capabilities 从现有实体标签升级为指标能力标签：`erp_documents`、`paid_amount`、`paid_orders`、`aov`、`quantity`、`product_paid_amount`、`refund_amount`、`cash_difference`、`cohort_refund_rate`。旧的 `orders` 只能表示采集实体存在，不能自动授予支付能力。
- 空 capabilities、缺档案、未知平台或不支持的指标返回 `missing_data` + 稳定原因 `capability_unavailable`，不得进入金额 SQL；pdd 单据数仍可在对应覆盖成立时查询。

## 5. 未匹配退款改为可量化限制

全局删除“出现一条未匹配就整次 missing_data”的政策，包括 metrics 门禁与 `reconcile_source_quality` 的质量判定，避免换成另一个门禁仍拒答。

每店、每个结果均披露 `unmatched_count / successful_count`、比例（0/0 为 NULL）、未匹配金额、退款统计期间和金额币种；分母限定为同一窗口 canonical 平台成功退款，失败、待处理、重复工单排除。使用 Decimal，不使用店铺比例的平均值作为总比例。

| 指标 | 未匹配时的行为 |
| --- | --- |
| refund_amount | 发生额包含所有 canonical 平台成功退款，matched=false 也计入；返回 ok 并披露未匹配比例；不能说这些退款全属于本期支付 |
| cash_difference | 支付窗口能力/覆盖满足时返回支付减期间全额退款，披露两端来源及未匹配金额；不能称同批净收入或净利润 |
| cohort_refund_rate | 已匹配且属于支付 cohort 的退款/已验证 cohort 支付额；有无法判定归属的退款时返回可计算值并标 `matched_cohort_only`，明确不是完整同批退款率；不得把所有未匹配退款猜配本期、不得报作确定的全量率 |
| paid_amount | 不因未匹配退款拒答，但必须报告未认证支付的数量/已知原始金额及原因；orphan/undetermined 不能无声消失，未知金额保持 NULL |

未匹配诊断与 cohort 数值各自标明窗口：本期退款未匹配占比不能冒充 cohort 匹配率；cohort 截止以前、处于已证明售后观察范围内而无法归属的退款另列，无法证明观察完整则保持 cohort 覆盖不足。分母为 0 时率为 NULL，仍可说明事实。

质量状态沿用 unknown/passed/failed。未匹配是归属限制，不再独自置 failed 或宣布已完整对账；金额冲突、重复认证、分页/覆盖损坏等真实核验失败仍阻止受影响指标。升级 `quality_rule`，旧规则因 unmatched 标记的 failed 只能在逐源重核、确认没有其他失败原因后解除，不批量清空真实失败。版本化能力和 basis 随结果保存。

## 6. 请求、结果、提示词与运行版本

`QueryRequest` 增加 `basis_policy: Literal['strict', 'separate'] = 'strict'`：strict 禁止把不兼容口径汇成一个值；separate 允许明确的分店/分口径结果。模型只能请求政策，不能自行认证或覆盖 basis。兼容性由指标、时间归属、币种和认证版本共同决定，不能只比较平台名。

`ToolResult` 增加结构化 `basis` 与 `diagnostics`；单店也必须有 basis。内部项关联 `shop_id/metric/source/basis/time_basis/metric_version`，公开投影转换成稳定 `shop_ref`，保留可理解标签、质量与限制，不泄露 ERP 主键。`basis` 与支付认证内部的 `order_payments.basis=head/items` 是不同概念，后者仅表示取证方式。

- 跨平台 total/day/product 的不兼容口径，strict 返回 `invalid_parameters` + `basis_incompatible`，提示按店分列；不执行混合金额汇总。
- `group_by=shop, basis_policy=separate` 返回每店独立结果，必须附 basis；不产出跨口径总计、相互增长率或平台优劣结论。分组存在缺能力时整体不得冒充全量成功；明确给出缺失组及可查询组建议，用户确认范围后再查。
- fxg 的 `platform_payment/v1` 与 tb/tm 的 `erp_outstock_payment/v1` 不直接合计比较；现金差须标明支付来源+平台退款发生口径。
- 不同 basis 的上期比较同样禁止；同店换来源时不能把两期差额解释为增长。
- 系统提示词：销售额先确认期间和口径；平台对比只展示已认证的可比结果；不能把同名指标自动同义化。Q15 不能固定回答“只支持抖音”，应基于授权店铺的实时能力列出已支持范围和缺失平台。
- `business_query` 请求解析/状态、结果投影、`runtime` 白名单、确定性摘要、Artifact、会话继承和 fingerprint 一并传递这些字段；前端沿用现有 limitations 槽位，仅补标签展示所需类型与渲染。
- fingerprint/provenance 纳入来源绑定、能力版本、basis、时间口径和规则版本，来源/能力变更必须失效旧结果复用。旧 Artifact 原样可读并标旧口径，不能冒充新认证；不可承诺任意历史 SQL 重跑一致。

## 7. 交付分层

本轮 main 合并只代表同步接入与架构代码在同一基线、合成集成回归通过。后续顺序：关闭单与历史修复契约 → 注册表/能力/时间覆盖 → 退款及未认证支付披露 → basis 全链路 → Task 11 的原 20 题修订和新增 6 题 → 真实库逐店取证及发布。

不受影响：原 Task 1–4、6–8、10 的路线及 Task 11 部署步骤。Task 9 的 Agent 结构不重建，其提示词/投影适配由 Task 5 负责。运营工作流 Task 6–10 的目标不变；依赖本契约的多平台结果未验收前不能宣布交付。
