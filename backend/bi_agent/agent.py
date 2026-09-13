"""单Agent对话：两个工具、有限回合、会话筛选与不透明引用。

模型只看到店铺的 ent- 引用与聚合结果，永远看不到真实店名、品名或 ERP 主键；
真名在后端确定性地改写到正文里，权限验证在映射前后都执行；
金额一律来自确定性工具结果，模型不重算。
"""

from __future__ import annotations

import json
import re
import time as time_module
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any, Iterator, Literal
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field

from .business_query.tool import (
    execute_business_query_tool,
    to_model_result as _to_model_result,
    to_public_artifact as _to_public_artifact,
)
from .business_query import BusinessQueryContext
from .commerce.models import DomainContext
from .commerce.tool import commerce_request_schema, execute_commerce_tool
from .catalog import (
    Catalog,
    build_catalog,
    ref_for_key,
    render_display_text,
    shop_display_labels,
)
from .llm import ChatModel, Message, ModelError, ModelReply, ToolCall
from .metrics import QueryRequest, ToolResult, resolve_period
from .promotion import PromotionRequest, evaluate_promotion
from .runtime import MemoryQueryRunStore, PostgresQueryRunStore, QueryRunStore, TurnContext

BEIJING = ZoneInfo("Asia/Shanghai")
TOTAL_BUDGET_SECONDS = 30
MAX_TOOL_CALLS = 4
MAX_MODEL_TURNS = 5
MAX_KEPT_TURNS = 6

_SYSTEM_PROMPT = """你是内部电商经营助手。当前北京时间：{now:%Y-%m-%d %H:%M}（Asia/Shanghai）。
只能使用三个工具：
- query_business：按已确认口径查询经营指标，日期end排他；shop_ids 只能填 ent- 形式的店铺引用，
  可用引用：{ref_doc}。引用与真实店名的对应关系你看不到，也不要猜。
- analyze_product_performance：查**一个指定商品**在获准店铺内的跨店经营报告与七日趋势；
  product 只能填 ent- 商品引用或一段商品文字，范围用 scope（all_authorized 或显式引用/平台）。
  商品文字只用于找候选：命中多个候选时工具返 needs_input 并附候选引用，必须把候选问回用户，
  不能自己选一个；解析不出商品是 missing_data，**绝不能说成销量为 0**。
- evaluate_promotion：仅按当前用户明确假设测算预算；当前未取得真实推广消耗。
支持指标：支付金额、支付订单数、客单价、ERP单据数、退款发生额、期间收支差额、同批退款率、商品销量、商品支付金额。
支持维度：合计、按日、按店铺、按商品。
结果里的 shop_ref/product_ref 是实体引用：正文直接引用它们，系统会负责换成经营者可读的名称。
「销售额」在未确认支付/出库口径前不能直接当支付金额；只能按店铺筛，不能按商品名筛
（要按商品查只能走 analyze_product_performance，它自己会先把文字换成商品引用）。
商品报告里的 product_gross_profit_reference / product_gross_margin_reference 是**参考指标**：
只含成本与分摊都已核验的普通销售行，未扣售后、平台费、运费与广告费，不是净利润；
它与 ERP 单据毛利是两个口径面，不能相加、相除，也不能互相分摊。null 是证据不足，不是 0。
partial 只覆盖 evaluated_scope 里的店铺，excluded_scope 里每家店都带原因：不能把 partial 的
合计说成“所有店铺合计”，也不能拿它做全量排名；不含 shop_ref 的行只是已评估集合的合计。
每个结果都带 basis（统计口径）与 time_basis（时间归属）：同名指标不代表同一口径，
不能自动同义化。平台/店铺对比只展示 basis 相同且已认证的结果；basis 或 time_basis 不同
时不要汇总、不要算增长率、不要排名（工具会以 basis_incompatible 拒绝，提示按店铺分列，
必要时用 basis_policy=separate 重新发起 group_by=shop 的查询）。
未被认证的付款时间口径只能作为可观测样本转述，不得说成“完整支付窗口”。
数据共同截止与覆盖限制会在工具结果中给出；未覆盖的历史不能编造数字。
已知能力之外（广告实耗、全平台汇总、净利润）明确说不可用。
不要重算金额，不要把相关性写成因果；数字以工具结果为准。"""

