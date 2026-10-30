# 持续库存通知本地验收（continuous-inventory 计划 Task 6）

日期：2026-09-25。基线：`main@af989f9`（实现前 HEAD；Task 6 改动与本记录一并留在工作树，本轮按任务边界不提交）。

> **状态（先读这一段）**
>
> 1. 本文件只声称："持续库存通知的**本地实现**与本机/离线测试通过"。它**不构成**库存来源验收通过（仍 **FAIL**）、**不构成** Task 11 统一发布门禁通过（第 4–7 项仍 open）、**不构成**生产就绪、**不构成**通知已启用。
> 2. `INVENTORY_MONITOR_ENABLED` 在 `.env.example` 与 `backend/bi_agent/config.py` 中默认 `false`；生产 monitor 保持 disabled。只要 `physical` 层仍未登记已核验来源，本验收未解除任一门禁，也没有任何层级转为 verified。
> 3. 判定依据与所有者决定：[库存来源验收](2026-09-14-inventory-source-acceptance.md)（§8，2026-09-17：官方公开目录无逐店可售量读取端点，`channel` 为一等 `data_missing`/unknown，来源门禁改为**逐层**判定，`physical` 四维仍全部 NOT MET）；执行授权：[持续库存通知本地开发例外](2026-09-17-continuous-inventory-local-development-exception.md)（仅 Task 1–6、仅本机 `*_test` 库、离线/stub 测试、不注册调度、`INVENTORY_MONITOR_ENABLED=false` 默认）。

## 0. 一句话结论

计划 Task 6 六步在本机完成：真实 PostgreSQL 并发/崩溃重启/登记退场/跨策略矩阵（Task 6 Step 1 的 4 个新用例）全绿；`test_monitoring` 补两支 fail-closed 归属钉（渠道侧码只去掉 `shop_sellable`；物理轮渠道码=投影契约违约）；调度与回滚只写进文档（未注册任何计划任务、未创建 systemd timer / Windows 任务、不创建 Codex automation）；指标按实现闭集登记进 metrics §4.9；本机一次性 dry-run 以 **fail-closed 形态**执行两轮（"首轮 triggered"的裸进程形态结构性不可达成，原因与等价证据见 §6）；父级门禁全部零失败。**库存来源验收仍 FAIL；生产监控保持 disabled。**

## 1. 交付物与工作树状态

| 文件 | 内容 |
| --- | --- |
| `backend/tests/test_db.py` | Task 6 Step 1 矩阵 4 用例（上一窗口已写入，本轮逐条验证并原样保留） |
| `backend/tests/test_api.py` | 通知读通道认证闸 1 用例（同上，保留） |
| `frontend/src/components/NotificationCenter.test.tsx` | 列表通道 401 → `ApiError` 1 用例（同上，保留） |
| `backend/tests/test_monitoring.py` | 本轮新增 2 用例（§5 归属用例后两支；先按错误期望证红、恢复后证绿的反向核查） |
| `README.md` | "持续库存通知（默认关闭）"一节 |
| `docs/runbook.md` | 调度（Windows Task Scheduler 与 systemd 两份**文档模板**，未注册）、回滚顺序、"零告警不等于安全"三类合法原因、回归命令 |
| `docs/metrics.md` | §4.9：八个定名指标的真实现状（进程内计数器 / 可派生 / 目标插桩）与闭集词表 |
| 本记录 | 日期化验收（文件名按实际执行日期；未覆盖任何已存日期记录） |

工作树同时携带一份与本任务无关的运维改动（`.agents/skills/` 下 SKILL.md）与若干受保护的未跟踪运维/参考文件：不属本任务改动集，未读取、未修改、未暂存、未提交。

## 2. 引用与边界（逐层口径）

