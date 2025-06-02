# -*- coding: utf-8 -*-
import os
import json
import requests
import telebot
from telebot import types

# —————————————————————————————————————————————————————————————
#   1. ЧТЕНИЕ ОКРУЖЕНИЯ
# —————————————————————————————————————————————————————————————
TOKEN = os.getenv("TOKEN")
if not TOKEN:
    raise RuntimeError("Переменная окружения TOKEN не задана! Запустите бот с -e TOKEN=<ваш_токен>.")

# ID чатов для отправки заказов
GROUP_CHAT_ID = int(os.getenv("GROUP_CHAT_ID", "0"))      # например: -1001234567890
PERSONAL_CHAT_ID = int(os.getenv("PERSONAL_CHAT_ID", "0"))  # например: 123456789

bot = telebot.TeleBot(TOKEN, parse_mode="HTML")

# Пути к JSON-файлам
MENU_PATH = "menu.json"
LANG_PATH = "languages.json"

# —————————————————————————————————————————————————————————————
#   2. ЗАГРУЗКА JSON-ФАЙЛОВ (меню и переводы)
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
#   3. ХРАНЕНИЕ ДАННЫХ ПОЛЬЗОВАТЕЛЕЙ В ПАМЯТИ
#       (Для простоты — словарь; при перезапуске бот забудет всё)
# —————————————————————————————————————————————————————————————
user_data = {}
# Структура user_data[chat_id]:
# {
#   "lang": "ru" или "en",
#   "cart": [ { "category": str, "flavor": str, "price": int }, ... ],
#   "edit_phase": None или "enter_new_qty",
#   "edit_idx": int,
#   "wait_for_address": bool,
#   "wait_for_contact": bool,
#   "wait_for_comment": bool,
#   "address": str,
#   "contact": str,
#   "comment": str
# }

# —————————————————————————————————————————————————————————————
#   4. ФУНКЦИЯ ДЛЯ ПОЛУЧЕНИЯ ПЕРЕВОДА
# —————————————————————————————————————————————————————————————
def t(chat_id: int, key: str) -> str:
    """
    Возвращает перевод строки по ключу `key` для пользователя chat_id.
    Если язык или ключ не найдены, возвращает сам ключ.
    """
    lang = user_data.get(chat_id, {}).get("lang", "ru")
    return translations.get(lang, {}).get(key, key)

# —————————————————————————————————————————————————————————————
#   5. КНОПКИ ДЛЯ INLINE-ВЫБОРА
# —————————————————————————————————————————————————————————————
def get_inline_categories(chat_id: int) -> types.InlineKeyboardMarkup:
    """
    Inline-кнопки: список категорий (menu.keys()).
    callback_data: "category|<имя_категории>"
    """
    kb = types.InlineKeyboardMarkup(row_width=2)
    for cat in menu.keys():
        kb.add(types.InlineKeyboardButton(text=cat, callback_data=f"category|{cat}"))
    return kb

def get_inline_flavors(chat_id: int, cat: str) -> types.InlineKeyboardMarkup:
    """
    Inline-кнопки внутри одной категории: список вкусов, где stock > 0.
    callback_data: "flavor|<cat>|<flavor>"
    """
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
    # Кнопка «Назад к категориям»
    kb.add(types.InlineKeyboardButton(text=f"⬅️ {t(chat_id, 'back_to_categories')}", callback_data="go_back_to_categories"))
    return kb

def get_inline_after_add(chat_id: int, cat: str) -> types.InlineKeyboardMarkup:
    """
    Inline-кнопки после того, как пользователь добавил товар:
      [➕ Добавить ещё] [🛒 Посмотреть корзину]
      [✅ Завершить заказ]
    """
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        types.InlineKeyboardButton(text=f"➕ {t(chat_id,'add_more')}", callback_data=f"go_back_to_category|{cat}"),
        types.InlineKeyboardButton(text=f"🛒 {t(chat_id,'view_cart')}", callback_data="view_cart")
    )
    kb.add(types.InlineKeyboardButton(text=f"✅ {t(chat_id,'finish_order')}", callback_data="finish_order"))
    return kb

