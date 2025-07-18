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

# --- Inicializar Firestore con variable de entorno JSON doblemente serializada ---
google_credentials_raw = os.getenv("GOOGLE_APPLICATION_CREDENTIALS_JSON")
if not google_credentials_raw:
    raise ValueError("❌ La variable GOOGLE_APPLICATION_CREDENTIALS_JSON no está configurada.")

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
PROVIDER_TOKEN = os.getenv("PROVIDER_TOKEN", "")
APP_URL = os.getenv("APP_URL")
PORT = int(os.getenv("PORT", "8080"))

if not TOKEN:
    raise ValueError("❌ ERROR: La variable de entorno TOKEN no está configurada.")
if not APP_URL:
    raise ValueError("❌ ERROR: La variable de entorno APP_URL no está configurada.")

# --- Logging ---
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- Variables en memoria ---
user_premium = {}          # {user_id: expire_at datetime}
user_daily_views = {}      # {user_id: {date: count}}
content_packages = {}      # {pkg_id: {photo_id, caption, video_id}}
known_chats = set()
current_photo = {}

# NUEVO: series con temporadas y capítulos
series_data = {}           # {serie_id: {"title", "photo_id", "caption", "temporadas": {T1: [video_id, ...], ...}}}
current_series = {}        # {user_id: {"title", "photo_id", "caption", "serie_id", "temporada", "capitulos": []}}

# --- Firestore colecciones ---
COLLECTION_USERS = "users_premium"
COLLECTION_VIDEOS = "videos"
COLLECTION_VIEWS = "user_daily_views"
COLLECTION_CHATS = "known_chats"
COLLECTION_SERIES = "series_data"   # NUEVO para series

# --- Funciones Firestore (Síncronas) ---
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

# NUEVO: Guardar y cargar series
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

# --- Guardar y cargar todo ---
def save_data():
    save_user_premium_firestore()
    save_videos_firestore()
    save_user_daily_views_firestore()
    save_known_chats_firestore()
    save_series_firestore()  # NUEVO

def load_data():
    global user_premium, content_packages, user_daily_views, known_chats, series_data
    user_premium = load_user_premium_firestore()
    content_packages = load_videos_firestore()
    user_daily_views = load_user_daily_views_firestore()
    known_chats = load_known_chats_firestore()
    series_data = load_series_firestore()  # NUEVO

# --- Planes ---
FREE_LIMIT_VIDEOS = 3

PREMIUM_ITEM = {
    "title": "Plan Premium",
    "description": "Acceso y reenvíos ilimitados por 30 días.",
    "payload": "premium_plan",
    "currency": "XTR",
    "prices": [LabeledPrice("Premium por 30 días", 1)],
}

PLAN_PRO_ITEM = {
    "title": "Plan Pro",
    "description": "50 videos diarios, sin reenvíos ni compartir.",
    "payload": "plan_pro",
    "currency": "XTR",
    "prices": [LabeledPrice("Plan Pro por 30 días", 40)],
}

PLAN_ULTRA_ITEM = {
    "title": "Plan Ultra",
    "description": "Videos y reenvíos ilimitados, sin restricciones.",
    "payload": "plan_ultra",
    "currency": "XTR",
    "prices": [LabeledPrice("Plan Ultra por 30 días", 1)],
}

# --- Control acceso ---
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
    save_data()

# --- Canales para verificación ---
CHANNELS = {
    "supertvw2": "@Supertvw2",
    "fullvvd": "@fullvvd",
}

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

