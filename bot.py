# bot.py
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
    raise RuntimeError("Переменная окружения TOKEN не задана! "
                       "Запустите контейнер с -e TOKEN=<ваш_токен>.")

ADMIN_ID = int(os.getenv("ADMIN_ID", "424751188"))
GROUP_CHAT_ID = int(os.getenv("GROUP_CHAT_ID", "0"))
PERSONAL_CHAT_ID = int(os.getenv("PERSONAL_CHAT_ID", "0"))

bot = telebot.TeleBot(TOKEN, parse_mode="HTML")

# —————————————————————————————————————————————————————————————
#   2. Пути к JSON-файлам и БД
# —————————————————————————————————————————————————————————————
MENU_PATH = "menu.json"
LANG_PATH = "languages.json"
DB_PATH = "database.db"

# —————————————————————————————————————————————————————————————
#   3. Инициализация SQLite и создание таблиц
# —————————————————————————————————————————————————————————————
conn = sqlite3.connect(DB_PATH, check_same_thread=False)
cursor = conn.cursor()

cursor.execute("""
CREATE TABLE IF NOT EXISTS users (
    chat_id        INTEGER PRIMARY KEY,
    points         INTEGER DEFAULT 0,
    referral_code  TEXT UNIQUE,
    referred_by    INTEGER
)
""")
cursor.execute("""
CREATE TABLE IF NOT EXISTS orders (
    order_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id     INTEGER,
    items_json  TEXT,
    total       INTEGER,
    timestamp   TEXT
)
""")
cursor.execute("""
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
conn.commit()

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
#   "cart": [ {"category":str,"flavor":str,"price":int}, ... ],
#   "current_category": str or None,
#   "wait_for_address": bool,
#   "wait_for_contact": bool,
#   "wait_for_comment": bool,
#   "address": str,
#   "contact": str,
#   "comment": str,
#   "pending_discount": int
# }

# —————————————————————————————————————————————————————————————
#   6. Утилиты
# —————————————————————————————————————————————————————————————
def t(chat_id: int, key: str) -> str:
    """
    Получает перевод из languages.json по ключу.
    Если перевод не найден — возвращает ключ.
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
#   8. Inline-кнопки для категорий и вкусов
# —————————————————————————————————————————————————————————————
def get_inline_categories(chat_id: int) -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup(row_width=2)
    for cat in menu.keys():
        kb.add(types.InlineKeyboardButton(text=cat, callback_data=f"category|{cat}"))
    return kb

def get_inline_flavors(chat_id: int, cat: str) -> types.InlineKeyboardMarkup:
    """
    Показывает только те вкусы, у которых stock > 0.
    Нет кнопки «нет в наличии».
    """
    kb = types.InlineKeyboardMarkup(row_width=1)
    price = menu[cat]["price"]
    for item in menu[cat]["flavors"]:
        if item.get("stock", 0) > 0:
            emoji = item.get("emoji", "")
            flavor_name = item["flavor"]
            label = f"{emoji} {flavor_name} — {price}₺ [{item['stock']}шт]"
            kb.add(types.InlineKeyboardButton(text=label, callback_data=f"flavor|{cat}|{flavor_name}"))
    # Кнопка «Назад к категориям»
    kb.add(types.InlineKeyboardButton(text=f"⬅️ {t(chat_id,'back_to_categories')}", callback_data="go_back_to_categories"))
    return kb

def get_inline_after_add(chat_id: int, cat: str) -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        types.InlineKeyboardButton(text=f"➕ {t(chat_id,'add_more')}", callback_data=f"go_back_to_category|{cat}"),
        types.InlineKeyboardButton(text=f"🛒 {t(chat_id,'view_cart')}", callback_data="view_cart")
    )
    kb.add(types.InlineKeyboardButton(text=f"✅ {t(chat_id,'finish_order')}", callback_data="finish_order"))
    return kb