def get_inline_cart(chat_id: int) -> types.InlineKeyboardMarkup:
    """
    Inline-кнопки в корзине: для каждого сгруппированного товара [❌ Удалить i] [✏️ Изменить i],
    плюс «⬅️ Назад к категориям» и «✅ Завершить заказ».
    """
    kb = types.InlineKeyboardMarkup(row_width=2)
    data = user_data.get(chat_id, {})
    cart = data.get("cart", [])
    if not cart:
        return None

    # Группируем по (category, flavor, price)
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
#   6. ХЕНДЛЕР /start → ВЫБОР ЯЗЫКА
# —————————————————————————————————————————————————————————————
@bot.message_handler(commands=['start'])
def cmd_start(message):
    chat_id = message.chat.id
    # Сразу предложим выбрать язык (Inline-кнопки)
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        types.InlineKeyboardButton(text="Русский 🇷🇺", callback_data="set_lang|ru"),
        types.InlineKeyboardButton(text="English 🇬🇧", callback_data="set_lang|en")
    )
    bot.send_message(chat_id, t(chat_id, "choose_language"), reply_markup=kb)

@bot.callback_query_handler(func=lambda call: call.data and call.data.startswith("set_lang|"))
def handle_set_lang(call):
    chat_id = call.from_user.id
    _, lang_code = call.data.split("|", 1)
    data = user_data.setdefault(chat_id, {})
    data["lang"] = lang_code
    # Инициализируем корзину, если ещё не было
    data.setdefault("cart", [])
    data["edit_phase"] = None
    data["edit_idx"] = None
    data["wait_for_address"] = False
    data["wait_for_contact"] = False
    data["wait_for_comment"] = False

    bot.answer_callback_query(call.id, t(chat_id, "lang_set"))
    # После выбора языка выводим категории
    bot.send_message(chat_id, t(chat_id, "welcome"), reply_markup=get_inline_categories(chat_id))

# —————————————————————————————————————————————————————————————
#   7. ОБРАБОТКА ВЫБОРА КАТЕГОРИИ (INLINE)
# —————————————————————————————————————————————————————————————
@bot.callback_query_handler(func=lambda call: call.data and call.data.startswith("category|"))
def handle_category(call):
    chat_id = call.from_user.id
    _, cat = call.data.split("|", 1)

    if cat not in menu:
        bot.answer_callback_query(call.id, "Категория не найдена.")
        return

    bot.answer_callback_query(call.id)
    # Сохраняем текущую категорию (не обязательно, но может пригодиться)
    user_data[chat_id]["current_category"] = cat

    # Отправляем inline-кнопки для вкусов
    bot.send_message(chat_id, f"{t(chat_id, 'choose_flavor')} «{cat}»", reply_markup=get_inline_flavors(chat_id, cat))

# —————————————————————————————————————————————————————————————
#   8. ОБРАБОТКА ВЫБОРА «⬅️ Назад к категориям»
# —————————————————————————————————————————————————————————————
@bot.callback_query_handler(func=lambda call: call.data == "go_back_to_categories")
def handle_go_back_to_categories(call):
    chat_id = call.from_user.id
    bot.answer_callback_query(call.id)
    bot.send_message(chat_id, t(chat_id, "choose_category"), reply_markup=get_inline_categories(chat_id))

# —————————————————————————————————————————————————————————————
#   9. ОБРАБОТКА ВЫБОРА ВКУСА (INLINE)
# —————————————————————————————————————————————————————————————
@bot.callback_query_handler(func=lambda call: call.data and call.data.startswith("flavor|"))
def handle_flavor(call):
    chat_id = call.from_user.id
    _, cat, flavor = call.data.split("|", 2)

    # Находим объект вкуса в menu.json
    item_obj = next((i for i in menu[cat]["flavors"] if i["flavor"] == flavor), None)
    if not item_obj:
        bot.answer_callback_query(call.id, t(chat_id, "error_invalid"))
        return

    bot.answer_callback_query(call.id)
    user_lang = user_data.get(chat_id, {}).get("lang", "ru")
    description = item_obj.get(f"description_{user_lang}", "")
    price = menu[cat]["price"]
    photo_url = item_obj.get("photo_url")

    # Сначала отправляем изображение (если photo_url указано)
    if photo_url:
        bot.send_photo(chat_id, photo_url, caption=f"<b>{flavor}</b>\n\n{description}\n\n📌 {price}₺")

    # Кнопки «Добавить в корзину» и «Назад к категориям»
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        types.InlineKeyboardButton(text=f"➕ {t(chat_id,'add_to_cart')}", callback_data=f"add_to_cart|{cat}|{flavor}"),
        types.InlineKeyboardButton(text=f"⬅️ {t(chat_id,'back_to_categories')}", callback_data=f"category|{cat}")
    )
    bot.send_message(chat_id, t(chat_id, "choose_action"), reply_markup=kb)

