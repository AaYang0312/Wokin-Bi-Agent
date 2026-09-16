"""隔离边界守卫（计划 2026-09-14-isolated-analysis-agent.md Task 4 Step 4）。

对 `bi_agent/analysis/calculations.py` 与 `summarizer.py` 的源码做 AST 白名单
检查：只批准标准库的一个显式子集、pydantic、`analysis.models` / `calculations`，
以及 ChatModel 类型面（`bi_agent.llm` 的 ChatModel/Message/ModelReply/ModelError
按名导入）；显式拒绝 psycopg、requests、httpx、socket、pathlib、subprocess、os
等数据库/文件/网络/系统通道，以及一切 `bi_agent.*.repository|tool|sync` 业务
模块。同时把模型调用钉成唯一形状
`model.complete(messages, [], timeout_s=timeout_s)`：tools 恒为空列表字面量。

除 import 语句外，对 `__import__` / `eval` / `exec` / `compile` 的裸名调用一律
报 `dynamic_import:*`，堵 `__import__("psycopg")` 这类绕过 import 语句的动态
通道；属性形式如 `re.compile(...)` 不是裸名调用，不误报。

已知局限：模型调用形状钉只覆盖直接的 `<name>.complete(...)` 属性调用；经
`getattr(model, "complete")` 之类的别名通道发起的调用对本检查不可见。该别名
通道由代码评审把守（AST 钉不追踪动态取属性），import 面白名单已先把可及
依赖收窄到本清单。

本模块是可信检查器：它只依赖标准库 `ast`，本身不属于被隔离的分析运行时面，
因此不在被扫描名单里（扫描对象固定为 calculations.py 与 summarizer.py）。
"""

from __future__ import annotations

import ast

__all__ = ["ALLOWED_RELATIVE", "ALLOWED_ROOTS", "BANNED_MODULES",
           "CHAT_MODEL_MODULE", "CHAT_MODEL_SURFACE", "import_violations",
           "model_call_violations"]

# 批准的依赖根（标准库显式子集 + pydantic）：分析模块只能从这个清单取依赖，
# 新依赖必须先改清单再改代码，保证“加依赖”永远是一次显式评审。
ALLOWED_ROOTS = frozenset({"__future__", "decimal", "hashlib", "json", "math",
                           "re", "time", "typing", "pydantic"})
# 相对导入只许指向分析包内的契约/纯计算模块；loader 依赖 runtime，不在列。
ALLOWED_RELATIVE = frozenset({"models", "calculations"})
# ChatModel 类型面：bi_agent 侧唯一放行的模块，且只能按名导入这几个符号；
# 整模块 `import bi_agent.llm` 会解锁 transport/工厂，同样拒绝。
CHAT_MODEL_MODULE = "bi_agent.llm"
CHAT_MODEL_SURFACE = frozenset({"ChatModel", "Message", "ModelReply",
                                "ModelError"})
# 显式禁运清单：命中报 import_banned；其余未批准模块报 import_unapproved。
BANNED_MODULES = frozenset({
    # 数据库驱动
    "psycopg", "psycopg2", "sqlite3",
    # 文件与系统
    "os", "os.path", "pathlib", "shutil", "glob", "tempfile", "subprocess",
    # 网络
    "socket", "ssl", "select", "http", "httpx", "requests", "urllib",
    "urllib.request", "ftplib", "telnetlib", "smtplib",
    # 并发/进程与动态执行通道
    "asyncio", "concurrent", "multiprocessing", "threading", "ctypes",
    "importlib", "pickle", "shelve", "marshal",
})
# bi_agent 侧按词素禁运的通道：repository=DB、tool(s)=业务工具、sync=同步任务。
_BANNED_BI_AGENT_SEGMENTS = frozenset({"repository", "tool", "tools", "sync"})
# 动态执行/动态 import 的裸名调用：import 语句之外的绕过通道，见
# import_violations 里的 `dynamic_import:*` 规则。
_DYNAMIC_CALL_NAMES = frozenset({"__import__", "eval", "exec", "compile"})
# 分析包内被批准的绝对模块名。
_ANALYSIS_INTERNAL = frozenset({"bi_agent.analysis.models",
                                "bi_agent.analysis.calculations"})


