import os
import json
import sqlite3
import threading
import time
from datetime import datetime
import telebot
from telebot import types
import requests
import matplotlib.pyplot as plt
import pandas as pd
from apscheduler.schedulers.background import BackgroundScheduler

# Пути к JSON-файлам
MENU_PATH = "menu.json"
LANG_PATH = "languages.json"

# Подключение к SQLite (хранит данные о пользователях, корзинах и подписках)
DB_PATH = "bot.db"

# Загрузка переменных окружения
TOKEN = os.getenv("TOKEN")
if not TOKEN:
    raise RuntimeError("Переменная окружения TOKEN не задана! Запустите контейнер с -e TOKEN=<ваш_токен>.")
bot = telebot.TeleBot(TOKEN, parse_mode="HTML")

# Глобальные структуры данных
menu = {}            # Словарь с товарами и их характеристиками
translations = {}    # Словарь с переводами текстов
user_data = {}       # Временные данные по каждому пользователю (корзина, состояние)

# ==== Функции для работы с JSON ====

def load_json(path):
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"Error loading {path}: {e}")
        return {}

def save_json(path, data):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=4)
    except Exception as e:
        print(f"Error saving {path}: {e}")

# ==== Инициализация БД ====

def init_db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    cursor = conn.cursor()
    # Таблица для хранения корзины: chat_id, category, flavor, quantity
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS cart (
            chat_id INTEGER,
            category TEXT,
            flavor TEXT,
            quantity INTEGER,
            PRIMARY KEY (chat_id, category, flavor)
        )
    """)
    # Таблица для подписок: chat_id, category, flavor
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS subscriptions (
            chat_id INTEGER,
            category TEXT,
            flavor TEXT,
            PRIMARY KEY (chat_id, category, flavor)
        )
    """)
    conn.commit()
    return conn, cursor

conn, cursor = init_db()

# ==== Загрузка данных при старте ====

menu = load_json(MENU_PATH)
translations = load_json(LANG_PATH)

# ==== Вспомогательные функции ====

def t(chat_id, key):
    """
    Функция возвращает перевод строки по ключу для данного пользователя.
    Если перевода нет, возвращает сам ключ.
    """
    lang = user_data.get(chat_id, {}).get("lang", "en")
    return translations.get(lang, {}).get(key, key)

def get_inline_categories(chat_id):
    """
    Построить inline-клавиатуру с категориями (имена всех товаров из menu).
    """
    kb = types.InlineKeyboardMarkup(row_width=1)
    for category in menu.keys():
        btn_text = f"{category} — {menu[category]['price']}₺"
        kb.add(types.InlineKeyboardButton(text=btn_text, callback_data=f"category|{category}"))
    return kb

def get_inline_flavors(chat_id, category):
    """
    Построить inline-клавиатуру со вкусами для выбранной категории товара.
    """
    kb = types.InlineKeyboardMarkup(row_width=2)
    for item in menu[category]["flavors"]:
        flavor = item["flavor"]
        emoji = item.get("emoji", "")
        kb.add(types.InlineKeyboardButton(text=f"{emoji} {flavor}", callback_data=f"flavor|{category}|{flavor}"))
    kb.add(types.InlineKeyboardButton(text=t(chat_id, "back_to_categories"), callback_data="go_back_to_categories"))
    return kb

def get_cart_keyboard(chat_id):
    """
    Построить inline-клавиатуру для управления корзиной: +, -, удалить, оформление.
    """
    data = user_data.get(chat_id, {})
    cart = data.get("cart", {})
    kb = types.InlineKeyboardMarkup(row_width=2)
    for (cat, flavor), qty in cart.items():
        text = f"{flavor} x{qty}"
        kb.add(
            types.InlineKeyboardButton(text=f"➖ {flavor}", callback_data=f"decrease|{cat}|{flavor}"),
            types.InlineKeyboardButton(text=f"➕ {flavor}", callback_data=f"increase|{cat}|{flavor}")
        )
    if cart:
        kb.add(types.InlineKeyboardButton(text=t(chat_id, "checkout"), callback_data="checkout"))
        kb.add(types.InlineKeyboardButton(text=t(chat_id, "clear_cart"), callback_data="clear_cart"))
    return kb

def calculate_total(cart):
    """
    Посчитать итоговую стоимость корзины.
    """
    total = 0
    for (cat, flavor), qty in cart.items():
        price = menu[cat]["price"]
        total += price * qty
    return total

# ==== Хендлеры команд ====

