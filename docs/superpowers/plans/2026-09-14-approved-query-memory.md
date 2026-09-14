# Approved Query Memory Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将人工批准、已脱敏且版本兼容的规范化查询样例提供给路由和参数规范化作最多 3 条 few-shot，同时保证模型、成功运行和用户纠错都不能自动改变长期记忆。

**Architecture:** PostgreSQL 保存候选、审批修订和不可变事件；服务端从成功运行引用生成去标识化草稿，只有授权审核者能执行 `draft → approved → superseded/revoked`。检索先做 domain、授权作用域和 `VersionSet` 精确过滤，再做确定性词项排序；Agent 只收到稳定 ref、槽位化问题和规范化请求，不收到原始聊天、真实实体、SQL 或结果行。

**Tech Stack:** Python 3.11+、Pydantic 2、psycopg 3、PostgreSQL 17、FastAPI、React 19、TypeScript、现有 `unittest` / Vitest。

**Spec:** [Task 11 后置子项目总设计](../specs/2026-09-14-post-task11-subprojects-design.md) §3–4、§7、§10–12；前置计划：[Semantic Catalog and Schema Retrieval](2026-09-14-semantic-catalog-and-schema-retrieval.md) 与 [Controlled SQL Exploration](2026-09-14-controlled-sql-exploration.md)。

## Global Constraints

- Task 11、语义目录和受控 SQL 三份日期化验收必须已经通过；任一记录缺失时停止实现。
- 生命周期只允许 `draft → approved → superseded`、`draft → revoked`、`approved → revoked`；撤销和替换不可逆，重新启用必须新建草稿。
- 只有 `APPROVED_QUERY_MEMORY_ENABLED=true` 且状态为 approved、domain/授权范围/全部版本精确匹配的记录可被检索。
- 模型不能创建、批准、撤销或替换样例；成功运行、用户点赞和自然语言纠错都只能成为人工审核候选。
- 不保存原始聊天、真实店铺/商品/SKU 名称、真实主键、SQL 原文、SQL 结果、DSN、密钥或错误堆栈。
- 目标价、阈值、预算、日期区间和店铺选择必须保存为槽位定义，不得保存为长期常量。
- 记忆只能改善 Tool/参数/语义 ref 选择，不得覆盖服务端身份、能力、coverage、basis、来源、授权或固定 Tool 优先级。
- 检索默认最多 3 条；没有合法样例时返回空元组并保持现有路由行为。
- 本计划固定使用迁移 `021_approved_query_memory.sql`；`020_controlled_sql_exploration.sql` 必须已存在并已应用。

## Execution Preflight

- [ ] 验证三重门禁、迁移顺序和工作树边界。

Run:

```powershell
git status --short --branch
rg --files docs/superpowers/research | rg '(task-11-release-acceptance|semantic-catalog-acceptance|controlled-sql-acceptance)\.md$'
Get-ChildItem backend/sql -Filter '*.sql' | Sort-Object Name | Select-Object -Last 3 -ExpandProperty Name
```

Expected: 三份验收文件均存在；最后一个迁移是 `020_controlled_sql_exploration.sql`；只提交本计划列出的文件，不覆盖无关改动。

---

### Task 1: Strict Contracts, Feature Gate, and Migration

**Files:**
- Create: `backend/bi_agent/query_memory/__init__.py`
- Create: `backend/bi_agent/query_memory/models.py`
- Create: `backend/sql/021_approved_query_memory.sql`
- Modify: `backend/bi_agent/config.py`
- Modify: `.env.example`
- Create: `backend/tests/test_query_memory.py`
- Modify: `backend/tests/test_core.py`
- Modify: `backend/tests/test_db.py`

**Interfaces:**
- Consumes: `backend/bi_agent/runtime/versions.py::VersionSet` and stable `ent-` / semantic refs.
- Produces: `ApprovalStatus`、`QuerySlot`、`ApprovedExample`、`StoredMemoryRecord`、`ApprovalCommand`、`AppSettings.approved_query_memory_enabled`、`AppSettings.approver_subjects`。

- [ ] **Step 1: 写状态、槽位和值污染的失败测试。**

在 `backend/tests/test_query_memory.py` 写入：

