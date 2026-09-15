"""approved 查询学习记忆（子项目 C）。

Task 1 交付契约：状态、槽位、样例与审核命令的形状，以及默认关闭的 feature gate
（`AppSettings.approved_query_memory_enabled`）。Task 2 在此之上交付写入侧：
成功运行的脱敏草稿构建（`build_draft_from_run`）与人工状态机
（`QueryMemoryRepository`，draft→approved→superseded/revoked）。Task 3 交付读取
侧：授权与版本过滤后的确定性检索（`retrieve_approved_examples`，只读 reporting
投影视图）。审核 API 属 Task 4，路由接入属 Task 5——本包此刻不提供任何 Agent
Tool。

模型、成功运行、点赞与纠错都不能改变长期记忆：能写 `bi.approved_query_examples`
的只有 `bi_approver` 身份（独立审核 DSN，最小权限）；聊天身份 `bi_app` 没有底表
权限，只能读 reporting 里 status='approved' 的投影视图（迁移 021 钉住）。
"""

from .models import (
    FORBIDDEN_VALUE_KEYS,
    ApprovalCommand,
    ApprovalStatus,
    ApprovedExample,
    QuerySlot,
    SlotKind,
    StoredMemoryRecord,
)
from .repository import QueryMemoryRepository, build_draft_from_run
from .retrieval import retrieve_approved_examples
from .sanitize import ALLOWED_REQUEST_KEYS, sanitize_normalized_request

__all__ = [
    "ALLOWED_REQUEST_KEYS",
    "FORBIDDEN_VALUE_KEYS",
    "ApprovalCommand",
    "ApprovalStatus",
    "ApprovedExample",
    "QueryMemoryRepository",
    "QuerySlot",
    "SlotKind",
    "StoredMemoryRecord",
    "build_draft_from_run",
    "retrieve_approved_examples",
    "sanitize_normalized_request",
]
