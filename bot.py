import logging
import json
import os
from datetime import datetime, timedelta
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup, LabeledPrice
)
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, MessageHandler, ContextTypes,
    filters, PreCheckoutQueryHandler
)

TOKEN = "8139687252:AAF16ffsjmrlwNuZ2yoULQ3BZWXhh7Vb91g"
PROVIDER_TOKEN = ""  # Token de proveedor para pagos, si tienes

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

CHANNELS = {
    'supertvw2': '@Supertvw2',
    'fullvvd': '@fullvvd'
}

USER_PREMIUM_FILE = "user_premium.json"
USER_VIEWS_FILE = "user_views.json"
CONTENT_PACKAGES_FILE = "content_packages.json"
KNOWN_CHATS_FILE = "known_chats.json"

FREE_LIMIT_VIDEOS = 3  # Puedes cambiar el límite para usuarios free

PREMIUM_ITEM = {
    "title": "Plan Premium",
    "description": "Acceso y reenvíos ilimitados por 30 días.",
    "payload": "premium_plan",
    "currency": "XTR",
    "prices": [LabeledPrice("Premium por 30 días", 100)]  # Ajusta el precio según moneda
}

# Estructuras en memoria
user_premium = {}        # user_id: datetime expiracion
user_daily_views = {}    # user_id: {fecha_str: conteo}
content_packages = {}    # pkg_id: {"photo_id":..., "caption":..., "video_id":...}
known_chats = set()
current_photo = {}       # user_id: {"photo_id":..., "caption":...}

# --- Persistencia --- #
def save_json(filename, data):
    with open(filename, "w", encoding="utf-8") as f:
        json.dump(data, f)

def load_json(filename):
    if not os.path.exists(filename):
        return {}
    with open(filename, "r", encoding="utf-8") as f:
        return json.load(f)

def save_data():
    save_json(USER_PREMIUM_FILE, {str(k): v.isoformat() for k,v in user_premium.items()})
    save_json(USER_VIEWS_FILE, user_daily_views)
    save_json(CONTENT_PACKAGES_FILE, content_packages)
    save_json(KNOWN_CHATS_FILE, list(known_chats))

def load_data():
    global user_premium, user_daily_views, content_packages, known_chats
    up = load_json(USER_PREMIUM_FILE)
    user_premium = {int(k): datetime.fromisoformat(v) for k,v in up.items()}
    user_daily_views = load_json(USER_VIEWS_FILE)
    content_packages = load_json(CONTENT_PACKAGES_FILE)
    known_chats = set(load_json(KNOWN_CHATS_FILE))

# --- Helpers --- #
def is_premium(user_id):
    return user_id in user_premium and user_premium[user_id] > datetime.utcnow()

def can_view_video(user_id):
    if is_premium(user_id):
        return True
    today = str(datetime.utcnow().date())
    return user_daily_views.get(str(user_id), {}).get(today, 0) < FREE_LIMIT_VIDEOS

def register_view(user_id):
    today = str(datetime.utcnow().date())
    uid = str(user_id)
    if uid not in user_daily_views:
        user_daily_views[uid] = {}
    user_daily_views[uid][today] = user_daily_views[uid].get(today, 0) + 1
    save_data()

def get_main_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📢 Canal", url="https://t.me/hsitotv"),
         InlineKeyboardButton("👥 Grupo", url="https://t.me/udyat_channel")],
        [InlineKeyboardButton("💎 Planes", callback_data="planes"),
         InlineKeyboardButton("🧑 Perfil", callback_data="perfil")],
        [InlineKeyboardButton("ℹ️ Info", callback_data="info"),
         InlineKeyboardButton("❓ Ayuda", callback_data="ayuda")]
    ])

