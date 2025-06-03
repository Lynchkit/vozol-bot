import os
import psycopg2


def get_connection():
    """
    Открывает соединение с базой данных PostgreSQL по переменной окружения DATABASE_URL.
    В Railway обычно доступна именно эта переменная.
    """
    DATABASE_URL = os.getenv("DATABASE_URL")
    if not DATABASE_URL:
        raise RuntimeError("Переменная окружения DATABASE_URL не задана!")
    # Включаем sslmode=require, чтобы корректно подключаться в продакшн-среде Railway
    return psycopg2.connect(DATABASE_URL, sslmode="require")


def get_points(chat_id: int) -> int:
    """
    Возвращает текущее количество баллов у пользователя (chat_id).
    Если пользователя нет в таблице, возвращает 0.
    """
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT points FROM users WHERE chat_id = %s;", (chat_id,))
    row = cur.fetchone()
    cur.close()
    conn.close()
    return row[0] if row else 0


def set_points(chat_id: int, points: int) -> None:
    """
    Устанавливает точное количество баллов у пользователя (chat_id).
    """
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("UPDATE users SET points = %s WHERE chat_id = %s;", (points, chat_id))
    conn.commit()
    cur.close()
    conn.close()


def add_points(chat_id: int, delta: int) -> None:
    """
    Прибавляет delta баллов к существующим (может быть отрицательным значением).
    Если пользователя нет, ничего не делает.
    """
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("UPDATE users SET points = points + %s WHERE chat_id = %s;", (delta, chat_id))
    conn.commit()
    cur.close()
    conn.close()
