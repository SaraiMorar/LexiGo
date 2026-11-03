"""CSV 词库导入：把 data/words.csv 同步到数据库。

每次同步中新增的词，统一归到一个新的"批次"（一坨词 = 一个批次）。

用法（.venv 环境）：
    python -m lexigo.importer
"""

import csv

from . import config, models
from .db import init_db


def sync_from_csv(csv_path=None) -> dict:
    """读取 CSV 并导入新词，返回 {added, skipped, batch_id}。"""
    init_db()
    # 历史遗留（无批次）的词先归到第 1 批
    models.ensure_history_batched()
    csv_path = csv_path or config.WORDS_CSV_PATH

    added_ids = []
    skipped = 0
    with open(csv_path, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            en = (row.get("en") or "").strip()
            zh = (row.get("zh") or "").strip()
            if not en or not zh:
                continue

            item_type = (row.get("item_type") or "").strip() or config.TYPE_PHRASE
            if item_type not in (
                config.TYPE_WORD, config.TYPE_PHRASE, config.TYPE_SENTENCE
            ):
                item_type = config.TYPE_PHRASE

            before = models.get_overall_stats()["total"]
            phrase_id = models.insert_phrase(
                en=en,
                zh=zh,
                note=(row.get("note") or "").strip(),
                tag=(row.get("tag") or "").strip(),
                item_type=item_type,
                phonetic=(row.get("phonetic") or "").strip(),
                pos=(row.get("pos") or "").strip(),
            )
            after = models.get_overall_stats()["total"]
            if after > before:
                models.create_cards_for_phrase(phrase_id)
                added_ids.append(phrase_id)
            else:
                skipped += 1

    # 本次新增词归到一个新批次
    batch_id = None
    if added_ids:
        batch_id = models.create_batch(models.next_batch_name())
        for pid in added_ids:
            models.assign_batch(pid, batch_id)

    return {"added": len(added_ids), "skipped": skipped, "batch_id": batch_id}


if __name__ == "__main__":
    result = sync_from_csv()
    print(
        f"导入完成：新增 {result['added']} 条，"
        f"跳过（已存在）{result['skipped']} 条，"
        f"批次 {result['batch_id']}"
    )
