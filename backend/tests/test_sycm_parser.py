"""生意参谋手工导出·解析器与校验管线（计划 §3.M2，纯离线单元）。

锁六件事：
1) 标准库 xlsx 读取器的五道防护（文件 ≤5MB、解压总量 ≤64MB、行 ≤20 万、
   列 ≤256、sheet ≤16）真的会拒——每道上限都有恰内/恰外双向边界样本，
   上限值本身也被断言钉住（反向突变），不是恒真断言；
2) 校验管线原因码精确等于集中定义的枚举值（断言结构化字段，不做子串搜索）；
3) 店铺映射只认数据列「店铺名称」与登记表精确相等：文件名不可信、未映射
   整文件拒绝、多店铺整文件拒绝；
4) 商品维度文件本轮一律拒（not_imported_this_round）；出现个体级明细列名
   整文件拒（pii_columns_rejected）；
5) 数值清洗：千分位、比率 % ÷100 存分数、推广花费空值按 0 并披露计数、
   不启用列（包裹/物流）与白名单外聚合列不读不拒；
6) docs/手动导出表格/ 真实文件只读断言：表头覆盖 028 白名单、4 店分类与
   计划一致（2 店入库 61 天、2 店 shop_not_registered、商品维度全拒）。
   真实数据不写任何数据库。

xlsx 夹具用标准库 zipfile 现场构造（inlineStr 单元格，与真实导出同构），
供 test_sycm_import 复用。
"""

from __future__ import annotations

import hashlib
import io
import os
import tempfile
import unittest
import zipfile
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from xml.sax.saxutils import escape

import bi_agent.sycm_import as sycm_module
from bi_agent.sycm_import import (
    MAX_COLUMNS,
    MAX_FILE_BYTES,
    MAX_ROWS,
    MAX_SHEETS,
    MAX_UNCOMPRESSED_BYTES,
    FileKind,
    ReasonCode,
    SycmValidationError,
    parse_sycm_file,
    read_sycm_sheet,
)

# 合成登记表：display_name -> shop_id。真实导入器由 bi.shops 派生同一形状。
REGISTERED = {"测试淘店": "166517", "沃金五金机电商城": "166647"}

SHOP_HEADER = (
    ["统计日期", "店铺名称"]
    + ["访客数", "商品访客数", "浏览量", "支付金额", "支付买家数", "支付件数",
       "下单金额", "成功退款金额", "全站推广花费", "关键词推广花费",
       "精准人群推广花费", "智能场景花费", "淘宝客佣金", "支付转化率", "客单价",
       "加购人数", "商品收藏买家数"]
    # 本轮不启用的包裹/物流列：读出后必须被忽略，绝不因此拒绝或落库。
    + ["揽收包裹数", "发货包裹数", "派送包裹数", "签收成功包裹数"]
)
# SHOP_HEADER 里各指标列的下标（0 基）：身份 2 列 + 8 个流量交易列 + 推广 5 列
# （10..14）+ 转化率/客单价/加购/收藏 + 包裹 4 列。
SPEND_COLUMN_INDEXES = range(10, 15)
CUSTOMER_HEADER = (
    ["统计日期", "店铺名称"]
    + ["用户数", "已购用户数", "新访客户数", "父订单数", "复购父订单数",
       "180天复购率", "店铺粉丝数", "店铺会员数"]
    # 白名单外的日聚合列：不读、不拒（028 只登记 8 列）。
    + ["未购买用户数", "总粉丝数"]
)
ITEM_HEADER = ["统计日期", "店铺名称", "商品ID", "商品名称", "商品访客数",
               "支付金额", "支付转化率"]

SHOP_ROW_FIELDS = {"stat_date", "shop_name", "visitors", "item_visitors", "views",
                   "paid_amount_sycm", "paid_buyers", "paid_items", "order_amount",
                   "refund_amount", "ad_spend_all", "ad_spend_keyword",
                   "ad_spend_crowd", "ad_spend_smart", "ke_commission",
                   "pay_conversion_rate", "aov", "cart_count", "favorite_count"}
CUSTOMER_ROW_FIELDS = {"stat_date", "shop_name", "users", "paid_users",
                       "new_customers", "parent_orders", "repeat_parent_orders",
                       "repeat_rate_180d", "shop_fans", "shop_members"}


# ---------------------------------------------------------------------------
# 合成 xlsx 夹具（标准库现场构造；单元格与真实导出同为 inlineStr）
# ---------------------------------------------------------------------------

def _column_ref(index: int, row_number: int) -> str:
    letters = ""
    n = index
    while True:
        letters = chr(ord("A") + n % 26) + letters
        n = n // 26 - 1
        if n < 0:
            break
    return f"{letters}{row_number}"


