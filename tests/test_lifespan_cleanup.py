"""App shutdown cancels owned Amazon tasks without services or network."""
import asyncio
import unittest
from unittest.mock import patch

import web_server


class LifespanCleanupTests(unittest.IsolatedAsyncioTestCase):
    async def test_shutdown_does_not_start_scheduler_and_cancels_owned_tasks(self):
        cleaned = asyncio.Event()
        blocker = asyncio.Event()
        async def browser_job():
            try: await blocker.wait()
            finally: cleaned.set()
        old_jobs = web_server.S["jobs"]
        old_job_id = web_server.S.get("amazon_job_id")
        old_tasks = set(web_server.AMZ_TASKS)
        web_server.S["jobs"] = {"synthetic": {"type":"amazon_price","status":"running"}}
        web_server.S["amazon_job_id"] = "synthetic"
        web_server.AMZ_TASKS.clear()
        task = asyncio.create_task(browser_job()); web_server.AMZ_TASKS.add(task)
        try:
            with patch.object(web_server.amazon_scheduler, "AmazonDailyScheduler") as scheduler:
                async with web_server._lifespan(web_server.app):
                    await asyncio.sleep(0)
                self.assertTrue(task.done())
                self.assertTrue(cleaned.is_set())
                self.assertEqual(web_server.S["jobs"]["synthetic"]["status"], "error")
                self.assertFalse(hasattr(web_server, "TRACKING_QUERY"))
                self.assertIsNone(web_server.AMZ_SCHEDULER)
                scheduler.assert_not_called()
        finally:
            web_server.S["jobs"] = old_jobs
            web_server.S["amazon_job_id"] = old_job_id
            web_server.AMZ_TASKS.clear(); web_server.AMZ_TASKS.update(old_tasks)


if __name__ == "__main__":
    unittest.main()
