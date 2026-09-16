"""隔离分析（子项目 D）。

本切片（计划 2026-09-14-isolated-analysis-agent.md Task 1）只交付契约面：
`AnalysisRequest` → `AnalysisDataset` → `Finding` → `AnalysisResult` 的严格
模型，加上 `isolated_analysis` 领域与 `analysis_result` Artifact 类型的登记。

本包不连库、不读文件、不发起网络请求、不调用模型：授权 loader 属
Task 2（已交付），确定性 Decimal 计算属 Task 3（本切片），无工具总结属
Task 4，图与 Agent Tool 属 Task 5，所以本包仍不新增任何模型可见工具。

门禁：`AppSettings.isolated_analysis_enabled`（`ISOLATED_ANALYSIS_ENABLED`）
默认关闭。关闭与变量缺席时，Tool 列表、数据库读取与聊天结果必须与本切片
之前逐字一致。
"""

from .calculations import (
    MAD_METHOD,
    MAD_THRESHOLD,
    MAD_ZERO_SCORE,
    PREVIOUS_PERIOD_UNAVAILABLE,
    compute_findings,
)
from .models import (
    AnalysisDataset,
    AnalysisKind,
    AnalysisObservation,
    AnalysisRequest,
    AnalysisResult,
    Finding,
)

__all__ = [
    "AnalysisDataset",
    "AnalysisKind",
    "AnalysisObservation",
    "AnalysisRequest",
    "AnalysisResult",
    "Finding",
    "MAD_METHOD",
    "MAD_THRESHOLD",
    "MAD_ZERO_SCORE",
    "PREVIOUS_PERIOD_UNAVAILABLE",
    "compute_findings",
]
