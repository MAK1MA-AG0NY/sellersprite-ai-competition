# -*- coding: utf-8 -*-
"""Parse order/tracking pairs and manually copied 17TRACK summary text.

This module is deliberately offline: it does not open a browser or contact any
tracking service. Timestamps copied from the summary have no timezone, so a
small, explicit US-local-time inference is used only when the copied location
is sufficiently specific.
"""
from __future__ import annotations

import calendar
import csv
import datetime as _dt
import io
import re
import xml.etree.ElementTree as _ET
import zipfile
from typing import Any


MAX_PAIRS = 500
_TRACK_RE = re.compile(r"\A[A-Za-z0-9]{5,50}\Z")
_TIMESTAMP_RE = re.compile(
    r"^\s*(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}(?::\d{2})?)\s*(.*?)\s*$"
)
_COUNTRY_MARKER_RE = re.compile(r",\s*(USA?)\b", re.IGNORECASE)
_POWERED_RE = re.compile(r"powered\s+by\b", re.IGNORECASE)
_TRANSIT_DAYS_RE = re.compile(r"\(?\s*(\d+)\s*(?:天|days?|d)\s*\)?", re.IGNORECASE)


# Standard-time UTC offsets and abbreviations for states with one civil-time
# zone. Split-zone states intentionally need a known city below; guessing from
# the state alone could silently shift a 48-hour risk calculation.
_STATE_ZONE: dict[str, tuple[int, str, str, bool]] = {}

def _add_states(states: str, offset: int, standard: str, daylight: str,
                observes_dst: bool = True) -> None:
    for state in states.split():
        _STATE_ZONE[state] = (offset, standard, daylight, observes_dst)


_add_states("CT DE DC GA MA MD ME NH NJ NY OH PA RI SC VA VT WV", -5, "EST", "EDT")
_add_states("AL AR IA IL LA MN MO MS OK WI", -6, "CST", "CDT")
_add_states("CO MT NM UT WY", -7, "MST", "MDT")
_add_states("CA WA", -8, "PST", "PDT")
_add_states("HI", -10, "HST", "HST", False)
_add_states("AZ", -7, "MST", "MST", False)

# Cities in otherwise ambiguous states / regions. These are explicit rather
# than an assumption that an entire split-zone state shares one offset.
_CITY_ZONE: dict[str, tuple[int, str, str, bool]] = {
    "merrillville, in": (-6, "CST", "CDT", True),
    "hammond, in": (-6, "CST", "CDT", True),
    "gary, in": (-6, "CST", "CDT", True),
    "east chicago, in": (-6, "CST", "CDT", True),
    "portage, in": (-6, "CST", "CDT", True),
    "valparaiso, in": (-6, "CST", "CDT", True),
    "michigan city, in": (-6, "CST", "CDT", True),
    "chesterton, in": (-6, "CST", "CDT", True),
    "homestead, fl": (-5, "EST", "EDT", True),
    "miami, fl": (-5, "EST", "EDT", True),
    "orlando, fl": (-5, "EST", "EDT", True),
    "tampa, fl": (-5, "EST", "EDT", True),
    "jacksonville, fl": (-5, "EST", "EDT", True),
    "fort lauderdale, fl": (-5, "EST", "EDT", True),
    "dallas, tx": (-6, "CST", "CDT", True),
    "houston, tx": (-6, "CST", "CDT", True),
    "austin, tx": (-6, "CST", "CDT", True),
    "san antonio, tx": (-6, "CST", "CDT", True),
    "el paso, tx": (-7, "MST", "MDT", True),
    "new york, ny": (-5, "EST", "EDT", True),
    "los angeles, ca": (-8, "PST", "PDT", True),
    "chicago, il": (-6, "CST", "CDT", True),
    "phoenix, az": (-7, "MST", "MST", False),
}


class _USLocalTimezone(_dt.tzinfo):
    """Minimal standard-library timezone for modern US DST wall-clock input."""

    def __init__(self, offset_hours: int, standard: str, daylight: str,
                 observes_dst: bool, assumed_default: bool = False) -> None:
        self._standard_offset = _dt.timedelta(hours=offset_hours)
        self._standard = standard
        self._daylight = daylight
        self._observes_dst = observes_dst
        self.assumed_default = assumed_default

    @staticmethod
    def _sunday_in_month(year: int, month: int, nth: int) -> int:
        first_weekday, _ = calendar.monthrange(year, month)
        first_sunday = 1 + ((6 - first_weekday) % 7)
        return first_sunday + (nth - 1) * 7

    def _is_dst(self, value: _dt.datetime) -> bool:
        if not self._observes_dst or value.year < 2007:
            return False
        start_day = self._sunday_in_month(value.year, 3, 2)
        end_day = self._sunday_in_month(value.year, 11, 1)
        if value.month < 3 or value.month > 11:
            return False
        if 3 < value.month < 11:
            return True
        if value.month == 3:
            return value.day > start_day or (value.day == start_day and value.hour >= 3)
        # The 01:00 hour repeats at the autumn transition. fold=0 is the
        # daylight occurrence, fold=1 the standard-time occurrence.
        if value.day < end_day:
            return True
        if value.day > end_day:
            return False
        if value.hour < 1:
            return True
        if value.hour > 1:
            return False
        return value.fold == 0

    def is_nonexistent_or_ambiguous(self, value: _dt.datetime) -> bool:
        if not self._observes_dst or value.year < 2007:
            return False
        start_day = self._sunday_in_month(value.year, 3, 2)
        end_day = self._sunday_in_month(value.year, 11, 1)
        if value.month == 3 and value.day == start_day and value.hour == 2:
            return True  # spring-forward gap: 02:00–02:59 never happened
        if value.month == 11 and value.day == end_day and value.hour == 1:
            return True  # without an offset, the repeated hour is ambiguous
        return False

    def utcoffset(self, value: _dt.datetime | None) -> _dt.timedelta:
        if value is not None and self._is_dst(value):
            return self._standard_offset + _dt.timedelta(hours=1)
        return self._standard_offset

    def dst(self, value: _dt.datetime | None) -> _dt.timedelta:
        if value is not None and self._is_dst(value):
            return _dt.timedelta(hours=1)
        return _dt.timedelta(0)

    def tzname(self, value: _dt.datetime | None) -> str:
        if value is not None and self._is_dst(value):
            return self._daylight
        return self._standard


