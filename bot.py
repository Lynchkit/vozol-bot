# -*- coding: utf-8 -*-
import os
import json
import requests
import telebot
from telebot import types

# ——— Сразу в начале: удаляем возможный старый webhook ———
TOKEN = os.getenv("TOKEN")
if not TOKEN:
    raise RuntimeError("Environment variable TOKEN is not set! Start the container with -e TOKEN=<your_token>.")

# Пробуем удалить webhook у Telegram, чтобы бот работал только в polling-режиме
try:
    requests.get(f"https://api.telegram.org/bot{TOKEN}/deleteWebhook", timeout=5)
except Exception:
    pass

# ——— После этого создаём экземпляр бота ———
bot = telebot.TeleBot(TOKEN)

# ——— Настройки ———
GROUP_CHAT_ID    = int(os.getenv("GROUP_CHAT_ID",    "-1002414380144"))
PERSONAL_CHAT_ID = int(os.getenv("PERSONAL_CHAT_ID", "424751188"))
MENU_PATH = "menu.json"
DEFAULT_CATEGORY_PRICE = 1300  # Default price for new categories

# Пытаемся загрузить меню из файла; если файл отсутствует или JSON битый, создаём пустое
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

def save_menu(menu):
    with open(MENU_PATH, "w", encoding="utf-8") as f:
        json.dump(menu, f, ensure_ascii=False, indent=2)

menu = load_menu()
user_data = {}

# ——— Клавиатуры для заказа ———
def get_main_keyboard():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
    for cat in menu:
        kb.add(cat)
    kb.add("📝 Device Descriptions")
    kb.add("📷 Device Images")
    return kb

def get_flavors_keyboard(cat):
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
    category_price = menu[cat]["price"]
    for it in menu[cat]["flavors"]:
        if it.get("stock", 0) > 0:
            emoji = it.get("emoji", "").strip()
            flavor = it["flavor"]
            stock = it.get("stock", 0)
            if emoji:
                label = f"{emoji} {flavor} ({category_price}₺) [{stock} pcs]"
            else:
                label = f"{flavor} ({category_price}₺) [{stock} pcs]"
            kb.add(label)
    kb.add("⬅️ Back")
    return kb

def description_keyboard():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
    kb.add("⬅️ Back")
    return kb

def address_keyboard():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
    kb.add(types.KeyboardButton("📍 Share Location", request_location=True))
    kb.add("🗺️ Choose on Map")
    kb.add("✏️ Enter Address")
    kb.add("⬅️ Back")
    return kb

def contact_keyboard():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
    kb.add(types.KeyboardButton("📞 Share Contact", request_contact=True))
    kb.add("✏️ Enter Username")
    kb.add("⬅️ Back")
    return kb

def comment_keyboard():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
    kb.add("✏️ Add Comment")
    kb.add("📤 Submit Order")
    kb.add("⬅️ Back")
    return kb

# ——— Клавиатура для редактирования (/change) ———
def edit_action_keyboard():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
    kb.add("➕ Add Category", "➖ Remove Category")
    kb.add("💲 Fix Price", "ALL IN")
    kb.add("🔄 Actual Flavor")
    kb.add("❌ Cancel")
    return kb

# ——— Конвертация валют (для /convert) ———
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

@bot.message_handler(commands=['convert'])
def handle_convert(message):
    parts = message.text.split()[1:]
    if not parts:
        bot.reply_to(message, "Use: /convert 1300 1400 ...")
        return
    rates = fetch_rates()
    if not any(rates.values()):
        bot.reply_to(message, "Unable to fetch rates.")
        return
    out = []
    for p in parts:
        try:
            t = float(p)
        except:
            out.append(f"{p}₺ → invalid format")
            continue
        rub = round(t * rates.get("RUB", 0) + 400, 2)
        usd = round(t * rates.get("USD", 0) + 2,   2)
        uah = round(t * rates.get("UAH", 0),      2)
        out.append(f"{int(t)}₺ → {rub}₽, ${usd}, ₴{uah}")
    bot.reply_to(message, "\n".join(out))

# ——— Команда /change — теперь доступна всем ———
@bot.message_handler(commands=['change'])
def cmd_change(message):
    data = user_data.setdefault(message.chat.id, {
        "cart": [], "current_category": None,
        "wait_for_address": False, "wait_for_contact": False, "wait_for_comment": False
    })
    data['edit_phase'] = 'choose_action'
    bot.send_message(message.chat.id, "Menu editing: choose action", reply_markup=edit_action_keyboard())

