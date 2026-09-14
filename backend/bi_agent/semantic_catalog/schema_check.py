"""启动预检：目录声明的 reporting 结构必须与数据库里的真实 schema 一致（计划 Task 4）。

它只回答一个问题——"登记表还在骗人吗"——并且为此把边界收得极窄：

1. **只读元数据**：执行一条 `information_schema.columns` 查询，不读任何业务事实行，
   也不需要任何新权限。用的就是运行角色（`bi_app`）对获准视图已有的 SELECT 面；
   `tests.test_semantic_catalog.SemanticSchemaCheckDatabaseTests` 在只读角色下证明
   预检通过而 `bi.orders` 依旧读不到。
2. **只发那一条 SQL**：不发 `SELECT 1` 探活、不设 GUC、不建 prepared statement，
   也不 `SET ROLE`。测试里的连接替身对任何其它语句直接报错，退化会变红。
3. **错误只有稳定原因码**：`semantic_schema_mismatch:<稳定 ref>`。SQL 标识符、列名、
   数据库返回的原文、DSN 一律不进消息——这句话会进启动日志，也可能被运维工具原样
   转发；`_stable_refs()` 因此按 ref 词表再过一道，形状不符就换成固定码。
4. **失败即失败**：视图缺失、列缺失、类型族不符都让进程起不来，绝不降级成"目录为空"
   或"这次先不检索"。旧目录版本能否解释历史 Artifact 是 registry 的事；预检只认传进来
   的那一份，因此调用方必须只传当前版本（`api.create_runtime_app` 传的就是 `CATALOG`）。

类型族来自 `registry.DATA_TYPE_SQL_FAMILIES`：那是目录里唯一的"声明类型 ↔ PostgreSQL
`data_type`"知识，这里不另立第二份映射，否则同一件事就有了两种拼法并且迟早分叉。
"""

from __future__ import annotations

import re
from typing import Mapping, Protocol, Sequence

from .models import SemanticCatalog
from .registry import DATA_TYPE_SQL_FAMILIES, catalog_indexes, validate_catalog

# 计划 Task 4 Step 3 钉下的唯一查询：只取 reporting 的列，按视图与列序稳定返回。
COLUMNS_SQL = (
    "SELECT table_schema, table_name, column_name, data_type\n"
    "FROM information_schema.columns\n"
    "WHERE table_schema = 'reporting'\n"
    "ORDER BY table_name, ordinal_position"
)

# 公开消息里允许出现的唯一形状：目录 ref（kebab-case）。与 `models.REF_RE` 同一条规则。
_REF_SHAPE = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")

# 一个不像 ref 的候选只能来自绕过契约层构造的目录；不回显原文，只给这个固定码。
UNSAFE_REF = "catalog-entry"

REASON_PREFIX = "semantic_schema_mismatch"


class SchemaMismatch(ValueError):
    """目录声明与真实 schema 不一致：启动失败的原因码，不带任何 SQL 文本。

    消息固定为 `semantic_schema_mismatch:<第一个排序后的稳定 ref>`；完整清单挂在
    `refs` 上（同样是消毒过的 ref），供诊断使用。
    """

    def __init__(self, refs: Sequence[str]) -> None:
        stable = _stable_refs(refs)
        self.refs: tuple[str, ...] = stable
        super().__init__(f"{REASON_PREFIX}:{stable[0] if stable else UNSAFE_REF}")


class IntrospectionConnection(Protocol):
    """预检只需要"能执行那条只读查询"，因此不要求真是 psycopg 连接。"""

    def execute(self, sql: str, params: object = None) -> object:
        ...


def _stable_refs(refs: Sequence[str]) -> tuple[str, ...]:
    """去重 + 排序 + 消毒：消息必须可复现，且只能是 ref。

    排序是契约的一部分：同一组缺陷在任何进程、任何执行顺序里给出同一个字符串，
    运维才能 grep、计数与比较。
    """
    return tuple(sorted({
        ref if isinstance(ref, str) and _REF_SHAPE.fullmatch(ref) else UNSAFE_REF
        for ref in refs
    }))


ActualColumns = Mapping[tuple[str, str], Mapping[str, str]]


def _declared_columns(conn: IntrospectionConnection) -> ActualColumns:
    """执行那一条查询，映射成 `{(schema, view): {column: data_type}}`。

    键带 schema：只按视图名建索引会让 `public.v_shop_daily` 替 `reporting.v_shop_daily`
    交差——查询的 `WHERE` 本来就是为了不让别名混进来，映射这边也必须认三元组。
    """
    tables: dict[tuple[str, str], dict[str, str]] = {}
    for row in conn.execute(COLUMNS_SQL).fetchall():
        table_schema, table_name, column_name, data_type = row
        tables.setdefault((table_schema, table_name), {})[column_name] = data_type
    return tables


def validate_catalog_schema(conn: IntrospectionConnection, catalog: SemanticCatalog) -> None:
    """核对目录声明与库里 reporting 的真实列；不一致就抛 `SchemaMismatch`。

    先跑 `validate_catalog`：目录自己不闭包时"库里缺了什么"这个问题没有意义，
    更不该在碰数据库之后才发现。视图整张缺失只报视图 ref 一次——把它的每一列
    再念一遍不会让诊断更有用，只会让"第一个 ref"变成随机噪音。
    """
    validate_catalog(catalog)
    indexes = catalog_indexes(catalog)
    actual = _declared_columns(conn)

    missing: list[str] = []
    for view in catalog.views:
        if not actual.get((view.schema, view.name)):
            missing.append(view.ref)
    for field in catalog.fields:
        owner = indexes.views[field.view_ref]
        columns = actual.get((owner.schema, owner.name))
        if not columns:
            continue                                    # 已在视图一级报过
        data_type = columns.get(field.column)
        if data_type is None:
            missing.append(field.ref)
        elif data_type not in DATA_TYPE_SQL_FAMILIES[field.data_type]:
            missing.append(field.ref)
    if missing:
        raise SchemaMismatch(missing)
