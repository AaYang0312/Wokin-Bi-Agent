# Semantic Catalog and Schema Retrieval Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 建立只描述已批准 reporting 视图、字段、指标与 JOIN 粒度的版本化语义目录，并对经营问题返回安全、可复现的 Top 5 Schema 候选。

**Architecture:** 新增 `semantic_catalog` 包保存不可变目录与确定性词项检索；SQL 标识符只在服务端注册表内解析，模型只看到稳定语义 ref。启用 feature gate 时，API 工厂先用 PostgreSQL introspection 验证目录声明与真实 reporting schema 一致；本计划不执行 SQL 探索，也不改变现有固定 Tool 路由。

**Tech Stack:** Python 3.11+、Pydantic 2、psycopg 3、PostgreSQL 17、FastAPI、标准库 `dataclasses/re/unicodedata`、`unittest`；不增加向量数据库或 embedding 依赖。

**Spec:** [Task 11 后置子项目总设计](../specs/2026-09-14-post-task11-subprojects-design.md) §3–5、§10–12。

## Global Constraints

- 实施前必须确认日期化 `task-11-release-acceptance` 报告满足设计 §4；没有该报告只允许继续评审本计划，不写功能代码。
- Python 3.11+、Pydantic 2、psycopg 3、PostgreSQL 17、FastAPI、React 19 和现有 `unittest` / Vitest 技术栈保持不变。
- 模型不能提供身份、真实店铺 ID、DSN、来源认证或版本认证；目录只返回稳定语义 ref。
- 底层 `bi.*` 不得进入目录；只登记显式批准的 `reporting.*` 视图。
- 金额和业务判定仍由现有确定性 Tool 负责；本计划只选择 Schema 候选，不查询业务事实。
- 新功能默认关闭；`SEMANTIC_CATALOG_ENABLED=false` 时启动和聊天行为必须与当前版本一致。
- 当前计划不引入向量库、embedding、通用 Text2SQL、动态插件或数据库自动发现登记。
- 实施时读取最新 HEAD 和已占用迁移号；本计划不需要数据库迁移，不修改已应用 SQL。
- `VersionSet.schema_version` 固定表示 reporting 结构契约版本 `reporting/2026-09-14.1`，不是“当前最高 SQL 文件号”；只增加审批、审计或通知表不会让已批准示例失效。

## Execution Preflight

- [ ] 从仓库根目录确认 Task 11 门禁和干净的实施基线。

Run:

```powershell
git status --short --branch
rg --files docs/superpowers/research | rg 'task-11-release-acceptance\.md$'
$report = Get-ChildItem docs/superpowers/research -Filter '*task-11-release-acceptance.md' | Select-Object -ExpandProperty FullName
rg -n '26/26|test_operator_workflows|恢复检查|一周试用' $report
```

Expected: 验收报告唯一存在并记录 26/26、四工作流、恢复检查和一周试用；工作树若有无关改动，保持不动并只提交本计划列出的文件。

---

### Task 1: Shared Version and Semantic Contracts

**Files:**
- Create: `backend/bi_agent/runtime/versions.py`
- Create: `backend/bi_agent/semantic_catalog/__init__.py`
- Create: `backend/bi_agent/semantic_catalog/models.py`
- Create: `backend/tests/test_semantic_catalog.py`

**Interfaces:**
- Consumes: `backend/bi_agent/runtime/artifacts.py::QueryProvenance` 的版本字段命名；不修改现有类。
- Produces: `VersionSet`、`SemanticEntity`、`SemanticField`、`SemanticMetric`、`SemanticView`、`SemanticJoin`、`SemanticCatalog`、`SemanticSelection`。

- [ ] **Step 1: 写 VersionSet 与语义 ref 的失败测试。**

在 `backend/tests/test_semantic_catalog.py` 写入：

