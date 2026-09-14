# 语义目录与 Schema 检索：本地验收记录

日期：2026-09-14。
基线：`main`，实现 HEAD `f708ba6`（`feat: validate semantic catalog at startup`）。
范围：**仅本机 `*_test` 数据库与离线/合成环境**。本文不构成发布就绪声明，也不改变 Task 11 统一发布门禁的状态。

口径依据：[Task 11 后置子项目总设计](../specs/2026-09-14-post-task11-subprojects-design.md) §3–§5、§10–§12、
[语义目录与 Schema 检索实施计划](../plans/2026-09-14-semantic-catalog-and-schema-retrieval.md)、
[Task 11 后置项目本地开发例外](2026-09-14-post-task11-local-development-exception.md)、
[Task 11 日期化发布验收](2026-09-14-task-11-release-acceptance.md)、
[bi-agent 开发流程](../../../.agents/skills/bi-agent-development-workflow/SKILL.md)。

## 0. 一句话结论

**语义目录与确定性 Top 5 检索（计划 Task 1–4）在 HEAD `f708ba6` 上通过本地回归：全量后端 984 项零失败零跳过（含测试库）、离线结构化验收 26/26、30 题 gold set 30/30 匹配且所需视图 Top 5 召回率 100%（27/27）、非法 JOIN 返回数 0、只读角色下目录预检通过而 `bi.orders` 仍被拒（`InsufficientPrivilege`/SQLSTATE 42501）、`SEMANTIC_CATALOG_ENABLED=false` 与"变量缺席"在工具集/请求/状态/Artifact/确定性结果与启动面上逐项相等（70 行比较里 67 行为相等判定且全为 true，0 处差异）且没有任何新增 Agent Tool。**

缺的是**外部证据而不是代码路径**：真实 provider、目标环境迁移与授权、生产启用、部署与试用一律记 `未执行`。Task 11 统一发布门禁的 4–7 项仍为 open（本项本地开发例外只解除"允许写本地代码"，见 §9）。

## 1. 环境（实测版本）

| 项 | 实测值 |
| --- | --- |
| 仓库 / 分支 / HEAD | `D:/Projects/bi-agent`、`main`、`f708ba605da160076cab1f744fa4f3537b35ccc3` |
| Python | 3.11.16（`python -VV`：`MSC v.1944 64 bit (AMD64)`，uv 管理的 cpython-3.11-windows-x86_64） |
| 包管理 | uv 0.12.7（命令统一带 `--locked`，未增删依赖、未改锁文件） |
| PostgreSQL 服务端 | `PostgreSQL 17.6 (Debian 17.6-2.pgdg13+1) on x86_64-pc-linux-gnu, compiled by gcc 14.2.0, 64-bit`（本机 docker `postgres:17.6`，宿主端口 54329，`deploy/postgres` compose 工程） |
| 测试库 | `bi_agent_test`（本机 `localhost:54329`），DSN 只从 `backend/.env.test` 读：`BI_TEST_ADMIN_DSN` / `BI_TEST_READER_DSN` / `BI_TEST_SYNC_DSN` |
| 只读角色 | `bi_reader`（§4 的目录预检与底表拒读探针都用它，`current_user` 已核对）；授权面核对用 `BI_TEST_ADMIN_DSN`（实测 `current_user = postgres`，只查 `information_schema.table_privileges`） |
| Node / npm | v22.23.2 / 10.9.8（前端未改动，仅跑回归） |
| 语义目录版本 | `semantic/2026-09-14.1`（`SEMANTIC_CATALOG_VERSION`，`CATALOG.version` 同值；30 题输出里的 `catalog_version` 全部为该值） |
| 目录规模 | 11 视图 / 84 字段 / 22 指标 / 10 实体 / 4 条 JOIN 边 |
| 结构契约版本 | `reporting/2026-09-14.1`（`VersionSet.schema_version`，示例值取自计划） |

开工时的受保护未跟踪文件（`.cloudflared.*`、`.streamlit.*`、`.tools/`、`taoxi-probe-status.py`）全程未读取、未修改、未暂存。

## 2. Step 1：串行执行的回归门禁（`backend/`，Windows）

所有命令逐条串行跑（这些套件共用 `bi_agent_test`，外层事务回滚但种子行/锁/共享表是全局态）。

| 命令 | 结果 |
| --- | --- |
| `uv run --locked --env-file ../.env.test python -m unittest discover -s tests -t .` | `Ran 984 tests … OK`（**0 failure / 0 error / 0 skip**） |
| `uv run --locked --env-file ../.env.test python -m tests.acceptance --offline` | `{"mode":"offline","total":26,"passed":26,"failures":0,"result":"pass"}`，exit=0 |
| `uv run --locked --env-file ../.env.test python -m unittest tests.test_channel_mapping tests.test_commerce tests.test_comparison tests.test_listing_audit tests.test_inventory tests.test_operator_workflows tests.test_semantic_catalog` | `Ran 494 tests … OK`（Task 11 记的 387 + 语义目录 107） |
| `uv run --locked --env-file ../.env.test python -m unittest tests.test_db tests.test_api tests.test_runtime_db` | `Ran 128 tests … OK`（DB-enabled，非整组 skip；Task 11 记的 124 + `test_api` 因 Task 4 新增的 4 项） |
| `uv run --locked --env-file ../.env.test python -m unittest tests.test_core tests.test_runtime tests.test_business_query_graph tests.test_catalog tests.test_data_quality tests.test_recovery tests.test_multi_source_metrics` | `Ran 362 tests … OK`（Task 11 记的 360 + `test_core` 因 Task 4 新增的 2 项） |
| `uv run --locked --env-file ../.env.test python -m unittest tests.test_semantic_catalog -v` | `Ran 107 tests … OK` |
| `cd ../frontend && npm test -- --run` | `Test Files 8 passed (8)`、`Tests 82 passed (82)` |
| `cd ../frontend && npm run build` | `tsc -b && vite build` 成功，`38 modules transformed` |

