"""上架复核的数据访问：快照读取、roster 与本轮目标价冻结。

三条结构性约束：

1. **读走视图，写走授权**。复核图以 `bi_app` 身份连接，快照只能从
   `reporting.v_listing_*` 读（018 把底表从 bi_app 收回）；`evidence` 这类取证
   凭据不进视图，也就不会经任何投影漏给模型。
2. **每家店只取本轮需要的那一次快照**：一条按店铺集合的窗口查询取回全部快照头，
   再一条按 snapshot_id 集合取回明细。逐店循环查询正是 spec §2 反对的做法，
   也让"一次复核"在不同店里读到不同时刻的数据版本。
3. **写入只在 `persist_audit` 发生**，而且必须落在只读快照之外：读事务里写库
   会被 `transaction_read_only` 直接拒掉，那才是真实的部署形状。
"""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any, NamedTuple, Sequence

from bi_agent.catalog import REF_RE
from bi_agent.commerce.repository import ShopProfile, shop_profiles

from .models import RosterItem, SnapshotBundle, SnapshotHeader, SnapshotItem
from .rules import (
    APPLIES_TO_VALUES, CURRENCY_PRECISIONS, LISTING_REF_RE, LISTING_SOURCE_KINDS,
    PRICE_BASES)

_DECIMAL_TEXT_RE = re.compile(r"^-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?$")

MAX_SHOPS_PER_QUERY = 200


class ChannelLanding(NamedTuple):
    """一条已确认映射给出的落点：哪个账号范围、哪家店、哪条链接、哪个平台 SKU。"""

    namespace: str
    platform: str
    shop_id: str
    listing_id: str
    platform_sku_id: str
    erp_product_id: str
    erp_sku_id: str
    status: str
    source: str
    mapping_version: str

    def roster_item(self, shop_ref: str) -> RosterItem:
        return RosterItem(shop_id=self.shop_id, shop_ref=shop_ref,
                          namespace=self.namespace, listing_id=self.listing_id,
                          platform_sku_id=self.platform_sku_id,
                          erp_sku_id=self.erp_sku_id)


def insert_listing_snapshot(conn, *, snapshot_id: str, shop_id: str, platform: str,
                            namespace: str, source: str, evidence: str,
                            captured_at: datetime | None,
                            enumeration_complete: bool,
                            enumeration_evidence: str | None = None,
                            batch_id: str | None = None,
                            schema_version: str = "018") -> None:
    """登记一次渠道在售快照（导入 / 连接器路径，不由模型触发）。

    参数在进入 SQL 前先按契约过一遍：018 的 CHECK 会拒同一批形状，但把拒绝放在
    这里能让调用方拿到**具名**错误，而不是一个数据库约束码。
    """
    if source not in LISTING_SOURCE_KINDS:
        raise ValueError("listing_source_kind_unapproved")
    if not str(evidence or "").strip():
        raise ValueError("listing_snapshot_evidence_required")
    if captured_at is None:
        raise ValueError("listing_snapshot_captured_at_required")
    if not str(namespace or "").strip():
        raise ValueError("listing_snapshot_namespace_required")
    if enumeration_complete and not str(enumeration_evidence or "").strip():
        # 「完整枚举」是一个主张，不是一个布尔偏好：没有凭据就不许声明。
        raise ValueError("listing_snapshot_enumeration_evidence_required")
    conn.execute(
        """INSERT INTO bi.listing_snapshots (
               snapshot_id, namespace, platform, shop_id, source, evidence,
               captured_at, enumeration_complete, enumeration_evidence, batch_id,
               schema_version
           ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
           ON CONFLICT (namespace, shop_id, snapshot_id) DO NOTHING""",
        (snapshot_id, namespace, str(platform or "").strip().lower(), shop_id, source,
         str(evidence).strip(), captured_at, bool(enumeration_complete),
         str(enumeration_evidence).strip() if enumeration_complete and
         enumeration_evidence else None, batch_id, schema_version))