# —————————————————————————————————————————————————————————————
#  10. ОБРАБОТКА «➕ Добавить в корзину» (INLINE)
# —————————————————————————————————————————————————————————————
@bot.callback_query_handler(func=lambda call: call.data and call.data.startswith("add_to_cart|"))
def handle_add_to_cart(call):
    chat_id = call.from_user.id
    _, cat, flavor = call.data.split("|", 2)

    bot.answer_callback_query(call.id, t(chat_id, "added_to_cart").format(flavor=flavor, count="..."))
    data = user_data.setdefault(chat_id, {})
    cart = data.setdefault("cart", [])
    price = menu[cat]["price"]

    # Добавляем один экземпляр
    cart.append({"category": cat, "flavor": flavor, "price": price})
    count = len(cart)

    # Кнопки «➕ Добавить ещё», «🛒 Посмотреть корзину», «✅ Завершить заказ»
    kb = get_inline_after_add(chat_id, cat)
    # Обновим сообщение с правильным числом в корзине
    bot.send_message(chat_id, t(chat_id, "added_to_cart").format(flavor=flavor, count=count), reply_markup=kb)

# —————————————————————————————————————————————————————————————
#  11. ОБРАБОТКА «➕ Добавить ещё» (INLINE)
# —————————————————————————————————————————————————————————————
@bot.callback_query_handler(func=lambda call: call.data and call.data.startswith("go_back_to_category|"))
def handle_go_back_to_category(call):
    chat_id = call.from_user.id
    _, cat = call.data.split("|", 1)
    bot.answer_callback_query(call.id)
    bot.send_message(chat_id, f"{t(chat_id, 'choose_flavor')} «{cat}»", reply_markup=get_inline_flavors(chat_id, cat))

# —————————————————————————————————————————————————————————————
#  12. ОБРАБОТКА «🛒 Посмотреть корзину» (INLINE)
# —————————————————————————————————————————————————————————————
@bot.callback_query_handler(func=lambda call: call.data == "view_cart")
def handle_view_cart(call):
    chat_id = call.from_user.id
    bot.answer_callback_query(call.id)

    data = user_data.get(chat_id, {})
    cart = data.get("cart", [])
    if not cart:
        # Если корзина пуста, вернуть к категориям
        bot.send_message(chat_id, t(chat_id, "cart_empty"), reply_markup=get_inline_categories(chat_id))
        return

    # Сгруппировка по (category, flavor, price)
    grouped = {}
    for item in cart:
        key = (item["category"], item["flavor"], item["price"])
        grouped[key] = grouped.get(key, 0) + 1

    text_lines = ["🛒 " + t(chat_id, "view_cart") + ":"]
    for idx, ((cat, flavor, price), qty) in enumerate(grouped.items(), start=1):
        text_lines.append(f"{idx}. {cat} — {flavor} — {price}₺ x {qty}")
    msg = "\n".join(text_lines)

    kb = get_inline_cart(chat_id)
    bot.send_message(chat_id, msg, reply_markup=kb)

# —————————————————————————————————————————————————————————————
#  13. ОБРАБОТКА «❌ Удалить i» (INLINE)
# —————————————————————————————————————————————————————————————
@bot.callback_query_handler(func=lambda call: call.data and call.data.startswith("remove_item|"))
def handle_remove_item(call):
    chat_id = call.from_user.id
    _, idx_str = call.data.split("|", 1)
    idx = int(idx_str) - 1

    data = user_data.get(chat_id, {})
    cart = data.get("cart", [])

    # Сгруппируем, чтобы найти нужный ключ
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
    # Удаляем все вхождения
    new_cart = [it for it in cart if not (it["category"] == cat and it["flavor"] == flavor and it["price"] == price)]
    data["cart"] = new_cart

    bot.answer_callback_query(call.id, t(chat_id, "item_removed").format(flavor=flavor))
    # Обновляем отображение корзины
    handle_view_cart(call)

# —————————————————————————————————————————————————————————————
#  14. ОБРАБОТКА «✏️ Изменить i» (INLINE → запрос количества)
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
    data["edit_phase"] = "enter_new_qty"
    data["edit_idx"] = idx

    # Просим пользователя ввести новое количество
    bot.send_message(chat_id, t(chat_id, "enter_new_qty").format(flavor=flavor, qty=old_qty))

