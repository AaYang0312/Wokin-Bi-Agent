"""生意参谋手工导出·HTTP 上传面（计划 §3.M4；字节体适配版）。

与计划文本的两处已批准适配（M4 切片落地，主会话决定/批准）：
- 计划写 multipart，实现用**原始字节体**（application/octet-stream）：文件名
  经查询参数（优先）或 ``X-File-Name`` 头传递，服务端消毒；不引入
  python-multipart 依赖；
- 写面授权见 ``sql/031_sycm_upload_write_path.sql``（主会话批准偏差：任务书
  清单 + bi.shops SELECT + bi.sync_state SELECT/INSERT/UPDATE，后者以 RLS
  限定在 manual 来源族）。上传经既有 ``get_conn``（app_dsn=bi_app）执行 M2
  导入器，不另设 DSN——config 纪律本就禁止聊天 API 环境携带同步写 DSN。

边界（全部 fail closed）：

- **gate**：``SYCM_MANUAL_IMPORT_ENABLED`` 关（缺省）时路由 404（既有稳定
  envelope），复用 query_memory 的"路由常驻、依赖里判"模式；依赖顺序刻意：
  身份 → gate → 字节体 WebWrite → 连接，被拒的请求一条连接都不建。
- **写面边界适配字节体**：``X-BI-Agent: web``、``Origin == public_origin``、
  认证主体（development 沿用 local-development 先例）；任一缺失 403 + 稳定
  原因码（web_header_missing / origin_mismatch / unsupported_media_type）。
- **大小上限**：请求体 ≤ ``MAX_FILE_BYTES``（Content-Length 预检 + 读取时
  流式封顶）；超限拒绝并留台账事件。台账 content_sha256 记的是**保留内容**
  的哈希（封顶前缀或零字节），事件 detail 写明 content_scope，不冒充整文件
  内容哈希。
- **处理契约**：响应恰好 {upload_id, status, reason_code, rows_read,
  rows_written, shops}；status/reason_code 用 M2 的集中枚举（ReasonCode /
  'accepted'|'rejected'）。进入处理管线的每次上传（含拒绝）都落
  bi.manual_uploads + manual_upload_events 不可变事件；拒绝路径不写事实表。
- **边界外的请求形状**（401/403/404/422）不产生台账：没有可信主体或没有
  可归属的上传尝试，写一行"匿名拒绝"反而制造审计噪声。
- **写面身份 fail-closed**：gate 开启但数据库写面不可用（031 授权缺席 →
  InsufficientPrivilege）时给稳定 503 码 sycm_upload_write_path_unavailable，
  数据库文本绝不交给客户端。
- **文件名消毒**：长度 ≤255 UTF-8 字节；字符白名单 = Unicode 字母数字
  （含汉字）+ ``-_.``；拒路径分隔符、控制字符、结尾点号与 Windows 保留
  设备名。不合格 422 invalid_filename。文件名只作参考与台账记账，解析与
  店铺映射永远只认内容（计划 §1）。
"""

import hashlib
import shutil
import tempfile
from collections.abc import Callable, Generator
from pathlib import Path
from typing import Annotated, Literal
from uuid import UUID

import psycopg
from fastapi import Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel, ConfigDict

from bi_agent.config import AppSettings

from .sycm_import import (
    MAX_FILE_BYTES,
    ImportOutcome,
    ReasonCode,
    SycmValidationError,
    import_sycm_file,
    record_upload_rejection,
)

UPLOAD_ROUTE = "/api/uploads/sycm"

# 响应词表（M6 收紧）：与 ImportOutcome.status 的两个字面量一致。
UploadStatus = Literal["accepted", "rejected"]

# 文件名消毒（A.1）：长度上限 + 字符白名单 + Windows 保留设备名。
_FILENAME_MAX_BYTES = 255
_FILENAME_EXTRA_CHARS = frozenset("-_.")
_WINDOWS_DEVICE_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL",
     *(f"COM{index}" for index in range(1, 10)),
     *(f"LPT{index}" for index in range(1, 10))})


class SycmUploadResult(BaseModel):
    """上传结果契约（M5 前端按它渲染；键集精确，多一个键都不许）。

    M6 收紧：status 用两词 Literal、reason_code 用 M2 的集中枚举 ``ReasonCode``
    （StrEnum，JSON 序列化仍是原词字符串）——词表外的取值在构造处即被拒，
    响应契约（键集、取值域、形状）与 M4/M5 逐字不变。
    """

    model_config = ConfigDict(extra="forbid")

    upload_id: UUID
    status: UploadStatus
    reason_code: ReasonCode | None
    rows_read: int
    rows_written: int
    shops: list[str]


def sanitize_upload_filename(raw: str | None) -> str | None:
    """消毒客户端文件名；不合格返回 None（路由据此给 422 invalid_filename）。"""
    name = (raw or "").strip()
    if not name or len(name.encode("utf-8")) > _FILENAME_MAX_BYTES:
        return None
    if name.endswith("."):
        return None
    for char in name:
        if char in ("/", "\\"):
            return None
        if ord(char) < 0x20 or ord(char) == 0x7F:
            return None
        if not (char.isalnum() or char in _FILENAME_EXTRA_CHARS):
            return None
    if name.split(".", 1)[0].upper() in _WINDOWS_DEVICE_NAMES:
        return None
    return name


