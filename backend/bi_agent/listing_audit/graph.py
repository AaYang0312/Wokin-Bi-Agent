"""ListingPriceAuditGraph：spec §6 的固定节点链（运营工作流计划 Task 9）。

    resolve_scope_product_and_skus → load_expected_listing_roster
    → capture_user_expected_prices → check_listing_source → load_listing_snapshot
    → verify_completeness_and_freshness → join_expected_and_actual
    → compare_decimal_prices → classify_discrepancies → persist_audit → finalize

十一格各自回答一个**可归因**的问题，链上没有一处可以合并：范围与商品身份、期望
roster、本轮目标价、来源是否已核验、快照读不读得到、快照新不新、期望与实际怎么对上、
金额怎么比、结论怎么分类、结果怎么发布。合并任意两格，逐格状态就失去归因对象，
而那份差异表也就没法告诉经营者下一步该去补哪一件证据。

四条结构性约束：

1. **分母是期望 roster**，不是抓到的链接数：用实际上架表 LEFT JOIN 期望表才会让
   「缺一家」演成「那一家不存在」，从而报出「全部正确」。
2. **来源门禁先于一切比较**。没有已核验的渠道在售价来源，就一格都不判（连采集到的
   价格都不发），只报 `unsupported`。ERP 建议价、历史成交均价、上一轮目标价都不能
   替代（spec §9）。
3. **缺证据不是缺数据**。`not_listed` 要有全量枚举凭据；过期快照给 `stale`；
   币种不可比给 `unknown`。每种状态都写清"要补什么"，不把 null 演成 0，
   也不把不一致重试到匹配为止（实价不同是业务发现）。
4. **降级有边界**。单格证据不足可以是 partial；授权失败、契约违规、必需结果存不下
   一律 failed。`all_correct` 只在每一格都有新鲜、完整且匹配的证据时才成立。
"""

from __future__ import annotations

from dataclasses import dataclass, field
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
    AuditItem,
    DomainContext,
    JoinedItem,
    ListingAuditReport,
    ListingPriceAuditRequest,
    RosterItem,
    SnapshotBundle,
)
from .rules import (
    JUDGED_STATUSES,
    UNDECIDED_STATUSES,
    LISTING_GRAPH_VERSION,
    LISTING_METRIC_VERSION,
    LISTING_NO_MAPPING,
    LISTING_RULE_VERSION,
    LISTING_SCHEMA_VERSION,
    LISTING_SOURCE_REGISTRY_VERSION,
    LISTING_TEMPLATE_ID,
    LISTING_TEMPLATE_VERSION,
    MAX_ROSTER_ITEMS,
    RosterConflict,
    amount_difference,
    beijing_iso,
    compare_price,
    currency_precision,
    freshness_ok,
    listing_limitation_codes,
    price_text,
    resolve_price_rules,
    verified_listing_source,
)

DOMAIN = "listing_price_audit"

# 公开披露文本（全部登记在 `rules.LISTING_CODE_BY_TEXT`，未登记的一句不进状态）。
TEXT_SOURCE_UNVERIFIED = "渠道在售价来源尚未取证，本次上架复核不能判定"
TEXT_SNAPSHOT_MISSING = "本轮没有该店铺的渠道在售快照，未判定项保持未知"
TEXT_STALE = "快照已超过该来源的时效策略，按过期披露，不判正确"
TEXT_ENUMERATION = "缺少店铺全量上架枚举证据，不能判定未上架"
TEXT_CURRENCY = "快照币种与本轮目标价币种不一致，不能比较"
TEXT_PRICE_BASIS = "快照未声明活动价，按请求的活动价口径无法判定"
TEXT_EXPECTATION_MISSING = "部分期望项没有本轮目标价，不判通过"
TEXT_CONFLICT = "目标价规则相互冲突，请先确认每个规格的目标价"
TEXT_UNMAPPED = "该 SKU 没有已确认的渠道落点映射，未判定"
TEXT_MISMATCH = "存在目标价与实际价不一致的项，这是业务发现，不重试到匹配为止"
TEXT_NOT_LISTED = "存在经完整上架枚举确认的未上架项"
TEXT_NOT_ON_SALE = "存在当前不在售的链接"
TEXT_INCOMPLETE = "期望项未全部判定，不能声称全部正确"
TEXT_EMPTY_SCOPE = "授权范围内没有可复核的店铺"
TEXT_UNRESOLVED = "本轮授权范围内没有解析出该商品，不能当成没有上架项"
TEXT_AMBIGUOUS = "该商品命中多个候选，请指定要复核哪一个"
TEXT_ROSTER_TOO_LARGE = "期望复核项超过上限，已拒绝出数以避免静默截断；请缩小店铺或规格范围"


class ListingNode(StrEnum):
    RESOLVE_SCOPE_PRODUCT_AND_SKUS = "resolve_scope_product_and_skus"
    LOAD_EXPECTED_LISTING_ROSTER = "load_expected_listing_roster"
    CAPTURE_USER_EXPECTED_PRICES = "capture_user_expected_prices"
    CHECK_LISTING_SOURCE = "check_listing_source"
    LOAD_LISTING_SNAPSHOT = "load_listing_snapshot"
    VERIFY_COMPLETENESS_AND_FRESHNESS = "verify_completeness_and_freshness"
    JOIN_EXPECTED_AND_ACTUAL = "join_expected_and_actual"
    COMPARE_DECIMAL_PRICES = "compare_decimal_prices"
    CLASSIFY_DISCREPANCIES = "classify_discrepancies"
    PERSIST_AUDIT = "persist_audit"
    FINALIZE = "finalize"


# 链是固定的：下一格由这张表决定，不在运行时按状态挑。
_ORDER: tuple[ListingNode, ...] = tuple(ListingNode)
_NEXT_NODE: dict[ListingNode | None, ListingNode] = {None: _ORDER[0]}
_NEXT_NODE.update({_ORDER[index]: _ORDER[index + 1]
                   for index in range(len(_ORDER) - 1)})


class InvalidListingTransition(Exception):
    def __init__(self) -> None:
        super().__init__("invalid_listing_transition")


class InvalidListingRequest(ValueError):
    """入参契约不匹配：拿别的领域的请求跑这张图，会发出一份形状正确的假复核表。"""

    def __init__(self) -> None:
        super().__init__("listing_audit_request_required")


class ListingState(BusinessQueryState):
    """同一份可持久化状态契约，只换节点类型。

    状态能带哪些键、限制能取哪些码、引用长什么样，全部由 `runtime.models` 一处决定；
    这里不复制第二份白名单。
    """

    node: ListingNode = ListingNode.RESOLVE_SCOPE_PRODUCT_AND_SKUS


def transition_state(state: ListingState, next_node: ListingNode) -> ListingState:
    """只允许沿链前进一步：跳格与回退都拒，否则"哪一格没执行"就没法回答。"""
    expected = _NEXT_NODE.get(state.node)
    if expected is None or expected is not next_node:
        raise InvalidListingTransition()
    return state.model_copy(update={"node": next_node})


@dataclass
class ListingRuntime:
    """图内可变状态：真实主键只活在这里，不进 Store、不进事件、不进模型载荷。"""

    state: ListingState
    context: DomainContext
    tool_call_id: str
    request: ListingPriceAuditRequest | None = None
    report: ListingAuditReport | None = None
    # `result` 只为事件载荷存在：本域没有 ToolResult 那种指标结果，行数据走自己的形状。
    result: ToolResult | None = None
    catalog: Any = None
    provenance: QueryProvenance | None = None
    identity: RequestIdentity | None = None
    # 范围与商品
    profiles: dict[str, Any] = field(default_factory=dict)
    candidate_shop_ids: tuple[str, ...] = ()
    excluded: list[dict[str, Any]] = field(default_factory=list)
    resolution: ProductResolution | None = None
    erp_product_id: str | None = None
    candidates: tuple[dict[str, str], ...] = ()
    # 期望 roster 与本轮目标价
    roster: tuple[RosterItem, ...] = ()
    expectations: dict[tuple, str] = field(default_factory=dict)
    mapping_version: str | None = None
    # 来源与快照
    bundles: dict[tuple[str, str], SnapshotBundle] = field(default_factory=dict)
    source_ready: dict[str, bool] = field(default_factory=dict)
    policy_seconds: dict[str, int] = field(default_factory=dict)
    fresh: dict[tuple[str, str], bool] = field(default_factory=dict)
    enumerated: dict[tuple[str, str], bool] = field(default_factory=dict)
    joined: tuple[JoinedItem, ...] = ()
    # 判定与发布
    items: tuple[AuditItem, ...] = ()
    summary: dict[str, Any] = field(default_factory=dict)
    limitations: list[str] = field(default_factory=list)
    data_as_of: datetime | None = None
    source_batches: tuple[str, ...] = ()
    final_status: RunStatus | None = None
    pending: list[tuple[ListingState, dict[str, object]]] = field(default_factory=list)
    published: list[tuple[Any, dict[str, Any]]] = field(default_factory=list)


