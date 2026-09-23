"""Contracts for the deterministic business-query state graph."""

import json
import unittest
from dataclasses import is_dataclass
from datetime import date, datetime, timedelta, timezone
from time import monotonic
from unittest.mock import patch
from uuid import uuid4

from pydantic import ValidationError

from bi_agent.business_query import (
    BusinessQueryContext,
    BusinessQueryInput,
    BusinessQueryNode,
    BusinessQueryRuntime,
    BusinessQueryState,
    InvalidBusinessQueryTransition,
    transition_state,
)
from bi_agent.business_query.nodes import (
    authorize_scope,
    resolve_parameters,
    validate_parameters,
)
from bi_agent.metrics import Coverage, ToolResult
from bi_agent.runtime.memory import MemoryQueryRunStore
from bi_agent.runtime.models import (
    ArtifactRef,
    ArtifactPersistenceError,
    DomainStatus,
    ErrorEnvelope,
    RecoveryAction,
    RunStatus,
)
from tests.fakeconn import S1_REF, S2_REF, CatalogConn


class BusinessQueryTransitionTests(unittest.TestCase):
    def test_normal_path_is_fixed(self):
        state = BusinessQueryState(run_id=uuid4())
        for node in (
            BusinessQueryNode.RESOLVE_PARAMETERS,
            BusinessQueryNode.VALIDATE_PARAMETERS,
            BusinessQueryNode.AUTHORIZE_SCOPE,
            BusinessQueryNode.EXECUTE_FIXED_QUERY,
            BusinessQueryNode.CLASSIFY_RESULT,
            BusinessQueryNode.PERSIST_ARTIFACT,
            BusinessQueryNode.FINALIZE,
        ):
            state = transition_state(state, node)
        self.assertEqual(state.node, BusinessQueryNode.FINALIZE)

    def test_execute_cannot_skip_validation(self):
        state = BusinessQueryState(run_id=uuid4())
        with self.assertRaisesRegex(InvalidBusinessQueryTransition, "^invalid_transition$"):
            transition_state(state, BusinessQueryNode.EXECUTE_FIXED_QUERY)


class BusinessQueryStateContractTests(unittest.TestCase):
    def test_state_rejects_ephemeral_context_fields(self):
        with self.assertRaises(ValidationError):
            BusinessQueryState(run_id=uuid4(), question="查询店铺 S1 的销售额")

    def test_constructor_rejects_raw_question_diagnostic_and_real_id(self):
        cases = (
            {"normalized_request": {"question": "查询店铺 S1 的销售额"}},
            {"limitations": ["psycopg.errors.SyntaxError: relation missing"]},
            {"normalized_request": {"shop_refs": ["S1"]}},
        )

        for values in cases:
            with self.subTest(values=values), self.assertRaisesRegex(
                ValidationError, "unsafe_persistence_payload"
            ):
                BusinessQueryState(run_id=uuid4(), **values)

    def test_assignment_rejects_unsafe_persisted_values(self):
        state = BusinessQueryState(run_id=uuid4())

        with self.assertRaisesRegex(ValidationError, "unsafe_persistence_payload"):
            state.limitations = ["psycopg.errors.SyntaxError: relation missing"]

        self.assertEqual(state.limitations, [])

    def test_all_dump_paths_reject_constructed_unsafe_state(self):
        state = BusinessQueryState.model_construct(
            run_id=uuid4(),
            normalized_request={"question": "查询店铺 S1 的销售额"},
        )
        dump_paths = (
            state.model_dump,
            lambda: state.model_dump(mode="json"),
            state.model_dump_json,
        )

        for dump in dump_paths:
            with self.subTest(dump=dump), self.assertRaisesRegex(
                ValueError, "^unsafe_persistence_payload$"
            ):
                dump()

    def test_model_copy_revalidates_unsafe_update(self):
        state = BusinessQueryState(run_id=uuid4())

        with self.assertRaisesRegex(ValidationError, "unsafe_persistence_payload"):
            state.model_copy(update={"normalized_request": {"shop_refs": ["S1"]}})

    def test_safe_future_state_shape_remains_serializable(self):
        now = datetime.now(timezone.utc)
        state = BusinessQueryState(
            run_id=uuid4(),
            node=BusinessQueryNode.CLASSIFY_RESULT,
            normalized_request={
                "shop_refs": [S1_REF],
                "metrics": ["paid_amount"],
                "start": "2026-09-01",
                "end": "2026-09-08",
                "group_by": "total",
                "compare": "none",
                "top_n": 10,
                "currency": "CNY",
            },
            problems=["invalid_metric"],
            tool_status="ok",
            target_status=DomainStatus.SUCCESS,
            coverage=Coverage(
                status="complete",
                start=date(2026, 9, 1),
                end=date(2026, 9, 8),
            ),
            data_as_of=now,
            limitations=["coverage_incomplete"],
            artifact_refs=[ArtifactRef(id=uuid4(), type="metric_result")],
            error=ErrorEnvelope(
                code="invalid_parameters",
                stage="validate_parameters",
                retryable=False,
                recovery=RecoveryAction.CORRECT_PARAMETERS,
                public_message="查询参数无效",
                problems=["invalid_metric"],
            ),
        )

        self.assertEqual(state.model_dump(mode="json")["node"], "classify_result")
        self.assertIn("classify_result", state.model_dump_json())

    def test_ephemeral_runtime_data_is_not_in_persisted_state(self):
        now = datetime.now(timezone.utc)
        context = BusinessQueryContext(
            chat_id=uuid4(),
            user_message_id=uuid4(),
            subject_id="user-1",
            question="查询真实店铺 S1 的销售额",
            previous_filters={"shop_id": "S1"},
            shop_refs={"S1": S1_REF},
            allowed_shop_ids={"S1"},
            now=now,
            deadline=monotonic() + 30,
            attempt_no=1,
        )
        runtime = BusinessQueryRuntime(
            state=BusinessQueryState(run_id=uuid4()),
            context=context,
        )

        self.assertTrue(is_dataclass(runtime))
        self.assertNotIn("S1", str(runtime.state.model_dump(mode="json")))
        self.assertNotIn("question", runtime.state.model_dump(mode="json"))


