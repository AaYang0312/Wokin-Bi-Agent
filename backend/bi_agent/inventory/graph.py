"""InventoryWatchGraph：spec §6 的固定节点链（运营工作流计划 Task 10）。

    resolve_full_catalog_and_scope → authorize_inventory_pools → load_inventory_policy
    → check_source_capabilities → load_snapshots → check_completeness_and_freshness
    → normalize_units_and_deduplicate_pools → compute_total_and_shop_levels
    → evaluate_thresholds → classify_actions → persist_alerts → finalize

五条结构性约束，每一条都对应一种会说谎的失败方式：

1. **实物按 (账号范围, 池, 仓库, SKU, 批次, 单位) 去重，只算一次**。同一仓库 100 件被
   三家店各展示 100 时，实物总量仍是 100：所以汇总身份里根本没有"店铺"这一维（池与店
   的关系另有其表，只用来决定谁能看）。同键不同数量、同键不同单位都是冲突——既不取
   第一个、也不取平均。
2. **两个口径永不相加**。`physical_total` 回答"要不要补货"，`shop_sellable` 回答
   "要不要调配额"：来源、批次、时效、阈值档位各自一套。把三家店的渠道显示数加成
   300 件实物，是本任务点名要防的第一种错。
3. **库存池授权独立于店铺授权**。未获准的池不进总量也不进明细，只以句柄出现在
   `excluded_scope`：让经营者知道"有个池没算进来"是必要的，交出真实池号不是。
4. **来源门禁按口径分别成立**。历史核查只验证过部分 ERP SKU / 仓库样本，渠道可售要
   独立取证（spec §9），所以交付时两条都是空的：真实部署只能报 `unsupported`，
   并且一个数量都不发。
5. **全商品盘点先扫完再截展示**。`scanned_items` / `expected_items` 是扫描事实，
   `truncated` 只描述展示；有截断时 `all_safe` 永远不成立——没展示不等于没风险。

首版是用户发起的只读检查：图上没有任何采购、库存调整或通知节点（spec §6）。
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import StrEnum
from typing import Any, Callable, Mapping, Sequence

import psycopg

from bi_agent.business_query.graph import _finish_run_as_failed, _persist_transition
from bi_agent.business_query.nodes import _event_payload
from bi_agent.business_query.state import BusinessQueryState
from bi_agent.catalog import build_catalog
from bi_agent.catalog.models import EntityKind, ref_for_key
from bi_agent.catalog.resolver import ProductResolution, Selector, resolve_product
from bi_agent.metrics import Coverage, ToolResult, _BudgetExhausted, read_only_snapshot
from bi_agent.runtime.artifacts import QueryProvenance, RequestIdentity
from bi_agent.runtime.models import (
    DomainArtifact,
    DomainResult,
    DomainStatus,
    ErrorEnvelope,
    NewArtifact,
    NewQueryRun,
    RecoveryAction,
    RunCompletion,
    RunStatus,
)

from . import repository
from .models import (
    ChannelRow,
    DomainContext,
    InventoryAlertReport,
    InventoryInspectionRequest,
    InventoryPool,
    PhysicalRow,
    StockAlertRow,
    Threshold,
)
from .rules import (
    AUDIT_LEVELS,
    INVENTORY_GRAPH_VERSION,
    INVENTORY_METRIC_VERSION,
    INVENTORY_RULE_VERSION,
    INVENTORY_SCHEMA_VERSION,
    INVENTORY_TEMPLATE_ID,
    INVENTORY_TEMPLATE_VERSION,
    JUDGED_STATUSES,
    MAX_DISPLAY_ITEMS,
    MAX_EXPECTED_ITEMS,
    STATUS_ANOMALY,
    STATUS_LOW,
    STATUS_RISK_ORDER,
    STATUS_NORMAL,
    STATUS_STALE,
    STATUS_UNKNOWN,
    STATUS_UNSUPPORTED,
    UNIT_CONVERSION_REGISTRY_VERSION,
    InventoryConflict,
    amount_sum,
    beijing_iso,
    classify_inventory,
    freshness_ok,
    inventory_limitation_codes,
    normalize_quantity,
    pool_handle,
    quantity_precision,
    sum_unique_physical_stock,
    verified_inventory_source,
)

DOMAIN = "inventory_watch"

TEXT_SOURCE_UNVERIFIED = "库存来源尚未取证，本次两级预警不能判定"
TEXT_PHYSICAL_UNVERIFIED = "实物库存来源尚未取证，本次不能出补货候选"
TEXT_CHANNEL_UNVERIFIED = "渠道可售库存来源尚未取证，本次不能出配额候选"
TEXT_POOL_SNAPSHOT_MISSING = "本轮没有该库存池的实物快照，未判定项保持未知"
TEXT_CHANNEL_SNAPSHOT_MISSING = "本轮没有该店铺的渠道可售快照，未判定项保持未知"
TEXT_STALE = "快照已超过该来源的时效策略，按过期披露，不判安全"
TEXT_SCAN_INCOMPLETE = "扫描分页未取尽，不能声称看完全目录"
TEXT_QUANTITY_CONFLICT = "同一库存身份出现两个不同数量，按数据异常处理，不任选其一"
TEXT_UNIT_CONFLICT = "同一库存身份挂了两种单位且没有已登记的换算表，不合成一个总数"
TEXT_NEGATIVE = "负库存按数据异常处理，不生成补货候选"
TEXT_THRESHOLD_MISSING = "缺少版本化阈值配置，本轮不作安全判定"
TEXT_THRESHOLD_CONFLICT = "本轮阈值与已配置策略同时给出，请先确认按哪一套"
TEXT_SCAN_TRUNCATED = "期望项超出可扫描上限，已拒绝出数以避免静默截断"
TEXT_DISPLAY_TRUNCATED = "展示已按风险截断，未展示的高风险项仍在预警集合里"
TEXT_POOL_UNAUTHORIZED = "部分库存池不在本轮授权范围内，未计入总量"
TEXT_SCOPE_EMPTY = "授权范围内没有可检查的店铺"
TEXT_SKU_UNRESOLVED = "本轮授权范围内没有解析出该 SKU，不能当成 0 库存"
TEXT_UNIVERSE_FROM_SNAPSHOT = ("全商品集合本轮只能由已授权快照批次派生，"
                               "未覆盖无库存记录的 SKU")
TEXT_REPLENISH = "存在低于阈值的实物库存，给出补货候选"
TEXT_QUOTA = "存在渠道配额缺口，给出店铺配额调整候选"
TEXT_INCOMPLETE = "期望项未全部判定，不能声称全部安全"

# 风险顺序由词表持有（`rules.STATUS_RISK_ORDER`），这里只换成可排序的下标：
# 在图上再抄一份字面量，两份顺序迟早会漂移，而漂移的结果是 Top N 先截掉坏消息。
_STATUS_RISK: dict[str, int] = {status: index
                                for index, status in enumerate(STATUS_RISK_ORDER)}


class InventoryNode(StrEnum):
    RESOLVE_FULL_CATALOG_AND_SCOPE = "resolve_full_catalog_and_scope"
    AUTHORIZE_INVENTORY_POOLS = "authorize_inventory_pools"
    LOAD_INVENTORY_POLICY = "load_inventory_policy"
    CHECK_SOURCE_CAPABILITIES = "check_source_capabilities"
    LOAD_SNAPSHOTS = "load_snapshots"
    CHECK_COMPLETENESS_AND_FRESHNESS = "check_completeness_and_freshness"
    NORMALIZE_UNITS_AND_DEDUPLICATE_POOLS = "normalize_units_and_deduplicate_pools"
    COMPUTE_TOTAL_AND_SHOP_LEVELS = "compute_total_and_shop_levels"
    EVALUATE_THRESHOLDS = "evaluate_thresholds"
    CLASSIFY_ACTIONS = "classify_actions"
    PERSIST_ALERTS = "persist_alerts"
    FINALIZE = "finalize"


_ORDER: tuple[InventoryNode, ...] = tuple(InventoryNode)
_NEXT_NODE: dict[InventoryNode | None, InventoryNode] = {None: _ORDER[0]}
_NEXT_NODE.update({_ORDER[index]: _ORDER[index + 1]
                   for index in range(len(_ORDER) - 1)})


class InvalidInventoryTransition(Exception):
    def __init__(self) -> None:
        super().__init__("invalid_inventory_transition")


class InvalidInventoryRequest(ValueError):
    """入参契约不匹配：拿别的领域的请求跑这张图，会发出一份形状正确的假预警表。"""

    def __init__(self) -> None:
        super().__init__("inventory_watch_request_required")


class InventoryState(BusinessQueryState):
    """同一份可持久化状态契约，只换节点类型。"""

    node: InventoryNode = InventoryNode.RESOLVE_FULL_CATALOG_AND_SCOPE


def transition_state(state: InventoryState, next_node: InventoryNode) -> InventoryState:
    """只允许沿链前进一步：跳格与回退都拒，否则"哪一格没执行"就没法回答。"""
    expected = _NEXT_NODE.get(state.node)
    if expected is None or expected is not next_node:
        raise InvalidInventoryTransition()
    return state.model_copy(update={"node": next_node})


@dataclass
class SkuGroup:
    """一个实物汇总身份 `(池, 仓库, SKU)` 在本轮的去重结果。"""

    namespace: str
    pool_ref: str
    warehouse_ref: str
    erp_sku_id: str
    sku_ref: str
    quantity: str | None = None
    unit: str = "piece"
    batch_count: int = 0
    status: str = STATUS_UNKNOWN
    fresh: bool = False
    scanned: bool = False
    captured_at: datetime | None = None


@dataclass
class InventoryRuntime:
    """图内可变状态：真实主键只活在这里，不进 Store、不进事件、不进模型载荷。"""

    state: InventoryState
    context: DomainContext
    tool_call_id: str
    request: InventoryInspectionRequest | None = None
    report: InventoryAlertReport | None = None
    result: ToolResult | None = None
    catalog: Any = None
    provenance: QueryProvenance | None = None
    identity: RequestIdentity | None = None
    # 范围与 SKU 全集
    profiles: dict[str, Any] = field(default_factory=dict)
    candidate_shop_ids: tuple[str, ...] = ()
    requested_skus: tuple[str, ...] = ()
    erp_product_ids: tuple[str, ...] = ()
    resolution: ProductResolution | None = None
    candidates: tuple[dict[str, str], ...] = ()
    universe_from_snapshot: bool = False
    # 节点 1 为了知道"这个主体能谈到哪些 SKU"就要先看池范围；节点 2 才是**决定**授权的那一步。
    # 两个节点共用同一次连接查询，不各查一遍——两遍读会读到两个数据版本。
    scope_pairs: tuple[tuple[str, str], ...] = ()
    connected_pairs: tuple[tuple[str, str], ...] = ()
    excluded: list[dict[str, Any]] = field(default_factory=list)
    # 池与两批快照
    pools: tuple[InventoryPool, ...] = ()
    authorized_pools: tuple[InventoryPool, ...] = ()
    excluded_pools: tuple[dict[str, str], ...] = ()
    physical_rows: tuple[PhysicalRow, ...] = ()
    channel_rows: tuple[ChannelRow, ...] = ()
    source_ready: dict[str, bool] = field(default_factory=dict)
    policy_seconds: dict[str, int] = field(default_factory=dict)
    groups: dict[tuple[str, str, str], SkuGroup] = field(default_factory=dict)
    pool_fresh: dict[str, bool] = field(default_factory=dict)
    pool_scanned: dict[str, bool] = field(default_factory=dict)
    pool_captured: dict[str, datetime] = field(default_factory=dict)
    shop_fresh: dict[str, bool] = field(default_factory=dict)
    shop_scanned: dict[str, bool] = field(default_factory=dict)
    shop_captured: dict[str, datetime] = field(default_factory=dict)
    # 同刻并列的读数：不挑一个，交判定层报 data_anomaly（见 `deduplicate_physical_rows`）
    # 阈值与判定
    thresholds: tuple[Threshold, ...] = ()
    threshold_source: str = "none"
    rows: tuple[StockAlertRow, ...] = ()
    display_rows: tuple[StockAlertRow, ...] = ()
    summary: dict[str, Any] = field(default_factory=dict)
    limitations: list[str] = field(default_factory=list)
    data_as_of: datetime | None = None
    source_batches: tuple[str, ...] = ()
    final_status: RunStatus | None = None
    pending: list[tuple[InventoryState, dict[str, object]]] = field(default_factory=list)
    published: list[tuple[Any, dict[str, Any]]] = field(default_factory=list)


# ---------------------------------------------------------------------------
# 节点 1：全目录与范围
# ---------------------------------------------------------------------------


def resolve_full_catalog_and_scope(runtime: InventoryRuntime) -> None:
    """展开获准店铺与本轮 SKU 全集；显式越权引用直接 forbidden，不静默剔除。"""
    context = runtime.context
    request = runtime.request
    assert request is not None
    try:
        profiles = {profile.shop_id: profile for profile in repository.shop_profiles(
            context.conn, sorted(context.allowed_shop_ids), deadline=context.deadline)}
    except _BudgetExhausted:
        _stop(runtime, kind="unavailable", code="deadline_exceeded", problems=[],
              stage=InventoryNode.RESOLVE_FULL_CATALOG_AND_SCOPE,
              message="本次查询时间预算已耗尽，请缩小日期或店铺范围后重试。",
              limitations=["本次查询时间预算已耗尽"])
        return
    runtime.profiles = profiles

    if request.scope.mode == "selected" and request.scope.shop_refs:
        wanted: list[str] = []
        for ref in request.scope.shop_refs:
            shop_id = context.ref_to_shop_id.get(ref)
            if shop_id is None or shop_id not in context.allowed_shop_ids:
                _stop(runtime, kind="forbidden", code="forbidden", problems=["forbidden"],
                      stage=InventoryNode.RESOLVE_FULL_CATALOG_AND_SCOPE,
                      message="查询范围无权限。", limitations=["店铺不在授权范围"])
                return
            wanted.append(shop_id)
    else:
        wanted = sorted(context.allowed_shop_ids)
    if request.scope.platforms:
        platforms = set(request.scope.platforms)
        wanted = [shop for shop in wanted
                  if shop in profiles and profiles[shop].platform in platforms]
    kept: list[str] = []
    for shop in wanted:
        profile = profiles.get(shop)
        if profile is None:
            _exclude(runtime, shop, "shop_not_synced", TEXT_POOL_SNAPSHOT_MISSING)
            continue
        if not profile.enabled:
            _exclude(runtime, shop, "shop_disabled", "部分店铺已停用，仅返回剩余范围")
            continue
        kept.append(shop)
    runtime.candidate_shop_ids = tuple(sorted(kept))
    runtime.state = runtime.state.model_copy(
        update={"normalized_request": _normalized_request(runtime)})
    if not runtime.candidate_shop_ids:
        if (runtime.context.allowed_shop_ids
                or "shop_sellable" in set(runtime.request.levels)):
            _stop(runtime, kind="missing_data", code="missing_parameters", problems=[],
                  stage=InventoryNode.RESOLVE_FULL_CATALOG_AND_SCOPE,
                  message="缺少查询参数，请补充后重试。", limitations=[TEXT_SCOPE_EMPTY])
            return
        # pool-only scope (monitor empty-shop projection): no shop grants in
        # context and shop_sellable not requested -> continue on pool grants;
        # chat-path semantics of the stop above stay byte-identical.
    _resolve_skus(runtime)


def _resolve_skus(runtime: InventoryRuntime) -> None:
    """确定本轮 SKU 全集。

    `selected` 时要点名的引用**不能反查**主键（`ent-` 是单向摘要，007 也不把
    引用→主键映射给应用身份），所以在授权范围内的落点表上派生引用后比对——
    与 `catalog.resolver._resolve_by_ref` 同一做法。解析不出的引用是"身份没确定"，
    不是"0 库存"。

    `all` 时本轮只能由已授权快照批次派生（目录侧全集仍受成交行限制，Task 1/6），
    这个限制必须写进披露："没出现在快照里"与"没有库存"是两个不同的答案。
    """
    request = runtime.request
    assert request is not None
    # 两种模式都要先算池范围：`all` 那一途的 SKU 全集由已授权快照派生，而快照要先知道
    # 读哪些池。只在点名那一途算，全商品那一格就会拿到空池集合，然后被读成
    # "这些店都没有库存记录"——那正是"缺数据"被演成"没有货"的形状。
    _pool_pairs_for_scope(runtime)
    if request.products == "all":
        runtime.universe_from_snapshot = True
        runtime.limitations.append(TEXT_UNIVERSE_FROM_SNAPSHOT)
        runtime.state = runtime.state.model_copy(
            update={"normalized_request": _normalized_request(runtime)})
        return

    wanted = sorted({str(ref) for ref in request.sku_refs})
    if wanted:
        # SKU 全集 = 渠道映射表 ∪ 已授权快照里出现过的 SKU。只用前者会把"有货但还没
        # 建渠道映射"的那一格从分母里抖掉，而库存恰恰是 ERP 侧事实。
        known = list(runtime.requested_skus)
        try:
            snapshot_keys, blocked = repository.inventory_sku_keys(
                runtime.context.conn,
                namespace_pool_ids=_pool_pairs_for_scope(runtime),
                shop_ids=list(runtime.candidate_shop_ids))
        except _BudgetExhausted:
            snapshot_keys, blocked = (), []
        if blocked:
            _stop(runtime, kind="unavailable", code="result_too_large", problems=[],
                  stage=InventoryNode.RESOLVE_FULL_CATALOG_AND_SCOPE,
                  message="查询暂不可用，请稍后重试。", limitations=[TEXT_SCAN_TRUNCATED])
            return
        try:
            matched, unresolved = repository.resolve_sku_refs(
                runtime.context.conn, wanted_refs=wanted,
                shop_ids=list(runtime.candidate_shop_ids),
                extra_keys=list(snapshot_keys) + known)
        except _BudgetExhausted:
            _stop(runtime, kind="unavailable", code="deadline_exceeded", problems=[],
                  stage=InventoryNode.RESOLVE_FULL_CATALOG_AND_SCOPE,
                  message="本次查询时间预算已耗尽，请缩小日期或店铺范围后重试。",
                  limitations=["本次查询时间预算已耗尽"])
            return
        if unresolved:
            _stop(runtime, kind="missing_data", code="product_not_resolved", problems=[],
                  stage=InventoryNode.RESOLVE_FULL_CATALOG_AND_SCOPE,
                  message="缺少查询参数，请补充后重试。", limitations=[TEXT_SKU_UNRESOLVED])
            return
        runtime.requested_skus = tuple(matched)

    for ref in request.product_refs:
        ids, blocked = _resolve_product_skus(runtime, ref)
        if blocked:
            return
        runtime.requested_skus = tuple(sorted(set(runtime.requested_skus) | set(ids)))
    if not runtime.requested_skus:
        _stop(runtime, kind="missing_data", code="product_not_resolved", problems=[],
              stage=InventoryNode.RESOLVE_FULL_CATALOG_AND_SCOPE,
              message="缺少查询参数，请补充后重试。", limitations=[TEXT_SKU_UNRESOLVED])
        return
    runtime.state = runtime.state.model_copy(
        update={"normalized_request": _normalized_request(runtime)})


def _resolve_product_skus(runtime: InventoryRuntime, ref: str) -> tuple[tuple[str, ...],
                                                                        bool]:
    """商品引用 → 该商品在授权店铺上的 SKU 落点。"""
    try:
        resolution = resolve_product(runtime.context.conn, selector=Selector(ref=ref),
                                     authorized_shop_ids=runtime.candidate_shop_ids,
                                     at=runtime.context.now.date())
    except _BudgetExhausted:
        _stop(runtime, kind="unavailable", code="deadline_exceeded", problems=[],
              stage=InventoryNode.RESOLVE_FULL_CATALOG_AND_SCOPE,
              message="本次查询时间预算已耗尽，请缩小日期或店铺范围后重试。",
              limitations=["本次查询时间预算已耗尽"])
        return (), True
    runtime.resolution = resolution
    if resolution.status == "ambiguous":
        runtime.candidates = tuple({"ref": item.product_ref}
                                       for item in resolution.candidates[:20])
        _stop(runtime, kind="needs_input", code="missing_parameters", problems=[],
              stage=InventoryNode.RESOLVE_FULL_CATALOG_AND_SCOPE,
              message="缺少查询参数，请补充后重试。",
              limitations=[TEXT_SKU_UNRESOLVED])
        return (), True
    if resolution.status != "resolved" or not resolution.erp_product_id:
        _stop(runtime, kind="missing_data", code="product_not_resolved", problems=[],
              stage=InventoryNode.RESOLVE_FULL_CATALOG_AND_SCOPE,
              message="缺少查询参数，请补充后重试。", limitations=[TEXT_SKU_UNRESOLVED])
        return (), True
    runtime.erp_product_ids = tuple(sorted(set(runtime.erp_product_ids)
                                           | {resolution.erp_product_id}))
    try:
        keys, blocked = repository.sku_keys_in_scope(
            runtime.context.conn, shop_ids=list(runtime.candidate_shop_ids),
            extra=[])
    except _BudgetExhausted:
        return (), True
    if blocked:
        _stop(runtime, kind="unavailable", code="result_too_large", problems=[],
              stage=InventoryNode.RESOLVE_FULL_CATALOG_AND_SCOPE,
              message="查询暂不可用，请稍后重试。", limitations=[TEXT_SCAN_TRUNCATED])
        return (), True
    landing = {key for key in keys
               if ref_for_key(EntityKind.SKU.value, key) in set(resolution.sku_refs)}
    return tuple(sorted(landing | {key for key in keys if False})), False


# ---------------------------------------------------------------------------
# 节点 2：库存池授权
# ---------------------------------------------------------------------------


def _pool_pairs_for_scope(runtime: InventoryRuntime) -> list[tuple[str, str]]:
    """本轮**范围**里可能相关的 (账号范围, 池号)：只做 SKU 全集推导用。

    这不是授权决定：未获准的池在这一格只参与"这个 SKU 是不是你谈的那个"，它在
    节点 2 就被排除出总量与明细。两件事分开，才不会出现"为了知道目录就读进了数"。
    """
    if runtime.scope_pairs:
        return list(runtime.scope_pairs)
    if not runtime.candidate_shop_ids:
        # pool-only scope: authorized pairs derive straight from the pool
        # grant set (no shop-join derivation); connected_pairs stays empty
        # so no shop-side excluded_scope declarations can appear.
        derived = repository.authorized_pool_pairs_by_ids(
            runtime.context.conn,
            allowed_pool_ids=sorted(runtime.context.allowed_inventory_pool_ids),
            deadline=runtime.context.deadline)
        runtime.scope_pairs = tuple(sorted(set(derived)))
        runtime.connected_pairs = ()
        return list(runtime.scope_pairs)
    # 两个集合分开算，各自只服务一件事：
    #   - `authorized_pairs`：本轮真正能读的池。SKU 全集**只能**由它派生——把未获准池
    #     的池号混进全集，一个只存在于那个池里的 SKU 引用就会被"解析成功"，随后那句
    #     "这个 SKU 没有库存记录"成了一次隐形的越权探测；
    #   - `connected`：只用来产生排除声明。它取回的是 (账号范围, 池号) 本身，句柄由它
    #     派生，不读任何数量、标签或新鲜度。
    connected = repository.connected_pairs(
        runtime.context.conn, shop_ids=list(runtime.candidate_shop_ids))
    authorized = repository.authorized_pool_pairs(
        runtime.context.conn, shop_ids=list(runtime.candidate_shop_ids),
        allowed_pool_ids=sorted(runtime.context.allowed_inventory_pool_ids))
    runtime.scope_pairs = tuple(sorted(set(authorized)))
    runtime.connected_pairs = tuple(sorted(set(connected)))
    return list(runtime.scope_pairs)


def authorize_inventory_pools(runtime: InventoryRuntime) -> None:
    """池与店的连接关系只说明"有关系"；能不能读由服务端的池授权集决定。

    未获准的池不进总量、不进明细，只以句柄进 `excluded_scope`。
    """
    context = runtime.context
    if not runtime.pools:
        loaded = repository.pool_connections(
            context.conn, namespace_pool_ids=list(runtime.scope_pairs),
            allowed_pool_ids=context.allowed_inventory_pool_ids)
        runtime.pools = loaded.granted
        # 排除声明由连接关系直接派生句柄：那些池一行都没被查过。
        granted_ids = {pool.pool_id for pool in loaded.granted}
        seen: set[str] = set()
        excluded = []
        for namespace, pool_id in runtime.connected_pairs:
            if pool_id in granted_ids:
                continue
            handle = pool_handle(namespace, pool_id)
            if handle in seen:
                continue
            seen.add(handle)
            excluded.append({"pool_ref": handle, "reason": "pool_not_authorized"})
        runtime.excluded_pools = tuple(excluded)
    else:
        allowed = set(context.allowed_inventory_pool_ids)
        granted = [pool for pool in runtime.pools if pool.pool_id in allowed]
        excluded: list[dict[str, str]] = []
        seen: set[str] = set()
        for pool in runtime.pools:
            if pool.pool_id in allowed or pool.pool_ref in seen:
                continue
            seen.add(pool.pool_ref)
            excluded.append({"pool_ref": pool.pool_ref,
                             "reason": "pool_not_authorized"})
        runtime.excluded_pools = tuple(excluded)
        runtime.pools = tuple(granted)
    runtime.authorized_pools = tuple(runtime.pools)
    if runtime.excluded_pools:
        _note(runtime, TEXT_POOL_UNAUTHORIZED)


# ---------------------------------------------------------------------------
# 节点 3：阈值
# ---------------------------------------------------------------------------


def load_inventory_policy(runtime: InventoryRuntime) -> None:
    """本轮 inline 阈值与已配置策略各归其位；两者同给就是两套分母。"""
    request = runtime.request
    assert request is not None
    inline = [Threshold(level=str(rule["level"]), sku_ref=str(rule["sku_ref"]),
                        quantity=str(rule["quantity"]), unit=str(rule["unit"]),
                        shop_ref=str(rule.get("shop_ref") or ""),
                        pool_ref=str(rule.get("pool_ref") or ""))
              for rule in request.threshold_rules]
    if inline and request.threshold_policy_ref:
        _stop(runtime, kind="needs_input", code="missing_parameters", problems=[],
              stage=InventoryNode.LOAD_INVENTORY_POLICY,
              message="缺少查询参数，请补充后重试。", limitations=[TEXT_THRESHOLD_CONFLICT])
        return
    if inline:
        runtime.thresholds = tuple(inline)
        runtime.threshold_source = "this_turn"
        return
    if request.threshold_policy_ref is None:
        # 两者都没有：不替经营者设阈值。缺配置的格是 unconfigured，不是 normal。
        runtime.thresholds = ()
        runtime.threshold_source = "none"
        runtime.limitations.append(TEXT_THRESHOLD_MISSING)
        return
    try:
        # 全商品那一途还没有 SKU 全集（要等下一格读快照），所以按范围取全部策略。
        universe = None if runtime.universe_from_snapshot else list(inventory_skus(runtime))
        configured = repository.load_thresholds(
            runtime.context.conn, erp_sku_ids=universe,
            pool_ids=[pool.pool_id for pool in runtime.authorized_pools],
            shop_ids=list(runtime.candidate_shop_ids), at=runtime.context.now.date(),
            version=request.threshold_policy_ref)
    except _BudgetExhausted:
        _stop(runtime, kind="unavailable", code="deadline_exceeded", problems=[],
              stage=InventoryNode.LOAD_INVENTORY_POLICY,
              message="本次查询时间预算已耗尽，请缩小日期或店铺范围后重试。",
              limitations=["本次查询时间预算已耗尽"])
        return
    runtime.thresholds = tuple(configured)
    runtime.threshold_source = "configured" if configured else "none"
    if not configured:
        runtime.limitations.append(TEXT_THRESHOLD_MISSING)


def inventory_skus(runtime: InventoryRuntime) -> tuple[str, ...]:
    """本轮要检查的 ERP SKU 主键集合。

    `all` 时由已授权快照派生：先把两批快照读出来才知道哪些 SKU 有库存记录。快照里
    没出现的 SKU 仍是"不知道"，不是"0 件"——那正是 `unknown` 与 `low` 的区别。
    """
    if runtime.request is not None and runtime.request.products == "all" \
            and not runtime.requested_skus:
        return tuple(sorted({row.erp_sku_id for row in runtime.physical_rows}
                            | {row.erp_sku_id for row in runtime.channel_rows}))
    return runtime.requested_skus


# ---------------------------------------------------------------------------
# 节点 4：来源门禁
# ---------------------------------------------------------------------------


def check_inventory_source(runtime: InventoryRuntime) -> None:
    """两个口径分别问：实物取证成功不代表渠道可售也可信（spec §9）。

    默认两条都没登记，所以真实部署每一格都是 `unsupported`，而且一个数量都不发。
    """
    ready: dict[str, bool] = {}
    policies: dict[str, int] = {}
    for level in request_levels(runtime):
        entry = verified_inventory_source(level)
        ready[level] = entry is not None
        policies[level] = 0 if entry is None else int(entry.max_age_seconds)
    runtime.source_ready = ready
    runtime.policy_seconds = policies
    if not any(ready.values()):
        runtime.limitations.append(TEXT_SOURCE_UNVERIFIED)
        return
    for level, text in (("physical_total", TEXT_PHYSICAL_UNVERIFIED),
                        ("shop_sellable", TEXT_CHANNEL_UNVERIFIED)):
        if level in ready and not ready[level]:
            _note(runtime, text)


def request_levels(runtime: InventoryRuntime) -> tuple[str, ...]:
    if runtime.request is None:
        return AUDIT_LEVELS
    return tuple(runtime.request.levels)


# ---------------------------------------------------------------------------
# 节点 5 / 6：读快照，判时效与扫描完整性
# ---------------------------------------------------------------------------


def load_inventory_snapshots(runtime: InventoryRuntime) -> None:
    """两批快照各取各的源；每个库存身份只取本轮时点之前最新的那一批。"""
    context = runtime.context
    pools = runtime.authorized_pools
    pairs = [(pool.namespace, pool.pool_id) for pool in pools]
    # 全商品那一途不筛 SKU：传 None（"没有这个过滤条件"），而不是传一个空列表
    # （空列表在 SQL 里会返回零行，然后被读成"都没库存"）。
    erp_skus: list[str] | None = (None if runtime.universe_from_snapshot
                                  else list(inventory_skus(runtime)))
    if pairs:
        read = repository.physical_snapshots(
            context.conn, namespace_pool_ids=pairs,
            erp_sku_ids=erp_skus, as_of=context.now, deadline=context.deadline)
        runtime.physical_rows = read.rows
    if context.allowed_shop_ids:
        namespaces = sorted({pool.namespace for pool in pools}
                            | {repository.DEFAULT_NAMESPACE})
        read = repository.channel_snapshots(
            context.conn, namespaces=namespaces,
            shop_ids=list(runtime.candidate_shop_ids),
            erp_sku_ids=erp_skus, as_of=context.now, deadline=context.deadline)
        runtime.channel_rows = read.rows
    if runtime.universe_from_snapshot and not runtime.requested_skus:
        # 全商品：SKU 全集由刚读回来的两批快照派生，再回写一次规范化请求（它进指纹）。
        runtime.requested_skus = tuple(sorted(
            {row.erp_sku_id for row in runtime.physical_rows}
            | {row.erp_sku_id for row in runtime.channel_rows}))
        runtime.state = runtime.state.model_copy(
            update={"normalized_request": _normalized_request(runtime)})
    runtime.source_batches = tuple(sorted(
        {row.snapshot_id for row in runtime.physical_rows}
        | {row.snapshot_id for row in runtime.channel_rows}))
    if pools and not runtime.physical_rows:
        runtime.limitations.append(TEXT_POOL_SNAPSHOT_MISSING)
    if runtime.candidate_shop_ids and not runtime.channel_rows:
        runtime.limitations.append(TEXT_CHANNEL_SNAPSHOT_MISSING)


def check_source_completeness_and_freshness(runtime: InventoryRuntime) -> None:
    """逐池 / 逐店判新鲜与"分页是否取尽"。

    缺抓取时间在类型上就是 None，按不新鲜处理：一个没有时点的库存数既不能判低也不能
    判安全。扫描完整性决定"这个 SKU 没有记录"能不能被说出来。
    """
    for row in _authorized_physical_rows(runtime):
        key = row.pool_ref
        # 单位没登记的行连"新鲜"都谈不上：先按不新鲜处理，让它去走 data_anomaly 那一途。
        runtime.pool_fresh[key] = runtime.pool_fresh.get(key, True) and freshness_ok(
            captured_at=row.captured_at, now=runtime.context.now,
            max_age_seconds=runtime.policy_seconds.get("physical_total", 0)
        ) and quantity_precision(row.unit) is not None
        # AND 聚合，与新鲜度同一方向：`scan_complete` 在 019 里说的是"该 (池, 仓库) 的全
        # 目录分页取尽"。用 or 聚合时，一个扫完的仓库就能替没扫完的那个作证，`all_safe`
        # 就发在了一次明明没扫完的扫描上——漏掉的那个 SKU 再没人会去查。
        runtime.pool_scanned[key] = runtime.pool_scanned.get(key, True) \
            and bool(row.scan_complete)
        if row.captured_at is not None:
            current = runtime.pool_captured.get(key)
            if current is None or row.captured_at < current:
                runtime.pool_captured[key] = row.captured_at
    for row in _authorized_channel_rows(runtime):
        shop_ref = ref_for_key(EntityKind.SHOP.value, row.shop_id)
        runtime.shop_fresh[shop_ref] = runtime.shop_fresh.get(shop_ref, True)             and freshness_ok(captured_at=row.captured_at, now=runtime.context.now,
                             max_age_seconds=runtime.policy_seconds.get(
                                 "shop_sellable", 0))
        # 店铺那一侧同理：一条链接的完整声明不能替另一条没扫完的作证。
        runtime.shop_scanned[shop_ref] = runtime.shop_scanned.get(shop_ref, True) \
            and bool(row.scan_complete)
        if row.captured_at is not None:
            current = runtime.shop_captured.get(shop_ref)
            if current is None or row.captured_at < current:
                runtime.shop_captured[shop_ref] = row.captured_at
    fresh_claims = list(runtime.pool_fresh.values()) + list(runtime.shop_fresh.values())
    if fresh_claims and not all(fresh_claims):
        _note(runtime, TEXT_STALE)
    if not _scan_claims_complete(runtime) and (runtime.pool_scanned or runtime.shop_scanned):
        _note(runtime, TEXT_SCAN_INCOMPLETE)
    stamps = list(runtime.pool_captured.values()) + list(runtime.shop_captured.values())
    aware = [stamp for stamp in stamps if stamp.tzinfo is not None]
    if aware:
        # 共同截止取最早：一次预警只说一个"什么时候数的"，取最新会把旧数据说成新数据。
        runtime.data_as_of = min(aware)


# ---------------------------------------------------------------------------
# 节点 7：单位归一与去重
# ---------------------------------------------------------------------------


def _authorized_physical_rows(runtime: InventoryRuntime) -> tuple[PhysicalRow, ...]:
    """只保留获准池的行。

    读库那一格本来就按 `pool_id = ANY(获准池)` 查，所以这里通常是恒等的。但"通常是
    恒等的"不是理由：授权是这条链上最不能依赖上游自觉的一条线，任何一次读路径改动
    都会把未获准池的量直接加进总量。在这里再收窄一次，越权的行连参与聚合的机会都没有。
    """
    allowed = {pool.pool_ref for pool in runtime.authorized_pools}
    return tuple(row for row in runtime.physical_rows if row.pool_ref in allowed)


def _authorized_channel_rows(runtime: InventoryRuntime) -> tuple[ChannelRow, ...]:
    """同上，按店铺授权收窄渠道可售的行。"""
    allowed = set(runtime.candidate_shop_ids)
    return tuple(row for row in runtime.channel_rows if row.shop_id in allowed)


def deduplicate_physical_rows(runtime: InventoryRuntime) -> None:
    """按实物汇总身份去重求和：单位或数量打架就是数据异常。"""
    groups: dict[tuple[str, str, str], list[PhysicalRow]] = {}
    for row in _authorized_physical_rows(runtime):
        groups.setdefault(row.group_key, []).append(row)
    for key, rows in groups.items():
        payload = [{"namespace": row.namespace, "pool_ref": row.pool_ref,
                    "warehouse_ref": row.warehouse_ref, "sku_ref": row.sku_ref,
                    "batch_id": row.batch_id, "unit": row.unit,
                    "available_quantity": row.available_quantity} for row in rows]
        pool_ref, warehouse_ref, erp_sku_id = key
        namespace = rows[0].namespace
        base = SkuGroup(namespace=namespace, pool_ref=pool_ref,
                        warehouse_ref=warehouse_ref, erp_sku_id=erp_sku_id,
                        sku_ref=rows[0].sku_ref, unit=str(rows[0].unit or "piece"),
                        batch_count=len({row.batch_id for row in rows}),
                        fresh=all(runtime.pool_fresh.get(row.pool_ref, False)
                                  for row in rows),
                        scanned=all(runtime.pool_scanned.get(row.pool_ref, False)
                                    for row in rows),
                        captured_at=runtime.pool_captured.get(pool_ref))
        # 未登记单位或不可解析的数量先就地归类：让它们冒到 `amount_sum` 里就会抛一个
        # 普通 ValueError，被外层当成"契约违规"关掉三个出口，而它其实是一次可以说明白
        # 的数据异常（019 允许存 kit 与小数形态，所以这条路真的走得通）。
        bad_unit = [row for row in rows
                    if quantity_precision(row.unit) is None
                    or normalize_quantity(row.available_quantity, row.unit) is None]
        if bad_unit:
            base.quantity = None
            base.status = STATUS_ANOMALY
            _note(runtime, TEXT_UNIT_CONFLICT if any(
                quantity_precision(row.unit) is None for row in bad_unit)
                else TEXT_QUANTITY_CONFLICT)
            runtime.groups[key] = base
            continue
        try:
            base.quantity = sum_unique_physical_stock(payload)
        except InventoryConflict as conflict:
            base.quantity = None
            base.status = STATUS_ANOMALY
            _note(runtime, TEXT_UNIT_CONFLICT if conflict.reason == "unit_conflict"
                  else TEXT_QUANTITY_CONFLICT)
        else:
            negative = any(str(row.available_quantity or "").strip().startswith("-")
                           for row in rows)
            if negative:
                base.quantity = None
                base.status = STATUS_ANOMALY
                _note(runtime, TEXT_NEGATIVE)
            else:
                base.status = STATUS_UNKNOWN      # 待阈值判定那一格换成正式状态
        runtime.groups[key] = base


def compute_total_and_shop_levels(runtime: InventoryRuntime) -> None:
    """实物格按 (SKU, 池, 仓库) 出，店铺格按 (店铺, SKU) 出：两者永不相加也互不顶替。"""
    rows: list[StockAlertRow] = []
    levels = set(request_levels(runtime))
    for erp_sku_id in inventory_skus(runtime):
        sku_ref = ref_for_key(EntityKind.SKU.value, erp_sku_id)
        matched_groups = [group for key, group in runtime.groups.items()
                          if key[2] == erp_sku_id]
        if "physical_total" in levels:
            if matched_groups:
                for group in sorted(matched_groups, key=lambda item: (item.pool_ref,
                                                                      item.warehouse_ref)):
                    rows.append(StockAlertRow(
                        level="physical_total", sku_ref=sku_ref, erp_sku_id=erp_sku_id,
                        quantity=group.quantity, unit=group.unit,
                        batch_count=group.batch_count, pool_ref=group.pool_ref,
                        warehouse_ref=group.warehouse_ref,
                        snapshot_at=beijing_iso(group.captured_at)))
            else:
                # 点名了这个 SKU 但没有任何实物记录：那是缺证据，不是 0 件。
                # 占位行也带着 SKU 主键，否则下游按身份回查聚合时会把它错认成构造错误。
                rows.append(StockAlertRow(level="physical_total", sku_ref=sku_ref,
                                          erp_sku_id=erp_sku_id, status=STATUS_UNKNOWN,
                                          batch_count=0))
        if "shop_sellable" in levels:
            for shop_id in runtime.candidate_shop_ids:
                shop_rows = [row for row in _authorized_channel_rows(runtime)
                             if row.shop_id == shop_id and row.erp_sku_id == erp_sku_id]
                quantity, unit, shop_conflict = _shop_quantity(shop_rows)
                shop_ref = ref_for_key(EntityKind.SHOP.value, shop_id)
                stamps = [row.captured_at for row in shop_rows if row.captured_at]
                rows.append(StockAlertRow(
                    level="shop_sellable", sku_ref=sku_ref, erp_sku_id=erp_sku_id,
                    shop_id=shop_id, shop_ref_value=shop_ref,
                    channel_quantity=quantity, unit=unit,
                    batch_count=len({row.listing_id for row in shop_rows}),
                    status=STATUS_ANOMALY if shop_conflict else STATUS_UNKNOWN,
                    snapshot_at=beijing_iso(runtime.shop_captured.get(shop_ref))
                    or (beijing_iso(min(stamps)) if stamps else None)))
    if len(rows) > MAX_EXPECTED_ITEMS:
        # 全商品盘点没有上限就是一次无界扫描：宁可拒绝出数并说清下一步（缩范围或
        # 走离线作业），也不扫到一半就把扫到的那些当全集报出去。
        _stop(runtime, kind="unavailable", code="result_too_large", problems=[],
              stage=InventoryNode.COMPUTE_TOTAL_AND_SHOP_LEVELS,
              message="查询暂不可用，请稍后重试。", limitations=[TEXT_SCAN_TRUNCATED])
        return
    runtime.rows = tuple(rows)


def _shop_quantity(rows: Sequence[ChannelRow]) -> tuple[str | None, str, bool]:
    """一个店铺一个 SKU 的可售数：返回 (数量, 单位, 是否冲突)。

    四条规则，与实物那一格同源：

    - 多条链接是同店同 SKU 的独立事实，可以相加；
    - **同一身份**（链接 + 平台 SKU + 单位）只算一次；同一身份出现两个不同数量就是
      冲突，不取第一个也不取平均——那正是"两次抓取都算进当前可售"的入口；
    - 单位不一致不给总数（相加的前提是同一单位）；
    - 任一链接没报数就不给总数：少加一条会得到一个偏小的数，而它会被拿去说"配额不够"。
    """
    if not rows:
        return None, "piece", False
    units = {str(row.unit or "") for row in rows}
    if len(units) > 1:
        return None, sorted(units)[0], True
    unit = str(rows[0].unit or "piece")
    per_identity: dict[tuple[str, str, str], list[str]] = {}
    for row in rows:
        if row.sellable_quantity is None:
            return None, unit, False
        per_identity.setdefault((row.listing_id, row.platform_sku_id, str(row.unit)),
                                []).append(str(row.sellable_quantity))
    quantities: list[str] = []
    for values in per_identity.values():
        if len(set(values)) > 1:
            return None, unit, True      # 同身份两个数量：不选一个
        quantities.append(values[0])
    try:
        return amount_sum(quantities, unit), unit, False
    except ValueError:
        return None, unit, True


# ---------------------------------------------------------------------------
# 节点 9：阈值判定
# ---------------------------------------------------------------------------


def evaluate_inventory_thresholds(runtime: InventoryRuntime) -> None:
    """逐格定状态。顺序本身就是结论的一部分（`rules.classify_inventory`）。"""
    rows: list[StockAlertRow] = []
    for row in runtime.rows:
        threshold = _threshold_for(runtime, row)
        if not runtime.source_ready.get(row.level, False):
            # 来源未取证：连数出来的库存也不发，不给"看着像能用"留任何余地。
            rows.append(replace(row, status=STATUS_UNSUPPORTED, quantity=None,
                                channel_quantity=None, threshold=None, batch_count=0))
            continue
        quantity = row.quantity if row.level == "physical_total" else row.channel_quantity
        # 点名了这个 SKU 但本轮没有任何一批快照记录它：那一格是构造出来的占位行
        # （没有池、没有仓库），它没有聚合可查，答案就是"缺证据"。在这里拿它去
        # `_group_key_of` 会把这种合法占位行当成构造错误抛出去。
        group = (runtime.groups.get(_group_key_of(row))
                 if row.level == "physical_total" and row.pool_ref
                 and row.warehouse_ref else None)
        if row.level == "physical_total":
            if group is None:
                rows.append(replace(row, status=STATUS_UNKNOWN))
                continue
            if group.status == STATUS_ANOMALY:
                rows.append(replace(row, status=STATUS_ANOMALY))
                continue
            if not group.fresh:
                rows.append(replace(row, status=STATUS_STALE, threshold=None))
                continue
        else:
            if row.status == STATUS_ANOMALY:
                # 同身份两个数量：先说源数据自相矛盾，不说"没记录"，也不说安全。
                _note(runtime, TEXT_QUANTITY_CONFLICT)
                rows.append(replace(row, threshold=None))
                continue
            if quantity is None:
                rows.append(replace(row, status=STATUS_UNKNOWN))
                continue
            if not runtime.shop_fresh.get(row.shop_ref, False):
                rows.append(replace(row, status=STATUS_STALE, threshold=None))
                continue
        status = classify_inventory(quantity, threshold.quantity if threshold else None,
                                    fresh=True, unit=row.unit)
        rows.append(replace(row, status=status,
                            threshold=threshold.quantity if threshold else None,
                            unit=threshold.unit if threshold else row.unit))
    runtime.rows = tuple(rows)


def _group_key_of(row: StockAlertRow) -> tuple[str, str, str]:
    """预警行回到它自己的汇总身份。

    实物行永远带着 `pool_ref` 与 `warehouse_ref`（它们由那一批快照填进来），所以这里
    缺任一个就是构造错误——直接抛出来，不让它退化成"查不到聚合"然后被读成缺证据。
    """
    if not row.pool_ref or not row.warehouse_ref:
        raise ValueError("inventory_physical_row_needs_a_batch")
    return (str(row.pool_ref), str(row.warehouse_ref), row.erp_sku_id)


def _threshold_for(runtime: InventoryRuntime, row: StockAlertRow) -> Threshold | None:
    """按具体到宽找阈值：精确命中 (档位, SKU, 店/池) 优先于只按 SKU 的全局阈值。"""
    wanted = "low_quota" if row.level == "shop_sellable" else "low_replenish"
    best: Threshold | None = None
    best_specificity = -1
    for threshold in runtime.thresholds:
        if threshold.level != wanted or threshold.sku_ref != row.sku_ref:
            continue
        if wanted == "low_quota":
            if threshold.shop_ref and threshold.shop_ref != row.shop_ref:
                continue
            specificity = 2 if threshold.shop_ref else 1
        else:
            if threshold.pool_ref and threshold.pool_ref != row.pool_ref:
                continue
            specificity = 2 if threshold.pool_ref else 1
        if specificity > best_specificity:
            best, best_specificity = threshold, specificity
    return best


# ---------------------------------------------------------------------------
# 节点 10：候选与展示截断
# ---------------------------------------------------------------------------


def classify_inventory_actions(runtime: InventoryRuntime) -> None:
    """两种候选各自对应一个口径：低实物给补货候选，渠道缺口给配额调整候选。"""
    rows: list[StockAlertRow] = []
    # 同一 SKU 的实物结论：配额候选的理由要说"共享实物还充足"，就必须真的看到那一格
    # 判成了 normal。只看渠道那个 0 就写"仍充足"，是在同一份产物里同时说"要补货"和
    # "货很足"——两个动作互相打脸，而经营者看到的是一句无法执行的话。
    physical_status = {row.sku_ref: row.status for row in runtime.rows
                       if row.level == "physical_total"}
    for row in runtime.rows:
        action, reason = "", ""
        if row.status == STATUS_LOW:
            if row.level == "physical_total":
                action, reason = "replenish", "实物低于阈值，建议补货"
                _note(runtime, TEXT_REPLENISH)
            else:
                action = "quota_adjust"
                zero = str(row.channel_quantity or "") == "0"
                ample = physical_status.get(row.sku_ref) == STATUS_NORMAL
                if zero and ample:
                    # 只有这两个事实都成立，才敢说"配额问题、不是采购问题"。
                    reason = "渠道可售为 0 且共享实物充足，建议调整店铺配额"
                elif zero:
                    reason = "渠道可售为 0（实物侧未判为充足，先按缺证据处理）"
                else:
                    reason = "渠道可售低于配额阈值，建议调整店铺配额"
                _note(runtime, TEXT_QUOTA)
        elif row.status == STATUS_ANOMALY:
            reason = "数据异常，先核对来源与导入批次"
        elif row.status == STATUS_STALE:
            reason = "快照过期，先重扫再判定"
        rows.append(replace(row, action=action, reason=reason))
    runtime.rows = tuple(rows)
    ordered = sorted(rows, key=lambda item: (_STATUS_RISK.get(item.status, 9),
                                              _risk_quantity(item), item.sku_ref,
                                              item.shop_ref, str(item.pool_ref or "")))
    runtime.display_rows = tuple(ordered[:MAX_DISPLAY_ITEMS])


def _risk_quantity(row: StockAlertRow) -> float:
    value = row.quantity if row.level == "physical_total" else row.channel_quantity
    parsed = normalize_quantity(value, row.unit)
    return float(parsed) if parsed is not None else float("inf")


def summarize_inventory_levels(runtime: InventoryRuntime) -> None:
    """出汇总：分母是期望项，`scanned` 是扫描事实，`truncated` 只描述展示。"""
    counts: dict[str, int] = {}
    for row in runtime.rows:
        counts[row.status] = counts.get(row.status, 0) + 1
    expected = len(runtime.rows)
    evaluated = sum(count for status, count in counts.items() if status in JUDGED_STATUSES)
    truncated = expected > MAX_DISPLAY_ITEMS
    all_normal = bool(expected > 0 and evaluated == expected
                      and all(row.status == STATUS_NORMAL for row in runtime.rows))
    scanned = _scan_claims_complete(runtime)
    summary: dict[str, Any] = {
        "expected_items": expected,
        "evaluated_items": evaluated,
        "scanned_items": expected,
        "truncated": truncated,
        # "全部安全"要求：每一格都判过且都是 normal、扫描声明完整、没有截断。
        "all_safe": bool(all_normal and scanned and not truncated),
        "counts": dict(sorted(counts.items())),
        "levels": list(request_levels(runtime)),
        "pools": _pool_claims(runtime),
        "threshold_source": runtime.threshold_source,
        "rule_version": INVENTORY_RULE_VERSION,
    }
    if runtime.excluded_pools:
        # 未获准的池也要能被数出来：只说"少了几格"不说"少了哪几个池"，经营者就没法
        # 去补那件真正缺的东西（池授权）。句柄不是凭证，所以这一步不泄露池号。
        summary["excluded_pools"] = [dict(entry) for entry in runtime.excluded_pools]
    policies = sorted({value for value in runtime.policy_seconds.values() if value})
    if policies:
        summary["freshness_policy_seconds"] = policies[0]
    runtime.summary = summary
    if truncated:
        _note(runtime, TEXT_DISPLAY_TRUNCATED)
    if evaluated < expected:
        _note(runtime, TEXT_INCOMPLETE)
    runtime.provenance = _provenance_of(runtime)
    runtime.identity = None
    _ensure_lineage(runtime)
    if summary["all_safe"]:
        status = "ok"
    elif evaluated:
        status = "partial"
    else:
        status = "missing_data"
    runtime.state = runtime.state.model_copy(update={
        "target_status": {"ok": DomainStatus.SUCCESS, "partial": DomainStatus.PARTIAL,
                          "missing_data": DomainStatus.MISSING_DATA}[status],
        "tool_status": {"ok": "ok", "partial": "ok",
                        "missing_data": "missing_data"}[status],
        "limitations": inventory_limitation_codes(runtime.limitations),
        "data_as_of": runtime.data_as_of,
        "normalized_request": _normalized_request(runtime)})
    runtime.final_status = {"ok": RunStatus.SUCCEEDED, "partial": RunStatus.PARTIAL,
                            "missing_data": RunStatus.MISSING_DATA}[status]
    runtime.report = _report(runtime, status=status, run_status=runtime.final_status)


def _scan_claims_complete(runtime: InventoryRuntime) -> bool:
    """所有出现过的池 / 店铺都声明"分页取尽"才为真。

    一个都没读到时返回 False：没有证据不等于有反证。
    """
    claims = list(runtime.pool_scanned.values()) + list(runtime.shop_scanned.values())
    return bool(claims) and all(claims)


def _pool_claims(runtime: InventoryRuntime) -> list[dict[str, Any]]:
    """逐池声明用了哪一批快照：缺快照的池也必须能被数出来（缺项 = 没读到）。"""
    by_pool: dict[str, dict[str, Any]] = {}
    # 只声明获准池：把未授权池的句柄与新鲜度一起发出去，等于替它作了一次存在声明。
    for row in _authorized_physical_rows(runtime):
        entry = by_pool.setdefault(row.pool_ref, {
            "pool_ref": row.pool_ref,
            "connection_kind": _connection_kind(runtime, row.pool_ref),
            "fresh": bool(runtime.pool_fresh.get(row.pool_ref)),
            "scan_complete": bool(runtime.pool_scanned.get(row.pool_ref))})
        captured = row.captured_at
        if captured is not None:
            current = by_pool[row.pool_ref].get("snapshot_at")
            stamp = beijing_iso(captured)
            if current is None or stamp < current:
                entry["snapshot_at"] = stamp
    return [by_pool[key] for key in sorted(by_pool)]


def _connection_kind(runtime: InventoryRuntime, pool_ref: str) -> str:
    """池的连接方式只能来自它自己的声明；查不到就抛，不默认 shared。

    `rules` 的模块说明写明了"没有未知即 shared 这一档"：把未声明的池当共享池，就是在
    替所有店把同一批库存重复数一遍——而这里连"是哪一批"都没确认。
    """
    for pool in runtime.authorized_pools:
        if pool.pool_ref == pool_ref:
            return pool.connection_kind
    raise ValueError("inventory_pool_connection_undeclared")


# ---------------------------------------------------------------------------
# 投影、血缘与落库材料
# ---------------------------------------------------------------------------


def alert_rows(runtime: InventoryRuntime) -> list[dict[str, Any]]:
    """展示行（已投影形态）：只有引用、句柄与数值，没有任何真实主键。

    数量列**带着 null 出现**：把键删掉，展示层就分不清"没记录"与"这一格没检查"。
    引用与句柄列反过来：没有值就不发这个键。
    """
    return [row.as_payload() for row in runtime.display_rows]


def _requested_scope(runtime: InventoryRuntime) -> dict[str, Any]:
    request = runtime.request
    if request is None:
        return {"mode": "all_authorized"}
    scope: dict[str, Any] = {"mode": request.scope.mode,
                             "platforms": list(request.scope.platforms)}
    if request.scope.shop_refs:
        scope["shop_refs"] = list(request.scope.shop_refs)
    return scope


def evaluated_scope(runtime: InventoryRuntime) -> dict[str, Any]:
    """本轮真正被评估的店铺（引用形态）：契约 v2 的第三面。"""
    return {"shop_refs": [ref_for_key(EntityKind.SHOP.value, shop)
                          for shop in runtime.candidate_shop_ids]}


def _normalized_request(runtime: InventoryRuntime) -> dict[str, object]:
    return runtime.request.normalized() if runtime.request is not None else {}


def _exclude(runtime: InventoryRuntime, shop_id: str, reason: str, limitation: str) -> None:
    ref = ref_for_key(EntityKind.SHOP.value, shop_id)
    if all(str(item.get("shop_ref")) != ref for item in runtime.excluded):
        runtime.excluded.append({"shop_ref": ref, "reason": reason})
    _note(runtime, limitation)


def _resolved_product(runtime: InventoryRuntime) -> dict[str, Any]:
    if not runtime.erp_product_ids:
        return {}
    resolved: dict[str, Any] = {
        "product_ref": ref_for_key(EntityKind.PRODUCT.value, runtime.erp_product_ids[0])}
    skus = sorted({row.sku_ref for row in runtime.rows if row.sku_ref})
    if skus:
        resolved["sku_refs"] = skus
    resolution = runtime.resolution
    if resolution is not None and resolution.mapping_version:
        resolved["mapping_version"] = resolution.mapping_version
    return resolved


def _provenance_of(runtime: InventoryRuntime) -> QueryProvenance:
    """血缘：哪一版规则、哪一版来源注册表与换算表、哪几批快照。"""
    return QueryProvenance(
        template_id=INVENTORY_TEMPLATE_ID, template_version=INVENTORY_TEMPLATE_VERSION,
        metric_version=INVENTORY_METRIC_VERSION, graph_version=INVENTORY_GRAPH_VERSION,
        schema_version=INVENTORY_SCHEMA_VERSION, catalog_version=0,
        mapping_version="channel-map/none",
        policy_version=UNIT_CONVERSION_REGISTRY_VERSION,
        source_batches=tuple(runtime.source_batches), data_as_of=runtime.data_as_of)


def _fingerprint(runtime: InventoryRuntime) -> str:
    from bi_agent.runtime.artifacts import request_fingerprint

    return request_fingerprint(
        subject_id=runtime.context.subject_id,
        allowed_shop_ids=runtime.context.allowed_shop_ids,
        normalized_request=_normalized_request(runtime),
        provenance=runtime.provenance or _provenance_of(runtime))


def _ensure_lineage(runtime: InventoryRuntime) -> None:
    if runtime.provenance is None:
        runtime.provenance = _provenance_of(runtime)
    if runtime.identity is None:
        status = runtime.final_status or runtime.state.status
        runtime.identity = RequestIdentity(
            root_request_id=runtime.context.root_request_id,
            request_fingerprint=_fingerprint(runtime),
            attempt_no=runtime.context.attempt_no, recovery_count=0,
            termination_reason=_termination_reason(runtime, status))


def _report(runtime: InventoryRuntime, *, status: str,
            run_status: RunStatus | None = None) -> InventoryAlertReport:
    resolved = run_status or (runtime.state.status
                              if runtime.state.status is not RunStatus.RUNNING
                              else RunStatus.SUCCEEDED)
    return InventoryAlertReport(
        status=status, rows=runtime.rows, summary=dict(runtime.summary),
        requested_scope=_requested_scope(runtime),
        evaluated_scope=evaluated_scope(runtime),
        # `excluded_scope` 只说店铺（它的 reason 词表是店铺口径的）；被排除的池走
        # `inventory.excluded_pools`。把两种范围塞进同一个键，就会有一句"没算进来"
        # 找不到它自己的原因词表。
        excluded_scope=tuple(runtime.excluded),
        limitations=tuple(runtime.limitations),
        resolved_product=_resolved_product(runtime),
        termination_reason=_termination_reason(runtime, resolved),
        data_as_of=runtime.data_as_of)


# ---------------------------------------------------------------------------
# 终止与恢复映射
# ---------------------------------------------------------------------------

_OUTCOMES = {
    "needs_input": (RunStatus.NEEDS_INPUT, DomainStatus.NEEDS_INPUT,
                    "invalid_parameters", "needs_input"),
    "missing_data": (RunStatus.MISSING_DATA, DomainStatus.MISSING_DATA,
                     "missing_data", "missing_data"),
    "forbidden": (RunStatus.FAILED, DomainStatus.FAILED, "forbidden", "forbidden"),
    "unavailable": (RunStatus.FAILED, DomainStatus.FAILED, "unavailable", "unavailable"),
}

_ERROR_BY_FINDING: dict[str, tuple[str, RecoveryAction]] = {
    "missing_parameters": ("missing_parameters", RecoveryAction.CORRECT_PARAMETERS),
    "invalid_parameters": ("invalid_parameters", RecoveryAction.CORRECT_PARAMETERS),
    "forbidden": ("forbidden", RecoveryAction.NONE),
    "deadline_exceeded": ("deadline_exceeded", RecoveryAction.RETRY_LATER),
    "query_timeout": ("deadline_exceeded", RecoveryAction.RETRY_LATER),
    "result_too_large": ("unavailable", RecoveryAction.NONE),
    "product_not_resolved": ("missing_parameters", RecoveryAction.CORRECT_PARAMETERS),
    "unavailable": ("unavailable", RecoveryAction.RETRY_LATER),
    "artifact_persistence_failed": ("artifact_persistence_failed",
                                    RecoveryAction.RETRY_LATER),
    "result_contract_violation": ("result_contract_violation", RecoveryAction.NONE),
}

# 归因码 → 终止原因：与 `runtime.artifacts.TERMINATION_REASONS` 和 009 的 SQL CHECK
# 同一词表。本轮不新增原因码：那要重新声明 CHECK 并推一条部署顺序硬约束。
_TERMINATION_BY_CODE = {
    "missing_parameters": "missing_parameters",
    "invalid_parameters": "invalid_parameters",
    "forbidden": "forbidden",
    "coverage_incomplete": "coverage_incomplete",
    "data_as_of_unknown": "data_as_of_unknown",
    "source_not_onboarded": "source_not_onboarded",
    "capability_unavailable": "capability_unavailable",
    "result_too_large": "result_too_large",
    "deadline_exceeded": "deadline_exceeded",
    "query_timeout": "query_timeout",
    "artifact_persistence_failed": "persistence_failed",
    "result_contract_violation": "contract_violation",
    "unavailable": "upstream_unavailable",
    "inventory_source_unverified": "capability_unavailable",
    "inventory_physical_source_unverified": "capability_unavailable",
    "inventory_channel_source_unverified": "capability_unavailable",
    "inventory_snapshot_missing": "coverage_incomplete",
    "inventory_channel_snapshot_missing": "coverage_incomplete",
    "inventory_snapshot_stale": "data_as_of_unknown",
    "inventory_scan_incomplete": "coverage_incomplete",
    "inventory_data_anomaly": "data_as_of_unknown",
    "inventory_unit_conflict": "data_as_of_unknown",
    "inventory_negative_quantity": "data_as_of_unknown",
    "inventory_threshold_unconfigured": "missing_parameters",
    "inventory_threshold_conflict": "missing_parameters",
    "inventory_scan_truncated": "result_too_large",
    "inventory_pool_not_authorized": "coverage_incomplete",
    "inventory_scope_empty": "missing_parameters",
    "inventory_sku_unresolved": "missing_parameters",
    "inventory_universe_from_snapshot": "coverage_incomplete",
    "inventory_display_truncated": "succeeded",
    "inventory_replenish_candidate": "succeeded",
    "inventory_quota_candidate": "succeeded",
    "inventory_audit_incomplete": "coverage_incomplete",
    "product_not_resolved": "missing_parameters",
    "shop_not_synced": "missing_parameters",
}

# 业务发现类码：说的是"做完了，结果是这样"，不该抢走"还缺哪件证据"的归因。
_FINDING_CODES = frozenset({"inventory_replenish_candidate", "inventory_quota_candidate",
                            "inventory_display_truncated"})

# 授权缺失不能被业务发现盖过去：它排在最前单独判。
# 归因优先序（不是限制被追加的顺序）：一份"全商品全集只由快照派生 + 扫描被截断 +
# 池没授权"的结果，下一步该去拿池授权；把三条按文案顺序比，谁先被追加谁就赢，
# 那个"原因"就变成写入顺序的副产品而不是结论。
_CODE_PRECEDENCE: tuple[str, ...] = (
    "inventory_pool_not_authorized", "inventory_scope_empty", "inventory_sku_unresolved",
    "inventory_source_unverified", "inventory_physical_source_unverified",
    "inventory_channel_source_unverified", "inventory_threshold_conflict",
    "inventory_scan_truncated", "inventory_snapshot_stale", "inventory_data_anomaly",
    "inventory_unit_conflict", "inventory_negative_quantity",
    "inventory_scan_incomplete", "inventory_snapshot_missing",
    "inventory_channel_snapshot_missing", "inventory_universe_from_snapshot",
    "inventory_threshold_unconfigured", "inventory_audit_incomplete",
)


def _stop(runtime: InventoryRuntime, *, kind: str, code: str, problems: Sequence[str],
          stage: InventoryNode, message: str, limitations: Sequence[str] = ()) -> None:
    """提前终止：状态、血缘与报告一起定下来，后面的节点不再执行。"""
    run_status, domain_status, tool_status, report_status = _OUTCOMES[kind]
    for limitation in limitations:
        _note(runtime, limitation)
    error: ErrorEnvelope | None = None
    if kind != "missing_data":
        envelope_code, recovery = _ERROR_BY_FINDING.get(
            code, ("unavailable", RecoveryAction.NONE))
        error = ErrorEnvelope(
            code=envelope_code, stage=stage.value,  # type: ignore[arg-type]
            retryable=recovery is RecoveryAction.RETRY_LATER,
            recovery=recovery, public_message=message,  # type: ignore[arg-type]
            problems=list(problems))  # type: ignore[arg-type]
    runtime.state = runtime.state.model_copy(update={
        "status": run_status, "target_status": domain_status,
        "tool_status": tool_status, "problems": list(problems),
        "limitations": inventory_limitation_codes(runtime.limitations),
        "data_as_of": runtime.data_as_of,
        "normalized_request": _normalized_request(runtime),
        "error": error})
    runtime.final_status = run_status
    _ensure_lineage(runtime)
    runtime.report = _report(runtime, status=report_status, run_status=run_status)


def _termination_reason(runtime: InventoryRuntime, status: RunStatus) -> str:
    """为什么这次执行停在这里：只查已登记的码表，不自造说法，也不留空白。"""
    if status is RunStatus.SUCCEEDED:
        return "succeeded"
    codes = [code for code in inventory_limitation_codes(runtime.limitations)
             + list(runtime.state.limitations) if code in _TERMINATION_BY_CODE]
    for code in _CODE_PRECEDENCE:
        if code in codes:
            return _TERMINATION_BY_CODE[code]
    for code in codes:
        if code in _FINDING_CODES:
            return _TERMINATION_BY_CODE[code]
    error = runtime.state.error
    if error is not None:
        code = str(getattr(error.code, "value", error.code))
        return _TERMINATION_BY_CODE.get(code, "contract_violation")
    if status is RunStatus.MISSING_DATA:
        return "coverage_incomplete"
    return "recovery_exhausted"


def _note(runtime: InventoryRuntime, text: str) -> None:
    if text not in runtime.limitations:
        runtime.limitations.append(text)


# ---------------------------------------------------------------------------
# 节点 11 / 12：发布与收尾
# ---------------------------------------------------------------------------


def build_inventory_catalog(runtime: InventoryRuntime) -> None:
    """建一次目录：引用与展示名的唯一换面处，越权店铺在这里直接失败。"""
    rows: list[dict[str, Any]] = []
    for shop_id in runtime.candidate_shop_ids:
        row: dict[str, Any] = {"shop_id": shop_id}
        if runtime.erp_product_ids:
            row["product_id"] = runtime.erp_product_ids[0]
        rows.append(row)
    if not rows and runtime.erp_product_ids:
        rows.append({"product_id": runtime.erp_product_ids[0]})
    coverage = Coverage(status="complete", start=runtime.context.now.date(),
                        end=runtime.context.now.date(), gaps=[])
    source = ToolResult(status="ok", data=rows, filters={}, coverage=coverage)
    runtime.catalog = build_catalog(runtime.context.conn, source,
                                    allowed_shop_ids=runtime.context.allowed_shop_ids)


def persist_alerts(runtime: InventoryRuntime, store: Any) -> None:
    """必需结果存不下就是 failed：一份"看着有卡片其实没落库"的预警最危险。"""
    if runtime.catalog is None or runtime.report is None:
        _stop(runtime, kind="unavailable", code="result_contract_violation",
              problems=["result_contract_violation"], stage=InventoryNode.PERSIST_ALERTS,
              message="查询结果异常。")
        return
    try:
        from .tool import project_alerts

        payload = project_alerts(runtime, runtime.catalog, runtime.report)
    except ValueError:
        # 载荷不符合安全契约，不是"存不下"：这两个码的下一步完全不同。把它们混成
        # `artifact_persistence_failed`（retryable=True）就是在告诉用户稍后重试一件
        # 永远不会成功的重试。
        runtime.published = []
        _stop(runtime, kind="unavailable", code="result_contract_violation",
              problems=["result_contract_violation"], stage=InventoryNode.PERSIST_ALERTS,
              message="查询结果异常。")
        return
    try:
        ref = store.save_artifact(runtime.state.run_id, NewArtifact(
            artifact_type="inventory_alerts", payload=payload,
            data_as_of=runtime.data_as_of, coverage=None))
    except Exception:  # noqa: BLE001 - 不带出数据库原文
        runtime.published = []
        _stop(runtime, kind="unavailable", code="artifact_persistence_failed",
              problems=["artifact_persistence_failed"], stage=InventoryNode.PERSIST_ALERTS,
              message="结果保存失败，请稍后重试。")
        return
    runtime.published = [(ref, payload)]
    runtime.state = runtime.state.model_copy(update={"artifact_refs": [ref]})


def finalize_run(runtime: InventoryRuntime, store: Any) -> None:
    """收尾：终态由汇总节点定下来，不在这里重新推断。"""
    status = runtime.final_status or _run_status(
        runtime.state.target_status or DomainStatus.FAILED)
    runtime.state = runtime.state.model_copy(update={
        "status": status, "revision": runtime.state.revision + 1})
    store.finish(runtime.state.run_id, RunCompletion(
        expected_revision=runtime.state.revision - 1,
        node=InventoryNode.FINALIZE.value, status=status,
        state=runtime.state.model_dump(mode="json"),
        payload=_event_payload(runtime),  # type: ignore[arg-type]
        error_code=runtime.state.error.code if runtime.state.error else None,
        termination_reason=_termination_reason(runtime, status)))


def _run_status(status: DomainStatus) -> RunStatus:
    return {DomainStatus.SUCCESS: RunStatus.SUCCEEDED,
            DomainStatus.NEEDS_INPUT: RunStatus.NEEDS_INPUT,
            DomainStatus.MISSING_DATA: RunStatus.MISSING_DATA,
            DomainStatus.PARTIAL: RunStatus.PARTIAL,
            DomainStatus.FAILED: RunStatus.FAILED}[status]


# ---------------------------------------------------------------------------
# 驱动
# ---------------------------------------------------------------------------

_PRE_NODES: tuple[tuple[InventoryNode, Callable[[InventoryRuntime], None]], ...] = (
    (InventoryNode.RESOLVE_FULL_CATALOG_AND_SCOPE, resolve_full_catalog_and_scope),
    (InventoryNode.AUTHORIZE_INVENTORY_POOLS, authorize_inventory_pools),
    (InventoryNode.LOAD_INVENTORY_POLICY, load_inventory_policy),
)
_SNAPSHOT_NODES: tuple[tuple[InventoryNode, Callable[[InventoryRuntime], None]], ...] = (
    (InventoryNode.CHECK_SOURCE_CAPABILITIES, check_inventory_source),
    (InventoryNode.LOAD_SNAPSHOTS, load_inventory_snapshots),
    (InventoryNode.CHECK_COMPLETENESS_AND_FRESHNESS,
     check_source_completeness_and_freshness),
    (InventoryNode.NORMALIZE_UNITS_AND_DEDUPLICATE_POOLS, deduplicate_physical_rows),
    (InventoryNode.COMPUTE_TOTAL_AND_SHOP_LEVELS, compute_total_and_shop_levels),
    (InventoryNode.EVALUATE_THRESHOLDS, evaluate_inventory_thresholds),
    (InventoryNode.CLASSIFY_ACTIONS, classify_inventory_actions),
)


@dataclass
class InventoryExecution:
    """内部兼容结果：绝不直接持久化。"""

    domain_result: DomainResult
    report: InventoryAlertReport | None = None
    session_filters: Mapping[str, object] = field(default_factory=dict)


def run_inventory_graph(*, request: InventoryInspectionRequest | None,
                        context: DomainContext, tool_call_id: str,
                        arguments: Mapping[str, Any] | None = None,
                        arguments_error: str | None = None) -> InventoryExecution:
    """执行一次两级预警：一次工具调用 = 一次图执行，内部节点不消耗额外模型回合。

    参数解析失败**也**要走图：`needs_input` 必须留下运行记录与终止原因，否则恢复
    策略只能去猜聊天文本（与经营图 / 价审图同一契约）。
    """
    if request is not None and not isinstance(request, InventoryInspectionRequest):
        raise InvalidInventoryRequest()
    store = context.store
    run_id = store.create_run(NewQueryRun(
        chat_id=context.chat_id, user_message_id=context.user_message_id,
        subject_id=context.subject_id, tool_call_id=tool_call_id, domain=DOMAIN,
        attempt_no=context.attempt_no, normalized_request={},
        state={"node": InventoryNode.RESOLVE_FULL_CATALOG_AND_SCOPE.value,
               "status": RunStatus.RUNNING.value, "revision": 0}))
    runtime = InventoryRuntime(
        state=InventoryState(run_id=run_id, normalized_request={}), context=context,
        tool_call_id=tool_call_id, request=request)
    try:
        return _run_nodes(runtime, store, arguments_error=arguments_error)
    except Exception:  # noqa: BLE001 - 收尾后原样上抛，由外层做脱敏
        _finish_run_as_failed(runtime, store)  # type: ignore[arg-type]
        raise


def _run_nodes(runtime: InventoryRuntime, store: Any, *,
               arguments_error: str | None) -> InventoryExecution:
    if arguments_error is not None or runtime.request is None:
        # 缺参数（包括没给阈值档位、没点名 SKU）都在第一格终止：往下跑就需要一份
        # "这一格该有多少货"的标准，而那份标准除了经营者给的值之外没有合法来源。
        _stop(runtime, kind="needs_input", code="invalid_parameters",
              problems=["invalid_parameters"],
              stage=InventoryNode.RESOLVE_FULL_CATALOG_AND_SCOPE,
              message="缺少查询参数，请补充后重试。")
        runtime.state = runtime.state.model_copy(update={"status": RunStatus.NEEDS_INPUT})
        _persist_transition(runtime, store, _event_payload(runtime))  # type: ignore[arg-type]
        _finish(runtime, store)
        return _execution_result(runtime)

    for node, action in _PRE_NODES:
        if runtime.state.node is not node:
            runtime.state = transition_state(runtime.state, node)
        action(runtime)
        _persist_transition(runtime, store, _event_payload(runtime))  # type: ignore[arg-type]
        if runtime.state.status is not RunStatus.RUNNING:
            _finish(runtime, store)
            return _execution_result(runtime)

    try:
        with read_only_snapshot(runtime.context.conn):
            for node, action in _SNAPSHOT_NODES:
                runtime.state = transition_state(runtime.state, node)
                action(runtime)
                runtime.pending.append((runtime.state, _event_payload(runtime)))  # type: ignore[arg-type]
                if runtime.state.status is not RunStatus.RUNNING:
                    break
        # 汇总必须在只读快照**退出之后**：它要把终态与限制码写进状态，
        # 而快照内一切写入都会被 `transaction_read_only` 拒掉。
        summarize_inventory_levels(runtime)
    except _BudgetExhausted:
        _stop(runtime, kind="unavailable", code="deadline_exceeded", problems=[],
              stage=InventoryNode.LOAD_SNAPSHOTS,
              message="本次查询时间预算已耗尽，请缩小日期或店铺范围后重试。",
              limitations=["本次查询时间预算已耗尽"])
    except psycopg.errors.QueryCanceled:
        _stop(runtime, kind="unavailable", code="query_timeout", problems=[],
              stage=InventoryNode.LOAD_SNAPSHOTS,
              message="查询已超时，请稍后重试。", limitations=["查询超时"])
    except (psycopg.errors.UndefinedTable, psycopg.errors.InsufficientPrivilege):
        # 表还没随迁移建立，或应用身份没有读视图的权限：这是**部署缺件**，不是
        # "这些池都没货"。说成 unavailable 才是诚实答案。
        _stop(runtime, kind="unavailable", code="unavailable", problems=[],
              stage=InventoryNode.LOAD_SNAPSHOTS,
              message="查询暂不可用，请稍后重试。", limitations=[TEXT_SOURCE_UNVERIFIED])
    except (psycopg.OperationalError, psycopg.InterfaceError):
        _stop(runtime, kind="unavailable", code="unavailable", problems=[],
              stage=InventoryNode.LOAD_SNAPSHOTS,
              message="查询暂不可用，请稍后重试。")
    except ValueError:
        # 判定不变量、快照形状或血缘值不合法：按契约违规关掉三个出口，不带原文。
        _stop(runtime, kind="unavailable", code="result_contract_violation",
              problems=["result_contract_violation"], stage=runtime.state.node,
              message="查询结果异常。")
    if not runtime.pending or runtime.pending[-1][0] is not runtime.state:
        runtime.pending.append((runtime.state, _event_payload(runtime)))  # type: ignore[arg-type]
    for captured, payload in runtime.pending:
        runtime.state = captured.model_copy(update={"revision": runtime.state.revision})
        _persist_transition(runtime, store, payload)  # type: ignore[arg-type]
    if runtime.state.status is not RunStatus.RUNNING:
        _finish(runtime, store)
        return _execution_result(runtime)

    try:
        build_inventory_catalog(runtime)
    except Exception:  # noqa: BLE001 - 名称解析失败就不能把未核验的行发出去
        _stop(runtime, kind="unavailable", code="result_contract_violation",
              problems=["result_contract_violation"], stage=InventoryNode.PERSIST_ALERTS,
              message="查询结果异常。")
        _persist_transition(runtime, store, _event_payload(runtime))  # type: ignore[arg-type]
        _finish(runtime, store)
        return _execution_result(runtime)

    assert runtime.provenance is not None and runtime.identity is not None
    store.record_provenance(runtime.state.run_id, provenance=runtime.provenance,
                            identity=runtime.identity)
    runtime.state = transition_state(runtime.state, InventoryNode.PERSIST_ALERTS)
    persist_alerts(runtime, store)
    _persist_transition(runtime, store, _event_payload(runtime))  # type: ignore[arg-type]
    if runtime.state.status is not RunStatus.RUNNING:
        _finish(runtime, store)
        return _execution_result(runtime)
    runtime.state = transition_state(runtime.state, InventoryNode.FINALIZE)
    finalize_run(runtime, store)
    return _execution_result(runtime)


def _finish(runtime: InventoryRuntime, store: Any) -> None:
    """提前收尾：终止原因与血缘必须跟终态一起落库。"""
    previous = runtime.state
    if runtime.provenance is not None or runtime.identity is not None:
        store.record_provenance(previous.run_id, provenance=runtime.provenance,
                                identity=runtime.identity)
    finished = previous.model_copy(update={"revision": previous.revision + 1})
    store.finish(finished.run_id, RunCompletion(
        expected_revision=previous.revision, node=finished.node.value,
        status=finished.status, state=finished.model_dump(mode="json"),
        payload=_event_payload(runtime),  # type: ignore[arg-type]
        error_code=finished.error.code if finished.error else None,
        termination_reason=_termination_reason(runtime, finished.status)))
    runtime.state = finished


def _execution_result(runtime: InventoryRuntime) -> InventoryExecution:
    from .tool import model_payload

    state = runtime.state
    status = state.target_status or DomainStatus.FAILED
    report = runtime.report or _report(runtime, status="unavailable")
    safe = not (state.error is not None and state.error.code
                in {"artifact_persistence_failed", "result_contract_violation"})
    model: dict[str, object]
    artifacts: list[DomainArtifact] = []
    if runtime.catalog is not None and runtime.published and safe:
        try:
            model = model_payload(runtime, runtime.catalog, report)
            artifacts = [DomainArtifact(ref=ref, public_payload=payload)
                         for ref, payload in runtime.published]
        except ValueError:
            # 投影失败说明结果不符合安全契约：三个出口一起关闭，被拒数字不得回流。
            status = DomainStatus.FAILED
            model = {"status": "failed"}
            artifacts = []
            safe = False
    else:
        model = _silent_payload(report, safe=safe)
    return InventoryExecution(
        domain_result=DomainResult(
            run_id=state.run_id, status=status, model_payload=model,
            artifacts=artifacts, data_as_of=state.data_as_of, coverage=None,
            error=state.error, provenance=runtime.provenance, identity=runtime.identity),
        report=report if safe else None, session_filters={})


def _silent_payload(report: InventoryAlertReport, *, safe: bool) -> dict[str, object]:
    """没有可发预警表时的模型载荷：只给状态与已登记的披露，数量一个都不给。

    授权失败连原因都不发：一句"这家店不在授权范围"本身就在证实那家店存在，而越权
    引用该得到的只是一个否。其余情态必须带披露：经营者要能分清"没解析出 SKU"、
    "来源没取证"、"池没授权"是三个不同的答案。
    """
    if not safe or report.status == "forbidden":
        return {"status": "failed"}
    payload: dict[str, object] = {"status": report.status}
    if report.limitations:
        payload["limitations"] = list(report.limitations)
    return payload
