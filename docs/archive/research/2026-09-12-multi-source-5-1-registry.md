# Task 5.1 交付记录：来源注册表与逐指标能力门禁

日期：2026-09-12，北京时间。范围：原路线图 [Task 5](../plans/2026-09-07-ecommerce-bi-agent.md) 的
5.1a / 5.1b 与 5.1c 的代码部分。分支 `feat/multi-source-contract`，提交
`0766741`（拼多多范围决定）与 `01d462c`（实现）。

口径依据：[多来源指标设计](../specs/2026-09-12-multi-source-metrics-design.md) §3–§4。
范围前提：[放弃拼多多支付接入](2026-09-12-drop-pdd-onboarding.md)。

## 1. 交付内容

| 位置 | 变化 |
| --- | --- |
| `backend/bi_agent/sources.py` | 新增代码内有限注册表：平台 →（订单来源、支付口径、时间口径、业务时间是否已认证、能力上限），以及 `ShopRecord`、`SourceBinding`、`resolve_order_source`、`resolve_metric_sources`、`unsupported_reason`、`capabilities_from_evidence`、`ENTITY_REQUIREMENTS`（唯一真源，`data_quality` 转发） |
| `bi_agent/sync.py` | 平台路由改为消费注册表；`_shop_order_source` 对未登记平台报错退出（不再回退交易源）；新增 `capabilities` 维护命令（`--apply` / `--all-shops`）与 `recompute_shop_capabilities`、`capability_target_shops` |
| `bi_agent/metrics.py` | `_SHOPS_SQL` 加载平台与能力标签；金额 SQL 之前新增 `_capability_gap`，把「平台未登记来源」与「有来源但该指标未授予能力」分成两条披露 |
| `bi_agent/data_quality.py` | `source_unconfigured` 改为「平台是否登记过来源」，不再拿实体标签当能力证据；与能力门禁去重 |
| 词表 | `capability_unavailable` 进入 limitation 码、公开文本正则、`PublicMessage`、`RequestIdentity.termination_reason`、恢复 `GAP_REASONS`（0 次追加）与 `014` 的 SQL CHECK |
| `sql/014_multi_source_contract.sql` | 重新声明终止原因 CHECK（009 不可改写）；改写 `bi.shops.capabilities` 列注释，实体标签退役 |
| `runtime/repository.py` | 每进程一次预检：库里的 CHECK 不认识本进程码表时早报 `SchemaOutdated`，而不是收尾撞 CHECK 变成看不懂的失败 |

能力标签与指标同名，只能由 `bi.sync_state` 的逐来源对账证据推导；证据消失即回收，
`quality_rule` 过期视同未核验。拼多多上限只有 `erp_documents`，支付族即使被误写进库
也不放行（平台上限优先于标签）。未登记平台（1688、淘工厂）没有来源，也没有回退。

## 2. 验证

```sh
cd backend
uv run --no-sync --env-file ../.env.test python -m unittest discover -s tests -t .   # 438 项 OK
uv run --no-sync --env-file ../.env.test python -m tests.acceptance --offline        # 20/20 pass
cd ../frontend && npm test && npm run build                                          # 46/46，构建通过
```

- 新用例 `tests/test_multi_source_metrics.py` 44 项：注册表解析（纯代码，任何环境都跑）、
  能力推导与回收、真实测试库上的门禁（14 项，含以 `bi_reader` 身份跑的成对用例：
  撤标签 → 零数字；给标签 → 照旧出 1000）。
- 变异检查（评审时执行）：把 `metrics._capability_gap` 打桩成 `None`，5 项门禁用例转红，
  证明门禁确实是被验证的那一段。
- 契约变化的旧用例已按新口径改写，没有删除断言：`test_platform_routing_table`、
  `test_shop_order_source_lookup`（未登记平台改为断言报错）、
  `test_*_unconfigured`（改为平台登记口径）、种子显式写能力标签
  （`test_db.set_capabilities` / `ALL_CAPABILITIES` / `FakeWarehouse` 五列 v_shops）。
- 测试库为本机 Docker `bi-agent-pg`（`bi_agent_test`，001–009 + 本轮 014），只用合成数据，
  外层事务回滚。**没有**触碰生产库、快麦接口或真实模型。

## 3. 已知边界与下一步

1. **`coverage_certified` / `time_basis` 目前只有数据没有消费者**：出库通道样本不得当
   完整支付窗口，由 5.2c 实现；本轮已用例如钉住旗标不被删（`tm` 能力可开通而
   `coverage_certified=False`）。
2. **覆盖层仍按单源常量读 `orders` 状态**（5.2b）：因此对 `tb`/`tm`/`pdd` 执行
   `capabilities --apply` 会写出 `coverage_source_mismatch` 警告——能力开通 ≠ 立刻出数，
   现在这类店仍报覆盖缺口。这不是接入完成的证据。
3. `reporting.v_*` 视图仍可被直查绕过门禁（5.4c）；Artifact 层的 basis/diagnostics 全链路
   未做（5.4b/5.4c）。
4. 生产发布顺序硬约束：**先应用 014，再部署本代码**（见 runbook）。真实库的逐店
   `reconcile` + `capabilities --apply` 未执行——宿主 `ibn5100-2` 本轮离线，
   Task 5.0c（历史修复核验）同样受阻。
5. 拼多多旧的（若存在的）交易通道 `sync_state` 行被 pdd→出库路由抛成孤儿：既不当覆盖
   凭证也不删除，由 5.2 的多源交集统一处置。逐店开启 `erp_documents` 前必须重跑取证。
