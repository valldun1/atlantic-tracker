import os
import logging
from datetime import datetime, timezone, timedelta

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
PASSWORD_KOSTYA = os.environ["PASSWORD_KOSTYA"]
PASSWORD_YURA = os.environ["PASSWORD_YURA"]
GOOGLE_CREDENTIALS_JSON = os.environ["GOOGLE_CREDENTIALS_JSON"]
REMINDER_HOURS = int(os.environ.get("REMINDER_HOURS", "3"))

# --- Сессии: user_id → captain name ---
authorized_users: dict[int, str] = {}

# --- Напоминания отключены: user_id ---
paused_users: set[int] = set()

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

def append_point(lat: float, lon: float, captain: str):
    sheet = get_sheet()
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    sheet.append_row([timestamp, lat, lon, captain])

def get_last_timestamps() -> dict[str, datetime]:
    try:
        sheet = get_sheet()
        rows = sheet.get_all_values()[1:]
        last: dict[str, datetime] = {}
        for row in rows:
            if len(row) < 4:
                continue
            captain = row[3].strip()
            ts_str = row[0].strip()
            try:
                ts = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
                if captain not in last or ts > last[captain]:
                    last[captain] = ts
            except ValueError:
                continue
        return last
    except Exception as e:
        logger.error(f"get_last_timestamps error: {e}")
        return {}

# --- Клавиатура ---
def geo_keyboard(paused: bool = False):
    geo_btn = KeyboardButton("📍 Поделиться геопозицией", request_location=True)
    toggle_btn = KeyboardButton("🔔 Включить напоминания" if paused else "🔕 Отключить напоминания")
    return ReplyKeyboardMarkup([[geo_btn], [toggle_btn]], resize_keyboard=True)

# --- Reminder job ---
async def check_and_remind(context):
    if not authorized_users:
        return

    last_ts = get_last_timestamps()
    now = datetime.now(timezone.utc)
    threshold = timedelta(hours=REMINDER_HOURS)
    captain_to_uid = {v: k for k, v in authorized_users.items()}

    for captain, user_id in captain_to_uid.items():
        if user_id in paused_users:
            continue
        ts = last_ts.get(captain)
        if ts is None:
            msg = "⚓ Привет! Не забудь поставить точку на трек."
        elif now - ts > threshold:
            hours_ago = int((now - ts).total_seconds() / 3600)
            msg = f"⏰ Последняя точка была {hours_ago}ч назад. Поставь новую!"
        else:
            continue

        try:
            await context.bot.send_message(
                chat_id=user_id,
                text=msg,
                reply_markup=geo_keyboard(user_id in paused_users),
            )
            logger.info(f"Reminder sent to {captain} (uid={user_id})")
        except Exception as e:
            logger.error(f"Reminder error for {captain}: {e}")

# --- Live location: запись каждого обновления ---
async def handle_location(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    if user_id not in authorized_users:
        await update.message.reply_text("🔐 Сначала введи пароль. Напиши /start")
        return

    location = update.message.location
    lat = location.latitude
    lon = location.longitude
    captain = authorized_users[user_id]
    is_live = location.live_period is not None

    try:
        append_point(lat, lon, captain)
        if not is_live:
            # Ручная точка — подтверждаем
            await update.message.reply_text(
                f"✅ Точка записана\n📍 {lat:.5f}, {lon:.5f}",
                reply_markup=geo_keyboard(user_id in paused_users),
            )
        # Live location — пишем тихо, без ответа каждый раз
        logger.info(f"Point saved: {captain} {lat:.5f} {lon:.5f} live={is_live}")
    except Exception as e:
        logger.error(f"Sheets error: {e}")
        if not is_live:
            await update.message.reply_text(
                "❌ Ошибка записи. Попробуй ещё раз.",
                reply_markup=geo_keyboard(user_id in paused_users),
            )

# --- Live location: обновления (edited_message) ---
async def handle_edited_location(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in authorized_users:
        return

    location = update.edited_message.location
    if not location:
        return

    lat = location.latitude
    lon = location.longitude
    captain = authorized_users[user_id]

    try:
        append_point(lat, lon, captain)
        logger.info(f"Live update: {captain} {lat:.5f} {lon:.5f}")
    except Exception as e:
        logger.error(f"Live update error: {e}")

# --- Handlers ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id in authorized_users:
        captain = authorized_users[user_id]
        await update.message.reply_text(
            f"✅ Привет, {captain}!\n\n"
            f"📍 Ручная точка — кнопка ниже\n"
            f"🛰 Автотрек — 📎 → Геолокация → В реальном времени → 8ч",
            reply_markup=geo_keyboard(user_id in paused_users),
        )
    else:
        await update.message.reply_text("🔐 Введи пароль:")

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text.strip()

    # Кнопки паузы (работают даже без авторизации — просто игнорируем)
    if text == "🔕 Отключить напоминания":
        paused_users.add(user_id)
        await update.message.reply_text(
            "🔕 Напоминания отключены.",
            reply_markup=geo_keyboard(paused=True),
        )
        return
    if text == "🔔 Включить напоминания":
        paused_users.discard(user_id)
        await update.message.reply_text(
            "🔔 Напоминания включены.",
            reply_markup=geo_keyboard(paused=False),
        )
        return

    if user_id in authorized_users:
        await update.message.reply_text(
            "Нажми 📍 или запусти автотрек: 📎 → Геолокация → В реальном времени",
            reply_markup=geo_keyboard(user_id in paused_users),
        )
        return

    if text == PASSWORD_KOSTYA:
        authorized_users[user_id] = "kostya"
        await update.message.reply_text(
            "✅ Добро пожаловать, Костя!\n\n"
            "📍 Ручная точка — кнопка ниже\n"
            "🛰 Автотрек — 📎 → Геолокация → В реальном времени → 8ч",
            reply_markup=geo_keyboard(False),
        )
    elif text == PASSWORD_YURA:
        authorized_users[user_id] = "yura"
        await update.message.reply_text(
            "✅ Добро пожаловать, Юра!\n\n"
            "📍 Ручная точка — кнопка ниже\n"
            "🛰 Автотрек — 📎 → Геолокация → В реальном времени → 8ч",
            reply_markup=geo_keyboard(False),
        )
    else:
        await update.message.reply_text("❌ Неверный пароль. Попробуй ещё раз:")

async def pause(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    paused_users.add(user_id)
    await update.message.reply_text("🔕 Напоминания отключены.", reply_markup=geo_keyboard(True))

async def resume(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    paused_users.discard(user_id)
    await update.message.reply_text("🔔 Напоминания включены.", reply_markup=geo_keyboard(False))

# --- Main ---
def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("pause", pause))
    app.add_handler(CommandHandler("resume", resume))
    app.add_handler(MessageHandler(filters.LOCATION, handle_location))
    app.add_handler(MessageHandler(filters.UpdateType.EDITED_MESSAGE & filters.LOCATION, handle_edited_location))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    # Проверка каждый час
    app.job_queue.run_repeating(check_and_remind, interval=3600, first=60)

    logger.info("Atlantic Tracker bot v4 started")
    app.run_polling(allowed_updates=["message", "edited_message"])

if __name__ == "__main__":
    main()
