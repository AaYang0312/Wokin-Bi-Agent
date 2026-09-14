"""数据就绪判定：覆盖、业务截止、质量三件事分开取证。

口径来自 docs/superpowers/plans/2026-09-11-data-and-query-closure.md Task 1：

- `covered` 只说明哪段业务时间已完整入库，不说明数据对不对。
- `data_as_of` 只说明已完整处理的源数据截止时刻，禁止用 `last_success_at` 顶替。
- `quality_status` 只说明来源是否按版本化口径核验过；`unknown` 可出数但必须披露，
  `failed` 一律禁止出数。历史遗留的“没核验过”是 unknown，不是 failed。

本模块不 import `bi_agent.metrics`：查询请求按属性读取，指标层反过来依赖这里，
避免两个模块互相引用后各自演化出口径。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Iterable, Literal, Sequence
from zoneinfo import ZoneInfo

BEIJING = ZoneInfo("Asia/Shanghai")

# 指标→实体依赖定义在 sources 注册表（来源与能力的唯一真源）；本模块继续向外转发，
# 不养第二份，否则覆盖门禁与指标层会各自演化出口径。
from bi_agent.sources import (
    ENTITY_REQUIREMENTS, ORDERS_ENTITY, PAYMENT_WINDOW_METRICS, PAY_TIME_METRICS,
    ShopRecord, SourceBinding, resolve_metric_dependencies, resolve_order_source)

# 默认来源名：只给 `reconcile_source_quality` 在调用方没显式传 source 时兜底。
# 覆盖判定不再读这份常量——Task 5.2b 起按 `sources.resolve_metric_dependencies`
# 逐店逐实体解析真实来源再取交集。拿它当“某平台可用”的证据就是回到旧的单源假设。
ENTITY_SOURCES: dict[str, str] = {
    "orders": "erp.trade.list.query",
    "aftersales_occurrence": "erp.aftersale.list.query",
    "aftersales_cohort": "erp.aftersale.list.query",
}

_COVERAGE_SQL = """
SELECT shop_id,
       tstzmultirange(tstzrange(%s, %s, '[)')) * covered AS covered_part,
       tstzmultirange(tstzrange(%s, %s, '[)')) - covered AS missing,
       data_as_of, quality_status, quality_rule
FROM reporting.v_coverage
WHERE source=%s AND entity=%s AND shop_id = ANY(%s)
"""

_BATCHES_SQL = """
SELECT DISTINCT batch_id FROM reporting.v_source_batches
WHERE source=%s AND entity=%s AND shop_id = ANY(%s)
  AND window_kind = 'business' AND window_start < %s AND window_end > %s
"""

# 退款归属诊断：分母固定为同一窗口内 canonical 平台成功退款，失败/待处理/重复工单
# 排除在外。未匹配是**归属限制**，不是数据有错（计划 5.3a/5.3b），所以它只披露不拒答。
UNMATCHED_REFUNDS_SQL = """
SELECT count(*) FILTER (WHERE commercial_id IS NULL OR NOT matched),
       count(*),
       coalesce(sum(raw_platform_amount) FILTER (
           WHERE commercial_id IS NULL OR NOT matched), 0)
FROM reporting.v_refunds
WHERE shop_id = ANY(%s) AND platform_success AND refund_canonical
  AND platform_completed_at >= %s AND platform_completed_at < %s
"""

# 未认证支付诊断：verified=false 的行不能无声消失。金额未定与「确实为 0」必须分开，
# 已知金额只对有原始金额的行求和，笔数一起给出，避免把 0 读成“这些单值 0 元”。
UNVERIFIED_PAYMENTS_SQL = """
SELECT count(*),
       count(*) FILTER (WHERE amount IS NULL),
       count(*) FILTER (WHERE amount IS NOT NULL),
       coalesce(sum(amount) FILTER (WHERE amount IS NOT NULL), 0)
FROM reporting.v_payments
WHERE shop_id = ANY(%s) AND NOT verified
  AND paid_at >= %s AND paid_at < %s
