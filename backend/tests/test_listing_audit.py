"""Task 9：用户指定目标价的上架复核（listing_price_audit）。

计划 Task 9 点名的失败用例一次到位：当次目标价缺失、上一轮价不可隐式继承、
多 SKU / 多链接、缺一家、币种不一致、过期与缺完整枚举证据。另加本任务的三条红线：

1. **来源门禁**。交付代码里没有任何已核验的渠道在售价来源，所以真实部署下 Tool
   只能报 `unsupported`，不能声称全店复核可用（spec §9：真实功能开关必须等字段、
   授权、时效、分页完整性和对账证据全部齐备）。测试同时钉住「默认关」与
   「取证之后确实能出判定」，否则门禁只是一个永不为真的常量，也就证明不了它守住了什么。
2. **目标价只来自本轮**。缺来源与缺目标价都不许回退到上一轮、历史售价或档案建议价；
   本文件用记录型连接断言图内**从不 SELECT** `bi.price_audit_expectations`。
3. **判定分母是期望 roster**。只检查抓到的链接会把「缺一家」说成「全部正确」。

真实测试库跑法与既有约定一致：管理员连接 + 外层事务回滚，只写合成店铺。
无 DSN 时显式 skip——skip 不是通过证明。
"""

from __future__ import annotations

import os
import time
import unittest
from datetime import date, datetime, timedelta
from typing import Sequence
from decimal import Decimal
from unittest import mock
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

import psycopg
from pydantic import ValidationError

from bi_agent.catalog import ref_for_key
from bi_agent.catalog.channel_mapping import (
    DEFAULT_NAMESPACE, IdentifierMapping, record_identifier_mapping)
from bi_agent.catalog.models import EntityKind
from bi_agent.commerce.models import DomainContext
from bi_agent.commerce.repository import ShopProfile
from bi_agent.listing_audit import repository
from bi_agent.listing_audit.graph import LISTING_GRAPH_VERSION, ListingExecution
from bi_agent.listing_audit.graph import (
    ListingNode, ListingRuntime, ListingState, TEXT_INCOMPLETE, TEXT_SOURCE_UNVERIFIED,
    check_listing_source, classify_discrepancies, compare_decimal_prices,
    _guard_invariants, join_expected_and_actual, verify_completeness_and_freshness)
from bi_agent.listing_audit.graph import run_listing_audit_graph
from bi_agent.listing_audit.models import (
    AuditItem, ListingPriceAuditRequest, RosterItem, SnapshotBundle, SnapshotHeader,
    SnapshotItem)
from bi_agent.listing_audit.rules import (
    AUDIT_STATUSES, CNY_PRECISION, LISTING_CODE_BY_TEXT,
    LISTING_GRAPH_VERSION as RULES_GRAPH_VERSION,
    LISTING_LIMITATION_CODES, LISTING_METRIC_VERSION, LISTING_NUMERIC_RESULT_COLUMNS,
    LISTING_PUBLIC_LIMITATIONS, LISTING_SCHEMA_VERSION, LISTING_SOURCE_KINDS,
    LISTING_SOURCE_REGISTRY_VERSION, MAX_ROSTER_ITEMS, PRICE_BASES, RosterConflict,
    ListingSourceRegistration,
    amount_difference, compare_price, currency_precision, freshness_ok,
    normalize_amount, register_listing_source, reset_listing_sources, price_text,
    resolve_price_rules, verified_listing_source, verified_listing_sources)
from bi_agent.listing_audit.graph import TEXT_SOURCE_UNVERIFIED
from bi_agent.listing_audit.tool import (
    audit_listing_prices, listing_audit_request_schema)
from bi_agent.runtime.artifacts import QueryProvenance, request_fingerprint
from bi_agent.runtime.domain_registry import LISTING_NODES, allows_artifact_type, spec_for
from bi_agent.runtime.memory import MemoryQueryRunStore
from bi_agent.runtime.models import (
    NewQueryRun, validate_artifact_payload, validate_model_payload,
    validate_persisted_state)

from .dbfixtures import connect_test_db

BEIJING = ZoneInfo("Asia/Shanghai")
NOW = datetime(2026, 9, 13, 12, tzinfo=BEIJING)
FRESH = NOW - timedelta(minutes=20)
OLDER = NOW - timedelta(hours=5)
STALE = NOW - timedelta(days=30)
FRESH_ISO = FRESH.astimezone(BEIJING).isoformat()
NEWER_ISO = (NOW - timedelta(minutes=5)).astimezone(BEIJING).isoformat()
PRODUCT = "P9"
AUDIT_ARTIFACT = "price_audit"


def _shop_ref(key: str) -> str:
    return ref_for_key("shop", f"S{key}")


def _sku_ref(sku: str) -> str:
    return ref_for_key(EntityKind.SKU.value, sku)


def _price(amount: str, **overrides: object) -> dict:
    entry: dict[str, object] = {"applies_to": "all_selected",
                               "expected_amount": amount, "currency": "CNY"}
    entry.update(overrides)
    return entry


def _request(**overrides: object) -> ListingPriceAuditRequest:
    base: dict[str, object] = {
        "product": {"text": "直钉枪"},
        "scope": {"mode": "all_authorized"},
        "as_of": "latest",
        "price_basis": "list_price",
        "expected_prices": [_price("19.90")],
    }
    base.update(overrides)
    return ListingPriceAuditRequest.model_validate(base)


def _payload_for(result, artifact_type: str = AUDIT_ARTIFACT) -> dict:
    matches = [artifact.public_payload for artifact in result.artifacts
               if artifact.ref.type == artifact_type]
    assert len(matches) == 1, ([artifact.ref.type for artifact in result.artifacts],
                               artifact_type)
    return matches[0]


def _rows(payload: dict) -> list[dict]:
    return [row for row in payload["data"] if "audit_status" in row]


def _row_for(payload: dict, *, shop_ref: str, sku_ref: str | None = None,
             listing_ref: str | None = None) -> dict:
    """按 (店, SKU, 链接) 取那一行：命中多行就是断言没钉住粒度。"""
    hits = [row for row in _rows(payload)
            if row["shop_ref"] == shop_ref
            and (sku_ref is None or row.get("sku_ref") == sku_ref)
            and (listing_ref is None or row.get("listing_ref") == listing_ref)]
    assert len(hits) == 1, (shop_ref, sku_ref, listing_ref, _rows(payload))
    return hits[0]


def _status_for(payload: dict, **keys: str) -> str:
    return str(_row_for(payload, **keys)["audit_status"])


def _limitation_text(payload: dict) -> str:
    return "；".join(str(item) for item in payload.get("limitations") or [])


class _RecordingConn:
    """记录每条 SQL：用来证明「本轮目标价不回退到上一轮」不是只靠注释。"""

    def __init__(self, conn) -> None:  # noqa: ANN001
        self.conn = conn
        self.sql: list[str] = []

    def execute(self, sql, params=None):  # noqa: ANN001
        self.sql.append(" ".join(str(sql).split()))
        return self.conn.execute(sql, params) if params is not None \
            else self.conn.execute(sql)

    def __getattr__(self, name):  # noqa: ANN001
        return getattr(self.conn, name)

    def matching(self, *fragments: str) -> list[str]:
        return [entry for entry in self.sql
                if all(fragment in entry for fragment in fragments)]


class _FailingExpectationWrite:
    """只让 price_audit_expectations 的写入失败：必需结果存不下必须是 failed。"""

    def __init__(self, conn) -> None:  # noqa: ANN001
        self.conn = conn
        self.attempts = 0

    def execute(self, sql, params=None):  # noqa: ANN001
        text = " ".join(str(sql).split())
        if text.upper().startswith("INSERT") and "bi.price_audit_expectations" in text:
            self.attempts += 1
            raise psycopg.errors.UndefinedTable("simulated persistence failure")
        return self.conn.execute(sql, params) if params is not None \
            else self.conn.execute(sql)

    def __getattr__(self, name):  # noqa: ANN001
        return getattr(self.conn, name)


def _reply(text: str | None = None, calls: list | None = None):  # noqa: E501, ANN001
    """构造一个带 `_message` 的模型回合：`ModelReply.as_message()` 需要它。

    与 `tests/test_core._reply` 同一形状——主层在每次回合后都会 `as_message()`，
    直接 `ModelReply(...)` 会在那一步炸掉。
    """
    from bi_agent.llm import Message, ModelReply

    calls = calls or []
    reply = ModelReply(text=text, tool_calls=calls)
    reply._message = Message(role="assistant", content=text, tool_calls=calls)
    return reply


class _ShopListConn:
    """主层接线用例用的连接替身：只回答 `_fetch_shops` 那一条两列档案查询。

    其他任何 SQL 都直接报错。图本身被打桩，所以主层不应再读一次库；一个会回话的
    万能替身会把"主层自己多算了一次查询"这类错归入默响。
    """

    def __init__(self, shops: Sequence[tuple[str, str]] = (("S1", "钉枪工厂店"),)) -> None:
        self.shops = list(shops)
        self.queries: list[str] = []

    def execute(self, sql, params=None):  # noqa: ANN001
        text = " ".join(str(sql).split())
        self.queries.append(text)
        if "FROM reporting.v_shops" not in text:
            raise AssertionError(f"主层不应在本用以外再读一次库：{text[:120]}")
        rows = list(self.shops)

        class _Result:
            def fetchall(inner):  # noqa: ANN001
                return rows

            def fetchone(inner):  # noqa: ANN001
                return rows[0] if rows else None
        return _Result()


class _FailingStore(MemoryQueryRunStore):
    def save_artifact(self, run_id, artifact):  # noqa: ANN001
        if artifact.artifact_type == "price_audit":
            raise RuntimeError("simulated artifact failure")
        return super().save_artifact(run_id, artifact)


# ---------------------------------------------------------------------------
# 1. 纯规则：Decimal 比较、规范化与时效
# ---------------------------------------------------------------------------


class ListingPriceRuleTests(unittest.TestCase):
    """计划 Task 9 逐字给出的三条断言，加上本任务自己需要的边界。"""

    def test_exact_price_and_missing_target(self):
        # 计划正文里那四行断言，原样成立。
        self.assertEqual(compare_price("19.90", "19.9"), "match")
        self.assertEqual(compare_price("29.90", "19.90"), "mismatch")
        self.assertEqual(compare_price("19.90", None), "missing_standard")
        self.assertEqual(compare_price(None, "19.90"), "unknown")

    def test_both_sides_missing_is_missing_standard_not_match(self):
        """两端都没有就是「没有标准」：它比「价格未知」更可执行，缺的是用户那一侧。"""
        self.assertEqual(compare_price(None, None), "missing_standard")

    def test_compare_price_never_invents_a_tolerance(self):
        """spec §4：默认精确比较，不引入容差，也不靠舍入把差异抹平。"""
        self.assertEqual(compare_price("19.90", "19.899"), "mismatch",
                         "把 19.899 舍成 19.90 就是把一分钱的差异说成没差")
        self.assertEqual(compare_price("0.00", "0"), "match",
                         "真实零价与 0 是同一个数；缺价是 null，不是 0")

    def test_unparseable_price_is_unknown_rather_than_a_crash_or_zero(self):
        for actual in ("", "  ", "19,90", "19.90元", "abc", "1e3", "-", "19.9.0"):
            with self.subTest(actual=actual):
                self.assertEqual(compare_price(actual, "19.90"), "unknown")

    def test_price_text_keeps_the_currency_scale_without_rounding(self):
        self.assertEqual(price_text("39.9000", "CNY"), "39.90")
        self.assertEqual(price_text("20", "CNY"), "20.00")
        self.assertEqual(price_text("19.899", "CNY"), "19.899",
                         "来源给了三位小数就发三位：舍到两位会把差异说成没差")
        self.assertIsNone(price_text("19.90", "USD"))

    def test_currency_precision_is_registered_or_absent(self):
        self.assertEqual(currency_precision("CNY"), CNY_PRECISION)
        # 未登记币种没有「已核验的货币精度」：不能凭猜量化，比较结果只能是 unknown。
        self.assertIsNone(currency_precision("USD"))
        self.assertIsNone(currency_precision(""))
        self.assertIsNone(currency_precision(None))
        self.assertEqual(compare_price("19.90", "19.90", currency="USD"), "unknown")

    def test_normalize_amount_returns_decimal_or_none(self):
        self.assertEqual(normalize_amount("19.900", "CNY"), Decimal("19.90"))
        self.assertEqual(normalize_amount("20", "CNY"), Decimal("20.00"))
        self.assertIsNone(normalize_amount("19.90", "USD"))
        self.assertIsNone(normalize_amount("drop table", "CNY"))

    def test_amount_difference_is_signed_and_none_when_undecidable(self):
        self.assertEqual(amount_difference("29.90", "19.90"), "10")
        self.assertEqual(amount_difference("9.90", "19.90"), "-10")
        self.assertEqual(amount_difference("19.90", "19.90"), "0")
        self.assertIsNone(amount_difference(None, "19.90"))
        self.assertIsNone(amount_difference("19.90", None))
        self.assertIsNone(amount_difference("x", "19.90", currency="CNY"))

    def test_freshness_policy_is_a_boundary_not_a_vibe(self):
        self.assertTrue(freshness_ok(captured_at=NOW - timedelta(hours=23),
                                     now=NOW, max_age_seconds=86400))
        self.assertFalse(freshness_ok(captured_at=NOW - timedelta(hours=25),
                                      now=NOW, max_age_seconds=86400))
        self.assertFalse(freshness_ok(captured_at=None, now=NOW, max_age_seconds=86400),
                         "没有抓取时间就没有时效可言：判过期，不判新鲜")

    def test_status_vocabulary_matches_the_design(self):
        # spec §5.4 的九个状态，一个不多一个不少：多出来的状态没有对应的恢复动作。
        self.assertEqual(set(AUDIT_STATUSES), {
            "match", "mismatch", "not_listed", "not_on_sale", "missing_standard",
            "unmapped", "stale", "unsupported", "unknown"})
        self.assertEqual(set(PRICE_BASES), {"list_price", "campaign_price"})

    def test_listing_columns_have_one_validation_bucket_each(self):
        """每一列都必须有明确的校验类别，而且只能有一个。

        拿"剩下就是数值"当兼容写法时，新登记一个不认识的类别会默默落进数值校验，
        把任意文本当金额收下来。本用例就是 C-3 那一类漏口的护栏。
        """
        from bi_agent.runtime import models as runtime_models

        listing = runtime_models.LISTING_RESULT_COLUMNS
        buckets = {
            "decimal": runtime_models._NUMERIC_RESULT_COLUMNS & listing,
            "ref": runtime_models._REF_RESULT_COLUMNS & listing,
            "datetime": runtime_models._DATETIME_RESULT_COLUMNS & listing,
            "listing_ref": runtime_models._LISTING_REF_RESULT_COLUMNS & listing,
            "label": frozenset(runtime_models._LABEL_RESULT_VALUES) & listing,
        }
        self.assertEqual(set(LISTING_NUMERIC_RESULT_COLUMNS), buckets["decimal"])
        union: set[str] = set()
        for bucket in buckets.values():
            self.assertFalse(union & set(bucket), "一列落两个类别就有两种校验结果")
            union |= set(bucket)
        self.assertEqual(union, set(listing) - {"currency"},
                         "currency 沿用运行契约已有的标签列，不在本域重定义")

    def test_resolve_price_rules_matches_per_variant(self):
        sku_a, sku_b = _sku_ref("SKU-A"), _sku_ref("SKU-B")
        shop_1, shop_2 = _shop_ref("1"), _shop_ref("2")
        items = [(shop_1, sku_a), (shop_1, sku_b), (shop_2, sku_a)]
        rules = [dict(applies_to="sku", sku_ref=sku_a, expected_amount="10.00",
                      currency="CNY", price_basis="list_price")]
        resolved = resolve_price_rules(rules, items)
        # `sku` 档说的是"这个规格在各家获准店铺都按这个价"，所以它跨店生效；
        # 要钉在一家店上必须用 shop_sku 档。两个档位是两个不同的问题。
        self.assertEqual(resolved.get((shop_1, sku_a)), "10.00")
        self.assertEqual(resolved.get((shop_2, sku_a)), "10.00")
        # 没被命中的项**不在**返回结果里（而不是一个 None 值）：调用方用 .get()，
        # 于是「没命中」与「命中但值为空」在代码里不可能被混为一谈。
        self.assertIsNone(resolved.get((shop_1, sku_b)))
        self.assertEqual(sorted(resolved), [(shop_1, sku_a), (shop_2, sku_a)])
        # shop_sku 档只命中那一家店
        pinned = resolve_price_rules(
            [dict(applies_to="shop_sku", shop_ref=shop_1, sku_ref=sku_a,
                  expected_amount="12.00", currency="CNY", price_basis="list_price")],
            items)
        self.assertEqual(sorted(pinned), [(shop_1, sku_a)])
        # 产品级那一格（sku_ref=None）不该被任何 sku 档命中：那等于替用户决定
        # "这个商品所有规格都是这个价"，而那句话只有 all_selected 能说。
        bare = [(shop_1, None)]
        self.assertEqual(resolve_price_rules(rules, bare), {})
        self.assertEqual(resolve_price_rules(
            [dict(applies_to="all_selected", sku_ref=None, shop_ref=None,
                  expected_amount="10.00", currency="CNY", price_basis="list_price")],
            bare), {bare[0]: "10.00"})

    def test_resolve_price_rules_raises_on_conflicting_targets(self):
        sku_a = _sku_ref("SKU-A")
        items = [(_shop_ref("1"), sku_a)]
        rules = [dict(applies_to="sku", sku_ref=sku_a, expected_amount="10.00",
                      currency="CNY", price_basis="list_price"),
                 dict(applies_to="all_selected", expected_amount="12.00",
                      currency="CNY", price_basis="list_price")]
        with self.assertRaises(RosterConflict) as error:
            resolve_price_rules(rules, items)
        self.assertEqual(list(error.exception.items), items)
        # 同一条目标价重复给两次不是冲突：那是同一句话说了两遍。
        same = resolve_price_rules([rules[0], dict(rules[0])], items)
        self.assertEqual(same[items[0]], "10.00")
    def test_listing_ref_is_stable_and_does_not_carry_the_channel_id(self):
        """同一 (namespace, 店, 链接, 平台 SKU) 必须稳定派生同一个 lst- 句柄：
        两条链接的比价结果要能跨轮次追到同一条链接上。"""
        item = RosterItem(shop_id="S1", listing_id="L1", platform_sku_id="PS1",
                          namespace=DEFAULT_NAMESPACE)
        again = RosterItem(shop_id="S1", listing_id="L1", platform_sku_id="PS1",
                           namespace=DEFAULT_NAMESPACE)
        self.assertEqual(item.listing_ref, again.listing_ref)
        self.assertRegex(item.listing_ref, r"^lst-[0-9a-f]{12}$")
        other = RosterItem(shop_id="S1", listing_id="L2", platform_sku_id="PS1",
                           namespace=DEFAULT_NAMESPACE)
        self.assertNotEqual(item.listing_ref, other.listing_ref)
        # 另一账号里的相同链接号不是同一条链接（namespace 进摘要）。
        elsewhere = RosterItem(shop_id="S1", listing_id="L1", platform_sku_id="PS1",
                               namespace="acct-other")
        self.assertNotEqual(item.listing_ref, elsewhere.listing_ref)