def _timezone_for_location(location: str, year: int) -> _USLocalTimezone:
    normalized = re.sub(r"\s+", " ", location.strip(" ,")).casefold()
    zone = _CITY_ZONE.get(normalized)
    if zone is None:
        match = re.search(r",\s*([A-Z]{2})\s*$", location.strip(), re.IGNORECASE)
        zone = _STATE_ZONE.get(match.group(1).upper()) if match else None
    if zone is None or (zone[3] and year < 2007):
        # When the carrier omits a reliable US location, the user's chosen
        # fallback is US Central Time. Historical years also use standard CST
        # because the compact DST rules below only model the modern US rule.
        return _USLocalTimezone(-6, "CST", "CDT", True, assumed_default=True)
    return _USLocalTimezone(*zone)


def assume_central_time(timestamp_raw: Any) -> str | None:
    """Interpret a stored local scan time as America/Chicago for legacy unknown-zone rows."""
    if not isinstance(timestamp_raw, str):
        return None
    match = _TIMESTAMP_RE.match(timestamp_raw.strip())
    if not match:
        return None
    value = match.group(1).strip()
    pattern = "%Y-%m-%d %H:%M:%S" if value.count(":") == 2 else "%Y-%m-%d %H:%M"
    try:
        naive = _dt.datetime.strptime(value, pattern)
    except ValueError:
        return None
    tz = _USLocalTimezone(-6, "CST", "CDT", True, assumed_default=True)
    return naive.replace(tzinfo=tz).isoformat(timespec="seconds")


def _parse_leading_timestamp(event_cell: str, location: str) -> tuple[str | None, str | None, str, bool]:
    """Return ISO-localized time, copied time, event text and uncertainty."""
    match = _TIMESTAMP_RE.match(event_cell)
    if not match:
        return None, None, event_cell.strip(), False

    timestamp_raw = match.group(1).strip()
    remainder = match.group(2).strip()
    try:
        pattern = "%Y-%m-%d %H:%M:%S" if timestamp_raw.count(":") == 2 else "%Y-%m-%d %H:%M"
        naive = _dt.datetime.strptime(timestamp_raw, pattern)
    except ValueError:
        return None, timestamp_raw, remainder, True

    tz = _timezone_for_location(location, naive.year)
    if tz.is_nonexistent_or_ambiguous(naive):
        # The source local clock is ambiguous/nonexistent; use the user's
        # chosen US Central fallback instead of leaving it pending confirmation.
        tz = _USLocalTimezone(-6, "CST", "CDT", True, assumed_default=True)
    aware = naive.replace(tzinfo=tz)
    return aware.isoformat(timespec="seconds"), timestamp_raw, remainder, False


def _split_location_and_event(remainder: str) -> tuple[str | None, str]:
    marker = _COUNTRY_MARKER_RE.search(remainder)
    if not marker:
        return None, remainder.strip()
    location = remainder[:marker.start()].strip(" ,\t") or None
    event_text = remainder[marker.end():].strip(" ,\t;:-")
    return location, event_text


def _status_from_text(status_text: str, event_text: str) -> str:
    """Map common Chinese carrier status labels, then fall back to English wording."""
    primary = status_text.casefold().replace(" ", "")
    combined = f"{status_text} {event_text}".casefold()

    # Search explicit Chinese status values first; this column is more
    # authoritative than a possibly historical English event description.
    chinese_groups = (
        ("not_found", ("查询不到", "查不到", "未查询到", "查无信息", "暂无信息", "暂无跟踪信息", "无查询结果", "暂未上线")),
        ("delivered", ("成功签收", "签收成功", "已签收", "已妥投", "已送达", "投递成功")),
        ("out_for_delivery", ("派送途中", "派送中", "正在派送", "派件中")),
        ("exception", ("投递失败", "派送失败", "可能异常", "包裹退回", "运输过久", "异常", "问题件")),
        ("awaiting_pickup", ("到达待取", "待取件", "等待取件", "到达自提点", "可供取件")),
        ("in_transit", ("运输途中", "运输中", "运输", "已发货", "处理中")),
    )
    for status, terms in chinese_groups:
        if any(term in primary for term in terms):
            return status

    english_groups = (
        ("not_found", ("not found", "no information", "unable to find", "not available")),
        ("delivered", ("delivered", "signed for", "successfully delivered")),
        ("exception", ("delivery failed", "delivery exception", "exception", "possible exception", "alert",
                        "delivery attempt", "notice left", "redelivery")),
        ("out_for_delivery", ("out for delivery", "delivery in progress")),
        ("awaiting_pickup", ("awaiting pickup", "ready for pickup", "available for pickup")),
        ("in_transit", ("in transit", "arrived at", "departed", "processed", "accepted at", "shipment information")),
    )
    for status, terms in english_groups:
        if any(term in combined for term in terms):
            return status
    return "unknown"


