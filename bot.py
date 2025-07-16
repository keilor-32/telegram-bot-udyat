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

os.remove(temp_path)  # Eliminar archivo temporal para limpieza

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

# Series con temporadas y capítulos
series_data = {}           # {serie_id: {"title", "photo_id", "caption", "temporadas": {T1: [video_id, ...], ...}}}
current_series = {}        # {user_id: {"title", "photo_id", "caption", "serie_id", "temporada_activa", ...}}

# --- Firestore colecciones ---
COLLECTION_USERS = "users_premium"
COLLECTION_VIDEOS = "videos"
COLLECTION_VIEWS = "user_daily_views"
COLLECTION_CHATS = "known_chats"
COLLECTION_SERIES = "series_data"

# --- Funciones Firestore ---
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
    logger.info(f"Cargados {len(user_premium)} usuarios premium.")
    logger.info(f"Cargados {len(content_packages)} paquetes de contenido.")
    logger.info(f"Cargados {len(user_daily_views)} datos de vistas diarias.")
    logger.info(f"Cargados {len(known_chats)} chats conocidos.")
    logger.info(f"Cargadas {len(series_data)} series.")

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
    global user_premium
    return user_id in user_premium and user_premium[user_id] > datetime.utcnow()

def can_view_video(user_id):
    if is_premium(user_id):
        return True
    today = str(datetime.utcnow().date())
    return user_daily_views.get(str(user_id), {}).get(today, 0) < FREE_LIMIT_VIDEOS

async def register_view(user_id):
    global user_daily_views
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

# --- Handlers ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    user_id = update.effective_user.id

    if args and args[0].startswith("video_"):
        pkg_id = args[0].split("_")[1]
        pkg = content_packages.get(pkg_id)
        if not pkg or "video_id" not in pkg:
            await update.message.reply_text("❌ Video no disponible.")
            return

        # Verifica suscripción a canales
        for name, username in CHANNELS.items():
            try:
                member = await context.bot.get_chat_member(chat_id=username, user_id=user_id)
                if member.status not in ["member", "administrator", "creator"]:
                    await update.message.reply_text(
                        "🔒 Para ver este contenido debes unirte a los canales.",
                        reply_markup=InlineKeyboardMarkup(
                            [
                                [InlineKeyboardButton("🔗 Unirse a Supertv", url=f"https://t.me/{CHANNELS['supertvw2'][1:]}")],
                                [InlineKeyboardButton("🔗 Unirse a fullvvd", url=f"https://t.me/{CHANNELS['fullvvd'][1:]}")],
                                [InlineKeyboardButton("✅ Verificar suscripción", callback_data="verify")],
                            ]
                        ),
                    )
                    return
            except Exception as e:
                logger.warning(f"Error verificando canal: {e}")
                await update.message.reply_text("❌ Error al verificar canales. Intenta más tarde.")
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
        await update.message.reply_text(
            "👋 ¡Hola! Para acceder al contenido exclusivo debes unirte a los canales y verificar.",
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton("🔗 Unirse a Supertv", url=f"https://t.me/{CHANNELS['supertvw2'][1:]}"),
                        InlineKeyboardButton("🔗 Unirse a fullvvd", url=f"https://t.me/{CHANNELS['fullvvd'][1:]}"),
                    ],
                    [InlineKeyboardButton("✅ Verificar suscripción", callback_data="verify")],
                    [InlineKeyboardButton("📋 Menú Principal", callback_data="menu")],
                ]
            ),
        )

