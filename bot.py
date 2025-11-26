import os
import json
import requests
import datetime
import random
import re
import string
import sqlite3
import pytz

from apscheduler.schedulers.background import BackgroundScheduler
from telebot import TeleBot, types

def _normalize(text: str) -> str:
    """
    Убирает эмодзи и любые спецсимволы, заменяя их на пробел,
    сводит к нижнему регистру и склеивает повторяющиеся пробелы.
    """
    # всё, что не буква/цифра → пробел
    cleaned = re.sub(r'[^0-9A-Za-zА-Яа-я]+', ' ', text)
    # убрать «лишние» пробелы и привести к lower
    return re.sub(r'\s+', ' ', cleaned).strip().lower()

# ------------------------------------------------------------------------
#   1. Загрузка переменных окружения и инициализация бота
# ------------------------------------------------------------------------
TOKEN = os.getenv("TOKEN")
if not TOKEN:
    raise RuntimeError(
        "Environment variable TOKEN is not set! "
        "Run the container with -e TOKEN=<your_token>."
    )

ADMIN_ID      = int(os.getenv("ADMIN_ID",      "424751188"))
ADMIN_ID_TWO  = int(os.getenv("ADMIN_ID_TWO",  "748250885"))
ADMIN_ID_THREE= int(os.getenv("ADMIN_ID_THREE","6492697568"))
ADMINS        = {ADMIN_ID, ADMIN_ID_TWO, ADMIN_ID_THREE}

GROUP_CHAT_ID    = int(os.getenv("GROUP_CHAT_ID",    "-1002414380144"))
PERSONAL_CHAT_ID = int(os.getenv("PERSONAL_CHAT_ID", "0"))

print("GROUP_CHAT_ID =", GROUP_CHAT_ID)

bot = TeleBot(TOKEN, parse_mode="HTML")

# ------------------------------------------------------------------------
#   2. Пути к JSON-файлам и БД (персистентный том /data)
# ------------------------------------------------------------------------
MENU_PATH = "/data/menu.json"
LANG_PATH = "/data/languages.json"
DB_PATH = "/data/database.db"
# ------------------------------------------------------------------------
#   3. Функция для получения локального подключения к БД
# ------------------------------------------------------------------------
def get_db_connection():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    return conn

# ------------------------------------------------------------------------

# ------------------------------------------------------------------------
# ------------------------------------------------------------------------
#   4. Инициализация SQLite и создание таблиц (при старте)
# ------------------------------------------------------------------------


conn_init = get_db_connection()
cursor_init = conn_init.cursor()

# лог всех нажатий "Order Delivered"
cursor_init.execute("""
    CREATE TABLE IF NOT EXISTS delivered_log (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        order_id   INTEGER,
        currency   TEXT,
        qty        INTEGER,
        timestamp  TEXT
    )
""")
conn_init.commit()

#   Инициализация таблицы для хранения счётчиков доставленных товаров
# ------------------------------------------------------------------------
cursor_init.execute("""
    CREATE TABLE IF NOT EXISTS delivered_counts (
        currency TEXT PRIMARY KEY,
        count    INTEGER DEFAULT 0
    )
""")
conn_init.commit()

# Попытка добавить новые столбцы — выполнится только один раз
try:
    cursor_init.execute("ALTER TABLE orders ADD COLUMN points_spent  INTEGER DEFAULT 0")
    cursor_init.execute("ALTER TABLE orders ADD COLUMN points_earned INTEGER DEFAULT 0")
    conn_init.commit()
except sqlite3.OperationalError:
    # Если столбцы уже существуют — просто пропускаем
    pass

# Создание таблицы users
cursor_init.execute("""
    CREATE TABLE IF NOT EXISTS users (
        chat_id        INTEGER PRIMARY KEY,
        points         INTEGER DEFAULT 0,
        referral_code  TEXT UNIQUE,
        referred_by    INTEGER
    )
""")

# Создание таблицы orders (с новыми полями уже учтёнными через ALTER)
cursor_init.execute("""
    CREATE TABLE IF NOT EXISTS orders (
        order_id       INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id        INTEGER,
        items_json     TEXT,
        total          INTEGER,
        timestamp      TEXT,
        points_spent   INTEGER DEFAULT 0,
        points_earned  INTEGER DEFAULT 0
    )
""")

# Создание таблицы reviews
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

# 0. Убедимся, что у пользователя всегда есть запись в user_data, новое добавленное
def init_user(chat_id: int):
    if chat_id not in user_data:
        user_data[chat_id] = {
            "lang": None,
            "cart": [],
            "current_category": None,
            "wait_for_points": False,
            "wait_for_address": False,
            "wait_for_contact": False,
            "wait_for_comment": False,
            "address": "",
            "contact": "",
            "comment": "",
            "pending_discount": 0,
            "pending_points_spent": 0,
            "temp_total_try": 0,
            "temp_user_points": 0,
            "edit_phase": None,
            "edit_cat": None,
            "edit_flavor": None,
            "edit_index": None,
            "edit_cart_phase": None,
            "awaiting_review_flavor": None,
            "awaiting_review_rating": False,
            "awaiting_review_comment": False,
            "temp_review_flavor": None,
            "temp_review_rating": 0
        }

# ------------------------------------------------------------------------
#   6. Хранилище данных пользователей (in-memory)
# ------------------------------------------------------------------------
user_data = {}  # структура объяснялась ранее
# 6.2 Декоратор для гарантированной инициализации
def ensure_user(handler):
    def wrapper(message_or_call, *args, **kwargs):
        # для Message и CallbackQuery chat_id берём по-разному:
        if hasattr(message_or_call, "from_user"):
            cid = message_or_call.from_user.id
        else:
            cid = message_or_call.chat.id
        init_user(cid)
        return handler(message_or_call, *args, **kwargs)

    return wrapper
def push_state(chat_id: int, state: str):
    """Пушит текущее имя шага в стек."""
    stack = user_data[chat_id].setdefault("state_stack", [])
    stack.append(state)

def pop_state(chat_id: int) -> str | None:
    """Удаляет текущее состояние и возвращает предыдущее."""
    stack = user_data[chat_id].get("state_stack", [])
    if not stack:
        return None
    stack.pop()
    return stack[-1] if stack else None

# ------------------------------------------------------------------------
#   7. Утилиты
# ------------------------------------------------------------------------
import time

def t(chat_id: int, key: str) -> str:
    """
    Возвращает перевод из languages.json по ключу.
    Если перевод не найден — возвращает сам ключ.
    """
    lang = user_data.get(chat_id, {}).get("lang") or "ru"
    return translations.get(lang, {}).get(key, key)


def generate_ref_code(length: int = 6) -> str:
    """
    Генерирует случайный реферальный код из заглавных букв и цифр.
    """
    return "".join(random.choices(string.ascii_uppercase + string.digits, k=length))

# ─── Кешированные курсы валют ───────────────────────────────────────────────
_RATE_CACHE: dict[str, float] | None = None
_RATE_CACHE_TS: float = 0.0
_RATE_TTL: int = 10 * 60  # 10 минут

def fetch_rates() -> dict[str, float]:
    """
    Возвращает курсы валют TRY → RUB, USD, UAH, EUR,
    кешируя результат на _RATE_TTL секунд.
    """
    global _RATE_CACHE, _RATE_CACHE_TS

    now = time.time()
    # Если кеш ещё «жив» — отдаём его
    if _RATE_CACHE is not None and (now - _RATE_CACHE_TS) < _RATE_TTL:
        return _RATE_CACHE

    # Иначе запрашиваем из внешних источников
    sources = [
        ("https://api.exchangerate.host/latest", {"base": "TRY", "symbols": "RUB,USD,UAH,EUR"}),
        ("https://open.er-api.com/v6/latest/TRY", {})
    ]
    for url, params in sources:
        try:
            r = requests.get(url, params=params, timeout=5)
            data = r.json()
            rates = data.get("rates") or data.get("conversion_rates")
            if rates:
                result = {k: rates[k] for k in ("RUB", "USD", "UAH", "EUR") if k in rates}
                _RATE_CACHE = result
                _RATE_CACHE_TS = now
                return result
        except Exception:
            continue

    # Фоллбэк при ошибке
    fallback = {"RUB": 0, "USD": 0, "EUR": 0, "UAH": 0}
    _RATE_CACHE = fallback
    _RATE_CACHE_TS = now
    return fallback

def translate_to_en(text: str) -> str:
    """
    Переводит русский текст на английский через Google Translate API.
    Если что-то пошло не так — возвращает исходный текст.
    """
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
        # отправка POST вместо GET — так передаётся весь текст
        res = requests.post(base_url, data=params, timeout=10)
        data = res.json()
        # data[0] — список сегментов, каждый seg[0] содержит часть перевода
        return "".join(seg[0] for seg in data[0])
    except Exception:
        return text

# ------------------------------------------------------------------------
#   8. Inline-кнопки для выбора языка
# ------------------------------------------------------------------------
def get_inline_language_buttons(chat_id: int) -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        types.InlineKeyboardButton(text="Русский 🇷🇺", callback_data="set_lang|ru"),
        types.InlineKeyboardButton(text="English 🇬🇧", callback_data="set_lang|en")
    )
    return kb

# ------------------------------------------------------------------------
#   9. Inline-кнопки для главного меню
# ------------------------------------------------------------------------
def get_inline_main_menu(chat_id: int) -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup(row_width=2)
    lang = user_data.get(chat_id, {}).get("lang") or "ru"

    # Категории
    for cat in menu:
        total_stock = sum(item.get("stock", 0) for item in menu[cat]["flavors"])
        label = f"{cat} (out of stock)" if total_stock == 0 and lang == "en" \
                else f"{cat} (нет в наличии)" if total_stock == 0 \
                else cat
        kb.add(types.InlineKeyboardButton(text=label, callback_data=f"category|{cat}"))

    # Кнопки корзины и дальнейших действий — только если в корзине есть товары
    cart_count = len(user_data.get(chat_id, {}).get("cart", []))
    if cart_count > 0:
        # «Посмотреть корзину» с количеством
        kb.add(types.InlineKeyboardButton(
            text=f"🛒 {t(chat_id, 'view_cart')} ({cart_count})",
            callback_data="view_cart"
        ))
        # «Очистить» и «Завершить»
        kb.add(types.InlineKeyboardButton(
            text=f"🗑️ {t(chat_id, 'clear_cart')}",
            callback_data="clear_cart"
        ))
        kb.add(types.InlineKeyboardButton(
            text=f"✅ {t(chat_id, 'finish_order')}",
            callback_data="finish_order"
        ))

    return kb
# ------------------------------------------------------------------------
#   10. Inline-кнопки для выбора вкусов
# ------------------------------------------------------------------------
def get_inline_flavors(chat_id: int, cat: str) -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup(row_width=1)
    price = menu[cat]["price"]

    # Сохраняем в user_data текущий список «живых» вкусов
    user_data[chat_id]["current_flavors"] = [
        item for item in menu[cat]["flavors"]
        if int(item.get("stock", 0)) > 0
    ]

    for idx, item in enumerate(user_data[chat_id]["current_flavors"]):
        emoji  = item.get("emoji", "")
        flavor = item["flavor"]
        stock  = int(item.get("stock", 0))
        # Берём средний рейтинг из menu.json, если он есть
        rating = item.get("rating")
        rating_str = f" ⭐{rating}" if rating else ""
        label  = f"{emoji} {flavor}{rating_str} — {price}₺ [{stock} шт]"
        kb.add(types.InlineKeyboardButton(
            text=label,
            callback_data=f"flavor|{idx}"
        ))

    kb.add(types.InlineKeyboardButton(
        text=f"⬅️ {t(chat_id, 'back_to_categories')}",
        callback_data="go_back_to_categories"
    ))
    return kb

# ------------------------------------------------------------------------
#   11. Reply-клавиатуры (альтернатива inline)
# ------------------------------------------------------------------------
def address_keyboard(chat_id: int) -> types.ReplyKeyboardMarkup:
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
    kb.add(types.KeyboardButton(t(chat_id, "share_location"), request_location=True))
    kb.add(t(chat_id, "choose_on_map"))
    kb.add(t(chat_id, "enter_address_text"))
    kb.add(t(chat_id, "back"))
    return kb



def contact_keyboard(chat_id: int) -> types.ReplyKeyboardMarkup:
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
    kb.add(types.KeyboardButton(t(chat_id, "share_contact"), request_contact=True))
    kb.add(t(chat_id, "enter_nickname"))
    kb.add(t(chat_id, "back"))
    return kb



def comment_keyboard(chat_id: int) -> types.ReplyKeyboardMarkup:
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
    kb.add(t(chat_id, "enter_comment"))
    kb.add(t(chat_id, "send_order"))
    kb.add(t(chat_id, "back"))
    return kb

# ------------------------------------------------------------------------
#   12. Клавиатура редактирования меню (/change) — ВСЁ НА АНГЛИЙСКОМ
# ------------------------------------------------------------------------
def edit_action_keyboard() -> types.ReplyKeyboardMarkup:
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
    kb.add("➕ Add Category", "➖ Remove Category", "✏️ Rename Category")
    kb.add("💲 Fix Price", "ALL IN", "🔄 Actual Flavor")
    kb.add("🖼️ Add Category Picture", "Set Category Flavor to 0")
    kb.add("📦 New Supply")  # новая кнопка
    kb.add("⬅️ Back", "❌ Cancel")
    return kb

# ------------------------------------------------------------------------
#   13. Планировщик – еженедельный дайджест (необязательно)
# ------------------------------------------------------------------------
def send_weekly_digest():
    conn = get_db_connection()
    cursor = conn.cursor()

    # Собираем заказы за последние 7 дней
    one_week_ago = (datetime.datetime.utcnow() - datetime.timedelta(days=7)).isoformat()
    cursor.execute("SELECT items_json FROM orders WHERE timestamp >= ?", (one_week_ago,))
    recent = cursor.fetchall()

    # Считаем количество продаж по вкусам
    counts = {}
    for (items_json,) in recent:
        items = json.loads(items_json)
        for i in items:
            counts[i["flavor"]] = counts.get(i["flavor"], 0) + 1

    # Берём топ-3
    top3 = sorted(counts.items(), key=lambda x: x[1], reverse=True)[:3]

    # Формируем текст на русском
    if not top3:
        text = "📢 За прошлую неделю не было продаж."
    else:
        lines = [f"{flavor}: {qty} шт." for flavor, qty in top3]
        text = "📢 Топ-3 вкуса за неделю:\n" + "\n".join(lines)

    # Рассылаем всем зарегистрированным пользователям
    cursor.execute("SELECT chat_id FROM users")
    for (uid,) in cursor.fetchall():
        try:
            bot.send_message(uid, text)
        except Exception as e:
            print(f"Не удалось отправить дайджест пользователю {uid}: {e}")

    cursor.close()
    conn.close()

