# Dockerfile
# Используем официальный Python 3.11 slim-образ
FROM python:3.11-slim

# Устанавливаем рабочую директорию внутри контейнера
WORKDIR /app

# 1) Копируем файл с зависимостями и ставим их
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 2) Копируем сам скрипт бота и файлы меню/переводов
COPY bot.py .
COPY menu.json .
COPY languages.json .

# 3) Запуск: просто запускаем bot.py
CMD ["python", "bot.py"]
