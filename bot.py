import os
import json
import requests
import telebot
from telebot import types

# ——— Настройки ———
TOKEN = os.getenv("TOKEN", "7931006644:AAEVeRpgQivZL5Qmv113tqNlWWAF6sndwbk")
GROUP_CHAT_ID = int(os.getenv("GROUP_CHAT_ID", "-1002414380144"))
PERSONAL_CHAT_ID = int(os.getenv("PERSONAL_CHAT_ID", "424751188"))
ADMIN_IDS = {PERSONAL_CHAT_ID}
MENU_PATH = "menu.json"
DEFAULT_CATEGORY_PRICE = 1300  # Цена по умолчанию для новых категорий

# ▼▼▼ Принудительно удаляем Webhook через HTTP перед созданием TeleBot
delete_url = f"https://api.telegram.org/bot{TOKEN}/deleteWebhook"
try:
    requests.get(delete_url, timeout=5)
except Exception:
    pass
# ▼▼▼

bot = telebot.TeleBot(TOKEN)
bot.remove_webhook()  # простой вызов, без аргументов
user_data = {}

# ——— Загрузка/сохранение меню ———
# Ожидаемый формат menu.json:
# {
#   "CategoryName": {
#       "price": 1000,
#       "flavors": [
#           { "emoji": "🍓", "flavor": "Strawberry Mango", "stock": 3 },
#           ...
#       ]
#   },
#   ...
# }
def load_menu():
    with open(MENU_PATH, "r", encoding="utf-8") as f:
        return json.load(f)

def save_menu(menu):
    with open(MENU_PATH, "w", encoding="utf-8") as f:
        json.dump(menu, f, ensure_ascii=False, indent=2)

menu = load_menu()

# ——— Клавиатуры ———
def get_main_keyboard():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
    for cat in menu:
        kb.add(cat)
    return kb

def get_flavors_keyboard(cat):
    """
    Строит клавиатуру со всеми доступными вкусами из категории cat.
    Цена каждого вкуса берётся из menu[cat]["price"].
    Если emoji пустое, лишний пробел не добавляется.
    """
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
    category_price = menu[cat]["price"]
    for it in menu[cat]["flavors"]:
        if it.get("stock", 0) > 0:
            emoji = it.get("emoji", "").strip()
            flavor = it["flavor"]
            stock = it.get("stock", 0)
            # Формируем метку без лишнего пробела, если emoji пустой
            if emoji:
                label = f"{emoji} {flavor} ({category_price}₺) [{stock} шт]"
            else:
                label = f"{flavor} ({category_price}₺) [{stock} шт]"
            kb.add(label)
    kb.add("⬅️ Назад")
    return kb

def address_keyboard():
    """
    Добавили кнопку «⬅️ Назад» для отмены ввода адреса.
    """
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
    kb.add(types.KeyboardButton("📍 Поделиться геопозицией", request_location=True))
    kb.add("🗺️ Выбрать точку на карте")
    kb.add("✏️ Ввести адрес")
    kb.add("⬅️ Назад")
    return kb

def contact_keyboard():
    """
    Добавили кнопку «⬅️ Назад» для отмены ввода контакта.
    """
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
    kb.add(types.KeyboardButton("📞 Поделиться контактом", request_contact=True))
    kb.add("✏️ Ввести ник")
    kb.add("⬅️ Назад")
    return kb

def comment_keyboard():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
    kb.add("✏️ Комментарий к заказу")
    kb.add("📤 Отправить заказ")
    return kb

def edit_action_keyboard():
    """
    Клавиатура для режима /change:
    ➕ Add Category   — добавить категорию
    ➖ Remove Category — удалить категорию
    ➕ Add Flavor     — добавить вкус
    ➖ Remove Flavor   — убрать вкус
    💲 Fix Price      — задать цену категории (все ее вкусы получат эту цену)
    ALL IN           — полностью заменить список вкусов категории
    ❌ Cancel         — отменить редактирование
    """
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
    kb.add("➕ Add Category", "➖ Remove Category")
    kb.add("➕ Add Flavor",   "➖ Remove Flavor")
    kb.add("💲 Fix Price",   "ALL IN")
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

