# Isolated Analysis Agent Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 对当前用户仍有权读取的不可变数据集 Artifact 做确定性贡献、变化拆分与 MAD 异常分析，再用无工具模型生成带 finding 引用的解释；分析运行本身没有数据库、文件、网络或业务 Tool 能力。

**Architecture:** 可信服务层先用 `ArtifactReader` 检查归属、授权、类型、版本、大小和 fingerprint，并投影成只含 opaque 维度与 Decimal 指标的 `AnalysisDataset`。纯函数分析器在隔离输入上产生 `Finding`；可选模型仅通过 `complete(..., tools=[])` 总结这些 finding，失败时仍发布确定性结果。分析结果以新 `analysis_result` Artifact 持久化并引用来源 fingerprint，绝不修改或重查来源 Artifact。

**Tech Stack:** Python 3.11+、Pydantic 2、psycopg 3、PostgreSQL 17、FastAPI、标准库 `decimal/statistics/hashlib`、React 19、TypeScript、现有 `unittest` / Vitest；不加入代码执行沙箱、Notebook 或网络研究依赖。

**Spec:** [Task 11 后置子项目总设计](../specs/2026-09-14-post-task11-subprojects-design.md) §3–4、§8、§10–12。

## Global Constraints

- 只依赖 Task 11 发布门禁；不依赖语义目录、受控 SQL 或 approved 查询记忆的接口与迁移。
- 只接受已持久化的 `metric_result`、`comparison_table` 或 `trend_series` Artifact；`chart_spec`、价格复核、库存预警和分析结果不能作为来源。
- 服务层必须重新检查来源 run 的 `subject_id`、当前授权实体集合、Artifact 类型、schema/metric/policy/source/graph 版本和数据集/图表配对。
- 分析运行只接收 `AnalysisDataset` 值对象；其模块不得 import `psycopg`、repository、HTTP client、文件 API、现有业务 Tool 或同步代码。
- 金额、数量、比率、排名、贡献、变化和异常分数全部由 Decimal 纯函数计算；模型不能产生或改写 numeric finding。
- 模型只可总结经验证 finding，并必须引用 `finding_ref`；无证据因果或行动结论进入 `unsupported_claims`。
- 最大 500 行、20 列、8 个分析 kind、256 KiB 安全投影、8,000 输入 token；沿用主请求剩余 deadline，不另起 30 秒。
- 来源失权、过期、缺列、错配、超限或 fingerprint 变化在模型调用前拒绝；禁止自动重查数据库。
- `ISOLATED_ANALYSIS_ENABLED=false` 为默认值；关闭时不注册分析 Tool、不读取 Artifact、不改变既有聊天。
- 本计划固定使用迁移 `022_isolated_analysis_artifacts.sql`；它只扩展 domain/artifact CHECK，不创建事实表。

## Execution Preflight

- [ ] 验证 Task 11 门禁与当前迁移基线。

Run:

```powershell
git status --short --branch
rg --files docs/superpowers/research | rg 'task-11-release-acceptance\.md$'
Get-ChildItem backend/sql -Filter '*.sql' | Sort-Object Name | Select-Object -ExpandProperty Name
```

Expected: Task 11 验收文件唯一存在并记录 26/26、DB-enabled 零失败和真实 provider；迁移清单包含 019，隔离分析固定占用 022；无关工作树改动保持不动。

---

### Task 1: Analysis Contracts, Domain Registry, and Feature Gate

**Files:**
- Create: `backend/bi_agent/analysis/__init__.py`
- Create: `backend/bi_agent/analysis/models.py`
- Modify: `backend/bi_agent/runtime/domain_registry.py`
- Modify: `backend/bi_agent/runtime/models.py`
- Modify: `backend/bi_agent/config.py`
- Modify: `.env.example`
- Create: `backend/sql/022_isolated_analysis_artifacts.sql`
- Create: `backend/tests/test_analysis.py`
- Modify: `backend/tests/test_runtime.py`
- Modify: `backend/tests/test_db.py`