def _cell_xml(index: int, row_number: int, value, numeric: bool) -> str:
    ref = _column_ref(index, row_number)
    if value is None:
        return f'<c r="{ref}"/>'
    if numeric:
        return f'<c r="{ref}"><v>{escape(str(value))}</v></c>'
    return (f'<c r="{ref}" t="inlineStr">'
            f"<is><t>{escape(str(value))}</t></is></c>")


def build_xlsx(rows: list[list], *, numeric_cols: frozenset[int] = frozenset(),
               sheets: int = 1, extra_members: dict[str, bytes] | None = None,
               raw_sheet: bytes | None = None) -> bytes:
    """构造最小合法 xlsx：rows[0] 是表头，其余是数据行。"""
    sheet_members: list[tuple[str, bytes]] = []
    for sheet_i in range(1, sheets + 1):
        if raw_sheet is not None and sheet_i == 1:
            xml = raw_sheet
        else:
            body = []
            for row_number, row in enumerate(rows, start=1):
                cells = "".join(
                    _cell_xml(i, row_number, value, i in numeric_cols)
                    for i, value in enumerate(row))
                body.append(f'<row r="{row_number}">{cells}</row>')
            xml = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                   '<worksheet xmlns="http://schemas.openxmlformats.org/'
                   f'spreadsheetml/2006/main"><sheetData>{"".join(body)}'
                   "</sheetData></worksheet>").encode("utf-8")
        sheet_members.append((f"xl/worksheets/sheet{sheet_i}.xml", xml))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml",
                   '<?xml version="1.0"?><Types xmlns="http://schemas.'
                   'openxmlformats.org/package/2006/content-types"/>')
        z.writestr("_rels/.rels", '<?xml version="1.0"?><Relationships '
                   'xmlns="http://schemas.openxmlformats.org/package/2006/'
                   'relationships"/>')
        z.writestr("xl/workbook.xml", '<?xml version="1.0"?><workbook xmlns='
                   '"http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
                   "/>")
        for name, xml in sheet_members:
            z.writestr(name, xml)
        for name, data in (extra_members or {}).items():
            z.writestr(name, data)
    return buf.getvalue()


def shop_rows(count: int = 1, *, shop: str = "测试淘店",
              start: str = "2026-09-30") -> list[list]:
    rows: list[list] = [list(SHOP_HEADER)]
    base = date.fromisoformat(start)
    for i in range(count):
        day = base - timedelta(days=i)
        rows.append([
            day.isoformat(), shop,
            "100", "90", "120",          # 访客数/商品访客数/浏览量
            "4,693.78", "20", "43",      # 支付金额/支付买家数/支付件数
            "7,449.10", "518.00",        # 下单金额/成功退款金额
            "377.44", "106.14", "48.53", "0", "0",   # 推广 5 列
            "0.14%", "234.69", "31", "19",           # 转化率/客单价/加购/收藏
            "0", "0", "0", "0",          # 包裹 4 列（不启用）
        ])
    return rows


def customer_rows(count: int = 1, *, shop: str = "测试淘店",
                  start: str = "2026-09-30") -> list[list]:
    rows: list[list] = [list(CUSTOMER_HEADER)]
    base = date.fromisoformat(start)
    for i in range(count):
        day = base - timedelta(days=i)
        rows.append([
            day.isoformat(), shop,
            "13,975", "20", "11,870", "24", "11",    # 用户数..复购父订单数
            "15.76%", "8", "0",                      # 180天复购率/粉丝/会员
            "999", "1",                              # 白名单外聚合列
        ])
    return rows


def write_fixture(path: Path, rows: list[list], **kwargs) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(build_xlsx(rows, **kwargs))
    return path


def crc_flip_first_member_byte(data: bytes) -> bytes:
    """真实损坏样本（反向突变语料）：翻转工作表成员压缩数据的首字节。

    翻转落在 deflate 流内：读取该成员时 CRC/解压层必然爆掉（BadZipFile 或
    zlib.error）——这正是 B1 要求收敛为 parse_failed 的逃逸面。
    """
    payload = bytearray(data)
    with zipfile.ZipFile(io.BytesIO(bytes(payload))) as archive:
        info = next(i for i in archive.infolist()
                    if i.filename.startswith("xl/worksheets/"))
        offset = info.header_offset + 30 + len(info.filename)
    payload[offset] ^= 0xFF
    return bytes(payload)


def parse_bytes(payload: bytes, *, registered=REGISTERED,
                filename: str = "淘店-测试淘店-店铺维度_20261001_x.xlsx"):
    """把任意字节样本送进校验管线（损坏样本经临时文件走真实读取路径）。"""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / filename
        path.write_bytes(payload)
        return parse_sycm_file(path, registered_shops=registered)


