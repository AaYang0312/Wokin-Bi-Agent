# Continuous Inventory Notifications Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在真实库存来源已核验时，由部署主机周期性调用一次性 CLI 复用 `InventoryWatchGraph`，以稳定去重状态机生成应用内库存通知；重复、并发、崩溃、stale 和不完整扫描都不能误报恢复或重复首发。来源门禁**按层级分别判定**（2026-09-17 所有者 Option A 决定）：已核验层级可以出告警，缺已核验登记的层级是一等 `data_missing`/unknown——永不补零、永不替位、永不触发、永不解除，也不向用户周期性播报“无法判断”。

**Architecture:** 专用 `bi_monitor` 身份只读获准库存视图并写监控表/运行审计；CLI 每次加载已批准策略与授权范围，以 `MemoryQueryRunStore` 执行现有 InventoryWatchGraph，得到不可变安全来源 Artifact。repository 在单事务中持久化来源 run/artifact、执行 `open/acknowledged/resolved/suppressed` 状态迁移并写 PostgreSQL outbox；独立投递步骤幂等写入应用内 notification center。没有邮件、短信、IM、webhook、Codex automation 或自动采购动作。

**Tech Stack:** Python 3.11+、Pydantic 2、psycopg 3、PostgreSQL 17、FastAPI、React 19、TypeScript、Windows Task Scheduler / systemd timer 调用 CLI、现有 `unittest` / Vitest。

**Spec:** [Task 11 后置子项目总设计](../specs/2026-09-14-post-task11-subprojects-design.md) §3–4、§9–12。

**前置与授权：** 逐层来源判定与其证据局限见 [库存来源验收](../research/2026-09-14-inventory-source-acceptance.md)（2026-09-17 追加节记录所有者 Option A 决定）；本计划本轮只按 [持续库存通知本地开发例外](../research/2026-09-17-continuous-inventory-local-development-exception.md) 在本机实施 Task 1–6。

## Global Constraints

- Task 11 发布门禁必须通过；此外必须有日期化 inventory source acceptance，**按策略请求的层级逐层**证明该层级有登记来源、freshness policy、完整扫描凭据和生产对账。截至 2026-09-17 该验收仍为 **FAIL**：`physical` 四个维度全部 NOT MET；`channel` 在全量公开 API 目录核对后，按所有者决定记为“当前无权威读取来源”的一等 `data_missing`/unknown。
- 来源门禁按层级判定：`physical_total` 已核验而 `shop_sellable` 缺来源时，策略仍可就实物层触发/更新/解除。runner 把交给 graph 的**两份输入同时收窄为 `verified_levels`**：`InventoryInspectionRequest.levels` 与 `DomainContext` 的授权投影（策略仍记录完整请求层级与全部 opaque refs，见 Task 4 Step 3）。只核验实物时上下文只带获准池、店铺授权投影为空集——graph 的渠道快照读以 `context.allowed_shop_ids` 非空为闸（`inventory/graph.py:613-620`），空投影下根本不执行，渠道快照、店铺新鲜/扫描声明、渠道侧时点与渠道独有 SKU 都进不了本轮的行集、聚合与指纹；只核验渠道时反之（仅店铺范围，池授权为空集）；两层都核验时两者都带。未核验的请求层级根本不被询问，因此 monitor 路径上既没有它的行、也没有它的去重键与数量占位行；这一格的缺失由 gate 自己的**闭集固定码与计数**承载（`monitor_level_unverified` + `inventory_monitor_level_unverified_total{level}`），不靠 graph 为它出 `unsupported` 占位行。未核验层级不能触发也不能解除，并从 `complete_levels` 中省略；它的任何诊断永远不能使**已在跑的已核验层级**变得不完整。策略请求的层级里一个都没有核验时，该策略以 disabled 退出（不跑图）——fail-closed 的粒度是层级，全部未核验时才回到策略级。
- 已核验层级的完整性只由它自己的证据决定：渠道侧不可用（未核验、未同步、无快照）不得把 `physical_total` 赶出 `complete_levels`，否则实物层会被一个它从未被询问的层级永久拖成惰性（见 Task 4 Step 4 的归属规则与 `inventory_snapshot_missing` 双重来源）。
- 缺数量不是零，也不做层级替位：`physical_total` 不得复制、扇出或换算成逐店 `shop_sellable`，反之亦然；店铺级触发与解除只能由已核验的店铺级来源支撑。未核验层级的行不进入状态迁移、不产生告警、不写 outbox，也不产生任何面向用户的“无法判断”通知——只记固定诊断与固定计数指标。收窄请求后，graph 在 monitor 路径上不会为这一层出 `unsupported` + null 数量的占位行（它没被问过），因此也不会拿它去撑 `expected_items`、截断或去重；runner 仍保留一层防御过滤（Task 4 Step 4）处掉形状不对的入镜行，且两种情况下都不改写已持久化载荷、不重算 fingerprint。
- 一个曾经核验的来源或能力退场（本轮不再登记该层级）只能走既有 `suppressed` 语义，绝不能把 open/acknowledged 告警写成 `resolved`；该层级本轮只是没有快照（缺页、stale、无行）时保持原状态并记诊断。
- `erp` 不是 `shop_sellable` 的合法来源种类（`backend/bi_agent/inventory/rules.py` 的 `CHANNEL_SOURCE_KINDS`），任何拼多多通道永久排除：按层级放宽门禁不得变成给渠道层登记 ERP 派生数据或给 PDD 开口的理由。
- 本轮实现范围由 `docs/superpowers/research/2026-09-17-continuous-inventory-local-development-exception.md` 授权：**仅** Task 1–6 的本机开发（本机 `*_test` 库、离线/stub 测试、`INVENTORY_MONITOR_ENABLED=false` 默认）。真实快麦 API 调用、生产/预发布迁移与部署、部署主机计划任务注册、生产对账、真实来源取证与生产启用都不在本计划本轮范围内，本计划落地也不得声明来源验收 PASS、生产就绪或通知已启用。
- 缺少来源验收、来源被撤销、策略版本失效或 `INVENTORY_MONITOR_ENABLED=false` 时 CLI 以 disabled 成功退出，不产生告警或“无法判断”通知。
- 调度器只调用一次性 `python -m bi_agent.monitoring.cli run`；不在 API 进程建循环，不创建 Codex automation。
- 首版渠道只有 PostgreSQL outbox + 应用内通知中心；不包含邮件、短信、企业微信、Slack、Teams、任意 webhook 或外部凭证。
- 告警可由 `open → acknowledged`；**同层级** fresh/complete 的恢复扫描可将 open 或 acknowledged 置为 resolved；open/acknowledged 也可因策略失效、或该层级来源/能力退场置为 suppressed。acknowledge 不等于恢复，resolved 不能人工设置。
- 只有新的、fresh、完整、覆盖相同 policy scope 且属于**该告警自身已核验层级**的扫描能 resolved；stale、缺页、缺层级、层级未核验、单位冲突或 unsupported 只保留原状态并写诊断，不发送恢复。
- 相同事实重复扫描只更新 `last_observed_at`；冷却期后持续异常才发 retriggered；恢复后再次下降创建新的 alert generation。
- dedupe key 只由 policy version、库存层级、SKU ref、opaque scope ref 和 rule code 的 canonical JSON 计算；不含真实 ID、显示名、数量或时间。
- 一格要成为告警，先得能被唯一识别：`scope_ref` 只能来自该行自己的层级引用（实物 = `pool_ref` + `warehouse_ref` 一对，渠道 = `shop_ref`）。载荷契约本身允许这些键缺席（`backend/bi_agent/runtime/models.py:1514-1531` 只强制 `level/sku_ref/inventory_status`），而 graph 在已核验实物层也会出这种行：点名了某 SKU 但本轮无快照时是一格无池无仓库的 `unknown` 占位行（`inventory/graph.py:777-782`、`:858-867`）。runner 必须把缺任一层级作用域引用的行排除在 `MonitorScan.rows` 之外，记固定码 `monitor_row_scope_missing` 与固定计数：不借用另一层级的引用、不拼一个“默认池/默认店”、不补零、不生成去重键；这样的行既不触发也不解除。
- 所有数量、阈值、状态和变化由确定性代码计算；模型不参与调度、状态迁移、文案、投递或 acknowledge。
- `bi_monitor` 与 `bi_app`、`bi_sync` 分离；没有底表任意读写、聊天写入、外部网络或来源凭证权限。
- 每次 run 保持 30 秒总 deadline；策略数上限 100、每策略 500 行、outbox batch 100。**500 行是请求/决策预算，不是持久化量**：`inventory_alerts` 来源 Artifact 是 graph 的展示投影，`data` 最多 `MAX_DISPLAY_ITEMS = 20` 行（`inventory/rules.py:48`、`inventory/graph.py:966`，载荷契约在 `runtime/models.py:1448,1508` 直接按这个上限要 `inventory.truncated`），因此期望项超 20 时投影必然是全集的子集。本轮不实现全量决策投影（那是一个需要所有者另做的决定），也不得声称 500 行已全部持久化，更不得从被截断的投影里推断“没出现的那 480 格都安全”；截断轮次的 fail-closed 口径见 Task 4 Step 4。因此在投影扩大之前，实际可出告警的策略作用域受限于 ≤20 期望格：这必须是批准策略时的显式检查，不能等到运行时才发现“永远零告警”。
- 本计划固定使用迁移 `023_continuous_inventory_notifications.sql`；023 只允许在本机 `*_test` 库顺序执行和幂等重放，不在生产或预发布库执行。

## Execution Preflight

- [ ] 验证双门禁、逐层来源状态和迁移位置。

Run:

```powershell
git status --short --branch
rg --files docs/superpowers/research | rg '(task-11-release-acceptance|inventory-source-acceptance|continuous-inventory-local-development-exception)\.md$'
rg -n 'physical|channel|freshness|scan_complete|reconciliation|enabled|data_missing|按层级' docs/superpowers/research/2026-09-14-inventory-source-acceptance.md
rg -n 'INVENTORY_MONITOR_ENABLED|Task 1|023|本地' docs/superpowers/research/2026-09-17-continuous-inventory-local-development-exception.md
Get-ChildItem backend/sql -Filter '*.sql' | Sort-Object Name | Select-Object -ExpandProperty Name
```

Expected（三种结论必须分开写，不得合并成“通过”）：

1. **生产启用：仍 FAIL 并停止。** 库存来源验收为 FAIL——`physical` 逐层 NOT MET，`channel` 按所有者决定为一等 `data_missing`——因此生产/预发布启用、调度任务注册与真实来源冒烟一律不做。
2. **本地开发：已授权。** 2026-09-17 本地开发例外允许实施本计划 Task 1–6，但只限本机 `*_test` 库、离线/stub 测试，`INVENTORY_MONITOR_ENABLED=false` 默认，023 只在本机测试库执行。
3. **迁移位置：** 023 未被占用。

缺任一份文件时预检明确 FAIL 并停止全部实现。预检不得把第 2 条读成第 1 条已满足。

---

### Task 1: Monitor Contracts, Least-Privilege Settings, and Source Gate

**Files:**
- Create: `backend/bi_agent/monitoring/__init__.py`
- Create: `backend/bi_agent/monitoring/models.py`
- Create: `backend/bi_agent/monitoring/source_gate.py`
- Modify: `backend/bi_agent/config.py`
- Modify: `backend/bi_agent/inventory/rules.py`
- Modify: `.env.example`
- Create: `backend/tests/test_monitoring.py`
- Modify: `backend/tests/test_core.py`
- Modify: `backend/tests/test_inventory.py`

**Interfaces:**
- Consumes: `verified_inventory_sources()`、`InventoryInspectionRequest` and opaque catalog refs.
- Produces: `MonitorSettings`、`MonitorRunRequest`、`MonitorPolicy`、`MonitorScan`、`AlertTransition`、`load_monitor_settings(env)`、`verified_monitor_levels(policy, registrations) -> tuple[str, ...]`（策略请求层级里已核验的那个子集，保持 `policy.levels` 的顺序）与 `assert_monitor_sources_verified(policy, registrations) -> tuple[str, ...]`（只在该集合为空时抛 `monitor_source_unverified`，否则原样返回它）。返回集合有三个消费点，同一个值：runner 交给 graph 的 `levels` 与 `DomainContext` 授权投影（Task 4 Step 3 的服务端收窄，两者由同一次 gate 结果派生）与状态机的 `verified_levels`（Task 3）；策略本身仍记录完整请求层级与全部 opaque scope refs，不得被改写。

- [ ] **Step 1: 写默认关闭、独立 DSN 和来源门禁测试。**

```python
import unittest
from pydantic import ValidationError


class MonitorContractTests(unittest.TestCase):
    def test_monitor_settings_require_separate_identity_when_enabled(self):
        from bi_agent.config import load_monitor_settings
        env = valid_monitor_env() | {
            "INVENTORY_MONITOR_ENABLED": "true",
            "BI_MONITOR_DSN": valid_monitor_env()["BI_APP_DSN"],
        }
        with self.assertRaisesRegex(ValueError, "MONITOR_DSN_MUST_BE_SEPARATE"):
            load_monitor_settings(env)

    def test_policy_with_no_verified_level_exits_disabled(self):
        from bi_agent.monitoring.source_gate import assert_monitor_sources_verified
        with self.assertRaisesRegex(ValueError, "monitor_source_unverified"):
            assert_monitor_sources_verified(physical_and_channel_policy(), registrations={})

    def test_verified_physical_runs_while_channel_is_data_missing(self):
        from bi_agent.monitoring.source_gate import assert_monitor_sources_verified
        runnable = assert_monitor_sources_verified(physical_and_channel_policy(),
                                                   registrations=physical_only_registrations())
        self.assertEqual(runnable, ("physical_total",))
        # 这个返回值就是 runner 交给 graph 的 levels（Task 4 Step 3）：未核验的渠道层
        # 不被询问一次，所以不会以 graph 占位行的形式回来，也不会撑 expected_items。
        self.assertNotIn("shop_sellable", runnable)
        self.assertEqual(
            assert_monitor_sources_verified(physical_only_policy(),
                                            registrations=physical_only_registrations()),
            ("physical_total",))   # 只请求一层的策略行为不变
```

