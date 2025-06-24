import os
import logging
from datetime import datetime, timedelta
from aiohttp import web
from dotenv import load_dotenv
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    LabeledPrice, MessageEntity
)
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, ContextTypes, filters, PreCheckoutQueryHandler
)

load_dotenv()
TOKEN = os.getenv("TOKEN")
PORT = int(os.getenv("PORT", "8080"))
WEBHOOK_URL = f"https://telegram-bot-udyat-8.onrender.com/webhook/{TOKEN}"
WEBHOOK_PATH = f"/webhook/{TOKEN}"

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

CHANNELS = {
    'supertvw2': '@Supertvw2',
    'fullvvd': '@fullvvd'
}
user_premium = {}     # user_id: expiration datetime
user_reenvios = {}    # user_id: count
admin_videos = {}
FREE_LIMIT = 3

# Definición del producto premium
PREMIUM_ITEM = {
    "title": "Plan Premium",
    "description": "Reenvíos ilimitados por 30 días.",
    "payload": "premium_plan",
    "currency": "XTR",
    "prices": [LabeledPrice("Plan Premium", 100)]  # 100 estrellas
}

def get_main_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📢 Canal", url="https://t.me/hsitotv"),
         InlineKeyboardButton("👥 Grupo", url="https://t.me/udyat_channel")],
        [InlineKeyboardButton("💎 Planes", callback_data="planes"),
         InlineKeyboardButton("🧑 Perfil", callback_data="perfil")],
        [InlineKeyboardButton("ℹ️ Info", callback_data="info"),
         InlineKeyboardButton("❓ Ayuda", callback_data="ayuda")]
    ])

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("👋 ¡Hola! Antes de comenzar debes unirte a los canales.")
    keyboard = [
        [InlineKeyboardButton("🔗 Unirse a Supertv", url=f"https://t.me/{CHANNELS['supertvw2'][1:]}")],
        [InlineKeyboardButton("🔗 Unirse a fullvvd", url=f"https://t.me/{CHANNELS['fullvvd'][1:]}")],
        [InlineKeyboardButton("✅ Verificar suscripción", callback_data='verify')]
    ]
    await update.message.reply_text("📌 Únete a ambos y luego presiona '✅ Verificar suscripción'.", reply_markup=InlineKeyboardMarkup(keyboard))

async def verify(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
        await query.edit_message_text("✅ Verificación completada. Aquí tienes el menú:")
        await query.message.reply_text("📋 Menú principal:", reply_markup=get_main_menu())
    else:
        msg = "❌ Aún no estás suscrito a:\n" + "\n".join(f"• {c}" for c in not_joined)
        await query.edit_message_text(msg)

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = query.from_user
    user_id = user.id
    data = query.data

    if data == "planes":
        await query.message.reply_text(
            "💎 *Planes disponibles:*\n\n"
            "🔹 Free – Hasta 3 reenvíos.\n"
            "🔹 Premium – Reenvíos ilimitados por 1 mes.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("💸 Comprar Premium", callback_data="comprar")],
                [InlineKeyboardButton("🔙 Volver", callback_data="volver")]
            ])
        )
    elif data == "comprar":
        now = datetime.utcnow()
        exp = user_premium.get(user_id)
        if exp and exp > now:
            await query.message.reply_text("✅ Ya eres usuario Premium hasta " + exp.strftime("%Y-%m-%d") + ".")
            return
        await context.bot.send_invoice(
            chat_id=query.message.chat_id,
            title=PREMIUM_ITEM["title"],
            description=PREMIUM_ITEM["description"],
            payload=PREMIUM_ITEM["payload"],
            provider_token="",
            currency=PREMIUM_ITEM["currency"],
            prices=PREMIUM_ITEM["prices"],
            start_parameter="buy-premium"
        )
    elif data.startswith("reenviar_"):
        original_id = int(data.split("_")[1])
        sent = user_reenvios.get(user_id, 0)
        exp = user_premium.get(user_id)
        now = datetime.utcnow()
        if exp and exp > now:
            await query.message.reply_text("✅ Reenvío ilimitado (Premium activo).")
        elif sent < FREE_LIMIT:
            user_reenvios[user_id] = sent + 1
            await query.message.reply_text(f"📤 Reenvío {sent+1}/{FREE_LIMIT}.")
        else:
            await query.message.reply_text(
                "🚫 Límite alcanzado.\n💎 Compra Premium para seguir reenviando.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("💸 Comprar Premium", callback_data="comprar")],
                    [InlineKeyboardButton("🔙 Volver", callback_data="volver")]
                ]),
                parse_mode="Markdown"
            )
    elif data == "perfil":
        await query.message.reply_text(
            f"🧑 Perfil:\n• {user.full_name}\n• @{user.username or 'Sin usuario'}\n• ID: {user_id}\n• Plan: {'Premium' if user_premium.get(user_id, False) and user_premium[user_id] > datetime.utcnow() else 'Free'}",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Volver", callback_data="volver")]])
        )
    elif data == "info":
        await query.message.reply_text("ℹ️ Bot para compartir contenido exclusivo.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Volver", callback_data="volver")]]))
    elif data == "ayuda":
        await query.message.reply_text("❓ Contacta @SoporteUdyat si necesitas ayuda.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Volver", callback_data="volver")]]))
    elif data == "volver":
        await query.message.reply_text("🔙 Menú principal:", reply_markup=get_main_menu())