@bot.message_handler(commands=["start", "help"])
def handle_start(message):
    chat_id = message.chat.id
    user_data.setdefault(chat_id, {
        "cart": {},
        "lang": "en",
        "current_category": None,
        "last_flavor": None
    })
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.add(
        types.KeyboardButton(text=t(chat_id, "choose_category")),
        types.KeyboardButton(text=t(chat_id, "view_cart")),
        types.KeyboardButton(text=t(chat_id, "change_language"))
    )
    bot.send_message(chat_id, t(chat_id, "welcome"), reply_markup=kb)

@bot.message_handler(commands=["language"])
def handle_language_command(message):
    chat_id = message.chat.id
    kb = types.InlineKeyboardMarkup(row_width=2)
    for lang_code in translations.keys():
        kb.add(types.InlineKeyboardButton(text=lang_code.upper(), callback_data=f"set_lang|{lang_code}"))
    bot.send_message(chat_id, t(chat_id, "choose_language"), reply_markup=kb)

# ==== Обработка простых текстовых сообщений ====

@bot.message_handler(func=lambda msg: msg.text == t(msg.chat.id, "choose_category"))
def handle_choose_category(message):
    chat_id = message.chat.id
    bot.send_message(chat_id, t(chat_id, "choose_category"), reply_markup=get_inline_categories(chat_id))

@bot.message_handler(func=lambda msg: msg.text == t(msg.chat.id, "view_cart"))
def handle_view_cart_text(message):
    chat_id = message.chat.id
    data = user_data.get(chat_id, {})
    cart = data.get("cart", {})
    if not cart:
        bot.send_message(chat_id, t(chat_id, "cart_empty"))
        return
    total = calculate_total(cart)
    text = t(chat_id, "your_cart") + "\n\n"
    for (cat, flavor), qty in cart.items():
        price = menu[cat]["price"]
        text += f"{flavor} x{qty} — {price * qty}₺\n"
    text += f"\n{t(chat_id, 'total')}: {total}₺"
    bot.send_message(chat_id, text, reply_markup=get_cart_keyboard(chat_id))

@bot.message_handler(func=lambda msg: msg.text == t(msg.chat.id, "change_language"))
def handle_change_language(message):
    chat_id = message.chat.id
    kb = types.InlineKeyboardMarkup(row_width=2)
    for lang_code in translations.keys():
        kb.add(types.InlineKeyboardButton(text=lang_code.upper(), callback_data=f"set_lang|{lang_code}"))
    bot.send_message(chat_id, t(chat_id, "choose_language"), reply_markup=kb)

# ==== Хендлеры callback_query ====

@bot.callback_query_handler(func=lambda call: call.data and call.data.startswith("set_lang|"))
def handle_set_language(call):
    _, lang_code = call.data.split("|", 1)
    chat_id = call.from_user.id
    user_data.setdefault(chat_id, {
        "cart": {},
        "lang": "en",
        "current_category": None,
        "last_flavor": None
    })
    if lang_code in translations:
        user_data[chat_id]["lang"] = lang_code
        bot.answer_callback_query(call.id)
        bot.send_message(chat_id, t(chat_id, "language_set").format(lang=lang_code.upper()))
    else:
        bot.answer_callback_query(call.id, t(chat_id, "error_invalid_lang"))

@bot.callback_query_handler(func=lambda call: call.data and call.data.startswith("category|"))
def handle_category(call):
    chat_id = call.from_user.id
    _, category = call.data.split("|", 1)
    user_data.setdefault(chat_id, {
        "cart": {},
        "lang": "en",
        "current_category": None,
        "last_flavor": None
    })
    user_data[chat_id]["current_category"] = category
    bot.answer_callback_query(call.id)
    bot.send_message(chat_id, f"{t(chat_id, 'choose_flavor')} «{category}»",
                     reply_markup=get_inline_flavors(chat_id, category))

@bot.callback_query_handler(func=lambda call: call.data == "go_back_to_categories")
def handle_go_back_to_categories(call):
    chat_id = call.from_user.id
    bot.answer_callback_query(call.id)
    bot.send_message(chat_id, t(chat_id, "choose_category"), reply_markup=get_inline_categories(chat_id))