_METRIC_KEYWORDS: tuple[tuple[str, str], ...] = (
    ("同批退款率", "cohort_refund_rate"),
    ("商品支付金额", "product_paid_amount"),
    ("支付金额", "paid_amount"),
    ("支付额", "paid_amount"),
    ("支付订单数", "paid_orders"),
    ("客单价", "aov"),
    ("退款发生", "refund_amount"),
    ("实际退款", "refund_amount"),
    ("退款", "refund_amount"),
    ("收支差", "cash_difference"),
    ("ERP单", "erp_documents"),
    ("销量", "quantity"),
)

_PII_PATTERNS = (
    re.compile(r"1[3-9]\d{9}"),                      # 手机号
    re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+"),          # 邮箱
    re.compile(r"\d{10,}"),                          # 订单号类长数字
)


class SessionState(BaseModel):
    model_config = ConfigDict(extra="forbid")
    subject: str
    shop_refs: dict[str, str] = Field(default_factory=dict)   # shop_id -> ent- 引用
    filters: dict[str, object] = Field(default_factory=dict)
    turns: list[Message] = Field(default_factory=list)


class TurnResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str
    results: list[ToolResult] = Field(default_factory=list)
    # 已投影的公开载荷（带 entities）：展示层直接用，不再二次投影。
    artifacts: list[dict[str, object]] = Field(default_factory=list)
    clarification: str | None = None
    error_code: str | None = None
    state: SessionState


class ChatEvent(BaseModel):
    """浏览器SSE事件；正文永远放入一行JSON data字段。"""

    event: Literal["status", "artifact", "message", "error", "done"]
    data: dict[str, object]


def explicit_assumptions(question: str) -> dict[str, object]:
    """只识别明确表达的金额/比率/日期；不确定表达交给澄清。"""
    values: dict[str, object] = {}
    match = re.search(r"(?:假设|预计).*?销售额\s*(\d+(?:\.\d+)?)\s*(万)?元", question)
    if match:
        values["sales_estimate"] = Decimal(match.group(1)) * (10000 if match.group(2) else 1)
    ratio = re.search(r"(?:推广费|费用率).*?(\d+(?:\.\d+)?)\s*%", question)
    if ratio:
        values["target_ratio"] = Decimal(ratio.group(1)) / 100
    budget = re.search(r"预算\s*(\d+(?:\.\d+)?)\s*元", question)
    if budget:
        values["budget"] = Decimal(budget.group(1))
    spend = re.search(r"已花\s*(\d+(?:\.\d+)?)\s*元", question)
    if spend:
        values["assumed_spend"] = Decimal(spend.group(1))
    through = re.search(r"(?:实耗统计到|实耗到|已花到)\s*(\d{1,2})月(\d{1,2})日", question)
    if through:
        values["_spent_through_md"] = (int(through.group(1)), int(through.group(2)))
    return values


# ---------------------------------------------------------------------------
# 会话辅助
# ---------------------------------------------------------------------------


def _fetch_shops(conn, allowed_shop_ids: frozenset[str]) -> list[tuple[str, str]]:
    rows = conn.execute(
        "SELECT shop_id, display_name FROM reporting.v_shops WHERE shop_id = ANY(%s)",
        (sorted(allowed_shop_ids),),
    ).fetchall()
    return [(str(row[0]), str(row[1] or "")) for row in rows]


def _ensure_shop_refs(state: SessionState,
                      allowed_shop_ids: frozenset[str]) -> SessionState:
    """引用由 (kind, shop_id) 纯派生：重排、换授权范围、重跑都不会换号。"""
    refs = dict(state.shop_refs)
    changed = False
    for shop_id in sorted(allowed_shop_ids):
        ref = ref_for_key("shop", shop_id)
        if refs.get(shop_id) != ref:
            refs[shop_id] = ref
            changed = True
    for shop_id in list(refs):
        if shop_id not in allowed_shop_ids:
            refs.pop(shop_id)
            changed = True
    if changed:
        state = state.model_copy(update={"shop_refs": refs})
    return state