# ------------------------------------------------------------------------
#   14. Хендлер /start – регистрация, реферальная система, выбор языка
# ------------------------------------------------------------------------
@ensure_user
@bot.message_handler(commands=['start'])
def cmd_start(message):
    chat_id = message.chat.id

    if chat_id not in user_data:
        user_data[chat_id] = {
            "lang": None,
            "cart": [],
            "current_category": None,
            "wait_for_points": False,
            "wait_for_address": False,
            "wait_for_contact": False,
            "wait_for_comment": False,
            "address": "",
            "contact": "",
            "comment": "",
            "pending_discount": 0,
            "pending_points_spent": 0,
            "temp_total_try": 0,
            "temp_user_points": 0,
            "edit_phase": None,
            "edit_cat": None,
            "edit_flavor": None,
            "edit_index": None,
            "edit_cart_phase": None,
            "awaiting_review_flavor": None,
            "awaiting_review_rating": False,
            "awaiting_review_comment": False,
            "temp_review_flavor": None,
            "temp_review_rating": 0
        }
    data = user_data[chat_id]

    # Сбрасываем всё, кроме lang
    data.update({
        "cart": [],
        "current_category": None,
        "wait_for_points": False,
        "wait_for_address": False,
        "wait_for_contact": False,
        "wait_for_comment": False,
        "address": "",
        "contact": "",
        "comment": "",
        "pending_discount": 0,
        "pending_points_spent": 0,
        "temp_total_try": 0,
        "temp_user_points": 0,
        "edit_phase": None,
        "edit_cat": None,
        "edit_flavor": None,
        "edit_index": None,
        "edit_cart_phase": None,
        "awaiting_review_flavor": None,
        "awaiting_review_rating": False,
        "awaiting_review_comment": False,
        "temp_review_flavor": None,
        "temp_review_rating": 0
    })

    conn_local = get_db_connection()
    cursor_local = conn_local.cursor()
    cursor_local.execute("SELECT chat_id FROM users WHERE chat_id = ?", (chat_id,))
    if cursor_local.fetchone() is None:
        text = message.text or ""
        referred_by = None
        if "ref=" in text:
            code = text.split("ref=")[1]
            cursor_local.execute("SELECT chat_id FROM users WHERE referral_code = ?", (code,))
            row = cursor_local.fetchone()
            if row:
                referred_by = row[0]
        new_code = generate_ref_code()
        while True:
            cursor_local.execute("SELECT referral_code FROM users WHERE referral_code = ?", (new_code,))
            if cursor_local.fetchone() is None:
                break
            new_code = generate_ref_code()
        cursor_local.execute(
            "INSERT INTO users (chat_id, points, referral_code, referred_by) VALUES (?, ?, ?, ?)",
            (chat_id, 0, new_code, referred_by)
        )
        conn_local.commit()
    cursor_local.close()
    conn_local.close()

    bot.send_message(
        chat_id,
        t(chat_id, "choose_language"),
        reply_markup=get_inline_language_buttons(chat_id)
    )


# ------------------------------------------------------------------------
#   15. Callback: выбор языка
# ------------------------------------------------------------------------
@ensure_user
@bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("set_lang|"))
def handle_set_lang(call):
    chat_id = call.from_user.id
    _, lang_code = call.data.split("|", 1)

    if chat_id not in user_data:
        user_data[chat_id] = {
            "lang": lang_code,
            "cart": [],
            "current_category": None,
            "wait_for_points": False,
            "wait_for_address": False,
            "wait_for_contact": False,
            "wait_for_comment": False,
            "address": "",
            "contact": "",
            "comment": "",
            "pending_discount": 0,
            "pending_points_spent": 0,
            "temp_total_try": 0,
            "temp_user_points": 0,
            "edit_phase": None,
            "edit_cat": None,
            "edit_flavor": None,
            "edit_index": None,
            "edit_cart_phase": None,
            "awaiting_review_flavor": None,
            "awaiting_review_rating": False,
            "awaiting_review_comment": False,
            "temp_review_flavor": None,
            "temp_review_rating": 0
        }
    else:
        user_data[chat_id]["lang"] = lang_code

    bot.answer_callback_query(call.id, t(chat_id, "lang_set"))
    bot.send_message(chat_id, t(chat_id, "choose_category"), reply_markup=get_inline_main_menu(chat_id))

    conn_local = get_db_connection()
    cursor_local = conn_local.cursor()
    cursor_local.execute("SELECT referral_code FROM users WHERE chat_id = ?", (chat_id,))
    row = cursor_local.fetchone()
    cursor_local.close()
    conn_local.close()

    if row:
        code = row[0]
        bot_username = bot.get_me().username
        ref_link = f"https://t.me/{bot_username}?start=ref={code}"
        if user_data[chat_id]["lang"] == "en":
            bot.send_message(
                chat_id,
                f"Your referral code: {code}\nShare this link with friends:\n{ref_link}"
            )
        else:
            bot.send_message(
                chat_id,
                f"Зарабатывайте баллы! Ваш реферальный код: {code}\nПоделитесь этой ссылкой с друзьями:\n{ref_link}"
            )


# ------------------------------------------------------------------------
#   16. Callback: выбор категории (показываем вкусы)
# ------------------------------------------------------------------------
@ensure_user
@bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("category|"))
def handle_category(call):
    chat_id = call.from_user.id
    _, cat = call.data.split("|", 1)

    if cat not in menu:
        return bot.answer_callback_query(call.id, t(chat_id, "error_invalid"), show_alert=True)

    bot.answer_callback_query(call.id)
    user_data[chat_id]["current_category"] = cat

    photo_url = menu[cat].get("photo_url", "").strip()
    if photo_url:
        try:
            bot.send_photo(chat_id, photo_url)
        except Exception as e:
            print(f"Failed to send category photo for {cat}: {e}")

    # Генерируем клавиатуру уже с индексами вкусов
    kb = get_inline_flavors(chat_id, cat)

    bot.send_message(
        chat_id,
        f"{t(chat_id, 'choose_flavor')} «{cat}»",
        reply_markup=kb
    )

# ------------------------------------------------------------------------
#   17. Callback: «Назад к категориям»
# ------------------------------------------------------------------------
@ensure_user
@bot.callback_query_handler(func=lambda call: call.data == "go_back_to_categories")
def handle_go_back_to_categories(call):
    chat_id = call.from_user.id
    bot.answer_callback_query(call.id)
    bot.send_message(chat_id, t(chat_id, "choose_category"), reply_markup=get_inline_main_menu(chat_id))


# ------------------------------------------------------------------------
#   18. Callback: выбор вкуса
# ------------------------------------------------------------------------
@ensure_user
@bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("flavor|"))
def handle_flavor(call):
    chat_id = call.from_user.id
    _, idx_str = call.data.split("|", 1)
    try:
        idx = int(idx_str)
        cat = user_data[chat_id]["current_category"]
        item = user_data[chat_id]["current_flavors"][idx]
    except (ValueError, KeyError, IndexError):
        return bot.answer_callback_query(call.id, t(chat_id, "error_invalid"), show_alert=True)

    flavor = item["flavor"]
    price  = menu[cat]["price"]
    bot.answer_callback_query(call.id)

    desc = item.get(f"description_{user_data[chat_id]['lang']}", "")
    caption = f"<b>{flavor}</b> — {cat}\n{desc}\n📌 {price}₺" if desc else f"<b>{flavor}</b> — {cat}\n📌 {price}₺"

    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(
        types.InlineKeyboardButton(
            text=f"➕ {t(chat_id, 'add_to_cart')}",
            callback_data=f"add_to_cart|{idx}"
        ),
        types.InlineKeyboardButton(
            text=f"⬅️ {t(chat_id, 'back_to_categories')}",
            callback_data="go_back_to_categories"
        )
    )
    if user_data[chat_id]["cart"]:
        kb.add(types.InlineKeyboardButton(
            text=f"✅ {t(chat_id, 'finish_order')}",
            callback_data="finish_order"
        ))

    bot.send_message(chat_id, caption, parse_mode="HTML", reply_markup=kb)

# ------------------------------------------------------------------------
#   19. Callback: добавить в корзину (без изменения stock)
# ------------------------------------------------------------------------
@ensure_user
@bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("add_to_cart|"))
def handle_add_to_cart(call):
    chat_id = call.from_user.id
    _, idx_str = call.data.split("|", 1)

    # парсим индекс
    try:
        idx = int(idx_str)
        cat = user_data[chat_id]["current_category"]
        item = user_data[chat_id]["current_flavors"][idx]
    except (ValueError, KeyError, IndexError):
        # неверные данные — просто игнорируем
        return bot.answer_callback_query(call.id, t(chat_id, "error_invalid"), show_alert=True)

    # проверяем наличие
    if int(item.get("stock", 0)) <= 0:
        return bot.answer_callback_query(call.id, t(chat_id, "error_out_of_stock"), show_alert=True)

    bot.answer_callback_query(call.id)

    # добавляем в корзину
    data = user_data.setdefault(chat_id, {})
    cart = data.setdefault("cart", [])
    price = menu[cat]["price"]
    cart.append({
        "category": cat,
        "flavor": item["flavor"],
        "price": price
    })

    # отправляем подтверждение
    template = t(chat_id, "added_to_cart")
    suffix = template.split("»", 1)[1].strip()
    count = len(cart)
    bot.send_message(
        chat_id,
        f"«{cat} — {item['flavor']}» {suffix.format(flavor=item['flavor'], count=count)}",
        reply_markup=get_inline_main_menu(chat_id)
    )



# ------------------------------------------------------------------------
#   20. Callback: «Просмотр корзины»
# ------------------------------------------------------------------------
@ensure_user
@bot.callback_query_handler(func=lambda call: call.data == "view_cart")
def handle_view_cart(call):
    chat_id = call.from_user.id
    bot.answer_callback_query(call.id)
    data = user_data.get(chat_id, {})
    cart = data.get("cart", [])
    if not cart:
        bot.send_message(chat_id, t(chat_id, "cart_empty"), reply_markup=get_inline_main_menu(chat_id))
        return

    grouped = {}
    for item in cart:
        key = (item["category"], item["flavor"], item["price"])
        grouped[key] = grouped.get(key, 0) + 1

    text_lines = [f"🛒 {t(chat_id, 'view_cart')}:"]

    for idx, ((cat, flavor, price), qty) in enumerate(grouped.items(), start=1):
        text_lines.append(f"{idx}. {cat} — {flavor} — {price}₺ x {qty}")
    msg = "\n".join(text_lines)

    kb = types.InlineKeyboardMarkup(row_width=2)
    for idx, ((cat, flavor, price), qty) in enumerate(grouped.items(), start=1):
        kb.add(
            types.InlineKeyboardButton(
                text=f"{t(chat_id, 'remove_item')} {idx}",
                callback_data=f"remove_item|{idx}"
            ),
            types.InlineKeyboardButton(
                text=f"{t(chat_id, 'edit_item')} {idx}",
                callback_data=f"edit_item|{idx}"
            )
        )
    kb.add(
        types.InlineKeyboardButton(
            text=f"⬅️ {t(chat_id, 'back_to_categories')}",
            callback_data="go_back_to_categories"
        )
    )
    bot.send_message(chat_id, msg, reply_markup=kb)


# ------------------------------------------------------------------------
#   21. Callback: «Удалить i» из корзины
# ------------------------------------------------------------------------
@ensure_user
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

    (cat, flavor, price), qty = items_list[idx]

    removed = False
    new_cart = []
    for it in cart:
        if not removed and it["category"] == cat and it["flavor"] == flavor and it["price"] == price:
            removed = True
            continue
        new_cart.append(it)
    data["cart"] = new_cart

    bot.answer_callback_query(call.id, t(chat_id, "item_removed").format(flavor=flavor))
    handle_view_cart(call)


# ------------------------------------------------------------------------
#   22. Callback: «Изменить i» в корзине → ввод количества
# ------------------------------------------------------------------------
@ensure_user
@bot.callback_query_handler(func=lambda call: call.data and call.data.startswith("edit_item|"))
def handle_edit_item_request(call):
    chat_id = call.from_user.id
    _, idx_str = call.data.split("|", 1)
    idx = int(idx_str) - 1
    data = user_data.get(chat_id, {})
    cart = data.get("cart", [])

    # Группируем для подсчёта
    grouped = {}
    for item in cart:
        key = (item["category"], item["flavor"], item["price"])
        grouped[key] = grouped.get(key, 0) + 1
    items_list = list(grouped.items())
    if idx < 0 or idx >= len(items_list):
        bot.answer_callback_query(call.id, t(chat_id, "error_invalid"))
        return

    (cat, flavor, price), old_qty = items_list[idx]
    bot.answer_callback_query(call.id)

    # Сохраняем состояние для ввода количества
    data["edit_cart_phase"] = "enter_qty"
    data["edit_index"] = idx
    data["edit_cat"] = cat
    data["edit_flavor"] = flavor
    user_data[chat_id] = data

    # Выводим по-русски или по-английски в зависимости от языка
    lang = user_data.get(chat_id, {}).get("lang", "ru")
    if lang == "ru":
        prefix = f"Текущий товар: {cat} — {flavor} — {price}₺ (в корзине {old_qty} шт)."
    else:
        prefix = f"Current item: {cat} — {flavor} — {price}₺ (in cart {old_qty} pcs)."

    bot.send_message(
        chat_id,
        prefix + "\n" + t(chat_id, "enter_new_qty"),
        reply_markup=types.ReplyKeyboardRemove()
    )


@ensure_user
@bot.message_handler(
    func=lambda m: user_data.get(m.chat.id, {}).get("edit_cart_phase") == "enter_qty",
    content_types=['text']
)
def handle_enter_new_qty(message):
    chat_id = message.chat.id
    data = user_data.get(chat_id, {})
    text = message.text.strip()
    if not text.isdigit():
        bot.send_message(chat_id, t(chat_id, "error_invalid"))
        data["edit_cart_phase"] = None
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
        return

    (cat, flavor, price), old_qty = items_list[idx]

    new_cart = [it for it in cart if not (it["category"] == cat and it["flavor"] == flavor and it["price"] == price)]
    for _ in range(new_qty):
        new_cart.append({"category": cat, "flavor": flavor, "price": price})
    data["cart"] = new_cart

    data["edit_cart_phase"] = None
    data.pop("edit_index", None)
    data.pop("edit_cat", None)
    data.pop("edit_flavor", None)

    if new_qty == 0:
        bot.send_message(chat_id, t(chat_id, "item_removed").format(flavor=flavor),
                         reply_markup=get_inline_main_menu(chat_id))
    else:
        bot.send_message(chat_id, t(chat_id, "qty_changed").format(flavor=flavor, qty=new_qty),
                         reply_markup=get_inline_main_menu(chat_id))

    user_data[chat_id] = data


# ------------------------------------------------------------------------
#   23. Callback: «Очистить корзину»
# ------------------------------------------------------------------------
@ensure_user
@bot.callback_query_handler(func=lambda call: call.data == "clear_cart")
def handle_clear_cart(call):
    chat_id = call.from_user.id
    bot.answer_callback_query(call.id)
    data = user_data.get(chat_id, {})
    data["cart"] = []
    bot.send_message(chat_id, t(chat_id, "cart_cleared"), reply_markup=get_inline_main_menu(chat_id))
    user_data[chat_id] = data


# ------------------------------------------------------------------------
#   24. Callback: завершить заказ (с проверкой и списанием stock)
# ------------------------------------------------------------------------
@ensure_user
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

    conn_local = get_db_connection()
    cursor_local = conn_local.cursor()
    cursor_local.execute("SELECT points FROM users WHERE chat_id = ?", (chat_id,))
    row = cursor_local.fetchone()
    cursor_local.close()
    conn_local.close()

    user_points = row[0] if row else 0

    if user_points > 0:
        max_points = min(user_points, total_try)
        points_try = user_points * 1
        msg = (
                t(chat_id, "points_info").format(points=user_points, points_try=points_try)
                + "\n"
                + t(chat_id, "enter_points").format(max_points=max_points)
        )
        bot.send_message(chat_id, msg, reply_markup=types.ReplyKeyboardRemove())
        data["wait_for_points"] = True
        data["temp_total_try"] = total_try
        data["temp_user_points"] = user_points
    else:
        kb = address_keyboard(chat_id)
        bot.send_message(
            chat_id,
            f"🛒 {t(chat_id, 'view_cart')}:\n\n" +
            "\n".join(f"{item['category']}: {item['flavor']} — {item['price']}₺" for item in cart) +
            f"\n\n{t(chat_id, 'enter_address')}",
            reply_markup=kb
        )
        data["wait_for_address"] = True

    user_data[chat_id] = data


