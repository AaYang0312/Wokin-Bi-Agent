"""审核者 API（计划 2026-09-14-approved-query-memory.md Task 4）。

审核工作流的唯一 HTTP 面：候选列表、草稿创建/列表与 approve/revoke/supersede。
这不是聊天接口，更不是模型 Tool：模型与成功运行永远够不到这里。边界（总设计
§7、§10，计划 Task 4）：

- **门槛**：`require_approver` 先判 feature gate（关闭时所有 query-memory 路由
  一律 404，走既有稳定 envelope），再判 `approver_subjects`（非审核者 403）。
  两个检查都在任何审核连接建立**之前**：被拒的请求一条连接都不建。
- **连接**：审核读写只走 `approver_dsn`（`bi_approver` 身份）的短连接；
  `app_dsn`（bi_app）在本模块一次都不出现——bi_app 对记忆底表没有任何权限
  （迁移 021），这里也不给它开口子。挂载方（bi_agent.api）传入既有
  `get_subject` / `require_web_write`，写请求继续过同一道 WebWrite 边界。
- **投影**：候选只暴露 `source_run_ref/domain/normalized_request/expected_tool/
  version_requirements/created_at`；草稿只暴露计划 DraftProjection 字段加
  `source_run_ref`。owner、授权域、created_by、事件理由、SQL、聊天、结果、
  DSN、错误文本永不进响应（response_model 收口 + tests 钉死字段集）。
- **来源**：`source_run_ref` 固定为 `run-<uuid>` opaque ref。服务端解析后读出
  运行本人的 subject 作为 owner，再交 Task 2 builder 做全部资格复核
  （succeeded、owner、血缘、领域、净化）——来源所有者与审核者（created_by /
  actor）保持分离，创建载荷里连这两个字段都收不到。
- **错误**：repository 的稳定 `memory_*` 错误映射为固定 404/409/422 envelope；
  未登记的错误一律泛化成 500 `internal_error`，不返回数据库异常文本。

（本模块不用 `from __future__ import annotations`：路由函数定义在工厂内部，
FastAPI 只按模块 globals 解析字符串注解，工厂局部别名会被当成 query 参数。）
"""

from collections.abc import Callable, Generator
from datetime import datetime
from typing import Annotated
from uuid import UUID

import psycopg
from fastapi import Depends, FastAPI, HTTPException, Path
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from bi_agent.config import AppSettings
from bi_agent.runtime.domain_registry import known_domain
from bi_agent.runtime.versions import VersionSet

from .models import ApprovalCommand, ApprovalStatus, QuerySlot, StoredMemoryRecord
# Task 2 的资格判定与派生规则是唯一来源：这里复用同一份实现，不抄第二份
# （第二份迟早与 repository 漂移）。它们是模块私有的，恰恰说明不该有第二个调用方
# 形状——候选列表与 builder 看的是同一条资格规则。
from .repository import (
    QueryMemoryRepository,
    _authorization_refs,
    _expected_tool,
    _frozen_versions,
    build_draft_from_run,
)
from .sanitize import sanitize_normalized_request

_RUN_REF_PATTERN = r"^run-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
_EXAMPLE_REF_PATTERN = r"^mem-[a-z0-9-]{1,60}$"

# 候选列表只读安全列：成功状态 + 完整血缘 JOIN + 排除已有任何草稿的运行。
# state / error_code / 聊天 / Artifact / 诊断一个都不出现；硬上限 100。
_CANDIDATES_SQL = """SELECT r.id, r.domain, r.normalized_request, r.started_at,
       p.template_id, p.template_version, p.metric_version, p.schema_version,
       p.catalog_version, p.mapping_version, p.policy_version, p.graph_version,
       p.source_registry_version
FROM bi.query_runs AS r
JOIN bi.query_provenance AS p ON p.run_id = r.id
WHERE r.status = 'succeeded'
  AND NOT EXISTS (
    SELECT 1 FROM bi.approved_query_examples AS m WHERE m.source_run_id = r.id)
ORDER BY r.started_at DESC
LIMIT 100"""

