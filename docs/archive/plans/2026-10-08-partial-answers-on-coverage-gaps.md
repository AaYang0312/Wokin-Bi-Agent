# 技术方案：覆盖缺口的分级部分答案（拒答不连坐）

日期：2026-10-08。基线：`main@b7968e6`。

前置取证（本会话调研，2026-10-08）：

- 现状接缝：非分区查询路径覆盖不完整一律**整体** `missing_data`——`metrics.py:895`（"覆盖未完成，拒绝部分汇总"）；`business_query/nodes.py:485` 注释明说"非分区路径不会产生 ok+partial（覆盖不完整在 metrics 里先降级成 missing_data）"。
- 可沿用的先例：未认证付款时间口径**可出数+披露**（`metrics.py:874`）；来源质量 unknown **可出数+披露**（`metrics.py:903`，先例记录 `research/2026-09-25-s3-acceptance.md`）；分区榜 ok+partial → `DomainStatus.PARTIAL` 的分类与载荷通路已存在且通用（`business_query/nodes.py:480-486`）。
- sycm 手工线（`2026-10-02-manual-sycm-export-import.md`，M1–M5 已随 `b7968e6` 落地）：推广花费/ROAS 等指标在 2 家映射店 × 61 天窗口可答；**越窗 → 既有 missing_data + remediation 上传闭环**。该计划 M3 明确保留了"窗口不足 → missing_data"的尾巴，本计划接这个尾巴。
- 公开 `Coverage` 契约已带 `gaps`（日期串）与 `suggested_window`（`metrics.py:175-182`）；服务端 `CoverageAssessment` 已有 `covered_windows / missing_windows / data_as_of`（`data_quality.py:131`）。
- 语义目录已有"参考口径变体"先例：`metric-product-gross-profit-reference`（`semantic_catalog/registry.py:407`，明示未扣广告费/售后/运费）。

关联文档（必读）：

- `docs/superpowers/plans/2026-10-02-manual-sycm-export-import.md`（manual 来源、remediation 块、口径隔离）
- `docs/superpowers/specs/2026-09-12-multi-source-metrics-design.md` §3–§6（来源注册、能力与覆盖契约）
- `docs/metrics.md`（指标口径）、`docs/runbook.md`（来源注册与回退）
- `research/2026-09-25-s3-acceptance.md`（"未核验可出数+披露"先例）

**已锁定方向（2026-10-08 所有者决策）**：把"整体拒答"升级为"可分解的部分答案"——能认证的部分出数 + 服务端派生披露；拒答范围收缩到**真正不可认证的指标/窗口**。不估算、不补零、不混口径。

## 0. 目标与原则

目标两件事：

1. **窗口级部分答案**：时间聚合类指标在覆盖部分窗口时，按已覆盖 span 出数并披露实际窗口与缺口（例：sycm 手工导入盖到 9/28，问 9 月 → 出 9/1–9/28 的数 + "9/29–30 缺"）。
2. **指标级拆分**：一次请求多个指标时，可答指标照常出数；不可答指标逐项枚举原因与 remediation——"问净利和推广费，推广费缺来源"不再连累净利一起拒答。

原则（全部沿用既有纪律，不重开）：

1. **Fail-closed 不动摇**：缺口永不补零、不外推、不跨口径混算（sycm `paid_amount_sycm` 与 ERP `paid_amount` 隔离测试保持绿）；"部分"必须由服务端派生披露，模型不得自行宣称覆盖完整。
2. **可分解性白名单**：只有时间求和/计数类指标允许窗口部分出数；比值类仅当分子分母**同源同表**（行级同窗，如 ROAS）才允许；库存时点类永不部分出数。
3. **出数窗口只收窄不改写**：原请求窗口按 `Coverage.start/end` 原样返回（既有契约），实际出数窗口单独披露。
4. 一切新 gate 默认关闭（`PARTIAL_ANSWERS_ENABLED=false`）；关闭与变量缺席时，工具行为、载荷与聊天结果与现状**逐字一致**。
5. 不新增依赖、不新增迁移（覆盖凭证在 `bi.sync_state`、运行记录既有表已足够）。

## 1. 范围

**做**：`query_business` 金额/量类聚合查询（含 sycm manual 指标）的窗口级部分答案；请求内指标集拆分；`partial_answer` 公开载荷与前端披露渲染；agent 提示规则与离线题库扩展。

**不做（明确排除）**：跨源比值类的部分出数；库存时点类部分出数；`compare_performance`（分组级部分答案已存在，不动）；复合口径降级链（`gross-profit-reference` 模式的自动降级——开放问题）；对聊天最终答案文本的服务端 claim guard（开放问题）。

## 2. 设计决策

