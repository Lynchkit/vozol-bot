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

# —————————————————————————————————————————————————————————————
#   1. Загрузка переменных окружения
# —————————————————————————————————————————————————————————————
TOKEN = os.getenv("TOKEN")
if not TOKEN:
    raise RuntimeError(
        "Переменная окружения TOKEN не задана! "
        "Запустите контейнер с -e TOKEN=<ваш_токен>."
    )

ADMIN_ID = int(os.getenv("ADMIN_ID", "424751188"))
GROUP_CHAT_ID = int(os.getenv("GROUP_CHAT_ID", "0"))       # Например: -1002414380144
PERSONAL_CHAT_ID = int(os.getenv("PERSONAL_CHAT_ID", "0"))

bot = telebot.TeleBot(TOKEN, parse_mode="HTML")

# —————————————————————————————————————————————————————————————
#   2. Пути к JSON-файлам и БД
# —————————————————————————————————————————————————————————————
MENU_PATH = "menu.json"
LANG_PATH = "languages.json"
DB_PATH = "/data/database.db"

# —————————————————————————————————————————————————————————————
#   3. Функция для получения локального подключения к БД
# —————————————————————————————————————————————————————————————
def get_db_connection():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    return conn

# —————————————————————————————————————————————————————————————
#   4. Инициализация SQLite и создание таблиц (при старте)
# —————————————————————————————————————————————————————————————
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
cursor_init.close()
conn_init.close()

# —————————————————————————————————————————————————————————————
#   5. Загрузка menu.json и languages.json
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
#   6. Хранилище данных пользователей (in-memory)
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
#   "wait_for_order_comment": False,
#   "address": str,
#   "contact": str,
#   "order_comment": str,
#   "pending_discount": int,
#   "pending_points_spent": int,
#   "temp_total_try": int,
#   "temp_user_points": int,
#   "edit_phase": None / str,
#   "edit_cat": None / str,
#   "edit_flavor": None / str,
#   "edit_index": None / int,
#   "edit_cart_phase": None / str,
#   # Новые поля для /review
#   "pending_review_flavor": None,
#   "pending_review_category": None,
#   "wait_for_review_rating": False,
#   "pending_review_rating": None,
#   "wait_for_review_comment": False
# }

# —————————————————————————————————————————————————————————————
#   7. Хелперы
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
#   8. Inline-кнопки для выбора языка
# —————————————————————————————————————————————————————————————
def get_inline_language_buttons(chat_id: int) -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        types.InlineKeyboardButton(text="Русский 🇷🇺", callback_data="set_lang|ru"),
        types.InlineKeyboardButton(text="English 🇬🇧", callback_data="set_lang|en")
    )
    return kb

# —————————————————————————————————————————————————————————————
#   9. Inline-кнопки для главного меню 
# —————————————————————————————————————————————————————————————
def get_inline_main_menu(chat_id: int) -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup(row_width=2)
    for cat in menu.keys():
        kb.add(types.InlineKeyboardButton(text=cat, callback_data=f"category|{cat}"))
    kb.add(types.InlineKeyboardButton(text=f"🛒 {t(chat_id,'view_cart')}", callback_data="view_cart"))
    kb.add(types.InlineKeyboardButton(text=f"🗑️ {t(chat_id,'clear_cart')}", callback_data="clear_cart"))
    kb.add(types.InlineKeyboardButton(text=f"✅ {t(chat_id,'finish_order')}", callback_data="finish_order"))
    return kb

# —————————————————————————————————————————————————————————————
#   10. Inline-кнопки для выбора вкусов
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
    kb.add(types.InlineKeyboardButton(text=f"⬅️ {t(chat_id,'back_to_categories')}", callback_data="go_back_to_categories"))
    return kb

# —————————————————————————————————————————————————————————————
#   11. Reply-клавиатуры (альтернатива inline)
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
#   12. Клавиатура редактирования меню (/change) — ВСЁ НА АНГЛИЙСКОМ
# —————————————————————————————————————————————————————————————
def edit_action_keyboard() -> types.ReplyKeyboardMarkup:
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
    kb.add("➕ Add Category", "➖ Remove Category")
    kb.add("💲 Fix Price", "ALL IN", "🔄 Actual Flavor")
    kb.add("⬅️ Back", "❌ Cancel")
    return kb

