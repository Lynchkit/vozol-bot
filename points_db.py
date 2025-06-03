import os
import psycopg2

if os.getenv("RAILWAY_ENV") == "production":
    # Приватный endpoint (только внутри Railway):
    DATABASE_URL = "postgresql://postgres:gCoBCrIecBLIoooUcoNoeHXTgQVKPCVh@postgres.railway.internal:5432/railway"
else:
    # Локальная разработка: публичный endpoint с обязательным ?sslmode=require
    DATABASE_URL = (
        "postgresql://postgres:gCoBCrIecBLIoooUcoNoeHXTgQVKPCVh"
        "@ballast.proxy.rlwy.net:26514/railway?sslmode=require"
    )

def get_points(user_id):
    print("DEBUG: using DATABASE_URL =", DATABASE_URL)
    conn = psycopg2.connect(DATABASE_URL)
    cur = conn.cursor()
    cur.execute("SELECT points FROM users WHERE chat_id = %s", (user_id,))
    res = cur.fetchone()
    cur.close()
    conn.close()
    return res[0] if res else 0

def set_points(user_id, points):
    conn = psycopg2.connect(DATABASE_URL)
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO users (chat_id, points)
        VALUES (%s, %s)
        ON CONFLICT (chat_id) DO UPDATE SET points = EXCLUDED.points
    """, (user_id, points))
    conn.commit()
    cur.close()
    conn.close()

def add_points(user_id, delta):
    current = get_points(user_id)
    set_points(user_id, current + delta)
