import logging
import os
import asyncio
import mysql.connector
from datetime import datetime, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, LabeledPrice
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, MessageHandler,
    ContextTypes, filters, PreCheckoutQueryHandler
)
from aiohttp import web

# --- CONFIGURACIÓN --- #
TOKEN = os.getenv("TOKEN")
PROVIDER_TOKEN = os.getenv("PROVIDER_TOKEN", "")
APP_URL = os.getenv("APP_URL")
PORT = int(os.getenv("PORT", "8080"))

DB_HOST = os.getenv("DB_HOST", "localhost")
DB_USER = os.getenv("DB_USER", "root")
DB_PASS = os.getenv("DB_PASS", "")
DB_NAME = os.getenv("DB_NAME", "telegram_bot")

if not TOKEN or not APP_URL:
    raise ValueError("❌ TOKEN o APP_URL no están configurados")

# --- BASE DE DATOS --- #
def get_db():
    return mysql.connector.connect(
        host=DB_HOST,
        user=DB_USER,
        password=DB_PASS,
        database=DB_NAME
    )

# --- CONSTANTES --- #
CHANNELS = {
    'supertvw2': '@Supertvw2',
    'fullvvd': '@fullvvd'
}

FREE_LIMIT_VIDEOS = 3
PREMIUM_ITEM = {
    "title": "Plan Premium",
    "description": "Acceso y reenvíos ilimitados por 30 días.",
    "payload": "premium_plan",
    "currency": "XTR",
    "prices": [LabeledPrice("Premium por 30 días", 1)]
}

# --- MENÚ PRINCIPAL --- #
def get_main_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🎧 Audio Libros", url="https://t.me/+3lDaURwlx-g4NWJk"),
         InlineKeyboardButton("📚 Libro PDF", url="https://t.me/+iJ5D1VLCAW5hYzhk")],
        [InlineKeyboardButton("💬 Chat Pedido", callback_data="chat_pedido"),
         InlineKeyboardButton("🎓 Cursos", callback_data="cursos")],
        [InlineKeyboardButton("📢 Canal", url="https://t.me/hsitotv"),
         InlineKeyboardButton("👥 Grupo", url="https://t.me/udyat_channel")],
        [InlineKeyboardButton("💎 Planes", callback_data="planes"),
         InlineKeyboardButton("🧑 Perfil", callback_data="perfil")],
        [InlineKeyboardButton("ℹ️ Info", callback_data="info"),
         InlineKeyboardButton("❓ Ayuda", callback_data="ayuda")]
    ])

# --- FUNCIONES PREMIUM --- #
def is_premium(user_id):
    db = get_db()
    cursor = db.cursor()
    cursor.execute("SELECT expire_at FROM premium_users WHERE user_id = %s", (user_id,))
    row = cursor.fetchone()
    cursor.close()
    db.close()
    return row and row[0] > datetime.utcnow()

def set_premium(user_id, days=30):
    expire_at = datetime.utcnow() + timedelta(days=days)
    db = get_db()
    cursor = db.cursor()
    cursor.execute("REPLACE INTO premium_users (user_id, expire_at) VALUES (%s, %s)", (user_id, expire_at))
    db.commit()
    cursor.close()
    db.close()

# --- HANDLERS --- #
async def verify(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    not_joined = []
    for name, username in CHANNELS.items():
        try:
            member = await context.bot.get_chat_member(chat_id=username, user_id=user_id)
            if member.status not in ['member', 'administrator', 'creator']:
                not_joined.append(username)
        except:
            not_joined.append(username)

    if not not_joined:
        await query.edit_message_text("✅ Verificación completada. Menú disponible:")
        await query.message.reply_text("📋 Menú principal:", reply_markup=get_main_menu())
    else:
        await query.edit_message_text("❌ Aún no estás suscrito a:\n" + "\n".join(not_joined))

# --- APLICACIÓN --- #
app_telegram = Application.builder().token(TOKEN).build()
app_telegram.add_handler(CallbackQueryHandler(verify, pattern="^verify$"))

web_app = web.Application()

async def main():
    logging.basicConfig(level=logging.INFO)
    await app_telegram.initialize()
    await app_telegram.start()

    runner = web.AppRunner(web_app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()

    logging.info(f"Servidor corriendo en puerto {PORT}")
    try:
        while True:
            await asyncio.sleep(3600)
    except (KeyboardInterrupt, SystemExit):
        logging.info("Deteniendo bot...")
    finally:
        await app_telegram.stop()
        await app_telegram.shutdown()
        await runner.cleanup()

if __name__ == "__main__":
    asyncio.run(main())