def get_inline_cart(chat_id: int) -> types.InlineKeyboardMarkup:
    data = user_data.get(chat_id, {})
    cart = data.get("cart", [])
    if not cart:
        return None
    kb = types.InlineKeyboardMarkup(row_width=2)
    grouped = {}
    for item in cart:
        key = (item["category"], item["flavor"], item["price"])
        grouped[key] = grouped.get(key, 0) + 1
    for idx, ((cat, flavor, price), qty) in enumerate(grouped.items(), start=1):
        kb.add(
            types.InlineKeyboardButton(text=f"❌ {t(chat_id,'remove_item')} {idx}", callback_data=f"remove_item|{idx}"),
            types.InlineKeyboardButton(text=f"✏️ {t(chat_id,'edit_item')} {idx}", callback_data=f"edit_item|{idx}")
        )
    kb.add(types.InlineKeyboardButton(text=f"⬅️ {t(chat_id,'back_to_categories')}", callback_data="go_back_to_categories"))
    kb.add(types.InlineKeyboardButton(text=f"✅ {t(chat_id,'finish_order')}", callback_data="finish_order"))
    return kb

# —————————————————————————————————————————————————————————————
#   9. Reply-клавиатуры (альтернатива inline для части потока)
# —————————————————————————————————————————————————————————————
def address_keyboard() -> types.ReplyKeyboardMarkup:
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
    kb.add(types.KeyboardButton("📍 Поделиться геопозицией", request_location=True))
    kb.add("🗺️ Выбрать точку на карте")
    kb.add("✏️ Ввести адрес")
    kb.add("⬅️ Назад")
    return kb

def contact_keyboard() -> types.ReplyKeyboardMarkup:
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
    kb.add(types.KeyboardButton("📞 Поделиться контактом", request_contact=True))
    kb.add("✏️ Ввести ник")
    kb.add("⬅️ Назад")
    return kb

def comment_keyboard() -> types.ReplyKeyboardMarkup:
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
    kb.add("✏️ Комментарий к заказу")
    kb.add("📤 Отправить заказ")
    kb.add("⬅️ Назад")
    return kb

# —————————————————————————————————————————————————————————————
#   10. Планировщик – еженедельный дайджест (необязательно)
# —————————————————————————————————————————————————————————————
def send_weekly_digest():
    one_week_ago = (datetime.datetime.utcnow() - datetime.timedelta(days=7)).isoformat()
    cursor.execute("SELECT items_json FROM orders WHERE timestamp >= ?", (one_week_ago,))
    recent = cursor.fetchall()
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
    cursor.execute("SELECT DISTINCT chat_id FROM orders")
    users = cursor.fetchall()
    for (uid,) in users:
        bot.send_message(uid, text)

scheduler = BackgroundScheduler(timezone="Europe/Riga")
scheduler.add_job(send_weekly_digest, trigger="cron", day_of_week="mon", hour=9, minute=0)
scheduler.start()

# —————————————————————————————————————————————————————————————
#   11. Хендлер /start – регистрация, реферальная система, выбор языка
# —————————————————————————————————————————————————————————————
@bot.message_handler(commands=['start'])
def cmd_start(message):
    chat_id = message.chat.id
    # Инициализируем или сбрасываем состояние пользователя
    data = user_data.setdefault(chat_id, {
        "lang": None,
        "cart": [],
        "current_category": None,
        "wait_for_address": False,
        "wait_for_contact": False,
        "wait_for_comment": False,
        "address": "",
        "contact": "",
        "comment": "",
        "pending_discount": 0
    })
    data.update({
        "cart": [],
        "current_category": None,
        "wait_for_address": False,
        "wait_for_contact": False,
        "wait_for_comment": False,
        "address": "",
        "contact": "",
        "comment": ""
    })

    # Реферальная регистрация
    cursor.execute("SELECT chat_id FROM users WHERE chat_id = ?", (chat_id,))
    if cursor.fetchone() is None:
        text = message.text or ""
        referred_by = None
        if "ref=" in text:
            code = text.split("ref=")[1]
            cursor.execute("SELECT chat_id FROM users WHERE referral_code = ?", (code,))
            row = cursor.fetchone()
            if row:
                referred_by = row[0]
        new_code = generate_ref_code()
        while True:
            cursor.execute("SELECT referral_code FROM users WHERE referral_code = ?", (new_code,))
            if cursor.fetchone() is None:
                break
            new_code = generate_ref_code()
        cursor.execute(
            "INSERT INTO users (chat_id, points, referral_code, referred_by) VALUES (?, ?, ?, ?)",
            (chat_id, 0, new_code, referred_by)
        )
        conn.commit()

    # Отправляем кнопку выбора языка
    bot.send_message(
        chat_id,
        t(chat_id, "choose_language"),
        reply_markup=get_inline_language_buttons(chat_id)
    )

