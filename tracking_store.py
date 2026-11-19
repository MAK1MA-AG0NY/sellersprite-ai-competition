# -*- coding: utf-8 -*-
"""Lazy, local-only SQLite storage for manually imported tracking summaries."""
from __future__ import annotations

import datetime as _dt
import re
from typing import Any

import local_db
import tracking_carrier
import tracking_import


def _is_delivered_package(status: Any, pkg_status_text: Any) -> bool:
    """Conservatively recognize a delivered package even in legacy/non-normalized rows."""
    if isinstance(status, str) and status.strip().casefold() == "delivered":
        return True
    text = str(pkg_status_text or "").strip().casefold()
    if not text:
        return False
    negative = ("未妥投", "未送达", "未签收", "未投递", "尚未送达", "投递失败", "派送失败", "未成功")
    if any(term in text for term in negative) or re.search(
            r"\b(?:not delivered|undelivered|delivery failed|failed delivery|return to sender)\b", text, re.I):
        return False
    positive = ("成功签收", "已签收", "已妥投", "已送达", "妥投", "签收成功", "投递成功", "已完成投递")
    return any(term in text for term in positive) or bool(re.search(
        r"\b(?:delivered|signed for|successfully delivered|proof of delivery)\b", text, re.I))


MAX_ORDERS = 500
BATCH_SIZE = 40
_VALID_STATUSES = {
    "delivered", "in_transit", "out_for_delivery", "exception",
    "not_found", "awaiting_pickup",
}
_TRACK_RE = re.compile(r"\A[A-Za-z0-9]{5,50}\Z")
_EXCEPTION_EVENT_RE = re.compile(
    r"\b(?:delivery attempt|notice left|action needed|redelivery|held at post office|"
    r"available for pickup|ready for pickup)\b|到达待取|待取件|等待取件|到达自提点|未取件",
    re.IGNORECASE,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS lt_orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id TEXT NOT NULL,
    tracking_number TEXT NOT NULL,
    created_at TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    status TEXT,
    pkg_status_text TEXT,
    last_event_at TEXT,
    last_event_at_raw TEXT,
    last_event_text TEXT,
    last_location TEXT,
    last_checked_at TEXT,
    last_error TEXT,
    transit_days INTEGER,
    timezone_unknown INTEGER NOT NULL DEFAULT 0,
    source_country TEXT,
    destination_country TEXT,
    carrier_override TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_lt_orders_pair
    ON lt_orders(order_id, tracking_number COLLATE NOCASE);
CREATE INDEX IF NOT EXISTS idx_lt_orders_tracking
    ON lt_orders(tracking_number COLLATE NOCASE);
CREATE TABLE IF NOT EXISTS lt_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tracking_number TEXT NOT NULL,
    event_at TEXT,
    event_at_raw TEXT,
    event_text TEXT,
    raw_event TEXT,
    location TEXT,
    status TEXT,
    pkg_status_text TEXT,
    source_country TEXT,
    destination_country TEXT,
    transit_days INTEGER,
    timezone_unknown INTEGER NOT NULL DEFAULT 0,
    imported_at TEXT NOT NULL,
    source TEXT,
    source_event_id INTEGER,
    source_received_at TEXT,
    raw_eta_text TEXT
);
CREATE INDEX IF NOT EXISTS idx_lt_events_tracking
    ON lt_events(tracking_number COLLATE NOCASE, id DESC);
CREATE TABLE IF NOT EXISTS lt_order_meta (
    order_id TEXT PRIMARY KEY,
    note TEXT NOT NULL DEFAULT '',
    archived_at TEXT
);
CREATE TABLE IF NOT EXISTS lt_order_activity (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    note TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_lt_order_activity_order
    ON lt_order_activity(order_id, created_at DESC, id DESC);
"""


def _conn():
    """Open the application's normal DB connection and idempotently migrate tracking metadata."""
    conn = local_db._conn()
    conn.executescript(_SCHEMA)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(lt_orders)")}
    if "carrier_override" not in columns:
        conn.execute("ALTER TABLE lt_orders ADD COLUMN carrier_override TEXT")
    event_columns = {row[1] for row in conn.execute("PRAGMA table_info(lt_events)")}
    for column, declaration in (
        ("source", "TEXT"),
        ("source_event_id", "INTEGER"),
        ("source_received_at", "TEXT"),
        ("raw_eta_text", "TEXT"),
    ):
        if column not in event_columns:
            conn.execute(f"ALTER TABLE lt_events ADD COLUMN {column} {declaration}")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_lt_events_feta_source_id "
                 "ON lt_events(source, source_event_id) WHERE source='feta' AND source_event_id IS NOT NULL")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_lt_orders_enabled_status ON lt_orders(enabled, status, id)")
    return conn


def _clean_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _valid_pair(pair: Any) -> tuple[str, str]:
    if isinstance(pair, dict):
        order_id = pair.get("order_id")
        tracking_number = pair.get("tracking_number", pair.get("track_number"))
    elif isinstance(pair, (tuple, list)) and len(pair) == 2:
        order_id, tracking_number = pair
    else:
        raise ValueError("每项必须包含 order_id 和 tracking_number")
    if not isinstance(order_id, str) or not isinstance(tracking_number, str):
        raise ValueError("订单号和追踪号必须是文本")
    order_id = order_id.strip()
    tracking_number = tracking_number.strip()
    if not order_id or len(order_id) > 100:
        raise ValueError("订单号须为 1–100 个字符")
    if not _TRACK_RE.fullmatch(tracking_number):
        raise ValueError("追踪号须为 5–50 位字母或数字")
    return order_id, tracking_number


def _aware_datetime(value: Any) -> _dt.datetime | None:
    if isinstance(value, _dt.datetime):
        result = value
    elif isinstance(value, str) and value.strip():
        try:
            result = _dt.datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if result.tzinfo is None or result.utcoffset() is None:
        return None
    return result