# ---------------------------------------------------------------------------
# 2. 来源门禁：交付代码里没有任何已核验来源
# ---------------------------------------------------------------------------


class ListingSourceRegistryTests(unittest.TestCase):

    def tearDown(self) -> None:
        reset_listing_sources()

    def test_no_platform_ships_with_a_verified_listing_source(self):
        """真实部署里 Tool 只能报 unsupported：这条断言就是「不冒充线上可用」的凭据。

        任何一次为某个平台登记已核验来源都必须改代码并带证据，也就必须重新过本文件
        的行为用例与验收报告，不能靠配置或入参悄悄放开。
        """
        self.assertEqual(verified_listing_sources(), {})
        for platform in ("tb", "tm", "fxg", "jd", "kuaishou", "wxsph", "pdd", "1688"):
            self.assertIsNone(verified_listing_source(platform))

    def test_no_module_outside_the_registry_can_turn_the_gate_on(self):
        """仓库级门禁：除了 `listing_audit.rules` 本身，任何产品代码都不能登记来源。

        只断言"此刻注册表为空"是不够的：测试夹具的 cleanup 会把一个产品代码里
        import 时就开了门的痕迹抹平。本用例扫全部产品模块源码，不靠跑顺序。
        """
        import pathlib

        root = pathlib.Path(__file__).resolve().parents[1] / "bi_agent"
        offenders: list[str] = []
        allowed = root / "listing_audit" / "rules.py"
        for path in sorted(root.rglob("*.py")):
            if path == allowed:
                continue      # 注册函数当然定义在自己那里：其余任何一处都是绕门
            if "register_listing_source(" in path.read_text(encoding="utf-8"):
                offenders.append(str(path.relative_to(root.parent)))
        self.assertEqual(offenders, [],
                         "开通一个平台必须带证据改注册表，不能从旁边绕进去")
        self.assertEqual(verified_listing_sources(), {})

    def test_source_registration_is_not_reachable_from_model_input(self):
        """入参契约里没有一个字段能影响来源门禁：能被模型传的开关不叫门禁。"""
        schema = str(listing_audit_request_schema())
        for forbidden in ("source", "evidence", "register", "max_age", "shop_id",
                          "snapshot"):
            self.assertNotIn(forbidden, schema)

    def test_registration_requires_evidence_and_a_freshness_policy(self):
        cases = (
            {"platform": "tb", "source_kind": "official_export", "evidence": "",
             "max_age_seconds": 86400},
            {"platform": "tb", "source_kind": "official_export", "evidence": "  ",
             "max_age_seconds": 86400},
            {"platform": "tb", "source_kind": "official_export", "evidence": "probe-1",
             "max_age_seconds": 0},
            {"platform": "tb", "source_kind": "official_export", "evidence": "probe-1",
             "max_age_seconds": -1},
            # ERP 建议价连候选来源种类都不算（spec §9）。
            {"platform": "tb", "source_kind": "erp_suggested_price",
             "evidence": "probe-1", "max_age_seconds": 86400},
            {"platform": "", "source_kind": "official_export",
             "evidence": "probe-1", "max_age_seconds": 86400})
        for kwargs in cases:
            with self.subTest(**kwargs):
                with self.assertRaises(ValueError):
                    register_listing_source(ListingSourceRegistration(**kwargs))

    def test_registration_is_readable_after_it_is_made(self):
        register_listing_source(ListingSourceRegistration(
            platform="tb", source_kind="official_export", evidence="probe-1",
            max_age_seconds=86400))
        entry = verified_listing_source(" TB ")
        self.assertIsNotNone(entry)
        self.assertEqual(entry.source_kind, "official_export")
        self.assertEqual(set(verified_listing_sources()), {"tb"})
        reset_listing_sources()
        self.assertIsNone(verified_listing_source("tb"))

    def test_source_kind_vocabulary_is_the_three_approved_channels(self):
        self.assertEqual(set(LISTING_SOURCE_KINDS),
                         {"channel_api", "official_export", "manual_import"})

    def test_registry_version_is_declared_once(self):
        """不拿同一个名字自己比自己：真正的跨层断言在血缘用例里（policy_version）。"""
        from bi_agent.listing_audit import graph

        self.assertIs(graph.LISTING_GRAPH_VERSION, RULES_GRAPH_VERSION)

    def test_vocabulary_does_not_drift_from_the_runtime_contract(self):
        """与运行契约比词表（与 009 的 SQL CHECK 同一做派）。

        披露文本与归因码必须在 `runtime.models` 里都已登记：否则它们会在运行现场
        以 `unsafe_persistence_payload` 的形式爆，而不是在开发时被抓到。
        """
        from bi_agent.runtime import models as runtime_models

        self.assertTrue(LISTING_LIMITATION_CODES <= runtime_models._LIMITATION_CODES,
                        sorted(LISTING_LIMITATION_CODES - runtime_models._LIMITATION_CODES))
        self.assertTrue(LISTING_PUBLIC_LIMITATIONS
                        <= runtime_models._PUBLIC_LIMITATIONS,
                        sorted(LISTING_PUBLIC_LIMITATIONS
                               - runtime_models._PUBLIC_LIMITATIONS))
        # 码表只能由文本表派生：手写两份就会有一边多一个名字。
        self.assertEqual(LISTING_LIMITATION_CODES,
                         frozenset(LISTING_CODE_BY_TEXT.values()))


# ---------------------------------------------------------------------------
# 3. 入参契约
# ---------------------------------------------------------------------------


class ListingAuditRequestContractTests(unittest.TestCase):

    def test_this_round_target_price_is_mandatory(self):
        """当次目标价缺失：入口就拒，不给图任何「也许能沿用上轮」的机会。"""
        for bad in ([], None, [{}], [{"expected_amount": "19.90"}]):
            with self.subTest(bad=bad):
                with self.assertRaises(ValidationError):
                    _request(expected_prices=bad)

    def test_expected_amount_must_be_a_decimal_string(self):
        for bad in ("19.9元", "19,90", "", "  ", "19.9.0", None):
            with self.subTest(amount=bad):
                with self.assertRaises(ValidationError):
                    _request(expected_prices=[_price(bad)])
        # 19.9 与 19.90 都是合法输入：规范化在 rules 里做，不在入口改用户给的数。
        self.assertEqual(_request(expected_prices=[_price("19.9")])
                         .expected_prices[0].expected_amount, "19.9")

    def test_applies_to_shape_is_enforced_per_variant(self):
        sku = _sku_ref("SKU-1")
        shop = _shop_ref("1")
        ok_cases = (
            [_price("19.90", applies_to="sku", sku_ref=sku)],
            [_price("19.90", applies_to="shop_sku", shop_ref=shop, sku_ref=sku)])
        for rules in ok_cases:
            _request(expected_prices=rules)
        bad_cases = (
            {"applies_to": "sku"},                                   # 缺 sku_ref
            {"applies_to": "sku", "sku_ref": sku, "shop_ref": shop},  # sku 不该带店
            {"applies_to": "shop_sku", "sku_ref": sku},              # 缺 shop_ref
            {"applies_to": "shop_sku", "shop_ref": shop},            # 缺 sku_ref
            {"applies_to": "all_selected", "shop_ref": shop},
            {"applies_to": "all_selected", "sku_ref": sku},
            {"applies_to": "shop", "shop_ref": shop},                # 未登记取值
            {"applies_to": "sku", "sku_ref": "SKU-1"})               # 不是 ent- 引用
        for bad in bad_cases:
            with self.subTest(**bad):
                with self.assertRaises(ValidationError):
                    _request(expected_prices=[_price("19.90", **bad)])

    def test_only_this_round_currencies_are_comparable(self):
        """币种词表与运行契约同源：不放开成任意三字母码。"""
        with self.assertRaises(ValidationError):
            _request(expected_prices=[_price("19.90", currency="USD")])
        with self.assertRaises(ValidationError):
            _request(expected_prices=[_price("19.90")], currency="USD")

    def test_as_of_is_latest_only(self):
        """历史时点必须有对应快照才能接受；本轮没有历史快照读取路径，所以拒。"""
        self.assertEqual(_request().as_of, "latest")
        with self.assertRaises(ValidationError):
            _request(as_of="2026-09-01")

    def test_price_basis_vocabulary(self):
        self.assertEqual(_request().price_basis, "list_price")
        self.assertEqual(_request(price_basis="campaign_price").price_basis,
                         "campaign_price")
        with self.assertRaises(ValidationError):
            _request(price_basis="member_price")
        with self.assertRaises(ValidationError):
            _request(expected_prices=[_price("19.90", price_basis="member_price")])

    def test_all_authorized_scope_refuses_extra_shop_refs(self):
        """`mode=all_authorized` 又带一份 shop_refs：在本领域里这就是答案的分母歧义。"""
        with self.assertRaises(ValidationError):
            _request(scope={"mode": "all_authorized",
                            "shop_refs": [_shop_ref("1")]})
        _request(scope={"mode": "selected", "shop_refs": [_shop_ref("1")]})

    def test_extra_fields_are_refused(self):
        """多传一个字段就当合法参数放行，等于给绕过白名单开门。"""
        with self.assertRaises(ValidationError):
            _request(shop_ids=["S1"])
        with self.assertRaises(ValidationError):
            ListingPriceAuditRequest.model_validate(
                {"product": {"text": "a", "shop_id": "S1"},
                 "expected_prices": [_price("19.90")]})

    def test_product_selector_is_ref_or_text_only(self):
        with self.assertRaises(ValidationError):
            _request(product={"text": "直钉枪", "ref": ref_for_key("product", "P1")})
        with self.assertRaises(ValidationError):
            _request(product={})

    def test_model_schema_carries_no_real_identifiers(self):
        schema = listing_audit_request_schema()
        self.assertIn("expected_prices", schema["required"])
        self.assertIn("expected_prices", schema["properties"])
        self.assertNotIn("S1", str(schema))
        self.assertNotIn("shop_id", str(schema))

    def test_normalized_request_freezes_this_round_targets(self):
        request = _request(expected_prices=[_price("19.90")])
        normalized = request.normalized()
        self.assertEqual(normalized["expected_prices"], [{
            "applies_to": "all_selected", "expected_amount": "19.90",
            "currency": "CNY", "price_basis": "list_price"}])
        self.assertEqual(normalized["price_basis"], "list_price")
        self.assertEqual(normalized["as_of"], "latest")
        validate_persisted_state({"node": "capture_user_expected_prices",
                                  "status": "running", "revision": 0,
                                  "normalized_request": normalized})

    def test_request_fingerprint_changes_with_the_target_price(self):
        """换价就是换问题：指纹不变的话旧结果会被当成这一轮的答案。"""
        base = {"product_ref": ref_for_key("product", "P1"), "scope_mode": "selected",
                "shop_refs": [_shop_ref("1")], "price_basis": "list_price",
                "as_of": "latest", "expected_prices": [_price("19.90")]}

        def fingerprint(request: dict) -> str:
            return request_fingerprint(
                subject_id="u1", allowed_shop_ids=frozenset({"S1"}),
                normalized_request=request,
                provenance=QueryProvenance(template_id="listing_price_audit",
                                           template_version="1"))

        self.assertNotEqual(fingerprint(base),
                            fingerprint({**base, "expected_prices": [_price("29.90")]}))
        self.assertNotEqual(fingerprint(base),
                            fingerprint({**base, "price_basis": "campaign_price"}))
        self.assertEqual(fingerprint(base), fingerprint(dict(base)))


# ---------------------------------------------------------------------------
# 4. 领域注册表与 price_audit 载荷契约
# ---------------------------------------------------------------------------


def _audit_payload_skeleton() -> dict:
    """一份最小合法载荷（合成引用）：判别分支的正反两面都要有用例。"""
    item = {"shop_ref": _shop_ref("1"), "listing_ref": "lst-0123456789ab",
            "expected_amount": "19.90", "actual_amount": "29.90",
            "amount_difference": "10", "audit_status": "mismatch",
            "price_basis": "list_price", "currency": "CNY", "snapshot_at": FRESH_ISO}
    return {
        "status": "partial",
        "audit": {
            "expected_items": 1, "evaluated_items": 1, "matched_items": 0,
            "all_correct": False, "counts": {"mismatch": 1},
            "sources": [{"shop_ref": _shop_ref("1"), "source_kind": "official_export",
                         "snapshot_at": FRESH_ISO, "enumeration_complete": True,
                         "fresh": True}],
            "freshness_policy_seconds": 86400,
            "rule_version": "listing-rules/2026-09-13.1"},
        "data": [dict(item)],
        # 不带 `mode`：运行契约里那个键属于推广工具的范围词（incremental/budget_cap）。
        "filters": {"price_basis": "list_price",
                    "as_of": "latest", "currency": "CNY", "expected_prices": [
                        {"applies_to": "all_selected", "expected_amount": "19.90",
                         "currency": "CNY", "price_basis": "list_price"}]},
        "limitations": [],
    }


