"""approved 查询学习记忆的契约与生命周期（计划 2026-09-14-approved-query-memory.md
Task 1、Task 2）。

Task 1 钉形状与净化规则；QueryMemoryLifecycleTests 钉 Task 2 的草稿来源边界与
人工状态机（离线用替身，真库用例在 tests.test_runtime_db）。所有断言都落在稳定
原因码（`memory_*` / `replacement_ref_action_mismatch`）上；被拒的输入本身不允许
出现在错误文本里（`hide_input_in_errors`），否则报错就成了第二条泄露通道。
"""

import unittest
from uuid import UUID

from pydantic import ValidationError


def versions():
    from bi_agent.runtime.versions import VersionSet

    return VersionSet(
        schema_version="reporting/2026-09-14.1", semantic_catalog_version="semantic/2026-09-14.1",
        data_catalog_version=7, metric_version="metrics/2026-09-12.1",
        policy_version="multi-source-policy/2026-09-12.1",
        source_registry_version="sources/2026-09-12.1",
        graph_version="business_query-graph/2026-09-11.1")


def valid_slots():
    from bi_agent.query_memory.models import QuerySlot

    return (QuerySlot(name="shop_scope", kind="entity_scope"),
            QuerySlot(name="date_window", kind="date_window"))


def valid_payload(**overrides):
    from bi_agent.query_memory.models import ApprovedExample

    payload = {
        "example_ref": "mem-approved-001",
        "domain": "controlled_exploration",
        "intent_signature": "cost-by-shop-and-window",
        "question_template": "比较 {shop_scope} 在 {date_window} 的成本",
        "slots": valid_slots(),
        "normalized_request": {"requested_metric_refs": ["metric-cost-total"]},
        "expected_tool": "explore_business_data",
        "version_requirements": versions(),
        "approval_revision": 1,
    }
    payload.update(overrides)
    return payload


