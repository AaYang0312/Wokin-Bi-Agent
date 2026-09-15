"""approved 记忆写入前的请求净化（计划 2026-09-14-approved-query-memory.md Task 2）。

两层各挡各的：`models` 的递归契约挡形状与绑定业务值（任意深度的键与标量）；
本模块在它之上把「稳定」落到实处——kebab 形状的引用必须真的登记在当前已发布
语义目录里，业务码必须是既有封闭词表的成员。只靠拼形状的 `sep-01`、
`select-drop` 在 Task 1 契约层合法，在这里被拒收：写入路径不接受形状正确但
目录里不存在的发明引用。

白名单与剥离清单逐字取自计划 Task 2：日期区间、店铺选择、目标价、阈值、预算
先剥掉（它们只能以槽位存在，槽位合法性由模板/QuerySlot 契约把守）；剩余顶层键
必须全部落在白名单内——运行时专有键（top_n、expected_prices、thresholds 等）
一律 `memory_request_field_unapproved`。白名单不因当前运行记录的形状而放宽。
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from bi_agent.catalog import REF_RE
from bi_agent.semantic_catalog.registry import CATALOG, catalog_indexes

from .models import (
    BUSINESS_CODES,
    _MEMORY_REF_RE,
    _reject_bound_values,
)

# 计划 Task 2 的白名单：顶层键只允许这十三个。这份顺序同时是 intent_signature
# 的规范化顺序（repository 与这里共用，避免两处各抄一份键序）。
ALLOWED_REQUEST_KEYS: tuple[str, ...] = (
    "metrics", "requested_metric_refs", "group_by_field_refs", "sales_basis",
    "profit_basis", "currency", "compare", "scope_mode", "report_kind",
    "platforms", "product_ref", "sku_refs", "semantic_selection")
_ALLOWED = frozenset(ALLOWED_REQUEST_KEYS)

# 计划 Task 2 的剥离清单：一次性业务值只能以槽位存在，不进长期记忆。
_STRIP_KEYS: tuple[str, ...] = (
    "start", "end", "date", "shop_refs", "target_price", "thresholds", "budget")

# 当前已发布目录的 ref 索引。CATALOG 是导入即校验的冻结快照，索引随之只读；
# 记忆里登记的引用以它为词表，版本由 repository 冻结成同一个 CATALOG.version。
_INDEXES = catalog_indexes(CATALOG)


def _registered(value: str) -> bool:
    """kebab 引用是否登记在当前目录的五类对象里（实体/字段/指标/视图/连接）。"""
    return (value in _INDEXES.entities or value in _INDEXES.fields
            or value in _INDEXES.metrics or value in _INDEXES.views
            or value in _INDEXES.joins)


def _require_registered_refs(node: Any) -> None:
    """第二个遍历：形状合法的 kebab 引用必须真的登记在已发布目录里。

    走到字符串分支的值都已通过 Task 1 契约——`ent-` 引用与已登记业务码直接放行；
    剩下只可能是 kebab 形状，未登记即拒绝，沿用 Task 1 的稳定原因码：一个目录里
    不存在的引用不是稳定引用。
    """
    if isinstance(node, dict):
        for value in node.values():
            _require_registered_refs(value)
    elif isinstance(node, (list, tuple)):
        for item in node:
            _require_registered_refs(item)
    elif isinstance(node, str):
        if REF_RE.fullmatch(node) or node in BUSINESS_CODES:
            return
        if not _registered(node):
            raise ValueError("memory_value_not_a_stable_ref_or_code")


def validate_stable_refs_and_codes(clean: dict[str, object]) -> None:
    """形状检查（Task 1 契约原样复用）+ 登记检查（Task 2 的写入路径加严）。"""
    _reject_bound_values(clean)
    _require_registered_refs(clean)


def sanitize_normalized_request(value: dict[str, object], *,
                                slots: tuple) -> dict[str, object]:
    """把一次成功运行的规范化请求净化成可入记忆的形状。

    `slots` 是计划签名的显式参数：槽位清单与请求净化各自独立，槽位的形状与
    模板占位由 QuerySlot/模板契约把守，净化器不读也不改写槽位。
    """
    clean = deepcopy(value)
    for key in _STRIP_KEYS:
        clean.pop(key, None)
    if set(clean) - _ALLOWED:
        raise ValueError("memory_request_field_unapproved")
    validate_stable_refs_and_codes(clean)
    return clean
