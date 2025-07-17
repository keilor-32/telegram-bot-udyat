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
import re # ¡IMPORTANTE! Nuevo import para manejar el escape de Markdown

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
user_premium = {}           # {user_id: {"expire_at": datetime, "plan_type": "payload_del_plan"}}
user_daily_views = {}       # {user_id: {date: count}}
content_packages = {}       # {pkg_id: {photo_id, caption, video_id}}
known_chats = set()
current_photo = {}
user_verified = {}          # {user_id: True} si el usuario ya se verificó

# NUEVO: series con temporadas y capítulos
series_data = {}            # {serie_id: {"title", "photo_id", "caption", "temporadas": {T1: [video_id, ...], ...}}}
current_series = {}         # {user_id: {"title", "photo_id", "caption", "serie_id", "temporada", "capitulos": []}}

# --- Firestore colecciones ---
COLLECTION_USERS = "users_premium"
COLLECTION_VIDEOS = "videos"
COLLECTION_VIEWS = "user_daily_views"
COLLECTION_CHATS = "known_chats"
COLLECTION_SERIES = "series_data"   # NUEVO para series
COLLECTION_VERIFIED_USERS = "verified_users" # NUEVO para usuarios verificados

# --- Funciones Firestore (Síncronas) ---
def save_user_premium_firestore():
    batch = db.batch()
    for uid, exp_data in user_premium.items():
        doc_ref = db.collection(COLLECTION_USERS).document(str(uid))
        # Convertir datetime a string ISO para Firestore
        data_to_save = exp_data.copy()
        if "expire_at" in data_to_save and isinstance(data_to_save["expire_at"], datetime):
            data_to_save["expire_at"] = data_to_save["expire_at"].isoformat()
        batch.set(doc_ref, data_to_save)
    batch.commit()

def load_user_premium_firestore():
    docs = db.collection(COLLECTION_USERS).stream()
    result = {}
    for doc in docs:
        data = doc.to_dict()
        try:
            # Convertir string ISO a datetime al cargar
            if "expire_at" in data and isinstance(data["expire_at"], str):
                data["expire_at"] = datetime.fromisoformat(data["expire_at"])
            result[int(doc.id)] = data
        except Exception as e:
            logger.error(f"Error cargando datos premium para {doc.id}: {e}")
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

# NUEVO: Guardar y cargar usuarios verificados
def save_user_verified_firestore():
    batch = db.batch()
    for uid, verified_status in user_verified.items():
        doc_ref = db.collection(COLLECTION_VERIFIED_USERS).document(str(uid))
        batch.set(doc_ref, {"verified": verified_status})
    batch.commit()

def load_user_verified_firestore():
    docs = db.collection(COLLECTION_VERIFIED_USERS).stream()
    result = {}
    for doc in docs:
        data = doc.to_dict()
        result[int(doc.id)] = data.get("verified", False)
    return result


# --- Guardar y cargar todo ---
def save_data():
    save_user_premium_firestore()
    save_videos_firestore()
    save_user_daily_views_firestore()
    save_known_chats_firestore()
    save_series_firestore()
    save_user_verified_firestore() # NUEVO

def load_data():
    global user_premium, content_packages, user_daily_views, known_chats, series_data, user_verified
    user_premium = load_user_premium_firestore()
    content_packages = load_videos_firestore()
    user_daily_views = load_user_daily_views_firestore()
    known_chats = load_known_chats_firestore()
    series_data = load_series_firestore()
    user_verified = load_user_verified_firestore() # NUEVO

# --- Planes ---
FREE_LIMIT_VIDEOS = 10

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
    "prices": [LabeledPrice("Plan Ultra por 30 días", 100)], # Corregido a 100 estrellas
}

# --- Funciones de Utilidad ---
def escape_markdown_v2(text):
    """
    Helper function to escape characters for Markdown (which often behaves like MarkdownV2)
    to prevent parsing errors if text contains special Markdown characters.
    """
    escape_chars = r'_*[]()~`>#+-=|{}.!'
    return re.sub(f'([{re.escape(escape_chars)}])', r'\\\1', text)