# --- Handler /start corregido ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    logger.info(f"/start recibido de {user_id}")

    async def esta_verificado():
        for name, username in CHANNELS.items():
            try:
                member = await context.bot.get_chat_member(chat_id=username, user_id=user_id)
                if member.status not in ["member", "administrator", "creator"]:
                    return False
            except Exception as e:
                logger.error(f"Error verificando canal {username} para user {user_id}: {e}")
                return False
        return True

    verificado = await esta_verificado()

    args = context.args if update.message and update.message.text else []

    if verificado:
        if args and args[0].startswith("video_"):
            pkg_id = args[0].split("_", 1)[1]
            pkg = content_packages.get(pkg_id)
            if not pkg or "video_id" not in pkg:
                await update.message.reply_text("❌ Video no disponible.")
                return

            if can_view_video(user_id):
                await register_view(user_id)
                await update.message.reply_video(
                    video=pkg["video_id"], caption="🎬 Aquí tienes el video completo.", protect_content=not is_premium(user_id)
                )
            else:
                await update.message.reply_text(
                    f"🚫 Has alcanzado tu límite diario de {FREE_LIMIT_VIDEOS} videos.\n"
                    "💎 Compra un plan para más acceso y reenvíos ilimitados.",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("💎 Comprar Planes", callback_data="planes")]]),
                )
                return

        elif args and args[0].startswith("serie_"):
            serie_id = args[0].split("_", 1)[1]
            serie = series_data.get(serie_id)
            if not serie:
                await update.message.reply_text("❌ Serie no encontrada.")
                return
            botones = []
            for temporada in serie.get("temporadas", {}).keys():
                botones.append(
                    [InlineKeyboardButton(f"Temporada {temporada[1:]}", callback_data=f"ver_{serie_id}_{temporada}")]
                )
            await update.message.reply_text(
                f"📺 {serie['title']}\n\n{serie['caption']}",
                reply_markup=InlineKeyboardMarkup(botones),
                disable_web_page_preview=True,
            )
        else:
            await update.message.reply_text("📋 Menú principal:", reply_markup=get_main_menu())
    else:
        await update.message.reply_text(
            "👋 ¡Hola! Para acceder al contenido exclusivo debes unirte a los canales y verificar.",
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
    user = query.from_user
    user_id = user.id
    data = query.data

    if data == "planes":
        texto_planes = (
            f"💎 *Planes disponibles:*\n\n"
            f"🔹 Free – Hasta {FREE_LIMIT_VIDEOS} videos por día.\n\n"
            "🔸 *Plan Pro*\n"
            "Precio: 40 estrellas\n"
            "Beneficios: 50 videos diarios, sin reenvíos ni compartir.\n\n"
            "🔸 *Plan Ultra*\n"
            "Precio: 100 estrellas\n"
            "Beneficios: Videos y reenvíos ilimitados, sin restricciones.\n"
        )
        botones_planes = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("💸 Comprar Plan Pro (40 ⭐)", callback_data="comprar_pro")],
                [InlineKeyboardButton("💸 Comprar Plan Ultra (100 ⭐)", callback_data="comprar_ultra")],
                [InlineKeyboardButton("🔙 Volver", callback_data="menu_principal")],
            ]
        )
        await query.message.reply_text(texto_planes, parse_mode="Markdown", reply_markup=botones_planes)

    elif data == "comprar_pro":
        if is_premium(user_id):
            exp = user_premium[user_id].strftime("%Y-%m-%d")
            await query.message.reply_text(f"✅ Ya tienes un plan activo hasta {exp}.")
            return
        await context.bot.send_invoice(
            chat_id=query.message.chat_id,
            title=PLAN_PRO_ITEM["title"],
            description=PLAN_PRO_ITEM["description"],
            payload=PLAN_PRO_ITEM["payload"],
            provider_token=PROVIDER_TOKEN,
            currency=PLAN_PRO_ITEM["currency"],
            prices=PLAN_PRO_ITEM["prices"],
            start_parameter="buy-plan-pro",
        )

    elif data == "comprar_ultra":
        if is_premium(user_id):
            exp = user_premium[user_id].strftime("%Y-%m-%d")
            await query.message.reply_text(f"✅ Ya tienes un plan activo hasta {exp}.")
            return
        await context.bot.send_invoice(
            chat_id=query.message.chat_id,
            title=PLAN_ULTRA_ITEM["title"],
            description=PLAN_ULTRA_ITEM["description"],
            payload=PLAN_ULTRA_ITEM["payload"],
            provider_token=PROVIDER_TOKEN,
            currency=PLAN_ULTRA_ITEM["currency"],
            prices=PLAN_ULTRA_ITEM["prices"],
            start_parameter="buy-plan-ultra",
        )

    elif data == "perfil":
        plan = "Premium" if is_premium(user_id) else "Free"
        exp = user_premium.get(user_id)
        await query.message.reply_text(
            f"🧑 Perfil:\n• {user.full_name}\n• @{user.username or 'Sin usuario'}\n"
            f"• ID: {user_id}\n• Plan: {plan}\n• Expira: {exp.strftime('%Y-%m-%d') if exp else 'N/A'}",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Volver", callback_data="planes")]]),
        )

    elif data == "menu_principal":
        await query.message.reply_text("📋 Menú principal:", reply_markup=get_main_menu())

    elif data == "audio_libros":
        await query.message.reply_text("🎧 Aquí estará el contenido de Audio Libros.")
    elif data == "libro_pdf":
        await query.message.reply_text("📚 Aquí estará el contenido de Libro PDF.")
    elif data == "chat_pedido":
        await query.message.reply_text("💬 Aquí puedes hacer tu pedido en el chat.")
    elif data == "cursos":
        await query.message.reply_text("🎓 Aquí estarán los cursos disponibles.")

    # NUEVO: Mostrar capítulos de temporada
    elif data.startswith("ver_"):
        # formato ver_{serie_id}_{temporada}
        _, serie_id, temporada = data.split("_", 2)
        serie = series_data.get(serie_id)
        if not serie or temporada not in serie.get("temporadas", {}):
            await query.message.reply_text("❌ Temporada no disponible.")
            return
        botones = []
        for i, _ in enumerate(serie["temporadas"][temporada]):
            botones.append(
                [InlineKeyboardButton(f"▶️ Ver Capítulo {i+1}", callback_data=f"cap_{serie_id}_{temporada}_{i}")]
            )
        await query.edit_message_text(f"📺 Capítulos de Temporada {temporada[1:]}:", reply_markup=InlineKeyboardMarkup(botones))

    # NUEVO: Mostrar video capítulo con navegación
    elif data.startswith("cap_"):
        # formato cap_{serie_id}_{temporada}_{indice}
        _, serie_id, temporada, index = data.split("_")
        index = int(index)
        serie = series_data.get(serie_id)
        if not serie or temporada not in serie.get("temporadas", {}):
            await query.message.reply_text("❌ Capítulo no disponible.")
            return

        capitulos = serie["temporadas"][temporada]
        total = len(capitulos)
        if index < 0 or index >= total:
            await query.message.reply_text("❌ Capítulo fuera de rango.")
            return

        video_id = capitulos[index]

        # Botones para siguiente, anterior, volver temporada
        botones = []
        if index > 0:
            botones.append(InlineKeyboardButton("⬅️ Anterior", callback_data=f"cap_{serie_id}_{temporada}_{index-1}"))
        if index < total - 1:
            botones.append(InlineKeyboardButton("➡️ Siguiente", callback_data=f"cap_{serie_id}_{temporada}_{index+1}"))
        botones.append(InlineKeyboardButton("🔙 Volver Temporada", callback_data=f"ver_{serie_id}_{temporada}"))

        # Enviar video protegido o solo si el usuario puede
        if can_view_video(user_id):
            await register_view(user_id)
            await query.message.reply_video(video=video_id, caption=f"📺 Capítulo {index+1} de Temporada {temporada[1:]}", reply_markup=InlineKeyboardMarkup([botones]))
        else:
            await query.message.reply_text(
                f"🚫 Has alcanzado tu límite diario de {FREE_LIMIT_VIDEOS} videos.\n"
                "💎 Compra un plan para más acceso y reenvíos ilimitados.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("💎 Comprar Planes", callback_data="planes")]]),
            )