```python
import unittest

from pydantic import ValidationError


class SemanticContractTests(unittest.TestCase):
    def test_versions_and_refs_reject_free_text(self):
        from bi_agent.runtime.versions import VersionSet
        from bi_agent.semantic_catalog.models import SemanticField

        versions = VersionSet(
            schema_version="reporting/2026-09-14.1",
            semantic_catalog_version="semantic/2026-09-14.1",
            data_catalog_version=7,
            metric_version="metrics/2026-09-12.1",
            policy_version="multi-source-policy/2026-09-12.1",
            source_registry_version="sources/2026-09-12.1",
            graph_version="business_query-graph/2026-09-11.1",
        )
        self.assertEqual(versions.data_catalog_version, 7)
        with self.assertRaises(ValidationError):
            SemanticField(
                ref="field-drop table",
                view_ref="view-shop-daily",
                column="paid_amount",
                data_type="decimal",
                role="measure",
                aliases=("销售额",),
            )
```

- [ ] **Step 2: 运行测试并确认模块不存在。**

Run:

```powershell
Set-Location backend
uv run --locked python -m unittest tests.test_semantic_catalog.SemanticContractTests.test_versions_and_refs_reject_free_text -v
```

Expected: FAIL，错误为 `ModuleNotFoundError: bi_agent.runtime.versions` 或 `bi_agent.semantic_catalog`。

- [ ] **Step 3: 实现 VersionSet。**

`backend/bi_agent/runtime/versions.py` 使用下面的完整字段和校验：

```python
from pydantic import BaseModel, ConfigDict, Field, field_validator


class VersionSet(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    schema_version: str
    semantic_catalog_version: str
    data_catalog_version: int = Field(ge=0)
    metric_version: str
    policy_version: str
    source_registry_version: str
    graph_version: str

    @field_validator("schema_version", "semantic_catalog_version", "metric_version",
                     "policy_version", "source_registry_version", "graph_version")
    @classmethod
    def stable_identifier(cls, value: str) -> str:
        if not value or value != value.strip() or any(ch.isspace() for ch in value):
            raise ValueError("invalid_version_identifier")
        return value
```

- [ ] **Step 4: 实现语义模型。**

`backend/bi_agent/semantic_catalog/models.py` 定义 `REF_RE = r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$"`，并实现：

```python
from dataclasses import dataclass
from typing import Literal

DataType = Literal["date", "datetime", "decimal", "integer", "boolean", "text", "ref"]
FieldRole = Literal["dimension", "measure", "time", "authorization", "internal"]
Cardinality = Literal["one_to_one", "many_to_one"]


@dataclass(frozen=True)
class SemanticEntity:
    ref: str
    domains: tuple[str, ...]
    aliases: tuple[str, ...]
    description: str


@dataclass(frozen=True)
class SemanticField:
    ref: str
    view_ref: str
    column: str
    data_type: DataType
    role: FieldRole
    aliases: tuple[str, ...]


@dataclass(frozen=True)
class SemanticMetric:
    ref: str
    domains: tuple[str, ...]
    aliases: tuple[str, ...]
    required_field_refs: tuple[str, ...]
    allowed_aggregates: tuple[Literal["sum", "count", "avg", "min", "max"], ...]
    default_aggregate: Literal["sum", "count", "avg", "min", "max"]
    basis_required: bool


@dataclass(frozen=True)
class SemanticView:
    ref: str
    schema: Literal["reporting"]
    name: str
    entity_ref: str
    grain: tuple[str, ...]
    authorization_field_ref: str
    field_refs: tuple[str, ...]
    domains: tuple[str, ...]


@dataclass(frozen=True)
class SemanticJoin:
    ref: str
    left_view_ref: str
    right_view_ref: str
    left_field_ref: str
    right_field_ref: str
    cardinality: Cardinality
    allowed_group_grains: tuple[str, ...]
    anti_amplification: str


@dataclass(frozen=True)
class SemanticCatalog:
    version: str
    entities: tuple[SemanticEntity, ...]
    fields: tuple[SemanticField, ...]
    metrics: tuple[SemanticMetric, ...]
    views: tuple[SemanticView, ...]
    joins: tuple[SemanticJoin, ...]


@dataclass(frozen=True)
class SemanticSelection:
    catalog_version: str
    entity_refs: tuple[str, ...]
    metric_refs: tuple[str, ...]
    view_refs: tuple[str, ...]
    field_refs: tuple[str, ...]
    join_path_refs: tuple[str, ...]
    missing_concepts: tuple[str, ...]
    requires_clarification: bool
```

