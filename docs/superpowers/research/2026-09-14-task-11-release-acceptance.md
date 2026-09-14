# Task 11 日期化发布验收：四工作流端到端与首位运营用户交付

日期：2026-09-14。基线：`main`，实现前 HEAD `8680182`（Task 10 库存预警）；本文件提交时主线另有
`22bca35` / `4f3b072` 两份后置子项目设计文档，未被本轮改动。
口径依据：[首位运营用户的跨平台工作流设计](../specs/2026-09-11-operator-workflows-design.md) §10、
[多来源指标与口径契约](../specs/2026-09-12-multi-source-metrics-design.md) §3–§6、
[实施计划 Task 11](../plans/2026-09-11-data-and-query-closure.md)、
[Task 11 后置子项目总设计 §4 发布门禁](../specs/2026-09-14-post-task11-subprojects-design.md)、
[拼多多范围决定](2026-09-12-drop-pdd-onboarding.md)。

## 0. 一句话结论

**四工作流的产品内契约已在合成数据与独立 `*_test` 库上端到端验收通过（26/26 离线、11 个运营场景
全部成为可执行用例）；Task 11 的七项发布门禁中第 1–3 项本轮满足，第 4–7 项未执行，因此
"可以发布给首位运营用户试用"这一结论本轮不成立。**

缺的不是代码路径，而是外部证据：真实 provider、真实来源逐店取证、生产环境迁移/备份恢复、
一周试用。这些都需要凭证与生产访问，本轮任务边界明确禁止触碰，一律记"未执行"，不记为通过。

## 1. 本轮交付物

| 文件 | 内容 |
| --- | --- |
| `backend/tests/test_operator_workflows.py` | 68 项：spec §10 的 11 个场景 + 四领域各自的正常 / 部分来源 / 无权限 / 持久化失败路径 + 主层路由、调用预算、图表-表格相等、多来源守护、26 题合同 + 库用例 DSN gate 的两项守护（§13） |
| `backend/tests/questions.jsonl` | 20 题 → 26 题：修订 Q08 / Q15，新增 Q21–Q26；结构化字段扩到 `codes`、`basis`、`diagnostics`、`coverage`、`no_values` / `no_rows` / `no_total`、`calls`、`allowed`、`seeds` |
| `backend/tests/acceptance.py` | 多来源合成基准 `seed_multi_source_case`（TB1 出库、PDD1 单据、FX1 关闭已付款单）、`apply_coverage_holes`、逐题 savepoint 与独立授权集、结构化比对、26 题汇总 |
| `backend/bi_agent/agent.py` | Q08 修订所需的窄改动：「销售额」澄清改为按授权店铺档案 + 来源注册表派生口径候选，并同时问期间（详见 §7） |
| `backend/bi_agent/data_quality.py` | 把「授权店铺平台档案」读取升为单一公开常量 `SHOP_PLATFORMS_SQL`，供覆盖门禁与口径澄清共用一份拼写 |
| `backend/tests/test_core.py`、`backend/tests/fakeconn.py` | 澄清路径的聚焦用例（单平台 / 混合平台 / 拼多多 / 未登记平台 / 空授权）与替身补一条两列读取分支 |
| 本文 | 日期化验收记录 + 未执行清单 |

未改动：Tasks 7–10 的实现与其测试、任何迁移 SQL、`frontend/`、真实库与同步入口。

## 2. 命令与结果（本机 Windows，`backend/` 与 `frontend/`）

```text
uv run --env-file ../.env.test python -m unittest tests.test_channel_mapping \
  tests.test_commerce tests.test_comparison tests.test_listing_audit \
  tests.test_inventory tests.test_operator_workflows
→ 387 tests OK（13 / 43 / 33 / 113 / 117 / 68）

uv run --env-file ../.env.test python -m unittest tests.test_core tests.test_runtime \
  tests.test_business_query_graph tests.test_catalog tests.test_data_quality \
  tests.test_recovery tests.test_multi_source_metrics
→ 360 tests OK

uv run --env-file ../.env.test python -m unittest tests.test_db tests.test_api tests.test_runtime_db
→ 124 tests OK（DB-enabled，非整组 skip）

uv run --env-file ../.env.test python -m unittest discover -s tests -t .
→ 871 tests OK，0 failure / 0 error / 0 skip

# 无 DSN 基线（本轮补记：26/26 需要真的测试库，没库时只能拿到非库用例）
python -m unittest discover -s tests -t .              # 环境里没有 BI_TEST_ADMIN_DSN
→ Ran 871 tests OK (skipped=382)
python -m unittest tests.test_channel_mapping … tests.test_operator_workflows   # 计划首条命令，不传 --env-file
→ Ran 387 tests OK (skipped=200)     ← 补 gate 之前是 FAILED (errors=11, skipped=44)
python -m tests.acceptance --offline                    # 无 DSN
→ {"mode":"offline","result":"skipped","reason":"未配置BI_TEST_ADMIN_DSN"}，exit=2

uv run --env-file ../.env.test python -m tests.acceptance --offline
→ {"mode":"offline","total":26,"passed":26,"failures":0,"result":"pass"}

cd ../frontend && npm test   → 8 files / 82 tests passed
cd ../frontend && npm run build → tsc -b && vite build 成功（38 modules）
```

