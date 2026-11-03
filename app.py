"""LexiGo 入口：启动本地网页服务。

运行（.venv 环境）：
    .venv\\Scripts\\python.exe app.py
然后浏览器打开 http://127.0.0.1:5000
"""

import csv
import io
import os
import threading
import webbrowser
from datetime import date, timedelta
from functools import wraps

from flask import Flask, jsonify, render_template, request, Response, send_file, url_for

from lexigo import config, models
from lexigo.batch_import import import_pasted_batch, preview_pasted_table
from lexigo.db import get_conn, init_db
from lexigo.importer import sync_from_csv
from lexigo.session import STAGE_DONE, StudySession

app = Flask(__name__)


def write_transaction(handler):
    """会话读取、排期、日志和断点作为一个事务提交。"""
    @wraps(handler)
    def wrapped(*args, **kwargs):
        with get_conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            return handler(*args, **kwargs)
    return wrapped


def integer(value):
    if type(value) not in (int, str):
        raise ValueError("应为整数")
    return int(value)


@app.context_processor
def inject_helpers():
    def static_v(filename):
        """静态资源 URL 带文件修改时间版本号，文件一变 URL 即变，避免浏览器用旧缓存。"""
        path = os.path.join(app.static_folder, filename)
        try:
            ver = int(os.path.getmtime(path))
        except OSError:
            ver = 0
        return url_for("static", filename=filename, v=ver)

    return {"static_v": static_v}


# 启动时：建库 + 同步 CSV 新词
init_db()
sync_from_csv()


@app.get("/")
def index():
    return render_template("index.html")


@app.get("/api/status")
def api_status():
    today = date.today()
    iso = today.isoformat()
    existing = StudySession.load()
    # 只有"未完成"的会话才提示继续；已完成会话不作为"今日完成"依据
    if existing is not None and existing.stage != STAGE_DONE:
        session_info = {"stage": existing.stage, "done": False}
    else:
        session_info = None
    return jsonify(
        {
            "today": iso,
            "new_waiting": len(models.get_new_phrase_ids()),
            "review_due": len(models.get_due_review_phrase_ids(iso)),
            "session": session_info,
            "overall": models.get_overall_stats(),
            "streak": models.get_streak(today),
            "batches": models.list_batches(),
        }
    )


@app.post("/api/session/start")
@write_transaction
def api_start():
    data = request.get_json(silent=True)
    today = date.today()
    if data is not None and not isinstance(data, dict):
        return jsonify({"error": "请求格式不正确"}), 400

    # 主按钮不带请求体：意图是“继续当前未完成会话”，没有才全量新建，
    # 绝不把当前的批次会话误重建为全量。
    if not isinstance(data, dict) or "batch_id" not in data:
        existing = StudySession.load()
        if existing is not None and existing.stage != STAGE_DONE:
            return jsonify(existing.current())
        sess = StudySession.get_or_create(today, None)
        return jsonify(sess.current())

    # 显式带 batch_id 键（点具体批次；null 表示全量）：按目标切换
    raw = data.get("batch_id")
    if raw is not None:
        try:
            batch_id = integer(raw)
        except (TypeError, ValueError):
            return jsonify({"error": "bad request"}), 400
    else:
        batch_id = None
    if batch_id is not None and not any(b["id"] == batch_id for b in models.list_batches()):
        return jsonify({"error": "批次不存在"}), 404
    sess = StudySession.get_or_create(today, batch_id)
    return jsonify(sess.current())


@app.post("/api/session/rate")
@write_transaction
def api_rate():
    data = request.get_json(force=True)
    if not isinstance(data, dict):
        return jsonify({"error": "请求格式不正确"}), 400
    try:
        rating = integer(data.get("rating"))
        elapsed_ms = integer(data.get("elapsed_ms", 0))
        card_id = integer(data["card_id"]) if "card_id" in data else None
    except (TypeError, ValueError):
        return jsonify({"error": "bad request"}), 400
    if rating not in (1, 2, 3, 4) or not 0 <= elapsed_ms <= 2147483647:
        return jsonify({"error": "评分须为 1–4，用时须为非负整数"}), 400
    if "version" in data and (not isinstance(data["version"], str) or not data["version"]):
        return jsonify({"error": "学习状态不正确"}), 400
    today = date.today()
    # 评分始终在当前会话上进行，不改变其批次范围；没有会话才新建
    sess = StudySession.load()
    if sess is None and ("version" in data or card_id is not None):
        return jsonify({"error": "学习状态已变化，请重新开始学习"}), 409
    sess = sess or StudySession.get_or_create(today)
    current = sess.current()
    if (("version" in data and data["version"] != sess.version) or
            (card_id is not None and (current["done"] or card_id != current["card"]["card_id"]))):
        return jsonify({"error": "学习状态已变化，已刷新当前卡片，请重新翻面评分", "state": current}), 409
    return jsonify(sess.rate(rating, elapsed_ms, today))


