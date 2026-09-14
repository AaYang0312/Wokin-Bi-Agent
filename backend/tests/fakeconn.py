"""测试替身：目录投影（bi_agent.catalog.projection）只读这两条 reporting 视图。

指标查询在各测试文件里都被替换掉，所以这里只需要回答：
- 全表店铺档案 → 引用展示名
- 目录版本 → Artifact 记录它解析名称时用的版本

真实数据库用例（tests.test_catalog / test_api）走真视图，不用本替身。
"""

from __future__ import annotations

from typing import Sequence

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