class QueryMemoryContractTests(unittest.TestCase):
    def test_example_accepts_slots_and_rejects_bound_business_values(self):
        from bi_agent.query_memory.models import ApprovedExample

        example = ApprovedExample(**valid_payload())
        self.assertEqual(example.slots[0].name, "shop_scope")
        with self.assertRaises(ValidationError):
            ApprovedExample(**valid_payload(normalized_request={
                "start": "2026-09-01", "target_price": "99.00"}))

    def test_forbidden_value_keys_match_the_documented_vocabulary(self):
        """021 的 `approved_query_no_bound_values` CHECK 与这份词表逐一对应。"""
        from bi_agent.query_memory.models import FORBIDDEN_VALUE_KEYS

        self.assertEqual(FORBIDDEN_VALUE_KEYS, frozenset({
            "start", "end", "date", "shop_id", "subject_id", "target_price",
            "threshold", "budget", "sql_text", "rows", "result", "prompt"}))

    def test_forbidden_keys_are_rejected_at_any_depth(self):
        """污染几乎都藏在嵌套结构里：只查顶层键等于没查。"""
        from bi_agent.query_memory.models import ApprovedExample

        for poisoned in (
            {"semantic_selection": {"join_path_refs": ["join-shop-daily"],
                                    "filters": {"date": "2026-09"}}},
            {"requested_metric_refs": [{"metric": "metric-cost-total",
                                        "window": {"start": "2026-09-01"}}]},
            {"scope_mode": "selected", "notes": [{"budget": "300"}]},
        ):
            with self.subTest(payload=str(poisoned)[:44]):
                with self.assertRaises(ValidationError) as caught:
                    ApprovedExample(**valid_payload(normalized_request=poisoned))
                self.assertIn("memory_contains_bound_business_value",
                              str(caught.exception))

    def test_request_values_must_be_stable_refs_or_registered_codes(self):
        """值字符串只收稳定 ref 与已登记业务码；日期、金额、真名、UUID、SQL 一律不收。"""
        from bi_agent.query_memory.models import ApprovedExample

        for accepted in (
            {"requested_metric_refs": ["metric-cost-total", "view-shop-daily"]},
            {"product_ref": "ent-1a2b3c4d"},
            {"sales_basis": "erp_effective_parent", "profit_basis": "existing_fields"},
            {"currency": "CNY", "compare": "previous_period", "scope_mode": "selected"},
            {"report_kind": "comparison", "metrics": ["paid_amount"]},
            {"platforms": ["fxg", "1688"]},
        ):
            with self.subTest(request=str(accepted)[:44]):
                ApprovedExample(**valid_payload(normalized_request=accepted))
        for rejected in ("2026-09-01", "99.00", "店铺A",
                         "550e8400-e29b-41d4-a716-446655440000",
                         "select", "DROP TABLE", "446655440000"):
            with self.subTest(value=rejected):
                with self.assertRaises(ValidationError) as caught:
                    ApprovedExample(**valid_payload(normalized_request={
                        "requested_metric_refs": [rejected]}))
                self.assertIn("memory_value_not_a_stable_ref_or_code",
                              str(caught.exception))
        # 数字、布尔与 null 不是稳定 ref：金额与阈值可以藏在任何标量里，标量一律不收。
        for scalar in (99.0, 7, True, None):
            with self.subTest(scalar=repr(scalar)):
                with self.assertRaises(ValidationError):
                    ApprovedExample(**valid_payload(normalized_request={
                        "requested_metric_refs": [scalar]}))

    def test_template_rejects_bound_literals_and_sensitive_text(self):
        """模板只许文字与槽位占位符：UUID、长数字、日期、金额、SQL 关键字与非
        `ent-` 内部 ref 都是绑定信息或注入面。"""
        from bi_agent.query_memory.models import ApprovedExample

        for bad_template in (
            "比较 {shop_scope} 在 {date_window} 的成本 "
            "550e8400-e29b-41d4-a716-446655440000",
            "比较 {shop_scope} 在 446655440000 的成本",
            "比较 {shop_scope} 在 2026-09-01 的成本",
            "比较 {shop_scope} 在 99.00 的成本",
            "SELECT 成本 FROM {shop_scope}",
            "比较 metric-cost-total 在 {date_window} 的成本",
        ):
            with self.subTest(template=bad_template[:24]):
                with self.assertRaises(ValidationError) as caught:
                    ApprovedExample(**valid_payload(question_template=bad_template))
                self.assertIn("memory_template_bound_value", str(caught.exception))
        # `ent-` 引用是稳定 opaque ref，允许出现在模板里；占位符不受 ref 检查影响。
        ApprovedExample(**valid_payload(
            question_template="比较 ent-1a2b3c4d 在 {date_window} 的成本",
            slots=valid_slots()[1:]))

    def test_slots_must_appear_in_template_and_stay_unique(self):
        from bi_agent.query_memory.models import ApprovedExample, QuerySlot

        with self.assertRaises(ValidationError) as caught:
            ApprovedExample(**valid_payload(slots=valid_slots() + (
                QuerySlot(name="target_price", kind="target_price"),)))
        self.assertIn("memory_slot_missing_from_template", str(caught.exception))
        with self.assertRaises(ValidationError) as caught:
            ApprovedExample(**valid_payload(
                slots=(valid_slots()[0], valid_slots()[0]),
                question_template="比较 {shop_scope} 的成本"))
        self.assertIn("memory_slot_duplicate", str(caught.exception))

    def test_contracts_reject_unknown_fields(self):
        from bi_agent.query_memory.models import ApprovedExample

        with self.assertRaises(ValidationError):
            ApprovedExample(**valid_payload(shop_id="S1"))

    def test_validation_errors_do_not_echo_the_rejected_input(self):
        from bi_agent.query_memory.models import ApprovedExample

        with self.assertRaises(ValidationError) as caught:
            ApprovedExample(**valid_payload(normalized_request={
                "requested_metric_refs": ["店铺A"]}))
        self.assertNotIn("店铺A", str(caught.exception))

    def test_stored_record_applies_the_same_sanitation_and_gate(self):
        """存储记录与 approved 投影同规则：污染字段进不了库的形状，draft 升不了投影。"""
        from bi_agent.query_memory.models import ApprovedExample, StoredMemoryRecord

        def record(**overrides):
            payload = {
                "example_ref": "mem-approved-001",
                "source_run_id": "550e8400-e29b-41d4-a716-446655440000",
                "owner_subject_id": "subject-a",
                "domain": "controlled_exploration",
                "intent_signature": "cost-by-shop-and-window",
                "question_template": "比较 {shop_scope} 在 {date_window} 的成本",
                "slots": valid_slots(),
                "normalized_request": {"requested_metric_refs": ["metric-cost-total"]},
                "expected_tool": "explore_business_data",
                "version_requirements": versions(),
                "authorization_refs": ("ent-1a2b3c4d",),
                "status": "approved",
                "approval_revision": 1,
            }
            payload.update(overrides)
            return StoredMemoryRecord(**payload)

        with self.assertRaises(ValidationError) as caught:
            record(normalized_request={"scope_mode": "selected",
                                       "filters": {"shop_id": "S1"}})
        self.assertIn("memory_contains_bound_business_value", str(caught.exception))
        with self.assertRaisesRegex(ValueError, "memory_not_approved"):
            record(status="draft", approval_revision=0).as_approved()
        with self.assertRaisesRegex(ValueError, "memory_not_approved"):
            record(status="revoked").as_approved()
        example = record().as_approved()
        self.assertIsInstance(example, ApprovedExample)
        self.assertEqual(example.example_ref, "mem-approved-001")
        dumped = example.model_dump()
        for forbidden in ("source_run_id", "owner_subject_id",
                          "authorization_refs", "status"):
            self.assertNotIn(forbidden, dumped)

    def test_approval_commands_pair_supersede_with_a_replacement(self):
        from bi_agent.query_memory.models import ApprovalCommand

        ApprovalCommand(action="approve", reason="人工复核通过")
        ApprovalCommand(action="revoke", reason="证据失效，立即撤销")
        ApprovalCommand(action="supersede", reason="版本升级，换用新样例",
                        replacement_ref="mem-approved-002")
        for command in (
            {"action": "supersede", "reason": "缺少替换目标"},
            {"action": "approve", "reason": "批准不该带替换目标",
             "replacement_ref": "mem-approved-002"},
            {"action": "revoke", "reason": "撤销不该带替换目标",
             "replacement_ref": "mem-approved-002"},
        ):
            with self.subTest(action=command["action"]), \
                    self.assertRaisesRegex(ValidationError,
                                           "replacement_ref_action_mismatch"):
                ApprovalCommand(**command)