```python
import unittest
from pydantic import ValidationError


class QueryMemoryContractTests(unittest.TestCase):
    def test_example_accepts_slots_and_rejects_bound_business_values(self):
        from bi_agent.query_memory.models import ApprovedExample, QuerySlot
        from bi_agent.runtime.versions import VersionSet

        versions = VersionSet(
            schema_version="reporting/2026-09-14.1", semantic_catalog_version="semantic/2026-09-14.1",
            data_catalog_version=7, metric_version="metrics/2026-09-12.1",
            policy_version="multi-source-policy/2026-09-12.1",
            source_registry_version="sources/2026-09-12.1",
            graph_version="business_query-graph/2026-09-11.1")
        example = ApprovedExample(
            example_ref="mem-approved-001", domain="controlled_exploration",
            intent_signature="cost-by-shop-and-window",
            question_template="比较 {shop_scope} 在 {date_window} 的成本",
            slots=(QuerySlot(name="shop_scope", kind="entity_scope"),
                   QuerySlot(name="date_window", kind="date_window")),
            normalized_request={"requested_metric_refs": ["metric-cost-total"]},
            expected_tool="explore_business_data", version_requirements=versions,
            approval_revision=1)
        self.assertEqual(example.slots[0].name, "shop_scope")
        with self.assertRaises(ValidationError):
            ApprovedExample(**example.model_dump() | {
                "normalized_request": {"start": "2026-09-01", "target_price": "99.00"}})
```

- [ ] **Step 2: 运行测试并确认查询记忆模块不存在。**

Run:

```powershell
Set-Location backend
uv run --locked python -m unittest tests.test_query_memory.QueryMemoryContractTests -v
```

Expected: FAIL with `ModuleNotFoundError: bi_agent.query_memory`。

- [ ] **Step 3: 实现严格模型与净化常量。**

`models.py` 定义以下完整公共字段，并用 `extra="forbid"`、`frozen=True`、`hide_input_in_errors=True`：

```python
from typing import Literal
from uuid import UUID
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from bi_agent.runtime.versions import VersionSet

ApprovalStatus = Literal["draft", "approved", "superseded", "revoked"]
SlotKind = Literal["entity_scope", "date_window", "target_price", "threshold", "budget"]
FORBIDDEN_VALUE_KEYS = frozenset({
    "start", "end", "date", "shop_id", "subject_id", "target_price",
    "threshold", "budget", "sql_text", "rows", "result", "prompt"})

class QuerySlot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    name: str = Field(pattern=r"^[a-z][a-z0-9_]{0,31}$")
    kind: SlotKind

class ApprovedExample(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)
    example_ref: str = Field(pattern=r"^mem-[a-z0-9-]{1,60}$")
    domain: str = Field(pattern=r"^[a-z][a-z0-9_]{0,31}$")
    intent_signature: str = Field(pattern=r"^[a-z][a-z0-9-]{0,79}$")
    question_template: str = Field(min_length=1, max_length=400)
    slots: tuple[QuerySlot, ...]
    normalized_request: dict[str, object]
    expected_tool: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    version_requirements: VersionSet
    approval_revision: int = Field(ge=1)

    @model_validator(mode="after")
    def safe_template_and_request(self) -> "ApprovedExample":
        if FORBIDDEN_VALUE_KEYS & self.normalized_request.keys():
            raise ValueError("memory_contains_bound_business_value")
        expected = {"{" + slot.name + "}" for slot in self.slots}
        if any(token not in self.question_template for token in expected):
            raise ValueError("memory_slot_missing_from_template")
        if len({slot.name for slot in self.slots}) != len(self.slots):
            raise ValueError("memory_slot_duplicate")
        return self

class StoredMemoryRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)
    example_ref: str
    source_run_id: UUID
    owner_subject_id: str
    domain: str
    intent_signature: str
    question_template: str
    slots: tuple[QuerySlot, ...]
    normalized_request: dict[str, object]
    expected_tool: str
    version_requirements: VersionSet
    authorization_refs: tuple[str, ...]
    status: ApprovalStatus
    approval_revision: int = Field(ge=0)

    def as_approved(self) -> ApprovedExample:
        if self.status != "approved" or self.approval_revision < 1:
            raise ValueError("memory_not_approved")
        return ApprovedExample(**self.model_dump(exclude={
            "source_run_id", "owner_subject_id", "authorization_refs", "status"}))

class ApprovalCommand(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    action: Literal["approve", "revoke", "supersede"]
    reason: str = Field(min_length=3, max_length=500)
    replacement_ref: str | None = Field(default=None, pattern=r"^mem-[a-z0-9-]{1,60}$")

    @model_validator(mode="after")
    def replacement_pair(self) -> "ApprovalCommand":
        if (self.action == "supersede") != (self.replacement_ref is not None):
            raise ValueError("replacement_ref_action_mismatch")
        return self
```

问题模板额外拒绝 UUID、连续 6 位数字、SQL 关键字和 `ent-` 之外的内部 ref；递归遍历 `normalized_request` 的所有键和值，字符串只能是已登记业务码或稳定 ref，不能含日期、金额字面量或真实名称。