class ListingDomainRegistryTests(unittest.TestCase):

    def test_listing_nodes_are_the_designed_chain_not_the_legacy_placeholder(self):
        """未实现时价审领域站在旧节点集上；本任务必须换成 spec §6 那一条链。"""
        spec = spec_for("listing_price_audit")
        self.assertEqual(
            set(spec.nodes),
            {"resolve_scope_product_and_skus", "load_expected_listing_roster",
             "capture_user_expected_prices", "check_listing_source",
             "load_listing_snapshot", "verify_completeness_and_freshness",
             "join_expected_and_actual", "compare_decimal_prices",
             "classify_discrepancies", "persist_audit", "finalize"})
        self.assertEqual(spec.nodes, LISTING_NODES)
        self.assertNotIn("assess_readiness", spec.nodes)
        self.assertNotIn("audit_prices", spec.nodes)
        self.assertTrue(allows_artifact_type("listing_price_audit", "price_audit"))
        self.assertFalse(allows_artifact_type("business_query", "price_audit"))

    def test_price_audit_uses_its_own_discriminated_schema(self):
        """price_audit 走自己的判别分支：metric_result 形状不能冒充复核结果。"""
        with self.assertRaises(ValueError):
            validate_artifact_payload({"status": "ok", "data": []}, AUDIT_ARTIFACT)
        import copy

        payload = _audit_payload_skeleton()
        before = copy.deepcopy(payload)
        # 比较对象是进入前的深拷：校验器必须只判不改，不能顺手修正生产方的输出。
        self.assertEqual(validate_artifact_payload(payload, AUDIT_ARTIFACT), before)

    def test_audit_rows_refuse_real_identifiers_and_untyped_values(self):
        payload = _audit_payload_skeleton()
        bad_rows = (
            {**payload["data"][0], "shop_id": "S1"},
            {**payload["data"][0], "listing_id": "L1"},
            {**payload["data"][0], "erp_sku_id": "SKU1"},
            {**payload["data"][0], "audit_status": "fine"},
            {**payload["data"][0], "expected_amount": 19.9},
            {**payload["data"][0], "expected_amount": "19,90"},
            {**payload["data"][0], "listing_ref": "L1"},
            {**payload["data"][0], "snapshot_at": "2026-09-13"},
            {**payload["data"][0], "currency": "USD"},
            # 金额相同却标 mismatch：标签与数字互相矛盾，模型拿哪一句去说都是一半对。
            {**payload["data"][0], "amount_difference": "0.00",
             "actual_amount": "19.90", "expected_amount": "19.90"},
            # 状态不是判定态却带差额：那会被当成"当前差异"转述出去。
            {**payload["data"][0], "audit_status": "stale"})
        for row in bad_rows:
            with self.subTest(row=row):
                with self.assertRaises(ValueError):
                    rows = [row] if row is not payload["data"][0] else [row, row]
                    validate_artifact_payload({**payload, "data": rows,
                                               "audit": {**payload["audit"],
                                                         "expected_items": len(rows)}},
                                              AUDIT_ARTIFACT)

    def test_audit_summary_denominator_and_count_rules(self):
        base = _audit_payload_skeleton()
        good = {"expected_items": 1, "evaluated_items": 1, "matched_items": 1,
                "all_correct": True, "counts": {"match": 1}}
        # 每一例只违反一条不变量：其余全部自洽，否则看不出是哪一条在把关。
        one_violation = (
            ("判定数不能超过期望数",
             {**good, "expected_items": 2, "counts": {"match": 1, "unknown": 1}}),
            ("全部正确必须有匹配证据",
             {**good, "matched_items": 0}),
            ("没有期望项就不能说全对",
             {**good, "expected_items": 0, "evaluated_items": 0, "matched_items": 0,
              "counts": {}}),
            ("计数词表外不接受任何名字", {**good, "counts": {"fine": 1}}),
            ("evaluated 必须等于 match + mismatch", {**good, "evaluated_items": 0}))
        for label, bad in one_violation:
            with self.subTest(rule=label):
                payload = {**base, "audit": {**base["audit"], **bad}}
                rows = payload["data"]
                payload["data"] = rows * int(bad.get("expected_items",
                                                     payload["audit"]["expected_items"]))
                with self.assertRaises(ValueError):
                    validate_artifact_payload(payload, AUDIT_ARTIFACT)
        # 反例：完全自洽的那一份必须能过，否则上面是在比垃圾互斥而不是在比规则。
        clean = {**base, "status": "ok", "audit": {**base["audit"], **good},
                 "data": [{**base["data"][0], "audit_status": "match",
                           "actual_amount": "19.90", "amount_difference": "0"}]}
        self.assertEqual(validate_artifact_payload(clean, AUDIT_ARTIFACT)["status"], "ok")

    def test_the_public_payload_states_which_shops_were_evaluated(self):
        """契约 v2 的第三面：不能只给"问了谁"与"排除谁"，让展示层去推中间那个差。"""
        payload = _audit_payload_skeleton()
        payload["evaluated_scope"] = {"shop_refs": [_shop_ref("1")]}
        checked = validate_artifact_payload(payload, AUDIT_ARTIFACT)
        self.assertEqual(checked["evaluated_scope"]["shop_refs"], [_shop_ref("1")])

    def test_free_texts_and_unknown_keys_are_refused(self):
        base = _audit_payload_skeleton()
        for payload in ({**base, "unexpected": 1},
                        {**base, "limitations": ["我自己编的一句说明"]},
                        {**base, "audit": {**base["audit"], "note": "自由文本"}}):
            with self.subTest(keys=sorted(payload)):
                with self.assertRaises(ValueError):
                    validate_artifact_payload(payload, AUDIT_ARTIFACT)


# ---------------------------------------------------------------------------
# 5. 图：真实库上的合成来源
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# 5. 节点链：不需要数据库的那一层执行
# ---------------------------------------------------------------------------