async def precheckout_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.pre_checkout_query.answer(ok=True)

async def successful_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    payment = update.message.successful_payment
    user_id = update.effective_user.id
    if payment.invoice_payload == PREMIUM_ITEM["payload"]:
        exp = datetime.utcnow() + timedelta(days=30)
        user_premium[user_id] = exp
        user_reenvios[user_id] = 0
        await update.message.reply_text(
            f"🎉 ¡Gracias por tu compra!\nAcceso Premium hasta {exp.strftime('%Y-%m-%d')}\nReenvíos ilimitados activados."
        )

async def detectar_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    from_user = msg.from_user
    member = await context.bot.get_chat_member(chat_id=msg.chat_id, user_id=from_user.id)
    if member.status not in ["administrator", "creator"]:
        return
    tipo = "video" if msg.video else ("link" if any(ent.type == MessageEntity.URL for ent in msg.entities or []) else None)
    if tipo:
        admin_videos[msg.message_id] = tipo
        boton = InlineKeyboardMarkup([[InlineKeyboardButton("📤 Reenviar", callback_data=f"reenviar_{msg.message_id}")]])
        await msg.reply_text("🔁 Puedes reenviar este contenido:", reply_markup=boton)

async def bienvenida(update: Update, context: ContextTypes.DEFAULT_TYPE):
    for u in update.message.new_chat_members:
        await update.message.reply_text(f"👋 Bienvenido, {u.full_name} 🎉")

async def activar_premium(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_premium[user_id] = datetime.utcnow() + timedelta(days=30)
    await update.message.reply_text("✅ Premium activado manualmente por 30 días.")

async def webhook_handler(request):
    data = await request.json()
    upd = Update.de_json(data, app.bot)
    await app.update_queue.put(upd)
    return web.Response(text="OK")

app = Application.builder().token(TOKEN).build()
app.add_handler(CommandHandler("start", start))
app.add_handler(CommandHandler("premium", activar_premium))
app.add_handler(CallbackQueryHandler(verify, pattern="^verify$"))
app.add_handler(CallbackQueryHandler(handle_callback))
app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, bienvenida))
app.add_handler(MessageHandler(filters.VIDEO | filters.Entity("url"), detectar_admin))
app.add_handler(PreCheckoutQueryHandler(precheckout_handler))
app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment))

def main():
    import asyncio
    async def run():
        await app.initialize()
        await app.bot.set_webhook(WEBHOOK_URL)
        await app.start()
        web_app = web.Application()
        web_app.router.add_post(WEBHOOK_PATH, webhook_handler)
        runner = web.AppRunner(web_app)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", PORT)
        await site.start()
        logger.info("✅ Bot y servidor web corriendo")
        await asyncio.Event().wait()
    asyncio.run(run())

if __name__ == "__main__":
    main()






