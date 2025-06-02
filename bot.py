# -*- coding: utf-8 -*-
import os
import json
import requests
import telebot
from telebot import types

# ——— Конфигурация бота ———
TOKEN = os.getenv("TOKEN")
if not TOKEN:
    raise RuntimeError("Переменная окружения TOKEN не задана! Запустите контейнер с -e TOKEN=<ваш_токен>.")
bot = telebot.TeleBot(TOKEN)

GROUP_CHAT_ID    = int(os.getenv("GROUP_CHAT_ID",    "-1002414380144"))
PERSONAL_CHAT_ID = int(os.getenv("PERSONAL_CHAT_ID", "424751188"))
MENU_PATH = "menu.json"
DEFAULT_CATEGORY_PRICE = 1300

# Реквизиты для оплаты
PAY_COD        = "💵 Оплата при получении"
PAY_TRANSFER   = "💳 Перевод гривнами или рублями"
PAY_CRYPTO     = "₿ Оплатить криптой"

# Платежные данные
TRANSFER_UAH_CARD     = "4441 1111 5771 8424 — Влад"
TRANSFER_RUB_PHONE    = "+7 996 996 12 99 — Артур, Тинькофф"
CRYPTO_ADDRESS        = "TUnMJ7oCtSDCHZiQSMrFjShkUPv18SVFDc"  # Tron (TRC20)

