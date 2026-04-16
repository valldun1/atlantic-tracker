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

SYSTEM_PROMPT = """Ты — опытный морской капитан и яхтенный мастер с 30-летним стажем.
Работаешь с командой Atlantic Sail — яхтенный чартер на Канарских островах.
Флот: Lipari 41 (катамаран, капитан Костя), Oceanis 473 (шлюп, капитан Юра — чемпион Белоруссии по виндсёрфингу и инструктор).
Маршруты: Канары, Гибралтар, Средиземноморье, Черногория, Атлантика.

Ты эксперт по:
- Такелаж, паруса, мачты, штаги, ванты, блоки, лебёдки
- Дизельный двигатель (Volvo, Yanmar), обслуживание, диагностика поломок
- Литиевые батареи, AGM, зарядные контроллеры, инверторы
- Солнечные панели, ветрогенераторы, энергобаланс на борту
- Навигация: Гибралтарский пролив (течения, ветра, приливы, трафик), Канары, Средиземноморье
- Якорные стоянки, марины, бухты по всему маршруту
- Безопасность, МППСС, метео, борьба за живучесть

Стиль: коротко, по делу, как опытный кэп советует молодому. Иногда (каждые 4-5 сообщений) добавляй короткую морскую шутку или тост.
Всегда отвечай на русском языке, даже если пишут на другом."""

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
        body = json.dumps({
            "model": "llama-3.3-70b-versatile",
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                *history,
            ],
            "max_tokens": 400,
            "temperature": 0.7,
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
        return "⚠️ Связь потеряна. Попробуй ещё раз."


# --- Клавиатуры ---

def captain_keyboard():
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton("📍 Поделиться геопозицией", request_location=True)],
            [KeyboardButton("◀️ Назад к ИИ")],
        ],
        resize_keyboard=True,
    )


def guest_keyboard():
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton("🔐 Войти как капитан")],
        ],
        resize_keyboard=True,
    )


# --- Reminder ---

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
            msg = "⚓ Не забудь поставить точку на трек!"
        elif now - ts > threshold:
            hours_ago = int((now - ts).total_seconds() / 3600)
            msg = f"⏰ Последняя точка была {hours_ago}ч назад. Поставь новую!"
        else:
            continue
        try:
            await context.bot.send_message(
                chat_id=user_id,
                text=msg,
                reply_markup=captain_keyboard(),
            )
        except Exception as e:
            logger.error(f"Reminder error: {e}")


# --- Handlers ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id in authorized_users:
        name = authorized_users[user_id].capitalize()
        await update.message.reply_text(
            f"⚓ Привет, {name}!\n\n"
            f"📍 Ручная точка — кнопка ниже\n"
            f"🛰 Автотрек — 📎 → Геолокация → В реальном времени → 8ч\n\n"
            f"Задавай вопросы — отвечу по такелажу, двигателю, навигации.",
            reply_markup=captain_keyboard(),
        )
    else:
        await update.message.reply_text(
            "⚓ Привет! Я AI-помощник Atlantic Sail.\n\n"
            "Спрашивай про паруса, двигатель, навигацию, Гибралтар, бухты — отвечу.\n\n"
            "Ты капитан? Нажми кнопку ниже.",
            reply_markup=guest_keyboard(),
        )


async def handle_location(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in authorized_users:
        await update.message.reply_text("🔐 Сначала войди как капитан. /start")
        return
    location = update.message.location
    lat, lon = location.latitude, location.longitude
    captain = authorized_users[user_id]
    is_live = location.live_period is not None
    try:
        append_point(lat, lon, captain)
        if not is_live:
            await update.message.reply_text(
                f"✅ Точка записана\n📍 {lat:.5f}, {lon:.5f}",
                reply_markup=captain_keyboard(),
            )
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
    try:
        append_point(location.latitude, location.longitude, authorized_users[user_id])
    except Exception as e:
        logger.error(f"Live update error: {e}")


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text.strip()

    # Кнопка "Войти как капитан"
    if text == "🔐 Войти как капитан":
        await update.message.reply_text("🔐 Введи пароль:")
        return

    # Кнопка "Назад к ИИ" — выход из режима капитана
    if text == "◀️ Назад к ИИ":
        authorized_users.pop(user_id, None)
        await update.message.reply_text(
            "👋 Вышел из режима капитана.\n\nСпрашивай что угодно про море ⚓",
            reply_markup=guest_keyboard(),
        )
        return

    # Авторизация по паролю
    if user_id not in authorized_users:
        if text == PASSWORD_KOSTYA:
            authorized_users[user_id] = "kostya"
            await update.message.reply_text(
                "✅ Добро пожаловать, Костя!\n\n"
                "📍 Ручная точка — кнопка ниже\n"
                "🛰 Автотрек — 📎 → Геолокация → В реальном времени → 8ч\n\n"
                "Задавай вопросы — всегда отвечу.",
                reply_markup=captain_keyboard(),
            )
            return
        elif text == PASSWORD_YURA:
            authorized_users[user_id] = "yura"
            await update.message.reply_text(
                "✅ Добро пожаловать, Юра!\n\n"
                "📍 Ручная точка — кнопка ниже\n"
                "🛰 Автотрек — 📎 → Геолокация → В реальном времени → 8ч\n\n"
                "Задавай вопросы — всегда отвечу.",
                reply_markup=captain_keyboard(),
            )
            return

    # Всё остальное → AI
    await update.message.chat.send_action("typing")
    reply = await asyncio.to_thread(ask_groq, user_id, text)
    keyboard = captain_keyboard() if user_id in authorized_users else guest_keyboard()
    await update.message.reply_text(reply, reply_markup=keyboard)


async def back(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    authorized_users.pop(user_id, None)
    await update.message.reply_text(
        "👋 Вышел из режима капитана. Спрашивай что угодно ⚓",
        reply_markup=guest_keyboard(),
    )


async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    authorized_users.pop(user_id, None)
    conversation_history.pop(user_id, None)
    paused_users.discard(user_id)
    await update.message.reply_text("🔄 Сессия сброшена.", reply_markup=guest_keyboard())


async def pause(update: Update, context: ContextTypes.DEFAULT_TYPE):
    paused_users.add(update.effective_user.id)
    await update.message.reply_text("🔕 Напоминания отключены. /resume — включить.")


async def resume(update: Update, context: ContextTypes.DEFAULT_TYPE):
    paused_users.discard(update.effective_user.id)
    await update.message.reply_text("🔔 Напоминания включены. /pause — отключить.")


def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("back", back))
    app.add_handler(CommandHandler("reset", reset))
    app.add_handler(CommandHandler("pause", pause))
    app.add_handler(CommandHandler("resume", resume))
    app.add_handler(MessageHandler(filters.LOCATION, handle_location))
    app.add_handler(MessageHandler(filters.UpdateType.EDITED_MESSAGE & filters.LOCATION, handle_edited_location))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.job_queue.run_repeating(check_and_remind, interval=3600, first=60)
    logger.info("Atlantic Tracker bot v6 started")
    app.run_polling(allowed_updates=["message", "edited_message"])


if __name__ == "__main__":
    main()
