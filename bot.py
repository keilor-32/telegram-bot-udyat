
import logging
import json
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

def get_db():
    return mysql.connector.connect(
        host=DB_HOST,
        user=DB_USER,
        password=DB_PASS,
        database=DB_NAME
    )

if not TOKEN or not APP_URL:
    raise ValueError("❌ TOKEN o APP_URL no están configurados")

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
        [InlineKeyboardButton("🎧 Audio Libros", callback_data="audio_libros"),
         InlineKeyboardButton("📚 Libro PDF", callback_data="libro_pdf")],
        [InlineKeyboardButton("💬 Chat Pedido", callback_data="chat_pedido"),
         InlineKeyboardButton("🎓 Cursos", callback_data="cursos")],
        [InlineKeyboardButton("📢 Canal", url="https://t.me/hsitotv"),
         InlineKeyboardButton("👥 Grupo", url="https://t.me/udyat_channel")],
        [InlineKeyboardButton("💎 Planes", callback_data="planes"),
         InlineKeyboardButton("🧑 Perfil", callback_data="perfil")],
        [InlineKeyboardButton("ℹ️ Info", callback_data="info"),
         InlineKeyboardButton("❓ Ayuda", callback_data="ayuda")]
    ])

# --- UTILIDADES PREMIUM --- #
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

def save_video(pkg_id, photo_id, caption, video_id):
    db = get_db()
    cursor = db.cursor()
    cursor.execute("INSERT INTO videos (id, photo_id, caption, video_id, created_at) VALUES (%s, %s, %s, %s, NOW())",
                   (pkg_id, photo_id, caption, video_id))
    db.commit()
    cursor.close()
    db.close()

def get_video(pkg_id):
    db = get_db()
    cursor = db.cursor()
    cursor.execute("SELECT photo_id, caption, video_id FROM videos WHERE id = %s", (pkg_id,))
    row = cursor.fetchone()
    cursor.close()
    db.close()
    return row

# --- GLOBAL --- #
known_chats = set()
current_photo = {}
user_views = {}

# --- HANDLERS --- #
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    user_id = update.effective_user.id

    if args and args[0].startswith("video_"):
        pkg_id = args[0].split("_")[1]
        pkg = get_video(pkg_id)
        if not pkg:
            await update.message.reply_text("❌ Video no disponible.")
            return

        for name, username in CHANNELS.items():
            try:
                member = await context.bot.get_chat_member(chat_id=username, user_id=user_id)
                if member.status not in ['member', 'administrator', 'creator']:
                    await update.message.reply_text(
                        "🔒 Debes unirte a los canales.",
                        reply_markup=InlineKeyboardMarkup([
                            [InlineKeyboardButton("🔗 Unirse a Supertv", url=f"https://t.me/{CHANNELS['supertvw2'][1:]}")],
                            [InlineKeyboardButton("🔗 Unirse a fullvvd", url=f"https://t.me/{CHANNELS['fullvvd'][1:]}")],
                            [InlineKeyboardButton("✅ Verificar", callback_data="verify")]
                        ])
                    )
                    return
            except:
                await update.message.reply_text("❌ Error al verificar canales.")
                return

        if is_premium(user_id):
            await update.message.reply_video(video=pkg[2], caption="🎬 Video Premium", protect_content=False)
        else:
            today = str(datetime.utcnow().date())
            if user_id not in user_views:
                user_views[user_id] = {}
            if user_views[user_id].get(today, 0) >= FREE_LIMIT_VIDEOS:
                await update.message.reply_text("🚫 Límite diario alcanzado.",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("💎 Comprar Plan", callback_data="planes")]]))
                return
            user_views[user_id][today] = user_views[user_id].get(today, 0) + 1
            await update.message.reply_video(video=pkg[2], caption="🎬 Video", protect_content=True)

    else:
        await update.message.reply_text("👋 ¡Hola! Unete a los canales:",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔗 Unirse a Supertv", url=f"https://t.me/{CHANNELS['supertvw2'][1:]}")],
                [InlineKeyboardButton("🔗 Unirse a fullvvd", url=f"https://t.me/{CHANNELS['fullvvd'][1:]}")],
                [InlineKeyboardButton("✅ Verificar", callback_data="verify")]
            ]))