# —————————————————————————————————————————————————————————————
#   13. Планировщик – еженедельный дайджест (необязательно)
# —————————————————————————————————————————————————————————————
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
        text = "📢 За прошедшую неделю не было продаж."
    else:
        lines = [f"{flavor}: {qty} продаж" for flavor, qty in top3]
        text = "📢 Топ-3 вкуса за неделю:\n" + "\n".join(lines)
    cursor_local.execute("SELECT DISTINCT chat_id FROM orders")
    users = cursor_local.fetchall()
    for (uid,) in users:
        bot.send_message(uid, text)

    cursor_local.close()
    conn_local.close()

scheduler = BackgroundScheduler(timezone="Europe/Riga")
scheduler.add_job(send_weekly_digest, trigger="cron", day_of_week="mon", hour=9, minute=0)
scheduler.start()

# —————————————————————————————————————————————————————————————
#   14. Хендлер /start – регистрация, реферальная система, выбор языка
# —————————————————————————————————————————————————————————————
@bot.message_handler(commands=['start'])
def cmd_start(message):
    chat_id = message.chat.id

    # Если пользователя нет, создаём запись в user_data с default-значениями (lang=None)
    if chat_id not in user_data:
        user_data[chat_id] = {
            "lang": None,
            "cart": [],
            "current_category": None,
            "wait_for_points": False,
            "wait_for_address": False,
            "wait_for_contact": False,
            "wait_for_order_comment": False,
            "address": "",
            "contact": "",
            "order_comment": "",
            "pending_discount": 0,
            "pending_points_spent": 0,
            "temp_total_try": 0,
            "temp_user_points": 0,
            "edit_phase": None,
            "edit_cat": None,
            "edit_flavor": None,
            "edit_index": None,
            "edit_cart_phase": None,
            # Новые поля для /review
            "pending_review_flavor": None,
            "pending_review_category": None,
            "wait_for_review_rating": False,
            "pending_review_rating": None,
            "wait_for_review_comment": False
        }
    data = user_data[chat_id]

    # Сбрасываем всё, кроме lang
    data.update({
        "cart": [],
        "current_category": None,
        "wait_for_points": False,
        "wait_for_address": False,
        "wait_for_contact": False,
        "wait_for_order_comment": False,
        "address": "",
        "contact": "",
        "order_comment": "",
        "pending_discount": 0,
        "pending_points_spent": 0,
        "temp_total_try": 0,
        "temp_user_points": 0,
        "edit_phase": None,
        "edit_cat": None,
        "edit_flavor": None,
        "edit_index": None,
        "edit_cart_phase": None,
        # Сбросить review-поля
        "pending_review_flavor": None,
        "pending_review_category": None,
        "wait_for_review_rating": False,
        "pending_review_rating": None,
        "wait_for_review_comment": False
    })

    # Проверяем наличие пользователя в БД и создаём, если нет
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

    # Показываем кнопку выбора языка
    bot.send_message(
        chat_id,
        t(chat_id, "choose_language"),
        reply_markup=get_inline_language_buttons(chat_id)
    )

# —————————————————————————————————————————————————————————————
#   15. Callback: выбор языка
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
            "wait_for_order_comment": False,
            "address": "",
            "contact": "",
            "order_comment": "",
            "pending_discount": 0,
            "pending_points_spent": 0,
            "temp_total_try": 0,
            "temp_user_points": 0,
            "edit_phase": None,
            "edit_cat": None,
            "edit_flavor": None,
            "edit_index": None,
            "edit_cart_phase": None,
            # Новые поля для /review
            "pending_review_flavor": None,
            "pending_review_category": None,
            "wait_for_review_rating": False,
            "pending_review_rating": None,
            "wait_for_review_comment": False
        }
    else:
        user_data[chat_id]["lang"] = lang_code

    bot.answer_callback_query(call.id, t(chat_id, "lang_set"))
    bot.send_message(chat_id, t(chat_id, "choose_category"), reply_markup=get_inline_main_menu(chat_id))

    # Отправляем реферальную ссылку с предложением "Зарабатывай баллы"
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
            # English message remains as before
            bot.send_message(
                chat_id,
                f"Your referral code: {code}\n"
                f"Share this link with friends:\n{ref_link}"
            )
        else:
            # Russian: добавляем "Зарабатывай баллы!" перед текстом
            bot.send_message(
                chat_id,
                f"Зарабатывай баллы!\nВаш реферальный код: {code}\n"
                f"Поделитесь этой ссылкой с друзьями:\n{ref_link}"
            )

