"""无工具叙事总结器与 claim 守卫（计划 2026-09-14-isolated-analysis-agent.md
Task 4）。

模型在隔离分析里只有一个职责：把已验证的 `Finding` 组织成带引用的解释文本。
它拿不到来源行、数据库上下文、用户提问、授权内部或任何工具——
`model.complete(messages, [], timeout_s=…)` 的 tools 恒为空列表（该调用形状由
import_guard 钉死），超时沿用主请求剩余 deadline，不另起计时。

发布纪律（全部在本模块内判完才落进 `AnalysisResult`）：
- 每条 fact/observation 必须引用存在的 finding_ref，文本中的数字 token 必须
  逐字出现在所引 finding 的“单个”value 里（禁止换算、汇总、四舍五入，也
  禁止跨 value 拼接蒙混）；
- 因果词（因为/导致/由于/caused/because/therefore）只有在引用的 finding 带
  专门因果 evidence code 时才可发布（对 hypothesis 同样适用）；Task 3 的
  确定性词表没有这种 code，所以因果句子一律移入 unsupported_claims；
- 行动建议词（应该立即/马上采购/自动改价）在任何 claim_kind 里都不发布；
- hypothesis 必须自带“假设/待验证”标记才进 hypotheses，否则视为无证据说法；
- 非法引用、多余响应字段、控制字符文本、解析失败的整体回复都进
  unsupported_claims，绝不伪装成事实；
- 模型异常/超时/空回复由 run_isolated_analysis 转成 `narrative_unavailable`
  limitation，确定性 findings 原样保留。

输入 token 预算固定 8,000：按 CJK 字符计 1、其余每 4 字符计 1 的保守估计，
超限在任何模型调用之前拒绝（无外部依赖）。
"""

from __future__ import annotations

import json
import re
import time
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from bi_agent.llm import ChatModel, Message

from .calculations import PREVIOUS_PERIOD_UNAVAILABLE, compute_findings
from .models import AnalysisDataset, AnalysisKind, AnalysisResult, Finding

__all__ = ["ANALYSIS_VERSION", "CAUSAL_EVIDENCE_CODES", "INPUT_TOKEN_LIMIT",
           "MIN_NARRATIVE_SECONDS", "NARRATIVE_UNAVAILABLE", "NarrativeItem",
           "NarrativeResult", "run_isolated_analysis", "summarize_findings"]

# 计划 Task 5 的固定分析版本；图节点持久化时沿用同一拼写。
ANALYSIS_VERSION = "isolated-analysis/2026-09-14.1"
NARRATIVE_UNAVAILABLE = "narrative_unavailable"
# 计划 Global Constraints：模型输入 ≤8,000 token。
INPUT_TOKEN_LIMIT = 8000
# 计划 Task 5：剩余不足 2 秒不再请求模型；该规则落在本入口，图层直接复用。
MIN_NARRATIVE_SECONDS = 2.0
# 与 runtime 公开载荷校验一致的三个上界（narrative/hypotheses/unsupported ≤20）。
_CLAIM_LIST_LIMIT = 20
# AnalysisResult.limitations 的 Field 上限（analysis/models 的 _LIST_LIMIT）：
# 数据集码 + 派生码的合并总量确定性封顶在这里，永不溢出校验器。
_LIMITATIONS_CAP = 20
_TEXT_LIMIT = 500
_REFS_LIMIT = 5

_CAUSAL_MARKERS = ("因为", "导致", "由于", "caused", "because", "therefore")
_ACTION_MARKERS = ("应该立即", "马上采购", "自动改价")
# 计划语义：“因果词 + 专门 evidence code”才可发布。Task 3 的确定性词表没有
# 任何因果证据 code，此集合为空 ⇒ 因果句子现在一律进 unsupported_claims；
# 将来若登记因果 evidence code，必须同时扩这个 frozenset 并补测试。
CAUSAL_EVIDENCE_CODES: frozenset[str] = frozenset()
_HYPOTHESIS_MARKERS = ("假设", "待验证")
# 数字 token：前后不能贴着字母/数字/连字符/点，避免把 row-001、finding-7、
# v1.2 里的片段当成需要引用支持的数值。
_NUMBER_RE = re.compile(r"(?<![0-9A-Za-z_.-])[0-9]+(?:\.[0-9]+)?(?![0-9A-Za-z])")

