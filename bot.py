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

# Se deserializa dos veces porque la variable de entorno está doblemente serializada
# Esta parte puede variar dependiendo de cómo Render maneja la variable JSON.
# Si solo es una cadena JSON directa, solo necesitarías json.loads(google_credentials_raw)
try:
    google_credentials_str = json.loads(google_credentials_raw)
    google_credentials_dict = json.loads(google_credentials_str)
except json.JSONDecodeError:
    # Si falla la doble deserialización, intenta una única deserialización
    google_credentials_dict = json.loads(google_credentials_raw)

# Guardar temporalmente las credenciales en un archivo para Firebase
with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as temp:
    json.dump(google_credentials_dict, temp)
    temp_path = temp.name

cred = credentials.Certificate(temp_path)
firebase_admin.initialize_app(cred)
db = firestore.client()

print("✅ Firestore inicializado correctamente.")

# --- Configuración del Bot ---
TOKEN = os.getenv("TOKEN")
PROVIDER_TOKEN = os.getenv("PROVIDER_TOKEN", "") # Token de proveedor de pagos, opcional si no usas pagos
APP_URL = os.getenv("APP_URL") # URL de tu aplicación en Render
PORT = int(os.getenv("PORT", "8080"))

if not TOKEN:
    raise ValueError("❌ ERROR: La variable de entorno TOKEN no está configurada.")
if not APP_URL:
    raise ValueError("❌ ERROR: La variable de entorno APP_URL no está configurada.")

# --- Configuración de Logging ---
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- Variables en memoria (se cargarán desde Firestore) ---
user_premium = {}           # {user_id: {"expire_at": datetime, "plan_type": "payload"}}
user_daily_views = {}       # {user_id: {date: count}}
content_packages = {}       # {pkg_id: {photo_id, caption, video_id}}
known_chats = set()         # IDs de chats a los que se difunde contenido
current_photo = {}          # Para guardar la foto y sinopsis temporalmente antes de un video/serie
user_verified = {}          # {user_id: True} si el usuario ya se verificó con los canales

series_data = {}            # {serie_id: {"title", "photo_id", "caption", "temporadas": {T1: [video_id, ...], ...}}}
current_series = {}         # {user_id: {"serie_id", "title", "photo_id", "caption", "temporadas": {}}} para creación

# --- Nombres de Colecciones de Firestore ---
COLLECTION_USERS = "users_premium"
COLLECTION_VIDEOS = "videos"
COLLECTION_VIEWS = "user_daily_views"
COLLECTION_CHATS = "known_chats"
COLLECTION_SERIES = "series_data"
COLLECTION_VERIFIED_USERS = "verified_users"

# --- Funciones de guardado y carga de datos de Firestore ---
# Estas funciones son síncronas porque interactúan con el SDK de Firebase que no es asyncio nativo
# Se llaman dentro de funciones async, lo cual es manejado por python-telegram-bot
def save_user_premium_firestore():
    batch = db.batch()
    for uid, data in user_premium.items():
        doc_ref = db.collection(COLLECTION_USERS).document(str(uid))
        exp = data["expire_at"]
        plan_type = data["plan_type"]
        if exp.tzinfo is None: # Asegurarse de que el datetime sea timezone-aware
            exp = exp.replace(tzinfo=timezone.utc)
        batch.set(doc_ref, {"expire_at": exp.isoformat(), "plan_type": plan_type})
    batch.commit()

def load_user_premium_firestore():
    docs = db.collection(COLLECTION_USERS).stream()
    result = {}
    for doc in docs:
        data = doc.to_dict()
        try:
            expire_at_str = data.get("expire_at")
            plan_type = data.get("plan_type", "premium_plan") # Default si no existe
            if expire_at_str:
                expire_at = datetime.fromisoformat(expire_at_str)
                if expire_at.tzinfo is None: # Asegurarse de que sea timezone-aware
                    expire_at = expire_at.replace(tzinfo=timezone.utc)
                result[int(doc.id)] = {"expire_at": expire_at, "plan_type": plan_type}
        except Exception as e:
            logger.error(f"Error cargando datos premium del usuario {doc.id}: {e}")
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

