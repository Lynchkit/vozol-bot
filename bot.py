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
#   "pending_discount": int,
#   "edit_phase": None or str,
#   "edit_cat": None or str,
#   "edit_flavor": None or str,
#   "edit_index": None or int,
#   "edit_cart_phase": None or str
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
#   8. Inline-кнопки для главного меню (категории + завершить заказ)
# —————————————————————————————————————————————————————————————
def get_inline_main_menu(chat_id: int) -> types.InlineKeyboardMarkup:
    """
    Возвращает Inline-клавиатуру со списком категорий (моделей) и кнопкой «Завершить заказ».
    """
    kb = types.InlineKeyboardMarkup(row_width=2)
    for cat in menu.keys():
        kb.add(types.InlineKeyboardButton(text=cat, callback_data=f"category|{cat}"))
    # Кнопка завершения заказа:
    kb.add(types.InlineKeyboardButton(text=f"✅ {t(chat_id,'finish_order')}", callback_data="finish_order"))
    return kb

# —————————————————————————————————————————————————————————————
#   9. Inline-кнопки для выбора вкусов
# —————————————————————————————————————————————————————————————
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

# —————————————————————————————————————————————————————————————
#   10. Reply-клавиатуры (альтернатива inline)
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
#   11. Клавиатура редактирования меню (/change)
# —————————————————————————————————————————————————————————————
def edit_action_keyboard() -> types.ReplyKeyboardMarkup:
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
    kb.add("➕ Add Category", "➖ Remove Category")
    kb.add("💲 Fix Price", "ALL IN", "🔄 Actual Flavor")
    kb.add("⬅️ Back", "❌ Cancel")
    return kb

# —————————————————————————————————————————————————————————————
#   12. Планировщик – еженедельный дайджест (необязательно)
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
#   13. Хендлер /start – регистрация, реферальная система, выбор языка
# —————————————————————————————————————————————————————————————
@bot.message_handler(commands=['start'])
def cmd_start(message):
    chat_id = message.chat.id
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
        "pending_discount": 0,
        "edit_phase": None,
        "edit_cat": None,
        "edit_flavor": None,
        "edit_index": None,
        "edit_cart_phase": None
    })
    data.update({
        "cart": [],
        "current_category": None,
        "wait_for_address": False,
        "wait_for_contact": False,
        "wait_for_comment": False,
        "address": "",
        "contact": "",
        "comment": "",
        "edit_phase": None,
        "edit_cat": None,
        "edit_flavor": None,
        "edit_index": None,
        "edit_cart_phase": None
    })

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
        "pending_discount": 0,
        "edit_phase": None,
        "edit_cat": None,
        "edit_flavor": None,
        "edit_index": None,
        "edit_cart_phase": None
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
        "comment": "",
        "edit_phase": None,
        "edit_cat": None,
        "edit_flavor": None,
        "edit_index": None,
        "edit_cart_phase": None
    })

    bot.answer_callback_query(call.id, t(chat_id, "lang_set"))

    # Сразу показываем главное меню (категории + «Завершить заказ»)
    bot.send_message(chat_id, t(chat_id, "welcome"), reply_markup=types.ReplyKeyboardRemove())
    bot.send_message(chat_id, t(chat_id, "choose_category"), reply_markup=get_inline_main_menu(chat_id))

    cursor.execute("SELECT referral_code FROM users WHERE chat_id = ?", (chat_id,))
    row = cursor.fetchone()
    if row:
        code = row[0]
        bot.send_message(chat_id, f"Ваш реферальный код: {code}\nПриглашайте друзей – получите бонусные баллы!")

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
    bot.send_message(chat_id, f"{t(chat_id, 'choose_flavor')} «{cat}»",
                     reply_markup=get_inline_flavors(chat_id, cat))

# —————————————————————————————————————————————————————————————
#   16. Callback: «Назад к категориям»
# —————————————————————————————————————————————————————————————
@bot.callback_query_handler(func=lambda call: call.data == "go_back_to_categories")
def handle_go_back_to_categories(call):
    chat_id = call.from_user.id
    bot.answer_callback_query(call.id)
    bot.send_message(chat_id, t(chat_id, "choose_category"), reply_markup=get_inline_main_menu(chat_id))

