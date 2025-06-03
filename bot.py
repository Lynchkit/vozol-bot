# -*- coding: utf-8 -*-
import os
import json
import requests
import datetime
import random
import string
from apscheduler.schedulers.background import BackgroundScheduler

import psycopg2
from psycopg2 import sql
import telebot
from telebot import types

# Импортируем наши функции для работы с баллами
from points_db import get_points, set_points, add_points, get_connection

# —————————————————————————————————————————————————————————————
#   1. Загрузка переменных окружения
# —————————————————————————————————————————————————————————————
TOKEN = os.getenv("TOKEN")
if not TOKEN:
    raise RuntimeError(
        "Переменная окружения TOKEN не задана! "
        "В Railway в разделе Variables настройте переменную TOKEN=<ваш-токен>."
    )

ADMIN_ID         = int(os.getenv("ADMIN_ID", "0"))
GROUP_CHAT_ID    = int(os.getenv("GROUP_CHAT_ID", "0"))
PERSONAL_CHAT_ID = int(os.getenv("PERSONAL_CHAT_ID", "0"))
RAILWAY_ENV      = os.getenv("RAILWAY_ENV", "development")

bot = telebot.TeleBot(TOKEN, parse_mode="HTML")

# —————————————————————————————————————————————————————————————
#   2. Пути к JSON-файлам
# —————————————————————————————————————————————————————————————
MENU_PATH = "menu.json"
LANG_PATH = "languages.json"