红线自证（避免"恒真断言"）：把 Q21 的分列数值改成反序、Q24 的缺口段改错、Q25 的
`matched_cohort_only` 抹掉、Q22 的口径凭证换成支付口径、Q08 的澄清词改成不存在的词、
Q23 的未匹配条数改成 9，runner 六项全部转红并逐条报出期望与实际；恢复后 26/26。
该自检脚本为一次性排查工具，未进仓库（把它固定成可执行回归已记进 §11.7）。

两个跑法约束（其中一个已归因并修掉）：

1. 这些套件共用 `bi_agent_test`（外层事务回滚，但种子行、advisory 锁与共享表仍是全局态），
   所以请**逐条顺序跑**：有一次两个 DB 套件并发时其中一份多一个错误，串行重跑均干净；
   该现象未能稳定复现，因此只作约束不作归因。本文引用的数字全部来自串行重跑。
2. **不得拿整份载荷扫数字子串当护栏**。本报告初稿 §11.5 把一次假红归给了“并发”，
   实测不成立：真正的错因是 `assertNotIn("300", json.dumps(payload))` 这类写法——
   `"300"` / `"900"` / `"110"` 会撞进 Artifact 的随机句柄（UUID 与 `ent-` / `pl-` / `wh-`
   的 8–12 位十六进制，如 `…-a1ba-3005068a03b6`），所以它是随机红，而且本来就没证明
   去重。`test_operator_workflows.py` 里三处已在第一轮改成按值断言；同一命令里还剩两处
   同形写法（`test_comparison.py` 扫 `"900"`、`test_inventory.py` 扫 `"110"`），第二轮全部
   改成按值断言（见 §13）。

## 3. spec §10 的 11 个验收场景

| # | 场景 | 落到的用例 | 关键断言 |
| --- | --- | --- | --- |
| 1 | A 商品两种 SKU 跨三平台五店 | `OperatorProductWorkflowTests.test_product_report_tracks_two_skus_and_keeps_incomparable_shops_apart` | `resolved_product` 带 product/sku 引用与映射版本；逐店均价 100/50/100 各自保留且 ERP 主键不外泄；跨口径合计发 null、`metric_statuses` 全部 `incomparable/basis_incompatible`、逐店份额也发 None；七日趋势七天齐全；淘系与拼多多逐家 `coverage_time_basis_unverified`；总状态 partial。（已评估集合内部的排名属比较域，由下面第 3 行与 §5 的用例钉）|
| 2 | 1 件 / 100 元 + 9 件 / 450 元 | `test_two_shop_weighted_price_is_not_the_average_of_shop_averages` | 总均价 55；逐店均价按值钉为 100/50，列表里不出现 75；份额 0.181818/0.818182（按金额不按件）；商品毛利参考 240 / 收入 550 / 件数 10 |
| 3 | 三平台中一平台无完整支付来源 | `OperatorComparisonWorkflowTests.test_platform_without_payment_source_stays_an_explicit_missing_group`、`OperatorMultiSourceGuardrailTests.test_taoxi_outstock_is_excluded_from_a_platform_payment_total_not_zeroed`、`test_certified_and_unverified_payment_windows_share_no_total_or_ranking` | 缺失组不发布也不给 0；合计与排名只覆盖已评估分组（`ranking_scope=evaluated_only`，缺失平台不入名次）；同为支付口径但认证状态不同时名次全发 null；`excluded_scope` 逐家带原因；`evaluated_scope.platforms` 与请求平台一比即知缺谁 |
| 4 | 新上架 SKU 没有成交 | `OperatorListingWorkflowTests.test_new_sku_without_any_sale_still_enters_the_audit`、`OperatorInventoryWorkflowTests.test_sku_without_any_sale_still_enters_the_inventory_check` | 无成交但有标识映射 → roster 仍含该格并判 `not_listed`；库存两个口径各出一行；分母仍是期望项 |
| 5 | 目标 5 店只采到 4 店 | `test_missing_snapshot_for_one_shop_blocks_the_all_correct_claim`、`test_complete_enumeration_without_the_item_is_not_listed_not_unknown` | `expected_items=5 / evaluated_items=4 / counts.unknown=1 / all_correct=false`；有全量枚举凭据时才允许 `not_listed`；缺快照与缺来源两种原因不互代 |
| 6 | 两 SKU 标准价不同、同 SKU 两链接 | `test_per_sku_targets_and_multiple_listings_keep_every_mismatch`、`test_conflicting_targets_for_one_sku_ask_instead_of_choosing` | 三格逐链接出行；两条不匹配各留 10 / 20 差额；规则冲突 → needs_input 且期望 roster 不落库 |
| 7 | 三店共享仓库 100 件各显示 100 | `test_shared_pool_is_counted_once_and_never_summed_per_shop` | 实物一行 100、`batch_count=1`、池与仓库句柄可见且只指向该共享池；渠道三行各 100；“没有 300”按**数量格的值**逐项断言，不拿载荷扫子串（那会撞 UUID，见 §13）|
| 8 | 店铺 0、仓库 100，两类阈值都配 | `test_shop_zero_with_ample_shared_stock_is_a_quota_action_not_a_purchase` | 只有 `quota_adjust` 候选；无 `replenish`；披露文案只说配额缺口 |
| 9 | 价格 / 库存快照过期 | `test_stale_snapshot_is_reported_stale_not_correct`、`test_stale_inventory_snapshot_is_stale_not_safe` | 状态 `stale` / 采集价不以数字出现；`evaluated_items=0` 而分母保持；`all_correct=false`、`all_safe=false`；读取时刻被披露 |
| 10 | 缺历史成本或费用 | `test_missing_cost_nulls_profit_but_keeps_quantity_and_sales` | 商品毛利参考 null；销量 3、金额 300 照发；总体仍可 ok |
| 11 | 测试用户只能看 2 店 | `test_product_workflow_never_sees_shops_outside_the_authorized_set`、`test_unauthorized_pool_is_excluded_without_leaking_its_contents` + 各域 forbidden 用例 | `all_authorized` 也只展开授权集；未授权店的 ERP 主键不出现在任何载荷；未获准池只以 `pool_not_authorized` 句柄出现，数量列为 null |

