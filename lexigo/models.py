"""数据访问层：词组、卡片、复习记录的操作与统计。"""

import random
import json
from datetime import date, timedelta
from uuid import uuid4

from . import config
from .db import get_conn


# 任一方向仍未首次通过，且词条没有被搁置：算作未学完的新词。
_UNFINISHED_NEW = """
NOT EXISTS (SELECT 1 FROM cards blocked WHERE blocked.phrase_id = p.id AND blocked.state = 'suspended')
AND EXISTS (
    SELECT 1 FROM cards unfinished WHERE unfinished.phrase_id = p.id
    AND (unfinished.state = 'new' OR (
        unfinished.state = 'learning' AND NOT EXISTS (
            SELECT 1 FROM review_logs passed
            WHERE passed.card_id = unfinished.id AND passed.rating IN (3, 4)
        )
    ))
)
"""


# ---------- 批次 ----------

def create_batch(name: str) -> int:
    """创建一个批次，返回 id。"""
    with get_conn() as conn:
        cur = conn.execute("INSERT INTO batches (name) VALUES (?)", (name,))
        return cur.lastrowid


def ensure_first_batch(name: str = "第 1 批") -> int:
    """没有批次时创建首个批次，返回其 id。"""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id FROM batches ORDER BY id LIMIT 1"
        ).fetchone()
        if row:
            return row["id"]
        cur = conn.execute("INSERT INTO batches (name) VALUES (?)", (name,))
        return cur.lastrowid


def next_batch_name() -> str:
    """按现有批次数量生成下一个批次名称。"""
    with get_conn() as conn:
        n = conn.execute("SELECT COUNT(*) AS n FROM batches").fetchone()["n"]
    return f"第 {n + 1} 批"


def assign_batch(phrase_id: int, batch_id: int):
    """把单个词归到指定批次。"""
    with get_conn() as conn:
        conn.execute(
            "UPDATE phrases SET batch_id = ? WHERE id = ?",
            (batch_id, phrase_id),
        )


def assign_unbatched(batch_id: int) -> int:
    """把所有未归属批次的词归到指定批次，返回受影响行数。"""
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE phrases SET batch_id = ? WHERE batch_id IS NULL",
            (batch_id,),
        )
        return cur.rowcount


def ensure_history_batched():
    """把历史遗留（无批次）的词归到第 1 批；没有批次则创建。"""
    with get_conn() as conn:
        n_unbatched = conn.execute(
            "SELECT COUNT(*) AS n FROM phrases WHERE batch_id IS NULL"
        ).fetchone()["n"]
        if n_unbatched == 0:
            return
        first = conn.execute(
            "SELECT id FROM batches ORDER BY id LIMIT 1"
        ).fetchone()
        if first:
            bid = first["id"]
        else:
            cur = conn.execute(
                "INSERT INTO batches (name) VALUES ('第 1 批')"
            )
            bid = cur.lastrowid
        conn.execute(
            "UPDATE phrases SET batch_id = ? WHERE batch_id IS NULL", (bid,)
        )


def list_batches() -> list[dict]:
    """批次及进度：total 总词数，seen 至少学过一张，done 两卡都学完。"""
    with get_conn() as conn:
        rows = conn.execute(
            f"""
            SELECT b.id, b.name, b.created_at, b.order_mode,
                   COUNT(p.id) AS total,
                   SUM(CASE WHEN p.not_new >= 1 THEN 1 ELSE 0 END) AS seen,
                   SUM(CASE WHEN p.passed = 2 THEN 1 ELSE 0 END) AS done,
                   SUM(CASE WHEN p.unfinished THEN 1 ELSE 0 END) AS new_waiting
            FROM batches b
            LEFT JOIN (
                SELECT p.id, p.batch_id, ({_UNFINISHED_NEW}) AS unfinished,
                       SUM(CASE WHEN c.state != 'new' THEN 1 ELSE 0 END)
                         AS not_new,
                       SUM(CASE WHEN c.state IN ('review', 'mastered') OR EXISTS (
                           SELECT 1 FROM review_logs l WHERE l.card_id = c.id AND l.rating IN (3, 4)
                       ) THEN 1 ELSE 0 END) AS passed
                FROM phrases p JOIN cards c ON c.phrase_id = p.id
                GROUP BY p.id
            ) p ON p.batch_id = b.id
            GROUP BY b.id
            ORDER BY b.id
            """
        ).fetchall()
    return [dict(r) for r in rows]


