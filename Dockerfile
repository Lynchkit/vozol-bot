# Используем официальный Python 3.11 slim-образ
FROM python:3.11-slim

# Устанавливаем рабочую директорию внутри контейнера
WORKDIR /app

# 1) Копируем файл с зависимостями и ставим их
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 2) Копируем сам скрипт бота, menu.json и languages.json
COPY bot.py .
COPY menu.json .
COPY languages.json .

# 3) Запуск бота
CMD ["python", "bot.py"]