## 4. 四领域 × 四类路径

| 领域 | 正常 | 部分来源 | 无权限 | 持久化失败 |
| --- | --- | --- | --- | --- |
| 商品运营 `analyze_product_performance` | 场景 1/2 + `test_product_workflow_publishes_persisted_datasets_with_lineage`（metric_result + trend_series、血缘含 `source_batches`、图版本、指纹） | `test_product_report_with_one_source_missing_is_partial_not_silently_complete`（`capability_ungranted`）、`test_product_workflow_refuses_unverified_time_basis_without_zeroing_the_shop` | `test_product_workflow_forbids_an_out_of_scope_shop_ref`：failed + `forbidden`，载荷只有 `{"status":"failed"}`，不发 Artifact | `test_product_workflow_persistence_failure_is_not_reported_as_success` + `test_agent_turn_reports_persistence_failure_instead_of_a_partial_answer`（主层 `error_code`，卡片列表清空，正文不残留"查到了"） |
| 平台 / 店铺比较 `compare_performance` | 场景 3 + `test_five_platform_chart_and_table_agree_row_by_row` | `test_group_with_a_coverage_gap_publishes_no_group_number`（整组不发 + 下钻仍逐店归因） | `test_comparison_forbids_a_shop_outside_the_authorized_set` | 必需数据集失败 → failed；只 `chart_spec` 失败 → 表格 + `trend_series` 保留并披露"只发表格" |
| 上架复核 `audit_listing_prices` | `test_price_audit_normal_path_publishes_a_complete_difference_table`（2/2/2、`all_correct=true`、`rule_version`、`policy_version=来源注册表版本`、快照批次进血缘） | `test_unverified_source_for_one_platform_marks_cells_unsupported`（tb 成 / fxg 未取证 → unsupported 一格，partial）、`test_without_any_verified_source_the_audit_reports_unsupported_only`（交付态：0 项判定、missing_data） | `test_forbidden_scope_reveals_nothing_about_the_other_shop` | `test_audit_persistence_failure_is_not_reported_as_a_finished_review`；另加 `test_missing_this_round_target_price_leaves_no_inherited_standard`（真库 Store：本轮无价 → `price_audit_expectations` 零行，上一轮那行仍属旧 run_id） |
| 库存预警 `inspect_inventory` | 场景 7/8 + `test_all_safe_needs_every_cell_judged_normal_with_complete_evidence` | `test_only_one_level_verified_leaves_the_other_level_unsupported`（只登记渠道 → 实物 unsupported、`levels` 仍两档）、`test_without_any_verified_source_inventory_reports_unsupported_only` | `test_inventory_forbids_a_shop_outside_the_authorized_set` + 池授权用例（店铺授权不推导池授权） | `test_inventory_alert_persistence_failure_is_not_reported_as_safe` |