# --- Funciones de carga y guardado general ---
def save_data():
    save_user_premium_firestore()
    save_videos_firestore()
    save_user_daily_views_firestore()
    save_known_chats_firestore()
    save_series_firestore()
    save_user_verified_firestore()

def load_data():
    global user_premium, content_packages, user_daily_views, known_chats, series_data, user_verified
    user_premium = load_user_premium_firestore()
    content_packages = load_videos_firestore()
    user_daily_views = load_user_daily_views_firestore()
    known_chats = load_known_chats_firestore()
    series_data = load_series_firestore()
    user_verified = load_user_verified_firestore()

# --- Configuración de Planes de Suscripción ---
FREE_LIMIT_VIDEOS = 3 # Límite de videos para usuarios gratis

PREMIUM_ITEM = {
    "title": "Plan Premium (Básico)",
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
    "description": "Videos ilimitados y reenvíos ilimitados, sin restricciones.",
    "payload": "plan_ultra",
    "currency": "XTR",
    "prices": [LabeledPrice("Plan Ultra por 30 días", 100)],
}

# Diccionario para mapear payloads a títulos de planes (para mostrar al usuario)
PLAN_PAYLOAD_TO_TITLE = {
    PREMIUM_ITEM["payload"]: PREMIUM_ITEM["title"],
    PLAN_PRO_ITEM["payload"]: PLAN_PRO_ITEM["title"],
    PLAN_ULTRA_ITEM["payload"]: PLAN_ULTRA_ITEM["title"],
}

# --- Control de Acceso y Límites ---
def is_premium(user_id):
    user_data = user_premium.get(user_id)
    if user_data and isinstance(user_data, dict) and "expire_at" in user_data:
        return user_data["expire_at"] > datetime.now(timezone.utc)
    return False

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

# --- Canales para Verificación de Suscripción ---
CHANNELS = {
    "supertvw2": "@Supertvw2",
    "fullvvd": "@fullvvd",
}

# --- Menú Principal del Bot ---
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
                InlineKeyboardButton("🎬 Películas", url="https://t.me/+dVTzx8dMGf81NTcx"),
                InlineKeyboardButton("🎬 Series", url="https://t.me/+qiFtv2EmV-xmNWFh"),
            ],
            [
                InlineKeyboardButton("💎 Planes", callback_data="planes"),
             ],
            [
                InlineKeyboardButton("🧑 Perfil", callback_data="perfil"),
            ],
            [
                InlineKeyboardButton("ℹ️ Info", callback_data="info"),
                InlineKeyboardButton("❓ Soporte", url="https://t.me/Hsito"),
            ],
        ]
    )

# --- Funciones de Utilidad ---
def escape_for_telegram_markdown(text: str) -> str:
    """Escapa los caracteres especiales de Markdown para Telegram (parse_mode='Markdown').
    Esto previene errores de "Can't parse entities" al mostrar contenido generado por el usuario.
    """
    text = text.replace("_", "\\_")
    text = text.replace("*", "\\*")
    text = text.replace("`", "\\`")
    text = text.replace("[", "\\[")
    return text

