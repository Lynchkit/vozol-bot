# Используем официальный Python-образ
FROM python:3.11-slim

# Рабочая директория внутри контейнера
WORKDIR /app

# Копируем файлы в контейнер
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot2.py .
COPY menu.json .

# Переменные окружения (если хотим задать дефолтные)
# ENV TOKEN=""
# ENV GROUP_CHAT_ID=""
# ENV PERSONAL_CHAT_ID=""

# Команда запуска
CMD ["python", "bot2.py"]