def _detect_metrics(question: str) -> list[str]:
    found: list[str] = []
    for keyword, metric in _METRIC_KEYWORDS:
        if keyword in question and metric not in found:
            found.append(metric)
    return found


def _detect_shop_ids(question: str, shops: list[tuple[str, str]],
                     refs: dict[str, str]) -> tuple[list[str], list[str]]:
    """返回 (匹配的shop_ids, 同名店铺歧义名单)。"""
    matched: list[str] = []
    ambiguous: list[str] = []
    ref_reverse = {ref: shop_id for shop_id, ref in refs.items()}
    for ref, shop_id in ref_reverse.items():
        if ref in question:
            matched.append(shop_id)
    for shop_id, display_name in shops:
        if display_name and display_name in question:
            if display_name not in matched and shop_id not in matched:
                matched.append(shop_id)
    # 同名检查：问题提到的名字对应多家店铺
    names = [name for _, name in shops if name and name in question]
    for name in set(names):
        owners = [shop_id for shop_id, display in shops if display == name]
        if len(owners) > 1:
            ambiguous.append(name)
    return matched, sorted(set(ambiguous))


def _contains_pii(question: str) -> bool:
    return any(pattern.search(question) for pattern in _PII_PATTERNS)


def _anonymize_question(question: str, shops: list[tuple[str, str]],
                        refs: dict[str, str]) -> str:
    """把用户问题里的真实店名换成引用：同名店己在前面的歧义澄清里拦下。"""
    for shop_id, display_name in shops:
        if display_name:
            question = question.replace(display_name, refs.get(shop_id, "未授权店铺"))
    return question


def _trim_turns(turns: list[Message]) -> list[Message]:
    """整体删除一个回合裁剪；不留孤立tool结果，不丢当前回合provider元数据。"""
    turn_starts = [index for index, msg in enumerate(turns) if msg.role == "user"]
    while len(turn_starts) > MAX_KEPT_TURNS:
        cut = turn_starts[1]  # 下一回合起点即第一回合整体边界
        turns = turns[cut:]
        turn_starts = [index - cut for index in turn_starts if index - cut >= 0]
    return turns


def to_model_result(result: ToolResult, catalog: Catalog) -> dict[str, object]:
    """模型只看引用与必要聚合结果。"""
    return _to_model_result(result, catalog)


def to_public_artifact(result: ToolResult, catalog: Catalog) -> dict[str, object]:
    """聊天附件不保存ERP ID，但随附授权范围内的真实展示名。"""
    return _to_public_artifact(result, catalog)


def _display_map(artifacts: list[dict[str, object]]) -> dict[str, str | None]:
    """从已投影的公开载荷里收集 ref -> 展示名，用于正文确定性改写。"""
    mapping: dict[str, str | None] = {}
    for payload in artifacts:
        entities = payload.get("entities")
        if not isinstance(entities, list):
            continue
        for entity in entities:
            if isinstance(entity, dict) and isinstance(entity.get("ref"), str):
                name = entity.get("display_name")
                mapping[entity["ref"]] = name if isinstance(name, str) else None
    return mapping


def encode_sse(event: ChatEvent) -> bytes:
    data = json.dumps(event.data, ensure_ascii=False, default=str, separators=(",", ":"))
    return f"event: {event.event}\ndata: {data}\n\n".encode("utf-8")


