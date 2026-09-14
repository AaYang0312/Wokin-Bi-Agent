# Continuous Inventory Notifications Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在真实库存来源已核验时，由部署主机周期性调用一次性 CLI 复用 `InventoryWatchGraph`，以稳定去重状态机生成应用内库存通知；重复、并发、崩溃、stale 和不完整扫描都不能误报恢复或重复首发。

**Architecture:** 专用 `bi_monitor` 身份只读获准库存视图并写监控表/运行审计；CLI 每次加载已批准策略与授权范围，以 `MemoryQueryRunStore` 执行现有 InventoryWatchGraph，得到不可变安全来源 Artifact。repository 在单事务中持久化来源 run/artifact、执行 `open/acknowledged/resolved/suppressed` 状态迁移并写 PostgreSQL outbox；独立投递步骤幂等写入应用内 notification center。没有邮件、短信、IM、webhook、Codex automation 或自动采购动作。

**Tech Stack:** Python 3.11+、Pydantic 2、psycopg 3、PostgreSQL 17、FastAPI、React 19、TypeScript、Windows Task Scheduler / systemd timer 调用 CLI、现有 `unittest` / Vitest。

**Spec:** [Task 11 后置子项目总设计](../specs/2026-09-14-post-task11-subprojects-design.md) §3–4、§9–12。

## Global Constraints

- Task 11 发布门禁必须通过；此外必须有日期化 inventory source acceptance，证明 physical/channel 中策略依赖的每个层级都有登记来源、freshness policy、完整扫描凭据和生产对账。
- 缺少来源验收、来源被撤销、策略版本失效或 `INVENTORY_MONITOR_ENABLED=false` 时 CLI 以 disabled 成功退出，不产生告警或“无法判断”通知。
- 调度器只调用一次性 `python -m bi_agent.monitoring.cli run`；不在 API 进程建循环，不创建 Codex automation。
- 首版渠道只有 PostgreSQL outbox + 应用内通知中心；不包含邮件、短信、企业微信、Slack、Teams、任意 webhook 或外部凭证。
- 告警可由 `open → acknowledged`；fresh/complete 的恢复扫描可将 open 或 acknowledged 置为 resolved；open/acknowledged 也可因策略失效置为 suppressed。acknowledge 不等于恢复，resolved 不能人工设置。
- 只有新的、fresh、完整且覆盖相同 policy scope 的扫描能 resolved；stale、缺页、缺层级、单位冲突或 unsupported 只保留原状态并写诊断，不发送恢复。
- 相同事实重复扫描只更新 `last_observed_at`；冷却期后持续异常才发 retriggered；恢复后再次下降创建新的 alert generation。
- dedupe key 只由 policy version、库存层级、SKU ref、opaque scope ref 和 rule code 的 canonical JSON 计算；不含真实 ID、显示名、数量或时间。
- 所有数量、阈值、状态和变化由确定性代码计算；模型不参与调度、状态迁移、文案、投递或 acknowledge。
- `bi_monitor` 与 `bi_app`、`bi_sync` 分离；没有底表任意读写、聊天写入、外部网络或来源凭证权限。
- 每次 run 保持 30 秒总 deadline；策略数上限 100、每策略 500 行、outbox batch 100。
- 本计划固定使用迁移 `023_continuous_inventory_notifications.sql`。

## Execution Preflight

- [ ] 验证双门禁、来源能力和迁移位置。

Run:

```powershell
git status --short --branch
rg --files docs/superpowers/research | rg '(task-11-release-acceptance|inventory-source-acceptance)\.md$'
rg -n 'physical|channel|freshness|scan_complete|reconciliation|enabled' docs/superpowers/research/2026-09-14-inventory-source-acceptance.md
Get-ChildItem backend/sql -Filter '*.sql' | Sort-Object Name | Select-Object -ExpandProperty Name
```

