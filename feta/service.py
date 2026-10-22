"""Use-case layer for FETA. External mail access is explicit and injectable for tests."""
from __future__ import annotations

import csv
from datetime import datetime, timezone
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
import hashlib
from pathlib import Path

from . import parser as reply_parser
from . import repository as repo
from .config import ConfigError, Settings, require_mail_config, validate_alert_destination
from .csv_import import import_csv
from .transport import (
    DeliveryFailed, DeliveryUncertain, MailAuthenticationFailed, MAX_EMAIL_HEADER_BYTES,
    MAX_IMAP_MESSAGES_PER_FETCH, MAX_RAW_EMAIL_BYTES, build_query_message,
    fetch_message_uid, fetch_new_messages,
    send_operator_alert, send_query_email, test_imap_auth, test_smtp_auth,
)


def preview_query(now=None, db_path=None) -> list[dict]:
    batches = repo.preview_batches(now=now, db_path=db_path)
    return [{"count": len(numbers), "tracking_numbers": numbers, "body": "\n".join(numbers)} for numbers in batches]


def send_next_query(settings: Settings, *, now=None, sender=None) -> dict:
    if settings.dry_run:
        raise PermissionError("DRY_RUN=true：只允许 query preview；真实发送须显式设 DRY_RUN=false 并传 --confirm-send")
    require_mail_config(settings, smtp=True)
    state = repo.mail_account_state(settings.db_path)
    if state == "disabled":
        raise PermissionError("邮箱账号已停用；先完成 mail test-auth 后再使用")
    if state == "auth_failed":
        raise PermissionError("邮箱账号认证已失败；先完成 mail test-auth 后再使用")
    repo.recover_interrupted_sends(now=now, db_path=settings.db_path)
    repo.expire_batches(now=now, db_path=settings.db_path)
    account = repo.register_mail_account(settings.mail_user, settings.mail_provider, now, settings.db_path)
    batch, reason = repo.prepare_batch(now=now, interval_hours=settings.query_interval_hours,
                                       reply_timeout_minutes=settings.reply_timeout_minutes,
                                       db_path=settings.db_path)
    if batch is None:
        return {"status": reason, "batch_id": None, "count": 0}
    repo.attach_batch_account(batch["id"], account["id"], settings.db_path)
    message = build_query_message(settings.mail_user, batch["tracking_numbers"])
    try:
        message_id = (sender or send_query_email)(settings, message)
        result = repo.finish_attempt(batch["id"], "submitted", now,
                                     message_id=message_id or str(message["Message-ID"]),
                                     interval_hours=settings.query_interval_hours,
                                     reply_timeout_minutes=settings.reply_timeout_minutes,
                                     db_path=settings.db_path)
    except MailAuthenticationFailed as exc:
        repo.set_mail_account_state("auth_failed", now=now, db_path=settings.db_path)
        result = repo.finish_attempt(batch["id"], "failed", now, failure_reason=type(exc).__name__,
                                     interval_hours=settings.query_interval_hours,
                                     reply_timeout_minutes=settings.reply_timeout_minutes,
                                     db_path=settings.db_path)
    except DeliveryFailed as exc:
        result = repo.finish_attempt(batch["id"], "failed", now, failure_reason=type(exc).__name__,
                                     interval_hours=settings.query_interval_hours,
                                     reply_timeout_minutes=settings.reply_timeout_minutes,
                                     db_path=settings.db_path)
    except DeliveryUncertain as exc:
        result = repo.finish_attempt(batch["id"], "unknown", now, failure_reason=type(exc).__name__,
                                     interval_hours=settings.query_interval_hours,
                                     reply_timeout_minutes=settings.reply_timeout_minutes,
                                     db_path=settings.db_path)
    except Exception as exc:
        # Any unclassified error after calling the sender might happen after SMTP accepted DATA.
        result = repo.finish_attempt(batch["id"], "unknown", now, failure_reason=type(exc).__name__,
                                     interval_hours=settings.query_interval_hours,
                                     reply_timeout_minutes=settings.reply_timeout_minutes,
                                     db_path=settings.db_path)
    return {"status": result["send_status"], "batch_id": batch["id"],
            "count": len(batch["tracking_numbers"]), "retry_count": result["retry_count"]}


def reconcile_query(batch_id: str, result: str, settings: Settings, now=None) -> dict:
    return repo.reconcile_batch(batch_id, result, now=now,
                                interval_hours=settings.query_interval_hours,
                                reply_timeout_minutes=settings.reply_timeout_minutes,
                                db_path=settings.db_path)