## 5. 主层路由与调用预算（脚本替身，不含真实模型）

| 提问 | 结果 |
| --- | --- |
| 「直钉枪在所有店铺近七天卖得怎么样？」 | 一次 `analyze_product_performance` → metric_result + trend_series 两张卡片；模型侧只见引用，展示侧带真名 |
| 「五个平台近七天哪个卖得好？」 | 一次 `compare_performance(group_by=platform)` → comparison_table + chart_spec，`chart.dataset_ref == 表格 artifact_id` |
| 「比较一下抖音各店铺的支付金额」 | 一次 `compare_performance(group_by=shop, scope.platforms=["fxg"])`；另一平台的店完全不进结果 |
| 「直钉枪 6mm 在获准店铺的标价是 19.90，都对不对？」 | 一次 `audit_listing_prices`，目标价取自本轮；下一句不给价 → `needs_input`、不发差异表、会话 filters 无回写 |
| 「看一下全商品总库存和店铺预警」 | 一次 `inspect_inventory`；两级各自成行，主层无池授权 → 实物不判、渠道可判，`all_safe=false` |
| 店数 2 → 7 | 业务 Tool 调用仍 1 次，事实取数 SQL 条数不变（`shop_id = ANY(%s)` 一条集合查询），卡片数不变 |
| 预算耗尽（30 秒收紧到 0） | 文本明说预算、无卡片、无数字；不改窗口、不把 partial 演成 ok |

## 6. 修订后的 26 题离线验收

`python -m tests.acceptance --offline` → **26/26 pass**。01–07、09–14、16–20 的数值与安全边界沿用；
结构化断言扩到原因码、逐店逐指标口径凭证、退款诊断与覆盖形状（原因码 / 覆盖 / 诊断 / 口径缺失会被判失败）。

新增六题钉的是多来源护栏：

| 题 | 断言要点 |
| --- | --- |
| Q21 | 混口径 `paid_amount` 先被 `basis_incompatible` 拒（strict），分列仍因淘系付款时间口径不成立而不发数；ERP 单据数走 `basis_policy=separate` 得到 S1=6（pay_time）与 TB1=1（outstock_time），无跨店合计行 |
| Q22 | 拼多多支付金额 / 支付订单数 `capability_unavailable` 且不跑金额 SQL；追问 ERP 单据数得 3，口径为 `erp_document/v1 / outstock_time` |
| Q23 | TB1 `refund_amount=50` 可答并披露未匹配 1/2、20 元、50%；同请求里的 `cash_difference` + `cohort_refund_rate` 因支付窗口未认证整体拒答 |
| Q24 | 公共覆盖按 (店 × 来源 × 实体) 取交集：缺口恰为 09-01~09-03 与 09-05~09-06，原窗口 09-01~09-08 不被缩短，不给部分汇总 |
| Q25 | 关闭已付款单：支付 100 / 退款 30 / 差额 70（窗口取在未匹配那条之前），商品维度零行（销量不回活），差额以 `revenue_not_attributed` 披露；整店同批率 30% 且必须标 `matched_cohort_only` |
| Q26 | 「接口扫描成功」不等于支付窗口完整：`coverage_time_basis_unverified`，无数值 |

**刻意的取证映射（不要读成"淘系已可答支付"）**：原路线图表把 Q21/Q23/Q25 的示例数字挂在 TB1
上，但 Task 5.2c 已把出库通道的付款时间口径实测判为 `disproved`，Q26 钉的正是同一条规则。
经主 Agent 确认后本轮按以下方式落地：混口径分列的可执行证据用两家都能答的 `erp_documents`；
未匹配退款披露留在 TB1 的 `refund_amount`；`100/30/70`、销量不回活与 `matched_cohort_only`
放到具备合成时间口径认证的 FX1。题内 `allowed` 字段写明各题授权集，`seeds` 写明覆盖替换，
避免用一题的窗口冒充另一题。

真实模型侧：`.env.test` 里是 `LLM_PROVIDER=deepseek / LLM_MODEL=demo-model / DEEPSEEK_API_KEY=fake-test-key`
的桩配置，**不构成真实模型证据**。`--provider-smoke` 与 `--live` 本轮未执行（见 §9）。

## 7. Q08 澄清的窄改动（唯一的产品代码变更）

