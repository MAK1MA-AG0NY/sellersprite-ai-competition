"""Opt-in Chromium regression for the built preview; all HTTP is intercepted.

Build frontend/price-workspace first, then run with AMZ_PREVIEW_BROWSER_TESTS=1.
Playwright uses its normal Chromium discovery (including PLAYWRIGHT_BROWSERS_PATH).
"""

import os
import re
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlsplit


INDEX = Path(__file__).resolve().parents[1] / "frontend" / "price-workspace" / "dist" / "index.html"
HOST = "127.0.0.1:8765"
PREVIEW_URL = f"http://{HOST}/price-workspace/"
GROUPS = "/api/amazon/groups"
WATCH = "/api/amazon/watch"
RUN = "/api/amazon/run"
PLAN = "/api/amazon/run-all/preview"
RUN_ALL = "/api/amazon/run-all"
FIRST, SECOND, EMPTY = 7, 12, 19
LOW, HIGH, OLD, OTHER_CURRENCY, OTHER, NEW = (f"B00000000{i}" for i in range(1, 7))


def simple_path(group_id):
    return f"/api/amazon/simple?group_id={group_id}"


def row(asin, title="", price="—", **fields):
    return {"asin": asin, "title": title, "price": price, "offer": "—", "below_own_price": False,
            "comparison": "—", "checked_at": "—", "status": "今天未巡查", "error": "", **fields}


class PreviewFixture:
    def __init__(self):
        self.names = {FIRST: "电站合成组", SECOND: "太阳能合成组", EMPTY: "空名单合成组"}
        self.rows = {
            FIRST: [row(OLD, "Yesterday only", checked_at="11-18 16:00 · 非今天"),
                    row(HIGH, "Trail 1200", "$1,299.00", status="今日已检查"),
                    row(LOW, "Trail Mini", "$99.50", status="今日已检查（库存未确认）",
                        offer="Save $8 coupon", comparison="低于本品 $0.50", below_own_price=True),
                    row(OTHER_CURRENCY, "EU only", "€60.00 EUR", status="非 USD，不比较",
                        comparison="不比较（非 USD）")],
            SECOND: [row(OTHER, "Solar Panel", "$100.00", status="今日已检查"), row(LOW, "Trail Mini")],
            EMPTY: [],
        }
        self.calls = []
        self.writes = []
        self.unexpected = []
        self.job = None

    def begin_job(self, id_, group_id=None, total=1):
        self.job = {"job_id": id_, "status": "running", "done": 1, "total": total, "failed": 0,
                    "group_id": group_id, "group_name": self.names.get(group_id, "全部竞品组"),
                    "detail": "合成巡查进度"}

    def route(self, route):
        request = route.request
        url = urlsplit(request.url)
        method, path = request.method, url.path
        if url.scheme != "http" or url.netloc != HOST:
            self.unexpected.append(request.url)
            return route.abort()
        if method == "GET" and path == "/price-workspace/":
            return route.fulfill(path=INDEX, content_type="text/html; charset=utf-8")
        if method == "GET" and path == "/favicon.ico":
            return route.fulfill(status=204)

        endpoint = path + (f"?{url.query}" if url.query else "")
        self.calls.append((method, endpoint))
        payload = request.post_data_json if method == "POST" else None
        if method == "POST":
            self.writes.append((path, payload))
        groups = [{"id": id_, "name": name, "count": len(self.rows[id_])} for id_, name in self.names.items()]
        running = self.job is not None and self.job["status"] == "running"
        running_id = self.job["job_id"] if running else None
        if (method, path) == ("GET", GROUPS):
            result = {"items": groups, "default_group_id": FIRST, "running_job_id": running_id}
        elif (method, path) == ("GET", "/api/amazon/simple"):
            group_id = int(parse_qs(url.query)["group_id"][0])
            result = {"group_id": group_id, "group_name": self.names[group_id],
                      "asins": [item["asin"] for item in self.rows[group_id]], "max_asins": 20,
                      "reference_price": "100.00", "items": self.rows[group_id], "today": "2026-11-19",
                      "running_job_id": running_id, "running_status": "running" if running else None,
                      "running_group_id": self.job["group_id"] if running else None}
        elif (method, path) == ("POST", WATCH):
            group_id = payload["group_id"]
            old = {item["asin"]: item for item in self.rows[group_id]}
            self.rows[group_id] = [old.get(asin, row(asin)) for asin in payload["asins"]]
            result = {"group_id": group_id, "group_name": self.names[group_id], "max_asins": 20,
                      "items": [{"asin": item["asin"]} for item in self.rows[group_id]], "running_job_id": running_id}
        elif (method, path) == ("POST", RUN):
            group_id = payload["group_id"]
            self.begin_job("single-fixture", group_id, len(self.rows[group_id]))
            result = {"job_id": self.job["job_id"], "group_id": group_id, "group_name": self.names[group_id]}
        elif (method, path) == ("GET", PLAN):
            result = {"groups": [group for group in groups if group["count"]], "group_names": list(self.names.values())[:2],
                      "saved_group_count": 3, "group_count": 2, "empty_group_count": 1, "membership_count": 6,
                      "unique_asin_count": 5, "duplicates_skipped": 1, "limit": 200, "can_run": True,
                      "plan_hash": f"fixture-plan-{self.calls.count(('GET', PLAN))}"}
        elif (method, path) == ("POST", RUN_ALL):
            self.begin_job("bulk-fixture", total=5)
            self.job.update(job_type="all_groups", group_count=2)
            result = {"job_id": self.job["job_id"], "job_type": "all_groups", "group_count": 2,
                      "membership_count": 6, "unique_asin_count": 5, "duplicates_skipped": 1}
        elif method == "GET" and self.job and path == f"/api/job/{self.job['job_id']}":
            if self.job.get("missing"):
                self.job = None
                return route.fulfill(status=404, json={"detail": "任务不存在"})
            result = self.job
        else:
            self.unexpected.append(f"{method} {request.url}")
            return route.abort()
        return route.fulfill(json=result)