def _message_received_at(item: dict) -> datetime:
    value = item.get("received_at")
    if isinstance(value, datetime) and value.tzinfo is not None:
        return value.astimezone(timezone.utc)
    if isinstance(value, str):
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is not None:
            return parsed.astimezone(timezone.utc)
    return datetime.now(timezone.utc)


def _advance_mailbox_checkpoint(settings: Settings, item: dict, now=None) -> None:
    uidvalidity, uid = str(item.get("uidvalidity") or ""), str(item.get("uid") or "")
    if uidvalidity.isdigit() and uid.isdigit():
        repo.save_mailbox_checkpoint(settings.mail_user, uidvalidity, uid,
                                     provider=settings.mail_provider, now=now, db_path=settings.db_path)


def _record_oversize_message(settings: Settings, item: dict, now=None) -> bool:
    raw = item.get("raw") if isinstance(item.get("raw"), bytes) else b""
    bounded_header = raw[:MAX_EMAIL_HEADER_BYTES]
    message = BytesParser(policy=policy.default).parsebytes(bounded_header)
    verified, sender, auth_reason = reply_parser.sender_authentication(message)
    message_id = str(message.get("Message-ID") or "").strip() or None
    if message_id and len(message_id) > 500:
        message_id = message_id[:500]
    summary = {"auth_result": auth_reason, "skipped": item.get("oversize_reason", "message_too_large"),
               "reported_size": item.get("reported_size")}
    incoming_id, inserted = repo.insert_incoming(
        email=settings.mail_user, provider=settings.mail_provider,
        uidvalidity=str(item.get("uidvalidity") or "unknown"), uid=str(item.get("uid") or "unknown"),
        message_id=message_id, received_at=_message_received_at(item), sender=sender,
        auth_verified=verified, body_sha256=hashlib.sha256(bounded_header).hexdigest(),
        reply_status="oversize", parse_result=summary, now=now, db_path=settings.db_path,
    )
    if inserted:
        repo.update_incoming_result(incoming_id, "oversize", summary, settings.db_path)
    _advance_mailbox_checkpoint(settings, item, now)
    return inserted