Expected: 两份验收文件存在；source acceptance 逐层写明 verified evidence；迁移 023 未被占用。缺少库存验收时预检明确 FAIL 并停止 Task 1 实现。

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
- Produces: `MonitorSettings`、`MonitorRunRequest`、`MonitorPolicy`、`MonitorScan`、`AlertTransition`、`load_monitor_settings(env)`、`assert_monitor_sources_verified(policy, registrations)`。

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

    def test_policy_cannot_run_without_verified_required_levels(self):
        from bi_agent.monitoring.source_gate import assert_monitor_sources_verified
        with self.assertRaisesRegex(ValueError, "monitor_source_unverified"):
            assert_monitor_sources_verified(physical_and_channel_policy(), registrations={})
```

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

class MonitorScan(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    policy_ref: str
    source_artifact_payload: dict[str, object]
    source_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    data_as_of: datetime
    fresh: bool
    complete_levels: tuple[str, ...]
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

- [ ] **Step 5: 实现来源 gate。**

```python
def assert_monitor_sources_verified(
    policy: MonitorPolicy,
    registrations: Mapping[str, InventorySourceRegistration],
) -> None:
    for level in policy.levels:
        entry = registrations.get(level)
        if (entry is None or not entry.evidence or entry.max_age_seconds <= 0
                or not entry.scan_complete_supported
                or entry.production_reconciled_at is None):
            raise ValueError("monitor_source_unverified")
```

`InventorySourceRegistration` 增加必填 `scan_complete_supported: bool` 与 `production_reconciled_at: datetime | None`；Task 10 现有测试 fixture 显式填值，生产注册为空时仍无 verified source。任一策略层级失败使整个 policy disabled，不允许仅靠另一层级运行后宣称完整。

- [ ] **Step 6: 运行契约/配置测试并提交。**

```powershell
uv run --locked python -m unittest tests.test_monitoring.MonitorContractTests tests.test_core.ConfigTests -v
git add backend/bi_agent/monitoring backend/bi_agent/config.py backend/bi_agent/inventory/rules.py backend/tests/test_monitoring.py backend/tests/test_core.py backend/tests/test_inventory.py .env.example
git commit -m "feat: define inventory monitor boundaries"
```

Expected: gate off 不要求秘密；gate on 独立身份；未核验层级全部 fail closed。

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

payload CHECK 使用 `bi.inventory_notification_payload_safe(jsonb)` 不可变递归函数，拒绝 `shop_id/pool_id/warehouse_id/erp_sku_id/subject_id/dsn/evidence/scan_evidence` 键。迁移另建 `bi.commit_inventory_monitor_scan(policy_ref text, source_payload jsonb, source_fingerprint text, data_as_of timestamptz, decisions jsonb, observed_at timestamptz) RETURNS jsonb`，设为 `SECURITY DEFINER SET search_path = pg_catalog, bi`；函数只接受通过上述 validator 的 `inventory_alerts` payload/decision 码表，使用固定 service chat/message，并在锁内计算全局 attempt_no。

迁移同时创建 `bi.deliver_inventory_outbox(delivery_at timestamptz, batch_limit integer) RETURNS jsonb`、`bi.mark_inventory_notification_read(notification_ref text, actor_subject_id text)` 和 `bi.acknowledge_inventory_alert(alert_ref text, actor_subject_id text)` 三个固定签名的 SECURITY DEFINER 函数；各函数固定 `search_path`、重验 owner/status/payload，且 revoke PUBLIC execute。

`bi_monitor` 只获得 reporting 库存视图 SELECT、monitor policy SELECT 以及 commit/deliver 两函数 EXECUTE；不给 alert/outbox/query runtime/chat/来源底表的直接写权限。`bi_app` 只读按 owner 过滤的 notification/alert API view，并只可执行 read/ack 两函数，不直接改表。

- [ ] **Step 4: 实现单事务 commit_scan。**

`commit_scan()` 先用 `NewArtifact`、`validate_artifact_payload` 和 `AlertDecision` 重验输入，再在一个 `with conn.transaction():` 内只调用 `SELECT bi.commit_inventory_monitor_scan(...)`。数据库函数创建 service query run → 保存 immutable `inventory_alerts` source artifact → 对每个 dedupe key `pg_advisory_xact_lock(hashtextextended(key, 0))` → 锁 active alert → 应用预计算 decision → 插 event → 对 triggered/retriggered/resolved 插 outbox。updated 无显著变化只改 last_observed，不插 outbox。任何一步异常整批回滚。

必须复用 `NewQueryRun`、`NewArtifact`、`validate_artifact_payload` 和 `allows_artifact_type`；repository 不接受任意 JSON 或 SQL 片段。

- [ ] **Step 5: 运行迁移、角色和事务测试。**

```powershell
$env:TEST_DATABASE_URL='postgresql://postgres:postgres@127.0.0.1:5432/bi_agent_test'
uv run --locked python -m unittest tests.test_db tests.test_monitoring.MonitorRepositoryTests -v
```

Expected: 023 可重复应用；`bi_monitor` 权限矩阵精确；失败回滚五类行；并发 active unique 约束有效。

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
- Consumes: safe InventoryWatchGraph alert rows, `MonitorPolicy`, current `StoredAlert` tuple, scan freshness/completeness.
- Produces: `dedupe_key(policy_version, level, sku_ref, scope_ref, rule_code) -> str`、`decide_alert_transitions(scan, *, policy, active, now) -> tuple[AlertDecision, ...]`。

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
```

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
if not scan.fresh or not required_levels_complete(scan, policy):
    return observe_without_resolution(active, diagnostics=scan.diagnostics)