def parse(rows: list[list], *, registered=REGISTERED, **kwargs):
    with tempfile.TemporaryDirectory() as tmp:
        path = write_fixture(Path(tmp) / "淘店-测试淘店-店铺维度_20261001_x.xlsx",
                             rows, **kwargs)
        return parse_sycm_file(path, registered_shops=registered)


def expect_rejection(case: unittest.TestCase, rows: list[list],
                     code: ReasonCode, **kwargs) -> SycmValidationError:
    try:
        parse(rows, **kwargs)
    except SycmValidationError as error:
        case.assertEqual(
            error.reason_code, code,
            f"期望 {code}，实际 {error.reason_code}（detail={error.detail}）")
        return error
    case.fail(f"期望被拒 {code}，实际解析成功")
    raise AssertionError("unreachable")  # pragma: no cover


# ---------------------------------------------------------------------------
# 正常路径与数值清洗
# ---------------------------------------------------------------------------

class ShopDimensionParsingTests(unittest.TestCase):

    def test_shop_file_parses_rows_with_cleaned_values(self):
        result = parse(shop_rows(2))
        self.assertEqual(result.kind, FileKind.SHOP)
        self.assertEqual(result.shop_id, "166517")
        self.assertEqual(result.shop_name, "测试淘店")
        self.assertEqual(len(result.rows), 2)
        first = result.rows[0]
        self.assertEqual(first.stat_date, date(2026, 9, 30))
        self.assertEqual(first.visitors, 100)
        self.assertIsInstance(first.visitors, int, "计数列必须是整数")
        self.assertEqual(first.paid_amount_sycm, Decimal("4693.78"),
                         "千分位必须被清洗")
        self.assertEqual(first.order_amount, Decimal("7449.10"))
        self.assertEqual(first.refund_amount, Decimal("518.00"))
        self.assertEqual(first.ad_spend_all, Decimal("377.44"))
        self.assertEqual(first.pay_conversion_rate, Decimal("0.0014"),
                         "导出是百分数，必须 ÷100 存分数")
        self.assertEqual(first.cart_count, 31)
        self.assertEqual(first.favorite_count, 19)

    def test_row_fields_are_exactly_the_whitelist(self):
        """解析产物字段精确等于 028 白名单：多一个字段都是口径/PII 泄漏开口。"""
        self.assertEqual(set(parse(shop_rows(1)).rows[0].__dataclass_fields__),
                         SHOP_ROW_FIELDS)
        self.assertEqual(
            set(parse(customer_rows(1)).rows[0].__dataclass_fields__),
            CUSTOMER_ROW_FIELDS)

    def test_zero_spend_defaults_are_disclosed(self):
        rows = shop_rows(1)
        # 推广 5 列（全站/关键词/人群/智能/淘宝客）全部置空：按 0 处理并计数披露。
        for offset in SPEND_COLUMN_INDEXES:
            rows[1][offset] = None
        result = parse(rows)
        first = result.rows[0]
        self.assertEqual(first.ad_spend_all, Decimal("0"))
        self.assertEqual(first.ad_spend_keyword, Decimal("0"))
        self.assertEqual(first.ad_spend_crowd, Decimal("0"))
        self.assertEqual(first.ad_spend_smart, Decimal("0"))
        self.assertEqual(first.ke_commission, Decimal("0"))
        self.assertEqual(result.zero_spend_cells, 5,
                         "空推广花费必须按 0 写入并披露计数")
        self.assertEqual(first.refund_amount, Decimal("518.00"),
                         "置空只落在推广 5 列，不殃及相邻列")

    def test_literal_null_cells_count_as_empty_spend(self):
        """真实导出用字面量 NULL 标记缺失（实测只在推广花费列）：按 0+披露。"""
        rows = shop_rows(1)
        rows[1][SHOP_HEADER.index("全站推广花费")] = "NULL"
        result = parse(rows)
        self.assertEqual(result.rows[0].ad_spend_all, Decimal("0"))
        self.assertEqual(result.zero_spend_cells, 1)
        # 非花费列的 NULL 同样是缺值记号：存 NULL 不补零。
        rows = shop_rows(1)
        rows[1][SHOP_HEADER.index("访客数")] = "NULL"
        result = parse(rows)
        self.assertIsNone(result.rows[0].visitors)
        self.assertEqual(result.zero_spend_cells, 0)

    def test_non_spend_empty_cells_stay_none(self):
        rows = shop_rows(1)
        rows[1][SHOP_HEADER.index("访客数")] = None
        rows[1][SHOP_HEADER.index("支付金额")] = None
        result = parse(rows)
        self.assertIsNone(result.rows[0].visitors, "缺值存 NULL 不补零")
        self.assertIsNone(result.rows[0].paid_amount_sycm)
        self.assertEqual(result.zero_spend_cells, 0)

    def test_numeric_typed_cells_also_parse(self):
        """无 t 属性的数字单元格（<v>原文含千分位）与字符串单元格同权清洗。"""
        rows = shop_rows(1)
        result = parse(rows, numeric_cols=frozenset(range(2, 19)))
        self.assertEqual(result.rows[0].visitors, 100)
        self.assertEqual(result.rows[0].paid_amount_sycm, Decimal("4693.78"))

    def test_blank_data_rows_are_skipped(self):
        rows = [list(SHOP_HEADER), [None] * len(SHOP_HEADER)] + shop_rows(1)[1:]
        result = parse(rows)
        self.assertEqual(len(result.rows), 1, "表头一行、全空数据行跳过")

    def test_content_sha256_is_of_file_bytes(self):
        rows = shop_rows(1)
        payload = build_xlsx(rows)
        result = parse(rows)
        self.assertEqual(result.content_sha256,
                         hashlib.sha256(payload).hexdigest())
        self.assertNotEqual(
            result.content_sha256,
            hashlib.sha256("淘店-测试淘店-店铺维度.xlsx".encode("utf-8")).hexdigest(),
            "批次是内容哈希，文件名哈希只作参考")


