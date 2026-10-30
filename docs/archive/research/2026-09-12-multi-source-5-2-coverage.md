# Task 5.2 交付记录：公共覆盖取交集 + 付款时间口径三态认证

日期：2026-09-12，北京时间。范围：原路线图 [Task 5](../plans/2026-09-07-ecommerce-bi-agent.md)
的 5.2a / 5.2b / 5.2c。分支 `feat/multi-source-coverage`。

口径依据：[多来源指标设计](../specs/2026-09-12-multi-source-metrics-design.md) §4。
前序：[Task 5.1 注册表与能力门禁](2026-09-12-multi-source-5-1-registry.md)。

## 1. 修掉的错

`assess_query_coverage` 原来把各店 `covered` 取**并集**当公共已覆盖范围，并按单源常量
`ENTITY_SOURCES["orders"]` 查状态。两个后果都实测过：

1. 建议窗口可以是假的——「甲店有 09-03\~09-05、乙店有 09-06\~09-08」拼成
   「09-01\~09-08 已覆盖」，照建议再查仍然缺数（计划 5.2a 的固定反例）。
2. 淘系 12 家店在 09-12 出库接入后**完整不可查**（§1.6 探针：12/12 `missing_data`，
   与抖音店混合时连带拖死抖音部分），因为查询只认 `erp.trade.list.query` 的状态行。

## 2. 现在的行为

| 项 | 规则 |
| --- | --- |
| 依赖解析 | `sources.resolve_metric_dependencies`，**不看能力标签**：能力缺口与覆盖缺口分开归因 |
| 查询形态 | 按 `(source, entity)` 分组批量取状态，依赖去重；区间边界仍在 SQL 里裁剪 |
| `covered_windows` / `suggested_window` | 请求范围 ∩ 每个必需的 (店铺 × 来源 × 实体) 区间 |
| `missing_windows` | 请求范围 − 公共范围（孔洞原样保留，不取 min/max 凑连续） |
| `gaps` | 按实体、店铺与**具体来源**归因；`source` 与 `shop_id` 一样只留服务端 |
| `data_as_of` | 任一依赖没推进 → 整体未知；否则取最小值 |
| `source_batches` | 只收本次真用到的通道；旧通道残留批次不进血缘 |
| 无可用依赖 | 某店某指标解析不出来源 → 整段窗口按未知，绝不因「没有依赖」算出假完整 |

### 付款时间口径三态（5.2c）

「没测过」与「测了不成立」后果不同，所以布尔值不够用：

| 认证 | 依据 | 行为 |
| --- | --- | --- |
| `certified` | 抖音交易通道实测 0/9414 行越界 + 逐元对账 | 正常出数 |
| `unmeasured` | 同通道同 `timeType` 参数，但该平台未逐店与后台对照 | 出数 + 披露「按可观测样本」 |
| `disproved` | 出库接口实测 83/8367 行 `paid_at` 早于窗口起点（最早早 42 天） | 按支付时间归属的指标**拒答** `coverage_time_basis_unverified` |

`erp_documents` 是单据计数、不主张支付窗口，所以 `unmeasured`/`disproved` 都只披露不拒答
（设计 §4：拼多多单据数仍可在覆盖成立时查询）。`SourceBinding.coverage_certified` 保留为
设计 §3 的布尔契约（等价于 `certified`）。

新原因码 `coverage_time_basis_unverified` 已进入 limitation 码、公开文本正则、
`PublicMessage`、`RequestIdentity.termination_reason`、恢复 `GAP_REASONS`（0 次追加）与
014 的 SQL CHECK。归因顺序：授权 → 来源与能力 → 时间口径 → 覆盖/质量 → 金额 SQL。

**同时撤掉 5.1 的 `coverage_source_mismatch` 警告**：它的前提是「覆盖层还只读交易源」，
5.2b 之后这个前提不成立，留着就是过时的恐吓。

## 3. 验证

```sh
cd backend
uv run --no-sync --env-file ../.env.test python -m unittest discover -s tests -t .   # 452 项 OK
uv run --no-sync --env-file ../.env.test python -m tests.acceptance --offline        # 20/20
```

新增用例：`MultiSourceCoverageIntersectionTests`（8 项，含计划 5.2a 的孔洞反例、旧通道
干扰行、共同截止取最小/未知、上期区间、批次血缘、缺口按来源归因）、
`SpanAlgebraTests`（3 项纯函数边界）、`test_multi_source_metrics` 里 4 项通道认证用例
（`disproved` 拒答 / `erp_documents` 出数并披露 / `certified` 不波及 / `unmeasured` 披露）。

按新契约改写（未删除）的旧断言：并集语义下的 `partial` 改为交集语义的 `missing`，并补
`suggested_window is None` 断言——拿不出的指标不能靠建议窗口渗回一半结果。
`test_data_quality` 的对照店 DQ_S2 平台由 `pdd` 改为 `jd`：该类所有状态行都写在交易通道下，
pdd 的依赖现在是出库通道，混合两个话题会让失败原因说不清；出库通道由新的 `MS_*` 用例覆盖。

**未执行**：生产库迁移与同步（`ibn5100-2` 上的 `bi_agent` 库需要 014 + 一次
`reconcile` + `capabilities --apply` 才能验证真实淘系店）；真实模型验收。

## 4. 留下的口子

1. 逐店 `time_basis` 登记表未建：现在认证是按通道登记的代码事实。等拿到「与后台账单/
   业务日期对照」的逐店证据，再建表并与 `capabilities` 同批维护，不先建无人写入的表。
2. 旧的 pdd/淘系交易通道 `sync_state` 行成为孤儿：不参与覆盖判定，也不删除（删就是
   抹掉取过数的痕迹）。发布时需按店确认新通道状态行已存在，再决定台账处置。
3. `refund_amount` 仍按 `ENTITY_REQUIREMENTS` 依赖 orders 实体（5.3b 要收窄为只需退款发生源）。
4. 5.3（未匹配退款改可量化限制）、5.4（basis 全链路与版本失效）未开始；
   `data_quality.UNMATCHED_REFUNDS_SQL` 的整次硬拒答仍在。