def insert_listing_snapshot_item(conn, *, snapshot_id: str, shop_id: str,
                                 listing_id: str, namespace: str,
                                 platform_sku_id: str = "", erp_sku_id: str = "",
                                 erp_product_id: str | None = None,
                                 list_amount: Any = None, campaign_amount: Any = None,
                                 currency: str = "CNY", on_sale: bool = True,
                                 captured_at: datetime | None = None) -> None:
    """登记快照里的一条在售记录。

    `listing_id` 为空、两种标价都没声明、金额为负都由 018 的 CHECK 兜住；
    这里只补一条 SQL 兜不住的：空链接号。
    """
    if not str(namespace or "").strip():
        # 明细的主键与 FK 都含 namespace：漏写就是写出一条不属于任何快照批次的行，
        # 而数据库报的是 FK 违反——一个本来可以在入口就说清的错误。
        raise ValueError("listing_snapshot_item_namespace_required")
    if not str(listing_id or "").strip():
        raise ValueError("listing_snapshot_item_listing_required")
    if list_amount is None and campaign_amount is None:
        raise ValueError("listing_snapshot_item_declares_no_price")
    conn.execute(
        """INSERT INTO bi.listing_snapshot_items (
               snapshot_id, shop_id, namespace, listing_id, platform_sku_id,
               erp_sku_id, erp_product_id, list_amount, campaign_amount, currency,
               on_sale, captured_at
           ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
           ON CONFLICT (namespace, shop_id, snapshot_id, listing_id, platform_sku_id)
           DO UPDATE SET list_amount = EXCLUDED.list_amount,
                         campaign_amount = EXCLUDED.campaign_amount,
                         currency = EXCLUDED.currency,
                         on_sale = EXCLUDED.on_sale,
                         erp_sku_id = EXCLUDED.erp_sku_id,
                         erp_product_id = EXCLUDED.erp_product_id""",
        (snapshot_id, shop_id, namespace, str(listing_id).strip(), platform_sku_id,
         erp_sku_id, erp_product_id or "", list_amount, campaign_amount,
         str(currency or "CNY").strip().upper(), bool(on_sale), captured_at))


_LATEST_SNAPSHOTS_SQL = """
SELECT DISTINCT ON (namespace, shop_id)
       snapshot_id, namespace, shop_id, platform, source, captured_at,
       enumeration_complete, batch_id
FROM reporting.v_listing_snapshots
WHERE shop_id = ANY(%s)
  AND captured_at <= %s
ORDER BY namespace, shop_id, captured_at DESC, snapshot_id
"""

_SNAPSHOT_ITEMS_SQL = """
SELECT snapshot_id, namespace, shop_id, listing_id, platform_sku_id,
       erp_sku_id, erp_product_id, list_amount, campaign_amount, currency, on_sale,
       captured_at
FROM reporting.v_listing_snapshot_items
WHERE namespace = ANY(%s) AND snapshot_id = ANY(%s)
ORDER BY namespace, shop_id, listing_id, platform_sku_id
"""

# 一个 (namespace, snapshot_id) 的数组对：不用 ANY(ANY) 的笛卡尔形状，
# 那会让"这家店这次快照的明细"变成"这几家店这几批快照的交叉"。
_CHANNEL_ITEMS_SQL = """
SELECT namespace, platform, shop_id, listing_id, platform_sku_id,
       erp_product_id, erp_sku_id, status, source, mapping_version
FROM reporting.v_channel_items
WHERE erp_product_id = %s AND valid_from <= %s
  AND (valid_to IS NULL OR valid_to >= %s) AND shop_id = ANY(%s)
ORDER BY namespace, shop_id, listing_id, platform_sku_id
"""