- [ ] **Step 4: 写 021 迁移。**

`backend/sql/021_approved_query_memory.sql` 创建：

```sql
DO $$ BEGIN
  CREATE ROLE bi_approver NOLOGIN;
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

CREATE TABLE bi.approved_query_examples (
  example_ref text PRIMARY KEY,
  source_run_id uuid NOT NULL REFERENCES bi.query_runs(id),
  owner_subject_id text NOT NULL,
  domain text NOT NULL,
  intent_signature text NOT NULL,
  question_template text NOT NULL,
  slots jsonb NOT NULL,
  normalized_request jsonb NOT NULL,
  expected_tool text NOT NULL,
  version_requirements jsonb NOT NULL,
  authorization_refs text[] NOT NULL,
  status text NOT NULL DEFAULT 'draft',
  approval_revision integer NOT NULL DEFAULT 0,
  created_by text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT approved_query_status CHECK (status IN ('draft','approved','superseded','revoked')),
  CONSTRAINT approved_query_revision CHECK (approval_revision >= 0),
  CONSTRAINT approved_query_refs CHECK (cardinality(authorization_refs) > 0),
  CONSTRAINT approved_query_no_bound_values CHECK (
    NOT (normalized_request ?| ARRAY['start','end','date','shop_id','subject_id',
      'target_price','threshold','budget','sql_text','rows','result','prompt']))
);

CREATE TABLE bi.approved_query_events (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  example_ref text NOT NULL REFERENCES bi.approved_query_examples(example_ref),
  revision integer NOT NULL,
  actor_subject_id text NOT NULL,
  event_kind text NOT NULL,
  reason text NOT NULL,
  replacement_ref text REFERENCES bi.approved_query_examples(example_ref),
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (example_ref, revision),
  CONSTRAINT approved_query_event_kind CHECK (
    event_kind IN ('drafted','approved','superseded','revoked')),
  CONSTRAINT approved_query_event_reason CHECK (btrim(reason) <> '')
);

CREATE INDEX approved_query_lookup_idx ON bi.approved_query_examples
  (status, domain, intent_signature);
REVOKE ALL ON bi.approved_query_examples, bi.approved_query_events FROM PUBLIC, bi_app;
GRANT SELECT, INSERT, UPDATE ON bi.approved_query_examples, bi.approved_query_events TO bi_approver;
GRANT SELECT ON bi.query_runs, bi.query_provenance TO bi_approver;
GRANT USAGE, SELECT ON SEQUENCE bi.approved_query_events_id_seq TO bi_approver;

CREATE OR REPLACE VIEW reporting.v_approved_query_examples AS
SELECT example_ref, owner_subject_id, domain, intent_signature, question_template,
       slots, normalized_request, expected_tool, version_requirements,
       authorization_refs, approval_revision
FROM bi.approved_query_examples
WHERE status = 'approved';
REVOKE ALL ON reporting.v_approved_query_examples FROM PUBLIC;
GRANT SELECT ON reporting.v_approved_query_examples TO bi_app;
```

应用 API 使用属于 `bi_approver` 的单独审核 DSN 写入；聊天检索只读 projection view，`bi_app` 保持无底表和写权限。将新表加入迁移的现有 schema-version 记录方式，并在 DB 测试中证明 `bi_app` 的底表 SELECT/INSERT 均被拒、只可读 view。

- [ ] **Step 5: 添加默认关闭配置。**

`AppSettings` 增加：

```python
approved_query_memory_enabled: bool = False
approver_subjects: frozenset[str] = frozenset()
approver_dsn: SecretStr | None = None
```

`load_app_settings()` 只接受 `APPROVED_QUERY_MEMORY_ENABLED=true|false`；启用时要求 `APP_APPROVER_SUBJECTS` 非空、`BI_APPROVER_DSN` 存在，且 approver DSN 不等于 `BI_APP_DSN`。`.env.example` 加入默认关闭值和空审核者示例。

- [ ] **Step 6: 运行契约、配置和迁移权限测试。**

Run:

```powershell
uv run --locked python -m unittest tests.test_query_memory.QueryMemoryContractTests tests.test_core.ConfigTests -v
$env:TEST_DATABASE_URL='postgresql://postgres:postgres@127.0.0.1:5432/bi_agent_test'
uv run --locked python -m unittest tests.test_db -v
```

Expected: contract/config PASS；DB 测试证明 021 可重复应用、`bi_sync` 可写、`bi_app` 不可写。

- [ ] **Step 7: 提交契约和迁移。**

```powershell
git add backend/bi_agent/query_memory backend/bi_agent/config.py backend/sql/021_approved_query_memory.sql backend/tests/test_query_memory.py backend/tests/test_core.py backend/tests/test_db.py .env.example
git commit -m "feat: define approved query memory contracts"
```

