FROM python:3.11-slim
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py .
COPY menu.json .
COPY languages.json .

CMD ["python", "bot.py"]
