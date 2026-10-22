"""FETA-to-logistics bridge tests; all storage is temporary/in-memory and mail is mocked."""
import sqlite3
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import local_db
import web_server


class FetaBridgeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        uri = f"file:feta_bridge_{uuid.uuid4().hex}?mode=memory&cache=shared"
        self.keeper = sqlite3.connect(uri, uri=True)

        def connect():
            conn = sqlite3.connect(uri, uri=True)
            conn.executescript(local_db.SCHEMA)
            return conn

        self.db_patch = patch.object(local_db, "_conn", new=connect)
        self.db_patch.start()
        self.addCleanup(self.db_patch.stop)
        self.addCleanup(self.keeper.close)
        self.settings = SimpleNamespace(db_path=Path("unused-feta-test.sqlite3"), imap_configured=True)

    async def test_accept_is_explicit_maps_status_and_keeps_mail_time_out_of_scan_fields(self):
        added = web_server.track_store.add_pairs([{
            "order_id": "FETA-BRIDGE-ORDER", "tracking_number": "FETATEST501",
        }])
        event = {
            "id": 41, "tracking_number": "FETATEST501", "received_at_utc": "2026-11-02T12:00:00Z",
            "raw_status": "In transit", "normalized_status": "IN_TRANSIT", "raw_eta_text": "Nov 4",
            "parse_confidence": "low", "review_state": "pending",
        }
        with patch.object(web_server.feta_config, "load_settings", return_value=self.settings), \
             patch.object(web_server.feta_repo, "review_events", return_value=[event]), \
             patch.object(web_server.feta_repo, "validate_review_event") as validate, \
             patch.object(web_server.feta_service, "review_event", return_value={
                 "event_id": 41, "tracking_number": "FETATEST501", "review_state": "accepted"}) as review:
            pending = await web_server.tracking_fedex_email_reviews()
            self.assertEqual((pending["total"], pending["items"][0]["system_status"]), (1, "in_transit"))
            accepted = await web_server.tracking_fedex_email_review(41, {"decision": "accept"})

        validate.assert_called_once_with(41, "accept", db_path=self.settings.db_path)
        review.assert_called_once()
        self.assertEqual(accepted["integration"]["status_updated"], 1)
        order_id = added["items"][0]["id"]
        row = next(item for item in web_server.track_store.list_orders() if item["id"] == order_id)
        self.assertEqual(row["status"], "in_transit")
        self.assertIsNone(row["last_event_at"])
        audit = web_server.track_store.events(order_id)[0]
        self.assertEqual((audit["source"], audit["raw_eta_text"]), ("feta", "Nov 4"))
        self.assertEqual(audit["source_received_at"], "2026-11-02T12:00:00+00:00")
        self.assertIsNone(audit["event_at"])
        self.assertIsNone(audit["location"])

    async def test_ignore_and_unmatched_accept_do_not_write_main_status(self):
        web_server.track_store.add_pairs([{
            "order_id": "FETA-IGNORE-ORDER", "tracking_number": "FETATEST502",
        }])
        event = {
            "id": 42, "tracking_number": "FETAMISSING503", "received_at_utc": "2026-11-02T12:00:00Z",
            "raw_status": "Delivered", "normalized_status": "DELIVERED", "raw_eta_text": None,
        }
        from fastapi import HTTPException
        with patch.object(web_server.feta_config, "load_settings", return_value=self.settings), \
             patch.object(web_server.feta_repo, "review_events", return_value=[event]), \
             patch.object(web_server.feta_repo, "validate_review_event") as validate, \
             patch.object(web_server.feta_service, "review_event") as review:
            with self.assertRaises(HTTPException) as error:
                await web_server.tracking_fedex_email_review(42, {"decision": "accept"})
            self.assertEqual(error.exception.status_code, 409)
            validate.assert_called_once()
            review.assert_not_called()

        event["id"] = 43
        event["tracking_number"] = "FETATEST502"
        with patch.object(web_server.feta_config, "load_settings", return_value=self.settings), \
             patch.object(web_server.feta_repo, "review_events", return_value=[event]), \
             patch.object(web_server.track_store, "apply_feta_event") as apply, \
             patch.object(web_server.feta_service, "review_event", return_value={"review_state": "ignored"}):
            result = await web_server.tracking_fedex_email_review(43, {"decision": "ignore"})
        self.assertIsNone(result["integration"])
        apply.assert_not_called()
        self.assertIsNone(web_server.track_store.list_orders()[0]["status"])

    async def test_web_inbox_sync_is_explicit_and_mail_transport_is_mocked(self):
        result_value = {"processed": 1, "events_saved": 2}
        with patch.object(web_server.feta_config, "load_settings", return_value=self.settings), \
             patch.object(web_server.feta_service, "fetch_inbox", return_value=result_value) as fetch:
            result = await web_server.tracking_fedex_email_sync()
        self.assertEqual(result, result_value)
        fetch.assert_called_once_with(self.settings)


if __name__ == "__main__":
    unittest.main()
