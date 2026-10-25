"""结构化判定（计划 2026-10-24 T2/T3）：与 acceptance runner 共用的判定器。

原则：

- 判定器必须能拒绝被破坏的答案：非法/非有限数值（``None``、空串、``"bad"``、
  布尔、NaN、Infinity）不允许在比较中被吞成 0（旧 ``_quantize`` 的异常路径）。
- 数值精度策略显式命名：金额键按 2 位小数比较（``MONEY_VALUE_KEYS`` 白名单，
  与既有题库金额键一致）；其余键（数量、单数、比值）按精确数值比较——
  ``Decimal("7.00") == Decimal("7")`` 成立，但 ``7.005`` 不等于 7。
  未登记的新键默认精确比较：只会更严，不会更松。
- ``observed`` 是执行器采集的白名单投影（rows/limitations/basis/coverage/
  diagnostics/partial_answer/refused/remediation/text/calls），不是另一份
  expected；本模块不连库、不执行业务代码。
- 问题码是稳定中文短句，供 JSONL 报告逐条落盘。
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

# 金额键白名单：与既有 35 题合同的金额值键一致（元，两位小数比较）。
# 未列出的键一律按精确数值比较。
MONEY_VALUE_KEYS = frozenset({
    "paid_amount", "paid_amount_previous", "product_paid_amount",
    "refund_amount", "cash_difference", "aov", "ad_spend", "spend_cap",
    "overrun", "remaining_budget", "daily_allowance",
})


def parse_finite_decimal(value: object) -> Decimal:
    """把观测/期望值解析成有限 Decimal；非法值抛 ``ValueError``。

    拒绝 ``None``、空串、无法解析的字符串、布尔（bool 是 int 子类）、
    NaN/Infinity（字符串或 Decimal/float 形式）。这里不允许任何
    "解析失败当 0"的路径——那正是旧判定器错判通过的根因。
    """
    if value is None or isinstance(value, bool):
        raise ValueError(f"非有限数值：{value!r}")
    try:
        parsed = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError, ArithmeticError) as exc:
        raise ValueError(f"非有限数值：{value!r}") from exc
    if not parsed.is_finite():
        raise ValueError(f"非有限数值：{value!r}")
    return parsed


def _decimal_equal(expected: Decimal, actual: Decimal, *, money: bool) -> bool:
    if money:
        return expected.quantize(Decimal("0.01")) == actual.quantize(
            Decimal("0.01"))
    return expected == actual


def _money_key(key: str) -> bool:
    return key in MONEY_VALUE_KEYS


def _product_value_column(expected_value: str) -> tuple[str, bool]:
    """products 期望值落在哪一列：金额列（product_paid_amount）或数量列。

    既有题库的 products 期望是 (pid, value) 二元组，没有列名声明；按值是否
    为已知的数量期望（"7"/"4"，题 04/05）判列。判错会在下一步数值比较中
    翻红（读到的列值对不上），不会静默通过。新增商品期望时优先改用显式
    列名，而不是扩这个启发式。
    """
    if str(expected_value) in {"7", "4"}:
        return "quantity", False
    return "product_paid_amount", True


def compare_values(expected_values: dict, rows) -> list[str]:
    """核对工具结果数据行里的数值期望（原 acceptance._value_problems）。"""
    problems: list[str] = []
    if not rows:
        return ["工具结果无数据行"]
    for key, expected in expected_values.items():
        if key == "products":
            column, money = _product_value_column(expected[0][1])
            got_values = {row.get("product_id"): row.get(column)
                          for row in rows if row.get("product_id")}
            for pid, value in expected:
                actual = got_values.get(pid)
                try:
                    expected_decimal = parse_finite_decimal(value)
                    actual_decimal = parse_finite_decimal(actual)
                except ValueError:
                    problems.append(
                        f"商品{pid}期望{value!r}，实际{actual!r}，不是合法有限数值")
                    continue
                if not _decimal_equal(expected_decimal, actual_decimal,
                                      money=money):
                    problems.append(
                        f"商品{pid}期望{value}，实际{actual}")
            continue
        if isinstance(expected, list):
            try:
                expected_decimals = [parse_finite_decimal(item)
                                     for item in expected]
                actual_decimals = [parse_finite_decimal(row.get(key))
                                   for row in rows]
            except ValueError as exc:
                problems.append(f"{key}存在非法数值：{exc}")
                continue
            if len(actual_decimals) != len(expected_decimals) or any(
                    not _decimal_equal(want, got, money=_money_key(key))
                    for want, got in zip(expected_decimals,
                                         actual_decimals)):
                problems.append(
                    f"{key}期望{expected}，实际"
                    f"{[row.get(key) for row in rows]}")
            continue
        actual = rows[0].get(key)
        try:
            expected_decimal = parse_finite_decimal(expected)
            actual_decimal = parse_finite_decimal(actual)
        except ValueError:
            problems.append(
                f"{key}期望{expected!r}，实际{actual!r}，不是合法有限数值")
            continue
        if not _decimal_equal(expected_decimal, actual_decimal,
                              money=_money_key(key)):
            problems.append(f"{key}期望{expected}，实际{actual}")
    return problems


def normalize_params(request) -> dict:
    """query_business 规范化请求的公开参数投影（原 acceptance._normalize_params）。"""
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


def param_problems(expected_params: dict, request) -> list[str]:
    actual = normalize_params(request)
    problems = []
    for key, value in expected_params.items():
        actual_value = actual.get(key)
        if key in ("shop_ids", "metrics"):
            if sorted(value if isinstance(value, list) else [value]) != sorted(
                    actual_value if isinstance(actual_value, list)
                    else [actual_value]):
                problems.append(f"{key}期望{value}，实际{actual_value}")
        elif actual_value != value:
            problems.append(f"{key}期望{value}，实际{actual_value}")
    return problems


def limitation_codes(limitations) -> list[str]:
    """披露行 → 稳定原因码（与运行图同一张映射表）。"""
    from bi_agent.business_query.nodes import _limitation_codes as codes

    return codes(list(limitations))


def verify_expected(expected: dict, observed: dict) -> list[str]:
    """核对一份观测投影是否满足全部已声明期望；空列表=全部满足。

    ``observed`` 键（缺省键跳过对应检查；值为 ``None`` 表示"明确没有"）：

    - ``rows``：工具结果数据行（``None``=无工具结果/无数据行）
    - ``limitations``：披露行列表（原因码由此派生）
    - ``basis``：口径凭证 ``(shop_id, metric, basis, time_basis)`` 元组列表
    - ``coverage``：``{status,start,end,gaps,suggested_window}`` 投影
    - ``diagnostics``：诊断键值投影
    - ``partial_answer`` / ``refused`` / ``remediation``：与公开载荷同名
    - ``text``：最终回答文本（``None``=未产生）
    - ``calls``：本轮工具调用记录列表（长度即调用次数）
    - 期望侧 ``calls`` 是"前 N 次调用的参数核对"（既有题库语义，逐条对位，
      缺调用记"第N次工具调用未发生"）；总数断言用显式 ``call_count``。
    """
    problems: list[str] = []
    rows = observed.get("rows")
    limitations = observed.get("limitations") or []
    codes = limitation_codes(limitations)
    for code in expected.get("codes", []):
        if code not in codes:
            problems.append(f"原因码期望{code}，实际{codes}")
    for code in expected.get("not_codes", []):
        if code in codes:
            problems.append(f"原因码不应出现{code}，实际{codes}")
    basis_keys = {tuple(item) for item in (observed.get("basis") or [])}
    for entry in expected.get("basis", []):
        if tuple(entry) not in basis_keys:
            problems.append(f"口径凭证缺{entry}，实际{sorted(basis_keys)}")
    if expected.get("basis_exact"):
        # 「basis 只含 manual 来源」这类断言要的是集合相等，不是"逐项在场"。
        wanted = {tuple(entry) for entry in expected.get("basis", [])}
        if basis_keys != wanted:
            problems.append(f"口径凭证应恰为{sorted(wanted)}，"
                            f"实际{sorted(basis_keys)}")
    for fragment in expected.get("limitations_contain", []):
        if not any(fragment in str(line) for line in limitations):
            problems.append(f"披露未包含“{fragment}”，实际{list(limitations)}")
    if expected.get("partial_answer") is not None:
        block = observed.get("partial_answer")
        if block is None:
            problems.append("期望 partial_answer 在场，实际缺失")
        else:
            for field, value in expected["partial_answer"].items():
                if block.get(field) != value:
                    problems.append(f"partial_answer.{field}期望{value}，"
                                    f"实际{block.get(field)}")
    if expected.get("refused") is not None:
        actual = [{"metric": item.get("metric"), "reason": item.get("reason")}
                  for item in (observed.get("refused") or [])]
        if actual != expected["refused"]:
            problems.append(f"refused期望{expected['refused']}，实际{actual}")
    if expected.get("remediation") is not None:
        block = observed.get("remediation")
        if block is None:
            problems.append("期望 remediation 在场，实际缺失")
        else:
            for field, value in expected["remediation"].items():
                actual_value = block.get(field)
                if actual_value != value:
                    problems.append(f"remediation.{field}期望{value}，"
                                    f"实际{actual_value}")
    for key, expected_diagnostics in (expected.get("diagnostics") or {}).items():
        actual = (observed.get("diagnostics") or {}).get(key)
        if not isinstance(actual, dict):
            problems.append(f"诊断缺{key}，实际{observed.get('diagnostics')}")
            continue
        for field, value in expected_diagnostics.items():
            if str(actual.get(field)) != str(value):
                problems.append(f"诊断{key}.{field}期望{value}，"
                                f"实际{actual.get(field)}")
    for field, value in (expected.get("coverage") or {}).items():
        actual = (observed.get("coverage") or {}).get(field)
        if actual != value:
            problems.append(f"覆盖{field}期望{value}，实际{actual}")
    if expected.get("no_values") and rows:
        problems.append(f"期望无成功数值，实际{rows}")
    if expected.get("no_rows") and rows:
        problems.append(f"期望零行（真实没有），实际{rows}")
    if expected.get("no_total") and rows and any(
            "shop_id" not in row for row in rows):
        problems.append("分列结果里出现了不带店铺的合计行")
    if "values" in expected:
        problems.extend(compare_values(expected["values"], rows))
    text = observed.get("text")
    for fragment in expected.get("text_contains", []):
        if fragment not in (text or ""):
            problems.append(f"回答未包含“{fragment}”")
    for fragment in expected.get("text_absent", []):
        if fragment in (text or ""):
            problems.append(f"回答不该宣称“{fragment}”")
    if "call_count" in expected and "calls" in observed:
        actual_calls = observed.get("calls")
        wanted = expected["call_count"]
        got = len(actual_calls) if isinstance(actual_calls, list) else 0
        if got != wanted:
            problems.append(f"期望{wanted}次工具调用，实际{got}次")
    return problems


def verify_commerce_turn(expected: dict, calls: list) -> list[str]:
    """compare_performance / analyze_product_performance 回合的机器核对。

    （原 acceptance._verify_commerce_turn / _verify_product_turn：断言全部落在
    已校验载荷的结构化字段上，不做跨 UUID 载荷的子串扫描。）
    """
    if expected.get("tool") == "analyze_product_performance":
        return verify_product_turn(expected, calls)
    problems: list[str] = []
    if expected.get("tool") != "compare_performance":
        return problems
    if not calls:
        return ["compare_performance未被调用"]
    entry = calls[-1]
    arguments = entry["arguments"]
    for key, value in (expected.get("parameters") or {}).items():
        actual_value = arguments.get(key)
        if key in ("metrics", "platforms", "shop_refs"):
            if sorted(value if isinstance(value, list) else [value]) != sorted(
                    actual_value if isinstance(actual_value, list)
                    else [actual_value]):
                problems.append(f"{key}期望{value}，实际{actual_value}")
        elif actual_value != value:
            problems.append(f"{key}期望{value}，实际{actual_value}")
    payload = entry["payload"]
    if expected.get("status") == "needs_input":
        # needs_input 是正确的门禁结果，不是"没有载荷"的失败：只核对
        # 状态与披露句，不要求已发布数据。
        if not isinstance(payload, dict) or payload.get("status") != "needs_input":
            problems.append(f"期望needs_input，实际载荷：{payload}")
            return problems
        joined = " ".join(str(line) for line in payload.get("limitations") or [])
        for fragment in expected.get("limitations_contain", []):
            if fragment not in joined:
                problems.append(f"needs_input披露未包含“{fragment}”，实际{joined}")
        return problems
    if not isinstance(payload, dict) or "filters" not in payload:
        problems.append(f"compare_performance无已发布载荷：{payload}")
        return problems
    caliber = (expected.get("parameters") or {}).get("caliber", "erp_outstock")
    if payload["filters"].get("caliber") != caliber:
        problems.append(f"口径期望{caliber}，"
                        f"实际{payload['filters'].get('caliber')}")
    rows = {row["platform"]: row for row in payload.get("data", [])
            if isinstance(row, dict) and "platform" in row}
    for platform, cells in (expected.get("rows") or {}).items():
        if platform == "total":
            continue
        row = rows.get(platform)
        if row is None:
            problems.append(f"期望{platform}的分组行在场，实际行：{sorted(rows)}")
            continue
        for metric, value in cells.items():
            if row.get(metric) != value:
                problems.append(f"{platform}.{metric}期望{value}，实际{row.get(metric)}")
    if "total" in (expected.get("rows") or {}):
        totals = [row for row in payload.get("data", [])
                  if isinstance(row, dict) and "platform" not in row
                  and "shop_ref" not in row]
        for metric, value in expected["rows"]["total"].items():
            got = totals[0].get(metric) if totals else None
            if got != value:
                problems.append(f"合计.{metric}期望{value}，实际{got}")
    cells = {(item.get("platform"), str(item["metric"])): item
             for item in payload.get("metric_statuses", [])
             if isinstance(item, dict) and "platform" in item}
    for platform, metric, status, reason in expected.get("cells", []):
        item = cells.get((platform, metric))
        if item is None:
            problems.append(f"缺状态格({platform},{metric})，实际{sorted(cells)}")
            continue
        if item.get("status") != status or item.get("reason") != reason:
            problems.append(f"({platform},{metric})期望{status}/{reason}，"
                            f"实际{item.get('status')}/{item.get('reason')}")
    blocks = {block["metric"]: block for block in payload.get("ranking", [])
              if isinstance(block, dict)}
    for metric, want in (expected.get("ranking") or {}).items():
        block = blocks.get(metric)
        if block is None:
            problems.append(f"缺{metric}排名块")
            continue
        if block.get("status") != want.get("status"):
            problems.append(f"{metric}排名状态期望{want.get('status')}，"
                            f"实际{block.get('status')}")
        if block.get("caliber") != caliber:
            problems.append(f"{metric}排名块口径期望{caliber}，实际{block.get('caliber')}")
        for entry_want in want.get("missing", []):
            missing = block.get("missing") or []
            if not any(item.get("platform") == entry_want[0]
                       and item.get("reason") == entry_want[1]
                       for item in missing):
                problems.append(f"{metric}排名缺失清单缺{entry_want}，实际{missing}")
    groups = {item.get("platform"): item for item in payload.get("group_statuses", [])
              if isinstance(item, dict) and "platform" in item}
    for platform, want in (expected.get("groups") or {}).items():
        item = groups.get(platform)
        if item is None:
            problems.append(f"缺{platform}分组状态")
            continue
        for field in ("status", "shops_evaluated"):
            if field in want and item.get(field) != want[field]:
                problems.append(f"{platform}分组{field}期望{want[field]}，"
                                f"实际{item.get(field)}")
    reasons = sorted(str(item.get("reason")) for item in
                     payload.get("excluded_scope", []) if isinstance(item, dict))
    if "excluded_reasons" in expected and reasons != sorted(expected["excluded_reasons"]):
        problems.append(f"excluded_scope原因期望{expected['excluded_reasons']}，实际{reasons}")
    return problems


def verify_product_turn(expected: dict, calls: list) -> list[str]:
    """商品报告回合的机器核对（原 acceptance._verify_product_turn）。

    期望行以原始店号（如 S1）书写，映射到载荷里的 ent- 店引用再比对。
    """
    from bi_agent.catalog import ref_for_key

    if not calls:
        return ["analyze_product_performance未被调用"]
    problems: list[str] = []
    entry = calls[-1]
    arguments = entry["arguments"]
    for key, value in (expected.get("parameters") or {}).items():
        actual_value = arguments.get(key)
        if key in ("metrics",):
            if sorted(value) != sorted(actual_value or []):
                problems.append(f"{key}期望{value}，实际{actual_value}")
        elif actual_value != value:
            problems.append(f"{key}期望{value}，实际{actual_value}")
    payload = entry["payload"]
    if not isinstance(payload, dict) or "data" not in payload:
        return problems + [f"analyze_product_performance无已发布载荷：{payload}"]
    rows = {row.get("shop_ref"): row for row in payload.get("data", [])
            if isinstance(row, dict) and row.get("shop_ref")}
    for shop, cells in (expected.get("rows") or {}).items():
        row = rows.get(ref_for_key("shop", shop))
        if row is None:
            problems.append(f"期望{shop}的行在场，实际行：{sorted(rows)}")
            continue
        for column, value in cells.items():
            if row.get(column) != value:
                problems.append(f"{shop}.{column}期望{value}，实际{row.get(column)}")
    for key, want in (expected.get("diagnostics") or {}).items():
        diag = (payload.get("diagnostics") or {}).get(key)
        if not isinstance(diag, dict):
            problems.append(f"诊断缺{key}，实际{payload.get('diagnostics')}")
            continue
        for field, value in want.items():
            if str(diag.get(field)) != str(value):
                problems.append(
                    f"诊断{key}.{field}期望{value}，实际{diag.get(field)}")
    for metric, unit in (expected.get("metric_units") or {}).items():
        actual = (payload.get("metric_units") or {}).get(metric)
        if actual != unit:
            problems.append(f"单位{metric}期望{unit}，实际{actual}")
    return problems


def validate_case_shape(question: dict) -> list[str]:
    """场景形状校验：turns 非空、逐轮 expected 数量一致（防 zip 静默丢轮次）。"""
    problems: list[str] = []
    turns = question.get("turns")
    if not isinstance(turns, list) or not turns:
        problems.append("turns必须是非空列表")
        return problems
    if any(not isinstance(turn, str) or not turn.strip() for turn in turns):
        problems.append("turns每一轮都必须是非空文本")
    expected = question.get("expected")
    if isinstance(expected, list):
        if len(expected) != len(turns):
            problems.append(
                f"expected列表长度（{len(expected)}）必须等于turns长度"
                f"（{len(turns)}）")
        if any(not isinstance(item, dict) for item in expected):
            problems.append("expected列表每一项都必须是对象")
    elif not isinstance(expected, dict):
        problems.append("expected必须是对象或与turns等长的列表")
    return problems
