import csv
import sqlite3
import tempfile
import unittest
from pathlib import Path

from lexigo import config, models
from lexigo.db import get_conn, init_db
from lexigo.importer import sync_from_csv


class LibraryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.original = config.DATA_DIR, config.DB_PATH, config.WORDS_CSV_PATH
        self.addCleanup(self.restore_config)
        config.DATA_DIR = Path(self.temp.name)
        config.DB_PATH = config.DATA_DIR / "test.db"
        config.WORDS_CSV_PATH = config.DATA_DIR / "words.csv"
        self.write_csv()
        init_db()
        import app
        app.app.testing = True
        self.client = app.app.test_client()

    def restore_config(self):
        config.DATA_DIR, config.DB_PATH, config.WORDS_CSV_PATH = self.original

    def write_csv(self, rows=()):
        with config.WORDS_CSV_PATH.open("w", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=["en", "zh", "tag", "note", "item_type"])
            writer.writeheader()
            writer.writerows(rows)

    def add(self, en="take care of", **extra):
        response = self.client.post("/api/phrases", json={"en": en, "zh": "照顾", **extra})
        self.assertEqual(response.status_code, 200, response.data)
        return response.get_json()["id"]

    def phrase(self, pid):
        with get_conn() as conn:
            return dict(conn.execute("SELECT * FROM phrases WHERE id=?", (pid,)).fetchone())

    def test_move_and_filter_preserve_cards_and_reviews(self):
        pid = self.add()
        self.client.post("/api/session/start")
        self.client.post("/api/session/rate", json={"rating": 3})
        with get_conn() as conn:
            before = [tuple(r) for r in conn.execute("SELECT * FROM cards")]
            logs = [tuple(r) for r in conn.execute("SELECT * FROM review_logs")]
        target = models.create_batch("动词搭配")
        self.assertEqual(self.client.put(f"/api/phrases/{pid}", json={
            "en": "take care of", "zh": "照顾", "batch_id": target,
        }).status_code, 200)
        result = self.client.get(f"/api/phrases?batch_id={target}&state=new&q=care").get_json()
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["items"][0]["batch_id"], target)
        self.assertEqual(result["items"][0]["batch_name"], "动词搭配")
        with get_conn() as conn:
            self.assertEqual(before, [tuple(r) for r in conn.execute("SELECT * FROM cards")])
            self.assertEqual(logs, [tuple(r) for r in conn.execute("SELECT * FROM review_logs")])

    def test_duplicate_does_not_create_batch_even_with_new_name(self):
        pid = self.add()
        count = len(models.list_batches())
        result = self.client.post("/api/phrases", json={
            "en": "take care of", "zh": "照顾", "batch_name": "不应创建",
        }).get_json()
        self.assertEqual(result["id"], pid)
        self.assertTrue(result["existed"])
        self.assertEqual(len(models.list_batches()), count)

    def test_default_batch_reused_and_explicit_batch_created(self):
        first = self.add()
        second = self.add("look after")
        self.assertEqual(self.phrase(first)["batch_id"], self.phrase(second)["batch_id"])
        third = self.add("give up", batch_name="新词组")
        self.assertNotEqual(self.phrase(first)["batch_id"], self.phrase(third)["batch_id"])
        self.assertEqual(len(models.list_batches()), 2)

    def test_invalid_batch_changes_are_rejected(self):
        pid = self.add()
        original = self.phrase(pid)
        for invalid in [99999, "invalid"]:
            payload = {"en": "changed", "zh": "改动", "batch_id": invalid}
            self.assertEqual(self.client.put(f"/api/phrases/{pid}", json=payload).status_code, 400)
            self.assertEqual(self.client.post("/api/phrases", json=payload).status_code, 400)
        self.assertEqual(self.phrase(pid), original)
        self.assertEqual(self.client.get("/api/phrases?batch_id=invalid").status_code, 400)

    def test_rename_batch_does_not_change_members(self):
        pid = self.add()
        batch = self.phrase(pid)["batch_id"]
        self.assertEqual(self.client.put(f"/api/batches/{batch}", json={"name": "常用搭配"}).status_code, 200)
        self.assertEqual(self.phrase(pid)["batch_id"], batch)
        self.assertEqual(models.list_batches()[0]["name"], "常用搭配")
        self.assertEqual(self.client.put(f"/api/batches/{batch}", json={"name": "  "}).status_code, 400)

    def test_csv_deletion_survives_sync_and_can_be_explicitly_readded(self):
        self.write_csv([{"en": "take care of", "zh": "照顾"}])
        sync_from_csv()
        pid = models.list_phrases()["items"][0]["id"]
        self.client.delete(f"/api/phrases/{pid}")
        self.assertEqual(sync_from_csv()["added"], 0)
        self.assertEqual(models.list_phrases()["total"], 0)
        self.add()
        self.assertEqual(sync_from_csv()["added"], 0)
        self.assertEqual(models.list_phrases()["total"], 1)

    def test_same_name_batches_are_rejected_without_extra_rows(self):
        self.add(batch_name="常用搭配")
        self.assertEqual(self.client.post("/api/phrases", json={
            "en": "look after", "zh": "照顾", "batch_name": "常用搭配",
        }).status_code, 400)
        another = models.create_batch("其他")
        self.assertEqual(self.client.put(f"/api/batches/{another}", json={"name": "常用搭配"}).status_code, 400)
        self.assertEqual(models.list_phrases()["total"], 1)
        self.assertEqual(len(models.list_batches()), 2)

    def test_renamed_csv_word_does_not_reappear(self):
        self.write_csv([{"en": "old phrase", "zh": "词组"}])
        sync_from_csv()
        pid = models.list_phrases()["items"][0]["id"]
        self.client.put(f"/api/phrases/{pid}", json={"en": "correct phrase", "zh": "词组"})
        self.assertEqual(sync_from_csv()["added"], 0)
        self.assertEqual(models.list_phrases()["total"], 1)

    def test_csv_updates_preserve_web_edits_and_learning(self):
        self.write_csv([{"en": "care", "zh": "关心", "tag": "旧标签"}])
        sync_from_csv()
        pid = models.list_phrases()["items"][0]["id"]
        self.client.post("/api/session/start")
        self.client.post("/api/session/rate", json={"rating": 3})
        with get_conn() as conn:
            before = [tuple(r) for r in conn.execute("SELECT * FROM cards")]
        self.client.put(f"/api/phrases/{pid}", json={"en": "care", "zh": "网页释义", "tag": "旧标签"})
        self.write_csv([{"en": "care", "zh": "CSV释义", "tag": "新标签"}])
        self.assertEqual(sync_from_csv()["updated"], 1)
        self.assertEqual(self.phrase(pid)["zh"], "网页释义")
        self.assertEqual(self.phrase(pid)["tag"], "新标签")
        self.assertEqual(sync_from_csv()["updated"], 0)
        with get_conn() as conn:
            self.assertEqual(before, [tuple(r) for r in conn.execute("SELECT * FROM cards")])

    def test_delete_active_card_then_continue_and_rate(self):
        first = self.add()
        second = self.add("look after")
        self.client.post("/api/session/start")
        self.client.delete(f"/api/phrases/{first}")
        for _ in range(2):
            response = self.client.post("/api/session/rate", json={"rating": 3})
            self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["done"])
        self.assertEqual(response.get_json()["summary"]["new_learned"], 1)
        self.assertEqual(self.phrase(second)["en"], "look after")

    def test_csv_roundtrip_retains_groups_and_order(self):
        self.add("zebra", batch_name="单词")
        self.add("apple", batch_name="词组")
        batches = models.list_batches()
        models.set_batch_order_mode(batches[1]["id"], "shuffle")
        exported = self.client.get("/api/export/csv").data.decode("utf-8-sig")
        config.WORDS_CSV_PATH.write_text(exported, encoding="utf-8-sig")
        config.DB_PATH = config.DATA_DIR / "restored.db"
        self.assertEqual(sync_from_csv()["added"], 2)
        self.assertEqual([(b["name"], b["order_mode"], b["total"]) for b in models.list_batches()],
                         [("单词", "sequence", 1), ("词组", "shuffle", 1)])
        self.assertEqual(sync_from_csv()["added"], 0)
        self.assertEqual([p["en"] for p in models.list_phrases(sort="en")["items"]], ["apple", "zebra"])

    def test_migrate_original_database_without_changing_progress(self):
        original_db = Path(__file__).resolve().parents[1] / "data/lexigo.db"
        config.WORDS_CSV_PATH = original_db.with_name("words.csv")
        with sqlite3.connect(f"file:{original_db}?mode=ro", uri=True) as source:
            with sqlite3.connect(config.DB_PATH) as target:
                source.backup(target)
            before = {table: source.execute(f"SELECT * FROM {table}").fetchall()
                      for table in ("phrases", "cards", "review_logs", "session_state", "batches")}
        init_db()
        sync_from_csv()
        sync_from_csv()
        with sqlite3.connect(config.DB_PATH) as conn:
            for table, records in before.items():
                self.assertEqual(conn.execute(f"SELECT * FROM {table}").fetchall(), records, table)
            self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        self.assertEqual(models.list_phrases()["total"], 1526)
        self.assertEqual(self.client.get("/").status_code, 200)


if __name__ == "__main__":
    unittest.main()