class BusinessQueryInputTests(unittest.TestCase):
    def test_input_keeps_parse_failure_auditable(self):
        payload = BusinessQueryInput(
            tool_call_id="call_1",
            arguments=None,
            arguments_error="invalid_json",
        )

        self.assertEqual(payload.tool_call_id, "call_1")
        self.assertIsNone(payload.arguments)
        self.assertEqual(payload.arguments_error, "invalid_json")


class BusinessQueryInputNodeTests(unittest.TestCase):
    def _runtime(
        self,
        *,
        question: str,
        previous_filters: dict[str, object] | None = None,
        arguments: dict[str, object] | None = None,
    ) -> BusinessQueryRuntime:
        now = datetime(2026, 9, 10, tzinfo=timezone.utc)
        return BusinessQueryRuntime(
            state=BusinessQueryState(run_id=uuid4()),
            context=BusinessQueryContext(
                chat_id=uuid4(),
                user_message_id=uuid4(),
                subject_id="user-1",
                question=question,
                previous_filters=previous_filters or {},
                shop_refs={"S1": S1_REF},
                allowed_shop_ids=frozenset({"S1"}),
                now=now,
                deadline=monotonic() + 30,
                attempt_no=1,
            ),
            resolved_args=arguments or {},
        )

    def _run_to_authorization(self, runtime: BusinessQueryRuntime) -> None:
        resolve_parameters(runtime)
        validate_parameters(runtime)
        authorize_scope(runtime)

    def test_resolve_inherits_filters_fills_period_and_persists_refs(self):
        runtime = self._runtime(
            question="那上个月呢",
            previous_filters={
                "shop_ids": ["S1"],
                "metrics": ["paid_amount"],
            },
        )

        resolve_parameters(runtime)

        self.assertEqual(runtime.resolved_args["shop_ids"], ["S1"])
        self.assertEqual(runtime.resolved_args["metrics"], ["paid_amount"])
        self.assertEqual(runtime.resolved_args["start"], "2026-08-01")
        self.assertEqual(runtime.resolved_args["end"], "2026-09-01")
        self.assertEqual(runtime.state.normalized_request["shop_refs"], [S1_REF])
        self.assertNotIn("S1", json.dumps(runtime.state.model_dump(mode="json")))

    def test_missing_shop_stops_before_query_execution(self):
        runtime = self._runtime(
            question="2026-09-01",
            arguments={
                "start": "2026-09-01",
                "end": "2026-09-02",
                "metrics": ["paid_amount"],
            },
        )

        with patch("bi_agent.metrics.query_business") as query_business:
            resolve_parameters(runtime)

        self.assertEqual(runtime.state.status, RunStatus.NEEDS_INPUT)
        self.assertEqual(runtime.state.target_status, DomainStatus.NEEDS_INPUT)
        self.assertEqual(runtime.state.error.code, "missing_parameters")  # type: ignore[union-attr]
        self.assertEqual(runtime.state.problems, ["missing_parameters"])
        query_business.assert_not_called()

    def test_invalid_date_stops_without_persisting_pydantic_diagnostics(self):
        runtime = self._runtime(
            question="查询店铺",
            arguments={
                "shop_ids": [S1_REF],
                "start": "not-a-date",
                "end": "2026-09-02",
                "metrics": ["paid_amount"],
            },
        )

        with patch("bi_agent.metrics.query_business") as query_business:
            resolve_parameters(runtime)
            validate_parameters(runtime)

        self.assertEqual(runtime.state.status, RunStatus.NEEDS_INPUT)
        self.assertEqual(runtime.state.target_status, DomainStatus.NEEDS_INPUT)
        self.assertEqual(runtime.state.error.code, "invalid_parameters")  # type: ignore[union-attr]
        self.assertEqual(runtime.state.problems, ["invalid_date_range"])
        persisted = json.dumps(runtime.state.model_dump(mode="json"))
        self.assertNotIn("not-a-date", persisted)
        self.assertNotIn("date_from", persisted)
        query_business.assert_not_called()

    def test_validation_maps_each_supported_field_to_a_safe_problem_code(self):
        cases = (
            ("start", {"start": datetime(2026, 9, 1, 12, tzinfo=timezone.utc)}, "invalid_date_range"),
            ("metrics", {"metrics": ["not_a_metric"]}, "invalid_metric"),
            ("group_by", {"group_by": "region"}, "invalid_group_by"),
            ("compare", {"compare": "next_period"}, "invalid_compare"),
            ("top_n", {"top_n": 0}, "invalid_top_n"),
            ("shop_ids", {"shop_ids": S1_REF}, "invalid_shop"),
        )
        base = {
            "shop_ids": [S1_REF],
            "start": "2026-09-01",
            "end": "2026-09-02",
            "metrics": ["paid_amount"],
        }

        for field, invalid, expected_problem in cases:
            with self.subTest(field=field):
                runtime = self._runtime(question="查询店铺", arguments={**base, **invalid})

                resolve_parameters(runtime)
                validate_parameters(runtime)

                self.assertEqual(runtime.state.status, RunStatus.NEEDS_INPUT)
                self.assertEqual(runtime.state.problems, [expected_problem])
                self.assertEqual(runtime.state.error.code, "invalid_parameters")  # type: ignore[union-attr]

    def test_unknown_aliases_and_injection_strings_are_forbidden_without_querying(self):
        for shop_id in (S2_REF, "'; DROP TABLE reporting.v_shops; --"):
            with self.subTest(shop_id=shop_id):
                runtime = self._runtime(
                    question="查询店铺",
                    arguments={
                        "shop_ids": [shop_id],
                        "start": "2026-09-01",
                        "end": "2026-09-02",
                        "metrics": ["paid_amount"],
                    },
                )

                with patch("bi_agent.metrics.query_business") as query_business:
                    self._run_to_authorization(runtime)

                self.assertEqual(runtime.state.status, RunStatus.FAILED)
                self.assertEqual(runtime.state.target_status, DomainStatus.FAILED)
                self.assertEqual(runtime.state.error.code, "forbidden")  # type: ignore[union-attr]
                self.assertEqual(runtime.state.problems, ["forbidden"])
                self.assertEqual(
                    runtime.state.normalized_request["shop_refs"], ["invalid_shop"]
                )
                self.assertNotIn(shop_id, json.dumps(runtime.state.model_dump(mode="json")))
                query_business.assert_not_called()

    def test_model_supplied_real_shop_id_is_forbidden_without_querying(self):
        runtime = self._runtime(
            question="查询店铺",
            arguments={
                "shop_ids": ["S1"],
                "start": "2026-09-01",
                "end": "2026-09-02",
                "metrics": ["paid_amount"],
            },
        )

        with patch("bi_agent.metrics.query_business") as query_business:
            self._run_to_authorization(runtime)

        self.assertEqual(runtime.state.status, RunStatus.FAILED)
        self.assertEqual(runtime.state.target_status, DomainStatus.FAILED)
        self.assertEqual(runtime.state.error.code, "forbidden")  # type: ignore[union-attr]
        self.assertEqual(runtime.state.problems, ["forbidden"])
        self.assertEqual(
            runtime.state.normalized_request["shop_refs"], ["invalid_shop"]
        )
        self.assertNotIn("S1", json.dumps(runtime.state.model_dump(mode="json")))
        query_business.assert_not_called()

    def test_omitted_shop_ids_inherit_trusted_previous_scope(self):
        runtime = self._runtime(
            question="查询店铺",
            previous_filters={
                "shop_ids": ["S1"],
                "start": "2026-09-01",
                "end": "2026-09-02",
                "metrics": ["paid_amount"],
            },
        )

        self._run_to_authorization(runtime)

        self.assertEqual(runtime.request.shop_ids, ["S1"])  # type: ignore[union-attr]
        self.assertEqual(runtime.state.node, BusinessQueryNode.EXECUTE_FIXED_QUERY)
        self.assertEqual(runtime.state.status, RunStatus.RUNNING)

    def test_non_string_shop_members_are_invalid_parameters_without_querying(self):
        for shop_id in (None, 7):
            with self.subTest(shop_id=shop_id):
                runtime = self._runtime(
                    question="查询店铺",
                    arguments={
                        "shop_ids": [shop_id],
                        "start": "2026-09-01",
                        "end": "2026-09-02",
                        "metrics": ["paid_amount"],
                    },
                )

                with patch("bi_agent.metrics.query_business") as query_business:
                    resolve_parameters(runtime)
                    validate_parameters(runtime)

                self.assertEqual(runtime.state.status, RunStatus.NEEDS_INPUT)
                self.assertEqual(runtime.state.target_status, DomainStatus.NEEDS_INPUT)
                self.assertEqual(runtime.state.error.code, "invalid_parameters")  # type: ignore[union-attr]
                self.assertEqual(runtime.state.problems, ["invalid_shop"])
                self.assertEqual(
                    runtime.state.normalized_request["shop_refs"], ["invalid_shop"]
                )
                self.assertNotIn("shop_ids", runtime.state.normalized_request)
                query_business.assert_not_called()

    def test_basis_policy_enters_the_audited_normalized_request(self):
        """basis_policy 决定兼容性判定：它必须进规范化请求（审计身份），不能被丢掉。"""
        runtime = self._runtime(
            question="查询店铺",
            arguments={
                "shop_ids": [S1_REF],
                "start": "2026-09-01",
                "end": "2026-09-02",
                "metrics": ["paid_amount"],
                "group_by": "shop",
                "basis_policy": "separate",
            },
        )

        resolve_parameters(runtime)
        validate_parameters(runtime)

        self.assertEqual(runtime.state.normalized_request["basis_policy"], "separate")

    def test_half_month_window_overrides_a_model_end_that_reaches_today(self):
        """「近半个月」对两侧边界都是权威的：模型给的当天/未来结束日不得留下。"""
        runtime = self._runtime(
            question="近半月最好的10个商品",
            arguments={
                "shop_ids": [S1_REF],
                "start": "2026-09-01",
                "end": "2026-09-19",
                "metrics": ["product_paid_amount"],
                "group_by": "product",
                "basis_policy": "partitioned",
            },
        )

        resolve_parameters(runtime)
        validate_parameters(runtime)

        # now 是 2026-09-10（北京）：最近 15 个完整日为 [2026-08-26, 2026-09-10)。
        self.assertEqual(runtime.request.start, date(2026, 8, 26))
        self.assertEqual(runtime.request.end, date(2026, 9, 10))
        self.assertEqual(runtime.state.normalized_request["start"], "2026-08-26")
        self.assertEqual(runtime.state.normalized_request["end"], "2026-09-10")
        self.assertEqual(runtime.state.normalized_request["basis_policy"], "partitioned")

    def test_half_month_window_overrides_a_narrower_model_window(self):
        """更窄的模型窗口同样是残缺窗口：它会把后 8 天静默排在榜外。"""
        runtime = self._runtime(
            question="近半个月销量最好的商品",
            arguments={
                "shop_ids": [S1_REF],
                "start": "2026-09-03",
                "end": "2026-09-10",
                "metrics": ["quantity"],
                "group_by": "product",
            },
        )

        resolve_parameters(runtime)
        validate_parameters(runtime)

        self.assertEqual(runtime.request.start, date(2026, 8, 26))
        self.assertEqual(runtime.request.end, date(2026, 9, 10))

    def test_user_authorized_prior_window_survives_half_month_normalization(self):
        """Only a trusted fallback after a gap may shift the user's half-month window."""
        runtime = self._runtime(
            question="近半个月最好的商品，遇到数据缺口就把时间段向前移动",
            arguments={"shop_ids": [S1_REF], "start": "2026-08-21",
                       "end": "2026-09-05", "metrics": ["product_paid_amount"],
                       "group_by": "product", "basis_policy": "partitioned"},
        )
        runtime.context.trusted_window_override = ("2026-08-21", "2026-09-05")
        resolve_parameters(runtime)
        validate_parameters(runtime)
        self.assertEqual((runtime.request.start, runtime.request.end),
                         (date(2026, 8, 21), date(2026, 9, 5)))
        self.assertEqual(runtime.state.normalized_request["start"], "2026-08-21")

    def test_full_gap_allows_one_whole_prior_half_month(self):
        runtime = self._runtime(
            question="近半个月最好的商品，遇到数据缺口就把时间段向前移动",
            arguments={"shop_ids": [S1_REF], "start": "2026-08-11",
                       "end": "2026-08-26", "metrics": ["product_paid_amount"],
                       "group_by": "product", "basis_policy": "partitioned"},
        )
        runtime.context.trusted_window_override = ("2026-08-11", "2026-08-26")
        resolve_parameters(runtime)
        validate_parameters(runtime)
        self.assertEqual((runtime.request.start, runtime.request.end),
                         (date(2026, 8, 11), date(2026, 8, 26)))

    def test_unapproved_prior_window_is_still_overridden(self):
        runtime = self._runtime(
            question="近半个月最好的商品",
            arguments={"shop_ids": [S1_REF], "start": "2026-08-21",
                       "end": "2026-09-05", "metrics": ["product_paid_amount"],
                       "group_by": "product", "basis_policy": "partitioned"},
        )
        runtime.context.trusted_window_override = ("2026-08-21", "2026-09-05")
        resolve_parameters(runtime)
        validate_parameters(runtime)
        self.assertEqual((runtime.request.start, runtime.request.end),
                         (date(2026, 8, 26), date(2026, 9, 10)))

    def test_explicit_range_in_the_question_wins_over_model_args_and_relative_words(self):
        """问题里写明的日期范围优先：既盖过模型给的边界，也盖过句中的相对词。"""
        runtime = self._runtime(
            question="9月1日至7日的商品支付金额，最近7天也行",
            arguments={
                "shop_ids": [S1_REF],
                "start": "2026-08-01",
                "end": "2026-08-31",
                "metrics": ["product_paid_amount"],
                "group_by": "product",
            },
        )

        resolve_parameters(runtime)
        validate_parameters(runtime)

        self.assertEqual(runtime.request.start, date(2026, 9, 1))
        self.assertEqual(runtime.request.end, date(2026, 9, 8))

    def test_relative_calendar_words_keep_todays_setdefault_behaviour(self):
        """反恒真：未被授权的相对词仍只做缺省填充，不覆盖模型给的边界。"""
        runtime = self._runtime(
            question="那上个月呢",
            arguments={
                "shop_ids": [S1_REF],
                "start": "2026-09-03",
                "end": "2026-09-06",
                "metrics": ["paid_amount"],
            },
        )

        resolve_parameters(runtime)

        self.assertEqual(runtime.resolved_args["start"], "2026-09-03")
        self.assertEqual(runtime.resolved_args["end"], "2026-09-06")

    def test_only_authorized_runtime_reaches_execute_node(self):
        runtime = self._runtime(
            question="查询店铺",
            arguments={
                "shop_ids": [S1_REF],
                "start": "2026-09-01",
                "end": "2026-09-02",
                "metrics": ["paid_amount"],
            },
        )

        self._run_to_authorization(runtime)

        self.assertEqual(runtime.request.shop_ids, ["S1"])  # type: ignore[union-attr]
        self.assertEqual(runtime.state.node, BusinessQueryNode.EXECUTE_FIXED_QUERY)
        self.assertEqual(runtime.state.status, RunStatus.RUNNING)


