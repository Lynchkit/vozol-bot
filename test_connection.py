import psycopg2

# вставьте ровно вашу строку из Railway (проверьте, чтобы не было лишних пробелов или переносов)
DATABASE_URL = "postgresql://postgres:uaNtsbYAUgwBfJPzjUfZkfTDwdCFjnc@interchange.proxy.rlwy.net:44827/railway?sslmode=require"


try:
    conn = psycopg2.connect(DATABASE_URL)
    conn.close()
    print("OK: подключение прошло успешно")
except Exception as e:
    print("Ошибка при подключении:", e)
