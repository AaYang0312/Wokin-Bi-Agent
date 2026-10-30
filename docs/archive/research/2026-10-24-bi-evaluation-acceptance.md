# 2026-10-24 可用性与回归体系推进 —— 最终验收

计划：`docs/superpowers/plans/2026-10-24-bi-agent-reliability-and-evaluation.md`（T0–T8，共 9 项任务）。
分支：`reliability-evaluation-2026-10-24`（未推送）。开发接续记录：[checkpoint](2026-10-24-bi-evaluation-checkpoint.md)。

> **2026-10-26 返工后修订**：本报告曾声称"判定器可信""无遗留未修复的代码缺陷"，
> 均已按 2026-10-26 交付复核报告（F1–F9）撤回并修复（见 §6）。当前结论以
> 本版为准：F1–F9 已全部修复并有坏例必败证据；评测器可信范围以 §2.1 列明
> 的断言为限；仍未执行的范围见 §5。

## 0. 一句话结论

T0–T8 九项任务完成；2026-10-26 复核提出的 F1–F9 全部返工完成——两条假通过
路径（F1 删必需调用、F2 拼错期望键）已闭合并以复核坏例钉死，SSE 终态缺口
（F3）与计量/汇总/清理/脱敏/阻塞报告（F5–F9）修复，加权均价金标准（F4）
按业务合同独立计算。零外部模型 API、零快麦调用、零生产库连接、零部署；
用户未提交修改（前端 5 文件）原样保留。仍为部分完成的事项见 §5（live、
浏览器 E2E、App 层 busy 交互等）。

## 1. 最终验收命令与结果（2026-10-26 返工后复跑；同一基线、同一合成实例，串行执行）

| # | 命令（backend/ 下，除注明外） | 结果 | 退出码 |
| --- | --- | --- | --- |
| 1 | `uv run --locked --env-file ../.env.evaluation python -m unittest discover -s tests -t .` | **2142/2142 OK，0 skip** | 0 |
| 2 | `uv run --locked --env-file ../.env.evaluation python -m tests.acceptance --offline` | **35/35 pass** | 0 |
| 3 | `uv run --locked --env-file ../.env.evaluation python -m tests.evaluation.runner --mode offline --suite tests/evaluation/cases/operator_core.jsonl --repeat 3 --output ../.tmp/evaluation/rework-final` | **102/102 pass（34 场景 ×3；注意这是 34 个场景的重复运行，不是 102 个独立场景）** | 0 |
| 4 | 同 3 + `--order reverse` | 34/34 pass | 0 |
| 5 | frontend/：`npm test` → `npm run build` | **225/225 + build ✓**（用户 WIP 在场） | 0 |
| 6 | 仓库根 `git diff --check` | 干净 | 0 |

报告完整性核对（返工后 run `rework-final/summary.json`）：dataset SHA-256 与
场景文件一致；usage `partial`/`synthetic`（总量 null，不计真实成本）。缺环境时
DB 组用例 skip，skip 不计入通过。

## 2. 四栏结论

### 2.1 已验证可用（离线/合成证据，非 live 模型能力）

| 能力 | 证据 |
| --- | --- |
| 独立评测沙箱与安全边界 | 新 compose 实例（端口 55439、密码认证、仅回环）；库名恰为 `bi_agent_eval_test`、本机地址、沙箱标记三重校验，非法目标在种子前拒绝（15 用例） |
| 判定器可信性 | 47 项自检：坏值（None/空串/"bad"/布尔/NaN/∞）不等于 0；13+ 项逐字段变异全部翻红；`zip` 丢轮次不可能 |
| 扩展 runner 与报告 | 34 场景串行 ×3 + 反序逐场景一致；退出码 0/1/2；分层报告（cases/summary/report）不覆盖、脱敏（DSN/密码 ***）、零 executed 通过率 null |
| 精确选店与多轮切换 | 三轮收窄/切换、换店换窗不继承、同名澄清零查询、撤权即删引用、并行 subject 零泄漏（14 用例，agent.py 零改动） |
| 既有 35 题合同 | 同一判定器下 35/35 保持通过 |
| 前端 SSE/流契约 | 任意切点帧重组、CRLF、非法 JSON、**流中断不发 done（不完整回复不能标 complete）**、非 2xx→ApiError（223/223） |