async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Aquí manejas mensajes de usuarios, como envío de videos o imágenes
    # Puedes extender esta función según tu lógica
    await update.message.reply_text("📩 Mensaje recibido. Usa /start para comenzar.")

async def precheckout_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.pre_checkout_query
    await query.answer(ok=True)

async def successful_payment_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    payload = update.message.successful_payment.invoice_payload
    logger.info(f"Pago exitoso de {user_id} para {payload}")

    # Asignar fecha de expiración según el plan comprado
    now = datetime.utcnow()
    expire_at = now + timedelta(days=30)
    user_premium[user_id] = expire_at
    save_data()

    await update.message.reply_text(
        f"✅ ¡Gracias por tu compra! Tu plan es válido hasta {expire_at.strftime('%Y-%m-%d')}."
    )

# --- Main ---
async def on_startup(app):
    load_data()
    logger.info("Datos cargados desde Firestore.")

async def main():
    application = Application.builder().token(TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CallbackQueryHandler(handle_callback))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler))
    application.add_handler(PreCheckoutQueryHandler(precheckout_handler))
    application.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment_handler))

  
async def main():
    load_data()
    logger.info("🤖 Bot iniciado con webhook")

    # Inicializar la app de Telegram
    await app_telegram.initialize()
    await app_telegram.start()

    # Iniciar el servidor aiohttp
    runner = web.AppRunner(web_app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    logger.info(f"🌐 Webhook corriendo en puerto {PORT}")

    # Mantener la app corriendo
    try:
        while True:
            await asyncio.sleep(3600)
    except (KeyboardInterrupt, SystemExit):
        logger.info("🛑 Deteniendo bot...")
    finally:
        await app_telegram.stop()
        await app_telegram.shutdown()
        await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())

