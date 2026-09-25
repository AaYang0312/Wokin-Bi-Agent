# S2 淘系支付口径取证实验验收记录（CERTIFIED @ local_filter 层级）

日期：2026-09-25
计划：`docs/superpowers/plans/2026-09-25-first-two-workflows-launch.md` §2 S2
实现基线：`main@3bb86b6`（S1 已提交）+ 当前未提交工作树
结论：**S2 判定规则满足 → CERTIFIED（「本地过滤认证」层级）**。tb/tm 支付窗口同步链路已改为
宽出库窗 + 本地 payTime 过滤；查询侧「完整支付窗口」门禁保持 fail closed（见 §7 待追认项）。

## 1. 授权与纪律边界（本轮真实 API 调用的全部范围）

- 所有者明确授权：只读探针，1 家 tb 店 + 1 家 tm 店（下文按 `tb_sample` / `tm_sample` 指称，
  真实店铺 ID 只在探针进程内存中瞬时存在，未写入任何仓库文件/报告/工件）。
- 同一已完成半月窗口 `[2026-09-01 00:00:00, 2026-09-16 00:00:00)`（北京时间，`[start,end)`）。
- 仅既有快麦只读查询：`erp.shop.list.query`（候选解析）、`erp.trade.outstock.simple.query`
  （`timeType=pay_time` / `consign_time`，`queryType=0/1`）。
- 无生产/预生产库访问、无写库、无真实同步/回填、无模型调用；凭证经
  `uv run --env-file .env.sync` 注入进程环境，从未打开、打印、修改、暂存或复制任何 `.env*` / `auth.json`。
- 文档快照新鲜度：开工快照 `logs/kuaimai-llms-full-2026-09-25-launch.txt`
  （SHA-256 `1373eff5a232656f1c1a422b507d6cad899c04bb5172c3d2092bb990a7f682b6`，2026-09-25 05:41 抓取）。
  与 09-12 快照 diff（`logs/kuaimai-llms-2026-09-25-vs-09-12.diff`，共 6 处）逐条核对：
  达人字段调整×3（authorName 行增删；authorId 标记废弃并新增 authorNo，其中一处位于
  出库通道 `list/orders` 子表——响应字段层面，不在规范化白名单内）；新增整节
  「三方仓接口日志查询」（`erp.party3.warehouse.logs.query`，本轮未调用）；售后工单
  响应新增 shopShortTitle/shopTitle；`allocate.task.detail.query` 移除 pageNo/pageSize
  请求参数。`erp.trade.outstock.simple.query` 与 `erp.shop.list.query` 的**请求参数、
  timeType/queryType 与分页形状均无变化**，本实验通道形状不受影响。

## 2. 探针（`logs/probe_s2_paycaliber.py`，gitignored，遵循 `probe_tb.py` 约定）

命令形状（脱敏；店铺由探针内 `erp.shop.list.query` × 授权清单解析，每平台取第一个启用店）：

```sh
cd backend && uv run --env-file ../.env.sync python ../logs/probe_s2_paycaliber.py \
    --platforms tb,tm --start 2026-09-01 --end 2026-09-16
```

三路实验：A = API `timeType=pay_time` 精确窗口；B = `timeType=consign_time` 宽窗
`[start-15d, end+15d)`（仍按 ≤1 天切片、在线+归档双通道、与生产同一套分页终止证据）
+ 本地 `payTime ∈ [start,end)` 过滤；C = A/B 差集按闭集类别逐单归因。
闭集类别：`pay_time_missing_or_placeholder` / `api_pay_time_out_of_window` /
`consign_time_missing` / `consign_outside_wide_window`（含延迟桶）/ `unattributed`；
B-only 一律 `api_pay_time_filter_missed`。任何 `unattributed>0`、分页不完整、预算超限或
API 错误都 fail-closed（退出码非 0，不给部分成功假象）。预算：页上限**按店**
（每店 ≤1200 页，CountingClient 逐店新建）；时限为**整轮共享**（开工起 1800 秒，
任一店触发即整轮 fail-closed 终止，未跑的店记 incomplete）。
输出只有聚合计数、字段非空计数、平台标签、类别计数、完整性标志与**加盐聚合证据摘要**
（盐每轮随机生成即弃，不可逆，不能当跨运行的稳定逐单令牌）。原始响应体/订单号/买家信息/
店铺名/凭证从不落盘。探针分类逻辑先经合成用例自检（含 2000-01-01 占位、窗外支付、宽窗内
异常缺失等 10 例），全部通过后才发起真实调用。