**Interfaces:**
- Consumes: existing `ArtifactRef`, `DomainResult`, `NewArtifact`, `DomainContext` and provenance field names.
- Produces: `AnalysisKind`、`AnalysisRequest`、`AnalysisObservation`、`AnalysisDataset`、`Finding`、`AnalysisResult`、domain `isolated_analysis`、artifact type `analysis_result`、`AppSettings.isolated_analysis_enabled`。

- [ ] **Step 1: 写严格请求、不可变数据集和新 Artifact 失败测试。**

```python
import unittest
from decimal import Decimal
from pydantic import ValidationError


class AnalysisContractTests(unittest.TestCase):
    def test_request_and_dataset_have_bounded_safe_shape(self):
        from bi_agent.analysis.models import AnalysisDataset, AnalysisObservation, AnalysisRequest
        request = AnalysisRequest(
            artifact_ref="00000000-0000-4000-8000-000000000001",
            analysis_kinds=["contribution", "anomaly_candidates"])
        dataset = AnalysisDataset(
            source_artifact_ref=request.artifact_ref,
            source_fingerprint="a" * 64,
            metric_version="metrics/2026-09-12.1",
            observations=(AnalysisObservation(
                row_ref="row-001", dimensions={"shop": "ent-shop-a"},
                metrics={"paid_amount": Decimal("12.30")}),))
        self.assertEqual(dataset.observations[0].metrics["paid_amount"], Decimal("12.30"))
        with self.assertRaises(ValidationError):
            AnalysisRequest(artifact_ref="not-a-ref", analysis_kinds=["python"])

    def test_analysis_result_is_a_registered_isolated_artifact(self):
        from bi_agent.runtime.domain_registry import allows_artifact_type
        self.assertTrue(allows_artifact_type("isolated_analysis", "analysis_result"))
        self.assertFalse(allows_artifact_type("business_query", "analysis_result"))
```

- [ ] **Step 2: 运行测试并确认模块/注册项不存在。**

Run:

```powershell
Set-Location backend
uv run --locked python -m unittest tests.test_analysis.AnalysisContractTests -v
```

Expected: FAIL with `ModuleNotFoundError: bi_agent.analysis` or unregistered domain。

- [ ] **Step 3: 实现模型契约。**

`models.py` 使用以下完整字段：

```python
from decimal import Decimal
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

AnalysisKind = Literal["contribution", "change_decomposition", "anomaly_candidates", "followups"]

class AnalysisRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    artifact_ref: str = Field(pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
    analysis_kinds: list[AnalysisKind] = Field(min_length=1, max_length=8)

    @field_validator("analysis_kinds")
    @classmethod
    def kinds_are_unique(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("analysis_kind_duplicate")
        return value

class AnalysisObservation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)
    row_ref: str = Field(pattern=r"^row-[a-z0-9-]{1,60}$")
    dimensions: dict[str, str] = Field(max_length=10)
    metrics: dict[str, Decimal] = Field(max_length=10)
    previous_metrics: dict[str, Decimal] = Field(default_factory=dict, max_length=10)

class AnalysisDataset(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    source_artifact_ref: str
    source_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    metric_version: str
    observations: tuple[AnalysisObservation, ...] = Field(max_length=500)
    limitations: tuple[str, ...] = ()

class Finding(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    finding_ref: str = Field(pattern=r"^finding-[a-z0-9-]{1,60}$")
    kind: AnalysisKind
    metric: str
    row_refs: tuple[str, ...]
    values: dict[str, str]
    statement_code: str

class AnalysisResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    source_artifact_ref: str
    source_fingerprint: str
    analysis_version: str
    findings: tuple[Finding, ...]
    narrative: tuple[dict[str, object], ...] = ()
    hypotheses: tuple[str, ...] = ()
    unsupported_claims: tuple[str, ...] = ()
    limitations: tuple[str, ...] = ()
```

所有 dimension/metric 键使用 `^[a-z][a-z0-9_]{0,47}$`；dimension 值只能是 stable ref、ISO date 或登记枚举；所有输出 Decimal 必须量化后编码为字符串，禁止 float。

