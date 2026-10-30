# 主线整合与计划修订核验

日期：2026-09-12。任务：将既有开发成果统一到 main，并修订多来源指标计划。本文只记录本轮实际执行的工作，不代替真实来源对账。

## 分支与冲突处理

- 原 main：`06a5cc2`。
- FastAPI 重构：`aa89bfa`，包含会话前后端、商品身份、数据质量、运行版本与恢复等后续开发。
- 查询状态图：`ffd8844`，已由 `9a4cce9` 合入重构分支，经祖先关系检查确认，不重复 cherry-pick。
- 淘系接入：`f814a47`，保留销售出库源路由、非敏感字段规范化、测试和实测报告。
- 合并顺序：main → 重构（含状态图）→ 淘系。临时整合分支 `codex/multi-source-integration` 完成冲突修复和验证后，将结果合入 main，完整保留双方历史。

`sync.py` 的原地修改/目录迁移冲突在 `backend/bi_agent/sync.py` 内解决。淘系测试迁入 `backend/tests/`；淘系 pytest 开发依赖并入 `backend/pyproject.toml`，基于重构锁文件重新解析 `backend/uv.lock`。根目录冲突残留 `tests/test_core.py`、`tests/test_db.py`、`uv.lock` 删除，不恢复旧 Python 项目。

合并保留：

- tb/tm 的出库源与售后共用源；probe、backfill、incremental、reconcile、replay、按商业单补拉均传递实际订单源。
- 重构分支严格分页解析及归档例外，回填结束时刻 now 注入，GUARD_STATS 支付防降级计数。
- 关闭已付款订单保持 inactive，但继续参与支付认证和退款匹配；商品行仍按有效性统计。
- 现有状态/商品名称快照字段纳入淘系冻结白名单测试，PII 禁存断言继续成立。
- 质量核验支持显式 source；reconcile 传入实际订单源，避免将淘系批次误查为交易源或更新错误状态。

独立代码评审发现一个测试夹具的SQL参数数量不匹配，已还原固定fxg平台并通过数据库回归。新增关闭单测试需提供真实去重语义所需的合成平台退款号，避免把未canonical退款误当已认证退款。

## 验证结果

原 `.env.test` 的本地 54329 端口不可连接，对应 Docker 测试实例处于停止状态。本轮另建仅监听本机的临时 PostgreSQL 17 容器 `codex-bi-merge-test`，数据库 `bi_merge_test`，应用仓库 001、002、003、004、005、007、008、009 迁移。只用合成数据；测试沿用外层回滚事务。没有对生产库回放、迁移、同步或调用快麦/真实模型。

| 检查 | 结果 |
| --- | --- |
| 后端 unittest discover（独立测试DB） | 392 tests，0失败，0跳过 |
| 当前20题 offline（模拟模型、真实测试DB） | 20/20通过；不是新26题或真实模型验收 |
| 前端 Vitest | 46/46通过 |
| TypeScript + Vite生产构建 | 通过 |
| 淘系出库原有测试 | 字段映射、状态、splitSid、路由、PII、双源水位隔离通过 |
| 新增淘系关闭单完整链路 | 支付100、canonical退款30、收支差70；商品有效行0；unmatched_commercials为空 |
| 新增来源质量隔离 | 淘系重核只更新出库源quality，交易源状态保持unknown |

后端验证命令（从 backend 执行，DSN指向上述独立临时库）：

```sh
uv run --no-sync python -m unittest discover -s tests -t .
uv run --no-sync python -m tests.acceptance --offline
```

前端：`npm test`、`npm run build`。临时日志只存本机 `/tmp/bi-merge-isolated.log`、`/tmp/bi-merge-acceptance.log`；不作为仓库运行依赖。

## 计划交付与剩余工作

权威修订：[多来源指标设计](../specs/2026-09-12-multi-source-metrics-design.md)、[原路线图Task 5/11](../plans/2026-09-07-ecommerce-bi-agent.md)。运营工作流计划补齐相同前置，任务目标与部署路线不变。

已决定关闭单采用C修正版：活动性与支付事实分开，既恢复可认证支付又匹配退款。Task 5重新打开，明确唯一来源注册表、逐指标capabilities、公共覆盖交集、出库时间语义认证、未匹配退款可答披露、paid_amount未认证金额披露、basis全链路及版本失效。Task 11修订Q08/Q15并新增Q21–26；部署步骤沿用。

**尚未完成的查询实现：** 当前 data_quality 仍保留单源覆盖常量；metrics/quality 的未匹配退款硬门禁仍在；指标级capabilities与basis尚待实现。旧20题通过只证明合并兼容，不能称淘系查询已可用或全平台GMV可汇总。出库pay_time语义、历史支付/匹配重建和真实库逐店验收分别保留待办；pdd方舟支付及推广实耗均未开放。