def latest_snapshots(conn, *, shop_ids: Sequence[str], as_of: datetime,
                     deadline: float) -> dict[tuple[str, str], SnapshotBundle]:
    """每家店每个账号范围本轮读到的那一次快照（`as_of` 之前最新的一批）。

    返回按 `(shop_id, namespace)` 索引；一家店都没有就是空字典 —— 空字典不等于
    "这些店都没上架"，调用方必须把缺快照与缺商品分开处理（join 节点正是为此存在的）。
    """
    shops = sorted({str(shop) for shop in shop_ids if str(shop or "").strip()})
    if not shops:
        return {}
    if len(shops) > MAX_SHOPS_PER_QUERY:
        raise ValueError("listing_snapshot_scope_too_large")
    _check_budget(conn, deadline)
    rows = conn.execute(_LATEST_SNAPSHOTS_SQL, (shops, as_of)).fetchall()
    headers = [SnapshotHeader(
        snapshot_id=str(row[0]), namespace=str(row[1]), shop_id=str(row[2]),
        platform=str(row[3] or ""), source=str(row[4]), captured_at=row[5],
        enumeration_complete=bool(row[6]), batch_id=(str(row[7]) if row[7] else None))
        for row in rows]
    if not headers:
        return {}
    _check_budget(conn, deadline)
    pairs = sorted({header.snapshot_id for header in headers})
    namespaces = sorted({header.namespace for header in headers})
    items: dict[tuple[str, str], list[SnapshotItem]] = {}
    for row in conn.execute(_SNAPSHOT_ITEMS_SQL, (namespaces, pairs)).fetchall():
        key = (str(row[1]), str(row[0]))
        items.setdefault(key, []).append(SnapshotItem(
            snapshot_id=str(row[0]), shop_id=str(row[2]), listing_id=str(row[3]),
            platform_sku_id=str(row[4] or ""), erp_sku_id=str(row[5] or ""),
            erp_product_id=str(row[6] or ""),
            list_amount=(None if row[7] is None else str(row[7])),
            campaign_amount=(None if row[8] is None else str(row[8])),
            currency=str(row[9] or "CNY"), on_sale=bool(row[10]), captured_at=row[11]))
    # 一家店可以在两个账号范围里各有一批快照（同一 shop_id 被两个 namespace 同步）。
    # 按 shop_id 编址会让后读到的那一批把前一批顶掉：判定就会拿 A 范围的完整枚举去
    # 证明 B 范围的链接"未上架"，那是另一账号的凭据。所以键里必须带 namespace，
    # join 那一侧再按 `item.namespace` 精确取用。
    bundles: dict[tuple[str, str], SnapshotBundle] = {}
    for header in headers:
        mine = tuple(items.get((header.namespace, header.snapshot_id), ()))
        bundles[(header.shop_id, header.namespace)] = SnapshotBundle(header, mine)
    return bundles


def channel_landings(conn, *, erp_product_id: str, shop_ids: Sequence[str],
                     at: date, deadline: float) -> tuple[ChannelLanding, ...]:
    """某商品在授权店铺上的渠道落点（含无成交的链接）。

    不复用 `catalog.expand_channel_items` 而自带一条 SQL，原因有两个：本域需要
    `namespace` 与 `platform_sku_id` 才能定位到"哪一条链接"（roster 的粒度），而
    `ChannelItem` 不带这两个字段里的任何一个可用于派生句柄的完整组合。
    """
    shops = sorted({str(shop) for shop in shop_ids if str(shop or "").strip()})
    if not shops or not erp_product_id:
        return ()
    _check_budget(conn, deadline)
    rows = conn.execute(_CHANNEL_ITEMS_SQL, (erp_product_id, at, at, shops)).fetchall()
    return tuple(ChannelLanding(
        namespace=str(row[0]), platform=str(row[1] or ""), shop_id=str(row[2]),
        listing_id=str(row[3] or ""), platform_sku_id=str(row[4] or ""),
        erp_product_id=str(row[5] or ""), erp_sku_id=str(row[6] or ""),
        status=str(row[7]), source=str(row[8]), mapping_version=str(row[9] or ""))
        for row in rows)


def record_expected_roster(conn, *, run_id: Any, subject_id: str, items,
                           price_basis: str, currency: str = "CNY") -> None:  # noqa: ANN001
    """把本轮期望复核项落表：分母要能在事后被数出来。

    目标价不写进这张表（它在 `price_audit_expectations`）：roster 只说「这一格要复核」，
    两件事写在一起就会让「没给目标价的格」从分母里消失。
    """
    rows = list(items)
    if not rows:
        return
    placeholders = ", ".join(["(%s, %s, %s, %s, %s, %s, %s)"] * len(rows))
    params: list[Any] = []
    for shop_id, listing_ref, sku_ref in rows:
        params.extend([run_id, subject_id, shop_id, listing_ref, sku_ref, currency,
                       price_basis])
    conn.execute(
        f"""INSERT INTO bi.expected_listing_rosters (
                run_id, subject_id, shop_id, listing_ref, sku_ref, currency,
                price_basis
            ) VALUES {placeholders}
            ON CONFLICT (run_id, shop_id, listing_ref, sku_ref) DO NOTHING""",
        tuple(params))


