"""隔离分析（子项目 D）计划 Task 1：契约、领域登记与默认关闭门禁。

红绿顺序：本文件先于 `bi_agent.analysis` 包存在，所有 import 都在用例内进行，
模块缺失时每条用例各自报 ModuleNotFoundError，而不是收集期炸掉整个文件。

本文件钉住计划 Global Constraints 中属于契约切片的部分：

- 请求/观测/数据集/finding/结果全部有界、不可变、`hide_input_in_errors`——
  绑定业务值进不了校验错误文本；
- dimension/metric 键与值是安全形状：原始 ERP 主键、浮点、NaN、指数文本一律拒收；
- `isolated_analysis` 只发 `analysis_result`，且它不是任何领域的数据集来源；
- 公开载荷校验拒绝 `sql/prompt/tool_calls/raw_rows` 键：分析结果永远不是查询通道；
- 门禁默认关与严格 true/false 的解析在 tests.test_core.ConfigTests。

计划 Task 2 追加（授权不可变 Artifact loader）：

- 不是 owner / 不存在 / 非成功运行 → 同一个 `analysis_source_not_found`，无法枚举；
- 只接受三个数据集类型；版本逐项对当前常量，过期即拒；
- 投影只收精确十进制（float 与超长数字串拒收），fingerprint 与行序/键序无关；
- 实体引用必须落在当前授权集合或来源 entities 投影内；loader 不碰 conn。

计划 Task 3 追加（确定性 Decimal 计算）：

- 每个数值都来自 Decimal 纯函数：精确运算遇 Inexact 即稳定失败，除法只
  量化一次，全程无 float；finding 与观测顺序、指标键序无关；
- 缺 previous 不造零基线；previous=0 只发绝对变化；总额为 0 不发贡献；
- MAD 同指标 ≥4 个观测、阈值 3.5，mad=0 编码为 `mad_zero_non_median`；
- followups 只有三条固定证据模板，不给采购/改价/投放类建议；
- gold fixture 逐字段比对：反序输入不变，错期望与改输入都必须转红。

计划 Task 4 追加（无工具叙事总结与 claim 守卫）：

- 模型只收验证过的 finding 与 limitations，tools 恒为 `[]`，超时沿用主请求
  剩余 deadline（剩余不足 2 秒不发调用）；
- 每条 fact/observation 必须引用存在的 finding_ref，文本中的数字 token 必须
  逐字出现在所引 finding 的 values 里；
- 因果/行动语言、未知引用、多余响应字段、非 JSON 文本一律进
  unsupported_claims，绝不伪装成事实；hypothesis 必须自带“假设/待验证”；
- 模型失败/超时/空回复降级为 `narrative_unavailable` limitation，确定性
  findings 原样保留；输入预算 8,000 token 在任何模型调用之前拒绝；
- import 守卫把 calculations/summarizer 钉在批准面上，并把模型调用钉成
  唯一形状 `model.complete(messages, [], timeout_s=timeout_s)`。
"""

import copy
import json
import pathlib
import unittest
from datetime import datetime, timezone
from decimal import Decimal
import time
from uuid import uuid4

UUID_V4 = "00000000-0000-4000-8000-000000000001"
FINGERPRINT = "a" * 64
METRIC_VERSION = "metrics/2026-09-12.1"
ANALYSIS_VERSION = "isolated-analysis/2026-09-14.1"


def _observation(*, row_ref="row-001", dimensions=None, metrics=None,
                 previous_metrics=None):
    from bi_agent.analysis.models import AnalysisObservation

    return AnalysisObservation(
        row_ref=row_ref,
        dimensions=dimensions if dimensions is not None else {"shop": "ent-shop-a"},
        metrics=metrics if metrics is not None else {"paid_amount": Decimal("12.30")},
        previous_metrics=previous_metrics if previous_metrics is not None else {},
    )


def _finding(ref="finding-a", *, kind="contribution", metric="paid_amount",
             row_refs=("row-001",), values=None,
             statement_code="contribution_share"):
    from bi_agent.analysis.models import Finding

    return Finding(
        finding_ref=ref, kind=kind, metric=metric, row_refs=row_refs,
        values=values if values is not None else
        {"share": "1.000000", "value": "12.30"},
        statement_code=statement_code)


def _result_payload(*, findings=None, narrative=None, extra=None):
    payload = {
        "source_artifact_ref": UUID_V4,
        "source_fingerprint": FINGERPRINT,
        "analysis_version": ANALYSIS_VERSION,
        "findings": findings if findings is not None else [{
            "finding_ref": "finding-a", "kind": "contribution",
            "metric": "paid_amount", "row_refs": ["row-001"],
            "values": {"share": "1.000000"}, "statement_code": "contribution_share",
        }],
        "narrative": narrative if narrative is not None else [],
        "hypotheses": [],
        "unsupported_claims": [],
        "limitations": ["previous_period_unavailable"],
    }
    if extra:
        payload.update(extra)
    return payload


class AnalysisContractTests(unittest.TestCase):
    """计划 Task 1 Step 1/3 的严格契约：形状、边界与拒收通道。"""

    def test_request_and_dataset_have_bounded_safe_shape(self):
        from pydantic import ValidationError

        from bi_agent.analysis.models import (AnalysisDataset, AnalysisRequest,
                                              AnalysisObservation)
        request = AnalysisRequest(
            artifact_ref=UUID_V4,
            analysis_kinds=["contribution", "anomaly_candidates"])
        dataset = AnalysisDataset(
            source_artifact_ref=request.artifact_ref,
            source_fingerprint=FINGERPRINT,
            metric_version=METRIC_VERSION,
            observations=(_observation(),))
        self.assertEqual(dataset.observations[0].metrics["paid_amount"],
                         Decimal("12.30"))
        with self.assertRaises(ValidationError):
            AnalysisRequest(artifact_ref="not-a-ref", analysis_kinds=["python"])

    def test_request_kinds_are_unique_and_bounded(self):
        from pydantic import ValidationError

        from bi_agent.analysis.models import AnalysisRequest

        with self.assertRaises(ValidationError):
            AnalysisRequest(artifact_ref=UUID_V4,
                            analysis_kinds=["contribution", "contribution"])
        with self.assertRaises(ValidationError):
            AnalysisRequest(artifact_ref=UUID_V4, analysis_kinds=[])
        # 只有四个登记 kind：重复列表既超界也不唯一，两条路径都必须红。
        with self.assertRaises(ValidationError):
            AnalysisRequest(artifact_ref=UUID_V4,
                            analysis_kinds=["contribution"] * 9)

    def test_analysis_result_is_a_registered_isolated_artifact(self):
        from bi_agent.runtime.domain_registry import allows_artifact_type

        self.assertTrue(allows_artifact_type("isolated_analysis", "analysis_result"))
        self.assertFalse(allows_artifact_type("business_query", "analysis_result"))
        # 登记只给这一种类型：借道既有数据集类型等于绕过固定查询门禁。
        self.assertFalse(allows_artifact_type("isolated_analysis", "metric_result"))
        self.assertFalse(allows_artifact_type("isolated_analysis", "chart_spec"))
        self.assertFalse(allows_artifact_type("nope", "analysis_result"))

    def test_analysis_result_is_never_a_dataset_source(self):
        from bi_agent.runtime.domain_registry import (ARTIFACT_TYPES,
                                                      DATASET_ARTIFACT_TYPES,
                                                      allows_artifact_type,
                                                      domains)

        self.assertIn("analysis_result", ARTIFACT_TYPES)
        self.assertNotIn("analysis_result", DATASET_ARTIFACT_TYPES)
        for domain in domains():
            self.assertEqual(allows_artifact_type(domain, "analysis_result"),
                             domain == "isolated_analysis",
                             f"{domain} 不得产出 analysis_result")

    def test_observation_keys_are_safe_and_bounded(self):
        from pydantic import ValidationError

        from bi_agent.analysis.models import AnalysisObservation

        for bad_key in ("Shop", "1bad", "bad-key", "有中文", ""):
            with self.subTest(key=bad_key):
                with self.assertRaises(ValidationError):
                    _observation(dimensions={bad_key: "physical"})
                with self.assertRaises(ValidationError):
                    _observation(metrics={bad_key: Decimal("1")})
        with self.assertRaises(ValidationError):
            _observation(dimensions={f"dim_{index}": "physical"
                                     for index in range(11)})
        with self.assertRaises(ValidationError):
            _observation(metrics={f"metric_{index}": Decimal(index)
                                  for index in range(11)})
        with self.assertRaises(ValidationError):
            _observation(previous_metrics={f"metric_{index}": Decimal(index)
                                           for index in range(11)})
        with self.assertRaises(ValidationError):
            AnalysisObservation(row_ref="row-UPPER", dimensions={},
                                metrics={"paid_amount": Decimal("1")})

    def test_metric_values_reject_floats_and_nonfinite(self):
        from pydantic import ValidationError

        for bad in (12.30, True, "NaN", "Infinity", "-Infinity", "1e5", "1E+5"):
            with self.subTest(value=bad):
                with self.assertRaises(ValidationError):
                    _observation(metrics={"paid_amount": bad})
        for good in (Decimal("12.30"), "12.30", 5, 0,
                     Decimal("123456789012345678901234567890.123456")):
            with self.subTest(value=str(good)):
                observation = _observation(metrics={"paid_amount": good})
                self.assertEqual(observation.metrics["paid_amount"],
                                 Decimal(str(good)))

    def test_dimension_values_are_refs_dates_or_enum_tokens(self):
        from pydantic import ValidationError

        for good in ("ent-1a2b3c4d", "physical", "shop-sellable", "2026-09-14"):
            with self.subTest(value=good):
                _observation(dimensions={"shop": good})
        for bad in ("446655440000", "店铺A", "two words", "1abc", "UPPER",
                    "ent-1a2b3c4d;drop"):
            with self.subTest(value=bad):
                with self.assertRaises(ValidationError):
                    _observation(dimensions={"shop": bad})
        with self.assertRaises(ValidationError) as caught:
            _observation(dimensions={"shop": "店铺A"})
        # hide_input_in_errors：绑定业务值不回显在错误文本里。
        self.assertNotIn("店铺A", str(caught.exception))

    def test_dataset_is_frozen_bounded_and_closes_extra_fields(self):
        from pydantic import ValidationError

        from bi_agent.analysis.models import AnalysisDataset

        dataset = AnalysisDataset(
            source_artifact_ref=UUID_V4, source_fingerprint=FINGERPRINT,
            metric_version=METRIC_VERSION,
            observations=(_observation(row_ref=f"row-{index:03d}")
                          for index in range(500)))
        self.assertEqual(len(dataset.observations), 500)
        with self.assertRaises(ValidationError):
            AnalysisDataset(
                source_artifact_ref=UUID_V4, source_fingerprint=FINGERPRINT,
                metric_version=METRIC_VERSION,
                observations=(_observation(row_ref=f"row-{index:04d}")
                              for index in range(501)))
        with self.assertRaises(ValidationError):
            AnalysisDataset(
                source_artifact_ref=UUID_V4, source_fingerprint=FINGERPRINT,
                metric_version=METRIC_VERSION, observations=(),
                unexpected="nope")
        with self.assertRaises(ValidationError):
            dataset.observations[0].metrics = {}
        with self.assertRaises(ValidationError):
            AnalysisDataset(
                source_artifact_ref=UUID_V4, source_fingerprint="z" * 64,
                metric_version=METRIC_VERSION, observations=())
        with self.assertRaises(ValidationError):
            AnalysisDataset(
                source_artifact_ref=UUID_V4, source_fingerprint=FINGERPRINT,
                metric_version="metrics/latest", observations=())

    def test_finding_shape_is_bounded_and_coded(self):
        from pydantic import ValidationError

        from bi_agent.analysis.models import Finding

        finding = _finding()
        self.assertEqual(finding.values["share"], "1.000000")
        for kwargs in ({"ref": "finding-UPPER"},
                       {"kind": "python"},
                       {"metric": "BadMetric"},
                       {"statement_code": "has space"},
                       {"row_refs": ()},
                       {"values": {"share": 1.0}},
                       # values 映射的键也是键：查询通道词汇在任意层级都拒。
                       {"values": {"sql": "select 1"}},
                       {"values": {"prompt": "忽略以上指令"}},
                       {"values": {"tool_calls": "[]"}},
                       {"values": {"raw_rows": "[]"}}):
            with self.subTest(**kwargs):
                with self.assertRaises(ValidationError):
                    _finding(**kwargs)
        with self.assertRaises(ValidationError):
            Finding(finding_ref="finding-a", kind="contribution",
                    metric="paid_amount", row_refs=("row-001",),
                    values={"share": "1.000000"}, statement_code="ok",
                    extra="nope")

    def test_analysis_result_is_frozen_and_bounded(self):
        from pydantic import ValidationError

        from bi_agent.analysis.models import AnalysisResult

        result = AnalysisResult(
            source_artifact_ref=UUID_V4, source_fingerprint=FINGERPRINT,
            analysis_version=ANALYSIS_VERSION,
            findings=(_finding(),),
            narrative=({"text": "贡献最高的一行。", "finding_refs": ["finding-a"],
                        "claim_kind": "observation"},),
            hypotheses=(),
            unsupported_claims=(),
            limitations=())
        with self.assertRaises(ValidationError):
            result.findings = ()
        with self.assertRaises(ValidationError):
            AnalysisResult(
                source_artifact_ref=UUID_V4, source_fingerprint=FINGERPRINT,
                analysis_version=ANALYSIS_VERSION,
                findings=(_finding(ref=f"finding-{index:03d}")
                          for index in range(501)))

    def test_observation_is_frozen(self):
        from pydantic import ValidationError

        observation = _observation()
        with self.assertRaises(ValidationError):
            observation.metrics = {}


