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
TURSO_URL = os.getenv("TURSO_DATABASE_URL", "")
TURSO_TOKEN = os.getenv("TURSO_AUTH_TOKEN", "")

_lock = threading.RLock()

if TURSO_URL:
    import libsql_client
    DB_BACKEND = "turso"
    # У libsql_client есть два режима: WebSocket (схема libsql:///wss://) и
    # HTTP (схема https://). WebSocket на некоторых хостингах (например,
    # часть регионов Render) падает с ошибкой рукопожатия (400 Invalid
    # response status), поэтому здесь всегда используем HTTP-режим - он
    # надёжнее для облачных окружений с прокси/файрволами перед сервисом.
    _turso_http_url = re.sub(r"^libsql://", "https://", TURSO_URL)
    _raw = libsql_client.create_client_sync(url=_turso_http_url, auth_token=TURSO_TOKEN)
    log_msg = f"База данных: Turso ({_turso_http_url.split('@')[-1] if '@' in _turso_http_url else _turso_http_url})"
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
            rs = _raw.execute(sql, args)
            cols = list(rs.columns or [])
            rows = [dict(zip(cols, r)) for r in rs.rows]
            rowcount = getattr(rs, "rows_affected", None)
            if rowcount is None:
                rowcount = len(rows)
            lastrowid = getattr(rs, "last_insert_rowid", None)
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
                 "AND
