# -----------------------------------------------------------------------------
# 1) Базовый образ c Python 3.11
# -----------------------------------------------------------------------------
FROM python:3.11-slim

# -----------------------------------------------------------------------------
# 2) Будем работать в /usr/src/app (Railway монтирует репозиторий в /app, 
#    поэтому /usr/src/app останется свободным внутри образа)
# -----------------------------------------------------------------------------
WORKDIR /usr/src/app

# -----------------------------------------------------------------------------
# 3) Копируем зависимости и устанавливаем их
# -----------------------------------------------------------------------------
COPY requirements.txt ./ 
RUN pip install --no-cache-dir -r requirements.txt

# -----------------------------------------------------------------------------
# 4) Копируем код бота и шаблоны JSON в /usr/src/app
# -----------------------------------------------------------------------------
COPY bot.py            ./bot.py
COPY menu.json         ./menu.json
COPY languages.json    ./languages.json

# -----------------------------------------------------------------------------
# 5) ENTRYPOINT:
#    — работаем с /data (Railway монтирует туда volume автоматически);
#    — если в /data нет menu.json/languages.json, копируем их из /usr/src/app,
#      иначе создаём пустые файловки;
#    — запускаем бота из /usr/src/app
# -----------------------------------------------------------------------------
ENTRYPOINT [ "sh", "-c", "\
    mkdir -p /data && \
    if [ ! -f /data/menu.json ]; then \
      if [ -f /usr/src/app/menu.json ]; then \
        cp /usr/src/app/menu.json /data/menu.json; \
      else \
        echo '{}' > /data/menu.json; \
      fi; \
    fi && \
    if [ ! -f /data/languages.json ]; then \
      if [ -f /usr/src/app/languages.json ]; then \
        cp /usr/src/app/languages.json /data/languages.json; \
      else \
        echo '{}' > /data/languages.json; \
      fi; \
    fi && \
    python /usr/src/app/bot.py" ]
