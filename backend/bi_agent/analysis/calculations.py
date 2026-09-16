"""确定性 Decimal 分析计算（计划 2026-09-14-isolated-analysis-agent.md Task 3）。

纯函数切片：输入只有不可变的 `AnalysisDataset` 与请求的 kinds，输出只有
`Finding`。本模块不 import 本包之外的任何 bi_agent 模块，不碰 psycopg、
repository、文件、网络、模型或工具（Task 4 会把这条边界固定成 import 守卫）。

数值纪律：
- 精确运算（加、减、中位数）在 `prec=200` 且 Inexact 为陷阱的上下文里做：
  任何会被静默舍入的输入当场转成稳定错误码 `analysis_value_out_of_range`，
  绝不悄悄降级；
- 除法（贡献占比、变化率、MAD 分数）在 `prec=200` 的上下文里做一次，
  再按 ROUND_HALF_EVEN 量化到 6 位小数并编码成字符串——全程无 float；
- 量级界：调整指数 > 22（即 |值| ≥ 1e23）直接 `analysis_value_out_of_range`。
  这是 Task 1 `_safe_decimal` 注释里“超大 Decimal 由 Task 3 兜底”的落点；
- 输出确定性：指标名按字典序、行按 row_ref 排序后计算；finding 按
  （固定 kind 序、指标、row_refs）排序；`finding_ref` 由
  `(kind, metric, row_refs, statement_code)` 的 sha256 前 24 位派生——
  观测顺序与 metrics/previous_metrics 键序都不影响任何输出字节。

语义（计划 Task 3）：
- contribution：逐指标求总额；总额为 0 的指标不发贡献 finding（0/0 无
  定义，也不得把缺失演成 0）；每个 finding 带量化占比与未量化精确原值。
- change_decomposition：只有同时具备 current 与 previous 的 (行, 指标)
  才有 finding；previous 缺失不造零基线——limitation 拼写固定为
  `PREVIOUS_PERIOD_UNAVAILABLE`，由上层（Task 4/5）写进结果 limitations；
  previous 为 0 时只有绝对变化，不发 change_rate。
- anomaly_candidates：同一指标 ≥4 个观测才计算 MAD；分数为
  |0.6745·(x−中位数)/MAD| 量化后 ≥3.5 才是候选；MAD 为 0 时等于中位数
  的行静默，其余行把分数编码为 `MAD_ZERO_SCORE`——Infinity 永远不写成
  数值。阈值判定在量化后的分数上进行，显示与判定永远一致。
- followups：只有三条固定模板（核对来源批次 / 补充 previous period /
  检查 coverage gaps），分别只在异常候选行、请求了变化分解且行缺
  previous、数据集自带 limitations 时出现；绝不产生采购、改价、投放类
  行动建议。
"""

from __future__ import annotations

import hashlib
from decimal import (Context, Decimal, DivisionByZero, Inexact, InvalidOperation,
                     Overflow, ROUND_HALF_EVEN, localcontext)

from .models import AnalysisDataset, AnalysisKind, Finding

__all__ = ["MAD_METHOD", "MAD_THRESHOLD", "MAD_ZERO_SCORE",
           "PREVIOUS_PERIOD_UNAVAILABLE", "compute_findings"]

# 上层把“缺 previous”写进结果 limitations 时使用的唯一拼写。
PREVIOUS_PERIOD_UNAVAILABLE = "previous_period_unavailable"
# MAD 候选阈值（计划固定值 3.5）；比较在量化后的分数上进行。
MAD_THRESHOLD = Decimal("3.5")
MAD_METHOD = "median_absolute_deviation"
# MAD 为 0 且值偏离中位数时的分数编码：Infinity 不是数值，绝不写进 values。
MAD_ZERO_SCORE = "mad_zero_non_median"

_SIX_PLACES = Decimal("0.000001")
_MAD_WEIGHT = Decimal("0.6745")
_MIN_MAD_OBSERVATIONS = 4
# 调整指数 ≤22 保证量化结果 ≤30 位有效数字，距离计算精度（200）很远。
_MAX_ADJUSTED_EXPONENT = 22
_EXACT_CONTEXT = Context(prec=200, rounding=ROUND_HALF_EVEN,
                         traps=[InvalidOperation, DivisionByZero, Overflow,
                                Inexact])