def run_chat_turn(conn, chat_id: UUID, subject: str, content: str, *, model: ChatModel,
                  allowed_shop_ids: frozenset[str], now: datetime) -> Iterator[ChatEvent]:
    """保存可见消息并输出有限阶段事件；调用方负责会话锁和连接生命周期。"""
    from .chats import (
        load_chat_context,
        save_assistant_message,
        save_user_message,
        update_chat_filters,
    )

    filters, history = load_chat_context(conn, subject, chat_id)
    state = SessionState(
        subject=subject,
        filters=filters,
        turns=[Message(role=role, content=text) for role, text in history],
    )
    saved_user = save_user_message(conn, chat_id, subject, content)
    yield ChatEvent(event="status", data={"stage": "thinking"})
    try:
        turn = answer(
            content,
            state,
            model=model,
            conn=conn,
            allowed_shop_ids=allowed_shop_ids,
            now=now,
            run_store=PostgresQueryRunStore(
                conn, forbidden_values=allowed_shop_ids,
            ),
            turn_context=TurnContext(
                chat_id=chat_id,
                user_message_id=saved_user.id,
                subject_id=subject,
            ),
        )
        artifacts = list(turn.artifacts)
        if artifacts:
            yield ChatEvent(event="status", data={"stage": "querying"})
            for artifact in artifacts:
                yield ChatEvent(event="artifact", data=artifact)
        if turn.error_code not in {
            "artifact_persistence_failed", "result_contract_violation"
        }:
            update_chat_filters(conn, chat_id, subject, turn.state.filters)
        if turn.error_code:
            message = (
                turn.text
                if turn.error_code in {
                    "artifact_persistence_failed", "result_contract_violation"
                }
                else "模型服务暂时不可用，请稍后重试。"
            )
            save_assistant_message(
                conn, chat_id, subject, message, artifacts, status="error",
            )
            yield ChatEvent(event="error", data={
                "code": turn.error_code,
                "message": message,
            })
            yield ChatEvent(event="done", data={"status": "error"})
            return
        saved = save_assistant_message(
            conn, chat_id, subject, turn.clarification or turn.text or "暂未获得回答",
            artifacts, status="complete",
        )
        yield ChatEvent(event="status", data={"stage": "answering"})
        yield ChatEvent(event="message", data=saved.model_dump(mode="json"))
        yield ChatEvent(event="done", data={"status": "complete"})
    except Exception:  # noqa: BLE001 - 不向浏览器泄露模型、数据库或堆栈细节
        message = "本轮回答未完成，请稍后重试。"
        try:
            save_assistant_message(conn, chat_id, subject, message, [], status="error")
        except Exception:  # noqa: BLE001 - 数据库不可用时仍向浏览器结束事件
            pass
        yield ChatEvent(event="error", data={"code": "unavailable", "message": message})
        yield ChatEvent(event="done", data={"status": "error"})


# ---------------------------------------------------------------------------
# 工具执行
# ---------------------------------------------------------------------------


def _correction_message(call_id: str, problems: list[str]) -> Message:
    return Message(role="tool", tool_call_id=call_id,
                   content=json.dumps({"error": "invalid_parameters",
                                       "problems": problems}, ensure_ascii=False))


def _handle_evaluate_promotion(call: ToolCall, *, question: str, now: datetime) -> (
        ToolResult) | list[str]:
    args = dict(call.arguments or {})
    values = explicit_assumptions(question)
    if "start" not in args or "end" not in args:
        period = resolve_period(question, now=now)
        if period:
            args.setdefault("start", period[0].isoformat())
            args.setdefault("end", period[1].isoformat())
    if "spent_through" not in args:
        md = values.get("_spent_through_md")
        if isinstance(md, tuple):
            through = date(now.year, md[0], md[1]) + timedelta(days=1)
            args.setdefault("spent_through", through.isoformat())
    try:
        request = PromotionRequest.model_validate(args)
    except Exception as exc:  # noqa: BLE001
        return [str(exc).split("\n")[0]]
    return evaluate_promotion(request, confirmed_inputs={
        key: value for key, value in values.items() if not key.startswith("_")
    }, now=now)


# ---------------------------------------------------------------------------
# 主回合
# ---------------------------------------------------------------------------


