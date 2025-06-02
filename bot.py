# -*- coding: utf-8 -*-
import os
import json
import requests
import telebot
from telebot import types

# —————————————————————————————————————————————————————————————
#   1. Загрузка переменных окружения
# —————————————————————————————————————————————————————————————
TOKEN = os.getenv("TOKEN")
if not TOKEN:
    raise RuntimeError("Переменная окружения TOKEN не задана! Запустите бот с -e TOKEN=<ваш_токен>.")

GROUP_CHAT_ID = int(os.getenv("GROUP_CHAT_ID", "0"))      # ID группового чата для английских уведомлений
PERSONAL_CHAT_ID = int(os.getenv("PERSONAL_CHAT_ID", "0"))  # ID личного чата администратора для русских уведомлений

bot = telebot.TeleBot(TOKEN, parse_mode="HTML")

# —————————————————————————————————————————————————————————————
#   2. Пути к JSON-файлам
# —————————————————————————————————————————————————————————————
MENU_PATH = "menu.json"
LANG_PATH = "languages.json"

# —————————————————————————————————————————————————————————————
#   3. Функции загрузки JSON
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
#   4. Хранилище данных пользователей
# —————————————————————————————————————————————————————————————
user_data = {}
# Структура user_data[chat_id]:
# {
#   "lang": "ru" или "en",
#   "cart": [ { "category": str, "flavor": str, "price": int }, ... ],
#   "current_category": str или None,
#   "edit_phase": None или один из этапов редактирования меню,
#   "edit_cat": str (категория при редактировании),
#   "edit_flavor": str (вкус при редактировании),
#   "edit_cart_phase": None / "choose_action" / "enter_qty",
#   "edit_index": int (индекс позиции в корзине),
#   "wait_for_address": bool,
#   "wait_for_contact": bool,
#   "wait_for_comment": bool,
#   "address": str,
#   "contact": str,
#   "comment": str
# }

# —————————————————————————————————————————————————————————————
#   5. Функция перевода по ключу
# —————————————————————————————————————————————————————————————
def t(chat_id: int, key: str) -> str:
    lang = user_data.get(chat_id, {}).get("lang", "ru")
    return translations.get(lang, {}).get(key, key)

# —————————————————————————————————————————————————————————————
#   6. Функции для клавиатур
# —————————————————————————————————————————————————————————————

def edit_action_keyboard():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
    kb.add("➕ Add Category", "➖ Remove Category")
    kb.add("💲 Fix Price", "ALL IN", "🔄 Actual Flavor")
    kb.add("⬅️ Back", "❌ Cancel")
    return kb

def get_inline_categories(chat_id: int) -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup(row_width=2)
    for cat in menu.keys():
        kb.add(types.InlineKeyboardButton(text=cat, callback_data=f"category|{cat}"))
    return kb

def get_inline_flavors(chat_id: int, cat: str) -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup(row_width=1)
    price = menu[cat]["price"]
    for item in menu[cat]["flavors"]:
        if item.get("stock", 0) <= 0:
            continue
        emoji = item.get("emoji", "")
        flavor_name = item["flavor"]
        stock = item.get("stock", 0)
        label = f"{emoji} {flavor_name} — {price}₺ [{stock}шт]"
        kb.add(types.InlineKeyboardButton(text=label, callback_data=f"flavor|{cat}|{flavor_name}"))
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
    kb = types.InlineKeyboardMarkup(row_width=2)
    data = user_data.get(chat_id, {})
    cart = data.get("cart", [])
    if not cart:
        return None
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

def get_flavors_keyboard(cat: str) -> types.ReplyKeyboardMarkup:
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
    price = menu[cat]["price"]
    for it in menu[cat]["flavors"]:
        stock = it.get("stock", 0)
        if stock > 0:
            emoji = it.get("emoji", "").strip()
            flavor = it["flavor"]
            label = f"{emoji} {flavor} ({price}₺) [{stock} шт]" if emoji else f"{flavor} ({price}₺) [{stock} шт]"
            kb.add(label)
    kb.add("⬅️ Назад")
    return kb

