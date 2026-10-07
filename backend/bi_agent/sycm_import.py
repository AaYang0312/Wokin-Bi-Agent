"""生意参谋手工导出导入（计划 §3.M2）：标准库 xlsx 读取器 + 校验管线 + 导入器 + CLI。

设计契约（全部 fail closed，见 docs/superpowers/plans/2026-10-02-manual-sycm-export-import.md）：

1. **读取器防护**：文件 ≤5MB、zip 解压总量 ≤64MB（在读取任何成员**之前**按
   ZipInfo 声明大小判，绝不先解压再发现是炸弹）、数据行 ≤20 万、列 ≤256、
   sheet ≤16；本导出的唯一合法形态是单工作表。超限一律拒绝，不静默截断。
2. **原因码集中定义**（`ReasonCode`）：missing_columns / bad_date /
   shop_not_registered / row_overflow / pii_columns_rejected / parse_failed /
   not_imported_this_round。校验管线对每个文件给出**恰好一个**稳定原因。
3. **店铺映射只认数据列**「店铺名称」与 ``bi.shops.display_name`` 精确相等；
   文件名不可信。一个文件必须恰好解析到一家已登记店铺，否则整文件拒绝；
   pdd 平台店铺不在 manual sycm 来源的登记面里（同样整文件拒）。
4. **PII 白名单不变**：客户维度只收 028 登记的 8 个日聚合列；任何个体级明细
   列名（买家昵称/收货人/手机号等）出现即整文件拒。商品维度文件本轮一律拒。
5. **数值清洗**：千分位；比率列按导出百分数 ÷100 存分数（CHECK 拦忘除 100
   的回归）；推广花费 5 列空值按 0 处理并在结果里披露计数；其余列空值存
   NULL 不补零；不启用的列（包裹/物流）与白名单外聚合列不读。
6. **写入幂等**：按 (shop_id, stat_date) 后批覆盖前批；batch_id 与
   content_sha256 都取文件内容 SHA-256（文件名哈希只作为参考进事件 payload）。
   每文件一行 ``bi.manual_uploads`` 台账 + 不可变事件，拒绝也留台账。覆盖凭证
   upsert ``bi.sync_state``（covered multirange 累加、data_as_of、
   quality_status='unknown'、quality_rule='manual-export/unverified'）。
   能力授予沿用 ``bi.shops.capabilities`` 既有机制，pdd 有第二道独立硬守卫。
7. **CLI 默认关闭**：``SYCM_MANUAL_IMPORT_ENABLED`` 只认 true/false（沿用
   ``config._flag`` 语义），缺省/false 拒绝执行并给稳定提示。退出码显式：
   0=全部接收；1=存在失败（含容器损坏收敛出的拒绝），无未捕获 traceback；
   2=门禁/配置/输入路径错误。
8. **容器层异常收敛**（M4/B1 收口）：``PARSE_LAYER_ERRORS`` 覆盖 zip/XML 层
   的已知异常族，一律折算成 parse_failed 并落台账；批内逐文件隔离，单个
   损坏文件不中止批次。

已知边界（记录在案）：负数金额/未来日期/比率越界等行级值错误由 028 的表
约束作第二道闸——触发时整个文件事务回滚，**不落台账行**（台账词表里没有
能诚实描述这种失败的拒绝码，不得说谎）。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import sys
import uuid
import zipfile
import xml.etree.ElementTree as ET
import zlib
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from pathlib import Path
from typing import Iterable, Mapping
from zoneinfo import ZoneInfo

import psycopg
from psycopg.types.json import Jsonb
from psycopg.types.multirange import Multirange
from psycopg.types.range import TimestamptzRange

from .config import _flag
from .sources import (MANUAL_EXPORT_ENTITY, MANUAL_EXPORT_SOURCE,
                      MANUAL_METRIC_CAPABILITIES)

logger = logging.getLogger(__name__)

BEIJING = ZoneInfo("Asia/Shanghai")

# 覆盖凭证与能力授予契约（计划 §2/D1、§3.M2；词表与 M3 查询层对齐）。
# 名字的唯一真源在 sources 注册表；这里保留 M2 的公开名字（CLI/测试沿用）。
SYCM_MANUAL_SOURCE = MANUAL_EXPORT_SOURCE
SHOP_DAILY_ENTITY = MANUAL_EXPORT_ENTITY
MANUAL_QUALITY_RULE = "manual-export/unverified"
SYCM_MANUAL_CAPABILITIES = tuple(sorted(MANUAL_METRIC_CAPABILITIES))
CLI_SUBJECT = "sycm_import_cli"

# 读取器防护上限（计划 §3.M2；值本身被 tests.test_sycm_parser 钉死）。
MAX_FILE_BYTES = 5 * 1024 * 1024
MAX_UNCOMPRESSED_BYTES = 64 * 1024 * 1024
MAX_ROWS = 200_000
MAX_COLUMNS = 256
MAX_SHEETS = 16

_SSML = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"

# 解析层收敛异常族（M4/B1 欠账收口）：任何 zip/XML 层异常——成员 CRC 损坏
# （BadZipFile）、deflate 流截断（zlib.error/EOFError）、不支持的压缩位
# （NotImplementedError）、伪造单元引用（ValueError）、sharedStrings 越界
# （IndexError）、表单 XML 畸形（ET.ParseError）——都折算成 parse_failed，
# 绝不以未捕获 traceback 逃逸。OSError 刻意不在列：文件不存在/无权限是调用
# 方的输入错误，不是内容攻击面，不得伪装成解析拒绝。
PARSE_LAYER_ERRORS = (zipfile.BadZipFile, zlib.error, EOFError,
                      NotImplementedError, ET.ParseError, ValueError,
                      IndexError)


class ReasonCode(StrEnum):
    """校验管线的稳定原因码（枚举集中定义；词表进台账 reason_code）。"""

    MISSING_COLUMNS = "missing_columns"
    BAD_DATE = "bad_date"
    SHOP_NOT_REGISTERED = "shop_not_registered"
    ROW_OVERFLOW = "row_overflow"
    PII_COLUMNS_REJECTED = "pii_columns_rejected"
    PARSE_FAILED = "parse_failed"
    NOT_IMPORTED_THIS_ROUND = "not_imported_this_round"


class FileKind(StrEnum):
    SHOP = "shop"
    CUSTOMER = "customer"


class SycmValidationError(Exception):
    """校验管线拒绝：reason_code 稳定，detail 结构化（供台账事件与 CLI 摘要）。"""

    def __init__(self, reason_code: ReasonCode,
                 detail: Mapping[str, object] | None = None) -> None:
        super().__init__(f"{reason_code}: {detail or ''}")
        self.reason_code = reason_code
        self.detail: dict[str, object] = dict(detail or {})


# ---------------------------------------------------------------------------
# 表头 → 028 白名单列映射（实测表头见 028 迁移注释，docs/手动导出表格/ 12 文件）
# ---------------------------------------------------------------------------

DATE_HEADER = "统计日期"
SHOP_NAME_HEADER = "店铺名称"

_SHOP_HEADER_MAP = {
    "访客数": "visitors",
    "商品访客数": "item_visitors",
    "浏览量": "views",
    "支付金额": "paid_amount_sycm",
    "支付买家数": "paid_buyers",
    "支付件数": "paid_items",
    "下单金额": "order_amount",
    "成功退款金额": "refund_amount",
    "全站推广花费": "ad_spend_all",
    "关键词推广花费": "ad_spend_keyword",
    "精准人群推广花费": "ad_spend_crowd",
    "智能场景花费": "ad_spend_smart",
    "淘宝客佣金": "ke_commission",
    "支付转化率": "pay_conversion_rate",
    "客单价": "aov",
    "加购人数": "cart_count",
    "商品收藏买家数": "favorite_count",
}
_SHOP_INTEGER_COLUMNS = frozenset((
    "visitors", "item_visitors", "views", "paid_buyers", "paid_items",
    "cart_count", "favorite_count"))
# 推广花费 5 列（M1 选列口径）：空值按「零花费+披露」处理，其余列空值存 NULL。
_ZERO_DEFAULT_SPEND_COLUMNS = ("ad_spend_all", "ad_spend_keyword",
                               "ad_spend_crowd", "ad_spend_smart", "ke_commission")
_RATE_COLUMNS = frozenset(("pay_conversion_rate", "repeat_rate_180d"))

_CUSTOMER_HEADER_MAP = {
    "用户数": "users",
    "已购用户数": "paid_users",
    "新访客户数": "new_customers",
    "父订单数": "parent_orders",
    "复购父订单数": "repeat_parent_orders",
    "180天复购率": "repeat_rate_180d",
    "店铺粉丝数": "shop_fans",
    "店铺会员数": "shop_members",
}
_CUSTOMER_INTEGER_COLUMNS = frozenset((
    "users", "paid_users", "new_customers", "parent_orders",
    "repeat_parent_orders", "shop_fans", "shop_members"))

# 个体级明细列名词表：出现任何一个即整文件拒（PII 白名单不变，计划 §0/5）。
_PII_COLUMN_NAMES = frozenset((
    "买家昵称", "买家会员名", "买家ID", "用户ID", "会员ID", "旺旺号",
    "收货人姓名", "收货地址", "手机号", "联系电话", "身份证号",
))
# 商品维度签名列：出现即本轮不导入（商品 ID 无映射，挂起等 channel_items）。
_ITEM_SIGNATURE_COLUMNS = ("商品ID", "商品名称")
# 维度判定签名：客户维度以「用户数」为签名列，其余按店铺维度要求必需列。
_CUSTOMER_SIGNATURE_COLUMN = "用户数"


# ---------------------------------------------------------------------------
# 解析产物
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SycmShopRow:
    """店铺维度日聚合一行（字段精确等于 028 白名单，多一个字段都是泄漏开口）。"""

    stat_date: date
    shop_name: str
    visitors: int | None
    item_visitors: int | None
    views: int | None
    paid_amount_sycm: Decimal | None
    paid_buyers: int | None
    paid_items: int | None
    order_amount: Decimal | None
    refund_amount: Decimal | None
    ad_spend_all: Decimal | None
    ad_spend_keyword: Decimal | None
    ad_spend_crowd: Decimal | None
    ad_spend_smart: Decimal | None
    ke_commission: Decimal | None
    pay_conversion_rate: Decimal | None
    aov: Decimal | None
    cart_count: int | None
    favorite_count: int | None


@dataclass(frozen=True)
class SycmCustomerRow:
    """客户维度日聚合一行（只收 028 登记的 8 个聚合列）。"""

    stat_date: date
    shop_name: str
    users: int | None
    paid_users: int | None
    new_customers: int | None
    parent_orders: int | None
    repeat_parent_orders: int | None
    repeat_rate_180d: Decimal | None
    shop_fans: int | None
    shop_members: int | None


@dataclass(frozen=True)
class ParsedSycmFile:
    filename: str
    kind: FileKind
    shop_id: str
    shop_name: str
    header: tuple[str, ...]
    rows: tuple[SycmShopRow | SycmCustomerRow, ...]
    content_sha256: str
    zero_spend_cells: int


# ---------------------------------------------------------------------------
# 读取器（zipfile + ElementTree，无第三方依赖；五道防护全部在此生效）
# ---------------------------------------------------------------------------

def _column_index(cell_ref: str) -> int:
    """'CG5' → 84（0 基列序号）；无字母前缀的引用视为非法。"""
    letters = "".join(ch for ch in cell_ref if ch.isalpha()).upper()
    if not letters:
        raise ValueError(cell_ref)
    index = 0
    for ch in letters:
        index = index * 26 + (ord(ch) - ord("A") + 1)
    return index - 1


def _cell_text(cell: ET.Element, shared_strings: list[str]) -> str | None:
    kind = cell.get("t")
    if kind == "inlineStr":
        node = cell.find(f"{_SSML}is")
        if node is None:
            return None
        return "".join(t.text or "" for t in node.iter(f"{_SSML}t"))
    value = cell.find(f"{_SSML}v")
    if value is None or value.text is None:
        return None
    if kind == "s":
        return shared_strings[int(value.text)]
    return value.text  # 数字/公式字符串单元：原文交清洗层处理


def _ensure_size_within_limit(path: Path) -> int:
    """文件体积防线：只 stat 不读取——超大输入在进内存/进哈希之前就被拒。"""
    size = path.stat().st_size
    if size > MAX_FILE_BYTES:
        raise SycmValidationError(ReasonCode.PARSE_FAILED, {
            "detail": "file_too_large", "bytes": size,
            "limit_bytes": MAX_FILE_BYTES})
    return size


def read_sycm_sheet(path: Path) -> tuple[list[str], list[dict[int, str | None]]]:
    """读唯一数据表的表头与数据行。

    返回 (表头名列表, 数据行列表)；每行按 0 基列序号取值。五道防护（文件
    体积、解压总量、sheet 数、行数、列数）在这里生效，超限抛
    `SycmValidationError`——防护先于业务校验，结构攻击到不了语义层。
    """
    path = Path(path)
    _ensure_size_within_limit(path)
    try:
        archive = zipfile.ZipFile(path)
    except zipfile.BadZipFile as error:
        raise SycmValidationError(ReasonCode.PARSE_FAILED, {
            "detail": "not_a_zip_file"}) from error
    with archive:
        # 解压总量防线在读取任何成员**之前**：zip 炸弹绝不先解压再发现。
        total = sum(info.file_size for info in archive.infolist())
        if total > MAX_UNCOMPRESSED_BYTES:
            raise SycmValidationError(ReasonCode.PARSE_FAILED, {
                "detail": "uncompressed_too_large", "bytes": total,
                "limit_bytes": MAX_UNCOMPRESSED_BYTES})
        worksheets = sorted(
            name for name in archive.namelist()
            if name.startswith("xl/worksheets/") and name.endswith(".xml"))
        if len(worksheets) > MAX_SHEETS:
            raise SycmValidationError(ReasonCode.PARSE_FAILED, {
                "detail": "too_many_sheets", "sheets": len(worksheets),
                "limit": MAX_SHEETS})
        if len(worksheets) != 1:
            # 本导出的唯一合法形态是单工作表：多表无法确定哪张是真数据。
            raise SycmValidationError(ReasonCode.PARSE_FAILED, {
                "detail": "expected_single_worksheet", "sheets": len(worksheets)})
        if "xl/sharedStrings.xml" in archive.namelist():
            shared_root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
            shared_strings = [
                "".join(t.text or "" for t in si.iter(f"{_SSML}t"))
                for si in shared_root.findall(f"{_SSML}si")]
        else:
            shared_strings = []
        header: list[str] | None = None
        rows: list[dict[int, str | None]] = []
        try:
            with archive.open(worksheets[0]) as sheet:
                for _event, elem in ET.iterparse(sheet, events=("end",)):
                    if elem.tag != f"{_SSML}row":
                        continue
                    if header is None:
                        header = _read_header_row(elem, shared_strings)
                    else:
                        if len(rows) + 1 > MAX_ROWS:
                            raise SycmValidationError(ReasonCode.ROW_OVERFLOW, {
                                "rows_read": len(rows) + 1, "limit": MAX_ROWS})
                        row = _read_data_row(elem, shared_strings)
                        if row is not None:  # 全空行跳过
                            rows.append(row)
                    elem.clear()
        except ET.ParseError as error:
            raise SycmValidationError(ReasonCode.PARSE_FAILED, {
                "detail": "malformed_sheet_xml"}) from error
    if header is None:
        header = []
    return header, rows


def _read_header_row(row: ET.Element,
                     shared_strings: list[str]) -> list[str]:
    header: list[str] = []
    positional = 0
    for cell in row.findall(f"{_SSML}c"):
        ref = cell.get("r")
        index = _column_index(ref) if ref else positional
        positional = index + 1
        if index >= MAX_COLUMNS:
            raise SycmValidationError(ReasonCode.PARSE_FAILED, {
                "detail": "too_many_columns", "columns": index + 1,
                "limit": MAX_COLUMNS})
        while len(header) <= index:
            header.append("")
        header[index] = _cell_text(cell, shared_strings) or ""
    return header


def _read_data_row(row: ET.Element,
                   shared_strings: list[str]) -> dict[int, str | None] | None:
    values: dict[int, str | None] = {}
    positional = 0
    for cell in row.findall(f"{_SSML}c"):
        ref = cell.get("r")
        index = _column_index(ref) if ref else positional
        positional = index + 1
        if index >= MAX_COLUMNS:
            raise SycmValidationError(ReasonCode.PARSE_FAILED, {
                "detail": "too_many_columns", "columns": index + 1,
                "limit": MAX_COLUMNS})
        values[index] = _cell_text(cell, shared_strings)
    if all(text is None or text == "" for text in values.values()):
        return None
    return values


# ---------------------------------------------------------------------------
# 数值清洗与行装配
# ---------------------------------------------------------------------------

def _clean_decimal(raw: str | None, column: str) -> Decimal | None:
    if raw is None:
        return None
    text = raw.strip().replace(",", "").replace("，", "")
    # 生意参谋导出用字面量 NULL 标记缺失（实测只出现在推广花费列）：
    # 它是缺值记号不是数据，按空值走（推广花费列随后转 0+披露）。
    if not text or text.upper() == "NULL":
        return None
    try:
        return Decimal(text)
    except InvalidOperation as error:
        raise SycmValidationError(ReasonCode.PARSE_FAILED, {
            "detail": "unparsable_number", "column": column, "value": raw,
        }) from error


def _clean_rate(raw: str | None, column: str) -> Decimal | None:
    """导出是百分数（如 0.14%、15.76%）：÷100 存 [0,1] 分数（028 契约）。"""
    if raw is None:
        return None
    text = raw.strip()
    if text.endswith("%"):
        value = _clean_decimal(text[:-1], column)
        return None if value is None else value / Decimal(100)
    return _clean_decimal(text, column)  # 无百分号的按已是分数处理


def _clean_integer(raw: str | None, column: str) -> int | None:
    value = _clean_decimal(raw, column)
    if value is None:
        return None
    if value != value.to_integral_value():
        raise SycmValidationError(ReasonCode.PARSE_FAILED, {
            "detail": "non_integer_count", "column": column, "value": raw})
    return int(value)


def _clean_cell(raw: str | None, column: str, *,
                rate: bool, integer: bool, zero_default: bool) -> Decimal | int | None:
    value: Decimal | int | None
    if rate:
        value = _clean_rate(raw, column)
    elif integer:
        value = _clean_integer(raw, column)
    else:
        value = _clean_decimal(raw, column)
    if value is None and zero_default:
        # 「零花费+披露」：空推广花费按 0 处理，计数进导入结果。
        return Decimal("0")
    return value


def _assemble_rows(header: list[str], data_rows: list[dict[int, str | None]],
                   kind: FileKind) -> tuple[tuple, int]:
    """把数据行装配成白名单行对象；日期/数值错误在此整文件拒绝。"""
    names = [str(name or "").strip() for name in header]
    mapping = (_CUSTOMER_HEADER_MAP if kind is FileKind.CUSTOMER
               else _SHOP_HEADER_MAP)
    integer_columns = (_CUSTOMER_INTEGER_COLUMNS if kind is FileKind.CUSTOMER
                       else _SHOP_INTEGER_COLUMNS)
    # 列位一次性解析（行循环里不做 O(表头) 的 index 查找）。
    column_sources = sorted(
        (names.index(name), column)
        for name, column in mapping.items() if name in names)
    date_idx = names.index(DATE_HEADER)
    shop_idx = names.index(SHOP_NAME_HEADER)

    rows: list[SycmShopRow | SycmCustomerRow] = []
    zero_spend_cells = 0
    for offset, data in enumerate(data_rows, start=1):
        raw_date = (data.get(date_idx) or "").strip()
        try:
            stat_date = date.fromisoformat(raw_date)
        except ValueError as error:
            raise SycmValidationError(ReasonCode.BAD_DATE, {
                "rows_read": offset, "value": raw_date}) from error
        shop_name = (data.get(shop_idx) or "").strip()
        values: dict[str, object] = {}
        for column_idx, column in column_sources:
            raw = data.get(column_idx)
            zero_default = (kind is FileKind.SHOP
                            and column in _ZERO_DEFAULT_SPEND_COLUMNS)
            # 缺值记号与清洗层同一口径：空串与字面量 NULL 都算缺失。
            stripped = None if raw is None else raw.strip()
            was_missing = stripped is None or not stripped or stripped.upper() == "NULL"
            cleaned = _clean_cell(raw, column, rate=column in _RATE_COLUMNS,
                                  integer=column in integer_columns,
                                  zero_default=zero_default)
            if zero_default and was_missing:
                # 「零花费+披露」：空推广花费按 0 处理，计数进导入结果。
                zero_spend_cells += 1
            values[column] = cleaned
        if kind is FileKind.SHOP:
            rows.append(SycmShopRow(stat_date=stat_date, shop_name=shop_name,
                                    **values))  # type: ignore[arg-type]
        else:
            rows.append(SycmCustomerRow(stat_date=stat_date, shop_name=shop_name,
                                        **values))  # type: ignore[arg-type]
    return tuple(rows), zero_spend_cells


# ---------------------------------------------------------------------------
# 校验管线（每个文件恰好一个稳定原因码）
# ---------------------------------------------------------------------------

def parse_sycm_file(path: Path,
                    *, registered_shops: Mapping[str, str]) -> ParsedSycmFile:
    """解析并校验一个手工导出文件。

    `registered_shops` 是 display_name → shop_id 的精确匹配登记表（导入器从
    ``bi.shops`` 派生；纯函数测试可直接给字典）。校验顺序：
    结构防护 → 商品维度 → PII 列 → 必需列 → 行级日期/数值 → 店铺解析。
    任何一步不过即整文件拒绝，绝不部分导入。
    """
    path = Path(path)
    # 体积防线先于内容哈希：超限文件绝不整体读入内存（复审 P1）。
    _ensure_size_within_limit(path)
    content_sha256 = hashlib.sha256(path.read_bytes()).hexdigest()
    try:
        header, data_rows = read_sycm_sheet(path)
    except SycmValidationError:
        raise
    except PARSE_LAYER_ERRORS as error:
        # B1 收口：容器/XML 层的任何异常都收敛为稳定原因码，detail 结构化
        # 进台账事件；绝不把 zipfile/zlib/ElementTree 的原始异常交给调用方。
        raise SycmValidationError(ReasonCode.PARSE_FAILED, {
            "detail": "malformed_container",
            "error": type(error).__name__}) from error
    names = [str(name or "").strip() for name in header]

    if any(signature in names for signature in _ITEM_SIGNATURE_COLUMNS):
        raise SycmValidationError(ReasonCode.NOT_IMPORTED_THIS_ROUND, {
            "rows_read": len(data_rows), "kind": None,
            "item_columns": [name for name in names
                             if name in _ITEM_SIGNATURE_COLUMNS]})
    pii_columns = tuple(dict.fromkeys(
        name for name in names if name in _PII_COLUMN_NAMES))
    if pii_columns:
        raise SycmValidationError(ReasonCode.PII_COLUMNS_REJECTED, {
            "rows_read": len(data_rows), "pii_columns": pii_columns})

    kind = (FileKind.CUSTOMER if _CUSTOMER_SIGNATURE_COLUMN in names
            else FileKind.SHOP)
    required = (_CUSTOMER_HEADER_MAP if kind is FileKind.CUSTOMER
                else _SHOP_HEADER_MAP)
    missing = tuple(name for name in (DATE_HEADER, SHOP_NAME_HEADER, *required)
                    if name not in names)
    if missing:
        raise SycmValidationError(ReasonCode.MISSING_COLUMNS, {
            "rows_read": len(data_rows), "kind": str(kind),
            "missing_columns": missing})

    rows, zero_spend_cells = _assemble_rows(header, data_rows, kind)
    shop_names = {row.shop_name for row in rows} if rows else {""}
    unknown = tuple(sorted(name for name in shop_names
                           if name not in registered_shops))
    if unknown:
        raise SycmValidationError(ReasonCode.SHOP_NOT_REGISTERED, {
            "rows_read": len(rows), "kind": str(kind),
            "unknown_shops": unknown})
    if len(shop_names) != 1:
        # 一个文件必须恰好解析到一家店：混多店无法归属覆盖凭证，fail closed。
        raise SycmValidationError(ReasonCode.SHOP_NOT_REGISTERED, {
            "rows_read": len(rows), "kind": str(kind),
            "multiple_shops": tuple(sorted(shop_names))})
    shop_name = next(iter(shop_names))
    return ParsedSycmFile(
        filename=path.name, kind=kind, shop_id=registered_shops[shop_name],
        shop_name=shop_name, header=tuple(header), rows=rows,
        content_sha256=content_sha256, zero_spend_cells=zero_spend_cells)


# ---------------------------------------------------------------------------
# 导入器（幂等写入 + 台账 + 覆盖凭证 + 能力授予）
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ImportOutcome:
    """一次文件导入的结果摘要（CLI 逐文件输出；字段即公开契约）。

    `upload_id` 是台账行 ``bi.manual_uploads.id``：上传面（M4）的响应契约
    以它回指台账；CLI 的 JSON 摘要同样携带。
    """

    filename: str
    content_sha256: str | None
    kind: str | None
    status: str  # 'accepted' | 'rejected'
    reason_code: str | None
    rows_read: int
    rows_written: int
    shops: tuple[str, ...]
    zero_spend_cells: int = 0
    granted_capabilities: tuple[str, ...] = ()
    detail: Mapping[str, object] | None = None
    upload_id: uuid.UUID | None = None


def _ledger_sha256(path: Path) -> str | None:
    """流式分块哈希：拒绝路径可能面对超大文件，绝不整体读入内存（复审 P1）。"""
    try:
        digest = hashlib.sha256()
        with Path(path).open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return None


def eligible_shop_mapping(conn: psycopg.Connection) -> dict[str, str]:
    """display_name → shop_id 精确匹配表。

    pdd 平台店铺永不进入 manual sycm 来源的登记面（硬性边界，非延后）；
    display_name 重复的店视为歧义、同样不可映射——宁可拒文件，不可猜店铺。
    """
    profiles = conn.execute(
        "SELECT display_name, shop_id, platform FROM bi.shops").fetchall()
    counts: dict[str, int] = {}
    for display, _shop_id, _platform in profiles:
        name = str(display or "").strip()
        if name:
            counts[name] = counts.get(name, 0) + 1
    mapping: dict[str, str] = {}
    for display, shop_id, platform in profiles:
        name = str(display or "").strip()
        if (not name or counts[name] > 1
                or str(platform or "").strip().lower() == "pdd"):
            continue
        mapping[name] = str(shop_id)
    return mapping


def _insert_events(conn: psycopg.Connection, upload_id, events: list) -> None:
    for event_type, payload in events:
        conn.execute(
            "INSERT INTO bi.manual_upload_events(id, upload_id, event_type, "
            "payload) VALUES (%s, %s, %s, %s)",
            (uuid.uuid4(), upload_id, event_type, Jsonb(payload)))


def _reference_payload(filename: str, content_sha256: str | None) -> dict:
    return {
        "filename": filename,
        "content_sha256": content_sha256,
        # 文件名哈希只作参考（batch_id/Content sha 才是批次身份）。
        "filename_sha256": hashlib.sha256(
            filename.encode("utf-8")).hexdigest(),
    }


def _insert_upload_ledger(conn: psycopg.Connection, *, upload_id,
                          subject_id: str, filename: str,
                          content_sha256: str | None, kind, status: str,
                          reason_code: str | None, rows_read: int,
                          rows_written: int, shops: list[str]) -> None:
    """台账 INSERT 的单一实现：接受与拒绝两条路径共用同一列序。"""
    conn.execute(
        "INSERT INTO bi.manual_uploads(id, subject_id, filename, "
        "content_sha256, kind, status, reason_code, rows_read, rows_written, "
        "shops, completed_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, "
        "now())",
        (upload_id, subject_id, filename, content_sha256,
         str(kind) if kind else None, status, reason_code, rows_read,
         rows_written, shops))


def record_upload_rejection(conn: psycopg.Connection, *, filename: str,
                            content_sha256: str | None,
                            error: SycmValidationError,
                            subject_id: str) -> ImportOutcome:
    """无文件路径的拒绝台账（上传面）：字节体拒绝（超限等）也留一行 + 事件。

    与 `_record_rejection` 共用同一 INSERT 与事件结构；调用方自行给出
    `content_sha256`。上传面对超限请求传入的是**保留内容**的哈希（封顶前缀
    或零字节），detail 里的 content_scope 说清这一点——绝不冒充整文件哈希。
    """
    detail = dict(error.detail)
    kind = detail.get("kind")
    kind = kind if kind in (FileKind.SHOP, FileKind.CUSTOMER) else None
    rows_read = int(detail.get("rows_read") or 0)
    with conn.transaction():
        upload_id = uuid.uuid4()
        _insert_upload_ledger(
            conn, upload_id=upload_id, subject_id=subject_id,
            filename=filename, content_sha256=content_sha256, kind=kind,
            status="rejected", reason_code=str(error.reason_code),
            rows_read=rows_read, rows_written=0, shops=[])
        _insert_events(conn, upload_id, [
            ("received", _reference_payload(filename, content_sha256)),
            ("rejected", {"reason_code": str(error.reason_code),
                          "rows_read": rows_read, **detail}),
        ])
    logger.info("sycm manual upload rejected file=%s reason=%s",
                filename, error.reason_code)
    return ImportOutcome(
        filename=filename, content_sha256=content_sha256,
        kind=str(kind) if kind else None, status="rejected",
        reason_code=str(error.reason_code), rows_read=rows_read,
        rows_written=0, shops=(), detail=detail, upload_id=upload_id)


def _record_rejection(conn: psycopg.Connection, path: Path,
                      error: SycmValidationError, subject_id: str) -> ImportOutcome:
    """拒绝也留台账：一行 manual_uploads + received/rejected 事件（单事务）。"""
    return record_upload_rejection(
        conn, filename=Path(path).name,
        content_sha256=_ledger_sha256(path), error=error,
        subject_id=subject_id)


_SHOP_FACT_UPSERT_SQL = """
INSERT INTO bi.sycm_shop_daily(shop_id, stat_date, visitors, item_visitors,
  views, paid_amount_sycm, paid_buyers, paid_items, order_amount,
  refund_amount, ad_spend_all, ad_spend_keyword, ad_spend_crowd,
  ad_spend_smart, ke_commission, pay_conversion_rate, aov, cart_count,
  favorite_count, batch_id)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
        %s, %s, %s)