def _line_error(line_number: int, message: str, text: str) -> dict[str, Any]:
    return {"line": line_number, "error": message, "text": text}


def _validate_pair(order_id: str, tracking_number: str) -> tuple[str, str] | None:
    order_id = order_id.strip()
    tracking_number = tracking_number.strip()
    if not order_id:
        return None
    if len(order_id) > 100:
        return None
    if not _TRACK_RE.fullmatch(tracking_number):
        return None
    return order_id, tracking_number


_ORDER_HEADERS = {"平台参考号", "平台订单号", "订单号", "orderid", "amazonorderid", "platformorderid", "order"}
_TRACK_HEADERS = {"跟踪号", "追踪号", "运单号", "trackingnumber", "trackingno", "tracking", "tracknumber"}
MAX_XLSX_BYTES = 5 * 1024 * 1024
MAX_XLSX_UNPACKED = 25 * 1024 * 1024
MAX_XLSX_ROWS = 10000
MAX_XLSX_COLUMNS = 50


def _xlsx_column_number(reference: str) -> int:
    match = re.match(r"([A-Z]+)", str(reference or "").upper())
    if not match:
        return 0
    number = 0
    for char in match.group(1):
        number = number * 26 + ord(char) - ord("A") + 1
    return number


def _preflight_xlsx_bounds(archive: zipfile.ZipFile) -> None:
    """Check actual OOXML row/cell coordinates instead of trusting worksheet dimension tags."""
    sheet_files = [name for name in archive.namelist()
                   if re.fullmatch(r"xl/worksheets/[^/]+\.xml", name)]
    if not sheet_files:
        raise ValueError("XLSX 未包含工作表 XML")
    for name in sheet_files:
        current_row = 0
        current_column = 0
        try:
            with archive.open(name) as source:
                for event, element in _ET.iterparse(source, events=("start", "end")):
                    tag = element.tag.rsplit("}", 1)[-1]
                    if event == "start" and tag == "row":
                        raw_row = element.attrib.get("r")
                        row_number = int(raw_row) if raw_row and raw_row.isdigit() else current_row + 1
                        if row_number > MAX_XLSX_ROWS:
                            raise ValueError(f"工作表实际行号超过 {MAX_XLSX_ROWS} 行上限")
                        current_row, current_column = row_number, 0
                    elif event == "start" and tag == "c":
                        reference = element.attrib.get("r", "")
                        column_number = _xlsx_column_number(reference)
                        if column_number:
                            if column_number > MAX_XLSX_COLUMNS:
                                raise ValueError(f"工作表实际列号超过 {MAX_XLSX_COLUMNS} 列上限")
                            current_column = column_number
                        else:
                            current_column += 1
                            if current_column > MAX_XLSX_COLUMNS:
                                raise ValueError(f"工作表实际列数超过 {MAX_XLSX_COLUMNS} 列上限")
                    if event == "end":
                        element.clear()
        except (_ET.ParseError, UnicodeError) as exc:
            raise ValueError(f"工作表 XML 结构损坏：{name}") from exc


def _header_key(value: Any) -> str:
    text = str(value or "").strip().casefold()
    return re.sub(r"[\s_\-./（）()]+", "", text)


