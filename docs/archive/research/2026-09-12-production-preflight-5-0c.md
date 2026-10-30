# 生产库 5.0c 前置取证（2026-09-12 只读）

范围：用户启动 IBN5100 上的 PostgreSQL 后，通过 `ssh -L 5433:127.0.0.1:54329 home-win`
隧道对生产库 `bi_agent` 做**只读**核对（每条事务都 `SET TRANSACTION READ ONLY`）。
本轮**没有**迁移、回放、同步或改写生产库。目的：把 Task 5.0c「核验历史修复」的
before 状态钉下来，并确认部署顺序风险。

## 1. 迁移状态

| 项 | 结果 |
| --- | --- |
| 014（终止原因 CHECK 加 `capability_unavailable` / `coverage_time_basis_unverified`） | **未应用** |
| 015（血缘表加 `source_registry_version` / `basis_signature` / `quality_rule`） | **未应用** |
| 该实例库清单 | `bi_agent`（生产）、`bi_agent_test`（旧 schema 真实数据副本）、`postgres` |

结论：当前 main 上的代码**不能**直接部署到这台库。014/015 未跑时，一次正常的
「能力未开通」查询会在收尾写入撞 CHECK；运行层预检会早报 `schema_outdated:<码>`
（见 `docs/runbook.md` 部署顺序硬约束），但那等于服务不可用。

## 2. 能力标签与质量规则（决定部署后是否还能出数）

`bi.shops.capabilities` 现状：42 家店中 **7 家非空**，值全部是
`{aftersales_occurrence, orders}`（实体标签）；其余 35 家为空数组。

```
platform / capabilities / 店数
pdd   []                       16
tb    []                       10
fxg   [aftersales_occurrence, orders]  3
alibabac2m []                  2
tm    []                       2
wxsph []                       2
1688  []                       1
fxg   []                       1
jd    [aftersales_occurrence, orders]  1
kuaishou []                   1
kuaishou [aftersales_occurrence, orders] 1
wsxc  [aftersales_occurrence, orders]  1
wxsph [aftersales_occurrence, orders]  1
```

`bi.sync_state` 现状：7 家店 × {orders, aftersales_occurrence} 共 14 行
`quality_status=passed`，`quality_rule` 全是 **`kuaimai-reconcile/1`**；
7 行 `aftersales_cohort` 是 unknown（同批窗口不写批次凭证，与 09-11 记录一致）。

**这两条合起来的后果**：Task 5.1 把 capabilities 改成与指标同名的标签、实体标签不再
授予任何指标，Task 5.3 又把质量规则升到 `/2`（旧 `passed` 视同未核验）。所以
014/015 + 新代码上线后，在**重跑 `reconcile` 再 `capabilities --apply` 之前，
这 7 家店的全部指标都会返回 `capability_unavailable`**。这是 fail closed 的预期，
不是回归，但必须按顺序执行，否则会被当成"上线把功能做坏了"。

## 3. 历史修复的 before 快照（Task 5.0c 要核对的量）

| 量 | 生产实测 | 对照 |
| --- | --- | --- |
| `NOT active AND paid_at IS NOT NULL AND raw_pay_amount > 0` 的关闭已付款单 | **1864** | 09-12 淘系报告在 30 天窗口里量到 608；全库量级是它的 3 倍 |
| `NOT verified` 的支付事实 | **300 笔，`sum(amount)` 为 NULL（合计 0）** | 新披露会写成「300 笔（金额未定 300 笔），已知原始金额0元（0笔）」——金额未知保持 NULL，不当 0 |
| canonical 平台成功退款且未匹配（全库） | **42 条** | 与 09-12 报告同窗口 659 条不是同一区间，不能相减或混算 |
| 166754 的补拉集合 `unmatched_commercials` | **27 个 cid** | 5.0c 要求验证修复后收敛；本轮 5.3e 已在合成库钉住收敛语义（`RefetchConvergenceTests`） |

## 4. 下一步（顺序敏感，属真实库变更，需显式排期）

1. 在**独立测试库**导入生产数据副本，跑 `replay --entity orders` +
   `replay --entity aftersales_occurrence`，逐店记录上表四项的前后变化与批次号
   （计划 5.0c 要求的量），确认 27 个 cid 收敛、`payment_downgrade_blocked = 0`。
2. 生产库按 runbook 顺序执行 `014`、`015`（幂等写法，DROP + ADD）。
3. 部署新代码前先跑 `reconcile --days <覆盖到取证窗口>`（写 `kuaimai-reconcile/2`），
   再 `capabilities`（只报告）核对推导结果，最后 `capabilities --apply`。
4. 按 runbook 的回款逐元核对法重算支付总额，与 `reporting.v_shop_daily` 对照；
   然后才谈"淘系 12 家店可查"与 26 题真实模型验收。

第 2–4 步都会改生产数据或线上行为，本记录只固化 before 状态与执行顺序，不代表已执行。
