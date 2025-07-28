FROM python:3.11-slim

# 1) задаём рабочую папку
WORKDIR /app

# 2) копируем всё из текущей папки в /app
COPY . /app

# 3) устанавливаем зависимости
RUN pip install --no-cache-dir -r /app/requirements.txt

# 4) запускаем бота
CMD ["python", "/app/bot.py"]
