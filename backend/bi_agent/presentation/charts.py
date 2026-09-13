"""声明式图表契约（`ChartSpec`）：只描述「哪份已落库数据集的哪一列画成什么图」。

计划 Task 8 / spec §8 的三条边界，写成"会被拒掉的东西"更清楚：

- **不带数据**：图表只引用已持久化数据集（`dataset_ref` + 落库那份的 `data_as_of`
  与 `coverage`），渲染层按引用去同一条消息的 Artifact 里取行。把行抄进图表就有了
  两份"权威数字"，早晚在舍入、过滤与排序上分叉。
- **不带可执行代码**：字段全部是枚举、UUID 串、已登记列名与固定形状的口径签名；
  没有 SVG / HTML / JavaScript 字符串，也没有自由文本通道 —— 模型更不在这条路上。
- **不重算财务口径**：`unit` 必须等于该指标已登记的单位，`metric_basis` 必须是
  `指标|口径|时间归属` 形状的口径签名（与血缘签名同一规则、同一份正则）。

「与数据集同版本」由两件事钉住：构造时 `coverage_ref` 必须与被引用数据集逐字相同；
落库时 `runtime.artifacts.verify_chart_pairing` 再按**已存的行**核一遍（同一运行、
数据集类型、`data_as_of` 与 `coverage` 相同、`chart_version` 是当前契约版本）。

一**指标**一个 `ChartSpec`：把件数、金额、利润率混到同一根轴上，正是 spec §8
点名要防的错。本模块是这份词表的唯一所有者，`runtime.models` 引用它来校验载荷：
依赖方向只能朝这里走，反过来会成环。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Sequence, get_args
from uuid import UUID

from bi_agent.commerce.metrics import (
    CHART_METRIC_UNITS, CHART_SERIES_COLUMNS, CHART_X_COLUMNS, CHART_Y_COLUMNS)
from bi_agent.metrics import Coverage
from bi_agent.runtime.artifacts import (
    BASIS_SIGNATURE_RE, CHART_SPEC_VERSION, DATASET_ARTIFACT_TYPES)

ChartKind = Literal["bar", "line", "scatter"]
ChartUnit = Literal["piece", "CNY", "ratio"]
# 条形图必须零基线（spec §8）；折线允许自适应纵轴，但缺值仍然断开，不连成一条直线。
ChartBaseline = Literal["zero", "auto"]
ChartNullValues = Literal["break", "blank"]

CHART_KINDS: frozenset[str] = frozenset(get_args(ChartKind))
CHART_UNITS: frozenset[str] = frozenset(get_args(ChartUnit))
CHART_BASELINES: frozenset[str] = frozenset(get_args(ChartBaseline))
CHART_NULL_HANDLES: frozenset[str] = frozenset(get_args(ChartNullValues))
# 载荷键集：与数据集载荷完全不相交（这里没有 `status` / `data`），所以
# `artifact_type` 就是唯一的判别位。`currency` 只在金额为 CNY 时出现。
CHART_PAYLOAD_KEYS: frozenset[str] = frozenset({
    "chart_version", "spec_version", "kind", "x", "y", "series", "unit",
    "metric_basis", "dataset_ref", "dataset_type", "dataset_data_as_of",
    "coverage_ref", "coverage_status", "coverage_start", "coverage_end",
    "coverage_gaps", "baseline", "null_values", "currency",
})
CHART_OPTIONAL_PAYLOAD_KEYS: frozenset[str] = frozenset({"currency"})
CHART_REQUIRED_PAYLOAD_KEYS: frozenset[str] = (
    CHART_PAYLOAD_KEYS - CHART_OPTIONAL_PAYLOAD_KEYS)
# 图表契约的名字型版本：与进数据库列的整数（`runtime.artifacts.CHART_SPEC_VERSION`）
# 一起读。形状变了两个都要推进，旧聊天才能按旧形状继续读。
CHART_SPEC_NAME = "chart-spec/1"
# 轴与系列的列名由 `commerce.metrics` 供给（与结果列同一归属）：
# `y` 只能是报告指标 —— 支付面那一列没登记过单位契约，宁可少一张图，
# 也不要一张要靠猜单位才能读的图。
__all__ = [
    "CHART_BASELINES",
    "CHART_KINDS",
    "CHART_NULL_HANDLES",
    "CHART_PAYLOAD_KEYS",
    "CHART_REQUIRED_PAYLOAD_KEYS",
    "CHART_SERIES_COLUMNS",
    "CHART_SPEC_NAME",
    "CHART_UNITS",
    "CHART_X_COLUMNS",
    "CHART_Y_COLUMNS",
    "ChartContractError",
    "ChartKind",
    "ChartSpec",
    "PersistedDataset",
    "build_chart_spec",
]


class ChartContractError(ValueError):
    """图表契约不成立：宁可不画图，也不画一张口径不明或数据版本错位的图。

    `reason` 是固定码表，不拼自由文本：这条异常会被上游按码降级成
    「只发表格 + warning」，把原文拼进去就会变成给用户看的第二份说法。
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class PersistedDataset:
    """一份**已经落库**的数据集：图表只接受这个形状的被引用物。

    之所以不是裸 UUID：把"引用已持久化数据集"编码进类型，调用方就没法拿一个凭空
    造的 id、一份内存里的行集或一个尚未保存的载荷来构造图表。四个字段都取自
    `store.save_artifact` 的返回值与落库那一份载荷，不接受调用方另写一份。
    """

    artifact_id: UUID
    artifact_type: str
    data_as_of: datetime
    coverage: Coverage

    def __post_init__(self) -> None:
        if self.artifact_type not in DATASET_ARTIFACT_TYPES:
            raise ChartContractError("chart_dataset_type_invalid")
        if self.data_as_of.tzinfo is None:
            # 没有时区的"数据截止"会被前端按本地时区读，等于换了一个截止时间。
            raise ChartContractError("chart_dataset_version_invalid")

    def same_version(self, other: "PersistedDataset") -> bool:
        """同一份数据集、同一个数据截止、同一份覆盖：三者缺一就不叫同版本。"""
        return (self.artifact_id == other.artifact_id
                and self.artifact_type == other.artifact_type
                and self.data_as_of == other.data_as_of
                and self.coverage == other.coverage)


