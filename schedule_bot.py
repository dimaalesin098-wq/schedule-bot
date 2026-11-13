#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Бот расписания колледжа (GitHub Actions, несколько пользователей).

• Следит за страницей расписания, ищет новые/изменённые PDF изменений.
• Для каждого подписчика разбирает колонку ЕГО группы и присылает текстом.
• Команды: /start, /help, /groups, /group НАЗВАНИЕ, /mygroup, /stop.
• Подписки хранятся в subscriptions.json (файл коммитится workflow'ом).

Запуск локально:  python schedule_bot.py --once
"""

import json
import os
import re
import sys
import time
import hashlib
import socket
import ssl
import http.client
import urllib.parse
import urllib.request
import urllib.error
import traceback

import pymupdf

BASE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE, "config.json")
STATE_PATH = os.path.join(BASE, "state.json")
SUBS_PATH = os.path.join(BASE, "subscriptions.json")
LOG_PATH = os.path.join(BASE, "bot.log")

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) schedule-bot/2.0"
SITE = "https://www.fa.ru"

TIME_RE = re.compile(r"^\d{1,2}[.:]\d{2}-\d{1,2}[.:]\d{2}$")
DATE_RE = re.compile(r"(\d{2})\.(\d{2})\.(\d{4})")
LINK_RE = re.compile(
    r'<a[^>]*class="document-card__text"[^>]*href="([^"]+)"[^>]*>(.*?)</a>',
    re.S,
)
TEACHER_RE = re.compile(
    r"([А-ЯЁ][а-яё-]+)\s+([А-ЯЁ]\.\s*[А-ЯЁ]\.)\s*\(([^()]*)\)",
)


# --------------------------------------------------------------------------- файлы
def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def load_config():
    cfg = load_json(CONFIG_PATH, {})
    cfg["token"] = os.environ.get("BOT_TOKEN") or cfg.get("token", "")
    if os.environ.get("CHAT_ID"):
        cfg["chat_id"] = int(os.environ["CHAT_ID"])
    return cfg


def log(msg):
    line = "%s %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
    print(line, flush=True)
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def load_state():
    state = load_json(STATE_PATH, {})
    state.setdefault("files", {})
    state.setdefault("sent", {})
    state.setdefault("updates_offset", 0)
    state.setdefault("first_run", True)
    return state


def load_subs(cfg):
    data = load_json(SUBS_PATH, None)
    if data is None:
        # миграция: раньше был один чат и одна группа в config.json
        subs = []
        if cfg.get("chat_id") and cfg.get("group"):
            subs = [{
                "chat_id": int(cfg["chat_id"]),
                "group": cfg["group"],
                "active": True,
                "added": time.strftime("%Y-%m-%d"),
            }]
        return {"subs": subs}
    data.setdefault("subs", [])
    return data


def save_subs(subs):
    save_json(SUBS_PATH, subs)


def norm(s):
    return re.sub(r"[\s\u00a0]+", "", str(s or "")).lower()


# --------------------------------------------------------------------------- http
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
    return re.sub(r"\s+", " ", str(text or "")).strip()


# --------------------------------------------------------------------------- Telegram
API_HOST = "api.telegram.org"
API_IPS = [
    "149.154.167.220",
    "149.154.167.197",
    "149.154.175.50",
    "91.108.56.130",
]
_API_IP = None


def _https_json(path, body=None, headers=None, timeout=40):
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
    try:
        return _https_json(
            "/bot%s/%s" % (token, method),
            body=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
    except Exception as e:
        log("TG error %s: %s" % (method, e))
        return {"ok": False}


def send_text(cfg, chat_id, text, attempts=3):
    for i in range(attempts):
        res = tg(cfg["token"], "sendMessage", {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        })
        if res.get("ok"):
            return True
        if i + 1 < attempts:
            time.sleep(min(60, 4 * (2 ** i)))
    return False


# --------------------------------------------------------------------------- страница
def fetch_links(cfg):
    html = http_get(cfg["page_url"]).decode("utf-8", "replace")
    links = []
    for href, title in LINK_RE.findall(html):
        title = clean(re.sub(r"<[^>]+>", "", title))
        if not href.lower().endswith(".pdf"):
            continue
        links.append((href if href.startswith("http") else SITE + href, title))
    return links


def classify(title):
    m = DATE_RE.search(title)
    if m:
        return "changes", "%s.%s.%s" % (m.group(1), m.group(2), m.group(3))
    if "недел" in title.lower():
        return "weekly", None
    return None, None


# --------------------------------------------------------------------------- разбор PDF
def _page_items(page):
    words = [w for w in page.get_text("words") if w[4].strip()]
    words.sort(key=lambda w: (w[1], w[0]))
    return words


def _times(words):
    return sorted([w for w in words if TIME_RE.match(w[4])], key=lambda w: w[1])


def _header(words, header_bottom):
    merged = []
    for w in sorted([x for x in words if x[1] < header_bottom], key=lambda w: w[0]):
        if merged and w[0] - merged[-1]["x1"] < 1.3:
            merged[-1]["text"] += " " + w[4]
            merged[-1]["x1"] = w[2]
        else:
            merged.append({"text": w[4], "x0": w[0], "x1": w[2]})
    return merged


GROUP_RE = re.compile(r"^(РУПО|ЭБУ|СД|ОДЛ|Ф|БД|ЮР|ИСП)\s*\d+[а-яё]?-\d{2}$", re.I)


def list_groups(pdf_bytes):
    """Все названия групп из шапки PDF изменений (с фильтром мусора)."""
    doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    page = doc[0]
    words = _page_items(page)
    times = _times(words)
    if not times:
        return []
    names = []
    for m in _header(words, times[0][1] - 2.5):
        name = clean(m["text"])
        if GROUP_RE.match(name) and name not in names:
            names.append(name)
    return names


def parse_changes(pdf_bytes, group):
    doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    page = doc[0]
    words = _page_items(page)
    times = _times(words)
    if not times:
        return []

    header = _header(words, times[0][1] - 2.5)
    target = norm(group)
    idx = next((i for i, m in enumerate(header) if norm(m["text"]) == target), None)
    if idx is None:
        idx = next((i for i, m in enumerate(header) if norm(m["text"]).startswith(target)), None)
    if idx is None:
        return []

    cur = header[idx]
    left = (header[idx - 1]["x1"] + cur["x0"]) / 2 if idx > 0 else cur["x0"] - 2
    right = (cur["x1"] + header[idx + 1]["x0"]) / 2 if idx + 1 < len(header) else cur["x1"] + 4

    legend_y = None
    for w in words:
        if w[4] in ("замена", "лекционное", "кабинета") and w[0] < 130:
            legend_y = w[1] if legend_y is None else min(legend_y, w[1])
    table_bottom = (legend_y - 1) if legend_y else page.rect.height

    rows = []
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
    found = list(TEACHER_RE.finditer(text))
    if found:
        subject = clean(text[:found[0].start()])
        tail = " / ".join(
            "%s %s%s" % (m.group(1), clean(m.group(2)), (", каб. " + clean(m.group(3))) if m.group(3) else "")
            for m in found
        )
        return subject, tail
    return text, ""


def fmt_time(t):
    m = re.match(r"^(\d{1,2})[.:](\d{2})-(\d{1,2})[.:](\d{2})$", t)
    return "%s:%s–%s:%s" % m.groups() if m else t


def build_changes_message(group, date, rows):
    lines = ["🗓 <b>Изменения на %s</b>" % date, "Группа: <b>%s</b>" % group, ""]
    for num, tm, text in rows:
        subject, tail = split_cell(text)
        suffix = (" — %s" % tail) if tail else ""
        lines.append("<b>%d пара</b> · %s" % (num, fmt_time(tm)))
        lines.append("%s%s" % (subject, suffix))
        lines.append("")
    lines.append("<i>по данным fa.ru</i>")
    return "\n".join(lines)


# --------------------------------------------------------------------------- логика
HELP_TEXT = (
    "👋 Бот присылает изменения в расписании колледжа по <b>твоей</b> группе.\n\n"
    "Команды:\n"
    "/groups — список групп\n"
    "/group РУПО 9б-26 — подписаться на группу\n"
    "/mygroup — какая группа выбрана\n"
    "/stop — отписаться\n"
    "/help — эта справка\n\n"
    "Работает и в личке, и в групповом чате. Проверка каждые ~10 минут."
)


def apply_command(cfg, subs, chat_id, text, groups):
    t = (text or "").strip()
    low = t.lower()
    entry = next((s for s in subs["subs"] if int(s["chat_id"]) == int(chat_id)), None)

    if low in ("/start", "/help", "start", "help", "/помощь"):
        send_text(cfg, chat_id, HELP_TEXT)
        return True

    if low == "/mygroup":
        if entry and entry.get("active"):
            send_text(cfg, chat_id, "Твоя группа: <b>%s</b>" % entry["group"])
        else:
            send_text(cfg, chat_id, "Ты пока не подписан. Напиши /groups и выбери группу.")
        return True

    if low == "/stop":
        if entry:
            entry["active"] = False
            send_text(cfg, chat_id, "Отписал. Вернуться: /group НАЗВАНИЕ")
        else:
            send_text(cfg, chat_id, "Ты и так не подписан.")
        return True

    if low.startswith("/groups"):
        if not groups:
            send_text(cfg, chat_id, "Не смог получить список групп — попробуй позже.")
            return True
        chunk = []
        size = 0
        for g in groups:
            if size + len(g) + 2 > 3500:
                send_text(cfg, chat_id, "\n".join(chunk))
                chunk, size = [], 0
            chunk.append(g)
            size += len(g) + 2
        if chunk:
            send_text(cfg, chat_id, "<b>Группы:</b>\n" + "\n".join(chunk))
        return True

    if low.startswith("/group"):
        asked = clean(t[len("/group"):])
        if not asked:
            send_text(cfg, chat_id, "Напиши так: <code>/group РУПО 9б-26</code>")
            return True
        if not groups:
            send_text(cfg, chat_id, "Список групп сейчас недоступен — попробуй позже.")
            return True
        exact = next((g for g in groups if norm(g) == norm(asked)), None)
        if exact is None:
            import difflib
            close = difflib.get_close_matches(asked, groups, n=3, cutoff=0.5)
            hint = ("\n\nПохожие: " + ", ".join(close)) if close else ""
            send_text(cfg, chat_id, "Группа «%s» не найдена.%s" % (asked, hint))
            return True
        if entry:
            entry["group"] = exact
            entry["active"] = True
        else:
            subs["subs"].append({
                "chat_id": int(chat_id),
                "group": exact,
                "active": True,
                "added": time.strftime("%Y-%m-%d"),
            })
        send_text(cfg, chat_id, "Готово! Буду присылать изменения для <b>%s</b>." % exact)
        return True

    return False


def get_groups(cfg, links):
    """Список групп из самого свежего файла изменений."""
    changes = [(u, t, d) for (u, t) in links for (kind, d) in [classify(t)] if kind == "changes"]
    if not changes:
        return []
    changes.sort(key=lambda x: time.strptime(x[2], "%d.%m.%Y"), reverse=True)
    url = changes[0][0]
    try:
        return list_groups(http_get(url))
    except Exception as e:
        log("не смог получить список групп: %s" % e)
        return []


def handle_commands(cfg, state, subs):
    offset = state.get("updates_offset", 0)
    res = tg(cfg["token"], "getUpdates", {"offset": offset, "timeout": 0, "limit": 50})
    if not res.get("ok"):
        return
    groups = None
    for upd in res.get("result", []):
        offset = max(offset, upd["update_id"] + 1)
        msg = upd.get("message") or upd.get("edited_message") or {}
        chat_id = (msg.get("chat") or {}).get("id")
        text = msg.get("text") or ""
        if not chat_id or not text.startswith("/"):
            continue
        if groups is None:
            try:
                groups = get_groups(cfg, fetch_links(cfg))
            except Exception as e:
                log("ошибка списка групп: %s" % e)
                groups = []
        try:
            handled = apply_command(cfg, subs, chat_id, text, groups)
            if handled:
                save_subs(subs)
        except Exception:
            log("ошибка команды:\n%s" % traceback.format_exc())
    state["updates_offset"] = offset


def migrate_state(state, cfg):
    """Старый формат sent: {url: время} → {url|chat: время}."""
    sent = state.get("sent", {})
    if any("|" not in k for k in sent):
        migrated = {}
        for k, v in sent.items():
            if "|" in k:
                migrated[k] = v
            else:
                migrated["%s|%s" % (k, cfg.get("chat_id"))] = v
        state["sent"] = migrated
    return state


def check_once(cfg, state, subs):
    links = fetch_links(cfg)
    log("ссылок: %d | подписок: %d" % (len(links), len(subs["subs"])))

    first_run = bool(state.get("first_run"))
    pending = []
    for url, title in links:
        kind, date = classify(title)
        if kind != "changes":
            continue
        head = http_head(url)
        etag, lm = head.get("etag", ""), head.get("last-modified", "")
        prev = state["files"].get(url)
        changed = prev is None or prev.get("etag") != etag or prev.get("last_modified") != lm
        state["files"][url] = {"title": title, "date": date, "etag": etag, "last_modified": lm}
        if changed:
            pending.append((url, title, date))

    pending.sort(key=lambda x: time.strptime(x[2], "%d.%m.%Y"), reverse=True)
    if first_run:
        pending = pending[:1]
        state["first_run"] = False
    if not pending:
        save_json(STATE_PATH, state)
        return

    active = [s for s in subs["subs"] if s.get("active")]
    for url, title, date in pending:
        try:
            data = http_get(url)
        except Exception as e:
            log("не скачал %s: %s" % (url, e))
            continue
        cache = {}
        for s in active:
            group = s["group"]
            key = norm(group)
            if key not in cache:
                cache[key] = parse_changes(data, group)
            rows = cache[key]
            if not rows:
                log("для группы %s в %s нет занятий — пропускаю" % (group, title))
                continue
            chat = int(s["chat_id"])
            sent_key = "%s|%s" % (url, chat)
            if sent_key in state["sent"]:
                continue
            if send_text(cfg, chat, build_changes_message(group, date, rows)):
                state["sent"][sent_key] = time.strftime("%Y-%m-%d %H:%M:%S")
                log("отправлено %s -> %s" % (date, chat))

    save_json(STATE_PATH, state)


def main():
    cfg = load_config()
    state = migrate_state(load_state(), cfg)
    subs = load_subs(cfg)
    if not os.path.exists(SUBS_PATH):
        save_subs(subs)  # создаём файл подписок, чтобы он попал в репозиторий
    once = "--once" in sys.argv
    log("=== бот v2 запущен, подписок: %d ===" % len(subs["subs"]))

    while True:
        try:
            handle_commands(cfg, state, subs)
            check_once(cfg, state, subs)
        except Exception:
            log("ОШИБКА в цикле:\n%s" % traceback.format_exc())
        if once:
            return
        time.sleep(600)


if __name__ == "__main__":
    main()