# —————————————————————————————————————————————————————————————
#   12. Callback: выбор языка
# —————————————————————————————————————————————————————————————
@bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("set_lang|"))
def handle_set_lang(call):
    chat_id = call.from_user.id
    _, lang_code = call.data.split("|", 1)

    # Сбрасываем состояние, устанавливаем язык
    data = user_data.setdefault(chat_id, {
        "lang": None,
        "cart": [],
        "current_category": None,
        "wait_for_address": False,
        "wait_for_contact": False,
        "wait_for_comment": False,
        "address": "",
        "contact": "",
        "comment": "",
        "pending_discount": 0
    })
    data["lang"] = lang_code
    data.update({
        "cart": [],
        "current_category": None,
        "wait_for_address": False,
        "wait_for_contact": False,
        "wait_for_comment": False,
        "address": "",
        "contact": "",
        "comment": ""
    })

    bot.answer_callback_query(call.id, t(chat_id, "lang_set"))

    # Сначала убираем любую Reply-клавиатуру
    bot.send_message(chat_id, t(chat_id, "welcome"), reply_markup=types.ReplyKeyboardRemove())
    # Затем показываем inline-меню категорий
    bot.send_message(chat_id, t(chat_id, "welcome"), reply_markup=get_inline_categories(chat_id))

    # Отправляем реферальный код
    cursor.execute("SELECT referral_code FROM users WHERE chat_id = ?", (chat_id,))
    row = cursor.fetchone()
    if row:
        code = row[0]
        bot.send_message(chat_id, f"Ваш реферальный код: {code}\nПриглашайте друзей – получите бонусные баллы!")

# —————————————————————————————————————————————————————————————
#   13. Callback: выбор категории (показываем вкусы)
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
    bot.send_message(chat_id, f"{t(chat_id, 'choose_flavor')} «{cat}»",
                     reply_markup=get_inline_flavors(chat_id, cat))

# —————————————————————————————————————————————————————————————
#   14. Callback: «Назад к категориям»
# —————————————————————————————————————————————————————————————
@bot.callback_query_handler(func=lambda call: call.data == "go_back_to_categories")
def handle_go_back_to_categories(call):
    chat_id = call.from_user.id
    bot.answer_callback_query(call.id)
    bot.send_message(chat_id, t(chat_id, "choose_category"), reply_markup=get_inline_categories(chat_id))

# —————————————————————————————————————————————————————————————
#   15. Callback: выбор вкуса (только текст, без photo_url и рейтинга)
# —————————————————————————————————————————————————————————————
@bot.callback_query_handler(func=lambda call: call.data and call.data.startswith("flavor|"))
def handle_flavor(call):
    print("DEBUG: получен call.data =", call.data)
    _, cat, flavor = call.data.split("|", 2)
    chat_id = call.from_user.id

    # Проверяем, что категория существует
    if cat not in menu:
        bot.answer_callback_query(call.id, t(chat_id, "error_invalid"))
        return

    # Находим объект вкуса
    item_obj = next((i for i in menu[cat]["flavors"] if i["flavor"] == flavor), None)
    if not item_obj:
        bot.answer_callback_query(call.id, t(chat_id, "error_invalid"))
        return

    bot.answer_callback_query(call.id)

    user_lang = user_data.get(chat_id, {}).get("lang", "ru")
    description = item_obj.get(f"description_{user_lang}", "") or ""
    price = menu[cat]["price"]

    caption = (
        f"<b>{flavor}</b>\n\n"
        f"{description}\n\n"
        f"📌 {price}₺"
    )

    # Всегда отправляем только текст
    bot.send_message(chat_id, caption, parse_mode="HTML")

    # Кнопки «Добавить в корзину» и «Назад к категориям»
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        types.InlineKeyboardButton(
            text=f"➕ {t(chat_id,'add_to_cart')}",
            callback_data=f"add_to_cart|{cat}|{flavor}"
        ),
        types.InlineKeyboardButton(
            text=f"⬅️ {t(chat_id,'back_to_categories')}",
            callback_data=f"category|{cat}"
        )
    )
    bot.send_message(chat_id, t(chat_id, "choose_action"), reply_markup=kb)

