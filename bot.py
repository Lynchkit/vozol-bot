# -*- coding: utf-8 -*-
import os
import json
import requests
import sqlite3
import datetime
import random
import string

from apscheduler.schedulers.background import BackgroundScheduler
import telebot
from telebot import types

# ------------------------------------------------------------------------
#   1. Загрузка переменных окружения
# ------------------------------------------------------------------------
TOKEN = os.getenv("TOKEN")
if not TOKEN:
    raise RuntimeError(
        "Environment variable TOKEN is not set! "
        "Run the container with -e TOKEN=<your_token>."
    )
ADMIN_ID = int(os.getenv("ADMIN_ID", "424751188"))
ADMIN_ID_TWO = int(os.getenv("ADMIN_ID_TWO", "748250885"))
GROUP_CHAT_ID = int(os.getenv("GROUP_CHAT_ID", "0"))
PERSONAL_CHAT_ID = int(os.getenv("PERSONAL_CHAT_ID", "0"))

bot = telebot.TeleBot(TOKEN, parse_mode="HTML")

# ------------------------------------------------------------------------
#   2. Пути к JSON-файлам и БД (персистентный том /data)
# ------------------------------------------------------------------------
MENU_PATH = "/data/menu.json"
LANG_PATH = "/data/languages.json"
DB_PATH = "/data/database.db"


# ------------------------------------------------------------------------
#   3. Функция для получения локального подключения к БД
# ------------------------------------------------------------------------
def get_db_connection():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    return conn


# ------------------------------------------------------------------------
# ------------------------------------------------------------------------
#   4. Инициализация SQLite и создание таблиц (при старте)
# ------------------------------------------------------------------------
import sqlite3

conn_init = get_db_connection()
cursor_init = conn_init.cursor()

# Попытка добавить новые столбцы — выполнится только один раз
try:
    cursor_init.execute("ALTER TABLE orders ADD COLUMN points_spent  INTEGER DEFAULT 0")
    cursor_init.execute("ALTER TABLE orders ADD COLUMN points_earned INTEGER DEFAULT 0")
    conn_init.commit()
except sqlite3.OperationalError:
    # Если столбцы уже существуют — просто пропускаем
    pass

# Создание таблицы users
cursor_init.execute("""
    CREATE TABLE IF NOT EXISTS users (
        chat_id        INTEGER PRIMARY KEY,
        points         INTEGER DEFAULT 0,
        referral_code  TEXT UNIQUE,
        referred_by    INTEGER
    )
""")

# Создание таблицы orders (с новыми полями уже учтёнными через ALTER)
cursor_init.execute("""
    CREATE TABLE IF NOT EXISTS orders (
        order_id       INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id        INTEGER,
        items_json     TEXT,
        total          INTEGER,
        timestamp      TEXT,
        points_spent   INTEGER DEFAULT 0,
        points_earned  INTEGER DEFAULT 0
    )
""")

# Создание таблицы reviews
cursor_init.execute("""
    CREATE TABLE IF NOT EXISTS reviews (
        review_id   INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id     INTEGER,
        category    TEXT,
        flavor      TEXT,
        rating      INTEGER,
        comment     TEXT,
        timestamp   TEXT
    )
""")

conn_init.commit()
cursor_init.close()
conn_init.close()



# ------------------------------------------------------------------------
#   5. Загрузка menu.json и languages.json
# ------------------------------------------------------------------------
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


# 0. Убедимся, что у пользователя всегда есть запись в user_data, новое добавленное
def init_user(chat_id: int):
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
            "edit_cart_phase": None,
            "awaiting_review_flavor": None,
            "awaiting_review_rating": False,
            "awaiting_review_comment": False,
            "temp_review_flavor": None,
            "temp_review_rating": 0
        }


# ------------------------------------------------------------------------
#   6. Хранилище данных пользователей (in-memory)
# ------------------------------------------------------------------------
user_data = {}  # структура объяснялась ранее


# 6.2 Декоратор для гарантированной инициализации
def ensure_user(handler):
    def wrapper(message_or_call, *args, **kwargs):
        # для Message и CallbackQuery chat_id берём по-разному:
        if hasattr(message_or_call, "from_user"):
            cid = message_or_call.from_user.id
        else:
            cid = message_or_call.chat.id
        init_user(cid)
        return handler(message_or_call, *args, **kwargs)

    return wrapper


