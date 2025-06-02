# Dockerfile
FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py .
COPY menu.json .
COPY languages.json .
# database.db создаётся автоматически при первом запуске

CMD ["python", "bot.py"]
