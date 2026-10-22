# -*- coding: utf-8 -*-
"""Lazy, local-only SQLite storage for manually imported tracking summaries."""
from __future__ import annotations

import datetime as _dt
import re
from typing import Any

import local_db


def _is_delivered_package(status: Any, pkg_status_text: Any) -> bool:
    """Conservatively recognize explicit Delivered text in legacy rows."""
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
    destination_country TEXT
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
"""


def _conn():
    """Open the application's normal DB connection and lazily add our tables."""
    conn = local_db._conn()
    conn.executescript(_SCHEMA)
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
                cursor = conn.execute(
                    "INSERT INTO lt_orders(order_id, tracking_number, created_at) VALUES (?,?,?)",
                    (order_id, tracking_number, created_at),
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


def list_orders(now: Any = None) -> list[dict[str, Any]]:
    """Return stored order rows with derived stale age, risk and closure state."""
    current = _now_value(now)
    conn = _conn()
    try:
        raw_rows = conn.execute(
            "SELECT id, order_id, tracking_number, created_at, enabled, status, pkg_status_text, "
            "last_event_at, last_event_at_raw, last_event_text, last_location, last_checked_at, "
            "last_error, transit_days, timezone_unknown, source_country, destination_country "
            "FROM lt_orders ORDER BY id"
        ).fetchall()
        rows: list[dict[str, Any]] = []
        all_delivered: dict[str, bool] = {}
        for raw in raw_rows:
            row = {
                "id": raw[0], "order_id": raw[1], "tracking_number": raw[2],
                "created_at": raw[3], "enabled": bool(raw[4]), "status": raw[5],
                "pkg_status_text": raw[6], "last_event_at": raw[7],
                "last_event_at_raw": raw[8], "last_event_text": raw[9],
                "last_location": raw[10], "last_checked_at": raw[11],
                "last_error": raw[12], "transit_days": raw[13],
                "timezone_unknown": bool(raw[14]), "source_country": raw[15],
                "destination_country": raw[16],
            }
            all_delivered[row["order_id"]] = all_delivered.get(row["order_id"], True) and row["status"] == "delivered"
            rows.append(row)
        for row in rows:
            row["stale_hours"], row["risk"] = _risk_for(row, current)
            row["order_closed"] = bool(all_delivered.get(row["order_id"], False))
        return rows
    finally:
        conn.close()


def next_batch(limit: int = BATCH_SIZE, now: Any = None) -> list[dict[str, Any]]:
    """Return distinct enabled, non-delivered tracks, least recently checked first."""
    try:
        amount = max(0, min(int(limit), BATCH_SIZE))
    except (TypeError, ValueError):
        amount = BATCH_SIZE
    if amount == 0:
        return []
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT tracking_number, order_id, created_at, last_checked_at, status "
            "FROM lt_orders WHERE enabled=1 ORDER BY id"
        ).fetchall()
        groups: dict[str, dict[str, Any]] = {}
        for tracking_number, order_id, created_at, checked_at, status in rows:
            key = tracking_number.casefold()
            group = groups.setdefault(key, {
                "tracking_number": tracking_number, "order_ids": [], "created_at": created_at,
                "checked_values": [], "has_open_package": False,
            })
            if order_id not in group["order_ids"]:
                group["order_ids"].append(order_id)
            group["created_at"] = min(group["created_at"], created_at)
            group["checked_values"].append(checked_at)
            if status != "delivered":
                group["has_open_package"] = True

        def sort_key(group: dict[str, Any]) -> tuple[Any, ...]:
            values = group["checked_values"]
            # A never-checked track is oldest. Otherwise use the oldest check
            # among its enabled, linked order rows.
            checked = min(values) if values and all(values) else None
            return (0 if checked is None else 1, checked or "", group["created_at"],
                    group["tracking_number"].casefold())

        eligible = [group for group in groups.values() if group["has_open_package"]]
        eligible.sort(key=sort_key)
        # Finish the never-queried remainder before cycling through old scans;
        # otherwise a final partial batch would be padded with repeats.
        never_checked = [group for group in eligible
                         if not group["checked_values"] or any(value is None for value in group["checked_values"])]
        selected = never_checked if never_checked else eligible
        return [{"tracking_number": group["tracking_number"],
                 "order_ids": group["order_ids"]}
                for group in selected[:amount]]
    finally:
        conn.close()


