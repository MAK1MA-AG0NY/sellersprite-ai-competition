# -*- coding: utf-8 -*-
"""Parse order/tracking pairs and manually copied 17TRACK summary text.

This module is deliberately offline: it does not open a browser or contact any
tracking service. Timestamps copied from the summary have no timezone, so a
small, explicit US-local-time inference is used only when the copied location
is sufficiently specific.
"""
from __future__ import annotations

import calendar
import datetime as _dt
import re
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
                 observes_dst: bool) -> None:
        self._standard_offset = _dt.timedelta(hours=offset_hours)
        self._standard = standard
        self._daylight = daylight
        self._observes_dst = observes_dst

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


def _timezone_for_location(location: str, year: int) -> _USLocalTimezone | None:
    normalized = re.sub(r"\s+", " ", location.strip(" ,")).casefold()
    zone = _CITY_ZONE.get(normalized)
    if zone is None:
        match = re.search(r",\s*([A-Z]{2})\s*$", location.strip(), re.IGNORECASE)
        if not match:
            return None
        zone = _STATE_ZONE.get(match.group(1).upper())
    if zone is None:
        return None
    if zone[3] and year < 2007:
        # The custom rule below is the post-2007 US rule. Do not claim a
        # confident offset for historical years whose DST laws differ.
        return None
    return _USLocalTimezone(*zone)


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
    if tz is None or tz.is_nonexistent_or_ambiguous(naive):
        return None, timestamp_raw, remainder, True
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
    """Map 17TRACK Chinese status labels, then fall back to English wording."""
    primary = status_text.casefold().replace(" ", "")
    combined = f"{status_text} {event_text}".casefold()

    # Search explicit Chinese status values first; this column is more
    # authoritative than a possibly historical English event description.
    chinese_groups = (
        ("not_found", ("查询不到", "未查询到", "查无信息", "暂无信息", "暂无跟踪信息", "无查询结果")),
        ("delivered", ("成功签收", "已签收", "已妥投", "已送达", "投递成功")),
        ("out_for_delivery", ("派送途中", "派送中", "正在派送", "派件中")),
        ("exception", ("投递失败", "可能异常", "异常", "问题件")),
        ("awaiting_pickup", ("待取件", "等待取件", "到达自提点", "可供取件")),
        ("in_transit", ("运输途中", "运输中", "运输", "已发货", "处理中")),
    )
    for status, terms in chinese_groups:
        if any(term in primary for term in terms):
            return status

    english_groups = (
        ("not_found", ("not found", "no information", "unable to find", "not available")),
        ("delivered", ("delivered", "signed for", "successfully delivered")),
        ("exception", ("delivery failed", "delivery exception", "exception", "possible exception", "alert")),
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


def parse_pairs(text: str) -> dict[str, list[dict[str, str]]]:
    """Parse newline-separated ``order_id<TAB>tracking_number`` pairs.

    A comma is accepted when a row has no tab. Tabs always take precedence,
    which preserves order IDs containing commas in Excel-copied data.
    """
    pairs: list[dict[str, str]] = []
    errors: list[dict[str, Any]] = []
    if not isinstance(text, str):
        return {"pairs": pairs, "errors": [_line_error(0, "输入必须是文本", repr(text))]}

    nonblank_rows = 0
    for line_number, raw_line in enumerate(text.splitlines(), 1):
        line = raw_line.strip("\ufeff\r\n")
        if not line.strip():
            continue
        nonblank_rows += 1
        if nonblank_rows > MAX_PAIRS:
            errors.append(_line_error(line_number, f"最多支持 {MAX_PAIRS} 对", raw_line))
            continue
        delimiter = "\t" if "\t" in line else ","
        cells = line.split(delimiter)
        if len(cells) != 2:
            errors.append(_line_error(line_number, "每行必须恰好包含订单号和追踪号", raw_line))
            continue
        validated = _validate_pair(cells[0], cells[1])
        if validated is None:
            errors.append(_line_error(line_number, "订单号须为 1–100 字符，追踪号须为 5–50 位字母或数字", raw_line))
            continue
        order_id, tracking_number = validated
        pairs.append({"order_id": order_id, "tracking_number": tracking_number})
    return {"pairs": pairs, "errors": errors}


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


__all__ = ["MAX_PAIRS", "parse_pairs", "parse_summary"]