ON CONFLICT (shop_id, stat_date) DO UPDATE SET
  visitors = EXCLUDED.visitors, item_visitors = EXCLUDED.item_visitors,
  views = EXCLUDED.views, paid_amount_sycm = EXCLUDED.paid_amount_sycm,
  paid_buyers = EXCLUDED.paid_buyers, paid_items = EXCLUDED.paid_items,
  order_amount = EXCLUDED.order_amount, refund_amount = EXCLUDED.refund_amount,
  ad_spend_all = EXCLUDED.ad_spend_all,
  ad_spend_keyword = EXCLUDED.ad_spend_keyword,
  ad_spend_crowd = EXCLUDED.ad_spend_crowd,
  ad_spend_smart = EXCLUDED.ad_spend_smart,
  ke_commission = EXCLUDED.ke_commission,
  pay_conversion_rate = EXCLUDED.pay_conversion_rate, aov = EXCLUDED.aov,
  cart_count = EXCLUDED.cart_count,
  favorite_count = EXCLUDED.favorite_count,
  batch_id = EXCLUDED.batch_id, recorded_at = now()
"""

_CUSTOMER_FACT_UPSERT_SQL = """
INSERT INTO bi.sycm_customer_daily(shop_id, stat_date, users, paid_users,
  new_customers, parent_orders, repeat_parent_orders, repeat_rate_180d,
  shop_fans, shop_members, batch_id)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (shop_id, stat_date) DO UPDATE SET
  users = EXCLUDED.users, paid_users = EXCLUDED.paid_users,
  new_customers = EXCLUDED.new_customers,
  parent_orders = EXCLUDED.parent_orders,
  repeat_parent_orders = EXCLUDED.repeat_parent_orders,
  repeat_rate_180d = EXCLUDED.repeat_rate_180d,
  shop_fans = EXCLUDED.shop_fans, shop_members = EXCLUDED.shop_members,
  batch_id = EXCLUDED.batch_id, recorded_at = now()