- [ ] **Step 4: 登记 domain、payload 和迁移。**

`domain_registry.py` 增加：

```python
ANALYSIS_NODES = frozenset({"load_source", "validate_source", "compute_findings",
                            "summarize_findings", "persist_analysis", "finalize"})
"isolated_analysis": DomainSpec(
    name="isolated_analysis", nodes=ANALYSIS_NODES,
    artifact_types=frozenset({"analysis_result"}))
```

把 `analysis_result` 加入 `ARTIFACT_TYPES`，但不加入 `DATASET_ARTIFACT_TYPES`。`validate_artifact_payload()` 为它要求上面 `AnalysisResult` 的 JSON 形状，并拒绝 `sql/prompt/tool_calls/raw_rows` 键。

`022_isolated_analysis_artifacts.sql` 先 `DROP CONSTRAINT IF EXISTS query_runs_domain_check`、重建同名 CHECK 并保留 `business_query/commerce_performance/listing_price_audit/inventory_watch/controlled_sql_exploration`，再加入 `isolated_analysis`；随后 `DROP CONSTRAINT IF EXISTS query_artifacts_artifact_type_check`、重建同名 CHECK 并保留六个现有类型与 `exploration_result`，再加入 `analysis_result`。迁移末尾分别查询 `pg_constraint` 并断言两个同名约束存在。

- [ ] **Step 5: 添加默认关闭配置并运行测试。**

`AppSettings` 增加 `isolated_analysis_enabled: bool = False`；loader 只接受 `true/false`；`.env.example` 加 `ISOLATED_ANALYSIS_ENABLED=false`。

```powershell
uv run --locked python -m unittest tests.test_analysis.AnalysisContractTests tests.test_runtime tests.test_core.ConfigTests -v
$env:TEST_DATABASE_URL='postgresql://postgres:postgres@127.0.0.1:5432/bi_agent_test'
uv run --locked python -m unittest tests.test_db -v
```

Expected: contracts/runtime PASS；022 可重复应用；未知 domain/type 仍拒绝。

- [ ] **Step 6: 提交契约切片。**

```powershell
git add backend/bi_agent/analysis backend/bi_agent/runtime/domain_registry.py backend/bi_agent/runtime/models.py backend/bi_agent/config.py backend/sql/022_isolated_analysis_artifacts.sql backend/tests/test_analysis.py backend/tests/test_runtime.py backend/tests/test_db.py .env.example
git commit -m "feat: define isolated analysis contracts"
```

---

### Task 2: Authorized Immutable Artifact Loader

**Files:**
- Create: `backend/bi_agent/analysis/loader.py`
- Modify: `backend/bi_agent/runtime/models.py`
- Modify: `backend/bi_agent/runtime/repository.py`
- Modify: `backend/bi_agent/runtime/memory.py`
- Modify: `backend/tests/test_analysis.py`
- Modify: `backend/tests/test_runtime.py`
- Modify: `backend/tests/test_runtime_db.py`

**Interfaces:**
- Consumes: source `ArtifactRef`, `DomainContext.subject_id`, `DomainContext.shop_refs`, current provenance constants.
- Produces: `StoredArtifact`、`ArtifactReader.load_artifact_for_analysis(artifact_id: UUID, *, subject_id: str) -> StoredArtifact`、`load_analysis_dataset(ref: str, *, context: DomainContext) -> AnalysisDataset`。

- [ ] **Step 1: 写所有模型前拒绝场景。**

```python
class AnalysisLoaderTests(unittest.TestCase):
    def test_wrong_owner_type_version_and_size_fail_before_projection(self):
        cases = [
            (artifact(owner="subject-b"), "analysis_source_forbidden"),
            (artifact(type="chart_spec"), "analysis_source_type_unsupported"),
            (artifact(metric_version="old"), "analysis_source_version_mismatch"),
            (artifact(rows=501), "analysis_source_too_large"),
        ]
        for stored, reason in cases:
            with self.subTest(reason=reason), self.assertRaisesRegex(ValueError, reason):
                load_analysis_dataset(stored.ref, context=context_with(stored))

    def test_fingerprint_is_stable_across_key_and_row_order(self):
        first = load_analysis_dataset(ARTIFACT_REF, context=context_with(rows=ROWS))
        second = load_analysis_dataset(ARTIFACT_REF, context=context_with(rows=reordered(ROWS)))
        self.assertEqual(first.source_fingerprint, second.source_fingerprint)
```

