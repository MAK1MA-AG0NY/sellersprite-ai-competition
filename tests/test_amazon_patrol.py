"""价格巡查离线回归；只使用临时 SQLite，不访问 Amazon/卖家精灵。

AMZ_BROWSER_TESTS=1 可额外运行本地 HTML 选择器测试（需要项目 Chromium）。
"""
import asyncio
import json
import os
import sqlite3
import uuid
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import amazon_patrol as patrol
import local_db

ASIN = "B0HBPT6N7H"
OTHER = "B0H4W51ZMH"


def raw_product(**updates):
    data = {"title": "Example power station", "page_asin": ASIN,
            "price_text": "$299.00", "reference_price_text": "$399.00",
            "price_region_seen": True, "promo_region_seen": True,
            "coupon_text": "Save $20 with coupon", "promotions_text": "Limited time deal",
            "availability_text": "In Stock", "page_title": "Product", "body_excerpt": ""}
    data.update(updates)
    return data


class PriceParserTests(unittest.TestCase):
    def test_asin_list_deduplicates_and_rejects_urls(self):
        self.assertEqual(patrol.parse_asins(f"{ASIN.lower()}, {OTHER}\n{ASIN}"), [ASIN, OTHER])
        self.assertEqual(patrol.parse_asins(""), [])
        for raw in (f"https://www.amazon.com/dp/{ASIN}", [None], "wrong", None):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                patrol.parse_asins(raw)
        with self.assertRaises(ValueError):
            patrol.parse_asins([f"B0TST{i:05d}" for i in range(21)])

    def test_money_is_decimal_and_not_installments_or_other_currency(self):
        self.assertEqual(patrol.usd_price("$1,299.9"), "1299.90")
        self.assertEqual(patrol.usd_price("USD 299.00"), "299.00")
        for text in ("$24.99/month", "$24.99 per month", "Subscribe $199", "CAD $299.00", "€299", "$299.999", "299"):
            with self.subTest(text=text):
                self.assertIsNone(patrol.usd_price(text))

    def test_price_coupon_and_reference_are_independent_no_net_price(self):
        data, note = patrol.normalize_observation(ASIN, raw_product(), patrol.product_url(ASIN))
        self.assertEqual(data["price"], "299.00")
        self.assertEqual(data["reference_price"], "399.00")
        self.assertEqual(data["coupon"], "Save $20 with coupon")
        self.assertEqual(data["availability"], "available")
        self.assertNotIn("net_price", data)
        self.assertFalse(note)

    def test_unknown_coupon_is_not_coupon_cancellation(self):
        data, note = patrol.normalize_observation(
            ASIN, raw_product(coupon_text="", promotions_text=""), patrol.product_url(ASIN))
        self.assertNotIn("coupon", data)
        self.assertNotIn("promotions", data)
        self.assertIn("保留", note)
        confirmed, _ = patrol.normalize_observation(
            ASIN, raw_product(coupon_text="No coupons currently available"), patrol.product_url(ASIN))
        self.assertEqual(confirmed["coupon"], "")

    def test_blocked_mismatched_invalid_pages_cannot_be_saved(self):
        cases = [raw_product(captcha=True), raw_product(page_asin=OTHER),
                 raw_product(title=""), raw_product(price_text=""),
                 raw_product(price_text="$25 per month")]
        for raw in cases:
            with self.subTest(raw=raw), self.assertRaises(patrol.ObservationError):
                patrol.normalize_observation(ASIN, raw, patrol.product_url(ASIN))
        with self.assertRaises(patrol.ObservationError):
            patrol.normalize_observation(ASIN, raw_product(), "https://example.com/product")

    def test_unavailable_is_explicit_and_does_not_invent_a_price(self):
        data, _ = patrol.normalize_observation(
            ASIN, raw_product(price_text="", availability_text="Currently unavailable"), patrol.product_url(ASIN))
        self.assertEqual(data["availability"], "unavailable")
        self.assertNotIn("price", data)
        self.assertNotIn("coupon", data)


def isolated_memory_db():
    """多连接共享的临时内存库，避开 Windows 沙箱临时目录权限，不碰业务文件。"""
    uri = f"file:amz_test_{uuid.uuid4().hex}?mode=memory&cache=shared"
    keeper = sqlite3.connect(uri, uri=True)

    def connect():
        c = sqlite3.connect(uri, uri=True)
        c.executescript(local_db.SCHEMA)
        return c

    return keeper, patch.object(local_db, "_conn", new=connect)