def pending_count() -> int:
    """Count distinct enabled tracking numbers that are not fully delivered."""
    conn = _conn()
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM ("
            "SELECT lower(tracking_number) FROM lt_orders WHERE enabled=1 "
            "GROUP BY lower(tracking_number) "
            "HAVING MAX(CASE WHEN status IS NULL OR status!='delivered' THEN 1 ELSE 0 END)=1"
            ")"
        ).fetchone()
        return int(row[0])
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


_FETA_STATUS_MAP = {"IN_TRANSIT": "in_transit", "DELIVERED": "delivered"}
_FETA_NORMALIZED_STATUSES = {
    "UNKNOWN", "LABEL_CREATED", "PICKED_UP", "IN_TRANSIT", "DELAYED", "EXCEPTION",
    "OUT_FOR_DELIVERY", "DELIVERY_ATTEMPTED", "RETURN_TO_SENDER", "DELIVERED", "RETURNED",
}


def feta_target_status(normalized_status: Any) -> str | None:
    """Return only conservative FedEx-to-local status mappings."""
    if not isinstance(normalized_status, str):
        return None
    return _FETA_STATUS_MAP.get(normalized_status.strip().upper())


def apply_feta_event(event: Any, now: Any = None) -> dict[str, Any]:
    """Import a manually accepted FETA observation without inventing a scan node."""
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
    raw_value = event.get("raw_status")
    raw_status = raw_value[:500] if isinstance(raw_value, str) else normalized
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
        status_updated = stale_observations = 0
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
            row = conn.execute("SELECT tracking_number FROM lt_orders WHERE id=?",
                               (order_row_id,)).fetchone()
            if not row:
                return False
            tracking_number = row[0]
            conn.execute("DELETE FROM lt_orders WHERE id=?", (order_row_id,))
            still_used = conn.execute(
                "SELECT 1 FROM lt_orders WHERE tracking_number=? COLLATE NOCASE LIMIT 1",
                (tracking_number,),
            ).fetchone()
            if not still_used:
                conn.execute("DELETE FROM lt_events WHERE tracking_number=? COLLATE NOCASE",
                             (tracking_number,))
        return True
    finally:
        conn.close()


def events(order_row_id: int) -> list[dict[str, Any]]:
    conn = _conn()
    try:
        row = conn.execute("SELECT tracking_number FROM lt_orders WHERE id=?",
                           (order_row_id,)).fetchone()
        if not row:
            return []
        rows = conn.execute(
            "SELECT id, tracking_number, event_at, event_at_raw, event_text, raw_event, location, "
            "status, pkg_status_text, source_country, destination_country, transit_days, "
            "timezone_unknown, imported_at, source, source_event_id, source_received_at, raw_eta_text FROM lt_events "
            "WHERE tracking_number=? COLLATE NOCASE ORDER BY "
            "CASE WHEN event_at IS NULL THEN 1 ELSE 0 END, event_at DESC, id DESC",
            (row[0],),
        ).fetchall()
        return [{
            "id": item[0], "tracking_number": item[1], "event_at": item[2],
            "event_at_raw": item[3], "event_text": item[4], "raw_event": item[5],
            "location": item[6], "status": item[7], "pkg_status_text": item[8],
            "source_country": item[9], "destination_country": item[10],
            "transit_days": item[11], "timezone_unknown": bool(item[12]),
            "imported_at": item[13], "source": item[14], "source_event_id": item[15],
            "source_received_at": item[16], "raw_eta_text": item[17],
        } for item in rows]
    finally:
        conn.close()


__all__ = [
    "MAX_ORDERS", "BATCH_SIZE", "add_pairs", "list_orders", "next_batch", "pending_count",
    "import_records", "feta_target_status", "apply_feta_event", "set_enabled", "events", "remove_order",
]