跳过数核对（把 DSN 拿掉的同一批命令，用来证明"跳过是环境门禁而不是掩盖失败"）：

| 命令 | 结果 |
| --- | --- |
| 全量 discover（无 `BI_TEST_*_DSN`） | `Ran 984 tests … OK (skipped=384)`（600 项非库用例仍全跑） |
| `tests.test_semantic_catalog`（无 DSN） | `Ran 107 tests … OK (skipped=2)`（跳过的是需要 `*_test` 的两项：登记列与真实 `information_schema` 对账、只读角色预检） |
| `python -m tests.acceptance --offline`（无 `BI_TEST_ADMIN_DSN`） | `{"mode":"offline","result":"skipped","reason":"未配置BI_TEST_ADMIN_DSN"}`，exit=2 |

结论：**带库跑 0 skip、离线 26/26、前端零失败**。"984/26/82" 只对本机这套 `*_test` 环境负责，不能读成"任何机器都能跑"。

## 3. 30 题 gold set 逐项结果（S01–S30）

runner 是仓库内的 `tests.test_semantic_catalog.SemanticGoldSetTests`（6 项用例，含形状、覆盖、脱敏、可复现与"反恒真"自检）。逐题输出取自一次性探针脚本，该脚本只复用已提交 helper（`gold_rows()`、`retrieve()`、`SemanticGoldSetTests.check_row()`、`catalog_refs()`），未修改测试或实现，跑完即删；复现命令见 §8。

`指标（期望=实得）`与 `JOIN 边（期望=实得）`两列由 runner 的**集合相等**判定（`sorted(actual) == sorted(expected)`），不是"包含"；`Top 5 实得候选`是 `view_refs`（实现保证 ≤5）。

