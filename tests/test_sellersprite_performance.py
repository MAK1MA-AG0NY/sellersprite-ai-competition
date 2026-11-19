import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import ads_insights as adv
import sellersprite_deep_dive as sd
import web_server


class SellerspritePerformanceTests(unittest.TestCase):
    def test_account_profile_requests_run_concurrently(self):
        async def exercise():
            with tempfile.TemporaryDirectory() as directory:
                main_state = Path(directory) / "main.json"
                member_state = Path(directory) / "member.json"
                main_state.touch()
                member_state.touch()
                pages = {"main": object(), "member": object()}
                entered = set()
                both_started = asyncio.Event()

                async def fake_api_get(page, path):
                    role = next(name for name, value in pages.items() if value is page)
                    entered.add(role)
                    if len(entered) == 2:
                        both_started.set()
                    await asyncio.wait_for(both_started.wait(), timeout=0.5)
                    return {"data": {"nickname": role, "memberLevel": "G"}}

                with (
                    patch.object(web_server, "STATE_FILE", main_state),
                    patch.object(web_server, "MEMBER_STATE_FILE", member_state),
                    patch.object(web_server, "get_main_page", new=AsyncMock(return_value=pages["main"])),
                    patch.object(web_server, "get_vip_page", new=AsyncMock(return_value=pages["member"])),
                    patch.object(web_server.sd, "api_get", new=fake_api_get),
                    patch.object(web_server.local_db, "stats", return_value={"snapshots": 0, "asins": 0, "batches": 0}),
                ):
                    result = await web_server.accounts()

                self.assertTrue(result["main"]["logged_in"])
                self.assertTrue(result["member"]["logged_in"])
                self.assertEqual(entered, {"main", "member"})

        asyncio.run(exercise())

    def test_main_and_vip_pages_use_lightweight_same_origin_document(self):
        async def exercise():
            urls = []

            class FakePage:
                async def goto(self, url, wait_until=None):
                    urls.append(url)

            class FakeContext:
                def set_default_timeout(self, timeout):
                    pass

                async def new_page(self):
                    return FakePage()

                async def close(self):
                    pass

            class FakeBrowser:
                async def new_context(self, **kwargs):
                    return FakeContext()

            old_main, old_vip = web_server.S["main_page"], web_server.S["vip_page"]
            web_server.S["main_page"] = web_server.S["vip_page"] = None
            try:
                with (
                    patch.object(web_server, "get_browser", new=AsyncMock(return_value=FakeBrowser())),
                    patch.object(web_server, "_check_login", new=AsyncMock()),
                ):
                    await web_server.get_main_page()
                    await web_server.get_vip_page()
                expected = f"{web_server.BASE}/v3/api/client"
                self.assertEqual(urls, [expected, expected])
            finally:
                web_server.S["main_page"], web_server.S["vip_page"] = old_main, old_vip

        asyncio.run(exercise())

    def test_keyword_fetch_records_api_duration_and_http_status(self):
        async def exercise():
            class FakePage:
                async def evaluate(self, expression, url):
                    return {"s": 200, "j": {"code": "OK", "data": {
                        "items": [{"asin": "B012345678", "keyword": "solar"}], "total": 1}}}

            metrics = {}
            rows = await adv.fetch_group_keywords(
                FakePage(), "ad-id", 1, 2, asin="B012345678", metrics=metrics)
            self.assertEqual(len(rows), 1)
            self.assertGreaterEqual(metrics["api_ms"][0], 0)
            self.assertEqual(metrics["api_statuses"], [200])

        asyncio.run(exercise())

    def test_group_fetch_separates_api_and_debug_write_timings(self):
        async def exercise():
            class FakePage:
                async def evaluate(self, expression, url):
                    return {"s": 200, "j": {"code": "OK", "data": {
                        "groups": [
                            {"asin": "B012345678", "adId": "own"},
                            {"asin": "B098765432", "adId": "foreign"},
                        ], "adCount": 2, "campaignCount": 2, "maxKeywordsCount": 5}}}

            with tempfile.TemporaryDirectory() as directory:
                metrics = {}
                with patch.object(adv, "HERE", Path(directory)):
                    result = await adv.fetch_ads_groups(FakePage(), "B012345678", metrics=metrics)
                self.assertEqual([group["adId"] for group in result["groups"]], ["own"])
                self.assertEqual(result["skippedGroups"], 1)
                self.assertEqual(len(metrics["api_ms"]), 1)
                self.assertEqual(metrics["api_statuses"], [200])
                self.assertGreaterEqual(metrics["debug_write_ms"], 0)
                self.assertTrue((Path(directory) / "ads_debug" / "xds_p1_B012345678.json").exists())

        asyncio.run(exercise())

    def test_extract_asins_reports_fetch_and_parse_metrics(self):
        async def exercise():
            response = {"data": {"taskStatus": "COMPLETED", "content": json.dumps([
                {"asin": "b012345678", "parent": "b098765432"}, {"asin": "B012345678"}
            ])}}
            timings = {}
            with patch.object(sd, "api_get", new=AsyncMock(return_value=response)):
                asins, rows = await sd.extract_asins(object(), "task", timings=timings)
            self.assertEqual(asins, ["B012345678"])
            self.assertEqual(len(rows), 2)
            self.assertGreaterEqual(timings["fetch_ms"], 0)
            self.assertGreaterEqual(timings["parse_ms"], 0)
            self.assertGreater(timings["payload_kb"], 0)

        asyncio.run(exercise())

    def test_task_asins_writes_compact_snapshot_and_returns_timings(self):
        async def exercise():
            with tempfile.TemporaryDirectory() as directory:
                old_state = {key: web_server.S.get(key) for key in
                             ("tasks", "asins", "ai_rows", "current_task", "current_market", "current_seed")}
                web_server.S["tasks"] = []
                with (
                    patch.object(web_server, "OUT_DIR", Path(directory)),
                    patch.object(web_server, "get_main_page", new=AsyncMock(return_value=object())),
                    patch.object(web_server.sd, "extract_asins", new=AsyncMock(
                        return_value=(["B012345678"], [{"asin": "B012345678"}]))),
                ):
                    result = await web_server.task_asins("task1234567890")
                saved = Path(directory) / "asins_task12345678.json"
                payload = saved.read_text(encoding="utf-8")
                self.assertNotIn("\n", payload)
                self.assertEqual(json.loads(payload)["asins"], ["B012345678"])
                self.assertEqual(result["count"], 1)
                self.assertIn("save_ms", result["timings"])
                for key, value in old_state.items():
                    web_server.S[key] = value

        asyncio.run(exercise())

    def test_ads_auto_vip_initialization_failure_terminates_job(self):
        async def exercise():
            job_id = "performance-vip-failure"
            old_job = web_server.S["jobs"].get(job_id)
            web_server.S["jobs"][job_id] = {"status": "running", "log": [], "done": 0}
            try:
                with (
                    patch.object(web_server.local_db, "latest", return_value=[
                        {"asin": "B012345678", "totalUnits": 1}]),
                    patch.object(web_server, "get_vip_page", new=AsyncMock(
                        side_effect=TimeoutError("VIP init timeout"))),
                ):
                    await web_server.run_ads_auto(job_id, 1, 1, 1)
                job = web_server.S["jobs"][job_id]
                self.assertEqual(job["status"], "error")
                self.assertEqual(job["phase"], "任务失败")
                self.assertIn("VIP init timeout", job["detail"])
            finally:
                if old_job is None:
                    web_server.S["jobs"].pop(job_id, None)
                else:
                    web_server.S["jobs"][job_id] = old_job

        asyncio.run(exercise())

    def test_ads_progress_distinguishes_week_work_from_page_requests(self):
        html = Path(web_server.WEB_INDEX).read_text(encoding="utf-8")
        self.assertIn("est_week_jobs", html)
        self.assertIn("max_keyword_pages", html)
        self.assertIn("个组周任务", html)

    def test_login_confirmation_renders_response_without_global_account_refetch(self):
        html = Path(web_server.WEB_INDEX).read_text(encoding="utf-8")
        login_function = html.split("async function login(role, confirm) {", 1)[1].split(
            "async function loadHistory()", 1)[0]
        self.assertIn("renderAccountCard(role", login_function)
        self.assertNotIn("refreshAccounts()", login_function)

    def test_history_dom_fallback_waits_for_render_instead_of_sleeping(self):
        class FakePage:
            wait_timeout = None

            async def goto(self, *args, **kwargs):
                return None

            async def wait_for_function(self, expression, timeout):
                self.wait_timeout = timeout
                return None

            async def evaluate(self, expression):
                return []

        async def exercise():
            page = FakePage()
            self.assertEqual(await sd.dom_history(page), [])
            self.assertEqual(page.wait_timeout, 3000)

        asyncio.run(exercise())


if __name__ == "__main__":
    unittest.main()
