import os
import logging
import json
from datetime import datetime, timedelta
from aiohttp import web
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    LabeledPrice
)
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, ContextTypes, filters, PreCheckoutQueryHandler
)

TOKEN = os.getenv("TOKEN")
if not TOKEN:
    raise ValueError("⚠️ ERROR: La variable de entorno TOKEN no está configurada o está vacía. Verifica tu configuración.")

PROVIDER_TOKEN = os.getenv("PROVIDER_TOKEN") or ""  # Tu token proveedor de pagos aquí

PORT = int(os.getenv("PORT", "8080"))
WEBHOOK_URL = f"https://telegram-bot-udyat-8.onrender.com/webhook/{TOKEN}"
WEBHOOK_PATH = f"/webhook/{TOKEN}"

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

CHANNELS = {
    'supertvw2': '@Supertvw2',
    'fullvvd': '@fullvvd'
}

DATA_FILE = "bot_data.json"

user_premium = {}     # user_id: expiration datetime
user_daily_views = {} # user_id: {date_str: views_count}
known_chats = set()   # grupos donde enviar contenido
content_packages = {} # pkg_id: {"photo_id":..., "caption":..., "video_id":...}
current_photo = {}    # user_id: {"photo_id":..., "caption":...}

FREE_LIMIT_VIEWS = 3
FREE_LIMIT_REENVIOS = 3

PREMIUM_ITEM = {
    "title": "Plan Premium",
    "description": "Acceso y reenvíos ilimitados por 30 días.",
    "payload": "premium_plan",
    "currency": "XTR",  # Moneda que uses para Telegram Stars
    "prices": [LabeledPrice("Premium por 30 días", 100)]  # 100 estrellas
}

def save_data():
    data = {
        "user_premium": {str(k): v.isoformat() for k, v in user_premium.items()},
        "user_daily_views": {str(k): v for k, v in user_daily_views.items()},
        "content_packages": content_packages,
        "known_chats": list(known_chats)
    }
    with open(DATA_FILE, "w") as f:
        json.dump(data, f)

def load_data():
    global user_premium, user_daily_views, content_packages, known_chats
    if not os.path.exists(DATA_FILE):
        return
    with open(DATA_FILE, "r") as f:
        data = json.load(f)
    user_premium = {int(k): datetime.fromisoformat(v) for k, v in data.get("user_premium", {}).items()}
    user_daily_views = {int(k): v for k, v in data.get("user_daily_views", {}).items()}
    content_packages = data.get("content_packages", {})
    known_chats = set(data.get("known_chats", []))

def is_premium(user_id):
    return user_id in user_premium and user_premium[user_id] > datetime.utcnow()

def can_view_video(user_id):
    if is_premium(user_id):
        return True
    today = str(datetime.utcnow().date())
    return user_daily_views.get(user_id, {}).get(today, 0) < FREE_LIMIT_VIEWS

def register_view(user_id):
    today = str(datetime.utcnow().date())
    if user_id not in user_daily_views:
        user_daily_views[user_id] = {}
    user_daily_views[user_id][today] = user_daily_views[user_id].get(today, 0) + 1
    save_data()

async def is_subscribed_to_all(user_id, context):
    for name, username in CHANNELS.items():
        try:
            member = await context.bot.get_chat_member(chat_id=username, user_id=user_id)
            if member.status not in ['member', 'administrator', 'creator']:
                return False
        except Exception as e:
            logger.warning(f"Error verificando canal {username}: {e}")
            return False
    return True

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

