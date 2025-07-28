FROM python:3.11-slim

WORKDIR /app

# 1) копируем зависимости и ставим
COPY vozol_bot_windows_with_requirements/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 2) копируем весь код бота
COPY vozol_bot_windows_with_requirements .

# 3) запускаем бота
CMD ["python", "bot.py"]