## 3. 聚合取证证据（探针 stdout 原样摘要；run `2026-09-25 12:42:02`，exit=0，verdict=ATTRIBUTED）

| 指标 | tb_sample | tm_sample |
| --- | --- | --- |
| A 流（pay_time API）：切片/调用 | 15 / 30 | 15 / 30 |
| A 流唯一订单 | 1763 | 199 |
| B 流（consign 宽窗+本地过滤）：切片/调用 | 45 / 90 | 45 / 90 |
| B 流宽窗唯一订单 → 本地过滤后 | 4419 → 1698 | 499 → 186 |
| 归档通道行 / 双通道重复 | 0 / 0（两店同） | 0 / 0（两店同） |
| 分页完整（complete） | true（两流） | true（两流） |
| 字段非空（payTime/consignTime/payAmount/status/…） | 与行数一致（无缺失） | 与行数一致（无缺失） |
| A\B（多出） | **65** | **13** |
| B\A（漏掉） | **0** | **0** |
| `consign_time_missing` | 65 | 13 |
| `pay_time_missing_or_placeholder` | 0 | 0 |
| `api_pay_time_out_of_window` | 0 | 0 |
| `consign_outside_wide_window`（含 >15/30/60 天、before_pay 桶） | 0 | 0 |
| `unattributed` | **0** | **0** |
| 归因覆盖率 | **100%** | **100%** |
| A 流 status 分布（TRADE_CLOSED/FINISHED/WAIT_BUYER_CONFIRM_GOODS） | 113/1555/95 | 25/163/11 |
| B 过滤后 status 分布 | 48/1555/95 | 12/163/11 |
| 证据摘要（sha256，加盐不可逆） | `b297136d…930668b` | `8e8ec4f0…9e16e8c` |

交叉验证：A\B 计数与 status 差逐店自洽（tb：113−48=65；tm：25−12=13）——本窗口全部差集
都是**已付款但从未发货（未见 `consignTime`）的 TRADE_CLOSED 单**，属固定非 PII 类别
（出库人群结构上限，S3 关闭单口径的对象）；宽出库窗+本地过滤对已发货人群与 API pay_time
过滤**逐单等价**（B 捕获了 A 中全部带发货时间的单，且无多无漏）。
预算：每店 120 次调用 ≈ 页预算的 1/10，远低于时限；探针原始聚合输出留存于
gitignored 的 `logs/s2_paycaliber_2026-09-25.out`（内容仅聚合，无标识符）。

## 4. 判定与实现（smallest change）

判定规则满足（差集全部可归因 + 覆盖率 100% + 分页完整 + 预算内）→ 实施：

- `backend/bi_agent/sources.py`：`TimeCertification` 增加 `local_filter`；tb/tm 由
  `disproved` 改标 `local_filter`（含义：窗口归属由本地 payTime 过滤按构造精确，
  不再依赖 API pay_time 参数语义；出库人群上限仍在，完整窗口主张不开放）。
  新增 `PAYMENT_FETCH_API_PAY_TIME` / `PAYMENT_FETCH_WIDE_CONSIGN_LOCAL_FILTER`
  常量与 `payment_window_fetch(platform)` 路由（tb/tm→宽出库窗，其余平台→API pay_time，
  未登记→None fail closed）。注册表版本指纹（binding_signature）随之变化，旧缓存结果自动失效。