# ------------------------------------------------------------------------
#   25. Handler: ввод количества баллов для списания
# ------------------------------------------------------------------------
@ensure_user
@bot.message_handler(func=lambda m: user_data.get(m.chat.id, {}).get("wait_for_points"), content_types=['text'])
def handle_points_input(message):
    chat_id = message.chat.id
    data = user_data.get(chat_id, {})
    text = message.text.strip()

    if not text.isdigit():
        bot.send_message(chat_id, t(chat_id, "invalid_points").format(max_points=data.get("temp_total_try", 0)))
        return

    points_to_spend = int(text)
    user_points = data.get("temp_user_points", 0)
    total_try = data.get("temp_total_try", 0)
    max_points = min(user_points, total_try)

    if points_to_spend < 0 or points_to_spend > max_points:
        bot.send_message(chat_id, t(chat_id, "invalid_points").format(max_points=max_points))
        return

    if points_to_spend > 0:
        conn_local = get_db_connection()
        cursor_local = conn_local.cursor()
        cursor_local.execute("UPDATE users SET points = points - ? WHERE chat_id = ?", (points_to_spend, chat_id))
        conn_local.commit()
        cursor_local.close()
        conn_local.close()

    discount_try = points_to_spend * 1
    data["pending_discount"] = discount_try
    data["pending_points_spent"] = points_to_spend
    data["wait_for_points"] = False

    cart = data.get("cart", [])
    total_after = total_try - discount_try
    kb = address_keyboard(chat_id)

    summary_lines = [f"{item['category']}: {item['flavor']} — {item['price']}₺" for item in cart]
    summary = "\n".join(summary_lines)

    msg = (
        "🛒 Посмотреть корзину:\n\n"
        f"{summary}\n\n"
        f"Итог до скидки: {total_try}₺\n"
        f"Списано баллов: {points_to_spend} (−{discount_try}₺)\n"
        f"К оплате: {total_after}₺\n\n"
        "Чтобы завершить заказ, укажите адрес:"
    )

    bot.send_message(chat_id, msg, reply_markup=kb)
    data["wait_for_address"] = True

    user_data[chat_id] = data


# ------------------------------------------------------------------------
#   26. Handler: ввод адреса
# ------------------------------------------------------------------------
@ensure_user
@bot.message_handler(
    func=lambda m: user_data.get(m.chat.id, {}).get("wait_for_address"),
    content_types=['text', 'location', 'venue']
)
def handle_address_input(message):
    chat_id = message.chat.id
    data = user_data.get(chat_id, {})
    text = message.text or ""

    # ИСПРАВЛЁННЫЙ ВАРИАНТ

    if text == t(chat_id, "back"):
        data['wait_for_address'] = False
        data['current_category'] = None
        # 1) Убираем клавиатуру запроса локации
        bot.send_message(chat_id,
                         t(chat_id, "choose_category"),
                         reply_markup=types.ReplyKeyboardRemove())
        # 2) Показываем основное inline-меню
        bot.send_message(chat_id,
                         t(chat_id, "choose_category"),
                         reply_markup=get_inline_main_menu(chat_id))
        return

    if text == t(chat_id, "choose_on_map"):
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
    elif text == t(chat_id, "enter_address_text"):
        bot.send_message(chat_id, t(chat_id, "enter_address"), reply_markup=types.ReplyKeyboardRemove())
        return
    elif message.content_type == 'text' and message.text:
        address = message.text.strip()
    else:
        bot.send_message(chat_id, t(chat_id, "error_invalid"), reply_markup=address_keyboard(chat_id))

        return

    data['address'] = address
    data['wait_for_address'] = False
    data['wait_for_contact'] = True
    kb = contact_keyboard(chat_id)
    bot.send_message(chat_id, t(chat_id, "enter_contact"), reply_markup=kb)
    user_data[chat_id] = data


# ------------------------------------------------------------------------
#   27. Handler: ввод контакта
# ------------------------------------------------------------------------
@ensure_user
@bot.message_handler(
    func=lambda m: user_data.get(m.chat.id, {}).get("wait_for_contact"),
    content_types=['text', 'contact']
)
def handle_contact_input(message):
    chat_id = message.chat.id
    data = user_data.get(chat_id, {})
    text = message.text or ""

    # ИСПРАВЛЁННЫЙ ВАРИАНТ (если ты хочешь сразу в main-menu)
    if text == t(chat_id, "back"):
        data['wait_for_address'] = False
        data['wait_for_contact'] = False
        bot.send_message(chat_id,
                         t(chat_id, "choose_category"),
                         reply_markup=types.ReplyKeyboardRemove())
        bot.send_message(chat_id,
                         t(chat_id, "choose_category"),
                         reply_markup=get_inline_main_menu(chat_id))
        return

    if text == t(chat_id, "enter_nickname"):
        bot.send_message(chat_id, "Введите ваш Telegram-ник (без @):", reply_markup=types.ReplyKeyboardRemove())
        return

    if message.content_type == 'contact' and message.contact:
        contact = message.contact.phone_number
    elif message.content_type == 'text' and message.text:
        contact = "@" + message.text.strip().lstrip("@")
    else:
        bot.send_message(chat_id, t(chat_id, "enter_contact"), reply_markup=contact_keyboard(chat_id))
        return

    data['contact'] = contact
    data['wait_for_contact'] = False
    data['wait_for_comment'] = True
    kb = comment_keyboard(chat_id)
    bot.send_message(chat_id, t(chat_id, "enter_comment"), reply_markup=kb)
    user_data[chat_id] = data


# ------------------------------------------------------------------------
#   28. Handler: ввод комментария и сохранение заказа (с учётом списания stock)
# ------------------------------------------------------------------------
@ensure_user
@bot.message_handler(
    func=lambda m: user_data.get(m.chat.id, {}).get("wait_for_comment"),
    content_types=['text']
)
def handle_comment_input(message):
    chat_id = message.chat.id
    data = user_data.get(chat_id, {})
    text = message.text or ""

    # Обработка кнопки «Назад»
    # ИСПРАВЛЁННЫЙ ВАРИАНТ

    if text == t(chat_id, "back"):
        data['wait_for_comment'] = False
        bot.send_message(chat_id,
                         t(chat_id, "choose_category"),
                         reply_markup=types.ReplyKeyboardRemove())
        bot.send_message(chat_id,
                         t(chat_id, "choose_category"),
                         reply_markup=get_inline_main_menu(chat_id))
        return

    # Пользователь вводит текст комментария
    if text == t(chat_id, "enter_comment"):
        bot.send_message(chat_id, t(chat_id, "enter_comment"), reply_markup=types.ReplyKeyboardRemove())
        return

    if message.content_type == 'text' and text != t(chat_id, "send_order"):
        data['comment'] = text.strip()
        bot.send_message(chat_id, t(chat_id, "comment_saved"), reply_markup=comment_keyboard(chat_id))
        user_data[chat_id] = data
        return

    # Пользователь подтвердил отправку заказа
    if text == t(chat_id, "send_order"):
        cart = data.get('cart', [])
        if not cart:
            bot.send_message(chat_id, t(chat_id, "cart_empty"))
            return

        # Считаем сумму заказа и скидку
        total_try = sum(i['price'] for i in cart)
        discount = data.pop("pending_discount", 0)
        total_after = max(total_try - discount, 0)

        # Проверяем наличие на складе
        needed = {}
        for it in cart:
            key = (it["category"], it["flavor"])
            needed[key] = needed.get(key, 0) + 1

        for (cat0, flavor0), qty_needed in needed.items():
            item_obj = next((i for i in menu[cat0]["flavors"] if i["flavor"] == flavor0), None)
            if not item_obj or item_obj.get("stock", 0) < qty_needed:
                bot.send_message(chat_id, f"😕 К сожалению, «{flavor0}» больше не доступен в нужном количестве.")
                return

        # Списываем товары со склада
        for (cat0, flavor0), qty_needed in needed.items():
            for itm in menu[cat0]["flavors"]:
                if itm["flavor"] == flavor0:
                    itm["stock"] = itm.get("stock", 0) - qty_needed
                    break
        with open(MENU_PATH, "w", encoding="utf-8") as f:
            json.dump(menu, f, ensure_ascii=False, indent=2)

        # Подсчёт баллов
        pts_spent  = data.get("pending_points_spent", 0)  # уже списано до этого
        pts_earned = total_after // 30

        # Сохраняем в БД заказ вместе с баллами
        items_json = json.dumps(cart, ensure_ascii=False)
        now = datetime.datetime.utcnow().isoformat()
        conn_local = get_db_connection()
        cursor_local = conn_local.cursor()
        cursor_local.execute(
            "INSERT INTO orders "
            "(chat_id, items_json, total, timestamp, points_spent, points_earned) "
            "VALUES (?,?,?,?,?,?)",
            (chat_id, items_json, total_after, now, pts_spent, pts_earned)
        )
        order_id = cursor_local.lastrowid
        conn_local.commit()

        # Начисляем пользователю новые баллы
        if pts_earned > 0:
            cursor_local.execute(
                "UPDATE users SET points = points + ? WHERE chat_id = ?",
                (pts_earned, chat_id)
            )
            bot.send_message(chat_id, f"👍 Вы получили {pts_earned} бонусных баллов за этот заказ.")

        # 2) Обработка реферальной премии — всегда, если есть referred_by
        cursor_local.execute(
            "SELECT referred_by FROM users WHERE chat_id = ?",
            (chat_id,)
        )
        row = cursor_local.fetchone()
        if row and row[0]:
            inviter = row[0]
            cursor_local.execute(
                "UPDATE users SET points = points + 200 WHERE chat_id = ?",
                (inviter,)
            )
            bot.send_message(inviter, "🎉 Вам начислено 200 бонусных баллов за приглашение нового клиента!")
            cursor_local.execute(
                "UPDATE users SET referred_by = NULL WHERE chat_id = ?",
                (chat_id,)
            )

        # 3) Закрываем соединение
        conn_local.commit()
        cursor_local.close()
        conn_local.close()
        # Обрабатываем реферальную систему (если нужно)...
        # (ваш уже существующий код по начислению 200 баллов пригласившему)

        # Отправляем уведомления в личный чат и группу
        summary = "\n".join(f"{i['category']}: {i['flavor']} — {i['price']}₺" for i in cart)
        # Количество единиц товара в заказе
        qty_total = len(cart)

        rates = fetch_rates()
        rub = round(total_after * rates.get("RUB", 0) + 500 * qty_total, 2)
        usd = round(total_after * rates.get("USD", 0) + 2 * qty_total, 2)
        eur = round(total_after * rates.get("EUR", 0) + 2 * qty_total, 2)
        uah = round(total_after * rates.get("UAH", 0) + 350 * qty_total, 2)
        conv = f"({rub}₽, ${usd}, €{eur}, ₴{uah})"

        # Русский
        full_rus = (
            f"📥 Новый заказ от @{message.from_user.username or message.from_user.first_name}:\n\n"
            f"{summary}\n\n"
            f"Итог: {total_after}₺ {conv}\n"
            f"📍 Адрес: {data.get('address','—')}\n"
            f"📱 Контакт: {data.get('contact','—')}\n"
            f"💬 Комментарий: {data.get('comment','—')}"
        )
        bot.send_message(PERSONAL_CHAT_ID, full_rus)

        # Английский с кнопкой отмены
        full_en = (
            f"📥 New order from @{message.from_user.username or message.from_user.first_name}:\n\n"
            f"{summary}\n\n"
            f"Total: {total_after}₺ {conv}\n"
            f"📍 Address: {data.get('address', '—')}\n"  # <-- без перевода
            f"📱 Contact: {data.get('contact', '—')}\n"
            f"💬 Comment: {translate_to_en(data.get('comment', ''))}"
        )

        kb = types.InlineKeyboardMarkup(row_width=2)
        kb.add(
            types.InlineKeyboardButton(
                text="❌ Отменить заказ",
                callback_data=f"cancel_order|{order_id}"
            ),
            types.InlineKeyboardButton(
                text="✅ Order Delivered",
                callback_data=f"order_delivered|{order_id}"
            )
        )
        bot.send_message(GROUP_CHAT_ID, full_en, reply_markup=kb)

        # Завершаем диалог с пользователем
        bot.send_message(
            chat_id,
            t(chat_id, "order_accepted"),
            reply_markup=types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
                              .add(f"➕ {t(chat_id, 'add_more')}")

        )
        # Отправляем пользователю полную историю заказа (как админам)
        user_order_summary = (
            f"📋 Ваш заказ:\n\n"
            f"{summary}\n\n"
            f"Итог: {total_after}₺ {conv}\n"
            f"📍 Адрес: {data.get('address', '—')}\n"
            f"📱 Контакт: {data.get('contact', '—')}\n"
            f"💬 Комментарий: {data.get('comment', '—')}"
        )
        bot.send_message(chat_id, user_order_summary)

        # Сбрасываем состояние
        data.update({
            "cart": [], "current_category": None,
            "wait_for_address": False,
            "wait_for_contact": False,
            "wait_for_comment": False,
            "pending_discount": 0,
            "pending_points_spent": 0
        })
        user_data[chat_id] = data

        cursor_local.close()
        conn_local.close()
        return


        # Списываем stock из menu и сохраняем JSON
        # ... после того, как вы записали заказ в БД и начислили баллы пользователю:

        # 1) Сформировать текст уведомления англ. для админ-группы
        full_en = (
            f"📥 New order from @{message.from_user.username or message.from_user.first_name}:\n\n"
            f"{summary}\n\n"
            f"Total: {total_after}₺ {conv}\n"
            f"📍 Address: {data.get('address', '—')}\n"
            f"📱 Contact: {data.get('contact', '—')}\n"
            f"💬 Comment: {translate_to_en(data.get('comment', ''))}"
        )

        # 2) Создать инлайн-клавиатуру с кнопкой отмены
        kb = types.InlineKeyboardMarkup()
        kb.add(
            types.InlineKeyboardButton(
                text="❌ Отменить заказ",
                callback_data=f"cancel_order|{order_id}"
            )
        )

        # 3) Отправить сообщение вместе с кнопкой
        bot.send_message(
            GROUP_CHAT_ID,
            full_en,
            reply_markup=kb
        )



        summary = "\n".join(f"{i['category']}: {i['flavor']} — {i['price']}₺" for i in cart)
        rates = fetch_rates()
        rub = round(total_after * rates.get("RUB", 0) + 500, 2)
        usd = round(total_after * rates.get("USD", 0) + 2, 2)
        eur = round(total_after * rates.get("EUR", 0) + 2, 2)
        uah = round(total_after * rates.get("UAH", 0) + 200, 2)
        conv = f"({rub}₽, ${usd}, €{eur}, ₴{uah})"

        full_rus = (
            f"📥 Новый заказ от @{message.from_user.username or message.from_user.first_name}:\n\n"
            f"{summary}\n\n"
            f"Итог: {total_after}₺ {conv}\n"
            f"📍 Адрес: {data.get('address', '—')}\n"
            f"📱 Контакт: {data.get('contact', '—')}\n"
            f"💬 Комментарий: {data.get('comment', '—')}"
        )
        bot.send_message(PERSONAL_CHAT_ID, full_rus)

        comment_ru = data.get('comment', '') or '—'
        comment_en = translate_to_en(comment_ru) or '—'

        full_en = (
            f"📥 New order from @{message.from_user.username or message.from_user.first_name}:\n\n"
            f"{summary}\n\n"
            f"Total: {total_after}₺ {conv}\n"
            f"📍 Address: {data.get('address', '—')}\n"
            f"📱 Contact: {data.get('contact', '—')}\n\n"
            f"💬 Comment: {comment_en}"
        )
        bot.send_message(GROUP_CHAT_ID, full_en, reply_markup=kb)

        bot.send_message(
            chat_id,
            t(chat_id, "order_accepted"),
            reply_markup=types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
            .add(f"➕ {t(chat_id, 'add_more')}")
        )

        data.update({
            "cart": [], "current_category": None,
            "wait_for_address": False, "wait_for_contact": False, "wait_for_comment": False
        })
        user_data[chat_id] = data
        return


