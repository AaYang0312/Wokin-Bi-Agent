"""隔离分析（子项目 D）。

计划 2026-09-14-isolated-analysis-agent.md Task 1–5 的交付面：

- Task 1：`AnalysisRequest` → `AnalysisDataset` → `Finding` → `AnalysisResult`
  的严格模型，加上 `isolated_analysis` 领域与 `analysis_result` Artifact 类型
  的登记；
- Task 2：`load_analysis_dataset` —— 可信服务层把已授权、版本匹配的来源
  Artifact 投影成隔离分析的唯一输入（本包内唯一同时看见 DomainContext 与
  值对象的地方）；
- Task 3：`compute_findings` —— Decimal 纯函数，数值只出自这里；
- Task 4：`run_isolated_analysis` / `summarize_findings` —— 无工具模型总结与
  claim 守卫，模型调用形状被 import 守卫钉成 `tools=[]`；
- Task 5：`analyze_artifact` 固定六节点图与 `analyze_artifact` 模型 Tool ——
  CAS 运行记录、恰好一份结果 Artifact、fail-closed 持久化。

本包的分析运行（calculations/summarizer）仍不连库、不读文件、不发网络；
连库的只有可信服务层（loader/graph），模型 Tool 只读不写。

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
from .graph import ANALYSIS_CHAIN, ANALYSIS_DOMAIN, ANALYSIS_VERSION, analyze_artifact
from .models import (
    AnalysisDataset,
    AnalysisKind,
    AnalysisObservation,
    AnalysisRequest,
    AnalysisResult,
    Finding,
)
from .tool import TOOL_NAME, analysis_request_schema, execute_analysis_tool

__all__ = [
    "ANALYSIS_CHAIN",
    "ANALYSIS_DOMAIN",
    "ANALYSIS_VERSION",
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
    "TOOL_NAME",
    "analysis_request_schema",
    "analyze_artifact",
    "compute_findings",
    "execute_analysis_tool",
]
