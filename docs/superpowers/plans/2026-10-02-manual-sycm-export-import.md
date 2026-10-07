# 技术方案：生意参谋手工导出导入与"缺数上传"闭环

日期：2026-10-02。基线：`main@26a93e3`。

前置取证：
- 两轮店铺经营者工作流测试（`research/2026-10-01-shop-manager-daily-workflow-offline-test.md`、`research/2026-10-02-shop-manager-workflow-retest.md`）：R17/W14 类"实际推广费率与 ROAS"因无推广实耗来源一律拒答；流量/归因类问题缺整个指标族。
- 手动导出表核对（2026-10-02，会话内完成）：`docs/手动导出表格/` 12 个 xlsx = 4 家淘店 × 商品/客户/店铺维度 × 61 天（2026-08-01 至 09-30）；店铺维度含 5 个推广花费列与支付/退款/流量列且 61 天有实值；商品 ID（12 位）与库内 ID 空间零交集；`channel_items` 为空表。

关联文档（必读）：
- `docs/superpowers/specs/2026-09-12-multi-source-metrics-design.md` §3–§6（来源注册、能力与覆盖契约）
- `docs/metrics.md`（指标口径与允许面）、`docs/runbook.md`（来源注册与回退）
- `research/2026-09-25-s3-acceptance.md`（披露先例：质量未核验可出数+披露）

**已锁定的所有者决策**：上传数据**立即可用+披露**（不做审核暂存）；未映射店铺（元发美特钉枪直销店、钉子工厂）**本轮整文件拒绝**并报稳定原因。

## 0. 目标与原则

目标：把生意参谋手工导出（店铺维度为主、客户维度随附）登记为一等"手工来源"，补两个长期缺口——**推广实耗（费率/ROAS）**与**流量指标族**；并把缺数拒答升级为行动闭环：拒答时说明缺哪份文件、前端给出上传按钮、补齐后同一问题可答。

原则（全部沿用现有纪律，不重开）：
1. Fail-closed + 一等 data_missing：缺窗口、缺来源仍是拒答+原因，永不补零。
2. 口径不混：生意参谋支付金额是独立口径列，与 ERP `paid_amount` 永不比较、合计或互相替代；ROAS/费率只用同源比值。
3. 缺文件提示由**服务端**从来源注册表与覆盖凭证派生；模型不得生成 remediation 内容（防幻觉/注入出假按钮）。
4. 上传是新的写面：走既有 WebWrite 边界（`X-BI-Agent: web` + Origin + 认证主体），全量限制与稳定原因码。
5. PII 白名单不变（客户维度只收聚合列）；拼多多永久排除（manual 来源永不授予 pdd 店铺）。
6. 一切新 gate 默认关闭；迁移只在本机 `*_test` 库。

## 1. 范围与输入契约

**每次导入的输入**（已与所有者确认）：

| 文件 | 必须 | 说明 |
| --- | --- | --- |
| `淘店-{店名}-店铺维度_YYYYMMDD_{hash}.xlsx` | ✅ | 推广花费 5 列、支付金额、成功退款金额、访客/浏览/转化/加购/收藏等；关键列：`统计日期`、`店铺名称` + 上述指标列 |
| `淘店-{店名}-客户维度_YYYYMMDD_{hash}.xlsx` | 可选 | 客群/复购/粉丝日聚合，只收白名单聚合列 |
| `淘店-{店名}-商品维度_….xlsx` | ❌ 本轮不导入 | 商品 ID 无映射（挂起，等 `channel_items`） |

- 店铺映射以**数据列**`店铺名称`为准（文件名可不信）：当前仅 宝威德滤纸直销→166517、沃金五金机电商城→166647；其余整文件拒绝（`shop_not_registered`）。
- 窗口自由、允许重叠：按 (shop_id, stat_date) 幂等覆盖，后批替换前批，批次 lineage（文件名哈希）保留。
- 数值清洗：千分位；推广花费空值按"零花费+披露"（同列其它天有值视为列可用）；揽收/发货/派送/签收包裹列本批全零，**本轮不启用**。
- 商品维度与两家未映射店铺的 onboarding 为后续独立决策，不在本方案内。

## 2. 设计决策

- **D1 立即可用+披露**：上传校验通过即写入事实表并更新覆盖凭证（`quality_status='unknown'`、`quality_rule='manual-export/unverified'`）；回答时披露"来源：手工导入生意参谋导出（批次、窗口、未核验）"。先例：同步质量 unknown 可出数+披露。
- **D2 同源比值**：`roas = sycm支付金额 ÷ sycm推广花费`、`ad_spend_ratio` 同理；sycm 支付金额存储为独立列 `paid_amount_sycm`，禁止与 ERP `paid_amount` 出现在同一合计/比较/排名（测试钉死）。
- **D3 remediation 服务端派生**：查询绑定 manual 来源且窗口不足时，Artifact 附结构化 `remediation{kind:'upload', file_kind:'店铺维度', shop_refs, covered_window, missing_range}`；公开文案为服务端固定模板。
- **D4 上传成功不自动重跑**：前端给"重新查询"按钮重发原问题文本，用户确认执行。
- **D5 开关**：`SYCM_MANUAL_IMPORT_ENABLED=false` 默认。关闭时上传路由 404、导入 CLI 拒绝、指标不注册，聊天行为与现状逐字一致（既有 gate 纪律）。

