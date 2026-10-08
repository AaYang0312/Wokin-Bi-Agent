# 覆盖缺口部分答案验收记录

日期：2026-10-08
计划：`docs/superpowers/plans/2026-10-08-partial-answers-on-coverage-gaps.md`
实现基线：`main@c6e56d6`（T1–T5）。本文、runbook 与 metrics §4.11 随 T6 提交。

## 0. 结论

部分答案按计划落地，门禁默认关闭。关闭时既有 32 道离线题行为不变（题 31 仍是越窗
`missing_data`）。开启后，单段已覆盖窗口可以出数，没有来源的指标不再连坐其余指标。

本次是离线验收：模拟模型消息序列加本机测试库。它不证明模型理解准确率，也不构成任何
平台真实来源就绪的证据。未跑 live 模型，未访问生产库，未改拼多多边界。

## 1. 行为

- 开关：`PARTIAL_ANSWERS_ENABLED`，缺省 `false`，只认 `true` / `false`。
- 单段窗口：同一条 SQL，窗口收窄到已覆盖段；`status=ok`，`coverage.status=partial`，
  原请求窗口留在 `coverage.start/end`。披露句为「仅覆盖 {起~止}，{缺口起点} 起缺口」。
  可上传的缺口仍带 `remediation`。
- 不收窄：多段窗口、跨源比值、时点库存。
- 指标拆分：无来源指标进入 `refused`（指标名与已登记原因码）。公开结果可带逐项上传提示；
  模型结果只保留原因。两边都能答的 ERP 指标与生意参谋指标仍拒绝合并。
- 回退：把门禁改回 `false` 后重启。无迁移。

## 2. 离线题

题集 35 题。01–32 不打开门禁。新增三题只在该题上打开门禁：

| 题 | 断言 |
| --- | --- |
| 33 | SY1 越窗推广花费出已覆盖的 30 元，披露句、缺口与上传提示都在 |
| 34 | PS1 支付金额 100 元出数，`ad_spend` 因 `capability_unavailable` 被逐项拒绝 |
| 35 | 回答必须复述实际窗口与缺口起点，正文不得出现「完整」 |

## 3. 本次门禁

T6 提交前在本机 `*_test` 上重跑：

- `python -m unittest discover -s tests -t .`：2001 项通过
- `python -m tests.acceptance --offline`：35/35 通过
- `frontend/`：`npm test` 214 项通过，`npm run build` 通过

若重跑计数不同，以当次命令输出为准，并改写本节。