def _check(value: str, allowed: frozenset[str], reason: str) -> str:
    # 列名、单位与枚举只允许已登记词表，不用正则放宽：新增一列必须先出现在词表里。
    if value not in allowed:
        raise ChartContractError(reason)
    return value


def build_chart_spec(dataset_ref: PersistedDataset, *, kind: str, x: str, y: str,
                     series: Sequence[str] = (), unit: str, metric_basis: str,
                     coverage_ref: PersistedDataset) -> ChartSpec:
    """把一次「已落库数据集 + 一个指标」换成声明式图表规范。

    门禁逐条对应 spec §8，全部 fail closed：

    1. 被引用的必须是一份**数据集**（类型在 `DATASET_ARTIFACT_TYPES` 里：图表不得
       引用另一张图表，也不得借尚未开放的类型混进来）；
    2. 覆盖必须与数据同源：`coverage_ref` 必须指向同一份、同一版本的数据集，
       否则图上的缺口与数字来自两个数据版本，读起来就是"覆盖了却有洞"；
    3. `y` 必须是已登记指标，`unit` 必须等于该指标登记的单位（不接受调用方自填）；
    4. `metric_basis` 前缀必须就是被画的那个指标：张冠李戴比不画更难被发现；
    5. 轴与系列的列名取自固定词表 —— 条形图的 x 不许是日期（缺失日会被画成
       "没有这根柱子"），折线图的 x 必须是日期。
    """
    _check(kind, CHART_KINDS, "chart_kind_unsupported")
    _check(x, CHART_X_COLUMNS, "chart_axis_column_invalid")
    _check(y, CHART_Y_COLUMNS, "chart_axis_column_invalid")
    if x == y or y in tuple(series):
        # 同一列既当轴又当系列：图上会出现两个"这一列是什么"的答案。
        raise ChartContractError("chart_axis_column_invalid")
    for column in series:
        _check(column, CHART_SERIES_COLUMNS, "chart_series_column_invalid")
    if kind == "line" and x != "day":
        raise ChartContractError("chart_kind_axis_mismatch")
    if kind == "bar" and x == "day":
        raise ChartContractError("chart_kind_axis_mismatch")
    if kind == "scatter":
        # 本轮没有散点图的取数路径（spec §8 把它留给商品机会视图）：不接受，
        # 也不预备 —— 一张没人核验过的图比少一张图危险。
        raise ChartContractError("chart_kind_unsupported")
    if not coverage_ref.same_version(dataset_ref):
        raise ChartContractError("chart_coverage_version_mismatch")
    if unit != CHART_METRIC_UNITS.get(y):
        raise ChartContractError("chart_unit_mismatch")
    if not BASIS_SIGNATURE_RE.fullmatch(metric_basis or ""):
        raise ChartContractError("chart_basis_invalid")
    if metric_basis.split("|", 1)[0] != y:
        raise ChartContractError("chart_basis_metric_mismatch")
    return ChartSpec(
        kind=kind, x=x, y=y, series=tuple(series), unit=unit, metric_basis=metric_basis,
        dataset_ref=dataset_ref, coverage_ref=coverage_ref,
        baseline="zero" if kind == "bar" else "auto",
        null_values="blank" if kind == "bar" else "break")


