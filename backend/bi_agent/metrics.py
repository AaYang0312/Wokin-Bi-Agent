"""查询模型、结果模型、覆盖校验、固定SQL及指标口径。

没有自由SQL：指标、维度和比较映射到服务端固定模板；
权限店铺来自部署配置；SQL超时与行数受限。
"""

from __future__ import annotations

import time
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Literal, NamedTuple

import psycopg
from pydantic import BaseModel, ConfigDict, Field, model_validator
from zoneinfo import ZoneInfo

from bi_agent.catalog import pick_sku_label

BEIJING = ZoneInfo("Asia/Shanghai")
MAX_SPAN_DAYS = 366
MAX_ROWS = 500
PRODUCT_METRICS = {"quantity", "product_paid_amount"}
PERIOD_METRICS = {
    "paid_amount", "paid_orders", "erp_documents", "aov",
    "refund_amount", "cash_difference", "cohort_refund_rate",
}

Metric = Literal["paid_amount", "paid_orders", "erp_documents", "aov",
                 "refund_amount", "cash_difference", "cohort_refund_rate",
                 "quantity", "product_paid_amount"]

METRIC_DEFINITIONS: dict[str, str] = {
    "paid_amount": "已验证商业订单支付金额之和（人民币，按支付时间归属，[start,end)）",
    "paid_orders": "已验证商业订单数（一行对应一次支付事实）",
    "erp_documents": "ERP单据数（拆合单粒度，仅作对账参考，不作客单价分母）",
    "aov": "客单价=支付金额/商业订单数（总口径，不平均每日客单价）",
    "refund_amount": "平台退款成功发生额（按平台完成时间归属；系统实退口径未发布）",
    "cash_difference": "期间收支差额=支付金额-期间退款发生额（不是净利润，也不是同批净收入）",
    "cohort_refund_rate": "同批退款率=[start,end)支付商业单在明确截止时刻前的累计退款/同批支付额",
    "quantity": "有效非赠品父项数量（含套件/组合/加工，按line_kind标注）",
    "product_paid_amount": "已核验的非赠品父项行级分摊支付金额（按line_kind标注）",
}

# 指标→实体依赖与覆盖来源定义在 sources 注册表（唯一真源）；data_quality 向外转发，
# 本模块只引用，不再存第二份，避免门禁与指标两边口径漂移。
from bi_agent.data_quality import (
    ENTITY_REQUIREMENTS, UNMATCHED_REFUNDS_SQL, attribution_gap,
    assess_query_coverage, describe_attribution_gap)
from bi_agent.sources import ShopRecord, unsupported_reason


class QueryRequest(BaseModel):
    """唯一查询入口；keyword-only服务端参数不在此模型内。"""

    model_config = ConfigDict(extra="forbid")
    start: date
    end: date                  # 排他，不使用用户口语的包含结束日
    shop_ids: list[str] = Field(min_length=1)
    metrics: list[Metric] = Field(min_length=1)
    group_by: Literal["total", "day", "shop", "product"] = "total"
    compare: Literal["none", "previous_period"] = "none"
    top_n: int = Field(default=10, ge=1, le=500)
    currency: Literal["CNY"] = "CNY"

    @model_validator(mode="after")
    def _check_bounds(self) -> "QueryRequest":
        if self.end <= self.start:
            raise ValueError("end必须晚于start（排他区间）")
        if (self.end - self.start).days > MAX_SPAN_DAYS:
            raise ValueError(f"日期跨度最多{MAX_SPAN_DAYS}天")
        if self.group_by == "product":
            bad = sorted({m for m in self.metrics if m not in PRODUCT_METRICS})
            if bad:
                raise ValueError(f"商品分组只支持 {PRODUCT_METRICS}，不支持 {bad}")
        else:
            bad = sorted({m for m in self.metrics if m in PRODUCT_METRICS})
            if bad:
                raise ValueError(f"商品指标只能用product分组，不支持 {bad}")
        if "cohort_refund_rate" in self.metrics and self.group_by not in ("total", "shop"):
            raise ValueError("同批退款率只支持total/shop分组")
        return self


class Coverage(BaseModel):
    status: Literal["complete", "partial", "missing"]
    start: date | None
    end: date | None
    gaps: list[str] = Field(default_factory=list)
    # 建议窗口只是建议：原请求窗口始终按 start/end 原样返回，不被悄悄裁剪。
    suggested_window: tuple[str, str] | None = None


