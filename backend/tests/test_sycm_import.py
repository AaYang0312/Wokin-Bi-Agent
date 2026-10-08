"""生意参谋手工导出导入·写入面（计划 §3.M2，共享测试库）。

锁六件事：
1) 导入写事实：白名单值（千分位清洗、比率 ÷100）按 (shop_id, stat_date)
   upsert，batch_id = 文件内容 SHA-256；
2) 幂等：同一文件导两遍，事实行数/取值/batch_id/覆盖区间不变（后批覆盖前批
   由内容相同的批自然成立），台账按次追加（每次导入一行，事件不可变）；
3) 覆盖凭证：covered multirange 跨批累加且自动归并、data_as_of 只进不退、
   quality_status='unknown' + quality_rule='manual-export/unverified'；
4) 能力授予：店铺维度导入成功才把 6 个能力词并入 bi.shops.capabilities
   （保留既有词），pdd 平台两层守卫（登记面排除 + 授予函数硬拒）；
5) 台账：拒绝也留一行 manual_uploads + received/rejected 事件（含稳定原因码
   与结构化 detail），shops 空、不落事实、不写覆盖；
6) CLI：SYCM_MANUAL_IMPORT_ENABLED 缺省/false 拒绝执行（稳定码），true 才
   工作；目录展开、逐文件 JSON 摘要。

测试全部在外层回滚事务内进行（connect_test_db 协议），共享测试库零残留；
无测试 DSN 时显式 skip——skip 不是通过证明。真实导出数据不进数据库。
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import tempfile
import unittest
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import psycopg

from bi_agent.sycm_import import (
    MANUAL_QUALITY_RULE,
    SYCM_MANUAL_CAPABILITIES,
    SYCM_MANUAL_SOURCE,
    import_sycm_file,
    import_sycm_paths,
    main,
    manual_import_enabled,
)

from .dbfixtures import connect_test_db
from .test_sycm_parser import (
    CUSTOMER_HEADER,
    ITEM_HEADER,
    SHOP_HEADER,
    build_xlsx,
    crc_flip_first_member_byte,
    customer_rows,
    shop_rows,
)

BEIJING = ZoneInfo("Asia/Shanghai")

SHOP_ID = "M2-A"
SHOP_NAME = "M2测试淘店"
PDD_SHOP_ID = "M2-PDD"
PDD_SHOP_NAME = "M2拼多店"
DAY = date(2026, 9, 30)


def _day_start(day: date) -> datetime:
    return datetime.combine(day, time.min, tzinfo=BEIJING)


@unittest.skipUnless(os.getenv("BI_TEST_ADMIN_DSN"), "未配置独立测试数据库")
class SycmManualImportTests(unittest.TestCase):

    def setUp(self):
        self.conn = connect_test_db(self)
        tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(tmp.name)
        self.addCleanup(tmp.cleanup)

    # -- 夹具 -----------------------------------------------------------------

    def _shop(self, shop_id: str = SHOP_ID, display_name: str = SHOP_NAME,
              platform: str = "tb") -> None:
        self.conn.execute(
            "INSERT INTO bi.shops(shop_id, platform, display_name, capabilities) "
            "VALUES (%s, %s, %s, '{}') ON CONFLICT (shop_id) DO UPDATE SET "
            "platform = EXCLUDED.platform, display_name = EXCLUDED.display_name, "
            "capabilities = '{}'", (shop_id, platform, display_name))

    def _file(self, name: str, rows: list[list], **kwargs) -> Path:
        path = self.tmp / name
        path.write_bytes(build_xlsx(rows, **kwargs))
        return path

    def _shop_file(self, name: str = "淘店-M2测试淘店-店铺维度_20261001_x.xlsx",
                   **row_kwargs) -> Path:
        return self._file(name, shop_rows(1, shop=SHOP_NAME, **row_kwargs))

    def _one(self, sql: str, params: tuple = ()) -> tuple | None:
        return self.conn.execute(sql, params).fetchone()

    def _events(self, upload_id) -> list[tuple[str, dict]]:
        return [(row[0], row[1]) for row in self.conn.execute(
            "SELECT event_type, payload FROM bi.manual_upload_events "
            "WHERE upload_id = %s ORDER BY created_at, id", (upload_id,))]

    def _upload_row(self, filename: str) -> tuple | None:
        return self._one(
            "SELECT id, kind, status, reason_code, rows_read, rows_written, "
            "shops, content_sha256, completed_at FROM bi.manual_uploads "
            "WHERE filename = %s", (filename,))

    # -- 1) 事实与台账 ---------------------------------------------------------

    def test_shop_import_writes_facts_ledger_coverage_capabilities(self):
        self._shop()
        path = self._shop_file()
        outcome = import_sycm_file(self.conn, path)

        self.assertEqual(outcome.status, "accepted")
        self.assertIsNone(outcome.reason_code)
        self.assertEqual(outcome.shops, (SHOP_ID,))
        self.assertEqual(outcome.rows_written, outcome.rows_read, 1)
        self.assertEqual(outcome.granted_capabilities,
                         tuple(sorted(SYCM_MANUAL_CAPABILITIES)))

        fact = self._one(
            "SELECT paid_amount_sycm, pay_conversion_rate, visitors, cart_count, "
            "batch_id, recorded_at FROM bi.sycm_shop_daily "
            "WHERE shop_id = %s AND stat_date = %s", (SHOP_ID, DAY))
        self.assertIsNotNone(fact, "事实行必须落库")
        self.assertEqual(fact[0], Decimal("4693.78"), "千分位清洗后入库")
        self.assertEqual(fact[1], Decimal("0.0014"), "比率 ÷100 存分数")
        self.assertEqual(fact[2], 100)
        self.assertEqual(fact[3], 31)
        import hashlib
        expected_sha = hashlib.sha256(path.read_bytes()).hexdigest()
        self.assertEqual(fact[4], expected_sha, "batch_id = 文件内容哈希")
        self.assertIsNotNone(fact[5])

        upload = self._upload_row(path.name)
        self.assertIsNotNone(upload, "台账必须有一行")
        self.assertEqual(str(outcome.upload_id), str(upload[0]),
                         "结果携带台账行 id（上传面契约字段）")
        self.assertEqual(upload[1], "shop")
        self.assertEqual(upload[2], "accepted")
        self.assertIsNone(upload[3])
        self.assertEqual(upload[4:7], (1, 1, [SHOP_ID]))
        self.assertEqual(upload[7], expected_sha)
        self.assertIsNotNone(upload[8], "accepted 必须有 completed_at")
        events = self._events(upload[0])
        self.assertEqual({name for name, _ in events},
                         {"received", "accepted"})
        received = next(payload for name, payload in events
                        if name == "received")
        self.assertEqual(received["content_sha256"], expected_sha)
        self.assertEqual(received["filename_sha256"],
                         hashlib.sha256(path.name.encode("utf-8")).hexdigest())
        self.assertNotEqual(received["filename_sha256"], expected_sha,
                            "文件名哈希只作参考，绝不顶替内容哈希")

    def test_capability_merge_preserves_existing_words(self):
        self._shop()
        self.conn.execute("UPDATE bi.shops SET capabilities = %s "
                          "WHERE shop_id = %s", (["aov", "quantity"], SHOP_ID))
        outcome = import_sycm_file(self.conn, self._shop_file())
        expected = sorted({"aov", "quantity", *SYCM_MANUAL_CAPABILITIES})
        # B2：granted_capabilities 只含**本次新增**授予的词；aov/quantity 是
        # 店铺既有词，不是这次导入授的。
        self.assertEqual(outcome.granted_capabilities,
                         tuple(sorted(SYCM_MANUAL_CAPABILITIES)))
        stored = self._one("SELECT capabilities FROM bi.shops "
                           "WHERE shop_id = %s", (SHOP_ID,))[0]
        self.assertEqual(list(stored), expected, "库内仍是并入后的完整词表")

    # -- 2) 幂等与后批覆盖 ------------------------------------------------------

    def test_reimporting_the_same_file_is_idempotent(self):
        self._shop()
        path = self._shop_file()
        first = import_sycm_file(self.conn, path)
        second = import_sycm_file(self.conn, path)

        count = self._one("SELECT count(*) FROM bi.sycm_shop_daily "
                          "WHERE shop_id = %s", (SHOP_ID,))[0]
        self.assertEqual(count, 1, "同 (店,日) 只能有一行")
        fact = self._one("SELECT batch_id, paid_amount_sycm FROM bi.sycm_shop_daily "
                         "WHERE shop_id = %s", (SHOP_ID,))
        self.assertEqual(fact[0], first.content_sha256)
        self.assertEqual(fact[1], Decimal("4693.78"))

        state = self._one("SELECT covered FROM bi.sync_state WHERE source=%s "
                          "AND entity='shop_daily' AND shop_id=%s",
                          (SYCM_MANUAL_SOURCE, SHOP_ID))
        self.assertEqual(len(list(state[0])), 1, "重复区间归并，覆盖不膨胀")

        uploads = self.conn.execute(
            "SELECT count(*) FROM bi.manual_uploads WHERE filename = %s",
            (path.name,)).fetchone()[0]
        self.assertEqual(uploads, 2, "每次导入各留一行台账（追加历史）")

    def test_later_batch_overwrites_earlier_batch(self):
        self._shop()
        import_sycm_file(self.conn, self._shop_file("批A.xlsx"))
        # 后批含 {09-30, 09-29}（shop_rows 自 start 往回数）：重叠窗口整体被替换。
        later_rows = shop_rows(2, shop=SHOP_NAME, start=DAY.isoformat())
        later_rows[2][SHOP_HEADER.index("支付金额")] = "1,111.11"  # 09-29 行
        path_b = self._file("批B.xlsx", later_rows)
        outcome = import_sycm_file(self.conn, path_b)

        self.assertEqual(outcome.status, "accepted")
        for stat_date in (DAY, date(2026, 9, 29)):
            fact = self._one(
                "SELECT paid_amount_sycm, batch_id FROM bi.sycm_shop_daily "
                "WHERE shop_id=%s AND stat_date=%s", (SHOP_ID, stat_date))
            self.assertEqual(fact[1], outcome.content_sha256,
                             "重叠窗口必须整体被后批替换")
        day_row = self._one(
            "SELECT paid_amount_sycm FROM bi.sycm_shop_daily "
            "WHERE shop_id=%s AND stat_date=%s", (SHOP_ID, DAY))
        self.assertEqual(day_row[0], Decimal("4693.78"), "非重叠日按本批值")
        other_row = self._one(
            "SELECT paid_amount_sycm FROM bi.sycm_shop_daily "
            "WHERE shop_id=%s AND stat_date=%s", (SHOP_ID, date(2026, 9, 29)))
        self.assertEqual(other_row[0], Decimal("1111.11"))

    def test_coverage_accumulates_and_data_as_of_never_regresses(self):
        self._shop()
        import_sycm_file(self.conn, self._shop_file(
            "批A.xlsx", start="2026-09-29"))  # 覆盖 [09-29, 09-30)
        # 批B 覆盖 {09-30, 10-01} → 与批A 相邻，归并成 [09-29, 10-02)。
        import_sycm_file(self.conn, self._file(
            "批B.xlsx", shop_rows(2, shop=SHOP_NAME, start="2026-10-01")))

        state = self._one(
            "SELECT covered, data_as_of, quality_status, quality_rule "
            "FROM bi.sync_state WHERE source=%s AND entity='shop_daily' "
            "AND shop_id=%s", (SYCM_MANUAL_SOURCE, SHOP_ID))
        ranges = list(state[0])
        self.assertEqual(len(ranges), 1, "相邻日区间必须归并成一段")
        self.assertEqual(ranges[0].lower, _day_start(date(2026, 9, 29)))
        self.assertEqual(ranges[0].upper, _day_start(date(2026, 10, 2)))
        self.assertEqual(state[1], _day_start(date(2026, 10, 2)))
        self.assertEqual(state[2], "unknown")
        self.assertEqual(state[3], MANUAL_QUALITY_RULE)

        # 更早窗口后到：data_as_of 只进不退，covered 只增不减。
        import_sycm_file(self.conn, self._shop_file(
            "批C.xlsx", start="2026-09-15"))
        state = self._one(
            "SELECT covered, data_as_of FROM bi.sync_state "
            "WHERE source=%s AND entity='shop_daily' AND shop_id=%s",
            (SYCM_MANUAL_SOURCE, SHOP_ID))
        ranges = sorted(list(state[0]), key=lambda r: r.lower)
        self.assertEqual(len(ranges), 2, "不相邻窗口各成一段")
        self.assertEqual(ranges[0].lower, _day_start(date(2026, 9, 15)))
        self.assertEqual(state[1], _day_start(date(2026, 10, 2)),
                         "data_as_of 不回退")

    # -- 3) 客户维度 ------------------------------------------------------------

    def test_customer_import_writes_facts_but_neither_coverage_nor_capabilities(
            self):
        self._shop()
        rows = customer_rows(1, shop=SHOP_NAME)
        path = self._file("淘店-M2测试淘店-客户维度_20261001_x.xlsx", rows)
        outcome = import_sycm_file(self.conn, path)

        self.assertEqual(outcome.status, "accepted")
        self.assertEqual(outcome.kind, "customer")
        fact = self._one(
            "SELECT users, repeat_rate_180d, shop_fans, batch_id "
            "FROM bi.sycm_customer_daily WHERE shop_id=%s AND stat_date=%s",
            (SHOP_ID, DAY))
        self.assertEqual(fact[0], 13975)
        self.assertEqual(fact[1], Decimal("0.1576"))
        self.assertEqual(fact[2], 8)

        self.assertEqual(self._one(
            "SELECT count(*) FROM bi.sync_state WHERE source=%s AND shop_id=%s",
            (SYCM_MANUAL_SOURCE, SHOP_ID))[0], 0,
            "客户维度不写覆盖凭证（entity 只有 shop_daily）")
        self.assertEqual(self._one(
            "SELECT capabilities FROM bi.shops WHERE shop_id=%s",
            (SHOP_ID,))[0], [], "客户聚合不授予任何能力词")
        upload = self._upload_row(path.name)
        self.assertEqual(upload[1], "customer")
        self.assertEqual(upload[2], "accepted")

    # -- 4) pdd 硬守卫 ----------------------------------------------------------

    def test_pdd_shop_is_rejected_and_never_granted(self):
        self._shop(PDD_SHOP_ID, PDD_SHOP_NAME, platform="pdd")
        path = self._file("淘店-M2拼多店-店铺维度_20261001_x.xlsx",
                          shop_rows(1, shop=PDD_SHOP_NAME))
        outcome = import_sycm_file(self.conn, path)

        self.assertEqual(outcome.status, "rejected")
        self.assertEqual(outcome.reason_code, "shop_not_registered")
        self.assertEqual(outcome.shops, ())
        self.assertEqual(self._one(
            "SELECT count(*) FROM bi.sycm_shop_daily WHERE shop_id=%s",
            (PDD_SHOP_ID,))[0], 0, "pdd 店铺永不落 manual sycm 事实")
        self.assertEqual(self._one(
            "SELECT count(*) FROM bi.sync_state WHERE shop_id=%s",
            (PDD_SHOP_ID,))[0], 0)

        # 第二道独立守卫：即便有人直接调用授予函数，pdd 也拿不到任何词。
        from bi_agent.sycm_import import _grant_capabilities
        self.assertEqual(_grant_capabilities(self.conn, PDD_SHOP_ID), ())
        self.assertEqual(self._one(
            "SELECT capabilities FROM bi.shops WHERE shop_id=%s",
            (PDD_SHOP_ID,))[0], [])

    # -- 5) 拒绝台账 ------------------------------------------------------------

    def test_unregistered_shop_rejection_leaves_ledger_and_no_facts(self):
        self._shop()
        rows = shop_rows(2, shop="元发美特钉枪直销店")
        path = self._file("淘店-元发美特钉枪直销店-店铺维度_20261001_x.xlsx", rows)
        outcome = import_sycm_file(self.conn, path)

        self.assertEqual(outcome.status, "rejected")
        self.assertEqual(outcome.reason_code, "shop_not_registered")
        self.assertEqual(outcome.rows_read, 2)
        upload = self._upload_row(path.name)
        self.assertIsNotNone(upload, "拒绝也要留台账")
        self.assertEqual(upload[1], "shop", "维度可判时台账带 kind")
        self.assertEqual(upload[2], "rejected")
        self.assertEqual(upload[3], "shop_not_registered")
        self.assertEqual(upload[4], 2)
        self.assertEqual(upload[5], 0)
        self.assertEqual(upload[6], [], "未映射店铺不留任何 shop")
        events = self._events(upload[0])
        self.assertEqual({name for name, _ in events},
                         {"received", "rejected"})
        rejected_payload = next(payload for name, payload in events
                                if name == "rejected")
        self.assertEqual(rejected_payload["reason_code"], "shop_not_registered")
        self.assertEqual(rejected_payload["unknown_shops"],
                         ["元发美特钉枪直销店"])
        self.assertEqual(self._one(
            "SELECT count(*) FROM bi.sycm_shop_daily WHERE shop_id=%s",
            (SHOP_ID,))[0], 0, "整文件拒绝不落任何事实")
        # 覆盖凭证断言按本用例店铺限定（M6 主会话批准的范围修正，非放宽）：
        # 共享测试库可合法存在**其它店铺**的 manual 覆盖行——计划 §3.M6 的
        # 真实导入批次（docs/手动导出表格/ 12 文件）按批准提交并留库；全表
        # 计数=0 只是"表恰好为空"时代的偶然隔离形态。本断言语义仍是
        # "这次拒绝没有写下任何覆盖"：本店在 manual 来源下必须零行。
        self.assertEqual(self._one(
            "SELECT count(*) FROM bi.sync_state WHERE source=%s AND shop_id=%s",
            (SYCM_MANUAL_SOURCE, SHOP_ID))[0], 0,
            "拒绝不产生任何覆盖凭证")

    def test_item_file_rejection_leaves_ledger_with_unknown_kind(self):
        rows = [list(ITEM_HEADER),
                ["2026-09-30", SHOP_NAME, "123456789012", "商品", "90",
                 "1,234.56", "0.14%"]]
        path = self._file("淘店-M2测试淘店-商品维度_20261001_x.xlsx", rows)
        outcome = import_sycm_file(self.conn, path)

        self.assertEqual(outcome.status, "rejected")
        self.assertEqual(outcome.reason_code, "not_imported_this_round")
        upload = self._upload_row(path.name)
        self.assertIsNone(upload[1], "item 不是可导入 kind，台账 kind 为空")
        self.assertEqual(upload[3], "not_imported_this_round")

    # -- 5b) B1 欠账收口：容器损坏收敛 + 批次逐文件隔离 ---------------------------

    def test_corrupt_container_converges_to_parse_failed_with_ledger(self):
        """zip 成员 CRC 损坏/截断绝不以未捕获异常逃逸：收敛 parse_failed + 台账。"""
        self._shop()
        payload = build_xlsx(shop_rows(1, shop=SHOP_NAME))
        cases = {
            "crc": crc_flip_first_member_byte(payload),
            "truncated": payload[:-40],
        }
        for label, corrupted in cases.items():
            with self.subTest(sample=label):
                path = self.tmp / f"淘店-{SHOP_NAME}-店铺维度_20261001_{label}.xlsx"
                path.write_bytes(corrupted)
                outcome = import_sycm_file(self.conn, path)  # 不得抛异常
                self.assertEqual(outcome.status, "rejected")
                self.assertEqual(outcome.reason_code, "parse_failed")
                upload = self._upload_row(path.name)
                self.assertIsNotNone(upload, "损坏样本也留台账")
                self.assertEqual(upload[2], "rejected")
                self.assertEqual(upload[3], "parse_failed")
                events = self._events(upload[0])
                self.assertEqual({name for name, _ in events},
                                 {"received", "rejected"})
                self.assertEqual(self._one(
                    "SELECT count(*) FROM bi.sycm_shop_daily WHERE shop_id=%s",
                    (SHOP_ID,))[0], 0, "损坏样本不落任何事实")

    def test_batch_import_isolates_a_corrupt_file(self):
        """批内逐文件隔离：一个文件损坏不中止批次，其余文件照常导入。"""
        self._shop()
        self._shop_file("good.xlsx")
        corrupt = self.tmp / "corrupt.xlsx"
        corrupt.write_bytes(crc_flip_first_member_byte(
            build_xlsx(shop_rows(1, shop=SHOP_NAME))))

        outcomes = import_sycm_paths(self.conn, [self.tmp])
        self.assertEqual([o.filename for o in outcomes],
                         ["corrupt.xlsx", "good.xlsx"])
        by_name = {o.filename: o for o in outcomes}
        self.assertEqual(by_name["corrupt.xlsx"].status, "rejected")
        self.assertEqual(by_name["corrupt.xlsx"].reason_code, "parse_failed")
        self.assertEqual(by_name["good.xlsx"].status, "accepted",
                         "单文件失败不中止批次")

    def test_entity_vocabulary_migration_replays_idempotently(self):
        """029 开放 sync_state.entity='shop_daily'；重放两遍不报错、词表只增。"""
        path = Path(__file__).parents[1] / "sql" / "029_sycm_shop_daily_entity.sql"
        self.conn.execute(path.read_text(encoding="utf-8"))
        self.conn.execute(path.read_text(encoding="utf-8"))  # 幂等重放
        constraint = self._one(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE conname = 'sync_state_entity'")[0]
        self.assertIn("shop_daily", constraint)
        for word in ("orders", "orders_consign", "aftersales_occurrence",
                     "aftersales_cohort", "session"):
            self.assertIn(word, constraint, "既有词表不得被 029 收窄")
        # 反向突变：词表外的实体仍被 CHECK 拒。
        with self.assertRaises(psycopg.errors.CheckViolation), \
                self.conn.transaction():
            self.conn.execute(
                "INSERT INTO bi.sync_state(source, entity, shop_id) "
                "VALUES ('manual.sycm.export', 'not_a_real_entity', %s)",
                (SHOP_ID,))

    # -- 6) 批量与 CLI ----------------------------------------------------------

    def test_directory_import_expands_sorted_xlsx_only(self):
        self._shop()
        other = "M2-B店"
        self._shop("M2-B", other)
        self._file("a.xlsx", shop_rows(1, shop=SHOP_NAME))
        self._file("b.xlsx", customer_rows(1, shop=other))
        (self.tmp / "notes.txt").write_text("not an export", encoding="utf-8")
        (self.tmp / "c.xlsx.bak").write_bytes(b"junk")

        outcomes = import_sycm_paths(self.conn, [self.tmp])
        self.assertEqual([o.filename for o in outcomes], ["a.xlsx", "b.xlsx"])
        self.assertTrue(all(o.status == "accepted" for o in outcomes))

        # 单文件入参同样直达（不经目录展开）。
        single = import_sycm_paths(self.conn, [self.tmp / "a.xlsx"])
        self.assertEqual([o.filename for o in single], ["a.xlsx"])
        self.assertEqual(single[0].status, "accepted")

    def test_cli_disabled_by_default_with_stable_code(self):
        out, err = io.StringIO(), io.StringIO()
        code = main(argv=[str(self.tmp)], env={}, out=out, err=err,
                    connect=lambda dsn: self.conn)
        self.assertEqual(code, 2)
        self.assertIn("SYCM_MANUAL_IMPORT_DISABLED", err.getvalue())
        self.assertIn("SYCM_MANUAL_IMPORT_ENABLED", err.getvalue())
        self.assertEqual(out.getvalue(), "")

    def test_cli_rejects_loose_boolean_flags(self):
        for bad in ("1", "yes", "True", "TRUE"):
            with self.subTest(value=bad):
                out, err = io.StringIO(), io.StringIO()
                code = main(argv=[str(self.tmp)],
                            env={"SYCM_MANUAL_IMPORT_ENABLED": bad},
                            out=out, err=err, connect=lambda dsn: self.conn)
                self.assertEqual(code, 2)
                self.assertIn("SYCM_MANUAL_IMPORT_BAD_FLAG", err.getvalue())

    def test_manual_import_enabled_semantics(self):
        self.assertFalse(manual_import_enabled({}))
        self.assertFalse(manual_import_enabled({"SYCM_MANUAL_IMPORT_ENABLED": ""}))
        self.assertFalse(manual_import_enabled({"SYCM_MANUAL_IMPORT_ENABLED": "false"}))
        self.assertTrue(manual_import_enabled({"SYCM_MANUAL_IMPORT_ENABLED": "true"}))
        with self.assertRaises(ValueError):
            manual_import_enabled({"SYCM_MANUAL_IMPORT_ENABLED": "ok"})

    def test_cli_end_to_end_with_injected_connection(self):
        self._shop()
        self._shop_file("淘店-M2测试淘店-店铺维度_20261001_ok.xlsx")
        rows = shop_rows(1, shop="未登记店铺")
        self._file("淘店-未登记店铺-店铺维度_20261001_bad.xlsx", rows)

        out, err = io.StringIO(), io.StringIO()
        code = main(
            argv=[str(self.tmp)],
            env={"SYCM_MANUAL_IMPORT_ENABLED": "true", "BI_WRITER_DSN": "unused"},
            connect=lambda dsn: self.conn, out=out, err=err)
        # B1 退出码契约：存在失败（含带码拒绝）⇒ 1，且无未捕获 traceback。
        self.assertEqual(code, 1, err.getvalue())
        self.assertNotIn("Traceback", err.getvalue())
        records = [json.loads(line) for line in out.getvalue().splitlines()]
        self.assertEqual(
            {(r["filename"], r["status"]) for r in records},
            {("淘店-M2测试淘店-店铺维度_20261001_ok.xlsx", "accepted"),
             ("淘店-未登记店铺-店铺维度_20261001_bad.xlsx", "rejected")})
        accepted = next(r for r in records if r["status"] == "accepted")
        self.assertEqual(accepted["rows_written"], 1)
        self.assertEqual(accepted["shops"], [SHOP_ID])
        self.assertEqual(sorted(accepted["granted_capabilities"]),
                         sorted(SYCM_MANUAL_CAPABILITIES))
        self.assertIsNotNone(accepted["upload_id"], "台账行 id 随结果返回")
        rejected = next(r for r in records if r["status"] == "rejected")
        self.assertEqual(rejected["reason_code"], "shop_not_registered")
        self.assertEqual(rejected["detail"]["unknown_shops"], ["未登记店铺"])

    def test_cli_exit_code_is_zero_only_when_every_file_is_accepted(self):
        self._shop()
        self._shop_file("all-good.xlsx")
        out, err = io.StringIO(), io.StringIO()
        code = main(
            argv=[str(self.tmp)],
            env={"SYCM_MANUAL_IMPORT_ENABLED": "true", "BI_WRITER_DSN": "unused"},
            connect=lambda dsn: self.conn, out=out, err=err)
        self.assertEqual(code, 0, err.getvalue())

    def test_cli_owned_connection_persists_after_close(self):
        """CLI 自持连接必须真提交（M6 验收批次实测发现的 P1 收口）。

        复现：默认（非 autocommit）连接上，首条语句是店铺登记读取
        （`eligible_shop_mapping` 的 SELECT），它开启隐式事务；之后每个
        逐文件 `conn.transaction()` 都被降级成 SAVEPOINT（transaction.py 按
        连接是否 IDLE 判内外层），`conn.close()` 把未提交的外层事务整个丢弃
        ——CLI 打印 accepted、退出码 0，却一条事实/台账都不落。
        既有用例全部注入外层回滚事务内的连接，看不见这个洞。

        契约：CLI 自持连接用 autocommit（与上传面 get_conn 同一语义），
        逐文件事务块因此是真正的 BEGIN/COMMIT；本测试用独立连接核对
        持久化并全量清理（共享测试库零残留）。
        """
        dsn = os.environ["BI_TEST_ADMIN_DSN"]
        shop_id, shop_name = "M2-PERSIST", "M2持久化淘店"
        filename = f"淘店-{shop_name}-店铺维度_20261001_persist.xlsx"
        self._file(filename, shop_rows(1, shop=shop_name))
        # 店铺行必须先提交：CLI 的独立连接看不见本测试外层事务里的未提交行。
        seed = psycopg.connect(dsn)
        try:
            seed.execute(
                "INSERT INTO bi.shops(shop_id, platform, display_name, "
                "capabilities) VALUES (%s, 'tb', %s, '{}') "
                "ON CONFLICT (shop_id) DO UPDATE SET capabilities = '{}'",
                (shop_id, shop_name))
            seed.commit()
        finally:
            seed.close()
        try:
            out, err = io.StringIO(), io.StringIO()
            code = main(argv=[str(self.tmp / filename)],
                        env={"SYCM_MANUAL_IMPORT_ENABLED": "true",
                             "BI_WRITER_DSN": dsn},
                        out=out, err=err)  # connect=None：CLI 自持连接生命周期
            self.assertEqual(code, 0, err.getvalue())
            records = [json.loads(line) for line in out.getvalue().splitlines()]
            self.assertEqual(records[0]["status"], "accepted")
            self.assertEqual(records[0]["shops"], [shop_id])
            verify = psycopg.connect(dsn)
            try:
                facts = verify.execute(
                    "SELECT count(*) FROM bi.sycm_shop_daily "
                    "WHERE shop_id = %s", (shop_id,)).fetchone()[0]
                ledger = verify.execute(
                    "SELECT count(*) FROM bi.manual_uploads "
                    "WHERE filename = %s", (filename,)).fetchone()[0]
                coverage = verify.execute(
                    "SELECT count(*) FROM bi.sync_state "
                    "WHERE source = %s AND shop_id = %s",
                    (SYCM_MANUAL_SOURCE, shop_id)).fetchone()[0]
            finally:
                verify.close()
            self.assertEqual(facts, 1, "事实必须在 CLI 连接 close 后仍在")
            self.assertEqual(ledger, 1, "台账必须真实落库")
            self.assertEqual(coverage, 1, "覆盖凭证必须真实落库")
        finally:
            cleanup = psycopg.connect(dsn)
            try:
                # ON DELETE CASCADE 清 sycm 事实；台账删除级联事件。
                cleanup.execute("DELETE FROM bi.shops WHERE shop_id = %s",
                                (shop_id,))
                cleanup.execute("DELETE FROM bi.manual_uploads "
                                "WHERE filename = %s", (filename,))
                cleanup.execute(
                    "DELETE FROM bi.sync_state WHERE source = %s "
                    "AND shop_id = %s", (SYCM_MANUAL_SOURCE, shop_id))
                cleanup.commit()
            finally:
                cleanup.close()


    def test_cli_corrupt_file_is_isolated_exits_one_without_traceback(self):
        self._shop()
        self._shop_file("good.xlsx")
        corrupt = self.tmp / "corrupt.xlsx"
        corrupt.write_bytes(crc_flip_first_member_byte(
            build_xlsx(shop_rows(1, shop=SHOP_NAME))))

        out, err = io.StringIO(), io.StringIO()
        code = main(
            argv=[str(self.tmp)],
            env={"SYCM_MANUAL_IMPORT_ENABLED": "true", "BI_WRITER_DSN": "unused"},
            connect=lambda dsn: self.conn, out=out, err=err)
        self.assertEqual(code, 1)
        self.assertNotIn("Traceback", err.getvalue(), "无未捕获 traceback")
        records = {r["filename"]: r
                   for r in map(json.loads, out.getvalue().splitlines())}
        self.assertEqual(records["corrupt.xlsx"]["status"], "rejected")
        self.assertEqual(records["corrupt.xlsx"]["reason_code"], "parse_failed")
        self.assertEqual(records["good.xlsx"]["status"], "accepted")

    def test_cli_missing_path_fails_with_stable_code_not_traceback(self):
        out, err = io.StringIO(), io.StringIO()
        code = main(
            argv=[str(self.tmp / "不存在的目录")],
            env={"SYCM_MANUAL_IMPORT_ENABLED": "true", "BI_WRITER_DSN": "unused"},
            connect=lambda dsn: self.conn, out=out, err=err)
        self.assertEqual(code, 2)
        self.assertNotIn("Traceback", err.getvalue())
        self.assertIn("SYCM_IMPORT_PATH_NOT_FOUND", err.getvalue())


if __name__ == "__main__":
    unittest.main()