# ---------------------------------------------------------------------------
# 节点 1：授权范围 + 商品 + SKU
# ---------------------------------------------------------------------------


def resolve_scope_product_and_skus(runtime: ListingRuntime) -> None:
    """展开获准店铺并解析商品；显式越权引用直接 forbidden，不静默剔除。

    目标店铺集合来自**用户本轮选择的已授权店铺全集**，不来自「已有 listing 或订单」
    的集合（spec §5.4）：一家从没上架过的获准店铺也必须进 roster，否则它会从分母里
    静默消失，而「全部正确」就是这么来的。
    """
    context = runtime.context
    request = runtime.request
    assert request is not None
    try:
        profiles = repository.shop_profiles(context.conn, sorted(context.allowed_shop_ids),
                                            deadline=context.deadline)
    except _BudgetExhausted:
        _stop(runtime, kind="unavailable", code="deadline_exceeded", problems=[],
              stage=ListingNode.RESOLVE_SCOPE_PRODUCT_AND_SKUS,
              message="本次查询时间预算已耗尽，请缩小日期或店铺范围后重试。",
              limitations=["本次查询时间预算已耗尽"])
        return
    runtime.profiles = {profile.shop_id: profile for profile in profiles}

    if request.scope.mode == "selected" and request.scope.shop_refs:
        wanted: list[str] = []
        for ref in request.scope.shop_refs:
            shop_id = context.ref_to_shop_id.get(ref)
            if shop_id is None or shop_id not in context.allowed_shop_ids:
                # 明确点名的引用不在授权范围内：整次请求拒绝，不删掉这家店继续算。
                _stop(runtime, kind="forbidden", code="forbidden", problems=["forbidden"],
                      stage=ListingNode.RESOLVE_SCOPE_PRODUCT_AND_SKUS,
                      message="查询范围无权限。", limitations=["店铺不在授权范围"])
                return
            wanted.append(shop_id)
    else:
        wanted = sorted(context.allowed_shop_ids)
    if request.scope.platforms:
        platforms = set(request.scope.platforms)
        wanted = [shop for shop in wanted
                  if shop in runtime.profiles and runtime.profiles[shop].platform in platforms]

    kept: list[str] = []
    for shop in wanted:
        profile = runtime.profiles.get(shop)
        if profile is None:
            _exclude(runtime, shop, "shop_not_synced", TEXT_SNAPSHOT_MISSING)
            continue
        if not profile.enabled:
            _exclude(runtime, shop, "shop_disabled", "部分店铺已停用，仅返回剩余范围")
            continue
        kept.append(shop)
    runtime.candidate_shop_ids = tuple(sorted(kept))
    runtime.state = runtime.state.model_copy(
        update={"normalized_request": _normalized_request(runtime)})
    if not runtime.candidate_shop_ids:
        _stop(runtime, kind="missing_data", code="missing_parameters", problems=[],
              stage=ListingNode.RESOLVE_SCOPE_PRODUCT_AND_SKUS,
              message="缺少查询参数，请补充后重试。", limitations=[TEXT_EMPTY_SCOPE])
        return
    _resolve_product(runtime)


def _resolve_product(runtime: ListingRuntime) -> None:
    """解析商品身份：歧义交回澄清，零候选不是「没有上架项」。"""
    request = runtime.request
    assert request is not None
    selector = Selector(ref=request.product.ref, text=request.product.text,
                        sku_refs=tuple(request.product.sku_refs))
    try:
        resolution = resolve_product(runtime.context.conn, selector=selector,
                                     authorized_shop_ids=runtime.candidate_shop_ids,
                                     at=runtime.context.now.date())
    except _BudgetExhausted:
        _stop(runtime, kind="unavailable", code="deadline_exceeded", problems=[],
              stage=ListingNode.RESOLVE_SCOPE_PRODUCT_AND_SKUS,
              message="本次查询时间预算已耗尽，请缩小日期或店铺范围后重试。",
              limitations=["本次查询时间预算已耗尽"])
        return
    runtime.resolution = resolution
    if resolution.status == "ambiguous":
        # 只发引用：这条路径不建目录，也就没有已授权的展示名可发（与经营图同一处置）。
        runtime.candidates = tuple({"ref": item.product_ref}
                                   for item in resolution.candidates[:20])
        _stop(runtime, kind="needs_input", code="missing_parameters", problems=[],
              stage=ListingNode.RESOLVE_SCOPE_PRODUCT_AND_SKUS,
              message="缺少查询参数，请补充后重试。", limitations=[TEXT_AMBIGUOUS])
        return
    if resolution.status != "resolved" or not resolution.erp_product_id:
        # 「解析不出来」与「这个商品没上架」是两个答案：后者要有快照证据才可以说。
        _stop(runtime, kind="missing_data", code="product_not_resolved", problems=[],
              stage=ListingNode.RESOLVE_SCOPE_PRODUCT_AND_SKUS,
              message="缺少查询参数，请补充后重试。", limitations=[TEXT_UNRESOLVED])
        return
    runtime.erp_product_id = resolution.erp_product_id
    runtime.mapping_version = resolution.mapping_version


# ---------------------------------------------------------------------------
# 节点 2：期望 roster（目标上架全集）
# ---------------------------------------------------------------------------


def load_expected_listing_roster(runtime: ListingRuntime) -> None:
    """从本轮授权店铺集合 × 商品 SKU / 渠道落点展开期望项。

    落点取自 `bi.channel_items`（Task 6），**不**从订单历史推导：没成交的已映射链接
    照样要复核。一个 SKU 有多条链接就出多格，逐条比，不取最便宜那条（spec §5.4）。
    """
    context = runtime.context
    request = runtime.request
    assert request is not None and runtime.erp_product_id is not None
    try:
        landings = repository.channel_landings(
            context.conn, erp_product_id=runtime.erp_product_id,
            shop_ids=list(runtime.candidate_shop_ids), at=context.now.date(),
            deadline=context.deadline)
    except _BudgetExhausted:
        _stop(runtime, kind="unavailable", code="deadline_exceeded", problems=[],
              stage=ListingNode.LOAD_EXPECTED_LISTING_ROSTER,
              message="本次查询时间预算已耗尽，请缩小日期或店铺范围后重试。",
              limitations=["本次查询时间预算已耗尽"])
        return
    items: list[RosterItem] = []
    covered: set[str] = set()
    for landing in landings:
        # 只有 approved 的落点才能定位"哪一条链接"：ambiguous / unresolved 是候选，
        # 拿它们展开 roster 等于让未确认的合并决定复核范围。
        if landing.status != "approved" or not landing.listing_id:
            continue
        if not runtime.mapping_version:
            runtime.mapping_version = landing.mapping_version
        items.append(landing.roster_item(ref_for_key(EntityKind.SHOP.value,
                                                     landing.shop_id)))
        if landing.erp_sku_id:
            covered.add(ref_for_key(EntityKind.SKU.value, landing.erp_sku_id))
    # 用户点名、本轮没有任何落点的 SKU：身份已知、落点未知，仍要占一格（→ unmapped），
    # 不然"这个规格没上架"会被演成"本轮没问这个规格"。
    for sku_ref in sorted({str(ref) for ref in request.product.sku_refs} - covered):
        for shop_id in runtime.candidate_shop_ids:
            items.append(RosterItem(shop_id=shop_id,
                                    shop_ref=ref_for_key(EntityKind.SHOP.value, shop_id),
                                    named_sku_ref=sku_ref))
    if not items:
        # 既无落点也无点名 SKU：按「这一家有没有这个商品」出产品级一格。
        for shop_id in runtime.candidate_shop_ids:
            items.append(RosterItem(shop_id=shop_id,
                                    shop_ref=ref_for_key(EntityKind.SHOP.value, shop_id)))
    unique = {(item.shop_id, item.namespace, item.listing_id, item.platform_sku_id,
               item.erp_sku_id, item.named_sku_ref): item for item in items}
    items = sorted(unique.values(), key=lambda item: (item.shop_id, item.listing_id,
                                                      item.platform_sku_id,
                                                      item.sku_ref or ""))
    if len(items) > MAX_ROSTER_ITEMS:
        # 全量盘点扫不完就必须拒绝：先取 Top N 再报"复核完成"是最坏的一种答案。
        _stop(runtime, kind="unavailable", code="result_too_large", problems=[],
              stage=ListingNode.LOAD_EXPECTED_LISTING_ROSTER,
              message="查询暂不可用，请稍后重试。",
              limitations=[TEXT_ROSTER_TOO_LARGE])
        return
    runtime.roster = tuple(items)


