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
    # approved 查询记忆默认关：关闭时检索、写路径与审核入口都不存在，聊天行为与
    # 没有这个功能的版本一致。审核者与审核 DSN 只在开启时从环境装入，功能关就是
    # 全关（含配置面）。
    approved_query_memory_enabled: bool = False
    approver_subjects: frozenset[str] = frozenset()
    approver_dsn: SecretStr | None = None
    # 隔离分析（子项目 D）默认关：关闭时不注册 analyze_artifact Tool、不读取
    # Artifact，聊天行为与没有这个功能的版本一致（计划 Task 1 Step 5）。
    # 本切片的门禁开启不需要附加环境项；loader/graph 属后续 Task。
    isolated_analysis_enabled: bool = False


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


class MonitorSettings(BaseModel):
    """持续库存监控（计划 2026-09-14-continuous-inventory-notifications Task 1）的
    独立监控身份：``bi_monitor`` 不与 ``bi_app`` / ``bi_sync`` 共享任何连接。

    默认全关；``enabled=False`` 时不装载 DSN / 主体 / 策略清单——功能关就是
    全关（含配置面），与 approved 查询记忆的审核者设置同一口径。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    enabled: bool = False
    monitor_dsn: SecretStr | None = None
    service_subject_id: str = ""
    policy_refs: tuple[str, ...] = ()
    max_policies: int = 100
    run_deadline_seconds: int = 30


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
    approved_query_memory_enabled = _flag(env, "APPROVED_QUERY_MEMORY_ENABLED")
    approver_subjects: frozenset[str] = frozenset()
    approver_dsn: SecretStr | None = None
    if approved_query_memory_enabled:
        # 记忆开启必须有明确的审核者与独立审核身份：bi_approver 的写权限不能搭在
        # bi_app 的聊天连接上，否则“模型/聊天路径不能写长期记忆”就只剩口头承诺
        # （计划 Task 1 Step 5；总设计 §7.3）。
        approver_subjects = frozenset(
            part.strip()
            for part in (env.get("APP_APPROVER_SUBJECTS") or "").split(",")
            if part.strip()
        )
        if not approver_subjects:
            raise ValueError("APPROVED_QUERY_MEMORY_REQUIRES_APPROVER_SUBJECTS")
        approver_dsn_raw = (env.get("BI_APPROVER_DSN") or "").strip()
        if not approver_dsn_raw:
            raise ValueError("APPROVED_QUERY_MEMORY_REQUIRES_APPROVER_DSN")
        if approver_dsn_raw == app_dsn.get_secret_value():
            raise ValueError("APPROVED_QUERY_MEMORY_APPROVER_DSN_MUST_DIFFER")
        approver_dsn = SecretStr(approver_dsn_raw)
    # 门禁解析沿用 `_flag` 的严格 true/false：缺席与空白都是关，
    # "1"/"yes"/"True" 之类一律当场报错（计划 Task 1 Step 5）。
    isolated_analysis_enabled = _flag(env, "ISOLATED_ANALYSIS_ENABLED")
    return AppSettings(
        app_dsn=app_dsn,
        shop_ids=shop_ids,
        environment=environment,  # type: ignore[arg-type]
        allowed_subjects=allowed,
        public_origin=public_origin,
        auth_subject_header=(env.get("AUTH_SUBJECT_HEADER") or "X-Auth-Request-Sub").strip(),
        semantic_catalog_enabled=semantic_catalog_enabled,
        controlled_sql_enabled=controlled_sql_enabled,
        approved_query_memory_enabled=approved_query_memory_enabled,
        approver_subjects=approver_subjects,
        approver_dsn=approver_dsn,
        isolated_analysis_enabled=isolated_analysis_enabled,
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


def load_monitor_settings(env: Mapping[str, str]) -> MonitorSettings:
    """加载持续库存监控配置（计划 Task 1 Step 4）。

    只读取 ``INVENTORY_MONITOR_ENABLED`` / ``BI_MONITOR_DSN`` /
    ``MONITOR_SERVICE_SUBJECT`` / ``MONITOR_POLICY_REFS`` 四个变量；disabled
    时不要求任何秘密。enabled 时三件套（独立 DSN、服务主体、策略清单）缺一
    不可、策略清单最多 100 个去重引用，且 ``BI_MONITOR_DSN`` 不得等于 app /
    writer(同步) / approver 任一既有身份的 DSN——报错只给固定码，不回显任何
    DSN 原文。
    """
    enabled = _flag(env, "INVENTORY_MONITOR_ENABLED")
    if not enabled:
        return MonitorSettings(enabled=False)
    monitor_dsn_raw = (env.get("BI_MONITOR_DSN") or "").strip()
    if not monitor_dsn_raw:
        raise ValueError("INVENTORY_MONITOR_REQUIRES_BI_MONITOR_DSN")
    subject = (env.get("MONITOR_SERVICE_SUBJECT") or "").strip()
    if not subject:
        raise ValueError("INVENTORY_MONITOR_REQUIRES_MONITOR_SERVICE_SUBJECT")
    policy_refs = tuple(
        part.strip()
        for part in (env.get("MONITOR_POLICY_REFS") or "").split(",")
        if part.strip())
    if not policy_refs:
        raise ValueError("INVENTORY_MONITOR_REQUIRES_MONITOR_POLICY_REFS")
    if len(set(policy_refs)) != len(policy_refs):
        raise ValueError("MONITOR_POLICY_REFS_MUST_BE_UNIQUE")
    if len(policy_refs) > 100:
        raise ValueError("MONITOR_POLICY_REFS_TOO_MANY")
    for other in ("BI_APP_DSN", "BI_WRITER_DSN", "BI_APPROVER_DSN"):
        other_dsn = (env.get(other) or "").strip()
        if other_dsn and other_dsn == monitor_dsn_raw:
            raise ValueError("MONITOR_DSN_MUST_BE_SEPARATE")
    return MonitorSettings(
        enabled=True,
        monitor_dsn=SecretStr(monitor_dsn_raw),
        service_subject_id=subject,
        policy_refs=policy_refs,
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
