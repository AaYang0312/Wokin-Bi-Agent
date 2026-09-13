# 跨平台运营工作流与数据闭环 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**2026-09-12 多来源修订：** 开发主线统一为 main；本计划任务编号保持不变。[原路线图 Task 5/11](2026-09-07-ecommerce-bi-agent.md) 和 [多来源指标设计](../specs/2026-09-12-multi-source-metrics-design.md) 是本计划基础验收及四工作流验收的前置。关闭单保留 inactive、独立认证支付；按平台解析源并求公共覆盖交集；capabilities 真门禁及逐结果 basis 必须进入验收。010–013保留给本计划现有任务，多来源登记如需迁移使用独立014，不形成对未实现010–013的依赖。

**Goal:** 在当前真实名称与确定性查询基础上，交付商品跨店分析、平台 / 店铺图形对比、用户指定价格的上架复核，以及实物 / 店铺库存预警。

**Architecture:** 保留 FastAPI / React、目录引用、固定指标引擎和现有业务图。向薄主 Agent 提供四个业务 Tool，商品与平台对比共用 CommercePerformanceGraph，价审与库存各自使用 ListingPriceAuditGraph / InventoryWatchGraph；授权、数据能力、版本、恢复、结果持久化为共享节点。

**Tech Stack:** Python 3.11+、Pydantic 2、psycopg 3、PostgreSQL、FastAPI、React 19、TypeScript、unittest、Vitest；近期不引入新的 Agent 图框架。

**Spec:** [首位运营用户的跨平台工作流设计](../specs/2026-09-11-operator-workflows-design.md)，为本次修订的权威设计；[早期进度快照](../research/2026-09-11-progress-and-roadmap.md) 只作历史证据。

**修订说明：** 用户确认上架正确价由当次查询指定，利润按已有表项设计。Task 2 主体提交 `0624220` 已落地，保留下面已有勾选及 SKU 遗留；本轮核查期间另一 Agent 正在补 SKU 规格的聚合展示。Task 1 / 3 / 4 扩展到三领域共用底座；Task 5 保留为基础回归；新增 Task 6–11 按首位用户工作流交付。此前“先通用 SQL，再扩库存”的优先级由本计划替代。

## Global Constraints

- 核查基线为 `0624220`；执行前读取最新 HEAD、迁移序号和其他 Agent 的改动，避免覆盖并行工作。
- 005 商品档案、007 目录身份已提交，Task 2 的 SKU 展示收尾仍在并行进行；接收其最终交付，禁止重建第二套商品表 / ref 系统。
- 现有阶段 1/2 已合入，不重复实现或从旧工作树覆盖当前代码。
- 目标范围是淘宝 / 天猫、抖音、京东、快手、视频号、微购相册的已授权店铺；按平台 / 店铺 / 指标逐个取证与启用，抖音只作为现有验证基线，不将店铺档案数当同步完成数。
- **2026-09-12 决定：拼多多不再接入。** 方舟授权成本过高，用户放弃申请；拼多多只保留已核验的 ERP 单据能力（`erp_documents`），支付类指标永久 fail closed，跨平台对比中作为显式缺失组 / `excluded_scope` 出现，不再是待办前置。见 [范围决定](../research/2026-09-12-drop-pdd-onboarding.md)。
- 金额由确定性工具产生，沿用 `[start,end)`、北京时间、支付与退款口径；商品父项与子件不可重复计入销售额。
- 真实业务名称在授权应用展示层解析；模型使用稳定 opaque ref。名称字段不与 ERP 主键混为一类。
- 独立测试库与真实数据环境隔离；缺少集成环境必须标注未执行，不能记为通过。
- Schema / 口径 / 名称变化不覆写历史 Artifact；旧聊天继续可读。
- 商品、店铺、平台、SKU / listing 分层映射；多规格、单位和套件不可靠名称合并。单次图内集合查询多个店铺，不由主 Agent 逐店循环。
- 利润仅使用已存在的行成本 / 单据成本 / ERP 毛利字段；首版展示商品毛利参考和 ERP 毛利参考，缺成本不补 0，不使用当前采购价重算历史，不新增净利润 / ROAS 声明。
- 上架复核目标价取用户当前明确输入并冻结为本次依据；默认不继承上轮目标价，也不要求先建长期价格表。成交均价与上架价分开。
- 实物库存按库存池 / 仓库 / SKU 去重，店铺可售库存单独统计；库存池有独立授权，缺阈值 / 来源 / 时效不判正常。
- 本次仅设计查询 / 复核 / 预警结果，未包含改价、库存调整、采购、广告投放或外部通知动作。
- 新迁移建议为 008 数据能力、009 多领域运行契约、010 渠道映射、011 经营事实视图、012 价审、013 库存；执行时核对实际已占编号。原预留但未创建的 006 不补作旧序号迁移，007 不因本计划改写历史。
- 本计划是待实施交付清单，本次只编写文档，不执行迁移或同步。

