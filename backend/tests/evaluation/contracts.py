"""评测场景契约（计划 2026-10-24 T3）：场景 schema、加载校验与报告模型。

字段最小集（全部必填，未知字段拒绝）：``id``、``category``、``family_id``、
``split``（regression/holdout）、``turns``、``expected``、``seed_names``、
``allowed``、``feature_flags``、``scripted_replies``、``requires_human_review``。

- ``expected`` 固定为与 ``turns`` 等长的列表；每项的键与语义复用
  acceptance ``_verify_turn`` / ``_verify_commerce_turn`` 已支持的集合
  （status/parameters/values/calls/call_count/codes/not_codes/basis/
  basis_exact/limitations_contain/partial_answer/refused/remediation/
  diagnostics/coverage/no_values/no_rows/no_total/text_contains/
  text_absent/clarify/clarify_contains/clarify_absent/domain_status/
  scope_only_authorized/tool/rows/cells/ranking/groups/excluded_reasons/
  metric_units），判定实现只有 tests.evaluation.verification 与
  acceptance._verify_turn 一份。
- ``seed_names`` 必须在 acceptance ``SEED_STEPS`` 注册表内。
- ``feature_flags`` 只允许登记过的门禁；测试可显式打开被测门禁，
  不改生产默认值。
- 冻结时钟复用 ``tests.test_db.FROZEN_NOW``，场景不自带时钟。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


def _seed_registry() -> dict[str, object]:
    from tests.acceptance import SEED_STEPS

    return SEED_STEPS


# 只登记 runner 能安全显式打开的门禁（与生产默认关闭一致）。
FEATURE_FLAG_REGISTRY = frozenset({"PARTIAL_ANSWERS_ENABLED"})


class ToolCallSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    arguments: dict[str, object] = Field(default_factory=dict)
    call_id: str = "call_1"


class ScriptedReply(BaseModel):
    """一次脚本回复：显式声明 text / tool_calls / 合成 usage（可缺省）。

    ``error`` 非空时不产生回复，而是让模型调用抛 ``ModelError(error)``，
    用于故障场景（超时/限流等脱敏类别码）。
    """

    model_config = ConfigDict(extra="forbid")

    text: str | None = None
    tool_calls: list[ToolCallSpec] = Field(default_factory=list)
    usage: dict[str, int | None] | None = None
    error: str | None = None


class EvaluationCase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    category: str = Field(min_length=1)
    family_id: str = Field(min_length=1)
    split: Literal["regression", "holdout"]
    turns: list[str] = Field(min_length=1)
    expected: list[dict]
    seed_names: list[str] = Field(default_factory=list)
    allowed: list[str] = Field(default_factory=lambda: ["S1"])
    feature_flags: dict[str, bool] = Field(default_factory=dict)
    scripted_replies: list[ScriptedReply] = Field(default_factory=list)
    requires_human_review: bool = False

    @field_validator("turns")
    @classmethod
    def _turns_non_empty_text(cls, value: list[str]) -> list[str]:
        if any(not turn.strip() for turn in value):
            raise ValueError("turns每一轮都必须是非空文本")
        return value

    @field_validator("expected")
    @classmethod
    def _expected_matches_turns(cls, value: list[dict], info) -> list[dict]:
        turns = info.data.get("turns")
        if turns is not None and len(value) != len(turns):
            raise ValueError(
                f"expected长度（{len(value)}）必须等于turns长度（{len(turns)}）")
        return value

    @field_validator("seed_names")
    @classmethod
    def _seeds_registered(cls, value: list[str]) -> list[str]:
        registry = _seed_registry()
        unknown = [name for name in value if name not in registry]
        if unknown:
            raise ValueError(
                f"未登记的种子步骤：{unknown}；注册表：{sorted(registry)}")
        return value

    @field_validator("feature_flags")
    @classmethod
    def _flags_registered(cls, value: dict[str, bool]) -> dict[str, bool]:
        unknown = [key for key in value if key not in FEATURE_FLAG_REGISTRY]
        if unknown:
            raise ValueError(
                f"未登记的feature flag：{unknown}；可设置：{sorted(FEATURE_FLAG_REGISTRY)}")
        return value


class CaseResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    repeat: int
    status: Literal["pass", "fail", "error", "blocked", "skipped"]
    problems: list[str]
    duration_ms: float
    model_call_count: int
    tool_call_count: int
    run_count: int
    artifact_count: int
    error_class: str | None = None
    usage_source: str | None = None


class EvaluationSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["offline", "live"]
    note: str
    dataset_sha256: str
    source: dict
    frozen_time: str
    flags: dict[str, bool]
    repeat: int
    command: str
    totals: dict
    usage: dict
    cases: list[CaseResult]


def dataset_sha256(path: Path) -> str:
    import hashlib

    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_cases(path: Path) -> list[EvaluationCase]:
    """读入并严格校验场景文件；ID 唯一、形状合法、种子/门禁在注册表内。"""
    cases: list[EvaluationCase] = []
    seen_ids: set[str] = set()
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(),
                                  start=1):
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"第{number}行不是合法JSON：{exc.msg}") from exc
        try:
            case = EvaluationCase(**payload)
        except Exception as exc:  # pydantic 校验错误统一转成 ValueError 文本
            raise ValueError(f"第{number}行场景不合法：{exc}") from exc
        if case.id in seen_ids:
            raise ValueError(f"场景ID重复：{case.id}")
        seen_ids.add(case.id)
        cases.append(case)
    if not cases:
        raise ValueError("场景文件为空")
    return cases
