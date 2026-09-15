"""人工审核生命周期（计划 2026-09-14-approved-query-memory.md Task 2）。

草稿的唯一入口与 `draft → approved → superseded/revoked` 的唯一状态机。写入边界
（总设计 §7、§10）：

- `build_draft_from_run` 只做一次资格读取（`bi.query_runs` LEFT JOIN
  `bi.query_provenance`），核成功状态、精确 owner、已登记领域与完整血缘；
  `bi.chat_messages` 与 `bi.query_artifacts`（含 payload）永远不出现在本模块
  的 SQL 里，由 tests.fakeconn.QueryMemoryConn 的拒绝性替身钉住。
- 草稿插入与 `drafted` 事件同一事务；状态迁移在一个事务里锁行
  （SELECT … FOR UPDATE）、按当前 revision 做 CAS、并追加恰好一条不可变事件。
  任何冲突或非法迁移都失败收场，绝不重放审批。
- 身份与授权：调用方（Task 4 的审核 API，经独立审核 DSN）先做过审核者判定；
  本模块把 actor 原样记进事件，不做第二次鉴权。

字段推导（父级批准的决定）：`example_ref = mem-<run_id.hex>` 确定性派生——同一
运行终生最多一份草稿，重复构建撞主键失败收场；`intent_signature` 从净化后的
请求确定性派生（词项化、排序、整 token、上限 80，语义 token 耗尽回退领域
kebab）；`expected_tool` 由领域（commerce 再按 report_kind）查固定映射；
`authorization_refs` 只取该运行自己的 shop_refs；`VersionSet` 冻结血缘版本，
语义目录版本取当前 CATALOG.version（血缘表没有该列，这是父级批准的本地开发
桥接，不改 schema）。
"""

from __future__ import annotations

import re
from typing import Any
from uuid import UUID

from psycopg import errors
from psycopg.types.json import Jsonb
from pydantic import ValidationError

from bi_agent.catalog import REF_RE
from bi_agent.runtime.domain_registry import known_domain
from bi_agent.runtime.versions import VersionSet
from bi_agent.semantic_catalog.registry import CATALOG

from .models import ApprovalCommand, QuerySlot, StoredMemoryRecord
from .sanitize import ALLOWED_REQUEST_KEYS, sanitize_normalized_request

# 计划 Task 2 Step 4 的状态机：撤销与替换不可逆，重新启用必须新建草稿。
ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    "draft": frozenset({"approved", "revoked"}),
    "approved": frozenset({"superseded", "revoked"}),
    "superseded": frozenset(),
    "revoked": frozenset(),
}

_NEXT_STATUS = {"approve": "approved", "revoke": "revoked", "supersede": "superseded"}


def next_status(command: ApprovalCommand) -> str:
    return _NEXT_STATUS[command.action]


# 领域 → 工具的固定映射：与 agent.py 的路由一一对应（tests 钉映射恰好覆盖
# domain_registry 的全部领域）。commerce 的两个 Tool 由 report_kind 二选一，
# 缺失或未知都不是合格来源。
_TOOL_FOR_DOMAIN: dict[str, str] = {
    "business_query": "query_business",
    "listing_price_audit": "audit_listing_prices",
    "inventory_watch": "inspect_inventory",
    "controlled_sql_exploration": "explore_business_data",
}
_PRODUCT_REPORT_TOOL = "analyze_product_performance"
_COMPARISON_REPORT_TOOL = "compare_performance"

_DRAFTED_REASON = "drafted"
_MEMORY_REF_MAX_CHARS = 80
_SIGNATURE_TOKEN_RE = re.compile(r"^[a-z][a-z0-9-]*$")

_ELIGIBILITY_SQL = """SELECT r.subject_id, r.status, r.domain, r.normalized_request,
       p.template_id, p.template_version, p.metric_version, p.schema_version,
       p.catalog_version, p.mapping_version, p.policy_version, p.graph_version,
       p.source_registry_version
FROM bi.query_runs AS r
LEFT JOIN bi.query_provenance AS p ON p.run_id = r.id
WHERE r.id = %s"""

_EXAMPLE_COLUMNS = """example_ref, source_run_id, owner_subject_id, domain,
       intent_signature, question_template, slots, normalized_request,
       expected_tool, version_requirements, authorization_refs, status,
       approval_revision"""

_SELECT_FOR_UPDATE_SQL = f"""SELECT {_EXAMPLE_COLUMNS}
FROM bi.approved_query_examples
WHERE example_ref = %s
FOR UPDATE"""

_REPLACEMENT_SQL = ("SELECT domain, status FROM bi.approved_query_examples "
                    "WHERE example_ref = %s")

_CAS_SQL = """UPDATE bi.approved_query_examples
SET status = %s, approval_revision = %s, updated_at = now()
WHERE example_ref = %s AND approval_revision = %s
RETURNING approval_revision"""

