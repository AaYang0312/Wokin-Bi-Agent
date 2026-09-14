"""语义目录：只描述已批准的 reporting 视图、字段、指标与合法 JOIN 粒度。

Task 1 交付契约类型；Task 2 交付首版登记内容与服务端解析入口。检索
（`retrieve_schema_candidates`）与启动一致性校验（`validate_catalog_schema`）
分属 Task 3/Task 4，此刻还不存在——本包因此不查任何业务事实，也不新增 Agent Tool。
"""

from .models import (
    SemanticCatalog,
    SemanticEntity,
    SemanticField,
    SemanticJoin,
    SemanticMetric,
    SemanticSelection,
    SemanticView,
)
from .registry import (
    CATALOG,
    CATALOGS_BY_VERSION,
    SEMANTIC_CATALOG_VERSION,
    catalog_for_version,
    catalog_indexes,
    resolve_sql_identifier,
    validate_catalog,
)

__all__ = [
    "CATALOG",
    "CATALOGS_BY_VERSION",
    "SEMANTIC_CATALOG_VERSION",
    "SemanticCatalog",
    "SemanticEntity",
    "SemanticField",
    "SemanticJoin",
    "SemanticMetric",
    "SemanticSelection",
    "SemanticView",
    "catalog_for_version",
    "catalog_indexes",
    "resolve_sql_identifier",
    "validate_catalog",
]