# ——— Загрузка/сохранение меню ———
def load_menu():
    if not os.path.exists(MENU_PATH):
        with open(MENU_PATH, "w", encoding="utf-8") as f:
            json.dump({}, f, ensure_ascii=False, indent=2)
        return {}
    try:
        with open(MENU_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except json.JSONDecodeError:
        with open(MENU_PATH, "w", encoding="utf-8") as f:
            json.dump({}, f, ensure_ascii=False, indent=2)
        return {}

def save_menu(menu_data):
    with open(MENU_PATH, "w", encoding="utf-8") as f:
        json.dump(menu_data, f, ensure_ascii=False, indent=2)

menu = load_menu()
user_data = {}

# ——— Помощник для получения курсов валют ———
def fetch_rates():
    sources = [
        ("https://api.exchangerate.host/latest", {"base":"TRY","symbols":"RUB,USD,UAH"}),
        ("https://open.er-api.com/v6/latest/TRY", {})
    ]
    for url, params in sources:
        try:
            r = requests.get(url, params=params, timeout=5)
            data = r.json()
            rates = data.get("rates") or data.get("conversion_rates")
            if rates:
                return {k: rates[k] for k in ("RUB","USD","UAH") if k in rates}
        except:
            continue
    return {"RUB":0, "USD":0, "UAH":0}

# ——— Клавиатуры ———
def get_main_keyboard():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
    for cat in menu:
        kb.add(cat)
    kb.add("🛒 Корзина")
    kb.add("📝 Описание устройств")
    kb.add("📷 Изображения устройств")
    return kb

def get_flavors_keyboard(cat):
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

def description_keyboard():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
    kb.add("⬅️ Назад")
    return kb

def address_keyboard():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
    kb.add(types.KeyboardButton("📍 Поделиться геопозицией", request_location=True))
    kb.add("🗺️ Выбрать точку на карте")
    kb.add("✏️ Ввести адрес")
    kb.add("⬅️ Назад")
    return kb

def contact_keyboard():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
    kb.add(types.KeyboardButton("📞 Поделиться контактом", request_contact=True))
    kb.add("✏️ Ввести ник")
    kb.add("⬅️ Назад")
    return kb

def comment_keyboard():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
    kb.add("✏️ Комментарий к заказу")
    kb.add("📤 Отправить заказ")
    kb.add("⬅️ Назад")
    return kb

def view_cart_keyboard():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
    kb.add("⬅️ Назад")
    return kb

def edit_action_keyboard():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
    kb.add("➕ Add Category", "➖ Remove Category")
    kb.add("💲 Fix Price", "ALL IN", "🔄 Actual Flavor")
    kb.add("⬅️ Back", "❌ Cancel")
    return kb

# ——— Команда /start ———
@bot.message_handler(commands=['start'])
def cmd_start(message):
    user_data[message.chat.id] = {
        "cart": [], "current_category": None,
        "wait_for_address": False, "wait_for_contact": False, "wait_for_comment": False,
        "pending_order": None,
        "awaiting_transfer_tx": False, "awaiting_crypto_tx": False,
        "edit_phase": None, "edit_cat": None, "edit_flavor": None,
        "edit_cart_phase": None, "edit_index": None
    }
    bot.send_message(
        message.chat.id,
        "Добро пожаловать! Выберите категорию:",
        reply_markup=get_main_keyboard()
    )

# ——— Команда /change — редактирование меню (доступна всем) ———
@bot.message_handler(commands=['change'])
def cmd_change(message):
    data = user_data.setdefault(message.chat.id, {
        "cart": [], "current_category": None,
        "wait_for_address": False, "wait_for_contact": False, "wait_for_comment": False,
        "pending_order": None,
        "awaiting_transfer_tx": False, "awaiting_crypto_tx": False,
        "edit_phase": None, "edit_cat": None, "edit_flavor": None,
        "edit_cart_phase": None, "edit_index": None
    })
    data['edit_phase'] = 'choose_action'
    bot.send_message(message.chat.id, "Menu editing: choose action", reply_markup=edit_action_keyboard())

# ——— Универсальный хендлер ———
@bot.message_handler(content_types=['text','location','venue','contact'])
def universal_handler(message):
    cid = message.chat.id
    text = message.text or ""
    data = user_data.setdefault(cid, {
        "cart": [], "current_category": None,
        "wait_for_address": False, "wait_for_contact": False, "wait_for_comment": False,
        "pending_order": None,
        "awaiting_transfer_tx": False, "awaiting_crypto_tx": False,
        "edit_phase": None, "edit_cat": None, "edit_flavor": None,
        "edit_cart_phase": None, "edit_index": None
    })

    # ——— Режим редактирования корзины (удаление/изменение) ———
    if data.get('edit_cart_phase'):
        # 1) choose_action: «Удалить N» или «Изменить N» или «⬅️ Назад»
        if data['edit_cart_phase'] == 'choose_action':
            if text == "⬅️ Назад":
                data['edit_cart_phase'] = None
                data['edit_index'] = None
                bot.send_message(cid, "Вернулись в главное меню.", reply_markup=get_main_keyboard())
                return

            if text.startswith("Удалить "):
                try:
                    idx = int(text.split()[1]) - 1
                except:
                    bot.send_message(cid, "Неверный индекс. Попробуйте снова.", reply_markup=get_main_keyboard())
                    data['edit_cart_phase'] = None
                    data['edit_index'] = None
                    return
                grouped = {}
                for item in data['cart']:
                    key = (item['category'], item['flavor'], item['price'])
                    grouped[key] = grouped.get(key, 0) + 1
                items_list = list(grouped.items())
                if idx < 0 or idx >= len(items_list):
                    bot.send_message(cid, "Индекс вне диапазона.", reply_markup=get_main_keyboard())
                    data['edit_cart_phase'] = None
                    return
                key_to_remove, _ = items_list[idx]
                cat, flavor, price = key_to_remove
                data['cart'] = [it for it in data['cart'] if not (it['category']==cat and it['flavor']==flavor and it['price']==price)]
                data['edit_cart_phase'] = None
                data['edit_index'] = None
                bot.send_message(cid, f"Удалено все позиции «{flavor}».", reply_markup=get_main_keyboard())
                return

            if text.startswith("Изменить "):
                try:
                    idx = int(text.split()[1]) - 1
                except:
                    bot.send_message(cid, "Неверный индекс. Попробуйте снова.", reply_markup=get_main_keyboard())
                    data['edit_cart_phase'] = None
                    return
                grouped = {}
                for item in data['cart']:
                    key = (item['category'], item['flavor'], item['price'])
                    grouped[key] = grouped.get(key, 0) + 1
                items_list = list(grouped.items())
                if idx < 0 or idx >= len(items_list):
                    bot.send_message(cid, "Индекс вне диапазона.", reply_markup=get_main_keyboard())
                    data['edit_cart_phase'] = None
                    return
                data['edit_index'] = idx
                data['edit_cart_phase'] = 'enter_qty'
                key_chosen, count = items_list[idx]
                cat, flavor, price = key_chosen
                bot.send_message(cid, f"Товар: {cat} — {flavor} — {price}₺ (в корзине {count} шт).\nВведите новое количество (0 чтобы удалить):")
                return

        # 2) enter_qty: ввод нового количества
        if data['edit_cart_phase'] == 'enter_qty':
            if text == "⬅️ Назад":
                data['edit_cart_phase'] = None
                data['edit_index'] = None
                bot.send_message(cid, "Редактирование отменено.", reply_markup=get_main_keyboard())
                return
            if not text.isdigit():
                bot.send_message(cid, "Введите число, пожалуйста.")
                return
            new_qty = int(text)
            grouped = {}
            for item in data['cart']:
                key = (item['category'], item['flavor'], item['price'])
                grouped[key] = grouped.get(key, 0) + 1
            items_list = list(grouped.items())
            idx = data['edit_index']
            if idx < 0 or idx >= len(items_list):
                bot.send_message(cid, "Индекс вне диапазона.", reply_markup=get_main_keyboard())
                data['edit_cart_phase'] = None
                data['edit_index'] = None
                return
            key_chosen, old_count = items_list[idx]
            cat, flavor, price = key_chosen
            data['cart'] = [it for it in data['cart'] if not (it['category']==cat and it['flavor']==flavor and it['price']==price)]
            for _ in range(new_qty):
                data['cart'].append({'category': cat, 'flavor': flavor, 'price': price})
            data['edit_cart_phase'] = None
            data['edit_index'] = None
            if new_qty == 0:
                bot.send_message(cid, f"Товар «{flavor}» удалён из корзины.", reply_markup=get_main_keyboard())
            else:
                bot.send_message(cid, f"Количество «{flavor}» изменено на {new_qty}.", reply_markup=get_main_keyboard())
            return

    # ——— Обработка «Корзина» ———
    if text == "🛒 Корзина":
        cart = data['cart']
        if not cart:
            bot.send_message(cid, "Корзина пуста.", reply_markup=get_main_keyboard())
            return
        grouped = {}
        for item in cart:
            key = (item['category'], item['flavor'], item['price'])
            grouped[key] = grouped.get(key, 0) + 1
        items_list = list(grouped.items())
        msg_lines = ["Ваши товары в корзине:"]
        for idx, (key, count) in enumerate(items_list, start=1):
            cat, flavor, price = key
            msg_lines.append(f"{idx}. {cat} — {flavor} — {price}₺ x {count}")
        msg_text = "\n".join(msg_lines)
        kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
        for idx, (_key, _count) in enumerate(items_list, start=1):
            kb.add(f"Удалить {idx}", f"Изменить {idx}")
        kb.add("⬅️ Назад")
        data['edit_cart_phase'] = 'choose_action'
        bot.send_message(cid, msg_text, reply_markup=kb)
        return

    # ——— Обработка редактирования меню (/change) ———
    if data.get('edit_phase'):
        phase = data['edit_phase']
        if text == "⬅️ Back":
            data.pop('edit_cat', None)
            data.pop('edit_flavor', None)
            data['edit_phase'] = 'choose_action'
            bot.send_message(cid, "Back to editing menu:", reply_markup=edit_action_keyboard())
            return
        if text == "❌ Cancel":
            data.pop('edit_phase', None)
            data.pop('edit_cat', None)
            data.pop('edit_flavor', None)
            bot.send_message(cid, "Menu editing cancelled.", reply_markup=get_main_keyboard())
            return
        # 1) choose_action
        if phase == 'choose_action':
            if text == "➕ Add Category":
                data['edit_phase'] = 'add_category'
                kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
                kb.add("⬅️ Back")
                bot.send_message(cid, "Enter new category name:", reply_markup=kb)
            elif text == "➖ Remove Category":
                data['edit_phase'] = 'remove_category'
                kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
                for cat in menu:
                    kb.add(cat)
                kb.add("⬅️ Back")
                bot.send_message(cid, "Select category to remove:", reply_markup=kb)
            elif text == "💲 Fix Price":
                data['edit_phase'] = 'choose_fix_price_cat'
                kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
                for cat in menu:
                    kb.add(cat)
                kb.add("⬅️ Back")
                bot.send_message(cid, "Select category to fix price for:", reply_markup=kb)
            elif text == "ALL IN":
                data['edit_phase'] = 'choose_all_in_cat'
                kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
                for cat in menu:
                    kb.add(cat)
                kb.add("⬅️ Back")
                bot.send_message(cid, "Select category to replace full flavor list:", reply_markup=kb)
            elif text == "🔄 Actual Flavor":
                data['edit_phase'] = 'choose_cat_actual'
                kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
                for cat in menu:
                    kb.add(cat)
                kb.add("⬅️ Back")
                bot.send_message(cid, "Select category to update flavor stock:",	reply_markup=kb)
            else:
                bot.send_message(cid, "Choose action:", reply_markup=edit_action_keyboard())
            return
        # 2) add_category
        if phase == 'add_category':
            if text == "⬅️ Back":
                data['edit_phase'] = 'choose_action'
                bot.send_message(cid, "Back to editing menu:", reply_markup=edit_action_keyboard())
                return
            new_cat = text.strip()
            if not new_cat or new_cat in menu:
                kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
                kb.add("⬅️ Back")
                bot.send_message(cid, "Invalid or existing name. Try again:", reply_markup=kb)
                return
            menu[new_cat] = {"price": DEFAULT_CATEGORY_PRICE, "flavors": []}
            save_menu(menu)
            data.pop('edit_cat', None)
            data['edit_phase'] = 'choose_action'
            bot.send_message(
                cid,
                f"Category «{new_cat}» added with price {DEFAULT_CATEGORY_PRICE}₺.",
                reply_markup=edit_action_keyboard()
            )
            return
        # 3) remove_category
        if phase == 'remove_category':
            if text == "⬅️ Back":
                data['edit_phase'] = 'choose_action'
                bot.send_message(cid, "Back to editing menu:", reply_markup=edit_action_keyboard())
                return
            if text in menu:
                del menu[text]
                save_menu(menu)
                data.pop('edit_cat', None)
                data['edit_phase'] = 'choose_action'
                bot.send_message(cid, f"Category «{text}» removed.", reply_markup=edit_action_keyboard())
            else:
                kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
                kb.add("⬅️ Back")
                bot.send_message(cid, "Select valid category.", reply_markup=kb)
            return
        # 4) choose_fix_price_cat
        if phase == 'choose_fix_price_cat':
            if text == "⬅️ Back":
                data['edit_phase'] = 'choose_action'
                bot.send_message(cid, "Back to editing menu:", reply_markup=edit_action_keyboard())
                return
            if text in menu:
                data['edit_cat'] = text
                data['edit_phase'] = 'enter_new_price'
                kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
                kb.add("⬅️ Back")
                bot.send_message(cid, f"Enter new price in ₺ for category «{text}»:", reply_markup=kb)
            else:
                kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
                kb.add("⬅️ Back")
                bot.send_message(cid, "Choose category from the list.", reply_markup=kb)
            return
        # 5) enter_new_price
        if phase == 'enter_new_price':
            if text == "⬅️ Back":
                data.pop('edit_cat', None)
                data['edit_phase'] = 'choose_action'
                bot.send_message(cid, "Back to editing menu:", reply_markup=edit_action_keyboard())
                return
            cat = data.get('edit_cat')
            try:
                new_price = float(text.strip())
            except:
                kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
                kb.add("⬅️ Back")
                bot.send_message(cid, "Invalid price format. Enter a number, e.g. 1500:", reply_markup=kb)
                return
            menu[cat]["price"] = int(new_price)
            save_menu(menu)
            bot.send_message(cid, f"Price for category «{cat}» set to {int(new_price)}₺.", reply_markup=edit_action_keyboard())
            data.pop('edit_cat', None)
            data['edit_phase'] = 'choose_action'
            return
        # 6) choose_all_in_cat
        if phase == 'choose_all_in_cat':
            if text == "⬅️ Back":
                data['edit_phase'] = 'choose_action'
                bot.send_message(cid, "Back to editing menu:", reply_markup=edit_action_keyboard())
                return
            if text in menu:
                data['edit_cat'] = text
                current_list = [f"{itm['flavor']} - {itm['stock']}" for itm in menu[text]["flavors"]]
                joined = "\n".join(current_list) if current_list else "(empty)"
                kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
                kb.add("⬅️ Back")
                bot.send_message(
                    cid,
                    f"Current flavors in «{text}» (one per line as \"Name - qty\"):\n\n{joined}\n\n"
                    "Send the full updated list in the same format. Each line: “Name - qty”.",
                    reply_markup=kb
                )
                data['edit_phase'] = 'replace_all_in'
            else:
                kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
                kb.add("⬅️ Back")
                bot.send_message(cid, "Choose category from the list.", reply_markup=kb)
            return
        # 7) replace_all_in
        if phase == 'replace_all_in':
            if text == "⬅️ Back":
                data.pop('edit_cat', None)
                data['edit_phase'] = 'choose_action'
                bot.send_message(cid, "Back to editing menu:", reply_markup=edit_action_keyboard())
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
                new_flavors.append({"emoji": "", "flavor": name, "stock": int(qty)})
            menu[cat]["flavors"] = new_flavors
            save_menu(menu)
            bot.send_message(cid, f"Full flavor list for «{cat}» has been replaced.", reply_markup=edit_action_keyboard())
            data.pop('edit_cat', None)
            data['edit_phase'] = 'choose_action'
            return
        # 8) choose_cat_actual
        if phase == 'choose_cat_actual':
            if text == "⬅️ Back":
                data['edit_phase'] = 'choose_action'
                bot.send_message(cid, "Back to editing menu:", reply_markup=edit_action_keyboard())
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
                bot.send_message(cid, "Select flavor to update stock:",	reply_markup=kb)
            else:
                kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
                kb.add("⬅️ Back")
                bot.send_message(cid, "Choose category from the list.", reply_markup=kb)
            return
        # 9) choose_flavor_actual
        if phase == 'choose_flavor_actual':
            if text == "⬅️ Back":
                data.pop('edit_cat', None)
                data['edit_phase'] = 'choose_action'
                bot.send_message(cid, "Back to editing menu:", reply_markup=edit_action_keyboard())
                return
            cat = data.get('edit_cat')
            flavor_name = text.split(' [')[0]
            exists = any(it["flavor"] == flavor_name for it in menu.get(cat, {}).get("flavors", []))
            if exists:
                data['edit_flavor'] = flavor_name
                data['edit_phase'] = 'enter_actual_qty'
                kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
                kb.add("⬅️ Back")
                bot.send_message(cid, "Введите актуальное количество штук!", reply_markup=kb)
            else:
                bot.send_message(cid, "Flavor not found. Choose again:", reply_markup=edit_action_keyboard())
                data['edit_phase'] = 'choose_action'
            return
        # 10) enter_actual_qty
        if phase == 'enter_actual_qty':
            if text == "⬅️ Back":
                data.pop('edit_flavor', None)
                data['edit_phase'] = 'choose_action'
                bot.send_message(cid, "Back to editing menu:", reply_markup=edit_action_keyboard())
                return
            cat = data.get('edit_cat')
            flavor = data.get('edit_flavor')
            if not text.isdigit():
                kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
                kb.add("⬅️ Back")
                bot.send_message(cid, "Please enter a valid number!", reply_markup=kb)
                return
            new_stock = int(text)
            for it in menu[cat]["flavors"]:
                if it["flavor"] == flavor:
                    it["stock"] = new_stock
                    break
            save_menu(menu)
            bot.send_message(cid, f"Stock for flavor «{flavor}» in category «{cat}» set to {new_stock}.", reply_markup=edit_action_keyboard())
            data.pop('edit_cat', None)
            data.pop('edit_flavor', None)
            data['edit_phase'] = 'choose_action'
            return

        data['edit_phase'] = 'choose_action'
        bot.send_message(cid, "Back to editing menu:", reply_markup=edit_action_keyboard())
        return

    # ——— Если ожидаем ввод адреса ———
    if data.get('wait_for_address'):
        if text == "⬅️ Назад":
            data['wait_for_address'] = False
            data['current_category'] = None
            bot.send_message(cid, "Адрес не указан. Вернитесь к выбору категории:", reply_markup=get_main_keyboard())
            return
        if text == "🗺️ Выбрать точку на карте":
            bot.send_message(cid, "Чтобы выбрать точку:\n📎 → Местоположение → «Выбрать на карте» → метка → Отправить", reply_markup=types.ReplyKeyboardRemove())
            return
        if message.content_type == 'venue' and message.venue:
            v = message.venue
            address = f"{v.title}, {v.address}\n🌍 https://maps.google.com/?q={v.location.latitude},{v.location.longitude}"
        elif message.content_type == 'location' and message.location:
            lat, lon = message.location.latitude, message.location.longitude
            address = f"🌍 https://maps.google.com/?q={lat},{lon}"
        elif text == "✏️ Ввести адрес":
            bot.send_message(cid, "Напишите адрес текстом:", reply_markup=types.ReplyKeyboardRemove())
            return
        elif message.content_type == 'text' and message.text:
            address = message.text.strip()
        else:
            bot.send_message(cid, "Нужен адрес или локация:", reply_markup=address_keyboard())
            return

        data['address'] = address
        data['wait_for_address'] = False
        data['wait_for_contact'] = True
        bot.send_message(cid, "Укажите контакт для связи:", reply_markup=contact_keyboard())
        return

    # ——— Если ожидаем ввод контакта ———
    if data.get('wait_for_contact'):
        if text == "⬅️ Назад":
            data['wait_for_address'] = True
            data['wait_for_contact'] = False
            bot.send_message(cid, "Вернулись к выбору адреса. Укажите адрес:", reply_markup=address_keyboard())
            return
        if text == "✏️ Ввести ник":
            bot.send_message(cid, "Введите ваш Telegram-ник (без @):", reply_markup=types.ReplyKeyboardRemove())
            return
        if message.content_type == 'contact' and message.contact:
            contact = message.contact.phone_number
        elif message.content_type == 'text' and message.text:
            contact = "@" + message.text.strip().lstrip("@")
        else:
            bot.send_message(cid, "Выберите способ связи:", reply_markup=contact_keyboard())
            return

        data['contact'] = contact
        data['wait_for_contact'] = False
        data['wait_for_comment'] = True
        bot.send_message(cid, "Напишите комментарий к заказу:", reply_markup=comment_keyboard())
        return

    # ——— Если ожидаем ввод комментария ———
    if data.get('wait_for_comment'):
        if text == "⬅️ Назад":
            data['wait_for_contact'] = True
            data['wait_for_comment'] = False
            bot.send_message(cid, "Вернулись к выбору контакта. Укажите контакт:", reply_markup=contact_keyboard())
            return
        if text == "✏️ Комментарий к заказу":
            bot.send_message(cid, "Введите текст комментария:", reply_markup=types.ReplyKeyboardRemove())
            return
        if message.content_type == 'text' and text != "📤 Отправить заказ":
            data['comment'] = text.strip()
            bot.send_message(cid, "Комментарий сохранён. Нажмите 📤 Отправить заказ.", reply_markup=comment_keyboard())
            return
        if text == "📤 Отправить заказ":
            total_try = sum(i['price'] for i in data['cart'])
            rates = fetch_rates()
            rub = round(total_try * rates.get("RUB", 0) + 500, 2)
            usd = round(total_try * rates.get("USD", 0) + 2,   2)
            uah = round(total_try * rates.get("UAH", 0) + 200, 2)

            summary_rus = "\n".join(f"{i['category']}: {i['flavor']} — {i['price']}₺" for i in data['cart'])
            summary_en = summary_rus  # можно перевести при желании

            data['pending_order'] = {
                "cart": data['cart'][:],
                "summary_rus": (
                    f"📥 Новый заказ от @{message.from_user.username or message.from_user.first_name}:\n\n"
                    f"{summary_rus}\n\nИтог: {total_try}₺ (≈{rub}₽, ${usd}, ₴{uah})\n"
                    f"📍 Адрес: {data.get('address','—')}\n"
                    f"📱 Контакт: {data.get('contact','—')}\n"
                    f"💬 Комментарий: {data.get('comment','—')}"
                ),
                "summary_en": (
                    f"📥 New order from @{message.from_user.username or message.from_user.first_name}:\n\n"
                    f"{summary_en}\n\nTotal: {total_try}₺ (≈{rub}₽, ${usd}, ₴{uah})\n"
                    f"📍 Address: {data.get('address','—')}\n"
                    f"📱 Contact: {data.get('contact','—')}\n"
                    f"💬 Comment: {data.get('comment','—')}"
                ),
                "total_try": total_try,
                "rates": rates,
                "address": data.get("address"),
                "contact": data.get("contact"),
                "comment": data.get("comment", "")
            }

            curr_text = f"{total_try}₺ (≈ {rub}₽, ${usd}, ₴{uah})"
            pay_kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
            pay_kb.add(PAY_COD, PAY_TRANSFER)
            pay_kb.add(PAY_CRYPTO, "⬅️ Назад")

            bot.send_message(
                cid,
                f"Сумма к оплате: {curr_text}\n\n"
                f"💵 Оплата при получении: оплачиваете курьеру наличными или картой (переводом рубли и гривны)\n\n"
                f"💳 Перевод гривнами или рублями:\n"
                f"   • гривны: карта {TRANSFER_UAH_CARD}\n"
                f"   • рубли: {TRANSFER_RUB_PHONE}\n\n"
                f"₿ Оплатить криптой: переводите USDT (TRC20) на адрес {CRYPTO_ADDRESS}",
                reply_markup=pay_kb
            )
            return

    # ——— Обработка «Оплата при получении» ———
    if text == PAY_COD and data.get("pending_order"):
        pend = data["pending_order"]
        bot.send_message(PERSONAL_CHAT_ID, pend["summary_rus"])
        bot.send_message(GROUP_CHAT_ID,    pend["summary_en"])
        bot.send_message(
            cid,
            "Спасибо! Оплатите курьеру наличными или картой при доставке.",
            reply_markup=types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True).add("🛒 Оформить новый заказ")
        )
        for o in pend["cart"]:
            cat = o['category']
            for itm in menu[cat]["flavors"]:
                if itm['flavor'] == o['flavor']:
                    itm['stock'] = max(itm.get('stock', 1) - 1, 0)
                    break
        save_menu(menu)
        data["cart"] = []
        data["pending_order"] = None
        data["wait_for_address"] = False
        data["wait_for_contact"] = False
        data["wait_for_comment"] = False
        return

    # ——— Обработка «Перевод гривнами или рублями» ———
    if text == PAY_TRANSFER and data.get("pending_order"):
        pend = data["pending_order"]
        transfer_info = (
            "Для перевода используйте следующие реквизиты:\n\n"
            f"• Гривны: карта {TRANSFER_UAH_CARD}\n"
            f"• Рубли: {TRANSFER_RUB_PHONE}\n\n"
            "После перевода пришлите скриншот платежа или описание (TX-ID).\n"
            "Ваш заказ будет обработан после подтверждения."
        )
        bot.send_message(cid, transfer_info, reply_markup=view_cart_keyboard())
        data["awaiting_transfer_tx"] = True
        return

    # ——— Ожидание скрина или TX-ID для перевода ———
    if data.get("awaiting_transfer_tx") and message.content_type == "text":
        tx_info = message.text.strip()
        # Замените логику проверки на реальную
        confirmed = True
        if confirmed:
            pend = data["pending_order"]
            bot.send_message(
                GROUP_CHAT_ID,
                f"✅ Перевод (гривны/рубли) получен:\n{pend['summary_en']}\nПлатёж: {tx_info}"
            )
            bot.send_message(cid, "Перевод подтверждён! Ваш заказ будет доставлен.", reply_markup=get_main_keyboard())
            for o in pend["cart"]:
                cat = o['category']
                for itm in menu[cat]["flavors"]:
                    if itm['flavor'] == o['flavor']:
                        itm['stock'] = max(itm.get('stock', 1) - 1, 0)
                        break
            save_menu(menu)
            data["cart"] = []
            data["pending_order"] = None
            data["awaiting_transfer_tx"] = False
        else:
            bot.send_message(cid, "Перевод не найден или не зачислен. Попробуйте позже.", reply_markup=get_main_keyboard())
        return

    # ——— Обработка «Оплатить криптой» с добавлением $1 ———
    if text == PAY_CRYPTO and data.get("pending_order"):
        pend = data["pending_order"]
        total_try = pend["total_try"]
        rates = pend["rates"]

        # Получаем курс TRON (TRC20) в TRY и курс доллара
        try:
            r = requests.get("https://api.coingecko.com/api/v3/simple/price",
                             params={"ids":"tron","vs_currencies":"try,usd"}, timeout=5)
            prices = r.json()
            tron_price_try = prices["tron"]["try"]
            usd_to_try = 1 / rates.get("USD", 1)
            adjusted_try = total_try + usd_to_try
            amount_trx = round(adjusted_try / tron_price_try, 2)
        except:
            amount_trx = None

        text_crypto  = f"Сумма к оплате: {total_try}₺ + экв. $1 (для комиссии).\n\n"
        if amount_trx:
            text_crypto += f"≈ {amount_trx} TRX (TRC20) на адрес:\n`{CRYPTO_ADDRESS}`\n\n"
        else:
            text_crypto += f"Переведите необходимое количество TRX (TRC20) на адрес:\n`{CRYPTO_ADDRESS}`\n\n"
        text_crypto += (
            "После перевода отправьте, пожалуйста, скриншот транзакции или TX-ID.\n"
            "Ваш заказ будет обработан после подтверждения."
        )
        bot.send_message(cid, text_crypto, parse_mode="Markdown", reply_markup=get_main_keyboard())
        data["awaiting_crypto_tx"] = True
        return

    # ——— Ожидание TX-ID для криптовалюты ———
    if data.get("awaiting_crypto_tx") and message.content_type == "text":
        tx_hash = message.text.strip()
        # Ваша логика проверки в блокчейн-эксплорере
        confirmed = True
        if confirmed:
            pend = data["pending_order"]
            bot.send_message(
                GROUP_CHAT_ID,
                f"✅ Оплата криптой (TRC20) получена (tx: {tx_hash}):\n{pend['summary_en']}"
            )
            bot.send_message(cid, "Оплата подтверждена! Ваш заказ будет доставлен.", reply_markup=get_main_keyboard())
            for o in pend["cart"]:
                cat = o['category']
                for itm in menu[cat]["flavors"]:
                    if itm['flavor'] == o['flavor']:
                        itm['stock'] = max(itm.get('stock', 1) - 1, 0)
                        break
            save_menu(menu)
            data["cart"] = []
            data["pending_order"] = None
            data["awaiting_crypto_tx"] = False
        else:
            bot.send_message(cid, "Транзакция не найдена или не подтверждена. Попробуйте снова.", reply_markup=get_main_keyboard())
        return

    # ——— Кнопка «⬅️ Назад» — возврат в главное меню и сброс ожиданий ———
    if text == "⬅️ Назад":
        data["current_category"] = None
        data["wait_for_address"] = False
        data["wait_for_contact"] = False
        data["wait_for_comment"] = False
        data["pending_order"] = None
        data["awaiting_transfer_tx"] = False
        data["awaiting_crypto_tx"] = False
        bot.send_message(cid, "Выберите категорию:", reply_markup=get_main_keyboard())
        return

    # ——— Обычный заказ ———
    if text == "🗑️ Очистить корзину":
        data['cart'].clear()
        data['current_category'] = None
        data['wait_for_address'] = False
        data['wait_for_contact'] = False
        data['wait_for_comment'] = False
        bot.send_message(cid, "Корзина очищена.", reply_markup=get_main_keyboard())
        return

    if text == "➕ Добавить ещё":
        data['current_category'] = None
        bot.send_message(cid, "Выберите категорию:", reply_markup=get_main_keyboard())
        return

    if text == "✅ Завершить заказ" and not data.get('wait_for_address'):
        if not data['cart']:
            bot.send_message(cid, "Корзина пуста.")
            return
        total_try = sum(i['price'] for i in data['cart'])
        summary = "\n".join(f"{i['category']}: {i['flavor']} — {i['price']}₺" for i in data['cart'])
        bot.send_message(
            cid,
            f"🛒 Ваш заказ:\n\n{summary}\n\nИтог: {total_try}₺\n\nВыберите способ указания адреса:",
            reply_markup=address_keyboard()
        )
        data['wait_for_address'] = True
        return

    if text in menu:
        data['current_category'] = text
        bot.send_message(cid, f"Выберите вкус ({text}):", reply_markup=get_flavors_keyboard(text))
        return

    cat = data.get('current_category')
    if cat:
        price = menu[cat]["price"]
        for it in menu[cat]["flavors"]:
            emoji = it.get("emoji", "").strip()
            flavor = it["flavor"]
            stock = it.get("stock", 0)
            label = f"{emoji} {flavor} ({price}₺) [{stock} шт]" if emoji else f"{flavor} ({price}₺) [{stock} шт]"
            if text == label and stock > 0:
                data['cart'].append({'category': cat, 'flavor': flavor, 'price': price})
                count = len(data['cart'])
                kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
                kb.add("➕ Добавить ещё", "✅ Завершить заказ", "🗑️ Очистить корзину", "🛒 Корзина")
                bot.send_message(
                    cid,
                    f"{cat} — {flavor} ({price}₺) добавлен(а) в корзину. В корзине [{count}] товар(ов).",
                    reply_markup=kb
                )
                return
        bot.send_message(cid, "Пожалуйста, выберите вкус из списка:", reply_markup=get_flavors_keyboard(cat))
        return

if __name__ == "__main__":
    bot.remove_webhook()
    bot.polling(none_stop=True)
