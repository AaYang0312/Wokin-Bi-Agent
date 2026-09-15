"""approved 查询学习记忆（子项目 C）。

Task 1 只交付契约：状态、槽位、样例与审核命令的形状，以及默认关闭的 feature gate
（`AppSettings.approved_query_memory_enabled`）。本包此刻不连库、不检索、不提供任何
Agent Tool——草稿构建与生命周期属 Task 2，检索属 Task 3，路由接入属 Task 5。

模型、成功运行、点赞与纠错都不能改变长期记忆：能写 `bi.approved_query_examples` 的
只有 `bi_approver` 身份（独立审核 DSN，最小权限）；聊天身份 `bi_app` 没有底表权限，
只能读 reporting 里 status='approved' 的投影视图（迁移 021 钉住）。
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

__all__ = [
    "FORBIDDEN_VALUE_KEYS",
    "ApprovalCommand",
    "ApprovalStatus",
    "ApprovedExample",
    "QuerySlot",
    "SlotKind",
    "StoredMemoryRecord",
]
