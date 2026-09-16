"""可信服务层：把已持久化的数据集 Artifact 投影成隔离分析的 `AnalysisDataset`。

计划 2026-09-14-isolated-analysis-agent.md Task 2。本模块是唯一允许同时看见
`DomainContext`（服务端上下文）与 `AnalysisDataset`（纯值对象）的地方；它的
全部判断都发生在分析运行之前，失败一律 fail closed，绝不自动重查数据库、
绝不发起任何 reporting 查询——`context.conn` 在本模块内不可触碰（测试用地雷
替身钉住）。

拒绝通道（全部是稳定原因码，模型调用前生效）：

- `analysis_source_not_found`：不存在、跨 owner、非成功运行、缺血缘、ref 非法
  ——同一个码，读不到就是读不到，无法枚举他人的 Artifact；
- `analysis_source_type_unsupported`：只有 `metric_result / comparison_table /
  trend_series` 三个数据集 schema 是合法来源，图表/价审/库存/分析结果不是；
- `analysis_source_invalid`：落库载荷没有通过现有公开校验（篡改或腐败），
  或出现了 JSON 通道不可能出现的形状（float/bool）；
- `analysis_source_version_mismatch`：血缘五个版本字段逐项对照当前常量，
  过期血缘零容忍，不自动迁移；
- `analysis_source_coverage_incomplete`：覆盖必须是 complete，或明确带 gaps
  的 partial；`missing` 与自相矛盾的行一律拒收；
- `analysis_source_time_invalid`：来源快照时点不得晚于当前时刻；
- `analysis_source_too_large`：行数、每行投影字段数、canonical JSON 尺寸
  三道预算（500 行 / 20 字段 / 256 KiB）；
- `analysis_source_metric_missing`：一行投影后没有任何指标（缺失不当作 0）；
- `analysis_row_ref_duplicate`：内容相同的行必然派生同一个 row_ref，重复即拒，
  绝不静默去重；
- `analysis_dimension_unauthorized`：实体引用必须落在当前授权集合或来源
  `entities` 投影内；
- 内部主键（原始 ERP 主键、长数字串）不得借道进入分析：这一条由载荷重校验
  执行——公开契约里引用列只收 `ent-` 句柄、文本列不夹带长数字串，读回侧
  重新过同一份契约，篡改或腐败的载荷在投影前就被拒。

数值纪律（Task 1 信任生产方注记的兑现）：指标只收精确十进制——写入侧校验
保证了合法载荷里只有十进制文本与整数，读回侧看到 float 就说明载荷被篡改，
直接拒收，永不走浮点通道；位宽上限沿用 `analysis.models` 的词表边界。

fingerprint 与 row_ref 都从行内容规范化派生（键序、行序无关）：行的增删改
必然改变 fingerprint，纯重排不改变。
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import TYPE_CHECKING, Any
from uuid import UUID

from bi_agent.catalog import REF_RE
from bi_agent.commerce.metrics import COMMERCE_GRAPH_VERSION, COMMERCE_METRIC_VERSION
from bi_agent.inventory.rules import (
    INVENTORY_GRAPH_VERSION,
    INVENTORY_METRIC_VERSION,
    INVENTORY_SCHEMA_VERSION,
    UNIT_CONVERSION_REGISTRY_VERSION,
)
from bi_agent.listing_audit.rules import (
    LISTING_GRAPH_VERSION,
    LISTING_METRIC_VERSION,
    LISTING_SCHEMA_VERSION,
    LISTING_SOURCE_REGISTRY_VERSION,
)
from bi_agent.runtime.artifacts import QueryProvenance
from bi_agent.runtime.models import validate_artifact_payload

from .models import AnalysisDataset, AnalysisObservation

if TYPE_CHECKING:  # 仅类型标注：分析包不对 commerce 建立运行时依赖。
    from bi_agent.commerce.models import DomainContext

__all__ = ["dataset_fingerprint", "load_analysis_dataset"]

# 数据集来源白名单：计划 Global Constraints 的三个 schema，一个不多。
SOURCE_ARTIFACT_TYPES = frozenset(
    {"metric_result", "comparison_table", "trend_series"})

# 三道预算（计划 Global Constraints / Task 2 Step 4）。
MAX_OBSERVATIONS = 500
MAX_ROW_FIELDS = 20
MAX_PROJECTION_BYTES = 256 * 1024

# 当前版本常量集：血缘五字段逐项对照。schema/policy/source/graph 允许各域
# 当前值（business_query / listing / inventory / commerce），metric_version
# 随领域冻结——过期的任何一项都让整份来源不可分析。
_CURRENT = QueryProvenance()
_CURRENT_SCHEMAS = frozenset({_CURRENT.schema_version, LISTING_SCHEMA_VERSION,
                              INVENTORY_SCHEMA_VERSION})
_CURRENT_METRICS = frozenset({_CURRENT.metric_version, COMMERCE_METRIC_VERSION,
                              LISTING_METRIC_VERSION, INVENTORY_METRIC_VERSION})
_CURRENT_POLICIES = frozenset({_CURRENT.policy_version, LISTING_SOURCE_REGISTRY_VERSION,
                               UNIT_CONVERSION_REGISTRY_VERSION})
_CURRENT_SOURCES = frozenset({_CURRENT.source_registry_version,
                              LISTING_SOURCE_REGISTRY_VERSION})
_CURRENT_GRAPHS = frozenset({_CURRENT.graph_version, COMMERCE_GRAPH_VERSION,
                             LISTING_GRAPH_VERSION, INVENTORY_GRAPH_VERSION})

# 与 analysis.models 同一条键形规则；日期与安全 token 是 dimension 值的仅有的
# 两种非 ref 形态。十进制文本属于指标通道。
_DECIMAL_TEXT_RE = re.compile(r"^-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?$")
_ISO_DATE_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
_TOKEN_RE = re.compile(r"^[a-z][a-z0-9]*(-[a-z0-9]+)*$")


def dataset_fingerprint(observations: tuple[AnalysisObservation, ...]) -> str:
    """行/键序无关的规范化指纹：行按 row_ref 排序后整列 canonical JSON 哈希。"""
    canonical = [item.model_dump(mode="json") for item in observations]
    canonical.sort(key=lambda row: row["row_ref"])
    encoded = json.dumps(canonical, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _not_found() -> ValueError:
    return ValueError("analysis_source_not_found")


def load_analysis_dataset(ref: str, *, context: Any) -> AnalysisDataset:
    """把一次已授权的数据集 Artifact 投影成分析运行的唯一输入。"""
    try:
        artifact_id = UUID(str(ref))
    except (TypeError, ValueError):
        raise _not_found() from None
    stored = context.store.load_artifact_for_analysis(artifact_id,
                                                      subject_id=context.subject_id)
    # 双保险：即使 Store 实现回归（漏掉过滤），快照归属也在这里复核。
    if stored.subject_id != context.subject_id or stored.ref.id != artifact_id:
        raise _not_found()
    if stored.artifact_type not in SOURCE_ARTIFACT_TYPES:
        raise ValueError("analysis_source_type_unsupported")
    payload = _revalidate(stored)
    _require_current_versions(stored.provenance)
    _require_publishable_coverage(payload)
    _require_not_future(stored.data_as_of, now=context.now)
    rows = payload.get("data", [])
    if not isinstance(rows, list) or len(rows) > MAX_OBSERVATIONS:
        raise ValueError("analysis_source_too_large")
    authorized = _authorized_refs(payload, context)
    seen: set[str] = set()
    projected: list[AnalysisObservation] = []
    for row in rows:
        observation = _project_row(row, authorized=authorized)
        if observation.row_ref in seen:
            # 内容相同的行派生同一个 row_ref：重复即拒，绝不静默去重。
            raise ValueError("analysis_row_ref_duplicate")
        seen.add(observation.row_ref)
        projected.append(observation)
    observations = tuple(projected)
    encoded = json.dumps([item.model_dump(mode="json") for item in observations],
                         ensure_ascii=False, sort_keys=True,
                         separators=(",", ":")).encode("utf-8")
    if len(encoded) > MAX_PROJECTION_BYTES:
        raise ValueError("analysis_source_too_large")
    return AnalysisDataset(
        source_artifact_ref=str(stored.ref.id),
        source_fingerprint=dataset_fingerprint(observations),
        metric_version=stored.provenance.metric_version,
        observations=observations,
        limitations=())


def _revalidate(stored) -> dict[str, object]:
    """落库载荷按当前公开契约重校验：只信现在的规则，不信写入时的版本。"""
    try:
        return validate_artifact_payload(stored.payload, stored.artifact_type)
    except ValueError:
        raise ValueError("analysis_source_invalid") from None


def _require_current_versions(provenance: QueryProvenance) -> None:
    """五个版本字段逐项对照当前常量集；任一过期即拒，不自动迁移。"""
    if (provenance.schema_version not in _CURRENT_SCHEMAS
            or provenance.metric_version not in _CURRENT_METRICS
            or provenance.policy_version not in _CURRENT_POLICIES
            or provenance.source_registry_version not in _CURRENT_SOURCES
            or provenance.graph_version not in _CURRENT_GRAPHS):
        raise ValueError("analysis_source_version_mismatch")


def _require_publishable_coverage(payload: dict[str, object]) -> None:
    """覆盖必须是 complete，或明确带 gaps 的 partial；自相矛盾与 missing 都拒。"""
    coverage = payload.get("coverage")
    if not isinstance(coverage, dict):
        raise ValueError("analysis_source_coverage_incomplete")
    status = coverage.get("status")
    gaps = coverage.get("gaps")
    if status == "partial" and isinstance(gaps, list) and gaps:
        return
    if status == "complete" and gaps == []:
        return
    raise ValueError("analysis_source_coverage_incomplete")


def _require_not_future(data_as_of, *, now) -> None:
    if data_as_of is None:
        return
    moment = data_as_of
    if moment.tzinfo is None:
        # 列本该带时区；防御性归一到 UTC 再比较，不把天真时间当任意本地时刻。
        from datetime import timezone

        moment = moment.replace(tzinfo=timezone.utc)
    if moment > now:
        raise ValueError("analysis_source_time_invalid")


def _authorized_refs(payload: dict[str, object], context: Any) -> frozenset[str]:
    """当前授权集合 ∪ 来源 entities 投影（计划 Task 2 Step 4 的并集语义）。"""
    refs = {str(item) for item in (context.shop_refs or {}).values()}
    entities = payload.get("entities")
    if isinstance(entities, list):
        for item in entities:
            if isinstance(item, dict) and isinstance(item.get("ref"), str):
                refs.add(item["ref"])
    return frozenset(refs)


def _row_ref(row: dict[str, object]) -> str:
    """行内容派生的稳定句柄：内容相同即同 ref，重复行因此无处遁形。"""
    canonical = json.dumps(row, ensure_ascii=False, sort_keys=True,
                           separators=(",", ":")).encode("utf-8")
    return f"row-{hashlib.sha256(canonical).hexdigest()[:12]}"


def _project_row(row: object, *, authorized: frozenset[str]) -> AnalysisObservation:
    if not isinstance(row, dict):
        raise ValueError("analysis_source_invalid")
    dimensions: dict[str, str] = {}
    metrics: dict[str, str] = {}
    for key, value in row.items():
        if not isinstance(key, str):
            raise ValueError("analysis_source_invalid")
        if value is None:
            continue                      # 缺失不是 0：不进投影，由分析层面对“没有”
        if isinstance(value, bool) or isinstance(value, (float, list, dict)):
            # 合法落库载荷里不存在这些形状；读到即视为篡改/腐败。
            raise ValueError("analysis_source_invalid")
        if isinstance(value, int):
            metrics[key] = str(value)
            continue
        text = str(value)
        if _DECIMAL_TEXT_RE.fullmatch(text):
            metrics[key] = text
            continue
        if not (_ISO_DATE_RE.fullmatch(text) or _TOKEN_RE.fullmatch(text)):
            continue                      # 展示文本等非分析列不进投影
        if REF_RE.fullmatch(text) and text not in authorized:
            raise ValueError("analysis_dimension_unauthorized")
        dimensions[key] = text
    if not metrics:
        raise ValueError("analysis_source_metric_missing")
    if len(dimensions) + len(metrics) > MAX_ROW_FIELDS or len(dimensions) > 10 \
            or len(metrics) > 10:
        raise ValueError("analysis_source_too_large")
    return AnalysisObservation(row_ref=_row_ref(row), dimensions=dimensions,
                               metrics=metrics)
