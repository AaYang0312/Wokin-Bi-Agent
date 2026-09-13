import unittest
from datetime import datetime
from uuid import uuid4
from zoneinfo import ZoneInfo

BEIJING = ZoneInfo("Asia/Shanghai")

from pydantic import ValidationError

from .fakeconn import price_audit_payload
from bi_agent.runtime.models import (
    ArtifactRef,
    ArtifactPersistenceError,
    DomainArtifact,
    DomainResult,
    DomainStatus,
    ErrorEnvelope,
    NewArtifact,
    NewQueryRun,
    RecoveryAction,
    RunCompletion,
    RunNotFound,
    RunStatus,
    RunTransition,
    SchemaOutdated,
    StaleRunRevision,
)
from bi_agent.catalog import ref_for_key
from bi_agent.runtime.memory import MemoryQueryRunStore

# 稳定引用由 (kind, ERP主键) 纯派生；测试用同源常量，不手抄哈希。
S1_REF = ref_for_key("shop", "S1")
S2_REF = ref_for_key("shop", "S2")


class RuntimeModelTests(unittest.TestCase):
    def test_error_envelope_forbids_extra_fields(self):
        with self.assertRaises(ValidationError):
            ErrorEnvelope(
                code="invalid_parameters",
                stage="validate_parameters",
                retryable=False,
                recovery=RecoveryAction.CORRECT_PARAMETERS,
                public_message="查询参数无效",
                secret="database detail",
            )

    def test_domain_result_keeps_ref_and_public_projection(self):
        artifact_id = uuid4()
        result = DomainResult(
            run_id=uuid4(),
            status=DomainStatus.SUCCESS,
            model_payload={"status": "ok", "data": [{"shop_ref": S1_REF}]},
            artifacts=[DomainArtifact(
                ref=ArtifactRef(id=artifact_id, type="metric_result"),
                public_payload={
                    "status": "ok", "data": [{"shop_ref": S1_REF}],
                    "entities": [{"ref": S1_REF, "kind": "shop",
                               "display_name": "元发钉枪(抖音)", "name_source": "shop_profile"}],
                    "catalog_version": 3,
                },
            )],
        )
        self.assertEqual(result.artifacts[0].ref.id, artifact_id)
        self.assertEqual(result.artifacts[0].public_payload["data"][0]["shop_ref"], S1_REF)
        self.assertEqual(result.artifacts[0].public_payload["entities"][0]["display_name"],
                       "元发钉枪(抖音)")
        self.assertNotIn("entities", result.model_payload)

    def test_error_envelope_rejects_diagnostic_and_secret_text(self):
        for field, value in (
            ("public_message", "psycopg.errors.UndefinedTable: relation missing"),
            ("problems", ["password=supersecret"]),
        ):
            values = {
                "code": "unavailable",
                "stage": "execute_fixed_query",
                "retryable": True,
                "recovery": RecoveryAction.RETRY_LATER,
                "public_message": "查询暂不可用",
                "problems": [],
                field: value,
            }
            with self.subTest(field=field), self.assertRaises(ValidationError) as context:
                ErrorEnvelope(**values)
            self.assertNotIn(str(value), str(context.exception))

    def test_persistence_command_models_reject_sensitive_payloads(self):
        raw_question = "请查询真实店铺 S1 的销售额"
        hidden_reasoning = "模型隐藏推理：先尝试绕过权限"
        database_error = "psycopg.errors.UndefinedTable: relation bi.secret does not exist"
        credentials = "postgresql://app:supersecret@db.example/bi"
        cases = (
            (raw_question, lambda: NewQueryRun(
                chat_id=uuid4(), user_message_id=uuid4(), subject_id="u1",
                tool_call_id="call_1", attempt_no=1,
                normalized_request={"raw_question": raw_question},
            )),
            (hidden_reasoning, lambda: RunTransition(
                expected_revision=0, node="resolve_parameters", status=RunStatus.RUNNING,
                state={"node": "resolve_parameters"},
                payload={"hidden_reasoning": hidden_reasoning},
            )),
            (database_error, lambda: NewArtifact(
                payload={"status_detail": database_error},
            )),
            (credentials, lambda: RunCompletion(
                expected_revision=0, node="finalize", status=RunStatus.FAILED,
                state={"status_detail": credentials},
            )),
        )
        for sensitive_value, command in cases:
            with self.subTest(sensitive_value=sensitive_value), self.assertRaises(ValidationError) as context:
                command()
            self.assertNotIn(sensitive_value, str(context.exception))

    def test_artifact_persistence_error_accepts_reason_without_exposing_it(self):
        self.assertEqual(
            str(ArtifactPersistenceError("artifact_persistence_failed")),
            "artifact_persistence_error",
        )
        self.assertEqual(
            str(ArtifactPersistenceError("password=supersecret")),
            "artifact_persistence_error",
        )

    def test_allowlisted_models_reject_exact_persistence_bypasses(self):
        raw_question = "请查询店铺 S1 的销售额"
        opaque_credential = "sk_live_51OpaqueCredentialValue"
        generic_database_excerpt = "database engine returned status 42"
        cases = (
            (raw_question, lambda: NewQueryRun(
                chat_id=uuid4(), user_message_id=uuid4(), subject_id="u1",
                tool_call_id="call_1", attempt_no=1,
                state={"message": raw_question},
            )),
            ("reasoning_content", lambda: RunTransition(
                expected_revision=0, node="resolve_parameters", status=RunStatus.RUNNING,
                state={"node": "resolve_parameters", "revision": 1},
                payload={"reasoning_content": "opaque"},
            )),
            ("S1", lambda: NewArtifact(
                payload={"status": "ok", "data": [{"shop_id": "S1"}]},
            )),
            ("ERP-P-9", lambda: NewArtifact(
                payload={"status": "ok", "data": [{"product_id": "ERP-P-9"}]},
            )),
            (opaque_credential, lambda: NewArtifact(
                payload={"status": "ok", "data": [{"paid_amount": opaque_credential}]},
            )),
            (generic_database_excerpt, lambda: ErrorEnvelope(
                code="unavailable", stage="execute_fixed_query", retryable=True,
                recovery=RecoveryAction.RETRY_LATER,
                public_message=generic_database_excerpt,
            )),
        )
        for unsafe_value, command in cases:
            with self.subTest(unsafe_value=unsafe_value), self.assertRaises(ValidationError) as context:
                command()
            self.assertNotIn(unsafe_value, str(context.exception))

    def test_transition_rejects_normalized_request_that_diverges_from_state(self):
        with self.assertRaisesRegex(ValidationError, "normalized_request_mismatch"):
            RunTransition(
                expected_revision=0,
                node="validate_parameters",
                status=RunStatus.RUNNING,
                normalized_request={"shop_refs": [S2_REF]},
                state={
                    "node": "validate_parameters",
                    "revision": 1,
                    "normalized_request": {"shop_refs": [S1_REF]},
                },
            )

    def test_allowlisted_future_state_event_and_artifact_shapes_are_valid(self):
        normalized_request = {
            "shop_refs": [S1_REF],
            "metrics": ["paid_amount"],
            "start": "2026-09-01",
            "end": "2026-09-08",
            "group_by": "shop",
            "compare": "none",
            "top_n": 10,
            "currency": "CNY",
        }
        coverage = {
            "status": "complete",
            "start": "2026-09-01",
            "end": "2026-09-08",
            "gaps": [],
        }
        state = {
            "node": "resolve_parameters",
            "status": "running",
            "revision": 1,
            "normalized_request": normalized_request,
            "problems": [],
            "coverage": coverage,
            "data_as_of": "2026-09-08T09:00:00+08:00",
            "limitations": ["coverage_incomplete"],
            "artifact_refs": [],
        }
        event_payload = {
            "problem_codes": ["invalid_parameters"],
            "coverage_status": "complete",
            "data_as_of": "2026-09-08T09:00:00+08:00",
            "limitation_codes": ["coverage_incomplete"],
            "result_count": 1,
        }
        public_artifact = {
            "status": "ok",
            "metric_definition": {
                "paid_amount": "已验证商业订单支付金额之和（人民币，按支付时间归属，[start,end)）",
            },
            "coverage": coverage,
            "limitations": ["同批支付额为0或无支付，同批退款率不可计算"],
            "data_as_of": "2026-09-08T09:00:00+08:00",
            "filters": {
                "start": "2026-09-01",
                "end": "2026-09-08",
                "shop_refs": [S1_REF],
                "metrics": ["paid_amount"],
                "group_by": "shop",
                "compare": "none",
                "currency": "CNY",
            },
            "data": [{"shop_ref": S1_REF, "paid_amount": "1000"}],
        }
        record = NewQueryRun(
            chat_id=uuid4(), user_message_id=uuid4(), subject_id="u1",
            tool_call_id="call_1", attempt_no=1,
            normalized_request=normalized_request,
            state={"node": "received", "status": "running", "revision": 0},
        )
        transition = RunTransition(
            expected_revision=0, node="resolve_parameters", status=RunStatus.RUNNING,
            state=state, payload=event_payload,
        )
        artifact = NewArtifact(payload=public_artifact, coverage=coverage)
        completion = RunCompletion(
            expected_revision=1, node="finalize", status=RunStatus.SUCCEEDED,
            state={**state, "node": "finalize", "status": "succeeded", "revision": 2},
            payload={"result_count": 1},
        )
        store = MemoryQueryRunStore(forbidden_values={"S1", "ERP-P-9"})
        run_id = store.create_run(record)
        store.transition(run_id, transition)
        self.assertEqual(store.save_artifact(run_id, artifact).type, "metric_result")
        store.finish(run_id, completion)
        self.assertEqual(store.runs[run_id]["status"], "succeeded")