# --- Handlers del Bot ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Maneja el comando /start. Puede redirigir a contenido o al menú principal."""
    args = context.args
    user_id = update.effective_user.id

    # Lógica para enlaces de contenido individual (películas)
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
        return

    # Lógica para enlaces de series
    elif args and args[0].startswith("serie_"):
        serie_id = args[0].split("_", 1)[1]
        serie = series_data.get(serie_id)
        if not serie:
            await update.message.reply_text("❌ Serie no encontrada.")
            return
        
        temporada_keys = sorted(serie.get("temporadas", {}).keys())
        
        if not temporada_keys:
            await update.message.reply_text("❌ Esta serie no tiene capítulos disponibles.")
            return

        first_temporada_key = temporada_keys[0] # Muestra la primera temporada por defecto
        capitulos = serie["temporadas"][first_temporada_key]
        
        botones = []
        row = []
        for i, _ in enumerate(capitulos):
            row.append(InlineKeyboardButton(f"{i+1}", callback_data=f"cap_{serie_id}_{first_temporada_key}_{i}"))
            if len(row) == 5: # 5 botones por fila para capítulos
                botones.append(row)
                row = []
        if row: # Añadir la última fila si no está completa
            botones.append(row)
        
        if len(temporada_keys) > 1: # Si hay más de una temporada, ofrecer opción de ver todas
            botones.append([InlineKeyboardButton("🔙 Ver Temporadas", callback_data=f"list_temporadas_{serie_id}")])

        await update.message.reply_text(
            f"📺 *{escape_for_telegram_markdown(serie['title'])}*\n\n{escape_for_telegram_markdown(serie['caption'])}\n\nCapítulos de la Temporada {first_temporada_key[1:]}:",
            reply_markup=InlineKeyboardMarkup(botones),
            parse_mode="Markdown",
            disable_web_page_preview=True, # Evita la previsualización de enlaces en la descripción
        )
        return

    # Flujo de verificación para usuarios no verificados
    if not user_verified.get(user_id):
        await update.message.reply_text(
            "👋 ¡Hola! Debes unirte a todos nuestros canales para poder usar este bot. Una vez te hayas unido, haz clic en 'Verificar suscripción' para continuar.",
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

    # Si el usuario ya está verificado, mostrar menú principal
    await update.message.reply_text("📋 Menú principal:", reply_markup=get_main_menu())


async def verify(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Maneja el callback del botón de verificación de suscripción a canales."""
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
            logger.warning(f"Error verificando canal ({username}): {e}")
            not_joined.append(username) # Asumimos que no está unido si hay error

    if not not_joined:
        user_verified[user_id] = True # Marcar como verificado
        save_data() # Guardar el estado de verificación
        await query.edit_message_text("✅ Verificación completada. Menú disponible:")
        await query.message.reply_text("📋 Menú principal:", reply_markup=get_main_menu())
    else:
        await query.edit_message_text("❌ Aún no estás suscrito a los siguientes canales:\n" + "\n".join(not_joined))


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Maneja todos los callbacks de los botones en línea."""
    query = update.callback_query
    await query.answer() # Siempre responde a la query para quitar el "cargando" del botón
    user = query.from_user
    user_id = user.id
    data = query.data

    if data == "planes":
        texto_planes = (
            f"💎 *Planes disponibles:*\n\n"
            f"🔹 *Gratis* – Hasta {FREE_LIMIT_VIDEOS} videos por día.\n\n"
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
            user_data = user_premium.get(user_id, {})
            exp = user_data.get("expire_at").strftime("%Y-%m-%d %H:%M UTC") if user_data.get("expire_at") else "N/A"
            plan_type = PLAN_PAYLOAD_TO_TITLE.get(user_data.get("plan_type"), "Premium")
            await query.message.reply_text(f"✅ Ya tienes el **{escape_for_telegram_markdown(plan_type)}** activo hasta {exp}.", parse_mode="Markdown")
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
            user_data = user_premium.get(user_id, {})
            exp = user_data.get("expire_at").strftime("%Y-%m-%d %H:%M UTC") if user_data.get("expire_at") else "N/A"
            plan_type = PLAN_PAYLOAD_TO_TITLE.get(user_data.get("plan_type"), "Premium")
            await query.message.reply_text(f"✅ Ya tienes el **{escape_for_telegram_markdown(plan_type)}** activo hasta {exp}.", parse_mode="Markdown")
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
        plan_actual = "Gratis"
        expiracion = "N/A"
        
        user_data = user_premium.get(user_id)
        if user_data and isinstance(user_data, dict) and "expire_at" in user_data:
            if user_data["expire_at"] > datetime.now(timezone.utc):
                expiracion = user_data["expire_at"].strftime("%Y-%m-%d %H:%M UTC")
                plan_actual = PLAN_PAYLOAD_TO_TITLE.get(user_data.get("plan_type"), "Premium (Desconocido)")
            else:
                plan_actual = "Gratis (Expirado)"
        
        # Obtener vistas diarias si el usuario no es premium
        vistas_hoy = 0
        if not is_premium(user_id):
            today = str(datetime.utcnow().date())
            vistas_hoy = user_daily_views.get(str(user_id), {}).get(today, 0)
        
        vistas_info = f"• Vistas hoy: {vistas_hoy}/{FREE_LIMIT_VIDEOS}" if not is_premium(user_id) else "• Vistas: Ilimitadas"

        # --- CORRECCIÓN PARA EL BOTÓN PERFIL: ESCAPAR NOMBRES DE USUARIO ---
        escaped_full_name = escape_for_telegram_markdown(user.full_name)
        user_username_display = user.username or 'Sin usuario'
        escaped_username_display = escape_for_telegram_markdown(user_username_display)
        # --- FIN DE LA CORRECCIÓN ---

        await query.message.reply_text(
            f"🧑 Perfil:\n"
            f"• Nombre: {escaped_full_name}\n"
            f"• Usuario: @{escaped_username_display}\n"
            f"• ID: `{user_id}`\n"
            f"• Plan: **{escape_for_telegram_markdown(plan_actual)}**\n" # También escapar el nombre del plan
            f"• Expira: {expiracion}\n"
            f"{vistas_info}",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Volver", callback_data="menu_principal")]]),
            parse_mode="Markdown"
        )

    elif data == "menu_principal":
        # Reemplaza el mensaje actual con el menú principal
        await query.message.edit_text("📋 Menú principal:", reply_markup=get_main_menu())

    elif data == "audio_libros":
        await query.message.reply_text("🎧 Aquí estará el contenido de Audio Libros. ¡Próximamente!")
    elif data == "libro_pdf":
        await query.message.reply_text("📚 Aquí estará el contenido de Libro PDF. ¡Próximamente!")
    elif data == "chat_pedido":
        await query.message.reply_text("💬 Aquí puedes hacer tu pedido en el chat: [Unirse al chat](https://t.me/+6eA7AdRfgq81NzBh)", parse_mode="Markdown")
    elif data == "cursos":
        await query.message.reply_text("🎓 Aquí estarán los cursos disponibles: [Acceder a Clases Digitales](https://t.me/clasesdigitales)", parse_mode="Markdown")
    elif data == "info":
        await query.message.reply_text(
            "ℹ️ Este bot te permite acceder a una amplia variedad de contenido multimedia, "
            "incluyendo películas, series, audiolibros y más.\n\n"
            "Puedes disfrutar de contenido gratuito con un límite diario o adquirir uno de nuestros planes "
            "Premium para acceso ilimitado y beneficios adicionales.\n\n"
            "¡Explora el menú principal para más opciones!",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Volver", callback_data="menu_principal")]])
        )

    # --- Lógica para mostrar el video individual después del paso intermedio ---
    elif data.startswith("show_video_"):
        _, pkg_id = data.rsplit('_', 1)
        
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
                        "🔒 Para ver este contenido debes unirte a nuestros canales de Telegram para continuar:",
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
                logger.warning(f"Error verificando canal ({username}) para video individual: {e}")
                await query.answer("❌ Error al verificar canales. Intenta más tarde.", show_alert=True)
                return

        if can_view_video(user_id):
            await register_view(user_id)
            await query.message.reply_video(
                video=pkg["video_id"],
                caption=f"🎬 *{escape_for_telegram_markdown(pkg['caption'].splitlines()[0])}*", # Título del video
                parse_mode="Markdown",
                protect_content=not is_premium(user_id) # Protege el contenido si no es premium
            )
            try:
                await query.delete_message() # Elimina el mensaje de sinopsis intermedia para limpiar el chat
            except Exception as e:
                logger.warning(f"No se pudo eliminar el mensaje de sinopsis intermedia: {e}")
        else:
            await query.answer("🚫 Has alcanzado tu límite diario de videos. Compra un plan para más acceso.", show_alert=True)
            await query.message.reply_text(
                f"🚫 Has alcanzado tu límite diario de {FREE_LIMIT_VIDEOS} videos.\n"
                "💎 Compra un plan para acceso ilimitado y reenvíos.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("💎 Comprar Planes", callback_data="planes")]]),
            )

    # --- Bloque para listar temporadas de una serie ---
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
            f"📺 Temporadas de *{escape_for_telegram_markdown(serie['title'])}*:",
            reply_markup=InlineKeyboardMarkup(botones),
            parse_mode="Markdown"
        )
        try:
            await query.delete_message()
        except Exception as e:
            logger.warning(f"No se pudo eliminar el mensaje anterior en 'list_temporadas_': {e}")


    # --- Bloque para mostrar capítulos de una temporada específica ---
    elif data.startswith("ver_"):
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
        
        if len(serie.get("temporadas", {})) > 1:
            botones.append([InlineKeyboardButton("🔙 Volver a Temporadas", callback_data=f"list_temporadas_{serie_id}")])
        else: # Si solo hay una temporada, volver al menú principal de la serie (vista de capítulos de la primera temporada)
            botones.append([InlineKeyboardButton("🔙 Volver a Serie", callback_data=f"serie_{serie_id}")])

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
                        "🔒 Para ver este contenido debes unirte a nuestros canales de Telegram para continuar:",
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
                logger.warning(f"Error verificando canal ({username}) para serie: {e}")
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
            
            # Navegación entre temporadas si hay más de una, o volver a la lista de capítulos de la temporada actual
            if len(serie.get("temporadas", {})) > 1:
                    markup_buttons.append([InlineKeyboardButton("🔙 Ver Temporadas", callback_data=f"list_temporadas_{serie_id}")])
            else:
                markup_buttons.append([InlineKeyboardButton("🔙 Ver Capítulos", callback_data=f"ver_{serie_id}_{temporada}")])


            markup = InlineKeyboardMarkup(markup_buttons)

            await query.message.reply_video(
                video=video_id,
                caption=f"📺 *{escape_for_telegram_markdown(serie['title'])}*\n\nTemporada {temporada[1:]} Capítulo {index+1}",
                parse_mode="Markdown",
                reply_markup=markup,
                protect_content=not is_premium(user_id) # Protege el contenido si no es premium
            )
            try:
                await query.delete_message()
            except Exception as e:
                logger.warning(f"No se pudo eliminar el mensaje anterior en 'cap_': {e}")

        else:
            await query.answer("🚫 Has alcanzado tu límite diario de videos. Compra un plan para más acceso.", show_alert=True)
            await query.message.reply_text(
                f"🚫 Has alcanzado tu límite diario de {FREE_LIMIT_VIDEOS} videos.\n"
                "💎 Compra un plan para acceso ilimitado y reenvíos.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("💎 Comprar Planes", callback_data="planes")]]),
            )


# --- Manejo de Pagos ---
async def precheckout_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Maneja la verificación previa al pago."""
    await update.pre_checkout_query.answer(ok=True) # Siempre responde OK para permitir el pago

async def successful_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Maneja los pagos exitosos."""
    user_id = update.effective_user.id
    payload = update.message.successful_payment.invoice_payload
    
    plan_adquirido_title = PLAN_PAYLOAD_TO_TITLE.get(payload, "Plan Desconocido")

    expire_at = datetime.now(timezone.utc) + timedelta(days=30)

    user_premium[user_id] = {"expire_at": expire_at, "plan_type": payload}
    save_data()
    await update.message.reply_text(f"🎉 ¡Gracias por tu compra! Tu **{escape_for_telegram_markdown(plan_adquirido_title)}** se activó por 30 días.", parse_mode="Markdown")

# --- Recepción de Contenido (Sinopsis y Video/Series) ---
async def recibir_foto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handler para recibir una foto con sinopsis (paso previo a enviar video o crear serie)."""
    msg = update.message
    user_id = msg.from_user.id
    if not is_admin(user_id):
        await msg.reply_text("🚫 No tienes permiso para agregar contenido. Este comando es solo para administradores.")
        return

    if msg.photo and msg.caption:
        current_photo[user_id] = {
            "photo_id": msg.photo[-1].file_id,
            "caption": msg.caption,
        }
        await msg.reply_text("✅ Sinopsis recibida. Ahora envía el **video** o usa \/crear_serie para series.", parse_mode="Markdown")
    else:
        await msg.reply_text("❌ Envía una imagen con sinopsis para agregar contenido.")

async def recibir_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handler para recibir un video después de una sinopsis (para contenido individual)."""
    msg = update.message
    user_id = msg.from_user.id
    if not is_admin(user_id):
        await msg.reply_text("🚫 No tienes permiso para agregar contenido. Este comando es solo para administradores.")
        return

    if user_id not in current_photo:
        await msg.reply_text("❌ Primero envía una sinopsis con imagen y descripción usando el comando de subir foto.")
        return

    pkg_id = str(int(datetime.now(timezone.utc).timestamp())) # ID único basado en el tiempo
    photo_id = current_photo[user_id]["photo_id"]
    caption = current_photo[user_id]["caption"]
    video_id = msg.video.file_id

    content_packages[pkg_id] = {
        "photo_id": photo_id,
        "caption": caption,
        "video_id": video_id,
    }
    del current_photo[user_id] # Limpiamos el estado temporal del admin

    save_data()

    bot_info = await context.bot.get_me()
    boton = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "▶️ Ver Contenido", url=f"https://t.me/{bot_info.username}?start=content_{pkg_id}"
                )
            ]
        ]
    )
    
    # Envía el contenido a todos los chats conocidos para difusión
    for chat_id in known_chats:
        try:
            await context.bot.send_photo(
                chat_id=chat_id,
                photo=photo_id,
                caption=caption, # La sinopsis ya debe venir escapada si es contenido de usuario
                reply_markup=boton,
                protect_content=True, # Siempre protege el contenido al difundir
                parse_mode="Markdown",
            )
        except Exception as e:
            logger.warning(f"No se pudo enviar contenido a chat {chat_id}: {e}")

    await msg.reply_text("✅ Contenido enviado a los grupos de difusión.")

