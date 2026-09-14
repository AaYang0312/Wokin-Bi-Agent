"""库存预警工具入口与结果投影（模型可见面的唯一出口）。

`inspect_inventory` 是计划 Task 10 指定的公开接口；`execute_inventory_tool` 是主
Agent 侧的适配器。两者都不产生数字：数量只来自 `graph.py` 的去重与求和，这里只做
引用换面与契约校验。

投影只有两个出口，共用同一份校验：

- `model_payload`：给模型的载荷 —— 引用、句柄、数量、阈值、状态、缺口码，**没有真实名称**；
- `project_alerts`：进 Artifact 的公开载荷 —— 同一份内容加 `entities` 展示名。

两者都过 `runtime.models` 的白名单校验；抛 `ValueError` 时上游一律按 failed 处理，
被拒的数字不回流（与经营图 / 价审图同一条规则）。

不走 `business_query.tool.safe_result_body` 的理由与价审图相同：那份投影服务指标行，
而预警行里有它不认识的两类东西——目录解析不出的**池 / 仓库句柄**（`pl-` / `wh-`），
以及必须**以 null 出现**的数量列。把这两种形状塞进指标投影，就会需要"为了复用而放开
白名单"的那种改动；这里宁可自己写一份窄投影，也不放宽共享投影的任何一条规则。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Mapping

from bi_agent.catalog import Catalog
from bi_agent.runtime.models import (
    DomainResult, validate_artifact_payload, validate_model_payload)

from .graph import InventoryRuntime, run_inventory_graph
from .models import DomainContext, InventoryAlertReport, InventoryInspectionRequest

if TYPE_CHECKING:
    from bi_agent.llm import ToolCall

ARTIFACT_TYPE = "inventory_alerts"
# 契约 v2 的附加材料：范围三面与恢复线索（spec §3）。
_EXTRAS = ("requested_scope", "evaluated_scope", "excluded_scope", "resolved_product",
           "termination_reason")


def _body(runtime: InventoryRuntime, catalog: Catalog,
          report: InventoryAlertReport, *, model_view: bool) -> dict[str, Any]:
    """共享投影体：行换引用、范围换引用、汇总块原样带上（它不含任何真实主键）。"""
    from .graph import alert_rows

    payload: dict[str, Any] = {
        # `status` 说的是"这份预警做完没有"：low 与 data_anomaly 都是做完了之后的
        # 业务结论，而缺来源、缺快照才是没能判定。两个声称必须分开说。
        "status": report.status if report.status in ("ok", "partial", "missing_data")
        else "failed",
        "inventory": dict(report.summary),
        "data": alert_rows(runtime),
        "filters": _filters(runtime),
        "limitations": list(report.limitations),
    }
    if report.data_as_of is not None:
        payload["data_as_of"] = report.data_as_of.isoformat()
    for key in _EXTRAS:
        value = getattr(report, key, None)
        if value in (None, {}, ()):
            continue
        payload[key] = _plain(value)
    if not model_view:
        payload["entities"] = catalog.entities_payload()
        payload["catalog_version"] = catalog.catalog_version
    return payload


def _filters(runtime: InventoryRuntime) -> dict[str, Any]:
    """本轮请求的可展示形状：真实主键一个都不进，阈值按本轮原文带。

    不写 `mode`：那个键在运行契约里已被推广工具占用（incremental / budget_cap）。
    本轮范围形状说在 `requested_scope.mode`，它有自己的词表校验。
    """
    request = runtime.request
    if request is None:
        return {}
    filters: dict[str, Any] = {
        "products": request.products,
        "levels": list(request.levels),
        "as_of": request.as_of,
        # 拿 `normalized()` 那一份，不拿原列表：去重规则只能有一处，否则会出现
        # "指纹里一条、卡片上两条重复声明"，而卡片上那两条会被载荷契约直接拒掉。
        "thresholds": list(request.normalized()["thresholds"]),
    }
    if request.threshold_policy_ref:
        filters["threshold_policy_ref"] = request.threshold_policy_ref
    if request.sku_refs:
        filters["sku_refs"] = sorted(set(request.sku_refs))
    if request.product_refs:
        filters["product_refs"] = sorted(set(request.product_refs))
    if request.scope.shop_refs:
        filters["shop_refs"] = list(request.scope.shop_refs)
    if request.scope.platforms:
        filters["platforms"] = list(request.scope.platforms)
    return filters


def _plain(value: Any) -> Any:
    """dataclass / Mapping / tuple 一律换成可 JSON 化的原生结构。"""
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


def project_alerts(runtime: InventoryRuntime, catalog: Catalog,
                   report: InventoryAlertReport) -> dict[str, Any]:
    """公开 Artifact 载荷（带授权展示名）。"""
    return validate_artifact_payload(_body(runtime, catalog, report, model_view=False),
                                     ARTIFACT_TYPE)


def model_payload(runtime: InventoryRuntime, catalog: Catalog,
                  report: InventoryAlertReport) -> dict[str, Any]:
    """给模型的载荷：同一份内容，不含展示名与目录版本。"""
    return validate_model_payload(_body(runtime, catalog, report, model_view=True),
                                  ARTIFACT_TYPE)


def inspect_inventory(request: InventoryInspectionRequest,
                      context: DomainContext) -> DomainResult:
    """计划 Task 10 的公开接口：一次调用拿到去重后的实物量与各店可售量两级预警。

    一次调用就是一次图执行：全集展开、池授权、两批快照、去重求和与阈值判定都在这
    一次里完成，内部节点不消耗额外的模型回合（spec §6）。
    """
    return run_inventory_graph(
        request=request, context=context,
        tool_call_id=f"inventory:{context.subject_id}").domain_result


class UnknownInventoryTool(ValueError):
    """不认识的库存工具名：配对只在这张表里成立，不认识就拒。"""

    def __init__(self, name: object) -> None:
        super().__init__(f"inventory_tool_unknown:{name}")


# 工具名 → 入参契约。本域目前只有一个公开 Tool；这张表存在的理由是下一个 Tool
# 不能靠"名字不是那个就是它"来路由。
_INVENTORY_TOOLS: Mapping[str, type] = {
    "inspect_inventory": InventoryInspectionRequest,
}


def _request_model_for(name: str) -> type:
    try:
        return _INVENTORY_TOOLS[name]
    except KeyError:
        raise UnknownInventoryTool(name) from None


def execute_inventory_tool(call: "ToolCall", context: DomainContext) -> Any:
    """主 Agent 适配器：把一条模型工具调用换成一次预警图执行。

    参数解析失败**也**要走图：`needs_input` 必须留下运行记录与终止原因，否则恢复
    策略只能去猜聊天文本。
    """
    arguments = dict(call.arguments) if call.arguments is not None else None
    error = call.arguments_error
    request: InventoryInspectionRequest | None = None
    if error is None and arguments is not None:
        try:
            request = _request_model_for(call.name).model_validate(arguments)
        except Exception as exc:  # noqa: BLE001 - 只取首行，不把校验细节发给模型
            error = str(exc).split("\n")[0]
    elif error is None:
        error = "arguments不是对象"
    return run_inventory_graph(request=request, context=context,
                               tool_call_id=call.id, arguments=arguments,
                               arguments_error=error)


def inventory_request_schema() -> dict[str, Any]:
    """给模型的工具 schema：只有业务字段与本轮阈值，真实 ID 与池授权都不在这里。"""
    return InventoryInspectionRequest.model_json_schema()


__all__ = ["UnknownInventoryTool", "execute_inventory_tool", "inspect_inventory",
           "inventory_request_schema", "model_payload", "project_alerts"]