# 系统提示只描述输出契约，不携带任何来源、授权或查询通道信息。
_SYSTEM_PROMPT = (
    "你只总结给定的 finding，不得引入外部信息。规则："
    "1. 每条输出引用 1-5 个存在的 finding_ref；"
    "2. 文本中的数字必须逐字来自所引 finding 的 values，不得计算或改写；"
    "3. fact/observation 不得包含因果推断（因为/导致/由于）或行动建议；"
    "4. hypothesis 必须以待验证口吻书写，句中包含“假设”或“待验证”；"
    "5. 只输出 JSON 数组："
    "[{\"text\": \"…\", \"finding_refs\": [\"finding-…\"], "
    "\"claim_kind\": \"fact|observation|hypothesis\"}]，"
    "不输出其它键、注释或代码块。"
)


class NarrativeItem(BaseModel):
    """模型回复里允许的单条叙事：三个键、有界文本、1-5 个引用。"""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    text: str = Field(min_length=1, max_length=_TEXT_LIMIT)
    finding_refs: tuple[str, ...] = Field(min_length=1, max_length=_REFS_LIMIT)
    claim_kind: Literal["fact", "observation", "hypothesis"]


class NarrativeResult(BaseModel):
    """守卫输出：可发布叙事、显式待验证假设与无证据说法三分。"""

    model_config = ConfigDict(extra="forbid", frozen=True,
                              hide_input_in_errors=True)

    narrative: tuple[NarrativeItem, ...] = Field(default=(),
                                                 max_length=_CLAIM_LIST_LIMIT)
    hypotheses: tuple[str, ...] = Field(default=(),
                                        max_length=_CLAIM_LIST_LIMIT)
    unsupported_claims: tuple[str, ...] = Field(default=(),
                                                max_length=_CLAIM_LIST_LIMIT)


def _estimate_tokens(text: str) -> int:
    """保守估算：CJK 全角字符计 1，其余字符每 4 个计 1（向上取整）。"""
    wide = sum(1 for character in text if ord(character) > 0x2E7F)
    return wide + (len(text) - wide + 3) // 4


def _user_prompt(findings: tuple[Finding, ...],
                 limitations: tuple[str, ...]) -> str:
    """只含验证过的 finding 与 limitations；不含来源行、授权或用户问题。"""
    return json.dumps({
        "findings": [finding.model_dump(mode="json") for finding in findings],
        "limitations": list(limitations),
    }, ensure_ascii=False, sort_keys=True)


def _parse_items(text: str) -> list[object] | None:
    """模型回复 → 候选 item 列表；None 表示整份回复无法当叙事解析。"""
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None
    if isinstance(parsed, list):
        return parsed
    if (isinstance(parsed, dict) and set(parsed) == {"narrative"}
            and isinstance(parsed["narrative"], list)):
        return parsed["narrative"]
    return None


def _claim_text(value: object) -> str | None:
    """把被拒内容压成可进 unsupported 的有界单段文本；无文本则 None。"""
    # strip→截断→再 strip：若截断落点是空白，尾随空白会让 AnalysisResult 的
    # 有界文本校验在 run_isolated_analysis 里炸掉整个运行（P1 修复）：
    # 任何被拒内容入库前必须以“干净有界文本”收尾，否则丢弃。
    if isinstance(value, str):
        text = value.strip()[:_TEXT_LIMIT].strip()
    elif isinstance(value, dict):
        raw = value.get("text")
        if isinstance(raw, str):
            text = raw.strip()[:_TEXT_LIMIT].strip()
        else:
            text = json.dumps(value, ensure_ascii=False,
                              sort_keys=True)[:_TEXT_LIMIT].strip()
    else:
        text = repr(value)[:_TEXT_LIMIT].strip()
    return text or None