# --- Comandos para Gestión de Series ---

async def crear_serie(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Inicia el proceso de creación de una nueva serie."""
    user_id = update.message.from_user.id
    if not is_admin(user_id):
        await update.message.reply_text("🚫 No tienes permiso para crear series. Este comando es solo para administradores.")
        return

    if user_id not in current_photo:
        await update.message.reply_text("❌ Primero envía la sinopsis con imagen y descripción de la serie, luego usa \/crear_serie.", parse_mode="Markdown")
        return
    
    serie_id = str(int(datetime.now(timezone.utc).timestamp()))
    data = current_photo[user_id]
    current_series[user_id] = {
        "serie_id": serie_id,
        "title": data["caption"].split("\n")[0],  # Toma la primera línea del caption como título
        "photo_id": data["photo_id"],
        "caption": data["caption"],
        "temporadas": {}, # Diccionario para almacenar capítulos por temporada
    }
    del current_photo[user_id] # Limpia la foto/caption temporal
    await update.message.reply_text(
        "✅ Serie iniciada. Ahora, usa \/agregar_temporada N (ej. `\/agregar_temporada 1`) para añadir una temporada.",
        parse_mode="Markdown"
    )

async def agregar_temporada(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Añade una nueva temporada a la serie en creación."""
    user_id = update.message.from_user.id
    if not is_admin(user_id):
        await update.message.reply_text("🚫 No tienes permiso para agregar temporadas.")
        return

    if user_id not in current_series:
        await update.message.reply_text("❌ No hay serie en creación. Usa \/crear_serie primero.", parse_mode="Markdown")
        return
    
    args = context.args
    if not args or not args[0].isdigit():
        await update.message.reply_text("❌ Uso: `\/agregar_temporada N` (donde N es el número de temporada, ej. `\/agregar_temporada 1`).", parse_mode="Markdown")
        return
    
    temporada_num = args[0]
    temporada_key = f"T{temporada_num}" # Formato T1, T2, etc.
    serie = current_series[user_id]
    
    if temporada_key in serie["temporadas"]:
        await update.message.reply_text(f"❌ La temporada **{temporada_num}** ya existe. Si quieres añadir capítulos a ella, usa `\/agregar_capitulo {temporada_num}`.", parse_mode="Markdown")
        return
    
    serie["temporadas"][temporada_key] = [] # Inicializa la lista de capítulos para esta temporada
    await update.message.reply_text(
        f"✅ Temporada **{temporada_num}** agregada.\n"
        f"Ahora envía los videos de los capítulos para esta temporada, uno por uno o en un álbum, "
        f"y luego usa el comando `\/agregar_capitulo {temporada_num}` *después de cada envío (o álbum completo)*.",
        parse_mode="Markdown"
    )

async def agregar_capitulo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Añade un capítulo de video a la temporada actual de la serie en creación."""
    user_id = update.message.from_user.id
    if not is_admin(user_id):
        await update.message.reply_text("🚫 No tienes permiso para agregar capítulos.")
        return

    if user_id not in current_series:
        await update.message.reply_text("❌ No hay serie en creación. Usa \/crear_serie primero.", parse_mode="Markdown")
        return
    
    args = context.args
    if not args or not args[0].isdigit():
        await update.message.reply_text("❌ Uso: `\/agregar_capitulo N` donde N es el número de la temporada. Luego envía el\/los video(s).", parse_mode="Markdown")
        return
    
    temporada_num = args[0]
    temporada_key = f"T{temporada_num}"
    serie = current_series[user_id]
    
    if temporada_key not in serie["temporadas"]:
        await update.message.reply_text(f"❌ La temporada **{temporada_num}** no existe para esta serie. Añádela con `\/agregar_temporada {temporada_num}`.", parse_mode="Markdown")
        return
    
    if update.message.video:
        video_id = update.message.video.file_id
        serie["temporadas"][temporada_key].append(video_id)
        current_chapter_count = len(serie["temporadas"][temporada_key])
        await update.message.reply_text(f"✅ Capítulo **{current_chapter_count}** añadido a Temporada **{temporada_num}**. "
                                        f"Envía el siguiente capítulo o usa \/finalizar_serie para publicar.", parse_mode="Markdown")
    else:
        await update.message.reply_text("❌ Por favor, envía un **video** para el capítulo después de usar este comando.", parse_mode="Markdown")

async def finalizar_serie(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Finaliza la creación de la serie y la publica en los canales de difusión."""
    user_id = update.message.from_user.id
    if not is_admin(user_id):
        await update.message.reply_text("🚫 No tienes permiso para finalizar series.")
        return

    if user_id not in current_series:
        await update.message.reply_text("❌ No hay serie en creación para finalizar.")
        return

    serie_data_to_save = current_series[user_id]
    serie_id = serie_data_to_save["serie_id"]

    if not serie_data_to_save["temporadas"]:
        await update.message.reply_text("❌ La serie no tiene temporadas. Añade al menos una con `\/agregar_temporada`.", parse_mode="Markdown")
        return
    
    has_chapters = False
    for temporada_capitulos in serie_data_to_save["temporadas"].values():
        if temporada_capitulos:
            has_chapters = True
            break
    
    if not has_chapters:
        await update.message.reply_text("❌ La serie no tiene capítulos. Añade al menos uno con `\/agregar_capitulo`.", parse_mode="Markdown")
        return

    series_data[serie_id] = serie_data_to_save
    del current_series[user_id] # Limpia el estado temporal del admin
    save_data() # Guarda la serie en Firestore

    bot_info = await context.bot.get_me()
    boton = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "▶️ Ver Serie", url=f"https://t.me/{bot_info.username}?start=serie_{serie_id}"
                )
            ]
        ]
    )

    # Enviar la serie a los chats conocidos
    for chat_id in known_chats:
        try:
            await context.bot.send_photo(
                chat_id=chat_id,
                photo=serie_data_to_save["photo_id"],
                caption=f"📺 *Nueva Serie: {escape_for_telegram_markdown(serie_data_to_save['title'])}*\n\n{escape_for_telegram_markdown(serie_data_to_save['caption'])}",
                reply_markup=boton,
                parse_mode="Markdown",
                protect_content=True,
            )
        except Exception as e:
            logger.warning(f"No se pudo enviar la serie a chat {chat_id}: {e}")

    await update.message.reply_text("✅ Serie finalizada y publicada en los grupos de difusión.")