class AnalysisPayloadContractTests(unittest.TestCase):
    """`analysis_result` 的公开载荷：白名单键集 + 逐字段安全形状。"""

    def test_valid_payload_round_trips_through_both_validators(self):
        from bi_agent.runtime.models import (validate_artifact_payload,
                                             validate_model_payload)

        payload = _result_payload(narrative=[{
            "text": "贡献最高的一行占三成。", "finding_refs": ["finding-a"],
            "claim_kind": "observation"}])
        for validate in (validate_artifact_payload, validate_model_payload):
            with self.subTest(validate=validate.__name__):
                self.assertEqual(validate(payload, "analysis_result"), payload)
        self.assertEqual(
            validate_artifact_payload(_result_payload(findings=[]),
                                      "analysis_result"),
            _result_payload(findings=[]),
            "findings 允许为空：单行数据集跑 MAD 候选就是零候选")

    def test_unsafe_payload_keys_are_rejected(self):
        from bi_agent.runtime.models import validate_artifact_payload

        for key in ("sql", "prompt", "tool_calls", "raw_rows",
                    "normalized_request", "entities"):
            with self.subTest(key=key):
                with self.assertRaisesRegex(ValueError,
                                            "unsafe_persistence_payload"):
                    validate_artifact_payload(
                        _result_payload(extra={key: "x"}), "analysis_result")

    def test_malformed_fields_are_rejected(self):
        from bi_agent.runtime.models import validate_artifact_payload

        cases = [
            ("bad_ref", {"source_artifact_ref": "not-a-ref"}),
            ("bad_fingerprint", {"source_fingerprint": "z" * 64}),
            ("bad_version", {"analysis_version": "latest"}),
            ("findings_not_list", {"findings": {}}),
            ("finding_extra_key",
             {"findings": [dict(_result_payload()["findings"][0],
                                sql="select 1")]}),
            ("finding_bad_kind",
             {"findings": [dict(_result_payload()["findings"][0],
                                kind="python")]}),
            ("finding_bad_rows",
             {"findings": [dict(_result_payload()["findings"][0],
                                row_refs=["nope"])]}),
            ("finding_float_value",
             {"findings": [dict(_result_payload()["findings"][0],
                                values={"share": 0.5})]}),
            ("finding_values_sql_key",
             {"findings": [dict(_result_payload()["findings"][0],
                                values={"sql": "select 1"})]}),
            ("finding_values_prompt_key",
             {"findings": [dict(_result_payload()["findings"][0],
                                values={"prompt": "x"})]}),
            ("finding_values_tool_calls_key",
             {"findings": [dict(_result_payload()["findings"][0],
                                values={"tool_calls": "[]"})]}),
            ("finding_values_raw_rows_key",
             {"findings": [dict(_result_payload()["findings"][0],
                                values={"raw_rows": "[]"})]}),
            ("narrative_bad_claim",
             {"narrative": [{"text": "x", "finding_refs": ["finding-a"],
                             "claim_kind": "factoid"}]}),
            ("narrative_unbounded_text",
             {"narrative": [{"text": "x" * 501, "finding_refs": ["finding-a"],
                             "claim_kind": "fact"}]}),
            ("narrative_empty_refs",
             {"narrative": [{"text": "x", "finding_refs": [],
                             "claim_kind": "fact"}]}),
            ("hypotheses_not_text", {"hypotheses": [1]}),
            ("too_many_limitations",
             {"limitations": [f"code_{index}" for index in range(21)]}),
        ]
        for label, extra in cases:
            with self.subTest(case=label):
                with self.assertRaises(ValueError):
                    validate_artifact_payload(
                        _result_payload(extra=extra), "analysis_result")


class AnalysisRegistryContractTests(unittest.TestCase):
    """领域登记与 PersistenceNode 词表的逐项对齐。"""

    def test_registry_nodes_are_exactly_the_plan_chain(self):
        from bi_agent.runtime.domain_registry import spec_for

        self.assertEqual(
            spec_for("isolated_analysis").nodes,
            {"load_source", "validate_source", "compute_findings",
             "summarize_findings", "persist_analysis", "finalize"})

    def test_analysis_nodes_are_in_the_persistence_vocabulary(self):
        import typing

        from bi_agent.runtime.domain_registry import spec_for
        from bi_agent.runtime.models import PersistenceNode

        vocabulary = set(typing.get_args(PersistenceNode))
        missing = spec_for("isolated_analysis").nodes - vocabulary
        self.assertEqual(missing, set(),
                         "登记节点必须先进入 PersistenceNode 词表")

    def test_package_ships_only_the_delivered_slices(self):
        import pathlib

        import bi_agent.analysis as analysis

        shipped = sorted(path.name for path in
                         pathlib.Path(analysis.__file__).parent.glob("*.py"))
        # Task 1 契约 + Task 2 授权 loader + Task 3 确定性计算 + Task 4 总结器
        # 与 import 守卫 + Task 5 固定图与模型 Tool。
        self.assertEqual(shipped, ["__init__.py", "calculations.py",
                                   "graph.py", "import_guard.py", "loader.py",
                                   "models.py", "summarizer.py", "tool.py"])


class _LandmineConn:
    """loader 决不允许碰 conn：任何属性访问都当场炸（钉“不自动重查数据库”）。"""

    def __getattr__(self, name):
        raise AssertionError(f"loader touched conn.{name}")


class _ScriptedReader:
    """实现 ArtifactReader 协议的最小替身：原样返回构造好的 StoredArtifact。"""

    def __init__(self, stored=None):
        self.stored = stored
        self.calls: list[tuple] = []

    def load_artifact_for_analysis(self, artifact_id, *, subject_id):
        self.calls.append((artifact_id, subject_id))
        if isinstance(self.stored, Exception):
            raise self.stored
        return self.stored


# 测试引用由 (kind, ERP主键) 同源派生，与仓库其它测试共用一套拼写。
def _ref(kind: str, key: str) -> str:
    from bi_agent.catalog import ref_for_key

    return ref_for_key(kind, key)


S1 = _ref("shop", "S1")
P1 = _ref("product", "P1")


def _row(**overrides):
    row = {"shop_ref": S1, "day": "2026-09-01",
           "paid_amount": "12.30", "paid_orders": 3}
    row.update(overrides)
    return row


def _dataset_payload(*rows, entities=None, coverage=None):
    if coverage is _OMIT:
        payload = {"status": "ok", "metric_definition": {},
                   "limitations": [], "data_as_of": None,
                   "filters": {}, "data": list(rows)}
    else:
        if coverage is None:
            coverage = {"status": "complete", "start": "2026-09-01",
                        "end": "2026-09-08", "gaps": []}
        payload = {"status": "ok", "metric_definition": {},
                   "coverage": coverage, "limitations": [], "data_as_of": None,
                   "filters": {}, "data": list(rows)}
    if entities is not None:
        payload["entities"] = entities
    return payload


# 哨兵：显式区分「载荷不带 coverage 键」与「使用默认 complete 覆盖」。
_OMIT = object()


def _entities(*refs):
    return [{"ref": ref, "kind": "shop", "name_source": "unresolved"}
            for ref in refs]


def _stored_artifact(*, artifact_type="metric_result", payload=None,
                     subject_id="u1", provenance=None, data_as_of=None,
                     coverage=None):
    from bi_agent.runtime.artifacts import QueryProvenance
    from bi_agent.runtime.models import ArtifactRef, StoredArtifact

    return StoredArtifact(
        ref=ArtifactRef(id=uuid4(), type=artifact_type),
        run_id=uuid4(), subject_id=subject_id, artifact_type=artifact_type,
        payload=payload if payload is not None else _dataset_payload(_row()),
        data_as_of=data_as_of, coverage=coverage,
        provenance=provenance if provenance is not None else QueryProvenance())


def _context(store, *, subject_id="u1", shop_refs=None, deadline=None):
    from bi_agent.commerce.models import DomainContext

    refs = shop_refs if shop_refs is not None else {"S1": S1}
    return DomainContext(
        subject_id=subject_id, allowed_shop_ids=frozenset(refs), shop_refs=refs,
        conn=_LandmineConn(), store=store,
        chat_id=uuid4(), user_message_id=uuid4(), root_request_id=uuid4(),
        now=datetime(2026, 9, 15, tzinfo=timezone.utc),
        deadline=time.monotonic() + 30 if deadline is None else deadline)


class _GraphStore:
    """图测试的 Store 替身：真 CAS 语义 + 可注入的读取/保存失败。

    读取路径实现 ArtifactReader 协议（图的 metadata 读与 loader 的授权读都走
    这里）；其余 create_run/transition/finish/save_artifact 委托内存 Store，
    保证图写的每一格状态都过真实校验，而不是测试自己放水。
    """

    def __init__(self, stored=None, *, fail_save=False, fail_reads_after=None,
                 read_error=None):
        from bi_agent.runtime.memory import MemoryQueryRunStore

        self.inner = MemoryQueryRunStore(forbidden_values={"S1"})
        self.stored = stored
        self.fail_save = fail_save
        self.fail_reads_after = fail_reads_after
        self.read_error = (read_error if read_error is not None
                           else RuntimeError("simulated read outage"))
        self.reads = 0
        self.saves = 0

    def load_artifact_for_analysis(self, artifact_id, *, subject_id):
        self.reads += 1
        if self.fail_reads_after is not None and self.reads > self.fail_reads_after:
            raise self.read_error
        if isinstance(self.stored, Exception):
            raise self.stored
        if self.stored is None:
            # 与真实 Store 同一契约：不存在/跨 owner 都报同一个稳定码。
            raise ValueError("analysis_source_not_found")
        return self.stored

    def save_artifact(self, run_id, artifact):
        self.saves += 1
        if self.fail_save:
            raise RuntimeError("simulated artifact failure")
        return self.inner.save_artifact(run_id, artifact)

    def __getattr__(self, name):
        return getattr(self.inner, name)