async def _read_capped_body(request: Request) -> tuple[bytes, int | None]:
    """读取请求体并强制 MAX_FILE_BYTES 封顶。

    Content-Length 已超限时一个字节都不读（保留零字节）；声明与实际不符
    （或分块传输）由流式封顶兜底——只保留封顶前的前缀，绝不把未上限的
    请求体读进内存。返回 (保留字节, 超限时实际字节总数或 None)。
    """
    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            size = int(declared)
        except ValueError:
            size = None  # 形状非法的声明交给流式封顶兜底
        if size is not None and size > MAX_FILE_BYTES:
            return b"", size
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > MAX_FILE_BYTES:
            return b"".join(chunks), total
        chunks.append(chunk)
    return b"".join(chunks), None


def _import_uploaded_bytes(conn: psycopg.Connection, filename: str,
                           content: bytes,
                           subject: str) -> ImportOutcome:
    """把上传字节交给 M2 校验管线：临时文件名 = 消毒后文件名，台账一致。

    mkdtemp 的随机目录名 + 已消毒的纯文件名不会逃出目录；用后整目录删除。
    """
    directory = Path(tempfile.mkdtemp(prefix="sycm-upload-"))
    try:
        path = directory / filename
        path.write_bytes(content)
        return import_sycm_file(conn, path, subject_id=subject)
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def mount_sycm_upload_api(app: FastAPI, settings: AppSettings, *,
                          get_subject: Callable[..., str],
                          get_conn: Callable[..., Generator[
                              psycopg.Connection, None, None]]) -> None:
    """把上传路由挂到既有 app 工厂上，复用同一套身份与 bi_app 连接依赖。

    路由常驻、门槛在依赖里判（M4/D5）：feature gate 关闭时返回 404（既有
    稳定 envelope），且依赖顺序保证被拒的请求永远不建连接。
    """
    Subject = Annotated[str, Depends(get_subject)]
    Connection = Annotated[psycopg.Connection, Depends(get_conn)]

    def require_sycm_upload_enabled() -> None:
        if not settings.sycm_manual_import_enabled:
            raise HTTPException(404, detail={
                "code": "not_found", "message": "功能未启用"})

    def require_web_bytes_write(request: Request) -> None:
        """WebWrite 的字节体适配：三件套任一缺失都 403 + 稳定原因码。"""
        content_type = request.headers.get("content-type", "").split(";", 1)[0]
        if request.headers.get("X-BI-Agent") != "web":
            raise HTTPException(403, detail={
                "code": "web_header_missing", "message": "请求来源不可信"})
        if request.headers.get("origin") != settings.public_origin:
            raise HTTPException(403, detail={
                "code": "origin_mismatch", "message": "请求来源不可信"})
        if content_type != "application/octet-stream":
            raise HTTPException(403, detail={
                "code": "unsupported_media_type",
                "message": "上传只接受原始字节体"})

    Gate = Annotated[None, Depends(require_sycm_upload_enabled)]
    WebBytes = Annotated[None, Depends(require_web_bytes_write)]

    @app.post(UPLOAD_ROUTE, response_model=SycmUploadResult)
    async def post_sycm_upload(request: Request, subject: Subject,
                               _gate: Gate, _web: WebBytes,
                               conn: Connection) -> SycmUploadResult:
        filename = sanitize_upload_filename(
            request.query_params.get("filename")
            or request.headers.get("x-file-name"))
        if filename is None:
            raise HTTPException(422, detail={
                "code": "invalid_filename", "message": "文件名不符合上传契约"})
        content, oversized = await _read_capped_body(request)
        try:
            if oversized is not None:
                # 超限拒绝照常落台账；content_sha256 是保留内容哈希，detail
                # 写明 content_scope，绝不冒充整文件哈希。
                outcome = record_upload_rejection(
                    conn, filename=filename,
                    content_sha256=hashlib.sha256(content).hexdigest(),
                    error=SycmValidationError(ReasonCode.PARSE_FAILED, {
                        "detail": "file_too_large", "bytes": oversized,
                        "limit_bytes": MAX_FILE_BYTES,
                        "retained_bytes": len(content),
                        "content_scope": "retained_bytes_only"}),
                    subject_id=subject)
            else:
                outcome = _import_uploaded_bytes(conn, filename, content,
                                                 subject)
        except psycopg.errors.InsufficientPrivilege as error:
            # 写面身份不可用（031 授权缺席等部署失配）：fail-closed 稳定码，
            # 不把数据库异常文本交给客户端。
            raise HTTPException(503, detail={
                "code": "sycm_upload_write_path_unavailable",
                "message": "上传写面未就绪"}) from error
        return SycmUploadResult(
            upload_id=outcome.upload_id, status=outcome.status,
            reason_code=outcome.reason_code, rows_read=outcome.rows_read,
            rows_written=outcome.rows_written, shops=list(outcome.shops))