class CustomerDimensionParsingTests(unittest.TestCase):

    def test_customer_file_parses_whitelisted_aggregates(self):
        result = parse(customer_rows(1))
        self.assertEqual(result.kind, FileKind.CUSTOMER)
        self.assertEqual(len(result.rows), 1)
        first = result.rows[0]
        self.assertEqual(first.users, 13975)
        self.assertEqual(first.paid_users, 20)
        self.assertEqual(first.new_customers, 11870)
        self.assertEqual(first.parent_orders, 24)
        self.assertEqual(first.repeat_parent_orders, 11)
        self.assertEqual(first.repeat_rate_180d, Decimal("0.1576"),
                         "复购率同样 ÷100 存分数")
        self.assertEqual(first.shop_fans, 8)
        self.assertEqual(first.shop_members, 0)

    def test_customer_file_with_individual_level_column_is_rejected(self):
        rows = customer_rows(1)
        rows[0] = rows[0] + ["买家昵称"]
        rows[1] = rows[1] + ["张三"]
        error = expect_rejection(self, rows, ReasonCode.PII_COLUMNS_REJECTED)
        self.assertIn("买家昵称", error.detail.get("pii_columns", ()),
                      "detail 必须点名个体级明细列")

    def test_shop_file_with_individual_level_column_is_rejected_too(self):
        """PII 白名单不因维度而松动：店铺维度文件混入个体列同样整文件拒。"""
        rows = shop_rows(1)
        rows[0] = rows[0] + ["收货人姓名"]
        rows[1] = rows[1] + ["李四"]
        expect_rejection(self, rows, ReasonCode.PII_COLUMNS_REJECTED)


# ---------------------------------------------------------------------------
# 容器层损坏收敛（B1/M4 欠账收口：任何 zip/XML 层异常都收敛为 parse_failed）
# ---------------------------------------------------------------------------


class CorruptContainerConvergenceTests(unittest.TestCase):
    """B1 反向突变：zip 成员 CRC 损坏/截断/伪造单元引用不得以未捕获异常逃逸。"""

    def _expect_parse_failed(self, payload: bytes, *, label: str):
        with self.subTest(sample=label):
            with self.assertRaises(SycmValidationError) as caught:
                parse_bytes(payload)
            self.assertEqual(caught.exception.reason_code,
                             ReasonCode.PARSE_FAILED)
            self.assertTrue(caught.exception.detail,
                            "detail 必须结构化（进台账事件 payload）")

    def test_crc_corrupted_member_converges_to_parse_failed(self):
        self._expect_parse_failed(
            crc_flip_first_member_byte(build_xlsx(shop_rows(1))),
            label="crc_flip")

    def test_truncated_zip_converges_to_parse_failed(self):
        payload = build_xlsx(shop_rows(1))
        self._expect_parse_failed(payload[:-40], label="truncated_eocd")

    def test_non_zip_bytes_converge_to_parse_failed(self):
        self._expect_parse_failed(b"plain text, not a container",
                                  label="not_zip")

    def test_numeric_cell_reference_converges_to_parse_failed(self):
        """无字母前缀的单元引用（<c r="123">）是 XML 层损坏：收敛不逃逸。"""
        raw = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
               '<worksheet xmlns="http://schemas.openxmlformats.org/'
               'spreadsheetml/2006/main"><sheetData>'
               '<row r="1"><c r="123"><v>1</v></c></row>'
               "</sheetData></worksheet>").encode("utf-8")
        self._expect_parse_failed(build_xlsx([], raw_sheet=raw),
                                  label="numeric_ref")

    def test_shared_strings_out_of_bounds_converge_to_parse_failed(self):
        """sharedStrings 索引越界（IndexError）：同样收敛，绝不 traceback。"""
        raw = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
               '<worksheet xmlns="http://schemas.openxmlformats.org/'
               'spreadsheetml/2006/main"><sheetData>'
               '<row r="1"><c r="A1" t="s"><v>99999</v></c></row>'
               "</sheetData></worksheet>").encode("utf-8")
        self._expect_parse_failed(build_xlsx([], raw_sheet=raw),
                                  label="sst_out_of_bounds")


