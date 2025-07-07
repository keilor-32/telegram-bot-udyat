import logging
from datetime import datetime, timedelta
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup, LabeledPrice
)
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, MessageHandler, ContextTypes,
    filters, PreCheckoutQueryHandler
)

TOKEN = "8139687252:AAF16ffsjmrlwNuZ2yoULQ3BZWXhh7Vb91g"
PROVIDER_TOKEN = ""  # Pon tu token real si tienes, sino deja vacío

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

CHANNELS = {
    'supertvw2': '@Supertvw2',
    'fullvvd': '@fullvvd'
}

user_premium = {}
user_daily_views = {}
known_chats = set()
content_packages = {}

FREE_LIMIT = 10

PREMIUM_ITEM = {
    "title": "Plan Premium",
    "description": "Acceso y reenvíos ilimitados por 30 días.",
    "payload": "premium_plan",
    "currency": "XTR",
    "prices": [LabeledPrice("Premium por 30 días", 1)]
}

def is_premium(user_id):
    return user_id in user_premium and user_premium[user_id] > datetime.utcnow()

def can_view_video(user_id):
    if is_premium(user_id):
        return True
    today = str(datetime.utcnow().date())
    return user_daily_views.get(user_id, {}).get(today, 0) < FREE_LIMIT

def register_view(user_id):
    today = str(datetime.utcnow().date())
    if user_id not in user_daily_views:
        user_daily_views[user_id] = {}
    user_daily_views[user_id][today] = user_daily_views[user_id].get(today, 0) + 1

current_photo = {}

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    user_id = update.effective_user.id

    if args and args[0].startswith("video_"):
        pkg_id = args[0].split("_")[1]
        pkg = content_packages.get(pkg_id)
        if not pkg or "video_id" not in pkg:
            await update.message.reply_text("❌ Video no disponible.")
            return

        for name, username in CHANNELS.items():
            try:
                member = await context.bot.get_chat_member(chat_id=username, user_id=user_id)
                if member.status not in ['member', 'administrator', 'creator']:
                    await update.message.reply_text(
                        "🔒 Para ver este contenido debes unirte a todos los canales primero.",
                        reply_markup=InlineKeyboardMarkup([
                            [InlineKeyboardButton("🔗 Unirse a Supertv", url=f"https://t.me/{CHANNELS['supertvw2'][1:]}")],
                            [InlineKeyboardButton("🔗 Unirse a fullvvd", url=f"https://t.me/{CHANNELS['fullvvd'][1:]}")],
                            [InlineKeyboardButton("✅ Verificar suscripción", callback_data='verify')]
                        ])
                    )
                    return
            except:
                await update.message.reply_text("❌ Error al verificar canales. Intenta más tarde.")
                return

        if can_view_video(user_id):
            register_view(user_id)
            await update.message.reply_video(
                video=pkg["video_id"],
                caption="🎬 Aquí tienes el video completo.",
                protect_content=True
            )
        else:
            await update.message.reply_text(
                "🚫 Límite de 10 videos por día alcanzado.\n💎 Compra Premium para ver sin límites.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("💸 Comprar Premium", callback_data="comprar")]])
            )
    else:
        keyboard = [
            [InlineKeyboardButton("🔗 Unirse a Supertv", url=f"https://t.me/{CHANNELS['supertvw2'][1:]}")],
            [InlineKeyboardButton("🔗 Unirse a fullvvd", url=f"https://t.me/{CHANNELS['fullvvd'][1:]}")],
            [InlineKeyboardButton("✅ Verificar suscripción", callback_data='verify')]
        ]
        await update.message.reply_text("👋 ¡Hola! Para acceder debes unirte a los canales y verificar.", reply_markup=InlineKeyboardMarkup(keyboard))

async def verificar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    not_joined = []
    for name, username in CHANNELS.items():
        try:
            member = await context.bot.get_chat_member(chat_id=username, user_id=user_id)
            if member.status not in ['member', 'administrator', 'creator']:
                not_joined.append(name)
        except:
            not_joined.append(name)
    if not not_joined:
        await query.edit_message_text("✅ Verificación completada. Menú disponible:")
        await query.message.reply_text("📋 Menú:", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("💎 Planes", callback_data="planes")]]))
    else:
        await query.edit_message_text("❌ Aún no estás suscrito a todos los canales.")

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    data = query.data

    if data == "planes":
        await query.message.reply_text(
            "🔹 Free: Hasta 10 videos por día.\n🔸 Premium: Acceso y reenvíos ilimitados.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("💸 Comprar Premium", callback_data="comprar")]])
        )
    elif data == "comprar":
        if is_premium(user_id):
            exp = user_premium[user_id].strftime("%Y-%m-%d")
            await query.message.reply_text(f"✅ Ya eres Premium hasta {exp}.")
            return
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

async def precheckout_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.pre_checkout_query.answer(ok=True)

async def successful_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if update.message.successful_payment.invoice_payload == PREMIUM_ITEM["payload"]:
        user_premium[user_id] = datetime.utcnow() + timedelta(days=30)
        await update.message.reply_text("🎉 ¡Gracias por tu compra! Premium activado por 30 días.")

async def recibir_foto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if msg.photo and msg.caption:
        current_photo[msg.from_user.id] = {
            "photo_id": msg.photo[-1].file_id,
            "caption": msg.caption
        }
        await msg.reply_text("✅ Sinopsis recibida. Ahora envía el video.")

async def recibir_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    user_id = msg.from_user.id
    if user_id not in current_photo:
        await msg.reply_text("❌ Primero debes enviar una sinopsis con imagen.")
        return

    pkg_id = str(int(datetime.utcnow().timestamp()))
    content_packages[pkg_id] = {
        "photo_id": current_photo[user_id]["photo_id"],
        "caption": current_photo[user_id]["caption"],
        "video_id": msg.video.file_id
    }
    del current_photo[user_id]

    boton = InlineKeyboardMarkup([[
        InlineKeyboardButton("▶️ Ver video completo", url=f"https://t.me/{(await context.bot.get_me()).username}?start=video_{pkg_id}")
    ]])
    for chat_id in known_chats:
        try:
            await context.bot.send_photo(
                chat_id=chat_id,
                photo=content_packages[pkg_id]["photo_id"],
                caption=content_packages[pkg_id]["caption"],
                reply_markup=boton,
                protect_content=True
            )
        except Exception as e:
            logger.warning(f"No se pudo enviar a {chat_id}: {e}")

    await msg.reply_text("✅ Portada enviada a los grupos.")

async def detectar_grupo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if chat.type in ['group', 'supergroup']:
        known_chats.add(chat.id)
        logger.info(f"Grupo detectado: {chat.id}")

app = Application.builder().token(TOKEN).build()

app.add_handler(CommandHandler("start", start))
app.add_handler(CallbackQueryHandler(verificar, pattern="^verify$"))
app.add_handler(CallbackQueryHandler(handle_callback))
app.add_handler(PreCheckoutQueryHandler(precheckout_handler))
app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment))
app.add_handler(MessageHandler(filters.PHOTO & filters.ChatType.PRIVATE, recibir_foto))
app.add_handler(MessageHandler(filters.VIDEO & filters.ChatType.PRIVATE, recibir_video))
app.add_handler(MessageHandler(filters.ALL & filters.ChatType.GROUPS, detectar_grupo))

def main():
    logger.info("🤖 Bot iniciado con polling")
    app.run_polling()

if __name__ == "__main__":
    main()







