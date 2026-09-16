# 隔离分析：本地验收记录

日期：2026-09-16（文件名按[实施计划](../plans/2026-09-14-isolated-analysis-agent.md) Task 6 的
`2026-09-14-isolated-analysis-acceptance.md` 索引；验证矩阵实测于 2026-09-16）。
基线：`main`，Task 1–5 已提交至 `262ce61`（`feat: add isolated artifact analysis tool`）。
Task 6 本轮只改前端展示与文档，共九个文件：`frontend/src/types.ts`、
`frontend/src/components/ArtifactView.tsx`、`frontend/src/styles.css`、
`README.md`、`docs/runbook.md`、`docs/metrics.md`，新增
`frontend/src/components/AnalysisArtifact.tsx`、`frontend/src/components/AnalysisArtifact.test.tsx`
与本文（完整清单与逐文件内容见 §4），**不改任何
`backend/**` 实现、迁移、配置或依赖**。
范围：**仅本机 `*_test` 数据库与离线/合成环境**。本文不构成发布就绪声明，也不改变 Task 11
统一发布门禁的状态（其第 4–7 项仍 open）。

口径依据：[Task 11 后置子项目总设计](../specs/2026-09-14-post-task11-subprojects-design.md)
§3–§4、§8、§10–§12；[隔离分析实施计划](../plans/2026-09-14-isolated-analysis-agent.md)
Task 1–6；[隔离分析本地开发例外](2026-09-16-isolated-analysis-local-development-exception.md)；
[Task 11 日期化发布验收](2026-09-14-task-11-release-acceptance.md)；
[受控 SQL 探索本地验收](2026-09-14-controlled-sql-acceptance.md)；
[approved 查询学习记忆本地验收](2026-09-14-approved-query-memory-acceptance.md)。

## 0. 一句话结论

**隔离分析（计划 Task 1–6）在 HEAD `262ce61` + Task 6 展示切片上通过本地验收：后端全量
1497 项零失败；`tests.test_analysis` 98 项、`tests.test_db + tests.test_runtime_db` 157 项
串行零失败；离线 26 题 26/26（门禁关闭）；前端 141 项（10 files，含 Task 6 新增 34 项）与
`tsc -b && vite build` 零失败。数值结论全部由 Decimal 纯函数产生并与 13 例手算 gold set
逐项一致，顺序与键序不变性有测试钉住；模型只以字面空工具列表总结已验证 finding，失败时
findings 照常发布；无证据因果/行动说法只能进 `unsupported_claims`；所有模型前拒绝
（归属/授权/类型/版本/大小/覆盖/fingerprint）都映射到稳定码——其中跨属主与不存在刻意
不可区分——且不重查数据库；来源读取故障走
`unavailable` 通道且零模型调用零写入；持久化失败整次失败、不发布任何分析结果；
`isolated_analysis` 领域与 `analysis_result` 类型经 022 进 CHECK，幂等可重放。
`ISOLATED_ANALYSIS_ENABLED` 默认 `false`，回滚 = 保持/改回 `false`。**

缺的是**外部证据而不是代码路径**：真实 provider 下的叙述质量、目标环境迁移与授权核对、
HTTP 生产启用、部署与试用一律记 `未执行`（§6）。已知限制逐条见 §5。

## 1. 五个实现提交（Task 1–5，全部经子代理工作流 fresh 独立评审）

| 提交 | 计划切片 | 评审结论（工作流子代理证据） |
| --- | --- | --- |
| `a3bbbe0` | Task 1：严格契约、默认关闭门禁、迁移 022 | 首轮 P1（`Finding.values` 键未递归拒 `sql/prompt/tool_calls/raw_rows`）已在模型层与 runtime 层同时修复，附 8 条负向回归；复审 OK |
| `bdbf585` | Task 2：授权不可变 Artifact loader | OK with notes，无 P0/P1（3 条非阻塞 P2 随验收接受） |
| `e2ca29f` | Task 3：确定性贡献/变化/MAD + 13 例 gold set | 首轮 gold 缺"重复行/极大 Decimal"两类已补齐并由 fresh 评审逐项手算；终审 OK |
| `7e4240f` | Task 4：无工具叙述 summarizer + import 守卫 | 首轮两项 P1 已修复；父级加固四项 P2（逐值数字证据、普通内部绝对导入不崩溃、20 条限制上限合并派生码优先、因果假设进 unsupported）后 fresh 复审 OK with notes |
| `262ce61` | Task 5：分析图、Artifact 持久化与 Agent Tool | 首轮评审补交结构化结论 OK with notes；两项 P2 加固（来源读取故障稳定降级通道、`agent.py` 注释如实化）后 fresh 复审 VERDICT: OK |

Task 6（本切片）未提交；工作树恰好只含以下九个文件改动，暂存区为空：
`frontend/src/types.ts`、`frontend/src/components/ArtifactView.tsx`、`frontend/src/styles.css`、
`README.md`、`docs/runbook.md`、`docs/metrics.md`、`frontend/src/components/AnalysisArtifact.tsx`（新）、
`frontend/src/components/AnalysisArtifact.test.tsx`（新）、
`docs/superpowers/research/2026-09-14-isolated-analysis-acceptance.md`（新，即本文）。