- [ ] **Step 2: 运行测试并确认读取接口不存在。**

Run: `uv run --locked python -m unittest tests.test_analysis.AnalysisLoaderTests -v`

Expected: FAIL with missing loader/reader methods。

- [ ] **Step 3: 扩展只读 Store 协议。**

`runtime.models` 增加：

```python
class StoredArtifact(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    ref: ArtifactRef
    run_id: UUID
    subject_id: str
    artifact_type: str
    payload: dict[str, object]
    data_as_of: datetime | None
    coverage: dict[str, object] | None
    provenance: QueryProvenance

class ArtifactReader(Protocol):
    def load_artifact_for_analysis(self, artifact_id: UUID, *, subject_id: str) -> StoredArtifact: ...
```

Postgres 查询 `query_artifacts JOIN query_runs` 并在 SQL 中限定 `a.id`、`r.subject_id`、`r.status='succeeded'`；Memory Store 做相同判断并 deep copy。不存在和跨 subject 都抛同一个 `analysis_source_not_found`，避免枚举。

- [ ] **Step 4: 实现安全投影和 fingerprint。**

`loader.py` 只允许三个 dataset schema 的公开 `results` 行，按 artifact type 映射成 `AnalysisObservation`；所有实体必须出现在 `context.shop_refs.values()` 或来源 payload 的已授权 `entities` 投影。版本逐项比较现有 `QueryProvenance` 常量，coverage 必须为 complete 或明确带 gaps 的 partial，data_as_of 不得晚于当前时刻。

```python
def dataset_fingerprint(observations: tuple[AnalysisObservation, ...]) -> str:
    canonical = [item.model_dump(mode="json") for item in observations]
    canonical.sort(key=lambda row: row["row_ref"])
    encoded = json.dumps(canonical, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
```

限制后的 canonical JSON 超过 256 KiB、列数超过 20、row_ref 重复、metric 缺失或 payload 内含内部 ID 时拒绝；不调用任何 reporting query。

- [ ] **Step 5: 运行内存与真实库读取测试。**

```powershell
uv run --locked python -m unittest tests.test_analysis.AnalysisLoaderTests tests.test_runtime -v
$env:TEST_DATABASE_URL='postgresql://postgres:postgres@127.0.0.1:5432/bi_agent_test'
uv run --locked python -m unittest tests.test_runtime_db -v
```

Expected: 所有失权/错配/超限在分析前拒绝；Postgres 与 Memory 语义一致；fingerprint 与行/键顺序无关。

- [ ] **Step 6: 提交授权 loader。**

```powershell
git add backend/bi_agent/analysis/loader.py backend/bi_agent/runtime/models.py backend/bi_agent/runtime/repository.py backend/bi_agent/runtime/memory.py backend/tests/test_analysis.py backend/tests/test_runtime.py backend/tests/test_runtime_db.py
git commit -m "feat: load authorized analysis artifacts"
```

---

### Task 3: Deterministic Contribution, Change, MAD, and Follow-up Analysis

**Files:**
- Create: `backend/bi_agent/analysis/calculations.py`
- Modify: `backend/bi_agent/analysis/__init__.py`
- Modify: `backend/tests/test_analysis.py`
- Create: `backend/tests/fixtures/analysis_gold.json`

**Interfaces:**
- Consumes: immutable `AnalysisDataset` and requested `AnalysisKind` tuple.
- Produces: `compute_findings(dataset: AnalysisDataset, kinds: tuple[AnalysisKind, ...]) -> tuple[Finding, ...]`。

- [ ] **Step 1: 写 Decimal gold cases。**

