"""命名分组与缺货的本地 Chromium 交互回归；HTTP 全部 stub，不访问真实站点或数据库。"""
import json
import os
import re
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlsplit


@unittest.skipUnless(os.environ.get("AMZ_BROWSER_TESTS") == "1", "可选本地 Chromium 分组交互测试")
class AmazonGroupUITests(unittest.IsolatedAsyncioTestCase):
    async def test_named_tabs_member_removal_task_isolation_and_stock(self):
        from playwright.async_api import async_playwright
        html = (Path(__file__).resolve().parents[1] / "web_index.html").read_text(encoding="utf-8")
        first_asin, missing_asin = "B0HBPT6N7H", "B0D17CW6KK"
        groups = {1: "默认分组"}
        members = {1: [first_asin]}
        state = {"next_id": 2, "running": False, "complete": False, "job_group": None}
        calls, prompts = [], []
        malicious = '<img src=x onerror="window.groupInjected=true">'
        records = {
            first_asin: {"asin": first_asin, "current": {"title": malicious, "price": "299.00", "availability": "available"},
                         "last_status": "baseline", "last_checked": "2026-01-01", "last_error": ""},
            missing_asin: {"asin": missing_asin, "current": {"title": "Competitor", "price": "249.00", "availability": "unavailable"},
                           "last_status": "unchanged", "last_checked": "2026-01-01",
                           "last_error": "当前未展示购买价，表中金额为上次有效值"},
        }

        def group_items():
            return [{"id": gid, "name": name, "count": len(members[gid])} for gid, name in groups.items()]

        def watch(gid):
            return {"group_id": gid, "group_name": groups[gid],
                    "items": [records[a] for a in members[gid]], "max_asins": 20,
                    "running_job_id": "group-job" if state["running"] else None}

        async def server(route):
            request = route.request
            parsed = urlsplit(request.url)
            path, method = parsed.path, request.method
            calls.append((path, method))
            payload = request.post_data_json if method in ("POST", "PATCH") else {}
            payload = payload or {}
            if path == "/":
                await route.fulfill(content_type="text/html", body=html)
                return
            if path == "/api/amazon/groups":
                if method == "POST":
                    gid = state["next_id"]
                    state["next_id"] += 1
                    groups[gid] = payload["name"].strip()
                    members[gid] = []
                    data = {"group": {"id": gid, "name": groups[gid], "count": 0}, "items": group_items()}
                else:
                    data = {"items": group_items(), "default_group_id": min(groups),
                            "running_job_id": "group-job" if state["running"] else None}
            elif path.startswith("/api/amazon/groups/"):
                gid = int(path.rsplit("/", 1)[1])
                if method == "PATCH":
                    groups[gid] = payload["name"].strip()
                    data = {"group": {"id": gid, "name": groups[gid], "count": len(members[gid])}, "items": group_items()}
                elif method == "DELETE":
                    del groups[gid]
                    del members[gid]
                    data = {"items": group_items(), "group_id": min(groups)}
                else:
                    await route.abort()
                    return
            elif path == "/api/amazon/watch":
                query = parse_qs(parsed.query)
                gid = payload.get("group_id", int(query.get("group_id", [min(groups)])[0]))
                if type(gid) is not int:
                    await route.fulfill(status=400, content_type="application/json", body='{"detail":"group_id must be integer"}')
                    return
                if method == "POST":
                    values = payload["asins"]
                    values = re.split(r"[,，\s]+", values) if isinstance(values, str) else values
                    members[gid] = list(dict.fromkeys(a.upper() for a in values if a))
                data = watch(gid)
            elif path == "/api/amazon/changes":
                gid = int(parse_qs(parsed.query).get("group_id", [min(groups)])[0])
                data = {"items": [{"id": 1, "asin": a, "checked_at": "2026-01-01", "changes": {
                    "availability": {"old": "available", "new": "unavailable"}}}
                    for a in members[gid] if a == missing_asin]}
            elif path == "/api/amazon/run":
                state["running"], state["complete"] = True, False
                state["job_group"] = payload["group_id"]
                gid = state["job_group"]
                data = {"job_id": "group-job", "group_id": gid, "group_name": groups[gid]}
            elif path == "/api/job/group-job":
                gid = state["job_group"]
                state["running"] = not state["complete"]
                data = {"status": "done" if state["complete"] else "running", "group_id": gid, "group_name": groups[gid],
                        "done": 1, "total": 1, "baselines": 0, "changed": 0, "unchanged": 1, "failed": 0,
                        "log": ["fixture only"], "results": [{"asin": missing_asin, "status": "unchanged",
                            "changes": {}, "availability": "unavailable", "message": "缺货：页面明确提示暂时无货；保留金额为历史价"}]}
            else:
                await route.abort()
                return
            await route.fulfill(content_type="application/json", body=json.dumps(data))

        async def dialog_handler(dialog):
            if dialog.type == "prompt":
                if not prompts:
                    raise AssertionError("Unexpected group naming prompt")
                await dialog.accept(prompts.pop(0))
            else:
                await dialog.accept()

        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            try:
                page = await browser.new_page(viewport={"width": 1440, "height": 1100})
                page.set_default_timeout(7000)
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.on("dialog", dialog_handler)
                await page.route("**/*", server)
                await page.goto("http://groups.test/#amazon")
                await page.wait_for_function("document.querySelector('#amzCurrentBody').textContent.includes('299.00')")
                self.assertTrue(all(path == "/" or path.startswith("/api/amazon/") for path, _ in calls))

                prompts.append("F2400 竞品")
                await page.locator("#amzGroupAddBtn").click()
                await page.wait_for_function("!document.querySelector('#amzAsins').disabled && document.querySelector('#amzAsins').value === ''")
                await page.locator("#amzAsins").fill(f"{missing_asin}\n{first_asin}")
                await page.locator("#amzSaveBtn").click()
                await page.wait_for_function("document.querySelector('#amzCurrentBody').textContent.includes('249.00')")
                self.assertEqual(members[2], [missing_asin, first_asin])
                self.assertIn("缺货", await page.locator("#amzCurrentBody").inner_text())
                self.assertIn("历史价", await page.locator("#amzCurrentBody").inner_text())

                prompts.append("电站 A 竞品")
                await page.locator("#amzGroupRenameBtn").click()
                await page.wait_for_function("document.querySelector('#amzGroupTabs').textContent.includes('电站 A 竞品')")
                await page.locator(f'[data-amz-remove="{first_asin}"]').click()
                await page.wait_for_function(f"!document.querySelector('#amzCurrentBody').textContent.includes('{first_asin}')")
                self.assertEqual(members[1], [first_asin])
                self.assertEqual(members[2], [missing_asin])

                await page.locator("#amzRunBtn").click()
                await page.wait_for_function("document.querySelector('#amzProgressText').textContent.includes('1/1')")
                self.assertEqual(state["job_group"], 2)
                self.assertIn("缺货", await page.locator("#amzResultsBody").inner_text())
                await page.locator("#amzGroupTab0").click()
                await page.wait_for_function(f"document.querySelector('#amzCurrentBody').textContent.includes('{first_asin}')")
                self.assertNotIn(missing_asin, await page.locator("#amzResultsBody").inner_text())
                self.assertTrue(state["running"])
                await page.locator("#amzGroupTab1").click()
                await page.wait_for_function(f"document.querySelector('#amzCurrentBody').textContent.includes('{missing_asin}')")
                state["complete"] = True
                await page.evaluate("amzPollPriceJob()")
                await page.wait_for_function("!document.querySelector('#amzGroupDeleteBtn').disabled")
                self.assertIn("缺货", await page.locator("#amzResultsBody").inner_text())
                await page.locator("#amzGroupDeleteBtn").click()
                await page.wait_for_function("document.querySelectorAll('#amzGroupTabs [role=tab]').length === 1")
                self.assertEqual(groups, {1: "默认分组"})
                self.assertEqual(members[1], [first_asin])
                self.assertIn(missing_asin, records)  # 删除分类不删除既有采集记录。
                self.assertTrue(await page.locator("#amzGroupDeleteBtn").is_disabled())
                prompts.append(malicious)
                await page.locator("#amzGroupRenameBtn").click()
                await page.wait_for_function("document.querySelector('#amzGroupTabs').textContent.includes('<img')")
                self.assertEqual(await page.locator("#amzGroupTabs img, #amzCurrentBody img").count(), 0)
                self.assertFalse(await page.evaluate("Boolean(window.groupInjected)"))
                self.assertEqual(errors, [])
            finally:
                await browser.close()


if __name__ == "__main__":
    unittest.main()