`answer()` 的「销售额」澄清原来固定回答"支付还是出库"，既不问期间也不说这些店实际拿得到什么口径，
无法满足修订版 Q08。现在按 `SHOP_PLATFORMS_SQL` 读到的授权店铺档案 + `sources.registration()` 派生
候选：`certified` 说"付款时间窗口已认证"、`unmeasured` 说"未逐店对照，只能作为可观测样本"、
`disproved` 说"实测不成立，不能按完整支付窗口出数"，出库通道额外明写"ERP 销售出库口径，非平台账单 GMV"，
拼多多说"无支付口径能力（只有 erp_document/v1 单据口径）"，未登记平台说"来源未登记，没有可用口径
（不回退到其它通道）"。仍然：不调用模型、不执行查询、不输出真实店号、不替用户选口径、不把"已登记"
说成"已就绪"。范围之外的澄清路径未改。

## 8. 逐平台 / 来源就绪清单

原则同 `docs/metrics.md`：没有实测的接口不填"可用"。本轮不触任何真实接口、凭证与生产库，
因此真实证据列全部是"未执行"；下表"代码状态"只描述确定性契约是否可发布，不是来源就绪。

| 平台 | 账号范围 | 覆盖期 | 成本质量 | 上架实际价时点 | 库存批次 | 匹配覆盖 | 代码状态 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 抖音 `fxg` | 未执行（需 `.env.sync` 授权清单） | 未执行 | 未执行（行成本覆盖率需逐店核） | 未执行 | 未执行 | 未执行 | 支付族能力按逐源证据开通；本卷用合成认证店演示 |
| 京东 `jd`、快手 `kuaishou`、视频号 `wxsph`、微购相册 `wsxc` | 未执行 | 未执行 | 未执行 | 未执行 | 未执行 | 未执行 | 同交易通道、`unmeasured`：出数必披露为可观测样本 |
| 淘宝 `tb`、天猫 `tm` | 未执行 | 未执行（历史：交易接口回"非法店铺编码"） | 未执行 | 未执行 | 未执行 | 未执行 | 出库口径；支付窗口类指标 fail closed |
| 拼多多 `pdd` | 不接入（2026-09-12 决定） | — | — | — | — | — | 仅 `erp_documents`；支付族永久解析不通 |
| 渠道在售价（全部平台） | 未执行 | — | — | **无任何已核验来源** | — | 未执行 | `listing_audit` 来源注册表默认为空 → 真实部署只能报 `unsupported` |
| 实物 / 渠道库存（全部平台） | 未执行 | — | — | — | **无任何已核验来源** | 未执行 | `inventory` 来源注册表默认为空 → 真实部署只能报 `unsupported` |
| 广告实耗与归因 | 未执行 | — | — | — | — | — | 无来源；ROAS / 净利润一律不可主张 |

本轮四工作流"正常路径"依赖的，是测试进程内 `register_listing_source` / `register_inventory_source`
登记的**合成**来源（跑完即 reset）。真实功能开关必须等字段、授权、时效、分页完整性与对账证据齐备。

## 9. 未执行的外部验证（逐条，均为"未执行"而非"通过"）

1. 真实 provider 的 `--provider-smoke` 与 `--live` 26 题：未执行（本轮无模型凭证，也不得调用真实模型）。
   真实模型的版本、调用次数与错误分类因此无法给出；旧 20 题 14/20 与真实模型 26 题结果都仍是历史/未知。
2. 真实来源接口（快麦各通道）、历史回填、增量与 `reconcile` / `capabilities --apply`：未执行。
3. 生产库与预发库的迁移核对（001→019）、`bi_app` / `bi_reader` 生产角色实测：未执行（仅本机 `bi_agent_test`）。
4. 部署、反向代理 + OIDC 身份边界、Origin、SSE 断线恢复、一小时同步与每日重核任务注册：未执行。
5. 备份与一次真实恢复检查（含恢复前后摘要对比）：未执行。
6. 首位运营用户一店一周试用（问题、缺口、模型错误与处理决定归档）：未执行。
7. 逐店与后台账单 / 业务日期对照（解除 `unmeasured` / 建逐店 time_basis 登记）：未执行。
8. 渠道在售价与库存的真实来源取证（平台授权 API 或带时点与完整性声明的官方导出）：未执行。
9. 发布前旧契约回归（`query_business`、`evaluate_promotion`、聊天 SSE）的**线上**验证：本轮只跑了离线套件
   （`tests.test_business_query_graph`、`tests.test_core`、`tests.test_api`），生产链路未执行。

## 10. 拼多多永久口径的落点（不可回退的三条）

1. 注册表无 pdd 支付分支、上限只含 `erp_documents`；误写进库的支付标签不开任何能力。
2. 本轮未新增任何 pdd 连接器、来源登记、凭证入口或 onboarding 步骤；`bi_agent/config.py` 无 pdd 配置项，
   来源注册表里也没有臆造的接口方法名（用例 `test_no_pdd_connector_credential_or_onboarding_path_exists` 钉住）。