## Task 1：数据覆盖验收与版本来源（P0，先交付）

**Files:**
- Create: `backend/bi_agent/data_quality.py`、`backend/tests/test_data_quality.py`。
- Modify: `backend/bi_agent/sync.py`、`backend/bi_agent/metrics.py`、`docs/runbook.md`、`docs/metrics.md`。
- Create: `backend/sql/008_data_readiness.sql`（执行时再次确认并顺延）。

**Interfaces:** 新增 `assess_query_coverage(conn, request: QueryRequest) -> CoverageAssessment` 用于旧查询；新增 `assess_readiness(conn, *, scope, requirements, window, freshness_policy) -> ReadinessAssessment` 供三领域内部调用。后者的 scope 为服务端授权范围，requirements 为指标 / 数据实体 / 口径集合，window 为请求日期或快照时点，freshness_policy 为来源的已批准时效策略。两者结果包含 `requested_window`、`covered_windows`、`missing_windows`、`data_as_of`、`quality_status`、`source_batches`；ReadinessAssessment 额外按平台 / 店铺 / 指标列出可用性、incomparable 和 excluded_scope。不以最后任务成功时间替代业务截止。

- [ ] 在独立测试库增加“有店铺无事实”“窗口有缺口”“成功同步但业务截止未推进”“对账失败”用例，运行后确认旧实现不能通过新增断言。
- [ ] 明确 `quality_ok` 的升级规则和历史数据处理：新增 `unknown/passed/failed` 质量状态及对账时间 / 版本；历史 false 不直接当已发现数据错误。失败范围禁止出数，未知质量明确披露，完成核验后才提升为 passed。
- [ ] 将每个店铺 / 实体 / 窗口的同步批次、覆盖和对账证据持久化；部分分页失败时窗口不推进，金额缺失与真实零分开。
- [ ] 在固定查询调用前返回结构化缺口；冻结原查询窗口，建议的可用窗口只作为建议返回。
- [ ] 更新 runbook，按执行时的授权清单运行历史回填、增量和核对，并记录各组合完成 / 不支持 / 失败及原因。
- [ ] 对五个平台逐项记录订单、成本、上架实际价、实物库存、渠道库存的数据来源；没有实测的接口不填“可用”。先验证现有表项对商品 / 平台毛利的粒度，不要求一次把所有平台强行开通。
- [ ] 新上架商品无交易仍进入目录 / 价审 / 库存目标全集；为每源定义 freshness policy。来源缺失只影响依赖它的指标，输出 partial 且禁止完整跨平台总额。
- [ ] 回归后形成独立提交：`feat: expose verified data coverage and provenance`。

验证命令：

```sh
cd backend
.venv/bin/python -m unittest tests.test_data_quality tests.test_core tests.test_business_query_graph -v
```

关键断言示例（测试数据由新测试模块建立）：

```python
self.assertEqual(assessment.requested_window, ("2026-09-04", "2026-09-11"))
self.assertIn(("2026-09-09", "2026-09-11"), assessment.missing_windows)
self.assertNotEqual(assessment.quality_status, "passed")
```

## Task 2：真实名称、稳定引用和授权展示（P0，主体已实现，保持原交付记录）

**Files:**
- Create: `backend/bi_agent/catalog/models.py`、`backend/bi_agent/catalog/repository.py`、`backend/tests/test_catalog.py`。
- Reuse: 并行任务的 `backend/sql/005_product_dimension.sql` 与 `sync_products()`；Create: `backend/sql/007_catalog_identity.sql`（只扩展缺少的引用 / SKU / 历史名称，执行时重新确认序号）。
- Modify: `backend/bi_agent/sync.py`、`backend/bi_agent/metrics.py`、`backend/bi_agent/business_query/tool.py`、`backend/bi_agent/runtime/models.py`、`backend/bi_agent/chats.py`、`backend/bi_agent/agent.py`。
- Modify: `frontend/src/components/ArtifactView.tsx`、`frontend/src/types.ts`；Create: `frontend/src/components/ArtifactView.test.tsx`。

**Interfaces:** `EntityRef` 包含 `kind: shop|product|sku`、`ref`、`catalog_version`。`resolve_display_entities(conn, subject_id, refs) -> list[DisplayEntity]` 返回授权范围内的 `ref/display_name/sku_label/name_source`。模型投影只返回引用，展示投影在应用内附名称；旧匿名 Artifact 保持兼容。