---

### Task 2: Draft Builder and Human-Only Lifecycle Repository

**Files:**
- Create: `backend/bi_agent/query_memory/sanitize.py`
- Create: `backend/bi_agent/query_memory/repository.py`
- Modify: `backend/bi_agent/query_memory/__init__.py`
- Modify: `backend/tests/test_query_memory.py`
- Modify: `backend/tests/fakeconn.py`
- Modify: `backend/tests/test_runtime_db.py`

**Interfaces:**
- Consumes: succeeded `bi.query_runs` by opaque `run_id`, its `normalized_request` and `QueryProvenance`; never consumes chat message text.
- Produces: `build_draft_from_run(conn, *, run_id: UUID, owner_subject_id: str, question_template: str, slots: tuple[QuerySlot, ...], created_by: str) -> StoredMemoryRecord`、`QueryMemoryRepository.transition(example_ref: str, *, command: ApprovalCommand, actor_subject_id: str) -> StoredMemoryRecord`。

- [ ] **Step 1: 写草稿来源和状态机失败测试。**

```python
class QueryMemoryLifecycleTests(unittest.TestCase):
    def test_only_succeeded_owned_run_can_become_draft(self):
        from bi_agent.query_memory.repository import build_draft_from_run
        with self.assertRaisesRegex(ValueError, "memory_source_run_not_eligible"):
            build_draft_from_run(
                failed_run_conn(owner="subject-a"), run_id=RUN_ID,
                owner_subject_id="subject-a",
                question_template="比较 {shop_scope} 在 {date_window} 的成本",
                slots=valid_slots(), created_by="reviewer-a")

    def test_approved_cannot_return_to_draft(self):
        repository = memory_repository(status="approved", revision=1)
        with self.assertRaisesRegex(ValueError, "memory_transition_invalid"):
            repository.transition(
                "mem-approved-001",
                command=ApprovalCommand(action="approve", reason="cannot approve twice"),
                actor_subject_id="reviewer-a")
```

- [ ] **Step 2: 运行测试并确认 builder/repository 尚不存在。**

Run: `uv run --locked python -m unittest tests.test_query_memory.QueryMemoryLifecycleTests -v`

Expected: FAIL with missing `bi_agent.query_memory.repository` symbols。

- [ ] **Step 3: 实现运行引用净化和槽位化。**

`sanitize.py` 实现：

```python
def sanitize_normalized_request(value: dict[str, object], *, slots: tuple[QuerySlot, ...]) -> dict[str, object]:
    clean = deepcopy(value)
    for key in ("start", "end", "date", "shop_refs", "target_price", "thresholds", "budget"):
        clean.pop(key, None)
    allowed = {
        "metrics", "requested_metric_refs", "group_by_field_refs", "sales_basis",
        "profit_basis", "currency", "compare", "scope_mode", "report_kind",
        "platforms", "product_ref", "sku_refs", "semantic_selection"}
    if set(clean) - allowed:
        raise ValueError("memory_request_field_unapproved")
    validate_stable_refs_and_codes(clean)
    return clean
```

`build_draft_from_run()` 必须在一条查询中检查 run 为 `succeeded`、`subject_id` 等于 owner、provenance 完整、domain 已登记；从 provenance 构造 `VersionSet`，调用净化器后再插入 draft 与 `drafted` 事件。禁止读取 `bi.chat_messages` 和 `bi.query_artifacts.payload`。

- [ ] **Step 4: 实现锁行状态迁移。**

`QueryMemoryRepository.transition()` 使用一个事务、`SELECT ... FOR UPDATE` 和 revision CAS：

```python
ALLOWED_TRANSITIONS = {
    "draft": frozenset({"approved", "revoked"}),
    "approved": frozenset({"superseded", "revoked"}),
    "superseded": frozenset(),
    "revoked": frozenset(),
}

def next_status(command: ApprovalCommand) -> str:
    return {"approve": "approved", "revoke": "revoked",
            "supersede": "superseded"}[command.action]
```

锁内验证 replacement 已存在、状态为 approved、domain 相同且不等于自身；更新 revision 与状态后写同 revision 事件。任何 CAS 失败返回 `memory_revision_conflict`，不得重放审批。

- [ ] **Step 5: 运行内存替身和真实库生命周期测试。**

```powershell
uv run --locked python -m unittest tests.test_query_memory.QueryMemoryLifecycleTests -v
$env:TEST_DATABASE_URL='postgresql://postgres:postgres@127.0.0.1:5432/bi_agent_test'
uv run --locked python -m unittest tests.test_runtime_db -v
```