| 题 | 提问 | 允许领域 | Top 5 实得候选 | 指标（期望=实得） | JOIN 边（期望=实得） | 澄清 | 缺失概念 | 判定 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| S01 | 按店铺看每日支付金额 | business_query | `view-shop-daily`、`view-shops` | `metric-paid-amount` | — | 否 | — | 通过 |
| S02 | 各店的退款金额和平台原始退款金额分别看 | business_query | `view-refunds`、`view-shop-daily` | `metric-refund-amount`、`metric-refund-record-amount` | — | 是 | — | 通过 |
| S03 | 按仓库和SKU看库存池可用量、入库量、锁定量与计量单位 | inventory_watch | `view-physical-stock-items`、`view-channel-stock-items` | `metric-inbound-quantity`、`metric-locked-quantity`、`metric-physical-available-quantity` | — | 是 | — | 通过 |
| S04 | 按店铺和平台看支付订单数 | business_query | `view-shop-daily`、`view-shops` | `metric-paid-orders` | `join-shop-daily-shops` | 否 | — | 通过 |
| S05 | ERP单据数是多少 | business_query | `view-shop-daily` | `metric-erp-documents` | — | 否 | — | 通过 |
| S06 | 按币种看支付金额 | business_query | `view-shop-daily`、`view-payments`、`view-shops` | `metric-paid-amount` | — | 是 | — | 通过 |
| S07 | 按出库口径看销售额 | business_query | — | — | — | 是 | — | 通过 |
| S08 | 收支差和现金差按店铺看 | business_query | `view-shop-daily`、`view-shops` | `metric-cash-difference` | — | 否 | — | 通过 |
| S09 | 上架价和标价哪个高 | listing_price_audit | `view-listing-items` | `metric-listing-price` | — | 否 | — | 通过 |
| S10 | 成交均价与上架价哪个更能说明问题 | commerce_performance、listing_price_audit | `view-product-cost-daily`、`view-listing-items` | `metric-listing-price`、`metric-transaction-average-price` | — | 是 | `price_basis` | 通过 |
| S11 | 实物库存与渠道库存一起看 | inventory_watch | `view-channel-stock-items`、`view-physical-stock-items` | `metric-channel-sellable-quantity`、`metric-physical-available-quantity` | — | 是 | `inventory_grain` | 通过 |
| S12 | 按出库口径看ERP单据数 | business_query | `view-shop-daily` | `metric-erp-documents` | — | 否 | — | 通过 |
| S13 | 销量和赠品件数按商品看 | business_query、commerce_performance | `view-product-daily`、`view-product-cost-daily` | `metric-gift-quantity`、`metric-sold-quantity` | — | 否 | — | 通过 |
| S14 | 成本口径件数与带成本的行数、商品行数 | commerce_performance | `view-product-cost-daily`、`view-product-daily` | — | — | 否 | — | 通过 |
| S15 | 商品分摊支付金额与销量 | business_query、commerce_performance | `view-product-daily`、`view-product-cost-daily` | `metric-product-paid-amount`、`metric-sold-quantity` | — | 否 | — | 通过 |
| S16 | 商品销售额和销量一起看 | business_query、commerce_performance | `view-product-daily`、`view-product-cost-daily` | `metric-sales-amount`、`metric-sold-quantity` | — | 是 | — | 通过 |
| S17 | 商品成本合计和单据成本哪个高 | commerce_performance | `view-erp-document-daily`、`view-product-cost-daily`、`view-product-daily` | `metric-cost-total`、`metric-erp-document-cost` | — | 是 | — | 通过 |
| S18 | 按店铺看ERP单据数和商品销售额 | business_query、commerce_performance | `view-product-cost-daily`、`view-shop-daily`、`view-erp-document-daily`、`view-product-daily`、`view-shops` | `metric-erp-documents`、`metric-sales-amount` | — | 是 | — | 通过 |
| S19 | 在上架价旁边顺便看看支付金额 | listing_price_audit | `view-listing-items` | `metric-listing-price` | — | 是 | — | 通过 |
| S20 | 上架价与活动价按在售链接看 | listing_price_audit | `view-listing-items` | `metric-campaign-price`、`metric-listing-price` | — | 否 | — | 通过 |
| S21 | 按平台看支付金额和销量 | business_query、commerce_performance | `view-product-daily`、`view-shop-daily`、`view-shops` | `metric-paid-amount`、`metric-sold-quantity` | `join-product-daily-shops`、`join-shop-daily-shops` | 否 | — | 通过 |
| S22 | 支付流水金额按支付时间看 | business_query | `view-payments` | `metric-payment-flow-amount` | — | 否 | — | 通过 |
| S23 | 把支付流水和退款单逐笔对上 | business_query | `view-payments`、`view-refunds` | — | — | 是 | — | 通过 |
| S24 | 数据质量状态按来源看 | business_query、inventory_watch | `view-coverage` | — | — | 否 | — | 通过 |
| S25 | 比较商品毛利和ERP单据毛利 | commerce_performance | `view-erp-document-daily`、`view-product-cost-daily`、`view-product-daily` | `metric-erp-gross-profit-reference`、`metric-product-gross-profit-reference` | — | 是 | `profit_grain` | 通过 |
| S26 | 广告归因后的净利润是多少 | business_query | — | — | — | 是 | `attribution`、`net_profit` | 通过 |
| S27 | 推广花费和流量能算进净利润吗 | business_query | — | — | — | 是 | `net_profit`、`promotion_spend`、`traffic` | 通过 |
| S28 | 归因后的成交均价 | commerce_performance | `view-product-cost-daily` | `metric-transaction-average-price` | — | 是 | `attribution` | 通过 |
| S29 | 净利润率与广告花费能算进收支差吗 | business_query | `view-shop-daily` | `metric-cash-difference` | — | 是 | `net_profit`、`promotion_spend` | 通过 |
| S30 | 商品销售额与销量按商品看 | business_query | `view-product-daily` | `metric-sold-quantity` | — | 是 | — | 通过 |

### 3.1 汇总数

| 指标 | 数值 |
| --- | --- |
| gold 行数 / 逐题与期望完全相符 | 30 / **30** |
| 有"所需视图"的题数 | 27（整份拒绝候选的三题：S07、S26、S27） |
| 所需视图全部落进 Top 5 的题数 | **27 / 27 ⇒ Top 5 必需视图召回率 100%** |
| 候选视图数 > 5 的题 | 0 |
| JOIN 路径断言相符的题 | **30 / 30**（其中期望非空边的 2 题：S04 得 `join-shop-daily-shops`、S21 得 `join-product-daily-shops` + `join-shop-daily-shops`，与期望完全相同） |
| 返回未登记 JOIN 边（非法 JOIN）的次数 | **0** |
| 期望了未登记 ref 的题 | 0 |
| 输出泄露 `reporting.` / `bi.` / `v_` / `shop_id` / `pool_id` 的题 | **0**（逐题对 `repr(selection)` 扫描） |
| 返回非法 ref 形状（不匹配 `REF_RE`）或目录外 ref | 0 |
| `requires_clarification=true` / `false` 的题数 | 17 / 13 |
| 缺失概念码集合 | `profit_grain`(S25)、`inventory_grain`(S11)、`price_basis`(S10)、`attribution`(S26/S28)、`net_profit`(S26/S27/S29)、`promotion_spend`(S27/S29)、`traffic`(S27)——全部取自固定表，未回传任何原文 token |
| 三对故意不登记的边 | `payments↔refunds`（S23）、`商品成本↔单据毛利`（S25、S17）、`实物库存↔渠道库存`（S11）全部 `join_paths=[]` + 要求澄清 |
| 逐题重复调用结果一致（确定性） | `test_every_row_is_reproducible` 通过（30 题各跑两遍，逐项相等） |

设计 §5.4 的四条验收线（≥30 题冻结、Top 5 召回 100% 且禁止视图召回为 0、合法 JOIN 与人工 gold 完全一致、候选不含真实店铺 ID/底表名）本轮全部满足。

## 4. 目录声明与真实 schema / 权限核对（只读角色，本机 `bi_agent_test`）

