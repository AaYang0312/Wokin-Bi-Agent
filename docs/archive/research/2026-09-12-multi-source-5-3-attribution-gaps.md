# Task 5.3 交付记录：未匹配退款与未认证支付改为可量化限制

日期：2026-09-12，北京时间。范围：原路线图 [Task 5](../plans/2026-09-07-ecommerce-bi-agent.md)
的 5.3a–5.3e。分支 `feat/multi-source-coverage`。
口径依据：[多来源指标设计](../specs/2026-09-12-multi-source-metrics-design.md) §5。
前序：[5.1 注册表](2026-09-12-multi-source-5-1-registry.md)、[5.2 覆盖交集](2026-09-12-multi-source-5-2-coverage.md)。

## 1. 拆掉的门禁

`metrics._query_in_transaction` 与 `data_quality.reconcile_source_quality` 都对
「窗口内存在未匹配的平台成功退款」做整次拒答 / 整店降 `failed`。09-12 淘系实测同窗口
659/717 未匹配（抖音对照 0/255），这条门禁的实际效果是：修好覆盖之后，淘系退款类指标
仍然几乎必然不可查——即“换成另一个门禁继续拒答”。

现在三条限制都逐结果披露，不拒答也不静默：

| 披露 | 文本 | 稳定码 |
| --- | --- | --- |
| 退款归属 | `退款归属未确认：未匹配{u}条/共{t}条，金额{a}元，比例{r}` | `unmatched_refunds` |
| 同批率范围 | `同批退款率仅含已匹配退款（{u}条未匹配退款无法归属，未计入）` | `matched_cohort_only` |
| 未认证支付 | `未认证支付{n}笔（金额未定{u}笔），已知原始金额{a}元（{k}笔）` | `unverified_payments` |

比例按**条数**算、分母固定为同窗口 canonical 平台成功退款（失败/待处理/重复工单排除），
0/0 是「未知」不是 0%，全部 Decimal，不拿各店比例求平均。三条文本都登记为公开披露正则，
未登记会在保存 Artifact 时被契约校验拒成 `result_contract_violation`（新增
`QuantifiedLimitationContractTests` 钉住，含伪造/夹带店铺主键的拒绝用例）。

## 2. 依赖与口径修正

- `refund_amount` 依赖收窄为只需 `aftersales_occurrence`（设计 §4：退款发生不强制依赖订单
  匹配）。旧写法会拿“订单还没覆盖”打死一个本可回答的退款查询。`cash_difference` 保留双依赖。
- `matched=false` 的 canonical 成功退款**计入退款发生额**（`reporting.v_shop_daily` 本来就这样
  聚合），但同批率只算能归属到本期支付原单的部分。
- `unverified_payments()` 从 `reporting.v_payments` 读 `NOT verified`，拆「金额未定」与
  「有原始金额」两档 + 已知合计，笔数一起给出，避免把 0 元读成“这些单值 0 元”。
- `QUALITY_RULE` → `kuaimai-reconcile/2`。未匹配仍写 `quality_reason`，但不再置 `failed`；
  旧 `failed` 不自动解禁，必须重跑能留下批次凭证的 `reconcile`（禁止批量清空）。

## 3. 补拉收敛（5.3e）：补上此前零覆盖的路径

`tests/test_db.py::RefetchConvergenceTests`（5 项）覆盖 09-12 实测报告点名的空白
（`unmatched_commercials` / `refetch_orders_for_commercials` 无任何用例，导致“集合永不
收敛、每轮白跑 ≥625 次上游调用”长期未被发现）：

1. 已付款关闭单退出补拉集合（5.0b 口径的收敛证明）；
2. 原单真的没到时留在集合里；
3. 无 `commercial_id` 的退款不作为补拉目标（归披露，不白跑上游）；
4. 补拉按店铺平台走实际通道（tb→出库），取回原单后集合收敛；
5. 上游失败不留订单/批次半成品，集合保持原状。

## 4. 验证

```sh
cd backend
uv run --no-sync --env-file ../.env.test python -m unittest discover -s tests -t .   # 460 项 OK
uv run --no-sync --env-file ../.env.test python -m tests.acceptance --offline        # 20/20
cd ../frontend && npm test && npm run build                                          # 46/46，构建通过
```

红灯先留档再改实现：`test_unmatched_refund_degrades_to_missing_data`（`'ok' != 'missing_data'`）
与 `test_unmatched_success_refund_demotes_to_failed_with_reason`（`'passed' != 'failed'`）
在实现前转红，之后按新契约改写为量化披露断言，**没有删除旧断言**。

**未执行**：生产库重跑 `reconcile` + `capabilities --apply`（旧 unmatched-only `failed`
必须在真实库上逐源解除）；真实模型 26 题验收。

## 5. 留下的口子

1. `reporting.v_payments` 无 `basis` 列，所以「冲突 / 孤立」只能按金额是否已知两档近似；
   要细分需开一张只读登记视图（与 5.4 的 basis 全链路一起做）。
2. 披露目前只进 `limitations` 文本；`ToolResult.diagnostics` 结构化字段属于 5.4b。
3. 旧 unmatched-only `failed` 的逐源解禁依赖真实库运维窗口。
4. 5.4（basis 全链路、`basis_policy`、指纹与版本失效）与 5.5（26 题合同）未开始。