两个用例一起钉住方向：只有请求层级全部未核验才 disabled；已核验实物层、渠道层缺登记时返回 `("physical_total",)`——那一格是一等 `data_missing`，不是零，也不是整条策略的死刑，而且它也不会被拿去问一次 graph（渠道缺席不得拖慢或弄不完整实物层）。

- [ ] **Step 2: 运行测试并确认 monitoring 模块不存在。**

Run:

```powershell
Set-Location backend
uv run --locked python -m unittest tests.test_monitoring.MonitorContractTests -v
```

Expected: FAIL with `ModuleNotFoundError: bi_agent.monitoring`。

- [ ] **Step 3: 实现严格监控模型。**

`models.py` 使用：

```python
from datetime import datetime
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator

AlertStatus = Literal["open", "acknowledged", "resolved", "suppressed"]
AlertEventKind = Literal["triggered", "retriggered", "updated", "resolved"]

class MonitorRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    policy_ref: str = Field(pattern=r"^inventory-monitor/[0-9][0-9._-]{0,31}$")
    as_of: Literal["latest"] = "latest"

class MonitorPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)
    policy_ref: str = Field(pattern=r"^inventory-monitor/[0-9][0-9._-]{0,31}$")
    threshold_policy_ref: str
    owner_subject_id: str
    shop_refs: tuple[str, ...]
    inventory_pool_refs: tuple[str, ...]
    levels: tuple[Literal["physical_total", "shop_sellable"], ...]
    cooldown_seconds: int = Field(ge=3600, le=604800)
    enabled: bool

class MonitorAlertRow(BaseModel):
    """状态机可见的 graph 投影行：只有引用、句柄与数值，没有任何真实主键。

    `quantity` 取本层级的判定数量（`physical_total`→quantity、`shop_sellable`→
    channel_quantity），永不从另一层级取数；`status` 就是 graph 投影里的
    `inventory_status`。`scope_ref: str` 必填且非空，只能由本层级的 opaque 引用给出：
    实物是 `pool_ref` + `warehouse_ref` 的 canonical 对（graph 的真实物格本来就两
    个都带，见 `graph.py:894-902`；只取池会把同池两仓的两格抖成一个告警），渠道是
    `shop_ref`。载荷里缺任一成员的 graph 行（如已核验实物层的无池无仓库 `unknown`
    占位行）**不得被构造成 `MonitorAlertRow`**：runner 直接不把它放进 `rows`，只记
    固定码与固定计数（Task 4 Step 4）。行上不携
    `rule_code`：`StockAlertRow.as_payload` 没有这个字段，而首版只有阈值规则，所以
    `rule_code` 由状态机以固定常量参与 Step 3 的 `dedupe_key`，不从行里读。
    """
    model_config = ConfigDict(extra="forbid", frozen=True)
    level: Literal["physical_total", "shop_sellable"]
    status: str
    sku_ref: str
    scope_ref: str = Field(min_length=1)   # 缺作用域引用的行根本拼不出这个模型
    quantity: str | None
    threshold: str | None
    unit: str

class MonitorScan(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    policy_ref: str
    source_artifact_payload: dict[str, object]
    source_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    data_as_of: datetime
    fresh: bool
    complete_levels: tuple[str, ...]
    # 状态机唯一的行输入；已由 runner 按 verified_levels 过滤，未核验层级的 graph
    # 占位行不在这里（下方 Step 4 决策表与 Task 4 Step 1 的 rows 断言都消费它）。
    rows: tuple[MonitorAlertRow, ...]
    diagnostics: tuple[str, ...]

class AlertTransition(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    alert_ref: str = Field(pattern=r"^alert-[a-z0-9-]{1,60}$")
    previous_status: AlertStatus | None
    next_status: AlertStatus
    event_kind: AlertEventKind
    dedupe_key: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_artifact_ref: str

class StoredAlert(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    alert_ref: str
    dedupe_key: str
    generation: int = Field(ge=1)
    status: AlertStatus
    last_observed_at: datetime
    last_notified_at: datetime | None

class AlertDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    dedupe_key: str
    generation: int = Field(ge=1)
    previous_status: AlertStatus | None
    next_status: AlertStatus
    event_kind: AlertEventKind
    notify: bool
    observed: bool
    rule_code: str
    level: Literal["physical_total", "shop_sellable"]
    sku_ref: str
    scope_ref: str

class DeliverySummary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    selected: int = Field(ge=0, le=100)
    delivered: int = Field(ge=0, le=100)
    retried: int = Field(ge=0, le=100)
    dead_lettered: int = Field(ge=0, le=100)
```

`MonitorPolicy` 要求 shop/pool ref 非空、去重且排序，levels 非空；`physical_total` 要求 pool refs，`shop_sellable` 要求 shop refs。owner subject 和真实 scope ID 只留在服务端 policy，不进入 outbox payload。

`AlertDecision.observed` 区分两种 `event_kind=updated`：本轮真看到那一格的异常（`observed=true`，推进 `last_observed_at`）与本轮没有该层新观测、只是不能解除（`observed=false`，不推进 `last_observed_at`）。两者都不写 outbox，都不变 `resolved`。缺来源与缺扫描的保留决策永远取 `observed=false`——把“没看到”记成“刚看到”，下一个 stale 周期就再也不会报。

`MonitorPolicy.levels` 是**请求**层级，不是已核验层级；已核验集合只能由 Step 5 的 gate 从来源注册表派生。`MonitorScan.complete_levels` 只能是「已核验 ∩ 本轮该层级扫描完整」的子集：未核验层级永远不在其中，因此它既不能被当成“缺页”撑住解除，也不能被补成零数量行，也不从另一层级取数。`MonitorScan.rows` 是状态机的唯一行输入：正常路径上它只包含已核验层级的行，因为 runner 把 graph 请求与授权投影同时收窄到 `verified_levels`（Task 4 Step 3），未核验层级根本不会以 `unsupported` + null 占位行的形式出现在这份载荷里；作为第二道闸，runner 仍按 `verified_levels` 过滤一次 `data` 里的行（`graph.py` 的 `evaluate_inventory_thresholds` 与 `alert_rows` 确实会为你所请求的每一层出占位行，聊天路径至今如此），但不得把这层过滤当成未核验层级的表达方式。两种情况下已持久化的 `source_artifact_payload` 与 `source_fingerprint` 都保持 graph 原样，不改写、不重算。一个没被询问的层级只能由 `monitor_level_unverified` 与 `inventory_monitor_level_unverified_total{level}` 表达。`diagnostics` 只允许两个闭集：graph 已有的固定限制码（如 `inventory_snapshot_missing`、`inventory_channel_snapshot_missing`、`inventory_scan_incomplete`、`inventory_audit_incomplete`、`inventory_snapshot_stale`、`inventory_display_truncated`；`inventory_channel_source_unverified` 在收窄后的 monitor 路径上不应出现——该层根本不被询问，它属于聊天路径；`inventory_channel_snapshot_missing` 只在 `shop_sellable` 是本轮已核验层级之一时出现，物理轮出现即授权投影契约违约，按 Task 4 Step 4 当聚合码 fail closed），以及 monitor 自有的 `monitor_` 前缀固定码（`monitor_source_unverified`、`monitor_level_unverified`、`monitor_unverified_level_row`、`monitor_row_scope_missing`）；不自创文案，也不新增未在本计划里命名的码。

- [ ] **Step 4: 实现独立 MonitorSettings。**

`config.py` 新增而不扩张 `AppSettings` 的秘密范围：

```python
class MonitorSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    enabled: bool = False
    monitor_dsn: SecretStr | None = None
    service_subject_id: str = ""
    policy_refs: tuple[str, ...] = ()
    max_policies: int = 100
    run_deadline_seconds: int = 30

def load_monitor_settings(env: Mapping[str, str]) -> MonitorSettings: ...
```

loader 只读取 `INVENTORY_MONITOR_ENABLED/BI_MONITOR_DSN/MONITOR_SERVICE_SUBJECT/MONITOR_POLICY_REFS`；enabled=false 时不得要求 DSN；enabled=true 时三项非空、policy refs 最多 100，且 `BI_MONITOR_DSN` 不得等于 env 中 app/writer DSN。`.env.example` 只放变量名，不放凭证值。

- [ ] **Step 5: 实现按层级的来源 gate。**

```python
def verified_monitor_levels(
    policy: MonitorPolicy,
    registrations: Mapping[str, InventorySourceRegistration],
) -> tuple[str, ...]:
    """策略请求层级里已核验的那些；其余层级是一等 data_missing，不是零。"""
    verified: list[str] = []
    for level in policy.levels:
        entry = registrations.get(level)
        if (entry is not None and entry.evidence and entry.max_age_seconds > 0
                and entry.scan_complete_supported
                and entry.production_reconciled_at is not None):
            verified.append(level)
    return tuple(verified)

def assert_monitor_sources_verified(
    policy: MonitorPolicy,
    registrations: Mapping[str, InventorySourceRegistration],
) -> tuple[str, ...]:
    verified = verified_monitor_levels(policy, registrations)
    if not verified:
        # 请求层级一个都没核验：整条策略 disabled 退出，不产生行也不产生通知。
        raise ValueError("monitor_source_unverified")
    return verified
```

`InventorySourceRegistration` 增加必填 `scan_complete_supported: bool` 与 `production_reconciled_at: datetime | None`；Task 10 现有测试 fixture 显式填值，生产注册为空时仍无 verified source。**核验判定逐层独立**（2026-09-17 所有者 Option A 决定）：某层的 registration/freshness/scan_complete/reconciliation 缺失只把那一层排除在可运行集合之外——不出数、不触发、不解除——并把策略留在可运行状态；fail-closed 的粒度是层级，只有请求层级全部未核验才回到策略级 disabled。被排除的层级不得从 `physical_total` 拿数、不得被当作已恢复、也不得静默消失：它同时从 runner 的 graph 请求与图上下文授权投影里剔除（Task 4 Step 3），每轮以 `monitor_level_unverified` 与 `inventory_monitor_level_unverified_total{level}` 落地，不产生用户通知；它自己的缺席或诊断也永远不得反过来使一个已在跑的已核验层级变得不完整。按层级放宽只改“谁能出数”，不改“登记凭什么算核验”：`CHANNEL_SOURCE_KINDS` 与 PDD 排除白名单不变。

- [ ] **Step 6: 运行契约/配置测试并提交。**

```powershell
uv run --locked python -m unittest tests.test_monitoring.MonitorContractTests tests.test_core.ConfigTests -v
git add backend/bi_agent/monitoring backend/bi_agent/config.py backend/bi_agent/inventory/rules.py backend/tests/test_monitoring.py backend/tests/test_core.py backend/tests/test_inventory.py .env.example
git commit -m "feat: define inventory monitor boundaries"
```

Expected: gate off 不要求秘密；gate on 独立身份；未核验层级逐层 fail closed（不出数、不触发、不解除、不补零），请求层级全部未核验时策略 disabled 退出。测试只跑本机离线用例，不碰真实 API、真实密钥与生产库。

---

### Task 2: Monitoring Schema, `bi_monitor` Role, and Repository

**Files:**
- Create: `backend/sql/023_continuous_inventory_notifications.sql`
- Create: `backend/bi_agent/monitoring/repository.py`
- Modify: `backend/bi_agent/monitoring/__init__.py`
- Modify: `backend/tests/test_db.py`
- Modify: `backend/tests/test_monitoring.py`
- Modify: `backend/tests/fakeconn.py`

**Interfaces:**
- Consumes: `MonitorPolicy`, validated `NewArtifact(artifact_type="inventory_alerts")`, state-machine transitions.
- Produces: `MonitorRepository.load_policy(policy_ref) -> MonitorPolicy`、`MonitorRepository.load_active_alerts(policy_ref) -> tuple[StoredAlert, ...]`、`MonitorRepository.commit_scan(scan, decisions, *, now) -> tuple[AlertTransition, ...]`。

- [ ] **Step 1: 写权限、约束和事务原子性失败测试。**

```python
class MonitorRepositoryTests(unittest.TestCase):
    def test_bi_monitor_cannot_read_chat_or_inventory_base_tables(self):
        with self.assertRaises(InsufficientPrivilege):
            monitor_conn.execute("SELECT * FROM bi.chat_messages").fetchall()
        with self.assertRaises(InsufficientPrivilege):
            monitor_conn.execute("SELECT * FROM bi.physical_stock_items").fetchall()

    def test_outbox_failure_rolls_back_alert_and_source_artifact(self):
        repository = repository_with_failure("insert_outbox")
        with self.assertRaisesRegex(RuntimeError, "outbox_write_failed"):
            repository.commit_scan(SCAN, DECISIONS, now=NOW)
        self.assertEqual(repository.counts(), {"runs": 0, "artifacts": 0,
                                               "alerts": 0, "events": 0, "outbox": 0})
```

- [ ] **Step 2: 运行测试并确认迁移/Repository 不存在。**

Run: `uv run --locked python -m unittest tests.test_monitoring.MonitorRepositoryTests -v`