# ---------- 词库管理 ----------

def _phrase_payload(data) -> dict:
    """从请求体提取并校验词组字段。"""
    if not isinstance(data, dict):
        return None
    if any(value is not None and not isinstance(value, str) for key, value in data.items()
           if key in ("en", "zh", "note", "tag", "item_type", "phonetic", "pos")):
        return None
    en = (data.get("en") or "").strip()
    zh = (data.get("zh") or "").strip()
    if not en or not zh:
        return None
    item_type = (data.get("item_type") or config.TYPE_PHRASE).strip()
    if item_type not in (
        config.TYPE_WORD, config.TYPE_PHRASE, config.TYPE_SENTENCE
    ):
        item_type = config.TYPE_PHRASE
    return {
        "en": en,
        "zh": zh,
        "note": (data.get("note") or "").strip(),
        "tag": (data.get("tag") or "").strip(),
        "item_type": item_type,
        "phonetic": (data.get("phonetic") or "").strip(),
        "pos": (data.get("pos") or "").strip(),
    }


@app.get("/api/tags")
def api_tags():
    return jsonify({"tags": models.get_all_tags()})


@app.get("/api/batches")
def api_batches():
    return jsonify({"batches": models.list_batches()})


@app.post("/api/batches/import/preview")
def api_batch_import_preview():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "请求格式不正确"}), 400
    try:
        return jsonify(preview_pasted_table(data.get("text")))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400


@app.post("/api/batches/import")
def api_batch_import():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "请求格式不正确"}), 400
    try:
        return jsonify(import_pasted_batch(data.get("name"), data.get("rows"),
                                          data.get("item_type", config.TYPE_PHRASE)))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400


@app.put("/api/batches/<int:batch_id>")
def api_batch_rename(batch_id: int):
    data = request.get_json(silent=True) or {}
    name = str(data.get("name") or "").strip() if isinstance(data, dict) else ""
    if not name or len(name) > 100:
        return jsonify({"error": "批次名称应为 1–100 个字符"}), 400
    try:
        renamed = models.rename_batch(batch_id, name)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    if not renamed:
        return jsonify({"error": "批次不存在"}), 404
    return jsonify({"id": batch_id, "name": name})


@app.post("/api/batches/<int:batch_id>/order-mode")
def api_batch_order_mode(batch_id: int):
    data = request.get_json(silent=True) or {}
    order_mode = data.get("order_mode") if isinstance(data, dict) else None
    if order_mode not in config.ORDER_MODES:
        return jsonify({"error": "bad request"}), 400
    ok = models.set_batch_order_mode(batch_id, order_mode)
    if not ok:
        return jsonify({"error": "batch not found"}), 404
    return jsonify({"ok": True, "id": batch_id, "order_mode": order_mode})


@app.get("/api/dashboard")
def api_dashboard():
    today = date.today()
    due_next_week = [
        {"date": (today + timedelta(days=i + 1)).isoformat(),
         "count": models.get_due_count(today + timedelta(days=i + 1))}
        for i in range(7)
    ]
    return jsonify(
        {
            "series": models.get_daily_series(30, today),
            "activity": models.get_activity_map(90, today),
            "overall": models.get_overall_stats(),
            "forever": models.get_forever_stats(),
            "streak": models.get_streak(today),
            "due_next_week": due_next_week,
        }
    )


@app.get("/api/phrases")
def api_phrases():
    try:
        page = max(1, int(request.args.get("page", 1)))
        per_page = min(100, max(5, int(request.args.get("per_page", 20))))
    except (TypeError, ValueError):
        page, per_page = 1, 20
    raw_batch = request.args.get("batch_id", "")
    try:
        batch_id = int(raw_batch) if raw_batch else None
    except ValueError:
        return jsonify({"error": "批次格式不正确"}), 400
    result = models.list_phrases(
        query=request.args.get("q", "").strip(),
        tag=request.args.get("tag", "").strip(),
        item_type=request.args.get("type", "").strip(),
        state=request.args.get("state", "").strip(),
        page=page,
        per_page=per_page,
        batch_id=batch_id,
        sort=request.args.get("sort", "added"),
    )
    return jsonify(result)


