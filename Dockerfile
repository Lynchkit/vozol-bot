# базовый образ
FROM python:3.11-slim

# рабочая директория
WORKDIR /app

# 1) копируем зависимости и устанавливаем
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 2) копируем весь код бота
COPY . .

# 3) запускаем бота
CMD ["python", "bot.py"]