仓库内用例 `tests.test_semantic_catalog.SemanticSchemaCheckDatabaseTests`（`@skipUnless(BI_TEST_READER_DSN)`）：`Ran 1 test … OK`。它断言 `conn.info.user == "bi_reader"`、库名以 `_test` 结尾、`validate_catalog_schema(conn, CATALOG)` 返回 `None`、`SELECT count(*) FROM reporting.v_shop_daily` 可读、`SELECT 1 FROM bi.orders LIMIT 1` 抛 `psycopg.errors.InsufficientPrivilege`。

一次性探针（复用 `tests.dbfixtures.connect_test_db`，只读元数据 + `SAVEPOINT` 包裹每条探针，跑完即删）实测：

```text
reader user: bi_reader | db: bi_agent_test | host: localhost | port: 54329
postgres version(): PostgreSQL 17.6 (Debian 17.6-2.pgdg13+1) on x86_64-pc-linux-gnu …
validate_catalog_schema(reader, CATALOG) -> None
catalog sizes: views=11 fields=84 metrics=22 joins=4 entities=10 version=semantic/2026-09-14.1
denied  : SELECT 1 FROM bi.orders LIMIT 1        -> InsufficientPrivilege 42501
denied  : SELECT 1 FROM bi.order_items LIMIT 1   -> InsufficientPrivilege 42501
denied  : SELECT 1 FROM bi.shops LIMIT 1         -> InsufficientPrivilege 42501
denied  : SELECT 1 FROM bi.sync_state LIMIT 1    -> InsufficientPrivilege 42501
denied  : INSERT INTO bi.app_chats DEFAULT VALUES -> InsufficientPrivilege 42501
```

即：**`bi.orders` 在预检所用的只读身份下仍然被拒，预检没有扩大任何业务事实读取面**。

授权面核对（`information_schema.table_privileges`，本机测试库）：11 张登记视图对 `bi_app` 与 `bi_reader` **均只有 SELECT**；`bi_app` 在 `bi.*` 上只有聊天与运行追踪表（`app_chats`、`app_messages`、`query_runs`、`query_run_events`、`query_artifacts`、`query_diagnostics`、`query_provenance`、`expected_listing_rosters`、`price_audit_expectations`），事实表 `orders` / `order_items` / `shops` / `sync_state` 等一个都没有。

目标环境（生产/预发布）的同一核对：`未执行`（见 §9）。

## 5. 启动预检的替身级契约（不碰库的部分）

`tests.test_semantic_catalog.SemanticSchemaCheckTests` 15 项 + `SemanticCatalogValidationTests` 8 项 + `SemanticRegistryTests` 14 项全绿（本模块共 107 项：`SemanticContractTests` 15、`SemanticRetrievalTests` 38、`SemanticGoldSetTests` 6、`SemanticRetrievalSafetyTests` 4、`SemanticTermTests` 5、`SemanticRegistrySchemaTests` 1、`SemanticSchemaCheckDatabaseTests` 1），钉住的是错误契约与登记闭包：

- 预检恰好执行计划指定的那**一条** `information_schema.columns` 查询，替身对任何其它 SQL（含 `SELECT 1`）直接报错；该查询不带参数、不含 `bi.`、不含分号。
- 缺列 / 缺视图 / 类型族不符 → `SchemaMismatch`（`ValueError` 子类），消息固定 `semantic_schema_mismatch:<第一个排序后的稳定 ref>`；同一缺陷集合在任何进程给出同一字符串。
- 消息里不出现 `reporting.`、视图名、列名、`shop_id`、`SELECT`、`information_schema`、`postgresql://`、密码；绕过契约层构造的畸形 ref 也只能得到 `semantic_schema_mismatch:catalog-entry`（原文不回显）。
- 目录不闭包时在**碰数据库之前**就失败（`semantic_catalog_*`），且登记列/视图逐条对账（断言下限 ≥60 字段、≥8 视图，实际实现为全量遍历不抽样）。
- `resolve_sql_identifier()` 只认已登记 view/field ref；带点号的原始 SQL 名一律 `KeyError` 且不回显入参。

## 6. Step 2：`SEMANTIC_CATALOG_ENABLED=false` 与"变量缺席"的实测对照

这是**取证比较**，不是推论：同一份一次性探针（复用 `tests.acceptance` 的 `_load_questions` / `_offline_model` / `_run_turn` / `_memory_store` / `_verify_turn` 与 `tests.test_db` 的合成基准，未修改任何测试或实现）分别在两种环境下各跑一次，再把两次输出逐字段比较；探针与其输出均在仓库外、跑完即删。

环境差异只有两处：显式 `SEMANTIC_CATALOG_ENABLED=false`，与该变量从环境中缺席（`uv run --locked --env-file ../.env.test …` 的 `--env-file` 内容两边相同，`.env.test` 本身不含该变量）。

### 6.1 Task 11 的 tool schema 快照（6 个工具，无新增）

| 项 | 两种环境实测值 |
| --- | --- |
| 工具数 | `6` / `6` |
| 工具名（顺序） | `query_business`、`analyze_product_performance`、`compare_performance`、`audit_listing_prices`、`inspect_inventory`、`evaluate_promotion` |
| `_tool_schemas()` 全量摘要（UUID 归一后的 sha256） | `5d73ccc5c43831431367385d036f8a2ce351810499f5bb4e6f20859c2fe8629d`（两侧相同） |
| 逐工具摘要（两侧相同） | `query_business` `273d0030b90e…408833`、`analyze_product_performance` `b8745bdb23dd…52e1c82`、`compare_performance` `b9ac34b8d448…ec538f`、`audit_listing_prices` `ff4b57b9aa7d…a45eda`、`inspect_inventory` `8190d5837594…349f1cb`、`evaluate_promotion` `bfc6c8cc11f8…6d0829` |