Expected: FAIL with missing schema/repository symbols。

- [ ] **Step 3: 写 023 表和状态约束。**

迁移创建：

```sql
DO $$ BEGIN
  CREATE ROLE bi_monitor NOLOGIN;
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

INSERT INTO bi.app_chats (id, subject_id, title, title_source)
VALUES ('00000000-0000-4000-8000-000000000023', 'inventory-monitor-service',
        '库存监控服务运行', 'auto')
ON CONFLICT (id) DO NOTHING;
INSERT INTO bi.app_messages (id, chat_id, role, content, status)
VALUES ('00000000-0000-4000-8000-000000000024',
        '00000000-0000-4000-8000-000000000023', 'user',
        'inventory-monitor/scheduled-run', 'complete')
ON CONFLICT (id) DO NOTHING;

CREATE TABLE bi.inventory_monitor_policies (
  policy_ref text PRIMARY KEY,
  threshold_policy_ref text NOT NULL,
  owner_subject_id text NOT NULL,
  shop_refs text[] NOT NULL,
  inventory_pool_refs text[] NOT NULL,
  levels text[] NOT NULL,
  cooldown_seconds integer NOT NULL CHECK (cooldown_seconds BETWEEN 3600 AND 604800),
  enabled boolean NOT NULL DEFAULT false,
  created_at timestamptz NOT NULL DEFAULT now(),
  CHECK (levels <@ ARRAY['physical_total','shop_sellable']::text[]),
  CHECK (cardinality(levels) > 0)
);

CREATE TABLE bi.inventory_alert_instances (
  alert_ref text PRIMARY KEY,
  dedupe_key text NOT NULL,
  generation integer NOT NULL CHECK (generation >= 1),
  policy_ref text NOT NULL REFERENCES bi.inventory_monitor_policies(policy_ref),
  rule_code text NOT NULL,
  level text NOT NULL CHECK (level IN ('physical_total','shop_sellable')),
  sku_ref text NOT NULL,
  scope_ref text NOT NULL,
  status text NOT NULL CHECK (status IN ('open','acknowledged','resolved','suppressed')),
  source_artifact_id uuid NOT NULL REFERENCES bi.query_artifacts(id),
  opened_at timestamptz NOT NULL,
  last_observed_at timestamptz NOT NULL,
  last_notified_at timestamptz,
  resolved_at timestamptz,
  acknowledged_by text,
  acknowledged_at timestamptz,
  UNIQUE (dedupe_key, generation)
);

CREATE UNIQUE INDEX inventory_one_active_alert_idx
  ON bi.inventory_alert_instances(dedupe_key)
  WHERE status IN ('open','acknowledged');

CREATE TABLE bi.inventory_alert_events (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  alert_ref text NOT NULL REFERENCES bi.inventory_alert_instances(alert_ref),
  event_kind text NOT NULL CHECK (event_kind IN ('triggered','retriggered','updated','resolved')),
  previous_status text,
  next_status text NOT NULL,
  source_artifact_id uuid NOT NULL REFERENCES bi.query_artifacts(id),
  idempotency_key text NOT NULL UNIQUE,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE bi.notification_outbox (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  topic text NOT NULL CHECK (topic = 'inventory_alert'),
  owner_subject_id text NOT NULL,
  alert_ref text NOT NULL REFERENCES bi.inventory_alert_instances(alert_ref),
  event_id bigint NOT NULL REFERENCES bi.inventory_alert_events(id),
  idempotency_key text NOT NULL UNIQUE,
  payload jsonb NOT NULL,
  available_at timestamptz NOT NULL DEFAULT now(),
  delivered_at timestamptz,
  attempts integer NOT NULL DEFAULT 0 CHECK (attempts BETWEEN 0 AND 20)
);

CREATE TABLE bi.in_app_notifications (
  notification_ref text PRIMARY KEY,
  owner_subject_id text NOT NULL,
  alert_ref text NOT NULL REFERENCES bi.inventory_alert_instances(alert_ref),
  event_kind text NOT NULL,
  payload jsonb NOT NULL,
  idempotency_key text NOT NULL UNIQUE,
  read_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now()
);
```

payload CHECK 使用 `bi.inventory_notification_payload_safe(jsonb)` 不可变递归函数，拒绝 `shop_id/pool_id/warehouse_id/erp_sku_id/subject_id/dsn/evidence/scan_evidence` 键。迁移另建 `bi.commit_inventory_monitor_scan(policy_ref text, source_payload jsonb, source_fingerprint text, data_as_of timestamptz, decisions jsonb, observed_at timestamptz) RETURNS jsonb`，设为 `SECURITY DEFINER SET search_path = pg_catalog, bi`；函数只接受通过上述 validator 的 `inventory_alerts` payload/decision 码表（decision 必须携带 `observed` 布尔，仅当其为 true 时推进 `last_observed_at`），使用固定 service chat/message，并在锁内计算全局 attempt_no。

迁移同时创建 `bi.deliver_inventory_outbox(delivery_at timestamptz, batch_limit integer) RETURNS jsonb`、`bi.mark_inventory_notification_read(notification_ref text, actor_subject_id text)` 和 `bi.acknowledge_inventory_alert(alert_ref text, actor_subject_id text)` 三个固定签名的 SECURITY DEFINER 函数；各函数固定 `search_path`、重验 owner/status/payload，且 revoke PUBLIC execute。

`bi_monitor` 只获得 reporting 库存视图 SELECT、monitor policy SELECT、目录展示面只读（`reporting.v_shops`、`reporting.v_catalog_version`：共享 graph 收尾的 catalog 投影只读店铺显示名目录与目录版本号，两者都不含库存数量、渠道快照或扫描证据）以及 commit/deliver 两函数 EXECUTE；不给 alert/outbox/query runtime/chat/来源底表的直接写权限。`bi_app` 只读按 owner 过滤的 notification/alert API view，并只可执行 read/ack 两函数，不直接改表。

- [ ] **Step 4: 实现单事务 commit_scan。**

`commit_scan()` 先用 `NewArtifact`、`validate_artifact_payload` 和 `AlertDecision` 重验输入，再在一个 `with conn.transaction():` 内只调用 `SELECT bi.commit_inventory_monitor_scan(...)`。数据库函数创建 service query run → 保存 immutable `inventory_alerts` source artifact → 对每个 dedupe key `pg_advisory_xact_lock(hashtextextended(key, 0))` → 锁 active alert → 应用预计算 decision → 插 event → 对 triggered/retriggered/resolved 插 outbox。updated 无显著变化只改 last_observed，不插 outbox；但 `observed=false` 的 updated（缺来源/缺扫描/退场保留）不得推进 `last_observed_at`，也不得插 outbox。任何一步异常整批回滚。

必须复用 `NewQueryRun`、`NewArtifact`、`validate_artifact_payload` 和 `allows_artifact_type`；repository 不接受任意 JSON 或 SQL 片段。

未核验/退场层级的 `suppressed` 决策按 `event_kind=updated`、`notify=false` 落库：只写 event 历史供审计，不写 outbox，所以来源退场不会变成一条用户通知。已核验层级之外没有任何行可写：数据库不得被要求替缺来源的层级造零数量告警。

- [ ] **Step 5: 运行迁移、角色和事务测试。**

```powershell
$env:TEST_DATABASE_URL='postgresql://postgres:postgres@127.0.0.1:5432/bi_agent_test'
uv run --locked python -m unittest tests.test_db tests.test_monitoring.MonitorRepositoryTests -v
```

Expected: 023 可重复应用；`bi_monitor` 权限矩阵精确；失败回滚五类行；并发 active unique 约束有效。023 只在本机 `bi_agent_test` 执行；`TEST_DATABASE_URL` 不是本机测试库时用例必须 skip 并报出原因，不得执行迁移。

- [ ] **Step 6: 提交 schema/repository。**

```powershell
git add backend/sql/023_continuous_inventory_notifications.sql backend/bi_agent/monitoring/repository.py backend/bi_agent/monitoring/__init__.py backend/tests/test_db.py backend/tests/test_monitoring.py backend/tests/fakeconn.py
git commit -m "feat: persist inventory alert state atomically"
```

---

### Task 3: Deterministic Alert State Machine and Dedupe

**Files:**
- Create: `backend/bi_agent/monitoring/state_machine.py`
- Modify: `backend/bi_agent/monitoring/__init__.py`
- Modify: `backend/tests/test_monitoring.py`
- Create: `backend/tests/fixtures/inventory_monitor_transitions.json`

**Interfaces:**
- Consumes: `MonitorScan`（含 Task 1 Step 3 定义的 `rows: tuple[MonitorAlertRow, ...]`，已由 runner 按 `verified_levels` 过滤），`MonitorPolicy`, current `StoredAlert` tuple, scan freshness/completeness, 以及 Task 1 gate 给出的 `verified_levels`。
- Produces: `dedupe_key(policy_version, level, sku_ref, scope_ref, rule_code) -> str`、`decide_alert_transitions(scan, *, policy, active, verified_levels, now) -> tuple[AlertDecision, ...]`。`verified_levels` 是必需关键字：层级门禁是状态机的输入，不是调用方的约定。

- [ ] **Step 1: 写阈值边界、重复、恢复、再次下降和 suppression gold tests。**

```python
class AlertStateMachineTests(unittest.TestCase):
    def test_threshold_equal_triggers_once_then_only_updates(self):
        first = decide(scan(quantity="10", threshold="10", fresh=True, complete=True), active=())
        self.assertEqual(events(first), ["triggered"])
        second = decide(scan(quantity="10", threshold="10", fresh=True, complete=True),
                        active=apply(first))
        self.assertEqual(events(second), ["updated"])
        self.assertFalse(second[0].notify)

    def test_only_fresh_complete_recovery_resolves(self):
        for candidate in (scan(quantity="11", fresh=False, complete=True),
                          scan(quantity="11", fresh=True, complete=False)):
            self.assertEqual(decide(candidate, active=(OPEN,))[0].next_status, "open")
        self.assertEqual(decide(scan(quantity="11", fresh=True, complete=True),
                                active=(OPEN,))[0].next_status, "resolved")

    def test_acknowledged_is_not_resolved_and_new_drop_creates_generation_two(self):
        self.assertEqual(decide(low_scan(), active=(ACKNOWLEDGED,))[0].next_status,
                         "acknowledged")
        reopened = decide(low_scan(), active=(RESOLVED_GENERATION_ONE,))
        self.assertEqual(reopened[0].generation, 2)
        self.assertEqual(reopened[0].event_kind, "triggered")

    def test_verified_physical_still_alerts_while_channel_level_is_unverified(self):
        decisions = decide(physical_only_low_scan(), active=(),
                           verified_levels=("physical_total",))
        self.assertEqual([(d.level, d.event_kind, d.next_status) for d in decisions],
                         [("physical_total", "triggered", "open")])
        self.assertFalse(any(d.level == "shop_sellable" for d in decisions))

    def test_unverified_channel_level_emits_no_decisions_and_no_inferred_rows(self):
        # 渠道层未核验又没有历史告警：不产生任何渠道决策。状态机只看 ref 不看数量，
        # “实物 100 不得出现在渠道行里”由四件事钉住：runner 根本没把渠道层发给 graph
        # （Task 4 Step 3 的 `levels = verified_levels` 收窄，故本轮无渠道行可被误用）、
        # graph 对被请求而未登记的层级逐行清空数量并置 `unsupported`（聊天路径仍靠它，
        # `graph.py` 的 `evaluate_inventory_thresholds`）、Task 4 runner 按 `verified_levels`
        # 的第二道行过滤、以及本用例的 ref 断言。023 的 `level CHECK` 只约束枚举
        # 取值，管不到数量出处，不得当成防扇出的屏障。
        scan = physical_only_low_scan()
        decisions = decide(scan, active=(), verified_levels=("physical_total",))
        self.assertEqual({d.level for d in decisions}, {"physical_total"})
        self.assertEqual(len(decisions), len(rows_of(scan, "physical_total")))

    def test_withdrawn_channel_source_suppresses_but_never_resolves(self):
        # 渠道层已有 active 告警而本轮不再核验该层 ⇒ 来源/能力退场，只能 suppressed。
        decisions = decide(physical_only_low_scan(),
                           active=(CHANNEL_OPEN, CHANNEL_ACKNOWLEDGED),
                           verified_levels=("physical_total",))
        channel = [d for d in decisions if d.level == "shop_sellable"]
        self.assertEqual([(d.next_status, d.event_kind, d.notify) for d in channel],
                         [("suppressed", "updated", False),
                          ("suppressed", "updated", False)])
        self.assertFalse(any(d.next_status == "resolved" for d in decisions))

    def test_missing_scan_from_verified_channel_keeps_status_and_cannot_resolve(self):
        # 渠道登记仍在，但本轮没有它的快照 ⇒ 该层不算完整，不解除也不写 outbox。
        decisions = decide(low_physical_scan_no_channel_rows(), active=(CHANNEL_OPEN,),
                           verified_levels=("physical_total", "shop_sellable"))
        channel = [d for d in decisions if d.level == "shop_sellable"]
        self.assertEqual([(d.next_status, d.event_kind, d.notify) for d in channel],
                         [("open", "updated", False)])
        self.assertFalse(channel[0].observed)  # 没看到就不是刚看到，不推进 last_observed_at
        self.assertEqual(outbox_decisions_of(decisions), [])
```

逐层用例是 2026-09-17 决定的直接翻译：已核验的实物层照旧首发；未核验又没有历史告警的层级不出行也不被补零；未核验但已有 active 告警只能解释为来源/能力退场，走 `suppressed`（`event_kind=updated`、`notify=false`、不写 outbox）；已核验但本轮缺扫描的层级保持原状态。`resolved` 永远需要一个 fresh、完整、同层级且该层级仍在注册表里的扫描。