```python
class AnalysisCalculationTests(unittest.TestCase):
    def test_contribution_and_change_reconcile_exactly(self):
        findings = compute_findings(dataset(
            current=[("a", "30.00"), ("b", "70.00")],
            previous=[("a", "20.00"), ("b", "80.00")]),
            ("contribution", "change_decomposition"))
        self.assertEqual(value(findings, "a", "contribution"), "0.300000")
        self.assertEqual(value(findings, "a", "change"), "10.00")
        self.assertEqual(sum_changes(findings), Decimal("0.00"))

    def test_mad_flags_outlier_without_float(self):
        findings = compute_findings(dataset_values("10", "10", "11", "100"),
                                    ("anomaly_candidates",))
        self.assertEqual(anomaly_rows(findings), ["row-004"])
        self.assertEqual(value(findings, "row-004", "method"), "median_absolute_deviation")
```

- [ ] **Step 2: 运行测试并确认计算函数不存在。**

Run: `uv run --locked python -m unittest tests.test_analysis.AnalysisCalculationTests -v`

Expected: FAIL with missing `compute_findings`。

- [ ] **Step 3: 实现贡献和变化拆分。**

```python
SIX_PLACES = Decimal("0.000001")

def contribution(value: Decimal, total: Decimal) -> str | None:
    if total == 0:
        return None
    return str((value / total).quantize(SIX_PLACES, rounding=ROUND_HALF_EVEN))

def change(current: Decimal, previous: Decimal) -> tuple[str, str | None]:
    absolute = current - previous
    relative = None if previous == 0 else str((absolute / abs(previous)).quantize(
        SIX_PLACES, rounding=ROUND_HALF_EVEN))
    return (format(absolute, "f"), relative)
```

每个 metric 单独求总额；贡献总和允许的舍入误差最多 `0.000001 * row_count`。变化 finding 同时保存 current/previous/change/change_rate；没有 previous_metrics 时写 limitation `previous_period_unavailable`，不构造零基线。

- [ ] **Step 4: 实现 MAD 异常与确定性 follow-up。**

```python
def mad_score(value: Decimal, values: tuple[Decimal, ...]) -> Decimal | None:
    center = Decimal(str(median(values)))
    mad = Decimal(str(median(tuple(abs(item - center) for item in values))))
    if mad == 0:
        return None if value == center else Decimal("Infinity")
    return (Decimal("0.6745") * (value - center) / mad).copy_abs()
```

至少 4 个同 metric 观测才计算；score `>= 3.5` 才产生候选，Infinity 编码为 `mad_zero_non_median` 而非数值。follow-up 只从固定模板产生：“核对 {row_ref} 的来源批次”“补充 previous period”“检查 coverage gaps”，不建议采购、改价或投放。

- [ ] **Step 5: 加载 gold fixture 并运行排列不变性测试。**

`analysis_gold.json` 覆盖负数、零总额、previous=0、MAD=0、部分 coverage、多个 metric、重复行和极大 Decimal；测试把每组 observation 反转后比较完整 JSON。

Run: `uv run --locked python -m unittest tests.test_analysis.AnalysisCalculationTests -v`

Expected: gold 全通过；所有数值逐项相等；顺序变化不改变 finding_ref、fingerprint 或 finding 内容。

- [ ] **Step 6: 提交确定性分析器。**

```powershell
git add backend/bi_agent/analysis/calculations.py backend/bi_agent/analysis/__init__.py backend/tests/test_analysis.py backend/tests/fixtures/analysis_gold.json
git commit -m "feat: compute deterministic artifact findings"
```

---

### Task 4: Tool-Free Narrative Summarizer and Claim Guard

**Files:**
- Create: `backend/bi_agent/analysis/summarizer.py`
- Create: `backend/bi_agent/analysis/import_guard.py`
- Modify: `backend/tests/test_analysis.py`

**Interfaces:**
- Consumes: validated `Finding` tuple, limitations, `ChatModel`, remaining seconds.
- Produces: `summarize_findings(findings: tuple[Finding, ...], *, limitations: tuple[str, ...], model: ChatModel, timeout_s: float) -> NarrativeResult`、`run_isolated_analysis(dataset: AnalysisDataset, *, kinds: tuple[AnalysisKind, ...], model: ChatModel | None, deadline: float) -> AnalysisResult`。