- 来源验收（[`2026-09-14-inventory-source-acceptance.md`](2026-09-14-inventory-source-acceptance.md)）：判定 **FAIL** 且本轮不变——`physical` 的 registration / freshness policy / 完整扫描凭据 / 生产对账四维全部 NOT MET；`channel` 按所有者 2026-09-17 决定为一等 `data_missing`/unknown。缺数量永远不是零、不从 `physical_total` 复制扇出、不参与店铺级触发或解除。
- 执行授权（[`2026-09-17-continuous-inventory-local-development-exception.md`](2026-09-17-continuous-inventory-local-development-exception.md)）：一切执行限定本机 `*_test` 库与离线/stub 测试；本轮没有真实快麦 API 调用、没有真实模型调用、没有生产/预发布库访问、没有部署。
- Task 11 门禁（[`2026-09-14-task-11-release-acceptance.md`](2026-09-14-task-11-release-acceptance.md) §9）：第 4–7 项（真实 provider smoke/live、目标环境迁移与来源核对、部署/备份/真实恢复、一店一周试用）仍 open；本验收与它们互不替代。
- 提交与迁移：基线 `main@af989f9`；`backend/sql/023_continuous_inventory_notifications.sql` 只在本机 `bi_agent_test` 顺序执行并幂等重放，未接触生产/预发布。
- 角色分离：`bi_monitor` 由 023 创建为 **NOLOGIN** 最小权限身份（只读获准库存视图与策略表、仅两个 SECURITY DEFINER 函数的 EXECUTE），与 `bi_app`、`bi_sync`（及 `bi_reader`/`bi_approver`）分离；权限矩阵由 `test_bi_monitor_privilege_matrix` 钉住。
- 词表边界（不变）：曾核验的来源/能力退场只走 `suppressed`、永不 `resolved`；`erp` 不是渠道层合法来源种类，注册表无任何拼多多通道；模型不参与调度、状态迁移、文案或投递；缺层、缺证据与投影截断只以固定诊断码与固定计数出现，绝不以用户通知出现。

## 3. 命令与结果（本机 Windows；共享库套件逐条顺序执行）

```text
# (0) 聚焦基线（文档工作开始前，验证继承编辑）
uv run --locked --env-file ../.env.test python -m unittest tests.test_db.InventoryMonitorConcurrencyTests -v
→ Ran 5 tests ... OK（4 个 Task 6 新用例 + 1 个既有 advisory 锁用例）
uv run --locked --env-file ../.env.test python -m unittest tests.test_api.NotificationApiTests
→ Ran 10 tests ... OK
cd ../frontend && npx vitest run src/components/NotificationCenter.test.tsx → 31 passed

# (1) 全量后端发现（含本轮全部改动）
uv run --locked --env-file ../.env.test python -m unittest discover -s tests -t .
→ 加两支归属钉之前：Ran 1699 tests ... OK；之后：Ran 1701 tests ... OK（0 failure / 0 error / 0 skip）

# (2) DB-enabled 三件套（须 0 skip）
uv run --locked --env-file ../.env.test python -m unittest tests.test_db tests.test_runtime_db tests.test_monitoring -v
→ Ran 289 tests ... OK；verbose 输出中 "... skipped" 计 0（其中 tests.test_monitoring 105 项、0 skip）

# (3) 26 题离线验收（stub 模型；未跑 --provider-smoke / --live）
uv run --locked --env-file ../.env.test python -m tests.acceptance --offline
→ {"mode":"offline","total":26,"passed":26,"failures":0,"result":"pass"}，exit 0

# (4) 前端
npm test    → 11 个测试文件 / 172 tests passed
npm run build → tsc -b && vite build 成功
```

**命令拼写映射（如实记录）**：计划 Task 6 Step 2 写的是 `python -m unittest discover -s tests`。当前仓库的测试模块使用包内相对导入，缺 `-t .` 时 13 个模块装载失败（实测 `Ran 889 tests ... FAILED (errors=13)`，报 `attempted relative import with no known parent package`）。按仓库现行等价拼写（与 Task 11 验收 §2 相同）加 `-t .` 后全绿。这是计划命令拼写过时，不是产品缺陷。

`tests.test_monitoring` 本轮新增用例的反向核查：先故意把 `test_shop_side_code_only_removes_shop_sellable_when_it_runs` 的期望改成错的（断言两层仍完整），实测 `FAILED (failures=1)`（`('physical_total',)` != `('physical_total', 'shop_sellable')`），证明断言真的绑住了归属行为；恢复正确期望后转绿。反向核查用的临时改动已还原，最终 diff 仅 +42 行。

## 4. 覆盖矩阵（计划 Step 5 要求的逐项证据落点）