# —————————————————————————————————————————————————————————————
#   16. Callback: выбор категории (показываем вкусы)
# —————————————————————————————————————————————————————————————
@bot.callback_query_handler(func=lambda call: call.data and call.data.startswith("category|"))
def handle_category(call):
    chat_id = call.from_user.id
    _, cat = call.data.split("|", 1)

    # Если категория не найдена, отвечаем ошибкой
    if cat not in menu:
        bot.answer_callback_query(call.id, t(chat_id, "error_invalid"))
        return

    # Подтверждаем нажатие (убираем «часики»)
    bot.answer_callback_query(call.id)

    # Сохраняем, что сейчас пользователь в этой категории
    user_data[chat_id]["current_category"] = cat

    # 1) Если в menu[cat] есть photo_url, отправляем её
    photo_url = menu[cat].get("photo_url", "").strip()
    if photo_url:
        try:
            bot.send_photo(chat_id, photo_url)
        except Exception as e:
            print(f"Failed to send category photo for {cat}: {e}")

    # 2) Отправляем текст «Выберите вкус...» + кнопки со вкусами
    bot.send_message(
        chat_id,
        f"{t(chat_id, 'choose_flavor')} «{cat}»",
        reply_markup=get_inline_flavors(chat_id, cat)
    )

# —————————————————————————————————————————————————————————————
#   17. Callback: «Назад к категориям»
# —————————————————————————————————————————————————————————————
@bot.callback_query_handler(func=lambda call: call.data == "go_back_to_categories")
def handle_go_back_to_categories(call):
    chat_id = call.from_user.id
    bot.answer_callback_query(call.id)
    bot.send_message(chat_id, t(chat_id, "choose_category"), reply_markup=get_inline_main_menu(chat_id))

# —————————————————————————————————————————————————————————————
#   18. Callback: выбор вкуса
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

    # Inline-клавиатура: добавить в корзину, назад к категориям, завершить заказ
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

# —————————————————————————————————————————————————————————————
#   19. Callback: добавить в корзину (без изменения stock)
# —————————————————————————————————————————————————————————————
@bot.callback_query_handler(func=lambda call: call.data and call.data.startswith("add_to_cart|"))
def handle_add_to_cart(call):
    chat_id = call.from_user.id
    _, cat, flavor = call.data.split("|", 2)

    # Проверяем наличие ОДНОГО товара в stock, но не уменьшаем его здесь
    item_obj = next((i for i in menu[cat]["flavors"] if i["flavor"] == flavor), None)
    if not item_obj or item_obj.get("stock", 0) <= 0:
        bot.answer_callback_query(call.id, t(chat_id, "error_out_of_stock"))
        return

    bot.answer_callback_query(call.id)
    data = user_data.setdefault(chat_id, {})
    cart = data.setdefault("cart", [])

    price = menu[cat]["price"]
    cart.append({"category": cat, "flavor": flavor, "price": price})

    # Сообщаем пользователю, что товар добавлен
    template = t(chat_id, "added_to_cart")
    suffix = template.split("»", 1)[1].strip()
    count = len(cart)
    bot.send_message(
        chat_id,
        f"«{cat} — {flavor}» {suffix.format(flavor=flavor, count=count)}",
        reply_markup=get_inline_main_menu(chat_id)
    )

# —————————————————————————————————————————————————————————————
#   20. Callback: «Просмотр корзины»
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

    # Inline-кнопки «Удалить i» и «Изменить i»
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
    # Кнопка «Назад к категориям»
    kb.add(
        types.InlineKeyboardButton(
            text=f"⬅️ {t(chat_id,'back_to_categories')}",
            callback_data="go_back_to_categories"
        )
    )
    bot.send_message(chat_id, msg, reply_markup=kb)