没有任何工具名或 schema 描述涉及语义目录：`semantic` 一词在 `bi_agent/agent.py` 与 `bi_agent/llm.py` 里出现 0 次（`grep -rn "semantic" backend/bi_agent/agent.py backend/bi_agent/llm.py` 无输出），该包只被 `bi_agent/api.py`（启动预检）导入。**语义目录没有新增 Agent Tool。**

### 6.2 Q01 / Q15 离线回合（真实测试库 + 预制模型）

比较字段（每回合 29 行，每题再加题级 `status` 与 `verification_problems` 两行＝31 行；工具集 5 行；配置与环境标签 3 行；共 70 行）。逐题字段包括：`normalized_requests`（发给查询层的 `QueryRequest.model_dump()`）、`normalized_requests_sha`、运行层 `normalized_request` / `status` / `domain` / `run_count` / `event_count`、`artifact_count`、`artifact_types`、`artifact_payloads_sha`、`tool_result_status`、`tool_result_data` / `diagnostics` / `coverage` / `basis` / `limitations` / `source_batches` / `metric_definition`、`result_payloads_sha`、`turn_text`、`turn_clarification`、`turn_error_code`、`model_received_tool_names` / `model_received_tools_sha`、`scripted_tool_call_names`、`query_business_calls`、`verification_problems`。

| 项 | Q01（店铺A 近七天支付金额） | Q15（全公司全平台近七天支付总额，授权 S1/TB1/PDD1） |
| --- | --- | --- |
| 离线验收判定 | pass（两侧都是） | pass（两侧都是） |
| `query_business` 调用数 | 1 | 1 |
| 规范化请求（两侧逐字相同） | `{"basis_policy":"strict","compare":"none","currency":"CNY","end":"2026-09-08","group_by":"total","metrics":["paid_amount"],"shop_ids":["S1"],"start":"2026-09-01","top_n":10}` | 同上形状，`shop_ids` 为 `["TB1","S1","PDD1"]` |
| 运行状态 / 领域 | `succeeded` / `business_query` | `missing_data` / `business_query` |
| 工具状态 | `ok` | `missing_data` |
| 结果数据 | `[{"paid_amount":"1000.000000"}]` | `[]`（能力门禁拒答，不补 0） |
| 限制披露 | 「来源质量未核验（尚无对账记录）」 | 「1 家店铺缺少 paid_amount 的已核验能力，未执行金额查询」 |
| 口径凭证 | `platform_payment/v1` + `pay_time` + `metrics/2026-09-12.1` | `[]`（未出数 ⇒ 无凭证） |
| 覆盖 | `status=complete`、`[2026-09-01,2026-09-08)`、gaps `[]` | `status=missing`、start/end/suggested 均 `null` |
| 事件数 / 运行数 / Artifact | 7 / 1 / **1 个 `metric_result`** | 7 / 1 / **1 个 `metric_result`** |
| Artifact 载荷摘要（UUID 归一） | `338997295db443ab…` 两侧相同 | `6e794f2a89ab7f67…` 两侧相同 |
| 工具结果整体摘要 | `2d3ed9f18ba4adeb…` 两侧相同 | `74f7a8e4ea0f4461…` 两侧相同 |
| 模型侧收到的工具清单 | 每次 `complete` 收到 6 个工具名（合计 12 项），两侧相同 | 同 |

两次运行的 JSON 输出：除"当前环境变量取值"标签本身（`"false"` vs `"<absent>"`）外，**结构完全相等**（比较脚本对去掉该标签后的两份文档给出 `raw equal ignoring label: True`）。70 行比较里 67 行是"相等与否"判定且全部 `true`（工具集 5 行 + 每题 31 行），剩下 3 行是信息性取值：两侧 `load_app_settings(...).semantic_catalog_enabled` 均为 `False`、`AppSettings` 默认也为 `False`、环境变量标签本身。

### 6.3 启动面（`create_runtime_app()`）

同一进程内先后用两种环境各跑一次工厂（`bi_agent.api.psycopg.connect`、`bi_agent.api.validate_catalog_schema`、`bi_agent.llm.create_model` 全换计数替身，因此这一段不碰数据库、不碰模型网络）：

| 观察项 | `false` | 变量缺席 | `true`（参照） |
| --- | --- | --- | --- |
| 事件序列 | `["model"]` | `["model"]` | `["connect","preflight-connection","validate","preflight-closed","model"]` |
| `settings.semantic_catalog_enabled` | `False` | `False` | `True` |
| 解析后的 `AppSettings`（DSN 为 `**********`） | 与左列相同 | 相同 | — |
| `/api` 路由集合（7 条） | `delete /api/chats/{chat_id}`、`get /api/chats`、`get /api/chats/{chat_id}/messages`、`get /api/health`、`patch /api/chats/{chat_id}`、`post /api/chats`、`post /api/chats/{chat_id}/messages` | 完全相同 | 完全相同（开关不改 HTTP 面） |
| Agent 工具清单 | 6 个，见 §6.1 | 相同 | 相同 |

