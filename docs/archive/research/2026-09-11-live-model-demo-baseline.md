# 2026-09-11 真实模型演示基线（已退役）

> 2026-10-31 从 `docs/demo.md` 原样迁出，仅供追溯。当前真实模型验收状态见 [项目状态](../../PROJECT_STATUS.md)。

## 真实模型验收基线（2026-09-11，首次执行；已退役为历史基线）

> **2026-09-14 状态：** 本节用的是旧 20 题题库，只能作为历史基线。题库已扩到修订后的 26 题，
> 真实 provider 的 smoke 与 26 题 live **尚未重跑**（本轮不接触模型凭证），因此本节数字
> 不得引用为多来源或四工作流的真实模型验收结果。

此前 `docs/metrics.md` 与运行手册都把真实模型联调标为"未实测"。本节是第一次执行结果。

- 模型：`deepseek / deepseek-v4-flash`（唯一 provider，无自动切换）
- 数据：**独立合成验收库** `bi_agent_acceptance`（本机实例内新建，按 001→009 初始化），
  题库为 `backend/tests/questions.jsonl` 的 20 题
- 结果：**通过 14 / 20**，平均单题 3.6 秒，最慢 13.3 秒；`--offline` 仍为 20/20

失败分类（逐条查证，不统一归给"模型不行"）：

| 题 | 现象 | 查证结论 |
| --- | --- | --- |
| 07 / 12 / 20 | 期望 `metrics=['paid_amount']`，实际多带了 `aov`/`paid_orders`/`refund_amount` | 数据与金额正确；是题库断言**要求指标集合完全相等**。是否允许"多取指标"是口径决定，未擅自放宽断言 |
| 06 | 把"9月1日至7日比前7天"解析成 8/25–9/8 且 `compare=none` | **真实的模型日期推理缺陷**：窗口与比较方式都错，连带数值错（500 vs 1000） |
| 16 | 期望 `evaluate_promotion` 返回 `missing_data`，实际 `ok` | 单独调用该工具核实：确实返回 `missing_data` +「尚未取得推广实耗…只能做明确假设的预算测算」，**没有编造广告数据**。失败在模型的工具选择/路由 |
| 18 | `remaining_days` 期望 2、模型算 3 | 题库期望按 `end - spent_through` 计，模型按含首端计。属**未定义口径**，需先定规则再改断言 |

结论：本轮暴露的 6 项里没有一项是"金额算错"或"数据被编造"——确定性工具与门禁都按预期工作；
缺陷集中在模型的时间解析与工具路由，以及题库断言口径过窄。

### 执行方式与一个安全前提

`tests.acceptance --live` 会把 `seed_business_case` **写进 `BI_TEST_ADMIN_DSN` 指向的库**。
本仓库的 `bi_agent_test` 里存有真实同步数据，直接指过去会污染它，因此必须先建独立库：

```powershell
psql -d postgres -c "CREATE DATABASE bi_agent_acceptance"
# 按运行手册顺序对其执行 001 → 009
uv run --env-file ../.env.app python -m tests.acceptance --live
```

另需注意：`--live` 只从环境变量取模型配置。若沿用 `.env.test` 里的桩模型，它会以
`"model": "fake-model"` 静默跑完并报出 17 题失败——那不是产品结果。执行前必须确认
输出的 `model` 字段是真实型号。