- [ ] 建立改名、同名商品、不同店铺、多个 SKU、名称缺失、超 26 个商品、排名变化、越权名称读取的失败用例。已覆盖：改名、同名（店铺/商品）、名称缺失、30 商品、重排稳定性、越权读取。**未覆盖：多个 SKU**（见下方遗留）。
- [ ] 订单行新增成交名称 / SKU 文本快照；建立商品和 SKU 主档、店铺平台关系、名称来源与有效时间，主键必须区分平台与账号范围。已完成：`product_name_snapshot` / `sku_label_snapshot` 落 `bi.order_items`、商品主档 `bi.products`、店铺平台关系与 `source_modified_at` 版本保护。**未完成：SKU 主档**。
- [x] 从订单行先补可用名称，再同步获准主档并标注来源。历史订单未带名称且主档不存在时展示“名称未取得”与稳定引用，禁止编造。取用优先级单点实现于 `catalog.pick_display_name`（档案 > 成交快照 > 未取得）。
- [x] 使用持久化 opaque ref 替换每次查询重新编号的商品别名；将引用作为下一轮实体筛选的可信输入，重新检查用户授权。`bi.entity_refs` 落表 + `ref_for_key` 同源派生；`_resolve_shops` 只接受已登记引用，未识别值标记 `invalid_shop` 且不进入授权节点。
- [x] 在聊天保存 / 读取投影与前端卡片接入真实展示名；扩展明确字段白名单，保持模型与展示投影分别校验。`ARTIFACT_RESULT_COLUMNS` 与模型投影分别校验，前端 `ArtifactView` 渲染真实名并保留“名称未取得”占位。
- [x] 历史重放补名称前后比较相同范围支付额、销量和父项聚合，数值应不变；测试完成后独立提交：`feat: resolve real catalog names with stable references`。见 `tests/test_db.py::test_archive_name_join_leaves_totals_untouched`。

**Task 2 遗留（SKU 维度，保留历史验收点）：** 初次提交时 `EntityKind.SKU` 与 `DisplayEntity.sku_label` 已就位，前端也会渲染规格，但尚未形成 SKU 主档 / 粒度。修订期间已看到 `pick_sku_label` 和 007 视图规格列的并行收尾改动，具体完成状态以该 Agent 最终测试与提交为准。补名称不得改变 `(shop, day, product, line_kind)` 聚合粒度；多个不同规格只显示商品名，不能任选其一。展示规格不等于已有渠道 SKU 映射，后者明确由新增 Task 6 承接。

```sh
cd backend
.venv/bin/python -m unittest tests.test_catalog tests.test_core tests.test_runtime tests.test_business_query_graph -v
cd ../frontend
npm test -- src/components/ArtifactView.test.tsx
npm run build
```

核心断言：

```python
self.assertEqual(first_ref, reranked_ref)
self.assertEqual(len(set(refs_for_30_products)), 30)
self.assertNotIn("真实商品名称", model_payload_json)
self.assertIn("真实商品名称", authorized_artifact_json)
self.assertEqual(unauthorized_display_entities, [])
```

## Task 3：版本化、多领域运行契约（P1，依赖 Task 1/2）

**Files:**
- Modify: `backend/bi_agent/runtime/models.py`、`runtime/repository.py`、`runtime/memory.py`、`business_query/state.py`、`business_query/nodes.py`。
- Create: `backend/sql/009_query_provenance.sql`（执行时重新确认序号）、`backend/bi_agent/runtime/domain_registry.py`、`backend/bi_agent/runtime/artifacts.py`。
- Modify: `backend/tests/test_runtime.py`、`backend/tests/test_runtime_db.py`、`backend/tests/test_business_query_graph.py`。

**Interfaces:** 新增 `QueryProvenance`：`template_id/template_version/metric_version/schema_version/catalog_version/mapping_version/policy_version/graph_version/source_batches/data_as_of`；新增 `RequestIdentity`：`root_request_id/request_fingerprint/attempt_no/recovery_count/termination_reason`。现有 `revision` 只表示状态推进。领域注册表登记 `business_query/commerce_performance/listing_price_audit/inventory_watch` 的状态模型、节点与 payload 白名单；`DomainResult` v2 为本设计第 3 节的字段集合，旧 v1 兼容。

共享 `DomainContext` 定义在 `runtime/models.py` 的进程内 dataclass：`subject_id`、`chat_id`、`user_message_id`、`root_request_id`、`allowed_shop_ids`、`allowed_inventory_pool_ids`、`conn`、`store`、`now`、`deadline`；连接、原始授权 ID 不进入序列化状态。后续各图入口消费此结构，不再各自从模型参数解析身份。

- [ ] 测试同一 state revision 对应不同数据批次、数据更新后缓存不复用、旧 Artifact 读取和非法字段写入。
- [ ] 固定查询记录模板 ID / 版本；需要执行诊断的 SQL 与参数进入受控记录，通过引用关联，不写入模型消息或普通事件文本。
- [ ] 扩展状态、事件、Artifact 的独立白名单和两类 Store，保证规范化请求与状态一致且 CAS 仍生效。
- [ ] 迁移 004 中仅允许 business_query / metric_result 的 CHECK 约束，增加领域与六类 Artifact 类型；未知领域 / 任意错误文本仍拒绝，不能直接移除约束。
- [ ] 用“新领域写入 → 事件推进 → 表格 / 图表引用持久化 → 旧聊天读取”集成用例验证 schema_version / 判别联合；图表和数据集必须同版本，目录版本可供模型使用 opaque 版本号但真实名称仍不进入模型投影。
- [ ] 请求指纹包括授权范围、规范化参数、查询 / 数据版本；数据版本变化后不得命中旧结果。
- [ ] 说明复现等级：当前至少可重现已存 Artifact；没有版本化事实库时不承诺任意历史 SQL 重跑一致。测试后提交：`feat: persist query versions and recovery identity`。