每个 dataclass 的 `__post_init__` 必须用同一个 `_require_ref()` 校验所有 `*-ref`，验证 aliases 非空白、集合无重复、view.schema 只能为 `reporting`，拒绝 `SemanticJoin.cardinality="one_to_many"`，并要求 `SemanticMetric.default_aggregate` 出现在 `allowed_aggregates` 中。

- [ ] **Step 5: 导出公共契约并运行契约测试。**

`backend/bi_agent/semantic_catalog/__init__.py` 只导出上述七个语义类型和后续 `CATALOG/retrieve_schema_candidates`；此刻先导出模型类型。运行：

```powershell
uv run --locked python -m unittest tests.test_semantic_catalog.SemanticContractTests -v
```

Expected: PASS。

- [ ] **Step 6: 提交契约切片。**

```powershell
git add backend/bi_agent/runtime/versions.py backend/bi_agent/semantic_catalog backend/tests/test_semantic_catalog.py
git commit -m "feat: define versioned semantic catalog contracts"
```

---

### Task 2: Curated Reporting Registry and Consistency Checks

**Files:**
- Create: `backend/bi_agent/semantic_catalog/registry.py`
- Modify: `backend/bi_agent/semantic_catalog/__init__.py`
- Modify: `backend/tests/test_semantic_catalog.py`

**Interfaces:**
- Consumes: Task 1 的语义 dataclass。
- Produces: `SEMANTIC_CATALOG_VERSION`、`CATALOG`、`CATALOGS_BY_VERSION`、`catalog_for_version(version)`、`catalog_indexes(catalog)`、`resolve_sql_identifier(catalog, ref)`、`validate_catalog(catalog)`。

- [ ] **Step 1: 写禁止底表、悬空 ref 与 JOIN 放大的失败测试。**

追加：

```python
class SemanticRegistryTests(unittest.TestCase):
    def test_registry_contains_only_reporting_views_and_closed_refs(self):
        from bi_agent.semantic_catalog.registry import CATALOG, catalog_indexes

        indexes = catalog_indexes(CATALOG)
        self.assertGreaterEqual(len(CATALOG.views), 8)
        self.assertTrue(all(view.schema == "reporting" for view in CATALOG.views))
        self.assertNotIn("bi", {view.schema for view in CATALOG.views})
        for view in CATALOG.views:
            self.assertIn(view.authorization_field_ref, indexes.fields)
            self.assertTrue(set(view.field_refs) <= set(indexes.fields))
        for join in CATALOG.joins:
            self.assertIn(join.cardinality, {"one_to_one", "many_to_one"})

    def test_model_facing_selection_never_resolves_to_sql_identifiers(self):
        from bi_agent.semantic_catalog.registry import CATALOG, resolve_sql_identifier

        self.assertEqual(
            resolve_sql_identifier(CATALOG, "view-shop-daily"),
            ("reporting", "v_shop_daily"),
        )
        with self.assertRaises(KeyError):
            resolve_sql_identifier(CATALOG, "reporting.v_shop_daily")
```

- [ ] **Step 2: 运行测试并确认 registry 不存在。**

Run: `uv run --locked python -m unittest tests.test_semantic_catalog.SemanticRegistryTests -v`

Expected: FAIL with `ModuleNotFoundError: bi_agent.semantic_catalog.registry`。

- [ ] **Step 3: 建立最小但完整的首版登记表。**

`registry.py` 固定 `SEMANTIC_CATALOG_VERSION = "semantic/2026-09-14.1"`，至少登记下表；列名必须逐字取自对应 SQL migration：

