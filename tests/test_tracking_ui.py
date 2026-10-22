"""17TRACK helper UI regression; all browser traffic is intercepted by local fixtures."""
import json
import os
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlsplit


@unittest.skipUnless(os.environ.get("TRACKING_BROWSER_TESTS") == "1", "可选本地 Chromium 17TRACK 辅助交互测试")
class TrackingUITests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _html():
        return (Path(__file__).resolve().parents[1] / "web_index.html").read_text(encoding="utf-8")

    @staticmethod
    def _response(data, status=200):
        return {"status": status, "content_type": "application/json", "body": json.dumps(data)}

    def _fake_api(self, html, state, calls, external_requests):
        async def server(route):
            request = route.request
            parsed = urlsplit(request.url)
            if parsed.netloc != "tracking.test":
                external_requests.append(request.url)
                await route.abort()
                return
            path, method = parsed.path, request.method
            calls.append((path, method))
            payload = request.post_data_json if method in ("POST", "PATCH") else {}
            payload = payload or {}
            if path == "/":
                await route.fulfill(content_type="text/html", body=html)
            elif path == "/favicon.ico":
                await route.fulfill(status=204, body="")
            elif path == "/api/tracking/orders" and method == "GET":
                await route.fulfill(**self._response({
                    "items": state["orders"], "max_orders": state.get("max_orders", 500), "batch_size": 40,
                }))
            elif path == "/api/tracking/orders" and method == "POST":
                state["pairs_payload"] = payload
                added, duplicates = [], 0
                for pair in payload.get("pairs", []):
                    if any(str(item.get("order_id")) == pair["order_id"] for item in state["orders"]):
                        duplicates += 1
                        continue
                    item = {
                        "id": len(state["orders"]) + 1, "order_id": pair["order_id"],
                        "tracking_number": pair["tracking_number"], "enabled": True,
                        "status": "pending", "status_text": "待查询", "last_event_at": None,
                        "last_event_at_raw": None, "last_event_text": "", "last_location": "",
                        "last_checked_at": None, "last_error": "", "timezone_unknown": False,
                        "stale_hours": None, "risk": "pending", "order_closed": False,
                    }
                    state["orders"].append(item)
                    added.append(item)
                await route.fulfill(**self._response({"added": len(added), "duplicates": duplicates, "items": added}))
            elif path == "/api/tracking/next-batch":
                state.setdefault("batch_queries", []).append(parse_qs(parsed.query).get("limit"))
                await route.fulfill(**self._response({"items": state.get("batch", []), "total": state.get("batch_total", 0)}))
            elif path == "/api/tracking/import-summary" and method == "POST":
                state["summary_payload"] = payload
                if "batch_after_import" in state:
                    state["batch"] = state["batch_after_import"]
                    state["batch_total"] = len(state["batch"])
                await route.fulfill(**self._response(state.get("summary_result", {
                    "matched": 0, "unmatched": [], "errors": [], "items": [],
                })))
            elif path == "/api/tracking/orders/1/events":
                await route.fulfill(**self._response({"items": state.get("events", [])}))
            elif path == "/api/tracking/fedex-email/reviews":
                items = state.get("feta_reviews", [])
                await route.fulfill(**self._response({"items": items, "total": len(items)}))
            elif path == "/api/tracking/fedex-email/sync" and method == "POST":
                state.setdefault("feta_sync_calls", []).append(1)
                await route.fulfill(**self._response({"processed": 0, "events_saved": 0}))
            elif path.startswith("/api/tracking/fedex-email/events/") and method == "POST":
                state.setdefault("feta_review_payloads", []).append((path, payload))
                state["feta_reviews"] = []
                await route.fulfill(**self._response({"review": {"review_state": payload.get("decision")},
                    "integration": {"matched_orders": 1, "status_updated": 1, "duplicate": False}}))
            elif path == "/api/amazon/groups":
                await route.fulfill(**self._response({
                    "items": [{"id": 1, "name": "默认分组", "count": 0}],
                    "default_group_id": 1, "running_job_id": None,
                }))
            elif path == "/api/amazon/watch":
                gid = int(parse_qs(parsed.query).get("group_id", ["1"])[0])
                await route.fulfill(**self._response({
                    "group_id": gid, "group_name": "默认分组", "items": [], "max_asins": 20,
                    "running_job_id": None,
                }))
            elif path == "/api/amazon/changes":
                await route.fulfill(**self._response({"items": []}))
            elif path == "/api/accounts":
                await route.fulfill(**self._response({
                    "main": {"logged_in": False}, "member": {"logged_in": False},
                    "lookup_cfg": False, "db": {},
                }))
            elif path == "/api/pool":
                await route.fulfill(**self._response({"slots": [], "next_slot": 1}))
            elif path == "/api/watchlist":
                await route.fulfill(**self._response({
                    "items": [], "count": 0, "own": 0, "blocked": {}, "bsr_ts": {},
                }))
            elif path == "/api/bsr/sources":
                await route.fulfill(**self._response({"items": []}))
            elif path == "/api/routine/plan":
                await route.fulfill(**self._response({
                    "weekday": "Mon", "date": "2026-06-01", "plan": [], "cl_seed": None,
                }))
            else:
                await route.abort()
        return server

    async def _open_page(self, server, url="http://tracking.test/#track17"):
        from playwright.async_api import async_playwright

        self.playwright = await async_playwright().start()
        self.addAsyncCleanup(self.playwright.stop)
        launch_options = {"headless": True}
        executable = os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE")
        if executable:
            launch_options["executable_path"] = executable
        self.browser = await self.playwright.chromium.launch(**launch_options)
        self.addAsyncCleanup(self.browser.close)
        page = await self.browser.new_page(viewport={"width": 1440, "height": 1100})
        page.set_default_timeout(7000)
        self.page_errors = []
        page.on("pageerror", lambda error: self.page_errors.append(str(error)))
        await page.add_init_script("""
            window.__trackingClipboard = '';
            Object.defineProperty(navigator, 'clipboard', {
              configurable: true,
              value: { writeText: async text => { window.__trackingClipboard = text; } }
            });
        """)
        await page.route("**/*", server)
        await page.goto(url)
        return page

    async def test_three_tabs_default_hash_and_no_external_requests(self):
        calls, external_requests = [], []
        state = {"orders": [], "batch": [], "batch_total": 0}
        server = self._fake_api(self._html(), state, calls, external_requests)
        page = await self._open_page(server, "http://tracking.test/")

        await page.wait_for_function("document.querySelector('#routine-plan').textContent.includes('计划：')")
        self.assertEqual(await page.evaluate("window.location.hash"), "")
        self.assertFalse(await page.locator("#amzPanelSellersprite").is_hidden())
        self.assertTrue(await page.locator("#amzPanelAmazon").is_hidden())
        self.assertTrue(await page.locator("#amzPanelTracking").is_hidden())
        self.assertEqual(await page.locator("#amzTabSellersprite").get_attribute("aria-selected"), "true")

        await page.locator("#amzTabAmazon").click()
        await page.wait_for_function("document.querySelector('#amzCurrentBody').textContent.includes('当前组名单为空')")
        self.assertFalse(await page.locator("#amzPanelAmazon").is_hidden())
        self.assertTrue(await page.locator("#amzPanelSellersprite").is_hidden())
        self.assertTrue(await page.locator("#amzPanelTracking").is_hidden())

        await page.locator("#amzTabTracking").click()
        await page.wait_for_function("document.querySelector('#trackingBatchMeta').textContent.includes('本批 0 个唯一运单')")
        self.assertFalse(await page.locator("#amzPanelTracking").is_hidden())
        self.assertTrue(await page.locator("#amzPanelSellersprite").is_hidden())
        self.assertTrue(await page.locator("#amzPanelAmazon").is_hidden())
        tracking_calls = [path for path, _ in calls if path.startswith("/api/tracking/")]
        self.assertTrue(tracking_calls)
        self.assertTrue(all(path == "/" or path.startswith("/api/tracking/") for path, _ in calls[-len(tracking_calls):]))
        self.assertTrue(any(path.startswith("/api/amazon/") for path, _ in calls))
        self.assertTrue(any(path == "/api/accounts" for path, _ in calls))
        self.assertEqual(external_requests, [])
        self.assertEqual(self.page_errors, [])

    async def test_tsv_registration_and_copy_at_most_40_distinct_numbers(self):
        calls, external_requests = [], []
        batch = [
            {"tracking_number": f"BATCH-{index:02d}", "order_ids": [f"ORDER-{index:02d}"]}
            for index in range(43)
        ]
        batch.append({"tracking_number": "BATCH-00", "order_ids": ["ORDER-ALIAS"]})
        state = {"orders": [], "batch": batch, "batch_total": 43, "max_orders": 2}
        server = self._fake_api(self._html(), state, calls, external_requests)
        page = await self._open_page(server)
        await page.wait_for_function("document.querySelector('#trackingBatchList').textContent.includes('BATCH-00')")

        # Count pair rows against MAX_ORDERS, even when those rows repeat the same order ID.
        await page.locator("#trackingPairs").fill("ORDER-SAME\tTRACK-A\nORDER-SAME\tTRACK-B\nORDER-SAME\tTRACK-C")
        self.assertIn("超出本地保存上限", await page.locator("#trackingPairsMeta").inner_text())
        self.assertTrue(await page.locator("#trackingAddBtn").is_disabled())
        self.assertIsNone(state.get("pairs_payload"))

        await page.locator("#trackingPairs").fill("ORDER-NEW\tTRACK-NEW\nORDER-NEW-2\tTRACK-NEW-2")
        self.assertIn("有效 2 行", await page.locator("#trackingPairsMeta").inner_text())
        await page.locator("#trackingAddBtn").click()
        await page.wait_for_function("document.querySelector('#trackingOrdersBody').textContent.includes('ORDER-NEW-2')")
        self.assertEqual(state["pairs_payload"], {"pairs": [
            {"order_id": "ORDER-NEW", "tracking_number": "TRACK-NEW"},
            {"order_id": "ORDER-NEW-2", "tracking_number": "TRACK-NEW-2"},
        ]})

        await page.locator("#trackingCopyBtn").click()
        await page.wait_for_function("document.querySelector('#trackingBatchMeta').textContent.includes('已复制 40')")
        copied_lines = (await page.evaluate("window.__trackingClipboard")).splitlines()
        self.assertEqual(len(copied_lines), 40)
        self.assertEqual(len(set(copied_lines)), 40)
        self.assertNotIn("BATCH-40", copied_lines)
        self.assertEqual(state["batch_queries"][-1], ["40"])
        self.assertEqual(external_requests, [])
        self.assertEqual(self.page_errors, [])

    async def test_summary_import_risk_closure_safe_text_and_external_link(self):
        calls, external_requests = [], []
        malicious = '<img src=x onerror="window.trackInjected=true">'
        orders = [
            {
                "id": 1, "order_id": "ORDER-48", "tracking_number": "TRACK-48", "enabled": True,
                "status": "in_transit", "status_text": "旧状态文本", "pkg_status_text": "运输中",
                "last_event_at": "2026-06-01T08:00:00+00:00", "last_event_at_raw": "2026/06/01 08:00",
                "last_event_text": "Arrived at facility", "last_location": "Los Angeles Hub",
                "last_checked_at": "2026-06-01T08:01:00+00:00",
                "last_error": "", "transit_days": 3, "timezone_unknown": False,
                "stale_hours": 49, "risk": "stalled", "order_closed": False,
            },
            {
                "id": 2, "order_id": "ORDER-DONE", "tracking_number": "TRACK-DONE", "enabled": True,
                "status": "delivered", "status_text": "Delivered", "pkg_status_text": "Delivered",
                "last_event_at": "2026-06-01T08:00:00+00:00", "last_event_at_raw": "2026-06-01 08:00",
                "last_event_text": "Delivered",
                "last_location": "Recipient", "last_checked_at": "2026-06-01T08:01:00+00:00",
                "last_error": "", "transit_days": 2, "timezone_unknown": False,
                "stale_hours": 80, "risk": "delivered", "order_closed": True,
            },
            {
                "id": 3, "order_id": malicious, "tracking_number": "TRACK-XSS", "enabled": True,
                "status": "In Transit", "status_text": "运输中", "last_event_at": None,
                "last_event_at_raw": None, "last_event_text": malicious, "last_location": "Hub",
                "last_checked_at": "2026-06-01T08:01:00+00:00", "last_error": malicious,
                "transit_days": 1, "timezone_unknown": False, "stale_hours": None,
                "risk": "normal", "order_closed": False,
            },
            {
                "id": 4, "order_id": "ORDER-TZ", "tracking_number": "TRACK-TZ", "enabled": True,
                "status": "In Transit", "status_text": "运输中", "last_event_at": None,
                "last_event_at_raw": "2026-06-01 08:00", "last_event_text": "Scan time lacks timezone",
                "last_location": "Unknown timezone hub", "last_checked_at": "2026-06-01T08:01:00+00:00",
                "last_error": "", "transit_days": 2, "timezone_unknown": True,
                "stale_hours": None, "risk": "时区待确认", "order_closed": False,
            },
            {
                "id": 5, "order_id": "ORDER-AWAIT", "tracking_number": "TRACK-AWAIT", "enabled": True,
                "status": "awaiting_event", "status_text": "等待首个扫描", "last_event_at": None,
                "last_event_at_raw": None, "last_event_text": "", "last_location": "",
                "last_checked_at": "2026-06-01T08:01:00+00:00", "last_error": "",
                "transit_days": 4, "timezone_unknown": False, "stale_hours": 72,
                "risk": "awaiting_event", "order_closed": False,
            },
        ]
        state = {
            "orders": orders,
            "batch": [{"tracking_number": "TRACK-OLD-BATCH", "order_ids": ["ORDER-OLD"]}],
            "batch_after_import": [{"tracking_number": "TRACK-NEW-BATCH", "order_ids": ["ORDER-48"]}],
            "batch_total": 1,
            "summary_result": {"matched": 1, "unmatched": 1, "errors": [malicious], "items": [
                orders[0], {"matched": False, "tracking_number": malicious, "order_id": "ORDER-UNMATCHED"},
            ]},
            "events": [{
                "event_at": "2026-06-01T08:00:00+00:00", "event_at_raw": "2026/06/01 08:00",
                "event_text": "Parsed scan description", "raw_event": "Original carrier scan",
                "location": "Los Angeles Hub", "status": "in_transit", "pkg_status_text": "运输中",
                "imported_at": "2026-06-01T08:01:00+00:00",
            }],
        }
        server = self._fake_api(self._html(), state, calls, external_requests)
        page = await self._open_page(server)
        await page.wait_for_function("document.querySelector('#trackingOrdersBody').textContent.includes('ORDER-48')")
        await page.wait_for_function("document.querySelector('#trackingBatchList').textContent.includes('TRACK-OLD-BATCH')")

        copied_summary = "Manual 17TRACK result\n" + malicious
        await page.locator("#trackingSummary").fill(copied_summary)
        await page.locator("#trackingImportBtn").click()
        await page.wait_for_function("document.querySelector('#trackingImportResult').textContent.includes('已匹配 1')")
        await page.wait_for_function("document.querySelector('#trackingBatchList').textContent.includes('TRACK-NEW-BATCH')")
        self.assertNotIn("TRACK-OLD-BATCH", await page.locator("#trackingBatchList").inner_text())
        self.assertEqual(state["summary_payload"], {"text": copied_summary})
        self.assertEqual(await page.locator("#trackingSummary").input_value(), copied_summary)
        self.assertIn("未匹配 1", await page.locator("#trackingImportResult").inner_text())
        details = await page.locator("#trackingImportDetails").inner_text()
        self.assertIn("ORDER-UNMATCHED", details)
        self.assertIn(malicious, details)
        self.assertEqual(await page.locator("#amzPanelTracking img, #amzPanelTracking script, #amzPanelTracking svg").count(), 0)
        self.assertFalse(await page.evaluate("Boolean(window.trackInjected)"))

        stalled_row = page.locator("#trackingOrdersBody tr").filter(has_text="ORDER-48")
        stalled_text = await stalled_row.inner_text()
        self.assertIn("48h+ 无更新预警", stalled_text)
        self.assertNotIn("时区待确认", stalled_text)
        self.assertIn("Los Angeles Hub", stalled_text)
        self.assertIn("包裹：运输中", stalled_text)
        self.assertNotIn("旧状态文本", stalled_text)
        timezone_row = page.locator("#trackingOrdersBody tr").filter(has_text="ORDER-TZ")
        timezone_text = await timezone_row.inner_text()
        self.assertIn("时区待确认", timezone_text)
        self.assertNotIn("48h+ 无更新预警", timezone_text)
        awaiting_row = page.locator("#trackingOrdersBody tr").filter(has_text="ORDER-AWAIT")
        awaiting_text = await awaiting_row.inner_text()
        self.assertIn("等待首个扫描", awaiting_text)
        self.assertIn("72h", awaiting_text)
        self.assertNotIn("48h+ 无更新预警", awaiting_text)
        delivered_row = page.locator("#trackingOrdersBody tr").filter(has_text="ORDER-DONE")
        self.assertIn("Delivered", await delivered_row.inner_text())
        self.assertIn("订单：已结单", await delivered_row.inner_text())
        self.assertNotIn("48h+ 无更新预警", await delivered_row.inner_text())
        unsafe_row = page.locator("#trackingOrdersBody tr").filter(has_text="TRACK-XSS")
        self.assertIn(malicious, await unsafe_row.inner_text())

        await stalled_row.get_by_role("button", name="事件").click()
        await page.wait_for_function("!document.querySelector('#trackingEventsPanel').hidden")
        event_detail = await page.locator("#trackingEventsList").inner_text()
        self.assertIn("2026/06/01 08:00", event_detail)
        self.assertIn("Parsed scan description", event_detail)
        self.assertIn("Original carrier scan", event_detail)
        self.assertIn("Los Angeles Hub", event_detail)
        self.assertIn("运输中", event_detail)

        state["summary_result"] = {"matched": 1, "unmatched": 0, "errors": [], "items": [
            {"matched": True, "tracking_number": "TRACK-48"},
        ]}
        successful_summary = "All tracking numbers matched"
        await page.locator("#trackingSummary").fill(successful_summary)
        await page.locator("#trackingImportBtn").click()
        await page.wait_for_function("document.querySelector('#trackingSummary').value === ''")
        self.assertEqual(state["summary_payload"], {"text": successful_summary})

        official_link = page.locator("#trackingOfficialLink")
        self.assertEqual(await official_link.get_attribute("href"), "https://www.17track.net/zh-cn")
        self.assertEqual(await official_link.get_attribute("target"), "_blank")
        rel = (await official_link.get_attribute("rel")).split()
        self.assertIn("noopener", rel)
        self.assertIn("noreferrer", rel)
        self.assertEqual(external_requests, [])
        self.assertTrue(any(path == "/api/tracking/import-summary" for path, _ in calls))
        self.assertEqual(self.page_errors, [])


if __name__ == "__main__":
    unittest.main()
