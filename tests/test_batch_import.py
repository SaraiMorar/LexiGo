import tempfile
import unittest
from pathlib import Path

from lexigo import config, models
from lexigo.batch_import import parse_pasted_table
from lexigo.db import get_conn, init_db


class TableParserTests(unittest.TestCase):
    def test_common_table_formats(self):
        cases = [
            "序号\t英文\t中文\n1\tlook after\t照顾\n2\tgive up\t放弃",
            "1. look after 照顾\n2、give up 放弃",
            "| 英文 | 中文 |\n| --- | --- |\n| look after | 照顾 |\n| give up | 放弃 |",
            '序号,英文,中文\n1,"look after","照顾"\n2,"give up","放弃"',
            "中文\t英文\n照顾\tlook after\n放弃\tgive up",
            "英文\t中文\t英文\t中文\nlook after\t照顾\tgive up\t放弃",
            "look after\t照顾\tgive up\t放弃",
            "1\tlook after\t照顾\t2\tgive up\t放弃",
            "英文 中文\nlook after 照顾\ngive up 放弃",
        ]
        for text in cases:
            with self.subTest(text=text):
                rows, _ = parse_pasted_table(text)
                self.assertEqual([(row["en"], row["zh"]) for row in rows],
                                 [("look after", "照顾"), ("give up", "放弃")])

    def test_preserves_notes_quoted_commas_and_multiline_cells(self):
        rows, _ = parse_pasted_table('English,Chinese,Note\n"take care of","照顾，照料","Take care of yourself.\n保重。"')
        self.assertEqual(rows[0]["zh"], "照顾，照料")
        self.assertEqual(rows[0]["note"], "Take care of yourself.\n保重。")
        rows, _ = parse_pasted_table("take off 起飞，脱下\nset off 出发;使爆炸")
        self.assertEqual([r["en"] for r in rows], ["take off", "set off"])
        self.assertEqual([r["zh"] for r in rows], ["起飞，脱下", "出发;使爆炸"])

    def test_unrecognized_lines_stay_visible(self):
        rows, _ = parse_pasted_table("look after 照顾\nunknown phrase\n课堂词组")
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[1]["en"], "unknown phrase")
        self.assertEqual(rows[1]["zh"], "")


class BatchImportTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        original = config.DATA_DIR, config.DB_PATH, config.WORDS_CSV_PATH
        def restore():
            config.DATA_DIR, config.DB_PATH, config.WORDS_CSV_PATH = original
        self.addCleanup(restore)
        config.DATA_DIR = Path(temp.name)
        config.DB_PATH = config.DATA_DIR / "test.db"
        config.WORDS_CSV_PATH = config.DATA_DIR / "words.csv"
        config.WORDS_CSV_PATH.write_text("en,zh\n", encoding="utf-8")
        init_db()
        import app
        app.app.testing = True
        self.client = app.app.test_client()

    def preview(self, text):
        return self.client.post("/api/batches/import/preview", json={"text": text})

    def submit(self, **values):
        return self.client.post("/api/batches/import", json={"name": "今日课堂词组", "rows": [
            {"en": "look after", "zh": "照顾"}, {"en": "give up", "zh": "放弃"},
        ], **values})

    def snapshot(self):
        with get_conn() as conn:
            return {table: [tuple(r) for r in conn.execute(f"SELECT * FROM {table}")]
                    for table in ("batches", "phrases", "cards", "review_logs", "session_state")}

    def test_preview_is_read_only_and_marks_duplicates_and_invalid_rows(self):
        models.add_phrase_full("Look After", "旧释义")
        before = self.snapshot()
        data = self.preview("英文\t中文\nlook after\t照顾\ngive up\t放弃\nGIVE   UP\t放弃\nunknown\t").get_json()
        self.assertEqual([row["status"] for row in data["rows"]], ["existing", "new", "duplicate", "invalid"])
        self.assertEqual(data["headers_skipped"], 1)
        self.assertEqual(before, self.snapshot())

    def test_import_creates_one_batch_and_two_cards_per_word(self):
        response = self.submit()
        self.assertEqual(response.status_code, 200, response.data)
        result = response.get_json()
        self.assertEqual(result["added"], 2)
        self.assertEqual(len(models.list_batches()), 1)
        items = models.list_phrases(batch_id=result["batch_id"])["items"]
        self.assertEqual([row["en"] for row in items], ["look after", "give up"])
        self.assertTrue(all(row["item_type"] == "phrase" for row in items))
        with get_conn() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM cards WHERE state='new'").fetchone()[0], 4)
        state = self.client.post("/api/session/start", json={"batch_id": result["batch_id"]}).get_json()
        for _ in range(4):
            state = self.client.post("/api/session/rate", json={"rating": 3, "version": state["version"],
                                      "card_id": state["card"]["card_id"]}).get_json()
        self.assertTrue(state["done"])

    def test_existing_progress_and_active_session_are_unchanged(self):
        pid, _ = models.add_phrase_full("look after", "旧释义", batch_name="已有批次")
        self.client.post("/api/session/start")
        self.client.post("/api/session/rate", json={"rating": 3})
        before = self.snapshot()
        result = self.submit(rows=[{"en": "LOOK  AFTER", "zh": "新释义"},
                                   {"en": "give up", "zh": "放弃"}, {"en": "Give Up", "zh": "重复"}]).get_json()
        self.assertEqual(result["added"], 1)
        self.assertEqual(result["skipped_existing"], 1)
        self.assertEqual(result["skipped_duplicate"], 1)
        after = self.snapshot()
        for table in ("review_logs", "session_state"):
            self.assertEqual(before[table], after[table])
        self.assertEqual(before["phrases"][0], after["phrases"][0])
        self.assertEqual(before["cards"], after["cards"][:2])
        self.assertEqual(models.get_phrase_card_ids(pid).keys(), {"zh2en", "en2zh"})

    def test_all_existing_and_repeated_request_create_no_empty_batch(self):
        self.assertEqual(self.submit().status_code, 200)
        before = self.snapshot()
        self.assertEqual(self.submit().status_code, 400)
        self.assertEqual(self.submit(name="另一个批次").status_code, 400)
        self.assertEqual(before, self.snapshot())

    def test_invalid_import_rolls_back_without_partial_rows(self):
        for payload in [{"name": " "}, {"rows": []}, {"item_type": "invalid"},
                        {"rows": [{"en": "valid", "zh": "有效"}, {"en": "missing", "zh": ""}]},
                        {"rows": [{"en": 123, "zh": "错误"}]}]:
            with self.subTest(payload=payload):
                self.assertEqual(self.submit(**payload).status_code, 400)
                self.assertEqual(self.snapshot()["batches"], [])
                self.assertEqual(models.list_phrases()["total"], 0)

    def test_database_write_failure_rolls_back_entire_batch(self):
        with get_conn() as conn:
            conn.execute("CREATE TRIGGER fail_second BEFORE INSERT ON phrases WHEN NEW.en = 'give up' "
                         "BEGIN SELECT RAISE(ABORT, 'simulated write failure'); END")
        before = self.snapshot()
        with self.assertRaisesRegex(Exception, "simulated write failure"):
            self.submit()
        self.assertEqual(before, self.snapshot())

    def test_user_can_explicitly_reimport_deleted_word(self):
        pid, _ = models.add_phrase_full("look after", "照顾")
        models.delete_phrase(pid)
        self.assertEqual(self.submit().status_code, 200)
        with get_conn() as conn:
            self.assertIsNone(conn.execute("SELECT 1 FROM deleted_phrases WHERE en='look after'").fetchone())

    def test_type_and_edited_preview_fields_are_saved(self):
        response = self.submit(item_type="word", rows=[{"en": "effort", "zh": "努力", "note": "make an effort"}])
        self.assertEqual(response.status_code, 200)
        row = models.list_phrases()["items"][0]
        self.assertEqual((row["item_type"], row["note"]), ("word", "make an effort"))

    def test_limits_and_bad_requests_are_rejected(self):
        for text in ["", None, [], "x" * 100001, "look after\t照顾\n" * 1001]:
            self.assertEqual(self.preview(text).status_code, 400)
        for path in ("/api/batches/import", "/api/batches/import/preview"):
            self.assertEqual(self.client.post(path, json=[]).status_code, 400)