# ------------------------------------------------------------------------
#   29. /change: перевод в режим редактирования меню (только на английском)
# ------------------------------------------------------------------------
@ensure_user
@bot.message_handler(commands=['change'])
def cmd_change(message):
    chat_id = message.chat.id

    # Доступ к /change только для трёх админов
    if chat_id not in ADMINS:
        bot.send_message(chat_id, "У вас нет доступа к этой команде.")
        return

    # Инициализируем данные пользователя, если нужно
    if chat_id not in user_data:
        user_data[chat_id] = {
            "lang": "ru",
            "cart": [],
            "current_category": None,
            "wait_for_points": False,
            "wait_for_address": False,
            "wait_for_contact": False,
            "wait_for_comment": False,
            "address": "",
            "contact": "",
            "comment": "",
            "pending_discount": 0,
            "pending_points_spent": 0,
            "temp_total_try": 0,
            "temp_user_points": 0,
            "edit_phase": None,
            "edit_cat": None,
            "edit_flavor": None,
            "edit_index": None,
            "edit_cart_phase": None,
            "awaiting_review_flavor": None,
            "awaiting_review_rating": False,
            "awaiting_review_comment": False,
            "temp_review_flavor": None,
            "temp_review_rating": 0
        }

    # Переходим в режим редактирования меню
    data = user_data[chat_id]
    data.update({
        "current_category": None,
        "wait_for_points": False,
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
    user_data[chat_id] = data
@ensure_user
@bot.message_handler(func=lambda m: m.text == "📦 New Supply")
def handle_new_supply(message):
    if message.chat.id not in ADMINS:
        return bot.reply_to(message, "У вас нет доступа.")

    # Берём всех пользователей из базы
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT chat_id FROM users")
    users = [row[0] for row in cur.fetchall()]
    cur.close()
    conn.close()

    # Шлём каждому сообщение
    for uid in users:
        try:
            bot.send_message(uid, "🚚 Новая поставка прибыла. Проверь меню")
        except Exception as e:
            print(f"Не удалось отправить сообщение {uid}: {e}")

    bot.reply_to(message, "✅ Сообщение о новой поставке разослано всем пользователям.")

@bot.message_handler(commands=['stock'])
def cmd_stock(message: types.Message):
    if message.chat.id != GROUP_CHAT_ID:
        return bot.reply_to(message, "❌ This command is available only in the admin group.")

    parts = message.text.strip().split()
    if len(parts) != 2 or not parts[1].isdigit():
        return bot.reply_to(
            message,
            "Usage: /stock <total_deliveries>\nExample: /stock 42"
        )

    new_total = int(parts[1])
    conn = get_db_connection()
    cur = conn.cursor()

    # очищаем всё и вставляем новую метку
    cur.execute("DELETE FROM delivered_counts")
    cur.execute("INSERT INTO delivered_counts(currency, count) VALUES ('total', ?)", (new_total,))
    cur.execute("DELETE FROM delivered_log")

    conn.commit()
    cur.close()
    conn.close()

    # отвечаем коротко, без старого значения
    bot.reply_to(
        message,
        f"✅ Overall delivered orders count set to {new_total} pcs, and delivery log cleared."
    )



# ------------------------------------------------------------------------
#   30. Хендлер /points
# ------------------------------------------------------------------------
@ensure_user
@bot.message_handler(commands=['points'])
def cmd_points(message):
    chat_id = message.chat.id
    conn_local = get_db_connection()
    cursor_local = conn_local.cursor()
    cursor_local.execute("SELECT points FROM users WHERE chat_id = ?", (chat_id,))
    row = cursor_local.fetchone()
    cursor_local.close()
    conn_local.close()

    if row is None or row[0] == 0:
        bot.send_message(chat_id, t(chat_id, "points_info").format(points=0, points_try=0))
    else:
        points = row[0]
        bot.send_message(chat_id, t(chat_id, "points_info").format(points=points, points_try=points))


# ------------------------------------------------------------------------
#   31. Хендлер /convert — курсы и конвертация суммы TRY
# ------------------------------------------------------------------------
@ensure_user
@bot.message_handler(commands=['convert'])
def cmd_convert(message):
    chat_id = message.chat.id
    parts   = message.text.split()
    rates   = fetch_rates()
    rub     = rates.get("RUB", 0)
    usd     = rates.get("USD", 0)
    eur     = rates.get("EUR", 0)
    uah     = rates.get("UAH", 0)

    # Если хотя бы один курс не вытащился — сразу вылетаем
    if 0 in (rub, usd, eur, uah):
        return bot.send_message(chat_id, "Курсы валют сейчас недоступны, попробуйте позже.")

    # Просто показать текущие курсы
    if len(parts) == 1:
        text = (
            "📊 Курс лиры сейчас:\n"
            f"1₺ = {rub:.2f} ₽\n"
            f"1₺ = {usd:.2f} $\n"
            f"1₺ = {uah:.2f} ₴\n\n"
            f"1₺ = {eur:.2f} €\n"
            "Для пересчёта напишите: /convert 1300"
        )
        return bot.send_message(chat_id, text)

    # Если передали сумму — делаем расчёт
    if len(parts) == 2:
        try:
            amount = float(parts[1].replace(",", "."))
        except ValueError:
            return bot.send_message(chat_id, "Формат: /convert 1300 (или другую сумму в лирах)")

        res_rub = amount * rub
        res_usd = amount * usd
        # вот здесь мы прибавляем 2 ₼ к евро
        res_eur = amount * eur + 2
        res_uah = amount * uah

        text = (
            f"{amount:.2f}₺ = {res_rub:.2f} ₽\n"
            f"{amount:.2f}₺ = {res_usd:.2f} $\n"
            f"{amount:.2f}₺ = {res_eur:.2f} €\n"
            f"{amount:.2f}₺ = {res_uah:.2f} ₴"
        )
        return bot.send_message(chat_id, text)

    # Если больше аргументов — просим уточнить
    return bot.send_message(chat_id, "Использование: /convert 1300")

@ensure_user
@bot.message_handler(commands=['total'])
def cmd_total(message):
    chat_id = message.chat.id

    lines = []
    total_pcs = 0
    for cat, cat_data in menu.items():
        lines.append(f"<b>{cat}</b>:")
        for itm in cat_data.get("flavors", []):
            flavor = itm.get("flavor", "—")
            stock  = int(itm.get("stock", 0))
            total_pcs += stock
            lines.append(f"  • {flavor} — {stock} pcs")
        lines.append("")  # разделитель между категориями

    # удаляем последнюю пустую строку
    if lines and lines[-1] == "":
        lines.pop()

    # добавляем итог
    lines.append(f"\n<b>Total:</b> {total_pcs} pcs")

    text = "\n".join(lines) if lines else "No flavors available."
    bot.send_message(chat_id, text, parse_mode="HTML")

@bot.message_handler(commands=['stocknow'])
def cmd_stocknow(message: types.Message):
    # Доступ только в админ-группе
    if message.chat.id != GROUP_CHAT_ID:
        return

    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT SUM(count) FROM delivered_counts")
    total = cur.fetchone()[0] or 0
    cur.close()
    conn.close()

    bot.reply_to(message, f"✅ Total delivered: {total} pcs.")



@ensure_user
@bot.message_handler(commands=['payment'])
def cmd_payment(message):
    chat_id = message.chat.id
    # 1) Номер IBAN
    bot.send_message(chat_id, "TR22 0004 6013 3088 8000 0301 47")
    # 2) Имя получателя
    bot.send_message(chat_id, "Artur Yuldashev")
    # 3) Адрес кошелька
    bot.send_message(chat_id, "TUnMJ7oCtSDCHZiQSMrFjShkUPv18SVFDc")
    # 4) Сеть
    bot.send_message(chat_id, "Tron (TRC-20)")
    # 5) Карта
    bot.send_message(chat_id, "4441111157718424")
    # 6) Валюта
    bot.send_message(chat_id, "Grivne Vlad")
    # 7) Контакт
    bot.send_message(chat_id, "+90 553 006 52 04")
    # Дополнительно Тинькофф в рублях
    bot.send_message(chat_id, "Артур М. (T BANK RUB)")
    bot.send_message(chat_id, "Or by RUB Card number")
    bot.send_message(chat_id, "2200701785613040")

# в самом верху вашего файла, сразу после импорта и констант:

def compose_sold_report() -> str:
    """
    Отчёт за сегодня:
    - список доставок
    - сводка по валютам
    - общая выручка, выплаты курьеру, остаток
    - остатки по категориям и общий остаток
    - общее количество проданных штук
    """
    import datetime, pytz, json
    from sqlite3 import connect

    # 1️⃣ Начало текущего дня по Москве → UTC
    moscow_tz = pytz.timezone("Europe/Moscow")
    now_msk = datetime.datetime.now(moscow_tz)
    start_msk = now_msk.replace(hour=0, minute=0, second=0, microsecond=0)
    start_utc = start_msk.astimezone(pytz.utc).isoformat()

    # 2️⃣ Достаём сегодняшние доставки из БД
    conn = connect(DB_PATH, check_same_thread=False)
    cur = conn.cursor()
    cur.execute("""
        SELECT dl.timestamp, dl.order_id, dl.currency, dl.qty, o.items_json, o.total
        FROM delivered_log dl
        JOIN orders o ON o.order_id = dl.order_id
        WHERE dl.timestamp >= ?
        ORDER BY dl.timestamp ASC
    """, (start_utc,))
    rows = cur.fetchall()
    cur.close()
    conn.close()

    if not rows:
        return "📊 Deliveries report: no deliveries recorded today."

    # 3️⃣ Собираем данные по доставкам
    detail_lines = []
    summary_by_currency = {}
    total_sold_today = 0
    cash_revenue = 0
    delivered_qty_exc_free = 0

    for ts, order_id, currency, qty, items_json, order_total in rows:
        ts_dt = datetime.datetime.fromisoformat(ts).replace(tzinfo=datetime.timezone.utc)
        time_str = ts_dt.astimezone(moscow_tz).strftime("%H:%M:%S")
        items = json.loads(items_json)
        items_repr = ", ".join(f"{i['flavor']} — {i['price']}₺" for i in items)

        detail_lines.append(f"{time_str} — Order #{order_id} — {currency.upper()}: {qty} pcs ({items_repr})")

        summary_by_currency[currency] = summary_by_currency.get(currency, 0) + qty
        total_sold_today += qty

        if currency.lower() != 'free':
            delivered_qty_exc_free += qty
        if currency.lower() == 'cash':
            cash_revenue += order_total

    # 4️⃣ Сводка по валютам
    summary_lines = ["Summary by currency:"]
    for cur, cnt in summary_by_currency.items():
        summary_lines.append(f"{cur.upper()}: {cnt} pcs")

    courier_pay = delivered_qty_exc_free * 150
    remaining = cash_revenue - courier_pay

    # 5️⃣ Остатки по категориям (без разбивки по вкусам)
    total_stock_left = 0
    stock_lines = ["\n📦 Current stock by category:"]
    for cat, cat_data in menu.items():
        cat_total = sum(int(itm.get("stock", 0)) for itm in cat_data.get("flavors", []))
        total_stock_left += cat_total
        stock_lines.append(f"• {cat}: {cat_total} pcs")

    # 6️⃣ Итоги
    stock_lines.append(f"\n🧾 Sold today: {total_sold_today} pcs")
    stock_lines.append(f"📦 Remaining stock total: {total_stock_left} pcs")

    # 7️⃣ Финальный текст
    report = (
        "📊 Deliveries today:\n\n"
        + "\n".join(detail_lines)
        + "\n\n" + "\n".join(summary_lines)
        + f"\n\n📊 Cash revenue: {cash_revenue}₺"
        + f"\n🏃‍♂️ Courier earnings: {courier_pay}₺"
        + f"\n💰 Remaining revenue: {remaining}₺"
        + "\n\n" + "\n".join(stock_lines)
    )
    return report



def send_daily_sold_report():
    """
    Функция, которую будет вызывать APScheduler.
    """
    text = compose_sold_report()
    # отправляем в вашу группу
    bot.send_message(GROUP_CHAT_ID, text)

@ensure_user
@bot.message_handler(commands=['sold'])
def cmd_sold(message):
    report = compose_sold_report()
    # при ручном вызове шлём в тот же чат, откуда команда
    bot.send_message(message.chat.id, report)


# 1) Определяем отдельный хендлер прямо рядом с /convert, /points и т.д.
@ensure_user
@bot.message_handler(commands=['stats'])
def cmd_stats(message: types.Message):
    user_id = message.from_user.id
    if user_id not in ADMINS:
        return bot.reply_to(message, "У вас нет доступа.")

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM orders")
    total_orders = cursor.fetchone()[0]
    cursor.execute("SELECT SUM(total) FROM orders")
    total_revenue = cursor.fetchone()[0] or 0
    cursor.execute("SELECT items_json FROM orders")
    all_items = cursor.fetchall()
    cursor.close()
    conn.close()

    # Собираем топ-5 вкусов
    counts = {}
    for (items_json,) in all_items:
        for i in json.loads(items_json):
            counts[i["flavor"]] = counts.get(i["flavor"], 0) + 1
    top5 = sorted(counts.items(), key=lambda x: x[1], reverse=True)[:5]
    lines = [f"{fl}:{qty} шт." for fl,qty in top5] or ["Пока нет данных."]

    report = (
        f"📊 Статистика магазина:\n"
        f"Всего заказов: {total_orders}\n"
        f"Общая выручка: {total_revenue}₺\n\n"
        f"Топ-5 продаваемых вкусов:\n" +
        "\n".join(lines)
    )
    bot.send_message(message.chat.id, report)


@ensure_user
@bot.message_handler(commands=['users'])
def cmd_users(message):
    if message.chat.id not in ADMINS:
        return bot.reply_to(message, "У вас нет доступа.")

    conn = get_db_connection()
    cur = conn.cursor()

    # Общее количество пользователей
    cur.execute("SELECT COUNT(*) FROM users")
    total_users = cur.fetchone()[0]

    # Последние 10 зарегистрированных
    cur.execute("SELECT chat_id, referral_code FROM users ORDER BY rowid DESC LIMIT 10")
    recent = cur.fetchall()

    cur.close()
    conn.close()

    lines = [f"Всего пользователей: {total_users}", "", "Последние 10 зарегистрированных:"]
    for uid, ref in recent:
        lines.append(f"• {uid} (ref: {ref})")

    bot.send_message(message.chat.id, "\n".join(lines))


@ensure_user
@bot.message_handler(commands=['review'])
def cmd_review(message):
    chat_id = message.chat.id
    parts = message.text.split(maxsplit=1)
    if len(parts) != 2:
        return bot.send_message(chat_id, "Использование: /review <название_вкуса>")

    q = _normalize(parts[1])
    # Сначала точное совпадение
    matches = [
        itm["flavor"]
        for cat in menu.values()
        for itm in cat["flavors"]
        if _normalize(itm["flavor"]) == q
    ]
    # Если нет — подстрока
    if not matches:
        matches = [
            itm["flavor"]
            for cat in menu.values()
            for itm in cat["flavors"]
            if q in _normalize(itm["flavor"])
        ]

    if not matches:
        all_fl = sorted({itm["flavor"] for cat in menu.values() for itm in cat["flavors"]})
        return bot.send_message(
            chat_id,
            "Вкус не найден. Доступные вкусы:\n" + "\n".join(all_fl)
        )
    if len(matches) > 1:
        return bot.send_message(
            chat_id,
            "Найдено несколько вкусов, уточните:\n" +
            "\n".join(f"/review {m}" for m in matches)
        )

    # ровно один матч
    flavor = matches[0]
    user_data[chat_id]["temp_review_flavor"] = flavor

    kb = types.InlineKeyboardMarkup(row_width=5)
    for i in range(1, 6):
        kb.add(types.InlineKeyboardButton(text="⭐️"*i, callback_data=f"review_rate|{i}"))
    bot.send_message(chat_id, f"Пожалуйста, оцените вкус «{flavor}»", reply_markup=kb)

@ensure_user
@bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("review_rate|"))
def callback_review_rate(call):
    chat_id = call.from_user.id
    rating = int(call.data.split("|",1)[1])
    data = user_data[chat_id]
    data["temp_review_rating"] = rating
    data["awaiting_review_comment"] = True
    user_data[chat_id] = data

    bot.answer_callback_query(call.id, f"Вы выбрали {rating}⭐️")
    bot.send_message(chat_id, "Оставьте комментарий или отправьте /skip, чтобы пропустить",
                     reply_markup=types.ReplyKeyboardRemove())

@ensure_user
@bot.message_handler(func=lambda m: user_data.get(m.chat.id,{}).get("awaiting_review_comment"), content_types=['text'])
def handle_review_comment(message):
    chat_id = message.chat.id
    data = user_data[chat_id]
    flavor = data.pop("temp_review_flavor")
    rating = data.pop("temp_review_rating")
    raw = message.text.strip()
    comment = "" if raw.lower() == "/skip" else raw
    data["awaiting_review_comment"] = False
    user_data[chat_id] = data

    now = datetime.datetime.utcnow().isoformat()
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO reviews (chat_id, category, flavor, rating, comment, timestamp)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (chat_id, None, flavor, rating, comment, now)
    )
    conn.commit()
    cur.execute("SELECT AVG(rating) FROM reviews WHERE flavor = ?", (flavor,))
    avg = round(cur.fetchone()[0] or 0, 1)
    cur.close()
    conn.close()

    # обновляем меню
    for cat in menu.values():
        for itm in cat["flavors"]:
            if itm["flavor"] == flavor:
                itm["rating"] = avg
    with open(MENU_PATH, "w", encoding="utf-8") as f:
        json.dump(menu, f, ensure_ascii=False, indent=2)

    bot.send_message(
        chat_id,
        f"Спасибо за отзыв! Средний рейтинг «{flavor}» теперь {avg}⭐️",
        reply_markup=get_inline_main_menu(chat_id)
    )