class ToolResult(BaseModel):
    status: Literal["ok", "missing_data", "invalid_parameters", "forbidden",
                    "unavailable"]
    data: list[dict[str, str | int | None]] = Field(default_factory=list)
    metric_definition: dict[str, str] = Field(default_factory=dict)
    filters: dict[str, object] = Field(default_factory=dict)
    data_as_of: datetime | None = None
    coverage: Coverage
    limitations: list[str] = Field(default_factory=list)
    # 结果依赖了哪几批同步：血缘由门禁一次算出，运行层直接引用。
    source_batches: tuple[str, ...] = ()


# ---------------------------------------------------------------------------
# 日期词解析：只处理有限常见词及明确日期，不能理解时返回None
# ---------------------------------------------------------------------------


def resolve_period(text: str, *, now: datetime) -> tuple[date, date] | None:
    """返回 [start, end) 的排他日期区间；无法理解返回None交给澄清。"""
    import re

    text = text.strip()
    today = now.astimezone(BEIJING).date()

    match = re.search(r"最近\s*(\d+)\s*天", text) or re.search(r"近\s*(\d+)\s*天", text)
    if match:
        days = int(match.group(1))
        if 1 <= days <= MAX_SPAN_DAYS:
            return today - timedelta(days=days), today
        return None
    if "今天" in text or "今日" in text:
        return today, today + timedelta(days=1)
    if "昨天" in text or "昨日" in text:
        return today - timedelta(days=1), today
    if re.search(r"上{1,2}个?月", text) or "上月" in text:
        first = today.replace(day=1)
        prev_first = (first - timedelta(days=1)).replace(day=1)
        return prev_first, first
    if "本月" in text or "这个月" in text:
        first = today.replace(day=1)
        nxt = (first + timedelta(days=32)).replace(day=1)
        return first, nxt
    if "上周" in text:
        monday = today - timedelta(days=today.weekday())
        return monday - timedelta(days=7), monday
    if "本周" in text or "这周" in text:
        monday = today - timedelta(days=today.weekday())
        return monday, monday + timedelta(days=7)
    range_match = re.search(
        r"(\d{4})[-/年](\d{1,2})[-/月](\d{1,2})日?至\s*(?:(\d{4})[-/年])?(\d{1,2})[-/月](\d{1,2})日?",
        text)
    if range_match:
        y1, m1, d1, y2, m2, d2 = (int(g) if g else None for g in range_match.groups())
        y2 = y2 or y1
        try:
            start = date(y1, m1, d1)
            end = date(y2, m2, d2)
        except (TypeError, ValueError):
            return None
        return start, end + timedelta(days=1)
    single = re.search(r"(\d{4})[-/年](\d{1,2})[-/月](\d{1,2})日?", text)
    if single:
        try:
            day = date(int(single.group(1)), int(single.group(2)), int(single.group(3)))
        except ValueError:
            return None
        return day, day + timedelta(days=1)
    spoken = re.search(
        r"(\d{1,2})月(\d{1,2})日至\s*(?:(\d{1,2})月)?(\d{1,2})日", text)
    if spoken:
        m1, d1, m2, d2 = (int(g) if g else None for g in spoken.groups())
        try:
            start = date(now.year, m1, d1)
            end_day = d2 if m2 is None else d2
            end = date(now.year, m2 or m1, end_day)
        except (TypeError, ValueError):
            return None
        if end < start:
            return None
        return start, end + timedelta(days=1)
    spoken_single = re.search(r"(\d{1,2})月(\d{1,2})日", text)
    if spoken_single:
        try:
            day = date(now.year, int(spoken_single.group(1)), int(spoken_single.group(2)))
        except ValueError:
            return None
        return day, day + timedelta(days=1)
    month_only = re.search(r"(\d{1,2})月", text)
    if month_only:
        try:
            start = date(now.year, int(month_only.group(1)), 1)
        except ValueError:
            return None
        return start, (start + timedelta(days=32)).replace(day=1)
    return None


# ---------------------------------------------------------------------------
# 覆盖与质量
# ---------------------------------------------------------------------------


def _window_range(start: date, end: date) -> tuple[datetime, datetime]:
    return (datetime(start.year, start.month, start.day, tzinfo=BEIJING),
            datetime(end.year, end.month, end.day, tzinfo=BEIJING))


