# 快麦拉取稳定性修复 + 历史回填拉长到 180 天

## 背景诊断（已核实）

"问七天报错"= 查询侧 fail-closed 拒答：最近 7 天任一 (店铺×来源×实体) 覆盖有缺口即返回 `missing_data`（`backend/bi_agent/metrics.py:735-739`）。快麦 API 并无"7 天限制"，所有拉取已按官方建议切成 ≤1 天窗口（`sync.py:830-836`）。缺口反复出现的根因有两个：

1. **整店中断**：`_run_window` 抛错上抛（`sync.py:1869-1882`），`_backfill_shop`/`_reconcile_shop` 里一个日窗口失败，该店本次运行的所有后续窗口全部跳过。
2. **限流重试预算过小**：HTTP 429 的 Retry-After 被压到最多 5 秒、总共 3 次尝试（`kuaimai.py:26-27,148-157`）。淘系店每支付日宽窗取数 ≥62 次流式请求（±15 天×31 日切片×双通道，`sync.py:1195-1235`），7 天重核 ≥434 次请求，极易触发限流后耗尽重试。

## 改动内容

### 1. 只读诊断（先做，不改代码）

对本机 `bi_agent` 库执行只读 SELECT：`bi.sync_state` 逐 (shop×source×entity) 取 covered 与 `[now-30d, now)` 的差集、`last_error_code` 分布、`data_as_of`/watermark。确认缺口集中在哪些店/实体/错误码（rate_limit vs timeout），决定修复侧重；结果写进验收报告。

### 2. 客户端限流重试预算（`backend/bi_agent/kuaimai.py`）

- `RETRY_AFTER_CAP_SECONDS` 5.0 → 30.0：HTTP 429 时在上限内完整尊重服务端 Retry-After。
- rate_limit 类（HTTP 429 与业务信封"频繁/限流"）单独给更多尝试次数（新增 `RATE_LIMIT_MAX_ATTEMPTS = 5`）；其余错误类维持 `MAX_ATTEMPTS = 3` 与现有指数退避不变。
- 红灯测试先行：改 `tests/test_core.py:426-457` 的重试用例（429 带 Retry-After: 20 → 等满 20 秒（patch sleep）、新尝试上限、其他类别不受影响）。

### 3. 回填/重核窗口级容错（`backend/bi_agent/sync.py`）

- `_backfill_shop` 与 `_reconcile_shop` 的业务时间窗口循环：单窗口 `KuaimaiError` 先冷却重试（如 2 次、冷却 30s 级、patchable sleep），最终失败则记入失败清单并**继续下一个窗口**，不再中断整店；运行结束在 stats/CLI JSON 输出中汇总失败窗口。`record_failure`/整窗事务回滚/fail-closed 覆盖语义全部不变（失败窗口只是不推进覆盖）。
- `_reconcile_shop` 每窗口的 cohort 检查仅在 orders+aftersales 都成功时执行。
- **`_incremental_shop` 保持现有中断语义不改**：watermark 单调推进，失败后下一小时从原水位自愈，继续跑反而会留永久洞。
- 回填末尾的 scan 段（建水位）失败仍按现状上抛报错。
- 红灯测试先行：stub client 指定第 k 窗口失败 → 断言后续窗口仍入库、覆盖只含成功窗口、最终输出含失败汇总（更新 `tests/test_db.py` 相关 reconcile/backfill 用例）。

### 4. 缺口自愈模式：`reconcile --missing-only`（`sync.py` CLI + 查询辅助）

- 用 `bi.sync_state.covered`（tstzmultirange）算 `[end-N, end)` 的未覆盖日窗口集合（orders / aftersales_occurrence / aftersales_cohort 逐实体），只对这些窗口走 `_run_window`/`check_cohort_window`，复用现有窗口机器，不建平行实现。
- 价值：已覆盖窗口零请求（淘系全量重核每店每天 434+ 次请求，missing-only 稳态接近 0），可安排更高频次任务持续保持 7 天无洞。
- 红灯测试先行：预置部分覆盖的 sync_state → 只拉缺口窗口。

### 5. 文档

`docs/runbook.md`：更新重试参数说明、`--missing-only` 用法、180 天历史分段回填指引（见下）与"长时间任务与每小时增量任务的锁竞争属预期（IgnoreNew 快速失败）"。

## 历史拉长到 180 天（真实拉取，执行时逐条列出命令）

无需改代码（`MAX_QUERY_DAYS=366` 覆盖 180）。依据诊断结果二选一：

- 近 90 天覆盖已完整：只用 `replay --entity orders / aftersales_occurrence / aftersales_cohort` 按**月分段**补 `[now-180d, now-90d)`，避免重拉已覆盖区间、缩短连续持锁时间（advisory lock 单实例）。
- 覆盖不足 90 天：直接 `backfill --days 180`。

拉取后只读核对：180 天与最近 7 天覆盖差集为空，再在应用里问"最近七天"确认正常出数。真实同步按 runbook 用 `.env.sync`、单写者；不碰 PDD、不碰受保护文件（`.cloudflared.*`/`.streamlit.*`/`.tools/` 等）、不新增依赖。

## 验收门禁（backend/，串行）

```bash
uv run --env-file ../.env.test python -m unittest discover -s tests -t .
uv run --env-file ../.env.test python -m tests.acceptance --offline
```

新增迁移：无。前端不受影响（不改 `frontend/`）。测试全绿后单个逻辑提交（如 robustness 与 missing-only 拆两个提交，以实际体量定），提交前 `git diff --check`、staged 白名单核对。

## 明确不做

- 不给快麦请求加固定 QPS 节流（无证据需要，先靠重试+冷却；诊断若显示持续限流再议）。
- 不改查询侧 fail-closed 门禁（缺口就该拒答，修数据不修门禁）。
- 不动 `_incremental_shop` 的中断自愈语义、不动 PDD 相关边界、不提高 `MAX_QUERY_DAYS`。