def _parse_pair_rows(rows: list[tuple[int, list[Any]]]) -> dict[str, list[dict[str, Any]]]:
    pairs: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    usable = [(line, cells) for line, cells in rows if any(str(cell or "").strip() for cell in cells)]
    if not usable:
        return {"pairs": pairs, "errors": errors}

    first_line, first_cells = usable[0]
    keys = [_header_key(cell) for cell in first_cells]
    order_cols = [i for i, key in enumerate(keys) if key in _ORDER_HEADERS]
    track_cols = [i for i, key in enumerate(keys) if key in _TRACK_HEADERS]
    has_known_header = bool(order_cols or track_cols)
    generic_header = any(key in {"order", "ordernumber", "tracking", "trackingnumber", "tracknumber", "shipmenttracking"}
                         for key in keys)
    order_col, track_col = 0, 1
    data_rows = usable
    if has_known_header or generic_header:
        data_rows = usable[1:]
        if len(order_cols) != 1 or len(track_cols) != 1 or order_cols[0] == track_cols[0]:
            errors.append(_line_error(first_line, "表头映射不明确：需要唯一的订单号列和跟踪号列", "\t".join(map(str, first_cells))))
            data_rows = []
        else:
            order_col, track_col = order_cols[0], track_cols[0]

    def row_error(line_number, message, rendered, cells):
        error = _line_error(line_number, message, rendered)
        if order_col < len(cells) and isinstance(cells[order_col], str):
            candidate = cells[order_col].strip()
            if candidate and len(candidate) <= 100:
                error["order_id"] = candidate
        return error

    nonblank_rows = 0
    for line_number, cells in data_rows:
        nonblank_rows += 1
        rendered = "\t".join(str(cell or "") for cell in cells)
        if nonblank_rows > MAX_PAIRS:
            errors.append(row_error(line_number, f"最多支持 {MAX_PAIRS} 对", rendered, cells))
            continue
        if order_col >= len(cells) or track_col >= len(cells):
            errors.append(row_error(line_number, "该行缺少已映射的订单号列或跟踪号列", rendered, cells))
            continue
        if any(not isinstance(value, str) for value in (cells[order_col], cells[track_col])):
            errors.append(row_error(line_number, "订单号和跟踪号单元格必须保存为文本；数值格式可能丢失前导零或精度", rendered, cells))
            continue
        validated = _validate_pair(cells[order_col], cells[track_col])
        if validated is None:
            errors.append(row_error(line_number, "订单号须为 1–100 字符，跟踪号须为 5–50 位字母或数字", rendered, cells))
            continue
        order_id, tracking_number = validated
        pairs.append({"order_id": order_id, "tracking_number": tracking_number})
    return {"pairs": pairs, "errors": errors}


def parse_pairs(text: str) -> dict[str, list[dict[str, str]]]:
    """Parse TSV/CSV or pasted ERP columns, optionally with recognized headers."""
    if not isinstance(text, str):
        return {"pairs": [], "errors": [_line_error(0, "输入必须是文本", repr(text))]}
    rows: list[tuple[int, list[Any]]] = []
    for line_number, raw_line in enumerate(text.splitlines(), 1):
        line = raw_line.strip("\ufeff\r\n")
        if not line.strip():
            continue
        delimiter = "\t" if "\t" in line else ","
        try:
            cells = next(csv.reader([line], delimiter=delimiter, quotechar='"', strict=True))
        except (csv.Error, StopIteration):
            rows.append((line_number, [line]))
            continue
        rows.append((line_number, [cell.strip() for cell in cells]))
    return _parse_pair_rows(rows)


def parse_xlsx(data: bytes, sheet_name: str | None = None) -> dict[str, Any]:
    """Read a bounded XLSX upload without extracting it or evaluating formulas."""
    base = {"pairs": [], "errors": [], "sheet_names": [], "sheet_name": None}
    if not isinstance(data, (bytes, bytearray)) or not data:
        base["errors"] = [_line_error(0, "XLSX 文件为空或输入类型错误", "")]
        return base
    if len(data) > MAX_XLSX_BYTES:
        base["errors"] = [_line_error(0, f"XLSX 文件超过 {MAX_XLSX_BYTES // (1024 * 1024)} MiB 限制", "")]
        return base
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            members = archive.infolist()
            total_size = sum(item.file_size for item in members)
            if len(members) > 200 or total_size > MAX_XLSX_UNPACKED:
                raise ValueError("XLSX 解压规模超过安全限制")
            if any(item.file_size > MAX_XLSX_UNPACKED for item in members):
                raise ValueError("XLSX 单个部件超过安全限制")
            if any(item.file_size > 1024 * 1024 and item.compress_size and item.file_size / item.compress_size > 100 for item in members):
                raise ValueError("XLSX 压缩比异常，已拒绝解析")
            if "[Content_Types].xml" not in archive.namelist() or "xl/workbook.xml" not in archive.namelist():
                raise ValueError("文件不是有效的 XLSX 工作簿")
            _preflight_xlsx_bounds(archive)
        from openpyxl import load_workbook
        workbook = load_workbook(io.BytesIO(data), read_only=True, data_only=False)
    except Exception as exc:
        base["errors"] = [_line_error(0, f"XLSX 无法安全读取：{str(exc)[:200]}", "")]
        return base

    try:
        base["sheet_names"] = list(workbook.sheetnames)
        if sheet_name is None:
            worksheet = workbook[workbook.sheetnames[0]]
        elif sheet_name in workbook.sheetnames:
            worksheet = workbook[sheet_name]
        else:
            base["errors"] = [_line_error(0, "指定工作表不存在", str(sheet_name))]
            return base
        base["sheet_name"] = worksheet.title
        if worksheet.max_row and worksheet.max_row > MAX_XLSX_ROWS:
            base["errors"] = [_line_error(0, f"工作表超过 {MAX_XLSX_ROWS} 行上限", worksheet.title)]
            return base
        if worksheet.max_column and worksheet.max_column > MAX_XLSX_COLUMNS:
            base["errors"] = [_line_error(0, f"工作表超过 {MAX_XLSX_COLUMNS} 列上限", worksheet.title)]
            return base
        rows: list[tuple[int, list[Any]]] = []
        try:
            for line_number, row in enumerate(worksheet.iter_rows(
                    max_row=MAX_XLSX_ROWS, max_col=MAX_XLSX_COLUMNS, values_only=False), 1):
                cells: list[Any] = []
                invalid_formula = False
                for cell in row:
                    value = cell.value
                    if cell.data_type == "f":
                        invalid_formula = True
                    elif cell.data_type == "e":
                        rows.append((line_number, ["__CELL_ERROR__", str(value or "")]))
                        invalid_formula = False
                        break
                    if value is None:
                        cells.append("")
                    elif isinstance(value, str):
                        cells.append(value.strip())
                    else:
                        cells.append(value)
                else:
                    if invalid_formula:
                        rows.append((line_number, ["__FORMULA__", "公式"])); continue
                    rows.append((line_number, cells))
            parsed = _parse_pair_rows(rows)
            for row in rows:
                if any(str(cell).startswith("__CELL_ERROR__") for cell in row[1]):
                    parsed["errors"].append(_line_error(row[0], "工作表包含 Excel 错误单元格", ""))
                elif any(str(cell).startswith("__FORMULA__") for cell in row[1]):
                    parsed["errors"].append(_line_error(row[0], "不接受公式单元格；请粘贴为纯文本值", ""))
            parsed["pairs"] = [pair for pair in parsed["pairs"] if pair["order_id"] not in {"__CELL_ERROR__", "__FORMULA__"}]
        except Exception as exc:
            return {**base, "pairs": [], "errors": [_line_error(0, f"XLSX 工作表读取失败：{str(exc)[:200]}", worksheet.title)]}
        return {**base, **parsed}
    finally:
        workbook.close()