def fetch_inbox(settings: Settings, *, now=None, messages: list[dict] | None = None, fetcher=None) -> dict:
    require_mail_config(settings, imap=True)
    account_state = repo.mail_account_state(settings.db_path)
    if account_state == "disabled":
        raise PermissionError("邮箱账号已停用")
    if account_state == "auth_failed":
        raise PermissionError("邮箱账号认证已失败；先完成 mail test-auth")
    repo.register_mail_account(settings.mail_user, settings.mail_provider, now, settings.db_path)
    repo.expire_batches(now=now, db_path=settings.db_path)
    if messages is not None:
        downloaded = list(messages)
    else:
        checkpoint = repo.mailbox_checkpoint(settings.mail_user, settings.mail_provider, now, settings.db_path)
        downloaded = (fetcher or fetch_new_messages)(settings, checkpoint)
    downloaded.sort(key=lambda item: int(item.get("uid") or 0) if str(item.get("uid") or "0").isdigit() else 0)
    downloaded = downloaded[:MAX_IMAP_MESSAGES_PER_FETCH]
    known = repo.list_shipments(settings.db_path)
    number_to_shipment = {row["tracking_number"].casefold(): row for row in known}
    numbers_by_timezone: dict[str, list[str]] = {}
    for row in known:
        numbers_by_timezone.setdefault(row["destination_timezone"], []).append(row["tracking_number"])

    processed = reprocessed = duplicates = unverified = events_saved = ambiguous = unmatched = oversize = 0
    for item in downloaded:
        if item.get("checkpoint_only"):
            _advance_mailbox_checkpoint(settings, item, now)
            continue
        raw = item.get("raw")
        if not isinstance(raw, bytes):
            raise DeliveryFailed("IMAP 邮件记录缺少完整/受限原始字节；停止处理并保留 UID 游标")
        if item.get("oversize") or len(raw) > MAX_RAW_EMAIL_BYTES:
            oversize += 1
            if _record_oversize_message(settings, item, now):
                processed += 1
            else:
                duplicates += 1
            continue
        received = _message_received_at(item)
        message = BytesParser(policy=policy.default).parsebytes(raw)
        verified, sender, auth_reason = reply_parser.sender_authentication(message)
        message_id = str(message.get("Message-ID") or "").strip() or None
        if message_id and len(message_id) > 500:
            message_id = message_id[:500]
        digest = hashlib.sha256(raw).hexdigest()
        results: list[dict] = []
        correlations: list[dict] = []
        if verified:
            text = reply_parser.extract_message_text(message)
            for timezone_name, tz_numbers in numbers_by_timezone.items():
                results.extend(reply_parser.parse_reply_text(text, tz_numbers,
                               destination_timezone=timezone_name,
                               received_at_utc=repo.utc_iso(received)))
            for result in results:
                candidates = repo.candidate_attempts(result["tracking_number"], received, db_path=settings.db_path)
                candidate_batch_ids = {candidate["batch_id"] for candidate in candidates}
                if len(candidate_batch_ids) == 1:
                    # A retry is another attempt of the same logical batch. Match to that batch,
                    # preferring the latest attempt returned by the repository query.
                    correlations.append({**result, "correlation": "matched", "batch_id": candidates[0]["batch_id"]})
                elif len(candidate_batch_ids) > 1:
                    correlations.append({**result, "correlation": "ambiguous", "batch_id": None})
                    ambiguous += 1
                else:
                    correlations.append({**result, "correlation": "unmatched", "batch_id": None})
                    unmatched += 1
        else:
            unverified += 1

        if not verified:
            status = "unverified"
        elif not correlations:
            status = "unmatched"
        elif any(row["correlation"] == "ambiguous" for row in correlations):
            status = "ambiguous"
        elif all(row["correlation"] == "matched" for row in correlations):
            status = "matched"
        elif any(row["correlation"] == "matched" for row in correlations):
            status = "partial"
        else:
            status = "unmatched"
        summary = {"auth_result": auth_reason, "events": correlations}
        incoming_id, inserted = repo.insert_incoming(
            email=settings.mail_user, provider=settings.mail_provider,
            uidvalidity=str(item.get("uidvalidity") or "unknown"), uid=str(item.get("uid") or "unknown"),
            message_id=message_id, received_at=received, sender=sender, auth_verified=verified,
            body_sha256=digest, reply_status=status, parse_result=summary, now=now, db_path=settings.db_path,
        )
        if not inserted:
            if not verified or not repo.promote_unverified_incoming(
                    incoming_id, sender=sender, body_sha256=digest, reply_status=status,
                    parse_result=summary, db_path=settings.db_path):
                duplicates += 1
                _advance_mailbox_checkpoint(settings, item, now)
                continue
            reprocessed += 1
        else:
            processed += 1
        matched_by_batch: dict[str, set[str]] = {}
        for result in correlations:
            if result["correlation"] != "matched":
                continue
            shipment = number_to_shipment.get(result["tracking_number"].casefold())
            if not shipment:
                continue
            repo.record_event(
                shipment_id=shipment["id"], batch_id=result["batch_id"], source_message_id=incoming_id,
                received_at=received, raw_status=result["raw_status"],
                normalized_status=result["normalized_status"], raw_eta_text=result["raw_eta_text"],
                destination_timezone=result["destination_timezone"],
                parse_confidence=result["parse_confidence"], parser_version=result["parser_version"],
                db_path=settings.db_path,
            )
            matched_by_batch.setdefault(result["batch_id"], set()).add(result["tracking_number"])
            events_saved += 1
        batch_statuses = []
        for batch_id, matched_numbers in matched_by_batch.items():
            batch_statuses.append(repo.update_batch_reply_status(batch_id, matched_numbers, partial=False,
                                                                 db_path=settings.db_path))
        if any(value == "partial" for value in batch_statuses):
            status = "partial"
        elif batch_statuses and all(value == "completed" for value in batch_statuses) and status != "ambiguous":
            status = "matched"
        repo.update_incoming_result(incoming_id, status, summary, settings.db_path)
        _advance_mailbox_checkpoint(settings, item, now)
    batch_targets = [(str(item.get("uidvalidity") or ""), int(item["checkpoint_after_batch"]))
                     for item in downloaded
                     if str(item.get("uidvalidity") or "").isdigit()
                     and str(item.get("checkpoint_after_batch") or "").isdigit()]
    if batch_targets:
        uidvalidity, checkpoint_uid = batch_targets[0]
        _advance_mailbox_checkpoint(settings, {"uidvalidity": uidvalidity, "uid": str(max(uid for _, uid in batch_targets))}, now)
    return {"processed": processed, "reprocessed": reprocessed, "duplicates": duplicates, "unverified": unverified,
            "oversize": oversize, "events_saved": events_saved, "ambiguous": ambiguous, "unmatched": unmatched}


