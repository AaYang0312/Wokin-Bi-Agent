"""快麦开放平台只读客户端：官方参数、签名、HTTP、分页响应和会话续期。

签名与公共参数依据官方公开全文「API调用方法详解」（llms-full.txt，快照
SHA-256 见 docs/superpowers/research/2026-09-06-kuaimai-data-verification.md）。
错误只携带脱敏类别，不携带服务端原文；日志只输出字段白名单。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from datetime import datetime, timedelta
from typing import Any, Mapping
from zoneinfo import ZoneInfo

import httpx
from pydantic import BaseModel

BEIJING = ZoneInfo("Asia/Shanghai")
ROUTER_URL = "https://gw.superboss.cc/router"
VERSION = "1.0"
SIGN_METHOD = "hmac-sha256"
MAX_ATTEMPTS = 3
# 限流单独放宽：淘系宽窗取数每支付日 62+ 次流式请求，429 只给 3 次机会会整窗失败，
# 覆盖缺口直接拒答查询；服务端 Retry-After 在上限内完整尊重（原 5 秒截断等于
# 让限流重试必然失败）。
RATE_LIMIT_MAX_ATTEMPTS = 5
RETRY_AFTER_CAP_SECONDS = 30.0

logger = logging.getLogger(__name__)

_sleep = time.sleep


class KuaimaiError(Exception):
    """脱敏类别错误：authentication/permission/rate_limit/timeout/upstream/invalid_response/unknown_empty。"""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class Page(BaseModel):
    rows: list[dict[str, Any]]
    total: int | None
    has_next: bool | None
    cursor: str | None
    verified_empty: bool


def sign(params: Mapping[str, str], secret: str) -> str:
    """官方签名：键名升序拼接 k+v（排除 sign），HMAC-SHA256 后转大写十六进制。"""
    canonical = "".join(k + params[k] for k in sorted(params) if k != "sign")
    return hmac.new(secret.encode(), canonical.encode(), hashlib.sha256).hexdigest().upper()


def parse_page(payload: dict[str, object], *, allow_omitted_list: bool = False,
               list_key: str = "list") -> Page:
    """校验分页响应形状；省略列表只是接口差异，永远不构成完成证据。

    verified_empty 只认正向完成证据（total=0 或 hasNext=false）。“没有 list 也没有
    任何完成证据”是**不可信空**：网关HTML页、错误信封、字段改名都可能长成这个样子，
    把它们当“确定没记录”发布，整窗口会被标成 covered（C-6）。

    list_key 只改结果列表的键名，不改完成证据规则：交易/售后/店铺三个接口实测返回
    `list`，而 `item.list.query` 实测返回 `items`（2026-09-11 只读核验）。

    allow_omitted_list 仅给实测确实会省略 list 的接口预留（见
    docs/superpowers/research/2026-09-06-kuaimai-data-recheck.json 中
    erp.item.history.cost.price.query / erp.item.sku.list.get /
    stock.api.status.query / erp.item.warehouse.list.get /
    erp.wave.logistics.order.query / erp.aftersale.refund.warehouse.query /
    purchase.order.query 的 unexpected_list_shape → success_no_records）；
    它只能把空页解析成“未核验的空”，不能判为已覆盖。
    """
    if payload.get("success") is False:
        raise KuaimaiError("upstream")
    rows = payload.get(list_key)
    total = payload.get("total")
    # total 只有是整数时才可参与“总数为0”判断（字符串 "0" 不算，bool 不算 int）
    total_count = (total if isinstance(total, int) and not isinstance(total, bool)
                   else None)
    has_next_raw = payload.get("hasNext")
    has_next = has_next_raw if isinstance(has_next_raw, bool) else None
    # 正向完成证据：上游明说“总数为0”或“没有下一页了”。
    completion_evidence = total_count == 0 or has_next is False
    if rows is None:
        if completion_evidence:
            rows = []
        elif allow_omitted_list:
            rows = []          # 已实测省略 list 的接口：当作空页解析，但不算完成证据
        else:
            raise KuaimaiError("unknown_empty" if total_count is None
                               else "invalid_response")
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        raise KuaimaiError("invalid_response")
    cursor_raw = payload.get("cursor")
    cursor = cursor_raw if isinstance(cursor_raw, str) else None
    verified_empty = len(rows) == 0 and completion_evidence
    return Page(
        rows=rows,  # type: ignore[arg-type]
        total=total_count,
        has_next=has_next,
        cursor=cursor,
        verified_empty=verified_empty,
    )


def _classify_error_code(code: object, message: object) -> str:
    text = f"{code} {message}".lower()
    if "token" in text or "session" in text or "授权" in str(message) or "签名" in str(message):
        return "authentication"
    if "权限" in str(message):
        return "permission"
    if "频繁" in str(message) or "限流" in str(message) or "rate" in text:
        return "rate_limit"
    return "upstream"


class KuaimaiClient:
    """一次调用一层：公共参数 + 签名 + 表单 POST + 分页形状校验。"""

    def __init__(self, settings, http: httpx.Client):
        self.settings = settings
        self.http = http

    def call(self, method: str, params: dict[str, str]) -> dict[str, object]:
        secret = self.settings.app_secret.get_secret_value()
        last_error: KuaimaiError | None = None
        upstream_attempts = 0
        rate_limit_attempts = 0
        while True:
            public: dict[str, str] = {
                "appKey": self.settings.app_key.get_secret_value(),
                "session": self.settings.access_token.get_secret_value(),
                "method": method,
                "timestamp": datetime.now(BEIJING).strftime("%Y-%m-%d %H:%M:%S"),
                "version": VERSION,
                "sign_method": SIGN_METHOD,
            }
            public.update(params)
            public["sign"] = sign(public, secret)
            try:
                response = self.http.post(ROUTER_URL, data=public, follow_redirects=False)
            except httpx.TransportError as exc:
                logger.warning("kuaimai transport error attempt=%s", upstream_attempts + 1)
                last_error = KuaimaiError("timeout" if isinstance(exc, httpx.TimeoutException) else "upstream")
                upstream_attempts += 1
                if upstream_attempts >= MAX_ATTEMPTS:
                    raise last_error
                _sleep(2.0 ** (upstream_attempts - 1))
                continue
            if response.status_code == 429:
                logger.warning("kuaimai rate limit attempt=%s", rate_limit_attempts + 1)
                retry_after = response.headers.get("Retry-After")
                delay = RETRY_AFTER_CAP_SECONDS
                if retry_after is not None and retry_after.isdigit():
                    delay = min(float(retry_after), RETRY_AFTER_CAP_SECONDS)
                last_error = KuaimaiError("rate_limit")
                rate_limit_attempts += 1
                if rate_limit_attempts >= RATE_LIMIT_MAX_ATTEMPTS:
                    raise last_error
                _sleep(delay)
                continue
            if response.status_code in (401, 403):
                raise KuaimaiError("authentication")
            if response.status_code >= 500:
                logger.warning("kuaimai server error attempt=%s", upstream_attempts + 1)
                last_error = KuaimaiError("upstream")
                upstream_attempts += 1
                if upstream_attempts >= MAX_ATTEMPTS:
                    raise last_error
                _sleep(2.0 ** (upstream_attempts - 1))
                continue
            if response.status_code != 200:
                raise KuaimaiError("upstream")
            try:
                payload = response.json()
            except (json.JSONDecodeError, ValueError):
                raise KuaimaiError("invalid_response") from None
            if not isinstance(payload, dict):
                raise KuaimaiError("invalid_response")
            if payload.get("success") is False:
                code = _classify_error_code(payload.get("code"), payload.get("message"))
                if code == "rate_limit":
                    logger.warning("kuaimai envelope rate limit attempt=%s",
                                   rate_limit_attempts + 1)
                    last_error = KuaimaiError(code)
                    rate_limit_attempts += 1
                    if rate_limit_attempts >= RATE_LIMIT_MAX_ATTEMPTS:
                        raise last_error
                    _sleep(2.0 ** (rate_limit_attempts - 1))
                    continue
                raise KuaimaiError(code)
            return payload

    def refresh_session(self, *, now: datetime | None = None) -> datetime:
        """续期会话：核对两个 Token 不变，返回已核验单位后的过期时刻。

        普通查询不自动刷新；返回 token 意外变化时停止自动处理。
        """
        payload = self.call("open.token.refresh", {
            "refreshToken": self.settings.refresh_token.get_secret_value(),
        })
        new_token = payload.get("accessToken") or payload.get("sessionKey")
        if isinstance(new_token, str) and new_token != self.settings.access_token.get_secret_value():
            raise KuaimaiError("upstream")
        expire_raw = payload.get("expireTime")
        if not isinstance(expire_raw, str) or not expire_raw.strip():
            raise KuaimaiError("invalid_response")
        try:
            expires = datetime.strptime(expire_raw.strip(), "%Y-%m-%d %H:%M:%S").replace(tzinfo=BEIJING)
        except ValueError:
            raise KuaimaiError("invalid_response") from None
        reference = now or datetime.now(BEIJING)
        if expires <= reference:
            raise KuaimaiError("invalid_response")
        return expires
