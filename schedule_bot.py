#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Бот расписания: следит за страницей расписания колледжа и присылает
в Telegram только расписание своей группы.

Сейчас умеет:
  • ежедневные изменения (PDF с текстовым слоем) — разбирает по координатам
    и присылает текстом;
  • команды /start, /help, /now.

Запуск:  python schedule_bot.py
"""

import json
import os
import re
import sys
import time
import hashlib
import shutil
import socket
import ssl
import http.client
import urllib.parse
import urllib.request
import urllib.error
import traceback

try:
    import numpy as np
except Exception:  # в GitHub Actions numpy не нужен (только текст)
    np = None

import pymupdf

try:
    from PIL import Image
    HAS_IMAGE = np is not None
except Exception:  # в GitHub Actions Pillow не нужен (только текст)
    HAS_IMAGE = False

BASE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE, "config.json")
STATE_PATH = os.path.join(BASE, "state.json")
LOG_PATH = os.path.join(BASE, "bot.log")

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) schedule-bot/1.0"
SITE = "https://www.fa.ru"

TIME_RE = re.compile(r"^\d{1,2}[.:]\d{2}-\d{1,2}[.:]\d{2}$")
DATE_RE = re.compile(r"(\d{2})\.(\d{2})\.(\d{4})")
LINK_RE = re.compile(
    r'<a[^>]*class="document-card__text"[^>]*href="([^"]+)"[^>]*>(.*?)</a>',
    re.S,
)
TEACHER_RE = re.compile(
    r"^(.*?)\s+([А-ЯЁ][а-яё\-]+)\s+([А-ЯЁ]\.\s*[А-ЯЁ]\.)\s*\(([^()]*)\)\s*$",
    re.S,
)


# --------------------------------------------------------------------------- утилиты
def load_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    # В GitHub Actions токен и чат берутся из секретов, а не из файла
    cfg["token"] = os.environ.get("BOT_TOKEN") or cfg.get("token", "")
    if os.environ.get("CHAT_ID"):
        cfg["chat_id"] = int(os.environ["CHAT_ID"])
    return cfg


def load_state():
    if os.path.exists(STATE_PATH):
        try:
            with open(STATE_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"files": {}, "first_run": True, "updates_offset": 0, "sent": {}}


def save_state(state):
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE_PATH)


def log(msg):
    line = "%s %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
    print(line, flush=True)
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def http_get(url, timeout=90):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def http_head(url, timeout=30):
    req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return {k.lower(): v for k, v in r.headers.items()}
    except Exception as e:
        log("HEAD failed %s: %s" % (url, e))
        return {}


def clean(text):
    return re.sub(r"\s+", " ", text).strip()


# --------------------------------------------------------------------------- Telegram
API_HOST = "api.telegram.org"
# Резервные IP Telegram: DNS часто отдаёт заблокированный адрес,
# поэтому при ошибке пробуем подключиться к рабочим IP напрямую (SNI остаётся api.telegram.org).
API_IPS = [
    "149.154.167.220",
    "149.154.167.197",
    "149.154.175.50",
    "91.108.56.130",
]
_API_IP = None  # запоминаем рабочий IP, чтобы не тратить время на мёртвый DNS


def _https_json(path, body=None, headers=None, timeout=40):
    """POST/GET к api.telegram.org: сначала по DNS, при ошибке — по резервным IP."""
    global _API_IP
    method = "POST" if body is not None else "GET"

    if _API_IP is None:
        try:
            req = urllib.request.Request(
                "https://%s%s" % (API_HOST, path), data=body, headers=headers or {}, method=method
            )
            with urllib.request.urlopen(req, timeout=min(15, timeout)) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception as e:
            log("api.telegram.org недоступен (%s), пробую по IP" % e)

    ctx = ssl.create_default_context()
    last = None
    candidates = ([_API_IP] if _API_IP else []) + [ip for ip in API_IPS if ip != _API_IP]
    for ip in candidates:
        try:
            raw = socket.create_connection((ip, 443), timeout=min(12, timeout))
            sock = ctx.wrap_socket(raw, server_hostname=API_HOST)
            conn = http.client.HTTPSConnection(API_HOST, context=ctx, timeout=timeout)
            conn.sock = sock
            hdrs = dict(headers or {})
            hdrs["Host"] = API_HOST
            conn.request(method, path, body=body, headers=hdrs)
            resp = conn.getresponse()
            data = resp.read()
            conn.close()
            _API_IP = ip
            return json.loads(data.decode("utf-8"))
        except Exception as e:
            last = e
            continue
    raise last or RuntimeError("нет доступного IP Telegram")


def tg(token, method, payload):
    path = "/bot%s/%s" % (token, method)
    data = json.dumps(payload).encode("utf-8")
    try:
        return _https_json(path, body=data, headers={"Content-Type": "application/json"})
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        log("TG error %s: %s %s" % (method, e.code, body[:300]))
        return {"ok": False}
    except Exception as e:
        log("TG error %s: %s" % (method, e))
        return {"ok": False}


def send_text(cfg, text, attempts=4):
    """Отправка текста с повторами: связь с Telegram бывает нестабильной."""
    for i in range(attempts):
        res = tg(cfg["token"], "sendMessage", {
            "chat_id": cfg["chat_id"],
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        })
        if res.get("ok"):
            return True
        if i + 1 < attempts:
            delay = min(90, 5 * (2 ** i))
            log("sendMessage: повтор через %d с" % delay)
            time.sleep(delay)
    return False


def send_document(cfg, path, caption="", attempts=3):
    """Отправка файла (multipart/form-data) без внешних зависимостей, с повторами."""
    for i in range(attempts):
        ok = _send_document_once(cfg, path, caption)
        if ok:
            return True
        if i + 1 < attempts:
            delay = min(90, 10 * (2 ** i))
            log("sendDocument: повтор через %d с" % delay)
            time.sleep(delay)
    return False


def _send_document_once(cfg, path, caption=""):
    token, chat_id = cfg["token"], cfg["chat_id"]
    boundary = "----bot" + hashlib.md5(str(time.time()).encode()).hexdigest()
    with open(path, "rb") as f:
        file_data = f.read()
    filename = os.path.basename(path)
    lower = filename.lower()
    mime = ("image/png" if lower.endswith(".png")
            else "image/jpeg" if lower.endswith((".jpg", ".jpeg"))
            else "application/pdf")

    parts = []

    def field(name, value):
        parts.append(
            ("--%s\r\nContent-Disposition: form-data; name=\"%s\"\r\n\r\n%s\r\n"
             % (boundary, name, value)).encode("utf-8")
        )

    field("chat_id", str(chat_id))
    if caption:
        field("caption", caption)
    parts.append(
        ("--%s\r\nContent-Disposition: form-data; name=\"document\"; filename=\"%s\"\r\n"
         "Content-Type: %s\r\n\r\n" % (boundary, filename, mime)).encode("utf-8")
    )
    parts.append(file_data)
    parts.append(("\r\n--%s--\r\n" % boundary).encode("utf-8"))
    body = b"".join(parts)

    try:
        res = _https_json(
            "/bot%s/sendDocument" % token,
            body=body,
            headers={"Content-Type": "multipart/form-data; boundary=%s" % boundary},
            timeout=90,
        )
        return bool(res.get("ok"))
    except Exception as e:
        log("sendDocument error: %s" % e)
        return False


# --------------------------------------------------------------------------- страница
def fetch_links(cfg):
    html = http_get(cfg["page_url"]).decode("utf-8", "replace")
    links = []
    for href, title in LINK_RE.findall(html):
        title = clean(re.sub(r"<[^>]+>", "", title))
        if not href.lower().endswith(".pdf"):
            continue
        url = href if href.startswith("http") else SITE + href
        links.append((url, title))
    return links


def classify(title):
    m = DATE_RE.search(title)
    if m:
        return "changes", "%s.%s.%s" % (m.group(1), m.group(2), m.group(3))
    if "недел" in title.lower():
        return "weekly", None
    return None, None


# --------------------------------------------------------------------------- разбор PDF изменений
def parse_changes(pdf_bytes, group):
    """Возвращает список пар: [(номер, время, текст ячейки), ...] для нужной группы."""
    doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    page = doc[0]
    words = [w for w in page.get_text("words") if w[4].strip()]
    if not words:
        return []

    words.sort(key=lambda w: (w[1], w[0]))
    times = [w for w in words if TIME_RE.match(w[4])]
    if not times:
        return []
    times.sort(key=lambda w: w[1])
    header_bottom = times[0][1] - 2.5

    head = [w for w in words if w[1] < header_bottom]
    merged = []
    for w in sorted(head, key=lambda w: w[0]):
        if merged and w[0] - merged[-1]["x1"] < 1.3:
            merged[-1]["text"] += " " + w[4]
            merged[-1]["x1"] = w[2]
        else:
            merged.append({"text": w[4], "x0": w[0], "x1": w[2]})

    def norm(s):
        return re.sub(r"[\s\u00a0]+", "", s).lower()

    target = norm(group)
    idx = next((i for i, m in enumerate(merged) if norm(m["text"]) == target), None)
    if idx is None:
        idx = next((i for i, m in enumerate(merged) if norm(m["text"]).startswith(target)), None)
    if idx is None:
        return []

    cur = merged[idx]
    left = (merged[idx - 1]["x1"] + cur["x0"]) / 2 if idx > 0 else cur["x0"] - 2
    right = (cur["x1"] + merged[idx + 1]["x0"]) / 2 if idx + 1 < len(merged) else cur["x1"] + 4

    rows = []
    # легенда внизу листа («замена преподавателя», «лекционное занятие») — не часть таблицы
    legend_y = None
    for w in words:
        if w[4] in ("замена", "лекционное", "кабинета") and w[0] < 130:
            legend_y = w[1] if legend_y is None else min(legend_y, w[1])
    table_bottom = (legend_y - 1) if legend_y else page.rect.height

    for n, t in enumerate(times):
        y0 = t[1] - 2.4
        y1 = times[n + 1][1] - 2.4 if n + 1 < len(times) else table_bottom
        cell = [w for w in words if left <= (w[0] + w[2]) / 2 <= right and y0 <= w[1] < y1]
        cell.sort(key=lambda w: (round(w[1], 1), w[0]))
        text = clean(" ".join(w[4] for w in cell))
        if text:
            rows.append((n + 1, t[4], text))
    return rows


def split_cell(text):
    m = TEACHER_RE.match(text)
    if m:
        subject = clean(m.group(1))
        teacher = "%s %s" % (m.group(2), clean(m.group(3)))
        room = clean(m.group(4))
        return subject, teacher, room
    return text, "", ""


def _cluster(idx, gap=4):
    groups = []
    for v in idx:
        if groups and v - groups[-1][-1] <= gap:
            groups[-1].append(v)
        else:
            groups.append([v])
    return [int(np.mean(g)) for g in groups]


def build_weekly_column(pdf_bytes, column_index, out_path, label_cols=3, dpi=200):
    """Вырезает из скана колонку нужной группы + колонку с днями/парами."""
    if not HAS_IMAGE:
        raise RuntimeError("для недельного расписания нужны numpy и Pillow")
    doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    page = doc[0]
    pix = page.get_pixmap(dpi=dpi)
    img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
    gray = np.asarray(img.convert("L"))
    h, w = gray.shape
    dark = gray < 215

    vlines = _cluster(np.where(dark.sum(axis=0) > 0.5 * h)[0])
    hlines = _cluster(np.where(dark.sum(axis=1) > 0.35 * w)[0])
    if len(vlines) < label_cols + column_index:
        raise RuntimeError("не нашёл сетку таблицы (вертикальных линий: %d)" % len(vlines))

    top, bottom = (hlines[0], hlines[-1]) if hlines else (0, h)
    lx0, lx1 = vlines[0], vlines[label_cols - 1]
    gx0 = vlines[label_cols - 1 + column_index - 1]
    gx1 = vlines[label_cols - 1 + column_index]

    left = img.crop((lx0, top, lx1, bottom))
    group = img.crop((gx0, top, gx1, bottom))

    gap = 6
    canvas = Image.new("RGB", (left.width + gap + group.width, left.height), "white")
    canvas.paste(left, (0, 0))
    canvas.paste(group, (left.width + gap, 0))
    canvas = canvas.resize((int(canvas.width * 1.6), int(canvas.height * 1.6)), Image.LANCZOS)
    canvas.save(out_path, "JPEG", quality=88)
    return out_path


def fmt_time(t):
    m = re.match(r"^(\d{1,2})[.:](\d{2})-(\d{1,2})[.:](\d{2})$", t)
    if not m:
        return t
    return "%s:%s–%s:%s" % (m.group(1), m.group(2), m.group(3), m.group(4))


def build_changes_message(group, date, rows):
    lines = ["🗓 <b>Изменения на %s</b>" % date, "Группа: <b>%s</b>" % group, ""]
    for num, tm, text in rows:
        subject, teacher, room = split_cell(text)
        tail = ""
        if teacher:
            tail = " — %s" % teacher
        if room:
            tail += ", каб. %s" % room
        lines.append("<b>%d пара</b> · %s" % (num, fmt_time(tm)))
        lines.append("%s%s" % (subject, tail))
        lines.append("")
    lines.append("<i>по данным fa.ru</i>")
    return "\n".join(lines)


# --------------------------------------------------------------------------- основная логика
def process_changes(cfg, url, title, date, state):
    log("обрабатываю изменения: %s (%s)" % (title, date))
    try:
        data = http_get(url)
    except Exception as e:
        log("не скачал %s: %s" % (url, e))
        return False
    rows = parse_changes(data, cfg["group"])
    if not rows:
        log("группа %s не найдена в %s" % (cfg["group"], title))
        return False
    msg = build_changes_message(cfg["group"], date, rows)
    ok = send_text(cfg, msg)
    log("отправлено: %s" % ok)
    if ok:
        state.setdefault("sent", {})[url] = time.strftime("%Y-%m-%d %H:%M:%S")
    return ok


def process_weekly(cfg, url, title, state):
    wcfg = cfg.get("weekly") or {}
    column = int(wcfg.get("column", 3))
    log("обрабатываю недельное расписание: %s (колонка %d)" % (title, column))
    try:
        data = http_get(url)
    except Exception as e:
        log("не скачал %s: %s" % (url, e))
        return False
    tmp = os.path.join(BASE, "tmp")
    os.makedirs(tmp, exist_ok=True)
    out = os.path.join(tmp, "week.jpg")
    try:
        build_weekly_column(data, column, out)
    except Exception as e:
        log("не смог вырезать колонку: %s" % e)
        return False
    caption = "📅 %s\nГруппа: %s" % (title, cfg["group"])
    ok = send_document(cfg, out, caption)
    log("недельное отправлено: %s" % ok)
    if ok:
        state.setdefault("sent", {})[url] = time.strftime("%Y-%m-%d %H:%M:%S")
    return ok


def check_once(cfg, state):
    links = fetch_links(cfg)
    log("ссылок на странице: %d" % len(links))

    weekly_cfg = cfg.get("weekly") or {}
    weekly_needle = (weekly_cfg.get("title_contains") or "").lower()
    first_run = bool(state.get("first_run"))

    pending_changes = []
    pending_weekly = []
    for url, title in links:
        kind, date = classify(title)
        if kind is None:
            continue

        head = http_head(url)
        etag = head.get("etag", "")
        lastmod = head.get("last-modified", "")
        length = head.get("content-length", "")

        prev = state["files"].get(url)
        changed = prev is None or prev.get("etag") != etag or prev.get("last_modified") != lastmod

        state["files"][url] = {
            "title": title,
            "kind": kind,
            "date": date,
            "etag": etag,
            "last_modified": lastmod,
            "content_length": length,
        }
        if not changed:
            continue

        if kind == "changes":
            pending_changes.append((url, title, date))
        elif weekly_needle and weekly_needle in title.lower():
            pending_weekly.append((url, title))

    if first_run:
        # на первом запуске не спамим старыми файлами
        pending_changes.sort(key=lambda x: time.strptime(x[2], "%d.%m.%Y"), reverse=True)
        pending_changes = pending_changes[:1]
        pending_weekly = [x for x in pending_weekly if "1 неделя" in x[1].lower()][:1]
        state["first_run"] = False

    for url, title, date in pending_changes:
        try:
            process_changes(cfg, url, title, date, state)
        except Exception:
            log("ОШИБКА обработки %s:\n%s" % (title, traceback.format_exc()))

    for url, title in pending_weekly:
        try:
            process_weekly(cfg, url, title, state)
        except Exception:
            log("ОШИБКА недельного %s:\n%s" % (title, traceback.format_exc()))

    save_state(state)


HELP_TEXT = (
    "Привет! Я слежу за расписанием колледжа и присылаю только твою группу.\n\n"
    "Группа: <b>%s</b>\n"
    "Проверяю сайт каждые %d мин.\n\n"
    "Команды:\n"
    "/now — изменения за последний день\n"
    "/week — недельное расписание (вырезка по группе)\n"
    "/help — эта справка"
)


def handle_commands(cfg, state):
    offset = state.get("updates_offset", 0)
    try:
        res = tg(cfg["token"], "getUpdates", {"offset": offset, "timeout": 0, "limit": 20})
    except Exception:
        return
    if not res.get("ok"):
        return
    for upd in res.get("result", []):
        offset = max(offset, upd["update_id"] + 1)
        msg = upd.get("message") or {}
        text = (msg.get("text") or "").strip().lower()
        chat_id = (msg.get("chat") or {}).get("id")
        if chat_id != cfg["chat_id"]:
            continue
        if text in ("/start", "/help", "start", "help"):
            send_text(cfg, HELP_TEXT % (cfg["group"], cfg["interval_minutes"]))
        elif text == "/now":
            try:
                state_copy = dict(state)
                state_copy["first_run"] = True
                check_once(cfg, state_copy)
            except Exception:
                log("ошибка /now:\n%s" % traceback.format_exc())
        elif text == "/week":
            try:
                needle = ((cfg.get("weekly") or {}).get("title_contains") or "").lower()
                cands = [
                    (u, t) for u, t in fetch_links(cfg)
                    if needle and needle in t.lower() and "1 неделя" in t.lower()
                ]
                if cands:
                    process_weekly(cfg, cands[0][0], cands[0][1], state)
                else:
                    send_text(cfg, "Не нашёл недельное расписание на сайте.")
            except Exception:
                log("ошибка /week:\n%s" % traceback.format_exc())
    state["updates_offset"] = offset


def main():
    cfg = load_config()
    state = load_state()
    once = "--once" in sys.argv
    log("=== бот запущен, группа: %s ===" % cfg["group"])

    if state.get("first_run"):
        send_text(cfg, "Бот запущен. Группа: <b>%s</b>.\nБуду присылать изменения, как только они появятся." % cfg["group"])

    while True:
        try:
            check_once(cfg, state)
            handle_commands(cfg, state)
        except Exception:
            log("ОШИБКА в цикле:\n%s" % traceback.format_exc())
        if once:
            return
        time.sleep(max(1, int(cfg.get("interval_minutes", 10))) * 60)


if __name__ == "__main__":
    main()