Expected: 非成功/跨 owner 运行为 0 草稿；并发批准只有一个成功；每次状态变化恰有一个不可变事件。

- [ ] **Step 6: 提交生命周期切片。**

```powershell
git add backend/bi_agent/query_memory backend/tests/test_query_memory.py backend/tests/fakeconn.py backend/tests/test_runtime_db.py
git commit -m "feat: add human-approved query lifecycle"
```

---

### Task 3: Authorization- and Version-Filtered Retrieval

**Files:**
- Create: `backend/bi_agent/query_memory/retrieval.py`
- Modify: `backend/bi_agent/query_memory/__init__.py`
- Modify: `backend/tests/test_query_memory.py`
- Create: `backend/tests/fixtures/approved_memory_gold.jsonl`

**Interfaces:**
- Consumes: `DomainContext.subject_id`、`DomainContext.shop_refs`、`VersionSet` and approved examples.
- Produces: `retrieve_approved_examples(question: str, *, context: DomainContext, domain: str, current_versions: VersionSet, limit: int = 3) -> tuple[ApprovedExample, ...]`。

- [ ] **Step 1: 写 fail-closed 过滤和稳定排序测试。**

```python
class QueryMemoryRetrievalTests(unittest.TestCase):
    def test_retrieval_excludes_every_incompatible_candidate(self):
        from bi_agent.query_memory import retrieve_approved_examples
        result = retrieve_approved_examples(
            "按店比较成本", context=context_for("subject-a", refs={"ent-shop-a"}),
            domain="controlled_exploration", current_versions=CURRENT, limit=3)
        self.assertEqual([item.example_ref for item in result], ["mem-current-cost"])
        self.assertNotIn("mem-revoked", {item.example_ref for item in result})
        self.assertNotIn("mem-other-scope", {item.example_ref for item in result})
        self.assertNotIn("mem-old-version", {item.example_ref for item in result})

    def test_equal_scores_sort_by_ref(self):
        result = retrieve_fixture("比较店铺成本")
        self.assertEqual([item.example_ref for item in result], sorted(x.example_ref for x in result))
```

- [ ] **Step 2: 运行测试并确认检索函数不存在。**

Run: `uv run --locked python -m unittest tests.test_query_memory.QueryMemoryRetrievalTests -v`

Expected: FAIL with missing `retrieve_approved_examples`。

- [ ] **Step 3: 实现 SQL 前置过滤。**

`retrieval.py` 的 repository query 必须包含：

```sql
SELECT example_ref, domain, intent_signature, question_template, slots,
       normalized_request, expected_tool, version_requirements, approval_revision
FROM reporting.v_approved_query_examples
WHERE domain = %(domain)s
  AND owner_subject_id = %(subject_id)s
  AND authorization_refs <@ %(allowed_refs)s::text[]
  AND version_requirements = %(versions)s::jsonb
ORDER BY example_ref
LIMIT 100
```

`allowed_refs` 只从 `context.shop_refs` 的 opaque 值生成；空授权集合直接返回 `()`，不发 SQL。反序列化失败、未知 ref、重复 ref 或 revision 小于 1 时整条候选丢弃并记固定指标 `query_memory_candidate_invalid_total`，不把异常文本传给模型。

- [ ] **Step 4: 实现确定性词项排名。**

```python
def lexical_score(question: str, example: ApprovedExample) -> tuple[int, int, str]:
    question_tokens = tokenize(question)
    example_tokens = tokenize(example.intent_signature.replace("-", " ") + " " +
                              example.question_template)
    overlap = len(question_tokens & example_tokens)
    phrase_bonus = sum(2 for token in example_tokens if len(token) >= 2 and token in question)
    return (overlap, phrase_bonus, example.example_ref)

ranked = sorted(candidates, key=lambda item: (-lexical_score(question, item)[0],
                                               -lexical_score(question, item)[1],
                                               item.example_ref))[:limit]
```

tokenize 复用语义目录的 Unicode/英文/中文归一化规则；`limit` 只允许 1–3。得分为 0 时返回空，禁止用“最新三条”填充。

- [ ] **Step 5: 加入 30 题 gold set 并验证召回边界。**

`approved_memory_gold.jsonl` 每行固定 `question`、`subject_id`、`allowed_refs`、`domain`、`versions`、`expected_refs`；覆盖 approved、draft、revoked、superseded、跨 subject、跨 scope、七种版本单项失配、注入问题和零命中。

Run: `uv run --locked python -m unittest tests.test_query_memory.QueryMemoryRetrievalTests -v`

Expected: 30/30；所有非法候选召回为 0，同输入顺序变化不改变结果。

