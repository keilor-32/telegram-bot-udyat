import os
import logging
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, MessageEntity
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# Cargar variables de entorno desde .env (solo para pruebas locales)
load_dotenv()
TOKEN = os.getenv('TOKEN')

if not TOKEN:
    raise ValueError("❌ TOKEN no definido. Configura la variable de entorno.")

CHANNELS = {
    'supertvw2': '@Supertvw2',
    'fullvvd': '@fullvvd'
}

user_premium = {}
user_reenvios = {}
admin_videos = {}
FREE_LIMIT = 3

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)

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
    await update.message.reply_text(
        "📌 Únete a ambos y luego presiona '✅ Verificar suscripción'.",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

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
        except Exception:
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

    if query.data == "planes":
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

    elif query.data == "comprar":
        await query.message.reply_text(
            "💰 Contacta con @SoporteUdyat para comprar el Plan Premium.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Volver", callback_data="volver")]])
        )

    elif query.data.startswith("reenviar_"):
        original_msg_id = int(query.data.split("_")[1])
        user_reenviados = user_reenvios.get(user_id, 0)
        is_premium = user_premium.get(user_id, False)

        if is_premium:
            await query.message.reply_text("✅ Puedes reenviar sin límites.")
        elif user_reenviados < FREE_LIMIT:
            user_reenvios[user_id] = user_reenviados + 1
            remaining = FREE_LIMIT - user_reenviados - 1
            await query.message.reply_text(f"📤 Reenvío permitido ({user_reenviados + 1}/{FREE_LIMIT}). Te quedan {remaining}.")
        else:
            await query.message.reply_text(
                "🚫 Has alcanzado el límite de reenvíos.\n\n"
                "💎 Compra el plan Premium para continuar.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("💸 Comprar Premium", callback_data="comprar")],
                    [InlineKeyboardButton("🔙 Volver", callback_data="volver")]
                ]),
                parse_mode="Markdown"
            )

    elif query.data == "perfil":
        await query.message.reply_text(
            f"""🧑 Tu perfil:
• Nombre: {user.full_name}
• Usuario: @{user.username or "No tiene"}
• ID: {user.id}
• Plan: {"Premium" if user_premium.get(user_id, False) else "Free"}""",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Volver", callback_data="volver")]])
        )

    elif query.data == "info":
        await query.message.reply_text("ℹ️ Bot para compartir contenido exclusivo.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Volver", callback_data="volver")]]))

    elif query.data == "ayuda":
        await query.message.reply_text("❓ Contacta @SoporteUdyat si necesitas ayuda.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Volver", callback_data="volver")]]))

    elif query.data == "volver":
        await query.message.reply_text("🔙 Menú principal:", reply_markup=get_main_menu())

async def detectar_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    chat_id = msg.chat_id
    from_user = msg.from_user

    if not from_user or not from_user.id:
        return

    member = await context.bot.get_chat_member(chat_id, from_user.id)
    if member.status not in ["administrator", "creator"]:
        return

    msg_id = msg.message_id

    tipo = None
    if msg.video:
        tipo = "video"
    elif any(ent.type == MessageEntity.URL for ent in msg.entities or []):
        tipo = "link"

    if tipo:
        admin_videos[msg_id] = tipo
        boton = InlineKeyboardMarkup([
            [InlineKeyboardButton("📤 Reenviar", callback_data=f"reenviar_{msg_id}")]
        ])
        await msg.reply_text("🔁 Puedes reenviar este contenido (hasta 3 veces si eres Free).", reply_markup=boton)

async def bienvenida(update: Update, context: ContextTypes.DEFAULT_TYPE):
    for user in update.message.new_chat_members:
        await update.message.reply_text(f"👋 Bienvenido, {user.full_name} al grupo 🎉")

async def activar_premium(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_premium[user_id] = True
    await update.message.reply_text("✅ Ahora tienes acceso Premium. ¡Disfruta sin límites!")

def main():
    import telegram
    print("🧪 Versión python-telegram-bot:", telegram.__version__)
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("premium", activar_premium))
    app.add_handler(CallbackQueryHandler(verify, pattern="^verify$"))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, bienvenida))
    app.add_handler(MessageHandler(filters.VIDEO | filters.Entity("url"), detectar_admin))
    print("✅ BOT INICIADO CORRECTAMENTE")
    app.run_polling()

if __name__ == "__main__":
    main()