def _graph_stored(*, artifact_type="metric_result", payload=None,
                  subject_id="u1", provenance=None, data_as_of=None,
                  coverage=None):
    """固定 ref 的来源快照：请求 ref 与存储 ref 必须一致，图才读得到。"""
    from uuid import UUID

    from bi_agent.runtime.models import ArtifactRef

    stored = _stored_artifact(artifact_type=artifact_type, payload=payload,
                              subject_id=subject_id, provenance=provenance,
                              data_as_of=data_as_of, coverage=coverage)
    return stored.model_copy(update={
        "ref": ArtifactRef(id=UUID(UUID_V4), type=artifact_type)})


def _analysis_request(ref=None, kinds=("contribution",)):
    from bi_agent.analysis.models import AnalysisRequest

    return AnalysisRequest(artifact_ref=ref or UUID_V4,
                           analysis_kinds=list(kinds))


def _load(stored, *, context=None):
    from bi_agent.analysis.loader import load_analysis_dataset

    return load_analysis_dataset(str(stored.ref.id) if stored is not None
                                 else str(uuid4()),
                                 context=context or _context(_ScriptedReader(stored)))


class AnalysisLoaderTests(unittest.TestCase):
    """计划 Task 2 Step 1/4：授权、类型、版本、大小与投影纪律，全部在分析前拒绝。"""

    def test_wrong_owner_type_version_and_size_fail_before_projection(self):
        stale = _stored_artifact(provenance=_provenance(metric_version="metrics/2026-09-01.1"))
        big = _stored_artifact(payload=_dataset_payload(
            *(_row(paid_amount=str(index)) for index in range(501))))
        cases = [
            (_stored_artifact(subject_id="subject-b"), "analysis_source_not_found"),
            (_stored_artifact(artifact_type="chart_spec"),
             "analysis_source_type_unsupported"),
            (stale, "analysis_source_version_mismatch"),
            (big, "analysis_source_too_large"),
        ]
        for stored, reason in cases:
            with self.subTest(reason=reason), \
                    self.assertRaisesRegex(ValueError, f"^{reason}$"):
                _load(stored)

    def test_not_found_and_cross_owner_are_indistinguishable(self):
        from bi_agent.analysis.loader import load_analysis_dataset

        context = _context(_ScriptedReader(ValueError("analysis_source_not_found")))
        with self.assertRaisesRegex(ValueError, "^analysis_source_not_found$"):
            load_analysis_dataset(str(uuid4()), context=context)
        # 跨 owner 与不存在都走同一个码：读不到就是读不到，无法枚举他人 Artifact。
        context = _context(_ScriptedReader(ValueError("analysis_source_not_found")))
        with self.assertRaisesRegex(ValueError, "^analysis_source_not_found$"):
            load_analysis_dataset("not-a-uuid", context=context)

    def test_only_dataset_schemas_are_sources(self):
        for artifact_type in ("chart_spec", "price_audit", "inventory_alerts",
                              "exploration_result", "analysis_result"):
            with self.subTest(artifact_type=artifact_type):
                stored = _stored_artifact(artifact_type=artifact_type,
                                          payload={"status": "ok"})
                with self.assertRaisesRegex(ValueError,
                                            "^analysis_source_type_unsupported$"):
                    _load(stored)

    def test_fingerprint_is_stable_across_key_and_row_order(self):
        from bi_agent.analysis.loader import dataset_fingerprint

        rows = [
            {"shop_ref": S1, "day": "2026-09-01",
             "paid_amount": "12.30", "paid_orders": 3},
            {"paid_amount": "1.00", "day": "2026-09-02",
             "shop_ref": S1, "product_ref": P1},
        ]
        entities = _entities(S1) + [{"ref": P1, "kind": "product",
                                     "name_source": "unresolved"}]
        first = _load(_stored_artifact(payload=_dataset_payload(
            *rows, entities=entities)))
        reordered = [dict(reversed(list(row.items()))) for row in reversed(rows)]
        second = _load(_stored_artifact(payload=_dataset_payload(
            *reordered, entities=entities)))
        self.assertEqual(first.source_fingerprint, second.source_fingerprint)
        # 反向突变：内容真的变了指纹必须变，防“恒等指纹”假绿。
        changed = _load(_stored_artifact(payload=_dataset_payload(
            *rows, {"shop_ref": S1, "day": "2026-09-01", "paid_amount": "13.30"},
            entities=entities)))
        self.assertNotEqual(first.source_fingerprint, changed.source_fingerprint)
        observations = sorted(first.observations, key=lambda item: item.row_ref)
        self.assertEqual(dataset_fingerprint(tuple(observations)),
                         first.source_fingerprint)

    def test_projection_keeps_exact_decimals_and_never_float(self):
        dataset = _load(_stored_artifact(payload=_dataset_payload(
            {"shop_ref": S1, "paid_amount": "12.30", "paid_orders": 3})))
        observation = dataset.observations[0]
        self.assertEqual(observation.metrics["paid_amount"], Decimal("12.30"))
        self.assertEqual(observation.metrics["paid_orders"], Decimal(3))
        self.assertEqual(observation.dimensions, {"shop_ref": S1})

        # float 是 JSON 通道进不来、也绝不能从读回侧放进来：直接在 StoredArtifact
        # 里携带 float（绕过写入校验）时，loader 必须拒收而不是转 Decimal。
        smuggled = _dataset_payload({"shop_ref": S1, "paid_amount": 12.30})
        with self.assertRaisesRegex(ValueError, "^analysis_source_invalid$"):
            _load(_stored_artifact(payload=smuggled))

    def test_unauthorized_entity_dimension_is_rejected(self):
        stranger = _ref("shop", "OTHER")
        with self.assertRaisesRegex(ValueError, "^analysis_dimension_unauthorized$"):
            _load(_stored_artifact(payload=_dataset_payload(
                {"shop_ref": stranger, "paid_amount": "1.00"})))
        # 来源 entities 投影里的引用与当前授权同权：两家都放行（计划 Step 4）。
        payload = _dataset_payload(
            {"shop_ref": stranger, "paid_amount": "1.00"},
            entities=[{"ref": stranger, "kind": "shop",
                       "name_source": "unresolved"}])
        dataset = _load(_stored_artifact(payload=payload))
        self.assertEqual(dataset.observations[0].dimensions["shop_ref"], stranger)

    def test_internal_identifier_tampering_is_rejected_before_projection(self):
        """内部主键禁令由载荷重校验执行：文本列不夹带长数字串，篡改即拒。"""
        with self.assertRaisesRegex(ValueError, "^analysis_source_invalid$"):
            _load(_stored_artifact(payload=_dataset_payload(
                {"shop_ref": S1, "paid_amount": "12.30",
                 "notice": "订单446655440000已核对"})))

    def test_row_without_any_metric_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "^analysis_source_metric_missing$"):
            _load(_stored_artifact(payload=_dataset_payload(
                {"shop_ref": S1, "day": "2026-09-01",
                 "paid_amount": None, "paid_orders": None})))

    def test_duplicate_rows_are_rejected_not_deduplicated(self):
        duplicate = _row()
        with self.assertRaisesRegex(ValueError, "^analysis_row_ref_duplicate$"):
            _load(_stored_artifact(payload=_dataset_payload(duplicate, dict(duplicate))))

    def test_row_field_budget_is_enforced(self):
        # 真实数值列拼出的 11 指标行：重校验放行，但超出投影预算即拒。
        wide = {"shop_ref": S1, "paid_amount": "1", "paid_orders": "1",
                "quantity": "1", "product_paid_amount": "1", "refund_amount": "1",
                "sales_amount": "1", "sold_quantity": "1", "sales_share": "1",
                "weighted_avg_paid_price": "1",
                "product_gross_profit_reference": "1",
                "erp_gross_profit_reference": "1"}
        with self.assertRaisesRegex(ValueError, "^analysis_source_too_large$"):
            _load(_stored_artifact(payload=_dataset_payload(wide)))

    def test_canonical_projection_size_is_bounded(self):
        fat = {"shop_ref": S1, "paid_orders": "1" * 50, "quantity": "1" * 50,
               "product_paid_amount": "1" * 50, "refund_amount": "1" * 50,
               "sales_amount": "1" * 50, "sold_quantity": "1" * 50,
               "sales_share": "1" * 50,
               "weighted_avg_paid_price": "1" * 50,
               "product_gross_profit_reference": "1" * 50}
        rows = [dict(fat, paid_amount=str(index)) for index in range(500)]
        self.assertEqual(len({row["paid_amount"] for row in rows}), 500,
                         "行必须互不相同，否则先撞重复而不是尺寸")
        with self.assertRaisesRegex(ValueError, "^analysis_source_too_large$"):
            _load(_stored_artifact(payload=_dataset_payload(*rows)))

    def test_coverage_must_be_complete_or_gapped_partial(self):
        for coverage in (_OMIT,
                         {"status": "partial", "start": "2026-09-01",
                          "end": "2026-09-08", "gaps": []},
                         {"status": "missing", "start": "2026-09-01",
                          "end": "2026-09-08", "gaps": []},
                         {"status": "complete", "start": "2026-09-01",
                          "end": "2026-09-08", "gaps": ["2026-09-02~2026-09-03"]}):
            with self.subTest(coverage=coverage):
                with self.assertRaisesRegex(
                        ValueError, "^analysis_source_coverage_incomplete$"):
                    _load(_stored_artifact(payload=_dataset_payload(
                        _row(), coverage=coverage)))
        partial = _load(_stored_artifact(payload=_dataset_payload(
            _row(),
            coverage={"status": "partial", "start": "2026-09-01",
                      "end": "2026-09-08", "gaps": ["2026-09-02~2026-09-03"]})))
        self.assertEqual(len(partial.observations), 1)

    def test_future_data_as_of_is_rejected(self):
        from datetime import timedelta

        stored = _stored_artifact(
            payload=_dataset_payload(_row()),
            data_as_of=datetime(2026, 9, 16, tzinfo=timezone.utc))
        with self.assertRaisesRegex(ValueError, "^analysis_source_time_invalid$"):
            _load(stored)
        stale_ok = _stored_artifact(
            payload=_dataset_payload(_row()),
            data_as_of=datetime(2026, 9, 15, tzinfo=timezone.utc) - timedelta(days=1))
        self.assertEqual(len(_load(stale_ok).observations), 1)

    def test_dataset_carries_frozen_source_identity(self):
        from bi_agent.commerce.metrics import (COMMERCE_GRAPH_VERSION,
                                               COMMERCE_METRIC_VERSION)

        stored = _stored_artifact(payload=_dataset_payload(_row()))
        dataset = _load(stored)
        self.assertEqual(dataset.source_artifact_ref, str(stored.ref.id))
        self.assertEqual(dataset.metric_version, stored.provenance.metric_version)
        self.assertEqual(dataset.limitations, ())
        # commerce 形状的当前常量同样是“当前版本”：血缘随领域，不随 business_query。
        commerce = _stored_artifact(provenance=_provenance(
            metric_version=COMMERCE_METRIC_VERSION,
            graph_version=COMMERCE_GRAPH_VERSION))
        self.assertEqual(_load(commerce).metric_version, COMMERCE_METRIC_VERSION)


# ---------------------------------------------------------------------------
# 计划 Task 3：确定性 Decimal 计算的共用构造器与访问器
# ---------------------------------------------------------------------------


def _calc_obs(row_ref, metrics, previous_metrics=None):
    return _observation(row_ref=row_ref, metrics=metrics,
                        previous_metrics=previous_metrics or {})


