# 店铺管理者日常工作流离线测试记录（2026-10-01）

## 1. 目的与范围

以"店铺管理者一个工作日的例行提问"为脚本，对四张**已启用**的领域状态图做一次端到端测试，核对：
每张图的完整节点链、五种领域终态（`succeeded / partial / missing_data / needs_input / failed`）、
Artifact 落盘与 SSE 事件顺序，以及真实同步数据下的各类边界行为。

不在范围内：受控 SQL 探索与隔离分析两张默认关闭的图（门禁未开，本轮不启用）；
`evaluate_promotion` 是纯函数工具、不产生运行记录，仅作为工作流完整性对照。

## 2. 环境与边界声明

| 项 | 值 |
| --- | --- |
| 数据库 | 本机隔离测试库 `bi_agent_test`（`BI_TEST_ADMIN_DSN`，真实同步数据至 2026-10-01 18:35，orders 92,904 行） |
| API | `create_app(settings, ScriptedModel)` 隔离实例，`127.0.0.1:8001`，`environment=development`（主体 `local-development`） |
| 模型 | **离线脚本化桩模型**（与 `tests.acceptance --offline` 同一模式：预制工具调用，服务端合并/授权/查询/状态图/持久化全部真实执行）。未调用真实模型 |
| 前端 | 操作者已有 Vite 开发页在 5175 运行，代理目标 8001 空闲；本轮临时占用 8001，测试后释放 |
| 授权范围 | ERP 同步覆盖的 20 家店 + 拼多多 166857/166860（负向边界用）；166853（disabled pdd）刻意留在授权集外作越权探针；探针店 `SF79F91`（capabilities 为空）不进授权集 |
| 会话 | main=`0d4e5d9f-dce1-49bd-8cb1-21552179e00d`，fresh=`77f5bc0a-f7e8-421e-baa2-f2748e4a792d`（留在测试库供复核） |

未执行：真实模型调用、真实平台 API / 同步 / 回填、生产或预发布环境的任何操作、前端构建门禁。

## 3. 问题集与执行结果（18 问，2026-10-01 模拟晨会→午间）

| # | 问题（口语原文） | 领域 / 工具 | 终态 | 事件数 | Artifact |
| --- | --- | --- | --- | --- | --- |
| W01 | 所有店铺最近7天的支付金额，按店铺分列 | business_query / query_business | **failed**（`unavailable`，见 §5.1 缺陷） | 5 | 无 |
| W02 | 元发工具最近7天每天的支付金额走势 | business_query | **succeeded** | 7 | metric_result |
| W03 | 元发工具最近7天支付金额比前7天怎么样？ | business_query | **succeeded** | 7 | metric_result |
| W04 | 沃金五金机电商城3月1日至3月10日支付金额 | business_query | **missing_data**（覆盖自 3月2日起且 3月3-5日有洞，带建议窗口） | 7 | metric_result |
| W05 | 沃金五金机电商城2025年12月1日至31日支付金额 | business_query | **missing_data**（完全无覆盖，不按零出数） | 7 | metric_result |
| W06a | 支付金额是多少？（新会话，无期间） | business_query | **needs_input**（缺期间 → 反问） | 3 | 无 |
| W06b | 就最近7天 | business_query | **succeeded**（补期间后成功） | 7 | metric_result |
| W07 | 钉枪最近七天卖得怎么样？ | commerce_performance / analyze_product_performance | **needs_input**（多候选反问，不代选） | 3 | 无 |
| W08 | 保温钉枪-元发在所有店铺近七天卖得怎么样？ | commerce_performance | **partial**（跨店报告+七日趋势，含排除明细） | 12 | metric_result+trend_series |
| W09 | 各平台近七天支付口径的销售额对比 | commerce_performance / compare_performance | **partial**（fxg 出数，未认证平台 data_missing 不补零） | 12 | comparison_table |
| W10 | 淘宝各店铺近七天支付金额比较 | commerce_performance | **missing_data**（出库单缺 consigned_at → 完整性门禁 fail-closed，见 §5.2） | 5 | 无 |
| W11 | 保温钉枪-元发的标价是25.80，各店都挂对了吗？ | listing_price_audit | **missing_data**（`listing_source_unverified`，快照存在但来源未核验） | 11 | price_audit |
| W12 | 看一下全商品总库存和店铺可售库存 | inventory_watch | **missing_data**（快照表 0 行，`inventory_source_unverified`） | 13 | inventory_alerts |
| W13 | 假设10月销售额10万元、推广费用率12%，最多花多少？ | evaluate_promotion（非状态图） | ok（`spend_cap=12000.00`，basis=用户输入假设） | — | 公开 artifact |
| W14 | 最近7天实际推广费率和ROAS | evaluate_promotion | missing_data（无实耗来源，拒答不编造） | — | 公开 artifact |
| W15 | 拼多多店铺最近7天支付金额 | business_query | **missing_data**（`capability_unavailable`，PDD 永久无支付口径） | 7 | metric_result |
| W16 | 帮我查一下宝威德官方旗舰店最近7天支付金额 | business_query | **failed**（`forbidden`，authorize_scope 拒绝，禁止重试） | 4 | 无 |
| W17 | 元发钉枪五金工具最近7天支付金额（新会话） | 主层同名店澄清拦截 | 不产生运行记录（fxg 与 kuaishou 各有一家"元发钉枪"） | — | — |
| W18 | 各店铺近七天保温钉枪-元发的销量排行 | business_query（partitioned） | **succeeded**（按口径分区 Top-N） | 7 | metric_result |

