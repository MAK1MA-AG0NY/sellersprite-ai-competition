"""Strict CSV intake for FedEx tracking rows."""
from __future__ import annotations

import csv
from pathlib import Path
import os
import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .repository import import_rows

_REQUIRED = {"order_id", "tracking_number", "carrier", "destination_timezone", "active"}
_INVISIBLE = {"\u200b", "\u200c", "\u200d", "\ufeff"}
_TZ_NAME_RE = re.compile(r"(?:UTC|[A-Za-z][A-Za-z0-9_+-]*(?:/[A-Za-z0-9_+.-]+)+)\Z")
_WINDOWS_NO_TZDATA_US_ZONES = {
    "UTC", "America/Adak", "America/Anchorage", "America/Boise", "America/Chicago",
    "America/Denver", "America/Detroit", "America/Indiana/Indianapolis", "America/Juneau",
    "America/Kentucky/Louisville", "America/Los_Angeles", "America/New_York", "America/Phoenix",
    "America/Puerto_Rico", "America/Sitka", "America/Yakutat", "Pacific/Honolulu",
}


def validate_timezone_name(name: str) -> None:
    if not _TZ_NAME_RE.fullmatch(name) or any(part in {"", ".", ".."} for part in name.split("/")):
        raise ValueError("destination_timezone 不是有效 IANA 时区名称")
    try:
        ZoneInfo(name)
    except ZoneInfoNotFoundError as exc:
        # Windows may lack TZif files. Accept only a small, explicit US fallback list;
        # unknown IANA-looking names must never silently pass as valid time zones.
        if os.name == "nt":
            try:
                ZoneInfo("America/Chicago")
            except ZoneInfoNotFoundError:
                if name in _WINDOWS_NO_TZDATA_US_ZONES:
                    return
        raise ValueError("destination_timezone 未在本机 IANA 数据库中确认；安装 tzdata 后重试") from exc


def normalize_tracking_number(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("跟踪号必须是字符串")
    cleaned = "".join(ch for ch in value if not ch.isspace() and ch not in _INVISIBLE)
    if not cleaned or len(cleaned) > 50 or not re.fullmatch(r"[A-Za-z0-9-]+", cleaned):
        raise ValueError("跟踪号须为 1–50 位 ASCII 字母、数字或连字符；不会强制固定长度")
    return cleaned


def _active(value: str) -> bool:
    normalized = value.strip().casefold()
    if normalized in {"1", "true", "yes", "active"}:
        return True
    if normalized in {"0", "false", "no", "inactive"}:
        return False
    raise ValueError("active 只接受 1/0、true/false、yes/no")


def parse_csv(source: str | Path | bytes) -> list[dict]:
    if isinstance(source, bytes):
        try:
            text = source.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise ValueError("CSV 必须使用 UTF-8 编码") from exc
        from io import StringIO
        stream = StringIO(text, newline="")
    else:
        path = Path(source)
        try:
            stream = path.open("r", encoding="utf-8-sig", newline="")
        except OSError as exc:
            raise ValueError(f"无法读取 CSV：{path.name}") from exc

    try:
        reader = csv.DictReader(stream)
        headers = {str(name or "").strip() for name in (reader.fieldnames or [])}
        missing = _REQUIRED - headers
        if missing:
            raise ValueError("CSV 缺少列：" + ", ".join(sorted(missing)))
        rows = []
        for line, record in enumerate(reader, 2):
            try:
                if None in record:
                    raise ValueError("列数与表头不一致")
                order_id = str(record.get("order_id") or "").strip()
                if not order_id or len(order_id) > 160:
                    raise ValueError("order_id 不能为空且最多 160 字符")
                tracking = normalize_tracking_number(str(record.get("tracking_number") or ""))
                carrier = str(record.get("carrier") or "").strip()
                if carrier.casefold() != "fedex":
                    raise ValueError("当前 FETA 只接受 carrier=FedEx")
                timezone_name = str(record.get("destination_timezone") or "").strip()
                if not timezone_name:
                    raise ValueError("destination_timezone 不能为空")
                try:
                    validate_timezone_name(timezone_name)
                except ValueError as exc:
                    raise ValueError(str(exc)) from exc
                rows.append({"order_id": order_id, "tracking_number": tracking, "carrier": "FedEx",
                             "destination_timezone": timezone_name, "active": _active(str(record.get("active") or ""))})
            except ValueError as exc:
                raise ValueError(f"第 {line} 行：{exc}") from exc
        return rows
    finally:
        stream.close()


def import_csv(source: str | Path | bytes, db_path=None, now=None) -> dict:
    return import_rows(parse_csv(source), now=now, db_path=db_path)