```sh
cd backend
.venv/bin/python -m unittest tests.test_runtime tests.test_business_query_graph -v
uv run --env-file ../.env.test python -m unittest tests.test_runtime_db -v
```

## Task 4：确定性恢复与回答兜底（P1，依赖 Task 3）

**Files:**
- Create: `backend/bi_agent/business_query/recovery.py`、`backend/bi_agent/response_summary.py`、`backend/tests/test_recovery.py`。
- Modify: `backend/bi_agent/agent.py`、`backend/bi_agent/business_query/graph.py`、`business_query/nodes.py`、`runtime/models.py`、`backend/tests/test_core.py`、`backend/tests/test_api.py`。

**Interfaces:** `decide_recovery(error, coverage, request_identity, remaining_seconds) -> RecoveryDecision`；`render_result_summary(domain_result: DomainResult) -> str`。决策包含 `action`、`reason_code`、`max_additional_attempts`、`suggested_window`，不能包含未获确认的替代原请求。

| 条件 | 确定性处理 | 自动追加次数 |
| --- | --- | --- |
| 缺少参数 / 实体有歧义 | 询问缺少字段，不执行 SQL | 0 |
| 参数非法 | 返回安全字段错误，允许模型修正 | 整轮最多 1 |
| 缺数据覆盖 | 返回原窗口、缺口、共同截止、建议窗口 | 原请求重复查询 0 |
| 无权限 / 未支持指标 | 返回明确原因，终止 | 0 |
| 已识别临时连接故障 | 有足够预算且事务已回滚时重试 | 最多 1 |
| SQL 超时 / 总预算耗尽 | 终止，给范围建议 / 已有结果 | 0 |
| Artifact 持久化失败 | 不发布成功结果 | 0 |
| 相同请求、同一数据版本已有结果 | 复用 Artifact 引用 | 新 SQL 0 |

- [ ] 添加表中每个分支的失败测试，重点证明重复缺覆盖不会耗尽工具次数，也不会缩短用户日期。
- [ ] 在指标与图边界将可识别异常映射到固定错误码；未知错误保持 unavailable，禁止因猜测错误类型自动重试。
- [ ] 子图执行恢复决策；主 Agent 消费结构化 action，不从错误文案决定如何修复。共享整轮 deadline，任何重试不重置预算。
- [ ] 保留现有纯文本补答；补答失败或预算不足时使用基于 Artifact 的确定性摘要，包含指标、窗口、截止和限制，不重新计算金额。
- [ ] API 的错误文案消费具体终止原因，避免把查询预算或数据缺口一律显示成模型不可用。测试后提交：`feat: recover queries with typed bounded decisions`。
- [ ] 将价审 stale / missing_standard、库存 unconfigured / stale、经营 incomparable 纳入领域判定；分清业务异常（售价不一致、库存低）与系统错误（连接失败），业务异常不能触发重试。
- [ ] 必需数据 Artifact 写入失败禁止成功；可选图表失败可以保留已经保存的表格。所有来源共用整轮 deadline，跨店批量查询不额外获得预算。

```sh
cd backend
.venv/bin/python -m unittest tests.test_recovery tests.test_core tests.test_runtime tests.test_business_query_graph -v
uv run --env-file ../.env.test python -m unittest tests.test_api -v
```

核心断言：

```python
self.assertEqual(query.call_count, 1)  # 原请求缺覆盖后不循环执行
self.assertEqual(final_result.filters["end"], "2026-09-11")
self.assertIn("数据覆盖不足", final_text)
self.assertEqual(retry_deadlines, [original_deadline, original_deadline])
```

## Task 5：基础闭环端到端验收（保留，四类工作流最终验收见 Task 11）

**新增前置：** 先完成原路线图 Task 5 的来源注册表、指标能力、时间覆盖、退款披露及 basis 全链路，使用修订后的26题验收。旧20题通过或真实模型14/20仅是历史基线，不代表多来源可用。

**Files:** Modify `backend/tests/acceptance.py`、`docs/runbook.md`、`docs/metrics.md`、`docs/demo.md`；新增验收记录到 `docs/superpowers/research/`。

