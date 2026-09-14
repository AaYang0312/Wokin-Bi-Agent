"""语义目录契约：模型只见稳定 ref，SQL 标识符留在服务端注册表里。

这套类型故意收得很窄。理由不是审美：下游会把它们编译成只读 SQL，一旦放过
`one_to_many` 边或原始 `schema.table`，同一个问题就能在 JOIN 放大之后发出双倍
金额，而结果看起来完全正常——数字对、口径对、就是错了。

因此三条规则在这层一次定死：
1. ref 是 kebab-case 稳定标识（`_require_ref` 一处定义，所有 `*ref`/`*refs` 共用）；
2. 词表是封闭的 Literal（数据类型、字段角色、基数、聚合函数、`schema` 只能是 reporting）；
3. 集合是元组、项非空白、不重复——冻结契约里不许夹一个可变 list。

违反任何一条都抛 `pydantic.ValidationError`（契约类型是 stdlib 冻结 dataclass，
但报错形状与 `VersionSet` 一致，调用方只需认一种异常）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Any, Callable, Literal

from pydantic import AfterValidator, Field, TypeAdapter, ValidationError
from pydantic_core import InitErrorDetails, PydanticCustomError

from bi_agent.runtime.versions import check_version_identifier

# 一个 ref 的合法形状。下划线不是合法字符：SQL 列名走 `column`，不冒充 ref。
REF_RE = r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$"
# 视图名/列名：必须是裸标识符，带点号或空白的原始 SQL 名一律拒绝。
SQL_IDENTIFIER_RE = r"^[a-z_][a-z0-9_]*$"
# 概念码（domain、缺失概念）允许下划线：`profit_grain` 是码，不是可解析的 ref。
STABLE_CODE_RE = r"^[a-z][a-z0-9_]*$"

DataType = Literal["date", "datetime", "decimal", "integer", "boolean", "text", "ref"]
FieldRole = Literal["dimension", "measure", "time", "authorization", "internal"]
Cardinality = Literal["one_to_one", "many_to_one"]
Aggregate = Literal["sum", "count", "avg", "min", "max"]

_CONTRACT_VIOLATION = "semantic_contract_violation"

_REF = TypeAdapter(Annotated[str, Field(pattern=REF_RE)])
_SQL_IDENTIFIER = TypeAdapter(Annotated[str, Field(pattern=SQL_IDENTIFIER_RE)])
_STABLE_CODE = TypeAdapter(Annotated[str, Field(pattern=STABLE_CODE_RE)])
_VERSION = TypeAdapter(Annotated[str, AfterValidator(check_version_identifier)])
_DATA_TYPE = TypeAdapter(DataType)
_FIELD_ROLE = TypeAdapter(FieldRole)
_CARDINALITY = TypeAdapter(Cardinality)
_AGGREGATE = TypeAdapter(Aggregate)
_SCHEMA_NAME = TypeAdapter(Literal["reporting"])


def _describe(exc: ValueError) -> str:
    """取一条人能读的原因；pydantic 的 ValidationError 也走这里。"""
    errors = getattr(exc, "errors", None)
    if callable(errors):
        return str(errors(include_url=False)[0]["msg"])
    return str(exc)


def _violation(of: str, where: str, value: object, cause: ValueError) -> ValidationError:
    """把失败定位到具体字段：`SemanticField.ref` 而不是"某个地方不对"。"""
    return ValidationError.from_exception_data(
        title=of,
        line_errors=[InitErrorDetails(
            type=PydanticCustomError(
                _CONTRACT_VIOLATION, "{detail}", {"detail": _describe(cause)}),
            loc=(where,),
            input=value,
        )],
    )


def _validate(of: str,
              checks: tuple[tuple[str, Callable[[Any], Any], object], ...]) -> None:
    """按声明顺序跑检查：词表先于跨字段规则，报错才有稳定优先级。"""
    for where, check, value in checks:
        try:
            check(value)
        except ValueError as exc:      # ValidationError 也是 ValueError
            raise _violation(of, where, value, exc) from None


# --- 逐项规则：每个概念只有一种拼法，所有 ref 共用 `_require_ref` ---------------

def _require_ref(value: object) -> None:
    """唯一的 ref 规则：稳定 kebab-case；自由文本、原始 SQL 名、非字符串都拒绝。"""
    _REF.validate_python(value)


def _require_tuple(value: object) -> tuple[Any, ...]:
    # 冻结契约里不许夹可变容器：形状只能是 tuple，否则"不可变"是假的。
    if not isinstance(value, tuple):
        raise ValueError("not_a_tuple")
    return value


def _require_unique(values: tuple[Any, ...], what: str) -> None:
    # 重复项会让按 ref 建的索引静默覆盖前者：宁可当场拒绝。
    if len(set(values)) != len(values):
        raise ValueError(f"duplicate_{what}")


def _require_refs(value: object) -> None:
    """ref 集合：逐个跑同一条 ref 规则，再要求不重复。"""
    values = _require_tuple(value)
    for item in values:
        _require_ref(item)
    _require_unique(values, "ref")


def _require_terms(value: object) -> tuple[Any, ...]:
    """自然语言词表（别名）与维度名（grain）：项非空白、不重复。"""
    values = _require_tuple(value)
    for item in values:
        if not isinstance(item, str) or not item.strip():
            raise ValueError("blank_term")
    _require_unique(values, "term")
    return values


def _require_codes(value: object) -> tuple[Any, ...]:
    """码值集合：非空白、不重复，且必须是稳定的小写码。"""
    values = _require_terms(value)
    for item in values:
        _STABLE_CODE.validate_python(item)
    return values


def _require_domains(value: object) -> None:
    """domain 为空的条目永远检索不到，是静默失效的死目录项：直接拒绝。"""
    values = _require_codes(value)
    if not values:
        raise ValueError("empty_domains")


def _require_aggregates(value: object) -> None:
    values = _require_tuple(value)
    for item in values:
        _AGGREGATE.validate_python(item)
    _require_unique(values, "aggregate")


def _require_member(container: tuple[Any, ...]) -> Callable[[Any], None]:
    """跨字段规则工厂：默认值必须是已允许的取值之一。"""
    def check(value: object) -> None:
        if value not in container:
            raise ValueError("value_not_in_allowed_set")
    return check


def _require_text(value: object) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("blank_text")


def _require_bool(value: object) -> None:
    # `bool("false")` 是真：文本真值不能当开关用。
    if not isinstance(value, bool):
        raise ValueError("not_a_bool")


def _require_items(value: object) -> None:
    """目录集合：元组、项必须是带 ref 的契约对象、ref 不重复。"""
    values = _require_tuple(value)
    refs = []
    for item in values:
        ref = getattr(item, "ref", None)
        if not isinstance(ref, str):
            raise ValueError("item_without_ref")
        refs.append(ref)
    _require_unique(tuple(refs), "item_ref")


# --- 契约类型 -------------------------------------------------------------------

@dataclass(frozen=True)
class SemanticEntity:
    """一个业务实体（店铺、商品、SKU、库存池……）：只带 ref 与叫法，不带表名。"""

    ref: str
    domains: tuple[str, ...]
    aliases: tuple[str, ...]
    description: str

    def __post_init__(self) -> None:
        _validate(type(self).__name__, (
            ("ref", _require_ref, self.ref),
            ("domains", _require_domains, self.domains),
            ("aliases", _require_terms, self.aliases),
            ("description", _require_text, self.description),
        ))


@dataclass(frozen=True)
class SemanticField:
    """一个已批准视图里的列。`column` 只在服务端解析成标识符时用，不进模型载荷。"""

    ref: str
    view_ref: str
    column: str
    data_type: DataType
    role: FieldRole
    aliases: tuple[str, ...]

    def __post_init__(self) -> None:
        _validate(type(self).__name__, (
            ("ref", _require_ref, self.ref),
            ("view_ref", _require_ref, self.view_ref),
            ("column", _SQL_IDENTIFIER.validate_python, self.column),
            ("data_type", _DATA_TYPE.validate_python, self.data_type),
            ("role", _FIELD_ROLE.validate_python, self.role),
            ("aliases", _require_terms, self.aliases),
        ))


@dataclass(frozen=True)
class SemanticMetric:
    """一个指标的语义条目：需要哪些字段、允许怎么聚合、是否必须声明口径。"""

    ref: str
    domains: tuple[str, ...]
    aliases: tuple[str, ...]
    required_field_refs: tuple[str, ...]
    allowed_aggregates: tuple[Aggregate, ...]
    default_aggregate: Aggregate
    basis_required: bool

    def __post_init__(self) -> None:
        _validate(type(self).__name__, (
            ("ref", _require_ref, self.ref),
            ("domains", _require_domains, self.domains),
            ("aliases", _require_terms, self.aliases),
            ("required_field_refs", _require_refs, self.required_field_refs),
            ("allowed_aggregates", _require_aggregates, self.allowed_aggregates),
            # 词表过了再判归属：`median` 的错要报"不是合法聚合"，不是"不在集合里"。
            ("default_aggregate", _AGGREGATE.validate_python, self.default_aggregate),
            ("default_aggregate",
             _require_member(self.allowed_aggregates), self.default_aggregate),
            ("basis_required", _require_bool, self.basis_required),
        ))


@dataclass(frozen=True)
class SemanticView:
    """一个获准的 reporting 视图：粒度、授权字段与它对外开放的字段集合。"""

    ref: str
    schema: Literal["reporting"]
    name: str
    entity_ref: str
    grain: tuple[str, ...]
    authorization_field_ref: str
    field_refs: tuple[str, ...]
    domains: tuple[str, ...]

    def __post_init__(self) -> None:
        _validate(type(self).__name__, (
            ("ref", _require_ref, self.ref),
            ("schema", _SCHEMA_NAME.validate_python, self.schema),
            ("name", _SQL_IDENTIFIER.validate_python, self.name),
            ("entity_ref", _require_ref, self.entity_ref),
            ("grain", _require_terms, self.grain),
            ("authorization_field_ref", _require_ref, self.authorization_field_ref),
            ("field_refs", _require_refs, self.field_refs),
            ("domains", _require_domains, self.domains),
        ))


@dataclass(frozen=True)
class SemanticJoin:
    """一条合法 JOIN 边。基数只准 1:1 / N:1：放大金额的边根本不进词表。"""

    ref: str
    left_view_ref: str
    right_view_ref: str
    left_field_ref: str
    right_field_ref: str
    cardinality: Cardinality
    allowed_group_grains: tuple[str, ...]
    anti_amplification: str

    def __post_init__(self) -> None:
        _validate(type(self).__name__, (
            ("ref", _require_ref, self.ref),
            ("left_view_ref", _require_ref, self.left_view_ref),
            ("right_view_ref", _require_ref, self.right_view_ref),
            ("left_field_ref", _require_ref, self.left_field_ref),
            ("right_field_ref", _require_ref, self.right_field_ref),
            ("cardinality", _CARDINALITY.validate_python, self.cardinality),
            ("allowed_group_grains", _require_terms, self.allowed_group_grains),
            ("anti_amplification", _require_text, self.anti_amplification),
        ))


@dataclass(frozen=True)
class SemanticCatalog:
    """一个版本下的完整目录快照：换版本必须新建实例，不在原对象上改。"""

    version: str
    entities: tuple[SemanticEntity, ...]
    fields: tuple[SemanticField, ...]
    metrics: tuple[SemanticMetric, ...]
    views: tuple[SemanticView, ...]
    joins: tuple[SemanticJoin, ...]

    def __post_init__(self) -> None:
        _validate(type(self).__name__, (
            ("version", _VERSION.validate_python, self.version),
            ("entities", _require_items, self.entities),
            ("fields", _require_items, self.fields),
            ("metrics", _require_items, self.metrics),
            ("views", _require_items, self.views),
            ("joins", _require_items, self.joins),
        ))


@dataclass(frozen=True)
class SemanticSelection:
    """检索结果：只有 ref、缺失概念与是否需要澄清，没有任何 SQL 标识符。"""

    catalog_version: str
    entity_refs: tuple[str, ...]
    metric_refs: tuple[str, ...]
    view_refs: tuple[str, ...]
    field_refs: tuple[str, ...]
    join_path_refs: tuple[str, ...]
    missing_concepts: tuple[str, ...]
    requires_clarification: bool

    def __post_init__(self) -> None:
        _validate(type(self).__name__, (
            ("catalog_version", _VERSION.validate_python, self.catalog_version),
            ("entity_refs", _require_refs, self.entity_refs),
            ("metric_refs", _require_refs, self.metric_refs),
            ("view_refs", _require_refs, self.view_refs),
            ("field_refs", _require_refs, self.field_refs),
            ("join_path_refs", _require_refs, self.join_path_refs),
            ("missing_concepts", _require_codes, self.missing_concepts),
            ("requires_clarification", _require_bool, self.requires_clarification),
        ))
