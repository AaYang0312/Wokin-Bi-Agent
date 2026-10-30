# 2026-10-08 经营者实测：可修复项

日期：2026-10-08。来源：本机 `http://127.0.0.1:5175` 真实模型对话（DeepSeek），不是离线桩模型验收。API 实际连接库为 `bi_agent_test`（`.env.app` 的 `BI_APP_DSN`）。本轮曾误把迁移 018–032 应用到同实例的 `bi_agent`；应用读写发生在 `bi_agent_test`。

关联：

- 生意参谋导入方案：[2026-10-02-manual-sycm-export-import.md](../plans/2026-10-02-manual-sycm-export-import.md)
- 写面迁移：`backend/sql/031_sycm_upload_write_path.sql`
- 授权断言：`backend/tests/test_sycm_upload.py` 的 `APP_TABLE_GRANTS`
- 出库对比开通：[runbook 出库口径](../../runbook.md)、[S1 验收](2026-09-25-s1-dual-caliber-platform-comparison-acceptance.md)
- 上一轮离线复测：[2026-10-02-shop-manager-workflow-retest.md](2026-10-02-shop-manager-workflow-retest.md)

## 1. 判定标准

一项算可修复，只当同时满足：

1. 有明确的代码、版本化迁移，或 runbook 已写明的运维路径；
2. 不把缺数、未认证、未取证写成 0 或「全部正确」；
3. 不混用出库 / 支付 / 生意参谋口径，不把淘宝与天猫并成未批准的「淘系」；
4. 不接入拼多多支付，不新增 PDD 连接器或能力。

下面只列通过该判定的项。第 4 节列出本轮看过、判定为不修的现象，避免以后重复开任务。

## 2. 建议按代码修复

### F1. 生意参谋上传成功路径缺 `SELECT`（P0）

**现象。** 门禁 `SYCM_MANUAL_IMPORT_ENABLED=true` 时，`docs/手动导出表格/` 里「淘店-沃金五金机电商城-店铺维度」xlsx 走 `POST /api/uploads/sycm`，响应 503，码 `sycm_upload_write_path_unavailable`。以 `bi_app` 直接执行 `import_sycm_file` 得到 `permission denied for table sycm_shop_daily`。当时 `has_table_privilege(..., 'INSERT'/'UPDATE')` 为真、`SELECT` 为假。补 `GRANT SELECT` 后同一文件 HTTP 200：`status=accepted`，`rows_written=61`，店铺 `166647`。

**原因。** `031` 只授予 `bi_app` 对 `bi.sycm_shop_daily` / `bi.sycm_customer_daily` 的 `INSERT, UPDATE`。导入器对这两张表使用 `INSERT ... ON CONFLICT DO UPDATE`（`sycm_import.py` 的 `_SHOP_FACT_UPSERT_SQL` / `_CUSTOMER_FACT_UPSERT_SQL`）。PostgreSQL 在规划这条语句时就要求目标表 `SELECT`，冲突分支没执行也会拒绝。测试把授权集钉成不含 `SELECT`（`APP_TABLE_GRANTS`），且成功路径没有用 `bi_app` 真正跑过 upsert，所以门禁是绿的、线上写面是 503。

拒绝路径（商品维度、未登记店名）只写台账，不碰事实表，因此那些请求仍是 200 + `rejected`。不能用「拒绝成功」证明写面可用。

**修法。**

- 新增迁移（建议 `033`），不要改已应用过的 `031` 正文来冒充历史：`GRANT SELECT ON bi.sycm_shop_daily, bi.sycm_customer_daily TO bi_app`。不授 `DELETE`。
- 把 `APP_TABLE_GRANTS` 改成包含这两张表的 `SELECT`。
- 加一条用 `bi_app` 执行店铺维度 upsert（含第二次覆盖）的测试；权限不足必须失败，不能由超级用户代写后只断言行数。
- 本机 `bi_agent` 与 `bi_agent_test` 上已有会话内手工 `GRANT SELECT`。该授权不在仓库里，重建库或只重放 `001–032` 会回到 503。以迁移为准，不要把手工 GRANT 当成已交付。

**不在此项里做：** 不放宽文件校验，不开始导入商品维度，不改质量恒为 `unknown` 的披露。

### F2. `compare_performance` 不接受已登记平台的中文名（P1，契约放宽）

