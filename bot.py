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
BASE_DIR = os.path.dirname(__file__)
MENU_PATH = os.path.join(BASE_DIR, "menu.json")
LANG_PATH = os.path.join(BASE_DIR, "languages.json")
DB_PATH = os.path.join(BASE_DIR, "database.db")

# ------------------------------------------------------------------------
#   3. Функция для получения локального подключения к БД
# ------------------------------------------------------------------------
def get_db_connection():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    return conn
