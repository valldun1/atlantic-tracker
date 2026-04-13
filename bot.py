import os
import logging
from datetime import datetime, timezone

import gspread
from google.oauth2.service_account import Credentials
import json

from telegram import Update, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
)

# --- Logging ---
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- ENV ---
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
PASSWORD = os.environ["PASSWORD"]
GOOGLE_CREDENTIALS_JSON = os.environ["GOOGLE_CREDENTIALS_JSON"]

# --- Авторизованные сессии (в памяти) ---
authorized_users: set[int] = set()

# --- Google Sheets ---
def get_sheet():
    creds_dict = json.loads(GOOGLE_CREDENTIALS_JSON)
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    client = gspread.authorize(creds)
    spreadsheet = client.open("Atlantic Tracker")
    return spreadsheet.worksheet("track")

def append_point(lat: float, lon: float):
    sheet = get_sheet()
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    sheet.append_row([timestamp, lat, lon])

# --- Клавиатура ---
def geo_keyboard():
    button = KeyboardButton("📍 Поделиться геопозицией", request_location=True)
    return ReplyKeyboardMarkup([[button]], resize_keyboard=True)

# --- Handlers ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id in authorized_users:
        await update.message.reply_text(
            "✅ Ты уже авторизован. Жми кнопку чтобы записать точку.",
            reply_markup=geo_keyboard(),
        )
    else:
        await update.message.reply_text("🔐 Введи пароль:")

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text.strip()

    if user_id in authorized_users:
        # Уже авторизован — игнорируем текст
        await update.message.reply_text(
            "Нажми кнопку 📍 чтобы записать геопозицию.",
            reply_markup=geo_keyboard(),
        )
        return

    # Проверка пароля
    if text == PASSWORD:
        authorized_users.add(user_id)
        await update.message.reply_text(
            "✅ Доступ открыт. Нажми кнопку чтобы записать точку.",
            reply_markup=geo_keyboard(),
        )
    else:
        await update.message.reply_text("❌ Неверный пароль. Попробуй ещё раз:")

async def handle_location(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    if user_id not in authorized_users:
        await update.message.reply_text("🔐 Сначала введи пароль. Напиши /start")
        return

    location = update.message.location
    lat = location.latitude
    lon = location.longitude

    try:
        append_point(lat, lon)
        await update.message.reply_text(
            f"✅ Точка записана\n📍 {lat:.5f}, {lon:.5f}",
            reply_markup=geo_keyboard(),
        )
    except Exception as e:
        logger.error(f"Sheets error: {e}")
        await update.message.reply_text(
            "❌ Ошибка записи в таблицу. Попробуй ещё раз.",
            reply_markup=geo_keyboard(),
        )

# --- Main ---
def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.LOCATION, handle_location))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    logger.info("Atlantic Tracker bot started")
    app.run_polling()

if __name__ == "__main__":
    main()
