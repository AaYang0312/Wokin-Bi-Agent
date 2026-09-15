"""授权与版本过滤后的 approved 样例检索（计划 2026-09-14-approved-query-memory.md
Task 3）。

读取边界（总设计 §7.3、§10）：检索只读 `reporting.v_approved_query_examples`
这一层投影视图——视图本身钉住 `status='approved'`，记忆底表与事件表、聊天、
Artifact、诊断、SQL、结果、凭据、错误文本一概不出现在本模块的 SQL 里；
tests.test_query_memory.RetrievalConn 对视图之外的任何语句显式报错。

三条硬规则：

1. **先过滤后排序。** owner 精确相等、domain 精确相等、`authorization_refs` 是
   本轮 opaque 授权 ref 的子集、`version_requirements` 与当前 `VersionSet` 的
   完整 JSON 精确相等，四条都落在同一条 SQL 里，`ORDER BY example_ref`
   `LIMIT 100` 硬上限；授权域只取 `context.shop_refs` 的 opaque 值（键是内部
   店铺 id，永不进 SQL），空授权域直接返回空元组，一条 SQL 都不发。
2. **候选 fail-closed。** 每条候选都要过 Task 2 的目录登记校验与 Task 1 的严格
   `ApprovedExample` 契约：反序列化失败、引用未登记、槽位重复、revision 小于 1
   的整条丢弃，只递增固定计数器 `query_memory_candidate_invalid_total`——异常
   文本与候选内容不出现在任何返回值或错误里。
3. **排序是死的。** 词项计分复用语义目录的 NFKC/英文/中文归一化（同一份
   `normalize_terms`，不另抄一份规则）；重叠数为零的候选一律不召回，禁止用
   "最新几条"回填；同分按 `example_ref` 升序决胜；`limit` 只允许 1–3。同一份
   候选无论以什么顺序喂进来，结果逐条一致。
"""

from __future__ import annotations

from typing import Any

from psycopg.types.json import Jsonb

from bi_agent.commerce.models import DomainContext
from bi_agent.runtime.versions import VersionSet
# 词项化与归一化直接复用语义目录检索层的同一套实现：这里再抄一份迟早漂移
# （`_normalize` 只用于短语加成的子串判定，与 spans 命中同一套文本形态）。
from bi_agent.semantic_catalog.retrieval import _normalize, normalize_terms

from .models import ApprovedExample, QuerySlot
from .sanitize import validate_stable_refs_and_codes

# 计划 Task 3 Step 3 逐字固定的投影与前置过滤；`ORDER BY example_ref LIMIT 100`
# 是候选硬上限，排序在 Python 侧做完后只取前 limit 条。
_RETRIEVAL_SQL = """SELECT example_ref, domain, intent_signature, question_template, slots,
       normalized_request, expected_tool, version_requirements, approval_revision
FROM reporting.v_approved_query_examples
WHERE domain = %(domain)s
  AND owner_subject_id = %(subject_id)s
  AND authorization_refs <@ %(allowed_refs)s::text[]
  AND version_requirements = %(versions)s::jsonb
ORDER BY example_ref
LIMIT 100"""

_MIN_LIMIT = 1
_MAX_LIMIT = 3
_PHRASE_BONUS = 2

# 固定命名的进程内计数器：只数被丢弃的候选条数，不记异常文本、候选内容或问题
# 文本。读取方先快照再取差值；本仓库没有指标后端，也不为此新增依赖。
_candidate_invalid_total = 0


def _validated_limit(limit: int) -> int:
    """`limit` 只允许 1–3：越界是调用方程序错误，在发 SQL 之前就当场面红。"""
    if isinstance(limit, bool) or not isinstance(limit, int) \
            or not _MIN_LIMIT <= limit <= _MAX_LIMIT:
        raise ValueError("memory_limit_out_of_range")
    return limit


def _example_from_row(row: Any) -> ApprovedExample:
    """一行视图投影 → 严格契约样例；任何缺陷都以异常浮出，由调用方整条丢弃。"""
    (example_ref, domain, intent_signature, question_template, slots,
     normalized_request, expected_tool, version_requirements,
     approval_revision) = row
    # Task 2 的登记校验先跑：kebab 引用必须真的登记在已发布目录里，业务码必须
    # 是封闭词表成员——形状合法的发明引用在这里就被拒收，不等投影阶段。
    validate_stable_refs_and_codes(normalized_request)
    return ApprovedExample(
        example_ref=example_ref, domain=domain,
        intent_signature=intent_signature,
        question_template=question_template,
        slots=tuple(QuerySlot(**slot) for slot in slots),
        normalized_request=normalized_request, expected_tool=expected_tool,
        version_requirements=VersionSet(**version_requirements),
        approval_revision=approval_revision)


def lexical_score(question: str, example: ApprovedExample) -> tuple[int, int, str]:
    """计划 Task 3 Step 4 的确定性词项分：重叠数、短语加成、`example_ref`。

    词项化复用语义目录的 `normalize_terms`（NFKC + 小写 + 中文整段/二元窗口 +
    ASCII 整块）；短语加成在归一化后的问题文本上做子串判定，避免大小写与全角
    形态漏配。`example_ref` 只作并列时的最终决胜键，不参与分数。
    """
    question_tokens = frozenset(normalize_terms(question))
    example_tokens = frozenset(normalize_terms(
        example.intent_signature.replace("-", " ") + " "
        + example.question_template))
    overlap = len(question_tokens & example_tokens)
    normalized_question = _normalize(question)
    phrase_bonus = sum(_PHRASE_BONUS for token in sorted(example_tokens)
                       if len(token) >= 2 and token in normalized_question)
    return (overlap, phrase_bonus, example.example_ref)


def retrieve_approved_examples(
        question: str, *, context: DomainContext, domain: str,
        current_versions: VersionSet,
        limit: int = 3) -> tuple[ApprovedExample, ...]:
    """给出与当前授权域和版本精确兼容的最多 `limit` 条已批准样例。

    空授权域或零词项重叠都返回空元组：调用方（Task 5 的路由接入）把空记忆当作
    "没有样例"继续既有路由，不存在需要降级的第三种状态。
    """
    global _candidate_invalid_total
    limit = _validated_limit(limit)
    # 授权域只取 opaque 引用值：`shop_refs` 的键是内部店铺 id，一个都不进 SQL。
    allowed_refs = sorted(set(context.shop_refs.values()))
    if not allowed_refs:
        return ()
    if not normalize_terms(question):
        return ()
    rows = context.conn.execute(_RETRIEVAL_SQL, {
        "domain": domain,
        "subject_id": context.subject_id,
        "allowed_refs": allowed_refs,
        "versions": Jsonb(current_versions.model_dump(mode="json")),
    }).fetchall()                       # 硬上限由 SQL 的 LIMIT 100 唯一钉住
    candidates: list[ApprovedExample] = []
    for row in rows:
        try:
            candidates.append(_example_from_row(row))
        except Exception:
            # fail-closed 数据边界：一条坏候选只损失它自己。宽捕获是刻意的——
            # 反序列化缺陷的形状无法枚举（jsonb 里什么都可能来），唯一不能做的
            # 是把异常文本或候选内容递给上层。
            _candidate_invalid_total += 1
    scored = [(lexical_score(question, item), item) for item in candidates]
    scored.sort(key=lambda pair: (-pair[0][0], -pair[0][1],
                                  pair[1].example_ref))
    hits = [pair for pair in scored if pair[0][0] > 0]
    return tuple(item for _, item in hits[:limit])
