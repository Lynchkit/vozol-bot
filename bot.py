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
#   4. Инициализация SQLite и создание таблиц (при старте)
# ------------------------------------------------------------------------
conn_init = get_db_connection()
cursor_init = conn_init.cursor()

cursor_init.execute("""
    CREATE TABLE IF NOT EXISTS users (
        chat_id        INTEGER PRIMARY KEY,
        points         INTEGER DEFAULT 0,
        referral_code  TEXT UNIQUE,
        referred_by    INTEGER
    )
""")
cursor_init.execute("""
    CREATE TABLE IF NOT EXISTS orders (
        order_id    INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id     INTEGER,
        items_json  TEXT,
        total       INTEGER,
        timestamp   TEXT
    )
""")
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

# Добавляем новые колонки в orders, если их ещё нет
for ddl in [
    "ALTER TABLE orders ADD COLUMN status TEXT DEFAULT 'completed'",
    "ALTER TABLE orders ADD COLUMN points_earned INTEGER DEFAULT 0",
    "ALTER TABLE orders ADD COLUMN points_spent INTEGER DEFAULT 0",
]:
    try:
        cursor_init.execute(ddl)
        conn_init.commit()
    except sqlite3.OperationalError:
        pass  # колонка уже существует

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

# ------------------------------------------------------------------------
#   6. Хранилище данных пользователей (in-memory)
# ------------------------------------------------------------------------
user_data = {}

# ------------------------------------------------------------------------
#   7. Утилиты
# ------------------------------------------------------------------------
def t(chat_id: int, key: str) -> str:
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
        label = cat
        if total_stock == 0:
            label += " (нет в наличии)" if lang == "ru" else " (out of stock)"
        kb.add(types.InlineKeyboardButton(text=label, callback_data=f"category|{cat}"))
    kb.add(types.InlineKeyboardButton(text=f"🛒 {t(chat_id,'view_cart')}", callback_data="view_cart"))
    kb.add(types.InlineKeyboardButton(text=f"🗑️ {t(chat_id,'clear_cart')}", callback_data="clear_cart"))
    kb.add(types.InlineKeyboardButton(text=f"✅ {t(chat_id,'finish_order')}", callback_data="finish_order"))
    return kb

# ------------------------------------------------------------------------
#   10. Inline-кнопки для выбора вкусов
# ------------------------------------------------------------------------
def get_inline_flavors(chat_id: int, cat: str) -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup(row_width=1)
    price = menu[cat]["price"]
    for item in menu[cat]["flavors"]:
        stock = item.get("stock", 0)
        if isinstance(stock, str) and stock.isdigit():
            stock = int(stock)
            item["stock"] = stock
        if stock > 0:
            emoji = item.get("emoji", "")
            flavor_name = item["flavor"]
            label = f"{emoji} {flavor_name} - {price}₺ [{stock}шт]"
            kb.add(types.InlineKeyboardButton(text=label, callback_data=f"flavor|{cat}|{flavor_name}"))
    kb.add(types.InlineKeyboardButton(text=f"⬅️ {t(chat_id,'back_to_categories')}", callback_data="go_back_to_categories"))
    return kb

# ------------------------------------------------------------------------
#   11. Reply-клавиатуры (адрес, контакт, комментарий)
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
#   12. Клавиатура редактирования меню (/change)
# ------------------------------------------------------------------------
def edit_action_keyboard() -> types.ReplyKeyboardMarkup:
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
    kb.add("➕ Add Category", "➖ Remove Category")
    kb.add("💲 Fix Price", "ALL IN", "🔄 Actual Flavor")
    kb.add("🖼️ Add Category Picture", "Set Category Flavor to 0")
    kb.add("⬅️ Back", "❌ Cancel")
    return kb

