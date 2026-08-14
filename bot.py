import os
import json
import html
import hashlib
import requests
import datetime
import random
import re
import string
import sqlite3
import threading
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

ADMIN_ID = int(os.getenv("ADMIN_ID", "424751188"))

# Баллы начисляются вдвое медленнее, чем раньше.
PURCHASE_POINTS_DIVISOR = 60
REFERRAL_BONUS_POINTS = 200

GROUP_CHAT_ID    = int(os.getenv("GROUP_CHAT_ID",    "-1002414380144"))
PERSONAL_CHAT_ID = int(os.getenv("PERSONAL_CHAT_ID", "0"))

# Сами платёжные данные задаются только в закрытых Railway Variables.
# В исходном коде и пользовательском профиле реквизиты не хранятся.
PAYMENT_METHODS = {
    "rub": ("🇷🇺 Рубли", "🇷🇺 Rubles", "PAYMENT_RUB"),
    "iban": ("🇹🇷 IBAN", "🇹🇷 IBAN", "PAYMENT_IBAN"),
    "crypto": ("₿ Крипта", "₿ Crypto", "PAYMENT_CRYPTO"),
    "uah": ("🇺🇦 Гривны", "🇺🇦 Hryvnia", "PAYMENT_UAH"),
}

BOT_VERSION = "2026.08.14-payment-env-cancel-v11"

print("GROUP_CHAT_ID =", GROUP_CHAT_ID, flush=True)
print("BOT_VERSION =", BOT_VERSION, flush=True)
print(
    "PAYMENT_VARIABLES =",
    {
        env_name: bool(os.getenv(env_name, "").strip())
        for _label_ru, _label_en, env_name in PAYMENT_METHODS.values()
    },
    flush=True,
)

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

# Создание таблицы users
cursor_init.execute("""
    CREATE TABLE IF NOT EXISTS users (
        chat_id        INTEGER PRIMARY KEY,
        points         INTEGER DEFAULT 0,
        referral_code  TEXT UNIQUE,
        referred_by    INTEGER,
        last_address   TEXT,
        last_contact   TEXT,
        language       TEXT
    )
""")
# Миграция существующей БД Railway: каждый отсутствующий столбец добавляется
# отдельно, поэтому наличие одного поля не мешает добавить остальные.
cursor_init.execute("PRAGMA table_info(users)")
user_columns = {row[1] for row in cursor_init.fetchall()}
for column_name, column_type in (
    ("last_address", "TEXT"),
    ("last_contact", "TEXT"),
    ("language", "TEXT"),
):
    if column_name not in user_columns:
        cursor_init.execute(f"ALTER TABLE users ADD COLUMN {column_name} {column_type}")
conn_init.commit()

# Создание таблицы orders (с новыми полями уже учтёнными через ALTER)
cursor_init.execute("""
    CREATE TABLE IF NOT EXISTS orders (
        order_id       INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id        INTEGER,
        items_json     TEXT,
        total          INTEGER,
        timestamp      TEXT,
        points_spent   INTEGER DEFAULT 0,
        points_earned  INTEGER DEFAULT 0,
        promo_code     TEXT,
        promo_discount INTEGER DEFAULT 0
    )
""")

# Railway может хранить старую версию таблицы. Проверяем каждый столбец
# отдельно: наличие одного поля больше не мешает добавить второе.
cursor_init.execute("PRAGMA table_info(orders)")
order_columns = {row[1] for row in cursor_init.fetchall()}
for column_name, column_type in (
    ("points_spent", "INTEGER DEFAULT 0"),
    ("points_earned", "INTEGER DEFAULT 0"),
    ("promo_code", "TEXT"),
    ("promo_discount", "INTEGER DEFAULT 0"),
):
    if column_name not in order_columns:
        cursor_init.execute(
            f"ALTER TABLE orders ADD COLUMN {column_name} {column_type}"
        )

# Незавершённая корзина хранится отдельно от оперативного состояния бота.
# Поэтому redeploy/restart Railway не уничтожает выбранные пользователем товары.
cursor_init.execute("""
    CREATE TABLE IF NOT EXISTS user_carts (
        chat_id     INTEGER PRIMARY KEY,
        items_json  TEXT NOT NULL DEFAULT '[]',
        updated_at  TEXT NOT NULL
    )
""")

# Промокод хранит общий лимит кампании. Отдельная таблица использований
# гарантирует, что один пользователь применит конкретную кампанию только раз.
cursor_init.execute("""
    CREATE TABLE IF NOT EXISTS promo_codes (
        promo_id         INTEGER PRIMARY KEY AUTOINCREMENT,
        code             TEXT NOT NULL UNIQUE,
        discount_amount  INTEGER NOT NULL,
        usage_limit      INTEGER NOT NULL,
        used_count       INTEGER NOT NULL DEFAULT 0,
        active           INTEGER NOT NULL DEFAULT 1,
        created_at       TEXT NOT NULL
    )
""")
cursor_init.execute("""
    CREATE TABLE IF NOT EXISTS promo_redemptions (
        promo_id         INTEGER NOT NULL,
        chat_id          INTEGER NOT NULL,
        order_id         INTEGER NOT NULL,
        discount_amount  INTEGER NOT NULL,
        redeemed_at      TEXT NOT NULL,
        PRIMARY KEY (promo_id, chat_id)
    )
""")
cursor_init.execute(
    "CREATE INDEX IF NOT EXISTS idx_promo_redemptions_order "
    "ON promo_redemptions(order_id)"
)

