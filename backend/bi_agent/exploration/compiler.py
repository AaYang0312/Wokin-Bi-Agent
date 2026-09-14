"""确定性单基表 SELECT 编译器（计划 Task 2 Step 4）。

产出是一条参数化 `SELECT`：模板固定、标识符全部由服务端从语义目录解出来、真值全部
走参数。五条不可让，每一条都是代码层面的强制，不是注释里的约定：

1. **只有一条事实粒度。** 所有指标必须落在同一张 base view 上，且每个指标恰好一个
   必需字段——多字段的比值 / 毛利参考要分子分母各自求和再相除，不是本模板一个聚合
   能发的数。跨粒度在这里必须是错误而不是"先拼上去看看"：`商品成本 ↔ 单据毛利`、
   `实物库存 ↔ 渠道库存` 这类边目录里故意没登记，一旦被拼出来，金额会在 JOIN 放大
   之后翻倍，而结果看起来完全正常（见 `semantic_catalog/registry.py` 的 JOIN 注记）。
2. **标识符只有一个来源。** `resolve_sql_identifier(CATALOG, ref)` 的每一段都必须过
   目录那条 `SQL_IDENTIFIER_RE`，再逐段双引号包裹；关系别名（`fact` / `shops`）与输出
   列别名同样过这条正则并带引号。schema / view / column / 聚合函数 / 排序 / limit 都
   不接受请求侧给的字符串——`ExplorationRequest` 里没有这些字段，这里也不开"透传"的
   口子（总设计 §6.3）。稳定 ref 只出现在输出列别名里，别名由 ref 做 `-` → `_` 规整
   之后再过同一条正则。
3. **授权集合与窗口只进参数。** `allowed_shop_ids` 排序后进 `%(allowed_shop_ids)s`；
   `start` / `end` / `limit` 是服务端类型的具名参数，key 顺序固定。SQL 文本里永远没有
   真店号，也没有逐店一圈的形状：一条 `= ANY(...)` 就是集合语义，200 家店与 2 家店得
   到逐字相同的 SQL（不 fan out、不 N+1）。
4. **粒度键只有一处例外。** 分组列必须属于 base view；唯一的跨视图例外是走目录里登记
   的 `N:1` 店铺档案边取平台码（计划写作 `field-platform`，已发布目录里的 ref 是
   `field-shops-platform`）。那条边的两侧就是两张视图各自的授权列，所以它不放大行数，
   聚合仍然只在事实侧做一次。
5. **每个事实视图必须有自己的 time 列与授权列。** 半开区间要知道落在哪一列，
   `= ANY(...)` 要知道落在哪一列；缺一列就不编译。授权列是哪一列由目录决定，所以
   调用方（计划 Task 5 的 `authorize_scope`）必须喂**与该视图授权列同域**的集合：
   按店铺授权的视图喂 `allowed_shop_ids`，按库存池授权的视图喂池集合。

分组列与指标列都按稳定 ref 排序后输出：同一组维度、同一组指标的问题必须得到逐字相同
的 SQL 与同一份 statement fingerprint（计划 Task 3 / Task 5 的复用依赖这一点）。
`start` / `end` 的 366 天上界与"同时给或同时不给"只在 `ExplorationRequest` 里判一次
（同一个概念不写第二份规则）；本模块只复查服务端 owns 的三件事：参数类型、`limit`
预算、授权集合形状。本模块不发 SQL、不解析 AST、不查库：AST 策略属 Task 3，只读执行
与投影属 Task 4。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

from bi_agent.exploration.models import MAX_ROWS, ExplorationRequest, SqlDraft
from bi_agent.semantic_catalog.models import (
    SQL_IDENTIFIER_RE,
    SemanticSelection,
    SemanticView,
)
from bi_agent.semantic_catalog.registry import (
    CATALOG,
    CatalogIndexes,
    catalog_indexes,
    resolve_sql_identifier,
)

# 服务端唯一允许出现的四个参数 key：元组顺序就是注入顺序，而它同时也是排序后的顺序
# ——`SqlDraft.parameters` 的 key 形状因此与序列化方式无关。
PARAMETER_KEYS = ("allowed_shop_ids", "end", "limit", "start")
# 目录聚合词表（`sum/count/avg/min/max`）里，编译器只放"对这一列聚一次就是答案"的那
# 四个。`avg` 故意不授权：`semantic_catalog/registry.py` 明写比值型指标（成交均价、
# 上架价）登记的 `avg` 只表示"它是一个平均意义上的量"，真正的值必须由分子分母在同一
# 行集合上各自求和再相除——那是本模板（一列一个聚合）发不出来的形状。
PERMITTED_AGGREGATES = frozenset({"sum", "count", "min", "max"})
# 能当分组维度的字段角色。`measure` 不是维度；`internal` 是 ERP 主键，目录登记它只为
# 让粒度可解析，并明确规定不得作为分组维度直接暴露（对外仍是既有 Tool 的 opaque ref）。
GROUPABLE_ROLES = frozenset({"dimension", "time", "authorization"})
FACT_ALIAS = "fact"
# 唯一允许跨视图分组的列：店铺档案上的平台码（计划里的 `field-platform`）。
SHOPS_VIEW_REF = "view-shops"
PLATFORM_FIELD_REF = "field-shops-platform"
# 授权列的输出别名前缀：以下划线开头的列在投影层（Task 4）里必须换成 opaque ref。
INTERNAL_ALIAS_PREFIX = "_"
MANY_TO_ONE = "many_to_one"


@dataclass(frozen=True)
class _Column:
    """一个输出列：SQL 表达式、结果别名，以及它来自哪个稳定 ref。"""

    expression: str
    alias: str
    ref: str

    @property
    def selected(self) -> str:
        return f"{self.expression} AS {_quote(self.alias)}"


@dataclass(frozen=True)
class _Fact:
    """一个指标解析出来的四件事：它自己、它的必需列、用哪个聚合、叫什么别名。"""

    metric_ref: str
    field_ref: str
    view_ref: str
    column: _Column


def compile_query(request: ExplorationRequest, *, selection: SemanticSelection,
                  allowed_shop_ids: frozenset[str]) -> SqlDraft:
    """把一次探索请求编成一条参数化 SELECT。

    失败只抛 `ValueError`，消息只有 `exploration_*` 原因码：不回显 ref、店号或问题
    原文——那些字符串可能带着 SQL 片段，不该被抄进日志、诊断或模型载荷。入参根本不是
    那两个契约属编程错误，抛 `TypeError`。
    """
    if not isinstance(request, ExplorationRequest):
        raise TypeError("exploration_request_contract_required")
    if not isinstance(selection, SemanticSelection):
        raise TypeError("exploration_selection_contract_required")
    if selection.catalog_version != CATALOG.version:
        # 标识符只能从**当前发布**的目录解：旧快照只用于解释历史 Artifact。
        raise ValueError("exploration_catalog_version_mismatch")

    scope = _server_scope(allowed_shop_ids)
    start, end, limit = _server_values(request)
    indexes = catalog_indexes(CATALOG)
    _require_selected(request.entity_refs, indexes.entities, selection.entity_refs)
    facts = _resolve_metrics(request.requested_metric_refs, selection, indexes)

    view_ref = facts[0].view_ref
    if view_ref not in selection.view_refs:
        raise ValueError("exploration_ref_not_selected")
    view = indexes.views[view_ref]
    time_ref = _time_field(view, indexes)
    authorization_ref = _authorization_field(view, indexes)
    if start is None or end is None:
        # 没有窗口就没有分母：时间口径问题不许靠"扫全表"绕过去。
        raise ValueError("exploration_window_required")

    edge = _registered_edge(view_ref, indexes) if _wants_platform(request) else None
    groups = [_group_column(ref, view_ref, edge, selection, indexes)
              for ref in sorted(request.group_by_field_refs)]
    if edge is not None and edge.ref not in selection.join_path_refs:
        # 本轮检索没把这条边选进来：不自己补一份 JOIN 路径，直接拒。
        raise ValueError("exploration_join_not_selected")

    columns = groups + [fact.column for fact in facts]
    selected = {view_ref, time_ref, authorization_ref}
    selected |= {fact.metric_ref for fact in facts}
    # 指标的必需列也要在清单里：Task 3 的 AST 比对要看"这一列是从哪个 ref 解出来的"。
    selected |= {fact.field_ref for fact in facts}
    selected |= {column.ref for column in columns}
    if edge is not None:
        selected |= {edge.ref, SHOPS_VIEW_REF, edge.left_field_ref, edge.right_field_ref}
    values = {"allowed_shop_ids": scope, "end": end, "limit": limit, "start": start}
    return SqlDraft(
        sql_text=_sql_text(view, edge, columns, groups, authorization_ref, time_ref, indexes),
        # key 集合与顺序都由 `PARAMETER_KEYS` 定：请求侧没地方往里加第五个参数。
        parameters={key: values[key] for key in PARAMETER_KEYS},
        selected_refs=sorted(selected),
    )


# --- 服务端 owns 的三件事：授权集合、参数类型、行数预算 -----------------------------

def _server_scope(allowed_shop_ids) -> list[str]:
    """授权集合只收 frozenset / set 的非空白字符串，并且在这里排序一次。

    空集合不是"查不到东西"，是"本轮没有授权"：那条 SELECT 必须不发。字符串不当集合看
    （`"S1"` 也是可迭代的），可变异容器不收——调用方交出来的授权集合不能事后又变。
    """
    if isinstance(allowed_shop_ids, (str, bytes)) or not isinstance(
            allowed_shop_ids, (frozenset, set)):
        raise ValueError("exploration_scope_invalid")
    if not all(isinstance(shop_id, str) and shop_id.strip() for shop_id in allowed_shop_ids):
        raise ValueError("exploration_scope_invalid")
    if not allowed_shop_ids:
        raise ValueError("exploration_scope_empty")
    return sorted(allowed_shop_ids)


def _server_values(request: ExplorationRequest) -> tuple[date | None, date | None, int]:
    """复查参数类型与 `limit` 预算。

    `ExplorationRequest` 的字段校验判过同样的线；再判一次是因为 `model_construct`
    一类路径能绕过字段校验，而"把 limit 抬到 500 以上"或"把日期塞成一段文本"正是请求
    方覆盖服务端参数。窗口上界（366 天）不在这里重复：那是请求契约唯一的真源。
    """
    start, end = request.start, request.end
    if (start is None) != (end is None):
        raise ValueError("exploration_window_required")
    if any(value is not None and type(value) is not date for value in (start, end)):
        raise ValueError("exploration_parameter_override")
    limit = request.limit
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_ROWS:
        raise ValueError("exploration_parameter_override")
    return start, end, limit


def _require_selected(refs, registered, selected_refs) -> None:
    """请求里的 ref 必须既在目录里、也在本轮选择里：两个理由两个码。"""
    for ref in sorted(refs):
        if ref not in registered:
            raise ValueError("exploration_ref_unregistered")
        if ref not in selected_refs:
            raise ValueError("exploration_ref_not_selected")


# --- 目录解析 ---------------------------------------------------------------------

def _resolve_metrics(metric_refs, selection: SemanticSelection,
                     indexes: CatalogIndexes) -> tuple[_Fact, ...]:
    """把指标 ref 解成 `_Fact`，并强制"同一张 base view、一个必需字段、一个聚合"。

    按 ref 排序后逐个判：目录里没有的写法（计划矩阵里的 `metric-quantity`）先撞
    `exploration_ref_unregistered`，不会因为它在固定 Tool 矩阵里出现就拿到解析路径。
    """
    facts: list[_Fact] = []
    for ref in sorted(metric_refs):
        metric = indexes.metrics.get(ref)
        if metric is None:
            raise ValueError("exploration_ref_unregistered")
        if ref not in selection.metric_refs:
            raise ValueError("exploration_ref_not_selected")
        if len(metric.required_field_refs) != 1:
            # 比值与毛利参考是"多个数各自求和再相除"，一个聚合发不出来。
            raise ValueError("exploration_metric_field_ambiguous")
        aggregate = metric.default_aggregate
        if aggregate not in PERMITTED_AGGREGATES or aggregate not in metric.allowed_aggregates:
            raise ValueError("exploration_aggregate_not_permitted")
        field_ref = metric.required_field_refs[0]
        field = indexes.fields.get(field_ref)
        if field is None:                    # validate_catalog 之后不可达，仍不放开
            raise ValueError("exploration_ref_unregistered")
        facts.append(_Fact(
            metric_ref=ref, field_ref=field_ref, view_ref=field.view_ref,
            # 聚合函数名只能来自目录的封闭词表（`SemanticMetric.default_aggregate`）。
            column=_Column(
                expression=f"{_bare(aggregate)}({_bare(FACT_ALIAS)}."
                           f"{_quote(field.column)})",
                alias=ref.replace("-", "_"), ref=ref)))
    if len({fact.view_ref for fact in facts}) > 1:
        raise ValueError("exploration_multiple_fact_grains")
    # 指标列按稳定 ref 排序：`sorted(metric_refs)` 已经保证了顺序，这里不再动。
    return tuple(facts)


def _time_field(view: SemanticView, indexes: CatalogIndexes) -> str:
    """事实视图必须**恰好一个** time 字段：半开区间要知道落在哪一列。"""
    times = [ref for ref in view.field_refs if indexes.fields[ref].role == "time"]
    if not times:
        raise ValueError("exploration_window_required")
    if len(times) > 1:
        # 两个时间列就是两种时间口径：选哪一个都是在猜，宁可不答。
        raise ValueError("exploration_time_field_ambiguous")
    return times[0]


def _authorization_field(view: SemanticView, indexes: CatalogIndexes) -> str:
    """事实视图必须有一个属于自己的授权列：没有它就没有 `= ANY(...)` 谓词。"""
    ref = view.authorization_field_ref
    authorization = indexes.fields.get(ref)
    if (authorization is None or authorization.role != "authorization"
            or ref not in view.field_refs or authorization.view_ref != view.ref):
        raise ValueError("exploration_authorization_field_missing")
    return ref


def _wants_platform(request: ExplorationRequest) -> bool:
    """请求的分组列里是否有"只能从店铺档案来"的那个平台码。"""
    return PLATFORM_FIELD_REF in request.group_by_field_refs


def _registered_edge(view_ref: str, indexes: CatalogIndexes):
    """取 `view_ref` → 店铺档案那条登记过的边；没有就 `exploration_join_not_registered`。

    只认 `many_to_one`：1:1 同样不放大行数，但本目录里到店铺档案的边全部登记成 N:1，
    放开别的基数等于替目录做一个没批过的决定。
    """
    for candidate in indexes.joins.values():
        if (candidate.left_view_ref == view_ref and candidate.right_view_ref == SHOPS_VIEW_REF
                and candidate.cardinality == MANY_TO_ONE):
            return candidate
    raise ValueError("exploration_join_not_registered")


def _group_column(ref: str, view_ref: str, edge, selection: SemanticSelection,
                  indexes: CatalogIndexes) -> _Column:
    """分组列 → 表达式与别名；跨视图只允许那条档案边上的平台列。"""
    field = indexes.fields.get(ref)
    if field is None:
        raise ValueError("exploration_ref_unregistered")
    if ref not in selection.field_refs:
        raise ValueError("exploration_ref_not_selected")
    if field.role not in GROUPABLE_ROLES:
        raise ValueError("exploration_group_not_permitted")
    if field.view_ref == view_ref:
        relation = FACT_ALIAS
    elif edge is not None and field.ref == PLATFORM_FIELD_REF:
        relation = _relation_alias(indexes.views[SHOPS_VIEW_REF].name)
    else:
        raise ValueError("exploration_join_not_registered")
    return _Column(expression=f"{relation}.{_quote(field.column)}",
                   alias=_group_alias(ref, field.role, field.column),
                   ref=ref)


def _group_alias(ref: str, role: str, column: str) -> str:
    """授权列不按 ref 输出，而按 `_column` 输出。

    投影层（计划 Task 4）的规则是"以 `_` 开头的列必须是内部列并换成 opaque ref"：把
    `shop_id` 按稳定 ref 发出去，真店号就成了一条看起来正常的结果列。
    """
    if role == "authorization":
        return f"{INTERNAL_ALIAS_PREFIX}{column}"
    return ref.replace("-", "_")


# --- 标识符：只从目录解，只以引号形式出现 ------------------------------------------

def _resolve(ref: str) -> tuple[str, ...]:
    try:
        return resolve_sql_identifier(CATALOG, ref)
    except KeyError:                          # 不回显入参：它可能带着 SQL 片段
        raise ValueError("exploration_ref_unresolved") from None


def _table_of(view_ref: str) -> str:
    identifiers = _resolve(view_ref)
    if len(identifiers) != 2:
        raise ValueError("exploration_ref_unresolved")
    return ".".join(_quote(part) for part in identifiers)


def _column_of(field_ref: str) -> str:
    identifiers = _resolve(field_ref)
    if len(identifiers) != 3:
        raise ValueError("exploration_ref_unresolved")
    return identifiers[2]


def _relation_alias(view_name: str) -> str:
    """JOIN 侧的关系别名：由已登记的视图名去掉 `v_` 前缀得到（`v_shops` → `shops`）。"""
    alias = view_name[2:] if view_name.startswith("v_") else view_name
    if alias == FACT_ALIAS:
        raise ValueError("exploration_identifier_invalid")
    return _bare(alias)


def _bare(identifier: str) -> str:
    """模板自带的标识符（关系别名与聚合函数名）：同样只收过正则的裸名字。

    它们不是从请求侧来的：`fact` 是本模块的字面量，档案别名由已登记视图名推导，
    函数名只能来自目录的封闭词表。不包引号是为了让 SQL 文本与计划的模板逐字一致
    （`FROM {reporting_view} AS fact`），但过正则这一点不打折：一个带引号或点号的
    "别名"在这里同样进不来。
    """
    if re.fullmatch(SQL_IDENTIFIER_RE, identifier) is None or '"' in identifier:
        raise ValueError("exploration_identifier_invalid")
    return identifier


def _quote(identifier: str) -> str:
    """目录解出来的标识符逐段双引号包裹。

    `SQL_IDENTIFIER_RE` 已经排除引号、空白与点号；`'"' in identifier` 那一判是防正则
    被改掉时静默放开转义面。
    """
    if re.fullmatch(SQL_IDENTIFIER_RE, identifier) is None or '"' in identifier:
        raise ValueError("exploration_identifier_invalid")
    return f'"{identifier}"'


# --- SQL 文本（模板固定）----------------------------------------------------------

def _sql_text(view: SemanticView, edge, columns: list[_Column], groups: list[_Column],
              authorization_ref: str, time_ref: str, indexes: CatalogIndexes) -> str:
    fact = _bare(FACT_ALIAS)
    lines = [f"SELECT {', '.join(column.selected for column in columns)}",
             f"FROM {_table_of(view.ref)} AS {fact}"]
    if edge is not None:
        relation = _relation_alias(indexes.views[SHOPS_VIEW_REF].name)
        lines.append(
            f"INNER JOIN {_table_of(edge.right_view_ref)} AS {relation}"
            f" ON {relation}.{_quote(_column_of(edge.right_field_ref))}"
            f" = {fact}.{_quote(_column_of(edge.left_field_ref))}")
    time_column = _quote(_column_of(time_ref))
    lines.append(f"WHERE {fact}.{_quote(_column_of(authorization_ref))}"
                 f" = ANY(%(allowed_shop_ids)s)")
    lines.append(f"  AND {fact}.{time_column} >= %(start)s")
    lines.append(f"  AND {fact}.{time_column} < %(end)s")
    if groups:
        # 只按分组列排序：聚合值没有请求侧的排序旋钮（那属于 Task 4 的展示层）。
        keys = ", ".join(column.expression for column in groups)
        lines.append(f"GROUP BY {keys}")
        lines.append(f"ORDER BY {keys}")
    lines.append("LIMIT %(limit)s")
    return "\n".join(lines)