# 草稿列表只读计划 DraftProjection 的安全列（含 source_run_id 仅为派生 opaque
# ref）；owner、授权域、created_by 不进 SELECT，自然也进不了响应。
_DRAFT_LIST_SQL = """SELECT example_ref, source_run_id, domain, intent_signature,
       question_template, slots, normalized_request, expected_tool,
       version_requirements, status, approval_revision
FROM bi.approved_query_examples
ORDER BY example_ref
LIMIT 100"""

# 草稿创建只解析来源运行本人的 subject 作为 owner；资格复核仍由 builder 全量重做。
_OWNER_SQL = "SELECT subject_id FROM bi.query_runs WHERE id = %s"

# repository 稳定错误 → 固定状态与固定话术；话术不含任何数据库或异常文本。
_MEMORY_ERROR_STATUS: dict[str, tuple[int, str]] = {
    "memory_example_not_found": (404, "记忆样例不存在"),
    "memory_draft_exists": (409, "该运行已存在草稿"),
    "memory_transition_invalid": (409, "当前状态不允许该操作"),
    "memory_revision_conflict": (409, "记录已被其他人更新，请刷新后重试"),
    "memory_source_run_not_eligible": (422, "来源运行不符合记忆草稿资格"),
    "memory_replacement_invalid": (422, "替换目标不是同领域的已批准样例"),
}
_MEMORY_ERROR_FALLBACK = (500, "请求无法完成")


class DraftCreate(BaseModel):
    """创建草稿只收三样：来源 opaque ref、槽位化模板、带类型的槽位。

    `extra="forbid"` 把 subject、owner、status、domain、normalized_request、
    版本与授权引用全部挡在请求体之外——这些字段一律由服务端从来源运行推导。
    """

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    source_run_ref: str = Field(pattern=_RUN_REF_PATTERN)
    question_template: str = Field(min_length=1, max_length=400)
    slots: tuple[QuerySlot, ...]


class ReviewReason(BaseModel):
    """审核决定的理由：必须是非空白的真人理由（与 ApprovalCommand 同界）。"""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    reason: str = Field(min_length=3, max_length=500)

    @field_validator("reason")
    @classmethod
    def _nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("reason_blank")
        return value


class ApproveBody(ReviewReason):
    """approve/revoke 的请求体：只有 reason，连 replacement_ref 都不收。"""


class SupersedeBody(ReviewReason):
    """supersede 的请求体：必须指名要替换成的样例。"""

    replacement_ref: str = Field(pattern=_EXAMPLE_REF_PATTERN)


class CandidateProjection(BaseModel):
    """候选来源的安全投影：净化后的 value-free 请求，绝无聊天/SQL/结果。"""

    model_config = ConfigDict(extra="forbid")

    source_run_ref: str
    domain: str
    normalized_request: dict[str, object]
    expected_tool: str
    version_requirements: VersionSet
    created_at: datetime


class DraftProjection(BaseModel):
    """计划 Task 4 的 DraftProjection + source_run_ref。

    owner_subject_id、authorization_refs、created_by、事件理由、SQL、结果在这层
    就不存在——响应模型收口，而不是靠调用方自觉。
    """

    model_config = ConfigDict(extra="forbid")

    example_ref: str
    domain: str
    intent_signature: str
    question_template: str
    slots: tuple[QuerySlot, ...]
    normalized_request: dict[str, object]
    expected_tool: str
    version_requirements: VersionSet
    status: ApprovalStatus
    approval_revision: int
    source_run_ref: str


def _memory_http_error(code: str) -> HTTPException:
    """稳定 `memory_*` 错误码 → 固定 404/409/422 envelope；未知码不外泄文本。"""
    status, message = _MEMORY_ERROR_STATUS.get(code, _MEMORY_ERROR_FALLBACK)
    public_code = code if code in _MEMORY_ERROR_STATUS else "internal_error"
    return HTTPException(status, detail={"code": public_code, "message": message})