def set_batch_order_mode(batch_id: int, order_mode: str) -> bool:
    """更新某批次的背诵顺序模式；成功返回 True，批次不存在返回 False。"""
    if order_mode not in config.ORDER_MODES:
        raise ValueError("未知顺序模式")
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE batches SET order_mode = ? WHERE id = ?",
            (order_mode, batch_id),
        )
        return cur.rowcount > 0


def rename_batch(batch_id: int, name: str) -> bool:
    with get_conn() as conn:
        if conn.execute("SELECT 1 FROM batches WHERE name = ? AND id != ?", (name, batch_id)).fetchone():
            raise ValueError("已存在同名批次，请使用不同名称")
        return conn.execute(
            "UPDATE batches SET name = ? WHERE id = ?", (name, batch_id)
        ).rowcount > 0


# ---------- 词组 ----------

def insert_phrase(en: str, zh: str, note: str = "", tag: str = "",
                  item_type: str = config.TYPE_PHRASE,
                  phonetic: str = "", pos: str = "",
                  batch_id: int | None = None) -> int:
    """插入一个新词组，返回 phrase_id；已存在（en 相同）返回已有 id。"""
    with get_conn() as conn:
        existing = conn.execute(
            "SELECT id FROM phrases WHERE en = ?", (en,)
        ).fetchone()
        if existing:
            return existing["id"]
        cur = conn.execute(
            """INSERT INTO phrases (en, zh, note, tag, item_type, phonetic,
                                    pos, batch_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (en, zh, note, tag, item_type, phonetic, pos, batch_id),
        )
        return cur.lastrowid


def add_phrase_full(en: str, zh: str, note: str = "", tag: str = "",
                    item_type: str = config.TYPE_PHRASE,
                    phonetic: str = "", pos: str = "",
                    batch_id: int | None = None,
                    batch_name: str | None = None) -> tuple[int, bool]:
    """网页端加词：插入词组并创建双向卡片，返回 (phrase_id, already_exists)。"""
    with get_conn() as conn:
        existing = conn.execute(
            "SELECT id FROM phrases WHERE en = ?", (en,)
        ).fetchone()
        if existing:
            return existing["id"], True
        if batch_name is not None:
            if conn.execute("SELECT 1 FROM batches WHERE name = ?", (batch_name,)).fetchone():
                raise ValueError("已存在同名批次，请直接选择该批次")
            batch_id = conn.execute(
                "INSERT INTO batches (name) VALUES (?)", (batch_name,)
            ).lastrowid
        elif batch_id is not None:
            if not conn.execute("SELECT 1 FROM batches WHERE id = ?", (batch_id,)).fetchone():
                raise ValueError("批次不存在")
        else:
            # 未指定时沿用最近一个批次，不为每个单词生成新批次。
            batch = conn.execute("SELECT id FROM batches ORDER BY id DESC LIMIT 1").fetchone()
            batch_id = batch["id"] if batch else conn.execute(
                "INSERT INTO batches (name) VALUES ('第 1 批')"
            ).lastrowid
        conn.execute("DELETE FROM deleted_phrases WHERE en = ?", (en,))
        cur = conn.execute(
            """INSERT INTO phrases (en, zh, note, tag, item_type, phonetic,
                                    pos, batch_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (en, zh, note, tag, item_type, phonetic, pos, batch_id),
        )
        phrase_id = cur.lastrowid
        for direction in (config.DIR_ZH2EN, config.DIR_EN2ZH):
            conn.execute(
                "INSERT INTO cards (phrase_id, direction) VALUES (?, ?)",
                (phrase_id, direction),
            )
        return phrase_id, False


def update_phrase(phrase_id: int, fields: dict):
    """更新词组字段（en/zh/note/tag/item_type/phonetic/pos/batch_id）。"""
    allowed = ["en", "zh", "note", "tag", "item_type", "phonetic", "pos",
               "batch_id"]
    sets, params = [], []
    for key in allowed:
        if key in fields:
            sets.append(f"{key} = ?")
            params.append(fields[key])
    if not sets:
        return
    params.append(phrase_id)
    with get_conn() as conn:
        previous = conn.execute("SELECT en FROM phrases WHERE id = ?", (phrase_id,)).fetchone()
        if previous is None:
            raise LookupError("词条不存在")
        if fields.get("batch_id") is not None and not conn.execute(
            "SELECT 1 FROM batches WHERE id = ?", (fields["batch_id"],)
        ).fetchone():
            raise ValueError("批次不存在")
        conn.execute(f"UPDATE phrases SET {', '.join(sets)} WHERE id = ?", params)
        if "en" in fields and fields["en"] != previous["en"]:
            conn.execute("INSERT OR IGNORE INTO deleted_phrases (en) VALUES (?)", (previous["en"],))
            conn.execute("DELETE FROM deleted_phrases WHERE en = ?", (fields["en"],))


def delete_phrase(phrase_id: int):
    """删除词组及其卡片、复习记录。"""
    with get_conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO deleted_phrases (en) SELECT en FROM phrases WHERE id = ?",
            (phrase_id,),
        )
        _remove_from_session(conn, phrase_id)
        conn.execute("DELETE FROM review_logs WHERE phrase_id = ?", (phrase_id,))
        conn.execute("DELETE FROM cards WHERE phrase_id = ?", (phrase_id,))
        conn.execute("DELETE FROM phrases WHERE id = ?", (phrase_id,))