"""

# 核验口径版本：规则一变，旧的 passed 自动失效（降级为 unknown）。
# /2：未匹配退款从「整店 failed」改成「可量化限制 + 逐结果披露」（计划 5.3d）。
# 升级后必须按运行手册重跑 `reconcile` 再 `capabilities --apply`，否则既有 passed
# 全部视同未核验，能力会被回收——这是刻意的：口径变了，旧凭证不能继续给新口径背书。
QUALITY_RULE = "kuaimai-reconcile/2"

# 授权范围内各店的平台档案。名字上是「平台」而不是「能力」：本查询只回答「这个平台登记了
# 什么来源与口径」，逐指标能力仍由 `metrics._capability_gap` 判。主层问「销售额能用哪些口径」
# 时读的也是这一份，所以常量公开并只有一处拼写（`agent.sales_basis_clarification`）。
SHOP_PLATFORMS_SQL = """
SELECT shop_id, platform FROM reporting.v_shops WHERE shop_id = ANY(%s)
"""

QualityStatus = Literal["unknown", "passed", "failed"]
CoverageStatus = Literal["complete", "partial", "missing"]
Window = tuple[str, str]
Span = tuple[date, date]


@dataclass(frozen=True)
class CoverageGap:
    """结构化缺口：归因到实体、店铺与**具体来源**，供恢复策略决定该怎么回答。

    shop_id 是 ERP 主键，source 是接口方法名，两者都只能留在服务端对象里；
    对外投影依旧只走 coverage 的日期串缺口，不能让缺口反而成为名称/主键的泄露面。
    """

    entity: str
    shop_id: str
    start: date
    end: date
    source: str = ""

    @property
    def window(self) -> Window:
        return (self.start.isoformat(), self.end.isoformat())


@dataclass(frozen=True)
class CoverageAssessment:
    """一次请求的就绪结论。原请求窗口在此冻结，建议窗口只是建议。"""

    status: CoverageStatus
    requested_window: Window
    covered_windows: tuple[Window, ...]
    missing_windows: tuple[Window, ...]
    data_as_of: datetime | None
    quality_status: QualityStatus
    source_batches: tuple[str, ...]
    gaps: tuple[CoverageGap, ...]
    suggested_window: Window | None
    # 来源尚未开通（能力未登记）的店铺：只能留在服务端，不得迚入模型载荷。
    source_unconfigured: tuple[str, ...] = ()
    # 业务时间口径认证：`blocking` 里的店不得给支付窗口类结果（实测不成立），
    # `disclosure` 里的店可以出数但必须披露为可观测样本。两者都只留在服务端。
    time_basis_blocking: tuple[str, ...] = ()
    time_basis_disclosure: tuple[str, ...] = ()


def _effective_quality(status: object, rule: object) -> QualityStatus:
    """passed 只在口径版本仍旧时成立；旧版本的对账结果不能自动沿用。"""
    if str(status) == "passed" and str(rule or "") != QUALITY_RULE:
        return "unknown"
    return str(status) if str(status) in ("unknown", "passed", "failed") else "unknown"


def reconcile_source_quality(conn, *, shop_id: str, entity: str,
                             start: datetime, end: datetime,
                             source: str | None = None) -> QualityStatus:
    """跑一次可审计的核验，并回写质量状态；返回新的状态。

    “同步成功”本身不是核验：只有本窗口内确实落了 reconcile 批次凭证，
    才有资格改质量状态。没凭证就维持 unknown，既不谎称已核验，
    也不凭空降级成 failed。
    """
    source = source or ENTITY_SOURCES[entity]
    evidence = conn.execute(
        "SELECT count(*) FROM reporting.v_source_batches "
        "WHERE source=%s AND entity=%s AND shop_id=%s "
        "AND window_kind = 'business' AND window_start < %s AND window_end > %s",
        (source, entity, shop_id, end, start),
    ).fetchone()[0]
    if not evidence:
        return "unknown"

    unmatched = conn.execute(UNMATCHED_REFUNDS_SQL, ([shop_id], start, end)).fetchone()[0]
    # 未匹配退款只记录为归属原因，不再独自把来源打成 failed：换成另一道门禁等于
    # 换个理由继续拒答（设计 §5）。金额冲突、覆盖损坏等真实失败仍在各自路径上拦。
    conn.execute(
        "UPDATE bi.sync_state SET quality_status=%s, quality_checked_at=now(), "
        "quality_rule=%s, quality_reason=%s "
        "WHERE source=%s AND entity=%s AND shop_id=%s",
        ("passed", QUALITY_RULE,
         "unmatched_success_refunds" if unmatched else None, source, entity, shop_id),
    )
    return "passed"


_ATTRIBUTION_SQL = """
WITH pay AS (
  SELECT p.commercial_id, p.amount
  FROM reporting.v_payments p
  WHERE p.shop_id = ANY(%s) AND p.verified AND p.amount IS NOT NULL
    AND p.paid_at >= %s AND p.paid_at < %s
), ln AS (
  -- eligible 与 reporting.v_product_daily 的纳入条件同集合，否则披露会与数字自相矛盾。
  SELECT a.commercial_id,
         coalesce(sum(a.eligible_amount) FILTER (
             WHERE a.paid_at >= %s AND a.paid_at < %s), 0) AS eligible,
         coalesce(sum(a.closed_amount), 0) AS closed,
         coalesce(sum(a.gift_amount), 0) AS gift,
         coalesce(sum(a.no_product_amount), 0) AS no_product
  FROM reporting.v_payment_attribution a
  WHERE a.shop_id = ANY(%s)
    AND a.commercial_id IN (SELECT commercial_id FROM pay)
  GROUP BY a.commercial_id
), per AS (
  -- 只取正差额：行合计大于支付额（分摊溢出方向）不算未归属收入。
  SELECT greatest(pay.amount - coalesce(ln.eligible, 0), 0) AS residual,
         coalesce(ln.closed, 0) AS closed, coalesce(ln.gift, 0) AS gift,
         coalesce(ln.no_product, 0) AS no_product
  FROM pay LEFT JOIN ln ON ln.commercial_id = pay.commercial_id
), bucketed AS (
  -- 四类互斥，按 residual 逐项扣减归因；扣不掉的进 other，绝不凭空造成因。
  SELECT residual,
         least(residual, closed) AS b_closed,
         least(residual - least(residual, closed), gift) AS b_gift,
         least(residual - least(residual, closed)
                       - least(residual - least(residual, closed), gift),
               no_product) AS b_no_product
  FROM per
)
SELECT coalesce(sum(residual), 0), coalesce(sum(b_closed), 0),
       coalesce(sum(b_gift), 0), coalesce(sum(b_no_product), 0),
       coalesce(sum(residual - b_closed - b_gift - b_no_product), 0)