- **D1 不新增 DomainStatus**：部分答案 = 让非分区路径也产出 `ok + coverage.status=partial`，走**既有** `DomainStatus.PARTIAL` 分类（`nodes.py:480` 本就通用，只是从没人从非分区路径触发它）。
- **D2 可分解性判定是纯函数**：输入 `(assessment, metric set)` → `{answerable_spans, refused: [metric → 稳定原因码]}`。规则：聚合类 → covered spans；同源比值 → 分子分母**公共** covered span；跨源比值 / 时点类 → 拒。covered 为空，或 covered 为空且 `data_as_of is None` → 整体 `missing_data`（现状不变）。
- **D3 披露块服务端派生**：`metric_result` Artifact 新增白名单键 `partial_answer{answered_windows, missing_windows, metric_refs, disclosure}`，`disclosure` 是服务端固定模板文案；模型可见但不可生成（`runtime/models` 白名单验形 + 前端镜像验形，同 remediation 先例）。
- **D4 执行 = 窗口收窄的同一 SQL 模板**：不改 SQL 生成面，只把窗口参数换成服务端算出的 covered spans；预算、行数上限、口径隔离全部沿用。
- **D5 拆分优先级**：先按指标拆（可答子集出数、拒答子集枚举），再对可答子集做窗口部分；全部不可答 → `missing_data`，但载荷必须带**逐项**原因与 remediation，不再一句"覆盖不足"。
- **D6 与 recovery / remediation 的交互**：部分答案**优先**于"建议缩小窗口重试"；`suggested_window` 降级为附带 limitation（想拿完整数的指引）；缺口可补时 remediation 块照常携带。

## 3. 切片任务书（红先行）

### T1 公开契约与红测试
- 定义 `partial_answer` 载荷形状并进 `runtime/models.py` 白名单与前端镜像（`frontend/src` 验形，参照 remediation 卡做法）。
- 红测试四条：聚合类+部分覆盖 → PARTIAL 且带披露键；同源比值+部分 → PARTIAL；跨源比值/库存+部分 → 仍 `missing_data`；gate 关闭 → 与现状逐字一致。

### T2 可分解性判定（纯函数）
- 新纯函数（放 `data_quality` 或 `business_query`，不连库）：`(assessment, metrics) → 分解结论 + 稳定原因码`。
- 红测试覆盖边界：covered 空、`data_as_of is None`、单指标 vs 混合指标集、manual 与 ERP 指标混合请求、同源比值公共窗口计算。

### T3 执行与分类改造
- `metrics.py:895` 接缝：判定允许 → 收窄窗口执行 → `ok + partial` 载荷（covered/missing 窗口、disclosure、remediation）；不允许 → 现状 `missing_data` 原样。
- `_classify` 不动（已通用），补非分区路径 ok+partial 用例。
- SQL 生成面与预算机制零改动（扫描测试钉住）。

### T4 指标集拆分
- 请求指标集按 T2 结论拆分执行：可答子集正常查询；拒答子集进 `payload.refused`（metric_ref → 原因码）并逐项挂 remediation。
- 红测试：`{paid_amount 可答, ad_spend 无来源}` → paid_amount 出数 + ad_spend 枚举原因；全拒 → `missing_data` 且带逐项原因。

### T5 模型契约与前端
- agent 提示规则：部分答案**必须**复述实际覆盖窗口与缺口，禁止把部分数说成完整数；违反即按既有提示纪律处理。
- 离线题库扩展 ≥3 题：越窗聚合可答（含披露断言）、混合指标集不连坐（含逐项原因断言）、"不得宣称完整"断言。
- 前端 `ArtifactView` 披露渲染（覆盖窗口徽标 + 缺口日期段，白名单验形，非法整卡拒绝）。
- `exploration_attacks` 语料补充：篡改模型载荷试图移除/伪造 `partial_answer` 不得影响服务端披露。

### T6 验收与文档
- 既有离线题库零回归；后端全量门禁（unittest discover + acceptance --offline）+ 前端 `npm test` / `npm run build`。
- `runbook.md`（gate、行为变化、回退 = 关 gate）与 `docs/metrics.md`（部分答案口径与披露语义）更新；acceptance 结论写 `research/`。

## 4. 工程与流程约束（写给执行 agent）

红先行（T1–T4 每片先有失败测试再实现）；共享库测试串行；单写者 + 独立复审 + 父级门禁 + 单 Task 提交；不新增依赖；无迁移故无 `*_test` 重放项；gate 默认关且关闭态逐字一致由测试钉住；`disclosure` 与 remediation 文案全部服务端常量，模型可见载荷中不含任何"可自行宣称完整"的措辞；sycm/ERP 支付金额隔离扫描测试保持绿；开工前报告工作树状态，存在非本 Task 改动先确认归属。

## 5. 风险与开放问题

- **部分数被当完整数**：三重防护——服务端披露模板措辞（"仅覆盖 X~Y，Z 起缺口"）、前端显著渲染、题库断言；对最终答案文本的服务端 claim guard 列为开放问题（isolated-analysis 的 claim 守卫模式可参考）。
- **covered span 极短也出数**（如 61 天只盖 1 天）：诚实但低价值；v1 规则是不设阈值照样出数 + 必附 `suggested_window`；是否加"极低覆盖率提示"留待实测。
- **复合口径降级链**（含广告净利 → 不含广告毛利参考的自动降级）：等真实复合指标登记需求出现再设计，本轮只在 `metrics.md` 留概念注记。
- **与回填竞态**：covered spans 在请求时冻结（`CoverageAssessment` 既有语义），回填后同一问题自然变完整；不额外处理。

## 6. 发布形态（本方案完成时）

`PARTIAL_ANSWERS_ENABLED=false` 默认；本地启用步骤与回退写入 runbook；生产启用为所有者显式决定（沿用受控探索、隔离分析、sycm 导入的延后模式）。完成态：越窗聚合问题给"覆盖窗口内的数 + 缺口披露 + 上传/等回填指引"；混合指标集不再连坐拒答；既有题库与全部门禁零回归。