# --- Control acceso ---
def is_premium(user_id):
    if user_id in user_premium and "expire_at" in user_premium[user_id]:
        return user_premium[user_id]["expire_at"] > datetime.utcnow()
    return False

def get_user_plan_name(user_id):
    if is_premium(user_id):
        plan_data = user_premium.get(user_id, {})
        plan_type = plan_data.get("plan_type", "premium_plan") # Default a 'premium_plan' si no está
        if plan_type == PLAN_PRO_ITEM["payload"]:
            return PLAN_PRO_ITEM["title"]
        elif plan_type == PLAN_ULTRA_ITEM["payload"]:
            return PLAN_ULTRA_ITEM["title"]
        else:
            return PREMIUM_ITEM["title"] # Por compatibilidad si no se guarda el tipo
    return "Gratis"

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


# --- Handlers ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    user_id = update.effective_user.id

    # --- Lógica para el paso intermedio de videos individuales (mantener) ---
    if args and args[0].startswith("content_"):
        pkg_id = args[0].split("_")[1]
        pkg = content_packages.get(pkg_id)
        if not pkg:
            await update.message.reply_text("❌ Contenido no disponible o eliminado.")
            return

        boton_ver_video = InlineKeyboardMarkup(
            [[InlineKeyboardButton("▶️ Ver Video", callback_data=f"show_video_{pkg_id}")]]
        )
        await update.message.reply_photo(
            photo=pkg["photo_id"],
            caption=pkg["caption"],
            reply_markup=boton_ver_video,
            parse_mode="Markdown"
        )
        return # Importante: salimos de la función después de manejar el parámetro content_

    # NUEVO: manejo de argumentos para series (ej: start=serie_serieid)
    elif args and args[0].startswith("serie_"):
        serie_id = args[0].split("_", 1)[1]
        serie = series_data.get(serie_id)
        if not serie:
            await update.message.reply_text("❌ Serie no encontrada.")
            return
        
        # Obtener la primera temporada y sus capítulos para mostrarlos directamente
        temporada_keys = sorted(serie.get("temporadas", {}).keys())
        
        if not temporada_keys:
            await update.message.reply_text("❌ Esta serie no tiene capítulos disponibles.")
            return

        first_temporada_key = temporada_keys[0]
        capitulos = serie["temporadas"][first_temporada_key]
        
        # Generar botones en cuadrícula
        botones = []
        row = []
        for i, _ in enumerate(capitulos):
            row.append(InlineKeyboardButton(f"{i+1}", callback_data=f"cap_{serie_id}_{first_temporada_key}_{i}"))
            if len(row) == 5: # 5 botones por fila
                botones.append(row)
                row = []
        if row: # Añadir la última fila si no está completa
            botones.append(row)
        
        # Botón para volver a la lista de temporadas si hubiera más de una
        if len(temporada_keys) > 1:
            botones.append([InlineKeyboardButton("🔙 Ver Temporadas", callback_data=f"list_temporadas_{serie_id}")])

        await update.message.reply_text(
            f"📺 *{escape_markdown_v2(serie['title'])}*\n\n{escape_markdown_v2(serie['caption'])}\n\nCapítulos de la Temporada {first_temporada_key[1:]}:",
            reply_markup=InlineKeyboardMarkup(botones),
            parse_mode="Markdown",
            disable_web_page_preview=True,
        )
        return # Importante: salimos de la función después de manejar el parámetro serie_

    # --- Lógica para usuarios ya verificados ---
    if user_verified.get(user_id):
        await update.message.reply_text("📋 Menú principal:", reply_markup=get_main_menu())
        return # Salir si el usuario ya está verificado

    # --- Flujo de verificación para usuarios no verificados ---
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
        except Exception as e:
            logger.warning(f"Error verificando canal: {e}")
            not_joined.append(username) # Asumimos que no está unido si hay error

    if not not_joined:
        user_verified[user_id] = True # Marcar como verificado
        save_data() # Guardar el estado de verificación
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
            f"🔹 Gratis – Hasta {FREE_LIMIT_VIDEOS} videos por día.\n\n"
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
            exp_data = user_premium.get(user_id, {})
            exp = exp_data.get("expire_at", datetime.min).strftime("%Y-%m-%d")
            plan_name = get_user_plan_name(user_id)
            await query.message.reply_text(f"✅ Ya tienes el plan *{escape_markdown_v2(plan_name)}* activo hasta {exp}.", parse_mode="Markdown")
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
            exp_data = user_premium.get(user_id, {})
            exp = exp_data.get("expire_at", datetime.min).strftime("%Y-%m-%d")
            plan_name = get_user_plan_name(user_id)
            await query.message.reply_text(f"✅ Ya tienes el plan *{escape_markdown_v2(plan_name)}* activo hasta {exp}.", parse_mode="Markdown")
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
        plan_name = get_user_plan_name(user_id)
        exp_data = user_premium.get(user_id, {})
        exp = exp_data.get("expire_at")
        
        # Escapamos el nombre del plan antes de ponerlo en negrita
        escaped_plan_name = escape_markdown_v2(plan_name)
        
        await query.message.reply_text(
            f"🧑 Perfil:\n• {escape_markdown_v2(user.full_name)}\n• @{escape_markdown_v2(user.username or 'Sin usuario')}\n"
            f"• ID: {user_id}\n• Plan: *{escaped_plan_name}*\n• Expira: {exp.strftime('%Y-%m-%d') if exp else 'N/A'}",
            parse_mode="Markdown",
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

    # --- Lógica para mostrar el video individual después del paso intermedio ---
    elif data.startswith("show_video_"):
        prefix, pkg_id = data.rsplit('_', 1)
        
        pkg = content_packages.get(pkg_id)
        if not pkg:
            await query.message.reply_text("❌ Video no disponible o eliminado.")
            return

        # Verificar suscripción a canales antes de permitir ver el video
        for name, username in CHANNELS.items():
            try:
                member = await context.bot.get_chat_member(chat_id=username, user_id=user_id)
                if member.status not in ["member", "administrator", "creator"]:
                    await query.answer("🔒 Para ver este contenido debes unirte a los canales.", show_alert=True)
                    await query.message.reply_text(
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
                logger.warning(f"Error verificando canal para video individual: {e}")
                await query.answer("❌ Error al verificar canales. Intenta más tarde.", show_alert=True)
                return

        if can_view_video(user_id):
            await register_view(user_id)
            await query.message.reply_video(
                video=pkg["video_id"],
                caption=f"🎬 *{escape_markdown_v2(pkg['caption'].splitlines()[0])}*", # Escapar título también
                parse_mode="Markdown",
                protect_content=not is_premium(user_id)
            )
            try:
                await query.delete_message()
            except Exception as e:
                logger.warning(f"No se pudo eliminar el mensaje de sinopsis intermedia: {e}")
        else:
            await query.answer("🚫 Has alcanzado tu límite diario de videos. Compra un plan para más acceso.", show_alert=True)
            await query.message.reply_text(
                f"🚫 Has alcanzado tu límite diario de {FREE_LIMIT_VIDEOS} videos.\n"
                "💎 Compra un plan para más acceso y reenvíos ilimitados.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("💎 Comprar Planes", callback_data="planes")]]),
            )

    # --- Bloque para listar temporadas de una serie (ahora solo accesible si se añade un botón específico) ---
    elif data.startswith("list_temporadas_"):
        _, serie_id = data.split("_", 2)
        serie = series_data.get(serie_id)
        if not serie:
            await query.message.reply_text("❌ Serie no encontrada.")
            return

        botones = []
        for temporada_key in sorted(serie.get("temporadas", {}).keys()):
            botones.append(
                [InlineKeyboardButton(f"Temporada {temporada_key[1:]}", callback_data=f"ver_{serie_id}_{temporada_key}")]
            )
        
        await query.message.reply_text(
            f"📺 Temporadas de *{escape_markdown_v2(serie['title'])}*:",
            reply_markup=InlineKeyboardMarkup(botones),
            parse_mode="Markdown"
        )
        try:
            await query.delete_message()
        except Exception as e:
            logger.warning(f"No se pudo eliminar el mensaje anterior en 'list_temporadas_': {e}")


    # --- Bloque para mostrar capítulos de una temporada específica ---
    elif data.startswith("ver_"):
        # formato ver_{serie_id}_{temporada}
        _, serie_id, temporada = data.split("_", 2)
        serie = series_data.get(serie_id)
        if not serie or temporada not in serie.get("temporadas", {}):
            await query.message.reply_text("❌ Temporada no disponible.")
            return

        capitulos = serie["temporadas"][temporada]
        botones = []
        row = []
        for i, _ in enumerate(capitulos):
            row.append(InlineKeyboardButton(f"{i+1}", callback_data=f"cap_{serie_id}_{temporada}_{i}"))
            if len(row) == 5: # 5 botones por fila
                botones.append(row)
                row = []
        if row: # Añadir la última fila si no está completa
            botones.append(row)
        
        # Botón para volver a la lista de temporadas (si aplica)
        if len(serie.get("temporadas", {})) > 1:
            botones.append([InlineKeyboardButton("🔙 Volver a Temporadas", callback_data=f"list_temporadas_{serie_id}")])
        else: # Si solo hay una temporada, volver al menú principal de la serie
            botones.append([InlineKeyboardButton("🔙 Volver", callback_data=f"serie_{serie_id}")]) # Asumiendo que "serie_" llevaría a la primera temporada

        await query.message.reply_text(
            f"📺 Capítulos de Temporada {temporada[1:]}:",
            reply_markup=InlineKeyboardMarkup(botones)
        )
        
        try:
            await query.delete_message()
        except Exception as e:
            logger.warning(f"No se pudo eliminar el mensaje anterior en 'ver_': {e}")


    # --- Bloque para mostrar video capítulo con navegación y seguridad de reenvíos ---
    elif data.startswith("cap_"):
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

        # Verificar suscripción a canales antes de permitir ver el capítulo
        for name, username in CHANNELS.items():
            try:
                member = await context.bot.get_chat_member(chat_id=username, user_id=user_id)
                if member.status not in ["member", "administrator", "creator"]:
                    await query.answer("🔒 Para ver este contenido debes unirte a los canales.", show_alert=True)
                    await query.message.reply_text(
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
                logger.warning(f"Error verificando canal para serie: {e}")
                await query.answer("❌ Error al verificar canales. Intenta más tarde.", show_alert=True)
                return

        if can_view_video(user_id):
            await register_view(user_id)
            video_id = capitulos[index]

            botones_navegacion = []
            if index > 0:
                botones_navegacion.append(InlineKeyboardButton("⬅️ Anterior", callback_data=f"cap_{serie_id}_{temporada}_{index - 1}"))
            if index < total - 1:
                botones_navegacion.append(InlineKeyboardButton("➡️ Siguiente", callback_data=f"cap_{serie_id}_{temporada}_{index + 1}"))
            
            markup_buttons = [botones_navegacion]
            
            # Si hay más de una temporada, damos la opción de volver a la lista de temporadas
            # de lo contrario, si solo hay una temporada, volvemos a la lista de capítulos de esa temporada.
            if len(serie.get("temporadas", {})) > 1:
                 markup_buttons.append([InlineKeyboardButton("🔙 Ver Temporadas", callback_data=f"list_temporadas_{serie_id}")])
            else: # Si solo hay una temporada, vuelve a la lista de capítulos de la misma temporada
                markup_buttons.append([InlineKeyboardButton("🔙 Ver Capítulos", callback_data=f"ver_{serie_id}_{temporada}")])


            markup = InlineKeyboardMarkup(markup_buttons)

            await query.message.reply_video(
                video=video_id,
                caption=f"📺 *{escape_markdown_v2(serie['title'])}*\n\nTemporada {temporada[1:]} Capítulo {index+1}",
                parse_mode="Markdown",
                reply_markup=markup,
                protect_content=not is_premium(user_id)
            )
            try:
                await query.delete_message()
            except Exception as e:
                logger.warning(f"No se pudo eliminar el mensaje anterior en 'cap_': {e}")

        else:
            await query.answer("🚫 Has alcanzado tu límite diario de videos. Compra un plan para más acceso.", show_alert=True)
            await query.message.reply_text(
                f"🚫 Has alcanzado tu límite diario de {FREE_LIMIT_VIDEOS} videos.\n"
                "💎 Compra un plan para más acceso y reenvíos ilimitados.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("💎 Comprar Planes", callback_data="planes")]]),
            )


# --- Pagos ---
async def precheckout_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.pre_checkout_query.answer(ok=True)

async def successful_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    payload = update.message.successful_payment.invoice_payload
    
    expire_at = datetime.utcnow() + timedelta(days=30)
    user_premium[user_id] = {
        "expire_at": expire_at,
        "plan_type": payload # Guarda el tipo de plan adquirido
    }
    save_data()
    
    plan_name = "tu plan"
    if payload == PLAN_PRO_ITEM["payload"]:
        plan_name = PLAN_PRO_ITEM["title"]
    elif payload == PLAN_ULTRA_ITEM["payload"]:
        plan_name = PLAN_ULTRA_ITEM["title"]
    elif payload == PREMIUM_ITEM["payload"]:
        plan_name = PREMIUM_ITEM["title"]

    await update.message.reply_text(f"🎉 ¡Gracias por tu compra! Tu *{escape_markdown_v2(plan_name)}* se activó por 30 días.", parse_mode="Markdown")

# --- Recepción contenido (sinopsis + video) ---
async def recibir_foto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    user_id = msg.from_user.id
    if msg.photo and msg.caption:
        current_photo[user_id] = {
            "photo_id": msg.photo[-1].file_id,
            "caption": msg.caption,
        }
        await msg.reply_text("✅ Sinopsis recibida. Ahora envía el video o usa /crear_serie para series.")
    else:
        await msg.reply_text("❌ Envía una imagen con sinopsis.")

async def recibir_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    user_id = msg.from_user.id
    if user_id not in current_photo:
        await msg.reply_text("❌ Primero envía una sinopsis con imagen.")
        return

    pkg_id = str(int(datetime.utcnow().timestamp()))
    photo_id = current_photo[user_id]["photo_id"]
    caption = current_photo[user_id]["caption"]
    video_id = msg.video.file_id

    content_packages[pkg_id] = {
        "photo_id": photo_id,
        "caption": caption,
        "video_id": video_id,
    }
    del current_photo[user_id]

    save_data()

    boton = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "Ver Contenido", url=f"https://t.me/{(await context.bot.get_me()).username}?start=content_{pkg_id}"
                )
            ]
        ]
    )
    for chat_id in known_chats:
        try:
            await context.bot.send_photo(
                chat_id=chat_id,
                photo=photo_id,
                caption=caption, # La caption ya debería estar limpia
                reply_markup=boton,
                protect_content=True,
                parse_mode="Markdown" # Asegúrate de que las captions que envías estén bien formadas si tienen Markdown
            )
        except Exception as e:
            logger.warning(f"No se pudo enviar a {chat_id}: {e}")

    await msg.reply_text("✅ Contenido enviado a los grupos.")