同一契约在仓库内已有的固定回归：`tests.test_api.ApiTests.test_runtime_factory_makes_no_preflight_connection_when_disabled`（subTest 覆盖 `env_value=None` 与 `"false"`，要求不建连接、不调用 `validate_catalog_schema`）、`test_the_feature_gate_changes_no_route_or_user_visible_surface`（开与关的 `openapi()["paths"]` 集合相等）、`test_runtime_factory_fails_startup_when_the_preflight_mismatches`（不一致就启动失败）。

### 6.4 本节测到与没测到的

- 没测：真实模型下的路由/工具选择差异——`TurnResult` 不暴露"模型实际发出的工具调用"这一字段，探针里的工具调用名取自离线脚本（`acc._model_script`），因此这一列证明的是"预制的工具调用与服务端派发一致"，不是真实模型行为。真实 provider 的 26 题 live：`未执行`。
- 没测：浏览器端到端 SSE 往返与渲染（前端本轮零改动，只跑既有 82 项测试与生产构建）；线上链路：`未执行`。
- 未做：把门禁开成 `true` 去跑用户可见路径（本地也只验证预检本身与其失败契约）；生产启用：`未执行`。

## 7. 已知偏差与交接（不静默改代码）

以下都是已评审接受、有测试钉住的偏差，记录映射关系；本轮**未修改任何实现、测试或配置**。

### 7.1 计划示例 ↔ 已交付契约的映射

| 计划里的写法 | 实际交付 | 原因 |
| --- | --- | --- |
| Task 4：`SchemaMismatch("semantic_schema_mismatch:" + refs[0])`（收单个字符串） | `SchemaMismatch(refs: Sequence[str])`：内部去重 + 消毒 + 排序，消息为 `semantic_schema_mismatch:<refs[0]>`，完整清单在 `.refs` | 消息必须可复现且只能是 ref。计划里那句 `assertRaisesRegex(SchemaMismatch, "field-paid-amount")` 不能照抄：`field-paid-amount` 不是真实消息 `semantic_schema_mismatch:field-shop-daily-paid-amount` 的子串（裸 ref 因下一行的视图作用域命名而不存在）。交付用例改为**更强**的相等断言：`str(error) == "semantic_schema_mismatch:field-shop-daily-paid-amount"` 与 `error.refs == ("field-shop-daily-paid-amount",)` |
| Task 2 表格：authorization 列写 `field-shop-id`（多视图共用） | 字段 ref 一律视图作用域：`field-shop-daily-shop-id`、`field-shops-shop-id`、`field-physical-stock-items-pool-id`…… | `SemanticField.view_ref` 单归属，`shop_id` 出现在 10 张视图；共用 ref 会让按 ref 建的索引静默保留最后一个视图，其余视图的授权列解析到别的表——那是能发错金额的缺陷。计划那一句读作"该视图以 `shop_id` 授权" |
| Task 2 表格："至少 8 张视图"、`v_shops` 允许 `capabilities` | 11 张视图；`v_shops.capabilities`（数组）、`v_product_cost_daily.sku_ids`（数组）、`v_coverage.covered`（tstzmultirange）**不登记** | `DataType` 词表封闭、没有 array/multirange；伪装成 `text` 会让启动预检在真实 schema 上永久失败。列仍在库里，只是不可被检索选中；开放必须先扩词表并升目录版本 |
| Task 4 替身：`FakeIntrospectionConn(columns={"shop_id", "day", …})` | `FakeIntrospectionConn(tables={"reporting.v_shop_daily": {"shop_id": "text", …}})` | 类型族比对需要每列的 `data_type`；键带 schema，`public.v_shop_daily` 不能替 `reporting.v_shop_daily` 交差 |
| 类型族 | 声明 `ref` ⇒ 库里就是 `text`；声明 `integer` ⇒ `bigint/integer/smallint` 任一成员都算一致 | 唯一一份"声明类型 ↔ SQL `data_type`"知识放在 `registry.DATA_TYPE_SQL_FAMILIES`，成员互换不是漂移、改族才是 |
| 视图定义来源 | `reporting.v_product_daily` 以 `007_catalog_identity.sql` 的宽版本为准（003 的窄版本已被取代）；`v_coverage` 以 `008` 为准 | `CREATE OR REPLACE VIEW` 不能减列（runbook 已记），登记列逐字取自**当前生效**定义并由 `SemanticRegistrySchemaTests` 与真实 `information_schema` 对账 |
| Task 3 gold 示例：S01/S02 行里没有 `missing_concepts` 键；示例把"比较商品毛利和 ERP 单据毛利"写作 S02、"广告归因后的净利润"写作 S03 | 每行固定 8 个键（含 `missing_concepts`），那两题实际位于 **S25 / S26**；S01 文本与计划一致 | runner 要求逐题显式声明缺失概念（缺键即形状失败），编号按交付文件的冻结顺序；两者语义等价，本文 §3 以实得编号为准 |
| 总设计 §5.3 的失败语义 `needs_input` / `schema_ambiguous` | 当前公共契约只有 `SemanticSelection.requires_clarification: bool` + `missing_concepts`（无原因码通道） | 拒绝理由的分类法归属下游运行层；`registry.py` / `retrieval.py` 的注释仍用 `schema_ambiguous` 描述"意图"，那是**措辞陈旧**而非行为缺口——本轮不改（见 §7.3） |

