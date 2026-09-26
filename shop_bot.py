# -*- coding: utf-8 -*-
"""
Магазин цифровых товаров в Telegram: Stars / Premium / Подарки.
Один файл, библиотека pyTelegramBotAPI, база SQLite.

Оплата (клиент выбирает сам):
  1) С баланса бота (внутренний счёт)
  2) CryptoBot (Crypto Pay API) - автоматический счёт, бот сам видит оплату
  3) Крипто-чек CryptoBot - клиент присылает ссылку на чек, админ активирует и подтверждает
  4) Перевод на кошелёк - клиент присылает TXID/скриншот, админ подтверждает

Баланс пополняется теми же способами (кроме оплаты с самого баланса).

Реферальная система: за каждого приглашённого рефереру начисляется сумма на баланс
(размер и режим - «за вход» или «за первую оплату» - меняются в админке).

Админка (/admin): заказы, товары, пользователи и балансы, рефералы, рассылка (текст+фото+кнопки),
«Контент и кнопки» - приветствие с фото, свои страницы (текст+фото+кнопки), свои кнопки в меню.

Установка:  pip install pyTelegramBotAPI requests
Запуск:     BOT_TOKEN=... ADMIN_IDS=123,456 CRYPTOPAY_TOKEN=... python shop_bot.py
"""
import html
import logging
import os
import re
import sqlite3
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import quote

import requests
import telebot
from telebot import types

# ===================== НАСТРОЙКИ =====================
BOT_TOKEN = os.getenv("BOT_TOKEN", "PUT_BOT_TOKEN_HERE")
ADMIN_IDS = [int(x) for x in os.getenv("ADMIN_IDS", "123456789").split(",") if x.strip()]
# Токен приложения: @CryptoBot -> Crypto Pay -> Create App. Пусто = автооплата отключена.
CRYPTOPAY_TOKEN = os.getenv("CRYPTOPAY_TOKEN", "")
# Для теста: https://testnet-pay.crypt.bot/api
CRYPTOPAY_API = os.getenv("CRYPTOPAY_API", "https://pay.crypt.bot/api")
CRYPTOPAY_ASSETS = "USDT,TON,BTC,ETH,LTC,USDC"
DB_FILE = os.getenv("DB_FILE", "shop.db")
CURRENCY = "USD"  # цены и баланс хранятся и показываются в этой валюте
MAX_TOPUP = 100000  # верхний предел одного пополнения

CATS = {
    "stars": "⭐ Telegram Stars",
    "premium": "💎 Telegram Premium",
    "gifts": "🎁 Подарки",
}

# Стартовые товары. Цены - ПРИМЕРНЫЕ, поменяйте в админке (Товары).
SEED = [
    ("stars", "⭐ 50 Stars", 0.9), ("stars", "⭐ 100 Stars", 1.7),
    ("stars", "⭐ 250 Stars", 4.2), ("stars", "⭐ 500 Stars", 8.2),
    ("stars", "⭐ 1000 Stars", 16),
    ("premium", "💎 Premium 3 мес.", 12), ("premium", "💎 Premium 6 мес.", 16),
    ("premium", "💎 Premium 12 мес.", 28),
    ("gifts", "🧸 Подарок «Мишка»", 0.35), ("gifts", "❤️ Подарок «Сердце»", 0.35),
    ("gifts", "🎁 Подарок «Коробка»", 0.6), ("gifts", "🌹 Подарок «Роза»", 0.6),
    ("gifts", "🚀 Подарок «Ракета»", 1.2), ("gifts", "🏆 Подарок «Кубок»", 2.3),
]

DEFAULTS = {
    "welcome": "👋 <b>Магазин цифровых товаров</b>\n\nTelegram Stars, Premium и подарки. "
               "Оплата криптовалютой или с баланса. Выберите действие:",
    "welcome_photo": "",
    "wallets": "USDT (TRC20): ВАШ_АДРЕС\nTON: ВАШ_АДРЕС",
    "support": "@your_support",
    "ref_on": "1",          # реферальная программа включена
    "ref_bonus": "0.5",     # сколько начислять рефереру за 1 человека (в CURRENCY)
    "ref_mode": "start",    # start = за вход по ссылке, paid = после первой оплаты приглашённого
    "min_topup": "1",       # минимальное пополнение баланса
    "notify_new": "1",      # уведомлять админов о новых пользователях
    "force_channels": "",   # обязательная подписка: @канал1,@канал2 (пусто = выключено)
    "reviews_public": "1",  # показывать отзывы покупателям (1) или только админу (0)
}

STATUS = {
    "new": "🆕 Создан", "wait": "⏳ Ждёт оплаты", "review": "🔎 На проверке",
    "paid": "✅ Оплачен, готовим выдачу", "done": "🎉 Выполнен",
    "rejected": "❌ Отклонён", "cancel": "🚫 Отменён",
}
METHOD = {"cb": "CryptoBot (авто)", "check": "Крипто-чек", "wallet": "Перевод на кошелёк",
          "bal": "С баланса"}
TXK = {"topup": "Пополнение", "purchase": "Покупка", "ref": "Реферальный бонус",
       "admin": "Корректировка админом", "refund": "Возврат"}

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("shop")

if BOT_TOKEN == "PUT_BOT_TOKEN_HERE":
    sys.exit("Укажите BOT_TOKEN (переменная окружения или в начале файла).")

bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")
BOT_USERNAME = ""  # заполняется при старте (для реферальных ссылок)

# ===================== БАЗА ДАННЫХ =====================
# Если заданы TURSO_DATABASE_URL/TURSO_AUTH_TOKEN — используется удалённая
# база Turso (нужна для хостингов вроде Render, где диск не сохраняется
# между перезапусками). Если не заданы — как раньше, локальный файл SQLite.
#
# С Turso бот общается напрямую по её официальному HTTP-протоколу
# ("Hrana over HTTP", эндпоинт /v2/pipeline) через requests, без
# сторонних библиотек - у пакета libsql_client оказались нестабильные
# WebSocket- и HTTP-клиенты, ломающиеся именно на этом хостинге.
def _clean_env(v):
    """Убирает частые огрехи копирования на телефоне: пробелы/переносы
    строк по краям, обрамляющие кавычки, случайно попавшее слово Bearer
    (с двоеточием или без) перед самим значением."""
    v = (v or "").strip().strip('"').strip("'").strip()
    v = re.sub(r"(?i)^bearer[:\s]+", "", v).strip()
    return v


TURSO_URL = _clean_env(os.getenv("TURSO_DATABASE_URL", "")).rstrip("/")
TURSO_TOKEN = _clean_env(os.getenv("TURSO_AUTH_TOKEN", ""))

_lock = threading.RLock()


def _turso_encode_arg(v):
    if v is None:
        return {"type": "null"}
    if isinstance(v, bool):
        return {"type": "integer", "value": str(int(v))}
    if isinstance(v, int):
        return {"type": "integer", "value": str(v)}
    if isinstance(v, float):
        return {"type": "float", "value": str(v)}
    if isinstance(v, (bytes, bytearray)):
        import base64
        return {"type": "blob", "base64": base64.b64encode(v).decode()}
    return {"type": "text", "value": str(v)}


def _turso_decode_cell(cell):
    t = cell.get("type")
    if t == "null":
        return None
    if t == "integer":
        return int(cell["value"])
    if t == "float":
        return float(cell["value"])
    if t == "blob":
        import base64
        return base64.b64decode(cell["base64"])
    return cell.get("value")


def _turso_execute(sql, args):
    url = re.sub(r"^libsql://", "https://", TURSO_URL).rstrip("/") + "/v2/pipeline"
    body = {"requests": [
        {"type": "execute", "stmt": {"sql": sql, "args": [_turso_encode_arg(a) for a in args]}},
        {"type": "close"},
    ]}
    r = requests.post(url, json=body,
                      headers={"Authorization": f"Bearer {TURSO_TOKEN}"}, timeout=20)
    if r.status_code != 200:
        raise RuntimeError(f"Turso HTTP {r.status_code}: {r.text[:500]}")
    data = r.json()
    res = data["results"][0]
    if res["type"] == "error":
        raise RuntimeError(f"Turso SQL error: {res['error']} | sql={sql!r} args={args!r}")
    result = res["response"]["result"]
    cols = [c["name"] for c in result.get("cols", [])]
    rows = [dict(zip(cols, [_turso_decode_cell(c) for c in row])) for row in result.get("rows", [])]
    rowcount = result.get("affected_row_count", 0) or len(rows)
    lastrowid = result.get("last_insert_rowid")
    return rows, rowcount, lastrowid


if TURSO_URL:
    DB_BACKEND = "turso"
    if TURSO_TOKEN and not TURSO_TOKEN.startswith("eyJ"):
        log.warning("TURSO_AUTH_TOKEN не похож на настоящий токен Turso (обычно начинается "
                    "с 'eyJ'). Похоже, при копировании попало что-то лишнее - проверьте "
                    "значение в Render → Environment.")
    log_msg = (f"База данных: Turso ({re.sub(r'^https?://|^libsql://', '', TURSO_URL)}), "
               f"токен: {len(TURSO_TOKEN)} симв., начинается с '{TURSO_TOKEN[:6]}…'")
else:
    DB_BACKEND = "sqlite"
    _raw = sqlite3.connect(DB_FILE, check_same_thread=False)
    _raw.row_factory = sqlite3.Row
    log_msg = f"База данных: локальный файл {DB_FILE}"


class _Result:
    __slots__ = ("_rows", "rowcount", "lastrowid")

    def __init__(self, rows, rowcount, lastrowid):
        self._rows = rows
        self.rowcount = rowcount
        self.lastrowid = lastrowid

    def fetchall(self):
        return self._rows


def ex(sql, args=()):
    """Выполняет один SQL-запрос. Работает одинаково с локальным SQLite
    и с удалённой базой Turso — вся остальная часть бота не знает, какая
    из них используется."""
    args = list(args)
    with _lock:
        if DB_BACKEND == "turso":
            rows, rowcount, lastrowid = _turso_execute(sql, args)
            return _Result(rows, rowcount, lastrowid)
        cur = _raw.execute(sql, args)
        _raw.commit()
        return _Result([dict(r) for r in cur.fetchall()], cur.rowcount, cur.lastrowid)


def q(sql, args=()):
    return ex(sql, args).fetchall()


def q1(sql, args=()):
    r = q(sql, args)
    return r[0] if r else None


