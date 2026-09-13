"""上架复核工具入口与结果投影（模型可见面的唯一出口）。

`audit_listing_prices` 是计划 Task 9 指定的公开接口；`execute_listing_audit_tool`
是主 Agent 侧的适配器。两者都不产生判定：判定来自 `graph.py` 按固定节点链算出的
结果，这里只做引用换面与契约校验。

投影只有两个出口，而且共用同一份校验：

- `model_payload`：给模型的载荷 —— 引用、句柄、金额、状态、缺口码，**没有真实名称**；
- `project_audit`：进 Artifact 的公开载荷 —— 同一份内容加 `entities` 展示名。

两者都过 `runtime.models` 的白名单校验；抛 `ValueError` 时上游一律按 failed 处理，
被拒的数字不回流（与经营图同一条规则）。

为什么不走 `business_query.tool.safe_result_body`：那份投影服务的是"指标行"——
它把 `shop_id`/`product_id` 换成引用、把其余列按指标白名单过一遍。上架复核的行里
还有两类东西它不认识：目录解析不出的**链接句柄**（`lst-`，故意不是 `ent-`）和
必须**以 null 出现**的缺价列。把这两种形状塞进指标投影，就会出现"为了复用而放开
白名单"的那种改动；这里宁可自己写一份窄投影，也不放宽共享投影的任何一条规则。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Mapping

from bi_agent.catalog import Catalog
from bi_agent.catalog.models import DisplayEntity, EntityKind, ref_for_key
from bi_agent.runtime.models import (
    DomainResult, validate_artifact_payload, validate_model_payload)

from .graph import ListingRuntime, run_listing_audit_graph
from .models import DomainContext, ListingAuditReport, ListingPriceAuditRequest

if TYPE_CHECKING:
    from bi_agent.llm import ToolCall

ARTIFACT_TYPE = "price_audit"
# 契约 v2 的附加材料：范围三面与恢复线索（spec §3）。
_EXTRAS = ("requested_scope", "evaluated_scope", "excluded_scope", "resolved_product",
           "termination_reason")


def _body(runtime: ListingRuntime, catalog: Catalog, report: ListingAuditReport,
          *, model_view: bool) -> dict[str, Any]:
    """共享投影体：行换引用、范围换引用、汇总块原样带上（它不含任何真实主键）。"""
    from .graph import audit_rows

    payload: dict[str, Any] = {
        # `status` 说的是"这份复核完成了没有"，不是"有没有发现问题"：mismatch 是一份
        # 成功的复核的业务结论，而缺来源是一份没能完成的复核。两者不能共用一个词。
        "status": report.status if report.status in ("ok", "partial", "missing_data")
        else "failed",
        "audit": dict(report.audit),
        "data": audit_rows(runtime),
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
        payload["entities"] = _entities(runtime, catalog)
        payload["catalog_version"] = catalog.catalog_version
    return payload


def _filters(runtime: ListingRuntime) -> dict[str, Any]:
    """本轮请求的可展示形状：真实主键一个都不进，目标价按本轮原文带。"""
    request = runtime.request
    if request is None:
        return {}
    filters: dict[str, Any] = {
        # 不写 `mode`：那个键在运行契约里已被推广工具占用（"incremental" /
        # "budget_cap" 那一套）。同一个键在两个领域说两件事，正是白名单火不掉的错。
        # 本轮范围形状说在 `requested_scope.mode`，它有自己的词表校验。
        "price_basis": request.price_basis,
        "as_of": request.as_of,
        "currency": request.currency,
        # 拿 `normalized()` 那一份，不拿 `expected_prices` 原列表：去重规则只能有一处。
        # 两者分开写就会出现"指纹里一条、卡片上两条重复声明"，而卡片上的那两条会被
        # 载荷契约当成"同一目标项重复声明"直接拒掉。
        "expected_prices": list(request.normalized()["expected_prices"]),
    }
    if request.scope.platforms:
        filters["platforms"] = list(request.scope.platforms)
    if request.scope.shop_refs:
        filters["shop_refs"] = list(request.scope.shop_refs)
    if runtime.erp_product_id:
        # 只在商品真的解析出来时带引用：None 进 `product_ref` 会被引用校验拒掉，
        # 而那句拒绝会被读成"载荷不安全"，不是"这一格还没确定"。
        filters["product_ref"] = ref_for_key(EntityKind.PRODUCT.value,
                                             runtime.erp_product_id)
    return filters


def _plain(value: Any) -> Any:
    """dataclass / Mapping / tuple 一律换成可 JSON 化的原生结构。"""
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


def _entities(runtime: ListingRuntime, catalog: Catalog) -> list[dict[str, Any]]:
    """展示实体：目录给的店 / 商品名，加上本轮 roster 里那些 SKU 的占位。

    SKU 不进目录解析：规格文本的唯一合法来源是成交快照或商品主档，而上架复核可以
    完全没碰过这两处（新链接没成交）。宁可显示"名称未取得"，也不拿规格文本猜一个
    名字 —— 那是 Task 2 就定下的取用优先级，本域不改写它。
    """
    entities = list(catalog.entities_payload())
    known = {str(entry.get("ref")) for entry in entities}
    for item in runtime.roster:
        ref = item.sku_ref
        if not ref or ref in known:
            continue
        known.add(ref)
        entities.append(DisplayEntity(
            ref=ref, kind=EntityKind.SKU, display_name=None,
            name_source="unresolved").model_dump(mode="json"))
    return entities


def project_audit(runtime: ListingRuntime, catalog: Catalog,
                  report: ListingAuditReport) -> dict[str, Any]:
    """公开 Artifact 载荷（带授权展示名）。"""
    return validate_artifact_payload(_body(runtime, catalog, report, model_view=False),
                                     ARTIFACT_TYPE)


def model_payload(runtime: ListingRuntime, catalog: Catalog,
                  report: ListingAuditReport) -> dict[str, Any]:
    """给模型的载荷：同一份内容，不含展示名与目录版本。"""
    return validate_model_payload(_body(runtime, catalog, report, model_view=True),
                                  ARTIFACT_TYPE)


def audit_listing_prices(request: ListingPriceAuditRequest,
                         context: DomainContext) -> DomainResult:
    """计划 Task 9 的公开接口：按用户本轮指定的目标价复核各店 / 各链接的在售价。

    一次调用就是一次图执行：范围展开、roster、来源门禁、快照读取与逐格判定都在这
    一次里完成，内部节点不消耗额外的模型回合（spec §6）。
    """
    return run_listing_audit_graph(
        request=request, context=context,
        tool_call_id=f"listing:{context.subject_id}").domain_result


class UnknownListingTool(ValueError):
    """不认识的复核工具名：配对只在这张表里成立，不认识就拒。"""

    def __init__(self, name: object) -> None:
        super().__init__(f"listing_tool_unknown:{name}")


# 工具名 → 入参契约。本域目前只有一个公开 Tool；这张表存在的理由是**下一**个
# （Task 10 的库存）不能靠"如果名字不是那个就是它"来路由。
_LISTING_TOOLS: Mapping[str, type] = {
    "audit_listing_prices": ListingPriceAuditRequest,
}


def _request_model_for(name: str) -> type:
    try:
        return _LISTING_TOOLS[name]
    except KeyError:
        raise UnknownListingTool(name) from None


def execute_listing_audit_tool(call: "ToolCall",
                               context: DomainContext) -> Any:
    """主 Agent 适配器：把一条模型工具调用换成一次复核图执行。

    参数解析失败**也**要走图：缺本轮目标价时 `needs_input` 必须留下运行记录与终止
    原因，否则恢复策略只能去猜聊天文本。
    """
    arguments = dict(call.arguments) if call.arguments is not None else None
    error = call.arguments_error
    request: ListingPriceAuditRequest | None = None
    if error is None and arguments is not None:
        try:
            request = _request_model_for(call.name).model_validate(arguments)
        except Exception as exc:  # noqa: BLE001 - 只取首行，不把校验细节发给模型
            error = str(exc).split("\n")[0]
    elif error is None:
        error = "arguments不是对象"
    return run_listing_audit_graph(request=request, context=context,
                                   tool_call_id=call.id, arguments=arguments,
                                   arguments_error=error)


def listing_audit_request_schema() -> dict[str, Any]:
    """给模型的工具 schema：只有业务字段与本轮目标价，真实 ID 与连接信息都不在这里。"""
    return ListingPriceAuditRequest.model_json_schema()


__all__ = ["UnknownListingTool", "audit_listing_prices", "execute_listing_audit_tool",
           "listing_audit_request_schema", "model_payload", "project_audit"]