def _calc_dataset(observations, *, limitations=()):
    from bi_agent.analysis.models import AnalysisDataset

    return AnalysisDataset(
        source_artifact_ref=UUID_V4, source_fingerprint=FINGERPRINT,
        metric_version=METRIC_VERSION, observations=tuple(observations),
        limitations=tuple(limitations))


def _compute_rows(observations, kinds, *, limitations=()):
    from bi_agent.analysis.calculations import compute_findings

    return compute_findings(_calc_dataset(observations, limitations=limitations),
                            tuple(kinds))


def _dump(finding):
    return finding.model_dump(mode="json")


def _single_row_finding(findings, row_ref, kind=None):
    matched = [item for item in findings if item.row_refs == (row_ref,)
               and (kind is None or item.kind == kind)]
    if len(matched) != 1:
        raise AssertionError(f"expected exactly one finding for {row_ref}, "
                             f"got {len(matched)}")
    return matched[0]


def _value(findings, row_ref, key, kind=None):
    return _single_row_finding(findings, row_ref, kind).values[key]


def _provenance(*, metric_version=None, graph_version=None):
    from bi_agent.runtime.artifacts import QueryProvenance

    fields = {}
    if metric_version is not None:
        fields["metric_version"] = metric_version
    if graph_version is not None:
        fields["graph_version"] = graph_version
    return QueryProvenance(**fields)


class AnalysisCalculationTests(unittest.TestCase):
    """计划 Task 3：Decimal 纯函数、顺序不变、fail-closed 与固定模板。"""

    def test_contribution_and_change_reconcile_exactly(self):
        findings = _compute_rows(
            (_calc_obs("row-a", {"paid_amount": Decimal("30.00")},
                       {"paid_amount": Decimal("20.00")}),
             _calc_obs("row-b", {"paid_amount": Decimal("70.00")},
                       {"paid_amount": Decimal("80.00")})),
            ("contribution", "change_decomposition"))
        self.assertEqual(_value(findings, "row-a", "contribution",
                                kind="contribution"), "0.300000")
        self.assertEqual(_value(findings, "row-b", "contribution",
                                kind="contribution"), "0.700000")
        self.assertEqual(_value(findings, "row-a", "change",
                                kind="change_decomposition"), "10.00")
        self.assertEqual(_value(findings, "row-b", "change",
                                kind="change_decomposition"), "-10.00")
        self.assertEqual(_value(findings, "row-b", "change_rate",
                                kind="change_decomposition"), "-0.125000")
        changes = sum((Decimal(item.values["change"]) for item in findings
                       if item.kind == "change_decomposition"), Decimal(0))
        self.assertEqual(changes, Decimal("0.00"))

    def test_contribution_rounding_stays_within_the_plan_bound(self):
        rows = tuple(
            _calc_obs(f"row-{index:03d}", {"paid_amount": Decimal(value)})
            for index, value in enumerate(("1.00", "2.00", "10.00"), start=1))
        findings = _compute_rows(rows, ("contribution",))
        total = sum((Decimal(item.values["contribution"])
                     for item in findings), Decimal(0))
        self.assertLessEqual(abs(total - Decimal(1)),
                             Decimal("0.000001") * len(rows))
        self.assertEqual(total, Decimal("1.000000"))

    def test_zero_total_publishes_no_contribution(self):
        findings = _compute_rows(
            (_calc_obs("row-a", {"paid_amount": Decimal("-50.00")}),
             _calc_obs("row-b", {"paid_amount": Decimal("50.00")})),
            ("contribution",))
        self.assertEqual(findings, ())

    def test_change_rate_is_omitted_when_previous_is_zero(self):
        findings = _compute_rows(
            (_calc_obs("row-a", {"paid_amount": Decimal("25.00")},
                       {"paid_amount": Decimal("0.00")}),),
            ("change_decomposition",))
        finding = _single_row_finding(findings, "row-a")
        self.assertEqual(finding.values,
                         {"current": "25.00", "previous": "0.00",
                          "change": "25.00"})
        self.assertNotIn("change_rate", finding.values)

    def test_missing_previous_never_becomes_a_zero_baseline(self):
        from bi_agent.analysis.calculations import PREVIOUS_PERIOD_UNAVAILABLE

        self.assertEqual(PREVIOUS_PERIOD_UNAVAILABLE,
                         "previous_period_unavailable")
        findings = _compute_rows(
            (_calc_obs("row-a", {"paid_amount": Decimal("10.00")},
                       {"paid_amount": Decimal("10.00")}),
             _calc_obs("row-b", {"paid_amount": Decimal("20.00")})),
            ("change_decomposition",))
        self.assertEqual([item.row_refs for item in findings], [("row-a",)])

    def test_mad_flags_outlier_without_float(self):
        findings = _compute_rows(
            tuple(_calc_obs(f"row-{index:03d}", {"paid_amount": Decimal(value)})
                  for index, value in enumerate(
                      ("10.00", "10.00", "11.00", "100.00"), start=1)),
            ("anomaly_candidates",))
        self.assertEqual([item.row_refs[0] for item in findings], ["row-004"])
        finding = findings[0]
        self.assertEqual(finding.values["method"], "median_absolute_deviation")
        self.assertEqual(finding.values["score"], "120.735500")
        self.assertEqual(finding.values["center"], "10.50")
        self.assertEqual(finding.values["mad"], "0.50")
        self.assertEqual(finding.statement_code, "mad_outlier_candidate")

    def test_mad_needs_four_observations(self):
        findings = _compute_rows(
            tuple(_calc_obs(f"row-{index:03d}", {"paid_amount": Decimal(value)})
                  for index, value in enumerate(("1.00", "1.00", "999.00"),
                                                start=1)),
            ("anomaly_candidates",))
        self.assertEqual(findings, ())

    def test_mad_zero_with_all_equal_values_is_silent(self):
        findings = _compute_rows(
            tuple(_calc_obs(f"row-{index:03d}", {"paid_amount": Decimal("10.00")})
                  for index in range(1, 5)),
            ("anomaly_candidates",))
        self.assertEqual(findings, ())

    def test_mad_zero_non_median_is_encoded_as_a_code(self):
        findings = _compute_rows(
            tuple(_calc_obs(f"row-{index:03d}", {"paid_amount": Decimal(value)})
                  for index, value in enumerate(
                      ("10.00", "10.00", "10.00", "50.00"), start=1)),
            ("anomaly_candidates",))
        finding = _single_row_finding(findings, "row-004")
        self.assertEqual(finding.values["score"], "mad_zero_non_median")
        self.assertEqual(finding.values["center"], "10.00")
        self.assertEqual(finding.values["mad"], "0.00")

    def test_mad_handles_negative_metrics_symmetrically(self):
        findings = _compute_rows(
            tuple(_calc_obs(f"row-{index:03d}", {"paid_amount": Decimal(value)})
                  for index, value in enumerate(
                      ("-10.00", "-10.00", "-11.00", "-100.00"), start=1)),
            ("anomaly_candidates",))
        finding = _single_row_finding(findings, "row-004")
        self.assertEqual(finding.values["score"], "120.735500")
        self.assertEqual(finding.values["center"], "-10.50")
        self.assertEqual(finding.values["value"], "-100.00")

    def test_multiple_metrics_are_sorted_and_independent(self):
        findings = _compute_rows(
            (_calc_obs("row-a", {"paid_amount": Decimal("30.00"),
                                 "quantity": Decimal("2.00")},
                       {"paid_amount": Decimal("20.00"),
                        "quantity": Decimal("1.00")}),
             _calc_obs("row-b", {"paid_amount": Decimal("70.00"),
                                 "quantity": Decimal("3.00")},
                       {"paid_amount": Decimal("80.00"),
                        "quantity": Decimal("1.00")})),
            ("contribution", "change_decomposition"))
        identity = [(item.kind, item.metric, item.row_refs)
                    for item in findings]
        self.assertEqual(identity, [
            ("contribution", "paid_amount", ("row-a",)),
            ("contribution", "paid_amount", ("row-b",)),
            ("contribution", "quantity", ("row-a",)),
            ("contribution", "quantity", ("row-b",)),
            ("change_decomposition", "paid_amount", ("row-a",)),
            ("change_decomposition", "paid_amount", ("row-b",)),
            ("change_decomposition", "quantity", ("row-a",)),
            ("change_decomposition", "quantity", ("row-b",)),
        ])
        shares = {(item.metric, item.row_refs[0]): item.values["contribution"]
                  for item in findings if item.kind == "contribution"}
        self.assertEqual(shares, {
            ("paid_amount", "row-a"): "0.300000",
            ("paid_amount", "row-b"): "0.700000",
            ("quantity", "row-a"): "0.400000",
            ("quantity", "row-b"): "0.600000",
        })

    def test_output_is_invariant_under_observation_and_key_order(self):
        rows = (_calc_obs("row-a", {"paid_amount": Decimal("30.00"),
                                    "quantity": Decimal("2.00")},
                          {"paid_amount": Decimal("20.00"),
                           "quantity": Decimal("1.00")}),
                _calc_obs("row-b", {"paid_amount": Decimal("70.00"),
                                    "quantity": Decimal("3.00")},
                          {"paid_amount": Decimal("80.00"),
                           "quantity": Decimal("1.00")}))
        kinds = ("contribution", "change_decomposition",
                 "anomaly_candidates", "followups")
        base = [_dump(item) for item in _compute_rows(rows, kinds)]
        flipped = [_dump(item) for item in _compute_rows(
            tuple(_calc_obs(item.row_ref,
                            dict(reversed(list(item.metrics.items()))),
                            dict(reversed(list(item.previous_metrics.items()))))
                  for item in reversed(rows)), kinds)]
        self.assertEqual(base, flipped)
        # 反向突变：数值真的变了输出必须变，防“恒等输出”假绿。
        bumped = (_calc_obs("row-a", {"paid_amount": Decimal("31.00"),
                                      "quantity": Decimal("2.00")},
                            {"paid_amount": Decimal("20.00"),
                             "quantity": Decimal("1.00")}),
                  rows[1])
        self.assertNotEqual(base,
                            [_dump(item) for item in _compute_rows(bumped, kinds)])

    def test_extreme_bounded_decimals_stay_exact(self):
        findings = _compute_rows(
            (_calc_obs("row-a", {"paid_amount":
                                 Decimal("60000000000000000000000.00")},
                       {"paid_amount":
                        Decimal("59000000000000000000000.00")}),
             _calc_obs("row-b", {"paid_amount":
                                 Decimal("40000000000000000000000.00")},
                       {"paid_amount":
                        Decimal("40000000000000000000000.00")})),
            ("contribution", "change_decomposition"))
        shares = {(item.metric, item.row_refs[0]): item.values["contribution"]
                  for item in findings if item.kind == "contribution"}
        self.assertEqual(shares, {
            ("paid_amount", "row-a"): "0.600000",
            ("paid_amount", "row-b"): "0.400000",
        })
        finding = _single_row_finding(findings, "row-a",
                                      kind="change_decomposition")
        self.assertEqual(finding.values["change"],
                         "1000000000000000000000.00")
        self.assertEqual(finding.values["change_rate"], "0.016949")

    def test_oversized_or_overprecise_values_fail_closed(self):
        with self.assertRaisesRegex(ValueError,
                                    "^analysis_value_out_of_range$"):
            _compute_rows((_calc_obs(
                "row-a", {"paid_amount": Decimal("1" + "0" * 30)}),),
                ("contribution",))
        with self.assertRaisesRegex(ValueError,
                                    "^analysis_value_out_of_range$"):
            _compute_rows(
                (_calc_obs("row-a", {"paid_amount": Decimal("0." + "1" * 250)},
                           {"paid_amount": Decimal("0." + "2" * 250)}),),
                ("change_decomposition",))

    def test_repeated_kinds_are_computed_once(self):
        rows = (_calc_obs("row-a", {"paid_amount": Decimal("30.00")}),)
        base = [_dump(item) for item in _compute_rows(rows, ("contribution",))]
        repeated = [_dump(item) for item in
                    _compute_rows(rows, ("contribution", "contribution"))]
        self.assertEqual(base, repeated)

    def test_unknown_kind_fails_closed(self):
        with self.assertRaisesRegex(ValueError, "^analysis_kind_invalid$"):
            _compute_rows(
                (_calc_obs("row-a", {"paid_amount": Decimal("1.00")}),),
                ("contribution", "python"))

    def test_followups_use_only_the_three_fixed_templates(self):
        rows = [_calc_obs("row-001", {"paid_amount": Decimal("10.00")},
                          {"paid_amount": Decimal("10.00")}),
                _calc_obs("row-002", {"paid_amount": Decimal("10.00")},
                          {"paid_amount": Decimal("10.00")}),
                _calc_obs("row-003", {"paid_amount": Decimal("11.00")},
                          {"paid_amount": Decimal("10.00")}),
                _calc_obs("row-004", {"paid_amount": Decimal("100.00")})]
        kinds = ("contribution", "change_decomposition",
                 "anomaly_candidates", "followups")
        findings = _compute_rows(rows, kinds, limitations=("coverage_partial",))
        followups = [item for item in findings if item.kind == "followups"]
        self.assertEqual([item.statement_code for item in followups],
                         ["inspect_coverage_gaps", "supply_previous_period",
                          "verify_source_batch"])
        texts = {item.values["followup"] for item in followups}
        self.assertEqual(texts, {"检查 coverage gaps", "补充 previous period",
                                 "核对 row-004 的来源批次"})
        for item in findings:
            for text in item.values.values():
                for banned in ("采购", "改价", "投放", "立即", "自动"):
                    self.assertNotIn(banned, text)

    def test_followups_are_gated_by_their_evidence_kinds(self):
        rows = (_calc_obs("row-001", {"paid_amount": Decimal("10.00")}),
                _calc_obs("row-002", {"paid_amount": Decimal("100.00")}))
        self.assertEqual(_compute_rows(rows, ("followups",)), ())
        gated = _compute_rows(rows, ("followups",),
                              limitations=("coverage_partial",))
        self.assertEqual([item.statement_code for item in gated],
                         ["inspect_coverage_gaps"])
        self.assertEqual(gated[0].row_refs, ("row-001", "row-002"))

    def test_statement_code_vocabulary_is_fixed(self):
        rows = [_calc_obs("row-001", {"paid_amount": Decimal("10.00")},
                          {"paid_amount": Decimal("10.00")}),
                _calc_obs("row-002", {"paid_amount": Decimal("10.00")}),
                _calc_obs("row-003", {"paid_amount": Decimal("10.00")}),
                _calc_obs("row-004", {"paid_amount": Decimal("100.00")})]
        findings = _compute_rows(rows, ("contribution", "change_decomposition",
                                        "anomaly_candidates", "followups"),
                                 limitations=("coverage_partial",))
        self.assertEqual({item.statement_code for item in findings},
                         {"contribution_share", "change_vs_previous",
                          "mad_outlier_candidate", "verify_source_batch",
                          "supply_previous_period", "inspect_coverage_gaps"})

    def test_package_reexports_the_calculation_surface(self):
        import bi_agent.analysis
        from bi_agent.analysis import calculations

        self.assertIs(bi_agent.analysis.compute_findings,
                      calculations.compute_findings)
        self.assertIs(bi_agent.analysis.PREVIOUS_PERIOD_UNAVAILABLE,
                      calculations.PREVIOUS_PERIOD_UNAVAILABLE)