### 7.2 检索计分的实现口径（超出计划三行描述的部分，均以已提交测试为准）

- `+5` 的"token 命中"计数单位是**每个"只沾到边但未完整命中"的别名一次**，不是每个共享子串一次（按子串计数会让长中文片段切出十几个二元组，把 `+40` 的字段完整命中压过去）。
- 视图分取"它自己证据里的最高一项"而不是求和（否则列多的视图靠数量压过被点得更准的视图）。
- 领域分是 `+10 × |条目领域 ∩ 允许领域|`；"未允许领域直接排除"之外，实现还保留被排除证据，用于"本轮答不了"时必须要求澄清（S19、S30 钉的就是这条）。
- 同类目"最长命中优先"跨条目裁剪：`商品销售额` 盖住 `销售额`，两种口径的金额不会同时点亮（Q08 那个坑）；领域过滤在裁剪之后做，避免被排除的长词让位给它的子串冒充。
- 两条口径前置判定：点名出库口径时把"只能按支付窗口回答"的金额（指标 ref **与其度量列 ref 两条通道**）从候选里摘走并要求澄清（gold S07 整份不发；同形说法由 `SemanticRetrievalTests.test_outstock_basis_never_resolves_to_a_product_sales_amount` 等三项钉住）；只用通用词（`销售额` / `GMV`）问金额视为口径未定。度量列通道必须一起看：支付指标可能被领域门禁挡住而裸列还在，只看指标就会把支付列当成出库问题的答案。
- 空允许域什么都不发；`limit` 只接受 1..5；非字符串入参、未知领域、旧目录版本一律在任何工作之前报错。

### 7.3 字段通道与 basis 归属（交接给后续项目，不在本轮改）

- `metric-sold-quantity` 是**唯一**的"销量"指标 ref：`v_product_daily.quantity` 与 `v_product_cost_daily.quantity` 在各自迁移里是同一条 WHERE + 同一条 GROUP BY（同一个数），成本视图那列只作为必需字段登记，不另起指标名（同一概念不许两种拼法）。
- 字段不拥有 domain：`_field_domains()` 让字段的可用性完全跟随所属视图，避免第二份说法分叉。
- 聚合语义两条约定只写在 `registry` 注释里，契约字段无处安放：`("avg",)` 表示比值型指标（分子分母在同一行集合上各自求和再相除），**不授权 `AVG(col)`**；上架价/活动价只在单个快照内有意义，跨快照必须先按 snapshot 过滤。将来受控 SQL 编译时必须把这两条落到执行层，而不是回头改目录。
- 首批目录不含任何"ERP 出库金额"列：出库口径仍在现有固定 Tool 与 Python 内计算（`sources.OUTSTOCK_BASIS`），不落批准视图的列上。这是"目录答不了"的边界，不是遗漏。
- `__init__.py` 包 docstring 仍写"启动一致性校验（`validate_catalog_schema`）与 feature gate 属 Task 4，此刻还不存在"——Task 4 已交付，该句过期（同段里"本包不查业务事实、不新增 Agent Tool"仍然成立）。`registry.py` / `retrieval.py` 提到的 `schema_ambiguous` / `needs_input` 同理（见 §7.1 末行）。两处都是文档措辞滞后，本轮按"只修 P0/P1"的边界不动代码与测试，登记为下一个 diff。

### 7.4 从 Task 11 报告继续挂着的项目（本轮零改动，状态不变）

`BasisContractDatabaseTests` 三个重名方法（§11.5）、库存血缘缺 `source_registry_version`（§11.6）、反恒真突变回归未固定（§11.7）、库存池授权无服务端写入方（§11.1）——本轮既未复现也未修复。

另外三处旧文案（属文案事实滞后，本轮按窄范围不顺手改）：

- `README.md` 仍写离线验收“验证 20 个业务问题”（Task 11 已升到 26 题）与“新克隆时…在两个库各跑一遍（001 → 016）”（runbook 的完整清单已到 019）；
- `README.md` 架构节仍只列 `query_business` 与 `evaluate_promotion` 两个内部工具，而实际工具清单已是 6 个（§6.1）；
- `docs/metrics.md` 首段仍写“单源覆盖、退款硬门禁与跨平台 basis 尚待原路线图 Task 5 实现”，而 5.1–5.4 已交付（见 runbook “平台路由与来源注册表”一节与 `docs/metrics.md` §6 的 5.2b / 5.2c / 5.4 小节）。

这三条都是下一个文档 diff 的事项，不影响本文的验收数字。

## 8. 复现命令（reviewer 可直接串行跑）

```powershell
Set-Location backend
uv run --locked --env-file ../.env.test python -m unittest discover -s tests -t .
uv run --locked --env-file ../.env.test python -m tests.acceptance --offline
uv run --locked --env-file ../.env.test python -m unittest tests.test_semantic_catalog -v
uv run --locked --env-file ../.env.test python -m unittest tests.test_semantic_catalog.SemanticGoldSetTests -v
uv run --locked --env-file ../.env.test python -m unittest tests.test_semantic_catalog.SemanticSchemaCheckDatabaseTests -v
Set-Location ../frontend
npm test -- --run
npm run build
```