# Последние успешно полученные курсы сохраняются на постоянном диске Railway.
# Если внешний сервис временно недоступен, итог заказа всё равно можно показать
# по последнему известному набору курсов, не подставляя выдуманные значения.
cursor_init.execute("""
    CREATE TABLE IF NOT EXISTS exchange_rate_cache (
        currency   TEXT PRIMARY KEY,
        rate       REAL NOT NULL,
        updated_at TEXT NOT NULL
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
menu_lock = threading.RLock()


def save_menu_safely() -> None:
    """Атомарно сохраняет каталог, не оставляя частично записанный JSON."""
    temporary_path = f"{MENU_PATH}.tmp"
    with open(temporary_path, "w", encoding="utf-8") as file_obj:
        json.dump(menu, file_obj, ensure_ascii=False, indent=2)
        file_obj.flush()
        os.fsync(file_obj.fileno())
    os.replace(temporary_path, MENU_PATH)

# 0. Убедимся, что у пользователя всегда есть запись в user_data, новое добавленное
def init_user(chat_id: int):
    if chat_id not in user_data:
        saved_language = None
        saved_cart = []
        conn_local = get_db_connection()
        cursor_local = conn_local.cursor()
        try:
            cursor_local.execute("SELECT language FROM users WHERE chat_id = ?", (chat_id,))
            row = cursor_local.fetchone()
            if row and row[0] in ("ru", "en"):
                saved_language = row[0]

            cursor_local.execute(
                "SELECT items_json FROM user_carts WHERE chat_id = ?",
                (chat_id,),
            )
            cart_row = cursor_local.fetchone()
            if cart_row and cart_row[0]:
                parsed_cart = json.loads(cart_row[0])
                if isinstance(parsed_cart, list):
                    saved_cart = [
                        item for item in parsed_cart
                        if isinstance(item, dict)
                        and isinstance(item.get("category"), str)
                        and isinstance(item.get("flavor"), str)
                        and isinstance(item.get("price"), (int, float))
                    ]
        except (sqlite3.OperationalError, json.JSONDecodeError, TypeError):
            # На случай первого запуска во время миграции старой БД.
            saved_language = None
        finally:
            cursor_local.close()
            conn_local.close()

        user_data[chat_id] = {
            "lang": saved_language,
            "cart": saved_cart,
            "current_category": None,
            "wait_for_points": False,
            "wait_for_address": False,
            "wait_for_contact": False,
            "wait_for_comment": False,
            "wait_for_promo": False,
            "address": "",
            "contact": "",
            "comment": "",
            "pending_discount": 0,
            "pending_points_spent": 0,
            "promo_code": "",
            "promo_discount": 0,
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


def save_user_cart(chat_id: int) -> None:
    """Сохраняет текущую корзину пользователя в SQLite."""
    cart = user_data.get(chat_id, {}).get("cart", [])
    conn_local = get_db_connection()
    cursor_local = conn_local.cursor()
    cursor_local.execute(
        """
        INSERT INTO user_carts (chat_id, items_json, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT(chat_id) DO UPDATE SET
            items_json = excluded.items_json,
            updated_at = excluded.updated_at
        """,
        (
            chat_id,
            json.dumps(cart, ensure_ascii=False),
            datetime.datetime.utcnow().isoformat(),
        ),
    )
    conn_local.commit()
    cursor_local.close()
    conn_local.close()
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
    if chat_id not in user_data:
        init_user(chat_id)
    lang = user_data.get(chat_id, {}).get("lang") or "ru"
    fallback = {
        "ru": {
            "error_out_of_stock": "Этого товара больше нет в наличии.",
        },
        "en": {
            "error_out_of_stock": "This item is no longer in stock.",
        },
    }
    return translations.get(lang, {}).get(key, fallback.get(lang, {}).get(key, key))


def tr(chat_id: int, ru_text: str, en_text: str) -> str:
    """Возвращает короткий встроенный перевод без зависимости от JSON-файла."""
    if chat_id not in user_data:
        init_user(chat_id)
    lang = user_data.get(chat_id, {}).get("lang") or "ru"
    return en_text if lang == "en" else ru_text


def format_money(value) -> str:
    """Форматирует сумму без лишнего .0."""
    number = float(value)
    return str(int(number)) if number.is_integer() else f"{number:.2f}"


def normalize_promo_code(value: str) -> str:
    """Промокоды нечувствительны к регистру и не содержат пробелов по краям."""
    return str(value or "").strip().upper()


def is_valid_promo_code(value: str) -> bool:
    """Разрешает удобные короткие коды на русском или английском языке."""
    return bool(re.fullmatch(r"[0-9A-ZА-ЯЁ_-]{2,32}", value))


def clear_promo_state(data: dict) -> None:
    data.update({
        "wait_for_promo": False,
        "promo_code": "",
        "promo_discount": 0,
    })
    data.pop("points_before_promo", None)


class PromoCodeError(Exception):
    """Ожидаемая ошибка повторного либо недоступного промокода."""


def _stable_token(*parts: str) -> str:
    payload = "\x1f".join(str(part) for part in parts).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:16]


def category_token(category: str) -> str:
    return _stable_token("category", category)


def product_token(category: str, flavor: str) -> str:
    return _stable_token("product", category, flavor)


def resolve_category(token: str) -> str | None:
    """Поддерживает новые ID и старые сообщения с названием категории."""
    if token in menu:
        return token
    for category in menu:
        if category_token(category) == token:
            return category
    return None


def resolve_product(token: str) -> tuple[str, dict] | None:
    for category, category_data in menu.items():
        for item in category_data.get("flavors", []):
            if product_token(category, str(item.get("flavor", ""))) == token:
                return category, item
    return None


def cart_quantity(chat_id: int, category: str, flavor: str) -> int:
    return sum(
        1 for item in user_data.get(chat_id, {}).get("cart", [])
        if item.get("category") == category and item.get("flavor") == flavor
    )


def cart_totals(chat_id: int) -> tuple[int, float]:
    cart = user_data.get(chat_id, {}).get("cart", [])
    return len(cart), sum(float(item.get("price", 0)) for item in cart)


def disable_inline_keyboard(call) -> None:
    """Убирает кнопки у устаревшего сообщения, если Telegram это позволяет."""
    try:
        bot.edit_message_reply_markup(
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            reply_markup=None,
        )
    except Exception:
        pass


def render_inline_screen(
    chat_id: int,
    text: str,
    reply_markup,
    call=None,
    *,
    allow_media_edit: bool = True,
) -> None:
    """Обновляет активный экран; при невозможности безопасно создаёт новый."""
    if call is not None:
        message = getattr(call, "message", None)
        if message is not None:
            is_media = bool(getattr(message, "photo", None)) or getattr(
                message, "content_type", None
            ) == "photo"
            try:
                if is_media and allow_media_edit:
                    bot.edit_message_caption(
                        caption=text,
                        chat_id=message.chat.id,
                        message_id=message.message_id,
                        parse_mode="HTML",
                        reply_markup=reply_markup,
                    )
                elif not is_media:
                    bot.edit_message_text(
                        text,
                        chat_id=message.chat.id,
                        message_id=message.message_id,
                        parse_mode="HTML",
                        reply_markup=reply_markup,
                    )
                else:
                    raise RuntimeError("text screen requested from media message")
                return
            except Exception as exc:
                if "message is not modified" in str(exc).lower():
                    return
                disable_inline_keyboard(call)
    bot.send_message(chat_id, text, parse_mode="HTML", reply_markup=reply_markup)


def is_owner(user_id: int) -> bool:
    """Единственный владелец бота задаётся переменной Railway ADMIN_ID."""
    return user_id == ADMIN_ID


def payment_detail(method_key: str) -> str:
    """Читает один реквизит из Railway, не записывая его в БД или код."""
    method = PAYMENT_METHODS.get(method_key)
    if not method:
        return ""
    return os.getenv(method[2], "").strip().replace("\\n", "\n")


def payment_order_target(order_id: int):
    """Возвращает покупателя и сумму существующего заказа."""
    connection = get_db_connection()
    cursor = connection.cursor()
    cursor.execute(
        "SELECT chat_id, total FROM orders WHERE order_id = ?",
        (order_id,),
    )
    row = cursor.fetchone()
    cursor.close()
    connection.close()
    return row


def admin_order_keyboard(
    order_id: int,
    customer_chat_id: int,
    sent_method: str | None = None,
) -> types.InlineKeyboardMarkup:
    """Все действия над заказом в одном сообщении админ-группы."""
    kb = types.InlineKeyboardMarkup(row_width=3)
    kb.add(
        types.InlineKeyboardButton(
            text="❌ Cancel Order",
            callback_data=f"cancel_order|{order_id}|{customer_chat_id}",
        ),
        types.InlineKeyboardButton(
            text="✅ Delivered",
            callback_data=f"order_delivered|{order_id}|{customer_chat_id}",
        ),
        types.InlineKeyboardButton(
            text="🚗 OMW",
            callback_data=f"courier_on_way|{order_id}|{customer_chat_id}",
        ),
    )
    if sent_method in PAYMENT_METHODS:
        label = PAYMENT_METHODS[sent_method][0]
        text = f"✅ Реквизиты отправлены: {label} · отправить другие"
    else:
        text = "💳 Выслать реквизиты"
    kb.add(types.InlineKeyboardButton(
        text=text,
        callback_data=f"payment_menu|{order_id}",
    ))
    return kb


def payment_methods_keyboard(order_id: int) -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup(row_width=2)
    buttons = [
        types.InlineKeyboardButton(
            text=method[0],
            callback_data=f"payment_send|{order_id}|{method_key}",
        )
        for method_key, method in PAYMENT_METHODS.items()
    ]
    kb.add(*buttons)
    kb.add(types.InlineKeyboardButton(
        text="⬅️ Назад к управлению заказом",
        callback_data=f"payment_back|{order_id}",
    ))
    return kb


def save_user_language(chat_id: int, lang_code: str) -> None:
    """Сохраняет язык в SQLite, чтобы Railway restart его не сбрасывал."""
    conn_local = get_db_connection()
    cursor_local = conn_local.cursor()
    cursor_local.execute(
        "UPDATE users SET language = ? WHERE chat_id = ?",
        (lang_code, chat_id),
    )
    if cursor_local.rowcount == 0:
        referral_code = generate_ref_code()
        while True:
            cursor_local.execute(
                "SELECT 1 FROM users WHERE referral_code = ?",
                (referral_code,),
            )
            if cursor_local.fetchone() is None:
                break
            referral_code = generate_ref_code()
        cursor_local.execute(
            "INSERT INTO users (chat_id, points, referral_code, language) VALUES (?, 0, ?, ?)",
            (chat_id, referral_code, lang_code),
        )
    conn_local.commit()
    cursor_local.close()
    conn_local.close()


def send_referral_info(chat_id: int, referral_code: str) -> None:
    """Отправляет реферальные данные на выбранном пользователем языке."""
    bot_username = bot.get_me().username
    invite_link = f"https://t.me/{bot_username}?start=ref={referral_code}"
    if user_data.get(chat_id, {}).get("lang") == "en":
        text = (
            f"🎁 <b>Get {REFERRAL_BONUS_POINTS} points for inviting a friend!</b>\n\n"
            f"<b>Referral code:</b> <code>{referral_code}</code>\n"
            f"<b>Invitation link:</b>\n{invite_link}"
        )
    else:
        text = (
            f"🎁 <b>{REFERRAL_BONUS_POINTS} баллов за приглашение друга!</b>\n\n"
            f"<b>Реферальный код:</b> <code>{referral_code}</code>\n"
            f"<b>Ссылка для приглашения:</b>\n{invite_link}"
        )
    bot.send_message(chat_id, text, parse_mode="HTML")


def broadcast_message_to_users(source_chat_id: int, source_message_id: int) -> tuple[int, int]:
    """Копирует исходное сообщение всем зарегистрированным пользователям."""
    conn_local = get_db_connection()
    cursor_local = conn_local.cursor()
    cursor_local.execute("SELECT chat_id FROM users")
    recipients = [row[0] for row in cursor_local.fetchall()]
    cursor_local.close()
    conn_local.close()

    sent = 0
    failed = 0
    for recipient_id in recipients:
        try:
            bot.copy_message(
                chat_id=recipient_id,
                from_chat_id=source_chat_id,
                message_id=source_message_id,
            )
            sent += 1
        except Exception as exc:
            failed += 1
            print(f"Broadcast failed for {recipient_id}: {exc}")
        # Не превышаем безопасный темп массовых отправок Telegram.
        time.sleep(0.05)

    return sent, failed


def generate_ref_code(length: int = 6) -> str:
    """
    Генерирует случайный реферальный код из заглавных букв и цифр.
    """
    return "".join(random.choices(string.ascii_uppercase + string.digits, k=length))

# ─── Кешированные курсы валют ───────────────────────────────────────────────
_RATE_CACHE: dict[str, float] | None = None
_RATE_CACHE_TS: float = 0.0
_RATE_TTL: int = 10 * 60  # 10 минут


def valid_exchange_rates(rates: dict[str, float] | None) -> bool:
    required = ("RUB", "USD", "EUR", "UAH")
    if not rates:
        return False
    try:
        return all(float(rates.get(code, 0)) > 0 for code in required)
    except (TypeError, ValueError):
        return False


def save_exchange_rates(rates: dict[str, float]) -> None:
    """Сохраняет только полный и положительный набор курсов."""
    if not valid_exchange_rates(rates):
        return
    connection = get_db_connection()
    cursor = connection.cursor()
    updated_at = datetime.datetime.utcnow().isoformat()
    for currency in ("RUB", "USD", "EUR", "UAH"):
        cursor.execute(
            """
            INSERT INTO exchange_rate_cache (currency, rate, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(currency) DO UPDATE SET
                rate = excluded.rate,
                updated_at = excluded.updated_at
            """,
            (currency, float(rates[currency]), updated_at),
        )
    connection.commit()
    cursor.close()
    connection.close()


def load_saved_exchange_rates() -> dict[str, float]:
    connection = get_db_connection()
    cursor = connection.cursor()
    try:
        cursor.execute(
            "SELECT currency, rate FROM exchange_rate_cache "
            "WHERE currency IN ('RUB', 'USD', 'EUR', 'UAH')"
        )
        result = {str(currency): float(rate) for currency, rate in cursor.fetchall()}
    except sqlite3.Error:
        result = {}
    finally:
        cursor.close()
        connection.close()
    return result if valid_exchange_rates(result) else {}

def fetch_rates() -> dict[str, float]:
    """
    Возвращает курсы валют TRY → RUB, USD, UAH, EUR,
    кешируя результат на _RATE_TTL секунд.
    """
    global _RATE_CACHE, _RATE_CACHE_TS

    now = time.time()
    # Если кеш ещё «жив» — отдаём его
    if valid_exchange_rates(_RATE_CACHE) and (now - _RATE_CACHE_TS) < _RATE_TTL:
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
                result = {
                    code: float(rates[code])
                    for code in ("RUB", "USD", "UAH", "EUR")
                    if code in rates
                }
            else:
                result = {}
            if valid_exchange_rates(result):
                _RATE_CACHE = result
                _RATE_CACHE_TS = now
                save_exchange_rates(result)
                return result
        except Exception:
            continue

    # При временной ошибке используем последний успешный набор — сначала из
    # памяти, затем с постоянного диска Railway.
    if valid_exchange_rates(_RATE_CACHE):
        return _RATE_CACHE
    saved_rates = load_saved_exchange_rates()
    if saved_rates:
        _RATE_CACHE = saved_rates
        _RATE_CACHE_TS = now
        return saved_rates
    return {"RUB": 0, "USD": 0, "EUR": 0, "UAH": 0}

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
def get_inline_language_buttons(
    chat_id: int,
    include_back: bool = False,
    back_callback: str | None = None,
) -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        types.InlineKeyboardButton(text="Русский 🇷🇺", callback_data="set_lang|ru"),
        types.InlineKeyboardButton(text="English 🇬🇧", callback_data="set_lang|en")
    )
    if include_back:
        kb.add(types.InlineKeyboardButton(
            text=tr(chat_id, "⬅️ Назад", "⬅️ Back"),
            callback_data=back_callback or "go_back_to_categories",
        ))
    return kb

# ------------------------------------------------------------------------
#   9. Inline-кнопки для главного меню
# ------------------------------------------------------------------------
def get_inline_main_menu(chat_id: int, show_language: bool = False) -> types.InlineKeyboardMarkup:
    # show_language оставлен в сигнатуре для совместимости со старыми вызовами.
    # Язык находится только в разделе «Профиль».
    kb = types.InlineKeyboardMarkup(row_width=1)

    # Категории для пользователя:
    # показываем только те категории, где есть хотя бы один вкус со stock > 0.
    # Пустые категории НЕ удаляются из menu.json и остаются доступными в /change.
    for cat, cat_data in menu.items():
        flavors = cat_data.get("flavors", [])
        total_stock = sum(int(item.get("stock", 0)) for item in flavors)

        if total_stock <= 0:
            continue

        price = format_money(cat_data.get("price", 0))
        kb.add(types.InlineKeyboardButton(
            text=f"{cat} · {price}₺",
            callback_data=f"category|{category_token(cat)}"
        ))

    # Кнопки корзины и дальнейших действий — только если в корзине есть товары
    cart_count, cart_total = cart_totals(chat_id)
    if cart_count > 0:
        kb.add(types.InlineKeyboardButton(
            text=tr(
                chat_id,
                f"🛒 Корзина · {cart_count} шт. · {format_money(cart_total)}₺",
                f"🛒 Cart · {cart_count} pcs · {format_money(cart_total)}₺",
            ),
            callback_data="view_cart"
        ))

    kb.add(types.InlineKeyboardButton(
        text=tr(chat_id, "👤 Профиль", "👤 Profile"),
        callback_data="profile",
    ))

    return kb
def points_choice_keyboard(chat_id: int, max_points: int):
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(types.InlineKeyboardButton(
        text=tr(
            chat_id,
            f"🎁 Использовать все {max_points}",
            f"🎁 Use all {max_points}",
        ),
        callback_data="points_all",
    ))
    kb.add(types.InlineKeyboardButton(
        text=tr(chat_id, "✏️ Ввести другую сумму", "✏️ Enter another amount"),
        callback_data="points_custom",
    ))
    kb.add(types.InlineKeyboardButton(
        text=nav_text(chat_id, "review"),
        callback_data="review_order",
    ))
    kb.add(types.InlineKeyboardButton(
        text=nav_text(chat_id, "menu"),
        callback_data="go_back_to_categories",
    ))
    return kb


def skip_points_keyboard(chat_id: int):
    """Совместимость со старым именем функции."""
    data = user_data.get(chat_id, {})
    max_points = min(
        int(data.get("temp_user_points", 0) or 0),
        int(data.get("temp_total_try", 0) or 0),
    )
    return points_choice_keyboard(chat_id, max_points)


def back_to_main_keyboard(chat_id: int) -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton(
        text=nav_text(chat_id, "menu"),
        callback_data="go_back_to_categories",
    ))
    return kb
# ------------------------------------------------------------------------
#   10. Inline-кнопки для выбора вкусов
# ------------------------------------------------------------------------
def get_inline_flavors(chat_id: int, cat: str) -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup(row_width=1)
    price = format_money(menu[cat]["price"])

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
        stock_unit = tr(chat_id, "шт", "pcs")
        label = f"{emoji} {flavor}{rating_str} · {price}₺ · {stock} {stock_unit}"
        kb.add(types.InlineKeyboardButton(
            text=label,
            callback_data=f"product|{product_token(cat, flavor)}"
        ))

    kb.add(types.InlineKeyboardButton(
        text=tr(chat_id, "⬅️ К категориям", "⬅️ To categories"),
        callback_data="go_back_to_categories"
    ))
    return kb


def main_menu_text(chat_id: int) -> str:
    return tr(
        chat_id,
        "<b>🛍 Главное меню</b>\n\nВыберите модель:",
        "<b>🛍 Main menu</b>\n\nChoose a model:",
    )


def show_main_menu(chat_id: int, call=None) -> None:
    render_inline_screen(
        chat_id,
        main_menu_text(chat_id),
        get_inline_main_menu(chat_id),
        call,
        allow_media_edit=False,
    )


def show_category_screen(chat_id: int, category: str, call=None) -> None:
    if category not in menu:
        show_main_menu(chat_id, call)
        return

    user_data[chat_id]["current_category"] = category
    price = format_money(menu[category].get("price", 0))
    text = tr(
        chat_id,
        f"<b>{html.escape(category)}</b>\nЦена: <b>{price}₺</b>\n\nВыберите вкус:",
        f"<b>{html.escape(category)}</b>\nPrice: <b>{price}₺</b>\n\nChoose a flavor:",
    )
    keyboard = get_inline_flavors(chat_id, category)
    photo_url = str(menu[category].get("photo_url", "") or "").strip()

    # Если пользователь уже находится на фото-карточке этой категории,
    # обновляем её подпись. Иначе выключаем старые кнопки и создаём карточку.
    message = getattr(call, "message", None) if call is not None else None
    is_media = bool(getattr(message, "photo", None)) or getattr(
        message, "content_type", None
    ) == "photo"
    if call is not None and is_media:
        render_inline_screen(chat_id, text, keyboard, call)
        return
    if photo_url:
        if call is not None:
            disable_inline_keyboard(call)
        try:
            bot.send_photo(
                chat_id,
                photo_url,
                caption=text,
                parse_mode="HTML",
                reply_markup=keyboard,
            )
            return
        except Exception as exc:
            print(f"Failed to send category photo for {category}: {exc}")
    render_inline_screen(chat_id, text, keyboard, call)


def recover_catalog_screen(chat_id: int, call=None) -> None:
    """Возвращает пользователя с устаревшей кнопки в актуальный каталог."""
    category = user_data.get(chat_id, {}).get("current_category")
    if category in menu:
        show_category_screen(chat_id, category, call)
    else:
        show_main_menu(chat_id, call)

# ------------------------------------------------------------------------
#   11. Reply-клавиатуры (альтернатива inline)
# ------------------------------------------------------------------------
def nav_text(chat_id: int, destination: str) -> str:
    labels = {
        "cart": ("⬅️ Назад в корзину", "⬅️ Back to cart"),
        "address": ("⬅️ Назад к адресу", "⬅️ Back to address"),
        "contact": ("⬅️ Назад к контакту", "⬅️ Back to contact"),
        "points": ("⬅️ Назад к баллам", "⬅️ Back to points"),
        "review": ("⬅️ Назад к заказу", "⬅️ Back to order"),
        "menu": ("🏠 В главное меню", "🏠 Main menu"),
    }
    ru_text, en_text = labels.get(destination, ("⬅️ Назад", "⬅️ Back"))
    return tr(chat_id, ru_text, en_text)


def is_navigation_text(chat_id: int, text: str, destination: str | None = None) -> bool:
    accepted = {t(chat_id, "back"), nav_text(chat_id, "back")}
    if destination:
        accepted.add(nav_text(chat_id, destination))
    return text in accepted


def no_comment_text(chat_id: int) -> str:
    return tr(chat_id, "Без комментария", "No comment")


def leave_checkout_to_main(chat_id: int) -> None:
    """Закрывает checkout, сохраняя товары пользователя в корзине."""
    data = user_data.get(chat_id, {})
    data.update({
        "current_category": None,
        "wait_for_points": False,
        "wait_for_address": False,
        "wait_for_contact": False,
        "wait_for_comment": False,
        "wait_for_promo": False,
        "pending_discount": 0,
        "pending_points_spent": 0,
    })
    clear_promo_state(data)
    data.pop("return_to_review_after_address", None)
    data.pop("return_to_review_after_contact", None)
    data.pop("return_to_review_after_comment", None)
    user_data[chat_id] = data
    bot.send_message(
        chat_id,
        tr(
            chat_id,
            "Оформление закрыто. Товары остались в корзине.",
            "Checkout closed. Your items are still in the cart.",
        ),
        reply_markup=types.ReplyKeyboardRemove(),
    )
    show_main_menu(chat_id)


def address_keyboard(chat_id: int, back_destination: str = "cart") -> types.ReplyKeyboardMarkup:
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
    kb.add(types.KeyboardButton(t(chat_id, "share_location"), request_location=True))
    kb.add(t(chat_id, "choose_on_map"))
    kb.add(t(chat_id, "enter_address_text"))
    kb.add(nav_text(chat_id, back_destination), nav_text(chat_id, "menu"))
    return kb



def contact_keyboard(chat_id: int, back_destination: str = "address") -> types.ReplyKeyboardMarkup:
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
    kb.add(types.KeyboardButton(t(chat_id, "share_contact"), request_contact=True))
    kb.add(t(chat_id, "enter_nickname"))
    kb.add(nav_text(chat_id, back_destination), nav_text(chat_id, "menu"))
    return kb



def comment_keyboard(chat_id: int) -> types.ReplyKeyboardMarkup:
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
    kb.add(no_comment_text(chat_id))
    kb.add(nav_text(chat_id, "contact"), nav_text(chat_id, "menu"))
    return kb


def text_entry_back_keyboard(
    chat_id: int,
    destination: str = "back",
    *,
    include_no_comment: bool = False,
) -> types.ReplyKeyboardMarkup:
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
    if include_no_comment:
        kb.add(no_comment_text(chat_id))
    kb.add(nav_text(chat_id, destination), nav_text(chat_id, "menu"))
    return kb
# ------------------------------------------------------------------------
#   12. Клавиатура редактирования меню (/change) — ВСЁ НА АНГЛИЙСКОМ
# ------------------------------------------------------------------------
def edit_action_keyboard() -> types.ReplyKeyboardMarkup:
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
    kb.add("➕ Add Category", "➖ Remove Category", "✏️ Rename Category")
    kb.add("💲 Fix Price", "ALL IN", "🔄 Actual Flavor")
    kb.add("🖼️ Add Category Picture", "Set Category Flavor to 0")
    kb.add("📦 New Supply", "MESSAGE")
    kb.add("PROMO CREATE")
    kb.add("⬅️ Back", "❌ Cancel")
    return kb

# ------------------------------------------------------------------------
#   14. Хендлер /start – регистрация, реферальная система, выбор языка
# ------------------------------------------------------------------------
@ensure_user
@bot.message_handler(commands=['start'])
def cmd_start(message):
    chat_id = message.chat.id
    init_user(chat_id)

    # /start возвращает в меню, но не уничтожает уже собранную корзину.
    lang = user_data[chat_id].get("lang")
    existing_cart = list(user_data[chat_id].get("cart", []))
    user_data[chat_id] = {
        "lang": lang,
        "cart": existing_cart,
        "current_category": None,
        "wait_for_points": False,
        "wait_for_address": False,
        "wait_for_contact": False,
        "wait_for_comment": False,
        "wait_for_promo": False,
        "address": "",
        "contact": "",
        "comment": "",
        "pending_discount": 0,
        "pending_points_spent": 0,
        "promo_code": "",
        "promo_discount": 0,
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

    # --- регистрация пользователя / обработка referral ---
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute(
        "SELECT referral_code, referred_by, language FROM users WHERE chat_id = ?",
        (chat_id,),
    )
    row = cur.fetchone()
    is_new_user = row is None

    if is_new_user:
        referred_by = None
        text = message.text or ""

        # если зашёл по ссылке ref
        if "ref=" in text:
            code = text.split("ref=")[1]
            cur.execute("SELECT chat_id FROM users WHERE referral_code = ?", (code,))
            r = cur.fetchone()
            if r:
                referred_by = r[0]

        # генерируем уникальный код
        new_code = generate_ref_code()
        while True:
            cur.execute("SELECT chat_id FROM users WHERE referral_code = ?", (new_code,))
            if cur.fetchone() is None:
                break
            new_code = generate_ref_code()

        cur.execute(
            "INSERT INTO users (chat_id, points, referral_code, referred_by) VALUES (?, ?, ?, ?)",
            (chat_id, 0, new_code, referred_by)
        )
        conn.commit()

        referral_code = new_code
    else:
        referral_code = row[0]  # уже существующий
        if row[2] in ("ru", "en"):
            user_data[chat_id]["lang"] = row[2]

    cur.close()
    conn.close()

    # --- если язык еще не выбран — показать выбор языка ---
    if user_data[chat_id]["lang"] is None:
        bot.send_message(
            chat_id,
            t(chat_id, "choose_language"),
            reply_markup=get_inline_language_buttons(chat_id)
        )
        return

    # У известного пользователя убираем reply-клавиатуру предыдущего шага.
    # До первого выбора языка её ещё нет, поэтому лишнее русское сообщение
    # перед двуязычным экраном больше не появляется.
    bot.send_message(
        chat_id,
        tr(chat_id, "🔙 Вернулись в главное меню", "🔙 Back to the main menu"),
        reply_markup=types.ReplyKeyboardRemove(),
    )

    # Реферальная карточка показывается один раз при регистрации, а не при
    # каждом возврате через /start.
    if is_new_user:
        send_referral_info(chat_id, referral_code)

    # --- главное меню ---
    show_main_menu(chat_id)

# ------------------------------------------------------------------------
#   15. Callback: выбор языка
# ------------------------------------------------------------------------
@bot.message_handler(commands=['language', 'lang'])
def cmd_language(message):
    chat_id = message.chat.id
    init_user(chat_id)
    user_data[chat_id]["changing_language"] = True
    user_data[chat_id]["language_return"] = "menu"
    bot.send_message(
        chat_id,
        "🌐 Выберите язык / Choose your language:",
        reply_markup=get_inline_language_buttons(
            chat_id,
            include_back=True,
            back_callback="go_back_to_categories",
        ),
    )


@bot.callback_query_handler(func=lambda call: call.data == "change_language")
def handle_change_language(call):
    chat_id = call.from_user.id
    init_user(chat_id)
    user_data[chat_id]["changing_language"] = True
    return_to = user_data[chat_id].get("language_return") or "menu"
    user_data[chat_id]["language_return"] = return_to
    bot.answer_callback_query(call.id)
    bot.send_message(
        chat_id,
        "🌐 Выберите язык / Choose your language:",
        reply_markup=get_inline_language_buttons(
            chat_id,
            include_back=True,
            back_callback="profile" if return_to == "profile" else "go_back_to_categories",
        ),
    )


@ensure_user
@bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("set_lang|"))
def handle_set_lang(call):
    chat_id = call.from_user.id
    _, lang_code = call.data.split("|", 1)
    if lang_code not in ("ru", "en"):
        return bot.answer_callback_query(call.id, "Invalid language", show_alert=True)

    init_user(chat_id)
    previous_language = user_data[chat_id].get("lang")
    was_changing = user_data[chat_id].pop("changing_language", False)
    return_to = user_data[chat_id].pop("language_return", "menu")
    user_data[chat_id]["lang"] = lang_code
    save_user_language(chat_id, lang_code)

    initial_selection = previous_language not in ("ru", "en") and not was_changing

    bot.answer_callback_query(call.id, t(chat_id, "lang_set"))
    try:
        bot.edit_message_reply_markup(
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            reply_markup=None,
        )
    except Exception:
        pass

    conn_local = get_db_connection()
    cursor_local = conn_local.cursor()
    cursor_local.execute("SELECT referral_code FROM users WHERE chat_id = ?", (chat_id,))
    row = cursor_local.fetchone()
    cursor_local.close()
    conn_local.close()

    if initial_selection and row:
        send_referral_info(chat_id, row[0])

    # При первой настройке главное меню отправляется последним сообщением,
    # чтобы оно не оказалось выше реферальной карточки и не потерялось.
    if return_to == "profile" and previous_language in ("ru", "en"):
        show_profile(chat_id, call)
    elif initial_selection:
        show_main_menu(chat_id)
    else:
        show_main_menu(chat_id, call)


# ------------------------------------------------------------------------
#   16. Callback: выбор категории (показываем вкусы)
# ------------------------------------------------------------------------
@ensure_user
@bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("category|"))
def handle_category(call):
    chat_id = call.from_user.id
    init_user(chat_id)
    _, token = call.data.split("|", 1)
    cat = resolve_category(token)

    if not cat:
        bot.answer_callback_query(call.id, t(chat_id, "error_invalid"), show_alert=True)
        show_main_menu(chat_id, call)
        return

    bot.answer_callback_query(call.id)
    show_category_screen(chat_id, cat, call)

# ------------------------------------------------------------------------
#   17. Callback: «Назад к категориям»
# ------------------------------------------------------------------------
@ensure_user
@bot.callback_query_handler(func=lambda call: call.data == "go_back_to_categories")
def handle_go_back_to_categories(call):
    chat_id = call.from_user.id
    bot.answer_callback_query(call.id)
    data = user_data.get(chat_id, {})
    data.update({
        "wait_for_points": False,
        "wait_for_address": False,
        "wait_for_contact": False,
        "wait_for_comment": False,
        "edit_cart_phase": None,
        "pending_discount": 0,
        "pending_points_spent": 0,
    })
    clear_promo_state(data)
    data.pop("return_to_review_after_address", None)
    data.pop("return_to_review_after_contact", None)
    data.pop("return_to_review_after_comment", None)
    data.pop("changing_language", None)
    data.pop("language_return", None)
    show_main_menu(chat_id, call)


# ------------------------------------------------------------------------
#   18. Callback: выбор вкуса
# ------------------------------------------------------------------------
@ensure_user
@bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("product|"))
def handle_flavor(call):
    chat_id = call.from_user.id
    init_user(chat_id)
    _, token = call.data.split("|", 1)
    resolved = resolve_product(token)
    if not resolved:
        bot.answer_callback_query(call.id, t(chat_id, "error_invalid"), show_alert=True)
        recover_catalog_screen(chat_id, call)
        return

    cat, item = resolved
    if int(item.get("stock", 0)) <= 0:
        return bot.answer_callback_query(call.id, t(chat_id, "error_out_of_stock"), show_alert=True)

    flavor = item["flavor"]
    price = menu[cat]["price"]
    stock = int(item.get("stock", 0))
    in_cart = cart_quantity(chat_id, cat, flavor)
    bot.answer_callback_query(call.id)

    desc = item.get(f"description_{user_data[chat_id]['lang']}", "")
    lines = [
        f"<b>{html.escape(str(flavor))}</b>",
        html.escape(str(cat)),
        "",
    ]
    if desc:
        lines.extend([html.escape(str(desc)), ""])
    lines.extend([
        tr(chat_id, f"Цена: <b>{format_money(price)}₺</b>", f"Price: <b>{format_money(price)}₺</b>"),
        tr(chat_id, f"В наличии: {stock} шт.", f"In stock: {stock} pcs"),
    ])
    if in_cart:
        lines.append(tr(chat_id, f"В корзине: {in_cart} шт.", f"In cart: {in_cart} pcs"))
    caption = "\n".join(lines)

    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(
        types.InlineKeyboardButton(
            text=f"➕ {t(chat_id, 'add_to_cart')}",
            callback_data=f"cart_add|{token}"
        ),
        types.InlineKeyboardButton(
            text=tr(chat_id, "⬅️ К вкусам", "⬅️ To flavors"),
            callback_data=f"category|{category_token(cat)}"
        )
    )
    count, total = cart_totals(chat_id)
    if count:
        kb.add(types.InlineKeyboardButton(
            text=tr(
                chat_id,
                f"🛒 Корзина · {count} шт. · {format_money(total)}₺",
                f"🛒 Cart · {count} pcs · {format_money(total)}₺",
            ),
            callback_data="view_cart"
        ))

    render_inline_screen(chat_id, caption, kb, call)


@ensure_user
@bot.callback_query_handler(func=lambda call: call.data == "back_to_flavors")
def handle_back_to_flavors(call):
    chat_id = call.from_user.id
    bot.answer_callback_query(call.id)
    cat = user_data.get(chat_id, {}).get("current_category")
    if not cat or cat not in menu:
        bot.send_message(
            chat_id,
            t(chat_id, "choose_category"),
            reply_markup=get_inline_main_menu(chat_id),
        )
        return
    show_category_screen(chat_id, cat, call)

# ------------------------------------------------------------------------
#   19. Callback: добавить в корзину (без изменения stock)
# ------------------------------------------------------------------------
@ensure_user
@bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("cart_add|"))
def handle_add_to_cart(call):
    chat_id = call.from_user.id
    init_user(chat_id)
    _, token = call.data.split("|", 1)
    resolved = resolve_product(token)
    if not resolved:
        bot.answer_callback_query(call.id, t(chat_id, "error_invalid"), show_alert=True)
        recover_catalog_screen(chat_id, call)
        return

    cat, item = resolved
    stock = int(item.get("stock", 0))
    current_qty = cart_quantity(chat_id, cat, item["flavor"])
    if stock <= current_qty:
        return bot.answer_callback_query(
            call.id,
            tr(
                chat_id,
                f"В корзине уже всё доступное количество: {stock} шт.",
                f"Your cart already contains all {stock} available units.",
            ),
            show_alert=True,
        )

    bot.answer_callback_query(call.id)

    # добавляем в корзину
    data = user_data.setdefault(chat_id, {})
    cart = data.setdefault("cart", [])
    data.update({
        "pending_discount": 0,
        "pending_points_spent": 0,
        "wait_for_points": False,
        "wait_for_address": False,
        "wait_for_contact": False,
        "wait_for_comment": False,
    })
    clear_promo_state(data)
    existing_item = next(
        (
            cart_item for cart_item in cart
            if cart_item.get("category") == cat
            and cart_item.get("flavor") == item["flavor"]
        ),
        None,
    )
    price = existing_item["price"] if existing_item else menu[cat]["price"]
    cart.append({
        "category": cat,
        "flavor": item["flavor"],
        "price": price
    })
    save_user_cart(chat_id)

    count, total = cart_totals(chat_id)
    text = tr(
        chat_id,
        f"<b>✅ Добавлено в корзину</b>\n\n"
        f"{html.escape(cat)}\n{html.escape(str(item['flavor']))}\n\n"
        f"В корзине: {count} шт. на {format_money(total)}₺",
        f"<b>✅ Added to cart</b>\n\n"
        f"{html.escape(cat)}\n{html.escape(str(item['flavor']))}\n\n"
        f"Cart: {count} pcs totaling {format_money(total)}₺",
    )
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(types.InlineKeyboardButton(
        text=tr(chat_id, "➕ Добавить ещё из этой модели", "➕ Add another from this model"),
        callback_data=f"category|{category_token(cat)}",
    ))
    kb.add(types.InlineKeyboardButton(
        text=tr(
            chat_id,
            f"🛒 Открыть корзину · {count} шт. · {format_money(total)}₺",
            f"🛒 Open cart · {count} pcs · {format_money(total)}₺",
        ),
        callback_data="view_cart",
    ))
    kb.add(types.InlineKeyboardButton(
        text=tr(chat_id, "⬅️ К моделям", "⬅️ Back to models"),
        callback_data="go_back_to_categories",
    ))
    render_inline_screen(chat_id, text, kb, call)


@bot.callback_query_handler(
    func=lambda c: c.data and (
        c.data.startswith("flavor|") or c.data.startswith("add_to_cart|")
    )
)
def handle_stale_product_button(call):
    init_user(call.from_user.id)
    bot.answer_callback_query(
        call.id,
        tr(
            call.from_user.id,
            "Эта кнопка устарела. Откройте товар заново.",
            "This button is outdated. Please open the product again.",
        ),
        show_alert=True,
    )
    recover_catalog_screen(call.from_user.id, call)


# ------------------------------------------------------------------------
#   20. Корзина: стабильные ID, + / −, удаление и подтверждение очистки
# ------------------------------------------------------------------------
def get_grouped_cart(chat_id: int) -> list[tuple[tuple[str, str, int | float], int]]:
    grouped = {}
    for item in user_data.get(chat_id, {}).get("cart", []):
        key = (item["category"], item["flavor"], item["price"])
        grouped[key] = grouped.get(key, 0) + 1
    return list(grouped.items())


def find_cart_group(chat_id: int, token: str):
    for (category, flavor, price), qty in get_grouped_cart(chat_id):
        if product_token(category, flavor) == token:
            return category, flavor, price, qty
    return None


def send_cart(chat_id: int, call=None) -> None:
    items_list = get_grouped_cart(chat_id)
    if not items_list:
        kb = types.InlineKeyboardMarkup(row_width=1)
        kb.add(types.InlineKeyboardButton(
            text=tr(chat_id, "➕ Перейти к товарам", "➕ Browse products"),
            callback_data="go_back_to_categories",
        ))
        kb.add(types.InlineKeyboardButton(
            text=tr(chat_id, "👤 Профиль", "👤 Profile"),
            callback_data="profile",
        ))
        render_inline_screen(
            chat_id,
            tr(
                chat_id,
                "<b>🛒 Ваша корзина</b>\n\nКорзина пока пуста.",
                "<b>🛒 Your cart</b>\n\nYour cart is empty.",
            ),
            kb,
            call,
            allow_media_edit=False,
        )
        return

    text_lines = [tr(chat_id, "<b>🛒 Ваша корзина</b>", "<b>🛒 Your cart</b>"), ""]
    total = 0
    total_qty = 0
    for idx, ((cat, flavor, price), qty) in enumerate(items_list, start=1):
        line_total = float(price) * qty
        total += line_total
        total_qty += qty
        text_lines.extend([
            f"<b>{idx}. {html.escape(str(cat))}</b>",
            f"   {html.escape(str(flavor))}",
            f"   {format_money(price)}₺ × {qty} = <b>{format_money(line_total)}₺</b>",
            "",
        ])

    text_lines.append(
        tr(
            chat_id,
            f"Всего: {total_qty} шт.\n<b>К оплате: {format_money(total)}₺</b>",
            f"Items: {total_qty} pcs\n<b>Amount due: {format_money(total)}₺</b>",
        )
    )

    kb = types.InlineKeyboardMarkup(row_width=4)
    for idx, ((cat, flavor, _price), qty) in enumerate(items_list, start=1):
        token = product_token(cat, flavor)
        item_label = f"{idx}. {flavor}"
        if len(item_label) > 48:
            item_label = item_label[:45] + "…"
        kb.add(types.InlineKeyboardButton(
            text=item_label,
            callback_data=f"cart_qty|{token}",
        ))
        kb.add(
            types.InlineKeyboardButton(text="➖", callback_data=f"cart_dec|{token}"),
            types.InlineKeyboardButton(
                text=tr(chat_id, f"{qty} шт.", f"{qty} pcs"),
                callback_data=f"cart_qty|{token}",
            ),
            types.InlineKeyboardButton(text="➕", callback_data=f"cart_inc|{token}"),
            types.InlineKeyboardButton(text="🗑", callback_data=f"cart_remove|{token}"),
        )

    kb.add(types.InlineKeyboardButton(
        text=tr(
            chat_id,
            f"✅ Оформить · {format_money(total)}₺",
            f"✅ Checkout · {format_money(total)}₺",
        ),
        callback_data="finish_order",
    ))
    kb.add(types.InlineKeyboardButton(
        text=tr(chat_id, "➕ Продолжить покупки", "➕ Continue shopping"),
        callback_data="go_back_to_categories",
    ))
    kb.add(types.InlineKeyboardButton(
        text=tr(chat_id, "🗑 Очистить корзину", "🗑 Clear cart"),
        callback_data="clear_cart",
    ))
    render_inline_screen(
        chat_id,
        "\n".join(text_lines),
        kb,
        call,
        allow_media_edit=False,
    )


@ensure_user
@bot.callback_query_handler(func=lambda call: call.data == "view_cart")
def handle_view_cart(call):
    chat_id = call.from_user.id
    init_user(chat_id)
    bot.answer_callback_query(call.id)
    send_cart(chat_id, call)


@ensure_user
@bot.callback_query_handler(func=lambda call: call.data == "back_to_cart")
def handle_back_to_cart(call):
    chat_id = call.from_user.id
    init_user(chat_id)
    bot.answer_callback_query(call.id)
    data = user_data[chat_id]
    data.update({
        "wait_for_points": False,
        "wait_for_address": False,
        "wait_for_contact": False,
        "wait_for_comment": False,
        "pending_discount": 0,
        "pending_points_spent": 0,
    })
    clear_promo_state(data)
    data.pop("return_to_review_after_address", None)
    data.pop("return_to_review_after_contact", None)
    data.pop("return_to_review_after_comment", None)
    send_cart(chat_id, call)


@bot.callback_query_handler(
    func=lambda call: call.data and call.data.startswith(
        ("cart_inc|", "cart_dec|", "cart_remove|", "cart_qty|")
    )
)
def handle_cart_action(call):
    chat_id = call.from_user.id
    init_user(chat_id)
    action, token = call.data.split("|", 1)
    group = find_cart_group(chat_id, token)
    if not group:
        bot.answer_callback_query(
            call.id,
            tr(chat_id, "Эта позиция уже изменилась.", "This cart item has changed."),
            show_alert=True,
        )
        send_cart(chat_id, call)
        return

    category, flavor, price, qty = group
    data = user_data[chat_id]
    cart = data["cart"]

    if action == "cart_qty":
        bot.answer_callback_query(
            call.id,
            tr(chat_id, f"В корзине: {qty} шт.", f"In cart: {qty} pcs"),
        )
        return

    if action == "cart_inc":
        resolved = resolve_product(token)
        stock = int(resolved[1].get("stock", 0)) if resolved else 0
        if qty >= stock:
            return bot.answer_callback_query(
                call.id,
                tr(
                    chat_id,
                    f"Доступно только {stock} шт.",
                    f"Only {stock} pcs are available.",
                ),
                show_alert=True,
            )
        cart.append({"category": category, "flavor": flavor, "price": price})
        bot.answer_callback_query(call.id, tr(chat_id, "Добавлено", "Added"))

    elif action == "cart_dec":
        for index, item in enumerate(cart):
            if item.get("category") == category and item.get("flavor") == flavor:
                cart.pop(index)
                break
        bot.answer_callback_query(call.id, tr(chat_id, "Количество уменьшено", "Quantity decreased"))

    else:
        user_data[chat_id]["cart"] = [
            item for item in cart
            if not (
                item.get("category") == category
                and item.get("flavor") == flavor
            )
        ]
        bot.answer_callback_query(
            call.id,
            tr(chat_id, "Позиция удалена", "Item removed"),
        )

    data.update({
        "pending_discount": 0,
        "pending_points_spent": 0,
        "wait_for_points": False,
        "wait_for_address": False,
        "wait_for_contact": False,
        "wait_for_comment": False,
    })
    clear_promo_state(data)
    save_user_cart(chat_id)
    send_cart(chat_id, call)


@ensure_user
@bot.callback_query_handler(func=lambda call: call.data == "clear_cart")
def handle_clear_cart(call):
    chat_id = call.from_user.id
    init_user(chat_id)
    bot.answer_callback_query(call.id)
    count, total = cart_totals(chat_id)
    if not count:
        send_cart(chat_id, call)
        return

    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        types.InlineKeyboardButton(
            text=tr(chat_id, "Да, очистить", "Yes, clear it"),
            callback_data="clear_cart_confirm",
        ),
        types.InlineKeyboardButton(
            text=tr(chat_id, "Отмена", "Cancel"),
            callback_data="clear_cart_cancel",
        ),
    )
    render_inline_screen(
        chat_id,
        tr(
            chat_id,
            f"<b>Очистить корзину?</b>\n\nБудут удалены все {count} шт. на сумму {format_money(total)}₺.",
            f"<b>Clear your cart?</b>\n\nAll {count} items totaling {format_money(total)}₺ will be removed.",
        ),
        kb,
        call,
        allow_media_edit=False,
    )


@bot.callback_query_handler(func=lambda call: call.data == "clear_cart_confirm")
def handle_clear_cart_confirm(call):
    chat_id = call.from_user.id
    init_user(chat_id)
    user_data[chat_id].update({
        "cart": [],
        "pending_discount": 0,
        "pending_points_spent": 0,
        "wait_for_points": False,
        "wait_for_address": False,
        "wait_for_contact": False,
        "wait_for_comment": False,
    })
    clear_promo_state(user_data[chat_id])
    save_user_cart(chat_id)
    bot.answer_callback_query(call.id, tr(chat_id, "Корзина очищена", "Cart cleared"))
    send_cart(chat_id, call)


@bot.callback_query_handler(func=lambda call: call.data == "clear_cart_cancel")
def handle_clear_cart_cancel(call):
    chat_id = call.from_user.id
    init_user(chat_id)
    bot.answer_callback_query(call.id)
    send_cart(chat_id, call)


@bot.callback_query_handler(
    func=lambda call: call.data and (
        call.data.startswith("remove_item|") or call.data.startswith("edit_item|")
    )
)
def handle_stale_cart_button(call):
    chat_id = call.from_user.id
    init_user(chat_id)
    bot.answer_callback_query(
        call.id,
        tr(
            chat_id,
            "Эта кнопка корзины устарела. Откройте корзину снова.",
            "This cart button is outdated. Please open your cart again.",
        ),
        show_alert=True,
    )
    send_cart(chat_id, call)


def checkout_conversion_text(chat_id: int, total_after: int | float, qty: int) -> str:
    rates = fetch_rates()
    if not all(rates.get(code, 0) for code in ("RUB", "USD", "EUR", "UAH")):
        return tr(
            chat_id,
            " (≈ ₽—, €—, $—, ₴—; курсы временно недоступны)",
            " (≈ ₽—, €—, $—, ₴—; rates temporarily unavailable)",
        )
    rub = round(total_after * rates["RUB"] + 500 * qty, 2)
    usd = round(total_after * rates["USD"] + 2 * qty, 2)
    eur = round(total_after * rates["EUR"] + 2 * qty, 2)
    uah = round(total_after * rates["UAH"] + 350 * qty, 2)
    return (
        f" (≈ {format_money(rub)}₽, "
        f"€{format_money(eur)}, "
        f"${format_money(usd)}, "
        f"₴{format_money(uah)})"
    )


def ask_saved_or_new_delivery_data(
    chat_id: int,
    total_try: int,
    points_to_spend: int = 0,
    call=None,
) -> bool:
    """Показывает первый шаг checkout: сохранённые либо новые данные."""
    data = user_data.get(chat_id, {})
    cart = data.get("cart", [])
    promo_discount = (
        min(int(data.get("promo_discount", 0) or 0), int(total_try))
        if normalize_promo_code(data.get("promo_code", ""))
        else 0
    )
    total_after = max(total_try - promo_discount - points_to_spend, 0)

    conn_check = get_db_connection()
    cur_check = conn_check.cursor()
    cur_check.execute(
        "SELECT last_address, last_contact FROM users WHERE chat_id = ?",
        (chat_id,),
    )
    row = cur_check.fetchone()
    cur_check.close()
    conn_check.close()

    data.update({
        "wait_for_points": False,
        "wait_for_address": False,
        "wait_for_contact": False,
        "wait_for_comment": False,
    })
    data["checkout_total_try"] = total_try
    conversion = checkout_conversion_text(chat_id, total_after, len(cart))

    if row and row[0] and row[1]:
        last_address, last_contact = row
        kb = types.InlineKeyboardMarkup(row_width=1)
        kb.add(types.InlineKeyboardButton(
            text=tr(chat_id, "✅ Использовать сохранённые данные", "✅ Use saved details"),
            callback_data="use_last_data",
        ))
        kb.add(types.InlineKeyboardButton(
            text=tr(chat_id, "✏️ Ввести новые данные", "✏️ Enter new details"),
            callback_data="enter_new_data",
        ))
        kb.add(types.InlineKeyboardButton(
            text=nav_text(chat_id, "cart"),
            callback_data="back_to_cart",
        ))
        kb.add(types.InlineKeyboardButton(
            text=nav_text(chat_id, "menu"),
            callback_data="go_back_to_categories",
        ))
        saved_data_text = tr(
            chat_id,
            "<b>1/3 · Доставка</b>\n\n"
            "Использовать сохранённые данные?\n\n"
            f"📍 {html.escape(str(last_address))}\n"
            f"📱 {html.escape(str(last_contact))}\n\n"
            f"<b>К оплате: {format_money(total_after)}₺</b>{conversion}",
            "<b>1/3 · Delivery</b>\n\n"
            "Use your saved details?\n\n"
            f"📍 {html.escape(str(last_address))}\n"
            f"📱 {html.escape(str(last_contact))}\n\n"
            f"<b>Amount due: {format_money(total_after)}₺</b>{conversion}",
        )
        render_inline_screen(
            chat_id,
            saved_data_text,
            kb,
            call,
            allow_media_edit=False,
        )
        return True

    data["wait_for_address"] = True
    message_text = tr(
        chat_id,
        "<b>1/3 · Доставка</b>\n\n"
        f"<b>К оплате: {format_money(total_after)}₺</b>{conversion}\n\n"
        "Укажите адрес доставки:",
        "<b>1/3 · Delivery</b>\n\n"
        f"<b>Amount due: {format_money(total_after)}₺</b>{conversion}\n\n"
        "Enter the delivery address:",
    )
    if call is not None:
        disable_inline_keyboard(call)
    bot.send_message(
        chat_id,
        message_text,
        parse_mode="HTML",
        reply_markup=address_keyboard(chat_id),
    )
    return False


# ------------------------------------------------------------------------
#   24. Начало checkout и выбор баллов на финальном экране
# ------------------------------------------------------------------------
def checkout_points_state(chat_id: int) -> tuple[int, int, int]:
    """Возвращает баланс, максимум для списания и сумму товаров."""
    data = user_data.get(chat_id, {})
    total_try = int(sum(float(item.get("price", 0)) for item in data.get("cart", [])))
    promo_discount = (
        min(int(data.get("promo_discount", 0) or 0), total_try)
        if normalize_promo_code(data.get("promo_code", ""))
        else 0
    )
    connection = get_db_connection()
    cursor = connection.cursor()
    cursor.execute("SELECT points FROM users WHERE chat_id = ?", (chat_id,))
    row = cursor.fetchone()
    cursor.close()
    connection.close()
    user_points = int(row[0]) if row else 0
    max_points = min(user_points, max(total_try - promo_discount, 0))
    return user_points, max_points, total_try


def show_points_choice(chat_id: int, call=None) -> None:
    user_points, max_points, total_try = checkout_points_state(chat_id)
    data = user_data[chat_id]
    selected_points = min(
        int(data.get("pending_points_spent", 0) or 0),
        max_points,
    )
    data.update({
        "wait_for_points": False,
        "temp_total_try": total_try,
        "temp_user_points": user_points,
        "pending_discount": selected_points,
        "pending_points_spent": selected_points,
    })
    selected_line = tr(
        chat_id,
        f"\nСейчас выбрано: <b>{selected_points}</b> баллов.",
        f"\nCurrently selected: <b>{selected_points}</b> points.",
    ) if selected_points else ""
    text = tr(
        chat_id,
        "<b>🎁 Баллы для заказа</b>\n\n"
        f"Баланс: {user_points}\n"
        f"Можно списать до <b>{max_points}</b> баллов."
        f"{selected_line}",
        "<b>🎁 Points for this order</b>\n\n"
        f"Balance: {user_points}\n"
        f"You can use up to <b>{max_points}</b> points."
        f"{selected_line}",
    )
    render_inline_screen(
        chat_id,
        text,
        points_choice_keyboard(chat_id, max_points),
        call,
        allow_media_edit=False,
    )


@ensure_user
@bot.callback_query_handler(func=lambda call: call.data == "finish_order")
def handle_finish_order(call):
    chat_id = call.from_user.id
    init_user(chat_id)
    bot.answer_callback_query(call.id)

    data = user_data.get(chat_id, {})
    cart = data.get("cart", [])
    if not cart:
        send_cart(chat_id, call)
        return

    clear_promo_state(data)

    total_try = sum(i['price'] for i in cart)
    data["pending_discount"] = 0
    data["pending_points_spent"] = 0
    data["temp_total_try"] = total_try
    user_data[chat_id] = data

    ask_saved_or_new_delivery_data(chat_id, total_try, 0, call)


@ensure_user
@bot.callback_query_handler(func=lambda call: call.data == "edit_points")
def handle_edit_points(call):
    chat_id = call.from_user.id
    user_points, max_points, _ = checkout_points_state(chat_id)
    if user_points <= 0:
        bot.answer_callback_query(
            call.id,
            tr(chat_id, "На балансе пока нет баллов.", "You do not have any points yet."),
            show_alert=True,
        )
        return
    if max_points <= 0:
        bot.answer_callback_query(
            call.id,
            tr(
                chat_id,
                "Сумма к оплате уже равна 0₺ — баллы не нужны.",
                "The amount due is already 0₺, so no points are needed.",
            ),
            show_alert=True,
        )
        return
    bot.answer_callback_query(call.id)
    show_points_choice(chat_id, call)


@bot.callback_query_handler(func=lambda call: call.data == "points_all")
def handle_points_all(call):
    chat_id = call.from_user.id
    init_user(chat_id)
    data = user_data[chat_id]
    _, max_points, _ = checkout_points_state(chat_id)
    data["pending_discount"] = max_points
    data["pending_points_spent"] = max_points
    if data.get("promo_code"):
        data["points_before_promo"] = max_points
    data["wait_for_points"] = False
    bot.answer_callback_query(call.id, tr(chat_id, "Баллы применены", "Points applied"))
    show_order_review(chat_id, call)


@bot.callback_query_handler(func=lambda call: call.data == "points_custom")
def handle_points_custom(call):
    chat_id = call.from_user.id
    init_user(chat_id)
    data = user_data[chat_id]
    data["wait_for_points"] = True
    user_points, max_points, total_try = checkout_points_state(chat_id)
    data["temp_user_points"] = user_points
    data["temp_total_try"] = total_try
    bot.answer_callback_query(call.id)
    disable_inline_keyboard(call)
    bot.send_message(
        chat_id,
        tr(
            chat_id,
            f"<b>🎁 Баллы для заказа</b>\n\nВведите число от 0 до {max_points}:",
            f"<b>🎁 Points for this order</b>\n\nEnter a number from 0 to {max_points}:",
        ),
        parse_mode="HTML",
        reply_markup=text_entry_back_keyboard(chat_id, "review"),
    )


def show_comment_choice(chat_id: int, call=None) -> None:
    data = user_data[chat_id]
    data.update({
        "wait_for_address": False,
        "wait_for_contact": False,
        "wait_for_comment": False,
    })
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(types.InlineKeyboardButton(
        text=tr(chat_id, "✏️ Добавить комментарий", "✏️ Add a comment"),
        callback_data="comment_add",
    ))
    kb.add(types.InlineKeyboardButton(
        text=tr(chat_id, "Без комментария", "No comment"),
        callback_data="comment_skip",
    ))
    kb.add(types.InlineKeyboardButton(
        text=nav_text(chat_id, "contact"),
        callback_data="back_to_contact",
    ))
    kb.add(types.InlineKeyboardButton(
        text=nav_text(chat_id, "menu"),
        callback_data="go_back_to_categories",
    ))
    render_inline_screen(
        chat_id,
        tr(
            chat_id,
            "<b>2/3 · Комментарий</b>\n\nХотите что-нибудь добавить к заказу?",
            "<b>2/3 · Comment</b>\n\nWould you like to add a note to your order?",
        ),
        kb,
        call,
        allow_media_edit=False,
    )


@ensure_user
@bot.callback_query_handler(func=lambda c: c.data == "use_last_data")
def handle_use_last_data(call):
    chat_id = call.from_user.id
    init_user(chat_id)
    bot.answer_callback_query(call.id)

    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute(
        "SELECT last_address, last_contact FROM users WHERE chat_id = ?",
        (chat_id,),
    )
    row = cur.fetchone()
    cur.close()
    conn.close()

    if row and row[0] and row[1]:
        data = user_data[chat_id]
        data["address"] = row[0]
        data["contact"] = row[1]
        data["delivery_used_saved"] = True
        data["comment"] = ""
        show_comment_choice(chat_id, call)
        return

    user_data[chat_id]["wait_for_address"] = True
    disable_inline_keyboard(call)
    bot.send_message(
        chat_id,
        tr(
            chat_id,
            "<b>1/3 · Доставка</b>\n\nСохранённых данных пока нет. Укажите адрес:",
            "<b>1/3 · Delivery</b>\n\nNo saved details were found. Enter your address:",
        ),
        parse_mode="HTML",
        reply_markup=address_keyboard(chat_id),
    )


@ensure_user
@bot.callback_query_handler(func=lambda c: c.data == "enter_new_data")
def handle_enter_new_data(call):
    chat_id = call.from_user.id
    init_user(chat_id)
    bot.answer_callback_query(call.id)

    data = user_data[chat_id]
    data["address"] = ""
    data["contact"] = ""
    data["delivery_used_saved"] = False
    data["wait_for_address"] = True
    data["wait_for_contact"] = False
    data["wait_for_comment"] = False

    disable_inline_keyboard(call)
    bot.send_message(
        chat_id,
        tr(
            chat_id,
            "<b>1/3 · Доставка</b>\n\nВведите новый адрес:",
            "<b>1/3 · Delivery</b>\n\nEnter a new address:",
        ),
        parse_mode="HTML",
        reply_markup=address_keyboard(chat_id),
    )


@bot.callback_query_handler(func=lambda call: call.data == "back_to_delivery")
def handle_back_to_delivery(call):
    chat_id = call.from_user.id
    init_user(chat_id)
    bot.answer_callback_query(call.id)
    data = user_data[chat_id]
    ask_saved_or_new_delivery_data(
        chat_id,
        int(data.get("temp_total_try", 0) or sum(item["price"] for item in data.get("cart", []))),
        int(data.get("pending_points_spent", 0) or 0),
        call,
    )


@bot.callback_query_handler(func=lambda call: call.data == "comment_add")
def handle_comment_add(call):
    chat_id = call.from_user.id
    init_user(chat_id)
    bot.answer_callback_query(call.id)
    user_data[chat_id]["wait_for_comment"] = True
    disable_inline_keyboard(call)
    bot.send_message(
        chat_id,
        tr(
            chat_id,
            "<b>2/3 · Комментарий</b>\n\nНапишите комментарий одним сообщением:",
            "<b>2/3 · Comment</b>\n\nSend your comment in one message:",
        ),
        parse_mode="HTML",
        reply_markup=text_entry_back_keyboard(
            chat_id,
            "contact",
            include_no_comment=True,
        ),
    )


@bot.callback_query_handler(func=lambda call: call.data == "comment_skip")
def handle_comment_skip(call):
    chat_id = call.from_user.id
    init_user(chat_id)
    bot.answer_callback_query(call.id)
    data = user_data[chat_id]
    data["comment"] = "—"
    data["wait_for_comment"] = False
    show_order_review(chat_id, call)

# ------------------------------------------------------------------------
#   25. Handler: ввод количества баллов для списания
# ------------------------------------------------------------------------
@ensure_user
@bot.message_handler(
    func=lambda m: user_data.get(m.chat.id, {}).get("wait_for_points"),
    content_types=['text']
)
def handle_points_input(message):
    chat_id = message.chat.id
    data = user_data.get(chat_id, {})
    text = message.text.strip()
    user_points, max_points, total_try = checkout_points_state(chat_id)
    data["temp_user_points"] = user_points
    data["temp_total_try"] = total_try

    if text == nav_text(chat_id, "menu"):
        leave_checkout_to_main(chat_id)
        return

    if is_navigation_text(chat_id, text, "review"):
        data["wait_for_points"] = False
        bot.send_message(
            chat_id,
            tr(chat_id, "Возвращаемся к заказу.", "Back to your order."),
            reply_markup=types.ReplyKeyboardRemove(),
        )
        show_order_review(chat_id)
        return

    # --- проверяем корректность ввода ---
    if not text.isdigit():
        bot.send_message(
            chat_id,
            t(chat_id, "invalid_points").format(max_points=max_points),
            reply_markup=text_entry_back_keyboard(chat_id, "review"),
        )
        return

    points_to_spend = int(text)

    if points_to_spend < 0 or points_to_spend > max_points:
        bot.send_message(
            chat_id,
            t(chat_id, "invalid_points").format(max_points=max_points),
            reply_markup=text_entry_back_keyboard(chat_id, "review"),
        )
        return

    # --- сохраняем скидку и ждём адрес; баллы НЕ списываем здесь ---
    data["pending_discount"] = points_to_spend
    data["pending_points_spent"] = points_to_spend
    if data.get("promo_code"):
        data["points_before_promo"] = points_to_spend
    data["wait_for_points"] = False

    user_data[chat_id] = data
    bot.send_message(
        chat_id,
        tr(
            chat_id,
            f"✅ Будет использовано {points_to_spend} баллов.",
            f"✅ {points_to_spend} points will be used.",
        ),
        reply_markup=types.ReplyKeyboardRemove(),
    )
    show_order_review(chat_id)

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

    if text == nav_text(chat_id, "menu"):
        leave_checkout_to_main(chat_id)
        return

    # --- Кнопка Назад ---
    back_destination = "review" if data.get("return_to_review_after_address") else "cart"
    if is_navigation_text(chat_id, text, back_destination):
        if data.pop("return_to_review_after_address", False):
            data['wait_for_address'] = False
            user_data[chat_id] = data
            bot.send_message(
                chat_id,
                tr(chat_id, "Возвращаемся к заказу.", "Back to your order."),
                reply_markup=types.ReplyKeyboardRemove(),
            )
            show_order_review(chat_id)
            return
        data['wait_for_address'] = False
        data['current_category'] = None

        bot.send_message(
            chat_id,
            tr(chat_id, "Возвращаемся в корзину.", "Back to your cart."),
            reply_markup=types.ReplyKeyboardRemove()
        )
        send_cart(chat_id)
        return

    # --- Выбор на карте ---
    if text == t(chat_id, "choose_on_map"):
        bot.send_message(
            chat_id,
            tr(
                chat_id,
                "Чтобы выбрать точку:\n📎 → Геопозиция → «Выбрать на карте» → метка → Отправить",
                "To choose a point:\n📎 → Location → Choose on map → place the pin → Send",
            ),
            reply_markup=text_entry_back_keyboard(chat_id, back_destination)
        )
        return

    # --- Адрес как venue ---
    if message.content_type == 'venue' and message.venue:
        v = message.venue
        address = f"{v.title}, {v.address}\n🌍 https://maps.google.com/?q={v.location.latitude},{v.location.longitude}"

    # --- Адрес как location ---
    elif message.content_type == 'location' and message.location:
        lat, lon = message.location.latitude, message.location.longitude
        address = f"🌍 https://maps.google.com/?q={lat},{lon}"

    # --- Ввод текста адреса ---
    elif text == t(chat_id, "enter_address_text"):
        bot.send_message(
            chat_id,
            t(chat_id, "enter_address"),
            reply_markup=text_entry_back_keyboard(chat_id, back_destination),
        )
        return

    elif message.content_type == 'text' and message.text:
        address = message.text.strip()

    else:
        bot.send_message(
            chat_id,
            t(chat_id, "error_invalid"),
            reply_markup=address_keyboard(chat_id, back_destination),
        )
        return

    # --- Сохраняем адрес ---
    data['address'] = address
    data['wait_for_address'] = False
    if data.pop("return_to_review_after_address", False):
        user_data[chat_id] = data
        bot.send_message(chat_id, tr(chat_id, "Адрес обновлён.", "Address updated."),
                         reply_markup=types.ReplyKeyboardRemove())
        show_order_review(chat_id)
        return
    data['wait_for_contact'] = True
    user_data[chat_id] = data

    # --- Переходим к контакту ---
    kb = contact_keyboard(chat_id)
    bot.send_message(
        chat_id,
        tr(
            chat_id,
            "<b>1/3 · Доставка</b>\n\nУкажите телефон или Telegram для связи:",
            "<b>1/3 · Delivery</b>\n\nEnter a phone number or Telegram username:",
        ),
        parse_mode="HTML",
        reply_markup=kb,
    )



# Альтернативный вариант с принудительным удалением reply-клавиатуры
@ensure_user
@bot.message_handler(
    func=lambda m: user_data.get(m.chat.id, {}).get("wait_for_contact"),
    content_types=['text', 'contact']
)
def handle_contact_input(message):
    chat_id = message.chat.id
    data = user_data.get(chat_id, {})
    text = message.text or ""

    if text == nav_text(chat_id, "menu"):
        leave_checkout_to_main(chat_id)
        return

    # --- Назад ---
    back_destination = "review" if data.get("return_to_review_after_contact") else "address"
    if is_navigation_text(chat_id, text, back_destination):
        if data.pop("return_to_review_after_contact", False):
            data['wait_for_contact'] = False
            user_data[chat_id] = data
            bot.send_message(chat_id, tr(chat_id, "Возвращаемся к заказу.", "Back to your order."),
                             reply_markup=types.ReplyKeyboardRemove())
            show_order_review(chat_id)
            return
        data['wait_for_address'] = True
        data['wait_for_contact'] = False
        kb = address_keyboard(chat_id)
        bot.send_message(
            chat_id,
            tr(
                chat_id,
                "<b>1/3 · Доставка</b>\n\nУкажите адрес доставки:",
                "<b>1/3 · Delivery</b>\n\nEnter the delivery address:",
            ),
            parse_mode="HTML",
            reply_markup=kb,
        )
        user_data[chat_id] = data
        return

    # --- Ввод ника ---
    if text == t(chat_id, "enter_nickname"):
        bot.send_message(
            chat_id,
            tr(chat_id, "Введите ваш Telegram-ник (без @):", "Enter your Telegram username (without @):"),
            reply_markup=text_entry_back_keyboard(chat_id, back_destination),
        )
        return

    # --- Ввод контакта ---
    if message.content_type == 'contact' and message.contact:
        contact = message.contact.phone_number
    elif message.content_type == 'text' and message.text:
        raw_contact = message.text.strip()
        if re.fullmatch(r"\+?[0-9()\-\s]{7,24}", raw_contact):
            contact = raw_contact
        else:
            contact = "@" + raw_contact.lstrip("@")
    else:
        bot.send_message(
            chat_id,
            t(chat_id, "enter_contact"),
            reply_markup=contact_keyboard(chat_id, back_destination),
        )
        return

    data['contact'] = contact
    data['wait_for_contact'] = False
    if data.pop("return_to_review_after_contact", False):
        user_data[chat_id] = data
        bot.send_message(chat_id, tr(chat_id, "Контакт обновлён.", "Contact updated."),
                         reply_markup=types.ReplyKeyboardRemove())
        show_order_review(chat_id)
        return
    data['wait_for_comment'] = False
    user_data[chat_id] = data
    bot.send_message(
        chat_id,
        tr(chat_id, "Контакт сохранён.", "Contact saved."),
        reply_markup=types.ReplyKeyboardRemove()
    )
    show_comment_choice(chat_id)


def show_order_review(chat_id: int, call=None) -> None:
    """Показывает все данные заказа до его окончательной отправки."""
    data = user_data.get(chat_id, {})
    cart = data.get("cart", [])
    if not cart:
        send_cart(chat_id, call)
        return
    if not data.get("address"):
        data["wait_for_address"] = True
        if call is not None:
            disable_inline_keyboard(call)
        bot.send_message(
            chat_id,
            tr(
                chat_id,
                "<b>1/3 · Доставка</b>\n\nУкажите адрес доставки:",
                "<b>1/3 · Delivery</b>\n\nEnter the delivery address:",
            ),
            parse_mode="HTML",
            reply_markup=address_keyboard(chat_id),
        )
        return
    if not data.get("contact"):
        data["wait_for_contact"] = True
        if call is not None:
            disable_inline_keyboard(call)
        bot.send_message(
            chat_id,
            tr(
                chat_id,
                "<b>1/3 · Доставка</b>\n\nУкажите контакт для связи:",
                "<b>1/3 · Delivery</b>\n\nEnter your contact details:",
            ),
            parse_mode="HTML",
            reply_markup=contact_keyboard(chat_id),
        )
        return

    data["wait_for_promo"] = False
    total_before = sum(int(item["price"]) for item in cart)
    promo_code = normalize_promo_code(data.get("promo_code", ""))
    promo_discount = min(
        int(data.get("promo_discount", 0) or 0),
        total_before,
    ) if promo_code else 0
    # Промокод имеет приоритет, поэтому при заказе на всю сумму лишние
    # выбранные баллы сохраняются на балансе пользователя.
    user_points, max_points, _ = checkout_points_state(chat_id)
    points_spent = min(int(data.get("pending_points_spent", 0) or 0), max_points)
    data["pending_points_spent"] = points_spent
    data["pending_discount"] = points_spent
    total_after = max(total_before - promo_discount - points_spent, 0)
    conversion = checkout_conversion_text(chat_id, total_after, len(cart))
    item_lines = []
    for (cat, flavor, price), qty in get_grouped_cart(chat_id):
        item_lines.append(
            f"• {html.escape(cat)} — {html.escape(flavor)} × {qty} = {price * qty}₺"
        )

    address = html.escape(str(data.get("address") or "—"))
    contact = html.escape(str(data.get("contact") or "—"))
    comment = html.escape(str(data.get("comment") or "—"))
    if user_data.get(chat_id, {}).get("lang") == "en":
        points_line = f"Points: −{points_spent}\n" if points_spent else ""
        promo_line = (
            f"Promo code {html.escape(promo_code)}: −{format_money(promo_discount)}₺\n"
            if promo_code else ""
        )
        review_text = (
            "<b>3/3 · Check your order</b>\n\n"
            + "\n".join(item_lines)
            + f"\n\nSubtotal: {format_money(total_before)}₺\n"
            + points_line
            + promo_line
            + f"<b>Amount due: {format_money(total_after)}₺</b>{conversion}\n\n"
            + f"📍 Address: {address}\n📱 Contact: {contact}\n💬 Comment: {comment}"
        )
    else:
        points_line = f"Баллы: −{points_spent}\n" if points_spent else ""
        promo_line = (
            f"Промокод {html.escape(promo_code)}: −{format_money(promo_discount)}₺\n"
            if promo_code else ""
        )
        review_text = (
            "<b>3/3 · Проверьте заказ</b>\n\n"
            + "\n".join(item_lines)
            + f"\n\nТовары: {format_money(total_before)}₺\n"
            + points_line
            + promo_line
            + f"<b>К оплате: {format_money(total_after)}₺</b>{conversion}\n\n"
            + f"📍 Адрес: {address}\n📱 Контакт: {contact}\n💬 Комментарий: {comment}"
        )

    kb = types.InlineKeyboardMarkup(row_width=2)
    if user_points > 0 and max_points > 0:
        points_button_text = (
            tr(
                chat_id,
                f"🎁 Баллы: −{points_spent}",
                f"🎁 Points: −{points_spent}",
            )
            if points_spent
            else tr(chat_id, "🎁 Списать баллы", "🎁 Use points")
        )
        kb.add(types.InlineKeyboardButton(
            text=points_button_text,
            callback_data="edit_points",
        ))
    if promo_code:
        kb.add(
            types.InlineKeyboardButton(
                text=tr(chat_id, "🎟 Изменить промокод", "🎟 Change promo code"),
                callback_data="enter_promo",
            ),
            types.InlineKeyboardButton(
                text=tr(chat_id, "❌ Убрать промокод", "❌ Remove promo code"),
                callback_data="remove_promo",
            ),
        )
    else:
        kb.add(types.InlineKeyboardButton(
            text=tr(chat_id, "🎟 Промокод", "🎟 Promo code"),
            callback_data="enter_promo",
        ))
    kb.add(types.InlineKeyboardButton(
        text=tr(chat_id, "✅ Подтвердить заказ", "✅ Confirm order"),
        callback_data="confirm_order",
    ))
    kb.add(types.InlineKeyboardButton(
        text=tr(chat_id, "🛒 Изменить корзину", "🛒 Edit cart"),
        callback_data="back_to_cart",
    ))
    kb.add(
        types.InlineKeyboardButton(
            text=tr(chat_id, "📍 Изменить адрес", "📍 Edit address"),
            callback_data="edit_order_address",
        ),
        types.InlineKeyboardButton(
            text=tr(chat_id, "📱 Изменить контакт", "📱 Edit contact"),
            callback_data="edit_order_contact",
        ),
    )
    kb.add(types.InlineKeyboardButton(
        text=tr(chat_id, "💬 Изменить комментарий", "💬 Edit comment"),
        callback_data="edit_order_comment",
    ))
    kb.add(types.InlineKeyboardButton(
        text=tr(chat_id, "⬅️ К комментарию", "⬅️ Back to comment"),
        callback_data="back_to_comment",
    ))
    kb.add(types.InlineKeyboardButton(
        text=nav_text(chat_id, "menu"),
        callback_data="go_back_to_categories",
    ))
    render_inline_screen(
        chat_id,
        review_text,
        kb,
        call,
        allow_media_edit=False,
    )


def promo_input_keyboard(chat_id: int) -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(types.InlineKeyboardButton(
        text=nav_text(chat_id, "review"),
        callback_data="review_order",
    ))
    kb.add(types.InlineKeyboardButton(
        text=nav_text(chat_id, "menu"),
        callback_data="go_back_to_categories",
    ))
    return kb


@ensure_user
@bot.callback_query_handler(func=lambda call: call.data == "enter_promo")
def handle_enter_promo(call):
    chat_id = call.from_user.id
    data = user_data[chat_id]
    if not data.get("promo_code") and "points_before_promo" not in data:
        data["points_before_promo"] = int(data.get("pending_points_spent", 0) or 0)
    data["wait_for_promo"] = True
    data.update({
        "wait_for_points": False,
        "wait_for_address": False,
        "wait_for_contact": False,
        "wait_for_comment": False,
    })
    bot.answer_callback_query(call.id)
    disable_inline_keyboard(call)
    bot.send_message(
        chat_id,
        tr(
            chat_id,
            "Введите промокод, чтобы получить скидку на заказ:",
            "Enter a promo code to receive a discount on your order:",
        ),
        reply_markup=promo_input_keyboard(chat_id),
    )


@ensure_user
@bot.callback_query_handler(func=lambda call: call.data == "remove_promo")
def handle_remove_promo(call):
    chat_id = call.from_user.id
    data = user_data[chat_id]
    points_to_restore = int(
        data.get("points_before_promo", data.get("pending_points_spent", 0)) or 0
    )
    cart_total = int(sum(float(item.get("price", 0)) for item in data.get("cart", [])))
    clear_promo_state(data)
    data["pending_points_spent"] = min(points_to_restore, cart_total)
    data["pending_discount"] = data["pending_points_spent"]
    bot.answer_callback_query(
        call.id,
        tr(chat_id, "Промокод убран", "Promo code removed"),
    )
    show_order_review(chat_id, call)


@ensure_user
@bot.message_handler(
    func=lambda message: user_data.get(message.chat.id, {}).get("wait_for_promo"),
    content_types=['text'],
)
def handle_promo_input(message):
    chat_id = message.chat.id
    data = user_data[chat_id]
    text = (message.text or "").strip()
    if text == nav_text(chat_id, "menu"):
        leave_checkout_to_main(chat_id)
        return
    if is_navigation_text(chat_id, text, "review"):
        data["wait_for_promo"] = False
        bot.send_message(
            chat_id,
            tr(chat_id, "Возвращаемся к заказу.", "Back to your order."),
            reply_markup=types.ReplyKeyboardRemove(),
        )
        show_order_review(chat_id)
        return

    code = normalize_promo_code(text)
    if not is_valid_promo_code(code):
        bot.send_message(
            chat_id,
            tr(
                chat_id,
                "Промокод может содержать 2–32 буквы, цифры, дефис или подчёркивание.",
                "A promo code may contain 2–32 letters, numbers, hyphens, or underscores.",
            ),
            reply_markup=promo_input_keyboard(chat_id),
        )
        return

    connection = get_db_connection()
    cursor = connection.cursor()
    cursor.execute(
        "SELECT promo_id, discount_amount, usage_limit, used_count, active "
        "FROM promo_codes WHERE code = ?",
        (code,),
    )
    promo_row = cursor.fetchone()
    already_used = False
    if promo_row:
        cursor.execute(
            "SELECT 1 FROM promo_redemptions WHERE promo_id = ? AND chat_id = ?",
            (promo_row[0], chat_id),
        )
        already_used = cursor.fetchone() is not None
    cursor.close()
    connection.close()

    if not promo_row or not int(promo_row[4]) or int(promo_row[3]) >= int(promo_row[2]):
        error_text = tr(
            chat_id,
            "Такой промокод не найден или его лимит уже закончился.",
            "This promo code was not found or has reached its usage limit.",
        )
    elif already_used:
        error_text = tr(
            chat_id,
            "Вы уже использовали этот промокод.",
            "You have already used this promo code.",
        )
    else:
        error_text = ""

    if error_text:
        bot.send_message(
            chat_id,
            error_text,
            reply_markup=promo_input_keyboard(chat_id),
        )
        return

    cart_total = sum(float(item.get("price", 0)) for item in data.get("cart", []))
    discount = min(int(promo_row[1]), int(cart_total))
    data.update({
        "wait_for_promo": False,
        "promo_code": code,
        "promo_discount": discount,
    })
    bot.send_message(
        chat_id,
        tr(
            chat_id,
            f"✅ Промокод применён. Скидка: {format_money(discount)}₺.",
            f"✅ Promo code applied. Discount: {format_money(discount)}₺.",
        ),
        reply_markup=types.ReplyKeyboardRemove(),
    )
    show_order_review(chat_id)


@ensure_user
@bot.callback_query_handler(func=lambda call: call.data == "review_order")
def handle_review_order(call):
    chat_id = call.from_user.id
    bot.answer_callback_query(call.id)
    user_data[chat_id].update({
        "wait_for_points": False,
        "wait_for_promo": False,
        "wait_for_comment": False,
    })
    show_order_review(chat_id, call)


@ensure_user
@bot.callback_query_handler(func=lambda call: call.data == "edit_order_address")
def handle_edit_order_address(call):
    chat_id = call.from_user.id
    bot.answer_callback_query(call.id)
    data = user_data[chat_id]
    data.update({"wait_for_address": True, "wait_for_contact": False, "wait_for_comment": False})
    data["return_to_review_after_address"] = True
    disable_inline_keyboard(call)
    bot.send_message(
        chat_id,
        tr(chat_id, "Введите новый адрес:", "Enter a new address:"),
        reply_markup=address_keyboard(chat_id, "review"),
    )


@ensure_user
@bot.callback_query_handler(func=lambda call: call.data == "edit_order_contact")
def handle_edit_order_contact(call):
    chat_id = call.from_user.id
    bot.answer_callback_query(call.id)
    data = user_data[chat_id]
    data.update({"wait_for_address": False, "wait_for_contact": True, "wait_for_comment": False})
    data["return_to_review_after_contact"] = True
    disable_inline_keyboard(call)
    bot.send_message(
        chat_id,
        t(chat_id, "enter_contact"),
        reply_markup=contact_keyboard(chat_id, "review"),
    )


@ensure_user
@bot.callback_query_handler(func=lambda call: call.data == "edit_order_comment")
def handle_edit_order_comment(call):
    chat_id = call.from_user.id
    bot.answer_callback_query(call.id)
    data = user_data[chat_id]
    data.update({"wait_for_address": False, "wait_for_contact": False, "wait_for_comment": True})
    data["return_to_review_after_comment"] = True
    disable_inline_keyboard(call)
    bot.send_message(
        chat_id,
        tr(chat_id, "Введите новый комментарий:", "Enter a new comment:"),
        reply_markup=text_entry_back_keyboard(
            chat_id,
            "review",
            include_no_comment=True,
        ),
    )


@ensure_user
@bot.callback_query_handler(func=lambda call: call.data == "back_to_comment")
def handle_back_to_comment(call):
    chat_id = call.from_user.id
    bot.answer_callback_query(call.id)
    data = user_data[chat_id]
    data["wait_for_comment"] = False
    show_comment_choice(chat_id, call)


@ensure_user
@bot.message_handler(
    func=lambda m: user_data.get(m.chat.id, {}).get("wait_for_comment"),
    content_types=['text']
)
def handle_comment_input(message):
    chat_id = message.chat.id
    data = user_data.get(chat_id, {})
    text = message.text.strip()
    editing_review = bool(data.get("return_to_review_after_comment"))
    back_destination = "review" if editing_review else "contact"

    if text == nav_text(chat_id, "menu"):
        leave_checkout_to_main(chat_id)
        return

    if is_navigation_text(chat_id, text, back_destination):
        if data.pop("return_to_review_after_comment", False):
            data["wait_for_comment"] = False
            bot.send_message(
                chat_id,
                tr(chat_id, "Возвращаемся к заказу.", "Back to your order."),
                reply_markup=types.ReplyKeyboardRemove(),
            )
            show_order_review(chat_id)
            return
        data["wait_for_comment"] = False
        data["wait_for_contact"] = True
        bot.send_message(chat_id, t(chat_id, "enter_contact"), reply_markup=contact_keyboard(chat_id))
        return

    # --- сохраняем комментарий ---
    if text == no_comment_text(chat_id):
        data["comment"] = "—"
    elif text:
        data["comment"] = text
    else:
        data["comment"] = "—"

    data["wait_for_comment"] = False
    data.pop("return_to_review_after_comment", None)
    user_data[chat_id] = data
    bot.send_message(
        chat_id,
        tr(chat_id, "💬 Комментарий сохранён.", "💬 Comment saved."),
        reply_markup=types.ReplyKeyboardRemove(),
    )
    show_order_review(chat_id)


# ------------------------------------------------------------------------
#   Callback: финальное оформление заказа (списание баллов, запись в БД)
# ------------------------------------------------------------------------
@ensure_user
@bot.callback_query_handler(func=lambda call: call.data == "confirm_order")
def finalize_order(call):
    chat_id = call.from_user.id
    data = user_data.get(chat_id, {})
    bot.answer_callback_query(call.id)

    cart = data.get("cart", [])
    if not cart:
        bot.send_message(chat_id, t(chat_id, "cart_empty"))
        return

    # --- СЧИТАЕМ ИТОГИ ---
    total_try = sum(i['price'] for i in cart)
    pending_points = int(data.get("pending_points_spent", 0) or 0)
    requested_promo_code = normalize_promo_code(data.get("promo_code", ""))
    promo_code = ""
    promo_discount = 0
    comment = data.get("comment", "") or "—"
    address = data.get("address", "")
    contact = data.get("contact", "")

    if not address or not contact:
        bot.send_message(
            chat_id,
            tr(
                chat_id,
                "Не хватает адреса или контакта. Вернитесь назад и заполните данные.",
                "Address or contact details are missing. Go back and complete them.",
            ),
            reply_markup=back_to_main_keyboard(chat_id),
        )
        return

    if data.get("order_processing"):
        bot.send_message(
            chat_id,
            tr(chat_id, "Заказ уже оформляется. Подождите немного.", "Your order is already being processed."),
        )
        return
    data["order_processing"] = True
    disable_inline_keyboard(call)

    # Окончательная сумма пересчитывается внутри транзакции после повторной
    # проверки промокода. Здесь задаём безопасные значения по умолчанию.
    total_after = max(total_try - pending_points, 0)

    # --- проверяем склад ---
    needed = {}
    for it in cart:
        key = (it["category"], it["flavor"])
        needed[key] = needed.get(key, 0) + 1

    pts_earned = total_after // PURCHASE_POINTS_DIVISOR
    items_json = json.dumps(cart, ensure_ascii=False)
    now = datetime.datetime.utcnow().isoformat()
    inviter = None
    order_id = None
    stock_changes = []
    conn_local = None

    try:
        # Один заказ целиком проходит под блокировкой: два одновременных клика
        # не смогут продать один и тот же остаток.
        with menu_lock:
            selected_stock_items = []
            for (cat0, flavor0), qty_needed in needed.items():
                item_obj = next(
                    (
                        item
                        for item in menu.get(cat0, {}).get("flavors", [])
                        if item.get("flavor") == flavor0
                    ),
                    None,
                )
                if not item_obj or int(item_obj.get("stock", 0)) < qty_needed:
                    data["order_processing"] = False
                    bot.send_message(
                        chat_id,
                        tr(
                            chat_id,
                            f"😕 К сожалению, «{flavor0}» больше не доступен в нужном количестве.",
                            f"😕 Unfortunately, “{flavor0}” is no longer available in the requested quantity.",
                        ),
                    )
                    send_cart(chat_id)
                    return
                selected_stock_items.append((item_obj, qty_needed))

            conn_local = get_db_connection()
            cursor_local = conn_local.cursor()
            cursor_local.execute("BEGIN IMMEDIATE")
            cursor_local.execute(
                "SELECT points, referred_by FROM users WHERE chat_id = ?",
                (chat_id,),
            )
            user_row = cursor_local.fetchone()
            current_points = int(user_row[0]) if user_row else 0
            inviter = user_row[1] if user_row else None

            promo_id = None
            if requested_promo_code:
                cursor_local.execute(
                    "SELECT promo_id, discount_amount, usage_limit, used_count, active "
                    "FROM promo_codes WHERE code = ?",
                    (requested_promo_code,),
                )
                promo_row = cursor_local.fetchone()
                if (
                    not promo_row
                    or not int(promo_row[4])
                    or int(promo_row[3]) >= int(promo_row[2])
                ):
                    raise PromoCodeError("promo_unavailable")
                promo_id = int(promo_row[0])
                cursor_local.execute(
                    "SELECT 1 FROM promo_redemptions WHERE promo_id = ? AND chat_id = ?",
                    (promo_id, chat_id),
                )
                if cursor_local.fetchone():
                    raise PromoCodeError("promo_already_used")
                promo_code = requested_promo_code
                promo_discount = min(int(promo_row[1]), int(total_try))

            max_points_for_order = max(int(total_try) - promo_discount, 0)
            if pending_points < 0 or pending_points > min(current_points, max_points_for_order):
                raise ValueError("invalid_points_balance")
            total_after = max(total_try - promo_discount - pending_points, 0)
            pts_earned = int(total_after) // PURCHASE_POINTS_DIVISOR

            for item_obj, qty_needed in selected_stock_items:
                old_stock = int(item_obj.get("stock", 0))
                stock_changes.append((item_obj, old_stock))
                item_obj["stock"] = old_stock - qty_needed
            save_menu_safely()

            cursor_local.execute(
                "INSERT INTO orders "
                "(chat_id, items_json, total, timestamp, points_spent, points_earned, "
                "promo_code, promo_discount) VALUES (?,?,?,?,?,?,?,?)",
                (
                    chat_id,
                    items_json,
                    total_after,
                    now,
                    pending_points,
                    pts_earned,
                    promo_code or None,
                    promo_discount,
                ),
            )
            order_id = cursor_local.lastrowid
            if promo_id is not None:
                cursor_local.execute(
                    """
                    UPDATE promo_codes
                    SET used_count = used_count + 1,
                        active = CASE
                            WHEN used_count + 1 >= usage_limit THEN 0
                            ELSE 1
                        END
                    WHERE promo_id = ? AND active = 1 AND used_count < usage_limit
                    """,
                    (promo_id,),
                )
                if cursor_local.rowcount != 1:
                    raise PromoCodeError("promo_unavailable")
                cursor_local.execute(
                    "INSERT INTO promo_redemptions "
                    "(promo_id, chat_id, order_id, discount_amount, redeemed_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (promo_id, chat_id, order_id, promo_discount, now),
                )
            cursor_local.execute(
                "UPDATE users SET points = points - ? + ?, last_address = ?, last_contact = ? "
                "WHERE chat_id = ?",
                (pending_points, pts_earned, address, contact, chat_id),
            )
            # Заказ и очистка сохранённой корзины фиксируются одной транзакцией.
            # Если Railway перезапустится сразу после commit, оформленный заказ
            # уже не появится в корзине повторно.
            cursor_local.execute(
                """
                INSERT INTO user_carts (chat_id, items_json, updated_at)
                VALUES (?, '[]', ?)
                ON CONFLICT(chat_id) DO UPDATE SET
                    items_json = excluded.items_json,
                    updated_at = excluded.updated_at
                """,
                (chat_id, now),
            )
            if inviter:
                cursor_local.execute(
                    "UPDATE users SET points = points + ? WHERE chat_id = ?",
                    (REFERRAL_BONUS_POINTS, inviter),
                )
                cursor_local.execute(
                    "UPDATE users SET referred_by = NULL WHERE chat_id = ?",
                    (chat_id,),
                )
            conn_local.commit()
            cursor_local.close()
            conn_local.close()
            conn_local = None
    except PromoCodeError as exc:
        if conn_local is not None:
            conn_local.rollback()
            conn_local.close()
        with menu_lock:
            for item_obj, old_stock in stock_changes:
                item_obj["stock"] = old_stock
            if stock_changes:
                save_menu_safely()
        data["order_processing"] = False
        points_to_restore = int(
            data.get("points_before_promo", data.get("pending_points_spent", 0)) or 0
        )
        clear_promo_state(data)
        data["pending_points_spent"] = min(points_to_restore, int(total_try))
        data["pending_discount"] = data["pending_points_spent"]
        message_text = tr(
            chat_id,
            "Промокод больше недоступен. Он не применён — проверьте сумму заказа.",
            "The promo code is no longer available. It was removed — please check your order total.",
        )
        if str(exc) == "promo_already_used":
            message_text = tr(
                chat_id,
                "Вы уже использовали этот промокод. Он не применён к заказу.",
                "You have already used this promo code. It was removed from the order.",
            )
        bot.send_message(chat_id, message_text)
        show_order_review(chat_id)
        return
    except ValueError:
        if conn_local is not None:
            conn_local.rollback()
            conn_local.close()
        with menu_lock:
            for item_obj, old_stock in stock_changes:
                item_obj["stock"] = old_stock
            if stock_changes:
                save_menu_safely()
        data["order_processing"] = False
        data["pending_discount"] = 0
        data["pending_points_spent"] = 0
        bot.send_message(
            chat_id,
            tr(
                chat_id,
                "Баланс баллов изменился. Баллы не списаны — проверьте корзину ещё раз.",
                "Your points balance changed. No points were spent — please review the cart again.",
            ),
        )
        send_cart(chat_id)
        return
    except Exception as exc:
        if conn_local is not None:
            conn_local.rollback()
            conn_local.close()
        with menu_lock:
            for item_obj, old_stock in stock_changes:
                item_obj["stock"] = old_stock
            if stock_changes:
                try:
                    save_menu_safely()
                except Exception as restore_exc:
                    print(f"Failed to restore menu after order error: {restore_exc}")
        data["order_processing"] = False
        print(f"Order confirmation failed for {chat_id}: {exc}")
        bot.send_message(
            chat_id,
            tr(
                chat_id,
                "Не удалось оформить заказ. Корзина и баллы сохранены — попробуйте ещё раз.",
                "The order could not be completed. Your cart and points are unchanged — please try again.",
            ),
            reply_markup=back_to_main_keyboard(chat_id),
        )
        return

    # Заказ уже записан. Сразу очищаем корзину, чтобы повторное нажатие на
    # старую кнопку подтверждения не создало дубликат, даже если одно из
    # служебных уведомлений Telegram временно не отправится.
    data.update({
        "cart": [],
        "current_category": None,
        "wait_for_address": False,
        "wait_for_contact": False,
        "wait_for_comment": False,
        "wait_for_promo": False,
        "pending_discount": 0,
        "pending_points_spent": 0,
        "promo_code": "",
        "promo_discount": 0,
        "comment": "",
        "order_processing": False,
    })
    data.pop("points_before_promo", None)
    data.pop("return_to_review_after_address", None)
    data.pop("return_to_review_after_contact", None)
    data.pop("return_to_review_after_comment", None)
    user_data[chat_id] = data
    save_user_cart(chat_id)

    if inviter:
        try:
            init_user(inviter)
            bot.send_message(
                inviter,
                tr(
                    inviter,
                    f"🎉 Вам начислено {REFERRAL_BONUS_POINTS} бонусных баллов за приглашение нового клиента!",
                    f"🎉 You received {REFERRAL_BONUS_POINTS} bonus points for inviting a new customer!",
                ),
            )
        except Exception as exc:
            print(f"Referral notification failed for {inviter}: {exc}")

    # --- уведомления ---
    grouped_summary = {}
    for item in cart:
        key = (item["category"], item["flavor"], item["price"])
        grouped_summary[key] = grouped_summary.get(key, 0) + 1
    summary = "\n".join(
        f"{html.escape(str(category))}: {html.escape(str(flavor))} × {qty} — "
        f"{format_money(float(price) * qty)}₺"
        for (category, flavor, price), qty in grouped_summary.items()
    )

    conversion_suffix = checkout_conversion_text(chat_id, total_after, len(cart))

    username = getattr(call.from_user, "username", None)
    first_name = getattr(call.from_user, "first_name", None)
    customer_label = f"@{username}" if username else (first_name or str(chat_id))
    safe_customer = html.escape(str(customer_label))
    safe_address = html.escape(str(address))
    safe_contact = html.escape(str(contact))
    safe_comment = html.escape(str(comment))
    translated_comment = html.escape(translate_to_en(str(comment)))
    safe_promo_code = html.escape(promo_code)
    promo_line_ru = (
        f"🎟 Промокод {safe_promo_code}: −{format_money(promo_discount)}₺\n"
        if promo_code else ""
    )
    promo_line_en = (
        f"🎟 Promo code {safe_promo_code}: −{format_money(promo_discount)}₺\n"
        if promo_code else ""
    )

    full_rus = (
        f"📥 Новый заказ №{order_id} от {safe_customer}:\n\n"
        f"{summary}\n\n"
        f"{promo_line_ru}"
        f"Итог: {format_money(total_after)}₺{conversion_suffix}\n"
        f"📍 Адрес: {safe_address}\n"
        f"📱 Контакт: {safe_contact}\n"
        f"💬 Комментарий: {safe_comment}"
    )
    if PERSONAL_CHAT_ID:
        try:
            bot.send_message(PERSONAL_CHAT_ID, full_rus)
        except Exception as exc:
            print(f"Personal order notification failed for order {order_id}: {exc}")

    full_en = (
        f"📥 New order #{order_id} from {safe_customer}:\n\n"
        f"{summary}\n\n"
        f"{promo_line_en}"
        f"Total: {format_money(total_after)}₺{conversion_suffix}\n"
        f"📍 Address: {safe_address}\n"
        f"📱 Contact: {safe_contact}\n"
        f"💬 Comment: {translated_comment}"
    )
    kb_admin = admin_order_keyboard(order_id, chat_id)

    try:
        bot.send_message(GROUP_CHAT_ID, full_en, reply_markup=kb_admin)
    except Exception as exc:
        print(f"Group order notification failed for order {order_id}: {exc}")

    # --- сообщение пользователю ---
    bot.send_message(
        chat_id,
        t(chat_id, "order_accepted"),
        reply_markup=types.ReplyKeyboardRemove(),
    )
    if user_data.get(chat_id, {}).get("lang") == "en":
        user_order_summary = (
            f"📋 Your order #{order_id}:\n\n"
            f"{summary}\n\n"
            f"{promo_line_en}"
            f"Total: {format_money(total_after)}₺{conversion_suffix}\n"
            f"📍 Address: {safe_address}\n"
            f"📱 Contact: {safe_contact}\n"
            f"💬 Comment: {safe_comment}"
        )
    else:
        user_order_summary = (
            f"📋 Ваш заказ №{order_id}:\n\n"
            f"{summary}\n\n"
            f"{promo_line_ru}"
            f"Итог: {format_money(total_after)}₺{conversion_suffix}\n"
            f"📍 Адрес: {safe_address}\n"
            f"📱 Контакт: {safe_contact}\n"
            f"💬 Комментарий: {safe_comment}"
        )
    bot.send_message(chat_id, user_order_summary, reply_markup=back_to_main_keyboard(chat_id))


# ------------------------------------------------------------------------
#   Callback: возврат от комментария к контактным данным
# ------------------------------------------------------------------------
@ensure_user
@bot.callback_query_handler(func=lambda call: call.data == "back_to_contact")
def handle_back_to_contact(call):
    """Обработка нажатия кнопки 'Назад' в этапе комментария"""
    chat_id = call.from_user.id
    data = user_data.get(chat_id, {})

    bot.answer_callback_query(call.id)

    # Возвращаемся к предыдущему шагу (ввод контакта)
    data['wait_for_comment'] = False
    data['wait_for_contact'] = True

    # Показываем клавиатуру для ввода контакта
    kb = contact_keyboard(chat_id)
    bot.send_message(
        chat_id,
        t(chat_id, "enter_contact"),
        reply_markup=kb
    )

    user_data[chat_id] = data

# ------------------------------------------------------------------------
#   29. /change: перевод в режим редактирования меню (только на английском)
# ------------------------------------------------------------------------
@ensure_user
@bot.message_handler(commands=['change'])
def cmd_change(message):
    chat_id = message.chat.id

    # Доступ к /change только владельцу и только в личном чате с ботом.
    if not is_owner(message.from_user.id) or message.chat.type != "private":
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
            "wait_for_promo": False,
            "address": "",
            "contact": "",
            "comment": "",
            "pending_discount": 0,
            "pending_points_spent": 0,
            "promo_code": "",
            "promo_discount": 0,
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
        "wait_for_promo": False,
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
    if not is_owner(message.from_user.id):
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
            init_user(uid)
            bot.send_message(
                uid,
                tr(
                    uid,
                    "🚚 Новая поставка прибыла. Проверьте меню.",
                    "🚚 A new shipment has arrived. Check the menu.",
                ),
            )
        except Exception as e:
            print(f"Не удалось отправить сообщение {uid}: {e}")

    bot.reply_to(message, "✅ Сообщение о новой поставке разослано всем пользователям.")

@bot.message_handler(commands=['stock'])
def cmd_stock(message: types.Message):
    if not is_owner(message.from_user.id):
        return bot.reply_to(message, "❌ You do not have access to this command.")
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


def profile_back_keyboard(chat_id: int) -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(types.InlineKeyboardButton(
        text=tr(chat_id, "⬅️ Назад в профиль", "⬅️ Back to profile"),
        callback_data="profile",
    ))
    return kb


def show_profile(chat_id: int, call=None) -> None:
    init_user(chat_id)
    conn_local = get_db_connection()
    cursor_local = conn_local.cursor()
    cursor_local.execute("SELECT points FROM users WHERE chat_id = ?", (chat_id,))
    row = cursor_local.fetchone()
    points = int(row[0]) if row else 0
    cursor_local.execute("SELECT COUNT(*) FROM orders WHERE chat_id = ?", (chat_id,))
    order_count = int(cursor_local.fetchone()[0])
    cursor_local.close()
    conn_local.close()

    text = tr(
        chat_id,
        "<b>👤 Профиль</b>\n\n"
        f"🎁 Баллы: <b>{points}</b>\n"
        f"📦 Заказы: <b>{order_count}</b>",
        "<b>👤 Profile</b>\n\n"
        f"🎁 Points: <b>{points}</b>\n"
        f"📦 Orders: <b>{order_count}</b>",
    )
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        types.InlineKeyboardButton(
            text=tr(chat_id, "🎁 Мои баллы", "🎁 My points"),
            callback_data="profile_points",
        ),
        types.InlineKeyboardButton(
            text=tr(chat_id, "📦 Мои заказы", "📦 My orders"),
            callback_data="profile_history",
        ),
    )
    kb.add(types.InlineKeyboardButton(
        text=tr(chat_id, "🌐 Язык", "🌐 Language"),
        callback_data="profile_language",
    ))
    kb.add(types.InlineKeyboardButton(
        text=tr(chat_id, "ℹ️ Помощь", "ℹ️ Help"),
        callback_data="profile_help",
    ))
    kb.add(types.InlineKeyboardButton(
        text=nav_text(chat_id, "menu"),
        callback_data="go_back_to_categories",
    ))
    render_inline_screen(
        chat_id,
        text,
        kb,
        call,
        allow_media_edit=False,
    )


def show_points_info(chat_id: int, call=None) -> None:
    conn_local = get_db_connection()
    cursor_local = conn_local.cursor()
    cursor_local.execute("SELECT points FROM users WHERE chat_id = ?", (chat_id,))
    row = cursor_local.fetchone()
    cursor_local.close()
    conn_local.close()
    points = int(row[0]) if row else 0
    render_inline_screen(
        chat_id,
        tr(
            chat_id,
            f"<b>🎁 Бонусные баллы</b>\n\nУ вас <b>{points}</b> баллов.\n1 балл = 1₺ скидки.",
            f"<b>🎁 Bonus points</b>\n\nYou have <b>{points}</b> points.\n1 point = 1₺ discount.",
        ),
        profile_back_keyboard(chat_id),
        call,
        allow_media_edit=False,
    )


def show_order_history(chat_id: int, call=None) -> None:
    conn_local = get_db_connection()
    cursor_local = conn_local.cursor()
    cursor_local.execute(
        "SELECT order_id, items_json, total, timestamp, promo_code, promo_discount "
        "FROM orders "
        "WHERE chat_id = ? ORDER BY timestamp DESC LIMIT 10",
        (chat_id,),
    )
    rows = cursor_local.fetchall()
    cursor_local.close()
    conn_local.close()

    if not rows:
        text = tr(
            chat_id,
            "<b>📦 Мои заказы</b>\n\nУ вас пока нет заказов.",
            "<b>📦 My orders</b>\n\nYou do not have any orders yet.",
        )
    else:
        blocks = [tr(chat_id, "<b>📦 Последние заказы</b>", "<b>📦 Recent orders</b>")]
        for order_id, items_json, total, timestamp, promo_code, promo_discount in rows:
            try:
                items = json.loads(items_json)
            except (json.JSONDecodeError, TypeError):
                items = []
            grouped = {}
            for item in items:
                flavor = str(item.get("flavor", "—"))
                grouped[flavor] = grouped.get(flavor, 0) + 1
            summary = ", ".join(
                f"{html.escape(flavor)} × {qty}" for flavor, qty in grouped.items()
            ) or "—"
            date = str(timestamp).split("T")[0]
            promo_history_line = (
                f"🎟 {html.escape(str(promo_code))}: "
                f"−{format_money(promo_discount or 0)}₺\n"
                if promo_code else ""
            )
            blocks.append(
                f"<b>#{order_id} · {html.escape(date)}</b>\n"
                f"{summary}\n"
                f"{promo_history_line}"
                f"{tr(chat_id, 'Итого', 'Total')}: {format_money(total)}₺"
            )
        text = "\n\n".join(blocks)

    render_inline_screen(
        chat_id,
        text,
        profile_back_keyboard(chat_id),
        call,
        allow_media_edit=False,
    )


def show_payment_info(chat_id: int, call=None) -> None:
    """Показывает владельцу его закрытые Railway-реквизиты."""
    blocks = ["<b>💳 Мои платёжные реквизиты</b>"]
    for method_key, (label_ru, _label_en, env_name) in PAYMENT_METHODS.items():
        detail = payment_detail(method_key)
        if detail:
            blocks.append(f"<b>{label_ru}</b>\n<pre>{html.escape(detail)}</pre>")
        else:
            blocks.append(
                f"<b>{label_ru}</b>\n"
                f"⚠️ Не настроено: <code>{env_name}</code>"
            )
    blocks.append(
        "Эту страницу и команду /payment может открыть только владелец бота."
    )
    text = "\n\n".join(blocks)
    render_inline_screen(
        chat_id,
        text,
        back_to_main_keyboard(chat_id),
        call,
        allow_media_edit=False,
    )


def show_help_info(chat_id: int, call=None) -> None:
    text = tr(
        chat_id,
        "<b>ℹ️ Как сделать заказ</b>\n\n"
        "1. Выберите модель и вкус.\n"
        "2. Добавьте нужное количество в корзину.\n"
        "3. Откройте корзину и нажмите «Оформить».\n"
        "4. Укажите доставку и подтвердите заказ.\n\n"
        "Если что-то пошло не так, используйте кнопки «Назад» — корзина сохранится.",
        "<b>ℹ️ How to order</b>\n\n"
        "1. Choose a model and flavor.\n"
        "2. Add the required quantity to your cart.\n"
        "3. Open the cart and tap “Checkout”.\n"
        "4. Enter delivery details and confirm the order.\n\n"
        "If something goes wrong, use the Back buttons — your cart will be preserved.",
    )
    render_inline_screen(
        chat_id,
        text,
        profile_back_keyboard(chat_id),
        call,
        allow_media_edit=False,
    )


@bot.callback_query_handler(func=lambda call: call.data == "profile")
def handle_profile(call):
    init_user(call.from_user.id)
    bot.answer_callback_query(call.id)
    show_profile(call.from_user.id, call)


@bot.callback_query_handler(func=lambda call: call.data == "profile_points")
def handle_profile_points(call):
    init_user(call.from_user.id)
    bot.answer_callback_query(call.id)
    show_points_info(call.from_user.id, call)


@bot.callback_query_handler(func=lambda call: call.data == "profile_history")
def handle_profile_history(call):
    init_user(call.from_user.id)
    bot.answer_callback_query(call.id)
    show_order_history(call.from_user.id, call)


@bot.callback_query_handler(func=lambda call: call.data == "profile_payment")
def handle_profile_payment(call):
    if not is_owner(call.from_user.id) or call.message.chat.id != ADMIN_ID:
        return bot.answer_callback_query(
            call.id,
            "У вас нет доступа.",
            show_alert=True,
        )
    init_user(call.from_user.id)
    bot.answer_callback_query(call.id)
    show_payment_info(call.from_user.id, call)


@bot.callback_query_handler(func=lambda call: call.data == "profile_help")
def handle_profile_help(call):
    init_user(call.from_user.id)
    bot.answer_callback_query(call.id)
    show_help_info(call.from_user.id, call)


@bot.callback_query_handler(func=lambda call: call.data == "profile_language")
def handle_profile_language(call):
    chat_id = call.from_user.id
    init_user(chat_id)
    user_data[chat_id]["changing_language"] = True
    user_data[chat_id]["language_return"] = "profile"
    bot.answer_callback_query(call.id)
    render_inline_screen(
        chat_id,
        "<b>🌐 Выберите язык / Choose your language</b>",
        get_inline_language_buttons(
            chat_id,
            include_back=True,
            back_callback="profile",
        ),
        call,
        allow_media_edit=False,
    )


# ------------------------------------------------------------------------
#   30. Пользовательские команды
# ------------------------------------------------------------------------
@ensure_user
@bot.message_handler(commands=['points'])
def cmd_points(message):
    chat_id = message.chat.id
    init_user(chat_id)
    show_points_info(chat_id)


@ensure_user
@bot.message_handler(commands=['history'])
def cmd_history(message):
    chat_id = message.chat.id
    init_user(chat_id)
    show_order_history(chat_id)


# ------------------------------------------------------------------------
#   31. Хендлер /convert — курсы и конвертация суммы TRY
# ------------------------------------------------------------------------
@ensure_user
@bot.message_handler(commands=['convert'])
def cmd_convert(message):
    chat_id = message.chat.id
    init_user(chat_id)
    parts   = message.text.split()
    rates   = fetch_rates()
    rub     = rates.get("RUB", 0)
    usd     = rates.get("USD", 0)
    eur     = rates.get("EUR", 0)
    uah     = rates.get("UAH", 0)

    # Если хотя бы один курс не вытащился — сразу вылетаем
    if 0 in (rub, usd, eur, uah):
        return bot.send_message(
            chat_id,
            tr(
                chat_id,
                "Курсы валют сейчас недоступны, попробуйте позже.",
                "Exchange rates are currently unavailable. Please try again later.",
            ),
            reply_markup=back_to_main_keyboard(chat_id),
        )

    # Просто показать текущие курсы
    if len(parts) == 1:
        text = tr(
            chat_id,
            "📊 Курс лиры сейчас:\n"
            f"1₺ = {rub:.2f} ₽\n"
            f"1₺ = {usd:.2f} $\n"
            f"1₺ = {uah:.2f} ₴\n\n"
            f"1₺ = {eur:.2f} €\n"
            "Для пересчёта напишите: /convert 1300",
            "📊 Current Turkish lira rates:\n"
            f"1₺ = {rub:.2f} ₽\n"
            f"1₺ = {usd:.2f} $\n"
            f"1₺ = {uah:.2f} ₴\n\n"
            f"1₺ = {eur:.2f} €\n"
            "To convert an amount, send: /convert 1300",
        )
        return bot.send_message(chat_id, text, reply_markup=back_to_main_keyboard(chat_id))

    # Если передали сумму — делаем расчёт
    if len(parts) == 2:
        try:
            amount = float(parts[1].replace(",", "."))
        except ValueError:
            return bot.send_message(
                chat_id,
                tr(
                    chat_id,
                    "Формат: /convert 1300 (или другую сумму в лирах)",
                    "Format: /convert 1300 (or another amount in TRY)",
                ),
                reply_markup=back_to_main_keyboard(chat_id),
            )

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
        return bot.send_message(chat_id, text, reply_markup=back_to_main_keyboard(chat_id))

    # Если больше аргументов — просим уточнить
    return bot.send_message(
        chat_id,
        tr(chat_id, "Использование: /convert 1300", "Usage: /convert 1300"),
        reply_markup=back_to_main_keyboard(chat_id),
    )

@ensure_user
@bot.message_handler(commands=['total'])
def cmd_total(message):
    chat_id = message.chat.id
    if not is_owner(message.from_user.id):
        return bot.reply_to(message, "У вас нет доступа.")

    lines = []
    total_pcs = 0

    for cat, cat_data in menu.items():
        cat_lines = []
        cat_total = 0

        for itm in cat_data.get("flavors", []):
            stock = int(itm.get("stock", 0))
            if stock <= 0:
                continue  # ⬅️ скрываем нулевые вкусы

            flavor = itm.get("flavor", "—")
            cat_total += stock
            total_pcs += stock
            cat_lines.append(f"  • {flavor} — {stock} pcs")

        # если в категории есть хоть что-то — показываем
        if cat_lines:
            lines.append(f"<b>{cat}</b>:")
            lines.extend(cat_lines)
            lines.append("")

    # убираем последний перенос
    if lines and lines[-1] == "":
        lines.pop()

    lines.append(f"\n<b>Total:</b> {total_pcs} pcs")

    text = "\n".join(lines) if total_pcs > 0 else "No stock available."
    bot.send_message(chat_id, text, parse_mode="HTML")


@bot.message_handler(commands=['stocknow'])
def cmd_stocknow(message: types.Message):
    if not is_owner(message.from_user.id):
        return bot.reply_to(message, "❌ You do not have access to this command.")
    if message.chat.id != GROUP_CHAT_ID:
        return bot.reply_to(message, "❌ This command is available only in the admin group.")

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
    if not is_owner(message.from_user.id) or message.chat.type != "private":
        bot.send_message(chat_id, "У вас нет доступа к этой команде.")
        return
    init_user(chat_id)
    show_payment_info(chat_id)


@ensure_user
@bot.message_handler(commands=['envcheck'])
def cmd_envcheck(message):
    """Показывает владельцу версию процесса и наличие секретов без их значений."""
    chat_id = message.chat.id
    if not is_owner(message.from_user.id) or message.chat.type != "private":
        bot.send_message(chat_id, "У вас нет доступа к этой команде.")
        return

    status_lines = []
    for _method_key, (_label_ru, _label_en, env_name) in PAYMENT_METHODS.items():
        configured = bool(os.getenv(env_name, "").strip())
        status_lines.append(f"{'✅' if configured else '❌'} <code>{env_name}</code>")

    deployment_id = os.getenv("RAILWAY_DEPLOYMENT_ID", "не определён")
    text = (
        "<b>🔎 Проверка запущенного процесса</b>\n\n"
        f"Версия: <code>{html.escape(BOT_VERSION)}</code>\n"
        f"Deployment: <code>{html.escape(deployment_id)}</code>\n\n"
        + "\n".join(status_lines)
    )
    bot.send_message(chat_id, text)


def reject_payment_callback(call) -> bool:
    """Разрешает платёжные кнопки только владельцу в админ-группе."""
    if is_owner(call.from_user.id) and call.message.chat.id == GROUP_CHAT_ID:
        return False
    bot.answer_callback_query(
        call.id,
        "У вас нет доступа.",
        show_alert=True,
    )
    return True


@bot.callback_query_handler(
    func=lambda call: call.data and call.data.startswith("payment_menu|")
)
def handle_payment_menu(call):
    if reject_payment_callback(call):
        return
    try:
        order_id = int(call.data.split("|", 1)[1])
    except (ValueError, IndexError):
        return bot.answer_callback_query(call.id, "Некорректный заказ.", show_alert=True)
    if not payment_order_target(order_id):
        return bot.answer_callback_query(call.id, "Заказ не найден или отменён.", show_alert=True)
    bot.edit_message_reply_markup(
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        reply_markup=payment_methods_keyboard(order_id),
    )
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(
    func=lambda call: call.data and call.data.startswith("payment_back|")
)
def handle_payment_back(call):
    if reject_payment_callback(call):
        return
    try:
        order_id = int(call.data.split("|", 1)[1])
    except (ValueError, IndexError):
        return bot.answer_callback_query(call.id, "Некорректный заказ.", show_alert=True)
    order_row = payment_order_target(order_id)
    if not order_row:
        return bot.answer_callback_query(call.id, "Заказ не найден или отменён.", show_alert=True)
    bot.edit_message_reply_markup(
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        reply_markup=admin_order_keyboard(order_id, int(order_row[0])),
    )
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(
    func=lambda call: call.data and call.data.startswith("payment_send|")
)
def handle_payment_send(call):
    if reject_payment_callback(call):
        return
    try:
        _, order_id_raw, method_key = call.data.split("|", 2)
        order_id = int(order_id_raw)
    except (ValueError, IndexError):
        return bot.answer_callback_query(call.id, "Некорректные данные.", show_alert=True)

    method = PAYMENT_METHODS.get(method_key)
    if not method:
        return bot.answer_callback_query(call.id, "Неизвестный способ оплаты.", show_alert=True)
    detail = payment_detail(method_key)
    if not detail:
        return bot.answer_callback_query(
            call.id,
            f"Сначала настройте {method[2]} в Railway Variables.",
            show_alert=True,
        )
    order_row = payment_order_target(order_id)
    if not order_row:
        return bot.answer_callback_query(call.id, "Заказ не найден или отменён.", show_alert=True)

    customer_chat_id = int(order_row[0])
    init_user(customer_chat_id)
    method_name = tr(customer_chat_id, method[0], method[1])
    message_text = tr(
        customer_chat_id,
        f"<b>💳 Реквизиты для оплаты заказа №{order_id}</b>\n\n"
        f"Способ: <b>{method_name}</b>\n"
        f"<pre>{html.escape(detail)}</pre>\n"
        "После оплаты отправьте продавцу подтверждение платежа.",
        f"<b>💳 Payment details for order #{order_id}</b>\n\n"
        f"Method: <b>{method_name}</b>\n"
        f"<pre>{html.escape(detail)}</pre>\n"
        "After payment, send the seller your payment confirmation.",
    )
    try:
        bot.send_message(customer_chat_id, message_text)
    except Exception as exc:
        print(f"Payment details delivery failed for order {order_id}: {exc}")
        return bot.answer_callback_query(
            call.id,
            "Не удалось отправить пользователю. Возможно, он заблокировал бота.",
            show_alert=True,
        )

    bot.edit_message_reply_markup(
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        reply_markup=admin_order_keyboard(order_id, customer_chat_id, method_key),
    )
    bot.answer_callback_query(call.id, f"Отправлено: {method[0]}", show_alert=True)

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
        total_stock = sum(
            int(itm.get("stock", 0))
            for cat in menu.values()
            for itm in cat.get("flavors", [])
            if int(itm.get("stock", 0)) > 0
        )

        return (
            "📊 Deliveries today: 0\n"
            f"📦 Stock remaining: {total_stock} pcs"
        )

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

    courier_pay = delivered_qty_exc_free * 200
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
    if not is_owner(message.from_user.id):
        return bot.reply_to(message, "У вас нет доступа.")
    report = compose_sold_report()
    # при ручном вызове шлём в тот же чат, откуда команда
    bot.send_message(message.chat.id, report)


# 1) Определяем отдельный хендлер прямо рядом с /convert, /points и т.д.
@ensure_user
@bot.message_handler(commands=['stats'])
def cmd_stats(message: types.Message):
    user_id = message.from_user.id
    if not is_owner(user_id):
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
    if not is_owner(message.from_user.id) or message.chat.type != "private":
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
@bot.message_handler(commands=['help'])
def cmd_help(message: types.Message):
    if message.chat.id == GROUP_CHAT_ID:
        if not is_owner(message.from_user.id):
            return bot.reply_to(message, "У вас нет доступа.")
        help_text = (
          "/stats      — View store statistics (ADMIN only)\n"
          "/change     — Enter menu-edit mode (ADMIN only)\n"
          "/stock &lt;N&gt;  — Set overall delivered count & clear log\n"
          "/sold       — Today's deliveries report (MSK-based)\n"
          "/total      — Show stock levels for all flavors\n"
          "/help       — This help message"
        )
        bot.send_message(message.chat.id, help_text, parse_mode="HTML")
    else:
        init_user(message.chat.id)
        show_help_info(message.chat.id)


# ------------------------------------------------------------------------
#   35. Универсальный хендлер (всё остальное, включая /change логику)
# ------------------------------------------------------------------------
@ensure_user
@bot.message_handler(content_types=['text', 'location', 'venue', 'contact'])
def universal_handler(message):
    chat_id = message.chat.id
    text = message.text or ""
    if chat_id not in user_data:
        init_user(chat_id)
    data = user_data[chat_id]

    # ─── Режим редактирования меню (/change) ────────────────────────────────────────
    if data.get('edit_phase'):
        if not is_owner(message.from_user.id):
            data['edit_phase'] = None
            user_data[chat_id] = data
            bot.send_message(chat_id, "У вас нет доступа к админскому меню.")
            return

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

            if text == "PROMO CREATE":
                data['edit_phase'] = 'promo_create_code'
                data.pop('promo_create_code', None)
                data.pop('promo_create_limit', None)
                kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
                kb.add("⬅️ Back", "❌ Cancel")
                bot.send_message(
                    chat_id,
                    "Enter the promo code name (2–32 letters, numbers, - or _):",
                    reply_markup=kb,
                )
                user_data[chat_id] = data
                return

            if text == "MESSAGE":
                data['edit_phase'] = 'broadcast_message'
                data.pop('broadcast_source_message_id', None)
                kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
                kb.add("⬅️ Back", "❌ Cancel")
                bot.send_message(
                    chat_id,
                    "Write the message that should be sent to all bot users:",
                    reply_markup=kb,
                )
                user_data[chat_id] = data
                return

            bot.send_message(chat_id, "Choose action:", reply_markup=edit_action_keyboard())
            return

        # Создание промокода: название → общий лимит → фиксированная скидка.
        if phase == 'promo_create_code':
            if text == "⬅️ Back":
                data['edit_phase'] = 'choose_action'
                bot.send_message(chat_id, "Back to editing menu:", reply_markup=edit_action_keyboard())
                user_data[chat_id] = data
                return
            if text == "❌ Cancel":
                data['edit_phase'] = None
                data.pop('promo_create_code', None)
                data.pop('promo_create_limit', None)
                bot.send_message(chat_id, "Editing cancelled.", reply_markup=types.ReplyKeyboardRemove())
                show_main_menu(chat_id)
                user_data[chat_id] = data
                return

            promo_code = normalize_promo_code(text)
            if not is_valid_promo_code(promo_code):
                kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
                kb.add("⬅️ Back", "❌ Cancel")
                bot.send_message(
                    chat_id,
                    "Invalid code. Use 2–32 letters, numbers, - or _:",
                    reply_markup=kb,
                )
                return

            connection = get_db_connection()
            cursor = connection.cursor()
            cursor.execute(
                "SELECT active, used_count, usage_limit FROM promo_codes WHERE code = ?",
                (promo_code,),
            )
            existing = cursor.fetchone()
            cursor.close()
            connection.close()
            if existing and int(existing[0]) and int(existing[1]) < int(existing[2]):
                kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
                kb.add("⬅️ Back", "❌ Cancel")
                bot.send_message(
                    chat_id,
                    "This promo code is already active. Enter another name:",
                    reply_markup=kb,
                )
                return

            data['promo_create_code'] = promo_code
            data['edit_phase'] = 'promo_create_limit'
            kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
            kb.add("⬅️ Back", "❌ Cancel")
            bot.send_message(
                chat_id,
                "How many different users may use this promo code? Enter a positive integer:",
                reply_markup=kb,
            )
            user_data[chat_id] = data
            return

        if phase == 'promo_create_limit':
            if text == "⬅️ Back":
                data['edit_phase'] = 'promo_create_code'
                kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
                kb.add("⬅️ Back", "❌ Cancel")
                bot.send_message(chat_id, "Enter the promo code name:", reply_markup=kb)
                user_data[chat_id] = data
                return
            if text == "❌ Cancel":
                data['edit_phase'] = None
                data.pop('promo_create_code', None)
                data.pop('promo_create_limit', None)
                bot.send_message(chat_id, "Editing cancelled.", reply_markup=types.ReplyKeyboardRemove())
                show_main_menu(chat_id)
                user_data[chat_id] = data
                return
            if not text.isdigit() or int(text) <= 0:
                kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
                kb.add("⬅️ Back", "❌ Cancel")
                bot.send_message(chat_id, "Enter a positive integer, for example 10:", reply_markup=kb)
                return

            data['promo_create_limit'] = int(text)
            data['edit_phase'] = 'promo_create_discount'
            kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
            kb.add("⬅️ Back", "❌ Cancel")
            bot.send_message(
                chat_id,
                "Enter the fixed discount amount in TRY, for example 100:",
                reply_markup=kb,
            )
            user_data[chat_id] = data
            return

        if phase == 'promo_create_discount':
            if text == "⬅️ Back":
                data['edit_phase'] = 'promo_create_limit'
                kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
                kb.add("⬅️ Back", "❌ Cancel")
                bot.send_message(chat_id, "Enter the usage limit:", reply_markup=kb)
                user_data[chat_id] = data
                return
            if text == "❌ Cancel":
                data['edit_phase'] = None
                data.pop('promo_create_code', None)
                data.pop('promo_create_limit', None)
                bot.send_message(chat_id, "Editing cancelled.", reply_markup=types.ReplyKeyboardRemove())
                show_main_menu(chat_id)
                user_data[chat_id] = data
                return
            if not text.isdigit() or int(text) <= 0:
                kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
                kb.add("⬅️ Back", "❌ Cancel")
                bot.send_message(chat_id, "Enter a positive TRY amount, for example 100:", reply_markup=kb)
                return

            promo_code = data.get('promo_create_code')
            usage_limit = int(data.get('promo_create_limit', 0) or 0)
            discount_amount = int(text)
            if not promo_code or usage_limit <= 0:
                data['edit_phase'] = 'promo_create_code'
                bot.send_message(chat_id, "Promo data was lost. Enter the code again.")
                user_data[chat_id] = data
                return

            connection = get_db_connection()
            cursor = connection.cursor()
            try:
                cursor.execute("BEGIN IMMEDIATE")
                cursor.execute(
                    "SELECT promo_id, active, used_count, usage_limit "
                    "FROM promo_codes WHERE code = ?",
                    (promo_code,),
                )
                existing = cursor.fetchone()
                if existing and int(existing[1]) and int(existing[2]) < int(existing[3]):
                    raise PromoCodeError("promo_already_active")
                if existing:
                    # История старой кампании остаётся в promo_redemptions по
                    # старому promo_id; новая кампания получает новый ID.
                    cursor.execute("DELETE FROM promo_codes WHERE promo_id = ?", (existing[0],))
                cursor.execute(
                    "INSERT INTO promo_codes "
                    "(code, discount_amount, usage_limit, used_count, active, created_at) "
                    "VALUES (?, ?, ?, 0, 1, ?)",
                    (
                        promo_code,
                        discount_amount,
                        usage_limit,
                        datetime.datetime.utcnow().isoformat(),
                    ),
                )
                connection.commit()
            except (PromoCodeError, sqlite3.IntegrityError):
                connection.rollback()
                data['edit_phase'] = 'promo_create_code'
                kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
                kb.add("⬅️ Back", "❌ Cancel")
                bot.send_message(
                    chat_id,
                    "This promo code became active already. Enter another name.",
                    reply_markup=kb,
                )
                user_data[chat_id] = data
                return
            finally:
                if connection:
                    try:
                        cursor.close()
                        connection.close()
                    except Exception:
                        pass

            data.pop('promo_create_code', None)
            data.pop('promo_create_limit', None)
            data['edit_phase'] = 'choose_action'
            bot.send_message(
                chat_id,
                f"✅ Promo code {promo_code} created: {discount_amount}₺ discount, "
                f"{usage_limit} different users.",
                reply_markup=edit_action_keyboard(),
            )
            user_data[chat_id] = data
            return

        # 1.1) Текст массовой рассылки
        if phase == 'broadcast_message':
            if text == "⬅️ Back":
                data['edit_phase'] = 'choose_action'
                bot.send_message(chat_id, "Back to editing menu:", reply_markup=edit_action_keyboard())
                user_data[chat_id] = data
                return
            if text == "❌ Cancel":
                data['edit_phase'] = None
                data.pop('broadcast_source_message_id', None)
                bot.send_message(chat_id, "Editing cancelled.", reply_markup=types.ReplyKeyboardRemove())
                bot.send_message(
                    chat_id,
                    t(chat_id, "choose_category"),
                    reply_markup=get_inline_main_menu(chat_id),
                )
                user_data[chat_id] = data
                return
            if message.content_type != 'text' or not text.strip():
                bot.send_message(chat_id, "Please send a non-empty text message.")
                return

            data['broadcast_source_message_id'] = message.message_id
            data['edit_phase'] = 'broadcast_confirm'
            kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
            kb.add("✅ SEND TO ALL", "✏️ EDIT MESSAGE")
            kb.add("⬅️ Back", "❌ Cancel")
            bot.send_message(
                chat_id,
                f"Preview:\n\n{html.escape(text)}\n\nSend this message to every registered user?",
                reply_markup=kb,
            )
            user_data[chat_id] = data
            return

        # 1.2) Подтверждение массовой рассылки
        if phase == 'broadcast_confirm':
            if text in ("⬅️ Back", "✏️ EDIT MESSAGE"):
                data['edit_phase'] = 'broadcast_message'
                data.pop('broadcast_source_message_id', None)
                kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
                kb.add("⬅️ Back", "❌ Cancel")
                bot.send_message(chat_id, "Write the new message:", reply_markup=kb)
                user_data[chat_id] = data
                return
            if text == "❌ Cancel":
                data['edit_phase'] = None
                data.pop('broadcast_source_message_id', None)
                bot.send_message(chat_id, "Editing cancelled.", reply_markup=types.ReplyKeyboardRemove())
                bot.send_message(
                    chat_id,
                    t(chat_id, "choose_category"),
                    reply_markup=get_inline_main_menu(chat_id),
                )
                user_data[chat_id] = data
                return
            if text != "✅ SEND TO ALL":
                bot.send_message(chat_id, "Confirm the broadcast or edit the message.")
                return

            source_message_id = data.get('broadcast_source_message_id')
            if not source_message_id:
                data['edit_phase'] = 'broadcast_message'
                bot.send_message(chat_id, "Message was not saved. Please write it again.")
                user_data[chat_id] = data
                return

            bot.send_message(chat_id, "⏳ Sending broadcast...", reply_markup=types.ReplyKeyboardRemove())
            sent, failed = broadcast_message_to_users(chat_id, source_message_id)
            data.pop('broadcast_source_message_id', None)
            data['edit_phase'] = 'choose_action'
            bot.send_message(
                chat_id,
                f"✅ Broadcast finished. Sent: {sent}. Failed: {failed}.",
                reply_markup=edit_action_keyboard(),
            )
            user_data[chat_id] = data
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
            save_menu_safely()

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
                    save_menu_safely()
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
                save_menu_safely()
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
                save_menu_safely()
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
            save_menu_safely()
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
            save_menu_safely()

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
            save_menu_safely()

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
                save_menu_safely()

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

    # Пользовательский интерфейс работает через inline-кнопки и отдельные
    # state-handlers выше. Этот fallback не дублирует checkout и не может
    # случайно создать второй заказ.
    known_navigation = {
        nav_text(chat_id, "cart"),
        nav_text(chat_id, "address"),
        nav_text(chat_id, "contact"),
        nav_text(chat_id, "points"),
        nav_text(chat_id, "review"),
        nav_text(chat_id, "menu"),
        t(chat_id, "back"),
    }
    if text == nav_text(chat_id, "cart"):
        data.update({
            "wait_for_points": False,
            "wait_for_address": False,
            "wait_for_contact": False,
            "wait_for_comment": False,
            "wait_for_promo": False,
            "pending_discount": 0,
            "pending_points_spent": 0,
        })
        clear_promo_state(data)
        bot.send_message(
            chat_id,
            tr(chat_id, "Возвращаемся в корзину.", "Back to your cart."),
            reply_markup=types.ReplyKeyboardRemove(),
        )
        send_cart(chat_id)
        return

    if text in known_navigation:
        data.update({
            "current_category": None,
            "wait_for_points": False,
            "wait_for_address": False,
            "wait_for_contact": False,
            "wait_for_comment": False,
            "wait_for_promo": False,
            "pending_discount": 0,
            "pending_points_spent": 0,
        })
        clear_promo_state(data)
        bot.send_message(
            chat_id,
            tr(chat_id, "Возвращаемся в меню.", "Back to the menu."),
            reply_markup=types.ReplyKeyboardRemove(),
        )
        show_main_menu(chat_id)
        return

    show_main_menu(chat_id)

@ensure_user
@bot.callback_query_handler(func=lambda call: call.data == "no_points")
def callback_no_points(call):
    chat_id = call.from_user.id
    init_user(chat_id)
    bot.answer_callback_query(call.id)

    data = user_data.get(chat_id, {})
    # выключаем режим ввода баллов
    data["wait_for_points"] = False
    data["pending_discount"] = 0
    data["pending_points_spent"] = 0
    if data.get("promo_code"):
        data["points_before_promo"] = 0

    user_data[chat_id] = data
    show_order_review(chat_id, call)



@bot.callback_query_handler(func=lambda call: call.data and call.data.startswith("cancel_order|"))
def handle_cancel_order(call):
    user_id = call.from_user.id
    if not is_owner(user_id):
        return bot.answer_callback_query(call.id, "Нет доступа", show_alert=True)
    if call.message.chat.id != GROUP_CHAT_ID:
        return bot.answer_callback_query(
            call.id,
            "Кнопка доступна только в админ-группе",
            show_alert=True,
        )

    try:
        order_id = int(call.data.split("|", 2)[1])
    except (ValueError, IndexError):
        return bot.answer_callback_query(call.id, "Data error", show_alert=True)

    conn = None
    menu_snapshot = None
    stock_warnings = []
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("BEGIN IMMEDIATE")
        cursor.execute(
            "SELECT chat_id, items_json, points_spent, points_earned "
            "FROM orders WHERE order_id = ?",
            (order_id,),
        )
        row = cursor.fetchone()
        if not row:
            conn.rollback()
            return bot.answer_callback_query(
                call.id,
                "Заказ уже отменён или не найден",
                show_alert=True,
            )

        user_chat_id, items_json, pts_spent, pts_earned = row
        items = json.loads(items_json or "[]")
        if not isinstance(items, list):
            raise ValueError("items_json is not a list")

        cursor.execute(
            "SELECT promo_id FROM promo_redemptions WHERE order_id = ?",
            (order_id,),
        )
        promo_redemption = cursor.fetchone()
        promo_restored = False
        pts_spent = int(pts_spent or 0)
        pts_earned = int(pts_earned or 0)

        # Снимок позволяет вернуть каталог в исходное состояние при любой ошибке.
        menu_snapshot = json.loads(json.dumps(menu, ensure_ascii=False))
        for item in items:
            if not isinstance(item, dict):
                stock_warnings.append("некорректная позиция")
                continue
            category = item.get("category")
            flavor = item.get("flavor")
            category_data = menu.get(category)
            if not category_data or not isinstance(category_data.get("flavors"), list):
                stock_warnings.append(f"категория {category!r} не найдена")
                continue
            if not flavor:
                stock_warnings.append(f"в позиции {category!r} нет вкуса")
                continue
            try:
                quantity = max(int(item.get("quantity", item.get("qty", 1)) or 1), 1)
            except (TypeError, ValueError):
                quantity = 1

            found_item = next(
                (
                    menu_item
                    for menu_item in category_data["flavors"]
                    if menu_item.get("flavor") == flavor
                ),
                None,
            )
            if found_item is not None:
                found_item["stock"] = int(found_item.get("stock", 0) or 0) + quantity
            else:
                category_data["flavors"].append({
                    "flavor": flavor,
                    "stock": quantity,
                    "emoji": item.get("emoji", ""),
                    "tags": [],
                    "description_ru": "",
                    "description_en": "",
                    "photo_url": "",
                })

        save_menu_safely()

        if pts_spent:
            cursor.execute(
                "UPDATE users SET points = points + ? WHERE chat_id = ?",
                (pts_spent, user_chat_id),
            )
        if pts_earned:
            cursor.execute(
                "UPDATE users SET points = points - ? WHERE chat_id = ?",
                (pts_earned, user_chat_id),
            )

        if promo_redemption:
            promo_id = int(promo_redemption[0])
            cursor.execute(
                "DELETE FROM promo_redemptions WHERE order_id = ?",
                (order_id,),
            )
            cursor.execute(
                "UPDATE promo_codes "
                "SET used_count = CASE WHEN used_count > 0 THEN used_count - 1 ELSE 0 END, "
                "active = 1 WHERE promo_id = ?",
                (promo_id,),
            )
            promo_restored = cursor.rowcount == 1

        cursor.execute("DELETE FROM orders WHERE order_id = ?", (order_id,))
        if cursor.rowcount != 1:
            raise RuntimeError("order deletion did not affect exactly one row")
        conn.commit()
    except Exception as exc:
        if conn is not None:
            try:
                conn.rollback()
            except Exception:
                pass
        if menu_snapshot is not None:
            menu.clear()
            menu.update(menu_snapshot)
            try:
                save_menu_safely()
            except Exception as restore_exc:
                print(
                    f"Cancel order {order_id}: menu rollback failed: {restore_exc}",
                    flush=True,
                )
        print(
            f"Cancel order {order_id} failed: {type(exc).__name__}: {exc}",
            flush=True,
        )
        try:
            bot.answer_callback_query(
                call.id,
                "Не удалось отменить заказ. Ошибка записана в Railway Logs.",
                show_alert=True,
            )
        except Exception:
            pass
        return
    finally:
        if conn is not None:
            conn.close()

    if stock_warnings:
        print(
            f"Cancel order {order_id} stock warnings: {'; '.join(stock_warnings)}",
            flush=True,
        )

    try:
        init_user(user_chat_id)
        user_language = user_data.get(user_chat_id, {}).get("lang")
    except Exception as exc:
        print(f"Cancel order {order_id}: language lookup failed: {exc}", flush=True)
        user_language = "ru"

    if user_language == "en":
        msg = f"Your order #{order_id} has been cancelled."
        if pts_spent:
            msg += f" {pts_spent} spent points were returned."
        if pts_earned:
            msg += f" {pts_earned} earned points were removed."
        if promo_restored:
            msg += " The promo-code use was restored."
    else:
        msg = f"Ваш заказ #{order_id} отменён."
        if pts_spent:
            msg += f" Возвращено {pts_spent} списанных баллов."
        if pts_earned:
            msg += f" Списано {pts_earned} начисленных баллов."
        if promo_restored:
            msg += " Использование промокода восстановлено."

    notification_sent = True
    try:
        bot.send_message(user_chat_id, msg)
    except Exception as exc:
        notification_sent = False
        print(
            f"Cancel order {order_id}: customer notification failed: {exc}",
            flush=True,
        )

    try:
        bot.edit_message_reply_markup(
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            reply_markup=None,
        )
    except Exception as exc:
        print(f"Cancel order {order_id}: keyboard cleanup failed: {exc}", flush=True)

    callback_text = "Заказ отменён"
    if not notification_sent:
        callback_text += "; пользователь не получил уведомление"
    try:
        bot.answer_callback_query(call.id, callback_text, show_alert=not notification_sent)
    except Exception as exc:
        print(f"Cancel order {order_id}: callback answer failed: {exc}", flush=True)


# 1) Обработчик нажатия "Order Delivered"
# 1) When “Order Delivered” is pressed, show currency choices (EN only)

# 1) Заказ доставлен → предложить валюту «внутри» того же сообщения
# 1) Нажали «✅ Order Delivered»
# 1) Нажали «✅ Order Delivered»
# 1) Заказ доставлен → предложить валюту «внутри» того же сообщения
@bot.callback_query_handler(func=lambda call: call.data and call.data.startswith("order_delivered|"))
def handle_order_delivered(call: types.CallbackQuery):

    if not is_owner(call.from_user.id):
        return bot.answer_callback_query(call.id, "Нет доступа", show_alert=True)
    if call.message.chat.id != GROUP_CHAT_ID:
        return bot.answer_callback_query(call.id, "Нажали не в том чате", show_alert=True)

    bot.answer_callback_query(call.id)

    parts = call.data.split("|")
    if len(parts) < 2:
        return bot.answer_callback_query(call.id, "Data error", show_alert=True)

    order_id = int(parts[1])

    # Формируем клавиатуру выбора валют
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

    bot.edit_message_reply_markup(
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        reply_markup=kb
    )


@bot.callback_query_handler(func=lambda call: call.data and call.data.startswith("deliver_currency|"))
def handle_deliver_currency(call: types.CallbackQuery):

    if not is_owner(call.from_user.id):
        return bot.answer_callback_query(call.id, "Нет доступа", show_alert=True)
    if call.message.chat.id != GROUP_CHAT_ID:
        return bot.answer_callback_query(call.id, "Нажали не в том чате", show_alert=True)

    bot.answer_callback_query(call.id)

    _, oid, currency = call.data.split("|", 2)
    order_id = int(oid)

    conn = get_db_connection()
    cur = conn.cursor()

    # Проверяем, не отмечен ли уже заказ
    cur.execute("SELECT 1 FROM delivered_log WHERE order_id = ? LIMIT 1", (order_id,))
    if cur.fetchone():
        cur.close()
        conn.close()
        return bot.answer_callback_query(call.id, "This order has already been marked delivered.", show_alert=True)

    cur.execute("SELECT items_json FROM orders WHERE order_id = ?", (order_id,))
    row = cur.fetchone()

    if not row:
        cur.close()
        conn.close()
        return bot.answer_callback_query(call.id, "Order not found", show_alert=True)

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

    cur.execute("SELECT SUM(count) FROM delivered_counts")
    overall_total = cur.fetchone()[0] or 0

    cur.close()
    conn.close()

    # Обновляем текст и убираем старые статусы
    text = call.message.text
    text = text.replace("🚗 In Delivery", "")
    text = text.replace("❌ Cancelled", "")
    text = text.replace("✅ Delivered", "")

    new_text = (
        f"{text.strip()}\n\n"
        f"<b>Already delivered:</b>\n"
        f"PAYED IN {currency.upper()}: {qty} pcs\n\n"
        f"<b>Total:</b> {overall_total} pcs\n\n"
        f"✅ Delivered"
    )

    # Финально убираем ВСЕ кнопки
    bot.edit_message_text(
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        text=new_text,
        parse_mode="HTML",
        reply_markup=None
    )


# 3) Нажали «⏪ Back»
@bot.callback_query_handler(func=lambda call: call.data and call.data.startswith("back_to_options|"))
def handle_back_to_options(call: types.CallbackQuery):
    if not is_owner(call.from_user.id):
        return bot.answer_callback_query(call.id, "Нет доступа", show_alert=True)
    if call.message.chat.id != GROUP_CHAT_ID:
        return bot.answer_callback_query(call.id, "Нажали не в том чате", show_alert=True)
    # сразу прекращаем крутилку
    bot.answer_callback_query(call.id)
    order_id = int(call.data.split("|", 1)[1])
    order_row = payment_order_target(order_id)
    if not order_row:
        return bot.answer_callback_query(call.id, "Order not found", show_alert=True)
    kb = admin_order_keyboard(order_id, int(order_row[0]))

    bot.edit_message_reply_markup(
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        reply_markup=kb
    )
# 3) «Back» — возвращаем оригинальную клавиатуру (❌ и ✅) без изменения текста
@bot.callback_query_handler(func=lambda call: call.data and call.data.startswith("back_to_group|"))
def handle_back_to_group(call: types.CallbackQuery):
    if not is_owner(call.from_user.id):
        return bot.answer_callback_query(call.id, "Нет доступа", show_alert=True)
    if call.message.chat.id != GROUP_CHAT_ID:
        return bot.answer_callback_query(call.id, "Нажали не в том чате", show_alert=True)
    bot.answer_callback_query(call.id)
    _, oid = call.data.split("|", 1)
    order_id = int(oid)
    order_row = payment_order_target(order_id)
    if not order_row:
        return bot.answer_callback_query(call.id, "Order not found", show_alert=True)
    kb = admin_order_keyboard(order_id, int(order_row[0]))
    bot.edit_message_reply_markup(
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        reply_markup=kb
    )
@bot.callback_query_handler(func=lambda call: call.data and call.data.startswith("courier_on_way|"))
def handle_courier_on_way(call):
    if not is_owner(call.from_user.id):
        return bot.answer_callback_query(call.id, "Нет доступа", show_alert=True)
    if call.message.chat.id != GROUP_CHAT_ID:
        return bot.answer_callback_query(call.id, "Нажали не в том чате", show_alert=True)

    parts = call.data.split("|")

    if len(parts) < 3:
        return bot.answer_callback_query(call.id, "Data error ❌", show_alert=True)

    order_id = int(parts[1])
    user_chat_id = int(parts[2])

    # 1️⃣ Уведомляем клиента
    init_user(user_chat_id)
    bot.send_message(
        user_chat_id,
        tr(
            user_chat_id,
            "🚗 Курьер принял Ваш заказ и уже в пути!",
            "🚗 The courier has accepted your order and is on the way!",
        )
    )

    # 2️⃣ Добавляем статус в текст (но оставляем кнопки)
    if "🚗 In Delivery" not in call.message.text:
        new_text = call.message.text + "\n\n🚗 In Delivery"

        bot.edit_message_text(
            new_text,
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            reply_markup=call.message.reply_markup  # ← ВАЖНО: сохраняем кнопки
        )

    bot.answer_callback_query(call.id, "Marked as In Delivery 🚗")
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