def _draft_projection(record: StoredMemoryRecord) -> DraftProjection:
    """严格契约记录 → 安全投影；source_run_id 只以 `run-<uuid>` 形状离开服务端。"""
    return DraftProjection(
        example_ref=record.example_ref, domain=record.domain,
        intent_signature=record.intent_signature,
        question_template=record.question_template, slots=record.slots,
        normalized_request=record.normalized_request,
        expected_tool=record.expected_tool,
        version_requirements=record.version_requirements, status=record.status,
        approval_revision=record.approval_revision,
        source_run_ref=f"run-{record.source_run_id}")


def _draft_from_row(row: tuple) -> DraftProjection:
    """列表行 → 安全投影；列里根本没有 owner/授权/created_by 可泄露。"""
    (example_ref, source_run_id, domain, intent_signature, question_template,
     slots, normalized_request, expected_tool, version_requirements, status,
     approval_revision) = row
    return DraftProjection(
        example_ref=example_ref, domain=domain, intent_signature=intent_signature,
        question_template=question_template,
        slots=tuple(QuerySlot(**slot) for slot in slots),
        normalized_request=normalized_request, expected_tool=expected_tool,
        version_requirements=VersionSet(**version_requirements), status=status,
        approval_revision=approval_revision,
        source_run_ref=f"run-{source_run_id}")


def _candidate_from_row(row: tuple) -> CandidateProjection:
    """一行候选 → 安全投影；逐条应用 Task 2 的资格与净化规则。

    任何一步不合格都以异常浮出，由调用方整条跳过（fail-closed）：不能净化或
    派生工具的运行永远成不了草稿，就不该出现在候选里。
    """
    run_id, domain, request, started_at = row[0], row[1], row[2], row[3]
    versions = _frozen_versions(row[4:13])
    if versions is None or not known_domain(domain) or not isinstance(request, dict):
        raise ValueError("memory_source_run_not_eligible")
    if _authorization_refs(request) is None:
        raise ValueError("memory_source_run_not_eligible")
    clean = sanitize_normalized_request(request, slots=())
    tool = _expected_tool(domain, clean)
    if tool is None:
        raise ValueError("memory_source_run_not_eligible")
    return CandidateProjection(
        source_run_ref=f"run-{run_id}", domain=domain, normalized_request=clean,
        expected_tool=tool, version_requirements=versions, created_at=started_at)