@app.post("/api/phrases")
def api_phrases_create():
    data = request.get_json(force=True)
    payload = _phrase_payload(data)
    if payload is None:
        return jsonify({"error": "英文和中文不能为空"}), 400
    # 批次和词条一起保存，重复词不会留下空批次。
    try:
        batch_id = int(data.get("batch_id")) if data.get("batch_id") else None
    except (TypeError, ValueError):
        return jsonify({"error": "批次格式不正确"}), 400
    payload["batch_id"] = batch_id
    if "batch_name" in data:
        name = str(data["batch_name"] or "").strip()
        if not name or len(name) > 100:
            return jsonify({"error": "请填写批次名称（1–100 个字符）"}), 400
        payload["batch_name"] = name
    try:
        phrase_id, existed = models.add_phrase_full(**payload)
    except Exception as exc:  # 重复 en 等
        return jsonify({"error": f"保存失败：{exc}"}), 400
    return jsonify({"id": phrase_id, "existed": existed}), 200


@app.put("/api/phrases/<int:phrase_id>")
def api_phrases_update(phrase_id: int):
    data = request.get_json(force=True)
    payload = _phrase_payload(data)
    if payload is None:
        return jsonify({"error": "英文和中文不能为空"}), 400
    if "batch_id" in data:
        try:
            payload["batch_id"] = int(data["batch_id"]) if data["batch_id"] is not None else None
        except (TypeError, ValueError):
            return jsonify({"error": "批次格式不正确"}), 400
    try:
        models.update_phrase(phrase_id, payload)
    except LookupError as exc:
        return jsonify({"error": str(exc)}), 404
    except Exception as exc:
        return jsonify({"error": f"更新失败：{exc}"}), 400
    return jsonify({"id": phrase_id})


@app.delete("/api/phrases/<int:phrase_id>")
def api_phrases_delete(phrase_id: int):
    models.delete_phrase(phrase_id)
    return jsonify({"deleted": phrase_id})


@app.post("/api/phrases/<int:phrase_id>/suspend")
def api_phrases_suspend(phrase_id: int):
    models.suspend_phrase(phrase_id)
    return jsonify({"id": phrase_id, "suspended": True})


@app.post("/api/phrases/<int:phrase_id>/resume")
def api_phrases_resume(phrase_id: int):
    models.resume_phrase(phrase_id, date.today().isoformat())
    return jsonify({"id": phrase_id, "resumed": True})


# ---------- 导出 ----------

@app.get("/api/export/csv")
def api_export_csv():
    """导出全量词库为 CSV（与 words.csv 同结构）。"""
    result = models.list_phrases(page=1, per_page=100000)
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["item_type", "en", "zh", "phonetic", "pos", "tag", "note",
                     "batch_name", "order_mode"])
    for p in result["items"]:
        writer.writerow(
            [p["item_type"], p["en"], p["zh"], p["phonetic"] or "",
             p["pos"] or "", p["tag"] or "", p["note"] or "",
             p["batch_name"] or "", p["order_mode"] or config.ORDER_SEQUENCE]
        )
    return Response(
        "\ufeff" + buf.getvalue(),
        mimetype="text/csv; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=lexigo_export.csv"},
    )


@app.get("/api/export/db")
def api_export_db():
    """下载完整数据库备份。"""
    return send_file(
        config.DB_PATH,
        as_attachment=True,
        download_name="lexigo_backup.db",
    )


# ---------- 突击模式 ----------

@app.post("/api/cram/start")
@write_transaction
def api_cram_start():
    data = request.get_json(force=True)
    if not isinstance(data, dict):
        return jsonify({"error": "请求格式不正确"}), 400
    try:
        limit = min(200, max(1, int(data.get("limit", 30))))
    except (TypeError, ValueError):
        limit = 30
    sess = StudySession.create_cram(limit, date.today())
    return jsonify(sess.current())


if __name__ == "__main__":
    # 服务启动后自动打开浏览器
    threading.Timer(1.2, lambda: webbrowser.open("http://127.0.0.1:5000")).start()
    # 仅本机访问；调试模式关闭，避免误触
    app.run(host="127.0.0.1", port=5000, debug=False)