FROM bucketed
"""


@dataclass(frozen=True)
class AttributionGap:
    """已核验支付额里没能进商品维度的部分及其成因分解。"""

    total: Decimal
    closed: Decimal
    gift: Decimal
    no_product: Decimal
    other: Decimal

    @property
    def material(self) -> bool:
        return self.total > 0


def money_text(value: Decimal) -> str:
    """对外披露用的金额文本：原值去尾零，不舍入，分项与合计才对得上。"""
    return _money(value)


def _money(value: Decimal) -> str:
    """金额按原值渲染，只去尾零：不做舍入，免得分项与合计对不上。"""
    text = format(value, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def attribution_gap(conn, *, shop_ids: Sequence[str],
                    start_ts: datetime, end_ts: datetime) -> AttributionGap:
    """按请求窗口与店铺范围算支付额的商品归属差额。"""
    shops = [str(shop_id) for shop_id in shop_ids]
    if not shops:
        return AttributionGap(Decimal(0), Decimal(0), Decimal(0), Decimal(0), Decimal(0))
    row = conn.execute(_ATTRIBUTION_SQL,
                       (shops, start_ts, end_ts, start_ts, end_ts, shops)).fetchone()
    return AttributionGap(*(Decimal(str(value or 0)) for value in row))


def describe_attribution_gap(gap: AttributionGap) -> str | None:
    """把差额连同成因写成一句披露；没有差额就返回 None（不给正常查询添噪声）。

    四个分项必须全部出现：实测模型在拿不到归因时会自己编原因
    （把关闭行造成的差额说成“赠品/非父项行”），显式给 0 才能排除错猜。
    """
    if not gap.material:
        return None
    return (f"支付额中{_money(gap.total)}元未计入商品维度"
            f"（关闭订单行{_money(gap.closed)}元；赠品行{_money(gap.gift)}元；"
            f"无商品归属{_money(gap.no_product)}元；其他{_money(gap.other)}元）")


_SWITCHED_SOURCES_SQL = """
SELECT DISTINCT shop_id, entity, source FROM reporting.v_coverage
WHERE entity = ANY(%s) AND shop_id = ANY(%s)
  AND covered && tstzmultirange(tstzrange(%s, %s, '[)'))