def _remove_from_session(conn, phrase_id: int):
    """删除或搁置时，移除该词在当前会话所有阶段的卡片。"""
    ids = {r["id"] for r in conn.execute("SELECT id FROM cards WHERE phrase_id = ?", (phrase_id,))}
    session = conn.execute("SELECT * FROM session_state WHERE id = 1").fetchone()
    if session and ids:
        queues = {key: [i for i in json.loads(session[key]) if i not in ids]
                  for key in ("queue", "pending")}
        snapshots = {key: {direction: [i for i in cards if i not in ids]
                          for direction, cards in json.loads(session[key]).items()}
                     for key in ("new_cards", "review_cards")}
        counts = [max(0, session[count] - int(any(
            i in ids for cards in json.loads(session[key]).values() for i in cards
        ))) for key, count in (("new_cards", "new_count"), ("review_cards", "review_count"))]
        conn.execute(
            "UPDATE session_state SET queue=?, pending=?, new_cards=?, review_cards=?, "
            "new_count=?, review_count=?, version=? WHERE id=1",
            (*[json.dumps(value) for value in (*queues.values(), *snapshots.values())], *counts, uuid4().hex),
        )


def suspend_phrase(phrase_id: int):
    """标记为不再复习：两张卡 suspended，清空到期日。"""
    with get_conn() as conn:
        conn.execute(
            "UPDATE cards SET state = ?, due = NULL WHERE phrase_id = ?",
            (config.STATE_SUSPENDED, phrase_id),
        )
        _remove_from_session(conn, phrase_id)


def resume_phrase(phrase_id: int, today_iso: str):
    """恢复复习：没学过回到 new；学过回到 review 并今天到期。"""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, reps, last_rating FROM cards WHERE phrase_id = ? AND state = ?",
            (phrase_id, config.STATE_SUSPENDED),
        ).fetchall()
        for row in rows:
            if row["last_rating"] is None:
                conn.execute(
                    "UPDATE cards SET state = ?, due = NULL WHERE id = ?",
                    (config.STATE_NEW, row["id"]),
                )
            else:
                conn.execute(
                    "UPDATE cards SET state = ?, due = ? WHERE id = ?",
                    (config.STATE_REVIEW if row["reps"] else config.STATE_LEARNING, today_iso, row["id"]),
                )


def get_all_tags() -> list[str]:
    """所有非空标签，按使用数量排序。"""
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT tag, COUNT(*) AS n FROM phrases
               WHERE tag != '' GROUP BY tag ORDER BY n DESC, tag"""
        ).fetchall()
        return [r["tag"] for r in rows]


# 词组聚合状态（两张卡取"最靠前"的状态）
_STATE_CODE = """
CASE MIN(
    CASE c.state
        WHEN 'new' THEN 0
        WHEN 'learning' THEN 1
        WHEN 'review' THEN 2
        WHEN 'mastered' THEN 3
        WHEN 'suspended' THEN 4
    END)
    WHEN 0 THEN 'new'
    WHEN 1 THEN 'learning'
    WHEN 2 THEN 'review'
    WHEN 3 THEN 'mastered'
    WHEN 4 THEN 'suspended'