_INSERT_EXAMPLE_SQL = """INSERT INTO bi.approved_query_examples (
       example_ref, source_run_id, owner_subject_id, domain, intent_signature,
       question_template, slots, normalized_request, expected_tool,
       version_requirements, authorization_refs, status, approval_revision,
       created_by)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'draft', 0, %s)"""

_INSERT_EVENT_SQL = """INSERT INTO bi.approved_query_events (
       example_ref, revision, actor_subject_id, event_kind, reason,
       replacement_ref)
VALUES (%s, %s, %s, %s, %s, %s)"""


def _expected_tool(domain: str, clean: dict[str, object]) -> str | None:
    """来源运行所属领域的固定 Tool；commerce 按 report_kind 二选一。"""
    if domain == "commerce_performance":
        kind = clean.get("report_kind")
        if kind == "product":
            return _PRODUCT_REPORT_TOOL
        if kind == "comparison":
            return _COMPARISON_REPORT_TOOL
        return None
    return _TOOL_FOR_DOMAIN.get(domain)


def _authorization_refs(request: dict[str, object]) -> tuple[str, ...] | None:
    """授权域只来自该运行自己的 shop_refs；任何无效项整条拒收，绝不静默丢弃。"""
    refs = request.get("shop_refs")
    if not isinstance(refs, (list, tuple)) or not refs:
        return None
    for item in refs:
        if not isinstance(item, str) or not REF_RE.fullmatch(item):
            return None          # 含 invalid_shop 哨兵或裸主键的运行不是合格来源
    return tuple(sorted(set(refs)))


def _signature_tokens(value: object) -> list[str]:
    """把请求值里的字符串全部收出来：集合递归展开，字典按键排序后取值。"""
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple)):
        return [token for item in value for token in _signature_tokens(item)]
    if isinstance(value, dict):
        return [token for key in sorted(value)
                for token in _signature_tokens(value[key])]
    return []


def _intent_signature(clean: dict[str, object], domain: str) -> str:
    """确定性词项签名：白名单键序 + 键内排序，整 token 拼接，上限 80 字符。

    只用净化后的请求（已全部是登记引用与业务码），不掺模板或来源信息；签名
    不要求唯一。以数字开头的业务码进不了签名；语义 token 耗尽时回退到已登记
    领域的 kebab 形状，保证检索侧永远拿到合法的排序键。
    """
    tokens: list[str] = []
    for key in ALLOWED_REQUEST_KEYS:
        if key not in clean:
            continue
        for raw in sorted(set(_signature_tokens(clean[key]))):
            token = raw.replace("_", "-").lower()
            if _SIGNATURE_TOKEN_RE.fullmatch(token) and token not in tokens:
                tokens.append(token)
    signature = ""
    for token in tokens:
        candidate = token if not signature else f"{signature}-{token}"
        if len(candidate) > _MEMORY_REF_MAX_CHARS:
            break               # 只拼完整 token，绝不从中间截断
        signature = candidate
    return signature or domain.replace("_", "-")


def _frozen_versions(provenance: tuple) -> VersionSet | None:
    """从血缘行冻结 VersionSet；任一标识缺失/带空白或目录版本非法都算不完整。"""
    (template_id, template_version, metric_version, schema_version,
     catalog_version, mapping_version, policy_version, graph_version,
     source_registry_version) = provenance
    identifiers = (template_id, template_version, metric_version, schema_version,
                   mapping_version, policy_version, graph_version,
                   source_registry_version)
    if any(not isinstance(item, str) or not item or item != item.strip()
           for item in identifiers):
        return None
    if not isinstance(catalog_version, int) or isinstance(catalog_version, bool) \
            or catalog_version < 0:
        return None
    try:
        # semantic_catalog_version：血缘表没有这一列，冻结当前已发布目录版本
        # （引用登记与版本冻结同源，父级批准的本地开发桥接）。
        return VersionSet(
            schema_version=schema_version,
            semantic_catalog_version=CATALOG.version,
            data_catalog_version=catalog_version,
            metric_version=metric_version, policy_version=policy_version,
            source_registry_version=source_registry_version,
            graph_version=graph_version)
    except ValidationError:
        return None


def _require_approvable_shape(record: StoredMemoryRecord) -> None:
    """草稿必须是可批准的形状：槽位齐整，否则审核阶段（as_approved）必然卡死。"""
    if len({slot.name for slot in record.slots}) != len(record.slots):
        raise ValueError("memory_slot_duplicate")
    if any("{" + slot.name + "}" not in record.question_template
           for slot in record.slots):
        raise ValueError("memory_slot_missing_from_template")


