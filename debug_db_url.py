import psycopg2

# Вставьте СЮДА ровно то, что скопировали из Railway, включая ?sslmode=require, если он был в конце.
DATABASE_URL = "postgresql://postgres:uaNtsbYAUgwBfJPzjUfZkfTDwdCFjnc@interchange.proxy.rlwy.net:44827/railway?sslmode=require"

# Выведем, что получилось
print("DEBUG: repr =", repr(DATABASE_URL))
print("DEBUG: length =", len(DATABASE_URL))

# Попытаемся подключиться
try:
    conn = psycopg2.connect(DATABASE_URL)
    conn.close()
    print("OK: подключение прошло успешно")
except Exception as e:
    print("Ошибка при подключении:", e)
