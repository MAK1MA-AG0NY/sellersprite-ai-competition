"""Tracking store tests isolated in a shared in-memory SQLite database."""
import datetime as dt
import sqlite3
import unittest
import uuid
from unittest.mock import patch

import local_db
import tracking_store


UTC = dt.timezone.utc


def isolated_memory_db():
    uri = f"file:tracking_test_{uuid.uuid4().hex}?mode=memory&cache=shared"
    keeper = sqlite3.connect(uri, uri=True)

    def connect():
        conn = sqlite3.connect(uri, uri=True)
        conn.executescript(local_db.SCHEMA)
        return conn

    return keeper, patch.object(local_db, "_conn", new=connect)


class TrackingStoreTests(unittest.TestCase):
    def setUp(self):
        self.keeper, self.db_patch = isolated_memory_db()
        self.db_patch.start()

    def tearDown(self):
        self.db_patch.stop()
        self.keeper.close()

    def add(self, *pairs, now=None):
        return tracking_store.add_pairs(list(pairs), now=now)

    def row(self, row_id, now=None):
        return next(item for item in tracking_store.list_orders(now=now) if item["id"] == row_id)

    def test_add_pairs_validates_deduplicates_and_respects_cap(self):
        result = self.add(
            {"order_id": "ORDER-A", "tracking_number": "SYNTHETIC001"},
            {"order_id": "ORDER-A", "tracking_number": "synthetic001"},
            {"order_id": "ORDER-B", "tracking_number": "SYNTHETIC001"},
        )
        self.assertEqual((result["added"], result["duplicates"]), (2, 1))
        self.assertEqual(len(result["items"]), 3)
        self.assertEqual(result["items"][1]["added"], False)
        with self.assertRaises(ValueError):
            tracking_store.add_pairs([{"order_id": "", "tracking_number": "SYNTHETIC002"}])
        self.assertEqual(len(tracking_store.list_orders()), 2)
        with self.assertRaises(ValueError):
            tracking_store.add_pairs([
                {"order_id": f"ORDER-{i}", "tracking_number": f"TRACKING{i:04d}"}
                for i in range(tracking_store.MAX_ORDERS + 1)
            ])

    def test_order_note_archive_and_restore_share_metadata_across_packages(self):
        added = self.add(
            {"order_id": "ORDER-ARCHIVE", "tracking_number": "SYNTHETIC801"},
            {"order_id": "ORDER-ARCHIVE", "tracking_number": "SYNTHETIC802"},
            {"order_id": "ORDER-ACTIVE", "tracking_number": "SYNTHETIC803"},
        )
        first_id, second_id, _active_id = [item["id"] for item in added["items"]]
        note = "异常原因：联系承运商协调后确认已完成投递。"
        saved = tracking_store.set_order_note(first_id, note)
        self.assertEqual(saved["note"], note)
        self.assertEqual({row["order_note"] for row in tracking_store.list_orders()
                          if row["order_id"] == "ORDER-ARCHIVE"}, {note})

        archived_at = dt.datetime(2026, 10, 21, 10, tzinfo=UTC)
        tracking_store.archive_order(second_id, "已与客户确认妥投。", now=archived_at)
        self.assertEqual({row["order_id"] for row in tracking_store.list_orders()}, {"ORDER-ACTIVE"})
        archived_rows = tracking_store.list_orders(archived=True)
        self.assertEqual(len(archived_rows), 2)
        self.assertTrue(all(row["is_archived"] and row["order_note"] == note for row in archived_rows))
        self.assertNotIn("SYNTHETIC801", {row["tracking_number"] for row in tracking_store.pending_candidates()})
        self.assertNotIn("SYNTHETIC802", {row["tracking_number"] for row in tracking_store.pending_candidates()})
        history = tracking_store.events(first_id)
        self.assertEqual(next(event for event in history if event.get("event_type") == "archived")["event_text"],
                         "已与客户确认妥投。")

        tracking_store.restore_order(first_id, now=archived_at + dt.timedelta(hours=1))
        restored_rows = [row for row in tracking_store.list_orders() if row["order_id"] == "ORDER-ARCHIVE"]
        self.assertEqual(len(restored_rows), 2)
        self.assertTrue(all(row["order_note"] == note for row in restored_rows))
        self.assertFalse(tracking_store.list_orders(archived=True))
        self.assertEqual([event["event_type"] for event in tracking_store.events(first_id)
                          if event.get("source") == "order_activity"], ["restored", "archived"])

    def test_next_batch_groups_fedex_tracks_sorts_oldest_and_caps_at_40(self):
        tracks = [f"{123456780000 + i:012d}" for i in range(42)]
        pairs = [{"order_id": f"ORDER-{i}", "tracking_number": tracking_number}
                 for i, tracking_number in enumerate(tracks)]
        tracking_store.add_pairs(pairs, now=dt.datetime(2026, 1, 1, tzinfo=UTC))
        # Two order IDs share one FedEx track and it should only be copied once.
        tracking_store.add_pairs([
            {"order_id": "ORDER-OTHER-A", "tracking_number": tracks[0]},
            {"order_id": "ORDER-OTHER-B", "tracking_number": tracks[0]},
        ])
        batch = tracking_store.next_batch(limit=100)
        self.assertEqual(len(batch), 40)
        self.assertEqual(batch[0]["tracking_number"], tracks[0])
        self.assertIn("ORDER-OTHER-A", batch[0]["order_ids"])
        self.assertIn("ORDER-OTHER-B", batch[0]["order_ids"])
        self.assertEqual(len({item["tracking_number"].casefold() for item in batch}), 40)
        self.assertEqual(len(tracking_store.next_batch(limit=0)), 0)
        self.assertEqual(tracking_store.pending_count(carrier="fedex"), 42)
        tracking_store.import_records([{
            "tracking_number": tracks[0], "status": "delivered",
            "event_at": "2026-01-02T12:00:00+00:00", "pkg_status_text": "Delivered",
        }])
        self.assertEqual(tracking_store.pending_count(carrier="fedex"), 41)

    def test_next_batch_finishes_unchecked_remainder_before_refreshing_old_scans(self):
        created_at = dt.datetime(2026, 5, 1, tzinfo=UTC)
        tracking_store.add_pairs([
            {"order_id": f"BATCH-ORDER-{i}", "tracking_number": f"{234567890000 + i:012d}"}
            for i in range(43)
        ], now=created_at)
        first = tracking_store.next_batch(limit=40)
        self.assertEqual(len(first), 40)
        checked_at = created_at + dt.timedelta(minutes=5)
        tracking_store.import_records([
            {"tracking_number": item["tracking_number"], "status": "in_transit",
             "pkg_status_text": "运输途中", "event_at": None}
            for item in first
        ], now=checked_at)
        remainder = tracking_store.next_batch(limit=40)
        self.assertEqual(len(remainder), 3)
        self.assertTrue(set(item["tracking_number"] for item in remainder).isdisjoint(
            item["tracking_number"] for item in first))
        tracking_store.import_records([
            {"tracking_number": item["tracking_number"], "status": "in_transit",
             "pkg_status_text": "运输途中", "event_at": None}
            for item in remainder
        ], now=checked_at + dt.timedelta(minutes=5))
        refresh = tracking_store.next_batch(limit=40)
        self.assertEqual(len(refresh), 40)
        self.assertTrue(all(item["tracking_number"] in {row["tracking_number"] for row in first}
                            for item in refresh))

    def test_import_matches_all_order_rows_dedupes_events_and_closes_orders_only_when_all_delivered(self):
        added = self.add(
            {"order_id": "ORDER-A", "tracking_number": "SYNTHETIC101"},
            {"order_id": "ORDER-B", "tracking_number": "SYNTHETIC101"},
            {"order_id": "ORDER-A", "tracking_number": "SYNTHETIC102"},
        )
        checked_at = dt.datetime(2026, 10, 3, 12, tzinfo=UTC)
        record = {
            "tracking_number": "SYNTHETIC101", "status": "delivered",
            "pkg_status_text": "成功签收", "event_at": "2026-10-02T09:08:00-04:00",
            "event_at_raw": "2026-10-02 09:08", "event_text": "Delivered",
            "raw_event": "synthetic event", "location": "Homestead, FL",
            "source_country": "US", "destination_country": "US", "transit_days": 5,
        }
        imported = tracking_store.import_records([record], now=checked_at)
        self.assertEqual(imported["matched"], 1)
        self.assertEqual(imported["unmatched"], 0)
        self.assertEqual(imported["items"][0]["status"], "delivered")
        self.assertEqual(imported["items"][0]["order_ids"], ["ORDER-A", "ORDER-B"])
        rows = tracking_store.list_orders(now=checked_at)
        first_track_rows = [row for row in rows if row["tracking_number"] == "SYNTHETIC101"]
        self.assertTrue(all(row["status"] == "delivered" for row in first_track_rows))
        self.assertTrue(all(row["order_closed"] for row in first_track_rows if row["order_id"] == "ORDER-B"))
        self.assertFalse(next(row for row in rows if row["tracking_number"] == "SYNTHETIC102")["order_closed"])

        tracking_store.import_records([record], now=checked_at + dt.timedelta(hours=1))
        row_id = added["items"][0]["id"]
        self.assertEqual(len(tracking_store.events(row_id)), 1)
        self.assertEqual(self.row(row_id, now=checked_at)["risk"], "delivered")
        self.assertEqual(tracking_store.pending_candidates(), [
            {"tracking_number": "SYNTHETIC102", "order_ids": ["ORDER-A"]}
        ])

    def test_track718_import_persists_full_scan_history_but_keeps_summary_status(self):
        added = self.add({"order_id": "ORDER-718", "tracking_number": "SYNTHETIC718001"})
        row_id = added["items"][0]["id"]
        summary = {
            "tracking_number": "SYNTHETIC718001", "status": "awaiting_pickup",
            "pkg_status_text": "到达待取", "event_at": "2026-10-18T00:00:00-05:00",
            "event_at_raw": "2026-10-18 00:00:00", "event_text": "Delivery Attempt",
            "location": None, "timezone_unknown": True,
            "events": [
                {"status": "exception", "event_at": "2026-10-18T00:00:00-05:00",
                 "event_at_raw": "2026-10-18 00:00:00", "event_text": "Delivery Attempt",
                 "raw_event": "2026-10-18 00:00:00 Delivery Attempt", "timezone_unknown": True},
                {"status": "out_for_delivery", "event_at": "2026-10-13T06:10:00-04:00",
                 "event_at_raw": "2026-10-13 06:10:00", "event_text": "Out for Delivery",
                 "raw_event": "2026-10-13 06:10:00 LAKE MARY, FL 32746, Out for Delivery",
                 "location": "LAKE MARY, FL", "timezone_unknown": False},
            ],
        }
        result = tracking_store.import_records([summary])
        self.assertEqual(result["matched"], 1)
        self.assertEqual(self.row(row_id)["status"], "awaiting_pickup")
        history = tracking_store.events(row_id)
        self.assertEqual(len(history), 2)
        self.assertEqual(history[0]["status"], "exception")
        self.assertEqual(history[1]["status"], "out_for_delivery")

    def test_newer_event_updates_but_older_event_and_in_transit_cannot_undo_delivery(self):
        added = self.add({"order_id": "ORDER-C", "tracking_number": "SYNTHETIC201"})
        row_id = added["items"][0]["id"]
        now = dt.datetime(2026, 8, 20, 12, tzinfo=UTC)
        current = {
            "tracking_number": "SYNTHETIC201", "status": "in_transit",
            "event_at": "2026-08-19T08:00:00-05:00", "event_at_raw": "2026-08-19 08:00",
            "event_text": "Arrived at facility", "raw_event": "newer scan",
        }
        tracking_store.import_records([current], now=now)
        older = dict(current, status="out_for_delivery", event_at="2026-08-18T08:00:00-05:00",
                     event_at_raw="2026-08-18 08:00", raw_event="older scan")
        result = tracking_store.import_records([older], now=now + dt.timedelta(hours=1))
        self.assertEqual(result["items"][0]["updated"], 0)
        self.assertEqual(self.row(row_id, now=now)["status"], "in_transit")

        delivered = dict(current, status="delivered", event_at=None, event_at_raw=None,
                         event_text=None, raw_event=None, pkg_status_text="成功签收")
        tracking_store.import_records([delivered], now=now + dt.timedelta(hours=2))
        transit_again = dict(current, event_at="2026-08-20T09:00:00-05:00",
                             event_at_raw="2026-08-20 09:00")
        tracking_store.import_records([transit_again], now=now + dt.timedelta(hours=3))
        row = self.row(row_id, now=now + dt.timedelta(hours=3))
        self.assertEqual(row["status"], "delivered")
        self.assertEqual(row["pkg_status_text"], "成功签收")
        self.assertTrue(row["order_closed"])

    def test_same_timestamp_delivery_upgrades_but_older_delivery_does_not(self):
        added = self.add({"order_id": "ORDER-C2", "tracking_number": "SYNTHETIC202"})
        row_id = added["items"][0]["id"]
        now = dt.datetime(2026, 8, 20, 12, tzinfo=UTC)
        tracking_store.import_records([{
            "tracking_number": "SYNTHETIC202", "status": "in_transit",
            "event_at": "2026-08-19T08:00:00-05:00", "event_at_raw": "2026-08-19 08:00",
            "event_text": "Arrived at facility",
        }], now=now)
        older_delivery = {
            "tracking_number": "SYNTHETIC202", "status": "delivered",
            "event_at": "2026-08-19T07:59:00-05:00", "event_at_raw": "2026-08-19 07:59",
            "event_text": "Delivered", "pkg_status_text": "成功签收",
        }
        older_result = tracking_store.import_records([older_delivery], now=now)
        self.assertEqual(older_result["items"][0]["updated"], 0)
        self.assertEqual(self.row(row_id, now=now)["status"], "in_transit")

        same_time_delivery = dict(older_delivery,
                                  event_at="2026-08-19T08:00:00-05:00",
                                  event_at_raw="2026-08-19 08:00")
        same_result = tracking_store.import_records([same_time_delivery], now=now)
        self.assertEqual(same_result["items"][0]["updated"], 1)
        self.assertEqual(self.row(row_id, now=now)["status"], "delivered")
        self.assertEqual(self.row(row_id, now=now)["risk"], "delivered")

    def test_exception_filter_includes_stalled_and_actionable_usps_pickup(self):
        added = self.add(
            {"order_id": "STALE-ORDER", "tracking_number": "SYNTHETIC851"},
            {"order_id": "PICKUP-ORDER", "tracking_number": "94001118992238569284"},
            {"order_id": "NORMAL-ORDER", "tracking_number": "SYNTHETIC853"},
        )
        stale_id, pickup_id, normal_id = [item["id"] for item in added["items"]]
        tracking_store.set_carrier(pickup_id, "usps")
        now = dt.datetime(2026, 11, 13, 12, tzinfo=UTC)
        tracking_store.import_records([{
            "tracking_number": "SYNTHETIC851", "status": "in_transit",
            "event_at": "2026-11-02T00:00:00+00:00", "event_at_raw": "2026-11-02 00:00:00",
            "event_text": "In Transit to Next Facility",
        }], now=now)
        tracking_store.import_records([{
            "tracking_number": "94001118992238569284", "status": "awaiting_pickup",
            "pkg_status_text": "到达待取", "event_at": None,
            "event_at_raw": "2026-11-02 00:00:00",
            "event_text": "Delivery Attempt: Action Needed, Reminder to Schedule Redelivery of your item.",
            "timezone_unknown": True,
        }], now=now)
        tracking_store.import_records([{
            "tracking_number": "SYNTHETIC853", "status": "in_transit",
            "event_at": "2026-11-13T00:00:00+00:00", "event_text": "In Transit",
        }], now=now)

        flagged = tracking_store.list_orders(status="exception", now=now)
        self.assertEqual({row["order_id"] for row in flagged}, {"STALE-ORDER", "PICKUP-ORDER"})
        stale = next(row for row in flagged if row["id"] == stale_id)
        pickup = next(row for row in flagged if row["id"] == pickup_id)
        self.assertEqual(stale["risk"], "stalled")
        self.assertTrue(stale["exception_bucket"])
        self.assertEqual(pickup["risk"], "时区待确认")
        self.assertTrue(pickup["exception_bucket"])
        self.assertFalse(next(row for row in tracking_store.list_orders(now=now)
                              if row["id"] == normal_id)["exception_bucket"])

    def test_unknown_timezone_defaults_to_central_and_not_found_never_stalls(self):
        added = self.add(
            {"order_id": "ORDER-D", "tracking_number": "SYNTHETIC301"},
            {"order_id": "ORDER-E", "tracking_number": "SYNTHETIC302"},
        )
        far_future = dt.datetime(2026, 10, 10, tzinfo=UTC)
        tracking_store.import_records([{
            "tracking_number": "SYNTHETIC301", "status": "in_transit",
            "event_at": None, "event_at_raw": "2026-10-01 08:00",
            "event_text": "Arrived at facility", "raw_event": "copied event",
            "location": "Somewhere, XX", "timezone_unknown": True,
        }], now=far_future)
        assumed_zone = self.row(added["items"][0]["id"], now=far_future)
        self.assertEqual(assumed_zone["last_event_at"], "2026-10-01T08:00:00-05:00")
        self.assertFalse(assumed_zone["timezone_unknown"])
        self.assertEqual(assumed_zone["risk"], "stalled")
        self.assertEqual(assumed_zone["stale_hours"], 203.0)
        self.assertEqual(assumed_zone["last_event_at_raw"], "2026-10-01 08:00")
        assumed_event = tracking_store.events(added["items"][0]["id"])[0]
        self.assertEqual(assumed_event["event_at"], "2026-10-01T08:00:00-05:00")
        self.assertFalse(assumed_event["timezone_unknown"])

        earlier = far_future - dt.timedelta(days=4)
        tracking_store.import_records([{
            "tracking_number": "SYNTHETIC302", "status": "not_found",
            "pkg_status_text": "查询不到", "event_at": None,
        }], now=earlier)
        not_found = self.row(added["items"][1]["id"], now=far_future)
        self.assertEqual(not_found["risk"], "not_found")
        self.assertIsNone(not_found["stale_hours"])
        next_items = tracking_store.pending_candidates()
        self.assertEqual(next_items, [
            {"tracking_number": "SYNTHETIC302", "order_ids": ["ORDER-E"]},
            {"tracking_number": "SYNTHETIC301", "order_ids": ["ORDER-D"]},
        ])

    def test_new_unknown_zone_candidate_uses_central_and_suppresses_old_stall(self):
        added = self.add({"order_id": "ORDER-D2", "tracking_number": "SYNTHETIC303"})
        row_id = added["items"][0]["id"]
        now = dt.datetime(2026, 10, 10, tzinfo=UTC)
        tracking_store.import_records([{
            "tracking_number": "SYNTHETIC303", "status": "in_transit",
            "event_at": "2026-10-01T08:00:00+00:00", "event_at_raw": "2026-10-01 08:00",
            "event_text": "Old known scan",
        }], now=now - dt.timedelta(days=4))
        tracking_store.import_records([{
            "tracking_number": "SYNTHETIC303", "status": "out_for_delivery",
            "event_at": None, "event_at_raw": "2026-10-10 08:00",
            "event_text": "Local scan", "raw_event": "copied unknown-zone scan",
            "timezone_unknown": True,
        }], now=now)
        row = self.row(row_id, now=now)
        self.assertEqual(row["risk"], "normal")
        self.assertEqual(row["stale_hours"], 0.0)
        self.assertFalse(row["timezone_unknown"])
        self.assertEqual(row["last_event_at"], "2026-10-10T08:00:00-05:00")
        self.assertEqual(row["last_event_at_raw"], "2026-10-10 08:00")

    def test_missing_event_time_uses_check_age_only_without_claiming_stall(self):
        added = self.add({"order_id": "ORDER-F", "tracking_number": "SYNTHETIC401"})
        checked = dt.datetime(2026, 1, 1, tzinfo=UTC)
        later = checked + dt.timedelta(days=3)
        tracking_store.import_records([{
            "tracking_number": "SYNTHETIC401", "status": "in_transit",
            "pkg_status_text": "运输途中", "event_at": None, "event_at_raw": None,
        }], now=checked)
        row = self.row(added["items"][0]["id"], now=later)
        self.assertEqual(row["risk"], "awaiting_event")
        self.assertEqual(row["stale_hours"], 72.0)

    def test_unknown_or_malformed_status_preserves_shipment_data_but_records_check(self):
        added = self.add({"order_id": "ORDER-G", "tracking_number": "SYNTHETIC501"})
        row_id = added["items"][0]["id"]
        now = dt.datetime(2026, 5, 1, tzinfo=UTC)
        valid = {
            "tracking_number": "SYNTHETIC501", "status": "in_transit",
            "event_at": "2026-04-30T12:00:00+00:00", "event_at_raw": "2026-04-30 12:00",
            "event_text": "Departed facility", "raw_event": "raw scan",
        }
        tracking_store.import_records([valid], now=now)
        unknown = dict(valid, status="unknown", event_at="2026-05-01T10:00:00+00:00",
                       event_at_raw="2026-05-01 10:00", event_text="Unrecognized")
        tracking_store.import_records([unknown, {"tracking_number": "BAD!", "status": "delivered"}],
                                      now=now + dt.timedelta(hours=1))
        row = self.row(row_id, now=now + dt.timedelta(hours=1))
        self.assertEqual(row["status"], "in_transit")
        self.assertEqual(row["last_event_text"], "Departed facility")
        self.assertEqual(row["last_checked_at"], (now + dt.timedelta(hours=1)).isoformat(timespec="seconds"))
        self.assertEqual(len(tracking_store.events(row_id)), 1)

    def test_enable_remove_and_shared_tracking_events(self):
        added = self.add(
            {"order_id": "ORDER-H", "tracking_number": "SYNTHETIC601"},
            {"order_id": "ORDER-I", "tracking_number": "SYNTHETIC601"},
        )
        row_a, row_b = [item["id"] for item in added["items"]]
        tracking_store.import_records([{
            "tracking_number": "SYNTHETIC601", "status": "in_transit",
            "event_at": "2026-05-01T12:00:00+00:00", "event_text": "In transit",
        }], now=dt.datetime(2026, 5, 1, 13, tzinfo=UTC))
        self.assertEqual(len(tracking_store.events(row_a)), 1)
        self.assertTrue(tracking_store.set_enabled(row_a, False))
        self.assertEqual(tracking_store.pending_candidates(), [
            {"tracking_number": "SYNTHETIC601", "order_ids": ["ORDER-I"]}
        ])
        self.assertTrue(tracking_store.remove_order(row_a))
        self.assertEqual(len(tracking_store.events(row_b)), 1)
        self.assertTrue(tracking_store.remove_order(row_b))
        self.assertEqual(tracking_store.events(row_b), [])
        self.assertFalse(tracking_store.set_enabled(999, True))
        self.assertFalse(tracking_store.remove_order(999))

    def test_carrier_candidates_keep_original_number_and_manual_override_is_shared(self):
        extended = "4201234567890123456789012345678901"
        self.assertEqual(len(extended), 34)
        tracking_store.add_pairs([
            {"order_id": "ORDER-FEDEX", "tracking_number": "123456789012"},
            {"order_id": "ORDER-USPS", "tracking_number": "94001118992238569284"},
            {"order_id": "ORDER-EXT", "tracking_number": extended},
            {"order_id": "ORDER-SHARED", "tracking_number": "SYNTHETIC001"},
        ])
        rows = tracking_store.list_orders()
        fedex = next(row for row in rows if row["order_id"] == "ORDER-FEDEX")
        usps = next(row for row in rows if row["order_id"] == "ORDER-USPS")
        ext = next(row for row in rows if row["order_id"] == "ORDER-EXT")
        self.assertEqual(fedex["carrier_hint"], "fedex")
        self.assertEqual(usps["carrier_hint"], "usps")
        self.assertEqual(ext["carrier_hint"], "usps")
        self.assertEqual(ext["tracking_number"], extended)
        self.assertIn("不截断", ext["carrier_reason"])
        tracking_store.add_pairs([{"order_id": "ORDER-SHARED-2", "tracking_number": "SYNTHETIC001"}])
        shared = next(row for row in tracking_store.list_orders() if row["order_id"] == "ORDER-SHARED")
        self.assertTrue(tracking_store.set_carrier(shared["id"], "fedex"))
        related = [row for row in tracking_store.list_orders() if row["tracking_number"] == "SYNTHETIC001"]
        self.assertEqual({row["carrier"] for row in related}, {"fedex"})
        self.assertTrue(all(row["carrier_source"] == "manual" for row in related))
        self.assertTrue(tracking_store.set_carrier(shared["id"], "auto"))
        reset = next(row for row in tracking_store.list_orders() if row["id"] == shared["id"])
        self.assertEqual(reset["carrier"], "auto")

    def test_preview_matches_case_sensitive_order_and_case_insensitive_track_key_with_capacity(self):
        pairs = [{"order_id":"ORD-X","tracking_number":"TRACK00001"},
                 {"order_id":"ord-x","tracking_number":"track00001"},
                 {"order_id":"ORD-X","tracking_number":"track00001"}]
        preview = tracking_store.preview_pairs(pairs)
        self.assertEqual((preview["new_associations"], preview["input_duplicates"]), (2, 1))
        self.assertEqual(tracking_store.add_pairs(pairs)["added"], 2)

    def test_capacity_preview_keeps_mixed_case_associations_and_rejects_atomically(self):
        tracking_store.add_pairs([{"order_id":f"FILL-{i}","tracking_number":f"FILLTRACK{i:05d}"} for i in range(499)])
        pairs = [{"order_id":"ORD-X","tracking_number":"TRACK00001"},
                 {"order_id":"ord-x","tracking_number":"track00001"}]
        preview = tracking_store.preview_pairs(pairs)
        self.assertEqual(preview["new_associations"], 2)
        self.assertFalse(preview["capacity_ok"])
        with self.assertRaises(ValueError): tracking_store.add_pairs(pairs)
        self.assertEqual(tracking_store.order_count(), 499)

    def test_manual_carrier_is_inherited_through_paused_association_and_conflicts_are_visible(self):
        first = tracking_store.add_pairs([{"order_id":"ONE","tracking_number":"123456789012"}])["items"][0]
        self.assertTrue(tracking_store.set_carrier(first["id"], "usps"))
        second = tracking_store.add_pairs([{"order_id":"TWO","tracking_number":"123456789012"}])["items"][0]
        self.assertEqual(tracking_store.list_orders()[1]["carrier"], "usps")
        tracking_store.set_enabled(first["id"], False)
        candidates = tracking_store.pending_candidates(detailed=True)
        self.assertEqual(candidates[0]["carrier"], "usps")
        self.assertFalse(candidates[0]["carrier_conflict"])
        self.assertEqual(tracking_store.next_batch(detailed=True), [])
        conn = tracking_store._conn()
        conn.execute("UPDATE lt_orders SET carrier_override='fedex', enabled=0 WHERE id=?", (first["id"],)); conn.commit(); conn.close()
        candidates = tracking_store.pending_candidates(detailed=True)
        self.assertEqual(candidates[0]["carrier"], "auto")
        self.assertTrue(candidates[0]["carrier_conflict"])
        self.assertIn("冲突", candidates[0]["carrier_reason"])
        self.assertEqual(tracking_store.next_batch(detailed=True), [])

    def test_pending_candidates_split_carriers_and_offset_batches_never_repeat(self):
        fedex_tracks = [f"{123456789000 + index:012d}" for index in range(42)]
        pairs = [{"order_id": f"FEDEX-{index}", "tracking_number": number}
                 for index, number in enumerate(fedex_tracks)]
        pairs += [
            {"order_id": "FEDEX-SHARED-2", "tracking_number": fedex_tracks[0]},
            {"order_id": "USPS-A", "tracking_number": "94001118992238569284"},
            {"order_id": "USPS-B", "tracking_number": "420123456789012345678901234567"},
            {"order_id": "AUTO-A", "tracking_number": "SYNTHETIC901"},
        ]
        added = tracking_store.add_pairs(pairs)
        candidates = tracking_store.pending_candidates(detailed=True)
        by_carrier = {carrier: [item for item in candidates if item["carrier"] == carrier]
                      for carrier in ("fedex", "usps", "auto")}
        self.assertEqual((len(by_carrier["fedex"]), len(by_carrier["usps"]), len(by_carrier["auto"])), (42, 2, 1))
        shared = next(item for item in by_carrier["fedex"] if item["tracking_number"] == fedex_tracks[0])
        self.assertIn("FEDEX-SHARED-2", shared["order_ids"])
        first = tracking_store.next_batch(limit=40)
        second = tracking_store.next_batch(limit=40, offset=40)
        end = tracking_store.next_batch(limit=40, offset=80)
        self.assertEqual((len(first), len(second), end), (40, 2, []))
        self.assertTrue({row["tracking_number"] for row in first}.isdisjoint(
            row["tracking_number"] for row in second))
        self.assertEqual({row["tracking_number"] for row in first + second}, set(fedex_tracks))

        first_fedex_row = next(item for item in added["items"] if item["tracking_number"] == fedex_tracks[0])
        self.assertTrue(tracking_store.set_carrier(first_fedex_row["id"], "usps"))
        refreshed = tracking_store.pending_candidates(detailed=True)
        self.assertNotIn(fedex_tracks[0], {item["tracking_number"] for item in refreshed if item["carrier"] == "fedex"})
        self.assertIn(fedex_tracks[0], {item["tracking_number"] for item in refreshed if item["carrier"] == "usps"})
        new_track = "999999999999"
        tracking_store.add_pairs([{"order_id": "FEDEX-NEW", "tracking_number": new_track}])
        self.assertIn(new_track, {item["tracking_number"] for item in tracking_store.pending_candidates("fedex")})

    def test_delivered_cleanup_never_removes_archived_order_or_its_history(self):
        added = self.add(
            {"order_id": "ARCHIVED-DONE", "tracking_number": "SYNTHETIC710"},
            {"order_id": "ACTIVE-DONE", "tracking_number": "SYNTHETIC711"},
        )
        for number in ("SYNTHETIC710", "SYNTHETIC711"):
            tracking_store.import_records([{
                "tracking_number": number, "status": "delivered", "pkg_status_text": "已妥投",
                "event_at": "2026-06-01T12:00:00+00:00", "event_text": "Delivered",
            }])
        archived_row = added["items"][0]
        tracking_store.archive_order(archived_row["id"], "归档后保留完整历史。")
        self.assertEqual(tracking_store.delivered_cleanup_preview(), {"orders": 1, "package_rows": 1})
        result = tracking_store.clear_delivered_orders()
        self.assertEqual(result["deleted_orders"], 1)
        self.assertEqual([row["order_id"] for row in tracking_store.list_orders(archived=True)], ["ARCHIVED-DONE"])
        self.assertTrue(any(event.get("event_type") == "archived"
                            for event in tracking_store.events(archived_row["id"])))

    def test_clear_delivered_package_rows_from_mixed_orders_and_preserves_shared_events(self):
        added = self.add(
            {"order_id":"CLEAN-ORDER", "tracking_number":"SYNTHETIC701"},
            {"order_id":"CLEAN-ORDER", "tracking_number":"SYNTHETIC702"},
            {"order_id":"MIXED-ORDER", "tracking_number":"SYNTHETIC703"},
            {"order_id":"MIXED-ORDER", "tracking_number":"SYNTHETIC704"},
            {"order_id":"UNKNOWN-ORDER", "tracking_number":"SYNTHETIC705"},
            {"order_id":"SHARED-DONE", "tracking_number":"SYNTHETIC706"},
            {"order_id":"EXCEPTION-ORDER", "tracking_number":"SYNTHETIC707"},
            {"order_id":"TEXT-DELIVERED", "tracking_number":"SYNTHETIC708"},
            {"order_id":"NEGATIVE-DELIVERY", "tracking_number":"SYNTHETIC709"},
        )
        statuses = {
            "SYNTHETIC701":"delivered", "SYNTHETIC702":"delivered",
            "SYNTHETIC703":"delivered", "SYNTHETIC704":"in_transit",
            "SYNTHETIC706":"delivered", "SYNTHETIC707":"exception",
            "SYNTHETIC708":"unknown", "SYNTHETIC709":"in_transit",
        }
        for number, status in statuses.items():
            tracking_store.import_records([{"tracking_number":number,"status":status,
                "pkg_status_text":"未送达" if number == "SYNTHETIC709" else None,
                "event_at":"2026-06-01T12:00:00+00:00","event_text":f"Event {number}"}])
        # Legacy rows may have an unnormalized status while the explicit text says delivered.
        conn = tracking_store._conn()
        conn.execute("UPDATE lt_orders SET status='unknown',pkg_status_text='已妥投' WHERE tracking_number='SYNTHETIC708'")
        conn.commit(); conn.close()
        # A delivered package can share its event with an open order; its event must survive cleanup.
        shared_open_row = self.add({"order_id":"SHARED-OPEN", "tracking_number":"SYNTHETIC706"})["items"][0]
        self.assertEqual(tracking_store.delivered_cleanup_preview(), {"orders":4, "package_rows":5})
        shared_done_row = next(row for row in added["items"] if row["order_id"] == "SHARED-DONE")
        self.assertEqual(len(tracking_store.events(shared_open_row["id"])), 1)

        result = tracking_store.clear_delivered_orders()
        self.assertEqual(result, {"deleted_orders":4,"deleted_order_rows":5,"deleted_event_rows":3})
        rows = tracking_store.list_orders()
        order_ids = {row["order_id"] for row in rows}
        self.assertEqual(order_ids, {"MIXED-ORDER", "UNKNOWN-ORDER", "SHARED-OPEN", "EXCEPTION-ORDER", "NEGATIVE-DELIVERY"})
        mixed = [row for row in rows if row["order_id"] == "MIXED-ORDER"]
        self.assertEqual(len(mixed), 1)
        self.assertEqual(mixed[0]["status"], "in_transit")
        unknown = next(row for row in rows if row["order_id"] == "UNKNOWN-ORDER")
        self.assertIsNone(unknown["status"])
        exception = next(row for row in rows if row["order_id"] == "EXCEPTION-ORDER")
        self.assertEqual(exception["status"], "exception")
        negative = next(row for row in rows if row["order_id"] == "NEGATIVE-DELIVERY")
        self.assertEqual(negative["status"], "in_transit")
        self.assertEqual(len(tracking_store.events(shared_open_row["id"])), 1)
        self.assertEqual(tracking_store.events(shared_done_row["id"]), [])
        self.assertEqual(tracking_store.delivered_cleanup_preview(), {"orders":0,"package_rows":0})
        self.assertEqual(tracking_store.clear_delivered_orders(),
                         {"deleted_orders":0,"deleted_order_rows":0,"deleted_event_rows":0})

    def test_clear_delivered_orders_rolls_back_as_one_transaction(self):
        added = self.add(
            {"order_id":"ATOMIC-CLEAN", "tracking_number":"SYNTHETIC801"},
            {"order_id":"ATOMIC-CLEAN", "tracking_number":"SYNTHETIC802"},
        )
        for number in ("SYNTHETIC801", "SYNTHETIC802"):
            tracking_store.import_records([{"tracking_number":number,"status":"delivered",
                "event_at":"2026-06-01T12:00:00+00:00","event_text":"Synthetic delivered scan"}])
        conn = tracking_store._conn()
        conn.execute("CREATE TRIGGER reject_delivered_cleanup BEFORE DELETE ON lt_orders "
                     "BEGIN SELECT RAISE(ABORT, 'synthetic failure'); END")
        conn.commit(); conn.close()
        with self.assertRaises(sqlite3.IntegrityError):
            tracking_store.clear_delivered_orders()
        self.assertEqual(len(tracking_store.list_orders()), 2)
        self.assertEqual(len(tracking_store.events(added["items"][0]["id"])), 1)
        self.assertEqual(tracking_store.delivered_cleanup_preview(), {"orders":1,"package_rows":2})

    def test_feta_writeback_is_idempotent_and_never_fabricates_scan_time(self):
        added = self.add(
            {"order_id": "FETA-ORDER-A", "tracking_number": "FETATEST001"},
            {"order_id": "FETA-ORDER-B", "tracking_number": "FETATEST001"},
        )
        received = "2026-11-01T12:00:00Z"
        event = {"id": 301, "tracking_number": "FETATEST001", "received_at_utc": received,
                 "raw_status": "In transit", "normalized_status": "IN_TRANSIT",
                 "raw_eta_text": "Tuesday, Nov 3"}
        imported = tracking_store.apply_feta_event(event, now="2026-11-01T12:05:00Z")
        self.assertEqual((imported["matched_orders"], imported["status_updated"], imported["event_added"]), (2, 2, True))
        rows = [row for row in tracking_store.list_orders() if row["tracking_number"] == "FETATEST001"]
        self.assertEqual({row["status"] for row in rows}, {"in_transit"})
        self.assertTrue(all(row["last_event_at"] is None for row in rows))
        self.assertTrue(all(row["last_checked_at"] == "2026-11-01T12:00:00+00:00" for row in rows))
        events = tracking_store.events(added["items"][0]["id"])
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["source"], "feta")
        self.assertEqual(events[0]["source_event_id"], 301)
        self.assertIsNone(events[0]["event_at"])
        self.assertIsNone(events[0]["location"])
        self.assertEqual(events[0]["source_received_at"], "2026-11-01T12:00:00+00:00")
        self.assertEqual(events[0]["raw_eta_text"], "Tuesday, Nov 3")

        replay = tracking_store.apply_feta_event(event, now="2026-11-01T13:00:00Z")
        self.assertTrue(replay["duplicate"])
        self.assertEqual(len(tracking_store.events(added["items"][0]["id"])), 1)
        self.assertTrue(all(row["last_checked_at"] == "2026-11-01T12:00:00+00:00"
                            for row in tracking_store.list_orders()))

    def test_feta_unknown_label_created_stale_and_terminal_observations_do_not_downgrade(self):
        added = self.add(
            {"order_id": "FETA-UNKNOWN", "tracking_number": "FETATEST002"},
            {"order_id": "FETA-DELIVERED", "tracking_number": "FETATEST003"},
            {"order_id": "FETA-STALE", "tracking_number": "FETATEST004"},
        )
        ids = {item["tracking_number"]: item["id"] for item in added["items"]}
        tracking_store.import_records([{
            "tracking_number": "FETATEST003", "status": "delivered", "pkg_status_text": "Delivered",
            "event_at": "2026-11-01T09:00:00Z",
        }], now="2026-11-01T10:00:00Z")
        tracking_store.import_records([{
            "tracking_number": "FETATEST004", "status": "out_for_delivery", "pkg_status_text": "Out for delivery",
            "event_at": "2026-11-01T11:00:00Z",
        }], now="2026-11-01T12:00:00Z")

        unknown = tracking_store.apply_feta_event({
            "id": 302, "tracking_number": "FETATEST002", "received_at_utc": "2026-11-01T12:30:00Z",
            "raw_status": "No result", "normalized_status": "UNKNOWN", "raw_eta_text": None,
        })
        label = tracking_store.apply_feta_event({
            "id": 303, "tracking_number": "FETATEST002", "received_at_utc": "2026-11-01T12:31:00Z",
            "raw_status": "Label created", "normalized_status": "LABEL_CREATED", "raw_eta_text": None,
        })
        delivered = tracking_store.apply_feta_event({
            "id": 304, "tracking_number": "FETATEST003", "received_at_utc": "2026-11-01T13:00:00Z",
            "raw_status": "In transit", "normalized_status": "IN_TRANSIT", "raw_eta_text": None,
        })
        regression = tracking_store.apply_feta_event({
            "id": 306, "tracking_number": "FETATEST004", "received_at_utc": "2026-11-01T13:30:00Z",
            "raw_status": "In transit", "normalized_status": "IN_TRANSIT", "raw_eta_text": None,
        })
        stale = tracking_store.apply_feta_event({
            "id": 305, "tracking_number": "FETATEST004", "received_at_utc": "2026-11-01T11:30:00Z",
            "raw_status": "In transit", "normalized_status": "IN_TRANSIT", "raw_eta_text": None,
        })
        rows = {row["tracking_number"]: row for row in tracking_store.list_orders()}
        self.assertEqual((unknown["status_updated"], label["status_updated"]), (0, 0))
        self.assertIsNone(rows["FETATEST002"]["status"])
        self.assertEqual((delivered["status_updated"], rows["FETATEST003"]["status"]), (0, "delivered"))
        self.assertEqual(regression["status_updated"], 0)
        self.assertEqual((stale["stale_observations"], rows["FETATEST004"]["status"]), (1, "out_for_delivery"))
        self.assertEqual(rows["FETATEST004"]["last_checked_at"], "2026-11-01T13:30:00+00:00")
        self.assertEqual(len(tracking_store.events(ids["FETATEST002"])), 2)

    def test_feta_unmatched_or_invalid_event_does_not_write(self):
        result = tracking_store.apply_feta_event({
            "id": 306, "tracking_number": "FETAMISSING001", "received_at_utc": "2026-11-01T12:00:00Z",
            "raw_status": "Delivered", "normalized_status": "DELIVERED", "raw_eta_text": None,
        })
        self.assertFalse(result["matched"])
        self.assertFalse(result["event_added"])
        with self.assertRaisesRegex(ValueError, "收件时间"):
            tracking_store.apply_feta_event({
                "id": 307, "tracking_number": "FETAMISSING001", "received_at_utc": "2026-11-01T12:00:00",
                "raw_status": "Delivered", "normalized_status": "DELIVERED",
            })

    def test_feta_writeback_rolls_back_event_when_order_update_fails(self):
        added = self.add({"order_id": "FETA-ATOMIC", "tracking_number": "FETATEST005"})
        conn = tracking_store._conn()
        conn.execute("CREATE TRIGGER reject_feta_status BEFORE UPDATE ON lt_orders "
                     "BEGIN SELECT RAISE(ABORT, 'synthetic FETA writeback failure'); END")
        conn.commit(); conn.close()
        with self.assertRaises(sqlite3.IntegrityError):
            tracking_store.apply_feta_event({
                "id": 308, "tracking_number": "FETATEST005", "received_at_utc": "2026-11-01T12:00:00Z",
                "raw_status": "Delivered", "normalized_status": "DELIVERED", "raw_eta_text": "Nov 3",
            })
        self.assertEqual(tracking_store.events(added["items"][0]["id"]), [])
        self.assertIsNone(tracking_store.list_orders()[0]["status"])

    def test_needs_update_scope_includes_enabled_non_delivered_not_only_pending(self):
        added = self.add(
            {"order_id": "SCOPE-PENDING", "tracking_number": "SCOPEPENDING1"},
            {"order_id": "SCOPE-TRANSIT", "tracking_number": "SCOPETRANSIT1"},
            {"order_id": "SCOPE-OFD", "tracking_number": "SCOPEOUTFOR1"},
            {"order_id": "SCOPE-EXCEPTION", "tracking_number": "SCOPEEXCEPT1"},
            {"order_id": "SCOPE-DELIVERED", "tracking_number": "SCOPEDELIVER1"},
            {"order_id": "SCOPE-PAUSED", "tracking_number": "SCOPEPAUSED1"},
        )
        for number, status in (
            ("SCOPETRANSIT1", "in_transit"), ("SCOPEOUTFOR1", "out_for_delivery"),
            ("SCOPEEXCEPT1", "exception"), ("SCOPEDELIVER1", "delivered"),
            ("SCOPEPAUSED1", "in_transit"),
        ):
            tracking_store.import_records([{"tracking_number": number, "status": status,
                "event_at": "2026-11-03T12:00:00+00:00", "event_text": "Synthetic status"}])
        paused = next(item["id"] for item in added["items"] if item["tracking_number"] == "SCOPEPAUSED1")
        tracking_store.set_enabled(paused, False)

        pending = tracking_store.list_orders(limit=None, status="pending")
        needs_update = tracking_store.list_orders(limit=None, status="needs_update")
        self.assertEqual({row["tracking_number"] for row in pending}, {"SCOPEPENDING1"})
        self.assertEqual({row["tracking_number"] for row in needs_update}, {
            "SCOPEPENDING1", "SCOPETRANSIT1", "SCOPEOUTFOR1", "SCOPEEXCEPT1",
        })
        self.assertEqual(tracking_store.order_count(status="needs_update"), 4)

    def test_order_listing_supports_bounded_pages_and_filters(self):
        tracking_store.add_pairs([
            {"order_id": f"ORDER-PAGE-{i}", "tracking_number": f"PAGETRACK{i:04d}"}
            for i in range(5)
        ])
        page = tracking_store.list_orders(limit=2, offset=1)
        self.assertEqual(len(page), 2)
        self.assertEqual(page[0]["order_id"], "ORDER-PAGE-1")
        self.assertEqual(tracking_store.order_count(search="PAGETRACK0002"), 1)
        self.assertEqual(len(tracking_store.list_orders(limit=10, carrier="auto")), 5)