END
"""


def list_phrases(query: str = "", tag: str = "", item_type: str = "",
                 state: str = "", page: int = 1, per_page: int = 20,
                 batch_id: int | None = None, sort: str = "added") -> dict:
    """分页列出词组，支持搜索与筛选。"""
    where, params = [], []
    if batch_id is not None:
        where.append("p.batch_id = ?")
        params.append(batch_id)
    order = {"added": "p.id", "en": "p.en COLLATE NOCASE, p.id",
             "type": "p.item_type, p.en COLLATE NOCASE, p.id"}.get(sort, "p.id")
    if query:
        like = f"%{query}%"
        where.append("(p.en LIKE ? OR p.zh LIKE ? OR p.note LIKE ?)")
        params.extend([like, like, like])
    if tag:
        where.append("p.tag = ?")
        params.append(tag)
    if item_type:
        where.append("p.item_type = ?")
        params.append(item_type)
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""

    having = ""
    if state:
        having = "HAVING agg_state = ?"
        params.append(state)

    base_select = f"""
        FROM phrases p JOIN cards c ON c.phrase_id = p.id
        {where_sql}
        GROUP BY p.id
        {having}
    """
    with get_conn() as conn:
        total = conn.execute(
            f"SELECT COUNT(*) FROM (SELECT p.id, {_STATE_CODE} AS agg_state "
            f"{base_select})",
            params,
        ).fetchone()[0]

        rows = conn.execute(
            f"""
            SELECT p.id, p.en, p.zh, p.note, p.tag, p.item_type,
                   p.phonetic, p.pos, p.batch_id,
                   (SELECT name FROM batches WHERE id = p.batch_id) AS batch_name,
                   (SELECT order_mode FROM batches WHERE id = p.batch_id) AS order_mode,
                   {_STATE_CODE} AS agg_state,
                   MAX(c.lapses) AS max_lapses,
                   MIN(c.ease) AS min_ease,
                   MAX(c.interval_days) AS max_interval
            {base_select}
            ORDER BY {order}
            LIMIT ? OFFSET ?
            """,
            params + [per_page, (page - 1) * per_page],
        ).fetchall()

    return {
        "total": total,
        "page": page,
        "per_page": per_page,
        "items": [dict(r) for r in rows],
    }


def get_cram_phrase_ids(limit: int = 30) -> list[int]:
    """突击选词：遗忘次数多、难度系数低、到期早的词优先（排除未学和搁置）。"""
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT phrase_id,
                   SUM(lapses) AS total_lapses,
                   MIN(ease) AS min_ease,
                   MIN(COALESCE(due, '9999')) AS earliest_due
            FROM cards
            WHERE state IN (?, ?, ?)
            GROUP BY phrase_id
            ORDER BY total_lapses DESC, min_ease ASC, earliest_due ASC
            LIMIT ?
            """,
            (config.STATE_LEARNING, config.STATE_REVIEW,
             config.STATE_MASTERED, limit),
        ).fetchall()
        return [r["phrase_id"] for r in rows]


# ---------- 卡片 ----------

def create_cards_for_phrase(phrase_id: int):
    """为词组创建双向两张卡（若不存在）。"""
    with get_conn() as conn:
        for direction in (config.DIR_ZH2EN, config.DIR_EN2ZH):
            conn.execute(
                """INSERT OR IGNORE INTO cards (phrase_id, direction)
                   VALUES (?, ?)""",
                (phrase_id, direction),
            )


def get_card(card_id: int) -> dict | None:
    """取卡片（含词组信息）。"""
    with get_conn() as conn:
        row = conn.execute(
            """
            SELECT c.id AS card_id, c.direction, c.state, c.interval_days,
                   c.reps, c.lapses, c.ease, c.due,
                   p.id AS phrase_id, p.en, p.zh, p.note, p.tag,
                   p.item_type, p.phonetic, p.pos
            FROM cards c JOIN phrases p ON p.id = c.phrase_id
            WHERE c.id = ?
            """,
            (card_id,),
        ).fetchone()
        return dict(row) if row else None


def get_phrase_card_ids(phrase_id: int) -> dict:
    """返回 {direction: card_id}。"""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, direction FROM cards WHERE phrase_id = ?",
            (phrase_id,),
        ).fetchall()
        return {r["direction"]: r["id"] for r in rows}


def get_new_phrase_ids(batch_id: int | None = None) -> list[int]:
    with get_conn() as conn:
        sql = f"SELECT p.id FROM phrases p WHERE ({_UNFINISHED_NEW})"
        params = []
        if batch_id is not None:
            sql += " AND p.batch_id = ?"
            params.append(batch_id)
        return [r["id"] for r in conn.execute(sql, params)]