# —————————————————————————————————————————————————————————————
#   21. Callback: «Удалить i» из корзины (без возврата stock)
# —————————————————————————————————————————————————————————————
@bot.callback_query_handler(func=lambda call: call.data and call.data.startswith("remove_item|"))
def handle_remove_item(call):
    chat_id = call.from_user.id
    _, idx_str = call.data.split("|", 1)
    idx = int(idx_str) - 1
    data = user_data.get(chat_id, {})
    cart = data.get("cart", [])

    # Собираем сгруппированный список (категория, вкус, цена) → количество
    grouped = {}
    for item in cart:
        key = (item["category"], item["flavor"], item["price"])
        grouped[key] = grouped.get(key, 0) + 1
    items_list = list(grouped.items())

    if idx < 0 or idx >= len(items_list):
        bot.answer_callback_query(call.id, t(chat_id, "error_invalid"))
        return

    (cat, flavor, price), qty = items_list[idx]

    # Удаляем ровно одну единицу из корзины (без изменения stock)
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
#   22. Callback: «Изменить i» в корзине → ввод количества (без возврата/списания stock)
# —————————————————————————————————————————————————————————————
@bot.callback_query_handler(func=lambda call: call.data and call.data.startswith("edit_item|"))
def handle_edit_item_request(call):
    chat_id = call.from_user.id
    _, idx_str = call.data.split("|", 1)
    idx = int(idx_str) - 1
    data = user_data.get(chat_id, {})
    cart = data.get("cart", [])

    # 1) Составляем сгруппированный список, чтобы узнать old_qty
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

    # Пересобираем корзину: убираем все старые вхождения и добавляем new_qty штук
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

# —————————————————————————————————————————————————————————————
#   23. Callback: «Очистить корзину»
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
#   24. Callback: завершить заказ (проверка баллов и списание stock)
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

    conn_local = get_db_connection()
    cursor_local = conn_local.cursor()
    cursor_local.execute("SELECT points FROM users WHERE chat_id = ?", (chat_id,))
    row = cursor_local.fetchone()
    cursor_local.close()
    conn_local.close()

    user_points = row[0] if row else 0

    if user_points > 0:
        max_points = min(user_points, total_try)
        # Показываем информацию о баллах и просим ввести, сколько потратить
        points_try = user_points * 1  # 1 балл = 1₺
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
#   25. Handler: ввод количества баллов для списания
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
    user_points = data.get("temp_user_points", 0)
    total_try = data.get("temp_total_try", 0)
    max_points = min(user_points, total_try)

    if points_to_spend < 0 or points_to_spend > max_points:
        bot.send_message(chat_id, t(chat_id, "invalid_points").format(max_points=max_points))
        return

    # Списываем баллы из БД (если points_to_spend > 0)
    if points_to_spend > 0:
        conn_local = get_db_connection()
        cursor_local = conn_local.cursor()
        cursor_local.execute("UPDATE users SET points = points - ? WHERE chat_id = ?", (points_to_spend, chat_id))
        conn_local.commit()
        cursor_local.close()
        conn_local.close()

    # Вычисляем скидку и сохраняем в data
    discount_try = points_to_spend * 1  # 1 балл = 1₺
    data["pending_discount"] = discount_try
    data["pending_points_spent"] = points_to_spend

    # Сбрасываем флаг, дальше — ввод адреса
    data["wait_for_points"] = False

    # Формируем сводку корзины и просим ввести адрес
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
#   26. Handler: ввод адреса (без изменений)
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
#   27. Handler: ввод контакта (без изменений)
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
    data['wait_for_order_comment'] = True
    kb = comment_keyboard()
    bot.send_message(chat_id, t(chat_id, "enter_comment"), reply_markup=kb)
    user_data[chat_id] = data

