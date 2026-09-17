"""应用内通知中心的授权 API（计划 Task 5 Step 4）。

三个路由挂在既有 app 工厂上，复用同一套身份与 WebWrite 依赖（挂载方传入，
与 ``query_memory.api`` 同一挂载协议）：

- ``GET /api/notifications``：当前 subject 的安全投影，最多 100 条。列来自
  023 的 ``reporting.v_inventory_notifications``（owner 过滤在 WHERE 里做，
  owner subject 本身永不进响应）。每一行都重新过严格投影校验，不合格的行
  整行跳过（fail-closed），原因文本不外泄。载荷里只能出现已核验层级的事件：
  缺来源/缺扫描的层级不写 outbox、不进通知表，这里没有为它造卡片的通路。
- ``POST /api/notifications/{notification_ref}/read``：只更新已读时间；owner
  复核在 023 的 ``bi.mark_inventory_notification_read`` 内部重做；重复已读
  幂等 200（保留第一次时间）。
- ``POST /api/inventory-alerts/{alert_ref}/acknowledge``：仅 owner 的 open
  告警可置 acknowledged（acknowledge 不等于 resolved）；已 acknowledged
  幂等 200；resolved/suppressed 返回 409；错 owner 与不存在同为 404，不泄漏
  任何告警的存在性。

没有任何客户端可供给的 subject、SQL 或状态。错误只报固定码与固定话术；
数据库异常原文、DSN 与参数永不进响应。本模块不投递、不轮询、不接外部 sink。
"""

from collections.abc import Callable, Generator
from datetime import datetime
from typing import Annotated, Literal

import psycopg
from fastapi import Depends, FastAPI, HTTPException, Path
from pydantic import BaseModel, ConfigDict, Field, ValidationError

# 与 023 的生成形状一致：notification_ref = 'ntf-' + 去连字符 UUID。
_NOTIFICATION_REF_PATTERN = r"^ntf-[0-9a-z-]{1,60}$"
_ALERT_REF_PATTERN = r"^alert-[a-z0-9-]{1,60}$"

# 数量/阈值文本：与 monitoring.models 的载荷契约同一形状（None = 没读到，不是零）。
_QUANTITY_TEXT_PATTERN = r"^-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?$"

# GET 只读安全列：owner_subject_id 只出现在 WHERE 里，永远不进 SELECT 输出。
# 硬上限 100 与计划的投影预算一致；evidence/完整来源载荷在视图层就不存在。
_NOTIFICATIONS_SQL = """
    SELECT notification_ref, alert_ref, event_kind, status, level, sku_ref,
           scope_ref, reason_code, quantity, threshold, unit, data_as_of,
           read_at, created_at
    FROM reporting.v_inventory_notifications
    WHERE owner_subject_id = %s
    ORDER BY created_at DESC, notification_ref
    LIMIT 100"""

# 两个固定 owner 复核函数：023 已 REVOKE PUBLIC 并只授给 bi_app。
_READ_SQL = "SELECT bi.mark_inventory_notification_read(%s, %s)"
_ACK_SQL = "SELECT bi.acknowledge_inventory_alert(%s, %s)"

# 稳定固定错误：023 函数的固定码 → 固定状态/固定话术；话术不含数据库文本。
_INTERNAL_ERROR = (500, "internal_error", "请求无法完成")
_READ_ERRORS = {
    "inventory_notification_not_found": (404, "not_found", "通知不存在"),
}
_ACK_ERRORS = {
    "inventory_alert_not_found": (404, "not_found", "告警不存在"),
    "inventory_alert_not_acknowledgeable": (409, "not_acknowledgeable",
                                            "当前状态不能确认收到"),
}

_INTERNAL_ENVELOPE = {"code": _INTERNAL_ERROR[1], "message": _INTERNAL_ERROR[2]}


class EmptyBody(BaseModel):
    """写路由的请求体：必须存在且为空对象，任何附带字段都是 422。"""

    model_config = ConfigDict(extra="forbid")


class NotificationProjection(BaseModel):
    """GET /api/notifications 的安全投影：计划 Task 5 Step 4 的十四个字段。

    owner subject、真实 ID、evidence 与完整来源载荷在这层就不存在；``quantity``
    为 null 与数量为 0 是两回事，且 null 永不从另一层级取值。
    """

    model_config = ConfigDict(extra="forbid")

    notification_ref: str = Field(pattern=_NOTIFICATION_REF_PATTERN)
    alert_ref: str = Field(pattern=_ALERT_REF_PATTERN)
    # outbox 只承载 trigger/retrigger/resolved 三种事件（updated 是纯审计事件，
    # 023 不为它写 outbox）：词表收在这里，不收第五个值。
    event_kind: Literal["triggered", "retriggered", "resolved"]
    status: Literal["open", "acknowledged", "resolved", "suppressed"]
    level: Literal["physical_total", "shop_sellable"]
    sku_ref: str = Field(min_length=1)
    scope_ref: str = Field(min_length=1)
    quantity: str | None = Field(default=None, pattern=_QUANTITY_TEXT_PATTERN)
    threshold: str | None = Field(default=None, pattern=_QUANTITY_TEXT_PATTERN)
    unit: str = Field(min_length=1)
    data_as_of: datetime
    reason_code: str = Field(min_length=1)
    read_at: datetime | None
    created_at: datetime