- [ ] **Step 2: 运行测试并确认 state machine 不存在。**

Run: `uv run --locked python -m unittest tests.test_monitoring.AlertStateMachineTests -v`

Expected: FAIL with missing state-machine symbols。

- [ ] **Step 3: 实现稳定 key 和事件幂等键。**

```python
def dedupe_key(*, policy_version: str, level: str, sku_ref: str,
               scope_ref: str, rule_code: str) -> str:
    payload = [policy_version, level, sku_ref, scope_ref, rule_code]
    encoded = json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()

def event_idempotency_key(key: str, generation: int, event_kind: str,
                          source_fingerprint: str) -> str:
    return hashlib.sha256(f"{key}:{generation}:{event_kind}:{source_fingerprint}".encode()).hexdigest()
```

key 输入全部用严格 ref/code regex 验证；显示名、数量、阈值、now 不参与 dedupe。

- [ ] **Step 4: 实现状态决策表。**

```python
if not policy.enabled:
    return suppress_active(active, reason="policy_disabled")
runnable = set(verified_levels) & set(policy.levels)   # 唯一可进入迁移的层级集合
for level in {row.level for row in scan.rows} - runnable:
    # 第二道闸：`scan.rows` 已由 runner 过滤；仍有该层行到达 ⇒ 上游门禁被绕过。
    # 只丢那一层的行并记固定 code，不处理、不补零、不解除，也不牵连其它层级。
    drop_rows_of(level, reason="monitor_unverified_level_row")
for level in policy.levels:
    if level not in verified_levels:
        # 该层没有已核验来源：不出行、不触发、不补零。该层已有 active 告警只可能是
        # “曾经核验、本轮退场”（未核验过的层根本不会有行可触发），走既有 suppression。
        yield from suppress_withdrawn(active, level=level)   # suppressed/updated/notify=false
        continue
    rows = [row for row in scan.rows if row.level == level]
    if not scan.fresh or level not in scan.complete_levels:
        # 缺页、过期、本轮无行：保留原状态与 last_observed_at，只记诊断，永不 resolved。
        yield from observe_without_resolution(active, level=level,
                                             diagnostics=scan.diagnostics)
        continue
    for row in rows:
        current = active_for(active, row)                    # 仅同 dedupe key、同 level
        if row_is_low(row):
            if current is None:
                yield trigger(row, generation=last_generation + 1)
            elif now - current.last_notified_at >= timedelta(seconds=policy.cooldown_seconds):
                yield retrigger(current)
            else:
                yield update(current, notify=False, observed=True)
        elif current is not None:
            yield resolve(current)   # 同层级、已核验、fresh、complete_levels 含该层，且 row.status == normal
        else:
            yield no_decision()
```

决策表以层级为外层循环：一个层级未核验只影响那一层，已核验且完整的层级照旧触发、持续、解除。“本轮缺该层扫描”（仍在注册表里但没有行/不完整）与“该层登记退场”（不在 `verified_levels`）是两条不同路径：前者保留 open/acknowledged 并记 `inventory_channel_snapshot_missing` 一类固定码，后者走 `suppressed`；两者都不得产生 `resolved`，也都不写 outbox。`resolve` 必须同时满足 `current.level == row.level`、`row.level in verified_levels` 与 `row.level in scan.complete_levels`；实物层的完整扫描永远不能解除渠道告警，反之亦然。`scan.rows` 里出现不在 `runnable` 集合（`policy.levels ∩ verified_levels`）内的层级时——正常路径下这一层根本不会被问到：runner 已把 graph 请求与授权投影同时收窄为 `verified_levels`（Task 4 Step 3），未核验层级既无真实数量行也无 `unsupported` 占位行；能走到这里意味着上游门禁被绕过——状态机按第二道闸只丢弃**那一层**的行，记固定 code `monitor_unverified_level_row` 与 `inventory_monitor_level_unverified_total{level}`，不处理该层、不补零、也永不把该层的 open/acknowledged 变成 `resolved`；已核验且完整的其它层级照旧触发、持续、解除。丢弃整轮不是更保守而是错的方向：那等于静默丢掉已核验层级的决策，与上面“fail-closed 的粒度是层级”直接矛盾。占位行自身数量与阈值都是 null，本来就过不了 `row_is_low`，也永不支撑解除。

策略从 repository 消失/撤销时只对其 active alerts 产生 next_status=suppressed、event_kind=updated、notify=false；不能生成 resolved。`row_is_low` 复用 inventory `quantity <= threshold` Decimal 规则，unconfigured/unit_conflict/data_anomaly/unknown/unsupported 永不触发采购告警，也永不支撑解除：上面的 `resolve` 分支只在 `row.status == normal` 时成立，不然一格“没读到数据”就会把已开告警报成恢复。进入 per-row 循环的每一行都已带合法的 `(level, sku_ref, scope_ref)` 三元组：缺作用域引用的 graph 行（已核验实物层的无池无仓库 `unknown` 占位行）在 runner 那一层就被排除，因此它既生成不了 dedupe key，也匹配不上 `active_for`——既不首发，也不解除。

- [ ] **Step 5: 运行完整转移矩阵。**

fixture 至少覆盖 low、equal、normal、second drop、stale、missing page、physical missing、channel missing、unconfigured、unit conflict、shared pool、policy disabled、cooldown just-before/at-boundary、duplicate input rows，再加四组逐层门禁形态：只核验实物层且无渠道历史告警（只有 physical trigger，`scan.rows` 里渠道零行、零补零）、只核验实物层但渠道有 active 告警（渠道 suppressed/updated/notify=false/observed=false，resolved 计数 0）、两层都核验但渠道本轮无行（渠道保留 open，不推进 last_observed_at）、以及只核验渠道层而实物层缺来源的对称用例。再加一组绕闸 fixture：某层已在 graph 的来源注册表里（故它本身能出带真实数量的行）但缺 `production_reconciled_at` 因而 monitor 未核验——正常情况下 runner 不会把这一层发给 graph（Task 4 Step 3），所以只能人工把它的行塞进 `scan.rows` 来模拟“以后有人把请求重新改宽”这类回归——期望该层被记 `monitor_unverified_level_row` 并只丢那一层的行，已核验层级的 triggered/updated/resolved 决策全部照常产出（不得整轮丢弃），该层仍零告警、零 outbox。再加两组决策表边界的 fixture：已核验且完整的层级里出现一行 `unknown`（非 low 亦非 normal）而该格已有 active 告警时，期望保持原状态、`observed=false`、`resolved` 计数 0——“没读到数据”不得被读成恢复；以及 `complete_levels` 为空而两层都有 active 告警时（展示投影被截断那一轮交给状态机的形状），期望只有 `updated`、零 triggered、零 resolved、零 outbox。每条固定 previous/scan/verified_levels/now/expected transition/outbox count。

Run: `uv run --locked python -m unittest tests.test_monitoring.AlertStateMachineTests -v`

Expected: fixture 全通过；同一快照反复计算得到相同 idempotency key；三店共享 100 件只产生一个 physical key；没有任何一条 fixture 以“缺来源”为输入产生用户通知或 `resolved`。

- [ ] **Step 6: 提交状态机。**

```powershell
git add backend/bi_agent/monitoring/state_machine.py backend/bi_agent/monitoring/__init__.py backend/tests/test_monitoring.py backend/tests/fixtures/inventory_monitor_transitions.json
git commit -m "feat: decide idempotent inventory alerts"
```

---

### Task 4: One-Shot Runner Reusing InventoryWatchGraph

**Files:**
- Create: `backend/bi_agent/monitoring/runner.py`
- Create: `backend/bi_agent/monitoring/cli.py`
- Modify: `backend/pyproject.toml`
- Modify: `backend/bi_agent/inventory/graph.py`
- Modify: `backend/tests/test_monitoring.py`
- Modify: `backend/tests/test_inventory.py`

**Interfaces:**
- Consumes: `run_inventory_graph`、`MemoryQueryRunStore`、`MonitorRepository`、verified registrations and `MonitorSettings`.
- Produces: `run_inventory_monitor(request: MonitorRunRequest, *, settings: MonitorSettings, now: datetime, deadline: float, registrations: Mapping[str, InventorySourceRegistration] | None = None) -> tuple[AlertTransition, ...]`（`registrations` 省略时由服务端读 `verified_inventory_sources()`，测试可注入以逐层断言）与 console scripts `bi-inventory-monitor` / `bi-inventory-outbox`。runner 交给 graph 的 `InventoryInspectionRequest.levels` 必须是 gate 返回的 `verified_levels` 子集（Step 3），而不是 `MonitorPolicy.levels`。同一个 gate 结果同时决定 `DomainContext` 的授权投影：只核验实物 ⇒ 仅获准池 + 空店铺授权（`allowed_shop_ids=frozenset()`、`shop_refs={}`）；只核验渠道 ⇒ 仅策略店铺范围 + 空池授权；两层都核验 ⇒ 两者都带（Step 3）。

- [ ] **Step 1: 写 gate disabled、无模型、source Artifact 和 deadline 测试。**