class AnalysisGoldTests(unittest.TestCase):
    """gold fixture：每个字段逐字相等；反序输入不变；错期望与改输入都转红。"""

    @staticmethod
    def _cases():
        path = pathlib.Path(__file__).parent / "fixtures" / "analysis_gold.json"
        return json.loads(path.read_text(encoding="utf-8"))["cases"]

    def _payloads(self, case):
        from bi_agent.analysis.calculations import compute_findings
        from bi_agent.analysis.models import AnalysisDataset, AnalysisObservation

        data = case["dataset"]
        dataset = AnalysisDataset(
            source_artifact_ref=data["source_artifact_ref"],
            source_fingerprint=data["source_fingerprint"],
            metric_version=data["metric_version"],
            observations=tuple(AnalysisObservation(**observation)
                               for observation in data["observations"]),
            limitations=tuple(data["limitations"]))
        return [finding.model_dump(mode="json") for finding in
                compute_findings(dataset, tuple(case["kinds"]))]

    @staticmethod
    def _flipped(case):
        data = case["dataset"]
        observations = []
        for observation in reversed(data["observations"]):
            observations.append({
                "row_ref": observation["row_ref"],
                "dimensions": dict(
                    reversed(list(observation["dimensions"].items()))),
                "metrics": dict(
                    reversed(list(observation["metrics"].items()))),
                "previous_metrics": dict(
                    reversed(list(observation["previous_metrics"].items()))),
            })
        return {**case, "dataset": {**data, "observations": observations}}

    def test_every_gold_case_matches_field_by_field(self):
        for case in self._cases():
            with self.subTest(case=case["name"]):
                self.assertEqual(self._payloads(case),
                                 case["expected_findings"])

    def test_gold_findings_survive_reversed_input_order(self):
        for case in self._cases():
            with self.subTest(case=case["name"]):
                self.assertEqual(self._payloads(self._flipped(case)),
                                 case["expected_findings"])

    def test_gold_expectations_are_not_tautological(self):
        case = copy.deepcopy(self._cases()[0])
        original = self._payloads(case)
        self.assertEqual(original, case["expected_findings"])
        # 错期望：改一个数值字符，比较必须立刻转红。
        wrong = copy.deepcopy(case["expected_findings"])
        key, value = next(iter(wrong[0]["values"].items()))
        wrong[0]["values"][key] = value[:-1] + ("1" if value[-1] != "1" else "2")
        self.assertNotEqual(original, wrong)
        # 错引用：finding_ref 变了也必须被发现。
        wrong_ref = copy.deepcopy(case["expected_findings"])
        tail = wrong_ref[0]["finding_ref"][-1]
        wrong_ref[0]["finding_ref"] = (
            wrong_ref[0]["finding_ref"][:-1] + ("0" if tail != "0" else "1"))
        self.assertNotEqual(original, wrong_ref)
        # 改输入：数据动一格，输出必须跟着动。
        perturbed = copy.deepcopy(case)
        metrics = perturbed["dataset"]["observations"][0]["metrics"]
        metric = next(iter(metrics))
        metrics[metric] = str(Decimal(metrics[metric]) + Decimal("1"))
        self.assertNotEqual(self._payloads(perturbed),
                            case["expected_findings"])


# ---------------------------------------------------------------------------
# 计划 Task 4：无工具叙事总结与 claim 守卫的共用替身与构造器
# ---------------------------------------------------------------------------


class _Reply:
    """`ChatModel.complete` 返回值的最小替身：总结器只读 `.text`。"""

    def __init__(self, text):
        self.text = text


class RecordingModel:
    """记录每次 complete() 的 tools/timeout/messages，并回复构造时给的文本。"""

    def __init__(self, text=""):
        self.text = text
        self.calls: list[dict[str, object]] = []

    def complete(self, messages, tools, *, timeout_s):
        self.calls.append({
            "tools": list(tools),
            "timeout_s": timeout_s,
            "messages_as_text": "\n".join(message.content or ""
                                          for message in messages),
        })
        return _Reply(self.text)


class FakeModel(RecordingModel):
    """计划伪代码里的命名；行为与 RecordingModel 一致。"""


class FailingModel:
    """模型失败替身：按现有 ChatModel 契约抛 `ModelError`，绝不发网络请求。"""

    def __init__(self, code="timeout"):
        self.code = code
        self.calls: list[dict[str, object]] = []

    def complete(self, messages, tools, *, timeout_s):
        from bi_agent.llm import ModelError

        self.calls.append({"tools": list(tools), "timeout_s": timeout_s})
        raise ModelError(self.code)


def _summarizer_findings():
    return (_finding("finding-a"),
            _finding("finding-b",
                     values={"share": "0.000000", "value": "0.00"}))


def _summarizer_dataset(*, limitations=()):
    return _calc_dataset(
        (_calc_obs("row-001", {"paid_amount": Decimal("12.30")}),
         _calc_obs("row-002", {"paid_amount": Decimal("7.70")})),
        limitations=limitations)


def _deadline(seconds=30.0):
    return time.monotonic() + seconds


def _narrative_reply(*, text="paid_amount 的值为 12.30，贡献占比 1.000000。",
                     finding_refs=("finding-a",), claim_kind="observation",
                     extra=None):
    item = {"text": text, "finding_refs": list(finding_refs),
            "claim_kind": claim_kind}
    if extra:
        item.update(extra)
    return json.dumps({"narrative": [item]}, ensure_ascii=False)