- [ ] 先验 tb/tm 出库源、fxg 交易源、pdd 无支付能力、混合口径分列、multirange孔洞、未匹配退款可答披露、关闭单100/30/70与补拉收敛、时间语义未认证。
- [ ] 先在合成库验收：真实名称、歧义澄清、改名追问、近期缺数据、零业务、停用 / 越权、超时、补答失败、重复请求、持久化失败。
- [ ] 执行现有全量离线套件及新增用例，确认失败和跳过数；测试账号不能使用真实业务库。
- [ ] 再针对固定店铺 / 窗口核对源报表与结果；支付额按 Decimal 精确比较，计数一致，差异必须有记录，不能用统一容差掩盖缺失字段。
- [ ] 用已配置真实模型单独运行验收，记录模型版本、数据版本、问题、正确率、澄清率、平均调用次数、耗时和失败分类；不把离线替身结果称为真实模型通过。
- [ ] 更新运行手册中实际 provider 联调状态、当前工作目录、迁移和数据覆盖清单；将 P0/P1 验收结果提交归档。

```sh
cd backend
.venv/bin/python -m unittest tests.test_core tests.test_runtime tests.test_business_query_graph tests.test_catalog tests.test_data_quality tests.test_recovery -v
uv run --env-file ../.env.test python -m unittest tests.test_db tests.test_api tests.test_runtime_db -v
uv run --env-file ../.env.test python -m tests.acceptance --offline
cd ../frontend
npm test
npm run build
```

## Task 6：跨渠道商品 / SKU 映射与名称查询（P0，接 Task 2）

**Files:** Create `backend/bi_agent/catalog/channel_mapping.py`、`catalog/resolver.py`、`backend/sql/010_channel_catalog.sql`、`backend/tests/test_channel_mapping.py`；Modify `catalog/models.py`、`catalog/projection.py`、`backend/bi_agent/sync.py`。

**Interfaces:** `resolve_product(conn, *, selector, authorized_scope, at) -> ProductResolution`；selector 为 spec 第 3 节的 ref / text + sku_refs，结果 status=`resolved|ambiguous|unresolved`，携带 product_ref、sku_refs、mapping_version 与可展示候选。`expand_channel_items(conn, *, product_ref, scope, at) -> ChannelItemSet` 返回获准渠道 listing / SKU 引用和映射状态，不从订单历史推导上架全集。

- [x] 添加同名异物、多规格、多件装、改名、跨账号同号、新品无成交和未知 ref 的失败用例；先运行下方新模块确认红灯。
- [x] 建立 source namespace / company / platform / shop / listing / platform_sku 到 ERP 商品 / SKU 的有效期映射，保留 approved / ambiguous / unresolved；平台商品名只能辅助候选，不能决定合并。
- [x] 复用已发布 ent 引用并增加版本化映射，不能批量重算旧引用；碰撞时拒绝映射或显式迁移，兼容旧 Artifact。
- [x] 新增获准 reporting 映射视图，让 bi_app 完成实体查询与按引用筛选；始终以授权店铺 / 库存池过滤，不开放整张底层主档。
- [x] 增加单商品 / SKU 筛选和跟随查询的安全参数；无历史成交的 SKU 仍可解析，用于价审与库存。SKU 主档字段由已核验来源填充。
- [x] 回归后提交 `feat: map products and skus across sales channels`。

```sh
cd backend
.venv/bin/python -m unittest tests.test_channel_mapping tests.test_catalog -v
```

验收数据：同名“接头”的 6mm / 8mm 必须返回两个候选；同一 ERP SKU 映射到淘宝、抖音两个链接可归为同 SKU；另一账号相同数字 ID 不归并；无成交的已映射 listing 必须保留。自动合并仅接受已确认的显式标识映射。


> **2026-09-12 执行说明**：迁移编号由 010 顺延为 `016_channel_catalog.sql`——主线已按时间顺序应用到 015（Task 5 多来源契约与血缘版本），保留 010 会让「编号 = 应用顺序」失效。
> 渠道**上架**链接全集仍需已核验来源（渠道接口 / 官方导出）：本轮映射行只能来自显式标识映射（`manual_map` / `channel_api` / `import`）与成交行的 ERP 身份（`trade_line`，`listing_id` 留空）。`bi.skus` 主档未建：没有已核验来源时凭空建表只会让“已接入”看起来比实际更早，规格文本继续取自成交快照与 `pick_sku_label` 的单点规则。
## Task 7：现有表项经营指标与商品运营图（P1，依赖 Task 1/3/4/6）

**Files:** Create `backend/bi_agent/commerce/models.py`、`commerce/metrics.py`、`commerce/repository.py`、`commerce/graph.py`、`commerce/tool.py`、`backend/sql/011_commerce_views.sql`、`backend/tests/test_commerce.py`；Modify `backend/bi_agent/agent.py`、`business_query/tool.py`、`docs/metrics.md`。

**Interfaces:** `analyze_product_performance(request: ProductPerformanceRequest, context: DomainContext) -> DomainResult`。ProductPerformanceRequest 使用 spec 第 4 节该 Tool 的字段；DomainContext 包含服务端身份、授权范围、连接、Store、now、deadline 和 root_request_id，由 Task 3 的运行契约提供。内部 `run_commerce_graph(report_kind, request, context)` 的 report_kind=`product|comparison`；`compute_reference_metrics(lines: list[dict[str, object]]) -> dict[str, str | None]` 接受已验证的同币种 / 单位普通销售行，不承担数据库读取。

