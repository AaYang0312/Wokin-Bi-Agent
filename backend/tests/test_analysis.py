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
"""

import unittest
from decimal import Decimal

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

    def test_package_ships_only_the_contract_slice(self):
        import pathlib

        import bi_agent.analysis as analysis

        shipped = sorted(path.name for path in
                         pathlib.Path(analysis.__file__).parent.glob("*.py"))
        self.assertEqual(shipped, ["__init__.py", "models.py"],
                         "Task 1 只交付契约切片；loader/graph 等按各自 Task 登记")


if __name__ == "__main__":
    unittest.main()
