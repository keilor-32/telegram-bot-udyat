import os
import json
import tempfile
import logging
import asyncio
from datetime import datetime, timedelta, timezone
from aiohttp import web
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LabeledPrice,
    InputMediaVideo,
    InputMediaPhoto,
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
# MODIFICADO: Ahora user_premium guarda un diccionario {expire_at: datetime, plan_type: str}
user_premium = {}           # {user_id: {expire_at: datetime, plan_type: str}}
user_daily_views = {}      # {user_id: {date: count}}
content_packages = {}      # {pkg_id: {photo_id, caption, video_id}}
known_chats = set()
current_photo = {}
series_data = {}            # {serie_id: {"title", "photo_id", "caption", "capitulos": [video_id, ...], ...}}
current_series = {}        # {user_id: {"title", "photo_id", "caption", "serie_id", "capitulos": []}}

# --- Firestore colecciones ---
COLLECTION_USERS = "users_premium"
COLLECTION_VIDEOS = "videos"
COLLECTION_VIEWS = "user_daily_views"
COLLECTION_CHATS = "known_chats"
COLLECTION_SERIES = "series_data"

# --- Funciones Firestore (Síncronas) ---
def save_user_premium_firestore():
    batch = db.batch()
    for uid, data in user_premium.items(): # MODIFICADO: 'data' ahora es un dict
        doc_ref = db.collection(COLLECTION_USERS).document(str(uid))
        exp = data["expire_at"]
        if exp.tzinfo is None:
            batch.set(doc_ref, {"expire_at": exp.replace(tzinfo=timezone.utc).isoformat(), "plan_type": data["plan_type"]}) # MODIFICADO: Guardar plan_type
        else:
            batch.set(doc_ref, {"expire_at": exp.isoformat(), "plan_type": data["plan_type"]}) # MODIFICADO: Guardar plan_type
    batch.commit()

def load_user_premium_firestore():
    docs = db.collection(COLLECTION_USERS).stream()
    result = {}
    for doc in docs:
        data = doc.to_dict()
        try:
            expire_at_str = data.get("expire_at")
            plan_type = data.get("plan_type", "premium_legacy") # MODIFICADO: Cargar plan_type, default para compatibilidad
            if expire_at_str:
                expire_at = datetime.fromisoformat(expire_at_str)
                if expire_at.tzinfo is None:
                    expire_at = expire_at.replace(tzinfo=timezone.utc)
                result[int(doc.id)] = {"expire_at": expire_at, "plan_type": plan_type} # MODIFICADO: Guardar como dict
        except Exception as e:
            logger.error(f"Error al cargar fecha premium para {doc.id}: {e}")
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

# --- Guardar y cargar todo ---
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

# --- Planes ---
FREE_LIMIT_VIDEOS = 3
PRO_LIMIT_VIDEOS = 50
PLAN_PRO_ITEM = {
    "title": "Plan Pro",
    "description": "50 videos diarios, sin reenvíos ni compartir.",
    "payload": "plan_pro", # Usado como plan_type
    "currency": "XTR",
    "prices": [LabeledPrice("Plan Pro por 30 días", 25)],
}
PLAN_ULTRA_ITEM = {
    "title": "Plan Ultra",
    "description": "Videos y reenvíos ilimitados, sin restricciones.",
    "payload": "plan_ultra", # Usado como plan_type
    "currency": "XTR",
    "prices": [LabeledPrice("Plan Ultra por 30 días", 50)],
}

# --- Control acceso (MODIFICADO) ---
def is_premium(user_id):
    # Verifica si el usuario tiene CUALQUIER plan pago activo.
    if user_id in user_premium:
        user_plan_data = user_premium[user_id]
        if isinstance(user_plan_data, dict) and "expire_at" in user_plan_data:
            return user_plan_data["expire_at"] > datetime.now(timezone.utc)
        # Compatibilidad con versiones antiguas donde user_premium[user_id] era solo la fecha
        elif isinstance(user_plan_data, datetime):
            return user_plan_data > datetime.now(timezone.utc)
    return False

