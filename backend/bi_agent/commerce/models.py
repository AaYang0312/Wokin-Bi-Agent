"""商品运营域的输入契约与服务端上下文（模型可见部分只有业务字段）。

spec §3：真实身份、授权全集、连接与 deadline 都由服务端上下文提供，
**不进入模型工具参数**。所以本文件里有两个方向完全不同的对象：

- `ProductPerformanceRequest` / `PerformanceComparisonRequest`：给模型的 JSON schema，
  字段全是业务语义，店铺 / 商品只能以 `ent-` 引用出现；两个 Tool 跑同一张经营图
  （spec §6），所以范围 / 指标 / 口径词表也共用同一份；
- `DomainContext`：服务端在请求内组装的上下文，带真实主键、连接与 Store。

两者都不接受 `extra` 字段：多传一个字段就当合法参数放行，等于给绕过白名单开门。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from types import MappingProxyType
from typing import Any, Literal, Mapping
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from bi_agent.catalog import REF_RE
from bi_agent.metrics import MAX_SPAN_DAYS, ToolResult
from bi_agent.sources import registration

from .metrics import (
    COMMERCE_METRICS,
    CommerceMetric,
    ComparisonGroupBy,
    ComparisonMode,
    PLATFORM_GROUP_RULE,
    ProfitBasis,
    SalesBasis,
    ScopeMode,
    TREND_DAYS,
)


_POLICY_REF_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}/[0-9][0-9._-]{0,15}$")


def normalize_platform(value: object) -> str:
    """平台码规范化 + 未登记平台 fail closed。

    只有 `sources` 注册表认识的平台才是平台：不在表里的写法（1688、淘工厂、
    任何中文别名）一律拒绝，不猜名称也不猜能力（spec §3「未知平台不能猜测名称或能力」）。
    """
    code = str(value or "").strip().lower()
    if not code:
        raise ValueError("platform_empty")
    if registration(code) is None:
        raise ValueError("platform_unregistered")
    return code


class ProductSelector(BaseModel):
    """商品选择器：`ref` 与 `text` 二选一，可附 SKU 引用。

    两者都给或都不给都不猜优先级（与 `catalog.resolver.Selector` 同一口径）；
    模型优先传 opaque ref，文本只在服务端用于找候选，不写进运行事件与提示词。
    """

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    ref: str | None = Field(default=None, pattern=REF_RE.pattern,
                            description="ent- 形式的商品引用；不猜、不拼、不用商品号代替")
    text: str | None = Field(default=None, min_length=1, max_length=80)
    sku_refs: list[str] = Field(default_factory=list, max_length=50)

    @model_validator(mode="after")
    def _exactly_one_selector(self) -> "ProductSelector":
        if bool(self.ref) == bool(self.text):
            raise ValueError("product 需要 ref 或 text 二选一（不能同时给）")
        if self.text is not None and not self.text.strip():
            raise ValueError("product.text 不能是空白")
        for ref in self.sku_refs:
            if not REF_RE.fullmatch(str(ref)):
                raise ValueError("sku_refs 只能是 ent- 引用")
        return self


class CommerceScope(BaseModel):
    """分析范围：全授权或显式选定，均不以「有成交」当全集。

    商品报告与对比报告共用这一份范围契约：同一句“这几个平台”，两个 Tool 不许
    解出两套含义。`platforms` 与 `shop_refs` 能否同时给由各个 Tool 自己定：
    商品报告里交集只是“范围空”，对比报告里它会把整组静默拆掉（见
    `PerformanceComparisonRequest`）。
    """

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    mode: ScopeMode = "all_authorized"
    platforms: list[str] = Field(default_factory=list, max_length=20)
    shop_refs: list[str] = Field(default_factory=list, max_length=200)

    @model_validator(mode="after")
    def _selected_needs_targets(self) -> "CommerceScope":
        codes = [normalize_platform(item) for item in self.platforms]
        self.platforms = sorted(set(codes))
        for ref in self.shop_refs:
            if not REF_RE.fullmatch(str(ref)):
                raise ValueError("shop_refs 只能是 ent- 引用")
        self.shop_refs = sorted(set(str(ref) for ref in self.shop_refs))
        if self.mode == "selected" and not (self.platforms or self.shop_refs):
            raise ValueError("selected 范围必须给出 shop_refs 或 platforms")
        return self


class ProductPerformanceRequest(BaseModel):
    """`analyze_product_performance` 的公开输入（spec §4 字段逐项对应）。"""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True,
                              allow_inf_nan=False)

    product: ProductSelector
    scope: CommerceScope = Field(default_factory=CommerceScope)
    start: date
    end: date                       # 排他，沿用固定指标的 [start,end) 口径
    metrics: list[CommerceMetric] = Field(min_length=1)
    sales_basis: SalesBasis = "erp_effective_parent"
    profit_basis: ProfitBasis = "none"
    trend_days: Literal[7] = TREND_DAYS
    comparison: ComparisonMode = "none"
    opportunity_policy_ref: str | None = Field(
        default=None,
        description="版本化低利润策略引用；本轮没有策略库，未配置时只排序不判阈值",
        pattern=_POLICY_REF_RE.pattern)
    currency: Literal["CNY"] = "CNY"

    @model_validator(mode="after")
    def _check_bounds(self) -> "ProductPerformanceRequest":
        if self.end <= self.start:
            raise ValueError("end必须晚于start（排他区间）")
        if (self.end - self.start).days > MAX_SPAN_DAYS:
            raise ValueError(f"日期跨度最多{MAX_SPAN_DAYS}天")
        unknown = sorted({str(metric) for metric in self.metrics} - COMMERCE_METRICS)
        if unknown:
            raise ValueError(f"不支持的商品运营指标：{unknown}")
        self.metrics = sorted(set(str(metric) for metric in self.metrics))
        if self.profit_basis == "none" and any(
                metric.endswith("_reference") for metric in self.metrics):
            # 「按已有表项算参考毛利」是一个必须显式确认的口径选择：
            # 静默替用户决定要不要看毛利，比让他多选一次危险。
            raise ValueError("请求了毛利参考指标，需显式设置 profit_basis=existing_fields")
        return self

    @property
    def trend_window(self) -> tuple[date, date]:
        """趋势区间独立标注为 [end-trend_days, end)，即使主期间比它短。"""
        return (self.end - timedelta(days=TREND_DAYS), self.end)

    @property
    def previous_window(self) -> tuple[date, date] | None:
        if self.comparison != "previous_period":
            return None
        span = self.end - self.start
        return (self.start - span, self.start)

    def normalized(self) -> dict[str, object]:
        """可持久化、可指纹化的规范化请求：只含引用与业务码，不含真实主键。"""
        payload: dict[str, object] = {
            "metrics": list(self.metrics),
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "currency": self.currency,
            "sales_basis": self.sales_basis,
            "profit_basis": self.profit_basis,
            "trend_days": self.trend_days,
            # 持久化沿用固定指标的 compare 键与词表：同一个概念不取两个名字。
            "compare": self.comparison,
            "scope_mode": self.scope.mode,
            "platforms": list(self.scope.platforms),
            "report_kind": "product",
        }
        if self.scope.shop_refs:
            payload["shop_refs"] = list(self.scope.shop_refs)
        if self.product.ref:
            payload["product_ref"] = self.product.ref
        if self.product.sku_refs:
            payload["sku_refs"] = sorted(set(self.product.sku_refs))
        if self.opportunity_policy_ref:
            payload["opportunity_policy_ref"] = self.opportunity_policy_ref
        return payload


class PerformanceComparisonRequest(BaseModel):
    """`compare_performance` 的公开输入（spec §4 Tool 目录逐项对应）。

    与商品报告共用指标 / 口径 / 范围词表，不开放第二套指标名。`group_by=shop`
    要求范围里恰好一个平台：拿一个平台外的店来比“店间差距”，那个差距其实是
    平台差距，所以这一条在入口就拒，而不是迲到图上再静默剔除。
    """

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True,
                              allow_inf_nan=False)

    scope: CommerceScope = Field(default_factory=CommerceScope)
    start: date
    end: date
    group_by: ComparisonGroupBy
    metrics: list[CommerceMetric] = Field(min_length=1)
    sales_basis: SalesBasis = "erp_effective_parent"
    profit_basis: ProfitBasis = "none"
    trend_days: Literal[7] = TREND_DAYS
    currency: Literal["CNY"] = "CNY"

    @model_validator(mode="after")
    def _check_bounds(self) -> "PerformanceComparisonRequest":
        if self.end <= self.start:
            raise ValueError("end必须晚于start（排他区间）")
        if (self.end - self.start).days > MAX_SPAN_DAYS:
            raise ValueError(f"日期跨度最多{MAX_SPAN_DAYS}天")
        unknown = sorted({str(metric) for metric in self.metrics} - COMMERCE_METRICS)
        if unknown:
            raise ValueError(f"不支持的对比指标：{unknown}")
        self.metrics = sorted(set(str(metric) for metric in self.metrics))
        if self.profit_basis == "none" and any(
                metric.endswith("_reference") for metric in self.metrics):
            # 与商品报告同一条规则：要不要看毛利必须是用户显式选过的。
            raise ValueError("请求了毛利参考指标，需显式设置 profit_basis=existing_fields")
        if self.scope.platforms and self.scope.shop_refs:
            # 交集会把“不在该平台”的引用静默抖掉，而分组完整性恰好依赖“这组有几家”。
            raise ValueError("platforms 与 shop_refs 二选一：交集会拆散分组")
        if self.group_by == "shop":
            if len(self.scope.platforms) != 1:
                raise ValueError("shop 分组必须且只能选定一个平台（spec §4）")
            if self.scope.shop_refs:
                raise ValueError("shop 分组按平台展开获准店铺，不接受额外 shop_refs")
        return self

    @property
    def trend_window(self) -> tuple[date, date]:
        """趋势区间独立标注为 [end-trend_days, end)，与商品报告同一规则。"""
        return (self.end - timedelta(days=TREND_DAYS), self.end)

    @property
    def previous_window(self) -> tuple[date, date] | None:
        """本轮对比不做上期环比：它需要自己的取数与覆盖判定，本轮没有取。"""
        return None

    @property
    def group_column(self) -> str:
        """分组键在结果行里的列名（行原文形态）：`shop_id` 或 `platform`。"""
        return "shop_id" if self.group_by == "shop" else "platform"

    @property
    def group_label(self) -> str:
        """分组键在已投影行里的列名（`shop_ref` / `platform`）：图表轴用它。"""
        return "shop_ref" if self.group_by == "shop" else "platform"

    def normalized(self) -> dict[str, object]:
        """可持久化、可指纹化的规范化请求：只含引用与业务码，不含真实主键。"""
        payload: dict[str, object] = {
            "metrics": list(self.metrics),
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "currency": self.currency,
            "group_by": self.group_by,
            "sales_basis": self.sales_basis,
            "profit_basis": self.profit_basis,
            "trend_days": self.trend_days,
            "scope_mode": self.scope.mode,
            "platforms": list(self.scope.platforms),
            "report_kind": "comparison",
        }
        if self.scope.shop_refs:
            payload["shop_refs"] = list(self.scope.shop_refs)
        if self.group_by == "platform":
            # 平台分组规则参与指纹：今天 tb/tm 不合并，明天配了版本化合并规则，
            # 旧结果就不是同一个问题的答案。
            payload["platform_group_rule"] = PLATFORM_GROUP_RULE
        return payload


@dataclass
class DomainContext:
    """一次工具调用的服务端上下文（spec §3 的 DomainContext）。
    `deadline` 是 `time.monotonic()` 上的绝对时刻，与固定指标查询共用主层的 30 秒
    总预算：领域图不另起一份预算，也不在重试时重置它。外层（主层 / 恢复路径）在
    发起一次执行之前可以**收紧**它，图内只读不写：预算只会变小，不会被领域图偷偷重置。
    之所以不是 frozen dataclass：收紧 deadline 正是主层要能做的事（用例钉住“耗尽之后
    仍然拿到 unavailable 而不是空答案”）。
    """

    subject_id: str
    allowed_shop_ids: frozenset[str]
    shop_refs: Mapping[str, str]
    conn: Any
    store: Any
    chat_id: UUID
    user_message_id: UUID
    root_request_id: UUID
    now: datetime
    deadline: float
    attempt_no: int = 1

    @property
    def ref_to_shop_id(self) -> Mapping[str, str]:
        return MappingProxyType({ref: shop_id
                                 for shop_id, ref in self.shop_refs.items()})


@dataclass(frozen=True)
class CommerceDataset:
    """一份要发布的数据集：类型决定它进哪张 Artifact，载荷仍走同一个投影契约。

    `ToolResult` 是既有的结果契约（行、覆盖、口径凭证、披露、血缘批次都在里面）：
    运营面不另立一套结果模型，两套结果模型迟早会在"谁是权威数字"上分叉。
    """

    artifact_type: Literal["metric_result", "trend_series", "comparison_table"]
    result: ToolResult


@dataclass(frozen=True)
class CommerceReport:
    """商品运营图的一次输出：范围、状态、数据集与恢复线索各自分列。

    `status` 是契约 v2 给展示层与模型载荷看的报告状态；`tool_status`（只进运行状态，
    不进报告）沿用固定指标的词表，两者在 `needs_input` 上就是两个词，不合并。
    """

    status: Literal["ok", "partial", "missing_data", "needs_input", "forbidden",
                    "unavailable"]
    datasets: tuple[CommerceDataset, ...] = ()
    requested_scope: Mapping[str, object] = field(
        default_factory=lambda: MappingProxyType({}))
    evaluated_shop_ids: tuple[str, ...] = ()
    excluded_scope: tuple[Mapping[str, object], ...] = ()
    metric_statuses: tuple[Mapping[str, object], ...] = ()
    resolved_product: Mapping[str, object] = field(
        default_factory=lambda: MappingProxyType({}))
    comparison: Mapping[str, object] | None = None
    opportunity: Mapping[str, object] | None = None
    trend_window: tuple[str, str] | None = None
    termination_reason: str | None = None
    # 对比报告（Task 8）专属：分组完整性与排名范围。两者都是“谁进了合计”的证据：
    # 缺了它们，一份只覆盖四个分组的合计就会被读成五个分组的合计。
    group_statuses: tuple[Mapping[str, object], ...] = ()
    ranking: tuple[Mapping[str, object], ...] = ()
    # 已发布完整分组（只有平台分组会填）：与 `requested_scope.platforms` 一比就是
    # “哪个平台没出数”，展示层不需要自己推分组归属。
    evaluated_platforms: tuple[str, ...] = ()
    # 公开披露文本：没有可发数据集时，这是模型还能读到的唯一说明。
    limitations: tuple[str, ...] = ()
    # 歧义 / 参数缺失时的候选卡片：只带引用。候选本身就是按**授权全集**查出来的（见
    # resolve_product_if_needed），所以“有权查看”是构造上成立的，不是事后筛的。
    # 不发名称：needs_input 路径不发 Artifact，也就没有已授权的目录投影可用；
    # spec §3 把“有权查看的候选”当作 needs_input 的一部分：只说“有多个候选”而不给
    # 可选的东西，用户就没法回答那个问题。展示名由已授权解析层换，本层只发引用。
    candidates: tuple[Mapping[str, object], ...] = ()