class AnalysisSummarizerTests(unittest.TestCase):
    """计划 Task 4：空 tools、因果降级、失败保底、预算与剩余 deadline。"""

    def test_model_receives_findings_and_empty_tools(self):
        from bi_agent.analysis.summarizer import summarize_findings

        model = RecordingModel(_narrative_reply(finding_refs=["finding-a"]))
        result = summarize_findings(_summarizer_findings(), limitations=(),
                                    model=model, timeout_s=3.0)
        self.assertEqual(model.calls[0]["tools"], [])
        self.assertNotIn("database",
                         str(model.calls[0]["messages_as_text"]).lower())
        self.assertEqual(model.calls[0]["timeout_s"], 3.0)
        # 通过守卫的叙事按原引用发布，且不夹带任何无证据说法。
        self.assertEqual(result.narrative[0].finding_refs, ("finding-a",))
        self.assertEqual(result.unsupported_claims, ())

    def test_uncited_causal_claim_is_not_published_as_fact(self):
        from bi_agent.analysis.summarizer import summarize_findings

        result = summarize_findings(
            _summarizer_findings(), limitations=(),
            model=FakeModel("销量下降是因为广告停投"), timeout_s=3.0)
        self.assertEqual(result.narrative, ())
        self.assertEqual(result.unsupported_claims, ("销量下降是因为广告停投",))

    def test_model_failure_keeps_deterministic_findings(self):
        from bi_agent.analysis.summarizer import run_isolated_analysis

        result = run_isolated_analysis(_summarizer_dataset(),
                                       model=FailingModel(),
                                       kinds=("contribution",),
                                       deadline=_deadline())
        self.assertTrue(result.findings)
        self.assertIn("narrative_unavailable", result.limitations)
        self.assertEqual(result.narrative, ())
        self.assertEqual(result.unsupported_claims, ())

    def test_verbatim_numbers_publish_but_invented_numbers_do_not(self):
        from bi_agent.analysis.summarizer import summarize_findings

        good = summarize_findings(_summarizer_findings(), limitations=(),
                                  model=RecordingModel(_narrative_reply()),
                                  timeout_s=3.0)
        self.assertEqual(len(good.narrative), 1)
        self.assertEqual(good.unsupported_claims, ())
        bad = summarize_findings(
            _summarizer_findings(), limitations=(),
            model=RecordingModel(_narrative_reply(
                text="paid_amount 约为 12.31。")),
            timeout_s=3.0)
        self.assertEqual(bad.narrative, ())
        self.assertEqual(bad.unsupported_claims, ("paid_amount 约为 12.31。",))

    def test_numeric_evidence_is_checked_per_value_not_across_boundary(self):
        """P2 回归：0.5 与 20 拼出 0.520，不能让挑造的 520 冒充逐字证据。"""
        from bi_agent.analysis.summarizer import summarize_findings

        findings = (_finding("finding-a",
                             values={"share": "0.5", "value": "20"}),)
        fabricated = summarize_findings(
            findings, limitations=(),
            model=RecordingModel(_narrative_reply(
                text="paid_amount 合计 520。",
                finding_refs=("finding-a",))),
            timeout_s=3.0)
        self.assertEqual(fabricated.narrative, ())
        self.assertEqual(fabricated.unsupported_claims,
                         ("paid_amount 合计 520。",))
        # 正向对照：逐字出现在单个 value 里的数字照常发布。
        verbatim = summarize_findings(
            findings, limitations=(),
            model=RecordingModel(_narrative_reply(
                text="paid_amount 的值为 20，占比 0.5。",
                finding_refs=("finding-a",))),
            timeout_s=3.0)
        self.assertEqual(len(verbatim.narrative), 1)
        self.assertEqual(verbatim.unsupported_claims, ())

    def test_unknown_finding_ref_is_never_published(self):
        from bi_agent.analysis.summarizer import summarize_findings

        result = summarize_findings(
            _summarizer_findings(), limitations=(),
            model=RecordingModel(_narrative_reply(finding_refs=["finding-zz"])),
            timeout_s=3.0)
        self.assertEqual(result.narrative, ())
        self.assertEqual(len(result.unsupported_claims), 1)

    def test_causal_and_action_language_stays_out_even_with_valid_citations(self):
        from bi_agent.analysis.summarizer import summarize_findings

        for text in ("paid_amount 为 12.30，因为广告停投。",
                     "paid_amount 为 12.30，导致库存偏低。",
                     "paid_amount 为 12.30，应该立即补货。",
                     "paid_amount 为 12.30，建议马上采购。",
                     "paid_amount 为 12.30，系统将自动改价。"):
            with self.subTest(text=text):
                result = summarize_findings(
                    _summarizer_findings(), limitations=(),
                    model=RecordingModel(_narrative_reply(text=text)),
                    timeout_s=3.0)
                self.assertEqual(result.narrative, ())
                self.assertEqual(result.unsupported_claims, (text,))

    def test_hypothesis_requires_pending_validation_marker(self):
        from bi_agent.analysis.summarizer import summarize_findings

        unmarked = summarize_findings(
            _summarizer_findings(), limitations=(),
            model=RecordingModel(_narrative_reply(
                text="paid_amount 的 12.30 也许与促销有关。",
                claim_kind="hypothesis")),
            timeout_s=3.0)
        self.assertEqual(unmarked.hypotheses, ())
        self.assertEqual(unmarked.narrative, ())
        self.assertEqual(len(unmarked.unsupported_claims), 1)
        marked_text = "假设 paid_amount 的 12.30 与促销有关，待验证。"
        marked = summarize_findings(
            _summarizer_findings(), limitations=(),
            model=RecordingModel(_narrative_reply(
                text=marked_text, claim_kind="hypothesis")),
            timeout_s=3.0)
        self.assertEqual(marked.hypotheses, (marked_text,))
        self.assertEqual(marked.narrative, ())
        self.assertEqual(marked.unsupported_claims, ())

    def test_causal_wording_is_unsupported_in_hypotheses_too(self):
        """P2 回归（严格安全解释）：含因果词的假设句进 unsupported，不进 hypotheses。"""
        from bi_agent.analysis.summarizer import summarize_findings

        result = summarize_findings(
            _summarizer_findings(), limitations=(),
            model=RecordingModel(_narrative_reply(
                text="假设 paid_amount 的 12.30 因为广告停投下滑，待验证。",
                claim_kind="hypothesis")),
            timeout_s=3.0)
        self.assertEqual(result.hypotheses, ())
        self.assertEqual(result.narrative, ())
        self.assertEqual(result.unsupported_claims,
                         ("假设 paid_amount 的 12.30 因为广告停投下滑，待验证。",))
        # 正向对照：无因果词的合格假设句照旧发布。
        marked = summarize_findings(
            _summarizer_findings(), limitations=(),
            model=RecordingModel(_narrative_reply(
                text="假设 paid_amount 的 12.30 与促销有关，待验证。",
                claim_kind="hypothesis")),
            timeout_s=3.0)
        self.assertEqual(marked.hypotheses,
                         ("假设 paid_amount 的 12.30 与促销有关，待验证。",))

    def test_malformed_and_extra_field_replies_move_to_unsupported(self):
        from bi_agent.analysis.summarizer import (NarrativeResult,
                                                  summarize_findings)

        cases = [
            ("free_text", "看起来整体平稳。"),
            ("object_with_extra_field",
             json.dumps({"narrative": [], "confidence": 1}, ensure_ascii=False)),
            ("item_with_extra_field", _narrative_reply(extra={"score": 1})),
            ("item_is_not_object",
             json.dumps(["看起来整体平稳。"], ensure_ascii=False)),
        ]
        for label, reply in cases:
            with self.subTest(case=label):
                result = summarize_findings(_summarizer_findings(),
                                            limitations=(),
                                            model=RecordingModel(reply),
                                            timeout_s=3.0)
                self.assertEqual(result.narrative, ())
                self.assertTrue(result.unsupported_claims)
        # 恰好 {"narrative": []} 是合法的“无可叙事”：不产生任何 claim。
        empty = summarize_findings(
            _summarizer_findings(), limitations=(),
            model=RecordingModel(json.dumps({"narrative": []})), timeout_s=3.0)
        self.assertEqual(empty, NarrativeResult())

    def test_overlong_malformed_reply_degrades_without_losing_findings(self):
        """P1 回归：装不进有界 claim 通道的畸形回复必须降级，不得炸运行。"""
        from bi_agent.analysis.summarizer import run_isolated_analysis

        model = RecordingModel("x" * 499 + " tail")
        result = run_isolated_analysis(_summarizer_dataset(), model=model,
                                       kinds=("contribution",),
                                       deadline=_deadline())
        self.assertTrue(result.findings)
        self.assertIn("narrative_unavailable", result.limitations)
        self.assertEqual(result.narrative, ())
        self.assertEqual(result.unsupported_claims, ())
        self.assertEqual(len(model.calls), 1)

    def test_truncated_claim_never_keeps_trailing_whitespace(self):
        """P1 回归：claim 文本截断落点为空白时必须再 strip，不得入库尾随空白。"""
        from bi_agent.analysis.summarizer import summarize_findings

        result = summarize_findings(
            _summarizer_findings(), limitations=(),
            model=RecordingModel(_narrative_reply(text="y" * 499 + " tail")),
            timeout_s=3.0)
        self.assertEqual(result.narrative, ())
        self.assertEqual(result.unsupported_claims, ("y" * 499,))

    def test_empty_reply_degrades_to_narrative_unavailable(self):
        from bi_agent.analysis.summarizer import run_isolated_analysis

        model = RecordingModel(None)
        result = run_isolated_analysis(_summarizer_dataset(), model=model,
                                       kinds=("contribution",),
                                       deadline=_deadline())
        self.assertTrue(result.findings)
        self.assertIn("narrative_unavailable", result.limitations)
        self.assertEqual(result.narrative, ())
        self.assertEqual(result.unsupported_claims, ())
        self.assertEqual(len(model.calls), 1)

    def test_no_model_and_short_deadline_skip_the_model_call(self):
        from bi_agent.analysis.summarizer import run_isolated_analysis

        without_model = run_isolated_analysis(
            _summarizer_dataset(), model=None, kinds=("contribution",),
            deadline=_deadline())
        self.assertIn("narrative_unavailable", without_model.limitations)
        self.assertTrue(without_model.findings)

        model = RecordingModel(_narrative_reply())
        result = run_isolated_analysis(_summarizer_dataset(), model=model,
                                       kinds=("contribution",),
                                       deadline=_deadline(1.0))
        self.assertEqual(model.calls, [])
        self.assertIn("narrative_unavailable", result.limitations)
        self.assertTrue(result.findings)

    def test_remaining_deadline_is_passed_as_timeout(self):
        from bi_agent.analysis.summarizer import run_isolated_analysis

        model = RecordingModel(_narrative_reply())
        run_isolated_analysis(_summarizer_dataset(), model=model,
                              kinds=("contribution",), deadline=_deadline(30.0))
        timeout_s = model.calls[0]["timeout_s"]
        self.assertGreater(timeout_s, 2.0)
        self.assertLessEqual(timeout_s, 30.0)

    def test_input_token_bound_rejects_before_any_model_call(self):
        from bi_agent.analysis.summarizer import summarize_findings

        oversized = tuple(
            _finding(f"finding-{index:03d}",
                     values={f"value_{position}": "9" * 200
                             for position in range(10)})
            for index in range(100))
        model = RecordingModel(_narrative_reply())
        with self.assertRaisesRegex(ValueError, "^narrative_input_too_large$"):
            summarize_findings(oversized, limitations=(), model=model,
                               timeout_s=3.0)
        self.assertEqual(model.calls, [])

    def test_prompt_carries_only_findings_and_limitations(self):
        from bi_agent.analysis.summarizer import summarize_findings

        model = RecordingModel(_narrative_reply())
        summarize_findings(_summarizer_findings(),
                           limitations=("coverage_partial",),
                           model=model, timeout_s=3.0)
        prompt = str(model.calls[0]["messages_as_text"])
        self.assertIn("finding-a", prompt)
        self.assertIn("coverage_partial", prompt)
        self.assertIn("paid_amount", prompt)
        # 来源行投影、实体值与内部通道词汇到不了模型。
        for banned in ("ent-shop-a", "psycopg", "database", "select ", "http"):
            self.assertNotIn(banned, prompt.lower())

    def test_run_publishes_guarded_narrative_with_provenance(self):
        from bi_agent.analysis.calculations import compute_findings
        from bi_agent.analysis.summarizer import run_isolated_analysis
        from bi_agent.runtime.models import validate_artifact_payload

        dataset = _summarizer_dataset(limitations=("coverage_partial",))
        first, second = compute_findings(dataset, ("contribution",))
        self.assertEqual(first.values["value"], "12.30")
        self.assertEqual(first.values["contribution"], "0.615000")
        self.assertEqual(second.values["value"], "7.70")
        reply = json.dumps({"narrative": [
            {"text": "paid_amount 的值为 12.30，贡献占比 0.615000。",
             "finding_refs": [first.finding_ref], "claim_kind": "observation"},
            {"text": "假设 12.30 主要来自单笔大额订单，待验证。",
             "finding_refs": [first.finding_ref], "claim_kind": "hypothesis"},
            {"text": "7.70 的下滑是因为竞争加剧。",
             "finding_refs": [second.finding_ref], "claim_kind": "fact"},
        ]}, ensure_ascii=False)
        result = run_isolated_analysis(dataset, model=RecordingModel(reply),
                                       kinds=("contribution",),
                                       deadline=_deadline())
        self.assertEqual(len(result.narrative), 1)
        self.assertEqual(result.narrative[0]["finding_refs"],
                         (first.finding_ref,))
        self.assertEqual(result.hypotheses,
                         ("假设 12.30 主要来自单笔大额订单，待验证。",))
        self.assertEqual(result.unsupported_claims, ("7.70 的下滑是因为竞争加剧。",))
        self.assertEqual(result.limitations, ("coverage_partial",))
        payload = result.model_dump(mode="json")
        self.assertEqual(validate_artifact_payload(payload, "analysis_result"),
                         payload)

    def test_missing_previous_records_the_plan_limitation(self):
        from bi_agent.analysis.calculations import PREVIOUS_PERIOD_UNAVAILABLE
        from bi_agent.analysis.summarizer import run_isolated_analysis

        partial = run_isolated_analysis(
            _calc_dataset(
                (_calc_obs("row-001", {"paid_amount": Decimal("12.30")},
                           {"paid_amount": Decimal("10.00")}),
                 _calc_obs("row-002", {"paid_amount": Decimal("7.70")}))),
            model=None, kinds=("contribution", "change_decomposition"),
            deadline=_deadline())
        self.assertIn(PREVIOUS_PERIOD_UNAVAILABLE, partial.limitations)
        self.assertIn("narrative_unavailable", partial.limitations)
        complete = run_isolated_analysis(
            _calc_dataset(
                (_calc_obs("row-001", {"paid_amount": Decimal("12.30")},
                           {"paid_amount": Decimal("10.00")}),
                 _calc_obs("row-002", {"paid_amount": Decimal("7.70")},
                           {"paid_amount": Decimal("7.70")}))),
            model=None, kinds=("contribution", "change_decomposition"),
            deadline=_deadline())
        self.assertNotIn(PREVIOUS_PERIOD_UNAVAILABLE, complete.limitations)

    def test_derived_limitations_take_precedence_at_the_19_boundary(self):
        """P2 回归：19 条数据集码 + 2 条派生码必须封顶在 20，不得炸结果构造。"""
        from bi_agent.analysis.calculations import PREVIOUS_PERIOD_UNAVAILABLE
        from bi_agent.analysis.summarizer import run_isolated_analysis

        dataset = _calc_dataset(
            (_calc_obs("row-001", {"paid_amount": Decimal("12.30")}),),
            limitations=tuple(f"code_{index:02d}" for index in range(19)))
        result = run_isolated_analysis(dataset, model=None,
                                       kinds=("contribution",
                                              "change_decomposition"),
                                       deadline=_deadline())
        self.assertTrue(result.findings)
        self.assertEqual(len(result.limitations), 20)
        self.assertEqual(result.limitations[:2],
                         (PREVIOUS_PERIOD_UNAVAILABLE, "narrative_unavailable"))
        self.assertEqual(result.limitations[2:],
                         tuple(f"code_{index:02d}" for index in range(18)))

    def test_derived_limitations_win_the_cap_at_20_dataset_codes(self):
        """P2 回归：20 条数据集码 + 2 条派生码——派生码优先，溢出按序去尾。"""
        from bi_agent.analysis.calculations import PREVIOUS_PERIOD_UNAVAILABLE
        from bi_agent.analysis.summarizer import run_isolated_analysis

        codes = tuple(f"code_{index:02d}" for index in range(20))
        dataset = _calc_dataset(
            (_calc_obs("row-001", {"paid_amount": Decimal("12.30")}),),
            limitations=codes)
        result = run_isolated_analysis(dataset, model=None,
                                       kinds=("contribution",
                                              "change_decomposition"),
                                       deadline=_deadline())
        self.assertTrue(result.findings)
        self.assertEqual(len(result.limitations), 20)
        self.assertEqual(result.limitations[:2],
                         (PREVIOUS_PERIOD_UNAVAILABLE, "narrative_unavailable"))
        self.assertEqual(result.limitations[2:], codes[:18])
        self.assertNotIn("code_18", result.limitations)
        self.assertNotIn("code_19", result.limitations)

    def test_model_failure_with_a_full_limitation_book_keeps_findings(self):
        """P2 回归：20 条数据集码 + 模型失败降级——findings 原样保留。"""
        from bi_agent.analysis.summarizer import run_isolated_analysis

        codes = tuple(f"code_{index:02d}" for index in range(20))
        dataset = _calc_dataset(
            (_calc_obs("row-001", {"paid_amount": Decimal("12.30")}),),
            limitations=codes)
        result = run_isolated_analysis(dataset, model=FailingModel(),
                                       kinds=("contribution",),
                                       deadline=_deadline())
        self.assertTrue(result.findings)
        self.assertEqual(result.limitations,
                         ("narrative_unavailable",) + codes[:19])
        self.assertNotIn("code_19", result.limitations)

    def test_18_dataset_codes_plus_two_derived_fit_exactly(self):
        """边界：18 + 2 恰好 20，全部保留——回归护栏。"""
        from bi_agent.analysis.calculations import PREVIOUS_PERIOD_UNAVAILABLE
        from bi_agent.analysis.summarizer import run_isolated_analysis

        codes = tuple(f"code_{index:02d}" for index in range(18))
        dataset = _calc_dataset(
            (_calc_obs("row-001", {"paid_amount": Decimal("12.30")}),),
            limitations=codes)
        result = run_isolated_analysis(dataset, model=None,
                                       kinds=("contribution",
                                              "change_decomposition"),
                                       deadline=_deadline())
        self.assertTrue(result.findings)
        self.assertEqual(result.limitations,
                         (PREVIOUS_PERIOD_UNAVAILABLE, "narrative_unavailable")
                         + codes)

    def test_dataset_code_duplicating_a_derived_code_is_deduped(self):
        """数据集码与派生码同名时去重，且派生码排前（优先级钉住）。"""
        from bi_agent.analysis.calculations import PREVIOUS_PERIOD_UNAVAILABLE
        from bi_agent.analysis.summarizer import run_isolated_analysis

        dataset = _calc_dataset(
            (_calc_obs("row-001", {"paid_amount": Decimal("12.30")}),),
            limitations=("narrative_unavailable", "coverage_partial"))
        result = run_isolated_analysis(dataset, model=None,
                                       kinds=("contribution",
                                              "change_decomposition"),
                                       deadline=_deadline())
        self.assertEqual(result.limitations,
                         (PREVIOUS_PERIOD_UNAVAILABLE, "narrative_unavailable",
                          "coverage_partial"))

    def test_results_are_deterministic_for_identical_inputs(self):
        from bi_agent.analysis.summarizer import run_isolated_analysis

        def one_run():
            return run_isolated_analysis(
                _summarizer_dataset(),
                model=RecordingModel(_narrative_reply()),
                kinds=("contribution",), deadline=_deadline(5.0))

        self.assertEqual(one_run().model_dump(mode="json"),
                         one_run().model_dump(mode="json"))