## 3. 切片任务书

### M1 数据层迁移（仅本机测试库）
- `bi.sycm_shop_daily`：PK(shop_id, stat_date)，列白名单锁定店铺维度中选定的 ~18 列（visitors、item_visitors、views、paid_amount_sycm、paid_buyers、paid_items、order_amount、refund_amount、ad_spend_all/keyword/crowd/smart、ke_commission、pay_conversion_rate、aov、cart_count、favorite_count），附 batch_id、recorded_at。
- `bi.sycm_customer_daily`（可选同构，白名单聚合列）。
- `bi.manual_uploads`（id、subject_id、filename、content_sha256、kind、status、reason_code、rows_read/written、shops、created_at/completed_at）+ 不可变事件表 `manual_upload_events`。
- 覆盖凭证复用 `bi.sync_state`（source='manual.sycm.export'、entity='shop_daily'、covered multirange、data_as_of）。

### M2 解析器与来源注册（纯函数 + 注册表，红先行）
- 标准库 xlsx 读取器（zipfile + ElementTree，不新增依赖）；防护：文件 ≤5MB、解压总量 ≤64MB、行 ≤20 万、列 ≤256、sheet ≤16（zip 炸弹/实体膨胀防护测试）。
- 校验管线输出稳定原因码：`missing_columns / bad_date / shop_not_registered / row_overflow / pii_columns_rejected / parse_failed`。
- 来源注册（新 manual-export family）+ 指标能力授予挂 `bi.shops.capabilities` 既有机制；pdd 店铺永不授予。
- CLI：`python -m bi_agent.sycm_import <目录|文件>`（batch 模式，供首批 12 文件与运维补数）。

### M3 指标与查询层（固定工具优先不变）
- `query_business` 词表新增：`ad_spend`（可按 channel 细分列）、`ad_spend_ratio`、`roas`、`visitors`、`pay_conversion_rate`、`cart_count` 等；逐店来源绑定解析 manual 覆盖；窗口不足 → 既有 missing_data + `coverage.gaps` 机制。
- remediation 载荷（D3）进公开 Artifact 白名单与前端验形。
- `evaluate_promotion` `actual_budget` 模式：对有 manual 覆盖的店从 missing_data 改为可答（实耗=手工导入，带披露）。

### M4 上传接口
- `POST /api/uploads/sycm`（multipart；WebWrite 三件套）；响应 ingestion 记录与原因码。
- 仅接受授权范围内店铺名的文件；非 xlsx/超限/结构坏/含个体级 PII 列名一律拒绝并落审计事件。
- 幂等：重复上传同窗口按 M1 规则覆盖并留 lineage。

### M5 前端闭环
- remediation 白名单渲染 →"上传缺失文件"按钮（file input 限 xlsx）→ 进度/结果（原因码对照文案）→ 成功后"重新查询"。
- 载荷验形不合法整卡拒绝（沿用前端白名单纪律）。

### M6 验收与文档
- 用现有 12 文件跑 CLI：2 店入库（61 天 × 指标列齐全）、2 店拒绝带 `shop_not_registered`、商品维度文件拒绝带 `not_imported_this_round`。
- 离线题库扩展 ≥3 题：推广花费可答（含披露断言）、越窗缺数带 remediation 断言、ROAS 同源断言；既有 29 题零回归。
- 全量后端门禁 + 前端 `npm test`/`npm run build`；`runbook.md`（来源注册、上传操作、回退=关 gate）与 `metrics.md`（新指标口径）更新。
- 真实数据冒烟：隔离实例问答"沃金最近 30 天全站推广花费与 ROAS"（可答）与"10 月推广花费"（missing_data + 上传提示）。

## 4. 工程与流程约束（写给执行 agent）

红先行（解析器攻击语料、口径混用禁令、remediation 服务端派生都要先红）；共享库测试串行；单写者 + 独立复审 + 父级门禁 + 单 Task 提交；不新增依赖；迁移在 `*_test` 按序重放并验证幂等；`sycm_paid_amount` 与 ERP `paid_amount` 不得出现在同一 SQL 聚合/比较（扫描测试钉住）；remediation 文案与按钮全部来自服务端常量/注册表，模型可见载荷中不含任何"请上传"指令。

## 5. 风险与开放问题

- 手工窗口一次性：问题常落在窗口外 → remediation 提示会频繁出现，这是诚实行为而非缺陷；后续可做"定期导出"提醒。
- 上传解析的安全面：zip 炸弹/实体膨胀/超大行——防护上限 + 攻击语料测试；上传接口是新增写面，注意限流与审计。
- sycm 支付金额与 ERP 支付金额的系统性差异**未对账**：首版只披露不对账；后续可加逐店逐日对账报告作为该来源的认证路径（通过后质量从 unknown 升 passed）。
- 元发美特钉枪直销店、钉子工厂 onboarding：后续决策。
- 商品维度导入：挂起至 `channel_items` 映射建立。

## 6. 发布形态（本方案完成时）

`SYCM_MANUAL_IMPORT_ENABLED=false` 默认；本地启用步骤、上传操作与回退写入 runbook；生产启用为所有者显式决定（沿用受控探索等子项目的延后模式）。完成态：两店推广/流量可答带披露、越窗问题给上传闭环、既有 29 题与全部门禁零回归。
