"""受控 SQL 探索的阶段契约（计划 Task 1）。

这条链上每个形状都属于不同阶段，混在一起就分不清"谁还没过哪道门禁"：
`ExplorationRequest`（模型能给的全部输入）→ `SqlDraft`（编译器产出，还没过校验）→
`ValidatedQueryPlan`（过了 AST/权限/成本，可以被执行）→ `ExplorationResult`
（投影过后的安全结果）。`ExplorationColumn` 是结果列的描述。

本模块只有形状，没有任何行为：不解析 SQL、不查目录、不连库、不执行。编译属 Task 2，
AST 策略属 Task 3，只读执行与投影属 Task 4，运行域与 Agent Tool 属 Task 5。

三条收口理由：

1. ref 一律复用语义目录的 `_require_ref()`／`_require_refs()`。探索层不写第二份 ref 正则，
   也不接受 `reporting.v_shop_daily`、`SUM(paid_amount)` 这类 SQL 片段——一旦接受，标识符
   就又回到调用方手里（总设计 §6.3）。
2. 列表去重后长度必须不变。静默丢掉一项会让"两个指标"变成一个指标的查询，而结果看起来
   完全正常。
3. 数字、版本与指纹 fail closed。`limit` 只认 1..500 的整数（`"100"` / `100.0` / `True`
   在 Pydantic lax 强转下都会变成 100/1），成本只认有限非负 `Decimal`，版本标识与指纹
   共用 `runtime.versions` / `runtime.artifacts` 已有的那一条规则。
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Annotated

from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from bi_agent.runtime.versions import check_version_identifier
from bi_agent.semantic_catalog.models import (
    DataType,
    _require_ref,
    _require_refs,
    _require_text,
)

# 请求窗口上界（天）：整轮 30 秒 deadline 与 EXPLAIN 成本门禁之外，先由形状限住。
MAX_WINDOW_DAYS = 366
# 行数上限：请求侧的 `limit` 与结果侧的 `rows` 是同一个预算，不各写一遍数字。
MAX_ROWS = 500
MAX_QUESTION_CHARS = 4000
# SHA-256 指纹的十六进制小写：与 `runtime.artifacts.request_fingerprint` 同一形状。
SHA256_HEX_RE = r"^[0-9a-f]{64}$"


def _require_int(value: object) -> object:
    """整数只认整数：计数与 limit 不许靠字符串、浮点或布尔混过关。"""
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("exploration_integer_required")
    return value


def _stable_ref(value: object) -> object:
    """把目录的 ref 规则包成逐列可用的校验器。

    失败时只报稳定原因码，不回显入参：一个试图混进来的 `SUM(paid_amount)` 不该被
    抄进错误消息、再进日志或模型载荷。
    """
    try:
        _require_ref(value)
    except ValueError:
        raise ValueError("exploration_ref_invalid") from None
    return value


StrictInt = Annotated[int, BeforeValidator(_require_int)]
# 逐项校验：loc 里带下标，才能知道八个 ref 里到底哪一个不对。
SemanticRef = Annotated[str, AfterValidator(_stable_ref)]


class ExplorationRequest(BaseModel):
    """模型能提交的全部输入：只有稳定语义 ref 与业务值，没有任何标识符。

    日期是半开区间 `[start, end)`，沿用固定指标的口径；两者必须同时给或同时不给。
    "没有日期窗口"在本契约层是合法的（有些探索不需要时间），要不要窗口由 Task 2
    编译时按 view 是否有 time field 判定，不在这里猜。
    """

    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True,
                              allow_inf_nan=False)

    question: str = Field(min_length=1, max_length=MAX_QUESTION_CHARS)
    start: date | None = None
    end: date | None = None
    entity_refs: list[SemanticRef] = Field(default_factory=list, max_length=5)
    requested_metric_refs: list[SemanticRef] = Field(min_length=1, max_length=8)
    group_by_field_refs: list[SemanticRef] = Field(default_factory=list, max_length=4)
    limit: StrictInt = Field(default=100, ge=1, le=MAX_ROWS)

    @field_validator("question")
    @classmethod
    def _question_text(cls, value: str) -> str:
        # 只判"非空白"，不改原文：改过的提问就对不上诊断里记录的那一句了。
        _require_text(value)
        return value

    @field_validator("entity_refs", "requested_metric_refs", "group_by_field_refs")
    @classmethod
    def _unique_refs(cls, value: list[str]) -> list[str]:
        # 去重后长度变了就拒：静默丢一项等于静默改掉用户问的那个问题的形状。
        _require_refs(tuple(value))
        return value

    @model_validator(mode="after")
    def _window(self) -> "ExplorationRequest":
        if (self.start is None) != (self.end is None):
            raise ValueError("exploration_window_incomplete")
        if self.start is not None and self.end is not None:
            if not self.start < self.end:
                raise ValueError("exploration_window_ordered")
            if (self.end - self.start).days > MAX_WINDOW_DAYS:
                raise ValueError("exploration_window_too_large")
        return self


class SqlDraft(BaseModel):
    """编译器产出的单条 SELECT 草案：还没过 AST、权限与成本任何一道门禁。

    `sql_text` 与 `parameters` 分开存是刻意的：真值只走参数，文本里不许出现店号。
    """

    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True,
                              allow_inf_nan=False)

    sql_text: str
    parameters: dict[str, object]
    selected_refs: list[SemanticRef]

    @field_validator("sql_text")
    @classmethod
    def _draft_sql_text(cls, value: str) -> str:
        _require_text(value)
        return value


class ValidatedQueryPlan(BaseModel):
    """已过策略校验、可以执行的计划：指纹与版本一起冻结，换版本必须重新生成。

    `estimated_rows` / `estimated_total_cost` 是 Task 4 `EXPLAIN` 的产物，所以本切片
    默认空着——非负、有限，且不允许由调用方填一个看起来安全的数。
    """

    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True,
                              allow_inf_nan=False)

    template_version: str
    catalog_version: str
    statement_fingerprint: str = Field(pattern=SHA256_HEX_RE)
    sql_text: str
    parameters: dict[str, object]
    selected_refs: list[SemanticRef]
    estimated_rows: StrictInt | None = Field(default=None, ge=0)
    estimated_total_cost: Decimal | None = Field(default=None, ge=0)
    warnings: list[str] = Field(default_factory=list)

    @field_validator("template_version", "catalog_version")
    @classmethod
    def _version_identifier(cls, value: str) -> str:
        return check_version_identifier(value)

    @field_validator("sql_text")
    @classmethod
    def _plan_sql_text(cls, value: str) -> str:
        _require_text(value)
        return value


class ExplorationColumn(BaseModel):
    """一列的身份：稳定 ref 加目录词表里的数据类型，不含 SQL 列名。

    `data_type` 直接复用 `semantic_catalog.models.DataType`：封闭词表只有一份，
    否则这里能写出 `money`，目录那边却从来没有这种列。
    """

    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True,
                              allow_inf_nan=False)

    ref: SemanticRef
    data_type: DataType


class ExplorationResult(BaseModel):
    """公开 Artifact 的载荷：安全结果 + 口径/覆盖/诊断/限制，永远没有 SQL 原文。

    行的值类型留给 Task 4 投影（`None/bool/int/Decimal/date/datetime/str`）；本形状只
    管得住行数上限与列的身份。
    """

    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True,
                              allow_inf_nan=False)

    template_version: str
    catalog_version: str
    statement_fingerprint: str = Field(pattern=SHA256_HEX_RE)
    columns: list[ExplorationColumn]
    rows: list[dict[str, object]] = Field(max_length=MAX_ROWS)
    basis: list[dict[str, str]]
    coverage: dict[str, object]
    diagnostics: list[dict[str, object]]
    limitations: list[str]

    @field_validator("template_version", "catalog_version")
    @classmethod
    def _version_identifier(cls, value: str) -> str:
        return check_version_identifier(value)