- [ ] **Step 1: 写无工具、因果降级和模型失败测试。**

```python
class AnalysisSummarizerTests(unittest.TestCase):
    def test_model_receives_findings_and_empty_tools(self):
        model = RecordingModel(narrative(finding_refs=["finding-a"]))
        summarize_findings(FINDINGS, limitations=(), model=model, timeout_s=3.0)
        self.assertEqual(model.calls[0].tools, [])
        self.assertNotIn("database", model.calls[0].messages_as_text.lower())

    def test_uncited_causal_claim_is_not_published_as_fact(self):
        result = summarize_findings(FINDINGS, limitations=(),
            model=FakeModel("销量下降是因为广告停投"), timeout_s=3.0)
        self.assertEqual(result.narrative, ())
        self.assertEqual(result.unsupported_claims, ("销量下降是因为广告停投",))

    def test_model_failure_keeps_deterministic_findings(self):
        result = run_isolated_analysis(
            DATASET, model=FailingModel(), kinds=("contribution",), deadline=DEADLINE)
        self.assertTrue(result.findings)
        self.assertIn("narrative_unavailable", result.limitations)
```

- [ ] **Step 2: 运行测试并确认 summarizer 不存在。**

Run: `uv run --locked python -m unittest tests.test_analysis.AnalysisSummarizerTests -v`

Expected: FAIL with missing summarizer symbols。

- [ ] **Step 3: 实现严格 narrative schema。**

模型响应只允许：

```python
class NarrativeItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1, max_length=500)
    finding_refs: tuple[str, ...] = Field(min_length=1, max_length=5)
    claim_kind: Literal["fact", "observation", "hypothesis"]

class NarrativeResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    narrative: tuple[NarrativeItem, ...] = ()
    hypotheses: tuple[str, ...] = ()
    unsupported_claims: tuple[str, ...] = ()
```

每个引用必须存在；fact/observation 文本中的数值必须逐字来自引用 finding values。出现 `因为/导致/由于/caused/because/therefore/应该立即/马上采购/自动改价` 且没有专门 evidence code 时移入 unsupported；hypothesis 必须带“假设/待验证”。

- [ ] **Step 4: 实现隔离 import 守卫。**

`import_guard.py` 用 `ast` 扫描 `backend/bi_agent/analysis/calculations.py` 和 `summarizer.py`，允许标准库、pydantic 和 `analysis.models`，拒绝 `psycopg/requests/httpx/socket/pathlib/subprocess/os` 以及 `bi_agent.*.repository/tool/sync`。测试同时检查 summarizer 调用固定为 `model.complete(messages, [], timeout_s=timeout_s)`。

- [ ] **Step 5: 运行 summarizer 和 import 守卫测试。**

Run: `uv run --locked python -m unittest tests.test_analysis.AnalysisSummarizerTests tests.test_analysis.AnalysisImportBoundaryTests -v`

Expected: tools 永远为空；禁用 import 集合为零；模型失败仍保留 finding；无证据因果不作为事实。

- [ ] **Step 6: 提交隔离总结器。**

```powershell
git add backend/bi_agent/analysis/summarizer.py backend/bi_agent/analysis/import_guard.py backend/tests/test_analysis.py
git commit -m "feat: summarize findings without tools"
```

---

### Task 5: Analysis Graph, Artifact Persistence, and Agent Entry

**Files:**
- Create: `backend/bi_agent/analysis/graph.py`
- Create: `backend/bi_agent/analysis/tool.py`
- Modify: `backend/bi_agent/analysis/__init__.py`
- Modify: `backend/bi_agent/agent.py`
- Modify: `backend/tests/test_analysis.py`
- Modify: `backend/tests/test_core.py`
- Modify: `backend/tests/test_operator_workflows.py`

