"""17TRACK helper UI regression; all browser traffic is intercepted by local fixtures."""
import json
import os
import re
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
        def parse_preview(text):
            pairs, errors = [], []
            lines = [(index, line) for index, line in enumerate(str(text).splitlines(), 1) if line.strip()]
            if lines and any(token in lines[0][1].casefold() for token in ('订单号','跟踪号','tracking_number','order_id','平台参考号')):
                lines = lines[1:]
            for line_no, line in lines:
                cells = line.split(chr(9)) if chr(9) in line else line.split(',')
                if len(cells) != 2 or not cells[0].strip() or not re.fullmatch(r'[A-Za-z0-9]{5,50}', cells[1].strip()):
                    errors.append({"line": line_no, "error": "格式错误", "text": line}); continue
                pairs.append({"order_id": cells[0].strip(), "tracking_number": cells[1].strip()})
            seen, unique = set(), []
            input_duplicates = 0
            for pair in pairs:
                key = (pair['order_id'].casefold(), pair['tracking_number'].casefold())
                if key in seen: input_duplicates += 1
                else: seen.add(key); unique.append(pair)
            existing = {(item.get('order_id','').casefold(), item.get('tracking_number','').casefold()) for item in state['orders']}
            db_duplicates = sum(1 for pair in unique if (pair['order_id'].casefold(), pair['tracking_number'].casefold()) in existing)
            new = len(unique) - db_duplicates
            hints = {pair['tracking_number']: {"carrier_hint":"auto","confidence":"low","reason":"fixture"} for pair in unique}
            return {"pairs": pairs, "invalid_rows": errors, "row_count": len(pairs)+len(errors), "valid_rows": len(pairs),
                    "valid_associations": len(pairs), "unique_associations": len(unique),
                    "unique_orders": len({p['order_id'] for p in unique}), "unique_tracking_numbers": len({p['tracking_number'] for p in unique}),
                    "input_duplicates": input_duplicates, "database_duplicates": db_duplicates, "new_associations": new,
                    "stored_associations": len(state['orders']), "capacity": state.get('max_orders',500),
                    "capacity_ok": len(state['orders']) + new <= state.get('max_orders',500), "carrier_hints": hints}

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
                query = parse_qs(parsed.query); offset = int(query.get('offset', ['0'])[0]); limit = int(query.get('limit', ['100'])[0])
                status = query.get('status', [''])[0]; carrier = query.get('carrier', [''])[0]; search = query.get('search', [''])[0].casefold()
                rows = list(state["orders"])
                if status == 'needs_update': rows = [item for item in rows if item.get('enabled', True) and item.get('status') != 'delivered']
                elif status: rows = [item for item in rows if str(item.get('status') or 'pending') == status]
                if carrier: rows = [item for item in rows if item.get('carrier') == carrier]
                if search: rows = [item for item in rows if search in item.get('order_id', '').casefold() or search in item.get('tracking_number', '').casefold()]
                state.setdefault('order_scope_queries', []).append(status)
                await route.fulfill(**self._response({"items": rows[offset:offset+limit], "total": len(rows),
                    "limit": limit, "offset": offset, "max_orders": state.get("max_orders", 500), "batch_size": 40,
                }))
            elif path == "/api/tracking/preview" and method == "POST":
                state.setdefault('preview_payloads', []).append(payload)
                await route.fulfill(**self._response(parse_preview(payload.get('text', ''))))
            elif path == "/api/tracking/import" and method == "POST":
                state["import_payload"] = payload
                preview = parse_preview(payload.get('text', '')); added, duplicates = [], 0
                seen = set()
                for pair in preview['pairs']:
                    key = (pair['order_id'].casefold(), pair['tracking_number'].casefold())
                    if key in seen or any((item.get('order_id','').casefold(), item.get('tracking_number','').casefold()) == key for item in state['orders']):
                        duplicates += 1; continue
                    seen.add(key)
                    number = pair["tracking_number"]
                    carrier = "fedex" if number.isdigit() and len(number) == 12 else ("usps" if number.isdigit() and len(number) in (20,22) else "auto")
                    item = {"id": len(state["orders"]) + 1, "order_id": pair["order_id"],
                        "tracking_number": number, "enabled": True, "status": "pending",
                        "status_text": "待查询", "last_event_at": None, "last_event_at_raw": None,
                        "last_event_text": "", "last_location": "", "last_checked_at": None, "last_error": "",
                        "timezone_unknown": False, "stale_hours": None, "risk": "pending", "order_closed": False,
                        "carrier": carrier, "carrier_hint": carrier, "carrier_confidence": "medium", "carrier_reason": "fixture"}
                    state['orders'].append(item); added.append(item)
                    if carrier != "auto":
                        batch = state.setdefault("batch", [])
                        if number.lower() not in {candidate.get("tracking_number", "").lower() for candidate in batch}:
                            batch.append({"tracking_number":number,"order_ids":[pair["order_id"]],"carrier":carrier})
                await route.fulfill(**self._response({"added": len(added), "duplicates": duplicates, "items": added,
                    "invalid_rows": preview['invalid_rows'], "ignored_invalid_rows": len(preview['invalid_rows'])}))
            elif path.startswith('/api/tracking/orders/') and path.endswith('/carrier') and method == 'POST':
                order_id = int(path.split('/')[-2]); state.setdefault('carrier_payloads', []).append((order_id,payload))
                target_numbers = set()
                for item in state['orders']:
                    if item.get('id') == order_id:
                        item['carrier'] = payload.get('carrier'); item['carrier_source'] = 'manual'
                        target_numbers.add(str(item.get('tracking_number', '')).casefold())
                for candidate in state.get('batch', []):
                    if str(order_id) in {str(value) for value in candidate.get('order_ids', [])} or str(candidate.get('tracking_number', '')).casefold() in target_numbers:
                        candidate['carrier'] = payload.get('carrier', 'auto')
                await route.fulfill(**self._response({"updated": True}))
            elif path == "/api/tracking/candidates":
                scope = parse_qs(parsed.query).get("scope", ["all"])[0]
                items = state.get("unscanned_candidates", state.get("candidates", state.get("batch", []))) if scope == "pending" else state.get("candidates", state.get("batch", []))
                counts = {carrier: sum(1 for item in items if item.get("carrier", "auto") == carrier)
                          for carrier in ("fedex", "usps", "auto")}
                state.setdefault("candidate_queries", []).append(scope)
                await route.fulfill(**self._response({"items": items, "counts": counts,
                    "total": len(items), "batch_size": 40, "scope": scope}))
            elif path == "/api/tracking/fedex-email/reviews":
                await route.fulfill(**self._response({"items": state.get("feta_reviews", []),
                    "total": len(state.get("feta_reviews", []))}))
            elif path == "/api/tracking/fedex-email/reviews/accept-all" and method == "POST":
                state.setdefault("feta_bulk_payloads", []).append(payload)
                accepted = len(state.get("feta_reviews", [])); state["feta_reviews"] = []
                await route.fulfill(**self._response({"total": accepted, "accepted": accepted,
                    "status_updated": accepted, "audit_only": 0, "unmatched": 0, "pending_after": 0}))
            elif path == "/api/tracking/fedex-email/sync" and method == "POST":
                state.setdefault("feta_sync_calls", []).append(1)
                await route.fulfill(**self._response({"processed": 0, "events_saved": 0}))
            elif path.startswith("/api/tracking/fedex-email/events/") and method == "POST":
                state.setdefault("feta_review_payloads", []).append((path, payload))
                state["feta_reviews"] = []
                await route.fulfill(**self._response({"review": {"review_state": "accepted"},
                    "integration": {"matched_orders": 1, "status_updated": 1, "duplicate": False}}))
            elif path == "/api/tracking/next-batch":
                state.setdefault("batch_queries", []).append(parse_qs(parsed.query).get("limit"))
                await route.fulfill(**self._response({"items": state.get("batch", []), "total": state.get("batch_total", 0)}))
            elif path == "/api/tracking/cleanup-preview":
                await route.fulfill(**self._response(state.get("cleanup_preview", {"orders":0,"package_rows":0})))
            elif path == "/api/tracking/clear-delivered" and method == "POST":
                state["clear_payload"] = payload
                state["orders"] = state.get("orders_after_clear", [])
                state["batch"] = state.get("batch_after_clear", state.get("batch", []))
                state["cleanup_preview"] = {"orders":0,"package_rows":0}
                await route.fulfill(**self._response(state.get("clear_result", {
                    "deleted_orders":0,"deleted_order_rows":0,"deleted_event_rows":0})))
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
            elif path == "/api/amazon/groups":
                await route.fulfill(**self._response({
                    "items": [{"id": 1, "name": "默认分组", "count": 0}],
                    "default_group_id": 1, "running_job_id": None,
                }))
            elif path == "/api/amazon/simple":
                gid = int(parse_qs(parsed.query).get("group_id", ["1"])[0])
                await route.fulfill(**self._response({
                    "group_id": gid, "group_name": "默认分组", "asins": [], "reference_price": "",
                    "items": [], "max_asins": 20, "today": "2026-06-01", "running_job_id": None,
                }))
            elif path == "/api/amazon/changes":
                await route.fulfill(**self._response({"items": []}))
            elif path == "/api/amazon/product":
                await route.fulfill(**self._response({"product":{"name":"","asin":None,"spec_notes":"","manual_price":None,"protected_floor":None,"currency":"USD","price_source":"manual","updated_at":None}}))
            elif path == "/api/amazon/comparison":
                await route.fulfill(**self._response({"group_id":1,"group_name":"默认分组","product":{"price":None,"currency":"USD","price_source":"manual"},"comparisons":[],"advice":{"label":"观察","evidence":[]},"limitations":[],"freshness_hours":72,"generated_at":"2026-06-01T00:00:00+00:00"}))
            elif path == "/api/amazon/schedule/runs":
                await route.fulfill(**self._response({"items":[]}))
            elif path == "/api/amazon/schedule":
                await route.fulfill(**self._response({"schedule":{"enabled":False,"local_time":"08:00","timezone":"Asia/Shanghai"},"today":None,"supported":True,"scheduler_error":"not enabled"}))
            elif path == "/api/amazon/observations":
                await route.fulfill(**self._response({"items":[],"total":0}))
            elif path == "/api/amazon/analyses":
                await route.fulfill(**self._response({"items":[]}))
            elif path == "/api/amazon/ai-status":
                await route.fulfill(**self._response({"enabled":False,"model":None,"reason":"AI 默认禁用"}))
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

    async def _open_page(self, server, url="http://tracking.test/#track17", dialog_handler=None):
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
        async def accept_dialog(dialog): await dialog.accept()
        page.on("dialog", dialog_handler or accept_dialog)
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
        await page.wait_for_function("document.querySelector('#amzSimpleStatus').textContent.includes('还没有竞品 ASIN')")
        self.assertFalse(await page.locator("#amzPanelAmazon").is_hidden())
        self.assertTrue(await page.locator("#amzPanelSellersprite").is_hidden())
        self.assertTrue(await page.locator("#amzPanelTracking").is_hidden())

        await page.locator("#amzTabTracking").click()
        await page.wait_for_function("document.querySelector('#trackingBatchMeta').textContent.includes('FedEx 待复制 0')")
        self.assertFalse(await page.locator("#amzPanelTracking").is_hidden())
        self.assertTrue(await page.locator("#amzPanelSellersprite").is_hidden())
        self.assertTrue(await page.locator("#amzPanelAmazon").is_hidden())
        tracking_calls = [path for path, _ in calls if path.startswith("/api/tracking/")]
        self.assertTrue(tracking_calls)
        self.assertTrue(all(path == "/" or path.startswith("/api/tracking/") for path, _ in calls[-len(tracking_calls):]))
        self.assertTrue(any(path.startswith("/api/amazon/") for path, _ in calls))
        self.assertTrue(any(path == "/api/accounts" for path, _ in calls))
        self.assertEqual(await page.locator("#trackingQueryStatus,#trackingQueryStartBtn,#trackingQueryContinueBtn,#trackingQueryCancelBtn").count(), 0)
        self.assertFalse(any(path.startswith("/api/tracking/query/") for path, _ in calls))
        self.assertEqual(external_requests, [])
        self.assertEqual(self.page_errors, [])

    async def test_feta_mail_events_wait_for_manual_review(self):
        calls, external_requests = [], []
        event = {"id": 501, "tracking_number": "TRACK001", "received_at_utc": "2026-11-02T12:00:00Z",
                 "raw_status": "Delivered", "normalized_status": "DELIVERED", "system_status": "delivered",
                 "raw_eta_text": "Nov 3", "parse_confidence": "low"}
        order = {"id": 1, "order_id": "ORDER-FETA", "tracking_number": "TRACK001", "enabled": True,
                 "status": "pending", "carrier": "fedex", "carrier_hint": "fedex", "carrier_confidence": "high",
                 "carrier_reason": "fixture"}
        state = {"orders": [order], "batch": [], "feta_reviews": [event]}
        server = self._fake_api(self._html(), state, calls, external_requests)
        page = await self._open_page(server)
        await page.wait_for_function("document.querySelector('#trackingFetaReviewsBody').textContent.includes('#501')")
        self.assertIn("接受后更新为：已送达", await page.locator("#trackingFetaReviewsBody").inner_text())
        self.assertIn("Nov 3", await page.locator("#trackingFetaReviewsBody").inner_text())
        self.assertEqual(order["status"], "pending", "pending FETA data must not auto-update the local order")
        await page.locator("#trackingFetaReviewsBody button").first.click()
        await page.wait_for_function("document.querySelector('#trackingFetaMeta').textContent.includes('待人工复核 0')")
        self.assertEqual(state["feta_review_payloads"], [
            ("/api/tracking/fedex-email/events/501/review", {"decision": "accept"})])
        self.assertEqual(state.get("feta_sync_calls", []), [])
        self.assertEqual(external_requests, [])
        self.assertEqual(self.page_errors, [])

    async def test_feta_accept_all_button_sends_one_explicit_bulk_decision(self):
        calls, external_requests = [], []
        events = [
            {"id": 511, "tracking_number": "TRACK011", "received_at_utc": "2026-11-02T12:00:00Z",
             "raw_status": "In transit", "normalized_status": "IN_TRANSIT", "system_status": "in_transit",
             "parse_confidence": "low"},
            {"id": 512, "tracking_number": "TRACK012", "received_at_utc": "2026-11-02T12:01:00Z",
             "raw_status": "No result", "normalized_status": "UNKNOWN", "system_status": None,
             "parse_confidence": "low"},
        ]
        state = {"orders": [], "batch": [], "feta_reviews": events}
        server = self._fake_api(self._html(), state, calls, external_requests)
        page = await self._open_page(server)
        await page.wait_for_function("document.querySelector('#trackingFetaAcceptAllBtn').textContent.includes('2')")
        await page.locator('#trackingFetaAcceptAllBtn').click()
        await page.wait_for_function("document.querySelector('#trackingFetaMeta').textContent.includes('待人工复核 0')")
        self.assertEqual(state.get('feta_bulk_payloads'), [{"confirm": True}])
        self.assertEqual(state.get('feta_review_payloads', []), [])
        self.assertEqual(external_requests, [])
        self.assertEqual(self.page_errors, [])

    async def test_batch_scope_switches_unscanned_and_all_needs_update(self):
        calls, external_requests = [], []
        unscanned = [
            {"tracking_number":"UNSCANNED001","order_ids":["ORDER-U1"],"carrier":"fedex"},
            {"tracking_number":"UNSCANNED002","order_ids":["ORDER-U2"],"carrier":"fedex"},
            {"tracking_number":"USPS-UNSCANNED","order_ids":["ORDER-U3"],"carrier":"usps"},
        ]
        all_open = unscanned + [
            {"tracking_number":"PREVIOUSLYCHECKED003","order_ids":["ORDER-C3"],"carrier":"fedex"},
        ]
        state = {"orders": [], "batch": [], "candidates": all_open,
                 "unscanned_candidates": unscanned, "feta_reviews": []}
        server = self._fake_api(self._html(), state, calls, external_requests)
        page = await self._open_page(server)
        await page.wait_for_function("document.querySelector('#trackingBatchMeta').textContent.includes('未扫描待查询 2')")
        self.assertEqual(state['candidate_queries'][-1], 'pending')
        self.assertEqual(await page.locator('#trackingBatchScopePending').get_attribute('aria-pressed'), 'true')
        self.assertEqual(await page.locator('#trackingBatchList li').count(), 2)

        await page.locator('#trackingBatchScopeAll').click()
        await page.wait_for_function("document.querySelector('#trackingBatchMeta').textContent.includes('全部需更新 3')")
        self.assertEqual(state['candidate_queries'][-1], 'all')
        self.assertEqual(await page.locator('#trackingBatchScopeAll').get_attribute('aria-pressed'), 'true')
        self.assertIn('PREVIOUSLYCHECKED003', await page.locator('#trackingBatchList').inner_text())
        self.assertEqual(external_requests, [])
        self.assertEqual(self.page_errors, [])

    async def test_tsv_registration_and_fedex_batches_advance_without_usps(self):
        calls, external_requests = [], []
        batch = [
            {"tracking_number": f"BATCH-{index:02d}", "order_ids": [f"ORDER-{index:02d}"], "carrier":"fedex"}
            for index in range(43)
        ]
        batch += [
            {"tracking_number":"94001118992238569284","order_ids":["USPS-ORDER-1"],"carrier":"usps"},
            {"tracking_number":"420123456789012345678901234567","order_ids":["USPS-ORDER-2"],"carrier":"usps"},
            {"tracking_number":"SYNTHETIC903","order_ids":["AUTO-ORDER"],"carrier":"auto"},
        ]
        state = {"orders": [], "batch": batch, "batch_total": 43, "max_orders": 2}
        server = self._fake_api(self._html(), state, calls, external_requests)
        page = await self._open_page(server)
        await page.wait_for_function("document.querySelector('#trackingBatchList').textContent.includes('BATCH-00')")
        self.assertIn("USPS 单独待办：2", await page.locator("#trackingUspsMeta").inner_text())
        self.assertIn("94001118992238569284", await page.locator("#trackingUspsList").inner_text())
        self.assertIn("不进入 FedEx 复制批次", await page.locator("#amzPanelTracking").inner_text())
        self.assertNotIn("94001118992238569284", await page.locator("#trackingBatchList").inner_text())
        self.assertIn("自动识别 / 冲突待人工确认：1", await page.locator("#trackingAutoMeta").inner_text())

        # A missing row on either side must never be silently zipped to another order.
        await page.locator("#trackingOrderIds").fill("ORDER-ALIGN-1\nORDER-ALIGN-2")
        await page.locator("#trackingTrackingNumbers").fill("TRACKA001")
        await page.wait_for_function("document.querySelector('#trackingPairsMeta').textContent.includes('对齐问题')")
        self.assertTrue(await page.locator("#trackingAddBtn").is_disabled())
        self.assertIn("第 2 行", await page.locator("#trackingPreviewErrors").inner_text())
        self.assertFalse(state.get("preview_payloads"))

        # Count pair rows against MAX_ORDERS, even when those rows repeat the same order ID.
        await page.locator("#trackingOrderIds").fill("ORDER-SAME\nORDER-SAME\nORDER-SAME")
        await page.locator("#trackingTrackingNumbers").fill("TRACKA001\nTRACKB002\nTRACKC003")
        await page.wait_for_function("document.querySelector('#trackingPairsMeta').textContent.includes('超出本地保存容量')")
        self.assertTrue(await page.locator("#trackingAddBtn").is_disabled())
        self.assertIsNone(state.get("pairs_payload"))

        new_text = "ORDER-NEW\t123456789098\nORDER-NEW-2\t123456789099"
        mapped_rows = [line.split("\t") for line in new_text.splitlines()]
        await page.locator("#trackingOrderIds").fill("\n".join(row[0] for row in mapped_rows))
        await page.locator("#trackingTrackingNumbers").fill("\n".join(row[1] for row in mapped_rows))
        await page.wait_for_function("document.querySelector('#trackingPairsMeta').textContent.includes('有效 2')")
        await page.locator("#trackingAddBtn").click()
        await page.wait_for_function("document.querySelector('#trackingOrdersBody').textContent.includes('ORDER-NEW-2')")
        self.assertEqual(state["import_payload"], {"text": new_text, "only_valid_rows": True})

        await page.locator("#trackingCopyBtn").click()
        await page.wait_for_function("document.querySelector('#trackingBatchMeta').textContent.includes('已复制第 1 组 40')")
        copied_lines = (await page.evaluate("window.__trackingClipboard")).splitlines()
        self.assertEqual(len(copied_lines), 40)
        self.assertEqual(len(set(copied_lines)), 40)
        self.assertNotIn("BATCH-40", copied_lines)
        self.assertNotIn("94001118992238569284", copied_lines)
        await page.locator("#trackingBatchRefreshBtn").click()
        await page.wait_for_function("document.querySelector('#trackingBatchList').textContent.includes('BATCH-40')")
        second_group = (await page.locator("#trackingBatchList").inner_text()).splitlines()
        self.assertTrue(any(line.startswith("BATCH-42") for line in second_group))
        self.assertTrue(any(line.startswith("123456789098") for line in second_group))
        self.assertTrue(any(line.startswith("123456789099") for line in second_group))
        self.assertFalse(any(line.startswith("BATCH-00") for line in second_group))
        await page.locator("#trackingBatchRefreshBtn").click()
        await page.wait_for_function("document.querySelector('#trackingBatchMeta').textContent.includes('没有新的本地未查批次')")
        self.assertIn("本轮没有新的本地未查 FedEx 批次", await page.locator("#trackingBatchList").inner_text())
        self.assertEqual(await page.locator("#trackingBatchList li").count(),1)
        await page.locator("#trackingBatchRefreshBtn").click()
        await page.wait_for_function("document.querySelector('#trackingBatchList').textContent.includes('BATCH-00')")
        self.assertEqual(await page.locator("#trackingBatchRefreshBtn").inner_text(),"下一组 / 下 40 个")
        self.assertFalse(any(path.startswith("/api/tracking/query/") for path, _ in calls))
        self.assertEqual(external_requests, [])
        self.assertEqual(self.page_errors, [])

    async def test_carrier_override_removes_usps_from_fedex_copy_list(self):
        calls, external_requests = [], []
        order = {"id":1,"order_id":"ORDER-LOCAL","tracking_number":"TRACK001","enabled":True,"status":"pending",
            "status_text":"待查询","last_event_at":None,"last_event_at_raw":None,"last_event_text":"","last_location":"",
            "last_checked_at":None,"last_error":"","timezone_unknown":False,"stale_hours":None,"risk":"pending",
            "order_closed":False,"carrier":"auto","carrier_hint":"fedex","carrier_confidence":"medium","carrier_reason":"prefix candidate"}
        state = {"orders":[order],"batch":[{"tracking_number":"TRACK001","order_ids":["ORDER-LOCAL"],"carrier":"fedex",
            "carrier_hint":"fedex","carrier_confidence":"medium","carrier_reason":"prefix candidate"}],"batch_total":1}
        server = self._fake_api(self._html(), state, calls, external_requests)
        page = await self._open_page(server)
        await page.wait_for_function("document.querySelector('#trackingBatchList').textContent.includes('TRACK001')")
        copy_links = page.locator('#trackingOrdersBody button.track-copy-link')
        await copy_links.nth(0).click()
        self.assertEqual(await page.evaluate('window.__trackingClipboard'), 'ORDER-LOCAL')
        await copy_links.nth(1).click()
        self.assertEqual(await page.evaluate('window.__trackingClipboard'), 'TRACK001')
        await page.locator('#trackingOrdersBody select').first.select_option('usps')
        await page.wait_for_function("document.querySelector('#trackingNotice').textContent.includes('承运商已保存')")
        self.assertEqual(state['carrier_payloads'], [(1,{"carrier":"usps"})])
        self.assertNotIn("TRACK001", await page.locator('#trackingBatchList').inner_text())
        self.assertIn("TRACK001", await page.locator('#trackingUspsList').inner_text())
        self.assertTrue(await page.locator('#trackingCopyBtn').is_disabled())
        self.assertEqual(await page.locator("#trackingQueryStatus,#trackingQueryStartBtn,#trackingQueryContinueBtn,#trackingQueryCancelBtn").count(), 0)
        self.assertFalse(any(path.startswith("/api/tracking/query/") for path, _ in calls))
        self.assertEqual(external_requests, [])
        self.assertEqual(self.page_errors, [])

    async def test_delivered_cleanup_is_one_click_and_refreshes_local_queues(self):
        calls, external_requests = [], []
        orders = [
            {"id":1,"order_id":"CLEAN-UI","tracking_number":"CLEANTRACK01","enabled":True,
             "status":"delivered","pkg_status_text":"Delivered","risk":"delivered","order_closed":True},
            {"id":2,"order_id":"KEEP-UI","tracking_number":"KEEPTRACK001","enabled":True,
             "status":"in_transit","pkg_status_text":"In Transit","risk":"normal","order_closed":False},
        ]
        state = {"orders":orders,"orders_after_clear":[orders[1]],
            "batch":[{"tracking_number":"123456789012","order_ids":["KEEP-UI"],"carrier":"fedex"}],
            "batch_after_clear":[{"tracking_number":"123456789012","order_ids":["KEEP-UI"],"carrier":"fedex"}],
            "cleanup_preview":{"orders":1,"package_rows":1},
            "clear_result":{"deleted_orders":1,"deleted_order_rows":1,"deleted_event_rows":1}}
        dialogs = []
        async def unexpected_dialog(dialog):
            dialogs.append(dialog.message)
            await dialog.dismiss()
        server = self._fake_api(self._html(), state, calls, external_requests)
        page = await self._open_page(server, dialog_handler=unexpected_dialog)
        await page.wait_for_function("!document.querySelector('#trackingClearDeliveredBtn').disabled")
        button = page.locator("#trackingClearDeliveredBtn")
        self.assertIn("清除已妥投运单（1）", await button.inner_text())

        await button.click()
        await page.wait_for_function("document.querySelector('#trackingOrdersBody').textContent.includes('KEEP-UI') && !document.querySelector('#trackingOrdersBody').textContent.includes('CLEAN-UI')")
        await page.wait_for_function("document.querySelector('#trackingNotice').textContent.includes('清理完成：1 条已妥投运单，涉及 1 个订单')")
        self.assertEqual(dialogs, [])
        self.assertEqual(state["clear_payload"], {})
        self.assertEqual(len([1 for path, method in calls if path == "/api/tracking/clear-delivered" and method == "POST"]),1)
        self.assertTrue(await button.is_disabled())
        self.assertIn("0 个订单", await page.locator("#trackingCleanupMeta").inner_text())
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
            "batch": [{"tracking_number": "TRACK-OLD-BATCH", "order_ids": ["ORDER-OLD"], "carrier":"fedex"}],
            "batch_after_import": [{"tracking_number": "TRACK-NEW-BATCH", "order_ids": ["ORDER-48"], "carrier":"fedex"}],
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