# ------------------------------------------------------------------------
#   13. Планировщик – еженедельный дайджест
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
#   14. /start – регистрация, выбор языка
# ------------------------------------------------------------------------
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
    data.update({
        "cart": [], "current_category": None,
        "wait_for_points": False, "wait_for_address": False,
        "wait_for_contact": False, "wait_for_comment": False,
        "address": "", "contact": "", "comment": "",
        "pending_discount": 0, "pending_points_spent": 0,
        "temp_total_try": 0, "temp_user_points": 0,
        "edit_phase": None, "edit_cat": None, "edit_flavor": None,
        "edit_index": None, "edit_cart_phase": None,
        "awaiting_review_flavor": None,
        "awaiting_review_rating": False,
        "awaiting_review_comment": False,
        "temp_review_flavor": None, "temp_review_rating": 0
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
#   15. выбор языка
# ------------------------------------------------------------------------
@bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("set_lang|"))
def handle_set_lang(call):
    chat_id = call.from_user.id
    _, lang_code = call.data.split("|", 1)
    if chat_id not in user_data:
        user_data[chat_id] = {"lang": lang_code}
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
#   16. выбор категории
# ------------------------------------------------------------------------
@bot.callback_query_handler(func=lambda call: call.data and call.data.startswith("category|"))
def handle_category(call):
    chat_id = call.from_user.id
    _, cat = call.data.split("|", 1)
    if cat not in menu:
        bot.answer_callback_query(call.id, t(chat_id, "error_invalid"))
        return
    user_data[chat_id]["current_category"] = cat
    photo_url = menu[cat].get("photo_url", "").strip()
    if photo_url:
        try:
            bot.send_photo(chat_id, photo_url)
        except Exception:
            pass
    bot.send_message(
        chat_id,
        f"{t(chat_id, 'choose_flavor')} «{cat}»",
        reply_markup=get_inline_flavors(chat_id, cat)
    ) 
    bot.answer_callback_query(call.id)

# ------------------------------------------------------------------------
#   17. назад к категориям
# ------------------------------------------------------------------------
@bot.callback_query_handler(func=lambda call: call.data == "go_back_to_categories")
def handle_go_back_to_categories(call):
    chat_id = call.from_user.id
    bot.answer_callback_query(call.id)
    bot.send_message(chat_id, t(chat_id, "choose_category"), reply_markup=get_inline_main_menu(chat_id))

# ------------------------------------------------------------------------
#   18. выбор вкуса
# ------------------------------------------------------------------------
@bot.callback_query_handler(func=lambda call: call.data and call.data.startswith("flavor|"))
def handle_flavor(call):
    _, cat, flavor = call.data.split("|", 2)
    chat_id = call.from_user.id
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
            text=f"➕ {t(chat_id,'add_to_cart')}",
            callback_data=f"add_to_cart|{cat}|{flavor}"
        )
    )
    kb.add(
        types.InlineKeyboardButton(
            text=f"⬅️ {t(chat_id,'back_to_categories')}",
            callback_data="go_back_to_categories"
        )
    )
    kb.add(
        types.InlineKeyboardButton(
            text=f"✅ {t(chat_id,'finish_order')}",
            callback_data="finish_order"
        )
    )
    bot.send_message(chat_id, t(chat_id, "choose_action"), reply_markup=kb)

# ------------------------------------------------------------------------
#   19. добавить в корзину
# ------------------------------------------------------------------------
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
#   20. просмотр корзины
# ------------------------------------------------------------------------
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
                text=f"{t(chat_id,'remove_item')} {idx}",
                callback_data=f"remove_item|{idx}"
            ),
            types.InlineKeyboardButton(
                text=f"{t(chat_id,'edit_item')} {idx}",
                callback_data=f"edit_item|{idx}"
            )
        )
    kb.add(
        types.InlineKeyboardButton(
            text=f"⬅️ {t(chat_id,'back_to_categories')}",
            callback_data="go_back_to_categories"
        )
    )
    bot.send_message(chat_id, msg, reply_markup=kb)

# ------------------------------------------------------------------------
#   21. удалить i
# ------------------------------------------------------------------------
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
#   22. редактирование i → ввод количества
# ------------------------------------------------------------------------
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
        bot.send_message(chat_id, t(chat_id, "item_removed").format(flavor=flavor), reply_markup=get_inline_main_menu(chat_id))
    else:
        bot.send_message(chat_id, t(chat_id, "qty_changed").format(flavor=flavor, qty=new_qty), reply_markup=get_inline_main_menu(chat_id))
    user_data[chat_id] = data