def _keep(bucket: list, entry) -> None:
    if len(bucket) < _CLAIM_LIST_LIMIT:
        bucket.append(entry)


def _move(unsupported: list[str], text: str) -> None:
    # 截断后必须再 strip：防截断落点为空白（理由同 _claim_text，P1 修复）。
    claim = text.strip()[:_TEXT_LIMIT].strip()
    if claim:
        _keep(unsupported, claim)


def _validated_item(raw: object, unsupported: list[str]) -> NarrativeItem | None:
    """形状不对/带多余字段的 item 不发布；能提取的文本移入 unsupported。"""
    try:
        return NarrativeItem.model_validate(raw)
    except (ValidationError, ValueError):
        claim = _claim_text(raw)
        if claim:
            _keep(unsupported, claim)
        return None


def _publish(item: NarrativeItem, findings_by_ref: dict[str, Finding],
             narrative: list[NarrativeItem], hypotheses: list[str],
             unsupported: list[str]) -> None:
    """逐条判发布资格；任何不合格都整条移入 unsupported，绝不截半句。"""
    text = item.text
    if (text != text.strip()
            or any(ord(character) < 0x20 or ord(character) == 0x7F
                   for character in text)):
        _move(unsupported, text)
        return
    cited = [findings_by_ref[ref] for ref in item.finding_refs
             if ref in findings_by_ref]
    if len(cited) != len(item.finding_refs):
        # 未知引用：整条不是事实，只能作为无证据说法留档。
        _move(unsupported, text)
        return
    lowered = text.lower()
    if any(marker in text for marker in _ACTION_MARKERS):
        # 行动建议没有 evidence code 可豁免：任何 claim_kind 里都不发布。
        _move(unsupported, text)
        return
    if any(marker in text or marker in lowered for marker in _CAUSAL_MARKERS):
        # 严格安全解释（P2 修复）：因果词对 hypothesis 同样不可发布——只有
        # 引用的 finding 带专门因果 evidence code 才豁免；当前确定性词表
        # 没有这种 code，所以含因果词的假设句一律进 unsupported_claims。
        codes = {finding.statement_code for finding in cited}
        if not codes & CAUSAL_EVIDENCE_CODES:
            _move(unsupported, text)
            return
    if item.claim_kind == "hypothesis":
        if not any(marker in text for marker in _HYPOTHESIS_MARKERS):
            _move(unsupported, text)
            return
        _keep(hypotheses, text)
        return
    # 数字逐字证据（P2 修复）：token 必须完整出现在“单个”value 里，不得靠
    # 跨 value 拼接蒙混（如 0.5+20 拼出 0.520 放行挑造的 520）。
    for token in _NUMBER_RE.findall(text):
        if not any(token in value for finding in cited
                   for value in finding.values.values()):
            _move(unsupported, text)
            return
    _keep(narrative, item)