# ---------------------------------------------------------------------------
# 节点 3：本轮目标价
# ---------------------------------------------------------------------------


def capture_user_expected_prices(runtime: ListingRuntime) -> None:
    """把用户本轮指定的目标价摊到期望项上。

    这一格**只读 `request.expected_prices`**：不查聊天历史、不查 `SessionState.filters`、
    不读 `bi.price_audit_expectations`。上一轮的目标价不是本轮的依据（spec §5.4
    默认不继承），档案建议价与历史成交均价同样不是。

    一部分项没被任何规则命中 → 那些格是 `missing_standard`，整份不判通过；不拿已有的
    价去补没给的格（那正是"猜一个标准"）。规则冲突 → `needs_input`，不取平均。
    """
    request = runtime.request
    assert request is not None
    keys = [item.key for item in runtime.roster]
    try:
        resolved = resolve_price_rules(request.rules(), keys)
    except RosterConflict:
        _stop(runtime, kind="needs_input", code="missing_parameters", problems=[],
              stage=ListingNode.CAPTURE_USER_EXPECTED_PRICES,
              message="缺少查询参数，请补充后重试。", limitations=[TEXT_CONFLICT])
        return
    runtime.expectations = resolved
    if len(resolved) < len(runtime.roster):
        runtime.limitations.append(TEXT_EXPECTATION_MISSING)
    runtime.state = runtime.state.model_copy(
        update={"normalized_request": _normalized_request(runtime)})


# ---------------------------------------------------------------------------
# 节点 4：来源门禁
# ---------------------------------------------------------------------------


def check_listing_source(runtime: ListingRuntime) -> None:
    """这一家的渠道在售价算不算「已核验来源」——先看来源成不成立，再看快照有没有。

    两道门禁缺一不可：快照表里有行只证明"有人导入过一批数"，不证明那批数来自已核验
    通道；反过来，注册表开了但本轮读不到快照也判不了。**默认注册表为空**，所以真实
    部署下每一格都是 `unsupported`（`tests/test_listing_audit.py` 钉住这一点）。
    """
    ready: dict[str, bool] = {}
    policies: dict[str, int] = {}
    for shop_id in runtime.candidate_shop_ids:
        platform = runtime.profiles[shop_id].platform
        entry = verified_listing_source(platform)
        ready[shop_id] = entry is not None
        policies[shop_id] = 0 if entry is None else int(entry.max_age_seconds)
    runtime.source_ready = ready
    runtime.policy_seconds = policies
    if not any(ready.values()):
        runtime.limitations.append(TEXT_SOURCE_UNVERIFIED)


# ---------------------------------------------------------------------------
# 节点 5：读取快照
# ---------------------------------------------------------------------------


def load_listing_snapshot(runtime: ListingRuntime) -> None:
    """一条集合查询取回各店本轮那一批快照（不逐店循环，也不跨批混用）。"""
    context = runtime.context
    wanted = [shop for shop in runtime.candidate_shop_ids if runtime.source_ready.get(shop)]
    if not wanted:
        runtime.bundles = {}
        return
    runtime.bundles = repository.latest_snapshots(context.conn, shop_ids=wanted,
                                                  as_of=context.now,
                                                  deadline=context.deadline)
    # 一家店在任何账号范围里都没读到快照才说"缺快照"：同一个店在两个 namespace 下
    # 各有一批时不是缺，那两批会在 join 那一格各自被对到。
    covered = {shop for shop, _namespace in runtime.bundles}
    if any(shop not in covered for shop in wanted):
        runtime.limitations.append(TEXT_SNAPSHOT_MISSING)
    runtime.source_batches = tuple(sorted({bundle.header.snapshot_id
                                           for bundle in runtime.bundles.values()}))


# ---------------------------------------------------------------------------
# 节点 6：完整性与时效
# ---------------------------------------------------------------------------


def verify_completeness_and_freshness(runtime: ListingRuntime) -> None:
    """快照能不能代表「现在」的标价：逐店判时效与枚举完整性。

    时效按该来源已批准的 freshness policy 判；缺抓取时间在类型上就是 None，这里按
    不新鲜处理。枚举完整性决定缺行能不能说成 `not_listed`（spec §5.4）。
    """
    fresh: dict[tuple[str, str], bool] = {}
    enumerated: dict[tuple[str, str], bool] = {}
    for (shop_id, namespace), bundle in runtime.bundles.items():
        # 时效上限来自该平台的已核验来源（节点 4 已逐店算好）。读不到快照的店不在
        # 这个循环里：它们的结论是 `unknown`，不是 `stale`——两者要补的东西不同。
        key = (shop_id, namespace)
        fresh[key] = freshness_ok(captured_at=bundle.header.captured_at,
                                  now=runtime.context.now,
                                  max_age_seconds=runtime.policy_seconds.get(
                                      shop_id, 0))
        enumerated[key] = bool(bundle.header.enumeration_complete)
    runtime.fresh = fresh
    runtime.enumerated = enumerated
    if fresh and not all(fresh.values()):
        runtime.limitations.append(TEXT_STALE)
    if enumerated and not any(enumerated.values()):
        runtime.limitations.append(TEXT_ENUMERATION)
    stamps = [bundle.header.captured_at for bundle in runtime.bundles.values()
              if bundle.header.captured_at is not None
              # 无时区的抓取时间不能当共同截止：同一个文本会被一个读者当 UTC、另一个当其
            # 本地时间，而两者能差八小时。时效判定已经把它归入 stale，这里同理不入总截止。
              if bundle.header.captured_at.tzinfo is not None]
    if stamps:
        # 共同截止：一次复核只说一个"什么时候的价格"，取最早的那一批。
        runtime.data_as_of = min(stamps)


# ---------------------------------------------------------------------------
# 节点 7：期望 LEFT JOIN 实际
# ---------------------------------------------------------------------------