def _now_value(now: Any = None) -> _dt.datetime:
    value = _aware_datetime(now)
    if value is not None:
        return value
    if isinstance(now, _dt.datetime):
        return now.astimezone()
    if isinstance(now, str) and now.strip():
        try:
            return _dt.datetime.fromisoformat(now.strip().replace("Z", "+00:00")).astimezone()
        except ValueError:
            pass
    return _dt.datetime.now().astimezone()


def _iso_now(now: Any = None) -> str:
    return _now_value(now).isoformat(timespec="seconds")


def _to_utc(value: _dt.datetime) -> _dt.datetime:
    return value.astimezone(_dt.timezone.utc)


def _elapsed_hours(value: Any, now: _dt.datetime) -> float | None:
    parsed = _aware_datetime(value)
    if parsed is None:
        return None
    hours = (_to_utc(now) - _to_utc(parsed)).total_seconds() / 3600.0
    return round(max(0.0, hours), 1)


def add_pairs(pairs: Any, now: Any = None) -> dict[str, Any]:
    """Add unique order/tracking pairs; reject the whole input on bad rows."""
    if isinstance(pairs, dict):
        pairs = pairs.get("pairs", [])
    if not isinstance(pairs, (list, tuple)):
        raise ValueError("pairs 必须是列表")
    if len(pairs) > MAX_ORDERS:
        raise ValueError(f"最多支持 {MAX_ORDERS} 对")
    cleaned = [_valid_pair(pair) for pair in pairs]
    created_at = _iso_now(now)
    conn = _conn()
    try:
        added = 0
        duplicates = 0
        items: list[dict[str, Any]] = []
        with conn:
            for order_id, tracking_number in cleaned:
                existing = conn.execute(
                    "SELECT id FROM lt_orders WHERE order_id=? AND tracking_number=? COLLATE NOCASE",
                    (order_id, tracking_number),
                ).fetchone()
                if existing:
                    duplicates += 1
                    items.append({"id": existing[0], "order_id": order_id,
                                  "tracking_number": tracking_number, "added": False,
                                  "status": None})
                    continue
                count = conn.execute("SELECT COUNT(*) FROM lt_orders").fetchone()[0]
                if count >= MAX_ORDERS:
                    raise ValueError(f"本地最多保存 {MAX_ORDERS} 个订单追踪项")
                manual_carriers = {row[0] for row in conn.execute(
                    "SELECT DISTINCT carrier_override FROM lt_orders WHERE tracking_number=? COLLATE NOCASE "
                    "AND carrier_override IN ('fedex','usps')", (tracking_number,)).fetchall()}
                if len(manual_carriers) > 1:
                    raise ValueError("同一跟踪号存在冲突的历史人工承运商选择；请先核对后再新增关联")
                inherited = next(iter(manual_carriers), None)
                cursor = conn.execute(
                    "INSERT INTO lt_orders(order_id, tracking_number, created_at, carrier_override) VALUES (?,?,?,?)",
                    (order_id, tracking_number, created_at, inherited),
                )
                added += 1
                items.append({"id": cursor.lastrowid, "order_id": order_id,
                              "tracking_number": tracking_number, "added": True,
                              "status": None})
        return {"added": added, "duplicates": duplicates, "items": items}
    finally:
        conn.close()


def _risk_for(row: dict[str, Any], now: _dt.datetime) -> tuple[float | None, str]:
    status = row.get("status")
    if status == "delivered":
        return None, "delivered"
    if status == "exception":
        return _elapsed_hours(row.get("last_event_at"), now), "exception"
    if status == "not_found":
        # A no-result lookup says nothing about delivery delay.
        return None, "not_found"
    if row.get("timezone_unknown"):
        return None, "时区待确认"
    if row.get("last_event_at"):
        stale_hours = _elapsed_hours(row.get("last_event_at"), now)
        if stale_hours is None:
            return None, "时区待确认"
        if stale_hours >= 48:
            return stale_hours, "stalled"
        return stale_hours, "normal"
    # Without a scan timestamp, only the manual-import/check time is known.
    # This age is informational; it must never be promoted to a stalled alert.
    return _elapsed_hours(row.get("last_checked_at"), now), "awaiting_event"


def _is_exception_case(row: dict[str, Any]) -> bool:
    """Include native exceptions, stalled scans and USPS/actionable pickup cases."""
    status = str(row.get("status") or "").casefold()
    if status in {"delivered", "not_found"}:
        return False
    if status == "exception" or row.get("risk") in {"exception", "stalled"}:
        return True
    stale_hours = row.get("stale_hours")
    if isinstance(stale_hours, (int, float)) and stale_hours >= 48:
        return True
    if status == "awaiting_pickup" and row.get("carrier") == "usps":
        return True
    event_summary = " ".join(str(row.get(key) or "") for key in ("pkg_status_text", "last_event_text"))
    return bool(_EXCEPTION_EVENT_RE.search(event_summary))


