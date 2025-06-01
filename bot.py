# -*- coding: utf-8 -*-
import os
import json
import requests
import telebot
from telebot import types

# ——— Сразу в начале: удаляем возможный старый webhook ———
TOKEN = os.getenv("TOKEN")
if not TOKEN:
    raise RuntimeError("Переменная окружения TOKEN не задана! Запустите контейнер с -e TOKEN=<ваш_токен>.")

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
DEFAULT_CATEGORY_PRICE = 1300  # Цена по умолчанию для новых категорий

# ——— Работа с меню ———
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

# ——— Клавиатуры ———
def get_main_keyboard():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
    for cat in menu:
        kb.add(cat)
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

def comment_keyboard():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
    kb.add("✏️ Комментарий к заказу")
    kb.add("📤 Отправить заказ")
    return kb

def edit_action_keyboard():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
    kb.add("➕ Add Category", "➖ Remove Category")
    kb.add("💲 Fix Price",   "ALL IN")
    kb.add("🔄 Actual Flavor")
    kb.add("⬅️ Back", "❌ Cancel")
    return kb

# ——— Конвертация валют ———
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
    return {"RUB": 0, "USD": 0, "UAH": 0}

# ——— Обработчики команд ———
@bot.message_handler(commands=['start'])
def cmd_start(message):
    user_data[message.chat.id] = {
        "cart": [], "current_category": None,
        "wait_for_address": False, "wait_for_contact": False, "wait_for_comment": False
    }
    bot.send_message(
        message.chat.id,
        "Добро пожаловать! Выберите категорию:",
        reply_markup=get_main_keyboard()
    )

@bot.message_handler(commands=['change'])
def cmd_change(message):
    data = user_data.setdefault(message.chat.id, {
        "cart": [], "current_category": None,
        "wait_for_address": False, "wait_for_contact": False, "wait_for_comment": False
    })
    data['edit_phase'] = 'choose_action'
    bot.send_message(message.chat.id, "Menu editing: choose action", reply_markup=edit_action_keyboard())