- `backend/bi_agent/sync.py`：`PAYMENT_WINDOW_PAD=15d`；`fetch_payment_window`（宽出库窗
  **逐日切片**：用 `day_windows` 遍历 `[start-15d, end+15d)`，每片先在线
  （cursor/hasNext）后归档（total 证据 + 宽容空位）双通道 + `_pay_time_in_window`
  本地过滤，复用既有分页终止证据机器，任何一片不完整抛 `KuaimaiError`、
  调用方窗口事务整体回滚）；`fetch_window`/`sync_window`/`_run_window`/`_probe` 增加
  `payment_fetch` 参数（默认 `api_pay_time`，增量/扫描/售后不受影响）；
  `_shop_payment_fetch` 按 `bi.shops.platform` 路由（缺档案/未登记 SystemExit）；
  CLI `probe`/`backfill`/`reconcile`/`replay` 逐店接入。幂等/回补沿用既有
  `apply_trade` 版本守卫与 replay 同版本重述入口；覆盖仍只写请求窗口 `[start,end)`，
  宽窗只是取数范围，绝不写成覆盖。
- `backend/bi_agent/data_quality.py`：`local_filter` 与 `disproved` 同列支付窗口拒答层
  （`coverage_time_basis_unverified`）——严格按已证层级：出库人群上限 + S3 口径决策未落地前，
  「完整支付窗口」主张 fail closed。
- `backend/bi_agent/agent.py`：注册表派生的口径澄清文案增加 `local_filter` 说明
  （「付款窗口由本地 payTime 过滤建立（出库来源），完整支付窗口仍不宣称」）。
- 文档：`docs/metrics.md`（注册表 tb/tm 行改 `local_filter` + 四态说明 + 支付口径格仍
  data_missing）、`docs/runbook.md`（淘系支付窗口取数语义 + 认证三态→含 local_filter 的表述）。
- 红线保持：PDD 永久排除不变；whitelist-only 规范化未扩；无 missing-as-zero；
  无跨源替位；未改任何既有迁移（无 026 需要）；未动 `.env*`、操作者文件、受保护路径。

## 5. 测试与门禁（串行执行）

| 门禁 | 结果 |
| --- | --- |
| 新增红测试先行（8 个新用例在对应实现前全部 FAIL，实现后 GREEN） | PASS |
| `cd backend && uv run --locked --env-file ../.env.test python -m unittest discover -s tests -t .` | **1732 tests OK**（基线 1724 + 8） |
| `python -m unittest tests.test_db tests.test_runtime_db tests.test_monitoring` | **302 tests OK，0 skip** |
| `python -m tests.acceptance --offline` | **28/28 pass**（Q21/23/26/28 原样通过：拒答行为保持） |
| `cd frontend && npm test` / `npm run build`（里程碑证据；本轮未改前端） | 11 files / 177 tests passed；build PASS |
| `git diff --check`；`git diff --cached --name-only` 为空 | PASS / 空 |

新增用例（红→绿）：`test_core` 宽出库窗+本地过滤取数形状（宽窗边界/切片/只放行窗内
payTime 行）、宽窗分页 fail-closed、取数方式与订单源配对校验、`_shop_payment_fetch`
平台路由（tb/tm/pdd/fxg…/未登记）、tb/tm `local_filter` 注册表断言；
`test_db` 支付窗口回填只收窗内行且覆盖只写 `[start,end)`（宽窗界不出现在覆盖）+ 幂等重跑、
宽窗分页破坏时零覆盖+fail-closed 凭证。既有守卫更新：`allow_omitted_list=True` 宽容位
计数 2→3（新增宽窗归档通道，同接口家族同实测行为）、tb 拒答用例改述 `local_filter`
语义（断言不变）、淘系澄清文案断言更新。

## 6. 诚实边界（本文件证明了什么、没证明什么）

- 证明了：在 2 店 × 1 个半月窗口上，宽出库窗+本地过滤与 API pay_time 过滤的差集
  100% 落在固定类别（未发货单），且对已发货人群逐单等价；分页/预算 fail-closed 机器
  在新路径上成立；同步代码、测试与文档门禁全绿。
- 未证明：任意店、任意窗口的等价性（未来窗口如出现新差集形状，探针可复跑复核，
  `unattributed` 会直接显形）；「完整支付窗口」（出库人群上限是结构性的：未发货单不可见，
  只能由 upd_time 增量/发货后重放补齐）；与后台账单的对照（S6 工具的事）。
- 未调用真实模型；`--offline` 验收不证明模型理解；无生产/预生产访问；未 push/commit。
- 探针与原始聚合输出在 gitignored `logs/`；仓库内只有本脱敏证据文件。

