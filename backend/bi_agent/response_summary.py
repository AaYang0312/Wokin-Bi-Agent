"""基于已存结果的确定性回答兜底。

工具/模型预算耗尽或补答失败时，仍然要给用户一个可用的回答。这个摘要只做三件事：
复述已持久化的指标、窗口、共同截止与限制说明——**不重新计算任何金额**，
因为金额只由确定性查询产生，二次汇总会造出第二套口径。
"""

from __future__ import annotations

from typing import Any

_MISSING = "本轮未能生成可靠结果，请稍后重试或缩小查询范围。"


def render_result_summary(domain_result: Any) -> str:
    """把一次已完成的领域结果复述成纯文本兜底回答。"""
    if domain_result is None:
        return _MISSING
    payload = _payload_of(domain_result)
    # invalid_parameters 也在这里：口径不兼容这类确定性拒答已被交付（有 Artifact），
    # 兜底摘要同样要把“为什么不兼容、要怎么改范围”说给用户，而不是只说“请重试”。
    if not isinstance(payload, dict) or payload.get("status") not in (
            "ok", "missing_data", "invalid_parameters"):
        return _MISSING

    filters = payload.get("filters") if isinstance(payload.get("filters"), dict) else {}
    metrics = [str(item) for item in (filters.get("metrics") or [])]
    definitions = payload.get("metric_definition") if isinstance(
        payload.get("metric_definition"), dict) else {}
    rows = [row for row in (payload.get("data") or []) if isinstance(row, dict)]

    lines: list[str] = []
    start, end = filters.get("start"), filters.get("end")
    if start and end:
        lines.append(f"窗口 {start} ~ {end}（北京时间，右开）。")
    if metrics:
        lines.append("口径：" + "；".join(
            f"{name}＝{definitions[name]}" if name in definitions else name
            for name in metrics))
    data_as_of = payload.get("data_as_of")
    lines.append(f"数据截止 {data_as_of}。" if data_as_of else "数据截止未知（回填未完成）。")

    coverage = payload.get("coverage") if isinstance(payload.get("coverage"), dict) else {}
    gaps = [str(item) for item in (coverage.get("gaps") or [])]
    if gaps:
        lines.append("覆盖缺口：" + "、".join(gaps) + "。")

    rendered = _render_rows(rows)
    if rendered:
        lines.append("已核验结果：" + rendered)

    basis = [item for item in (payload.get("basis") or []) if isinstance(item, dict)]
    if basis:
        # 同名指标可能来自不同通道：摘要必须带上口径与时间归属，否则兜底回答
        # 比正常结果更容易被拿去跨平台比较。
        lines.append("统计口径：" + "；".join(
            f"{item.get('metric')}＝{item.get('basis')}（时间归属 {item.get('time_basis')}）"
            for item in basis))

    groups = [item for item in (payload.get("rank_groups") or [])
              if isinstance(item, dict)]
    if groups:
        # 分区榜有好几份名次：不写出来，兜底摘要就会把几个分区读成一个全局榜单。
        lines.append("分区（各自出榜，不跨区比较）：" + "；".join(
            f"{item.get('group')} "
            + ("认证口径" if item.get("status") == "certified"
               else "未认证口径，按可观测样本")
            + f"，已出{item.get('groups_published')}/共{item.get('groups_total')}个商品"
            for item in groups))

    limitations = [str(item) for item in (payload.get("limitations") or [])]
    if limitations:
        lines.append("限制：" + "；".join(limitations) + "。")
    return "\n".join(lines)


def _payload_of(domain_result: Any) -> dict[str, Any] | None:
    """取领域结果给模型的那份载荷；没有 model_payload 时按 ToolResult 形状读。

    Agent 主层把已交付的确定性结果按 ToolResult 收集（不是 DomainResult），
    兜底摘要要能直接复述它，而不是因为拿不到 model_payload 就退化成占位文案。
    """
    payload = getattr(domain_result, "model_payload", None)
    if isinstance(payload, dict):
        return payload
    if not hasattr(domain_result, "status"):
        return None
    coverage = getattr(domain_result, "coverage", None)
    return {
        "status": getattr(domain_result, "status", None),
        "metric_definition": getattr(domain_result, "metric_definition", None),
        "coverage": (coverage.model_dump(mode="json")
                     if hasattr(coverage, "model_dump") else coverage),
        "filters": getattr(domain_result, "filters", None),
        "data": getattr(domain_result, "data", None),
        "data_as_of": getattr(domain_result, "data_as_of", None),
        "limitations": getattr(domain_result, "limitations", None),
        "basis": getattr(domain_result, "basis", None),
    }


def _render_rows(rows: list[dict[str, Any]]) -> str:
    """只复述行内已有数值，不求和、不排名、不换算。"""
    parts: list[str] = []
    for row in rows[:10]:
        cells = [f"{key} {value}" for key, value in sorted(row.items())
                 if isinstance(value, str) and not value.startswith("ent-")]
        if cells:
            parts.append("，".join(cells))
    if not parts:
        return ""
    tail = "" if len(rows) <= 10 else f"（另有 {len(rows) - 10} 行未列出）"
    return "；".join(parts) + tail
