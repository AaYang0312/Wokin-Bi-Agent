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
from decimal import Decimal
from typing import Iterable, Literal, Sequence
from zoneinfo import ZoneInfo

BEIJING = ZoneInfo("Asia/Shanghai")

# 指标→实体依赖定义在 sources 注册表（来源与能力的唯一真源）；本模块继续向外转发，
# 不养第二份，否则覆盖门禁与指标层会各自演化出口径。
from bi_agent.sources import (
    ENTITY_REQUIREMENTS, ShopRecord, resolve_order_source)

# 同一覆盖来源名：同步状态按数据来源记录。
# 注意：`orders` 在这里仍是单源常量，只反映交易通道。淘系出库通道的逐店来源解析已经
# 由 `sources.resolve_order_source` 提供，覆盖按 `(source, entity, time_basis)` 取交集
# 是计划 Task 5.2 的范围；在那之前不得拿本常量当平台可用性证据。
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

# 平台成功退款找不到原单：退款归属未确认，不能当已核验数据出数。
# 定义在本模块，指标层引用同一份，避免两边口径漂移。
UNMATCHED_REFUNDS_SQL = """
SELECT count(*) FROM reporting.v_refunds
WHERE shop_id = ANY(%s) AND platform_success AND refund_canonical
  AND platform_completed_at >= %s AND platform_completed_at < %s
  AND (commercial_id IS NULL OR NOT matched)
"""

# 核验口径版本：规则一变，旧的 passed 自动失效（降级为 unknown）。
QUALITY_RULE = "kuaimai-reconcile/1"

_CAPABILITIES_SQL = """
SELECT shop_id, platform FROM reporting.v_shops WHERE shop_id = ANY(%s)
"""

QualityStatus = Literal["unknown", "passed", "failed"]
CoverageStatus = Literal["complete", "partial", "missing"]
Window = tuple[str, str]
Span = tuple[date, date]


@dataclass(frozen=True)
class CoverageGap:
    """结构化缺口：归因到实体与店铺，供恢复策略决定该怎么回答。

    shop_id 是 ERP 主键，只能留在服务端对象里；对外投影依旧走 coverage
    的日期串缺口，不能让缺口反而成为名称/主键的泄露面。
    """

    entity: str
    shop_id: str
    start: date
    end: date

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
    status: QualityStatus = "failed" if unmatched else "passed"
    conn.execute(
        "UPDATE bi.sync_state SET quality_status=%s, quality_checked_at=now(), "
        "quality_rule=%s, quality_reason=%s "
        "WHERE source=%s AND entity=%s AND shop_id=%s",
        (status, QUALITY_RULE, "unmatched_success_refunds" if unmatched else None,
         source, entity, shop_id),
    )
    return status


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
    rows = conn.execute(_CAPABILITIES_SQL, (list(shop_ids),)).fetchall()
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
    missing_spans: list[Span] = []
    gaps: list[CoverageGap] = []
    cutoffs: list[datetime] = []
    qualities: list[QualityStatus] = []
    batches: list[str] = []
    pairs = 0

    entities = required_entities(request.metrics)
    unconfigured = _unconfigured_shops(conn, shop_ids, entities)

    for entity in entities:
        source = ENTITY_SOURCES[entity]
        # 一个实体一次查完：区间运算留在 SQL 里，不按店铺逐条往返。
        states = {str(row[0]): row for row in conn.execute(
            _COVERAGE_SQL, (start_ts, end_ts, start_ts, end_ts,
                            source, entity, shop_ids)).fetchall()}
        batches.extend(str(row[0]) for row in conn.execute(
            _BATCHES_SQL, (source, entity, shop_ids, end_ts, start_ts)).fetchall())

        for shop_id in shop_ids:
            pairs += 1
            row = states.get(shop_id)
            if row is None:
                # 没有同步状态行就是从未取过数：未知，不是“确实没有交易”。
                qualities.append("unknown")
                gaps.append(CoverageGap(entity=entity, shop_id=shop_id,
                                        start=start, end=end))
                missing_spans.append((start, end))
                continue
            qualities.append(_effective_quality(row[4], row[5]))
            if row[3] is not None:
                cutoffs.append(row[3])
            covered_spans.extend(_spans(row[1]))
            missing = _spans(row[2])
            if missing:
                missing_spans.extend(missing)
                gaps.extend(CoverageGap(entity=entity, shop_id=shop_id,
                                        start=gap[0], end=gap[1]) for gap in missing)

    if "failed" in qualities:
        quality_status: QualityStatus = "failed"
    elif qualities and all(item == "passed" for item in qualities):
        quality_status = "passed"
    else:
        quality_status = "unknown"

    covered_windows = _windows(covered_spans)
    missing_windows = _windows(missing_spans)
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
        data_as_of=min(cutoffs) if len(cutoffs) == pairs else None,
        quality_status=quality_status,
        source_batches=tuple(sorted(set(batches))),
        gaps=tuple(gaps),
        source_unconfigured=unconfigured,
        suggested_window=_suggested(covered_windows, requested, status),
    )
