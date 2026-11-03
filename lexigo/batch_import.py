"""粘贴表格的解析、预览与原子批次导入，不改动已有词条和进度。"""

import csv
import io
import re
import unicodedata

from . import config
from .db import get_conn

MAX_TEXT = 100_000
MAX_ROWS = 1_000
EN_HEADERS = {"en", "english", "word", "words", "phrase", "phrases", "expression",
              "英文", "英语", "单词", "词组", "英文词组", "英文短语", "英文表达"}
ZH_HEADERS = {"zh", "chinese", "meaning", "translation", "中文", "汉语", "释义",
              "中文释义", "中文意思", "中文含义", "中文翻译", "翻译", "意思"}
NOTE_HEADERS = {"note", "notes", "example", "备注", "例句", "注释"}
HAN = re.compile(r"[\u3400-\u9fff]")


def english_key(value):
    return " ".join(unicodedata.normalize("NFKC", value).translate(
        str.maketrans({"’": "'", "‘": "'"})
    ).casefold().split())


def validate_pair(row):
    if not isinstance(row, dict):
        raise ValueError("词条格式不正确")
    clean = {}
    for field, limit in (("en", 1000), ("zh", 2000), ("note", 4000)):
        value = row.get(field, "")
        if not isinstance(value, str) or len(value) > limit:
            raise ValueError("词条内容过长或格式不正确")
        clean[field] = value.strip()
    if not clean["en"] or not re.search(r"[a-zA-Z]", clean["en"]):
        raise ValueError("请补全英文")
    if not clean["zh"]:
        raise ValueError("请补全中文释义")
    return clean


def _cells(text):
    """优先保留制表符和 CSV 引号，其次识别 Markdown 和逐行中英文本。"""
    if "\t" in text:
        reader = csv.reader(io.StringIO(text), delimiter="\t")
        for row in reader:
            yield reader.line_num, row
        return
    lines = text.splitlines()
    if any(line.strip().startswith("|") for line in lines):
        for number, line in enumerate(lines, 1):
            yield number, re.split(r"(?<!\\)\|", line.strip().strip("|"))
        return
    try:
        dialect = csv.Sniffer().sniff(text[:8192], delimiters=",，;")
    except csv.Error:
        dialect = None
    if dialect:
        sample = [row for row in csv.reader(io.StringIO(text), dialect) if any(row)]
        has_header = any(any(cell.strip().lower() in EN_HEADERS for cell in row)
                         and any(cell.strip().lower() in ZH_HEADERS for cell in row) for row in sample)
        if not has_header and not all(len(row) >= 2 and (
            (not HAN.search(row[0]) and (HAN.search(row[1]) or row[0].strip().lower() in EN_HEADERS))
            or (HAN.search(row[0]) and not HAN.search(row[1]) and re.search(r"[a-zA-Z]", row[1]))
        ) for row in sample):
            dialect = None
    if dialect:
        reader = csv.reader(io.StringIO(text), dialect)
        for row in reader:
            yield reader.line_num, row
        return
    for number, line in enumerate(lines, 1):
        line = re.sub(r"^\s*(?:\d+[.、．)]|[（(]\d+[)）])\s*", "", line).strip()
        tokens = line.split()
        if any(token.lower() in EN_HEADERS for token in tokens) and any(token.lower() in ZH_HEADERS for token in tokens):
            cells = tokens
        else:
            cells = re.split(r"\s{2,}|\s*\|\s*", line)
        if len(cells) == 1:
            match = HAN.search(line)
            if match and match.start() > 0:
                cells = [line[:match.start()].rstrip(" :：-–—"), line[match.start():]]
            else:
                cells = line.split() if line.lower().split() in (["english", "chinese"], ["en", "zh"]) else [line]
        yield number, cells


