import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path
from threading import Barrier
from unittest.mock import patch

from lexigo import config, models
from lexigo.db import get_conn, init_db


class StudyTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        original = config.DATA_DIR, config.DB_PATH, config.WORDS_CSV_PATH
        def restore():
            config.DATA_DIR, config.DB_PATH, config.WORDS_CSV_PATH = original
        self.addCleanup(restore)
        config.DATA_DIR = Path(tmp.name)
        config.DB_PATH = config.DATA_DIR / "test.db"
        config.WORDS_CSV_PATH = config.DATA_DIR / "words.csv"
        config.WORDS_CSV_PATH.write_text("en,zh\n", encoding="utf-8")
        init_db()
        import app
        app.app.testing = True
        self.app = app.app
        self.client = app.app.test_client()
        self.pid, _ = models.add_phrase_full("look after", "照顾")
        self.batch = models.list_batches()[0]["id"]

    def start(self, batch_id=None):
        response = self.client.post("/api/session/start", json={"batch_id": batch_id})
        self.assertEqual(response.status_code, 200, response.data)
        return response.get_json()

    def payload(self, state, rating=3):
        return {"rating": rating, "elapsed_ms": 123, "version": state["version"],
                "card_id": state["card"]["card_id"]}

    def rate(self, state, rating=3):
        response = self.client.post("/api/session/rate", json=self.payload(state, rating))
        self.assertEqual(response.status_code, 200, response.data)
        return response.get_json()

    def snapshot(self):
        with get_conn() as conn:
            return {table: [tuple(r) for r in conn.execute(f"SELECT * FROM {table}")]
                    for table in ("cards", "review_logs", "session_state")}

    def test_switching_batch_keeps_remaining_direction(self):
        state = self.rate(self.start(self.batch))
        self.assertEqual(state["card"]["direction"], "en2zh")
        other = models.create_batch("其他批次")
        models.add_phrase_full("give up", "放弃", batch_id=other)
        self.start(other)
        self.assertEqual(self.client.get("/api/status").get_json()["new_waiting"], 2)
        state = self.start(self.batch)
        self.assertFalse(state["done"])
        self.assertEqual(state["card"]["direction"], "en2zh")
        self.assertEqual(state["base_total"], 1)
        self.assertEqual(state["rated_count"], 0)
        self.assertTrue(self.rate(state)["done"])

    def test_both_failed_directions_survive_restarting_batch(self):
        state = self.rate(self.start(self.batch), 1)
        self.rate(state, 2)
        batch = models.list_batches()[0]
        self.assertEqual(batch["done"], 0)
        self.assertEqual(batch["new_waiting"], 1)
        state = self.start(self.batch)
        self.assertFalse(state["done"])
        state = self.rate(state)
        self.assertTrue(self.rate(state)["done"])
        self.assertEqual(models.list_batches()[0]["done"], 1)

    def test_unfinished_new_not_in_review_queue_twice(self):
        self.rate(self.start(), 1)
        state = self.start()
        self.assertEqual(state["review_count"], 0)
        state = self.rate(state)
        self.assertTrue(self.rate(state)["done"])
        with get_conn() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM review_logs").fetchone()[0], 3)

    def test_suspended_phrase_removed_from_all_session_stages(self):
        state = self.rate(self.start(), 1)
        response = self.client.post(f"/api/phrases/{self.pid}/suspend")
        self.assertEqual(response.status_code, 200)
        conflict = self.client.post("/api/session/rate", json=self.payload(state))
        self.assertEqual(conflict.status_code, 409)
        self.assertTrue(conflict.get_json()["state"]["done"])
        with get_conn() as conn:
            self.assertEqual([r[0] for r in conn.execute("SELECT state FROM cards")], ["suspended"] * 2)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM review_logs").fetchone()[0], 1)
        self.client.post(f"/api/phrases/{self.pid}/resume")
        self.assertFalse(self.start(self.batch)["done"])

    def test_resume_is_noop_for_active_card(self):
        self.rate(self.start())
        before = self.snapshot()["cards"]
        self.client.post(f"/api/phrases/{self.pid}/resume")
        self.assertEqual(before, self.snapshot()["cards"])

    def test_invalid_ratings_never_change_progress(self):
        self.start()
        before = self.snapshot()
        invalid = [None, [], {}, {"rating": 99}, {"rating": 0}, {"rating": True},
                   {"rating": 3.5}, {"rating": 3, "elapsed_ms": -1},
                   {"rating": 3, "elapsed_ms": 2**63}, {"rating": 3, "version": ""}]
        for payload in invalid:
            with self.subTest(payload=payload):
                response = self.client.post("/api/session/rate", json=payload,
                                            content_type="application/json")
                self.assertEqual(response.status_code, 400)
                self.assertEqual(before, self.snapshot())

    def test_stale_rating_cannot_advance_next_card(self):
        state = self.start()
        self.rate(state)
        before = self.snapshot()
        response = self.client.post("/api/session/rate", json=self.payload(state))
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.get_json()["state"]["card"]["direction"], "en2zh")
        self.assertEqual(before, self.snapshot())

    def test_stale_rating_rejected_even_when_same_card_reappears(self):
        first = self.start()
        second = self.rate(first, 1)
        repeated = self.rate(second, 1)
        self.assertEqual(repeated["card"]["card_id"], first["card"]["card_id"])
        self.assertEqual(self.client.post("/api/session/rate", json=self.payload(first)).status_code, 409)

    def test_switching_session_invalidates_old_rating(self):
        state = self.start()
        restarted = self.start()
        self.assertEqual(state["card"]["card_id"], restarted["card"]["card_id"])
        self.assertNotEqual(state["version"], restarted["version"])
        self.assertEqual(self.client.post("/api/session/rate", json=self.payload(state)).status_code, 409)

    def test_concurrent_duplicate_rating_only_commits_once(self):
        payload = self.payload(self.start())
        barrier = Barrier(2)
        def submit():
            with self.app.test_client() as client:
                barrier.wait(timeout=5)
                return client.post("/api/session/rate", json=payload).status_code
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: submit(), range(2)))
        self.assertEqual(sorted(results), [200, 409])
        self.assertEqual(len(self.snapshot()["review_logs"]), 1)

    def test_failure_rolls_back_card_log_and_queue_together(self):
        state = self.start()
        before = self.snapshot()
        with patch("lexigo.models.add_log", side_effect=RuntimeError("simulated write failure")):
            with self.assertRaisesRegex(RuntimeError, "simulated write failure"):
                self.client.post("/api/session/rate", json=self.payload(state))
        self.assertEqual(before, self.snapshot())
        self.assertFalse(self.rate(state)["done"])

    def test_progress_is_for_this_session_and_waits_for_relearning(self):
        state = self.rate(self.start(), 1)
        self.assertEqual(state["rated_count"], 0)
        state = self.rate(state)
        self.assertEqual(state["rated_count"], 1)
        self.assertTrue(self.rate(state)["done"])
        models.add_phrase_full("give up", "放弃", batch_name="第二批")
        state = self.start(models.list_batches()[-1]["id"])
        self.assertEqual(state["rated_count"], 0)
        self.assertEqual(state["base_total"], 2)

    def test_nonexistent_batch_does_not_replace_session(self):
        self.start()
        before = self.snapshot()
        for value in [99999, True, 1.5, "invalid"]:
            response = self.client.post("/api/session/start", json={"batch_id": value})
            self.assertIn(response.status_code, [400, 404])
            self.assertEqual(before, self.snapshot())

    def test_review_hard_on_lapsed_card_requires_relearning(self):
        self.rate(self.rate(self.start()))
        with get_conn() as conn:
            conn.execute("UPDATE cards SET state='review', reps=2, interval_days=3, due=?", (date.today().isoformat(),))
        state = self.rate(self.start(), 1)
        self.assertEqual(state["stage"], "review_en2zh")
        self.start()  # 丢弃旧会话，模拟忘词后切换任务。
        state = self.rate(self.start(), 2)
        state = self.rate(state)
        self.assertFalse(state["done"])
        self.assertEqual(state["stage"], "review_relearn")
        self.assertTrue(self.rate(state)["done"])