def mount_reviewer_api(app: FastAPI, settings: AppSettings, *,
                       get_subject: Callable[..., str],
                       require_web_write: Callable[..., None]) -> None:
    """把审核路由挂到既有 app 工厂上，复用同一套身份与 WebWrite 依赖。

    路由常驻、门槛在依赖里判：feature gate 关闭时所有 query-memory 路由返回
    404（既有稳定 envelope），而不是消失成 FastAPI 默认的裸 404。依赖顺序是
    刻意的：身份 → 门槛 → WebWrite → 审核连接，被拒的请求永远不建连接。
    """
    Subject = Annotated[str, Depends(get_subject)]
    WebWrite = Annotated[None, Depends(require_web_write)]

    def require_approver(subject: Subject) -> str:
        if not settings.approved_query_memory_enabled:
            raise HTTPException(404, detail={"code": "not_found", "message": "功能未启用"})
        if subject not in settings.approver_subjects:
            raise HTTPException(403, detail={"code": "forbidden", "message": "无审核权限"})
        return subject

    def get_approver_conn() -> Generator[psycopg.Connection, None, None]:
        if settings.approver_dsn is None:
            # 没有独立审核身份就绝不开连接：既不回退 bi_app，也不静默放行。
            raise HTTPException(404, detail={"code": "not_found", "message": "功能未启用"})
        with psycopg.connect(settings.approver_dsn.get_secret_value(), autocommit=True) as conn:
            yield conn

    Reviewer = Annotated[str, Depends(require_approver)]
    ApproverConn = Annotated[psycopg.Connection, Depends(get_approver_conn)]
    ExampleRef = Annotated[str, Path(pattern=_EXAMPLE_REF_PATTERN)]

    @app.get("/api/query-memory/candidates", response_model=list[CandidateProjection])
    def get_candidates(reviewer: Reviewer, conn: ApproverConn) -> list[CandidateProjection]:
        candidates: list[CandidateProjection] = []
        for row in conn.execute(_CANDIDATES_SQL).fetchall():
            try:
                projection = _candidate_from_row(row)
            except Exception:
                # fail-closed：不合格的候选整条跳过，原因文本不外泄。宽捕获是
                # 刻意的——坏行的形状无法枚举，唯一不能做的是把它递给上层。
                projection = None
            if projection is not None:
                candidates.append(projection)
        return candidates

    @app.get("/api/query-memory/drafts", response_model=list[DraftProjection])
    def get_drafts(reviewer: Reviewer, conn: ApproverConn) -> list[DraftProjection]:
        drafts: list[DraftProjection] = []
        for row in conn.execute(_DRAFT_LIST_SQL).fetchall():
            try:
                drafts.append(_draft_from_row(row))
            except Exception:
                continue
        return drafts

    @app.post("/api/query-memory/drafts", status_code=201, response_model=DraftProjection)
    def post_drafts(body: DraftCreate, reviewer: Reviewer, _: WebWrite,
                    conn: ApproverConn) -> DraftProjection:
        # opaque ref 只在这里解析成 UUID；响应里永远只有 run-<uuid> 形状。
        run_id = UUID(body.source_run_ref[4:])
        owner = conn.execute(_OWNER_SQL, (run_id,)).fetchone()
        if owner is None:
            # 不区分「不存在」与「不合格」：对外的稳定原因只有一个。
            raise _memory_http_error("memory_source_run_not_eligible")
        try:
            record = build_draft_from_run(
                conn, run_id=run_id, owner_subject_id=owner[0],
                question_template=body.question_template, slots=body.slots,
                created_by=reviewer)
        except ValidationError:
            raise HTTPException(422, detail={
                "code": "invalid_draft", "message": "问题模板或槽位不符合记忆契约"})
        except ValueError as error:
            raise _memory_http_error(str(error))
        return _draft_projection(record)

    def _decide(example_ref: str, action: str, body: ReviewReason,
                reviewer: str, conn: psycopg.Connection) -> DraftProjection:
        replacement = getattr(body, "replacement_ref", None)
        try:
            command = ApprovalCommand(action=action, reason=body.reason,
                                      replacement_ref=replacement)
        except ValidationError:
            raise HTTPException(422, detail={
                "code": "invalid_review_command", "message": "审核决定不合法"})
        try:
            record = QueryMemoryRepository(conn).transition(
                example_ref, command=command, actor_subject_id=reviewer)
        except ValueError as error:
            raise _memory_http_error(str(error))
        return _draft_projection(record)

    @app.post("/api/query-memory/drafts/{example_ref}/approve",
              response_model=DraftProjection)
    def post_approve(example_ref: ExampleRef, body: ApproveBody, reviewer: Reviewer,
                     _: WebWrite, conn: ApproverConn) -> DraftProjection:
        return _decide(example_ref, "approve", body, reviewer, conn)

    @app.post("/api/query-memory/drafts/{example_ref}/revoke",
              response_model=DraftProjection)
    def post_revoke(example_ref: ExampleRef, body: ApproveBody, reviewer: Reviewer,
                    _: WebWrite, conn: ApproverConn) -> DraftProjection:
        return _decide(example_ref, "revoke", body, reviewer, conn)

    @app.post("/api/query-memory/drafts/{example_ref}/supersede",
              response_model=DraftProjection)
    def post_supersede(example_ref: ExampleRef, body: SupersedeBody, reviewer: Reviewer,
                       _: WebWrite, conn: ApproverConn) -> DraftProjection:
        return _decide(example_ref, "supersede", body, reviewer, conn)
