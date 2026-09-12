"""Safe projections for business-query tool results.

This module deliberately depends on the request-scoped ``Catalog`` rather than
``SessionState`` so the state graph and the legacy Agent share one projection.
The catalog is the only place that turns real ERP keys into opaque refs and real
names, so neither view can leak an identifier by accident.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from bi_agent.metrics import ToolResult
from bi_agent.runtime.models import (
    ARTIFACT_FILTER_COLUMNS,
    ARTIFACT_RESULT_COLUMNS,
    DomainResult,
    validate_artifact_payload,
    validate_model_payload,
)

from .state import BusinessQueryContext, BusinessQueryExecution, BusinessQueryInput

if TYPE_CHECKING:
    from bi_agent.catalog import Catalog
    from bi_agent.llm import ToolCall

# 白名单单一定义在 runtime/models.py（推广列由 promotion.py 供给），
# 这里只引用，避免第二份手抄集合与校验端漂移。
_PUBLIC_RESULT_COLUMNS = ARTIFACT_RESULT_COLUMNS
_PUBLIC_FILTER_COLUMNS = ARTIFACT_FILTER_COLUMNS


def to_model_result(result: ToolResult, catalog: "Catalog") -> dict[str, object]:
    """Return the validated ref-only payload permitted for the model."""
    payload = _safe_result(result, catalog, model_view=True)
    return validate_model_payload(payload)


def to_public_artifact(result: ToolResult, catalog: "Catalog") -> dict[str, object]:
    """Return the validated public artifact payload: refs plus authorized names."""
    payload = _safe_result(result, catalog, model_view=False)
    return validate_artifact_payload(payload)


def run_business_query(
    conn: object,
    store: object,
    tool_input: BusinessQueryInput,
    context: BusinessQueryContext,
) -> DomainResult:
    """Execute one graph-backed query and expose only its public domain result."""
    from .graph import _execute_business_query_graph

    return _execute_business_query_graph(conn, store, tool_input, context).domain_result


def execute_business_query_tool(
    call: "ToolCall",
    session_state: object,
    context: BusinessQueryContext,
    conn: object,
    store: object,
) -> BusinessQueryExecution:
    """Adapt a legacy model tool call to the deterministic query graph.

    Real identifiers stay in the request-local context and the returned
    ``session_filters``; persisted graph state only receives safe aliases.
    """
    from .graph import _execute_business_query_graph

    refs = getattr(session_state, "shop_refs", {})
    if not isinstance(refs, dict):
        refs = {}
    graph_context = BusinessQueryContext(
        chat_id=context.chat_id,
        user_message_id=context.user_message_id,
        subject_id=context.subject_id,
        question=context.question,
        previous_filters=dict(context.previous_filters),
        shop_refs={str(key): str(value) for key, value in refs.items()},
        allowed_shop_ids=context.allowed_shop_ids,
        now=context.now,
        deadline=context.deadline,
        attempt_no=context.attempt_no,
    )
    return _execute_business_query_graph(
        conn,
        store,
        BusinessQueryInput(
            tool_call_id=call.id,
            arguments=dict(call.arguments) if call.arguments is not None else None,
            arguments_error=call.arguments_error,
        ),
        graph_context,
    )


def _safe_result(
    result: ToolResult, catalog: "Catalog", *, model_view: bool
) -> dict[str, object]:
    """Keep allowlisted aggregates and replace every ERP identifier with its ref.

    Real names never enter the model view; they ride along only in the public
    artifact as ``entities``. Unknown or unauthorized identifiers raise
    ``CatalogUnauthorized`` so the caller fails closed instead of guessing.
    """
    payload = result.model_dump(mode="json")

    rows: list[dict[str, object]] = []
    raw_rows = payload.get("data")
    if isinstance(raw_rows, list):
        for row in raw_rows:
            if not isinstance(row, dict):
                continue
            # 白名单里没有 shop_id/product_id：真实主键在这一步自然被丢掉。
            clean = {key: value for key, value in row.items()
                     if key in _PUBLIC_RESULT_COLUMNS}
            if row.get("shop_id") is not None:
                clean["shop_ref"] = catalog.shop_ref(str(row["shop_id"]))
            if row.get("product_id") is not None:
                clean["product_ref"] = catalog.product_ref(str(row["product_id"]))
            rows.append(clean)

    filters: dict[str, object] = {}
    raw_filters = payload.get("filters")
    if isinstance(raw_filters, dict):
        filters = {
            key: value for key, value in raw_filters.items()
            if key in _PUBLIC_FILTER_COLUMNS
        }
        raw_shops = raw_filters.get("shop_ids")
        if isinstance(raw_shops, list):
            filters["shop_refs"] = [catalog.shop_ref(str(shop_id)) for shop_id in raw_shops]

    # 口径凭证：内部带真实 shop_id 与来源，公开形状只留 shop_ref + 口径 + 时间归属。
    # 模型必须看得见口径，否则它会把同名指标当同义词去汇总、比较与排名。
    basis: list[dict[str, object]] = []
    raw_basis = payload.get("basis")
    if isinstance(raw_basis, list):
        for item in raw_basis:
            if not isinstance(item, dict):
                continue
            entry = {
                "metric": item.get("metric"),
                "basis": item.get("basis"),
                "time_basis": item.get("time_basis"),
                "metric_version": item.get("metric_version"),
            }
            if item.get("shop_id") is not None:
                entry["shop_ref"] = catalog.shop_ref(str(item["shop_id"]))
            basis.append(entry)

    raw_diagnostics = payload.get("diagnostics")
    diagnostics = raw_diagnostics if isinstance(raw_diagnostics, dict) else {}

    body: dict[str, object] = {
        "status": payload.get("status"),
        "metric_definition": payload.get("metric_definition"),
        "coverage": payload.get("coverage"),
        "limitations": payload.get("limitations"),
        "data_as_of": payload.get("data_as_of"),
        "filters": filters,
        "data": rows,
        "basis": basis,
        "diagnostics": diagnostics,
    }
    if not model_view:
        body["entities"] = catalog.entities_payload()
        body["catalog_version"] = catalog.catalog_version
    return body