# ------------------------------------------------------------------------
#   7. Утилиты
# ------------------------------------------------------------------------
def t(chat_id: int, key: str) -> str:
    """
    Возвращает перевод из languages.json по ключу.
    Если перевод не найден — возвращает сам ключ.
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


# ------------------------------------------------------------------------
#   8. Inline-кнопки для выбора языка
# ------------------------------------------------------------------------
def get_inline_language_buttons(chat_id: int) -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        types.InlineKeyboardButton(text="Русский 🇷🇺", callback_data="set_lang|ru"),
        types.InlineKeyboardButton(text="English 🇬🇧", callback_data="set_lang|en")
    )
    return kb


# ------------------------------------------------------------------------
#   9. Inline-кнопки для главного меню
# ------------------------------------------------------------------------
def get_inline_main_menu(chat_id: int) -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup(row_width=2)
    lang = user_data.get(chat_id, {}).get("lang") or "ru"
    for cat in menu.keys():
        total_stock = sum(item.get("stock", 0) for item in menu[cat]["flavors"])
        if total_stock == 0:
            if lang == "en":
                label = f"{cat} (out of stock)"
            else:
                label = f"{cat} (нет в наличии)"
        else:
            label = cat
        kb.add(types.InlineKeyboardButton(text=label, callback_data=f"category|{cat}"))

    kb.add(types.InlineKeyboardButton(text=f"🛒 {t(chat_id, 'view_cart')}", callback_data="view_cart"))
    kb.add(types.InlineKeyboardButton(text=f"🗑️ {t(chat_id, 'clear_cart')}", callback_data="clear_cart"))
    kb.add(types.InlineKeyboardButton(text=f"✅ {t(chat_id, 'finish_order')}", callback_data="finish_order"))
    return kb


# ------------------------------------------------------------------------
#   10. Inline-кнопки для выбора вкусов
# ------------------------------------------------------------------------
def get_inline_flavors(chat_id: int, cat: str) -> types.InlineKeyboardMarkup:
    # === ОТЛАДКА ===
    print(f"DEBUG: вызван get_inline_flavors для категории '{cat}'")
    print(f"DEBUG: menu['{cat}']['flavors'] = {menu[cat]['flavors']}")
    # ================

    kb = types.InlineKeyboardMarkup(row_width=1)
    price = menu[cat]["price"]

    for item in menu[cat]["flavors"]:
        stock = item.get("stock", 0)
        # Если stock хранится как строка, но состоит из цифр — приводим к int
        if isinstance(stock, str) and stock.isdigit():
            stock = int(stock)
            item["stock"] = stock

        if isinstance(stock, int) and stock > 0:
            emoji = item.get("emoji", "")
            flavor_name = item["flavor"]
            label = f"{emoji} {flavor_name} - {price}₺ [{stock}шт]"
            kb.add(types.InlineKeyboardButton(text=label, callback_data=f"flavor|{cat}|{flavor_name}"))

    kb.add(types.InlineKeyboardButton(text=f"⬅️ {t(chat_id, 'back_to_categories')}",
                                      callback_data="go_back_to_categories"))
    return kb


# ------------------------------------------------------------------------
#   11. Reply-клавиатуры (альтернатива inline)
# ------------------------------------------------------------------------
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


# ------------------------------------------------------------------------
#   12. Клавиатура редактирования меню (/change) — ВСЁ НА АНГЛИЙСКОМ
# ------------------------------------------------------------------------
def edit_action_keyboard() -> types.ReplyKeyboardMarkup:
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
    kb.add("➕ Add Category", "➖ Remove Category")
    kb.add("💲 Fix Price", "ALL IN", "🔄 Actual Flavor")
    kb.add("🖼️ Add Category Picture", "Set Category Flavor to 0")
    kb.add("⬅️ Back", "❌ Cancel")
    return kb


# ------------------------------------------------------------------------
#   13. Планировщик – еженедельный дайджест (необязательно)
# ------------------------------------------------------------------------
def send_weekly_digest():
    conn_local = get_db_connection()
    cursor_local = conn_local.cursor()

    one_week_ago = (datetime.datetime.utcnow() - datetime.timedelta(days=7)).isoformat()
    cursor_local.execute("SELECT items_json FROM orders WHERE timestamp >= ?", (one_week_ago,))
    recent = cursor_local.fetchall()
    counts = {}
    for (items_json,) in recent:
        items = json.loads(items_json)
        for i in items:
            counts[i["flavor"]] = counts.get(i["flavor"], 0) + 1
    top3 = sorted(counts.items(), key=lambda x: x[1], reverse=True)[:3]
    if not top3:
        text = "📢 No sales in the past week."
    else:
        lines = [f"{flavor}: {qty} sold" for flavor, qty in top3]
        text = "📢 Top-3 flavors this week:\n" + "\n".join(lines)

    cursor_local.execute("SELECT DISTINCT chat_id FROM orders")
    users = cursor_local.fetchall()
    for (uid,) in users:
        bot.send_message(uid, text)

    cursor_local.close()
    conn_local.close()


scheduler = BackgroundScheduler(timezone="Europe/Riga")
scheduler.add_job(send_weekly_digest, trigger="cron", day_of_week="mon", hour=9, minute=0)
scheduler.start()


# ------------------------------------------------------------------------
#   14. Хендлер /start – регистрация, реферальная система, выбор языка
# ------------------------------------------------------------------------
@ensure_user
@bot.message_handler(commands=['start'])
def cmd_start(message):
    chat_id = message.chat.id

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
            "edit_cart_phase": None,
            "awaiting_review_flavor": None,
            "awaiting_review_rating": False,
            "awaiting_review_comment": False,
            "temp_review_flavor": None,
            "temp_review_rating": 0
        }
    data = user_data[chat_id]

    # Сбрасываем всё, кроме lang
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
        "edit_cart_phase": None,
        "awaiting_review_flavor": None,
        "awaiting_review_rating": False,
        "awaiting_review_comment": False,
        "temp_review_flavor": None,
        "temp_review_rating": 0
    })

    conn_local = get_db_connection()
    cursor_local = conn_local.cursor()
    cursor_local.execute("SELECT chat_id FROM users WHERE chat_id = ?", (chat_id,))
    if cursor_local.fetchone() is None:
        text = message.text or ""
        referred_by = None
        if "ref=" in text:
            code = text.split("ref=")[1]
            cursor_local.execute("SELECT chat_id FROM users WHERE referral_code = ?", (code,))
            row = cursor_local.fetchone()
            if row:
                referred_by = row[0]
        new_code = generate_ref_code()
        while True:
            cursor_local.execute("SELECT referral_code FROM users WHERE referral_code = ?", (new_code,))
            if cursor_local.fetchone() is None:
                break
            new_code = generate_ref_code()
        cursor_local.execute(
            "INSERT INTO users (chat_id, points, referral_code, referred_by) VALUES (?, ?, ?, ?)",
            (chat_id, 0, new_code, referred_by)
        )
        conn_local.commit()
    cursor_local.close()
    conn_local.close()

    bot.send_message(
        chat_id,
        t(chat_id, "choose_language"),
        reply_markup=get_inline_language_buttons(chat_id)
    )


# ------------------------------------------------------------------------
#   15. Callback: выбор языка
# ------------------------------------------------------------------------
@ensure_user
@bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("set_lang|"))
def handle_set_lang(call):
    chat_id = call.from_user.id
    _, lang_code = call.data.split("|", 1)

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
            "edit_cart_phase": None,
            "awaiting_review_flavor": None,
            "awaiting_review_rating": False,
            "awaiting_review_comment": False,
            "temp_review_flavor": None,
            "temp_review_rating": 0
        }
    else:
        user_data[chat_id]["lang"] = lang_code

    bot.answer_callback_query(call.id, t(chat_id, "lang_set"))
    bot.send_message(chat_id, t(chat_id, "choose_category"), reply_markup=get_inline_main_menu(chat_id))

    conn_local = get_db_connection()
    cursor_local = conn_local.cursor()
    cursor_local.execute("SELECT referral_code FROM users WHERE chat_id = ?", (chat_id,))
    row = cursor_local.fetchone()
    cursor_local.close()
    conn_local.close()

    if row:
        code = row[0]
        bot_username = bot.get_me().username
        ref_link = f"https://t.me/{bot_username}?start=ref={code}"
        if user_data[chat_id]["lang"] == "en":
            bot.send_message(
                chat_id,
                f"Your referral code: {code}\nShare this link with friends:\n{ref_link}"
            )
        else:
            bot.send_message(
                chat_id,
                f"Зарабатывайте баллы! Ваш реферальный код: {code}\nПоделитесь этой ссылкой с друзьями:\n{ref_link}"
            )


# ------------------------------------------------------------------------
#   16. Callback: выбор категории (показываем вкусы)
# ------------------------------------------------------------------------
@ensure_user
@bot.callback_query_handler(func=lambda call: call.data and call.data.startswith("category|"))
def handle_category(call):
    chat_id = call.from_user.id
    _, cat = call.data.split("|", 1)

    # === ОТЛАДКА ===
    print(f"DEBUG: нажата категория → '{cat}'")
    print(f"DEBUG: ключи menu сейчас = {list(menu.keys())}")
    # ================

    if cat not in menu:
        bot.answer_callback_query(call.id, t(chat_id, "error_invalid"))
        return

    bot.answer_callback_query(call.id)
    user_data[chat_id]["current_category"] = cat

    photo_url = menu[cat].get("photo_url", "").strip()
    if photo_url:
        try:
            bot.send_photo(chat_id, photo_url)
        except Exception as e:
            print(f"Failed to send category photo for {cat}: {e}")

    bot.send_message(
        chat_id,
        f"{t(chat_id, 'choose_flavor')} «{cat}»",
        reply_markup=get_inline_flavors(chat_id, cat)
    )


# ------------------------------------------------------------------------
#   17. Callback: «Назад к категориям»
# ------------------------------------------------------------------------
@ensure_user
@bot.callback_query_handler(func=lambda call: call.data == "go_back_to_categories")
def handle_go_back_to_categories(call):
    chat_id = call.from_user.id
    bot.answer_callback_query(call.id)
    bot.send_message(chat_id, t(chat_id, "choose_category"), reply_markup=get_inline_main_menu(chat_id))


# ------------------------------------------------------------------------
#   18. Callback: выбор вкуса
# ------------------------------------------------------------------------
@ensure_user
@bot.callback_query_handler(func=lambda call: call.data and call.data.startswith("flavor|"))
def handle_flavor(call):
    _, cat, flavor = call.data.split("|", 2)
    chat_id = call.from_user.id

    if cat not in menu:
        bot.answer_callback_query(call.id, t(chat_id, "error_invalid"))
        return

    item_obj = next((i for i in menu[cat]["flavors"] if i["flavor"] == flavor), None)
    if not item_obj or item_obj.get("stock", 0) <= 0:
        bot.answer_callback_query(call.id, t(chat_id, "error_out_of_stock"))
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


# ------------------------------------------------------------------------
#   19. Callback: добавить в корзину (без изменения stock)
# ------------------------------------------------------------------------
@ensure_user
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


# ------------------------------------------------------------------------
#   20. Callback: «Просмотр корзины»
# ------------------------------------------------------------------------
@ensure_user
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


# ------------------------------------------------------------------------
#   21. Callback: «Удалить i» из корзины
# ------------------------------------------------------------------------
@ensure_user
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


# ------------------------------------------------------------------------
#   22. Callback: «Изменить i» в корзине → ввод количества
# ------------------------------------------------------------------------
@ensure_user
@bot.callback_query_handler(func=lambda call: call.data and call.data.startswith("edit_item|"))
def handle_edit_item_request(call):
    chat_id = call.from_user.id
    _, idx_str = call.data.split("|", 1)
    idx = int(idx_str) - 1
    data = user_data.get(chat_id, {})
    cart = data.get("cart", [])

    # Группируем для подсчёта
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

    # Сохраняем состояние для ввода количества
    data["edit_cart_phase"] = "enter_qty"
    data["edit_index"] = idx
    data["edit_cat"] = cat
    data["edit_flavor"] = flavor
    user_data[chat_id] = data

    # Выводим по-русски или по-английски в зависимости от языка
    lang = user_data.get(chat_id, {}).get("lang", "ru")
    if lang == "ru":
        prefix = f"Текущий товар: {cat} — {flavor} — {price}₺ (в корзине {old_qty} шт)."
    else:
        prefix = f"Current item: {cat} — {flavor} — {price}₺ (in cart {old_qty} pcs)."

    bot.send_message(
        chat_id,
        prefix + "\n" + t(chat_id, "enter_new_qty"),
        reply_markup=types.ReplyKeyboardRemove()
    )


@ensure_user
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


# ------------------------------------------------------------------------
#   23. Callback: «Очистить корзину»
# ------------------------------------------------------------------------
@ensure_user
@bot.callback_query_handler(func=lambda call: call.data == "clear_cart")
def handle_clear_cart(call):
    chat_id = call.from_user.id
    bot.answer_callback_query(call.id)
    data = user_data.get(chat_id, {})
    data["cart"] = []
    bot.send_message(chat_id, t(chat_id, "cart_cleared"), reply_markup=get_inline_main_menu(chat_id))
    user_data[chat_id] = data


# ------------------------------------------------------------------------
#   24. Callback: завершить заказ (с проверкой и списанием stock)
# ------------------------------------------------------------------------
@ensure_user
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

    conn_local = get_db_connection()
    cursor_local = conn_local.cursor()
    cursor_local.execute("SELECT points FROM users WHERE chat_id = ?", (chat_id,))
    row = cursor_local.fetchone()
    cursor_local.close()
    conn_local.close()

    user_points = row[0] if row else 0

    if user_points > 0:
        max_points = min(user_points, total_try)
        points_try = user_points * 1
        msg = (
                t(chat_id, "points_info").format(points=user_points, points_try=points_try)
                + "\n"
                + t(chat_id, "enter_points").format(max_points=max_points)
        )
        bot.send_message(chat_id, msg, reply_markup=types.ReplyKeyboardRemove())
        data["wait_for_points"] = True
        data["temp_total_try"] = total_try
        data["temp_user_points"] = user_points
    else:
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


# ------------------------------------------------------------------------
#   25. Handler: ввод количества баллов для списания
# ------------------------------------------------------------------------
@ensure_user
@bot.message_handler(func=lambda m: user_data.get(m.chat.id, {}).get("wait_for_points"), content_types=['text'])
def handle_points_input(message):
    chat_id = message.chat.id
    data = user_data.get(chat_id, {})
    text = message.text.strip()

    if not text.isdigit():
        bot.send_message(chat_id, t(chat_id, "invalid_points").format(max_points=data.get("temp_total_try", 0)))
        return

    points_to_spend = int(text)
    user_points = data.get("temp_user_points", 0)
    total_try = data.get("temp_total_try", 0)
    max_points = min(user_points, total_try)

    if points_to_spend < 0 or points_to_spend > max_points:
        bot.send_message(chat_id, t(chat_id, "invalid_points").format(max_points=max_points))
        return

    if points_to_spend > 0:
        conn_local = get_db_connection()
        cursor_local = conn_local.cursor()
        cursor_local.execute("UPDATE users SET points = points - ? WHERE chat_id = ?", (points_to_spend, chat_id))
        conn_local.commit()
        cursor_local.close()
        conn_local.close()

    discount_try = points_to_spend * 1
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


# ------------------------------------------------------------------------
#   26. Handler: ввод адреса
# ------------------------------------------------------------------------
@ensure_user
@bot.message_handler(
    func=lambda m: user_data.get(m.chat.id, {}).get("wait_for_address"),
    content_types=['text', 'location', 'venue']
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


# ------------------------------------------------------------------------
#   27. Handler: ввод контакта
# ------------------------------------------------------------------------
@ensure_user
@bot.message_handler(
    func=lambda m: user_data.get(m.chat.id, {}).get("wait_for_contact"),
    content_types=['text', 'contact']
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


# ------------------------------------------------------------------------
#   28. Handler: ввод комментария и сохранение заказа (с учётом списания stock)
# ------------------------------------------------------------------------
@ensure_user
@bot.message_handler(
    func=lambda m: user_data.get(m.chat.id, {}).get("wait_for_comment"),
    content_types=['text']
)
def handle_comment_input(message):
    chat_id = message.chat.id
    data = user_data.get(chat_id, {})
    text = message.text or ""

    # Обработка кнопки «Назад»
    if text == t(chat_id, "back"):
        data['wait_for_contact'] = True
        data['wait_for_comment'] = False
        bot.send_message(chat_id, t(chat_id, "enter_contact"), reply_markup=contact_keyboard())
        user_data[chat_id] = data
        return

    # Пользователь вводит текст комментария
    if text == t(None, "enter_comment"):
        bot.send_message(chat_id, t(chat_id, "enter_comment"), reply_markup=types.ReplyKeyboardRemove())
        return

    if message.content_type == 'text' and text != t(None, "send_order"):
        data['comment'] = text.strip()
        bot.send_message(chat_id, t(chat_id, "comment_saved"), reply_markup=comment_keyboard())
        user_data[chat_id] = data
        return

    # Пользователь подтвердил отправку заказа
    if text == t(None, "send_order"):
        cart = data.get('cart', [])
        if not cart:
            bot.send_message(chat_id, t(chat_id, "cart_empty"))
            return

        # Считаем сумму заказа и скидку
        total_try = sum(i['price'] for i in cart)
        discount = data.pop("pending_discount", 0)
        total_after = max(total_try - discount, 0)

        # Проверяем наличие на складе
        needed = {}
        for it in cart:
            key = (it["category"], it["flavor"])
            needed[key] = needed.get(key, 0) + 1

        for (cat0, flavor0), qty_needed in needed.items():
            item_obj = next((i for i in menu[cat0]["flavors"] if i["flavor"] == flavor0), None)
            if not item_obj or item_obj.get("stock", 0) < qty_needed:
                bot.send_message(chat_id, f"😕 К сожалению, «{flavor0}» больше не доступен в нужном количестве.")
                return

        # Списываем товары со склада
        for (cat0, flavor0), qty_needed in needed.items():
            for itm in menu[cat0]["flavors"]:
                if itm["flavor"] == flavor0:
                    itm["stock"] = itm.get("stock", 0) - qty_needed
                    break
        with open(MENU_PATH, "w", encoding="utf-8") as f:
            json.dump(menu, f, ensure_ascii=False, indent=2)

        # Подсчёт баллов
        pts_spent  = data.get("pending_points_spent", 0)  # уже списано до этого
        pts_earned = total_after // 30

        # Сохраняем в БД заказ вместе с баллами
        items_json = json.dumps(cart, ensure_ascii=False)
        now = datetime.datetime.utcnow().isoformat()
        conn_local = get_db_connection()
        cursor_local = conn_local.cursor()
        cursor_local.execute(
            "INSERT INTO orders "
            "(chat_id, items_json, total, timestamp, points_spent, points_earned) "
            "VALUES (?,?,?,?,?,?)",
            (chat_id, items_json, total_after, now, pts_spent, pts_earned)
        )
        order_id = cursor_local.lastrowid
        conn_local.commit()

        # Начисляем пользователю новые баллы
        if pts_earned > 0:
            cursor_local.execute(
                "UPDATE users SET points = points + ? WHERE chat_id = ?",
                (pts_earned, chat_id)
            )
            conn_local.commit()
            bot.send_message(chat_id, f"👍 Вы получили {pts_earned} бонусных баллов за этот заказ.")

        # Обрабатываем реферальную систему (если нужно)...
        # (ваш уже существующий код по начислению 200 баллов пригласившему)

        # Отправляем уведомления в личный чат и группу
        summary = "\n".join(f"{i['category']}: {i['flavor']} — {i['price']}₺" for i in cart)
        rates = fetch_rates()
        rub = round(total_after * rates.get("RUB", 0) + 500, 2)
        usd = round(total_after * rates.get("USD", 0) + 2, 2)
        uah = round(total_after * rates.get("UAH", 0) + 200, 2)
        conv = f"({rub}₽, ${usd}, ₴{uah})"

        # Русский
        full_rus = (
            f"📥 Новый заказ от @{message.from_user.username or message.from_user.first_name}:\n\n"
            f"{summary}\n\n"
            f"Итог: {total_after}₺ {conv}\n"
            f"📍 Адрес: {data.get('address','—')}\n"
            f"📱 Контакт: {data.get('contact','—')}\n"
            f"💬 Комментарий: {data.get('comment','—')}"
        )
        bot.send_message(PERSONAL_CHAT_ID, full_rus)

        # Английский с кнопкой отмены
        full_en = (
            f"📥 New order from @{message.from_user.username or message.from_user.first_name}:\n\n"
            f"{summary}\n\n"
            f"Total: {total_after}₺ {conv}\n"
            f"📍 Address: {data.get('address','—')}\n"
            f"📱 Contact: {data.get('contact','—')}\n"
            f"💬 Comment: {translate_to_en(data.get('comment',''))}"
        )
        kb = types.InlineKeyboardMarkup()
        kb.add(types.InlineKeyboardButton(
            text="❌ Отменить заказ",
            callback_data=f"cancel_order|{order_id}"
        ))
        bot.send_message(GROUP_CHAT_ID, full_en, reply_markup=kb)

        # Завершаем диалог с пользователем
        bot.send_message(
            chat_id,
            t(chat_id, "order_accepted"),
            reply_markup=types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
                              .add(f"➕ {t(chat_id, 'add_more')}")
        )

        # Сбрасываем состояние
        data.update({
            "cart": [], "current_category": None,
            "wait_for_address": False,
            "wait_for_contact": False,
            "wait_for_comment": False,
            "pending_discount": 0,
            "pending_points_spent": 0
        })
        user_data[chat_id] = data

        cursor_local.close()
        conn_local.close()
        return


        # Списываем stock из menu и сохраняем JSON
        # ... после того, как вы записали заказ в БД и начислили баллы пользователю:

        # 1) Сформировать текст уведомления англ. для админ-группы
        full_en = (
            f"📥 New order from @{message.from_user.username or message.from_user.first_name}:\n\n"
            f"{summary}\n\n"
            f"Total: {total_after}₺ {conv}\n"
            f"📍 Address: {data.get('address', '—')}\n"
            f"📱 Contact: {data.get('contact', '—')}\n"
            f"💬 Comment: {translate_to_en(data.get('comment', ''))}"
        )

        # 2) Создать инлайн-клавиатуру с кнопкой отмены
        kb = types.InlineKeyboardMarkup()
        kb.add(
            types.InlineKeyboardButton(
                text="❌ Отменить заказ",
                callback_data=f"cancel_order|{order_id}"
            )
        )

        # 3) Отправить сообщение вместе с кнопкой
        bot.send_message(
            GROUP_CHAT_ID,
            full_en,
            reply_markup=kb
        )

        # реферальная система
        cursor_local.execute("SELECT referred_by FROM users WHERE chat_id = ?", (chat_id,))
        row = cursor_local.fetchone()
        if row and row[0]:
            inviter = row[0]
            cursor_local.execute("UPDATE users SET points = points + 200 WHERE chat_id = ?", (inviter,))
            conn_local.commit()
            bot.send_message(inviter, "🎉 Вам начислено 200 бонусных баллов за приглашение нового клиента!")
            cursor_local.execute("UPDATE users SET referred_by = NULL WHERE chat_id = ?", (chat_id,))
            conn_local.commit()

        cursor_local.close()
        conn_local.close()

        summary = "\n".join(f"{i['category']}: {i['flavor']} — {i['price']}₺" for i in cart)
        rates = fetch_rates()
        rub = round(total_after * rates.get("RUB", 0) + 500, 2)
        usd = round(total_after * rates.get("USD", 0) + 2, 2)
        uah = round(total_after * rates.get("UAH", 0) + 200, 2)
        conv = f"({rub}₽, ${usd}, ₴{uah})"

        full_rus = (
            f"📥 Новый заказ от @{message.from_user.username or message.from_user.first_name}:\n\n"
            f"{summary}\n\n"
            f"Итог: {total_after}₺ {conv}\n"
            f"📍 Адрес: {data.get('address', '—')}\n"
            f"📱 Контакт: {data.get('contact', '—')}\n"
            f"💬 Комментарий: {data.get('comment', '—')}"
        )
        bot.send_message(PERSONAL_CHAT_ID, full_rus)

        comment_ru = data.get('comment', '')
        comment_en = translate_to_en(comment_ru) if comment_ru else "—"
        full_en = (
            f"📥 New order from @{message.from_user.username or message.from_user.first_name}:\n\n"
            f"{summary}\n\n"
            f"Total: {total_after}₺ {conv}\n"
            f"📍 Address: {data.get('address', '—')}\n"
            f"📱 Contact: {data.get('contact', '—')}\n"
            f"💬 Comment: {comment_en}"
        )
        bot.send_message(GROUP_CHAT_ID, full_en)

        bot.send_message(
            chat_id,
            t(chat_id, "order_accepted"),
            reply_markup=types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
            .add(f"➕ {t(chat_id, 'add_more')}")
        )

        data.update({
            "cart": [], "current_category": None,
            "wait_for_address": False, "wait_for_contact": False, "wait_for_comment": False
        })
        user_data[chat_id] = data
        return


# ------------------------------------------------------------------------
#   29. /change: перевод в режим редактирования меню (только на английском)
# ------------------------------------------------------------------------
@ensure_user
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
            "edit_cart_phase": None,
            "awaiting_review_flavor": None,
            "awaiting_review_rating": False,
            "awaiting_review_comment": False,
            "temp_review_flavor": None,
            "temp_review_rating": 0
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
    user_data[chat_id] = data


# ------------------------------------------------------------------------
#   30. Хендлер /points
# ------------------------------------------------------------------------
@ensure_user
@bot.message_handler(commands=['points'])
def cmd_points(message):
    chat_id = message.chat.id
    conn_local = get_db_connection()
    cursor_local = conn_local.cursor()
    cursor_local.execute("SELECT points FROM users WHERE chat_id = ?", (chat_id,))
    row = cursor_local.fetchone()
    cursor_local.close()
    conn_local.close()

    if row is None or row[0] == 0:
        bot.send_message(chat_id, "У вас пока нет бонусных баллов.")
    else:
        points = row[0]
        bot.send_message(chat_id, f"У вас сейчас {points} бонусных баллов.")


# ------------------------------------------------------------------------
#   31. Хендлер /convert — курсы и конвертация суммы TRY
# ------------------------------------------------------------------------
@ensure_user
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


# ------------------------------------------------------------------------
#   32. Хендлер /review (запуск процесса отзывов)
# ------------------------------------------------------------------------
@ensure_user
@bot.message_handler(commands=['review'])
def cmd_review_start(message):
    chat_id = message.chat.id
    parts = message.text.split(maxsplit=1)
    if len(parts) != 2:
        bot.send_message(chat_id, "Usage: /review <flavor_name>")
        return

    flavor_query = parts[1].strip()
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
        bot.send_message(chat_id, t(chat_id, "error_invalid"))
        return

    data = user_data.setdefault(chat_id, {})
    data["awaiting_review_flavor"] = db_flavor
    data["awaiting_review_rating"] = True
    data["awaiting_review_comment"] = False
    data["temp_review_flavor"] = db_flavor
    bot.send_message(
        chat_id,
        t(chat_id, "review_prompt_rate").format(flavor=db_flavor)
    )
    user_data[chat_id] = data


# ------------------------------------------------------------------------
#   33. Handler для ввода оценки после /review
# ------------------------------------------------------------------------
@ensure_user
@bot.message_handler(func=lambda m: user_data.get(m.chat.id, {}).get("awaiting_review_rating"), content_types=['text'])
def handle_review_rating(message):
    chat_id = message.chat.id
    data = user_data.get(chat_id, {})
    text = message.text.strip()

    if not text.isdigit() or not (1 <= int(text) <= 5):
        bot.send_message(chat_id, t(chat_id, "review_prompt_rate").format(flavor=data["temp_review_flavor"]))
        return

    rating = int(text)
    data["temp_review_rating"] = rating
    data["awaiting_review_rating"] = False
    data["awaiting_review_comment"] = True
    bot.send_message(chat_id, t(chat_id, "review_prompt_comment"))
    user_data[chat_id] = data


# ------------------------------------------------------------------------
#   34. Handler для ввода комментария или /skip после оценки
# ------------------------------------------------------------------------
@ensure_user
@bot.message_handler(func=lambda m: user_data.get(m.chat.id, {}).get("awaiting_review_comment"), content_types=['text'])
def handle_review_comment(message):
    chat_id = message.chat.id
    data = user_data.get(chat_id, {})
    text = message.text.strip()

    if text.lower() == "/skip":
        comment = ""
    else:
        comment = text

    flavor_name = data.get("temp_review_flavor")
    rating = data.get("temp_review_rating")

    now = datetime.datetime.utcnow().isoformat()
    conn_local = get_db_connection()
    cursor_local = conn_local.cursor()
    cursor_local.execute(
        "INSERT INTO reviews (chat_id, category, flavor, rating, comment, timestamp) VALUES (?, ?, ?, ?, ?, ?)",
        (chat_id, None, flavor_name, rating, comment, now)
    )
    conn_local.commit()

    cursor_local.execute(
        "SELECT AVG(rating) FROM reviews WHERE flavor = ?",
        (flavor_name,)
    )
    avg_rating = cursor_local.fetchone()[0] or 0

    # Обновим средний рейтинг в menu.json (если есть поле "rating")
    for cat_key, cat_data in menu.items():
        for itm in cat_data["flavors"]:
            if itm["flavor"] == flavor_name:
                itm["rating"] = round(avg_rating, 1)
    with open(MENU_PATH, "w", encoding="utf-8") as f:
        json.dump(menu, f, ensure_ascii=False, indent=2)

    cursor_local.close()
    conn_local.close()

    bot.send_message(
        chat_id,
        t(chat_id, "review_thanks").format(flavor=flavor_name, avg=round(avg_rating, 1))
    )

    # Сбросим флаги
    data["awaiting_review_flavor"] = None
    data["awaiting_review_rating"] = False
    data["awaiting_review_comment"] = False
    data["temp_review_flavor"] = None
    data["temp_review_rating"] = 0
    user_data[chat_id] = data


# ------------------------------------------------------------------------
#   35. Универсальный хендлер (всё остальное, включая /change логику)
# ------------------------------------------------------------------------
@ensure_user
@bot.message_handler(content_types=['text', 'location', 'venue', 'contact'])
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
            "edit_cart_phase": None,
            "awaiting_review_flavor": None,
            "awaiting_review_rating": False,
            "awaiting_review_comment": False,
            "temp_review_flavor": None,
            "temp_review_rating": 0
        }
    data = user_data[chat_id]

    @bot.callback_query_handler(func=lambda call: call.data and call.data.startswith("cancel_order|"))
    def handle_cancel_order(call):
        # 1) Проверяем права
        admin_id = call.from_user.id
        if admin_id not in (ADMIN_ID, ADMIN_ID_TWO):
            bot.answer_callback_query(call.id, "Нет доступа", show_alert=True)
            return

        # 2) Извлекаем order_id
        _, oid = call.data.split("|", 1)
        order_id = int(oid)

        # 3) Получаем данные заказа
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT chat_id, items_json, points_earned FROM orders WHERE order_id = ?",
            (order_id,)
        )
        row = cursor.fetchone()
        if not row:
            bot.answer_callback_query(call.id, "Заказ не найден", show_alert=True)
            cursor.close();
            conn.close()
            return

        user_chat_id, items_json, pts_earned = row

        # 4) Возвращаем товары на склад
        items = json.loads(items_json)
        for it in items:
            cat, flav = it["category"], it["flavor"]
            for itm in menu[cat]["flavors"]:
                if itm["flavor"] == flav:
                    itm["stock"] = itm.get("stock", 0) + 1
                    break
        with open(MENU_PATH, "w", encoding="utf-8") as f:
            json.dump(menu, f, ensure_ascii=False, indent=2)

        # 5) Списываем у пользователя начисленные баллы
        if pts_earned > 0:
            cursor.execute(
                "UPDATE users SET points = points - ? WHERE chat_id = ?",
                (pts_earned, user_chat_id)
            )
            conn.commit()

        # 6) Удаляем запись о заказе
        cursor.execute("DELETE FROM orders WHERE order_id = ?", (order_id,))
        conn.commit()
        cursor.close();
        conn.close()

        # 7) Уведомляем пользователя
        bot.send_message(
            user_chat_id,
            f"Ваш заказ #{order_id} отменён, {pts_earned} бонусных баллов списано."
        )

        # 8) Убираем кнопку из админского сообщения
        bot.edit_message_reply_markup(
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            reply_markup=None
        )
        bot.answer_callback_query(call.id, "Заказ отменён")

    # ─── Режим редактирования меню (/change) ────────────────────────────────────────
    if data.get('edit_phase'):
        phase = data['edit_phase']

        # 1) Главное меню редактирования (всё на английском)
        if phase == 'choose_action':
            # Cancel
            if text == "❌ Cancel":
                data['edit_phase'] = None
                data['edit_cat'] = None
                bot.send_message(chat_id, "Editing cancelled.", reply_markup=get_inline_main_menu(chat_id))
                user_data[chat_id] = data
                return

            # Back
            if text == "⬅️ Back":
                data['edit_phase'] = None
                data['edit_cat'] = None
                bot.send_message(chat_id, "Returned to main menu.", reply_markup=get_inline_main_menu(chat_id))
                user_data[chat_id] = data
                return

            if text == "➕ Add Category":
                data['edit_phase'] = 'add_category'
                kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
                kb.add("⬅️ Back", "❌ Cancel")
                bot.send_message(chat_id, "Enter new category name:", reply_markup=kb)
                user_data[chat_id] = data
                return

            if text == "➖ Remove Category":
                data['edit_phase'] = 'remove_category'
                kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
                for cat_key in menu:
                    kb.add(cat_key)
                kb.add("⬅️ Back", "❌ Cancel")
                bot.send_message(chat_id, "Select category to remove:", reply_markup=kb)
                user_data[chat_id] = data
                return

            if text == "💲 Fix Price":
                data['edit_phase'] = 'choose_fix_price_cat'
                kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
                for cat_key in menu:
                    kb.add(cat_key)
                kb.add("⬅️ Back", "❌ Cancel")
                bot.send_message(chat_id, "Select category to fix price for:", reply_markup=kb)
                user_data[chat_id] = data
                return

            if text == "ALL IN":
                data['edit_phase'] = 'choose_all_in_cat'
                kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
                for cat_key in menu:
                    kb.add(cat_key)
                kb.add("⬅️ Back", "❌ Cancel")
                bot.send_message(chat_id, "Select category to replace full flavor list:", reply_markup=kb)
                user_data[chat_id] = data
                return

            if text == "🔄 Actual Flavor":
                data['edit_phase'] = 'choose_cat_actual'
                kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
                for cat_key in menu:
                    kb.add(cat_key)
                kb.add("⬅️ Back", "❌ Cancel")
                bot.send_message(chat_id, "Select category to update individual flavor stock:", reply_markup=kb)
                user_data[chat_id] = data
                return

            if text == "🖼️ Add Category Picture":
                data['edit_phase'] = 'choose_category_for_picture'
                kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
                for cat_key in menu:
                    kb.add(cat_key)
                kb.add("⬅️ Back", "❌ Cancel")
                bot.send_message(chat_id, "Select category to update picture for:", reply_markup=kb)
                user_data[chat_id] = data
                return

            if text == "Set Category Flavor to 0":
                data['edit_phase'] = 'choose_cat_zero'
                kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
                for cat_key in menu:
                    kb.add(cat_key)
                kb.add("⬅️ Back", "❌ Cancel")
                bot.send_message(chat_id, "Select category to set all flavors to zero stock:", reply_markup=kb)
                user_data[chat_id] = data
                return

            bot.send_message(chat_id, "Choose action:", reply_markup=edit_action_keyboard())
            return

        # 2) Добавить категорию
        if phase == 'add_category':
            if text in ["⬅️ Back", "❌ Cancel"]:
                data['edit_phase'] = 'choose_action'
                bot.send_message(chat_id, "Back to editing menu:", reply_markup=edit_action_keyboard())
                user_data[chat_id] = data
                return

            new_cat = text.strip()
            if not new_cat or new_cat in menu:
                kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
                kb.add("⬅️ Back", "❌ Cancel")
                bot.send_message(chat_id, "Invalid or existing name. Try again:", reply_markup=kb)
                return

            menu[new_cat] = {
                "price": 1300,
                "flavors": []
            }
            with open(MENU_PATH, "w", encoding="utf-8") as f:
                json.dump(menu, f, ensure_ascii=False, indent=2)

            data['edit_phase'] = 'choose_action'
            bot.send_message(chat_id, f"Category '{new_cat}' added.", reply_markup=edit_action_keyboard())
            user_data[chat_id] = data
            return

        # 3) Выбор категории для загрузки картинки
        if phase == 'choose_category_for_picture':
            if text in ["⬅️ Back", "❌ Cancel"]:
                data['edit_phase'] = 'choose_action'
                bot.send_message(chat_id, "Back to editing menu:", reply_markup=edit_action_keyboard())
                user_data[chat_id] = data
                return

            if text in menu:
                data['edit_cat'] = text
                data['edit_phase'] = 'enter_category_picture_url'
                kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
                kb.add("⬅️ Back", "❌ Cancel")
                bot.send_message(chat_id, "Please send RAW URL for the new category picture:", reply_markup=kb)
                user_data[chat_id] = data
                return
            else:
                kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
                kb.add("⬅️ Back", "❌ Cancel")
                bot.send_message(chat_id, "Select a valid category from the list:", reply_markup=kb)
                return

        # 4) Ввод URL для картинки категории
        if phase == 'enter_category_picture_url':
            if text in ["⬅️ Back", "❌ Cancel"]:
                data.pop('edit_cat', None)
                data['edit_phase'] = 'choose_action'
                bot.send_message(chat_id, "Back to editing menu:", reply_markup=edit_action_keyboard())
                user_data[chat_id] = data
                return

            new_url = text.strip()
            cat0 = data.get('edit_cat')
            if cat0 and new_url:
                if isinstance(menu.get(cat0), dict):
                    menu[cat0]['photo_url'] = new_url
                    with open(MENU_PATH, "w", encoding="utf-8") as f:
                        json.dump(menu, f, ensure_ascii=False, indent=2)
                    bot.send_message(chat_id, f"Picture for category '{cat0}' updated.",
                                     reply_markup=edit_action_keyboard())
                else:
                    bot.send_message(chat_id, "Error: category not found.", reply_markup=edit_action_keyboard())
            else:
                bot.send_message(chat_id, "Invalid URL. Try again or press Cancel.",
                                 reply_markup=edit_action_keyboard())

            data.pop('edit_cat', None)
            data['edit_phase'] = 'choose_action'
            user_data[chat_id] = data
            return


        # 5) Установить все вкусы категории на ноль
        if phase == 'choose_cat_zero':
            if text in ["⬅️ Back", "❌ Cancel"]:
                data['edit_phase'] = 'choose_action'
                bot.send_message(chat_id, "Back to editing menu:", reply_markup=edit_action_keyboard())
                user_data[chat_id] = data
                return

            if text in menu:
                cat0 = text
                for itm in menu[cat0]["flavors"]:
                    itm["stock"] = 0
                with open(MENU_PATH, "w", encoding="utf-8") as f:
                    json.dump(menu, f, ensure_ascii=False, indent=2)
                bot.send_message(chat_id, f"All flavors in category '{cat0}' set to 0 stock.",
                                 reply_markup=edit_action_keyboard())
                data.pop('edit_cat', None)
                data['edit_phase'] = 'choose_action'
                user_data[chat_id] = data
            else:
                kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
                kb.add("⬅️ Back", "❌ Cancel")
                bot.send_message(chat_id, "Select a valid category to zero out:", reply_markup=kb)
            return

        # 6) Удалить категорию
        if phase == 'remove_category':
            if text in ["⬅️ Back", "❌ Cancel"]:
                data['edit_phase'] = 'choose_action'
                bot.send_message(chat_id, "Back to editing menu:", reply_markup=edit_action_keyboard())
                user_data[chat_id] = data
                return

            if text in menu:
                del menu[text]
                with open(MENU_PATH, "w", encoding="utf-8") as f:
                    json.dump(menu, f, ensure_ascii=False, indent=2)
                data['edit_phase'] = 'choose_action'
                bot.send_message(chat_id, f"Category '{text}' removed.", reply_markup=edit_action_keyboard())
                user_data[chat_id] = data
            else:
                kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
                kb.add("⬅️ Back", "❌ Cancel")
                bot.send_message(chat_id, "Select a valid category.", reply_markup=kb)
            return

        # 7) Выбрать категорию для Fix Price
        if phase == 'choose_fix_price_cat':
            if text in ["⬅️ Back", "❌ Cancel"]:
                data['edit_phase'] = 'choose_action'
                bot.send_message(chat_id, "Back to editing menu:", reply_markup=edit_action_keyboard())
                user_data[chat_id] = data
                return

            if text in menu:
                data['edit_cat'] = text
                data['edit_phase'] = 'enter_new_price'
                kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
                kb.add("⬅️ Back", "❌ Cancel")
                bot.send_message(chat_id, f"Enter new price in ₺ for category '{text}':", reply_markup=kb)
                user_data[chat_id] = data
            else:
                kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
                kb.add("⬅️ Back", "❌ Cancel")
                bot.send_message(chat_id, "Choose a category from the list.", reply_markup=kb)
            return

        # 8) Ввод новой цены для категории
        if phase == 'enter_new_price':
            if text in ["⬅️ Back", "❌ Cancel"]:
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
                kb.add("⬅️ Back", "❌ Cancel")
                bot.send_message(chat_id, "Invalid price format. Enter a number, e.g. 1500:", reply_markup=kb)
                return

            menu[cat0]["price"] = int(new_price)
            with open(MENU_PATH, "w", encoding="utf-8") as f:
                json.dump(menu, f, ensure_ascii=False, indent=2)

            bot.send_message(chat_id, f"Price for category '{cat0}' set to {int(new_price)}₺.",
                             reply_markup=edit_action_keyboard())
            data.pop('edit_cat', None)
            data['edit_phase'] = 'choose_action'
            user_data[chat_id] = data
            return

        # 9) Выбрать категорию для ALL IN
        if phase == 'choose_all_in_cat':
            if text in ["⬅️ Back", "❌ Cancel"]:
                data['edit_phase'] = 'choose_action'
                bot.send_message(chat_id, "Back to editing menu:", reply_markup=edit_action_keyboard())
                user_data[chat_id] = data
                return

            if text in menu:
                data['edit_cat'] = text
                current_list = []
                for itm in menu[text]["flavors"]:
                    current_list.append(f"{itm['flavor']} - {itm.get('stock', 0)}")
                joined = "\n".join(current_list) if current_list else "(empty)"
                kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
                kb.add("⬅️ Back", "❌ Cancel")
                bot.send_message(
                    chat_id,
                    f"Current flavors in '{text}' (one per line as \"Name - qty\"):\n\n{joined}\n\n"
                    "Send the full updated list in the same format. Each line: “Name - qty”.",
                    reply_markup=kb
                )
                data['edit_phase'] = 'replace_all_in'
                user_data[chat_id] = data
            else:
                kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
                kb.add("⬅️ Back", "❌ Cancel")
                bot.send_message(chat_id, "Choose a valid category from the list.", reply_markup=kb)
            return

        # 10) Заменить полный список вкусов (ALL IN)
        if phase == 'replace_all_in':
            if text in ["⬅️ Back", "❌ Cancel"]:
                data.pop('edit_cat', None)
                data['edit_phase'] = 'choose_action'
                bot.send_message(chat_id, "Back to editing menu:", reply_markup=edit_action_keyboard())
                user_data[chat_id] = data
                return

            cat0 = data.get('edit_cat')
            lines = text.strip().splitlines()
            new_flavors = []
            for line in lines:
                stripped = line.strip()
                if not stripped or stripped.lower() == "(empty)":
                    continue
                if '-' in stripped:
                    parts = stripped.rsplit('-', 1)
                else:
                    continue
                name = parts[0].strip()
                qty_part = parts[-1].strip()
                if not qty_part.isdigit() or not name:
                    continue
                qty = int(qty_part)
                new_flavors.append({
                    "emoji": "",
                    "flavor": name,
                    "stock": qty,
                    "tags": [],
                    "description_ru": "",
                    "description_en": "",
                    "photo_url": ""
                })
            menu[cat0]["flavors"] = new_flavors
            with open(MENU_PATH, "w", encoding="utf-8") as f:
                json.dump(menu, f, ensure_ascii=False, indent=2)

            bot.send_message(chat_id, f"Full flavor list for '{cat0}' replaced.", reply_markup=edit_action_keyboard())
            data.pop('edit_cat', None)
            data['edit_phase'] = 'choose_action'
            user_data[chat_id] = data
            return

        # 11) Выбрать категорию для Actual Flavor (обновлённый список)
        if phase == 'choose_cat_actual':
            if text in ["⬅️ Back", "❌ Cancel"]:
                data['edit_phase'] = 'choose_action'
                bot.send_message(chat_id, "Back to editing menu:", reply_markup=edit_action_keyboard())
                user_data[chat_id] = data
                return

            if text in menu:
                # Сохраняем выбранную категорию и переходим к выбору вкуса
                data['edit_cat'] = text
                data['edit_phase'] = 'choose_flavor_actual'

                # Формируем клавиатуру с теми вкусами, в которых stock > 0
                kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
                any_in_stock = False
                for itm in menu[text]["flavors"]:
                    stock = itm.get("stock", 0)
                    if isinstance(stock, str) and stock.isdigit():
                        stock = int(stock)
                        itm["stock"] = stock
                    if isinstance(stock, int) and stock > 0:
                        any_in_stock = True
                        kb.add(f"{itm['flavor']} (current: {stock})")
                if not any_in_stock:
                    bot.send_message(
                        chat_id,
                        f"No flavors with stock > 0 in category '{text}'.",
                        reply_markup=edit_action_keyboard()
                    )
                    data.pop('edit_cat', None)
                    data['edit_phase'] = 'choose_action'
                    user_data[chat_id] = data
                    return

                kb.add("⬅️ Back", "❌ Cancel")
                bot.send_message(
                    chat_id,
                    f"Select a flavor from category '{text}' to update its stock:",
                    reply_markup=kb
                )
                user_data[chat_id] = data
            else:
                kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
                kb.add("⬅️ Back", "❌ Cancel")
                bot.send_message(chat_id, "Choose a valid category from the list:", reply_markup=kb)
            return

        # 12) Фаза 'choose_flavor_actual' — получаем выбор одного вкуса и запрашиваем новую qty
        if phase == 'choose_flavor_actual':
            if text in ["⬅️ Back", "❌ Cancel"]:
                data.pop('edit_cat', None)
                data['edit_phase'] = 'choose_action'
                bot.send_message(chat_id, "Back to editing menu:", reply_markup=edit_action_keyboard())
                user_data[chat_id] = data
                return

            cat0 = data.get('edit_cat')
            if not cat0 or cat0 not in menu:
                bot.send_message(chat_id, "Error: category not found.", reply_markup=edit_action_keyboard())
                data.pop('edit_cat', None)
                data['edit_phase'] = 'choose_action'
                user_data[chat_id] = data
                return

            # Пытаемся сопоставить введённый текст с форматом "Flavor (current: X)"
            chosen_flavor = None
            for itm in menu[cat0]["flavors"]:
                name = itm["flavor"]
                display_label = f"{name} (current: {itm.get('stock', 0)})"
                if text == display_label:
                    chosen_flavor = name
                    break

            if not chosen_flavor:
                kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
                for itm in menu[cat0]["flavors"]:
                    if itm.get("stock", 0) > 0:
                        kb.add(f"{itm['flavor']} (current: {itm['stock']})")
                kb.add("⬅️ Back", "❌ Cancel")
                bot.send_message(chat_id, "Select a valid flavor (in stock > 0):", reply_markup=kb)
                return

            # Сохраняем выбранный вкус и просим ввести новую quantity
            data['edit_flavor'] = chosen_flavor
            data['edit_phase'] = 'enter_actual_qty'
            bot.send_message(
                chat_id,
                f"Enter the new stock quantity for '{chosen_flavor}':",
                reply_markup=types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
                .add("⬅️ Back", "❌ Cancel")
            )
            user_data[chat_id] = data
            return

        # 13) Фаза 'enter_actual_qty' — получаем новую qty и обновляем stock
        if phase == 'enter_actual_qty':
            if text in ["⬅️ Back", "❌ Cancel"]:
                data.pop('edit_cat', None)
                data.pop('edit_flavor', None)
                data['edit_phase'] = 'choose_action'
                bot.send_message(chat_id, "Back to editing menu:", reply_markup=edit_action_keyboard())
                user_data[chat_id] = data
                return

            # Проверяем, что введён неотрицательный integer
            if not text.isdigit():
                bot.send_message(chat_id, "Invalid number. Please enter a non-negative integer:")
                return

            new_qty = int(text)
            cat0 = data.get('edit_cat')
            flavor0 = data.get('edit_flavor')

            # Находим и обновляем соответствующий объект вкуса
            updated = False
            for itm in menu.get(cat0, {}).get("flavors", []):
                if itm["flavor"] == flavor0:
                    itm["stock"] = new_qty
                    updated = True
                    break

            if not updated:
                bot.send_message(chat_id, f"Error: flavor '{flavor0}' not found in '{cat0}'.",
                                 reply_markup=edit_action_keyboard())
            else:
                # Сохраняем JSON на диск
                with open(MENU_PATH, "w", encoding="utf-8") as f:
                    json.dump(menu, f, ensure_ascii=False, indent=2)

                bot.send_message(
                    chat_id,
                    f"Stock for '{flavor0}' in category '{cat0}' has been updated to {new_qty}.",
                    reply_markup=edit_action_keyboard()
                )

            # Очищаем данные и возвращаемся в главное меню редактирования
            data.pop('edit_cat', None)
            data.pop('edit_flavor', None)
            data['edit_phase'] = 'choose_action'
            user_data[chat_id] = data
            return

        # Если ни одна фаза не совпала, возвращаем пользователя в меню редактирования
        data['edit_phase'] = 'choose_action'
        bot.send_message(chat_id, "Back to editing menu:", reply_markup=edit_action_keyboard())
        user_data[chat_id] = data
        return
    # ────────────────────────────────────────────────────────────────────────────────

    # Остальной universal_handler (cart-функции, /history, /stats, /help и т.д.)
    # ... (тот же код, что и ранее, без изменений) ...

    # ——— Режим редактирования корзины — (оставляем без изменений) ———
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
                    data['edit_index'] = None
                    user_data[chat_id] = data
                    return

                key_to_remove, _ = items_list[idx]
                new_cart = [it for it in data['cart'] if not (
                            it['category'] == key_to_remove[0] and it['flavor'] == key_to_remove[1] and it['price'] ==
                            key_to_remove[2])]
                data['cart'] = new_cart
                data['edit_cart_phase'] = None
                data['edit_index'] = None
                bot.send_message(chat_id, t(chat_id, "item_removed").format(flavor=key_to_remove[1]),
                                 reply_markup=get_inline_main_menu(chat_id))
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
                    f"Current item: {cat0} — {flavor0} — {price0}₺ (in cart {count} pcs).\n{t(chat_id, 'enter_new_qty')}"
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

            data['cart'] = [it for it in data['cart'] if
                            not (it['category'] == cat0 and it['flavor'] == flavor0 and it['price'] == price0)]
            for _ in range(new_qty):
                data['cart'].append({'category': cat0, 'flavor': flavor0, 'price': price0})

            data['edit_cart_phase'] = None
            data['edit_index'] = None
            if new_qty == 0:
                bot.send_message(chat_id, t(chat_id, "item_removed").format(flavor=flavor0),
                                 reply_markup=get_inline_main_menu(chat_id))
            else:
                bot.send_message(chat_id, t(chat_id, "qty_changed").format(flavor=flavor0, qty=new_qty),
                                 reply_markup=get_inline_main_menu(chat_id))
            user_data[chat_id] = data
            return

    # ——— «Корзина» через Reply-кнопку ———
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
        bot.send_message(chat_id, t(chat_id, "item_removed").format(flavor=flavor0),
                         reply_markup=get_inline_main_menu(chat_id))
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
            cart = data.get('cart', [])
            if not cart:
                bot.send_message(chat_id, t(chat_id, "cart_empty"))
                return

            total_try = sum(i['price'] for i in cart)
            discount = data.pop("pending_discount", 0)
            total_after = max(total_try - discount, 0)

            needed = {}
            for it in cart:
                key = (it["category"], it["flavor"])
                needed[key] = needed.get(key, 0) + 1

            for (cat0, flavor0), qty_needed in needed.items():
                item_obj = next((i for i in menu[cat0]["flavors"] if i["flavor"] == flavor0), None)
                if not item_obj or item_obj.get("stock", 0) < qty_needed:
                    bot.send_message(chat_id, f"😕 К сожалению, «{flavor0}» больше не доступен в нужном количестве.")
                    return

            for (cat0, flavor0), qty_needed in needed.items():
                for itm in menu[cat0]["flavors"]:
                    if itm["flavor"] == flavor0:
                        itm["stock"] -= qty_needed
                        break
            with open(MENU_PATH, "w", encoding="utf-8") as f:
                json.dump(menu, f, ensure_ascii=False, indent=2)

            items_json = json.dumps(cart, ensure_ascii=False)
            now = datetime.datetime.utcnow().isoformat()
            conn_local = get_db_connection()
            cursor_local = conn_local.cursor()
            cursor_local.execute(
                "INSERT INTO orders (chat_id, items_json, total, timestamp) VALUES (?, ?, ?, ?)",
                (chat_id, items_json, total_after, now)
            )
            conn_local.commit()

            earned = total_after // 30
            if earned > 0:
                cursor_local.execute("UPDATE users SET points = points + ? WHERE chat_id = ?", (earned, chat_id))
                conn_local.commit()
                bot.send_message(chat_id, f"👍 Вы получили {earned} бонусных баллов за этот заказ.")

            cursor_local.execute("SELECT referred_by FROM users WHERE chat_id = ?", (chat_id,))
            row = cursor_local.fetchone()
            if row and row[0]:
                inviter = row[0]
                cursor_local.execute("UPDATE users SET points = points + 200 WHERE chat_id = ?", (inviter,))
                conn_local.commit()
                bot.send_message(inviter, "🎉 Вам начислено 200 бонусных баллов за приглашение нового клиента!")
                cursor_local.execute("UPDATE users SET referred_by = NULL WHERE chat_id = ?", (chat_id,))
                conn_local.commit()

            cursor_local.close()
            conn_local.close()

            summary = "\n".join(f"{i['category']}: {i['flavor']} — {i['price']}₺" for i in cart)
            rates = fetch_rates()
            rub = round(total_after * rates.get("RUB", 0) + 500, 2)
            usd = round(total_after * rates.get("USD", 0) + 2, 2)
            uah = round(total_after * rates.get("UAH", 0) + 200, 2)
            conv = f"({rub}₽, ${usd}, ₴{uah})"

            full_rus = (
                f"📥 Новый заказ от @{message.from_user.username or message.from_user.first_name}:\n\n"
                f"{summary}\n\n"
                f"Итог: {total_after}₺ {conv}\n"
                f"📍 Адрес: {data.get('address', '—')}\n"
                f"📱 Контакт: {data.get('contact', '—')}\n"
                f"💬 Комментарий: {data.get('comment', '—')}"
            )
            bot.send_message(PERSONAL_CHAT_ID, full_rus)

            comment_ru = data.get('comment', '')
            comment_en = translate_to_en(comment_ru) if comment_ru else "—"
            full_en = (
                f"📥 New order from @{message.from_user.username or message.from_user.first_name}:\n\n"
                f"{summary}\n\n"
                f"Total: {total_after}₺ {conv}\n"
                f"📍 Address: {data.get('address', '—')}\n"
                f"📱 Contact: {data.get('contact', '—')}\n"
                f"💬 Comment: {comment_en}"
            )
            # вместо:
            # bot.send_message(GROUP_CHAT_ID, full_en)

            # создаём клавиатуру
            kb = types.InlineKeyboardMarkup()
            kb.add(types.InlineKeyboardButton(
                text="❌ Отменить заказ",
                callback_data=f"cancel_order|{order_id}"
            ))

            # отправляем админам вместе с кнопкой
            bot.send_message(
                GROUP_CHAT_ID,
                full_en,
                reply_markup=kb
            )

    # ────────────────────────────────────────────────────────────────────────────────

    # ——— Список команд ———
    if text.strip() == "/":
        commands_text = (
            "<b>Available commands:</b>\n\n"
            "/start           — Restart bot / registration\n"
            "/points          — Check your bonus points balance\n"
            "/convert [amount] — Currency rates and conversion TRY → RUB/USD/UAH\n"
            "/review <flavor>   — Leave a review for a flavor\n"
            "/show_reviews <flavor> — Show reviews for a flavor\n"
            "/history         — Show your last orders\n"
            "/stats           — Store statistics (ADMIN only)\n"
            "/help            — This help message\n"
        )
        bot.send_message(chat_id, commands_text, parse_mode="HTML")
        return

    # ——— Команда /help ———
    if text == "/help":
        commands_text = (
            "<b>Help: available commands</b>\n\n"
            "/start           — Restart bot / registration\n"
            "/points          — Check your bonus points balance\n"
            "/convert [amount] — Currency rates and conversion TRY → RUB/USD/UAH\n"
            "/review <flavor>   — Leave a review for a flavor\n"
            "/show_reviews <flavor> — Show reviews for a flavor\n"
            "/history         — Show your last orders\n"
            "/stats           — Store statistics (ADMIN only)\n"
            "/help            — This help message\n"
        )
        bot.send_message(chat_id, commands_text, parse_mode="HTML")
        return

    # ——— «Back» во всём остальном ———
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

    # ——— «Add more» ———
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

        conn_local = get_db_connection()
        cursor_local = conn_local.cursor()
        cursor_local.execute("SELECT points FROM users WHERE chat_id = ?", (chat_id,))
        row = cursor_local.fetchone()
        cursor_local.close()
        conn_local.close()

        user_points = row[0] if row else 0

        if user_points > 0:
            max_points = min(user_points, total_try)
            points_try = user_points * 1
            msg = (
                    t(chat_id, "points_info").format(points=user_points, points_try=points_try)
                    + "\n"
                    + t(chat_id, "enter_points").format(max_points=max_points)
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
        bot.send_message(chat_id, f"{t(chat_id, 'choose_flavor')} «{text}»",
                         reply_markup=get_inline_flavors(chat_id, text))
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
                label = f"{emoji} {flavor0} — {price}₺ [{it['stock']} шт]"
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
        conn_local = get_db_connection()
        cursor_local = conn_local.cursor()
        cursor_local.execute(
            "SELECT order_id, items_json, total, timestamp FROM orders WHERE chat_id = ? ORDER BY timestamp DESC",
            (chat_id,)
        )
        rows = cursor_local.fetchall()
        cursor_local.close()
        conn_local.close()

        if not rows:
            bot.send_message(chat_id, "У вас пока нет сохранённых заказов.")
            return
        texts = []
        for order_id, items_json, total, timestamp in rows[:10]:
            items = json.loads(items_json)
            summary = "\n".join(f"{i['flavor']} — {i['price']}₺" for i in items)
            date = timestamp.split("T")[0]
            texts.append(f"Заказ #{order_id} ({date}):\n{summary}\nИтого: {total}₺")
        bot.send_message(chat_id, "\n\n".join(texts))
        return

    # ——— /show_reviews ———
    if text.startswith("/show_reviews"):
        parts = text.split(maxsplit=1)
        if len(parts) != 2:
            bot.send_message(chat_id, "Использование: /show_reviews <название_вкуса>")
            return
        flavor_query = parts[1]

        conn_local = get_db_connection()
        cursor_local = conn_local.cursor()
        cursor_local.execute(
            "SELECT chat_id, rating, comment, timestamp FROM reviews WHERE flavor = ? ORDER BY timestamp DESC",
            (flavor_query,)
        )
        rows = cursor_local.fetchall()
        cursor_local.close()
        conn_local.close()

        if not rows:
            bot.send_message(chat_id, "Пока нет отзывов для этого вкуса.")
            return
        texts = []
        for uid, rating, comment, ts in rows[:10]:
            date = ts.split("T")[0]
            texts.append(f"👤 {uid} [{rating}⭐]\n🕒 {date}\n«{comment}»")
        bot.send_message(chat_id, "\n\n".join(texts))
        return

        # ——— /stats ———
    if text == "/stats":
        if chat_id not in (ADMIN_ID, ADMIN_ID_TWO):
            bot.send_message(chat_id, "У вас нет доступа к этой команде.")
            return

        conn_local = get_db_connection()
        cursor_local = conn_local.cursor()
        cursor_local.execute("SELECT COUNT(*) FROM orders")
        total_orders = cursor_local.fetchone()[0]
        cursor_local.execute("SELECT SUM(total) FROM orders")
        total_revenue = cursor_local.fetchone()[0] or 0
        cursor_local.execute("SELECT items_json FROM orders")
        all_items = cursor_local.fetchall()

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
        cursor_local.close()
        conn_local.close()

        bot.send_message(chat_id, report)
        return





@bot.callback_query_handler(func=lambda call: call.data and call.data.startswith("cancel_order|"))
def handle_cancel_order(call):
    # 1) Проверяем, что админ
    if call.from_user.id not in (ADMIN_ID, ADMIN_ID_TWO):
        return bot.answer_callback_query(call.id, "Нет доступа", show_alert=True)

    # 2) Извлекаем order_id
    _, oid = call.data.split("|", 1)
    order_id = int(oid)

    # 3) Достаём заказ из БД
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT chat_id, items_json, points_earned FROM orders WHERE order_id = ?",
        (order_id,)
    )
    row = cursor.fetchone()
    if not row:
        cursor.close()
        conn.close()
        return bot.answer_callback_query(call.id, "Заказ не найден", show_alert=True)
    user_chat_id, items_json, pts_earned = row

    # 4) Возвращаем товары на склад
    items = json.loads(items_json)
    for it in items:
        for itm in menu[it["category"]]["flavors"]:
            if itm["flavor"] == it["flavor"]:
                itm["stock"] = itm.get("stock", 0) + 1
                break
    with open(MENU_PATH, "w", encoding="utf-8") as f:
        json.dump(menu, f, ensure_ascii=False, indent=2)

    # 5) Списываем баллы
    if pts_earned:
        cursor.execute(
            "UPDATE users SET points = points - ? WHERE chat_id = ?",
            (pts_earned, user_chat_id)
        )
        conn.commit()

    # 6) Удаляем заказ
    cursor.execute("DELETE FROM orders WHERE order_id = ?", (order_id,))
    conn.commit()
    cursor.close()
    conn.close()

    # 7) Уведомляем пользователя
    bot.send_message(
        user_chat_id,
        f"Ваш заказ #{order_id} отменён, {pts_earned} бонусных баллов списано."
    )

    # 8) Убираем кнопку в админском сообщении
    bot.edit_message_reply_markup(
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        reply_markup=None
    )
    bot.answer_callback_query(call.id, "Заказ отменён")



# ------------------------------------------------------------------------
#   36. Запуск бота
# ------------------------------------------------------------------------
if __name__ == "__main__":
    bot.delete_webhook()
    bot.polling(none_stop=True)