def get_new_card_ids(direction: str, batch_id: int | None = None) -> list[int]:
    """取某方向未学（new）卡 id，按各批次设置的 order_mode 排列。

    批次之间按批次 id（早→晚）；批次内根据该批次顺序模式：
      sequence 正序（按 phrase id）、reverse 倒序、shuffle 随机乱序。
    传入 batch_id 时只取该批次；否则取全部批次。
    """
    sql = (
        "SELECT c.id AS card_id, p.batch_id AS batch_id, "
        "COALESCE(b.order_mode, ?) AS order_mode\n"
        "FROM cards c\n"
        "JOIN phrases p ON p.id = c.phrase_id\n"
        "LEFT JOIN batches b ON b.id = p.batch_id\n"
        "WHERE c.state IN (?, ?) AND c.direction = ?\n"
        f"AND ({_UNFINISHED_NEW})\n"
    )
    params: list = [config.ORDER_SEQUENCE, config.STATE_NEW, config.STATE_LEARNING, direction]
    if batch_id is not None:
        sql += "AND p.batch_id = ?\n"
        params.append(batch_id)
    sql += "ORDER BY p.batch_id, p.id, c.id"

    groups: dict = {}
    modes: dict = {}
    with get_conn() as conn:
        for r in conn.execute(sql, params):
            bid = r["batch_id"]
            groups.setdefault(bid, []).append(r["card_id"])
            modes[bid] = r["order_mode"]

    result: list = []
    for bid, ids in groups.items():
        mode = modes.get(bid, config.ORDER_SEQUENCE)
        if mode == config.ORDER_REVERSE:
            result.extend(reversed(ids))
        elif mode == config.ORDER_SHUFFLE:
            shuffled = list(ids)
            random.shuffle(shuffled)
            result.extend(shuffled)
        else:
            result.extend(ids)
    return result


def get_due_review_phrase_ids(today_iso: str) -> list[int]:
    """取今天到期的复习/已掌握/学习中卡所属词组 id（去重）。

    learning 卡包含跨天遗留、昨天没学完的词，今天必须继续，不能漏。
    """
    with get_conn() as conn:
        rows = conn.execute(
            f"""
            SELECT DISTINCT c.phrase_id FROM cards c JOIN phrases p ON p.id = c.phrase_id
            WHERE c.state IN (?, ?, ?) AND c.due IS NOT NULL AND c.due <= ?
            AND NOT ({_UNFINISHED_NEW})
            AND NOT EXISTS (SELECT 1 FROM cards blocked WHERE blocked.phrase_id = p.id AND blocked.state = 'suspended')
            ORDER BY c.phrase_id
            """,
            (config.STATE_REVIEW, config.STATE_MASTERED,
             config.STATE_LEARNING, today_iso),
        ).fetchall()
        return [r["phrase_id"] for r in rows]


def update_card(card_id: int, fields: dict, rating: int):
    """按调度结果更新卡片。"""
    with get_conn() as conn:
        conn.execute(
            """
            UPDATE cards SET state = ?, ease = ?, interval_days = ?,
                             reps = ?, lapses = ?, due = ?, last_rating = ?
            WHERE id = ?
            """,
            (
                fields["state"], fields["ease"], fields["interval_days"],
                fields["reps"], fields["lapses"], fields["due"],
                rating, card_id,
            ),
        )


# ---------- 复习日志 ----------

def add_log(card_id: int, rating: int, elapsed_ms: int):
    with get_conn() as conn:
        row = conn.execute(
            "SELECT phrase_id, direction FROM cards WHERE id = ?", (card_id,)
        ).fetchone()
        conn.execute(
            """INSERT INTO review_logs (card_id, phrase_id, direction,
                                        rating, elapsed_ms)
               VALUES (?, ?, ?, ?, ?)""",
            (card_id, row["phrase_id"], row["direction"], rating, elapsed_ms),
        )


# ---------- 统计 ----------

def get_today_log_count(day_iso: str) -> int:
    """指定日期的评级次数（含重做）。"""
    with get_conn() as conn:
        return conn.execute(
            "SELECT COUNT(*) AS n FROM review_logs WHERE date(reviewed_at) = ?",
            (day_iso,),
        ).fetchone()["n"]