# —————————————————————————————————————————————————————————————
#   28. Handler: ввод комментария к заказу и сохранение заказа (списание stock)
# —————————————————————————————————————————————————————————————
@bot.message_handler(
    func=lambda m: user_data.get(m.chat.id, {}).get("wait_for_order_comment"),
    content_types=['text']
)
def handle_order_comment_input(message):
    chat_id = message.chat.id
    data = user_data.get(chat_id, {})
    text = message.text or ""

    if text == t(chat_id, "back"):
        data['wait_for_contact'] = True
        data['wait_for_order_comment'] = False
        bot.send_message(chat_id, t(chat_id, "enter_contact"), reply_markup=contact_keyboard())
        user_data[chat_id] = data
        return

    if text == t(None, "enter_comment"):
        bot.send_message(chat_id, t(chat_id, "enter_comment"), reply_markup=types.ReplyKeyboardRemove())
        return

    if message.content_type == 'text' and text != t(None, "send_order"):
        data['order_comment'] = text.strip()
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

        # —— Проверяем наличие и списываем stock ——
        needed = {}
        for it in cart:
            key = (it["category"], it["flavor"])
            needed[key] = needed.get(key, 0) + 1

        for (cat0, flavor0), qty_needed in needed.items():
            item_obj = next((i for i in menu[cat0]["flavors"] if i["flavor"] == flavor0), None)
            if not item_obj or item_obj.get("stock", 0) < qty_needed:
                bot.send_message(chat_id, f"😕 К сожалению, «{flavor0}» больше не доступен в нужном количестве.")
                return

        # Если всё ок, снимаем с menu.json
        for (cat0, flavor0), qty_needed in needed.items():
            for itm in menu[cat0]["flavors"]:
                if itm["flavor"] == flavor0:
                    itm["stock"] -= qty_needed
                    break
        with open(MENU_PATH, "w", encoding="utf-8") as f:
            json.dump(menu, f, ensure_ascii=False, indent=2)

        # Сохраняем заказ в БД
        items_json = json.dumps(cart, ensure_ascii=False)
        now = datetime.datetime.utcnow().isoformat()
        conn_local = get_db_connection()
        cursor_local = conn_local.cursor()
        cursor_local.execute(
            "INSERT INTO orders (chat_id, items_json, total, timestamp) VALUES (?, ?, ?, ?)",
            (chat_id, items_json, total_after, now)
        )
        conn_local.commit()

        # Начисление бонусных баллов (1 балл = 30₺)
        earned = total_after // 30
        if earned > 0:
            cursor_local.execute("UPDATE users SET points = points + ? WHERE chat_id = ?", (earned, chat_id))
            conn_local.commit()
            bot.send_message(chat_id, f"👍 Вы получили {earned} бонусных баллов за этот заказ.")

        # Начисление бонусного приглашения
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

        # Отправляем админам
        summary_rus = "\n".join(f"{i['category']}: {i['flavor']} — {i['price']}₺" for i in cart)
        summary_en = "\n".join(f"{i['category']}: {i['flavor']} — {i['price']}₺" for i in cart)
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
            f"💬 Комментарий: {data.get('order_comment', '—')}"
        )
        bot.send_message(PERSONAL_CHAT_ID, full_rus)

        comment_ru = data.get('order_comment', '')
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
            "wait_for_order_comment": False
        })
        user_data[chat_id] = data
        return

# —————————————————————————————————————————————————————————————
#   29. /change: перевод в режим редактирования меню (только на английском)
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
            "wait_for_order_comment": False,
            "address": "",
            "contact": "",
            "order_comment": "",
            "pending_discount": 0,
            "pending_points_spent": 0,
            "temp_total_try": 0,
            "temp_user_points": 0,
            "edit_phase": None,
            "edit_cat": None,
            "edit_flavor": None,
            "edit_index": None,
            "edit_cart_phase": None,
            # Новые поля для /review
            "pending_review_flavor": None,
            "pending_review_category": None,
            "wait_for_review_rating": False,
            "pending_review_rating": None,
            "wait_for_review_comment": False
        }
    data = user_data[chat_id]
    data.update({
        "current_category": None,
        "wait_for_points": False,
        "wait_for_address": False,
        "wait_for_contact": False,
        "wait_for_order_comment": False,
        "edit_phase": "choose_action",
        "edit_cat": None,
        "edit_flavor": None,
        "edit_index": None,
        "edit_cart_phase": None
    })
    # Только на английском: показываем меню редактирования
    bot.send_message(chat_id, "Menu editing: choose action", reply_markup=edit_action_keyboard())

# —————————————————————————————————————————————————————————————
#   30. Хендлер /points (должен быть ДО универсального «ловца»)
# —————————————————————————————————————————————————————————————
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