@ensure_user
@bot.message_handler(commands=['reviewtop'])
def cmd_reviewtop(message):
    chat_id = message.chat.id

    conn = get_db_connection()
    cur = conn.cursor()
    # сгруппируем по вкусу, посчитаем средний рейтинг и кол-во отзывов
    cur.execute("""
        SELECT flavor,
               ROUND(AVG(rating),1) AS avg_r,
               COUNT(*) AS cnt
        FROM reviews
        GROUP BY flavor
        HAVING cnt > 0
        ORDER BY avg_r DESC, cnt DESC
        LIMIT 5
    """)
    rows = cur.fetchall()
    cur.close()
    conn.close()

    if not rows:
        return bot.send_message(chat_id, "Пока нет отзывов ни на один вкус.")

    text = ["🏆 Топ-5 вкусов по оценкам:"]
    for i, (flavor, avg_r, cnt) in enumerate(rows, start=1):
        text.append(f"{i}. {flavor} — {avg_r}⭐ ({cnt} отз.)")

    bot.send_message(chat_id, "\n".join(text))

@ensure_user
@bot.message_handler(commands=['show_reviews'])
def cmd_show_reviews(message):
    chat_id = message.chat.id
    parts = message.text.split(maxsplit=1)
    if len(parts) != 2:
        return bot.send_message(chat_id, "Использование: /show_reviews <название_вкуса>")

    flavor_query = parts[1].strip()
    print(f"DEBUG: show_reviews for '{flavor_query}'")  # лог в консоль

    conn = get_db_connection()
    cur = conn.cursor()
    # нечувствительный к регистру поиск
    cur.execute(
        "SELECT rating, comment, timestamp FROM reviews "
        "WHERE LOWER(flavor) LIKE '%' || LOWER(?) || '%' "
        "ORDER BY timestamp DESC",
        (flavor_query,)
    )
    rows = cur.fetchall()
    cur.close()
    conn.close()

    if not rows:
        return bot.send_message(chat_id, f"Для вкуса «{flavor_query}» ещё нет отзывов.")

    lines = [f"📝 Отзывы для «{flavor_query}»:"]
    for rating, comment, ts in rows:
        date = ts.split("T")[0]
        if comment:
            lines.append(f"⭐️ {rating} — {comment} ({date})")
        else:
            lines.append(f"⭐️ {rating} — без комментария ({date})")

    bot.send_message(chat_id, "\n".join(lines))

@ensure_user
@bot.message_handler(commands=['help'])
def cmd_help(message: types.Message):
    if message.chat.id == GROUP_CHAT_ID:
        help_text = (
          "/stats      — View store statistics (ADMIN only)\n"
          "/change     — Enter menu-edit mode (ADMIN only)\n"
          "/stock &lt;N&gt;  — Set overall delivered count & clear log\n"
          "/sold       — Today's deliveries report (MSK-based)\n"
          "/payment    — Payment details\n"
          "/total      — Show stock levels for all flavors\n"
          "/help       — This help message"
        )
        bot.send_message(message.chat.id, help_text, parse_mode="HTML")
    else:
        help_text = (
          "<b>Доступные команды:</b>\n\n"
          "/start         — Перезапустить бота / регистрация\n"
          "/points        — Проверить баланс бонусных баллов\n"
          "/convert [N]   — Курсы и конвертация TRY → RUB/USD/UAH\n"
          "/review &lt;вкус&gt; — Оставить отзыв\n"
          "/show_reviews  — Показать отзывы\n"
          "/history       — История заказов\n"
          "/help          — Это сообщение помощи"
        )
        bot.send_message(message.chat.id, help_text, parse_mode="HTML")



