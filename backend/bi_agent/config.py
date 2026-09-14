"""分别加载应用、同步、模型配置。

各加载器只取所属字段，不建立配置注册中心；
Pydantic ``SecretStr`` 隐藏 DSN 和密钥，``repr`` 不泄露凭证。
"""

from __future__ import annotations

from typing import Literal, Mapping

from pydantic import BaseModel, ConfigDict, SecretStr

Environment = Literal["development", "production"]


class AppSettings(BaseModel):
    """聊天 API 配置：只读报表并读写所属会话。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    app_dsn: SecretStr
    shop_ids: frozenset[str]
    environment: Environment
    allowed_subjects: frozenset[str]
    public_origin: str
    auth_subject_header: str
    # 语义目录启动预检默认关：关掉时启动与聊天行为必须与 Task 1-3 版本一致（计划 Task 4）。
    semantic_catalog_enabled: bool = False
    # 受控 SQL 探索默认关：关闭时 Tool 列表、数据库读与聊天结果必须与当前版本一致。
    # 它只能建在已发布的语义目录之上，依赖关系在 `load_app_settings()` 里判。
    controlled_sql_enabled: bool = False


class SyncSettings(BaseModel):
    """同步命令配置：写入身份与快麦凭证。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    writer_dsn: SecretStr
    shop_ids: frozenset[str]
    app_key: SecretStr
    app_secret: SecretStr
    access_token: SecretStr
    refresh_token: SecretStr


class ModelSettings(BaseModel):
    """模型配置：只使用所选 provider 的密钥。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: Literal["qwen", "deepseek"]
    model: str
    api_key: SecretStr
    base_url: str | None = None


def _required(env: Mapping[str, str], key: str) -> str:
    value = (env.get(key) or "").strip()
    if not value:
        raise ValueError(f"缺少 {key}")
    return value


def _shop_ids(env: Mapping[str, str]) -> frozenset[str]:
    raw = (env.get("BI_SHOP_IDS") or "").strip()
    if not raw:
        raise ValueError("缺少 BI_SHOP_IDS")
    shops = frozenset(part.strip() for part in raw.split(",") if part.strip())
    if not shops:
        raise ValueError("BI_SHOP_IDS 不能为空集合")
    return shops


def _flag(env: Mapping[str, str], key: str) -> bool:
    """feature gate 只认 `true` / `false`：缺省与空白都是“关”。

    不走 Pydantic 的布尔强转也不写 `bool(value)`：`"1"` / `"yes"` / `"True"` 这种
    “看起来像真”的文本要么静默翻转门禁，要么静默保持关——两者都比直接报错更糟，
    因为部署方会以为自己在开（或以为自己在关）。
    """
    raw = (env.get(key) or "").strip()
    if not raw:
        return False
    if raw == "true":
        return True
    if raw == "false":
        return False
    raise ValueError(f"{key} 只能是 true 或 false")


def load_app_settings(env: Mapping[str, str]) -> AppSettings:
    """加载聊天 API 配置；API 环境不得包含同步凭证。"""
    environment = (_required(env, "APP_ENV") or "development").strip()
    if environment not in ("development", "production"):
        raise ValueError("APP_ENV 只能是 development 或 production")
    allowed = frozenset(
        part.strip()
        for part in (env.get("APP_ALLOWED_SUBJECTS") or "").split(",")
        if part.strip()
    )
    if environment == "production" and not allowed:
        raise ValueError("生产环境必须配置 APP_ALLOWED_SUBJECTS")
    public_origin = _required(env, "APP_PUBLIC_ORIGIN").rstrip("/")
    if environment == "production" and not public_origin.startswith("https://"):
        raise ValueError("生产环境 APP_PUBLIC_ORIGIN 必须为 HTTPS")
    if not public_origin.startswith(("http://", "https://")):
        raise ValueError("APP_PUBLIC_ORIGIN 必须为 HTTP(S) origin")
    forbidden = ("BI_WRITER_DSN", "KUAI_MAI_APP_KEY", "KUAI_MAI_APP_SECRET",
                 "KUAI_MAI_ACCESS_TOKEN", "KUAI_MAI_REFRESH_TOKEN")
    if any((env.get(key) or "").strip() for key in forbidden):
        raise ValueError("聊天 API 环境不得包含同步凭证")
    # 先验完原有必填项再判门禁：缺 DSN 的报错顺位不能因为新门禁而后移。
    app_dsn = SecretStr(_required(env, "BI_APP_DSN"))
    shop_ids = _shop_ids(env)
    semantic_catalog_enabled = _flag(env, "SEMANTIC_CATALOG_ENABLED")
    controlled_sql_enabled = _flag(env, "CONTROLLED_SQL_ENABLED")
    if controlled_sql_enabled and not semantic_catalog_enabled:
        # 探索层的 SQL 标识符只能从语义目录的稳定 ref 解析：目录关着就没有解析路径，
        # 只能当场失败，不能“退化成不带目录的 SQL”（总设计 §6.1、计划 Task 1）。
        raise ValueError("CONTROLLED_SQL_REQUIRES_SEMANTIC_CATALOG")
    return AppSettings(
        app_dsn=app_dsn,
        shop_ids=shop_ids,
        environment=environment,  # type: ignore[arg-type]
        allowed_subjects=allowed,
        public_origin=public_origin,
        auth_subject_header=(env.get("AUTH_SUBJECT_HEADER") or "X-Auth-Request-Sub").strip(),
        semantic_catalog_enabled=semantic_catalog_enabled,
        controlled_sql_enabled=controlled_sql_enabled,
    )


def load_sync_settings(env: Mapping[str, str]) -> SyncSettings:
    """加载同步配置：写入 DSN 与快麦四个凭证字段。"""
    return SyncSettings(
        writer_dsn=SecretStr(_required(env, "BI_WRITER_DSN")),
        shop_ids=_shop_ids(env),
        app_key=SecretStr(_required(env, "KUAI_MAI_APP_KEY")),
        app_secret=SecretStr(_required(env, "KUAI_MAI_APP_SECRET")),
        access_token=SecretStr(_required(env, "KUAI_MAI_ACCESS_TOKEN")),
        refresh_token=SecretStr(_required(env, "KUAI_MAI_REFRESH_TOKEN")),
    )


def load_model_settings(env: Mapping[str, str]) -> ModelSettings:
    """加载模型配置；只读取所选 provider 自己的密钥。"""
    provider = _required(env, "LLM_PROVIDER")
    key_name = {"qwen": "QWEN_API_KEY", "deepseek": "DEEPSEEK_API_KEY"}.get(provider)
    if key_name is None:
        raise ValueError("不支持的模型 provider")
    api_key = (env.get(key_name) or "").strip()
    if not api_key:
        raise ValueError(f"缺少 {key_name}")
    model = _required(env, "LLM_MODEL")
    base_url_raw = (env.get("LLM_BASE_URL") or "").strip() or None
    if base_url_raw is not None and not base_url_raw.lower().startswith("https://"):
        raise ValueError("LLM_BASE_URL 只允许 HTTPS 地址")
    return ModelSettings(
        provider=provider,  # type: ignore[arg-type]
        model=model,
        api_key=SecretStr(api_key),
        base_url=base_url_raw,
    )
