"""Private SQLite repository for FETA, deliberately separate from the seller app DB."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import uuid

from .config import ROOT

MAX_BATCH_SIZE = 30
MAX_RETRIES = 4  # initial attempt plus up to three backoff retries
BACKOFF_MINUTES = (30, 60, 120)
_STATUS_PROGRESS = {
    "UNKNOWN": -1, "LABEL_CREATED": 0, "PICKED_UP": 1,
    "IN_TRANSIT": 2, "DELAYED": 2, "EXCEPTION": 3,
    "OUT_FOR_DELIVERY": 3, "DELIVERY_ATTEMPTED": 3,
    "RETURN_TO_SENDER": 4, "DELIVERED": 4, "RETURNED": 5,
}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS mail_accounts (
    id INTEGER PRIMARY KEY CHECK(id = 1), provider TEXT NOT NULL, email TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'active', created_at_utc TEXT NOT NULL, updated_at_utc TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS mailbox_state (
    account_id INTEGER PRIMARY KEY REFERENCES mail_accounts(id) ON DELETE CASCADE,
    uidvalidity TEXT NOT NULL, last_uid INTEGER NOT NULL DEFAULT 0, updated_at_utc TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS shipments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    carrier TEXT NOT NULL DEFAULT 'fedex',
    tracking_number TEXT NOT NULL,
    destination_timezone TEXT NOT NULL DEFAULT 'America/Chicago',
    status TEXT NOT NULL DEFAULT 'UNKNOWN', raw_status TEXT, raw_eta_text TEXT, eta_utc TEXT,
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    paused INTEGER NOT NULL DEFAULT 0 CHECK(paused IN (0,1)), pause_reason TEXT,
    retry_count INTEGER NOT NULL DEFAULT 0, force_requery INTEGER NOT NULL DEFAULT 0 CHECK(force_requery IN (0,1)),
    created_at_utc TEXT NOT NULL, last_query_at_utc TEXT, last_success_at_utc TEXT, next_query_at_utc TEXT,
    UNIQUE(carrier, tracking_number COLLATE NOCASE)
);
CREATE TABLE IF NOT EXISTS order_shipments (
    order_id TEXT NOT NULL, shipment_id INTEGER NOT NULL REFERENCES shipments(id) ON DELETE CASCADE,
    PRIMARY KEY(order_id, shipment_id)
);
CREATE INDEX IF NOT EXISTS idx_feta_shipments_due ON shipments(active, paused, next_query_at_utc);
CREATE TABLE IF NOT EXISTS query_batches (
    id TEXT PRIMARY KEY, account_id INTEGER REFERENCES mail_accounts(id), created_at_utc TEXT NOT NULL,
    submitted_at_utc TEXT, timeout_at_utc TEXT,
    send_status TEXT NOT NULL CHECK(send_status IN ('sending','submitted','failed','unknown')),
    reply_status TEXT NOT NULL CHECK(reply_status IN ('pending','partial','completed','unmatched','timeout')),
    retry_count INTEGER NOT NULL DEFAULT 0, next_retry_at_utc TEXT,
    outgoing_message_id TEXT, failure_reason TEXT
);
CREATE TABLE IF NOT EXISTS query_batch_items (
    batch_id TEXT NOT NULL REFERENCES query_batches(id) ON DELETE CASCADE,
    shipment_id INTEGER NOT NULL REFERENCES shipments(id), tracking_number TEXT NOT NULL,
    PRIMARY KEY(batch_id, shipment_id)
);
CREATE INDEX IF NOT EXISTS idx_feta_batch_items_tracking ON query_batch_items(tracking_number COLLATE NOCASE);
CREATE TABLE IF NOT EXISTS query_attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT, batch_id TEXT NOT NULL REFERENCES query_batches(id) ON DELETE CASCADE,
    attempt_no INTEGER NOT NULL, started_at_utc TEXT NOT NULL, finished_at_utc TEXT,
    status TEXT NOT NULL CHECK(status IN ('sending','submitted','failed','unknown')),
    message_id TEXT, failure_reason TEXT, UNIQUE(batch_id, attempt_no)
);
CREATE TABLE IF NOT EXISTS query_attempt_items (
    attempt_id INTEGER NOT NULL REFERENCES query_attempts(id) ON DELETE CASCADE,
    shipment_id INTEGER NOT NULL REFERENCES shipments(id), tracking_number TEXT NOT NULL,
    PRIMARY KEY(attempt_id, shipment_id)
);
CREATE INDEX IF NOT EXISTS idx_feta_attempt_items_tracking ON query_attempt_items(tracking_number COLLATE NOCASE);
CREATE TABLE IF NOT EXISTS incoming_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT, account_id INTEGER NOT NULL REFERENCES mail_accounts(id),
    uidvalidity TEXT NOT NULL, uid TEXT NOT NULL, message_id TEXT, received_at_utc TEXT NOT NULL,
    sender TEXT NOT NULL DEFAULT '', auth_verified INTEGER NOT NULL DEFAULT 0,
    body_sha256 TEXT NOT NULL, reply_status TEXT NOT NULL DEFAULT 'unmatched', parse_result TEXT NOT NULL DEFAULT '{}',
    UNIQUE(account_id, uidvalidity, uid)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_feta_incoming_message_id
    ON incoming_messages(account_id, message_id) WHERE message_id IS NOT NULL AND message_id != '';
CREATE TABLE IF NOT EXISTS tracking_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, shipment_id INTEGER NOT NULL REFERENCES shipments(id),
    batch_id TEXT REFERENCES query_batches(id), source_message_id INTEGER NOT NULL REFERENCES incoming_messages(id),
    received_at_utc TEXT NOT NULL, raw_status TEXT, normalized_status TEXT NOT NULL,
    raw_eta_text TEXT, eta_utc TEXT, destination_timezone TEXT NOT NULL,
    parse_confidence TEXT NOT NULL, parser_version TEXT NOT NULL, needs_manual_review INTEGER NOT NULL DEFAULT 1,
    review_state TEXT NOT NULL DEFAULT 'pending' CHECK(review_state IN ('pending','accepted','ignored')),
    UNIQUE(shipment_id, source_message_id)
);
CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT, shipment_id INTEGER NOT NULL REFERENCES shipments(id),
    rule_code TEXT NOT NULL, normalized_status TEXT NOT NULL, severity TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'open' CHECK(state IN ('open','acknowledged','ignored','resolved')),
    first_seen_at_utc TEXT NOT NULL, last_sent_at_utc TEXT, acknowledged_at_utc TEXT,
    notification_status TEXT NOT NULL DEFAULT 'pending'
        CHECK(notification_status IN ('pending','sending','sent','failed','unknown')),
    notification_attempts INTEGER NOT NULL DEFAULT 0, notification_last_attempt_at_utc TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_feta_active_alert
    ON alerts(shipment_id, rule_code, normalized_status) WHERE state IN ('open','acknowledged');
CREATE TABLE IF NOT EXISTS feta_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


def utc_iso(value: datetime | str | None = None) -> str:
    if value is None:
        instant = datetime.now(timezone.utc)
    elif isinstance(value, str):
        instant = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if instant.tzinfo is None:
            raise ValueError("时间必须包含时区")
        instant = instant.astimezone(timezone.utc)
    elif isinstance(value, datetime) and value.tzinfo is not None:
        instant = value.astimezone(timezone.utc)
    else:
        raise ValueError("时间必须是带时区的 datetime/ISO8601")
    return instant.isoformat(timespec="seconds").replace("+00:00", "Z")


def _datetime(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def default_db_path() -> Path:
    return ROOT / "deep_results" / "feta.sqlite3"


@contextmanager
def _db(db_path: str | Path | None = None):
    path = Path(db_path or default_db_path())
    if str(path) != ":memory:":
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=15, isolation_level=None)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=15000")
        if str(path) != ":memory:":
            conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(_SCHEMA)
        migrations = {
            "shipments": {"retry_count": "INTEGER NOT NULL DEFAULT 0",
                          "force_requery": "INTEGER NOT NULL DEFAULT 0"},
            "alerts": {
                "notification_status": "TEXT NOT NULL DEFAULT 'pending'",
                "notification_attempts": "INTEGER NOT NULL DEFAULT 0",
                "notification_last_attempt_at_utc": "TEXT",
            },
        }
        conn.execute("BEGIN IMMEDIATE")
        try:
            for table, columns in migrations.items():
                existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
                for column, declaration in columns.items():
                    if column not in existing:
                        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        yield conn
        if conn.in_transaction:
            conn.commit()
    except BaseException:
        if conn.in_transaction:
            conn.rollback()
        raise
    finally:
        conn.close()


def init_db(db_path: str | Path | None = None) -> None:
    with _db(db_path):
        pass


def _begin(conn: sqlite3.Connection) -> None:
    conn.execute("BEGIN IMMEDIATE")


def register_mail_account(email: str, provider: str = "gmail", now=None, db_path=None) -> dict:
    if not email or "@" not in email:
        raise ValueError("邮箱地址无效")
    stamp = utc_iso(now)
    with _db(db_path) as conn:
        _begin(conn)
        conn.execute(
            "INSERT INTO mail_accounts(id,provider,email,state,created_at_utc,updated_at_utc) "
            "VALUES(1,?,?, 'active',?,?) ON CONFLICT(id) DO UPDATE SET "
            "provider=excluded.provider,email=excluded.email,updated_at_utc=excluded.updated_at_utc",
            (provider, email, stamp, stamp),
        )
        row = conn.execute("SELECT id,provider,email,state FROM mail_accounts WHERE id=1").fetchone()
        return dict(row)


def mail_account_state(db_path=None) -> str:
    with _db(db_path) as conn:
        row = conn.execute("SELECT state FROM mail_accounts WHERE id=1").fetchone()
        return row[0] if row else "active"


def set_mail_account_state(state: str, now=None, db_path=None) -> None:
    if state not in {"active", "auth_failed", "disabled"}:
        raise ValueError("邮箱状态无效")
    with _db(db_path) as conn:
        _begin(conn)
        conn.execute("UPDATE mail_accounts SET state=?,updated_at_utc=? WHERE id=1", (state, utc_iso(now)))


def import_rows(rows: list[dict], now=None, db_path=None) -> dict:
    stamp = utc_iso(now)
    added = links_added = duplicates = 0
    with _db(db_path) as conn:
        _begin(conn)
        for row in rows:
            order_id = str(row.get("order_id") or "").strip()
            tracking = str(row.get("tracking_number") or "").strip()
            carrier = str(row.get("carrier") or "FedEx").strip().casefold()
            tz_name = str(row.get("destination_timezone") or "America/Chicago").strip()
            active = bool(row.get("active", True))
            if not order_id or not tracking or carrier != "fedex" or not tz_name:
                raise ValueError("导入行缺少订单号/跟踪号、承运商须为 FedEx，并提供目的地时区")
            existing = conn.execute(
                "SELECT id FROM shipments WHERE carrier='fedex' AND tracking_number=? COLLATE NOCASE", (tracking,)
            ).fetchone()
            if existing:
                shipment_id = existing[0]
                conn.execute("UPDATE shipments SET destination_timezone=? WHERE id=?", (tz_name, shipment_id))
                duplicates += 1
            else:
                cur = conn.execute(
                    "INSERT INTO shipments(carrier,tracking_number,destination_timezone,active,created_at_utc) "
                    "VALUES('fedex',?,?,?,?)", (tracking, tz_name, int(active), stamp)
                )
                shipment_id = cur.lastrowid
                added += 1
            cur = conn.execute("INSERT OR IGNORE INTO order_shipments(order_id,shipment_id) VALUES(?,?)",
                               (order_id, shipment_id))
            links_added += max(0, cur.rowcount)
    return {"added_shipments": added, "duplicate_rows": duplicates, "order_links_added": links_added}


def list_shipments(db_path=None) -> list[dict]:
    with _db(db_path) as conn:
        rows = conn.execute(
            "SELECT s.*, COALESCE((SELECT group_concat(order_id, ', ') FROM "
            "(SELECT order_id FROM order_shipments o WHERE o.shipment_id=s.id ORDER BY order_id)), '') AS order_ids "
            "FROM shipments s ORDER BY s.id"
        ).fetchall()
        return [dict(row) for row in rows]


def _eligible(conn: sqlite3.Connection, now_iso: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT s.* FROM shipments s WHERE s.active=1 AND s.paused=0 "
        "AND (s.status NOT IN ('DELIVERED','RETURNED') OR s.force_requery=1) "
        "AND (s.next_query_at_utc IS NULL OR s.next_query_at_utc<=?) "
        "AND NOT EXISTS (SELECT 1 FROM query_batch_items qi JOIN query_batches b ON b.id=qi.batch_id "
        " WHERE qi.shipment_id=s.id AND (b.send_status='unknown' "
        " OR (b.send_status='sending') "
        " OR (b.send_status='submitted' AND b.reply_status IN ('pending','partial') "
        "     AND (b.timeout_at_utc IS NULL OR b.timeout_at_utc>?)))) "
        "ORDER BY CASE s.status WHEN 'EXCEPTION' THEN 0 WHEN 'DELIVERY_ATTEMPTED' THEN 1 "
        " WHEN 'RETURN_TO_SENDER' THEN 1 WHEN 'OUT_FOR_DELIVERY' THEN 2 ELSE 3 END, "
        "s.next_query_at_utc IS NOT NULL, s.next_query_at_utc, s.created_at_utc, s.id",
        (now_iso, now_iso),
    ).fetchall()


def preview_batches(now=None, db_path=None, batch_size: int = MAX_BATCH_SIZE) -> list[list[str]]:
    if not 1 <= batch_size <= MAX_BATCH_SIZE:
        raise ValueError(f"每批最多 {MAX_BATCH_SIZE} 个跟踪号")
    now_iso = utc_iso(now)
    with _db(db_path) as conn:
        rows = _eligible(conn, now_iso)
        numbers = list(dict.fromkeys(row["tracking_number"] for row in rows))
        return [numbers[i:i + batch_size] for i in range(0, len(numbers), batch_size)]


def prepare_batch(now=None, interval_hours: float = 12, reply_timeout_minutes: int = 60,
                  db_path=None) -> tuple[dict | None, str]:
    if interval_hours <= 0 or interval_hours > 168:
        raise ValueError("查询间隔必须在 0–168 小时之间")
    if not 1 <= reply_timeout_minutes <= 10080:
        raise ValueError("回复超时须为 1–10080 分钟")
    instant = _datetime(utc_iso(now))
    now_iso = utc_iso(instant)
    with _db(db_path) as conn:
        _begin(conn)
        retry = conn.execute(
            "SELECT * FROM query_batches WHERE (send_status='failed' OR reply_status='timeout') "
            "AND retry_count<? AND next_retry_at_utc IS NOT NULL AND next_retry_at_utc<=? "
            "ORDER BY next_retry_at_utc,created_at_utc LIMIT 1", (MAX_RETRIES, now_iso)
        ).fetchone()
        is_retry = retry is not None
        if retry:
            batch_id = retry["id"]
            attempt_no = conn.execute("SELECT COALESCE(MAX(attempt_no),0)+1 FROM query_attempts WHERE batch_id=?",
                                      (batch_id,)).fetchone()[0]
            conn.execute("UPDATE query_batches SET send_status='sending',reply_status='pending',"
                         "next_retry_at_utc=NULL,failure_reason=NULL WHERE id=?", (batch_id,))
            numbers = [row[0] for row in conn.execute(
                "SELECT qi.tracking_number FROM query_batch_items qi WHERE qi.batch_id=? AND NOT EXISTS "
                "(SELECT 1 FROM tracking_events e WHERE e.batch_id=qi.batch_id AND e.shipment_id=qi.shipment_id) "
                "ORDER BY qi.rowid", (batch_id,))]
            if not numbers:
                conn.execute("UPDATE query_batches SET reply_status='completed',next_retry_at_utc=NULL WHERE id=?",
                             (batch_id,))
                return None, "no_due_shipments"
        else:
            last = conn.execute("SELECT value FROM feta_meta WHERE key='last_round_at_utc'").fetchone()
            if last and instant - _datetime(last[0]) < timedelta(hours=interval_hours):
                return None, "cooldown"
            rows = _eligible(conn, now_iso)[:MAX_BATCH_SIZE]
            numbers = list(dict.fromkeys(row["tracking_number"] for row in rows))
            if not numbers:
                return None, "no_due_shipments"
            batch_id = str(uuid.uuid4())
            conn.execute(
                "INSERT INTO query_batches(id,created_at_utc,send_status,reply_status) VALUES(?,?,'sending','pending')",
                (batch_id, now_iso),
            )
            for row in rows:
                conn.execute("INSERT OR IGNORE INTO query_batch_items(batch_id,shipment_id,tracking_number) VALUES(?,?,?)",
                             (batch_id, row["id"], row["tracking_number"]))
            attempt_no = 1
        attempt_cursor = conn.execute(
            "INSERT INTO query_attempts(batch_id,attempt_no,started_at_utc,status) VALUES(?,?,?,'sending')",
            (batch_id, attempt_no, now_iso))
        attempt_id = attempt_cursor.lastrowid
        selected_numbers = {number.casefold() for number in numbers}
        attempt_items = [(attempt_id, row["shipment_id"], row["tracking_number"])
                         for row in conn.execute("SELECT shipment_id,tracking_number FROM query_batch_items "
                                                 "WHERE batch_id=?", (batch_id,)).fetchall()
                         if row["tracking_number"].casefold() in selected_numbers]
        conn.executemany("INSERT INTO query_attempt_items(attempt_id,shipment_id,tracking_number) VALUES(?,?,?)",
                         attempt_items)
        conn.execute("INSERT INTO feta_meta(key,value) VALUES('last_round_at_utc',?) "
                     "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (now_iso,))
        conn.execute("UPDATE shipments SET last_query_at_utc=?,next_query_at_utc=? WHERE id IN "
                     "(SELECT shipment_id FROM query_batch_items WHERE batch_id=?)",
                     (now_iso, utc_iso(instant + timedelta(hours=interval_hours)), batch_id))
        return {"id": batch_id, "tracking_numbers": numbers, "attempt_no": attempt_no,
                "retry_count": retry["retry_count"] if retry else 0}, "retry" if is_retry else "new"


def finish_attempt(batch_id: str, status: str, now=None, *, message_id: str | None = None,
                   failure_reason: str | None = None, interval_hours: float = 12,
                   reply_timeout_minutes: int = 60, db_path=None) -> dict:
    if status not in {"submitted", "failed", "unknown"}:
        raise ValueError("发送结果无效")
    instant = _datetime(utc_iso(now))
    now_iso = utc_iso(instant)
    with _db(db_path) as conn:
        _begin(conn)
        batch = conn.execute("SELECT * FROM query_batches WHERE id=?", (batch_id,)).fetchone()
        if not batch:
            raise ValueError("查询批次不存在")
        attempt = conn.execute("SELECT id FROM query_attempts WHERE batch_id=? AND status='sending' "
                               "ORDER BY attempt_no DESC LIMIT 1", (batch_id,)).fetchone()
        if not attempt:
            raise ValueError("批次没有进行中的发送尝试")
        attempt_id = attempt[0]
        reason = (failure_reason or "")[:200]
        if status == "submitted":
            timeout_at = utc_iso(instant + timedelta(minutes=reply_timeout_minutes))
            conn.execute("UPDATE query_batches SET send_status='submitted',submitted_at_utc=?,timeout_at_utc=?,"
                         "next_retry_at_utc=NULL,outgoing_message_id=?,failure_reason=NULL WHERE id=?",
                         (now_iso, timeout_at, message_id, batch_id))
            conn.execute("UPDATE query_attempts SET status='submitted',finished_at_utc=?,message_id=? WHERE id=?",
                         (now_iso, message_id, attempt_id))
            conn.execute("UPDATE shipments SET next_query_at_utc=? WHERE id IN "
                         "(SELECT shipment_id FROM query_batch_items WHERE batch_id=?)",
                         (utc_iso(instant + timedelta(hours=interval_hours)), batch_id))
        elif status == "failed":
            retries = int(batch["retry_count"]) + 1
            retry_at = None if retries >= MAX_RETRIES else utc_iso(instant + timedelta(minutes=BACKOFF_MINUTES[min(retries - 1, len(BACKOFF_MINUTES) - 1)]))
            conn.execute("UPDATE query_batches SET send_status='failed',retry_count=?,next_retry_at_utc=?,"
                         "failure_reason=? WHERE id=?", (retries, retry_at, reason, batch_id))
            conn.execute("UPDATE query_attempts SET status='failed',finished_at_utc=?,failure_reason=? WHERE id=?",
                         (now_iso, reason, attempt_id))
            if retry_at:
                conn.execute("UPDATE shipments SET next_query_at_utc=? WHERE id IN "
                             "(SELECT shipment_id FROM query_batch_items WHERE batch_id=?)", (retry_at, batch_id))
            else:
                conn.execute("UPDATE shipments SET paused=1,pause_reason='send_retries_exhausted',next_query_at_utc=NULL "
                             "WHERE id IN (SELECT qi.shipment_id FROM query_batch_items qi WHERE qi.batch_id=? "
                             "AND NOT EXISTS (SELECT 1 FROM tracking_events e WHERE e.batch_id=qi.batch_id "
                             "AND e.shipment_id=qi.shipment_id))", (batch_id,))
        else:
            conn.execute("UPDATE query_batches SET send_status='unknown',next_retry_at_utc=NULL,"
                         "failure_reason=? WHERE id=?", (reason or "smtp_result_unknown", batch_id))
            conn.execute("UPDATE query_attempts SET status='unknown',finished_at_utc=?,failure_reason=? WHERE id=?",
                         (now_iso, reason or "smtp_result_unknown", attempt_id))
            conn.execute("UPDATE shipments SET next_query_at_utc=NULL WHERE id IN "
                         "(SELECT shipment_id FROM query_batch_items WHERE batch_id=?)", (batch_id,))
        return dict(conn.execute("SELECT * FROM query_batches WHERE id=?", (batch_id,)).fetchone())


def recover_interrupted_sends(now=None, db_path=None, stale_after_seconds: int = 300) -> int:
    instant = _datetime(utc_iso(now))
    now_iso = utc_iso(instant)
    stale_before = utc_iso(instant - timedelta(seconds=stale_after_seconds))
    with _db(db_path) as conn:
        _begin(conn)
        ids = [row[0] for row in conn.execute(
            "SELECT b.id FROM query_batches b WHERE b.send_status='sending' AND EXISTS "
            "(SELECT 1 FROM query_attempts a WHERE a.batch_id=b.id AND a.status='sending' "
            "AND a.started_at_utc<=?)", (stale_before,)).fetchall()]
        for batch_id in ids:
            conn.execute("UPDATE query_batches SET send_status='unknown',failure_reason='process_interrupted' WHERE id=?",
                         (batch_id,))
            conn.execute("UPDATE query_attempts SET status='unknown',finished_at_utc=?,failure_reason='process_interrupted' "
                         "WHERE batch_id=? AND status='sending'", (now_iso, batch_id))
            conn.execute("UPDATE shipments SET next_query_at_utc=NULL WHERE id IN "
                         "(SELECT shipment_id FROM query_batch_items WHERE batch_id=?)", (batch_id,))
        return len(ids)


def reconcile_batch(batch_id: str, result: str, now=None, *, interval_hours=12,
                    reply_timeout_minutes=60, db_path=None) -> dict:
    if result not in {"submitted", "failed"}:
        raise ValueError("人工核对结果只能是 submitted 或 failed")
    with _db(db_path) as conn:
        _begin(conn)
        row = conn.execute("SELECT send_status FROM query_batches WHERE id=?", (batch_id,)).fetchone()
        if not row or row[0] != "unknown":
            raise ValueError("只能核对结果不确定的批次")
        conn.execute("UPDATE query_attempts SET status='sending',finished_at_utc=NULL,failure_reason=NULL "
                     "WHERE id=(SELECT id FROM query_attempts WHERE batch_id=? ORDER BY attempt_no DESC LIMIT 1)",
                     (batch_id,))
    return finish_attempt(batch_id, result, now, message_id=None,
                          failure_reason="manual_reconciliation_failed" if result == "failed" else None,
                          interval_hours=interval_hours, reply_timeout_minutes=reply_timeout_minutes,
                          db_path=db_path)


def batch_details(batch_id: str, db_path=None) -> dict:
    with _db(db_path) as conn:
        batch = conn.execute("SELECT * FROM query_batches WHERE id=?", (batch_id,)).fetchone()
        if not batch:
            raise ValueError("查询批次不存在")
        result = dict(batch)
        result["tracking_numbers"] = [row[0] for row in conn.execute(
            "SELECT tracking_number FROM query_batch_items WHERE batch_id=? ORDER BY rowid", (batch_id,))]
        result["attempts"] = [dict(row) for row in conn.execute(
            "SELECT * FROM query_attempts WHERE batch_id=? ORDER BY attempt_no", (batch_id,))]
        for attempt in result["attempts"]:
            attempt["tracking_numbers"] = [row[0] for row in conn.execute(
                "SELECT tracking_number FROM query_attempt_items WHERE attempt_id=? ORDER BY rowid",
                (attempt["id"],))]
        return result


def expire_batches(now=None, db_path=None) -> list[str]:
    instant = _datetime(utc_iso(now))
    now_iso = utc_iso(instant)
    expired: list[str] = []
    with _db(db_path) as conn:
        _begin(conn)
        rows = conn.execute("SELECT * FROM query_batches WHERE send_status='submitted' "
                            "AND reply_status IN ('pending','partial') AND timeout_at_utc<=?", (now_iso,)).fetchall()
        for batch in rows:
            expired.append(batch["id"])
            retries = int(batch["retry_count"]) + 1
            retry_at = None if retries >= MAX_RETRIES else utc_iso(instant + timedelta(minutes=BACKOFF_MINUTES[min(retries - 1, len(BACKOFF_MINUTES) - 1)]))
            conn.execute("UPDATE query_batches SET reply_status='timeout',retry_count=?,next_retry_at_utc=?,"
                         "failure_reason='reply_timeout' WHERE id=?", (retries, retry_at, batch["id"]))
            if retry_at:
                conn.execute("UPDATE shipments SET next_query_at_utc=? WHERE id IN "
                             "(SELECT shipment_id FROM query_batch_items WHERE batch_id=?)", (retry_at, batch["id"]))
            else:
                conn.execute("UPDATE shipments SET paused=1,pause_reason='reply_retries_exhausted',next_query_at_utc=NULL "
                             "WHERE id IN (SELECT qi.shipment_id FROM query_batch_items qi WHERE qi.batch_id=? "
                             "AND NOT EXISTS (SELECT 1 FROM tracking_events e WHERE e.batch_id=qi.batch_id "
                             "AND e.shipment_id=qi.shipment_id))", (batch["id"],))
    return expired


def enable_shipment(tracking_number: str, now=None, db_path=None) -> int:
    with _db(db_path) as conn:
        _begin(conn)
        cur = conn.execute("UPDATE shipments SET active=1,paused=0,pause_reason=NULL,retry_count=0,force_requery=1,"
                           "next_query_at_utc=? WHERE tracking_number=? COLLATE NOCASE",
                           (utc_iso(now), tracking_number.strip()))
        return cur.rowcount


def account_id_for(email: str, provider: str = "gmail", now=None, db_path=None) -> int:
    register_mail_account(email, provider, now, db_path)
    with _db(db_path) as conn:
        return int(conn.execute("SELECT id FROM mail_accounts WHERE id=1").fetchone()[0])


def insert_incoming(*, email: str, provider: str, uidvalidity: str, uid: str,
                    message_id: str | None, received_at, sender: str, auth_verified: bool,
                    body_sha256: str, reply_status: str, parse_result: dict,
                    now=None, db_path=None) -> tuple[int | None, bool]:
    account_id = account_id_for(email, provider, now, db_path)
    received = utc_iso(received_at)
    with _db(db_path) as conn:
        _begin(conn)
        existing = conn.execute("SELECT id FROM incoming_messages WHERE account_id=? AND ("
                                "(uidvalidity=? AND uid=?) OR (? IS NOT NULL AND message_id=?)) LIMIT 1",
                                (account_id, str(uidvalidity), str(uid), message_id, message_id)).fetchone()
        if existing:
            return int(existing[0]), False
        cur = conn.execute(
            "INSERT INTO incoming_messages(account_id,uidvalidity,uid,message_id,received_at_utc,sender,"
            "auth_verified,body_sha256,reply_status,parse_result) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (account_id, str(uidvalidity), str(uid), message_id, received, sender[:320], int(auth_verified),
             body_sha256, reply_status, json.dumps(parse_result, ensure_ascii=False)),
        )
        return int(cur.lastrowid), True


def promote_unverified_incoming(incoming_id: int, *, sender: str, body_sha256: str,
                               reply_status: str, parse_result: dict, db_path=None) -> bool:
    """Allow an explicit mailbox re-fetch to upgrade an unverified UID, never downgrade a verified one."""
    with _db(db_path) as conn:
        _begin(conn)
        cur = conn.execute("UPDATE incoming_messages SET sender=?,auth_verified=1,body_sha256=?,reply_status=?,"
                           "parse_result=? WHERE id=? AND auth_verified=0",
                           (sender[:320], body_sha256, reply_status,
                            json.dumps(parse_result, ensure_ascii=False), incoming_id))
        return cur.rowcount == 1


def candidate_attempts(tracking_number: str, received_at, window_hours: int = 48, db_path=None) -> list[dict]:
    received = _datetime(utc_iso(received_at))
    lower = utc_iso(received - timedelta(hours=window_hours))
    upper = utc_iso(received)
    with _db(db_path) as conn:
        rows = conn.execute(
            "SELECT b.id AS batch_id,a.id AS attempt_id,a.finished_at_utc "
            "FROM query_attempt_items ai JOIN query_attempts a ON a.id=ai.attempt_id "
            "JOIN query_batches b ON b.id=a.batch_id WHERE ai.tracking_number=? COLLATE NOCASE "
            "AND a.status='submitted' AND a.finished_at_utc>=? AND a.finished_at_utc<=? "
            "ORDER BY a.finished_at_utc DESC", (tracking_number, lower, upper)
        ).fetchall()
        return [dict(row) for row in rows]


def record_event(*, shipment_id: int, batch_id: str | None, source_message_id: int,
                 received_at, raw_status: str | None, normalized_status: str,
                 raw_eta_text: str | None, destination_timezone: str,
                 parse_confidence: str, parser_version: str, db_path=None) -> int:
    with _db(db_path) as conn:
        _begin(conn)
        cur = conn.execute(
            "INSERT OR IGNORE INTO tracking_events(shipment_id,batch_id,source_message_id,received_at_utc,raw_status,"
            "normalized_status,raw_eta_text,destination_timezone,parse_confidence,parser_version,needs_manual_review) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,1)",
            (shipment_id, batch_id, source_message_id, utc_iso(received_at), raw_status, normalized_status,
             raw_eta_text, destination_timezone, parse_confidence, parser_version),
        )
        if cur.rowcount:
            return int(cur.lastrowid)
        row = conn.execute("SELECT id FROM tracking_events WHERE shipment_id=? AND source_message_id=?",
                           (shipment_id, source_message_id)).fetchone()
        return int(row[0])


def batch_item_ids(batch_id: str, db_path=None) -> list[dict]:
    with _db(db_path) as conn:
        return [dict(row) for row in conn.execute(
            "SELECT shipment_id,tracking_number FROM query_batch_items WHERE batch_id=? ORDER BY rowid", (batch_id,))]


def update_batch_reply_status(batch_id: str, matched_numbers: set[str], *, partial: bool,
                              db_path=None) -> str:
    with _db(db_path) as conn:
        _begin(conn)
        expected = {row[0].casefold() for row in conn.execute(
            "SELECT tracking_number FROM query_batch_items WHERE batch_id=?", (batch_id,))}
        received = {value.casefold() for value in matched_numbers}
        received.update(row[0].casefold() for row in conn.execute(
            "SELECT DISTINCT s.tracking_number FROM tracking_events e JOIN shipments s ON s.id=e.shipment_id "
            "WHERE e.batch_id=?", (batch_id,)))
        status = "completed" if expected and expected.issubset(received) else ("partial" if received or partial else "unmatched")
        conn.execute("UPDATE query_batches SET reply_status=? WHERE id=?", (status, batch_id))
        return status


def update_incoming_result(incoming_id: int, reply_status: str, parse_result: dict, db_path=None) -> None:
    with _db(db_path) as conn:
        _begin(conn)
        conn.execute("UPDATE incoming_messages SET reply_status=?,parse_result=? WHERE id=?",
                     (reply_status, json.dumps(parse_result, ensure_ascii=False), incoming_id))


def list_incoming_messages(limit: int = 100, db_path=None) -> list[dict]:
    with _db(db_path) as conn:
        rows = conn.execute("SELECT id,uidvalidity,uid,message_id,received_at_utc,auth_verified,reply_status "
                            "FROM incoming_messages ORDER BY received_at_utc DESC,id DESC LIMIT ?",
                            (max(1, min(int(limit), 500)),)).fetchall()
        return [dict(row) for row in rows]


def incoming_message_detail(message_id: int, db_path=None) -> dict:
    with _db(db_path) as conn:
        row = conn.execute("SELECT id,uidvalidity,uid,message_id,received_at_utc,auth_verified,reply_status,parse_result "
                           "FROM incoming_messages WHERE id=?", (message_id,)).fetchone()
        if not row:
            raise ValueError("邮件摘要不存在")
        result = dict(row)
        result["parse_result"] = json.loads(result["parse_result"] or "{}")
        return result


def review_events(db_path=None) -> list[dict]:
    with _db(db_path) as conn:
        return [dict(row) for row in conn.execute(
            "SELECT e.id,e.shipment_id,s.tracking_number,e.batch_id,e.received_at_utc,e.raw_status,"
            "e.normalized_status,e.raw_eta_text,e.destination_timezone,e.parse_confidence,e.parser_version,"
            "e.review_state,e.needs_manual_review FROM tracking_events e JOIN shipments s ON s.id=e.shipment_id "
            "WHERE e.review_state='pending' ORDER BY e.received_at_utc,e.id")]


def _validate_review_row(row, decision: str) -> None:
    if decision not in {"accept", "ignore"}:
        raise ValueError("复核操作必须是 accept 或 ignore")
    if not row:
        raise ValueError("物流事件不存在")
    if row["review_state"] != "pending":
        raise ValueError("该物流事件已复核")
    if decision != "accept":
        return
    normalized = row["normalized_status"]
    terminal = {"DELIVERED", "RETURNED"}
    if row["last_success_at_utc"] and _datetime(row["received_at_utc"]) < _datetime(row["last_success_at_utc"]):
        raise ValueError("事件早于当前已接受状态；请将旧事件标记为 ignore，避免状态回退")
    if row["current_status"] in terminal and normalized not in terminal and not row["force_requery"]:
        raise ValueError("运单已处于 Delivered/Returned 终态；如需重新查询请先执行 shipments resume")
    current_rank = _STATUS_PROGRESS.get(row["current_status"], -1)
    event_rank = _STATUS_PROGRESS.get(normalized, -1)
    if not row["force_requery"] and event_rank < current_rank:
        raise ValueError("事件状态低于当前已接受进度；如确认回退，请先执行 shipments resume")


def validate_review_event(event_id: int, decision: str, db_path=None) -> dict:
    """Check an event-review decision without changing FETA or target-system data."""
    with _db(db_path) as conn:
        row = conn.execute("SELECT e.*,s.tracking_number,s.status AS current_status,s.active AS current_active,"
                           "s.force_requery,s.last_success_at_utc FROM tracking_events e "
                           "JOIN shipments s ON s.id=e.shipment_id WHERE e.id=?", (event_id,)).fetchone()
        _validate_review_row(row, decision)
        return {"event_id": event_id, "tracking_number": row["tracking_number"],
                "normalized_status": row["normalized_status"], "review_state": row["review_state"]}


def review_event(event_id: int, decision: str, now=None, db_path=None) -> dict:
    with _db(db_path) as conn:
        _begin(conn)
        row = conn.execute("SELECT e.*,s.tracking_number,s.status AS current_status,s.active AS current_active,"
                           "s.force_requery,s.last_success_at_utc FROM tracking_events e "
                           "JOIN shipments s ON s.id=e.shipment_id WHERE e.id=?", (event_id,)).fetchone()
        _validate_review_row(row, decision)
        normalized = row["normalized_status"]
        terminal = {"DELIVERED", "RETURNED"}
        state = "accepted" if decision == "accept" else "ignored"
        conn.execute("UPDATE tracking_events SET review_state=? WHERE id=?", (state, event_id))
        if decision == "accept":
            active = 0 if normalized in terminal else int(row["current_active"])
            conn.execute("UPDATE shipments SET status=?,raw_status=?,raw_eta_text=?,last_success_at_utc=?,active=?,"
                         "force_requery=0 WHERE id=?", (normalized, row["raw_status"], row["raw_eta_text"],
                                                         row["received_at_utc"], active, row["shipment_id"]))
        return {"event_id": event_id, "tracking_number": row["tracking_number"], "review_state": state}


def evaluate_alerts(now=None, db_path=None) -> list[dict]:
    stamp = utc_iso(now)
    rules = {
        "EXCEPTION": ("exception", "high"),
        "DELIVERY_ATTEMPTED": ("delivery_attempted", "high"),
        "RETURN_TO_SENDER": ("return_to_sender", "high"),
        "DELAYED": ("delayed", "medium"),
    }
    created: list[dict] = []
    with _db(db_path) as conn:
        _begin(conn)
        shipments = conn.execute("SELECT id,tracking_number,status FROM shipments").fetchall()
        for shipment in shipments:
            expected = rules.get(shipment["status"])
            open_rows = conn.execute("SELECT id,rule_code,normalized_status FROM alerts WHERE shipment_id=? "
                                     "AND state IN ('open','acknowledged','ignored')", (shipment["id"],)).fetchall()
            for alert in open_rows:
                if not expected or alert["rule_code"] != expected[0] or alert["normalized_status"] != shipment["status"]:
                    conn.execute("UPDATE alerts SET state='resolved' WHERE id=?", (alert["id"],))
            if not expected:
                continue
            rule_code, severity = expected
            existing = conn.execute("SELECT id,state FROM alerts WHERE shipment_id=? AND rule_code=? "
                                    "AND normalized_status=? AND state IN ('open','acknowledged','ignored')",
                                    (shipment["id"], rule_code, shipment["status"])).fetchone()
            if existing:
                continue
            cur = conn.execute("INSERT INTO alerts(shipment_id,rule_code,normalized_status,severity,state,first_seen_at_utc) "
                               "VALUES(?,?,?,?,'open',?)",
                               (shipment["id"], rule_code, shipment["status"], severity, stamp))
            created.append({"id": cur.lastrowid, "tracking_number": shipment["tracking_number"],
                            "rule_code": rule_code, "normalized_status": shipment["status"],
                            "severity": severity, "state": "open"})
    return created


def list_alerts(db_path=None) -> list[dict]:
    with _db(db_path) as conn:
        return [dict(row) for row in conn.execute(
            "SELECT a.*,s.tracking_number FROM alerts a JOIN shipments s ON s.id=a.shipment_id "
            "WHERE a.state IN ('open','acknowledged') ORDER BY a.first_seen_at_utc,a.id")]


def set_alert_state(alert_id: int, state: str, now=None, db_path=None) -> None:
    if state not in {"open", "acknowledged", "ignored"}:
        raise ValueError("告警状态无效")
    stamp = utc_iso(now)
    with _db(db_path) as conn:
        _begin(conn)
        existing = conn.execute("SELECT last_sent_at_utc,notification_status FROM alerts WHERE id=?", (alert_id,)).fetchone()
        if not existing:
            raise ValueError("告警不存在")
        last_sent = None if state == "open" else existing[0]
        notification_status = "pending" if state == "open" else existing[1]
        conn.execute("UPDATE alerts SET state=?,acknowledged_at_utc=?,last_sent_at_utc=?,notification_status=? "
                     "WHERE id=?", (state, stamp if state == "acknowledged" else None,
                                     last_sent, notification_status, alert_id))


def report_rows(db_path=None) -> list[dict]:
    with _db(db_path) as conn:
        return [dict(row) for row in conn.execute(
            "SELECT s.carrier,s.tracking_number,o.order_id,s.status,s.raw_status,s.raw_eta_text,s.eta_utc,"
            "s.destination_timezone,s.last_query_at_utc,s.last_success_at_utc,s.active,s.paused "
            "FROM shipments s LEFT JOIN order_shipments o ON o.shipment_id=s.id "
            "ORDER BY s.id,o.order_id")]


def attach_batch_account(batch_id: str, account_id: int, db_path=None) -> None:
    with _db(db_path) as conn:
        _begin(conn)
        conn.execute("UPDATE query_batches SET account_id=? WHERE id=?", (account_id, batch_id))


def pending_alert_notifications(db_path=None) -> list[dict]:
    with _db(db_path) as conn:
        return [dict(row) for row in conn.execute(
            "SELECT a.*,s.tracking_number FROM alerts a JOIN shipments s ON s.id=a.shipment_id "
            "WHERE a.state='open' AND a.notification_status='pending' ORDER BY a.first_seen_at_utc,a.id")]


def begin_alert_notification(alert_id: int, now=None, db_path=None) -> bool:
    with _db(db_path) as conn:
        _begin(conn)
        cur = conn.execute("UPDATE alerts SET notification_status='sending',notification_attempts=notification_attempts+1,"
                           "notification_last_attempt_at_utc=? WHERE id=? AND state='open' AND notification_status='pending'",
                           (utc_iso(now), alert_id))
        return cur.rowcount == 1


def finish_alert_notification(alert_id: int, status: str, now=None, db_path=None) -> None:
    if status not in {"sent", "failed", "unknown"}:
        raise ValueError("告警邮件状态无效")
    with _db(db_path) as conn:
        _begin(conn)
        cur = conn.execute("UPDATE alerts SET notification_status=?,last_sent_at_utc=? WHERE id=? "
                           "AND notification_status='sending'",
                           (status, utc_iso(now) if status == "sent" else None, alert_id))
        if cur.rowcount != 1:
            raise ValueError("告警没有待完成的邮件发送尝试")


def recover_interrupted_alert_notifications(now=None, stale_after_seconds: int = 300, db_path=None) -> int:
    instant = _datetime(utc_iso(now))
    cutoff = utc_iso(instant - timedelta(seconds=stale_after_seconds))
    with _db(db_path) as conn:
        _begin(conn)
        cur = conn.execute("UPDATE alerts SET notification_status='unknown' WHERE notification_status='sending' "
                           "AND notification_last_attempt_at_utc<=?", (cutoff,))
        return cur.rowcount


def mailbox_checkpoint(email: str, provider: str = "gmail", now=None, db_path=None) -> dict | None:
    account_id = account_id_for(email, provider, now, db_path)
    with _db(db_path) as conn:
        row = conn.execute("SELECT uidvalidity,last_uid,updated_at_utc FROM mailbox_state WHERE account_id=?",
                           (account_id,)).fetchone()
        return dict(row) if row else None


def save_mailbox_checkpoint(email: str, uidvalidity: str, uid: str | int, *,
                             provider: str = "gmail", now=None, db_path=None) -> None:
    if not str(uidvalidity).isdigit() or not str(uid).isdigit():
        return
    account_id = account_id_for(email, provider, now, db_path)
    with _db(db_path) as conn:
        _begin(conn)
        conn.execute(
            "INSERT INTO mailbox_state(account_id,uidvalidity,last_uid,updated_at_utc) VALUES(?,?,?,?) "
            "ON CONFLICT(account_id) DO UPDATE SET "
            "last_uid=CASE WHEN mailbox_state.uidvalidity=excluded.uidvalidity "
            "THEN MAX(mailbox_state.last_uid,excluded.last_uid) ELSE excluded.last_uid END, "
            "uidvalidity=excluded.uidvalidity,updated_at_utc=excluded.updated_at_utc",
            (account_id, str(uidvalidity), int(uid), utc_iso(now)),
        )