def _tool_schemas() -> list[dict[str, object]]:
    return [
        {"type": "function", "function": {
            "name": "query_business",
            "description": "按已确认口径查询经营指标，日期end排他；shop_ids 只填 ent- 店铺引用；"
                           "跨口径范围要分列时用 basis_policy=separate 且 group_by=shop",
            "parameters": QueryRequest.model_json_schema()}},
        {"type": "function", "function": {
            "name": "analyze_product_performance",
            "description": "查一个指定商品在获准店铺范围内的跨店经营报告与七日趋势；"
                           "product 用 ent- 商品引用或商品文字（歧义会返回 needs_input 与候选，"
                           "不要自己选）；只能参考的毛利未扣售后/平台费/运费/广告费",
            "parameters": commerce_request_schema()}},
        {"type": "function", "function": {
            "name": "evaluate_promotion",
            "description": "仅按当前用户明确假设测算预算；当前未取得真实推广消耗",
            "parameters": PromotionRequest.model_json_schema()}},
    ]


# 工具预算或模型回合上限用尽时的兜底文案：不把内部占位语当成回答。
NO_TEXT_WITH_RESULTS = ("本轮已取得确定性查询结果（见下方数据），但没能组织成文字总结，"
                       "请重试或换个问法。")
NO_TEXT_WITHOUT_RESULTS = "本轮没能给出回答，请重试或换个问法。"


def _deterministic_summary(results: list[object]) -> str | None:
    """从已持久化的领域结果生成兜底文本；没有任何成功结果时返回 None。"""
    from .response_summary import render_result_summary

    for outcome in reversed(results):
        domain_result = getattr(outcome, "domain_result", outcome)
        if getattr(domain_result, "status", None) not in ("success", "missing_data"):
            continue
        text = render_result_summary(domain_result)
        if text:
            return text
    return None


def _final_text_answer(model: ChatModel, messages: list[Message],
                       deadline: float) -> str | None:
    """补一次不挂工具的文本回合，让模型用已拿到的确定性结果作答。

    工具预算或回合上限用尽时，历史里已经附齐了工具结果：此时不再提工具，
    只允许模型输出正文。补答失败不影响已取得的确定性结果。
    """
    remaining = deadline - time_module.monotonic()
    if remaining <= 0:
        return None
    try:
        reply = model.complete(messages, [], timeout_s=remaining)
    except Exception:  # noqa: BLE001 - 补答失败不能吞掉已取得的确定性结果
        return None
    text = (reply.text or "").strip()
    if not text:
        return None
    if not reply.tool_calls:
        # 只有本回合没再要求工具调用时才能入历史，否则下一回合会拿到孤立 tool_calls。
        messages.append(reply.as_message())
    return text