def description_keyboard() -> types.ReplyKeyboardMarkup:
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
    kb.add("⬅️ Назад")
    return kb

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
#   7. Функция для получения курсов валют (RUB, USD, UAH)
# —————————————————————————————————————————————————————————————
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

# —————————————————————————————————————————————————————————————
#   8. Функция для перевода комментария через Google Translate API
# —————————————————————————————————————————————————————————————
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
#   9. Хендлер /start → выбор языка
# —————————————————————————————————————————————————————————————
@bot.message_handler(commands=['start'])
def cmd_start(message):
    chat_id = message.chat.id
    # Сбросим все старые флаги
    user_data.setdefault(chat_id, {
        "lang": None,
        "cart": [],
        "current_category": None,
        "edit_phase": None,
        "edit_cat": None,
        "edit_flavor": None,
        "edit_cart_phase": None,
        "edit_index": None,
        "wait_for_address": False,
        "wait_for_contact": False,
        "wait_for_comment": False
    })
    data = user_data[chat_id]
    data["cart"] = []
    data["current_category"] = None
    data["edit_phase"] = None
    data["edit_cat"] = None
    data["edit_flavor"] = None
    data["edit_cart_phase"] = None
    data["edit_index"] = None
    data["wait_for_address"] = False
    data["wait_for_contact"] = False
    data["wait_for_comment"] = False

    # Предложение выбрать язык
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        types.InlineKeyboardButton(text="Русский 🇷🇺", callback_data="set_lang|ru"),
        types.InlineKeyboardButton(text="English 🇬🇧", callback_data="set_lang|en")
    )
    bot.send_message(chat_id, "Выберите язык / Choose your language:", reply_markup=kb)

# —————————————————————————————————————————————————————————————
#   10. Хендлер выбора языка (inline callback)
# —————————————————————————————————————————————————————————————
@bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("set_lang|"))
def handle_set_lang(call):
    chat_id = call.from_user.id
    _, lang_code = call.data.split("|", 1)
    data = user_data.setdefault(chat_id, {})
    data["lang"] = lang_code
    # Инициализируем корзину, если ещё не было
    data.setdefault("cart", [])
    data["current_category"] = None
    data["edit_phase"] = None
    data["edit_cat"] = None
    data["edit_flavor"] = None
    data["edit_cart_phase"] = None
    data["edit_index"] = None
    data["wait_for_address"] = False
    data["wait_for_contact"] = False
    data["wait_for_comment"] = False

    bot.answer_callback_query(call.id, t(chat_id, "lang_set"))
    # После выбора языка выводим категории
    bot.send_message(chat_id, t(chat_id, "welcome"), reply_markup=get_inline_categories(chat_id))

# —————————————————————————————————————————————————————————————
#   11. Хендлер /change (админ-режим редактирования меню)
# —————————————————————————————————————————————————————————————
@bot.message_handler(commands=['change'])
def cmd_change(message):
    chat_id = message.chat.id
    data = user_data.setdefault(chat_id, {
        "lang": "ru",
        "cart": [],
        "current_category": None,
        "edit_phase": None,
        "edit_cat": None,
        "edit_flavor": None,
        "edit_cart_phase": None,
        "edit_index": None,
        "wait_for_address": False,
        "wait_for_contact": False,
        "wait_for_comment": False
    })
    data['edit_phase'] = 'choose_action'
    bot.send_message(chat_id, "Menu editing: choose action", reply_markup=edit_action_keyboard())