```python
class MonitorRunnerTests(unittest.TestCase):
    def test_disabled_or_unverified_exits_without_graph_or_write(self):
        result = run_inventory_monitor(REQUEST, settings=disabled_settings(),
                                       now=NOW, deadline=DEADLINE)
        self.assertEqual(result, ())
        self.assertEqual(graph.calls, [])
        self.assertEqual(repository.writes, [])

    def test_graph_runs_in_memory_then_repository_commits_one_scan(self):
        result = run_inventory_monitor(REQUEST, settings=enabled_settings(),
                                       now=NOW, deadline=DEADLINE)
        self.assertIsInstance(graph.store, MemoryQueryRunStore)
        self.assertEqual(repository.commit_calls[0].scan.source_fingerprint,
                         fingerprint_of_graph_artifact())
        self.assertEqual(len(result), 1)

    def test_verified_physical_only_policy_runs_but_never_fans_out_to_channel_rows(self):
        run_inventory_monitor(REQUEST, settings=enabled_settings(),
                              registrations=physical_only_registrations(),
                              now=NOW, deadline=DEADLINE)
        self.assertEqual(graph.last_request.levels, ("physical_total",))   # 请求被服务端收窄
        self.assertEqual(graph.last_context.allowed_shop_ids, frozenset())  # 店铺授权投影同样收窄
        self.assertEqual(graph.last_context.shop_refs, {})
        self.assertEqual(graph.last_context.allowed_inventory_pool_ids,
                         policy_pool_ids(repository.policy))               # 只带获准池
        self.assertEqual(repository.policy.levels,
                         ("physical_total", "shop_sellable"))              # 策略仍记完整请求层级
        scan = repository.commit_calls[0].scan
        self.assertEqual(scan.complete_levels, ("physical_total",))
        self.assertEqual(level_of_rows(scan), ["physical_total"])
        self.assertIn("monitor_level_unverified", scan.diagnostics)
        self.assertNotIn("inventory_channel_source_unverified", scan.diagnostics)
        self.assertEqual(counter_total("inventory_monitor_level_unverified_total",
                                       level="shop_sellable"), 1)
        self.assertEqual(outbox_levels(repository), ["physical_total"])
        self.assertEqual(notifications_for_level("shop_sellable"), [])

    def test_authorization_projection_is_a_pure_function_of_verified_levels(self):
        # 纯契约（不跑图）：三种核验形态 ⇒ 三种投影；投影与策略存储是两回事，
        # 存储永不收窄（shop_refs/pool_refs/levels 原样保留）。
        self.assertEqual(projection_for(policy=POLICY, verified=("physical_total",),
                                        resolved=RESOLVED),
                         ContextProjection(shop_ids=frozenset(), shop_refs={},
                                           pool_ids=RESOLVED.pool_ids))
        self.assertEqual(projection_for(policy=POLICY, verified=("shop_sellable",),
                                        resolved=RESOLVED),
                         ContextProjection(shop_ids=RESOLVED.shop_ids,
                                           shop_refs=RESOLVED.shop_refs,
                                           pool_ids=frozenset()))
        self.assertEqual(projection_for(policy=POLICY, verified=BOTH, resolved=RESOLVED),
                         ContextProjection(shop_ids=RESOLVED.shop_ids,
                                           shop_refs=RESOLVED.shop_refs,
                                           pool_ids=RESOLVED.pool_ids))
        self.assertEqual(repository.policy.shop_refs, POLICY.shop_refs)

    def test_physical_only_round_never_reads_or_projects_channel_facts(self):
        # 真库夹具（仅本机 bi_agent_test；TEST_DATABASE_URL 非本机测试库时 skip 并报原因）。
        # 底层仓库里渠道侧真实存在且“难看”：过期 72h、scan_complete=false、还有一个
        # 只出现在渠道快照里的 SKU；店铺档案表缺 shop-2（shop_not_synced 触发条件齐备）。
        # 实物侧 fresh + scan_complete，阈值 low_replenish=2，实物 3+4 ⇒ 必须能首发。
        seed_repo(physical=[physical_snapshot("pool-a", "SKU1", "3", age=minutes(5)),
                            physical_snapshot("pool-b", "SKU1", "4", age=minutes(5))],
                  channel=[channel_snapshot("shop-1", "SKU1", "0", age=hours(72),
                                            scan_complete=False),
                           channel_snapshot("shop-1", "SKU-CHANNEL-ONLY", "9",
                                            age=hours(72), scan_complete=False)],
                  shops_with_profile=("shop-1",),      # shop-2 无 profile
                  threshold=("SKU1", "low_replenish", "2"))
        run_inventory_monitor(REQUEST, settings=enabled_settings(),
                              registrations=physical_only_registrations(),
                              now=NOW, deadline=DEADLINE)
        request, context = graph.last_request, graph.last_context
        self.assertEqual(request.levels, ["physical_total"])
        self.assertEqual(request.products, "all")               # 策略无 SKU 维度
        self.assertEqual(request.scope.shop_refs, [])           # 策略 shop_refs 不进请求
        self.assertEqual(context.allowed_shop_ids, frozenset())  # 空店铺授权投影
        self.assertEqual(context.shop_refs, {})
        self.assertEqual(context.allowed_inventory_pool_ids,
                         frozenset({"pool-a-id", "pool-b-id"}))
        self.assertNotIn("v_channel_stock", executed_sql(context.conn))  # 渠道快照零查询
        payload = repository.commit_calls[0].scan.source_artifact_payload
        self.assertTrue(payload["data"]
                        and all(row["level"] == "physical_total"
                                for row in payload["data"]))
        self.assertTrue(all("shop_ref" not in row for row in payload["data"]))
        self.assertEqual(sorted(claim["pool_ref"]
                                for claim in payload["inventory"]["pools"]),
                         sorted(policy_pool_refs(repository.policy)))
        self.assertEqual(payload["data_as_of"],
                         (NOW - timedelta(minutes=5)).isoformat())  # 渠道 72h 前时点没进来
        for absent in ("inventory_channel_snapshot_missing", "inventory_scope_empty",
                       "inventory_snapshot_missing", "inventory_channel_source_unverified"):
            self.assertNotIn(absent, payload["limitations"])
        self.assertNotIn("excluded_scope", payload)      # 无任何店铺侧排除声明
        self.assertNotIn(channel_only_sku_ref,
                         {row["sku_ref"] for row in payload["data"]})  # 渠道独有 SKU 不进全集
        scan = repository.commit_calls[0].scan
        self.assertEqual(scan.complete_levels, ("physical_total",))  # 实物完整、可触发
        self.assertEqual(counter_total("inventory_monitor_rows_dropped_total",
                                       level="shop_sellable"), 0)    # 渠道侧不产生行集信号
        self.assertEqual(outbox_levels(repository), ["physical_total"])   # 实物首发照旧
        self.assertEqual([t.next_status for t in transitions(repository)], ["open"])
        self.assertEqual(events_for_level(repository, "shop_sellable"), [])  # 零渠道迁移
        self.assertEqual(notifications_for_level("shop_sellable"), [])       # 零渠道卡片

    def test_channel_facts_cannot_enter_a_physical_round_fingerprint(self):
        # 泄漏反向证明：同策略、同实物数据下补插更多渠道快照与店铺档案，重跑得到的
        # 来源 Artifact 与 fingerprint 逐字节不变——空店铺投影下渠道侧事实没有通路。
        # （request_fingerprint 把 allowed_shop_ids 与 normalized_request 一起哈希，
        # runtime/artifacts.py:187-207；物理轮两者都不含店铺。）
        first = run_physical_round_and_fingerprint()
        seed_repo(channel=[channel_snapshot(f"shop-{n}", "SKU1", "7", age=minutes(1))
                           for n in (3, 4)],
                  shops_with_profile=("shop-3", "shop-4"))
        second = run_physical_round_and_fingerprint()
        self.assertEqual(first.payload, second.payload)
        self.assertEqual(first.fingerprint, second.fingerprint)
        self.assertNotIn("shop-3", json.dumps(second.payload, default=str))

    def test_channel_only_round_passes_only_shop_scope_and_never_touches_physical(self):
        seed_repo(physical=[physical_snapshot("pool-a", "SKU1", "1", age=minutes(5))],
                  channel=[channel_snapshot("shop-1", "SKU1", "0", age=minutes(5))],
                  threshold=("SKU1", "low_quota", "5", shop="shop-1"))
        run_inventory_monitor(REQUEST, settings=enabled_settings(),
                              registrations=channel_only_registrations(),
                              now=NOW, deadline=DEADLINE)
        context = graph.last_context
        self.assertEqual(context.allowed_inventory_pool_ids, frozenset())  # 池授权为空
        self.assertEqual(context.allowed_shop_ids, frozenset({"shop-1-id"}))
        self.assertNotIn("v_physical_stock", executed_sql(context.conn))   # 实物快照零查询
        scan = repository.commit_calls[0].scan
        self.assertEqual(scan.complete_levels, ("shop_sellable",))
        self.assertEqual({row["level"] for row in
                          scan.source_artifact_payload["data"]}, {"shop_sellable"})
        self.assertEqual(outbox_levels(repository), ["shop_sellable"])

    def test_both_verified_rounds_pass_both_projections(self):
        run_inventory_monitor(REQUEST, settings=enabled_settings(),
                              registrations=physical_and_channel_registrations(),
                              now=NOW, deadline=DEADLINE)
        self.assertEqual(graph.last_context.allowed_shop_ids, frozenset({"shop-1-id"}))
        self.assertEqual(graph.last_context.allowed_inventory_pool_ids,
                         frozenset({"pool-a-id"}))
        self.assertEqual(sorted(graph.last_request.levels),
                         ["physical_total", "shop_sellable"])

    def test_physical_pool_claim_gap_does_make_physical_incomplete(self):
        # 双重来源的另一侧：在册池缺一行快照声明 ⇒ inventory_snapshot_missing 打在实物层。
        graph.artifact = physical_clean_artifact(
            expected_items=2, status="partial",
            limitations=("inventory_snapshot_missing",),
            pool_claims=("pool-a",),        # 策略在册 pool-b 没有声明
            excluded_scope=())
        run_inventory_monitor(REQUEST, settings=enabled_settings(),
                              registrations=physical_only_registrations(),
                              now=NOW, deadline=DEADLINE)
        scan = repository.commit_calls[0].scan
        self.assertEqual(scan.complete_levels, ())     # 这一轮实物层不触发也不解除
        self.assertEqual(outbox_rows(repository), [])
        self.assertEqual(events_of_kind(repository, "resolved"), [])

    def test_no_requested_level_verified_exits_before_graph_and_before_write(self):
        result = run_inventory_monitor(REQUEST, settings=enabled_settings(),
                                       registrations={}, now=NOW, deadline=DEADLINE)
        self.assertEqual(result, ())
        self.assertEqual(graph.calls, [])
        self.assertEqual(repository.writes, [])

    def test_physical_row_without_pool_or_warehouse_is_dropped_without_a_key(self):
        # 已核验实物层里“点名该 SKU 但本轮无快照”的 unknown 占位行没有 pool_ref/warehouse_ref，
        # 而载荷契约允许这些键缺席（runtime/models.py:1514-1531）⇒ 它不得成为一格告警。
        run_inventory_monitor(REQUEST, settings=enabled_settings(),
                              registrations=physical_only_registrations(),
                              now=NOW, deadline=DEADLINE)
        scan = repository.commit_calls[0].scan
        self.assertNotIn("sku-no-snapshot", {row.sku_ref for row in scan.rows})
        self.assertTrue(all(row.scope_ref for row in scan.rows))   # 进入 rows 的行都有作用域
        from pydantic import ValidationError
        from bi_agent.monitoring.models import MonitorAlertRow
        with self.assertRaises(ValidationError):   # 空作用域引用根本拼不出行模型
            MonitorAlertRow(level="physical_total", status="unknown", sku_ref="sku-x",
                            scope_ref="", quantity=None, threshold=None, unit="piece")
        self.assertIn("monitor_row_scope_missing", scan.diagnostics)
        self.assertEqual(counter_total("inventory_monitor_rows_dropped_total",
                                       level="physical_total",
                                       reason="monitor_row_scope_missing"), 1)
        self.assertEqual(alert_keys_for(sku_ref="sku-no-snapshot"), [])  # 不生成去重键
        self.assertEqual(events_for(sku_ref="sku-no-snapshot"), [])      # 既不触发也不解除

    def test_truncated_display_projection_completes_no_level_and_cannot_resolve(self):
        # 回测：data 只是 top-20 展示投影（rules.py:48、graph.py:966、models.py:1448,1508）。
        # 截断时 all_safe 必为 false ⇒ status 只是 partial 而不是 failed
        # （graph.py:992,1014-1017），而唯一信号 inventory_display_truncated 本身是
        # 归因 succeeded 的业务发现码（graph.py:1241,1249-1251）。
        graph.artifact = display_truncated_artifact(
            expected_items=500, data_rows=20, status="partial",
            limitations=("inventory_display_truncated",))
        repository.active = (OPEN_PHYSICAL, ACKNOWLEDGED_CHANNEL)
        result = run_inventory_monitor(REQUEST, settings=enabled_settings(),
                                       registrations=physical_and_channel_registrations(),
                                       now=NOW, deadline=DEADLINE)
        scan = repository.commit_calls[0].scan
        self.assertEqual(scan.complete_levels, ())   # 不能安全归属 ⇒ 两个已核验层全部不算完整
        self.assertEqual([t.event_kind for t in result], ["updated", "updated"])
        self.assertEqual([t.next_status for t in result], ["open", "acknowledged"])
        self.assertTrue(all(t.previous_status == t.next_status for t in result))
        self.assertFalse(any(t.event_kind in ("triggered", "retriggered", "resolved")
                             for t in result))
        self.assertEqual(outbox_rows(repository), [])
        self.assertEqual(notifications(repository), [])
        self.assertEqual(counter_total("inventory_monitor_skipped_total",
                                       reason="display_projection_truncated"), 1)
```

- [ ] **Step 2: 运行测试并确认 runner/CLI 不存在。**

Run: `uv run --locked python -m unittest tests.test_monitoring.MonitorRunnerTests -v`

Expected: FAIL with missing runner symbols。

- [ ] **Step 3: 提取 InventoryWatchGraph 安全执行入口。**

不改现有聊天行为；只让 graph 接受由服务端构造的 `DomainContext` + `MemoryQueryRunStore`。runner 将 policy 的 opaque refs 经服务端目录解析为 allowed shop/pool IDs，构造原有 `InventoryInspectionRequest`；threshold 仍只来自 `threshold_policy_ref`，不把默认阈值塞给 graph。

`InventoryInspectionRequest.levels` 由 runner 在服务端**收窄为 gate 返回的 `verified_levels`**（2026-09-17 所有者 Option A 的实现落点）：未核验的请求层级不被问一次，所以 monitor 路径上不会出 `unsupported` + null 数量的占位行，也不会因此撑大 `expected_items`、多拿一份截断或多生一份去重键。这一格的缺席由 gate 自己的闭集词汇表达：固定码 `monitor_level_unverified` + 固定计数 `inventory_monitor_level_unverified_total{level}`；runner 不得为了让 graph “多报一条”而把未核验层级塞回请求。`MonitorPolicy.levels` 仍原样记录策略请求的全部层级（它是版本化策略的一部分，不参与本轮可运行集合），而 `InventoryInspectionRequest.levels` 只能是它的已核验子集；两者不得被写成同一个字段。`complete_levels` 只能来自已核验子集。聊天路径不受影响：聊天仍可同时请求两层并由 graph 自己报 `*_source_unverified`。

**只收窄请求不够，同一 gate 结果必须同时收窄图上下文的授权投影。**源码上 graph 的渠道快照读以 `context.allowed_shop_ids` 非空为闸（`inventory/graph.py:613-620`），店铺新鲜/扫描声明与渠道时点被并进聚合分母（`graph.py:671`、`:676-683`、`:1033-1039`），`products=all` 的 SKU 全集由两批快照并集派生（`graph.py:626-628`），未同步店铺还会以 `_exclude(..., "shop_not_synced", TEXT_POOL_SNAPSHOT_MISSING)` 把实物文案的 `inventory_snapshot_missing` 记进同一份限制表（`graph.py:293`）。若 runner 只改 `levels` 而 `DomainContext` 仍带全部获准店铺，这些渠道侧信号会全部产生：一个过期/不完整的渠道抓取就能把已核验实物层永久拖成 stale/incomplete/截断，未核验渠道独有 SKU 会撑大去重全集——这与“缺层级的诊断永远不能使已核验层级变得不完整”直接矛盾。因此 runner 按 `verified_levels` 构造**投影后的** `DomainContext`：只核验实物 ⇒ `allowed_shop_ids=frozenset()`、`shop_refs={}`、`allowed_inventory_pool_ids=` 仅获准池（空店铺投影下 `graph.py:634-635` 的渠道缺快照文案与 `graph.py:293` 的店铺排除都结构上不可产生）；只核验渠道 ⇒ 仅策略店铺范围、`allowed_inventory_pool_ids` 为空集（池授权为空时 `graph.py` 根本不读实物快照，实物侧声明/聚合分母为空）；两层都核验 ⇒ 两者都带。请求侧同步收窄：`products="all"`（`MonitorPolicy` 没有 SKU 维度，全集由本轮在跑层级的快照派生并按既有 `inventory_universe_from_snapshot` 披露），scope 为默认 `CommerceScope`（`all_authorized`、无 `shop_refs`、无 `platforms`——策略 `shop_refs` 不进请求 scope，只进策略存储）。`request_fingerprint` 把 `allowed_shop_ids` 与 normalized request 一起哈希（`runtime/artifacts.py:187-207`），因此物理轮指纹按空店铺投影计算，渠道侧事实没有进入指纹的通路；已持久化载荷与指纹照旧不改写、不重算。

