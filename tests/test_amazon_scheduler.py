"""Daily schedule policy tests; simulated UTC+08 clock, no real browser/service task."""
import datetime as dt
import sqlite3
import unittest
import uuid
from unittest.mock import patch

import amazon_scheduler
import local_db


class AmazonSchedulerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        uri = f"file:amz_schedule_{uuid.uuid4().hex}?mode=memory&cache=shared"
        self.keeper = sqlite3.connect(uri, uri=True)
        self.keeper.executescript(local_db.SCHEMA)
        def connect():
            c = sqlite3.connect(uri, uri=True); c.executescript(local_db.SCHEMA); return c
        self.patch = patch.object(local_db, "_conn", new=connect); self.patch.start()
        self.addAsyncCleanup(self._cleanup)
        self.calls = []
        self.group = local_db.amz_get_group()
        self.local_db = local_db

    async def _cleanup(self):
        self.patch.stop(); self.keeper.close()

    async def _start(self, group_id, source):
        self.calls.append((group_id, source)); return "job-synthetic"

    def scheduler(self, busy=False, supported=True):
        return amazon_scheduler.AmazonDailyScheduler(self._start, lambda: busy, single_process=supported)

    async def test_default_off_then_one_daily_run_dedupes(self):
        scheduler = self.scheduler()
        clock = dt.datetime(2026, 10, 21, 8, 5, tzinfo=amazon_scheduler.SHANGHAI)
        self.assertEqual(await scheduler.tick(clock), [])
        self.assertEqual(self.calls, [])
        local_db.amz_save_schedule(self.group["id"], True, "08:00")
        started = await scheduler.tick(clock)
        self.assertEqual(len(started), 1)
        self.assertEqual(self.calls, [(self.group["id"], "schedule")])
        self.assertEqual(local_db.amz_schedule_run_for(self.group["id"], "2026-10-21")["status"], "running")
        self.assertEqual(await scheduler.tick(clock), [])
        self.assertEqual(len(self.calls), 1)

    async def test_busy_tasks_wait_until_grace_then_mark_missed_without_catchup(self):
        local_db.amz_save_schedule(self.group["id"], True, "08:00")
        busy_scheduler = self.scheduler(busy=True)
        due = dt.datetime(2026, 10, 21, 8, 5, tzinfo=amazon_scheduler.SHANGHAI)
        self.assertEqual(await busy_scheduler.tick(due), [])
        self.assertIsNone(local_db.amz_schedule_run_for(self.group["id"], "2026-10-21"))
        late = dt.datetime(2026, 10, 21, 8, 11, 1, tzinfo=amazon_scheduler.SHANGHAI)
        self.assertEqual(await busy_scheduler.tick(late), [])
        self.assertEqual(local_db.amz_schedule_run_for(self.group["id"], "2026-10-21")["status"], "missed")
        self.assertEqual(self.calls, [])

    async def test_multiworker_policy_and_pause_on_manual_recovery(self):
        self.assertFalse(amazon_scheduler.AmazonDailyScheduler.supported(2))
        self.assertTrue(amazon_scheduler.AmazonDailyScheduler.supported(1))
        local_db.amz_save_schedule(self.group["id"], True, "08:00")
        scheduler = self.scheduler(supported=False)
        result = await scheduler.tick(dt.datetime(2026, 10, 21, 8, 1, tzinfo=amazon_scheduler.SHANGHAI))
        self.assertEqual(result, [])
        self.assertIn("单 worker", scheduler.last_error)
        scheduler.finish(self.group["id"], "2026-10-21", "paused", "验证码，等待人工恢复", "job")
        self.assertFalse(local_db.amz_get_schedule(self.group["id"])["enabled"])
        self.assertEqual(local_db.amz_schedule_run_for(self.group["id"], "2026-10-21")["status"], "paused")


if __name__ == "__main__":
    unittest.main()