- [ ] 先写指定商品、多店、七日缺口、成本 null、混合赠品 / 套件、退款跨期和 JOIN 放大测试，确认失败。
- [ ] 从现有 order_items 生成受限 sales / cost 视图，延用父项与金额分摊规则；商品毛利参考只用已核验行 raw_unit_cost × quantity。成本覆盖不全时完整毛利 null，不拿已知成本子集冒充整体。
- [ ] 平台 / 店铺 ERP 毛利参考从 orders.raw_gross_profit 按唯一 ERP 单据聚合，检查拆合单语义与覆盖；与商品毛利分面，不能订单 JOIN 多行后 SUM 或按销售比例分摊商品毛利。
- [ ] 为新接口选择 `erp_effective_parent` 或 `verified_payment` 事实口径，明确限制：商业支付可含关闭已付，商品有效父项可能不含，旧 paid_amount 与 quantity 的语义保持不变。
- [ ] 执行 spec 第 6 节经营图；冻结批次与数据库读取快照，一次集合查询返回跨店合计和七日序列；多指标能力独立判断，不要求所有指标同时可算才给已有销量。
- [ ] 生成销售份额 / 趋势 / 毛利参考候选；低利润阈值或最小样本未配置时仅排序，不自动推导投放回报或预算。
- [ ] 注册商品 Tool，旧 query_business 通过适配器保持原契约；回归后提交 `feat: analyze product performance with verified cost references`。

核心纯函数测试（合成普通销售行，金额字符串）：

```python
import unittest
from bi_agent.commerce.metrics import compute_reference_metrics

class ReferenceMetricTests(unittest.TestCase):
    def test_weighted_price_and_existing_line_cost(self):
        result = compute_reference_metrics([
            {"quantity": "1", "allocated_paid_amount": "100", "raw_unit_cost": "40"},
            {"quantity": "9", "allocated_paid_amount": "450", "raw_unit_cost": "30"},
        ])
        self.assertEqual(result["weighted_avg_paid_price"], "55")
        self.assertEqual(result["product_gross_profit_reference"], "240")

    def test_missing_cost_cannot_produce_complete_profit(self):
        result = compute_reference_metrics([
            {"quantity": "1", "allocated_paid_amount": "100", "raw_unit_cost": None},
        ])
        self.assertEqual(result["sales_amount"], "100")
        self.assertIsNone(result["product_gross_profit_reference"])
```

```sh
cd backend
.venv/bin/python -m unittest tests.test_commerce tests.test_core tests.test_business_query_graph -v
```

## Task 8：平台 / 店铺比较 Tool 与图表（P1，复用 Task 7）

**Files:** Modify `commerce/models.py`、`commerce/graph.py`、`commerce/tool.py`、`agent.py`；Create `backend/bi_agent/presentation/charts.py`、`backend/tests/test_comparison.py`、`frontend/src/components/ChartArtifact.tsx`、`frontend/src/components/ChartArtifact.test.tsx`；Modify `frontend/src/components/ArtifactView.tsx`、`frontend/src/types.ts`。

**Interfaces:** `compare_performance(request: PerformanceComparisonRequest, context: DomainContext) -> DomainResult`，字段见 spec Tool 目录，shop 分组必须选定一个平台。`build_chart_spec(dataset_ref, *, kind, x, y, series, unit, metric_basis, coverage_ref) -> ChartSpec` 只返回 spec 第 8 节的声明式契约。

- [ ] 先写五平台含一缺失、一口径不一致场景、平台下钻和图表数据引用失配测试。
- [ ] 平台 / 店铺聚合复用经营图，统一指标定义；淘宝和天猫分组必须服从显式规则。总量只能针对完整可比集合，缺失不画成 0。
- [ ] 生成 comparison_table 与 chart_spec，图表只引用已保存数据；每指标独立轴，条形图从 0 开始，趋势缺口断开。
- [ ] 前端实现柱状 / 折线与表格切换，平台点击下钻保留原时间与口径，并重新走授权。使用已有依赖或在实现时选一种轻量绘图库，不让模型生成可执行代码。
- [ ] 验证 5 平台总览与单平台多店对比的数字、单位、不可用原因、图表与表格一致；提交 `feat: compare platform and shop performance with charts`。

```sh
cd backend
.venv/bin/python -m unittest tests.test_comparison tests.test_commerce -v
cd ../frontend
npm test -- src/components/ChartArtifact.test.tsx src/components/ArtifactView.test.tsx
npm run build
```

## Task 9：用户指定目标价的上架复核（P1，可与库存并行）

**Files:** Create `backend/bi_agent/listing_audit/models.py`、`listing_audit/repository.py`、`listing_audit/rules.py`、`listing_audit/graph.py`、`listing_audit/tool.py`、`backend/sql/012_listing_audit.sql`、`backend/tests/test_listing_audit.py`；Modify `agent.py`、`runtime/domain_registry.py`、前端 `ArtifactView.tsx`。