# ——— Команда /change ———
@bot.message_handler(commands=['change'])
def cmd_change(message):
    if message.chat.id not in ADMIN_IDS:
        return
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
        "Добро пожаловать! Выберите категорию:",
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

    # — Режим редактирования меню (/change) —
    if data.get('edit_phase'):
        phase = data['edit_phase']

        # Кнопка ❌ Cancel — отмена редактирования и возвращение в обычный режим
        if text == "❌ Cancel":
            data.pop('edit_phase', None)
            data.pop('edit_cat', None)
            data.pop('new_price', None)
            bot.send_message(cid, "Menu editing cancelled.", reply_markup=get_main_keyboard())
            return

        # 1) Главное меню редактирования
        if phase == 'choose_action':
            if text == "➕ Add Category":
                data['edit_phase'] = 'add_category'
                bot.send_message(cid, "Enter new category name:")
            elif text == "➖ Remove Category":
                data['edit_phase'] = 'remove_category'
                kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
                for cat in menu:
                    kb.add(cat)
                kb.add("❌ Cancel")
                bot.send_message(cid, "Select category to remove:", reply_markup=kb)
            elif text == "➕ Add Flavor":
                data['edit_phase'] = 'choose_cat_add_flavor'
                kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
                for cat in menu:
                    kb.add(cat)
                kb.add("❌ Cancel")
                bot.send_message(cid, "Select category to add flavor to:", reply_markup=kb)
            elif text == "➖ Remove Flavor":
                data['edit_phase'] = 'choose_cat_remove_flavor'
                kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
                for cat in menu:
                    kb.add(cat)
                kb.add("❌ Cancel")
                bot.send_message(cid, "Select category to remove flavor from:", reply_markup=kb)
            elif text == "💲 Fix Price":
                data['edit_phase'] = 'choose_fix_price_cat'
                kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
                for cat in menu:
                    kb.add(cat)
                kb.add("❌ Cancel")
                bot.send_message(cid, "Select category to fix price for:", reply_markup=kb)
            elif text == "ALL IN":
                data['edit_phase'] = 'choose_all_in_cat'
                kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
                for cat in menu:
                    kb.add(cat)
                kb.add("❌ Cancel")
                bot.send_message(cid, "Select category to replace full flavor list:", reply_markup=kb)
            else:
                bot.send_message(cid, "Choose action:", reply_markup=edit_action_keyboard())
            return

        # 2) Добавить категорию
        if phase == 'add_category':
            new_cat = text.strip()
            if not new_cat or new_cat in menu:
                bot.send_message(cid, "Invalid or existing name. Try again:")
                return
            # Создаём новую категорию с дефолтной ценой и пустым списком вкусов
            menu[new_cat] = {
                "price": DEFAULT_CATEGORY_PRICE,
                "flavors": []
            }
            save_menu(menu)
            data.pop('edit_cat', None)
            data['edit_phase'] = 'choose_action'
            bot.send_message(
                cid,
                f"Category «{new_cat}» added with price {DEFAULT_CATEGORY_PRICE}₺.",
                reply_markup=edit_action_keyboard()
            )
            return

        # 3) Удалить категорию
        if phase == 'remove_category':
            if text in menu:
                del menu[text]
                save_menu(menu)
                data.pop('edit_cat', None)
                data['edit_phase'] = 'choose_action'
                bot.send_message(cid, f"Category «{text}» removed.", reply_markup=edit_action_keyboard())
            else:
                bot.send_message(cid, "Select valid category.")
            return

        # 4) Выбрать категорию для добавления вкуса
        if phase == 'choose_cat_add_flavor':
            if text in menu:
                data['edit_cat'] = text
                data['edit_phase'] = 'add_flavor'
                bot.send_message(cid, "Enter flavor and quantity (e.g.: Strawberry Mango - 1):")
            else:
                bot.send_message(cid, "Choose category from the list.")
            return

        # 5) Добавить вкус в категорию
        if phase == 'add_flavor':
            cat = data.get('edit_cat')
            if not cat:
                data['edit_phase'] = 'choose_action'
                data.pop('edit_cat', None)
                bot.send_message(cid, "Category error. Back to menu.", reply_markup=edit_action_keyboard())
                return
            if '-' not in text:
                bot.send_message(cid, "Use format: Name - qty")
                return
            name, qty = map(str.strip, text.rsplit('-', 1))
            if not qty.isdigit():
                bot.send_message(cid, "Quantity must be a number.")
                return
            menu[cat]["flavors"].append({
                "emoji": "",
                "flavor": name,
                "stock": int(qty)
            })
            save_menu(menu)
            data.pop('edit_cat', None)
            data['edit_phase'] = 'choose_action'
            bot.send_message(cid, f"Added flavor «{name}» ({qty} pcs) to «{cat}».", reply_markup=edit_action_keyboard())
            return

        # 6) Выбрать категорию для удаления вкуса
        if phase == 'choose_cat_remove_flavor':
            if text in menu:
                data['edit_cat'] = text
                data['edit_phase'] = 'remove_flavor'
                kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
                for it in menu[text]["flavors"]:
                    kb.add(f"{it['flavor']} [{it['stock']} шт]")
                kb.add("❌ Cancel")
                bot.send_message(cid, "Select flavor to remove:", reply_markup=kb)
            else:
                bot.send_message(cid, "Choose category from the list.")
            return

        # 7) Удалить вкус из категории
        if phase == 'remove_flavor':
            cat = data.get('edit_cat')
            flavor_name = text.split(' [')[0]
            if cat and flavor_name:
                for it in menu[cat]["flavors"][:]:
                    if it['flavor'] == flavor_name:
                        if it['stock'] > 1:
                            it['stock'] -= 1
                        else:
                            menu[cat]["flavors"].remove(it)
                        save_menu(menu)
                        bot.send_message(cid, f"Updated flavor «{flavor_name}» in «{cat}».", reply_markup=edit_action_keyboard())
                        data.pop('edit_cat', None)
                        data['edit_phase'] = 'choose_action'
                        return
                bot.send_message(cid, "Flavor not found.", reply_markup=edit_action_keyboard())
            else:
                bot.send_message(cid, "Error, choose again.", reply_markup=edit_action_keyboard())
            return

        # 8) Выбрать категорию для фиксации цены
        if phase == 'choose_fix_price_cat':
            if text in menu:
                data['edit_cat'] = text
                data['edit_phase'] = 'enter_new_price'
                bot.send_message(cid, f"Enter new price in ₺ for category «{text}»:")
            else:
                bot.send_message(cid, "Choose category from the list.")
            return

        # 9) Ввод новой цены для категории
        if phase == 'enter_new_price':
            cat = data.get('edit_cat')
            try:
                new_price = float(text.strip())
            except:
                bot.send_message(cid, "Invalid price format. Enter a number, e.g. 1500:")
                return
            menu[cat]["price"] = int(new_price)
            save_menu(menu)
            bot.send_message(cid, f"Price for category «{cat}» set to {int(new_price)}₺ (all flavors inherit this price).", reply_markup=edit_action_keyboard())
            data.pop('edit_cat', None)
            data.pop('new_price', None)
            data['edit_phase'] = 'choose_action'
            return

        # 10) Выбрать категорию для ALL IN (полностью заменить список вкусов)
        if phase == 'choose_all_in_cat':
            if text in menu:
                data['edit_cat'] = text
                # Собираем текущие вкусы в текст для подсказки
                current_list = []
                for itm in menu[text]["flavors"]:
                    current_list.append(f"{itm['flavor']} - {itm['stock']}")
                joined = "\n".join(current_list) if current_list else "(пусто)"
                bot.send_message(
                    cid,
                    f"Current flavors in «{text}» (one per line as \"Name - qty\"):\n\n{joined}\n\n"
                    "Send the full updated list in the same format. Each line: “Name - qty”."
                )
                data['edit_phase'] = 'replace_all_in'
            else:
                bot.send_message(cid, "Choose category from the list.")
            return

        # 11) Заменить полный список вкусов в категории (ALL IN)
        if phase == 'replace_all_in':
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
            # Заменяем список вкусов, сохраняя прежнюю цену категории
            menu[cat]["flavors"] = new_flavors
            save_menu(menu)
            bot.send_message(cid, f"Full flavor list for «{cat}» has been replaced.", reply_markup=edit_action_keyboard())
            data.pop('edit_cat', None)
            data['edit_phase'] = 'choose_action'
            return

        # Если фаза неизвестна → возвращаемся к выбору действия
        data['edit_phase'] = 'choose_action'
        bot.send_message(cid, "Back to editing menu:", reply_markup=edit_action_keyboard())
        return

    # — Если ожидаем ввод адреса — обработка локации/текста перед выбором категории —
    if data.get('wait_for_address'):
        # Новая обработка «⬅️ Назад»: вернуться к выбору категории, отменив адрес
        if text == "⬅️ Назад":
            data['wait_for_address'] = False
            data['current_category'] = None
            bot.send_message(cid, "Адрес не указан. Вернитесь к выбору категории:", reply_markup=get_main_keyboard())
            return

        if text == "🗺️ Выбрать точку на карте":
            bot.send_message(
                cid,
                "Чтобы выбрать точку:\n📎 → Местоположение → «Выбрать на карте» → метка → Отправить",
                reply_markup=types.ReplyKeyboardRemove()
            )
            return

        # Если пользователь прислал геометку (venue)
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

    # — Если ожидаем ввод контакта — обработка перед следующими блоками —
    if data.get('wait_for_contact'):
        # Обработка «⬅️ Назад»: вернуться на шаг выбора адреса
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

    # — Если ожидаем ввод комментария — обработка перед выбором —
    if data.get('wait_for_comment'):
        if text == "✏️ Комментарий к заказу":
            bot.send_message(cid, "Введите текст комментария:", reply_markup=types.ReplyKeyboardRemove())
            return

        if message.content_type == 'text' and message.text and text != "📤 Отправить заказ":
            data['comment'] = message.text.strip()
            bot.send_message(
                cid,
                "Комментарий сохранён. Нажмите 📤 Отправить заказ.",
                reply_markup=types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True).add("📤 Отправить заказ").add("⬅️ Назад")
            )
            return

        # Если пользователь нажал «⬅️ Назад» из комментария — возвращаем к выбору контакта
        if text == "⬅️ Назад":
            data['wait_for_contact'] = True
            data['wait_for_comment'] = False
            bot.send_message(cid, "Вернулись к выбору контакта. Укажите контакт:", reply_markup=contact_keyboard())
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
                f"📍 Адрес: {data['address']}\n"
                f"📱 Контакт: {data['contact']}\n"
                f"💬 Комментарий: {data.get('comment','—')}"
            )
            # Уменьшаем stock для каждого выбранного вкуса
            for o in cart:
                cat = o['category']
                for itm in menu[cat]["flavors"]:
                    if itm['flavor'] == o['flavor']:
                        itm['stock'] = max(itm.get('stock', 1) - 1, 0)
                        break
            save_menu(menu)

            # Сброс данных заказа в user_data
            data['cart'] = []
            data['current_category'] = None
            data['wait_for_address'] = False
            data['wait_for_contact'] = False
            data['wait_for_comment'] = False
            data.pop('comment', None)
            data.pop('address', None)
            data.pop('contact', None)

            kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
            kb.add("🛒 Оформить новый заказ")
            bot.send_message(cid, "Ваш заказ принят! Спасибо.", reply_markup=kb)
            bot.send_message(GROUP_CHAT_ID, full)
            bot.send_message(PERSONAL_CHAT_ID, "[Копия заказа]\n\n" + full)
            return

    # — Обычный сценарий заказа — (если не в стадии адрес/контакт/комментарий) —

    # «Назад» к выбору категории
    if text == "⬅️ Назад":
        data['current_category'] = None
        bot.send_message(cid, "Выберите категорию:", reply_markup=get_main_keyboard())
        return

    # Очистить корзину
    if text == "🗑️ Очистить корзину":
        data['cart'].clear()
        data['current_category'] = None
        data['wait_for_address'] = False
        data['wait_for_contact'] = False
        data['wait_for_comment'] = False
        bot.send_message(cid, "Корзина очищена.", reply_markup=get_main_keyboard())
        return

    # Добавить ещё (к корзине)
    if text == "➕ Добавить ещё":
        data['current_category'] = None
        bot.send_message(cid, "Выберите категорию:", reply_markup=get_main_keyboard())
        return

    # Завершить заказ → запрос адреса
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

    # Выбор категории для заказа
    if text in menu:
        data['current_category'] = text
        bot.send_message(cid, f"Выберите вкус ({text}):", reply_markup=get_flavors_keyboard(text))
        return

    # Выбор вкуса в категории
    cat = data.get('current_category')
    if cat:
        category_price = menu[cat]["price"]
        for it in menu[cat]["flavors"]:
            emoji = it.get("emoji", "").strip()
            flavor = it["flavor"]
            stock = it.get("stock", 0)
            if emoji:
                label = f"{emoji} {flavor} ({category_price}₺) [{stock} шт]"
            else:
                label = f"{flavor} ({category_price}₺) [{stock} шт]"

            # Сравниваем текст сообщения с текущей «меткой» вкуса
            if text == label and stock > 0:
                data['cart'].append({
                    'category': cat,
                    'emoji':    emoji,
                    'flavor':   flavor,
                    'price':    category_price
                })
                kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
                kb.add("➕ Добавить ещё", "✅ Завершить заказ", "🗑️ Очистить корзину")
                bot.send_message(cid, f"{flavor} добавлен в корзину.", reply_markup=kb)
                return
        # Если не совпало ни с одной «меткой», просто перепоказываем клавиатуру вкусов
        bot.send_message(cid, "Пожалуйста, выберите вкус из списка:", reply_markup=get_flavors_keyboard(cat))
        return

if __name__ == "__main__":
    bot.polling()
