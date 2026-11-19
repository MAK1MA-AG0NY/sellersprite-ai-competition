"""Synthetic-only USPS/Track718 parsing and browser-flow tests; no live lookups."""
import unittest

import tracking_import
import usps_browser


TRACK = "SYNTHETIC001"
USPS_PAGE_TEXT = """USPS Tracking
Tracking Number:
SYNTHETIC001
Latest Update
Your item was delivered to an individual at the address.
Delivered, Left with Individual
August 8, 2026 at 3:35 pm
MIAMI, FL 33146
Tracking History
August 8, 2026 at 3:35 pm
Delivered, Left with Individual
MIAMI, FL 33146
"""
TRACK718_TRACK = "SYNTHETIC718001"
TRACK718_PAGE_TEXT = """TRACK YOUR PACKAGE WITH TRACK718
全部 (1)
查不到 (0)
暂未上线 (0)
运输途中 (0)
到达待取 (1)
派送途中 (0)
SYNTHETIC718001
到达待取
9.1 天
美国
美国邮政
美国
2026-10-18 00:00:00 Delivery Attempt: Action Needed, Reminder to Schedule Redelivery
轨迹翻译
2026-10-13 15:54:00
LAKE MARY, FL 32746, Notice Left (No Authorized Recipient Available)
"""
TRACK718_COPIED_TEXT = """物流单号: SYNTHETIC718001
包裹状态: 到达待取
目的国: 美国
发件国: 美国
2026-10-18 00:00:00  Delivery Attempt: Action Needed, Reminder to Schedule Redelivery of your item before October 27, 2026 -> This is a reminder to arrange for redelivery.
2026-10-13 15:54:00  LAKE MARY, FL 32746, Notice Left (No Authorized Recipient Available)
2026-10-13 06:10:00  LAKE MARY, FL 32746, Out for Delivery
==============================
物流单号: SYNTHETIC718UNREQUESTED
包裹状态: 查询不到
目的国: Unkown
发件国: Unkown
==============================
物流单号: SYNTHETIC718DELIVERED
包裹状态: 签收成功(4.3天)
目的国: 美国
发件国: 美国
2026-09-08 13:01:00  WILLIS, TX 77318, Delivered, Left with Individual -> Your item was delivered.
==============================
物流单号: SYNTHETIC718NOTFOUND
包裹状态: 查询不到
目的国: Unkown
发件国: Unkown
==============================
"""


class USPSParserTests(unittest.TestCase):
    def test_parses_requested_delivered_result_and_event(self):
        parsed = tracking_import.parse_usps_summary(USPS_PAGE_TEXT, [TRACK])
        self.assertEqual(len(parsed["records"]), 1)
        record = parsed["records"][0]
        self.assertEqual(record["tracking_number"], TRACK)
        self.assertEqual(record["status"], "delivered")
        self.assertEqual(record["location"], "MIAMI, FL")
        self.assertIsNotNone(record["event_at"])

    def test_unrequested_result_card_is_not_imported(self):
        text = USPS_PAGE_TEXT.replace(TRACK, "SYNTHETIC002")
        parsed = tracking_import.parse_usps_summary(text, [TRACK])
        self.assertEqual(parsed["records"], [])
        self.assertTrue(parsed["errors"])

    def test_unknown_current_status_is_not_marked_checked(self):
        text = USPS_PAGE_TEXT.replace(
            "Your item was delivered to an individual at the address.", "A page section without a recognized status."
        ).replace("Delivered, Left with Individual", "A page section without a recognized status.")
        parsed = tracking_import.parse_usps_summary(text, [TRACK])
        self.assertEqual(parsed["records"], [])
        self.assertTrue(parsed["errors"])

    def test_rejects_more_than_35_unique_numbers(self):
        numbers = [f"SYNTHETIC{index:03d}" for index in range(36)]
        parsed = tracking_import.parse_usps_summary("", numbers)
        self.assertEqual(parsed["records"], [])
        self.assertIn("35", parsed["errors"][0]["error"])