_ROUND_CONTEXT = Context(prec=200, rounding=ROUND_HALF_EVEN,
                         traps=[InvalidOperation, DivisionByZero, Overflow])

# finding 的固定输出顺序 = AnalysisKind 的声明顺序。
_KIND_ORDER: tuple[str, ...] = ("contribution", "change_decomposition",
                                "anomaly_candidates", "followups")

_STATEMENT_CONTRIBUTION = "contribution_share"
_STATEMENT_CHANGE = "change_vs_previous"
_STATEMENT_ANOMALY = "mad_outlier_candidate"
_STATEMENT_VERIFY_BATCH = "verify_source_batch"
_STATEMENT_SUPPLY_PREVIOUS = "supply_previous_period"
_STATEMENT_INSPECT_GAPS = "inspect_coverage_gaps"


def _fail_out_of_range() -> ValueError:
    return ValueError("analysis_value_out_of_range")


def _exact(operation):
    """精确 Decimal 运算：任何会被上下文舍入的输入都转成稳定错误码。"""
    try:
        with localcontext(_EXACT_CONTEXT):
            return operation()
    except Inexact:
        raise _fail_out_of_range() from None


def _rounded(operation):
    """高精度运算后一次性量化的通道；量化放不下时同样 fail closed。"""
    try:
        with localcontext(_ROUND_CONTEXT):
            return operation()
    except InvalidOperation:
        raise _fail_out_of_range() from None


def _share(value: Decimal, total: Decimal) -> str:
    """value/total 一次性 ROUND_HALF_EVEN 量化到 6 位小数的字符串。"""
    quotient = _rounded(lambda: value / total)
    return str(_rounded(lambda: quotient.quantize(_SIX_PLACES,
                                                  rounding=ROUND_HALF_EVEN)))


def _exact_text(value: Decimal) -> str:
    """未量化精确值的定点字符串：不用科学计数法，保留输入精度。"""
    return format(value, "f")


def _guard(value: Decimal) -> None:
    if value.adjusted() > _MAX_ADJUSTED_EXPONENT:
        raise _fail_out_of_range()


def _median(values: tuple[Decimal, ...]) -> Decimal:
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return _exact(lambda: (ordered[middle - 1] + ordered[middle]) / 2)


def _finding(kind: str, metric: str, row_refs: tuple[str, ...],
             values: dict[str, str], statement: str) -> Finding:
    seed = "|".join((kind, metric, ",".join(row_refs), statement))
    return Finding(
        finding_ref="finding-"
                    + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:24],
        kind=kind, metric=metric, row_refs=row_refs, values=values,
        statement_code=statement)


def _grouped(dataset: AnalysisDataset):
    """按指标分组并排序；全量数值先过量级守护，与请求的 kinds 无关。"""
    current: dict[str, list[tuple[str, Decimal]]] = {}
    previous: dict[str, dict[str, Decimal]] = {}
    for observation in dataset.observations:
        for value in observation.metrics.values():
            _guard(value)
        for value in observation.previous_metrics.values():
            _guard(value)
        for metric, value in observation.metrics.items():
            current.setdefault(metric, []).append((observation.row_ref, value))
        for metric, value in observation.previous_metrics.items():
            previous.setdefault(metric, {})[observation.row_ref] = value
    for rows in current.values():
        rows.sort(key=lambda item: item[0])
    return current, previous


def _contribution_findings(current: dict[str, list[tuple[str, Decimal]]]
                           ) -> list[Finding]:
    findings: list[Finding] = []
    for metric in sorted(current):
        rows = current[metric]
        total = _exact(
            lambda rows=rows: sum((value for _, value in rows), Decimal(0)))
        if total == 0:
            # 总额为 0：占比无定义；正负抵消的合计不得拆成“人均 0”。
            continue
        for row_ref, value in rows:
            findings.append(_finding(
                "contribution", metric, (row_ref,),
                {"contribution": _share(value, total),
                 "value": _exact_text(value)},
                _STATEMENT_CONTRIBUTION))
    return findings