class ListingAuditNodeTests(unittest.TestCase):
    """直接驱动 spec §6 的判定节点链，不连数据库。

    为什么要有单独一个类（而不是只靠下面那个 DSN 类）：本模块宣传的两条红线——
    "来源未取证时只能报 unsupported"与"判定分母是期望 roster"——如果只在带 DSN 的
    集成跑里被执行，那么计划 Task 9 给出的验证命令（不含 DSN）就是一份绿着但什么都没
    证明的报告。节点级用例把这两条搬进默认跑。

    `load_listing_snapshot` 被跳过（它唯一的职责就是读库），快照由夹具直接注入。
    """

    def setUp(self) -> None:
        self.tag = uuid4().hex[:5].upper()
        self.product_id = f"{self.tag}{PRODUCT}"
        self.addCleanup(reset_listing_sources)

    def _verified(self) -> None:
        register_listing_source(ListingSourceRegistration(
            platform="tb", source_kind="official_export", evidence="probe-node",
            max_age_seconds=86400))

    def _header(self, key: str, *, captured_at: datetime | None = FRESH,
                enumeration: bool = True) -> SnapshotHeader:
        return SnapshotHeader(
            snapshot_id=f"snap-{self.tag}-{key}", namespace=DEFAULT_NAMESPACE,
            shop_id=f"S{self.tag}{key}", platform="tb", source="official_export",
            captured_at=captured_at, enumeration_complete=enumeration, batch_id=None)

    def _item(self, key: str, *, listing: str, platform_sku: str = "",
              erp_sku: str = "", erp_product: str = "", amount: str | None = "19.90",
              currency: str = "CNY", captured_at: datetime | None = None) -> SnapshotItem:
        return SnapshotItem(
            snapshot_id=f"snap-{self.tag}-{key}", shop_id=f"S{self.tag}{key}",
            listing_id=listing, platform_sku_id=platform_sku, erp_sku_id=erp_sku,
            erp_product_id=erp_product, list_amount=amount, campaign_amount=None,
            currency=currency, on_sale=True,
            captured_at=captured_at if captured_at is not None else FRESH)

    def _roster(self, key: str, *, listing: str = "", platform_sku: str = "",
                erp_sku: str = "") -> RosterItem:
        shop_id = f"S{self.tag}{key}"
        return RosterItem(shop_id=shop_id, shop_ref=_shop_ref(self.tag + key),
                          namespace=DEFAULT_NAMESPACE, listing_id=listing,
                          platform_sku_id=platform_sku, erp_sku_id=erp_sku)

    @staticmethod
    def _namespace_of(bundles: dict) -> list:
        """把夹具里 `shop_id -> bundle` 的写法转成图上用的 `(shop_id, namespace)` 键。"""
        return [((key[0], key[1]) if isinstance(key, tuple)
                 else (key, DEFAULT_NAMESPACE), bundle)
                for key, bundle in bundles.items()]

    def _runtime(self, *, roster, bundles=None, price="19.90",
                 price_basis="list_price"):
        shop_ids = [item.shop_id for item in roster]
        profiles = {shop: ShopProfile(shop_id=shop, enabled=True, currency="CNY",
                                     platform="tb", capabilities=frozenset())
                    for shop in shop_ids}
        context = DomainContext(
            subject_id=f"t9-node-{self.tag}", allowed_shop_ids=frozenset(shop_ids),
            shop_refs={item.shop_id: item.shop_ref for item in roster},
            conn=None, store=None, chat_id=UUID(int=1), user_message_id=UUID(int=2),
            root_request_id=UUID(int=3), now=NOW,
            deadline=time.monotonic() + 30, attempt_no=1)
        # 夹具按 shop_id 写快照；图上按 (店铺, 账号范围) 编址（同一个店在两个账号范围
        # 各有一批快照时不得互相遮蔽），这里就按 roster 用的那个范围补上。
        namespaced = dict(self._namespace_of(bundles or {}))
        runtime = ListingRuntime(
            state=ListingState(run_id=uuid4(), normalized_request={}), context=context,
            tool_call_id="node",
            request=_request(price_basis=price_basis,
                             expected_prices=[_price(price, price_basis=price_basis)]),
            profiles=profiles, candidate_shop_ids=tuple(shop_ids),
            roster=tuple(roster), expectations={item.key: price for item in roster},
            bundles=namespaced, erp_product_id=self.product_id)
        check_listing_source(runtime)
        verify_completeness_and_freshness(runtime)
        join_expected_and_actual(runtime)
        compare_decimal_prices(runtime)
        classify_discrepancies(runtime)
        return runtime

    # -- 红线一：来源门禁 --------------------------------------------------

    def test_unverified_source_gates_every_cell_without_a_database(self):
        """默认注册表为空 → 有映射、有新鲜完整快照也一格都不判。"""
        item = self._roster("1", listing="L1", erp_sku=f"{self.tag}SKU1")
        bundle = SnapshotBundle(
            header=self._header("1"),
            items=(self._item("1", listing="L1", erp_sku=f"{self.tag}SKU1",),))
        runtime = self._runtime(roster=[item], bundles={item.shop_id: bundle})
        self.assertEqual(runtime.summary["counts"], {"unsupported": 1})
        self.assertEqual(runtime.summary["evaluated_items"], 0)
        self.assertFalse(runtime.summary["all_correct"])
        row = runtime.items[0]
        self.assertIsNone(row.actual_price,
                          "来源未成立时采集到的标价也不发：不给「看着能用」留余地")
        self.assertIsNone(row.snapshot_at,
                          "时效那一关根本不该被说：来源那一关就没通过")
        self.assertEqual(str(runtime.state.target_status), "missing_data")
        self.assertIn(TEXT_SOURCE_UNVERIFIED, runtime.limitations)

    def test_verified_source_is_what_turns_the_judgement_on(self):
        """同一份输入，登记来源之后必须真能判：否则门禁只是一个永不为真的常量。"""
        item = self._roster("1", listing="L1", erp_sku=f"{self.tag}SKU1")
        bundle = SnapshotBundle(
            header=self._header("1"),
            items=(self._item("1", listing="L1", erp_sku=f"{self.tag}SKU1",
                              amount="29.90"),))
        before = self._runtime(roster=[item], bundles={item.shop_id: bundle})
        self.assertEqual(before.items[0].status, "unsupported")
        self._verified()
        after = self._runtime(roster=[item], bundles={item.shop_id: bundle})
        self.assertEqual(after.items[0].status, "mismatch")
        self.assertEqual(after.items[0].difference, "10")
        self.assertEqual(after.summary["counts"], {"mismatch": 1})

    # -- 红线二：分母与判定顺序 ------------------------------------------

    def test_every_cell_keeps_a_status_so_the_denominator_cannot_shrink(self):
        """三家店只读到两份快照：第三家必须还在，并且带着 unknown。"""
        self._verified()
        items = [self._roster(key, listing=f"L{key}", erp_sku=f"{self.tag}SKU1")
                 for key in ("1", "2", "3")]
        bundles = {
            items[0].shop_id: SnapshotBundle(
                header=self._header("1"),
                items=(self._item("1", listing="L1", erp_sku=f"{self.tag}SKU1"),)),
            items[1].shop_id: SnapshotBundle(
                header=self._header("2"),
                items=(self._item("2", listing="L2", erp_sku=f"{self.tag}SKU1",
                                  amount="29.90"),))}
        runtime = self._runtime(roster=items, bundles=bundles)
        self.assertEqual(runtime.summary["expected_items"], 3)
        self.assertEqual(runtime.summary["evaluated_items"], 2)
        self.assertEqual(runtime.summary["counts"],
                         {"match": 1, "mismatch": 1, "unknown": 1})
        self.assertFalse(runtime.summary["all_correct"])

    def test_all_correct_needs_every_cell_fresh_complete_and_matching(self):
        self._verified()
        item = self._roster("1", listing="L1", erp_sku=f"{self.tag}SKU1")
        bundle = SnapshotBundle(header=self._header("1"), items=(
            self._item("1", listing="L1", erp_sku=f"{self.tag}SKU1"),))
        clean = self._runtime(roster=[item], bundles={item.shop_id: bundle})
        self.assertTrue(clean.summary["all_correct"])
        self.assertEqual(str(clean.state.target_status), "success")
        # 同一格换个目标价："全部正确"的声称立刻不成立
        dirty = self._runtime(roster=[item], bundles={item.shop_id: bundle},
                              price="20.00")
        self.assertFalse(dirty.summary["all_correct"])
        self.assertEqual(str(dirty.state.target_status), "success",
                         "复核做完了，只是发现了差异；partial 只说有格没评到")
        # 有一格没读到快照时，partial 才回来
        gap = self._runtime(roster=[item, self._roster("2", listing="L2",
                                                       erp_sku=f"{self.tag}SKU1")],
                            bundles={item.shop_id: bundle})
        self.assertEqual(str(gap.state.target_status), "partial")
        self.assertIn(TEXT_INCOMPLETE, gap.limitations)

    def test_stale_snapshot_cannot_produce_a_match_or_a_difference(self):
        self._verified()
        item = self._roster("1", listing="L1", erp_sku=f"{self.tag}SKU1")
        bundle = SnapshotBundle(
            header=self._header("1", captured_at=STALE),
            items=(self._item("1", listing="L1", erp_sku=f"{self.tag}SKU1",
                              captured_at=STALE),))
        runtime = self._runtime(roster=[item], bundles={item.shop_id: bundle})
        self.assertEqual(runtime.items[0].status, "stale",
                         "价格相同也不判 match：过期快照证明不了当下是对的")
        self.assertIsNone(runtime.items[0].difference)

    def test_naive_captured_at_is_treated_as_not_fresh(self):
        """无时区的时间会被当成 UTC 或本地时间：两种猜法都能把过期说成新鲜。"""
        self._verified()
        item = self._roster("1", listing="L1", erp_sku=f"{self.tag}SKU1")
        bundle = SnapshotBundle(
            header=self._header("1", captured_at=NOW.replace(tzinfo=None)),
            items=(self._item("1", listing="L1", erp_sku=f"{self.tag}SKU1"),))
        runtime = self._runtime(roster=[item], bundles={item.shop_id: bundle})
        self.assertEqual(runtime.items[0].status, "stale")

    def test_currency_mismatch_is_not_compared_at_the_node_level(self):
        self._verified()
        item = self._roster("1", listing="L1", erp_sku=f"{self.tag}SKU1")
        bundle = SnapshotBundle(
            header=self._header("1"),
            items=(self._item("1", listing="L1", erp_sku=f"{self.tag}SKU1",
                              currency="USD"),))
        runtime = self._runtime(roster=[item], bundles={item.shop_id: bundle})
        self.assertEqual(runtime.items[0].status, "unknown")
        self.assertIsNone(runtime.items[0].actual_price,
                          "没有换算依据的金额比没有金额更危险")

    def test_not_listed_needs_enumeration_evidence_at_the_node_level(self):
        self._verified()
        mapped = self._roster("1", listing="L1", platform_sku="PS1",
                              erp_sku=f"{self.tag}SKU1")
        unmapped = self._roster("2")
        runtime = self._runtime(roster=[mapped, unmapped], bundles={
            mapped.shop_id: SnapshotBundle(header=self._header("1"), items=()),
            unmapped.shop_id: SnapshotBundle(header=self._header("2", enumeration=False),
                                             items=())})
        self.assertEqual([row.status for row in runtime.items],
                         ["not_listed", "unmapped"],
                         "有落点 + 全量枚举 → 未上架；连落点都没有 → 未映射")
        self.assertFalse(runtime.summary["all_correct"])

    def test_missing_target_shows_the_captured_price_but_blocks_the_pass(self):
        """只给一个规格的目标价：另一格是 missing_standard，实价照发（spec §6）。"""
        self._verified()
        items = [self._roster("1", listing="L1", erp_sku=f"{self.tag}SKU1"),
                 self._roster("1", listing="L1", platform_sku="PS2",
                              erp_sku=f"{self.tag}SKU2")]
        bundle = SnapshotBundle(header=self._header("1"), items=(
            self._item("1", listing="L1", erp_sku=f"{self.tag}SKU1", amount="19.90"),
            self._item("1", listing="L1", platform_sku="PS2",
                       erp_sku=f"{self.tag}SKU2", amount="39.90")))
        runtime = self._runtime(roster=items, bundles={items[0].shop_id: bundle})
        runtime.expectations = {items[0].key: "19.90"}
        join_expected_and_actual(runtime)
        compare_decimal_prices(runtime)
        classify_discrepancies(runtime)
        self.assertEqual([row.status for row in runtime.items],
                         ["match", "missing_standard"])
        self.assertEqual(runtime.items[1].actual_price, "39.90")
        self.assertIsNone(runtime.items[1].expected_price)
        self.assertFalse(runtime.summary["all_correct"])
        self.assertEqual(runtime.summary["evaluated_items"], 1)

    # -- 匹配轮次与"一格一条记录" ----------------------------------------

    def test_listing_round_matches_when_the_source_omits_the_platform_sku(self):
        """来源只报链接号不拆平台 SKU：第 3 轮（listing）必须能对上。"""
        self._verified()
        item = self._roster("1", listing="L1", platform_sku="PS-IGNORED",
                            erp_sku=f"{self.tag}SKU1")
        bundle = SnapshotBundle(header=self._header("1"), items=(
            self._item("1", listing="L1", amount="29.90"),))
        runtime = self._runtime(roster=[item], bundles={item.shop_id: bundle})
        self.assertEqual(runtime.items[0].status, "mismatch")
        self.assertEqual(runtime.items[0].actual_price, "29.90")

    def test_product_round_matches_a_product_level_cell(self):
        """没有任何 SKU 身份的格（新链接没映射）对来源声明的 ERP 商品号。"""
        self._verified()
        item = self._roster("1")                      # 无落点、无 SKU
        bundle = SnapshotBundle(header=self._header("1"), items=(
            self._item("1", listing="LX", erp_product=self.product_id, amount="9.90"),))
        runtime = self._runtime(roster=[item], bundles={item.shop_id: bundle})
        self.assertEqual(runtime.items[0].status, "mismatch")
        self.assertEqual(runtime.items[0].actual_price, "9.90")

    def test_one_snapshot_record_cannot_answer_two_cells(self):
        """两条链接只有一条记录：第二格不能复用它的价格，必须回到未判定。

        这就是 `_MATCH_ROUNDS` 那条 `claimed` 守卫存在的全部理由。拿掉它，两条链接会
        共享同一个标价并发出两行"一致「——那正是 spec §5.4 禁掉的」只看一条链接"。
        """
        self._verified()
        first = self._roster("1", listing="LA", platform_sku="PS-A",
                             erp_sku=f"{self.tag}SKU1")
        second = self._roster("1", listing="LB", platform_sku="PS-B",
                              erp_sku=f"{self.tag}SKU1")
        bundle = SnapshotBundle(header=self._header("1"), items=(
            self._item("1", listing="LA", platform_sku="PS-A",
                       erp_sku=f"{self.tag}SKU1", amount="19.90"),))
        runtime = self._runtime(roster=[first, second], bundles={first.shop_id: bundle})
        self.assertEqual([row.status for row in runtime.items], ["match", "not_listed"])
        self.assertIsNone(runtime.items[1].actual_price, "第二格不得复用第一格的金额")
        self.assertEqual(runtime.summary["evaluated_items"], 1)

    def test_match_round_order_prefers_the_explicit_landing(self):
        """轮次顺序就是匹配精度：两边都写清链接号时，不得被「同一 SKU」的宽匹配抢走。"""
        self._verified()
        item = self._roster("1", listing="LB", platform_sku="PS-B",
                            erp_sku=f"{self.tag}SKU1")
        bundle = SnapshotBundle(header=self._header("1"), items=(
            self._item("1", listing="LA", platform_sku="PS-A",
                       erp_sku=f"{self.tag}SKU1", amount="19.90"),
            self._item("1", listing="LB", platform_sku="PS-B",
                       erp_sku=f"{self.tag}SKU1", amount="29.90")))
        runtime = self._runtime(roster=[item], bundles={item.shop_id: bundle})
        self.assertEqual(runtime.items[0].actual_price, "29.90",
                         "取错链接就是把 A 链接的价说成 B 链接的价")

    # -- 出表前的不变量与共同截止 ----------------------------------------

    def test_guard_rejects_a_judgement_without_both_sides(self):
        """`_guard_invariants` 拦的是"标签说有结论、数字缺一半"。"""
        item = self._roster("1", listing="L1", erp_sku=f"{self.tag}SKU1")
        with self.assertRaises(ValueError):
            _guard_invariants(
                object(), [AuditItem(roster=item, status="match",
                                     expected_price="19.90", actual_price=None)])

    def test_guard_rejects_a_difference_on_an_undecided_cell(self):
        item = self._roster("1", listing="L1", erp_sku=f"{self.tag}SKU1")
        with self.assertRaises(ValueError):
            _guard_invariants(
                object(), [AuditItem(roster=item, status="stale", expected_price="19.90",
                                     actual_price="29.90", difference="10")])

    def test_common_data_as_of_is_the_earliest_snapshot_read(self):
        """一次复核只说一个"什么时候的价格"：取最早那一批，不取最新那一批。"""
        self._verified()
        items = [self._roster("1", listing="L1", erp_sku=f"{self.tag}SKU1"),
                 self._roster("2", listing="L2", erp_sku=f"{self.tag}SKU1")]
        bundles = {
            items[0].shop_id: SnapshotBundle(
                header=self._header("1", captured_at=FRESH),
                items=(self._item("1", listing="L1", erp_sku=f"{self.tag}SKU1"),)),
            items[1].shop_id: SnapshotBundle(
                header=self._header("2", captured_at=OLDER),
                items=(self._item("2", listing="L2", erp_sku=f"{self.tag}SKU1",
                                  captured_at=OLDER),))}
        runtime = self._runtime(roster=items, bundles=bundles)
        self.assertEqual(runtime.data_as_of, OLDER)
        self.assertEqual(runtime.report.data_as_of, OLDER)

    def test_another_accounts_enumeration_cannot_declare_this_one_not_listed(self):
        """同一个店在两个账号范围各有一批快照时，A 的全量枚举不得替 B 说"未上架"。

        快照按 (店铺, 账号范围) 编址就是为了让这一格只能由自己那一批证据决定：
        另一个账号写了"本店全集已枚举"，本账号只有一份过期/未声明完整的快照时，
        这一家对不上任何记录的那格必须是 unknown，不是 not_listed。
        """
        self._verified()
        item = self._roster("1", listing="L1", platform_sku="PS1",
                            erp_sku=f"{self.tag}SKU1")
        mine = SnapshotBundle(header=self._header("1", captured_at=STALE,
                                                  enumeration=False),
                              items=())
        other = SnapshotBundle(header=self._header("1", enumeration=True), items=())
        single = self._runtime(roster=[item], bundles={(item.shop_id, item.namespace):
                                                       mine})
        self.assertEqual(single.items[0].status, "stale",
                         "本范围自己的快照说了算：过期就先判过期")
        # 本范围完全没读到，另一个范围读到了完整枚举：不能借它的凭据
        borrowed = self._runtime(
            roster=[RosterItem(shop_id=item.shop_id, shop_ref=item.shop_ref,
                               namespace="acct-missing", listing_id="L1",
                               platform_sku_id="PS1", erp_sku_id=item.erp_sku_id)],
            bundles={(item.shop_id, item.namespace): mine,
                     (item.shop_id, "acct-other"): other})
        self.assertEqual(borrowed.items[0].status, "unknown",
                         "缺本范围的快照就是缺，不能拿别的账号的枚举当凭据")

    def test_node_chain_and_domain_registry_cannot_drift(self):
        """节点枚举与领域白名单跳模块比对：这才是「登记即边界」的凭据。

        拿 `LISTING_NODES` 跟它自己比是恒等式，看不出两份清单有没有漂移。
        """
        self.assertEqual(frozenset(node.value for node in ListingNode), LISTING_NODES)
        self.assertEqual(frozenset(node.value for node in ListingNode),
                         spec_for("listing_price_audit").nodes)

    def test_termination_reason_distinguishes_findings_from_missing_evidence(self):
        """业务发现不是故障原因：没做完要说清"缺哪一件证据"。

        三个终止原因在本域都有真实去处，而它们对应的下一步完全不同：全部判完只是
        有差异（succeeded）、没读到快照（coverage_incomplete）、来源未取证
        （capability_unavailable）。
        """
        self._verified()
        item = self._roster("1", listing="L1", erp_sku=f"{self.tag}SKU1")
        bundle = SnapshotBundle(header=self._header("1"), items=(
            self._item("1", listing="L1", erp_sku=f"{self.tag}SKU1", amount="29.90"),))
        judged = self._runtime(roster=[item], bundles={item.shop_id: bundle})
        self.assertEqual(judged.report.termination_reason, "succeeded",
                         "每一格都判完了：差异是发现，不是没做完")
        no_snapshot = self._runtime(roster=[item], bundles={})
        self.assertEqual(no_snapshot.report.termination_reason, "coverage_incomplete",
                         "来源成了但读不到快照：要去补抓取，不是去补授权")
        reset_listing_sources()
        no_source = self._runtime(roster=[item], bundles={item.shop_id: bundle})
        self.assertEqual(no_source.report.termination_reason, "capability_unavailable",
                         "一格都没判成，原因必须落在来源上")
        self.assertIn(TEXT_SOURCE_UNVERIFIED, no_source.limitations)