def parse_pasted_table(text):
    if not isinstance(text, str) or not text.strip():
        raise ValueError("请先粘贴表格内容")
    if len(text) > MAX_TEXT:
        raise ValueError("内容过长，请分批导入（每次最多 10 万字符）")
    rows, headers = [], 0
    mapping = None
    try:
        for number, cells in _cells(text.lstrip("\ufeff")):
            cells = [cell.strip().replace("\\|", "|") for cell in cells]
            if not any(cells):
                continue
            if all(not cell or re.fullmatch(r":?-{3,}:?", cell) for cell in cells):
                headers += 1
                continue
            labels = [re.sub(r"\s+", "", cell).lower() for cell in cells]
            en_index = next((i for i, label in enumerate(labels) if label in EN_HEADERS), None)
            zh_index = next((i for i, label in enumerate(labels) if label in ZH_HEADERS), None)
            if en_index is not None and zh_index is not None:
                english = [i for i, label in enumerate(labels) if label in EN_HEADERS]
                chinese = [i for i, label in enumerate(labels) if label in ZH_HEADERS]
                note = next((i for i, label in enumerate(labels) if label in NOTE_HEADERS), None)
                mapping = [(en, zh, note if len(english) == 1 else None)
                           for en, zh in zip(english, chinese)]
                headers += 1
                continue
            if mapping:
                pairs = [tuple(cells[i] if i is not None and i < len(cells) else "" for i in group)
                         for group in mapping]
            else:
                numbered_pairs = len(cells) >= 6 and len(cells) % 3 == 0 and all(
                    re.fullmatch(r"\d+[.、．)]?", cells[i]) for i in range(0, len(cells), 3)
                )
                if len(cells) > 2 and re.fullmatch(r"\d+[.、．)]?", cells[0]):
                    if not numbered_pairs:
                        cells = cells[1:]
                while cells and not cells[-1]:
                    cells.pop()
                if numbered_pairs:
                    pairs = [(cells[i + 1], cells[i + 2], "") for i in range(0, len(cells), 3)]
                elif len(cells) >= 4 and len(cells) % 2 == 0:
                    pairs = [(cells[i], cells[i + 1], "") for i in range(0, len(cells), 2)]
                else:
                    pairs = [(cells[0] if cells else "", cells[1] if len(cells) > 1 else "",
                              " · ".join(cells[2:]))]
                pairs = [(zh, en, note) if HAN.search(en) and not HAN.search(zh) and re.search(r"[a-zA-Z]", zh)
                         else (en, zh, note) for en, zh, note in pairs]
            for en, zh, note in pairs:
                if not en and not zh:
                    continue
                rows.append({"line": number, "en": en, "zh": zh, "note": note})
                if len(rows) > MAX_ROWS:
                    raise ValueError("词条过多，请分批导入（每次最多 1000 条）")
    except csv.Error as exc:
        raise ValueError("表格格式不完整，请检查引号或改为直接复制表格") from exc
    if not rows:
        raise ValueError("没有识别到词条，请粘贴包含英文和中文释义的表格")
    return rows, headers


def preview_pasted_table(text):
    rows, headers = parse_pasted_table(text)
    with get_conn() as conn:
        existing = {english_key(row["en"]) for row in conn.execute("SELECT en FROM phrases")}
    seen = set()
    for row in rows:
        try:
            validate_pair(row)
        except ValueError as exc:
            row.update(status="invalid", message=str(exc))
            continue
        key = english_key(row["en"])
        if key in existing:
            row.update(status="existing", message="词库已有，保留原批次和进度")
        elif key in seen:
            row.update(status="duplicate", message="本次内容重复")
        else:
            row.update(status="new", message="待导入")
        seen.add(key)
    return {"rows": rows, "headers_skipped": headers}


def import_pasted_batch(name, rows, item_type=config.TYPE_PHRASE):
    if not isinstance(name, str) or not 1 <= len(name.strip()) <= 100:
        raise ValueError("请填写批次名称（1–100 个字符）")
    if item_type not in (config.TYPE_WORD, config.TYPE_PHRASE, config.TYPE_SENTENCE):
        raise ValueError("词条类型不正确")
    if not isinstance(rows, list) or not 1 <= len(rows) <= MAX_ROWS:
        raise ValueError("请选择 1–1000 条词条导入")
    clean = [validate_pair(row) for row in rows]
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        name = name.strip()
        if conn.execute("SELECT 1 FROM batches WHERE name = ?", (name,)).fetchone():
            raise ValueError("已存在同名批次，请换一个名称")
        existing = {english_key(row["en"]) for row in conn.execute("SELECT en FROM phrases")}
        new_rows, seen = [], set()
        skipped_existing = skipped_duplicate = 0
        for row in clean:
            key = english_key(row["en"])
            if key in existing:
                skipped_existing += 1
            elif key in seen:
                skipped_duplicate += 1
            else:
                new_rows.append(row)
            seen.add(key)
        if not new_rows:
            raise ValueError("所选词条都已存在，未创建空批次；原批次和学习进度保持不变")
        batch_id = conn.execute("INSERT INTO batches (name) VALUES (?)", (name,)).lastrowid
        for row in new_rows:
            phrase_id = conn.execute(
                "INSERT INTO phrases (en, zh, note, item_type, batch_id) VALUES (?, ?, ?, ?, ?)",
                (row["en"], row["zh"], row["note"], item_type, batch_id),
            ).lastrowid
            conn.executemany("INSERT INTO cards (phrase_id, direction) VALUES (?, ?)",
                             [(phrase_id, config.DIR_ZH2EN), (phrase_id, config.DIR_EN2ZH)])
            conn.execute("DELETE FROM deleted_phrases WHERE en = ?", (row["en"],))
    return {"batch_id": batch_id, "name": name, "added": len(new_rows),
            "skipped_existing": skipped_existing, "skipped_duplicate": skipped_duplicate}