### 2.2 正确拒答（fail-closed 行为验证）

| 场景 | 证据 |
| --- | --- |
| 大量无关档案快照 ≠ 销售超限 | 20 店 × 42 行（840 行）复现：销售照常发布、成交 SKU 参考价保留、无"缩小日期"误导 |
| 销售父行真超 500 | 499/500 出数、**501 拒出数**（披露句 + 零数据集） |
| 越窗上传不等于覆盖 | 上传 9 月数据后 10 月窗口仍 missing_data（oc-028 同族语义） |
| 覆盖断成多段不收窄 | oc-013：多段窗口整窗拒答（部分答案门禁也不放行） |
| 未授权/伪造/撤销引用 | 不解析、不扩大、被裁剪（oc-020/025 + T5 全组） |
| 拼多多边界 | 无支付能力（capability_unavailable 逐项拒绝）、无出库凭证整组排除不当 0（oc-002/016/017） |
| 毛利参考须确认 | 缺 profit_basis→needs_input；**换平台重试仍 needs_input**；只有显式确认才执行（oc-004 三轮回放） |
| 缺失格不是 0 | 支付口径未认证格 missing、图表缺值断线、真实 0 才落基线（oc-017 + ChartArtifact/ArtifactView 既有用例） |

### 2.3 已修复的代码/判定问题（含 2026-10-26 复核返工）

首轮修复（T2–T6）：

1. 判定器 `_quantize` 异常吞 0 路径（T2，迁移时删除）。
2. `zip(turns, expected)` 静默丢轮次（T2，形状校验先行）。
3. commerce needs_input/拒答回合被误判"无已发布载荷"、`status` 等键回合级与载荷级
   双义、commerce 调用数不进观测（T4/T6，`verification.py` + `acceptance._verify_turn`）。

复核返工（F1–F9，详见 §6）：假通过两条（F1 删必需调用、F2 拼错期望键）、
SSE 终态（F3）、加权金标准（F4）、usage 缺失计量（F5）、错误分母（F6）、
准备失败清理（F7）、引号密码脱敏（F8）、阻塞报告（F9）。

**已知限制（不再声称"无遗留缺陷"）**：评测器的可信范围限于 §2.1 列明并已
注册/判定/反例三件齐备的断言键；`_product_value_column` 的商品列判别仍是
登记过的启发式（判错会翻红，不会静默通过）；App 层上传 busy 交互未测
（见 §5）；T5 授权结论限于已覆盖组合，未做完整授权安全审计。

### 2.4 真实数据/产品决策/外部调用待办（本轮不做，不代所有者作答）

| 待办 | 阻塞的用户场景 | 现状 |
| --- | --- | --- |
| 商品级出库口径（或淘系支付对账取证） | 跨店单品四指标+7 天趋势（10-09 P1） | 产品决策；本轮未改口径、未把缺口写成 0 |
| 真实店铺别名映射（钉子工厂等 3 家） | 这 3 家的导出文件整文件拒绝 | 需所有者写下 shop_id 对应 |
| 渠道在售价/库存来源取证 + 版本化阈值 | 上架复核全 unsupported、库存两级 0 项可判 | 运维取证（runbook 门禁） |
| live 模型评测 | 真实模型理解准确率 | **本轮 not_run**（零外部 API；runner 已留 `--allow-live` 路径，脚本不进模型） |
| 真实来源质量对账 | "来源质量未核验"披露常驻 | 对账流程已有，未执行 |
| "上次金额最高的那家"类指代解析 | 模型侧无名称映射，跨轮指代断链 | 待设计（P2，10-09 已记录） |

## 3. 五条合成演示路径（全部可离线复跑）

运行方式：backend/ 下
`uv run --locked --env-file ../.env.evaluation python -m tests.evaluation.runner --mode offline --suite tests/evaluation/cases/operator_core.jsonl --repeat 1 --output ../.tmp/evaluation/demo`
（或单看 `tests/evaluation/cases/operator_core.jsonl` 对应场景）。

