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

# ------------------------------------------------------------------------
#   1. Загрузка переменных окружения
# ------------------------------------------------------------------------
TOKEN = os.getenv("TOKEN")
if not TOKEN:
    raise RuntimeError(
        "Environment variable TOKEN is not set! "
        "Run the container with -e TOKEN=<your_token>."
    )
ADMIN_ID = int(os.getenv("ADMIN_ID", "424751188"))
GROUP_CHAT_ID = int(os.getenv("GROUP_CHAT_ID", "0"))
PERSONAL_CHAT_ID = int(os.getenv("PERSONAL_CHAT_ID", "0"))

bot = telebot.TeleBot(TOKEN, parse_mode="HTML")

# ------------------------------------------------------------------------
#   2. Пути к JSON-файлам и БД
# ------------------------------------------------------------------------
MENU_PATH = "/data/menu.json"
LANG_PATH = "/data/languages.json"
DB_PATH = "/data/database.db"

# ------------------------------------------------------------------------
#   3. Подключение к БД
# ------------------------------------------------------------------------
def get_db_connection():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    return conn

# ------------------------------------------------------------------------
#   4. Инициализация БД
# ------------------------------------------------------------------------
conn_init = get_db_connection()
cursor_init = conn_init.cursor()
cursor_init.execute("""
    CREATE TABLE IF NOT EXISTS users (
        chat_id        INTEGER PRIMARY KEY,
        points         INTEGER DEFAULT 0,
        referral_code  TEXT UNIQUE,
        referred_by    INTEGER
    )
""")
cursor_init.execute("""
    CREATE TABLE IF NOT EXISTS orders (
        order_id    INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id     INTEGER,
        items_json  TEXT,
        total       INTEGER,
        timestamp   TEXT
    )
""")
cursor_init.execute("""
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
conn_init.commit()
cursor_init.close()
conn_init.close()

# ------------------------------------------------------------------------
#   5. Загрузка menu.json и languages.json
# ------------------------------------------------------------------------
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

# ------------------------------------------------------------------------
#   6. In-memory хранилище состояния пользователей
# ------------------------------------------------------------------------
user_data = {}

# ------------------------------------------------------------------------
#   7. Утилиты перевода
# ------------------------------------------------------------------------
def t(chat_id: int, key: str) -> str:
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
                return {k: rates[k] for k in ("RUB","USD","UAH") if k in rates}
        except:
            continue
    return {"RUB":0,"USD":0,"UAH":0}

# ------------------------------------------------------------------------
#   8–11. Клавиатуры (language, main menu, flavors, address/contact/comment)
# ------------------------------------------------------------------------
def get_inline_language_buttons(chat_id:int)->types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        types.InlineKeyboardButton("Русский 🇷🇺", callback_data="set_lang|ru"),
        types.InlineKeyboardButton("English 🇬🇧", callback_data="set_lang|en")
    )
    return kb

def get_inline_main_menu(chat_id:int)->types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup(row_width=2)
    lang = user_data.get(chat_id,{}).get("lang") or "ru"
    for cat in menu:
        stock = sum(i.get("stock",0) for i in menu[cat]["flavors"])
        label = f"{cat} ({'out of stock' if lang=='en' else 'нет в наличии'})" if stock==0 else cat
        kb.add(types.InlineKeyboardButton(label, callback_data=f"category|{cat}"))
    kb.add(
        types.InlineKeyboardButton(f"🛒 {t(chat_id,'view_cart')}", "view_cart"),
        types.InlineKeyboardButton(f"🗑️ {t(chat_id,'clear_cart')}", "clear_cart"),
        types.InlineKeyboardButton(f"✅ {t(chat_id,'finish_order')}", "finish_order"),
    )
    return kb

def get_inline_flavors(chat_id:int, cat:str)->types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup(row_width=1)
    price = menu[cat]["price"]
    for it in menu[cat]["flavors"]:
        stock = it.get("stock",0)
        if isinstance(stock,str) and stock.isdigit():
            stock = int(stock); it["stock"]=stock
        if stock>0:
            kb.add(types.InlineKeyboardButton(
                f"{it.get('emoji','')} {it['flavor']} — {price}₺ [{stock}шт]",
                callback_data=f"flavor|{cat}|{it['flavor']}"
            ))
    kb.add(types.InlineKeyboardButton(f"⬅️ {t(chat_id,'back_to_categories')}", "go_back_to_categories"))
    return kb

def address_keyboard() -> types.ReplyKeyboardMarkup:
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
    kb.add(types.KeyboardButton(t(None,"share_location"), request_location=True))
    kb.add(t(None,"choose_on_map"), t(None,"enter_address_text"), t(None,"back"))
    return kb

def contact_keyboard() -> types.ReplyKeyboardMarkup:
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
    kb.add(types.KeyboardButton(t(None,"share_contact"), request_contact=True))
    kb.add(t(None,"enter_nickname"), t(None,"back"))
    return kb

def comment_keyboard() -> types.ReplyKeyboardMarkup:
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
    kb.add(t(None,"enter_comment"), t(None,"send_order"), t(None,"back"))
    return kb

# ------------------------------------------------------------------------
#   12. Меню редактирования (на англ.)
# ------------------------------------------------------------------------
def edit_action_keyboard() -> types.ReplyKeyboardMarkup:
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
    kb.add("➕ Add Category","➖ Remove Category")
    kb.add("💲 Fix Price","ALL IN","🔄 Actual Flavor")
    kb.add("🖼️ Add Category Picture","Set Category Flavor to 0")
    kb.add("⬅️ Back","❌ Cancel")
    return kb

# ------------------------------------------------------------------------
#   13. Планировщик (дайджест)
# ------------------------------------------------------------------------
def send_weekly_digest():
    conn = get_db_connection(); cur = conn.cursor()
    week = (datetime.datetime.utcnow() - datetime.timedelta(days=7)).isoformat()
    cur.execute("SELECT items_json FROM orders WHERE timestamp>=?",(week,))
    rows = cur.fetchall(); counts={}
    for (ij,) in rows:
        for it in json.loads(ij):
            counts[it["flavor"]] = counts.get(it["flavor"],0)+1
    top3 = sorted(counts.items(), key=lambda x:x[1], reverse=True)[:3]
    text = "📢 No sales in the past week." if not top3 else "📢 Top-3 flavors this week:\n" + "\n".join(f"{f}: {q} sold" for f,q in top3)
    cur.execute("SELECT DISTINCT chat_id FROM orders")
    for (uid,) in cur.fetchall():
        bot.send_message(uid, text)
    cur.close(); conn.close()

scheduler = BackgroundScheduler(timezone="Europe/Riga")
scheduler.add_job(send_weekly_digest, trigger="cron", day_of_week="mon", hour=9, minute=0)
scheduler.start()

# ------------------------------------------------------------------------
#   14–23. /start, язык, категории, вкусы, добавление в корзину, просмотр,
#           удаление, редактирование количества (с учётом локации/контакта)
# ------------------------------------------------------------------------
# (Весь код точно такой же, как у вас, за исключением того, что после сохранения заказа
#  мы добавим отправку в PERSONAL_CHAT_ID с кнопкой "Отменить заказ".)
# ------------------------------------------------------------------------

# ... (ваши неизменённые хендлеры до handle_comment_input) ...

# ------------------------------------------------------------------------
#   28. Handler: ввод комментария и сохранение заказа (с учётом списания stock)
# ------------------------------------------------------------------------
@bot.message_handler(
    func=lambda m: user_data.get(m.chat.id,{}).get("wait_for_comment"),
    content_types=['text']
)
def handle_comment_input(message):
    chat_id = message.chat.id
    data = user_data.get(chat_id,{})
    text = message.text or ""

    # логика BACK / COMMENT как у вас...
    # когда дошли до отправки заказа:
    if text == t(None,"send_order"):
        cart = data.get('cart',[])
        # ваша валидация наличия и списания stock...

        # Списываем stock
        needed = {}
        for it in cart:
            needed[(it["category"],it["flavor"])] = needed.get((it["category"],it["flavor"]),0)+1
        for (cat0,flavor0),qty in needed.items():
            for itm in menu[cat0]["flavors"]:
                if itm["flavor"]==flavor0:
                    itm["stock"] -= qty
                    break
        with open(MENU_PATH,"w",encoding="utf-8") as f:
            json.dump(menu,f,ensure_ascii=False,indent=2)

        items_json = json.dumps(cart, ensure_ascii=False)
        now = datetime.datetime.utcnow().isoformat()
        conn = get_db_connection(); cur = conn.cursor()
        cur.execute(
            "INSERT INTO orders (chat_id, items_json, total, timestamp) VALUES (?,?,?,?)",
            (chat_id, items_json, sum(i['price'] for i in cart), now)
        )
        order_id = cur.lastrowid
        conn.commit()

        # Начисляем баллы
        total = sum(i['price'] for i in cart)
        earned = total // 30
        if earned>0:
            cur.execute("UPDATE users SET points=points+? WHERE chat_id=?",(earned,chat_id))
            conn.commit()
            bot.send_message(chat_id, f"👍 Вы получили {earned} бонусных баллов за этот заказ.")

        cur.close(); conn.close()

        # Собираем summary
        summary = "\n".join(f"{i['category']}: {i['flavor']} — {i['price']}₺" for i in cart)

        # Отправляем в PERSONAL_CHAT_ID с кнопкой Отменить
        kb = types.InlineKeyboardMarkup()
        kb.add(types.InlineKeyboardButton("❌ Отменить заказ", callback_data=f"cancel_order|{order_id}"))
        bot.send_message(
            PERSONAL_CHAT_ID,
            f"📥 Новый заказ #{order_id} от @{message.from_user.username or message.from_user.first_name}:\n\n"
            f"{summary}\n\n"
            f"Итого: {total}₺",
            reply_markup=kb
        )

        # оставшаяся отправка в GROUP_CHAT_ID и клиенту...
        data.update({"cart":[],"wait_for_comment":False})
        user_data[chat_id] = data
        return

# ------------------------------------------------------------------------
#   Новый хендлер: отмена заказа по кнопке
# ------------------------------------------------------------------------
@bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("cancel_order|"))
def handle_cancel_order(call):
    call.answer("Отмена заказа...")
    _, oid = call.data.split("|",1)
    order_id = int(oid)

    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("SELECT chat_id, items_json, total FROM orders WHERE order_id=?", (order_id,))
    row = cur.fetchone()
    if not row:
        bot.send_message(call.from_user.id, "⚠️ Заказ не найден или уже отменён.")
        cur.close(); conn.close()
        return

    user_chat, ij, total = row
    items = json.loads(ij)

    # Восстанавливаем stock
    for it in items:
        for itm in menu[it['category']]['flavors']:
            if itm['flavor']==it['flavor']:
                itm['stock'] += 1
                break
    with open(MENU_PATH,"w",encoding="utf-8") as f:
        json.dump(menu,f,ensure_ascii=False,indent=2)

    # Списываем начисленные бонусы
    earned = total // 30
    if earned>0:
        cur.execute("UPDATE users SET points=points-? WHERE chat_id=?", (earned, user_chat))
        conn.commit()

    # Удаляем запись заказа
    cur.execute("DELETE FROM orders WHERE order_id=?", (order_id,))
    conn.commit()

    cur.close(); conn.close()

    # Убираем кнопку из сообщения
    bot.edit_message_reply_markup(
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        reply_markup=None
    )
    bot.send_message(call.from_user.id, f"✅ Заказ #{order_id} отменён. Начисленные {earned} баллов списаны, товар возвращён на склад.")

# ------------------------------------------------------------------------
#   36. Запуск
# ------------------------------------------------------------------------
if __name__ == "__main__":
    bot.delete_webhook()
    bot.polling(none_stop=True)