if row_is_low(row):
    if current is None:
        return trigger(generation=last_generation + 1)
    if now - current.last_notified_at >= timedelta(seconds=policy.cooldown_seconds):
        return retrigger(current)
    return update(current, notify=False)
if current is not None:
    return resolve(current)
return no_decision()
```

策略从 repository 消失/撤销时只对其 active alerts 产生 next_status=suppressed、event_kind=updated、notify=false；不能生成 resolved。`row_is_low` 复用 inventory `quantity <= threshold` Decimal 规则，unconfigured/unit_conflict/data_anomaly 永不触发采购告警。

- [ ] **Step 5: 运行完整转移矩阵。**

fixture 至少覆盖 low、equal、normal、second drop、stale、missing page、physical missing、channel missing、unconfigured、unit conflict、shared pool、policy disabled、cooldown just-before/at-boundary、duplicate input rows。每条固定 previous/scan/now/expected transition/outbox count。

Run: `uv run --locked python -m unittest tests.test_monitoring.AlertStateMachineTests -v`

Expected: fixture 全通过；同一快照反复计算得到相同 idempotency key；三店共享 100 件只产生一个 physical key。

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
- Produces: `run_inventory_monitor(request: MonitorRunRequest, *, settings: MonitorSettings, now: datetime, deadline: float) -> tuple[AlertTransition, ...]` and console scripts `bi-inventory-monitor` / `bi-inventory-outbox`.

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
```

- [ ] **Step 2: 运行测试并确认 runner/CLI 不存在。**

Run: `uv run --locked python -m unittest tests.test_monitoring.MonitorRunnerTests -v`

Expected: FAIL with missing runner symbols。

- [ ] **Step 3: 提取 InventoryWatchGraph 安全执行入口。**

不改现有聊天行为；只让 graph 接受由服务端构造的 `DomainContext` + `MemoryQueryRunStore`。runner 将 policy 的 opaque refs 经服务端目录解析为 allowed shop/pool IDs，构造原有 `InventoryInspectionRequest`；threshold 仍只来自 `threshold_policy_ref`，不把默认阈值塞给 graph。

```python
memory_store = MemoryQueryRunStore(forbidden_values=real_scope_ids)
context = DomainContext(
    subject_id=policy.owner_subject_id, allowed_shop_ids=allowed_shop_ids,
    allowed_inventory_pool_ids=allowed_pool_ids, shop_refs=shop_refs,
    conn=readonly_conn, store=memory_store, chat_id=service_chat_id,
    user_message_id=service_message_id, root_request_id=service_request_id,
    now=now, deadline=deadline)
execution = run_inventory_graph(
    request=inventory_request(policy), context=context,
    tool_call_id=f"monitor-{run_ref}")
```