class TrackingMigrationTests(unittest.TestCase):
    def test_pre_carrier_schema_migrates_idempotently_without_losing_orders(self):
        uri = f"file:tracking_migration_{uuid.uuid4().hex}?mode=memory&cache=shared"
        keeper = sqlite3.connect(uri, uri=True)
        keeper.execute("""CREATE TABLE lt_orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT, order_id TEXT NOT NULL, tracking_number TEXT NOT NULL,
            created_at TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1, status TEXT, pkg_status_text TEXT,
            last_event_at TEXT, last_event_at_raw TEXT, last_event_text TEXT, last_location TEXT,
            last_checked_at TEXT, last_error TEXT, transit_days INTEGER, timezone_unknown INTEGER NOT NULL DEFAULT 0,
            source_country TEXT, destination_country TEXT)""")
        keeper.execute("""CREATE TABLE lt_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT, tracking_number TEXT NOT NULL, event_at TEXT,
            event_at_raw TEXT, event_text TEXT, raw_event TEXT, location TEXT, status TEXT,
            pkg_status_text TEXT, source_country TEXT, destination_country TEXT, transit_days INTEGER,
            timezone_unknown INTEGER NOT NULL DEFAULT 0, imported_at TEXT NOT NULL)""")
        keeper.execute("INSERT INTO lt_orders(order_id,tracking_number,created_at,status,last_event_text) VALUES(?,?,?,?,?)",
                       ("ORDER-OLD", "SYNTHETIC001", "2026-01-01T00:00:00+00:00", "in_transit", "synthetic event"))
        keeper.commit()
        def connect(): return sqlite3.connect(uri, uri=True)
        db_patch = patch.object(local_db, "_conn", new=connect); db_patch.start()
        try:
            row = tracking_store.list_orders()[0]
            self.assertEqual((row["order_id"], row["tracking_number"], row["last_event_text"]),
                             ("ORDER-OLD", "SYNTHETIC001", "synthetic event"))
            migration_conn = tracking_store._conn()
            columns = {info[1] for info in migration_conn.execute("PRAGMA table_info(lt_orders)")}
            event_columns = {info[1] for info in migration_conn.execute("PRAGMA table_info(lt_events)")}
            migration_conn.close()
            self.assertIn("carrier_override", columns)
            self.assertTrue({"source", "source_event_id", "source_received_at", "raw_eta_text"}.issubset(event_columns))
            self.assertTrue(tracking_store.set_carrier(row["id"], "fedex"))
            self.assertEqual(tracking_store.list_orders()[0]["carrier"], "fedex")
            tracking_store.list_orders()  # Repeated startup migration stays idempotent.
            self.assertEqual(tracking_store.order_count(), 1)
        finally:
            db_patch.stop(); keeper.close()


if __name__ == "__main__":
    unittest.main()
