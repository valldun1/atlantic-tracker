import os
import logging
import json
import requests
import asyncio
from datetime import datetime, timezone, timedelta

import gspread
from google.oauth2.service_account import Credentials

from telegram import Update, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
PASSWORD_KOSTYA = os.environ["PASSWORD_KOSTYA"]
PASSWORD_YURA = os.environ["PASSWORD_YURA"]
GOOGLE_CREDENTIALS_JSON = os.environ["GOOGLE_CREDENTIALS_JSON"]
GROQ_API_KEY = os.environ["GROQ_API_KEY"]
REMINDER_HOURS = int(os.environ.get("REMINDER_HOURS", "3"))

SYSTEM_PROMPT = (
    "You are a marine AI assistant for Atlantic Sail yacht charter. "
    "Captains: Kostya on Lipari 41 catamaran, Yura on Oceanis 473 - Yura is Belarusian windsurfing champion and sailing instructor. "
    "You are expert in: rigging, engine, lithium batteries, solar panels, navigation, "
    "Gibraltar currents, anchorages in Mediterranean, Canaries and Atlantic. "
    "Be concise. Every 4-5 messages add a sea joke or toast. "
    "Always reply in Russian, regardless of the language the user writes in."
)

authorized_users: dict = {}
paused_users: set = set()
conversation_history: dict = {}


def get_sheet():
    creds_dict = json.loads(GOOGLE_CREDENTIALS_JSON)
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    client = gspread.authorize(creds)
    return client.open("Atlantic Tracker").worksheet("track")


def append_point(lat, lon, captain):
    sheet = get_sheet()
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    sheet.append_row([timestamp, lat, lon, captain])


def get_last_timestamps():
    try:
        sheet = get_sheet()
        rows = sheet.get_all_values()[1:]
        last = {}
        for row in rows:
            if len(row) < 4:
                continue
            captain = row[3].strip()
            try:
                ts = datetime.strptime(row[0].strip(), "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
                if captain not in last or ts > last[captain]:
                    last[captain] = ts
            except ValueError:
                continue
        return last
    except Exception as e:
        logger.error(f"get_last_timestamps error: {e}")
        return {}


def ask_groq(user_id, user_message):
    if user_id not in conversation_history:
        conversation_history[user_id] = []
    history = conversation_history[user_id]
    history.append({"role": "user", "content": user_message})
    if len(history) > 10:
        history = history[-10:]
        conversation_history[user_id] = history

    try:
        import json as _json
        body = _json.dumps({
            "model": "llama-3.3-70b-versatile",
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                *history,
            ],
            "max_tokens": 400,
            "temperature": 0.8,
        }, ensure_ascii=False).encode("utf-8")
        resp = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {GROQ_API_KEY}",
                "Content-Type": "application/json; charset=utf-8",
            },
            data=body,
            timeout=30,
        )
        resp.raise_for_status()
        reply = resp.json()["choices"][0]["message"]["content"]
        history.append({"role": "assistant", "content": reply})
        return reply
    except Exception as e:
        logger.error(f"Groq error: {e}")
        return "Connection lost. Try again."


def captain_keyboard():
    return ReplyKeyboardMarkup(
        [[KeyboardButton("📍 Share location", request_location=True)]],
        resize_keyboard=True,
    )


def guest_keyboard():
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton("⚓ Ask AI captain")],
            [KeyboardButton("🔐 Enter captain password")],
        ],
        resize_keyboard=True,
    )


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
            msg = "Reminder: don't forget to drop a track point!"
        elif now - ts > threshold:
            hours_ago = int((now - ts).total_seconds() / 3600)
            msg = f"Last point was {hours_ago}h ago. Drop a new one!"
        else:
            continue
        try:
            await context.bot.send_message(chat_id=user_id, text=msg, reply_markup=captain_keyboard())
        except Exception as e:
            logger.error(f"Reminder error: {e}")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id in authorized_users:
        name = authorized_users[user_id]
        await update.message.reply_text(
            f"Welcome back, {name}!\n"
            f"📍 Manual point — button below\n"
            f"🛰 Auto-track — 📎 → Location → Live → 8h\n"
            f"💬 Ask me anything about sailing",
            reply_markup=captain_keyboard(),
        )
    else:
        await update.message.reply_text(
            "⚓ Atlantic Sail AI assistant.\n"
            "Ask me anything about sailing!\n\n"
            "Atlantic Sail captain? Enter your password.",
            reply_markup=guest_keyboard(),
        )


async def handle_location(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in authorized_users:
        await update.message.reply_text("Enter password first. /start")
        return
    location = update.message.location
    lat, lon = location.latitude, location.longitude
    captain = authorized_users[user_id]
    is_live = location.live_period is not None
    try:
        append_point(lat, lon, captain)
        if not is_live:
            await update.message.reply_text(
                f"Point saved\n📍 {lat:.5f}, {lon:.5f}",
                reply_markup=captain_keyboard(),
            )
    except Exception as e:
        logger.error(f"Sheets error: {e}")
        if not is_live:
            await update.message.reply_text("Error saving point. Try again.")


async def handle_edited_location(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in authorized_users:
        return
    location = update.edited_message.location
    if not location:
        return
    try:
        append_point(location.latitude, location.longitude, authorized_users[user_id])
    except Exception as e:
        logger.error(f"Live update error: {e}")


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text.strip()

    if text == "⚓ Ask AI captain":
        await update.message.reply_text("Ask your question! ⚓")
        return
    if text == "🔐 Enter captain password":
        await update.message.reply_text("Enter your password:")
        return

    if user_id not in authorized_users:
        if text == PASSWORD_KOSTYA:
            authorized_users[user_id] = "kostya"
            await update.message.reply_text(
                "Welcome, Kostya!\n📍 Button below for track\n🛰 Live: 📎 → Location → Live → 8h\n💬 Ask me anything",
                reply_markup=captain_keyboard(),
            )
            return
        elif text == PASSWORD_YURA:
            authorized_users[user_id] = "yura"
            await update.message.reply_text(
                "Welcome, Yura!\n📍 Button below for track\n🛰 Live: 📎 → Location → Live → 8h\n💬 Ask me anything",
                reply_markup=captain_keyboard(),
            )
            return

    await update.message.chat.send_action("typing")
    reply = await asyncio.to_thread(ask_groq, user_id, text)
    keyboard = captain_keyboard() if user_id in authorized_users else guest_keyboard()
    await update.message.reply_text(reply, reply_markup=keyboard)


async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    authorized_users.pop(user_id, None)
    conversation_history.pop(user_id, None)
    paused_users.discard(user_id)
    await update.message.reply_text(
        "Session reset. You are now a guest.",
        reply_markup=guest_keyboard(),
    )

async def pause(update: Update, context: ContextTypes.DEFAULT_TYPE):

    paused_users.add(update.effective_user.id)
    await update.message.reply_text("Reminders paused. /resume to enable.")


async def resume(update: Update, context: ContextTypes.DEFAULT_TYPE):
    paused_users.discard(update.effective_user.id)
    await update.message.reply_text("Reminders enabled. /pause to disable.")


def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("reset", reset))
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