def _coverage_of(assessment) -> Coverage:
    """把就绪判定结果换成对外契约的覆盖形状。

    缺口只给日期段：实体与店铺归因留在服务端 assessment.gaps，
    ERP 店铺主键不能经由 limitations / coverage 混进模型载荷。
    """
    return Coverage(
        status=assessment.status,
        start=date.fromisoformat(assessment.requested_window[0]),
        end=date.fromisoformat(assessment.requested_window[1]),
        gaps=[f"{gap_start}~{gap_end}" for gap_start, gap_end in assessment.missing_windows],
        suggested_window=assessment.suggested_window,
    )


# ---------------------------------------------------------------------------
# 固定SQL
# ---------------------------------------------------------------------------

_DAILY_SQL = """
SELECT shop_id, day, paid_amount, paid_orders, erp_documents, refund_amount, cash_difference
FROM reporting.v_shop_daily
WHERE shop_id = ANY(%s) AND day >= %s AND day < %s
ORDER BY day, shop_id
LIMIT %s
"""

# total/shop 分组按店在SQL端聚合：一行一家店，Python 不再累加被 LIMIT 截断的日行。
# 计数列 cast 成 bigint，保证驱动返回 int（sum(bigint) 在PG里是 numeric，
# 而 _compute_aov 只接受 int 分母）。
_AGG_SQL = """
SELECT shop_id,
       coalesce(sum(paid_amount), 0),
       coalesce(sum(paid_orders), 0)::bigint,
       coalesce(sum(erp_documents), 0)::bigint,
       coalesce(sum(refund_amount), 0),
       coalesce(sum(cash_difference), 0)
FROM reporting.v_shop_daily
WHERE shop_id = ANY(%s) AND day >= %s AND day < %s
GROUP BY shop_id
ORDER BY shop_id
LIMIT %s
"""

_PRODUCT_SQL = """
SELECT shop_id, day, product_id, quantity, gift_quantity, product_paid_amount,
       allocation_verified, line_kind, product_name, product_name_snapshot,
       sku_label
FROM reporting.v_product_daily
WHERE shop_id = ANY(%s) AND day >= %s AND day < %s
ORDER BY day, shop_id, product_id
LIMIT %s
"""

_GROUP_COUNT_SQL = """
SELECT count(*) FROM (
    SELECT DISTINCT shop_id, day FROM reporting.v_shop_daily
    WHERE shop_id = ANY(%s) AND day >= %s AND day < %s
) t
"""

_COHORT_SQL = """
WITH cohort AS (
    SELECT shop_id, commercial_id, amount
    FROM reporting.v_payments
    WHERE shop_id = ANY(%s) AND paid_at >= %s AND paid_at < %s AND verified
), refunds AS (
    SELECT shop_id, commercial_id, sum(raw_platform_amount) AS refunded
    FROM reporting.v_refunds
    WHERE platform_success AND refund_canonical AND platform_completed_at < %s
    GROUP BY shop_id, commercial_id
)
SELECT sum(c.amount) AS cohort_paid,
       sum(coalesce(r.refunded, 0)) AS cohort_refunded
FROM cohort c LEFT JOIN refunds r USING (shop_id, commercial_id)
"""

_SHOPS_SQL = """
SELECT shop_id, enabled, currency, platform, capabilities
FROM reporting.v_shops WHERE shop_id = ANY(%s)
"""


def _set_query_budget(conn, deadline: float) -> bool:
    """每条SQL前重算deadline剩余值并收紧timeout，不给后续SQL重新授予预算。"""
    remaining_ms = int((deadline - time.monotonic()) * 1000)
    if remaining_ms <= 0:
        return False
    conn.execute("SELECT set_config('statement_timeout', %s, true)",
                 (f"{min(5000, remaining_ms)}ms",))
    return True


class _BudgetExhausted(Exception):
    """取行阶段时间预算耗尽。

    必须与“查询真的返回零行”区分：返回 [] 会被上层当成 status="ok" 的空结果，
    模型就会把 0 当确定答案播报（C-4）。抛到这里，统一降级为 unavailable。
    """


class _RowsTruncated(Exception):
    """命中 MAX_ROWS 上限：截断后的汇总额偏低，禁止冒充 ok（C-3）。"""