def get_main_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📢 Canal", url="https://t.me/hsitotv"),
         InlineKeyboardButton("👥 Grupo", url="https://t.me/udyat_channel")],
        [InlineKeyboardButton("💎 Planes", callback_data="planes"),
         InlineKeyboardButton("🧑 Perfil", callback_data="perfil")],
        [InlineKeyboardButton("ℹ️ Info", callback_data="info"),
         InlineKeyboardButton("❓ Ayuda", callback_data="ayuda")]
    ])

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = query.from_user
    user_id = user.id
    data = query.data

    if data == "planes":
        await query.message.reply_text(
            "💎 *Planes disponibles:*\n\n"
            f"🔹 Free – Hasta {FREE_LIMIT_REENVIOS} reenvíos y {FREE_LIMIT_VIEWS} vistas por día.\n"
            "🔹 Premium – Reenvíos y vistas ilimitadas por 1 mes.",
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
            provider_token=PROVIDER_TOKEN,
            currency=PREMIUM_ITEM["currency"],
            prices=PREMIUM_ITEM["prices"],
            start_parameter="buy-premium"
        )
    elif data == "perfil":
        plan = "Premium" if is_premium(user_id) else "Free"
        exp_str = user_premium.get(user_id).strftime("%Y-%m-%d") if is_premium(user_id) else "-"
        await query.message.reply_text(
            f"🧑 Perfil:\n• {user.full_name}\n• @{user.username or 'Sin usuario'}\n• ID: {user_id}\n• Plan: {plan}\n• Expira: {exp_str}",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Volver", callback_data="volver")]])
        )
    elif data == "info":
        await query.message.reply_text("ℹ️ Bot para compartir contenido exclusivo.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Volver", callback_data="volver")]]))
    elif data == "ayuda":
        await query.message.reply_text("❓ Contacta @SoporteUdyat si necesitas ayuda.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Volver", callback_data="volver")]]))
    elif data == "volver":
        await query.message.reply_text("🔙 Menú principal:", reply_markup=get_main_menu())
    elif data.startswith("video_"):
        pkg_id = data.split("_", 1)[1]
        pkg = content_packages.get(pkg_id)
        if not pkg:
            await query.message.reply_text("❌ Video no encontrado o expirado.")
            return
        if not await is_subscribed_to_all(user_id, context):
            await query.message.reply_text(
                "🔒 Debes estar suscripto a todos los canales para ver este video.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔗 Unirse a Supertv", url=f"https://t.me/{CHANNELS['supertvw2'][1:]}")],
                    [InlineKeyboardButton("🔗 Unirse a fullvvd", url=f"https://t.me/{CHANNELS['fullvvd'][1:]}")],
                    [InlineKeyboardButton("✅ Verificar suscripción", callback_data="verify")]
                ])
            )
            return
        if not can_view_video(user_id):
            await query.message.reply_text(
                f"🚫 Límite de {FREE_LIMIT_VIEWS} videos por día alcanzado.\n💎 Compra Premium para ver sin límites.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("💸 Comprar Premium", callback_data="comprar")]])
            )
            return
        register_view(user_id)
        await query.message.reply_video(
            video=pkg["video_id"],
            caption="🎬 Aquí tienes el video completo.",
            protect_content=True
        )

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
    save_data()

    boton = InlineKeyboardMarkup([[InlineKeyboardButton("▶️ Ver video completo", callback_data=f"video_{pkg_id}")]])
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
        save_data()
        logger.info(f"Grupo detectado y guardado: {chat.id}")

async def precheckout_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.pre_checkout_query.answer(ok=True)

async def successful_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    payment = update.message.successful_payment
    user_id = update.effective_user.id
    if payment.invoice_payload == PREMIUM_ITEM["payload"]:
        exp = datetime.utcnow() + timedelta(days=30)
        user_premium[user_id] = exp
        user_daily_views[user_id] = {}  # Reset diario al comprar premium
        save_data()
        await update.message.reply_text(
            f"🎉 ¡Gracias por tu compra!\nAcceso Premium hasta {exp.strftime('%Y-%m-%d')}\nReenvíos y vistas ilimitadas activados."
        )

async def webhook_handler(request):
    data = await request.json()
    upd = Update.de_json(data, app.bot)
    await app.update_queue.put(upd)
    return web.Response(text="OK")

app = Application.builder().token(TOKEN).build()

app.add_handler(CommandHandler("start", start))
app.add_handler(CallbackQueryHandler(verify, pattern="^verify$"))
app.add_handler(CallbackQueryHandler(handle_callback))
app.add_handler(MessageHandler(filters.PHOTO & filters.CaptionRegex(".+"), recibir_foto))
app.add_handler(MessageHandler(filters.VIDEO & filters.ChatType.PRIVATE, recibir_video))
app.add_handler(MessageHandler(filters.ALL & filters.ChatType.GROUPS, detectar_grupo))
app.add_handler(PreCheckoutQueryHandler(precheckout_handler))
app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment))

if __name__ == "__main__":
    load_data()

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