@bot.callback_query_handler(func=lambda call: call.data and call.data.startswith("flavor|"))
def handle_flavor(call):
    # Обязательно отвечаем сразу, чтобы Телеграм-клиент не «зависал»
    bot.answer_callback_query(call.id)

    _, cat, flavor = call.data.split("|", 2)
    chat_id = call.from_user.id
    item_obj = next((i for i in menu[cat]["flavors"] if i["flavor"] == flavor), None)
    if not item_obj:
        # Вдруг товара сейчас нет в наличии
        bot.send_message(chat_id, t(chat_id, "item_unavailable"), reply_markup=get_inline_flavors(chat_id, cat))
        return

    price = menu[cat]["price"]
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(
        types.InlineKeyboardButton(
            text=f"➕ {price}₺ – {t(chat_id, 'add_to_cart')}",
            callback_data=f"add_to_cart|{cat}|{flavor}"
        )
    )
    kb.add(
        types.InlineKeyboardButton(
            text=f"⬅️ {t(chat_id, 'back_to_categories')}",
            callback_data=f"category|{cat}"
        )
    )
    user_data.setdefault(chat_id, {
        "cart": {},
        "lang": "en",
        "current_category": None,
        "last_flavor": None
    })
    user_data[chat_id]["last_flavor"] = flavor
    user_data[chat_id]["current_category"] = cat

    bot.send_message(
        chat_id,
        f"{flavor} — {item_obj.get('description', '')}",
        reply_markup=kb
    )

@bot.callback_query_handler(func=lambda call: call.data and call.data.startswith("go_back_to_category|"))
def handle_go_back_to_category(call):
    chat_id = call.from_user.id
    _, cat = call.data.split("|", 1)
    bot.answer_callback_query(call.id)
    bot.send_message(chat_id, f"{t(chat_id, 'choose_flavor')} «{cat}»",
                     reply_markup=get_inline_flavors(chat_id, cat))

@bot.callback_query_handler(func=lambda call: call.data and call.data.startswith("add_to_cart|"))
def handle_add_to_cart(call):
    bot.answer_callback_query(call.id)
    _, cat, flavor = call.data.split("|", 2)
    chat_id = call.from_user.id
    user_data.setdefault(chat_id, {
        "cart": {},
        "lang": "en",
        "current_category": None,
        "last_flavor": None
    })
    cart = user_data[chat_id].setdefault("cart", {})
    key = (cat, flavor)
    cart[key] = cart.get(key, 0) + 1

    # Обновляем базу
    cursor.execute(
        "INSERT OR REPLACE INTO cart (chat_id, category, flavor, quantity) VALUES (?, ?, ?, ?)",
        (chat_id, cat, flavor, cart[key])
    )
    conn.commit()

    bot.send_message(chat_id, t(chat_id, "item_added").format(flavor=flavor))

@bot.callback_query_handler(func=lambda call: call.data and call.data.startswith("increase|"))
def handle_increase(call):
    bot.answer_callback_query(call.id)
    _, cat, flavor = call.data.split("|", 2)
    chat_id = call.from_user.id
    cart = user_data.get(chat_id, {}).get("cart", {})
    key = (cat, flavor)
    if key in cart:
        cart[key] += 1
        cursor.execute(
            "UPDATE cart SET quantity = ? WHERE chat_id = ? AND category = ? AND flavor = ?",
            (cart[key], chat_id, cat, flavor)
        )
        conn.commit()
    bot.edit_message_reply_markup(chat_id, call.message.message_id, reply_markup=get_cart_keyboard(chat_id))

@bot.callback_query_handler(func=lambda call: call.data and call.data.startswith("decrease|"))
def handle_decrease(call):
    bot.answer_callback_query(call.id)
    _, cat, flavor = call.data.split("|", 2)
    chat_id = call.from_user.id
    cart = user_data.get(chat_id, {}).get("cart", {})
    key = (cat, flavor)
    if key in cart:
        if cart[key] > 1:
            cart[key] -= 1
            cursor.execute(
                "UPDATE cart SET quantity = ? WHERE chat_id = ? AND category = ? AND flavor = ?",
                (cart[key], chat_id, cat, flavor)
            )
        else:
            del cart[key]
            cursor.execute(
                "DELETE FROM cart WHERE chat_id = ? AND category = ? AND flavor = ?",
                (chat_id, cat, flavor)
            )
        conn.commit()
    bot.edit_message_reply_markup(chat_id, call.message.message_id, reply_markup=get_cart_keyboard(chat_id))

@bot.callback_query_handler(func=lambda call: call.data == "clear_cart")
def handle_clear_cart(call):
    bot.answer_callback_query(call.id)
    chat_id = call.from_user.id
    user_data.setdefault(chat_id, {"cart": {}, "lang": "en", "current_category": None, "last_flavor": None})
    user_data[chat_id]["cart"].clear()
    cursor.execute("DELETE FROM cart WHERE chat_id = ?", (chat_id,))
    conn.commit()
    bot.edit_message_text(chat_id=chat_id, message_id=call.message.message_id, text=t(chat_id, "cart_cleared"))

