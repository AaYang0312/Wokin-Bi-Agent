"""approved 查询学习记忆的契约（计划 2026-09-14-approved-query-memory.md Task 1）。

本文件只钉形状与净化规则：生命周期运行时属 Task 2，检索属 Task 3。所有断言都落在
稳定原因码（`memory_*` / `replacement_ref_action_mismatch`）上；被拒的输入本身
不允许出现在错误文本里（`hide_input_in_errors`），否则报错就成了第二条泄露通道。
"""

import unittest

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


if __name__ == "__main__":
    unittest.main()
