import logging
import json
import os
import asyncio
from datetime import datetime, timedelta
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup, LabeledPrice
)
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, MessageHandler, ContextTypes,
    filters, PreCheckoutQueryHandler
)
from aiohttp import web

# --- CONFIGURACIÓN --- #
TOKEN = os.getenv("TOKEN")
PROVIDER_TOKEN = os.getenv("PROVIDER_TOKEN", "")  # Se lee de variable entorno
APP_URL = os.getenv("APP_URL")  # Ejemplo: https://telegram-bot-udyat.onrender.com
PORT = int(os.getenv("PORT", "8080"))

if not TOKEN:
    raise ValueError("❌ ERROR: La variable de entorno TOKEN no está configurada.")
if not APP_URL:
    raise ValueError("❌ ERROR: La variable de entorno APP_URL no está configurada.")

CHANNELS = {
    'supertvw2': '@Supertvw2',
    'fullvvd': '@fullvvd'
}

FREE_LIMIT_VIDEOS = 3  # Free: 3 vistas por día

PREMIUM_ITEM = {
    "title": "Plan Premium",
    "description": "Acceso y reenvíos ilimitados por 30 días.",
    "payload": "premium_plan",
    "currency": "XTR",  # XTR = Telegram Stars
    "prices": [LabeledPrice("Premium por 30 días", 1)]  # 1 estrella
}

PLAN_PRO_ITEM = {
    "title": "Plan Pro",
    "description": "50 videos diarios, sin reenvíos ni compartir.",
    "payload": "plan_pro",
    "currency": "XTR",
    "prices": [LabeledPrice("Plan Pro por 30 días", 40)]  # 40 estrellas
}

PLAN_ULTRA_ITEM = {
    "title": "Plan Ultra",
    "description": "Videos y reenvíos ilimitados por 30 días.",
    "payload": "plan_ultra",
    "currency": "XTR",
    "prices": [LabeledPrice("Plan Ultra por 30 días", 100)]  # 100 estrellas
}

# --- ARCHIVOS --- #
USER_PREMIUM_FILE = "user_premium.json"
USER_VIEWS_FILE = "user_views.json"
CONTENT_PACKAGES_FILE = "content_packages.json"
KNOWN_CHATS_FILE = "known_chats.json"

# --- LOGGING --- #
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- VARIABLES EN MEMORIA --- #
user_premium = {}
user_daily_views = {}
content_packages = {}
known_chats = set()
current_photo = {}

# --- UTILIDADES --- #
def save_json(filename, data):
    with open(filename, "w", encoding="utf-8") as f:
        json.dump(data, f)

def load_json(filename):
    if not os.path.exists(filename):
        return {}
    with open(filename, "r", encoding="utf-8") as f:
        return json.load(f)

def save_data():
    save_json(USER_PREMIUM_FILE, {str(k): v.isoformat() for k, v in user_premium.items()})
    save_json(USER_VIEWS_FILE, user_daily_views)
    save_json(CONTENT_PACKAGES_FILE, content_packages)
    save_json(KNOWN_CHATS_FILE, list(known_chats))

def load_data():
    global user_premium, user_daily_views, content_packages, known_chats
    up = load_json(USER_PREMIUM_FILE)
    user_premium = {int(k): datetime.fromisoformat(v) for k, v in up.items()}
    user_daily_views = load_json(USER_VIEWS_FILE)
    content_packages = load_json(CONTENT_PACKAGES_FILE)
    known_chats = set(load_json(KNOWN_CHATS_FILE))

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
        [InlineKeyboardButton("🎧 Audio Libros", callback_data="audio_libros"),
         InlineKeyboardButton("📚 Libro PDF", callback_data="libro_pdf")],
        [InlineKeyboardButton("💬 Chat Pedido", callback_data="chat_pedido"),
         InlineKeyboardButton("🎓 Cursos", callback_data="cursos")],
        [InlineKeyboardButton("ℹ️ Info", callback_data="info"),
         InlineKeyboardButton("❓ Ayuda", callback_data="ayuda")]
    ])