class _ArtifactFailingStore(MemoryQueryRunStore):
    def save_artifact(self, run_id, artifact):  # type: ignore[no-untyped-def]
        raise ArtifactPersistenceError("database password=not-for-public-output")


class _StoreFailingAtNode(MemoryQueryRunStore):
    """Fails one mid-graph transition the way a lost database connection would."""

    def __init__(self, *, forbidden_values, fail_at_node):  # type: ignore[no-untyped-def]
        super().__init__(forbidden_values=forbidden_values)
        self.fail_at_node = fail_at_node

    def transition(self, run_id, transition):  # type: ignore[no-untyped-def]
        if transition.node == self.fail_at_node:
            raise RuntimeError("psycopg.OperationalError connection reset")
        return super().transition(run_id, transition)


class BusinessQueryExecutionTests(unittest.TestCase):
    START = date(2026, 9, 1)
    END = date(2026, 9, 8)
    NOW = datetime(2026, 9, 10, tzinfo=timezone.utc)

    def _context(self, *, deadline: float | None = None) -> BusinessQueryContext:
        return BusinessQueryContext(
            chat_id=uuid4(),
            user_message_id=uuid4(),
            subject_id="user-1",
            question="查询店铺的支付金额",
            previous_filters={},
            shop_refs={"S1": S1_REF},
            allowed_shop_ids=frozenset({"S1"}),
            now=self.NOW,
            deadline=deadline if deadline is not None else monotonic() + 30,
            attempt_no=1,
        )

    def _tool_input(self) -> BusinessQueryInput:
        return BusinessQueryInput(
            tool_call_id="call_1",
            arguments={
                "shop_ids": [S1_REF],
                "start": self.START.isoformat(),
                "end": self.END.isoformat(),
                "metrics": ["paid_amount"],
            },
        )

    def _result(
        self,
        *,
        status: str = "ok",
        data: list[dict[str, str | int | None]] | None = None,
        coverage: Coverage | None = None,
        filters: dict[str, object] | None = None,
    ) -> ToolResult:
        return ToolResult(
            status=status,  # type: ignore[arg-type]
            data=data if data is not None else [{"paid_amount": "1000"}],
            coverage=coverage or Coverage(
                status="complete", start=self.START, end=self.END
            ),
            filters=filters or {},
        )

    def _execute(self, store, result: ToolResult, *, context: BusinessQueryContext | None = None):  # type: ignore[no-untyped-def]
        from bi_agent.business_query.graph import _execute_business_query_graph

        with patch("bi_agent.metrics.query_business", return_value=result) as query_business:
            execution = _execute_business_query_graph(
                CatalogConn(), store, self._tool_input(), context or self._context()
            )
        return execution, query_business

    def test_success_executes_once_persists_artifact_and_records_fixed_nodes(self):
        store = MemoryQueryRunStore(forbidden_values={"S1", "ERP-P-9"})

        execution, query_business = self._execute(store, self._result())

        query_business.assert_called_once()
        self.assertEqual(execution.domain_result.status, DomainStatus.SUCCESS)
        run = store.runs[execution.domain_result.run_id]
        self.assertEqual(run["status"], RunStatus.SUCCEEDED.value)
        self.assertEqual(
            run["normalized_request"],
            {
                "shop_refs": [S1_REF],
                "metrics": ["paid_amount"],
                "start": "2026-09-01",
                "end": "2026-09-08",
                "group_by": "total",
                "compare": "none",
                "top_n": 10,
                "currency": "CNY",
                # 兼容性判据的一半：口径策略是审计身份的一部分，不能丢。
                "basis_policy": "strict",
            },
        )
        self.assertEqual(
            run["normalized_request"], run["state"]["normalized_request"]
        )
        self.assertEqual(
            [event["node"] for event in store.events[execution.domain_result.run_id]],
            [
                "resolve_parameters",
                "validate_parameters",
                "authorize_scope",
                "execute_fixed_query",
                "classify_result",
                "persist_artifact",
                "finalize",
            ],
        )
        self.assertEqual(len(execution.domain_result.artifacts), 1)
        self.assertEqual(len(store.artifacts), 1)

    def test_classifies_zero_missing_and_unavailable_results_without_inventing_partial(self):
        cases = (
            (
                self._result(data=[{"paid_amount": "0"}]),
                DomainStatus.SUCCESS,
            ),
            (
                self._result(
                    status="missing_data",
                    data=[],
                    coverage=Coverage(status="missing", start=self.START, end=self.END),
                ),
                DomainStatus.MISSING_DATA,
            ),
            (
                self._result(
                    status="missing_data",
                    data=[],
                    coverage=Coverage(
                        status="partial",
                        start=self.START,
                        end=self.END,
                        gaps=["2026-09-05~2026-09-08"],
                    ),
                ),
                DomainStatus.MISSING_DATA,
            ),
            (
                self._result(
                    status="unavailable",
                    data=[],
                    coverage=Coverage(status="missing", start=None, end=None),
                ),
                DomainStatus.FAILED,
            ),
        )
        for result, expected_status in cases:
            with self.subTest(tool_status=result.status, coverage=result.coverage.status):
                store = MemoryQueryRunStore(forbidden_values={"S1", "ERP-P-9"})
                execution, query_business = self._execute(
                    store, result
                )

                query_business.assert_called_once()
                self.assertEqual(execution.domain_result.status, expected_status)
                self.assertEqual(len(execution.domain_result.artifacts), 1)
                self.assertIn(
                    "persist_artifact",
                    [event["node"] for event in store.events[execution.domain_result.run_id]],
                )

    def test_artifact_persistence_failure_overrides_result_without_exposing_reason(self):
        store = _ArtifactFailingStore(forbidden_values={"S1", "ERP-P-9"})

        execution, query_business = self._execute(store, self._result())

        query_business.assert_called_once()
        self.assertEqual(execution.domain_result.status, DomainStatus.FAILED)
        self.assertEqual(execution.domain_result.error.code, "artifact_persistence_failed")  # type: ignore[union-attr]
        self.assertEqual(execution.domain_result.artifacts, [])
        self.assertIsNone(execution.tool_result)
        self.assertEqual(execution.session_filters, {})
        self.assertEqual(store.artifacts, {})
        public_output = json.dumps(execution.domain_result.model_dump(mode="json"))
        self.assertNotIn("database password=not-for-public-output", public_output)

    def test_result_contract_violation_cannot_expose_result_or_session_filters(self):
        from bi_agent.business_query.graph import _execute_business_query_graph

        store = MemoryQueryRunStore(forbidden_values={"S1", "ERP-P-9"})
        with patch(
            "bi_agent.metrics.query_business", return_value=self._result()
        ) as query_business, patch(
            "bi_agent.business_query.nodes.to_public_artifact",
            side_effect=ValueError("malformed projection"),
        ):
            execution = _execute_business_query_graph(
                CatalogConn(), store, self._tool_input(), self._context()
            )

        query_business.assert_called_once()
        self.assertEqual(execution.domain_result.status, DomainStatus.FAILED)
        self.assertEqual(execution.domain_result.error.code, "result_contract_violation")  # type: ignore[union-attr]
        self.assertIsNone(execution.tool_result)
        self.assertEqual(execution.session_filters, {})
        self.assertEqual(execution.domain_result.artifacts, [])

    def test_unexpected_store_failure_still_finishes_the_run(self):
        """C-7：图半途抛错不得把 run 留在 running，必须先兜底写入 FAILED 再原样上抛。"""
        from bi_agent.business_query.graph import _execute_business_query_graph

        store = _StoreFailingAtNode(forbidden_values={"S1", "ERP-P-9"},
                                    fail_at_node="execute_fixed_query")
        with patch("bi_agent.metrics.query_business", return_value=self._result()):
            with self.assertRaisesRegex(RuntimeError, "connection reset"):
                _execute_business_query_graph(
                    CatalogConn(), store, self._tool_input(), self._context()
                )

        self.assertEqual(len(store.runs), 1)
        run = next(iter(store.runs.values()))
        self.assertEqual(run["status"], RunStatus.FAILED.value)
        self.assertEqual(run["error_code"], "unavailable")
        self.assertIsNotNone(run["completed_at"])
        state = run["state"]
        self.assertEqual(state["status"], RunStatus.FAILED.value)
        self.assertEqual(state["target_status"], DomainStatus.FAILED.value)
        self.assertEqual(state["error"]["code"], "unavailable")
        self.assertEqual(state["error"]["stage"], "execute_fixed_query")
        self.assertEqual(state["error"]["public_message"], "查询暂不可用，请稍后重试。")
        self.assertEqual(
            [event["node"] for event in store.events[run["id"]]],
            ["resolve_parameters", "validate_parameters", "authorize_scope",
             "execute_fixed_query"],
        )
        last_event = store.events[run["id"]][-1]
        self.assertEqual(last_event["event_type"], "failed")
        self.assertEqual(last_event["status"], RunStatus.FAILED.value)
        # 兜底写入不得把驱动细节带进审计行。
        self.assertNotIn("connection reset",
                         json.dumps(run, ensure_ascii=False, default=str))

    def test_projection_failure_gates_result_filters_and_artifacts(self):
        """C-8：_execution_result 里投影被拒时，三个出口必须同时关。"""
        from bi_agent.business_query.graph import _execute_business_query_graph

        store = MemoryQueryRunStore(forbidden_values={"S1", "ERP-P-9"})
        with patch("bi_agent.metrics.query_business", return_value=self._result()), patch(
            "bi_agent.business_query.tool.to_public_artifact",
            side_effect=ValueError("unsafe_persistence_payload"),
        ) as project:
            execution = _execute_business_query_graph(
                CatalogConn(), store, self._tool_input(), self._context()
            )

        project.assert_called_once()
        self.assertEqual(execution.domain_result.status, DomainStatus.FAILED)
        self.assertEqual(execution.domain_result.model_payload, {"status": "failed"})
        self.assertEqual(execution.domain_result.artifacts, [])
        self.assertIsNone(execution.tool_result)
        self.assertEqual(execution.session_filters, {})
        serialized = json.dumps(
            execution.domain_result.model_dump(mode="json"),
            ensure_ascii=False, default=str,
        ) + json.dumps([execution.session_filters, execution.tool_result],
                       ensure_ascii=False, default=str)
        self.assertNotIn("1000", serialized)

    def test_expired_deadline_fails_without_calling_metrics(self):
        store = MemoryQueryRunStore(forbidden_values={"S1", "ERP-P-9"})

        with patch("bi_agent.business_query.nodes.monotonic", return_value=100.0):
            execution, query_business = self._execute(
                store, self._result(), context=self._context(deadline=100.0)
            )

        query_business.assert_not_called()
        self.assertEqual(execution.domain_result.status, DomainStatus.FAILED)
        self.assertEqual(execution.domain_result.error.code, "deadline_exceeded")  # type: ignore[union-attr]

    def test_query_receives_the_original_nearly_expired_monotonic_deadline(self):
        store = MemoryQueryRunStore(forbidden_values={"S1", "ERP-P-9"})

        with patch("bi_agent.business_query.nodes.monotonic", return_value=99.9):
            execution, query_business = self._execute(
                store, self._result(), context=self._context(deadline=100.0)
            )

        self.assertEqual(execution.domain_result.status, DomainStatus.SUCCESS)
        query_business.assert_called_once()
        self.assertEqual(query_business.call_args.kwargs["deadline"], 100.0)

    def test_projections_and_persistence_never_expose_real_or_requested_identifiers(self):
        store = MemoryQueryRunStore(forbidden_values={"S1", "ERP-P-9"})
        result = self._result(
            data=[{"shop_id": "S1", "product_id": "ERP-P-9", "paid_amount": "1000"}],
            filters={
                "shop_ids": ["S1"],
                "requested_shop_ids": ["S1"],
            },
        )

        execution, query_business = self._execute(store, result)

        query_business.assert_called_once()
        safe_values = (
            execution.domain_result.model_dump(mode="json"),
            [artifact["payload"] for artifact in store.artifacts.values()],
            store.runs[execution.domain_result.run_id]["state"],
            store.events[execution.domain_result.run_id],
        )
        for value in safe_values:
            serialized = json.dumps(value, ensure_ascii=False, default=str)
            self.assertNotIn("S1", serialized)
            self.assertNotIn("ERP-P-9", serialized)
        self.assertNotIn("requested_shop_ids", execution.domain_result.model_payload["filters"])
