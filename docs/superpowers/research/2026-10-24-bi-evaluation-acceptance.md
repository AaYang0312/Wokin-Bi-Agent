# 2026-10-24 可用性与回归体系推进 —— 最终验收

计划：`docs/superpowers/plans/2026-10-24-bi-agent-reliability-and-evaluation.md`（T0–T8）。
分支：`reliability-evaluation-2026-10-24`（8 个提交，`c671995..4cd84ea`；验收时源码
HEAD `4cd84ea`，其后仅文档提交）。开发接续记录：[checkpoint](2026-10-24-bi-evaluation-checkpoint.md)。

## 0. 一句话结论

全部 8 个任务按计划完成：既有两项 10 月 9 日修复验真通过、判定器与回合接缝的
3 个可复现缺陷修复、独立合成评测沙箱 + 判定器自检 + 扩展 runner/报告 + 34 场景
回归集 + 多轮授权回归 + 前端流契约全部落地并全绿。零外部模型 API、零快麦调用、
零生产库连接、零部署；用户未提交修改（前端 5 文件）原样保留。

## 1. 最终验收命令与结果（同一基线、同一合成实例，串行执行）

| # | 命令（backend/ 下，除注明外） | 结果 | 退出码 |
| --- | --- | --- | --- |
| 1 | `uv run --locked --env-file ../.env.evaluation python -m unittest discover -s tests -t .` | **2129/2129 OK，0 skip** | 0 |
| 2 | `uv run --locked --env-file ../.env.evaluation python -m tests.acceptance --offline` | **35/35 pass** | 0 |
| 3 | `uv run --locked --env-file ../.env.evaluation python -m tests.evaluation.runner --mode offline --suite tests/evaluation/cases/operator_core.jsonl --repeat 3 --output ../.tmp/evaluation/final-001` | **102/102 pass（34 场景 ×3）** | 0 |
| 4 | 同 3 + `--order reverse`（T6 执行） | 34/34 pass | 0 |
| 5 | frontend/：`npm ci` → `npm test` → `npm run build` | **223/223 + build ✓**（用户 WIP 在场） | 0 |
| 6 | 仓库根 `git diff --check` | 干净 | 0 |

报告完整性核对：`final-001/summary.json` 的 `dataset_sha256` 与
`operator_core.jsonl` 文件 SHA-256 **逐字节一致**
（`df5578ef…644ff2`）；`source.git_head=4cd84ea`（dirty=True 如实记录了当时
未提交的文档改动）。usage 聚合为 `partial`（脚本未全量声明 usage）且来源
`synthetic`——**没有计成真实成本，总量为 null**。缺环境时 DB 组用例 skip
（如 `tests.test_evaluation_runner` 无 env 跑 28 过 6 skip），skip 不计入通过。

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

### 2.3 本轮修复的代码/判定问题（均已修复并有回归）

1. 判定器 `_quantize` 异常吞 0 路径（T2，迁移时删除）。
2. `zip(turns, expected)` 静默丢轮次（T2，形状校验先行）。
3. commerce needs_input/拒答回合被误判"无已发布载荷"、`status` 等键回合级与载荷级
   双义、commerce 调用数不进观测（T4/T6，`verification.py` + `acceptance._verify_turn`）。

无遗留未修复的代码缺陷。

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

## 5. 未执行与边界（如实）

- 未跑任何真实模型 API（live not_run）、真实平台同步/回填/对账、生产库连接、
  部署、推送、合并；未启用任何线上功能门禁（生产默认值零改动）。
- 拼多多零接入：本轮只出现负向边界（capability_unavailable/整组排除）。
- 现有共享库 `bi_agent_test` 与生产库 `bi_agent` 本轮零连接零写入。
- 浏览器端到端未做（T7 仅组件/API 两层）。
- 用户前端 WIP（App.tsx 等 5 文件）未触碰、未提交、已验证与其兼容。