def summarize_findings(findings: tuple[Finding, ...], *,
                       limitations: tuple[str, ...], model: ChatModel,
                       timeout_s: float) -> NarrativeResult:
    """把验证过的 finding 总结成受守卫的叙事；tools 恒为空列表。"""
    if not findings:
        # 没有可引用的 finding：模型无从引用，直接给空叙事（不花预算）。
        return NarrativeResult()
    messages = [Message(role="system", content=_SYSTEM_PROMPT),
                Message(role="user",
                        content=_user_prompt(findings, limitations))]
    budget = (_estimate_tokens(_SYSTEM_PROMPT)
              + _estimate_tokens(messages[-1].content or ""))
    if budget > INPUT_TOKEN_LIMIT:
        raise ValueError("narrative_input_too_large")
    reply = model.complete(messages, [], timeout_s=timeout_s)
    text = reply.text
    if not isinstance(text, str) or not text.strip():
        raise ValueError("narrative_empty_response")
    items = _parse_items(text)
    if items is None:
        # 整份回复解析失败：装得进有界 claim 通道的整句留档（计划 Step 1 用例，
        # 如“销量下降是因为广告停投”）；装不进的按畸形输出当场拒绝，由
        # run_isolated_analysis 降级为 narrative_unavailable——比把无界 blob
        # 截断硬塞进 unsupported 更诚实，也从根上消灭“截断落点为空白”这条
        # 曾让结果构造在守卫区外炸掉的路径（P1 修复，见回归测试）。
        claim = text.strip()
        if len(claim) > _TEXT_LIMIT:
            raise ValueError("narrative_malformed")
        return NarrativeResult(unsupported_claims=(claim,))
    findings_by_ref = {finding.finding_ref: finding for finding in findings}
    narrative: list[NarrativeItem] = []
    hypotheses: list[str] = []
    unsupported: list[str] = []
    for raw in items:
        item = _validated_item(raw, unsupported)
        if item is not None:
            _publish(item, findings_by_ref, narrative, hypotheses, unsupported)
    return NarrativeResult(narrative=tuple(narrative),
                           hypotheses=tuple(hypotheses),
                           unsupported_claims=tuple(unsupported))


def _missing_previous(dataset: AnalysisDataset, kinds: tuple[AnalysisKind, ...],
                      findings: tuple[Finding, ...]) -> bool:
    """请求了变化分解且有 (行, 指标) 缺 previous 时，写计划固定的 limitation。"""
    if "change_decomposition" not in kinds:
        return False
    pairs = sum(len(observation.metrics)
                for observation in dataset.observations)
    changes = sum(1 for finding in findings
                  if finding.kind == "change_decomposition")
    return changes < pairs


def _merge_limitations(derived: list[str],
                       dataset_codes: tuple[str, ...]) -> tuple[str, ...]:
    """派生码在前且永不丢弃；数据集码按原顺序去重补位，总量封顶 20。

    任何 dataset.limitations（≤20 条）与派生码（≤2 条）的组合都确定性落在
    AnalysisResult 的上限内——结果构造不再可能因溢出在校验器上炸掉（P2 修复）。
    """
    merged = list(derived)
    for code in dataset_codes:
        if code in merged or len(merged) >= _LIMITATIONS_CAP:
            continue
        merged.append(code)
    return tuple(merged)


def run_isolated_analysis(dataset: AnalysisDataset, *,
                          kinds: tuple[AnalysisKind, ...],
                          model: ChatModel | None,
                          deadline: float) -> AnalysisResult:
    """确定性 findings + 受守卫叙事；模型任何失败都只降级、不覆盖事实。"""
    findings = compute_findings(dataset, tuple(kinds))
    # 派生码（安全/降级语义）单独收集：合并时带优先级，永不因上限被丢弃。
    derived: list[str] = []
    if _missing_previous(dataset, kinds, findings):
        derived.append(PREVIOUS_PERIOD_UNAVAILABLE)
    narrative_result = NarrativeResult()
    remaining = deadline - time.monotonic()
    if model is None or remaining < MIN_NARRATIVE_SECONDS:
        derived.append(NARRATIVE_UNAVAILABLE)
    else:
        try:
            narrative_result = summarize_findings(
                findings,
                limitations=_merge_limitations(derived, dataset.limitations),
                model=model, timeout_s=remaining)
        except Exception:  # 模型层任何失败（超时/错误/坏回复）都降级为无叙事
            narrative_result = NarrativeResult()
            derived.append(NARRATIVE_UNAVAILABLE)
    return AnalysisResult(
        source_artifact_ref=dataset.source_artifact_ref,
        source_fingerprint=dataset.source_fingerprint,
        analysis_version=ANALYSIS_VERSION,
        findings=findings,
        narrative=tuple(item.model_dump()
                        for item in narrative_result.narrative),
        hypotheses=narrative_result.hypotheses,
        unsupported_claims=narrative_result.unsupported_claims,
        limitations=_merge_limitations(derived, dataset.limitations))