def reprocess_incoming(settings: Settings, incoming_id: int, *, fetcher=None, now=None) -> dict:
    """Explicitly re-fetch one unverified UID; only a newly verified message can create review events."""
    detail = repo.incoming_message_detail(incoming_id, settings.db_path)
    if detail["auth_verified"]:
        return {"status": "already_verified", "reprocessed": 0, "events_saved": 0}
    item = (fetcher or fetch_message_uid)(settings, detail["uidvalidity"], detail["uid"])
    result = fetch_inbox(settings, now=now, messages=[item])
    return {"status": "reprocessed" if result["reprocessed"] else "still_unverified_or_duplicate", **result}


def test_mail_auth(settings: Settings, *, smtp_test=None, imap_test=None, now=None) -> dict:
    require_mail_config(settings, smtp=True, imap=True)
    repo.register_mail_account(settings.mail_user, settings.mail_provider, now, settings.db_path)
    try:
        (smtp_test or test_smtp_auth)(settings)
    except Exception as exc:
        repo.set_mail_account_state("auth_failed", now=now, db_path=settings.db_path)
        detail = type(exc.__cause__).__name__ if exc.__cause__ is not None else type(exc).__name__
        raise ConfigError(f"SMTP 连接/认证失败：{detail}") from exc
    try:
        (imap_test or test_imap_auth)(settings)
    except Exception as exc:
        repo.set_mail_account_state("auth_failed", now=now, db_path=settings.db_path)
        detail = type(exc.__cause__).__name__ if exc.__cause__ is not None else type(exc).__name__
        raise ConfigError(f"IMAP 连接/认证失败：{detail}") from exc
    repo.set_mail_account_state("active", now=now, db_path=settings.db_path)
    return {"smtp": "ok", "imap": "ok", "account_state": "active"}


def review_event(event_id: int, decision: str, settings: Settings, now=None) -> dict:
    return repo.review_event(event_id, decision, now=now, db_path=settings.db_path)


def _safe_csv_cell(value):
    if value is None:
        return ""
    text = str(value)
    if text.startswith(("=", "+", "-", "@", "\t", "\r")):
        return "'" + text
    return text


def export_report(output: str | Path, db_path=None) -> int:
    path = Path(output)
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = repo.report_rows(db_path)
    columns = ["order_id", "tracking_number", "carrier", "status", "raw_status", "raw_eta_text",
               "eta_utc", "destination_timezone", "last_query_at_utc", "last_success_at_utc", "active", "paused"]
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({key: _safe_csv_cell(row.get(key)) for key in columns})
    return len(rows)


def _alert_message(settings: Settings, alert: dict) -> EmailMessage:
    message = EmailMessage()
    message["From"] = settings.mail_user
    message["To"] = settings.alert_email
    message["Subject"] = f"FETA {alert['severity'].upper()} alert: {alert['rule_code']}"
    message.set_content(
        "FedEx email tracking status requires operator review.\n\n"
        f"Tracking number: {alert['tracking_number']}\n"
        f"Status: {alert['normalized_status']}\n"
        f"Rule: {alert['rule_code']}\n"
        "This is a logistics status alert, not an SMTP/query timeout alert.\n"
    )
    return message


def run_alerts(settings: Settings, *, send: bool = False, sender=None, now=None) -> dict:
    repo.recover_interrupted_alert_notifications(now=now, db_path=settings.db_path)
    alerts = repo.evaluate_alerts(now=now, db_path=settings.db_path)
    pending_notifications = repo.pending_alert_notifications(settings.db_path)
    sent = failed = unknown = 0
    if send and pending_notifications:
        if settings.dry_run:
            raise PermissionError("DRY_RUN=true：不会发送告警邮件")
        require_mail_config(settings, smtp=True)
        validate_alert_destination(settings)
        for alert in pending_notifications:
            if not repo.begin_alert_notification(alert["id"], now=now, db_path=settings.db_path):
                continue
            try:
                (sender or send_operator_alert)(settings, _alert_message(settings, alert))
            except DeliveryFailed:
                repo.finish_alert_notification(alert["id"], "failed", now=now, db_path=settings.db_path)
                failed += 1
            except Exception:
                # SMTP may have accepted the message before the connection failed; hold for manual check.
                repo.finish_alert_notification(alert["id"], "unknown", now=now, db_path=settings.db_path)
                unknown += 1
            else:
                repo.finish_alert_notification(alert["id"], "sent", now=now, db_path=settings.db_path)
                sent += 1
    return {"new_alerts": alerts, "pending_notifications": len(pending_notifications),
            "sent": sent, "failed": failed, "unknown": unknown}