def _fetch_capped(conn, sql: str, params: tuple) -> list[tuple]:
    """按 MAX_ROWS+1 取行：多出来的那一行就是截断证据，而不是默默少算。"""
    rows = conn.execute(sql, (*params, MAX_ROWS + 1)).fetchall()
    if len(rows) > MAX_ROWS:
        raise _RowsTruncated
    return rows


def _group_rows(values: dict[str, Decimal | int | None],
                metrics: list[str]) -> dict[str, str | int | None]:
    row: dict[str, str | int | None] = {}
    for metric in metrics:
        row[metric] = _render(values.get(metric))
    return row


def _render(value: Decimal | int | None) -> str | int | None:
    if isinstance(value, Decimal):
        return format(value, "f")
    return value


def _compute_aov(values: dict[str, Decimal | int | None]) -> Decimal | None:
    amount = values.get("paid_amount")
    orders = values.get("paid_orders")
    if not isinstance(amount, Decimal) or not isinstance(orders, int) or orders <= 0:
        return None
    return amount / orders


class _ShopRow(NamedTuple):
    """一家店的服务端档案：开关、币种与来源/能力解析所需的平台+能力标签。"""

    enabled: bool
    currency: str | None
    record: ShopRecord


def _capability_gap(records, metrics) -> list[str]:
    """把「这次问的指标哪些店回答不了」写成固定披露，并分开两类缺口。

    - 平台没登记来源：换指标、改日期都救不了，只能先完成接入取证。
    - 来源在但该指标未授予能力：只有逐源对账 + `sync capabilities --apply` 能开通。
    两者混成一句就会说错话（把前者说成“换个指标试试”）。

    只报家数与指标名：ERP 店铺主键不得经局限性文本进入模型载荷。能力缺口与
    覆盖缺口是两回事，所以这一步走在读取覆盖之前，也不能被“缩小日期范围”掩盖。
    """
    unregistered: set[str] = set()
    ungranted: set[str] = set()
    unregistered_metrics: set[str] = set()
    ungranted_metrics: set[str] = set()
    for record in records:
        for metric in metrics:
            reason = unsupported_reason(record, metric)
            if reason is None:
                continue
            if reason == "source_unregistered":
                unregistered.add(record.shop_id)
                unregistered_metrics.add(metric)
            else:
                ungranted.add(record.shop_id)
                ungranted_metrics.add(metric)
    texts: list[str] = []
    if unregistered:
        texts.append(
            f"{len(unregistered)} 家店铺的来源尚未开通（未授权或未同步），"
            "缩小日期范围不会补上这段数据")
    if ungranted:
        names = "、".join(sorted(ungranted_metrics))
        texts.append(f"{len(ungranted)} 家店铺缺少 {names} 的已核验能力，"
                     "未执行金额查询")
    return texts


def query_business(conn, request: QueryRequest, *, allowed_shop_ids: frozenset[str],
                   now: datetime, deadline: float) -> ToolResult:
    """确定性指标查询：覆盖门禁优先，拒绝部分汇总冒充总额。"""
    if not set(request.shop_ids) <= allowed_shop_ids:
        return ToolResult(
            status="forbidden", coverage=Coverage(status="missing", start=None, end=None),
            limitations=["店铺不在授权范围"], filters=_filters(request))
    if not _set_query_budget(conn, deadline):
        return ToolResult(
            status="unavailable", coverage=Coverage(status="missing", start=None, end=None),
            limitations=["本次查询时间预算已耗尽"], filters=_filters(request))
    try:
        if conn.info.transaction_status == psycopg.pq.TransactionStatus.IDLE:
            tx = conn.transaction()
            with tx:
                # 事务首条命令：可重复读，防止同步并发造成前后口径漂移
                conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
                conn.execute("SELECT set_config('transaction_read_only', 'on', true)")
                return _query_in_transaction(conn, request, now=now, deadline=deadline)
        else:
            # 已在外层事务（测试注入合成数据）：保存点即可，读一致怿由外层保证
            with conn.transaction():
                conn.execute("SELECT set_config('transaction_read_only', 'on', true)")
                return _query_in_transaction(conn, request, now=now, deadline=deadline)
    except psycopg.errors.QueryCanceled:
        return ToolResult(
            status="unavailable", coverage=Coverage(status="missing", start=None, end=None),
            limitations=["查询超时"], filters=_filters(request))