| 证据项 | 落点（真实本机库 / 真实状态机 / 真实 023 提交函数） |
| --- | --- |
| state fixture | `AlertTransitionGoldTests` + `tests/fixtures/inventory_monitor_transitions.json`：25 条 gold 逐字段钉死（dedupe key 是字面钉值），反序输入结果不变 |
| threshold equal | `test_threshold_equal_triggers_once_then_only_updates`；gold `equal_threshold_triggers` |
| shared pool | gold `shared_pool_one_physical_key_three_shop_cells`：三店共享一池仍只有一个实物键，不逐店翻倍、不相加 |
| stale / incomplete | gold `stale_scan_retains_and_never_triggers_or_resolves`、`test_missing_scan_from_verified_channel_keeps_status_and_cannot_resolve`、新矩阵 `test_incomplete_scan_after_open_preserves_open_across_restart`（"重启"=全新连接重读存储；缺扫描不推进 `last_observed_at`、零 resolved、零新 outbox） |
| generation 2 | `test_acknowledged_is_not_resolved_and_new_drop_creates_generation_two`、`test_repository_history_feeds_generation_two_after_resolved/_suppressed`、`test_generation_two_commit_passes_sql_side_generation_check`、gold `second_drop_after_resolved_creates_generation_two` |
| 并发（两进程同刻） | 新矩阵 `test_two_monitor_processes_create_one_active_alert_and_one_trigger`：两个独立连接并发提交同一低扫描 → 恰 1 个 active、1 个 triggered、1 行 outbox（advisory 锁串行化 + 事件幂等键折叠） |
| 崩溃恢复 | `test_outbox_failure_rolls_back_alert_and_source_artifact`、`test_rollback_covers_every_injected_failure_step`、`test_crash_after_insert_retries_without_duplicate`；缺扫描跨重启用例见上 |
| outbox 重投 | `test_same_outbox_delivered_twice_creates_one_notification`、`test_at_least_once_delivery_creates_exactly_one_notification`（at-least-once 投递、恰好一份通知） |
| 跨 subject | `test_subject_cannot_read_or_acknowledge_another_subject_alert`（test_api）与 test_db 的通知归属断言（他人不可读/不可确认） |
| gate disabled | `test_gate_defaults_off_and_disabled_needs_no_secret`、`test_disabled_or_unverified_exits_without_graph_or_write`、`test_no_requested_level_verified_exits_before_graph_and_before_write`、`test_disabled_policy_is_skipped_without_an_impossible_commit`、`test_run_main_disabled_prints_one_safe_line_and_exits_zero`、`test_run_main_config_error_exits_one_without_connecting` |
| 登记退场 | 新矩阵 `test_channel_source_withdrawal_suppresses_and_never_resolves_across_restart`：渠道 active → `suppressed` 审计（`updated`、零通知、零 resolved outbox），`resolved` 计数恒 0；同轮实物层照常首发不受牵连 |
| 跨策略不互喂 | 新矩阵 `test_physical_only_and_both_level_policies_do_not_cross_feed`：仅实物与两层策略并发扫同一池，各实物键恰一个 active 首发，全库零 `shop_sellable` 告警 |
| 读通道认证 | `test_read_channel_without_subject_is_unauthenticated_before_any_read`（缺 subject 401 且认证前零视图读取）+ 前端 `surfaces the list channel auth failure as an ApiError` |

## 5. 三份逐字回测结果与归属用例

