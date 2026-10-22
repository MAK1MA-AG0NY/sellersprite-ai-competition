"""17TRACK manual-import API tests; shared in-memory SQLite only, no browser/network."""
import sqlite3
import unittest
import uuid
from unittest.mock import AsyncMock, patch

import local_db
import web_server


class TrackingEndpointTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        uri = f"file:tracking_api_{uuid.uuid4().hex}?mode=memory&cache=shared"
        self.keeper = sqlite3.connect(uri, uri=True)

        def connect():
            c = sqlite3.connect(uri, uri=True)
            c.executescript(local_db.SCHEMA)
            return c

        self.db_patch = patch.object(local_db, "_conn", new=connect)
        self.db_patch.start()
        self.addCleanup(self.db_patch.stop)
        self.addCleanup(self.keeper.close)

    async def test_manual_pairs_batch_summary_import_and_close(self):
        added = await web_server.tracking_add_orders({"pairs": [
            {"order_id": "ORDER-TEST-1", "tracking_number": "123456789012"},
            {"order_id": "ORDER-TEST-2", "tracking_number": "123456789013"},
        ]})
        self.assertEqual(added["added"], 2)
        batch = await web_server.tracking_next_batch(limit=40)
        self.assertEqual(len(batch["items"]), 2)
        self.assertEqual(batch["total"], 2)
        self.assertEqual(set(batch["items"][0]), {"tracking_number", "order_ids"})

        summary = (
            "123456789012\t美国\t美国\t2020-08-17 12:17 HOMESTEAD, FL, US Delivered\t成功签收\t(5 天)\n"
            "======================================\n"
            "Powered by www.17track.net\n"
        )
        imported = await web_server.tracking_import_summary({"text": summary})
        self.assertEqual(imported["matched"], 1)
        self.assertEqual((await web_server.tracking_next_batch())["total"], 1)
        items = (await web_server.tracking_orders())["items"]
        first = next(item for item in items if item["order_id"] == "ORDER-TEST-1")
        second = next(item for item in items if item["order_id"] == "ORDER-TEST-2")
        self.assertEqual(first["status"], "delivered")
        self.assertFalse(second["order_closed"])
        self.assertEqual((await web_server.tracking_order_events(first["id"]))["items"],
                         web_server.track_store.events(first["id"]))
        self.assertEqual((await web_server.tracking_orders())["items"], web_server.track_store.list_orders())

    async def test_invalid_json_inputs_are_rejected_and_manual_flow_never_opens_seller_browser(self):
        from fastapi import HTTPException

        for payload in ({}, {"pairs": "ORDER\t123456789012"},
                        {"pairs": [{"order_id": "ORDER", "tracking_number": "bad"}]}):
            with self.subTest(payload=payload), self.assertRaises(HTTPException) as error:
                await web_server.tracking_add_orders(payload)
            self.assertEqual(error.exception.status_code, 400)

        with self.assertRaises(HTTPException) as error:
            await web_server.tracking_import_summary({"text": ""})
        self.assertEqual(error.exception.status_code, 400)

        with patch.object(web_server, "get_pw", new_callable=AsyncMock) as seller_browser:
            await web_server.tracking_orders()
            seller_browser.assert_not_awaited()

    async def test_next_batch_limit_is_capped_at_site_maximum(self):
        from fastapi import HTTPException
        with self.assertRaises(HTTPException) as error:
            await web_server.tracking_next_batch(limit=41)
        self.assertEqual(error.exception.status_code, 400)


if __name__ == "__main__":
    unittest.main()