@unittest.skipUnless(os.getenv("BI_TEST_ADMIN_DSN"), "未配置独立测试数据库")
class ListingAuditGraphTests(unittest.TestCase):

    def setUp(self) -> None:
        self.conn = connect_test_db(self)
        self.tag = uuid4().hex[:5].upper()
        self.shops: dict[str, str] = {}
        self.product_id = f"{self.tag}{PRODUCT}"
        self.addCleanup(reset_listing_sources)

    # -- 夹具 --------------------------------------------------------------

    def _shop(self, key: str, platform: str = "tb") -> str:
        shop_id = f"S{self.tag}{key}"
        self.conn.execute(
            "INSERT INTO bi.shops(shop_id, platform, display_name, capabilities) "
            "VALUES (%s, %s, %s, %s) ON CONFLICT (shop_id) DO UPDATE SET "
            "platform = EXCLUDED.platform, display_name = EXCLUDED.display_name",
            (shop_id, platform, f"价审测试店{key}", ["quantity", "paid_amount"]))
        self.shops[key] = shop_id
        return shop_id

    def _sku_id(self, sku: str) -> str:
        return f"{self.tag}{sku}"

    def _trade(self, key: str, *, sku: str = "SKU1", product_id: str | None = None,
               erp_id: str | None = None, name: str = "直钉枪") -> None:
        """一行成交：让商品能在授权范围内解析出来（解析全集仍受 Task 1/6 限制）。"""
        shop_id = self.shops[key]
        paid_at = datetime(2026, 9, 10, 12, tzinfo=BEIJING)
        order_id = erp_id or f"{self.tag}{key}E1"
        self.conn.execute(
            "INSERT INTO bi.orders(shop_id, erp_id, commercial_ids, source, "
            "source_updated_at, paid_at, active, batch_id) "
            "VALUES (%s, %s, %s, 'erp.trade.list.query', now(), %s, true, 't9-seed') "
            "ON CONFLICT (shop_id, erp_id) DO NOTHING",
            (shop_id, order_id, [f"C{order_id}"], paid_at))
        self.conn.execute(
            "INSERT INTO bi.order_items(shop_id, erp_id, line_id, commercial_id, "
            "product_id, sku_id, paid_at, quantity, allocated_paid_amount, "
            "allocation_verified, line_kind, active, product_name_snapshot, "
            "sku_label_snapshot) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, '1', '19.90', true, 'sale', true, %s, %s) "
            "ON CONFLICT (shop_id, erp_id, line_id) DO NOTHING",
            (shop_id, order_id, f"{order_id}-L1", f"C{order_id}",
             product_id or self.product_id, self._sku_id(sku), paid_at, name, "6mm"))

    def _map(self, key: str, *, listing: str, sku: str = "SKU1",
             platform_sku: str | None = None) -> None:
        """一条已确认标识映射：(店, 链接, 平台 SKU) → ERP 商品 / SKU。"""
        record_identifier_mapping(self.conn, IdentifierMapping(
            namespace=DEFAULT_NAMESPACE, platform="tb", shop_id=self.shops[key],
            listing_id=listing, platform_sku_id=platform_sku or f"PS-{listing}-{sku}",
            erp_product_id=self.product_id, erp_sku_id=self._sku_id(sku),
            evidence=f"probe-t9-{self.tag}-{key}-{listing}", source="manual_map"),
            at=date(2026, 9, 1))

    def _verify_source(self, platform: str = "tb") -> None:
        register_listing_source(ListingSourceRegistration(
            platform=platform, source_kind="official_export",
            evidence=f"probe-t9-{self.tag}", max_age_seconds=86400))

    def _snapshot(self, key: str, *, captured_at: datetime | None = FRESH,
                  enumeration_complete: bool = True,
                  enumeration_evidence: str | None = None,
                  source: str = "official_export",
                  items: tuple[dict, ...] = (), platform: str = "tb",
                  snapshot_id: str | None = None, evidence: str | None = None) -> str:
        sid = snapshot_id or f"snap-{self.tag}-{key}-{uuid4().hex[:6]}"
        # `evidence=None` 才是"用默认凭据"；显式传空串必须原样送进去，否则夹具会把
        # 它要检验的那条契约替调用方补上，测试就成了自证。
        declared = (f"probe-t9-{self.tag}-{key}" if evidence is None else evidence)
        repository.insert_listing_snapshot(
            self.conn, snapshot_id=sid, shop_id=self.shops[key], platform=platform,
            namespace=DEFAULT_NAMESPACE, source=source, evidence=declared,
            captured_at=captured_at, enumeration_complete=enumeration_complete,
            # 018 的成对 CHECK：声明完整枚举就必须带凭据，反之就必须不带。
            # `None` 才是"用默认凭据"；显式传空串必须原样送进去，否则夹具会把它
            # 要检验的那条契约替调用方补上。
            enumeration_evidence=((enumeration_evidence
                                   if enumeration_evidence is not None else declared)
                                  if enumeration_complete else None),
            batch_id=f"batch-{sid}")
        for index, item in enumerate(items):
            repository.insert_listing_snapshot_item(
                self.conn, snapshot_id=sid, shop_id=self.shops[key],
                namespace=DEFAULT_NAMESPACE,
                listing_id=item.get("listing_id", f"L{index}"),
                platform_sku_id=item.get("platform_sku_id", ""),
                erp_sku_id=item.get("erp_sku_id", ""),
                erp_product_id=item.get("erp_product_id"),
                list_amount=item.get("list_amount"),
                campaign_amount=item.get("campaign_amount"),
                currency=item.get("currency", "CNY"),
                on_sale=item.get("on_sale", True),
                captured_at=item.get("captured_at", captured_at))
        return sid

    def _allow(self, *keys: str) -> frozenset[str]:
        return frozenset(self.shops[key] for key in keys)

    def _seed_run_context(self, subject: str | None = None):
        """应库 Store 需要真实的聊天与用户消息行（run_id 有外键）。"""
        chat_id, message_id = uuid4(), uuid4()
        subject = subject or f"t9-{self.tag}"
        self.conn.execute(
            "INSERT INTO bi.app_chats(id, subject_id, title) VALUES (%s, %s, '价审')",
            (chat_id, subject))
        self.conn.execute(
            "INSERT INTO bi.app_messages(id, chat_id, role, content, status) "
            "VALUES (%s, %s, 'user', '标价是否正确', 'complete')",
            (message_id, chat_id))
        return chat_id, message_id, subject

    def _db_store(self, conn: object = None) -> MemoryQueryRunStore:
        """一个真的 PostgresQueryRunStore：运行行、血缘与本轮依据都落库。

        专项用例默认用内存 Store（快，也不依赖聊天行）；只有要断言「依据真的被
        冻结进了库」时才换成本个。两种 Store 跑同一份形式校验，所以内存版能过的
        形状在数据库那边不会撞 CHECK。

        `conn` 要和图的 `context.conn` 用同一个包装：Store 与图各自拿一条连接时，
        记录型连接就看不见 Store 那一路的 SQL，"图里没有回读"这条断言就会变成
        一个只证明了"我没看那条连接"的空断言。
        """
        from bi_agent.runtime.repository import PostgresQueryRunStore

        chat_id, message_id, _subject = self._seed_run_context()
        self._chat_id, self._message_id = chat_id, message_id
        return PostgresQueryRunStore(self.conn if conn is None else conn,
                                     forbidden_values=set(self.shops.values()))

    def _context(self, *, allowed: frozenset[str] | None = None,
                 store: MemoryQueryRunStore | None = None,
                 conn: object = None) -> DomainContext:
        allowed = self._allow(*self.shops) if allowed is None else allowed
        refs = {shop: ref_for_key("shop", shop) for shop in allowed}
        chat_id = getattr(self, "_chat_id", None) or UUID(int=1)
        message_id = getattr(self, "_message_id", None) or UUID(int=2)
        return DomainContext(
            subject_id=f"t9-{self.tag}", allowed_shop_ids=allowed, shop_refs=refs,
            conn=self.conn if conn is None else conn,
            # 禁字集合用"本轮合成店的全部真实店号"，不管它们在不在 allowed 里：
            # 任何一条被持久化的状态都不该带着它们。不要用占位串兜底——"none"
            # 会和 RecoveryAction.NONE 的取值撞车，把一次正常的 forbidden 终止报成
            # 载荷不安全。
            store=store or MemoryQueryRunStore(
                forbidden_values=set(self.shops.values()) or {"unused"}),
            chat_id=chat_id, user_message_id=message_id,
            root_request_id=UUID(int=3), now=NOW,
            deadline=time.monotonic() + 30, attempt_no=1)

    def _run(self, *, store: MemoryQueryRunStore | None = None,
             allowed: frozenset[str] | None = None, conn: object = None,
             **request_overrides: object):
        return audit_listing_prices(_request(**request_overrides),
                                    self._context(store=store, allowed=allowed,
                                                  conn=conn))

    def _two_shop_setup(self) -> None:
        """两家店各一条链接，都登记来源、都有新鲜完整快照，目标价一致。"""
        self._shop("1")
        self._shop("2")
        self._trade("1")
        # 两家店都留一行成交：商品解析的候选全集仍受"本轮授权范围内有成交/档案行"限制
        # （Task 1/6 的已知边界），只在一家里有行的话，缩小授权到另一家就会解析不出。
        self._trade("2")
        self._map("1", listing="L1", sku="SKU1")
        self._map("2", listing="L2", sku="SKU1")
        self._verify_source()
        self._snapshot("1", items=[{"listing_id": "L1", "erp_sku_id": self._sku_id("SKU1"),
                                    "list_amount": "19.90"}])
        self._snapshot("2", items=[{"listing_id": "L2", "erp_sku_id": self._sku_id("SKU1"),
                                    "list_amount": "19.90"}])

    # -- 5.1 来源门禁 ------------------------------------------------------

    def test_missing_verified_source_reports_unsupported_for_every_item(self):
        """有映射、有快照，但平台没有已核验来源 → 全部 unsupported，0 项判定。"""
        self._shop("1")
        self._trade("1")
        self._map("1", listing="L1", sku="SKU1")
        self._snapshot("1", items=[{"listing_id": "L1", "erp_sku_id": self._sku_id("SKU1"),
                                    "list_amount": "29.90"}])
        result = self._run()
        payload = _payload_for(result)
        self.assertEqual(result.status.value, "missing_data",
                         "来源未取证不是「做完了且全对」，也不是系统故障")
        rows = _rows(payload)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["audit_status"], "unsupported")
        self.assertIsNone(rows[0]["actual_amount"],
                          "来源未成立时连采集到的价格都不发：不给「看着能用」留余地")
        self.assertEqual(payload["audit"]["expected_items"], 1)
        self.assertEqual(payload["audit"]["evaluated_items"], 0)
        self.assertFalse(payload["audit"]["all_correct"])
        self.assertIn("渠道在售价来源", _limitation_text(payload))
        self.assertEqual(result.model_payload["status"], "missing_data")

    def test_mismatch_is_reported_once_the_source_is_verified(self):
        """同一份数据的另一种世界：来源取证之后必须真能出判定，否则门禁是假开关。"""
        self._shop("1")
        self._trade("1")
        self._map("1", listing="L1", sku="SKU1")
        self._snapshot("1", items=[{"listing_id": "L1", "erp_sku_id": self._sku_id("SKU1"),
                                    "list_amount": "29.90"}])
        self._verify_source()
        result = self._run()
        payload = _payload_for(result)
        self.assertEqual(result.status.value, "success",
                         "每一格都有确定结论：复核做完了，只是结果是对不上")
        self.assertFalse(payload["audit"]["all_correct"],
                         "做完 ≠ 全部正确：两个声称必须分开")
        self.assertTrue(any("不一致" in text for text in payload["limitations"]),
                        payload["limitations"])
        row = _row_for(payload, shop_ref=_shop_ref(self.tag + "1"))
        self.assertEqual(row["audit_status"], "mismatch")
        self.assertEqual(row["expected_amount"], "19.90")
        self.assertEqual(row["actual_amount"], "29.90")
        self.assertEqual(row["amount_difference"], "10")
        self.assertEqual(payload["audit"]["counts"], {"mismatch": 1})

    # -- 5.2 本轮目标价 ----------------------------------------------------

    def test_no_target_price_this_round_needs_input_and_never_inherits(self):
        """上一轮给过 19.90，本轮不给：必须 needs_input，也不许回读上一轮的冻结表。"""
        self._two_shop_setup()
        first = self._run()
        self.assertEqual(first.status.value, "success", _limitation_text(
            _payload_for(first)))
        recorder = _RecordingConn(self.conn)
        second = run_listing_audit_graph(
            request=None, context=self._context(conn=recorder),
            tool_call_id="call-2", arguments={"product": {"text": "直钉枪"}},
            arguments_error="expected_prices is required")
        self.assertEqual(second.domain_result.status.value, "needs_input")
        self.assertEqual(second.domain_result.artifacts, [],
                         "澄清路径不发结果卡片")
        self.assertEqual(recorder.matching("SELECT", "price_audit_expectations"), [],
                         "本轮没有目标价时，任何回读旧标准的 SQL 都是隐式继承")
        self.assertEqual(recorder.matching("INSERT", "price_audit_expectations"), [],
                         "澄清路径不落任何标准")
        self.assertEqual(recorder.matching("UPDATE", "bi.shops"), [])

    def test_expectations_are_frozen_once_per_run_and_never_read_back(self):
        self._two_shop_setup()
        recorder = _RecordingConn(self.conn)
        store = self._db_store(recorder)
        result = self._run(conn=recorder, store=store)
        self.assertEqual(result.status.value, "success", str(result.model_payload))
        self.assertEqual(len(recorder.matching("INSERT INTO bi.price_audit_expectations")),
                         2, "两家店各冻结一行本轮目标价")
        self.assertEqual(recorder.matching("SELECT", "price_audit_expectations"), [],
                         "复核结论只能来自本轮输入：读回历史标准就是隐式继承")
        self.assertEqual(recorder.matching("purchase_price"), [],
                         "档案建议价 / 采购价不是上架标准")
        rows = self.conn.execute(
            "SELECT captured_from, count(*) FROM bi.price_audit_expectations "
            "WHERE run_id = %s GROUP BY captured_from", (result.run_id,)).fetchall()
        self.assertEqual([tuple(row) for row in rows], [("current_user_input", 2)])
        roster = self.conn.execute(
            "SELECT count(*) FROM bi.expected_listing_rosters WHERE run_id = %s",
            (result.run_id,)).fetchone()
        self.assertEqual(roster[0], 2)
        # 冻结行带的是本轮引用：一个链接句柄、一个 SKU 引用，不带渠道主键；而且这张表
        # 根本不放金额（金额只能在 `price_audit_expectations` 带出处地写）。
        shape = self.conn.execute(
            "SELECT listing_ref <> '', sku_ref FROM bi.expected_listing_rosters "
            "WHERE run_id = %s ORDER BY shop_id", (result.run_id,)).fetchall()
        self.assertEqual([bool(row[0]) for row in shape], [True, True])
        self.assertTrue(all(row[1].startswith("ent-") for row in shape), shape)
        columns = self.conn.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = 'bi' AND table_name = 'expected_listing_rosters'",
            ).fetchall()
        self.assertNotIn("expected_amount", [str(row[0]) for row in columns],
                         "roster 里留一个可写的金额位就是一张没有来源、没有凭据的第二价格表")
        self.assertEqual(recorder.matching("SELECT", "expected_listing_rosters"), [],
                         "roster 表也是历史记录：读它就等于沿用上一轮的目标范围")
        fingerprint = self.conn.execute(
            "SELECT request_fingerprint FROM bi.price_audit_expectations "
            "WHERE run_id = %s LIMIT 1", (result.run_id,)).fetchone()[0]
        self.assertRegex(str(fingerprint), r"^[0-9a-f]{64}$",
                         "冻进去的依据要能追到哪一个请求：没指纹的行无法归因")

    def test_frozen_rows_record_which_kind_of_rule_answered_each_cell(self):
        """`applies_to` 是"这一格的价是按哪一档给的"唯一凭据。

        事后复盘要能分清"用户逐规格给了价"与"用户说了一句统一价"：两者对多 SKU 商品
        的含义完全不同。不读这一列的话，整条规则链就只是图上的一句话。
        """
        self._shop("1")
        self._shop("2")
        self._trade("1")
        self._trade("2")
        self._map("1", listing="L1", sku="SKU1")
        self._map("2", listing="L2", sku="SKU1")
        self._verify_source()
        self._snapshot("1", items=[{"listing_id": "L1",
                                    "platform_sku_id": "PS-L1-SKU1",
                                    "erp_sku_id": self._sku_id("SKU1"),
                                    "list_amount": "19.90"}])
        self._snapshot("2", items=[{"listing_id": "L2",
                                    "platform_sku_id": "PS-L2-SKU1",
                                    "erp_sku_id": self._sku_id("SKU1"),
                                    "list_amount": "29.90"}])
        shop1 = _shop_ref(self.tag + "1")
        sku1 = _sku_ref(self._sku_id("SKU1"))
        recorder = _RecordingConn(self.conn)
        result = self._run(
            conn=recorder, store=self._db_store(recorder),
            expected_prices=[_price("19.90", applies_to="shop_sku", shop_ref=shop1,
                                    sku_ref=sku1)])
        self.assertEqual(result.status.value, "partial", str(result.model_payload))
        rows = self.conn.execute(
            "SELECT shop_id, applies_to, expected_amount FROM bi.price_audit_expectations "
            "WHERE run_id = %s ORDER BY shop_id", (result.run_id,)).fetchall()
        self.assertEqual([tuple(row) for row in rows],
                         [(self.shops["1"], "shop_sku", Decimal("19.9000"))],
                         "shop_sku 那一档只能冻结到它点名的那一家店")
        # 第 2 家没被任何规则命中：不落标准行（空不等于一个可回填的默认价）。
        self.assertNotIn(self.shops["2"], [row[0] for row in rows])
        payload = _payload_for(result)
        self.assertEqual(_status_for(payload, shop_ref=shop1, sku_ref=sku1), "match")
        second = [row for row in _rows(payload)
                  if row["shop_ref"] == _shop_ref(self.tag + "2")][0]
        self.assertEqual(second["audit_status"], "missing_standard")

    def test_basis_freeze_is_validated_by_the_store_not_only_by_the_database(self):
        """两种 Store 跑同一份形式校验：内存版不能比数据库宽。"""
        store = MemoryQueryRunStore(forbidden_values={"S1"})
        run_id = store.create_run(NewQueryRun(
            chat_id=UUID(int=1), user_message_id=UUID(int=2), subject_id="u1",
            tool_call_id="c", domain="listing_price_audit", attempt_no=1,
            normalized_request={}, state={"node": "finalize", "status": "running",
                                          "revision": 0}))
        with self.assertRaises(ValueError):
            store.record_listing_audit_basis(
                run_id, subject_id="u1", fingerprint=None,
                roster=[("S1", "not-a-ref", "")], expectations=[],
                price_basis="list_price", currency="CNY")
        with self.assertRaises(ValueError):
            store.record_listing_audit_basis(
                run_id, subject_id="u1", fingerprint=None, roster=[],
                expectations=[("S1", "", 19.9, "all_selected")],  # float 金额
                price_basis="list_price", currency="CNY")
        with self.assertRaises(ValueError):
            store.record_listing_audit_basis(
                run_id, subject_id="u1", fingerprint=None, roster=[],
                expectations=[("S1", "", "19.90", "shop")],  # 未登记档位
                price_basis="list_price", currency="CNY")
        store.record_listing_audit_basis(
            run_id, subject_id="u1", fingerprint=None, roster=[],
            expectations=[("S1", "", "19.90", "all_selected")],
            price_basis="list_price", currency="CNY")
        self.assertEqual(store.runs[run_id]["listing_audit_basis"]["expectations"],
                         (("S1", "", "19.90", "all_selected"),))

    def test_this_round_authorization_defines_the_roster(self):
        """roster 只展开本轮授权店铺：上一轮查过哪些店对本次范围没有任何影响。"""
        self._two_shop_setup()
        payload = _payload_for(self._run(allowed=self._allow("2")))
        self.assertEqual([row["shop_ref"] for row in _rows(payload)],
                         [_shop_ref(self.tag + "2")])
        # 同一轮里把授权放开到两家，roster 就变两格：分母跟着本轮范围走。
        both = _payload_for(self._run(allowed=self._allow("1", "2")))
        self.assertEqual(both["audit"]["expected_items"], 2)

    def test_conflicting_targets_for_the_same_item_need_input(self):
        self._two_shop_setup()
        result = self._run(expected_prices=[
            _price("19.90"),
            _price("29.90", applies_to="shop_sku",
                   shop_ref=_shop_ref(self.tag + "1"),
                   sku_ref=_sku_ref(self._sku_id("SKU1")))])
        self.assertEqual(result.status.value, "needs_input")
        self.assertIn("冲突", _limitation_text(result.model_payload)
                      + str(result.model_payload.get("candidates", "")))
        self.assertEqual(result.artifacts, [])

    def test_duplicate_identical_targets_are_not_a_conflict(self):
        self._two_shop_setup()
        result = self._run(expected_prices=[_price("19.90"), _price("19.90")])
        self.assertEqual(result.status.value, "success")

    def test_partial_sku_coverage_yields_missing_standard_not_pass(self):
        """只给一个 SKU 的目标价：另一个 SKU 是 missing_standard，整份不判通过。"""
        self._shop("1")
        self._trade("1")
        self._map("1", listing="L1", sku="SKU1")
        self._map("1", listing="L1", sku="SKU2")
        self._verify_source()
        self._snapshot("1", items=[
            {"listing_id": "L1", "platform_sku_id": "PS-L1-SKU1",
             "erp_sku_id": self._sku_id("SKU1"), "list_amount": "19.90"},
            {"listing_id": "L1", "platform_sku_id": "PS-L1-SKU2",
             "erp_sku_id": self._sku_id("SKU2"), "list_amount": "39.90"}])
        sku1, sku2 = _sku_ref(self._sku_id("SKU1")), _sku_ref(self._sku_id("SKU2"))
        result = self._run(
            expected_prices=[_price("19.90", applies_to="sku", sku_ref=sku1)],
            product={"text": "直钉枪", "sku_refs": [sku1, sku2]})
        payload = _payload_for(result)
        self.assertEqual(result.status.value, "partial")
        self.assertEqual(_status_for(payload, shop_ref=_shop_ref(self.tag + "1"),
                                     sku_ref=sku1), "match")
        second = _row_for(payload, shop_ref=_shop_ref(self.tag + "1"), sku_ref=sku2)
        self.assertEqual(second["audit_status"], "missing_standard")
        self.assertEqual(second["actual_amount"], "39.90",
                         "缺标准价仍展示已采集价格（spec §6），但不判通过")
        self.assertIsNone(second["expected_amount"])
        self.assertIsNone(second["amount_difference"])
        self.assertFalse(payload["audit"]["all_correct"])
        self.assertEqual(payload["audit"]["counts"], {"match": 1, "missing_standard": 1})

    def test_explicit_all_selected_covers_every_sku(self):
        """只有用户明确「所有规格统一价」（applies_to=all_selected）才覆盖多 SKU。"""
        self._shop("1")
        self._trade("1")
        self._map("1", listing="L1", sku="SKU1")
        self._map("1", listing="L1", sku="SKU2")
        self._verify_source()
        self._snapshot("1", items=[
            {"listing_id": "L1", "platform_sku_id": "PS-L1-SKU1",
             "erp_sku_id": self._sku_id("SKU1"), "list_amount": "19.90"},
            {"listing_id": "L1", "platform_sku_id": "PS-L1-SKU2",
             "erp_sku_id": self._sku_id("SKU2"), "list_amount": "19.90"}])
        result = self._run()
        payload = _payload_for(result)
        self.assertEqual({row["audit_status"] for row in _rows(payload)}, {"match"})
        self.assertEqual(payload["audit"]["expected_items"], 2)
        self.assertTrue(payload["audit"]["all_correct"])
        self.assertEqual(result.status.value, "success")

    # -- 5.3 roster 与「缺一家」 ------------------------------------------

    def test_missing_shop_snapshot_stays_unknown_and_blocks_clean_verdict(self):
        """目标三店只采到两店：第三店是 unknown，整份结果不能说「全部正确」。"""
        self._two_shop_setup()
        self._shop("3")
        self._map("3", listing="L3", sku="SKU1")
        result = self._run()
        payload = _payload_for(result)
        self.assertEqual(result.status.value, "partial")
        self.assertEqual(_status_for(payload, shop_ref=_shop_ref(self.tag + "3")),
                         "unknown")
        self.assertFalse(payload["audit"]["all_correct"])
        self.assertEqual(payload["audit"]["expected_items"], 3)
        self.assertEqual(payload["audit"]["evaluated_items"], 2)
        self.assertIn("快照", _limitation_text(payload))

    def test_mapped_listing_without_any_trade_still_enters_the_roster(self):
        """没成交的已映射链接必须进目标全集：否则「新上架没卖动」会被说成没有这项。"""
        self._two_shop_setup()
        self._shop("4")
        self._map("4", listing="L4", sku="SKU1")        # 只有映射，没有成交行
        self._snapshot("4", items=[{"listing_id": "L4", "erp_sku_id": self._sku_id("SKU1"),
                                    "list_amount": "19.90"}])
        payload = _payload_for(self._run(), )
        self.assertEqual(_status_for(payload, shop_ref=_shop_ref(self.tag + "4")), "match")

    def test_explicit_unauthorized_shop_ref_is_forbidden(self):
        self._two_shop_setup()
        outsider = self._shop("9", platform="jd")
        result = self._run(scope={"mode": "selected",
                                  "shop_refs": [ref_for_key("shop", outsider)]},
                           allowed=frozenset())
        self.assertEqual(result.status.value, "failed")
        self.assertEqual(result.model_payload, {"status": "failed"},
                         "越权只得到一个否：不解释，也不证实那家店存在")

    def test_roster_larger_than_the_cap_refuses_instead_of_truncating(self):
        """全商品盘点不能因为先 Top N 而漏检：超出上限必须拒绝出数。"""
        self._shop("1")
        self._trade("1")
        for index in range(MAX_ROSTER_ITEMS + 1):
            self._map("1", listing=f"L{index}", sku=f"SKU{index}")
        self._verify_source()
        self._snapshot("1", items=[])
        result = self._run()
        self.assertEqual(result.status.value, "failed",
                         "超出上限必须显式拒绝，不能扫前 N 项就说复核完成")
        self.assertEqual(result.artifacts, [])
        self.assertEqual(str(result.error.code), "unavailable")

    # -- 5.4 快照质量：完整枚举、时效、币种、价口径 -------------------------

    def test_not_listed_requires_enumeration_evidence(self):
        self._shop("1")
        self._shop("2")
        self._trade("1")
        self._map("1", listing="L1", sku="SKU1")
        self._map("2", listing="L2", sku="SKU1")
        self._verify_source()
        # 店 1：全量枚举证据成立，链接确实不在架上。
        self._snapshot("1", enumeration_complete=True, enumeration_evidence="probe-enum-1",
                       items=[])
        # 店 2：没有枚举完整性声明，缺行只能算未知。
        self._snapshot("2", enumeration_complete=False, items=[])
        payload = _payload_for(self._run())
        shop1, shop2 = _shop_ref(self.tag + "1"), _shop_ref(self.tag + "2")
        self.assertEqual(_status_for(payload, shop_ref=shop1), "not_listed")
        self.assertEqual(_status_for(payload, shop_ref=shop2), "unknown",
                         "缺列表 / 无权限不能当未上架（spec §5.4）")
        self.assertFalse(payload["audit"]["all_correct"])
        self.assertIn("枚举", _limitation_text(payload))

    def test_stale_snapshot_is_never_reported_as_correct(self):
        self._shop("1")
        self._trade("1")
        self._map("1", listing="L1", sku="SKU1")
        self._verify_source()
        self._snapshot("1", captured_at=STALE,
                       items=[{"listing_id": "L1", "erp_sku_id": self._sku_id("SKU1"),
                               "list_amount": "19.90"}])
        payload = _payload_for(self._run())
        rows = _rows(payload)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["audit_status"], "stale",
                         "价格相同也不判 match：过期快照证明不了「现在」是对的")
        self.assertIsNone(rows[0]["amount_difference"],
                          "过期项不给差额：那会被读成当前差异")
        self.assertFalse(payload["audit"]["all_correct"])
        self.assertIn("时效", _limitation_text(payload))

    def test_currency_mismatch_is_not_compared(self):
        self._shop("1")
        self._trade("1")
        self._map("1", listing="L1", sku="SKU1")
        self._verify_source()
        self._snapshot("1", items=[{"listing_id": "L1", "erp_sku_id": self._sku_id("SKU1"),
                                    "list_amount": "19.90", "currency": "USD"}])
        rows = _rows(_payload_for(self._run()))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["audit_status"], "unknown")
        self.assertIsNone(rows[0]["actual_amount"],
                          "币种不可比时不发金额：没有换算依据的数比没有数更危险")
        self.assertIsNone(rows[0]["amount_difference"])
        self.assertEqual(rows[0]["currency"], "CNY", "行内币种说的是本轮标准那一侧")

    def test_campaign_price_basis_requires_a_declared_campaign(self):
        self._shop("1")
        self._trade("1")
        self._map("1", listing="L1", sku="SKU1")
        self._verify_source()
        self._snapshot("1", items=[{"listing_id": "L1", "erp_sku_id": self._sku_id("SKU1"),
                                    "list_amount": "19.90"}])
        payload = _payload_for(self._run(
            price_basis="campaign_price",
            expected_prices=[_price("19.90", price_basis="campaign_price")]))
        rows = _rows(payload)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["audit_status"], "unknown")
        self.assertEqual(rows[0]["price_basis"], "campaign_price")
        self.assertIn("活动价", _limitation_text(payload))

    def test_campaign_amount_is_compared_when_declared(self):
        self._shop("1")
        self._trade("1")
        self._map("1", listing="L1", sku="SKU1")
        self._verify_source()
        self._snapshot("1", items=[{"listing_id": "L1", "erp_sku_id": self._sku_id("SKU1"),
                                    "list_amount": "19.90", "campaign_amount": "15.90"}])
        payload = _payload_for(self._run(
            price_basis="campaign_price",
            expected_prices=[_price("15.90", price_basis="campaign_price")]))
        self.assertEqual(_rows(payload)[0]["audit_status"], "match")

    def test_off_sale_listing_is_not_a_match(self):
        self._shop("1")
        self._trade("1")
        self._map("1", listing="L1", sku="SKU1")
        self._verify_source()
        self._snapshot("1", items=[{"listing_id": "L1", "erp_sku_id": self._sku_id("SKU1"),
                                    "list_amount": "19.90", "on_sale": False}])
        payload = _payload_for(self._run())
        self.assertEqual(_rows(payload)[0]["audit_status"], "not_on_sale")
        self.assertFalse(payload["audit"]["all_correct"])

    def test_unmapped_sku_is_not_not_listed(self):
        """SKU 没有渠道落点、快照又没声明身份：这是 unmapped，不是「确认没上架」。"""
        self._shop("1")
        self._trade("1")
        self._verify_source()
        self._snapshot("1", enumeration_complete=False, items=[])
        rows = _rows(_payload_for(self._run()))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["audit_status"], "unmapped")
        self.assertIsNone(rows[0].get("sku_ref"))

    def test_every_listing_of_one_sku_is_audited_separately(self):
        """同 SKU 两个链接：逐个复核，不取最便宜那条，也不合并成一行。"""
        self._shop("1")
        self._trade("1")
        self._map("1", listing="LA", sku="SKU1")
        self._map("1", listing="LB", sku="SKU1")
        self._verify_source()
        self._snapshot("1", items=[
            {"listing_id": "LA", "platform_sku_id": "PS-A",
             "erp_sku_id": self._sku_id("SKU1"), "list_amount": "19.90"},
            {"listing_id": "LB", "platform_sku_id": "PS-B",
             "erp_sku_id": self._sku_id("SKU1"), "list_amount": "29.90"}])
        payload = _payload_for(self._run())
        rows = _rows(payload)
        self.assertEqual(len(rows), 2, "两条链接各一行，不折成一条")
        self.assertEqual({row["audit_status"] for row in rows}, {"match", "mismatch"})
        self.assertEqual(len({row["listing_ref"] for row in rows}), 2)
        self.assertFalse(payload["audit"]["all_correct"])
        self.assertEqual(payload["audit"]["expected_items"], 2)
        # 两条链接都判完了，所以这份复核是"做完了"（success），只是结论是"有一对不上"。
        # 把它说成 partial 就会楘掉另一个信号：partial 在本应用里一贯意思是"有范围没评到"。
        judged = self._run()
        self.assertEqual(judged.status.value, "success", judged.model_payload)
        # 标准价是按 (店, SKU) 冻的，不是按链接冻的：一个用户给的价回答的是这个规格
        # 在这家店应该卖多少，两条链接共享同一个标准但各自占一行 roster。
        recorder = _RecordingConn(self.conn)
        stored = self._run(conn=recorder, store=self._db_store(recorder))
        counts = self.conn.execute(
            "SELECT (SELECT count(*) FROM bi.expected_listing_rosters "
            "            WHERE run_id = %s), "
            "       (SELECT count(*) FROM bi.price_audit_expectations WHERE run_id = %s)",
            (stored.run_id, stored.run_id)).fetchone()
        self.assertEqual(tuple(counts), (2, 1),
                         "roster 逐链接，标准逐 (店, SKU)：两者不是一个粒度不是写错了")

    def test_two_skus_with_different_targets_are_audited_per_sku(self):
        self._shop("1")
        self._trade("1")
        self._map("1", listing="L1", sku="SKU1")
        self._map("1", listing="L1", sku="SKU2")
        self._verify_source()
        self._snapshot("1", items=[
            {"listing_id": "L1", "platform_sku_id": "PS-L1-SKU1",
             "erp_sku_id": self._sku_id("SKU1"), "list_amount": "19.90"},
            {"listing_id": "L1", "platform_sku_id": "PS-L1-SKU2",
             "erp_sku_id": self._sku_id("SKU2"), "list_amount": "39.90"}])
        sku1, sku2 = _sku_ref(self._sku_id("SKU1")), _sku_ref(self._sku_id("SKU2"))
        payload = _payload_for(self._run(expected_prices=[
            _price("19.90", applies_to="sku", sku_ref=sku1),
            _price("39.90", applies_to="sku", sku_ref=sku2)]))
        self.assertEqual({row["audit_status"] for row in _rows(payload)}, {"match"})
        self.assertTrue(payload["audit"]["all_correct"])

    def test_latest_snapshot_per_shop_is_used_not_the_oldest(self):
        self._two_shop_setup()
        self._snapshot("1", snapshot_id=f"snap-{self.tag}-1-new",
                       captured_at=NOW - timedelta(minutes=5),
                       items=[{"listing_id": "L1", "erp_sku_id": self._sku_id("SKU1"),
                               "list_amount": "29.90"}])
        payload = _payload_for(self._run())
        row = _row_for(payload, shop_ref=_shop_ref(self.tag + "1"))
        self.assertEqual(row["actual_amount"], "29.90")
        self.assertEqual(row["snapshot_at"], NEWER_ISO)
        sources = {entry["shop_ref"]: entry for entry in payload["audit"]["sources"]}
        self.assertEqual(sources[_shop_ref(self.tag + "1")]["snapshot_at"], NEWER_ISO,
                         "来源清单与判定行必须指同一次快照")

    def test_disabled_shop_is_excluded_with_a_reason(self):
        self._two_shop_setup()
        self._shop("5")
        self.conn.execute("UPDATE bi.shops SET enabled = false WHERE shop_id = %s",
                          (self.shops["5"],))
        payload = _payload_for(self._run())
        excluded = {item["shop_ref"]: item["reason"] for item in payload["excluded_scope"]}
        shop5 = _shop_ref(self.tag + "5")
        self.assertIn(shop5, excluded)
        self.assertNotIn(shop5, {row["shop_ref"] for row in _rows(payload)},
                         "被排除的店不进 roster，也不给一个 0")

    # -- 5.5 发布与失败路径 ------------------------------------------------

    def test_all_correct_requires_fresh_complete_matching_evidence(self):
        self._two_shop_setup()
        result = self._run()
        payload = _payload_for(result)
        self.assertEqual(result.status.value, "success")
        self.assertTrue(payload["audit"]["all_correct"])
        self.assertEqual(payload["audit"]["counts"], {"match": 2})
        self.assertEqual(payload["audit"]["expected_items"],
                         payload["audit"]["evaluated_items"])

    def test_artifact_persistence_failure_is_failed_not_partial(self):
        self._two_shop_setup()
        result = self._run(store=_FailingStore(forbidden_values=set(self.shops.values())))
        self.assertEqual(result.status.value, "failed")
        self.assertEqual(result.artifacts, [])
        self.assertEqual(result.model_payload, {"status": "failed"},
                         "被拒数字不得回流")

    def test_expectation_freeze_failure_is_failed_not_silently_dropped(self):
        self._two_shop_setup()
        # 把 Store 与图共用的连接换成一个"写依据时会报错"的替身：必需结果存不下必须
        # 是 failed，不能退化成"少一份依据但结论照发"。
        failing = _FailingExpectationWrite(self.conn)
        store = self._db_store(failing)
        result = self._run(conn=failing, store=store)
        self.assertEqual(result.status.value, "failed")
        self.assertEqual(result.model_payload, {"status": "failed"})
        self.assertEqual(str(result.error.code), "artifact_persistence_failed")

    def test_unresolved_product_is_missing_data_not_zero_items(self):
        self._shop("1")
        self._trade("1")
        result = self._run(product={"text": "不存在的商品名"})
        self.assertEqual(result.status.value, "missing_data")
        self.assertEqual(result.artifacts, [])
        self.assertIn("商品", _limitation_text(result.model_payload))

    def test_ambiguous_product_returns_candidate_refs_only(self):
        self._shop("1")
        self._trade("1")
        self._trade("1", product_id=f"{self.tag}OTHER", erp_id=f"{self.tag}EO",
                    sku="OTHER")
        result = self._run()
        self.assertEqual(result.status.value, "needs_input")
        candidates = result.model_payload.get("candidates")
        self.assertEqual(len(candidates), 2, candidates)
        self.assertTrue(all(set(item) == {"ref"} for item in candidates), candidates)

    def test_model_payload_carries_refs_only_and_artifact_carries_names(self):
        self._two_shop_setup()
        result = self._run()
        raw = str(result.model_payload)
        for leak in (self.shops["1"], self.product_id, "L1", "PS-L1-SKU1",
                     self._sku_id("SKU1"), "probe-t9"):
            self.assertNotIn(leak, raw, f"模型载荷不得出现真实标识 {leak}")
        payload = _payload_for(result)
        names = {entity["ref"]: entity.get("display_name")
                 for entity in payload["entities"]}
        self.assertEqual(names[_shop_ref(self.tag + "1")], "价审测试店1")
        self.assertNotIn("entities", result.model_payload)
        self.assertNotIn("catalog_version", result.model_payload)
        validate_model_payload(result.model_payload)
        for row in _rows(payload):
            self.assertIn("shop_ref", row)
            self.assertNotIn("shop_id", row)

    def test_run_walks_the_fixed_chain_under_its_own_domain(self):
        self._two_shop_setup()
        store = MemoryQueryRunStore(forbidden_values=set(self.shops.values()))
        result = self._run(store=store)
        run = store.runs[result.run_id]
        self.assertEqual(run["domain"], "listing_price_audit")
        self.assertEqual(run["status"], "succeeded")
        nodes = [event["node"] for event in store.events[result.run_id]]
        self.assertEqual(set(nodes) & set(LISTING_NODES), set(LISTING_NODES),
                         "十一节点一条链，不另开第二条路径")
        self.assertEqual(nodes[0], "resolve_scope_product_and_skus")
        self.assertEqual(nodes[-1], "finalize")

    def test_provenance_records_rule_and_graph_versions(self):
        self._two_shop_setup()
        store = MemoryQueryRunStore(forbidden_values=set(self.shops.values()))
        result = self._run(store=store)
        provenance = store.runs[result.run_id]["provenance"]
        self.assertEqual(provenance.graph_version, LISTING_GRAPH_VERSION)
        self.assertEqual(provenance.template_id, "listing_price_audit")
        # 来源注册表版本进血缘：某个平台的门禁一开，旧结果就不能再被命中。
        self.assertEqual(provenance.policy_version, LISTING_SOURCE_REGISTRY_VERSION)
        self.assertEqual(provenance.schema_version, LISTING_SCHEMA_VERSION)
        self.assertEqual(provenance.metric_version, LISTING_METRIC_VERSION)
        self.assertTrue(provenance.mapping_version,
                        "roster 来自渠道映射：映射版本必须进血缘")
        # 本轮快照批次进血缘：没有它就无法回答"这些价格是抓哪一批"。
        self.assertEqual(len(provenance.source_batches), 2, provenance.source_batches)
        run = store.runs[result.run_id]
        self.assertEqual(run["state"]["target_status"], "success")
        self.assertEqual([entry["node"] for entry in store.events[result.run_id]][-1],
                         "finalize")

    def test_opening_the_gate_changes_the_verdict_for_the_same_question(self):
        """同一句提问：门禁前只能 missing_data，门禁后才能给出确定结论。

        这条与节点级的 A/B 用例同一形状，但走的是完整图（含真库读取）。两边都要有：
        节点级保证默认跑能执行，图级保证真数据路径上这个开关真的通得过。
        """
        self._two_shop_setup()
        reset_listing_sources()        # 夹具登记的来源只服务于其他用例
        before = audit_listing_prices(_request(), self._context())
        self.assertEqual(before.status.value, "missing_data", before.model_payload)
        self.assertEqual(_payload_for(before)["audit"]["evaluated_items"], 0)
        self._verify_source()
        after = audit_listing_prices(_request(), self._context())
        self.assertEqual(after.status.value, "success", after.model_payload)
        # 注册表版本参与血缘：它是代码常量，所以"变的是结果，不是标签"。
        assert after.provenance is not None and before.provenance is not None
        self.assertEqual(after.provenance.policy_version,
                         before.provenance.policy_version)


    def test_deadline_exhaustion_reports_failure_not_an_empty_pass(self):
        self._two_shop_setup()
        context = self._context()
        context.deadline = time.monotonic() - 1
        result = run_listing_audit_graph(request=_request(), context=context,
                                         tool_call_id="call-x").domain_result
        self.assertEqual(result.status.value, "failed")
        # 载荷说的是"暂不可用 + 为什么"，不是一句 failed：预算耗尽与保存失败是两个
        # 不同的下一步（前者缩范围重问，后者原样重试）。
        self.assertEqual(result.model_payload["status"], "unavailable")
        self.assertIn("预算", "；".join(result.model_payload["limitations"]))
        self.assertEqual(result.artifacts, [])

    def test_no_authorized_shops_is_missing_data(self):
        self._shop("1")
        result = self._run(allowed=frozenset())
        self.assertEqual(result.status.value, "missing_data")
        self.assertEqual(result.artifacts, [])

    def test_the_app_role_can_actually_write_this_round_basis(self):
        """在真实部署身份下跑一遍写入路径：只以表所有者身份跑过的写入不是写入路径。

        本用例报过一个真错：`ON CONFLICT DO UPDATE` 需要 UPDATE 权限，而 018 故意只给
        应用身份 `SELECT, INSERT`（"本轮依据一次写入后不可改写"）。绿色测试当时藏住了
        这一点，因为它们都以表所有者身份写。
        """
        shop = self._shop("1")
        chat, message, subject = self._seed_run_context()
        run_id = uuid4()
        self.conn.execute(
            "INSERT INTO bi.query_runs(id, chat_id, user_message_id, subject_id, "
            "tool_call_id, domain, attempt_no, status) "
            "VALUES (%s, %s, %s, %s, 'app-role-write', 'listing_price_audit', 1, "
            "'running')", (run_id, chat, message, subject))
        with self.conn.transaction():
            self.conn.execute("SET LOCAL ROLE bi_app")
            # 走的就是生产那一条：Store 方法 + 领域模块里的 SQL，不手拄 SQL。
            from bi_agent.runtime.repository import PostgresQueryRunStore

            store = PostgresQueryRunStore(self.conn, forbidden_values={shop})
            store.record_listing_audit_basis(
                run_id, subject_id=subject, fingerprint=None,
                roster=[(shop, "lst-0123456789ab", _sku_ref("SKU-APP"))],
                expectations=[(shop, _sku_ref("SKU-APP"), "19.90", "all_selected")],
                price_basis="list_price", currency="CNY")
            frozen = self.conn.execute(
                "SELECT captured_from, expected_amount FROM bi.price_audit_expectations "
                "WHERE run_id = %s", (run_id,)).fetchall()
            self.assertEqual([tuple(row) for row in frozen],
                             [("current_user_input", Decimal("19.9000"))])
            # 一次写入后不可改写：应用身份既没有 UPDATE，代码也不叕事重写。
            with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                with self.conn.transaction():
                    self.conn.execute(
                        "UPDATE bi.price_audit_expectations SET expected_amount = 1 "
                        "WHERE run_id = %s", (run_id,))

    def test_two_links_of_one_sku_freeze_one_standard_not_two_rows(self):
        """同一 (店, SKU) 的两条链接共享一个标准行：合并只在一逐字段完全一致时允许。"""
        entries = [
            repository.ExpectationRow(shop_id="S1", sku_ref=_sku_ref("SKU-A"),
                                      expected_amount="19.90", currency="CNY",
                                      price_basis="list_price", applies_to="all_selected"),
            repository.ExpectationRow(shop_id="S1", sku_ref=_sku_ref("SKU-A"),
                                      expected_amount="19.90", currency="CNY",
                                      price_basis="list_price", applies_to="all_selected")]
        self.assertEqual(len(repository.dedupe_expectations(entries)), 1)
        entries[1] = entries[1]._replace(expected_amount="29.90")
        with self.assertRaises(ValueError):
            # 同一次执行对同一格给两个价：那不是"去重"，是必须被看见的矛盾。
            repository.dedupe_expectations(entries)

    def test_a_node_invariant_failure_degrades_to_a_contract_violation(self):
        """判定不变量报错必须被降级成契约违规，不能把异常抛到主层吞掉整回合。

        一抛出去：已建立的运行记录靠 `_finish_run_as_failed` 收尾，而用户拿到的是一个
        错误码而不是"这份结果发不了"。文档里写了这一层处置，就得有用例看着它发生。
        """
        self._two_shop_setup()
        original = repository.latest_snapshots

        def exploding(conn, **kwargs):
            raise ValueError("listing_judgement_needs_both_sides")

        repository.latest_snapshots = exploding
        try:
            result = self._run()
        finally:
            repository.latest_snapshots = original
        self.assertEqual(result.status.value, "failed")
        self.assertEqual(str(result.error.code), "result_contract_violation")
        self.assertEqual(result.model_payload, {"status": "failed"},
                         "一个数字都不能发：被拒的结果不得以正文或卡片形式回流")
        self.assertEqual(result.artifacts, [])

    def test_only_undecided_cells_make_the_audit_incomplete(self):
        """全部格都判出"未上架"是一份做完了的复核，不是一次证据不足的复核。

        两种报告的下一步完全不同：前者去上架，后者去补拓。把确定结论归入
        "没定下来"，一份完整的"这家店确实没上"就会被说成"证据不足"。
        """
        self._two_shop_setup()
        self._shop("3")
        self._trade("3")
        self._map("3", listing="L3", sku="SKU1")
        # 第三家有全量枚举凭据，但架上没有这个商品：这一格是确定结论。
        self._snapshot("3", items=[])
        payload = _payload_for(self._run())
        statuses = {row["shop_ref"]: row["audit_status"] for row in _rows(payload)}
        self.assertEqual(statuses[_shop_ref(self.tag + "3")], "not_listed")
        self.assertFalse(payload["audit"]["all_correct"],
                         "做完了 ≠ 全对了：未上架就是一对不上")
        # 三格都有确定结论（两格一致 + 一格未上架）：不报 partial，也不说证据不足
        whole = self._run(expected_prices=[_price("19.90")])
        self.assertNotIn("期望项未全部判定", str(whole.model_payload))

    def test_snapshot_captured_at_cannot_be_re_stamped_as_today(self):
        """把一批旧数据重新贴一个今天的抓取时点，在存储层就该被拒。

        `recorded_at` 是服务端默认值，写入方改不了，所以"captured_at 不得晚于写入
        时刻"是一条可执行约束；没有它，时效门禁就只是一个可以被重新导入绕过的数字。
        """
        shop = self._shop("1")
        with self.assertRaises(psycopg.errors.CheckViolation):
            with self.conn.transaction():
                self.conn.execute(
                    "INSERT INTO bi.listing_snapshots(snapshot_id, namespace, platform, "
                    "shop_id, source, evidence, captured_at) VALUES "
                    "('stamped', %s, 'tb', %s, 'official_export', 'probe', now() + "
                    "interval '2 hours')", (DEFAULT_NAMESPACE, shop))
        with self.assertRaises(psycopg.errors.CheckViolation):
            with self.conn.transaction():
                self.conn.execute(
                    "INSERT INTO bi.listing_snapshots(snapshot_id, namespace, platform, "
                    "shop_id, source, evidence, captured_at) VALUES "
                    "('', %s, 'tb', %s, 'official_export', 'probe', now())",
                    (DEFAULT_NAMESPACE, shop),
                )

    def test_the_audit_reads_through_reporting_views_as_the_app_role(self):
        """真实部署以 bi_app 身份连接：读走视图、写走授权，一条都不能多。

        每条预期失败的语句都包在自己的保存点里：一次权限异常会把后面全部语句变成
        InFailedSqlTransaction，那时后面的断言过的就不是它们声称的那一道门了。
        """
        self._two_shop_setup()
        with self.conn.transaction():
            self.conn.execute("SET LOCAL ROLE bi_app")
            rows = self.conn.execute(
                "SELECT count(*) FROM reporting.v_listing_snapshot_items").fetchone()
            self.assertGreater(rows[0], 0, "快照读取必须有应用身份可用的视图")
            for table in ("bi.listing_snapshots", "bi.listing_snapshot_items"):
                with self.assertRaises(psycopg.errors.InsufficientPrivilege,
                                       msg=table):
                    with self.conn.transaction():
                        self.conn.execute(f"SELECT count(*) FROM {table}").fetchone()
            # 取证凭据不进视图：模型路径上任何一层都拿不到它，而不是"拿到但不展示"。
            with self.assertRaises(psycopg.errors.UndefinedColumn):
                with self.conn.transaction():
                    self.conn.execute(
                        "SELECT evidence FROM reporting.v_listing_snapshots").fetchone()
            with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                with self.conn.transaction():
                    self.conn.execute(
                        "SELECT evidence FROM bi.listing_snapshots").fetchone()
            # 写入路径：应用身份能写本轮依据，但不能写快照（快照只属于同步/导入身份）。
            chat, message, subject = self._seed_run_context()
            basis_run = uuid4()
            with self.conn.transaction():
                self.conn.execute(
                    "INSERT INTO bi.query_runs(id, chat_id, user_message_id, subject_id, "
                    "tool_call_id, domain, attempt_no, status) VALUES "
                    "(%s, %s, %s, %s, 'app-role-probe', 'listing_price_audit', 1, "
                    "'running')", (basis_run, chat, message, subject))
                self.conn.execute(
                    "INSERT INTO bi.price_audit_expectations(run_id, subject_id, shop_id, "
                    "expected_amount, currency, price_basis, applies_to) VALUES "
                    "(%s, %s, %s, '19.90', 'CNY', 'list_price', 'all_selected')",
                    (basis_run, subject, self.shops["1"]))
                with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                    with self.conn.transaction():
                        # 快照只能由同步/导入身份写：应用身份能写本轮依据，不能写快照。
                        self.conn.execute(
                            "INSERT INTO bi.listing_snapshots(snapshot_id, namespace, "
                            "platform, shop_id, source, evidence, captured_at) VALUES "
                            "('probe', 'ns', 'tb', %s, 'official_export', 'e', now())",
                            (self.shops["1"],))

    def test_storage_layer_refuses_what_the_python_helper_cannot_see(self):
        """直写 SQL 过一遍 018 的 CHECK：证明它们是存储层约束，不只是入口函数客气。

        入口校验能挡住走正路的调用，但部署里真正不能发生的是"有人绕过函数写了
        一行半个声明"。这一用例逐条把那种行递到数据库面前。
        """
        shop = self._shop("1")
        base = dict(snapshot_id="snap-check", namespace=DEFAULT_NAMESPACE, platform="tb",
                    shop_id=shop, source="official_export", evidence="probe-check",
                    captured_at=FRESH, enumeration_complete=False)

        def insert(**overrides):
            row = {**base, **overrides}
            with self.conn.transaction():
                self.conn.execute(
                    "INSERT INTO bi.listing_snapshots(snapshot_id, namespace, platform, "
                    "shop_id, source, evidence, captured_at, enumeration_complete, "
                    "enumeration_evidence) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                    (row["snapshot_id"], row["namespace"], row["platform"],
                     row["shop_id"], row["source"], row["evidence"], row["captured_at"],
                     row["enumeration_complete"], row.get("enumeration_evidence")))

        for label, overrides in (
                ("来源码白名单", {"source": "erp_suggested_price"}),
                ("来源码白名单-平台API外写法", {"source": "scrape"}),
                ("证据非空", {"evidence": "  "}),
                ("完整性声明与凭据成对",
                 {"enumeration_complete": True, "enumeration_evidence": None}),
                ("未声明完整就不能带凭据",
                 {"enumeration_complete": False, "enumeration_evidence": "x"}),
                ("命名空间非空", {"namespace": "  "}),
                ("平台码形式", {"platform": "TB 淘宝"})):
            with self.subTest(rule=label):
                with self.assertRaises(psycopg.errors.CheckViolation):
                    insert(**overrides)
        # 目标价冻结表的 captured_from 是一句 CHECK，不是一句约定。
        chat, message, subject = self._seed_run_context()
        run_id = uuid4()
        with self.conn.transaction():
            self.conn.execute(
                "INSERT INTO bi.query_runs(id, chat_id, user_message_id, subject_id, "
                "tool_call_id, domain, attempt_no, status) VALUES "
                "(%s, %s, %s, %s, 'check', 'listing_price_audit', 1, 'running')",
                (run_id, chat, message, subject))
            # 每条都要在自己的保存点里失败：一次权限/约束异常会把后面几条都变成
            # InFailedSqlTransaction，那时通过的断言说的就不是它声称的那条规则了。
            for label, sql, params in (
                    ("captured_from 钉死本轮",
                     "INSERT INTO bi.price_audit_expectations(run_id, subject_id, shop_id, "
                     "expected_amount, currency, price_basis, applies_to, captured_from) "
                     "VALUES (%s, %s, %s, '19.90', 'CNY', 'list_price', 'all_selected', "
                     "'previous_run')", (run_id, subject, shop)),
                    ("档位词表可枚举",
                     "INSERT INTO bi.price_audit_expectations(run_id, subject_id, shop_id, "
                     "expected_amount, currency, price_basis, applies_to) "
                     "VALUES (%s, %s, %s, '19.90', 'CNY', 'list_price', 'shop')",
                     (run_id, subject, shop))):
                with self.subTest(rule=label):
                    with self.assertRaises(psycopg.errors.CheckViolation):
                        with self.conn.transaction():
                            self.conn.execute(sql, params)
            # 没有运行行就记不下依据：审计行不能悬空存在。
            with self.assertRaises(psycopg.errors.ForeignKeyViolation):
                with self.conn.transaction():
                    self.conn.execute(
                        "INSERT INTO bi.price_audit_expectations(run_id, subject_id, "
                        "shop_id, expected_amount, currency, price_basis, applies_to) "
                        "VALUES (%s, %s, %s, '19.90', 'CNY', 'list_price', "
                        "'all_selected')", (uuid4(), subject, shop))

    def test_snapshot_amount_contract_at_the_database_layer(self):
        """金额为负与"两种标价都没声明"都必须在存储层被拒。"""
        shop = self._shop("1")
        sid = self._snapshot("1", items=[])
        for label, (list_amount, campaign) in (
                ("负标价", ("-1.00", None)),
                ("什么价都没说", (None, None)),
                ("活动价也不能为负", (None, "-0.01"))):
            with self.subTest(rule=label):
                with self.assertRaises(psycopg.errors.CheckViolation):
                    with self.conn.transaction():
                        self.conn.execute(
                            "INSERT INTO bi.listing_snapshot_items(snapshot_id, shop_id, "
                            "namespace, listing_id, list_amount, campaign_amount, "
                            "captured_at) VALUES (%s, %s, %s, %s, %s, %s, now())",
                            (sid, shop, DEFAULT_NAMESPACE, f"LX-{label}", list_amount,
                             campaign))

    def test_snapshot_import_contract_refuses_undeclared_provenance(self):
        """导入契约：来源码、证据与完整性声明缺一即拒，不给「半个声明」留活路。"""
        self._shop("1")
        with self.assertRaises(Exception):
            self._snapshot("1", enumeration_complete=True, enumeration_evidence="   ",
                           items=[])
        with self.assertRaises(Exception):
            self._snapshot("1", source="erp_suggested_price", items=[])
        with self.assertRaises(Exception):
            self._snapshot("1", evidence="", items=[])
        with self.assertRaises(Exception):
            self._snapshot("1", captured_at=None, items=[])

    def test_snapshot_items_cannot_claim_a_price_without_a_listing(self):
        self._shop("1")
        with self.assertRaises(Exception):
            self._snapshot("1", items=[{"listing_id": "", "list_amount": "19.90"}])