"""


def switched_sources_between(conn, *, shop_ids: Sequence[str], entities: Sequence[str],
                             current: set[tuple[str, str, str]],
                             start_ts: datetime, end_ts: datetime) -> tuple[str, ...]:
    """找出“上期由另一个来源覆盖”的店铺：这是换来源，不是增长或下滑。

    两期差额只有在同来源、同口径、同时间归属时才有经营含义。上期窗口被一条**本次没在用**
    的来源覆盖，说明这家店换过通道——把它解释成增长率会把口径变化说成业务变化。
    """
    if not shop_ids or not entities:
        return ()
    # 参数顺序与 SQL 一致：先 entity 再 shop_id，写反了查不到行就会静默放过换来源。
    rows = conn.execute(_SWITCHED_SOURCES_SQL,
                        (list(entities), [str(s) for s in shop_ids],
                         start_ts, end_ts)).fetchall()
    used_keys = {(shop_id, entity) for shop_id, entity, _source in current}
    switched: set[str] = set()
    for shop_id, entity, source in rows:
        key = (str(shop_id), str(entity))
        if key not in used_keys:
            continue
        if (str(shop_id), str(entity), str(source)) not in current:
            switched.add(str(shop_id))
    return tuple(sorted(switched))


@dataclass(frozen=True)
class RefundAttribution:
    """退款归属限制的量化形状：0/0 时比例是未知，不是 0%。"""

    unmatched: int
    total: int
    unmatched_amount: Decimal

    @property
    def ratio_text(self) -> str:
        """比例只在有条目可分时存在：0/0 是未知，不是 0%。

        按**条数**算，不按金额算，也不拿各店比例求平均（设计 §5 冻结的口径）。
        """
        if self.total <= 0:
            return "未知"
        ratio = (Decimal(self.unmatched) * 100 / Decimal(self.total)).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP)
        text = format(ratio, "f").rstrip("0").rstrip(".")
        return f"{text}%"

    @property
    def material(self) -> bool:
        return self.unmatched > 0


def refund_attribution_gap(conn, *, shop_ids: Sequence[str],
                           start_ts: datetime, end_ts: datetime) -> RefundAttribution:
    """这批退款里有多少找不到原单：条数、分母、金额与比例一起给。"""
    shops = [str(shop_id) for shop_id in shop_ids]
    if not shops:
        return RefundAttribution(0, 0, Decimal(0))
    unmatched, total, amount = conn.execute(
        UNMATCHED_REFUNDS_SQL, (shops, start_ts, end_ts)).fetchone()
    return RefundAttribution(int(unmatched or 0), int(total or 0),
                             Decimal(str(amount or 0)))


@dataclass(frozen=True)
class UnverifiedPayments:
    """拿不到核验章的支付事实：数量、金额未定的笔数与已知金额分开披露。"""

    total: int
    amount_undetermined: int
    amount_known: int
    known_amount: Decimal

    @property
    def material(self) -> bool:
        return self.total > 0


def unverified_payments(conn, *, shop_ids: Sequence[str],
                        start_ts: datetime, end_ts: datetime) -> UnverifiedPayments:
    shops = [str(shop_id) for shop_id in shop_ids]
    if not shops:
        return UnverifiedPayments(0, 0, 0, Decimal(0))
    total, undetermined, known, amount = conn.execute(
        UNVERIFIED_PAYMENTS_SQL, (shops, start_ts, end_ts)).fetchone()
    return UnverifiedPayments(int(total or 0), int(undetermined or 0), int(known or 0),
                              Decimal(str(amount or 0)))


def required_entities(metrics: Sequence[str]) -> list[str]:
    """这次查询到底依赖哪些业务实体。"""
    return sorted({entity for metric in metrics for entity in ENTITY_REQUIREMENTS[metric]})


def _spans(value) -> list[Span]:
    """把 SQL 算好的 multirange 读成北京时间下的日期区间对。

    必须显式换回北京时区再取日期：数据库会话默认 UTC，直接 .date()
    会把其峰8点的边界算成前一天，整段缺口都会偏早一天。
    """
    spans: list[Span] = []
    for rng in value or []:
        if rng.lower is None or rng.upper is None:
            continue
        spans.append((rng.lower.astimezone(BEIJING).date(),
                      rng.upper.astimezone(BEIJING).date()))
    return spans


def _windows(spans: Iterable[Span]) -> tuple[Window, ...]:
    return tuple(sorted({(span_start.isoformat(), span_end.isoformat())
                         for span_start, span_end in spans}))


def intersect_spans(left: Sequence[Span], right: Sequence[Span]) -> list[Span]:
    """两个 [start,end) 区间集合的交集（输入各自不重叠，输出按起点排序）。

    公共覆盖必须靠交集算：并集会把“甲店有这两天、乙店有那两天”拼成一个谁都不
    完整的“建议窗口”，拿着它再查一次仍然缺数。
    """
    out: list[Span] = []
    for a_start, a_end in left:
        for b_start, b_end in right:
            start = max(a_start, b_start)
            end = min(a_end, b_end)
            if start < end:
                out.append((start, end))
    return sorted(out)


def subtract_spans(whole: Sequence[Span], parts: Sequence[Span]) -> list[Span]:
    """从区间集合里去掉另一组区间：请求窗口减公共覆盖就是真正的缺口。"""
    out: list[Span] = list(whole)
    for part_start, part_end in parts:
        kept: list[Span] = []
        for span_start, span_end in out:
            if part_end <= span_start or part_start >= span_end:
                kept.append((span_start, span_end))
                continue
            if span_start < part_start:
                kept.append((span_start, part_start))
            if part_end < span_end:
                kept.append((part_end, span_end))
        out = kept
    return sorted(out)


def _suggested(covered_windows: tuple[Window, ...], requested: Window,
               status: CoverageStatus) -> Window | None:
    """请求内最大的连续已覆盖段；只返回来当建议，调用方不得回写窗口。"""
    if status == "complete":
        return requested
    if not covered_windows:
        return None
    return max(covered_windows,
               key=lambda item: (date.fromisoformat(item[1])
                                 - date.fromisoformat(item[0]), item[0]))


def _unconfigured_shops(conn, shop_ids: Sequence[str], entities: Sequence[str]) -> tuple[str, ...]:
    """本次请求需要的实体里，哪家店还根本没有已登记的取数来源。

    口径与 Task 5.1 能力门禁分开：本函数只回答「这个平台有没有登记来源」。
    「有来源但该指标未授予能力」由 `metrics._capability_gap` 报
    `capability_unavailable`，两者不得混成一个原因。旧的 `orders` 一类实体标签
    不再参与判断：它们只表示采集过实体，不表示任何指标可用（设计 §4）。

    `entities` 保留在签名里：只查订单的查询与查退款的查询以后可以各自解析依赖。
    """
    if not shop_ids or not entities:
        return ()
    rows = conn.execute(SHOP_PLATFORMS_SQL, (list(shop_ids),)).fetchall()
    unconfigured: list[str] = []
    for shop_id, platform in rows:
        record = ShopRecord.from_row(str(shop_id), platform, ())
        if resolve_order_source(record) is None:
            unconfigured.append(str(shop_id))
    return tuple(sorted(unconfigured))


def assess_query_coverage(conn, request) -> CoverageAssessment:
    """在跑指标 SQL 之前判定：能不能出数、缺哪一段、来源是否已核验。

    只读 reporting 视图，所以聊天 API 的 bi_app 身份也能调用。
    """
    start, end = request.start, request.end
    start_ts = datetime(start.year, start.month, start.day, tzinfo=BEIJING)
    end_ts = datetime(end.year, end.month, end.day, tzinfo=BEIJING)
    requested: Window = (start.isoformat(), end.isoformat())
    shop_ids = sorted({str(shop_id) for shop_id in request.shop_ids})

    covered_spans: list[Span] = []
    gaps: list[CoverageGap] = []
    cutoffs: list[datetime] = []
    qualities: list[QualityStatus] = []
    batches: list[str] = []

    entities = required_entities(request.metrics)
    unconfigured = _unconfigured_shops(conn, shop_ids, entities)
    profiles = {str(row[0]): ShopRecord.from_row(row[0], row[1], ())
                for row in conn.execute(SHOP_PLATFORMS_SQL, (shop_ids,)).fetchall()}

    # 逐店逐指标解析依赖，再按 (source, entity) 分组一次查完：既不再拿单源常量
    # 当全部平台的来源，也不按店铺逐条往返。同一条依赖去重只查一次。
    dependencies: dict[tuple[str, str, str], SourceBinding] = {}
    resolved_dependencies: list[tuple[str, SourceBinding]] = []
    unresolvable = False
    for metric in request.metrics:
        for shop_id in shop_ids:
            record = profiles.get(shop_id)
            bindings = () if record is None else resolve_metric_dependencies(record,
                                                                            str(metric))
            if not bindings:
                # 这个平台的来源拿不到该口径（或根本没有店铺档案）：整段窗口当未知，
                # 绝不能因为“没有依赖”而拼出一个“完整覆盖”。
                unresolvable = True
                qualities.append("unknown")
                gaps.append(CoverageGap(entity=metric, shop_id=shop_id,
                                        start=start, end=end))
                continue
            for binding in bindings:
                dependencies[(binding.shop_id, binding.source, binding.entity)] = binding
                resolved_dependencies.append((str(metric), binding))

    groups: dict[tuple[str, str], list[str]] = {}
    for shop_id, source, entity in dependencies:
        groups.setdefault((source, entity), []).append(shop_id)

    states: dict[tuple[str, str, str], object] = {}
    for (source, entity), group_shops in sorted(groups.items()):
        group_shops = sorted(set(group_shops))
        for row in conn.execute(_COVERAGE_SQL, (start_ts, end_ts, start_ts, end_ts,
                                                source, entity, group_shops)).fetchall():
            states[(source, entity, str(row[0]))] = row
        # 批次血缘只收本次实际用到的来源：旧通道残留的批次不能混进结果。
        batches.extend(str(row[0]) for row in conn.execute(
            _BATCHES_SQL, (source, entity, group_shops, end_ts, start_ts)).fetchall())

    # 时间口径核对（设计 §4）：先于覆盖读取判定，因为“实测不成立”与“没测过”后果不同。
    # 只有按支付时间归属的指标才受这条约束；退款发生额按平台完成时间归属。
    blocking: set[str] = set()
    disclosure: set[str] = set()
    pay_time_shops = {binding.shop_id for _metric, binding in resolved_dependencies
                      if _metric in PAY_TIME_METRICS and binding.entity == ORDERS_ENTITY}
    window_shops = {binding.shop_id for _metric, binding in resolved_dependencies
                    if _metric in PAYMENT_WINDOW_METRICS
                    and binding.entity == ORDERS_ENTITY}
    for (shop_id, source, entity), binding in dependencies.items():
        if shop_id not in pay_time_shops or entity != ORDERS_ENTITY:
            continue
        certification = binding.time_certification
        if certification == "certified":
            continue
        if certification == "disproved" and shop_id in window_shops:
            blocking.add(shop_id)
        else:
            disclosure.add(shop_id)

    # 公共可覆盖范围 = 请求范围 ∩ 每一个必需的 (店铺 × 来源 × 实体) 区间。
    common: list[Span] = [(start, end)]
    for (shop_id, source, entity), _binding in sorted(dependencies.items()):
        row = states.get((source, entity, shop_id))
        if row is None:
            # 没有同步状态行就是从未取过数：未知，不是“确实没有交易”。
            qualities.append("unknown")
            gaps.append(CoverageGap(entity=entity, shop_id=shop_id,
                                    start=start, end=end, source=source))
            common = []
            continue
        qualities.append(_effective_quality(row[4], row[5]))
        if row[3] is not None:
            cutoffs.append(row[3])
        clipped = _spans(row[1])
        common = intersect_spans(common, clipped)
        gaps.extend(
            CoverageGap(entity=entity, shop_id=shop_id, start=gap[0], end=gap[1],
                        source=source)
            for gap in subtract_spans([(start, end)], clipped))

    if unresolvable:
        common = []

    if "failed" in qualities:
        quality_status: QualityStatus = "failed"
    elif qualities and all(item == "passed" for item in qualities):
        quality_status = "passed"
    else:
        quality_status = "unknown"

    covered_windows = _windows(common)
    missing_windows = _windows(subtract_spans([(start, end)], common))
    if not missing_windows:
        status: CoverageStatus = "complete"
    elif covered_windows:
        status = "partial"
    else:
        status = "missing"

    return CoverageAssessment(
        status=status,
        requested_window=requested,
        covered_windows=covered_windows,
        missing_windows=missing_windows,
        # 共同截止：任一依赖项没有推进 data_as_of，整体截止就是未知。
        data_as_of=(min(cutoffs)
                    if cutoffs and len(cutoffs) == len(dependencies) and not unresolvable
                    else None),
        quality_status=quality_status,
        source_batches=tuple(sorted(set(batches))),
        gaps=tuple(gaps),
        source_unconfigured=unconfigured,
        time_basis_blocking=tuple(sorted(blocking)),
        time_basis_disclosure=tuple(sorted(disclosure - blocking)),
        suggested_window=_suggested(covered_windows, requested, status),
    )