# 普通 INSERT，不是 `ON CONFLICT DO UPDATE`。两个理由叠在一起：
#   1) DO UPDATE 会向应用身份要到 UPDATE 权限，而 018 故意不给——"本轮依据一次写入后
#      不可改写"是这条链的存储层形状，不是可以事后补的权限；
#   2) 同一个 (run, 店, SKU) 写两次本身就是程序错误，静默覆盖会把"改过标准"这件事
#      演成"标准一直如此"。合并只在 `_dedupe_expectations` 里做，且必须逐字段一致。
_EXPECTATION_INSERT_SQL = """
INSERT INTO bi.price_audit_expectations (
    run_id, subject_id, shop_id, sku_ref, expected_amount, currency, price_basis,
    applies_to, captured_from, request_fingerprint
) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'current_user_input', %s)
"""


def dedupe_expectations(entries):  # noqa: ANN001
    """一个 (店, SKU) 只冻一行；同一条链接的多个落点共享同一标准是允许的。

    同键但金额 / 币种 / 口径 / 档位不一致就不是"同一句话说了两遍"，而是代码在同一次
    执行里对同一格给了两个标准：那种情况直接报错（外层降级为写入失败），不选一个。
    """
    by_key: dict[tuple[str, str], list] = {}
    for entry in entries:
        by_key.setdefault((entry.shop_id, entry.sku_ref), []).append(entry)
    merged = []
    for (shop_id, sku_ref), group in sorted(by_key.items()):
        first = group[0]
        if any(entry.expected_amount != first.expected_amount
               or entry.currency != first.currency
               or entry.price_basis != first.price_basis
               or entry.applies_to != first.applies_to for entry in group[1:]):
            raise ValueError("audit_basis_conflicts_within_run")
        merged.append(first)
    return merged


def record_price_audit_expectations(conn, *, run_id: Any, subject_id: str,
                                    fingerprint: str | None, entries) -> None:  # noqa: ANN001
    """冻结本轮用户指定的目标价（一行一格）。

    `captured_from` 由 SQL 常量写死成 `'current_user_input'`，配合 018 的 CHECK，
    「继承上一轮」在这条路上写不出行：要让一个旧标准重新生效，必须有人再问用户一次。

    本函数只写不读。读取路径由 `tests/test_listing_audit.py` 用 SQL 断言钉住。
    """
    rows = dedupe_expectations(list(entries))
    if not rows:
        return
    for entry in rows:
        conn.execute(_EXPECTATION_INSERT_SQL, (
            run_id, subject_id, entry.shop_id, entry.sku_ref, entry.expected_amount,
            entry.currency, entry.price_basis, entry.applies_to, fingerprint))


class ExpectationRow(NamedTuple):
    """一条待冻结的本轮目标价。"""

    shop_id: str
    sku_ref: str
    expected_amount: str
    currency: str
    price_basis: str
    applies_to: str


# ---------------------------------------------------------------------------
# 运行级持久化入口（由 QueryRunStore 调用）
# ---------------------------------------------------------------------------
# 为什么不在复核图里直接写这两张表：它们的 `run_id` 外键指向 `bi.query_runs`，而运行
# 记录由各 Store 写。图绕过 Store 去写，内存 Store 下一跑就是 FK 违反（而那只在测试里
# 暴露，真实部署用 Postgres Store 反而不会报）——一个只在测试里通过的写入路径不是
# 写入路径。Task 3 已经把运行级持久化定在 Store 一处（`save_artifact` /
# `record_provenance` / `record_diagnostic`），本域同一做派：Store 持有事务与身份，
# 一张表的 SQL 仍只写在本模块里。

_FINGERPRINT_RE = re.compile(r"^[0-9a-f]{64}$")


