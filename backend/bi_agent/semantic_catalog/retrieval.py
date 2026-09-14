"""确定性词项检索：把一句经营问题映射到首批目录里的稳定 ref（计划 Task 3）。

这一层只回答"这个问题可能用到哪些已批准结构"：不查业务事实、不编译 SQL、不授予
任何能力，也不改现有固定 Tool 路由（总设计 §5.1、§5.3）。三条硬约束：

1. **只吐 ref。** `SemanticSelection` 里没有列名、视图名、真实 ID，也没有问题原文；
   未知的词不原样回传，`missing_concepts` 只取固定表里的码。
2. **计分是死的。** 计划 Step 3 固定了三档权重、冲突表与缺失概念表，所以这里只有
   查表、数命中、比大小：同一问题在任何机器、任何子句书写顺序下得到同一份选择。
3. **答不了就说答不了。** 没有完整 JOIN 路径、口径未定、命中冲突粒度、出现未登记
   概念或一个候选都没有时，`requires_clarification` 为真——绝不为了"给个候选"而退回
   一个看着像的视图。

## 三档计分（对应计划 Step 3）

* `+100 / +40 / +20`：指标 / 字段 / 实体的**别名完整命中**（别名出现在问题里）。同类目
  内按"最长命中优先"作废被包含的短命中：没有这条，`商品销售额` 会因为含 `销售额`
  而同时点亮 `metric-paid-amount`，把两种口径的金额勾进同一个答案——那正是 Q08
  花力气澄清的坑。跨类目不作废（`商品销售额` 仍然算点名了商品实体）。
* `+5`：**词块命中**——**每个"只沾到边但没完整命中"的别名算一次**，不是每个共享子串
  算一次。计划只固定了权重，没固定计数单位；按子串计数的话，一个长中文片段能切出
  十几个二元组，把 `+40` 的字段完整命中压过去，排序就变成"谁词多谁赢"。
* `+10 × |条目领域 ∩ 本轮允许领域|`：允许领域加分。计划里"未允许领域"是直接排除，
  所以这一档的实际作用是"覆盖面更对的排前面"。**没有完整命中的条目不靠加分及格**：
  否则允许领域下每张视图都天然带分，Top 5 会退化成按字母序取前五。

视图分取"它自己证据里的最高一项"而不是求和：否则列多的视图会靠数量压过被点得更准
的视图。

## 两个口径前置判定（不是第四条 CONFLICT，也不是新的缺失概念码）

首批目录里没有任何"ERP 出库金额"列（见 `registry` 模块注释），目录里的金额全在支付
窗口上：`PAYMENT_BASIS_AMOUNT_METRICS` 里的 `支付金额`、`商品销售额`、
`商品分摊支付金额`、`支付流水金额`（后三个底层列按 `paid_at` 归日，017），以及它们
在目录里对应的**度量列**（`_payment_basis_amount_field_refs`，从已提交目录派生）。两条
判定只影响"发不发这个候选"和一个布尔位，不新增原因码——拒绝理由的分类法属于下游
运行层（总设计 §5.3 的 `needs_input` / `schema_ambiguous`）。

* 点名出库口径、又被集合里任意一个金额**或它的度量列**命中、且本轮拿不出被显式点名的
  支付金额指标 ⇒ **把支付窗口金额从候选里摘掉**并要求澄清；若整句只要这个金额，就
  退化成整份候选不发。把出库口径的金额说成支付金额，是本项目最贵的一类错；`销售额`
  是通用词，`商品销售额` 是更具体的同一个坑，两者都得拦下。
  度量列这条通道必须一起看：`metric-paid-amount` 只登记在 business_query 下，但
  `v_shop_daily` 跨两种领域，所以在只允许 commerce_performance 的一轮里，指标会被领域
  门禁挡住、剩下的只有 `field-shop-daily-paid-amount`——只看指标就会把支付窗口列当成
  出库问题的答案递出去。
* 只用通用词（销售额 / GMV）问金额，既没说支付也没说出库；或者出库口径与支付说法
  撞在一句里 ⇒ 候选照发，但 `requires_clarification` 为真（`agent.py`：「销售额」在
  未确认支付/出库口径前不能直接当支付金额——同一条红线）。

不在这个集合里的概念不会因为出现"出库"两字而被摘掉：`按出库口径看ERP单据数` 照常
发候选。但一句里同时点名出库金额与其他概念时，其他概念留下、金额被摘走，本轮仍然
要求澄清——宁可不答，也不交出一半被顶替的数。

## 还有一条与口径无关的前置判定

**问题点名的别名只有本轮允许领域用不了的条目命中时**，`requires_clarification` 为真。
计划的"未允许领域直接排除"只说了不发候选，没说可以不说——静默把半个问题扔掉，
下游就会以为剩下那半句就是全部。
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, replace
from types import MappingProxyType
from typing import Callable, Iterable, Mapping

from bi_agent.runtime.domain_registry import known_domain
from bi_agent.runtime.versions import VersionSet

from .models import SemanticCatalog, SemanticSelection
from .registry import CATALOG, catalog_indexes

MAX_VIEW_CANDIDATES = 5

METRIC_ALIAS_SCORE = 100
FIELD_ALIAS_SCORE = 40
ENTITY_ALIAS_SCORE = 20
TOKEN_SCORE = 5
DOMAIN_SCORE = 10

# 计划 Step 3 逐字固定的冲突表：两边粒度混在一个答案里必然误导。
CONFLICTS: Mapping[frozenset[str], str] = MappingProxyType({
    frozenset({"metric-product-gross-profit-reference",
               "metric-erp-gross-profit-reference"}): "profit_grain",
    frozenset({"metric-physical-available-quantity",
               "metric-channel-sellable-quantity"}): "inventory_grain",
    frozenset({"metric-transaction-average-price",
               "metric-listing-price"}): "price_basis",
})

# 目录里根本不存在的概念：只回固定码，不回原文。注意"广告"本身不是花费概念，所以
# 计划的 S03（广告归因后的净利润）只回 attribution + net_profit 两个码。
KNOWN_MISSING_CONCEPTS: Mapping[str, tuple[str, ...]] = MappingProxyType({
    "promotion_spend": ("推广费", "广告费", "投放费", "推广花费", "广告花费",
                        "投放花费", "广告消耗", "推广消耗", "投放消耗", "花费",
                        "消耗", "投产比", "ROI", "ROAS"),
    "traffic": ("流量", "访客", "曝光", "浏览量", "点击量", "UV", "PV"),
    "attribution": ("归因", "广告单", "自然单", "关联成交", "成交归因"),
    "net_profit": ("净利润", "纯利润", "净利"),
})

# 「销售额」在本项目里是两口径词：只有这些写法算"通用点名"，不是口径声明。
GENERIC_SALES_ALIASES = frozenset({"销售额", "GMV"})
PAYMENT_BASIS_PHRASES = ("支付", "付款", "已付", "买家", "账单", "回款", "流水")
# 只收"口径"说法：`出库单` 是 ERP 单据的别名，不该被当成出库口径声明。
OUTSTOCK_BASIS_PHRASES = ("出库口径", "出库金额", "出库销售额", "按出库", "以出库")
# 首批目录里"只能按支付窗口回答"的金额指标。`sales_amount` / `product_paid_amount`
# 同样是支付窗口的数（017 视图按 `paid_at` 归日），所以只盯通用词 `销售额` 会漏掉
# `商品销售额`、`商品分摊支付金额`、`支付流水金额` 这些更具体的写法——出库口径问到它们
# 时同样不许拿支付数顶数。
PAYMENT_BASIS_AMOUNT_METRICS: frozenset[str] = frozenset({
    "metric-paid-amount",           # reporting.v_shop_daily.paid_amount
    "metric-sales-amount",          # reporting.v_product_cost_daily.sales_amount
    "metric-product-paid-amount",   # reporting.v_product_daily.product_paid_amount
    "metric-payment-flow-amount",   # reporting.v_payments.amount
})

_TOKEN_RE = re.compile(r"[\u4e00-\u9fff]+|[a-z0-9_]+")
_ASCII_RE = re.compile(r"[a-z0-9_]+")
_BLANK_RE = re.compile(r"\s+")


def normalize_terms(text: object) -> tuple[str, ...]:
    """NFKC + 小写 + 词块化；去重但保留首次出现顺序。

    中文连续片段给出「整段 + 每个二元窗口」，ASCII 给出整块：只切整段的话，一句长问句
    只有一个 token，计划里的 `token 命中 +5` 就永远是 0；只切单字的话，"金""额"这种
    碎片会把排序变成字符雨。
    """
    if not isinstance(text, str):
        raise ValueError("semantic_retrieval_text_invalid")
    seen: set[str] = set()
    terms: list[str] = []
    for run in _TOKEN_RE.findall(_normalize(text)):
        for piece in _slices(run):
            if piece not in seen:
                seen.add(piece)
                terms.append(piece)
    return tuple(terms)


def _slices(run: str) -> tuple[str, ...]:
    if len(run) < 2 or _ASCII_RE.fullmatch(run):
        return (run,)
    return (run,) + tuple(run[i:i + 2] for i in range(len(run) - 1))


def _normalize(text: str) -> str:
    return _BLANK_RE.sub(" ", unicodedata.normalize("NFKC", text).strip()).lower()


@dataclass(frozen=True)
class _Span:
    """一次别名命中：位置只服务同类目的最长优先裁剪。"""

    alias: str
    start: int
    end: int

    @property
    def length(self) -> int:
        return self.end - self.start


@dataclass(frozen=True)
class _Evidence:
    """一个目录条目在本轮问题下的命中。

    先以裁剪前的原始 `spans` 建好（互相要看得见才能做最长优先），再用
    `replace(spans=kept)` 收成"确实被点名了"的证据。`terms` / `domains` 只用来算分，
    全部来自目录。
    """

    ref: str
    weight: int
    terms: tuple[str, ...]
    domains: tuple[str, ...]
    spans: tuple[_Span, ...]

    def score(self, question_terms: frozenset[str],
              allowed_domains: frozenset[str]) -> int:
        # 词块命中按词项计数（见模块注释）：一个别名最多贡献一次 +5。
        partial = sum(1 for term in dict.fromkeys(self.terms)
                      if not any(span.alias == term for span in self.spans)
                      and not set(normalize_terms(term)).isdisjoint(question_terms))
        return (len(self.spans) * self.weight
                + TOKEN_SCORE * partial
                + _domain_bonus(self.domains, allowed_domains))

    def aliases_matched(self) -> frozenset[str]:
        return frozenset(_normalize(span.alias) for span in self.spans)


def retrieve_schema_candidates(
    question: str,
    *,
    allowed_domains: frozenset[str],
    current_versions: VersionSet,
    limit: int = MAX_VIEW_CANDIDATES,
) -> SemanticSelection:
    """返回 Top `limit` 视图候选与配套的语义 ref。

    顺序按计划 Step 3 固定：领域过滤 → 别名/词项匹配 → 冲突与缺失概念 → JOIN 连通性
    → 候选裁剪 → 版本冻结。

    检索只服务当前已发布目录：`current_versions.semantic_catalog_version` 不等于
    `CATALOG.version` 时一律 `ValueError("semantic_catalog_version_mismatch")`。历史
    快照走 `catalog_for_version` 只用于解释旧 Artifact，不能用来挑选新查询
    （总设计 §5.4），所以这里也不开一个"传个旧目录进来"的口子。
    """
    normalized = _validate(question=question, allowed_domains=allowed_domains,
                           current_versions=current_versions, limit=limit)
    if current_versions.semantic_catalog_version != CATALOG.version:
        raise ValueError("semantic_catalog_version_mismatch")
    return _retrieve(CATALOG, normalized, allowed_domains=allowed_domains, limit=limit)


def _validate(*, question: object, allowed_domains: object, current_versions: object,
              limit: object) -> str:
    if not isinstance(question, str):
        raise ValueError("semantic_retrieval_question_required")
    normalized = _normalize(question)
    if not normalized:
        raise ValueError("semantic_retrieval_question_required")
    if not isinstance(allowed_domains, frozenset) or not all(
            isinstance(domain, str) for domain in allowed_domains):
        raise ValueError("semantic_retrieval_domains_invalid")
    for domain in sorted(allowed_domains):
        if not known_domain(domain):
            raise ValueError("semantic_retrieval_domain_unknown")
    if isinstance(limit, bool) or not isinstance(limit, int) or not (
            1 <= limit <= MAX_VIEW_CANDIDATES):
        raise ValueError("semantic_retrieval_limit_invalid")
    if not isinstance(current_versions, VersionSet):
        raise ValueError("semantic_retrieval_versions_invalid")
    return normalized


def _domain_allows(domains: tuple[str, ...], allowed_domains: frozenset[str]) -> bool:
    # 空允许集 = 什么都不许查：一条都不能进候选（不是"少一条"）。
    return bool(allowed_domains) and bool(set(domains) & allowed_domains)


def _domain_bonus(domains: tuple[str, ...], allowed_domains: frozenset[str]) -> int:
    return DOMAIN_SCORE * len(set(domains) & allowed_domains)


def _spans(alias: str, question: str) -> tuple[_Span, ...]:
    needle = _normalize(alias)
    if not needle:
        return ()
    if _ASCII_RE.fullmatch(needle):
        # 纯 ASCII 别名按整词匹配：`GMV` 不该在 `GMVX` 里命中。
        return tuple(_Span(alias=alias, start=match.start(), end=match.end())
                     for match in re.finditer(_ASCII_RE, question)
                     if match.group() == needle)
    found: list[_Span] = []
    start = question.find(needle)
    while start != -1:
        found.append(_Span(alias=alias, start=start, end=start + len(needle)))
        start = question.find(needle, start + 1)
    return tuple(found)


def _evidence(entries: Iterable, weight: int, normalized: str,
              allowed_domains: frozenset[str],
              domains_of: Callable[[object], tuple[str, ...]]) -> tuple[
                  dict[str, _Evidence], dict[str, _Evidence]]:
    """按类目收完整命中：先跨条目做同类目最长优先裁剪，再按允许领域分流。

    裁剪跨条目而不是只在单个条目的别名之间：`商品销售额`（一个指标）盖住 `销售额`
    （另一个指标）才是要命的情形，各自只看自己的话两种口径会一起被点亮。

    领域过滤在裁剪**之后**：词的包含关系是问题本身的性质。先筛再裁会出现"本轮不允许
    的长词让位，它的子串反过来冒充它"——`商品销售额` 在 business_query 轮里让
    `销售额` 冒充成交金额。返回 `(本轮可用, 被领域排除)`，后者只用于告知"问题点名的
    东西本轮答不了"，绝不进入候选。
    """
    candidates: list[_Evidence] = []
    for entry in entries:
        domains = tuple(domains_of(entry) or ())
        if not entry.aliases:
            continue
        spans = [span for alias in entry.aliases for span in _spans(alias, normalized)]
        if spans:
            candidates.append(_Evidence(ref=entry.ref, weight=weight, terms=entry.aliases,
                                        domains=domains, spans=tuple(spans)))
    allowed: dict[str, _Evidence] = {}
    excluded: dict[str, _Evidence] = {}
    for entry in candidates:
        kept = tuple(span for span in entry.spans if not _shadowed(span, entry, candidates))
        if not kept:
            continue
        evidence = replace(entry, spans=kept)
        target = allowed if _domain_allows(entry.domains, allowed_domains) else excluded
        target[entry.ref] = evidence
    return allowed, excluded


def _shadowed(span: _Span, entry: _Evidence, pool: list[_Evidence]) -> bool:
    """同类目里有更长的命中把这一条整段包住 ⇒ 它只是长词的一部分，不算点名。"""
    return any(other.start <= span.start and span.end <= other.end and other.length > span.length
               for item in pool if item.weight == entry.weight
               for other in item.spans)


def _ordered(evidence: Mapping[str, _Evidence], question_terms: frozenset[str],
             allowed_domains: frozenset[str]) -> tuple[str, ...]:
    """按 (-score, ref) 定序：子句书写顺序与集合遍历顺序都不得影响结果。"""
    scored = [(item.score(question_terms, allowed_domains), ref)
              for ref, item in evidence.items()]
    return tuple(ref for _score, ref in sorted(scored, key=lambda item: (-item[0], item[1])))


def _names_payment_basis(normalized: str) -> bool:
    return any(phrase in normalized for phrase in PAYMENT_BASIS_PHRASES)


def _names_outstock_basis(normalized: str) -> bool:
    return any(phrase in normalized for phrase in OUTSTOCK_BASIS_PHRASES)


def _payment_basis_amount_field_refs(indexes) -> frozenset[str]:
    """集合里那四个金额指标在目录里的度量列 ref。

    从已提交目录推出来，不再手写一份名单：同一列若改了名字或归属，指标那边变了这里
    就跟着变，不会出现"指标拦得住、列拦不住"的错位。
    """
    return frozenset(field_ref
                     for metric_ref in PAYMENT_BASIS_AMOUNT_METRICS
                     for field_ref in indexes.metrics[metric_ref].required_field_refs)


def _payment_window_amount_hits(metrics: Mapping[str, _Evidence],
                                fields: Mapping[str, _Evidence],
                                amount_field_refs: frozenset[str]) -> dict[str, _Evidence]:
    """本轮被点名的支付窗口金额：指标 ref 与它的度量列 ref 都算（可能不止一个）。"""
    return ({ref: item for ref, item in metrics.items()
            if ref in PAYMENT_BASIS_AMOUNT_METRICS}
            | {ref: item for ref, item in fields.items() if ref in amount_field_refs})


def _basis_needs_clarification(hits: Mapping[str, _Evidence], normalized: str) -> bool:
    """口径未定：只用通用词（销售额 / GMV）问金额，或出库与支付说法撞在一起。"""
    if not hits:
        return False
    if _names_outstock_basis(normalized):
        return True
    generic = {_normalize(alias) for alias in GENERIC_SALES_ALIASES}
    return (all(item.aliases_matched() <= generic for item in hits.values())
            and not _names_payment_basis(normalized))


def _drop_outstock_substitutes(metrics: dict[str, _Evidence], fields: dict[str, _Evidence],
                               hits: Mapping[str, _Evidence], indexes,
                               normalized: str) -> bool:
    """出库口径下把"只能按支付窗口回答"的金额（指标与度量列）从候选里摘走。

    只在拿不出"被显式点名的支付金额指标"时摘：`按出库口径看支付金额` 在支付指标本轮
    可用时只是自相矛盾（摘不摘都会因出库措辞而 `requires_clarification=True`）；但若本轮
    只剩裸度量列（支付指标已被领域门禁排除），把那一列发回去就是拿支付数顶出库数。
    """
    if not hits or not _names_outstock_basis(normalized):
        return False
    lit_metrics = {ref for ref in hits if ref in PAYMENT_BASIS_AMOUNT_METRICS}
    if _names_payment_basis(normalized) and lit_metrics:
        return False
    for ref in hits:
        metrics.pop(ref, None)
        fields.pop(ref, None)
        if ref in PAYMENT_BASIS_AMOUNT_METRICS:
            for field_ref in indexes.metrics[ref].required_field_refs:
                fields.pop(field_ref, None)
    return True


def _field_domains(catalog: SemanticCatalog,
                   indexes) -> Callable[[object], tuple[str, ...]]:
    """字段能不能被用完全跟着所属视图（字段自没有 domains，不分叉第二份说法）。"""
    domains = {field.ref: tuple(indexes.views[field.view_ref].domains)
               for field in catalog.fields}

    def domains_of(entry: object) -> tuple[str, ...]:
        return domains[entry.ref]
    return domains_of


def _retrieve(catalog: SemanticCatalog, normalized: str, *,
              allowed_domains: frozenset[str], limit: int) -> SemanticSelection:
    indexes = catalog_indexes(catalog)
    question_terms = frozenset(normalize_terms(normalized))

    def score_of(items: Mapping[str, _Evidence]) -> tuple[str, ...]:
        return _ordered(items, question_terms, allowed_domains)

    metrics, metrics_out = _evidence(
        catalog.metrics, METRIC_ALIAS_SCORE, normalized, allowed_domains,
        lambda item: item.domains)
    entities, entities_out = _evidence(
        catalog.entities, ENTITY_ALIAS_SCORE, normalized, allowed_domains,
        lambda item: item.domains)
    fields, fields_out = _evidence(
        catalog.fields, FIELD_ALIAS_SCORE, normalized, allowed_domains,
        _field_domains(catalog, indexes))

    # 口径判定要在摘除之前算：摘完之后"这一轮口径未定"这件事仍得留个痕迹。
    # 指标与度量列一起看：只允许某个领域时，指标可能被领域门禁挡住而列还留着。
    amount_field_refs = _payment_basis_amount_field_refs(indexes)
    basis_hits = _payment_window_amount_hits(metrics, fields, amount_field_refs)
    clarify = _basis_needs_clarification(basis_hits, normalized)
    if _drop_outstock_substitutes(metrics, fields, basis_hits, indexes, normalized):
        clarify = True
        if not metrics and not fields:
            # 整句只要这个金额：首批目录答不了，整份候选不发。
            return _no_candidates(catalog, clarify=True)

    metric_refs = score_of(metrics)
    entity_refs = score_of(entities)

    # 选中的指标自带必需字段：否则这份选择不够让下游把数算出来（"小而完整"）。
    hit_fields = dict(fields)
    for ref in metric_refs:
        for field_ref in indexes.metrics[ref].required_field_refs:
            if field_ref in hit_fields:
                continue
            field = indexes.fields[field_ref]
            hit_fields[field_ref] = _Evidence(
                ref=field_ref, weight=FIELD_ALIAS_SCORE, terms=field.aliases,
                domains=indexes.views[field.view_ref].domains, spans=())
    field_refs = _ordered(hit_fields, question_terms, allowed_domains)

    # 锚点视图 = 被完整命中的字段所属视图 ∪ 被选中指标所属视图。实体命中不锚定视图：
    # `entity-shop` 挂在十张视图上，让它锚定就等于"问了店铺就发十张表"。
    anchored = {indexes.fields[ref].view_ref for ref in fields}
    anchored |= {_metric_view(indexes, ref) for ref in metric_refs}

    # 视图分取自己证据里的最高一项（不求和）：否则"列多的视图"会靠数量压过
    # "被点得更准的视图"。
    evidence: dict[str, int] = {}

    def record(view_ref: str, score: int) -> None:
        # 证据可以是跨领域共享的（`entity-shop` 与 `店铺可售库存` 都挂在十张视图上），
        # 但视图本身没被本轮允许就不能进候选：否则一个 inventory_watch 的问题会拿到
        # business_query 的表当候选，领域门禁就白设了。
        if not _domain_allows(indexes.views[view_ref].domains, allowed_domains):
            return
        evidence[view_ref] = max(evidence.get(view_ref, 0), score)

    for ref, item in fields.items():
        record(indexes.fields[ref].view_ref, item.score(question_terms, allowed_domains))
    for ref in metric_refs:
        record(_metric_view(indexes, ref), metrics[ref].score(question_terms, allowed_domains))
    for ref, item in entities.items():
        for view in catalog.views:
            if view.entity_ref == ref:
                record(view.ref, item.score(question_terms, allowed_domains))

    ranked = tuple(sorted(evidence, key=lambda ref: (-evidence[ref], ref)))
    required = tuple(ref for ref in ranked if ref in anchored)
    fill = tuple(ref for ref in ranked if ref not in anchored)
    view_refs = (required + fill)[:limit]

    if len(required) > limit:
        # 一轮里点名了超过 limit 张视图：本轮答不清，不截一半当答案。
        clarify = True
    joins, disconnected = _join_paths(catalog, view_refs, anchored)
    missing = set(_conflict_codes(metric_refs)) | set(_known_missing(normalized))
    if disconnected or not view_refs or not anchored or missing:
        # 没有完整 JOIN 路径 / 一个候选都没有 / 没点到任何可度量的东西 / 未登记概念。
        clarify = True
    if metrics_out or fields_out or entities_out:
        # 问题点名的东西有一部分本轮允许领域给不出：静默丢掉就是猜。
        unreachable = {span.alias for item in (*metrics_out.values(), *fields_out.values(),
                                              *entities_out.values()) for span in item.spans}
        named = {span.alias for item in (*metrics.values(), *fields.values(),
                                         *entities.values()) for span in item.spans}
        # 同一个写法只要在本轮已经能用，就不算"答不了"：`抓取时间` 在三个视图里同名，
        # 允许价审时它就是可答的，不能因为库存领域被排除就跟着报澄清。
        if {_normalize(alias) for alias in unreachable} - {_normalize(a) for a in named}:
            clarify = True

    return SemanticSelection(
        catalog_version=catalog.version,
        entity_refs=entity_refs,
        metric_refs=metric_refs,
        view_refs=view_refs,
        field_refs=field_refs,
        join_path_refs=joins,
        missing_concepts=tuple(sorted(missing)),
        requires_clarification=clarify,
    )


def _no_candidates(catalog: SemanticCatalog, *, clarify: bool) -> SemanticSelection:
    return SemanticSelection(
        catalog_version=catalog.version,
        entity_refs=(),
        metric_refs=(),
        view_refs=(),
        field_refs=(),
        join_path_refs=(),
        missing_concepts=(),
        requires_clarification=clarify,
    )


def _metric_view(indexes, metric_ref: str) -> str:
    owners = {indexes.fields[ref].view_ref
              for ref in indexes.metrics[metric_ref].required_field_refs}
    # validate_catalog 已保证一个指标不跨视图；这里再兜一次，宁可不答也不猜。
    if len(owners) != 1:
        raise ValueError("semantic_catalog_metric_owner_ambiguous")
    return owners.pop()


def _join_paths(catalog: SemanticCatalog, view_refs: tuple[str, ...],
                anchored: set[str]) -> tuple[tuple[str, ...], bool]:
    """JOIN 连通性校验：只走已登记的边，且只认两侧都是本轮锚点视图的边。

    穿过店铺档案把两张事实表连起来**不算**合法路径——那正是首批目录故意不登记
    `商品成本 ↔ 单据毛利`、`实物库存 ↔ 渠道库存` 这类边的原因：一行金额被乘成两行，
    数字看起来还是对。
    """
    allowed = frozenset(view_refs) & frozenset(anchored)
    if len(allowed) < 2:
        return (), False
    needed = sorted(allowed)
    endpoints: dict[str, tuple[str, str]] = {}
    adjacency: dict[str, list[str]] = {ref: [] for ref in needed}
    for edge in catalog.joins:
        if edge.left_view_ref in allowed and edge.right_view_ref in allowed:
            endpoints[edge.ref] = (edge.left_view_ref, edge.right_view_ref)
            adjacency[edge.left_view_ref].append(edge.ref)
            adjacency[edge.right_view_ref].append(edge.ref)

    used: set[str] = set()
    reached = {needed[0]}
    pending = [ref for ref in needed if ref not in reached]
    while pending:
        step = None
        for edge_ref in sorted({e for view in reached for e in adjacency[view]}):
            left, right = endpoints[edge_ref]
            other = right if left in reached else left
            if other in pending:
                step = (edge_ref, other)
                break
        if step is None:
            return (), True                    # 有一张表接不上：不给半条路径
        used.add(step[0])
        reached.add(step[1])
        pending.remove(step[1])
    return tuple(sorted(used)), False


def _conflict_codes(metric_refs: tuple[str, ...]) -> tuple[str, ...]:
    selected = set(metric_refs)
    return tuple(code for pair, code in CONFLICTS.items() if pair <= selected)


def _known_missing(normalized: str) -> tuple[str, ...]:
    return tuple(code for code, phrases in KNOWN_MISSING_CONCEPTS.items()
                 if any(_normalize(phrase) in normalized for phrase in phrases))


__all__ = ["CONFLICTS", "KNOWN_MISSING_CONCEPTS", "MAX_VIEW_CANDIDATES",
           "normalize_terms", "retrieve_schema_candidates"]