def _star_imports(violations: list[str], where: str,
                  names: list[ast.alias]) -> None:
    for alias in names:
        if alias.name == "*":
            violations.append(f"import_banned_star:{where}")


def _absolute(violations: list[str], module: str, names) -> None:
    """判定一个绝对 import；names 为 None 表示 `import x` 形式。"""
    root = module.split(".")[0]
    if module in BANNED_MODULES or root in BANNED_MODULES:
        violations.append(f"import_banned:{module}")
        return
    if root == "bi_agent":
        if module in _ANALYSIS_INTERNAL:
            if names is not None:
                _star_imports(violations, module, names)
            # names 为 None（`import bi_agent.analysis.models` 整模块形式）不是
            # star 导入，直接给通过判定——此前把 None 递进 _star_imports 会让
            # 检查器自己 TypeError（P2 修复：守卫必须给判定，不许崩）。
            return
        if module == CHAT_MODEL_MODULE:
            if names is None:
                violations.append(f"import_banned:{module}")
                return
            off_surface = sorted(alias.name for alias in names
                                 if alias.name not in CHAT_MODEL_SURFACE)
            if off_surface:
                violations.append("import_unapproved_name:" + module + ":"
                                  + ",".join(off_surface))
            return
        if any(segment in _BANNED_BI_AGENT_SEGMENTS
               for segment in module.split(".")):
            violations.append(f"import_banned:{module}")
            return
        violations.append(f"import_unapproved:{module}")
        return
    if root in ALLOWED_ROOTS:
        return
    violations.append(f"import_unapproved:{module}")


def import_violations(source: str) -> tuple[str, ...]:
    """返回源码里全部越界 import 的稳定码；干净源码返回空元组。"""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return ("parse_error",)
    violations: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                _absolute(violations, alias.name, None)
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0:
                _absolute(violations, node.module or "", node.names)
            elif node.level == 1 and node.module in ALLOWED_RELATIVE:
                _star_imports(violations, f".{node.module}", node.names)
            else:
                violations.append(
                    f"import_unapproved:relative-level{node.level}")
        elif isinstance(node, ast.Call):
            # 动态绕过通道：`__import__("psycopg")` 不经过任何 import 语句，
            # 单扫 import 节点会静默漏放（P1 修复）；eval/exec/compile 同类。
            func = node.func
            if isinstance(func, ast.Name) and func.id in _DYNAMIC_CALL_NAMES:
                violations.append(f"dynamic_import:{func.id}")
    return tuple(violations)


def model_call_violations(source: str, *, expected_calls: int = 1
                          ) -> tuple[str, ...]:
    """把模型调用钉成 `model.complete(messages, [], timeout_s=timeout_s)`。

    tools 必须是空列表字面量（而不是变量或非空列表），超时必须是名为
    `timeout_s` 的实参关键字。调用次数不等于 `expected_calls` 同样算违规。
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return ("parse_error",)
    calls = [node for node in ast.walk(tree)
             if isinstance(node, ast.Call)
             and isinstance(node.func, ast.Attribute)
             and node.func.attr == "complete"]
    violations: list[str] = []
    if len(calls) != expected_calls:
        violations.append(f"model_call_count:{len(calls)}")
    for call in calls:
        if not _is_pinned_call(call):
            violations.append("model_call_shape")
    return tuple(violations)


def _is_pinned_call(call: ast.Call) -> bool:
    if len(call.args) != 2:
        return False
    messages_arg, tools_arg = call.args
    if not isinstance(messages_arg, ast.Name) or messages_arg.id != "messages":
        return False
    if not isinstance(tools_arg, ast.List) or tools_arg.elts:
        return False
    keywords = {keyword.arg: keyword.value for keyword in call.keywords}
    if set(keywords) != {"timeout_s"}:
        return False
    timeout = keywords["timeout_s"]
    return isinstance(timeout, ast.Name) and timeout.id == "timeout_s"