class AttributionDisclosureContractTests(unittest.TestCase):
    """归属披露是参数化文本：形状要校验，不能给注入留口。"""

    TEXT = ("支付额中3130.51元未计入商品维度"
            "（关闭订单行3130.51元；赠品行0元；无商品归属0元；其他0元）")

    def _payload(self, limitation):
        return {
            "status": "ok", "metric_definition": {},
            "coverage": {"status": "complete", "start": "2026-09-01",
                         "end": "2026-09-10", "gaps": []},
            "limitations": [limitation], "data_as_of": None,
            "filters": {}, "data": [],
        }

    def test_well_formed_disclosure_is_accepted(self):
        from bi_agent.runtime.models import validate_artifact_payload, validate_model_payload

        validate_artifact_payload(self._payload(self.TEXT))
        validate_model_payload(self._payload(self.TEXT))

    def test_disclosure_carrying_identifiers_or_free_text_is_rejected(self):
        """披露只能是一串金额，不得夹入店铺主键或自行改写的成因。"""
        from bi_agent.runtime.models import validate_artifact_payload

        for bad in (
            "支付额中3130.51元未计入商品维度（关闭订单行3130.51元；赠品行0元；"
            "无商品归属0元；其他0元；店铺166754）",
            "支付额中abc元未计入商品维度（关闭订单行0元；赠品行0元；无商品归属0元；其他0元）",
            "支付额中3130.51元未计入商品维度",
            "支付额中99元未计入商品维度（赠品99元）",
        ):
            with self.subTest(bad=bad[:24]):
                with self.assertRaises(ValueError):
                    validate_artifact_payload(self._payload(bad))

    def test_graph_maps_disclosure_to_a_stable_code(self):
        """归因文本要能进结构化事件，不能只存一句人话。"""
        from bi_agent.business_query.nodes import _limitation_codes

        self.assertEqual(_limitation_codes([self.TEXT]), ["revenue_not_attributed"])


    def test_entity_platform_code_is_validated(self):
        """旧 Artifact 无 platform 仍可读；新字段只接受短码。"""
        from bi_agent.runtime.models import validate_artifact_payload

        base = {"ref": S1_REF, "kind": "shop", "display_name": "元发钉枪",
                "name_source": "shop_profile"}
        validate_artifact_payload(self._payload_with_entities([dict(base)]))
        validate_artifact_payload(self._payload_with_entities([dict(base, platform="fxg")]))
        for bad in ("FXG 抖音", "fxg;drop", "", "x" * 20):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    validate_artifact_payload(self._payload_with_entities(
                        [dict(base, platform=bad)]))

    def _payload_with_entities(self, entities):
        return {
            "status": "ok", "metric_definition": {},
            "coverage": {"status": "complete", "start": "2026-09-01",
                         "end": "2026-09-08", "gaps": []},
            "limitations": [], "data_as_of": None, "filters": {}, "data": [],
            "entities": entities, "catalog_version": 0,
        }


    def test_unonboarded_source_disclosure_is_public_and_mapped(self):
        """“来源未开通”得能进载荷并归因，否则与覆盖缺口无法区分。"""
        from bi_agent.business_query.nodes import _limitation_codes
        from bi_agent.runtime.models import validate_artifact_payload

        text = "2 家店铺的来源尚未开通（未授权或未同步），缩小日期范围不会补上这段数据"
        payload = self._payload(text, status="missing_data")
        validate_artifact_payload(payload)
        self.assertEqual(_limitation_codes([text]), ["source_not_onboarded"])
        for bad in (" 家店铺的来源尚未开通（未授权或未同步），缩小日期范围不会补上这段数据",
                    "2 家店铺的来源尚未开通，随便加点什么"):
            with self.subTest(bad=bad[:12]):
                with self.assertRaises(ValueError):
                    validate_artifact_payload(self._payload(bad, status="missing_data"))

    def _payload(self, limitation, status="unavailable"):
        return {
            "status": status, "metric_definition": {},
            "coverage": {"status": "complete", "start": "2026-09-01",
                         "end": "2026-09-10", "gaps": []},
            "limitations": [limitation], "data_as_of": None,
            "filters": {}, "data": [],
        }