# ------------------------------------------------------------------------
#   35. Универсальный хендлер (всё остальное, включая /change логику)
# ------------------------------------------------------------------------
@ensure_user
@bot.message_handler(content_types=['text', 'location', 'venue', 'contact'])
def universal_handler(message):
    chat_id = message.chat.id
    text = message.text or ""
    if chat_id not in user_data:
        user_data[chat_id] = {
            "lang": "ru",
            "cart": [],
            "current_category": None,
            "wait_for_points": False,
            "wait_for_address": False,
            "wait_for_contact": False,
            "wait_for_comment": False,
            "address": "",
            "contact": "",
            "comment": "",
            "pending_discount": 0,
            "pending_points_spent": 0,
            "temp_total_try": 0,
            "temp_user_points": 0,
            "edit_phase": None,
            "edit_cat": None,
            "edit_flavor": None,
            "edit_index": None,
            "edit_cart_phase": None,
            "awaiting_review_flavor": None,
            "awaiting_review_rating": False,
            "awaiting_review_comment": False,
            "temp_review_flavor": None,
            "temp_review_rating": 0
        }
    data = user_data[chat_id]

    # ─── Режим редактирования меню (/change) ────────────────────────────────────────
    if data.get('edit_phase'):
        phase = data['edit_phase']

        # 1) Главное меню редактирования (всё на английском)
        if phase == 'choose_action':
            # Cancel
            # ИСПРАВЛЁННЫЙ ВАРИАНТ

            # Cancel
            if text == "❌ Cancel":
                data['edit_phase'] = None
                data['edit_cat'] = None
                # 1) Сначала убираем любую reply-клавиатуру
                bot.send_message(chat_id,
                                 "Editing cancelled.",
                                 reply_markup=types.ReplyKeyboardRemove())
                # 2) Затем показываем inline-меню
                bot.send_message(chat_id,
                                 t(chat_id, "choose_category"),
                                 reply_markup=get_inline_main_menu(chat_id))
                user_data[chat_id] = data
                return

            # Back
            if text == "⬅️ Back":
                data['edit_phase'] = None
                data['edit_cat'] = None
                bot.send_message(chat_id,
                                 "Returned to main menu.",
                                 reply_markup=types.ReplyKeyboardRemove())
                bot.send_message(chat_id,
                                 t(chat_id, "choose_category"),
                                 reply_markup=get_inline_main_menu(chat_id))
                return

            if text == "➕ Add Category":
                data['edit_phase'] = 'add_category'
                kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
                kb.add("⬅️ Back", "❌ Cancel")
                bot.send_message(chat_id, "Enter new category name:", reply_markup=kb)
                user_data[chat_id] = data
                return

            if text == "➖ Remove Category":
                data['edit_phase'] = 'remove_category'
                kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
                for cat_key in menu:
                    kb.add(cat_key)
                kb.add("⬅️ Back", "❌ Cancel")
                bot.send_message(chat_id, "Select category to remove:", reply_markup=kb)
                user_data[chat_id] = data
                return

            if text == "✏️ Rename Category":
                data['edit_phase'] = 'rename_category_select'
                kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
                for cat_key in menu:
                    kb.add(cat_key)
                kb.add("⬅️ Back", "❌ Cancel")
                bot.send_message(chat_id, "Выберите категорию для переименования:", reply_markup=kb)
                user_data[chat_id] = data
                return

            if text == "💲 Fix Price":
                data['edit_phase'] = 'choose_fix_price_cat'
                kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
                for cat_key in menu:
                    kb.add(cat_key)
                kb.add("⬅️ Back", "❌ Cancel")
                bot.send_message(chat_id, "Select category to fix price for:", reply_markup=kb)
                user_data[chat_id] = data
                return

            if text == "ALL IN":
                data['edit_phase'] = 'choose_all_in_cat'
                kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
                for cat_key in menu:
                    kb.add(cat_key)
                kb.add("⬅️ Back", "❌ Cancel")
                bot.send_message(chat_id, "Select category to replace full flavor list:", reply_markup=kb)
                user_data[chat_id] = data
                return

            if text == "🔄 Actual Flavor":
                data['edit_phase'] = 'choose_cat_actual'
                kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
                for cat_key in menu:
                    kb.add(cat_key)
                kb.add("⬅️ Back", "❌ Cancel")
                bot.send_message(chat_id, "Select category to update individual flavor stock:", reply_markup=kb)
                user_data[chat_id] = data
                return

            if text == "🖼️ Add Category Picture":
                data['edit_phase'] = 'choose_category_for_picture'
                kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
                for cat_key in menu:
                    kb.add(cat_key)
                kb.add("⬅️ Back", "❌ Cancel")
                bot.send_message(chat_id, "Select category to update picture for:", reply_markup=kb)
                user_data[chat_id] = data
                return

            if text == "Set Category Flavor to 0":
                data['edit_phase'] = 'choose_cat_zero'
                kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
                for cat_key in menu:
                    kb.add(cat_key)
                kb.add("⬅️ Back", "❌ Cancel")
                bot.send_message(chat_id, "Select category to set all flavors to zero stock:", reply_markup=kb)
                user_data[chat_id] = data
                return

            bot.send_message(chat_id, "Choose action:", reply_markup=edit_action_keyboard())
            return

        # 2) Добавить категорию
        if phase == 'add_category':
            #TODO
            if text == "⬅️ Back":
                data['edit_phase'] = 'choose_action'
                bot.send_message(chat_id, "Back to editing menu:", reply_markup=edit_action_keyboard())
                user_data[chat_id] = data
                return
            if text == "❌ Cancel":
                data['edit_phase'] = None
                data['edit_cat'] = None
                # 1) Убираем reply-клавиатуру
                bot.send_message(chat_id,
                                 "Editing cancelled.",
                                 reply_markup=types.ReplyKeyboardRemove())
                # 2) Показываем inline-меню
                bot.send_message(chat_id,
                                 t(chat_id, "choose_category"),
                                 reply_markup=get_inline_main_menu(chat_id))
                user_data[chat_id] = data
                return

            new_cat = text.strip()
            if not new_cat or new_cat in menu:
                kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
                kb.add("⬅️ Back", "❌ Cancel")
                bot.send_message(chat_id, "Invalid or existing name. Try again:", reply_markup=kb)
                return

            menu[new_cat] = {
                "price": 1300,
                "flavors": []
            }
            with open(MENU_PATH, "w", encoding="utf-8") as f:
                json.dump(menu, f, ensure_ascii=False, indent=2)

            data['edit_phase'] = 'choose_action'
            bot.send_message(chat_id, f"Category '{new_cat}' added.", reply_markup=edit_action_keyboard())
            user_data[chat_id] = data
            return

        # 3) Выбор категории для загрузки картинки
        if phase == 'choose_category_for_picture':
            if text == "⬅️ Back":
                data['edit_phase'] = 'choose_action'
                bot.send_message(chat_id, "Back to editing menu:", reply_markup=edit_action_keyboard())
                user_data[chat_id] = data
                return
            if text == "❌ Cancel":
                data['edit_phase'] = None
                data['edit_cat'] = None
                # 1) Убираем reply-клавиатуру
                bot.send_message(chat_id,
                                 "Editing cancelled.",
                                 reply_markup=types.ReplyKeyboardRemove())
                # 2) Показываем inline-меню
                bot.send_message(chat_id,
                                 t(chat_id, "choose_category"),
                                 reply_markup=get_inline_main_menu(chat_id))
                user_data[chat_id] = data
                return

            if text in menu:
                data['edit_cat'] = text
                data['edit_phase'] = 'enter_category_picture_url'
                kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
                kb.add("⬅️ Back", "❌ Cancel")
                bot.send_message(chat_id, "Please send RAW URL for the new category picture:", reply_markup=kb)
                user_data[chat_id] = data
                return
            else:
                kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
                kb.add("⬅️ Back", "❌ Cancel")
                bot.send_message(chat_id, "Select a valid category from the list:", reply_markup=kb)
                return

        # 4) Ввод URL для картинки категории
        if phase == 'enter_category_picture_url':
            if text == "⬅️ Back":
                data['edit_phase'] = 'choose_action'
                bot.send_message(chat_id, "Back to editing menu:", reply_markup=edit_action_keyboard())
                user_data[chat_id] = data
                return
            if text == "❌ Cancel":
                data['edit_phase'] = None
                data['edit_cat'] = None
                # 1) Убираем reply-клавиатуру
                bot.send_message(chat_id,
                                 "Editing cancelled.",
                                 reply_markup=types.ReplyKeyboardRemove())
                # 2) Показываем inline-меню
                bot.send_message(chat_id,
                                 t(chat_id, "choose_category"),
                                 reply_markup=get_inline_main_menu(chat_id))
                user_data[chat_id] = data
                return

            new_url = text.strip()
            cat0 = data.get('edit_cat')
            if cat0 and new_url:
                if isinstance(menu.get(cat0), dict):
                    menu[cat0]['photo_url'] = new_url
                    with open(MENU_PATH, "w", encoding="utf-8") as f:
                        json.dump(menu, f, ensure_ascii=False, indent=2)
                    bot.send_message(chat_id, f"Picture for category '{cat0}' updated.",
                                     reply_markup=edit_action_keyboard())
                else:
                    bot.send_message(chat_id, "Error: category not found.", reply_markup=edit_action_keyboard())
            else:
                bot.send_message(chat_id, "Invalid URL. Try again or press Cancel.",
                                 reply_markup=edit_action_keyboard())

            data.pop('edit_cat', None)
            data['edit_phase'] = 'choose_action'
            user_data[chat_id] = data
            return


        # 5) Установить все вкусы категории на ноль
        if phase == 'choose_cat_zero':
            if text == "⬅️ Back":
                data['edit_phase'] = 'choose_action'
                bot.send_message(chat_id, "Back to editing menu:", reply_markup=edit_action_keyboard())
                user_data[chat_id] = data
                return
            if text == "❌ Cancel":
                data['edit_phase'] = None
                data['edit_cat'] = None
                # 1) Убираем reply-клавиатуру
                bot.send_message(chat_id,
                                 "Editing cancelled.",
                                 reply_markup=types.ReplyKeyboardRemove())
                # 2) Показываем inline-меню
                bot.send_message(chat_id,
                                 t(chat_id, "choose_category"),
                                 reply_markup=get_inline_main_menu(chat_id))
                user_data[chat_id] = data
                return

            if text in menu:
                cat0 = text
                for itm in menu[cat0]["flavors"]:
                    itm["stock"] = 0
                with open(MENU_PATH, "w", encoding="utf-8") as f:
                    json.dump(menu, f, ensure_ascii=False, indent=2)
                bot.send_message(chat_id, f"All flavors in category '{cat0}' set to 0 stock.",
                                 reply_markup=edit_action_keyboard())
                data.pop('edit_cat', None)
                data['edit_phase'] = 'choose_action'
                user_data[chat_id] = data
            else:
                kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
                kb.add("⬅️ Back", "❌ Cancel")
                bot.send_message(chat_id, "Select a valid category to zero out:", reply_markup=kb)
            return

        # 6) Удалить категорию
        if phase == 'remove_category':
            if text == "⬅️ Back":
                data['edit_phase'] = 'choose_action'
                bot.send_message(chat_id, "Back to editing menu:", reply_markup=edit_action_keyboard())
                user_data[chat_id] = data
                return
            if text == "❌ Cancel":
                data['edit_phase'] = None
                data['edit_cat'] = None
                # 1) Убираем reply-клавиатуру
                bot.send_message(chat_id,
                                 "Editing cancelled.",
                                 reply_markup=types.ReplyKeyboardRemove())
                # 2) Показываем inline-меню
                bot.send_message(chat_id,
                                 t(chat_id, "choose_category"),
                                 reply_markup=get_inline_main_menu(chat_id))
                user_data[chat_id] = data
                return

            if text in menu:
                del menu[text]
                with open(MENU_PATH, "w", encoding="utf-8") as f:
                    json.dump(menu, f, ensure_ascii=False, indent=2)
                data['edit_phase'] = 'choose_action'
                bot.send_message(chat_id, f"Category '{text}' removed.", reply_markup=edit_action_keyboard())
                user_data[chat_id] = data
            else:
                kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
                kb.add("⬅️ Back", "❌ Cancel")
                bot.send_message(chat_id, "Select a valid category.", reply_markup=kb)
            return

        # ——————————————————————————————————————————
        if phase == 'rename_category_select':
            if text == "⬅️ Back":
                data['edit_phase'] = 'choose_action'
                bot.send_message(chat_id, "Back to editing menu:", reply_markup=edit_action_keyboard())
                user_data[chat_id] = data
                return
            if text == "❌ Cancel":
                data['edit_phase'] = None
                bot.send_message(chat_id, "Editing cancelled.", reply_markup=types.ReplyKeyboardRemove())
                bot.send_message(chat_id, t(chat_id, "choose_category"), reply_markup=get_inline_main_menu(chat_id))
                user_data[chat_id] = data
                return
            if text in menu:
                data['edit_cat'] = text
                data['edit_phase'] = 'rename_category_enter'
                bot.send_message(
                    chat_id,
                    f"Enter new name for category «{text}»:",
                    reply_markup=types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
                    .add("⬅️ Back", "❌ Cancel")
                )
                user_data[chat_id] = data
                return
            # Если ввели несуществующую категорию
            bot.send_message(chat_id, "Select a valid category or press Cancel.")
            return
        # ——————————————————————————————————————————
        if phase == 'rename_category_enter':
            old_name = data.get('edit_cat')
            if text == "⬅️ Back":
                data['edit_phase'] = 'rename_category_select'
                # показать список категорий заново
                kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
                for cat_key in menu:
                    kb.add(cat_key)
                kb.add("⬅️ Back", "❌ Cancel")
                bot.send_message(chat_id, "Select a category to rename:", reply_markup=kb)
                user_data[chat_id] = data
                return
            if text == "❌ Cancel":
                data['edit_phase'] = None
                bot.send_message(chat_id, "Editing cancelled.", reply_markup=types.ReplyKeyboardRemove())
                bot.send_message(chat_id, t(chat_id, "choose_category"), reply_markup=get_inline_main_menu(chat_id))
                user_data[chat_id] = data
                return
            new_name = text.strip()
            if not new_name or new_name in menu:
                bot.send_message(chat_id, "Invalid or already existing name. Try again:")
                return
            # Переименование
            menu[new_name] = menu.pop(old_name)
            with open(MENU_PATH, "w", encoding="utf-8") as f:
                json.dump(menu, f, ensure_ascii=False, indent=2)
            bot.send_message(chat_id, f"Category “{old_name}” renamed to “{new_name}”.",
                             reply_markup=edit_action_keyboard())
            data['edit_phase'] = 'choose_action'
            data.pop('edit_cat', None)
            user_data[chat_id] = data
            return

        # 7) Выбрать категорию для Fix Price
        if phase == 'choose_fix_price_cat':
            if text == "⬅️ Back":
                data['edit_phase'] = 'choose_action'
                bot.send_message(chat_id, "Back to editing menu:", reply_markup=edit_action_keyboard())
                user_data[chat_id] = data
                return
            if text == "❌ Cancel":
                data['edit_phase'] = None
                data['edit_cat'] = None
                # 1) Убираем reply-клавиатуру
                bot.send_message(chat_id,
                                 "Editing cancelled.",
                                 reply_markup=types.ReplyKeyboardRemove())
                # 2) Показываем inline-меню
                bot.send_message(chat_id,
                                 t(chat_id, "choose_category"),
                                 reply_markup=get_inline_main_menu(chat_id))
                user_data[chat_id] = data
                return

            if text in menu:
                data['edit_cat'] = text
                data['edit_phase'] = 'enter_new_price'
                kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
                kb.add("⬅️ Back", "❌ Cancel")
                bot.send_message(chat_id, f"Enter new price in ₺ for category '{text}':", reply_markup=kb)
                user_data[chat_id] = data
            else:
                kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
                kb.add("⬅️ Back", "❌ Cancel")
                bot.send_message(chat_id, "Choose a category from the list.", reply_markup=kb)
            return

        # 8) Ввод новой цены для категории
        if phase == 'enter_new_price':
            if text == "⬅️ Back":
                data['edit_phase'] = 'choose_action'
                bot.send_message(chat_id, "Back to editing menu:", reply_markup=edit_action_keyboard())
                user_data[chat_id] = data
                return
            if text == "❌ Cancel":
                data['edit_phase'] = None
                data['edit_cat'] = None
                # 1) Убираем reply-клавиатуру
                bot.send_message(chat_id,
                                 "Editing cancelled.",
                                 reply_markup=types.ReplyKeyboardRemove())
                # 2) Показываем inline-меню
                bot.send_message(chat_id,
                                 t(chat_id, "choose_category"),
                                 reply_markup=get_inline_main_menu(chat_id))
                user_data[chat_id] = data
                return

            cat0 = data.get('edit_cat')
            try:
                new_price = float(text.strip())
            except:
                kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
                kb.add("⬅️ Back", "❌ Cancel")
                bot.send_message(chat_id, "Invalid price format. Enter a number, e.g. 1500:", reply_markup=kb)
                return

            menu[cat0]["price"] = int(new_price)
            with open(MENU_PATH, "w", encoding="utf-8") as f:
                json.dump(menu, f, ensure_ascii=False, indent=2)

            bot.send_message(chat_id, f"Price for category '{cat0}' set to {int(new_price)}₺.",
                             reply_markup=edit_action_keyboard())
            data.pop('edit_cat', None)
            data['edit_phase'] = 'choose_action'
            user_data[chat_id] = data
            return

        # 9) Выбрать категорию для ALL IN
        if phase == 'choose_all_in_cat':
            if text == "⬅️ Back":
                data['edit_phase'] = 'choose_action'
                bot.send_message(chat_id, "Back to editing menu:", reply_markup=edit_action_keyboard())
                user_data[chat_id] = data
                return
            if text == "❌ Cancel":
                data['edit_phase'] = None
                data['edit_cat'] = None
                # 1) Убираем reply-клавиатуру
                bot.send_message(chat_id,
                                 "Editing cancelled.",
                                 reply_markup=types.ReplyKeyboardRemove())
                # 2) Показываем inline-меню
                bot.send_message(chat_id,
                                 t(chat_id, "choose_category"),
                                 reply_markup=get_inline_main_menu(chat_id))
                user_data[chat_id] = data
                return

            if text in menu:
                data['edit_cat'] = text
                current_list = []
                for itm in menu[text]["flavors"]:
                    current_list.append(f"{itm['flavor']} - {itm.get('stock', 0)}")
                joined = "\n".join(current_list) if current_list else "(empty)"
                kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
                kb.add("⬅️ Back", "❌ Cancel")
                bot.send_message(
                    chat_id,
                    f"Current flavors in '{text}' (one per line as \"Name - qty\"):\n\n{joined}\n\n"
                    "Send the full updated list in the same format. Each line: “Name - qty”.",
                    reply_markup=kb
                )
                data['edit_phase'] = 'replace_all_in'
                user_data[chat_id] = data
            else:
                kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
                kb.add("⬅️ Back", "❌ Cancel")
                bot.send_message(chat_id, "Choose a valid category from the list.", reply_markup=kb)
            return

        # 10) Заменить полный список вкусов (ALL IN)
        if phase == 'replace_all_in':
            if text == "⬅️ Back":
                data['edit_phase'] = 'choose_action'
                bot.send_message(chat_id, "Back to editing menu:", reply_markup=edit_action_keyboard())
                user_data[chat_id] = data
                return
            if text == "❌ Cancel":
                data['edit_phase'] = None
                data['edit_cat'] = None
                # 1) Убираем reply-клавиатуру
                bot.send_message(chat_id,
                                 "Editing cancelled.",
                                 reply_markup=types.ReplyKeyboardRemove())
                # 2) Показываем inline-меню
                bot.send_message(chat_id,
                                 t(chat_id, "choose_category"),
                                 reply_markup=get_inline_main_menu(chat_id))
                user_data[chat_id] = data
                return

            cat0 = data.get('edit_cat')
            lines = text.strip().splitlines()
            new_flavors = []
            for line in lines:
                stripped = line.strip()
                if not stripped or stripped.lower() == "(empty)":
                    continue
                if '-' in stripped:
                    parts = stripped.rsplit('-', 1)
                else:
                    continue
                name = parts[0].strip()
                qty_part = parts[-1].strip()
                if not qty_part.isdigit() or not name:
                    continue
                qty = int(qty_part)
                new_flavors.append({
                    "emoji": "",
                    "flavor": name,
                    "stock": qty,
                    "tags": [],
                    "description_ru": "",
                    "description_en": "",
                    "photo_url": ""
                })
            menu[cat0]["flavors"] = new_flavors
            with open(MENU_PATH, "w", encoding="utf-8") as f:
                json.dump(menu, f, ensure_ascii=False, indent=2)

            bot.send_message(chat_id, f"Full flavor list for '{cat0}' replaced.", reply_markup=edit_action_keyboard())
            data.pop('edit_cat', None)
            data['edit_phase'] = 'choose_action'
            user_data[chat_id] = data
            return

        # 11) Выбрать категорию для Actual Flavor (обновлённый список)
        if phase == 'choose_cat_actual':
            if text == "⬅️ Back":
                data['edit_phase'] = 'choose_action'
                bot.send_message(chat_id, "Back to editing menu:", reply_markup=edit_action_keyboard())
                user_data[chat_id] = data
                return
            if text == "❌ Cancel":
                data['edit_phase'] = None
                data['edit_cat'] = None
                # 1) Убираем reply-клавиатуру
                bot.send_message(chat_id,
                                 "Editing cancelled.",
                                 reply_markup=types.ReplyKeyboardRemove())
                # 2) Показываем inline-меню
                bot.send_message(chat_id,
                                 t(chat_id, "choose_category"),
                                 reply_markup=get_inline_main_menu(chat_id))
                user_data[chat_id] = data
                return

            if text in menu:
                # Сохраняем выбранную категорию и переходим к выбору вкуса
                data['edit_cat'] = text
                data['edit_phase'] = 'choose_flavor_actual'

                # Формируем клавиатуру с теми вкусами, в которых stock > 0
                kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
                any_in_stock = False
                for itm in menu[text]["flavors"]:
                    stock = itm.get("stock", 0)
                    if isinstance(stock, str) and stock.isdigit():
                        stock = int(stock)
                        itm["stock"] = stock
                    if isinstance(stock, int) and stock > 0:
                        any_in_stock = True
                        kb.add(f"{itm['flavor']} (current: {stock})")
                if not any_in_stock:
                    bot.send_message(
                        chat_id,
                        f"No flavors with stock > 0 in category '{text}'.",
                        reply_markup=edit_action_keyboard()
                    )
                    data.pop('edit_cat', None)
                    data['edit_phase'] = 'choose_action'
                    user_data[chat_id] = data
                    return

                kb.add("⬅️ Back", "❌ Cancel")
                bot.send_message(
                    chat_id,
                    f"Select a flavor from category '{text}' to update its stock:",
                    reply_markup=kb
                )
                user_data[chat_id] = data
            else:
                kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
                kb.add("⬅️ Back", "❌ Cancel")
                bot.send_message(chat_id, "Choose a valid category from the list:", reply_markup=kb)
            return

        # 12) Фаза 'choose_flavor_actual' — получаем выбор одного вкуса и запрашиваем новую qty
        if phase == 'choose_flavor_actual':
            if text == "⬅️ Back":
                data['edit_phase'] = 'choose_action'
                bot.send_message(chat_id, "Back to editing menu:", reply_markup=edit_action_keyboard())
                user_data[chat_id] = data
                return
            if text == "❌ Cancel":
                data['edit_phase'] = None
                data['edit_cat'] = None
                # 1) Убираем reply-клавиатуру
                bot.send_message(chat_id,
                                 "Editing cancelled.",
                                 reply_markup=types.ReplyKeyboardRemove())
                # 2) Показываем inline-меню
                bot.send_message(chat_id,
                                 t(chat_id, "choose_category"),
                                 reply_markup=get_inline_main_menu(chat_id))
                user_data[chat_id] = data
                return

            cat0 = data.get('edit_cat')
            if not cat0 or cat0 not in menu:
                bot.send_message(chat_id, "Error: category not found.", reply_markup=edit_action_keyboard())
                data.pop('edit_cat', None)
                data['edit_phase'] = 'choose_action'
                user_data[chat_id] = data
                return

            # Пытаемся сопоставить введённый текст с форматом "Flavor (current: X)"
            chosen_flavor = None
            for itm in menu[cat0]["flavors"]:
                name = itm["flavor"]
                display_label = f"{name} (current: {itm.get('stock', 0)})"
                if text == display_label:
                    chosen_flavor = name
                    break

            if not chosen_flavor:
                kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
                for itm in menu[cat0]["flavors"]:
                    if itm.get("stock", 0) > 0:
                        kb.add(f"{itm['flavor']} (current: {itm['stock']})")
                kb.add("⬅️ Back", "❌ Cancel")
                bot.send_message(chat_id, "Select a valid flavor (in stock > 0):", reply_markup=kb)
                return

            # Сохраняем выбранный вкус и просим ввести новую quantity
            data['edit_flavor'] = chosen_flavor
            data['edit_phase'] = 'enter_actual_qty'
            bot.send_message(
                chat_id,
                f"Enter the new stock quantity for '{chosen_flavor}':",
                reply_markup=types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
                .add("⬅️ Back", "❌ Cancel")
            )
            user_data[chat_id] = data
            return

        # 13) Фаза 'enter_actual_qty' — получаем новую qty и обновляем stock
        if phase == 'enter_actual_qty':
            if text == "⬅️ Back":
                data['edit_phase'] = 'choose_action'
                bot.send_message(chat_id, "Back to editing menu:", reply_markup=edit_action_keyboard())
                user_data[chat_id] = data
                return
            if text == "❌ Cancel":
                data['edit_phase'] = None
                data['edit_cat'] = None
                # 1) Убираем reply-клавиатуру
                bot.send_message(chat_id,
                                 "Editing cancelled.",
                                 reply_markup=types.ReplyKeyboardRemove())
                # 2) Показываем inline-меню
                bot.send_message(chat_id,
                                 t(chat_id, "choose_category"),
                                 reply_markup=get_inline_main_menu(chat_id))
                user_data[chat_id] = data
                return

            # Проверяем, что введён неотрицательный integer
            if not text.isdigit():
                bot.send_message(chat_id, "Invalid number. Please enter a non-negative integer:")
                return

            new_qty = int(text)
            cat0 = data.get('edit_cat')
            flavor0 = data.get('edit_flavor')

            # Находим и обновляем соответствующий объект вкуса
            updated = False
            for itm in menu.get(cat0, {}).get("flavors", []):
                if itm["flavor"] == flavor0:
                    itm["stock"] = new_qty
                    updated = True
                    break

            if not updated:
                bot.send_message(chat_id, f"Error: flavor '{flavor0}' not found in '{cat0}'.",
                                 reply_markup=edit_action_keyboard())
            else:
                # Сохраняем JSON на диск
                with open(MENU_PATH, "w", encoding="utf-8") as f:
                    json.dump(menu, f, ensure_ascii=False, indent=2)

                bot.send_message(
                    chat_id,
                    f"Stock for '{flavor0}' in category '{cat0}' has been updated to {new_qty}.",
                    reply_markup=edit_action_keyboard()
                )

            # Очищаем данные и возвращаемся в главное меню редактирования
            data.pop('edit_cat', None)
            data.pop('edit_flavor', None)
            data['edit_phase'] = 'choose_action'
            user_data[chat_id] = data
            return

        # Если ни одна фаза не совпала, возвращаем пользователя в меню редактирования
        data['edit_phase'] = 'choose_action'
        bot.send_message(chat_id, "Back to editing menu:", reply_markup=edit_action_keyboard())
        user_data[chat_id] = data
        return
    # ────────────────────────────────────────────────────────────────────────────────

    # Остальной universal_handler (cart-функции, /history, /stats, /help и т.д.)
    # ... (тот же код, что и ранее, без изменений) ...

    # ——— Режим редактирования корзины — (оставляем без изменений) ———
    if data.get('edit_cart_phase'):
        if data['edit_cart_phase'] == 'choose_action':
            if text == t(chat_id, "back"):
                data['edit_cart_phase'] = None
                data['edit_index'] = None
                bot.send_message(chat_id, t(chat_id, "choose_category"), reply_markup=get_inline_main_menu(chat_id))
                user_data[chat_id] = data
                return

            if text.startswith(f"{t(chat_id, 'remove_item')} "):
                try:
                    idx = int(text.split()[1]) - 1
                except:
                    bot.send_message(chat_id, t(chat_id, "error_invalid"), reply_markup=get_inline_main_menu(chat_id))
                    data['edit_cart_phase'] = None
                    data['edit_index'] = None
                    user_data[chat_id] = data
                    return

                grouped = {}
                for item in data['cart']:
                    key = (item['category'], item['flavor'], item['price'])
                    grouped[key] = grouped.get(key, 0) + 1
                items_list = list(grouped.items())
                if idx < 0 or idx >= len(items_list):
                    bot.send_message(chat_id, t(chat_id, "error_invalid"), reply_markup=get_inline_main_menu(chat_id))
                    data['edit_cart_phase'] = None
                    data['edit_index'] = None
                    user_data[chat_id] = data
                    return

                key_to_remove, _ = items_list[idx]
                new_cart = [it for it in data['cart'] if not (
                            it['category'] == key_to_remove[0] and it['flavor'] == key_to_remove[1] and it['price'] ==
                            key_to_remove[2])]
                data['cart'] = new_cart
                data['edit_cart_phase'] = None
                data['edit_index'] = None
                bot.send_message(chat_id, t(chat_id, "item_removed").format(flavor=key_to_remove[1]),
                                 reply_markup=get_inline_main_menu(chat_id))
                user_data[chat_id] = data
                return

            if text.startswith(f"{t(chat_id, 'edit_item')} "):
                try:
                    idx = int(text.split()[1]) - 1
                except:
                    bot.send_message(chat_id, t(chat_id, "error_invalid"), reply_markup=get_inline_main_menu(chat_id))
                    data['edit_cart_phase'] = None
                    user_data[chat_id] = data
                    return

                grouped = {}
                for item in data['cart']:
                    key = (item['category'], item['flavor'], item['price'])
                    grouped[key] = grouped.get(key, 0) + 1
                items_list = list(grouped.items())
                if idx < 0 or idx >= len(items_list):
                    bot.send_message(chat_id, t(chat_id, "error_invalid"), reply_markup=get_inline_main_menu(chat_id))
                    data['edit_cart_phase'] = None
                    user_data[chat_id] = data
                    return

                data['edit_index'] = idx
                data['edit_cart_phase'] = 'enter_qty'
                key_chosen, count = items_list[idx]
                cat0, flavor0, price0 = key_chosen
                bot.send_message(
                    chat_id,
                    f"Current item: {cat0} — {flavor0} — {price0}₺ (in cart {count} pcs).\n{t(chat_id, 'enter_new_qty')}"
                )
                user_data[chat_id] = data
                return

        if data['edit_cart_phase'] == 'enter_qty':
            if text == t(chat_id, "back"):
                data['edit_cart_phase'] = None
                data['edit_index'] = None
                bot.send_message(chat_id, t(chat_id, "choose_category"), reply_markup=get_inline_main_menu(chat_id))
                user_data[chat_id] = data
                return
            if not text.isdigit():
                bot.send_message(chat_id, t(chat_id, "error_invalid"))
                return
            new_qty = int(text)
            grouped = {}
            for item in data['cart']:
                key = (item['category'], item['flavor'], item['price'])
                grouped[key] = grouped.get(key, 0) + 1
            items_list = list(grouped.items())
            idx = data['edit_index']
            if idx < 0 or idx >= len(items_list):
                bot.send_message(chat_id, t(chat_id, "error_invalid"), reply_markup=get_inline_main_menu(chat_id))
                data['edit_cart_phase'] = None
                data['edit_index'] = None
                user_data[chat_id] = data
                return
            key_chosen, old_count = items_list[idx]
            cat0, flavor0, price0 = key_chosen

            data['cart'] = [it for it in data['cart'] if
                            not (it['category'] == cat0 and it['flavor'] == flavor0 and it['price'] == price0)]
            for _ in range(new_qty):
                data['cart'].append({'category': cat0, 'flavor': flavor0, 'price': price0})

            data['edit_cart_phase'] = None
            data['edit_index'] = None
            if new_qty == 0:
                bot.send_message(chat_id, t(chat_id, "item_removed").format(flavor=flavor0),
                                 reply_markup=get_inline_main_menu(chat_id))
            else:
                bot.send_message(chat_id, t(chat_id, "qty_changed").format(flavor=flavor0, qty=new_qty),
                                 reply_markup=get_inline_main_menu(chat_id))
            user_data[chat_id] = data
            return

    # ——— «Корзина» через Reply-кнопку ———
    if text.startswith(f"{t(chat_id, 'remove_item')} "):
        try:
            idx = int(text.split()[1]) - 1
        except:
            bot.send_message(chat_id, t(chat_id, "error_invalid"), reply_markup=get_inline_main_menu(chat_id))
            data['edit_cart_phase'] = None
            data['edit_index'] = None
            user_data[chat_id] = data
            return

        grouped = {}
        for item in data['cart']:
            key = (item['category'], item['flavor'], item['price'])
            grouped[key] = grouped.get(key, 0) + 1
        items_list = list(grouped.items())
        if idx < 0 or idx >= len(items_list):
            bot.send_message(chat_id, t(chat_id, "error_invalid"), reply_markup=get_inline_main_menu(chat_id))
            data['edit_cart_phase'] = None
            user_data[chat_id] = data
            return

        (cat0, flavor0, price0), _ = items_list[idx]
        removed = False
        new_cart = []
        for it in data["cart"]:
            if not removed and it["category"] == cat0 and it["flavor"] == flavor0 and it["price"] == price0:
                removed = True
                continue
            new_cart.append(it)
        data['cart'] = new_cart
        data['edit_cart_phase'] = None
        data['edit_index'] = None
        bot.send_message(chat_id, t(chat_id, "item_removed").format(flavor=flavor0),
                         reply_markup=get_inline_main_menu(chat_id))
        user_data[chat_id] = data
        return

    # ——— Если ожидаем ввод адреса ———
    if data.get('wait_for_address'):
        if text == t(chat_id, "back"):
            data['wait_for_address'] = False
            data['current_category'] = None
            bot.send_message(chat_id, t(chat_id, "choose_category"), reply_markup=get_inline_main_menu(chat_id))
            user_data[chat_id] = data
            return

        if text == t(chat_id, "choose_on_map"):
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
        elif text == t(chat_id, "enter_address_text"):
            bot.send_message(chat_id, t(chat_id, "enter_address"), reply_markup=types.ReplyKeyboardRemove())
            return
        elif message.content_type == 'text' and message.text:
            address = message.text.strip()
        else:
            bot.send_message(chat_id, t(chat_id, "error_invalid"), reply_markup=address_keyboard(chat_id))
            return

        data['address'] = address
        data['wait_for_address'] = False
        data['wait_for_contact'] = True
        kb = contact_keyboard(chat_id)
        bot.send_message(chat_id, t(chat_id, "enter_contact"), reply_markup=kb)
        user_data[chat_id] = data
        return

    # ——— Если ожидаем ввод контакта ———
    if data.get('wait_for_contact'):
        if text == t(chat_id, "back"):
            data['wait_for_address'] = True
            data['wait_for_contact'] = False
            bot.send_message(chat_id, t(chat_id, "enter_address"), reply_markup=address_keyboard())
            user_data[chat_id] = data
            return

        if text == t(chat_id, "enter_nickname"):
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
        kb = comment_keyboard(chat_id)
        bot.send_message(chat_id, t(chat_id, "enter_comment"), reply_markup=kb)
        user_data[chat_id] = data
        return

    # ——— Если ожидаем ввод комментария ———
    if data.get('wait_for_comment'):
        if text == t(chat_id, "back"):
            data['wait_for_contact'] = True
            data['wait_for_comment'] = False
            bot.send_message(chat_id, t(chat_id, "enter_contact"), reply_markup=contact_keyboard())
            user_data[chat_id] = data
            return

        if text == t(chat_id, "enter_comment"):
            bot.send_message(chat_id, t(chat_id, "enter_comment"), reply_markup=types.ReplyKeyboardRemove())
            return

        if message.content_type == 'text' and text != t(chat_id, "send_order"):
            data['comment'] = text.strip()
            bot.send_message(chat_id, t(chat_id, "comment_saved"), reply_markup=comment_keyboard(chat_id))

            user_data[chat_id] = data
            return

        if text == t(chat_id, "send_order"):
            cart = data.get('cart', [])
            if not cart:
                bot.send_message(chat_id, t(chat_id, "cart_empty"))
                return

            total_try = sum(i['price'] for i in cart)
            discount = data.pop("pending_discount", 0)
            total_after = max(total_try - discount, 0)

            needed = {}
            for it in cart:
                key = (it["category"], it["flavor"])
                needed[key] = needed.get(key, 0) + 1

            for (cat0, flavor0), qty_needed in needed.items():
                item_obj = next((i for i in menu[cat0]["flavors"] if i["flavor"] == flavor0), None)
                if not item_obj or item_obj.get("stock", 0) < qty_needed:
                    bot.send_message(chat_id, f"😕 К сожалению, «{flavor0}» больше не доступен в нужном количестве.")
                    return

            for (cat0, flavor0), qty_needed in needed.items():
                for itm in menu[cat0]["flavors"]:
                    if itm["flavor"] == flavor0:
                        itm["stock"] -= qty_needed
                        break
            with open(MENU_PATH, "w", encoding="utf-8") as f:
                json.dump(menu, f, ensure_ascii=False, indent=2)

            items_json = json.dumps(cart, ensure_ascii=False)
            now = datetime.datetime.utcnow().isoformat()
            conn_local = get_db_connection()
            cursor_local = conn_local.cursor()
            cursor_local.execute(
                "INSERT INTO orders (chat_id, items_json, total, timestamp) VALUES (?, ?, ?, ?)",
                (chat_id, items_json, total_after, now)
            )
            conn_local.commit()
            order_id = cursor_local.lastrowid

            earned = total_after // 30
            if earned > 0:
                cursor_local.execute("UPDATE users SET points = points + ? WHERE chat_id = ?", (earned, chat_id))
                conn_local.commit()
                bot.send_message(chat_id, f"👍 Вы получили {earned} бонусных баллов за этот заказ.")

            cursor_local.execute("SELECT referred_by FROM users WHERE chat_id = ?", (chat_id,))
            row = cursor_local.fetchone()
            if row and row[0]:
                inviter = row[0]
                cursor_local.execute("UPDATE users SET points = points + 200 WHERE chat_id = ?", (inviter,))
                conn_local.commit()
                bot.send_message(inviter, "🎉 Вам начислено 200 бонусных баллов за приглашение нового клиента!")
                cursor_local.execute("UPDATE users SET referred_by = NULL WHERE chat_id = ?", (chat_id,))
                conn_local.commit()

            cursor_local.close()
            conn_local.close()

            summary = "\n".join(f"{i['category']}: {i['flavor']} — {i['price']}₺" for i in cart)
            rates = fetch_rates()
            rub = round(total_after * rates.get("RUB", 0) + 500, 2)
            usd = round(total_after * rates.get("USD", 0) + 2, 2)
            uah = round(total_after * rates.get("UAH", 0) + 200, 2)
            eur = round(total_after * rates.get("EUR", 0) + 2, 2)  # новая строка
            conv = f"({rub}₽, ${usd}, €{eur}, ₴{uah})"

            full_rus = (
                f"📥 Новый заказ от @{message.from_user.username or message.from_user.first_name}:\n\n"
                f"{summary}\n\n"
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
                f"{summary}\n\n"
                f"Total: {total_after}₺ {conv}\n"
                f"📍 Address: {data.get('address', '—')}\n"
                f"📱 Contact: {data.get('contact', '—')}\n"
                f"💬 Comment: {comment_en}"
            )
            # вместо:
            # bot.send_message(GROUP_CHAT_ID, full_en)

            # создаём клавиатуру
            kb = types.InlineKeyboardMarkup()
            kb.add(types.InlineKeyboardButton(
                text="❌ Отменить заказ",
                callback_data=f"cancel_order|{order_id}"
            ))

            # отправляем админам вместе с кнопкой
            bot.send_message(
                GROUP_CHAT_ID,
                full_en,
                reply_markup=kb
            )

    # ────────────────────────────────────────────────────────────────────────────────

    # ——— «Back» во всём остальном ———
    if text == t(chat_id, "back"):
        data.update({
            "current_category": None,
            "wait_for_points": False,
            "wait_for_address": False,
            "wait_for_contact": False,
            "wait_for_comment": False
        })
        bot.send_message(chat_id, t(chat_id, "choose_category"), reply_markup=get_inline_main_menu(chat_id))
        user_data[chat_id] = data
        return

    # ——— Очистка корзины (Reply-кнопка) ———
    if text == f"🗑️ {t(chat_id, 'clear_cart')}":
        data["cart"] = []
        data["current_category"] = None
        data["wait_for_points"] = False
        data["wait_for_address"] = False
        data["wait_for_contact"] = False
        data["wait_for_comment"] = False
        bot.send_message(chat_id, t(chat_id, "cart_cleared"), reply_markup=get_inline_main_menu(chat_id))
        user_data[chat_id] = data
        return

    # ——— «Add more» ———
    if text == f"➕ {t(chat_id, 'add_more')}":
        data["current_category"] = None
        bot.send_message(chat_id, t(chat_id, "choose_category"), reply_markup=get_inline_main_menu(chat_id))
        user_data[chat_id] = data
        return

    # ——— Завершить заказ по Reply-кнопке ———
    if text == f"✅ {t(chat_id, 'finish_order')}":
        cart = data.get("cart", [])
        if not cart:
            bot.send_message(chat_id, t(chat_id, "cart_empty"))
            return
        total_try = sum(i['price'] for i in cart)

        conn_local = get_db_connection()
        cursor_local = conn_local.cursor()
        cursor_local.execute("SELECT points FROM users WHERE chat_id = ?", (chat_id,))
        row = cursor_local.fetchone()
        cursor_local.close()
        conn_local.close()

        user_points = row[0] if row else 0

        if user_points > 0:
            max_points = min(user_points, total_try)
            points_try = user_points * 1
            msg = (
                    t(chat_id, "points_info").format(points=user_points, points_try=points_try)
                    + "\n"
                    + t(chat_id, "enter_points").format(max_points=max_points)
            )
            bot.send_message(chat_id, msg, reply_markup=types.ReplyKeyboardRemove())
            data["wait_for_points"] = True
            data["temp_total_try"] = total_try
            data["temp_user_points"] = user_points
            user_data[chat_id] = data
        else:
            kb = address_keyboard(chat_id)

            bot.send_message(
                chat_id,
                f"🛒 {t(chat_id, 'view_cart')}:\n\n" +
                "\n".join(f"{i['category']}: {i['flavor']} — {i['price']}₺" for i in data['cart']) +
                f"\n\n{t(chat_id, 'enter_address')}",
                reply_markup=kb
            )
            data["wait_for_address"] = True
            user_data[chat_id] = data
        return

    # ——— Выбор категории (Reply-клавиатура fallback) ———
    if text in menu:
        data['current_category'] = text
        bot.send_message(chat_id, f"{t(chat_id, 'choose_flavor')} «{text}»",
                         reply_markup=get_inline_flavors(chat_id, text))
        user_data[chat_id] = data
        return

    # ——— Выбор вкуса (Reply-клавиатура fallback) ———
    cat0 = data.get('current_category')
    if cat0:
        price = menu[cat0]["price"]
        for it in menu[cat0]["flavors"]:
            if it.get("stock", 0) > 0:
                emoji = it.get("emoji", "")
                flavor0 = it["flavor"]
                label = f"{emoji} {flavor0} — {price}₺ [{it['stock']} шт]"
                if text == label:
                    data['cart'].append({'category': cat0, 'flavor': flavor0, 'price': price})
                    template = t(chat_id, "added_to_cart")
                    suffix = template.split("»", 1)[1].strip()
                    count = len(data['cart'])
                    bot.send_message(
                        chat_id,
                        f"«{cat0} — {flavor0}» {suffix.format(flavor=flavor0, count=count)}",
                        reply_markup=get_inline_main_menu(chat_id)
                    )
                    user_data[chat_id] = data
                    return

        # если ввод не соответствует ни одному вкусу, показать только кнопку «Назад к категориям»
        kb = types.InlineKeyboardMarkup()
        kb.add(types.InlineKeyboardButton(
            text=f"⬅️ {t(chat_id, 'back_to_categories')}",
            callback_data="go_back_to_categories"
        ))
        bot.send_message(
            chat_id,
            t(chat_id, "error_invalid"),
            reply_markup=kb
        )
        return

    # ——— /history ———
    if text == "/history":
        conn_local = get_db_connection()
        cursor_local = conn_local.cursor()
        cursor_local.execute(
            "SELECT order_id, items_json, total, timestamp FROM orders WHERE chat_id = ? ORDER BY timestamp DESC",
            (chat_id,)
        )
        rows = cursor_local.fetchall()
        cursor_local.close()
        conn_local.close()

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