# —————————————————————————————————————————————————————————————
#   16. Callback: добавить в корзину
# —————————————————————————————————————————————————————————————
@bot.callback_query_handler(func=lambda call: call.data and call.data.startswith("add_to_cart|"))
def handle_add_to_cart(call):
    chat_id = call.from_user.id
    _, cat, flavor = call.data.split("|", 2)
    bot.answer_callback_query(call.id)

    data = user_data.setdefault(chat_id, {})
    cart = data.setdefault("cart", [])
    price = menu[cat]["price"]
    cart.append({"category": cat, "flavor": flavor, "price": price})

    count = len(cart)
    grouped = {}
    for item in cart:
        key = (item["category"], item["flavor"], item["price"])
        grouped[key] = grouped.get(key, 0) + 1

    lines = ["🛒 Ваша корзина:"]
    for idx, ((c, f, p), qty) in enumerate(grouped.items(), start=1):
        lines.append(f"{idx}. {c} — {f} — {p}₺ x {qty}")
    text_cart = "\n".join(lines)

    kb = get_inline_after_add(chat_id, cat)
    bot.send_message(
        chat_id,
        f"{cat} — {flavor} ({price}₺) {t(chat_id,'added_to_cart').format(flavor=flavor, count=count)}\n\n{text_cart}",
        reply_markup=kb
    )

# —————————————————————————————————————————————————————————————
#   17. Callback: «Вернуться к выбору этого же кат.»
# —————————————————————————————————————————————————————————————
@bot.callback_query_handler(func=lambda call: call.data and call.data.startswith("go_back_to_category|"))
def handle_go_back_to_category(call):
    chat_id = call.from_user.id
    _, cat = call.data.split("|", 1)
    bot.answer_callback_query(call.id)
    bot.send_message(chat_id, f"{t(chat_id, 'choose_flavor')} «{cat}»",
                     reply_markup=get_inline_flavors(chat_id, cat))

# —————————————————————————————————————————————————————————————
#   18. Callback: «Просмотр корзины»
# —————————————————————————————————————————————————————————————
@bot.callback_query_handler(func=lambda call: call.data == "view_cart")
def handle_view_cart(call):
    chat_id = call.from_user.id
    bot.answer_callback_query(call.id)
    data = user_data.get(chat_id, {})
    cart = data.get("cart", [])
    if not cart:
        bot.send_message(chat_id, t(chat_id, "cart_empty"), reply_markup=get_inline_categories(chat_id))
        return

    grouped = {}
    for item in cart:
        key = (item["category"], item["flavor"], item["price"])
        grouped[key] = grouped.get(key, 0) + 1

    text_lines = [f"🛒 {t(chat_id, 'view_cart')}:"]
    for idx, ((cat, flavor, price), qty) in enumerate(grouped.items(), start=1):
        text_lines.append(f"{idx}. {cat} — {flavor} — {price}₺ x {qty}")
    msg = "\n".join(text_lines)

    kb = get_inline_cart(chat_id)
    bot.send_message(chat_id, msg, reply_markup=kb)