3. 对外只出现 `excluded_scope` / `unsupported` / `capability_unavailable` 与"无支付口径能力"，
   不出现"待授权 / 等待方舟 / 延后"一类把已关闭决策重新写成待办的措辞（Q22、比较域用例与本表共同钉住）。

## 11. 残余风险

1. **库存池授权没有服务端写入方**：`inspect_inventory` 走主层时 `allowed_inventory_pool_ids` 只能是空集，
   因此真实聊天路径当前只能给店铺可售预警，实物那一格恒为未判定。域内用例能证明去重、阈值与池授权
   逻辑正确，但"运营用户实际看到实物总量"这件事需要一条服务端池授权配置来源（不在 Task 11 范围内，
   属首位用户试用前的产品决定）。
2. **逐店 / 逐平台真实就绪为零**：所有工作流的"能用"目前只到"契约正确 + 合成数据端到端"。
   §8 的每一格都要真实取证才能改状态。
3. **Q08 澄清是措辞级护栏**，不是硬门禁：模型仍可能把上一轮目标价重复发给工具（服务端不持久化目标价，
   也不会替它补一个），真实模型路由准确率必须由 §9.1 的 live 验收覆盖。
4. **同店换源的历史一致性**未做逐店登记：本轮只证明了指纹与血缘会因来源/批次变化而失效，
   旧数据行仍留在 `bi.sync_state`（Task 5.3 台账事项）。
5. `tests/test_multi_source_metrics.py::BasisContractDatabaseTests` 里有三个方名重复定义
   （`test_projection_keeps_basis_but_strips_identifiers_and_channels`、
   `test_basis_carrying_an_erp_key_is_rejected`、`test_deterministic_summary_states_the_basis`），
   Python 按后定义者生效，前一份永不执行（实际只跑 3 份而不是 6 份）。属 Task 5 遗留，
   本轮不修正已交付测试的归属，只把它记入待清理项。

6. **库存的来源门禁版本不在指纹里**（独立评审提出，本轮只改文字不改代码）：价审把 `policy_version`
   写成渠道来源注册表版本（`listing_audit/graph.py:911`），而库存把它写成单位换算表版本
   （`inventory/graph.py:1136`），`INVENTORY_SOURCE_REGISTRY_VERSION` 既不进血缘也不参与指纹。
   `runtime/artifacts.py` 的 `fingerprint_parts()` 只散列 `policy_version` / `source_registry_version` /
   `source_batches` / `data_as_of`，所以**只改登记（收紧时效上限、撤销某平台取证）而快照批次不动时
   指纹不变**，`find_reusable_run` 仍会命中旧结果。本报告初稿拿"换源会改 `source_batches`"为它开过
   脱——那句只对快照变化成立，对登记变化不成立，现在改回来。今天仍不可达（`find_reusable_run` 除
   测试外无生产调用方），修法是 `_provenance_of` 补一个 `source_registry_version=INVENTORY_SOURCE_REGISTRY_VERSION`
   关键字，属下一个 diff。
7. **反恒真自检未固定成回归**：§2 那六条"把期望改错应当转红"的检核本轮是手工跑的，仓库里没有
   任何保证 `_structured_problems` 继续强制 basis / codes / coverage / diagnostics 的用例。应补一条
   突变回归（拿一份故意错的 Q21 basis / Q24 gaps 期望去跑 `_verify_turn`，断言它返回问题）。
8. **无 DSN 基线已补记**（§2）：没库时全量 discover 只剩 489 个非库用例（`OK (skipped=382)`），
   `--offline` 直接 exit=2 报 skipped；所以"26/26"不能读成"任何机器上都能跑"。

## 13. 独立评审后的修正轮（2026-09-14；只修 P0/P1）

本节记两轮修正（两轮都只修复审给出的 P0/P1，P2 逐条只归档不改代码）：

- 第一轮的唯一 P1：`test_operator_workflows.py` 里 DSN gate 漏了一个库用例类（其余五个库类与
  Task 7–10 的全部库类都带同一个装饰器）。先写回归看红、再修复看绿。
- 第二轮的两个 P1（§13.1）：同一个验收命令里还残留的两处“扫整份载荷里的数字子串”护栏。

