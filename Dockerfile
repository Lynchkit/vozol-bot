# 1) базовый образ
FROM python:3.11-slim

# 2) сразу переходим в папку с ботом
WORKDIR /app/vozol_bot_windows_with_requirements

# 3) копируем и устанавливаем зависимости
COPY vozol_bot_windows_with_requirements/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 4) копируем все файлы бота в текущую папку
COPY vozol_bot_windows_with_requirements .

# 5) запускаем бот
CMD ["python", "bot.py"]