**Interfaces:** `audit_listing_prices(request: ListingPriceAuditRequest, context: DomainContext) -> DomainResult`；字段见 spec 第 4 节。`compare_price(actual: str | None, expected: str | None) -> str` 仅在币种、规格、时点、完整性已通过校验后使用，返回 match / mismatch / missing_standard / unknown；外层 graph 负责 not_listed / not_on_sale / stale 等状态。

- [ ] 先写当次目标价缺失、上一轮价不可隐式继承、多 SKU / 多链接、缺一家、币种不一致、过期与缺完整枚举证据的失败用例。
- [ ] 定义渠道在售快照适配接口；确认实际 listing / SKU 售价、上架状态、抓取时间、快照完成标志。没有已核验快麦方法时走获准平台 API 或带来源 / 时点声明的官方导出，不用 ERP 建议价替代。
- [ ] 新建 listing_snapshots / snapshot_items、price_audit_expectations；expected roster 从用户选定的授权店铺集合与商品 SKU 展开。本次用户明确目标价冻结入审计；缺价先询问，不建设强制长期价格表。
- [ ] 按 graph 固定节点执行完整 roster 左连接；不存在记录只有具备完整性证据时判 not_listed。实价不同为业务发现，不重试到匹配为止。
- [ ] 发布差异表：店铺、链接 / SKU、目标价、实际价、差额、状态、时间；全部通过要求所有期望项都有有效匹配证据。提交 `feat: audit listing prices against user supplied targets`。

```python
import unittest
from bi_agent.listing_audit.rules import compare_price

class ListingPriceRuleTests(unittest.TestCase):
    def test_exact_price_and_missing_target(self):
        self.assertEqual(compare_price("19.90", "19.9"), "match")
        self.assertEqual(compare_price("29.90", "19.90"), "mismatch")
        self.assertEqual(compare_price("19.90", None), "missing_standard")
        self.assertEqual(compare_price(None, "19.90"), "unknown")
```

```sh
cd backend
.venv/bin/python -m unittest tests.test_listing_audit tests.test_runtime -v
```

来源门禁：合成 / 导入验收可先完成，但未取得渠道真实售价和时点证据前，Tool 只能报告 unsupported / 缺来源，不能声称线上全店复核可用。

## Task 10：实物库存与店铺库存的两级预警（P1，依赖 Task 1/3/4/6）

**Files:** Create `backend/bi_agent/inventory/models.py`、`inventory/repository.py`、`inventory/rules.py`、`inventory/graph.py`、`inventory/tool.py`、`backend/sql/013_inventory_snapshots.sql`、`backend/tests/test_inventory.py`；Modify `agent.py`、`runtime/domain_registry.py`、前端 `ArtifactView.tsx`。

**Interfaces:** `inspect_inventory(request: InventoryInspectionRequest, context: DomainContext) -> DomainResult`；字段见 spec Tool 目录。`sum_unique_physical_stock(rows: list[dict[str, object]]) -> str` 接受相同批次 / SKU / 单位且已授权的 physical pool 行，以 pool_ref / warehouse_ref / sku_ref 去重；同键不同数量应拒绝为冲突，不能任选一个。`classify_inventory(quantity: str | None, threshold: str | None, *, fresh: bool) -> str` 返回 low / normal / unconfigured / unknown / stale / data_anomaly。

- [ ] 先写三店共用一库存池、渠道 0 / 实物充足、总量低、负库存、缺阈值、多单位套件、过期、无成交商品和扫描分页漏项测试。
- [ ] 接入已验证 ERP SKU / 仓库库存；渠道可售单独取源。建立库存池与店铺连接方式 shared / allocated / independent，独立验证库存池授权。
- [ ] 存储 physical / channel 两类快照与完整批次；不混批次合计，不把渠道显示数求和成实物总量。库存字段的可用 / 锁定 / 在途语义逐个核验。
- [ ] 按 SKU / 店铺 / 库存池配置版本化阈值，等于阈值也预警；无配置显示 unconfigured。全商品检查先完整扫描再截取展示，高风险不能因先 Top N 商品而漏检。
- [ ] 产出补货候选和店铺配额调整候选、各自原因和快照时间；不执行调整、采购或外部通知。提交 `feat: inspect physical and channel stock with scoped alerts`。

```python
import unittest
from bi_agent.inventory.rules import sum_unique_physical_stock, classify_inventory

class InventoryRuleTests(unittest.TestCase):
    def test_shared_stock_is_not_counted_once_per_shop(self):
        row = {"pool_ref": "pool-a", "warehouse_ref": "wh-a", "sku_ref": "sku-a",
               "batch_id": "batch-1", "available_quantity": "100", "unit": "piece"}
        self.assertEqual(sum_unique_physical_stock([dict(row), dict(row), dict(row)]), "100")

    def test_threshold_boundary_and_missing_evidence(self):
        self.assertEqual(classify_inventory("10", "10", fresh=True), "low")
        self.assertEqual(classify_inventory("100", None, fresh=True), "unconfigured")
        self.assertEqual(classify_inventory(None, "10", fresh=True), "unknown")
        self.assertEqual(classify_inventory("100", "10", fresh=False), "stale")
```