# —————————————————————————————————————————————————————————————
#  15. ОБРАБОТКА ВВОДА НОВОГО КОЛИЧЕСТВА (text → INLINE)
# —————————————————————————————————————————————————————————————
@bot.message_handler(func=lambda m: user_data.get(m.chat.id, {}).get("edit_phase") == "enter_new_qty", content_types=['text'])
def handle_enter_new_qty(message):
    chat_id = message.chat.id
    data = user_data.get(chat_id, {})
    text = message.text.strip()

    if not text.isdigit():
        bot.send_message(chat_id, t(chat_id, "error_invalid"))
        data["edit_phase"] = None
        data.pop("edit_idx", None)
        return

    new_qty = int(text)
    idx = data.get("edit_idx")

    cart = data.get("cart", [])
    grouped = {}
    for item in cart:
        key = (item["category"], item["flavor"], item["price"])
        grouped[key] = grouped.get(key, 0) + 1
    items_list = list(grouped.items())

    if idx < 0 or idx >= len(items_list):
        bot.send_message(chat_id, t(chat_id, "error_invalid"))
        data["edit_phase"] = None
        data.pop("edit_idx", None)
        return

    key_to_edit, old_qty = items_list[idx]
    cat, flavor, price = key_to_edit

    # Удаляем все текущие вхождения
    new_cart = [it for it in cart if not (it["category"] == cat and it["flavor"] == flavor and it["price"] == price)]
    # Добавляем заданное количество, если new_qty > 0
    for _ in range(new_qty):
        new_cart.append({"category": cat, "flavor": flavor, "price": price})
    data["cart"] = new_cart

    # Сброс режима редактирования
    data["edit_phase"] = None
    data.pop("edit_idx", None)

    if new_qty == 0:
        bot.send_message(chat_id, t(chat_id, "item_removed").format(flavor=flavor))
    else:
        bot.send_message(chat_id, t(chat_id, "qty_changed").format(flavor=flavor, qty=new_qty))

    # Вернуть пользователя в меню категорий
    bot.send_message(chat_id, t(chat_id, "choose_category"), reply_markup=get_inline_categories(chat_id))

# —————————————————————————————————————————————————————————————
#  16. ОБРАБОТКА «✅ Завершить заказ» (INLINE → запрос адреса)
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

    # Считаем сумму
    total_try = sum(item["price"] for item in cart)
    summary_lines = [f"{item['category']}: {item['flavor']} — {item['price']}₺" for item in cart]
    summary = "\n".join(summary_lines)

    # Просим указать адрес (Reply-клавиатура)
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
    kb.add(t(chat_id, "share_location"))
    kb.add(t(chat_id, "choose_on_map"))
    kb.add(t(chat_id, "enter_address_text"))
    kb.add(t(chat_id, "back"))
    msg = f"🛒 {t(chat_id, 'view_cart')}:\n\n{summary}\n\n📌 Итого: {total_try}₺\n\n{t(chat_id, 'enter_address')}"
    bot.send_message(chat_id, msg, reply_markup=kb)

    data["wait_for_address"] = True