class MemoryDBTests(unittest.TestCase):
    def setUp(self):
        self.keeper, self.db_patch = isolated_memory_db()
        self.db_patch.start()
        local_db.amz_save_watchlist([ASIN])

    def tearDown(self):
        self.db_patch.stop()
        self.keeper.close()

    def current(self):
        return local_db.amz_watchlist()[0]["current"]

    def test_first_baseline_and_identical_checks_do_not_append_history(self):
        data, _ = patrol.normalize_observation(ASIN, raw_product(), patrol.product_url(ASIN))
        self.assertEqual(local_db.amz_record(ASIN, data)["status"], "baseline")
        for _ in range(3):
            self.assertEqual(local_db.amz_record(ASIN, data)["status"], "unchanged")
        self.assertEqual(local_db.amz_changes(), [])
        self.assertEqual(self.current()["price"], "299.00")

    def test_only_changed_fields_are_appended_even_same_day_and_price_can_return(self):
        local_db.amz_record(ASIN, {"price": "299.00", "coupon": "Save $20", "currency": "USD"})
        result = local_db.amz_record(ASIN, {"price": "279.00", "coupon": "Save $20", "currency": "USD"})
        self.assertEqual(result["changes"], {"price": {"old": "299.00", "new": "279.00"}})
        local_db.amz_record(ASIN, {"price": "299.00"})
        self.assertEqual(len(local_db.amz_changes()), 2)

    def test_error_and_unknown_fields_preserve_values(self):
        local_db.amz_record(ASIN, {"price": "299.00", "coupon": "Save $20"})
        local_db.amz_record_error(ASIN, "验证码")
        self.assertEqual(self.current(), {"price": "299.00", "coupon": "Save $20"})
        self.assertEqual(local_db.amz_changes(), [])
        self.assertEqual(local_db.amz_record(ASIN, {"price": "299.00", "coupon": None})["status"], "unchanged")
        changed = local_db.amz_record(ASIN, {"coupon": ""})
        self.assertEqual(changed["changes"]["coupon"], {"old": "Save $20", "new": ""})

    def test_replacing_watchlist_preserves_removed_baseline_and_seller_snapshots(self):
        local_db.save_batch("old", "seller-task", 1, [{"asin": OTHER, "price": 500}])
        local_db.amz_record(ASIN, {"price": "299.00"})
        local_db.amz_save_watchlist([OTHER])
        self.assertEqual([r["asin"] for r in local_db.amz_watchlist()], [OTHER])
        local_db.amz_save_watchlist([ASIN])
        self.assertEqual(self.current()["price"], "299.00")
        self.assertEqual(local_db.latest()[0]["price"], 500)

    def test_change_and_current_update_roll_back_together(self):
        local_db.amz_record(ASIN, {"price": "299.00"})
        c = local_db._conn()
        c.executescript("CREATE TRIGGER fail_amz_update BEFORE UPDATE OF current_data ON amz_watch "
                        "BEGIN SELECT RAISE(ABORT, 'test write failure'); END;")
        c.close()
        with self.assertRaises(sqlite3.IntegrityError):
            local_db.amz_record(ASIN, {"price": "279.00"})
        self.assertEqual(self.current()["price"], "299.00")
        self.assertEqual(local_db.amz_changes(), [])


class EndpointTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import web_server
        self.server = web_server
        self.keeper, self.db_patch = isolated_memory_db()
        self.db_patch.start()
        self.old_jobs = web_server.S["jobs"]
        self.old_job = web_server.S.get("amazon_job_id")
        web_server.S["jobs"] = {}
        web_server.S["amazon_job_id"] = None

    async def asyncTearDown(self):
        self.server.S["jobs"] = self.old_jobs
        self.server.S["amazon_job_id"] = self.old_job
        self.db_patch.stop()
        self.keeper.close()

    async def test_watch_and_changes_never_touch_seller_browser(self):
        with patch.object(self.server, "get_pw", new_callable=AsyncMock) as seller_browser:
            self.assertEqual((await self.server.amazon_watch())["items"], [])
            saved = await self.server.amazon_save_watch({"asins": ASIN})
            self.assertEqual(saved["items"][0]["asin"], ASIN)
            self.assertEqual((await self.server.amazon_changes())["items"], [])
            seller_browser.assert_not_awaited()

    async def test_duplicate_run_and_watch_edit_are_rejected_while_running(self):
        from fastapi import HTTPException
        await self.server.amazon_save_watch({"asins": ASIN})
        gate = asyncio.Event()

        async def fake_run(asins, job):
            await gate.wait()
            job["status"] = "done"

        previous_asins = list(self.server.S["asins"])
        with patch.object(self.server.amz, "run_patrol", side_effect=fake_run):
            await self.server.amazon_run({})
            try:
                for operation in (lambda: self.server.amazon_run({}), lambda: self.server.amazon_save_watch({"asins": OTHER})):
                    with self.assertRaises(HTTPException) as error:
                        await operation()
                    self.assertEqual(error.exception.status_code, 409)
            finally:
                gate.set()
                await asyncio.sleep(0)
        self.assertEqual(self.server.S["asins"], previous_asins)

    async def test_http_run_requires_json_and_empty_list_does_not_start_browser(self):
        # 直接跑 ASGI 协议，不另装 HTTP 客户端、不启动真实服务器。
        for content_type, body, expected in ((b'application/x-www-form-urlencoded', b'x=1', 422),
                                              (b'text/plain', b'{}', 422),
                                              (b'application/json', b'{}', 400)):
            messages = []

            async def receive():
                return {"type": "http.request", "body": body, "more_body": False}

            async def send(message):
                messages.append(message)

            scope = {"type": "http", "http_version": "1.1", "method": "POST",
                     "scheme": "http", "path": "/api/amazon/run", "raw_path": b"/api/amazon/run",
                     "root_path": "", "query_string": b"", "headers": [(b"content-type", content_type)],
                     "server": ("127.0.0.1", 8765), "client": ("127.0.0.1", 12345)}
            await self.server.app(scope, receive, send)
            self.assertEqual(messages[0]["status"], expected)
        self.assertEqual(self.server.S["jobs"], {})

    async def test_invalid_url_input_does_not_replace_existing_list(self):
        from fastapi import HTTPException
        await self.server.amazon_save_watch({"asins": ASIN})
        with self.assertRaises(HTTPException) as error:
            await self.server.amazon_save_watch({"asins": "http://127.0.0.1/"})
        self.assertEqual(error.exception.status_code, 400)
        self.assertEqual((await self.server.amazon_watch())["items"][0]["asin"], ASIN)