# ---------------------------------------------------------------------------
# 校验管线原因码
# ---------------------------------------------------------------------------

class ValidationReasonCodeTests(unittest.TestCase):

    def test_missing_required_metric_column(self):
        rows = shop_rows(1)
        index = SHOP_HEADER.index("全站推广花费")
        rows[0] = [name for name in rows[0] if name != "全站推广花费"]
        rows[1] = rows[1][:index] + rows[1][index + 1:]
        error = expect_rejection(self, rows, ReasonCode.MISSING_COLUMNS)
        self.assertIn("全站推广花费", error.detail.get("missing_columns", ()))

    def test_missing_identity_columns(self):
        rows = customer_rows(1)
        rows[0] = [name for name in rows[0] if name != "统计日期"]
        rows[1] = rows[1][1:]
        error = expect_rejection(self, rows, ReasonCode.MISSING_COLUMNS)
        self.assertIn("统计日期", error.detail.get("missing_columns", ()))

    def test_bad_date_format(self):
        rows = shop_rows(1)
        rows[1][0] = "2026/09/30"
        expect_rejection(self, rows, ReasonCode.BAD_DATE)

    def test_bad_date_garbage(self):
        rows = shop_rows(1)
        rows[1][0] = "not-a-date"
        expect_rejection(self, rows, ReasonCode.BAD_DATE)

    def test_bad_date_in_any_row_rejects_whole_file(self):
        rows = shop_rows(3)
        rows[2][0] = "2026-13-40"
        expect_rejection(self, rows, ReasonCode.BAD_DATE)

    def test_unregistered_shop_rejects_whole_file(self):
        rows = shop_rows(2, shop="元发美特钉枪直销店")
        error = expect_rejection(self, rows, ReasonCode.SHOP_NOT_REGISTERED)
        self.assertEqual(error.detail.get("unknown_shops"),
                         ("元发美特钉枪直销店",))

    def test_file_name_is_never_trusted_for_mapping(self):
        """文件名写「测试淘店」但数据列是未登记店：以数据列为准（拒）。"""
        rows = shop_rows(1, shop="未登记店铺")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "淘店-测试淘店-店铺维度_20261001_x.xlsx"
            path.write_bytes(build_xlsx(rows))
            try:
                parse_sycm_file(path, registered_shops=REGISTERED)
            except SycmValidationError as error:
                self.assertEqual(error.reason_code, ReasonCode.SHOP_NOT_REGISTERED)
            else:
                self.fail("文件名不能替数据列完成映射")

    def test_multiple_shop_names_reject_whole_file(self):
        rows = shop_rows(2)
        rows[2][1] = "沃金五金机电商城"
        expect_rejection(self, rows, ReasonCode.SHOP_NOT_REGISTERED)

    def test_empty_shop_name_in_a_row_rejects_whole_file(self):
        rows = shop_rows(2)
        rows[2][1] = None
        expect_rejection(self, rows, ReasonCode.SHOP_NOT_REGISTERED)

    def test_item_dimension_file_always_rejected_this_round(self):
        rows = [list(ITEM_HEADER),
                ["2026-09-30", "测试淘店", "123456789012", "测试商品",
                 "90", "1,234.56", "0.14%"]]
        expect_rejection(self, rows, ReasonCode.NOT_IMPORTED_THIS_ROUND)

    def test_garbage_bytes_are_parse_failed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "not-an-xlsx.xlsx"
            path.write_bytes(b"this is not a zip file" * 100)
            try:
                parse_sycm_file(path, registered_shops=REGISTERED)
            except SycmValidationError as error:
                self.assertEqual(error.reason_code, ReasonCode.PARSE_FAILED)
            else:
                self.fail("非 zip 输入必须 parse_failed")

    def test_corrupt_sheet_xml_is_parse_failed(self):
        expect_rejection(self, shop_rows(1), ReasonCode.PARSE_FAILED,
                         raw_sheet=b"<worksheet><sheetData><row")

    def test_numeric_junk_in_metric_column_is_parse_failed(self):
        rows = shop_rows(1)
        rows[1][SHOP_HEADER.index("支付金额")] = "abc"
        expect_rejection(self, rows, ReasonCode.PARSE_FAILED)

    def test_header_only_file_cannot_resolve_shop(self):
        expect_rejection(self, [list(SHOP_HEADER)],
                         ReasonCode.SHOP_NOT_REGISTERED)