| view ref | SQL view | grain | authorization | 首版允许字段/指标 |
| --- | --- | --- | --- | --- |
| `view-shops` | `reporting.v_shops` | shop | `field-shop-id` | platform、currency、enabled、capabilities；display_name 标 internal，不给模型 |
| `view-shop-daily` | `reporting.v_shop_daily` | shop/day/currency | `field-shop-id` | day、paid_amount、paid_orders、erp_documents、refund_amount、cash_difference |
| `view-product-daily` | `reporting.v_product_daily` | shop/day/product/line_kind | `field-shop-id` | quantity、gift_quantity、product_paid_amount、allocation_verified |
| `view-product-cost-daily` | `reporting.v_product_cost_daily` | shop/day/product/line_kind | `field-shop-id` | sales_amount、quantity、cost_total、line_count、cost_line_count、cost_quantity |
| `view-erp-document-daily` | `reporting.v_erp_document_daily` | shop/erp_document | `field-shop-id` | day、raw_cost、raw_gross_profit、commercial_ids_count、normalization_status |
| `view-payments` | `reporting.v_payments` | shop/commercial_order | `field-shop-id` | paid_at、amount、currency、verified |
| `view-refunds` | `reporting.v_refunds` | shop/refund | `field-shop-id` | platform_completed_at、raw_platform_amount、platform_success、refund_canonical、matched |
| `view-coverage` | `reporting.v_coverage` | source/entity/shop | `field-shop-id` | covered、data_as_of、quality_status、quality_rule |
| `view-listing-items` | `reporting.v_listing_snapshot_items` | snapshot/listing/SKU | `field-shop-id` | list_amount、campaign_amount、currency、on_sale、captured_at |
| `view-physical-stock-items` | `reporting.v_physical_stock_items` | snapshot/pool/warehouse/SKU/unit | `field-pool-id` | available_quantity、inbound_quantity、locked_quantity、captured_at |
| `view-channel-stock-items` | `reporting.v_channel_stock_items` | snapshot/shop/listing/SKU/unit | `field-shop-id` | sellable_quantity、captured_at |

只登记以下 JOIN：`view-shop-daily → view-shops`、`view-product-daily → view-shops`、`view-product-cost-daily → view-shops`、`view-erp-document-daily → view-shops`，均为 `many_to_one` on shop_id；不登记 payments↔refunds、商品成本↔单据毛利或实物库存↔渠道库存的直接 JOIN。

- [ ] **Step 4: 实现闭包校验和内部解析索引。**

实现不可变索引：

```python
@dataclass(frozen=True)
class CatalogIndexes:
    entities: Mapping[str, SemanticEntity]
    fields: Mapping[str, SemanticField]
    metrics: Mapping[str, SemanticMetric]
    views: Mapping[str, SemanticView]
    joins: Mapping[str, SemanticJoin]
```

`validate_catalog()` 必须拒绝：重复 ref、非 reporting schema、悬空 field/entity/view、view field 属于另一个 view、metric 所需字段不存在、JOIN 键不属于左右 view、授权字段 role 不是 `authorization`、未写 `anti_amplification`。模块导入时调用一次 `validate_catalog(CATALOG)`。

`resolve_sql_identifier()` 只接受已登记 view ref 或 field ref：view 返回 `(schema, name)`，field 返回 `(view.schema, view.name, field.column)`；任何带点的原始 SQL 名直接 `KeyError`。

版本索引固定为 `CATALOGS_BY_VERSION: Mapping[str, SemanticCatalog] = MappingProxyType({CATALOG.version: CATALOG})`，`catalog_for_version(version)` 对未知版本返回 `None`。今后推进目录版本时旧的不可变 catalog 快照保留在该映射供历史 Artifact 展示解释；`retrieve_schema_candidates()` 仍只接受 `current_versions.semantic_catalog_version == CATALOG.version`，历史 catalog 不得用于新 SQL。

- [ ] **Step 5: 运行登记测试和现有口径回归。**

```powershell
uv run --locked python -m unittest tests.test_semantic_catalog tests.test_multi_source_metrics tests.test_commerce -v
```

Expected: PASS；需要测试库的用例必须连接 `BI_TEST_*`，不能整组 skip 后报告通过。

- [ ] **Step 6: 提交登记切片。**

```powershell
git add backend/bi_agent/semantic_catalog backend/tests/test_semantic_catalog.py
git commit -m "feat: register approved reporting semantics"
```

---

### Task 3: Deterministic Retrieval and Thirty-Question Gold Set

**Files:**
- Create: `backend/bi_agent/semantic_catalog/retrieval.py`
- Create: `backend/tests/semantic_questions.jsonl`
- Modify: `backend/bi_agent/semantic_catalog/__init__.py`
- Modify: `backend/tests/test_semantic_catalog.py`

**Interfaces:**
- Consumes: `CATALOG`、`VersionSet`。
- Produces: `normalize_terms(text) -> tuple[str, ...]`、`retrieve_schema_candidates(question, *, allowed_domains, current_versions, limit=5) -> SemanticSelection`。