- [ ] **Step 6: 提交检索切片。**

```powershell
git add backend/bi_agent/query_memory backend/tests/test_query_memory.py backend/tests/fixtures/approved_memory_gold.jsonl
git commit -m "feat: retrieve compatible approved examples"
```

---

### Task 4: Reviewer API and Review Panel

**Files:**
- Create: `backend/bi_agent/query_memory/api.py`
- Modify: `backend/bi_agent/api.py`
- Modify: `backend/tests/test_api.py`
- Modify: `frontend/src/types.ts`
- Modify: `frontend/src/api.ts`
- Create: `frontend/src/components/QueryMemoryReview.tsx`
- Create: `frontend/src/components/QueryMemoryReview.test.tsx`
- Modify: `frontend/src/App.tsx`
- Modify: `frontend/src/styles.css`

**Interfaces:**
- Consumes: authenticated subject, `AppSettings.approver_subjects`, `QueryMemoryRepository`.
- Produces: `GET /api/query-memory/candidates`、`POST /api/query-memory/drafts`、`GET /api/query-memory/drafts`、`POST /api/query-memory/drafts/{example_ref}/approve`、`POST /api/query-memory/drafts/{example_ref}/revoke`、`POST /api/query-memory/drafts/{example_ref}/supersede` and reviewer-only UI.

- [ ] **Step 1: 写非审核者拒绝、载荷脱敏和来源保护 API 测试。**

```python
class QueryMemoryApiTests(unittest.TestCase):
    def test_non_reviewer_cannot_list_or_approve(self):
        response = self.client.get("/api/query-memory/drafts",
                                   headers={"X-Auth-Request-Sub": "subject-a"})
        self.assertEqual(response.status_code, 403)

    def test_reviewer_projection_has_no_raw_chat_or_sql(self):
        response = self.reviewer_client.get("/api/query-memory/drafts")
        body = response.json()
        rendered = json.dumps(body, ensure_ascii=False)
        for forbidden in ("sql_text", "rows", "subject_id", "shop_id", "raw_prompt"):
            self.assertNotIn(forbidden, rendered)
```

- [ ] **Step 2: 运行测试并确认 routes 不存在。**

Run: `uv run --locked python -m unittest tests.test_api.QueryMemoryApiTests -v`

Expected: FAIL with 404 for `/api/query-memory/drafts`。

- [ ] **Step 3: 实现审核者依赖和 API。**

`query_memory/api.py` 定义 `CandidateProjection` 和 `DraftProjection`。Candidate 字段只含 `source_run_ref/domain/normalized_request/expected_tool/version_requirements/created_at`；Draft 只含 `example_ref/domain/intent_signature/question_template/slots/normalized_request/expected_tool/version_requirements/status/approval_revision/source_run_ref`。`source_run_ref` 固定为 `run-<uuid>` opaque ref，server 解析后仍按 owner/status 复核；不返回聊天或 Artifact 内容。API 工厂增加：

```python
def require_approver(subject: Subject) -> str:
    if not settings.approved_query_memory_enabled:
        raise HTTPException(404, detail={"code": "not_found", "message": "功能未启用"})
    if subject not in settings.approver_subjects:
        raise HTTPException(403, detail={"code": "forbidden", "message": "无审核权限"})
    return subject
```

写请求继续要求 `WebWrite`；使用 `approver_dsn` 建短连接。GET candidates 只列最近 100 个 succeeded、有完整 provenance、尚无 draft 的运行；POST drafts 只收 `source_run_ref/question_template/slots` 并调用 Task 2 builder。action 路由在 body 中只收 `reason`，supersede 另收 `replacement_ref`。repository 的稳定错误映射为 404/409/422，不返回数据库异常。

- [ ] **Step 4: 写审核面板失败测试。**

```tsx
it('requires an explicit reason before approval', async () => {
  render(<QueryMemoryReview drafts={[draft]} onDecide={onDecide} />)
  await userEvent.click(screen.getByRole('button', { name: '批准' }))
  expect(onDecide).not.toHaveBeenCalled()
  expect(screen.getByText('请填写审核理由')).toBeInTheDocument()
})

it('renders slots but no bound business values', () => {
  render(<QueryMemoryReview drafts={[draft]} onDecide={onDecide} />)
  expect(screen.getByText('date_window')).toBeInTheDocument()
  expect(screen.queryByText('2026-09-01')).not.toBeInTheDocument()
})
```

- [ ] **Step 5: 实现前端 API 和独立审核面板。**