def get_plan_menu_text(user_id):
    if is_premium(user_id):
        plan_actual = "Premium (ilimitado)"
    else:
        plan_actual = "Free (3 videos/día, sin reenvíos)"

    texto = (
        "💎 *Planes de suscripción*\n\n"
        f"Tu plan actual: *{plan_actual}*\n\n"
        "Elige uno de los siguientes planes:"
    )
    return texto

def get_plan_pro_text():
    return (
        "🔹 *Plan Pro*\n\n"
        "Precio: 40 estrellas\n"
        "Duración: 30 días\n"
        "Beneficios:\n"
        "- 50 videos diarios\n"
        "- Sin reenvíos ni compartir\n\n"
        "✖️ Reenvíos\n"
        "✖️ Compartir"
    )

def get_plan_ultra_text():
    return (
        "🔹 *Plan Ultra*\n\n"
        "Precio: 100 estrellas\n"
        "Duración: 30 días\n"
        "Beneficios:\n"
        "- Videos ilimitados\n"
        "- Reenvíos ilimitados ✅\n"
        "- Compartir ✅"
    )

# --- HANDLERS --- #
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
                        "🔒 Para ver este contenido debes unirte a los canales.",
                        reply_markup=InlineKeyboardMarkup([
                            [InlineKeyboardButton("🔗 Unirse a Supertv", url=f"https://t.me/{CHANNELS['supertvw2'][1:]}")],
                            [InlineKeyboardButton("🔗 Unirse a fullvvd", url=f"https://t.me/{CHANNELS['fullvvd'][1:]}")],
                            [InlineKeyboardButton("✅ Verificar suscripción", callback_data="verify")]
                        ])
                    )
                    return
            except Exception as e:
                logger.warning(f"Error verificando canal: {e}")
                await update.message.reply_text("❌ Error al verificar canales. Intenta más tarde.")
                return

        if can_view_video(user_id):
            register_view(user_id)
            await update.message.reply_video(
                video=pkg["video_id"],
                caption="🎬 Aquí tienes el video completo.",
                protect_content=not is_premium(user_id)  # Premium pueden reenviar
            )
        else:
            texto_planes = get_plan_menu_text(user_id)
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("🔹 Plan Pro", callback_data="plan_pro")],
                [InlineKeyboardButton("🔹 Plan Ultra", callback_data="plan_ultra")],
                [InlineKeyboardButton("🔙 Volver", callback_data="planes_volver")]
            ])
            await update.message.reply_text(
                f"🚫 Has alcanzado tu límite diario de {FREE_LIMIT_VIDEOS} videos.\n"
                "💎 Compra un plan para seguir viendo:",
                reply_markup=keyboard
            )
    else:
        await update.message.reply_text(
            "👋 ¡Hola! Para acceder al contenido exclusivo debes unirte a los canales y verificar.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔗 Unirse a Supertv", url=f"https://t.me/{CHANNELS['supertvw2'][1:]}")],
                [InlineKeyboardButton("🔗 Unirse a fullvvd", url=f"https://t.me/{CHANNELS['fullvvd'][1:]}")],
                [InlineKeyboardButton("✅ Verificar suscripción", callback_data="verify")]
            ])
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
        except:
            not_joined.append(username)
    if not not_joined:
        await query.edit_message_text("✅ Verificación completada. Menú disponible:")
        await query.message.reply_text("📋 Menú principal:", reply_markup=get_main_menu())
    else:
        await query.edit_message_text("❌ Aún no estás suscrito a:\n" + "\n".join(not_joined))

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = query.from_user
    user_id = user.id
    data = query.data

    if data == "planes":
        texto_planes = get_plan_menu_text(user_id)
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔹 Plan Pro", callback_data="plan_pro")],
            [InlineKeyboardButton("🔹 Plan Ultra", callback_data="plan_ultra")],
            [InlineKeyboardButton("🔙 Volver", callback_data="planes_volver")]
        ])
        await query.message.reply_text(texto_planes, parse_mode="Markdown", reply_markup=keyboard)

    elif data == "planes_volver":
        await query.message.reply_text("📋 Menú principal:", reply_markup=get_main_menu())

    elif data == "plan_pro":
        texto = get_plan_pro_text()
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("💸 Pagar 40 estrellas", callback_data="comprar_plan_pro")],
            [InlineKeyboardButton("🔙 Volver", callback_data="planes")]
        ])
        await query.message.reply_text(texto, parse_mode="Markdown", reply_markup=keyboard)

    elif data == "plan_ultra":
        texto = get_plan_ultra_text()
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("💸 Pagar 100 estrellas", callback_data="comprar_plan_ultra")],
            [InlineKeyboardButton("🔙 Volver", callback_data="planes")]
        ])
        await query.message.reply_text(texto, parse_mode="Markdown", reply_markup=keyboard)

    elif data == "comprar_plan_pro":
        if is_premium(user_id):
            exp = user_premium[user_id].strftime("%Y-%m-%d")
            await query.message.reply_text(f"✅ Ya tienes una suscripción activa hasta {exp}.")
            return
        await context.bot.send_invoice(
            chat_id=query.message.chat_id,
            title=PLAN_PRO_ITEM["title"],
            description=PLAN_PRO_ITEM["description"],
            payload=PLAN_PRO_ITEM["payload"],
            provider_token=PROVIDER_TOKEN,
            currency=PLAN_PRO_ITEM["currency"],
            prices=PLAN_PRO_ITEM["prices"],
            start_parameter="buy-plan-pro"
        )

    elif data == "comprar_plan_ultra":
        if is_premium(user_id):
            exp = user_premium[user_id].strftime("%Y-%m-%d")
            await query.message.reply_text(f"✅ Ya tienes una suscripción activa hasta {exp}.")
            return
        await context.bot.send_invoice(
            chat_id=query.message.chat_id,
            title=PLAN_ULTRA_ITEM["title"],
            description=PLAN_ULTRA_ITEM["description"],
            payload=PLAN_ULTRA_ITEM["payload"],
            provider_token=PROVIDER_TOKEN,
            currency=PLAN_ULTRA_ITEM["currency"],
            prices=PLAN_ULTRA_ITEM["prices"],
            start_parameter="buy-plan-ultra"
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

    elif data == "perfil":
        plan = "Premium" if is_premium(user_id) else "Free"
        exp = user_premium.get(user_id)
        await query.message.reply_text(
            f"🧑 Perfil:\n• {user.full_name}\n• @{user.username or 'Sin usuario'}\n"
            f"• ID: {user_id}\n• Plan: {plan}\n• Expira: {exp.strftime('%Y-%m-%d') if exp else 'N/A'}",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Volver", callback_data="planes")]])
        )

    # Botones para los nuevos menús del main
    elif data == "audio_libros":
        await query.message.reply_text("🎧 Aquí están los Audio Libros disponibles. (Implementa contenido o botones aquí.)",
                                       reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Volver", callback_data="planes")]]))
    elif data == "libro_pdf":
        await query.message.reply_text("📚 Aquí están los Libros PDF disponibles. (Implementa contenido o botones aquí.)",
                                       reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Volver", callback_data="planes")]]))
    elif data == "chat_pedido":
        await query.message.reply_text("💬 Chat para hacer pedidos o consultas. (Implementa aquí.)",
                                       reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Volver", callback_data="planes")]]))
    elif data == "cursos":
        await query.message.reply_text("🎓 Cursos disponibles. (Implementa contenido o botones aquí.)",
                                       reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Volver", callback_data="planes")]]))

async def precheckout_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.pre_checkout_query.answer(ok=True)

async def successful_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    payload = update.message.successful_payment.invoice_payload
    if payload in [PREMIUM_ITEM["payload"], PLAN_PRO_ITEM["payload"], PLAN_ULTRA_ITEM["payload"]]:
        user_premium[user_id] = datetime.utcnow() + timedelta(days=30)
        save_data()
        await update.message.reply_text("🎉 ¡Gracias por tu compra! Suscripción activada por 30 días.")

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
        await msg.reply_text("❌ Envía una imagen con sinopsis.")

async def recibir_video(update: Update, context: ContextTypes.DEFAULT_TYPE):

