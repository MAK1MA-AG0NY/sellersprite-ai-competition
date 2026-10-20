"""Amazon 分组离线回归：真实 SQLite 共享内存，不读业务库或登录态。"""
import json
import sqlite3
import unittest
import uuid
from unittest.mock import patch

import local_db

ASIN = "B000000001"
OTHER = "B000000002"
THIRD = "B000000003"


class AmazonGroupTests(unittest.TestCase):
    def setUp(self):
        uri = f"file:amz_groups_{uuid.uuid4().hex}?mode=memory&cache=shared"
        self.keeper = sqlite3.connect(uri, uri=True)
        self.keeper.executescript(local_db.SCHEMA)
        self.addCleanup(self.keeper.close)

        def connect():
            c = sqlite3.connect(uri, uri=True)
            # 与原巡查测试的 _conn patch 相同；分组表不依赖修改全局 SCHEMA。
            c.executescript(local_db.SCHEMA)
            return c

        db_patch = patch.object(local_db, "_conn", new=connect)
        db_patch.start()
        self.addCleanup(db_patch.stop)

    def seed_watch(self, asin, enabled=1, current=None):
        with self.keeper:
            self.keeper.execute(
                "INSERT INTO amz_watch VALUES (?, ?, ?, ?, ?, ?)",
                (asin, enabled, json.dumps(current or {}, ensure_ascii=False),
                 "2026-01-01T10:00:00+00:00", "changed", "旧检查备注"))

    def seed_change(self, asin):
        with self.keeper:
            self.keeper.execute(
                "INSERT INTO amz_changes(asin, checked_at, changes) VALUES (?, ?, ?)",
                (asin, "2026-01-01T10:00:00+00:00",
                 json.dumps({"price": {"old": "299.00", "new": "279.00"}})))

    def asins(self, group_id=None):
        return [item["asin"] for item in local_db.amz_watchlist(group_id)]

    def enabled(self):
        return dict(self.keeper.execute("SELECT asin, enabled FROM amz_watch"))

    def change_price(self, asin):
        local_db.amz_record(asin, {"price": "299.00"})
        local_db.amz_record(asin, {"price": "279.00"})

    def test_group_schema_is_lazy_and_does_not_change_global_connection_behavior(self):
        local_db.save_batch("old", "seller-task", 1, [{"asin": ASIN, "price": 500}])
        self.assertEqual(local_db.amz_changes(), [])
        tables = {row[0] for row in self.keeper.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertNotIn("amz_groups", tables)
        self.assertNotIn("amz_group_asins", tables)
        group = local_db.amz_get_group()
        self.assertEqual(group["name"], "默认分组")
        self.assertEqual(group["count"], 0)
        self.assertTrue(self.keeper.execute("SELECT created_at FROM amz_groups").fetchone()[0])
        self.assertEqual(local_db.latest()[0]["price"], 500)

    def test_legacy_enabled_list_current_metadata_and_history_migrate_without_loss(self):
        current = {"price": "279.00", "coupon": "Save $20", "title": "旧商品"}
        self.seed_watch(ASIN, current=current)
        self.seed_watch(OTHER)
        self.seed_watch(THIRD, enabled=0, current={"price": "199.00"})
        self.seed_change(ASIN)
        self.seed_change(THIRD)
        local_db.save_batch("old", "seller-task", 1, [{"asin": THIRD, "price": 500}])
        before_watch = self.keeper.execute("SELECT * FROM amz_watch ORDER BY rowid").fetchall()
        before_history = self.keeper.execute("SELECT * FROM amz_changes ORDER BY id").fetchall()

        groups = local_db.amz_groups()
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["name"], "默认分组")
        self.assertEqual(groups[0]["count"], 2)
        self.assertEqual(self.asins(), [ASIN, OTHER])
        item = local_db.amz_watchlist()[0]
        self.assertEqual(item, {"asin": ASIN, "url": f"https://www.amazon.com/dp/{ASIN}",
                                "current": current, "last_checked": "2026-01-01T10:00:00+00:00",
                                "last_status": "changed", "last_error": "旧检查备注"})
        self.assertEqual(self.keeper.execute("SELECT * FROM amz_watch ORDER BY rowid").fetchall(), before_watch)
        self.assertEqual(self.keeper.execute("SELECT * FROM amz_changes ORDER BY id").fetchall(), before_history)
        self.assertEqual([row["asin"] for row in local_db.amz_changes()], [THIRD, ASIN])
        self.assertEqual([row["asin"] for row in local_db.amz_changes(group_id=groups[0]["id"])], [ASIN])
        self.assertEqual(local_db.latest()[0]["price"], 500)

    def test_repeated_initialization_does_not_resurrect_removed_legacy_members(self):
        self.seed_watch(ASIN, current={"price": "279.00"})
        group = local_db.amz_get_group()
        self.assertEqual(local_db.amz_save_watchlist([]), [])
        self.assertEqual(self.enabled()[ASIN], 0)
        # 即使旧兼容标志残留为 1，也不能被下一次连接当作迁移源。
        with self.keeper:
            self.keeper.execute("UPDATE amz_watch SET enabled=1 WHERE asin=?", (ASIN,))
        for _ in range(3):
            self.assertEqual(local_db.amz_groups(), [{**group, "count": 0}])
            self.assertEqual(local_db.amz_get_group()["id"], group["id"])
            self.assertEqual(local_db.amz_watchlist(), [])
            self.assertEqual(local_db.amz_changes(group_id=group["id"]), [])
        self.assertEqual(self.keeper.execute("SELECT COUNT(*) FROM amz_group_asins").fetchone()[0], 0)

    def test_create_rename_order_duplicate_names_and_last_group_protection(self):
        first = local_db.amz_get_group()
        second = local_db.amz_create_group("  竞品组  ")
        third = local_db.amz_create_group("竞品组")
        self.assertEqual(second["name"], "竞品组")
        self.assertEqual(second["count"], 0)
        self.assertEqual(local_db.amz_groups(), [first, second, third])
        renamed = local_db.amz_rename_group(second["id"], "  储能组  ")
        self.assertEqual(renamed, {**second, "name": "储能组"})
        self.assertIsNone(local_db.amz_delete_group(first["id"]))
        self.assertIsNone(local_db.amz_delete_group(third["id"]))
        with self.assertRaises(ValueError):
            local_db.amz_delete_group(second["id"])
        self.assertEqual(local_db.amz_groups(), [renamed])

    def test_names_strip_validate_length_and_allow_parameterized_literal_text(self):
        first = local_db.amz_get_group()
        for name in ("", " \n\t ", "x" * 61, None, 3, [], b"bytes"):
            with self.subTest(name=name):
                with self.assertRaises(ValueError):
                    local_db.amz_create_group(name)
                with self.assertRaises(ValueError):
                    local_db.amz_rename_group(first["id"], name)
        self.assertEqual(local_db.amz_groups(), [first])
        boundary = local_db.amz_create_group("  " + "名" * 60 + "  ")
        self.assertEqual(boundary["name"], "名" * 60)
        literal = "组'; DROP TABLE amz_watch; --"
        self.assertEqual(local_db.amz_rename_group(boundary["id"], literal)["name"], literal)
        self.assertEqual(local_db.amz_create_group(literal)["name"], literal)
        local_db.amz_save_watchlist([ASIN])
        self.assertEqual(self.asins(), [ASIN])

    def test_missing_group_raises_without_modifying_existing_members(self):
        group = local_db.amz_get_group()
        local_db.amz_save_watchlist([ASIN])
        for missing in (group["id"] + 100, "1 OR 1=1"):
            operations = (lambda: local_db.amz_get_group(missing),
                          lambda: local_db.amz_watchlist(missing),
                          lambda: local_db.amz_save_watchlist([OTHER], missing),
                          lambda: local_db.amz_rename_group(missing, "新名字"),
                          lambda: local_db.amz_delete_group(missing),
                          lambda: local_db.amz_changes(group_id=missing))
            for operation in operations:
                with self.subTest(missing=missing, operation=operation), self.assertRaises(ValueError):
                    operation()
        self.assertEqual(self.asins(), [ASIN])
        self.assertEqual(self.enabled(), {ASIN: 1})

    def test_same_asin_in_two_groups_uses_one_baseline_and_one_history(self):
        first = local_db.amz_get_group()
        second = local_db.amz_create_group("共享")
        local_db.amz_save_watchlist([ASIN], first["id"])
        self.change_price(ASIN)
        saved = local_db.amz_save_watchlist([ASIN], second["id"])
        self.assertEqual(saved[0]["current"]["price"], "279.00")
        self.assertEqual(local_db.amz_record(ASIN, {"price": "279.00"})["status"], "unchanged")
        self.assertEqual(local_db.amz_watchlist(first["id"]), local_db.amz_watchlist(second["id"]))
        self.assertEqual(local_db.amz_changes(group_id=first["id"]), local_db.amz_changes(group_id=second["id"]))
        self.assertEqual(len(local_db.amz_changes()), 1)
        self.assertEqual(self.keeper.execute("SELECT COUNT(*) FROM amz_watch").fetchone()[0], 1)
        local_db.amz_save_watchlist([], first["id"])
        self.assertEqual(self.enabled()[ASIN], 1)
        self.assertEqual(self.asins(second["id"]), [ASIN])
        self.assertEqual(local_db.amz_watchlist(second["id"])[0]["current"]["price"], "279.00")

    def test_deleting_group_preserves_exclusive_and_shared_baselines_and_history(self):
        first = local_db.amz_get_group()
        second = local_db.amz_create_group("待删除")
        local_db.amz_save_watchlist([ASIN], first["id"])
        local_db.amz_save_watchlist([ASIN, OTHER], second["id"])
        self.change_price(ASIN)
        self.change_price(OTHER)
        history = local_db.amz_changes()
        values = self.keeper.execute(
            "SELECT asin, current_data, last_checked, last_status, last_error FROM amz_watch ORDER BY rowid").fetchall()
        local_db.amz_delete_group(second["id"])
        self.assertEqual(self.asins(), [ASIN])
        self.assertEqual(self.enabled(), {ASIN: 1, OTHER: 0})
        self.assertEqual(self.keeper.execute(
            "SELECT asin, current_data, last_checked, last_status, last_error FROM amz_watch ORDER BY rowid").fetchall(), values)
        self.assertEqual(local_db.amz_changes(), history)
        self.assertEqual(self.keeper.execute(
            "SELECT COUNT(*) FROM amz_group_asins WHERE group_id=?", (second["id"],)).fetchone()[0], 0)
        for _ in range(3):
            self.assertEqual(self.asins(), [ASIN])
            self.assertEqual(len(local_db.amz_groups()), 1)
        restored = local_db.amz_save_watchlist([ASIN, OTHER])
        self.assertEqual(restored[1]["current"]["price"], "279.00")
        self.assertEqual(local_db.amz_record(OTHER, {"price": "279.00"})["status"], "unchanged")
        self.assertEqual(local_db.amz_changes(), history)

    def test_saving_replaces_only_selected_group_and_enabled_tracks_union(self):
        first = local_db.amz_get_group()
        second = local_db.amz_create_group("第二组")
        local_db.amz_save_watchlist([ASIN, OTHER], first["id"])
        local_db.amz_save_watchlist([OTHER, THIRD, OTHER], second["id"])
        local_db.amz_save_watchlist([ASIN], first["id"])
        self.assertEqual(self.asins(first["id"]), [ASIN])
        self.assertEqual(self.asins(second["id"]), [OTHER, THIRD])
        self.assertEqual(local_db.amz_get_group(second["id"])["count"], 2)
        self.assertEqual(self.enabled(), {ASIN: 1, OTHER: 1, THIRD: 1})
        local_db.amz_save_watchlist([], second["id"])
        self.assertEqual(self.enabled(), {ASIN: 1, OTHER: 0, THIRD: 0})
        self.assertEqual(self.asins(first["id"]), [ASIN])
        local_db.amz_save_watchlist(iter([THIRD, THIRD]), second["id"])
        self.assertEqual(self.asins(second["id"]), [THIRD])
        self.assertEqual(self.enabled(), {ASIN: 1, OTHER: 0, THIRD: 1})

    def test_group_history_filters_current_members_and_keeps_old_positional_arguments(self):
        first = local_db.amz_get_group()
        second = local_db.amz_create_group("第二组")
        local_db.amz_save_watchlist([ASIN, THIRD], first["id"])
        local_db.amz_save_watchlist([OTHER], second["id"])
        for asin in (ASIN, OTHER, THIRD):
            self.change_price(asin)
        local_db.amz_record(ASIN, {"price": "259.00"})
        history = local_db.amz_changes()
        self.assertEqual([row["asin"] for row in history], [ASIN, THIRD, OTHER, ASIN])
        self.assertEqual(local_db.amz_changes(ASIN, 1), history[:1])
        self.assertEqual(local_db.amz_changes(None, 2, first["id"]), history[:2])
        self.assertEqual(local_db.amz_changes(OTHER, 100, first["id"]), [])
        self.assertEqual([row["asin"] for row in local_db.amz_changes(group_id=second["id"])], [OTHER])
        local_db.amz_save_watchlist([ASIN], first["id"])
        self.assertEqual([row["asin"] for row in local_db.amz_changes(group_id=first["id"])], [ASIN, ASIN])
        local_db.amz_save_watchlist([OTHER, THIRD], second["id"])
        self.assertEqual([row["asin"] for row in local_db.amz_changes(group_id=second["id"])], [THIRD, OTHER])
        self.assertEqual(local_db.amz_changes(None, 100, None), history)
        self.assertEqual(local_db.amz_changes(limit=0), history[:1])

    def test_empty_group_history_is_empty_but_global_removed_history_remains(self):
        first = local_db.amz_get_group()
        second = local_db.amz_create_group("空组")
        local_db.amz_save_watchlist([ASIN])
        self.change_price(ASIN)
        self.assertEqual(local_db.amz_changes(group_id=second["id"]), [])
        local_db.amz_save_watchlist([])
        self.assertEqual(local_db.amz_changes(group_id=first["id"]), [])
        self.assertEqual(len(local_db.amz_changes()), 1)

    def test_none_selects_first_surviving_group_after_original_first_is_deleted(self):
        first = local_db.amz_get_group()
        local_db.amz_save_watchlist([ASIN])
        self.change_price(ASIN)
        second = local_db.amz_create_group("留下的组")
        local_db.amz_save_watchlist([OTHER], second["id"])
        third = local_db.amz_create_group("不应修改")
        local_db.amz_save_watchlist([ASIN], third["id"])
        local_db.amz_delete_group(first["id"])
        self.assertEqual(local_db.amz_get_group(), {**second, "count": 1})
        self.assertEqual(self.asins(), [OTHER])
        self.assertEqual(local_db.amz_save_watchlist([THIRD])[0]["asin"], THIRD)
        self.assertEqual(self.asins(second["id"]), [THIRD])
        self.assertEqual(self.asins(third["id"]), [ASIN])
        self.assertEqual([g["id"] for g in local_db.amz_groups()], [second["id"], third["id"]])
        self.assertEqual(len(local_db.amz_changes()), 1)

    def test_failed_save_rolls_back_members_new_watch_rows_and_compatibility_flags(self):
        first = local_db.amz_get_group()
        second = local_db.amz_create_group("第二组")
        local_db.amz_save_watchlist([ASIN], first["id"])
        local_db.amz_save_watchlist([OTHER], second["id"])
        self.change_price(ASIN)
        before = self.keeper.execute("SELECT * FROM amz_watch ORDER BY rowid").fetchall()
        history = local_db.amz_changes()
        self.keeper.executescript(
            "CREATE TRIGGER fail_group_insert BEFORE INSERT ON amz_group_asins "
            f"WHEN NEW.asin='{THIRD}' BEGIN SELECT RAISE(ABORT, 'test insert failure'); END;")
        with self.assertRaises(sqlite3.IntegrityError):
            local_db.amz_save_watchlist([THIRD], first["id"])
        self.assertEqual(self.asins(first["id"]), [ASIN])
        self.assertEqual(self.asins(second["id"]), [OTHER])
        self.assertEqual(self.keeper.execute("SELECT * FROM amz_watch ORDER BY rowid").fetchall(), before)
        self.assertEqual(local_db.amz_changes(), history)

    def test_failed_delete_rolls_back_group_members_and_watch_flags(self):
        first = local_db.amz_get_group()
        second = local_db.amz_create_group("第二组")
        local_db.amz_save_watchlist([ASIN], first["id"])
        local_db.amz_save_watchlist([OTHER], second["id"])
        self.change_price(OTHER)
        history = local_db.amz_changes()
        self.keeper.executescript(
            "CREATE TRIGGER fail_enabled_update BEFORE UPDATE OF enabled ON amz_watch "
            "BEGIN SELECT RAISE(ABORT, 'test enabled failure'); END;")
        with self.assertRaises(sqlite3.IntegrityError):
            local_db.amz_delete_group(second["id"])
        self.assertEqual(local_db.amz_get_group(second["id"])["count"], 1)
        self.assertEqual(self.asins(first["id"]), [ASIN])
        self.assertEqual(self.asins(second["id"]), [OTHER])
        self.assertEqual(self.enabled(), {ASIN: 1, OTHER: 1})
        self.assertEqual(local_db.amz_changes(), history)


if __name__ == "__main__":
    unittest.main()