def parse_summary(text: str) -> dict[str, list[dict[str, Any]]]:
    """Parse tab-separated rows copied from a 17TRACK summary page.

    The expected row shape is tracking, origin, destination, event, status,
    transit-time. Rows that cannot be interpreted are reported in ``errors``
    and do not prevent the remaining summary from being imported.
    """
    records: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    if not isinstance(text, str):
        return {"records": records, "errors": [_line_error(0, "输入必须是文本", repr(text))]}

    for line_number, raw_line in enumerate(text.splitlines(), 1):
        line = raw_line.strip("\ufeff\r\n")
        if not line.strip():
            continue
        if re.fullmatch(r"\s*(?:={5,}|-{5,})\s*", line):
            continue
        if _POWERED_RE.search(line):
            continue
        if "\t" not in line:
            errors.append(_line_error(line_number, "summary 行必须是 TSV 且至少包含 5 列", raw_line))
            continue
        cells = [cell.strip() for cell in line.split("\t")]
        if len(cells) < 5:
            errors.append(_line_error(line_number, "summary 行至少需要 5 列", raw_line))
            continue
        tracking_number = cells[0]
        if not _TRACK_RE.fullmatch(tracking_number):
            errors.append(_line_error(line_number, "追踪号须为 5–50 位字母或数字", raw_line))
            continue

        event_cell = cells[3]
        remainder_match = _TIMESTAMP_RE.match(event_cell)
        remainder = remainder_match.group(2).strip() if remainder_match else event_cell.strip()
        location, english_event = _split_location_and_event(remainder)
        event_at, event_at_raw, event_after_location, timezone_unknown = _parse_leading_timestamp(
            event_cell, location or ""
        )
        # `_parse_leading_timestamp` returns text after the date but before
        # country separation. Prefer the cleaned English event when available.
        event_text = english_event if location is not None else event_after_location.strip(" ,\t")
        status_text = cells[4]
        status = _status_from_text(status_text, event_text)
        days_match = _TRANSIT_DAYS_RE.search(cells[5]) if len(cells) > 5 else None
        transit_days = int(days_match.group(1)) if days_match else None
        source_country = cells[1] or None
        destination_country = cells[2] or None
        records.append({
            "tracking_number": tracking_number,
            "status": status,
            "pkg_status_text": status_text or None,
            "event_at": event_at,
            "event_at_raw": event_at_raw,
            "event_text": event_text or None,
            "raw_event": event_cell or None,
            "location": location,
            "country": source_country,
            "origin_country": source_country,
            "source_country": source_country,
            "destination_country": destination_country,
            "transit_days": transit_days,
            "timezone_unknown": timezone_unknown,
        })
    return {"records": records, "errors": errors}


_USPS_TRACK_RE = re.compile(r"\A[A-Za-z0-9]{4,34}\Z")
_USPS_DATE_RE = re.compile(
    r"\b(?:January|February|March|April|May|June|July|August|September|October|November|December|"
    r"Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)\s+\d{1,2},\s+\d{4}"
    r"(?:\s+(?:at\s+)?\d{1,2}:\d{2}(?::\d{2})?\s*[ap]\.?m\.?)?",
    re.IGNORECASE,
)
_USPS_LOCATION_RE = re.compile(
    r"^\s*(.+?),\s*([A-Za-z]{2})\s+\d{5}(?:-\d{4})?\s*$"
)
_USPS_STATUS_GROUPS = (
    ("not_found", ("not found", "no record of that item", "no tracking information",
                    "unable to find", "tracking information is not available")),
    ("delivered", ("delivered", "successfully delivered", "left with individual")),
    ("out_for_delivery", ("out for delivery", "arriving today", "delivery scheduled")),
    ("exception", ("delivery exception", "action needed", "notice left", "attempted delivery",
                    "unable to deliver", "return to sender", "returned to sender", "delivery attempt")),
    ("awaiting_pickup", ("available for pickup", "ready for pickup", "held at post office")),
    ("in_transit", ("in transit", "moving through network", "arrived at", "departed",
                     "usps in possession", "accepted", "shipment received", "label created",
                     "pre-shipment", "awaiting item", "processed through", "processed at")),
)
_USPS_SECTION_END_RE = re.compile(
    r"^(?:tracking history|get updates|text & email updates|product information|"
    r"feedback|track another package|faqs?)\b", re.IGNORECASE,
)