class Track718ParserTests(unittest.TestCase):
    def test_parses_track718_status_summary_and_synthetic_event(self):
        parsed = tracking_import.parse_track718_summary(TRACK718_PAGE_TEXT, [TRACK718_TRACK])
        self.assertEqual(len(parsed["records"]), 1)
        record = parsed["records"][0]
        self.assertEqual(record["tracking_number"], TRACK718_TRACK)
        self.assertEqual(record["status"], "awaiting_pickup")
        self.assertIn("Delivery Attempt", record["event_text"])
        self.assertIsNotNone(record["event_at"])

    def test_parses_track718_copied_labeled_format_and_multiple_scan_events(self):
        expected = [TRACK718_TRACK, "SYNTHETIC718DELIVERED", "SYNTHETIC718NOTFOUND"]
        parsed = tracking_import.parse_track718_summary(TRACK718_COPIED_TEXT, expected)
        self.assertEqual(len(parsed["records"]), 3)
        records = {record["tracking_number"]: record for record in parsed["records"]}
        waiting = records[TRACK718_TRACK]
        self.assertEqual(waiting["status"], "awaiting_pickup")
        self.assertEqual(waiting["source_country"], "美国")
        self.assertEqual(waiting["destination_country"], "美国")
        self.assertEqual(len(waiting["events"]), 3)
        self.assertIn("Delivery Attempt", waiting["events"][0]["event_text"])
        self.assertEqual(waiting["events"][0]["status"], "exception")
        self.assertEqual(waiting["events"][1]["location"], "LAKE MARY, FL")
        self.assertEqual(records["SYNTHETIC718DELIVERED"]["status"], "delivered")
        self.assertEqual(records["SYNTHETIC718NOTFOUND"]["status"], "not_found")
        self.assertIsNone(records["SYNTHETIC718NOTFOUND"]["source_country"])
        self.assertNotIn("SYNTHETIC718UNREQUESTED", records)

    def test_rejects_unrequested_track718_card_and_over_40_batch(self):
        text = TRACK718_PAGE_TEXT.replace(TRACK718_TRACK, "SYNTHETIC718002")
        parsed = tracking_import.parse_track718_summary(text, [TRACK718_TRACK])
        self.assertEqual(parsed["records"], [])
        self.assertTrue(parsed["errors"])
        numbers = [f"SYNTHETIC{index:03d}" for index in range(41)]
        over_limit = tracking_import.parse_track718_summary("", numbers)
        self.assertIn("40", over_limit["errors"][0]["error"])


class _FakeLocator:
    def __init__(self, page, selector):
        self.page, self.selector = page, selector

    async def count(self):
        if self.selector == ".track-box .track-number":
            return len(self.page.tracking_tokens)
        return int(self.selector in {"body", "textarea", "input.search-input",
                                     "button[type='submit']", "button:has-text('查询')"})

    def nth(self, index):
        return self

    async def is_visible(self):
        return True

    async def fill(self, value, timeout=None):
        self.page.filled = value

    async def press(self, key, timeout=None):
        if key == "Enter" and self.page.filled:
            self.page.tracking_tokens.append(self.page.filled)
            self.page.filled = ""

    async def all_inner_texts(self):
        return list(self.page.tracking_tokens)

    async def click(self, timeout=None):
        self.page.clicked = True

    async def inner_text(self, timeout=None):
        return self.page.body

    def locator(self, selector):
        return _FakeLocator(self.page, selector)


class _FakePage:
    def __init__(self, body):
        self.body, self.filled, self.clicked = body, "", False
        self.tracking_tokens = []
        self.main_frame = object()
        self.closed = False

    def locator(self, selector):
        return _FakeLocator(self, selector)

    async def route(self, pattern, handler):
        self.route_handler = handler

    async def goto(self, url, **kwargs):
        self.url = url

    async def wait_for_selector(self, selector, **kwargs):
        return None

    async def wait_for_function(self, expression, arg=None, **kwargs):
        if len(self.tracking_tokens) != arg:
            raise AssertionError("expected Track718 tracking token count not reached")

    def is_closed(self):
        return self.closed


class _FakeContext:
    def __init__(self, page):
        self.page = page

    async def new_page(self):
        return self.page

    async def close(self):
        self.page.closed = True


class _FakeBrowser:
    def __init__(self, context):
        self.context = context

    async def new_context(self, **kwargs):
        return self.context

    async def close(self):
        return None


class _FakeChromium:
    def __init__(self, browser):
        self.browser = browser

    async def launch(self, **kwargs):
        return self.browser


class _FakePlaywright:
    def __init__(self, chromium):
        self.chromium = chromium

    async def stop(self):
        return None


class _FakePlaywrightFactory:
    def __init__(self, playwright):
        self.playwright = playwright

    async def start(self):
        return self.playwright