def get_user_plan_type(user_id):
    # Obtiene el tipo de plan actual del usuario.
    if is_premium(user_id):
        user_plan_data = user_premium[user_id]
        if isinstance(user_plan_data, dict) and "plan_type" in user_plan_data:
            return user_plan_data["plan_type"]
        # Compatibilidad: si es premium pero no tiene 'plan_type', asumir "premium_legacy" o "ultra"
        return "plan_ultra" # Asumir Ultra para planes antiguos sin tipo explícito
    return "free"

def can_resend_content(user_id):
    # SOLO el plan "ultra" (o "premium_legacy" para compatibilidad) permite reenviar.
    plan_type = get_user_plan_type(user_id)
    return plan_type == "plan_ultra" or plan_type == "premium_legacy"

def can_view_video(user_id):
    plan_type = get_user_plan_type(user_id)
    today = str(datetime.utcnow().date())
    current_views = user_daily_views.get(str(user_id), {}).get(today, 0)

    if plan_type == "plan_ultra" or plan_type == "premium_legacy":
        return True # Vistas ilimitadas
    elif plan_type == "plan_pro":
        return current_views < PRO_LIMIT_VIDEOS
    else: # plan_type == "free"
        return current_views < FREE_LIMIT_VIDEOS

async def register_view(user_id):
    today = str(datetime.utcnow().date())
    uid = str(user_id)
    if uid not in user_daily_views:
        user_daily_views[uid] = {}
    user_daily_views[uid][today] = user_daily_views[uid].get(today, 0) + 1
    save_data()

# --- Canales para verificación ---
CHANNELS = {
    "canal_1": "https://t.me/+rzFyi_cr_T1kNTAx",
    "canal_2": "@Jhonmaxs",
}

# --- Menú principal ---
def get_main_menu():
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("🎧 Audio Libros", url="https://t.me/+3lDaURwlx-g4NWJk"),
                InlineKeyboardButton("📚 Libro PDF", url="https://t.me/+iJ5D1VLCAW5hYzhk"),
            ],
            [
                InlineKeyboardButton("💬 Chat Pedido", url="https://t.me/+6eA7AdRfgq81NzBh"),
                InlineKeyboardButton("🎓 Cursos", url="https://t.me/clasesdigitales"),
            ],
            [
                InlineKeyboardButton("📽️ peliculas", url="https://t.me/+dVTzx8dMGf81NTcx"),
                ],
            [
                InlineKeyboardButton("🎬 series", url="https://t.me/+qiFtv2EmV-xmNWFh"),
            ],
            [
                InlineKeyboardButton("💎 Planes", callback_data="planes"),
                InlineKeyboardButton("🧑 Perfil", callback_data="perfil"),
            ],
            [
                InlineKeyboardButton("ℹ️ Info", callback_data="info"),
                InlineKeyboardButton("❓ soporte", url="https://t.me/Hsito"),
            ],
        ]
    )

# --- Función auxiliar para generar botones de capítulos en cuadrícula ---
def generate_chapter_buttons(serie_id, num_chapters, chapters_per_row=5):
    buttons = []
    row = []
    for i in range(num_chapters):
        row.append(InlineKeyboardButton(str(i + 1), callback_data=f"cap_{serie_id}_{i}"))
        if len(row) == chapters_per_row:
            buttons.append(row)
            row = []
    if row: # Añadir la última fila si no está completa
        buttons.append(row)
    
    # Añadir botón "Volver al menú principal" al final
    buttons.append([InlineKeyboardButton("🔙 Volver al menú principal", callback_data="menu_principal")])
    return InlineKeyboardMarkup(buttons)

