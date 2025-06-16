import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes
import os

TOKEN = os.getenv("8139687252:AAF16ffsjmrlwNuZ2yoULQ3BZWXhh7Vb91g")

CHANNELS = {
    'prueba1': '@hsitotv',
    'prueba2': '@udyat_channel'
}

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    keyboard = [
        [InlineKeyboardButton("🔗 Unirse a CHAT", url=f"https://t.me/{CHANNELS['hsitotv'][1:]}")],
        [InlineKeyboardButton("🔗 Unirse a HSITOTV", url=f"https://t.me/{CHANNELS['fullvvd'][1:]}")],
        [InlineKeyboardButton("✅ Verificar suscripción", callback_data='verify')]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text(
        "Debes unirte a ambos canales para usar este bot. Luego haz clic en '✅ Verificar suscripción'.",
        reply_markup=reply_markup
    )

async def verify(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
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
        await query.edit_message_text("✅ Verificación completada. ¡Gracias por unirte!")
    else:
        msg = "❌ Aún no estás suscrito a:" + "\n".join(f"• {c}" for c in not_joined)
        await query.edit_message_text(msg)

def main() -> None:
    application = Application.builder().token(TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CallbackQueryHandler(verify, pattern='^verify$'))
    print("Bot iniciado.")
    application.run_polling()

if __name__ == "__main__":
    main()
