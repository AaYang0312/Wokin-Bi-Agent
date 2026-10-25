"""模型计量（计划 2026-10-24 T3）：包装现有 ChatModel，采集 usage/耗时。

不改模型行为、不改 timeout 与模型配置：``MeteredModel`` 保持
``complete(messages, tools, *, timeout_s) -> ModelReply`` 原协议，回复原样
返回；``ScriptedModel`` 提供离线脚本回复（协议路径与真实模型一致），脚本
耗尽显式失败。

usage 聚合规则（诚实计量）：

- 全部调用都带完整 usage → ``complete``，给总量；
- 任一调用缺 usage → 总量 ``null``，保留 known 部分与 ``partial`` 状态；
- 全部缺失 → ``unavailable``。离线脚本的 usage 来源标 ``synthetic``，
  不能计成真实成本；无价格表，不估算费用。
"""

from __future__ import annotations

import time

from bi_agent.llm import Message, ModelReply, ToolCall


class ScriptExhaustedError(RuntimeError):
    """脚本回复耗尽：执行合同不满足，不允许静默重复或编造回复。"""


class ScriptedModel:
    """按预制脚本逐次回复；与真实模型走同一条 agent 协议路径。

    规格里带 ``error`` 的回复转成 ``ModelError(code)``（脱敏类别码），
    复现超时/限流等故障路径。
    """

    def __init__(self, replies: list, *, case_id: str = ""):
        self._replies = list(replies)
        self._case_id = case_id
        self.consumed = 0

    @property
    def remaining(self) -> int:
        return len(self._replies)

    def complete(self, messages: list[Message], tools: list[dict[str, object]],
                 *, timeout_s: float) -> ModelReply:
        if not self._replies:
            raise ScriptExhaustedError(
                f"场景{self._case_id}的脚本回复已耗尽（已用{self.consumed}次）")
        spec = self._replies.pop(0)
        self.consumed += 1
        if isinstance(spec, ModelReply):
            return spec
        if spec.error is not None:
            from bi_agent.llm import ModelError

            raise ModelError(spec.error)
        return build_scripted_reply(spec)


class MeteredModel:
    """包装任意 ChatModel：计数、计时、记录 usage；行为零改动。"""

    def __init__(self, inner):
        self.inner = inner
        self.call_count = 0
        self.model_duration_ms = 0.0
        self.usage_records: list[dict | None] = []

    def complete(self, messages: list[Message], tools: list[dict[str, object]],
                 *, timeout_s: float) -> ModelReply:
        started = time.monotonic()
        try:
            reply = self.inner.complete(messages, tools, timeout_s=timeout_s)
        except Exception:
            # 失败的调用也是一次真实尝试：usage 记为缺失（None），不能从
            # 聚合记录里消失——否则失败轮次会被当成零成本或"全量已知"。
            self.usage_records.append(None)
            raise
        finally:
            self.model_duration_ms += (time.monotonic() - started) * 1000.0
            self.call_count += 1
        self.usage_records.append(reply.usage)
        return reply


def build_scripted_reply(spec) -> ModelReply:
    """ScriptedReply 规格（模型或 dict）→ ModelReply（与 acceptance._reply 同一构造方式）。"""
    from tests.evaluation.contracts import ScriptedReply

    if not isinstance(spec, ScriptedReply):
        spec = ScriptedReply(**spec)
    calls = [ToolCall(id=call.call_id, name=call.name, arguments=call.arguments)
             for call in spec.tool_calls]
    assistant = Message(role="assistant", content=spec.text,
                        tool_calls=calls)
    reply = ModelReply(text=spec.text, tool_calls=calls, usage=spec.usage)
    reply._message = assistant
    return reply


def aggregate_usage(records: list[dict | None]) -> dict:
    """按诚实计量规则聚合 usage；records 为每次调用的 usage（缺省 None）。"""
    if not records:
        return {"status": "unavailable", "totals": None, "known": None}
    fields = ("prompt_tokens", "completion_tokens", "total_tokens")
    known = {field: sum(record[field] for record in records
                        if record and record.get(field) is not None)
             for field in fields}
    any_known = any(record and record.get(field) is not None
                    for record in records for field in fields)
    complete = all(
        record is not None and all(record.get(field) is not None
                                   for field in fields)
        for record in records)
    if complete:
        return {"status": "complete", "totals": known, "known": known}
    if any_known:
        return {"status": "partial", "totals": None, "known": known}
    return {"status": "unavailable", "totals": None, "known": None}
