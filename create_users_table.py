import os
import psycopg2

if os.getenv("RAILWAY_ENV") == "production":
    # Приватный endpoint (Railway)
    DATABASE_URL = "postgresql://postgres:gCoBCrIecBLIoooUcoNoeHXTgQVKPCVh@postgres.railway.internal:5432/railway"
else:
    # Локальная разработка: публичный endpoint c sslmode
    DATABASE_URL = (
        "postgresql://postgres:gCoBCrIecBLIoooUcoNoeHXTgQVKPCVh"
        "@ballast.proxy.rlwy.net:26514/railway?sslmode=require"
    )

ddl = """
CREATE TABLE IF NOT EXISTS users (
    chat_id      BIGINT PRIMARY KEY,
    points       INTEGER NOT NULL DEFAULT 0,
    referral_code TEXT UNIQUE,
    referred_by  BIGINT
);
"""

def create_table():
    conn = psycopg2.connect(DATABASE_URL)
    cur = conn.cursor()
    cur.execute(ddl)
    conn.commit()
    cur.close()
    conn.close()
    print("таблица users создана или уже существует")

if __name__ == "__main__":
    create_table()