class TruncationLimitationContractTests(unittest.TestCase):
    """行数上限是合法的范围提示，不是契约违规。

    真实库回归发现：90 天商品分组查询命中 MAX_ROWS 后，
    「结果行数达到N上限…」这句不在公开词表里，载荷校验抛
    unsafe_persistence_payload，图把合法降级误报成 result_contract_violation。
    """

    TEXT = "结果行数达到1000上限，已拒绝出数以避免静默截断；请缩小日期范围或店铺范围"

    def _payload(self, limitation, status="unavailable"):
        return {
            "status": status, "metric_definition": {},
            "coverage": {"status": "complete", "start": "2026-06-13",
                         "end": "2026-09-11", "gaps": []},
            "limitations": [limitation], "data_as_of": None,
            "filters": {}, "data": [],
        }

    def test_row_cap_disclosure_is_a_public_limitation(self):
        from bi_agent.runtime.models import validate_artifact_payload, validate_model_payload

        validate_model_payload(self._payload(self.TEXT))
        validate_artifact_payload(self._payload(self.TEXT))

    def test_row_cap_disclosure_maps_to_result_too_large(self):
        from bi_agent.business_query.nodes import _limitation_codes

        self.assertEqual(_limitation_codes([self.TEXT]), ["result_too_large"])

    def test_row_cap_text_cannot_be_padded_with_identifiers(self):
        from bi_agent.runtime.models import validate_artifact_payload

        with self.assertRaises(ValueError):
            validate_artifact_payload(self._payload(self.TEXT + " 店铺166754"))