async def callback_query_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    user_id = query.from_user.id

    if data == "menu":
        await query.edit_message_text("Menú principal:", reply_markup=get_main_menu())

    elif data == "planes":
        teclado_planes = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("Plan Pro (40 estrellas)", callback_data="comprar_plan_pro")],
                [InlineKeyboardButton("Plan Ultra (100 estrellas)", callback_data="comprar_plan_ultra")],
                [InlineKeyboardButton("Volver", callback_data="menu")],
            ]
        )
        await query.edit_message_text(
            "💎 Planes disponibles:\n\n"
            "🔹 Plan Pro: 50 videos diarios, sin reenvíos ni compartir (40 estrellas).\n"
            "🔹 Plan Ultra: acceso ilimitado y reenvíos ilimitados (100 estrellas).",
            reply_markup=teclado_planes,
        )
    elif data == "comprar_plan_pro":
        await context.bot.send_invoice(
            chat_id=user_id,
            title=PLAN_PRO_ITEM["title"],
            description=PLAN_PRO_ITEM["description"],
            payload=PLAN_PRO_ITEM["payload"],
            provider_token=PROVIDER_TOKEN,
            currency=PLAN_PRO_ITEM["currency"],
            prices=PLAN_PRO_ITEM["prices"],
        )
    elif data == "comprar_plan_ultra":
        await context.bot.send_invoice(
            chat_id=user_id,
            title=PLAN_ULTRA_ITEM["title"],
            description=PLAN_ULTRA_ITEM["description"],
            payload=PLAN_ULTRA_ITEM["payload"],
            provider_token=PROVIDER_TOKEN,
            currency=PLAN_ULTRA_ITEM["currency"],
            prices=PLAN_ULTRA_ITEM["prices"],
        )
    elif data.startswith("ver_"):  # Ejemplo: ver_serieId_T1
        parts = data.split("_", 2)
        if len(parts) == 3:
            serie_id, temporada = parts[1], parts[2]
            serie = series_data.get(serie_id)
            if serie:
                capitulos = serie.get("temporadas", {}).get(temporada, [])
                if capitulos:
                    botones = []
                    for idx, video_id in enumerate(capitulos, 1):
                        botones.append(
                            [InlineKeyboardButton(f"Capítulo {idx}", callback_data=f"cap_{serie_id}_{temporada}_{idx-1}")]
                        )
                    await query.edit_message_text(
                        f"📺 {serie['title']} - Temporada {temporada[1:]}\nSelecciona un capítulo:",
                        reply_markup=InlineKeyboardMarkup(botones),
                    )
                else:
                    await query.edit_message_text("No hay capítulos en esta temporada.")
            else:
                await query.edit_message_text("Serie no encontrada.")
    elif data.startswith("cap_"):  # Ejemplo: cap_serieId_T1_0
        parts = data.split("_", 3)
        if len(parts) == 4:
            serie_id, temporada, cap_index_str = parts[1], parts[2], parts[3]
            serie = series_data.get(serie_id)
            if serie:
                capitulos = serie.get("temporadas", {}).get(temporada, [])
                try:
                    cap_index = int(cap_index_str)
                    video_id = capitulos[cap_index]
                except Exception:
                    await query.edit_message_text("Capítulo inválido.")
                    return
                if can_view_video(user_id):
                    await register_view(user_id)
                    await query.message.reply_video(
                        video=video_id, caption=f"🎬 {serie['title']} - Temporada {temporada[1:]}, Capítulo {cap_index+1}",
                        protect_content=not is_premium(user_id)
                    )
                else:
                    await query.edit_message_text(
                        f"🚫 Límite diario alcanzado. Compra un plan para continuar.",
                        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("💎 Comprar Planes", callback_data="planes")]])
                    )
            else:
                await query.edit_message_text("Serie no encontrada.")
    elif data == "verify":
        await query.edit_message_text("✅ Verificación exitosa. Ahora puedes acceder al contenido.")
    else:
        await query.edit_message_text("Comando no reconocido.")

async def precheckout_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.pre_checkout_query
    await query.answer(ok=True)

async def successful_payment_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    payload = update.message.successful_payment.invoice_payload

    expire_date = datetime.utcnow() + timedelta(days=30)
    user_premium[user_id] = expire_date
    save_data()

    await update.message.reply_text(
        f"🎉 Gracias por tu compra! Tu acceso premium estará activo hasta {expire_date.strftime('%Y-%m-%d %H:%M:%S')} UTC."
    )

async def unknown_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❓ Comando no reconocido. Usa /start para comenzar.")

# --- Aplicación Telegram ---
app_telegram = Application.builder().token(TOKEN).build()

app_telegram.add_handler(CommandHandler("start", start))
app_telegram.add_handler(CallbackQueryHandler(callback_query_handler))
app_telegram.add_handler(PreCheckoutQueryHandler(precheckout_handler))
app_telegram.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment_handler))
app_telegram.add_handler(MessageHandler(filters.COMMAND, unknown_handler))

# --- Webhook/Aiohttp ---
web_app = web.Application()

async def telegram_webhook(request):
    data = await request.json()
    update = Update.de_json(data, app_telegram.bot)
    await app_telegram.update_queue.put(update)
    return web.Response(text="OK")

web_app.router.add_post(f"/{TOKEN}", telegram_webhook)

async def main():
    load_data()
    logger.info("🤖 Bot iniciado con webhook")

    await app_telegram.initialize()
    await app_telegram.start()

    runner = web.AppRunner(web_app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    logger.info(f"🌐 Webhook corriendo en puerto {PORT}")

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