@bot.callback_query_handler(func=lambda call: call.data and call.data.startswith("cancel_order|"))
def handle_cancel_order(call):
    user_id = call.from_user.id
    if user_id not in ADMINS:
        return bot.answer_callback_query(call.id, "Нет доступа", show_alert=True)

    _, oid = call.data.split("|", 1)
    order_id = int(oid)

    conn = get_db_connection()
    cursor = conn.cursor()
    # Теперь вытягиваем как points_spent (списано при заказе), так и points_earned (начислено за заказ)
    cursor.execute(
        "SELECT chat_id, items_json, points_spent, points_earned "
        "FROM orders WHERE order_id = ?",
        (order_id,)
    )
    row = cursor.fetchone()
    if not row:
        cursor.close()
        conn.close()
        return bot.answer_callback_query(call.id, "Заказ не найден", show_alert=True)

    user_chat_id, items_json, pts_spent, pts_earned = row
    items = json.loads(items_json)

    # 1) Возвращаем товары на склад (как раньше)
    for it in items:
        cat = it["category"]
        flavor = it["flavor"]
        found = False
        for itm in menu[cat]["flavors"]:
            if itm["flavor"] == flavor:
                itm["stock"] = itm.get("stock", 0) + 1
                found = True
                break
        if not found:
            # если вдруг вкус отсутствует — добавим его
            menu[cat]["flavors"].append({
                "flavor": flavor,
                "stock": 1,
                "emoji": "",
                "tags": [],
                "description_ru": "",
                "description_en": "",
                "photo_url": ""
            })

    # сохраняем menu.json
    with open(MENU_PATH, "w", encoding="utf-8") as f:
        json.dump(menu, f, ensure_ascii=False, indent=2)

    # 2) Возвращаем списанные баллы пользователю
    if pts_spent:
        cursor.execute(
            "UPDATE users SET points = points + ? WHERE chat_id = ?",
            (pts_spent, user_chat_id)
        )
        conn.commit()

    # 3) Убираем ранее начисленные за этот заказ баллы
    if pts_earned:
        cursor.execute(
            "UPDATE users SET points = points - ? WHERE chat_id = ?",
            (pts_earned, user_chat_id)
        )
        conn.commit()

    # 4) Удаляем сам заказ из БД
    cursor.execute("DELETE FROM orders WHERE order_id = ?", (order_id,))
    conn.commit()
    cursor.close()
    conn.close()

    # 5) Уведомляем пользователя
    msg = f"Ваш заказ #{order_id} отменён."
    if pts_spent:
        msg += f" Возвращено {pts_spent} списанных баллов."
    if pts_earned:
        msg += f" Списано {pts_earned} начисленных баллов."
    bot.send_message(user_chat_id, msg)

    # 6) Убираем кнопку «Отменить» в админском сообщении
    bot.edit_message_reply_markup(
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        reply_markup=None
    )
    bot.answer_callback_query(call.id, "Заказ отменён")