async def recibir_foto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if msg.photo and msg.caption:
        current_photo[msg.from_user.id] = {
            "photo_id": msg.photo[-1].file_id,
            "caption": msg.caption
        }
        await msg.reply_text("✅ Sinopsis recibida. Ahora envía el video.")
    else:
        await msg.reply_text("❌ Envía una imagen con sinopsis.")

async def recibir_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    if user_id not in current_photo:
        await update.message.reply_text("❌ Primero envía una sinopsis con imagen.")
        return
    pkg_id = str(int(datetime.utcnow().timestamp()))
    photo_id = current_photo[user_id]["photo_id"]
    caption = current_photo[user_id]["caption"]
    video_id = update.message.video.file_id
    save_video(pkg_id, photo_id, caption, video_id)
    del current_photo[user_id]

    boton = InlineKeyboardMarkup([[InlineKeyboardButton("▶️ Ver video completo", url=f"https://t.me/{(await context.bot.get_me()).username}?start=video_{pkg_id}")]])
    for chat_id in known_chats:
        try:
            await context.bot.send_photo(chat_id=chat_id, photo=photo_id, caption=caption, reply_markup=boton, protect_content=True)
        except:
            pass
    await update.message.reply_text("✅ Contenido enviado a los grupos.")

async def detectar_grupo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id not in known_chats:
        known_chats.add(update.effective_chat.id)

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    user_id = query.from_user.id

    if data == "planes":
        await query.message.reply_text("💎 Planes disponibles:",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("💸 Comprar Premium (1 ⭐)", callback_data="comprar_premium")]
            ]))
    elif data == "comprar_premium":
        await context.bot.send_invoice(
            chat_id=query.message.chat_id,
            title=PREMIUM_ITEM["title"],
            description=PREMIUM_ITEM["description"],
            payload=PREMIUM_ITEM["payload"],
            provider_token=PROVIDER_TOKEN,
            currency=PREMIUM_ITEM["currency"],
            prices=PREMIUM_ITEM["prices"],
            start_parameter="buy-premium"
        )
    elif data == "perfil":
        estado = "✅ Activo" if is_premium(user_id) else "❌ Inactivo"
        await query.message.reply_text(f"🧑 Tu estado Premium: {estado}")

async def precheckout_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.pre_checkout_query.answer(ok=True)

async def successful_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if update.message.successful_payment.invoice_payload == PREMIUM_ITEM["payload"]:
        set_premium(user_id)
        await update.message.reply_text("🎉 ¡Gracias por tu compra! Premium activo por 30 días.")

async def webhook_handler(request):
    data = await request.json()
    update = Update.de_json(data, app_telegram.bot)
    await app_telegram.update_queue.put(update)
    return web.Response(text="OK")

async def on_startup(app):
    await app_telegram.bot.set_webhook(f"{APP_URL}/webhook")

async def on_shutdown(app):
    await app_telegram.bot.delete_webhook()

# --- APLICACIÓN --- #
app_telegram = Application.builder().token(TOKEN).build()
app_telegram.add_handler(CommandHandler("start", start))
app_telegram.add_handler(MessageHandler(filters.PHOTO & filters.ChatType.PRIVATE, recibir_foto))
app_telegram.add_handler(MessageHandler(filters.VIDEO & filters.ChatType.PRIVATE, recibir_video))
app_telegram.add_handler(MessageHandler(filters.ALL & filters.ChatType.GROUPS, detectar_grupo))
app_telegram.add_handler(CallbackQueryHandler(handle_callback))
app_telegram.add_handler(PreCheckoutQueryHandler(precheckout_handler))
app_telegram.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment))

web_app = web.Application()
web_app.router.add_post("/webhook", webhook_handler)
web_app.on_startup.append(on_startup)
web_app.on_shutdown.append(on_shutdown)

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