class NotificationReadResult(BaseModel):
    """已读结果的固定形状：重复已读返回同一次时间。"""

    model_config = ConfigDict(extra="forbid")

    notification_ref: str
    read_at: datetime | None


class AcknowledgeResult(BaseModel):
    """确认结果的固定形状：acknowledge 只会到达 acknowledged。"""

    model_config = ConfigDict(extra="forbid")

    alert_ref: str
    status: Literal["acknowledged"]
    acknowledged_at: datetime | None


def _notification_from_row(row: tuple) -> NotificationProjection:
    """一行视图数据 → 严格投影；任何一步不合格都由调用方整行跳过。"""
    (notification_ref, alert_ref, event_kind, status, level, sku_ref, scope_ref,
     reason_code, quantity, threshold, unit, data_as_of, read_at, created_at) = row
    return NotificationProjection(
        notification_ref=notification_ref, alert_ref=alert_ref,
        event_kind=event_kind, status=status, level=level, sku_ref=sku_ref,
        scope_ref=scope_ref, reason_code=reason_code, quantity=quantity,
        threshold=threshold, unit=unit, data_as_of=data_as_of, read_at=read_at,
        created_at=created_at)


def _internal_error() -> HTTPException:
    """未登记的数据库/形状失败：固定 500 envelope，不外泄任何原文。"""
    return HTTPException(_INTERNAL_ERROR[0], detail=dict(_INTERNAL_ENVELOPE))


def _run_owner_function(conn: psycopg.Connection, sql: str, params: tuple,
                        model: type[BaseModel],
                        errors: dict[str, tuple[int, str, str]]):
    """调用一个固定 owner 复核函数并验证其固定结果形状。"""
    try:
        row = conn.execute(sql, params).fetchone()
    except psycopg.errors.Error as error:
        # 与 repository 的稳定错误映射同一纪律：优先读服务端 diag 固定码，
        # 读不到（如测试替身直抛）回退异常文本；两者都不在表内则泛化 500。
        code = getattr(getattr(error, "diag", None), "message_primary", None) \
            or str(error)
        if code in errors:
            status, public_code, message = errors[code]
            raise HTTPException(status, detail={
                "code": public_code, "message": message}) from None
        raise _internal_error() from None
    if row is None:
        raise _internal_error()
    try:
        return model.model_validate(row[0])
    except ValidationError as error:
        raise _internal_error() from error


def mount_notification_api(app: FastAPI, *, get_subject: Callable[..., str],
                           require_web_write: Callable[..., None],
                           get_conn: Callable[..., Generator[
                               psycopg.Connection, None, None]]) -> None:
    """把通知路由挂到既有 app 工厂上，复用既有身份 / WebWrite / bi_app 连接。

    依赖顺序是刻意的：身份 → WebWrite → 连接，被拒的请求永远不建语句；
    subject 只来自既有 ``get_subject``，客户端没有任何可供给的主体或状态。
    """
    Subject = Annotated[str, Depends(get_subject)]
    WebWrite = Annotated[None, Depends(require_web_write)]
    Conn = Annotated[psycopg.Connection, Depends(get_conn)]
    NotificationRef = Annotated[str, Path(pattern=_NOTIFICATION_REF_PATTERN)]
    AlertRef = Annotated[str, Path(pattern=_ALERT_REF_PATTERN)]

    @app.get("/api/notifications", response_model=list[NotificationProjection])
    def get_notifications(subject: Subject,
                          conn: Conn) -> list[NotificationProjection]:
        try:
            rows = conn.execute(_NOTIFICATIONS_SQL, (subject,)).fetchall()
        except psycopg.errors.Error as error:
            raise _internal_error() from error
        projections: list[NotificationProjection] = []
        for row in rows:
            try:
                projections.append(_notification_from_row(row))
            except Exception:  # noqa: BLE001 - 坏行整行跳过，原因文本不外泄
                continue
        return projections

    @app.post("/api/notifications/{notification_ref}/read",
              response_model=NotificationReadResult)
    def post_notification_read(notification_ref: NotificationRef, body: EmptyBody,
                               subject: Subject, _: WebWrite,
                               conn: Conn) -> NotificationReadResult:
        del body   # 请求体必须为空对象：没有任何可供给的状态。
        return _run_owner_function(conn, _READ_SQL, (notification_ref, subject),
                                   NotificationReadResult, _READ_ERRORS)

    @app.post("/api/inventory-alerts/{alert_ref}/acknowledge",
              response_model=AcknowledgeResult)
    def post_acknowledge(alert_ref: AlertRef, body: EmptyBody, subject: Subject,
                         _: WebWrite, conn: Conn) -> AcknowledgeResult:
        del body
        return _run_owner_function(conn, _ACK_SQL, (alert_ref, subject),
                                   AcknowledgeResult, _ACK_ERRORS)