def join_expected_and_actual(runtime: ListingRuntime) -> None:
    """把每一格与它在本轮快照里对应的那条记录对上；对不上的格保留。

    匹配按**从具体到宽**分四轮，而且每条快照记录只能被一格占住：

    1. `(链接, 平台 SKU)` 与映射给出的落点完全相等；
    2. 来源声明了同一个 ERP SKU（两边都写了链接号时，链接号也必须一致）；
    3. 只对得上链接号（来源不拆平台 SKU 时的唯一形状）；
    4. 产品级格（完全没有 SKU 身份）对来源声明的同一 ERP 商品。

    为什么"一条记录只能被一格占住"不是多余的谨慎：同一个 SKU 有两条链接时，如果两轮
    都让同一个 `erp_sku` 抢到第一条记录，第二条链接就从未被看过，而它的行会带着第一条
    链接的价格发出去——spec §5.4 禁的"只取一个链接"正是以这种形式重现的。
    名称不参与匹配：规格文本相似不等于同一个 SKU（Task 6 红线）。
    """
    claimed: dict[tuple[str, str], set[int]] = {}
    matched: dict[int, tuple[tuple[str, str], int]] = {}
    pending = list(enumerate(runtime.roster))
    for round_key in _MATCH_ROUNDS:
        still: list[tuple[int, RosterItem]] = []
        for position, item in pending:
            candidates = _bundle_keys(runtime, item)
            if not candidates:
                still.append((position, item))
                continue
            found = _find_match(item, candidates, round_key, claimed, runtime,
                                erp_product_id=runtime.erp_product_id or "")
            if found is None:
                still.append((position, item))
                continue
            key, index = found
            claimed.setdefault(key, set()).add(index)
            matched[position] = found
        pending = still
    joined: list[JoinedItem] = []
    for position, item in enumerate(runtime.roster):
        hit = matched.get(position)
        header_key = _cell_header(runtime, item, hit)
        header = runtime.bundles[header_key].header if header_key else None
        joined.append(JoinedItem(
            roster=item, header=header,
            matched=None if hit is None else runtime.bundles[hit[0]].items[hit[1]],
            expected_amount=runtime.expectations.get(item.key),
            source_ready=bool(runtime.source_ready.get(item.shop_id)),
            fresh=bool(runtime.fresh.get(header_key)),
            enumerated=bool(runtime.enumerated.get(header_key))))
    runtime.joined = tuple(joined)


# 从具体到宽：靠前的轮次先挑，所以"两边都说清了是哪条链接"永远优先于"来源顺手报了
# 一个 SKU 号"。这个顺序就是匹配精度，不是一份可以随意重排的列表。
_MATCH_ROUNDS: tuple[str, ...] = ("landing", "sku", "listing", "product")


def _bundle_keys(runtime: ListingRuntime, item: RosterItem) -> list[tuple[str, str]]:
    """这一格允许和哪些快照批对得上。

    账号范围明确的格只能用自己那一批；没有落点可依托的格（产品级那一格）在本店只有一批
    快照时归属唯一，多批时不归属任何一批："这一家到底在不在架"问的是哪个账号，代码回答
    不了，只能由 `unknown` 说不知道。
    """
    if item.namespace:
        key = (item.shop_id, item.namespace)
        return [key] if key in runtime.bundles else []
    return [key for key in runtime.bundles if key[0] == item.shop_id]


def _cell_header(runtime: ListingRuntime, item: RosterItem,
                 hit: tuple[tuple[str, str], int] | None) -> tuple[str, str] | None:
    """这一格由哪一批快照的头说话（时效与"全集已枚举"都记在头那一行）。

    对到了就用对到的那一批；没对到时，只有归属唯一才借得到那一批的枚举凭据。
    """
    if hit is not None:
        return hit[0]
    candidates = _bundle_keys(runtime, item)
    return candidates[0] if len(candidates) == 1 else None


def _find_match(item: RosterItem, candidates, round_key: str,
                claimed: dict[tuple[str, str], set[int]], runtime: ListingRuntime, *,
                erp_product_id: str) -> tuple[tuple[str, str], int] | None:
    """返回这一格在本轮应占的 `(快照批, 明细下标)`；对不上返回 None。"""
    for key in candidates:
        for index, candidate in enumerate(runtime.bundles[key].items):
            if index in claimed.get(key, ()):
                continue      # 一条记录只回答一格：见上面的"一条记录只能被一格占住"
            if round_key == "landing":
                if item.has_landing and candidate.listing_id == item.listing_id                         and candidate.platform_sku_id == item.platform_sku_id:
                    return key, index
            elif round_key == "sku":
                # SKU 相同还不够：两边都写了链接号时链接号也必须一致，否则同一 SKU 的
                # 两条链接会互相抢对方那一格。
                if item.erp_sku_id and candidate.erp_sku_id == item.erp_sku_id                         and not (item.listing_id and candidate.listing_id
                                 and item.listing_id != candidate.listing_id):
                    return key, index
            elif round_key == "listing":
                if item.has_landing and candidate.listing_id == item.listing_id                         and not candidate.platform_sku_id:
                    return key, index
            elif round_key == "product":
                if (not item.erp_sku_id and not item.has_landing and erp_product_id
                        and candidate.erp_product_id == erp_product_id):
                    return key, index
    return None




# ---------------------------------------------------------------------------
# 节点 8：金额比较与逐格状态
# ---------------------------------------------------------------------------


def compare_decimal_prices(runtime: ListingRuntime) -> None:
    """逐格定状态并比金额。比较本身由 `rules.compare_price` 做，这一格管顺序。

    顺序本身就是结论的一部分，不可调换：

    1. 来源未取证 → `unsupported`（不看快照，否则会把"导入过一批数"说成"复核过"）；
    2. 本轮读不到快照 → `unknown`；
    3. 快照过期 → `stale`（价格相同也不判 match：过期数据证明不了"现在"是对的）；
    4. 币种不一致 → `unknown`（没有换算依据的金额比没有金额更危险）；
    5. 快照里没有这一格 → 有全量枚举凭据才判 `not_listed`；有落点没凭据判 `unknown`；
       连落点都没有判 `unmapped`；
    6. 本轮没给这一格的目标价 → `missing_standard`（仍展示已采集的实价，不判通过）；
    7. 请求口径下来源没声明价格 → `unknown`；
    8. 链接不在售 → `not_on_sale`；
    9. 其余才进 `compare_price` → `match` / `mismatch`。
    """
    request = runtime.request
    assert request is not None
    rows: list[AuditItem] = []
    for line in runtime.joined:
        rows.append(_judge(runtime, line, price_basis=request.price_basis,
                           currency=request.currency))
    _guard_invariants(runtime, rows)
    runtime.items = tuple(rows)


def _judge(runtime: ListingRuntime, line: JoinedItem, *, price_basis: str,
           currency: str) -> AuditItem:
    item = line.roster
    # 时点与"全集已枚举"都由这一格归属的那一批快照说：`header` 为 None 就是没归属。
    snapshot_at = beijing_iso(line.header.captured_at) if line.header else None

    def build(status: str, *, actual: str | None = None, difference: str | None = None,
              at: str | None = snapshot_at) -> AuditItem:
        return AuditItem(roster=item, status=status,
                         expected_price=line.expected_amount, actual_price=actual,
                         difference=difference, snapshot_at=at, currency=currency,
                         price_basis=price_basis)

    if not line.source_ready:
        # 来源未取证：连采集到的价格也不发，不给"看着像能用"留任何余地。
        return build("unsupported", at=None)
    if line.header is None:
        # 本店本轮没读到快照，或读到了多批但这一格无法归属到任何一批：两者都是"没有
        # 可比的在架数据"，都不是"没上架"。
        return build("unknown")
    if not line.fresh:
        return build("stale")
    matched = line.matched
    if matched is None:
        if line.enumerated:
            return build("not_listed")
        if item.has_landing or item.sku_ref:
            return build("unknown")
        _note(runtime, TEXT_UNMAPPED)
        return build("unmapped")
    at = beijing_iso(matched.captured_at) or snapshot_at
    if str(matched.currency or "").strip().upper() != str(currency).strip().upper():
        _note(runtime, TEXT_CURRENCY)
        return build("unknown", at=at)
    if line.expected_amount is None:
        # 缺标准：把已采集的实价照发（spec §6），但这一格没资格进"通过"的分子。
        return build("missing_standard",
                     actual=price_text(matched.amount_for(price_basis), currency), at=at)
    actual = matched.amount_for(price_basis)
    if actual is None:
        if price_basis == "campaign_price":
            _note(runtime, TEXT_PRICE_BASIS)
        return build("unknown", at=at)
    if not matched.on_sale:
        # 不在售是判得出来的结论（不是"价格未知"），但它不等于标价正确。
        return build("not_on_sale", actual=price_text(actual, currency), at=at)
    status = compare_price(actual, line.expected_amount, currency=currency)
    shown = price_text(actual, currency)
    if status == "unknown":
        return build(status, at=at)
    if status == "missing_standard":      # 理论到不了：上面已经拦过
        return build(status, actual=shown, at=at)
    difference = amount_difference(shown, line.expected_amount, currency=currency) \
        if status in JUDGED_STATUSES else None
    return build(status, actual=shown, difference=difference, at=at)


