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


class ErpHeaderAndXlsxTests(unittest.TestCase):
    def test_header_alias_mapping_csv_quotes_extra_columns_and_casefold_dedup(self):
        first = tracking_import.parse_pairs('平台参考号,运单号,状态\n"ORDER, WITH COMMA",420123456789012345678901234567,已发货')
        self.assertEqual(first["errors"], [])
        self.assertEqual(first["pairs"][0]["order_id"], "ORDER, WITH COMMA")
        self.assertEqual(first["pairs"][0]["tracking_number"], "420123456789012345678901234567")
        second = tracking_import.parse_pairs('platform_order_id,tracking_number\nORDER-2,TRACKING002')
        self.assertEqual(second["errors"], [])
        self.assertEqual(second["pairs"], [{"order_id": "ORDER-2", "tracking_number": "TRACKING002"}])

    def test_ambiguous_headers_are_reported_not_saved_as_orders(self):
        parsed = tracking_import.parse_pairs("order_id,order_id,tracking_number\nORDER-1,ORDER-1,TRACKING001")
        self.assertEqual(parsed["pairs"], [])
        self.assertIn("表头映射不明确", parsed["errors"][0]["error"])

    def test_xlsx_actual_coordinates_and_lazy_xml_errors_fail_closed(self):
        import io, re, zipfile
        from openpyxl import Workbook
        def build(setup):
            workbook = Workbook(); setup(workbook.active)
            output = io.BytesIO(); workbook.save(output); workbook.close(); return output.getvalue()
        def edit_sheet(data, transform):
            output = io.BytesIO()
            with zipfile.ZipFile(io.BytesIO(data)) as source, zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as dest:
                for member in source.infolist():
                    body = source.read(member.filename)
                    if member.filename == 'xl/worksheets/sheet1.xml': body = transform(body.decode('utf-8')).encode('utf-8')
                    dest.writestr(member.filename, body)
            return output.getvalue()
        far_row = build(lambda sheet: (sheet.append(['order_id','tracking_number']),
            sheet.cell(tracking_import.MAX_XLSX_ROWS + 2, 1, 'SYNTHETIC-ORDER'),
            sheet.cell(tracking_import.MAX_XLSX_ROWS + 2, 2, 'TRACK00001')))
        far_row = edit_sheet(far_row, lambda xml: re.sub(r'<dimension\b[^>]*/>', '', xml))
        row_result = tracking_import.parse_xlsx(far_row)
        self.assertEqual(row_result['pairs'], [])
        self.assertTrue(row_result['errors'])
        far_column = build(lambda sheet: (sheet.append(['order_id','tracking_number']), sheet.cell(1, tracking_import.MAX_XLSX_COLUMNS + 1, 'extra')))
        far_column = edit_sheet(far_column, lambda xml: re.sub(r'<dimension\b[^>]*/>', '', xml))
        column_result = tracking_import.parse_xlsx(far_column)
        self.assertEqual(column_result['pairs'], [])
        self.assertTrue(column_result['errors'])
        broken = build(lambda sheet: (sheet.append(['order_id','tracking_number']), sheet.append(['ORDER-1','TRACK00001'])))
        broken = edit_sheet(broken, lambda xml: xml[:xml.index('</sheetData>')])
        broken_result = tracking_import.parse_xlsx(broken)
        self.assertEqual(broken_result['pairs'], [])
        self.assertTrue(broken_result['errors'])

    def test_xlsx_preserves_text_values_and_rejects_numeric_cells(self):
        import io
        from openpyxl import Workbook
        workbook = Workbook()
        sheet = workbook.active
        sheet.append(["平台参考号", "跟踪号", "状态"])
        sheet.append(["ORDER-LEADING", "001234567890", "ok"])
        sheet.append(["ORDER-NUMERIC", 4201234567890123456, "unsafe"])
        sheet.append(["ORDER-EXT", "420123456789012345678901234567", "ok"])
        output = io.BytesIO(); workbook.save(output); workbook.close()
        parsed = tracking_import.parse_xlsx(output.getvalue())
        self.assertEqual(parsed["sheet_name"], "Sheet")
        self.assertEqual([p["tracking_number"] for p in parsed["pairs"]],
                         ["001234567890", "420123456789012345678901234567"])
        self.assertTrue(any("必须保存为文本" in error["error"] for error in parsed["errors"]))


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

    def test_unknown_zone_defaults_to_us_central_time(self):
        parsed = tracking_import.parse_summary(
            "SYNTHETIC303\t美国\t美国\t2026-10-02 09:08 Somewhere, XX, US Arrived at facility\t运输途中\t(1 day)"
        )
        record = parsed["records"][0]
        self.assertEqual(record["event_at"], "2026-10-02T09:08:00-05:00")
        self.assertEqual(record["event_at_raw"], "2026-10-02 09:08")
        self.assertFalse(record["timezone_unknown"])
        self.assertEqual(record["status"], "in_transit")
        self.assertEqual(record["transit_days"], 1)

    def test_missing_location_and_ambiguous_unknown_location_use_central_time(self):
        missing = tracking_import.parse_summary(
            "SYNTHETIC304\tUS\tUS\t2026-01-05 08:30 Arrived at facility\t运输途中"
        )["records"][0]
        self.assertEqual(missing["event_at"], "2026-01-05T08:30:00-06:00")
        self.assertFalse(missing["timezone_unknown"])
        ambiguous = tracking_import.parse_summary(
            "SYNTHETIC305\tUS\tUS\t2026-11-01 01:30 Somewhere, XX, US In transit\t运输途中"
        )["records"][0]
        self.assertEqual(ambiguous["event_at"], "2026-11-01T01:30:00-05:00")
        self.assertFalse(ambiguous["timezone_unknown"])

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

    def test_dst_transition_local_times_default_to_us_central(self):
        spring = tracking_import.parse_summary(
            "SYNTHETIC606\tUS\tUS\t2026-03-08 02:30 Merrillville, IN, US In transit\t运输途中"
        )["records"][0]
        fall = tracking_import.parse_summary(
            "SYNTHETIC607\tUS\tUS\t2026-11-01 01:30 Homestead, FL, US In transit\t运输途中"
        )["records"][0]
        self.assertEqual(spring["event_at"], "2026-03-08T02:30:00-06:00")
        self.assertEqual(fall["event_at"], "2026-11-01T01:30:00-05:00")
        for record in (spring, fall):
            self.assertFalse(record["timezone_unknown"])
            self.assertIsNotNone(record["event_at_raw"])


if __name__ == "__main__":
    unittest.main()
