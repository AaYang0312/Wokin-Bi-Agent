"""分层报告（计划 2026-10-24 T3）：cases.jsonl / summary.json / report.md。

- 不覆盖已有 run 目录。
- 逐案例一行 JSONL、机器可读 summary、人读 Markdown 汇总三层分开。
- 全部落盘文本先过 ``sanitize``：DSN 密码与 password= 键值不落盘；
  异常只落类别名与安全摘要，不序列化异常对象、模型请求或 SQL 参数。
- offline 报告固定标注「脚本模型下的协议与业务回归，不衡量自然语言理解」；
  零 executed 时通过率为 null，绝不输出 100%。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

_DSN_PASSWORD = re.compile(r"(postgres(?:ql)?://[^:/@\s]+:)[^@/\s]+(@)")
# libpq 键值串的合法形态：不带引号、单引号、双引号（引号内可含空格）。
_PASSWORD_KV = re.compile(r"(password[=:\s])[^\s;]+", re.IGNORECASE)
_PASSWORD_SINGLE = re.compile(r"(password[=:\s]?)'[^']*'",
                              re.IGNORECASE)
_PASSWORD_DOUBLE = re.compile(r'(password[=:\s]?)"[^"]*"',
                              re.IGNORECASE)


def sanitize(text: str) -> str:
    """把 DSN/键值形态的密码（含带引号、含空格）整段替换成 ***。"""
    cleaned = _DSN_PASSWORD.sub(r"\1***\2", text)
    cleaned = _PASSWORD_SINGLE.sub(r"\1***", cleaned)
    cleaned = _PASSWORD_DOUBLE.sub(r"\1***", cleaned)
    return _PASSWORD_KV.sub(r"\1***", cleaned)


def _dump_summary_body(summary) -> dict:
    body = summary.model_dump()
    body.pop("cases")
    return body


def write_report(summary, output_dir: Path) -> None:
    output_dir = Path(output_dir)
    if output_dir.exists() and any(output_dir.iterdir()):
        raise ValueError(f"run目录已存在且非空，不覆盖：{output_dir}")
    output_dir.mkdir(parents=True, exist_ok=True)

    with (output_dir / "cases.jsonl").open("w", encoding="utf-8",
                                           newline="\n") as handle:
        for case in summary.cases:
            payload = case.model_dump()
            payload["problems"] = [sanitize(problem)
                                   for problem in payload["problems"]]
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")

    body = _dump_summary_body(summary)
    _write_text(output_dir / "summary.json",
                json.dumps(body, ensure_ascii=False, indent=2) + "\n")

    _write_text(output_dir / "report.md", _markdown(summary))


def _write_text(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8", newline="\n")


def _markdown(summary) -> str:
    totals = summary.totals
    if totals["pass_rate"] is None:
        pass_rate_text = "null（零executed，不输出通过率）"
    else:
        pass_rate_text = f"{totals['pass_rate']:.3f}"
    lines = [
        f"# 评测报告（{summary.mode}）",
        "",
        f"> {summary.note}",
        "",
        "## 汇总",
        "",
        "| 指标 | 值 |",
        "| --- | --- |",
        f"| attempted / executed | {totals['attempted']} / {totals['executed']} |",
        f"| passed / failed | {totals['passed']} / {totals['failed']} |",
        f"| blocked / skipped | {totals['blocked']} / {totals['skipped']} |",
        f"| human_review_pending | {totals['human_review_pending']} |",
        f"| pass_rate | {pass_rate_text} |",
        "",
        "## usage",
        "",
        f"- 状态：{summary.usage['status']}；来源：{summary.usage['source']}"
        "（合成脚本 usage 不计真实成本，不估算费用）",
        f"- 总量：{summary.usage['totals']}",
        f"- known 部分：{summary.usage['known']}",
        "",
        "## 环境",
        "",
        f"- 源码版本：`{summary.source.get('git_head')}`"
        f"{'（含未提交改动）' if summary.source.get('git_dirty') else ''}",
        f"- dataset SHA-256：`{summary.dataset_sha256}`",
        f"- 冻结时间：{summary.frozen_time}",
        f"- 门禁：{json.dumps(summary.flags, ensure_ascii=False)}；"
        f"重复：{summary.repeat}",
        f"- 命令：`{sanitize(summary.command)}`",
        "",
    ]
    problems_by_case = {
        case.id: [sanitize(problem) for problem in case.problems]
        for case in summary.cases if case.problems
    }
    if problems_by_case:
        lines.append("## 未通过 / 问题")
        lines.append("")
        for case_id, problems in problems_by_case.items():
            status = next(case.status for case in summary.cases
                          if case.id == case_id)
            lines.append(f"- **{case_id}**（{status}）：")
            for problem in problems:
                lines.append(f"  - {problem}")
        lines.append("")
    else:
        lines.append("全部场景满足已声明期望。")
        lines.append("")
    return "\n".join(lines)