class QueryMemoryLifecycleTests(unittest.TestCase):
    """计划 Task 2：草稿来源边界与 draft→approved→superseded/revoked 人工状态机。

    全部跑在 fakeconn.QueryMemoryConn 上：替身对白名单之外的 SQL（含聊天表与
    Artifact 载荷）显式报错，所以“builder 不读聊天与结果”是替身保证的性质。
    真库事务、锁与约束的用例在 tests.test_runtime_db。
    """

    RUN_ID = UUID("550e8400-e29b-41d4-a716-446655440000")

    # ---- 造替身的小工具 ---------------------------------------------------

    def _conn(self, **overrides):
        from tests.fakeconn import QueryMemoryConn, memory_run_row

        return QueryMemoryConn(run_row=memory_run_row(**overrides))

    def failed_run_conn(self, *, owner: str = "subject-a"):
        return self._conn(subject=owner, status="failed")

    def build(self, conn, *, owner: str = "subject-a", run_id=None,
              template: str = "比较 {shop_scope} 在 {date_window} 的成本",
              slots=None, created_by: str = "reviewer-a"):
        from bi_agent.query_memory.repository import build_draft_from_run

        return build_draft_from_run(
            conn, run_id=run_id or self.RUN_ID, owner_subject_id=owner,
            question_template=template,
            slots=valid_slots() if slots is None else slots,
            created_by=created_by)

    def memory_repository(self, *, status: str = "draft", revision: int = 0,
                          domain: str = "controlled_exploration",
                          extra=None):
        from bi_agent.query_memory.repository import QueryMemoryRepository
        from tests.fakeconn import QueryMemoryConn, example_row

        rows = {"mem-approved-001": example_row(
            "mem-approved-001", status=status, revision=revision, domain=domain)}
        rows.update(extra or {})
        return QueryMemoryRepository(QueryMemoryConn(examples=rows))

    def command(self, action: str, reason: str, replacement=None):
        from bi_agent.query_memory.models import ApprovalCommand

        return ApprovalCommand(action=action, reason=reason,
                               replacement_ref=replacement)

    def draft_command(self):
        """对默认 run 可用的最小构建参数（模板含全部槽位）。"""
        return dict(template="比较 {shop_scope} 在 {date_window} 的成本",
                    slots=valid_slots())

    # ---- 草稿来源：只有本人、已成功、血缘完整、领域登记的运行可入草稿 ----------

    def test_only_succeeded_owned_run_can_become_draft(self):
        with self.assertRaisesRegex(ValueError, "memory_source_run_not_eligible"):
            self.build(self.failed_run_conn(owner="subject-a"), **self.draft_command())

    def test_other_users_succeeded_run_is_not_eligible(self):
        conn = self._conn(subject="subject-b", status="succeeded")
        with self.assertRaisesRegex(ValueError, "memory_source_run_not_eligible"):
            self.build(conn, **self.draft_command())

    def test_incomplete_provenance_or_unknown_domain_is_not_eligible(self):
        # 血缘列占资格读取行的第 4–12 位：逐项探测「完整」判定的每个缺口。
        for name, index, value in (("missing", 4, None),
                                   ("metric_version", 6, None),
                                   ("catalog_version", 8, -1),
                                   ("source_registry", 12, "")):
            with self.subTest(provenance=name):
                conn = self._conn()
                row = list(conn.run_row)
                row[index] = value
                conn.run_row = tuple(row)
                with self.assertRaisesRegex(ValueError,
                                            "memory_source_run_not_eligible"):
                    self.build(conn, **self.draft_command())
        with self.subTest(provenance="absent"):
            with self.assertRaisesRegex(ValueError,
                                        "memory_source_run_not_eligible"):
                self.build(self._conn(provenance=False), **self.draft_command())
        with self.subTest(domain="unknown"):
            with self.assertRaisesRegex(ValueError,
                                        "memory_source_run_not_eligible"):
                self.build(self._conn(domain="not_a_registered_domain"),
                           **self.draft_command())

    def test_run_without_clean_shop_scope_is_not_eligible(self):
        from tests.fakeconn import S1_REF

        for shop_refs in ([], ["invalid_shop"], ["S1"], [S1_REF, "invalid_shop"]):
            with self.subTest(shop_refs=str(shop_refs)):
                conn = self._conn(shop_refs=shop_refs)
                with self.assertRaisesRegex(ValueError,
                                            "memory_source_run_not_eligible"):
                    self.build(conn, **self.draft_command())

    def test_unapproved_request_field_is_rejected(self):
        conn = self._conn(request_extra={"top_n": 5})
        with self.assertRaisesRegex(ValueError, "memory_request_field_unapproved"):
            self.build(conn, **self.draft_command())

    def test_invented_kebab_ref_is_rejected_but_registered_ref_passes(self):
        invented = self._conn(request_extra={
            "requested_metric_refs": ["metric-cost-total", "metric-never-registered"]})
        with self.assertRaisesRegex(ValueError,
                                    "memory_value_not_a_stable_ref_or_code"):
            self.build(invented, **self.draft_command())
        registered = self._conn(request_extra={
            "requested_metric_refs": ["metric-cost-total", "view-shop-daily"]})
        self.build(registered, **self.draft_command())

    # ---- 草稿内容：脱敏、授权域、工具与确定性签名 ---------------------------

    def test_draft_is_sanitized_and_scoped_from_the_run_itself(self):
        from bi_agent.query_memory.models import StoredMemoryRecord
        from bi_agent.semantic_catalog.registry import CATALOG
        from tests.fakeconn import S1_REF

        record = self.build(self._conn(), **self.draft_command())
        self.assertIsInstance(record, StoredMemoryRecord)
        self.assertEqual(record.example_ref, "mem-" + self.RUN_ID.hex)
        self.assertEqual(record.status, "draft")
        self.assertEqual(record.approval_revision, 0)
        self.assertEqual(record.source_run_id, self.RUN_ID)
        self.assertEqual(record.domain, "business_query")
        self.assertEqual(record.expected_tool, "query_business")
        # 一次性业务值剥离；授权域只来自该运行自己的 shop_refs。
        self.assertEqual(record.normalized_request, {"metrics": ["paid_amount"]})
        for stripped in ("start", "end", "shop_refs"):
            self.assertNotIn(stripped, record.normalized_request)
        self.assertEqual(record.authorization_refs, (S1_REF,))
        self.assertEqual(
            record.version_requirements.semantic_catalog_version, CATALOG.version)
        self.assertEqual(record.version_requirements.data_catalog_version, 7)

    def test_intent_signature_is_deterministic_and_readable(self):
        first = self.build(self._conn(), **self.draft_command())
        second = self.build(self._conn(
            request_extra={"metrics": ["paid_amount"], "sales_basis": "verified_payment"}),
            **self.draft_command())
        self.assertEqual(first.intent_signature, "paid-amount")
        self.assertEqual(second.intent_signature, "paid-amount-verified-payment")

    def test_intent_signature_falls_back_to_registered_domain(self):
        conn = self._conn()
        subject, status, domain, request = conn.run_row[:4]
        # 唯一的业务码以数字开头，进不了签名：语义 token 耗尽后回退到领域 kebab。
        conn.run_row = (subject, status, domain,
                        {"shop_refs": list(request["shop_refs"]),
                         "platforms": ["1688"]},
                        *conn.run_row[4:])
        record = self.build(conn, **self.draft_command())
        self.assertEqual(record.intent_signature, "business-query")

    def test_commerce_report_kind_selects_the_tool(self):
        cases = (("product", "analyze_product_performance"),
                 ("comparison", "compare_performance"))
        for kind, tool in cases:
            with self.subTest(report_kind=kind):
                record = self.build(self._conn(domain="commerce_performance",
                                               request_extra={"report_kind": kind}),
                                    **self.draft_command())
                self.assertEqual(record.expected_tool, tool)
        conn = self._conn(domain="commerce_performance")
        with self.assertRaisesRegex(ValueError, "memory_source_run_not_eligible"):
            self.build(conn, **self.draft_command())

    def test_tool_map_covers_exactly_the_registered_domains(self):
        from bi_agent.runtime import domain_registry
        from bi_agent.query_memory.repository import _TOOL_FOR_DOMAIN

        self.assertEqual(set(_TOOL_FOR_DOMAIN) | {"commerce_performance"},
                         set(domain_registry.domains()))

    def test_draft_insert_and_drafted_event_share_one_transaction(self):
        conn = self._conn()
        self.build(conn, **self.draft_command())
        self.assertEqual(len(conn.writes), 2)
        depth = conn.writes[0][0]
        self.assertEqual([write[0] for write in conn.writes], [depth, depth])
        self.assertEqual([write[1] for write in conn.writes], ["example", "event"])
        self.assertEqual(len(conn.events), 1)
        event_depth, ref, revision, actor, kind, reason, replacement = conn.events[0]
        self.assertEqual((event_depth, ref, revision, actor, kind, reason, replacement),
                         (depth, "mem-" + self.RUN_ID.hex, 0, "reviewer-a",
                          "drafted", "drafted", None))

    def test_builder_never_reads_chat_or_artifact_tables(self):
        conn = self._conn()
        self.build(conn, **self.draft_command())
        for sql in conn.sql_log:
            self.assertNotIn("chat_messages", sql)
            self.assertNotIn("query_artifacts", sql)

    def test_duplicate_draft_of_same_run_fails_closed(self):
        from tests.fakeconn import example_row

        conn = self._conn()
        conn.examples["mem-" + self.RUN_ID.hex] = example_row(
            "mem-" + self.RUN_ID.hex, run_id=self.RUN_ID)
        with self.assertRaisesRegex(ValueError, "memory_draft_exists"):
            self.build(conn, **self.draft_command())

    def test_slotless_template_is_rejected_before_any_write(self):
        conn = self._conn()
        with self.assertRaisesRegex(ValueError, "memory_slot_missing_from_template"):
            self.build(conn, template="比较店铺的成本")
        self.assertEqual(conn.writes, [])

    # ---- 人工状态机：锁行 + CAS + 恰好一条不可变事件 -------------------------

    def test_approved_cannot_return_to_draft(self):
        repository = self.memory_repository(status="approved", revision=1)
        with self.assertRaisesRegex(ValueError, "memory_transition_invalid"):
            repository.transition(
                "mem-approved-001",
                command=self.command("approve", "cannot approve twice"),
                actor_subject_id="reviewer-a")

    def test_terminal_states_reject_every_command(self):
        for status, action in (("superseded", "approve"), ("revoked", "revoke"),
                               ("revoked", "approve"), ("superseded", "supersede"),
                               ("draft", "supersede")):
            with self.subTest(status=status, action=action):
                repository = self.memory_repository(status=status)
                with self.assertRaisesRegex(ValueError, "memory_transition_invalid"):
                    repository.transition(
                        "mem-approved-001",
                        command=self.command(action, "不可能的迁移", "mem-approved-002")
                        if action == "supersede"
                        else self.command(action, "不可能的迁移"),
                        actor_subject_id="reviewer-a")

    def test_draft_approval_locks_the_row_and_appends_one_event(self):
        repository = self.memory_repository(status="draft", revision=0)
        record = repository.transition(
            "mem-approved-001", command=self.command("approve", "人工复核通过"),
            actor_subject_id="reviewer-a")
        self.assertEqual((record.status, record.approval_revision), ("approved", 1))
        conn = repository.conn
        self.assertTrue(any("FOR UPDATE" in sql for sql in conn.sql_log))
        self.assertEqual(len(conn.events), 1)
        depth, ref, revision, actor, kind, reason, replacement = conn.events[0]
        self.assertEqual((ref, revision, actor, kind, reason, replacement),
                         ("mem-approved-001", 1, "reviewer-a", "approved",
                          "人工复核通过", None))

    def test_approved_record_can_be_revoked_then_stays_terminal(self):
        repository = self.memory_repository(status="approved", revision=1)
        record = repository.transition(
            "mem-approved-001", command=self.command("revoke", "证据失效，立即撤销"),
            actor_subject_id="reviewer-a")
        self.assertEqual((record.status, record.approval_revision), ("revoked", 2))
        with self.assertRaisesRegex(ValueError, "memory_transition_invalid"):
            repository.transition(
                "mem-approved-001", command=self.command("approve", "撤销后不可再批准"),
                actor_subject_id="reviewer-a")

    def test_supersede_requires_approved_same_domain_replacement(self):
        from tests.fakeconn import example_row

        def repository_with(replacement):
            return self.memory_repository(
                status="approved", revision=1,
                extra={"mem-approved-002": replacement})

        good = example_row("mem-approved-002", status="approved", revision=1)
        for name, replacement in (
            ("missing", None),
            ("draft", example_row("mem-approved-002", status="draft")),
            ("cross-domain", example_row("mem-approved-002", status="approved",
                                         revision=1, domain="business_query")),
        ):
            with self.subTest(replacement=name):
                repository = repository_with(replacement)
                with self.assertRaisesRegex(ValueError, "memory_replacement_invalid"):
                    repository.transition(
                        "mem-approved-001",
                        command=self.command("supersede", "版本升级，换用新样例",
                                             "mem-approved-002"),
                        actor_subject_id="reviewer-a")
                self.assertEqual(repository.conn.events, [])
        # 自替换：替换目标指向自身，同样无效。
        repository = self.memory_repository(status="approved", revision=1)
        with self.assertRaisesRegex(ValueError, "memory_replacement_invalid"):
            repository.transition(
                "mem-approved-001",
                command=self.command("supersede", "版本升级，换用新样例",
                                     "mem-approved-001"),
                actor_subject_id="reviewer-a")
        # 合法替换：目标已批准、同领域、非自身。
        repository = repository_with(good)
        record = repository.transition(
            "mem-approved-001",
            command=self.command("supersede", "版本升级，换用新样例", "mem-approved-002"),
            actor_subject_id="reviewer-a")
        self.assertEqual((record.status, record.approval_revision), ("superseded", 2))
        self.assertEqual(len(repository.conn.events), 1)
        self.assertEqual(repository.conn.events[0][6], "mem-approved-002")
        self.assertEqual(repository.conn.events[0][4], "superseded")

    def test_stale_revision_conflicts_without_appending_an_event(self):
        """CAS 命中 0 行：状态与 revision 原样，事件一条不加，审批不重放。"""
        repository = self.memory_repository(status="draft", revision=0)
        repository.conn.cas_fail = True
        with self.assertRaisesRegex(ValueError, "memory_revision_conflict"):
            repository.transition(
                "mem-approved-001", command=self.command("approve", "人工复核通过"),
                actor_subject_id="reviewer-a")
        row = repository.conn.examples["mem-approved-001"]
        self.assertEqual((row["status"], row["approval_revision"]), ("draft", 0))
        self.assertEqual(repository.conn.events, [])

    def test_unknown_example_ref_is_not_found(self):
        repository = self.memory_repository(status="draft")
        with self.assertRaisesRegex(ValueError, "memory_example_not_found"):
            repository.transition(
                "mem-missing", command=self.command("approve", "人工复核通过"),
                actor_subject_id="reviewer-a")


if __name__ == "__main__":
    unittest.main()