def list_orders(now: Any = None, limit: int | None = None, offset: int = 0,
                search: str = "", status: str = "", carrier: str = "",
                archived: bool = False) -> list[dict[str, Any]]:
    """Return a stable page from the active or archived local order view."""
    current = _now_value(now)
    conn = _conn()
    try:
        raw_rows = conn.execute(
            "SELECT o.id, o.order_id, o.tracking_number, o.created_at, o.enabled, o.status, o.pkg_status_text, "
            "o.last_event_at, o.last_event_at_raw, o.last_event_text, o.last_location, o.last_checked_at, "
            "o.last_error, o.transit_days, o.timezone_unknown, o.source_country, o.destination_country, "
            "o.carrier_override, COALESCE(m.note, ''), m.archived_at "
            "FROM lt_orders o LEFT JOIN lt_order_meta m ON m.order_id=o.order_id ORDER BY o.id"
        ).fetchall()
        rows: list[dict[str, Any]] = []
        all_delivered: dict[str, bool] = {}
        for raw in raw_rows:
            carrier_data = tracking_carrier.carrier_value(raw[2], raw[17])
            hint_data = tracking_carrier.suggest_carrier(raw[2])
            row_status = "delivered" if _is_delivered_package(raw[5], raw[6]) else raw[5]
            last_event_at = raw[7]
            timezone_unknown = bool(raw[14])
            if timezone_unknown and not last_event_at:
                last_event_at = tracking_import.assume_central_time(raw[8])
                if last_event_at:
                    timezone_unknown = False
            row = {
                "id": raw[0], "order_id": raw[1], "tracking_number": raw[2],
                "created_at": raw[3], "enabled": bool(raw[4]), "status": row_status,
                "pkg_status_text": raw[6], "last_event_at": last_event_at,
                "last_event_at_raw": raw[8], "last_event_text": raw[9],
                "last_location": raw[10], "last_checked_at": raw[11],
                "last_error": raw[12], "transit_days": raw[13],
                "timezone_unknown": timezone_unknown, "source_country": raw[15],
                "destination_country": raw[16], "carrier_override": raw[17],
                "order_note": raw[18], "archived_at": raw[19], "is_archived": bool(raw[19]),
                "carrier": carrier_data["carrier"], "carrier_source": carrier_data["carrier_source"],
                "carrier_hint": hint_data["carrier_hint"], "carrier_confidence": hint_data["confidence"],
                "carrier_reason": hint_data["reason"],
            }
            all_delivered[row["order_id"]] = all_delivered.get(row["order_id"], True) and row["status"] == "delivered"
            rows.append(row)
        for row in rows:
            row["stale_hours"], row["risk"] = _risk_for(row, current)
            row["exception_bucket"] = _is_exception_case(row)
            row["order_closed"] = bool(all_delivered.get(row["order_id"], False))
        needle = search.strip().casefold()
        if needle:
            rows = [row for row in rows if needle in row["order_id"].casefold()
                    or needle in row["tracking_number"].casefold()
                    or needle in row["order_note"].casefold()]
        rows = [row for row in rows if row["is_archived"] == bool(archived)]
        if status == "needs_update":
            rows = [row for row in rows if row["enabled"]
                    and not _is_delivered_package(row["status"], row["pkg_status_text"])]
        elif status == "exception":
            rows = [row for row in rows if row["exception_bucket"]]
        elif status:
            rows = [row for row in rows if str(row.get("status") or "pending") == status]
        if carrier:
            rows = [row for row in rows if row["carrier"] == carrier]
        if limit is not None:
            start = max(0, int(offset))
            rows = rows[start:start + max(1, min(int(limit), 200))]
        return rows
    finally:
        conn.close()


def order_count(search: str = "", status: str = "", carrier: str = "",
                archived: bool = False) -> int:
    """Count filtered rows in the selected archive view."""
    return len(list_orders(limit=None, search=search, status=status, carrier=carrier,
                           archived=archived))


def preview_pairs(pairs: Any) -> dict[str, Any]:
    """Classify duplicate associations against current local data without writes."""
    if not isinstance(pairs, (list, tuple)):
        raise ValueError("pairs 必须是列表")
    cleaned = [_valid_pair(pair) for pair in pairs]
    seen: set[tuple[str, str]] = set()
    input_duplicates = 0
    unique_pairs = []
    for order_id, tracking_number in cleaned:
        key = (order_id, tracking_number.casefold())
        if key in seen:
            input_duplicates += 1
        else:
            seen.add(key)
            unique_pairs.append((order_id, tracking_number))
    conn = _conn()
    try:
        existing = set()
        for order_id, tracking_number in unique_pairs:
            row = conn.execute("SELECT 1 FROM lt_orders WHERE order_id=? AND tracking_number=? COLLATE NOCASE LIMIT 1",
                               (order_id, tracking_number)).fetchone()
            if row:
                existing.add((order_id, tracking_number.casefold()))
        stored = int(conn.execute("SELECT COUNT(*) FROM lt_orders").fetchone()[0])
        return {"input_duplicates": input_duplicates, "database_duplicates": len(existing),
                "new_associations": max(0, len(unique_pairs) - len(existing)),
                "stored_associations": stored, "capacity": MAX_ORDERS,
                "capacity_ok": stored + max(0, len(unique_pairs) - len(existing)) <= MAX_ORDERS}
    finally:
        conn.close()


