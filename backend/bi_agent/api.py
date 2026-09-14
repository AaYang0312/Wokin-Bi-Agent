"""FastAPI 聊天 API：身份边界、会话 CRUD 与健康检查。"""

from collections.abc import Generator
from datetime import datetime
from typing import Annotated
from uuid import UUID
from zoneinfo import ZoneInfo

import psycopg
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict

from .chats import (
    ChatBusy,
    ChatMessage,
    ChatNotFound,
    ChatRename,
    ChatSummary,
    MessageCreate,
    claim_chat_turn,
    create_chat,
    delete_chat,
    list_chats,
    load_messages,
    release_chat_turn,
    rename_chat,
)
from .config import AppSettings
from .agent import encode_sse, run_chat_turn
from .llm import ChatModel
from .semantic_catalog import CATALOG
from .semantic_catalog.schema_check import validate_catalog_schema


class EmptyBody(BaseModel):
    model_config = ConfigDict(extra="forbid")


def create_runtime_app() -> FastAPI:
    """供 Uvicorn 调用的延迟工厂，避免导入时读取部署环境。

    门禁开启时（计划 Task 4）：先用**一条** autocommit 连接核对语义目录声明与真实
    reporting schema，再建模型与 FastAPI app。不一致就让启动失败：带着一份骗人的目录
    起来，比不起来更危险（它会持续把错视图当成合法候选发给下游）。不降级、不重试、
    不把失败当成“目录为空”。
    """
    import os

    from .config import load_app_settings, load_model_settings
    from .llm import create_model

    settings = load_app_settings(os.environ)
    if settings.semantic_catalog_enabled:
        with psycopg.connect(settings.app_dsn.get_secret_value(), autocommit=True) as conn:
            validate_catalog_schema(conn, CATALOG)

    return create_app(settings, create_model(load_model_settings(os.environ)))


def create_app(settings: AppSettings, model: ChatModel | None = None) -> FastAPI:
    app = FastAPI(title="BI Agent", docs_url=None, redoc_url=None)
    app.state.settings = settings

    def get_subject(request: Request) -> str:
        if settings.environment == "development":
            return "local-development"
        subject = (request.headers.get(settings.auth_subject_header) or "").strip()
        if not subject:
            raise HTTPException(401, detail={"code": "unauthenticated", "message": "请先登录"})
        if subject not in settings.allowed_subjects:
            raise HTTPException(403, detail={"code": "forbidden", "message": "无权访问"})
        return subject

    def require_web_write(request: Request) -> None:
        content_type = request.headers.get("content-type", "").split(";", 1)[0]
        if (
            content_type != "application/json"
            or request.headers.get("X-BI-Agent") != "web"
            or request.headers.get("origin") != settings.public_origin
        ):
            raise HTTPException(403, detail={"code": "forbidden", "message": "请求来源不可信"})

    def get_conn() -> Generator[psycopg.Connection, None, None]:
        with psycopg.connect(settings.app_dsn.get_secret_value(), autocommit=True) as conn:
            yield conn

    Subject = Annotated[str, Depends(get_subject)]
    Connection = Annotated[psycopg.Connection, Depends(get_conn)]
    WebWrite = Annotated[None, Depends(require_web_write)]

    @app.exception_handler(ChatNotFound)
    async def chat_not_found(_: Request, __: ChatNotFound) -> JSONResponse:
        return JSONResponse(status_code=404, content={"code": "not_found", "message": "会话不存在"})

    @app.exception_handler(ChatBusy)
    async def chat_busy(_: Request, __: ChatBusy) -> JSONResponse:
        return JSONResponse(status_code=409, content={"code": "chat_busy", "message": "该会话正在回答"})

    @app.exception_handler(HTTPException)
    async def http_error(_: Request, error: HTTPException) -> JSONResponse:
        detail = error.detail if isinstance(error.detail, dict) else {}
        return JSONResponse(
            status_code=error.status_code,
            content={
                "code": detail.get("code", "request_error"),
                "message": detail.get("message", "请求无法完成"),
            },
        )

    @app.get("/api/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/chats", response_model=list[ChatSummary])
    def get_chats(subject: Subject, conn: Connection) -> list[ChatSummary]:
        return list_chats(conn, subject)

    @app.post("/api/chats", status_code=201, response_model=ChatSummary)
    def post_chats(_: EmptyBody, subject: Subject, conn: Connection, __: WebWrite) -> ChatSummary:
        return create_chat(conn, subject)

    @app.get("/api/chats/{chat_id}/messages", response_model=list[ChatMessage])
    def get_messages(chat_id: UUID, subject: Subject, conn: Connection) -> list[ChatMessage]:
        return load_messages(conn, subject, chat_id)

    @app.patch("/api/chats/{chat_id}", response_model=ChatSummary)
    def patch_chat(chat_id: UUID, body: ChatRename, subject: Subject,
                   conn: Connection, _: WebWrite) -> ChatSummary:
        return rename_chat(conn, subject, chat_id, body.title)

    @app.delete("/api/chats/{chat_id}", status_code=204)
    def remove_chat(chat_id: UUID, subject: Subject, conn: Connection,
                    _: WebWrite) -> Response:
        delete_chat(conn, subject, chat_id)
        return Response(status_code=204)

    @app.post("/api/chats/{chat_id}/messages")
    def post_message(chat_id: UUID, body: MessageCreate, subject: Subject,
                     _: WebWrite) -> StreamingResponse:
        if model is None:
            raise HTTPException(503, detail={
                "code": "unavailable", "message": "模型服务尚未配置",
            })
        conn = psycopg.connect(settings.app_dsn.get_secret_value(), autocommit=True)
        try:
            claim_chat_turn(conn, chat_id, subject)
        except Exception:
            conn.close()
            raise

        def events() -> Generator[bytes, None, None]:
            try:
                for event in run_chat_turn(
                    conn, chat_id, subject, body.content, model=model,
                    allowed_shop_ids=settings.shop_ids,
                    now=datetime.now(ZoneInfo("Asia/Shanghai")),
                ):
                    yield encode_sse(event)
            finally:
                try:
                    release_chat_turn(conn, chat_id)
                finally:
                    conn.close()

        return StreamingResponse(
            events(), media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    return app
