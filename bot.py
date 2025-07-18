import os
import json
import tempfile
import logging
import asyncio
from datetime import datetime, timedelta
from aiohttp import web
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LabeledPrice,
    InputMediaVideo,
)
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    PreCheckoutQueryHandler,
    filters,
)

import firebase_admin
from firebase_admin import credentials, firestore

# --- Inicializar Firestore con variable de entorno JSON ---
google_credentials_raw = os.getenv("GOOGLE_APPLICATION_CREDENTIALS_JSON")
if not google_credentials_raw:
    raise ValueError("❌ La variable GOOGLE_APPLICATION_CREDENTIALS_JSON no está configurada.")

# La variable puede estar doblemente serializada
google_credentials_str = json.loads(google_credentials_raw)
google_credentials_dict = json.loads(google_credentials_str)

with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as temp:
    json.dump(google_credentials_dict, temp)
    temp_path = temp.name

cred = credentials.Certificate(temp_path)
firebase_admin.initialize_app(cred)
db = firestore.client()

print("✅ Firestore inicializado correctamente.")

# --- Configuración ---
TOKEN = os.getenv("TOKEN")
PROVIDER_TOKEN = os.getenv("PROVIDER_TOKEN", "")  # Token de pagos Telegram Stars
APP_URL = os.getenv("APP_URL")  # URL para webhook si usas webhook
PORT = int(os.getenv("PORT", "8080"))

if not TOKEN:
    raise ValueError("❌ La variable TOKEN no está configurada.")
if not APP_URL:
    raise ValueError("❌ La variable APP_URL no está configurada.")

# --- Logging ---
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- Variables en memoria ---
user_premium = {}          # {user_id: expire_at datetime}
user_daily_views = {}      # {str(user_id): {date_str: count}}
content_packages = {}      # {pkg_id: {photo_id, caption, video_id}}
known_chats = set()
series_data = {}           # {serie_id: {title, photo_id, caption, temporadas: {T1: [videos], ...}}}

# --- Firestore colecciones ---
COLLECTION_USERS = "users_premium"
COLLECTION_VIDEOS = "videos"
COLLECTION_VIEWS = "user_daily_views"
COLLECTION_CHATS = "known_chats"
COLLECTION_SERIES = "series_data"

# --- Funciones para Firestore ---
def save_user_premium_firestore():
    batch = db.batch()
    for uid, exp in user_premium.items():
        doc_ref = db.collection(COLLECTION_USERS).document(str(uid))
        batch.set(doc_ref, {"expire_at": exp.isoformat()})
    batch.commit()

def load_user_premium_firestore():
    docs = db.collection(COLLECTION_USERS).stream()
    result = {}
    for doc in docs:
        data = doc.to_dict()
        try:
            expire_at = datetime.fromisoformat(data.get("expire_at"))
            result[int(doc.id)] = expire_at
        except Exception:
            pass
    return result

def save_videos_firestore():
    batch = db.batch()
    for pkg_id, content in content_packages.items():
        doc_ref = db.collection(COLLECTION_VIDEOS).document(pkg_id)
        batch.set(doc_ref, content)
    batch.commit()

def load_videos_firestore():
    docs = db.collection(COLLECTION_VIDEOS).stream()
    result = {}
    for doc in docs:
        result[doc.id] = doc.to_dict()
    return result

def save_user_daily_views_firestore():
    batch = db.batch()
    for uid, views in user_daily_views.items():
        doc_ref = db.collection(COLLECTION_VIEWS).document(uid)
        batch.set(doc_ref, views)
    batch.commit()

def load_user_daily_views_firestore():
    docs = db.collection(COLLECTION_VIEWS).stream()
    result = {}
    for doc in docs:
        result[doc.id] = doc.to_dict()
    return result

def save_known_chats_firestore():
    doc_ref = db.collection(COLLECTION_CHATS).document("chats")
    doc_ref.set({"chat_ids": list(known_chats)})

def load_known_chats_firestore():
    doc_ref = db.collection(COLLECTION_CHATS).document("chats")
    doc = doc_ref.get()
    if doc.exists:
        data = doc.to_dict()
        return set(data.get("chat_ids", []))
    return set()

def save_series_firestore():
    batch = db.batch()
    for serie_id, serie in series_data.items():
        doc_ref = db.collection(COLLECTION_SERIES).document(serie_id)
        batch.set(doc_ref, serie)
    batch.commit()

def load_series_firestore():
    docs = db.collection(COLLECTION_SERIES).stream()
    result = {}
    for doc in docs:
        result[doc.id] = doc.to_dict()
    return result

def save_data():
    save_user_premium_firestore()
    save_videos_firestore()
    save_user_daily_views_firestore()
    save_known_chats_firestore()
    save_series_firestore()

def load_data():
    global user_premium, content_packages, user_daily_views, known_chats, series_data
    user_premium = load_user_premium_firestore()
    content_packages = load_videos_firestore()
    user_daily_views = load_user_daily_views_firestore()
    known_chats = load_known_chats_firestore()
    series_data = load_series_firestore()

# --- Parámetros y planes ---
FREE_LIMIT_VIDEOS = 3

PREMIUM_ITEM = {
    "title": "Plan Premium",
    "description": "Acceso y reenvíos ilimitados por 30 días.",
    "payload": "premium_plan",
    "currency": "XTR",
    "prices": [LabeledPrice("Premium por 30 días", 1)],
}

# --- Canales para verificar ---
CHANNELS = {
    "supertvw2": "@Supertvw2",
    "fullvvd": "@fullvvd",
}

# --- Funciones auxiliares ---
def is_premium(user_id):
    return user_id in user_premium and user_premium[user_id] > datetime.utcnow()