def _pending_groups() -> list[dict[str, Any]]:
    """Build a stable, unique list of enabled tracks with at least one open package."""
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT o.tracking_number, o.order_id, o.created_at, o.last_checked_at, o.status, o.carrier_override "
            "FROM lt_orders o WHERE o.enabled=1 AND NOT EXISTS ("
            "SELECT 1 FROM lt_order_meta m WHERE m.order_id=o.order_id AND m.archived_at IS NOT NULL) "
            "ORDER BY o.id"
        ).fetchall()
        checked_tracks = {str(number).casefold() for number, checked_at in conn.execute(
            "SELECT tracking_number, last_checked_at FROM lt_orders "
            "WHERE last_checked_at IS NOT NULL AND trim(last_checked_at)!=''").fetchall()}
        manual_by_track: dict[str, set[str]] = {}
        for number, override in conn.execute(
                "SELECT tracking_number, carrier_override FROM lt_orders "
                "WHERE carrier_override IN ('fedex','usps')").fetchall():
            manual_by_track.setdefault(number.casefold(), set()).add(override)
        groups: dict[str, dict[str, Any]] = {}
        for tracking_number, order_id, created_at, checked_at, status, carrier_override in rows:
            key = tracking_number.casefold()
            hint_data = tracking_carrier.suggest_carrier(tracking_number)
            group = groups.setdefault(key, {
                "tracking_number": tracking_number, "order_ids": [], "created_at": created_at,
                "checked_values": [], "has_open_package": False, "has_previous_check": key in checked_tracks,
                "manual_carriers": set(manual_by_track.get(key, set())),
                "carrier_hint": hint_data["carrier_hint"], "carrier_confidence": hint_data["confidence"],
                "carrier_reason": hint_data["reason"],
            })
            if carrier_override in {"fedex", "usps"}:
                group["manual_carriers"].add(carrier_override)
            if order_id not in group["order_ids"]:
                group["order_ids"].append(order_id)
            group["created_at"] = min(group["created_at"], created_at)
            group["checked_values"].append(checked_at)
            if status != "delivered":
                group["has_open_package"] = True

        def sort_key(group: dict[str, Any]) -> tuple[Any, ...]:
            values = group["checked_values"]
            checked = min(values) if values and all(values) else None
            return (0 if checked is None else 1, checked or "", group["created_at"],
                    group["tracking_number"].casefold())

        eligible = [group for group in groups.values() if group["has_open_package"]]
        for group in eligible:
            manual_carriers = group.pop("manual_carriers")
            if len(manual_carriers) == 1:
                group["carrier"] = next(iter(manual_carriers))
                group["carrier_source"] = "manual"
                group["carrier_conflict"] = False
            elif len(manual_carriers) > 1:
                group["carrier"] = "auto"
                group["carrier_source"] = "conflict"
                group["carrier_conflict"] = True
                group["carrier_confidence"] = "low"
                group["carrier_reason"] = "历史人工承运商选择冲突；待人工确认，不采纳任一旧选择"
            else:
                automatic = tracking_carrier.carrier_value(group["tracking_number"])
                group["carrier"] = automatic["carrier"]
                group["carrier_source"] = automatic["carrier_source"]
                group["carrier_conflict"] = False
        eligible.sort(key=sort_key)
        return eligible
    finally:
        conn.close()


def _candidate_payload(group: dict[str, Any], detailed: bool) -> dict[str, Any]:
    result = {"tracking_number": group["tracking_number"], "order_ids": group["order_ids"]}
    if detailed:
        result.update({key: group[key] for key in (
            "carrier", "carrier_source", "carrier_hint", "carrier_confidence", "carrier_reason", "carrier_conflict")})
    return result


def pending_candidates(carrier: str | None = None, detailed: bool = False,
                       unscanned_only: bool = False) -> list[dict[str, Any]]:
    """Return distinct open tracks; optionally keep only tracks never checked locally."""
    if carrier not in {None, "fedex", "usps", "auto"}:
        raise ValueError("carrier 必须为 fedex、usps、auto 或空")
    if type(unscanned_only) is not bool:
        raise ValueError("unscanned_only 必须是布尔值")
    groups = _pending_groups()
    if unscanned_only:
        groups = [group for group in groups if not group["has_previous_check"]]
    if carrier:
        groups = [group for group in groups if group["carrier"] == carrier]
    return [_candidate_payload(group, detailed) for group in groups]


def next_batch(limit: int = BATCH_SIZE, now: Any = None, detailed: bool = False,
               offset: int = 0) -> list[dict[str, Any]]:
    """Return a capped FedEx-only slice of pending unique tracking numbers."""
    try:
        amount = max(0, min(int(limit), BATCH_SIZE))
    except (TypeError, ValueError):
        amount = BATCH_SIZE
    try:
        start = max(0, int(offset))
    except (TypeError, ValueError):
        start = 0
    if amount == 0:
        return []
    groups = [group for group in _pending_groups() if group["carrier"] == "fedex"]
    never_checked = [group for group in groups
                     if not group["checked_values"] or any(value is None for value in group["checked_values"])]
    selected = never_checked if never_checked else groups
    return [_candidate_payload(group, detailed) for group in selected[start:start + amount]]


def pending_count(carrier: str | None = None) -> int:
    """Count distinct enabled tracks with at least one package not marked delivered."""
    return len(pending_candidates(carrier=carrier))


def _delivered_order_rows(conn: Any) -> list[tuple[int, str, str]]:
    rows = conn.execute(
        "SELECT o.id, o.order_id, o.tracking_number, o.status, o.pkg_status_text "
        "FROM lt_orders o WHERE NOT EXISTS (SELECT 1 FROM lt_order_meta m "
        "WHERE m.order_id=o.order_id AND m.archived_at IS NOT NULL) ORDER BY o.id"
    ).fetchall()
    return [(int(row[0]), str(row[1]), str(row[2])) for row in rows
            if _is_delivered_package(row[3], row[4])]


def delivered_cleanup_preview() -> dict[str, int]:
    """Count explicit delivered package rows and affected orders, without modifying data."""
    conn = _conn()
    try:
        delivered = _delivered_order_rows(conn)
        return {"orders": len({row[1] for row in delivered}), "package_rows": len(delivered)}
    finally:
        conn.close()