# --- Handlers ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    user_id = update.effective_user.id
    bot_username = (await context.bot.get_me()).username

    # Manejo del start link para mostrar sinopsis + botón "Ver Video" (Videos individuales)
    if args and args[0].startswith("video_"):
        pkg_id = args[0].split("_")[1]
        pkg = content_packages.get(pkg_id)
        if not pkg:
            await update.message.reply_text("❌ Contenido no disponible.")
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
                                [
                                    InlineKeyboardButton(
                                        "🔗 Unirse a canal 1", url=f"https://t.me/{CHANNELS['canal_1'][1:]}"
                                    )
                                ],
                                [
                                    InlineKeyboardButton(
                                        "🔗 Unirse a canal 2", url=f"https://t.me/{CHANNELS['canal_2'][1:]}"
                                    )
                                ],
                                [InlineKeyboardButton("✅ Verificar suscripción", callback_data="verify")],
                            ]
                        ),
                    )
                    return
            except Exception as e:
                logger.warning(f"Error verificando canal: {e}")
                await update.message.reply_text("❌ Error al verificar canales. Intenta más tarde.")
                return

        # Mostrar sinopsis y botón "Ver Video"
        ver_video_button = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "▶️ Ver Video", callback_data=f"play_video_{pkg_id}" # Callback para cargar el video
                    )
                ]
            ]
        )
        await update.message.reply_text(
            f"🎬 **{pkg.get('caption', 'Contenido:')}**\n\nPresiona 'Ver Video' para iniciar la reproducción.",
            reply_markup=ver_video_button,
            parse_mode="Markdown"
        )
        return

    # Manejo del start link para reproducir video (Videos individuales)
    elif args and args[0].startswith("play_video_"):
        pkg_id = args[0].split("_")[2]
        pkg = content_packages.get(pkg_id)
        if not pkg or "video_id" not in pkg:
            await update.message.reply_text("❌ Video no disponible.")
            return

        # La verificación de canales ya se hizo en el paso 'video_' anterior,
        # pero para mayor seguridad o si el usuario llegó directamente aquí, se puede repetir.
        for name, username in CHANNELS.items():
            try:
                member = await context.bot.get_chat_member(chat_id=username, user_id=user_id)
                if member.status not in ["member", "administrator", "creator"]:
                    await update.message.reply_text(
                        "🔒 saludos debes unirte a todos nuestros canales para asi poder usar este bot una ves te hayas unido debes dar click en verificar suscripcion para con tinuar.",
                        reply_markup=InlineKeyboardMarkup(
                            [
                                [
                                    InlineKeyboardButton(
                                        "🔗 Unirse a canal 1", url=f"https://t.me/{CHANNELS['canal_1'][1:]}" # CORREGIDO
                                    )
                                ],
                                [
                                    InlineKeyboardButton(
                                        "🔗 Unirse a canal 2", url=f"https://t.me/{CHANNELS['canal_2'][1:]}"
                                    )
                                ],
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
            title_caption = pkg.get("caption", "🎬 Aquí tienes el video completo.")
            await update.message.reply_video(
                video=pkg["video_id"],
                caption=title_caption,
                protect_content=not can_resend_content(user_id)
            )
        else:
            await update.message.reply_text(
                f"🚫 Has alcanzado tu límite diario de {FREE_LIMIT_VIDEOS} videos.\n"
                "💎 Por favor, considera comprar un plan para acceso ilimitado.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("💎 Comprar Planes", callback_data="planes")]]),
            )
            return

    # Modificado: Manejo de argumentos para series (directo a capítulos)
    elif args and args[0].startswith("serie_"):
        serie_id = args[0].split("_", 1)[1]
        serie = series_data.get(serie_id)
        if not serie:
            await update.message.reply_text("❌ Serie no encontrada.")
            return

        # Verifica suscripción a canales (se mantiene para series)
        for name, username in CHANNELS.items():
            try:
                member = await context.bot.get_chat_member(chat_id=username, user_id=user_id)
                if member.status not in ["member", "administrator", "creator"]:
                    await update.message.reply_text(
                        "🔒 Para ver este contenido debes unirte a los canales.",
                        reply_markup=InlineKeyboardMarkup(
                            [
                                [
                                    InlineKeyboardButton(
                                        "🔗 Unirse a canal 1", url=f"https://t.me/{CHANNELS['canal_1'][1:]}"
                                    )
                                ],
                                [
                                    InlineKeyboardButton(
                                        "🔗 Unirse a canal 2", url=f"https://t.me/{CHANNELS['canal_2'][1:]}"
                                    )
                                ],
                                [InlineKeyboardButton("✅ Verificar suscripción", callback_data="verify")],
                            ]
                        ),
                    )
                    return
            except Exception as e:
                logger.warning(f"Error verificando canal: {e}")
                await update.message.reply_text("❌ Error al verificar canales. Intenta más tarde.")
                return

        # APLICACIÓN DE LA SEGURIDAD PARA SERIES AQUÍ
        if not can_view_video(user_id): # Verifica si tiene vistas disponibles
            await update.message.reply_text(
                f"🚫 Has alcanzado tu límite diario de {FREE_LIMIT_VIDEOS} vistas para series/videos.\n"
                "💎 Por favor, considera comprar un plan para acceso ilimitado.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("💎 Comprar Planes", callback_data="planes")]]),
            )
            return

        # Si puede ver, mostrar capítulos
        capitulos = serie.get("capitulos", [])
        if not capitulos:
            await update.message.reply_text("❌ Esta serie no tiene capítulos disponibles aún.")
            return
        
        # Usar la nueva función para generar los botones de los capítulos
        markup = generate_chapter_buttons(serie_id, len(capitulos))

        await update.message.reply_photo(
            photo=serie["photo_id"],
            caption=f"📺 *{serie['title']}*\n\n{serie['caption']}\n\nSelecciona un capítulo:",
            reply_markup=markup,
            parse_mode="Markdown"
        )
    else:
        await update.message.reply_text(
            "👋 ¡Hola! primero debes unirte a todos nuestros canales para usar este bot una ves te hayas unido haz click en verificar suscripcion para continuar.",
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton("🔗 Unirse a canal 1", url=f"https://t.me/{CHANNELS['canal_1'][1:]}"),
                        InlineKeyboardButton("🔗 Unirse a canal 2", url=f"https://t.me/{CHANNELS['canal_2'][1:]}"),
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
        except Exception:
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
            exp_date = user_premium[user_id].get("expire_at", datetime.now(timezone.utc)).strftime("%Y-%m-%d") # MODIFICADO
            await query.message.reply_text(f"✅ Ya tienes un plan activo hasta {exp_date}.")
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
            exp_date = user_premium[user_id].get("expire_at", datetime.now(timezone.utc)).strftime("%Y-%m-%d") # MODIFICADO
            await query.message.reply_text(f"✅ Ya tienes un plan activo hasta {exp_date}.")
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
        plan_type = get_user_plan_type(user_id)
        exp_date_str = "N/A"
        if is_premium(user_id):
            exp_date = user_premium[user_id].get("expire_at")
            if exp_date:
                exp_date_str = exp_date.strftime('%Y-%m-%d')

        await query.message.reply_text(
            f"🧑 Perfil:\n• {user.full_name}\n• @{user.username or 'Sin usuario'}\n"
            f"• ID: {user_id}\n• Plan: {plan_type.replace('plan_', '').capitalize()}\n• Expira: {exp_date_str}", # MODIFICADO: Mostrar tipo de plan
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

    # Manejo del callback para reproducir el video individual
    elif data.startswith("play_video_"):
        pkg_id = data.split("_")[2]
        pkg = content_packages.get(pkg_id)
        if not pkg or "video_id" not in pkg:
            await query.message.reply_text("❌ Video no disponible.")
            return

        # Verificación de seguridad (similar a 'start' handler)
        for name, username in CHANNELS.items():
            try:
                member = await context.bot.get_chat_member(chat_id=username, user_id=user_id)
                if member.status not in ["member", "administrator", "creator"]:
                    await query.message.reply_text(
                        "🔒 Para ver este contenido debes unirte a los canales.",
                        reply_markup=InlineKeyboardMarkup(
                            [
                                [
                                    InlineKeyboardButton(
                                        "🔗 Unirse a canal 1", url=f"https://t.me/{CHANNELS['canal_1'][1:]}"
                                    )
                                ],
                                [
                                    InlineKeyboardButton(
                                        "🔗 Unirse a canal 2", url=f"https://t.me/{CHANNELS['canal_2'][1:]}"
                                    )
                                ],
                                [InlineKeyboardButton("✅ Verificar suscripción", callback_data="verify")],
                            ]
                        ),
                    )
                    return
            except Exception as e:
                logger.warning(f"Error verificando canal: {e}")
                await query.message.reply_text("❌ Error al verificar canales. Intenta más tarde.")
                return

        if can_view_video(user_id):
            await register_view(user_id)
            title_caption = pkg.get("caption", "🎬 Aquí tienes el video completo.")
            await query.message.reply_video(
                video=pkg["video_id"],
                caption=title_caption,
                protect_content=not can_resend_content(user_id)
            )
            await query.message.delete() # Eliminar el mensaje anterior
        else:
            await query.answer("🚫 Has alcanzado tu límite diario de videos. Compra un plan para más acceso.", show_alert=True)
            await query.message.reply_text(
                f"🚫 Has alcanzado tu límite diario de {FREE_LIMIT_VIDEOS} videos.\n"
                "💎 Por favor, considera comprar un plan para acceso ilimitado.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("💎 Comprar Planes", callback_data="planes")]]),
            )

    # Mostrar video capítulo con navegación (series)
    elif data.startswith("cap_"):
        _, serie_id, index = data.split("_")
        index = int(index)
        serie = series_data.get(serie_id)
        
        if not serie or "capitulos" not in serie:
            await query.message.reply_text("❌ Serie o capítulos no disponibles.")
            return

        capitulos = serie["capitulos"]
        total = len(capitulos)
        if index < 0 or index >= total:
            await query.message.reply_text("❌ Capítulo fuera de rango.")
            return

        # APLICACIÓN DE LA SEGURIDAD PARA CAPÍTULOS DE SERIES AQUÍ
        if can_view_video(user_id): # Verifica si tiene vistas disponibles
            await register_view(user_id) # Registra la vista
            video_id = capitulos[index]

            botones = []
            if index > 0:
                botones.append(InlineKeyboardButton("⬅️ Anterior", callback_data=f"cap_{serie_id}_{index - 1}"))
            if index < total - 1:
                botones.append(InlineKeyboardButton("➡️ Siguiente", callback_data=f"cap_{serie_id}_{index + 1}"))
            
            # Botón "Volver a la Serie" que regresará a la lista de capítulos
            botones.append(InlineKeyboardButton("🔙 Volver a la Serie", callback_data=f"serie_list_{serie_id}")) # Nuevo callback para listar capítulos

            markup = InlineKeyboardMarkup([botones])

            await query.edit_message_media(
                media=InputMediaVideo(
                    media=video_id,
                    caption=f"{serie['title']} - Capítulo {index+1}",
                    parse_mode="Markdown"
                ),
                reply_markup=markup,
            )
        else:
            await query.answer("🚫 Has alcanzado tu límite diario de videos. Compra un plan para más acceso.", show_alert=True)
            await query.message.reply_text(
                f"🚫 Has alcanzado tu límite diario de {FREE_LIMIT_VIDEOS} videos.\n"
                "💎 Por favor, considera comprar un plan para acceso ilimitado.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("💎 Comprar Planes", callback_data="planes")]]),
            )
    
    # Nuevo callback para mostrar la lista de capítulos de una serie
    elif data.startswith("serie_list_"):
        serie_id = data.split("_")[2]
        serie = series_data.get(serie_id)
        if not serie:
            await query.message.reply_text("❌ Serie no encontrada.")
            return
        
        # APLICACIÓN DE LA SEGURIDAD PARA SERIES AQUÍ (al volver a la lista)
        if not can_view_video(user_id): # Verifica si tiene vistas disponibles
            await query.message.reply_text(
                f"🚫 Has alcanzado tu límite diario de {FREE_LIMIT_VIDEOS} vistas para series/videos.\n"
                "💎 Por favor, considera comprar un plan para acceso ilimitado.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("💎 Comprar Planes", callback_data="planes")]]),
            )
            return

        capitulos = serie.get("capitulos", [])
        if not capitulos:
            await query.message.reply_text("❌ Esta serie no tiene capítulos disponibles aún.")
            return
        
        # Reutilizar la función para generar los botones de los capítulos
        markup = generate_chapter_buttons(serie_id, len(capitulos))

        await query.edit_message_media(
            media=InputMediaPhoto(
                media=serie["photo_id"],
                caption=f"📺 *{serie['title']}*\n\n{serie['caption']}\n\nSelecciona un capítulo:",
                parse_mode="Markdown"
            ),
            reply_markup=markup,
        )


# --- Pagos ---
async def precheckout_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.pre_checkout_query.answer(ok=True)

async def successful_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    payload = update.message.successful_payment.invoice_payload
    # MODIFICADO: Guardar el tipo de plan junto con la fecha de expiración
    if payload == PLAN_PRO_ITEM["payload"]:
        expire_at = datetime.now(timezone.utc) + timedelta(days=30)
        user_premium[user_id] = {"expire_at": expire_at, "plan_type": "plan_pro"}
        await update.message.reply_text("🎉 ¡Gracias por tu compra! Tu *Plan Pro* se activó por 30 días.")
    elif payload == PLAN_ULTRA_ITEM["payload"]:
        expire_at = datetime.now(timezone.utc) + timedelta(days=30)
        user_premium[user_id] = {"expire_at": expire_at, "plan_type": "plan_ultra"}
        await update.message.reply_text("🎉 ¡Gracias por tu compra! Tu *Plan Ultra* se activó por 30 días.")
    save_data() # Guardar los cambios en Firestore

# --- Funciones de administración (solo para el propietario del bot) ---
ADMIN_IDS = [int(os.getenv("ADMIN_ID"))] if os.getenv("ADMIN_ID") else []

async def setvideo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in ADMIN_IDS:
        await update.message.reply_text("🚫 No tienes permisos para usar este comando.")
        return

    if not update.message.reply_to_message or not update.message.reply_to_message.video:
        await update.message.reply_text("Por favor, responde a un video con este comando para guardarlo.")
        return

    video_id = update.message.reply_to_message.video.file_id
    caption = update.message.reply_to_message.caption or ""
    pkg_id = context.args[0] if context.args else f"video_{video_id[:8]}" # Generar un ID único si no se proporciona

    content_packages[pkg_id] = {"video_id": video_id, "caption": caption}
    save_data()
    await update.message.reply_text(
        f"✅ Video guardado con ID: `{pkg_id}`\n"
        f"Comparte este enlace: `https://t.me/{(await context.bot.get_me()).username}?start=video_{pkg_id}`",
        parse_mode="Markdown"
    )

async def setserie(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in ADMIN_IDS:
        await update.message.reply_text("🚫 No tienes permisos para usar este comando.")
        return

    if not update.message.reply_to_message or not update.message.reply_to_message.photo:
        await update.message.reply_text("Para configurar una serie, responde a una imagen que será la portada de la serie. Luego, sigue con los siguientes pasos.")
        current_photo[user_id] = None # Reset
        return

    # Paso 1: Obtener la foto y pedir el título/descripción
    photo_id = update.message.reply_to_message.photo[-1].file_id
    current_photo[user_id] = photo_id
    await update.message.reply_text("✅ Portada de la serie recibida. Ahora, envía el título y una descripción corta para la serie.")

    # Guardar el estado para el siguiente paso
    context.user_data['waiting_for_serie_info'] = True
    context.user_data['serie_photo_id'] = photo_id

async def handle_serie_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in ADMIN_IDS or not context.user_data.get('waiting_for_serie_info'):
        return

    # Paso 2: Recibir título y descripción
    if update.message.text:
        title_description = update.message.text
        # Asumiendo que la primera línea es el título y el resto es la descripción
        lines = title_description.split('\n', 1)
        title = lines[0].strip()
        caption = lines[1].strip() if len(lines) > 1 else ""

        serie_id = f"serie_{title.replace(' ', '_').lower()[:10]}_{datetime.now().strftime('%f')}" # Generar un ID único

        series_data[serie_id] = {
            "title": title,
            "photo_id": context.user_data['serie_photo_id'],
            "caption": caption,
            "capitulos": []
        }
        current_series[user_id] = { # Almacenar la serie actual para añadir capítulos
            "serie_id": serie_id,
            "title": title,
            "photo_id": context.user_data['serie_photo_id'],
            "caption": caption,
            "capitulos": [] # Inicialmente vacía, se llenará con addcap
        }
        save_data()

        await update.message.reply_text(
            f"✅ Serie '{title}' creada. Ahora puedes añadir capítulos respondiendo a un video con `/addcap`."
            f"\n\nComparte este enlace a la serie: `https://t.me/{(await context.bot.get_me()).username}?start=serie_{serie_id}`",
            parse_mode="Markdown"
        )
        del context.user_data['waiting_for_serie_info']
        del context.user_data['serie_photo_id']
    else:
        await update.message.reply_text("Por favor, envía el título y la descripción como texto.")


async def addcap(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in ADMIN_IDS:
        await update.message.reply_text("🚫 No tienes permisos para usar este comando.")
        return

    if not update.message.reply_to_message or not update.message.reply_to_message.video:
        await update.message.reply_text("Por favor, responde a un video para añadirlo como capítulo.")
        return

    if not current_series.get(user_id):
        await update.message.reply_text("Primero debes crear o seleccionar una serie para añadirle capítulos. Usa /setserie.")
        return

    serie_id = current_series[user_id]["serie_id"]
    video_id = update.message.reply_to_message.video.file_id

    if serie_id in series_data:
        series_data[serie_id]["capitulos"].append(video_id)
        current_series[user_id]["capitulos"].append(video_id) # Actualizar también en memoria temporal
        save_data()
        await update.message.reply_text(
            f"✅ Capítulo añadido a la serie '{series_data[serie_id]['title']}'. Total de capítulos: {len(series_data[serie_id]['capitulos'])}"
        )
    else:
        await update.message.reply_text("❌ No se encontró la serie actual. Por favor, reinicia el proceso con /setserie.")


async def listseries(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in ADMIN_IDS:
        await update.message.reply_text("🚫 No tienes permisos para usar este comando.")
        return

    if not series_data:
        await update.message.reply_text("No hay series guardadas.")
        return

    message_text = "📺 *Series Guardadas:*\n\n"
    for serie_id, serie in series_data.items():
        message_text += (
            f"• *{serie['title']}* (`{serie_id}`)\n"
            f"  Capítulos: {len(serie.get('capitulos', []))}\n"
            f"  Enlace: `https://t.me/{(await context.bot.get_me()).username}?start=serie_{serie_id}`\n\n"
        )
    await update.message.reply_text(message_text, parse_mode="Markdown")

async def deletemedia(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in ADMIN_IDS:
        await update.message.reply_text("🚫 No tienes permisos para usar este comando.")
        return

    if not context.args:
        await update.message.reply_text("Uso: /deletemedia <ID_del_video_o_serie_o_capítulo>")
        return

    target_id = context.args[0]

    # Intentar borrar de videos individuales
    if target_id in content_packages:
        del content_packages[target_id]
        save_data()
        await update.message.reply_text(f"✅ Video individual con ID `{target_id}` eliminado.")
        return

    # Intentar borrar una serie completa
    if target_id in series_data:
        del series_data[target_id]
        save_data()
        await update.message.reply_text(f"✅ Serie con ID `{target_id}` eliminada.")
        return

    # Intentar borrar un capítulo específico de una serie (formato: serieID_cap_INDEX)
    if '_' in target_id and 'cap_' in target_id:
        parts = target_id.split('_')
        if len(parts) == 3 and parts[1] == 'cap':
            serie_id = parts[0]
            try:
                cap_index = int(parts[2])
                if serie_id in series_data and 0 <= cap_index < len(series_data[serie_id].get("capitulos", [])):
                    del series_data[serie_id]["capitulos"][cap_index]
                    save_data()
                    await update.message.reply_text(f"✅ Capítulo {cap_index+1} de la serie `{serie_id}` eliminado.")
                    return
            except ValueError:
                pass # No es un índice válido

    await update.message.reply_text(f"❌ No se encontró contenido (video, serie o capítulo) con el ID: `{target_id}`.")


async def listusers(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in ADMIN_IDS:
        await update.message.reply_text("🚫 No tienes permisos para usar este comando.")
        return
    
    if not user_premium:
        await update.message.reply_text("No hay usuarios premium registrados.")
        return

    response = "👥 *Usuarios Premium:*\n\n"
    for uid, data in user_premium.items():
        expire_at_str = data["expire_at"].strftime('%Y-%m-%d %H:%M:%S')
        plan_type = data.get("plan_type", "premium_legacy")
        response += f"• ID: `{uid}` - Plan: `{plan_type.replace('plan_', '').capitalize()}` - Expira: `{expire_at_str}`\n"
    
    await update.message.reply_text(response, parse_mode="Markdown")

async def send_daily_reminder(application: Application):
    logger.info("Enviando recordatorios diarios a chats conocidos...")
    for chat_id in list(known_chats): # Usar list() para evitar RuntimeError por set modificado durante iteración
        try:
            await application.bot.send_message(
                chat_id=chat_id,
                text="👋 ¡Hola! No olvides revisar nuestro contenido nuevo hoy. ¡Tenemos muchas películas y series para ti!"
            )
            logger.info(f"Recordatorio enviado a chat: {chat_id}")
        except Exception as e:
            logger.error(f"Error enviando recordatorio a chat {chat_id}: {e}")
            # Si el chat da error (ej. bot bloqueado), lo removemos
            known_chats.discard(chat_id)
            save_data() # Guardar el set actualizado

async def info_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    current_plan = get_user_plan_type(user_id)
    
    if current_plan == "free":
        daily_limit = FREE_LIMIT_VIDEOS
    elif current_plan == "plan_pro":
        daily_limit = PRO_LIMIT_VIDEOS
    else: # plan_ultra o premium_legacy
        daily_limit = "ilimitado"

    today = str(datetime.utcnow().date())
    views_today = user_daily_views.get(str(user_id), {}).get(today, 0)
    
    # Calcular tiempo restante si es premium
    premium_status = "No activo"
    if is_premium(user_id):
        expire_at = user_premium[user_id]["expire_at"]
        now_utc = datetime.now(timezone.utc)
        if expire_at > now_utc:
            remaining_time = expire_at - now_utc
            days = remaining_time.days
            hours, remainder = divmod(remaining_time.seconds, 3600)
            minutes, seconds = divmod(remainder, 60)
            premium_status = f"Activo ({days}d {hours}h {minutes}m restantes)"
        else:
            premium_status = "Expirado" # Debería ser capturado por is_premium, pero para mayor claridad

    info_message = (
        f"ℹ️ *Información de Uso:*\n\n"
        f"• Tu plan actual: *{current_plan.replace('plan_', '').capitalize()}*\n"
        f"• Vistas diarias restantes: *{daily_limit - views_today if isinstance(daily_limit, int) else 'Ilimitado'}*\n"
        f"• Estado Premium: *{premium_status}*\n\n"
        "Puedes ver tu límite de videos en la sección de planes."
    )
    
    await update.message.reply_text(info_message, parse_mode="Markdown")


async def main() -> None:
    load_data()
    logger.info("Datos cargados al inicio.")

    application = Application.builder().token(TOKEN).build()

    # Añadir job para recordatorios diarios
    # application.job_queue.run_daily(send_daily_reminder, time=datetime.min.time(), days=(0, 1, 2, 3, 4, 5, 6)) # Ejecutar a medianoche UTC
    # Para probar más rápido, puedes usar run_repeating
    # application.job_queue.run_repeating(send_daily_reminder, interval=3600, first=0) # Cada hora

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CallbackQueryHandler(verify, pattern="^verify$"))
    application.add_handler(CallbackQueryHandler(handle_callback))
    application.add_handler(PreCheckoutQueryHandler(precheckout_handler))
    application.add_handler(MessageHandler(filters.SuccessfulPayment(), successful_payment))

    # Comandos de administración
    application.add_handler(CommandHandler("setvideo", setvideo))
    application.add_handler(CommandHandler("setserie", setserie))
    application.add_handler(CommandHandler("addcap", addcap))
    application.add_handler(CommandHandler("listseries", listseries))
    application.add_handler(CommandHandler("deletemedia", deletemedia))
    application.add_handler(CommandHandler("listusers", listusers))
    application.add_handler(CommandHandler("info", info_command))
    application.add_handler(MessageHandler(filters.TEXT & filters.ChatType.PRIVATE & filters.User(ADMIN_IDS) & (lambda msg: msg.from_user.id in current_photo and current_photo[msg.from_user.id] is not None), handle_serie_info))


    # Webhook setup
    await application.bot.set_webhook(url=APP_URL)
    webhook_server = web.Application()
    webhook_server.router.add_post(f"/{TOKEN}", lambda request: web.Response(status=200) if asyncio.create_task(application.update_queue.put(Update.de_json(json.loads(request.text), application.bot))) else web.Response(status=200))

    runner = web.AppRunner(webhook_server)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()

    # Keep the application running indefinitely
    await asyncio.Event().wait()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as e:
        logger.exception(f"Error fatal en la aplicación: {e}")