@unittest.skipUnless(os.environ.get("AMZ_BROWSER_TESTS") == "1", "可选本地 Chromium DOM 测试")
class BrowserSelectorTests(unittest.IsolatedAsyncioTestCase):
    async def test_main_price_does_not_use_hidden_price_or_recommendations(self):
        from playwright.async_api import async_playwright
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                await page.set_content(f'''<title>Product</title><span id="productTitle">Power station</span>
                    <input id="ASIN" value="{ASIN}">
                    <div id="corePriceDisplay_desktop_feature_div">
                      <div style="display:none"><span class="a-price"><span class="a-offscreen">$1.00</span></span></div>
                      <span class="a-price priceToPay"><span class="a-offscreen">$299.00</span></span>
                      <span class="a-price a-text-price"><span class="a-offscreen">$399.00</span></span>
                    </div><div id="couponFeature">Save $20 with coupon</div><div id="availability">In Stock</div>
                    <div id="recommendations"><span class="a-price"><span class="a-offscreen">$9.99</span></span></div>''')
                raw = await page.evaluate(patrol.EXTRACT_PRODUCT)
                data, _ = patrol.normalize_observation(ASIN, raw, patrol.product_url(ASIN))
                self.assertEqual(data["price"], "299.00")
                self.assertEqual(data["reference_price"], "399.00")
                self.assertEqual(data["coupon"], "Save $20 with coupon")
                # 实测新版 Amazon：无障碍金额为空，主价拆成符号/整数/小数。
                await page.set_content(f'<span id="productTitle">Product</span><input id="ASIN" value="{ASIN}">'
                                       '<div id="corePriceDisplay_desktop_feature_div">'
                                       '<span class="a-price priceToPay apex-pricetopay-value"><span class="a-offscreen"> </span>'
                                       '<span aria-hidden="true"><span class="a-price-symbol">$</span>'
                                       '<span class="a-price-whole">1,709<span class="a-price-decimal">.</span></span>'
                                       '<span class="a-price-fraction">00</span></span></span></div>')
                split_price, _ = patrol.normalize_observation(ASIN, await page.evaluate(patrol.EXTRACT_PRODUCT), patrol.product_url(ASIN))
                self.assertEqual(split_price["price"], "1709.00")
                # 金额子节点无分期字样时，也必须检查祖先购买上下文。
                for context in ("Subscribe &amp; Save", "per month"):
                    await page.set_content(f'<span id="productTitle">Product</span><input id="ASIN" value="{ASIN}">'
                                           f'<div id="corePriceDisplay_desktop_feature_div">{context}'
                                           '<span class="priceToPay"><span class="a-offscreen">$19.99</span></span></div>')
                    with self.assertRaises(patrol.ObservationError):
                        patrol.normalize_observation(ASIN, await page.evaluate(patrol.EXTRACT_PRODUCT), patrol.product_url(ASIN))
                await page.set_content(f'<span id="productTitle">Product</span><input id="ASIN" value="{ASIN}">'
                                       '<div id="corePrice_feature_div"><span class="priceToPay">'
                                       '<span class="a-offscreen">$299.00</span></span></div>'
                                       '<div id="couponFeature" style="display:none">Save $20</div>'
                                       '<div id="promotions_feature_div">Limited time deal</div>')
                observed, _ = patrol.normalize_observation(ASIN, await page.evaluate(patrol.EXTRACT_PRODUCT), patrol.product_url(ASIN))
                self.assertNotIn("coupon", observed)
                self.assertEqual(observed["promotions"], "Limited time deal")
                await page.set_content(f'<span id="productTitle">Product</span><input id="ASIN" value="{ASIN}">'
                                       '<div class="a-price"><span class="a-offscreen">$9.99</span></div>')
                with self.assertRaises(patrol.ObservationError):
                    patrol.normalize_observation(ASIN, await page.evaluate(patrol.EXTRACT_PRODUCT), patrol.product_url(ASIN))
            finally:
                await browser.close()

    async def test_fixed_us_zip_and_new_apex_layout_without_real_network(self):
        from playwright.async_api import async_playwright
        from urllib.parse import urlsplit
        delivery = {"label": "New York 10001"}
        home = '''<a id="nav-global-location-popover-link" href="#">Deliver to Malaysia</a>
          <input id="GLUXZipUpdateInput"><button id="GLUXZipUpdate"
          onclick="setTimeout(()=>document.querySelectorAll('#GLUXConfirmClose')[1].style.display='block',80)">Apply</button>
          <button id="GLUXConfirmClose" style="display:none">Hidden duplicate</button>
          <button id="GLUXConfirmClose" style="display:none"
          onclick="document.querySelector('#nav-global-location-popover-link').textContent='New York '+document.querySelector('#GLUXZipUpdateInput').value;this.style.display='none'">Done</button>'''

        async def fixture(route):
            path = urlsplit(route.request.url).path
            product = (f'<a id="nav-global-location-popover-link">{delivery["label"]}</a>'
                       f'<span id="productTitle">Power station</span><input id="ASIN" value="{ASIN}">'
                       '<div id="corePrice_desktop">'
                       '<span class="a-price apex-price-to-pay-value"><span class="a-offscreen">$299.00</span></span>'
                       '<span class="a-price apex-basis-price-value"><span class="a-offscreen">$399.00</span></span>'
                       '</div><div id="availability">In Stock</div>')
            await route.fulfill(content_type="text/html", body=home if path == "/" else product)

        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                await page.route("**/*", fixture)
                await patrol.prepare_us_delivery(page)
                self.assertIn("10001", await page.locator("#nav-global-location-popover-link").inner_text())
                data, _ = await patrol.collect_page(page, ASIN)
                self.assertEqual(data["price"], "299.00")
                self.assertEqual(data["reference_price"], "399.00")
                delivery["label"] = "Malaysia"
                with self.assertRaises(patrol.ObservationError):
                    await patrol.collect_page(page, ASIN)
            finally:
                await browser.close()

    async def test_amazon_tab_is_isolated_and_renders_external_text_safely(self):
        from playwright.async_api import async_playwright
        html = (Path(__file__).resolve().parents[1] / "web_index.html").read_text(encoding="utf-8")
        calls = []
        malicious = '<img src=x onerror="window.amzInjected=true">'
        item = {"asin": ASIN, "url": "javascript:alert(1)",
                "current": {"title": malicious, "price": "299.00", "coupon": malicious},
                "last_status": "baseline", "last_checked": "2026-01-01", "last_error": ""}
        change = {"price": {"old": "299.00", "new": "279.00"}}

        async def fake_server(route):
            from urllib.parse import urlsplit
            path = urlsplit(route.request.url).path
            calls.append((path, route.request.method))
            if path == "/":
                await route.fulfill(content_type="text/html", body=html)
                return
            if path == "/api/amazon/watch":
                data = {"items": [item], "max_asins": 20, "running_job_id": None}
            elif path == "/api/amazon/changes":
                data = {"items": [{"id": 1, "asin": ASIN, "checked_at": "2026-01-02", "changes": change}]}
            elif path == "/api/amazon/run":
                data = {"job_id": "fixture-job"}
            elif path == "/api/job/fixture-job":
                data = {"status": "done", "done": 1, "total": 1, "changed": 1,
                        "baselines": 0, "unchanged": 0, "failed": 0, "log": [malicious],
                        "results": [{"asin": ASIN, "status": "changed", "changes": change, "message": malicious}]}
            elif path == "/api/accounts":
                data = {"main": {"logged_in": False}, "member": {"logged_in": False},
                        "db": {"snapshots": 0, "asins": 0, "batches": 0}}
            elif path == "/api/pool":
                data = {"slots": [], "next_slot": 1}
            elif path == "/api/watchlist":
                data = {"items": [], "count": 0, "own": 0, "blocked": {}, "bsr_configured": []}
            elif path == "/api/bsr/sources":
                data = {"items": []}
            elif path == "/api/routine/plan":
                data = {"plan": [], "last_run": []}
            else:
                await route.abort()
                return
            await route.fulfill(content_type="application/json", body=json.dumps(data))

        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            try:
                page = await browser.new_page(viewport={"width": 1440, "height": 1100})
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                await page.route("**/*", fake_server)
                await page.goto("http://mvp.test/#amazon")
                await page.wait_for_function("document.querySelector('#amzCurrentBody').textContent.includes('299.00')")
                self.assertTrue(await page.locator("#amzPanelAmazon").is_visible())
                self.assertFalse(await page.locator("#amzPanelSellersprite").is_visible())
                self.assertTrue(all(path == "/" or path.startswith("/api/amazon/") for path, _ in calls))
                self.assertIn(malicious, await page.locator("#amzCurrentBody").inner_text())
                self.assertEqual(await page.locator("#amzCurrentBody img").count(), 0)
                self.assertFalse(await page.evaluate("Boolean(window.amzInjected)"))
                self.assertEqual(await page.locator("#amzCurrentBody a").get_attribute("href"), patrol.product_url(ASIN))
                self.assertTrue(await page.locator("#amzScheduleBtn").is_disabled())
                await page.locator("#amzAsins").fill(ASIN.lower())
                await page.locator("#amzSaveBtn").click()
                await page.wait_for_function("!document.querySelector('#amzRunBtn').disabled")
                await page.locator("#amzRunBtn").click()
                await page.wait_for_function("document.querySelector('#amzProgressText').textContent.includes('有差异 1')")
                self.assertIn("299.00 → 279.00", await page.locator("#amzResultsBody").inner_text())
                await page.locator("#amzTabSellersprite").click()
                await page.wait_for_function("!document.querySelector('#amzPanelSellersprite').hidden")
                await page.wait_for_function("document.querySelector('#main-badge').textContent.includes('未登录')")
                await page.locator("#amzTabAmazon").click()
                await page.wait_for_function("!document.querySelector('#amzPanelAmazon').hidden")
                await page.locator("#amzTabSellersprite").click()
                self.assertEqual(sum(path == "/api/accounts" for path, _ in calls), 1)
                self.assertEqual(errors, [])
            finally:
                await browser.close()


if __name__ == "__main__":
    unittest.main()