§6 的逐字段比较目前**不在仓库里**（一次性探针，按流程要求跑完即删）。要复核结论：①`tests.test_api.ApiTests.test_runtime_factory_makes_no_preflight_connection_when_disabled` 与 `test_the_feature_gate_changes_no_route_or_user_visible_surface` 固定了"关闭/缺席时不建连接、不校验、路由面不变"；②`tests.test_semantic_catalog.SemanticGoldSetTests` 固定了 §3 的全部召回、JOIN 与脱敏判定；③Q01/Q15 的行为由 `python -m tests.acceptance --offline` 的 26/26 覆盖。若要把 §6 的 A/B 对比固定成回归，属于新增测试工程，需单独批准（本轮未做，也不擅自加）。

## 9. 未执行清单（逐条，全部为 `未执行`，不写"预计通过"）

1. 真实 provider 的 `--provider-smoke` 与 26 题 `--live`（含模型版本、调用数、错误分类）：`未执行`。本轮模型侧只有 `.env.test` 里的桩配置（`LLM_PROVIDER=deepseek` / `LLM_MODEL=demo-model` / `DEEPSEEK_API_KEY=fake-test-key`），不构成真实模型证据。
2. 语义目录与真实模型路由的接线（把检索结果交给 Agent 或在提示里使用）：`未执行`——本计划不接入，检索层没有任何生产调用方（全包只被 `bi_agent/api.py` 的启动预检导入）。同理，`runtime/versions.py::VersionSet` 目前只被检索层消费，与现有 `runtime.artifacts.QueryProvenance` **没有**接线（计划 Task 1 明写"不修改现有类"），所以运行版本还不会从 `VersionSet` 派生——那根线属子项目 B。
3. 受控 SQL 探索（子项目 B）：`未执行`。目录 ref 至今没有消费者，`resolve_sql_identifier()` 只被测试调用；本计划不生成、不校验、不执行任何 SQL。
4. `SEMANTIC_CATALOG_ENABLED=true` 在生产/预发布环境的启用与观察：`未执行`。
5. 目标环境（生产/预发布）的迁移核对（001→019）与 `bi_app` / `bi_reader` 授权实测：`未执行`（§4 的数字全部来自本机 `bi_agent_test`）。
6. 真实来源接口、历史回填、增量同步、`reconcile`、`capabilities --apply`：`未执行`。
7. 部署、反向代理 + OIDC 身份边界、Origin、SSE 断线恢复、每小时同步与每日重核任务注册：`未执行`。
8. 备份与一次真实恢复检查：`未执行`。
9. 首位运营用户一店一周试用与问题归档：`未执行`。
10. 逐店与后台账单 / 业务日期对照（`unmeasured` / 逐店 time_basis 登记）：`未执行`。
11. 渠道在售价与库存的真实来源取证：`未执行`——`listing_audit` 与 `inventory` 来源注册表默认为空，真实部署仍只能报 `unsupported`；目录登记这三张视图不代表来源就绪。
12. 拼多多任何形态的接入（连接器、来源登记、支付能力、凭证、onboarding）：永久不接入（2026-09-12 决定），本轮零改动。
13. Task 11 统一发布门禁 4–7 项（真实 provider live、目标环境核对、部署/备份/恢复与一周试用）：**open**，本轮**没有**通过，也不因本文而改变。第 1–3 项状态沿用 [Task 11 日期化发布验收](2026-09-14-task-11-release-acceptance.md)。
14. 依赖新增、数据库迁移、锁文件变更：零（本计划不需要迁移）。

## 10. 范围、红线与本轮改动

- 允许并实际修改：`README.md`、`docs/runbook.md`、`docs/metrics.md`、本文、`docs/superpowers/plans/2026-09-11-data-and-query-closure.md` 的"语义目录 / Schema 检索"索引条目一处。计划里的实现 checkbox **不勾选**（Task 1–4 的完成状态以本文与 Git 历史为准）。
- 未改动：任何 `backend/**` 实现与测试、`frontend/**`、迁移 SQL、`.env*`、`pyproject.toml` / 锁文件、受保护路径。
- 数据库侧只跑本机 `bi_agent_test`：探针全部在显式回滚事务里，验收命令按 runner 自身的 savepoint/rollback 收尾；无写入残留。
- 无 stage、无 commit、无 push（提交由主会话在父级验证后决定）；`git diff --check` 干净。
- 本文的数字只证明"默认关闭的目录与检索在本地正确、且不改变现有行为"。**不宣布发布就绪、不宣布任何平台真实来源就绪、不宣布 Task 11 门禁 4–7 已通过。**

## 11. 建议的下一步（顺序即依赖）

1. 主会话按 §8 复跑并决定是否按单个逻辑提交收口本轮文档（子代理不提交）。
2. 若要复用 §6 的 A/B 证据，先批准把该比较固定为仓库内回归（新增测试工程，属独立决定）。
3. 修 §7.3 的两处陈旧措辞（包 docstring 与 `schema_ambiguous` 说法）与 §7.4 的 Task 11 P2 遗留，放在下一个 diff 里，不与文档验收混提交。
4. 受控 SQL 探索（子项目 B）开工前需要：Task 11 门禁 4–7 的推进或新的所有者决定，以及"目录 ref 的消费方与原因码通道（`needs_input` / `schema_ambiguous`）归属"的明确设计决定。