def build_draft_from_run(conn: Any, *, run_id: UUID, owner_subject_id: str,
                         question_template: str, slots: tuple[QuerySlot, ...],
                         created_by: str) -> StoredMemoryRecord:
    """把一次合格的成功运行构建成 draft，并与其 `drafted` 事件同事务落库。

    资格判定全部落在一次读取里：succeeded、subject 精确等于 owner、领域已登记、
    血缘行存在且完整。任何不满足都以 `memory_source_run_not_eligible` 失败收场。
    """
    slots = tuple(slots)
    with conn.transaction():
        row = conn.execute(_ELIGIBILITY_SQL, (run_id,)).fetchone()
        if row is None:
            raise ValueError("memory_source_run_not_eligible")
        subject, status, domain, request = row[0], row[1], row[2], row[3]
        versions = _frozen_versions(row[4:13])
        if (status != "succeeded" or subject != owner_subject_id
                or versions is None or not known_domain(domain)
                or not isinstance(request, dict)):
            raise ValueError("memory_source_run_not_eligible")
        authorization_refs = _authorization_refs(request)
        if authorization_refs is None:
            raise ValueError("memory_source_run_not_eligible")
        clean = sanitize_normalized_request(request, slots=slots)
        tool = _expected_tool(domain, clean)
        if tool is None:
            raise ValueError("memory_source_run_not_eligible")
        record = StoredMemoryRecord(
            example_ref=f"mem-{run_id.hex}", source_run_id=run_id,
            owner_subject_id=subject, domain=domain,
            intent_signature=_intent_signature(clean, domain),
            question_template=question_template, slots=slots,
            normalized_request=clean, expected_tool=tool,
            version_requirements=versions,
            authorization_refs=authorization_refs,
            status="draft", approval_revision=0)
        _require_approvable_shape(record)
        try:
            conn.execute(_INSERT_EXAMPLE_SQL, (
                record.example_ref, run_id, subject, domain,
                record.intent_signature, question_template,
                Jsonb([slot.model_dump(mode="json") for slot in slots]),
                Jsonb(clean), tool,
                Jsonb(record.version_requirements.model_dump(mode="json")),
                list(authorization_refs), created_by))
        except errors.UniqueViolation as error:
            # 确定性 example_ref：同一运行终生最多一份草稿（含被撤销之后）。
            raise ValueError("memory_draft_exists") from error
        conn.execute(_INSERT_EVENT_SQL, (record.example_ref, 0, created_by,
                                         "drafted", _DRAFTED_REASON, None))
    return record


def _record_from_row(row: tuple) -> StoredMemoryRecord:
    """把锁行读取的一行转成严格契约记录；构造本身就是净化复核。"""
    return StoredMemoryRecord(
        example_ref=row[0], source_run_id=row[1], owner_subject_id=row[2],
        domain=row[3], intent_signature=row[4], question_template=row[5],
        slots=tuple(QuerySlot(**item) for item in row[6]),
        normalized_request=row[7], expected_tool=row[8],
        version_requirements=VersionSet(**row[9]),
        authorization_refs=tuple(row[10]), status=row[11],
        approval_revision=row[12])


class QueryMemoryRepository:
    """人工状态机的唯一写入口：锁行、CAS、恰好一条不可变事件，同一事务。"""

    def __init__(self, conn: Any) -> None:
        self.conn = conn

    def transition(self, example_ref: str, *, command: ApprovalCommand,
                   actor_subject_id: str) -> StoredMemoryRecord:
        if not isinstance(command, ApprovalCommand):
            raise TypeError("approval_command_required")
        target = next_status(command)
        with self.conn.transaction():
            row = self.conn.execute(_SELECT_FOR_UPDATE_SQL,
                                    (example_ref,)).fetchone()
            if row is None:
                raise ValueError("memory_example_not_found")
            record = _record_from_row(row)
            if target not in ALLOWED_TRANSITIONS[record.status]:
                raise ValueError("memory_transition_invalid")
            replacement_ref = None
            if command.action == "supersede":
                replacement = self.conn.execute(
                    _REPLACEMENT_SQL, (command.replacement_ref,)).fetchone()
                if (replacement is None or replacement[1] != "approved"
                        or replacement[0] != record.domain
                        or command.replacement_ref == example_ref):
                    raise ValueError("memory_replacement_invalid")
                replacement_ref = command.replacement_ref
            revision = record.approval_revision + 1
            updated = self.conn.execute(
                _CAS_SQL, (target, revision, example_ref,
                           record.approval_revision)).fetchone()
            if updated is None or updated[0] != revision:
                raise ValueError("memory_revision_conflict")
            self.conn.execute(_INSERT_EVENT_SQL, (
                example_ref, revision, actor_subject_id, target,
                command.reason, replacement_ref))
            # 重新过一遍严格契约再返回：写出去的状态就是读得回来的状态。
            return StoredMemoryRecord(**record.model_dump()
                                      | {"status": target,
                                         "approval_revision": revision})
