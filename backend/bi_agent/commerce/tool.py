"""商品运营工具入口与结果投影（模型可见面的唯一出口）。

`analyze_product_performance` 是计划 Task 7 指定的公开接口；`execute_commerce_tool`
是主 Agent 侧的适配器。两者都不产生数字：数字来自 `graph.py` 的确定性聚合，这里只做
引用换面与契约校验。

投影只有两个出口，而且共用同一份校验：

- `model_payload`：给模型的载荷 —— 引用、数值、口径、缺口码，**没有真实名称**；
- `project_dataset`：进 Artifact 的公开载荷 —— 同一份内容加 `entities` 展示名。

两者都过 `runtime.models` 的白名单校验；抛 `ValueError` 时上游一律按 failed 处理，
被拒的数字不回流（与 business_query 同一条规则）。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Mapping

from bi_agent.business_query.tool import safe_result_body
from bi_agent.catalog import Catalog
from bi_agent.commerce.metrics import COMMERCE_METRIC_UNITS
from bi_agent.runtime.models import (
    DomainResult, validate_artifact_payload, validate_model_payload)

from .graph import CommerceExecution, run_commerce_graph
from .models import (
    CommerceDataset,
    CommerceReport,
    DomainContext,
    PerformanceComparisonRequest,
    ProductPerformanceRequest,
)

if TYPE_CHECKING:
    from bi_agent.llm import ToolCall

# 商品运营契约 v2 的附加字段：spec §3 逐条列出的范围 / 状态 / 血缘材料。
_EXTRAS = ("requested_scope", "evaluated_scope", "excluded_scope", "metric_statuses",
           "group_statuses", "ranking", "resolved_product", "comparison", "opportunity",
           "trend_window", "metric_units", "termination_reason")


def _body(dataset: CommerceDataset, catalog: Catalog, report: CommerceReport,
          *, model_view: bool) -> dict[str, Any]:
    """共享投影（行 -> 引用、filters -> shop_refs、basis 去主键）+ 运营面附加材料。"""
    body = safe_result_body(dataset.result, catalog, model_view=model_view)
    # 报告状态才是“这份结果能不能当完整结果用”：数据集自身的 `ok` 只说明它那几行
    # 算得出来。两家可算一家缺口时，两份载荷都必须写 partial，不然展示层会拿
    # 着一份“合计”当成全量合计。`forbidden`/故障路径不发数字，也就不进这里。
    body["status"] = report.status if report.status in ("ok", "partial") else "ok"
    evaluated: dict[str, Any] = {"shop_refs": [
        catalog.shop_ref(shop) for shop in report.evaluated_shop_ids]}
    # 平台分组：把“哪些平台真的出了完整分组”与 shop_refs 并列给出。
    # 与 `requested_scope.platforms` 一比就知道哪个平台没数，不靠展示层推分组归属。
    if report.evaluated_platforms:
        evaluated["platforms"] = list(report.evaluated_platforms)
    body["evaluated_scope"] = evaluated
    for key in _EXTRAS:
        if key == "evaluated_scope":
            continue
        value = getattr(report, key, None)
        if value in (None, {}, ()):
            continue
        body[key] = _plain(value)
    # 模型必须看得见单位：同名指标既可能是件也可能是元，靠口径文本认单位会认错。
    units = {str(metric): COMMERCE_METRIC_UNITS[str(metric)]
             for metric in dataset.result.metric_definition
             if str(metric) in COMMERCE_METRIC_UNITS}
    if units:
        body["metric_units"] = units
    return body


def _plain(value: Any) -> Any:
    """dataclass / Mapping / tuple 一律换成可 JSON 化的原生结构。"""
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


def project_dataset(dataset: CommerceDataset, catalog: Catalog,
                    report: CommerceReport) -> dict[str, Any]:
    """一份数据集的公开 Artifact 载荷（带授权展示名）。"""
    return validate_artifact_payload(_body(dataset, catalog, report, model_view=False))


def model_payload(report: CommerceReport, catalog: Catalog) -> dict[str, Any]:
    """给模型的载荷：取主数据集，附范围 / 状态 / 恢复线索，不含展示名。"""
    if not report.datasets:
        raise ValueError("commerce_dataset_missing")
    return validate_model_payload(_body(report.datasets[0], catalog, report,
                                        model_view=True))


def analyze_product_performance(request: ProductPerformanceRequest,
                                context: DomainContext) -> DomainResult:
    """计划 Task 7 的公开接口：一次调用拿到商品跨店指标表、七日趋势与毛利参考。

    一次调用就是一次图执行：汇总、趋势与候选在同一份冻结数据里生成，内部节点不消耗
    额外的模型回合（spec §6）。
    """
    return run_commerce_graph(report_kind="product", request=request, context=context,
                              tool_call_id=f"commerce:{context.subject_id}").domain_result


class UnknownCommerceTool(ValueError):
    """不认识的运营工具名：报告种类与入参契约的配对只在这张表里成立，不认识就拒。"""

    def __init__(self, name: object) -> None:
        super().__init__(f"commerce_tool_unknown:{name}")


# 工具名 → (报告种类, 入参契约)。两个公开 Tool 跑同一张图，但入参不同：配对收在这一处，
# 就不会出现"拿商品请求去跑对比报告"那种发出一份看起来是对比表的商品表的错。
_COMMERCE_TOOLS: Mapping[str, tuple[str, type]] = {
    "analyze_product_performance": ("product", ProductPerformanceRequest),
    "compare_performance": ("comparison", PerformanceComparisonRequest),
}


def _pair_for(name: str) -> tuple[str, type]:
    try:
        return _COMMERCE_TOOLS[name]
    except KeyError:
        raise UnknownCommerceTool(name) from None


def _report_kind_for(name: str) -> str:
    return _pair_for(name)[0]


def _commerce_model_for(name: str) -> type:
    return _pair_for(name)[1]


def compare_performance(request: PerformanceComparisonRequest,
                       context: DomainContext) -> DomainResult:
    """计划 Task 8 的公开接口：一次调用拿到平台 / 店铺对比表、可选趋势与图表。

    与商品报告共用同一张图、同一次集合查询与同一套门禁：`report_kind=comparison`
    只换分组粒度，不另开一条执行路径，也不按店铺循环（spec §2、§6）。
    下钻（点一个平台看它各店）是一次**新**的 compare_performance：范围、窗口与
    口径按本次入参重新过授权，不沿用上一次的已评估集合。
    """
    return run_commerce_graph(report_kind="comparison", request=request,
                              context=context,
                              tool_call_id=f"commerce:{context.subject_id}").domain_result


def execute_commerce_tool(call: "ToolCall", context: DomainContext) -> CommerceExecution:
    """主 Agent 适配器：把一条模型工具调用换成一次经营图执行。

    参数解析失败**也**要走图：needs_input 必须留下运行记录与终止原因，否则恢复策略
    只能去猜聊天文本（旧 query_business 就是这条契约）。
    """
    arguments = dict(call.arguments) if call.arguments is not None else None
    error = call.arguments_error
    request: ProductPerformanceRequest | PerformanceComparisonRequest | None = None
    if error is None and arguments is not None:
        try:
            request = _commerce_model_for(call.name).model_validate(arguments)
        except Exception as exc:  # noqa: BLE001 - 只取首行，不把校验细节发给模型
            error = str(exc).split("\n")[0]
    elif error is None:
        error = "arguments不是对象"
    return run_commerce_graph(report_kind=_report_kind_for(call.name),
                              request=request, context=context,
                              tool_call_id=call.id, arguments=arguments,
                              arguments_error=error)


def comparison_request_schema() -> dict[str, Any]:
    """`compare_performance` 的模型可见 schema：分组维度与范围同样只有业务码与引用。"""
    return PerformanceComparisonRequest.model_json_schema()


def commerce_request_schema() -> dict[str, Any]:
    """给模型的工具 schema：只有业务字段，真实 ID 与连接信息都不在这里。"""
    return ProductPerformanceRequest.model_json_schema()


__all__ = ["analyze_product_performance", "commerce_request_schema",
           "comparison_request_schema", "compare_performance",
           "execute_commerce_tool", "model_payload", "project_dataset"]