# —————————————————————————————————————————————————————————————
#   31. Хендлер /convert — курсы и конвертация суммы TRY
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
#   32. /review – двухэтапный диалог: сначала вкус, потом оценка, потом опциональный комментарий
# —————————————————————————————————————————————————————————————
@bot.message_handler(commands=['review'])
def cmd_review_start(message):
    chat_id = message.chat.id
    args = message.text.split(maxsplit=1)
    if len(args) != 2:
        bot.send_message(chat_id, "Usage: /review <flavor_name>")
        return

    flavor_query = args[1].strip().lower()
    # Проверяем, есть ли такой flavor в меню (во всех категориях):
    found = False
    db_cat = None
    db_flavor = None
    for cat_key, cat_data in menu.items():
        for itm in cat_data["flavors"]:
            if itm["flavor"].lower() == flavor_query:
                found = True
                db_cat = cat_key
                db_flavor = itm["flavor"]
                break
        if found:
            break

    if not found:
        bot.send_message(chat_id, "Flavor not found. Make sure you spelled it exactly (case-insensitive).")
        return

    # Сохраняем, что ждём от пользователя рейтинг ревью
    data = user_data.setdefault(chat_id, {})
    data["pending_review_flavor"] = db_flavor
    data["pending_review_category"] = db_cat
    data["wait_for_review_rating"] = True
    data["pending_review_rating"] = None
    data["wait_for_review_comment"] = False

    bot.send_message(chat_id, f"Please rate «{db_flavor}» from 1 to 5 (send a number).")
    user_data[chat_id] = data

@bot.message_handler(func=lambda m: user_data.get(m.chat.id, {}).get("wait_for_review_rating"), content_types=['text'])
def handle_review_rating(message):
    chat_id = message.chat.id
    data = user_data.get(chat_id, {})

    text = message.text.strip()
    if not text.isdigit():
        bot.send_message(chat_id, "Invalid input. Please send a number from 1 to 5.")
        return

    rating = int(text)
    if rating < 1 or rating > 5:
        bot.send_message(chat_id, "Rating must be between 1 and 5. Try again.")
        return

    # Сохраняем рейтинг и переключаемся на ожидание комментария ревью
    data["pending_review_rating"] = rating
    data["wait_for_review_rating"] = False
    data["wait_for_review_comment"] = True

    bot.send_message(chat_id, "You can leave an optional comment about your experience, or send /skip to finish.")
    user_data[chat_id] = data

@bot.message_handler(func=lambda m: user_data.get(m.chat.id, {}).get("wait_for_review_comment"), content_types=['text'])
def handle_review_comment(message):
    chat_id = message.chat.id
    data = user_data.get(chat_id, {})

    text = message.text.strip()
    # Пользователь может написать /skip, чтобы пропустить комментарий
    if text == "/skip":
        comment = ""
    else:
        comment = text

    db_flavor = data.get("pending_review_flavor")
    db_category = data.get("pending_review_category")
    rating = data.get("pending_review_rating")
    now = datetime.datetime.utcnow().isoformat()

    conn_local = get_db_connection()
    cursor_local = conn_local.cursor()
    cursor_local.execute(
        "INSERT INTO reviews (chat_id, category, flavor, rating, comment, timestamp) VALUES (?, ?, ?, ?, ?, ?)",
        (chat_id, db_category, db_flavor, rating, comment, now)
    )
    conn_local.commit()

    # Обновляем средний рейтинг для этого вкуса
    cursor_local.execute(
        "SELECT AVG(rating) FROM reviews WHERE flavor = ?",
        (db_flavor,)
    )
    avg_rating = cursor_local.fetchone()[0] or 0

    # Сохраняем в menu.json (если нужно хранить там текущий рейтинг):
    for cat_key, cat_data in menu.items():
        for itm in cat_data["flavors"]:
            if itm["flavor"] == db_flavor:
                itm["rating"] = round(avg_rating, 1)
                break

    with open(MENU_PATH, "w", encoding="utf-8") as f:
        json.dump(menu, f, ensure_ascii=False, indent=2)

    cursor_local.close()
    conn_local.close()

    # Сброс флагов
    data["pending_review_flavor"] = None
    data["pending_review_category"] = None
    data["pending_review_rating"] = None
    data["wait_for_review_comment"] = False

    bot.send_message(chat_id, f"Thank you! Your rating for «{db_flavor}» is recorded. Current average rating: {avg_rating:.1f}")
    user_data[chat_id] = data