# —————————————————————————————————————————————————————————————
#   19. Callback: удалить/изменить элемент корзины
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
    key_to_remove, _ = items_list[idx]
    cat, flavor, price = key_to_remove
    new_cart = [it for it in cart if not (it["category"] == cat and it["flavor"] == flavor and it["price"] == price)]
    data["cart"] = new_cart
    bot.answer_callback_query(call.id, t(chat_id, "item_removed").format(flavor=flavor))
    handle_view_cart(call)

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
    key_to_edit, old_qty = items_list[idx]
    cat, flavor, price = key_to_edit
    bot.answer_callback_query(call.id)
    data["edit_cart_phase"] = "enter_qty"
    data["edit_index"] = idx
    bot.send_message(chat_id,
                     f"Текущий товар: {cat} — {flavor} — {price}₺ (в корзине {old_qty} шт).\n"
                     f"{t(chat_id, 'enter_new_qty')}",
                     reply_markup=types.ReplyKeyboardRemove())

@bot.message_handler(func=lambda m: user_data.get(m.chat.id, {}).get("edit_cart_phase") == "enter_qty", content_types=['text'])
def handle_enter_new_qty(message):
    chat_id = message.chat.id
    data = user_data.get(chat_id, {})
    text = message.text.strip()
    if not text.isdigit():
        bot.send_message(chat_id, t(chat_id, "error_invalid"))
        data["edit_cart_phase"] = None
        data.pop("edit_index", None)
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
        data.pop("edit_index", None)
        return
    key_to_edit, old_qty = items_list[idx]
    cat, flavor, price = key_to_edit
    new_cart = [it for it in cart if not (it["category"] == cat and it["flavor"] == flavor and it["price"] == price)]
    for _ in range(new_qty):
        new_cart.append({"category": cat, "flavor": flavor, "price": price})
    data["cart"] = new_cart
    data["edit_cart_phase"] = None
    data.pop("edit_index", None)
    if new_qty == 0:
        bot.send_message(chat_id, t(chat_id, "item_removed").format(flavor=flavor),
                         reply_markup=get_inline_categories(chat_id))
    else:
        bot.send_message(chat_id, t(chat_id, "qty_changed").format(flavor=flavor, qty=new_qty),
                         reply_markup=get_inline_categories(chat_id))

# —————————————————————————————————————————————————————————————
#   20. Callback: завершить заказ (запрос адреса)
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
    kb = address_keyboard()
    bot.send_message(
        chat_id,
        f"🛒 {t(chat_id, 'view_cart')}:\n\n" +
        "\n".join(f"{item['category']}: {item['flavor']} — {item['price']}₺" for item in cart) +
        f"\n\n{t(chat_id, 'enter_address')}",
        reply_markup=kb
    )
    data["wait_for_address"] = True