def clear_delivered_orders() -> dict[str, int]:
    """Atomically delete explicit delivered package rows and only orphaned events."""
    conn = _conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        delivered = _delivered_order_rows(conn)
        row_ids = [row[0] for row in delivered]
        order_ids = {row[1] for row in delivered}
        tracking_numbers = {row[2] for row in delivered}
        if row_ids:
            conn.executemany("DELETE FROM lt_orders WHERE id=?", [(row_id,) for row_id in row_ids])
        deleted_events = 0
        for number in tracking_numbers:
            still_used = conn.execute(
                "SELECT 1 FROM lt_orders WHERE tracking_number=? COLLATE NOCASE LIMIT 1", (number,)
            ).fetchone()
            if not still_used:
                cursor = conn.execute("DELETE FROM lt_events WHERE tracking_number=? COLLATE NOCASE", (number,))
                deleted_events += max(0, cursor.rowcount)
        for order_id in order_ids:
            still_used = conn.execute("SELECT 1 FROM lt_orders WHERE order_id=? LIMIT 1", (order_id,)).fetchone()
            if not still_used:
                conn.execute("DELETE FROM lt_order_meta WHERE order_id=?", (order_id,))
                conn.execute("DELETE FROM lt_order_activity WHERE order_id=?", (order_id,))
        conn.commit()
        return {"deleted_orders": len(order_ids), "deleted_order_rows": len(row_ids),
                "deleted_event_rows": deleted_events}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _normalize_record(record: Any) -> dict[str, Any] | None:
    if not isinstance(record, dict):
        return None
    tracking_number = record.get("tracking_number", record.get("track_number"))
    if not isinstance(tracking_number, str):
        return None
    tracking_number = tracking_number.strip()
    if not _TRACK_RE.fullmatch(tracking_number):
        return None
    status = record.get("status")
    if not isinstance(status, str):
        status = "unknown"
    status = status.strip().casefold()
    event_at = _aware_datetime(record.get("event_at"))
    event_at_iso = event_at.isoformat(timespec="seconds") if event_at is not None else None
    event_at_raw = _clean_text(record.get("event_at_raw"))
    timezone_unknown = bool(record.get("timezone_unknown")) or bool(event_at_raw and not event_at_iso)
    transit_days = record.get("transit_days")
    if isinstance(transit_days, bool):
        transit_days = None
    elif isinstance(transit_days, int):
        transit_days = transit_days if transit_days >= 0 else None
    elif isinstance(transit_days, str) and transit_days.strip().isdigit():
        transit_days = int(transit_days.strip())
    else:
        transit_days = None
    return {
        "tracking_number": tracking_number,
        "status": status,
        "pkg_status_text": _clean_text(record.get("pkg_status_text")),
        "event_at": event_at_iso,
        "event_at_raw": event_at_raw,
        "event_text": _clean_text(record.get("event_text")),
        "raw_event": _clean_text(record.get("raw_event")),
        "location": _clean_text(record.get("location")),
        "source_country": _clean_text(record.get("source_country", record.get("origin_country", record.get("country")))),
        "destination_country": _clean_text(record.get("destination_country")),
        "transit_days": transit_days,
        "timezone_unknown": timezone_unknown,
    }


def _insert_event(conn: Any, record: dict[str, Any], imported_at: str) -> bool:
    if not any(record.get(key) for key in ("event_at", "event_at_raw", "event_text", "raw_event", "location")):
        return False
    identity = (
        record["tracking_number"].casefold(), record.get("event_at") or "",
        record.get("event_at_raw") or "", record.get("event_text") or "",
        record.get("location") or "", record["status"],
    )
    exists = conn.execute(
        "SELECT 1 FROM lt_events WHERE lower(tracking_number)=? "
        "AND COALESCE(event_at,'')=? AND COALESCE(event_at_raw,'')=? "
        "AND COALESCE(event_text,'')=? AND COALESCE(location,'')=? AND status=? LIMIT 1",
        identity,
    ).fetchone()
    if exists:
        return False
    conn.execute(
        "INSERT INTO lt_events(tracking_number, event_at, event_at_raw, event_text, raw_event, "
        "location, status, pkg_status_text, source_country, destination_country, transit_days, "
        "timezone_unknown, imported_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (record["tracking_number"], record["event_at"], record["event_at_raw"],
         record["event_text"], record["raw_event"], record["location"], record["status"],
         record["pkg_status_text"], record["source_country"], record["destination_country"],
         record["transit_days"], int(record["timezone_unknown"]), imported_at),
    )
    return True


_STATUS_AUTHORITY = {
    "not_found": 0,
    "awaiting_pickup": 1,
    "in_transit": 2,
    "out_for_delivery": 3,
    "exception": 4,
    "delivered": 5,
}


def _new_event_is_usable(record: dict[str, Any], old_event_at: str | None,
                         old_status: str | None) -> bool:
    if old_status == "delivered" and record["status"] != "delivered":
        return False  # delivery is terminal; ordinary in-transit imports cannot undo it
    new_at = _aware_datetime(record.get("event_at"))
    old_at = _aware_datetime(old_event_at)
    if new_at is not None:
        if old_at is None or _to_utc(new_at) > _to_utc(old_at):
            return True
        if _to_utc(new_at) < _to_utc(old_at):
            return False
        # A repeated scan timestamp can still carry a more authoritative
        # package status (notably a Delivered summary). Equal-time regressions
        # are rejected; only an explicit status upgrade changes the current row.
        return _STATUS_AUTHORITY.get(record["status"], 0) > _STATUS_AUTHORITY.get(old_status, 0)
    # A status change with no interpretable scan time can update the package
    # summary only; it cannot replace a known, timestamped latest event.
    return old_status != record["status"] or old_status is None


