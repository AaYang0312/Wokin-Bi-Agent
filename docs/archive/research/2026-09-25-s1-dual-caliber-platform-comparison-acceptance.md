# S1 双口径平台对比验收记录

日期：2026-09-25
计划：`docs/superpowers/plans/2026-09-25-first-two-workflows-launch.md` §2 S1
实现基线：`main@2d73ee3` + 当前未提交工作树
结论：**代码与离线契约 PASS；真实来源与发布验收 FAIL / NOT MET。不得据此声明 S1 已上线或五平台出库排名已经可用。**

## 1. 本轮完成范围

- `platform_comparison` 增加闭集口径：
  - `erp_outstock`：ERP 出库口径；按 `bi.orders.consigned_at`（快麦 `consignTime`）归属。
  - `platform_payment`：平台支付口径；只发布已有认证来源的平台组，未认证格保持 `data_missing`。
- 请求、指纹、排名块、chart artifact 与前端徽标均携带口径；不允许跨口径总计或排名。
- 新增独立出库扫描命令 `consign-scan`：固定调用 `erp.trade.outstock.simple.query`，固定 `timeType=consign_time`；覆盖凭证只写 `orders_consign`，不得借用 `paid_at` 或支付覆盖。
- 已登记非 PDD 平台均可独立进入出库扫描；PDD 继续由既有边界排除。
- 扫描遇到缺失 `consignTime`、窗外单据、分页失败、预算超限或异常时整窗回滚，不推进覆盖。
- 出库扫描重写同一商业单时，会从既有支付事实集合式重导 `allocation_verified`；不重建、不改写、不降级支付行、金额、时间、basis 或支付覆盖。
- 新增迁移仅为 `backend/sql/025_outstock_consign_time.sql`；`001–024` 未修改。
- 离线业务题由 26 题扩展到 28 题，新增两种口径的结构化验收。

## 2. 关键契约证据

| 验收项 | 结果 | 证据 |
|---|---|---|
| 口径与事实绑定 | PASS | `erp_outstock` 只读 `consigned_at` 与 `orders_consign`；支付口径仍使用既有支付来源 |
| 请求与缓存隔离 | PASS | `caliber` 进入规范化请求与 fingerprint；两种口径不会共用缓存 |
| 无跨口径聚合 | PASS | 总计与排名要求同一完整口径签名；缺失格进入 `data_missing`，不补零、不替位 |
| 独立出库采集 | PASS（代码） | 所有已登记非 PDD 平台绑定专项出库通道；固定 `timeType=consign_time`，在线/归档分页沿用既有受控状态机 |
| 覆盖 fail-closed | PASS（代码） | 完整窗口才写 `orders_consign`；缺字段、越窗、分页失败和预算失败均回滚 |
| 支付事实不被扫描降级 | PASS | 同商业单等时间戳 forced replay 后，支付 tuple 不变、`allocation_verified=true`、出库金额仍发布；无已验证支付时不提升 |
| PDD 边界 | PASS | PDD 不进入出库或支付排名；不接入新增事实 |
| PII / SQL 边界 | PASS | normaliser 白名单不扩展 PII；无动态 SQL；无 `001–024` 改动 |
| 前端展示 | PASS | 口径徽标仅按结构化 payload 渲染；旧 artifact 不伪造徽标 |
| 真实来源完整性 | **FAIL / NOT RUN** | 本轮禁止真实 API/同步；没有任何真实 `orders_consign` 覆盖凭证 |
| 认证店双口径对账 | **FAIL / NOT RUN** | 未对认证店（计划建议抖音）执行出库金额与支付金额的聚合差异分析，未证明差率在阈值内 |
| 五平台可复现出库排名 | **FAIL / NOT MET** | 代码路径就绪，但未运行真实 `consign-scan`，因此当前全部平台应 fail closed，不能宣称排名可用 |

## 3. 父级最终门禁

2026-09-25 在本地测试环境串行执行：

| 门禁 | 结果 |
|---|---|
| `cd backend && uv run --locked --env-file ../.env.test python -m unittest discover -s tests -t .` | **1724 tests OK** |
| `cd backend && uv run --locked --env-file ../.env.test python -m unittest tests.test_db tests.test_runtime_db tests.test_monitoring -v` | **299 tests OK，0 skip** |
| `cd backend && uv run --locked --env-file ../.env.test python -m tests.acceptance --offline` | **28/28 pass**；该结果不证明模型理解或真实来源就绪 |
| `cd frontend && npm test` | **11 files / 177 tests passed** |
| `cd frontend && npm run build` | **PASS** |
| `git diff --check` | **PASS** |

## 4. 独立审查

- 初审 reviewer：`3f3d75af-ba8e-422d-8bc6-6bea90c55132`，发现 1 个 P1：出库扫描重写明细时可能把已验证分摊降级。
- 原 writer 修复：`cf2b3904-7b05-47ed-9242-09ef952e2df1`，增加集合式重导与同商业单 forced replay 回归。
- fresh 复审：`eccda4e2-c4ba-419e-8178-ac3b1c11f2c8`，结论 `VERDICT: OK`，无 P0/P1。
- 非阻塞 P2：
  1. chart artifact 读回路径尚未再次校验可选 `caliber` 的枚举值；写入端、排名、筛选、请求和前端已有校验。
  2. `sync_consign_window` 的 `out_of_window` 分支尚无独立 DB 用例；相邻的缺字段、分页、预算和回滚路径已有覆盖。

## 5. 发布阻断与下一步

S1 **尚不可发布**。在解除阻断前必须：

1. 按 `docs/runbook.md` 在获准的只读 API + `bi_agent_test` 边界执行真实 `consign-scan`；保存聚合、脱敏证据，不落订单号或买家信息。
2. 证明目标窗口内每个拟发布平台都有完整、连续且新鲜的 `orders_consign` 覆盖；任一失败的平台继续 `data_missing`。
3. 选一家已认证店完成双口径对账：ERP 出库金额/销量与支付金额逐项说明差异；差率超过批准阈值时先修聚合规则，不上图。
4. 用真实覆盖重新执行五平台默认出库排名，并记录输入窗口、来源指纹、完整性、确定性重放结果。
5. 另行形成日期化真实来源验收；只有该记录 PASS 后，才能把本文件的发布结论从 FAIL 改为 PASS。

## 6. 诚实边界

- 本文件证明的是代码路径、离线契约、迁移与测试门禁，不证明快麦真实返回了完整 `consignTime`。
- `0` 只表示已验证事实中的真实零；缺来源、缺覆盖、超窗或不完整永远是 `data_missing`。
- 未调用真实模型，未调用真实业务 API，未访问生产/预生产，未部署，未注册调度，未 push。