内存执行使用 023 预置的固定 service chat/message UUID；root request 与 tool call ref 由 `UUIDv5(NAMESPACE_URL, policy_ref + scheduled_at_utc_minute)` 稳定生成。真正 query run 的 attempt_no 由 `commit_inventory_monitor_scan` 在 advisory lock 内递增，避免两个 policy/process 撞唯一约束；不会创建或写入用户聊天。

- [ ] **Step 4: 只从已持久化形状构造 MonitorScan。**

从 memory store 找到唯一 `inventory_alerts` artifact，重新调用 `NewArtifact` / `validate_artifact_payload`；status 不是 ok/partial、Artifact 数不为 1、必要层级 diagnostics 有 stale/incomplete/unsupported 时 `fresh=false` 或 complete_levels 缺项。canonical public payload 生成 source fingerprint；然后加载 active state、调用纯状态机，最后唯一一次 `repository.commit_scan()`。

任何 graph exception、deadline exhausted 或 contract error 只记录固定 reason metric，保持 active alerts 不变且不写 outbox。

- [ ] **Step 5: 实现一次性 CLI。**

`pyproject.toml`：

```toml
[project.scripts]
bi-inventory-monitor = "bi_agent.monitoring.cli:run_main"
bi-inventory-outbox = "bi_agent.monitoring.cli:deliver_main"
```

`run_main()` 加载 `MonitorSettings`，enabled=false 打印单行 `inventory_monitor disabled` 并 exit 0；逐 policy 运行但共享总上限 30 秒，任一 policy 失败 exit 1 并只输出 policy ref + fixed code。命令无 daemon/loop/HTTP server 参数。

- [ ] **Step 6: 运行 runner、CLI 和原库存图回归。**

```powershell
uv run --locked python -m unittest tests.test_monitoring.MonitorRunnerTests tests.test_inventory -v
$env:INVENTORY_MONITOR_ENABLED='false'
uv run --locked bi-inventory-monitor
```

Expected: graph 使用内存 Store；repository 一次原子 commit；disabled CLI exit 0 且零 DB 连接；原库存测试零回退。

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