# —————————————————————————————————————————————————————————————
#   17. Callback: выбор вкуса (только текст)
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
#   18. Callback: добавить в корзину (возвращаем к главному меню с кнопкой «Завершить заказ»)
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

    bot.send_message(
        chat_id,
        f"«{flavor}» добавлен(а) в корзину! Теперь выберите другую категорию или завершите заказ:",
        reply_markup=get_inline_main_menu(chat_id)
    )

# —————————————————————————————————————————————————————————————
#   19. Callback: завершить заказ (запрос адреса)
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
#   20. /change: перевод в режим редактирования меню для любого пользователя
# —————————————————————————————————————————————————————————————
@bot.message_handler(commands=['change'])
def cmd_change(message):
    chat_id = message.chat.id
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
        "pending_discount": 0,
        "edit_phase": None,
        "edit_cat": None,
        "edit_flavor": None,
        "edit_index": None,
        "edit_cart_phase": None
    })
    data.update({
        "current_category": None,
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
#   21. Универсальный хендлер (всё остальное)
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
        "pending_discount": 0,
        "edit_phase": None,
        "edit_cat": None,
        "edit_flavor": None,
        "edit_index": None,
        "edit_cart_phase": None
    })

    # ─── Если мы сейчас в режиме редактирования меню (/change) ─────────────────────
    if data.get('edit_phase'):
        phase = data['edit_phase']

        # 1) Главное меню редактирования
        if phase == 'choose_action':
            if text == "⬅️ Back":
                data['edit_phase'] = None
                data['edit_cat'] = None
                data['edit_flavor'] = None
                bot.send_message(chat_id, "Returned to main menu.", reply_markup=get_inline_main_menu(chat_id))
                return

            if text == "❌ Cancel":
                data['edit_phase'] = None
                data['edit_cat'] = None
                data['edit_flavor'] = None
                bot.send_message(chat_id, "Menu editing cancelled.", reply_markup=get_inline_main_menu(chat_id))
                return

            if text == "➕ Add Category":
                data['edit_phase'] = 'add_category'
                kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
                kb.add("⬅️ Back")
                bot.send_message(chat_id, "Enter new category name:", reply_markup=kb)
                return

            if text == "➖ Remove Category":
                data['edit_phase'] = 'remove_category'
                kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
                for cat in menu:
                    kb.add(cat)
                kb.add("⬅️ Back")
                bot.send_message(chat_id, "Select category to remove:", reply_markup=kb)
                return

            if text == "💲 Fix Price":
                data['edit_phase'] = 'choose_fix_price_cat'
                kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
                for cat in menu:
                    kb.add(cat)
                kb.add("⬅️ Back")
                bot.send_message(chat_id, "Select category to fix price for:", reply_markup=kb)
                return

            if text == "ALL IN":
                data['edit_phase'] = 'choose_all_in_cat'
                kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
                for cat in menu:
                    kb.add(cat)
                kb.add("⬅️ Back")
                bot.send_message(chat_id, "Select category to replace full flavor list:", reply_markup=kb)
                return

            if text == "🔄 Actual Flavor":
                data['edit_phase'] = 'choose_cat_actual'
                kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
                for cat in menu:
                    kb.add(cat)
                kb.add("⬅️ Back")
                bot.send_message(chat_id, "Select category to update flavor stock:", reply_markup=kb)
                return

            bot.send_message(chat_id, "Choose action:", reply_markup=edit_action_keyboard())
            return

        # 2) Добавить категорию
        if phase == 'add_category':
            if text == "⬅️ Back":
                data['edit_phase'] = 'choose_action'
                bot.send_message(chat_id, "Back to editing menu:", reply_markup=edit_action_keyboard())
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
            return

        # 3) Удалить категорию
        if phase == 'remove_category':
            if text == "⬅️ Back":
                data['edit_phase'] = 'choose_action'
                bot.send_message(chat_id, "Back to editing menu:", reply_markup=edit_action_keyboard())
                return

            if text in menu:
                del menu[text]
                with open(MENU_PATH, "w", encoding="utf-8") as f:
                    json.dump(menu, f, ensure_ascii=False, indent=2)
                data['edit_phase'] = 'choose_action'
                bot.send_message(chat_id, f"Category «{text}» removed.", reply_markup=edit_action_keyboard())
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
                return

            if text in menu:
                data['edit_cat'] = text
                data['edit_phase'] = 'enter_new_price'
                kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
                kb.add("⬅️ Back")
                bot.send_message(chat_id, f"Enter new price in ₺ for category «{text}»:", reply_markup=kb)
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
            return

        # 6) Выбрать категорию для ALL IN
        if phase == 'choose_all_in_cat':
            if text == "⬅️ Back":
                data['edit_phase'] = 'choose_action'
                bot.send_message(chat_id, "Back to editing menu:", reply_markup=edit_action_keyboard())
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
            return

        # 8) Выбрать категорию для Actual Flavor
        if phase == 'choose_cat_actual':
            if text == "⬅️ Back":
                data['edit_phase'] = 'choose_action'
                bot.send_message(chat_id, "Back to editing menu:", reply_markup=edit_action_keyboard())
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
                return

            cat0 = data.get('edit_cat')
            flavor_name = text.split(' [')[0]
            exists = any(i["flavor"] == flavor_name for i in menu.get(cat0, {}).get("flavors", []))
            if exists:
                data['edit_flavor'] = flavor_name
                data['edit_phase'] = 'enter_actual_qty'
                kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
                kb.add("⬅️ Back")
                bot.send_message(chat_id, "Введите актуальное количество штук!", reply_markup=kb)
            else:
                bot.send_message(chat_id, "Flavor not found. Choose again:", reply_markup=edit_action_keyboard())
                data['edit_phase'] = 'choose_action'
            return

        # 10) Ввод актуального количества для Actual Flavor
        if phase == 'enter_actual_qty':
            if text == "⬅️ Back":
                data.pop('edit_flavor', None)
                data['edit_phase'] = 'choose_action'
                bot.send_message(chat_id, "Back to editing menu:", reply_markup=edit_action_keyboard())
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
            return

        data['edit_phase'] = 'choose_action'
        bot.send_message(chat_id, "Back to editing menu:", reply_markup=edit_action_keyboard())
        return
    # ────────────────────────────────────────────────────────────────────────────────

    # ——— Если мы сейчас в режиме редактирования корзины ———
    if data.get('edit_cart_phase'):
        if data['edit_cart_phase'] == 'choose_action':
            if text == "⬅️ Назад":
                data['edit_cart_phase'] = None
                data['edit_index'] = None
                bot.send_message(chat_id, "Вернулись в главное меню.", reply_markup=get_inline_main_menu(chat_id))
                return

            if text.startswith("Удалить "):
                try:
                    idx = int(text.split()[1]) - 1
                except:
                    bot.send_message(chat_id, "Неверный индекс. Попробуйте снова.", reply_markup=get_inline_main_menu(chat_id))
                    data['edit_cart_phase'] = None
                    data['edit_index'] = None
                    return
                grouped = {}
                for item in data['cart']:
                    key = (item['category'], item['flavor'], item['price'])
                    grouped[key] = grouped.get(key, 0) + 1
                items_list = list(grouped.items())
                if idx < 0 or idx >= len(items_list):
                    bot.send_message(chat_id, "Индекс вне диапазона.", reply_markup=get_inline_main_menu(chat_id))
                    data['edit_cart_phase'] = None
                    return
                key_to_remove, count = items_list[idx]
                new_cart = [it for it in data['cart'] if not (it['category'] == key_to_remove[0] and it['flavor'] == key_to_remove[1] and it['price'] == key_to_remove[2])]
                data['cart'] = new_cart
                data['edit_cart_phase'] = None
                data['edit_index'] = None
                bot.send_message(chat_id, f"Удалено все позиции «{key_to_remove[1]}» из корзины.", reply_markup=get_inline_main_menu(chat_id))
                return

            if text.startswith("Изменить "):
                try:
                    idx = int(text.split()[1]) - 1
                except:
                    bot.send_message(chat_id, "Неверный индекс. Попробуйте снова.", reply_markup=get_inline_main_menu(chat_id))
                    data['edit_cart_phase'] = None
                    return
                grouped = {}
                for item in data['cart']:
                    key = (item['category'], item['flavor'], item['price'])
                    grouped[key] = grouped.get(key, 0) + 1
                items_list = list(grouped.items())
                if idx < 0 or idx >= len(items_list):
                    bot.send_message(chat_id, "Индекс вне диапазона.", reply_markup=get_inline_main_menu(chat_id))
                    data['edit_cart_phase'] = None
                    return
                data['edit_index'] = idx
                data['edit_cart_phase'] = 'enter_qty'
                key_chosen, count = items_list[idx]
                cat0, flavor0, price0 = key_chosen
                bot.send_message(chat_id, f"Текущий товар: {cat0} — {flavor0} — {price0}₺ (в корзине {count} шт).\nВведите новое количество (0 чтобы удалить):")
                return

        if data['edit_cart_phase'] == 'enter_qty':
            if text == "⬅️ Назад":
                data['edit_cart_phase'] = None
                data['edit_index'] = None
                bot.send_message(chat_id, "Редактирование отменено. Возвращаемся в главное меню.", reply_markup=get_inline_main_menu(chat_id))
                return
            if not text.isdigit():
                bot.send_message(chat_id, "Введите число, пожалуйста.")
                return
            new_qty = int(text)
            grouped = {}
            for item in data['cart']:
                key = (item['category'], item['flavor'], item['price'])
                grouped[key] = grouped.get(key, 0) + 1
            items_list = list(grouped.items())
            idx = data['edit_index']
            if idx < 0 or idx >= len(items_list):
                bot.send_message(chat_id, "Индекс вне диапазона.", reply_markup=get_inline_main_menu(chat_id))
                data['edit_cart_phase'] = None
                data['edit_index'] = None
                return
            key_chosen, old_count = items_list[idx]
            cat0, flavor0, price0 = key_chosen

            data['cart'] = [it for it in data['cart'] if not (it['category']==cat0 and it['flavor']==flavor0 and it['price']==price0)]
            for _ in range(new_qty):
                data['cart'].append({'category': cat0, 'flavor': flavor0, 'price': price0})

            data['edit_cart_phase'] = None
            data['edit_index'] = None
            if new_qty == 0:
                bot.send_message(chat_id, f"Товар «{flavor0}» полностью удалён из корзины.", reply_markup=get_inline_main_menu(chat_id))
            else:
                bot.send_message(chat_id, f"Количество «{flavor0}» изменено на {new_qty}.", reply_markup=get_inline_main_menu(chat_id))
            return

    # ——— Обработка «Корзина» по клику Reply-кнопок ———
    if text == "🛒 Корзина":
        cart = data['cart']
        if not cart:
            bot.send_message(chat_id, "Корзина пуста.", reply_markup=get_inline_main_menu(chat_id))
            return

        grouped = {}
        for item in cart:
            key = (item['category'], item['flavor'], item['price'])
            grouped[key] = grouped.get(key, 0) + 1

        items_list = list(grouped.items())
        msg_lines = ["Ваши товары в корзине:"]
        for idx, (key, count) in enumerate(items_list, start=1):
            cat0, flavor0, price0 = key
            msg_lines.append(f"{idx}. {cat0} — {flavor0} — {price0}₺ x {count}")
        msg_text = "\n".join(msg_lines)

        kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
        for idx, (key, count) in enumerate(items_list, start=1):
            cat0, flavor0, price0 = key
            kb.add(f"Удалить {idx}", f"Изменить {idx}")
        kb.add("⬅️ Назад")
        data['edit_cart_phase'] = 'choose_action'
        bot.send_message(chat_id, msg_text, reply_markup=kb)
        return

    # ——— Если ожидаем ввод адреса ———
    if data.get('wait_for_address'):
        if text == "⬅️ Назад":
            data['wait_for_address'] = False
            data['current_category'] = None
            bot.send_message(chat_id, t(chat_id, "choose_category"), reply_markup=get_inline_main_menu(chat_id))
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
                cat0 = o["category"]
                for itm in menu[cat0]["flavors"]:
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
        bot.send_message(chat_id, t(chat_id, "choose_category"), reply_markup=get_inline_main_menu(chat_id))
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
        bot.send_message(chat_id, "Корзина очищена.", reply_markup=get_inline_main_menu(chat_id))
        return

    # ——— Добавить ещё ———
    if text == "➕ Добавить ещё":
        data["current_category"] = None
        bot.send_message(chat_id, t(chat_id, "choose_category"), reply_markup=get_inline_main_menu(chat_id))
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
                    bot.send_message(
                        chat_id,
                        f"«{flavor0}» добавлен(а) в корзину! Теперь выберите другую категорию или завершите заказ:",
                        reply_markup=get_inline_main_menu(chat_id)
                    )
                    return
        bot.send_message(chat_id, t(chat_id, "error_invalid"), reply_markup=get_inline_flavors(chat_id, cat0))
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
