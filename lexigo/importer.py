"""CSV 增量导入：保留网页修改、删除记录与学习进度。"""

import csv
import json

from . import config, models
from .db import get_conn, init_db

FIELDS = ("zh", "note", "tag", "item_type", "phonetic", "pos")


def sync_from_csv(csv_path=None) -> dict:
    """首次对齐已有词时只记录基线；以后仅同步未在网页改动的字段。"""
    init_db()
    models.ensure_history_batched()
    csv_path = csv_path or config.WORDS_CSV_PATH
    added = updated = skipped = 0
    batch_id = None
    with open(csv_path, encoding="utf-8-sig", newline="") as source, get_conn() as conn:
        for row in csv.DictReader(source):
            en = (row.get("en") or "").strip()
            zh = (row.get("zh") or "").strip()
            if not en or not zh:
                continue
            if conn.execute("SELECT 1 FROM deleted_phrases WHERE en = ?", (en,)).fetchone():
                skipped += 1
                continue
            payload = {field: (row.get(field) or "").strip() for field in FIELDS}
            if payload["item_type"] not in (config.TYPE_WORD, config.TYPE_PHRASE, config.TYPE_SENTENCE):
                payload["item_type"] = config.TYPE_PHRASE
            current = conn.execute("SELECT * FROM phrases WHERE en = ?", (en,)).fetchone()
            previous = conn.execute("SELECT payload FROM csv_import_state WHERE en = ?", (en,)).fetchone()
            if current:
                changes = {}
                if previous:
                    baseline = json.loads(previous["payload"])
                    changes = {field: value for field, value in payload.items()
                               if current[field] == baseline.get(field) and current[field] != value}
                if changes:
                    assignments = ", ".join(f"{field} = ?" for field in changes)
                    conn.execute(f"UPDATE phrases SET {assignments} WHERE id = ?",
                                 [*changes.values(), current["id"]])
                    updated += 1
                else:
                    skipped += 1
            else:
                name = (row.get("batch_name") or "").strip()
                if name:
                    batch = conn.execute("SELECT id FROM batches WHERE name = ? ORDER BY id LIMIT 1", (name,)).fetchone()
                    if batch:
                        target_batch = batch["id"]
                    else:
                        mode = row.get("order_mode", config.ORDER_SEQUENCE)
                        if mode not in config.ORDER_MODES:
                            mode = config.ORDER_SEQUENCE
                        target_batch = conn.execute(
                            "INSERT INTO batches (name, order_mode) VALUES (?, ?)", (name, mode)
                        ).lastrowid
                else:
                    if batch_id is None:
                        number = conn.execute("SELECT COALESCE(MAX(id), 0) + 1 FROM batches").fetchone()[0]
                        batch_id = conn.execute(
                            "INSERT INTO batches (name) VALUES (?)", (f"第 {number} 批",)
                        ).lastrowid
                    target_batch = batch_id
                phrase_id = conn.execute(
                    "INSERT INTO phrases (en, zh, note, tag, item_type, phonetic, pos, batch_id) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    [en, *payload.values(), target_batch],
                ).lastrowid
                conn.executemany(
                    "INSERT INTO cards (phrase_id, direction) VALUES (?, ?)",
                    [(phrase_id, config.DIR_ZH2EN), (phrase_id, config.DIR_EN2ZH)],
                )
                added += 1
            conn.execute(
                "INSERT INTO csv_import_state (en, payload) VALUES (?, ?) "
                "ON CONFLICT(en) DO UPDATE SET payload = excluded.payload",
                (en, json.dumps(payload, ensure_ascii=False)),
            )
    return {"added": added, "updated": updated, "skipped": skipped, "batch_id": batch_id}


if __name__ == "__main__":
    print(sync_from_csv())