class CoverageContractTests(unittest.TestCase):
    """覆盖契约加 suggested_window 时，旧 Artifact 必须继续可读。"""

    def _payload(self, coverage):
        return {
            "status": "ok",
            "metric_definition": {},
            "coverage": coverage,
            "limitations": [],
            "data_as_of": None,
            "filters": {},
            "data": [],
        }

    def test_legacy_coverage_without_suggested_window_still_validates(self):
        from bi_agent.runtime.models import validate_artifact_payload

        validate_artifact_payload(self._payload(
            {"status": "partial", "start": "2026-09-01", "end": "2026-09-08",
             "gaps": ["2026-09-05~2026-09-08"]}))

    def test_suggested_window_is_accepted_as_two_dates(self):
        from bi_agent.runtime.models import validate_artifact_payload

        validate_artifact_payload(self._payload(
            {"status": "partial", "start": "2026-09-01", "end": "2026-09-08",
             "gaps": ["2026-09-05~2026-09-08"],
             "suggested_window": ["2026-09-01", "2026-09-05"]}))

    def test_suggested_window_rejects_a_rewritten_request_range(self):
        """建议只能是两个日期，不能塞进被改写过的窗口或主键。"""
        from bi_agent.runtime.models import validate_artifact_payload

        for bad in (["2026-09-01"], ["2026-09-01", "S1"], ["2026-09-01", "not-a-date"],
                    "2026-09-01"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    validate_artifact_payload(self._payload(
                        {"status": "partial", "start": "2026-09-01", "end": "2026-09-08",
                         "gaps": [], "suggested_window": bad}))


class ProvenanceContractTests(unittest.TestCase):
    """Task 3：数据版本与请求身份必须可枚举、可复现、不可越权命中。"""

    def _provenance(self, **overrides):
        from bi_agent.runtime.artifacts import QueryProvenance

        return QueryProvenance(**overrides)

    def _fingerprint(self, *, shops=("S1",), provenance=None, request=None):
        from bi_agent.runtime.artifacts import request_fingerprint

        return request_fingerprint(
            subject_id="subject-a", allowed_shop_ids=frozenset(shops),
            normalized_request=(request if request is not None
                                else {"start": "2026-09-01", "metrics": ["paid_amount"]}),
            provenance=provenance or self._provenance())

    def test_same_request_same_fingerprint(self):
        self.assertEqual(self._fingerprint(), self._fingerprint())

    def test_data_version_change_changes_the_fingerprint(self):
        """回填推进 catalog_version / 截止时刻后，旧结果不允许再命中。

        这正是 revision 不能兼任数据版本的原因：状态推进号不变而数据已经变了。
        """
        base = self._fingerprint()
        changed_catalog = self._fingerprint(provenance=self._provenance(catalog_version=3))
        changed_cutoff = self._fingerprint(provenance=self._provenance(
            data_as_of=datetime(2026, 9, 9, tzinfo=BEIJING)))
        changed_batches = self._fingerprint(provenance=self._provenance(
            source_batches=("batch-1",)))

        self.assertNotEqual(base, changed_catalog)
        self.assertNotEqual(base, changed_cutoff)
        self.assertNotEqual(base, changed_batches)

    def test_authorization_scope_changes_the_fingerprint(self):
        """同一条查询换一个授权范围就不是同一请求，命中即越权。"""
        self.assertNotEqual(self._fingerprint(shops=("S1",)),
                            self._fingerprint(shops=("S1", "S2")))

    def test_basis_change_invalidates_previous_results(self):
        """同店换来源（口径或时间归属变了）不许命中旧结果。

        否则一次通道切换会被下游读成经营增长：数字差异其实来自口径差异。
        """
        paid = self._provenance(basis_signature=("paid_amount|platform_payment/v1|pay_time",))
        outstock = self._provenance(
            basis_signature=("paid_amount|erp_outstock_payment/v1|outstock_time",))

        self.assertNotEqual(self._fingerprint(provenance=paid),
                            self._fingerprint(provenance=outstock))

    def test_source_registry_and_quality_rule_versions_are_fingerprint_material(self):
        base = self._provenance()
        self.assertNotEqual(self._fingerprint(provenance=base), self._fingerprint(
            provenance=self._provenance(source_registry_version="sources/2026-01-01.1")))
        self.assertNotEqual(self._fingerprint(provenance=base), self._fingerprint(
            provenance=self._provenance(quality_rule="kuaimai-reconcile/1")))

    def test_basis_signature_rejects_identifiers_and_free_text(self):
        for bad in ("166754|platform_payment/v1|pay_time",
                    "paid_amount|erp.trade.list.query|pay_time",
                    "口径变了"):
            with self.subTest(bad=bad[:18]):
                with self.assertRaises(ValidationError):
                    self._provenance(basis_signature=(bad,))

    def test_basis_signature_drops_shop_dimension(self):
        """签名去重后只看 (指标, 口径, 时间归属)：多店同口径不该膨胀成多条。"""
        from bi_agent.runtime.artifacts import basis_signature_of

        entries = [
            {"shop_id": "S1", "metric": "paid_amount", "basis": "platform_payment/v1",
             "time_basis": "pay_time"},
            {"shop_id": "TB1", "metric": "paid_amount", "basis": "platform_payment/v1",
             "time_basis": "pay_time"},
        ]
        self.assertEqual(basis_signature_of(entries),
                         ("paid_amount|platform_payment/v1|pay_time",))

    def test_metric_version_change_invalidates_previous_results(self):
        from bi_agent.runtime.artifacts import METRIC_VERSION, QueryProvenance

        self.assertNotEqual(self._fingerprint(), self._fingerprint(
            provenance=QueryProvenance(metric_version="metrics/older")))
        self.assertIsInstance(METRIC_VERSION, str)

    def test_provenance_rejects_free_text_and_unknown_fields(self):
        from pydantic import ValidationError

        from bi_agent.runtime.artifacts import QueryProvenance

        for bad in ({"metric_version": "metrics/1; DROP TABLE"},
                   {"catalog_version": -1},
                   {"source_batches": (" batch-padded ",)},
                   {"sql_text": "SELECT 1"}):
            with self.subTest(keys=tuple(bad)):
                with self.assertRaises(ValidationError):
                    QueryProvenance(**bad)

    def test_identity_rejects_arbitrary_termination_text(self):
        from pydantic import ValidationError

        from bi_agent.runtime.artifacts import RequestIdentity
        from uuid import uuid4

        for bad in ("上游返回了很奇怪的一句话", "coverage_incomplete ", ""):
            with self.subTest(value=bad[:12]):
                with self.assertRaises(ValidationError):
                    RequestIdentity(root_request_id=uuid4(),
                                    request_fingerprint="0" * 64, attempt_no=1,
                                    termination_reason=bad)

    def test_domain_registry_refuses_unknown_domain_and_type(self):
        from bi_agent.runtime.domain_registry import (
            DomainUnknown, allows_artifact_type, spec_for)

        self.assertTrue(allows_artifact_type("business_query", "metric_result"))
        self.assertFalse(allows_artifact_type("business_query", "chart_spec"),
                         "未登记给 business_query 的类型不能借既有领域写入")
        self.assertFalse(allows_artifact_type("nope", "metric_result"))
        with self.assertRaises(DomainUnknown):
            spec_for("nope")

    def test_chart_spec_must_point_at_a_dataset_version(self):
        from pydantic import ValidationError

        from bi_agent.runtime.artifacts import ArtifactEnvelope
        from uuid import uuid4

        chart = {"domain": "commerce_performance", "payload": {"kind": "bar"},
                 "artifact_type": "chart_spec"}
        with self.assertRaises(ValidationError):
            ArtifactEnvelope(**chart)                        # 缺数据集引用
        ArtifactEnvelope(**chart, dataset_ref=uuid4(), chart_version=1)
        with self.assertRaises(ValidationError):
            # 非图表类型不许带数据集引用；business_query 也不许产出图表。
            ArtifactEnvelope(domain="commerce_performance", payload={},
                             artifact_type="metric_result",
                             dataset_ref=uuid4(), chart_version=1)
        with self.assertRaises(ValidationError):
            ArtifactEnvelope(domain="business_query", payload={},
                             artifact_type="chart_spec",
                             dataset_ref=uuid4(), chart_version=1)

    def test_termination_reason_vocabulary_matches_database(self):
        """SQL CHECK 与 Python 码表同源：两边各写一份必然漂移。

        009 已经应用过，不得改写：后续新增原因码只能由新迁移重新声明完整 CHECK，
        所以码表比对的是 009+014 两段清单的并集，不是单一文件。
        """
        import pathlib
        import re

        from bi_agent.runtime.artifacts import TERMINATION_REASONS

        sql_dir = pathlib.Path(__file__).parents[1] / "sql"
        # CHECK 是整段重新声明的（不能只追加），所以最新那份必须逐字等于码表；
        # 只比对 009∪014 的并集会漏掉“新版本删了某个码而库里还留着”。
        for name in ("009_query_provenance.sql", "014_multi_source_contract.sql"):
            sql = (sql_dir / name).read_text(encoding="utf-8")
            block = sql.split("query_runs_termination_reason CHECK", 1)[1].split(");", 1)[0]
            in_sql = set(re.findall(r"'([a-z_]+)'", block))
            if name.startswith("014"):
                self.assertEqual(in_sql, set(TERMINATION_REASONS),
                                 f"{name} 的 CHECK 必须与终止原因码表逐项一致")
            self.assertTrue(in_sql <= set(TERMINATION_REASONS),
                            f"{name} 含码表之外的原因码")


class _PreflightConn:
    """只回答 pg_constraint 预检的连接：任何业务写入都不该发生。"""

    def __init__(self, definition: str | None):
        self.definition = definition
        self.sql: list[str] = []

    def execute(self, sql, parameters=()):
        self.sql.append(sql)

        class _Result:
            def __init__(self, row):
                self._row = row

            def fetchone(self):
                return self._row

        return _Result((self.definition,) if "pg_constraint" in sql else None)


class StoreSchemaPreflightTests(unittest.TestCase):
    """库里 CHECK 不认识本进程的码表时必须早失败，而不是收尾撞出一个看不出原因的失败。"""

    def _record(self):
        return NewQueryRun(
            chat_id=uuid4(), user_message_id=uuid4(), subject_id="u1",
            tool_call_id="call_1", attempt_no=1,
            normalized_request={"shop_refs": [S1_REF]},
            state={"node": "received"},
        )

    def test_legacy_009_only_schema_is_reported_before_any_write(self):
        from bi_agent.runtime.repository import PostgresQueryRunStore

        legacy = ("CHECK (((termination_reason IS NULL) "
                  "OR (termination_reason = ANY (ARRAY['succeeded'::text, "
                  "'coverage_incomplete'::text]))))")
        store = PostgresQueryRunStore(_PreflightConn(legacy),
                                      forbidden_values={"S1"})
        with self.assertRaises(SchemaOutdated):
            store.create_run(self._record())
        self.assertEqual(len(store.conn.sql), 1, "预检必须走在任何业务写入之前")

    def test_current_schema_passes_and_is_checked_once_per_store(self):
        from bi_agent.runtime.artifacts import TERMINATION_REASONS
        from bi_agent.runtime.repository import PostgresQueryRunStore

        current = "CHECK (termination_reason IS NULL OR termination_reason IN (" + \
            ", ".join(f"'{code}'" for code in sorted(TERMINATION_REASONS)) + "))"
        conn = _PreflightConn(current)
        store = PostgresQueryRunStore(conn, forbidden_values={"S1"})

        for _ in range(2):
            # 预检过了以后会走到真实 INSERT；替身返回空行，说明已经越过门禁。
            with self.assertRaises(Exception) as caught:
                store.create_run(self._record())
            self.assertNotIsInstance(caught.exception, SchemaOutdated)
        self.assertEqual(len([item for item in conn.sql if "pg_constraint" in item]), 1,
                         "每个 Store 实例最多预检一次")


class QuantifiedLimitationContractTests(unittest.TestCase):
    """设计 §5 的可量化限制：披露文本要能通过公开校验，并映射到稳定码。

    没登记词表的披露会在保存 Artifact 时被契约校验拒掉，把一次正常查询变成
    result_contract_violation；只看数据库用例看不出来，所以三层一起钉。
    """

    CODES = {
        "退款归属未确认：未匹配1条/共4条，金额25元，比例25%": "unmatched_refunds",
        "退款归属未确认：未匹配1条/共0条，金额25元，比例未知": "unmatched_refunds",
        "退款归属未确认：未匹配1条/共4条，金额24.5元，比例25.5%": "unmatched_refunds",
        "同批退款率仅含已匹配退款（1条未匹配退款无法归属，未计入）": "matched_cohort_only",
        "未认证支付3笔（金额未定2笔），已知原始金额89669.39元（1笔）": "unverified_payments",
    }

    def _payload(self, limitation):
        return {
            "status": "ok", "metric_definition": {},
            "coverage": {"status": "complete", "start": "2026-09-01",
                         "end": "2026-09-08", "gaps": []},
            "limitations": [limitation], "data_as_of": None,
            "filters": {}, "data": [],
        }

    def test_each_disclosure_is_accepted_and_carries_a_stable_code(self):
        from bi_agent.business_query.nodes import _limitation_codes
        from bi_agent.runtime.models import (
            validate_artifact_payload, validate_model_payload)

        for text, code in self.CODES.items():
            with self.subTest(text=text[:20]):
                validate_artifact_payload(self._payload(text))
                validate_model_payload(self._payload(text))
                self.assertEqual(_limitation_codes([text]), [code])

    def test_invented_or_leaking_variants_are_rejected(self):
        from bi_agent.runtime.models import validate_artifact_payload

        for bad in (
            "退款归属未确认：未匹配1条/共4条，金额25元，比例25%（店铺166754）",
            "退款归属未确认：未匹配1条，比例25%",
            "未认证支付3笔（金额未定2笔），已知原始金额约9万元（1笔）",
            "同批退款率仅含已匹配退款",
        ):
            with self.subTest(bad=bad[:22]):
                with self.assertRaises(ValueError):
                    validate_artifact_payload(self._payload(bad))


class MemoryQueryRunStoreTests(unittest.TestCase):
    def setUp(self):
        self.store = MemoryQueryRunStore(forbidden_values={"S1", "ERP-P-9"})
        self.record = NewQueryRun(
            chat_id=uuid4(), user_message_id=uuid4(), subject_id="u1",
            tool_call_id="call_1", attempt_no=1,
            normalized_request={"shop_refs": [S1_REF]},
            state={"node": "received"},
        )

    def test_transition_is_revision_checked_and_appends_one_event(self):
        run_id = self.store.create_run(self.record)
        transition = RunTransition(
            expected_revision=0, node="resolve_parameters",
            status=RunStatus.RUNNING,
            state={"node": "resolve_parameters", "revision": 1},
        )
        self.store.transition(run_id, transition)
        self.assertEqual(self.store.runs[run_id]["revision"], 1)
        self.assertEqual(self.store.events[run_id][0]["revision"], 1)
        with self.assertRaises(StaleRunRevision):
            self.store.transition(run_id, transition)

    def test_transition_atomically_updates_only_the_validated_normalized_request(self):
        run_id = self.store.create_run(self.record)
        normalized_request = {
            "shop_refs": [S1_REF],
            "metrics": ["paid_amount"],
            "start": "2026-09-01",
            "end": "2026-09-08",
            "group_by": "total",
            "compare": "none",
            "top_n": 100,
            "currency": "CNY",
        }
        transition = RunTransition(
            expected_revision=0,
            node="validate_parameters",
            status=RunStatus.RUNNING,
            normalized_request=normalized_request,
            state={
                "node": "validate_parameters",
                "revision": 1,
                "normalized_request": normalized_request,
            },
        )

        self.store.transition(run_id, transition)

        self.assertEqual(self.store.runs[run_id]["normalized_request"], normalized_request)
        self.assertEqual(
            self.store.runs[run_id]["normalized_request"],
            self.store.runs[run_id]["state"]["normalized_request"],
        )

    def test_transition_derives_normalized_request_from_state_when_assertion_is_omitted(self):
        run_id = self.store.create_run(self.record)
        normalized_request = {"shop_refs": [S2_REF]}
        transition = RunTransition(
            expected_revision=0,
            node="resolve_parameters",
            status=RunStatus.RUNNING,
            state={
                "node": "resolve_parameters",
                "revision": 1,
                "normalized_request": normalized_request,
            },
        )

        self.store.transition(run_id, transition)

        self.assertEqual(self.store.runs[run_id]["normalized_request"], normalized_request)
        self.assertEqual(
            self.store.runs[run_id]["normalized_request"],
            self.store.runs[run_id]["state"]["normalized_request"],
        )

    def test_transition_rejects_real_identifiers_in_the_normalized_request(self):
        with self.assertRaisesRegex(ValidationError, "unsafe_persistence_payload"):
            RunTransition(
                expected_revision=0,
                node="resolve_parameters",
                status=RunStatus.RUNNING,
                normalized_request={"shop_refs": ["S1"]},
                state={
                    "node": "resolve_parameters",
                    "revision": 1,
                    "normalized_request": {"shop_refs": ["S1"]},
                },
            )

    def test_transition_rejects_mismatched_normalized_request_without_mutation(self):
        run_id = self.store.create_run(self.record)
        transition = RunTransition.model_construct(
            expected_revision=0,
            node="validate_parameters",
            event_type=RunTransition.model_fields["event_type"].default,
            status=RunStatus.RUNNING,
            normalized_request={"shop_refs": [S2_REF]},
            state={
                "node": "validate_parameters",
                "revision": 1,
                "normalized_request": {"shop_refs": [S1_REF]},
            },
        )

        with self.assertRaisesRegex(ValueError, "^normalized_request_mismatch$"):
            self.store.transition(run_id, transition)

        self.assertEqual(self.store.runs[run_id]["normalized_request"], {"shop_refs": [S1_REF]})
        self.assertEqual(self.store.runs[run_id]["state"], {"node": "received"})
        self.assertEqual(self.store.runs[run_id]["revision"], 0)
        self.assertEqual(self.store.events[run_id], [])

    def test_save_artifact_rejects_type_outside_the_run_domain(self):
        """领域能发哪种 Artifact 在写库前就拦：只校全局类型白名单等于允许
        business_query 往自己的运行下发价审载荷，而 009 的 CHECK 是全局的。
        """
        run_id = self.store.create_run(self.record)
        # 载荷本身是合法的价审载荷：这里要拦的是"business_query 不许发 price_audit"，
        # 不是载荷形状。两者混在一起的话，这条用例通过的理由就不是它声称的理由了。
        with self.assertRaisesRegex(ValueError, "^unsafe_persistence_payload$"):
            self.store.save_artifact(run_id, NewArtifact(
                artifact_type="price_audit", payload=price_audit_payload()))
        self.assertEqual(self.store.artifacts, {}, "拒绝不能先写一半")

    def test_save_artifact_returns_reference_and_finish_is_terminal(self):
        run_id = self.store.create_run(self.record)
        ref = self.store.save_artifact(run_id, NewArtifact(
            payload={"status": "ok", "data": []},
            coverage={
                "status": "complete",
                "start": "2026-09-01",
                "end": "2026-09-08",
                "gaps": [],
            },
        ))
        self.assertEqual(ref.type, "metric_result")
        self.store.finish(run_id, RunCompletion(
            expected_revision=0, node="finalize", status=RunStatus.SUCCEEDED,
            state={"node": "finalize", "revision": 1},
        ))
        self.assertEqual(self.store.runs[run_id]["status"], "succeeded")
        self.assertIsNotNone(self.store.runs[run_id]["completed_at"])

    def test_duplicate_run_context_is_rejected(self):
        self.store.create_run(self.record)
        with self.assertRaises(ValueError) as context:
            self.store.create_run(self.record)
        self.assertEqual(str(context.exception), "duplicate_query_run")

    def test_store_rejects_known_real_erp_identifiers_without_persisting_them(self):
        store = MemoryQueryRunStore(forbidden_values={"S1", "ERP-P-9"})
        run_id = store.create_run(self.record)
        transition = RunTransition.model_construct(
            expected_revision=0, node="resolve_parameters", status=RunStatus.RUNNING,
            event_type=RunTransition.model_fields["event_type"].default,
            state={"shop_id": "S1"}, payload={}, error_code=None,
        )
        with self.assertRaises(ValueError) as context:
            store.transition(run_id, transition)
        self.assertEqual(str(context.exception), "unsafe_persistence_payload")
        self.assertEqual(store.runs[run_id]["revision"], 0)
        self.assertEqual(store.events[run_id], [])
        with self.assertRaises(ValueError) as context:
            store.save_artifact(run_id, NewArtifact.model_construct(
                artifact_type="metric_result",
                payload={"status": "ok", "data": [{"product_id": "ERP-P-9"}]},
                data_as_of=None,
                coverage=None,
            ))
        self.assertEqual(str(context.exception), "unsafe_persistence_payload")
        self.assertEqual(store.artifacts, {})
        with self.assertRaises(ValueError) as context:
            store.save_artifact(run_id, NewArtifact.model_construct(
                artifact_type="metric_result",
                payload={"status": "ok", "data": [{"paid_amount": "sk_live_51Opaque"}]},
                data_as_of=None,
                coverage=None,
            ))
        self.assertEqual(str(context.exception), "unsafe_persistence_payload")
        self.assertEqual(store.artifacts, {})

    def test_store_requires_real_erp_identifiers(self):
        with self.assertRaises(TypeError):
            MemoryQueryRunStore()
        with self.assertRaises(ValueError) as context:
            MemoryQueryRunStore(forbidden_values=set())
        self.assertEqual(str(context.exception), "forbidden_values_required")

    def test_store_revalidates_constructed_event_payload(self):
        run_id = self.store.create_run(self.record)
        unsafe_transition = RunTransition.model_construct(
            expected_revision=0,
            node="resolve_parameters",
            event_type=RunTransition.model_fields["event_type"].default,
            status=RunStatus.RUNNING,
            state={"node": "resolve_parameters", "revision": 1},
            payload={"reasoning_content": "opaque"},
            error_code=None,
        )
        with self.assertRaises(ValueError) as context:
            self.store.transition(run_id, unsafe_transition)
        self.assertEqual(str(context.exception), "unsafe_persistence_payload")
        self.assertEqual(self.store.events[run_id], [])

    def test_store_revalidates_constructed_command_envelope(self):
        run_id = self.store.create_run(self.record)
        unsafe_completion = RunCompletion.model_construct(
            expected_revision=0,
            node="message",
            status=RunStatus.SUCCEEDED,
            state={"node": "finalize", "status": "succeeded", "revision": 1},
            payload={},
            error_code=None,
        )
        with self.assertRaises(ValueError) as context:
            self.store.finish(run_id, unsafe_completion)
        self.assertEqual(str(context.exception), "unsafe_persistence_payload")
        self.assertEqual(self.store.runs[run_id]["revision"], 0)

    def test_store_revalidates_constructed_state_and_error_content(self):
        unsafe_record = NewQueryRun.model_construct(
            chat_id=uuid4(),
            user_message_id=uuid4(),
            subject_id="u1",
            tool_call_id="call_1",
            domain="business_query",
            attempt_no=1,
            normalized_request={},
            state={"message": "请查询店铺 S1 的销售额"},
        )
        with self.assertRaises(ValueError) as context:
            self.store.create_run(unsafe_record)
        self.assertEqual(str(context.exception), "unsafe_persistence_payload")
        self.assertEqual(self.store.runs, {})

        run_id = self.store.create_run(self.record)
        unsafe_error = ErrorEnvelope.model_construct(
            code="unavailable",
            stage="execute_fixed_query",
            retryable=True,
            recovery=RecoveryAction.RETRY_LATER,
            public_message="database engine returned status 42",
            problems=[],
        )
        unsafe_completion = RunCompletion.model_construct(
            expected_revision=0,
            node="finalize",
            status=RunStatus.FAILED,
            state={
                "node": "finalize",
                "status": "failed",
                "revision": 1,
                "error": unsafe_error.model_dump(),
            },
            payload={},
            error_code="unavailable",
        )
        with self.assertRaises(ValueError) as context:
            self.store.finish(run_id, unsafe_completion)
        self.assertEqual(str(context.exception), "unsafe_persistence_payload")
        self.assertEqual(self.store.runs[run_id]["revision"], 0)

    def test_constructed_running_completion_is_rejected_as_non_terminal(self):
        run_id = self.store.create_run(self.record)
        completion = RunCompletion.model_construct(
            expected_revision=0,
            node="finalize",
            status="running",
            state={"node": "finalize", "status": "running", "revision": 1},
            payload={},
            error_code=None,
        )
        with self.assertRaises(ValueError) as context:
            self.store.finish(run_id, completion)
        self.assertEqual(str(context.exception), "finish_requires_terminal_status")
        self.assertEqual(self.store.runs[run_id]["revision"], 0)

    def test_constructed_transition_normalizes_raw_status(self):
        run_id = self.store.create_run(self.record)
        transition = RunTransition.model_construct(
            expected_revision=0,
            node="resolve_parameters",
            event_type="transitioned",
            status="running",
            state={"node": "resolve_parameters", "status": "running", "revision": 1},
            payload={},
            error_code=None,
        )
        self.store.transition(run_id, transition)
        self.assertEqual(self.store.runs[run_id]["status"], "running")
        self.assertEqual(self.store.events[run_id][0]["status"], "running")

    def test_constructed_transition_normalizes_raw_event_type(self):
        run_id = self.store.create_run(self.record)
        transition = RunTransition.model_construct(
            expected_revision=0,
            node="resolve_parameters",
            event_type="entered",
            status=RunStatus.RUNNING,
            state={"node": "resolve_parameters", "status": "running", "revision": 1},
            payload={},
            error_code=None,
        )
        self.store.transition(run_id, transition)
        self.assertEqual(self.store.events[run_id][0]["event_type"], "entered")

    def test_save_artifact_rejects_unknown_run(self):
        with self.assertRaises(RunNotFound):
            self.store.save_artifact(uuid4(), NewArtifact(payload={"status": "ok"}))

    def test_finish_rejects_running_status(self):
        run_id = self.store.create_run(self.record)
        with self.assertRaises(ValueError) as context:
            self.store.finish(run_id, RunCompletion(
                expected_revision=0, node="finalize", status=RunStatus.RUNNING,
                state={"node": "finalize"},
            ))
        self.assertEqual(str(context.exception), "finish_requires_terminal_status")
        self.assertEqual(self.store.runs[run_id]["revision"], 0)
        self.assertIsNone(self.store.runs[run_id]["completed_at"])