## 2. 验证矩阵（代码位置 = `backend/tests/test_analysis.py` 等处的真实测试名）

| 计划要求 | 证据（测试名，全部在本轮实测套件内通过） |
| --- | --- |
| 门禁默认关、严格 true/false | `test_core.py::test_isolated_analysis_gate_defaults_off_and_accepts_only_true_false`；`.env.example` 恰好一处 `ISOLATED_ANALYSIS_ENABLED=false` |
| 门禁关闭时 Tool 不注册、伪造调用不可见 | `test_core.py::test_disabled_gate_keeps_a_forged_analysis_call_unknown` |
| 授权拒绝（非属主/跨属主不可区分） | `test_wrong_owner_type_version_and_size_fail_before_projection`、`test_not_found_and_cross_owner_are_indistinguishable`、`test_not_found_and_cross_owner_are_needs_input` |
| 类型/版本/覆盖拒绝，全部在模型前 | `test_only_dataset_schemas_are_sources`、`test_type_and_version_refusals_stay_unavailable_without_a_model`、`test_coverage_must_be_complete_or_gapped_partial`、`test_future_data_as_of_is_rejected` |
| 大小/规模拒绝 | `test_row_field_budget_is_enforced`、`test_canonical_projection_size_is_bounded`、`test_oversized_metric_refuses_in_compute_before_any_model_call` |
| fingerprint 稳定与篡改拒绝 | `test_fingerprint_is_stable_across_key_and_row_order`、`test_dataset_carries_frozen_source_identity`、`test_internal_identifier_tampering_is_rejected_before_projection` |
| import 守卫（禁模块/动态导入/语法错误） | `test_isolated_modules_import_only_approved_surface`、`test_guard_rejects_every_planned_forbidden_module`、`test_dynamic_import_channels_are_rejected`、`test_guard_rejects_repository_tool_and_sync_channels`、`test_absolute_internal_import_returns_a_verdict_not_a_crash`、`test_syntax_errors_fail_closed` |
| 字面空工具列表 + 调用形状 AST 钉住 | `test_model_receives_findings_and_empty_tools`、`test_summarizer_pins_the_exact_model_call_shape` |
| MAD/gold/顺序不变 | `test_every_gold_case_matches_field_by_field`、`test_gold_findings_survive_reversed_input_order`、`test_gold_expectations_are_not_tautological`、`test_mad_flags_outlier_without_float`、`test_mad_zero_non_median_is_encoded_as_a_code`、`test_output_is_invariant_under_observation_and_key_order`、`test_results_are_deterministic_for_identical_inputs` |
| 模型失败仍发布确定性 findings | `test_model_failure_keeps_deterministic_findings`、`test_empty_reply_degrades_to_narrative_unavailable`、`test_overlong_malformed_reply_degrades_without_losing_findings`、`test_no_model_and_short_deadline_skip_the_model_call`、`test_model_failure_still_persists_deterministic_findings` |
| 无证据说法只进 unsupported | `test_uncited_causal_claim_is_not_published_as_fact`、`test_causal_and_action_language_stays_out_even_with_valid_citations`、`test_causal_wording_is_unsupported_in_hypotheses_too`、`test_numeric_evidence_is_checked_per_value_not_across_boundary` |
| 持久化 fail closed | `test_artifact_failure_publishes_no_result`（零 Artifact、模型载荷无 findings） |
| 来源读取故障 → 稳定 unavailable | `test_source_read_outage_fails_closed_as_unavailable`（两个读取点分别注入；零模型调用、零写入、异常原文不进任何公开位置） |
| deadline 纪律 | `test_expired_deadline_never_loads_or_calls_model`、`test_remaining_under_two_seconds_skips_the_model_call`、`test_remaining_deadline_is_passed_as_timeout` |
| 022 幂等与白名单 | `tests.test_db` 的隔离分析迁移类（022 重放两次、逐值保留、未知 domain/type `CheckViolation`） |
| Task 6 前端白名单渲染 | `frontend/src/components/AnalysisArtifact.test.tsx` 34 项：确定值与来源链接、unsupported 隔离、缺源/错型/多匹配禁用、未知字段/畸形 ref/fingerprint/版本/findings 整卡拒绝、假设标“待验证”、限制常显不折叠、组序固定与组内载荷序、敌意文本转义、载荷不外泄；份额换算 4 例（精确十进制串）：负份额 `−0.125000→−12.50%`、前导/尾随零 `0.005→0.50%` / `0.050000→5.00%` / `0.500000→50.00%`、半偶舍入 `0.01005→1.00%` / `0.01015→1.02%`、超大比值 `12345678901234567.890000→1234567890123456789.00%`（二进制浮点会漂成 …800.00%） |

