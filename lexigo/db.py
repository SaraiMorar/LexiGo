"""SQLite 数据库连接与建表。"""

import sqlite3
from contextlib import contextmanager

from . import config


SCHEMA = """
CREATE TABLE IF NOT EXISTS csv_import_state (
    en TEXT PRIMARY KEY,
    payload TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS deleted_phrases (
    en TEXT PRIMARY KEY
);

CREATE TABLE IF NOT EXISTS batches (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    order_mode  TEXT NOT NULL DEFAULT 'sequence',
    created_at  TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

CREATE TABLE IF NOT EXISTS phrases (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    en          TEXT NOT NULL UNIQUE,
    zh          TEXT NOT NULL,
    note        TEXT NOT NULL DEFAULT '',
    tag         TEXT NOT NULL DEFAULT '',
    item_type   TEXT NOT NULL DEFAULT 'phrase',
    phonetic    TEXT NOT NULL DEFAULT '',
    pos         TEXT NOT NULL DEFAULT '',
    batch_id    INTEGER,
    created_at  TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

CREATE TABLE IF NOT EXISTS cards (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    phrase_id     INTEGER NOT NULL,
    direction     TEXT NOT NULL,
    state         TEXT NOT NULL DEFAULT 'new',
    ease          REAL NOT NULL DEFAULT 2.5,
    interval_days INTEGER NOT NULL DEFAULT 0,
    reps          INTEGER NOT NULL DEFAULT 0,
    lapses        INTEGER NOT NULL DEFAULT 0,
    due           TEXT,
    last_rating   INTEGER,
    UNIQUE (phrase_id, direction),
    FOREIGN KEY (phrase_id) REFERENCES phrases (id)
);

CREATE TABLE IF NOT EXISTS review_logs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    card_id     INTEGER NOT NULL,
    phrase_id   INTEGER NOT NULL,
    direction   TEXT NOT NULL,
    rating      INTEGER NOT NULL,
    elapsed_ms  INTEGER NOT NULL DEFAULT 0,
    reviewed_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    FOREIGN KEY (card_id) REFERENCES cards (id)
);

CREATE TABLE IF NOT EXISTS session_state (
    id            INTEGER PRIMARY KEY CHECK (id = 1),
    session_date  TEXT NOT NULL,
    session_type  TEXT NOT NULL DEFAULT 'normal',
    batch_id      INTEGER,
    stage         TEXT NOT NULL,
    queue         TEXT NOT NULL DEFAULT '[]',
    pending       TEXT NOT NULL DEFAULT '[]',
    new_cards     TEXT NOT NULL DEFAULT '{}',
    review_cards  TEXT NOT NULL DEFAULT '{}',
    new_count     INTEGER NOT NULL DEFAULT 0,
    review_count  INTEGER NOT NULL DEFAULT 0,
    lapse_count   INTEGER NOT NULL DEFAULT 0,
    started_at    TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

CREATE INDEX IF NOT EXISTS idx_cards_due ON cards (due);
CREATE INDEX IF NOT EXISTS idx_cards_state ON cards (state);
CREATE INDEX IF NOT EXISTS idx_logs_time ON review_logs (reviewed_at);
"""


@contextmanager
def get_conn():
    """获取数据库连接（行工厂为 sqlite3.Row），自动提交与关闭。"""
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    """初始化数据库表结构，并对旧库做兼容迁移。"""
    with get_conn() as conn:
        conn.executescript(SCHEMA)
        # 旧库 session_state 可能缺 session_type 列
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(session_state)")}
        if "session_type" not in cols:
            conn.execute(
                "ALTER TABLE session_state ADD COLUMN session_type "
                "TEXT NOT NULL DEFAULT 'normal'"
            )
        # 旧库 session_state 可能缺 batch_id 列
        if "batch_id" not in cols:
            conn.execute(
                "ALTER TABLE session_state ADD COLUMN batch_id INTEGER"
            )
        # 旧库 phrases 可能缺 batch_id 列
        pcols = {r["name"] for r in conn.execute("PRAGMA table_info(phrases)")}
        if "batch_id" not in pcols:
            conn.execute("ALTER TABLE phrases ADD COLUMN batch_id INTEGER")
        # 旧库 batches 可能缺 order_mode 列（背诵顺序：sequence/shuffle/reverse）
        bcols = {r["name"] for r in conn.execute("PRAGMA table_info(batches)")}
        if "order_mode" not in bcols:
            conn.execute(
                "ALTER TABLE batches ADD COLUMN order_mode "
                "TEXT NOT NULL DEFAULT 'sequence'"
            )