# --- NUEVO: Comandos para series ---

async def crear_serie(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Comando para iniciar creación de serie (sinopsis + foto)."""
    user_id = update.message.from_user.id
    if user_id not in current_photo:
        await update.message.reply_text("❌ Primero envía la sinopsis con imagen.")
        return
    # Guardar temporalmente la info de la serie para que usuario añada temporadas y capítulos
    serie_id = str(int(datetime.utcnow().timestamp()))
    data = current_photo[user_id]
    current_series[user_id] = {
        "serie_id": serie_id,
        "title": data["caption"].split("\n")[0],  # Toma la primera línea como título
        "photo_id": data["photo_id"],
        "caption": data["caption"],
        "temporadas": {},
    }
    del current_photo[user_id]
    await update.message.reply_text(
        "✅ Serie creada temporalmente.\n"
        "Usa /agregar_temporada para añadir una temporada."
    )

async def agregar_temporada(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Comando para añadir temporada."""
    user_id = update.message.from_user.id
    if user_id not in current_series:
        await update.message.reply_text("❌ No hay serie en creación. Usa /crear_serie primero.")
        return
    args = context.args
    if not args or not args[0].isdigit():
        await update.message.reply_text("❌ Usa /agregar_temporada N , donde N es número de temporada.")
        return
    temporada_num = args[0]
    temporada_key = f"T{temporada_num}"
    serie = current_series[user_id]
    if temporada_key in serie["temporadas"]:
        await update.message.reply_text(f"❌ La temporada {temporada_num} ya existe.")
        return
    serie["temporadas"][temporada_key] = []
    await update.message.reply_text(f"✅ Temporada {temporada_num} agregada.\nAhora envía todos los videos de los capítulos para esta temporada en un álbum (o varios si hay muchos) o de uno en uno, usando el mismo comando /agregar_capitulo {temporada_num}.")

async def agregar_capitulo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Comando para agregar capítulo a temporada. Ahora adaptado para indicar envío masivo."""
    user_id = update.message.from_user.id
    if user_id not in current_series:
        await update.message.reply_text("❌ No hay serie en creación. Usa /crear_serie primero.")
        return
    args = context.args
    if len(args) < 1 or not args[0].isdigit():
        await update.message.reply_text("❌ Usa /agregar_capitulo N y envía el/los video(s) de los capítulos en un álbum o individualmente.")
        return
    temporada_num = args[0]
    temporada_key = f"T{temporada_num}"
    serie = current_series[user_id]
    if temporada_key not in serie["temporadas"]:
        await update.message.reply_text(f"❌ La temporada {temporada_num} no existe. Añádela con /agregar_temporada {temporada_num}")
        return
    
    await update.message.reply_text(
        f"📽️ Por favor envía ahora el/los video(s) para los capítulos de la temporada {temporada_num}. Puedes enviar un álbum de hasta 10 videos."
    )
    # Guardamos temporada activa para el usuario para el siguiente video(s)
    serie["temporada_activa"] = temporada_key

async def recibir_video_serie(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Para recibir video(s) y asignarlo(s) como capítulo(s)
    si el usuario está en proceso de agregar capítulo a temporada.
    Maneja tanto videos individuales como álbumes.
    """
    msg = update.message
    user_id = msg.from_user.id

    # Si no hay una serie en creación o una temporada activa, se maneja como video individual
    if user_id not in current_series or "temporada_activa" not in current_series[user_id]:
        # Para que el bot responda correctamente cuando envían una foto con caption
        # y luego un video, sin pasar por /crear_serie.
        # Filtramos solo si es un video, ya que la foto la maneja 'recibir_foto'.
        if msg.video:
            await recibir_video(update, context)
        elif msg.photo and msg.caption:
            await recibir_foto(update, context)
        return

    serie = current_series[user_id]
    temporada_key = serie["temporada_activa"]
    
    videos_added = 0
    
    # Manejar media_group (álbum)
    if msg.media_group_id and msg.video:
        # Se asume que cada video en un media_group es un capítulo.
        # No se necesita un cache explícito si `append` se encarga de añadir.
        serie["temporadas"][temporada_key].append(msg.video.file_id)
        videos_added = 1
        
        # Opcional: Para evitar múltiples mensajes de confirmación por cada video en un álbum,
        # podríamos guardar un contador y solo enviar el mensaje después de X segundos
        # o cuando se detecte el último video del álbum (más complejo).
        # Por ahora, un mensaje por cada video de un álbum está bien.

    elif msg.video: # Es un video individual
        serie["temporadas"][temporada_key].append(msg.video.file_id)
        videos_added = 1
    else:
        # Si no es un video ni parte de un álbum de videos en el contexto de una serie,
        # puede ser un mensaje de texto o algo no esperado.
        # Podríamos ignorarlo o dar una indicación más específica.
        return # No respondemos si no es un video aquí

    if videos_added > 0:
        total_chapters = len(serie["temporadas"][temporada_key])
        await msg.reply_text(
            f"✅ Capítulo(s) agregado(s) a la temporada {temporada_key[1:]}. "
            f"Total capítulos en esta temporada: {total_chapters}.\n"
            f"Usa /finalizar_serie para guardar la serie o /agregar_capitulo {temporada_key[1:]} para añadir más capítulos."
        )


async def finalizar_serie(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Finaliza y guarda la serie creada en Firestore y memoria."""
    user_id = update.message.from_user.id
    if user_id not in current_series:
        await update.message.reply_text("❌ No hay serie en creación.")
        return
    serie = current_series[user_id]
    # Guardar en memoria global series_data
    serie_id = serie["serie_id"]
    # Removemos estado temporal
    if "temporada_activa" in serie:
        del serie["temporada_activa"]
    
    # Escapar el título y la descripción antes de guardarlos si vas a mostrarlos con Markdown
    series_data[serie_id] = {
        "title": escape_markdown_v2(serie["title"]),
        "photo_id": serie["photo_id"],
        "caption": escape_markdown_v2(serie["caption"]),
        "temporadas": serie["temporadas"],
    }
    save_data()
    del current_series[user_id]

    # Enviar a grupos la portada con botón "Ver Serie"
    boton = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "▶️ Ver Serie",
                    # Al darle "Ver Serie", se redirige directamente a la lista de capítulos de la primera temporada
                    url=f"https://t.me/{(await context.bot.get_me()).username}?start=serie_{serie_id}",
                )
            ]
        ]
    )
    for chat_id in known_chats:
        try:
            await context.bot.send_photo(
                chat_id=chat_id,
                photo=serie["photo_id"],
                caption=serie["caption"], # La caption ya está escapada
                reply_markup=boton,
                protect_content=True,
                parse_mode="Markdown"
            )
        except Exception as e:
            logger.warning(f"No se pudo enviar serie a {chat_id}: {e}")

    await update.message.reply_text("✅ Serie guardada y enviada a los grupos.")


async def detectar_grupo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if chat.type in ["group", "supergroup"]:
        if chat.id not in known_chats:
            known_chats.add(chat.id)
            save_data()
            logger.info(f"Grupo registrado: {chat.id}")


# --- WEBHOOK aiohttp ---
async def webhook_handler(request):
    data = await request.json()
    update = Update.de_json(data, app_telegram.bot)
    await app_telegram.update_queue.put(update)
    return web.Response(text="OK")


async def on_startup(app):
    webhook_url = f"{APP_URL}/webhook"
    await app_telegram.bot.set_webhook(webhook_url)
    logger.info(f"Webhook configurado en {webhook_url}")


async def on_shutdown(app):
    await app_telegram.bot.delete_webhook()
    logger.info("Webhook eliminado")


# --- App Telegram ---
app_telegram = Application.builder().token(TOKEN).build()

# Agregar handlers
app_telegram.add_handler(CommandHandler("start", start))
app_telegram.add_handler(CallbackQueryHandler(verify, pattern="^verify$"))
app_telegram.add_handler(CallbackQueryHandler(handle_callback))
app_telegram.add_handler(PreCheckoutQueryHandler(precheckout_handler))
app_telegram.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment))

# Importante: el orden de los MessageHandlers importa.
# Primero: Mensajes de fotos con caption (sinopsis inicial)
app_telegram.add_handler(MessageHandler(filters.PHOTO & filters.Caption(True) & filters.ChatType.PRIVATE, recibir_foto))

# Segundo: Mensajes de video O mensajes de foto sin caption (para álbumes o videos individuales de series)
# La función `recibir_video_serie` está diseñada para manejar ambos escenarios (video individual, álbum de videos)
# y decidir si está en el contexto de una serie o si es un video "suelto".
# CAMBIO CLAVE AQUÍ: filters.PHOTO & filters.Caption(False)
app_telegram.add_handler(MessageHandler(filters.VIDEO & filters.ChatType.PRIVATE | filters.PHOTO & filters.Caption(False) & filters.ChatType.PRIVATE, recibir_video_serie))


app_telegram.add_handler(MessageHandler(filters.ALL & filters.ChatType.GROUPS, detectar_grupo))

# NUEVOS comandos para series
app_telegram.add_handler(CommandHandler("crear_serie", crear_serie))
app_telegram.add_handler(CommandHandler("agregar_temporada", agregar_temporada))
app_telegram.add_handler(CommandHandler("agregar_capitulo", agregar_capitulo))
app_telegram.add_handler(CommandHandler("finalizar_serie", finalizar_serie))

# --- Servidor aiohttp ---
web_app = web.Application()
web_app.router.add_post("/webhook", webhook_handler)
web_app.router.add_get("/ping", lambda request: web.Response(text="✅ Bot activo."))
web_app.on_startup.append(on_startup)
web_app.on_shutdown.append(on_shutdown)


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