# ——— Команда /start ———
@bot.message_handler(commands=['start'])
def cmd_start(message):
    user_data[message.chat.id] = {
        "cart": [], "current_category": None,
        "wait_for_address": False, "wait_for_contact": False, "wait_for_comment": False
    }
    bot.send_message(
        message.chat.id,
        "Welcome! Please choose a category:",
        reply_markup=get_main_keyboard()
    )

# ——— Универсальный хендлер ———
@bot.message_handler(content_types=['text','location','venue','contact'])
def universal_handler(message):
    # Игнорируем команды (чтобы /start и /change не перекрывались)
    if message.text and message.text.startswith('/'):
        return

    cid = message.chat.id
    text = message.text or ""
    data = user_data.setdefault(cid, {
        "cart": [], "current_category": None,
        "wait_for_address": False, "wait_for_contact": False, "wait_for_comment": False
    })

    # ——— Обработка кнопки «Device Descriptions» ———
    if text == "📝 Device Descriptions":
        description = (
            "🔹 vozol star 20 000\n"
            "– up to 20 000 puffs\n"
            "– 24 ml e-liquid capacity\n"
            "– 650 mAh battery, fast charging\n"
            "– LED screen showing battery level and e-liquid level\n"
            "– mesh coil\n"
            "– compact, convenient, MTL format\n\n"
            "🔹 vozol shisha gear 25 000\n"
            "– up to 25 000 puffs\n"
            "– 18 ml e-liquid capacity\n"
            "– 1000 mAh battery\n"
            "– large color screen\n"
            "– dual mesh coil\n"
            "– rich flavor, adjustable airflow, stylish design\n\n"
            "🔹 vozol vista 20 000\n"
            "– up to 20 000 puffs\n"
            "– 24 ml e-liquid capacity\n"
            "– 650 mAh battery, fast charging\n"
            "– OLED display\n"
            "– 6 power modes, mesh coil\n"
            "– eco-friendly body, MTL format\n\n"
            "🔹 vozol gear 20 000\n"
            "– up to 20 000 puffs\n"
            "– 20 ml e-liquid capacity\n"
            "– 500 mAh battery\n"
            "– informative display\n"
            "– two modes: eco and power\n"
            "– Type-C charging, mouthpiece protection"
        )
        bot.send_message(cid, description, reply_markup=description_keyboard())
        return

    # ——— Обработка кнопки «Device Images» ———
    if text == "📷 Device Images":
        bot.send_message(cid, "Sending all device images:", reply_markup=types.ReplyKeyboardRemove())
        urls = [
            "https://raw.githubusercontent.com/Lynchkit/vozol-bot/refs/heads/main/GEAR.png",
            "https://raw.githubusercontent.com/Lynchkit/vozol-bot/refs/heads/main/photo_1_2024-10-17_09-50-13.png",
            "https://raw.githubusercontent.com/Lynchkit/vozol-bot/refs/heads/main/photo_2025-03-06_09-11-29.jpg",
            "https://raw.githubusercontent.com/Lynchkit/vozol-bot/refs/heads/main/photo_3_2024-10-17_09-50-13.png"
        ]
        for url in urls:
            bot.send_photo(cid, url)
        bot.send_message(cid, "Back to main menu:", reply_markup=get_main_keyboard())
        return

    # ——— Режим редактирования меню (/change) ———
    if data.get('edit_phase'):
        phase = data['edit_phase']

        # Кнопка «⬅️ Back» — возврат на уровень выше в /change
        if text == "⬅️ Back":
            data.pop('edit_cat', None)
            data.pop('edit_flavor', None)
            data['edit_phase'] = 'choose_action'
            bot.send_message(cid, "Back to editing menu:", reply_markup=edit_action_keyboard())
            return

        # Кнопка ❌ Cancel — полная отмена редактирования и возврат в главное меню
        if text == "❌ Cancel":
            data.pop('edit_phase', None)
            data.pop('edit_cat', None)
            data.pop('edit_flavor', None)
            bot.send_message(cid, "Menu editing cancelled.", reply_markup=get_main_keyboard())
            return

        # 1) Главное меню редактирования
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
                bot.send_message(cid, "Select category to set new price:", reply_markup=kb)
            elif text == "ALL IN":
                data['edit_phase'] = 'choose_all_in_cat'
                kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
                for cat in menu:
                    kb.add(cat)
                kb.add("⬅️ Back")
                bot.send_message(cid, "Select category to replace entire flavor list:", reply_markup=kb)
            elif text == "🔄 Actual Flavor":
                data['edit_phase'] = 'choose_cat_actual'
                kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
                for cat in menu:
                    kb.add(cat)
                kb.add("⬅️ Back")
                bot.send_message(cid, "Select category to update flavor stock:", reply_markup=kb)
            else:
                bot.send_message(cid, "Choose action:", reply_markup=edit_action_keyboard())
            return

        # 2) Добавить категорию
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
            menu[new_cat] = {
                "price": DEFAULT_CATEGORY_PRICE,
                "flavors": []
            }
            save_menu(menu)
            data.pop('edit_cat', None)
            data['edit_phase'] = 'choose_action'
            bot.send_message(
                cid,
                f"Category \"{new_cat}\" added with price {DEFAULT_CATEGORY_PRICE}₺.",
                reply_markup=edit_action_keyboard()
            )
            return

        # 3) Удалить категорию
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
                bot.send_message(cid, f"Category \"{text}\" removed.", reply_markup=edit_action_keyboard())
            else:
                kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
                kb.add("⬅️ Back")
                bot.send_message(cid, "Select a valid category.", reply_markup=kb)
            return

        # 4) Выбрать категорию для фиксации цены
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
                bot.send_message(cid, f"Enter new price in ₺ for category \"{text}\":", reply_markup=kb)
            else:
                kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
                kb.add("⬅️ Back")
                bot.send_message(cid, "Choose category from the list.", reply_markup=kb)
            return

        # 5) Ввод новой цены для категории
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
            bot.send_message(cid, f"Price for category \"{cat}\" set to {int(new_price)}₺.", reply_markup=edit_action_keyboard())
            data.pop('edit_cat', None)
            data['edit_phase'] = 'choose_action'
            return

        # 6) Выбрать категорию для ALL IN (полностью заменить список вкусов)
        if phase == 'choose_all_in_cat':
            if text == "⬅️ Back":
                data['edit_phase'] = 'choose_action'
                bot.send_message(cid, "Back to editing menu:", reply_markup=edit_action_keyboard())
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
                    cid,
                    f"Current flavors in \"{text}\" (one per line as \"Name - qty\"):\n\n{joined}\n\n"
                    "Send the full updated list in the same format. Each line: “Name - qty”.",
                    reply_markup=kb
                )
                data['edit_phase'] = 'replace_all_in'
            else:
                kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
                kb.add("⬅️ Back")
                bot.send_message(cid, "Choose category from the list.", reply_markup=kb)
            return

        # 7) Заменить полный список вкусов в категории (ALL IN)
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
                new_flavors.append({
                    "emoji": "",
                    "flavor": name,
                    "stock": int(qty)
                })
            menu[cat]["flavors"] = new_flavors
            save_menu(menu)
            bot.send_message(cid, f"Full flavor list for \"{cat}\" has been replaced.", reply_markup=edit_action_keyboard())
            data.pop('edit_cat', None)
            data['edit_phase'] = 'choose_action'
            return

        # 8) Выбрать категорию для Actual Flavor
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
                    kb.add(f"{flavor} [{stock} pcs]")
                kb.add("⬅️ Back")
                bot.send_message(cid, "Select flavor to update stock:", reply_markup=kb)
            else:
                kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
                kb.add("⬅️ Back")
                bot.send_message(cid, "Choose category from the list.", reply_markup=kb)
            return

        # 9) Выбрать вкус для Actual Flavor
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
                bot.send_message(cid, "Enter actual quantity:", reply_markup=kb)
            else:
                bot.send_message(cid, "Flavor not found. Choose again:", reply_markup=edit_action_keyboard())
                data['edit_phase'] = 'choose_action'
            return

        # 10) Ввод актуального количества для выбранного вкуса
        if phase == 'enter_actual_qty':
            if text == "⬅️ Back":
                data.pop('edit_flavor', None)
                data['
