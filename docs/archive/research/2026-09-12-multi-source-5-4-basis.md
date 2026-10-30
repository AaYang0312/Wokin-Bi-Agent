# Task 5.4 交付记录：口径凭证全链路与混口径禁合并

日期：2026-09-12，北京时间。范围：原路线图 [Task 5](../plans/2026-09-07-ecommerce-bi-agent.md)
的 5.4a–5.4d。分支 `feat/multi-source-basis`。
口径依据：[多来源指标设计](../specs/2026-09-12-multi-source-metrics-design.md) §6。
前序：[5.1 注册表](2026-09-12-multi-source-5-1-registry.md) ·
[5.2 覆盖交集](2026-09-12-multi-source-5-2-coverage.md) ·
[5.3 可量化限制](2026-09-12-multi-source-5-3-attribution-gaps.md)。

## 1. 解决的问题

指标名会骗人。`erp_documents` 在抖音交易通道上按付款时间统计，在淘系出库通道上按出库
时间统计；`paid_amount` 一个是平台支付口径、一个是 ERP 出库口径。此前这些差别只存在于
服务端常量里，结果、Artifact、模型载荷和前端都看不到，于是：

- 模型会把同名指标当同义词，把两种口径加成“全平台销售额”或算成增长率/排名；
- 一次通道切换造成的差异会被解释成经营波动；
- 换来源之后旧结果还能按同一指纹命中。

## 2. 口径凭证的形状与边界

内部（`ToolResult.basis`）：`{shop_id, metric, source, basis, time_basis, metric_version}`
公开（模型载荷与 Artifact）：`{shop_ref, metric, basis, time_basis, metric_version}`

`source`（接口方法名）与 `shop_id`（ERP 主键）**在投影层就删掉**，由
`runtime/models._basis_items` 再校验一次：指标名必须来自固定词表，口径名必须是
`^[a-z0-9_-]+(/[a-z0-9][a-z0-9._-]*)?$` 形态，引用必须匹配 `REF_RE`。用例证明载荷里
不出现 `TB1`、不出现 `erp.trade.*`（既有投影测试也覆盖这一点）。

## 3. 判定规则

兼容性只看 `binding_signature = (basis, time_basis, time_certification)` 集合：

| 情况 | 行为 |
| --- | --- |
| 范围内出现 >1 种签名，分组是 `total` / `day` / `product` | `invalid_parameters` + `basis_incompatible`，不产出任何数字，提示按店铺分列 |
| `group_by=shop` 且 `basis_policy=separate` | 分店各带口径出数；不产出跨口径合计、增长率或排名 |
| `group_by=shop` 且 `strict`（默认） | 同样拒绝：strict 的语义是“先确认口径”，要分列必须显式改策略 |
| 上期由本次没在用的来源覆盖（换过通道） | 拒绝比较（`basis_incompatible`），不当成“上期覆盖不足”降级，更不当成增长 |
| 单店 | 照样附口径凭证 |

`basis_policy` 是模型可请求的**策略**，不是口径值：`QueryRequest` 是 `extra="forbid"`，
模型给不出能覆盖注册表的 basis。

## 4. 版本与复用（5.4d）

`015_provenance_basis.sql` 给 `bi.query_provenance` 增加
`source_registry_version` / `basis_signature text[]` / `quality_rule`，三者都进
`fingerprint_parts()`。`basis_signature_of()` 把凭证压成 `指标|口径|时间归属` 的去重签名，
所以店铺主键与接口方法名进不了血缘表，而任何口径/时间归属差异都会改变指纹。

版本常量原来在运行层与指标层各有一份 `METRIC_VERSION` / `POLICY_VERSION`（同名字面量
不同值），本轮统一由 `sources.py` 供给：

```
SOURCE_REGISTRY_VERSION = "sources/2026-09-12.1"
METRIC_VERSION          = "metrics/2026-09-12.1"
POLICY_VERSION          = "multi-source-policy/2026-09-12.1"
```

## 5. 其他同时修掉的错

1. **版本常量两处各写一份**（上表）——正是这份代码库反复警告的漂移模式。
2. `_SWITCHED_SOURCES_SQL` 的参数顺序一度写成 `(shop_ids, entities)` 而 SQL 里是
   `entity = ANY(%s) AND shop_id = ANY(%s)`：换来源检查会**静默查不到行**、永远放行。
   现在参数顺序与 SQL 一致，并在注释里写明“写反了就是静默放过”。
3. 兜底摘要（模型预算耗尽时）原先不说口径，比正常结果少说一件事 → 补“统计口径”行。
4. `_BASIS_NAME_RE` 初版只允许纯数字版本段，会把 `platform_payment/v1`、
   `metrics/2026-09-12.1` 判成非法 → 允许 `v1` 与日期式版本。

## 6. 验证

```sh
cd backend
uv run --no-sync --env-file ../.env.test python -m unittest discover -s tests -t .   # 473 项 OK
uv run --no-sync --env-file ../.env.test python -m tests.acceptance --offline        # 20/20
cd ../frontend && npm test && npm run build                                          # 49/49，构建通过
```

新增：`BasisContractDatabaseTests` 11 项（真实库 + `bi_reader`，含混口径拒绝、separate
分列、单店带口径、换来源拒比、投影脱敏、摘要带口径）、`ProvenanceContractTests` 5 项
（三种版本失效、签名形状拒绝、签名去重）、前端 3 项（口径展示、混口径提示、旧 Artifact 可读）。
红灯留档：改实现前 `BasisContractDatabaseTests` 6/6 失败（`QueryRequest` 无 `basis_policy`、
返回 `missing_data`/`ok` 而非 `invalid_parameters`）。

**未执行**：真实库迁移（014/015 需在生产 `bi_agent` 库上跑）、真实模型 26 题验收、
淘系逐店取证。`reporting.v_*` 视图直查绕过门禁的路径仍未闭（5.1c 遗留，属 5.5/发布项）。
