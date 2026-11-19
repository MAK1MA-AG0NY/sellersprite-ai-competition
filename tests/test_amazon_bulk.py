"""Bulk Amazon-group route tests; no browser, Amazon network, or database is used."""
import asyncio
import unittest
from unittest.mock import patch

import web_server


class AmazonBulkRouteTests(unittest.IsolatedAsyncioTestCase):
    async def test_preview_and_confirmed_run_deduplicate_asins_across_saved_groups(self):
        groups = [
            {"id": 1, "name": "F3600", "count": 2},
            {"id": 2, "name": "F2400", "count": 2},
            {"id": 3, "name": "F1800", "count": 0},
        ]
        members = {
            1: [{"asin": "B000000001"}, {"asin": "B000000002"}],
            2: [{"asin": "B000000002"}, {"asin": "B000000003"}],
            3: [],
        }
        calls = []
        tasks = set()
        state = {"jobs": {}, "amazon_job_id": None}

        async def fake_run_patrol(asins, job):
            calls.append(list(asins))
            job.update(status="done", done=len(asins), rows=2, changed=1, baselines=1,
                       unchanged=1, failed=0, detail="synthetic run complete")

        with patch.object(web_server, "S", state), \
             patch.object(web_server, "AMZ_TASKS", tasks), \
             patch.object(web_server.local_db, "amz_groups", return_value=groups), \
             patch.object(web_server.local_db, "amz_watchlist", side_effect=lambda group_id: members[group_id]), \
             patch.object(web_server.amz, "run_patrol", side_effect=fake_run_patrol):
            preview = await web_server.amazon_run_all_preview()
            self.assertEqual((preview["saved_group_count"], preview["group_count"], preview["empty_group_count"]), (3, 2, 1))
            self.assertEqual((preview["membership_count"], preview["unique_asin_count"], preview["duplicates_skipped"]), (4, 3, 1))
            self.assertEqual(preview["group_names"], ["F3600", "F2400"])
            self.assertNotIn("unique_asins", preview, "preview must not echo the ASIN list")
            self.assertTrue(preview["can_run"])

            response = await web_server.amazon_run_all({"confirm": True, "plan_hash": preview["plan_hash"]})
            self.assertEqual(response["job_type"], "all_groups")
            self.assertEqual(response["unique_asin_count"], 3)
            self.assertEqual(len(tasks), 1)
            await asyncio.gather(*list(tasks))

        self.assertEqual(calls, [["B000000001", "B000000002", "B000000003"]])
        job = state["jobs"][response["job_id"]]
        self.assertEqual(job["status"], "done")
        self.assertEqual(state["amazon_job_id"], response["job_id"])
        self.assertFalse(tasks)

    async def test_bulk_run_requires_a_fresh_confirmed_preview_and_respects_asin_cap(self):
        from fastapi import HTTPException
        groups = [{"id": 1, "name": "F3600", "count": 1}]
        members = {1: [{"asin": "B000000001"}]}
        state = {"jobs": {}, "amazon_job_id": None}
        with patch.object(web_server, "S", state), \
             patch.object(web_server.local_db, "amz_groups", return_value=groups), \
             patch.object(web_server.local_db, "amz_watchlist", side_effect=lambda group_id: members[group_id]), \
             patch.object(web_server.amz, "run_patrol") as run:
            with self.assertRaises(HTTPException) as error:
                await web_server.amazon_run_all({"confirm": True, "plan_hash": "stale"})
            self.assertEqual(error.exception.status_code, 409)
            run.assert_not_called()
            with self.assertRaises(HTTPException) as error:
                await web_server.amazon_run_all({"confirm": False, "plan_hash": "stale"})
            self.assertEqual(error.exception.status_code, 400)
            run.assert_not_called()

            groups[:] = [{"id": group_id, "name": f"GROUP-{group_id}", "count": 20}
                         for group_id in range(1, 12)]
            members.clear()
            members.update({group_id: [{"asin": f"B{(group_id - 1) * 20 + index:09d}"}
                                       for index in range(20)] for group_id in range(1, 12)})
            preview = await web_server.amazon_run_all_preview()
            self.assertFalse(preview["can_run"])
            with self.assertRaises(HTTPException) as error:
                await web_server.amazon_run_all({"confirm": True, "plan_hash": preview["plan_hash"]})
            self.assertEqual(error.exception.status_code, 413)
            run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