# --- Handlers --- #

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    user_id = update.effective_user.id

    # Si viene con start=video_pkgid
    if args and args[0].startswith("video_"):
        pkg_id = args[0].split("_")[1]
        pkg = content_packages.get(pkg_id)
        if not pkg or "video_id" not in pkg:
            await update.message.reply_text("❌ Video no disponible.")
            return

        # Verificar suscripción a canales
        for name, username in CHANNELS.items():
            try:
                member = await context.bot.get_chat_member(chat_id=username, user_id=user_id)
                if member.status not in ['member', 'administrator', 'creator']:
                    await update.message.reply_text(
                        "🔒 Para ver este contenido debes unirte a todos los canales primero.",
                        reply_markup=InlineKeyboardMarkup([
                            [InlineKeyboardButton(f"🔗 Unirse a {username}", url=f"https://t.me/{username[1:]}")],
                            [InlineKeyboardButton("✅ Verificar suscripción", callback_data='verify')]
                        ])
                    )
                    return
            except Exception as e:
                logger.warning(f"Error al verificar canal {username}: {e}")
                await update.message.reply_text("❌ Error al verificar canales. Intenta más tarde.")
                return

        # Revisar límite o premium
        if can_view_video(user_id):
            register_view(user_id)
            await update.message.reply_video(
                video=pkg["video_id"],
                caption="🎬 Aquí tienes el video completo.",
                protect_content=not is_premium(user_id)  # Usuarios free: protegido, premium: sin protección
            )
        else:
            await update.message.reply_text(
                f"🚫 Has alcanzado tu límite diario de {FREE_LIMIT_VIDEOS} videos.\n"
                "💎 Compra Premium para acceso ilimitado.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("💸 Comprar Premium", callback_data="comprar")]])
            )
        return

    # Mensaje de bienvenida y pedir unirse a canales
    keyboard = [
        [InlineKeyboardButton(f"🔗 Unirse a {CHANNELS['supertvw2']}", url=f"https://t.me/{CHANNELS['supertvw2'][1:]}")],
        [InlineKeyboardButton(f"🔗 Unirse a {CHANNELS['fullvvd']}", url=f"https://t.me/{CHANNELS['fullvvd'][1:]}")],
        [InlineKeyboardButton("✅ Verificar suscripción", callback_data='verify')]
    ]
    await update.message.reply_text(
        "👋 ¡Hola! Para acceder debes unirte a los canales y verificar.",
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
                not_joined.append(username)
        except Exception as e:
            logger.warning(f"Error al verificar canal {username}: {e}")
            not_joined.append(username)
    if not not_joined:
        await query.edit_message_text("✅ Verificación completada. Menú disponible:")
        await query.message.reply_text("📋 Menú principal:", reply_markup=get_main_menu())
    else:
        msg = "❌ Aún no estás suscrito a estos canales:\n" + "\n".join(f"• {c}" for c in not_joined)
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
            f"🔹 Free – Hasta {FREE_LIMIT_VIDEOS} videos gratis por día.\n"
            "🔹 Premium – Acceso ilimitado por 1 mes.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("💸 Comprar Premium", callback_data="comprar")],
                [InlineKeyboardButton("🔙 Volver", callback_data="volver")]
            ])
        )
    elif data == "comprar":
        if is_premium(user_id):
            exp = user_premium[user_id].strftime("%Y-%m-%d")
            await query.message.reply_text(f"✅ Ya eres usuario Premium hasta {exp}.")
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
    elif data == "perfil":
        plan = "Premium" if is_premium(user_id) else "Free"
        exp_str = user_premium.get(user_id).strftime("%Y-%m-%d") if is_premium(user_id) else "N/A"
        await query.message.reply_text(
            f"🧑 Perfil:\n• {user.full_name}\n• @{user.username or 'Sin usuario'}\n"
            f"• ID: {user_id}\n• Plan: {plan}\n• Expira: {exp_str}",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Volver", callback_data="volver")]])
        )
    elif data == "info":
        await query.message.reply_text(
            "ℹ️ Bot para compartir contenido exclusivo.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Volver", callback_data="volver")]])
        )
    elif data == "ayuda":
        await query.message.reply_text(
            "❓ Contacta @SoporteUdyat si necesitas ayuda.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Volver", callback_data="volver")]])
        )
    elif data == "volver":
        await query.message.reply_text("🔙 Menú principal:", reply_markup=get_main_menu())

async def precheckout_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.pre_checkout_query.answer(ok=True)

async def successful_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if update.message.successful_payment.invoice_payload == PREMIUM_ITEM["payload"]:
        user_premium[user_id] = datetime.utcnow() + timedelta(days=30)
        save_data()
        await update.message.reply_text(
            "🎉 ¡Gracias por tu compra!\nAcceso Premium activado por 30 días."
        )

async def recibir_foto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    user_id = msg.from_user.id
    if msg.photo and msg.caption:
        current_photo[user_id] = {
            "photo_id": msg.photo[-1].file_id,
            "caption": msg.caption
        }
        await msg.reply_text("✅ Sinopsis recibida. Ahora envía el video.")
    else:
        await msg.reply_text("❌ Envía una imagen con descripción (sinopsis).")

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
    save_data()

    boton = InlineKeyboardMarkup([[
        InlineKeyboardButton("▶️ Ver video completo", url=f"https://t.me/{(await context.bot.get_me()).username}?start=video_{pkg_id}")
    ]])

    # Enviar solo sinopsis + botón a los grupos guardados
    for chat_id in known_chats:
        try:
            await context.bot.send_photo(
                chat_id=chat_id,
                photo=content_packages[pkg_id]["photo_id"],
                caption=content_packages[pkg_id]["caption"],
                reply_markup=boton,
                protect_content=True  # Protegemos para que nadie reenvíe desde el grupo
            )
        except Exception as e:
            logger.warning(f"No se pudo enviar a {chat_id}: {e}")

    await msg.reply_text("✅ Portada enviada a los grupos.")

async def detectar_grupo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if chat.type in ['group', 'supergroup']:
        if chat.id not in known_chats:
            known_chats.add(chat.id)
            save_data()
            logger.info(f"Grupo detectado y guardado: {chat.id}")

async def bienvenida(update: Update, context: ContextTypes.DEFAULT_TYPE):
    for u in update.message.new_chat_members:
        await update.message.reply_text(f"👋 Bienvenido, {u.full_name} 🎉")

async def activar_premium(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_premium[user_id] = datetime.utcnow() + timedelta(days=30)
    save_data()
    await update.message.reply_text("✅ Premium activado manualmente por 30 días.")

# --- Configuración de bot y handlers --- #
app = Application.builder().token(TOKEN).build()

app.add_handler(CommandHandler("start", start))
app.add_handler(CommandHandler("premium", activar_premium))
app.add_handler(CallbackQueryHandler(verify, pattern="^verify$"))
app.add_handler(CallbackQueryHandler(handle_callback))
app.add_handler(PreCheckoutQueryHandler(precheckout_handler))
app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment))
app.add_handler(MessageHandler(filters.PHOTO & filters.ChatType.PRIVATE, recibir_foto))
app.add_handler(MessageHandler(filters.VIDEO & filters.ChatType.PRIVATE, recibir_video))
app.add_handler(MessageHandler(filters.ALL & filters.ChatType.GROUPS, detectar_grupo))
app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, bienvenida))

def main():
    load_data()
    logger.info("🤖 Bot iniciado con polling")
    app.run_polling()

if __name__ == "__main__":
    main()




