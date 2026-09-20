"""查询模型、结果模型、覆盖校验、固定SQL及指标口径。

没有自由SQL：指标、维度和比较映射到服务端固定模板；
权限店铺来自部署配置；SQL超时与行数受限。
"""

from __future__ import annotations

import contextlib
import time
from collections import Counter
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
# 口径政策词表：strict 拒答、separate 只按店铺分列、partitioned 按口径分区各自出 Top-N。
# 三个值一一对应三种后果，调用方与载荷校验都引用这一份，不再各拄一遍字符串。
BASIS_POLICIES: frozenset[str] = frozenset({"strict", "separate", "partitioned"})
PARTITIONED_POLICY = "partitioned"
# 组合输出上限就是最终分组上限本身：两个上限不是两个数字，而是同一条“不静默截断”规则。
MAX_PARTITIONED_ROWS = MAX_ROWS
# 分区数由注册表决定，不由店铺数决定。注册表漂到这个上界之外时 T11 先失败。
MAX_RANK_GROUPS = 4
# 「半个月」= 最近 15 个完整自然日（含今天会把没跑完的一天算进排名）。
HALF_MONTH_DAYS = 15

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
    ENTITY_REQUIREMENTS, assess_query_coverage, attribution_gap,
    describe_attribution_gap, money_text, refund_attribution_gap, required_entities,
    switched_sources_between, unverified_payments)
from bi_agent.sources import (
    METRIC_VERSION, ShopRecord, binding_signature, resolve_metric_sources,
    unsupported_reason)


class QueryRequest(BaseModel):
    """唯一查询入口；keyword-only服务端参数不在此模型内。"""

    model_config = ConfigDict(extra="forbid")
    start: date
    end: date                  # 排他，不使用用户口语的包含结束日
    shop_ids: list[str] = Field(min_length=1)
    metrics: list[Metric] = Field(min_length=1)
    group_by: Literal["total", "day", "shop", "product"] = "total"
    compare: Literal["none", "previous_period"] = "none"
    # strict 禁止把不兼容口径汇成一个值（连分列也不给，先确认口径）；
    # separate 只放行“明确分店、各带自己口径”的结果，从不产出跨口径合计；
    # partitioned 只用于商品排行：服务端按完整指标签名分区，每个分区各出 Top-N。
    basis_policy: Literal["strict", "separate", "partitioned"] = Field(
        default="strict",
        description="strict 拒绝口径不兼容的范围；separate 只允许按店铺分列、"
                    "各带自己口径的结果，永不产出跨口径合计；partitioned 仅用于"
                    "group_by=product 的多店排行，按口径分区各自出 Top-N，"
                    "分区之间不得汇总、比较或排名")
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
        if self.basis_policy == PARTITIONED_POLICY and self.group_by != "product":
            # 分区是为了“多店商品排行各出一份”；别的分组靠 strict/separate 表达分列。
            raise ValueError("basis_policy=partitioned 只支持 product 分组")
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
    # 逐店逐指标的口径凭证（内部形状，带真实 shop_id 与来源）：公开投影在
    # business_query/tool.py 里换成 shop_ref 并丢掉来源，单店结果也必须带。
    basis: list[dict[str, str]] = Field(default_factory=list)
    # 结构化诊断（可量化限制的机器可读形式）：文本披露之外还要能按字段核对。
    diagnostics: dict[str, dict[str, str | int | None]] = Field(default_factory=dict)
    # 分区榜身份（内部形状，带真实 shop_id）：公开投影在 business_query/tool.py 里
    # 换成 shop_ref。行上的 rank/rank_group 只有配合这一块才能被解释。
    rank_groups: list[dict[str, object]] = Field(default_factory=list)
    # 被排除的店铺逐家带原因（内部形状）：公开投影成 excluded_scope。
    rank_exclusions: list[dict[str, object]] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# 日期词解析：只处理有限常见词及明确日期，不能理解时返回None
# ---------------------------------------------------------------------------


# 解析种类：显式日期范围与「半个月」对窗口两侧都是权威的，其余相对/日历词只做缺省填充。
PeriodKind = Literal["explicit_range", "half_month", "relative"]


class ResolvedPeriod(NamedTuple):
    start: date
    end: date
    kind: PeriodKind