# —————————————————————————————————————————————————————————————
#   12. Универсальный хендлер: здесь обрабатываем и админ-режим, и пользовательский поток
# —————————————————————————————————————————————————————————————
@bot.message_handler(content_types=['text','location','venue','contact'])
def universal_handler(message):
    chat_id = message.chat.id
    text = message.text or ""
    data = user_data.setdefault(chat_id, {
        "lang": "ru",
        "cart": [],
        "current_category": None,
        "edit_phase": None,
        "edit_cat": None,
        "edit_flavor": None,
        "edit_cart_phase": None,
        "edit_index": None,
        "wait_for_address": False,
        "wait_for_contact": False,
        "wait_for_comment": False
    })

    # ——— Если мы в режиме редактирования меню ———
    if data.get('edit_phase'):
        phase = data['edit_phase']

        # 1) Главное меню редактирования
        if phase == 'choose_action':
            if text == "⬅️ Back":
                data['edit_cat'] = None
                data['edit_flavor'] = None
                data['edit_phase'] = None
                bot.send_message(chat_id, "Editing cancelled. Back to main menu.", reply_markup=get_inline_categories(chat_id))
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

            # Если не распознали кнопку — напомним действия
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
            bot.send_message(chat_id, f"Category «{new_cat}» added with price 1300₺.", reply_markup=edit_action_keyboard())
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

        # 4) Выбрать категорию для фиксации цены
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
                data['edit_cat'] = None
                data['edit_phase'] = 'choose_action'
                bot.send_message(chat_id, "Back to editing menu:", reply_markup=edit_action_keyboard())
                return
            cat = data.get('edit_cat')
            try:
                new_price = float(text.strip())
            except:
                kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
                kb.add("⬅️ Back")
                bot.send_message(chat_id, "Invalid price format. Enter a number, e.g. 1500:", reply_markup=kb)
                return
            menu[cat]["price"] = int(new_price)
            with open(MENU_PATH, "w", encoding="utf-8") as f:
                json.dump(menu, f, ensure_ascii=False, indent=2)
            bot.send_message(chat_id, f"Price for category «{cat}» set to {int(new_price)}₺.", reply_markup=edit_action_keyboard())
            data['edit_cat'] = None
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
                    current_list.append(f"{itm['flavor']} - {itm['stock']}")
                joined = "\n".join(current_list) if current_list else "(empty)"
                kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
                kb.add("⬅️ Back")
                bot.send_message(
                    chat_id,
                    f"Current flavors in «{text}» (one per line as “Name - qty”):\n\n{joined}\n\nSend the full updated list in the same format. Each line: “Name - qty”.",
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
                data['edit_cat'] = None
                data['edit_phase'] = 'choose_action'
                bot.send_message(chat_id, "Back to editing menu:", reply_markup=edit_action_keyboard())
                return
            cat = data.get('edit_cat')
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
            menu[cat]["flavors"] = new_flavors
            with open(MENU_PATH, "w", encoding="utf-8") as f:
                json.dump(menu, f, ensure_ascii=False, indent=2)
            bot.send_message(chat_id, f"Full flavor list for «{cat}» has been replaced.", reply_markup=edit_action_keyboard())
            data['edit_cat'] = None
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
                for it in menu[text]["flavors"]:
                    flavor = it["flavor"]
                    stock = it.get("stock", 0)
                    kb.add(f"{flavor} [{stock} шт]")
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
                data['edit_cat'] = None
                data['edit_phase'] = 'choose_action'
                bot.send_message(chat_id, "Back to editing menu:", reply_markup=edit_action_keyboard())
                return
            cat = data.get('edit_cat')
            flavor_name = text.split(' [')[0]
            exists = any(it["flavor"] == flavor_name for it in menu.get(cat, {}).get("flavors", []))
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

        # 10) Ввод актуального количества для выбранного вкуса
        if phase == 'enter_actual_qty':
            if text == "⬅️ Back":
                data['edit_flavor'] = None
                data['edit_phase'] = 'choose_action'
                bot.send_message(chat_id, "Back to editing menu:", reply_markup=edit_action_keyboard())
                return
            cat = data.get('edit_cat')
            flavor = data.get('edit_flavor')
            if not text.isdigit():
                kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
                kb.add("⬅️ Back")
                bot.send_message(chat_id, "Please enter a valid number!", reply_markup=kb)
                return
            new_stock = int(text)
            for it in menu[cat]["flavors"]:
                if it["flavor"] == flavor:
                    it["stock"] = new_stock
                    break
            with open(MENU_PATH, "w", encoding="utf-8") as f:
                json.dump(menu, f, ensure_ascii=False, indent=2)
            bot.send_message(chat_id, f"Stock for flavor «{flavor}» in category «{cat}» set to {new_stock}.", reply_markup=edit_action_keyboard())
            data['edit_cat'] = None
            data['edit_flavor'] = None
            data['edit_phase'] = 'choose_action'
            return

        # Неизвестный этап → возвращаемся
        data['edit_phase'] = 'choose_action'
        bot.send_message(chat_id, "Back to editing menu:", reply_markup=edit_action_keyboard())
        return

    # ——— Если ожидаем ввод адреса ———
    if data.get('wait_for_address'):
        if text == "⬅️ Назад":
            data['wait_for_address'] = False
            data['current_category'] = None
            bot.send_message(chat_id, "Адрес не указан. Вернитесь к выбору категории:", reply_markup=get_inline_categories(chat_id))
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
            bot.send_message(chat_id, "Напишите адрес текстом:", reply_markup=types.ReplyKeyboardRemove())
            return
        elif message.content_type == 'text' and message.text:
            address = message.text.strip()
        else:
            bot.send_message(chat_id, "Нужен адрес или локация:", reply_markup=address_keyboard())
            return

        data['address'] = address
        data['wait_for_address'] = False
        data['wait_for_contact'] = True
        kb = contact_keyboard()
        bot.send_message(chat_id, "Укажите контакт для связи:", reply_markup=kb)
        return

    # ——— Если ожидаем ввод контакта ———
    if data.get('wait_for_contact'):
        if text == "⬅️ Назад":
            data['wait_for_address'] = True
            data['wait_for_contact'] = False
            bot.send_message(chat_id, "Вернулись к выбору адреса. Укажите адрес:", reply_markup=address_keyboard())
            return

        if text == "✏️ Ввести ник":
            bot.send_message(chat_id, "Введите ваш Telegram-ник (без @):", reply_markup=types.ReplyKeyboardRemove())
            return

        if message.content_type == 'contact' and message.contact:
            contact = message.contact.phone_number
        elif message.content_type == 'text' and message.text:
            contact = "@" + message.text.strip().lstrip("@")
        else:
            bot.send_message(chat_id, "Выберите способ связи:", reply_markup=contact_keyboard())
            return

        data['contact'] = contact
        data['wait_for_contact'] = False
        data['wait_for_comment'] = True
        kb = comment_keyboard()
        bot.send_message(chat_id, "Напишите комментарий к заказу:", reply_markup=kb)
        return

    # ——— Если ожидаем ввод комментария ———
    if data.get('wait_for_comment'):
        if text == "⬅️ Назад":
            data['wait_for_contact'] = True
            data['wait_for_comment'] = False
            bot.send_message(chat_id, "Вернулись к выбору контакта. Укажите контакт:", reply_markup=contact_keyboard())
            return

        if text == "✏️ Комментарий к заказу":
            bot.send_message(chat_id, "Введите текст комментария:", reply_markup=types.ReplyKeyboardRemove())
            return

        if message.content_type == 'text' and text != "📤 Отправить заказ":
            data['comment'] = text.strip()
            bot.send_message(chat_id, "Комментарий сохранён. Нажмите 📤 Отправить заказ.", reply_markup=comment_keyboard())
            return

        if text == "📤 Отправить заказ":
            cart = data['cart']
            total_try = sum(i['price'] for i in cart)
            summary_rus = "\n".join(f"{i['category']}: {i['flavor']} — {i['price']}₺" for i in cart)
            summary_en = "\n".join(f"{i['category']}: {i['flavor']} — {i['price']}₺" for i in cart)

            rates = fetch_rates()
            rub = round(total_try * rates.get("RUB", 0) + 500, 2)
            usd = round(total_try * rates.get("USD", 0) + 2, 2)
            uah = round(total_try * rates.get("UAH", 0) + 200, 2)
            conv = f"({rub}₽, ${usd}, ₴{uah})"

            # Русскоязычная копия админу
            full_rus = (
                f"📥 Новый заказ от @{message.from_user.username or message.from_user.first_name}:\n\n"
                f"{summary_rus}\n\n"
                f"Итог: {total_try}₺ {conv}\n"
                f"📍 Адрес: {data.get('address', '—')}\n"
                f"📱 Контакт: {data.get('contact', '—')}\n"
                f"💬 Комментарий: {data.get('comment', '—')}"
            )
            bot.send_message(PERSONAL_CHAT_ID, full_rus)

            # Переводим комментарий на английский (если он существует)
            comment_ru = data.get('comment', '')
            comment_en = translate_to_en(comment_ru) if comment_ru else "—"

            # Англоязычный итог для группы
            full_en = (
                f"📥 New order from @{message.from_user.username or message.from_user.first_name}:\n\n"
                f"{summary_en}\n\n"
                f"Total: {total_try}₺ {conv}\n"
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
                cat = o['category']
                for itm in menu[cat]["flavors"]:
                    if itm['flavor'] == o['flavor']:
                        itm['stock'] = max(itm.get('stock', 1) - 1, 0)
                        break
            with open(MENU_PATH, "w", encoding="utf-8") as f:
                json.dump(menu, f, ensure_ascii=False, indent=2)

            data['cart'] = []
            data['current_category'] = None
            data['wait_for_address'] = False
            data['wait_for_contact'] = False
            data['wait_for_comment'] = False
            data.pop('comment', None)
            data.pop('address', None)
            data.pop('contact', None)
            return

    # ——— Кнопка «⬅️ Назад» в любое время (если не в режиме адрес/контакт/комментарий) ———
    if text == "⬅️ Назад":
        data['current_category'] = None
        data['wait_for_address'] = False
        data['wait_for_contact'] = False
        data['wait_for_comment'] = False
        bot.send_message(chat_id, t(chat_id, "choose_category"), reply_markup=get_inline_categories(chat_id))
        return

    # ——— Очистка корзины ———
    if text == "🗑️ Очистить корзину":
        data['cart'].clear()
        data['current_category'] = None
        data['wait_for_address'] = False
        data['wait_for_contact'] = False
        data['wait_for_comment'] = False
        bot.send_message(chat_id, "Корзина очищена.", reply_markup=get_inline_categories(chat_id))
        return

    # ——— Добавить ещё ———
    if text == "➕ Добавить ещё":
        data['current_category'] = None
        bot.send_message(chat_id, t(chat_id, "choose_category"), reply_markup=get_inline_categories(chat_id))
        return

    # ——— Завершить заказ ———
    if text == "✅ Завершить заказ":
        if not data['cart']:
            bot.send_message(chat_id, t(chat_id, "cart_empty"))
            return
        total_try = sum(i['price'] for i in data['cart'])
        summary = "\n".join(f"{i['category']}: {i['flavor']} — {i['price']}₺" for i in data['cart'])
        kb = address_keyboard()
        bot.send_message(
            chat_id,
            f"🛒 {t(chat_id, 'view_cart')}:\n\n{summary}\n\nИтог: {total_try}₺\n\n{t(chat_id, 'enter_address')}",
            reply_markup=kb
        )
        data['wait_for_address'] = True
        return

    # ——— Выбор категории ———
    if text in menu:
        data['current_category'] = text
        bot.send_message(chat_id, f"{t(chat_id, 'choose_flavor')} «{text}»", reply_markup=get_inline_flavors(chat_id, text))
        return

    # ——— Выбор вкуса → добавляем в корзину ———
    cat = data.get('current_category')
    if cat:
        price = menu[cat]["price"]
        for it in menu[cat]["flavors"]:
            emoji = it.get("emoji", "").strip()
            flavor = it["flavor"]
            stock = it.get("stock", 0)
            label = f"{emoji} {flavor} ({price}₺) [{stock} шт]" if emoji else f"{flavor} ({price}₺) [{stock} шт]"
            if text == label and stock > 0:
                data['cart'].append({
                    'category': cat,
                    'flavor': flavor,
                    'price': price
                })
                count = len(data['cart'])
                kb = types.InlineKeyboardMarkup(row_width=2)
                kb.add(
                    types.InlineKeyboardButton(text=f"➕ {t(chat_id,'add_more')}", callback_data=f"go_back_to_category|{cat}"),
                    types.InlineKeyboardButton(text=f"🛒 {t(chat_id,'view_cart')}", callback_data="view_cart")
                )
                kb.add(types.InlineKeyboardButton(text=f"✅ {t(chat_id,'finish_order')}", callback_data="finish_order"))
                bot.send_message(
                    chat_id,
                    f"{cat} — {flavor} ({price}₺) {t(chat_id,'added_to_cart').format(flavor=flavor, count=count)}",
                    reply_markup=kb
                )
                return

        bot.send_message(chat_id, t(chat_id, "error_invalid"), reply_markup=get_inline_flavors(chat_id, cat))
        return

# —————————————————————————————————————————————————————————————
#   13. Callback для inline: выбор категории
# —————————————————————————————————————————————————————————————
@bot.callback_query_handler(func=lambda call: call.data and call.data.startswith("category|"))
def handle_category(call):
    _, cat = call.data.split("|", 1)
    chat_id = call.from_user.id
    if cat not in menu:
        bot.answer_callback_query(call.id, t(chat_id, "error_invalid"))
        return
    bot.answer_callback_query(call.id)
    user_data[chat_id]["current_category"] = cat
    bot.send_message(chat_id, f"{t(chat_id, 'choose_flavor')} «{cat}»", reply_markup=get_inline_flavors(chat_id, cat))

# —————————————————————————————————————————————————————————————
#   14. Callback для inline: назад к категориям
# —————————————————————————————————————————————————————————————
@bot.callback_query_handler(func=lambda call: call.data == "go_back_to_categories")
def handle_go_back_to_categories(call):
    chat_id = call.from_user.id
    bot.answer_callback_query(call.id)
    bot.send_message(chat_id, t(chat_id, "choose_category"), reply_markup=get_inline_categories(chat_id))

# —————————————————————————————————————————————————————————————
#   15. Callback для inline: выбор вкуса
# —————————————————————————————————————————————————————————————
@bot.callback_query_handler(func=lambda call: call.data and call.data.startswith("flavor|"))
def handle_flavor(call):
    _, cat, flavor = call.data.split("|", 2)
    chat_id = call.from_user.id
    item_obj = next((i for i in menu[cat]["flavors"] if i["flavor"] == flavor), None)
    if not item_obj:
        bot.answer_callback_query(call.id, t(chat_id, "error_invalid"))
        return
    bot.answer_callback_query(call.id)
    user_lang = user_data.get(chat_id, {}).get("lang", "ru")
    description = item_obj.get(f"description_{user_lang}", "")
    price = menu[cat]["price"]
    photo_url = item_obj.get("photo_url")

    if photo_url:
        bot.send_photo(chat_id, photo_url, caption=f"<b>{flavor}</b>\n\n{description}\n\n📌 {price}₺")

    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        types.InlineKeyboardButton(text=f"➕ {t(chat_id,'add_to_cart')}", callback_data=f"add_to_cart|{cat}|{flavor}"),
        types.InlineKeyboardButton(text=f"⬅️ {t(chat_id,'back_to_categories')}", callback_data=f"category|{cat}")
    )
    bot.send_message(chat_id, t(chat_id, "choose_action"), reply_markup=kb)

# —————————————————————————————————————————————————————————————
#   16. Callback для inline: добавить в корзину
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
    kb = get_inline_after_add(chat_id, cat)
    bot.send_message(chat_id, t(chat_id, "added_to_cart").format(flavor=flavor, count=count), reply_markup=kb)

# —————————————————————————————————————————————————————————————
#   17. Callback для inline: «➕ Добавить ещё»
# —————————————————————————————————————————————————————————————
@bot.callback_query_handler(func=lambda call: call.data and call.data.startswith("go_back_to_category|"))
def handle_go_back_to_category(call):
    chat_id = call.from_user.id
    _, cat = call.data.split("|", 1)
    bot.answer_callback_query(call.id)
    bot.send_message(chat_id, f"{t(chat_id, 'choose_flavor')} «{cat}»", reply_markup=get_inline_flavors(chat_id, cat))

# —————————————————————————————————————————————————————————————
#   18. Callback для inline: «🛒 Посмотреть корзину»
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
#   19. Callback для inline: «❌ Удалить i»
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

# —————————————————————————————————————————————————————————————
#   20. Callback для inline: «✏️ Изменить i»
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
    key_to_edit, old_qty = items_list[idx]
    cat, flavor, price = key_to_edit
    bot.answer_callback_query(call.id)
    data["edit_cart_phase"] = "enter_qty"
    data["edit_index"] = idx
    bot.send_message(chat_id, f"Текущий товар: {cat} — {flavor} — {price}₺ (в корзине {old_qty} шт).\n{text_for_locale(chat_id, 'enter_new_qty')}", reply_markup=types.ReplyKeyboardRemove())

# —————————————————————————————————————————————————————————————
#   21. Обработка ввода нового количества (text)
# —————————————————————————————————————————————————————————————
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
    # Удаляем все текущие
    new_cart = [it for it in cart if not (it["category"] == cat and it["flavor"] == flavor and it["price"] == price)]
    for _ in range(new_qty):
        new_cart.append({"category": cat, "flavor": flavor, "price": price})
    data["cart"] = new_cart
    data["edit_cart_phase"] = None
    data.pop("edit_index", None)
    if new_qty == 0:
        bot.send_message(chat_id, t(chat_id, "item_removed").format(flavor=flavor), reply_markup=get_inline_categories(chat_id))
    else:
        bot.send_message(chat_id, t(chat_id, "qty_changed").format(flavor=flavor, qty=new_qty), reply_markup=get_inline_categories(chat_id))

# —————————————————————————————————————————————————————————————
#   22. Callback для inline: «✅ Завершить заказ»
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
    summary_lines = [f"{item['category']}: {item['flavor']} — {item['price']}₺" for item in cart]
    summary = "\n".join(summary_lines)
    kb = address_keyboard()
    bot.send_message(
        chat_id,
        f"🛒 {t(chat_id, 'view_cart')}:\n\n{summary}\n\n📌 Итого: {total_try}₺\n\n{t(chat_id, 'enter_address')}",
        reply_markup=kb
    )
    data["wait_for_address"] = True

# —————————————————————————————————————————————————————————————
#   23. Callback для inline: «⬅️ Назад к вкусам»
# —————————————————————————————————————————————————————————————
@bot.callback_query_handler(func=lambda call: call.data and call.data.startswith("go_back_to_category|"))
def handle_go_back_to_category_inline(call):
    chat_id = call.from_user.id
    _, cat = call.data.split("|", 1)
    bot.answer_callback_query(call.id)
    bot.send_message(chat_id, f"{t(chat_id, 'choose_flavor')} «{cat}»", reply_markup=get_inline_flavors(chat_id, cat))

# —————————————————————————————————————————————————————————————
#   24. Хендлер ввода адреса (завершение по address_callback)
# —————————————————————————————————————————————————————————————
@bot.message_handler(content_types=['text','location','venue'])
def handle_address_input(message):
    chat_id = message.chat.id
    data = user_data.get(chat_id, {})
    if not data.get("wait_for_address"):
        return
    text = message.text or ""
    if text == "⬅️ Назад":
        data["wait_for_address"] = False
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
    data["address"] = address
    data["wait_for_address"] = False
    data["wait_for_contact"] = True
    kb = contact_keyboard()
    bot.send_message(chat_id, t(chat_id, "enter_contact"), reply_markup=kb)

# —————————————————————————————————————————————————————————————
#   25. Хендлер ввода контакта
# —————————————————————————————————————————————————————————————
@bot.message_handler(content_types=['text','contact'])
def handle_contact_input(message):
    chat_id = message.chat.id
    data = user_data.get(chat_id, {})
    if not data.get("wait_for_contact"):
        return
    text = message.text or ""
    if text == "⬅️ Назад":
        data["wait_for_address"] = True
        data["wait_for_contact"] = False
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
    data["contact"] = contact
    data["wait_for_contact"] = False
    data["wait_for_comment"] = True
    kb = comment_keyboard()
    bot.send_message(chat_id, t(chat_id, "enter_comment"), reply_markup=kb)

# —————————————————————————————————————————————————————————————
#   26. Хендлер комментария и отправка заказа
# —————————————————————————————————————————————————————————————
@bot.message_handler(content_types=['text'])
def handle_comment_and_send(message):
    chat_id = message.chat.id
    data = user_data.get(chat_id, {})
    if not data.get("wait_for_comment"):
        return
    text = message.text or ""
    if text == "⬅️ Назад":
        data["wait_for_contact"] = True
        data["wait_for_comment"] = False
        bot.send_message(chat_id, t(chat_id, "enter_contact"), reply_markup=contact_keyboard())
        return
    if text == "✏️ Комментарий к заказу":
        bot.send_message(chat_id, t(chat_id, "enter_comment"), reply_markup=types.ReplyKeyboardRemove())
        return
    if text == "📤 Отправить заказ":
        cart = data.get("cart", [])
        if not cart:
            bot.send_message(chat_id, t(chat_id, "cart_empty"))
            return
        total_try = sum(i["price"] for i in cart)
        summary_rus = "\n".join(f"{i['category']}: {i['flavor']} — {i['price']}₺" for i in cart)
        summary_en = "\n".join(f"{i['category']}: {i['flavor']} — {i['price']}₺" for i in cart)
        rates = fetch_rates()
        rub = round(total_try * rates.get("RUB", 0) + 500, 2)
        usd = round(total_try * rates.get("USD", 0) + 2, 2)
        uah = round(total_try * rates.get("UAH", 0) + 200, 2)
        conv = f"({rub}₽, ${usd}, ₴{uah})"
        full_rus = (
            f"📥 Новый заказ от @{message.from_user.username or message.from_user.first_name}:\n\n"
            f"{summary_rus}\n\n"
            f"Итог: {total_try}₺ {conv}\n"
            f"📍 Адрес: {data.get('address', '—')}\n"
            f"📱 Контакт: {data.get('contact', '—')}\n"
            f"💬 Комментарий: {data.get('comment', '—')}"
        )
        bot.send_message(PERSONAL_CHAT_ID, full_rus)
        comment_ru = data.get("comment", "")
        comment_en = translate_to_en(comment_ru) if comment_ru else "—"
        full_en = (
            f"📥 New order from @{message.from_user.username or message.from_user.first_name}:\n\n"
            f"{summary_en}\n\n"
            f"Total: {total_try}₺ {conv}\n"
            f"📍 Address: {data.get('address', '—')}\n"
            f"📱 Contact: {data.get('contact', '—')}\n"
            f"💬 Comment: {comment_en}"
        )
        bot.send_message(GROUP_CHAT_ID, full_en)
        bot.send_message(chat_id, t(chat_id, "order_accepted"), reply_markup=types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True).add("🛒 Оформить новый заказ"))
        for o in cart:
            cat = o["category"]
            for itm in menu[cat]["flavors"]:
                if itm["flavor"] == o["flavor"]:
                    itm["stock"] = max(itm.get("stock", 1) - 1, 0)
                    break
        with open(MENU_PATH, "w", encoding="utf-8") as f:
            json.dump(menu, f, ensure_ascii=False, indent=2)
        data["cart"] = []
        data["current_category"] = None
        data["wait_for_address"] = False
        data["wait_for_contact"] = False
        data["wait_for_comment"] = False
        data.pop("comment", None)
        data.pop("address", None)
        data.pop("contact", None)
        return
    if data.get("wait_for_comment") and message.content_type == "text" and text not in ["📤 Отправить заказ", "✏️ Комментарий к заказу"]:
        data["comment"] = text.strip()
        bot.send_message(chat_id, t(chat_id, "comment_saved"), reply_markup=types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True).add("📤 Отправить заказ"))
        return

# —————————————————————————————————————————————————————————————
#   27. Запуск бота
# —————————————————————————————————————————————————————————————
if __name__ == "__main__":
    bot.remove_webhook()
    bot.polling(none_stop=True)
