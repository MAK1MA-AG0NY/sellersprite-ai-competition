"""Offline comparison/analysis tests with synthetic ASINs and in-memory SQLite."""
import datetime as dt
import json
import sqlite3
import unittest
import uuid
from unittest.mock import patch

import amazon_comparison as comparison
import local_db

OWN = "B000000001"
COMP = "B000000002"
OTHER = "B000000003"


class AmazonComparisonTests(unittest.TestCase):
    def setUp(self):
        uri = f"file:amz_comparison_{uuid.uuid4().hex}?mode=memory&cache=shared"
        self.keeper = sqlite3.connect(uri, uri=True)
        self.keeper.executescript(local_db.SCHEMA)
        def connect():
            conn = sqlite3.connect(uri, uri=True)
            conn.executescript(local_db.SCHEMA)
            return conn
        self.patch = patch.object(local_db, "_conn", new=connect)
        self.patch.start()
        self.addCleanup(self.patch.stop); self.addCleanup(self.keeper.close)
        self.group = local_db.amz_get_group()
        self.now = dt.datetime.now().astimezone()

    def test_decimal_comparison_uses_fresh_available_same_market_price_and_explains_rule(self):
        local_db.amz_save_watchlist([COMP], self.group["id"])
        local_db.amz_save_product(self.group["id"], {"name": "Own", "asin": OWN, "spec_notes": "500Wh",
            "manual_price": "299.00", "protected_floor": "250.00", "currency": "USD", "price_source": "manual"})
        local_db.amz_record(COMP, {"price": "249.00", "currency": "USD", "availability": "available",
                                   "offer_types": ["lightning_deal"],
                                   "offer_evidence": [{"type": "lightning_deal", "source": "region", "text": "Lightning Deal"}]},
                            checked_at=self.now.isoformat(timespec="seconds"))
        result = comparison.build_comparison(self.group["id"], self.now)
        item = result["comparisons"][0]
        self.assertEqual(item["delta_vs_own"], "-50.00")
        self.assertEqual(item["delta_percent"], "-16.72")
        self.assertTrue(item["eligible"])
        self.assertEqual(result["advice"]["action"], "review_competitor_price")
        self.assertIn("不会自动", "".join(result["limitations"]))

    def test_stale_or_unavailable_rows_do_not_drive_recommendation(self):
        local_db.amz_save_watchlist([COMP, OTHER], self.group["id"])
        local_db.amz_save_product(self.group["id"], {"name": "Own", "asin": OWN, "spec_notes": "",
            "manual_price": "299.00", "protected_floor": None, "currency": "USD", "price_source": "manual"})
        old = (self.now - dt.timedelta(days=5)).isoformat(timespec="seconds")
        local_db.amz_record(COMP, {"price": "100.00", "currency": "USD", "availability": "available"}, checked_at=old)
        local_db.amz_record(OTHER, {"price": "110.00", "currency": "USD", "availability": "out_of_stock"}, checked_at=self.now.isoformat(timespec="seconds"))
        result = comparison.build_comparison(self.group["id"], self.now)
        self.assertTrue(all(not item["eligible"] for item in result["comparisons"]))
        self.assertEqual(result["advice"]["action"], "observe")
        self.assertIsNone(result["comparisons"][0]["delta_percent"])

    def test_observed_own_price_and_analysis_staleness_are_group_scoped(self):
        local_db.amz_save_watchlist([OWN, COMP], self.group["id"])
        local_db.amz_save_product(self.group["id"], {"name": "Own", "asin": OWN, "spec_notes": "",
            "manual_price": None, "protected_floor": None, "currency": "USD", "price_source": "observed"})
        timestamp = self.now.isoformat(timespec="seconds")
        local_db.amz_record(OWN, {"price": "299.00", "currency": "USD", "availability": "available"}, checked_at=timestamp)
        local_db.amz_record(COMP, {"price": "300.00", "currency": "USD", "availability": "available"}, checked_at=timestamp)
        comparison.build_comparison(self.group["id"], self.now)
        local_db.amz_save_analysis(self.group["id"], "local-rule", "rule", timestamp, {"x": 1}, {"recommendation": "observe"})
        later = (self.now + dt.timedelta(seconds=10)).isoformat(timespec="seconds")
        local_db.amz_record(COMP, {"price": "279.00"}, checked_at=later)
        self.assertTrue(local_db.amz_analyses(self.group["id"])[0]["is_stale"])
        self.assertTrue(comparison.build_comparison(self.group["id"], self.now)["product"]["eligible"])

    def test_ai_is_off_by_default_and_output_is_schema_validated(self):
        self.assertFalse(comparison.ai_configuration({})["enabled"])
        self.assertFalse(comparison.ai_configuration({"AMZ_AI_ENDPOINT": "http://example.com", "AMZ_AI_MODEL": "m", "AMZ_AI_API_KEY": "secret"})["enabled"])
        self.assertTrue(comparison.ai_configuration({"AMZ_AI_ENDPOINT": "https://ai.example/v1", "AMZ_AI_MODEL": "test-model", "AMZ_AI_API_KEY": "secret"})["enabled"])
        self.assertEqual(comparison._validate_ai_output({"recommendation": "Observe", "rationale": ["Evidence"], "caveats": []})["recommendation"], "Observe")
        with self.assertRaises(ValueError): comparison._validate_ai_output({"recommendation": 5, "rationale": [], "caveats": []})
        with self.assertRaises(ValueError): comparison._validate_ai_output({"recommendation": "x", "rationale": ["x" * 401], "caveats": []})


class AmazonAIAsyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_ai_helper_is_explicit_and_masks_endpoint_errors_and_secrets(self):
        settings = {"AMZ_AI_ENDPOINT":"https://ai.example/v1", "AMZ_AI_MODEL":"fixture", "AMZ_AI_API_KEY":"DO_NOT_ECHO"}
        context = {"group_id":7,"prices":[{"asin":COMP,"price":"249.00"}]}
        with patch.object(comparison, "_call_ai_sync", return_value={"recommendation":"Observe","rationale":["Price only"],"caveats":[]} ) as call:
            answer, status = await comparison.analyze_with_ai(context, settings)
        self.assertEqual(answer["recommendation"], "Observe")
        self.assertTrue(status["enabled"])
        self.assertEqual(call.call_args.args[0], context)
        self.assertNotIn("DO_NOT_ECHO", str(answer) + str(status))
        with patch.object(comparison, "_call_ai_sync", side_effect=RuntimeError("DO_NOT_ECHO")):
            answer, status = await comparison.analyze_with_ai(context, settings)
        self.assertIsNone(answer)
        self.assertIn("RuntimeError", status["reason"])
        self.assertNotIn("DO_NOT_ECHO", status["reason"])


if __name__ == "__main__":
    unittest.main()