def resolve_period_detail(text: str, *, now: datetime) -> ResolvedPeriod | None:
    """返回 [start, end) 的排他日期区间及它的解析种类；无法理解返回None交给澄清。

    顺序即优先级：**显式日期范围/日期先于相对词**——一句话里同时出现“9月1日至7日”
    与“最近7天”时，写明的那个才是用户要的窗口（设计：显式范围盖过相对词）。
    """
    import re

    text = text.strip()
    today = now.astimezone(BEIJING).date()

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
        return ResolvedPeriod(start, end + timedelta(days=1), "explicit_range")
    single = re.search(r"(\d{4})[-/年](\d{1,2})[-/月](\d{1,2})日?", text)
    if single:
        try:
            day = date(int(single.group(1)), int(single.group(2)), int(single.group(3)))
        except ValueError:
            return None
        return ResolvedPeriod(day, day + timedelta(days=1), "explicit_range")
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
        return ResolvedPeriod(start, end + timedelta(days=1), "explicit_range")
    spoken_single = re.search(r"(\d{1,2})月(\d{1,2})日", text)
    if spoken_single:
        try:
            day = date(now.year, int(spoken_single.group(1)), int(spoken_single.group(2)))
        except ValueError:
            return None
        return ResolvedPeriod(day, day + timedelta(days=1), "explicit_range")

    if re.search(r"(?:近|最近)\s*半\s*个?月", text) or "半个月" in text:
        return ResolvedPeriod(today - timedelta(days=HALF_MONTH_DAYS), today,
                              "half_month")

    match = re.search(r"最近\s*(\d+)\s*天", text) or re.search(r"近\s*(\d+)\s*天", text)
    if match:
        days = int(match.group(1))
        if 1 <= days <= MAX_SPAN_DAYS:
            return ResolvedPeriod(today - timedelta(days=days), today, "relative")
        return None
    if "今天" in text or "今日" in text:
        return ResolvedPeriod(today, today + timedelta(days=1), "relative")
    if "昨天" in text or "昨日" in text:
        return ResolvedPeriod(today - timedelta(days=1), today, "relative")
    if re.search(r"上{1,2}个?月", text) or "上月" in text:
        first = today.replace(day=1)
        prev_first = (first - timedelta(days=1)).replace(day=1)
        return ResolvedPeriod(prev_first, first, "relative")
    if "本月" in text or "这个月" in text:
        first = today.replace(day=1)
        nxt = (first + timedelta(days=32)).replace(day=1)
        return ResolvedPeriod(first, nxt, "relative")
    if "上周" in text:
        monday = today - timedelta(days=today.weekday())
        return ResolvedPeriod(monday - timedelta(days=7), monday, "relative")
    if "本周" in text or "这周" in text:
        monday = today - timedelta(days=today.weekday())
        return ResolvedPeriod(monday, monday + timedelta(days=7), "relative")
    month_only = re.search(r"(\d{1,2})月", text)
    if month_only:
        try:
            start = date(now.year, int(month_only.group(1)), 1)
        except ValueError:
            return None
        return ResolvedPeriod(start, (start + timedelta(days=32)).replace(day=1),
                              "relative")
    return None


def resolve_period(text: str, *, now: datetime) -> tuple[date, date] | None:
    """返回 [start, end) 的排他日期区间；无法理解返回None交给澄清。

    形状保持不变（二元组）：调用方只关心窗口，分类由 `resolve_period_detail` 提供。
    """
    detail = resolve_period_detail(text, now=now)
    return None if detail is None else (detail.start, detail.end)


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