def import_records(records: Any, now: Any = None) -> dict[str, Any]:
    """Apply parsed summary rows to all matching order rows.

    Unknown statuses keep previous shipment data. A matching lookup still
    refreshes ``last_checked_at``; each event is recorded at most once.
    """
    if isinstance(records, dict):
        records = records.get("records", [])
    if not isinstance(records, (list, tuple)):
        return {"matched": 0, "unmatched": 0, "items": []}
    checked_at = _iso_now(now)
    matched_count = 0
    unmatched_count = 0
    items: list[dict[str, Any]] = []
    conn = _conn()
    try:
        with conn:
            for original in records:
                record = _normalize_record(original)
                if record is None:
                    unmatched_count += 1
                    items.append({"tracking_number": None, "status": None,
                                  "matched": False, "order_ids": [], "updated": 0})
                    continue
                matches = conn.execute(
                    "SELECT id, order_id, status, last_event_at FROM lt_orders "
                    "WHERE tracking_number=? COLLATE NOCASE ORDER BY id",
                    (record["tracking_number"],),
                ).fetchall()
                if not matches:
                    unmatched_count += 1
                    items.append({"tracking_number": record["tracking_number"],
                                  "status": record["status"], "matched": False,
                                  "order_ids": [], "updated": 0})
                    continue

                matched_count += 1
                order_ids = list(dict.fromkeys(row[1] for row in matches))
                conn.execute(
                    "UPDATE lt_orders SET last_checked_at=? WHERE tracking_number=? COLLATE NOCASE",
                    (checked_at, record["tracking_number"]),
                )
                if record["status"] in _VALID_STATUSES:
                    history = original.get("events") if isinstance(original, dict) else None
                    if isinstance(history, (list, tuple)):
                        for event in history[:500]:
                            if not isinstance(event, dict):
                                continue
                            event_input = {**original, **event,
                                           "tracking_number": record["tracking_number"]}
                            event_record = _normalize_record(event_input)
                            if event_record and event_record["status"] in _VALID_STATUSES:
                                _insert_event(conn, event_record, checked_at)
                    else:
                        _insert_event(conn, record, checked_at)
                updated = 0
                if record["status"] in _VALID_STATUSES:
                    for row_id, _order_id, old_status, old_event_at in matches:
                        if not _new_event_is_usable(record, old_event_at, old_status):
                            continue
                        fields: dict[str, Any] = {
                            "status": record["status"],
                            "pkg_status_text": record["pkg_status_text"],
                        }
                        # Do not erase a previous latest scan when the pasted
                        # summary has only a status and no timestamp/event.
                        if record["event_at"] is not None:
                            fields.update({
                                "last_event_at": record["event_at"],
                                "last_event_at_raw": record["event_at_raw"],
                                "last_event_text": record["event_text"],
                                "last_location": record["location"],
                                "timezone_unknown": int(record["timezone_unknown"]),
                            })
                        elif record["timezone_unknown"] and record["event_at_raw"]:
                            # A changed status with a new, unzoned local scan
                            # must suppress stale calculations based on an older
                            # normalized event; preserve the copied local time.
                            fields.update({
                                "last_event_at": None,
                                "last_event_at_raw": record["event_at_raw"],
                                "last_event_text": record["event_text"] or record["raw_event"],
                                "last_location": record["location"],
                                "timezone_unknown": 1,
                            })
                        elif old_event_at is None and any(record.get(key) for key in (
                                "event_at_raw", "event_text", "raw_event", "location")):
                            fields.update({
                                "last_event_at": None,
                                "last_event_at_raw": record["event_at_raw"],
                                "last_event_text": record["event_text"] or record["raw_event"],
                                "last_location": record["location"],
                                "timezone_unknown": int(record["timezone_unknown"]),
                            })
                        if record["transit_days"] is not None:
                            fields["transit_days"] = record["transit_days"]
                        if record["source_country"] is not None:
                            fields["source_country"] = record["source_country"]
                        if record["destination_country"] is not None:
                            fields["destination_country"] = record["destination_country"]
                        assignments = ", ".join(f"{key}=?" for key in fields)
                        conn.execute(f"UPDATE lt_orders SET {assignments} WHERE id=?",
                                     [*fields.values(), row_id])
                        updated += 1
                items.append({"tracking_number": record["tracking_number"],
                              "status": record["status"], "matched": True,
                              "order_ids": order_ids, "updated": updated})
        return {"matched": matched_count, "unmatched": unmatched_count, "items": items}
    finally:
        conn.close()


_FETA_STATUS_MAP = {
    "IN_TRANSIT": "in_transit",
    "DELIVERED": "delivered",
}
_FETA_NORMALIZED_STATUSES = {
    "UNKNOWN", "LABEL_CREATED", "PICKED_UP", "IN_TRANSIT", "DELAYED", "EXCEPTION",
    "OUT_FOR_DELIVERY", "DELIVERY_ATTEMPTED", "RETURN_TO_SENDER", "DELIVERED", "RETURNED",
}


def feta_target_status(normalized_status: Any) -> str | None:
    """Return only the conservative FedEx-to-local status mappings we support."""
    if not isinstance(normalized_status, str):
        return None
    return _FETA_STATUS_MAP.get(normalized_status.strip().upper())