1. **graph 只收到 `verified_levels` 与同样收窄的授权投影**：`MonitorRunnerDatabaseTests.test_physical_only_round_triggers_while_channel_junk_cannot_contribute`（真库、真图、SQL 记录器）证明：只核验实物时 graph 请求只含 `physical_total`，上下文店铺授权为空集、池授权只含在册池；底层即便存在过期/不完整的渠道快照、渠道独有 SKU 与无档案店铺，本轮对渠道快照**零查询**（记录器里没有任何 channel_stock 语句）；载荷（行、`data_as_of`、`source_batches`、去重全集）与 fingerprint 零渠道贡献（`test_channel_data_changes_cannot_enter_a_physical_round_fingerprint`）；未核验层在载荷里既无行也无 `*_source_unverified` 占位，信号只有固定码 `monitor_level_unverified` 与计数 `inventory_monitor_level_unverified_total{level="shop_sellable"}`（断言恰为 1）；对称用例 `test_channel_only_round_passes_only_shop_scope_and_never_touches_physical`（对实物快照零查询），两层都核验时两者都带（`test_both_verified_round_passes_both_projections`）。
2. **展示投影截断轮**：`test_truncated_display_projection_truncated` + gold `truncated_shape_empty_complete_levels_retain_all`：`inventory.truncated=true` 且出现 `inventory_display_truncated` 时 `complete_levels` 为空，open/acknowledged 保持原状态（只 `updated`、`notify=false`、`observed=false`），零 triggered、零 resolved、零 outbox；`inventory_monitor_skipped_total{reason="display_projection_truncated"}` 计 1。
3. **缺作用域引用行**：`test_physical_row_without_scope_refs_is_dropped_without_a_key`、`test_unverified_level_rows_in_payload_are_dropped_by_second_gate`：缺 `pool_ref`/`warehouse_ref`（实物）或 `shop_ref`（渠道）的 graph 行不进 `MonitorScan.rows`、不生成去重键、不触发也不解除、零事件零 outbox；只增加 `monitor_row_scope_missing`（或 `monitor_unverified_level_row`）诊断与 `inventory_monitor_rows_dropped_total{level,reason}` 计数；不借另一层级引用、不拼默认作用域、不补零。

**归属用例**（`inventory_snapshot_missing` 按"本轮真正在跑的范围"归属）：

- 在册池缺 `inventory/pools[]` 声明 ⇒ 停实物层：`test_physical_pool_claim_gap_does_make_physical_incomplete`（限制码在场）与 `test_pool_claim_gap_makes_physical_incomplete_even_without_the_code`（码缺席也按"判据是且只是池声明"补判，fail closed），真库/真图。
- 两层都核验时渠道侧信号只去掉 `shop_sellable`：本轮新增 `test_shop_side_code_only_removes_shop_sellable_when_it_runs`（`complete_levels` 仍含 `physical_total`，实物照常首发）；状态机侧输入由 gold `verified_channel_missing_rows_retained` 钉住。
- 物理轮（空店铺投影）同码缺席在结构上不可产生，出现即按投影契约违约当聚合码：本轮新增 `test_channel_code_in_a_physical_only_round_is_a_contract_violation`——`complete_levels` 清空、零迁移、该轮照常持久化、`inventory_monitor_skipped_total{reason="monitor_projection_contract_violation"}` 计 1；运维不得把它读成"实物不完整"，也不得据此改宽请求或投影。

## 6. 本机一次性 dry-run（计划 Step 3 的实际执行形态）

**已执行（fail-closed 形态，两轮；`INVENTORY_MONITOR_ENABLED=true` 只存在于驱动进程的环境里，进程退出即消失，仓库与共享配置仍为 false）：**

环境证明先行：驱动先连测试管理员连接串并打印 `current_database()=bi_agent_test`、`monitor_policies_committed=0`、`pending_outbox=0`——目标库确为本机测试库、无任何种子策略、无待投递行；全程零真实 API（监控路径本就不调用模型）、零生产/预发布接触；连接串值从未出现在任何命令、输出或日志中。

| 轮次 | 命令（`backend/`） | 退出码 | 输出（逐字） |
| --- | --- | --- | --- |
| 默认态探针 | `uv run --locked bi-inventory-monitor` | 0 | `inventory_monitor disabled` |
| 默认态探针 | `uv run --locked bi-inventory-outbox` | 0 | `inventory_outbox disabled` |
| 1 | `uv run --locked bi-inventory-monitor`（启用态） | 1 | `inventory_monitor failed policy_ref=inventory-monitor/1 code=monitor_policy_not_found` |
| 1 | `uv run --locked bi-inventory-outbox`（启用态） | 0 | `inventory_outbox selected=0 delivered=0 retried=0 dead_lettered=0` |
| 2 | 同第 1 轮两条 | 1 / 0 | 与第 1 轮逐字相同 |

运行后复查：`policies=0`、`alerts=0`、`outbox=0`——测试库回到 023 裸结构，零残留、零清理需求。启用态探针证明的是真实进程 → 真实本机库连接 → 策略装载 → fail-closed 固定码退出（monitor）与空投递无操作（outbox）。

**未执行（结构原因，如实记录，不记为通过）**：计划 Step 3 预期的"第一轮 triggered、第二轮仅 updated"在**裸 CLI 进程**里不可达成，本轮未为此改代码或改库：