- [ ] **Step 1: 写检索排序、歧义和脱敏测试。**

```python
class SemanticRetrievalTests(unittest.TestCase):
    def versions(self):
        from bi_agent.runtime.versions import VersionSet
        from bi_agent.semantic_catalog.registry import SEMANTIC_CATALOG_VERSION
        return VersionSet(
            schema_version="reporting/2026-09-14.1", semantic_catalog_version=SEMANTIC_CATALOG_VERSION,
            data_catalog_version=7, metric_version="metrics/2026-09-12.1",
            policy_version="multi-source-policy/2026-09-12.1",
            source_registry_version="sources/2026-09-12.1",
            graph_version="business_query-graph/2026-09-11.1")

    def test_paid_amount_retrieves_shop_daily_without_identifiers(self):
        from bi_agent.semantic_catalog import retrieve_schema_candidates
        result = retrieve_schema_candidates(
            "按店铺看每日支付金额", allowed_domains=frozenset({"business_query"}),
            current_versions=self.versions(), limit=5)
        self.assertEqual(result.view_refs[0], "view-shop-daily")
        self.assertIn("metric-paid-amount", result.metric_refs)
        self.assertNotIn("v_shop_daily", repr(result))

    def test_conflicting_profit_grains_require_clarification(self):
        from bi_agent.semantic_catalog import retrieve_schema_candidates
        result = retrieve_schema_candidates(
            "比较商品毛利和ERP单据毛利", allowed_domains=frozenset({"commerce_performance"}),
            current_versions=self.versions())
        self.assertTrue(result.requires_clarification)
        self.assertIn("profit_grain", result.missing_concepts)
```

- [ ] **Step 2: 运行测试并确认 retrieval 尚不存在。**

Run: `uv run --locked python -m unittest tests.test_semantic_catalog.SemanticRetrievalTests -v`

Expected: FAIL with import error。

- [ ] **Step 3: 实现确定性词项检索。**

`normalize_terms()` 必须 NFKC、lower、把中文连续片段与 `[a-z0-9_]+` token 化、去重但保留首次顺序。得分规则固定为：metric alias 完整命中 +100；field alias +40；entity alias +20；token 命中 +5；允许 domain +10；未允许 domain 直接排除。按 `(-score, ref)` 排序，view 截到 `1..5`。

冲突规则固定为：

```python
CONFLICTS = {
    frozenset({"metric-product-gross-profit-reference",
               "metric-erp-gross-profit-reference"}): "profit_grain",
    frozenset({"metric-physical-available-quantity",
               "metric-channel-sellable-quantity"}): "inventory_grain",
    frozenset({"metric-transaction-average-price",
               "metric-listing-price"}): "price_basis",
}
```

未知 token 不原样回传；只把从固定表 `KNOWN_MISSING_CONCEPTS` 识别出的 `promotion_spend/traffic/attribution/net_profit` 放进 `missing_concepts`。`current_versions.semantic_catalog_version != CATALOG.version` 时抛 `ValueError("semantic_catalog_version_mismatch")`。

- [ ] **Step 4: 写 30 道 gold set。**

`backend/tests/semantic_questions.jsonl` 每行固定形状：

```json
{"id":"S01","question":"按店铺看每日支付金额","domains":["business_query"],"required_views":["view-shop-daily"],"required_metrics":["metric-paid-amount"],"join_paths":[],"clarify":false}
{"id":"S02","question":"比较商品毛利和ERP单据毛利","domains":["commerce_performance"],"required_views":["view-product-cost-daily","view-erp-document-daily"],"required_metrics":["metric-product-gross-profit-reference","metric-erp-gross-profit-reference"],"join_paths":[],"clarify":true}
{"id":"S03","question":"广告归因后的净利润是多少","domains":["business_query"],"required_views":[],"required_metrics":[],"join_paths":[],"clarify":true,"missing_concepts":["attribution","net_profit"]}
```

继续按相同完整字段写到 S30，覆盖 spec §5.4 要求的现有指标、四工作流实体、同名字段、错误 JOIN、支付/出库 basis、库存双粒度、售价/成交均价和未支持概念。不得在问题或期望中出现真实店铺 ID。