def apply_feta_event(event: Any, now: Any = None) -> dict[str, Any]:
    """Import one manually accepted FETA observation without fabricating a scan node.

    FETA's received_at_utc is the email arrival time, not a carrier scan time. It is
    stored as source_received_at and may advance last_checked_at, but never last_event_at.
    Only IN_TRANSIT and DELIVERED have a direct local status mapping; all other parsed
    statuses remain audit-only until the local model has an explicit equivalent.
    """
    if not isinstance(event, dict):
        raise ValueError("FedEx 邮件事件格式无效")
    event_id = event.get("id")
    if isinstance(event_id, bool) or not isinstance(event_id, int) or event_id <= 0:
        raise ValueError("FedEx 邮件事件 ID 无效")
    tracking_number = event.get("tracking_number")
    if not isinstance(tracking_number, str) or not _TRACK_RE.fullmatch(tracking_number.strip()):
        raise ValueError("FedEx 邮件事件跟踪号格式无效")
    tracking_number = tracking_number.strip()
    normalized = event.get("normalized_status")
    if not isinstance(normalized, str):
        raise ValueError("FedEx 邮件事件状态无效")
    normalized = normalized.strip().upper()
    if normalized not in _FETA_NORMALIZED_STATUSES:
        raise ValueError("FedEx 邮件事件状态不在允许列表")
    received = _aware_datetime(event.get("received_at_utc"))
    if received is None:
        raise ValueError("FedEx 邮件事件收件时间无有效时区")
    received_iso = _to_utc(received).isoformat(timespec="seconds")
    imported_at = _iso_now(now)
    raw_status_value = event.get("raw_status")
    raw_status = raw_status_value[:500] if isinstance(raw_status_value, str) else normalized
    eta_value = event.get("raw_eta_text")
    raw_eta_text = eta_value[:500] if isinstance(eta_value, str) and eta_value else None
    target_status = feta_target_status(normalized)

    conn = _conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute(
            "SELECT 1 FROM lt_events WHERE source='feta' AND source_event_id=? LIMIT 1", (event_id,)
        ).fetchone()
        if existing:
            conn.commit()
            return {"event_id": event_id, "tracking_number": tracking_number, "matched": True,
                    "matched_orders": 0, "status_updated": 0, "event_added": False, "duplicate": True}
        matches = conn.execute(
            "SELECT id, order_id, status, pkg_status_text, last_checked_at FROM lt_orders "
            "WHERE tracking_number=? COLLATE NOCASE ORDER BY id", (tracking_number,)
        ).fetchall()
        if not matches:
            conn.rollback()
            return {"event_id": event_id, "tracking_number": tracking_number, "matched": False,
                    "matched_orders": 0, "status_updated": 0, "event_added": False, "duplicate": False}

        conn.execute(
            "INSERT INTO lt_events(tracking_number,event_at,event_at_raw,event_text,raw_event,location,status,"
            "pkg_status_text,source_country,destination_country,transit_days,timezone_unknown,imported_at,source,"
            "source_event_id,source_received_at,raw_eta_text) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (tracking_number, None, None, "FedEx 邮件查询结果（人工复核）", raw_status, None,
             target_status, raw_status, None, None, None, 0, imported_at, "feta", event_id,
             received_iso, raw_eta_text),
        )
        status_updated = 0
        stale_observations = 0
        for row in matches:
            row_id, _order_id, old_status, old_pkg_text, old_checked_text = row
            old_checked = _aware_datetime(old_checked_text)
            if old_checked_text and (old_checked is None or _to_utc(received) < _to_utc(old_checked)):
                stale_observations += 1
                continue
            fields: dict[str, Any] = {"last_checked_at": received_iso}
            old_status_key = str(old_status or "").strip().casefold().replace("-", "_").replace(" ", "_")
            old_pkg_key = str(old_pkg_text or "").strip().casefold()
            is_returned = (old_status_key in {"returned", "return_to_sender"}
                           or bool(re.search(r"\b(?:returned|return to sender)\b", old_pkg_key)))
            is_terminal = _is_delivered_package(old_status, old_pkg_text) or is_returned
            if target_status and (not is_terminal or target_status == "delivered"):
                old_rank = _STATUS_AUTHORITY.get(old_status_key, -1)
                new_rank = _STATUS_AUTHORITY.get(target_status, -1)
                if new_rank >= old_rank:
                    fields.update({"status": target_status, "pkg_status_text": raw_status})
                    if old_status_key != target_status:
                        status_updated += 1
            assignments = ", ".join(f"{key}=?" for key in fields)
            conn.execute(f"UPDATE lt_orders SET {assignments} WHERE id=?", [*fields.values(), row_id])
        conn.commit()
        return {"event_id": event_id, "tracking_number": tracking_number, "matched": True,
                "matched_orders": len(matches), "status_updated": status_updated,
                "stale_observations": stale_observations, "event_added": True, "duplicate": False,
                "status": target_status}
    except BaseException:
        if conn.in_transaction:
            conn.rollback()
        raise
    finally:
        conn.close()


def set_carrier(order_row_id: int, carrier: str) -> bool:
    if carrier not in {"auto", "fedex", "usps"}:
        raise ValueError("carrier 必须为 auto、fedex 或 usps")
    conn = _conn()
    try:
        with conn:
            row = conn.execute("SELECT tracking_number FROM lt_orders WHERE id=?", (order_row_id,)).fetchone()
            if not row:
                return False
            override = None if carrier == "auto" else carrier
            conn.execute("UPDATE lt_orders SET carrier_override=? WHERE tracking_number=? COLLATE NOCASE",
                         (override, row[0]))
        return True
    finally:
        conn.close()


_MAX_ORDER_NOTE_CHARS = 2000


def _validated_order_note(note: Any) -> str:
    if not isinstance(note, str):
        raise ValueError("备注必须是文本")
    if len(note) > _MAX_ORDER_NOTE_CHARS:
        raise ValueError(f"备注最多 {_MAX_ORDER_NOTE_CHARS} 个字符")
    return note.strip()


def set_order_note(order_row_id: int, note: Any) -> dict[str, str]:
    """Save one internal note shared by all tracking rows of the order."""
    clean_note = _validated_order_note(note)
    conn = _conn()
    try:
        with conn:
            row = conn.execute("SELECT order_id FROM lt_orders WHERE id=?", (order_row_id,)).fetchone()
            if not row:
                raise ValueError("订单追踪项不存在")
            order_id = str(row[0])
            conn.execute(
                "INSERT INTO lt_order_meta(order_id, note) VALUES (?, ?) "
                "ON CONFLICT(order_id) DO UPDATE SET note=excluded.note",
                (order_id, clean_note),
            )
        return {"order_id": order_id, "note": clean_note}
    finally:
        conn.close()


def archive_order(order_row_id: int, note: Any = "", now: Any = None) -> dict[str, str]:
    """Archive a complete logical order and append an auditable order event."""
    clean_note = _validated_order_note(note)
    archived_at = _iso_now(now)
    conn = _conn()
    try:
        with conn:
            row = conn.execute("SELECT order_id FROM lt_orders WHERE id=?", (order_row_id,)).fetchone()
            if not row:
                raise ValueError("订单追踪项不存在")
            order_id = str(row[0])
            meta = conn.execute(
                "SELECT archived_at FROM lt_order_meta WHERE order_id=?", (order_id,)
            ).fetchone()
            if meta and meta[0]:
                raise ValueError("订单已经归档")
            conn.execute(
                "INSERT INTO lt_order_meta(order_id, archived_at) VALUES (?, ?) "
                "ON CONFLICT(order_id) DO UPDATE SET archived_at=excluded.archived_at",
                (order_id, archived_at),
            )
            conn.execute(
                "INSERT INTO lt_order_activity(order_id, event_type, note, created_at) VALUES (?,?,?,?)",
                (order_id, "archived", clean_note or None, archived_at),
            )
        return {"order_id": order_id, "archived_at": archived_at}
    finally:
        conn.close()


