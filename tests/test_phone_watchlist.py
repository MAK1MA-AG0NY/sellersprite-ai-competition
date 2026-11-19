"""Offline regression tests for the curated FOSSIBOT phone watchlist.

The tests compile only the three pure watchlist functions from web_server.py;
no FastAPI server, Playwright browser, SellerSprite API, or quota-consuming job
is started.
"""
from __future__ import annotations

import ast
import copy
import json
import re
import unittest
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "web_server.py"
MANIFEST = ROOT / "plans" / "FOSSIBOT_US_在售手机ASIN_资料筛选.json"
CURATED_SOURCE = "自家·手机（在售清单）"
PHONE_BLOCK_SOURCE = "自家·手机"
PHONE_COMPETITOR_BLOCK = "TOP·户外三防手机"


def generated_asin(prefix: str, number: int) -> str:
    value = f"{prefix}{number:05d}"
    assert len(value) == 10, value
    return value


def snapshot(asin: str, brand: str, title: str, units: int = 0) -> dict:
    return {"asin": asin, "brand": brand, "title": title, "totalUnits": units}


def make_fixture(manifest: dict) -> tuple[dict, list[dict]]:
    """Reproduce the recorded 248-item baseline without reading the live DB."""
    selected = {asin for group in manifest["groups"] for asin in group["asins"]}
    missing = set(manifest["db_comparison"]["missing_asins"])
    db_selected = selected - missing
    extra_phone_asins = manifest["db_comparison"]["db_phone_outside_selection"]
    rows = [snapshot(a, "FOSSIBOT", "FOSSIBOT rugged phone") for a in sorted(db_selected)]
    rows.extend(snapshot(a, "FOSSIBOT", "FOSSIBOT rugged phone") for a in extra_phone_asins)

    # 51 non-phone FOSSIBOT products: 30 power stations, 19 solar panels, 2 other.
    for i in range(1, 52):
        if i <= 30:
            title = "FOSSIBOT portable power station"
        elif i <= 49:
            title = "FOSSIBOT solar panel"
        else:
            title = "FOSSIBOT accessory"
        rows.append(snapshot(generated_asin("B0TST", i), "FOSSIBOT", title))

    # Uncached categories fall back to TOP by title/units. The phone TOP is
    # present but blocked in the recorded baseline; portable stations stay live.
    rows.extend(snapshot(generated_asin("B0CMP", i), "Competitor",
                         "portable power station competitor", 1000 - i)
                for i in range(1, 51))
    rows.extend(snapshot(generated_asin("B0RUG", i), "Competitor",
                         "rugged phone competitor", 500 - i)
                for i in range(1, 51))

    cached_counts = {
        "太阳能板": 50,
        "户外发电机": 43,
        "太阳能板BSR": 24,
        "户外电源BSR": 30,
    }
    categories = [
        {"name": "便携电站", "tokens": ["power station", "solar generator"], "bsr_n": 50},
        {"name": "太阳能板", "tokens": ["solar panel"], "bsr_n": 50},
        {"name": "户外三防手机", "tokens": ["rugged phone"], "bsr_n": 50},
        {"name": "户外发电机", "tokens": [], "bsr_n": 50},
        {"name": "太阳能板BSR", "tokens": [], "bsr_n": 50},
        {"name": "户外电源BSR", "tokens": [], "bsr_n": 50},
    ]
    bsr_cache = {}
    counter = 1
    for category, count in cached_counts.items():
        items = []
        for _ in range(count):
            items.append({"asin": generated_asin("B0BSR", counter),
                          "title": f"{category} competitor", "units": 1})
            counter += 1
        bsr_cache[category] = {
            "asins": [item["asin"] for item in items],
            "items": items,
            "ts": "fixture",
        }
    config = {
        "manual": [],
        "categories": categories,
        "bsr_cache": bsr_cache,
        "blocked": [PHONE_BLOCK_SOURCE, PHONE_COMPETITOR_BLOCK],
    }
    return config, rows


def load_watchlist_functions(config: dict, rows: list[dict]):
    """Compile production pure functions against inert local dependencies."""
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"), filename=str(SOURCE))
    names = {"_own_source", "_watchlist_all_items", "_watchlist_items"}
    definitions = [node for node in tree.body
                   if isinstance(node, ast.FunctionDef) and node.name in names]
    if {node.name for node in definitions} != names:
        raise AssertionError("production watchlist functions were not found")
    module = ast.Module(body=definitions, type_ignores=[])
    namespace = {
        "re": re,
        "BSR_N_DEFAULT": 50,
        "_load_watchlist": lambda: config,
        "local_db": SimpleNamespace(latest=lambda: rows),
    }
    exec(compile(module, str(SOURCE), "exec"), namespace)
    return namespace


class PhoneWatchlistTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
        cls.selected = {asin for group in cls.manifest["groups"]
                        for asin in group["asins"]}
        cls.records = [
            {"asin": asin, "model": group["model"],
             "source_checked_at": cls.manifest["source_checked_at"]}
            for group in cls.manifest["groups"] for asin in group["asins"]
        ]
        cls.config, cls.rows = make_fixture(cls.manifest)

    def functions(self, config=None):
        return load_watchlist_functions(config or copy.deepcopy(self.config), self.rows)

    def test_legacy_config_keeps_the_248_item_baseline(self):
        # No own_phones field: pre-change configuration remains compatible.
        funcs = self.functions()
        all_items, own_count = funcs["_watchlist_all_items"]()
        active, own_active = funcs["_watchlist_items"]()
        self.assertEqual(len({item["asin"] for item in active}), 248)
        self.assertEqual(own_count, 158)
        self.assertEqual(own_active, 51)
        self.assertFalse(self.selected & {item["asin"] for item in active})
        self.assertEqual(len({item["asin"] for item in all_items}), len(all_items))

    def test_curated_selection_unions_to_348_and_preserves_other_groups(self):
        config = copy.deepcopy(self.config)
        config["own_phones"] = copy.deepcopy(self.records)
        funcs = self.functions(config)
        before, _ = self.functions()["_watchlist_items"]()
        all_items, own_count = funcs["_watchlist_all_items"]()
        active, own_active = funcs["_watchlist_items"]()
        all_by_asin = {item["asin"]: item for item in all_items}
        active_asins = {item["asin"] for item in active}

        self.assertEqual(len(self.records), 100)
        self.assertEqual(len(self.selected), 100)
        self.assertEqual(len(all_items), len(all_by_asin))
        self.assertEqual(own_count, 173)
        self.assertEqual(own_active, 151)
        self.assertEqual(len(active), 348)
        self.assertEqual(active_asins,
                         {item["asin"] for item in before} | self.selected)
        self.assertEqual({all_by_asin[a]["source"] for a in self.selected},
                         {CURATED_SOURCE})
        self.assertTrue(self.selected <= active_asins)
        self.assertFalse(set(self.manifest["db_comparison"]["db_phone_outside_selection"])
                         & active_asins)
        self.assertIn(PHONE_COMPETITOR_BLOCK, config["blocked"])

    def test_blocking_curated_group_does_not_leak_asins_back_through_db_or_manual(self):
        config = copy.deepcopy(self.config)
        config["own_phones"] = copy.deepcopy(self.records)
        config["manual"] = [self.records[0]["asin"]]
        funcs = self.functions(config)
        all_items, _ = funcs["_watchlist_all_items"]()
        self.assertEqual(next(i for i in all_items if i["asin"] == self.records[0]["asin"])
                         ["source"], CURATED_SOURCE)
        config["blocked"].append(CURATED_SOURCE)
        active, _ = funcs["_watchlist_items"]()
        before, _ = self.functions()["_watchlist_items"]()
        self.assertEqual({i["asin"] for i in active}, {i["asin"] for i in before})
        self.assertFalse(self.selected & {i["asin"] for i in active})

    def test_duplicate_case_and_invalid_curated_entries_are_normalized_or_ignored(self):
        config = copy.deepcopy(self.config)
        valid = copy.deepcopy(self.records[0])
        config["own_phones"] = [
            {**valid, "asin": valid["asin"].lower()},
            valid,
            {"asin": "NOT-AN-ASIN", "model": "invalid"},
            "B0CJM6QLVG",
        ]
        all_items, _ = self.functions(config)["_watchlist_all_items"]()
        matches = [item for item in all_items if item["asin"] == valid["asin"]]
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]["source"], CURATED_SOURCE)
        self.assertEqual(matches[0]["asin"], valid["asin"])

    def test_both_routine_entrypoints_use_the_shared_filtered_watchlist(self):
        tree = ast.parse(SOURCE.read_text(encoding="utf-8"), filename=str(SOURCE))
        functions = {node.name: node for node in tree.body
                     if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
        for name in ("watchlist_run", "run_routine"):
            calls = {node.func.id for node in ast.walk(functions[name])
                     if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)}
            self.assertIn("_watchlist_items", calls, name)


if __name__ == "__main__":
    unittest.main()
