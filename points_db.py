# points_db.py
import os
import psycopg2
from psycopg2.extras import RealDictCursor

def get_connection():
    """
    Открывает подключение к базе PostgreSQL по переменной окружения DATABASE_URL.
    В Railway обычно DATABASE_URL уже задана.
    """
    DATABASE_URL = os.getenv("DATABASE_URL")
    if not DATABASE_URL:
        raise RuntimeError("Переменная окружения DATABASE_URL не задана!")
    # В случае, если требуется SSL, можно добавить sslmode='require'
    return psycopg2.connect(DATABASE_URL, sslmode="require")

def get_points(chat_id: int) -> int:
    """
    Возвращает текущее количество баллов пользователя chat_id.
    Если пользователь отсутствует в таблице, возвращаем 0.
    """
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT points FROM users WHERE chat_id = %s;", (chat_id,))
    row = cur.fetchone()
    cur.close()
    conn.close()
    return row[0] if row else 0

def set_points(chat_id: int, new_points: int) -> None:
    """
    Устанавливает точное значение points для пользователя chat_id.
    Если запись уже есть, обновляем, иначе вставляем новую.
    """
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO users (chat_id, points)
        VALUES (%s, %s)
        ON CONFLICT (chat_id) DO UPDATE SET points = EXCLUDED.points;
    """, (chat_id, new_points))
    conn.commit()
    cur.close()
    conn.close()

def add_points(chat_id: int, delta: int) -> int:
    """
    Прибавляет delta к полю points пользователя chat_id.
    Если пользователя ещё нет в таблице, создаём запись с точкой = delta.
    Возвращает новое значение points.
    """
    conn = get_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)

    # Если пользователя нет, создаём с points=0
    cur.execute("SELECT points FROM users WHERE chat_id = %s;", (chat_id,))
    row = cur.fetchone()
    if row is None:
        cur.execute(
            "INSERT INTO users (chat_id, points) VALUES (%s, %s) RETURNING points;",
            (chat_id, delta)
        )
        new = cur.fetchone()["points"]
    else:
        cur.execute(
            "UPDATE users SET points = points + %s WHERE chat_id = %s RETURNING points;",
            (delta, chat_id)
        )
        new = cur.fetchone()["points"]

    conn.commit()
    cur.close()
    conn.close()
    return new