# 商品分组在 SQL 端先按 (店铺, 商品, line_kind) 聚合掉日行：LIMIT 只约束最终分组数，
# 不再先截断日行再聚合。否则 15 天 × 20 店的 550 行原始日行会误触 MAX_ROWS，
# 而它们其实只归成 128 个最终分组。名称/规格按 day 排序取数组，取用规则仍只在 Python /
# catalog 一处实现，SQL 不替商品任选一个展示名或规格。
_PRODUCT_SQL = """
SELECT shop_id,
       product_id,
       line_kind,
       coalesce(sum(quantity), 0),
       coalesce(sum(gift_quantity), 0),
       coalesce(sum(product_paid_amount), 0),
       bool_and(coalesce(allocation_verified, false)),
       min(day) FILTER (WHERE product_name IS NOT NULL),
       array_agg(product_name ORDER BY day)
           FILTER (WHERE product_name IS NOT NULL),
       min(day) FILTER (WHERE product_name_snapshot IS NOT NULL),
       array_agg(product_name_snapshot ORDER BY day)
           FILTER (WHERE product_name_snapshot IS NOT NULL),
       array_agg(sku_label ORDER BY day)
FROM reporting.v_product_daily
WHERE shop_id = ANY(%s) AND day >= %s AND day < %s
GROUP BY shop_id, product_id, line_kind
ORDER BY shop_id, product_id, line_kind
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


@contextlib.contextmanager
def read_only_snapshot(conn):
    """Open one REPEATABLE READ, read-only snapshot shared by all aggregate reads.

    汇总、趋势与上期必须来自同一个快照：中间插进一次回填，同一份报告就会把两个数据版本拼在一起。
    已在外层事务里（测试注入合成数据）时退化为保存点，读一致性由外层保证。
    """
    if conn.info.transaction_status == psycopg.pq.TransactionStatus.IDLE:
        with conn.transaction():
            # 事务首条命令：可重复读，防止同步并发造成前后口径漂移
            conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
            conn.execute("SELECT set_config('transaction_read_only', 'on', true)")
            yield
        return
    with conn.transaction():
        conn.execute("SELECT set_config('transaction_read_only', 'on', true)")
        yield


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


def _basis_evidence(records, metrics, bindings_by_shop_metric) -> list[dict[str, str]]:
    """给每个 (店铺, 指标) 生成一条口径凭证。

    模型不能指定口径：这份证据只由服务端注册表推导，写进结果、Artifact 与指纹。
    """
    entries: list[dict[str, str]] = []
    for record in records:
        for metric in metrics:
            bindings = bindings_by_shop_metric.get((record.shop_id, str(metric)), ())
            if not bindings:
                continue
            entries.append({
                "shop_id": record.shop_id,
                "metric": str(metric),
                "source": bindings[0].source,
                "basis": bindings[0].basis,
                "time_basis": bindings[0].time_basis,
                "metric_version": METRIC_VERSION,
            })
    return entries


def _incompatible_metrics(records, metrics, bindings_by_shop_metric) -> list[str]:
    """哪些指标在这次请求里出现了互不兼容的口径。"""
    bad: list[str] = []
    for metric in metrics:
        signatures = {
            binding_signature(bindings_by_shop_metric.get((record.shop_id, str(metric)), ()))
            for record in records
        }
        signatures.discard(())
        if len(signatures) > 1:
            bad.append(str(metric))
    return bad


def _partition_signature(metric_bindings) -> tuple:
    """分区的完整签名：**每个被请求指标各自**的 (口径, 时间归属, 认证状态) 元组。

    只看平台或只看认证状态都不够：同一个平台的两个指标可能走不同实体（`erp_documents`
    与支付族），把它们的行合在一起排行就是把两个问题的答案排进同一个榜。
    """
    return tuple(sorted((str(metric), binding_signature(bindings))
                        for metric, bindings in metric_bindings.items()))


def _partition_scope(records, metrics, bindings_by_shop_metric):
    """把请求范围拆成「答不了的店」与「按完整签名分组的可答分区」。

    返回 (unsupported, cohorts)：unsupported 是 shop_id -> 稳定原因码；cohorts 是
    按签名排序的 (signature, (shop_id, ...))，顺序确定，不依赖字典插入顺序。
    能力/来源缺口只缩到店铺粒度，不再打死整个请求；分区数由注册表决定。
    """
    names = [str(metric) for metric in metrics]
    unsupported: dict[str, str] = {}
    grouped: dict[tuple, list[str]] = {}
    for record in records:
        reason = None
        for metric in sorted(names):
            reason = unsupported_reason(record, metric)
            if reason is not None:
                break
        if reason is not None:
            unsupported[record.shop_id] = reason
            continue
        signature = _partition_signature(
            {metric: bindings_by_shop_metric[(record.shop_id, metric)]
             for metric in names})
        grouped.setdefault(signature, []).append(record.shop_id)
    cohorts = [(signature, tuple(sorted(shop_ids)))
               for signature, shop_ids in sorted(grouped.items())]
    assert len(cohorts) <= MAX_RANK_GROUPS, cohorts
    return unsupported, cohorts


def _rank_group_basis(cohort_shops, metrics, bindings_by_shop_metric) -> list[dict[str, str]]:
    """分区的口径陈述：逐指标写出 (口径, 时间归属, 认证状态)。

    分区是“同一签名”的集合，所以取第一家店的绑定就是整个分区的绑定；
    认证状态跟着一起披露，模型才能知道哪些是样本、哪些是完整窗口。
    """
    first = cohort_shops[0]
    entries: list[dict[str, str]] = []
    for metric in sorted(str(item) for item in metrics):
        bindings = bindings_by_shop_metric.get((first, metric), ())
        if not bindings:
            continue
        entries.append({"metric": metric, "basis": bindings[0].basis,
                        "time_basis": bindings[0].time_basis,
                        "time_certification": bindings[0].time_certification})
    return entries


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
        with read_only_snapshot(conn):
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
        "basis_policy": request.basis_policy,
    }


def _query_in_transaction(conn, request: QueryRequest, *, now: datetime,
                          deadline: float) -> ToolResult:
    start_ts, end_ts = _window_range(request.start, request.end)
    filters = _filters(request)
    limitations: list[str] = []
    diagnostics: dict[str, dict[str, str | int | None]] = {}

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

    records = [shops[shop_id].record for shop_id in request.shop_ids]
    entities = required_entities(request.metrics)
    bindings_by_shop_metric = {
        (record.shop_id, str(metric)): resolve_metric_sources(record, str(metric))
        for record in records for metric in request.metrics
    }

    # 分区商品排行：多店同一指标集只要不是同一口径就不能合并排名。
    # 这一支在能力门禁**之前**：能力缺口与时间口径缺口在分区粒度上是两种排除理由，
    # 把其中任何一种升级成整次请求的拒答，就是本任务要修的那个过度限制。
    if request.group_by == "product" and request.basis_policy == PARTITIONED_POLICY:
        return _partitioned_product_query(
            conn, request, records=records, filters=filters,
            bindings_by_shop_metric=bindings_by_shop_metric, start_ts=start_ts,
            end_ts=end_ts, deadline=deadline, limitations=limitations)

    # 能力门禁（设计 §4）：先解析逐店逐指标的来源与能力，缺任何一项都不进金额 SQL。
    # 这一步必须在覆盖读取之前：缺能力和缺覆盖是两种不同的缺口。
    gap_texts = _capability_gap(records, request.metrics)
    if gap_texts:
        return ToolResult(
            status="missing_data", coverage=Coverage(status="missing", start=None, end=None),
            metric_definition={m: METRIC_DEFINITIONS[m] for m in request.metrics},
            filters=filters, limitations=limitations + gap_texts)

    # 口径兼容性（设计 §6）：同名指标在不同通道上是不同问题的答案。
    # separate 只放行“分店、各带自己口径”的结果；跨口径合计、增长率与排名一律不产出。
    incompatible = _incompatible_metrics(records, request.metrics,
                                        bindings_by_shop_metric)
    if incompatible and not (request.group_by == "shop"
                             and request.basis_policy == "separate"):
        product_hint: list[str] = []
        if request.group_by == "product":
            # 多店商品排行本来就有正确出口：告诉他改哪个参数，而不是让他重试到预算耗尽。
            product_hint.append(
                "多店商品排行请改用 basis_policy=partitioned 重新发起 group_by=product "
                "查询，服务端按口径分区各自出Top-N")
        return ToolResult(
            status="invalid_parameters",
            coverage=Coverage(status="missing", start=None, end=None),
            metric_definition={m: METRIC_DEFINITIONS[m] for m in request.metrics},
            filters=filters,
            limitations=[f"这些指标在本次范围内口径互不兼容：{'、'.join(incompatible)}；"
                         "请按店铺分列后逐组查看，不能汇总或比较", *product_hint],
            basis=_basis_evidence(records, request.metrics, bindings_by_shop_metric))
    basis = _basis_evidence(records, request.metrics, bindings_by_shop_metric)

    # 覆盖与质量门禁：先判定再跑指标 SQL，缺哪段说哪段，不先聚合再掩饰。
    if not _set_query_budget(conn, deadline):
        return ToolResult(status="unavailable",
                          coverage=Coverage(status="missing", start=None, end=None),
                          filters=filters, limitations=limitations + ["本次查询时间预算已耗尽"])
    assessment = assess_query_coverage(conn, request)
    coverage = _coverage_of(assessment)
    data_as_of = assessment.data_as_of

    # 业务时间口径核对（设计 §4）：实测不成立的通道给不出“完整支付窗口”，不得出数；
    # 没逐店对照过的通道可以出数，但必须披露为可观测样本。两者都不改原请求窗口，
    # 也不与“缺覆盖”混成同一个原因——那会把未认证的口径说成等回填就能解决的问题。
    if assessment.time_basis_blocking:
        return ToolResult(
            status="missing_data", coverage=Coverage(status="missing", start=None, end=None),
            metric_definition={m: METRIC_DEFINITIONS[m] for m in request.metrics},
            filters=filters, data_as_of=data_as_of,
            limitations=limitations + [
                f"{len(assessment.time_basis_blocking)} 家店铺的付款时间口径未经认证，"
                "未执行金额查询"])
    if assessment.time_basis_disclosure:
        limitations.append(
            f"{len(assessment.time_basis_disclosure)} 家店铺的结果来自未认证付款时间口径，"
            "按可观测样本披露")

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
            limitations=limitations + ["来源质量核验未通过，拒绝出数"], basis=basis)
    if coverage.status != "complete" or data_as_of is None:
        limitations.append("覆盖未完成，拒绝部分汇总；缺口见coverage.gaps")
        if data_as_of is None:
            limitations.append("数据截止未知（回填未完成）")
        return ToolResult(status="missing_data", coverage=coverage,
                          metric_definition={m: METRIC_DEFINITIONS[m] for m in request.metrics},
                          filters=filters, data_as_of=data_as_of, limitations=limitations,
                          basis=basis, diagnostics=diagnostics)
    if assessment.quality_status == "unknown":
        # 从未对账不等于数据有错：可以出数，但必须把未核验这件事说明白。
        limitations.append("来源质量未核验（尚无对账记录）")

    # 商品归属披露：已核验支付额里没进商品维度的部分，连成因一起说清，
    # 不給模型留下自己猜原因的空间。
    attribution = describe_attribution_gap(
        attribution_gap(conn, shop_ids=request.shop_ids, start_ts=start_ts, end_ts=end_ts))
    if attribution:
        limitations.append(attribution)

    # 可量化限制而不是拒答（设计 §5、计划 5.3）：未匹配退款与未认证支付都逐结果披露。
    # 一条未匹配就把整次查询打成 missing_data，等于用另一个门禁继续拒答；
    # 让它们无声消失则是另一种错——数字会偏小而没人知道为什么。
    refund_gap = None
    if set(request.metrics) & {"refund_amount", "cash_difference", "cohort_refund_rate"}:
        refund_gap = refund_attribution_gap(conn, shop_ids=request.shop_ids,
                                            start_ts=start_ts, end_ts=end_ts)
        if refund_gap.material:
            diagnostics["unmatched_refunds"] = {
                "unmatched_count": refund_gap.unmatched,
                "successful_count": refund_gap.total,
                "unmatched_amount": money_text(refund_gap.unmatched_amount),
                "ratio": refund_gap.ratio_text,
                "currency": "CNY",
            }
        if refund_gap.material:
            limitations.append(
                f"退款归属未确认：未匹配{refund_gap.unmatched}条/共{refund_gap.total}条，"
                f"金额{money_text(refund_gap.unmatched_amount)}元，比例{refund_gap.ratio_text}")
        if "cohort_refund_rate" in request.metrics and refund_gap.material:
            # 同批率只能对已归属的退款计算：报出来的数不是“完整同批退款率”。
            limitations.append(
                f"同批退款率仅含已匹配退款（{refund_gap.unmatched}条未匹配退款无法归属，未计入）")

    if set(request.metrics) & {"paid_amount", "paid_orders", "aov",
                               "product_paid_amount", "cash_difference"}:
        unverified = unverified_payments(conn, shop_ids=request.shop_ids,
                                         start_ts=start_ts, end_ts=end_ts)
        if unverified.material:
            limitations.append(
                f"未认证支付{unverified.total}笔（金额未定{unverified.amount_undetermined}笔），"
                f"已知原始金额{money_text(unverified.known_amount)}元"
                f"（{unverified.amount_known}笔）")

    # 行数预检：只对逐日分组有意义（total/shop 在SQL端按店聚合，日行不进 Python）
    if request.group_by == "day":
        groups = conn.execute(_GROUP_COUNT_SQL,
                              (request.shop_ids, request.start, request.end)).fetchone()[0]
        if groups > MAX_ROWS:
            return ToolResult(
                status="invalid_parameters", coverage=coverage,
                metric_definition={m: METRIC_DEFINITIONS[m] for m in request.metrics},
                filters=filters, data_as_of=data_as_of,
                limitations=[f"结果超过{MAX_ROWS}组，请缩小日期范围或店铺范围"],
                basis=basis)

    compare = request.compare == "previous_period"
    prev_start = prev_end = None
    if compare:
        span = request.end - request.start
        prev_start = request.start - span
        prev_end = request.start
        prev_ts = _window_range(prev_start, prev_end)
        prev_assessment = assess_query_coverage(conn, request.model_copy(update={
            "start": prev_start, "end": prev_end}))
        # 同店换来源：两期差额只是口径变了，不是经营增长。这里拒答而不是降级比较。
        switched = switched_sources_between(conn, shop_ids=request.shop_ids,
                                            entities=entities,
                                            current={(binding.shop_id, binding.entity,
                                                      binding.source)
                                                     for bindings in
                                                     bindings_by_shop_metric.values()
                                                     for binding in bindings},
                                            start_ts=prev_ts[0], end_ts=prev_ts[1])
        if switched:
            return ToolResult(
                status="invalid_parameters", coverage=coverage,
                metric_definition={m: METRIC_DEFINITIONS[m] for m in request.metrics},
                filters=filters, data_as_of=data_as_of,
                limitations=limitations + [
                    f"{len(switched)} 家店铺的上期与本期数据来源不同，不能按增长比较"],
                basis=basis)
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
        limitations=limitations, source_batches=assessment.source_batches,
        basis=basis, diagnostics=diagnostics)


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
            # 逐日客单价同样按本日的支付金额/商业订单数算，不把整段总额摊到每一天；
            # 无单日不可除，_compute_aov 返回 None，_group_rows 只按请求指标投影。
            entry["aov"] = _compute_aov(entry)
            if need_cohort:
                entry["cohort_refund_rate"] = None  # 同批比率不逐日发布
            rows.append({"shop_id": shop_id, "day": day.isoformat(),
                         **_group_rows(entry, metrics)})
            day += timedelta(days=1)
    return rows


def _product_ranking(conn, request: QueryRequest, *, start_ts: datetime,
                     end_ts: datetime, deadline: float):
    """商品分组的全部最终分组，按排序指标降序；LIMIT 不参与（由调用方施加）。

    列序与 _PRODUCT_SQL 逐一对应；SQL 已把日行聚合成每个分组一行。
    """
    if not _set_query_budget(conn, deadline):
        raise _BudgetExhausted
    raw = _fetch_capped(conn, _PRODUCT_SQL,
                        (request.shop_ids, start_ts.date(), end_ts.date()))
    rank_metric = ("product_paid_amount" if "product_paid_amount" in request.metrics
                   else "quantity")
    by_product: dict[tuple[str, str, str], dict[str, Decimal | int | bool | None]] = {}
    names: dict[tuple[str, str], dict[str, object]] = {}
    sku_labels: dict[tuple[str, str, str], list[object]] = {}
    for row in raw:
        group_key = (row[0], row[1], row[2])
        by_product[group_key] = {
            "quantity": row[3], "gift_quantity": row[4],
            "product_paid_amount": row[5], "allocation_verified": bool(row[6])}
        # 名称列只当内部输入：真实展示名由 catalog 投影层按一处优先级解析。
        # 同一天多 line_kind 时按 day 取先到的非空值，与旧按 (day, shop, product) 逐行
        # 取“第一个非空”的语义一致（同一天内的顺序本来就不是契约）。
        kept = names.setdefault((row[0], row[1]), {})
        for field, day_index, values_index in (
                ("product_name", 7, 8), ("product_name_snapshot", 9, 10)):
            day, values = row[day_index], row[values_index]
            if day is None or not values:
                continue
            if kept.get(f"{field}_day") is None or day < kept[f"{field}_day"]:
                kept[field] = str(values[0])
                kept[f"{field}_day"] = day
        # 规格不能“先拿到的算”：分组内每一天都带上，由 pick_sku_label 统一判定。
        sku_labels[group_key] = list(row[11] or ())
    ranked = sorted(by_product.items(),
                    key=lambda item: (item[1][rank_metric] or 0, item[0]),
                    reverse=True)
    return ranked, names, sku_labels


def _product_row(group_key, entry, names, sku_labels, *, rank_group=None, rank=None):
    """一个最终分组的公开行（未投影）；rank/rank_group 只在分区榜里出现。"""
    shop_id, product_id, line_kind = group_key
    kept = names.get((shop_id, product_id), {})
    row: dict[str, str | int | None] = {
        "shop_id": shop_id, "product_id": product_id, "line_kind": line_kind,
        "product_name": kept.get("product_name"),
        "product_name_snapshot": kept.get("product_name_snapshot"),
        "sku_label": pick_sku_label(sku_labels.get(group_key, [])),
        "quantity": _render(entry["quantity"]),
        "gift_quantity": _render(entry["gift_quantity"]),
        "product_paid_amount": _render(entry["product_paid_amount"]),
        "allocation_verified": int(entry["allocation_verified"]),
    }
    if rank_group is not None:
        # 名次只在分区内计数：全局计数器会把另一个分区的第一名排到样本后面。
        row["rank_group"] = rank_group
        row["rank"] = rank
    return row


def _product_rows(conn, request: QueryRequest, *, start_ts: datetime,
                  end_ts: datetime, deadline: float) -> list[dict[str, str | int | None]]:
    ranked, names, sku_labels = _product_ranking(
        conn, request, start_ts=start_ts, end_ts=end_ts, deadline=deadline)
    rows = [_product_row(group_key, entry, names, sku_labels)
            for group_key, entry in ranked[:request.top_n]]
    if len(ranked) > request.top_n:
        rows.append({"notice": f"仅返回Top {request.top_n}，共{len(ranked)}个商品"})
    return rows


def _partitioned_unavailable(filters, texts: list[str], definitions) -> ToolResult:
    """分区查询遇到基础设施故障（预算/超时）：整个请求降级，绝不部分发布。"""
    return ToolResult(
        status="unavailable", coverage=Coverage(status="missing", start=None, end=None),
        metric_definition=definitions, filters=filters,
        limitations=texts + ["本次查询时间预算已耗尽"])


def _partitioned_product_query(conn, request: QueryRequest, *, records, filters,
                               bindings_by_shop_metric, start_ts: datetime,
                               end_ts: datetime, deadline: float,
                               limitations: list[str]) -> ToolResult:
    """多店商品排行：一次查询内按完整指标签名分区，每个分区各出 Top-N。

    可比性判断不在这里重写：分区键取 `binding_signature`，每区“能不能出数”直接
    读 `assess_query_coverage` 的现成结论。变的是**后果的作用域**——过去任何一家店
    不合格就打死整个请求，现在只打死它所在的分区，并逐家写出它为什么不进榜。
    跨分区汇总、比较与排名一律不产出；组合输出超限时一行都不发。
    """
    definitions = {str(metric): METRIC_DEFINITIONS[str(metric)]
                   for metric in request.metrics}
    texts = list(limitations)
    records_by_id = {record.shop_id: record for record in records}
    unsupported, cohorts = _partition_scope(
        records, request.metrics, bindings_by_shop_metric)
    exclusions: list[dict[str, object]] = []

    # 来源/能力缺口只缩到店铺粒度：一家店答不了，不该把其他店的榜一起拒掉。
    ungranted_metrics: set[str] = set()
    for shop_id, reason in sorted(unsupported.items()):
        if reason == "source_unregistered":
            exclusions.append({"shop_id": shop_id, "reason": "source_unregistered"})
            continue
        # 未授予与拿不到是同一句披露的两种成因：载荷里只留一个原因码。
        for metric in (str(item) for item in request.metrics):
            if unsupported_reason(records_by_id[shop_id], metric) is not None:
                ungranted_metrics.add(metric)
        exclusions.append({"shop_id": shop_id, "reason": "capability_unavailable"})

    published: list[dict[str, object]] = []
    for _signature, cohort_shops in cohorts:
        # 时间口径未认证/不成立由现成的覆盖判定给出；按分区收窄后重新判定，
        # 使“部分店铺不成立”时仍然只排除那几家。
        remaining = list(cohort_shops)
        assessment = None
        while remaining:
            if not _set_query_budget(conn, deadline):
                return _partitioned_unavailable(filters, texts, definitions)
            assessment = assess_query_coverage(
                conn, request.model_copy(update={"shop_ids": list(remaining),
                                                 "basis_policy": "strict"}))
            blocking = set(assessment.time_basis_blocking)
            if not blocking:
                break
            for shop_id in sorted(blocking):
                exclusions.append({"shop_id": shop_id,
                                   "reason": "coverage_time_basis_unverified"})
            remaining = [shop_id for shop_id in remaining if shop_id not in blocking]
        if assessment is None or not remaining:
            continue

        if assessment.quality_status == "failed":
            for shop_id in remaining:
                exclusions.append({"shop_id": shop_id, "reason": "source_quality_failed"})
            continue
        if assessment.status != "complete" or assessment.data_as_of is None:
            reason = ("coverage_incomplete" if assessment.status != "complete"
                      else "data_as_of_unknown")
            windows = [f"{gap_start}~{gap_end}"
                       for gap_start, gap_end in assessment.missing_windows]
            for shop_id in remaining:
                entry: dict[str, object] = {"shop_id": shop_id, "reason": reason}
                if windows:
                    entry["windows"] = list(windows)
                exclusions.append(entry)
            continue

        cohort = request.model_copy(update={"shop_ids": list(remaining),
                                            "basis_policy": "strict"})
        try:
            ranked, names, sku_labels = _product_ranking(
                conn, cohort, start_ts=start_ts, end_ts=end_ts, deadline=deadline)
        except _BudgetExhausted:
            return _partitioned_unavailable(filters, texts, definitions)
        except _RowsTruncated:
            # 只有行数超限变成分区排除；预算/超时是基础设施故障，绝不降级成部分发布。
            for shop_id in remaining:
                exclusions.append({"shop_id": shop_id, "reason": "result_too_large"})
            continue

        label = f"g{len(published) + 1}"
        truncated = len(ranked) > request.top_n
        rows: list[dict[str, str | int | None]] = [
            _product_row(group_key, entry, names, sku_labels,
                         rank_group=label, rank=position)
            for position, (group_key, entry)
            in enumerate(ranked[:request.top_n], start=1)]
        if truncated:
            rows.append({"notice": f"仅返回Top {request.top_n}，共{len(ranked)}个商品",
                         "rank_group": label})
        published.append({
            "label": label, "rows": rows, "shops": list(remaining),
            "sample": bool(assessment.time_basis_disclosure),
            "basis": _rank_group_basis(remaining, request.metrics,
                                       bindings_by_shop_metric),
            "groups_published": min(request.top_n, len(ranked)),
            "groups_total": len(ranked), "truncated": truncated,
            "data_as_of": assessment.data_as_of,
            "source_batches": assessment.source_batches})

    # 组合输出上限：行的总数（含分区提醒行）超限时一行都不发，绝不发布前几个分区。
    composite_rows = sum(len(cohort["rows"]) for cohort in published)
    if composite_rows > MAX_PARTITIONED_ROWS:
        return ToolResult(
            status="invalid_parameters", data=[], metric_definition=definitions,
            filters=filters,
            coverage=Coverage(status=("partial" if exclusions else "complete"),
                              start=request.start, end=request.end),
            limitations=texts + [
                f"分区结果共需 {composite_rows} 行，超过 {MAX_PARTITIONED_ROWS} 行上限；"
                "请缩小 top_n 后重试"],
            rank_groups=[], rank_exclusions=exclusions)

    reason_counts = Counter(str(entry["reason"]) for entry in exclusions)
    if reason_counts["source_unregistered"]:
        texts.append(f"{reason_counts['source_unregistered']} 家店铺的来源尚未开通"
                     "（未授权或未同步），未列入本次排行")
    if reason_counts["capability_unavailable"]:
        names = "、".join(sorted(ungranted_metrics))
        texts.append(f"{reason_counts['capability_unavailable']} 家店铺缺少 {names} 的"
                     "已核验能力，未列入本次排行")
    if reason_counts["coverage_time_basis_unverified"]:
        texts.append(f"{reason_counts['coverage_time_basis_unverified']} 家店铺的付款时间"
                     "口径未经认证（实测不成立），未列入本次排行")
    incomplete = (reason_counts["coverage_incomplete"]
                  + reason_counts["data_as_of_unknown"])
    if incomplete:
        texts.append(f"{incomplete} 家店铺的数据覆盖不足或截止未知，未列入本次排行")
    if reason_counts["source_quality_failed"]:
        texts.append(f"{reason_counts['source_quality_failed']} 家店铺的来源质量核验未通过，"
                     "未列入本次排行")
    if reason_counts["result_too_large"]:
        texts.append(f"{reason_counts['result_too_large']} 家店铺的商品分组数超过 {MAX_ROWS} "
                     "上限，未列入本次排行")

    if not published:
        return ToolResult(
            status="missing_data", data=[], metric_definition=definitions,
            filters=filters, coverage=Coverage(status="missing", start=None, end=None),
            limitations=texts, rank_exclusions=exclusions)

    published_shops = sorted({shop_id for cohort in published
                              for shop_id in cohort["shops"]})
    sample_shops = {shop_id for cohort in published if cohort["sample"]
                    for shop_id in cohort["shops"]}
    if sample_shops:
        texts.append(f"{len(sample_shops)} 家店铺的结果来自未认证付款时间口径，"
                     "按可观测样本披露")
    texts.append(f"本次结果按口径分为 {len(published)} 个分区，"
                 "分区之间不得汇总、比较或排名")

    # 商品归属披露按**已发布**店铺算：它描述的是正在发出去的那批行。
    if not _set_query_budget(conn, deadline):
        return _partitioned_unavailable(filters, texts, definitions)
    attribution = describe_attribution_gap(
        attribution_gap(conn, shop_ids=published_shops,
                        start_ts=start_ts, end_ts=end_ts))
    if attribution:
        texts.append(attribution)

    # 未认证支付披露也按**已发布**店铺算：strict 路径为同一批指标带这句话，
    # 分区路径不能因为“多店”就把它吞掉——否则金额会偏小而没人知道为什么。
    if set(request.metrics) & {"paid_amount", "paid_orders", "aov",
                               "product_paid_amount", "cash_difference"}:
        unverified = unverified_payments(conn, shop_ids=published_shops,
                                         start_ts=start_ts, end_ts=end_ts)
        if unverified.material:
            texts.append(
                f"未认证支付{unverified.total}笔（金额未定{unverified.amount_undetermined}笔），"
                f"已知原始金额{money_text(unverified.known_amount)}元"
                f"（{unverified.amount_known}笔）")

    return ToolResult(
        status="ok",
        data=[row for cohort in published for row in cohort["rows"]],
        metric_definition=definitions, filters=filters,
        data_as_of=min(cohort["data_as_of"] for cohort in published),
        coverage=Coverage(status=("partial" if exclusions else "complete"),
                          start=request.start, end=request.end),
        limitations=texts,
        source_batches=tuple(sorted({batch for cohort in published
                                     for batch in cohort["source_batches"]})),
        basis=_basis_evidence([records_by_id[shop_id] for shop_id in published_shops],
                              request.metrics, bindings_by_shop_metric),
        diagnostics={"product_ranking": {
            "groups_published": len(published),
            "shops_published": len(published_shops),
            "shops_excluded": len(exclusions),
            "rows_published": composite_rows,
            "row_cap": MAX_PARTITIONED_ROWS}},
        rank_groups=[{"group": cohort["label"],
                      "status": ("observable_sample" if cohort["sample"]
                                 else "certified"),
                      "shop_ids": cohort["shops"], "basis": cohort["basis"],
                      "groups_published": cohort["groups_published"],
                      "groups_total": cohort["groups_total"],
                      "truncated": cohort["truncated"],
                      "data_as_of": cohort["data_as_of"]}
                     for cohort in published],
        rank_exclusions=exclusions)


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