## 7. 待所有者追认 / 后续

1. **查询侧门禁翻转（S3 范围）**：本轮把 tb/tm 标为 `local_filter` 并保持与 `disproved`
   同层的支付窗口拒答。待 S3 关闭单口径决策（TRADE_CLOSED 剔除规则）落地并产生真实
   `local_filter` 覆盖后，可将 tb/tm 支付指标降为「可答+披露」并讨论支付口径 tab 成员资格；
   本轮证据（§3）是该翻转的事实基础。
2. **查询放大成本**：宽窗使淘系支付窗口取数变为逐日切片：每个 1 天支付窗口为 31 个日切片 × 在线+归档两通道 = 每支付日至少 62 次 stream start（切片超 200 行另有分页请求；原 pay_time 路径每支付日 2 次 stream start）；增量/扫描
   路径不受影响。调度频度与预算归运维，若不可接受可回调 pad 或分批扫描（需重跑探针复核）。
3. 本判定样本为 1 窗口 2 店；扩大窗口/店铺前建议复跑探针，确认闭集类别仍解释全部差集。

## 8. 独立复审与修复记录（2026-09-25，复审 P1 已修复）

- **P1（已修复）**：生产 `fetch_payment_window` 原实现把整个 ±15 天宽窗作为单条在线流 +
  单条归档流传给分页机器，与本探针实测、官方「跨度建议不超过一天」、docstring 与本文件
  §2/§4 的描述不一致；多日单流可能被服务端静默截断，而分页完成证据只覆盖服务端实际返回
  的内容，无法发现截断。修复：改用 `day_windows` 逐日切片，每片先在线后归档双通道，再套
  同一本地 payTime 过滤；任何一片不完整即抛 `KuaimaiError`，窗口事务整体回滚、请求窗口
  覆盖不推进。原测试的 `assertLessEqual(切片数, 31)` 上界断言过弱（vacuous），已替换为
  **逐字断言 31 个日切片的精确边界与「先在线后归档」通道顺序**（`test_core`），并新增
  **后续切片失败整体回滚/零覆盖** 的真实 DB 用例（`test_db`，失败落在第 10 片在线通道）。
  两个用例在修复前均为红灯（精确边界用例直接暴露单流形状；后续切片用例在单流形状下
  不触发失败），修复后转绿。真实探针证据（§3）本就按逐日切片执行，形状与修复后生产
  代码一致，无需重跑。
- **P2 验收可信度修正（父级已完成）**：fresh 复审发现后续切片回滚用例最初使用的
  `payTime` 实际落在请求窗口上界之外，虽能证明后续切片错误与零覆盖，却不能证明早先已写行
  随事务回滚。父级把该夹具改为窗口内的 `2026-09-01 12:00`，并重新运行该 DB 用例：
  早先行先进入窗口事务，第 10 片失败后 `bi.orders` 仍为零、覆盖仍为 `{}`、错误码仍为
  `invalid_response`，用例通过。因这会影响本文件对测试证据的主张，未作为普通 P2 延后。
- **非阻塞 P2（仅记录，不在本修复范围改动）**：① chart artifact 读回路径尚未再次校验
  可选 `caliber` 的枚举值（承接 S1 复审遗留项）；② `runtime/models.py` 的
  `_TIME_CERTIFICATIONS` 尚未加入新值 `local_filter`，当前发布路径会在更早的支付窗口门禁处
  拒答，故暂不可达，但未来若放开门禁必须同步闭集并加 validator 回归；③ commerce
  `_attributed_reason` 对 `coverage_time_basis_unverified` 的状态重映射尚只识别 `disproved`，
  未来放开 `local_filter` 门禁时必须一并扩展。三项留待后续切片处理。
- **验收信任性文字更正（复审指出）**：① 探针时限为整轮共享（1800 秒），页上限才是按店
  （1200 页）——§2 已更正；② 快照 diff 范围并非「仅达人字段」——§1 已改为逐条列出 6 处
  变更及对本实验通道的影响核对；③ 查询放大从错误的固定「约 90 次」更正为
  「31 切片 × 2 通道 = 每支付日至少 62 次 stream start」（§7；runbook 同步更正）。