```python
memory_store = MemoryQueryRunStore(forbidden_values=real_scope_ids)
projection = context_projection_for(policy, verified_levels, resolved)
context = DomainContext(
    subject_id=policy.owner_subject_id,
    allowed_shop_ids=projection.shop_ids,            # 仅实物核验时为 frozenset()
    allowed_inventory_pool_ids=projection.pool_ids,  # 仅渠道核验时为 frozenset()
    shop_refs=projection.shop_refs,                  # 与 shop_ids 同一收窄
    conn=readonly_conn, store=memory_store, chat_id=service_chat_id,
    user_message_id=service_message_id, root_request_id=service_request_id,
    now=now, deadline=deadline)
execution = run_inventory_graph(
    request=inventory_request(policy, verified_levels),   # levels = verified_levels；
                                                          # products="all"，scope 无 shop_refs
    context=context,
    tool_call_id=f"monitor-{run_ref}")
```

`context_projection_for` 是 `verified_levels` 的纯函数（Step 1 的投影契约用例钉死三种形态）。opaque refs 到主键的落点用枚举派生后比对，不反查单向引用：`inventory_pool_refs`（`pl-` 句柄）→ 对 `reporting.v_inventory_pools` 逐行派生 `pool_handle` 后与句柄集合求交（与 `resolve_sku_refs` 同一纪律，只读池身份列，不读数量或快照证据）得到 `allowed_inventory_pool_ids`；shop refs 同理经店铺目录派生。共享 graph 的收尾 catalog（`build_inventory_catalog` → `build_catalog`）只读 `reporting.v_shops` / `reporting.v_catalog_version` 的展示目录；空候选店铺集下实体表为空，这两个视图因此进 `bi_monitor` 的授权面（Task 2），除此之外不需要任何店铺读。

内存执行使用 023 预置的固定 service chat/message UUID；root request 与 tool call ref 由 `UUIDv5(NAMESPACE_URL, policy_ref + scheduled_at_utc_minute)` 稳定生成。真正 query run 的 attempt_no 由 `commit_inventory_monitor_scan` 在 advisory lock 内递增，避免两个 policy/process 撞唯一约束；不会创建或写入用户聊天。

**空店铺投影依赖 graph 的两个最小契约修订**（`backend/bi_agent/inventory/graph.py` 本就在本 Task 的文件清单里；两条都以「上下文没有店铺授权且请求不含 `shop_sellable`」为唯一触发形状，聊天路径——上下文总带店铺授权——逐字不变，原库存测试零回退）：

1. **节点 1 的空范围终止**（`graph.py:302-306` 今天对空 `candidate_shop_ids` 无条件停 `inventory_scope_empty` ⇒ 物理轮会在第一格死于 missing_data）：改为候选店铺为空**且**（上下文带来过店铺授权**或**请求要 `shop_sellable`）才停；空店铺授权且不含 `shop_sellable` 的运行按仅池范围继续。`inventory_scope_empty` 在聊天路径的语义、文案与码表不变——它仍回答“授权了店铺但没有可检查的店铺”，而不是“这个运行没有店铺可看”。
2. **授权池对推导**（`graph.py:433-451` 的 `_pool_pairs_for_scope`）：候选店铺为空时授权池对**不经店铺连接推导**——`repository.authorized_pool_pairs` 在 shops 为空时返回空（`inventory/repository.py:94-95`：池授权今天经由 `v_inventory_pool_shops` 的店铺侧交集），池-only 运行会静默得到零池零行。改为按池授权集直接对 `reporting.v_inventory_pools` 派生 `(namespace, pool_id)`（新增 `repository.authorized_pool_pairs_by_ids(conn, *, allowed_pool_ids)`，SQL 侧 `pool_id = ANY(获准池)`，与 `pool_connections` 同一授权面；`pool_connections` 内的逐池授权复核保持为第二道闸）；`connected_pairs` 保持空 ⇒ 不产生任何店铺口径的 `excluded_scope` 声明——物理轮的排除声明结构上不存在。`reporting.v_shops` 之外的店铺表不新增任何读取。

渠道快照读的闸（`graph.py:613-620` 的 `if context.allowed_shop_ids:`）保持上下文驱动不改：空投影下它天然不执行，不需要为 monitor 加层级分支。若 runner 违约传入了店铺授权，行级污染由状态机第二道闸（Task 3 Step 4）与 runner 行过滤（Step 4）拦住，但聚合污染（时点/扫描声明并集）不被允许发生——这正是投影必须由 Step 1 的空投影断言与零渠道查询断言钉死、而不是靠 graph 事后补救的原因。

- [ ] **Step 4: 只从已持久化形状构造 MonitorScan。**

从 memory store 找到唯一 `inventory_alerts` artifact，重新调用 `NewArtifact` / `validate_artifact_payload`；status 不是 ok/partial、Artifact 数不为 1 时本轮不出决策（只记固定 reason metric）；`fresh` 只在整份载荷拿不到可信 `data_as_of` 时才为 false，单格过期不改 `fresh`，只把对应层级赶出 `complete_levels`。`complete_levels` 只能等于 `verified_levels ∩ 本轮该层级扫描完整`，runner 不得把未核验层级放进来，也不得为其造零数量行。**完整性归属只有一条规则**，且只适用于本轮真正在跑的那几层（`verified_levels`，它们也正是 graph 被问到的全部层级）：限制码本身能说出一层的（`inventory_channel_snapshot_missing` 只说店铺 ⇒ 渠道侧）只把那一层从 `complete_levels` 去掉，其余层级照旧完整——且该码只在 `shop_sellable` 是本轮已核验层级之一时可能出现，物理轮的空店铺投影下它没有产生点（见 `inventory_snapshot_missing` 段）；聚合口径的码（`inventory_snapshot_stale`、`inventory_scan_incomplete`、`inventory_audit_incomplete`、`inventory_data_anomaly`、`inventory_unit_conflict`，以及下段单列的展示投影截断）说的是“整份行集里有格子没被判过或没被看全”，而 `inventory` 块里的 `expected_items/evaluated_items/truncated/counts` 全是跨层级合计（`graph.py:975-998`），无法安全归属到某一层——所以**本轮所有已核验请求层级都不进 `complete_levels`**。宁可这一轮什么都不改，也不能拿一份“看不出缺了谁”的投影去宣布恢复。收窄授权投影让这里的聚合天然只合计本轮在跑的层级：物理轮的新鲜/扫描/时点聚合在 graph 里就只剩池侧项（`graph.py:671`、`:676-683`、`:1033-1039` 的并集在空店铺集下为空），渠道侧永远凑不进分母——这是“已核验实物层可以完整并触发”的结构保证，不是归属规则的宽容。

**`inventory_snapshot_missing` 按「本轮真正在跑的范围」归属，不按文案判。**它的文案只说“本轮没有该库存池的实物快照”（`rules.py:143`），但在共享 graph 里有多个互不相干的产生点：实物侧确实有在册池而没读到行（`graph.py:631-632`），以及**店铺侧**遇到没有 profile 的店铺时被 `_exclude(..., "shop_not_synced", TEXT_POOL_SNAPSHOT_MISSING)` 直接 `_note` 进同一份限制列表（`graph.py:293`、`graph.py:1317`）。monitor 路径上**实物侧判据是且只是池声明**：策略 `inventory_pool_refs` 有一池对不上 `inventory/pools[].pool_ref` 逐池声明（`graph.py:1042-1051` 只为读到行的池出声明，缺项就是没读到）⇒ 打在实物侧，去掉 `physical_total`。全部在册池都有声明 ⇒ 实物侧被排除，剩下的归属取决于本轮范围：`shop_sellable` 是本轮已核验层级之一（店铺真正在范围内）⇒ 店铺侧证据（`excluded_scope[].reason == "shop_not_synced"`，`tool.py:37` 的 `_EXTRAS` 把它带进载荷）成立，只去掉 `shop_sellable`，**不得**牵连 `physical_total`；本轮是空店铺投影（物理轮）⇒ 店铺侧产生点结构上不存在，这个码出现在载荷里就是**授权投影契约违约**：按聚合码处理，本轮去掉全部已核验层级、照常单事务持久化不改写载荷，并在日志里记同一个固定码——绝不把违约信号解释成“实物不完整”，也绝不据此把请求或投影重新改宽（包括把店铺授权加回来）。`inventory_channel_snapshot_missing` 同一规则：`shop_sellable` 在跑时只去掉 `shop_sellable`；物理轮出现即投影违约 ⇒ 聚合 fail closed。两种证据都无法成立（含引用命名不一致）时维持既有的聚合处理。这条判据是单向的：它只能把层级从 `complete_levels` 里拿掉，永远不能反过来当作“该层已完整”的凭据。收窄授权投影后，“一个未同步店铺就把已核验实物告警永久弄哑”这条路**不是被归属规则兜住，而是结构上不存在**：物理轮根本没有店铺进入范围，`graph.py:293` 的 `_exclude` 无从执行。

**展示投影截断必须 fail closed**（本轮行集完整性的唯一来源就是这个载荷）。`data` 只是 graph 的 top-20 展示投影：`graph.py:963-966` 先按风险排序再切 `MAX_DISPLAY_ITEMS = 20`（`rules.py:48`），`inventory.truncated` 在 `expected_items > 20` 时必为 true（`graph.py:982,990`，载荷契约在 `runtime/models.py:1448,1508` 直接强制这条等式），而 `inventory_display_truncated` 是 graph 的“业务发现”类码、归因 `succeeded`（`graph.py:1241`、`graph.py:1249-1251`）。另请注意：截断时 `all_safe` 必为 false（`graph.py:992`），所以载荷 `status` 只是 `partial`而不是 `failed`（`graph.py:1014-1017`）——而 `partial` 在上面的规则里恰恰是“可以出决策”的合法状态。换一句话：一份完全合法、限制码看着只是“发现”的 `partial` Artifact 可以只带 20/500 行，这就是本段要补的洞。请注意收窄请求对这里的影响：未核验的渠道层不再被问，它的 `unsupported` 占位行也就不再撑大 `expected_items`（`graph.py:980` 的行集只包含被请求层级的格子），“42 家店铺×N 个 SKU 的占位行把实物层挤成永久截断”这条路被堵住；但截断规则本身不得削弱——只要真正在跑的已核验层级自己超 20 格，这一轮仍然全部不算完整。因此：`payload["inventory"]["truncated"]` 为 true，或 `limitations` 里出现 `inventory_display_truncated` 时，runner 把**本轮全部已核验请求层级**从 `complete_levels` 去掉（排序跨层级混合，不能安全归属到单层），状态机走既有的“缺扫描”分支：open/acknowledged 保持原状态、`observed=false`、不推进 `last_observed_at`、零 triggered、零 resolved、零 outbox、零用户通知。这一轮仍照常单事务持久化（run + 不改写的来源 Artifact），并记固定诊断与 `inventory_monitor_skipped_total{reason="display_projection_truncated"}`，使“这一轮没报”是一个可数的信号而不是静默失联。被截断的轮次不是“库存都安全”，也不是“已恢复”；runbook 与验收文件按这条口径写。策略作用域内期望项可能超过 20 时，本轮就是不出告警——这是已声明的局限（全量决策投影不在本轮范围，需所有者另行决定），不得被写成能力已完成。

`MonitorScan.rows` 还有一道行过滤：只把带全本层级 opaque 作用域引用的行放进 `rows`——实物行缺 `pool_ref` 或 `warehouse_ref`、渠道行缺 `shop_ref` 时（载荷契约允许这些键缺席：`runtime/models.py:1514-1531` 只强制 `level/sku_ref/inventory_status`；已核验实物层“点名 SKU 但本轮无快照”的 `unknown` 占位行就是这种合法行，`graph.py:777-782`、`:858-867`）不构造 `MonitorAlertRow`、不进 `rows`、不生成去重键，只记 `monitor_row_scope_missing` 与 `inventory_monitor_rows_dropped_total{level,reason}`；不得为它借另一层级的引用、拼一个默认作用域或补零，它既不触发也不解除。注意这一行过滤与上面收窄过的请求是两件事：它加在**已核验层级自己**的行上（这些行带着真实数量），不是用来处理未核验层级的——未核验层级在本轮载荷里本来就没有行。作为第二道闸，runner 仍先按 `verified_levels` 过滤一次 `data` 里的行（聊天路径证实了 `graph.py:850-856`、`:963-966` 会为你所请求的每一层出占位行）；过滤只发生在 `rows` 这一层，已持久化的 `source_artifact_payload` 保持 graph 原样，不改写载荷、不重算 `source_fingerprint`。缺层级、缺引用与缺完整只以固定诊断码与上述固定计数出现（labels 只取固定枚举值），不产生任何用户通知。canonical public payload 生成 source fingerprint；然后加载 active state、调用纯状态机（附 `verified_levels`），最后唯一一次 `repository.commit_scan()`。

请求层级全部未核验时，`verified_levels` 为空，runner 在调 graph 之前就返回空元组（CLI 记一次 `skipped{reason="monitor_source_unverified"}`）：不跑图、不开 DB 事务、不写 outbox，也不发“无法判断”通知。部分核验时照常跑图，但只跑已核验子集：缺来源那一层的可观测信号来自 gate 自己的固定码与计数，而不是来自向 graph 多问一次。

任何 graph exception、deadline exhausted 或 contract error 只记录固定 reason metric，保持 active alerts 不变且不写 outbox；尤其不能因“本轮跑失败”把已核验层级的告警当成恢复。

- [ ] **Step 5: 实现一次性 CLI。**

`pyproject.toml`：

```toml
[project.scripts]
bi-inventory-monitor = "bi_agent.monitoring.cli:run_main"
bi-inventory-outbox = "bi_agent.monitoring.cli:deliver_main"
```