# ---------------------------------------------------------------------------
# 读取器防护上限（上限值钉死 + 恰内/恰外双向边界，反向突变）
# ---------------------------------------------------------------------------

class ReaderGuardTests(unittest.TestCase):

    def test_guard_limits_have_the_planned_values(self):
        """上限值本身是契约（计划 §3.M2）：被调小/调大都必须被抓住。"""
        self.assertEqual(MAX_FILE_BYTES, 5 * 1024 * 1024)
        self.assertEqual(MAX_UNCOMPRESSED_BYTES, 64 * 1024 * 1024)
        self.assertEqual(MAX_ROWS, 200_000)
        self.assertEqual(MAX_COLUMNS, 256)
        self.assertEqual(MAX_SHEETS, 16)

    def test_oversized_file_is_rejected(self):
        rows = shop_rows(1)
        base = build_xlsx(rows)
        # urandom 不可压：保证落盘字节数真的越过 5MB（零填充会被 deflate 吃掉）。
        padding = os.urandom(MAX_FILE_BYTES - len(base) + 1)
        payload = build_xlsx(rows, extra_members={"xl/padding.bin": padding})
        self.assertGreater(len(payload), MAX_FILE_BYTES)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "big.xlsx"
            path.write_bytes(payload)
            try:
                parse_sycm_file(path, registered_shops=REGISTERED)
            except SycmValidationError as error:
                self.assertEqual(error.reason_code, ReasonCode.PARSE_FAILED)
            else:
                self.fail("超 5MB 的文件必须被拒")

    def test_file_at_exact_size_limit_still_parses(self):
        rows = shop_rows(1)
        base = build_xlsx(rows)
        # urandom 不可压；zip 成员头有固定开销，按实测长度收敛到恰 ≤5MB。
        padding_size = MAX_FILE_BYTES - len(base)
        for _ in range(8):
            payload = build_xlsx(rows, extra_members={
                "xl/padding.bin": os.urandom(padding_size)})
            if len(payload) <= MAX_FILE_BYTES:
                break
            padding_size -= len(payload) - MAX_FILE_BYTES
        self.assertLessEqual(len(payload), MAX_FILE_BYTES,
                             "恰在 5MB 内的合法文件必须仍可解析（反向突变）")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ok.xlsx"
            path.write_bytes(payload)
            result = parse_sycm_file(path, registered_shops=REGISTERED)
            self.assertEqual(len(result.rows), 1)

    def test_oversized_file_is_rejected_before_any_content_read(self):
        """复审 P1：体积防线必须先于内容读取/哈希——超大输入绝不进内存。"""
        rows = shop_rows(1)
        base = build_xlsx(rows)
        payload = build_xlsx(rows, extra_members={
            "xl/padding.bin": os.urandom(MAX_FILE_BYTES - len(base) + 1)})
        self.assertGreater(len(payload), MAX_FILE_BYTES)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "big.xlsx"
            path.write_bytes(payload)
            import unittest.mock as mock
            read_calls: list[Path] = []
            real_read_bytes = Path.read_bytes

            def counting_read_bytes(self):
                read_calls.append(self)
                return real_read_bytes(self)

            with mock.patch.object(Path, "read_bytes", counting_read_bytes):
                try:
                    parse_sycm_file(path, registered_shops=REGISTERED)
                except SycmValidationError as error:
                    self.assertEqual(error.reason_code, ReasonCode.PARSE_FAILED)
                else:
                    self.fail("超限文件必须被拒")
            self.assertEqual(read_calls, [],
                             "拒绝前不得整体读入文件内容（含内容哈希）")

    def test_ledger_sha256_streams_instead_of_materializing(self):
        """复审 P1：拒绝台账的哈希走流式分块，不依赖整体 read_bytes。"""
        from bi_agent.sycm_import import _ledger_sha256
        payload = build_xlsx(shop_rows(1))
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "f.xlsx"
            path.write_bytes(payload)
            import unittest.mock as mock
            with mock.patch.object(
                    Path, "read_bytes",
                    side_effect=AssertionError("禁止整体读入内存")):
                self.assertEqual(_ledger_sha256(path),
                                 hashlib.sha256(payload).hexdigest())

    def test_zip_bomb_total_is_rejected_before_extraction(self):
        # 单成员解压 65MB、高压缩比：压缩包本身很小，总量防线必须在读取前拒。
        bomb = build_xlsx(shop_rows(1), extra_members={
            "xl/bomb.bin": b"A" * (MAX_UNCOMPRESSED_BYTES + 1)})
        with zipfile.ZipFile(io.BytesIO(bomb)) as z:
            declared_total = sum(info.file_size for info in z.infolist())
        self.assertGreater(declared_total, MAX_UNCOMPRESSED_BYTES,
                           "夹具前提：解压总量确实越限")
        self.assertLess(len(bomb), 1024 * 1024, "压缩包本身必须很小（炸弹形态）")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bomb.xlsx"
            path.write_bytes(bomb)
            try:
                parse_sycm_file(path, registered_shops=REGISTERED)
            except SycmValidationError as error:
                self.assertEqual(error.reason_code, ReasonCode.PARSE_FAILED)
            else:
                self.fail("解压总量超 64MB 必须在读取任何成员前被拒")

    def test_row_overflow_at_boundary(self):
        """行上限反向突变：上限调到 4 时，恰 4 条数据行可行、5 条必拒。"""
        import unittest.mock as mock
        with mock.patch.object(sycm_module, "MAX_ROWS", 4):
            ok = parse(shop_rows(4))
            self.assertEqual(len(ok.rows), 4)
            expect_rejection(self, shop_rows(5), ReasonCode.ROW_OVERFLOW)

    def test_row_overflow_reports_rows_read(self):
        import unittest.mock as mock
        with mock.patch.object(sycm_module, "MAX_ROWS", 4):
            error = expect_rejection(self, shop_rows(6), ReasonCode.ROW_OVERFLOW)
            self.assertGreaterEqual(error.detail.get("rows_read", 0), 4)

    def test_column_overflow_at_boundary(self):
        rows = shop_rows(1)
        filler = [f"填充列{i}" for i in range(MAX_COLUMNS - len(rows[0]) + 1)]
        rows[0] = rows[0] + filler
        rows[1] = rows[1] + ["x"] * len(filler)
        expect_rejection(self, rows, ReasonCode.PARSE_FAILED)
        # 恰在 256 列内必须仍可解析（反向突变）。
        ok = shop_rows(1)
        pad = MAX_COLUMNS - len(ok[0])
        ok[0] = ok[0] + [f"填充列{i}" for i in range(pad)]
        ok[1] = ok[1] + ["x"] * pad
        self.assertEqual(len(parse(ok).rows), 1)

    def test_sheet_limits(self):
        expect_rejection(self, shop_rows(1), ReasonCode.PARSE_FAILED,
                         sheets=MAX_SHEETS + 1, registered=REGISTERED)
        # 单 sheet 是本导出的唯一合法形态；多 sheet（含恰 16 个）一律 parse_failed。
        expect_rejection(self, shop_rows(1), ReasonCode.PARSE_FAILED,
                         sheets=2)
        self.assertEqual(len(parse(shop_rows(1), sheets=1).rows), 1)