# —————————————————————————————————————————————————————————————
#   21. Обработка адреса, контакта и комментария
# —————————————————————————————————————————————————————————————
@bot.message_handler(content_types=['text','location','venue','contact'])
def universal_handler(message):
    chat_id = message.chat.id
    text = message.text or ""
    data = user_data.setdefault(chat_id, {
        "lang": "ru",
        "cart": [],
        "current_category": None,
        "wait_for_address": False,
        "wait_for_contact": False,
        "wait_for_comment": False,
        "address": "",
        "contact": "",
        "comment": "",
        "pending_discount": 0
    })

    # ——— Если ожидаем ввод адреса ———
    if data.get('wait_for_address'):
        if text == "⬅️ Назад":
            data['wait_for_address'] = False
            data['current_category'] = None
            bot.send_message(chat_id, t(chat_id, "choose_category"), reply_markup=get_inline_categories(chat_id))
            return

        if text == "🗺️ Выбрать точку на карте":
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
        elif text == "✏️ Ввести адрес":
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
        return

    # ——— Если ожидаем ввод контакта ———
    if data.get('wait_for_contact'):
        if text == "⬅️ Назад":
            data['wait_for_address'] = True
            data['wait_for_contact'] = False
            bot.send_message(chat_id, t(chat_id, "enter_address"), reply_markup=address_keyboard())
            return

        if text == "✏️ Ввести ник":
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
        return

    # ——— Если ожидаем ввод комментария ———
    if data.get('wait_for_comment'):
        if text == "⬅️ Назад":
            data['wait_for_contact'] = True
            data['wait_for_comment'] = False
            bot.send_message(chat_id, t(chat_id, "enter_contact"), reply_markup=contact_keyboard())
            return

        if text == "✏️ Комментарий к заказу":
            bot.send_message(chat_id, t(chat_id, "enter_comment"), reply_markup=types.ReplyKeyboardRemove())
            return

        if message.content_type == 'text' and text != "📤 Отправить заказ":
            data['comment'] = text.strip()
            bot.send_message(chat_id, t(chat_id, "comment_saved"), reply_markup=comment_keyboard())
            return

        if text == "📤 Отправить заказ":
            cart = data.get('cart', [])
            if not cart:
                bot.send_message(chat_id, t(chat_id, "cart_empty"))
                return

            total_try = sum(i['price'] for i in cart)
            discount = data.pop("pending_discount", 0)
            total_after = max(total_try - discount, 0)

            # Сохраняем заказ в БД
            items_json = json.dumps(cart, ensure_ascii=False)
            now = datetime.datetime.utcnow().isoformat()
            cursor.execute(
                "INSERT INTO orders (chat_id, items_json, total, timestamp) VALUES (?, ?, ?, ?)",
                (chat_id, items_json, total_after, now)
            )
            conn.commit()

            # Начисление бонусных баллов
            earned = total_after // 500
            if earned > 0:
                cursor.execute("UPDATE users SET points = points + ? WHERE chat_id = ?", (earned, chat_id))
                conn.commit()
                bot.send_message(chat_id, f"👍 Вы получили {earned} бонусных баллов за этот заказ.")

            # Начисление бонусного приглашения
            cursor.execute("SELECT referred_by FROM users WHERE chat_id = ?", (chat_id,))
            row = cursor.fetchone()
            if row and row[0]:
                inviter = row[0]
                cursor.execute("UPDATE users SET points = points + 10 WHERE chat_id = ?", (inviter,))
                conn.commit()
                bot.send_message(inviter, "🎉 Вам начислено 10 бонусных баллов за приглашение нового клиента!")
                cursor.execute("UPDATE users SET referred_by = NULL WHERE chat_id = ?", (chat_id,))
                conn.commit()

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
                f"💬 Комментарий: {data.get('comment', '—')}"
            )
            bot.send_message(PERSONAL_CHAT_ID, full_rus)

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

            bot.send_message(
                chat_id,
                t(chat_id, "order_accepted"),
                reply_markup=types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True).add("🛒 Оформить новый заказ")
            )

            # Обновляем остатки stock
            for o in cart:
                cat = o["category"]
                for itm in menu[cat]["flavors"]:
                    if itm["flavor"] == o["flavor"]:
                        itm["stock"] = max(itm.get("stock", 1) - 1, 0)
                        break
            with open(MENU_PATH, "w", encoding="utf-8") as f:
                json.dump(menu, f, ensure_ascii=False, indent=2)

            data.update({
                "cart": [],
                "current_category": None,
                "wait_for_address": False,
                "wait_for_contact": False,
                "wait_for_comment": False
            })
            return

    # ——— Кнопка «⬅️ Назад» в любое время ———
    if text == "⬅️ Назад":
        data.update({
            "current_category": None,
            "wait_for_address": False,
            "wait_for_contact": False,
            "wait_for_comment": False
        })
        bot.send_message(chat_id, t(chat_id, "choose_category"), reply_markup=get_inline_categories(chat_id))
        return

    # ——— Очистка корзины ———
    if text == "🗑️ Очистить корзину":
        data.update({
            "cart": [],
            "current_category": None,
            "wait_for_address": False,
            "wait_for_contact": False,
            "wait_for_comment": False
        })
        bot.send_message(chat_id, "Корзина очищена.", reply_markup=get_inline_categories(chat_id))
        return

    # ——— Добавить ещё ———
    if text == "➕ Добавить ещё":
        data["current_category"] = None
        bot.send_message(chat_id, t(chat_id, "choose_category"), reply_markup=get_inline_categories(chat_id))
        return

    # ——— Завершить заказ ———
    if text == "✅ Завершить заказ":
        if not data['cart']:
            bot.send_message(chat_id, t(chat_id, "cart_empty"))
            return
        total_try = sum(i['price'] for i in data['cart'])
        kb = address_keyboard()
        bot.send_message(
            chat_id,
            f"🛒 {t(chat_id, 'view_cart')}:\n\n" +
            "\n".join(f"{i['category']}: {i['flavor']} — {i['price']}₺" for i in data['cart']) +
            f"\n\n{t(chat_id, 'enter_address')}",
            reply_markup=kb
        )
        data['wait_for_address'] = True
        return

    # ——— Выбор категории (Reply-клавиатура fallback) ———
    if text in menu:
        data['current_category'] = text
        bot.send_message(chat_id, f"{t(chat_id, 'choose_flavor')} «{text}»",
                         reply_markup=get_inline_flavors(chat_id, text))
        return

    # ——— Выбор вкуса (Reply-клавиатура fallback) ———
    cat = data.get('current_category')
    if cat:
        price = menu[cat]["price"]
        for it in menu[cat]["flavors"]:
            if it.get("stock", 0) > 0:
                emoji = it.get("emoji", "")
                flavor = it["flavor"]
                label = f"{emoji} {flavor} ({price}₺) [{it['stock']} шт]"
                if text == label:
                    data['cart'].append({'category': cat, 'flavor': flavor, 'price': price})
                    count = len(data['cart'])
                    kb = get_inline_after_add(chat_id, cat)
                    bot.send_message(
                        chat_id,
                        f"{cat} — {flavor} ({price}₺) {t(chat_id,'added_to_cart').format(flavor=flavor, count=count)}",
                        reply_markup=kb
                    )
                    return
        bot.send_message(chat_id, t(chat_id, "error_invalid"), reply_markup=get_inline_flavors(chat_id, cat))
        return

    # ——— /history ———
    if text == "/history":
        cursor.execute(
            "SELECT order_id, items_json, total, timestamp FROM orders WHERE chat_id = ? ORDER BY timestamp DESC",
            (chat_id,)
        )
        rows = cursor.fetchall()
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
        now = datetime.datetime.utcnow().isoformat()
        cursor.execute(
            "INSERT INTO reviews (chat_id, category, flavor, rating, comment, timestamp) VALUES (?, ?, ?, ?, ?, ?)",
            (chat_id, db_cat, db_flavor, rating, comment, now)
        )
        conn.commit()
        cursor.execute(
            "SELECT AVG(rating) FROM reviews WHERE flavor = ?",
            (db_flavor,)
        )
        avg_rating = cursor.fetchone()[0] or 0
        for itm in menu[db_cat]["flavors"]:
            if itm["flavor"] == db_flavor:
                itm["rating"] = round(avg_rating, 1)
                break
        with open(MENU_PATH, "w", encoding="utf-8") as f:
            json.dump(menu, f, ensure_ascii=False, indent=2)
        bot.send_message(chat_id, f"Спасибо за отзыв! Текущий средний рейтинг «{db_flavor}»: {avg_rating:.1f}")
        return

    # ——— /show_reviews ———
    if text.startswith("/show_reviews"):
        parts = text.split(maxsplit=1)
        if len(parts) != 2:
            bot.send_message(chat_id, "Использование: /show_reviews <название_вкуса>")
            return
        flavor_query = parts[1]
        cursor.execute(
            "SELECT chat_id, rating, comment, timestamp FROM reviews WHERE flavor = ? ORDER BY timestamp DESC",
            (flavor_query,)
        )
        rows = cursor.fetchall()
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
        if chat_id != ADMIN_ID:
            bot.send_message(chat_id, "У вас нет доступа к этой команде.")
            return
        cursor.execute("SELECT COUNT(*) FROM orders")
        total_orders = cursor.fetchone()[0]
        cursor.execute("SELECT SUM(total) FROM orders")
        total_revenue = cursor.fetchone()[0] or 0
        cursor.execute("SELECT items_json FROM orders")
        all_items = cursor.fetchall()
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
        return

# —————————————————————————————————————————————————————————————
#   22. Запуск бота
# —————————————————————————————————————————————————————————————
if __name__ == "__main__":
    bot.delete_webhook()  # Сброс webhook перед polling
    bot.polling(none_stop=True)