`types.ts` 增加 `QueryMemoryCandidate`、`QueryMemoryDraft` 与 `ApprovalAction`；`api.ts` 使用已有 `writeHeaders` 创建 draft 和发送 action。`QueryMemoryReview` 先从 candidate 选择来源、填写槽位化模板，再显示模板、槽位、工具、domain、版本和 revision；批准/撤销/替换均需显式理由，替换需选已批准 ref。只有后端返回审核能力时 `App.tsx` 才显示入口；404 隐藏面板，403 显示无权限，不把聊天 UI 当审核入口。

- [ ] **Step 6: 运行后端与前端测试。**

```powershell
Set-Location backend
uv run --locked python -m unittest tests.test_api.QueryMemoryApiTests -v
Set-Location ..\frontend
npm test -- --run src/components/QueryMemoryReview.test.tsx src/api.test.ts
```

Expected: 非审核者 403；feature gate 关闭 404；审核载荷无敏感字段；前端测试 PASS。

- [ ] **Step 7: 提交审核工作流。**

```powershell
git add backend/bi_agent/query_memory backend/bi_agent/api.py backend/tests/test_api.py frontend/src/types.ts frontend/src/api.ts frontend/src/components/QueryMemoryReview.tsx frontend/src/components/QueryMemoryReview.test.tsx frontend/src/App.tsx frontend/src/styles.css
git commit -m "feat: add approved memory review workflow"
```

---

### Task 5: Routing Integration Without Automatic Learning

**Files:**
- Create: `backend/bi_agent/query_memory/prompt.py`
- Modify: `backend/bi_agent/agent.py`
- Modify: `backend/bi_agent/response_summary.py`
- Modify: `backend/tests/test_core.py`
- Modify: `backend/tests/test_operator_workflows.py`
- Modify: `backend/tests/test_query_memory.py`

**Interfaces:**
- Consumes: `retrieve_approved_examples(...) -> tuple[ApprovedExample, ...]`.
- Produces: `approved_examples_payload(examples: tuple[ApprovedExample, ...]) -> list[dict[str, object]]`; no new model Tool and no automatic write hook.

- [ ] **Step 1: 写 gate 关闭、固定 Tool 优先和无自动写入测试。**

```python
class QueryMemoryRoutingTests(unittest.TestCase):
    def test_gate_off_never_reads_memory(self):
        answer("按店看销售额", state(), model=fixed_tool_model(), conn=conn,
               approved_query_memory_enabled=False)
        conn.assert_no_sql_containing("approved_query_examples")

    def test_fixed_tool_wins_over_memory_example(self):
        result = answer_with_memory("按店看销售额", examples=[exploration_example()])
        self.assertEqual(result.tool_names, ["query_business"])

    def test_success_and_user_correction_do_not_write_memory(self):
        answer_with_memory("不是这家店，换另一家", examples=[])
        self.assertEqual(memory_repository().writes, [])
```

- [ ] **Step 2: 运行测试并确认路由还未读取记忆。**

Run: `uv run --locked python -m unittest tests.test_query_memory.QueryMemoryRoutingTests -v`

Expected: FAIL because memory integration parameters/payload are absent。

- [ ] **Step 3: 实现最小安全 few-shot 投影。**

`prompt.py`：

```python
def approved_examples_payload(examples: tuple[ApprovedExample, ...]) -> list[dict[str, object]]:
    return [{
        "example_ref": item.example_ref,
        "intent_signature": item.intent_signature,
        "question_template": item.question_template,
        "slots": [slot.model_dump(mode="json") for slot in item.slots],
        "normalized_request": item.normalized_request,
        "expected_tool": item.expected_tool,
        "approval_revision": item.approval_revision,
    } for item in examples]
```

禁止加入 source run、subject、authorization refs、SQL、结果、approval reason 和版本内部字段；版本只用于服务端过滤。

- [ ] **Step 4: 接入既有模型调用。**

在第一次模型路由前、固定 Tool 可表达性判断之后读取最多 3 条；将 payload 放入独立 system 段并附加固定规则：“示例只示范 Tool 与槽位结构；当前值必须从本轮问题提取；服务端规则优先”。检索异常按空记忆降级并增加 `query_memory_retrieval_failed_total`；deadline 少于 2 秒时跳过检索。任何模型输出仍走既有 JSON schema、授权和 capability 校验。

- [ ] **Step 5: 运行路由、注入和 26 题回归。**

```powershell
uv run --locked python -m unittest tests.test_query_memory.QueryMemoryRoutingTests tests.test_operator_workflows -v
uv run --locked python tests/acceptance.py --questions tests/questions.jsonl --expected-count 26 --mode offline
```

Expected: 固定 Tool 始终优先；注入样例不改变授权与拒答；gate on/off 都为 26/26；没有 query-memory INSERT/UPDATE 由聊天路径发出。