驱动器断言 **19/19 通过**：每条运行的事件链（连续去重后）均为所属领域合法节点顺序的子序列，
终态与领域符合预期，`run.status` 与最后事件状态一致。

## 4. 状态图覆盖矩阵

| 领域状态图（合法节点数） | succeeded | partial | missing_data | needs_input | failed |
| --- | --- | --- | --- | --- | --- |
| business_query（8） | W02/W03/W06b/W18 全链 | （唯一来源是分区排行 ok+partial，本轮分区完整未触发） | W04/W05/W15 全链 | W06a（validate 处截断） | W16（authorize 处截断）、W01（缺陷） |
| commerce_performance（12） | — | W08/W09 全链 | W10（check 门禁截断） | W07（resolve_product 截断） | — |
| listing_price_audit（11） | — | — | W11 全链 | — | — |
| inventory_watch（12） | — | — | W12 全链 | — | — |

说明：`business_query` 的 PARTIAL 终态按 `nodes._classify` 注释只能来自分区商品排行的
`ok+partial`；本轮分区范围覆盖完整所以落在 succeeded。commerce 图的 succeeded 本轮未单独
出现（W08/W09 均因真实数据中的排除项落 partial，属于合规输出而非失败）。

## 5. 发现

### 5.1 P1 缺陷：S3 披露码 `closed_refunded_excluded` 不在持久化状态白名单，真实数据上成功支付查询整体崩溃

**现象**：W01（也是管理者最自然的日常问题"所有店铺最近7天支付金额"）在 `classify_result`
节点崩溃：`BusinessQueryState.model_copy()` 触发 `unsafe_persistence_payload` 校验失败，图把
运行按 best-effort 收尾为 `FAILED/unavailable`，整轮回答以"本轮回答未完成，请稍后重试"报错。
服务端堆栈证据已留档（本轮 `server_trace.log`，1 次记录）。

**根因**（逐层核实）：

1. `business_query/nodes.py:550-552` 的 `_limitation_codes()` 把 S3 口径披露文本
   （"支付口径已剔除关闭后退款单的已核验支付 N 笔/M 元…"）映射为 `closed_refunded_excluded`；
2. `runtime/models.py` 的 `_LIMITATION_CODES`（持久化状态限制码白名单，约 178-201 行）
   **没有** `closed_refunded_excluded`——它只在 `_DIAGNOSIS_KEYS`（诊断键白名单）里登记过；
3. 于是 `classify_result` 把该码写进 `state.limitations` 后，`model_copy` 的
   `validate_persisted_state` 拒绝整份状态。

**触发条件**：任何携带该披露的成功 `query_business` 支付查询——即 local_filter 口径平台
（tb/tm）店铺窗口内存在"关闭后已退款"订单时的 `paid_amount` 等支付族查询。真实测试库的
tb/tm 店全部满足，因此**该缺陷在真实数据上挡住了最常见的日常支付查询**。不受影响的路径：
fxg（已认证交易通道，不产生该披露）、missing_data/needs_input/forbidden 等不产出该码的
结果、以 `quantity` 为指标的分区排行（W18 成功）。

**单元级复现**（隔离、无库）：

```python
from uuid import uuid4
from bi_agent.business_query.state import BusinessQueryState
from bi_agent.runtime.models import DomainStatus
from bi_agent.metrics import Coverage
from datetime import datetime, timezone
state = BusinessQueryState(run_id=uuid4(), node="classify_result")
state.model_copy(update={
    "tool_status": "ok", "target_status": DomainStatus.SUCCESS,
    "coverage": Coverage(status="complete", start=None, end=None),
    "data_as_of": datetime(2026, 10, 1, 18, 5, tzinfo=timezone.utc),
    "limitations": ["closed_refunded_excluded"],  # 同输入换成 revenue_not_attributed 则通过
    "problems": [], "error": None,
})   # ValidationError: unsafe_persistence_payload
```