GET 只返回当前 subject 未删除的 notification projection：`notification_ref/alert_ref/event_kind/status/level/sku_ref/scope_ref/quantity/threshold/unit/data_as_of/reason_code/read_at/created_at`。不返回 owner subject、真实 ID、evidence、完整来源 payload。

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
```

- [ ] **Step 6: 实现前端中心。**

`App.tsx` 启动后 fetch 最多 100 条并每 60 秒在页面可见时刷新；这是 UI polling，不是业务调度。按 event kind 显示“首次触发/持续异常/已恢复”，ack 与 read 两个动作分开；数量、阈值、单位、snapshot time、scope 和 reason 常显。未知 payload 拒绝渲染并计客户端错误，不猜商品名。

- [ ] **Step 7: 运行投递、API 和前端测试并提交。**

```powershell
Set-Location backend
uv run --locked python -m unittest tests.test_monitoring.OutboxDeliveryTests tests.test_api.NotificationApiTests -v
Set-Location ..\frontend
npm test -- --run src/components/NotificationCenter.test.tsx src/api.test.ts
git add backend/bi_agent/monitoring/delivery.py backend/bi_agent/monitoring/api.py backend/bi_agent/api.py backend/tests/test_monitoring.py backend/tests/test_api.py frontend/src/types.ts frontend/src/api.ts frontend/src/components/NotificationCenter.tsx frontend/src/components/NotificationCenter.test.tsx frontend/src/App.tsx frontend/src/styles.css
git commit -m "feat: deliver in-app inventory notifications"
```

Expected: outbox 可重复投递且通知唯一；跨 subject 返回 404；ack 不显示 resolved；前端测试 PASS。

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
- Produces: production scheduling/rollback evidence; no new runtime interface.

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

Expected: 后端、DB-enabled、前端和 build 零失败；DB-enabled 0 skip；26/26；并发矩阵只有一个 active/trigger/outbox。

- [ ] **Step 3: 做一轮 one-shot staging smoke。**

```powershell
Set-Location backend
uv run --locked bi-inventory-monitor
uv run --locked bi-inventory-outbox
uv run --locked bi-inventory-monitor
uv run --locked bi-inventory-outbox
```

Expected: 第一轮按 staging fixture 产生一个 triggered；第二轮仅 updated、无第二个首次通知；日志只含 policy ref、run ref、固定状态码和计数。

- [ ] **Step 4: 写部署主机调度与回滚。**

runbook 给出两种受控配置：Windows Task Scheduler 每 15 分钟运行 `uv run --locked bi-inventory-monitor` 后运行 `uv run --locked bi-inventory-outbox`；systemd timer 使用同两条 ExecStart。两者都设置单实例、30 秒超时、非交互服务账号、失败退出码告警和工作目录；明确“不创建 Codex automation”。回滚先置 `INVENTORY_MONITOR_ENABLED=false`，停 scheduler，再保留历史表审计；不得删除 open alert 或伪造 resolved。

- [ ] **Step 5: 记录验收与指标。**

验收文件写 Task 11/source acceptance 引用、commit、023、角色权限、完整命令/通过数、state fixture、threshold equal、shared pool、stale/incomplete、generation 2、并发、崩溃、outbox 重投、跨 subject、gate disabled 和 staging 两轮结果。

metrics 增加 `inventory_monitor_runs_total{status}`、`inventory_alert_transitions_total{event_kind}`、`inventory_monitor_skipped_total{reason}`、`notification_outbox_pending`、`notification_delivery_attempts_total{status}`、`inventory_alerts_active{status}`；labels 不含 policy ref、SKU、subject 或真实 ID。

- [ ] **Step 6: 检查文档、敏感词和差异。**

```powershell
rg -n 'INVENTORY_MONITOR_ENABLED|bi_monitor|bi-inventory-monitor|bi-inventory-outbox|Task Scheduler|systemd|Codex automation|023_continuous_inventory' README.md docs/runbook.md docs/metrics.md docs/superpowers/research/2026-09-14-continuous-inventory-notifications-acceptance.md
rg -n 'shop_id|pool_id|warehouse_id|erp_sku_id|dsn|evidence|scan_evidence' docs/superpowers/research/2026-09-14-continuous-inventory-notifications-acceptance.md
git diff --check
git status --short
```

Expected: 第一条能定位门禁、调度、回滚和迁移；第二条无输出；diff 无空白错误；仅计划内文件待提交。

- [ ] **Step 7: 提交验收和运维文档。**

```powershell
git add backend/tests/test_monitoring.py backend/tests/test_db.py backend/tests/test_api.py frontend/src/components/NotificationCenter.test.tsx README.md docs/runbook.md docs/metrics.md docs/superpowers/research/2026-09-14-continuous-inventory-notifications-acceptance.md
git commit -m "docs: accept continuous inventory notifications"
```

## Final Verification

- [ ] 确认 Task 11 与真实库存来源验收都通过；缺任一证据时生产 monitor 保持 disabled。
- [ ] 确认 scheduler 只调用 one-shot CLI，仓库和 Codex 均无后台自动化/常驻循环。
- [ ] 确认 alert 生命周期、dedupe、cooldown、generation 和 resolution gate 与 fixture 完全一致。
- [ ] 确认 stale/incomplete/unsupported/unit conflict 不会 resolved 或发“已恢复”。
- [ ] 确认 InventoryWatchGraph 在 Memory Store 运行，source Artifact + transition + outbox 单事务持久化。
- [ ] 确认 outbox 至少一次投递、notification 恰好一份、acknowledge 与 read/resolved 分离。
- [ ] 确认首版没有外部渠道、自动采购、调拨、改价或任意模型决策。
- [ ] 确认 `bi_monitor`、`bi_app`、`bi_sync` 权限分离且公开载荷无真实 ID/DSN/evidence。
