# points_db.py
import sqlite3
import os

# Путь к той же самой БД, что и в bot.py
DB_PATH = os.path.join(os.path.dirname(__file__), "database.db")

def get_connection():
    """
    Возвращает соединение с SQLite-базой.
    """
    return sqlite3.connect(DB_PATH, check_same_thread=False)

def get_points(user_id: int) -> int:
    """
    Возвращает текущее число бонусных баллов (points) для пользователя с chat_id = user_id.
    Если пользователя нет в таблице users, вернёт 0.
    """
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT points FROM users WHERE chat_id = ?", (user_id,))
    row = cur.fetchone()
    conn.close()
    return row[0] if row else 0

def set_points(user_id: int, new_value: int) -> None:
    """
    Устанавливает точное количество баллов для пользователя (chat_id = user_id) в значение new_value.
    Если пользователя ещё нет в таблице users, создаёт запись с этими баллами.
    """
    conn = get_connection()
    cur = conn.cursor()
    # Если записи нет — создать её
    cur.execute("INSERT OR IGNORE INTO users (chat_id, points) VALUES (?, ?)", (user_id, new_value))
    # Обновить количество баллов
    cur.execute("UPDATE users SET points = ? WHERE chat_id = ?", (new_value, user_id))
    conn.commit()
    conn.close()

def add_points(user_id: int, delta: int) -> None:
    """
    Прибавляет к существующим баллам пользователя delta (может быть отрицательным или положительным).
    Если пользователя нет в таблице, создаёт его с initial points = delta.
    """
    conn = get_connection()
    cur = conn.cursor()
    # Если пользователя ещё нет — вставить его с нулём (или с delta, ниже поправим)
    cur.execute("INSERT OR IGNORE INTO users (chat_id, points) VALUES (?, 0)", (user_id,))
    # Прибавить delta
    cur.execute("UPDATE users SET points = points + ? WHERE chat_id = ?", (delta, user_id))
    conn.commit()
    conn.close()