def _change_findings(current: dict[str, list[tuple[str, Decimal]]],
                     previous: dict[str, dict[str, Decimal]]) -> list[Finding]:
    findings: list[Finding] = []
    for metric in sorted(current):
        previous_by_row = previous.get(metric, {})
        for row_ref, value in current[metric]:
            if row_ref not in previous_by_row:
                continue
            base = previous_by_row[row_ref]
            absolute = _exact(lambda value=value, base=base: value - base)
            values = {"current": _exact_text(value),
                      "previous": _exact_text(base),
                      "change": _exact_text(absolute)}
            if base != 0:
                values["change_rate"] = _share(absolute, abs(base))
            findings.append(_finding("change_decomposition", metric,
                                     (row_ref,), values,
                                     _STATEMENT_CHANGE))
    return findings


def _anomaly_candidates(current: dict[str, list[tuple[str, Decimal]]]
                        ) -> list[tuple[str, str, dict[str, str]]]:
    """(metric, row_ref, values) 候选三元组；anomaly 与 followups 共用。"""
    candidates: list[tuple[str, str, dict[str, str]]] = []
    for metric in sorted(current):
        rows = current[metric]
        if len(rows) < _MIN_MAD_OBSERVATIONS:
            continue
        observations = tuple(value for _, value in rows)
        center = _median(observations)
        deviations = _exact(lambda observations=observations, center=center:
                            tuple(abs(value - center) for value in observations))
        mad = _median(deviations)
        for row_ref, value in rows:
            if mad == 0:
                if value == center:
                    continue
                score = MAD_ZERO_SCORE
            else:
                weighted = _exact(lambda value=value, center=center:
                                  _MAD_WEIGHT * (value - center))
                score = _share(weighted.copy_abs(), mad)
                if Decimal(score) < MAD_THRESHOLD:
                    continue
            candidates.append((metric, row_ref, {
                "method": MAD_METHOD, "score": score,
                "value": _exact_text(value), "center": _exact_text(center),
                "mad": _exact_text(mad)}))
    return candidates


def _followup_findings(current: dict[str, list[tuple[str, Decimal]]],
                       previous: dict[str, dict[str, Decimal]],
                       dataset: AnalysisDataset, requested: frozenset[str],
                       built: dict[str, list[Finding]]) -> list[Finding]:
    findings: list[Finding] = []
    for finding in built.get("anomaly_candidates", ()):
        (row_ref,) = finding.row_refs
        findings.append(_finding(
            "followups", finding.metric, (row_ref,),
            {"followup": f"核对 {row_ref} 的来源批次"},
            _STATEMENT_VERIFY_BATCH))
    if "change_decomposition" in requested:
        for metric in sorted(current):
            known = previous.get(metric, {})
            for row_ref, _value in current[metric]:
                if row_ref not in known:
                    findings.append(_finding(
                        "followups", metric, (row_ref,),
                        {"followup": "补充 previous period"},
                        _STATEMENT_SUPPLY_PREVIOUS))
    if dataset.limitations:
        for metric in sorted(current):
            findings.append(_finding(
                "followups", metric,
                tuple(row_ref for row_ref, _value in current[metric]),
                {"followup": "检查 coverage gaps"},
                _STATEMENT_INSPECT_GAPS))
    return findings


def compute_findings(dataset: AnalysisDataset,
                     kinds: tuple[AnalysisKind, ...]) -> tuple[Finding, ...]:
    """按请求 kinds 计算确定性 finding；输出与输入顺序、键序无关。"""
    unknown = sorted(set(kinds) - set(_KIND_ORDER))
    if unknown:
        raise ValueError("analysis_kind_invalid")
    requested = frozenset(kinds)
    current, previous = _grouped(dataset)
    built: dict[str, list[Finding]] = {}
    if "contribution" in requested:
        built["contribution"] = _contribution_findings(current)
    if "change_decomposition" in requested:
        built["change_decomposition"] = _change_findings(current, previous)
    if "anomaly_candidates" in requested:
        built["anomaly_candidates"] = [
            _finding("anomaly_candidates", metric, (row_ref,), values,
                     _STATEMENT_ANOMALY)
            for metric, row_ref, values in _anomaly_candidates(current)]
    if "followups" in requested:
        built["followups"] = _followup_findings(current, previous, dataset,
                                                requested, built)
    findings = [finding for kind in _KIND_ORDER
                for finding in built.get(kind, ())]
    findings.sort(key=lambda item: (_KIND_ORDER.index(item.kind), item.metric,
                                    item.row_refs, item.statement_code))
    return tuple(findings)