# --- Funciones y Comandos de Administración de Chats ---
# ADMIN_USER_IDS debe ser una lista de enteros con los IDs de usuario de los administradores
ADMIN_USER_IDS = [int(uid) for uid in os.getenv("ADMIN_IDS", "").split(',') if uid]

def is_admin(user_id):
    """Verifica si un user_id está en la lista de administradores."""
    return user_id in ADMIN_USER_IDS

async def add_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Añade el chat actual a la lista de difusión."""
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("🚫 No tienes permiso para usar este comando.")
        return
    chat_id = str(update.effective_chat.id)
    if chat_id not in known_chats:
        known_chats.add(chat_id)
        save_data()
        await update.message.reply_text(f"✅ Este chat (`{chat_id}`) ha sido añadido a la lista de difusión.", parse_mode="Markdown")
    else:
        await update.message.reply_text(f"Este chat (`{chat_id}`) ya estaba en la lista de difusión.", parse_mode="Markdown")

async def remove_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Elimina el chat actual de la lista de difusión."""
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("🚫 No tienes permiso para usar este comando.")
        return
    chat_id = str(update.effective_chat.id)
    if chat_id in known_chats:
        known_chats.remove(chat_id)
        save_data()
        await update.message.reply_text(f"❌ Este chat (`{chat_id}`) ha sido eliminado de la lista de difusión.", parse_mode="Markdown")
    else:
        await update.message.reply_text(f"Este chat (`{chat_id}`) no estaba en la lista de difusión.", parse_mode="Markdown")

