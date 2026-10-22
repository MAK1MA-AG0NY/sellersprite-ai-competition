from __future__ import annotations

from datetime import datetime, timedelta, timezone
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import smtplib

from feta import parser, repository as repo, transport
from feta.config import load_settings
from feta.csv_import import import_csv, parse_csv
from feta.service import fetch_inbox, preview_query, reprocess_incoming, run_alerts, send_next_query, test_mail_auth
from feta.transport import DeliveryFailed, MailAuthenticationFailed, build_query_message


NOW = datetime(2026, 1, 5, 15, 0, tzinfo=timezone.utc)


def csv_bytes(count: int) -> bytes:
    lines = ["order_id,tracking_number,carrier,destination_timezone,active"]
    for index in range(count):
        # Deliberately preserve a leading zero and avoid using live business data.
        lines.append(f"SYN-{index:04d},0{index + 1:011d},FedEx,America/Chicago,1")
    return ("\n".join(lines) + "\n").encode("utf-8")


def settings(db_path: Path, **overrides):
    base = {
        "FETA_DB_PATH": str(db_path), "DRY_RUN": "false", "MAIL_USER": "monitor@example.test",
        "MAIL_APP_PASSWORD": "test-only-not-a-real-secret", "SMTP_HOST": "smtp.example.test",
        "SMTP_PORT": "465", "IMAP_HOST": "imap.example.test", "IMAP_PORT": "993",
        "ALERT_EMAIL": "ops@example.test",
    }
    base.update(overrides)
    return load_settings(environ=base, env_file=db_path.parent / "missing.env")


class FetaTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / "feta-test.sqlite3"

    def import_count(self, count):
        return import_csv(csv_bytes(count), self.db, now=NOW)

    def test_batch_boundaries_and_global_one_batch_round_limit(self):
        for count, expected in ((0, 0), (1, 1), (30, 1), (31, 2), (62, 3)):
            with self.subTest(count=count):
                db = self.db.parent / f"{count}.sqlite3"
                if count:
                    import_csv(csv_bytes(count), db, now=NOW)
                batches = repo.preview_batches(now=NOW, db_path=db)
                self.assertEqual(len(batches), expected)
                self.assertTrue(all(1 <= len(batch) <= 30 for batch in batches))
                flattened = [number for batch in batches for number in batch]
                self.assertEqual(len(flattened), len(set(flattened)))
                if count:
                    conf = settings(db)
                    sent = []
                    result = send_next_query(conf, now=NOW, sender=lambda cfg, msg: sent.append(msg) or "<fake-id>")
                    self.assertEqual(result["status"], "submitted")
                    self.assertEqual(len(sent), 1)
                    self.assertEqual(len(sent[0].get_content().splitlines()), min(count, 30))
                    second = send_next_query(conf, now=NOW + timedelta(minutes=1),
                                             sender=lambda cfg, msg: self.fail("same round must not send twice"))
                    self.assertEqual(second["status"], "cooldown")

    def test_settings_repr_never_discloses_secret_or_mail_addresses(self):
        conf = settings(self.db)
        shown = repr(conf)
        self.assertNotIn("test-only-not-a-real-secret", shown)
        self.assertNotIn("monitor@example.test", shown)
        self.assertNotIn("ops@example.test", shown)

    def test_csv_deduplicates_tracking_but_keeps_multiple_orders_and_leading_zero(self):
        data = (b"order_id,tracking_number,carrier,destination_timezone,active\n"
                b"ORD-A,001234567890,FedEx,America/Chicago,1\n"
                b"ORD-B, 00\xe2\x80\x8b1234567890 ,fedex,America/Los_Angeles,1\n")
        result = import_csv(data, self.db, now=NOW)
        self.assertEqual(result, {"added_shipments": 1, "duplicate_rows": 1, "order_links_added": 2})
        rows = repo.list_shipments(self.db)
        self.assertEqual(rows[0]["tracking_number"], "001234567890")
        self.assertEqual(rows[0]["order_ids"], "ORD-A, ORD-B")
        self.assertEqual(rows[0]["destination_timezone"], "America/Los_Angeles")

    def test_csv_validation_is_atomic_and_rejects_non_fedex_or_bad_timezone(self):
        with self.assertRaisesRegex(ValueError, "FedEx"):
            parse_csv(b"order_id,tracking_number,carrier,destination_timezone,active\nO,123,USPS,UTC,1\n")
        with self.assertRaisesRegex(ValueError, "IANA"):
            parse_csv(b"order_id,tracking_number,carrier,destination_timezone,active\nO,123,FedEx,America/NoSuchCity,1\n")
        with self.assertRaisesRegex(ValueError, "跟踪号"):
            parse_csv(b"order_id,tracking_number,carrier,destination_timezone,active\nO,123$,FedEx,UTC,1\n")

    def test_query_message_has_empty_subject_and_only_tracking_numbers_in_body(self):
        numbers = ["000012345678", "FXA-12345"]
        message = build_query_message("monitor@example.test", numbers)
        self.assertIsNone(message.get("Subject"))
        self.assertEqual(message.get_content().splitlines(), numbers)
        self.assertNotIn("ORDER", message.get_content().upper())
        with self.assertRaises(ValueError):
            build_query_message("monitor@example.test", ["1"] * 31)

    def test_dry_run_blocks_smtp_without_creating_batch(self):
        self.import_count(1)
        conf = settings(self.db, DRY_RUN="true")
        with self.assertRaises(PermissionError):
            send_next_query(conf, now=NOW, sender=lambda *_: self.fail("must never be called"))
        self.assertEqual(repo.preview_batches(now=NOW, db_path=self.db)[0][0], "000000000001")

    def test_mail_auth_test_and_rejected_smtp_credentials_pause_account(self):
        self.import_count(1)
        conf = settings(self.db)
        result = test_mail_auth(conf, smtp_test=lambda *_: None, imap_test=lambda *_: None, now=NOW)
        self.assertEqual(result["account_state"], "active")
        failed = send_next_query(conf, now=NOW, sender=lambda *_: (_ for _ in ()).throw(
            MailAuthenticationFailed("synthetic auth rejection")))
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(repo.mail_account_state(self.db), "auth_failed")
        with self.assertRaises(PermissionError):
            send_next_query(conf, now=NOW + timedelta(days=1), sender=lambda *_: self.fail("auth_failed blocks retries"))

    def test_smtp_failure_retries_with_backoff_and_unknown_is_not_retried(self):
        self.import_count(1)
        conf = settings(self.db)
        failed = send_next_query(conf, now=NOW, sender=lambda *_: (_ for _ in ()).throw(DeliveryFailed("refused")))
        self.assertEqual(failed["status"], "failed")
        before = send_next_query(conf, now=NOW + timedelta(minutes=29), sender=lambda *_: self.fail("too early"))
        self.assertEqual(before["status"], "cooldown")
        retried = send_next_query(conf, now=NOW + timedelta(minutes=30), sender=lambda *_: "<retry-id>")
        self.assertEqual(retried["status"], "submitted")

        other_db = self.db.parent / "uncertain.sqlite3"
        import_csv(csv_bytes(1), other_db, now=NOW)
        uncertain_conf = settings(other_db)
        unknown = send_next_query(uncertain_conf, now=NOW,
                                  sender=lambda *_: (_ for _ in ()).throw(ConnectionError("maybe accepted")))
        self.assertEqual(unknown["status"], "unknown")
        with self.assertRaisesRegex(ValueError, "只能核对"):
            repo.reconcile_batch("not-a-batch", "submitted", db_path=other_db)
        self.assertEqual(repo.preview_batches(now=NOW + timedelta(days=2), db_path=other_db), [])

    def test_review_events_cannot_roll_back_status_or_reopen_terminal_without_resume(self):
        self.import_count(1)
        tracking = repo.list_shipments(self.db)[0]["tracking_number"]

        def add_event(uid, received, status):
            incoming_id, inserted = repo.insert_incoming(
                email="monitor@example.test", provider="gmail", uidvalidity="77", uid=str(uid),
                message_id=f"<review-{uid}@example.test>", received_at=received, sender="track@fedex.com",
                auth_verified=True, body_sha256=f"{uid:064x}", reply_status="matched", parse_result={}, db_path=self.db)
            self.assertTrue(inserted)
            return repo.record_event(
                shipment_id=repo.list_shipments(self.db)[0]["id"], batch_id=None, source_message_id=incoming_id,
                received_at=received, raw_status=status, normalized_status=status, raw_eta_text=None,
                destination_timezone="America/Chicago", parse_confidence="low", parser_version="test", db_path=self.db)

        delivered_at = NOW + timedelta(days=2)
        delivered_event = add_event(1, delivered_at, "DELIVERED")
        older_event = add_event(2, NOW + timedelta(days=1), "IN_TRANSIT")
        later_event = add_event(3, NOW + timedelta(days=3), "IN_TRANSIT")
        repo.review_event(delivered_event, "accept", db_path=self.db)
        with self.assertRaisesRegex(ValueError, "早于"):
            repo.review_event(older_event, "accept", db_path=self.db)
        self.assertEqual(repo.list_shipments(self.db)[0]["status"], "DELIVERED")
        with self.assertRaisesRegex(ValueError, "终态"):
            repo.review_event(later_event, "accept", db_path=self.db)
        repo.enable_shipment(tracking, now=NOW + timedelta(days=3), db_path=self.db)
        repo.review_event(later_event, "accept", db_path=self.db)
        row = repo.list_shipments(self.db)[0]
        self.assertEqual(row["status"], "IN_TRANSIT")
        self.assertEqual(row["active"], 1)

    def test_validate_review_event_is_read_only_and_reuses_accept_guards(self):
        self.import_count(1)
        shipment = repo.list_shipments(self.db)[0]
        received = NOW + timedelta(hours=1)
        incoming_id, inserted = repo.insert_incoming(
            email="monitor@example.test", provider="gmail", uidvalidity="88", uid="1",
            message_id="<validate@example.test>", received_at=received, sender="track@fedex.com",
            auth_verified=True, body_sha256="88" * 32, reply_status="matched", parse_result={}, db_path=self.db)
        self.assertTrue(inserted)
        event_id = repo.record_event(
            shipment_id=shipment["id"], batch_id=None, source_message_id=incoming_id,
            received_at=received, raw_status="In transit", normalized_status="IN_TRANSIT",
            raw_eta_text=None, destination_timezone="America/Chicago", parse_confidence="low",
            parser_version="test", db_path=self.db)

        preview = repo.validate_review_event(event_id, "accept", db_path=self.db)
        self.assertEqual(preview["review_state"], "pending")
        self.assertEqual(repo.list_shipments(self.db)[0]["status"], "UNKNOWN")
        self.assertEqual(repo.review_events(self.db)[0]["id"], event_id)
        repo.review_event(event_id, "accept", db_path=self.db)
        with self.assertRaisesRegex(ValueError, "已复核"):
            repo.validate_review_event(event_id, "accept", db_path=self.db)

    def test_late_email_with_older_scan_status_cannot_regress_progress(self):
        self.import_count(1)
        tracking = repo.list_shipments(self.db)[0]["tracking_number"]
        repo.register_mail_account("monitor@example.test", "gmail", db_path=self.db)

        def add(status, received, uid):
            incoming, _ = repo.insert_incoming(
                email="monitor@example.test", provider="gmail", uidvalidity="91", uid=str(uid),
                message_id=f"<progress-{uid}@example.test>", received_at=received, sender="track@fedex.com",
                auth_verified=True, body_sha256=f"{uid:064x}", reply_status="matched", parse_result={}, db_path=self.db)
            return repo.record_event(
                shipment_id=repo.list_shipments(self.db)[0]["id"], batch_id=None, source_message_id=incoming,
                received_at=received, raw_status=status, normalized_status=status, raw_eta_text=None,
                destination_timezone="America/Chicago", parse_confidence="low", parser_version="test", db_path=self.db)

        out_for_delivery = add("OUT_FOR_DELIVERY", NOW + timedelta(hours=1), 1)
        later_received_old_scan = add("IN_TRANSIT", NOW + timedelta(hours=2), 2)
        repo.review_event(out_for_delivery, "accept", db_path=self.db)
        with self.assertRaisesRegex(ValueError, "低于当前已接受进度"):
            repo.review_event(later_received_old_scan, "accept", db_path=self.db)
        row = repo.list_shipments(self.db)[0]
        self.assertEqual(row["status"], "OUT_FOR_DELIVERY")
        self.assertEqual(row["last_success_at_utc"], repo.utc_iso(NOW + timedelta(hours=1)))

    def test_timeout_is_system_state_not_package_exception_and_uses_backoff(self):
        self.import_count(1)
        conf = settings(self.db)
        sent = send_next_query(conf, now=NOW, sender=lambda *_: "<accepted>")
        expired = repo.expire_batches(now=NOW + timedelta(minutes=61), db_path=self.db)
        self.assertEqual(expired, [sent["batch_id"]])
        batch = repo.batch_details(sent["batch_id"], self.db)
        self.assertEqual(batch["reply_status"], "timeout")
        self.assertEqual(repo.list_shipments(self.db)[0]["status"], "UNKNOWN")
        before_retry = send_next_query(conf, now=NOW + timedelta(minutes=91), sender=lambda *_: "<retry>")
        self.assertEqual(before_retry["status"], "submitted")

    def test_interrupted_send_is_conservatively_held_for_reconciliation(self):
        self.import_count(1)
        batch, _ = repo.prepare_batch(now=NOW, db_path=self.db)
        self.assertIsNotNone(batch)
        self.assertEqual(repo.recover_interrupted_sends(now=NOW + timedelta(minutes=6), db_path=self.db), 1)
        details = repo.batch_details(batch["id"], self.db)
        self.assertEqual(details["send_status"], "unknown")
        self.assertEqual(repo.preview_batches(now=NOW + timedelta(days=1), db_path=self.db), [])
        result = repo.reconcile_batch(batch["id"], "submitted", now=NOW + timedelta(minutes=2), db_path=self.db)
        self.assertEqual(result["send_status"], "submitted")

    def test_imap_uid_and_message_id_dedup_with_synthetic_authenticated_mime(self):
        self.import_count(2)
        conf = settings(self.db)
        sent = send_next_query(conf, now=NOW, sender=lambda *_: "<outgoing>")
        outgoing = repo.batch_details(sent["batch_id"], self.db)
        self.assertEqual(outgoing["send_status"], "submitted")

        raw = self._email_bytes(repo.list_shipments(self.db)[0]["tracking_number"], "Delivered", "Estimated delivery: none")
        item = {"uidvalidity": "7", "uid": "12", "raw": raw, "received_at": NOW + timedelta(minutes=5)}
        first = fetch_inbox(conf, now=NOW + timedelta(minutes=5), messages=[item])
        self.assertEqual(first["processed"], 1)
        self.assertEqual(first["events_saved"], 1)
        second = fetch_inbox(conf, now=NOW + timedelta(minutes=6), messages=[item])
        self.assertEqual(second["duplicates"], 1)
        # UIDVALIDITY/UID changed, but the Message-ID still prevents replay.
        third = fetch_inbox(conf, now=NOW + timedelta(minutes=7), messages=[
            {"uidvalidity": "8", "uid": "99", "raw": raw, "received_at": NOW + timedelta(minutes=7)}])
        self.assertEqual(third["duplicates"], 1)
        checkpoint = repo.mailbox_checkpoint(conf.mail_user, conf.mail_provider, db_path=self.db)
        self.assertEqual((checkpoint["uidvalidity"], checkpoint["last_uid"]), ("8", 99))
        pending = repo.review_events(self.db)
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["normalized_status"], "DELIVERED")
        self.assertEqual(pending[0]["raw_eta_text"], "none")
        self.assertTrue(pending[0]["needs_manual_review"])
        repo.review_event(pending[0]["id"], "accept", db_path=self.db)
        self.assertEqual(repo.list_shipments(self.db)[0]["status"], "DELIVERED")
        self.assertEqual(repo.list_shipments(self.db)[0]["active"], 0)

    @staticmethod
    def _email_bytes(tracking, status, eta=""):
        return FetaTests._email_body_bytes(
            f"Tracking number: {tracking}\nStatus: {status}\n{eta}\n", "<fedex-reply-1@example.test>")

    @staticmethod
    def _email_body_bytes(body, message_id):
        message = EmailMessage()
        message["From"] = "FedEx Tracking <track@fedex.com>"
        message["To"] = "monitor@example.test"
        message["Message-ID"] = message_id
        message["Authentication-Results"] = "mx.google.com; dkim=pass header.d=fedex.com; dmarc=pass header.from=fedex.com"
        message.set_content(body)
        return message.as_bytes(policy=policy.SMTP)

    def test_html_only_is_stripped_and_unknown_stays_manual_review(self):
        message = BytesParser(policy=policy.default).parsebytes(
            b"From: track@fedex.com\nAuthentication-Results: mx.google.com; dkim=pass header.d=fedex.com\n"
            b"Content-Type: text/html; charset=utf-8\n\n<html><script>Delivered</script><p>Tracking: 000123</p>"
            b"<p>Unrecognized scan phrase</p></html>")
        self.assertNotIn("Delivered", parser.extract_message_text(message))
        result = parser.parse_reply_text(parser.extract_message_text(message), ["000123"],
                                         destination_timezone="America/Chicago", received_at_utc=repo.utc_iso(NOW))
        self.assertEqual(result[0]["normalized_status"], "UNKNOWN")
        self.assertTrue(result[0]["needs_manual_review"])
        self.assertIsNone(result[0]["eta_datetime_utc"])

    def test_negative_delivery_language_is_not_classified_as_delivered(self):
        results = parser.parse_reply_text(
            "Tracking number: 000123\\nStatus: Not delivered due to address exception",
            ["000123"], destination_timezone="America/Chicago", received_at_utc=repo.utc_iso(NOW))
        self.assertEqual(results[0]["normalized_status"], "EXCEPTION")
        self.assertNotEqual(results[0]["normalized_status"], "DELIVERED")
        no_status = parser.parse_reply_text(
            "Tracking number: 000123\\nPackage was not delivered",
            ["000123"], destination_timezone="America/Chicago", received_at_utc=repo.utc_iso(NOW))
        self.assertEqual(no_status[0]["normalized_status"], "UNKNOWN")

    def test_unverified_from_and_missing_auth_results_are_not_parsed(self):
        self.assertFalse(parser.sender_authentication(BytesParser(policy=policy.default).parsebytes(
            b"From: track@fedex.com\n\nDelivered 000123"))[0])
        self.assertFalse(parser.sender_authentication(BytesParser(policy=policy.default).parsebytes(
            b"From: track@evil.test\nAuthentication-Results: mx.google.com; dkim=pass header.d=fedex.com\n\nDelivered 000123"))[0])
        self.assertFalse(parser.sender_authentication(BytesParser(policy=policy.default).parsebytes(
            b"From: track@fedex.com\nAuthentication-Results: mx.google.com; dkim=pass header.d=attacker.test; "
            b"dmarc=pass header.from=attacker.test\n\nDelivered 000123"))[0])

    def test_alerts_deduplicate_and_reopen_after_status_recovery(self):
        self.import_count(1)
        with repo._db(self.db) as conn:
            conn.execute("UPDATE shipments SET status='EXCEPTION' WHERE tracking_number='000000000001'")
        first = run_alerts(settings(self.db), now=NOW)
        second = run_alerts(settings(self.db), now=NOW + timedelta(minutes=5))
        self.assertEqual(len(first["new_alerts"]), 1)
        self.assertEqual(len(second["new_alerts"]), 0)
        with repo._db(self.db) as conn:
            conn.execute("UPDATE shipments SET status='IN_TRANSIT' WHERE tracking_number='000000000001'")
        run_alerts(settings(self.db), now=NOW + timedelta(minutes=10))
        with repo._db(self.db) as conn:
            conn.execute("UPDATE shipments SET status='EXCEPTION' WHERE tracking_number='000000000001'")
        third = run_alerts(settings(self.db), now=NOW + timedelta(minutes=15))
        self.assertEqual(len(third["new_alerts"]), 1)

    def test_preview_is_pure_and_report_rows_preserve_order_associations(self):
        self.import_count(2)
        before = repo.preview_batches(now=NOW, db_path=self.db)
        self.assertEqual(len(before), 1)
        self.assertEqual(len(preview_query(now=NOW, db_path=self.db)[0]["body"].splitlines()), 2)
        with repo._db(self.db) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM query_batches").fetchone()[0], 0)
        self.assertEqual(len(repo.report_rows(self.db)), 2)


    def test_oversize_message_is_recorded_without_body_parsing_or_repeated_fetch_processing(self):
        conf = settings(self.db)
        header = (b"From: track@fedex.com\r\n"
                  b"Authentication-Results: mx.google.com; dkim=pass header.d=fedex.com\r\n"
                  b"Message-ID: <large@example.test>\r\n\r\n")
        item = {"uidvalidity": "50", "uid": "3", "raw": header, "oversize": True,
                "oversize_reason": "message_too_large", "reported_size": 2 * 1024 * 1024,
                "received_at": NOW}
        first = fetch_inbox(conf, now=NOW, messages=[item])
        self.assertEqual(first["oversize"], 1)
        self.assertEqual(first["events_saved"], 0)
        incoming = repo.list_incoming_messages(db_path=self.db)[0]
        detail = repo.incoming_message_detail(incoming["id"], self.db)
        self.assertEqual(detail["reply_status"], "oversize")
        self.assertEqual(detail["parse_result"]["skipped"], "message_too_large")
        repeated = fetch_inbox(conf, now=NOW + timedelta(minutes=1), messages=[item])
        self.assertEqual(repeated["duplicates"], 1)

    def test_unverified_mail_can_be_explicitly_refetched_and_revalidated(self):
        self.import_count(1)
        conf = settings(self.db)
        send_next_query(conf, now=NOW, sender=lambda *_: "<query>")
        tracking = repo.list_shipments(self.db)[0]["tracking_number"]
        unverified = EmailMessage()
        unverified["From"] = "FedEx Tracking <track@fedex.com>"
        unverified["To"] = conf.mail_user
        unverified["Message-ID"] = "<revalidate@example.test>"
        unverified.set_content(f"Tracking number: {tracking}\nStatus: Delivered")
        raw_unverified = unverified.as_bytes(policy=policy.SMTP)
        initial = fetch_inbox(conf, now=NOW + timedelta(minutes=5), messages=[
            {"uidvalidity": "41", "uid": "21", "raw": raw_unverified,
             "received_at": NOW + timedelta(minutes=5)}])
        self.assertEqual(initial["unverified"], 1)
        self.assertEqual(initial["events_saved"], 0)
        incoming_id = repo.list_incoming_messages(db_path=self.db)[0]["id"]
        raw_verified = self._email_body_bytes(
            f"Tracking number: {tracking}\nStatus: Delivered", "<revalidate@example.test>")
        retried = reprocess_incoming(
            conf, incoming_id, now=NOW + timedelta(minutes=6),
            fetcher=lambda *_: {"uidvalidity": "41", "uid": "21", "raw": raw_verified,
                                "received_at": NOW + timedelta(minutes=5)})
        self.assertEqual(retried["reprocessed"], 1)
        self.assertEqual(retried["events_saved"], 1)
        self.assertTrue(repo.incoming_message_detail(incoming_id, self.db)["auth_verified"])
        self.assertEqual(repo.list_shipments(self.db)[0]["status"], "UNKNOWN")
        self.assertEqual(repo.review_events(self.db)[0]["normalized_status"], "DELIVERED")

    def test_multi_number_reply_marks_partial_without_applying_status_automatically(self):
        self.import_count(3)
        conf = settings(self.db)
        sent = send_next_query(conf, now=NOW, sender=lambda *_: "<query-1>")
        rows = repo.list_shipments(self.db)
        body = (f"Tracking number: {rows[0]['tracking_number']}\nException\n"
                f"Tracking number: {rows[1]['tracking_number']}\nOut for delivery\n")
        raw = self._email_body_bytes(body, "<multi-reply@example.test>")
        result = fetch_inbox(conf, now=NOW + timedelta(minutes=5), messages=[
            {"uidvalidity": "10", "uid": "4", "raw": raw, "received_at": NOW + timedelta(minutes=5)}])
        self.assertEqual(result["events_saved"], 2)
        self.assertEqual(repo.batch_details(sent["batch_id"], self.db)["reply_status"], "partial")
        events = repo.review_events(self.db)
        self.assertEqual([event["normalized_status"] for event in events], ["EXCEPTION", "OUT_FOR_DELIVERY"])
        self.assertTrue(all(event["needs_manual_review"] for event in events))
        self.assertTrue(all(row["status"] == "UNKNOWN" for row in repo.list_shipments(self.db)))

    def test_partial_timeout_retry_only_queries_unanswered_and_same_batch_attempts_match(self):
        self.import_count(2)
        conf = settings(self.db)
        first_send = send_next_query(conf, now=NOW, sender=lambda *_: "<initial>")
        rows = repo.list_shipments(self.db)
        first_number, second_number = [row["tracking_number"] for row in rows]
        first_reply = self._email_bytes(first_number, "In transit")
        first_result = fetch_inbox(conf, now=NOW + timedelta(minutes=5), messages=[
            {"uidvalidity": "30", "uid": "1", "raw": first_reply, "received_at": NOW + timedelta(minutes=5)}])
        self.assertEqual(first_result["events_saved"], 1)
        self.assertEqual(repo.batch_details(first_send["batch_id"], self.db)["reply_status"], "partial")

        repo.expire_batches(now=NOW + timedelta(minutes=61), db_path=self.db)
        retry_body = []
        retry_result = send_next_query(
            conf, now=NOW + timedelta(minutes=91),
            sender=lambda _cfg, message: retry_body.append(message.get_content().splitlines()) or "<retry>")
        self.assertEqual(retry_result["batch_id"], first_send["batch_id"])
        self.assertEqual(retry_body, [[second_number]])
        attempts = repo.batch_details(first_send["batch_id"], self.db)["attempts"]
        self.assertEqual(attempts[0]["tracking_numbers"], [first_number, second_number])
        self.assertEqual(attempts[1]["tracking_numbers"], [second_number])
        candidates = repo.candidate_attempts(second_number, NOW + timedelta(minutes=92), db_path=self.db)
        self.assertEqual(len(candidates), 2)
        self.assertEqual({candidate["batch_id"] for candidate in candidates}, {first_send["batch_id"]})

        second_reply = self._email_body_bytes(
            f"Tracking number: {second_number}\nStatus: Out for delivery\n", "<fedex-reply-2@example.test>")
        second_result = fetch_inbox(conf, now=NOW + timedelta(minutes=92), messages=[
            {"uidvalidity": "30", "uid": "2", "raw": second_reply, "received_at": NOW + timedelta(minutes=92)}])
        self.assertEqual(second_result["events_saved"], 1)
        self.assertEqual(repo.batch_details(first_send["batch_id"], self.db)["reply_status"], "completed")

    def test_mime_plain_text_preferred_over_html_and_base64_decodes(self):
        message = EmailMessage()
        message.set_content("Tracking: 000123\nStatus: In transit", cte="base64")
        message.add_alternative("<p>Tracking: 000123</p><p>Status: Delivered</p>", subtype="html")
        text = parser.extract_message_text(message)
        self.assertIn("In transit", text)
        self.assertNotIn("Delivered", text)
        quoted = EmailMessage()
        quoted.set_content("Status: en route — at facility", cte="quoted-printable")
        self.assertIn("en route", parser.extract_message_text(quoted))
        result = parser.parse_reply_text(text, ["000123"], destination_timezone="America/Chicago",
                                        received_at_utc=repo.utc_iso(NOW))
        self.assertEqual(result[0]["normalized_status"], "IN_TRANSIT")
        self.assertIsNone(result[0]["eta_datetime_utc"])

    def test_partial_batch_timeout_exhaustion_pauses_only_unanswered_shipment(self):
        self.import_count(2)
        conf = settings(self.db)
        sent = send_next_query(conf, now=NOW, sender=lambda *_: "<initial>")
        rows = repo.list_shipments(self.db)
        answered, unanswered = [row["tracking_number"] for row in rows]
        reply = self._email_bytes(answered, "In transit")
        result = fetch_inbox(conf, now=NOW + timedelta(minutes=5), messages=[
            {"uidvalidity": "90", "uid": "1", "raw": reply, "received_at": NOW + timedelta(minutes=5)}])
        self.assertEqual(result["events_saved"], 1)
        with repo._db(self.db) as conn:
            conn.execute("UPDATE query_batches SET retry_count=?,timeout_at_utc=? WHERE id=?",
                         (repo.MAX_RETRIES - 1, repo.utc_iso(NOW + timedelta(minutes=6)), sent["batch_id"]))
        expired = repo.expire_batches(now=NOW + timedelta(minutes=7), db_path=self.db)
        self.assertEqual(expired, [sent["batch_id"]])
        states = {row["tracking_number"]: row for row in repo.list_shipments(self.db)}
        self.assertEqual(states[answered]["paused"], 0)
        self.assertEqual(states[unanswered]["paused"], 1)

    def test_three_backoff_retries_then_pauses_until_operator_resume(self):
        self.import_count(1)
        conf = settings(self.db)
        def refuse(*_):
            raise DeliveryFailed("synthetic refusal")
        moments = (NOW, NOW + timedelta(minutes=30), NOW + timedelta(minutes=90), NOW + timedelta(minutes=210))
        results = [send_next_query(conf, now=moment, sender=refuse) for moment in moments]
        self.assertTrue(all(result["status"] == "failed" for result in results))
        row = repo.list_shipments(self.db)[0]
        self.assertEqual(row["paused"], 1)
        self.assertEqual(repo.preview_batches(now=NOW + timedelta(days=2), db_path=self.db), [])
        self.assertEqual(repo.enable_shipment(row["tracking_number"], now=NOW + timedelta(minutes=211),
                                              db_path=self.db), 1)
        self.assertEqual(len(repo.preview_batches(now=NOW + timedelta(minutes=211), db_path=self.db)[0]), 1)

    def test_transport_classifies_explicit_smtp_refusal_vs_uncertain_disconnect(self):
        conf = settings(self.db)
        message = build_query_message(conf.mail_user, ["00012345"])

        class FakeClient:
            def __init__(self, error):
                self.error = error
            def login(self, *_):
                return (235, b"ok")
            def send_message(self, _message):
                raise self.error
            def quit(self):
                return (221, b"bye")

        with patch("feta.transport._smtp_client", return_value=FakeClient(smtplib.SMTPDataError(550, b"rejected"))):
            with self.assertRaises(transport.DeliveryFailed):
                transport.send_query_email(conf, message)
        with patch("feta.transport._smtp_client", return_value=FakeClient(smtplib.SMTPServerDisconnected("lost"))):
            with self.assertRaises(transport.DeliveryUncertain):
                transport.send_query_email(conf, message)

    def test_imap_fetch_failure_keeps_checkpoint_before_gap_and_stops_later_uid(self):
        conf = settings(self.db)
        repo.register_mail_account(conf.mail_user, conf.mail_provider, db_path=self.db)
        repo.save_mailbox_checkpoint(conf.mail_user, "42", 10, db_path=self.db)

        class FakeIMAP:
            def __init__(self):
                self.fetched_uids = []
            def login(self, *_):
                return "OK", [b"authenticated"]
            def select(self, _mailbox, readonly=False):
                return "OK", [b"2"]
            def response(self, _code):
                return "UIDVALIDITY", [b"42"]
            def uid(self, command, *args):
                if command == "SEARCH":
                    return "OK", [b"11 12"]
                if command == "FETCH":
                    self.fetched_uids.append(args[0])
                    return "NO", []
                raise AssertionError(command)
            def logout(self):
                return "BYE", [b"logout"]

        fake = FakeIMAP()
        with patch("feta.transport.imaplib.IMAP4_SSL", return_value=fake):
            with self.assertRaises(transport.DeliveryFailed):
                fetch_inbox(conf, now=NOW, messages=None)
        checkpoint = repo.mailbox_checkpoint(conf.mail_user, conf.mail_provider, db_path=self.db)
        self.assertEqual(checkpoint["last_uid"], 10)
        self.assertEqual(fake.fetched_uids, [b"11"])

    def test_imap_non_fedex_uid_range_is_skipped_using_search_hint_only(self):
        conf = settings(self.db)
        repo.register_mail_account(conf.mail_user, conf.mail_provider, db_path=self.db)
        repo.save_mailbox_checkpoint(conf.mail_user, "42", 100, db_path=self.db)

        class FakeIMAP:
            def login(self, *_):
                return "OK", [b"authenticated"]
            def select(self, _mailbox, readonly=False):
                return "OK", [b"2"]
            def response(self, _code):
                return "UIDVALIDITY", [b"42"]
            def uid(self, command, *args):
                if command == "SEARCH":
                    if "FROM" in args:
                        return "OK", [b""]
                    return "OK", [b"101 102"]
                raise AssertionError("non-FedEx candidate must not be fetched")
            def logout(self):
                return "BYE", [b"logout"]

        with patch("feta.transport.imaplib.IMAP4_SSL", return_value=FakeIMAP()):
            messages = transport.fetch_new_messages(conf, checkpoint={"uidvalidity": "42", "last_uid": 100})
        self.assertEqual(len(messages), 1)
        self.assertTrue(messages[0]["checkpoint_only"])
        self.assertEqual(messages[0]["uid"], "102")
        result = fetch_inbox(conf, now=NOW, messages=messages)
        self.assertEqual(result["processed"], 0)
        checkpoint = repo.mailbox_checkpoint(conf.mail_user, conf.mail_provider, db_path=self.db)
        self.assertEqual(checkpoint["last_uid"], 102)

    def test_imap_adapter_uses_readonly_inbox_and_uidvalidity(self):
        conf = settings(self.db)
        raw = self._email_bytes("000123", "In transit")

        class FakeIMAP:
            def __init__(self):
                self.selected = None
                self.search_calls = []
                self.fetch_requests = []
            def login(self, *_):
                return "OK", [b"authenticated"]
            def select(self, mailbox, readonly=False):
                self.selected = (mailbox, readonly)
                return "OK", [b"1"]
            def response(self, code):
                return "UIDVALIDITY", [b"42"]
            def uid(self, command, *args):
                if command == "SEARCH":
                    self.search_calls.append((command, *args))
                    return "OK", [b"5 6 7"]
                if command == "FETCH":
                    uid_value = args[0]
                    spec = args[-1]
                    self.fetch_requests.append(spec)
                    if spec == "(INTERNALDATE RFC822.SIZE)":
                        size = len(raw) if uid_value in {b"5", b"7"} else 2 * 1024 * 1024
                        metadata = f'5 (INTERNALDATE "05-Jan-2026 15:05:00 +0000" RFC822.SIZE {size})'.encode()
                        return "OK", [metadata]
                    if "HEADER.FIELDS" in spec:
                        if uid_value == b"7":
                            header = b"From: track@fedex.com\r\nMessage-ID: <unverified@example.test>\r\n\r\n"
                        else:
                            header = (b"From: FedEx Tracking <track@fedex.com>\r\n"
                                      b"Authentication-Results: mx.google.com; dkim=pass header.d=fedex.com; "
                                      b"dmarc=pass header.from=fedex.com\r\n"
                                      b"Message-ID: <fedex-reply-1@example.test>\r\n\r\n")
                        return "OK", [(b"5 (BODY[HEADER.FIELDS])", header), b")"]
                    return "OK", [(b"5 (BODY[])", raw), b")"]
                raise AssertionError(command)
            def logout(self):
                return "BYE", [b"logout"]

        fake = FakeIMAP()
        with patch("feta.transport.imaplib.IMAP4_SSL", return_value=fake):
            messages = transport.fetch_new_messages(conf, checkpoint={"uidvalidity": "42", "last_uid": 4})
        self.assertEqual(fake.selected, ("INBOX", True))
        self.assertEqual(fake.search_calls, [
            ("SEARCH", None, "UID", "5:*"),
            ("SEARCH", None, "UID", "5:*", "FROM", "fedex.com"),
        ])
        self.assertEqual(len(fake.fetch_requests), 6)
        self.assertEqual(fake.fetch_requests[0], "(INTERNALDATE RFC822.SIZE)")
        self.assertIn("BODY.PEEK[HEADER.FIELDS", fake.fetch_requests[1])
        self.assertEqual(fake.fetch_requests[2], "(BODY.PEEK[])")
        self.assertEqual(fake.fetch_requests[3], "(INTERNALDATE RFC822.SIZE)")
        self.assertEqual(fake.fetch_requests[4], "(INTERNALDATE RFC822.SIZE)")
        self.assertIn("BODY.PEEK[HEADER.FIELDS", fake.fetch_requests[5])
        self.assertEqual(messages[0]["uidvalidity"], "42")
        self.assertEqual(messages[0]["uid"], "5")
        self.assertEqual(messages[0]["checkpoint_after_batch"], "7")
        self.assertEqual(messages[0]["raw"], raw)
        self.assertEqual(messages[0]["received_at"], NOW + timedelta(minutes=5))
        self.assertEqual(messages[1]["uid"], "6")
        self.assertTrue(messages[1]["oversize"])
        self.assertEqual(messages[1]["reported_size"], 2 * 1024 * 1024)
        self.assertEqual(messages[2]["uid"], "7")
        self.assertTrue(messages[2]["headers_only"])
        self.assertNotIn("(BODY.PEEK[])", fake.fetch_requests[3])

    def test_alert_send_failure_remains_unsent_and_retries_explicitly(self):
        self.import_count(1)
        with repo._db(self.db) as conn:
            conn.execute("UPDATE shipments SET status='EXCEPTION' WHERE tracking_number='000000000001'")
        conf = settings(self.db)
        first = run_alerts(conf, send=True, now=NOW,
                           sender=lambda *_: (_ for _ in ()).throw(RuntimeError("fake")))
        self.assertEqual(first["unknown"], 1)
        no_duplicate = run_alerts(conf, send=True, now=NOW + timedelta(minutes=1), sender=lambda *_: "<alert-id>")
        self.assertEqual(no_duplicate["sent"], 0)
        alert_id = first["new_alerts"][0]["id"]
        repo.set_alert_state(alert_id, "open", now=NOW + timedelta(minutes=2), db_path=self.db)
        retry = run_alerts(conf, send=True, now=NOW + timedelta(minutes=3), sender=lambda *_: "<alert-id>")
        self.assertEqual(retry["sent"], 1)


if __name__ == "__main__":
    unittest.main()