def restore_order(order_row_id: int, now: Any = None) -> dict[str, str]:
    """Restore an archived logical order and record the reversal in its history."""
    restored_at = _iso_now(now)
    conn = _conn()
    try:
        with conn:
            row = conn.execute("SELECT order_id FROM lt_orders WHERE id=?", (order_row_id,)).fetchone()
            if not row:
                raise ValueError("订单追踪项不存在")
            order_id = str(row[0])
            meta = conn.execute(
                "SELECT archived_at FROM lt_order_meta WHERE order_id=?", (order_id,)
            ).fetchone()
            if not meta or not meta[0]:
                raise ValueError("订单当前不在归档中")
            conn.execute("UPDATE lt_order_meta SET archived_at=NULL WHERE order_id=?", (order_id,))
            conn.execute(
                "INSERT INTO lt_order_activity(order_id, event_type, note, created_at) VALUES (?,?,?,?)",
                (order_id, "restored", None, restored_at),
            )
        return {"order_id": order_id, "restored_at": restored_at}
    finally:
        conn.close()


def set_enabled(order_row_id: int, enabled: bool) -> bool:
    if not isinstance(enabled, bool):
        raise ValueError("enabled 必须是布尔值")
    conn = _conn()
    try:
        with conn:
            cursor = conn.execute("UPDATE lt_orders SET enabled=? WHERE id=?",
                                  (int(enabled), order_row_id))
        return cursor.rowcount > 0
    finally:
        conn.close()


def remove_order(order_row_id: int) -> bool:
    conn = _conn()
    try:
        with conn:
            row = conn.execute("SELECT order_id, tracking_number FROM lt_orders WHERE id=?",
                               (order_row_id,)).fetchone()
            if not row:
                return False
            order_id, tracking_number = row
            conn.execute("DELETE FROM lt_orders WHERE id=?", (order_row_id,))
            still_used = conn.execute(
                "SELECT 1 FROM lt_orders WHERE tracking_number=? COLLATE NOCASE LIMIT 1",
                (tracking_number,),
            ).fetchone()
            if not still_used:
                conn.execute("DELETE FROM lt_events WHERE tracking_number=? COLLATE NOCASE",
                             (tracking_number,))
            order_still_used = conn.execute("SELECT 1 FROM lt_orders WHERE order_id=? LIMIT 1",
                                            (order_id,)).fetchone()
            if not order_still_used:
                conn.execute("DELETE FROM lt_order_meta WHERE order_id=?", (order_id,))
                conn.execute("DELETE FROM lt_order_activity WHERE order_id=?", (order_id,))
        return True
    finally:
        conn.close()


def events(order_row_id: int) -> list[dict[str, Any]]:
    conn = _conn()
    try:
        row = conn.execute("SELECT order_id, tracking_number FROM lt_orders WHERE id=?",
                           (order_row_id,)).fetchone()
        if not row:
            return []
        order_id, tracking_number = row
        rows = conn.execute(
            "SELECT id, tracking_number, event_at, event_at_raw, event_text, raw_event, location, "
            "status, pkg_status_text, source_country, destination_country, transit_days, "
            "timezone_unknown, imported_at, source, source_event_id, source_received_at, raw_eta_text FROM lt_events "
            "WHERE tracking_number=? COLLATE NOCASE ORDER BY "
            "CASE WHEN event_at IS NULL THEN 1 ELSE 0 END, event_at DESC, id DESC",
            (tracking_number,),
        ).fetchall()
        result = []
        for item in rows:
            event_at = item[2]
            timezone_unknown = bool(item[12])
            if timezone_unknown and not event_at:
                event_at = tracking_import.assume_central_time(item[3])
                if event_at:
                    timezone_unknown = False
            result.append({
                "id": item[0], "order_id": order_id, "tracking_number": item[1], "event_at": event_at,
                "event_at_raw": item[3], "event_text": item[4], "raw_event": item[5],
                "location": item[6], "status": item[7], "pkg_status_text": item[8],
                "source_country": item[9], "destination_country": item[10],
                "transit_days": item[11], "timezone_unknown": timezone_unknown,
                "imported_at": item[13], "source": item[14], "source_event_id": item[15],
                "source_received_at": item[16], "raw_eta_text": item[17],
            })
        activities = conn.execute(
            "SELECT id, event_type, note, created_at FROM lt_order_activity "
            "WHERE order_id=? ORDER BY created_at DESC, id DESC", (order_id,)
        ).fetchall()
        activity_labels = {"archived": "订单归档", "restored": "恢复归档"}
        for item in activities:
            result.append({
                "id": item[0], "order_id": order_id, "tracking_number": None,
                "event_at": item[3], "event_at_raw": item[3], "event_text": item[2],
                "raw_event": None, "location": None, "status": item[1],
                "event_type": item[1], "pkg_status_text": activity_labels.get(item[1], "订单事件"),
                "source_country": None, "destination_country": None, "transit_days": None,
                "timezone_unknown": False, "imported_at": item[3], "source": "order_activity",
                "source_event_id": None, "source_received_at": None, "raw_eta_text": None,
            })

        def event_sort_key(event: dict[str, Any]):
            parsed = _aware_datetime(event.get("event_at") or event.get("imported_at")
                                     or event.get("event_at_raw"))
            timestamp = _to_utc(parsed) if parsed else _dt.datetime.min.replace(tzinfo=_dt.timezone.utc)
            return timestamp, int(event.get("id") or 0)

        result.sort(key=event_sort_key, reverse=True)
        return result
    finally:
        conn.close()


__all__ = [
    "MAX_ORDERS", "BATCH_SIZE", "add_pairs", "list_orders", "order_count", "preview_pairs",
    "pending_candidates", "next_batch", "pending_count", "delivered_cleanup_preview", "clear_delivered_orders",
    "import_records", "feta_target_status", "apply_feta_event", "set_carrier", "set_enabled",
    "set_order_note", "archive_order", "restore_order", "events", "remove_order",
]
