"""approved 查询学习记忆的存储契约（计划 Task 1）。

记忆里只允许两类文本：稳定 ref（`ent-*` 实体引用、语义目录的 kebab ref）和已登记
业务码（口径、币种、平台、指标等封闭词表）。一次性业务值——日期、金额、阈值、
预算、店铺选择——必须是槽位，不能是常量；原始聊天、SQL、结果行、真实主键在这层
就被拒收，而不是等到投影或审核时再过滤（总设计 §7.2、§10）。

三条规则在这层一次定死：

1. 形状收口：`extra="forbid"` + `frozen=True` + `hide_input_in_errors`。报错文本
   不回显被拒输入，否则校验器自己就成了第二条泄露通道。
2. 递归污染检查：`normalized_request` 的键与值在**任意深度**检查。只查顶层键等于
   没查——真实载荷的污染几乎都藏在嵌套结构里；数字、布尔与 null 也不是稳定 ref，
   金额与阈值可以藏在任何标量里，标量一律不收。
3. 词表单一来源：业务码集合从 `runtime.models` / `catalog` 的现有封闭集组装，ref
   形状复用 `catalog.REF_RE`，不另写第二份词表。`FORBIDDEN_VALUE_KEYS` 与迁移 021
   的 `approved_query_no_bound_values` CHECK 逐一对应，由 tests.test_db 用真实插入
   逐键探测，两边单边改都会在测试现场变红。
"""

from __future__ import annotations

import re
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from bi_agent.catalog import PLATFORM_LABELS, REF_RE
# 业务码词表跨模块复用 runtime 的封闭集（与 exploration 复用 semantic_catalog 的
# 私有校验器同一模式）：这里再抄一份，迟早两边漂移。
from bi_agent.runtime.models import (
    _COMPARE,
    _CURRENCY_VALUES,
    _METRICS,
    _PROFIT_BASES,
    _REPORT_KINDS,
    _SALES_BASES,
    _SCOPE_MODES,
)
from bi_agent.runtime.versions import VersionSet

ApprovalStatus = Literal["draft", "approved", "superseded", "revoked"]
SlotKind = Literal["entity_scope", "date_window", "target_price", "threshold", "budget"]

# 数据库 021 的 approved_query_no_bound_values CHECK 与这份词表逐一对应。
FORBIDDEN_VALUE_KEYS = frozenset({
    "start", "end", "date", "shop_id", "subject_id", "target_price",
    "threshold", "budget", "sql_text", "rows", "result", "prompt"})

# 已登记业务码：只收记忆的规范化请求里可能合法出现的封闭词表（口径、币种、比较
# 方式、范围模式、报表形态、指标码、平台码）。group_by、阈值等运行时专有词表不收：
# 它们对应的键本身就是 FORBIDDEN_VALUE_KEYS 或 Task 2 净化器白名单之外的键。
BUSINESS_CODES = (
    _SALES_BASES | _PROFIT_BASES | _CURRENCY_VALUES | _COMPARE
    | _SCOPE_MODES | _REPORT_KINDS | _METRICS | frozenset(PLATFORM_LABELS)
)

# 值字符串的稳定 ref 形状：语义目录引用的 kebab 形状（至少两段，中间有连字符）。
# 单独一个无连字符小写词不收——它若不是已登记业务码就是自由文本，自由文本不进记忆。
_MEMORY_REF_RE = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)+$")
# 模板与值里的绑定信息形状：UUID、长数字串、日期与金额字面量。
_UUID_TEXT_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
_LONG_DIGIT_RUN_RE = re.compile(r"[0-9]{6,}")
_DATE_TEXT_RE = re.compile(r"[0-9]{1,4}-[0-9]{1,2}-[0-9]{1,2}")
_MONEY_TEXT_RE = re.compile(r"[0-9]+\.[0-9]+")
# 模板里的内部 ref：ent- 引用之外的一律拒（槽位占位符带花括号，不匹配此形状）。
_TEMPLATE_INTERNAL_REF_RE = re.compile(
    r"(?<![0-9A-Za-z_-])[a-z][a-z0-9]*(?:-[a-z0-9]+)+(?![0-9A-Za-z_-])")
_SQL_KEYWORD_RE = re.compile(
    r"(?<![A-Za-z_])(select|insert|update|delete|drop|alter|create|truncate|grant|"
    r"revoke|union|exec|execute|copy|from|where|having|limit)(?![A-Za-z_])",
    re.IGNORECASE)