def get_overall_stats() -> dict:
    """总体统计。"""
    with get_conn() as conn:
        total = conn.execute("SELECT COUNT(*) AS n FROM phrases").fetchone()["n"]
        learned = conn.execute(
            "SELECT COUNT(DISTINCT phrase_id) AS n FROM cards "
            "WHERE state IN (?, ?)",
            (config.STATE_REVIEW, config.STATE_MASTERED),
        ).fetchone()["n"]
        mastered = conn.execute(
            "SELECT COUNT(DISTINCT phrase_id) AS n FROM cards WHERE state = ?",
            (config.STATE_MASTERED,),
        ).fetchone()["n"]
        return {"total": total, "learned": learned, "mastered": mastered}


def get_due_count(day: date) -> int:
    """指定日期到期的复习词组数。"""
    iso = day.isoformat()
    with get_conn() as conn:
        return conn.execute(
            """SELECT COUNT(DISTINCT phrase_id) AS n FROM cards
               WHERE state IN (?, ?) AND due = ?""",
            (config.STATE_REVIEW, config.STATE_MASTERED, iso),
        ).fetchone()["n"]


def get_streak(today: date) -> int:
    """从今天（或今天没有记录则从昨天）起连续有学习记录的天数。"""
    with get_conn() as conn:
        days = {
            r["d"]
            for r in conn.execute(
                "SELECT DISTINCT date(reviewed_at) AS d FROM review_logs"
            ).fetchall()
        }
    cursor = today
    if cursor.isoformat() not in days:
        cursor -= timedelta(days=1)
    streak = 0
    while cursor.isoformat() in days:
        streak += 1
        cursor -= timedelta(days=1)
    return streak


# 新学卡判定：该中译英卡当天首次通过（此前从无通过记录）
_FIRST_PASS = """
r.direction = 'zh2en' AND r.rating >= 3 AND NOT EXISTS (
    SELECT 1 FROM review_logs x
    WHERE x.card_id = r.card_id AND x.rating >= 3
      AND x.reviewed_at < r.reviewed_at
)
"""


def get_daily_series(days: int = 30, today: date | None = None) -> list[dict]:
    """近 N 天每日新学/复习/遗忘（补齐无记录的日期为 0）。"""
    today = today or date.today()
    start = today - timedelta(days=days - 1)
    with get_conn() as conn:
        rows = conn.execute(
            f"""
            SELECT date(r.reviewed_at) AS d,
                SUM(CASE WHEN {_FIRST_PASS} THEN 1 ELSE 0 END) AS new_learned,
                SUM(CASE WHEN r.rating >= 3 THEN 1 ELSE 0 END)
                  - SUM(CASE WHEN {_FIRST_PASS} THEN 1 ELSE 0 END) AS reviews,
                SUM(CASE WHEN r.rating = 1 THEN 1 ELSE 0 END) AS lapses
            FROM review_logs r
            WHERE date(r.reviewed_at) >= ?
            GROUP BY d
            """,
            (start.isoformat(),),
        ).fetchall()
    data = {r["d"]: dict(r) for r in rows}
    series = []
    for i in range(days):
        day = (start + timedelta(days=i)).isoformat()
        row = data.get(day)
        series.append(
            {
                "date": day,
                "new": row["new_learned"] if row else 0,
                "review": row["reviews"] if row else 0,
                "lapse": row["lapses"] if row else 0,
            }
        )
    return series


def get_activity_map(days: int = 90, today: date | None = None) -> dict:
    """近 N 天每天活动量（评级次数），用于热力图。"""
    today = today or date.today()
    start = today - timedelta(days=days - 1)
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT date(reviewed_at) AS d, COUNT(*) AS n
               FROM review_logs
               WHERE date(reviewed_at) >= ?
               GROUP BY d""",
            (start.isoformat(),),
        ).fetchall()
    return {r["d"]: r["n"] for r in rows}


def get_forever_stats() -> dict:
    """总体评级、遗忘率（Again 占全部评级的比例）。"""
    with get_conn() as conn:
        row = conn.execute(
            """SELECT
                 SUM(CASE WHEN rating = 1 THEN 1 ELSE 0 END) AS again,
                 COUNT(*) AS total
               FROM review_logs"""
        ).fetchone()
    total = row["total"] or 0
    again = row["again"] or 0
    rate = round(again / total * 100, 1) if total else 0.0
    return {"total_ratings": total, "again": again, "forget_rate": rate}