| 项 | 内容 |
| --- | --- |
| 缺陷 | `OperatorProductWorkflowTests` 是唯一没带 `@unittest.skipUnless(os.getenv("BI_TEST_ADMIN_DSN"), …)` 的库用例类；`OperatorFixture.setUp` 走 `dbfixtures.connect_test_db`，那里第一句就读 `os.environ["BI_TEST_ADMIN_DSN"]` |
| 实测红 | 无 DSN 跑单文件：`Ran 66 tests … FAILED (errors=11, skipped=44)`（那时本文件还是 66 项）。计划里第一条验收命令不传 `--env-file`，所以在任何没配库的环境上都是红 |
| 回归（先写，先红） | 新增 `OperatorTestDatabaseGatingTests` 两项：① AST 结构守护——所有 `OperatorFixture` 子类必须带 `BI_TEST_ADMIN_DSN` gate；② 行为守护——拿掉 DSN 重跑库用例子进程，要求无 `ERROR:` / `Traceback`、有 `skipped=`、退出码 0。修复前两项均红（① 报出 `['OperatorProductWorkflowTests']`，② 子进程 `errors=11`） |
| 修复 | 只补那一行装饰器（`os` / `unittest` 本已导入） |
| 实测绿 | 无 DSN：单文件 `OK (skipped=55)`、六库集合 `OK (skipped=200)`、全量 discover `OK (skipped=382)`，全部零 error；带 DSN：本文件 68 项 OK、六库集合 387 OK、全量 871 OK、离线 26/26 |
| 重跑时抓出的假断言（同一个验收文件，不修就不能交） | ①三处“扫整份载荷里的数字子串”当护栏（`"300"` / `"900"` / `"75"`）会撞进 Artifact 的 UUID，造成随机红与假绿（本报告初稿将其中一次误归为“并发”，§2 已改）——现已改成按值断言：逐行比 `quantity` / `channel_quantity` / `weighted_avg_paid_price` / `sales_share`，并补上共享池句柄断言；②`assertNotIn("ranking", payload)` 在商品报告上是恒真断言（实测：商品报告 `ranking` 恒为 `null`），已换成真护栏（合计 null + `metric_statuses` 全 `incomparable/basis_incompatible` + 逐店份额也为 None），跳店排名的护栏留在比较域用例 |
| 未改（P2，留给下一个 diff） | 库存血缘补 `source_registry_version`（§11.6）；Q08 澄清尾句不再写死“买家已支付 / ERP 出库”而是按候选拼（§11.3）；`test_no_question_claims_live_or_provider_readiness` 按 dict 顶层键判定的假断言；报告存在用例里 `"26"` 会被日期满中；注释里的 `[26:51]` 残字；把 §2 突变自证固定成回归（§11.7）；`BasisContractDatabaseTests` 三个重名方法（§11.5） |
| 一个没关掉的事实（本轮已关） | 修完之后六库集合出现过**一次** `FAILED (failures=1)`，当时没留下方名；此后同一命令串行重跑 18 次全绿。本轮复审把错因定住了：不是“并发”，也不是已删的那三处——同一命令里还留着两处同形的扫子串断言（`test_comparison.py` 扫 `"900"`、`test_inventory.py` 扫 `"110"`），它们同样会撞进随机句柄，详见下方“第二轮” |

### 13.1 第二轮：同一命令里残留的两处扫子串护栏（复审给出的两条 P1）

第一轮只改掉了 `test_operator_workflows.py` 自己那三处，但错因机制并没有从这个验收命令里清完：
`test_comparison.py` 与 `test_inventory.py` 各留了一处同形写法，拿整个序列化载荷扫一个数字子串。
本轮生成的行里带着 `ent-` / `pl-` / `wh-` 随机句柄（8–12 位十六进制，源头是 fixture 的
`uuid4().hex[:5]`），所以“扫不到”与“扫到了”都不说明被测行为。