def _reject_bound_values(node: object) -> None:
    """递归遍历请求结构：任意深度的键与值都不许夹带绑定业务值或自由文本。"""
    if isinstance(node, dict):
        for key, value in node.items():
            if not isinstance(key, str) or key in FORBIDDEN_VALUE_KEYS:
                raise ValueError("memory_contains_bound_business_value")
            _reject_bound_values(value)
    elif isinstance(node, (list, tuple)):
        for item in node:
            _reject_bound_values(item)
    elif isinstance(node, str):
        if (REF_RE.fullmatch(node) or node in BUSINESS_CODES
                or _MEMORY_REF_RE.fullmatch(node)):
            return
        raise ValueError("memory_value_not_a_stable_ref_or_code")
    else:
        raise ValueError("memory_value_not_a_stable_ref_or_code")


def _check_template(template: str) -> None:
    """问题模板只许文字与槽位占位符。

    UUID、连续六位以上数字、日期与金额字面量是绑定信息；SQL 关键字与 `ent-` 之外的
    内部 ref 是注入面。任何一处命中都整条拒绝，不做局部摘除——摘除后的模板会让人
    以为它还是审核时看到的那句话。
    """
    if (_UUID_TEXT_RE.search(template) or _LONG_DIGIT_RUN_RE.search(template)
            or _DATE_TEXT_RE.search(template) or _MONEY_TEXT_RE.search(template)
            or _SQL_KEYWORD_RE.search(template)):
        raise ValueError("memory_template_bound_value")
    for token in _TEMPLATE_INTERNAL_REF_RE.findall(template):
        if not REF_RE.fullmatch(token):
            raise ValueError("memory_template_bound_value")


class QuerySlot(BaseModel):
    """槽位定义：一次性业务值只能以槽位出现，不允许作为常量存进记忆。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(pattern=r"^[a-z][a-z0-9_]{0,31}$")
    kind: SlotKind


class ApprovedExample(BaseModel):
    """检索层可见的 approved 样例：不含来源运行、所有者与授权范围。"""

    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    example_ref: str = Field(pattern=r"^mem-[a-z0-9-]{1,60}$")
    domain: str = Field(pattern=r"^[a-z][a-z0-9_]{0,31}$")
    intent_signature: str = Field(pattern=r"^[a-z][a-z0-9-]{0,79}$")
    question_template: str = Field(min_length=1, max_length=400)
    slots: tuple[QuerySlot, ...]
    normalized_request: dict[str, object]
    expected_tool: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    version_requirements: VersionSet
    approval_revision: int = Field(ge=1)

    @model_validator(mode="after")
    def safe_template_and_request(self) -> "ApprovedExample":
        _reject_bound_values(self.normalized_request)
        _check_template(self.question_template)
        expected = {"{" + slot.name + "}" for slot in self.slots}
        if any(token not in self.question_template for token in expected):
            raise ValueError("memory_slot_missing_from_template")
        if len({slot.name for slot in self.slots}) != len(self.slots):
            raise ValueError("memory_slot_duplicate")
        return self


class StoredMemoryRecord(BaseModel):
    """存储层的完整记录：多出来源与授权字段；approved 投影经 `as_approved` 收窄。"""

    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    example_ref: str
    source_run_id: UUID
    owner_subject_id: str
    domain: str
    intent_signature: str
    question_template: str
    slots: tuple[QuerySlot, ...]
    normalized_request: dict[str, object]
    expected_tool: str
    version_requirements: VersionSet
    authorization_refs: tuple[str, ...]
    status: ApprovalStatus
    approval_revision: int = Field(ge=0)

    @model_validator(mode="after")
    def sanitized_content(self) -> "StoredMemoryRecord":
        """从库读回的行同样过一遍净化：契约不信任写入方，两道闸各挡各的。"""
        _reject_bound_values(self.normalized_request)
        _check_template(self.question_template)
        return self

    def as_approved(self) -> ApprovedExample:
        if self.status != "approved" or self.approval_revision < 1:
            raise ValueError("memory_not_approved")
        return ApprovedExample(**self.model_dump(exclude={
            "source_run_id", "owner_subject_id", "authorization_refs", "status"}))


class ApprovalCommand(BaseModel):
    """审核决定：supersede 必须指名 replacement，approve/revoke 必须不带。"""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    action: Literal["approve", "revoke", "supersede"]
    reason: str = Field(min_length=3, max_length=500)
    replacement_ref: str | None = Field(default=None, pattern=r"^mem-[a-z0-9-]{1,60}$")

    @model_validator(mode="after")
    def replacement_pair(self) -> "ApprovalCommand":
        if (self.action == "supersede") != (self.replacement_ref is not None):
            raise ValueError("replacement_ref_action_mismatch")
        return self