| # | 演示 | 场景 | 输入（示例轮次） | 预期结果 | 限制（如实） |
| --- | --- | --- | --- | --- | --- |
| 1 | 平台可比结果 | oc-017 | 按支付口径比 9/2–9/5 各平台支付金额 | fxg 100 元、tb/pdd 格 missing 不补零、合计 100 | 脚本模型驱动工具调用；不证明模型理解 |
| 2 | 精确店铺下钻 | oc-024 | 店铺A 同窗口支付 → 换 TB1 | 1000 → 出库口径 100 + 未认证披露 | 模型侧看不到店名映射 |
| 3 | 部分答案披露 | oc-012 | SY1 9/3–9/14 推广花费 | 仅覆盖 9/3–9/8 出 50 元 + 缺口起点披露 + 未核验披露 | 门禁默认关，场景内显式开 |
| 4 | 毛利确认 | oc-004 | 对比含毛利参考 → 缺确认 → 换平台重试 → 用户确认 | 前两次 needs_input；确认后执行、值与金标准同源 | 用户确认语义靠工具描述约束（服务端只认参数） |
| 5 | 上传后原题重查 | oc-028/029 | 上传 9 月数据后问 9/10–9/19 | 仍 missing_data（上传成功≠覆盖成功）；同窗重查值稳定不重复计数 | 合成事实；真实导出文件不在本轮 |

## 4. 项目证据摘要

- **做了什么/为什么**：复现并验真 10-09 两项修复（行数误导、校验提示）；堵住
  验收程序自身会错判通过的三个洞；把回归从"共享真实数据测试库"迁到独立合成
  实例；建立可重复、结构化、防泄漏的评测报告与 34 场景核心回归集；补齐多轮
  授权与前端流契约的证据空白。
- **复现方式**：见第 1 节命令表（全部可重跑）；沙箱建立见 runbook「评测沙箱实例」。
- **修复证据**：每个修复有先行红测试（T2 44 用例 / T4 判定器 3 处 / T6 首轮
  12 失败全部定位为期望侧或判定器缺口后修正）与全量门禁重跑。
- **性能/提升声明**：无对照实验，**不写任何"提升 X%"**；耗时仅记录为
  duration_ms/单调时钟，未做冷热对照。

## 5. 未执行与边界（如实；partial / not_run 标注）

- **not_run**：真实模型 API（live 评测）、真实平台同步/回填/对账、生产库连接、
  部署、推送、合并；未启用任何线上功能门禁（生产默认值零改动）。
- **partial**：前端只完成组件/API 两层——浏览器端到端 **not_run**；App 层
  （用户 WIP 文件内）重复上传的 busy 交互 **未测**（事实层幂等由后端与 api 层
  用例钉住）。SSE 终态契约（含完整帧后 EOF）已在 api 层修复并钉死（F3）。
- **partial**：T5 多轮授权结论限于已覆盖组合（精确名/同名/伪造/撤销/并行
  subject/时间+范围），未做完整授权安全审计。
- 拼多多零接入：本轮只出现负向边界（capability_unavailable/整组排除）。
- 现有共享库 `bi_agent_test` 与生产库 `bi_agent` 本轮零连接零写入。
- 用户前端 WIP（App.tsx 等 5 文件）未触碰、未提交、已验证与其兼容
  （含 225/225 测试与构建）。

## 6. 2026-10-26 复核返工记录（F1–F9）

每项：根因 → 修复位置 → 红→绿证据。坏例演示为 2026-10-26 在沙箱上的实际
运行输出（非复述）。