**Interfaces:**
- Consumes: `load_analysis_dataset`、`compute_findings`、`summarize_findings`、`QueryRunStore`.
- Produces: `analyze_artifact(request: AnalysisRequest, *, context: DomainContext, model: ChatModel | None) -> DomainResult` and model Tool `analyze_artifact`.

- [ ] **Step 1: 写 fail-closed 持久化和剩余 deadline 测试。**

```python
class AnalysisGraphTests(unittest.TestCase):
    def test_artifact_failure_publishes_no_result(self):
        result = analyze_artifact(REQUEST, context=context(store=FailingStore()), model=None)
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.artifacts, [])
        self.assertNotIn("findings", result.model_payload)

    def test_expired_deadline_never_loads_or_calls_model(self):
        result = analyze_artifact(REQUEST, context=context(deadline=monotonic() - 1),
                                  model=RecordingModel())
        self.assertEqual(result.model_payload["code"], "deadline_exceeded")
        self.assertEqual(result.model.calls, [])
```

- [ ] **Step 2: 运行测试并确认 graph/tool 不存在。**

Run: `uv run --locked python -m unittest tests.test_analysis.AnalysisGraphTests -v`

Expected: FAIL with missing `analyze_artifact`。

- [ ] **Step 3: 实现固定图。**

节点严格按：

```python
ANALYSIS_CHAIN = (
    "load_source", "validate_source", "compute_findings",
    "summarize_findings", "persist_analysis", "finalize")
ANALYSIS_VERSION = "isolated-analysis/2026-09-14.1"
```

load/validate 在可信服务层执行，得到 `AnalysisDataset` 后只把值对象传入纯分析运行；剩余不足 2 秒跳过 narrative 并记录 limitation。每步按现有 CAS Store 写状态；source 失权/版本错配用 `needs_input` 或 `unavailable` 稳定码，不把 loader 异常原文写日志。

- [ ] **Step 4: 持久化 analysis_result 并投影模型载荷。**

```python
ref = store.save_artifact(run_id, NewArtifact(
    artifact_type="analysis_result",
    payload=result.model_dump(mode="json"),
    data_as_of=source.data_as_of,
    coverage=source.coverage))
```

payload 保留 `source_artifact_ref/source_fingerprint/analysis_version/findings/hypotheses/unsupported_claims/limitations`；模型投影只含 finding、经守卫 narrative 和限制。保存失败将状态改 failed、清空 artifacts/model findings；来源 Artifact 不更新。

- [ ] **Step 5: 注册严格 Tool schema。**

只有 gate 开启才在 `_tool_schemas()` 加 `analyze_artifact`；参数仅 `artifact_ref/analysis_kinds`，不允许 question、SQL、店铺 ID、数据行或 tool list。handler 从当前 `DomainContext` 重建授权；同轮最多调用一次，分析结果不能再触发写操作 Tool。

- [ ] **Step 6: 运行图、Tool 和四工作流回归。**

```powershell
uv run --locked python -m unittest tests.test_analysis.AnalysisGraphTests tests.test_core tests.test_operator_workflows -v
uv run --locked python tests/acceptance.py --questions tests/questions.jsonl --expected-count 26 --mode offline
```

Expected: gate off schema 与 Task 11 snapshot 相同；gate on 只有一个新只读 Tool；Artifact 失败无结果；26/26 不回退。

- [ ] **Step 7: 提交分析入口。**

```powershell
git add backend/bi_agent/analysis backend/bi_agent/agent.py backend/tests/test_analysis.py backend/tests/test_core.py backend/tests/test_operator_workflows.py
git commit -m "feat: add isolated artifact analysis tool"
```

---

### Task 6: Analysis Artifact UI and Dated Acceptance

**Files:**
- Modify: `frontend/src/types.ts`
- Modify: `frontend/src/components/ArtifactView.tsx`
- Create: `frontend/src/components/AnalysisArtifact.tsx`
- Create: `frontend/src/components/AnalysisArtifact.test.tsx`
- Modify: `frontend/src/styles.css`
- Modify: `README.md`
- Modify: `docs/runbook.md`
- Modify: `docs/metrics.md`
- Create: `docs/superpowers/research/2026-09-14-isolated-analysis-acceptance.md`

