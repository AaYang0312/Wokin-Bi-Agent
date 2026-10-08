"""覆盖缺口能否拆开出数。纯函数，不连库。

调用方传入每个成分已经算好的覆盖窗口。聚合指标用自己的窗口；同源比值取
分子分母窗口的交集；跨源比值和库存时点不参与部分出数。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Literal, Mapping, Sequence

from bi_agent.data_quality import Span, intersect_spans, subtract_spans

Window = tuple[str, str]

AGGREGATE_METRICS = frozenset({
    "paid_amount", "paid_orders", "erp_documents", "refund_amount",
    "quantity", "product_paid_amount",
    "ad_spend", "visitors", "cart_count",
})
# 分子分母都落在同一张事实表上，公共窗口才有行级同窗的比值。
SAME_SOURCE_RATIO_PARTS: dict[str, tuple[str, ...]] = {
    "aov": ("paid_amount", "paid_orders"),
    "roas": ("paid_amount_sycm", "ad_spend"),
    "ad_spend_ratio": ("ad_spend", "paid_amount_sycm"),
    "pay_conversion_rate": ("paid_buyers", "visitors"),
}
# 分子分母来自不同实体：部分窗口会把两个口径拼成一个数。
CROSS_SOURCE_RATIOS = frozenset({"cash_difference", "cohort_refund_rate"})
# 时点库存没有“覆盖了哪几天就加总哪几天”的含义。
POINT_IN_TIME_METRICS = frozenset({"physical_total", "shop_sellable"})

REASON_COVERAGE_INCOMPLETE = "coverage_incomplete"
REASON_DATA_AS_OF_UNKNOWN = "data_as_of_unknown"
REASON_NOT_DECOMPOSABLE = "basis_incompatible"
REASON_UNKNOWN_METRIC = "capability_unavailable"


@dataclass(frozen=True)
class MetricCoverage:
    """一个成分在请求窗口内已经裁好的覆盖。covered 各自不重叠。"""

    covered: tuple[Window, ...]
    data_as_of: datetime | None


@dataclass(frozen=True)
class AnswerableGroup:
    """同一组已覆盖窗口上可以一起出数的指标。"""

    metrics: tuple[str, ...]
    answered_windows: tuple[Window, ...]
    missing_windows: tuple[Window, ...]


@dataclass(frozen=True)
class PartialDecomposition:
    status: Literal["missing_data", "partial", "complete"]
    groups: tuple[AnswerableGroup, ...]
    refused: tuple[tuple[str, str], ...]


def decompose_partial_answer(
    requested: Window,
    metrics: Sequence[str],
    coverages: Mapping[str, MetricCoverage],
) -> PartialDecomposition:
    """返回可出数的窗口分组，以及每个不能部分出数的指标和稳定原因码。"""
    requested_span = _spans((requested,))
    groups: dict[tuple[Window, ...], list[str]] = {}
    missing_of: dict[tuple[Window, ...], tuple[Window, ...]] = {}
    refused: list[tuple[str, str]] = []
    seen: set[str] = set()
    for metric in metrics:
        if metric in seen:
            continue
        seen.add(metric)
        resolved = _resolve(metric, coverages, requested_span)
        if isinstance(resolved, str):
            refused.append((metric, resolved))
            continue
        answered, missing = resolved
        groups.setdefault(answered, []).append(metric)
        missing_of[answered] = missing
    answerable = tuple(
        AnswerableGroup(metrics=tuple(sorted(names)),
                        answered_windows=windows,
                        missing_windows=missing_of[windows])
        for windows, names in sorted(groups.items(), key=lambda item: item[0])
    )
    refused_sorted = tuple(sorted(refused))
    if not answerable:
        status: Literal["missing_data", "partial", "complete"] = "missing_data"
    elif not refused_sorted and all(not group.missing_windows for group in answerable):
        status = "complete"
    else:
        status = "partial"
    return PartialDecomposition(status=status, groups=answerable, refused=refused_sorted)


def _resolve(metric: str, coverages: Mapping[str, MetricCoverage],
             requested: list[Span]) -> tuple[tuple[Window, ...], tuple[Window, ...]] | str:
    if metric in CROSS_SOURCE_RATIOS or metric in POINT_IN_TIME_METRICS:
        return REASON_NOT_DECOMPOSABLE
    if metric in SAME_SOURCE_RATIO_PARTS:
        parts = _ratio_coverages(metric, coverages)
        if parts is None:
            return REASON_COVERAGE_INCOMPLETE
        if any(part.data_as_of is None for part in parts):
            return REASON_DATA_AS_OF_UNKNOWN
        spans = _spans(parts[0].covered)
        for part in parts[1:]:
            spans = intersect_spans(spans, _spans(part.covered))
        return _clip(spans, requested)
    if metric not in AGGREGATE_METRICS:
        return REASON_UNKNOWN_METRIC
    coverage = coverages.get(metric)
    if coverage is None:
        return REASON_COVERAGE_INCOMPLETE
    if coverage.data_as_of is None:
        return REASON_DATA_AS_OF_UNKNOWN
    return _clip(_spans(coverage.covered), requested)


def _ratio_coverages(metric: str,
                     coverages: Mapping[str, MetricCoverage]) -> tuple[MetricCoverage, ...] | None:
    parts = SAME_SOURCE_RATIO_PARTS[metric]
    found = tuple(coverages[name] for name in parts if name in coverages)
    if len(found) == len(parts):
        return found
    # 调用方已经按同一张表求过交：只给比值指标自己的覆盖也可以。
    own = coverages.get(metric)
    if own is not None and not found:
        return (own,)
    return None


def _clip(spans: Sequence[Span],
          requested: list[Span]) -> tuple[tuple[Window, ...], tuple[Window, ...]] | str:
    covered = intersect_spans(spans, requested)
    if not covered:
        return REASON_COVERAGE_INCOMPLETE
    missing = subtract_spans(requested, covered)
    return _windows(covered), _windows(missing)


def _spans(windows: Sequence[Window]) -> list[Span]:
    return [(date.fromisoformat(start), date.fromisoformat(end))
            for start, end in windows]


def _windows(spans: Sequence[Span]) -> tuple[Window, ...]:
    return tuple((start.isoformat(), end.isoformat()) for start, end in spans)