def _filters(request: QueryRequest) -> dict[str, object]:
    return {
        "start": request.start.isoformat(),
        "end": request.end.isoformat(),
        "shop_ids": sorted(request.shop_ids),
        "metrics": list(request.metrics),
        "group_by": request.group_by,
        "compare": request.compare,
        "currency": request.currency,
    }


def _query_in_transaction(conn, request: QueryRequest, *, now: datetime,
                          deadline: float) -> ToolResult:
    start_ts, end_ts = _window_range(request.start, request.end)
    filters = _filters(request)
    limitations: list[str] = []

    # 店铺档案：平台与已授予的指标能力一起读，后面的来源解析不得再看模型输入。
    shops = {
        row[0]: _ShopRow(enabled=row[1], currency=row[2],
                         record=ShopRecord.from_row(row[0], row[3], row[4]))
        for row in conn.execute(_SHOPS_SQL, (request.shop_ids,)).fetchall()
    }
    unknown = [s for s in request.shop_ids if s not in shops]
    if unknown:
        return ToolResult(
            status="missing_data", coverage=Coverage(status="missing", start=None, end=None),
            filters=filters, limitations=["店铺尚未同步，无法查询"])
    disabled = [s for s in request.shop_ids if not shops[s].enabled]
    if disabled:
        limitations.append("部分店铺已停用，仅返回剩余范围")
        enabled_shop_ids = [s for s in request.shop_ids if shops[s].enabled]
        if not enabled_shop_ids:
            return ToolResult(
                status="missing_data", coverage=Coverage(status="missing", start=None, end=None),
                filters=filters, limitations=["所选店铺均已停用，无法查询"])
        requested_shop_ids = sorted(request.shop_ids)
        request = request.model_copy(update={"shop_ids": enabled_shop_ids})
        filters = _filters(request)
        filters["requested_shop_ids"] = requested_shop_ids

    # 能力门禁（设计 §4）：先解析逐店逐指标的来源与能力，缺任何一项都不进金额 SQL。
    # 这一步必须在覆盖读取之前：缺能力和缺覆盖是两种不同的缺口。
    gap_texts = _capability_gap([shops[shop_id].record for shop_id in request.shop_ids],
                                request.metrics)
    if gap_texts:
        return ToolResult(
            status="missing_data", coverage=Coverage(status="missing", start=None, end=None),
            metric_definition={m: METRIC_DEFINITIONS[m] for m in request.metrics},
            filters=filters, limitations=limitations + gap_texts)

    # 覆盖与质量门禁：先判定再跑指标 SQL，缺哪段说哪段，不先聚合再掩饰。
    if not _set_query_budget(conn, deadline):
        return ToolResult(status="unavailable",
                          coverage=Coverage(status="missing", start=None, end=None),
                          filters=filters, limitations=limitations + ["本次查询时间预算已耗尽"])
    assessment = assess_query_coverage(conn, request)
    coverage = _coverage_of(assessment)
    data_as_of = assessment.data_as_of

    # 来源尚未开通的店铺单独说清：这类店缩小日期范围永远拿不到数据。
    # 金额查询路径上能力门禁已经给过同一句（不重复追加）；本行继续为
    # 直接调用 assess_query_coverage 的其他领域保留同一归因。
    if assessment.source_unconfigured:
        onboarded = (
            f"{len(assessment.source_unconfigured)} 家店铺的来源尚未开通（未授权或未同步），"
            "缩小日期范围不会补上这段数据")
        if onboarded not in limitations:
            limitations.append(onboarded)
    if assessment.quality_status == "failed":
        # 对账已知失败：不能用“覆盖完整”盖住口径问题，直接拒绝出数。
        return ToolResult(
            status="unavailable", coverage=coverage,
            metric_definition={m: METRIC_DEFINITIONS[m] for m in request.metrics},
            filters=filters, data_as_of=data_as_of,
            limitations=limitations + ["来源质量核验未通过，拒绝出数"])
    if coverage.status != "complete" or data_as_of is None:
        limitations.append("覆盖未完成，拒绝部分汇总；缺口见coverage.gaps")
        if data_as_of is None:
            limitations.append("数据截止未知（回填未完成）")
        return ToolResult(status="missing_data", coverage=coverage,
                          metric_definition={m: METRIC_DEFINITIONS[m] for m in request.metrics},
                          filters=filters, data_as_of=data_as_of, limitations=limitations)
    if assessment.quality_status == "unknown":
        # 从未对账不等于数据有错：可以出数，但必须把未核验这件事说明白。
        limitations.append("来源质量未核验（尚无对账记录）")

    # 商品归属披露：已核验支付额里没进商品维度的部分，连成因一起说清，
    # 不給模型留下自己猜原因的空间。
    attribution = describe_attribution_gap(
        attribution_gap(conn, shop_ids=request.shop_ids, start_ts=start_ts, end_ts=end_ts))
    if attribution:
        limitations.append(attribution)

    # 未匹配成功退款影响退款归属
    refund_metrics = {"refund_amount", "cash_difference", "cohort_refund_rate"}
    if set(request.metrics) & refund_metrics:
        unmatched = conn.execute(
            UNMATCHED_REFUNDS_SQL, (request.shop_ids, start_ts, end_ts)).fetchone()[0]
        if unmatched:
            limitations.append(f"存在{unmatched}条未匹配的平台成功退款，退款归属未确认")
            return ToolResult(status="missing_data", coverage=coverage,
                              metric_definition={m: METRIC_DEFINITIONS[m] for m in request.metrics},
                              filters=filters, data_as_of=data_as_of, limitations=limitations)

    # 行数预检：只对逐日分组有意义（total/shop 在SQL端按店聚合，日行不进 Python）
    if request.group_by == "day":
        groups = conn.execute(_GROUP_COUNT_SQL,
                              (request.shop_ids, request.start, request.end)).fetchone()[0]
        if groups > MAX_ROWS:
            return ToolResult(
                status="invalid_parameters", coverage=coverage,
                metric_definition={m: METRIC_DEFINITIONS[m] for m in request.metrics},
                filters=filters, data_as_of=data_as_of,
                limitations=[f"结果超过{MAX_ROWS}组，请缩小日期范围或店铺范围"])

    compare = request.compare == "previous_period"
    prev_start = prev_end = None
    if compare:
        span = request.end - request.start
        prev_start = request.start - span
        prev_end = request.start
        prev_ts = _window_range(prev_start, prev_end)
        prev_assessment = assess_query_coverage(conn, request.model_copy(update={
            "start": prev_start, "end": prev_end}))
        if (prev_assessment.status != "complete"
                or prev_assessment.data_as_of is None):
            compare = False
            limitations.append("上期覆盖不足，无法比较，仅返回绝对值")

    if not _set_query_budget(conn, deadline):
        return ToolResult(status="unavailable", coverage=coverage,
                          metric_definition={m: METRIC_DEFINITIONS[m] for m in request.metrics},
                          filters=filters, data_as_of=data_as_of,
                          limitations=limitations + ["本次查询时间预算已耗尽"])

    try:
        if request.group_by in ("total", "shop", "day"):
            rows = _period_rows(conn, request, start_ts=start_ts, end_ts=end_ts,
                                deadline=deadline, data_as_of=data_as_of,
                                limitations=limitations)
        else:
            rows = _product_rows(conn, request, start_ts=start_ts, end_ts=end_ts,
                                 deadline=deadline)

        if compare:
            if request.group_by in ("total", "shop"):
                prev_request = request.model_copy(update={
                    "start": prev_start, "end": prev_end, "compare": "none"})
                prev_rows = _period_rows(conn, prev_request, start_ts=prev_ts[0],
                                         end_ts=prev_ts[1], deadline=deadline,
                                         data_as_of=data_as_of, limitations=[])
                _attach_compare(rows, prev_rows, request)
            else:
                limitations.append("比较仅支持total/shop分组")
    except _BudgetExhausted:
        return ToolResult(status="unavailable", coverage=coverage,
                          metric_definition={m: METRIC_DEFINITIONS[m] for m in request.metrics},
                          filters=filters, data_as_of=data_as_of,
                          limitations=limitations + ["本次查询时间预算已耗尽"])
    except _RowsTruncated:
        return ToolResult(status="unavailable", coverage=coverage,
                          metric_definition={m: METRIC_DEFINITIONS[m] for m in request.metrics},
                          filters=filters, data_as_of=data_as_of,
                          limitations=limitations + [
                              f"结果行数达到{MAX_ROWS}上限，已拒绝出数以避免静默截断；"
                              "请缩小日期范围或店铺范围"])

    return ToolResult(
        status="ok", data=rows,
        metric_definition={m: METRIC_DEFINITIONS[m] for m in request.metrics},
        filters=filters, data_as_of=data_as_of, coverage=coverage,
        limitations=limitations, source_batches=assessment.source_batches)