| 项 | 根因 | 修复位置 | 红测（先败） → 绿证（后过） | 坏例演示（现在必败） |
| --- | --- | --- | --- | --- |
| F1 commerce 调用断言被丢弃 | `_verify_turn` 剥掉 `call_count` 注释称交给 commerce 判定器，但后者只查最后一次调用 | `verification.py::verify_commerce_turn`（新增 `_call_bookkeeping_problems`：按轮查 `call_count` 与逐次 `call_statuses`）；oc-004 补 `call_statuses=["needs_input","needs_input"]` | `ReviewMutationTests.test_f1_removing_the_first_required_call…` 首跑 pass（假通过复现）→ 修复后翻红 | oc-004 删首次调用 → `fail`（期望2次实际1次/逐次状态错位/脚本未用完，4 条问题） |
| F2 拼错期望键静默通过 | `expected: list[dict]` 只校验轮数；内层键无人校验 | `verification.py::EXPECTED_KEYS` + `unknown_expected_keys`；`contracts.load_cases` 与 `verify_expected` 双层拒绝；35 题在用键全覆盖（含 `no_tool`，顺带从未消费变为真实判定）；修正 `_write_suite` 误用测试 3 处 | `test_f2_*` 三条首跑全红 → 修复后全绿 | oc-001 `valuez` → `load_cases` 拒绝："未登记的期望键「valuez」" |
| F3 完整帧后 EOF 无 done 仍 resolve | `sendMessage` 把字节流读完当业务完成 | `api.ts::sendMessage`（缺 done 终态抛"SSE流未以done终态结束"）；api.ts 非 WIP 文件 | 两条 review-F3 用例先红（空流/完整帧后 EOF 均 resolve）→ 修复后绿；正常 done 正例保持绿 | 前端 `rejects an empty stream…` / `rejects complete frames followed by EOF…` 用例钉死坏输入必拒 |
| F4 加权均价无断言且单店 | oc-010 请求了指标却只断销量/金额，授权集单店 | 新种子 `seed_weighted_price_case`（WP1 3件300 + WP2 1件200）；`verify_product_turn` 支持聚合行（空 shop_ref）；oc-010 金标准按业务合同手算：加权=500/4=**125**，均店均价陷阱=150 | `test_f4_oc010_asserts_independently_derived_weighted_price` 首红（仅1店无总行）→ 重建后绿 | 期望偷换成 150 → `fail`（"合计.weighted_avg_paid_price期望150，实际125"） |
| F5 失败调用 usage 消失 | `finally` 只计数，异常路径不追加 usage 记录 | `telemetry.py::MeteredModel.complete`（异常即 append None 再抛） | `test_failed_model_call_is_counted_as_missing_usage` 首红（records=1、complete）→ 绿 | 演示：超时+成功 → records=2、`partial`、totals=None、known=15 |
| F6 error 不进分母 | `executed` 只含 pass/fail | `runner.py::_summarize`（error 计入 executed） | `test_f6_errors_count_in_the_executed_denominator` 首红（pass_rate=1.0）→ 绿（0.5） | 1 pass + 1 error → executed=2、pass_rate=0.5、退出码仍 1 |
| F7 准备失败覆盖异常且跳过清理 | seed/模型工厂在内层 `try/finally` 之外，失败时 `metered` 未赋值 | `runner.py::_run_case` 重排：savepoint 后单一 try/except/finally，`metered` 可缺省，逐题回滚/释放全路径执行 | `test_seed_failure_keeps_original_error…` 首红（UnboundLocalError）→ 绿 | 演示：seed 抛 RuntimeError → error_class=**RuntimeError**，紧接正常场景 pass（连接/存点完好） |
| F8 引号密码残留 | 只匹配不带引号的 `password=\S+` | `reporting.py::sanitize`（单/双引号整段掩蔽，含空格） | `test_quoted_password_with_spaces_is_fully_masked` 首红（SecretTail 残留）→ 绿 | `password='Alpha SecretTail'` → 无 Alpha/SecretTail 残留 |
| F9 阻塞不落报告且口径不一致 | 阻塞路径早返回不写报告；attempted 未乘 repeat | `runner.py`（`blocked()` 统一出口：写报告 + attempted/blocked=场景×repeat） | `test_blocked_run_writes_report_and_counts_repeats` 首红 → 绿 | 无 DSN + repeat=3 → 三份报告文件、attempted=blocked=3、退出码 2 |

返工提交：`3cafadc`（F1/F2）、`50432b4`（F3）、`059da8a`（F4）、`6a3a4cb`
（F5–F9）。全部坏例演示于 2026-10-26 在独立合成沙箱实跑通过（失败/拒绝侧）。