def _usps_status_from_text(text: str) -> str:
    value = re.sub(r"\s+", " ", text or "").casefold()
    for status, phrases in _USPS_STATUS_GROUPS:
        if any(phrase in value for phrase in phrases):
            return status
    return "unknown"


def _usps_marker_number(lines: list[str], index: int) -> str | None:
    line = lines[index].strip()
    marker = re.match(r"^tracking\s*(?:number|no\.?|#)\s*:?\s*(.*)$", line, re.IGNORECASE)
    if marker:
        raw = marker.group(1).strip()
        if not raw:
            for candidate in lines[index + 1:index + 4]:
                if candidate.strip():
                    raw = candidate.strip()
                    break
        normalized = re.sub(r"[^A-Za-z0-9]", "", raw)
        return normalized if _USPS_TRACK_RE.fullmatch(normalized) else None
    normalized = re.sub(r"[^A-Za-z0-9]", "", line)
    return normalized if _USPS_TRACK_RE.fullmatch(normalized) else None


def _usps_location(line: str) -> str | None:
    match = _USPS_LOCATION_RE.match(line.strip())
    if not match:
        return None
    city = re.sub(r"\s+", " ", match.group(1).strip())
    return f"{city}, {match.group(2).upper()}" if city else None


def _usps_event_time(line: str, location: str | None) -> tuple[str | None, str | None, bool]:
    match = _USPS_DATE_RE.search(line or "")
    if not match:
        return None, None, False
    raw = match.group(0).strip().rstrip(".,")
    formats = (
        "%B %d, %Y at %I:%M %p", "%B %d, %Y %I:%M %p", "%B %d, %Y, %I:%M %p",
        "%b %d, %Y at %I:%M %p", "%b %d, %Y %I:%M %p", "%b %d, %Y, %I:%M %p",
        "%B %d, %Y", "%b %d, %Y",
    )
    parsed = None
    for fmt in formats:
        try:
            parsed = _dt.datetime.strptime(re.sub(r"\s+", " ", raw), fmt)
            break
        except ValueError:
            continue
    if parsed is None or not re.search(r"\d{1,2}:\d{2}", raw):
        return None, raw, False
    tz = _timezone_for_location(location or "", parsed.year)
    return parsed.replace(tzinfo=tz).isoformat(timespec="seconds"), raw, tz.assumed_default