async def list_chats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Lista todos los chats en la lista de difusión."""
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("🚫 No tienes permiso para usar este comando.")
        return
    if known_chats:
        chat_list = "\n".join([f"`{chat_id}`" for chat_id in known_chats])
        await update.message.reply_text(f"Chats en la lista de difusión:\n{chat_list}", parse_mode="Markdown")
    else:
        await update.message.reply_text("No hay chats en la lista de difusión.")

# --- Funciones de Inicio y Apagado del Bot ---
async def on_startup(application: Application):
    """Se ejecuta cuando la aplicación del bot se inicia."""
    logger.info("Cargando datos al iniciar el bot...")
    load_data()
    logger.info("Datos cargados.")

async def on_shutdown(application: Application):
    """Se ejecuta cuando la aplicación del bot se apaga."""
    logger.info("Guardando datos al apagar el bot...")
    save_data()
    logger.info("Datos guardados.")

# --- Función Principal del Bot ---
def main():
    """Configura y ejecuta el bot."""
    # Construir la aplicación del bot, pasando las funciones de inicio y apagado
    application = Application.builder().token(TOKEN).post_init(on_startup).post_shutdown(on_shutdown).build()

    # --- Handlers Públicos (accesibles por cualquier usuario) ---
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CallbackQueryHandler(handle_callback))
    application.add_handler(PreCheckoutQueryHandler(precheckout_handler))
    application.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment))

    # --- Handlers para Contenido (solo para administradores y en chats privados) ---
    # Usamos filters.ChatType.PRIVATE para asegurar que estos comandos solo funcionen en chats 1 a 1 con el bot
    application.add_handler(MessageHandler(filters.PHOTO & filters.ChatType.PRIVATE, recibir_foto))
    application.add_handler(MessageHandler(filters.VIDEO & filters.ChatType.PRIVATE, recibir_video))

    # --- Handlers para Creación de Series (solo para administradores) ---
    application.add_handler(CommandHandler("crear_serie", crear_serie))
    application.add_handler(CommandHandler("agregar_temporada", agregar_temporada))
    application.add_handler(CommandHandler("agregar_capitulo", agregar_capitulo))
    application.add_handler(CommandHandler("finalizar_serie", finalizar_serie))

    # --- Comandos de Administración de Chats (solo accesibles por los ADMIN_USER_IDS) ---
    application.add_handler(CommandHandler("add_chat", add_chat, filters=filters.User(user_id=ADMIN_USER_IDS)))
    application.add_handler(CommandHandler("remove_chat", remove_chat, filters=filters.User(user_id=ADMIN_USER_IDS)))
    application.add_handler(CommandHandler("list_chats", list_chats, filters=filters.User(user_id=ADMIN_USER_IDS)))

    # --- Comandos para forzar carga/guardado de datos (útil para depuración de administradores) ---
    # Estas lambdas permiten aplicar el filtro de admin directamente
    application.add_handler(CommandHandler("load_data", lambda u, c: asyncio.create_task(on_startup(application)) if is_admin(u.effective_user.id) else None))
    application.add_handler(CommandHandler("save_data", lambda u, c: asyncio.create_task(on_shutdown(application)) if is_admin(u.effective_user.id) else None))

    # --- Configurar Webhook para el Despliegue en Render ---
    application.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        url_path="/webhook", # Ruta donde Telegram enviará las actualizaciones
        webhook_url=APP_URL + "/webhook", # URL completa del webhook
        # on_startup y on_shutdown ya NO van aquí, se pasaron al constructor de Application
    )

if __name__ == "__main__":
    main()
