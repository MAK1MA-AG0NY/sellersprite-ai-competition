"""17TRACK manual-import API tests; shared in-memory SQLite only, no browser/network."""
import sqlite3
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import local_db
import web_server


class TrackingEndpointTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        uri = f"file:tracking_api_{uuid.uuid4().hex}?mode=memory&cache=shared"
        self.keeper = sqlite3.connect(uri, uri=True)

        def connect():
            c = sqlite3.connect(uri, uri=True)
            c.executescript(local_db.SCHEMA)
            return c

        self.db_patch = patch.object(local_db, "_conn", new=connect)
        self.db_patch.start()
        self.addCleanup(self.db_patch.stop)
        self.addCleanup(self.keeper.close)

    async def test_manual_pairs_batch_summary_import_and_close(self):
        added = await web_server.tracking_add_orders({"pairs": [
            {"order_id": "ORDER-TEST-1", "tracking_number": "123456789012"},
            {"order_id": "ORDER-TEST-2", "tracking_number": "123456789013"},
        ]})
        self.assertEqual(added["added"], 2)
        batch = await web_server.tracking_next_batch(limit=40)
        self.assertEqual(len(batch["items"]), 2)
        self.assertEqual(batch["total"], 2)
        self.assertEqual(set(batch["items"][0]), {"tracking_number", "order_ids"})

        summary = (
            "123456789012\t美国\t美国\t2020-08-17 12:17 HOMESTEAD, FL, US Delivered\t成功签收\t(5 天)\n"
            "======================================\n"
            "Powered by www.17track.net\n"
        )
        imported = await web_server.tracking_import_summary({"text": summary})
        self.assertEqual(imported["matched"], 1)
        self.assertEqual((await web_server.tracking_next_batch())["total"], 1)
        items = (await web_server.tracking_orders())["items"]
        first = next(item for item in items if item["order_id"] == "ORDER-TEST-1")
        second = next(item for item in items if item["order_id"] == "ORDER-TEST-2")
        self.assertEqual(first["status"], "delivered")
        self.assertFalse(second["order_closed"])
        self.assertEqual((await web_server.tracking_order_events(first["id"]))["items"],
                         web_server.track_store.events(first["id"]))
        self.assertEqual((await web_server.tracking_orders())["items"], web_server.track_store.list_orders())

    async def test_order_notes_archive_restore_and_archive_view_api(self):
        added = await web_server.tracking_add_orders({"pairs": [
            {"order_id": "ORDER-ARCHIVE", "tracking_number": "SYNTHETIC801"},
            {"order_id": "ORDER-ARCHIVE", "tracking_number": "SYNTHETIC802"},
            {"order_id": "ORDER-ACTIVE", "tracking_number": "SYNTHETIC803"},
        ]})
        first_id, second_id, _active_id = [item["id"] for item in added["items"]]
        note = "异常原因：已和承运商确认处理完成。"
        saved = await web_server.tracking_set_order_note(first_id, {"note": note})
        self.assertEqual(saved["item"]["note"], note)
        await web_server.tracking_archive_order(second_id, {"note": "协调后确认妥投。"})

        active = await web_server.tracking_orders()
        self.assertEqual({item["order_id"] for item in active["items"]}, {"ORDER-ACTIVE"})
        archived = await web_server.tracking_orders(archived=True)
        self.assertEqual(len(archived["items"]), 2)
        self.assertTrue(all(item["order_note"] == note and item["is_archived"] for item in archived["items"]))
        event_items = (await web_server.tracking_order_events(first_id))["items"]
        self.assertTrue(any(item.get("event_type") == "archived" and item["event_text"] == "协调后确认妥投。"
                            for item in event_items))

        await web_server.tracking_restore_order(first_id)
        restored = await web_server.tracking_orders()
        self.assertEqual({item["order_id"] for item in restored["items"]}, {"ORDER-ARCHIVE", "ORDER-ACTIVE"})
        self.assertFalse((await web_server.tracking_orders(archived=True))["items"])

    async def test_invalid_json_inputs_are_rejected_and_manual_flow_never_opens_seller_browser(self):
        from fastapi import HTTPException

        for payload in ({}, {"pairs": "ORDER\t123456789012"},
                        {"pairs": [{"order_id": "ORDER", "tracking_number": "bad"}]}):
            with self.subTest(payload=payload), self.assertRaises(HTTPException) as error:
                await web_server.tracking_add_orders(payload)
            self.assertEqual(error.exception.status_code, 400)

        with self.assertRaises(HTTPException) as error:
            await web_server.tracking_import_summary({"text": ""})
        self.assertEqual(error.exception.status_code, 400)

        with patch.object(web_server, "get_pw", new_callable=AsyncMock) as seller_browser:
            await web_server.tracking_orders()
            seller_browser.assert_not_awaited()

    async def test_next_batch_limit_is_capped_at_site_maximum(self):
        from fastapi import HTTPException
        with self.assertRaises(HTTPException) as error:
            await web_server.tracking_next_batch(limit=41)
        self.assertEqual(error.exception.status_code, 400)

    async def test_erp_preview_is_read_only_then_confirm_imports_only_valid_and_deduplicates(self):
        from fastapi import HTTPException
        text = ("平台参考号\t跟踪号\t状态\n"
                "ORDER-1\tSYNTHETIC001\tok\n"
                "ORDER-1\tsynthetic001\tduplicate\n"
                "ORDER-2\tTRACKING002\tok\n"
                "BAD-ROW\tonly-one-column")
        preview = await web_server.tracking_preview({"text": text})
        self.assertEqual(preview["valid_rows"], 3)
        self.assertEqual(preview["input_duplicates"], 1)
        self.assertEqual(preview["unique_associations"], 2)
        self.assertEqual(len(preview["invalid_rows"]), 1)
        self.assertEqual(tracking_store_count(), 0)
        with self.assertRaises(HTTPException):
            await web_server.tracking_import_pairs({"text": text, "only_valid_rows": False})
        imported = await web_server.tracking_import_pairs({"text": text, "only_valid_rows": True})
        self.assertEqual((imported["added"], imported["duplicates"], imported["ignored_invalid_rows"]), (2, 1, 1))
        self.assertEqual(len((await web_server.tracking_orders())["items"]), 2)

    async def test_xlsx_preview_maps_header_preserves_long_values_and_reports_numeric(self):
        import io
        from openpyxl import Workbook
        from starlette.requests import Request
        workbook = Workbook(); sheet = workbook.active
        sheet.append(["平台参考号", "跟踪号", "备注"])
        sheet.append(["ORDER-A", "4201234567890123456789012345678901", "keep full"])
        sheet.append(["ORDER-B", 4201234567890123456, "unsafe numeric"])
        output = io.BytesIO(); workbook.save(output); workbook.close(); data = output.getvalue()
        async def receive(): return {"type": "http.request", "body": data, "more_body": False}
        request = Request({"type": "http", "method": "POST", "headers": [], "query_string": b""}, receive)
        preview = await web_server.tracking_preview_xlsx(request)
        self.assertEqual(preview["sheet_name"], "Sheet")
        self.assertEqual(preview["pairs"][0]["tracking_number"], "4201234567890123456789012345678901")
        self.assertTrue(any("必须保存为文本" in error["error"] for error in preview["invalid_rows"]))
        self.assertEqual(tracking_store_count(), 0)

    async def test_carrier_override_and_detailed_batch(self):
        await web_server.tracking_add_orders({"pairs": [
            {"order_id": "ORDER-F", "tracking_number": "123456789012"},
            {"order_id": "ORDER-S", "tracking_number": "94001118992238569284"},
        ]})
        first = (await web_server.tracking_orders())['items'][0]
        await web_server.tracking_set_carrier(first['id'], {"carrier": "usps"})
        refreshed = (await web_server.tracking_orders())['items'][0]
        self.assertEqual(refreshed["carrier"], "usps")
        self.assertEqual(refreshed["carrier_source"], "manual")
        batch = await web_server.tracking_next_batch(detailed=True)
        self.assertEqual(batch["items"], [])
        candidates = await web_server.tracking_candidates()
        self.assertEqual(candidates["counts"]["usps"], 2)
        self.assertEqual(candidates["counts"]["fedex"], 0)

    async def test_xlsx_stream_stops_reading_soon_after_limit(self):
        from fastapi import HTTPException
        from starlette.requests import Request
        total, chunk_size, consumed = 6 * 1024 * 1024, 64 * 1024, 0
        async def receive():
            nonlocal consumed
            remaining = total - consumed
            if remaining <= 0: return {"type":"http.disconnect"}
            amount = min(chunk_size, remaining); consumed += amount
            return {"type":"http.request","body":b"x" * amount,"more_body":consumed < total}
        request = Request({"type":"http","method":"POST","path":"/upload","headers":[],"query_string":b""}, receive)
        with self.assertRaises(HTTPException) as error:
            await web_server.tracking_preview_xlsx(request)
        self.assertEqual(error.exception.status_code, 413)
        self.assertLess(consumed, total)

    async def test_xlsx_and_text_capacity_errors_are_http_conflicts_and_atomic(self):
        import io
        from fastapi import HTTPException
        from openpyxl import Workbook
        from starlette.requests import Request
        web_server.track_store.add_pairs([{"order_id":"EXISTING","tracking_number":"TRACK00001"}])
        workbook = Workbook(); sheet = workbook.active
        sheet.append(["order_id","tracking_number"]); sheet.append(["NEW","TRACK00002"])
        output = io.BytesIO(); workbook.save(output); workbook.close(); data = output.getvalue()
        async def receive(): return {"type":"http.request","body":data,"more_body":False}
        request = Request({"type":"http","method":"POST","path":"/upload","headers":[],"query_string":b""}, receive)
        with patch.object(web_server.track_store, "MAX_ORDERS", 1):
            for operation in (lambda: web_server.tracking_import_pairs({"text":"NEW\tTRACK00002","only_valid_rows":True}),
                              lambda: web_server.tracking_import_xlsx(request, only_valid_rows=True)):
                with self.assertRaises(HTTPException) as error:
                    await operation()
                self.assertEqual(error.exception.status_code, 409)
                self.assertEqual(len(web_server.track_store.list_orders()), 1)

    async def test_candidates_api_groups_carriers_and_offset_batch_does_not_repeat(self):
        numbers = [f"{234567890000 + index:012d}" for index in range(42)]
        await web_server.tracking_add_orders({"pairs": [
            *[{"order_id":f"CAND-F-{i}","tracking_number":number} for i, number in enumerate(numbers)],
            {"order_id":"CAND-U1","tracking_number":"94001118992238569284"},
            {"order_id":"CAND-U2","tracking_number":"420123456789012345678901234567"},
            {"order_id":"CAND-A","tracking_number":"SYNTHETIC902"},
        ]})
        result = await web_server.tracking_candidates()
        self.assertEqual(result["counts"], {"fedex":42,"usps":2,"auto":1})
        first = await web_server.tracking_next_batch(limit=40)
        second = await web_server.tracking_next_batch(limit=40, offset=40)
        self.assertEqual((len(first["items"]), len(second["items"])), (40, 2))
        self.assertEqual(set(row["tracking_number"] for row in first["items"] + second["items"]), set(numbers))
        self.assertTrue({row["carrier"] for row in result["items"] if row["tracking_number"] in numbers} == {"fedex"})

    async def test_tracking_scope_switches_between_pending_and_all_enabled_open_orders(self):
        added = await web_server.tracking_add_orders({"pairs": [
            {"order_id": "SCOPE-PENDING", "tracking_number": "SCOPEPENDING1"},
            {"order_id": "SCOPE-TRANSIT", "tracking_number": "SCOPETRANSIT1"},
            {"order_id": "SCOPE-DONE", "tracking_number": "SCOPEDELIVER1"},
            {"order_id": "SCOPE-PAUSED", "tracking_number": "SCOPEPAUSED1"},
        ]})
        for number, status in (("SCOPETRANSIT1", "in_transit"), ("SCOPEDELIVER1", "delivered"),
                               ("SCOPEPAUSED1", "in_transit")):
            web_server.track_store.import_records([{"tracking_number": number, "status": status,
                "event_at": "2026-11-03T12:00:00+00:00", "event_text": "Synthetic status"}])
        paused_id = next(item["id"] for item in added["items"] if item["tracking_number"] == "SCOPEPAUSED1")
        web_server.track_store.set_enabled(paused_id, False)

        pending = await web_server.tracking_orders(status="pending")
        needs_update = await web_server.tracking_orders(status="needs_update")
        self.assertEqual({item["tracking_number"] for item in pending["items"]}, {"SCOPEPENDING1"})
        self.assertEqual(pending["total"], 1)
        self.assertEqual({item["tracking_number"] for item in needs_update["items"]}, {
            "SCOPEPENDING1", "SCOPETRANSIT1",
        })
        self.assertEqual(needs_update["total"], 2)

    async def test_feta_accept_all_requires_confirmation_and_reports_partial_matches(self):
        await web_server.tracking_add_orders({"pairs": [
            {"order_id": "FETA-BULK-A", "tracking_number": "FETATEST811"},
            {"order_id": "FETA-BULK-B", "tracking_number": "FETATEST812"},
        ]})
        events = [
            {"id": 811, "tracking_number": "FETATEST811", "received_at_utc": "2026-11-03T12:00:00Z",
             "raw_status": "In transit", "normalized_status": "IN_TRANSIT", "raw_eta_text": None},
            {"id": 812, "tracking_number": "FETATEST812", "received_at_utc": "2026-11-03T12:01:00Z",
             "raw_status": "No result", "normalized_status": "UNKNOWN", "raw_eta_text": None},
            {"id": 813, "tracking_number": "FETAMISSING813", "received_at_utc": "2026-11-03T12:02:00Z",
             "raw_status": "Delivered", "normalized_status": "DELIVERED", "raw_eta_text": None},
        ]
        settings = SimpleNamespace(db_path=Path("not-used-feta-bulk.sqlite3"), imap_configured=True)
        from fastapi import HTTPException
        with self.assertRaises(HTTPException) as error:
            await web_server.tracking_fedex_email_accept_all({"confirm": False})
        self.assertEqual(error.exception.status_code, 400)

        with patch.object(web_server.feta_config, "load_settings", return_value=settings), \
             patch.object(web_server.feta_repo, "review_events", side_effect=[events, [events[2]]]), \
             patch.object(web_server.feta_repo, "validate_review_event") as validate, \
             patch.object(web_server.feta_service, "review_event", return_value={"review_state": "accepted"}) as review:
            result = await web_server.tracking_fedex_email_accept_all({"confirm": True})
        self.assertEqual((result["total"], result["accepted"], result["unmatched"], result["pending_after"]),
                         (3, 2, 1, 1))
        self.assertEqual((result["status_updated"], result["audit_only"], result["failed"]), (1, 1, 0))
        self.assertEqual(validate.call_count, 3)
        self.assertEqual(review.call_count, 2)
        statuses = {row["tracking_number"]: row["status"] for row in web_server.track_store.list_orders()}
        self.assertEqual(statuses, {"FETATEST811": "in_transit", "FETATEST812": None})

    async def test_feta_accept_all_requires_confirmation_and_reports_partial_matches(self):
        await web_server.tracking_add_orders({"pairs": [
            {"order_id": "FETA-BULK-A", "tracking_number": "FETATEST811"},
            {"order_id": "FETA-BULK-B", "tracking_number": "FETATEST812"},
        ]})
        events = [
            {"id": 811, "tracking_number": "FETATEST811", "received_at_utc": "2026-11-03T12:00:00Z",
             "raw_status": "In transit", "normalized_status": "IN_TRANSIT", "raw_eta_text": None},
            {"id": 812, "tracking_number": "FETATEST812", "received_at_utc": "2026-11-03T12:01:00Z",
             "raw_status": "No result", "normalized_status": "UNKNOWN", "raw_eta_text": None},
            {"id": 813, "tracking_number": "FETAMISSING813", "received_at_utc": "2026-11-03T12:02:00Z",
             "raw_status": "Delivered", "normalized_status": "DELIVERED", "raw_eta_text": None},
        ]
        settings = SimpleNamespace(db_path=Path("not-used-feta-bulk.sqlite3"), imap_configured=True)
        from fastapi import HTTPException
        with self.assertRaises(HTTPException) as error:
            await web_server.tracking_fedex_email_accept_all({"confirm": False})
        self.assertEqual(error.exception.status_code, 400)

        with patch.object(web_server.feta_config, "load_settings", return_value=settings), \
             patch.object(web_server.feta_repo, "review_events", side_effect=[events, [events[2]]]), \
             patch.object(web_server.feta_repo, "validate_review_event") as validate, \
             patch.object(web_server.feta_service, "review_event", return_value={"review_state": "accepted"}) as review:
            result = await web_server.tracking_fedex_email_accept_all({"confirm": True})
        self.assertEqual((result["total"], result["accepted"], result["unmatched"], result["pending_after"]),
                         (3, 2, 1, 1))
        self.assertEqual((result["status_updated"], result["audit_only"], result["failed"]), (1, 1, 0))
        self.assertEqual(validate.call_count, 3)
        self.assertEqual(review.call_count, 2)
        statuses = {row["tracking_number"]: row["status"] for row in web_server.track_store.list_orders()}
        self.assertEqual(statuses, {"FETATEST811": "in_transit", "FETATEST812": None})

    async def test_feta_review_route_requires_acceptance_and_writes_audited_status(self):
        added = await web_server.tracking_add_orders({"pairs": [
            {"order_id": "FETA-API", "tracking_number": "FETATEST101"},
        ]})
        event = {"id": 701, "tracking_number": "FETATEST101", "received_at_utc": "2026-11-02T12:00:00Z",
                 "raw_status": "In transit", "normalized_status": "IN_TRANSIT", "raw_eta_text": "Nov 4",
                 "parse_confidence": "low", "review_state": "pending"}
        settings = SimpleNamespace(db_path=Path("not-used.sqlite3"), imap_configured=True)
        with patch.object(web_server.feta_config, "load_settings", return_value=settings), \
             patch.object(web_server.feta_repo, "review_events", return_value=[event]), \
             patch.object(web_server.feta_repo, "validate_review_event") as validate, \
             patch.object(web_server.feta_service, "review_event", return_value={
                 "event_id": 701, "tracking_number": "FETATEST101", "review_state": "accepted"}) as review:
            pending = await web_server.tracking_fedex_email_reviews()
            self.assertEqual((pending["total"], pending["items"][0]["system_status"]), (1, "in_transit"))
            result = await web_server.tracking_fedex_email_review(701, {"decision": "accept"})

        validate.assert_called_once_with(701, "accept", db_path=settings.db_path)
        review.assert_called_once()
        self.assertEqual(result["integration"]["status_updated"], 1)
        row = next(item for item in web_server.track_store.list_orders() if item["id"] == added["items"][0]["id"])
        self.assertEqual(row["status"], "in_transit")
        self.assertIsNone(row["last_event_at"])
        audit = web_server.track_store.events(row["id"])[0]
        self.assertEqual((audit["source"], audit["raw_eta_text"]), ("feta", "Nov 4"))

    async def test_feta_unmatched_acceptance_stays_pending_and_ignore_never_writes_main(self):
        event = {"id": 702, "tracking_number": "FETAMISSING102", "received_at_utc": "2026-11-02T12:00:00Z",
                 "raw_status": "Delivered", "normalized_status": "DELIVERED", "raw_eta_text": None}
        settings = SimpleNamespace(db_path=Path("not-used.sqlite3"), imap_configured=True)
        from fastapi import HTTPException
        with patch.object(web_server.feta_config, "load_settings", return_value=settings), \
             patch.object(web_server.feta_repo, "review_events", return_value=[event]), \
             patch.object(web_server.feta_repo, "validate_review_event") as validate, \
             patch.object(web_server.feta_service, "review_event") as review:
            with self.assertRaises(HTTPException) as error:
                await web_server.tracking_fedex_email_review(702, {"decision": "accept"})
            self.assertEqual(error.exception.status_code, 409)
            validate.assert_called_once()
            review.assert_not_called()

        await web_server.tracking_add_orders({"pairs": [
            {"order_id": "FETA-IGNORE", "tracking_number": "FETATEST103"},
        ]})
        ignored = {**event, "id": 703, "tracking_number": "FETATEST103"}
        with patch.object(web_server.feta_config, "load_settings", return_value=settings), \
             patch.object(web_server.feta_repo, "review_events", return_value=[ignored]), \
             patch.object(web_server.track_store, "apply_feta_event") as apply, \
             patch.object(web_server.feta_service, "review_event", return_value={"review_state": "ignored"}):
            result = await web_server.tracking_fedex_email_review(703, {"decision": "ignore"})
        self.assertIsNone(result["integration"])
        apply.assert_not_called()
        self.assertIsNone(web_server.track_store.list_orders()[0]["status"])

    async def test_feta_sync_route_calls_readonly_inbox_fetch_only(self):
        settings = SimpleNamespace(db_path=Path("not-used.sqlite3"), imap_configured=True)
        result_value = {"processed": 1, "events_saved": 2}
        with patch.object(web_server.feta_config, "load_settings", return_value=settings), \
             patch.object(web_server.feta_service, "fetch_inbox", return_value=result_value) as fetch:
            result = await web_server.tracking_fedex_email_sync()
        self.assertEqual(result, result_value)
        fetch.assert_called_once_with(settings)

    async def test_query_automation_routes_are_removed(self):
        paths = {route.path for route in web_server.app.routes if hasattr(route, "path")}
        self.assertFalse(any(path.startswith("/api/tracking/query/") for path in paths))

    async def test_delivered_cleanup_is_one_click_atomic_and_reports_counts(self):
        await web_server.tracking_add_orders({"pairs": [
            {"order_id":"CLEAN-API","tracking_number":"SYNTHETIC811"},
            {"order_id":"CLEAN-API","tracking_number":"SYNTHETIC812"},
            {"order_id":"MIXED-API","tracking_number":"SYNTHETIC813"},
            {"order_id":"MIXED-API","tracking_number":"SYNTHETIC814"},
            {"order_id":"SHARED-DONE-API","tracking_number":"SYNTHETIC815"},
        ]})
        for number, status in (("SYNTHETIC811","delivered"),("SYNTHETIC812","delivered"),
                               ("SYNTHETIC813","delivered"),("SYNTHETIC814","in_transit"),
                               ("SYNTHETIC815","delivered")):
            web_server.track_store.import_records([{"tracking_number":number,"status":status,
                "event_at":"2026-06-01T12:00:00+00:00","event_text":"Synthetic event"}])
        shared_open = await web_server.tracking_add_orders({"pairs": [
            {"order_id":"SHARED-OPEN-API","tracking_number":"SYNTHETIC815"},
        ]})
        shared_open_id = shared_open["items"][0]["id"]
        self.assertEqual(await web_server.tracking_delivered_cleanup_preview(), {"orders":3,"package_rows":4})
        result = await web_server.tracking_clear_delivered()
        self.assertEqual(result, {"deleted_orders":3,"deleted_order_rows":4,"deleted_event_rows":3})
        remaining = (await web_server.tracking_orders())["items"]
        self.assertEqual({row["order_id"] for row in remaining}, {"MIXED-API","SHARED-OPEN-API"})
        mixed = [row for row in remaining if row["order_id"] == "MIXED-API"]
        self.assertEqual(len(mixed), 1)
        self.assertEqual(mixed[0]["status"], "in_transit")
        self.assertEqual(len(web_server.track_store.events(shared_open_id)), 1)
        self.assertEqual(await web_server.tracking_delivered_cleanup_preview(), {"orders":0,"package_rows":0})
        self.assertEqual(await web_server.tracking_clear_delivered(),
                         {"deleted_orders":0,"deleted_order_rows":0,"deleted_event_rows":0})

    async def test_delivered_cleanup_api_rolls_back_on_delete_failure(self):
        await web_server.tracking_add_orders({"pairs": [
            {"order_id":"ATOMIC-API","tracking_number":"SYNTHETIC821"},
            {"order_id":"ATOMIC-API","tracking_number":"SYNTHETIC822"},
        ]})
        for number in ("SYNTHETIC821","SYNTHETIC822"):
            web_server.track_store.import_records([{"tracking_number":number,"status":"delivered",
                "event_at":"2026-06-01T12:00:00+00:00","event_text":"Synthetic delivered"}])
        conn = web_server.track_store._conn()
        conn.execute("CREATE TRIGGER reject_api_cleanup BEFORE DELETE ON lt_orders "
                     "BEGIN SELECT RAISE(ABORT, 'synthetic rollback'); END")
        conn.commit(); conn.close()
        with self.assertRaises(sqlite3.IntegrityError):
            await web_server.tracking_clear_delivered()
        self.assertEqual(len((await web_server.tracking_orders())["items"]), 2)
        self.assertEqual(await web_server.tracking_delivered_cleanup_preview(), {"orders":1,"package_rows":2})


def tracking_store_count():
    return len(web_server.track_store.list_orders())


if __name__ == "__main__":
    unittest.main()