def answer(question: str, state: SessionState, *, model: ChatModel, conn,
           allowed_shop_ids: frozenset[str], now: datetime,
           run_store: QueryRunStore | None = None,
           turn_context: TurnContext | None = None) -> TurnResult:
    deadline = time_module.monotonic() + TOTAL_BUDGET_SECONDS
    if run_store is None:
        run_store = MemoryQueryRunStore(forbidden_values=allowed_shop_ids)
    if turn_context is None:
        turn_context = TurnContext(
            chat_id=uuid5(NAMESPACE_URL, f"business-query-chat:{state.subject}"),
            user_message_id=uuid4(),
            subject_id=state.subject,
        )
    filters = dict(state.filters)
    if _contains_pii(question):
        return TurnResult(
            text="请删除个人信息（手机号、邮箱、订单号）后重新提问；本工具只输出聚合结果。",
            clarification="请删除个人信息后重新提问", state=state)
    shops = _fetch_shops(conn, allowed_shop_ids)
    state = _ensure_shop_refs(state, allowed_shop_ids)
    # 只报引用：真实店名与 ERP 主键一律不进入提示词。
    ref_doc = "，".join(sorted(set(state.shop_refs.values())))

    # 澄清：销售额口径
    if "销售额" in question and "支付" not in question and "出库" not in question \
            and "假设" not in question and "预计" not in question:
        return TurnResult(
            text="", clarification="“销售额”需要确认口径：指买家已支付金额还是出库金额？"
                                   "请确认后我再查询。",
            state=state)

    values = explicit_assumptions(question)
    period = resolve_period(question, now=now)
    detected_metrics = _detect_metrics(question)
    matched_shops, ambiguous_names = _detect_shop_ids(question, shops, state.shop_refs)
    if ambiguous_names:
        names = "、".join(ambiguous_names)
        return TurnResult(
            text="", clarification=f"存在多个同名店铺（{names}），请加上平台或指明店铺引用。",
            state=state)

    tools = _tool_schemas()
    system = Message(role="system",
                     content=_SYSTEM_PROMPT.format(now=now.astimezone(BEIJING),
                                                   ref_doc=ref_doc or "（无店铺）"))
    messages = [system] + list(state.turns)
    messages.append(Message(role="user",
                            content=_anonymize_question(question, shops, state.shop_refs)))
    results: list[ToolResult] = []
    artifacts: list[dict[str, object]] = []
    calls_used = 0
    correction_used = False
    text_answer: str | None = None
    fallback_text: str | None = None
    last_error: str | None = None
    error_code: str | None = None
    business_attempt_no = 0
    commerce_attempt_no = 0

    for _ in range(MAX_MODEL_TURNS):
        remaining = deadline - time_module.monotonic()
        if remaining <= 0:
            last_error = "本次30秒预算已耗尽，已保留已取得的确定性结果"
            break
        try:
            reply: ModelReply = model.complete(messages, tools, timeout_s=remaining)
        except ModelError as exc:
            last_error = f"模型调用失败（{exc.code}）；固定查询入口仍可用"
            error_code = exc.code
            break
        messages.append(reply.as_message())
        if reply.text and reply.text.strip():
            # 正文与工具调用混发时先留下正文，兼作后续失败的兜底。
            fallback_text = reply.text.strip()
        if not reply.tool_calls:
            text_answer = fallback_text
            break
        stop_after_batch = False
        for call in reply.tool_calls:
            if calls_used >= MAX_TOOL_CALLS:
                messages.append(Message(role="tool", tool_call_id=call.id,
                                        content=json.dumps(
                                            {"error": "budget_exhausted",
                                             "detail": "本次对话工具调用已达上限"},
                                            ensure_ascii=False)))
                stop_after_batch = True
                continue
            if call.name == "query_business":
                business_attempt_no += 1
                execution = execute_business_query_tool(
                    call,
                    state,
                    BusinessQueryContext(
                        chat_id=turn_context.chat_id,
                        user_message_id=turn_context.user_message_id,
                        subject_id=turn_context.subject_id,
                        question=question,
                        previous_filters=filters,
                        shop_refs=state.shop_refs,
                        allowed_shop_ids=allowed_shop_ids,
                        now=now,
                        deadline=deadline,
                        attempt_no=business_attempt_no,
                    ),
                    conn,
                    run_store,
                )
                if execution.domain_result.status.value == "needs_input":
                    if correction_used:
                        last_error = "参数两次非法，已停止本次回答"
                        stop_after_batch = True
                        break
                    correction_used = True
                    problems = (execution.domain_result.error.problems
                                if execution.domain_result.error is not None
                                else ["invalid_parameters"])
                    messages.append(_correction_message(call.id, problems))
                    continue
                if (
                    execution.domain_result.error is not None
                    and execution.domain_result.error.code
                    in {"artifact_persistence_failed", "result_contract_violation"}
                ):
                    last_error = execution.domain_result.error.public_message
                    error_code = execution.domain_result.error.code
                    results.clear()
                    artifacts.clear()
                    filters = dict(state.filters)
                    stop_after_batch = True
                    break
                if execution.tool_result is not None:
                    calls_used += 1
                    results.append(execution.tool_result)
                    # 图内已按同一目录投出公开载荷：展示层直接复用，不二次投影。
                    artifacts.extend(
                        artifact.public_payload
                        for artifact in execution.domain_result.artifacts
                    )
                    if execution.session_filters:
                        filters.update(execution.session_filters)
                messages.append(Message(
                    role="tool", tool_call_id=call.id,
                    content=json.dumps(execution.domain_result.model_payload,
                                       ensure_ascii=False)))
                continue
            if call.name == "analyze_product_performance":
                commerce_attempt_no += 1
                commerce_execution = execute_commerce_tool(call, DomainContext(
                    subject_id=turn_context.subject_id,
                    allowed_shop_ids=allowed_shop_ids,
                    shop_refs=dict(state.shop_refs),
                    conn=conn,
                    store=run_store,
                    chat_id=turn_context.chat_id,
                    user_message_id=turn_context.user_message_id,
                    # 一条用户消息就是一个根请求：本回合里的商品图调用不另起身份。
                    root_request_id=turn_context.user_message_id,
                    now=now,
                    deadline=deadline,
                    attempt_no=commerce_attempt_no))
                calls_used += 1
                domain_result = commerce_execution.domain_result
                if (domain_result.error is not None
                        and domain_result.error.code
                        in {"artifact_persistence_failed", "result_contract_violation"}):
                    last_error = domain_result.error.public_message
                    error_code = domain_result.error.code
                    results.clear()
                    artifacts.clear()
                    stop_after_batch = True
                    break
                if commerce_execution.report is not None:
                    for dataset in commerce_execution.report.datasets:
                        # 主数据集进结果列表：兜底摘要与“本轮有没有拿到确定性结果”都看它。
                        results.append(dataset.result)
                        break
                # 图内已按同一目录投出公开载荷：展示层直接复用，不二次投影。
                artifacts.extend(artifact.public_payload
                                 for artifact in domain_result.artifacts)
                messages.append(Message(
                    role="tool", tool_call_id=call.id,
                    content=json.dumps(domain_result.model_payload,
                                       ensure_ascii=False)))
                continue
            if call.arguments_error is not None or call.arguments is None:
                if correction_used:
                    last_error = "参数两次非法，已停止本次回答"
                    stop_after_batch = True
                    break
                correction_used = True
                messages.append(_correction_message(
                    call.id, [call.arguments_error or "arguments不是对象"]))
                continue
            if call.name == "evaluate_promotion":
                outcome = _handle_evaluate_promotion(call, question=question, now=now)
            else:
                messages.append(Message(role="tool", tool_call_id=call.id,
                                        content=json.dumps(
                                            {"error": "unknown_tool",
                                             "detail": "只允许query_business/"
                                                       "analyze_product_performance/"
                                                       "evaluate_promotion"},
                                            ensure_ascii=False)))
                continue
            if isinstance(outcome, list):
                if correction_used:
                    last_error = "参数两次非法，已停止本次回答"
                    stop_after_batch = True
                    break
                correction_used = True
                messages.append(_correction_message(call.id, outcome))
                continue
            calls_used += 1
            results.append(outcome)
            promo_catalog = build_catalog(conn, outcome,
                                         allowed_shop_ids=allowed_shop_ids)
            artifacts.append(to_public_artifact(outcome, promo_catalog))
            messages.append(Message(
                role="tool", tool_call_id=call.id,
                content=json.dumps(to_model_result(outcome, promo_catalog),
                                   ensure_ascii=False)))
        if stop_after_batch:
            break
    if text_answer is None and last_error is None:
        # 已拿到工具结果但模型还没输出正文：补一次纯文本回合，而不是直接放弃。
        text_answer = (_final_text_answer(model, messages, deadline)
                       or fallback_text
                       # 补答也失败时，用已存 Artifact 复述确定性摘要：
                       # 只说指标、窗口、截止与限制，不重新计算任何金额。
                       or _deterministic_summary(results)
                       or (NO_TEXT_WITH_RESULTS if results else NO_TEXT_WITHOUT_RESULTS))

    new_state = state.model_copy(update={
        "filters": filters,
        "turns": _trim_turns(messages[1:]),
    })
    # 确定性改写：模型只可能写出引用，用户读到的是已核验的展示名。
    # 名字未取得时保留引用，不丢答案、不编名。
    text = render_display_text(text_answer or last_error or "", _display_map(artifacts))
    return TurnResult(text=text, results=results, artifacts=artifacts,
                      clarification=None, error_code=error_code, state=new_state)