**为何离线 26 题没拦住**：合成种子在成功支付结果上不产生 S3 剔除披露（无关闭后退款单），
`test_core` 只断言了 `_limitation_codes()` 能映射出该码，未走"真实限制码集合 × 状态持久化
校验"的组合路径。

**修复方向**（未实施，待所有者决策）：把 `closed_refunded_excluded` 加进
`runtime/models.py` 的 `_LIMITATION_CODES`（或从 commerce 词表合并），并补一条
"携带该码的成功结果能走完 classify→persist→finalize"的红灯测试。

### 5.2 真实数据边界观察（均为 fail-closed 的设计行为，非缺陷）

- **出库口径对比被发货时间完整性挡住**（W10）：所有 tb/tm 店的出库单存在 `consigned_at IS NULL`
  （如 166520 为 3161/7638），`erp_outstock` 口径的对比按"一张单缺发货时间就无法证明窗口归属"
  整店排除后无剩余分组。迁移 `025` 的回填完成后此门才会放行。
- **支付口径对比只有 fxg 已认证**（W09）：jd/kuaishou/wxsph 时间口径未逐店对照 → data_missing
  格子只给原因；tb/tm 属出库口径不混入。双口径契约按 S1 设计逐格披露。
- **同名店铺澄清的子串匹配**（W17）：问题里出现"元发钉枪五金工具"会因包含另一家同名店
  "元发钉枪"（fxg/kuaishou 各一家）触发澄清；措辞需加平台或改用引用。
- **探针店铺**：`SF79F91`（display_name='x'）在店铺表启用且带 orders 覆盖凭证，但 capabilities
  为空——混进授权范围会把支付指标的能力门禁整体触发 fail-closed。
- **上架价/库存来源未核验**（W11/W12）：价格快照 40 份（100,720 行明细）存在但
  `listing_source_unverified`；库存快照 0 行。两图都按"来源尚未取证"出 missing_data，
  不报价格差异也不报库存安全。
- **PDD 与越权**（W15/W16）：拼多多支付族永久解析不通、只保留单据口径语义；未授权店铺引用
  在 `authorize_scope` 终态 FAILED 且禁止重试。两者均不可通过换措辞绕过。

### 5.3 工作流层面的观察

- W06a→W06b 的"缺期间反问 → 补期间成功"链路按设计工作：needs_input 运行不落 Artifact、
  会话筛选不继承无效轮参数。
- W13 的问法必须是"月份 + 销售额 + 费用率"齐备（与验收题 Q17 同形）；"假设下月销售10万元"
  这类口语会因窗口与假设值解析不全被参数纠正环拦下，不出数也不编造。
- SSE 事件顺序在全部 18 轮中符合 `thinking → (querying → artifact*) → answering → message → done`
  的固定阶段序；error 轮只有 `thinking → error → done`。

## 6. 复现方式

本轮驱动脚本（离线桩模型题库 `daily_script.py`、隔离启动器 `serve_api.py`、驱动器
`run_workflow.py`）位于 `backend/.scratch_daily_workflow/`，为一次性探针，随本轮清理删除；
题库内容即 §3 的 18 问与各自预制工具调用，需要重跑时可按 §2 环境参数重建。

```powershell
# 隔离实例（测试库 + 离线桩模型）
cd backend
uv run --env-file ../.env.test python <serve_api 脚本>     # 127.0.0.1:8001
uv run --env-file ../.env.test python <run_workflow 脚本>
```

缺陷复现见 §5.1 的单元级片段，无需起服务。

## 7. 结论

- 四张已启用状态图的节点链、事件顺序与终态分类在真实数据下按设计执行；五种终态全部
  得到覆盖（business_query 5/5，commerce 4/5，listing 与 inventory 各自的 missing_data 全链）。
- **存在一个 P1 缺陷（§5.1）**：真实 tb/tm 数据上的成功支付查询会在状态持久化校验处崩溃，
  日常"看支付金额"类问题在修复前不可用（fxg 与非支付指标不受影响）。
- 出库口径对比、支付口径认证、上架价/库存来源核验三处门禁在真实数据上全部 fail-closed，
  与"缺数不补零、来源未取证不结论"的既定契约一致。
