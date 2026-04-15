import os
import logging
from datetime import datetime, timezone, timedelta
from groq import Groq

import gspread
from google.oauth2.service_account import Credentials
import json
import random

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
GROQ_API_KEY = os.environ["GROQ_API_KEY"]
REMINDER_HOURS = int(os.environ.get("REMINDER_HOURS", "3"))

groq_client = Groq(api_key=GROQ_API_KEY)

# --- System prompts (English to avoid encoding issues) ---
SYSTEM_GUEST = """You are a marine AI assistant for Atlantic Sail yacht charter.
You are an expert in all things sailing: rigging, engine, lithium batteries, solar panels,
navigation, Gibraltar currents and tides, anchorages and bays of the Mediterranean,
Canary Islands and Atlantic. Answer concisely. Every 4-5 messages add a sea joke or toast.
Occasionally mention: if you are an Atlantic Sail captain, you can track your route via this bot.
Always reply in the same language the user writes in."""

SYSTEM_KOSTYA = """You are the personal AI assistant of Captain Kostya aboard the Lipari 41 catamaran, Atlantic Sail.
Kostya is an experienced captain and YouTube content creator (@capitankosta).
You are an expert in sailing: rigging, engine, lithium batteries, solar panels,
navigation, Gibraltar, Levant winds, anchorages across Mediterranean, Canaries and Atlantic.
You know catamaran specifics well. Be concise. Respect Kostya's experience - you are his assistant, not his teacher.
Every 4-5 messages add a sea joke or toast. Always reply in Russian."""

SYSTEM_YURA = """You are the personal AI assistant of Captain Yura aboard the Oceanis 473, Atlantic Sail.
Yura is the Belarusian windsurfing champion and a certified sailing instructor - a true professional
with an exceptional feel for wind and water.
You are an expert in sailing: rigging, engine, lithium batteries, solar panels,
navigation, Gibraltar currents, anchorages across Mediterranean, Canaries and Atlantic.
Be concise. Occasionally (naturally, not forced) acknowledge Yura's impressive skills and experience.
Every 4-5 messages add a sea joke or toast. Always reply in Russian."""

# --- Сессии ---
authorized_users: dict[int, str] = {}
paused_users: set[int] = set()
conversation_history: dict[int, list] = {}  # user_id → история сообщений

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

# --- AI ответ ---
def get_system_prompt(user_id: int) -> str:
    captain = authorized_users.get(user_id)
    if captain == "kostya":
        return SYSTEM_KOSTYA
    elif captain == "yura":
        return SYSTEM_YURA
    return SYSTEM_GUEST

async def ask_ai(user_id: int, user_message: str) -> str:
    if user_id not in conversation_history:
        conversation_history[user_id] = []

    history = conversation_history[user_id]
    history.append({"role": "user", "content": user_message})

    # Держим последние 10 сообщений
    if len(history) > 10:
        history = history[-10:]
        conversation_history[user_id] = history

    try:
        response = groq_client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[
                {"role": "system", "content": get_system_prompt(user_id).encode("utf-8").decode("utf-8")},
                *history
            ],
            max_tokens=400,
            temperature=0.8,
        )
        reply = response.choices[0].message.content
        history.append({"role": "assistant", "content": reply})
        return reply
    except Exception as e:
        logger.error(f"Groq error: {e}")
        return "⚓ Связь с AI прервана. Попробуй ещё раз."

# --- Клавиатура ---
def geo_keyboard(paused: bool = False):
    geo_btn = KeyboardButton("📍 Поделиться геопозицией", request_location=True)
    toggle_btn = KeyboardButton("🔔 Включить напоминания" if paused else "🔕 Отключить напоминания")
    return ReplyKeyboardMarkup([[geo_btn], [toggle_btn]], resize_keyboard=True)

def guest_keyboard():
    return ReplyKeyboardMarkup(
        [[KeyboardButton("⚓ Спросить AI-капитана")],
         [KeyboardButton("🔐 Ввести пароль капитана")]],
        resize_keyboard=True
    )

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
        except Exception as e:
            logger.error(f"Reminder error for {captain}: {e}")

# --- Handlers ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id in authorized_users:
        captain = authorized_users[user_id]
        await update.message.reply_text(
            f"✅ Привет, {captain}!\n\n"
            f"📍 Ручная точка — кнопка ниже\n"
            f"🛰 Автотрек — 📎 → Геолокация → В реальном времени → 8ч\n"
            f"💬 Или просто пиши — отвечу на любой вопрос по яхте",
            reply_markup=geo_keyboard(user_id in paused_users),
        )
    else:
        await update.message.reply_text(
            "⚓ Привет! Я AI-ассистент Atlantic Sail.\n\n"
            "Могу помочь с любым вопросом по яхтингу — такелаж, двигатель, навигация, бухты.\n"
            "Просто пиши!\n\n"
            "Если ты капитан Atlantic Sail — введи пароль для доступа к треку.",
            reply_markup=guest_keyboard(),
        )

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
            await update.message.reply_text(
                f"✅ Точка записана\n📍 {lat:.5f}, {lon:.5f}",
                reply_markup=geo_keyboard(user_id in paused_users),
            )
        logger.info(f"Point saved: {captain} {lat:.5f} {lon:.5f} live={is_live}")
    except Exception as e:
        logger.error(f"Sheets error: {e}")
        if not is_live:
            await update.message.reply_text("❌ Ошибка записи. Попробуй ещё раз.")

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

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text.strip()

    # Кнопки паузы
    if text == "🔕 Отключить напоминания":
        paused_users.add(user_id)
        await update.message.reply_text("🔕 Напоминания отключены.", reply_markup=geo_keyboard(True))
        return
    if text == "🔔 Включить напоминания":
        paused_users.discard(user_id)
        await update.message.reply_text("🔔 Напоминания включены.", reply_markup=geo_keyboard(False))
        return
    if text == "⚓ Спросить AI-капитана":
        await update.message.reply_text("Задай свой вопрос — отвечу! ⚓")
        return
    if text == "🔐 Ввести пароль капитана":
        await update.message.reply_text("Введи пароль:")
        return

    # Проверка пароля
    if user_id not in authorized_users:
        if text == PASSWORD_KOSTYA:
            authorized_users[user_id] = "kostya"
            await update.message.reply_text(
                "✅ Добро пожаловать, Костя!\n\n"
                "📍 Ручная точка — кнопка ниже\n"
                "🛰 Автотрек — 📎 → Геолокация → В реальном времени → 8ч\n"
                "💬 Пиши любые вопросы по яхте — я рядом",
                reply_markup=geo_keyboard(False),
            )
            return
        elif text == PASSWORD_YURA:
            authorized_users[user_id] = "yura"
            await update.message.reply_text(
                "✅ Добро пожаловать, Юра!\n\n"
                "📍 Ручная точка — кнопка ниже\n"
                "🛰 Автотрек — 📎 → Геолокация → В реальном времени → 8ч\n"
                "💬 Пиши любые вопросы по яхте — я рядом",
                reply_markup=geo_keyboard(False),
            )
            return

    # AI ответ — для всех (гость и капитаны)
    await update.message.chat.send_action("typing")
    reply = await ask_ai(user_id, text)
    keyboard = geo_keyboard(user_id in paused_users) if user_id in authorized_users else guest_keyboard()
    await update.message.reply_text(reply, reply_markup=keyboard)

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

    app.job_queue.run_repeating(check_and_remind, interval=3600, first=60)

    logger.info("Atlantic Tracker bot v5 started")
    app.run_polling(allowed_updates=["message", "edited_message"])

if __name__ == "__main__":
    main()