1. 来源注册表是进程内的（`inventory/rules.py` 的 `_REGISTRY`；`register_inventory_source` 服务端专用、不读配置文件），而来源验收 FAIL 期间生产注册表**必须**保持为空——裸进程的逐层 gate 必然以 `monitor_source_unverified` 跳过策略，这是设计行为（fail-closed），不是缺陷。
2. 计划的命令块没有提供启用所需三件套（`BI_MONITOR_DSN`、`MONITOR_SERVICE_SUBJECT`、`MONITOR_POLICY_REFS`），照抄会以 `INVENTORY_MONITOR_REQUIRES_BI_MONITOR_DSN` 配置错误退出（`test_run_main_config_error_exits_one_without_connecting` 钉住该形态）。
3. 人为让裸进程产出 triggered 需要给本机测试库建一个可登录的 monitor 身份并手工提交种子策略/合成快照行——023 的 `bi_monitor` 是 NOLOGIN，本地例外只允许 023 顺序执行与幂等重放，两者都不在本轮授权内，故未做。

**等价证据**：产生 triggered/updated 的完整闭环在本机 `bi_agent_test` 上由 `MonitorRunnerDatabaseTests`（真图 + 真 023 提交函数 + 进程内合成登记、外层事务回滚）与 `InventoryMonitorConcurrencyTests`（跨连接并发/重启语义）证明——内容正是 Step 3 想要的"本机合成 fixture 一次闭环"，只是以受控测试形态而非裸进程形态执行。

## 7. 未执行项（逐条，均为"未执行"，不记为通过）

1. 真实快麦 API 的任何调用与真实来源取证：`physical` 四维（登记、时效策略、可重复完整扫描凭据、生产对账）仍全部 NOT MET；`channel` 一等 `data_missing`。
2. `--provider-smoke` 与 `--live`（真实模型）：未执行；`.env.test` 为 stub 模型配置，本记录不构成任何模型理解证据。
3. 目标环境（生产/预发布）迁移核对与 `bi_monitor` 生产角色实测：未执行（仅本机 `bi_agent_test`）。
4. 部署、反向代理 + OIDC、Origin、SSE、备份与真实恢复：未执行。
5. 计划任务注册（Windows Task Scheduler / systemd timer）与任何常驻循环：未执行；`docs/runbook.md` 中的两份配置是文档模板，本轮没有在任何机器注册或安装，也不创建 Codex automation。
6. 生产对账（reconciliation）与一店一周试用：未执行。

## 8. 残余风险与局限（原样记录）

- **top-20 展示投影**：来源 Artifact 是 graph 的 top-20 展示投影，本计划未实现全量决策投影；期望项超过 20 的策略作用域本轮不会出告警。500 行是请求/决策预算而非持久化量，不得描述成已全量持久化，也不得从被截断的投影推断"没出现的格子都安全"；扩大决策投影是需要所有者另行决定的变更。
- `_complete_levels` 的 `monitor_aggregate_limitation_code` / `monitor_unknown_limitation_code` 两支兜底分支暂无逐支直接测试（其余归属分支本轮已有直接用例或 gold 钉）。
- `monitor_policy_disabled` 是记录在案的张力：023 提交函数对 disabled 策略必然失败，runner 在跑图前跳过不做不可能的提交（`test_disabled_policy_is_skipped_without_an_impossible_commit`）。
- 并发矩阵的"两进程"由同一测试进程内两个独立连接模拟，所有等待有界；跨主机并发由 023 的键级 advisory lock 与唯一部分索引兜底（`test_advisory_lock_serializes_the_same_dedupe_key`）。
- 进程内固定计数随一次性进程退出即丢；生产观测 `inventory_monitor_skipped_total` / `inventory_monitor_level_unverified_total` / `inventory_monitor_rows_dropped_total` 需要指标后端接入（`docs/metrics.md` §4.9 现状列）。

## 9. 结论口径

正确的说法："持续库存通知的本地实现与本机/离线测试通过；来源验收仍 FAIL；生产监控保持 disabled。"

错误说法（任何文档不得出现，本文件亦不出现）："库存来源已核验""库存通知已就绪/已启用""渠道层已按实物层补齐""Task 11 门禁已通过"。任何生产启用、真实来源取证或层级模型变更，都需要新的、明确的所有者决定与新的日期化证据。
