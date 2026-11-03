"""学习会话：控制每日"新词 → 复习"的完整流程、错词循环与断点。

阶段流转：
    new_zh2en → new_en2zh → new_relearn
    → review_zh2en → review_en2zh → review_relearn → done
"""

import json
from datetime import date, datetime, timedelta

from . import config, models, scheduler
from .db import get_conn

STAGE_NEW_ZH2EN = "new_zh2en"
STAGE_NEW_EN2ZH = "new_en2zh"
STAGE_NEW_RELEARN = "new_relearn"
STAGE_REVIEW_ZH2EN = "review_zh2en"
STAGE_REVIEW_EN2ZH = "review_en2zh"
STAGE_REVIEW_RELEARN = "review_relearn"
STAGE_DONE = "done"

STAGE_LABELS = {
    STAGE_NEW_ZH2EN: "新词 · 中译英",
    STAGE_NEW_EN2ZH: "新词 · 英译中",
    STAGE_NEW_RELEARN: "新词 · 错词巩固",
    STAGE_REVIEW_ZH2EN: "复习 · 中译英",
    STAGE_REVIEW_EN2ZH: "复习 · 英译中",
    STAGE_REVIEW_RELEARN: "复习 · 错词重做",
    STAGE_DONE: "全部完成",
}


class StudySession:
    def __init__(self, row):
        self.session_date = row["session_date"]
        self.session_type = row["session_type"] if "session_type" in row.keys() else "normal"
        self.batch_id = row["batch_id"] if "batch_id" in row.keys() else None
        self.stage = row["stage"]
        self.queue = json.loads(row["queue"])
        self.pending = json.loads(row["pending"])
        self.new_cards = json.loads(row["new_cards"])
        self.review_cards = json.loads(row["review_cards"])
        self.new_count = row["new_count"]
        self.review_count = row["review_count"]
        self.lapse_count = row["lapse_count"]
        self.started_at = row["started_at"]

    # ---------- 生命周期 ----------

    @classmethod
    def load(cls):
        with get_conn() as conn:
            row = conn.execute(
                "SELECT * FROM session_state WHERE id = 1"
            ).fetchone()
        return cls(row) if row else None

    @classmethod
    def get_or_create(cls, today: date, batch_id: int | None = None) -> "StudySession":
        """显式选择目标（点具体批次，或全量）时，按该目标（重新）创建会话。

        卡片评分已实时落库，重建不会丢已学：_create 只取 state='new' 的卡，
        已学/在学的卡不会重新出现；shuffle 模式下每次重建都重新随机，
        满足“每次进入都重新随机”。
        主按钮“继续学习”走 load() 直接返回现有会话，不经过这里，断点不被打乱。
        """
        return cls._create(today, batch_id)

    @classmethod
    def _create(cls, today: date, batch_id: int | None = None) -> "StudySession":
        iso = today.isoformat()

        # 新词快照：会话创建时两个方向都还是 new。
        # 指定批次时只取该批次，否则取全部批次。
        new_zh2en = models.get_new_card_ids(config.DIR_ZH2EN, batch_id)
        new_en2zh = models.get_new_card_ids(config.DIR_EN2ZH, batch_id)
        new_cards = {
            config.DIR_ZH2EN: new_zh2en,
            config.DIR_EN2ZH: new_en2zh,
        }

        # 到期复习词组快照：任一方向到期，词组双向都测
        review_cards = {config.DIR_ZH2EN: [], config.DIR_EN2ZH: []}
        if batch_id is None:
            # 全量学习：新词之后接到期复习
            review_phrase_ids = models.get_due_review_phrase_ids(iso)
            for pid in review_phrase_ids:
                ids = models.get_phrase_card_ids(pid)
                review_cards[config.DIR_ZH2EN].append(ids[config.DIR_ZH2EN])
                review_cards[config.DIR_EN2ZH].append(ids[config.DIR_EN2ZH])
        # 指定批次：只学该批次新词，不混入到期复习
        session_type = "batch" if batch_id is not None else "normal"

        if new_zh2en:
            stage, queue = STAGE_NEW_ZH2EN, list(new_zh2en)
        elif review_cards[config.DIR_ZH2EN]:
            stage, queue = STAGE_REVIEW_ZH2EN, list(review_cards[config.DIR_ZH2EN])
        else:
            stage, queue = STAGE_DONE, []

        with get_conn() as conn:
            conn.execute("DELETE FROM session_state WHERE id = 1")
            conn.execute(
                """INSERT INTO session_state
                   (id, session_date, session_type, batch_id, stage, queue,
                    pending, new_cards, review_cards, new_count, review_count,
                    lapse_count)
                   VALUES (1, ?, ?, ?, ?, ?, '[]', ?, ?, ?, ?, 0)""",
                (
                    iso, session_type, batch_id, stage, json.dumps(queue),
                    json.dumps(new_cards), json.dumps(review_cards),
                    len(new_zh2en), len(review_cards[config.DIR_ZH2EN]),
                ),
            )
        return cls.load()

    @classmethod
    def create_cram(cls, limit: int, today: date) -> "StudySession":
        """创建突击会话：按易忘程度选词，无新词，直接进入复习式双向测试。"""
        iso = today.isoformat()
        phrase_ids = models.get_cram_phrase_ids(limit)
        review_cards = {config.DIR_ZH2EN: [], config.DIR_EN2ZH: []}
        for pid in phrase_ids:
            ids = models.get_phrase_card_ids(pid)
            review_cards[config.DIR_ZH2EN].append(ids[config.DIR_ZH2EN])
            review_cards[config.DIR_EN2ZH].append(ids[config.DIR_EN2ZH])

        stage = STAGE_REVIEW_ZH2EN if review_cards[config.DIR_ZH2EN] else STAGE_DONE
        queue = list(review_cards[config.DIR_ZH2EN])

        with get_conn() as conn:
            conn.execute("DELETE FROM session_state WHERE id = 1")
            conn.execute(
                """INSERT INTO session_state
                   (id, session_date, session_type, stage, queue, pending,
                    new_cards, review_cards, new_count, review_count,
                    lapse_count)
                   VALUES (1, ?, 'cram', ?, ?, '[]', '{}', ?, 0, ?, 0)""",
                (iso, stage, json.dumps(queue),
                 json.dumps(review_cards),
                 len(review_cards[config.DIR_ZH2EN])),
            )
        return cls.load()

    def save(self):
        with get_conn() as conn:
            conn.execute(
                """UPDATE session_state SET session_type = ?, stage = ?,
                       queue = ?, pending = ?, new_cards = ?,
                       review_cards = ?, new_count = ?, review_count = ?,
                       lapse_count = ? WHERE id = 1""",
                (
                    self.session_type, self.stage,
                    json.dumps(self.queue), json.dumps(self.pending),
                    json.dumps(self.new_cards), json.dumps(self.review_cards),
                    self.new_count, self.review_count, self.lapse_count,
                ),
            )

    # ---------- 阶段流转 ----------

    def _drain_pending(self) -> list:
        items = []
        for cid in self.pending:
            if cid not in items:
                items.append(cid)
        self.pending = []
        return items

    def _advance(self):
        """queue 为空时不断推进阶段，直到进入有卡的阶段或 done。"""
        while not self.queue and self.stage != STAGE_DONE:
            if self.stage == STAGE_NEW_ZH2EN:
                self.stage = STAGE_NEW_EN2ZH
                self.queue = list(self.new_cards.get(config.DIR_EN2ZH, []))
            elif self.stage == STAGE_NEW_EN2ZH:
                self.stage = STAGE_NEW_RELEARN
                self.queue = self._drain_pending()
            elif self.stage == STAGE_NEW_RELEARN:
                self.stage = STAGE_REVIEW_ZH2EN
                self.queue = list(self.review_cards.get(config.DIR_ZH2EN, []))
            elif self.stage == STAGE_REVIEW_ZH2EN:
                self.stage = STAGE_REVIEW_EN2ZH
                self.queue = list(self.review_cards.get(config.DIR_EN2ZH, []))
            elif self.stage == STAGE_REVIEW_EN2ZH:
                self.stage = STAGE_REVIEW_RELEARN
                self.queue = self._drain_pending()
            elif self.stage == STAGE_REVIEW_RELEARN:
                self.stage = STAGE_DONE
                self.queue = []

    # ---------- 对外接口 ----------

    def current(self) -> dict:
        self._advance()
        self.save()  # 阶段推进后立即落库，保证下一次请求读到正确队列
        if self.stage == STAGE_DONE:
            return {"done": True, "summary": self._summary()}
        card = models.get_card(self.queue[0])
        base_total = (self.new_count + self.review_count) * 2
        rated_count = models.get_today_log_count(self.session_date)
        return {
            "done": False,
            "session_type": self.session_type,
            "batch_id": self.batch_id,
            "stage": self.stage,
            "stage_label": STAGE_LABELS[self.stage],
            "card": {
                "card_id": card["card_id"],
                "direction": card["direction"],
                "item_type": card["item_type"],
                "tag": card["tag"],
                "en": card["en"],
                "zh": card["zh"],
                "note": card["note"],
                "phonetic": card["phonetic"],
                "pos": card["pos"],
            },
            "queue_left": len(self.queue),
            "base_total": base_total,
            "rated_count": rated_count,
            "new_count": self.new_count,
            "review_count": self.review_count,
        }

    def rate(self, rating: int, elapsed_ms: int, today: date) -> dict:
        if self.stage == STAGE_DONE:
            return {"done": True, "summary": self._summary()}

        card_id = self.queue[0]
        card = models.get_card(card_id)

        # 复习/已掌握的卡被评 Again，记一次遗忘
        if (rating == config.RATING_AGAIN and
                card["state"] in (config.STATE_REVIEW, config.STATE_MASTERED)):
            self.lapse_count += 1

        # 调度基准锁定为会话开始日期：即使背到凌晨，同一批词也同一天到期
        base_day = date.fromisoformat(self.session_date)
        fields = scheduler.schedule(card, rating, base_day)
        models.update_card(card_id, fields, rating)
        models.add_log(card_id, rating, elapsed_ms)

        self.queue.pop(0)
        if self.stage in (STAGE_NEW_ZH2EN, STAGE_NEW_EN2ZH):
            # 新词：Again/Hard 都进错词篮
            if rating in (config.RATING_AGAIN, config.RATING_HARD):
                self.pending.append(card_id)
        elif self.stage in (STAGE_REVIEW_ZH2EN, STAGE_REVIEW_EN2ZH):
            # 复习：只有 Again 进重做篮
            if rating == config.RATING_AGAIN:
                self.pending.append(card_id)
        else:
            # 错词循环：Again/Hard 重新排到队尾
            if rating in (config.RATING_AGAIN, config.RATING_HARD):
                self.queue.append(card_id)

        self.save()
        return self.current()

    def _summary(self) -> dict:
        try:
            start = datetime.strptime(self.started_at, "%Y-%m-%d %H:%M:%S")
            elapsed = max(0, int((datetime.now() - start).total_seconds()))
        except (TypeError, ValueError):
            elapsed = 0
        today = date.today()
        return {
            "done": True,
            "session_type": self.session_type,
            "new_learned": self.new_count,
            "review_done": self.review_count,
            "lapses": self.lapse_count,
            "elapsed_sec": elapsed,
            "streak": models.get_streak(today),
            "tomorrow_due": models.get_due_count(today + timedelta(days=1)),
        }