- [ ] **Step 5: 写 gold set runner 并断言 Top 5 召回。**

测试逐行加载 JSONL，断言：所需 view 全在 `result.view_refs[:5]`；metric、join path、clarify 与期望逐项相同；所有 ref 匹配 `REF_RE`；`repr(result)` 不含 `reporting.`、`bi.`、`shop_id`。

- [ ] **Step 6: 运行检索与 gold set。**

```powershell
uv run --locked python -m unittest tests.test_semantic_catalog -v
```

Expected: 30/30 gold rows 通过，Top 5 所需视图召回率 100%，非法 JOIN 0。

- [ ] **Step 7: 提交检索切片。**

```powershell
git add backend/bi_agent/semantic_catalog backend/tests/test_semantic_catalog.py backend/tests/semantic_questions.jsonl
git commit -m "feat: retrieve safe schema candidates deterministically"
```

---

### Task 4: Database Schema Preflight and Feature Gate

**Files:**
- Create: `backend/bi_agent/semantic_catalog/schema_check.py`
- Modify: `backend/bi_agent/config.py`
- Modify: `backend/bi_agent/api.py`
- Modify: `.env.example`
- Modify: `backend/tests/test_core.py`
- Modify: `backend/tests/test_api.py`
- Modify: `backend/tests/test_semantic_catalog.py`

**Interfaces:**
- Consumes: `CATALOG` 和 `AppSettings.app_dsn`。
- Produces: `SchemaMismatch`、`validate_catalog_schema(conn, catalog) -> None`、`AppSettings.semantic_catalog_enabled: bool`。

- [ ] **Step 1: 写 introspection 不匹配和 feature-off 测试。**

```python
class SemanticSchemaCheckTests(unittest.TestCase):
    def test_missing_registered_column_fails_closed(self):
        from bi_agent.semantic_catalog.schema_check import SchemaMismatch, validate_catalog_schema
        conn = FakeIntrospectionConn(columns={
            "reporting.v_shop_daily": {"shop_id", "day", "paid_orders"},
        })
        with self.assertRaisesRegex(SchemaMismatch, "field-paid-amount"):
            validate_catalog_schema(conn, catalog_with_shop_daily_only())
```

在 `test_core.py` 添加配置断言：缺 `SEMANTIC_CATALOG_ENABLED` 时为 `False`，值只接受 `true/false`。在 `test_api.py` patch `bi_agent.api.validate_catalog_schema`，断言 `create_runtime_app()` 仅在 flag 为 true 时建立一次连接并校验。

- [ ] **Step 2: 运行三组测试并确认失败。**

```powershell
uv run --locked python -m unittest tests.test_semantic_catalog.SemanticSchemaCheckTests tests.test_core.ConfigTests tests.test_api -v
```

Expected: FAIL，原因分别为 schema_check 不存在、AppSettings 缺字段、工厂未调用校验。

- [ ] **Step 3: 实现 schema introspection。**

`validate_catalog_schema()` 仅执行：

```sql
SELECT table_schema, table_name, column_name, data_type
FROM information_schema.columns
WHERE table_schema = 'reporting'
ORDER BY table_name, ordinal_position
```

将结果映射为 `(schema, view, column)`；逐个登记 field 核对列存在。不存在的 view/column 或类型族不符时收集稳定 ref，排序后抛 `SchemaMismatch("semantic_schema_mismatch:" + refs[0])`。不得把 SQL 原标识符或数据库错误文本放进异常消息。

- [ ] **Step 4: 实现配置和启动门禁。**

在 `AppSettings` 增加 `semantic_catalog_enabled: bool = False`；`load_app_settings()` 用固定 helper 解析 `SEMANTIC_CATALOG_ENABLED` 的 `true/false`。`create_runtime_app()` 加载 settings 后，仅在 true 时：

```python
with psycopg.connect(settings.app_dsn.get_secret_value(), autocommit=True) as conn:
    validate_catalog_schema(conn, CATALOG)
```

随后再创建模型和 FastAPI app。校验失败让进程启动失败，不降级为“目录为空”。`.env.example` 写 `SEMANTIC_CATALOG_ENABLED=false`。