**Interfaces:**
- Consumes: public `analysis_result` Artifact payload.
- Produces: validated findings/narrative/limitations rendering and dated release evidence.

- [ ] **Step 1: 写未知字段、无证据 claim 和来源链接的前端测试。**

```tsx
it('renders deterministic findings and source reference', () => {
  render(<AnalysisArtifact artifact={analysisArtifact} />)
  expect(screen.getByText('30.00%')).toBeInTheDocument()
  expect(screen.getByRole('button', { name: '查看来源数据' })).toBeEnabled()
})

it('keeps unsupported claims out of the findings section', () => {
  render(<AnalysisArtifact artifact={causalArtifact} />)
  expect(screen.getByText('证据不足的说法')).toBeInTheDocument()
  expect(within(screen.getByTestId('findings')).queryByText(/因为广告/)).toBeNull()
})
```

- [ ] **Step 2: 运行测试并确认 renderer 不存在。**

Run: `npm test -- --run src/components/AnalysisArtifact.test.tsx`

Expected: FAIL with missing component/module。

- [ ] **Step 3: 实现白名单 renderer。**

`AnalysisArtifact` 先验证 `artifact_type/source_artifact_ref/source_fingerprint/analysis_version/findings`；按 kind 显示贡献、变化和异常，Decimal 字符串用现有格式器但不重新计算。hypotheses 标“待验证”，unsupported_claims 单独折叠，limitations 常显；来源按钮只在当前 message 的 artifact 列表找到匹配 ref 时启用，不从 URL 或本地缓存猜载荷。

- [ ] **Step 4: 运行完整验证。**

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

Expected: backend、DB-enabled、frontend、build 零失败；DB-enabled 0 skip；26/26；所有分析数值 gold case 逐项一致。

- [ ] **Step 5: 记录验收与运维。**

验收文件写 commit、022、命令/通过数、授权拒绝、版本拒绝、大小拒绝、import guard、空 tools、MAD gold、顺序不变、模型失败降级、Artifact 写失败和 gate on/off。runbook 写开启、回滚、版本推进、来源失权处理；metrics 增加 `analysis_requests_total{status,kind}`、`analysis_source_rejected_total{reason}`、`analysis_narrative_failed_total`、`analysis_unsupported_claim_total`，不使用问题或实体作 label。

- [ ] **Step 6: 检查差异并提交。**

```powershell
rg -n 'ISOLATED_ANALYSIS_ENABLED|analysis_result|022_isolated_analysis|unsupported_claims' README.md docs/runbook.md docs/metrics.md docs/superpowers/research/2026-09-14-isolated-analysis-acceptance.md
git diff --check
git status --short
git add frontend/src/types.ts frontend/src/components/ArtifactView.tsx frontend/src/components/AnalysisArtifact.tsx frontend/src/components/AnalysisArtifact.test.tsx frontend/src/styles.css README.md docs/runbook.md docs/metrics.md docs/superpowers/research/2026-09-14-isolated-analysis-acceptance.md
git commit -m "docs: accept isolated artifact analysis"
```

Expected: 只提交计划内文件；提交成功且工作树仅保留实施前无关改动。

## Final Verification

- [ ] 确认隔离分析只依赖 Task 11，未 import semantic catalog、exploration 或 query memory。
- [ ] 确认来源 Artifact 的 owner、授权、类型、版本、大小和 fingerprint 均在模型前验证。
- [ ] 确认计算模块无 DB、文件、网络和业务 Tool import，模型调用的 tools 恒为 `[]`。
- [ ] 确认所有数值 finding 由 Decimal pure functions 产生并与 gold set 逐项一致。
- [ ] 确认无证据因果/行动结论仅在 unsupported 区域，不能成为事实或 Tool 输入。
- [ ] 确认模型失败仍可显示 findings，而 Artifact 持久化失败不会显示任何分析结果。
- [ ] 确认 gate off 不注册 Tool、不读取来源且保持 Task 11 的 26/26 基线。