def _period_rows(conn, request: QueryRequest, *, start_ts: datetime, end_ts: datetime,
                 deadline: float, data_as_of: datetime,
                 limitations: list[str]) -> list[dict[str, str | int | None]]:
    """total/shop/day三种分组的指标行；退款率单独查询。

    total/shop 取 _AGG_SQL（每店一行的SQL端聚合），day 取 _DAILY_SQL（日行）。
    预算耗尽/行数截断一律抛私有异常，由 _query_in_transaction 降级为 unavailable。
    """
    if not _set_query_budget(conn, deadline):
        raise _BudgetExhausted
    if request.group_by in ("total", "shop"):
        raw = _fetch_capped(conn, _AGG_SQL,
                            (request.shop_ids, start_ts.date(), end_ts.date()))
    else:
        raw = _fetch_capped(conn, _DAILY_SQL,
                            (request.shop_ids, start_ts.date(), end_ts.date()))
    need_cohort = "cohort_refund_rate" in request.metrics
    cohort_rate = None
    if need_cohort:
        if not _set_query_budget(conn, deadline):
            raise _BudgetExhausted
        cohort_paid, cohort_refunded = conn.execute(
            _COHORT_SQL, (request.shop_ids, start_ts, end_ts, data_as_of)).fetchone()
        if cohort_paid is not None and cohort_paid > 0:
            cohort_rate = (cohort_refunded or Decimal(0)) / cohort_paid
        else:
            cohort_rate = None
            limitations.append("同批支付额为0或无支付，同批退款率不可计算")

    metrics = list(request.metrics)
    if request.group_by == "total":
        totals: dict[str, Decimal | int | None] = {
            "paid_amount": Decimal(0), "paid_orders": 0, "erp_documents": 0,
            "refund_amount": Decimal(0), "cash_difference": Decimal(0)}
        for row in raw:  # 每店一行（日行已在SQL端聚合掉）
            totals["paid_amount"] += row[1]
            totals["paid_orders"] += row[2]
            totals["erp_documents"] += row[3]
            totals["refund_amount"] += row[4]
            totals["cash_difference"] += row[5]
        totals["aov"] = _compute_aov(totals)
        if need_cohort:
            totals["cohort_refund_rate"] = cohort_rate
        return [_group_rows(totals, metrics)]
    if request.group_by == "shop":
        by_shop: dict[str, dict[str, Decimal | int | None]] = {}
        for row in raw:  # 每店一行
            entry = by_shop.setdefault(row[0], {
                "paid_amount": Decimal(0), "paid_orders": 0, "erp_documents": 0,
                "refund_amount": Decimal(0), "cash_difference": Decimal(0)})
            entry["paid_amount"] += row[1]
            entry["paid_orders"] += row[2]
            entry["erp_documents"] += row[3]
            entry["refund_amount"] += row[4]
            entry["cash_difference"] += row[5]
        for entry in by_shop.values():
            entry["aov"] = _compute_aov(entry)
            if need_cohort:
                entry["cohort_refund_rate"] = cohort_rate
        return [{"shop_id": shop_id, **_group_rows(entry, metrics)}
                for shop_id, entry in sorted(by_shop.items())]
    # day：先完成覆盖检查，再对覆盖内的缺交易日补真实0
    by_day: dict[tuple[str, date], dict[str, Decimal | int | None]] = {}
    for row in raw:
        by_day[(row[0], row[1])] = {
            "paid_amount": row[2], "paid_orders": row[3], "erp_documents": row[4],
            "refund_amount": row[5], "cash_difference": row[6]}
    rows: list[dict[str, str | int | None]] = []
    for shop_id in sorted(request.shop_ids):
        day = request.start
        while day < request.end:
            entry = by_day.get((shop_id, day), {
                "paid_amount": Decimal(0), "paid_orders": 0, "erp_documents": 0,
                "refund_amount": Decimal(0), "cash_difference": Decimal(0)})
            if need_cohort:
                entry["cohort_refund_rate"] = None  # 同批比率不逐日发布
            rows.append({"shop_id": shop_id, "day": day.isoformat(),
                         **_group_rows(entry, metrics)})
            day += timedelta(days=1)
    return rows