def _guard_invariants(runtime: ListingRuntime, rows: Sequence[AuditItem]) -> None:
    """出表前的自检：非判定态不得带差额，判定态必须两侧都有金额。

    这些不是风格检查。一个 `stale` 行带着差额，就会被模型当"当前差 10 元"转述出去；
    一个没有实价的 `match` 则是一个没有依据的结论。宁可在这里抛错（外层降级为
    `result_contract_violation`，一个数字都不发），也不把错话说圆。
    """
    for row in rows:
        judged = row.status in JUDGED_STATUSES
        if judged and (row.actual_price is None or row.expected_price is None):
            raise ValueError("listing_judgement_needs_both_sides")
        if not judged and row.difference is not None:
            raise ValueError("listing_difference_needs_a_judgement")
        if judged and currency_precision(row.currency) is None:
            raise ValueError("listing_judgement_needs_a_verified_currency")


def _note(runtime: ListingRuntime, text: str) -> None:
    if text not in runtime.limitations:
        runtime.limitations.append(text)


# ---------------------------------------------------------------------------
# 节点 9：结论与状态
# ---------------------------------------------------------------------------


def classify_discrepancies(runtime: ListingRuntime) -> None:
    """按期望项分母出结论：全部通过要求每一格都有有效匹配证据。

    - 一格都没判成（全 `unsupported` 等）→ `missing_data`：这不是"复核完成且没问题"；
    - 判成过但不完整，或发现了不一致 → `partial`；
    - 每一格都是 `match` → `success`。
    """
    counts: dict[str, int] = {}
    for row in runtime.items:
        counts[row.status] = counts.get(row.status, 0) + 1
    expected = len(runtime.roster)
    matched = counts.get("match", 0)
    evaluated = matched + counts.get("mismatch", 0)
    all_correct = bool(expected > 0 and evaluated == expected and matched == expected)
    # "没定下来"的那一类：需要去补证据而不是去改价的格。`not_listed` 与
    # `not_on_sale` **不在**这一类里：它们是关于货架的确定结论，只是不是"价格一致"。
    undecided = sum(counts.get(status, 0) for status in UNDECIDED_STATUSES)
    summary: dict[str, Any] = {
        "expected_items": expected,
        "evaluated_items": evaluated,
        "matched_items": matched,
        "all_correct": all_correct,
        "counts": dict(sorted(counts.items())),
        "sources": _source_claims(runtime),
        "rule_version": LISTING_RULE_VERSION,
    }
    policies = sorted({value for value in runtime.policy_seconds.values() if value})
    if policies:
        # 一次复核只报一个时效上限：多家店取最严的那一个，不拿宽的说成都能用。
        summary["freshness_policy_seconds"] = policies[0]
    runtime.summary = summary
    if counts.get("mismatch"):
        _note(runtime, TEXT_MISMATCH)
    if counts.get("not_listed"):
        _note(runtime, TEXT_NOT_LISTED)
    if counts.get("not_on_sale"):
        _note(runtime, TEXT_NOT_ON_SALE)
    if undecided:
        # 这句说的必须是"还有格没定下来"（要去补证据），而不是"没全对"：一份完整的
        # "这家店确实没上架"的报告已经被判完了，只是结论不好看。把两句混用会让经营者
        # 去补根本不缺的证据。
        _note(runtime, TEXT_INCOMPLETE)

    # 状态词说的是"这份复核做完没有"，不是"结果好不好看"：
    #   全部格都有确定结论 → ok（哪怕全是 mismatch）；部分格没定下来 → partial；
    #   一格都没定下来 → missing_data。把 mismatch 说成 partial 会楁掉另一个信号：
    # partial 在本应用里一贯意思是"有范围没评到"，两者混用就没人能拿它决定下一步。
    # "全部正确"的声称另走 `all_correct`，它才是只给逐格都新鲜完整且一致的。
    if undecided == 0:
        status = "ok"
    elif undecided < expected:
        status = "partial"
    else:
        status = "missing_data"
    runtime.state = runtime.state.model_copy(update={
        "target_status": {"ok": DomainStatus.SUCCESS, "partial": DomainStatus.PARTIAL,
                          "missing_data": DomainStatus.MISSING_DATA}[status],
        "tool_status": {"ok": "ok", "partial": "ok",
                        "missing_data": "missing_data"}[status],
        "limitations": listing_limitation_codes(runtime.limitations),
        "data_as_of": runtime.data_as_of,
        "normalized_request": _normalized_request(runtime)})
    # 结论已定：先把终态定下来，再算血缘与终止原因。顺序不能倒：血缘里的
    # `termination_reason` 要说的是"这次为什么停在这"，而它在分类节点里就已经定了。
    # 本域没有经营图那个 `freeze_versions` 节点（没有需要冻结的查询窗口），但血缘不能
    # 因没这个节点就缺席：没指纹的成功运行在运行表上就是一条"查不出谁问过什么"的记录。
    runtime.final_status = {"ok": RunStatus.SUCCEEDED, "partial": RunStatus.PARTIAL,
                            "missing_data": RunStatus.MISSING_DATA}[status]
    runtime.provenance = _provenance_of(runtime)
    runtime.identity = None
    _ensure_lineage(runtime)
    # 本节点不换 `state.status`：驱动把"非 running"当成提前终止，那样 Artifact 就永远
    # 落不了库。终态由 `finalize_run` 按 `final_status` 一次性落。
    runtime.report = _report(runtime, status=status, run_status=runtime.final_status)


def _source_claims(runtime: ListingRuntime) -> list[dict[str, Any]]:
    """逐店声明用了哪一次快照：缺快照的店在这一项里也必须能被数出来（缺项=没读到）。

    一家店都没读到时返回空列表 —— 空列表说的是"一份快照都没读"，与"读到了但都过期"
    是两个不同的答案，展示层要能分开说。
    """
    claims: list[dict[str, Any]] = []
    for (shop_id, namespace), bundle in sorted(runtime.bundles.items()):
        entry: dict[str, Any] = {
            "shop_ref": ref_for_key(EntityKind.SHOP.value, shop_id),
            "source_kind": bundle.header.source,
            "enumeration_complete": bool(bundle.header.enumeration_complete),
            "fresh": bool(runtime.fresh.get((shop_id, namespace)))}
        captured = beijing_iso(bundle.header.captured_at)
        if captured is not None:
            entry["snapshot_at"] = captured
        claims.append(entry)
    return claims


# ---------------------------------------------------------------------------
# 投影、血缘与落库材料
# ---------------------------------------------------------------------------

def audit_rows(runtime: ListingRuntime) -> list[dict[str, Any]]:
    """差异表行（已投影形态：只有引用与句柄，没有任何真实主键）。

    三个金额列**必须带着 null 出现在行里**：把键删掉，展示层就分不清"没给目标价"
    与"这一格根本没复核"。引用与时点列反过来：没有值就不发这个键（运行契约不允许
    `sku_ref: null` 这种形状）。缺价与"价格为 0"永远是两件事。
    """
    rows: list[dict[str, Any]] = []
    for row in runtime.items:
        entry: dict[str, Any] = {
            "shop_ref": row.roster.shop_ref,
            "listing_ref": row.roster.listing_ref,
            "price_basis": row.price_basis,
            "currency": row.currency,
            "audit_status": row.status,
            "expected_amount": row.expected_price,
            "actual_amount": row.actual_price,
            "amount_difference": row.difference}
        if row.roster.sku_ref:
            entry["sku_ref"] = row.roster.sku_ref
        if row.snapshot_at is not None:
            entry["snapshot_at"] = row.snapshot_at
        rows.append(entry)
    return rows