# ---------------------------------------------------------------------------
# 真实导出文件（只读，绝不写库）
# ---------------------------------------------------------------------------

REAL_DIR = Path(__file__).parents[2] / "docs" / "手动导出表格"
REAL_SHOPS = {"宝威德滤纸直销": "166517", "沃金五金机电商城": "166647"}
SHOP_WHITELIST_HEADERS = ("统计日期", "店铺名称", "访客数", "商品访客数", "浏览量",
                          "支付金额", "支付买家数", "支付件数", "下单金额",
                          "成功退款金额", "全站推广花费", "关键词推广花费",
                          "精准人群推广花费", "智能场景花费", "淘宝客佣金",
                          "支付转化率", "客单价", "加购人数", "商品收藏买家数")
CUSTOMER_WHITELIST_HEADERS = ("统计日期", "店铺名称", "用户数", "已购用户数",
                              "新访客户数", "父订单数", "复购父订单数",
                              "180天复购率", "店铺粉丝数", "店铺会员数")


@unittest.skipUnless(REAL_DIR.is_dir(), "真实导出目录不存在")
class RealExportFileTests(unittest.TestCase):
    """docs/手动导出表格/ 只读断言：真实表头与 028 白名单一致。不碰数据库。"""

    def _files(self, suffix: str) -> list[Path]:
        return sorted(REAL_DIR.glob(f"*-{suffix}_*.xlsx"))

    def _parsed_shop_files(self) -> list:
        parsed = []
        for path in self._files("店铺维度"):
            try:
                parsed.append(parse_sycm_file(path, registered_shops=REAL_SHOPS))
            except SycmValidationError as error:
                self.assertEqual(error.reason_code,
                                 ReasonCode.SHOP_NOT_REGISTERED, path.name)
        return parsed

    def test_real_shop_headers_cover_the_whole_whitelist(self):
        for path in self._files("店铺维度"):
            with self.subTest(file=path.name):
                header, _rows = read_sycm_sheet(path)
                missing = [name for name in SHOP_WHITELIST_HEADERS
                           if name not in header]
                self.assertEqual(missing, [], "店铺维度表头必须覆盖 028 白名单")

    def test_real_customer_headers_cover_the_whole_whitelist(self):
        for path in self._files("客户维度"):
            with self.subTest(file=path.name):
                header, _rows = read_sycm_sheet(path)
                missing = [name for name in CUSTOMER_WHITELIST_HEADERS
                           if name not in header]
                self.assertEqual(missing, [], "客户维度表头必须覆盖 028 白名单")

    def test_registered_shop_files_parse_61_days_each(self):
        by_shop = {result.shop_id: result
                   for result in self._parsed_shop_files()}
        self.assertEqual(sorted(by_shop), ["166517", "166647"],
                         "只有两家已登记店铺能解析成功")
        for shop_id, result in by_shop.items():
            with self.subTest(shop=shop_id):
                self.assertEqual(result.kind, FileKind.SHOP)
                self.assertEqual(len(result.rows), 61,
                                 "2026-08-01..09-30 应为 61 天")
                self.assertEqual(result.rows[0].stat_date, date(2026, 9, 30))
                self.assertEqual(result.rows[-1].stat_date, date(2026, 8, 1))
                dates = [row.stat_date for row in result.rows]
                self.assertEqual(len(set(dates)), 61, "逐日一行、无重复")
                # 零花费披露计数与原始网格中的空/NULL 花费单元一致（交叉核对）。
                self.assertEqual(
                    result.zero_spend_cells,
                    self._raw_empty_spend_cells(result),
                    "空推广花费按 0 处理的计数必须与原始单元一致")

    @staticmethod
    def _raw_empty_spend_cells(result) -> int:
        for path in sorted(REAL_DIR.glob("*-店铺维度_*.xlsx")):
            header, rows = read_sycm_sheet(path)
            names = [str(name or "").strip() for name in header]
            if "全站推广花费" not in names:
                continue
            try:
                probe = parse_sycm_file(path, registered_shops=REAL_SHOPS)
            except SycmValidationError:
                continue
            if probe.shop_id != result.shop_id:
                continue
            column = names.index("全站推广花费")

            def is_empty(value: str | None) -> bool:
                if value is None:
                    return True
                text = value.strip()
                return not text or text.upper() == "NULL"

            return sum(1 for row in rows if is_empty(row.get(column)))
        return -1

    def test_unregistered_shop_files_rejected(self):
        rejected = []
        for path in self._files("店铺维度"):
            try:
                parse_sycm_file(path, registered_shops=REAL_SHOPS)
            except SycmValidationError as error:
                if error.reason_code == ReasonCode.SHOP_NOT_REGISTERED:
                    rejected.append(path.name)
        self.assertEqual(len(rejected), 2,
                         f"元发美特钉枪直销店与钉子工厂必须整文件拒：{rejected}")

    def test_item_dimension_files_all_rejected(self):
        self.assertEqual(len(self._files("商品维度")), 4)
        for path in self._files("商品维度"):
            with self.subTest(file=path.name):
                try:
                    parse_sycm_file(path, registered_shops=REAL_SHOPS)
                except SycmValidationError as error:
                    self.assertEqual(error.reason_code,
                                     ReasonCode.NOT_IMPORTED_THIS_ROUND)
                else:
                    self.fail(f"商品维度文件必须拒：{path.name}")

    def test_real_customer_files_parse_for_registered_shops(self):
        parsed = []
        rejected = 0
        for path in self._files("客户维度"):
            try:
                parsed.append(parse_sycm_file(path, registered_shops=REAL_SHOPS))
            except SycmValidationError as error:
                self.assertEqual(error.reason_code,
                                 ReasonCode.SHOP_NOT_REGISTERED, path.name)
                rejected += 1
        self.assertEqual(rejected, 2)
        by_shop = {result.shop_id: result for result in parsed}
        self.assertEqual(sorted(by_shop), ["166517", "166647"])
        for result in by_shop.values():
            self.assertEqual(result.kind, FileKind.CUSTOMER)
            self.assertEqual(len(result.rows), 61)


if __name__ == "__main__":
    unittest.main()
