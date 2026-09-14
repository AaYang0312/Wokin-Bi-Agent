"""安全投影：内部列换成 opaque 引用、值按目录声明类型出、公开载荷有字节上限（Task 4 Step 5）。

这一层是 SQL 结果变成"可以发给模型、可以写进公开 Artifact"的那道唯一关口。五条不可让：

1. **列身份只能来自目录。** 公开列必须是已发布的 field / metric ref：解得出 ref 才谈得上
   口径。`view-` / `entity-` / `join-` 这些"目录里有但根本不是列"的 ref 与完全没注册的
   写法分开报码。重复列一律拒：两列同名会让后一列静默覆盖前一列，行看起来还是那一行的
   宽度，数字却少了一个维度。
2. **原始主键没有对外通道。** 以下划线开头的列里只有 `_shop_id`（编译器给授权列的那个
   别名）被认，而且必须能在传进来的店铺引用表里查到，查到之后列名换成 `shop-ref`、值
   换成 opaque 引用。`_pool_id`、`_erp_id`、`_product_id` 与其余任何 `_` 前缀列一律拒；
   `authorization` / `internal` 角色的字段就算不带下划线也不许作为公开列出——那是 ERP 主键
   的另一条通道（目录登记它们只为让粒度可解析）。
3. **值的类型表是封闭的。** 允许进来的只有 `None/bool/int/Decimal/date/datetime/str`，
   出去的是 `None/bool/int/str` 四种 JSON 安全值：`Decimal` 出普通十进制文本并**保留标度**
   （`12.30` 不写成 `12.3`，那是数据库列的口径），带时区的 `datetime` 出 ISO 且**不换算
   时区**，`date` 出 ISO。其余都拒：`float`（含 NaN/Infinity 的来源）、bytes、list、dict、
   裸日期与时刻串位、不带时区的时刻、非整数的 `Decimal` 进整数列、布尔冒充整数
   （`isinstance(True, int)` 为真，所以按类型逐项判而不是靠数字家族兜）。
   空值是唯一不需要证据的值：一列全为 `None` 仍然按目录声明发类型。
4. **公开载荷有预算。** 行数按 `MAX_ROWS`（与请求侧同一个数）判，字节数按投影**之后**的
   紧凑 JSON（`ensure_ascii=False, separators=(",", ":")`，UTF-8）判 `MAX_RESULT_BYTES`。
   判安全表示而不是判数据库回的那份原始行：原始行里还有真店号，按它算预算就等于逼着
   这一层去留下或搬运那份表示。字节数在列身份与值都定下来之后才判，所以拒掉的结果里
   不会有任何一条真店号行被带走。
5. **错误只有稳定原因码。** 不回显列名、ref、店号、值文本或长度：这一层的入参正是
   未经清洗的数据库行，把任何一个字面量抄进消息都会让它一路进日志与模型载荷。

调用序列（Task 5 按此接线）：执行层给 `(columns, rows)`，本层给
`(list[ExplorationColumn], list[dict[str, object]])`；本层不打开事务、不发语句、不读时钟，
所以没有任何一条结果会在这里被"再看一眼"。
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import NoReturn

from bi_agent.exploration.compiler import INTERNAL_ALIAS_PREFIX
from bi_agent.exploration.models import (
    MAX_RESULT_BYTES,
    MAX_ROWS,
    MAX_TEXT_CHARS,
    SHOP_ID_COLUMN,
    SHOP_REF_COLUMN,
    ExplorationColumn,
)
from bi_agent.semantic_catalog.models import DataType, _require_ref
from bi_agent.semantic_catalog.registry import CATALOG, catalog_indexes

# 拒绝只有一个前缀：`_reject(原因)` 给 `ValueError("exploration_" + 原因)`，不回显任何入参。
CODE_PREFIX = "exploration_"
# 能对外发的字段角色。`authorization` 与 `internal` 都是 ERP 主键，`measure` 不是维度：
# 未聚合的度量列出现在结果里，说明这句话根本没按模板聚合过。
PUBLIC_FIELD_ROLES = frozenset({"dimension", "time"})
RAW_IDENTIFIER_ROLES = frozenset({"authorization", "internal"})
# 投影层能表示的类型词表：`ref` 只由 `shop-ref` 那一列发，不在公开字段的声明里。
PROJECTABLE_TYPES = frozenset({"date", "datetime", "decimal", "integer", "boolean", "text"})
SHOP_REF_TYPE: DataType = "ref"
# 唯一被认的内部列：编译器给店铺授权列的输出别名。
INTERNAL_COLUMNS = frozenset({SHOP_ID_COLUMN})


@dataclass(frozen=True)
class _Column:
    """一列的对外身份：列名（稳定 ref 或 `shop-ref`）与它按目录声明的类型。"""

    key: str
    data_type: DataType


def project_result(columns: Sequence[str], rows: "Sequence[Sequence[object]]", *,
                   shop_refs: Mapping[str, str]) -> tuple[list[ExplorationColumn], list[dict[str, object]]]:
    """把执行层给的列身份与原始行换成可以公开的结果。

    `shop_refs` 是服务端的"真店铺主键 → opaque 引用"表（`DomainContext.shop_refs`）：
    `_shop_id` 的每一个取值都必须在里面查得到，查不到就整条结果拒——把没登记过的店号
    发出去，或者干脆省掉这一列，都比"少一家店但形状正常"更安全。
    """
    declared = _declarations(columns)
    scope = _reference_table(shop_refs)
    _require_rows_shape(rows, len(declared))
    if len(rows) > MAX_ROWS:
        # 与请求侧同一个行数预算：这里再判一次，是因为本层不能只信上游取了多少行。
        _reject("row_limit_exceeded")
    keys = [column.key for column in declared]
    projected = [dict(zip(keys, (_value(column, value, scope)
                                 for column, value in zip(declared, row))))
                 for row in rows]
    published = [ExplorationColumn(ref=column.key, data_type=column.data_type)
                 for column in declared]
    _require_within_result_budget(published, projected)
    return published, projected


# --- 列身份 -----------------------------------------------------------------------

def _declarations(columns) -> list[_Column]:
    """按声明顺序解出每一列的身份；空结果与重复列都在这里拦。"""
    items = _text_sequence(columns, "columns_contract_required")
    if not items:
        _reject("column_mismatch")
    declared: list[_Column] = []
    for name in items:
        entry = _declaration(name)
        if any(known.key == entry.key for known in declared):
            _reject("column_duplicate")
        declared.append(entry)
    return declared


def _declaration(name: str) -> _Column:
    index = catalog_indexes(CATALOG)
    if name in INTERNAL_COLUMNS:
        return _Column(SHOP_REF_COLUMN, SHOP_REF_TYPE)
    if name.startswith(INTERNAL_ALIAS_PREFIX):
        _reject("internal_column_forbidden")
    field = index.fields.get(name)
    if field is not None:
        if field.role not in PUBLIC_FIELD_ROLES or field.data_type not in PROJECTABLE_TYPES:
            _reject("column_not_public")
        return _Column(name, field.data_type)
    metric = index.metrics.get(name)
    if metric is not None:
        if len(metric.required_field_refs) != 1:
            # 比与毛利参考是"几个数各自求和再相除"，一列发不出那个口径。
            _reject("column_not_public")
        required = index.fields[metric.required_field_refs[0]]
        if required.role in RAW_IDENTIFIER_ROLES or required.data_type not in PROJECTABLE_TYPES:
            _reject("column_not_public")
        return _Column(name, required.data_type)
    if name in index.views or name in index.entities or name in index.joins:
        _reject("column_not_public")                        # 目录里有，但它不是列
    _reject("ref_unregistered")


# --- 店铺引用表 -------------------------------------------------------------------

def _reference_table(shop_refs) -> dict[str, str]:
    """`真店号 → opaque 引用`：形状不合法是编程错误，取值不合法是稳定原因码。"""
    if not isinstance(shop_refs, Mapping):
        raise TypeError(CODE_PREFIX + "shop_refs_contract_required")
    table: dict[str, str] = {}
    for shop_id, ref in shop_refs.items():
        if (not isinstance(shop_id, str) or not shop_id.strip() or shop_id != shop_id.strip()
                or not isinstance(ref, str)):
            _reject("shop_refs_invalid")
        try:
            _require_ref(ref)
        except ValueError:
            # 引用表里出现不是 ref 的字面量：那多半就是被抄进来的主键。
            _reject("shop_refs_invalid")
        table[shop_id] = ref
    return table


# --- 行形状与值 -------------------------------------------------------------------

def _require_rows_shape(rows, width: int) -> None:
    if isinstance(rows, (str, bytes)) or not isinstance(rows, (list, tuple)):
        raise TypeError(CODE_PREFIX + "rows_contract_required")
    for row in rows:
        if isinstance(row, (str, bytes)) or not isinstance(row, (list, tuple)):
            raise TypeError(CODE_PREFIX + "rows_contract_required")
    if any(len(row) != width for row in rows):
        _reject("column_mismatch")                           # 列数（宽度）对不上


def _value(column: _Column, value: object, scope: Mapping[str, str]) -> object:
    if column.data_type == SHOP_REF_TYPE:
        return _shop_ref(value, scope)
    declared = column.data_type
    if value is None:
        return None
    if declared == "boolean":
        if not isinstance(value, bool):
            _reject("value_type_unsupported")
        return value
    if declared == "integer":
        return _integer(value)
    if declared == "decimal":
        return _decimal(value)
    if declared == "date":
        if type(value) is not date:                          # `datetime` 是 `date` 的子类
            _reject("value_type_unsupported")
        return value.isoformat()
    if declared == "datetime":
        return _moment(value)
    return _text(value, scope)


def _shop_ref(value: object, scope: Mapping[str, str]) -> str:
    if not isinstance(value, str) or value not in scope:
        _reject("shop_not_registered")
    return scope[value]


def _integer(value: object) -> int:
    if isinstance(value, bool):
        _reject("value_type_unsupported")
    if isinstance(value, int):
        return value
    if isinstance(value, Decimal) and value.is_finite() and value == value.to_integral_value():
        # `sum(integer 列)` 回的是 numeric：值不变，所以可以按整数列发。
        return int(value)
    _reject("value_type_unsupported")


def _decimal(value: object) -> str:
    if isinstance(value, bool) or not isinstance(value, Decimal):
        _reject("value_type_unsupported")
    if not value.is_finite():
        # NaN 与 ±Infinity 在 PostgreSQL 的 numeric 里真实存在，但它们不是钱数。
        _reject("value_not_finite")
    return format(value, "f")


def _moment(value: object) -> str:
    if not isinstance(value, datetime):
        _reject("value_type_unsupported")
    if value.tzinfo is None or value.utcoffset() is None:
        # 没有时区的时刻无法与窗口对齐：把它当本地时间还是 UTC 都是在猜。
        _reject("datetime_naive")
    return value.isoformat()


def _text(value: object, scope: Mapping[str, str]) -> str:
    if not isinstance(value, str):
        _reject("value_type_unsupported")
    if len(value) > MAX_TEXT_CHARS:
        _reject("text_too_large")
    if value in scope:
        # 真店号从任何一列回都是泄露，不只是"这一列不该有它"。
        _reject("raw_identifier")
    return value


# --- 公开载荷预算 -----------------------------------------------------------------

def _require_within_result_budget(columns: list[ExplorationColumn],
                                  rows: list[dict[str, object]]) -> None:
    payload = json.dumps({"columns": [column.model_dump() for column in columns],
                          "rows": rows}, ensure_ascii=False, separators=(",", ":"))
    if len(payload.encode("utf-8")) > MAX_RESULT_BYTES:
        _reject("result_too_large")


# --- 入参形状与拒绝 ---------------------------------------------------------------

def _text_sequence(values, error: str) -> list[str]:
    if isinstance(values, (str, bytes)) or not isinstance(values, (list, tuple)):
        raise TypeError(CODE_PREFIX + error)
    for item in values:
        if not isinstance(item, str):
            raise TypeError(CODE_PREFIX + error)
    return list(values)


def _reject(reason: str) -> NoReturn:
    # `from None`：从 `except` 里转码时不把带字面量的原异常链留给日志与模型载荷。
    raise ValueError(CODE_PREFIX + reason) from None