"""

# 覆盖凭证 upsert：covered multirange 累加（multirange 并集自动归并重叠区间），
# data_as_of 只进不退，质量恒为 unknown + manual-export/unverified（D1 立即可
# 用+披露；认证路径是后续对账切片的事）。
_COVERAGE_UPSERT_SQL = """
INSERT INTO bi.sync_state(source, entity, shop_id, covered, data_as_of,
                          quality_status, quality_rule)
VALUES (%s, %s, %s, %s, %s, 'unknown', %s)
ON CONFLICT (source, entity, shop_id) DO UPDATE SET
  covered = bi.sync_state.covered + EXCLUDED.covered,
  data_as_of = GREATEST(bi.sync_state.data_as_of, EXCLUDED.data_as_of),
  quality_status = 'unknown',
  quality_rule = EXCLUDED.quality_rule
"""


def _day_range(day: date) -> TimestamptzRange:
    start = datetime.combine(day, time.min, tzinfo=BEIJING)
    end = datetime.combine(day + timedelta(days=1), time.min, tzinfo=BEIJING)
    return TimestamptzRange(start, end, "[)")


def _write_coverage(conn: psycopg.Connection, shop_id: str,
                    rows: tuple) -> None:
    days = sorted({row.stat_date for row in rows})
    covered = Multirange([_day_range(day) for day in days])
    data_as_of = _day_range(days[-1]).upper
    conn.execute(_COVERAGE_UPSERT_SQL,
                 (SYCM_MANUAL_SOURCE, SHOP_DAILY_ENTITY, shop_id, covered,
                  data_as_of, MANUAL_QUALITY_RULE))


def _capability_list(capabilities: object) -> list[str]:
    """bi.shops.capabilities（text[]）→ 列表；psycopg 替身可能给 '{a,b}' 文本。"""
    if capabilities is None:
        return []
    if isinstance(capabilities, str):
        return [item for item in capabilities.strip("{}").split(",") if item]
    return [str(item) for item in capabilities]


def _grant_capabilities(conn: psycopg.Connection,
                        shop_id: str) -> tuple[str, ...]:
    """能力并入既有词表（沿用 bi.shops.capabilities 机制）；pdd 硬性守卫。

    返回值是**本次新增**授予的词（B2 收口）：店铺既有词不是这次导入授的，
    混进返回值会让调用方把旧词当新授披露。
    """
    row = conn.execute(
        "SELECT platform, capabilities FROM bi.shops WHERE shop_id=%s "
        "FOR UPDATE", (shop_id,)).fetchone()
    if row is None:
        raise RuntimeError(f"shop vanished mid-import: {shop_id}")
    platform, capabilities = row
    if str(platform or "").strip().lower() == "pdd":
        # 第二道独立守卫：即便登记面失守，这里也绝不给 pdd 开能力。
        logger.warning("sycm manual import refused capabilities for pdd shop=%s",
                       shop_id)
        return ()
    existing = _capability_list(capabilities)
    merged = sorted(set(existing) | set(SYCM_MANUAL_CAPABILITIES))
    if merged != existing:
        conn.execute("UPDATE bi.shops SET capabilities=%s WHERE shop_id=%s",
                     (merged, shop_id))
    return tuple(sorted(set(SYCM_MANUAL_CAPABILITIES) - set(existing)))


def _write_accepted(conn: psycopg.Connection, parsed: ParsedSycmFile,
                    subject_id: str) -> ImportOutcome:
    """单事务原子写入：事实 upsert + 台账 + 事件 +（店铺维度）覆盖凭证与能力。"""
    rows = parsed.rows
    batch_id = parsed.content_sha256
    granted: tuple[str, ...] = ()
    with conn.transaction():
        if parsed.kind is FileKind.SHOP:
            for row in rows:
                assert isinstance(row, SycmShopRow)
                conn.execute(_SHOP_FACT_UPSERT_SQL, (
                    parsed.shop_id, row.stat_date, row.visitors,
                    row.item_visitors, row.views, row.paid_amount_sycm,
                    row.paid_buyers, row.paid_items, row.order_amount,
                    row.refund_amount, row.ad_spend_all, row.ad_spend_keyword,
                    row.ad_spend_crowd, row.ad_spend_smart, row.ke_commission,
                    row.pay_conversion_rate, row.aov, row.cart_count,
                    row.favorite_count, batch_id))
        else:
            for row in rows:
                assert isinstance(row, SycmCustomerRow)
                conn.execute(_CUSTOMER_FACT_UPSERT_SQL, (
                    parsed.shop_id, row.stat_date, row.users, row.paid_users,
                    row.new_customers, row.parent_orders,
                    row.repeat_parent_orders, row.repeat_rate_180d,
                    row.shop_fans, row.shop_members, batch_id))
        upload_id = uuid.uuid4()
        _insert_upload_ledger(
            conn, upload_id=upload_id, subject_id=subject_id,
            filename=parsed.filename, content_sha256=batch_id,
            kind=parsed.kind, status="accepted", reason_code=None,
            rows_read=len(rows), rows_written=len(rows),
            shops=[parsed.shop_id])
        reference = _reference_payload(parsed.filename, batch_id)
        _insert_events(conn, upload_id, [
            ("received", reference),
            ("accepted", {"rows_read": len(rows), "rows_written": len(rows),
                          "zero_spend_cells": parsed.zero_spend_cells,
                          "shops": [parsed.shop_id],
                          "kind": str(parsed.kind)}),
        ])
        if parsed.kind is FileKind.SHOP:
            # 客户维度只补事实与台账：能力词表都是店铺维度指标，客户聚合
            # 证明不了任何推广/流量能力，授予只跟店铺维度导入走。
            _write_coverage(conn, parsed.shop_id, rows)
            granted = _grant_capabilities(conn, parsed.shop_id)
    logger.info("sycm manual import accepted file=%s shop=%s rows=%s",
                parsed.filename, parsed.shop_id, len(rows))
    return ImportOutcome(
        filename=parsed.filename, content_sha256=batch_id, kind=str(parsed.kind),
        status="accepted", reason_code=None, rows_read=len(rows),
        rows_written=len(rows), shops=(parsed.shop_id,),
        zero_spend_cells=parsed.zero_spend_cells,
        granted_capabilities=granted, upload_id=upload_id)


def import_sycm_file(conn: psycopg.Connection, path: Path | str, *,
                     subject_id: str = CLI_SUBJECT) -> ImportOutcome:
    """导入单个文件：校验通过的整文件原子写入；被拒的整文件留拒绝台账。"""
    path = Path(path)
    try:
        parsed = parse_sycm_file(path, registered_shops=eligible_shop_mapping(conn))
    except SycmValidationError as error:
        return _record_rejection(conn, path, error, subject_id)
    return _write_accepted(conn, parsed, subject_id)


def import_sycm_paths(conn: psycopg.Connection,
                      paths: Iterable[Path | str]) -> list[ImportOutcome]:
    """批量导入：目录展开为其中全部 xlsx（排序），文件按原样导入。

    批内逐文件隔离（B1 收口）：单个文件的容器层异常折算成 parse_failed
    拒绝（照常留台账），绝不中止批次——脏批次里每个文件都必须有确定结局。
    """
    outcomes: list[ImportOutcome] = []
    for entry in paths:
        path = Path(entry)
        candidates = (sorted(path.glob("*.xlsx")) if path.is_dir()
                      else [path])
        for candidate in candidates:
            try:
                outcomes.append(import_sycm_file(conn, candidate))
            except PARSE_LAYER_ERRORS as error:
                # 双保险：解析器已把容器层异常收敛成 SycmValidationError，
                # 这里兜住未来解析器改动漏出的同族异常，语义不变。
                outcomes.append(_record_rejection(
                    conn, candidate,
                    SycmValidationError(ReasonCode.PARSE_FAILED, {
                        "detail": "malformed_container",
                        "error": type(error).__name__}), CLI_SUBJECT))
    return outcomes


# ---------------------------------------------------------------------------
# CLI：python -m bi_agent.sycm_import <目录|文件...>
# ---------------------------------------------------------------------------

DISABLED_CODE = "SYCM_MANUAL_IMPORT_DISABLED"


def manual_import_enabled(env: Mapping[str, str]) -> bool:
    """门禁沿用 config._flag 语义：只认 true/false，缺省与空白都是关。"""
    return _flag(env, "SYCM_MANUAL_IMPORT_ENABLED")


def _outcome_record(outcome: ImportOutcome) -> dict:
    record = {
        "filename": outcome.filename,
        "content_sha256": outcome.content_sha256,
        "kind": outcome.kind,
        "status": outcome.status,
        "reason_code": outcome.reason_code,
        "rows_read": outcome.rows_read,
        "rows_written": outcome.rows_written,
        "shops": list(outcome.shops),
        "zero_spend_cells": outcome.zero_spend_cells,
        "granted_capabilities": list(outcome.granted_capabilities),
    }
    if outcome.detail:
        record["detail"] = {
            key: (list(value) if isinstance(value, tuple) else value)
            for key, value in outcome.detail.items()}
    record["upload_id"] = (str(outcome.upload_id)
                           if outcome.upload_id is not None else None)
    return record


def main(argv: list[str] | None = None, *, env: Mapping[str, str] | None = None,
         connect=None, out=None, err=None) -> int:
    """批模式入口。退出码显式（B1 收口）：

    - 0 = 每个文件都被接收；
    - 1 = 存在失败（带稳定码的拒绝，含容器损坏收敛出的 parse_failed）——
      运维据此感知"批次没有全绿"，且全程无未捕获 traceback；
    - 2 = 门禁/配置/输入路径问题（稳定提示在 stderr，不进入解析）。
    """
    env = os.environ if env is None else env
    out = sys.stdout if out is None else out
    err = sys.stderr if err is None else err
    try:
        enabled = manual_import_enabled(env)
    except ValueError as error:
        print(f"sycm_import: {error} (code=SYCM_MANUAL_IMPORT_BAD_FLAG)",
              file=err)
        return 2
    if not enabled:
        print(f"sycm_import: 手工导入未启用：SYCM_MANUAL_IMPORT_ENABLED 只接受"
              f" 'true'（当前缺省/false）(code={DISABLED_CODE})", file=err)
        return 2
    parser = argparse.ArgumentParser(
        prog="bi_agent.sycm_import",
        description="导入生意参谋手工导出（店铺/客户维度 xlsx）")
    parser.add_argument("paths", nargs="+", help="导出目录或 xlsx 文件")
    args = parser.parse_args(argv)
    missing = [argument for argument in args.paths
               if not Path(argument).exists()]
    if missing:
        # 输入路径错误是配置问题不是解析拒绝：稳定码 + 退出 2，不产生台账。
        print(f"sycm_import: 输入路径不存在：{missing[0]} "
              f"(code=SYCM_IMPORT_PATH_NOT_FOUND)", file=err)
        return 2
    dsn = (env.get("BI_WRITER_DSN") or "").strip()
    if not dsn:
        print("sycm_import: 缺少 BI_WRITER_DSN "
              "(code=SYCM_IMPORT_REQUIRES_BI_WRITER_DSN)", file=err)
        return 2
    open_connection = connect if connect is not None else psycopg.connect
    conn = open_connection(dsn)
    try:
        outcomes = import_sycm_paths(
            conn, [Path(argument) for argument in args.paths])
    finally:
        if connect is None:  # 注入连接的生命周期归调用方（测试外层事务）管
            conn.close()
    for outcome in outcomes:
        print(json.dumps(_outcome_record(outcome), ensure_ascii=False,
                         sort_keys=True), file=out)
    return 1 if any(outcome.status != "accepted" for outcome in outcomes) else 0


if __name__ == "__main__":
    raise SystemExit(main())