# 1) Обработчик нажатия "Order Delivered"
# 1) When “Order Delivered” is pressed, show currency choices (EN only)

# 1) Заказ доставлен → предложить валюту «внутри» того же сообщения
# 1) Нажали «✅ Order Delivered»
# 1) Нажали «✅ Order Delivered»
# 1) Заказ доставлен → предложить валюту «внутри» того же сообщения
@bot.callback_query_handler(func=lambda call: call.data and call.data.startswith("order_delivered|"))
def handle_order_delivered(call: types.CallbackQuery):
    # 1. Логируем факт нажатия (для отладки)
    print(f"[DEBUG] order_delivered invoked: chat_id={call.message.chat.id}, data={call.data}")

    # 2. Проверяем, что это именно наш админ-чат
    if call.message.chat.id != GROUP_CHAT_ID:
        return bot.answer_callback_query(call.id, "Нажали не в том чате", show_alert=True)

    # 3. Подтверждаем callback, чтобы убрать спиннер
    bot.answer_callback_query(call.id)

    # 4. Извлекаем order_id из callback_data
    _, oid = call.data.split("|", 1)
    order_id = int(oid)

    # 5. Формируем клавиатуру выбора валют
    currencies = ["cash", "rub", "dollar", "euro", "uah", "iban", "crypto", "free"]
    kb = types.InlineKeyboardMarkup(row_width=3)
    for cur in currencies:
        kb.add(
            types.InlineKeyboardButton(
                text=cur.upper(),
                callback_data=f"deliver_currency|{order_id}|{cur}"
            )
        )
    kb.add(
        types.InlineKeyboardButton(
            text="⏪ Back",
            callback_data=f"back_to_group|{order_id}"
        )
    )

    # 6. Меняем только inline-кнопки у существующего сообщения
    bot.edit_message_reply_markup(
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        reply_markup=kb
    )

@bot.callback_query_handler(func=lambda call: call.data and call.data.startswith("deliver_currency|"))
def handle_deliver_currency(call: types.CallbackQuery):
    bot.answer_callback_query(call.id)

    _, oid, currency = call.data.split("|", 2)
    order_id = int(oid)

    conn = get_db_connection()
    cur = conn.cursor()

    # проверяем, не было ли уже этой записи
    cur.execute("SELECT 1 FROM delivered_log WHERE order_id = ? LIMIT 1", (order_id,))
    if cur.fetchone():
        cur.close(); conn.close()
        # если уже отмечено — говорим админу и выходим
        return bot.answer_callback_query(call.id, "This order has already been marked delivered.", show_alert=True)

    # дальше ваша логика: считаем qty, заносим в delivered_counts и delivered_log
    cur.execute("SELECT items_json FROM orders WHERE order_id = ?", (order_id,))
    row = cur.fetchone()
    items = json.loads(row[0])
    qty = len(items)

    cur.execute("""
        INSERT INTO delivered_counts(currency, count)
        VALUES (?, ?)
        ON CONFLICT(currency) DO UPDATE
          SET count = delivered_counts.count + excluded.count
    """, (currency, qty))
    conn.commit()

    now = datetime.datetime.utcnow().isoformat()
    cur.execute(
        "INSERT INTO delivered_log(order_id, currency, qty, timestamp) VALUES (?, ?, ?, ?)",
        (order_id, currency, qty, now)
    )
    conn.commit()

    # получаем новые итоги
    cur.execute("SELECT SUM(count) FROM delivered_counts")
    overall_total = cur.fetchone()[0] or 0
    cur.close(); conn.close()

    # обновляем текст (без валютных кнопок)
    original = call.message.text.split("Select payment currency:")[0].rstrip()
    new_text = (
        f"{original}\n\n"
        f"<b>Already delivered:</b>\n"
        f"PAYED IN {currency.upper()}: {qty} pcs\n\n"
        f"<b>Total:</b> {overall_total} pcs"
    )

    # заменяем клавиатуру на ту, что уже не содержит валют
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        types.InlineKeyboardButton("❌ Cancel",   callback_data=f"cancel_order|{order_id}"),
        types.InlineKeyboardButton("✅ Order Delivered", callback_data=f"order_delivered|{order_id}")
    )

    bot.edit_message_text(
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        text=new_text,
        parse_mode="HTML",
        reply_markup=kb
    )


# 3) Нажали «⏪ Back»
@bot.callback_query_handler(func=lambda call: call.data and call.data.startswith("back_to_options|"))
def handle_back_to_options(call: types.CallbackQuery):
    # сразу прекращаем крутилку
    call.answer()
    order_id = int(call.data.split("|", 1)[1])

    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        types.InlineKeyboardButton(text="❌ Cancel",   callback_data=f"cancel_order|{order_id}"),
        types.InlineKeyboardButton(text="✅ Order Delivered", callback_data=f"order_delivered|{order_id}")
    )

    bot.edit_message_reply_markup(
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        reply_markup=kb
    )
# 3) «Back» — возвращаем оригинальную клавиатуру (❌ и ✅) без изменения текста
@bot.callback_query_handler(func=lambda call: call.data and call.data.startswith("back_to_group|"))
def handle_back_to_group(call: types.CallbackQuery):
    bot.answer_callback_query(call.id)
    _, oid = call.data.split("|", 1)
    order_id = int(oid)

    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        types.InlineKeyboardButton(
            text="❌ Cancel",
            callback_data=f"cancel_order|{order_id}"
        ),
        types.InlineKeyboardButton(
            text="✅ Order Delivered",
            callback_data=f"order_delivered|{order_id}"
        )
    )
    bot.edit_message_reply_markup(
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        reply_markup=kb
    )

# ------------------------------------------------------------------------
#   36. Запуск бота
# ------------------------------------------------------------------------
if __name__ == "__main__":
    # 1) Определяем московскую зону
    moscow_tz = pytz.timezone("Europe/Moscow")

    # 2) Создаём BackgroundScheduler с московской TZ
    scheduler = BackgroundScheduler(timezone=moscow_tz)

    # 3) Добавляем задачу ежедневно в 23:55 МСК
    scheduler.add_job(
        send_daily_sold_report,
        trigger='cron',
        hour=23,
        minute=55,
        timezone=moscow_tz    # <- убеждаемся, что триггер знает, что это МСК
    )

    scheduler.start()

    # 4) Для отладки посмотрим, когда следующая отработка
    for job in scheduler.get_jobs():
        print("Next run (UTC):", job.next_run_time)

    # 5) Запускаем бота
    bot.delete_webhook()
    bot.infinity_polling(timeout=10, long_polling_timeout=5)

