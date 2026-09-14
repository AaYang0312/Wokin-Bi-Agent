"""受控 SQL 探索（子项目 B）。

本切片（计划 Task 1）只有阶段契约：`ExplorationRequest` → `SqlDraft` →
`ValidatedQueryPlan` → `ExplorationResult`，加上结果列描述 `ExplorationColumn`。
没有编译器、AST 策略、只读执行、投影、运行域或 Agent Tool——那些依次属计划
Task 2／3／4／5，所以本包此刻不解析 SQL、不查库，也不新增任何模型可见工具。

门禁：`AppSettings.controlled_sql_enabled`（`CONTROLLED_SQL_ENABLED`）默认关闭。
关闭与变量缺席时，Tool 列表、数据库读取与聊天结果必须与本切片之前逐字一致；
开启还要求 `SEMANTIC_CATALOG_ENABLED=true`，因为探索层的标识符只能从已发布的语义
目录解析（目录关着就没有合法解析路径，只能失败）。
"""

from .models import (
    ExplorationColumn,
    ExplorationRequest,
    ExplorationResult,
    SqlDraft,
    ValidatedQueryPlan,
)

__all__ = [
    "ExplorationColumn",
    "ExplorationRequest",
    "ExplorationResult",
    "SqlDraft",
    "ValidatedQueryPlan",
]