def validate_audit_basis(*, subject_id: str, fingerprint: str | None, roster,
                         expectations, price_basis: str, currency: str) -> tuple:
    """把外层递来的元组校成可落库的形状；两种 Store 共用这一份，不各校一遍。

    roster 项是 `(shop_id, listing_ref, sku_ref)`，目标价项是
    `(shop_id, sku_ref, expected_amount, applies_to)`。用元组而不是领域对象：Store
    不需懂 roster 的语义，它只需守住形式。拒绝在入口比数据库 CHECK 报错更早，也不会
    把一个未登记的说法藏进一次回滚里。
    """
    if not str(subject_id or "").strip():
        raise ValueError("audit_basis_subject_required")
    if price_basis not in PRICE_BASES:
        raise ValueError("audit_basis_price_basis_unsupported")
    if currency not in CURRENCY_PRECISIONS:
        raise ValueError("audit_basis_currency_unregistered")
    if fingerprint is not None and not _FINGERPRINT_RE.fullmatch(str(fingerprint)):
        raise ValueError("audit_basis_fingerprint_format")
    clean_roster: list[tuple[str, str, str]] = []
    for entry in roster:
        shop_id, listing_ref, sku_ref = (str(part) for part in entry)
        if not shop_id.strip():
            raise ValueError("audit_basis_shop_required")
        if listing_ref and not LISTING_REF_RE.fullmatch(listing_ref):
            raise ValueError("audit_basis_listing_ref_format")
        if sku_ref and not REF_RE.fullmatch(sku_ref):
            raise ValueError("audit_basis_sku_ref_format")
        clean_roster.append((shop_id, listing_ref, sku_ref))
    clean_expectations: list[tuple[str, str, str, str]] = []
    for entry in expectations:
        shop_id, sku_ref, amount, applies_to = entry
        shop_id, sku_ref, applies_to = str(shop_id), str(sku_ref), str(applies_to)
        if not shop_id.strip():
            raise ValueError("audit_basis_shop_required")
        if sku_ref and not REF_RE.fullmatch(sku_ref):
            raise ValueError("audit_basis_sku_ref_format")
        # 金额只收**字符串文本**：float 转成文本也能过正则，但它带的是 0.1+0.2 那类
        # 二进制误差，而上架复核的全部意义就在那一分钱上（与 `to_decimal` 同一口径：
        # 拒 float 比静默转换诚实）。
        if not isinstance(amount, str) or not _DECIMAL_TEXT_RE.fullmatch(amount):
            raise ValueError("audit_basis_amount_format")
        if applies_to not in APPLIES_TO_VALUES:
            raise ValueError("audit_basis_applies_to_unsupported")
        clean_expectations.append((shop_id, sku_ref, amount, applies_to))
    return tuple(clean_roster), tuple(clean_expectations)


def write_audit_basis(conn, *, run_id: Any, subject_id: str, fingerprint: str | None,
                      roster, expectations, price_basis: str,
                      currency: str) -> None:  # noqa: ANN001
    """一次事务里写完 roster 与本轮目标价：两者要么一起成立，要么都不落。"""
    clean_roster, clean_expectations = validate_audit_basis(
        subject_id=subject_id, fingerprint=fingerprint, roster=roster,
        expectations=expectations, price_basis=price_basis, currency=currency)
    with conn.transaction():
        record_expected_roster(conn, run_id=run_id, subject_id=subject_id,
                               items=clean_roster, price_basis=price_basis,
                               currency=currency)
        entries = [ExpectationRow(shop_id=shop_id, sku_ref=sku_ref,
                                  expected_amount=amount, currency=currency,
                                  price_basis=price_basis, applies_to=applies_to)
                   for shop_id, sku_ref, amount, applies_to in clean_expectations]
        record_price_audit_expectations(
            conn, run_id=run_id, subject_id=subject_id, fingerprint=fingerprint,
            entries=entries)


def _check_budget(conn, deadline: float) -> None:
    """与经营图同一套预算：把 statement_timeout 收紧到剩余时间。

    预算耗尽抛 `_BudgetExhausted`，由图上统一降级为 unavailable —— 绝不能让
    一次超时查询返回空行，那会被 join 节点读成"这家店什么都没上架"。
    """
    from bi_agent.metrics import _BudgetExhausted, _set_query_budget

    if not _set_query_budget(conn, deadline):
        raise _BudgetExhausted


__all__ = ["ChannelLanding", "ExpectationRow", "SnapshotBundle", "channel_landings",
           "insert_listing_snapshot", "insert_listing_snapshot_item",
           "latest_snapshots", "record_expected_roster",
           "record_price_audit_expectations", "shop_profiles", "ShopProfile",
           "validate_audit_basis", "write_audit_basis"]
