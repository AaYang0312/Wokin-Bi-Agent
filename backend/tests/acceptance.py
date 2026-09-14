"""26题验收runner（修订后的多来源合同）。三种模式互斥：

- ``--offline``：模拟模型消息序列 + 真实测试数据库，验证协议与业务执行；
  **不能证明模型理解准确率**，也不能当作任何平台"真实来源已就绪"的证据。
- ``--provider-smoke``：选中provider的真实工具回合（模型提出工具调用→
  回传同ID结果→模型回答），不以纯文本问好代替。
- ``--live``：选中provider在合成测试DB跑26题；澄清与因果边界人工核看。

没有授权或凭证的provider记“未实测”，不算通过，也不阻碍离线验收。

2026-09-14（计划 Task 11）把题集从 20 题扩到修订后的 26 题：01–07 / 09–14 /
16–20 的数值与安全边界沿用，08 与 15 按多来源合同改预期，21–26 是原路线图
Task 5 交付的多来源必验项。结构化期望因此不再只看"最终正文含某个词"：
`codes`（原因码）、`basis`（逐店逐指标口径）、`diagnostics`、`coverage`、
`no_values` / `no_rows` / `no_total` 与 `calls`（一次提问里的多次工具调用）
都要能机器核对。

**21 / 23 / 25 的取证映射（刻意为之，别读成"淘系已可答支付"）**：原路线图表里
那三行拿 TB1 举例给支付数，但 Task 5.2c 已经把出库通道的付款时间口径实测判为
`disproved` —— 淘系问支付窗口类指标必须 fail closed，而 Q26 钉的正是同一条规则。
因此本卷把"混口径分列可执行"的证据换成两家都能答的 `erp_documents`（S1=6、
TB1=1，各自带 basis 与时间归属），把"未匹配退款可答披露"留在 TB1 的 `refund_amount`
上，把"关闭已付款单的 100/30/70 与销量不回活"放到有合成时间口径认证的 FX1 上。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo
from pathlib import Path
from unittest.mock import Mock, patch
from uuid import NAMESPACE_URL, uuid5

from bi_agent.agent import SessionState, answer
from bi_agent.catalog import ref_for_key
from bi_agent.llm import Message, ModelReply, ToolCall
from bi_agent.metrics import ToolResult
from bi_agent.sources import AFTERSALE_SOURCE, OUTSTOCK_SOURCE, TRADE_LIST_SOURCE
from tests.fakeconn import S1_REF

QUESTIONS_PATH = Path(__file__).with_name("questions.jsonl")
EXPECTED_QUESTION_COUNT = 26
# 多来源合成基准（原路线图 Task 5 的"新增多来源合成基准"一节）。
TB1 = "TB1"
PDD1 = "PDD1"
FX1 = "FX1"
TB1_REF = ref_for_key("shop", TB1)
PDD1_REF = ref_for_key("shop", PDD1)
FX1_REF = ref_for_key("shop", FX1)
ALL_NINE = ("paid_amount", "paid_orders", "erp_documents", "aov", "quantity",
            "product_paid_amount", "refund_amount", "cash_difference",
            "cohort_refund_rate")
BEIJING = ZoneInfo("Asia/Shanghai")
# 冻结时刻本身由 tests.test_db 提供（单一真源）；本模块只需要它的时区。


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
    assert len(questions) == EXPECTED_QUESTION_COUNT, (
        f"验收题数量应为{EXPECTED_QUESTION_COUNT}，实际{len(questions)}")
    ids = [question["id"] for question in questions]
    assert ids == [f"{index:02d}" for index in range(1, EXPECTED_QUESTION_COUNT + 1)], ids
    return questions


# ---------------------------------------------------------------------------
# 多来源合成基准：TB1（淘系出库）、PDD1（拼多多单据）、FX1（关闭已付款单）
# ---------------------------------------------------------------------------


def _ms(moment: datetime) -> int:
    return int(moment.timestamp() * 1000)


def _beijing(day: int, hour: int = 10, month: int = 9) -> datetime:
    return datetime(2026, month, day, hour, tzinfo=BEIJING)


def _sync_state(conn, *, source: str, entity: str, shop_id: str,
                spans: list[tuple[datetime, datetime]], data_as_of: datetime,
                quality_rule: str = "test-seed") -> None:
    """写覆盖凭证。质量口径与 `seed_business_case` 一致留 `test-seed`：

    合成种子**没有**跑过真实对账，所以这些店带着「来源质量未核验」的披露出数——
    把它写成 passed 口径版本就是拿测试夹具冒充对账证据。
    """
    for start, end in spans:
        conn.execute(
            "INSERT INTO bi.sync_state(source, entity, shop_id, watermark, covered, "
            "data_as_of, quality_status, quality_checked_at, quality_rule) "
            "VALUES (%s, %s, %s, %s, tstzmultirange(tstzrange(%s, %s, '[)')), %s, "
            "'passed', now(), %s) ON CONFLICT (source, entity, shop_id) DO UPDATE SET "
            "covered = bi.sync_state.covered + EXCLUDED.covered, "
            "data_as_of = greatest(coalesce(bi.sync_state.data_as_of, '-infinity'), "
            "EXCLUDED.data_as_of), quality_status = 'passed'",
            (source, entity, shop_id, end, start, end, data_as_of, quality_rule))


FULL_WINDOW = [(_beijing(1, 0), _beijing(8, 0))]
HOLE_WINDOWS = [(_beijing(3, 0), _beijing(5, 0)), (_beijing(6, 0), _beijing(8, 0))]


def _shop_row(conn, shop_id: str, platform: str, display_name: str,
              capabilities: list[str]) -> None:
    conn.execute(
        "INSERT INTO bi.shops(shop_id, platform, display_name, capabilities) "
        "VALUES (%s, %s, %s, %s) ON CONFLICT (shop_id) DO UPDATE SET "
        "platform = EXCLUDED.platform, display_name = EXCLUDED.display_name, "
        "capabilities = EXCLUDED.capabilities",
        (shop_id, platform, display_name, capabilities))


def _trade(conn, *, shop_id: str, erp_id: str, commercial_id: str, amount: str | None,
           day: int, source: str, unified_status: str | None = None,
           product_id: str = "P_TB") -> None:
    from bi_agent.sync import apply_trade, normalise_trade

    raw: dict[str, object] = {
        "sid": erp_id, "userId": shop_id, "tid": commercial_id,
        "payTime": _ms(_beijing(day)), "updTime": _ms(_beijing(day, 11)),
        "orders": [{"oid": f"{erp_id}-1", "tid": commercial_id, "num": "1",
                    "itemSysId": product_id}]}
    if amount is not None:
        raw["payAmount"] = amount
        raw["orders"][0]["payAmount"] = amount
    if unified_status:
        raw["unifiedStatus"] = unified_status
    record = normalise_trade(raw, source=source)
    assert record["normalization_status"] == "normal", record
    assert apply_trade(conn, record, batch_id=f"acceptance-{source}") or True


def _aftersale(conn, *, shop_id: str, aid: str, commercial_id: str | None,
               amount: str, day: int) -> None:
    from bi_agent.sync import apply_aftersale, normalise_aftersale

    complete = _beijing(day, 8)
    raw = {"aftersaleId": aid, "userId": shop_id, "rawRefundMoney": amount,
           "onlineStatus": 7, "status": 9, "modified": _ms(complete),
           "platformCompleteTime": _ms(complete), "refundId": f"PR-{aid}"}
    if commercial_id:
        raw["tid"] = commercial_id
    record = normalise_aftersale(raw)
    assert record["valid"]
    apply_aftersale(conn, record, batch_id="acceptance-aftersale")


def seed_multi_source_case(conn) -> None:
    """原路线图 Task 5 的多来源基准；S1 的既有数值一律不动。"""
    # TB1：出库通道，一单活动支付 100（09-02）+ 已匹配退款 30 + 未匹配成功退款 20。
    _shop_row(conn, TB1, "tb", "淘系测试店", list(ALL_NINE))
    _trade(conn, shop_id=TB1, erp_id="E-TB1", commercial_id="C-TB1",
           amount="100.00", day=2, source=OUTSTOCK_SOURCE)
    _aftersale(conn, shop_id=TB1, aid="R-TB1", commercial_id="C-TB1",
               amount="30.00", day=3)
    _aftersale(conn, shop_id=TB1, aid="R-TB2", commercial_id="C-TB-MISSING",
               amount="20.00", day=4)
    _sync_state(conn, source=OUTSTOCK_SOURCE, entity="orders", shop_id=TB1,
                spans=FULL_WINDOW, data_as_of=_beijing(8, 0))
    for entity in ("aftersales_occurrence", "aftersales_cohort"):
        _sync_state(conn, source=AFTERSALE_SOURCE, entity=entity, shop_id=TB1,
                    spans=FULL_WINDOW, data_as_of=_beijing(8, 0))
    # PDD1：三张 ERP 单据、单据覆盖完整，**没有任何支付能力**（不接入）。
    _shop_row(conn, PDD1, "pdd", "拼多多测试店", ["erp_documents"])
    for index, day in enumerate((2, 5, 6), start=1):
        _trade(conn, shop_id=PDD1, erp_id=f"E-PDD{index}",
               commercial_id=f"C-PDD{index}", amount=None, day=day,
               source=OUTSTOCK_SOURCE, product_id="P_PDD")
    _sync_state(conn, source=OUTSTOCK_SOURCE, entity="orders", shop_id=PDD1,
                spans=FULL_WINDOW, data_as_of=_beijing(8, 0))
    # FX1：交易通道 + 合成时间口径认证，一张"关闭但已付款 100 / 已匹配退款 30"的单，
    # 再加一条原单未到的成功退款 20（09-06）——它故意落在 Q25 第一问的窗口外，
    # 那一句要拿的是"这一张单 100/30/70"，而后续同批率那一问需要真未匹配。
    _shop_row(conn, FX1, "fxg", "关闭单测试店", list(ALL_NINE))
    _trade(conn, shop_id=FX1, erp_id="E-FX1", commercial_id="C-FX1",
           amount="100.00", day=2, source=TRADE_LIST_SOURCE,
           unified_status="CLOSED", product_id="P_FX")
    _aftersale(conn, shop_id=FX1, aid="R-FX1", commercial_id="C-FX1",
               amount="30.00", day=3)
    _aftersale(conn, shop_id=FX1, aid="R-FX2", commercial_id="C-FX-MISSING",
               amount="20.00", day=6)
    for entity, source in (("orders", TRADE_LIST_SOURCE),
                           ("aftersales_occurrence", AFTERSALE_SOURCE),
                           ("aftersales_cohort", AFTERSALE_SOURCE)):
        _sync_state(conn, source=source, entity=entity, shop_id=FX1,
                    spans=FULL_WINDOW, data_as_of=_beijing(8, 0))


def apply_coverage_holes(conn) -> None:
    """Q24 专用：把 TB1 的覆盖换成"有洞"的那两段（替换状态，不是追加）。"""
    for source in (OUTSTOCK_SOURCE, AFTERSALE_SOURCE):
        conn.execute("DELETE FROM bi.sync_state WHERE shop_id=%s AND source=%s",
                     (TB1, source))
    _sync_state(conn, source=OUTSTOCK_SOURCE, entity="orders", shop_id=TB1,
                spans=HOLE_WINDOWS, data_as_of=_beijing(5, 0))
    for entity in ("aftersales_occurrence", "aftersales_cohort"):
        _sync_state(conn, source=AFTERSALE_SOURCE, entity=entity, shop_id=TB1,
                    spans=HOLE_WINDOWS, data_as_of=_beijing(5, 0))


SEED_STEPS = {"multi_source": seed_multi_source_case,
              "coverage_holes": apply_coverage_holes}


# ---------------------------------------------------------------------------
# offline 脚本：按题预制最小参数的工具调用序列
# ---------------------------------------------------------------------------


def _model_script(question: dict) -> list[ModelReply]:
    """只提供真实模型大概率会给出的字段，其余依赖服务端合并逻辑；

    这正是离线验收要覆盖的协议路径。
    """
    qid = question["id"]
    query = {"shop_ids": [S1_REF]}
    if qid == "02":
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
    if qid == "15":
        # 修订版：全平台汇总按授权集原样递进去，由服务端逐店解析来源与能力。
        query = {"shop_ids": sorted([S1_REF, TB1_REF, PDD1_REF]),
                 "metrics": ["paid_amount"]}
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
    if qid == "21":
        mixed = {"shop_ids": sorted([S1_REF, TB1_REF]), "metrics": ["paid_amount"]}
        separate = {**mixed, "group_by": "shop", "basis_policy": "separate"}
        documents = {"shop_ids": sorted([S1_REF, TB1_REF]), "metrics": ["erp_documents"],
                     "group_by": "shop", "basis_policy": "separate"}
        return [_call_reply("query_business", mixed),
                _call_reply("query_business", separate, call_id="call_2"),
                _reply(text="两个口径互不兼容，本次不汇总也不排名。"),
                _call_reply("query_business", documents, call_id="call_3"),
                _reply(text="按店铺分列，各带自己的口径。")]
    if qid == "22":
        return [_call_reply("query_business", {
            "shop_ids": [PDD1_REF], "metrics": ["paid_amount", "paid_orders"]}),
            _reply(text="拼多多无支付口径能力，本次不给支付金额与支付订单数。"),
            _call_reply("query_business", {
                "shop_ids": [PDD1_REF], "metrics": ["erp_documents"]},
                call_id="call_2"),
            _reply(text="ERP单据数3，它不是支付订单数。")]
    if qid == "23":
        return [_call_reply("query_business", {
            "shop_ids": [TB1_REF], "metrics": ["refund_amount"]}),
            _reply(text="退款发生50元，其中1条未匹配。"),
            _call_reply("query_business", {
                "shop_ids": [TB1_REF],
                "metrics": ["cash_difference", "cohort_refund_rate"]},
                call_id="call_2"),
            _reply(text="出库通道的付款时间口径未认证，收支差与同批率不能出数。")]
    if qid == "24":
        return [_call_reply("query_business", {
            "shop_ids": sorted([S1_REF, TB1_REF]), "metrics": ["refund_amount"],
            "start": "2026-09-01", "end": "2026-09-08"}),
            _reply(text="两家共同完整覆盖的是9月3日至5日与9月6日至8日。")]
    if qid == "25":
        return [_call_reply("query_business", {
            "shop_ids": [FX1_REF], "start": "2026-09-01", "end": "2026-09-04",
            "metrics": ["paid_amount", "refund_amount", "cash_difference"]}),
            _reply(text="已认证支付100元、退款30元、差额70元。"),
            _call_reply("query_business", {
                "shop_ids": [FX1_REF],
                "metrics": ["refund_amount", "cohort_refund_rate"]},
                call_id="call_2"),
            _reply(text="同批率只含已匹配退款。"),
            _call_reply("query_business", {
                "shop_ids": [FX1_REF], "metrics": ["quantity"],
                "group_by": "product"}, call_id="call_3"),
            _reply(text="关闭单不回到商品有效销量。")]
    if qid == "26":
        return [_call_reply("query_business", {
            "shop_ids": [TB1_REF], "metrics": ["paid_amount"]}),
            _reply(text="不能：接口扫描覆盖与支付时间覆盖是两件事，"
                        "付款时间口径未经对照认证，本次不按完整支付窗口出数。")]
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
        "basis_policy": request.basis_policy,
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


def _limitation_codes(result: ToolResult) -> list[str]:
    from bi_agent.business_query.nodes import _limitation_codes as codes

    return codes(list(result.limitations))


def _basis_keys(result: ToolResult) -> set[tuple[str, str, str, str]]:
    return {(str(item["shop_id"]), str(item["metric"]), str(item["basis"]),
             str(item["time_basis"])) for item in result.basis}


def _coverage_shape(result: ToolResult) -> dict:
    coverage = result.coverage
    return {"status": coverage.status,
            "start": coverage.start.isoformat() if coverage.start else None,
            "end": coverage.end.isoformat() if coverage.end else None,
            "gaps": list(coverage.gaps),
            "suggested_window": list(coverage.suggested_window)
            if coverage.suggested_window else None}


def _structured_problems(expected: dict, result: ToolResult) -> list[str]:
    """结构化期望：口径、原因码、诊断与覆盖都要能机器核对。

    设计 §6 与计划 Task 11 都要求"不能只用最终自然语言包含某个词判定通过"。
    """
    problems: list[str] = []
    codes = _limitation_codes(result)
    for code in expected.get("codes", []):
        if code not in codes:
            problems.append(f"原因码期望{code}，实际{codes}")
    for code in expected.get("not_codes", []):
        if code in codes:
            problems.append(f"原因码不应出现{code}，实际{codes}")
    for entry in expected.get("basis", []):
        shop_id, metric, basis, time_basis = entry
        if (shop_id, metric, basis, time_basis) not in _basis_keys(result):
            problems.append(f"口径凭证缺{entry}，实际{sorted(_basis_keys(result))}")
    for key, expected_diagnostics in (expected.get("diagnostics") or {}).items():
        actual = result.diagnostics.get(key)
        if not isinstance(actual, dict):
            problems.append(f"诊断缺{key}，实际{result.diagnostics}")
            continue
        for field, value in expected_diagnostics.items():
            if str(actual.get(field)) != str(value):
                problems.append(f"诊断{key}.{field}期望{value}，实际{actual.get(field)}")
    for field, value in (expected.get("coverage") or {}).items():
        actual = _coverage_shape(result).get(field)
        if actual != value:
            problems.append(f"覆盖{field}期望{value}，实际{actual}")
    if expected.get("no_values") and result.data:
        problems.append(f"期望无成功数值，实际{result.data}")
    if expected.get("no_rows") and result.data:
        problems.append(f"期望零行（真实没有），实际{result.data}")
    if expected.get("no_total") and any(
            "shop_id" not in row for row in result.data):
        problems.append("分列结果里出现了不带店铺的合计行")
    return problems


def _run_turn(question: str, state: SessionState, model, conn, allowed, now, store):
    from bi_agent.runtime import TurnContext

    turn_number = len([message for message in state.turns if message.role == "user"])
    chat_id = uuid5(NAMESPACE_URL, f"acceptance-chat:{state.subject}")
    return answer(
        question,
        state,
        model=model,
        conn=conn,
        allowed_shop_ids=allowed,
        now=now,
        run_store=store,
        turn_context=TurnContext(
            chat_id=chat_id,
            user_message_id=uuid5(
                NAMESPACE_URL,
                f"acceptance-message:{state.subject}:{turn_number}:{question}",
            ),
            subject_id=state.subject,
        ),
    )


def _verify_turn(expected: dict, turn, recorded: list, store, recorded_start: int,
                 allowed: frozenset[str]) -> list[str]:
    problems: list[str] = []
    if expected.get("clarify"):
        if not turn.clarification:
            problems.append("期望澄清，但未产生澄清")
        if recorded:
            problems.append("期望先澄清再取数，但已经执行了查询")
        if turn.results:
            problems.append("澄清回合不应有工具结果")
        for fragment in expected.get("clarify_contains", []):
            if fragment not in (turn.clarification or ""):
                problems.append(f"澄清未包含“{fragment}”")
        for fragment in expected.get("clarify_absent", []):
            if fragment in (turn.clarification or ""):
                problems.append(f"澄清不该宣布“{fragment}”")
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
        this_turn = recorded[recorded_start:]
        for index, call in enumerate(expected.get("calls", [])):
            if index >= len(this_turn):
                problems.append(f"第{index + 1}次工具调用未发生")
                continue
            problems.extend(_param_problems(call.get("parameters", {}),
                                            this_turn[index]))
        request = recorded[-1]
        problems.extend(_param_problems(expected.get("parameters", {}), request))
        if expected.get("scope_only_authorized"):
            # “全公司/全平台”这类说法不能变成越权：递进工具的店集就是授权集。
            if sorted(request.shop_ids) != sorted(allowed):
                problems.append(f"工具范围应等于授权集{sorted(allowed)}，"
                                f"实际{sorted(request.shop_ids)}")
            leaked = {shop for call in recorded for shop in call.shop_ids
                      if shop not in allowed}
            if leaked:
                problems.append(f"越权店铺进了查询：{sorted(leaked)}")
    if expected.get("status"):
        if turn.results:
            actual_status = turn.results[-1].status
        else:
            actual_status = "no_result"
        if actual_status != expected["status"]:
            problems.append(f"status期望{expected['status']}，实际{actual_status}")
    if expected.get("domain_status"):
        statuses = [str(run["status"]) for run in store.runs.values()]
        if not statuses or statuses[-1] != expected["domain_status"]:
            problems.append(f"domain_status期望{expected['domain_status']}，"
                            f"实际{statuses[-1] if statuses else '无运行记录'}")
    result = turn.results[-1] if turn.results else None
    if expected.get("values") and result is not None:
        problems.extend(_value_problems(expected["values"], result))
    if result is not None:
        problems.extend(_structured_problems(expected, result))
    for fragment in expected.get("text_contains", []):
        if fragment not in (turn.text or ""):
            problems.append(f"回答未包含“{fragment}”")
    return problems


def _expected_list(question: dict) -> list[dict]:
    return (question["expected"] if isinstance(question["expected"], list)
            else [question["expected"]] * len(question["turns"]))


def _allowed_for(question: dict) -> frozenset[str]:
    return frozenset(question.get("allowed") or ["S1"])


def _run_questions(questions: list[dict], conn, *, make_model, prefix: str,
                   offline: bool) -> tuple[list[dict], int]:
    """逐题跑一遍：每题独立 savepoint、独立授权集、独立预制模型。

    `savepoint` 是必要的而不是讲究好看：多来源题会替换 TB1 的覆盖状态，不回滚就会
    把后面题目的答案一起改掉——那正是"用一题的窗口冒充另一题"的错。
    """
    from tests.test_db import FROZEN_NOW

    import bi_agent.metrics as metrics_module

    real_query = metrics_module.query_business
    report: list[dict] = []
    failures = 0
    for question in questions:
        started = time.monotonic()
        recorded: list = []
        conn.execute("SAVEPOINT acceptance_question")

        def spy(connection, request, **kwargs):
            recorded.append(request)
            return real_query(connection, request, **kwargs)

        for step in question.get("seeds", []):
            SEED_STEPS[step](conn)
        state = SessionState(subject=f"{prefix}-{question['id']}")
        allowed = _allowed_for(question)
        model = make_model(question)
        expected_list = _expected_list(question)
        problems: list[str] = []
        try:
            with patch("bi_agent.business_query.nodes.metrics.query_business",
                       side_effect=spy):
                for turn_text, expected in zip(question["turns"], expected_list):
                    store = _memory_store(allowed)
                    recorded_start = len(recorded)
                    turn = _run_turn(turn_text, state, model, conn, allowed,
                                     FROZEN_NOW, store)
                    state = turn.state
                    problems.extend(_verify_turn(expected, turn, recorded, store,
                                                 recorded_start, allowed))
                    if problems:
                        break
        except Exception as exc:  # noqa: BLE001
            problems.append(f"异常：{type(exc).__name__}: {exc}")
        finally:
            conn.execute("ROLLBACK TO acceptance_question")
        duration = time.monotonic() - started
        failures += 1 if problems else 0
        entry = {"id": question["id"], "status": "pass" if not problems else "fail",
                 "problems": problems, "duration_s": round(duration, 3),
                 "tokens": "unknown", "error_class":
                     problems[0].split("：")[0] if problems else None}
        if not offline:
            entry["human_review"] = expected_list[0].get("clarify", False)
        report.append(entry)
        print(json.dumps(entry, ensure_ascii=False))
    return report, failures


def _memory_store(allowed: frozenset[str]):
    from bi_agent.runtime import MemoryQueryRunStore

    return MemoryQueryRunStore(forbidden_values=set(allowed))


def run_offline() -> int:
    """模拟模型 + 真实测试数据库：验证协议与业务执行。"""
    import os

    import psycopg

    if not os.getenv("BI_TEST_ADMIN_DSN"):
        print(json.dumps({"mode": "offline", "result": "skipped",
                          "reason": "未配置BI_TEST_ADMIN_DSN"}))
        return 2
    from tests.test_db import seed_business_case

    # 单事务运行并在结束时回滚，不向共享测试库提交任何数据
    conn = psycopg.connect(os.environ["BI_TEST_ADMIN_DSN"])
    seed_business_case(conn)
    seed_multi_source_case(conn)
    conn.execute(
        "INSERT INTO bi.shops(shop_id, platform, display_name) "
        "VALUES ('S9','fxg','店铺A') ON CONFLICT (shop_id) DO NOTHING")
    from .test_db import ALL_CAPABILITIES, set_capabilities

    set_capabilities(conn, "S9", *ALL_CAPABILITIES)
    questions = _load_questions()
    report, failures = _run_questions(questions, conn,
                                      make_model=_offline_model, prefix="acceptance",
                                      offline=True)
    conn.rollback()
    conn.close()
    summary = {"mode": "offline", "total": len(questions),
               "passed": len(questions) - failures, "failures": failures,
               "result": "pass" if failures == 0 else "fail",
               "note": "离线模式不能证明模型理解准确率，也不构成任何平台真实来源就绪的证据；"
                       "live结果见docs/demo.md"}
    print(json.dumps(summary, ensure_ascii=False))
    return 0 if failures == 0 else 1


def _offline_model(question: dict) -> Mock:
    model = Mock()
    model.complete.side_effect = _model_script(question)
    return model


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
    from bi_agent.runtime import TurnContext
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
    store = _memory_store(frozenset({"S1"}))
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
    """选中provider在合成测试DB跑26题；澄清与因果边界人工核看。"""
    import os

    import psycopg

    model = _real_model_or_none()
    if model is None:
        return 0
    if not os.getenv("BI_TEST_ADMIN_DSN"):
        print(json.dumps({"result": "未实测", "reason": "缺少测试数据库配置"}))
        return 0
    from tests.test_db import seed_business_case

    conn = psycopg.connect(os.environ["BI_TEST_ADMIN_DSN"])
    seed_business_case(conn)
    seed_multi_source_case(conn)
    conn.execute(
        "INSERT INTO bi.shops(shop_id, platform, display_name) "
        "VALUES ('S9','fxg','店铺A') ON CONFLICT (shop_id) DO NOTHING")
    from .test_db import ALL_CAPABILITIES, set_capabilities

    set_capabilities(conn, "S9", *ALL_CAPABILITIES)
    questions = _load_questions()
    report, failures = _run_questions(questions, conn,
                                      make_model=lambda _question: model,
                                      prefix="live", offline=False)
    conn.rollback()
    conn.close()
    print(json.dumps({"mode": "live", "total": len(questions),
                      "passed": len(questions) - failures, "failures": failures,
                      "result": "pass" if failures == 0 else "fail",
                      "note": "澄清与因果边界仍需人工核看；"
                              "offline通过不代表live可用，反之亦然"}, ensure_ascii=False))
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