# —————————————————————————————————————————————————————————————
#   3. Создание таблиц в PostgreSQL (users, orders, reviews)
# —————————————————————————————————————————————————————————————
def init_postgres_tables():
    """
    При старте бота проверяем, что таблицы users, orders и reviews есть.
    Если их нет – создаём.
    """
    conn = get_connection()
    conn.autocommit = True
    cur = conn.cursor()

    # 3.1. Таблица users (если ещё нет) – здесь храним chat_id и points и ссылки для рефералов
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            chat_id        BIGINT PRIMARY KEY,
            points         INTEGER DEFAULT 0,
            referral_code  TEXT UNIQUE,
            referred_by    BIGINT
        );
    """)

    # 3.2. Таблица orders (чтобы не терять историю заказов)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS orders (
            order_id    SERIAL PRIMARY KEY,
            chat_id     BIGINT,
            items_json  TEXT,
            total       INTEGER,
            timestamp   TIMESTAMP
        );
    """)

    # 3.3. Таблица reviews (чтобы хранить отзывы)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS reviews (
            review_id   SERIAL PRIMARY KEY,
            chat_id     BIGINT,
            category    TEXT,
            flavor      TEXT,
            rating      INTEGER,
            comment     TEXT,
            timestamp   TIMESTAMP
        );
    """)

    cur.close()
    conn.close()

# Инициализируем таблицы при старте
init_postgres_tables()

# —————————————————————————————————————————————————————————————
#   4. Загрузка menu.json и languages.json
# —————————————————————————————————————————————————————————————
def load_json(path):
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except json.JSONDecodeError:
        return {}

menu = load_json(MENU_PATH)
translations = load_json(LANG_PATH)

# —————————————————————————————————————————————————————————————
#   5. Хранилище данных пользователей (in-memory)
# —————————————————————————————————————————————————————————————
user_data = {}
# Структура user_data[chat_id]:
# {
#   "lang": "ru"/"en",
#   "cart": [ {"category": str, "flavor": str, "price": int}, ... ],
#   "current_category": None / str,
#   "wait_for_points": False,
#   "wait_for_address": False,
#   "wait_for_contact": False,
#   "wait_for_comment": False,
#   "address": str,
#   "contact": str,
#   "comment": str,
#   "pending_discount": int,
#   "pending_points_spent": int,
#   "temp_total_try": int,
#   "temp_user_points": int,
#   "edit_phase": None / str,
#   "edit_cat": None / str,
#   "edit_flavor": None / str,
#   "edit_index": None / int,
#   "edit_cart_phase": None / str
# }

# —————————————————————————————————————————————————————————————
#   6. Утилиты
# —————————————————————————————————————————————————————————————
def t(chat_id: int, key: str) -> str:
    """
    Получает перевод из languages.json по ключу.
    Если перевод не найден — возвращает сам key.
    """
    lang = user_data.get(chat_id, {}).get("lang") or "ru"
    return translations.get(lang, {}).get(key, key)

def generate_ref_code(length=6):
    return "".join(random.choices(string.ascii_uppercase + string.digits, k=length))

def fetch_rates():
    sources = [
        ("https://api.exchangerate.host/latest", {"base": "TRY", "symbols": "RUB,USD,UAH"}),
        ("https://open.er-api.com/v6/latest/TRY", {})
    ]
    for url, params in sources:
        try:
            r = requests.get(url, params=params, timeout=5)
            data = r.json()
            rates = data.get("rates") or data.get("conversion_rates")
            if rates:
                return {k: rates[k] for k in ("RUB", "USD", "UAH") if k in rates}
        except:
            continue
    return {"RUB": 0, "USD": 0, "UAH": 0}

def translate_to_en(text: str) -> str:
    if not text:
        return ""
    try:
        base_url = "https://translate.googleapis.com/translate_a/single"
        params = {
            "client": "gtx",
            "sl": "ru",
            "tl": "en",
            "dt": "t",
            "q": text
        }
        res = requests.get(base_url, params=params, timeout=5)
        data = res.json()
        return data[0][0][0]
    except Exception:
        return text

# —————————————————————————————————————————————————————————————
#   7. Inline-кнопки для выбора языка
# —————————————————————————————————————————————————————————————
def get_inline_language_buttons(chat_id: int) -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        types.InlineKeyboardButton(text="Русский 🇷🇺", callback_data="set_lang|ru"),
        types.InlineKeyboardButton(text="English 🇬🇧", callback_data="set_lang|en")
    )
    return kb

# —————————————————————————————————————————————————————————————
#   8. Inline-кнопки для главного меню
# —————————————————————————————————————————————————————————————
def get_inline_main_menu(chat_id: int) -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup(row_width=2)
    for cat in menu.keys():
        kb.add(types.InlineKeyboardButton(text=cat, callback_data=f"category|{cat}"))
    kb.add(types.InlineKeyboardButton(text=f"🛒 {t(chat_id, 'view_cart')}", callback_data="view_cart"))
    kb.add(types.InlineKeyboardButton(text=f"🗑️ {t(chat_id, 'clear_cart')}", callback_data="clear_cart"))
    kb.add(types.InlineKeyboardButton(text=f"✅ {t(chat_id, 'finish_order')}", callback_data="finish_order"))
    return kb

# —————————————————————————————————————————————————————————————
#   9. Inline-кнопки для выбора вкусов
# —————————————————————————————————————————————————————————————
def get_inline_flavors(chat_id: int, cat: str) -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup(row_width=1)
    price = menu[cat]["price"]
    for item in menu[cat]["flavors"]:
        if item.get("stock", 0) > 0:
            emoji = item.get("emoji", "")
            flavor_name = item["flavor"]
            label = f"{emoji} {flavor_name} — {price}₺ [{item['stock']}шт]"
            kb.add(types.InlineKeyboardButton(text=label, callback_data=f"flavor|{cat}|{flavor_name}"))
    kb.add(types.InlineKeyboardButton(text=f"⬅️ {t(chat_id, 'back_to_categories')}", callback_data="go_back_to_categories"))
    return kb

# —————————————————————————————————————————————————————————————
#   10. Reply-клавиатуры (альтернатива inline)
# —————————————————————————————————————————————————————————————
def address_keyboard() -> types.ReplyKeyboardMarkup:
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
    kb.add(types.KeyboardButton(t(None, "share_location"), request_location=True))
    kb.add(t(None, "choose_on_map"))
    kb.add(t(None, "enter_address_text"))
    kb.add(t(None, "back"))
    return kb

def contact_keyboard() -> types.ReplyKeyboardMarkup:
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
    kb.add(types.KeyboardButton(t(None, "share_contact"), request_contact=True))
    kb.add(t(None, "enter_nickname"))
    kb.add(t(None, "back"))
    return kb

def comment_keyboard() -> types.ReplyKeyboardMarkup:
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
    kb.add(t(None, "enter_comment"))
    kb.add(t(None, "send_order"))
    kb.add(t(None, "back"))
    return kb

# —————————————————————————————————————————————————————————————
#   11. Клавиатура редактирования меню (/change) — ВСЁ НА АНГЛИЙСКОМ
# —————————————————————————————————————————————————————————————
def edit_action_keyboard() -> types.ReplyKeyboardMarkup:
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
    kb.add("➕ Add Category", "➖ Remove Category")
    kb.add("💲 Fix Price", "ALL IN", "🔄 Actual Flavor")
    kb.add("⬅️ Back", "❌ Cancel")
    return kb

# —————————————————————————————————————————————————————————————
#   12. Планировщик – еженедельный дайджест
# —————————————————————————————————————————————————————————————
def send_weekly_digest():
    """
    Собираем топ-3 самых продаваемых вкусов за неделю и раздаём всем клиентам.
    """
    conn = get_connection()
    cur = conn.cursor()

    one_week_ago = datetime.datetime.utcnow() - datetime.timedelta(days=7)
    cur.execute("SELECT items_json FROM orders WHERE timestamp >= %s;", (one_week_ago,))
    recent = cur.fetchall()

    counts = {}
    for (items_json,) in recent:
        items = json.loads(items_json)
        for i in items:
            counts[i["flavor"]] = counts.get(i["flavor"], 0) + 1

    top3 = sorted(counts.items(), key=lambda x: x[1], reverse=True)[:3]
    if not top3:
        text = "📢 За прошедшую неделю не было продаж."
    else:
        lines = [f"{flavor}: {qty} продаж" for flavor, qty in top3]
        text = "📢 Топ-3 вкуса за неделю:\n" + "\n".join(lines)

    # Рассылаем всем пользователям, которые когда-либо делали заказ
    cur.execute("SELECT DISTINCT chat_id FROM orders;")
    users = cur.fetchall()
    for (uid,) in users:
        bot.send_message(uid, text)

    cur.close()
    conn.close()

scheduler = BackgroundScheduler(timezone="UTC")
scheduler.add_job(send_weekly_digest, trigger="cron", day_of_week="mon", hour=9, minute=0)
scheduler.start()

# —————————————————————————————————————————————————————————————
#   13. Хендлер /start – регистрация, рефералька, выбор языка
# —————————————————————————————————————————————————————————————
@bot.message_handler(commands=['start'])
def cmd_start(message):
    chat_id = message.chat.id

    # Если пользователя нет в user_data, создаём ему начальную структуру
    if chat_id not in user_data:
        user_data[chat_id] = {
            "lang": None,
            "cart": [],
            "current_category": None,
            "wait_for_points": False,
            "wait_for_address": False,
            "wait_for_contact": False,
            "wait_for_comment": False,
            "address": "",
            "contact": "",
            "comment": "",
            "pending_discount": 0,
            "pending_points_spent": 0,
            "temp_total_try": 0,
            "temp_user_points": 0,
            "edit_phase": None,
            "edit_cat": None,
            "edit_flavor": None,
            "edit_index": None,
            "edit_cart_phase": None
        }
    data = user_data[chat_id]

    # Сбрасываем все флаги, кроме "lang"
    data.update({
        "cart": [],
        "current_category": None,
        "wait_for_points": False,
        "wait_for_address": False,
        "wait_for_contact": False,
        "wait_for_comment": False,
        "address": "",
        "contact": "",
        "comment": "",
        "pending_discount": 0,
        "pending_points_spent": 0,
        "temp_total_try": 0,
        "temp_user_points": 0,
        "edit_phase": None,
        "edit_cat": None,
        "edit_flavor": None,
        "edit_index": None,
        "edit_cart_phase": None
    })

    # Проверяем, есть ли уже запись о пользователе в таблице users (Postgres)
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT chat_id FROM users WHERE chat_id = %s;", (chat_id,))
    if cur.fetchone() is None:
        # Если пришли по реферальной ссылке (например, "/start ref=XYZ"), получаем код
        text = message.text or ""
        referred_by = None
        if "ref=" in text:
            code = text.split("ref=")[1]
            cur.execute("SELECT chat_id FROM users WHERE referral_code = %s;", (code,))
            row = cur.fetchone()
            if row:
                referred_by = row[0]

        # Генерируем уникальный referral_code
        new_code = generate_ref_code()
        while True:
            cur.execute("SELECT referral_code FROM users WHERE referral_code = %s;", (new_code,))
            if cur.fetchone() is None:
                break
            new_code = generate_ref_code()

        # Вставляем нового пользователя (chat_id, points=0, referral_code, referred_by)
        cur.execute(
            "INSERT INTO users (chat_id, points, referral_code, referred_by) VALUES (%s, %s, %s, %s);",
            (chat_id, 0, new_code, referred_by)
        )
        conn.commit()
    cur.close()
    conn.close()

    # Отправляем кнопку выбора языка
    bot.send_message(
        chat_id,
        t(chat_id, "choose_language"),
        reply_markup=get_inline_language_buttons(chat_id)
    )

# —————————————————————————————————————————————————————————————
#   14. Callback: выбор языка
# —————————————————————————————————————————————————————————————
@bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("set_lang|"))
def handle_set_lang(call):
    chat_id = call.from_user.id
    _, lang_code = call.data.split("|", 1)

    # Устанавливаем выбранный язык
    if chat_id not in user_data:
        user_data[chat_id] = {
            "lang": lang_code,
            "cart": [],
            "current_category": None,
            "wait_for_points": False,
            "wait_for_address": False,
            "wait_for_contact": False,
            "wait_for_comment": False,
            "address": "",
            "contact": "",
            "comment": "",
            "pending_discount": 0,
            "pending_points_spent": 0,
            "temp_total_try": 0,
            "temp_user_points": 0,
            "edit_phase": None,
            "edit_cat": None,
            "edit_flavor": None,
            "edit_index": None,
            "edit_cart_phase": None
        }
    else:
        user_data[chat_id]["lang"] = lang_code

    bot.answer_callback_query(call.id, t(chat_id, "lang_set"))

    # Отправляем главное меню после выбора языка
    bot.send_message(chat_id, t(chat_id, "choose_category"), reply_markup=get_inline_main_menu(chat_id))

    # Отправляем реферальную ссылку (код взят из базы)
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT referral_code FROM users WHERE chat_id = %s;", (chat_id,))
    row = cur.fetchone()
    if row:
        code = row[0]
        bot_username = bot.get_me().username
        ref_link = f"https://t.me/{bot_username}?start=ref={code}"
        if user_data[chat_id]["lang"] == "en":
            bot.send_message(
                chat_id,
                f"Your referral code: {code}\n"
                f"Share this link with friends:\n{ref_link}"
            )
        else:
            bot.send_message(
                chat_id,
                f"Ваш реферальный код: {code}\n"
                f"Поделитесь этой ссылкой с друзьями:\n{ref_link}"
            )
    cur.close()
    conn.close()

# —————————————————————————————————————————————————————————————
#   15. Callback: выбор категории (показываем вкусы)
# —————————————————————————————————————————————————————————————
@bot.callback_query_handler(func=lambda call: call.data and call.data.startswith("category|"))
def handle_category(call):
    chat_id = call.from_user.id
    _, cat = call.data.split("|", 1)

    if cat not in menu:
        bot.answer_callback_query(call.id, t(chat_id, "error_invalid"))
        return

    bot.answer_callback_query(call.id)
    user_data[chat_id]["current_category"] = cat

    # Если у категории есть фото, отправляем
    photo_url = menu[cat].get("photo_url", "").strip()
    if photo_url:
        try:
            bot.send_photo(chat_id, photo_url)
        except Exception as e:
            print(f"Не удалось отправить фото для категории {cat}: {e}")

    bot.send_message(
        chat_id,
        f"{t(chat_id, 'choose_flavor')} «{cat}»",
        reply_markup=get_inline_flavors(chat_id, cat)
    )

# —————————————————————————————————————————————————————————————
#   16. Callback: «Назад к категориям»
# —————————————————————————————————————————————————————————————
@bot.callback_query_handler(func=lambda call: call.data == "go_back_to_categories")
def handle_go_back_to_categories(call):
    chat_id = call.from_user.id
    bot.answer_callback_query(call.id)
    bot.send_message(chat_id, t(chat_id, "choose_category"), reply_markup=get_inline_main_menu(chat_id))

# —————————————————————————————————————————————————————————————
#   17. Callback: выбор вкуса
# —————————————————————————————————————————————————————————————
@bot.callback_query_handler(func=lambda call: call.data and call.data.startswith("flavor|"))
def handle_flavor(call):
    _, cat, flavor = call.data.split("|", 2)
    chat_id = call.from_user.id

    if cat not in menu:
        bot.answer_callback_query(call.id, t(chat_id, "error_invalid"))
        return

    item_obj = next((i for i in menu[cat]["flavors"] if i["flavor"] == flavor), None)
    if not item_obj:
        bot.answer_callback_query(call.id, t(chat_id, "error_invalid"))
        return

    bot.answer_callback_query(call.id)

    user_lang = user_data.get(chat_id, {}).get("lang", "ru")
    description = item_obj.get(f"description_{user_lang}", "") or ""
    price = menu[cat]["price"]

    caption = f"<b>{flavor}</b> — {cat}\n"
    if description:
        caption += f"{description}\n"
    caption += f"📌 {price}₺"

    bot.send_message(chat_id, caption, parse_mode="HTML")

    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(
        types.InlineKeyboardButton(
            text=f"➕ {t(chat_id, 'add_to_cart')}",
            callback_data=f"add_to_cart|{cat}|{flavor}"
        )
    )
    kb.add(
        types.InlineKeyboardButton(
            text=f"⬅️ {t(chat_id, 'back_to_categories')}",
            callback_data="go_back_to_categories"
        )
    )
    kb.add(
        types.InlineKeyboardButton(
            text=f"✅ {t(chat_id, 'finish_order')}",
            callback_data="finish_order"
        )
    )
    bot.send_message(chat_id, t(chat_id, "choose_action"), reply_markup=kb)

# —————————————————————————————————————————————————————————————
#   18. Callback: добавить в корзину (без изменения stock)
# —————————————————————————————————————————————————————————————
@bot.callback_query_handler(func=lambda call: call.data and call.data.startswith("add_to_cart|"))
def handle_add_to_cart(call):
    chat_id = call.from_user.id
    _, cat, flavor = call.data.split("|", 2)

    item_obj = next((i for i in menu[cat]["flavors"] if i["flavor"] == flavor), None)
    if not item_obj or item_obj.get("stock", 0) <= 0:
        bot.answer_callback_query(call.id, t(chat_id, "error_out_of_stock"))
        return

    bot.answer_callback_query(call.id)
    data = user_data.setdefault(chat_id, {})
    cart = data.setdefault("cart", [])

    price = menu[cat]["price"]
    cart.append({"category": cat, "flavor": flavor, "price": price})

    template = t(chat_id, "added_to_cart")
    suffix = template.split("»", 1)[1].strip()
    count = len(cart)
    bot.send_message(
        chat_id,
        f"«{cat} — {flavor}» {suffix.format(flavor=flavor, count=count)}",
        reply_markup=get_inline_main_menu(chat_id)
    )

# —————————————————————————————————————————————————————————————
#   19. Callback: «Просмотр корзины»
# —————————————————————————————————————————————————————————————
@bot.callback_query_handler(func=lambda call: call.data == "view_cart")
def handle_view_cart(call):
    chat_id = call.from_user.id
    bot.answer_callback_query(call.id)
    data = user_data.get(chat_id, {})
    cart = data.get("cart", [])
    if not cart:
        bot.send_message(chat_id, t(chat_id, "cart_empty"), reply_markup=get_inline_main_menu(chat_id))
        return

    grouped = {}
    for item in cart:
        key = (item["category"], item["flavor"], item["price"])
        grouped[key] = grouped.get(key, 0) + 1

    text_lines = [f"🛒 {t(chat_id, 'view_cart')}:"] 
    for idx, ((cat, flavor, price), qty) in enumerate(grouped.items(), start=1):
        text_lines.append(f"{idx}. {cat} — {flavor} — {price}₺ x {qty}")
    msg = "\n".join(text_lines)

    kb = types.InlineKeyboardMarkup(row_width=2)
    for idx, ((cat, flavor, price), qty) in enumerate(grouped.items(), start=1):
        kb.add(
            types.InlineKeyboardButton(
                text=f"{t(chat_id, 'remove_item')} {idx}",
                callback_data=f"remove_item|{idx}"
            ),
            types.InlineKeyboardButton(
                text=f"{t(chat_id, 'edit_item')} {idx}",
                callback_data=f"edit_item|{idx}"
            )
        )
    kb.add(
        types.InlineKeyboardButton(
            text=f"⬅️ {t(chat_id, 'back_to_categories')}",
            callback_data="go_back_to_categories"
        )
    )
    bot.send_message(chat_id, msg, reply_markup=kb)

# —————————————————————————————————————————————————————————————
#   20. Callback: «Удалить i» из корзины (без возврата stock)
# —————————————————————————————————————————————————————————————
@bot.callback_query_handler(func=lambda call: call.data and call.data.startswith("remove_item|"))
def handle_remove_item(call):
    chat_id = call.from_user.id
    _, idx_str = call.data.split("|", 1)
    idx = int(idx_str) - 1
    data = user_data.get(chat_id, {})
    cart = data.get("cart", [])

    grouped = {}
    for item in cart:
        key = (item["category"], item["flavor"], item["price"])
        grouped[key] = grouped.get(key, 0) + 1
    items_list = list(grouped.items())

    if idx < 0 or idx >= len(items_list):
        bot.answer_callback_query(call.id, t(chat_id, "error_invalid"))
        return

    (cat, flavor, price), qty = items_list[idx]

    removed = False
    new_cart = []
    for it in cart:
        if not removed and it["category"] == cat and it["flavor"] == flavor and it["price"] == price:
            removed = True
            continue
        new_cart.append(it)
    data["cart"] = new_cart

    bot.answer_callback_query(call.id, t(chat_id, "item_removed").format(flavor=flavor))
    handle_view_cart(call)

# —————————————————————————————————————————————————————————————
#   21. Callback: «Изменить i» в корзине → ввод количества
# —————————————————————————————————————————————————————————————
@bot.callback_query_handler(func=lambda call: call.data and call.data.startswith("edit_item|"))
def handle_edit_item_request(call):
    chat_id = call.from_user.id
    _, idx_str = call.data.split("|", 1)
    idx = int(idx_str) - 1
    data = user_data.get(chat_id, {})
    cart = data.get("cart", [])

    grouped = {}
    for item in cart:
        key = (item["category"], item["flavor"], item["price"])
        grouped[key] = grouped.get(key, 0) + 1
    items_list = list(grouped.items())
    if idx < 0 or idx >= len(items_list):
        bot.answer_callback_query(call.id, t(chat_id, "error_invalid"))
        return
    (cat, flavor, price), old_qty = items_list[idx]

    bot.answer_callback_query(call.id)
    data["edit_cart_phase"] = "enter_qty"
    data["edit_index"] = idx
    data["edit_cat"] = cat
    data["edit_flavor"] = flavor
    bot.send_message(
        chat_id,
        f"Текущий товар: {cat} — {flavor} — {price}₺ (в корзине {old_qty} шт).\n{t(chat_id, 'enter_new_qty')}",
        reply_markup=types.ReplyKeyboardRemove()
    )

@bot.message_handler(
    func=lambda m: user_data.get(m.chat.id, {}).get("edit_cart_phase") == "enter_qty",
    content_types=['text']
)
def handle_enter_new_qty(message):
    chat_id = message.chat.id
    data = user_data.get(chat_id, {})
    text = message.text.strip()
    if not text.isdigit():
        bot.send_message(chat_id, t(chat_id, "error_invalid"))
        data["edit_cart_phase"] = None
        return

    new_qty = int(text)
    idx = data.get("edit_index", -1)
    cart = data.get("cart", [])
    grouped = {}
    for item in cart:
        key = (item["category"], item["flavor"], item["price"])
        grouped[key] = grouped.get(key, 0) + 1
    items_list = list(grouped.items())
    if idx < 0 or idx >= len(items_list):
        bot.send_message(chat_id, t(chat_id, "error_invalid"))
        data["edit_cart_phase"] = None
        return

    (cat, flavor, price), old_qty = items_list[idx]

    # Пересобираем корзину
    new_cart = [it for it in cart if not (it["category"] == cat and it["flavor"] == flavor and it["price"] == price)]
    for _ in range(new_qty):
        new_cart.append({"category": cat, "flavor": flavor, "price": price})
    data["cart"] = new_cart

    data["edit_cart_phase"] = None
    data.pop("edit_index", None)
    data.pop("edit_cat", None)
    data.pop("edit_flavor", None)

    if new_qty == 0:
        bot.send_message(chat_id, t(chat_id, "item_removed").format(flavor=flavor),
                         reply_markup=get_inline_main_menu(chat_id))
    else:
        bot.send_message(chat_id, t(chat_id, "qty_changed").format(flavor=flavor, qty=new_qty),
                         reply_markup=get_inline_main_menu(chat_id))

    user_data[chat_id] = data

# —————————————————————————————————————————————————————————————
#   22. Callback: «Очистить корзину»
# —————————————————————————————————————————————————————————————
@bot.callback_query_handler(func=lambda call: call.data == "clear_cart")
def handle_clear_cart(call):
    chat_id = call.from_user.id
    bot.answer_callback_query(call.id)
    data = user_data.get(chat_id, {})
    data["cart"] = []
    bot.send_message(chat_id, t(chat_id, "cart_cleared"), reply_markup=get_inline_main_menu(chat_id))
    user_data[chat_id] = data

# —————————————————————————————————————————————————————————————
#   23. Callback: завершить заказ (с учётом списания баллов и stock)
# —————————————————————————————————————————————————————————————
@bot.callback_query_handler(func=lambda call: call.data == "finish_order")
def handle_finish_order(call):
    chat_id = call.from_user.id
    bot.answer_callback_query(call.id)
    data = user_data.get(chat_id, {})

    cart = data.get("cart", [])
    if not cart:
        bot.send_message(chat_id, t(chat_id, "cart_empty"))
        return

    total_try = sum(item["price"] for item in cart)

    # Берём текущее кол-во баллов пользователя
    user_points = get_points(chat_id)

    if user_points > 0:
        max_points = min(user_points, total_try)
        points_try = user_points  # 1 балл = 1₺
        msg = (
            t(chat_id, "points_info").format(points=user_points, points_try=points_try)
            + "\n" + t(chat_id, "enter_points").format(max_points=max_points)
        )
        bot.send_message(chat_id, msg, reply_markup=types.ReplyKeyboardRemove())
        data["wait_for_points"] = True
        data["temp_total_try"] = total_try
        data["temp_user_points"] = user_points
    else:
        # Если баллов нет, сразу просим адрес
        kb = address_keyboard()
        bot.send_message(
            chat_id,
            f"🛒 {t(chat_id, 'view_cart')}:\n\n" +
            "\n".join(f"{item['category']}: {item['flavor']} — {item['price']}₺" for item in cart) +
            f"\n\n{t(chat_id, 'enter_address')}",
            reply_markup=kb
        )
        data["wait_for_address"] = True

    user_data[chat_id] = data

# —————————————————————————————————————————————————————————————
#   24. Handler: ввод количества баллов для списания
# —————————————————————————————————————————————————————————————
@bot.message_handler(func=lambda m: user_data.get(m.chat.id, {}).get("wait_for_points"), content_types=['text'])
def handle_points_input(message):
    chat_id = message.chat.id
    data = user_data.get(chat_id, {})
    text = message.text.strip()

    if not text.isdigit():
        bot.send_message(chat_id, t(chat_id, "invalid_points").format(max_points=data.get("temp_total_try", 0)))
        return

    points_to_spend = int(text)
    user_points     = data.get("temp_user_points", 0)
    total_try       = data.get("temp_total_try", 0)
    max_points      = min(user_points, total_try)

    if points_to_spend < 0 or points_to_spend > max_points:
        bot.send_message(chat_id, t(chat_id, "invalid_points").format(max_points=max_points))
        return

    # Списываем баллы (если points_to_spend > 0)
    if points_to_spend > 0:
        add_points(chat_id, -points_to_spend)

    # Сохраняем скидку
    discount_try = points_to_spend
    data["pending_discount"] = discount_try
    data["pending_points_spent"] = points_to_spend
    data["wait_for_points"] = False

    cart = data.get("cart", [])
    total_after = total_try - discount_try
    kb = address_keyboard()

    summary_lines = [f"{item['category']}: {item['flavor']} — {item['price']}₺" for item in cart]
    summary = "\n".join(summary_lines)
    msg = (
        f"🛒 {t(chat_id, 'view_cart')}:\n\n"
        f"{summary}\n\n"
        f"Итог до скидки: {total_try}₺\n"
        f"Списано баллов: {points_to_spend} (−{discount_try}₺)\n"
        f"К оплате: {total_after}₺\n\n"
        f"{t(chat_id, 'enter_address')}"
    )
    bot.send_message(chat_id, msg, reply_markup=kb)
    data["wait_for_address"] = True
    user_data[chat_id] = data

# —————————————————————————————————————————————————————————————
#   25. Handler: ввод адреса
# —————————————————————————————————————————————————————————————
@bot.message_handler(
    func=lambda m: user_data.get(m.chat.id, {}).get("wait_for_address"),
    content_types=['text','location','venue']
)
def handle_address_input(message):
    chat_id = message.chat.id
    data = user_data.get(chat_id, {})
    text = message.text or ""

    if text == t(chat_id, "back"):
        data['wait_for_address'] = False
        data['current_category'] = None
        bot.send_message(chat_id, t(chat_id, "choose_category"), reply_markup=get_inline_main_menu(chat_id))
        user_data[chat_id] = data
        return

    if text == t(None, "choose_on_map"):
        bot.send_message(
            chat_id,
            "Чтобы выбрать точку:\n📎 → Местоположение → «Выбрать на карте» → метка → Отправить",
            reply_markup=types.ReplyKeyboardRemove()
        )
        return

    if message.content_type == 'venue' and message.venue:
        v = message.venue
        address = f"{v.title}, {v.address}\n🌍 https://maps.google.com/?q={v.location.latitude},{v.location.longitude}"
    elif message.content_type == 'location' and message.location:
        lat, lon = message.location.latitude, message.location.longitude
        address = f"🌍 https://maps.google.com/?q={lat},{lon}"
    elif text == t(None, "enter_address_text"):
        bot.send_message(chat_id, t(chat_id, "enter_address"), reply_markup=types.ReplyKeyboardRemove())
        return
    elif message.content_type == 'text' and message.text:
        address = message.text.strip()
    else:
        bot.send_message(chat_id, t(chat_id, "error_invalid"), reply_markup=address_keyboard())
        return

    data['address'] = address
    data['wait_for_address'] = False
    data['wait_for_contact'] = True
    kb = contact_keyboard()
    bot.send_message(chat_id, t(chat_id, "enter_contact"), reply_markup=kb)
    user_data[chat_id] = data

# —————————————————————————————————————————————————————————————
#   26. Handler: ввод контакта
# —————————————————————————————————————————————————————————————
@bot.message_handler(
    func=lambda m: user_data.get(m.chat.id, {}).get("wait_for_contact"),
    content_types=['text','contact']
)
def handle_contact_input(message):
    chat_id = message.chat.id
    data = user_data.get(chat_id, {})
    text = message.text or ""

    if text == t(chat_id, "back"):
        data['wait_for_address'] = True
        data['wait_for_contact'] = False
        bot.send_message(chat_id, t(chat_id, "enter_address"), reply_markup=address_keyboard())
        user_data[chat_id] = data
        return

    if text == t(None, "enter_nickname"):
        bot.send_message(chat_id, "Введите ваш Telegram-ник (без @):", reply_markup=types.ReplyKeyboardRemove())
        return

    if message.content_type == 'contact' and message.contact:
        contact = message.contact.phone_number
    elif message.content_type == 'text' and message.text:
        contact = "@" + message.text.strip().lstrip("@")
    else:
        bot.send_message(chat_id, t(chat_id, "enter_contact"), reply_markup=contact_keyboard())
        return

    data['contact'] = contact
    data['wait_for_contact'] = False
    data['wait_for_comment'] = True
    kb = comment_keyboard()
    bot.send_message(chat_id, t(chat_id, "enter_comment"), reply_markup=kb)
    user_data[chat_id] = data

# —————————————————————————————————————————————————————————————
#   27. Handler: ввод комментария и сохранение заказа (с учётом скидки и списания stock)
# —————————————————————————————————————————————————————————————
@bot.message_handler(
    func=lambda m: user_data.get(m.chat.id, {}).get("wait_for_comment"),
    content_types=['text']
)
def handle_comment_input(message):
    chat_id = message.chat.id
    data = user_data.get(chat_id, {})
    text = message.text or ""

    if text == t(chat_id, "back"):
        data['wait_for_contact'] = True
        data['wait_for_comment'] = False
        bot.send_message(chat_id, t(chat_id, "enter_contact"), reply_markup=contact_keyboard())
        user_data[chat_id] = data
        return

    if text == t(None, "enter_comment"):
        bot.send_message(chat_id, t(chat_id, "enter_comment"), reply_markup=types.ReplyKeyboardRemove())
        return

    if message.content_type == 'text' and text != t(None, "send_order"):
        data['comment'] = text.strip()
        bot.send_message(chat_id, t(chat_id, "comment_saved"), reply_markup=comment_keyboard())
        user_data[chat_id] = data
        return

    if text == t(None, "send_order"):
        cart = data.get('cart', [])
        if not cart:
            bot.send_message(chat_id, t(chat_id, "cart_empty"))
            return

        total_try = sum(i['price'] for i in cart)
        discount = data.pop("pending_discount", 0)
        total_after = max(total_try - discount, 0)

        # ——— Проверяем наличие stock ———
        needed = {}
        for it in cart:
            key = (it["category"], it["flavor"])
            needed[key] = needed.get(key, 0) + 1

        for (cat0, flavor0), qty_needed in needed.items():
            item_obj = next((i for i in menu[cat0]["flavors"] if i["flavor"] == flavor0), None)
            if not item_obj or item_obj.get("stock", 0) < qty_needed:
                bot.send_message(chat_id, f"😕 К сожалению, «{flavor0}» уже нет в нужном количестве.")
                return

        # Если всё в порядке, уменьшаем stock в menu.json
        for (cat0, flavor0), qty_needed in needed.items():
            for itm in menu[cat0]["flavors"]:
                if itm["flavor"] == flavor0:
                    itm["stock"] -= qty_needed
                    break
        with open(MENU_PATH, "w", encoding="utf-8") as f:
            json.dump(menu, f, ensure_ascii=False, indent=2)

        # Сохраняем заказ в Postgres
        items_json = json.dumps(cart, ensure_ascii=False)
        now = datetime.datetime.utcnow()
        conn = get_connection()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO orders (chat_id, items_json, total, timestamp) VALUES (%s, %s, %s, %s);",
            (chat_id, items_json, total_after, now)
        )
        conn.commit()
        cur.close()
        conn.close()

        # Начисление бонусных баллов: 1 балл = 30₺ вместо 20₺
        earned = total_after // 30
        if earned > 0:
            add_points(chat_id, earned)
            bot.send_message(chat_id, f"👍 Вы получили {earned} бонусных баллов за этот заказ.")

        # Начисление бонусного приглашения
        conn = get_connection()
        cur = conn.cursor()
        cur.execute("SELECT referred_by FROM users WHERE chat_id = %s;", (chat_id,))
        row = cur.fetchone()
        if row and row[0]:
            inviter = row[0]
            add_points(inviter, 200)
            bot.send_message(inviter, "🎉 Вам начислено 200 бонусных баллов за приглашение нового клиента!")
            cur.execute("UPDATE users SET referred_by = NULL WHERE chat_id = %s;", (chat_id,))
            conn.commit()
        cur.close()
        conn.close()

        # Отправляем админам (личный чат) – на русском
        summary_rus = "\n".join(f"{i['category']}: {i['flavor']} — {i['price']}₺" for i in cart)
        rates = fetch_rates()
        rub = round(total_after * rates.get("RUB", 0) + 500, 2)
        usd = round(total_after * rates.get("USD", 0) + 2, 2)
        uah = round(total_after * rates.get("UAH", 0) + 200, 2)
        conv = f"({rub}₽, ${usd}, ₴{uah})"

        full_rus = (
            f"📥 Новый заказ от @{message.from_user.username or message.from_user.first_name}:\n\n"
            f"{summary_rus}\n\n"
            f"Итог: {total_after}₺ {conv}\n"
            f"📍 Адрес: {data.get('address', '—')}\n"
            f"📱 Контакт: {data.get('contact', '—')}\n"
            f"💬 Комментарий: {data.get('comment', '—')}"
        )
        bot.send_message(PERSONAL_CHAT_ID, full_rus)

        # Отправляем в группу – на английском
        summary_en = "\n".join(f"{i['category']}: {i['flavor']} — {i['price']}₺" for i in cart)
        comment_ru = data.get('comment', '')
        comment_en = translate_to_en(comment_ru) if comment_ru else "—"
        full_en = (
            f"📥 New order from @{message.from_user.username or message.from_user.first_name}:\n\n"
            f"{summary_en}\n\n"
            f"Total: {total_after}₺ {conv}\n"
            f"📍 Address: {data.get('address', '—')}\n"
            f"📱 Contact: {data.get('contact', '—')}\n"
            f"💬 Comment: {comment_en}"
        )
        bot.send_message(GROUP_CHAT_ID, full_en)

        # Отправляем кнопку «➕ Добавить ещё»
        bot.send_message(
            chat_id,
            t(chat_id, "order_accepted"),
            reply_markup=types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
                             .add(f"➕ {t(chat_id, 'add_more')}")
        )

        # Очищаем корзину
        data.update({
            "cart": [],
            "current_category": None,
            "wait_for_address": False,
            "wait_for_contact": False,
            "wait_for_comment": False
        })
        user_data[chat_id] = data
        return

# —————————————————————————————————————————————————————————————
#   28. /change: перевод в режим редактирования меню (только на английском)
# —————————————————————————————————————————————————————————————
@bot.message_handler(commands=['change'])
def cmd_change(message):
    chat_id = message.chat.id
    if chat_id not in user_data:
        user_data[chat_id] = {
            "lang": "ru",
            "cart": [],
            "current_category": None,
            "wait_for_points": False,
            "wait_for_address": False,
            "wait_for_contact": False,
            "wait_for_comment": False,
            "address": "",
            "contact": "",
            "comment": "",
            "pending_discount": 0,
            "pending_points_spent": 0,
            "temp_total_try": 0,
            "temp_user_points": 0,
            "edit_phase": None,
            "edit_cat": None,
            "edit_flavor": None,
            "edit_index": None,
            "edit_cart_phase": None
        }
    data = user_data[chat_id]
    data.update({
        "current_category": None,
        "wait_for_points": False,
        "wait_for_address": False,
        "wait_for_contact": False,
        "wait_for_comment": False,
        "edit_phase": "choose_action",
        "edit_cat": None,
        "edit_flavor": None,
        "edit_index": None,
        "edit_cart_phase": None
    })
    bot.send_message(chat_id, "Menu editing: choose action", reply_markup=edit_action_keyboard())

# —————————————————————————————————————————————————————————————
#   29. /points — показать текущее количество баллов
# —————————————————————————————————————————————————————————————
@bot.message_handler(commands=['points'])
def cmd_points(message):
    chat_id = message.chat.id
    points = get_points(chat_id)
    if points == 0:
        bot.send_message(chat_id, "У вас пока нет бонусных баллов.")
    else:
        bot.send_message(chat_id, f"У вас сейчас {points} бонусных баллов.")

# —————————————————————————————————————————————————————————————
#   30. /addpoints — ручной добавление баллов (только для теста)
# —————————————————————————————————————————————————————————————
@bot.message_handler(commands=['addpoints'])
def cmd_addpoints(message):
    user_id = message.chat.id
    parts = message.text.split()
    if len(parts) != 2 or not parts[1].isdigit():
        bot.send_message(user_id, "Использование: /addpoints <число>")
        return
    delta = int(parts[1])
    add_points(user_id, delta)
    current = get_points(user_id)
    bot.send_message(user_id, f"Добавлено {delta} баллов. Сейчас у вас {current} баллов.")

# —————————————————————————————————————————————————————————————
#   31. /convert — курсы и конвертация суммы TRY
# —————————————————————————————————————————————————————————————
@bot.message_handler(commands=['convert'])
def cmd_convert(message):
    chat_id = message.chat.id
    parts = message.text.split()
    rates = fetch_rates()
    rub = rates.get("RUB", 0)
    usd = rates.get("USD", 0)
    uah = rates.get("UAH", 0)

    if rub == 0 or usd == 0 or uah == 0:
        bot.send_message(chat_id, "Курсы валют сейчас недоступны, попробуйте позже.")
        return

    # Если нет суммы: просто курс
    if len(parts) == 1:
        text = (
            "📊 Курс лиры сейчас:\n"
            f"1₺ = {rub:.2f} ₽\n"
            f"1₺ = {usd:.2f} $\n"
            f"1₺ = {uah:.2f} ₴\n\n"
            "Для пересчёта напишите: /convert 1300"
        )
        bot.send_message(chat_id, text)
        return

    # Если введена сумма
    if len(parts) == 2:
        try:
            amount = float(parts[1].replace(",", "."))
        except Exception:
            bot.send_message(chat_id, "Формат: /convert 1300 (или другую сумму в лирах)")
            return
        res_rub = amount * rub
        res_usd = amount * usd
        res_uah = amount * uah
        text = (
            f"{amount:.2f}₺ = {res_rub:.2f} ₽\n"
            f"{amount:.2f}₺ = {res_usd:.2f} $\n"
            f"{amount:.2f}₺ = {res_uah:.2f} ₴"
        )
        bot.send_message(chat_id, text)
        return

    bot.send_message(chat_id, "Использование: /convert 1300")

# —————————————————————————————————————————————————————————————
#   32. Универсальный хендлер (всё остальное)
# —————————————————————————————————————————————————————————————
@bot.message_handler(content_types=['text','location','venue','contact'])
def universal_handler(message):
    chat_id = message.chat.id
    text = message.text or ""
    if chat_id not in user_data:
        user_data[chat_id] = {
            "lang": "ru",
            "cart": [],
            "current_category": None,
            "wait_for_points": False,
            "wait_for_address": False,
            "wait_for_contact": False,
            "wait_for_comment": False,
            "address": "",
            "contact": "",
            "comment": "",
            "pending_discount": 0,
            "pending_points_spent": 0,
            "temp_total_try": 0,
            "temp_user_points": 0,
            "edit_phase": None,
            "edit_cat": None,
            "edit_flavor": None,
            "edit_index": None,
            "edit_cart_phase": None
        }
    data = user_data[chat_id]

    # ─── Режим редактирования меню (/change) ────────────────────────────────────────
    if data.get('edit_phase'):
        phase = data['edit_phase']

        # 1) Главное меню редактирования (весь текст — английский)
        if phase == 'choose_action':
            if text == "⬅️ Back":
                data['edit_phase'] = None
                data['edit_cat'] = None
                data['edit_flavor'] = None
                bot.send_message(chat_id, "Returned to main menu.", reply_markup=get_inline_main_menu(chat_id))
                user_data[chat_id] = data
                return

            if text == "❌ Cancel":
                data['edit_phase'] = None
                data['edit_cat'] = None
                data['edit_flavor'] = None
                bot.send_message(chat_id, "Menu editing cancelled.", reply_markup=get_inline_main_menu(chat_id))
                user_data[chat_id] = data
                return

            if text == "➕ Add Category":
                data['edit_phase'] = 'add_category'
                kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
                kb.add("⬅️ Back")
                bot.send_message(chat_id, "Enter new category name:", reply_markup=kb)
                user_data[chat_id] = data
                return

            if text == "➖ Remove Category":
                data['edit_phase'] = 'remove_category'
                kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
                for cat_key in menu:
                    kb.add(cat_key)
                kb.add("⬅️ Back")
                bot.send_message(chat_id, "Select category to remove:", reply_markup=kb)
                user_data[chat_id] = data
                return

            if text == "💲 Fix Price":
                data['edit_phase'] = 'choose_fix_price_cat'
                kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
                for cat_key in menu:
                    kb.add(cat_key)
                kb.add("⬅️ Back")
                bot.send_message(chat_id, "Select category to fix price for:", reply_markup=kb)
                user_data[chat_id] = data
                return

            if text == "ALL IN":
                data['edit_phase'] = 'choose_all_in_cat'
                kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
                for cat_key in menu:
                    kb.add(cat_key)
                kb.add("⬅️ Back")
                bot.send_message(chat_id, "Select category to replace full flavor list:", reply_markup=kb)
                user_data[chat_id] = data
                return

            if text == "🔄 Actual Flavor":
                data['edit_phase'] = 'choose_cat_actual'
                kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
                for cat_key in menu:
                    kb.add(cat_key)
                kb.add("⬅️ Back")
                bot.send_message(chat_id, "Select category to update flavor stock:", reply_markup=kb)
                user_data[chat_id] = data
                return

            bot.send_message(chat_id, "Choose action:", reply_markup=edit_action_keyboard())
            return

        # 2) Добавить категорию
        if phase == 'add_category':
            if text == "⬅️ Back":
                data['edit_phase'] = 'choose_action'
                bot.send_message(chat_id, "Back to editing menu:", reply_markup=edit_action_keyboard())
                user_data[chat_id] = data
                return

            new_cat = text.strip()
            if not new_cat or new_cat in menu:
                kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
                kb.add("⬅️ Back")
                bot.send_message(chat_id, "Invalid or existing name. Try again:", reply_markup=kb)
                return

            menu[new_cat] = {
                "price": 1300,
                "flavors": []
            }
            with open(MENU_PATH, "w", encoding="utf-8") as f:
                json.dump(menu, f, ensure_ascii=False, indent=2)

            data['edit_phase'] = 'choose_action'
            bot.send_message(chat_id, f"Category «{new_cat}» added.", reply_markup=edit_action_keyboard())
            user_data[chat_id] = data
            return

        # 3) Удалить категорию
        if phase == 'remove_category':
            if text == "⬅️ Back":
                data['edit_phase'] = 'choose_action'
                bot.send_message(chat_id, "Back to editing menu:", reply_markup=edit_action_keyboard())
                user_data[chat_id] = data
                return

            if text in menu:
                del menu[text]
                with open(MENU_PATH, "w", encoding="utf-8") as f:
                    json.dump(menu, f, ensure_ascii=False, indent=2)
                data['edit_phase'] = 'choose_action'
                bot.send_message(chat_id, f"Category «{text}» removed.", reply_markup=edit_action_keyboard())
                user_data[chat_id] = data
            else:
                kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
                kb.add("⬅️ Back")
                bot.send_message(chat_id, "Select valid category.", reply_markup=kb)
            return

        # 4) Выбрать категорию для Fix Price
        if phase == 'choose_fix_price_cat':
            if text == "⬅️ Back":
                data['edit_phase'] = 'choose_action'
                bot.send_message(chat_id, "Back to editing menu:", reply_markup=edit_action_keyboard())
                user_data[chat_id] = data
                return

            if text in menu:
                data['edit_cat'] = text
                data['edit_phase'] = 'enter_new_price'
                kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
                kb.add("⬅️ Back")
                bot.send_message(chat_id, f"Enter new price in ₺ for category «{text}»:", reply_markup=kb)
                user_data[chat_id] = data
            else:
                kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
                kb.add("⬅️ Back")
                bot.send_message(chat_id, "Choose category from the list.", reply_markup=kb)
            return

        # 5) Ввод новой цены для категории
        if phase == 'enter_new_price':
            if text == "⬅️ Back":
                data.pop('edit_cat', None)
                data['edit_phase'] = 'choose_action'
                bot.send_message(chat_id, "Back to editing menu:", reply_markup=edit_action_keyboard())
                user_data[chat_id] = data
                return

            cat0 = data.get('edit_cat')
            try:
                new_price = float(text.strip())
            except:
                kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
                kb.add("⬅️ Back")
                bot.send_message(chat_id, "Invalid price format. Enter a number, e.g. 1500:", reply_markup=kb)
                return

            menu[cat0]["price"] = int(new_price)
            with open(MENU_PATH, "w", encoding="utf-8") as f:
                json.dump(menu, f, ensure_ascii=False, indent=2)

            bot.send_message(chat_id, f"Price for category «{cat0}» set to {int(new_price)}₺.", reply_markup=edit_action_keyboard())
            data.pop('edit_cat', None)
            data['edit_phase'] = 'choose_action'
            user_data[chat_id] = data
            return

        # 6) Выбрать категорию для ALL IN
        if phase == 'choose_all_in_cat':
            if text == "⬅️ Back":
                data['edit_phase'] = 'choose_action'
                bot.send_message(chat_id, "Back to editing menu:", reply_markup=edit_action_keyboard())
                user_data[chat_id] = data
                return

            if text in menu:
                data['edit_cat'] = text
                current_list = []
                for itm in menu[text]["flavors"]:
                    current_list.append(f"{itm['flavor']} - {itm.get('stock',0)}")
                joined = "\n".join(current_list) if current_list else "(empty)"
                kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
                kb.add("⬅️ Back")
                bot.send_message(
                    chat_id,
                    f"Current flavors in «{text}» (one per line as \"Name - qty\"):\n\n{joined}\n\n"
                    "Send the full updated list in the same format. Each line: “Name - qty”.",
                    reply_markup=kb
                )
                data['edit_phase'] = 'replace_all_in'
                user_data[chat_id] = data
            else:
                kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
                kb.add("⬅️ Back")
                bot.send_message(chat_id, "Choose category from the list.", reply_markup=kb)
            return

        # 7) Заменить полный список вкусов (ALL IN)
        if phase == 'replace_all_in':
            if text == "⬅️ Back":
                data.pop('edit_cat', None)
                data['edit_phase'] = 'choose_action'
                bot.send_message(chat_id, "Back to editing menu:", reply_markup=edit_action_keyboard())
                user_data[chat_id] = data
                return

            cat0 = data.get('edit_cat')
            lines = text.strip().splitlines()
            new_flavors = []
            for line in lines:
                if '-' not in line:
                    continue
                name, qty = map(str.strip, line.rsplit('-', 1))
                if not qty.isdigit() or not name:
                    continue
                new_flavors.append({
                    "emoji": "",
                    "flavor": name,
                    "stock": int(qty)
                })
            menu[cat0]["flavors"] = new_flavors
            with open(MENU_PATH, "w", encoding="utf-8") as f:
                json.dump(menu, f, ensure_ascii=False, indent=2)

            bot.send_message(chat_id, f"Full flavor list for «{cat0}» replaced.", reply_markup=edit_action_keyboard())
            data.pop('edit_cat', None)
            data['edit_phase'] = 'choose_action'
            user_data[chat_id] = data
            return

        # 8) Выбрать категорию для Actual Flavor
        if phase == 'choose_cat_actual':
            if text == "⬅️ Back":
                data['edit_phase'] = 'choose_action'
                bot.send_message(chat_id, "Back to editing menu:", reply_markup=edit_action_keyboard())
                user_data[chat_id] = data
                return

            if text in menu:
                data['edit_cat'] = text
                data['edit_phase'] = 'choose_flavor_actual'
                kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
                for itm in menu[text]["flavors"]:
                    flavor0 = itm["flavor"]
                    stock0 = itm.get("stock", 0)
                    kb.add(f"{flavor0} [{stock0} шт]")
                kb.add("⬅️ Back")
                bot.send_message(chat_id, "Select flavor to update stock:", reply_markup=kb)
                user_data[chat_id] = data
            else:
                kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
                kb.add("⬅️ Back")
                bot.send_message(chat_id, "Choose category from the list.", reply_markup=kb)
            return

        # 9) Выбрать вкус для Actual Flavor
        if phase == 'choose_flavor_actual':
            if text == "⬅️ Back":
                data.pop('edit_cat', None)
                data['edit_phase'] = 'choose_action'
                bot.send_message(chat_id, "Back to editing menu:", reply_markup=edit_action_keyboard())
                user_data[chat_id] = data
                return

            cat0 = data.get('edit_cat')
            flavor_name = text.split(' [')[0]
            exists = any(i["flavor"] == flavor_name for i in menu.get(cat0, {}).get("flavors", []))
            if exists:
                data['edit_flavor'] = flavor_name
                data['edit_phase'] = 'enter_actual_qty'
                kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
                kb.add("⬅️ Back")
                bot.send_message(chat_id, "Enter new stock quantity:", reply_markup=kb)
                user_data[chat_id] = data
            else:
                bot.send_message(chat_id, "Flavor not found. Choose again:", reply_markup=edit_action_keyboard())
                data['edit_phase'] = 'choose_action'
                user_data[chat_id] = data
            return

        # 10) Ввод актуального количества для Actual Flavor
        if phase == 'enter_actual_qty':
            if text == "⬅️ Back":
                data.pop('edit_flavor', None)
                data['edit_phase'] = 'choose_action'
                bot.send_message(chat_id, "Back to editing menu:", reply_markup=edit_action_keyboard())
                user_data[chat_id] = data
                return

            cat0 = data.get('edit_cat')
            flavor0 = data.get('edit_flavor')
            if not text.isdigit():
                kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
                kb.add("⬅️ Back")
                bot.send_message(chat_id, "Please enter a valid number!", reply_markup=kb)
                return

            new_stock = int(text)
            for itm in menu[cat0]["flavors"]:
                if itm["flavor"] == flavor0:
                    itm["stock"] = new_stock
                    break
            with open(MENU_PATH, "w", encoding="utf-8") as f:
                json.dump(menu, f, ensure_ascii=False, indent=2)

            bot.send_message(chat_id, f"Stock for flavor «{flavor0}» in category «{cat0}» set to {new_stock}.", reply_markup=edit_action_keyboard())
            data.pop('edit_cat', None)
            data.pop('edit_flavor', None)
            data['edit_phase'] = 'choose_action'
            user_data[chat_id] = data
            return

        data['edit_phase'] = 'choose_action'
        bot.send_message(chat_id, "Back to editing menu:", reply_markup=edit_action_keyboard())
        user_data[chat_id] = data
        return
    # ────────────────────────────────────────────────────────────────────────────────

    # ——— Режим редактирования корзины ———
    if data.get('edit_cart_phase'):
        if data['edit_cart_phase'] == 'choose_action':
            if text == t(chat_id, "back"):
                data['edit_cart_phase'] = None
                data['edit_index'] = None
                bot.send_message(chat_id, t(chat_id, "choose_category"), reply_markup=get_inline_main_menu(chat_id))
                user_data[chat_id] = data
                return

            if text.startswith(f"{t(chat_id, 'remove_item')} "):
                try:
                    idx = int(text.split()[1]) - 1
                except:
                    bot.send_message(chat_id, t(chat_id, "error_invalid"), reply_markup=get_inline_main_menu(chat_id))
                    data['edit_cart_phase'] = None
                    data['edit_index'] = None
                    user_data[chat_id] = data
                    return
                grouped = {}
                for item in data['cart']:
                    key = (item['category'], item['flavor'], item['price'])
                    grouped[key] = grouped.get(key, 0) + 1
                items_list = list(grouped.items())
                if idx < 0 or idx >= len(items_list):
                    bot.send_message(chat_id, t(chat_id, "error_invalid"), reply_markup=get_inline_main_menu(chat_id))
                    data['edit_cart_phase'] = None
                    user_data[chat_id] = data
                    return
                key_to_remove, _ = items_list[idx]
                new_cart = [
                    it for it in data['cart']
                    if not (it['category'] == key_to_remove[0] and it['flavor'] == key_to_remove[1] and it['price'] == key_to_remove[2])
                ]
                data['cart'] = new_cart
                data['edit_cart_phase'] = None
                data['edit_index'] = None
                bot.send_message(chat_id, t(chat_id, "item_removed").format(flavor=key_to_remove[1]), reply_markup=get_inline_main_menu(chat_id))
                user_data[chat_id] = data
                return

            if text.startswith(f"{t(chat_id, 'edit_item')} "):
                try:
                    idx = int(text.split()[1]) - 1
                except:
                    bot.send_message(chat_id, t(chat_id, "error_invalid"), reply_markup=get_inline_main_menu(chat_id))
                    data['edit_cart_phase'] = None
                    user_data[chat_id] = data
                    return
                grouped = {}
                for item in data['cart']:
                    key = (item['category'], item['flavor'], item['price'])
                    grouped[key] = grouped.get(key, 0) + 1
                items_list = list(grouped.items())
                if idx < 0 or idx >= len(items_list):
                    bot.send_message(chat_id, t(chat_id, "error_invalid"), reply_markup=get_inline_main_menu(chat_id))
                    data['edit_cart_phase'] = None
                    user_data[chat_id] = data
                    return
                data['edit_index'] = idx
                data['edit_cart_phase'] = 'enter_qty'
                key_chosen, count = items_list[idx]
                cat0, flavor0, price0 = key_chosen
                bot.send_message(
                    chat_id,
                    f"Текущий товар: {cat0} — {flavor0} — {price0}₺ (в корзине {count} шт).\n{t(chat_id, 'enter_new_qty')}"
                )
                user_data[chat_id] = data
                return

        if data['edit_cart_phase'] == 'enter_qty':
            if text == t(chat_id, "back"):
                data['edit_cart_phase'] = None
                data['edit_index'] = None
                bot.send_message(chat_id, t(chat_id, "choose_category"), reply_markup=get_inline_main_menu(chat_id))
                user_data[chat_id] = data
                return
            if not text.isdigit():
                bot.send_message(chat_id, t(chat_id, "error_invalid"))
                return
            new_qty = int(text)
            grouped = {}
            for item in data['cart']:
                key = (item['category'], item['flavor'], item['price'])
                grouped[key] = grouped.get(key, 0) + 1
            items_list = list(grouped.items())
            idx = data['edit_index']
            if idx < 0 or idx >= len(items_list):
                bot.send_message(chat_id, t(chat_id, "error_invalid"), reply_markup=get_inline_main_menu(chat_id))
                data['edit_cart_phase'] = None
                data['edit_index'] = None
                user_data[chat_id] = data
                return
            key_chosen, old_count = items_list[idx]
            cat0, flavor0, price0 = key_chosen

            data['cart'] = [
                it for it in data['cart']
                if not (it['category'] == cat0 and it['flavor'] == flavor0 and it['price'] == price0)
            ]
            for _ in range(new_qty):
                data['cart'].append({'category': cat0, 'flavor': flavor0, 'price': price0})

            data['edit_cart_phase'] = None
            data['edit_index'] = None
            if new_qty == 0:
                bot.send_message(chat_id, t(chat_id, "item_removed").format(flavor=flavor0), reply_markup=get_inline_main_menu(chat_id))
            else:
                bot.send_message(chat_id, t(chat_id, "qty_changed").format(flavor=flavor0, qty=new_qty), reply_markup=get_inline_main_menu(chat_id))
            user_data[chat_id] = data
            return

    # ——— Обработка кнопки «Корзина» через Reply-кнопку ———
    if text.startswith(f"{t(chat_id, 'remove_item')} "):
        try:
            idx = int(text.split()[1]) - 1
        except:
            bot.send_message(chat_id, t(chat_id, "error_invalid"), reply_markup=get_inline_main_menu(chat_id))
            data['edit_cart_phase'] = None
            data['edit_index'] = None
            user_data[chat_id] = data
            return

        grouped = {}
        for item in data['cart']:
            key = (item['category'], item['flavor'], item['price'])
            grouped[key] = grouped.get(key, 0) + 1
        items_list = list(grouped.items())
        if idx < 0 or idx >= len(items_list):
            bot.send_message(chat_id, t(chat_id, "error_invalid"), reply_markup=get_inline_main_menu(chat_id))
            data['edit_cart_phase'] = None
            user_data[chat_id] = data
            return

        (cat0, flavor0, price0), _ = items_list[idx]

        removed = False
        new_cart = []
        for it in data["cart"]:
            if not removed and it["category"] == cat0 and it["flavor"] == flavor0 and it["price"] == price0:
                removed = True
                continue
            new_cart.append(it)
        data['cart'] = new_cart

        data['edit_cart_phase'] = None
        data['edit_index'] = None
        bot.send_message(chat_id, t(chat_id, "item_removed").format(flavor=flavor0), reply_markup=get_inline_main_menu(chat_id))
        user_data[chat_id] = data
        return

    # ——— Если ожидаем ввод адреса ———
    if data.get('wait_for_address'):
        if text == t(chat_id, "back"):
            data['wait_for_address'] = False
            data['current_category'] = None
            bot.send_message(chat_id, t(chat_id, "choose_category"), reply_markup=get_inline_main_menu(chat_id))
            user_data[chat_id] = data
            return

        if text == t(None, "choose_on_map"):
            bot.send_message(
                chat_id,
                "Чтобы выбрать точку:\n📎 → Местоположение → «Выбрать на карте» → метка → Отправить",
                reply_markup=types.ReplyKeyboardRemove()
            )
            return

        if message.content_type == 'venue' and message.venue:
            v = message.venue
            address = f"{v.title}, {v.address}\n🌍 https://maps.google.com/?q={v.location.latitude},{v.location.longitude}"
        elif message.content_type == 'location' and message.location:
            lat, lon = message.location.latitude, message.location.longitude
            address = f"🌍 https://maps.google.com/?q={lat},{lon}"
        elif text == t(None, "enter_address_text"):
            bot.send_message(chat_id, t(chat_id, "enter_address"), reply_markup=types.ReplyKeyboardRemove())
            return
        elif message.content_type == 'text' and message.text:
            address = message.text.strip()
        else:
            bot.send_message(chat_id, t(chat_id, "error_invalid"), reply_markup=address_keyboard())
            return

        data['address'] = address
        data['wait_for_address'] = False
        data['wait_for_contact'] = True
        kb = contact_keyboard()
        bot.send_message(chat_id, t(chat_id, "enter_contact"), reply_markup=kb)
        user_data[chat_id] = data
        return

    # ——— Если ожидаем ввод контакта ———
    if data.get('wait_for_contact'):
        if text == t(chat_id, "back"):
            data['wait_for_address'] = True
            data['wait_for_contact'] = False
            bot.send_message(chat_id, t(chat_id, "enter_address"), reply_markup=address_keyboard())
            user_data[chat_id] = data
            return

        if text == t(None, "enter_nickname"):
            bot.send_message(chat_id, "Введите ваш Telegram-ник (без @):", reply_markup=types.ReplyKeyboardRemove())
            return

        if message.content_type == 'contact' and message.contact:
            contact = message.contact.phone_number
        elif message.content_type == 'text' and message.text:
            contact = "@" + message.text.strip().lstrip("@")
        else:
            bot.send_message(chat_id, t(chat_id, "enter_contact"), reply_markup=contact_keyboard())
            return

        data['contact'] = contact
        data['wait_for_contact'] = False
        data['wait_for_comment'] = True
        kb = comment_keyboard()
        bot.send_message(chat_id, t(chat_id, "enter_comment"), reply_markup=kb)
        user_data[chat_id] = data
        return

    # ——— Если ожидаем ввод комментария ———
    if data.get('wait_for_comment'):
        if text == t(chat_id, "back"):
            data['wait_for_contact'] = True
            data['wait_for_comment'] = False
            bot.send_message(chat_id, t(chat_id, "enter_contact"), reply_markup=contact_keyboard())
            user_data[chat_id] = data
            return

        if text == t(None, "enter_comment"):
            bot.send_message(chat_id, t(chat_id, "enter_comment"), reply_markup=types.ReplyKeyboardRemove())
            return

        if message.content_type == 'text' and text != t(None, "send_order"):
            data['comment'] = text.strip()
            bot.send_message(chat_id, t(chat_id, "comment_saved"), reply_markup=comment_keyboard())
            user_data[chat_id] = data
            return

        if text == t(None, "send_order"):
            # Повторяем логику из handle_comment_input (п.27)
            cart = data.get('cart', [])
            if not cart:
                bot.send_message(chat_id, t(chat_id, "cart_empty"))
                return

            total_try = sum(i['price'] for i in cart)
            discount = data.pop("pending_discount", 0)
            total_after = max(total_try - discount, 0)

            # Проверяем наличие stock
            needed = {}
            for it in cart:
                key = (it["category"], it["flavor"])
                needed[key] = needed.get(key, 0) + 1

            for (cat0, flavor0), qty_needed in needed.items():
                item_obj = next((i for i in menu[cat0]["flavors"] if i["flavor"] == flavor0), None)
                if not item_obj or item_obj.get("stock", 0) < qty_needed:
                    bot.send_message(chat_id, f"😕 К сожалению, «{flavor0}» больше не доступен.")
                    return

            # Если всё ок, уменьшаем stock
            for (cat0, flavor0), qty_needed in needed.items():
                for itm in menu[cat0]["flavors"]:
                    if itm["flavor"] == flavor0:
                        itm["stock"] -= qty_needed
                        break
            with open(MENU_PATH, "w", encoding="utf-8") as f:
                json.dump(menu, f, ensure_ascii=False, indent=2)

            # Сохраняем заказ
            items_json = json.dumps(cart, ensure_ascii=False)
            now = datetime.datetime.utcnow()
            conn = get_connection()
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO orders (chat_id, items_json, total, timestamp) VALUES (%s, %s, %s, %s);",
                (chat_id, items_json, total_after, now)
            )
            conn.commit()

            # Начисляем бонусные баллы за заказ
            earned = total_after // 30
            if earned > 0:
                add_points(chat_id, earned)
                bot.send_message(chat_id, f"👍 Вы получили {earned} бонусных баллов за этот заказ.")

            # Бонус за приглашение
            cur.execute("SELECT referred_by FROM users WHERE chat_id = %s;", (chat_id,))
            row = cur.fetchone()
            if row and row[0]:
                inviter = row[0]
                add_points(inviter, 200)
                bot.send_message(inviter, "🎉 Вам начислено 200 бонусных баллов за приглашение нового клиента!")
                cur.execute("UPDATE users SET referred_by = NULL WHERE chat_id = %s;", (chat_id,))
                conn.commit()

            cur.close()
            conn.close()

            # Отправляем админам (русская версия)
            summary_rus = "\n".join(f"{i['category']}: {i['flavor']} — {i['price']}₺" for i in cart)
            rates = fetch_rates()
            rub = round(total_after * rates.get("RUB", 0) + 500, 2)
            usd = round(total_after * rates.get("USD", 0) + 2, 2)
            uah = round(total_after * rates.get("UAH", 0) + 200, 2)
            conv = f"({rub}₽, ${usd}, ₴{uah})"

            full_rus = (
                f"📥 Новый заказ от @{message.from_user.username or message.from_user.first_name}:\n\n"
                f"{summary_rus}\n\n"
                f"Итог: {total_after}₺ {conv}\n"
                f"📍 Адрес: {data.get('address', '—')}\n"
                f"📱 Контакт: {data.get('contact', '—')}\n"
                f"💬 Комментарий: {data.get('comment', '—')}"
            )
            bot.send_message(PERSONAL_CHAT_ID, full_rus)

            # Отправляем в группу – английская версия
            summary_en = "\n".join(f"{i['category']}: {i['flavor']} — {i['price']}₺" for i in cart)
            comment_ru = data.get('comment', '')
            comment_en = translate_to_en(comment_ru) if comment_ru else "—"
            full_en = (
                f"📥 New order from @{message.from_user.username or message.from_user.first_name}:\n\n"
                f"{summary_en}\n\n"
                f"Total: {total_after}₺ {conv}\n"
                f"📍 Address: {data.get('address', '—')}\n"
                f"📱 Contact: {data.get('contact', '—')}\n"
                f"💬 Comment: {comment_en}"
            )
            bot.send_message(GROUP_CHAT_ID, full_en)

            # Кнопка «➕ Добавить ещё»
            bot.send_message(
                chat_id,
                t(chat_id, "order_accepted"),
                reply_markup=types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
                                 .add(f"➕ {t(chat_id, 'add_more')}")
            )

            # Очищаем корзину
            data.update({
                "cart": [],
                "current_category": None,
                "wait_for_address": False,
                "wait_for_contact": False,
                "wait_for_comment": False
            })
            user_data[chat_id] = data
            return

    # ——— Кнопка «⬅️ Назад» во всём остальном ———
    if text == t(chat_id, "back"):
        data.update({
            "current_category": None,
            "wait_for_points": False,
            "wait_for_address": False,
            "wait_for_contact": False,
            "wait_for_comment": False
        })
        bot.send_message(chat_id, t(chat_id, "choose_category"), reply_markup=get_inline_main_menu(chat_id))
        user_data[chat_id] = data
        return

    # ——— Очистка корзины (Reply-кнопка) ———
    if text == f"🗑️ {t(chat_id, 'clear_cart')}":
        data["cart"] = []
        data["current_category"] = None
        data["wait_for_points"] = False
        data["wait_for_address"] = False
        data["wait_for_contact"] = False
        data["wait_for_comment"] = False
        bot.send_message(chat_id, t(chat_id, "cart_cleared"), reply_markup=get_inline_main_menu(chat_id))
        user_data[chat_id] = data
        return

    # ——— Кнопка «➕ Добавить ещё» ———
    if text == f"➕ {t(chat_id, 'add_more')}":
        data["current_category"] = None
        bot.send_message(chat_id, t(chat_id, "choose_category"), reply_markup=get_inline_main_menu(chat_id))
        user_data[chat_id] = data
        return

    # ——— Завершить заказ по Reply-кнопке ———
    if text == f"✅ {t(chat_id, 'finish_order')}":
        cart = data.get("cart", [])
        if not cart:
            bot.send_message(chat_id, t(chat_id, "cart_empty"))
            return
        total_try = sum(i['price'] for i in cart)
        user_points = get_points(chat_id)

        if user_points > 0:
            max_points = min(user_points, total_try)
            points_try = user_points
            msg = (
                t(chat_id, "points_info").format(points=user_points, points_try=points_try)
                + "\n" + t(chat_id, "enter_points").format(max_points=max_points)
            )
            bot.send_message(chat_id, msg, reply_markup=types.ReplyKeyboardRemove())
            data["wait_for_points"] = True
            data["temp_total_try"] = total_try
            data["temp_user_points"] = user_points
            user_data[chat_id] = data
        else:
            kb = address_keyboard()
            bot.send_message(
                chat_id,
                f"🛒 {t(chat_id, 'view_cart')}:\n\n" +
                "\n".join(f"{i['category']}: {i['flavor']} — {i['price']}₺" for i in data['cart']) +
                f"\n\n{t(chat_id, 'enter_address')}",
                reply_markup=kb
            )
            data["wait_for_address"] = True
            user_data[chat_id] = data
        return

    # ——— Выбор категории (Reply-клавиатура fallback) ———
    if text in menu:
        data['current_category'] = text
        bot.send_message(chat_id, f"{t(chat_id, 'choose_flavor')} «{text}»", reply_markup=get_inline_flavors(chat_id, text))
        user_data[chat_id] = data
        return

    # ——— Выбор вкуса (Reply-клавиатура fallback) ———
    cat0 = data.get('current_category')
    if cat0:
        price = menu[cat0]["price"]
        for it in menu[cat0]["flavors"]:
            if it.get("stock", 0) > 0:
                emoji = it.get("emoji", "")
                flavor0 = it["flavor"]
                label = f"{emoji} {flavor0} ({price}₺) [{it['stock']} шт]"
                if text == label:
                    data['cart'].append({'category': cat0, 'flavor': flavor0, 'price': price})
                    template = t(chat_id, "added_to_cart")
                    suffix = template.split("»", 1)[1].strip()
                    count = len(data['cart'])
                    bot.send_message(
                        chat_id,
                        f"«{cat0} — {flavor0}» {suffix.format(flavor=flavor0, count=count)}",
                        reply_markup=get_inline_main_menu(chat_id)
                    )
                    user_data[chat_id] = data
                    return
        bot.send_message(chat_id, t(chat_id, "error_invalid"), reply_markup=get_inline_flavors(chat_id, cat0))
        return

    # ——— /history ———
    if text == "/history":
        conn = get_connection()
        cur = conn.cursor()
        cur.execute("SELECT order_id, items_json, total, timestamp FROM orders WHERE chat_id = %s ORDER BY timestamp DESC;", (chat_id,))
        rows = cur.fetchall()
        if not rows:
            bot.send_message(chat_id, "У вас пока нет сохранённых заказов.")
            cur.close()
            conn.close()
            return
        texts = []
        for order_id, items_json, total, timestamp in rows[:10]:
            items = json.loads(items_json)
            summary = "\n".join(f"{i['flavor']} — {i['price']}₺" for i in items)
            date = timestamp.date().isoformat()
            texts.append(f"Заказ #{order_id} ({date}):\n{summary}\nИтого: {total}₺")
        bot.send_message(chat_id, "\n\n".join(texts))
        cur.close()
        conn.close()
        return

    # ——— /review ———
    if text.startswith("/review"):
        parts = text.split(maxsplit=2)
        if len(parts) < 3:
            bot.send_message(chat_id, "Использование: /review <название_вкуса> <оценка(1–5)> <комментарий>")
            return
        _, flavor_query, rest = parts
        parts2 = rest.split(maxsplit=1)
        if len(parts2) < 2 or not parts2[0].isdigit():
            bot.send_message(chat_id, "Пожалуйста, укажите оценку (число 1–5) и комментарий.")
            return
        rating = int(parts2[0])
        comment = parts2[1]
        if rating < 1 or rating > 5:
            bot.send_message(chat_id, "Оценка должна быть от 1 до 5.")
            return

        found = False
        db_cat = None
        db_flavor = None
        for cat_key, cat_data in menu.items():
            for itm in cat_data["flavors"]:
                if itm["flavor"].lower() == flavor_query.lower():
                    found = True
                    db_cat = cat_key
                    db_flavor = itm["flavor"]
                    break
            if found:
                break
        if not found:
            bot.send_message(chat_id, "Вкус не найден. Убедитесь в корректном написании.")
            return

        now = datetime.datetime.utcnow()
        conn = get_connection()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO reviews (chat_id, category, flavor, rating, comment, timestamp) VALUES (%s, %s, %s, %s, %s, %s);",
            (chat_id, db_cat, db_flavor, rating, comment, now)
        )
        conn.commit()

        cur.execute("SELECT AVG(rating) FROM reviews WHERE flavor = %s;", (db_flavor,))
        avg_rating = cur.fetchone()[0] or 0

        # Обновляем рейтинг внутри menu.json для этого вкуса (чтобы отображался среднего)
        for itm in menu[db_cat]["flavors"]:
            if itm["flavor"] == db_flavor:
                itm["rating"] = round(avg_rating, 1)
                break
        with open(MENU_PATH, "w", encoding="utf-8") as f:
            json.dump(menu, f, ensure_ascii=False, indent=2)

        bot.send_message(chat_id, f"Спасибо за отзыв! Текущий средний рейтинг «{db_flavor}»: {avg_rating:.1f}")

        cur.close()
        conn.close()
        return

    # ——— /show_reviews ———
    if text.startswith("/show_reviews"):
        parts = text.split(maxsplit=1)
        if len(parts) != 2:
            bot.send_message(chat_id, "Использование: /show_reviews <название_вкуса>")
            return
        flavor_query = parts[1]
        conn = get_connection()
        cur = conn.cursor()
        cur.execute(
            "SELECT chat_id, rating, comment, timestamp FROM reviews WHERE flavor = %s ORDER BY timestamp DESC;",
            (flavor_query,)
        )
        rows = cur.fetchall()
        if not rows:
            bot.send_message(chat_id, "Пока нет отзывов для этого вкуса.")
            cur.close()
            conn.close()
            return
        texts = []
        for uid, rating, comment, ts in rows[:10]:
            date = ts.date().isoformat()
            texts.append(f"👤 {uid} [{rating}⭐]\n🕒 {date}\n«{comment}»")
        bot.send_message(chat_id, "\n\n".join(texts))
        cur.close()
        conn.close()
        return

    # ——— /stats ———
    if text == "/stats":
        if chat_id != ADMIN_ID:
            bot.send_message(chat_id, "У вас нет доступа к этой команде.")
            return
        conn = get_connection()
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM orders;")
        total_orders = cur.fetchone()[0]
        cur.execute("SELECT SUM(total) FROM orders;")
        total_revenue = cur.fetchone()[0] or 0
        cur.execute("SELECT items_json FROM orders;")
        all_items = cur.fetchall()
        counts = {}
        for (items_json,) in all_items:
            items = json.loads(items_json)
            for i in items:
                key = i["flavor"]
                counts[key] = counts.get(key, 0) + 1
        top5 = sorted(counts.items(), key=lambda x: x[1], reverse=True)[:5]
        top5_lines = [f"{flavor}: {qty} шт." for flavor, qty in top5] or ["Пока нет данных."]
        report = (
            f"📊 Статистика магазина:\n"
            f"Всего заказов: {total_orders}\n"
            f"Общая выручка: {total_revenue}₺\n\n"
            f"Топ-5 продаваемых вкусов:\n" + "\n".join(top5_lines)
        )
        bot.send_message(chat_id, report)
        cur.close()
        conn.close()
        return

# —————————————————————————————————————————————————————————————
#   33. Запуск бота
# —————————————————————————————————————————————————————————————
if __name__ == "__main__":
    # Сбрасываем возможный webhook, чтобы не было конфликта
    bot.delete_webhook()
    bot.polling(none_stop=True)
