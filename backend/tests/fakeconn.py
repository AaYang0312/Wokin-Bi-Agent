"""测试替身：目录投影（bi_agent.catalog.projection）只读这两条 reporting 视图。

指标查询在各测试文件里都被替换掉，所以这里只需要回答：
- 全表店铺档案 → 引用展示名
- 目录版本 → Artifact 记录它解析名称时用的版本

真实数据库用例（tests.test_catalog / test_api）走真视图，不用本替身。
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Sequence
from uuid import UUID

import psycopg
from psycopg.types.json import Jsonb

from bi_agent.catalog import ref_for_key

# 引用由 (kind, ERP主键) 纯派生；测试用同源常量，不手抄哈希。
S1_REF = ref_for_key("shop", "S1")
S2_REF = ref_for_key("shop", "S2")
P1_REF = ref_for_key("product", "P1")

# 默认档案：店名可读、不含长数字主键，能通过展示名内容白名单。
DEFAULT_SHOPS: tuple[tuple[str, str, str], ...] = (("S1", "fxg", "钉枪工厂店"),)
DEFAULT_VERSION = 7


class Rows:
    """psycopg 结果对象的最小替身。"""

    def __init__(self, rows: Sequence[object]):
        self._rows = list(rows)

    def fetchall(self) -> list[object]:
        return list(self._rows)

    def fetchone(self) -> object:
        return self._rows[0] if self._rows else None


def catalog_rows(sql: str, *, shops: Sequence[object] = DEFAULT_SHOPS,
                 version: int = DEFAULT_VERSION) -> Rows | None:
    """命中目录投影的读取就返回结果，否则返回 None 交给调用方原本的分支。"""
    if "shop_id, platform, display_name" in sql:
        return Rows(shops)
    if "version FROM reporting.v_catalog_version" in sql:
        return Rows([(version,)])
    return None


class CatalogConn:
    """只服务目录投影的连接替身：其他 SQL 一律显式报错，不静默返回空。"""

    def __init__(self, *, shops: Sequence[object] = DEFAULT_SHOPS,
                 version: int = DEFAULT_VERSION):
        self.shops = list(shops)
        self.version = version

    def execute(self, sql: str, params: object = None) -> Rows:
        rows = catalog_rows(sql, shops=self.shops, version=self.version)
        if rows is None:
            raise AssertionError(f"未预期的SQL：{sql}")
        return rows


class ShopCatalogConn(CatalogConn):
    """Agent 侧替身：兼顾 `_fetch_shops` 的两列读取、口径候选的平台读取与目录投影的三列读取。

    三边都只读 reporting.v_shops，列数不同，所以按列名分支，不猜顺序。
    """

    def __init__(self, shops: Sequence[tuple[str, str]] = (("S1", "店铺A"),),
                 *, version: int = DEFAULT_VERSION,
                 platform: str = "fxg"):
        super().__init__(shops=[(shop_id, platform, name)
                                for shop_id, name in shops], version=version)
        self.profiles = [(shop_id, platform, name)
                         for shop_id, name in shops]

    def execute(self, sql: str, params: object = None) -> Rows:
        text = " ".join(sql.split())
        wanted = list(params[0]) if params else None
        if "shop_id, platform, display_name" in text:
            return Rows(self.profiles)
        if "version FROM reporting.v_catalog_version" in text:
            return Rows([(self.version,)])
        if text.startswith("SELECT shop_id, platform FROM reporting.v_shops"):
            # 口径候选问的是“这个平台登记了什么来源”（data_quality.SHOP_PLATFORMS_SQL）：
            # 两列就得回两列，不然店名会站进口径的位置上。
            return Rows([(shop_id, platform) for shop_id, platform, _ in self.profiles
                         if wanted is None or shop_id in wanted])
        if "FROM reporting.v_shops" in text:
            return Rows([(shop_id, name) for shop_id, _, name in self.profiles
                         if wanted is None or shop_id in wanted])
        raise AssertionError(f"未预期的SQL：{text}")


# --- approved 查询学习记忆的生命周期替身（计划 Task 2） ------------------------
#
# 只回答 bi_agent.query_memory.repository 的那几条 SQL：来源资格读取、锁行读取、
# 替换目标读取、CAS 状态更新、样例与事件插入；白名单之外的任何 SQL——包括
# bi.chat_messages 与 bi.query_artifacts（含 payload）的读取——一律显式报错。
# 于是「builder 不读聊天文本与结果载荷」由替身自身的拒绝性保证，而不是靠
# 用例恰好没写那条 SQL。

# 样例行列序：与 repository._SELECT_FOR_UPDATE_SQL 的投影逐一对应，
# 替身按名存取、按序返回，两边列序漂移会在用例现场炸出来。
EXAMPLE_COLUMNS = (
    "example_ref", "source_run_id", "owner_subject_id", "domain",
    "intent_signature", "question_template", "slots", "normalized_request",
    "expected_tool", "version_requirements", "authorization_refs",
    "status", "approval_revision",
)


def memory_versions_dict() -> dict:
    """一份对 VersionSet 契约合法的合成版本集合（与 test_query_memory 同源常量）。"""
    return {
        "schema_version": "reporting/2026-09-14.1",
        "semantic_catalog_version": "semantic/2026-09-14.1",
        "data_catalog_version": 7,
        "metric_version": "metrics/2026-09-12.1",
        "policy_version": "multi-source-policy/2026-09-12.1",
        "source_registry_version": "sources/2026-09-12.1",
        "graph_version": "business_query-graph/2026-09-11.1",
    }


def example_row(example_ref: str, *, run_id: UUID | None = None,
                owner: str = "subject-a", domain: str = "controlled_exploration",
                status: str = "draft", revision: int = 0) -> dict:
    """替身里的一个样例行：jsonb 列按真库形状回读（dict / list，不是 Jsonb 包裹）。"""
    return {
        "example_ref": example_ref,
        "source_run_id": run_id or UUID(int=1),
        "owner_subject_id": owner,
        "domain": domain,
        "intent_signature": "cost-by-shop-and-window",
        "question_template": "比较 {shop_scope} 在 {date_window} 的成本",
        "slots": [{"name": "shop_scope", "kind": "entity_scope"},
                  {"name": "date_window", "kind": "date_window"}],
        "normalized_request": {"requested_metric_refs": ["metric-cost-total"]},
        "expected_tool": "explore_business_data",
        "version_requirements": memory_versions_dict(),
        "authorization_refs": ["ent-1a2b3c4d"],
        "status": status,
        "approval_revision": revision,
    }


def memory_run_row(*, subject: str = "subject-a", status: str = "succeeded",
                   domain: str = "business_query",
                   shop_refs: Sequence[str] = (S1_REF,),
                   request_extra: dict | None = None,
                   provenance: bool = True) -> tuple:
    """资格读取的一行：前四列来自 query_runs，其余来自 query_provenance。

    `provenance=False` 时血缘各列按 LEFT JOIN 未命中回 None——「血缘不完整」的
    形状由这里提供，而不是让用例手拼 13 元组。
    """
    request: dict = {"shop_refs": list(shop_refs), "metrics": ["paid_amount"]}
    if request_extra:
        request.update(request_extra)
    if not provenance:
        return (subject, status, domain, request,
                None, None, None, None, None, None, None, None, None)
    return (subject, status, domain, request,
            "fixed_metric_query", "1", "metrics/2026-09-12.1", "008", 7,
            "identity/2026-09-11.1", "multi-source-policy/2026-09-12.1",
            "business_query-graph/2026-09-11.1", "sources/2026-09-12.1")


def _plain(value):
    """jsonb 入参解包：真库读回 dict/list，替身同样不把 Jsonb 包裹回给被测代码。"""
    return value.obj if isinstance(value, Jsonb) else value


class QueryMemoryConn:
    """审核写路径（builder + repository）的最小替身。

    只认本域 SQL；`sql_log` 留下全部执行过的语句供断言（比如确认没有读到聊天表），
    `events` / `writes` 带着事务深度记录写入，用来钉「插入与事件同事务」。
    """

    def __init__(self, *, run_row: tuple | None = None,
                 examples: dict[str, dict] | None = None):
        self.run_row = run_row
        self.examples = dict(examples or {})
        self.events: list[tuple] = []   # (事务深度, ref, revision, actor, kind, reason, replacement)
        self.writes: list[tuple] = []   # (事务深度, "example"/"event", example_ref)
        self.sql_log: list[str] = []
        # 显式测试钩子：置 True 后 CAS 更新命中 0 行，用于钉 revision 冲突映射。
        self.cas_fail = False
        self._depth = 0

    @contextmanager
    def transaction(self):
        self._depth += 1
        try:
            yield
        finally:
            self._depth -= 1

    def execute(self, sql: str, params: object = None) -> Rows:
        text = " ".join(sql.split())
        self.sql_log.append(text)
        values = list(params or [])
        if "FROM bi.query_runs" in text and "LEFT JOIN bi.query_provenance" in text:
            return Rows([self.run_row]) if self.run_row is not None else Rows([])
        if "FOR UPDATE" in text and "FROM bi.approved_query_examples" in text:
            row = self.examples.get(values[0])
            if row is None:
                return Rows([])
            return Rows([[row[column] for column in EXAMPLE_COLUMNS]])
        if text.startswith("SELECT domain, status FROM bi.approved_query_examples"):
            row = self.examples.get(values[0])
            if row is None:
                return Rows([])
            return Rows([(row["domain"], row["status"])])
        if text.startswith("UPDATE bi.approved_query_examples"):
            row = self.examples.get(values[2])
            if self.cas_fail or row is None or row["approval_revision"] != values[3]:
                return Rows([])
            row["status"] = values[0]
            row["approval_revision"] = values[1]
            return Rows([(values[1],)])
        if text.startswith("INSERT INTO bi.approved_query_examples"):
            if values[0] in self.examples:
                raise psycopg.errors.UniqueViolation("duplicate example_ref")
            self.examples[values[0]] = {
                "example_ref": values[0], "source_run_id": values[1],
                "owner_subject_id": values[2], "domain": values[3],
                "intent_signature": values[4], "question_template": values[5],
                "slots": _plain(values[6]),
                "normalized_request": _plain(values[7]),
                "expected_tool": values[8],
                "version_requirements": _plain(values[9]),
                "authorization_refs": list(values[10]),
                "status": "draft", "approval_revision": 0,
            }
            self.writes.append((self._depth, "example", values[0]))
            return Rows([])
        if text.startswith("INSERT INTO bi.approved_query_events"):
            self.events.append((self._depth, *values))
            self.writes.append((self._depth, "event", values[0]))
            return Rows([])
        raise AssertionError(f"未预期的SQL：{text}")


def price_audit_payload() -> dict:
    """一份对 `price_audit` 判别契约合法的**最小**载荷（只用合成引用）。

    存在的理由是给"领域能不能发这种 Artifact"那两条用例用：它们要拦的是
    **领域不匹配**，不是载荷形状。载荷不合法时 NewArtifact 会先一步拒绝，
    那两条用例就会变成"看起来在检查门禁、其实检查的是另一个东西"。
    """
    row = {"shop_ref": S1_REF, "listing_ref": "lst-0123456789ab",
           "expected_amount": "19.90", "actual_amount": "29.90",
           "amount_difference": "10", "audit_status": "mismatch",
           "price_basis": "list_price", "currency": "CNY",
           "snapshot_at": "2026-09-13T11:40:00+08:00"}
    return {
        "status": "partial",
        "audit": {"expected_items": 1, "evaluated_items": 1, "matched_items": 0,
                  "all_correct": False, "counts": {"mismatch": 1},
                  "sources": [{"shop_ref": S1_REF, "source_kind": "official_export",
                               "enumeration_complete": True, "fresh": True}],
                  "rule_version": "listing-rules/2026-09-13.1"},
        "data": [row],
        "filters": {"currency": "CNY", "price_basis": "list_price", "as_of": "latest",
                    "expected_prices": [{"applies_to": "all_selected",
                                         "expected_amount": "19.90",
                                         "currency": "CNY",
                                         "price_basis": "list_price"}]},
        "limitations": [],
    }