- [ ] **Step 6: 提交路由集成。**

```powershell
git add backend/bi_agent/query_memory backend/bi_agent/agent.py backend/bi_agent/response_summary.py backend/tests/test_core.py backend/tests/test_operator_workflows.py backend/tests/test_query_memory.py
git commit -m "feat: use approved examples for safe routing"
```

---

### Task 6: Revocation, Version Invalidation, Acceptance, and Operations

**Files:**
- Modify: `backend/tests/test_query_memory.py`
- Modify: `backend/tests/test_api.py`
- Modify: `backend/tests/test_db.py`
- Modify: `frontend/src/components/QueryMemoryReview.test.tsx`
- Modify: `README.md`
- Modify: `docs/runbook.md`
- Modify: `docs/metrics.md`
- Create: `docs/superpowers/research/2026-09-14-approved-query-memory-acceptance.md`

**Interfaces:**
- Consumes: all Tasks 1–5 contracts.
- Produces: dated release evidence and rollback procedure; no new runtime interface.

- [ ] **Step 1: 增加撤销即时生效与版本升级测试。**

```python
def test_revocation_is_invisible_on_next_retrieval(self):
    repository.transition("mem-current-cost", command=revoke("evidence invalid"),
                          actor_subject_id="reviewer-a")
    self.assertEqual(retrieve_current_refs(), [])

def test_each_version_dimension_invalidates_without_auto_upgrade(self):
    for field in VersionSet.model_fields:
        changed = CURRENT.model_copy(update={field: changed_value(field)})
        self.assertEqual(retrieve_refs(current_versions=changed), [])
    self.assertEqual(repository.status("mem-current-cost"), "approved")
```

- [ ] **Step 2: 运行完整验证。**

```powershell
Set-Location backend
uv sync --locked
uv run --locked python -m unittest discover -s tests -v
$env:TEST_DATABASE_URL='postgresql://postgres:postgres@127.0.0.1:5432/bi_agent_test'
uv run --locked python -m unittest tests.test_db tests.test_runtime_db -v
uv run --locked python tests/acceptance.py --questions tests/questions.jsonl --expected-count 26 --mode offline
Set-Location ..\frontend
npm test -- --run
npm run build
```

Expected: 后端、DB-enabled、前端和 build 零失败；DB-enabled 组 0 skip；离线验收 26/26。

- [ ] **Step 3: 记录验收与安全证据。**

验收文件逐项写入：commit、命令、通过数、DB 角色、迁移 021、30 题检索结果、gate on/off 26/26、撤销延迟、七维版本失配、非审核者 403、载荷敏感词扫描和已知限制。不得粘贴 DSN、原始聊天或真实实体。

- [ ] **Step 4: 更新运维文档。**

README 标记为“默认关闭、仅人工批准”；runbook 写明创建草稿、双人复核建议、撤销/替换、版本升级后的重新审核、关闭 `APPROVED_QUERY_MEMORY_ENABLED=false` 回滚；metrics 增加 `query_memory_retrieval_total`、`query_memory_candidate_invalid_total`、`query_memory_retrieval_failed_total`、按状态数量和审批冲突数，不用问题文本作 label。

- [ ] **Step 5: 验证文档和差异。**

```powershell
rg -n 'draft|approved|superseded|revoked|APPROVED_QUERY_MEMORY_ENABLED|021_approved_query_memory' README.md docs/runbook.md docs/metrics.md docs/superpowers/research/2026-09-14-approved-query-memory-acceptance.md
git diff --check
git status --short
```

Expected: 生命周期、回滚、指标和验收证据均可定位；无空白错误；只剩计划内文件。

- [ ] **Step 6: 提交验收与运维文档。**

```powershell
git add backend/tests/test_query_memory.py backend/tests/test_api.py backend/tests/test_db.py frontend/src/components/QueryMemoryReview.test.tsx README.md docs/runbook.md docs/metrics.md docs/superpowers/research/2026-09-14-approved-query-memory-acceptance.md
git commit -m "docs: accept approved query memory"
```

## Final Verification

- [ ] 确认 Task 11、semantic catalog 和 controlled SQL 验收记录存在。
- [ ] 确认 draft/revoked/superseded、跨授权域和任一版本不匹配均为零召回。
- [ ] 确认模型、成功运行、点赞和纠错没有写 memory 的接口或数据库权限。
- [ ] 确认公开/模型/日志载荷无原始聊天、SQL、结果、真实 ID 和一次性业务值。
- [ ] 确认 gate 关闭回到无记忆基线，gate 开启不降低 26 题和四工作流边界。
- [ ] 确认审核事件可追溯 actor、时间、理由、revision 和来源 run opaque ref。