@bot.message_handler(commands=['convert'])
def handle_convert(message):
    parts = message.text.split()[1:]
    if not parts:
        bot.reply_to(message, "Напиши: /convert 1300 1400 ...")
        return
    rates = fetch_rates()
    if not any(rates.values()):
        bot.reply_to(message, "Не удалось получить курсы.")
        return
    out = []
    for p in parts:
        try:
            t = float(p)
        except:
            out.append(f"{p}₺ → неверный формат")
            continue
        rub = round(t * rates.get("RUB", 0) + 400, 2)
        usd = round(t * rates.get("USD", 0) + 2,   2)
        uah = round(t * rates.get("UAH", 0),      2)
        out.append(f"{int(t)}₺ → {rub}₽, ${usd}, ₴{uah}")
    bot.reply_to(message, "\n".join(out))

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

    # — Режим редактирования меню (/change) —
    if data.get('edit_phase'):
        phase = data['edit_phase']

        # Кнопка «⬅️ Back» — вернуться к выбору действия
        if text == "⬅️ Back":
            data.pop('edit_cat', None)
            data.pop('edit_flavor', None)
            data['edit_phase'] = 'choose_action'
            bot.send_message(cid, "Back to editing menu:", reply_markup=edit_action_keyboard())
            return

        # Кнопка «❌ Cancel» — отмена редактирования
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
                bot.send_message(cid, "Select category to update flavor stock:", reply_markup=kb)
            else:
                bot.send_message(cid, "Choose action:", reply_markup=edit_action_keyboard())
            return

        # 2) Добавление категории
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
            bot.send_message(cid, f"Category «{new_cat}» added with price {DEFAULT_CATEGORY_PRICE}₺.", reply_markup=edit_action_keyboard())
            return

        # 3) Удаление категории
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
                bot.send_message(cid, f"Enter new price in ₺ for category «{text}»:", reply_markup=kb)
            else:
                kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
                kb.add("⬅️ Back")
                bot.send_message(cid, "Choose category from the list.", reply_markup=kb)
            return

        # 5) Ввод новой цены
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

        # 6) ALL IN: заменить весь список вкусов
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

        # 7) Заменить список вкусов (ALL IN)
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

        # 8) Actual Flavor: обновление stock
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
                bot.send_message(cid, "Select flavor to update stock:", reply_markup=kb)
            else:
                kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
                kb.add("⬅️ Back")
                bot.send_message(cid, "Choose category from the list.", reply_markup=kb)
            return

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
                bot.send_message(cid, "Enter actual quantity!", reply_markup=kb)
            else:
                bot.send_message(cid, "Flavor not found. Choose again:", reply_markup=edit_action_keyboard())
                data['edit_phase'] = 'choose_action'
            return

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

        # Если фаза неизвестна — возвращаемся к выбору действия
        data['edit_phase'] = 'choose_action'
        bot.send_message(cid, "Back to editing menu:", reply_markup=edit_action_keyboard())
        return

    # — Если ожидаем ввод адреса — (здесь не реализован, пропущено) —

    # — Если ожидаем ввод контакта — (пропущено) —

    # — Если ожидаем ввод комментария — (обработка дальше) —
    if data.get('wait_for_comment'):
        if text == "✏️ Комментарий к заказу":
            bot.send_message(cid, "Введите текст комментария:", reply_markup=types.ReplyKeyboardRemove())
            return

        if text != "📤 Отправить заказ":
            data['comment'] = text.strip()
            bot.send_message(
                cid,
                "Комментарий сохранён. Нажмите 📤 Отправить заказ.",
                reply_markup=comment_keyboard()
            )
            return

        if text == "📤 Отправить заказ":
            cart = data['cart']
            total_try = sum(i['price'] for i in cart)
            summary = "\n".join(f"{i['category']}: {i['flavor']} — {i['price']}₺" for i in cart)
            rates = fetch_rates()
            rub = round(total_try * rates.get("RUB", 0) + 400, 2)
            usd = round(total_try * rates.get("USD", 0) + 2,   2)
            uah = round(total_try * rates.get("UAH", 0),      2)
            conv = f"({rub}₽, ${usd}, ₴{uah})"
            full = (
                f"📥 Новый заказ от @{message.from_user.username or message.from_user.first_name}:\n\n"
                f"{summary}\n\nИтог: {total_try}₺ {conv}\n"
                f"💬 Комментарий: {data.get('comment','—')}"
            )
            # Уменьшаем stock
            for o in cart:
                cat = o['category']
                for itm in menu[cat]["flavors"]:
                    if itm['flavor'] == o['flavor']:
                        itm['stock'] = max(itm.get('stock', 1) - 1, 0)
                        break
            save_menu(menu)

            bot.send_message(cid, "Ваш заказ принят! Спасибо.", reply_markup=types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True).add("🛒 Оформить новый заказ"))
            bot.send_message(GROUP_CHAT_ID, full)
            bot.send_message(PERSONAL_CHAT_ID, "[Копия заказа]\n\n" + full)

            data['cart'] = []
            data['current_category'] = None
            data['wait_for_address'] = False
            data['wait_for_contact'] = False
            data['wait_for_comment'] = False
            data.pop('comment', None)
            return

    # — Обычный сценарий заказа —
    if text == "⬅️ Назад":
        data['current_category'] = None
        bot.send_message(cid, "Выберите категорию:", reply_markup=get_main_keyboard())
        return

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

    if text == "✅ Завершить заказ":
        if not data['cart']:
            bot.send_message(cid, "Корзина пуста.")
            return
        total_try = sum(i['price'] for i in data['cart'])
        summary = "\n".join(f"{i['category']}: {i['flavor']} — {i['price']}₺" for i in data['cart'])
        bot.send_message(
            cid,
            f"🛒 Ваш заказ:\n\n{summary}\n\nИтог: {total_try}₺\n\nВыберите способ указания адреса:",
            reply_markup=types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True).add("⬅️ Назад")
        )
        data['wait_for_address'] = True
        return

    # Выбор категории для заказа
    if text in menu:
        data['current_category'] = text
        bot.send_message(cid, f"Выберите вкус ({text}):", reply_markup=get_flavors_keyboard(text))
        return

    # Выбор вкуса в категории
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
                    'emoji':    emoji,
                    'flavor':   flavor,
                    'price':    price
                })
                count = len(data['cart'])
                kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
                kb.add("➕ Добавить ещё", "✅ Завершить заказ", "🗑️ Очистить корзину")
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
