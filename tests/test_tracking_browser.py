"""17TRACK session handoff tests with a synthetic visible-page fixture only."""
import unittest

import tracking_browser


SUMMARY_ROW = "SYNTHETIC001\tUS\tUS\t2026-10-21 10:00 New York, NY, US Delivered\tDelivered\t(1 day)"


class FakeLocator:
    def __init__(self, page, selector):
        self.page, self.selector = page, selector

    async def count(self):
        if self.selector == "body": return 1
        if self.selector == "textarea": return 1
        if self.selector == "button:has-text('Track')": return 1
        if self.selector == "table tbody tr": return 1
        return 0

    def nth(self, index): return self
    async def is_visible(self): return True
    async def fill(self, value, timeout=None): self.page.filled = value
    async def click(self, timeout=None): self.page.clicked = True
    async def inner_text(self, timeout=None):
        return self.page.body if self.selector == "body" else SUMMARY_ROW


class FakePage:
    def __init__(self):
        self.filled = ""; self.clicked = False; self.body = "Results complete"; self.closed = False
        self.navigated = None
    def locator(self, selector): return FakeLocator(self, selector)
    async def route(self, pattern, handler): self.route_handler = handler
    async def goto(self, url, **kwargs): self.navigated = url
    def is_closed(self): return self.closed


class FakeContext:
    def __init__(self, page): self.page = page; self.closed = False
    async def new_page(self): return self.page
    async def close(self): self.closed = True


class FakeBrowser:
    def __init__(self, context): self.context = context; self.closed = False
    async def new_context(self, **kwargs): return self.context
    async def close(self): self.closed = True


class FakeChromium:
    def __init__(self, browser): self.browser = browser
    async def launch(self, **kwargs):
        assert kwargs == {"headless": False}
        return self.browser


class FakePlaywright:
    def __init__(self, chromium): self.chromium = chromium; self.stopped = False
    async def stop(self): self.stopped = True


class FakePlaywrightFactory:
    def __init__(self, playwright): self.playwright = playwright
    async def start(self): return self.playwright


class TrackingBrowserTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.page = FakePage()
        self.context = FakeContext(self.page)
        self.browser = FakeBrowser(self.context)
        self.pw = FakePlaywright(FakeChromium(self.browser))
        self.imported = []
        self.session = tracking_browser.TrackingQuerySession(
            importer=lambda records: self.imported.extend(records) or {"matched": len(records), "unmatched": []},
            playwright_factory=lambda: FakePlaywrightFactory(self.pw),
            site_url="https://www.17track.net/zh-cn")

    async def test_user_click_opens_visible_window_sends_only_tracks_and_imports_requested_result(self):
        started = await self.session.start(["SYNTHETIC001", "synthetic001"])
        self.assertEqual(started["status"], "waiting_user")
        self.assertTrue(self.page.clicked)
        self.assertEqual(self.page.filled, "SYNTHETIC001")
        self.assertEqual(self.page.navigated, tracking_browser.TRACKING_URL)
        self.assertNotIn("ORDER", self.page.filled)
        result = await self.session.continue_reading()
        self.assertEqual(result["status"], "done")
        self.assertEqual(result["result"]["matched"], 1)
        self.assertEqual(self.imported[0]["tracking_number"], "SYNTHETIC001")
        self.assertTrue(self.context.closed)
        self.assertTrue(self.browser.closed)
        self.assertTrue(self.pw.stopped)

    async def test_human_verification_pauses_without_automation_then_allows_continue(self):
        await self.session.start(["SYNTHETIC001"])
        self.page.body = "请完成人机验证码后继续"
        pending = await self.session.continue_reading()
        self.assertEqual(pending["status"], "waiting_user")
        self.assertTrue(pending["needs_human"])
        self.assertEqual(self.imported, [])
        self.page.body = "Results complete"
        result = await self.session.continue_reading()
        self.assertEqual(result["status"], "done")

    async def test_invalid_tracks_and_concurrent_sessions_fail_closed(self):
        with self.assertRaises(tracking_browser.QueryError):
            await self.session.start(["ORDER-SECRET"])
        await self.session.start(["SYNTHETIC001"])
        with self.assertRaises(tracking_browser.SessionBusy):
            await self.session.start(["SYNTHETIC002"])
        cancelled = await self.session.cancel()
        self.assertEqual(cancelled["status"], "cancelled")


if __name__ == "__main__":
    unittest.main()
