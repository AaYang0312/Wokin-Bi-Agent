"""受控 SQL 的 AST 策略与语句指纹（计划 Task 3）。

编译器（Task 2）只会发出一条固定模板的 SELECT；这一层的职责是**独立地**证明递进来的
`SqlDraft` 确实是那条语句：即使编译器哪天被改掉、被绕过，或者调用方根本不用编译器，
过不了这一层的 SQL 也拿不到可执行计划。三条不可让：

1. **一条非递归 SELECT，并且只有这一条。** 占位符原样留在文本里，解析前逐个换成 SQL
   `NULL`（`sqlglot` 不认 psycopg 的 `%(name)s`），换完才解析；执行与指纹用的仍是**原文**。
   语句数必须为 1、根节点必须是 `exp.Select`，AST 里不许出现
   `With/Subquery/集合运算/命令/DML/DDL/Copy/Lock`。注释在文本层就拒：解析器会静默丢掉
   注释内容，那等于让策略看不到被吞掉的那半条 WHERE。
2. **白名单，不是黑名单。** 允许的函数只有 `sum/count/avg/min/max`，加上授权谓词里的
   `ANY`；允许的节点类型是一份封闭清单，清单外的任何节点（`Offset/Distinct/
   Having/Cast/Array/...`）一律拒。`SELECT *` 与 `count(*)` 都因为 `exp.Star` 被拒；未登记
   schema/view/column、未登记或不限定的 JOIN、CROSS/NATURAL/逗号连接同样在这里拒，而不
   是等数据库报"表不存在"——那时已经执行过了。

   一个必须记下来的陷阱：sqlglot 27.29 把 `And`/`Or` 这类连接词与 `Cast`/`Array` 这类
   语法都做成了 `exp.Func` 子类（`ANY` 在当前版本不是 `Func`，但以后可能是）。所以函数
   白名单必须同时参考节点白名单：`And` 是模板里的结构节点、在这里放行，`Or`/`Cast`/
   `Array` 不在节点清单里、继续被拒；`ANY` 即使被放行也只到"节点类型合法"为止，能过
   的具体形状仍然只有 `_is_authorization` 认的那一种（授权列 + `= ANY(<NULL>)`）。
3. **形状必须与本轮检索和目录解析完全相容。** 表、列、JOIN 配对既要在 `CATALOG` 里解得
   出来，又要在 `selection` 里；WHERE 必须恰好三条叶谓词——授权列 `= ANY(NULL)`、
   `>= NULL`、`< NULL`（半开区间 `[start, end)`）——并且 `LIMIT` 必须存在；四个占位符的
   **名字顺序**与**所在谓词位置**一起判，`ANY(%(limit)s)` 配 `LIMIT %(allowed_shop_ids)s`
   这种换位置的写法过不去。授权列与时间列由目录决定（库存池视图上是 `pool_id`），这里不
   写死 `shop_id`。

`DomainContext` 只取三样东西：`allowed_shop_ids`（必须与参数里的授权集合逐字相等）、
`now`（必须带时区，用来证明"策略不看墙上时间"而不是拿它算任何东西）、`deadline`（只校
形状，不读时钟——预算由 Task 4/5 在真正会花时间的步骤上 enforced，这里再读一次只会让
这个纯函数不可重现）。

错误只有一个 `exploration_*` 原因码：不回显 SQL 片段、ref、店号或问题原文（那些字符串
可能带着语句，会一路进日志与诊断）。同一概念的码与 Task 1/2 逐字共用：
`exploration_catalog_version_mismatch`、`exploration_join_not_selected`、
`exploration_ref_not_selected`、`exploration_scope_invalid`／`_empty`、
`exploration_parameter_override` 之外的取值问题叫 `exploration_parameter_invalid`。

指纹是计划 Task 3 Step 4 那份 JSON 的 SHA-256；授权集合以摘要进指纹，真店号不进任何
持久化载荷（`ValidatedQueryPlan` 是内部计划对象，它与 SQL 原文一起只进
`bi.query_diagnostics`，总设计 §6.2）。

本模块不连库、不 EXPLAIN、不执行、不投影：那分属 Task 4/5。
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from types import MappingProxyType
from typing import Iterator, Mapping, NoReturn, Sequence

import sqlglot
from sqlglot import expressions as exp

from bi_agent.commerce.models import DomainContext
from bi_agent.exploration.compiler import GROUPABLE_ROLES, PARAMETER_KEYS
from bi_agent.exploration.models import (
    MAX_ROWS,
    MAX_WINDOW_DAYS,
    SqlDraft,
    ValidatedQueryPlan,
)
from bi_agent.semantic_catalog.models import SQL_IDENTIFIER_RE, SemanticSelection
from bi_agent.semantic_catalog.registry import (
    CATALOG,
    CatalogIndexes,
    catalog_indexes,
)

# 解析方言按计划钉死为 postgres：换方言等于换一套语法，不在这里讨论。
PARSER_DIALECT = "postgres"
# 本切片产出的 SQL 模板版本：Task 5 把它连同 catalog_version 一起记进诊断与 Artifact，
# 模板推进后旧计划必须重新生成（总设计 §5.4 的"版本变化后不可复用"）。
EXPLORATION_TEMPLATE_VERSION = "exploration-sql/2026-09-14.1"
# 占位符的唯一合法写法：`%(<lower_snake_case>)s`。名字集合就是编译器 owns 的那四个
# key，这里的顺序是**模板里出现的顺序**（授权 → 起 → 止 → limit）。
PLACEHOLDER_NAME_RE = re.compile(r"%\(([a-z][a-z0-9_]*)\)s")
PLACEHOLDER_ORDER = ("allowed_shop_ids", "start", "end", "limit")
NULL_TOKEN = "NULL"
# 解析器会静默吞掉或改名的一律在文本层拒。`#` 与 `--`、`/* */` 是注释；反斜杠在标准
# 字符串字面量之外没有合法用处，留着它等于让策略与执行看到两条不同的文本。
UNSAFE_TEXT_TOKENS = ("--", "/*", "*/", "#", "\\")
# 每个参数位必须紧跟的谓词片段：把"名字"与"位置"绑在一起判。
PLACEHOLDER_SITES = (
    re.compile(r"=\s*ANY\(\s*\Z", re.IGNORECASE),
    re.compile(r">=\s*\Z"),
    re.compile(r"<\s*\Z"),
    re.compile(r"\bLIMIT\s+\Z", re.IGNORECASE),
)
POLICY_CODE_PREFIX = "exploration_"
# 聚合函数白名单（计划 Task 3 Step 4）。类必须逐字对上，子类不算：`exp.ArrayAgg` 与
# `exp.Sum` 是两套语法，"看起来像聚合"不该让它进到只允许那五个名字的位置。
AGGREGATE_NODES = MappingProxyType({
    "sum": exp.Sum, "count": exp.Count, "avg": exp.Avg, "min": exp.Min, "max": exp.Max,
})
AGGREGATE_NAMES = MappingProxyType({node: name for name, node in AGGREGATE_NODES.items()})
# 封闭的节点清单：固定模板能出现的节点只有这些，多一个都是没批过的语法面。
ALLOWED_NODES = frozenset({
    exp.Select, exp.From, exp.Join, exp.Where, exp.Group, exp.Order, exp.Ordered,
    exp.Limit, exp.Alias, exp.Column, exp.Table, exp.TableAlias, exp.Identifier,
    exp.Null, exp.And, exp.EQ, exp.GTE, exp.LT, exp.Any, exp.Paren,
}) | frozenset(AGGREGATE_NODES.values())
# 逐类扫描的顺序就是原因码的优先级：一条语句里同时藏着 CTE 与 DELETE 时报 CTE，
# 因为那是它先进入被禁类别的那一层。每条一个概念，不合并成"万能码"。
FORBIDDEN_CATEGORIES: tuple[tuple[str, tuple[type, ...]], ...] = (
    ("cte_forbidden", (exp.With, exp.CTE)),
    ("subquery_forbidden", (exp.Subquery, exp.Exists, exp.Lateral)),
    ("set_operation_forbidden", (exp.SetOperation,)),
    ("dml_forbidden", (exp.Insert, exp.Update, exp.Delete, exp.Merge, exp.Values)),
    ("ddl_forbidden", (exp.Create, exp.Drop, exp.Alter, exp.TruncateTable)),
    ("copy_forbidden", (exp.Copy,)),
    ("lock_forbidden", (exp.Lock,)),
    ("command_forbidden", (exp.Command, exp.Transaction, exp.Commit, exp.Rollback,
                           exp.Pragma, exp.Set, exp.Show, exp.Grant)),
    ("star_forbidden", (exp.Star,)),
)
# 只允许 `INNER JOIN`（含裸 `JOIN`）：LEFT/RIGHT/FULL 会改变行数语义，`USING` 把配对
# 条件从 AST 里收进名字清单，两者都不在计划批过的形状内。
PERMITTED_JOIN_SIDES = frozenset({""})
PERMITTED_JOIN_KINDS = frozenset({"", "INNER"})
# `exp.Ordered` 除 `this` 以外的全部修饰键（键名逐字取自它的 `arg_types`）：ORDER BY 一项
# 只要带上任何一个真值就不再是固定模板。测试会拿这份名单比 `arg_types`，防止再出现
# “读一个不存在的键”这种永远不生效的分句。
ORDER_MODIFIER_ARGS = ("desc", "nulls_first", "with_fill")
# 目录里视图名的 schema 只可能是 `reporting`（`semantic_catalog.models` 的封闭词表）；
# 这里仍然逐个比对解出来的那对名字，不拿这个前提当校验。


@dataclass(frozen=True)
class _Relation:
    """一个已解析的关系：目录里的 view ref，加上这句 SQL 引用它所使用的限定名。"""

    view_ref: str
    qualifier: str


@dataclass(frozen=True)
class _Skeleton:
    """过了结构校验的语句骨架：后面的阶段只认它，不再回头翻 AST。"""

    fact: _Relation
    authorization_field_ref: str
    time_field_ref: str
    relations: Mapping[str, _Relation]


@dataclass
class _Usage:
    """这句 SQL 真正落到了哪些 ref 上——指纹之外的第二份证据。"""

    views: set[str] = field(default_factory=set)
    fields: set[str] = field(default_factory=set)
    metrics: set[str] = field(default_factory=set)
    joins: set[str] = field(default_factory=set)

    @property
    def refs(self) -> frozenset[str]:
        return frozenset(self.views | self.fields | self.metrics | self.joins)


def validate_exploration_plan(draft: SqlDraft, *, selection: SemanticSelection,
                              context: DomainContext) -> ValidatedQueryPlan:
    """把一条 SQL 草案校验成可执行计划；不合格只抛 `exploration_*` 原因码。

    顺序即门禁：文本 → 占位符 → 解析 → 类别 → 关系/列 → 谓词 → 参数与授权 → 输出列 →
    节点白名单 → ref 闭环 → 指纹。本函数根本不连库，所以"在数据库执行前拒绝"是构造上
    成立的，不依赖调用点。
    """
    _require_input_contracts(draft, selection, context)
    if selection.catalog_version != CATALOG.version:
        # 旧快照只用来解释历史 Artifact，不能用来认新 SQL（总设计 §5.4）。
        _reject("catalog_version_mismatch")
    _require_aware_context(context)

    index = catalog_indexes(CATALOG)
    fragments, names = _split_placeholders(draft.sql_text)
    tree = _parse_single_select(NULL_TOKEN.join(fragments))
    nodes = list(tree.walk())
    # `walk()` 含根节点：顶层 DML/DDL/Copy/命令因此先拿到自己的码，
    # `statement_not_select` 只是"不是 SELECT 的别的东西"的兜底。
    _require_no_forbidden_category(nodes)
    if not isinstance(tree, exp.Select):
        _reject("statement_not_select")
    usage = _Usage()
    skeleton = _require_relations(tree, selection, index, usage)
    _require_columns(nodes, skeleton, selection, index, usage)
    _require_predicates(tree, nodes, skeleton, index)
    scope = _require_parameters(draft, fragments, names, context)
    _require_output_columns(tree, skeleton, draft.selected_refs, selection, index, usage)
    _require_allowed_nodes(nodes)
    _require_ref_closure(draft.selected_refs, usage, selection)
    return _plan(draft, selection, scope, names)


# --- 入参契约与上下文 ----------------------------------------------------------

def _require_input_contracts(draft, selection, context) -> None:
    """入参类型不对是编程错误（`TypeError`），不是策略拒绝（`ValueError`）。

    与编译器同一套规矩：`SqlDraft`／`SemanticSelection`／`DomainContext` 都是已交付契约，
    `model_construct` 造出来的假壳子在这里过不去（后面每一步都重校取值）。
    """
    if not isinstance(draft, SqlDraft):
        raise TypeError("exploration_draft_contract_required")
    if not isinstance(selection, SemanticSelection):
        raise TypeError("exploration_selection_contract_required")
    if not isinstance(context, DomainContext):
        raise TypeError("exploration_context_contract_required")


def _require_aware_context(context: DomainContext) -> None:
    """时刻必须带时区，预算必须是有限正数：只校形状，不读时钟。

    不要求"必须是 UTC"：仓库里 `DomainContext.now` 既有 UTC 也有 `Asia/Shanghai`，把
    UTC 当硬条件会把合法上下文误拒成"策略自己错了"。时间口径在 SQL 参数与 Task 4 执行
    层，本函数一次都不看墙上时间——指纹因此与 `now`／`deadline` 无关（有用例钉住）。
    """
    now = context.now
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        _reject("context_time_naive")
    deadline = context.deadline
    if (isinstance(deadline, bool) or not isinstance(deadline, (int, float))
            or not math.isfinite(deadline) or deadline <= 0):
        _reject("context_deadline_invalid")


# --- 文本层：注释与占位符 ------------------------------------------------------

def _split_placeholders(sql_text: str) -> tuple[list[str], tuple[str, ...]]:
    """按 `%(name)s` 切句：返回字面片段与按出现顺序排列的参数名。

    剩下的文本里只要还有一个 `%`，就说明某处写法不是这个形状（`%s`、`%(Foo)s`、
    `%(limit)d`、裸 `%`）：一律拒。这不是洁癖——psycopg 按自己的规则解释这些写法，
    那时策略看到的就不是将要执行的那句话。
    """
    if any(token in sql_text for token in UNSAFE_TEXT_TOKENS) \
            or any(ord(ch) < 32 and ch not in "\n\t" for ch in sql_text) \
            or "\r" in sql_text:
        # 注释会被解析器静默丢掉：被吞掉的文本没机会被看过。
        _reject("sql_text_unsafe")
    fragments: list[str] = []
    names: list[str] = []
    position = 0
    for match in PLACEHOLDER_NAME_RE.finditer(sql_text):
        fragments.append(sql_text[position:match.start()])
        names.append(match.group(1))
        position = match.end()
    fragments.append(sql_text[position:])
    if any("%" in fragment for fragment in fragments):
        _reject("placeholder_invalid")
    return fragments, tuple(names)


# --- 解析层：一条、且只是一条 SELECT -------------------------------------------

def _parse_single_select(normalized: str) -> exp.Expression:
    """解析 NULL 化后的文本：语句数必须为 1（根节点是不是 SELECT 由调用方接着判）。

    解析失败不当"看不懂就先放行"，也不当"语法小事"放过：拿不到 AST 就没有任何白名单可
    言，而解析器抛出的文本带着 SQL 片段与行列号，不能被拄进错误消息。
    """
    try:
        statements = sqlglot.parse(normalized, read=PARSER_DIALECT)
    except Exception:                              # 包括 sqlglot 的 ParseError
        _reject("sql_unparseable")
    if len(statements) != 1 or statements[0] is None:
        # 0 条与多条都不是"一条 SELECT"：`SELECT 1; DELETE ...` 在这里被拦。
        _reject("multiple_statements")
    return statements[0]


def _require_no_forbidden_category(nodes: Sequence[exp.Expression]) -> None:
    """按类别优先级扫一遍 AST：CTE、子查询、集合运算、DML/DDL/Copy/Lock/命令、星号。

    再加两条：`Select` 节点必须恰好一个（`(SELECT ...)`、`EXISTS (...)` 的体都算第二
    个），以及 JOIN 必须带登记过的形状、函数必须在五个名字里。
    """
    for reason, kinds in FORBIDDEN_CATEGORIES:
        if any(isinstance(node, kinds) for node in nodes):
            _reject(reason)
    if sum(1 for node in nodes if isinstance(node, exp.Select)) != 1:
        _reject("subquery_forbidden")
    for node in nodes:
        if isinstance(node, exp.Join) and _join_is_unqualified(node):
            # CROSS / NATURAL / 逗号连接 / USING：没有 ON 就等于笛卡尔积。
            _reject("cross_join_forbidden")
        if (isinstance(node, exp.Func) and type(node) not in AGGREGATE_NAMES
                and type(node) not in ALLOWED_NODES):
            # `exp.Anonymous`（`pg_read_file(...)`）与任何登记外的具名函数都走这里。
            # sqlglot 把 `And`/`Or` 这类连接词与 `Any` 也做成 `Func` 子类：它们不是"函数
            # 调用"，而是由节点白名单与下面的专用校验各自负责的形状——`ANY` 只在这里被
            # 放过，能过的具体形状仍然只有 `_is_authorization` 认的那一种。
            _reject("function_forbidden")


def _join_is_unqualified(node: exp.Join) -> bool:
    """CROSS / NATURAL / 逗号连接 / USING / 没有 ON：没有可核对的配对条件。"""
    return (not node.args.get("on") or node.args.get("using") is not None
            or bool(node.args.get("natural"))
            or (node.kind or "").upper() == "CROSS")


# --- 关系层：表、别名与登记过的 JOIN -------------------------------------------

def _require_relations(tree: exp.Expression, selection: SemanticSelection,
                       index: CatalogIndexes, usage: _Usage) -> _Skeleton:
    """一个基表 + 至多若干条**登记过并且本轮选中**的边：其余配对一律拒。

    视图必须带 schema（`FROM v_shops` 这种靠折叠规则认的写法直接拒），两侧标识符都必须是
    目录里那对名字并且带引号；关系限定名不许重复，否则"这一列属于谁"没有唯一答案。
    """
    from_ = tree.args.get("from")
    base = from_.this if isinstance(from_, exp.From) else None
    if not isinstance(base, exp.Table) or (from_ is not None and from_.expressions):
        _reject("source_missing")                  # 没有 FROM 就不是对某张视图的聚合
    joins = list(tree.args.get("joins") or [])
    declared = [base] + [join.this for join in joins]
    relations: dict[str, _Relation] = {}
    for table in declared:
        if not isinstance(table, exp.Table):
            _reject("table_forbidden")             # 派生表或函数调用被当关系用
        relation = _resolve_relation(table, selection, index)
        if relation.qualifier in relations:
            _reject("join_invalid")                # 两个关系共用一个限定名：列归属有歧义
        relations[relation.qualifier] = relation
        usage.views.add(relation.view_ref)
    fact = relations[_qualifier(base)]
    used: set[str] = set()
    for join in joins:
        _require_join_form(join)
        right = relations[_qualifier(join.this)]
        edge = _require_edge(fact.view_ref, right.view_ref, join, relations,
                             selection, index, used)
        used.add(edge)
        usage.joins.add(edge)
    view = index.views[fact.view_ref]
    return _Skeleton(fact=fact,
                     authorization_field_ref=_authorization_field(view, index),
                     time_field_ref=_time_field(view, index),
                     relations=MappingProxyType(relations))


def _require_join_form(join: exp.Join) -> None:
    """方向与类型：只允许 `INNER JOIN`（含裸 `JOIN`）。

    LEFT/RIGHT/FULL 会改掉行数语义（基表行可以没有对侧行），不在计划批过的形状里；
    方向不限定之后，登记边那条防放大理由也不再成立。
    """
    if ((join.side or "").upper() not in PERMITTED_JOIN_SIDES
            or (join.kind or "").upper() not in PERMITTED_JOIN_KINDS):
        _reject("join_invalid")


def _resolve_relation(table: exp.Table, selection: SemanticSelection,
                      index: CatalogIndexes) -> _Relation:
    """`"schema"."view" [AS alias]` → `_Relation`；不在目录或不在本轮选择里就拒。"""
    if table.args.get("db") is None or table.args.get("catalog") is not None:
        # 没写 schema 就不猜 search_path；写满三段（database.schema.table）同理。
        _reject("table_forbidden")
    schema = _identifier(table.args["db"], quoted=True, reason="table_forbidden")
    name = _identifier(table.this, quoted=True, reason="table_forbidden")
    view_ref = next((ref for ref, view in index.views.items()
                     if (view.schema, view.name) == (schema, name)), None)
    if view_ref is None:
        # 底表 `bi.*`、`information_schema.*`、以及任何没登记的 reporting 视图都在这里
        # 拒掉：等数据库报"表不存在"就已经是执行之后了。
        _reject("table_forbidden")
    if view_ref not in selection.view_refs:
        _reject("table_forbidden")                 # 本轮检索没选它：不许自己扩范围
    return _Relation(view_ref=view_ref, qualifier=_qualifier(table))


def _qualifier(table: exp.Table) -> str:
    """这个关系在 SQL 里怎么被引用：有别名用别名，没别名用视图名。"""
    alias = table.args.get("alias")
    if alias is None:
        return _identifier(table.this, quoted=True, reason="identifier_invalid")
    if not isinstance(alias, exp.TableAlias) or alias.expressions:
        _reject("identifier_invalid")              # 列别名表不是这个模板的形状
    return _identifier(alias.this, quoted=None, reason="identifier_invalid")


def _require_edge(left_view_ref: str, right_view_ref: str, join: exp.Join,
                  relations: Mapping[str, _Relation], selection: SemanticSelection,
                  index: CatalogIndexes, used: set[str]) -> str:
    """JOIN 配对必须是目录登记过、本轮选中、并且 ON 用的就是那条边的两侧列。"""
    edges = [entry.ref for entry in index.joins.values()
             if (entry.left_view_ref, entry.right_view_ref) == (left_view_ref, right_view_ref)]
    if not edges:
        _reject("join_not_registered")
    selected = [ref for ref in edges if ref in selection.join_path_refs]
    if not selected:
        # 边存在但检索没选它：与编译器同一口径（Task 2 的 exploration_join_not_selected）。
        _reject("join_not_selected")
    if len(selected) != 1:
        # 同一对视图之间如果登记了两条边，选哪一条是新决策，不在这里猜。
        _reject("join_invalid")
    edge = selected[0]
    if edge in used:
        _reject("join_invalid")                    # 同一条边走第二遍就是自连接放大
    on = _unwrap_paren(join.args.get("on"))
    if not isinstance(on, exp.EQ):
        _reject("join_invalid")
    pair = {_on_side(on.this, relations, selection, index),
            _on_side(on.expression, relations, selection, index)}
    entry = index.joins[edge]
    if pair != {entry.left_field_ref, entry.right_field_ref}:
        # ON 的两列必须就是这条边的左右列：换键会悄悄改掉基数。
        _reject("join_invalid")
    return edge


def _on_side(node, relations: Mapping[str, _Relation], selection: SemanticSelection,
             index: CatalogIndexes) -> str:
    if not isinstance(node, exp.Column):
        _reject("join_invalid")
    return _column_ref(node, relations, selection, index)


def _authorization_field(view, index: CatalogIndexes) -> str:
    ref = view.authorization_field_ref
    authorization = index.fields.get(ref)
    if (authorization is None or authorization.role != "authorization"
            or authorization.view_ref != view.ref or ref not in view.field_refs):
        _reject("authorization_field_missing")
    return ref


def _time_field(view, index: CatalogIndexes) -> str:
    times = [ref for ref in view.field_refs if index.fields[ref].role == "time"]
    if not times:
        _reject("window_required")
    if len(times) > 1:
        _reject("time_field_ambiguous")            # 两种时间口径：选哪个都是猜
    return times[0]


# --- 列层：每个 Column 节点都要能追溯到目录与本轮选择 --------------------------

def _require_columns(nodes: Sequence[exp.Expression], skeleton: _Skeleton,
                     selection: SemanticSelection, index: CatalogIndexes,
                     usage: _Usage) -> None:
    for node in nodes:
        if isinstance(node, exp.Column):
            usage.fields.add(_column_ref(node, skeleton.relations, selection, index))


def _column_ref(column: exp.Column, relations: Mapping[str, _Relation],
                selection: SemanticSelection, index: CatalogIndexes) -> str:
    """限定名 + 列名 → 字段 ref；任何一环对不上都是"这句 SQL 用了没批的东西"。"""
    if column.args.get("table") is None or column.args.get("db") is not None:
        # 不限定的列靠 search_path 猜；三段名同理不在模板里。
        _reject("column_forbidden")
    qualifier = _bare(column.table, reason="identifier_invalid")
    relation = relations.get(qualifier)
    if relation is None:
        _reject("column_forbidden")                # 限定名没在 FROM/JOIN 里声明过
    name = _identifier(column.this, quoted=True, reason="column_forbidden")
    field_ref = next((ref for ref, entry in index.fields.items()
                      if (entry.view_ref, entry.column) == (relation.view_ref, name)), None)
    if field_ref is None or field_ref not in selection.field_refs:
        # 目录里没有这列、或者这列属于别的视图、或者本轮没选它：三种都是没批过的列。
        _reject("column_forbidden")
    return field_ref


# --- 谓词层：授权、半开窗口、limit ---------------------------------------------

def _require_predicates(tree: exp.Expression, nodes: Sequence[exp.Expression],
                        skeleton: _Skeleton, index: CatalogIndexes) -> None:
    """WHERE 必须恰好三条叶谓词，`LIMIT` 必须是那一个 NULL。

    授权谓词钉在目录给出的那一列上（库存池视图是 `pool_id`）；真值只能来自参数，所以
    `ANY` 的参数必须是 NULL 化后的占位符——`= ANY(ARRAY['S1'])` 这种自带集合的写法在
    结构上就不是"用服务端授权集合"。
    """
    where = tree.args.get("where")
    leaves = list(_conjunction(where.this)) if isinstance(where, exp.Where) else []
    authorization = index.fields[skeleton.authorization_field_ref]
    time_field = index.fields[skeleton.time_field_ref]
    if sum(1 for leaf in leaves
           if _is_authorization(leaf, skeleton, authorization)) != 1:
        _reject("authorization_predicate_missing")
    lower = [leaf for leaf in leaves if _is_window(leaf, skeleton, time_field, exp.GTE)]
    upper = [leaf for leaf in leaves if _is_window(leaf, skeleton, time_field, exp.LT)]
    if len(lower) != 1 or len(upper) != 1:
        # `[start, end)` 缺任何一边都没有分母：不许靠"扫全表"把窗口含混过去。
        _reject("window_predicate_missing")
    limit = tree.args.get("limit")
    if not isinstance(limit, exp.Limit) or not isinstance(limit.expression, exp.Null):
        _reject("limit_required")                  # 没有 LIMIT，或者 LIMIT 跟的是字面量
    if len(leaves) != 3:
        _reject("predicate_unsupported")           # 多出来的条件没有可核对的语义
    if sum(1 for node in nodes if isinstance(node, exp.Null)) != len(PLACEHOLDER_ORDER):
        # 四个参数位之外还有一个 NULL：那是第二条真值通道（`IS NULL`、写死的空集合）。
        _reject("null_literal_forbidden")


def _conjunction(node: exp.Expression) -> Iterator[exp.Expression]:
    if isinstance(node, exp.And):
        yield from _conjunction(node.this)
        yield from _conjunction(node.expression)
        return
    yield node


def _is_authorization(leaf: exp.Expression, skeleton: _Skeleton, authorization) -> bool:
    if not isinstance(leaf, exp.EQ) or not isinstance(leaf.this, exp.Column):
        return False
    if not _is_fact_column(leaf.this, skeleton, authorization.column):
        return False
    any_node = leaf.expression
    return isinstance(any_node, exp.Any) and isinstance(_unwrap_paren(any_node.this), exp.Null)


def _is_window(leaf: exp.Expression, skeleton: _Skeleton, time_field, kind) -> bool:
    return (type(leaf) is kind
            and isinstance(leaf.this, exp.Column)
            and _is_fact_column(leaf.this, skeleton, time_field.column)
            and isinstance(leaf.expression, exp.Null))


def _is_fact_column(column: exp.Column, skeleton: _Skeleton, column_name: str) -> bool:
    return (column.name == column_name and column.table == skeleton.fact.qualifier
            and _quoted(column.this) and column.args.get("table") is not None
            and column.args.get("db") is None)


def _unwrap_paren(node: exp.Expression | None) -> exp.Expression | None:
    while isinstance(node, exp.Paren):
        node = node.this
    return node


# --- 参数层：key、位置、取值与授权集合 ------------------------------------------

def _require_parameters(draft: SqlDraft, fragments: Sequence[str],
                        names: tuple[str, ...], context: DomainContext) -> list[str]:
    """四个具名参数：名字、顺序、所在谓词位置与取值一起判，并且必须等于服务端授权集合。

    名字与位置分开判，因为两者会各自出错：换名字（`%(shop_ids)s`）是越权声明；换位置
    （`ANY(%(limit)s)` 配 `LIMIT %(allowed_shop_ids)s`）在 NULL 化之后形状完全一样，
    只有把名字钉回它所在的片段才能发现。
    """
    if sorted(draft.parameters) != sorted(PARAMETER_KEYS):
        _reject("parameter_mismatch")
    if names != PLACEHOLDER_ORDER:
        _reject("parameter_mismatch")
    for position, (fragment, name) in enumerate(zip(fragments, names)):
        if PLACEHOLDER_SITES[position].search(fragment) is None:
            _reject("parameter_mismatch")
    scope = _scope_values(draft.parameters["allowed_shop_ids"])
    _window_values(draft.parameters["start"], draft.parameters["end"])
    _limit_value(draft.parameters["limit"])
    if scope != sorted(_context_scope(context)):
        # 参数里的集合与服务端授权集合不等：要么越权，要么这条计划根本不该发。
        _reject("scope_mismatch")
    return scope


def _scope_values(value) -> list[str]:
    """参数里的授权集合：list、非空白字符串、已排序、去重、非空。

    排序与去重在这里再判一次（编译器已经保证了）：同一份授权集合必须编出逐字相同的 SQL
    与同一个指纹，否则 `[S2,S1]` 与 `[S1,S2]` 会在诊断里成为两条语句。
    """
    if not isinstance(value, list) or not value:
        _reject("parameter_invalid")
    if not all(isinstance(item, str) and item.strip() for item in value):
        _reject("parameter_invalid")
    if list(value) != sorted(set(value)):
        _reject("parameter_invalid")
    return list(value)


def _window_values(start, end) -> None:
    if type(start) is not date or type(end) is not date:
        # `datetime` 与文本日期都不收：窗口是日期半开区间，类型本身就是口径。
        _reject("parameter_invalid")
    if not start < end or (end - start).days > MAX_WINDOW_DAYS:
        # 366 天上界的唯一真源是 `ExplorationRequest`（Task 1）：这里只复用常数。
        _reject("parameter_invalid")


def _limit_value(limit) -> None:
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_ROWS:
        _reject("parameter_invalid")


def _context_scope(context: DomainContext) -> frozenset[str]:
    """服务端授权集合的形状与编译器同一口径（frozenset/set、非空白、非空）。"""
    allowed = context.allowed_shop_ids
    if isinstance(allowed, (str, bytes)) or not isinstance(allowed, (frozenset, set)):
        _reject("scope_invalid")                   # 字符串也是可迭代的：不能当集合看
    if not all(isinstance(shop_id, str) and shop_id.strip() for shop_id in allowed):
        _reject("scope_invalid")
    if not allowed:
        _reject("scope_empty")                     # 空集是"本轮没有授权"，不是"查不到东西"
    return frozenset(allowed)


# --- 输出列层：别名必须绑回一个已选 ref -----------------------------------------

def _require_output_columns(tree: exp.Expression, skeleton: _Skeleton, selected_refs,
                            selection: SemanticSelection, index: CatalogIndexes,
                            usage: _Usage) -> None:
    """每个输出列要么是一个已选指标的聚合，要么是一个已选维度的裸列。

    别名不是显示名：它把这一列钉回稳定 ref，投影层（Task 4）据此发列。`sum(...) AS
    "total_cost"`（没有这个 ref）、`avg(cost_total) AS metric_cost_total`（该指标只允许
    sum）、`sum(sales_amount) AS metric_cost_total`（该指标要的是另一列）都在这里拒，
    而不是等用户拿到一个名字看起来正常的错数。
    """
    alias_map = _alias_map(selected_refs, index)
    grouped: list[str] = []
    aggregates = 0
    for item in tree.expressions:
        if not isinstance(item, exp.Alias):
            _reject("alias_unbound")               # 没有别名就没有 ref，也就没有列身份
        ref = alias_map.get(_identifier(item.args.get("alias"), quoted=True,
                                        reason="identifier_unquoted"))
        if ref is None:
            _reject("alias_unbound")
        if ref in index.metrics:
            aggregates += 1
            _require_metric_column(item.this, ref, skeleton, selection, index, usage)
        elif ref in index.fields:
            _require_group_column(item.this, ref, skeleton, selection, index, usage,
                                  grouped)
        else:
            _reject("alias_unbound")               # view/join/entity ref 不能当输出列
    if not aggregates:
        _reject("aggregate_required")              # 只回明细不是受控聚合探索
    _require_grouping(tree, grouped)


def _alias_map(selected_refs, index: CatalogIndexes) -> dict[str, str]:
    """`SQL 别名 → 稳定 ref`：与编译器同一套推导（授权列按 `_列名`，其余按 ref 下划线化）。"""
    mapping: dict[str, str] = {}
    for ref in selected_refs:
        entry = index.fields.get(ref)
        if entry is not None:
            alias = (f"_{entry.column}" if entry.role == "authorization"
                     else ref.replace("-", "_"))
        elif ref in index.metrics:
            alias = ref.replace("-", "_")
        else:
            continue
        mapping[alias] = ref
    return mapping


def _require_metric_column(expression, metric_ref: str, skeleton: _Skeleton,
                           selection: SemanticSelection, index: CatalogIndexes,
                           usage: _Usage) -> None:
    """聚合列必须落在该指标唯一的那个必需字段上，并且用该指标允许的聚合函数。"""
    if type(expression) not in AGGREGATE_NAMES:
        _reject("aggregate_required")
    if metric_ref not in selection.metric_refs:
        _reject("ref_not_selected")
    metric = index.metrics[metric_ref]
    usage.metrics.add(metric_ref)
    if len(metric.required_field_refs) != 1:
        # 比值与毛利参考是"多个数各自求和再相除"，一个聚合发不出来（与 Task 2 同一口径）。
        _reject("metric_field_ambiguous")
    inner = expression.this
    if not isinstance(inner, exp.Column):
        _reject("column_forbidden")
    field_ref = _column_ref(inner, skeleton.relations, selection, index)
    if field_ref != metric.required_field_refs[0]:
        _reject("metric_field_mismatch")
    if AGGREGATE_NAMES[type(expression)] not in metric.allowed_aggregates:
        _reject("aggregate_not_permitted")
    usage.fields.add(field_ref)


def _require_group_column(expression, field_ref: str, skeleton: _Skeleton,
                          selection: SemanticSelection, index: CatalogIndexes,
                          usage: _Usage, grouped: list[str]) -> None:
    """维度列必须是可分组的角色，并且这一列在 GROUP BY / ORDER BY 里都出现。"""
    entry = index.fields[field_ref]
    if entry.role not in GROUPABLE_ROLES:
        # `measure` 不是维度，`internal` 是 ERP 主键：目录明确禁止把它们当分组维度外露。
        _reject("group_not_permitted")
    if not isinstance(expression, exp.Column):
        _reject("column_forbidden")
    if _column_ref(expression, skeleton.relations, selection, index) != field_ref:
        # 别名说这是 A 列，表达式里却是 B 列：投影层会按 A 的名字发 B 的数。
        _reject("alias_unbound")
    usage.fields.add(field_ref)
    grouped.append(expression.sql(dialect=PARSER_DIALECT))
    if len(grouped) != len(set(grouped)):
        _reject("alias_unbound")                   # 同一维度出现两次就是重复列


def _require_grouping(tree: exp.Expression, grouped: list[str]) -> None:
    """GROUP BY 与 ORDER BY 必须恰好是那批维度列：没有排序旋钮，也没有隐藏维度。"""
    keys = _plain_columns(tree.args.get("group"))
    order = _plain_columns(tree.args.get("order"))
    if sorted(keys) != sorted(grouped) or sorted(order) != sorted(grouped):
        _reject("group_mismatch")


def _plain_columns(node: exp.Expression | None) -> list[str]:
    """GROUP BY / ORDER BY 的每一项都必须是一条裸列：位序引用与排序修饰都不是模板。"""
    if node is None:
        return []
    texts: list[str] = []
    for item in node.expressions:
        column = item.this if isinstance(item, exp.Ordered) else item
        if isinstance(item, exp.Ordered) and any(item.args.get(key)
                                                 for key in ORDER_MODIFIER_ARGS):
            # 排序方向与 NULL 位置由模板定：`DESC`/`NULLS FIRST`/`WITH FILL` 都不是模板能发的
            # 形状。键名必须取自 `exp.Ordered.arg_types`：`"nulls"` 这个键根本不存在，拿
            # `args.get("nulls") is not None` 判是一条恒假分句（`NULLS FIRST` 就是这样溜过去
            # 的）；而 `nulls_first` 在本方言里总存在（裸列与 `ASC`/`NULLS LAST` 都是 False），
            # 所以只能按真值判，否则每一条编译器输出的 ORDER BY 都会被误拒。
            _reject("expression_forbidden")
        if not isinstance(column, exp.Column):
            _reject("expression_forbidden")        # `ORDER BY 2` 之类的位置引用
        texts.append(column.sql(dialect=PARSER_DIALECT))
    return texts


# --- 白名单收口与 ref 闭环 ------------------------------------------------------

def _require_allowed_nodes(nodes: Sequence[exp.Expression]) -> None:
    """落在封闭节点清单外的任何东西都拒：这是白名单，不是黑名单的最后一道。"""
    for node in nodes:
        if type(node) not in ALLOWED_NODES:
            _reject("expression_forbidden")


def _require_ref_closure(selected_refs, usage: _Usage,
                         selection: SemanticSelection) -> None:
    """`draft.selected_refs` 必须逐字等于这句 SQL 的 ref 闭环。

    多一项（这句 SQL 根本没用到它）、少一项（用了却没记）、重复、没排序，都让"这条
    fingerprint 对应哪些语义 ref"失去唯一答案——而 Task 5 的 Artifact 与 Task 6 的审计
    就靠这份清单。
    """
    available = (set(selection.entity_refs) | set(selection.metric_refs)
                 | set(selection.view_refs) | set(selection.field_refs)
                 | set(selection.join_path_refs))
    for ref in selected_refs:
        if ref not in available:
            _reject("ref_not_selected")
    if list(selected_refs) != sorted(usage.refs):
        _reject("ref_unbound")


# --- 计划与指纹 ----------------------------------------------------------------

def _plan(draft: SqlDraft, selection: SemanticSelection, scope: list[str],
          names: tuple[str, ...]) -> ValidatedQueryPlan:
    """组装唯一可被执行的计划对象。

    `estimated_rows` / `estimated_total_cost` 故意留空：那是 Task 4 `EXPLAIN` 的产物，
    不允许由策略填一个看起来安全的数。
    """
    parameters = draft.parameters
    return ValidatedQueryPlan(
        template_version=EXPLORATION_TEMPLATE_VERSION,
        catalog_version=selection.catalog_version,
        statement_fingerprint=_fingerprint(draft.sql_text, names, draft.selected_refs,
                                           scope, selection.catalog_version),
        sql_text=draft.sql_text,
        # 逐项复制：计划对象与草案不共享可变容器，事后改草案不得改已发出的计划。
        parameters={"allowed_shop_ids": list(scope), "end": parameters["end"],
                    "limit": parameters["limit"], "start": parameters["start"]},
        selected_refs=list(draft.selected_refs),
    )


def _fingerprint(sql_text: str, parameter_names: tuple[str, ...], selected_refs,
                 scope: Sequence[str], catalog_version: str) -> str:
    """计划 Task 3 Step 4 那份 JSON 的 SHA-256。

    五项输入：规范化 SQL、排序参数名、selection refs、`context.allowed_shop_ids` 的
    SHA-256、语义目录版本。SQL 用**原文**（NULL 化只为解析），所以指纹描述的正是将要
    执行的那句话；授权集合只以摘要出现，真店号不进指纹输入以外的任何持久化载荷。

    这是**语句**身份而不是**调用**身份：同一形状换窗口还是同一个指纹（参数值另记在
    诊断里），但换授权集合、换 ref 清单、换目录版本都必须换指纹——否则一份窄授权计划
    可以被当成宽授权计划复用。
    """
    digest = hashlib.sha256(json.dumps(sorted(scope), separators=(",", ":"))
                            .encode("utf-8")).hexdigest()
    payload = {"normalized_sql": " ".join(sql_text.split()),
               "parameter_names": sorted(parameter_names),
               "selected_refs": list(selected_refs),
               "scope_fingerprint": digest,
               "catalog_version": catalog_version}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"))
                          .encode("utf-8")).hexdigest()


# --- 标识符与拒绝 --------------------------------------------------------------

def _identifier(node, *, quoted: bool | None, reason: str) -> str:
    """取一个标识符文本：引号要求与 `SQL_IDENTIFIER_RE` 一起判。

    `quoted=True`：目录解出来的 schema/view/column 名必须带引号（编译器逐段包）。不带
    引号的写法会让 PostgreSQL 按折叠规则改名，那已经不是目录里那个对象了。
    `quoted=None`：关系别名两种都行（模板里 `AS fact` 不带引号）。
    """
    if not isinstance(node, exp.Identifier):
        _reject(reason)
    if quoted is not None and node.quoted is not quoted:
        _reject("identifier_unquoted")
    text = node.name
    if not isinstance(text, str) or re.fullmatch(SQL_IDENTIFIER_RE, text) is None:
        _reject("identifier_invalid")
    return text


def _bare(text: str, *, reason: str) -> str:
    """限定名（别名或视图名）：只做形状检查，它已经来自 `_qualifier`。"""
    if re.fullmatch(SQL_IDENTIFIER_RE, text or "") is None:
        _reject(reason)
    return text


def _quoted(node: exp.Expression) -> bool:
    return isinstance(node, exp.Identifier) and node.quoted


def _reject(reason: str) -> NoReturn:
    """只抛稳定原因码：不回显 SQL、ref、店号或问题原文。"""
    raise ValueError(POLICY_CODE_PREFIX + reason)
