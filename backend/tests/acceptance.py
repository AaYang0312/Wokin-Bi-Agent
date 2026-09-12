"""20题验收runner。三种模式互斥：

- ``--offline``：模拟模型消息序列 + 真实测试数据库，验证协议与业务执行；
  **不能证明模型理解准确率**。
- ``--provider-smoke``：选中provider的真实工具回合（模型提出工具调用→
  回传同ID结果→模型回答），不以纯文本问好代替。
- ``--live``：选中provider在合成测试DB跑20题；澄清与因果边界人工核看。

没有授权或凭证的provider记“未实测”，不算通过，也不阻碍离线验收。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from unittest.mock import Mock, patch
from uuid import NAMESPACE_URL, uuid5

from bi_agent.agent import SessionState, answer
from bi_agent.llm import Message, ModelReply, ToolCall
from bi_agent.metrics import ToolResult
from tests.fakeconn import S1_REF

QUESTIONS_PATH = Path(__file__).with_name("questions.jsonl")
FROZEN_NOW = None  # 由seed后的测试数据决定，从tests.test_db导入


def _reply(text=None, calls=None):
    assistant = Message(role="assistant", content=text, tool_calls=calls or [])
    reply = ModelReply(text=text, tool_calls=calls or [])
    reply._message = assistant
    return reply


def _call(name: str, arguments: dict, call_id: str = "call_1") -> ToolCall:
    return ToolCall(id=call_id, name=name, arguments=arguments)


def _quantize(value) -> Decimal:
    try:
        return Decimal(str(value)).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        return Decimal("0")


def _load_questions() -> list[dict]:
    questions = []
    for line in QUESTIONS_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            questions.append(json.loads(line))
    assert len(questions) == 20, f"验收题数量应为20，实际{len(questions)}"
    return questions


def _model_script(question: dict) -> list[ModelReply]:
    """offline模式：按题预制最小参数的工具调用序列。

    只提供真实模型大概率会给出的字段，其余依赖服务端合并逻辑；
    这正是离线验收要覆盖的协议路径。
    """
    qid = question["id"]
    query = {"shop_ids": [S1_REF]}
    if qid in {"02", }:
        query["metrics"] = ["paid_orders", "aov"]
    elif qid == "03":
        query.update({"metrics": ["paid_amount"], "group_by": "day"})
    elif qid == "04":
        query.update({"metrics": ["product_paid_amount"], "group_by": "product",
                      "top_n": 2})
    elif qid == "05":
        query.update({"metrics": ["quantity"], "group_by": "product", "top_n": 2})
    elif qid in {"06", "20"}:
        query.update({"metrics": ["paid_amount"], "compare": "previous_period"})
    elif qid in {"10", "11", "12", "14", "15"}:
        query["metrics"] = (["refund_amount"] if qid == "10" else
                            ["cohort_refund_rate"] if qid == "11" else
                            ["cash_difference"] if qid == "12" else ["paid_amount"])
    if qid == "19":
        query = {"shop_ids": ["S2"]}
        return [_call_reply("query_business", query),
                _reply(text="该店铺不在授权范围。")]
    if qid == "07":
        return [_call_reply("query_business", query), _reply(text="最近7天支付1000元。"),
                _call_reply("query_business", {"shop_ids": [S1_REF]}, call_id="call_2"),
                _reply(text="上个月覆盖不足，无法查询。")]
    if qid == "16":
        return [_call_reply("evaluate_promotion", {"mode": "actual_budget"}),
                _reply(text="当前未取得推广实耗，不能计算实际费率或ROAS。")]
    if qid == "17":
        return [_call_reply("evaluate_promotion", {
            "mode": "sales_cap", "sales_estimate": "100000", "target_ratio": "0.12"}),
            _reply(text="按用户假设，上限12000元。")]
    if qid == "18":
        return [_call_reply("evaluate_promotion", {
            "mode": "budget_scenario", "budget": "100", "assumed_spend": "120"}),
            _reply(text="已超支20元，剩余预算0。")]
    if qid == "20":
        return [_call_reply("query_business", query),
                _reply(text="数据显示支付较上一等长周期增长100%，并非下跌；"
                            "缺少流量与广告归因数据，不能下广告因果结论。")]
    return [_call_reply("query_business", query), _reply(text="已完成查询。")]


def _call_reply(name: str, arguments: dict, call_id: str = "call_1") -> ModelReply:
    return _reply(calls=[_call(name, arguments, call_id)])


def _normalize_params(request) -> dict:
    return {
        "start": request.start.isoformat(),
        "end": request.end.isoformat(),
        "shop_ids": sorted(request.shop_ids),
        "metrics": sorted(request.metrics),
        "group_by": request.group_by,
        "compare": request.compare,
        "top_n": request.top_n,
    }


def _value_problems(expected_values: dict, result: ToolResult) -> list[str]:
    problems: list[str] = []
    rows = result.data
    if not rows:
        return ["工具结果无数据行"]
    for key, expected in expected_values.items():
        if key == "products":
            got = [(row.get("product_id"), str(row.get(
                "product_paid_amount" if _is_money(expected[0][1]) else "quantity")))
                for row in rows if row.get("product_id")]
            got_values = {pid: _quantize(value) for pid, value in got}
            for pid, value in expected:
                if got_values.get(pid) != _quantize(value):
                    problems.append(f"商品{pid}期望{value}，实际{got_values.get(pid)}")
            continue
        if isinstance(expected, list):
            got = [str(row.get(key)) for row in rows]
            got_q = [_quantize(v) for v in got]
            exp_q = [_quantize(v) for v in expected]
            if got_q != exp_q:
                problems.append(f"{key}期望{expected}，实际{got}")
            continue
        actual = rows[0].get(key)
        if actual is None or _quantize(actual) != _quantize(expected):
            problems.append(f"{key}期望{expected}，实际{actual}")
    return problems


def _is_money(value: str) -> bool:
    return value not in {"7", "4"}


def _param_problems(expected_params: dict, request) -> list[str]:
    actual = _normalize_params(request)
    problems = []
    for key, value in expected_params.items():
        actual_value = actual.get(key)
        if key in ("shop_ids", "metrics"):
            if sorted(value if isinstance(value, list) else [value]) != sorted(
                    actual_value if isinstance(actual_value, list) else [actual_value]):
                problems.append(f"{key}期望{value}，实际{actual_value}")
        elif actual_value != value:
            problems.append(f"{key}期望{value}，实际{actual_value}")
    return problems


def _run_turn(question: str, state: SessionState, model, conn, allowed, now):
    from bi_agent.runtime import MemoryQueryRunStore, TurnContext

    turn_number = len([message for message in state.turns if message.role == "user"])
    chat_id = uuid5(NAMESPACE_URL, f"acceptance-chat:{state.subject}")
    return answer(
        question,
        state,
        model=model,
        conn=conn,
        allowed_shop_ids=allowed,
        now=now,
        run_store=MemoryQueryRunStore(forbidden_values=allowed),
        turn_context=TurnContext(
            chat_id=chat_id,
            user_message_id=uuid5(
                NAMESPACE_URL,
                f"acceptance-message:{state.subject}:{turn_number}:{question}",
            ),
            subject_id=state.subject,
        ),
    )


def _verify_turn(expected: dict, turn, recorded: list) -> list[str]:
    problems: list[str] = []
    if expected.get("clarify"):
        if not turn.clarification:
            problems.append("期望澄清，但未产生澄清")
        return problems
    if expected["tool"] == "query_business":
        if expected.get("status") == "forbidden":
            if recorded:
                problems.append("期望拒绝执行，但query_business被调用")
            if turn.results:
                problems.append("期望无成功工具结果")
            return problems
        if not recorded:
            return ["query_business未被调用"]
        request = recorded[-1]
        problems.extend(_param_problems(expected.get("parameters", {}), request))
    if expected.get("status"):
        if expected["tool"] == "evaluate_promotion" and turn.results:
            actual_status = turn.results[-1].status
        elif turn.results:
            actual_status = turn.results[-1].status
        else:
            actual_status = "no_result"
        if actual_status != expected["status"]:
            problems.append(f"status期望{expected['status']}，实际{actual_status}")
    if expected.get("values") and turn.results:
        problems.extend(_value_problems(expected["values"], turn.results[-1]))
    for fragment in expected.get("text_contains", []):
        if fragment not in (turn.text or ""):
            problems.append(f"回答未包含“{fragment}”")
    return problems


def run_offline() -> int:
    """模拟模型 + 真实测试数据库：验证协议与业务执行。"""
    import os

    import psycopg

    if not os.getenv("BI_TEST_ADMIN_DSN"):
        print(json.dumps({"mode": "offline", "result": "skipped",
                          "reason": "未配置BI_TEST_ADMIN_DSN"}))
        return 2
    from tests.test_db import FROZEN_NOW, seed_business_case

    # 单事务运行并在结束时回滚，不向共享测试库提交任何数据
    conn = psycopg.connect(os.environ["BI_TEST_ADMIN_DSN"])
    seed_business_case(conn)
    # 题09需要同名授权标签
    conn.execute(
        "INSERT INTO bi.shops(shop_id, platform, display_name) "
        "VALUES ('S9','fxg','店铺A') ON CONFLICT (shop_id) DO NOTHING")
    from .test_db import ALL_CAPABILITIES, set_capabilities

    set_capabilities(conn, "S9", *ALL_CAPABILITIES)

    import bi_agent.metrics as metrics_module

    real_query = metrics_module.query_business
    questions = _load_questions()
    report = []
    failures = 0
    for question in questions:
        started = time.monotonic()
        recorded: list = []
        spy_errors = []

        def spy(connection, request, **kwargs):
            recorded.append(request)
            return real_query(connection, request, **kwargs)

        state = SessionState(subject=f"acceptance-{question['id']}")
        allowed = frozenset({"S1", "S9"}) if question["id"] == "09" else frozenset({"S1"})
        model = Mock()
        model.complete.side_effect = _model_script(question)
        expected_list = (question["expected"] if isinstance(question["expected"], list)
                         else [question["expected"]] * len(question["turns"]))
        problems: list[str] = []
        try:
            with patch("bi_agent.business_query.nodes.metrics.query_business", side_effect=spy):
                for turn_text, expected in zip(question["turns"], expected_list):
                    turn = _run_turn(turn_text, state, model, conn, allowed, FROZEN_NOW)
                    state = turn.state
                    problems.extend(_verify_turn(expected, turn, recorded))
                    if problems:
                        break
        except Exception as exc:  # noqa: BLE001
            problems.append(f"异常：{type(exc).__name__}: {exc}")
        duration = time.monotonic() - started
        status = "pass" if not problems else "fail"
        failures += 1 if problems else 0
        report.append({"id": question["id"], "status": status,
                       "problems": problems, "duration_s": round(duration, 3),
                       "tokens": "unknown", "error_class":
                           problems[0].split("：")[0] if problems else None})
        print(json.dumps(report[-1], ensure_ascii=False))
    conn.rollback()
    conn.close()
    summary = {"mode": "offline", "total": len(questions), "failures": failures,
               "result": "pass" if failures == 0 else "fail",
               "note": "离线模式不能证明模型理解准确率；live结果见docs/demo.md"}
    print(json.dumps(summary, ensure_ascii=False))
    return 0 if failures == 0 else 1


def _real_model_or_none():
    import os

    from bi_agent.config import load_model_settings
    from bi_agent.llm import create_model

    try:
        settings = load_model_settings(os.environ)
    except ValueError as exc:
        print(json.dumps({"mode": "provider", "result": "未实测",
                          "reason": f"缺少凭证或配置：{exc}"}))
        return None
    base_url = settings.base_url or "default"
    print(json.dumps({"mode": "provider", "provider": settings.provider,
                      "model": settings.model, "base_url": base_url,
                      "date": time.strftime("%Y-%m-%d")}))
    return create_model(settings)


def run_provider_smoke() -> int:
    """真实工具回合：模型提出工具调用→回传同ID结果→模型回答。"""
    import os

    import psycopg

    model = _real_model_or_none()
    if model is None:
        return 0
    if not os.getenv("BI_TEST_ADMIN_DSN"):
        print(json.dumps({"result": "未实测", "reason": "缺少测试数据库配置"}))
        return 0
    from bi_agent.business_query import BusinessQueryContext, execute_business_query_tool
    from bi_agent.runtime import MemoryQueryRunStore, TurnContext
    from tests.test_db import FROZEN_NOW, seed_business_case

    conn = psycopg.connect(os.environ["BI_TEST_ADMIN_DSN"])
    seed_business_case(conn)
    from bi_agent.agent import _tool_schemas

    state = SessionState(subject="smoke", shop_refs={"S1": S1_REF})
    messages: list[Message] = [Message(
        role="user",
        content=f"店铺{S1_REF}最近7天（截至2026-09-08）的支付金额是多少？请调用工具查询。")]
    tools = _tool_schemas()
    reply = model.complete(messages, tools, timeout_s=30)
    if not reply.tool_calls:
        print(json.dumps({"result": "fail", "reason": "模型未提出工具调用"}))
        return 1
    messages.append(reply.as_message())
    turn_context = TurnContext(
        chat_id=uuid5(NAMESPACE_URL, "provider-smoke-chat"),
        user_message_id=uuid5(NAMESPACE_URL, "provider-smoke-message"),
        subject_id="smoke",
    )
    store = MemoryQueryRunStore(forbidden_values={"S1"})
    for attempt_no, call in enumerate(reply.tool_calls, start=1):
        execution = execute_business_query_tool(
            call,
            state,
            BusinessQueryContext(
                chat_id=turn_context.chat_id,
                user_message_id=turn_context.user_message_id,
                subject_id=turn_context.subject_id,
                question=messages[-1].content or "",
                previous_filters={},
                shop_refs=state.shop_refs,
                allowed_shop_ids=frozenset({"S1"}),
                now=FROZEN_NOW,
                deadline=time.monotonic() + 30,
                attempt_no=attempt_no,
            ),
            conn,
            store,
        )
        content = json.dumps(execution.domain_result.model_payload, ensure_ascii=False)
        messages.append(Message(role="tool", tool_call_id=call.id, content=content))
    final = model.complete(messages, tools, timeout_s=30)
    ok = bool(final.text)
    conn.rollback()
    conn.close()
    print(json.dumps({"result": "pass" if ok else "fail",
                      "tool_call_ids": [c.id for c in reply.tool_calls],
                      "final_text": (final.text or "")[:200]}, ensure_ascii=False))
    return 0 if ok else 1


def run_live() -> int:
    """选中provider在合成测试DB跑20题；澄清与因果边界人工核看。"""
    import os

    import psycopg

    model = _real_model_or_none()
    if model is None:
        return 0
    if not os.getenv("BI_TEST_ADMIN_DSN"):
        print(json.dumps({"result": "未实测", "reason": "缺少测试数据库配置"}))
        return 0
    from tests.test_db import FROZEN_NOW, seed_business_case

    conn = psycopg.connect(os.environ["BI_TEST_ADMIN_DSN"])
    seed_business_case(conn)
    conn.execute(
        "INSERT INTO bi.shops(shop_id, platform, display_name) "
        "VALUES ('S9','fxg','店铺A') ON CONFLICT (shop_id) DO NOTHING")
    from .test_db import ALL_CAPABILITIES, set_capabilities

    set_capabilities(conn, "S9", *ALL_CAPABILITIES)
    import bi_agent.metrics as metrics_module

    real_query = metrics_module.query_business
    questions = _load_questions()
    failures = 0
    for question in questions:
        started = time.monotonic()
        recorded: list = []

        def spy(connection, request, **kwargs):
            recorded.append(request)
            return real_query(connection, request, **kwargs)

        state = SessionState(subject=f"live-{question['id']}")
        allowed = frozenset({"S1", "S9"}) if question["id"] == "09" else frozenset({"S1"})
        expected_list = (question["expected"] if isinstance(question["expected"], list)
                         else [question["expected"]] * len(question["turns"]))
        problems: list[str] = []
        try:
            with patch("bi_agent.business_query.nodes.metrics.query_business", side_effect=spy):
                for turn_text, expected in zip(question["turns"], expected_list):
                    turn = _run_turn(turn_text, state, model, conn, allowed, FROZEN_NOW)
                    state = turn.state
                    problems.extend(_verify_turn(expected, turn, recorded))
                    if problems:
                        break
        except Exception as exc:  # noqa: BLE001
            problems.append(f"异常：{type(exc).__name__}: {exc}")
        failures += 1 if problems else 0
        print(json.dumps({"id": question["id"],
                          "status": "pass" if not problems else "fail",
                          "problems": problems,
                          "duration_s": round(time.monotonic() - started, 2),
                          "human_review": expected_list[0].get("clarify", False)},
                         ensure_ascii=False))
    conn.rollback()
    conn.close()
    print(json.dumps({"mode": "live", "failures": failures,
                      "result": "pass" if failures == 0 else "fail",
                      "note": "澄清与因果边界仍需人工核看"}, ensure_ascii=False))
    return 0 if failures == 0 else 1


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="tests.acceptance")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--offline", action="store_true")
    group.add_argument("--provider-smoke", action="store_true")
    group.add_argument("--live", action="store_true")
    args = parser.parse_args(argv)
    if args.offline:
        return run_offline()
    if args.provider_smoke:
        return run_provider_smoke()
    return run_live()


if __name__ == "__main__":
    sys.exit(main())