def parse_usps_summary(text: str, expected_tracking_numbers: Any) -> dict[str, list[dict[str, Any]]]:
    """Parse visible USPS result text, importing only requested result cards.

    The USPS page is a changing UI, so this parser is deliberately conservative:
    it requires a visible matching tracking ID and recognizable current status.
    Unrecognized cards are returned as errors rather than being marked checked
    or changing local order state.
    """
    records: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    if not isinstance(text, str):
        return {"records": records, "errors": [_line_error(0, "USPS 页面文本必须是文本", "")]}
    if not isinstance(expected_tracking_numbers, (list, tuple)) or not expected_tracking_numbers:
        return {"records": records, "errors": [_line_error(0, "必须提供本批跟踪号白名单", "")]}
    expected: dict[str, str] = {}
    for number in expected_tracking_numbers:
        if not isinstance(number, str) or not _USPS_TRACK_RE.fullmatch(number.strip()):
            return {"records": [], "errors": [_line_error(0, "USPS 跟踪号格式无效", "")]}
        expected.setdefault(number.strip().casefold(), number.strip())
    if len(expected) > 35:
        return {"records": [], "errors": [_line_error(0, "USPS 每批最多解析 35 个唯一跟踪号", "")]}

    lines = [re.sub(r"\s+", " ", line).strip() for line in text.splitlines()]
    markers: list[tuple[int, str]] = []
    for index, line in enumerate(lines):
        number = _usps_marker_number(lines, index)
        if number and number.casefold() in expected:
            markers.append((index, expected[number.casefold()]))
    deduped: list[tuple[int, str]] = []
    seen: set[str] = set()
    for index, number in markers:
        key = number.casefold()
        if key not in seen:
            seen.add(key)
            deduped.append((index, number))
    markers = deduped
    if not markers:
        return {"records": [], "errors": [_line_error(0, "未识别到带跟踪号的 USPS 结果卡片；未写入本地状态", "")]}

    for marker_index, (start, number) in enumerate(markers):
        end = markers[marker_index + 1][0] if marker_index + 1 < len(markers) else len(lines)
        block = [line for line in lines[start:end] if line]
        if block and re.match(r"^tracking\s*(?:number|no\.?|#)\s*:?\s*$", block[0], re.I):
            block = block[1:]
        current_label = next((i for i, line in enumerate(block)
                              if re.match(r"^latest update\b", line, re.I)), None)
        if current_label is None:
            history_boundary = next((i for i, line in enumerate(block)
                                     if re.match(r"^tracking history\b", line, re.I)), len(block))
            current_lines = block[:min(history_boundary, 20)]
        else:
            current_lines = []
            for line in block[current_label + 1:]:
                if _USPS_SECTION_END_RE.match(line):
                    break
                current_lines.append(line)
        current_text = " ".join(current_lines)
        status = _usps_status_from_text(current_text)
        if status == "unknown":
            errors.append(_line_error(start + 1, "USPS 当前状态未能安全识别；此号码未标记为已查", ""))
            continue

        history_start = next((i for i, line in enumerate(block)
                              if re.match(r"^tracking history\b", line, re.I)), None)
        timeline = block[history_start + 1:] if history_start is not None else block
        event_index = next((i for i, line in enumerate(timeline) if _USPS_DATE_RE.search(line)), None)
        event_at = event_at_raw = event_text = location = None
        timezone_unknown = False
        if event_index is not None:
            time_line = timeline[event_index]
            for candidate in timeline[event_index + 1:event_index + 5]:
                candidate_location = _usps_location(candidate)
                if candidate_location:
                    location = candidate_location
                    break
            for candidate in timeline[event_index + 1:event_index + 5]:
                if candidate and not _usps_location(candidate) and not _USPS_SECTION_END_RE.match(candidate):
                    event_text = candidate
                    break
            event_at, event_at_raw, timezone_unknown = _usps_event_time(time_line, location)
        status_text = next((line for line in current_lines if _usps_status_from_text(line) == status), None)
        status_text = status_text or next((line for line in current_lines if line), "USPS status")
        records.append({
            "tracking_number": number,
            "status": status,
            "pkg_status_text": status_text[:300],
            "event_at": event_at,
            "event_at_raw": event_at_raw,
            "event_text": event_text or status_text,
            "raw_event": " | ".join(part for part in (event_at_raw, event_text) if part) or status_text,
            "location": location,
            "country": None,
            "origin_country": None,
            "source_country": None,
            "destination_country": None,
            "transit_days": None,
            "timezone_unknown": timezone_unknown,
        })

    missing = len(expected) - len({record["tracking_number"].casefold() for record in records})
    if missing:
        errors.append(_line_error(0, f"有 {missing} 个请求号码未在页面中得到可识别结果；未自动标记为已查", ""))
    return {"records": records, "errors": errors}


_TRACK718_TRACK_RE = re.compile(r"\A[A-Za-z0-9]{4,50}\Z")
_TRACK718_FIELD_RE = re.compile(r"^\s*(物流单号|包裹状态|目的国|发件国)\s*[:：]\s*(.*?)\s*$")
_TRACK718_LOCATION_EVENT_RE = re.compile(
    r"^\s*(.+?),\s*([A-Za-z]{2})\s+(\d{5}(?:-\d{4})?),\s*(.*?)\s*$"
)
_TRACK718_UI_LABEL_RE = re.compile(
    r"^(?:复制结果|复制链接|联系物流商|免费订阅邮件|轨迹翻译|包裹状态|目的国|发件国|物流单号|query help|"
    r"查询|查询结果|global logistics track platform)(?:\b|\s*[:：]|$)", re.IGNORECASE,
)


def _track718_location_event(line: str) -> tuple[str | None, str | None]:
    match = _TRACK718_LOCATION_EVENT_RE.match(line.strip())
    if not match:
        return None, line.strip() or None
    city, state, _zip_code, event_text = match.groups()
    clean_city = re.sub(r"\s+", " ", city.strip())
    return f"{clean_city}, {state.upper()}", event_text.strip() or None