| 项 | 内容 |
| --- | --- |
| P1-① 缺陷 | `tests/test_comparison.py` 的 `test_pdd_stays_an_explicit_missing_group_without_any_payment_number` 用 `assertNotIn("900", json.dumps(payload, …))` 当“拼多多被排除”的护栏 |
| P1-① 机制 | 它扫的是 model_payload 本身。实测（`_p1_measure` 量测脚本，已删）：一份 model_payload 的文本里出现 5 次 `ent-` 句柄（每轮 2 个不同的 8 位十六进制句柄，来自 `excluded_scope` / `evaluated_scope` / `basis`），抽 20 万次得到单次运行撞中 `"900"` 的机率为 **0.413%**；而它从来没直接证过“900 没被补成支付销售额”（合计算成 1000 时它也看不见） |
| P1-① 反向核查（假绿已实测） | 拿一个临时突变把被排除分组的出库 900 混进合计行（只改 `_total_value`，跑完 `git checkout` 还原）：旧写法仍判通过（model_payload 里找不到 `"900"`），新的合计行断言报 `['1000'] != ['100']` —— 旧断言是个假绿护栏 |
| P1-① 修复 | 按值钉本轮真发出去的数：分组行只有 `[("jd", "100")]`；不带分组键的合计行 `sales_amount` 是 `"100"`（把出库 900 顶进来就是 1000）；再把 `comparison_table` 与 `trend_series` 两份数据集里实际出现的 `sales_amount` 列成值列表，要求其中没有 `"900"` |
| P1-② 缺陷 | `tests/test_inventory.py` 的 `test_two_channel_scans_of_one_shop_report_the_current_one_only` 用 `assertNotIn("110", str(payload["data"]))` 防“把两次抓取相加” |
| P1-② 机制 | 同一份 data 里两个扫描行的量只可能是 `"10"` 与 `"1000"`，`"110"` 本来就不会作为数出现；它能扫到的只有行上的四个随机句柄（每轮 2 个 `ent-` 8 位 + 1 个 `pl-` 12 位 + 1 个 `wh-` 12 位；实测例 `pl-81a9b9169236` / `wh-66a14aaec6e7`），抽 20 万次得到单次运行撞中 `"110"` 的机率为 **0.778%** |
| P1-② 修复 | 逐行比对 `(level, quantity, channel_quantity)`：只发 `physical_total = ("1000", None)` 与 `shop_sellable = (None, "10")`——旧一次抓的 100 既不参与相加，也不顶替当前值 |
| P1-② 反向核查 | 把期望里的渠道数改成 `"110"` 后，该断言报 `AssertionError: Lists differ: … ('shop_sellable', None, '10')] != … ('shop_sellable', None, '110')]`：它确实在比发出去的数。旧写法的扫描面 `str(payload["data"])` 里带 `sku_ref` / `shop_ref` / `pool_ref` / `warehouse_ref` 四个句柄（实测 `pl-81a9b9169236` / `wh-66a14aaec6e7`），所以它同样只可能在句柄上随机红 |
| 先红后绿 | ①把期望值改错（分组行写成 `("jd", "900")`、合计写成 `["1000"]`、渠道数写成 `"110"`）后两条用例均报 `AssertionError: Lists differ`，说明新断言确实绑住了值；②恢复后两条用例逐个跑绿，本文件与六库集合重跑均 OK；③旧写法在位时该集合出现过一次 `FAILED (failures=1)`（上一行表已记，当时未留方名），机制与 P1-① 反向核查均已完成 |
| 总数不变 | 本轮只改两条断言，不新增/删除用例：`test_operator_workflows.py` **68** 项、Task 7–11 六库集合 **387** 项（13 / 43 / 33 / 113 / 117 / 68）、全量 discover **871** 项（零 failure / error / skip）、离线 **26/26** |
| 随机红的量级 | 两条旧断言各自每轮撞中率实测为 0.413% 与 0.778%，同一验收命令一次就都跑一遍 ⇒ 每 ~100 次串行重跑出现一次与行为无关的红。上一行表里“出现过一次 `FAILED (failures=1)`、18 次重跑全绿”与此完全一致，“共用库并发”归因作废（§2、§11.5 已改） |
| 未变的护栏强度 | 除两条 P1 外本轮零改动：拼多多仍只会作为 `excluded_scope` 出现（它的 reason 断言原样保留），当前可售仍只能取后一次抓取；没有放开任何时间口径 / 能力门禁 / 授权边界 |
| 同类写法在本命令之外仍有三处 | `test_api.py:363`（扫页面文本里的 `"1000"`）、`test_business_query_graph.py:677`（扫 `serialized`）、`test_catalog.py:332`（扫 `model_json` 里的 ERP 主键）属 Tasks 1–6，且都不在这条验收命令的扫描面上（它们扫的是渲染文本 / 主键泄露，不是随机句柄），本轮按“只修两条 P1”的边界不动 |

本轮没碰：迁移、`frontend/`、Tasks 7–10 的实现（只改了 Task 8 / Task 10 测试里的两条断言）、
真实库 / 真实接口 / 真实模型 / 部署，以及受保护的未跟踪文件；拼多多边界不变（依旧只有
`unsupported` / `excluded_scope` / 能力解析为空，无连接器、无凭证入口、无 onboarding）。

## 12. 建议的下一步（顺序即依赖）

1. 按 §9.1 用一份真实 provider 配置跑 `--provider-smoke` 与 `--live` 26 题，逐题记录版本 / 调用数 / 错误分类。
2. 决定库存池授权的来源（配置还是运营侧登记），再补 §11.1 的端到端。
3. 逐能力发布：先开已对账平台的商品运营与平台比较，价审与库存保持 `unsupported`，
   并在 `docs/metrics.md` §6 表格里按平台记录取证结果。
4. 完成 §9.3–§9.6 的部署、恢复与一周试用，才把本文件升级为"发布门禁通过"。
