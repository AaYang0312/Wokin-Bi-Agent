"""受控 SQL 探索（子项目 B）。

本切片（计划 Task 1 + Task 2）交付两件事：阶段契约 `ExplorationRequest` → `SqlDraft`
→ `ValidatedQueryPlan` → `ExplorationResult`（加结果列描述 `ExplorationColumn`），以及
把它们连起来的前两道服务端判定——`fixed_tool_for`（固定 Tool 能表达就不许降级成 SQL）
与 `compile_query`（确定性单基表、全参数化的 SELECT 草案）。

到这里为止本包仍然不解析 SQL、不校验 AST、不连库、不执行：AST 策略属 Task 3，只读
执行与投影属 Task 4，运行域与 Agent Tool 属 Task 5，所以也不新增任何模型可见工具。

门禁：`AppSettings.controlled_sql_enabled`（`CONTROLLED_SQL_ENABLED`）默认关闭。
关闭与变量缺席时，Tool 列表、数据库读取与聊天结果必须与本切片之前逐字一致；开启还
要求 `SEMANTIC_CATALOG_ENABLED=true`，因为探索层的标识符只能从已发布的语义目录解析
（目录关着就没有合法解析路径，只能失败）。
"""

from .compiler import compile_query
from .eligibility import FIXED_TOOL_METRICS, fixed_tool_for
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
    "FIXED_TOOL_METRICS",
    "SqlDraft",
    "ValidatedQueryPlan",
    "compile_query",
    "fixed_tool_for",
]
