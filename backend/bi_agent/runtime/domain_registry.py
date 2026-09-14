"""领域注册表：一个领域能写哪些节点、哪些 Artifact 类型，由这一处决定。

004 的 CHECK 约束只允许 business_query / metric_result。扩展到多领域时必须继续
"白名单可枚举"，不能因为要加领域就把约束拆成任意文本——未知领域、未知节点、
未知 Artifact 类型都要在写库之前就被拒。
"""

from __future__ import annotations

from dataclasses import dataclass

# Artifact 类型白名单（docs/superpowers/specs/2026-09-11-operator-workflows-design.md
# 第 3 节），metric_result 是既有类型，必须继续可用。
ARTIFACT_TYPES = frozenset({
    "metric_result",
    "comparison_table",
    "trend_series",
    "chart_spec",
    "price_audit",
    "inventory_alerts",
})

# 数据集与图表必须成对同版本：chart_spec 只能引用这两类数据集 Artifact。
DATASET_ARTIFACT_TYPES = frozenset({"metric_result", "comparison_table", "trend_series"})


class DomainUnknown(ValueError):
    """未登记领域：拒绝，不给它任何默认能力。"""


@dataclass(frozen=True)
class DomainSpec:
    """一个领域的合法状态节点与可产出的 Artifact 类型集合。"""

    name: str
    nodes: frozenset[str]
    artifact_types: frozenset[str]


# 节点名沿用各自状态机的取值，注册表只列白名单不解释语义，避免同一份节点集合在
# SQL 约束、Pydantic 与这里各抄一份。`runtime.models.PersistenceNode` 是全局可达
# 节点集，本模块的每个集合是其中属于某个领域的那一段。
_BUSINESS_NODES = frozenset({
    "received", "resolve_parameters", "validate_parameters", "authorize_scope",
    "execute_fixed_query", "classify_result", "persist_artifact", "finalize",
})

# 经营图（计划 Task 7、spec §6 的固定节点链）。推进顺序由
# `commerce/graph.py` 的状态机决定，这里只回答"这个领域能不能写这个节点"。
COMMERCE_NODES = frozenset({
    "resolve_scope", "resolve_product_if_needed", "resolve_metric_basis",
    "check_capabilities_and_coverage", "freeze_versions", "plan_fixed_queries",
    "execute_aggregates", "compute_metrics", "build_comparison_and_trend",
    "classify_findings", "persist_artifacts", "finalize",
})
# 上架复核（计划 Task 9、spec §6 的固定节点链）。推进顺序由
# `listing_audit/graph.py` 的状态机决定，这里只回答"这个领域能不能写这个节点"。
# 旧版在这一行挂的是 `_BUSINESS_NODES | {assess_readiness, audit_prices}`——一份
# "还没实现先占位"的白名单；现在它必须与图上的节点枚举逐项相等（由用例比对），
# 因为"领域登记了什么节点"就是它能往运行记录里写什么的边界。
LISTING_NODES = frozenset({
    "resolve_scope_product_and_skus", "load_expected_listing_roster",
    "capture_user_expected_prices", "check_listing_source", "load_listing_snapshot",
    "verify_completeness_and_freshness", "join_expected_and_actual",
    "compare_decimal_prices", "classify_discrepancies", "persist_audit", "finalize",
})
# 库存预警（计划 Task 10、spec §6 的固定节点链）。推进顺序由
# `inventory/graph.py` 的状态机决定，这里只回答"这个领域能不能写这个节点"。
# 与价审图同一做派：必须与图上的节点枚举逐项相等（由用例比对），因为"领域登记了
# 什么节点"就是它能往运行记录里写什么的边界。
INVENTORY_NODES = frozenset({
    "resolve_full_catalog_and_scope", "authorize_inventory_pools",
    "load_inventory_policy", "check_source_capabilities", "load_snapshots",
    "check_completeness_and_freshness", "normalize_units_and_deduplicate_pools",
    "compute_total_and_shop_levels", "evaluate_thresholds", "classify_actions",
    "persist_alerts", "finalize",
})

_REGISTRY: dict[str, DomainSpec] = {
    "business_query": DomainSpec(
        name="business_query",
        nodes=_BUSINESS_NODES,
        artifact_types=frozenset({"metric_result"}),
    ),
    # 后三个领域在此登记契约，实现按计划 Task 7–10 落地；登记即表示
    # “未实现的节点/类型不允许提前写入”，而不是允许任意 payload。
    "commerce_performance": DomainSpec(
        name="commerce_performance", nodes=COMMERCE_NODES,
        artifact_types=frozenset({"metric_result", "comparison_table", "trend_series",
                                  "chart_spec"}),
    ),
    "listing_price_audit": DomainSpec(
        name="listing_price_audit", nodes=LISTING_NODES,
        artifact_types=frozenset({"price_audit"}),
    ),
    # 只登记这张图真写得出的类型。本文件自己的规则就是"未实现的类型不允许提前写入"：
    # 留着 trend_series / chart_spec，等于允许以后有人往预警运行下发一张没有数据集
    # 支撑的图表声明。（价审领域同样只发 price_audit。）
    "inventory_watch": DomainSpec(
        name="inventory_watch", nodes=INVENTORY_NODES,
        artifact_types=frozenset({"inventory_alerts"}),
    ),
}


def domains() -> tuple[str, ...]:
    return tuple(sorted(_REGISTRY))


def known_domain(value: object) -> bool:
    return isinstance(value, str) and value in _REGISTRY


def spec_for(domain: str) -> DomainSpec:
    try:
        return _REGISTRY[domain]
    except KeyError:
        raise DomainUnknown(domain) from None


def allows_artifact_type(domain: str, artifact_type: str) -> bool:
    """领域能否产出这种 Artifact：未知领域与未知类型都直接否。"""
    entry = _REGISTRY.get(domain)
    return entry is not None and artifact_type in entry.artifact_types


def allows_node(domain: str, node: object) -> bool:
    """领域能否把状态推进到这个节点：未知领域与未知节点都直接否。

    与 `allows_artifact_type` 同一条理由——白名单只能在一处枚举，不能因为
    "数据库那一列没有 CHECK"就在运行层放开。
    """
    entry = _REGISTRY.get(domain)
    if entry is None or not isinstance(node, str):
        return False
    return node in entry.nodes


__all__ = ["ARTIFACT_TYPES", "COMMERCE_NODES", "DATASET_ARTIFACT_TYPES", "DomainSpec",
           "DomainUnknown", "INVENTORY_NODES", "LISTING_NODES", "allows_artifact_type",
           "allows_node", "domains", "known_domain", "spec_for"]