# —————————————————————————————————————————————————————————————
#  17. ОБРАБОТКА ВВОДА АДРЕСА (location, venue, текст)
# —————————————————————————————————————————————————————————————
@bot.message_handler(content_types=['text', 'location', 'venue'])
def handle_address_input(message):
    chat_id = message.chat.id
    data = user_data.get(chat_id, {})

    # Если мы не в режиме ожидания адреса — игнорируем
    if not data.get("wait_for_address"):
        return

    text = message.text or ""
    # Кнопка «⬅️ Назад» возвращает в меню категорий
    if text == t(chat_id, "back"):
        data["wait_for_address"] = False
        bot.send_message(chat_id, t(chat_id, "choose_category"), reply_markup=get_inline_categories(chat_id))
        return

    # Если это venue (отметка на карте)
    if message.content_type == 'venue' and message.venue:
        v = message.venue
        address = f"{v.title}, {v.address}\n🌍 https://maps.google.com/?q={v.location.latitude},{v.location.longitude}"
    # Если это просто координаты
    elif message.content_type == 'location' and message.location:
        lat, lon = message.location.latitude, message.location.longitude
        address = f"🌍 https://maps.google.com/?q={lat},{lon}"
    # Если выбрали «🗺️ Выбрать точку на карте», покажем инструкцию
    elif text == t(chat_id, "choose_on_map"):
        bot.send_message(chat_id,
            "Чтобы выбрать точку:\n"
            "📎 → Местоположение → «Выбрать на карте» → метка → Отправить",
            reply_markup=types.ReplyKeyboardRemove()
        )
        return
    # Если нажали «✏️ Ввести адрес» — попросим текстовый ввод
    elif text == t(chat_id, "enter_address_text"):
        bot.send_message(chat_id, t(chat_id, "enter_address"), reply_markup=types.ReplyKeyboardRemove())
        return
    # Если просто пришёл текст (адрес)
    elif message.content_type == 'text' and message.text:
        address = message.text.strip()
    else:
        bot.send_message(chat_id, t(chat_id, "error_invalid"), reply_markup=types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True).add(t(chat_id, "share_location"), t(chat_id, "choose_on_map"), t(chat_id, "enter_address_text"), t(chat_id, "back")))
        return

    data["address"] = address
    data["wait_for_address"] = False
    data["wait_for_contact"] = True

    # Попросим контакт (Reply-клавиатура)
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
    kb.add(t(chat_id, "share_contact"))
    kb.add(t(chat_id, "enter_nickname"))
    kb.add(t(chat_id, "back"))
    bot.send_message(chat_id, t(chat_id, "enter_contact"), reply_markup=kb)

# —————————————————————————————————————————————————————————————
#  18. ОБРАБОТКА ВВОДА КОНТАКТА (contact или ник)
# —————————————————————————————————————————————————————————————
@bot.message_handler(content_types=['text', 'contact'])
def handle_contact_input(message):
    chat_id = message.chat.id
    data = user_data.get(chat_id, {})

    if not data.get("wait_for_contact"):
        return

    text = message.text or ""

    # «⬅️ Назад» → вернуться к вводу адреса
    if text == t(chat_id, "back"):
        data["wait_for_address"] = True
        data["wait_for_contact"] = False
        kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
        kb.add(t(chat_id, "share_location"))
        kb.add(t(chat_id, "choose_on_map"))
        kb.add(t(chat_id, "enter_address_text"))
        kb.add(t(chat_id, "back"))
        bot.send_message(chat_id, t(chat_id, "enter_address"), reply_markup=kb)
        return

    if message.content_type == 'contact' and message.contact:
        contact = message.contact.phone_number
    elif text == t(chat_id, "enter_nickname"):
        bot.send_message(chat_id, "Введите ваш Telegram-ник (без @):", reply_markup=types.ReplyKeyboardRemove())
        return
    elif message.content_type == 'text' and message.text:
        contact = "@" + message.text.strip().lstrip("@")
    else:
        kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
        kb.add(t(chat_id, "share_contact"))
        kb.add(t(chat_id, "enter_nickname"))
        kb.add(t(chat_id, "back"))
        bot.send_message(chat_id, t(chat_id, "enter_contact"), reply_markup=kb)
        return

    data["contact"] = contact
    data["wait_for_contact"] = False
    data["wait_for_comment"] = True

    # Попросим комментарий (Reply-клавиатура)
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
    kb.add(t(chat_id, "enter_comment"))
    kb.add(t(chat_id, "send_order"))
    kb.add(t(chat_id, "back"))
    bot.send_message(chat_id, t(chat_id, "enter_comment"), reply_markup=kb)