# —————————————————————————————————————————————————————————————
#   33. /show_reviews – выводим последние 10 отзывов по указанному вкусу
# —————————————————————————————————————————————————————————————
@bot.message_handler(commands=['show_reviews'])
def cmd_show_reviews(message):
    chat_id = message.chat.id
    args = message.text.split(maxsplit=1)
    if len(args) != 2:
        bot.send_message(chat_id, "Usage: /show_reviews <flavor_name>")
        return

    flavor_query = args[1].strip()
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
        bot.send_message(chat_id, "There are no reviews for this flavor yet.")
        return

    texts = []
    for uid, rating, comment, ts in rows[:10]:
        date = ts.split("T")[0]
        if comment:
            texts.append(f"👤 {uid} [{rating}⭐] — {date}\n“{comment}”")
        else:
            texts.append(f"👤 {uid} [{rating}⭐] — {date}\n(no comment)")

    bot.send_message(chat_id, "\n\n".join(texts))

# —————————————————————————————————————————————————————————————
#   34. /reviewtop – топ-5 вкусов по среднему рейтингу
# —————————————————————————————————————————————————————————————
@bot.message_handler(commands=['reviewtop'])
def cmd_review_top(message):
    chat_id = message.chat.id
    conn_local = get_db_connection()
    cursor_local = conn_local.cursor()
    cursor_local.execute(
        "SELECT flavor, AVG(rating) as avg_rating, COUNT(*) as cnt FROM reviews GROUP BY flavor HAVING cnt > 0 ORDER BY avg_rating DESC LIMIT 5"
    )
    rows = cursor_local.fetchall()
    cursor_local.close()
    conn_local.close()

    if not rows:
        bot.send_message(chat_id, "No reviews available yet.")
        return

    lines = []
    for rank, (flavor, avg_rating, cnt) in enumerate(rows, start=1):
        lines.append(f"{rank}. {flavor} — {avg_rating:.1f}⭐ ({cnt} reviews)")
    bot.send_message(chat_id, "🏆 Top-5 flavors by average rating:\n" + "\n".join(lines))

# —————————————————————————————————————————————————————————————
#   35. Универсальный хендлер (всё остальное)
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
            "wait_for_order_comment": False,
            "address": "",
            "contact": "",
            "order_comment": "",
            "pending_discount": 0,
            "pending_points_spent": 0,
            "temp_total_try": 0,
            "temp_user_points": 0,
            "edit_phase": None,
            "edit_cat": None,
            "edit_flavor": None,
            "edit_index": None,
            "edit_cart_phase": None,
            # Новые поля для /review
            "pending_review_flavor": None,
            "pending_review_category": None,
            "wait_for_review_rating": False,
            "pending_review_rating": None,
            "wait_for_review_comment": False
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
                    kb.add(f"{flavor0} [{stock0} pcs]")
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
                    data['edit_index'] = None
                    user_data[chat_id] = data
                    return
                key_to_remove, _ = items_list[idx]
                new_cart = [it for it in data['cart'] if not (it['category'] == key_to_remove[0] and it['flavor'] == key_to_remove[1] and it['price'] == key_to_remove[2])]
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

            # Пересобираем корзину: без изменения stock
            data['cart'] = [it for it in data['cart'] if not (it['category'] == cat0 and it['flavor'] == flavor0 and it['price'] == price0)]
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
    if text.startswith(f"{t(chat_id,'remove_item')} "):
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

        # Удаляем 1 шт. из локальной корзины (без изменения stock)
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
        data['wait_for_order_comment'] = True
        kb = comment_keyboard()
        bot.send_message(chat_id, t(chat_id, "enter_comment"), reply_markup=kb)
        user_data[chat_id] = data
        return

    # ——— Кнопка «⬅️ Назад» во всём остальном ———
    if text == t(chat_id, "back"):
        data.update({
            "current_category": None,
            "wait_for_points": False,
            "wait_for_address": False,
            "wait_for_contact": False,
            "wait_for_order_comment": False
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
        data["wait_for_order_comment"] = False
        bot.send_message(chat_id, t(chat_id, "cart_cleared"), reply_markup=get_inline_main_menu(chat_id))
        user_data[chat_id] = data
        return

    # ——— Обработка кнопки «➕ Добавить ещё» ———
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

    # Если ничего не подошло, бот молчит

# —————————————————————————————————————————————————————————————
#   36. Запуск бота
# —————————————————————————————————————————————————————————————
if __name__ == "__main__":
    bot.delete_webhook()  # Сброс webhook перед polling
    bot.polling(none_stop=True)
