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

    def test_precise_offer_types_require_explicit_evidence(self):
        no_offer = patrol.classify_offers({"offer_regions_seen": True, "coupon_text": "", "promotions_text": ""},
                                          price="299.00", reference_price="399.00")
        self.assertEqual(no_offer["offer_types"], ["reference_discount"])
        self.assertNotIn("lightning_deal", no_offer["offer_types"])
        self.assertIn("不构成秒杀证据", no_offer["offer_evidence"][0]["text"])
        explicit = patrol.classify_offers({"offer_regions_seen": True, "coupon_text": "Save $20 with coupon",
                                           "promotions_text": "Lightning Deal · Prime Exclusive Deal"})
        self.assertEqual(set(explicit["offer_types"]), {"lightning_deal", "prime_exclusive", "coupon"})
        self.assertEqual(patrol.classify_offers({"offer_regions_seen": False, "coupon_text": "", "promotions_text": ""})["offer_read_status"], "not_read")

    def test_unavailable_is_explicit_and_does_not_invent_a_price(self):
        data, note = patrol.normalize_observation(
            ASIN, raw_product(price_text="", availability_text="Currently unavailable"), patrol.product_url(ASIN))
        self.assertEqual(data["availability"], "unavailable")
        self.assertNotIn("price", data)
        self.assertNotIn("coupon", data)
        self.assertIn("缺货", note)
        self.assertNotIn("当前未展示购买价", note)

    def test_reported_b0d17cw6kk_is_out_of_stock_even_with_stale_price_markup(self):
        asin = "B0D17CW6KK"
        message = "Currently unavailable.\nWe don't know when or if this item will be back in stock."
        for price in ("", "$299.00"):
            data, note = patrol.normalize_observation(
                asin, raw_product(page_asin=asin, price_text=price, availability_text=message), patrol.product_url(asin))
            self.assertEqual(data["availability"], "unavailable")
            self.assertIn("缺货", note)
            self.assertNotIn("price", data)


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

    def test_stock_and_restock_are_differences_without_zeroing_history_price(self):
        initial, _ = patrol.normalize_observation(ASIN, raw_product(), patrol.product_url(ASIN))
        local_db.amz_record(ASIN, initial)
        missing, note = patrol.normalize_observation(
            ASIN, raw_product(price_text="", availability_text="Currently unavailable. We don't know when or if this item will be back in stock."),
            patrol.product_url(ASIN))
        result = local_db.amz_record(ASIN, missing, note=note)
        self.assertEqual(result["changes"], {"availability": {"old": "available", "new": "unavailable"}})
        self.assertEqual(self.current()["price"], "299.00")
        self.assertIn("缺货", local_db.amz_watchlist()[0]["last_error"])
        self.assertEqual(local_db.amz_record(ASIN, missing, note=note)["status"], "unchanged")
        restored = local_db.amz_record(ASIN, initial)
        self.assertEqual(restored["changes"], {"availability": {"old": "unavailable", "new": "available"}})
        self.assertEqual(len(local_db.amz_changes()), 2)

    def test_successful_checks_append_observations_while_unread_offers_are_preserved(self):
        first = {"price": "299.00", "currency": "USD", "offer_types": ["coupon"],
                 "offer_evidence": [{"type": "coupon", "source": "coupon_region", "text": "Save $20"}],
                 "offer_read_status": "observed", "offer_observed_at": "2026-01-01T00:00:00+00:00"}
        local_db.amz_record(ASIN, first, checked_at="2026-01-01T00:00:00+00:00")
        local_db.amz_record(ASIN, {"price": "299.00", "offer_read_status": "not_read"},
                            checked_at="2026-01-02T00:00:00+00:00")
        self.assertEqual(self.current()["offer_types"], ["coupon"])
        observations = local_db.amz_observations()
        self.assertEqual(observations["total"], 2)
        latest = observations["items"][0]["data"]
        self.assertEqual(latest["offer_types"], ["coupon"])
        self.assertEqual(latest["offer_read_status"], "not_read")
        local_db.amz_record_error(ASIN, "captcha")
        self.assertEqual(local_db.amz_observations()["total"], 2)

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

    async def test_product_comparison_rule_analysis_context_and_default_off_schedule(self):
        import os
        group_id = (await self.server.amazon_groups())["default_group_id"]
        await self.server.amazon_save_watch({"group_id": group_id, "asins": ASIN})
        local_db.amz_record(ASIN, {"price": "249.00", "currency": "USD", "availability": "available",
                               "offer_types": ["coupon"], "offer_evidence": [{"source": "coupon", "text": "Save $10"}]})
        saved = await self.server.amazon_save_product({"group_id": group_id, "name": "Own Product", "asin": OTHER,
            "spec_notes": "battery capacity", "manual_price": "299.00", "protected_floor": "220.00",
            "currency": "USD", "price_source": "manual"})
        self.assertEqual(saved["product"]["manual_price"], "299.00")
        comparison = await self.server.amazon_comparison_view(group_id)
        self.assertEqual(comparison["comparisons"][0]["asin"], ASIN)
        self.assertEqual(comparison["comparisons"][0]["delta_vs_own"], "-50.00")
        rules = await self.server.amazon_analysis({"group_id": group_id, "mode": "rules"})
        self.assertEqual(rules["analysis"]["kind"], "rule")
        context = await self.server.amazon_analysis_context({"group_id": group_id})
        self.assertEqual(context["type"], "seller-sprite-amazon-analysis-context")
        self.assertFalse(any("order" in key.lower() or "cookie" in key.lower() for key in context["context"]))
        with patch.dict(os.environ, {}, clear=True):
            ai = await self.server.amazon_analysis({"group_id": group_id, "mode": "ai"})
            self.assertEqual(ai["status"], "ai_disabled")
            self.assertFalse((await self.server.amazon_ai_status())["enabled"])
        schedule = await self.server.amazon_schedule_get(group_id)
        self.assertFalse(schedule["schedule"]["enabled"])
        with patch.dict(os.environ, {"WEB_CONCURRENCY": "1"}):
            enabled = await self.server.amazon_schedule_save({"group_id": group_id, "enabled": True,
                "local_time": "08:30", "timezone": "Asia/Shanghai"})
        self.assertTrue(enabled["schedule"]["enabled"])
        self.assertEqual(enabled["schedule"]["local_time"], "08:30")
        observations = await self.server.amazon_observations(group_id=group_id)
        self.assertEqual(observations["total"], 1)

    async def test_observed_own_product_is_collectible_without_competitor_membership_and_can_switch(self):
        group_id = (await self.server.amazon_groups())["default_group_id"]
        await self.server.amazon_save_product({"group_id": group_id, "name": "Own A", "asin": "B000000001",
            "price_source": "observed", "currency": "USD"})
        self.assertEqual(local_db.amz_watchlist(group_id), [])
        first = local_db.amz_record("B000000001", {"price":"100.00","currency":"USD","availability":"available"})
        self.assertEqual(first["status"], "baseline")
        await self.server.amazon_save_product({"group_id": group_id, "name": "Own B", "asin": "B000000002",
            "price_source": "observed", "currency": "USD"})
        self.assertEqual(local_db.amz_watchlist(group_id), [])
        second = local_db.amz_record("B000000002", {"price":"120.00","currency":"USD","availability":"available"})
        self.assertEqual(second["status"], "baseline")
        self.assertEqual(local_db.amz_observations(group_id)["total"], 1)
        self.assertEqual(local_db.amz_observations()["total"], 2)
        await self.server.amazon_save_watch({"group_id": group_id, "asins": "B000000003"})
        local_db.amz_record("B000000003", {"price":"90.00","currency":"USD","availability":"available"})
        await self.server.amazon_save_product({"group_id": group_id, "name": "Shared own", "asin": "B000000003",
            "price_source": "observed", "currency": "USD"})
        changed = local_db.amz_record("B000000003", {"price":"95.00","currency":"USD","availability":"available"})
        self.assertEqual(changed["status"], "changed")
        self.assertEqual([item["asin"] for item in local_db.amz_watchlist(group_id)], ["B000000003"])

    async def test_simple_today_view_is_honest_and_run_ignores_legacy_own_asin(self):
        group_id = (await self.server.amazon_groups())["default_group_id"]
        own_asin = "B000000099"
        await self.server.amazon_save_product({"group_id":group_id,"name":"Legacy name","asin":own_asin,
            "spec_notes":"keep legacy spec","manual_price":"299.00","protected_floor":"200.00",
            "currency":"USD","price_source":"observed"})
        asins = ["B000000001","B000000002","B000000003","B000000004","B000000005",
                 "B000000006","B000000007","B000000008","B000000009","B000000010","B000000011"]
        await self.server.amazon_save_watch({"group_id":group_id,"asins":"\n".join(asins)})
        now = self.server.datetime.now(self.server.AMZ_SHANGHAI).isoformat(timespec="seconds")
        yesterday = (self.server.datetime.now(self.server.AMZ_SHANGHAI) - self.server.timedelta(days=1)).isoformat(timespec="seconds")
        for asin, price in zip(asins[:3], ("249.00","299.00","329.00")):
            local_db.amz_record(asin, {"title":"Synthetic product","price":price,"currency":"USD","availability":"available",
                "offer_read_status":"observed","offer_observed_at":now,
                "offer_evidence":[{"type":"coupon","text":"Save $20 with coupon"}]}, checked_at=now)
        local_db.amz_record(asins[3], {"price":"100.00","currency":"USD","availability":"available"}, checked_at=yesterday)
        local_db.amz_record(asins[4], {"price":"90.00","currency":"USD","availability":"available"}, checked_at=yesterday)
        local_db.amz_record(asins[5], {"price":"80.00","currency":"USD","availability":"available",
            "offer_read_status":"observed","offer_observed_at":now,
            "offer_evidence":[{"type":"coupon","text":"Stale today coupon"}]}, checked_at=now)
        conn = local_db._conn()
        conn.execute("UPDATE amz_watch SET last_checked=?,last_status='error',last_error='Synthetic failure' WHERE asin=?", (now, asins[5])); conn.commit(); conn.close()
        local_db.amz_record(asins[6], {"price":"70.00","currency":"USD","availability":"available",
            "offer_read_status":"observed","offer_observed_at":now,
            "offer_evidence":[{"type":"coupon","text":"Stale today coupon"}]}, checked_at=now)
        local_db.amz_record(asins[6], {"price":"70.00","currency":"USD","availability":"out_of_stock"}, checked_at=now)
        local_db.amz_record(asins[7], {"price":"60.00","currency":"EUR","availability":"available"}, checked_at=now)
        local_db.amz_record(asins[8], {"price":"10.00","currency":"USD","availability":"available"}, checked_at=now)
        conn = local_db._conn(); conn.execute("UPDATE amz_watch SET last_status=NULL WHERE asin=?", (asins[8],)); conn.commit(); conn.close()
        local_db.amz_record(asins[9], {"price":"200.00","currency":"USD",
            "offer_read_status":"not_read"}, checked_at=now)
        local_db.amz_record(asins[10], {"price":"5.00","currency":"USD","availability":"unavailable"}, checked_at=now)
        conn = local_db._conn()
        conn.execute("UPDATE amz_group_products SET updated_at='2020-01-01T00:00:00+08:00' WHERE group_id=?", (group_id,)); conn.commit(); conn.close()
        state = await self.server.amazon_simple_state(group_id)
        rows = {row["asin"]: row for row in state["items"]}
        self.assertTrue(rows[asins[0]]["below_own_price"])
        self.assertIn("$50.00", rows[asins[0]]["comparison"])
        self.assertFalse(rows[asins[1]]["below_own_price"])
        self.assertEqual(rows[asins[1]]["comparison"], "与本品相同")
        self.assertFalse(rows[asins[2]]["below_own_price"])
        self.assertFalse(rows[asins[3]]["below_own_price"])
        self.assertEqual(rows[asins[3]]["price"], "—")
        self.assertEqual(rows[asins[3]]["offer"], "—")
        self.assertEqual(rows[asins[4]]["status"], "今天未巡查")
        self.assertEqual(rows[asins[4]]["price"], "—")
        self.assertEqual(rows[asins[5]]["status"], "采集失败")
        self.assertEqual(rows[asins[5]]["offer"], "采集失败，优惠未确认")
        self.assertFalse(rows[asins[5]]["below_own_price"])
        self.assertEqual(rows[asins[6]]["status"], "今日缺货 / 不可购买")
        self.assertEqual(rows[asins[6]]["offer"], "缺货时优惠不可确认")
        self.assertFalse(rows[asins[6]]["below_own_price"])
        self.assertEqual(rows[asins[7]]["status"], "非 USD，不比较")
        self.assertFalse(rows[asins[7]]["below_own_price"])
        self.assertEqual(rows[asins[8]]["status"], "今日状态未确认")
        self.assertEqual(rows[asins[8]]["price"], "—")
        self.assertFalse(rows[asins[8]]["below_own_price"])
        self.assertEqual(rows[asins[0]]["offer"], "Save $20 with coupon")
        self.assertTrue(rows[asins[9]]["below_own_price"])
        self.assertEqual(rows[asins[9]]["status"], "今日已检查（库存未确认）")
        self.assertEqual(rows[asins[9]]["offer"], "本次优惠未读取")
        self.assertIn("低于本品 $99.00", rows[asins[9]]["comparison"])
        self.assertEqual(rows[asins[10]]["status"], "今日缺货 / 不可购买")
        self.assertEqual(rows[asins[10]]["price"], "—")
        self.assertFalse(rows[asins[10]]["below_own_price"])
        await self.server.amazon_save_reference_price({"group_id":group_id,"price":None})
        no_reference = await self.server.amazon_simple_state(group_id)
        self.assertFalse(any(row["below_own_price"] for row in no_reference["items"]))
        self.assertEqual(no_reference["items"][0]["comparison"], "未设本品价")
        observations_before = local_db.amz_observations(group_id=group_id)
        changes_before = local_db.amz_changes(group_id=group_id)
        saved = await self.server.amazon_save_reference_price({"group_id":group_id,"price":"310.00"})
        self.assertEqual(saved["reference_price"], "310.00")
        self.assertEqual(local_db.amz_observations(group_id=group_id), observations_before)
        self.assertEqual(local_db.amz_changes(group_id=group_id), changes_before)
        product = local_db.amz_get_product(group_id)
        self.assertEqual((product["name"],product["asin"],product["spec_notes"],product["protected_floor"],product["price_source"]),
                         ("Legacy name",own_asin,"keep legacy spec","200.00","observed"))
        twenty = [f"B{i:09d}" for i in range(20)]
        await self.server.amazon_save_watch({"group_id":group_id,"asins":"\n".join(twenty)})
        observed = []; finished = asyncio.Event()
        async def fake_run(run_asins, job):
            observed.extend(run_asins); job["status"] = "done"; finished.set()
        with patch.object(self.server.amz, "run_patrol", side_effect=fake_run):
            await self.server.amazon_run({"group_id":group_id,"simple_mode":True})
            await asyncio.wait_for(finished.wait(), 2)
            await asyncio.sleep(0)
        self.assertEqual(len(observed), 20)
        self.assertNotIn(own_asin, observed)

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

    async def test_group_crud_and_deleted_default_keep_existing_baseline(self):
        from fastapi import HTTPException
        default_id = (await self.server.amazon_groups())["default_group_id"]
        created = await self.server.amazon_create_group({"name": " F2400 竞品 "})
        group_id = created["group"]["id"]
        await self.server.amazon_save_watch({"group_id": group_id, "asins": ASIN})
        local_db.amz_record(ASIN, {"price": "299.00"})
        self.assertEqual((await self.server.amazon_watch(default_id))["items"], [])
        self.assertEqual((await self.server.amazon_watch(group_id))["group_name"], "F2400 竞品")
        renamed = await self.server.amazon_rename_group(group_id, {"name": "主力电站竞品"})
        self.assertEqual(renamed["group"]["id"], group_id)
        await self.server.amazon_delete_group(default_id)
        fallback = await self.server.amazon_watch()
        self.assertEqual(fallback["group_id"], group_id)
        self.assertEqual(fallback["items"][0]["current"]["price"], "299.00")
        with self.assertRaises(HTTPException) as error:
            await self.server.amazon_delete_group(group_id)
        self.assertEqual(error.exception.status_code, 400)
        with self.assertRaises(HTTPException) as missing:
            await self.server.amazon_watch(default_id)
        self.assertEqual(missing.exception.status_code, 404)

    async def test_run_only_selected_group_and_group_changes_wait_for_completion(self):
        from fastapi import HTTPException
        first = (await self.server.amazon_groups())["default_group_id"]
        second = (await self.server.amazon_create_group({"name": "第二组"}))["group"]["id"]
        await self.server.amazon_save_watch({"group_id": first, "asins": ASIN})
        await self.server.amazon_save_watch({"group_id": second, "asins": OTHER})
        gate = asyncio.Event()
        observed = []

        async def fake_run(asins, job):
            observed.extend(asins)
            await gate.wait()
            job["status"] = "done"

        with patch.object(self.server.amz, "run_patrol", side_effect=fake_run):
            started = await self.server.amazon_run({"group_id": second})
            try:
                await asyncio.sleep(0)
                self.assertEqual(observed, [OTHER])
                self.assertEqual(started["group_id"], second)
                viewing_first = await self.server.amazon_watch(first)
                self.assertEqual(viewing_first["items"][0]["asin"], ASIN)
                self.assertEqual(viewing_first["running_job_id"], started["job_id"])
                for operation in (lambda: self.server.amazon_create_group({"name": "新组"}),
                                  lambda: self.server.amazon_rename_group(first, {"name": "改名"}),
                                  lambda: self.server.amazon_delete_group(first)):
                    with self.assertRaises(HTTPException) as error:
                        await operation()
                    self.assertEqual(error.exception.status_code, 409)
            finally:
                gate.set()
                await asyncio.sleep(0)
        self.assertEqual(self.server.S["jobs"][started["job_id"]]["group_name"], "第二组")

    async def test_history_is_group_scoped_and_bad_group_ids_are_rejected(self):
        from fastapi import HTTPException
        first = (await self.server.amazon_groups())["default_group_id"]
        second = (await self.server.amazon_create_group({"name": "第二组"}))["group"]["id"]
        await self.server.amazon_save_watch({"group_id": first, "asins": ASIN})
        await self.server.amazon_save_watch({"group_id": second, "asins": OTHER})
        for asin in (ASIN, OTHER):
            local_db.amz_record(asin, {"price": "299.00"})
            local_db.amz_record(asin, {"price": "279.00"})
        self.assertEqual([r["asin"] for r in (await self.server.amazon_changes(group_id=first))["items"]], [ASIN])
        self.assertEqual(len((await self.server.amazon_changes())["items"]), 2)
        for group_id in (True, "2", 0, -1):
            with self.assertRaises(HTTPException) as error:
                await self.server.amazon_save_watch({"group_id": group_id, "asins": ASIN})
            self.assertEqual(error.exception.status_code, 400)


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

    async def test_us_zip_reuses_existing_delivery_and_falls_back_to_visible_regional_input(self):
        from playwright.async_api import async_playwright
        from urllib.parse import urlsplit
        state = {"zipcode": "New York 10001", "clicks": 0, "apply": 0}

        async def fixture(route):
            if urlsplit(route.request.url).path != "/":
                await route.fulfill(content_type="text/html", body="<title>fixture</title>")
                return
            home = f'''<a id="nav-global-location-popover-link" onclick="window.locationClicks++">Deliver to {state['zipcode']}</a>
              <script>window.locationClicks=0;window.applies=0</script>'''
            if state["zipcode"] == "Malaysia":
                home += '''<div class="a-popover-wrapper" role="dialog" style="display:block">
                  <input id="GLUXZipUpdateInput" type="text" style="display:none">
                  <div id="a-popover-content-1">
                    <input aria-label="US zip code" type="text">
                    <button type="submit" onclick="window.applies++">Apply</button>
                    <button name="glowDoneButton" style="display:none"
                      onclick="document.querySelector('#nav-global-location-popover-link').textContent='Deliver to New York 10001'">Done</button>
                  </div></div><script>
                    document.querySelector('#nav-global-location-popover-link').onclick=function(){
                      window.locationClicks++;document.querySelector('[role=dialog]').style.display='block'};
                    document.querySelector('#a-popover-content-1 button[type=submit]').onclick=function(){
                      window.applies++;document.querySelector('[name=glowDoneButton]').style.display='block'};
                  </script>'''
            await route.fulfill(content_type="text/html", body=home)

        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                await page.route("**/*", fixture)
                # 若匿名上下文已有目标 ZIP，跳过弹窗及其中可能隐藏的 GLUX 输入框。
                await patrol.prepare_us_delivery(page)
                self.assertEqual(await page.evaluate("window.locationClicks"), 0)
                state["zipcode"] = "Malaysia"
                await patrol.prepare_us_delivery(page)
                self.assertEqual(await page.evaluate("window.locationClicks"), 1)
                self.assertEqual(await page.evaluate("window.applies"), 1)
                self.assertIn("10001", await page.locator("#nav-global-location-popover-link").inner_text())
            finally:
                await browser.close()

    async def test_us_zip_clicks_change_address_toaster_before_looking_for_glux_input(self):
        from playwright.async_api import async_playwright
        from urllib.parse import urlsplit
        home = '''<a id="nav-global-location-popover-link">Deliver to Malaysia</a>
          <div id="toaster" style="display:none"><span class="glow-toaster-button-submit">
            <input type="submit" value="Change Address"></span></div><div id="address"></div>
          <script>
            document.querySelector('#nav-global-location-popover-link').onclick=()=>document.querySelector('#toaster').style.display='block';
            document.querySelector('.glow-toaster-button-submit input').onclick=()=>{
              document.querySelector('#address').innerHTML='<div class="a-popover-wrapper"><input id="GLUXZipUpdateInput"><button id="GLUXZipUpdate">Apply</button></div>';
              document.querySelector('#GLUXZipUpdate').onclick=()=>document.querySelector('#nav-global-location-popover-link').textContent='Deliver to New York '+document.querySelector('#GLUXZipUpdateInput').value;
            };
          </script>'''

        async def fixture(route):
            body = home if urlsplit(route.request.url).path == "/" else "<title>fixture</title>"
            await route.fulfill(content_type="text/html", body=body)

        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                await page.route("**/*", fixture)
                await patrol.prepare_us_delivery(page)
                self.assertIn("10001", await page.locator("#nav-global-location-popover-link").inner_text())
                self.assertTrue(await page.locator("#toaster").is_visible())
                self.assertTrue(await page.locator("#GLUXZipUpdateInput").count())
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
          <span class="a-button"><input id="GLUXConfirmClose" type="submit"
          onclick="document.querySelector('#nav-global-location-popover-link').textContent='New York '+document.querySelector('#GLUXZipUpdateInput').value;this.style.display='none'">
          <button name="glowDoneButton" onclick="return false">Done</button></span>'''

        async def fixture(route):
            path = urlsplit(route.request.url).path
            product = (f'<a id="nav-global-location-popover-link">{delivery["label"]}</a>'
                       f'<span id="productTitle">Power station</span><input id="ASIN" value="{ASIN}">'
                       '<div id="corePrice_desktop">'
                       '<span class="a-price apex-price-to-pay-value"><span class="a-offscreen">$1,196.00</span></span>'
                       '</div>'
                       '<span aria-hidden="true" class="a-size-small a-color-secondary aok-align-center basisPrice">'
                       '<span class="apex-basisprice-label">List Price:</span>'
                       '<span class="a-price a-text-price apex-basisprice-value" data-a-size="s" data-a-strike="true" data-a-color="secondary">'
                       '<span class="a-offscreen">$1,329.00</span><span aria-hidden="true">$1,329.00</span></span></span>'
                       '<div id="availability">In Stock</div>')
            await route.fulfill(content_type="text/html", body=home if path == "/" else product)

        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                await page.route("**/*", fixture)
                await patrol.prepare_us_delivery(page)
                self.assertIn("10001", await page.locator("#nav-global-location-popover-link").inner_text())
                data, _ = await patrol.collect_page(page, ASIN)
                self.assertEqual(data["price"], "1196.00")
                self.assertEqual(data["reference_price"], "1329.00")
                self.assertIn("reference_discount", data["offer_types"])
                delivery["label"] = "Malaysia"
                with self.assertRaises(patrol.ObservationError):
                    await patrol.collect_page(page, ASIN)
            finally:
                await browser.close()

    async def test_amazon_tab_loads_only_the_simple_fixture_state(self):
        from playwright.async_api import async_playwright
        html = (Path(__file__).resolve().parents[1] / "web_index.html").read_text(encoding="utf-8")
        calls = []
        malicious = '<img src=x onerror="window.injected=true">'
        async def fixture(route):
            from urllib.parse import urlsplit, parse_qs
            parsed=urlsplit(route.request.url); path=parsed.path; calls.append(path)
            if path == "/": data=html; content_type="text/html"
            elif path == "/api/amazon/groups":
                payload={"items":[{"id":1,"name":"合成组","count":1}],"default_group_id":1,"running_job_id":None}
                await route.fulfill(content_type="application/json",body=json.dumps(payload)); return
            elif path == "/api/amazon/simple":
                payload={"group_id":1,"group_name":"合成组","asins":[ASIN],"reference_price":"299.00","today":"2026-10-21",
                    "items":[{"asin":ASIN,"title":malicious,"price":"$249.00","offer":"Save $10","below_own_price":True,
                    "comparison":"低于本品 $50.00","checked_at":"10-21 09:00","status":"今日已检查"}],
                    "running_job_id":None}
                await route.fulfill(content_type="application/json",body=json.dumps(payload)); return
            else:
                await route.abort(); return
            await route.fulfill(content_type=content_type,body=data)
        async with async_playwright() as pw:
            browser=await pw.chromium.launch(headless=True)
            try:
                page=await browser.new_page(viewport={"width":1280,"height":900})
                errors=[]; page.on("pageerror",lambda error:errors.append(str(error)))
                await page.route("**/*",fixture)
                await page.goto("http://mvp.test/#amazon")
                await page.wait_for_function("document.querySelector('#amzTodayBody td:first-child a') !== null")
                self.assertEqual(await page.locator("#amzPanelAmazon > .card").count(),2)
                self.assertEqual(await page.locator("#amzTodayBody img").count(),0)
                link=page.locator("#amzTodayBody tr").first.locator("td:first-child a")
                self.assertEqual(await link.inner_text(),malicious)
                self.assertEqual(await link.get_attribute("href"),f"https://www.amazon.com/dp/{ASIN}")
                self.assertNotIn(ASIN,await page.locator("#amzTodayBody tr").first.locator("td:first-child").inner_text())
                self.assertTrue(await page.locator("#amzTodayBody tr").first.evaluate("el => el.classList.contains('amz-low-row')"))
                self.assertEqual(set(calls),{"/","/api/amazon/groups","/api/amazon/simple"})
                for legacy in ("/api/amazon/comparison","/api/amazon/observations","/api/amazon/analyses","/api/amazon/analysis","/api/amazon/ai-status","/api/amazon/schedule"):
                    self.assertNotIn(legacy,calls)
                self.assertFalse(await page.evaluate("Boolean(window.injected)"))
                self.assertEqual(errors,[])
            finally:
                await browser.close()

if __name__ == "__main__":
    unittest.main()