def _product_rows(conn, request: QueryRequest, *, start_ts: datetime,
                  end_ts: datetime, deadline: float) -> list[dict[str, str | int | None]]:
    if not _set_query_budget(conn, deadline):
        raise _BudgetExhausted
    raw = _fetch_capped(conn, _PRODUCT_SQL,
                        (request.shop_ids, start_ts.date(), end_ts.date()))
    rank_metric = ("product_paid_amount" if "product_paid_amount" in request.metrics
                   else "quantity")
    by_product: dict[tuple[str, str, str], dict[str, Decimal | int | bool | None]] = {}
    names: dict[tuple[str, str], dict[str, str | None]] = {}
    sku_labels: dict[tuple[str, str, str], list[object]] = {}
    for row in raw:
        group_key = (row[0], row[2], row[7])
        entry = by_product.setdefault(group_key, {
            "quantity": Decimal(0), "gift_quantity": Decimal(0),
            "product_paid_amount": Decimal(0), "allocation_verified": True})
        entry["quantity"] += row[3]
        entry["gift_quantity"] += row[4]
        entry["product_paid_amount"] += row[5]
        entry["allocation_verified"] = bool(entry["allocation_verified"] and row[6])
        # 名称列只当内部输入：真实展示名由 catalog 投影层按一处优先级解析。
        kept = names.setdefault((row[0], row[2]), {"product_name": None,
                                                   "product_name_snapshot": None})
        for index, name_key in ((8, "product_name"), (9, "product_name_snapshot")):
            if kept[name_key] is None and row[index] is not None:
                kept[name_key] = str(row[index])
        # 规格不能“先拿到的算”：每一天都先存下来，由 pick_sku_label 统一判定。
        sku_labels.setdefault(group_key, []).append(row[10])
    ranked = sorted(by_product.items(),
                    key=lambda item: (item[1][rank_metric] or 0, item[0]),
                    reverse=True)
    rows: list[dict[str, str | int | None]] = []
    for (shop_id, product_id, line_kind), entry in ranked[:request.top_n]:
        kept = names.get((shop_id, product_id), {})
        rows.append({
            "shop_id": shop_id, "product_id": product_id, "line_kind": line_kind,
            "product_name": kept.get("product_name"),
            "product_name_snapshot": kept.get("product_name_snapshot"),
            "sku_label": pick_sku_label(sku_labels.get((shop_id, product_id, line_kind), [])),
            "quantity": _render(entry["quantity"]),
            "gift_quantity": _render(entry["gift_quantity"]),
            "product_paid_amount": _render(entry["product_paid_amount"]),
            "allocation_verified": int(entry["allocation_verified"]),
        })
    if len(ranked) > request.top_n:
        rows.append({"notice": f"仅返回Top {request.top_n}，共{len(ranked)}个商品"})
    return rows


def _attach_compare(rows: list[dict[str, str | int | None]],
                    prev_rows: list[dict[str, str | int | None]],
                    request: QueryRequest) -> None:
    prev_by_key: dict[str | None, dict[str, str | int | None]] = {}
    for row in prev_rows:
        prev_by_key[row.get("shop_id")] = row
    for row in rows:
        prev = prev_by_key.get(row.get("shop_id"))
        for metric in request.metrics:
            current = _to_decimal(row.get(metric))
            previous = _to_decimal(prev.get(metric)) if prev else None
            row[f"{metric}_previous"] = _render(previous)
            if current is None or previous is None:
                row[f"{metric}_change"] = None
                row[f"{metric}_change_ratio"] = None
                continue
            row[f"{metric}_change"] = _render(current - previous)
            if previous == 0:
                row[f"{metric}_change_ratio"] = None
            else:
                row[f"{metric}_change_ratio"] = _render((current - previous) / previous)


def _to_decimal(value: object) -> Decimal | None:
    if isinstance(value, Decimal):
        return value
    if isinstance(value, str):
        try:
            return Decimal(value)
        except ArithmeticError:
            return None
    if isinstance(value, int):
        return Decimal(value)
    return None