class AnalysisGraphTests(unittest.TestCase):
    """计划 Task 5：固定链、fail-closed 持久化、deadline 与稳定拒绝通道。

    六个节点就是六道推进位，顺序死在 ANALYSIS_CHAIN；load/validate 是可信
    服务层（Task 2 loader），compute/summarize 是纯分析运行（Task 3/4），
    persist/finalize 走既有 QueryRunStore CAS 契约。所有拒绝都发生在模型
    调用之前，公开位置只有稳定码，绝无 loader 异常原文。
    """

    _CHAIN_NODES = ["load_source", "validate_source", "compute_findings",
                    "summarize_findings", "persist_analysis", "finalize"]

    # -- 计划 Step 1 的两条骨架用例 ---------------------------------------

    def test_artifact_failure_publishes_no_result(self):
        from bi_agent.analysis.graph import analyze_artifact

        store = _GraphStore(_graph_stored(), fail_save=True)
        result = analyze_artifact(_analysis_request(), context=_context(store),
                                  model=None)
        self.assertEqual(result.status.value, "failed")
        self.assertEqual(result.artifacts, [])
        self.assertNotIn("findings", result.model_payload)
        self.assertNotIn("narrative", result.model_payload)
        # 一次写入，失败不重试：重复写就是对同一证据留两份说法。
        self.assertEqual(store.saves, 1)
        run = store.inner.runs[result.run_id]
        self.assertEqual(run["status"], "failed")
        self.assertEqual(run["error_code"], "artifact_persistence_failed")
        self.assertEqual(run["termination_reason"], "persistence_failed")

    def test_expired_deadline_never_loads_or_calls_model(self):
        from bi_agent.analysis.graph import analyze_artifact

        store = _GraphStore(_graph_stored())
        model = RecordingModel()
        result = analyze_artifact(
            _analysis_request(),
            context=_context(store, deadline=time.monotonic() - 1), model=model)
        self.assertEqual(result.status.value, "failed")
        self.assertEqual(result.model_payload["termination_reason"],
                         "deadline_exceeded")
        self.assertEqual(result.artifacts, [])
        self.assertEqual(model.calls, [])
        self.assertEqual(store.reads, 0, "过期 deadline 在任何读取之前拒绝")

    # -- 授权 / 类型 / 版本：稳定码，不泄露原因原文 -----------------------

    def test_not_found_and_cross_owner_are_needs_input(self):
        from bi_agent.analysis.graph import analyze_artifact

        for label, stored in (("不存在", None),
                              ("跨 owner", _graph_stored(subject_id="subject-b"))):
            with self.subTest(case=label):
                store = _GraphStore(stored)
                result = analyze_artifact(_analysis_request(),
                                          context=_context(store), model=None)
                self.assertEqual(result.status.value, "needs_input")
                self.assertEqual(result.artifacts, [])
                self.assertEqual(result.model_payload["termination_reason"],
                                 "invalid_parameters")
                # 原因码只在映射表里：公开载荷只带映射后的稳定码。
                self.assertNotIn("analysis_source_not_found",
                                 json.dumps(result.model_payload))
                run = store.inner.runs[result.run_id]
                self.assertEqual(run["status"], "needs_input")

    def test_type_and_version_refusals_stay_unavailable_without_a_model(self):
        from bi_agent.analysis.graph import analyze_artifact

        stale = _graph_stored(
            provenance=_provenance(metric_version="metrics/2026-09-01.1"))
        chart = _graph_stored(artifact_type="chart_spec", payload={"kind": "bar"})
        for label, stored, termination in (
                ("图表来源", chart, "invalid_parameters"),
                ("过期血缘", stale, "source_quality_failed")):
            with self.subTest(case=label):
                store = _GraphStore(stored)
                model = RecordingModel()
                result = analyze_artifact(_analysis_request(),
                                          context=_context(store), model=model)
                self.assertEqual(model.calls, [], "模型调用前必须拒绝")
                self.assertEqual(result.artifacts, [])
                expected_status = ("needs_input" if label == "图表来源"
                                   else "failed")
                self.assertEqual(result.status.value, expected_status)
                self.assertEqual(result.model_payload["termination_reason"],
                                 termination)

    # -- 正向路径：固定链推进、恰好一份 artifact、双校验器过卡 ------------

    def test_happy_path_persists_one_validated_artifact_along_the_chain(self):
        from bi_agent.analysis.calculations import compute_findings
        from bi_agent.analysis.graph import analyze_artifact
        from bi_agent.runtime.models import (validate_artifact_payload,
                                             validate_model_payload)

        coverage = {"status": "complete", "start": "2026-09-01",
                    "end": "2026-09-08", "gaps": []}
        data_as_of = datetime(2026, 9, 8, tzinfo=timezone.utc)
        stored = _graph_stored(coverage=coverage, data_as_of=data_as_of)
        # 先用真 loader 学出确定性 finding_ref，叙事才能真的发布。
        dataset = _load(_stored_artifact(payload=stored.payload,
                                         subject_id=stored.subject_id,
                                         provenance=stored.provenance,
                                         coverage=coverage))
        finding_ref = compute_findings(dataset.observations and dataset,
                                       ("contribution",))[0].finding_ref
        narrative = json.dumps({"narrative": [{
            "text": "paid_amount 的值为 12.30，贡献占比 1.000000。",
            "finding_refs": [finding_ref], "claim_kind": "observation"}]},
            ensure_ascii=False)
        store = _GraphStore(stored)
        model = RecordingModel(narrative)
        result = analyze_artifact(_analysis_request(), context=_context(store),
                                  model=model)
        self.assertEqual(result.status.value, "success")
        self.assertEqual(len(result.artifacts), 1)
        artifact = result.artifacts[0]
        self.assertEqual(artifact.ref.type, "analysis_result")
        payload = artifact.public_payload
        self.assertEqual(payload["source_artifact_ref"], UUID_V4)
        self.assertTrue(payload["findings"])
        self.assertEqual(payload["narrative"][0]["claim_kind"], "observation")
        self.assertNotIn("narrative_unavailable", payload["limitations"],
                         "成功发布叙事时不带降级码")
        # 两个持久化校验器都必须逐字放行同一份载荷。
        self.assertEqual(validate_artifact_payload(payload, "analysis_result"),
                         payload)
        self.assertEqual(validate_model_payload(payload, "analysis_result"),
                         payload)
        # 模型面：工具恒为空，超时取自主请求剩余 deadline。
        self.assertEqual(model.calls[0]["tools"], [])
        self.assertGreater(model.calls[0]["timeout_s"], 0)
        self.assertLessEqual(model.calls[0]["timeout_s"], 30.0)
        # 运行记录：节点按固定链推进，终态 succeeded，恰好一份 artifact。
        nodes = [event["node"] for event in store.inner.events[result.run_id]]
        self.assertEqual(nodes, self._CHAIN_NODES)
        run = store.inner.runs[result.run_id]
        self.assertEqual(run["status"], "succeeded")
        self.assertEqual(len(store.inner.artifacts), 1)
        saved = next(iter(store.inner.artifacts.values()))
        self.assertEqual(saved["artifact_type"], "analysis_result")
        self.assertEqual(saved["data_as_of"], data_as_of, "来源时点原样回写")
        self.assertEqual(saved["coverage"], coverage, "来源覆盖原样回写")

    def test_model_failure_still_persists_deterministic_findings(self):
        from bi_agent.analysis.graph import analyze_artifact

        store = _GraphStore(_graph_stored())
        result = analyze_artifact(_analysis_request(), context=_context(store),
                                  model=FailingModel())
        self.assertEqual(result.status.value, "success")
        payload = result.artifacts[0].public_payload
        self.assertTrue(payload["findings"])
        self.assertEqual(payload["narrative"], [])
        self.assertIn("narrative_unavailable", payload["limitations"])

    def test_remaining_under_two_seconds_skips_the_model_call(self):
        from bi_agent.analysis.graph import analyze_artifact

        store = _GraphStore(_graph_stored())
        model = RecordingModel()
        result = analyze_artifact(
            _analysis_request(),
            context=_context(store, deadline=time.monotonic() + 1.0), model=model)
        self.assertEqual(result.status.value, "success")
        self.assertEqual(model.calls, [])
        payload = result.artifacts[0].public_payload
        self.assertIn("narrative_unavailable", payload["limitations"])
        self.assertTrue(payload["findings"])

    def test_oversized_metric_refuses_in_compute_before_any_model_call(self):
        from bi_agent.analysis.graph import analyze_artifact

        huge = "1" + "0" * 23                      # adjusted exponent 23 > 22
        stored = _graph_stored(payload=_dataset_payload(_row(paid_amount=huge)))
        store = _GraphStore(stored)
        model = RecordingModel()
        result = analyze_artifact(_analysis_request(), context=_context(store),
                                  model=model)
        self.assertEqual(result.status.value, "failed")
        self.assertEqual(result.model_payload["termination_reason"],
                         "contract_violation")
        self.assertEqual(result.artifacts, [])
        self.assertEqual(model.calls, [])
        nodes = [event["node"] for event in store.inner.events[result.run_id]]
        self.assertEqual(nodes[-1], "finalize")
        self.assertNotIn("summarize_findings", nodes,
                         "计算失败不得推进到模型节点")

    def test_source_read_outage_fails_closed_as_unavailable(self):
        """P2 回归：源库/读取故障走 FAILED/unavailable，不冒充稳定拒绝。

        真实 Store 的契约（runtime/repository）：资格失败报
        `ValueError(analysis_source_not_found)`，数据库失败报消毒后的
        `ArtifactPersistenceError`。两者必须分流：故障是 retryable 的
        unavailable（终止原因 upstream_unavailable），绝不是
        needs_input/invalid_parameters，也不调模型、不写结果、不动来源。
        """
        from bi_agent.analysis.graph import analyze_artifact
        from bi_agent.runtime.models import ArtifactPersistenceError

        stored = _graph_stored()
        source_snapshot = copy.deepcopy(stored.payload)
        cases = [
            ("元数据读取故障", RuntimeError("simulated connection loss"), 0),
            ("loader 读取故障", ArtifactPersistenceError(), 1),
        ]
        for label, error, fail_after in cases:
            with self.subTest(case=label):
                store = _GraphStore(stored, fail_reads_after=fail_after,
                                    read_error=error)
                model = RecordingModel()
                result = analyze_artifact(_analysis_request(),
                                          context=_context(store), model=model)
                self.assertEqual(result.status.value, "failed",
                                 "基础设施故障不是 needs_input")
                self.assertEqual(result.model_payload["termination_reason"],
                                 "upstream_unavailable")
                self.assertEqual(result.model_payload["status"], "unavailable")
                self.assertNotIn("findings", result.model_payload)
                self.assertEqual(result.error.code, "unavailable")
                self.assertTrue(result.error.retryable,
                                "源读取故障可重试，与资格拒绝不同类")
                self.assertEqual(model.calls, [], "故障路径不调模型")
                self.assertEqual(store.saves, 0, "故障路径不写 analysis_result")
                self.assertEqual(result.artifacts, [])
                self.assertEqual(store.inner.artifacts, {}, "不落任何 Artifact")
                self.assertEqual(stored.payload, source_snapshot,
                                 "来源快照一个字节不动")
                evidence = json.dumps(result.model_payload) + json.dumps(
                    store.inner.events[result.run_id], default=str) + json.dumps(
                    store.inner.runs[result.run_id]["state"], default=str)
                self.assertNotIn(str(error), evidence, "异常原文不进公开记录")
                run = store.inner.runs[result.run_id]
                self.assertEqual(run["status"], "failed")
                self.assertEqual(run["termination_reason"], "upstream_unavailable")
                self.assertEqual(run["error_code"], "unavailable")

    def test_state_and_events_never_carry_raw_ids_or_source_rows(self):
        from bi_agent.analysis.graph import analyze_artifact

        store = _GraphStore(_graph_stored())
        result = analyze_artifact(_analysis_request(), context=_context(store),
                                  model=None)
        evidence = json.dumps(store.inner.events[result.run_id], default=str) \
            + json.dumps(store.inner.runs[result.run_id]["state"], default=str)
        self.assertNotIn("S1", evidence, "ERP 主键不进运行记录")
        self.assertNotIn("paid_amount", evidence,
                         "来源行不进运行记录：分析证据只在 Artifact 里")


