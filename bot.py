import os
import json
import sqlite3
import threading
import time
from datetime import datetime
import telebot
from telebot import types

# ─── Пути к файлам ───────────────────────────────────────────────────────────────

MENU_PATH = "menu.json"
LANG_PATH = "languages.json"
DB_PATH   = "bot.db"

# ─── Бот и БД ────────────────────────────────────────────────────────────────────

TOKEN = os.getenv("TOKEN")
if not TOKEN:
    raise RuntimeError("Переменная окружения TOKEN не задана! Запустите контейнер с -e TOKEN=<ваш_токен>.")
bot = telebot.TeleBot(TOKEN, parse_mode="HTML")

def init_db():
    conn  = sqlite3.connect(DB_PATH, check_same_thread=False)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS cart (
            chat_id INTEGER,
            category TEXT,
            flavor TEXT,
            quantity INTEGER,
            PRIMARY KEY (chat_id, category, flavor)
        );
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS subscriptions (
            chat_id INTEGER,
            category TEXT,
            flavor TEXT,
            PRIMARY KEY (chat_id, category, flavor)
        );
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS orders (
            order_id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER,
            items_json TEXT,
            address TEXT,
            contact TEXT,
            timestamp TEXT
        );
    """)
    conn.commit()
    return conn, cursor

conn, cursor = init_db()

# ─── Загрузка JSON ───────────────────────────────────────────────────────────────

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

menu         = load_json(MENU_PATH)
translations = load_json(LANG_PATH)

# ─── Словарь с временными данными по пользователям ──────────────────────────────

user_data = {}
# Структура user_data[chat_id] будет примерно такая:
# {
#   "lang": None or "ru" or "en",
#   "cart": { (category, flavor): quantity, … },
#   "current_category": None or <string>,
#   "last_flavor": None or <string>,
#   "wait_for_address": False/True,
#   "wait_for_contact": False/True,
#   "address": None or <string>,
#   "contact": None or <string>
# }

# ─── Функция для получения перевода ───────────────────────────────────────────────

def t(chat_id, key):
    """
    Возвращает перевод по ключу для данного пользователя.
    Если язык ещё не установлен (или не найден ключ), берёт "ru" по умолчанию.
    """
    lang = user_data.get(chat_id, {}).get("lang")
    if not lang or lang not in translations:
        lang = "ru"
    return translations.get(lang, {}).get(key, key)


# ─── Клавиатуры ──────────────────────────────────────────────────────────────────

def get_inline_language_buttons():
    """
    Inline‐клавиатура для выбора языка (RU / EN).
    """
    kb = types.InlineKeyboardMarkup(row_width=2)
    for lang_code in translations.keys():
        kb.add(types.InlineKeyboardButton(
            text=lang_code.upper(),
            callback_data=f"set_lang|{lang_code}"
        ))
    return kb

def get_reply_main_menu(chat_id):
    """
    Reply‐клавиатура главного меню:
      [ Выбрать категорию ]  [ Посмотреть корзину ]
      [ Изменить язык      ]
    """
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.add(
        types.KeyboardButton(text=t(chat_id, "choose_category")),
        types.KeyboardButton(text=t(chat_id, "view_cart"))
    )
    kb.add(types.KeyboardButton(text=t(chat_id, "change_language")))
    return kb

def get_inline_categories(chat_id):
    """
    Inline‐клавиатура со списком категорий из menu.json.
    """
    kb = types.InlineKeyboardMarkup(row_width=1)
    for category, data in menu.items():
        price = data.get("price", 0)
        kb.add(types.InlineKeyboardButton(
            text=f"{category} — {price}₺",
            callback_data=f"category|{category}"
        ))
    # Кнопка «Назад» возвращает в главное меню:
    kb.add(types.InlineKeyboardButton(text=t(chat_id, "back"), callback_data="back_to_main"))
    return kb

def get_inline_flavors(chat_id, category):
    """
    Inline‐клавиатура со вкусами для выбранной категории.
    """
    kb = types.InlineKeyboardMarkup(row_width=2)
    for item in menu[category]["flavors"]:
        flavor = item["flavor"]
        emoji  = item.get("emoji", "")
        kb.add(types.InlineKeyboardButton(
            text=f"{emoji} {flavor}",
            callback_data=f"flavor|{category}|{flavor}"
        ))
    kb.add(types.InlineKeyboardButton(
        text=t(chat_id, "back_to_categories"),
        callback_data="go_back_to_categories"
    ))
    return kb

def get_cart_keyboard(chat_id):
    """
    Inline‐клавиатура для управления корзиной: изменить количество / завершить заказ / назад
    """
    data = user_data.get(chat_id, {})
    cart = data.get("cart", {})
    kb = types.InlineKeyboardMarkup(row_width=2)
    for (cat, flavor), qty in cart.items():
        kb.add(
            types.InlineKeyboardButton(text=f"➖ {flavor}", callback_data=f"decrease|{cat}|{flavor}"),
            types.InlineKeyboardButton(text=f"➕ {flavor}", callback_data=f"increase|{cat}|{flavor}")
        )
    if cart:
        kb.add(
            types.InlineKeyboardButton(text=t(chat_id, "finish_order"), callback_data="checkout"),
            types.InlineKeyboardButton(text=t(chat_id, "back"), callback_data="back_to_main")
        )
    else:
        # Если корзина пуста, просто предлагаем вернуться назад
        kb.add(types.InlineKeyboardButton(text=t(chat_id, "back"), callback_data="back_to_main"))
    return kb

def calculate_total(cart):
    """
    Считает итоговую сумму корзины.
    """
    total = 0
    for (cat, flavor), qty in cart.items():
        total += menu[cat]["price"] * qty
    return total

def address_keyboard(chat_id):
    """
    Reply‐клавиатура для запроса адреса:
      [ 📍 Поделиться геопозицией ]
      [ ✏️ Ввести адрес         ]
      [ ⬅️ Назад                ]
    """
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
    kb.add(types.KeyboardButton(
        text=t(chat_id, "share_location"),
        request_location=True
    ))
    kb.add(types.KeyboardButton(text=t(chat_id, "enter_address_text")))
    kb.add(types.KeyboardButton(text=t(chat_id, "back")))
    return kb

def contact_keyboard(chat_id):
    """
    Reply‐клавиатура для запроса контакта:
      [ 📞 Поделиться контактом ]
      [ ✏️ Ввести ник           ]
      [ ⬅️ Назад               ]
    """
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
    kb.add(types.KeyboardButton(
        text=t(chat_id, "share_contact"),
        request_contact=True
    ))
    kb.add(types.KeyboardButton(text=t(chat_id, "enter_nickname")))
    kb.add(types.KeyboardButton(text=t(chat_id, "back")))
    return kb


# ─── Обработчики команд ─────────────────────────────────────────────────────────

@bot.message_handler(commands=["start"])
def cmd_start(message):
    chat_id = message.chat.id
    # Инициализируем состояние пользователя заново
    user_data[chat_id] = {
        "lang": None,
        "cart": {},
        "current_category": None,
        "last_flavor": None,
        "wait_for_address": False,
        "wait_for_contact": False,
        "address": None,
        "contact": None
    }
    # Приглашаем выбрать язык (текст сразу на русском/английском, поскольку lang ещё не задан)
    text = "Выберите язык / Choose your language:"
    bot.send_message(chat_id, text, reply_markup=get_inline_language_buttons())

@bot.message_handler(commands=["language"])
def cmd_language(message):
    """
    По команде /language можно повторно вызвать выбор языка.
    """
    chat_id = message.chat.id
    # Если пользователь ещё не инициализирован — инициализируем только поле lang=None
    user_data.setdefault(chat_id, {
        "lang": None,
        "cart": {},
        "current_category": None,
        "last_flavor": None,
        "wait_for_address": False,
        "wait_for_contact": False,
        "address": None,
        "contact": None
    })
    bot.send_message(chat_id, "Выберите язык / Choose your language:", reply_markup=get_inline_language_buttons())


# ─── Обработка простых текстовых сообщений ───────────────────────────────────────

@bot.message_handler(func=lambda msg: user_data.get(msg.chat.id, {}).get("lang") is not None and
                              msg.text == t(msg.chat.id, "choose_category"))
def handle_choose_category(message):
    chat_id = message.chat.id
    bot.send_message(
        chat_id,
        t(chat_id, "choose_category"),
        reply_markup=get_inline_categories(chat_id)
    )

@bot.message_handler(func=lambda msg: user_data.get(msg.chat.id, {}).get("lang") is not None and
                              msg.text == t(msg.chat.id, "view_cart"))
def handle_view_cart_text(message):
    chat_id = message.chat.id
    data = user_data.get(chat_id, {})
    cart = data.get("cart", {})
    if not cart:
        bot.send_message(chat_id, t(chat_id, "cart_empty"), reply_markup=get_reply_main_menu(chat_id))
        return

    total = calculate_total(cart)
    text = t(chat_id, "view_cart") + "\n\n"
    for (cat, flavor), qty in cart.items():
        price = menu[cat]["price"]
        text += f"{flavor} x{qty} — {price * qty}₺\n"
    # Поле «total» в файла перевода может отсутствовать, тогда покажем «Total» по умолчанию:
    total_key = t(chat_id, "total") if translations.get(user_data[chat_id]["lang"], {}).get("total") else "Total"
    text += f"\n{total_key}: {total}₺"
    bot.send_message(chat_id, text, reply_markup=get_cart_keyboard(chat_id))


@bot.message_handler(func=lambda msg: user_data.get(msg.chat.id, {}).get("lang") is not None and
                              msg.text == t(msg.chat.id, "change_language"))
def handle_change_language(message):
    chat_id = message.chat.id
    bot.send_message(chat_id, t(chat_id, "choose_language"), reply_markup=get_inline_language_buttons())

# Обработка ввода адреса (текст или геопозиция), если ранее бот запросил адрес
@bot.message_handler(func=lambda msg: user_data.get(msg.chat.id, {}).get("wait_for_address") and
                              msg.content_type in ["text", "location"])
def handle_address(message):
    chat_id = message.chat.id
    data = user_data[chat_id]
    data["wait_for_address"] = False

    if message.content_type == "location" and message.location:
        lat, lon = message.location.latitude, message.location.longitude
        address_str = f"{lat}, {lon}"
    else:
        address_str = message.text

    data["address"] = address_str
    data["wait_for_contact"] = True
    bot.send_message(chat_id, t(chat_id, "enter_contact"), reply_markup=contact_keyboard(chat_id))

# Обработка ввода контакта (телефон или текст), если ранее бот запросил контакт
@bot.message_handler(func=lambda msg: user_data.get(msg.chat.id, {}).get("wait_for_contact") and
                              msg.content_type in ["text", "contact"])
def handle_contact(message):
    chat_id = message.chat.id
    data = user_data[chat_id]
    data["wait_for_contact"] = False

    if message.content_type == "contact" and message.contact:
        contact_info = message.contact.phone_number
    else:
        contact_info = message.text

    data["contact"] = contact_info

    # Теперь сохраняем заказ в БД
    cart = data.get("cart", {})
    if not cart:
        bot.send_message(chat_id, t(chat_id, "cart_empty"), reply_markup=get_reply_main_menu(chat_id))
        return

    items = []
    for (cat, flavor), qty in cart.items():
        items.append({"category": cat, "flavor": flavor, "quantity": qty})
    items_json = json.dumps(items, ensure_ascii=False)
    address   = data.get("address", "")
    contact   = data.get("contact", "")
    timestamp = datetime.utcnow().isoformat()

    cursor.execute(
        "INSERT INTO orders (chat_id, items_json, address, contact, timestamp) VALUES (?, ?, ?, ?, ?)",
        (chat_id, items_json, address, contact, timestamp)
    )
    conn.commit()

    bot.send_message(chat_id, t(chat_id, "order_accepted"), reply_markup=get_reply_main_menu(chat_id))

    # Очищаем корзину и адрес/контакт в памяти
    data["cart"].clear()
    data["address"] = None
    data["contact"] = None


# ─── Обработка inline‐callback_query ─────────────────────────────────────────────

@bot.callback_query_handler(func=lambda call: call.data and call.data.startswith("set_lang|"))
def handle_set_language(call):
    chat_id   = call.from_user.id
    _, code  = call.data.split("|", 1)
    if code in translations:
        user_data.setdefault(chat_id, {
            "lang": None,
            "cart": {},
            "current_category": None,
            "last_flavor": None,
            "wait_for_address": False,
            "wait_for_contact": False,
            "address": None,
            "contact": None
        })
        user_data[chat_id]["lang"] = code
        bot.answer_callback_query(call.id)
        # Сообщаем, что язык установлен, и показываем главное меню
        bot.send_message(chat_id, t(chat_id, "lang_set"))
        bot.send_message(chat_id, t(chat_id, "welcome"), reply_markup=get_reply_main_menu(chat_id))
    else:
        bot.answer_callback_query(call.id, t(chat_id, "error_invalid"))

@bot.callback_query_handler(func=lambda call: call.data == "back_to_main")
def handle_back_to_main(call):
    chat_id = call.from_user.id
    bot.answer_callback_query(call.id)
    bot.send_message(chat_id, t(chat_id, "welcome"), reply_markup=get_reply_main_menu(chat_id))

@bot.callback_query_handler(func=lambda call: call.data and call.data.startswith("category|"))
def handle_category(call):
    chat_id  = call.from_user.id
    _, cat  = call.data.split("|", 1)

    user_data.setdefault(chat_id, {
        "lang": None,
        "cart": {},
        "current_category": None,
        "last_flavor": None,
        "wait_for_address": False,
        "wait_for_contact": False,
        "address": None,
        "contact": None
    })
    user_data[chat_id]["current_category"] = cat

    bot.answer_callback_query(call.id)
    bot.send_message(
        chat_id,
        f"{t(chat_id, 'choose_flavor')} «{cat}»",
        reply_markup=get_inline_flavors(chat_id, cat)
    )

@bot.callback_query_handler(func=lambda call: call.data == "go_back_to_categories")
def handle_go_back_to_categories(call):
    chat_id = call.from_user.id
    bot.answer_callback_query(call.id)
    bot.send_message(chat_id, t(chat_id, "choose_category"), reply_markup=get_inline_categories(chat_id))

@bot.callback_query_handler(func=lambda call: call.data and call.data.startswith("flavor|"))
def handle_flavor(call):
    bot.answer_callback_query(call.id)
    _, cat, flavor = call.data.split("|", 2)
    chat_id = call.from_user.id

    item_obj = next((i for i in menu[cat]["flavors"] if i["flavor"] == flavor), None)
    if not item_obj or item_obj.get("stock", 0) <= 0:
        bot.send_message(chat_id, t(chat_id, "error_invalid"), reply_markup=get_inline_flavors(chat_id, cat))
        return

    price = menu[cat]["price"]
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(types.InlineKeyboardButton(
        text=f"{t(chat_id, 'add_to_cart')} ({price}₺)",
        callback_data=f"add_to_cart|{cat}|{flavor}"
    ))
    kb.add(types.InlineKeyboardButton(
        text=t(chat_id, "back_to_categories"),
        callback_data="go_back_to_categories"
    ))

    user_data.setdefault(chat_id, {
        "lang": None,
        "cart": {},
        "current_category": None,
        "last_flavor": None,
        "wait_for_address": False,
        "wait_for_contact": False,
        "address": None,
        "contact": None
    })
    user_data[chat_id]["last_flavor"] = flavor
    user_data[chat_id]["current_category"] = cat

    bot.send_message(
        chat_id,
        f"{flavor} — {item_obj.get('description', '')}",
        reply_markup=kb
    )

@bot.callback_query_handler(func=lambda call: call.data and call.data.startswith("add_to_cart|"))
def handle_add_to_cart(call):
    bot.answer_callback_query(call.id)
    _, cat, flavor = call.data.split("|", 2)
    chat_id = call.from_user.id

    user_data.setdefault(chat_id, {
        "lang": None,
        "cart": {},
        "current_category": None,
        "last_flavor": None,
        "wait_for_address": False,
        "wait_for_contact": False,
        "address": None,
        "contact": None
    })
    cart = user_data[chat_id].setdefault("cart", {})
    key = (cat, flavor)
    cart[key] = cart.get(key, 0) + 1

    # Сохраняем в SQLite
    cursor.execute(
        "INSERT OR REPLACE INTO cart (chat_id, category, flavor, quantity) VALUES (?, ?, ?, ?)",
        (chat_id, cat, flavor, cart[key])
    )
    conn.commit()

    count = sum(cart.values())
    bot.send_message(
        chat_id,
        t(chat_id, "added_to_cart").format(flavor=flavor, count=count),
        reply_markup=get_reply_main_menu(chat_id)
    )

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

@bot.callback_query_handler(func=lambda call: call.data == "checkout")
def handle_checkout(call):
    bot.answer_callback_query(call.id)
    chat_id = call.from_user.id
    data = user_data.get(chat_id, {})
    cart = data.get("cart", {})
    if not cart:
        bot.send_message(chat_id, t(chat_id, "cart_empty"), reply_markup=get_reply_main_menu(chat_id))
        return

    total = calculate_total(cart)
    text = t(chat_id, "finish_order") + "\n\n"
    for (cat, flavor), qty in cart.items():
        price = menu[cat]["price"]
        text += f"{flavor} x{qty} — {price * qty}₺\n"
    text += f"\n{t(chat_id, 'enter_address')}"
    data["wait_for_address"] = True
    bot.send_message(chat_id, text, reply_markup=address_keyboard(chat_id))

# ─── Поток для проверки подписок (опционально) ─────────────────────────────────

def check_subscriptions():
    while True:
        cursor.execute("SELECT chat_id, category, flavor FROM subscriptions")
        subs = cursor.fetchall()
        for chat_id, cat, flavor in subs:
            item_obj = next((i for i in menu[cat]["flavors"] if i["flavor"] == flavor), None)
            if item_obj and item_obj.get("stock", 0) > 0:
                bot.send_message(chat_id, t(chat_id, "item_back_in_stock").format(flavor=flavor))
                cursor.execute(
                    "DELETE FROM subscriptions WHERE chat_id = ? AND category = ? AND flavor = ?",
                    (chat_id, cat, flavor)
                )
                conn.commit()
        time.sleep(3600)

sub_thread = threading.Thread(target=check_subscriptions, daemon=True)
sub_thread.start()

# ─── Запуск бота ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    bot.remove_webhook()
    bot.polling(none_stop=True)