@dataclass(frozen=True)
class ChartSpec:
    """spec §8 的 `ChartSpec`：`kind/dataset_ref/x/y/series/unit/metric_basis/coverage_ref`。

    构造只有一条路：`build_chart_spec`（它带全套门禁）。本类的 `as_payload` 只做
    序列化，既不补校验也不放松校验 —— 落库那份由 `runtime.models` 再核一次。
    """

    kind: ChartKind
    x: str
    y: str
    series: tuple[str, ...]
    unit: ChartUnit
    metric_basis: str
    dataset_ref: PersistedDataset
    coverage_ref: PersistedDataset
    baseline: ChartBaseline
    null_values: ChartNullValues

    @property
    def dataset_artifact_id(self) -> UUID:
        return self.dataset_ref.artifact_id

    def as_payload(self) -> dict[str, object]:
        """可落库的声明载荷（`runtime.models` 按 chart_spec 形状逐项校验）。

        载荷里的引用全是 UUID 串与已登记列名。展示层按 `dataset_ref` 在同一条消息
        的 Artifact 里找那份数据集，并核对 `dataset_type` 与 `dataset_data_as_of`；
        找不到或版本不合就明说「图表数据集未取得」，不自己拼一份数出来。
        """
        dataset = self.dataset_ref
        coverage = self.coverage_ref
        payload: dict[str, object] = {
            # 进 `bi.query_artifacts.chart_version` 的那个整数：两者必须一致。
            "chart_version": CHART_SPEC_VERSION,
            "spec_version": CHART_SPEC_NAME,
            "kind": self.kind,
            "x": self.x,
            "y": self.y,
            "series": list(self.series),
            "unit": self.unit,
            "metric_basis": self.metric_basis,
            "dataset_ref": str(dataset.artifact_id),
            "dataset_type": dataset.artifact_type,
            "dataset_data_as_of": dataset.data_as_of.isoformat(),
            "coverage_ref": str(coverage.artifact_id),
            "coverage_status": coverage.coverage.status,
            "coverage_start": coverage.coverage.start.isoformat(),
            "coverage_end": coverage.coverage.end.isoformat(),
            "coverage_gaps": list(coverage.coverage.gaps),
            "baseline": self.baseline,
            "null_values": self.null_values,
        }
        if self.unit == "CNY":
            # 金额单位必须带币种：只写 CNY 而不留币种字段，换币种时就没地方说。
            payload["currency"] = "CNY"
        return payload
