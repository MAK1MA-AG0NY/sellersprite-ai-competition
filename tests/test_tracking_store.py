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

    def test_next_batch_groups_tracks_sorts_oldest_and_caps_at_40(self):
        pairs = [{"order_id": f"ORDER-{i}", "tracking_number": f"TRACKING{i:04d}"}
                 for i in range(42)]
        tracking_store.add_pairs(pairs, now=dt.datetime(2026, 1, 1, tzinfo=UTC))
        # Two order IDs share one track and it should only be queried once.
        tracking_store.add_pairs([
            {"order_id": "ORDER-OTHER-A", "tracking_number": "TRACKING0000"},
            {"order_id": "ORDER-OTHER-B", "tracking_number": "TRACKING0000"},
        ])
        batch = tracking_store.next_batch(limit=100)
        self.assertEqual(len(batch), 40)
        self.assertEqual(batch[0]["tracking_number"], "TRACKING0000")
        self.assertIn("ORDER-OTHER-A", batch[0]["order_ids"])
        self.assertIn("ORDER-OTHER-B", batch[0]["order_ids"])
        self.assertEqual(len({item["tracking_number"].casefold() for item in batch}), 40)
        self.assertEqual(len(tracking_store.next_batch(limit=0)), 0)
        self.assertEqual(tracking_store.pending_count(), 42)
        tracking_store.import_records([{
            "tracking_number": "TRACKING0000", "status": "delivered",
            "event_at": "2026-01-02T12:00:00+00:00", "pkg_status_text": "Delivered",
        }])
        self.assertEqual(tracking_store.pending_count(), 41)

    def test_next_batch_finishes_unchecked_remainder_before_refreshing_old_scans(self):
        created_at = dt.datetime(2026, 5, 1, tzinfo=UTC)
        tracking_store.add_pairs([
            {"order_id": f"BATCH-ORDER-{i}", "tracking_number": f"BATCHTRACK{i:04d}"}
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
        self.assertEqual(tracking_store.next_batch(), [
            {"tracking_number": "SYNTHETIC102", "order_ids": ["ORDER-A"]}
        ])

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

    def test_unknown_timezone_and_not_found_never_trigger_false_stall(self):
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
        unknown_zone = self.row(added["items"][0]["id"], now=far_future)
        self.assertEqual(unknown_zone["risk"], "时区待确认")
        self.assertIsNone(unknown_zone["stale_hours"])
        self.assertIsNone(unknown_zone["last_event_at"])
        self.assertEqual(unknown_zone["last_event_at_raw"], "2026-10-01 08:00")

        earlier = far_future - dt.timedelta(days=4)
        tracking_store.import_records([{
            "tracking_number": "SYNTHETIC302", "status": "not_found",
            "pkg_status_text": "查询不到", "event_at": None,
        }], now=earlier)
        not_found = self.row(added["items"][1]["id"], now=far_future)
        self.assertEqual(not_found["risk"], "not_found")
        self.assertIsNone(not_found["stale_hours"])
        next_items = tracking_store.next_batch()
        self.assertEqual(next_items, [
            {"tracking_number": "SYNTHETIC302", "order_ids": ["ORDER-E"]},
            {"tracking_number": "SYNTHETIC301", "order_ids": ["ORDER-D"]},
        ])

    def test_new_unknown_zone_candidate_suppresses_old_stall_alarm(self):
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
        self.assertEqual(row["risk"], "时区待确认")
        self.assertIsNone(row["stale_hours"])
        self.assertIsNone(row["last_event_at"])
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
        self.assertEqual(tracking_store.next_batch(), [
            {"tracking_number": "SYNTHETIC601", "order_ids": ["ORDER-I"]}
        ])
        self.assertTrue(tracking_store.remove_order(row_a))
        self.assertEqual(len(tracking_store.events(row_b)), 1)
        self.assertTrue(tracking_store.remove_order(row_b))
        self.assertEqual(tracking_store.events(row_b), [])
        self.assertFalse(tracking_store.set_enabled(999, True))
        self.assertFalse(tracking_store.remove_order(999))


if __name__ == "__main__":
    unittest.main()