`run_main()` 加载 `MonitorSettings`，enabled=false 打印单行 `inventory_monitor disabled` 并 exit 0；逐 policy 运行但共享总上限 30 秒，全部请求层级未核验的 policy 记为 disabled 跳过（不失败），任一 policy 异常 exit 1 并只输出 policy ref + fixed code。命令无 daemon/loop/HTTP server 参数。本轮只允许本机执行：不注册计划任务、不接生产 DSN、不读真实快麦凭证（`2026-09-17-continuous-inventory-local-development-exception.md`）。

- [ ] **Step 6: 运行 runner、CLI 和原库存图回归。**

```powershell
uv run --locked python -m unittest tests.test_monitoring.MonitorRunnerTests tests.test_inventory -v
$env:INVENTORY_MONITOR_ENABLED='false'
uv run --locked bi-inventory-monitor
```

Expected: graph 使用内存 Store；repository 一次原子 commit；disabled CLI exit 0 且零 DB 连接；只有实物层核验时 runner 只把 `physical_total` 发给 graph（`graph.last_request.levels == ("physical_total",)`）且上下文为空店铺投影 + 仅获准池（`graph.last_context.allowed_shop_ids == frozenset()`），载荷里根本不出渠道行、不含任何店铺侧排除声明，渠道层零告警零通知，而实物告警照旧发出；底层即使存在过期/不完整渠道快照、渠道独有 SKU 与无档案店铺，本轮对渠道快照零查询，`data_as_of`、`source_batches`、SKU 全集与 fingerprint 均无渠道侧贡献；未核验渠道只以 `monitor_level_unverified` 与 `inventory_monitor_level_unverified_total{level}` 落地；物理轮的载荷里不出现 `inventory_channel_snapshot_missing` / `inventory_scope_empty` / `inventory_snapshot_missing`（缺席断言），两层都核验的运行里渠道侧码仍只去掉 `shop_sellable`；缺 pool_ref/warehouse_ref 的实物行不进 `rows`、不生成 key、不产生事件；`inventory.truncated` 为 true 的那轮 `complete_levels` 为空、零 triggered/零 resolved/零 outbox、open/acknowledged 原状保持；原库存测试零回退。

- [ ] **Step 7: 提交 runner。**

```powershell
git add backend/bi_agent/monitoring/runner.py backend/bi_agent/monitoring/cli.py backend/pyproject.toml backend/bi_agent/inventory/graph.py backend/tests/test_monitoring.py backend/tests/test_inventory.py
git commit -m "feat: run inventory monitor as one-shot job"
```

---

### Task 5: At-Least-Once Outbox and In-App Notification Center

**Files:**
- Create: `backend/bi_agent/monitoring/delivery.py`
- Create: `backend/bi_agent/monitoring/api.py`
- Modify: `backend/bi_agent/api.py`
- Modify: `backend/tests/test_monitoring.py`
- Modify: `backend/tests/test_api.py`
- Modify: `frontend/src/types.ts`
- Modify: `frontend/src/api.ts`
- Create: `frontend/src/components/NotificationCenter.tsx`
- Create: `frontend/src/components/NotificationCenter.test.tsx`
- Modify: `frontend/src/App.tsx`
- Modify: `frontend/src/styles.css`

**Interfaces:**
- Consumes: notification outbox and authenticated API subject.
- Produces: `deliver_inventory_outbox(conn, *, now, limit=100) -> DeliverySummary`、`GET /api/notifications`、`POST /api/notifications/{notification_ref}/read`、`POST /api/inventory-alerts/{alert_ref}/acknowledge` and notification-center UI.

- [ ] **Step 1: 写投递幂等、崩溃重试和授权 API 测试。**

```python
class OutboxDeliveryTests(unittest.TestCase):
    def test_same_outbox_delivered_twice_creates_one_notification(self):
        deliver_inventory_outbox(conn, now=NOW, limit=100)
        deliver_inventory_outbox(conn, now=NOW, limit=100)
        self.assertEqual(notification_count(IDEMPOTENCY_KEY), 1)

    def test_crash_after_insert_retries_without_duplicate(self):
        with self.assertRaises(SimulatedCrash):
            deliver_inventory_outbox(crash_after_notification_conn(), now=NOW, limit=100)
        deliver_inventory_outbox(conn, now=NOW, limit=100)
        self.assertEqual(notification_count(IDEMPOTENCY_KEY), 1)

class NotificationApiTests(unittest.TestCase):
    def test_subject_cannot_read_or_acknowledge_another_subject_alert(self):
        self.assertEqual(self.subject_b.get("/api/notifications").json(), [])
        response = self.subject_b.post(f"/api/inventory-alerts/{ALERT_A}/acknowledge",
                                       json={})
        self.assertEqual(response.status_code, 404)

class MissingLevelDeliveryTests(unittest.TestCase):
    def test_missing_channel_level_diagnostics_never_reach_outbox_or_api(self):
        # 只有实物层核验：渠道层从未被问给 graph，它在 run 里只允许以 gate 的固定码出现。
        commit_scan_with(missing_level_diagnostics=("monitor_level_unverified",))
        self.assertEqual(outbox_rows_for_level("shop_sellable"), [])
        self.assertEqual(notification_rows_for_level("shop_sellable"), [])
        self.assertEqual([n["level"] for n in api_notifications(subject_owner())],
                         ["physical_total"])
```

- [ ] **Step 2: 运行测试并确认 delivery/API 不存在。**

Run: `uv run --locked python -m unittest tests.test_monitoring.OutboxDeliveryTests tests.test_api.NotificationApiTests -v`

Expected: FAIL with missing delivery and routes。

- [ ] **Step 3: 实现 at-least-once 投递。**

Python delivery wrapper 在事务中只调用 `SELECT bi.deliver_inventory_outbox(%(now)s, %(limit)s)`；该 SECURITY DEFINER 函数内部执行：

```sql
SELECT id, owner_subject_id, alert_ref, event_id, idempotency_key, payload
FROM bi.notification_outbox
WHERE delivered_at IS NULL AND available_at <= %(now)s AND attempts < 20
ORDER BY id
FOR UPDATE SKIP LOCKED
LIMIT %(limit)s;
```

逐行 `INSERT ... ON CONFLICT (idempotency_key) DO NOTHING` 到 in_app_notifications，再更新 outbox delivered_at/attempts。事务失败时两者一起回滚；单行 payload validation 失败只增加 attempts、把 available_at 指数退避且记固定 metric，达到 20 次进入 dead-letter 指标，不接外部 sink。

- [ ] **Step 4: 实现授权 notification API。**

GET 只返回当前 subject 未删除的 notification projection：`notification_ref/alert_ref/event_kind/status/level/sku_ref/scope_ref/quantity/threshold/unit/data_as_of/reason_code/read_at/created_at`。不返回 owner subject、真实 ID、evidence、完整来源 payload。载荷里只能出现已核验层级的事件：缺来源/缺扫描的层级不写 outbox、不进通知中心，它的固定码只到运维侧诊断与指标；UI 不得为了“解释为什么没有渠道告警”而周期性给用户发卡片。`quantity` 为空与数量为 0 在投影里必须是两回事：前者只属于已核验层级的 `unknown`/`unconfigured` 判定，且永不从另一层级取值。

read 路由只更新 notification.read_at。acknowledge 调用数据库函数或 repository CAS：仅当前 owner 的 open alert 可变 acknowledged，记录 actor/time；已 acknowledged 幂等 200，resolved/suppressed 返回 409。两条 POST 都要求现有 `WebWrite`。

- [ ] **Step 5: 写 notification center 前端失败测试。**

```tsx
it('shows unread badge and keeps acknowledge separate from resolved', async () => {
  render(<NotificationCenter notifications={[openNotification]} onRead={onRead}
                             onAcknowledge={onAcknowledge} />)
  expect(screen.getByLabelText('1 条未读库存通知')).toBeInTheDocument()
  await userEvent.click(screen.getByRole('button', { name: '确认收到' }))
  expect(onAcknowledge).toHaveBeenCalledWith(openNotification.alert_ref)
  expect(screen.queryByText('已恢复')).not.toBeInTheDocument()
})

it('renders stale diagnostics without claiming recovery', () => {
  render(<NotificationCenter notifications={[staleUpdate]} {...handlers} />)
  expect(screen.getByText('数据未满足解除条件')).toBeInTheDocument()
  expect(screen.queryByText('库存已恢复')).not.toBeInTheDocument()
})

it('renders no card for a level without a verified source', () => {
  render(<NotificationCenter notifications={[]} {...handlers} />)
  expect(screen.queryByText(/无法判断|数据缺失/)).not.toBeInTheDocument()
})
```

- [ ] **Step 6: 实现前端中心。**

`App.tsx` 启动后 fetch 最多 100 条并每 60 秒在页面可见时刷新；这是 UI polling，不是业务调度。按 event kind 显示“首次触发/持续异常/已恢复”，ack 与 read 两个动作分开；数量、阈值、单位、snapshot time、scope 和 reason 常显。未知 payload 拒绝渲染并计客户端错误，不猜商品名。缺来源层级没有对应的通知卡片可渲染：它不来自 API，UI 也不得为它造一个“无法判断”占位卡。

- [ ] **Step 7: 运行投递、API 和前端测试并提交。**

```powershell
Set-Location backend
uv run --locked python -m unittest tests.test_monitoring.OutboxDeliveryTests tests.test_monitoring.MissingLevelDeliveryTests tests.test_api.NotificationApiTests -v
Set-Location ..\frontend
npm test -- --run src/components/NotificationCenter.test.tsx src/api.test.ts
git add backend/bi_agent/monitoring/delivery.py backend/bi_agent/monitoring/api.py backend/bi_agent/api.py backend/tests/test_monitoring.py backend/tests/test_api.py frontend/src/types.ts frontend/src/api.ts frontend/src/components/NotificationCenter.tsx frontend/src/components/NotificationCenter.test.tsx frontend/src/App.tsx frontend/src/styles.css
git commit -m "feat: deliver in-app inventory notifications"
```

Expected: outbox 可重复投递且通知唯一；跨 subject 返回 404；ack 不显示 resolved；缺来源层级不产生任何 outbox 行或通知卡片；前端测试 PASS。

---

### Task 6: Concurrency, Crash Recovery, Scheduler Runbook, and Acceptance

**Files:**
- Modify: `backend/tests/test_monitoring.py`
- Modify: `backend/tests/test_db.py`
- Modify: `backend/tests/test_api.py`
- Modify: `frontend/src/components/NotificationCenter.test.tsx`
- Modify: `README.md`
- Modify: `docs/runbook.md`
- Modify: `docs/metrics.md`
- Create: `docs/superpowers/research/2026-09-14-continuous-inventory-notifications-acceptance.md`

**Interfaces:**
- Consumes: all Tasks 1–5 monitor interfaces.
- Produces: 本机（`*_test` 库、离线/stub）并发、崩溃与逐层门禁的验收证据，以及只写进文档的生产调度/回滚步骤（本轮不执行）；no new runtime interface.

- [ ] **Step 1: 增加真实 PostgreSQL 并发与崩溃矩阵。**

```python
def test_two_monitor_processes_create_one_active_alert_and_one_trigger(self):
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: commit_same_low_scan(), range(2)))
    self.assertEqual(active_alert_count(DEDUPE_KEY), 1)
    self.assertEqual(event_count(DEDUPE_KEY, "triggered"), 1)
    self.assertEqual(outbox_count(DEDUPE_KEY, "triggered"), 1)

def test_incomplete_scan_after_open_preserves_open_across_restart(self):
    commit_low_scan()
    restart_process()
    commit_incomplete_normal_scan()
    self.assertEqual(active_status(DEDUPE_KEY), "open")
    self.assertEqual(event_count(DEDUPE_KEY, "resolved"), 0)

def test_channel_source_withdrawal_suppresses_and_never_resolves_across_restart(self):
    commit_channel_low_scan()
    restart_process(without_channel_registration=True)   # 只剩实物层已核验
    commit_physical_only_scan()
    self.assertEqual(active_status(CHANNEL_DEDUPE_KEY), "suppressed")
    self.assertEqual(event_count(CHANNEL_DEDUPE_KEY, "resolved"), 0)
    self.assertEqual(outbox_count(CHANNEL_DEDUPE_KEY, "resolved"), 0)
    self.assertEqual(active_status(PHYSICAL_DEDUPE_KEY), "open")  # 实物层不受影响

def test_physical_only_and_both_level_policies_do_not_cross_feed(self):
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(commit_scan_for_policy, POLICY_REFS))
    self.assertEqual(active_alert_count(PHYSICAL_DEDUPE_KEY), 1)
    self.assertEqual(alert_count_for_level("shop_sellable"), 0)
```

- [ ] **Step 2: 运行完整后端、DB、前端与 26 题验证。**

```powershell
Set-Location backend
uv sync --locked
uv run --locked python -m unittest discover -s tests -v
$env:TEST_DATABASE_URL='postgresql://postgres:postgres@127.0.0.1:5432/bi_agent_test'
uv run --locked python -m unittest tests.test_db tests.test_runtime_db tests.test_monitoring -v
uv run --locked python tests/acceptance.py --questions tests/questions.jsonl --expected-count 26 --mode offline
Set-Location ..\frontend
npm test -- --run
npm run build
```

Expected: 后端、DB-enabled、前端和 build 零失败；DB-enabled 0 skip；26/26；并发矩阵只有一个 active/trigger/outbox。本步全部命令只跑本机：`TEST_DATABASE_URL` 必须指向 `bi_agent_test`，26 题只以 `--mode offline`（stub 模型）跑，不得跑 `--provider-smoke` / `--live`，也不得碰真实快麦 API。