class AnalysisImportBoundaryTests(unittest.TestCase):
    """计划 Task 4 Step 4：AST import 白名单 + 钉死的模型调用形状。"""

    @staticmethod
    def _source(name):
        import bi_agent.analysis.calculations as calculations_module

        base = pathlib.Path(calculations_module.__file__).parent
        return (base / name).read_text(encoding="utf-8")

    def test_isolated_modules_import_only_approved_surface(self):
        from bi_agent.analysis.import_guard import import_violations

        for name in ("calculations.py", "summarizer.py"):
            with self.subTest(module=name):
                self.assertEqual(import_violations(self._source(name)), ())

    def test_guard_rejects_every_planned_forbidden_module(self):
        from bi_agent.analysis.import_guard import import_violations

        for module in ("os", "pathlib", "subprocess", "socket", "httpx",
                       "requests", "psycopg"):
            source = f"import {module}\nfrom {module} import connect\n"
            with self.subTest(module=module):
                self.assertTrue(import_violations(source))

    def test_dynamic_import_channels_are_rejected(self):
        """P1 回归：__import__ 不经过 import 语句，单扫 import 节点会静默漏放。"""
        from bi_agent.analysis.import_guard import import_violations

        for source in ('handle = __import__("psycopg").connect\n',
                       'module = __import__("os")\n',
                       'value = eval("1+1")\n',
                       'exec("import os")\n'):
            with self.subTest(source=source):
                self.assertTrue(import_violations(source))
        # 属性形式的 re.compile 不是裸名动态调用，不得误报。
        self.assertEqual(
            import_violations("import re\npattern = re.compile('a')\n"), ())

    def test_guard_rejects_repository_tool_and_sync_channels(self):
        from bi_agent.analysis.import_guard import import_violations

        for module in ("bi_agent.runtime.repository", "bi_agent.analysis.tool",
                       "bi_agent.commerce.sync", "bi_agent.agent",
                       "bi_agent.analysis.loader"):
            with self.subTest(module=module):
                self.assertTrue(import_violations(f"import {module}\n"))

    def test_absolute_internal_import_returns_a_verdict_not_a_crash(self):
        """P2 回归：`import bi_agent.analysis.models` 必须给出判定而非 TypeError。"""
        from bi_agent.analysis.import_guard import import_violations

        self.assertEqual(
            import_violations("import bi_agent.analysis.models\n"), ())
        self.assertEqual(
            import_violations("import bi_agent.analysis.models as models\n"), ())
        self.assertEqual(import_violations(
            "from bi_agent.analysis.models import Finding\n"), ())
        # 禁用对照：同一模块的 star 导入仍被点名。
        self.assertEqual(import_violations(
            "from bi_agent.analysis.models import *\n"),
            ("import_banned_star:bi_agent.analysis.models",))

    def test_chat_model_type_surface_is_the_only_bi_agent_import(self):
        from bi_agent.analysis.import_guard import import_violations

        self.assertEqual(import_violations(
            "from bi_agent.llm import ChatModel, Message\n"), ())
        self.assertEqual(import_violations(
            "from bi_agent.llm import ChatModel, Message, ModelReply, "
            "ModelError\n"), ())
        self.assertTrue(import_violations(
            "from bi_agent.llm import CompatibleChatModel\n"))
        self.assertTrue(import_violations("import bi_agent.llm\n"))

    def test_relative_imports_are_limited_to_models_and_calculations(self):
        from bi_agent.analysis.import_guard import import_violations

        self.assertEqual(import_violations("from .models import Finding\n"), ())
        self.assertEqual(import_violations(
            "from .calculations import compute_findings\n"), ())
        for source in ("from .loader import load_analysis_dataset\n",
                       "from . import loader\n",
                       "from ..runtime.models import ArtifactRef\n",
                       "from .models import *\n"):
            with self.subTest(source=source):
                self.assertTrue(import_violations(source))

    def test_syntax_errors_fail_closed(self):
        from bi_agent.analysis.import_guard import import_violations

        self.assertEqual(import_violations("def broken(:\n"), ("parse_error",))

    def test_summarizer_pins_the_exact_model_call_shape(self):
        from bi_agent.analysis.import_guard import model_call_violations

        self.assertEqual(model_call_violations(self._source("summarizer.py")), ())
        self.assertEqual(
            model_call_violations(self._source("calculations.py"),
                                  expected_calls=0), ())
        header = ("def fake(messages, tools, timeout_s, model):\n"
                  "    reply = ")
        for call in ("model.complete(messages, tools, timeout_s=timeout_s)",
                     "model.complete(messages, [], timeout)",
                     "model.complete(messages, [{}], timeout_s=timeout_s)",
                     "model.complete(payload, [], timeout_s=timeout_s)",
                     "model.complete(messages, [], **kwargs)",
                     "model.complete(messages, [], timeout_s=remaining_time)"):
            with self.subTest(call=call):
                self.assertTrue(model_call_violations(f"{header}{call}\n"))


if __name__ == "__main__":
    unittest.main()