@unittest.skipUnless(os.environ.get("AMZ_PREVIEW_BROWSER_TESTS") == "1", "opt-in preview Chromium tests")
class PriceWorkspaceUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import expect, sync_playwright

        if not INDEX.is_file():
            raise FileNotFoundError(f"Build the price workspace preview first: missing {INDEX}")
        cls.expect = expect
        playwright = sync_playwright().start()
        cls.addClassCleanup(playwright.stop)
        cls.browser = playwright.chromium.launch(headless=True)
        cls.addClassCleanup(cls.browser.close)

    def new_page(self):
        context = self.browser.new_context(service_workers="block")
        self.addCleanup(context.close)
        page = context.new_page()
        page.set_default_timeout(6500)
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        self.addCleanup(self.assertEqual, errors, [], "uncaught browser errors")
        return page

    def open_online(self, fixture=None):
        fixture = fixture or PreviewFixture()
        page = self.new_page()
        page.context.route("**/*", fixture.route)
        self.addCleanup(self.assertEqual, fixture.unexpected, [], "unexpected network requests")
        page.goto(PREVIEW_URL)
        self.expect(page.get_by_role("heading", name="今日巡查结果")).to_be_visible()
        self.expect(page.get_by_role("button", name=re.compile(f"^{fixture.names[FIRST]}"))).to_have_attribute("aria-pressed", "true")
        self.expect(page.locator("tbody tr").filter(has_text=LOW)).to_be_visible()
        return page, fixture

    def assert_rows(self, page, *asins):
        self.expect(page.locator("tbody tr")).to_have_text([re.compile(asin) for asin in asins])

    def test_file_and_explicit_http_demo_do_not_request_an_api(self):
        for url in (INDEX.as_uri(), PREVIEW_URL + "?demo=1"):
            with self.subTest(url=url):
                page = self.new_page()
                fixture = PreviewFixture()
                # File navigation itself isn't mocked; block HTTP(S) in both modes.
                page.context.route(re.compile(r"^https?://"), fixture.route)
                xhr = []
                page.on("request", lambda request: xhr.append(request.url)
                        if request.resource_type in {"fetch", "xhr"} else None)
                page.goto(url)
                self.expect(page.get_by_role("heading", name="价格巡查", exact=True)).to_be_visible()
                self.expect(page.get_by_text("离线演示", exact=True)).to_be_visible()
                self.expect(page.get_by_role("button", name=re.compile("^便携电站"))).to_have_attribute("aria-pressed", "true")
                self.expect(page.locator("tbody").get_by_text("Northline Trail 500", exact=False)).to_be_visible()
                page.get_by_role("button", name=re.compile("^太阳能板")).click()
                self.expect(page.locator("tbody").get_by_text("Northline Sun 120", exact=False)).to_be_visible()
                self.assertEqual(xhr, [])
                self.assertEqual(fixture.calls, [])
                self.assertEqual(fixture.unexpected, [])

    def test_online_rows_sort_search_filter_and_replace_group_members(self):
        page, fixture = self.open_online()
        self.assertEqual(fixture.calls, [("GET", GROUPS), ("GET", simple_path(FIRST))])
        price = page.get_by_role("button", name="今日购买价", exact=True)
        price_header = page.locator("th[aria-sort]").filter(has=price)
        self.expect(price_header).to_have_attribute("aria-sort", "ascending")
        self.assert_rows(page, LOW, HIGH, OLD, OTHER_CURRENCY)
        price.click()
        self.expect(price_header).to_have_attribute("aria-sort", "descending")
        self.assert_rows(page, HIGH, LOW, OLD, OTHER_CURRENCY)
        self.expect(page.locator("tbody tr").filter(has_text=LOW)).to_contain_text("Save $8 coupon")
        self.expect(page.locator("tbody tr").filter(has_text=LOW)).to_contain_text("$99.50")

        page.get_by_role("button", name="低于本品", exact=True).click()
        self.assert_rows(page, LOW)
        page.get_by_role("button", name="无 USD 价", exact=True).click()
        self.assert_rows(page, OLD, OTHER_CURRENCY)
        page.get_by_role("button", name="全部", exact=True).click()
        search = page.get_by_label("搜索商品标题或 ASIN")
        search.fill("tRaIL mINI")
        self.assert_rows(page, LOW)
        search.fill(OTHER_CURRENCY.lower())
        self.assert_rows(page, OTHER_CURRENCY)
        search.clear()

        edit = page.get_by_role("button", name="编辑 ASIN 名单", exact=True)
        edit.click()
        dialog = page.get_by_role("dialog", name="编辑 ASIN 名单")
        field = dialog.get_by_label("ASIN 名单", exact=True)
        self.expect(field).to_have_value("\n".join((OLD, HIGH, LOW, OTHER_CURRENCY)))
        save = dialog.get_by_role("button", name="保存名单", exact=True)
        field.fill("\n".join(f"B{i:09d}" for i in range(21)))
        save.click()
        self.expect(dialog.get_by_role("alert")).to_contain_text("20")
        self.assertEqual(fixture.writes, [])
        field.fill(f"{NEW.lower()}，{LOW}\n{NEW}")
        save.click()
        self.expect(dialog).not_to_be_visible()
        self.assert_rows(page, LOW, NEW)
        self.expect(page.get_by_role("button", name=re.compile(r"^电站合成组\s*2"))).to_be_visible()
        self.assertEqual(fixture.writes, [(WATCH, {"group_id": FIRST, "asins": [NEW, LOW]})])
        self.assertEqual([item["asin"] for item in fixture.rows[SECOND]], [OTHER, LOW])

    def test_only_explicit_runs_poll_and_refresh_the_viewed_group(self):
        page, fixture = self.open_online()
        single = page.get_by_role("button", name="巡查本组", exact=True)
        bulk = page.get_by_role("button", name="巡查全部分组", exact=True)
        edit = page.get_by_role("button", name="编辑 ASIN 名单", exact=True)
        progress = page.get_by_label("巡查进度", exact=True)
        self.expect(single).to_be_enabled()
        self.assertEqual(fixture.writes, [])
        single.click()
        self.expect(progress).to_have_attribute("value", "1")
        self.expect(progress).to_have_attribute("max", "4")
        self.assertEqual(fixture.writes, [(RUN, {"group_id": FIRST, "simple_mode": True})])
        self.expect(single).to_be_disabled()
        self.expect(bulk).to_be_disabled()
        self.expect(edit).to_be_disabled()
        page.get_by_role("button", name=re.compile("^太阳能合成组")).click()
        self.assert_rows(page, OTHER, LOW)
        self.expect(single).to_be_disabled()

        fixture.rows[SECOND][0]["price"] = "$369.00"
        fixture.job.update(status="done", done=4, detail="合成单组完成")
        self.expect(page.locator("tbody tr").filter(has_text=OTHER)).to_contain_text("$369.00")
        self.expect(single).to_be_enabled()
        self.assertGreaterEqual(fixture.calls.count(("GET", "/api/job/single-fixture")), 2)

        bulk.click()
        dialog = page.get_by_role("dialog", name="巡查全部分组")
        confirm = dialog.get_by_role("button", name="确认巡查", exact=True)
        self.expect(confirm).to_be_enabled()
        self.expect(dialog).to_contain_text("5 个唯一 ASIN")
        self.expect(dialog).to_contain_text("10001")
        self.assertNotIn(("POST", RUN_ALL), fixture.calls)
        dialog.get_by_role("button", name="取消", exact=True).click()
        self.expect(dialog).not_to_be_visible()
        self.assertNotIn(("POST", RUN_ALL), fixture.calls)
        self.assertNotIn(("GET", "/api/job/bulk-fixture"), fixture.calls)

        bulk.click()
        self.expect(confirm).to_be_enabled()
        confirm.click()
        self.expect(progress).to_have_attribute("max", "5")
        self.assertEqual(fixture.writes[-1], (RUN_ALL, {"confirm": True, "plan_hash": "fixture-plan-2"}))
        self.assertEqual(fixture.calls.count(("GET", PLAN)), 2)
        fixture.job.update(status="error", detail="合成中断：已保留部分结果", failed=1)
        fixture.rows[SECOND][0]["price"] = "$349.00"
        self.expect(page.locator("tbody tr").filter(has_text=OTHER)).to_contain_text("$349.00")
        self.expect(progress).not_to_be_visible()
        self.expect(bulk).to_be_enabled()

    def test_missing_discovered_job_reloads_and_unlocks_the_group(self):
        fixture = PreviewFixture()
        fixture.begin_job("missing-fixture", SECOND)
        fixture.job["missing"] = True
        page, _ = self.open_online(fixture)
        self.expect(page.get_by_role("alert")).to_contain_text("任务记录已不存在")
        self.expect(page.get_by_role("button", name="巡查本组", exact=True)).to_be_enabled()
        self.assertEqual(fixture.calls.count(("GET", "/api/job/missing-fixture")), 1)
        self.assertGreaterEqual(fixture.calls.count(("GET", simple_path(FIRST))), 2)
        self.assertEqual(fixture.writes, [])


if __name__ == "__main__":
    unittest.main()