@bot.callback_query_handler(func=lambda call: call.data == "checkout")
def handle_checkout(call):
    bot.answer_callback_query(call.id)
    chat_id = call.from_user.id
    data = user_data.get(chat_id, {})
    cart = data.get("cart", {})
    if not cart:
        bot.send_message(chat_id, t(chat_id, "cart_empty"))
        return
    total = calculate_total(cart)
    text = t(chat_id, "checkout_summary") + "\n\n"
    for (cat, flavor), qty in cart.items():
        price = menu[cat]["price"]
        text += f"{flavor} x{qty} — {price * qty}₺\n"
    text += f"\n{t(chat_id, 'total')}: {total}₺\n\n{t(chat_id, 'thank_you')}"
    # Очищаем корзину
    user_data[chat_id]["cart"].clear()
    cursor.execute("DELETE FROM cart WHERE chat_id = ?", (chat_id,))
    conn.commit()
    bot.send_message(chat_id, text)

@bot.callback_query_handler(func=lambda call: call.data and call.data.startswith("view_flavor_stats|"))
def handle_view_flavor_stats(call):
    bot.answer_callback_query(call.id)
    _, cat = call.data.split("|", 1)
    # Собираем статистику продаж (для примера просто генерируем случайный график)
    flavors = [item["flavor"] for item in menu[cat]["flavors"]]
    sales = [requests.get(f"https://example.com/sales/{cat}/{fl}").json().get("sold", 0) for fl in flavors]
    # Построение графика
    plt.figure()
    plt.bar(flavors, sales)
    plt.title(f"{t(call.from_user.id, 'sales_stats_for')} {cat}")
    plt.ylabel("Продано, шт.")
    plt.xlabel("Вкусы")
    chart_path = f"{cat}_sales.png"
    plt.savefig(chart_path)
    plt.close()
    bot.send_photo(call.from_user.id, open(chart_path, "rb"))

@bot.callback_query_handler(func=lambda call: call.data and call.data.startswith("subscribe|"))
def handle_subscribe(call):
    bot.answer_callback_query(call.id)
    _, cat, flavor = call.data.split("|", 2)
    chat_id = call.from_user.id
    cursor.execute(
        "INSERT OR IGNORE INTO subscriptions (chat_id, category, flavor) VALUES (?, ?, ?)",
        (chat_id, cat, flavor)
    )
    conn.commit()
    bot.send_message(chat_id, t(chat_id, "subscribed").format(flavor=flavor))

@bot.callback_query_handler(func=lambda call: call.data == "view_cart")
def handle_view_cart(call):
    chat_id = call.from_user.id
    bot.answer_callback_query(call.id)
    data = user_data.get(chat_id, {})
    cart = data.get("cart", {})
    if not cart:
        bot.send_message(chat_id, t(chat_id, "cart_empty"))
        return
    total = calculate_total(cart)
    text = t(chat_id, "your_cart") + "\n\n"
    for (cat, flavor), qty in cart.items():
        price = menu[cat]["price"]
        text += f"{flavor} x{qty} — {price * qty}₺\n"
    text += f"\n{t(chat_id, 'total')}: {total}₺"
    bot.send_message(chat_id, text, reply_markup=get_cart_keyboard(chat_id))

# Обработка подписок: уведомление, когда товар снова в наличии
def check_subscriptions():
    while True:
        cursor.execute("SELECT chat_id, category, flavor FROM subscriptions")
        subs = cursor.fetchall()
        for chat_id, cat, flavor in subs:
            # Проверяем наличие товара (в реальном случае, обращаемся к API или базе)
            item_obj = next((i for i in menu[cat]["flavors"] if i["flavor"] == flavor), None)
            if item_obj and item_obj.get("stock", 0) > 0:
                bot.send_message(chat_id, t(chat_id, "item_back_in_stock").format(flavor=flavor))
                cursor.execute(
                    "DELETE FROM subscriptions WHERE chat_id = ? AND category = ? AND flavor = ?",
                    (chat_id, cat, flavor)
                )
                conn.commit()
        time.sleep(3600)  # Проверяем каждый час

# Запускаем поток для проверки подписок
sub_thread = threading.Thread(target=check_subscriptions, daemon=True)
sub_thread.start()

# ==== Запуск бота ====

if __name__ == "__main__":
    bot.remove_webhook()
    bot.polling(none_stop=True)
