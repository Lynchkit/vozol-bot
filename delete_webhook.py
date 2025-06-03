import os
from telebot import TeleBot

TOKEN = os.getenv("TOKEN")
if not TOKEN:
    raise RuntimeError("Переменная окружения TOKEN не задана!")

bot = TeleBot(TOKEN)
bot.delete_webhook()
print("Webhook удалён, теперь можно запускать polling()")