def _add_col(table, col, ddl):
    cols = [r["name"] for r in q(f"PRAGMA table_info({table})")]
    if col not in cols:
        ex(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")


SCHEMA = [
    """CREATE TABLE IF NOT EXISTS users(
        id INTEGER PRIMARY KEY, username TEXT, first_name TEXT, joined INTEGER)""",
    """CREATE TABLE IF NOT EXISTS products(
        id INTEGER PRIMARY KEY AUTOINCREMENT, cat TEXT, title TEXT,
        price REAL, active INTEGER DEFAULT 1)""",
    """CREATE TABLE IF NOT EXISTS orders(
        id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, product_id INTEGER,
        title TEXT, price REAL, target TEXT, method TEXT, status TEXT, proof TEXT,
        invoice_id INTEGER, created INTEGER, updated INTEGER)""",
    "CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT)",
    """CREATE TABLE IF NOT EXISTS tx(
        id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, amount REAL,
        kind TEXT, note TEXT, created INTEGER)""",
    "CREATE INDEX IF NOT EXISTS tx_user ON tx(user_id)",
    """CREATE TABLE IF NOT EXISTS pages(
        id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT, text TEXT, photo TEXT DEFAULT '')""",
    """CREATE TABLE IF NOT EXISTS buttons(
        id INTEGER PRIMARY KEY AUTOINCREMENT, parent TEXT, label TEXT,
        kind TEXT, value TEXT, sort INTEGER DEFAULT 0)""",
    "CREATE INDEX IF NOT EXISTS btn_parent ON buttons(parent)",
    """CREATE TABLE IF NOT EXISTS screens(
        key TEXT PRIMARY KEY, text TEXT, photo TEXT DEFAULT '')""",
    """CREATE TABLE IF NOT EXISTS promocodes(
        code TEXT PRIMARY KEY, kind TEXT, value REAL,
        max_uses INTEGER DEFAULT 0, used INTEGER DEFAULT 0,
        active INTEGER DEFAULT 1, created INTEGER)""",
    """CREATE TABLE IF NOT EXISTS promo_uses(
        id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT, user_id INTEGER,
        order_id INTEGER, created INTEGER)""",
    """CREATE TABLE IF NOT EXISTS reviews(
        id INTEGER PRIMARY KEY AUTOINCREMENT, order_id INTEGER UNIQUE, user_id INTEGER,
        username TEXT, stars INTEGER, text TEXT, created INTEGER)""",
    """CREATE TABLE IF NOT EXISTS tasks(
        id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT, channel TEXT,
        reward REAL DEFAULT 0, active INTEGER DEFAULT 1, created INTEGER)""",
    """CREATE TABLE IF NOT EXISTS task_done(
        user_id INTEGER, task_id INTEGER, created INTEGER,
        PRIMARY KEY(user_id, task_id))""",
    """CREATE TABLE IF NOT EXISTS achievements(
        id INTEGER PRIMARY KEY AUTOINCREMENT, emoji TEXT DEFAULT '🏆', title TEXT,
        text TEXT, reward REAL DEFAULT 0, active INTEGER DEFAULT 1, created INTEGER)""",
    """CREATE TABLE IF NOT EXISTS user_achievements(
        user_id INTEGER, achievement_id INTEGER, created INTEGER,
        PRIMARY KEY(user_id, achievement_id))""",
]


def init_db():
    for stmt in SCHEMA:
        ex(stmt)
    # миграции для старых баз (данные не теряются)
    _add_col("users", "balance", "REAL DEFAULT 0")
    _add_col("users", "ref_by", "INTEGER")
    _add_col("users", "ref_paid", "INTEGER DEFAULT 0")
    _add_col("orders", "kind", "TEXT DEFAULT 'buy'")
    _add_col("orders", "promo", "TEXT DEFAULT ''")
    _add_col("orders", "base_price", "REAL")
    if not q1("SELECT 1 FROM products"):
        for cat, title, price in SEED:
            ex("INSERT INTO products(cat,title,price) VALUES(?,?,?)", (cat, title, price))


# ===================== ХЕЛПЕРЫ =====================
def now():
    return int(time.time())


def esc(s):
    return html.escape(str(s if s is not None else ""))


def money(v):
    return f"{round(float(v or 0), 2):g} {CURRENCY}"


def is_admin(uid):
    return uid in ADMIN_IDS


def get(key):
    r = q1("SELECT value FROM settings WHERE key=?", (key,))
    return r["value"] if r else DEFAULTS.get(key, "")


def put(key, val):
    ex("INSERT INTO settings(key,value) VALUES(?,?) "
       "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, val))


def fnum(key):
    try:
        return float(str(get(key)).replace(",", ".").strip())
    except ValueError:
        try:
            return float(DEFAULTS.get(key, 0) or 0)
        except ValueError:
            return 0.0


def support_url():
    return "https://t.me/" + get("support").strip().lstrip("@")


def touch_user(u):
    """Возвращает True, если пользователь новый."""
    cur = ex("INSERT OR IGNORE INTO users(id,username,first_name,joined) VALUES(?,?,?,?)",
             (u.id, u.username, u.first_name, now()))
    new = cur.rowcount == 1
    ex("UPDATE users SET username=?, first_name=? WHERE id=?", (u.username, u.first_name, u.id))
    return new


def ikb(*rows):
    """ikb([("Текст","callback")],[("Ссылка","https://...")])"""
    m = types.InlineKeyboardMarkup()
    for row in rows:
        btns = []
        for text, data in row:
            if data.startswith(("http", "tg://")):
                btns.append(types.InlineKeyboardButton(text, url=data))
            else:
                btns.append(types.InlineKeyboardButton(text, callback_data=data))
        m.row(*btns)
    return m


def send_rich(chat, text, kb=None, photo=None):
    """Отправка текста, либо фото с подписью (если подпись длиннее 1024 - фото и текст отдельно)."""
    if photo:
        try:
            if len(text or "") <= 1024:
                return bot.send_photo(chat, photo, caption=text or None, reply_markup=kb)
            bot.send_photo(chat, photo)
        except Exception as e:
            if any(s in str(e) for s in ("403", "blocked", "chat not found", "deactivated")):
                raise
            log.warning("send_photo failed: %s", e)
    return bot.send_message(chat, text or "…", reply_markup=kb, disable_web_page_preview=True)


def show(target, text, kb=None, photo=None):
    """CallbackQuery -> правим сообщение (если нужно фото - удаляем и шлём заново),
    Message -> отправляем новое."""
    if isinstance(target, types.CallbackQuery):
        chat = target.message.chat.id
        msg = target.message
        if not photo and msg.content_type == "text":
            try:
                bot.edit_message_text(text or "…", chat, msg.message_id,
                                      reply_markup=kb, disable_web_page_preview=True)
                return
            except Exception as e:
                if "not modified" in str(e):
                    return
        else:
            try:
                bot.delete_message(chat, msg.message_id)
            except Exception:
                pass
    else:
        chat = target.chat.id
    send_rich(chat, text, kb, photo)


def safe_send(uid, text, kb=None, photo=None):
    try:
        send_rich(uid, text, kb, photo)
        return True
    except Exception as e:
        log.warning("send to %s failed: %s", uid, e)
        return False


def notify_admins(text, kb=None, photo=None):
    for a in ADMIN_IDS:
        try:
            if photo:
                bot.send_photo(a, photo, caption=text, reply_markup=kb)
            else:
                bot.send_message(a, text, reply_markup=kb, disable_web_page_preview=True)
        except Exception as e:
            log.warning("notify admin %s failed: %s", a, e)


def fmt_wallets(t):
    out = []
    for line in t.splitlines():
        line = line.strip()
        if not line:
            continue
        if ":" in line:
            k, v = line.split(":", 1)
            out.append(f"{esc(k)}: <code>{esc(v.strip())}</code>")
        else:
            out.append(esc(line))
    return "\n".join(out)


def user_link(uid):
    u = q1("SELECT username FROM users WHERE id=?", (uid,))
    name = f"@{esc(u['username'])} " if u and u["username"] else ""
    return f'{name}<a href="tg://user?id={uid}">{uid}</a>'


def get_order(oid, uid=None):
    if uid is None:
        return q1("SELECT * FROM orders WHERE id=?", (oid,))
    return q1("SELECT * FROM orders WHERE id=? AND user_id=?", (oid, uid))


def set_status(oid, new, allowed):
    marks = ",".join("?" * len(allowed))
    cur = ex(f"UPDATE orders SET status=?, updated=? WHERE id=? AND status IN ({marks})",
             (new, now(), oid, *allowed))
    return cur.rowcount > 0


def is_topup(o):
    return o["kind"] == "topup"


def order_text(o):
    if is_topup(o):
        return (f"💰 <b>Пополнение баланса #{o['id']}</b>\nСумма: <b>{money(o['price'])}</b>\n"
                f"Статус: {STATUS.get(o['status'], o['status'])}")
    promo = f"\n🎟 Промокод: <code>{esc(o['promo'])}</code> (было {money(o['base_price'])})" \
            if o["promo"] else ""
    return (f"🧾 <b>Заказ #{o['id']}</b>\nТовар: {esc(o['title'])}\n"
            f"Получатель: @{esc(o['target'])}\nСумма: <b>{money(o['price'])}</b>{promo}\n"
            f"Статус: {STATUS.get(o['status'], o['status'])}")


def admin_summary(o):
    if is_topup(o):
        return (f"💰 <b>Пополнение #{o['id']}</b>\nСумма: <b>{money(o['price'])}</b>\n"
                f"Пользователь: {user_link(o['user_id'])}\n"
                f"Способ: {METHOD.get(o['method'], '-')}")
    promo = f"\n🎟 Промокод: {esc(o['promo'])} (было {money(o['base_price'])})" if o["promo"] else ""
    return (f"🧾 <b>Заказ #{o['id']}</b>\n{esc(o['title'])} → @{esc(o['target'])}\n"
            f"Сумма: <b>{money(o['price'])}</b>{promo}\nПокупатель: {user_link(o['user_id'])}\n"
            f"Способ: {METHOD.get(o['method'], '-')}")


def parse_price(text):
    try:
        v = float((text or "").replace(",", ".").strip())
        return v if v > 0 else None
    except ValueError:
        return None


def parse_num(text, zero_ok=False):
    """Число с 2 знаками. None если не число / отрицательное / слишком большое."""
    try:
        v = float((text or "").replace(",", ".").strip())
    except ValueError:
        return None
    if v != v or v < 0 or (v == 0 and not zero_ok) or v > 1_000_000:
        return None
    return round(v, 2)


def norm_url(v):
    v = (v or "").strip()
    if re.fullmatch(r"@[A-Za-z0-9_]{5,32}", v):
        return "https://t.me/" + v[1:]
    if re.fullmatch(r"(https?://|tg://)\S+", v):
        return v
    if re.fullmatch(r"[\w.-]+\.[A-Za-z]{2,}(/\S*)?", v):
        return "https://" + v
    return None


def parse_btn_lines(text):
    """Строки вида «Текст | ссылка» или «Текст - ссылка» -> ряды кнопок. None если формат неверный."""
    rows = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        mt = re.match(r"^(.+?)\s*[|—]\s*(\S+)$", line) or re.match(r"^(.+?)\s+-\s+(\S+)$", line)
        if not mt:
            return None
        url = norm_url(mt.group(2))
        if not url:
            return None
        rows.append([(mt.group(1).strip()[:60], url)])
    return rows


def parse_content(m):
    """Сообщение админа -> (html_текст, file_id_фото|None). Текст ИЛИ фото с подписью."""
    if m.content_type == "photo":
        cap = m.caption or ""
        txt = (m.html_caption if m.caption_entities else html.escape(cap, quote=False)) if cap else ""
        return txt.strip(), m.photo[-1].file_id
    if m.content_type == "text" and m.text:
        txt = m.html_text if m.entities else html.escape(m.text, quote=False)
        return txt.strip(), None
    return None


def aborted(m):
    """Любая команда во время ввода = отмена."""
    if m.content_type == "text" and m.text and m.text.startswith("/"):
        bot.send_message(m.chat.id, "Отменено.")
        if is_admin(m.from_user.id) and m.text.startswith("/admin"):
            admin_home(m)
        else:
            main_menu(m)
        return True
    return False


def ask(chat_id, prompt, handler, *args):
    msg = bot.send_message(chat_id, prompt + "\n\n/cancel - отмена")
    bot.register_next_step_handler(msg, handler, *args)


# ===================== БАЛАНС =====================
def get_balance(uid):
    r = q1("SELECT balance FROM users WHERE id=?", (uid,))
    return round(r["balance"] or 0, 2) if r else 0.0


def add_balance(uid, amount, kind, note=""):
    amount = round(float(amount), 2)
    with _lock:
        ex("UPDATE users SET balance=ROUND(COALESCE(balance,0)+?,2) WHERE id=?", (amount, uid))
        ex("INSERT INTO tx(user_id,amount,kind,note,created) VALUES(?,?,?,?,?)",
           (uid, amount, kind, note, now()))


def spend(uid, amount, kind, note=""):
    """Списывает с баланса. False - если денег не хватает.
    Защита от гонки - в самом условии WHERE у обновления баланса."""
    amount = round(float(amount), 2)
    with _lock:
        cur = ex("UPDATE users SET balance=ROUND(COALESCE(balance,0)-?,2) "
                 "WHERE id=? AND COALESCE(balance,0)>=?", (amount, uid, amount))
        if cur.rowcount == 0:
            return False
        ex("INSERT INTO tx(user_id,amount,kind,note,created) VALUES(?,?,?,?,?)",
           (uid, -amount, kind, note, now()))
    return True


def credit_topup(o):
    """Пополнение оплачено: сначала помечаем заказ выполненным (защита от
    повторного зачисления), затем зачисляем деньги."""
    amount = round(o["price"], 2)
    with _lock:
        cur = ex("UPDATE orders SET status='done', updated=? WHERE id=? "
                 "AND status IN ('new','wait','review')", (now(), o["id"]))
        if cur.rowcount == 0:
            return False
        ex("UPDATE users SET balance=ROUND(COALESCE(balance,0)+?,2) WHERE id=?",
           (amount, o["user_id"]))
        ex("INSERT INTO tx(user_id,amount,kind,note,created) VALUES(?,?,?,?,?)",
           (o["user_id"], amount, "topup", f"Пополнение #{o['id']}", now()))
    return True


def pay_with_balance(o):
    """Оплата заказа с баланса. Возвращает 'ok' | 'low' | 'busy'.
    Сначала списываем деньги (это действие само себя проверяет через WHERE),
    и только если списание прошло - помечаем заказ оплаченным. Так деньги
    никогда не "потеряются" между двумя отдельными запросами к базе."""
    price = round(o["price"], 2)
    if get_balance(o["user_id"]) + 1e-9 < price:
        return "low"
    # если по заказу уже создан счёт CryptoBot - закрываем его, чтобы не оплатили дважды
    if o["invoice_id"] and CRYPTOPAY_TOKEN:
        try:
            if cp_status(o["invoice_id"]) == "paid":
                mark_paid(o["id"])
                return "busy"
            cp_call("deleteInvoice", invoice_id=o["invoice_id"])
        except Exception:
            log.warning("invoice cleanup failed for order %s", o["id"])
    with _lock:
        fresh = get_order(o["id"])
        if not fresh or fresh["status"] not in ("new", "wait"):
            return "busy"
        if not spend(o["user_id"], price, "purchase", f"Заказ #{o['id']}"):
            return "low"
        cur = ex("UPDATE orders SET status='paid', method='bal', updated=? "
                 "WHERE id=? AND user_id=? AND status IN ('new','wait')",
                 (now(), o["id"], o["user_id"]))
        if cur.rowcount == 0:
            # деньги уже списаны, но заказ кто-то успел отменить - возвращаем их
            add_balance(o["user_id"], price, "refund", f"Возврат по заказу #{o['id']}")
            return "busy"
    return "ok"


def make_topup(uid, amount, back=None):
    """Создаёт заказ-пополнение. back - id заказа, к которому вернуться после пополнения."""
    oid = ex("INSERT INTO orders(user_id,product_id,title,price,target,status,created,updated,kind)"
             " VALUES(?,?,?,?,?,?,?,?,'topup')",
             (uid, 0, "Пополнение баланса", round(amount, 2), str(back or ""), "new",
              now(), now())).lastrowid
    return get_order(oid)


# ===================== ПРОМОКОДЫ =====================
def get_promo(code):
    return q1("SELECT * FROM promocodes WHERE code=?", ((code or "").strip().upper(),))


def promo_discount_text(pr):
    return f"-{pr['value']:g}%" if pr["kind"] == "percent" else f"-{money(pr['value'])}"


def apply_promo(oid, uid, raw_code):
    """Возвращает (ok, сообщение)."""
    o = get_order(oid, uid)
    if not o or o["kind"] != "buy" or o["status"] not in ("new", "wait"):
        return False, "Заказ уже обработан."
    if o["promo"]:
        return False, f"К заказу уже применён промокод «{esc(o['promo'])}»."
    pr = get_promo(raw_code)
    if not pr or not pr["active"]:
        return False, "Такой промокод не найден или отключён."
    if pr["max_uses"] and pr["used"] >= pr["max_uses"]:
        return False, "У этого промокода закончился лимит использований."
    if q1("SELECT 1 FROM promo_uses WHERE code=? AND user_id=?", (pr["code"], uid)):
        return False, "Вы уже использовали этот промокод."
    base = o["base_price"] or o["price"]
    if pr["kind"] == "percent":
        new_price = round(max(base * (1 - pr["value"] / 100), 0.01), 2)
    else:
        new_price = round(max(base - pr["value"], 0.01), 2)
    with _lock:
        ex("UPDATE orders SET price=?, promo=?, base_price=? WHERE id=?",
           (new_price, pr["code"], base, oid))
        ex("UPDATE promocodes SET used=used+1 WHERE code=?", (pr["code"],))
        ex("INSERT INTO promo_uses(code,user_id,order_id,created) VALUES(?,?,?,?)",
           (pr["code"], uid, oid, now()))
    return True, f"🎟 Промокод «{esc(pr['code'])}» применён ({promo_discount_text(pr)}). " \
                 f"Новая цена: <b>{money(new_price)}</b>."


# ===================== РЕФЕРАЛЫ =====================
def ref_link(uid):
    return f"https://t.me/{BOT_USERNAME}?start=ref{uid}" if BOT_USERNAME else ""


def ref_reward(uid):
    """Начисляет бонус тому, кто пригласил uid (один раз на приглашённого)."""
    if get("ref_on") != "1":
        return
    r = q1("SELECT ref_by, ref_paid, first_name FROM users WHERE id=?", (uid,))
    bonus = round(fnum("ref_bonus"), 2)
    if not r or not r["ref_by"] or r["ref_paid"] or bonus <= 0:
        return
    if ex("UPDATE users SET ref_paid=1 WHERE id=? AND COALESCE(ref_paid,0)=0", (uid,)).rowcount == 0:
        return
    add_balance(r["ref_by"], bonus, "ref", f"Реферал {uid}")
    safe_send(r["ref_by"],
              f"🎉 По вашей ссылке пришёл {esc(r['first_name'] or 'новый пользователь')}!\n"
              f"Вам начислено <b>{money(bonus)}</b>. Баланс: <b>{money(get_balance(r['ref_by']))}</b>.",
              ikb([("💰 Баланс", "bal")]))


def attach_ref(uid, raw):
    if get("ref_on") != "1" or not raw.isdigit():
        return
    rid = int(raw)
    if rid == uid or not q1("SELECT 1 FROM users WHERE id=?", (rid,)):
        return
    ex("UPDATE users SET ref_by=? WHERE id=? AND ref_by IS NULL", (rid, uid))
    if get("ref_mode") != "paid":
        ref_reward(uid)


# ===================== CRYPTO PAY API =====================
def cp_call(method, **params):
    r = requests.post(f"{CRYPTOPAY_API}/{method}",
                      headers={"Crypto-Pay-API-Token": CRYPTOPAY_TOKEN},
                      json=params, timeout=20)
    data = r.json()
    if not data.get("ok"):
        raise RuntimeError(data)
    return data["result"]


def cp_status(invoice_id):
    res = cp_call("getInvoices", invoice_ids=str(invoice_id))
    items = res.get("items", []) if isinstance(res, dict) else res
    return items[0]["status"] if items else None


def mark_paid(oid, notify=True):
    o = get_order(oid)
    if not o:
        return False
    if is_topup(o):
        if not credit_topup(o):
            return False
        rows = [[("💰 Баланс", "bal"), ("🛒 Каталог", "cat")]]
        back = (o["target"] or "").strip()
        if back.isdigit():
            bo = get_order(int(back), o["user_id"])
            if bo and bo["kind"] != "topup" and bo["status"] in ("new", "wait"):
                rows.insert(0, [(f"🛒 Оплатить заказ #{back}", f"pay:{back}")])
        safe_send(o["user_id"], f"✅ Баланс пополнен на <b>{money(o['price'])}</b>.\n"
                                f"Сейчас на балансе: <b>{money(get_balance(o['user_id']))}</b>.",
                  ikb(*rows))
        if notify:
            notify_admins(admin_summary(o) + "\n💰 Оплачено автоматически (Crypto Pay).")
        if get("ref_mode") == "paid":
            ref_reward(o["user_id"])
        return True
    if not set_status(oid, "paid", ("new", "wait", "review")):
        return False
    o = get_order(oid)
    safe_send(o["user_id"], f"✅ Оплата по заказу #{oid} подтверждена. "
                            f"Выдача обычно занимает несколько минут.")
    if notify:
        notify_admins(admin_summary(o) + "\n💰 Оплачено автоматически (Crypto Pay).",
                      ikb([("🎉 Выдано", f"a:done:{oid}")], [("Открыть", f"a:o:{oid}")]))
    if get("ref_mode") == "paid":
        ref_reward(o["user_id"])
    return True


def check_invoice(o):
    st = cp_status(o["invoice_id"])
    if st == "paid":
        mark_paid(o["id"])
    elif st == "expired" and set_status(o["id"], "cancel", ("wait",)):
        safe_send(o["user_id"], f"⌛ Счёт по заказу #{o['id']} истёк. Оформите заказ заново.")
    return st


def poller():
    while True:
        time.sleep(20)
        try:
            for o in q("SELECT * FROM orders WHERE method='cb' AND status='wait' "
                       "AND invoice_id IS NOT NULL"):
                check_invoice(o)
        except Exception:
            log.exception("poller")


# ===================== СВОИ СТРАНИЦЫ И КНОПКИ =====================
def get_page(pid):
    return q1("SELECT * FROM pages WHERE id=?", (pid,))


def custom_rows(parent):
    """Кнопки, добавленные админом: 'main' - главное меню, 'p<id>' - страница."""
    rows = []
    for b in q("SELECT * FROM buttons WHERE parent=? ORDER BY sort, id", (parent,)):
        rows.append([(b["label"], b["value"] if b["kind"] == "url" else f"pg:{b['value']}")])
    return rows


def page_rows(pid):
    return custom_rows(f"p{pid}") + [[("⬅️ Меню", "menu")]]


def show_page(target, pid):
    pg = get_page(pid)
    if not pg:
        return show(target, "Страница недоступна.", ikb([("⬅️ Меню", "menu")]))
    show(target, pg["text"] or esc(pg["title"]), ikb(*page_rows(pid)), photo=pg["photo"] or None)


def add_button(parent, label, kind, value):
    n = q1("SELECT COALESCE(MAX(sort),0)+1 n FROM buttons WHERE parent=?", (parent,))["n"]
    return ex("INSERT INTO buttons(parent,label,kind,value,sort) VALUES(?,?,?,?,?)",
              (parent, label, kind, str(value), n)).lastrowid


# ----- редактируемые разделы бота (текст + фото; кнопки - через buttons/custom_rows) -----
# "full"   - текст полностью заменяет стандартный (можно с плейсхолдерами {..})
# "footer" - текст добавляется ПОСЛЕ стандартного (динамического) содержимого, по умолчанию пусто
SECTIONS = [
    ("cat", "🛒 Каталог — список категорий", "full"),
    ("cat_stars", "⭐ Категория: Stars", "full"),
    ("cat_premium", "💎 Категория: Premium", "full"),
    ("cat_gifts", "🎁 Категория: Подарки", "full"),
    ("bal", "💰 Баланс", "full"),
    ("ref", "👥 Рефералы (шапка)", "full"),
    ("my", "📦 Мои заказы", "full"),
    ("prod", "🛍 Карточка товара (доп. текст)", "footer"),
    ("ord", "🧾 Карточка заказа (доп. текст)", "footer"),
    ("gate", "🔒 Экран обязательной подписки", "full"),
]
SECTION_LABELS = {k: lbl for k, lbl, _ in SECTIONS}
SECTION_MODE = {k: m for k, _, m in SECTIONS}
SECTION_DEFAULTS = {
    "cat": "Выберите категорию:",
    "cat_stars": CATS["stars"],
    "cat_premium": CATS["premium"],
    "cat_gifts": CATS["gifts"],
    "bal": "💰 <b>Ваш баланс:</b> {balance}\n\nБаланс можно потратить на любой товар в магазине.",
    "ref": "👥 <b>Реферальная программа</b>\n\nПриглашайте друзей и получайте "
           "<b>{bonus}</b> на баланс за каждого — {when}.",
    "my": "📦 <b>Ваши заказы</b>",
    "gate": "🔒 <b>Доступ к боту закрыт</b>\n\nПодпишитесь на наши каналы, чтобы пользоваться магазином, "
            "затем нажмите «Я подписался».",
}


class _SafeDict(dict):
    def __missing__(self, key):
        return "{" + key + "}"


def ph(text, **kw):
    """Подставляет {плейсхолдеры}, не трогая незнакомые фигурные скобки."""
    try:
        return (text or "").format_map(_SafeDict(**kw))
    except Exception:
        return text or ""


def screen_row(key):
    return q1("SELECT * FROM screens WHERE key=?", (key,))


def screen_text(key):
    r = screen_row(key)
    t = r["text"] if r else None
    return t if t not in (None, "") else SECTION_DEFAULTS.get(key, "")


def screen_photo(key):
    r = screen_row(key)
    return r["photo"] if r else ""


def set_screen_text(key, text):
    ex("INSERT INTO screens(key,text) VALUES(?,?) "
       "ON CONFLICT(key) DO UPDATE SET text=excluded.text", (key, text))


def set_screen_photo(key, photo):
    ex("INSERT INTO screens(key,photo) VALUES(?,?) "
       "ON CONFLICT(key) DO UPDATE SET photo=excluded.photo", (key, photo))


def reset_screen_text(key):
    ex("UPDATE screens SET text=NULL WHERE key=?", (key,))


# ===================== ПОКУПАТЕЛЬ =====================
def check_subscribed(channel, uid):
    ch = channel.strip()
    if not ch.startswith("@"):
        ch = "@" + ch.lstrip("@")
    try:
        member = bot.get_chat_member(ch, uid)
        return member.status in ("member", "administrator", "creator")
    except Exception as e:
        log.warning("get_chat_member(%s, %s) failed: %s", ch, uid, e)
        return False


def force_channels():
    return [c.strip() for c in get("force_channels").split(",") if c.strip()]


def gate_ok(uid):
    if is_admin(uid):
        return True
    chans = force_channels()
    return all(check_subscribed(ch, uid) for ch in chans) if chans else True


def show_gate(target, retry=False):
    rows = [[(f"📢 {ch}", "https://t.me/" + ch.lstrip("@"))] for ch in force_channels()]
    rows.append([("✅ Я подписался", "checksub")])
    text = screen_text("gate")
    if retry:
        text += "\n\n⚠️ Похоже, вы подписались не на все каналы. Проверьте и попробуйте снова."
    show(target, text, ikb(*rows), photo=screen_photo("gate") or None)


def main_menu(target):
    if not gate_ok(target.from_user.id):
        return show_gate(target)
    row3 = [("💬 Поддержка", support_url())]
    if get("ref_on") == "1":
        row3.insert(0, ("👥 Рефералы", "ref"))
    rows4 = [[("📋 Задания", "tasks"), ("🏆 Достижения", "ach")]]
    if get("reviews_public") == "1":
        rows4.append([("⭐ Отзывы", "revs")])
    show(target, get("welcome"), ikb(
        [("🛒 Каталог", "cat")],
        [("📦 Мои заказы", "my"), ("💰 Баланс", "bal")],
        row3,
        *rows4,
        *custom_rows("main"),
    ), photo=get("welcome_photo") or None)


@bot.message_handler(commands=["start"])
def cmd_start(m):
    new = touch_user(m.from_user)
    parts = (m.text or "").split(maxsplit=1)
    if new and len(parts) == 2 and parts[1].startswith("ref"):
        try:
            attach_ref(m.from_user.id, parts[1][3:])
        except Exception:
            log.exception("attach_ref")
    if new and get("notify_new") == "1":
        notify_admins(f"🆕 Новый пользователь: {user_link(m.from_user.id)}")
    main_menu(m)


@bot.message_handler(commands=["cancel"])
def cmd_cancel(m):
    main_menu(m)


@bot.message_handler(commands=["admin"])
def cmd_admin(m):
    if is_admin(m.from_user.id):
        admin_home(m)


def pay_methods_kb(oid):
    o = get_order(oid)
    rows = []
    if o and not is_topup(o):
        rows.append([(f"💰 С баланса ({money(get_balance(o['user_id']))})", f"pm:{oid}:bal")])
    if CRYPTOPAY_TOKEN:
        rows.append([("🤖 CryptoBot (автоматически)", f"pm:{oid}:cb")])
    rows.append([("🧾 Крипто-чек", f"pm:{oid}:check")])
    rows.append([("👛 Перевод на кошелёк", f"pm:{oid}:wallet")])
    if o and not is_topup(o) and not o["promo"]:
        rows.append([("🎟 Есть промокод?", f"promo:{oid}")])
    rows.append([("🚫 Отмена", f"cancel:{oid}")])
    return ikb(*rows)


def got_target(m, pid):
    if aborted(m):
        return
    t = (m.text or "").strip().replace("https://t.me/", "").lstrip("@")
    pr = q1("SELECT * FROM products WHERE id=? AND active=1", (pid,))
    if not pr:
        bot.send_message(m.chat.id, "Товар недоступен.")
        return
    if not re.fullmatch(r"[A-Za-z0-9_]{5,32}", t):
        ask(m.chat.id, "Некорректный username. Пришлите @username ещё раз.", got_target, pid)
        return
    oid = ex("INSERT INTO orders(user_id,product_id,title,price,target,status,created,updated,kind,base_price)"
             " VALUES(?,?,?,?,?,?,?,?,'buy',?)",
             (m.from_user.id, pid, pr["title"], pr["price"], t, "new", now(), now(), pr["price"])).lastrowid
    o = get_order(oid)
    bot.send_message(m.chat.id, order_text(o) + "\n\nВыберите способ оплаты:",
                     reply_markup=pay_methods_kb(oid))


def got_topup(m):
    if aborted(m):
        return
    lo = max(fnum("min_topup"), 0.01)
    v = parse_num(m.text)
    if v is None or v < lo or v > MAX_TOPUP:
        return ask(m.chat.id, f"Введите сумму числом от {money(lo)} до {money(MAX_TOPUP)}, "
                              f"например {money(max(lo, 5))}.", got_topup)
    o = make_topup(m.from_user.id, v)
    bot.send_message(m.chat.id, order_text(o) + "\n\nВыберите способ оплаты:",
                     reply_markup=pay_methods_kb(o["id"]))


def got_promo_code(m, oid):
    if aborted(m):
        return
    if not m.text:
        return ask(m.chat.id, "Пришлите промокод текстом.", got_promo_code, oid)
    ok, msg = apply_promo(oid, m.from_user.id, m.text)
    o = get_order(oid, m.from_user.id)
    if not ok:
        bot.send_message(m.chat.id, "⚠️ " + msg)
        if o and o["status"] in ("new", "wait"):
            bot.send_message(m.chat.id, order_text(o) + "\n\nВыберите способ оплаты:",
                             reply_markup=pay_methods_kb(oid))
        return
    bot.send_message(m.chat.id, msg)
    bot.send_message(m.chat.id, order_text(o) + "\n\nВыберите способ оплаты:",
                     reply_markup=pay_methods_kb(oid))


def got_review_text(m, oid, stars):
    if aborted(m):
        return
    o = get_order(oid, m.from_user.id)
    if not o or o["status"] != "done":
        return
    if q1("SELECT 1 FROM reviews WHERE order_id=?", (oid,)):
        return bot.send_message(m.chat.id, "Вы уже оценили этот заказ, спасибо!")
    txt = "" if (m.text or "").strip() in ("-", "—") else (m.text or "").strip()[:300]
    uname = m.from_user.username or m.from_user.first_name or "Аноним"
    ex("INSERT INTO reviews(order_id,user_id,username,stars,text,created) VALUES(?,?,?,?,?,?)",
       (oid, m.from_user.id, uname, stars, txt, now()))
    bot.send_message(m.chat.id, "Спасибо за отзыв! ⭐", reply_markup=ikb([("⬅️ Меню", "menu")]))
    notify_admins(f"⭐ Новый отзыв к заказу #{oid}\n{'⭐' * stars} от {esc(uname)}" +
                  (f"\n{esc(txt)}" if txt else ""))


def got_check(m, oid):
    if aborted(m):
        return
    o = get_order(oid, m.from_user.id)
    if not o or o["status"] != "wait":
        return
    txt = (m.text or "").strip()
    if not re.search(r"t\.me/\S+\?start=\S+", txt):
        ask(m.chat.id, "Это не похоже на ссылку чека. Пришлите ссылку вида "
                       "https://t.me/send?start=...", got_check, oid)
        return
    ex("UPDATE orders SET proof=?, status='review', updated=? WHERE id=?", (txt[:500], now(), oid))
    notify_admins(admin_summary(o) + f"\n🔗 Чек: {esc(txt)}\n\n"
                  "Активируйте чек в @CryptoBot, затем нажмите «Подтвердить».",
                  ikb([("✅ Подтвердить", f"a:ok:{oid}"), ("❌ Отклонить", f"a:no:{oid}")]))
    bot.send_message(m.chat.id, "Чек получен и отправлен на проверку. Обычно до 15 минут.",
                     reply_markup=ikb([("📦 Мои заказы", "my")]))


def got_proof(m, oid):
    if aborted(m):
        return
    o = get_order(oid, m.from_user.id)
    if not o or o["status"] != "wait":
        return
    photo = None
    if m.content_type == "photo":
        photo = m.photo[-1].file_id
        proof = "photo:" + photo
    elif m.text:
        proof = m.text.strip()[:500]
    else:
        ask(m.chat.id, "Пришлите TXID текстом или скриншот перевода.", got_proof, oid)
        return
    ex("UPDATE orders SET proof=?, status='review', updated=? WHERE id=?", (proof, now(), oid))
    extra = "\n🖼 Скриншот во вложении" if photo else f"\n🔗 TXID/сообщение: <code>{esc(proof)}</code>"
    notify_admins(admin_summary(o) + extra,
                  ikb([("✅ Подтвердить", f"a:ok:{oid}"), ("❌ Отклонить", f"a:no:{oid}")]),
                  photo=photo)
    bot.send_message(m.chat.id, "Спасибо! Платёж на проверке, обычно до 15 минут.",
                     reply_markup=ikb([("📦 Мои заказы", "my")]))


def user_cb(c, p):
    a, uid = p[0], c.from_user.id
    if a == "menu":
        main_menu(c)

    elif a == "pg":
        show_page(c, int(p[1]))

    elif a == "cat" and len(p) == 1:
        rows = [[(name, f"cat:{k}")] for k, name in CATS.items()] + custom_rows("cat") + \
               [[("⬅️ Меню", "menu")]]
        show(c, screen_text("cat"), ikb(*rows), photo=screen_photo("cat") or None)

    elif a == "cat":
        rows = q("SELECT * FROM products WHERE cat=? AND active=1 ORDER BY id", (p[1],))
        btns = [[(f"{r['title']} - {money(r['price'])}", f"prod:{r['id']}")] for r in rows]
        key = f"cat_{p[1]}"
        text = f"<b>{screen_text(key) or CATS.get(p[1], 'Каталог')}</b>" + \
               ("" if rows else "\n\nПока пусто.")
        show(c, text, ikb(*btns, *custom_rows(key), [("⬅️ Назад", "cat")]),
             photo=screen_photo(key) or None)

    elif a == "prod":
        pr = q1("SELECT * FROM products WHERE id=? AND active=1", (int(p[1]),))
        if not pr:
            return show(c, "Товар недоступен.", ikb([("⬅️ Каталог", "cat")]))
        extra = screen_text("prod")
        text = (f"<b>{esc(pr['title'])}</b>\nЦена: <b>{money(pr['price'])}</b>\n\n"
                "Выдача вручную после подтверждения оплаты." + (f"\n\n{extra}" if extra else ""))
        rows = [[("🛒 Купить", f"buy:{pr['id']}")]] + custom_rows("prod") + \
               [[("⬅️ Назад", f"cat:{pr['cat']}")]]
        show(c, text, ikb(*rows), photo=screen_photo("prod") or None)

    elif a == "buy":
        ask(c.message.chat.id, "Пришлите @username аккаунта Telegram, "
                               "на который нужно доставить товар.", got_target, int(p[1]))

    # ---------- баланс ----------
    elif a == "bal":
        text = ph(screen_text("bal"), balance=money(get_balance(uid)))
        rows = [[("➕ Пополнить", "topup"), ("📜 История", "txh")]] + custom_rows("bal") + \
               [[("⬅️ Меню", "menu")]]
        show(c, text, ikb(*rows), photo=screen_photo("bal") or None)

    elif a == "topup":
        lo = max(fnum("min_topup"), 0.01)
        ask(c.message.chat.id, f"Введите сумму пополнения в {CURRENCY} (минимум {money(lo)}):", got_topup)

    elif a == "txh":
        rows = q("SELECT * FROM tx WHERE user_id=? ORDER BY id DESC LIMIT 10", (uid,))
        lines = [f"{'+' if r['amount'] > 0 else '−'}{abs(r['amount']):g} {CURRENCY} · "
                 f"{TXK.get(r['kind'], r['kind'])} · "
                 f"{time.strftime('%d.%m %H:%M', time.localtime(r['created']))}" for r in rows]
        show(c, "📜 <b>История операций</b>\n\n" + ("\n".join(lines) if lines else "Операций пока нет.") +
             f"\n\nБаланс: <b>{money(get_balance(uid))}</b>",
             ikb([("➕ Пополнить", "topup")], [("⬅️ Баланс", "bal")]))

    elif a == "tu":  # быстрое пополнение на недостающую сумму
        o = get_order(int(p[1]), uid)
        if not o or is_topup(o) or o["status"] not in ("new", "wait"):
            return
        need = round(o["price"] - get_balance(uid), 2)
        t = make_topup(uid, max(need, fnum("min_topup"), 0.01), o["id"])
        show(c, order_text(t) + "\n\nВыберите способ оплаты:", pay_methods_kb(t["id"]))

    elif a == "pay":  # вернуться к выбору способа оплаты
        o = get_order(int(p[1]), uid)
        if not o or is_topup(o) or o["status"] not in ("new", "wait"):
            return show(c, "Заказ уже обработан.", ikb([("⬅️ Меню", "menu")]))
        show(c, order_text(o) + "\n\nВыберите способ оплаты:", pay_methods_kb(o["id"]))

    # ---------- рефералы ----------
    elif a == "ref":
        if get("ref_on") != "1":
            return show(c, "Реферальная программа сейчас отключена.", ikb([("⬅️ Меню", "menu")]))
        bonus, mode = fnum("ref_bonus"), get("ref_mode")
        st = q1("SELECT COUNT(*) n, COALESCE(SUM(ref_paid),0) r FROM users WHERE ref_by=?", (uid,))
        earned = q1("SELECT COALESCE(SUM(amount),0) s FROM tx WHERE user_id=? AND kind='ref'", (uid,))["s"]
        when = "после его первой оплаты" if mode == "paid" else "сразу, как только он зайдёт в бота"
        link = ref_link(uid)
        text = ph(screen_text("ref"), bonus=money(bonus), when=when) + "\n\n"
        text += (f"🔗 Ваша ссылка:\n<code>{link}</code>\n\n" if link else
                 "Ссылка временно недоступна.\n\n")
        text += (f"Приглашено: <b>{st['n']}</b>\nБонусов получено: <b>{int(st['r'])}</b>\n"
                 f"Заработано: <b>{money(earned)}</b>")
        rows = []
        if link:
            share = ("https://t.me/share/url?url=" + quote(link, safe="") +
                     "&text=" + quote("Магазин Telegram Stars, Premium и подарков 🎁"))
            rows.append([("📤 Поделиться ссылкой", share)])
        rows += custom_rows("ref")
        rows.append([("⬅️ Меню", "menu")])
        show(c, text, ikb(*rows), photo=screen_photo("ref") or None)

    # ---------- оплата ----------
    elif a == "pm":
        oid, method = int(p[1]), p[2]
        o = get_order(oid, uid)
        if not o or o["status"] not in ("new", "wait"):
            return
        title = "Пополнение" if is_topup(o) else "Заказ"
        if method == "bal":
            if is_topup(o):
                return
            bal = get_balance(uid)
            if bal + 1e-9 < o["price"]:
                need = round(o["price"] - bal, 2)
                return show(c, order_text(o) + f"\n\n⚠️ Недостаточно средств. На балансе: {money(bal)}, "
                                               f"не хватает: {money(need)}.",
                            ikb([(f"➕ Пополнить на {money(max(need, fnum('min_topup')))}", f"tu:{oid}")],
                                [("⬅️ Способы оплаты", f"pay:{oid}")]))
            res = pay_with_balance(o)
            if res != "ok":
                return bot.send_message(c.message.chat.id, "Не удалось оплатить с баланса: "
                                                           "заказ уже обработан или недостаточно средств.")
            o2 = get_order(oid)
            notify_admins(admin_summary(o2) + "\n💰 Оплачено с баланса.",
                          ikb([("🎉 Выдано", f"a:done:{oid}")], [("Открыть", f"a:o:{oid}")]))
            show(c, order_text(o2) + f"\n\n✅ Оплачено с баланса. Остаток: {money(get_balance(uid))}.\n"
                                     "Выдача обычно занимает несколько минут.",
                 ikb([("📦 Мои заказы", "my")], [("⬅️ Меню", "menu")]))
        elif method == "cb":
            if not CRYPTOPAY_TOKEN:
                return
            try:
                inv = cp_call("createInvoice", currency_type="fiat", fiat=CURRENCY,
                              amount=f"{o['price']:.2f}", accepted_assets=CRYPTOPAY_ASSETS,
                              description=f"{title} #{oid}", payload=str(oid), expires_in=3600)
            except Exception:
                log.exception("createInvoice")
                return show(c, "⚠️ Не удалось создать счёт. Выберите другой способ.",
                            pay_methods_kb(oid))
            ex("UPDATE orders SET method='cb', status='wait', invoice_id=?, updated=? WHERE id=?",
               (inv["invoice_id"], now(), oid))
            url = inv.get("bot_invoice_url") or inv.get("pay_url")
            show(c, order_text(get_order(oid)) +
                 "\n\nОплатите счёт по кнопке - бот сам увидит платёж (до минуты).",
                 ikb([("💳 Оплатить", url)], [("🔄 Проверить оплату", f"chk:{oid}")],
                     [("🚫 Отмена", f"cancel:{oid}")]))
        elif method == "check":
            ex("UPDATE orders SET method='check', status='wait', updated=? WHERE id=?", (now(), oid))
            ask(c.message.chat.id,
                f"Создайте чек в @CryptoBot (Кошелёк → Чеки) на сумму {money(o['price'])} "
                f"(в USDT или эквивалент) и пришлите ссылку на чек сюда.",
                got_check, oid)
        elif method == "wallet":
            ex("UPDATE orders SET method='wallet', status='wait', updated=? WHERE id=?", (now(), oid))
            show(c, order_text(get_order(oid)) + "\n\n<b>Реквизиты:</b>\n" +
                 fmt_wallets(get("wallets")) +
                 f"\n\nСумма: {money(o['price'])} в выбранной монете по текущему курсу. "
                 "Комиссию сети платит отправитель. После перевода нажмите «Я оплатил».",
                 ikb([("✅ Я оплатил", f"paid:{oid}")], [("🚫 Отмена", f"cancel:{oid}")]))

    elif a == "paid":
        o = get_order(int(p[1]), uid)
        if o and o["status"] == "wait" and o["method"] == "wallet":
            ask(c.message.chat.id, "Пришлите TXID перевода или скриншот.", got_proof, o["id"])

    elif a == "chk":
        o = get_order(int(p[1]), uid)
        if not o or o["method"] != "cb" or o["status"] != "wait":
            return bot.send_message(c.message.chat.id, "Заказ уже обработан.")
        try:
            st = check_invoice(o)
        except Exception:
            log.exception("check invoice")
            return bot.send_message(c.message.chat.id, "Не удалось проверить, попробуйте позже.")
        if st not in ("paid", "expired"):
            bot.send_message(c.message.chat.id, "Оплата пока не найдена. Подождите минуту и проверьте снова.")

    elif a == "cancel":
        oid = int(p[1])
        o = get_order(oid, uid)
        if o and set_status(oid, "cancel", ("new", "wait")):
            if o["invoice_id"] and CRYPTOPAY_TOKEN:
                try:
                    cp_call("deleteInvoice", invoice_id=o["invoice_id"])
                except Exception:
                    pass
        show(c, "Заказ отменён.", ikb([("🛒 Каталог", "cat"), ("⬅️ Меню", "menu")]))

    elif a == "my":
        rows = q("SELECT * FROM orders WHERE user_id=? AND status!='new' ORDER BY id DESC LIMIT 10", (uid,))
        btns = [[(f"#{r['id']} {r['title']} · {STATUS[r['status']].split()[0]}", f"ord:{r['id']}")]
                for r in rows]
        text = screen_text("my") + ("" if rows else "\n\nЗаказов пока нет.")
        show(c, text, ikb(*btns, *custom_rows("my"), [("⬅️ Меню", "menu")]),
             photo=screen_photo("my") or None)

    elif a == "ord":
        o = get_order(int(p[1]), uid)
        if not o:
            return
        rows = []
        if o["status"] == "wait":
            if o["method"] == "cb":
                rows.append([("🔄 Проверить оплату", f"chk:{o['id']}")])
            elif o["method"] == "wallet":
                rows.append([("✅ Я оплатил", f"paid:{o['id']}")])
            elif o["method"] == "check":
                rows.append([("🧾 Отправить чек", f"pm:{o['id']}:check")])
            rows.append([("🚫 Отмена", f"cancel:{o['id']}")])
        rows += custom_rows("ord")
        rows.append([("⬅️ К заказам", "my")])
        extra = screen_text("ord")
        show(c, order_text(o) + (f"\n\n{extra}" if extra else ""), ikb(*rows))

    # ---------- промокод ----------
    elif a == "promo":
        oid = int(p[1])
        o = get_order(oid, uid)
        if not o or o["status"] not in ("new", "wait"):
            return bot.send_message(c.message.chat.id, "Заказ уже обработан.")
        ask(c.message.chat.id, "Пришлите промокод текстом.", got_promo_code, oid)

    # ---------- обязательная подписка ----------
    elif a == "checksub":
        if gate_ok(uid):
            main_menu(c)
        else:
            show_gate(c, retry=True)

    # ---------- отзывы ----------
    elif a == "revs":
        if get("reviews_public") != "1":
            return show(c, "Раздел отзывов сейчас недоступен.", ikb([("⬅️ Меню", "menu")]))
        rows = q("SELECT * FROM reviews ORDER BY id DESC LIMIT 10")
        avg = q1("SELECT AVG(stars) a, COUNT(*) n FROM reviews")
        lines = [f"{'⭐' * r['stars']} — {esc(r['username'])}" +
                 (f"\n{esc(r['text'])}" if r["text"] else "") for r in rows]
        head = f"⭐ <b>Отзывы покупателей</b>\n\nСредняя оценка: {round(avg['a'] or 0, 1)} ({avg['n']})\n\n"
        show(c, head + ("\n\n".join(lines) if lines else "Отзывов пока нет."),
             ikb([("⬅️ Меню", "menu")]))

    elif a == "rv":
        oid, stars = int(p[1]), int(p[2])
        o = get_order(oid, uid)
        if not o or o["status"] != "done":
            return
        if q1("SELECT 1 FROM reviews WHERE order_id=?", (oid,)):
            return bot.send_message(c.message.chat.id, "Вы уже оценили этот заказ, спасибо!")
        ask(c.message.chat.id, f"Оценка: {'⭐' * stars}\n\nНапишите короткий отзыв текстом, "
                               "или «-», чтобы отправить без комментария.", got_review_text, oid, stars)

    # ---------- задания ----------
    elif a == "tasks":
        rows = q("SELECT * FROM tasks WHERE active=1 ORDER BY id")
        done_ids = {r["task_id"] for r in q("SELECT task_id FROM task_done WHERE user_id=?", (uid,))}
        btns = [[(f"{'✅' if t['id'] in done_ids else '🔓'} {t['title']} (+{money(t['reward'])})",
                  f"tsk:{t['id']}")] for t in rows]
        show(c, "📋 <b>Задания</b>\n\nВыполняйте задания и получайте бонусы на баланс." +
             ("" if rows else "\n\nЗаданий пока нет."), ikb(*btns, [("⬅️ Меню", "menu")]))

    elif a == "tsk":
        t = q1("SELECT * FROM tasks WHERE id=? AND active=1", (int(p[1]),))
        if not t:
            return bot.send_message(c.message.chat.id, "Задание недоступно.")
        if q1("SELECT 1 FROM task_done WHERE user_id=? AND task_id=?", (uid, t["id"])):
            return show(c, "Это задание уже выполнено. Спасибо!", ikb([("⬅️ Задания", "tasks")]))
        if check_subscribed(t["channel"], uid):
            ex("INSERT OR IGNORE INTO task_done(user_id,task_id,created) VALUES(?,?,?)",
               (uid, t["id"], now()))
            add_balance(uid, t["reward"], "admin", f"Задание «{t['title']}»")
            show(c, f"🎉 Задание выполнено! Начислено <b>{money(t['reward'])}</b>.\n"
                    f"Баланс: <b>{money(get_balance(uid))}</b>.",
                 ikb([("📋 Другие задания", "tasks")], [("⬅️ Меню", "menu")]))
        else:
            ch = t["channel"].lstrip("@")
            show(c, f"🔒 <b>{esc(t['title'])}</b>\n\nПодпишитесь на канал и нажмите «Проверить».",
                 ikb([("📢 Открыть канал", "https://t.me/" + ch)],
                     [("🔄 Проверить", f"tsk:{t['id']}")], [("⬅️ Задания", "tasks")]))

    # ---------- достижения ----------
    elif a == "ach":
        got = q("SELECT a.* FROM achievements a JOIN user_achievements u "
                "ON u.achievement_id=a.id WHERE u.user_id=? ORDER BY u.created DESC", (uid,))
        total = q1("SELECT COUNT(*) n FROM achievements WHERE active=1")["n"]
        lines = [f"{r['emoji']} <b>{esc(r['title'])}</b>" + (f"\n{esc(r['text'])}" if r["text"] else "")
                 for r in got]
        show(c, f"🏆 <b>Достижения</b>\n\nПолучено: {len(got)} из {total}\n\n" +
             ("\n\n".join(lines) if lines else "Пока нет полученных достижений."),
             ikb([("⬅️ Меню", "menu")]))


# ===================== АДМИНКА =====================
DRAFT = {}  # черновики многошаговых действий админа (добавление кнопки)
BC = {}     # черновик рассылки


def admin_home(target):
    n_rev = q1("SELECT COUNT(*) n FROM orders WHERE status='review'")["n"]
    n_paid = q1("SELECT COUNT(*) n FROM orders WHERE status='paid'")["n"]
    show(target, "🛠 <b>Админ-панель</b>", ikb(
        [(f"🔎 На проверке ({n_rev})", "a:list:review")],
        [(f"📦 К выдаче ({n_paid})", "a:list:paid")],
        [("🧾 Последние заказы", "a:list:all")],
        [("🛍 Товары", "a:prods"), ("📊 Статистика", "a:stats")],
        [("👤 Пользователи", "a:us"), ("🤝 Рефералы", "a:ref")],
        [("🎟 Промокоды", "a:promos"), ("⭐ Отзывы", "a:revs")],
        [("📋 Задания", "a:tasks"), ("🏆 Достижения", "a:achs")],
        [("🎨 Контент и кнопки", "a:cm")],
        [("📢 Рассылка", "a:bc"), ("⚙️ Настройки", "a:set")],
    ))


def admin_list(c, kind):
    if kind == "all":
        rows = q("SELECT * FROM orders WHERE status!='new' ORDER BY id DESC LIMIT 20")
    else:
        rows = q("SELECT * FROM orders WHERE status=? ORDER BY id DESC LIMIT 20", (kind,))
    btns = [[(f"#{r['id']} {r['title']} · {money(r['price'])} · {STATUS[r['status']].split()[0]}",
              f"a:o:{r['id']}")] for r in rows]
    show(c, "Заказы:" + ("" if rows else "\n\nПусто."), ikb(*btns, [("⬅️ Админка", "a:home")]))


def admin_order(c, oid):
    o = get_order(oid)
    if not o:
        return show(c, "Заказ не найден.", ikb([("⬅️ Админка", "a:home")]))
    text = admin_summary(o) + f"\nСтатус: {STATUS[o['status']]}"
    if o["proof"] and not o["proof"].startswith("photo:"):
        text += f"\n🔗 <code>{esc(o['proof'])}</code>"
    rows = []
    if o["status"] in ("review", "wait"):
        rows.append([("✅ Подтвердить оплату", f"a:ok:{oid}"), ("❌ Отклонить", f"a:no:{oid}")])
    elif o["status"] == "paid":
        rows.append([("🎉 Выдано", f"a:done:{oid}"), ("❌ Отклонить", f"a:no:{oid}")])
    rows.append([("⬅️ Админка", "a:home")])
    show(c, text, ikb(*rows))
    if o["proof"] and o["proof"].startswith("photo:"):
        try:
            bot.send_photo(c.message.chat.id, o["proof"][6:], caption=f"Скриншот к заказу #{oid}")
        except Exception:
            pass


def admin_action(c, a, oid):
    o = get_order(oid)
    if not o:
        return
    lbl = "Пополнение" if is_topup(o) else "Заказ"
    if a == "ok":
        mark_paid(oid, notify=False)
    elif a == "done":
        if not is_topup(o) and set_status(oid, "done", ("paid",)):
            safe_send(o["user_id"], f"🎉 Заказ #{oid} выполнен: {esc(o['title'])} → @{esc(o['target'])}. "
                                    "Спасибо за покупку!")
            safe_send(o["user_id"], "Поставьте оценку покупке:",
                      ikb([("⭐", f"rv:{oid}:1"), ("⭐⭐", f"rv:{oid}:2"), ("⭐⭐⭐", f"rv:{oid}:3")],
                          [("⭐⭐⭐⭐", f"rv:{oid}:4"), ("⭐⭐⭐⭐⭐", f"rv:{oid}:5")]))
    elif a == "no":
        if set_status(oid, "rejected", ("review", "wait", "paid")):
            extra = ""
            if o["method"] == "bal" and not is_topup(o):  # заказ был оплачен с баланса - возвращаем деньги
                add_balance(o["user_id"], o["price"], "refund", f"Возврат по заказу #{oid}")
                extra = f" Средства ({money(o['price'])}) возвращены на баланс."
            safe_send(o["user_id"], f"❌ {lbl} #{oid} отклонён.{extra} Если это ошибка - напишите в поддержку.",
                      ikb([("💬 Поддержка", support_url())]))
    admin_order(c, oid)


# ---------- товары ----------
def admin_prods(c):
    rows = q("SELECT * FROM products ORDER BY cat, id")
    btns = [[(f"{'✅' if r['active'] else '🚫'} {r['title']} - {money(r['price'])}", f"a:p:{r['id']}")]
            for r in rows]
    show(c, "🛍 <b>Товары</b>", ikb([("➕ Добавить", "a:padd")], *btns, [("⬅️ Админка", "a:home")]))


def admin_prod(c, pid):
    r = q1("SELECT * FROM products WHERE id=?", (pid,))
    if not r:
        return admin_prods(c)
    show(c, f"<b>{esc(r['title'])}</b>\nКатегория: {CATS.get(r['cat'], r['cat'])}\n"
            f"Цена: {money(r['price'])}\nВ продаже: {'да' if r['active'] else 'нет'}",
         ikb([("✏️ Название", f"a:pti:{pid}"), ("💲 Цена", f"a:ppr:{pid}")],
             [("🔁 Вкл/выкл", f"a:ptg:{pid}")], [("🗑 Удалить", f"a:pdel:{pid}")],
             [("⬅️ Товары", "a:prods")]))


def st_title(m, pid):
    if aborted(m):
        return
    if not m.text:
        return ask(m.chat.id, "Пришлите название текстом.", st_title, pid)
    ex("UPDATE products SET title=? WHERE id=?", (m.text.strip()[:80], pid))
    bot.send_message(m.chat.id, "Готово.", reply_markup=ikb([("⬅️ К товару", f"a:p:{pid}")]))


def st_price(m, pid):
    if aborted(m):
        return
    v = parse_price(m.text)
    if v is None:
        return ask(m.chat.id, "Нужно число больше нуля, например 4.5", st_price, pid)
    ex("UPDATE products SET price=? WHERE id=?", (v, pid))
    bot.send_message(m.chat.id, "Готово.", reply_markup=ikb([("⬅️ К товару", f"a:p:{pid}")]))


def st_add_title(m, cat):
    if aborted(m):
        return
    if not m.text:
        return ask(m.chat.id, "Пришлите название текстом.", st_add_title, cat)
    ask(m.chat.id, f"Цена в {CURRENCY}:", st_add_price, cat, m.text.strip()[:80])


def st_add_price(m, cat, title):
    if aborted(m):
        return
    v = parse_price(m.text)
    if v is None:
        return ask(m.chat.id, "Нужно число больше нуля, например 4.5", st_add_price, cat, title)
    pid = ex("INSERT INTO products(cat,title,price) VALUES(?,?,?)", (cat, title, v)).lastrowid
    bot.send_message(m.chat.id, "Товар добавлен.", reply_markup=ikb([("⬅️ К товару", f"a:p:{pid}")]))


# ---------- статистика ----------
def admin_stats(c):
    users = q1("SELECT COUNT(*) n FROM users")["n"]
    tot = q1("SELECT COUNT(*) n, COALESCE(SUM(price),0) s FROM orders "
             "WHERE kind='buy' AND status IN ('paid','done')")
    wk = q1("SELECT COUNT(*) n, COALESCE(SUM(price),0) s FROM orders "
            "WHERE kind='buy' AND status IN ('paid','done') AND created>?", (now() - 7 * 86400,))
    top = q1("SELECT COUNT(*) n, COALESCE(SUM(price),0) s FROM orders WHERE kind='topup' AND status='done'")
    bal = q1("SELECT COALESCE(SUM(balance),0) s FROM users")["s"]
    refs = q1("SELECT COUNT(*) n FROM users WHERE ref_by IS NOT NULL")["n"]
    by = "\n".join(f"{STATUS[r['status']]}: {r['n']}" for r in
                   q("SELECT status, COUNT(*) n FROM orders WHERE status!='new' GROUP BY status"))
    show(c, f"📊 <b>Статистика</b>\n\nПользователей: {users} (по рефералкам: {refs})\n"
            f"Оплаченных заказов: {tot['n']} на {money(round(tot['s'], 2))}\n"
            f"За 7 дней: {wk['n']} на {money(round(wk['s'], 2))}\n"
            f"Пополнений баланса: {top['n']} на {money(round(top['s'], 2))}\n"
            f"Сумма на балансах клиентов: {money(round(bal, 2))}\n\n{by or 'Заказов пока нет.'}",
         ikb([("⬅️ Админка", "a:home")]))


# ---------- рассылка (текст / фото + кнопки) ----------
def st_bc(m):
    if aborted(m):
        return
    cnt = parse_content(m)
    if not cnt or not (cnt[0] or cnt[1]):
        return ask(m.chat.id, "Пришлите текст рассылки или фото с подписью.", st_bc)
    BC[m.from_user.id] = {"text": cnt[0], "photo": cnt[1], "btns": []}
    ask(m.chat.id, "Нужны кнопки под сообщением? Пришлите по одной в строке:\n"
                   "Текст кнопки | https://ссылка\n\nИли отправьте «-», чтобы без кнопок.", st_bc_btns)


def st_bc_btns(m):
    if aborted(m):
        return
    d = BC.get(m.from_user.id)
    if not d:
        return
    if not m.text:
        return ask(m.chat.id, "Пришлите кнопки текстом или «-».", st_bc_btns)
    t = m.text.strip()
    rows = [] if t in ("-", "—") else parse_btn_lines(t)
    if rows is None:
        return ask(m.chat.id, "Не понял формат. Каждая строка: Текст | https://ссылка "
                              "(или @username). Или «-» без кнопок.", st_bc_btns)
    d["btns"] = rows
    bot.send_message(m.chat.id, "👁 Предпросмотр рассылки:")
    send_rich(m.chat.id, d["text"], ikb(*rows) if rows else None, d["photo"])
    bot.send_message(m.chat.id, "Отправить всем пользователям?",
                     reply_markup=ikb([("✅ Отправить", "a:bcgo")], [("⬅️ Отмена", "a:home")]))


def do_broadcast(admin_id, d):
    ok = bad = 0
    kb = ikb(*d["btns"]) if d["btns"] else None
    for u in q("SELECT id FROM users"):
        if safe_send(u["id"], d["text"], kb, d["photo"]):
            ok += 1
        else:
            bad += 1
        time.sleep(0.05)
    safe_send(admin_id, f"📢 Рассылка завершена. Доставлено: {ok}, не доставлено: {bad}.")


# ---------- настройки ----------
def admin_settings(c):
    chans = get("force_channels") or "не заданы"
    show(c, "⚙️ <b>Настройки</b>\n\n<b>Кошельки:</b>\n" + fmt_wallets(get("wallets")) +
         f"\n\n<b>Поддержка:</b> {esc(get('support'))}\n"
         f"<b>Мин. пополнение баланса:</b> {money(fnum('min_topup'))}\n"
         f"<b>Уведомление о новых пользователях:</b> {'вкл' if get('notify_new') == '1' else 'выкл'}\n"
         f"<b>Обязательные каналы:</b> {esc(chans)}\n\n"
         f"<b>Приветствие:</b>\n{get('welcome')}",
         ikb([("👛 Кошельки", "a:sv:wallets")], [("💬 Поддержка", "a:sv:support")],
             [("💵 Мин. пополнение", "a:sv:min_topup")],
             [("🆕 Уведомление о новых: вкл/выкл", "a:ntg")],
             [("🔒 Обязательные каналы", "a:sv:force_channels")],
             [("👋 Приветствие (текст+фото)", "a:wel")], [("⬅️ Админка", "a:home")]))


SETTING_HINT = {
    "wallets": "Каждый кошелёк с новой строки в формате:\nUSDT (TRC20): адрес\nTON: адрес",
    "support": "Пришлите @username поддержки.",
    "min_topup": f"Минимальная сумма пополнения баланса в {CURRENCY}, например 1",
    "ref_bonus": f"Сколько {CURRENCY} начислять за 1 приглашённого, например 0.5 (0 = не начислять)",
    "force_channels": "Каналы, на которые нужно подписаться, чтобы пользоваться ботом. "
                      "Через запятую, например: @news_channel, @second_channel\n"
                      "Пришлите «-», чтобы отключить обязательную подписку.\n"
                      "Важно: бот должен быть добавлен в каждый канал администратором.",
}
NUM_SETTINGS = ("min_topup", "ref_bonus")


def st_setting(m, key):
    if aborted(m):
        return
    if not m.text:
        return ask(m.chat.id, "Пришлите текст.", st_setting, key)
    back = "a:set"
    if key in NUM_SETTINGS:
        v = parse_num(m.text, zero_ok=(key == "ref_bonus"))
        if v is None:
            return ask(m.chat.id, "Нужно число, например 0.5", st_setting, key)
        put(key, f"{v:g}")
        back = "a:ref" if key == "ref_bonus" else "a:set"
    elif key == "force_channels":
        t = m.text.strip()
        if t in ("-", "—"):
            put(key, "")
        else:
            chans = []
            for part in re.split(r"[,\s]+", t):
                part = part.strip()
                if not part:
                    continue
                if not re.fullmatch(r"@?[A-Za-z0-9_]{5,32}", part):
                    return ask(m.chat.id, f"«{esc(part)}» не похоже на @username канала. "
                                          "Пришлите список ещё раз.", st_setting, key)
                chans.append("@" + part.lstrip("@"))
            put(key, ",".join(chans))
    else:
        put(key, m.text.strip())
    bot.send_message(m.chat.id, "Сохранено.", reply_markup=ikb([("⬅️ Назад", back)]))


# ---------- пользователи и балансы ----------
def find_user(text):
    t = (text or "").strip().lstrip("@")
    if t.isdigit():
        return q1("SELECT * FROM users WHERE id=?", (int(t),))
    return q1("SELECT * FROM users WHERE LOWER(username)=LOWER(?)", (t,)) if t else None


def st_user_find(m):
    if aborted(m):
        return
    u = find_user(m.text)
    if not u:
        return ask(m.chat.id, "Не нашёл. Пришлите числовой ID или @username "
                              "(пользователь должен хотя бы раз запустить бота).", st_user_find)
    admin_user(m, u["id"])


def admin_user(target, uid):
    u = q1("SELECT * FROM users WHERE id=?", (uid,))
    if not u:
        return show(target, "Пользователь не найден.", ikb([("⬅️ Админка", "a:home")]))
    inv = q1("SELECT COUNT(*) n, COALESCE(SUM(ref_paid),0) r FROM users WHERE ref_by=?", (uid,))
    od = q1("SELECT COUNT(*) n, COALESCE(SUM(price),0) s FROM orders "
            "WHERE user_id=? AND kind='buy' AND status IN ('paid','done')", (uid,))
    by = f"Пригласил: {user_link(u['ref_by'])}\n" if u["ref_by"] else ""
    show(target, f"👤 {user_link(uid)} {esc(u['first_name'])}\n"
                 f"Баланс: <b>{money(get_balance(uid))}</b>\n{by}"
                 f"Приглашено: {inv['n']} (с бонусом: {int(inv['r'])})\n"
                 f"Оплаченных заказов: {od['n']} на {money(round(od['s'], 2))}\n"
                 f"В боте с: {time.strftime('%d.%m.%Y', time.localtime(u['joined'] or 0))}",
         ikb([("➕ Начислить", f"a:uadd:{uid}"), ("➖ Списать", f"a:usub:{uid}")],
             [("⬅️ Админка", "a:home")]))


def st_adjust(m, uid, sign):
    if aborted(m):
        return
    v = parse_num(m.text)
    if v is None:
        return ask(m.chat.id, "Нужно число больше нуля, например 5", st_adjust, uid, sign)
    back = ikb([("👤 К пользователю", f"a:u:{uid}")])
    if sign > 0:
        add_balance(uid, v, "admin", "Начисление администратором")
        safe_send(uid, f"💰 Администратор пополнил ваш баланс на <b>{money(v)}</b>.",
                  ikb([("💰 Баланс", "bal")]))
    elif not spend(uid, v, "admin", "Списание администратором"):
        return bot.send_message(m.chat.id, "У пользователя недостаточно средств.", reply_markup=back)
    bot.send_message(m.chat.id, f"Готово. Баланс пользователя: {money(get_balance(uid))}", reply_markup=back)


def admin_top(c):
    rows = q("SELECT id, username, first_name, balance FROM users WHERE balance>0 "
             "ORDER BY balance DESC LIMIT 15")
    btns = [[(f"{r['first_name'] or r['username'] or r['id']} · {money(r['balance'])}", f"a:u:{r['id']}")]
            for r in rows]
    show(c, "💰 <b>Топ балансов</b>" + ("" if rows else "\n\nПока ни у кого нет средств."),
         ikb(*btns, [("⬅️ Пользователи", "a:us")]))


# ---------- рефералы (админ) ----------
def admin_ref(c):
    on, mode = get("ref_on") == "1", get("ref_mode")
    tot = q1("SELECT COUNT(*) n, COALESCE(SUM(ref_paid),0) r FROM users WHERE ref_by IS NOT NULL")
    paid_out = q1("SELECT COALESCE(SUM(amount),0) s FROM tx WHERE kind='ref'")["s"]
    mode_txt = "после первой оплаты приглашённого" if mode == "paid" else "сразу при входе по ссылке"
    show(c, f"🤝 <b>Реферальная программа</b>\n\nСтатус: {'✅ включена' if on else '🚫 выключена'}\n"
            f"Награда за 1 человека: <b>{money(fnum('ref_bonus'))}</b>\n"
            f"Когда начислять: {mode_txt}\n\n"
            f"Приглашённых всего: {tot['n']}\nБонусов выдано: {int(tot['r'])} на {money(round(paid_out, 2))}",
         ikb([("🔛 Вкл/выкл", "a:rtg")],
             [("💲 Сумма за 1 человека", "a:rbonus")],
             [("🔁 Режим: за вход ⇄ за 1-ю оплату", "a:rmode")],
             [("⬅️ Админка", "a:home")]))


# ---------- промокоды ----------
def admin_promos(c):
    rows = q("SELECT * FROM promocodes ORDER BY created DESC")
    btns = [[(f"{'✅' if r['active'] else '🚫'} {r['code']} · {promo_discount_text(r)} · "
              f"{r['used']}/{r['max_uses'] or '∞'}", f"a:promo:{r['code']}")] for r in rows]
    show(c, "🎟 <b>Промокоды</b>", ikb([("➕ Добавить", "a:paddc")], *btns, [("⬅️ Админка", "a:home")]))


def admin_promo(c, code):
    r = get_promo(code)
    if not r:
        return admin_promos(c)
    show(c, f"🎟 <b>{esc(r['code'])}</b>\nСкидка: {promo_discount_text(r)}\n"
            f"Использован: {r['used']} из {r['max_uses'] or '∞'}\n"
            f"Статус: {'✅ активен' if r['active'] else '🚫 выключен'}",
         ikb([("🔁 Вкл/выкл", f"a:ptgc:{r['code']}")], [("🗑 Удалить", f"a:pdelc:{r['code']}")],
             [("⬅️ Промокоды", "a:promos")]))


def st_promo_code(m):
    if aborted(m):
        return
    code = re.sub(r"\s+", "", (m.text or "")).upper()[:30]
    if not code or not re.fullmatch(r"[A-ZА-Я0-9_-]{2,30}", code):
        return ask(m.chat.id, "Код может содержать буквы, цифры, - и _ (2-30 символов).", st_promo_code)
    if get_promo(code):
        return ask(m.chat.id, "Такой промокод уже существует. Пришлите другой код.", st_promo_code)
    bot.send_message(m.chat.id, f"Код: <code>{esc(code)}</code>\nТип скидки:", reply_markup=ikb(
        [("% скидка", f"a:pkind:{code}:percent")], [("Фикс. сумма", f"a:pkind:{code}:fixed")]))


def st_promo_value(m, code, kind):
    if aborted(m):
        return
    v = parse_num(m.text)
    if v is None or v <= 0 or (kind == "percent" and v > 100):
        hint = "от 0 до 100" if kind == "percent" else "больше 0"
        return ask(m.chat.id, f"Нужно число {hint}, например {'20' if kind == 'percent' else '2'}.",
                   st_promo_value, code, kind)
    ask(m.chat.id, "Максимум использований (0 = без ограничения):", st_promo_max, code, kind, v)


def st_promo_max(m, code, kind, value):
    if aborted(m):
        return
    v = parse_num(m.text, zero_ok=True)
    if v is None or v != int(v):
        return ask(m.chat.id, "Нужно целое число, например 0 или 50.", st_promo_max, code, kind, value)
    ex("INSERT INTO promocodes(code,kind,value,max_uses,created) VALUES(?,?,?,?,?)",
       (code, kind, value, int(v), now()))
    bot.send_message(m.chat.id, "✅ Промокод создан.", reply_markup=ikb([("🎟 К промокоду", f"a:promo:{code}")]))


# ---------- отзывы ----------
def admin_revs(c):
    rows = q("SELECT * FROM reviews ORDER BY id DESC LIMIT 20")
    avg = q1("SELECT AVG(stars) a, COUNT(*) n FROM reviews")
    pub = get("reviews_public") == "1"
    lines = [f"{'⭐' * r['stars']} #{r['order_id']} — {esc(r['username'])}" +
             (f"\n{esc(r['text'])}" if r["text"] else "") for r in rows]
    show(c, f"⭐ <b>Отзывы</b>\n\nВидимость для покупателей: {'✅ показываются' if pub else '🙈 скрыты'}\n"
            f"Средняя оценка: {round(avg['a'] or 0, 1)} ({avg['n']})\n\n" +
            ("\n\n".join(lines) if lines else "Отзывов пока нет."),
         ikb([("🙈 Скрыть от покупателей" if pub else "👁 Показывать покупателям", "a:rvstg")],
             [("⬅️ Админка", "a:home")]))


# ---------- задания (подписка на канал) ----------
def admin_tasks(c):
    rows = q("SELECT * FROM tasks ORDER BY id")
    btns = [[(f"{'✅' if r['active'] else '🚫'} {r['title']} · {r['channel']} · +{money(r['reward'])}",
              f"a:task:{r['id']}")] for r in rows]
    show(c, "📋 <b>Задания</b>\n\nСейчас поддерживается тип «подписка на канал».",
         ikb([("➕ Добавить", "a:taddt")], *btns, [("⬅️ Админка", "a:home")]))


def admin_task(c, tid):
    r = q1("SELECT * FROM tasks WHERE id=?", (tid,))
    if not r:
        return admin_tasks(c)
    done = q1("SELECT COUNT(*) n FROM task_done WHERE task_id=?", (tid,))["n"]
    show(c, f"📋 <b>{esc(r['title'])}</b>\nКанал: {esc(r['channel'])}\nНаграда: {money(r['reward'])}\n"
            f"Выполнили: {done}\nСтатус: {'✅ активно' if r['active'] else '🚫 выключено'}",
         ikb([("🔁 Вкл/выкл", f"a:ttg:{tid}")], [("🗑 Удалить", f"a:tdel:{tid}")],
             [("⬅️ Задания", "a:tasks")]))


def st_task_title(m):
    if aborted(m):
        return
    if not m.text:
        return ask(m.chat.id, "Пришлите название задания.", st_task_title)
    ask(m.chat.id, "Канал для подписки (@username канала, бот должен быть в нём администратором):",
        st_task_channel, m.text.strip()[:80])


def st_task_channel(m, title):
    if aborted(m):
        return
    ch = (m.text or "").strip()
    if not re.fullmatch(r"@?[A-Za-z0-9_]{5,32}", ch):
        return ask(m.chat.id, "Нужен @username канала.", st_task_channel, title)
    ask(m.chat.id, f"Награда в {CURRENCY} за выполнение:", st_task_reward, title, "@" + ch.lstrip("@"))


def st_task_reward(m, title, channel):
    if aborted(m):
        return
    v = parse_num(m.text)
    if v is None or v <= 0:
        return ask(m.chat.id, "Нужно число больше нуля, например 0.5", st_task_reward, title, channel)
    tid = ex("INSERT INTO tasks(title,channel,reward,created) VALUES(?,?,?,?)",
             (title, channel, v, now())).lastrowid
    bot.send_message(m.chat.id, "✅ Задание создано.", reply_markup=ikb([("📋 К заданию", f"a:task:{tid}")]))


# ---------- достижения ----------
def admin_achs(c):
    rows = q("SELECT * FROM achievements ORDER BY id")
    btns = [[(f"{'✅' if r['active'] else '🚫'} {r['emoji']} {r['title']}", f"a:ach:{r['id']}")]
            for r in rows]
    show(c, "🏆 <b>Достижения</b>\n\nСоздавайте достижения и вручайте их пользователям вручную.",
         ikb([("➕ Добавить", "a:aaddt")], *btns, [("⬅️ Админка", "a:home")]))


def admin_ach(c, aid):
    r = q1("SELECT * FROM achievements WHERE id=?", (aid,))
    if not r:
        return admin_achs(c)
    got = q1("SELECT COUNT(*) n FROM user_achievements WHERE achievement_id=?", (aid,))["n"]
    show(c, f"{r['emoji']} <b>{esc(r['title'])}</b>\n{esc(r['text'] or '')}\n\n"
            f"Награда: {money(r['reward'])}\nПолучили: {got}\n"
            f"Статус: {'✅ активно' if r['active'] else '🚫 выключено'}",
         ikb([("🎁 Вручить пользователю", f"a:agive:{aid}")], [("🔁 Вкл/выкл", f"a:atg:{aid}")],
             [("🗑 Удалить", f"a:adel:{aid}")], [("⬅️ Достижения", "a:achs")]))


def st_ach_title(m):
    if aborted(m):
        return
    if not m.text:
        return ask(m.chat.id, "Пришлите название достижения.", st_ach_title)
    ask(m.chat.id, "Описание достижения (или «-», если без описания):", st_ach_text, m.text.strip()[:60])


def st_ach_text(m, title):
    if aborted(m):
        return
    txt = "" if (m.text or "").strip() in ("-", "—") else (m.text or "").strip()[:200]
    ask(m.chat.id, f"Награда в {CURRENCY} (0 = без награды):", st_ach_reward, title, txt)


def st_ach_reward(m, title, text):
    if aborted(m):
        return
    v = parse_num(m.text, zero_ok=True)
    if v is None:
        return ask(m.chat.id, "Нужно число, например 0 или 1.5", st_ach_reward, title, text)
    ask(m.chat.id, "Эмодзи для достижения (например 🏆, ⭐, 🎖):", st_ach_emoji, title, text, v)


def st_ach_emoji(m, title, text, reward):
    if aborted(m):
        return
    emoji = (m.text or "🏆").strip()[:4] or "🏆"
    aid = ex("INSERT INTO achievements(emoji,title,text,reward,created) VALUES(?,?,?,?,?)",
             (emoji, title, text, reward, now())).lastrowid
    bot.send_message(m.chat.id, "✅ Достижение создано.", reply_markup=ikb([("🏆 К достижению", f"a:ach:{aid}")]))


def st_ach_give(m, aid):
    if aborted(m):
        return
    u = find_user(m.text)
    if not u:
        return ask(m.chat.id, "Не нашёл. Пришлите ID или @username пользователя.", st_ach_give, aid)
    r = q1("SELECT * FROM achievements WHERE id=?", (aid,))
    if not r:
        return
    if q1("SELECT 1 FROM user_achievements WHERE user_id=? AND achievement_id=?", (u["id"], aid)):
        return bot.send_message(m.chat.id, "У пользователя уже есть это достижение.",
                                reply_markup=ikb([("🏆 К достижению", f"a:ach:{aid}")]))
    ex("INSERT INTO user_achievements(user_id,achievement_id,created) VALUES(?,?,?)",
       (u["id"], aid, now()))
    if r["reward"] > 0:
        add_balance(u["id"], r["reward"], "admin", f"Достижение «{r['title']}»")
    safe_send(u["id"], f"🏆 Новое достижение: {r['emoji']} <b>{esc(r['title'])}</b>!" +
              (f"\n{esc(r['text'])}" if r["text"] else "") +
              (f"\n\nНачислено: {money(r['reward'])}" if r["reward"] > 0 else ""),
              ikb([("🏆 Мои достижения", "ach")]))
    bot.send_message(m.chat.id, "✅ Вручено.", reply_markup=ikb([("🏆 К достижению", f"a:ach:{aid}")]))


# ---------- контент: приветствие, страницы, кнопки ----------
def admin_content(c):
    n = q1("SELECT COUNT(*) n FROM pages")["n"]
    nb = q1("SELECT COUNT(*) n FROM buttons WHERE parent='main'")["n"]
    show(c, "🎨 <b>Контент и кнопки</b>\n\nЗдесь можно менять приветствие (текст и фото), "
            "текст и кнопки в любом разделе бота (каталог, баланс, рефералы, заказы), "
            "создавать свои страницы (текст + фото + кнопки).\n\n"
            f"Страниц: {n}\nСвоих кнопок в главном меню: {nb}",
         ikb([("👋 Приветствие (текст+фото)", "a:wel")],
             [("🔘 Кнопки главного меню", "a:bm:main")],
             [("🗂 Разделы бота (текст и кнопки)", "a:secs")],
             [("📄 Страницы", "a:pgs")],
             [("⬅️ Админка", "a:home")]))


def admin_secs(c):
    kb = [[(label, f"a:sec:{key}")] for key, label, _ in SECTIONS]
    show(c, "🗂 <b>Разделы бота</b>\n\nВыберите раздел, чтобы изменить его текст, фото "
            "или добавить/убрать кнопки под сообщением.",
         ikb(*kb, [("⬅️ Контент", "a:cm")]))


def admin_section(c, key):
    if key not in SECTION_LABELS:
        return admin_secs(c)
    mode, photo = SECTION_MODE[key], screen_photo(key)
    nb = q1("SELECT COUNT(*) n FROM buttons WHERE parent=?", (key,))["n"]
    if mode == "full":
        hint = "Текст полностью заменяет стандартный. Можно использовать плейсхолдеры в фигурных скобках, показанные ниже."
        cur = screen_text(key)
    else:
        hint = "Текст добавляется ДОПОЛНИТЕЛЬНО, после обычного содержимого раздела (можно оставить пустым)."
        cur = screen_row(key)["text"] if screen_row(key) else ""
    ph_hint = ""
    if key == "bal":
        ph_hint = "\nПлейсхолдер: {balance} — баланс пользователя."
    elif key == "ref":
        ph_hint = "\nПлейсхолдеры: {bonus} — сумма бонуса, {when} — когда начисляется."
    show(c, f"🗂 <b>{esc(SECTION_LABELS[key])}</b>\n{hint}{ph_hint}\n"
            f"Фото: {'есть' if photo else 'нет'}\nСвоих кнопок: {nb}\n\n"
            f"<b>Текущий текст:</b>\n{cur or '— пусто —'}",
         ikb([("✏️ Изменить текст", f"a:sxt:{key}")],
             [("🖼 Задать фото", f"a:sxp:{key}"), ("🗑 Убрать фото", f"a:sxd:{key}")],
             *([[("♻️ Сбросить к стандартному", f"a:sxr:{key}")]] if mode == "full" else []),
             [("🔘 Кнопки раздела", f"a:bm:{key}")],
             [("⬅️ Разделы", "a:secs")]))


def st_section_text(m, key):
    if aborted(m):
        return
    if not m.text:
        return ask(m.chat.id, "Пришлите текст (можно с форматированием).", st_section_text, key)
    txt = m.html_text if m.entities else html.escape(m.text, quote=False)
    set_screen_text(key, txt.strip())
    bot.send_message(m.chat.id, "Сохранено.", reply_markup=ikb([("⬅️ К разделу", f"a:sec:{key}")]))


def st_section_photo(m, key):
    if aborted(m):
        return
    if m.content_type != "photo":
        return ask(m.chat.id, "Пришлите фото (как изображение, не файлом).", st_section_photo, key)
    set_screen_photo(key, m.photo[-1].file_id)
    bot.send_message(m.chat.id, "Фото сохранено.", reply_markup=ikb([("⬅️ К разделу", f"a:sec:{key}")]))


def admin_welcome(c):
    show(c, f"👋 <b>Приветствие</b>\nФото: {'есть' if get('welcome_photo') else 'нет'}\n\n{get('welcome')}",
         ikb([("✏️ Текст / фото", "a:wed")], [("🗑 Убрать фото", "a:wph")],
             [("🔘 Кнопки меню", "a:bm:main"), ("👁 Как у клиента", "a:wpv")],
             [("⬅️ Контент", "a:cm")]))


def st_welcome(m):
    if aborted(m):
        return
    cnt = parse_content(m)
    if not cnt:
        return ask(m.chat.id, "Пришлите текст или фото (с подписью - подпись станет текстом).", st_welcome)
    text, photo = cnt
    if photo:
        put("welcome_photo", photo)
    if text:
        put("welcome", text)
    bot.send_message(m.chat.id, "Сохранено.", reply_markup=ikb([("👋 К приветствию", "a:wel")]))


def parent_title(parent):
    if parent == "main":
        return "главного меню"
    if re.fullmatch(r"p\d+", parent):
        pg = get_page(int(parent[1:]))
        return f"страницы «{esc(pg['title'])}»" if pg else "страницы"
    return SECTION_LABELS.get(parent, f"раздела «{parent}»")


def parent_back(parent):
    if parent == "main":
        return "a:cm"
    if re.fullmatch(r"p\d+", parent):
        return f"a:pg:{parent[1:]}"
    if parent in SECTION_LABELS:
        return f"a:sec:{parent}"
    return "a:home"


def admin_btns(c, parent):
    rows = q("SELECT * FROM buttons WHERE parent=? ORDER BY sort, id", (parent,))
    lines, kb = [], []
    for i, b in enumerate(rows, 1):
        if b["kind"] == "url":
            target = esc(b["value"][:50])
            icon = "🔗"
        else:
            pg = get_page(int(b["value"]))
            target = "страница «" + esc(pg["title"] if pg else "удалена") + "»"
            icon = "📄"
        lines.append(f"{i}. {icon} <b>{esc(b['label'])}</b> → {target}")
        kb.append([(f"❌ {b['label'][:30]}", f"a:bdel:{b['id']}"), ("⬆️", f"a:bup:{b['id']}")])
    back = parent_back(parent)
    show(c, f"🔘 <b>Кнопки {parent_title(parent)}</b>\n\n" +
         ("\n".join(lines) if lines else "Своих кнопок пока нет.") +
         "\n\n❌ — убрать кнопку, ⬆️ — поднять выше.",
         ikb(*kb, [("➕ Добавить кнопку", f"a:bn:{parent}")], [("⬅️ Назад", back)]))


def st_btn_label(m, parent):
    if aborted(m):
        return
    label = (m.text or "").strip()[:60]
    if not label:
        return ask(m.chat.id, "Пришлите текст кнопки (можно с эмодзи).", st_btn_label, parent)
    DRAFT[m.from_user.id] = {"parent": parent, "label": label}
    bot.send_message(m.chat.id, f"Кнопка «{esc(label)}». Что она будет делать?", reply_markup=ikb(
        [("🔗 Открывать ссылку", "a:bt:url")],
        [("📄 Открывать готовую страницу", "a:bt:page")],
        [("➕ Новая страница (текст + фото)", "a:bt:new")],
        [("⬅️ Отмена", f"a:bm:{parent}")]))


def st_btn_url(m, d):
    if aborted(m):
        return
    url = norm_url(m.text)
    if not url:
        return ask(m.chat.id, "Нужна ссылка вида https://... или @username.", st_btn_url, d)
    add_button(d["parent"], d["label"], "url", url)
    DRAFT.pop(m.from_user.id, None)
    bot.send_message(m.chat.id, "✅ Кнопка добавлена.",
                     reply_markup=ikb([("🔘 К кнопкам", f"a:bm:{d['parent']}")]))


def btn_type(c, kind):
    d = DRAFT.get(c.from_user.id)
    if not d:
        return show(c, "Сессия истекла, начните добавление заново.", ikb([("⬅️ Админка", "a:home")]))
    chat = c.message.chat.id
    if kind == "url":
        ask(chat, "Пришлите ссылку (https://... или @username):", st_btn_url, d)
    elif kind == "new":
        ask(chat, "Пришлите текст новой страницы (можно с форматированием) "
                  "или фото с подписью:", st_pnew_content, d["label"], d)
    elif kind == "page":
        pgs = q("SELECT id, title FROM pages ORDER BY id")
        if not pgs:
            return show(c, "Страниц пока нет. Создайте новую.",
                        ikb([("➕ Новая страница", "a:bt:new")], [("⬅️ Отмена", f"a:bm:{d['parent']}")]))
        show(c, "Выберите страницу:", ikb(*[[(f"📄 {r['title']}", f"a:bp:{r['id']}")] for r in pgs],
                                          [("⬅️ Отмена", f"a:bm:{d['parent']}")]))


def btn_bind_page(c, pid):
    d = DRAFT.pop(c.from_user.id, None)
    if not d or not get_page(pid):
        return show(c, "Сессия истекла, начните заново.", ikb([("⬅️ Админка", "a:home")]))
    add_button(d["parent"], d["label"], "page", pid)
    admin_btns(c, d["parent"])


def btn_delete(c, bid):
    b = q1("SELECT * FROM buttons WHERE id=?", (bid,))
    if b:
        ex("DELETE FROM buttons WHERE id=?", (bid,))
        admin_btns(c, b["parent"])


def btn_up(c, bid):
    b = q1("SELECT * FROM buttons WHERE id=?", (bid,))
    if not b:
        return
    prev = q1("SELECT * FROM buttons WHERE parent=? AND (sort<? OR (sort=? AND id<?)) "
              "ORDER BY sort DESC, id DESC LIMIT 1", (b["parent"], b["sort"], b["sort"], b["id"]))
    if prev:
        with _lock:
            ex("UPDATE buttons SET sort=? WHERE id=?", (prev["sort"], b["id"]))
            ex("UPDATE buttons SET sort=? WHERE id=?", (b["sort"], prev["id"]))
            if prev["sort"] == b["sort"]:  # одинаковый sort - разводим
                ex("UPDATE buttons SET sort=sort+1 WHERE id=?", (prev["id"],))
    admin_btns(c, b["parent"])


def admin_pages(c):
    rows = q("SELECT * FROM pages ORDER BY id")
    btns = [[(f"📄 {r['title']}{' 🖼' if r['photo'] else ''}", f"a:pg:{r['id']}")] for r in rows]
    show(c, "📄 <b>Страницы</b>\n\nСвоя страница = текст + (фото) + кнопки. Открывается по кнопке в меню.",
         ikb(*btns, [("➕ Создать страницу", "a:pgnew")], [("⬅️ Контент", "a:cm")]))


def admin_page(c, pid):
    pg = get_page(pid)
    if not pg:
        return admin_pages(c)
    nb = q1("SELECT COUNT(*) n FROM buttons WHERE parent=?", (f"p{pid}",))["n"]
    plain = html.unescape(re.sub(r"<[^>]+>", "", pg["text"] or ""))[:300]
    show(c, f"📄 <b>{esc(pg['title'])}</b>\nФото: {'есть' if pg['photo'] else 'нет'}\n"
            f"Кнопок на странице: {nb}\n\n{esc(plain)}",
         ikb([("✏️ Текст / фото", f"a:pge:{pid}"), ("🗑 Убрать фото", f"a:pgd:{pid}")],
             [("✏️ Название", f"a:pgn:{pid}"), ("🔘 Кнопки", f"a:bm:p{pid}")],
             [("👁 Показать", f"a:pgv:{pid}")],
             [("🗑 Удалить страницу", f"a:pgx:{pid}")],
             [("⬅️ Страницы", "a:pgs")]))


def st_pnew_title(m):
    if aborted(m):
        return
    t = (m.text or "").strip()[:60]
    if not t:
        return ask(m.chat.id, "Пришлите название страницы (видите только вы).", st_pnew_title)
    ask(m.chat.id, "Пришлите текст страницы (можно с форматированием) или фото с подписью:",
        st_pnew_content, t, None)


def st_pnew_content(m, title, bind=None):
    if aborted(m):
        return
    cnt = parse_content(m)
    if not cnt or not (cnt[0] or cnt[1]):
        return ask(m.chat.id, "Пришлите текст страницы или фото с подписью.", st_pnew_content, title, bind)
    text, photo = cnt
    pid = ex("INSERT INTO pages(title,text,photo) VALUES(?,?,?)",
             (title, text or esc(title), photo or "")).lastrowid
    if bind:
        add_button(bind["parent"], bind["label"], "page", pid)
        DRAFT.pop(m.from_user.id, None)
        bot.send_message(m.chat.id, "✅ Страница и кнопка созданы.", reply_markup=ikb(
            [("🔘 К кнопкам", f"a:bm:{bind['parent']}")], [("📄 Открыть страницу", f"a:pg:{pid}")]))
    else:
        bot.send_message(m.chat.id, "✅ Страница создана. Добавьте на неё кнопки или привяжите к меню.",
                         reply_markup=ikb([("📄 Открыть страницу", f"a:pg:{pid}")]))


def st_page_edit(m, pid):
    if aborted(m):
        return
    cnt = parse_content(m)
    if not cnt:
        return ask(m.chat.id, "Пришлите текст или фото с подписью.", st_page_edit, pid)
    text, photo = cnt
    if photo:
        ex("UPDATE pages SET photo=? WHERE id=?", (photo, pid))
    if text:
        ex("UPDATE pages SET text=? WHERE id=?", (text, pid))
    bot.send_message(m.chat.id, "Сохранено.", reply_markup=ikb([("📄 К странице", f"a:pg:{pid}")]))


def st_page_title(m, pid):
    if aborted(m):
        return
    t = (m.text or "").strip()[:60]
    if not t:
        return ask(m.chat.id, "Пришлите название текстом.", st_page_title, pid)
    ex("UPDATE pages SET title=? WHERE id=?", (t, pid))
    bot.send_message(m.chat.id, "Сохранено.", reply_markup=ikb([("📄 К странице", f"a:pg:{pid}")]))


def page_delete(pid):
    ex("DELETE FROM buttons WHERE kind='page' AND value=?", (str(pid),))
    ex("DELETE FROM buttons WHERE parent=?", (f"p{pid}",))
    ex("DELETE FROM pages WHERE id=?", (pid,))


def admin_cb(c, p):
    a = p[0]
    chat = c.message.chat.id
    if a == "home":
        admin_home(c)
    elif a == "list":
        admin_list(c, p[1])
    elif a == "o":
        admin_order(c, int(p[1]))
    elif a in ("ok", "no", "done"):
        admin_action(c, a, int(p[1]))

    # --- товары ---
    elif a == "prods":
        admin_prods(c)
    elif a == "p":
        admin_prod(c, int(p[1]))
    elif a == "padd" and len(p) == 1:
        show(c, "Категория нового товара:", ikb(
            *[[(name, f"a:padd:{k}")] for k, name in CATS.items()], [("⬅️ Товары", "a:prods")]))
    elif a == "padd":
        ask(chat, "Название товара:", st_add_title, p[1])
    elif a == "pti":
        ask(chat, "Новое название:", st_title, int(p[1]))
    elif a == "ppr":
        ask(chat, f"Новая цена в {CURRENCY}:", st_price, int(p[1]))
    elif a == "ptg":
        ex("UPDATE products SET active=1-active WHERE id=?", (int(p[1]),))
        admin_prod(c, int(p[1]))
    elif a == "pdel":
        show(c, "Удалить товар? Старые заказы сохранятся.",
             ikb([("🗑 Да, удалить", f"a:pdel2:{p[1]}")], [("⬅️ Нет", f"a:p:{p[1]}")]))
    elif a == "pdel2":
        ex("DELETE FROM products WHERE id=?", (int(p[1]),))
        admin_prods(c)
    elif a == "stats":
        admin_stats(c)

    # --- промокоды ---
    elif a == "promos":
        admin_promos(c)
    elif a == "promo":
        admin_promo(c, p[1])
    elif a == "paddc":
        ask(chat, "Пришлите код промокода (буквы/цифры, например SALE20):", st_promo_code)
    elif a == "pkind":
        ask(chat, "Размер скидки" + (" в %:" if p[2] == "percent" else f" в {CURRENCY}:"),
            st_promo_value, p[1], p[2])
    elif a == "ptgc":
        ex("UPDATE promocodes SET active=1-active WHERE code=?", (p[1],))
        admin_promo(c, p[1])
    elif a == "pdelc":
        ex("DELETE FROM promocodes WHERE code=?", (p[1],))
        admin_promos(c)

    # --- отзывы ---
    elif a == "revs":
        admin_revs(c)
    elif a == "rvstg":
        put("reviews_public", "0" if get("reviews_public") == "1" else "1")
        admin_revs(c)

    # --- задания ---
    elif a == "tasks":
        admin_tasks(c)
    elif a == "task":
        admin_task(c, int(p[1]))
    elif a == "taddt":
        ask(chat, "Название задания (например «Подпишись на новости»):", st_task_title)
    elif a == "ttg":
        ex("UPDATE tasks SET active=1-active WHERE id=?", (int(p[1]),))
        admin_task(c, int(p[1]))
    elif a == "tdel":
        ex("DELETE FROM tasks WHERE id=?", (int(p[1]),))
        ex("DELETE FROM task_done WHERE task_id=?", (int(p[1]),))
        admin_tasks(c)

    # --- достижения ---
    elif a == "achs":
        admin_achs(c)
    elif a == "ach":
        admin_ach(c, int(p[1]))
    elif a == "aaddt":
        ask(chat, "Название достижения:", st_ach_title)
    elif a == "atg":
        ex("UPDATE achievements SET active=1-active WHERE id=?", (int(p[1]),))
        admin_ach(c, int(p[1]))
    elif a == "adel":
        ex("DELETE FROM achievements WHERE id=?", (int(p[1]),))
        ex("DELETE FROM user_achievements WHERE achievement_id=?", (int(p[1]),))
        admin_achs(c)
    elif a == "agive":
        ask(chat, "Пришлите ID или @username пользователя, которому вручить достижение:",
            st_ach_give, int(p[1]))

    # --- рассылка ---
    elif a == "bc":
        ask(chat, "Пришлите текст рассылки (можно с форматированием) или фото с подписью.", st_bc)
    elif a == "bcgo":
        d = BC.pop(c.from_user.id, None)
        if d:
            threading.Thread(target=do_broadcast, args=(c.from_user.id, d), daemon=True).start()
            show(c, "Рассылка запущена, пришлю отчёт по завершении.", ikb([("⬅️ Админка", "a:home")]))

    # --- настройки ---
    elif a == "set":
        admin_settings(c)
    elif a == "sv":
        ask(chat, SETTING_HINT[p[1]], st_setting, p[1])
    elif a == "ntg":
        put("notify_new", "0" if get("notify_new") == "1" else "1")
        admin_settings(c)

    # --- пользователи / балансы ---
    elif a == "us":
        show(c, "👤 <b>Пользователи</b>", ikb([("🔎 Найти по ID / @username", "a:usf")],
                                             [("💰 Топ балансов", "a:top")], [("⬅️ Админка", "a:home")]))
    elif a == "usf":
        ask(chat, "Пришлите ID или @username пользователя:", st_user_find)
    elif a == "u":
        admin_user(c, int(p[1]))
    elif a == "uadd":
        ask(chat, f"Сколько {CURRENCY} начислить на баланс?", st_adjust, int(p[1]), 1)
    elif a == "usub":
        ask(chat, f"Сколько {CURRENCY} списать с баланса?", st_adjust, int(p[1]), -1)
    elif a == "top":
        admin_top(c)

    # --- рефералы ---
    elif a == "ref":
        admin_ref(c)
    elif a == "rtg":
        put("ref_on", "0" if get("ref_on") == "1" else "1")
        admin_ref(c)
    elif a == "rmode":
        put("ref_mode", "start" if get("ref_mode") == "paid" else "paid")
        admin_ref(c)
    elif a == "rbonus":
        ask(chat, SETTING_HINT["ref_bonus"], st_setting, "ref_bonus")

    # --- контент: приветствие, кнопки, страницы ---
    elif a == "cm":
        admin_content(c)
    elif a == "secs":
        admin_secs(c)
    elif a == "sec":
        admin_section(c, p[1])
    elif a == "sxt":
        ask(chat, "Пришлите новый текст раздела (можно с форматированием).", st_section_text, p[1])
    elif a == "sxp":
        ask(chat, "Пришлите фото для раздела.", st_section_photo, p[1])
    elif a == "sxd":
        set_screen_photo(p[1], "")
        admin_section(c, p[1])
    elif a == "sxr":
        reset_screen_text(p[1])
        admin_section(c, p[1])
    elif a == "wel":
        admin_welcome(c)
    elif a == "wed":
        ask(chat, "Пришлите новый текст приветствия (можно с форматированием) "
                  "или фото (с подписью - подпись станет текстом).", st_welcome)
    elif a == "wph":
        put("welcome_photo", "")
        admin_welcome(c)
    elif a == "wpv":
        main_menu(c.message)
    elif a == "bm":
        admin_btns(c, p[1])
    elif a == "bn":
        ask(chat, "Текст на кнопке (до 60 символов, можно с эмодзи):", st_btn_label, p[1])
    elif a == "bt":
        btn_type(c, p[1])
    elif a == "bp":
        btn_bind_page(c, int(p[1]))
    elif a == "bdel":
        btn_delete(c, int(p[1]))
    elif a == "bup":
        btn_up(c, int(p[1]))
    elif a == "pgs":
        admin_pages(c)
    elif a == "pg":
        admin_page(c, int(p[1]))
    elif a == "pgnew":
        ask(chat, "Название страницы (видите только вы):", st_pnew_title)
    elif a == "pge":
        ask(chat, "Пришлите новый текст страницы или фото (с подписью - подпись станет текстом).",
            st_page_edit, int(p[1]))
    elif a == "pgd":
        ex("UPDATE pages SET photo='' WHERE id=?", (int(p[1]),))
        admin_page(c, int(p[1]))
    elif a == "pgn":
        ask(chat, "Новое название страницы:", st_page_title, int(p[1]))
    elif a == "pgv":
        pg = get_page(int(p[1]))
        if pg:
            send_rich(chat, pg["text"] or esc(pg["title"]), ikb(*page_rows(pg["id"])), pg["photo"] or None)
    elif a == "pgx":
        show(c, "Удалить страницу вместе с её кнопками и кнопками, которые на неё ведут?",
             ikb([("🗑 Да, удалить", f"a:pgx2:{p[1]}")], [("⬅️ Нет", f"a:pg:{p[1]}")]))
    elif a == "pgx2":
        page_delete(int(p[1]))
        admin_pages(c)


# ===================== РОУТЕР =====================
@bot.callback_query_handler(func=lambda c: True)
def on_cb(c):
    try:
        bot.answer_callback_query(c.id)
    except Exception:
        pass
    try:
        touch_user(c.from_user)
        p = c.data.split(":")
        if p[0] == "a":
            if is_admin(c.from_user.id):
                admin_cb(c, p[1:])
        else:
            user_cb(c, p)
    except Exception:
        log.exception("callback error: %s", c.data)


@bot.message_handler(func=lambda m: True, content_types=["text"])
def fallback(m):
    touch_user(m.from_user)
    main_menu(m)


def start_keepalive_server():
    """Render (и похожие бесплатные хостинги) требуют, чтобы сервис слушал
    HTTP-порт - иначе решают, что процесс не запустился, и убивают его.
    Сам ответ не используется, только сам факт "порт слушается"."""
    port = int(os.getenv("PORT", "10000"))

    class _Health(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"bot is running")

        def log_message(self, *a):
            pass  # не засорять логи каждым пингом

    threading.Thread(
        target=lambda: HTTPServer(("0.0.0.0", port), _Health).serve_forever(),
        daemon=True,
    ).start()
    log.info("Keepalive HTTP server on :%s", port)


if __name__ == "__main__":
    init_db()
    log.info(log_msg)
    if os.getenv("PORT"):
        # PORT задан хостингом (Render и т.п.) - значит нужен HTTP-порт,
        # чтобы сервис не считался упавшим. На своём ПК/VPS без Render
        # переменной PORT нет, и этот блок просто не запускается.
        start_keepalive_server()
    try:
        BOT_USERNAME = bot.get_me().username or ""
    except Exception:
        log.warning("Не удалось получить username бота - реферальные ссылки недоступны")
    if CRYPTOPAY_TOKEN:
        threading.Thread(target=poller, daemon=True).start()
    log.info("Bot started (@%s). Admins: %s", BOT_USERNAME, ADMIN_IDS)
    bot.infinity_polling(skip_pending=True, timeout=30, long_polling_timeout=30)