```sh
cd backend
.venv/bin/python -m unittest tests.test_inventory tests.test_runtime -v
```

## Task 11：四工作流验收与首次用户交付

**验收口径补充：** “五平台对比”必须逐店/逐结果标 basis 和能力缺口；fxg平台支付与tb/tm出库不得合为同口径总额或排名；pdd 按 2026-09-12 决定不计支付（不接入，非延后），成本/毛利与实耗门槛不变。跨领域 Artifact 继承来源、时间口径和能力版本，旧结果不得在换源后复用。先通过修订后的Q08/Q15与Q21–26，再执行原11个运营场景；逐店真实来源取证另记。

**Files:** Modify `backend/tests/acceptance.py`、`backend/tests/questions.jsonl`、`docs/metrics.md`、`docs/runbook.md`、`docs/demo.md`；Create `backend/tests/test_operator_workflows.py` 与日期化验收报告。

- [ ] 把 spec 第 10 节 11 个验收场景全部实现为集成测试；每领域包含正常、部分来源、无权限和持久化失败四类路径。
- [ ] 用户问题到 Tool 路由验收：“A 商品所有店铺近七天”“五平台对比”“抖音各店比较”“A 的该 SKU 全店标价 19.90 是否正确”“全商品总库存和店铺预警”。上架目标价必须来自当前输入；利润返回字段来源与参考口径。
- [ ] 每个启用平台 / 来源在独立验收报告中记录账号范围、覆盖期、成本质量、售价时点、库存批次和匹配覆盖；缺证据明确禁用该能力，不能把合成通过视为真实就绪。
- [ ] 验证主 Agent 每个完整业务请求通常只需一次业务 Tool；请求数量不随店铺数量线性增长。30 秒内无法完成时明确未完成范围，不偷偷裁剪 / 修改时间。
- [ ] 图表与表格逐项相等；price_audit 和 inventory_alerts 包含期望项数 / 已评估数与未知原因；全部正确或全部安全只有完整证据才允许。
- [ ] 真实模型验收单列版本、调用数和错误分类，发布前回归旧 query_business / evaluate_promotion / 聊天 SSE；提交 `test: verify first operator workflows end to end`。

```sh
cd backend
.venv/bin/python -m unittest tests.test_channel_mapping tests.test_commerce tests.test_comparison tests.test_listing_audit tests.test_inventory tests.test_operator_workflows -v
uv run --env-file ../.env.test python -m unittest tests.test_db tests.test_api tests.test_runtime_db -v
uv run --env-file ../.env.test python -m tests.acceptance --offline
cd ../frontend
npm test
npm run build
```

## 实施顺序与可并行边界

1. **接收 Task 2 收尾。** 完成现有 SKU 规格展示及回归；不把更大的渠道 SKU 主档塞回已交付名称任务。
2. **Task 1 + Task 6。** 一条线核验多平台 / 成本 / 价格 / 库存来源，另一条线完成商品与渠道映射；共享 sync.py 由单一集成人维护。同步推进 Task 3 的契约设计，落地依赖两者字段。
3. **Task 3 → Task 4 → 原路线图多来源Task 5 → 本计划Task 5。** 多领域状态、版本、Artifact 与恢复基础闭环先可测试。
4. **Task 7 → Task 8。** 先交付商品跨店经营分析，再以相同图完成平台 / 店铺比较和图形下钻。可先启用已核验平台，其余平台始终保留能力缺口标签。
5. **Task 9 与 Task 10。** 价格与库存各自按来源就绪并行推进；只读来源探针 / 导入契约可在第 2 步提前做，不等待经营图完成。
6. **Task 11。** 四类业务验收；逐能力发布，防止等待所有接口而没有任何可用工作流。

## 后置子项目

- **语义目录 / Schema 检索：** Task 1/6/7 已先记录实体、指标和关系元数据；下一阶段才增加候选检索与 Schema 选择，评测所需视图召回与 JOIN 粒度。
- **受控 SQL 探索：** 只承接固定业务模板无法表达的新增问题，不作为这四类工作流前置；继续使用 AST / 只读权限 / 预算 / 有限修复。
- **学习型记忆：** 只有 approved 且版本匹配的规范化查询进入 few-shot，用户当次目标价不得自动提升为长期标准。
- **隔离分析 Agent：** 用已持久化且版本一致的数据集做额外经营分析；现有趋势、排名、价差和阈值判断全部由确定性代码完成。
- **持续库存通知：** 若后续产品需要定时通知，复用 InventoryWatchGraph，增加事件去重、解除 / 再触发、通知渠道与调度；当前不创建自动化。