# —————————————————————————————————————————————————————————————
#  19. ОБРАБОТКА ВВОДА КОММЕНТАРИЯ И ОТПРАВКА ЗАКАЗА
# —————————————————————————————————————————————————————————————
@bot.message_handler(content_types=['text'])
def handle_comment_and_send(message):
    chat_id = message.chat.id
    data = user_data.get(chat_id, {})

    # Если не в режиме ожидания комментария — выходим
    if not data.get("wait_for_comment"):
        return

    text = message.text or ""

    # «⬅️ Назад» → вернём к вводу контакта
    if text == t(chat_id, "back"):
        data["wait_for_contact"] = True
        data["wait_for_comment"] = False
        kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
        kb.add(t(chat_id, "share_contact"))
        kb.add(t(chat_id, "enter_nickname"))
        kb.add(t(chat_id, "back"))
        bot.send_message(chat_id, t(chat_id, "enter_contact"), reply_markup=kb)
        return

    # Если нажали «✏️ Комментарий к заказу» — просим текст. Просто ждём, пока не нажмут «📤 Отправить заказ»
    if text == t(chat_id, "enter_comment"):
        bot.send_message(chat_id, t(chat_id, "enter_comment"), reply_markup=types.ReplyKeyboardRemove())
        return

    # Если нажали «📤 Отправить заказ» — собираем всё и шлём админам
    if text == t(chat_id, "send_order"):
        cart = data.get("cart", [])
        if not cart:
            bot.send_message(chat_id, t(chat_id, "cart_empty"))
            return

        total_try = sum(i["price"] for i in cart)
        summary_ru = "\n".join(f"{i['category']}: {i['flavor']} — {i['price']}₺" for i in cart)

        # Перевод комментария (если есть) на английский (через Google Translate API)
        comment_ru = data.get("comment", "")
        comment_en = translate_to_en(comment_ru) if comment_ru else "—"

        # Считаем курсы
        rates = fetch_rates()
        rub = round(total_try * rates.get("RUB", 0) + 500, 2)
        usd = round(total_try * rates.get("USD", 0) + 2, 2)
        uah = round(total_try * rates.get("UAH", 0) + 200, 2)
        conv = f"({rub}₽, ${usd}, ₴{uah})"

        # Русскоязычная версия для личного чата администратора
        full_rus = (
            f"📥 Новый заказ от @{message.from_user.username or message.from_user.first_name}:\n\n"
            f"{summary_ru}\n\n"
            f"Итог: {total_try}₺ {conv}\n"
            f"📍 Адрес: {data.get('address', '—')}\n"
            f"📱 Контакт: {data.get('contact', '—')}\n"
            f"💬 Комментарий: {comment_ru or '—'}"
        )
        bot.send_message(PERSONAL_CHAT_ID, full_rus)

        # Англоязычная версия для группового чата
        summary_en = "\n".join(f"{i['category']}: {i['flavor']} — {i['price']}₺" for i in cart)
        full_en = (
            f"📥 New order from @{message.from_user.username or message.from_user.first_name}:\n\n"
            f"{summary_en}\n\n"
            f"Total: {total_try}₺ {conv}\n"
            f"📍 Address: {data.get('address', '—')}\n"
            f"📱 Contact: {data.get('contact', '—')}\n"
            f"💬 Comment: {comment_en}"
        )
        bot.send_message(GROUP_CHAT_ID, full_en)

        # Уведомление пользователю
        bot.send_message(chat_id, t(chat_id, "order_accepted"), reply_markup=types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True).add(t(chat_id, "view_cart")))

        # Обновляем остатки stock
        for o in cart:
            cat = o["category"]
            for itm in menu[cat]["flavors"]:
                if itm["flavor"] == o["flavor"]:
                    itm["stock"] = max(itm.get("stock", 1) - 1, 0)
                    break
        # Сохраняем обновлённое меню в файл
        with open(MENU_PATH, "w", encoding="utf-8") as f:
            json.dump(menu, f, ensure_ascii=False, indent=2)

        # Сбрасываем данные корзины
        data["cart"] = []
        data["wait_for_address"] = False
        data["wait_for_contact"] = False
        data["wait_for_comment"] = False
        data.pop("address", None)
        data.pop("contact", None)
        data.pop("comment", None)

        return

    # Если пользователь вводит сам комментарий (текст), а не нажимает кнопки
    if data.get("wait_for_comment") and message.content_type == "text" and text != t(chat_id, "send_order") and text != t(chat_id, "enter_comment"):
        data["comment"] = text.strip()
        bot.send_message(chat_id, "💬 " + t(chat_id, "comment_saved"), reply_markup=types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True).add(t(chat_id, "send_order")))
        return

# —————————————————————————————————————————————————————————————
#   20. ФУНКЦИЯ ДЛЯ ПЕРЕВОДА ЧЕРЕЗ GOOGLE TRANSLATE API
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
#   21. ФУНКЦИЯ ДЛЯ ПОЛУЧЕНИЯ КУРСОВ (RUB, USD, UAH)
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
#   22. ЗАПУСК БОТА
# —————————————————————————————————————————————————————————————
if __name__ == "__main__":
    bot.remove_webhook()
    bot.polling(none_stop=True)