**现象。** 问「只看抖音……各店铺对比」时，模型传入 `scope.platforms=["抖音"]`，工具 `needs_input`，且没有平台候选。离线复测 R12 能走到 partial，是因为桩模型传的是平台码 `fxg`。

**原因。** `commerce.models.normalize_platform` 只接受 `sources` 注册表里的平台码；中文一律 `platform_unregistered`。展示层 `PLATFORM_LABELS` 已有 `fxg → 抖音` 等对照，工具参数侧没有这条精确反查。`compare_performance` 的描述写了「选定恰好一个平台」，没有写明必须是 `fxg` / `tb` / `tm` / `jd` / `kuaishou`。

**修法。** 在 `normalize_platform` 入口做 `PLATFORM_LABELS` 的精确反查（「抖音」→ `fxg`，以及表内其余中文名）。反查表之外的中文、别名、未登记码仍然拒绝，不猜。补测试：精确中文名通过；「抖店」「淘系」「拼多多支付」这类不在表内的写法仍拒绝。拼多多码若已在注册表中以 `unsupported` 存在，反查只解析到该码，不因此开通支付。

这是对「中文别名一律拒绝」的有意放宽，只覆盖已经写进 `PLATFORM_LABELS` 的名字。不要在模型提示里让模型自行发明平台码。

## 3. 路径已有，执行前要单独授权或确认

### F3. 出库口径对比与七日趋势：跑已有 `consign-scan`（运维）

**现象。** 2026-09-05 至 09-11 的五平台销量/金额/利润对比整段 `missing_data`（出库覆盖未完成、部分单据缺 `consignTime`），没有 `chart_spec`。同一商品的七日趋势终止于 `coverage_time_basis_unverified`。这与 [S1 验收](2026-09-25-s1-dual-caliber-platform-comparison-acceptance.md) 的「五平台出库排名 NOT MET」一致：代码路径在，扫描没做。

**修法。** 按 runbook「出库口径开通命令」在获准范围内执行 `consign-scan`，再按既有流程做来源质量核对。扫描前所有平台出库对比 fail closed 是预期，不是缺图表组件。

**约束。** 这是真实快麦 API + 写库。未获本轮明确授权不得执行。不把付款时间覆盖借给出库窗口，不把未认证支付平台补成 0。拼多多店铺扫描入口保持跳过。扫描后若仍有缺 `consignTime` 的单据，整窗继续拒绝，这是门禁而不是新缺陷。

支付口径的跨平台图目前只有已认证平台（抖音 `fxg`）能出数。给淘宝/京东/快手补支付认证是另一条取证，不并进这次扫描。

### F4. 三家导出文件的店铺名（数据登记，先确认映射）

**现象。** `docs/手动导出表格/` 12 个文件里，只有店铺名与 `bi.shops.display_name` 精确相等的能入库。实测：

| 导出数据列店铺名 | 结果 |
| --- | --- |
| 沃金五金机电商城 | 接受（`166647`） |
| 宝威德滤纸工厂店 | `shop_not_registered` |
| 元发美特钉枪直销店 | `shop_not_registered` |
| 钉子工厂 | `shop_not_registered` |

商品维度文件返回 `not_imported_this_round`，与方案一致，不是映射问题。

**原因。** 方案已锁定：映射只认数据列「店铺名称」与 `display_name` 精确相等；未映射的元发美特钉枪直销店、钉子工厂本轮整文件拒绝。库内淘系名称是「宝威德滤纸直销 / 宝威德滤纸」等，没有「宝威德滤纸工厂店」，不能把「工厂店」猜成其中一家。

**修法。** 所有者给出「导出店铺名称 → 已有 `shop_id`」的一一对应之后，再增加显式别名（或受控的登记更新）。别名必须精确、可测试、可撤销。禁止按文件名或相似度自动匹配，禁止为了吃进文件去改 ERP 同步写回的 `display_name` 而不留别名来源。在映射确认前，这三家继续整文件拒绝是正确行为。

## 4. 本轮判定为不修