def can_view_video(user_id):
    if is_premium(user_id):
        return True
    today = str(datetime.utcnow().date())
    return user_daily_views.get(str(user_id), {}).get(today, 0) < FREE_LIMIT_VIDEOS

async def register_view(user_id):
    today = str(datetime.utcnow().date())
    uid = str(user_id)
    if uid not in user_daily_views:
        user_daily_views[uid] = {}
    user_daily_views[uid][today] = user_daily_views[uid].get(today, 0) + 1
    save_user_daily_views_firestore()

# --- Menú principal ---
def get_main_menu():
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("🎧 Audio Libros", callback_data="audio_libros"),
                InlineKeyboardButton("📚 Libro PDF", callback_data="libro_pdf"),
            ],
            [
                InlineKeyboardButton("💬 Chat Pedido", callback_data="chat_pedido"),
                InlineKeyboardButton("🎓 Cursos", callback_data="cursos"),
            ],
            [
                InlineKeyboardButton("📢 Canal", url="https://t.me/hsitotv"),
                InlineKeyboardButton("👥 Grupo", url="https://t.me/udyat_channel"),
            ],
            [
                InlineKeyboardButton("💎 Planes", callback_data="planes"),
                InlineKeyboardButton("🧑 Perfil", callback_data="perfil"),
            ],
            [
                InlineKeyboardButton("ℹ️ Info", callback_data="info"),
                InlineKeyboardButton("❓ Ayuda", callback_data="ayuda"),
            ],
        ]
    )

# --- Handlers ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    # Verificar si está suscripto a canales, si no mostrar botones de suscripción
    not_joined = []
    for name, username in CHANNELS.items():
        try:
            member = await context.bot.get_chat_member(chat_id=username, user_id=user_id)
            if member.status not in ["member", "administrator", "creator"]:
                not_joined.append(username)
        except Exception as e:
            logger.warning(f"Error verificando canal {username}: {e}")
            not_joined.append(username)

    if not_joined:
        await update.message.reply_text(
            "🔒 Para acceder debes unirte a los canales y verificar:",
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton("🔗 Unirse a Supertv", url=f"https://t.me/{CHANNELS['supertvw2'][1:]}"),
                        InlineKeyboardButton("🔗 Unirse a fullvvd", url=f"https://t.me/{CHANNELS['fullvvd'][1:]}"),
                    ],
                    [InlineKeyboardButton("✅ Verificar suscripción", callback_data="verify")],
                ]
            ),
        )
        return

    # Si ya verificado, mostrar menú principal directo
    await update.message.reply_text("📋 Menú principal:", reply_markup=get_main_menu())

async def verify(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    not_joined = []
    for name, username in CHANNELS.items():
        try:
            member = await context.bot.get_chat_member(chat_id=username, user_id=user_id)
            if member.status not in ["member", "administrator", "creator"]:
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
    data = query.data
    user_id = query.from_user.id

    if data == "planes":
        texto_planes = (
            f"💎 *Planes disponibles:*\n\n"
            f"🔹 Free – Hasta {FREE_LIMIT_VIDEOS} videos por día.\n\n"
            "🔸 *Plan Premium*\n"
            "Precio: 1 estrella\n"
            "Beneficios: Acceso y reenvíos ilimitados por 30 días.\n\n"
        )
        botones_planes = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("💸 Comprar Plan Premium (1 ⭐)", callback_data="comprar_premium")],
                [InlineKeyboardButton("🔙 Volver", callback_data="menu_principal")],
            ]
        )
        await query.message.reply_text(texto_planes, parse_mode="Markdown", reply_markup=botones_planes)

    elif data == "comprar_premium":
        if is_premium(user_id):
            exp = user_premium[user_id].strftime("%Y-%m-%d")
            await query.message.reply_text(f"✅ Ya tienes un plan activo hasta {exp}.")
            return
        await context.bot.send_invoice(
            chat_id=query.message.chat_id,
            title=PREMIUM_ITEM["title"],
            description=PREMIUM_ITEM["description"],
            payload=PREMIUM_ITEM["payload"],
            provider_token=PROVIDER_TOKEN,
            currency=PREMIUM_ITEM["currency"],
            prices=PREMIUM_ITEM["prices"],
            start_parameter="buy-premium",
        )

    elif data == "menu_principal":
        await query.message.reply_text("📋 Menú principal:", reply_markup=get_main_menu())

    else:
        await query.message.reply_text("Función en desarrollo o no implementada.")

async def precheckout_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.pre_checkout_query
    await query.answer(ok=True)

async def successful_payment_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    payload = update.message.successful_payment.invoice_payload

    if payload == "premium_plan":
        expire_date = datetime.utcnow() + timedelta(days=30)
        user_premium[user_id] = expire_date
        save_user_premium_firestore()
        await update.message.reply_text(f"🎉 ¡Pago recibido! Tu acceso Premium está activo hasta {expire_date.strftime('%Y-%m-%d')}.")
    else:
        await update.message.reply_text("Pago recibido, pero no se reconoce el plan.")

async def unknown_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Comando no reconocido. Usa /start para comenzar.")

# --- Función main para iniciar el bot ---
async def main():
    load_data()
    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(verify, pattern="^verify$"))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(PreCheckoutQueryHandler(precheckout_callback))
    app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment_callback))
    app.add_handler(MessageHandler(filters.COMMAND, unknown_command))

    print("🤖 Bot iniciado...")

    # Usa polling (ideal para desarrollo o Render, cambiar si usas webhook)
    await app.run_polling()

if __name__ == "__main__":
    asyncio.run(main())