- [ ] **Step 5: 用 bi_app 在测试库验证只读 introspection 与底表拒绝。**

DB test 使用 `BI_TEST_READER_DSN`：`validate_catalog_schema()` 通过；随后 `SELECT 1 FROM bi.orders LIMIT 1` 必须 `InsufficientPrivilege`。这证明目录预检没有扩大业务事实权限。

- [ ] **Step 6: 运行配置、API 和语义目录测试。**

```powershell
uv run --locked --env-file ../.env.test python -m unittest tests.test_semantic_catalog tests.test_core tests.test_api -v
```

Expected: PASS，DB 测试 0 skip。

- [ ] **Step 7: 提交启动门禁。**

```powershell
git add backend/bi_agent/semantic_catalog/schema_check.py backend/bi_agent/config.py backend/bi_agent/api.py backend/tests/test_semantic_catalog.py backend/tests/test_core.py backend/tests/test_api.py .env.example
git commit -m "feat: validate semantic catalog at startup"
```

---

### Task 5: Regression, Documentation, and Acceptance Record

**Files:**
- Modify: `README.md`
- Modify: `docs/runbook.md`
- Modify: `docs/metrics.md`
- Create: `docs/superpowers/research/2026-09-14-semantic-catalog-acceptance.md`
- Modify: `docs/superpowers/plans/2026-09-11-data-and-query-closure.md`

**Interfaces:**
- Consumes: Task 1–4 的 `CATALOG`、检索和启动门禁。
- Produces: 可复现的语义目录验收记录；当前运营计划后置索引链接。

- [ ] **Step 1: 运行后端完整测试、26 题和前端回归。**

```powershell
Set-Location backend
uv run --locked --env-file ../.env.test python -m unittest discover -s tests -t .
uv run --locked --env-file ../.env.test python -m tests.acceptance --offline
Set-Location ../frontend
npm test -- --run
npm run build
```

Expected: 后端和前端零失败；DB-enabled 组 0 skip；`python -m tests.acceptance --offline` 明确报告 26/26。

- [ ] **Step 2: 验证 feature-off 完全不改变 Tool 集和聊天输出。**

运行 Task 11 的 tool schema snapshot 和 Q01/Q15；分别在 `SEMANTIC_CATALOG_ENABLED=false` 与变量缺省下比较，工具名、请求、状态、Artifact 数量和确定性结果必须相同。语义目录本计划不得新增 Agent tool。

- [ ] **Step 3: 更新运行和口径文档。**

README 写明语义目录只为后续受控 SQL 提供候选、默认关闭；runbook 写启用前迁移检查和启动失败恢复方式；metrics 增加首版登记 view/grain/JOIN 表，明确 payments↔refunds、商品毛利↔单据毛利、实物↔渠道库存没有直接 JOIN。

- [ ] **Step 4: 写日期化验收记录。**

记录 HEAD、Python/PostgreSQL 版本、`SEMANTIC_CATALOG_VERSION`、30 题逐项结果、Top 5 召回率、非法 JOIN 数、DB 权限结果、feature-off 对比和完整测试命令。所有未执行项写 `未执行`，不写预计通过。

- [ ] **Step 5: 更新后置子项目索引并提交。**

把现有计划的“语义目录 / Schema 检索”条目改为链接本计划与验收记录，状态写“计划完成；实现以 Task 11 门禁为前置”，不勾选实现完成。

```powershell
git add README.md docs/runbook.md docs/metrics.md docs/superpowers/research/2026-09-14-semantic-catalog-acceptance.md docs/superpowers/plans/2026-09-11-data-and-query-closure.md
git commit -m "docs: verify semantic catalog retrieval"
```

## Final Verification

- [ ] `git diff --check` 无输出。
- [ ] `git status --short` 只显示用户原有的无关改动。
- [ ] `rg -n 'bi\.|reporting\.|shop_id' backend/tests/semantic_questions.jsonl` 无输出。
- [ ] 30 道 gold set 的 required views Top 5 召回率为 100%，JOIN 路径断言 100%。
- [ ] 当前目录可用于新检索；旧目录只可解释历史 Artifact，不能生成新查询。
- [ ] `SEMANTIC_CATALOG_ENABLED=false` 时没有新增工具、数据库读或用户可见差异。
