"""极简竞品巡查 Chromium 回归；全部 HTTP 由本地合成 fixture 拦截。"""
import json
import os
import re
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlsplit


@unittest.skipUnless(os.environ.get("AMZ_BROWSER_TESTS") == "1", "可选本地 Chromium 极简巡查测试")
class AmazonGroupUITests(unittest.IsolatedAsyncioTestCase):
    async def test_two_groups_today_price_offer_low_mark_and_minimal_load_chain(self):
        from playwright.async_api import async_playwright
        root = Path(__file__).resolve().parents[1]
        html = (root / "web_index.html").read_text(encoding="utf-8")
        malicious = '<img src=x onerror="window.injected=true">'
        groups = {1: "A 竞品组", 2: "B 竞品组"}
        members = {1: ["B000000001", "B000000002", "B000000003", "B000000004", "B000000005", "B000000006", "B000000007", "B000000010", "B000000011", "B000000012"],
                   2: ["B000000008"]}
        reference = {1: "299.00", 2: "399.00"}
        today = "2026-10-21"
        now = "10-21 09:15"
        sample_rows = {
            1: [
                {"asin":"B000000001","title":malicious,"price":"$249.00","offer":"Save $20 coupon","below_own_price":True,"comparison":"低于本品 $50.00","checked_at":now,"status":"今日已检查"},
                {"asin":"B000000002","title":"Synthetic equal-price product","price":"$299.00","offer":"今日未发现明确优惠","below_own_price":False,"comparison":"与本品相同","checked_at":now,"status":"今日已检查"},
                {"asin":"B000000003","title":"Synthetic higher-price product","price":"$329.00","offer":"今日优惠未读取","below_own_price":False,"comparison":"高于本品 $30.00","checked_at":now,"status":"今日已检查"},
                {"asin":"B000000004","title":"Yesterday only","price":"—","offer":"—","below_own_price":False,"comparison":"—","checked_at":"10-20 15:30 · 非今天","status":"今天未巡查"},
                {"asin":"B000000005","title":"Synthetic failure","price":"—","offer":"—","below_own_price":False,"comparison":"无法判断","checked_at":now,"status":"采集失败","error":"本地模拟失败"},
                {"asin":"B000000006","title":"Synthetic out of stock","price":"—","offer":"—","below_own_price":False,"comparison":"不比较（缺货）","checked_at":now,"status":"今日缺货 / 不可购买"},
                {"asin":"B000000007","title":"Synthetic EUR","price":"€60.00 EUR","offer":"Save €5","below_own_price":False,"comparison":"不比较（非 USD）","checked_at":now,"status":"非 USD，不比较"},
                {"asin":"B000000010","title":"Synthetic unread coupon","price":"$250.00","offer":"本次优惠未读取","below_own_price":True,"comparison":"低于本品 $49.00","checked_at":now,"status":"今日已检查（库存未确认）"},
                {"asin":"B000000011","price":"—","offer":"—","below_own_price":False,"comparison":"—","checked_at":"—","status":"今天未巡查"},
                {"asin":"B000000012","title":"Synthetic unavailable","price":"—","offer":"缺货时优惠不可确认","below_own_price":False,"comparison":"不比较（缺货）","checked_at":now,"status":"今日缺货 / 不可购买"},
            ],
            2: [{"asin":"B000000008","title":"B group synthetic product","price":"$350.00","offer":"Save $5 coupon","below_own_price":True,"comparison":"低于本品 $49.00","checked_at":now,"status":"今日已检查"}],
        }
        calls = []
        state = {"next_id":3,"job_calls":0,"run_payload":None,"saved_asins":[],
                 "bulk_preview_calls":0,"bulk_payload":None,"bulk_job_calls":0}

        def group_items():
            return [{"id":gid,"name":name,"count":len(members[gid])} for gid,name in groups.items()]

        def simple_state(gid):
            return {"group_id":gid,"group_name":groups[gid],"asins":members[gid],"max_asins":20,
                    "reference_price":reference[gid],"items":sample_rows[gid],"today":today,
                    "running_job_id":"simple-fixture-job" if state["run_payload"] and state["job_calls"] < 2 else None,
                    "running_group_id":state["run_payload"].get("group_id") if state["run_payload"] else None,
                    "running_status":"running" if state["run_payload"] and state["job_calls"] < 2 else None}

        async def fixture(route):
            request = route.request
            parsed = urlsplit(request.url)
            path, method = parsed.path, request.method
            calls.append((path,method))
            payload = request.post_data_json if method in {"POST","PATCH"} else {}
            payload = payload or {}
            if path == "/":
                await route.fulfill(content_type="text/html",body=html); return
            if path == "/api/amazon/groups":
                if method == "POST":
                    gid=state["next_id"]; state["next_id"]+=1; groups[gid]=payload["name"]; members[gid]=[]; reference[gid]=""
                    data={"group":{"id":gid,"name":groups[gid],"count":0},"items":group_items()}
                else: data={"items":group_items(),"default_group_id":1,"running_job_id":None}
            elif path.startswith("/api/amazon/groups/"):
                gid=int(path.rsplit("/",1)[1])
                if method == "PATCH":
                    groups[gid]=payload["name"]; data={"group":{"id":gid,"name":groups[gid]},"items":group_items()}
                elif method == "DELETE":
                    del groups[gid]; del members[gid]; del reference[gid]
                    data={"items":group_items(),"group_id":next(iter(groups))}
                else: await route.abort(); return
            elif path == "/api/amazon/simple":
                gid=int(parse_qs(parsed.query).get("group_id",[1])[0]); data=simple_state(gid)
            elif path == "/api/amazon/reference-price":
                gid=payload["group_id"]; reference[gid]=payload["price"] or ""; data={"group_id":gid,"reference_price":reference[gid],"currency":"USD"}
            elif path == "/api/amazon/watch":
                gid=payload["group_id"]; values=payload["asins"]
                members[gid]=list(dict.fromkeys(x.upper() for x in re.split(r"[,，\s]+",values) if x))
                existing={row["asin"] for row in sample_rows[gid]}
                sample_rows[gid]=[row for row in sample_rows[gid] if row["asin"] in members[gid]]
                for asin in members[gid]:
                    if asin not in existing:
                        sample_rows[gid].append({"asin":asin,"title":"Today not checked","price":"—","offer":"—",
                            "below_own_price":False,"comparison":"—","checked_at":"—","status":"今天未巡查"})
                state["saved_asins"]=members[gid]; data={"group_id":gid,"group_name":groups[gid],"items":[],"max_asins":20}
            elif path == "/api/amazon/run":
                state["run_payload"]=payload; state["job_calls"]=0
                data={"job_id":"simple-fixture-job","group_id":payload["group_id"],"group_name":groups[payload["group_id"]]}
            elif path == "/api/amazon/run-all/preview":
                state["bulk_preview_calls"] += 1
                member_count = sum(len(values) for values in members.values())
                unique_count = len({asin for values in members.values() for asin in values})
                data={"group_count":sum(bool(values) for values in members.values()),
                      "saved_group_count":len(groups),"empty_group_count":sum(not values for values in members.values()),
                      "membership_count":member_count,"unique_asin_count":unique_count,
                      "duplicates_skipped":member_count-unique_count,"group_names":list(groups.values()),
                      "limit":200,"can_run":unique_count>0,"plan_hash":"synthetic-plan"}
            elif path == "/api/amazon/run-all":
                state["bulk_payload"]=payload; state["bulk_job_calls"]=0
                data={"job_id":"bulk-fixture-job","job_type":"all_groups","group_count":2,
                      "membership_count":2,"unique_asin_count":2,"duplicates_skipped":0}
            elif path == "/api/job/bulk-fixture-job":
                state["bulk_job_calls"] += 1
                data={"job_type":"all_groups","status":"running" if state["bulk_job_calls"]==1 else "done",
                      "group_count":2,"done":1 if state["bulk_job_calls"]==1 else 2,"total":2,
                      "failed":0,"detail":"synthetic only"}
            elif path == "/api/job/simple-fixture-job":
                state["job_calls"]+=1
                data={"status":"running" if state["job_calls"]==1 else "done","group_id":state["run_payload"]["group_id"],
                      "group_name":groups[state["run_payload"]["group_id"]],"done":1,"total":len(members[state["run_payload"]["group_id"]]),
                      "detail":"synthetic only"}
            else:
                await route.abort(); return
            await route.fulfill(content_type="application/json",body=json.dumps(data))

        async with async_playwright() as pw:
            browser=await pw.chromium.launch(headless=True)
            try:
                page=await browser.new_page(viewport={"width":1440,"height":1000})
                page.set_default_timeout(7000)
                async def accept_dialog(dialog): await dialog.accept()
                page.on("dialog", accept_dialog)
                errors=[]; page.on("pageerror",lambda error:errors.append(str(error)))
                await page.route("**/*",fixture)
                await page.goto("http://fixture.test/#amazon")
                try:
                    await page.wait_for_function("document.querySelector('#amzTodayBody td:first-child a') !== null")
                except Exception as error:
                    self.fail(f"Amazon result failed to load: {error}; calls={calls}; page_errors={errors}; status={await page.locator('#amzSimpleStatus').inner_text()}")
                self.assertEqual(set(calls), {("/","GET"),("/api/amazon/groups","GET"),("/api/amazon/simple","GET")})
                self.assertEqual(await page.locator("#amzPanelAmazon > .card").count(),2)
                self.assertFalse(await page.locator("#amzGroupEditor").get_attribute("open"))
                self.assertFalse(await page.locator("#amzGroupManagement").get_attribute("open"))
                self.assertFalse(await page.locator("#amzSimpleGroupRename").is_visible())
                self.assertFalse(await page.locator("#amzSimpleGroupDelete").is_visible())
                self.assertEqual(await page.locator("#amzSimpleGroups button").count(),2)
                self.assertEqual(await page.locator("#amzSimpleGroups button").nth(0).get_attribute("aria-pressed"),"true")
                await page.locator("#amzGroupManagement summary").click()
                self.assertTrue(await page.locator("#amzSimpleGroupRename").is_visible())
                self.assertTrue(await page.locator("#amzSimpleGroupDelete").is_visible())
                await page.locator("#amzGroupManagement summary").click()
                await page.locator("#amzGroupEditor summary").click()
                size = await page.locator("#amzSimpleAsins").evaluate("el => ({height:parseFloat(getComputedStyle(el).height), min:parseFloat(getComputedStyle(el).minHeight), max:parseFloat(getComputedStyle(el).maxHeight), resize:getComputedStyle(el).resize})")
                self.assertTrue(90 <= size["height"] <= 140, f"textarea computed constraints: {size}")
                self.assertTrue(90 <= size["min"] <= size["max"] <= 140)
                self.assertEqual(size["resize"],"vertical")
                await page.locator("#amzGroupEditor summary").click()
                for selector in ("#amzComparisonBody","#amzAIStatus","#amzScheduleEnabled","#amzObservationsBody","#amzHistoryBody","#amzTrendSvg","#amzProductName"):
                    self.assertEqual(await page.locator(selector).count(),0)
                first_row = page.locator("#amzTodayBody tr").nth(0)
                link = first_row.locator("td:first-child a")
                self.assertEqual(await link.count(),1)
                self.assertEqual(await link.inner_text(),malicious)
                self.assertEqual(await link.evaluate("el => el.textContent"),malicious)
                self.assertEqual(await link.locator("*").count(),0)
                self.assertEqual(await link.get_attribute("href"),"https://www.amazon.com/dp/B000000001")
                self.assertEqual(await link.get_attribute("target"),"_blank")
                self.assertEqual(await link.get_attribute("rel"),"noopener noreferrer")
                for index in range(8):
                    self.assertEqual(await page.locator("#amzTodayBody tr").nth(index).locator("td:first-child a").count(),1)
                self.assertNotIn("B000000001",await first_row.locator("td:first-child").inner_text())
                self.assertEqual(await page.locator("#amzTodayBody img").count(),0)
                self.assertEqual(await page.locator("#amzTodayBody script").count(),0)
                self.assertFalse(await page.evaluate("Boolean(window.injected)"))
                fallback = page.locator("#amzTodayBody tr").nth(8).locator("td:first-child")
                self.assertEqual(await fallback.inner_text(),"B000000011")
                self.assertEqual(await fallback.locator("a").count(),0)
                self.assertEqual(await page.locator("#amzTodayBody tr").nth(0).get_attribute("class"),"amz-low-row")
                self.assertEqual(await page.locator("#amzTodayBody tr").nth(7).get_attribute("class"),"amz-low-row")
                for index in (1,2,3,4,5,6,8,9):
                    self.assertNotEqual(await page.locator("#amzTodayBody tr").nth(index).get_attribute("class"),"amz-low-row")
                self.assertIn("今日缺货 / 不可购买",await page.locator("#amzTodayBody tr").nth(9).inner_text())
                unread_row = page.locator("#amzTodayBody tr").nth(7)
                self.assertIn("本次优惠未读取",await unread_row.inner_text())
                self.assertIn("低于本品",await unread_row.inner_text())
                self.assertIn("库存未确认",await unread_row.inner_text())
                self.assertIn("今天未巡查",await page.locator("#amzTodayBody").inner_text())
                self.assertIn("采集失败",await page.locator("#amzTodayBody").inner_text())
                self.assertIn("今日优惠未读取",await page.locator("#amzTodayBody").inner_text())
                screenshot_title="ALLPOWERS P1800 Portable Power Station · 1024Wh / 1800W"
                sample_rows[1][0]["title"]=screenshot_title
                await page.reload()
                await page.wait_for_function("document.querySelector('#amzTodayBody td:first-child a')?.textContent === 'ALLPOWERS P1800 Portable Power Station · 1024Wh / 1800W'")
                screenshot_link=page.locator("#amzTodayBody tr").first.locator("td:first-child a")
                self.assertEqual(await screenshot_link.inner_text(),screenshot_title)
                self.assertEqual(await screenshot_link.get_attribute("href"),"https://www.amazon.com/dp/B000000001")
                self.assertNotIn(malicious,await page.locator("#amzTodayBody").inner_text())
                artifacts=root/"plans"/"artifacts"; artifacts.mkdir(parents=True,exist_ok=True)
                screenshot=artifacts/"竞品巡查极简版-合成数据.png"
                await page.screenshot(path=str(screenshot),full_page=True)
                await page.set_viewport_size({"width":390,"height":844})
                self.assertTrue(await page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1"))
                self.assertTrue(await page.locator("#amzSimpleRun").is_visible())
                await page.set_viewport_size({"width":1440,"height":1000})

                await page.locator("#amzSimpleRun").click()
                await page.wait_for_function("document.querySelector('#amzSimpleStatus').textContent.includes('巡查完成')",timeout=6500)
                self.assertEqual(state["run_payload"],{"group_id":1,"simple_mode":True})
                await page.locator("#amzSimpleGroups button").nth(1).click()
                await page.wait_for_function("document.querySelector('#amzReferencePrice').value === '399.00'")
                self.assertEqual(await page.locator("#amzTodayBody tr").count(),1)
                self.assertEqual(await page.locator("#amzTodayBody tr").first.get_attribute("class"),"amz-low-row")
                await page.locator("#amzSimpleGroups button").nth(0).click()
                await page.wait_for_function("document.querySelector('#amzReferencePrice').value === '299.00'")
                await page.locator("#amzReferencePrice").fill("310.00")
                await page.locator("#amzReferenceSave").click()
                await page.wait_for_function("document.querySelector('#amzReferenceSave').disabled")
                await page.locator("#amzSimpleGroups button").nth(1).click()
                await page.wait_for_function("document.querySelector('#amzReferencePrice').value === '399.00'")
                await page.locator("#amzSimpleGroups button").nth(0).click()
                await page.wait_for_function("document.querySelector('#amzReferencePrice').value === '310.00'")
                await page.locator("#amzGroupEditor summary").click()
                await page.locator("#amzSimpleAsins").fill("B000000009")
                await page.locator("#amzSimpleAsinsSave").click()
                await page.wait_for_function("document.querySelector('#amzSimpleAsinsSave').disabled")
                self.assertEqual(members[1],["B000000009"])
                await page.locator("#amzSimpleGroups button").nth(1).click()
                await page.wait_for_function("document.querySelector('#amzSimpleAsins').value === 'B000000008'")
                self.assertEqual(members[2],["B000000008"])
                await page.locator("#amzSimpleGroups button").nth(0).click()
                await page.wait_for_function("document.querySelector('#amzSimpleAsins').value === 'B000000009'")
                await page.locator('#amzSimpleRunAll').click()
                await page.wait_for_function("document.querySelector('#amzSimpleStatus').textContent.includes('全部分组巡查完成')", timeout=6500)
                self.assertEqual(state['bulk_preview_calls'], 1)
                self.assertEqual(state['bulk_payload'], {"confirm": True, "plan_hash": "synthetic-plan"})
                self.assertEqual(state['bulk_job_calls'], 2)
                self.assertTrue(all(path not in {"/api/amazon/comparison","/api/amazon/observations","/api/amazon/analyses","/api/amazon/analysis","/api/amazon/analysis/context","/api/amazon/ai-status","/api/amazon/schedule","/api/amazon/schedule/runs"} for path,_ in calls))
                self.assertEqual(errors,[])
                self.assertTrue(screenshot.exists())
            finally:
                await browser.close()


if __name__ == "__main__":
    unittest.main()