def raw_audit_rows(runtime: ListingRuntime) -> list[dict[str, Any]]:
    """行原文形态（带真实主键）：只为给 `build_catalog` 换名材料与授权复核用。"""
    rows: list[dict[str, Any]] = []
    for row in runtime.items:
        entry: dict[str, Any] = {"shop_id": row.roster.shop_id}
        if runtime.erp_product_id:
            entry["product_id"] = runtime.erp_product_id
        rows.append(entry)
    return rows


def evaluated_scope(runtime: ListingRuntime) -> dict[str, Any]:
    """本轮真正被评估的店铺集合（引用形态）。

    它就是 `candidate_shop_ids` 换一面：不重新查一遍"谁进了 roster"，因为那会让
    "已评估"与"进了 roster"在两个地方各自算一次。少了这个面，读卡片的人只能从
    `requested_scope` 减 `excluded_scope` 去推，而那一步推理由不该留给展示层。
    """
    return {"shop_refs": [ref_for_key(EntityKind.SHOP.value, shop)
                          for shop in runtime.candidate_shop_ids]}


def _requested_scope(runtime: ListingRuntime) -> dict[str, Any]:
    request = runtime.request
    if request is None:
        return {"mode": "all_authorized"}
    scope: dict[str, Any] = {"mode": request.scope.mode,
                             "platforms": list(request.scope.platforms)}
    if request.scope.shop_refs:
        scope["shop_refs"] = list(request.scope.shop_refs)
    return scope


def _normalized_request(runtime: ListingRuntime) -> dict[str, object]:
    if runtime.request is None:
        return {}
    return runtime.request.normalized()


def _exclude(runtime: ListingRuntime, shop_id: str, reason: str, limitation: str) -> None:
    """被剪掉的店要能逐项说清原因：排除一家店既不给它一个 0，也不给它一句自由文本。"""
    ref = ref_for_key(EntityKind.SHOP.value, shop_id)
    if all(str(item.get("shop_ref")) != ref for item in runtime.excluded):
        runtime.excluded.append({"shop_ref": ref, "reason": reason})
    _note(runtime, limitation)


def _resolved_product(runtime: ListingRuntime) -> dict[str, Any]:
    if runtime.erp_product_id is None:
        return {}
    resolved: dict[str, Any] = {
        "product_ref": ref_for_key(EntityKind.PRODUCT.value, runtime.erp_product_id)}
    skus = sorted({ref for ref in (item.sku_ref for item in runtime.roster) if ref})
    if skus:
        resolved["sku_refs"] = skus
    if runtime.mapping_version:
        resolved["mapping_version"] = runtime.mapping_version
    return resolved


def _provenance_of(runtime: ListingRuntime) -> QueryProvenance:
    """血缘：哪一版规则、哪一版来源注册表、哪几批快照。没读到的部分留默认，不编。"""
    return QueryProvenance(
        template_id=LISTING_TEMPLATE_ID, template_version=LISTING_TEMPLATE_VERSION,
        metric_version=LISTING_METRIC_VERSION, graph_version=LISTING_GRAPH_VERSION,
        schema_version=LISTING_SCHEMA_VERSION, catalog_version=0,
        mapping_version=runtime.mapping_version or LISTING_NO_MAPPING,
        policy_version=LISTING_SOURCE_REGISTRY_VERSION,
        source_batches=tuple(runtime.source_batches), data_as_of=runtime.data_as_of)


def _fingerprint(runtime: ListingRuntime) -> str:
    from bi_agent.runtime.artifacts import request_fingerprint

    return request_fingerprint(
        subject_id=runtime.context.subject_id,
        allowed_shop_ids=runtime.context.allowed_shop_ids,
        normalized_request=_normalized_request(runtime),
        provenance=runtime.provenance or _provenance_of(runtime))


def _ensure_lineage(runtime: ListingRuntime) -> None:
    if runtime.provenance is None:
        runtime.provenance = _provenance_of(runtime)
    if runtime.identity is None:
        # 终止原因跟着"已定下来的那个终态"：分类节点把 final_status 先定下来再算血缘，
        # 而提前终止那一途的 state.status 本身就已经是终态。
        status = runtime.final_status or runtime.state.status
        runtime.identity = RequestIdentity(
            root_request_id=runtime.context.root_request_id,
            request_fingerprint=_fingerprint(runtime),
            attempt_no=runtime.context.attempt_no, recovery_count=0,
            termination_reason=_termination_reason(runtime, status))


def _report(runtime: ListingRuntime, *, status: str,
            run_status: RunStatus | None = None) -> ListingAuditReport:
    resolved = run_status or (runtime.state.status
                              if runtime.state.status is not RunStatus.RUNNING
                              else RunStatus.SUCCEEDED)
    return ListingAuditReport(
        status=status, items=runtime.items, audit=dict(runtime.summary),
        requested_scope=_requested_scope(runtime),
        evaluated_scope=evaluated_scope(runtime),
        evaluated_shop_ids=runtime.candidate_shop_ids,
        excluded_scope=tuple(runtime.excluded),
        limitations=tuple(runtime.limitations), candidates=runtime.candidates,
        resolved_product=_resolved_product(runtime),
        termination_reason=_termination_reason(runtime, resolved),
        data_as_of=runtime.data_as_of)


# ---------------------------------------------------------------------------
# 终止与恢复映射
# ---------------------------------------------------------------------------

# `kind` 决定状态四元组：四者必须一起换（与经营图同一契约）。
_OUTCOMES = {
    "needs_input": (RunStatus.NEEDS_INPUT, DomainStatus.NEEDS_INPUT,
                    "invalid_parameters", "needs_input"),
    "missing_data": (RunStatus.MISSING_DATA, DomainStatus.MISSING_DATA,
                     "missing_data", "missing_data"),
    "forbidden": (RunStatus.FAILED, DomainStatus.FAILED, "forbidden", "forbidden"),
    "unavailable": (RunStatus.FAILED, DomainStatus.FAILED, "unavailable", "unavailable"),
}

# 归因码 → ErrorEnvelope：`ErrorCode` 与 009 的 `error_code` 同一张固定码表，本轮不扩列。
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

# 归因码 → 终止原因：与 `runtime.artifacts.TERMINATION_REASONS` 以及 009 的 SQL CHECK
# 同一词表。本轮**不新增**原因码：新增就要重新声明 CHECK 并推一条部署顺序硬约束。
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
    "listing_source_unverified": "capability_unavailable",
    "listing_snapshot_missing": "coverage_incomplete",
    "listing_snapshot_stale": "data_as_of_unknown",
    "listing_enumeration_unproven": "coverage_incomplete",
    "listing_currency_incomparable": "coverage_incomplete",
    "listing_price_basis_undeclared": "coverage_incomplete",
    "listing_sku_unmapped": "coverage_incomplete",
    "listing_expectation_missing": "missing_parameters",
    "listing_expectation_conflict": "missing_parameters",
    "listing_scope_empty": "missing_parameters",
    "listing_product_ambiguous": "missing_parameters",
    "listing_roster_truncated": "result_too_large",
    "product_not_resolved": "missing_parameters",
    "shop_not_synced": "missing_parameters",
    "listing_price_mismatch_found": "succeeded",
    "listing_not_listed_found": "succeeded",
    "listing_not_on_sale_found": "succeeded",
    "listing_audit_incomplete": "coverage_incomplete",
}


def _stop(runtime: ListingRuntime, *, kind: str, code: str, problems: Sequence[str],
          stage: ListingNode, message: str, limitations: Sequence[str] = ()) -> None:
    """提前终止：状态、血缘与报告一起定下来，后面的节点不再执行。

    `code` 是归因码（与 `_TERMINATION_BY_CODE` 同一词表），不是错误码：细粒度归因走
    限制码与终止原因，错误码只有固定那一套（本轮不扩列），恢复动作跟着错误码走。
    """
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
        "limitations": listing_limitation_codes(runtime.limitations),
        "data_as_of": runtime.data_as_of,
        "normalized_request": _normalized_request(runtime),
        "error": error})
    # 终止路径也要有请求身份：主层拿不到指纹就没办法判断"同一个问题问过没有"。
    # `final_status` 先写上：`_ensure_lineage` 拿它算终止原因。
    runtime.final_status = run_status
    _ensure_lineage(runtime)
    runtime.report = _report(runtime, status=report_status, run_status=run_status)