| 现象 | 原因 |
| --- | --- |
| 跨店商品不能汇总排名、多数店毛利为 null | 出库与支付口径 `basis_incompatible`；套件父项成本未核验。null 不是 0。同口径分列是契约。 |
| 上架复核 20 店全 `unsupported` | 渠道在售价来源未取证。不能改成「用成交均价代替标价」。 |
| 库存两级预警 0 项可判 | 来源未取证、无版本化阈值、无渠道可售快照。缺数量保持 unknown。 |
| 拼多多无支付金额 | 永久不接入。只能作为能力边界出现。 |
| 淘宝与天猫不并成淘系 | 没有已批准的版本化合并规则。 |
| 商品维度导出被拒 | 方案明确本轮不导入，等 `channel_items` 映射。 |
| 成交均价不等于上架价 | 口径正确；模型已说明。 |
| 平台对比未画出图 | 图只引用已落库可比数据集。先有 F3 的完整同口径分组，才会有 `chart_spec`。 |
| 单轮工具次数用尽 | `MAX_TOOL_CALLS = 4` 是预算。平台对比第一次已是缺数，加次数不会变出数字。 |
| 浏览器自动化点不开系统文件框 | 测试工具限制。上传路由本身已用同一 xlsx 验证通过。对话里的「上传缺失文件」按钮在越窗推广花费上已出现。 |
| `8001` 被其他项目占用 | 本机端口冲突，不是仓库缺陷。 |

## 5. 实施状态（2026-10-08）

- **F1 已修。** `backend/sql/033_sycm_fact_select_for_upsert.sql` 给 `bi_app` 补两张事实表的 `SELECT`。`tests/test_sycm_upload.py` 先收回 `SELECT` 证明 upsert 失败，再重放 033，用 `bi_app` 覆盖同一店铺同一天，并覆盖客户维度 upsert。本机 `bi_agent_test` 与 `bi_agent` 已应用 033。
- **F2 已修。** `normalize_platform` 只对 `PLATFORM_LABELS` 做精确反查，反查结果必须仍在来源注册表里。`抖店`、`淘系`、`1688` 仍拒绝。`拼多多` 只解析成 `pdd`，支付口径仍是 `None`。
- **F3 已按授权扫描，五平台合计仍未成立。** 凭证在 `.env.sync`（写入库是本机 `bi_agent_test`；`.env.test` 没有快麦凭证，也没有 `BI_WRITER_DSN`）。`consign-scan --start 2026-09-01 --end 2026-10-01 --mode replay`：20 家授权店 × 30 天 = 600 个窗口，`windows_failed=0`。拼多多店不在这 20 家里，扫描没有为它出数。完整窗口原先只推进 `covered`，`data_as_of` 仍为空，查询会停在 `data_as_of_unknown`。现已改为完整窗口把 `data_as_of` 推到窗口排他终点（失败窗口回滚，不发布截止，也不把质量写成 `passed`）。补放 2026-09-30 那天之后，20 家店的 `orders_consign.data_as_of` 都是 `2026-10-01 00:00+08`，`quality_status` 仍是 `unknown`。
  用同一库重跑 2026-09-05 至 09-12 的出库口径平台对比：状态 `partial`。抖音 1374 件 / 54335.94 元，京东 123 件 / 13055.52 元，快手 160 件 / 11554.08 元；这三家平台的毛利参考分别是 19537.8、3798.68、3349.31。已有 `chart_spec`。淘宝 10 店、天猫 2 店整段排除（`coverage_incomplete`）：出库来源上仍有缺 `consignTime` 的单据（淘宝付款日落在 9 月的有 319 单，天猫 40 单；更早和 10 月也有）。这些单不在发货时间扫描的返回里，扫描窗口仍然完整，但读路径不能证明它们落在窗外，所以不能并进合计，也不能写成 0。来源质量未对账，排名只覆盖已评估平台。拼多多没有授权店，结果里没有拼多多行。
- **F4 未做。** 仍要所有者写下三家店的 `shop_id`。

## 6. 建议顺序

1. F1（迁移 + `bi_app` upsert 测试）。不依赖外部 API。
2. F2（中文平台名精确反查 + 拒绝未知别名的测试）。
3. F4 只在所有者写下三家店的 `shop_id` 之后做。
4. F3 只在明确授权真实 `consign-scan` 之后做。

F1 完成前，不要把「已在本机 GRANT SELECT」写成发布就绪。F3 的 9 月扫描完成后，不要把五平台对比写成可用：淘宝、天猫仍因缺发货时间的出库单据被排除，拼多多不在授权范围，质量仍是未核验。