- [ ] **Step 3: 做一轮 one-shot 本机 dry-run（不是 staging/生产冒烟）。**

```powershell
Set-Location backend
$env:TEST_DATABASE_URL='postgresql://postgres:postgres@127.0.0.1:5432/bi_agent_test'
$env:INVENTORY_MONITOR_ENABLED='true'   # 仅本进程、仅本机测试库
uv run --locked bi-inventory-monitor
uv run --locked bi-inventory-outbox
uv run --locked bi-inventory-monitor
uv run --locked bi-inventory-outbox
```

Expected: 第一轮按本机 `*_test` 库合成 fixture 产生一个 triggered；第二轮仅 updated、无第二个首次通知；日志只含 policy ref、run ref、固定状态码和计数。跑完立即清除该进程的环境变量：仓库内 `.env.example`、本机默认配置与任何共享环境里 `INVENTORY_MONITOR_ENABLED` 仍为 false。本轮不得向 staging/生产库执行 CLI，不得注册调度，不得接入真实来源；这些项在验收文件里记“未执行”，不记为通过。

- [ ] **Step 4: 写部署主机调度与回滚。**

runbook 给出两种受控配置：Windows Task Scheduler 每 15 分钟运行 `uv run --locked bi-inventory-monitor` 后运行 `uv run --locked bi-inventory-outbox`；systemd timer 使用同两条 ExecStart。两者都设置单实例、30 秒超时、非交互服务账号、失败退出码告警和工作目录；明确“不创建 Codex automation”。回滚先置 `INVENTORY_MONITOR_ENABLED=false`，停 scheduler，再保留历史表审计；不得删除 open alert 或伪造 resolved。

本步只写文档：本轮不得在任何机器上注册计划任务、不得创建 systemd timer / Windows 任务、不得部署。runbook 必须明写“当前状态：默认关闭、未部署、未注册调度；启用前置是逐层来源验收与 Task 11 门禁第 4–7 项”，并写出“只有实物层已核验时渠道层不会出告警”这一预期行为（runner 根本不会把渠道层发给 graph，也不给物理轮任何店铺授权投影——所以运维不会在物理轮看到 `inventory_channel_source_unverified`，也不该看到 `inventory_channel_snapshot_missing` 或未同步店铺一类店铺侧信号：这一格的信号只有 `monitor_level_unverified` 与 `inventory_monitor_level_unverified_total{shop_sellable}`），避免运维把零渠道告警误读成能力未实现。同一段必须写出另外两个同样会“零告警”的合法原因：来源 Artifact 只是 ≤20 行的展示投影，策略作用域内期望项超过 20 时本轮所有已核验层级都不算完整、不出告警也不解除；以及缺 `pool_ref`/`warehouse_ref`（实物）或 `shop_ref`（渠道）的格子不会成为告警。运维只能从固定诊断码（`monitor_level_unverified`、`monitor_row_scope_missing`、`inventory_display_truncated`）与指标 `inventory_monitor_level_unverified_total{level}`、`inventory_monitor_skipped_total{reason}`、`inventory_monitor_rows_dropped_total{level,reason}` 区分这三类，不得自行推断“没告警就是安全”；runbook 也要写明：物理轮根本不存在“未同步店铺”一类渠道侧信号（空店铺投影下它们结构上不可产生，渠道侧诊断在物理轮载荷里出现即投影契约违约）；两层都核验的运行里这类信号只去掉 `shop_sellable`（归属规则见 plan Task 4 Step 4），否则运维会去查错那一层。

- [ ] **Step 5: 记录验收与指标。**

验收文件写 Task 11/source acceptance 引用（包括 2026-09-17 的逐层判定与本地开发例外）、commit、023、角色权限、完整命令/通过数、state fixture、threshold equal、shared pool、stale/incomplete、generation 2、并发、崩溃、outbox 重投、跨 subject、gate disabled 和本机两轮 dry-run 结果，并逐条列出未执行项（真实 API 冒烟、目标环境迁移、部署/备份/恢复、计划任务注册、一周试用）。三份本轮回测结果必须逐字记下：graph 只收到 `verified_levels` 与同样收窄的授权投影（未核验层在载荷里既无行也无 `*_source_unverified`，物理轮 `allowed_shop_ids` 为空集、对渠道快照零查询，只留 `monitor_level_unverified` 与对应计数）；展示投影截断的那一轮 `complete_levels` 为空、零 triggered/零 resolved/零 outbox；缺作用域引用的 graph 行不进 `rows`、不生成去重键、不产生事件。另记下归属用例：两层都核验时 `shop_not_synced` 伴随的 `inventory_snapshot_missing` 只去掉 `shop_sellable`；在册池缺 `inventory/pools[]` 声明时停实物层；物理轮同码缺席（结构上不可产生，出现即按投影契约违约当聚合码）。同时必须原样记下局限：“来源 Artifact 是 graph 的 top-20 展示投影，本计划未实现全量决策投影；期望项超过 20 的策略作用域本轮不会出告警”，不得把它写成已解决或把 500 行行预算描述成已全量持久化。

验收文件不得写 PASS 以外的结论升级：它只能声称“本地实现与离线/本机测试通过”，不得声称库存来源验收通过、生产就绪或通知已启用；只要 `physical` 层仍未登记已核验来源，文件里就必须原样记下“今天生产监控保持 disabled，本验收未解除任一门禁”。文件名按实际执行日期固定（计划时占位为 `2026-09-14-continuous-inventory-notifications-acceptance.md`），不覆盖已存日期记录。

metrics 增加 `inventory_monitor_runs_total{status}`、`inventory_alert_transitions_total{event_kind}`、`inventory_monitor_skipped_total{reason}`、`notification_outbox_pending`、`notification_delivery_attempts_total{status}`、`inventory_alerts_active{status}`，再加缺来源层级的固定计数 `inventory_monitor_level_unverified_total{level}`（只取 `physical_total`/`shop_sellable` 两个枚举值；这一层被 runner 从 graph 请求里剔除，所以它的信号只有这个计数与 `monitor_level_unverified` 固定码，不是 graph 的 `*_source_unverified`）与被 runner 丢弃行的 `inventory_monitor_rows_dropped_total{level,reason}`（`reason` 只取 `monitor_row_scope_missing`、`monitor_unverified_level_row`）；`inventory_monitor_skipped_total{reason}` 的 `reason` 也是闭集，本轮只用 `monitor_source_unverified` 与 `display_projection_truncated`；labels 不含 policy ref、SKU、subject 或真实 ID。缺层、缺证据与投影截断信息只能以这类指标与诊断出现，不得以用户通知出现。

- [ ] **Step 6: 检查文档、敏感词和差异。**

```powershell
rg -n 'INVENTORY_MONITOR_ENABLED|bi_monitor|bi-inventory-monitor|bi-inventory-outbox|Task Scheduler|systemd|Codex automation|023_continuous_inventory' README.md docs/runbook.md docs/metrics.md docs/superpowers/research/2026-09-14-continuous-inventory-notifications-acceptance.md
rg -n '未执行|本机|逐层|data_missing|2026-09-17-continuous-inventory-local-development-exception' README.md docs/runbook.md docs/metrics.md docs/superpowers/research/2026-09-14-continuous-inventory-notifications-acceptance.md
rg -n 'shop_id|pool_id|warehouse_id|erp_sku_id|dsn|evidence|scan_evidence' docs/superpowers/research/2026-09-14-continuous-inventory-notifications-acceptance.md
git diff --check
git status --short
```

Expected: 第一条能定位门禁、调度、回滚和迁移；第二条能定位“未执行”清单、本机范围与逐层口径；第三条无输出；diff 无空白错误；仅计划内文件待提交（不含任何未跟踪的运维/参考文件）。

- [ ] **Step 7: 提交验收和运维文档。**

```powershell
git add backend/tests/test_monitoring.py backend/tests/test_db.py backend/tests/test_api.py frontend/src/components/NotificationCenter.test.tsx README.md docs/runbook.md docs/metrics.md docs/superpowers/research/2026-09-14-continuous-inventory-notifications-acceptance.md
git commit -m "docs: accept continuous inventory notifications"
```

## Final Verification

- [ ] 确认 Task 11 门禁与逐层库存来源验收的实际状态被原样引用：截至今日两者都未通过（`physical` 四维 NOT MET，`channel` 为一等 `data_missing`），因此生产 monitor 保持 disabled；本计划执行完成不解除任一门禁，也不得被写成来源验收 PASS、生产就绪或通知已启用。
- [ ] 确认来源门禁按层级判定：runner 只把 gate 返回的 `verified_levels` 发给 graph，并把 `DomainContext` 授权投影收窄到同一集合（物理轮 `allowed_shop_ids` 为空集、仅获准池；渠道轮仅店铺范围、池授权空集；两层都核验时两者都带；策略存储原样保留全部请求层级与 opaque refs，两者不是一个字段），因此未核验层级在 monitor 路径上无行、无占位行、无去重键，也不撑 `expected_items`/截断；它只以 `monitor_level_unverified` 与 `inventory_monitor_level_unverified_total{level}` 落地，不产生告警、outbox 或用户通知。只有实物层已核验时：底层即使存在真实渠道快照（含过期/不完整/渠道独有 SKU）与无档案店铺，本轮对渠道快照零查询，实物告警照旧触发/持续/解除，载荷、`data_as_of`、`source_batches`、去重全集与 fingerprint 均无渠道侧贡献；渠道侧信号（未核验、未同步店铺、无渠道快照）不得把已核验实物层赶出 `complete_levels`；载荷里若仍出现非已核验层级的行，只由 runner 的第二道过滤闸掉，已持久化载荷与 fingerprint 不改写；且没有任何一行把 `physical_total` 的数量复制、扇出或换算成逐店可售量；反之只核验渠道层时同理（对实物快照零查询）。
- [ ] 确认 monitor 图契约修订只有两个最小形状且聊天路径零改动：节点 1 空范围终止（`graph.py:302-306`）与授权池对推导（`graph.py:433-451`、`repository.py:94-95`）仅在「上下文无店铺授权且请求不含 `shop_sellable`」时走仅池路径（新 repository 辅助按池授权集直接派生 `(namespace, pool_id)`，授权面与 `pool_connections` 一致）；渠道快照读闸（`graph.py:613-620`）保持上下文驱动；原库存图回归零回退，`inventory_scope_empty` 在聊天路径语义不变。
- [ ] 确认未核验层级不能触发也不能解除：缺扫描保持 open/acknowledged（不推进 `last_observed_at`），来源/能力退场只走 `suppressed`；`resolved` 仅当同层级、已核验、fresh 且完整。
- [ ] 确认 `inventory_snapshot_missing` 按「本轮真正在跑的范围」一致归属：有池缺 `inventory/pools[]` 声明 ⇒ 实物侧，去掉 `physical_total`；全部在册池有声明且本轮 `shop_sellable` 在跑（`excluded_scope[].reason == "shop_not_synced"` 可共定位）⇒ 只算渠道侧；全部在册池有声明但本轮是空店铺投影 ⇒ 该码没有合法产生点，按投影契约违约当聚合码（本轮去掉全部已核验层级并照常持久化），绝不解释成“实物不完整”、绝不据此改宽请求或投影；`inventory_channel_snapshot_missing` 同一规则。不得再把它归为“只属于实物层”，也不得让任何店铺侧信号把已核验实物层赶出 `complete_levels`。
- [ ] 确认策略请求层级全部未核验时 CLI 在跑图前以 disabled 成功退出：零 graph 调用、零 DB 事务、零 outbox，也不发“无法判断”通知；缺层信息只进固定诊断与 `inventory_monitor_level_unverified_total{level}`。
- [ ] 确认本轮实现只跑本机：`*_test` 库、离线/stub 测试、`INVENTORY_MONITOR_ENABLED=false` 默认、迁移 023 只在本机测试库；未执行真实 API 调用、真实 provider smoke/live、目标环境迁移、部署、计划任务注册与一周试用。
- [ ] 确认 `CHANNEL_SOURCE_KINDS` 与拼多多永久排除未因按层级放宽而被改：`erp` 仍不是 `shop_sellable` 的合法来源种类，注册表仍无任何 PDD 通道。
- [ ] 确认 scheduler 只调用 one-shot CLI（文档阶段），仓库和 Codex 均无后台自动化/常驻循环。
- [ ] 确认 alert 生命周期、dedupe、cooldown、generation 和 resolution gate 与 fixture 完全一致。
- [ ] 确认 stale/incomplete/unsupported/unit conflict 不会 resolved 或发“已恢复”；同样确认**展示投影截断的轮次**（`inventory.truncated` 为 true 或出现 `inventory_display_truncated`）不进入 `complete_levels`，既不 triggered 也不 resolved，且不把 500 行预算描述成已全量持久化。
- [ ] 确认缺一格作用域引用的 graph 行（实物缺 `pool_ref`/`warehouse_ref`、渠道缺 `shop_ref`）不进入 `MonitorScan.rows`、不生成去重键、不触发也不解除，只增加 `monitor_row_scope_missing` 诊断与 `inventory_monitor_rows_dropped_total{level,reason}`；runner 不得为它借另一层级引用、拼默认作用域或补零。
- [ ] 确认 InventoryWatchGraph 在 Memory Store 运行，source Artifact + transition + outbox 单事务持久化。
- [ ] 确认 outbox 至少一次投递、notification 恰好一份、acknowledge 与 read/resolved 分离。
- [ ] 确认首版没有外部渠道、自动采购、调拨、改价或任意模型决策。
- [ ] 确认 `bi_monitor`、`bi_app`、`bi_sync` 权限分离且公开载荷无真实 ID/DSN/evidence。