def _termination_reason(runtime: ListingRuntime, status: RunStatus) -> str:
    """为什么这次执行停在这里：只查已登记的码表，不自造说法，也不留空白。

    两遍扫：先找"还缺哪件证据"（快照缺失 / 来源未取证 / 过期 / 枚举无凭据），一个都没
    找到才落到业务发现（`mismatch` / `not_listed` / `not_on_sale` → `succeeded`）。
    顺序不能反：一份"三家有两家对不上、一家没读到快照"的结果，下一步该去补快照，
    而把业务发现排在前面会把这件事说成"已经做完了"。
    """
    if status is RunStatus.SUCCEEDED:
        return "succeeded"
    codes = [code for code in listing_limitation_codes(runtime.limitations)
             + list(runtime.state.limitations) if code in _TERMINATION_BY_CODE]
    findings = ("listing_price_mismatch_found", "listing_not_listed_found",
                "listing_not_on_sale_found")
    for code in codes:
        if code not in findings:
            return _TERMINATION_BY_CODE[code]
    for code in codes:
        if code in findings:
            return _TERMINATION_BY_CODE[code]
    error = runtime.state.error
    if error is not None:
        code = str(getattr(error.code, "value", error.code))
        return _TERMINATION_BY_CODE.get(code, "contract_violation")
    if status is RunStatus.MISSING_DATA:
        return "coverage_incomplete"
    return "recovery_exhausted"


# ---------------------------------------------------------------------------
# 节点 10：发布
# ---------------------------------------------------------------------------


def build_audit_catalog(runtime: ListingRuntime) -> None:
    """建一次目录：引用与展示名的唯一换面处，越权店铺在这里直接失败。

    roster 里的店即使一格都没判成也要进目录 —— `evaluated_scope` 要换引用，少了材料
    就会在投影里抛 `shop_not_authorized`，那不是授权问题，是投影材料不够。
    """
    rows = raw_audit_rows(runtime)
    seen = {str(row.get("shop_id")) for row in rows}
    for shop_id in runtime.candidate_shop_ids:
        if shop_id in seen:
            continue
        row: dict[str, Any] = {"shop_id": shop_id}
        if runtime.erp_product_id:
            row["product_id"] = runtime.erp_product_id
        rows.append(row)
    coverage = Coverage(status="complete", start=runtime.context.now.date(),
                        end=runtime.context.now.date(), gaps=[])
    source = ToolResult(status="ok", data=rows, filters={}, coverage=coverage)
    runtime.catalog = build_catalog(runtime.context.conn, source,
                                    allowed_shop_ids=runtime.context.allowed_shop_ids)


def persist_audit(runtime: ListingRuntime, store: Any) -> None:
    """先冻结本轮依据，再发差异表。

    顺序不是风格：`price_audit` 卡片一旦发布就不能再改，而它声称"依据已冻结"——依据
    还没落库就发卡片，那个声称就成了空话。两者存不下都是 failed（spec §7：必需结果
    保存失败不发布成功）。
    """
    if runtime.catalog is None or runtime.report is None:
        _stop(runtime, kind="unavailable", code="result_contract_violation",
              problems=["result_contract_violation"], stage=ListingNode.PERSIST_AUDIT,
              message="查询结果异常。")
        return
    try:
        _freeze_this_round_basis(runtime, store)
    except Exception:  # noqa: BLE001 - 依据冻不住就不发结论，也不带出数据库原文
        _stop(runtime, kind="unavailable", code="artifact_persistence_failed",
              problems=["artifact_persistence_failed"], stage=ListingNode.PERSIST_AUDIT,
              message="结果保存失败，请稍后重试。")
        return
    try:
        from .tool import project_audit

        payload = project_audit(runtime, runtime.catalog, runtime.report)
        ref = store.save_artifact(runtime.state.run_id, NewArtifact(
            artifact_type="price_audit", payload=payload,
            data_as_of=runtime.data_as_of, coverage=None))
    except Exception:  # noqa: BLE001 - 必需结果存不下不是降级，是失败
        runtime.published = []
        _stop(runtime, kind="unavailable", code="artifact_persistence_failed",
              problems=["artifact_persistence_failed"], stage=ListingNode.PERSIST_AUDIT,
              message="结果保存失败，请稍后重试。")
        return
    runtime.published = [(ref, payload)]
    runtime.state = runtime.state.model_copy(update={"artifact_refs": [ref]})


def _freeze_this_round_basis(runtime: ListingRuntime, store: Any) -> None:
    """把期望 roster 与本轮目标价冻结落库（只写不读）。

    写入走 Store：两张表的 `run_id` 外键指向 `bi.query_runs`，而运行记录只由 Store 写
    （与 `record_provenance` 同一做派）。`captured_from` 由 SQL 常量写成
    `current_user_input`，配合 018 的 CHECK，「继承上一轮」在这条路上写不出行。没给
    目标价的那些格**不落标准行**：空不等于零，缺标准也不等于任何一个可回填的默认值。
    """
    context = runtime.context
    request = runtime.request
    assert request is not None
    roster = [(item.shop_id, item.listing_ref, item.sku_ref or "")
              for item in runtime.roster]
    expectations = []
    for item in runtime.roster:
        amount = runtime.expectations.get(item.key)
        if amount is None:
            continue
        expectations.append((item.shop_id, item.sku_ref or "",
                             price_text(amount, request.currency) or amount,
                             _rule_for(runtime, item)))
    store.record_listing_audit_basis(
        runtime.state.run_id, subject_id=context.subject_id,
        fingerprint=_fingerprint(runtime), roster=roster, expectations=expectations,
        price_basis=request.price_basis, currency=request.currency)


def _rule_for(runtime: ListingRuntime, item: RosterItem) -> str:
    """这一格被哪一档规则命中：追溯时"按 SKU 给的"与"按统一价给的"不是一回事。"""
    request = runtime.request
    assert request is not None
    for rule in request.expected_prices:
        if rule.applies_to == "sku" and rule.sku_ref == item.sku_ref:
            return "sku"
        if rule.applies_to == "shop_sku" and rule.shop_ref == item.shop_ref \
                and rule.sku_ref == item.sku_ref:
            return "shop_sku"
    return "all_selected"