def parse_track718_summary(text: str, expected_tracking_numbers: Any) -> dict[str, list[dict[str, Any]]]:
    """Parse copied Track718 cards and retain their scan history.

    Track718's copied text uses labeled fields (物流单号 / 包裹状态 / 目的国 /
    发件国), followed by timestamped USPS scans and a row of equals signs between
    cards. The visible-page layout without those labels remains supported too.
    Only exact requested tracking IDs are imported; unrequested labeled cards are
    used solely to delimit the preceding card.
    """
    records: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    if not isinstance(text, str):
        return {"records": [], "errors": [_line_error(0, "Track718 页面文本必须是文本", "")]}
    if not isinstance(expected_tracking_numbers, (list, tuple)) or not expected_tracking_numbers:
        return {"records": [], "errors": [_line_error(0, "必须提供本批跟踪号白名单", "")]}
    expected: dict[str, str] = {}
    for number in expected_tracking_numbers:
        if not isinstance(number, str) or not _TRACK718_TRACK_RE.fullmatch(number.strip()):
            return {"records": [], "errors": [_line_error(0, "Track718 跟踪号格式无效", "")]}
        expected.setdefault(number.strip().casefold(), number.strip())
    if len(expected) > 40:
        return {"records": [], "errors": [_line_error(0, "Track718 每批最多解析 40 个唯一跟踪号", "")]}

    lines = [re.sub(r"\s+", " ", line).strip() for line in text.splitlines()]
    markers: list[tuple[int, str, str | None]] = []
    for index, line in enumerate(lines):
        field = _TRACK718_FIELD_RE.match(line)
        if field and field.group(1) == "物流单号":
            raw_number = field.group(2).strip()
            if _TRACK718_TRACK_RE.fullmatch(raw_number):
                markers.append((index, raw_number, expected.get(raw_number.casefold())))
            continue
        # The visible result-card UI may show the tracking ID on its own line.
        # Keep this fallback, but only accept a whole-line ID from our allowlist.
        number = expected.get(line.casefold())
        if number:
            markers.append((index, number, number))

    deduped: list[tuple[int, str, str | None]] = []
    seen: set[str] = set()
    for marker in markers:
        key = marker[1].casefold()
        if key not in seen:
            seen.add(key)
            deduped.append(marker)
    markers = deduped
    if not markers:
        return {"records": [], "errors": [_line_error(0, "未识别到包含本批跟踪号及当前状态的 Track718 结果卡片", "")]}

    for marker_index, (start, _raw_number, number) in enumerate(markers):
        if number is None:
            continue  # Use unrequested labeled cards only as block boundaries.
        end = markers[marker_index + 1][0] if marker_index + 1 < len(markers) else len(lines)
        block = lines[start:end]
        status_index = None
        status = "unknown"
        status_text = ""
        for candidate_index in range(start + 1, min(end, start + 12)):
            candidate = lines[candidate_index]
            if not candidate or re.fullmatch(r"(?:={5,}|-{5,})", candidate):
                continue
            field = _TRACK718_FIELD_RE.match(candidate)
            if field and field.group(1) == "包裹状态":
                status_index = candidate_index
                status_text = field.group(2).strip()
                status = _status_from_text(status_text, "")
                break
            if field and field.group(1) == "物流单号":
                break
            if _TIMESTAMP_RE.match(candidate):
                break
            candidate_status = _status_from_text(candidate, "")
            if candidate_status != "unknown":
                status_index, status_text, status = candidate_index, candidate, candidate_status
                break
        if status_index is None or status == "unknown":
            continue

        fields: dict[str, str] = {}
        for line in block:
            field = _TRACK718_FIELD_RE.match(line)
            if field:
                fields[field.group(1)] = field.group(2).strip()
        source_country = fields.get("发件国") or None
        destination_country = fields.get("目的国") or None
        if source_country and source_country.casefold() in {"unknown", "unkown", "未知"}:
            source_country = None
        if destination_country and destination_country.casefold() in {"unknown", "unkown", "未知"}:
            destination_country = None

        parsed_events: list[dict[str, Any]] = []
        for index in range(status_index + 1, end):
            line = block[index - start]
            timestamp = _TIMESTAMP_RE.match(line)
            if not timestamp:
                continue
            remainder = timestamp.group(2).strip()
            location, event_text = _track718_location_event(remainder)
            raw_event = line
            if not remainder:
                for candidate in block[index - start + 1:min(len(block), index - start + 4)]:
                    if not candidate or _TRACK718_UI_LABEL_RE.match(candidate):
                        continue
                    if _TIMESTAMP_RE.match(candidate) or _TRACK718_FIELD_RE.match(candidate):
                        break
                    location, event_text = _track718_location_event(candidate)
                    raw_event = f"{line} | {candidate}"
                    break
            event_at, event_at_raw, _, timezone_unknown = _parse_leading_timestamp(
                line, location or ""
            )
            if event_at is not None and not location:
                timezone_unknown = True
            event_status = _status_from_text("", event_text or remainder)
            if event_status == "unknown":
                event_status = status
            parsed_events.append({
                "tracking_number": number,
                "status": event_status,
                "pkg_status_text": status_text[:300],
                "event_at": event_at,
                "event_at_raw": event_at_raw,
                "event_text": event_text or remainder or None,
                "raw_event": raw_event,
                "location": location,
                "source_country": source_country,
                "destination_country": destination_country,
                "transit_days": None,
                "timezone_unknown": timezone_unknown,
            })

        latest_event = parsed_events[0] if parsed_events else {}
        records.append({
            "tracking_number": number,
            "status": status,
            "pkg_status_text": status_text[:300],
            "event_at": latest_event.get("event_at"),
            "event_at_raw": latest_event.get("event_at_raw"),
            "event_text": latest_event.get("event_text"),
            "raw_event": latest_event.get("raw_event"),
            "location": latest_event.get("location"),
            "country": source_country,
            "origin_country": source_country,
            "source_country": source_country,
            "destination_country": destination_country,
            "transit_days": None,
            "timezone_unknown": latest_event.get("timezone_unknown", False),
            "events": parsed_events,
        })

    if not records:
        return {"records": [], "errors": [_line_error(0, "未识别到包含本批跟踪号及当前状态的 Track718 结果卡片", "")]}
    missing = len(expected) - len({record["tracking_number"].casefold() for record in records})
    if missing:
        errors.append(_line_error(0, f"有 {missing} 个请求号码未在 Track718 页面中得到可识别结果；保持未查", ""))
    return {"records": records, "errors": errors}


__all__ = ["MAX_PAIRS", "parse_pairs", "parse_summary", "parse_usps_summary", "parse_track718_summary"]