# ------------------------------------------------------------------------
#   23. очистить корзину
# ------------------------------------------------------------------------
@bot.callback_query_handler(func=lambda call: call.data == "clear_cart")
def handle_clear_cart(call):
    chat_id = call.from_user.id
    bot.answer_callback_query(call.id)
    data = user_data.get(chat_id, {})
    data["cart"] = []
    bot.send_message(chat_id, t(chat_id, "cart_cleared"), reply_markup=get_inline_main_menu(chat_id))
    user_data[chat_id] = data

# ------------------------------------------------------------------------
#   24. завершить заказ → ввод баллов или адрес
# ------------------------------------------------------------------------
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
#   25. ввод количества баллов для списания
# ------------------------------------------------------------------------
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
    total_after = total_try - discount_try
    kb = address_keyboard()
    summary_lines = [f"{item['category']}: {item['flavor']} — {item['price']}₺" for item in data.get("cart", [])]
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
#   26. ввод адреса
# ------------------------------------------------------------------------
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

# ------------------------------------------------------------------------
#   27. ввод контакта
# ------------------------------------------------------------------------
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

# ------------------------------------------------------------------------
#   28. ввод комментария и сохранение заказа (с учётом списка stock и баллов)
# ------------------------------------------------------------------------
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
        points_spent = data.pop("pending_points_spent", 0)
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
        points_earned = total_after // 30
        cursor_local.execute(
            """
            INSERT INTO orders
              (chat_id, items_json, total, timestamp, status, points_earned, points_spent)
            VALUES (?,?,?,?,?,?,?)
            """,
            (chat_id, items_json, total_after, now, 'completed', points_earned, points_spent)
        )
        conn_local.commit()
        if points_spent > 0:
            cursor_local.execute("UPDATE users SET points = points - ? WHERE chat_id = ?", (points_spent, chat_id))
        if points_earned > 0:
            cursor_local.execute("UPDATE users SET points = points + ? WHERE chat_id = ?", (points_earned, chat_id))
            bot.send_message(chat_id, f"👍 Вы получили {points_earned} бонусных баллов за этот заказ.")
        conn_local.commit()
        cursor_local.execute("SELECT referred_by FROM users WHERE chat_id = ?", (chat_id,))
        row = cursor_local.fetchone()
        if row and row[0]:
            inviter = row[0]
            cursor_local.execute("UPDATE users SET points = points + 200 WHERE chat_id = ?", (inviter,))
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
#   29. /change — меню редактирования (оставляем без изменений)
# ------------------------------------------------------------------------
@bot.message_handler(commands=['change'])
def cmd_change(message):
    chat_id = message.chat.id
    if chat_id not in user_data:
        user_data[chat_id] = {
            "lang": "ru",
            "cart": [], "current_category": None,
            "wait_for_points": False, "wait_for_address": False,
            "wait_for_contact": False, "wait_for_comment": False,
            "address": "", "contact": "", "comment": "",
            "pending_discount": 0, "pending_points_spent": 0,
            "temp_total_try": 0, "temp_user_points": 0,
            "edit_phase": None, "edit_cat": None,
            "edit_flavor": None, "edit_index": None,
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
#   30. /points
# ------------------------------------------------------------------------
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
#   31. /convert
# ------------------------------------------------------------------------
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
#   32. /review
# ------------------------------------------------------------------------
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
#   33. ввод оценки после /review
# ------------------------------------------------------------------------
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
#   34. ввод комментария или /skip после оценки
# ------------------------------------------------------------------------
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
    data["awaiting_review_flavor"] = None
    data["awaiting_review_rating"] = False
    data["awaiting_review_comment"] = False
    data["temp_review_flavor"] = None
    data["temp_review_rating"] = 0
    user_data[chat_id] = data

# ------------------------------------------------------------------------
#   35. universal_handler: остальные команды и fallback
# ------------------------------------------------------------------------
@bot.message_handler(content_types=['text','location','venue','contact'])
def universal_handler(message):
    chat_id = message.chat.id
    text = message.text or ""
    if chat_id not in user_data:
        user_data[chat_id] = {
            "lang": "ru", "cart": [], "current_category": None,
            "wait_for_points": False, "wait_for_address": False,
            "wait_for_contact": False, "wait_for_comment": False,
            "address": "", "contact": "", "comment": "",
            "pending_discount": 0, "pending_points_spent": 0,
            "temp_total_try": 0, "temp_user_points": 0,
            "edit_phase": None, "edit_cat": None,
            "edit_flavor": None, "edit_index": None,
            "edit_cart_phase": None,
            "awaiting_review_flavor": None,
            "awaiting_review_rating": False,
            "awaiting_review_comment": False,
            "temp_review_flavor": None,
            "temp_review_rating": 0
        }
    data = user_data[chat_id]
    # здесь можно добавить логику fallback, как /history, /show_reviews, /stats, /help и т.д.
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
    if text == "/stats":
        if chat_id != ADMIN_ID:
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
    if text == "/":
        bot.send_message(chat_id, "/help — показать список команд")
        return
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
    if text == f"➕ {t(chat_id, 'add_more')}":
        data["current_category"] = None
        bot.send_message(chat_id, t(chat_id, "choose_category"), reply_markup=get_inline_main_menu(chat_id))
        user_data[chat_id] = data
        return
    if text == f"✅ {t(chat_id, 'finish_order')}":
        handle_finish_order(types.SimpleNamespace(data="finish_order", from_user=message.from_user, message=message))
        return
    if text in menu:
        data['current_category'] = text
        bot.send_message(chat_id, f"{t(chat_id, 'choose_flavor')} «{text}»", reply_markup=get_inline_flavors(chat_id, text))
        user_data[chat_id] = data
        return
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

# ------------------------------------------------------------------------
#   команда /cancel_order для отмены заказа
# ------------------------------------------------------------------------
@bot.message_handler(commands=['cancel_order'])
def cmd_cancel_order(message):
    chat_id = message.chat.id
    if chat_id != ADMIN_ID:
        bot.send_message(chat_id, "❌ У вас нет доступа к этой команде.")
        return
    parts = message.text.split()
    if len(parts) != 2 or not parts[1].isdigit():
        bot.send_message(chat_id, "Использование: /cancel_order <order_id>")
        return
    order_id = int(parts[1])
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute(
        "SELECT chat_id, items_json, status, points_earned, points_spent FROM orders WHERE order_id = ?",
        (order_id,)
    )
    row = cur.fetchone()
    if not row:
        bot.send_message(chat_id, f"❓ Заказ #{order_id} не найден.")
        cur.close()
        conn.close()
        return
    user_id, items_json, status, earned, spent = row
    if status == 'cancelled':
        bot.send_message(chat_id, f"ℹ️ Заказ #{order_id} уже отменён.")
        cur.close()
        conn.close()
        return
    items = json.loads(items_json)
    for it in items:
        cat, flavor = it['category'], it['flavor']
        for itm in menu[cat]['flavors']:
            if itm['flavor'] == flavor:
                itm['stock'] += 1
                break
    with open(MENU_PATH, 'w', encoding='utf-8') as f:
        json.dump(menu, f, ensure_ascii=False, indent=2)
    cur.execute(
        "UPDATE orders SET status = 'cancelled' WHERE order_id = ?",
        (order_id,)
    )
    if earned > 0:
        cur.execute(
            "UPDATE users SET points = points - ? WHERE chat_id = ?",
            (earned, user_id)
        )
    # Чтобы вернуть списанные баллы, раскомментируйте:
    # if spent > 0:
    #     cur.execute(
    #         "UPDATE users SET points = points + ? WHERE chat_id = ?",
    #         (spent, user_id)
    #     )
    conn.commit()
    cur.close()
    conn.close()
    bot.send_message(
        chat_id,
        f"✅ Заказ #{order_id} отменён. Начисленные {earned} б. баллов списаны."
    )
    bot.send_message(
        user_id,
        f"⚠️ Ваш заказ #{order_id} был отменён. Начисленные за него {earned} б. баллов списаны."
    )

# ------------------------------------------------------------------------
#   36. Запуск бота
# ------------------------------------------------------------------------
if __name__ == "__main__":
    bot.delete_webhook()
    bot.polling(none_stop=True)