# ---------------------------------------------------------------------------
# 6. 主 Agent 接线
# ---------------------------------------------------------------------------


class ListingAuditAgentWiringTests(unittest.TestCase):
    """只钉主层接线：工具已公告、上下文由服务端注入、一次调用跑一次图。

    图本身的门禁与判定由上面的 `ListingAuditGraphTests` 钉；这里刻意把图打桩成
    一个返回固定结果的假函数，于是"主层有没有自己算钱/自己展开授权"这件事是唯一的
    被测对象。
    """

    def test_tool_is_announced_next_to_the_other_business_tools(self):
        """价审 Tool 在公告列表里，且排在经营 Tool 之后、推广 Tool 之前。

        Task 10 加了库存 Tool 之后，这里断言的是"本域的 Tool 仍在列表中且顺序稳定"，
        完整六元组顺序由 `tests.test_inventory` 钉（一份名单只在一处当权威）。
        """
        import bi_agent.agent as agent

        names = [item["function"]["name"] for item in agent._tool_schemas()]
        self.assertIn("audit_listing_prices", names)
        self.assertEqual(names.index("audit_listing_prices"),
         names.index("compare_performance") + 1)
        self.assertEqual(names[-1], "evaluate_promotion")

    def test_prompt_says_the_target_price_must_come_from_this_turn(self):
        from bi_agent.agent import _SYSTEM_PROMPT

        self.assertIn("个工具", _SYSTEM_PROMPT)
        self.assertIn("audit_listing_prices", _SYSTEM_PROMPT)
        # 三条模型必须自己承担的说法：本轮取值、缺价要问、未取证不称可用。
        self.assertIn("本轮", _SYSTEM_PROMPT)
        self.assertIn("unsupported", _SYSTEM_PROMPT)
        self.assertIn("needs_input", _SYSTEM_PROMPT)

    def _domain_result(self, status, payload):
        from bi_agent.runtime.models import (
            ArtifactRef, DomainArtifact, DomainResult)

        return DomainResult(
            run_id=uuid4(), status=status, model_payload=payload,
            artifacts=[DomainArtifact(ref=ArtifactRef(id=uuid4(), type=AUDIT_ARTIFACT),
                                      public_payload=payload)])

    def _answer(self, routed, *, payload, status):
        from bi_agent.agent import SessionState, answer
        from bi_agent.listing_audit.graph import ListingExecution
        from bi_agent.llm import ToolCall

        def fake_graph(**kwargs):
            routed.append(kwargs)
            return ListingExecution(domain_result=self._domain_result(status, payload))

        model = mock.Mock()
        model.complete.side_effect = [
            _reply(calls=[ToolCall(
                id="call_a", name="audit_listing_prices",
                arguments={"product": {"text": "直钉枪"},
                           "expected_prices": [_price("19.90")]})]),
            _reply(text="该商品上架价与本轮目标价存在差异")]
        with mock.patch("bi_agent.listing_audit.tool.run_listing_audit_graph",
                        side_effect=fake_graph) as graph:
            turn = answer("直钉枪全店标价 19.90 是否正确", SessionState(subject="u1"),
                          model=model, conn=_ShopListConn(),
                          allowed_shop_ids=frozenset({"S1"}), now=NOW,
                          run_store=MemoryQueryRunStore(forbidden_values={"S1"}))
        return turn, graph

    def test_call_is_routed_once_with_server_side_context(self):
        from bi_agent.runtime.models import DomainStatus

        calls: list[dict] = []
        turn, graph = self._answer(calls, payload=_audit_payload_skeleton(),
                         status=DomainStatus.PARTIAL)
        self.assertEqual(graph.call_count, 1, "一次工具调用只能跑一次图")
        self.assertEqual(len(calls), 1)
        context = calls[0]["context"]
        request = calls[0]["request"]
        self.assertEqual(context.allowed_shop_ids, frozenset({"S1"}),
                         "真实授权集只能由服务端注入，不接受模型给的范围")
        self.assertEqual(context.subject_id, "u1")
        self.assertEqual(calls[0]["tool_call_id"], "call_a")
        self.assertEqual(request.expected_prices[0].expected_amount, "19.90")
        self.assertEqual([artifact["artifact_type"] for artifact in turn.artifacts],
                         [AUDIT_ARTIFACT])
        self.assertEqual(turn.text, "该商品上架价与本轮目标价存在差异")

    def test_target_price_is_not_written_back_into_session_filters(self):
        """本轮目标价不能被回写进会话筛选器：那下一轮的「隐式继承」就有了通道。"""
        from bi_agent.runtime.models import DomainStatus

        calls: list[dict] = []
        turn, _graph = self._answer(calls, payload=_audit_payload_skeleton(),
                          status=DomainStatus.PARTIAL)
        self.assertNotIn("expected_prices", turn.state.filters)
        self.assertNotIn("expected_amount", str(turn.state.filters))

    def test_unsupported_source_is_transcribed_without_a_pass_claim(self):
        """门禁路径要能一路走到用户：主层不得把「一格都没判」的复核说成完成。"""
        from bi_agent.runtime.models import DomainStatus

        payload = {**_audit_payload_skeleton(), "status": "missing_data",
                   "limitations": [TEXT_SOURCE_UNVERIFIED],
                   "audit": {**_audit_payload_skeleton()["audit"],
                             "evaluated_items": 0, "matched_items": 0,
                             "counts": {"unsupported": 1}}}
        calls: list[dict] = []
        turn, _graph = self._answer(calls, payload=payload,
                          status=DomainStatus.MISSING_DATA)
        self.assertNotIn("全部正确", turn.text)
        self.assertIsNone(turn.error_code, "缺来源是数据状态，不是故障码")
        # 卡片仍然发出：期望 roster 与"为什么判不了"本身就是这一轮的答案。
        self.assertEqual([artifact["artifact_type"] for artifact in turn.artifacts],
                         [AUDIT_ARTIFACT])

    def test_audit_card_survives_a_missing_summary_sentence(self):
        """模型没组织出正文时，已发布的差异表不能被说成"本轮没拿到结果"。

        上架复核不产生 ToolResult（差异表不是指标行），所以判据只看 results 就会
        在这一个领域上谎报"没有结果"。这一条钉的就是那个判据。
        """
        from bi_agent.agent import SessionState, answer
        from bi_agent.listing_audit.graph import ListingExecution
        from bi_agent.llm import ToolCall
        from bi_agent.runtime.models import (
            ArtifactRef, DomainArtifact, DomainResult, DomainStatus)

        payload = _audit_payload_skeleton()
        model = mock.Mock()
        model.complete.side_effect = [
            _reply(calls=[ToolCall(
                id="call_a", name="audit_listing_prices",
                arguments={"product": {"text": "直钉枪"},
                           "expected_prices": [_price("19.90")]})]),
            _reply(text="")]
        with mock.patch("bi_agent.listing_audit.tool.run_listing_audit_graph",
                        side_effect=lambda **kwargs: ListingExecution(
                            domain_result=DomainResult(
                                run_id=uuid4(), status=DomainStatus.PARTIAL,
                                model_payload=payload,
                                artifacts=[DomainArtifact(
                                    ref=ArtifactRef(id=uuid4(), type=AUDIT_ARTIFACT),
                                    public_payload=payload)]))):
            turn = answer("直钉枪全店标价 19.90 是否正确", SessionState(subject="u1"),
                          model=model, conn=_ShopListConn(),
                          allowed_shop_ids=frozenset({"S1"}), now=NOW,
                          run_store=MemoryQueryRunStore(forbidden_values={"S1"}))
        self.assertEqual(len(turn.artifacts), 1)
        self.assertIn("见下方数据", turn.text)
        self.assertNotIn("没能给出回答", turn.text)

    def test_persistence_failure_clears_this_turn_results(self):
        """保存失败必须把本轮结果整体撤回，不能留一份「看起来成功」的半截回答。"""
        from bi_agent.agent import SessionState, answer
        from bi_agent.listing_audit.graph import ListingExecution
        from bi_agent.llm import ToolCall
        from bi_agent.runtime.models import DomainResult, DomainStatus, ErrorEnvelope

        failure = DomainResult(
            run_id=uuid4(), status=DomainStatus.FAILED,
            model_payload={"status": "failed"},
            error=ErrorEnvelope(code="artifact_persistence_failed", stage="persist_audit",
                                retryable=True, recovery="retry_later",
                                public_message="结果保存失败，请稍后重试。",
                                problems=["artifact_persistence_failed"]))
        model = mock.Mock()
        model.complete.side_effect = [
            _reply(calls=[ToolCall(
                id="call_a", name="audit_listing_prices",
                arguments={"product": {"text": "直钉枪"},
                           "expected_prices": [_price("19.90")]})]),
            _reply(text="已完成复核，全部正确")]
        with mock.patch("bi_agent.listing_audit.tool.run_listing_audit_graph",
                        side_effect=lambda **kwargs: ListingExecution(
                            domain_result=failure)):
            turn = answer("直钉枪全店标价 19.90 是否正确", SessionState(subject="u1"),
                          model=model, conn=_ShopListConn(),
                          allowed_shop_ids=frozenset({"S1"}), now=NOW,
                          run_store=MemoryQueryRunStore(forbidden_values={"S1"}))
        self.assertEqual(turn.artifacts, [])
        self.assertEqual(turn.error_code, "artifact_persistence_failed")
        self.assertNotIn("全部正确", turn.text)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