def finalize_run(runtime: ListingRuntime, store: Any) -> None:
    """收尾：终态由分类节点定下来，不在这里重新推断。"""
    status = runtime.final_status or _run_status(
        runtime.state.target_status or DomainStatus.FAILED)
    runtime.state = runtime.state.model_copy(update={
        "status": status, "revision": runtime.state.revision + 1})
    store.finish(runtime.state.run_id, RunCompletion(
        expected_revision=runtime.state.revision - 1,
        node=ListingNode.FINALIZE.value, status=status,
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


def _finish(runtime: ListingRuntime, store: Any) -> None:
    """提前收尾：终止原因与血缘必须跟终态一起落库。

    沿用业务查询图的 `_finish_early` 会少一个 `termination_reason`：“保存失败”与
    “没给目标价”在运行表上就会变成同一个空白，恢复层只能去查聊天文本。
    血缘同理：缺了指纹的提前终止是一条“查不出谁问过什么”的记录，而恢复与
    复用都按指纹判定。
    """
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


# ---------------------------------------------------------------------------
# 驱动
# ---------------------------------------------------------------------------

_PRE_NODES: tuple[tuple[ListingNode, Callable[[ListingRuntime], None]], ...] = (
    (ListingNode.RESOLVE_SCOPE_PRODUCT_AND_SKUS, resolve_scope_product_and_skus),
    (ListingNode.LOAD_EXPECTED_LISTING_ROSTER, load_expected_listing_roster),
    (ListingNode.CAPTURE_USER_EXPECTED_PRICES, capture_user_expected_prices),
)
_SNAPSHOT_NODES: tuple[tuple[ListingNode, Callable[[ListingRuntime], None]], ...] = (
    (ListingNode.CHECK_LISTING_SOURCE, check_listing_source),
    (ListingNode.LOAD_LISTING_SNAPSHOT, load_listing_snapshot),
    (ListingNode.VERIFY_COMPLETENESS_AND_FRESHNESS, verify_completeness_and_freshness),
    (ListingNode.JOIN_EXPECTED_AND_ACTUAL, join_expected_and_actual),
    (ListingNode.COMPARE_DECIMAL_PRICES, compare_decimal_prices),
    (ListingNode.CLASSIFY_DISCREPANCIES, classify_discrepancies),
)


@dataclass
class ListingExecution:
    """内部兼容结果：绝不直接持久化。"""

    domain_result: DomainResult
    report: ListingAuditReport | None = None
    session_filters: Mapping[str, object] = field(default_factory=dict)


def run_listing_audit_graph(*, request: ListingPriceAuditRequest | None,
                            context: DomainContext, tool_call_id: str,
                            arguments: Mapping[str, Any] | None = None,
                            arguments_error: str | None = None) -> ListingExecution:
    """执行一次上架复核：一次工具调用 = 一次图执行，内部节点不消耗额外模型回合。

    参数解析失败**也**要走图：`needs_input` 必须留下运行记录与终止原因，否则恢复策略
    只能去猜聊天文本（与经营图同一契约）。
    """
    if request is not None and not isinstance(request, ListingPriceAuditRequest):
        raise InvalidListingRequest()
    store = context.store
    run_id = store.create_run(NewQueryRun(
        chat_id=context.chat_id, user_message_id=context.user_message_id,
        subject_id=context.subject_id, tool_call_id=tool_call_id, domain=DOMAIN,
        attempt_no=context.attempt_no, normalized_request={},
        state={"node": ListingNode.RESOLVE_SCOPE_PRODUCT_AND_SKUS.value,
               "status": RunStatus.RUNNING.value, "revision": 0}))
    runtime = ListingRuntime(
        state=ListingState(run_id=run_id, normalized_request={}), context=context,
        tool_call_id=tool_call_id, request=request)
    try:
        return _run_nodes(runtime, store, arguments_error=arguments_error)
    except Exception:  # noqa: BLE001 - 收尾后原样上抛，由外层做脱敏
        _finish_run_as_failed(runtime, store)  # type: ignore[arg-type]
        raise


def _run_nodes(runtime: ListingRuntime, store: Any, *,
               arguments_error: str | None) -> ListingExecution:
    if arguments_error is not None or runtime.request is None:
        # 缺本轮目标价（以及任何入参问题）都在第一格终止：往下跑就需要一份"标准"，
        # 而那份标准除了用户本轮给的值之外没有别的合法来源。
        _stop(runtime, kind="needs_input", code="invalid_parameters",
              problems=["invalid_parameters"],
              stage=ListingNode.RESOLVE_SCOPE_PRODUCT_AND_SKUS,
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
    except _BudgetExhausted:
        _stop(runtime, kind="unavailable", code="deadline_exceeded", problems=[],
              stage=ListingNode.LOAD_LISTING_SNAPSHOT,
              message="本次查询时间预算已耗尽，请缩小日期或店铺范围后重试。",
              limitations=["本次查询时间预算已耗尽"])
    except psycopg.errors.QueryCanceled:
        _stop(runtime, kind="unavailable", code="query_timeout", problems=[],
              stage=ListingNode.LOAD_LISTING_SNAPSHOT,
              message="查询已超时，请稍后重试。", limitations=["查询超时"])
    except (psycopg.errors.UndefinedTable, psycopg.errors.InsufficientPrivilege):
        # 快照表还没随迁移建立，或应用身份没有读视图的权限：这是**部署缺件**，
        # 不是"这些店都没上架"。把整批复核说成 unavailable 才是诚实答案。
        _stop(runtime, kind="unavailable", code="unavailable", problems=[],
              stage=ListingNode.LOAD_LISTING_SNAPSHOT,
              message="查询暂不可用，请稍后重试。",
              limitations=[TEXT_SOURCE_UNVERIFIED])
    except (psycopg.OperationalError, psycopg.InterfaceError):
        # 连接类故障：不猜类型也不自动追加查询（预算与恢复决策归外层图）。
        _stop(runtime, kind="unavailable", code="unavailable", problems=[],
              stage=ListingNode.LOAD_LISTING_SNAPSHOT,
              message="查询暂不可用，请稍后重试。")
    except ValueError:
        # 判定不变量、快照形状或血缘值不合法：这就是"结果不符合安全契约"，必须
        # 按 `result_contract_violation` 关掉三个出口（一个数字都不发），而不是
        # 把异常抛到主层、让整回合连同已存的运行记录一起被吞掉。
        # 不带原文：那句 Python 文本不是给用户看的第二份说法。
        _stop(runtime, kind="unavailable", code="result_contract_violation",
              problems=["result_contract_violation"],
              stage=runtime.state.node, message="查询结果异常。")
    if not runtime.pending or runtime.pending[-1][0] is not runtime.state:
        runtime.pending.append((runtime.state, _event_payload(runtime)))  # type: ignore[arg-type]
    # 快照内不写库：退出只读事务后按节点原顺序补写状态。
    for captured, payload in runtime.pending:
        runtime.state = captured.model_copy(update={"revision": runtime.state.revision})
        _persist_transition(runtime, store, payload)  # type: ignore[arg-type]
    if runtime.state.status is not RunStatus.RUNNING:
        _finish(runtime, store)
        return _execution_result(runtime)

    try:
        build_audit_catalog(runtime)
    except Exception:  # noqa: BLE001 - 名称解析失败就不能把未核验的行发出去
        _stop(runtime, kind="unavailable", code="result_contract_violation",
              problems=["result_contract_violation"], stage=ListingNode.PERSIST_AUDIT,
              message="查询结果异常。")
        _persist_transition(runtime, store, _event_payload(runtime))  # type: ignore[arg-type]
        _finish(runtime, store)
        return _execution_result(runtime)

    assert runtime.provenance is not None and runtime.identity is not None
    store.record_provenance(runtime.state.run_id, provenance=runtime.provenance,
                            identity=runtime.identity)
    runtime.state = transition_state(runtime.state, ListingNode.PERSIST_AUDIT)
    persist_audit(runtime, store)
    _persist_transition(runtime, store, _event_payload(runtime))  # type: ignore[arg-type]
    if runtime.state.status is not RunStatus.RUNNING:
        _finish(runtime, store)
        return _execution_result(runtime)
    runtime.state = transition_state(runtime.state, ListingNode.FINALIZE)
    finalize_run(runtime, store)
    return _execution_result(runtime)


def _execution_result(runtime: ListingRuntime) -> ListingExecution:
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
    return ListingExecution(
        domain_result=DomainResult(
            run_id=state.run_id, status=status, model_payload=model,
            artifacts=artifacts, data_as_of=state.data_as_of, coverage=None,
            error=state.error, provenance=runtime.provenance, identity=runtime.identity),
        report=report if safe else None, session_filters={})


def _silent_payload(report: ListingAuditReport, *, safe: bool) -> dict[str, object]:
    """没有可发差异表时的模型载荷：只给状态与已登记的披露，金额一个都不给。

    授权失败连原因都不发：一句「这家店不在授权范围」本身就在证实那家店存在，而越权
    引用该得到的只是一个否。其余情态必须带披露：经营者要能分清「没解析出商品」
    「来源没取证」「没给目标价」是三个不同的答案，而不是三条一样的"没有数据"。
    """
    if not safe or report.status == "forbidden":
        return {"status": "failed"}
    payload: dict[str, object] = {"status": report.status}
    if report.limitations:
        payload["limitations"] = list(report.limitations)
    if report.candidates:
        payload["candidates"] = [dict(item) for item in report.candidates]
    return payload
