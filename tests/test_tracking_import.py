"""Offline parser tests using synthetic tracking/order values only."""
import unittest

import tracking_import


class ParsePairsTests(unittest.TestCase):
    def test_tabs_take_precedence_and_comma_rows_are_supported(self):
        parsed = tracking_import.parse_pairs(
            "  ORD-1  \t  SYNTHETIC001  \nORD-2,SYNTHETIC002\nORDER, WITH COMMA\tSYNTHETIC003"
        )
        self.assertEqual(parsed["errors"], [])
        self.assertEqual(parsed["pairs"], [
            {"order_id": "ORD-1", "tracking_number": "SYNTHETIC001"},
            {"order_id": "ORD-2", "tracking_number": "SYNTHETIC002"},
            {"order_id": "ORDER, WITH COMMA", "tracking_number": "SYNTHETIC003"},
        ])

    def test_invalid_pairs_and_pair_limit_are_reported_by_line(self):
        parsed = tracking_import.parse_pairs(
            "\nNO-TRACK\n\tSYNTHETIC004\n" + "A" * 101 + "\tSYNTHETIC005\n" +
            "A\tbad-characters!\n"
        )
        self.assertEqual(parsed["pairs"], [])
        self.assertEqual(len(parsed["errors"]), 4)
        self.assertEqual(parsed["errors"][0]["line"], 2)
        too_many = tracking_import.parse_pairs(
            "\n".join(f"ORD-{i}\tSYNTHETIC{i:03d}" for i in range(501))
        )
        self.assertEqual(len(too_many["pairs"]), 500)
        self.assertEqual(too_many["errors"][-1]["line"], 501)


class ParseSummaryTests(unittest.TestCase):
    def test_delivered_indiana_row_infers_central_daylight_time(self):
        parsed = tracking_import.parse_summary(
            "SYNTHETIC101\t美国\t美国\t2026-08-17 12:17 Merrillville, IN, US Delivered\t成功签收\t(5 天)"
        )
        self.assertEqual(parsed["errors"], [])
        record = parsed["records"][0]
        self.assertEqual(record["tracking_number"], "SYNTHETIC101")
        self.assertEqual(record["status"], "delivered")
        self.assertEqual(record["pkg_status_text"], "成功签收")
        self.assertEqual(record["event_at_raw"], "2026-08-17 12:17")
        self.assertEqual(record["event_at"], "2026-08-17T12:17:00-05:00")
        self.assertEqual(record["event_text"], "Delivered")
        self.assertEqual(record["raw_event"], "2026-08-17 12:17 Merrillville, IN, US Delivered")
        self.assertEqual(record["location"], "Merrillville, IN")
        self.assertEqual(record["source_country"], "美国")
        self.assertEqual(record["destination_country"], "美国")
        self.assertEqual(record["transit_days"], 5)
        self.assertFalse(record["timezone_unknown"])

    def test_homestead_event_fallback_and_separators_footer(self):
        parsed = tracking_import.parse_summary(
            "SYNTHETIC202\t美国\t美国\t2026-10-02 09:08 HOMESTEAD, FL, US Arrived at FedEx location\t运输途中\t(21 天)\n"
            "======================================\nPowered by www.17track.net"
        )
        self.assertEqual(parsed["errors"], [])
        self.assertEqual(len(parsed["records"]), 1)
        record = parsed["records"][0]
        self.assertEqual(record["status"], "in_transit")
        self.assertEqual(record["event_at"], "2026-10-02T09:08:00-04:00")
        self.assertEqual(record["event_text"], "Arrived at FedEx location")
        self.assertEqual(record["location"], "HOMESTEAD, FL")
        self.assertEqual(record["transit_days"], 21)

    def test_unknown_zone_keeps_raw_time_and_never_fabricates_offset(self):
        parsed = tracking_import.parse_summary(
            "SYNTHETIC303\t美国\t美国\t2026-10-02 09:08 Somewhere, XX, US Arrived at facility\t运输途中\t(1 day)"
        )
        record = parsed["records"][0]
        self.assertEqual(record["event_at"], None)
        self.assertEqual(record["event_at_raw"], "2026-10-02 09:08")
        self.assertTrue(record["timezone_unknown"])
        self.assertEqual(record["status"], "in_transit")
        self.assertEqual(record["transit_days"], 1)

    def test_not_found_is_not_a_delivery_delay_and_unknown_track_is_represented(self):
        parsed = tracking_import.parse_summary(
            "SYNTHETIC404\t\t\t\t查询不到\t"
        )
        self.assertEqual(parsed["errors"], [])
        self.assertEqual(parsed["records"][0]["status"], "not_found")
        self.assertIsNone(parsed["records"][0]["event_at"])
        self.assertFalse(parsed["records"][0]["timezone_unknown"])

    def test_malformed_rows_and_invalid_tracking_numbers_do_not_block_good_rows(self):
        parsed = tracking_import.parse_summary(
            "bad row\nTINY\tUS\tUS\t2026-01-01 12:00 New York, NY, US Delivered\t成功签收\n"
            "SYNTHETIC505\tUS\tUS\t2026-01-01 12:00 New York, NY, US Delivered\t成功签收\n"
            "-----\nPowered by www.17track.net"
        )
        self.assertEqual(len(parsed["records"]), 1)
        self.assertEqual(parsed["records"][0]["status"], "delivered")
        self.assertEqual(len(parsed["errors"]), 2)

    def test_dst_transition_local_times_are_not_guessed(self):
        spring = tracking_import.parse_summary(
            "SYNTHETIC606\tUS\tUS\t2026-03-08 02:30 Merrillville, IN, US In transit\t运输途中"
        )["records"][0]
        fall = tracking_import.parse_summary(
            "SYNTHETIC607\tUS\tUS\t2026-11-01 01:30 Homestead, FL, US In transit\t运输途中"
        )["records"][0]
        for record in (spring, fall):
            self.assertIsNone(record["event_at"])
            self.assertTrue(record["timezone_unknown"])
            self.assertIsNotNone(record["event_at_raw"])


if __name__ == "__main__":
    unittest.main()
