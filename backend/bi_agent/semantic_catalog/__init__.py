"""语义目录：只描述已批准的 reporting 视图、字段、指标与合法 JOIN 粒度。

Task 1 的范围只有契约类型本身。目录内容（`CATALOG`）与
`retrieve_schema_candidates` 分属 Task 2/Task 3，此刻还不存在——本包因此
不查任何业务事实，也不新增 Agent Tool。
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

__all__ = [
    "SemanticCatalog",
    "SemanticEntity",
    "SemanticField",
    "SemanticJoin",
    "SemanticMetric",
    "SemanticSelection",
    "SemanticView",
]