class USPSProxyConfigTests(unittest.TestCase):
    def test_default_flclash_candidates_try_http_then_socks5(self):
        session = usps_browser.USPSQuerySession(proxy_url="")
        self.assertEqual(session._proxy_candidates(), [
            ("http", "http://127.0.0.1:7897"),
            ("socks5", "socks5://127.0.0.1:7897"),
        ])

    def test_explicit_local_http_proxy_tries_socks5_alternative(self):
        session = usps_browser.USPSQuerySession(proxy_url="http://127.0.0.1:7897")
        self.assertEqual(session._proxy_candidates(), [
            ("http", "http://127.0.0.1:7897"),
            ("socks5", "socks5://127.0.0.1:7897"),
        ])

    def test_direct_mode_requires_explicit_opt_out(self):
        session = usps_browser.USPSQuerySession(proxy_url="direct")
        self.assertEqual(session._proxy_candidates(), [("direct", None)])

    def test_detects_usps_tracking_redirect_without_logging_request_data(self):
        session = usps_browser.USPSQuerySession(proxy_url="")
        request = type("Request", (), {"resource_type": "xhr"})()
        response = type("Response", (), {
            "url": "https://tools.usps.com/tracking/", "status": 302, "request": request
        })()
        session._record_response(response)
        self.assertIn("重定向", session._page_issue)

    def test_detects_akamai_processing_interstitial(self):
        session = usps_browser.USPSQuerySession(proxy_url="")
        session._record_interstitial_body(
            "Processing your request. If this page doesn't refresh automatically, resubmit your request."
        )
        self.assertIn("Akamai", session._page_issue)


class USPSBatchLimitTests(unittest.IsolatedAsyncioTestCase):
    async def test_session_rejects_more_than_35_before_opening_browser(self):
        numbers = [f"SYNTHETIC{index:03d}" for index in range(36)]
        session = usps_browser.USPSQuerySession()
        with self.assertRaises(usps_browser.USPSQueryError):
            await session.start(numbers)
        self.assertEqual(session.snapshot()["status"], "idle")

    async def test_playwright_imports_results_automatically_without_continue_click(self):
        page = _FakePage(USPS_PAGE_TEXT)
        context = _FakeContext(page)
        browser = _FakeBrowser(context)
        playwright = _FakePlaywright(_FakeChromium(browser))
        imported = []
        session = usps_browser.USPSQuerySession(
            importer=lambda records: imported.extend(records) or {
                "matched": len(records), "unmatched": 0, "items": []},
            playwright_factory=lambda: _FakePlaywrightFactory(playwright),
            proxy_url="",
        )
        started = await session.start([TRACK])
        self.assertEqual(started["status"], "submitted")
        await session._collector_task
        self.assertTrue(page.clicked)
        self.assertEqual(page.filled, TRACK)
        self.assertEqual(session.snapshot()["status"], "done")
        self.assertEqual(imported[0]["tracking_number"], TRACK)


class Track718BrowserTests(unittest.IsolatedAsyncioTestCase):
    async def test_track718_enforces_40_item_cap_before_browser_launch(self):
        numbers = [f"SYNTHETIC{index:03d}" for index in range(41)]
        session = usps_browser.Track718QuerySession(proxy_url="")
        with self.assertRaises(usps_browser.USPSQueryError):
            await session.start(numbers)
        self.assertEqual(session.snapshot()["status"], "idle")

    async def test_track718_page_flow_auto_imports_without_copying_results(self):
        page = _FakePage(TRACK718_PAGE_TEXT)
        context = _FakeContext(page)
        browser = _FakeBrowser(context)
        playwright = _FakePlaywright(_FakeChromium(browser))
        imported = []
        session = usps_browser.Track718QuerySession(
            importer=lambda records: imported.extend(records) or {
                "matched": len(records), "unmatched": 0, "items": []},
            playwright_factory=lambda: _FakePlaywrightFactory(playwright),
            proxy_url="",
        )
        second_track = "SYNTHETIC718002"
        started = await session.start([TRACK718_TRACK, second_track])
        self.assertEqual(started["provider"], "track718")
        self.assertEqual(started["status"], "submitted")
        await session._collector_task
        self.assertEqual(page.url, usps_browser.TRACK718_TRACKING_URL)
        self.assertEqual(page.tracking_tokens, [TRACK718_TRACK, second_track])
        self.assertEqual(page.filled, "")
        self.assertEqual(session.snapshot()["status"], "done")
        self.assertEqual(imported[0]["status"], "awaiting_pickup")


if __name__ == "__main__":
    unittest.main()