## 3. 命令与通过数（本轮父级实测，2026-09-16）

以下命令全部由本会话实际执行；输出摘要逐条对应。**没有执行过的命令不列在这里。**

| 命令（`backend/` 下，除注明外） | 结果 |
| --- | --- |
| `uv run --locked --env-file ../.env.test python -m unittest tests.test_analysis` | 98 tests OK |
| `uv run --locked --env-file ../.env.test python -m unittest`（全量） | 1497 tests OK |
| `uv run --locked --env-file ../.env.test python -m unittest tests.test_db tests.test_runtime_db`（本机 `*_test`，串行） | 157 tests OK |
| `uv run --locked --env-file ../.env.test python -m tests.acceptance --offline` | 26/26 pass（门禁关） |
| `frontend/`：`npm test` | 141 passed（10 files，含 Task 6 新增 34 项） |
| `frontend/`：`npm run build`（`tsc -b && vite build`） | 通过 |

红灯先证（Task 6）：`AnalysisArtifact.test.tsx` 先写、先跑——`npm test -- --run
src/components/AnalysisArtifact.test.tsx` 在组件不存在时 FAILED（模块未找到），实现后同
命令转绿；组件首个实现修出 1 条断言与夹具意图不符（组内载荷序），修正断言后 30 项全绿。评审修复轮的第二段红→绿：份额换算从二进制浮点改为精确十进制串（ROUND_HALF_EVEN）之前，同命令为 **2 failed / 32 passed**（半偶舍入 `0.01005→1.00%` 与超大比值两例失败）；实现后同文件 **34 项全绿**（负份额与前导/尾随零两例在两种实现下均通过，如实作为回归钉而非红→绿证据）。

## 4. Task 6 变更文件（全部在计划允许清单内）

| 文件 | 动作 | 内容 |
| --- | --- | --- |
| `frontend/src/components/AnalysisArtifact.tsx` | 新建 | 白名单渲染器：`parseAnalysisArtifact` 按后端 `_analysis_payload` 同一纪律整体验形（键集恰好相等、正则/边界镜像、悬空叙述引用拒绝）；按 kind 分块渲染；占比/变化率只做 ×100 单位写法；假设标"待验证"；unsupported 进独立折叠区；`resolveAnalysisSource` 只认同消息唯一匹配且类型为三种数据集之一的来源 |
| `frontend/src/components/AnalysisArtifact.test.tsx` | 新建 | 34 项（清单见 §2 末行；含 4 个份额换算用例：负份额、前导/尾随零、半偶舍入、超大比值不漂移） |
| `frontend/src/components/ArtifactView.tsx` | 修改 | `analysis_result` 分发到 `AnalysisArtifact`（既有三分发模式照搬） |
| `frontend/src/types.ts` | 修改 | 分析载荷的展示侧词表（校验后的视图类型，注释声明"类型不是信任凭据"） |
| `frontend/src/styles.css` | 修改 | 分析卡片样式块（折叠区、徽标、来源按钮） |
| `README.md` | 修改 | 新增"隔离分析（默认关闭）"小节；迁移链 001 → 022 |
| `docs/runbook.md` | 修改 | 新增"隔离分析（默认关闭）"运维小节（门禁/前置/版本推进/来源失权/回退/诊断）；迁移链与缺失影响补 022 |
| `docs/metrics.md` | 新增 §4.8 | 四个定名指标的现状与派生口径、label 边界、不得读出的结论 |
| `docs/superpowers/research/2026-09-14-isolated-analysis-acceptance.md` | 新建 | 本文 |

## 5. 已知限制（如实披露，均非阻塞）

1. **单值内数字片段可命中**：数值逐字校验按"token 出现在单个引用值内"判定，`"30"` 是
   `"12.30"` 的子串；评审已披露，属逐字语义的既定读法，后端量化输出使实际风险有限。
2. **允许根的星号导入可过 AST 守卫**：`from json import *` 形式不被动态导入规则命中；
   暴露面以 import 白名单收窄，守卫定位是回归绊线而非沙箱。
3. **分析叙述质量无真实模型证据**：全部降级/守卫证据来自替身模型与合成载荷。
4. **前端占比百分号写法**：×100 为展示单位换算，原始量化串保留在 `data-value` 审计属性。

## 6. 未执行（缺外部证据，不构成发布就绪）

- 真实 provider（Qwen/DeepSeek）下的分析回合与叙述质量实测 —— **未执行**。
- 目标环境迁移核对（001→022、`bi_app`/`bi_reader` 生产角色）—— **未执行**。
- HTTP 生产启用（与 `CONTROLLED_SQL_ENABLED` 先例一致刻意延后，需单独的所有者决定）
  —— **未执行**。
- 部署、反向代理、OIDC、SSE、备份恢复演练与单店试用 —— **未执行**。
- **Task 11 统一发布门禁第 4–7 项仍 open**：本子项目只依赖其 Task 1–3 已交付的
  Artifact/契约面，不以任何方式宣布这些门禁通过。
