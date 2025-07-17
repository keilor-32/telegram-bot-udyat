import os
import json
import tempfile
import logging
import asyncio
from datetime import datetime, timedelta, timezone
from aiohttp import web # aiohttp se importa para el servidor web si se usa con run_webhook
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LabeledPrice,
    InputMediaVideo, # No se usa directamente en este código, pero puede ser útil para medios
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
import re

# --- Inicializar Firestore con variable de entorno JSON doblemente serializada ---
# Esto asegura que el JSON se cargue correctamente incluso si está escapado.
google_credentials_raw = os.getenv("GOOGLE_APPLICATION_CREDENTIALS_JSON")
if not google_credentials_raw:
    raise ValueError("❌ La variable GOOGLE_APPLICATION_CREDENTIALS_JSON no está configurada.")

# Primero, cargamos la cadena de texto JSON escapada
try:
    google_credentials_str = json.loads(google_credentials_raw)
    # Luego, cargamos la cadena de texto JSON limpia en un diccionario
    google_credentials_dict = json.loads(google_credentials_str)
except json.JSONDecodeError as e:
    raise ValueError(f"❌ Error al decodificar GOOGLE_APPLICATION_CREDENTIALS_JSON: {e}. Asegúrate de que está doblemente serializado.")

# Guardar temporalmente las credenciales en un archivo para firebase_admin
with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as temp:
    json.dump(google_credentials_dict, temp)
    temp_path = temp.name

cred = credentials.Certificate(temp_path)
firebase_admin.initialize_app(cred)
db = firestore.client()

print("✅ Firestore inicializado correctamente.")

# --- Configuración ---
TOKEN = os.getenv("TOKEN")
PROVIDER_TOKEN = os.getenv("PROVIDER_TOKEN", "") # Opcional si no usas pagos
APP_URL = os.getenv("APP_URL")
PORT = int(os.getenv("PORT", "8080"))

if not TOKEN:
    raise ValueError("❌ ERROR: La variable de entorno TOKEN no está configurada.")
if not APP_URL:
    raise ValueError("❌ ERROR: La variable de entorno APP_URL no está configurada.")

# --- Logging ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# --- Variables en memoria ---
user_premium = {}             # {user_id: {"expire_at": datetime, "plan_type": "payload_del_plan"}}
user_daily_views = {}         # {user_id: {date: count}}
content_packages = {}         # {pkg_id: {photo_id, caption, video_id}} (para películas/videos individuales)
known_chats = set()           # IDs de chat donde se deben publicar los contenidos
current_photo = {}            # {user_id: {"photo_id", "caption"}} para cuando se envía la sinopsis
user_verified = {}            # {user_id: True} si el usuario ya se verificó con los canales

# Series: {serie_id: {"title", "photo_id", "caption", "temporadas": {T1: [video_id, ...], T2: [...], ...}}}
series_data = {}
# Estado temporal para la creación de series: {user_id: {"serie_id", "title", "photo_id", "caption", "temporadas": {}, "current_temporada_key"}}
current_series = {}

# --- Firestore colecciones ---
COLLECTION_USERS = "users_premium"
COLLECTION_VIDEOS = "videos" # Para películas/videos individuales
COLLECTION_VIEWS = "user_daily_views"
COLLECTION_CHATS = "known_chats"
COLLECTION_SERIES = "series_data" # Para series
COLLECTION_VERIFIED_USERS = "verified_users"

# --- Funciones Firestore (Síncronas para carga/guardado en memoria, luego se usan en el bot) ---
def save_user_premium_firestore():
    batch = db.batch()
    for uid, exp_data in user_premium.items():
        doc_ref = db.collection(COLLECTION_USERS).document(str(uid))
        data_to_save = exp_data.copy()
        if "expire_at" in data_to_save and isinstance(data_to_save["expire_at"], datetime):
            # Asegúrate de que expire_at sea aware antes de guardarlo como ISO
            if data_to_save["expire_at"].tzinfo is None:
                data_to_save["expire_at"] = data_to_save["expire_at"].replace(tzinfo=timezone.utc)
            data_to_save["expire_at"] = data_to_save["expire_at"].isoformat()
        batch.set(doc_ref, data_to_save)
    batch.commit()
    logger.info("Datos de usuarios premium guardados.")

def load_user_premium_firestore():
    docs = db.collection(COLLECTION_USERS).stream()
    result = {}
    for doc in docs:
        data = doc.to_dict()
        try:
            if "expire_at" in data and isinstance(data["expire_at"], str):
                # Al cargar, siempre interpreta como UTC y hazlo aware
                loaded_dt = datetime.fromisoformat(data["expire_at"])
                if loaded_dt.tzinfo is None: # Si fromisoformat no le puso tzinfo (Python < 3.11 para ISO sin Z)
                    loaded_dt = loaded_dt.replace(tzinfo=timezone.utc)
                data["expire_at"] = loaded_dt
            result[int(doc.id)] = data
        except Exception as e:
            logger.error(f"Error cargando datos premium para {doc.id}: {e}")
            pass
    logger.info(f"Cargados {len(result)} usuarios premium.")
    return result

def save_videos_firestore():
    batch = db.batch()
    for pkg_id, content in content_packages.items():
        doc_ref = db.collection(COLLECTION_VIDEOS).document(pkg_id)
        batch.set(doc_ref, content)
    batch.commit()
    logger.info(f"Guardados {len(content_packages)} videos individuales.")

def load_videos_firestore():
    docs = db.collection(COLLECTION_VIDEOS).stream()
    result = {}
    for doc in docs:
        result[doc.id] = doc.to_dict()
    logger.info(f"Cargados {len(result)} videos individuales.")
    return result

def save_user_daily_views_firestore():
    batch = db.batch()
    for uid, views in user_daily_views.items():
        doc_ref = db.collection(COLLECTION_VIEWS).document(uid)
        batch.set(doc_ref, views)
    batch.commit()
    logger.info("Datos de vistas diarias guardados.")

def load_user_daily_views_firestore():
    docs = db.collection(COLLECTION_VIEWS).stream()
    result = {}
    for doc in docs:
        data = doc.to_dict()
        result[doc.id] = data # Las claves de fecha ya serán strings, no es necesario procesar
    logger.info(f"Cargados {len(result)} registros de vistas diarias.")
    return result

def save_known_chats_firestore():
    doc_ref = db.collection(COLLECTION_CHATS).document("chats")
    doc_ref.set({"chat_ids": list(known_chats)})
    logger.info(f"Guardados {len(known_chats)} chats conocidos.")

def load_known_chats_firestore():
    doc_ref = db.collection(COLLECTION_CHATS).document("chats")
    doc = doc_ref.get()
    if doc.exists:
        data = doc.to_dict()
        loaded_chats = set(data.get("chat_ids", []))
        logger.info(f"Cargados {len(loaded_chats)} chats conocidos.")
        return loaded_chats
    logger.info("No se encontraron chats conocidos en Firestore.")
    return set()

def save_series_firestore():
    batch = db.batch()
    for serie_id, serie in series_data.items():
        doc_ref = db.collection(COLLECTION_SERIES).document(serie_id)
        batch.set(doc_ref, serie)
    batch.commit()
    logger.info(f"Guardadas {len(series_data)} series.")

def load_series_firestore():
    docs = db.collection(COLLECTION_SERIES).stream()
    result = {}
    for doc in docs:
        result[doc.id] = doc.to_dict()
    logger.info(f"Cargadas {len(result)} series.")
    return result

def save_user_verified_firestore():
    batch = db.batch()
    for uid, verified_status in user_verified.items():
        doc_ref = db.collection(COLLECTION_VERIFIED_USERS).document(str(uid))
        batch.set(doc_ref, {"verified": verified_status})
    batch.commit()
    logger.info(f"Guardados {len(user_verified)} usuarios verificados.")

def load_user_verified_firestore():
    docs = db.collection(COLLECTION_VERIFIED_USERS).stream()
    result = {}
    for doc in docs:
        data = doc.to_dict()
        result[int(doc.id)] = data.get("verified", False)
    logger.info(f"Cargados {len(result)} usuarios verificados.")
    return result

# --- Guardar y cargar todo (funciones principales) ---
def save_data():
    """Guarda todos los datos en Firestore."""
    save_user_premium_firestore()
    save_videos_firestore()
    save_user_daily_views_firestore()
    save_known_chats_firestore()
    save_series_firestore()
    save_user_verified_firestore()
    logger.info("Todos los datos guardados en Firestore.")

def load_data():
    """Carga todos los datos de Firestore al inicio del bot."""
    global user_premium, content_packages, user_daily_views, known_chats, series_data, user_verified
    user_premium = load_user_premium_firestore()
    content_packages = load_videos_firestore()
    user_daily_views = load_user_daily_views_firestore()
    known_chats = load_known_chats_firestore()
    series_data = load_series_firestore()
    user_verified = load_user_verified_firestore()
    logger.info("Todos los datos cargados de Firestore.")

# --- Funciones de administración adicionales (para el problema de "eliminar videos") ---
async def delete_all_videos_firestore_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Comando de administración para eliminar todos los documentos de la colección 'videos'.
    Solo accesible para los ADMIN_IDS.
    """
    user_id = update.effective_user.id
    ADMIN_IDS = [5603774849, 6505701831] # <--- ¡IMPORTANTE! Tus IDs de administrador

    if user_id not in ADMIN_IDS:
        await update.message.reply_text("❌ No tienes permisos para usar este comando.")
        return

    logger.info(f"Admin {user_id} iniciando eliminación de todos los videos individuales...")
    try:
        # Recupera los IDs de los videos para borrar el caché local
        video_ids_to_delete = list(content_packages.keys())
        
        docs = db.collection(COLLECTION_VIDEOS).stream()
        deleted_count = 0
        for doc in docs:
            doc.reference.delete()
            deleted_count += 1
            logger.info(f"Documento '{doc.id}' eliminado de Firestore.")
        
        # Limpia el caché en memoria
        content_packages.clear()

        # Guarda el estado vacío en Firestore (aunque no es estrictamente necesario, asegura consistencia)
        save_videos_firestore() 

        await update.message.reply_text(f"✅ Se eliminaron {deleted_count} videos individuales de la base de datos (y de la memoria del bot).")
        logger.info(f"✅ Se eliminaron {deleted_count} documentos de la colección '{COLLECTION_VIDEOS}'.")
    except Exception as e:
        logger.error(f"❌ Error al eliminar documentos de la colección '{COLLECTION_VIDEOS}': {e}")
        await update.message.reply_text(f"❌ Error al intentar eliminar videos: {e}")

# --- Planes ---
FREE_LIMIT_VIDEOS = 3 # Límite de videos para usuarios gratis

PREMIUM_ITEM = {
    "title": "Plan Premium",
    "description": "Acceso y reenvíos ilimitados por 30 días.",
    "payload": "premium_plan",
    "currency": "XTR", # Moneda para pagos (ej. "USD", "EUR", "XTR" para Telegram Stars)
    "prices": [LabeledPrice("Premium por 30 días", 1)], # Precio en la unidad más pequeña de la moneda (ej. 1 centavo para USD)
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

# --- Funciones de Utilidad ---
def escape_markdown_v2(text):
    """
    Helper function to escape characters for MarkdownV2.
    Required for Telegram's parse_mode="MarkdownV2" if text contains special characters.
    """
    if not isinstance(text, str):
        return str(text) # Asegurarse de que sea una cadena
    escape_chars = r'_*[]()~`>#+-=|{}.!'
    return re.sub(f'([{re.escape(escape_chars)}])', r'\\\1', text)

# --- Control acceso ---
def is_premium(user_id):
    """Verifica si un usuario tiene un plan premium activo."""
    if user_id in user_premium and "expire_at" in user_premium[user_id]:
        now_utc = datetime.now(timezone.utc) # Obtiene la hora actual en UTC y la hace aware
        return user_premium[user_id]["expire_at"] > now_utc # Compara datetimes aware
    return False

def get_user_plan_name(user_id):
    """Devuelve el nombre del plan actual del usuario."""
    if is_premium(user_id):
        plan_data = user_premium.get(user_id, {})
        plan_type = plan_data.get("plan_type", PREMIUM_ITEM["payload"]) # Default a "premium_plan" si no está definido
        if plan_type == PLAN_PRO_ITEM["payload"]:
            return PLAN_PRO_ITEM["title"]
        elif plan_type == PLAN_ULTRA_ITEM["payload"]:
            return PLAN_ULTRA_ITEM["title"]
        else: # Si el payload es "premium_plan" o desconocido, se asume el Premium básico
            return PREMIUM_ITEM["title"]
    return "Gratis"

def can_view_video(user_id):
    """Determina si un usuario puede ver un video (basado en plan o límite diario)."""
    if is_premium(user_id):
        return True
    today = str(datetime.now(timezone.utc).date()) # Usa datetime.now(timezone.utc).date() para la clave de fecha
    return user_daily_views.get(str(user_id), {}).get(today, 0) < FREE_LIMIT_VIDEOS

async def register_view(user_id):
    """Registra una vista de video para el usuario y guarda los datos."""
    today = str(datetime.now(timezone.utc).date())
    uid = str(user_id)
    if uid not in user_daily_views:
        user_daily_views[uid] = {}
    user_daily_views[uid][today] = user_daily_views[uid].get(today, 0) + 1
    save_data() # Guarda los cambios a Firestore
    logger.info(f"Vista registrada para el usuario {user_id}. Vistas hoy: {user_daily_views[uid][today]}")

# --- Canales para verificación (puedes añadir más si es necesario) ---
CHANNELS = {
    "supertvw2": "@Supertvw2", # Ejemplo: "nombre_interno": "@UsernameDelCanal"
    "fullvvd": "@fullvvd",
}

# --- Menú principal ---
def get_main_menu():
    """Devuelve el teclado de menú principal con botones inline."""
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
                InlineKeyboardButton("🎬 Peliculas", callback_data="peliculas_menu"), # Callback para menú interno
                InlineKeyboardButton("📺 Series", callback_data="list_series"),      # Callback para menú interno
            ],
            [
                InlineKeyboardButton("💎 Planes", callback_data="planes"),
            ],
            [
                InlineKeyboardButton("🧑 Perfil", callback_data="perfil"),
            ],
            [
                InlineKeyboardButton("ℹ️ Info", callback_data="info_hades"), # Callback para información del bot
                InlineKeyboardButton("❓ Soporte", url="https://t.me/Hsito"),
            ],
        ]
    )

# --- Handlers ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Maneja el comando /start. Responde con el menú de verificación o el menú principal."""
    args = context.args
    user_id = update.effective_user.id
    username = update.effective_user.username or "N/A"
    full_name = update.effective_user.full_name

    logger.info(f"Comando /start recibido de user_id: {user_id}, username: @{username}, name: {full_name}")

    # Manejo de deep linking para contenido individual
    if args and args[0].startswith("content_"):
        pkg_id = args[0].split("_")[1]
        pkg = content_packages.get(pkg_id)
        if not pkg:
            await update.message.reply_text("❌ Contenido no disponible o eliminado.")
            logger.warning(f"Intento de acceso a contenido no existente: {pkg_id} por {user_id}")
            return

        boton_ver_video = InlineKeyboardMarkup(
            [[InlineKeyboardButton("▶️ Ver Video", callback_data=f"show_video_{pkg_id}")]]
        )
        await update.message.reply_photo(
            photo=pkg["photo_id"],
            caption=escape_markdown_v2(pkg["caption"]), # Escapar caption para MarkdownV2
            reply_markup=boton_ver_video,
            parse_mode="MarkdownV2"
        )
        logger.info(f"Enviada sinopsis para contenido {pkg_id} a {user_id}.")
        return

    # Manejo de deep linking para series
    elif args and args[0].startswith("serie_"):
        serie_id = args[0].split("_", 1)[1]
        serie = series_data.get(serie_id)
        if not serie:
            await update.message.reply_text("❌ Serie no encontrada.")
            logger.warning(f"Intento de acceso a serie no existente: {serie_id} por {user_id}")
            return
        
        temporada_keys = sorted(serie.get("temporadas", {}).keys())
        
        if not temporada_keys:
            await update.message.reply_text("❌ Esta serie no tiene capítulos disponibles.")
            logger.info(f"Serie {serie_id} sin capítulos por {user_id}.")
            return

        # Mostrar capítulos de la primera temporada por defecto
        first_temporada_key = temporada_keys[0]
        capitulos = serie["temporadas"][first_temporada_key]
        
        botones = []
        row = []
        for i, _ in enumerate(capitulos):
            row.append(InlineKeyboardButton(f"{i+1}", callback_data=f"cap_{serie_id}_{first_temporada_key}_{i}"))
            if len(row) == 5: # 5 botones por fila
                botones.append(row)
                row = []
        if row: # Añadir la última fila si no está completa
            botones.append(row)
        
        # Añadir botón para ver todas las temporadas si hay más de una
        if len(temporada_keys) > 1:
            botones.append([InlineKeyboardButton("🔙 Ver Temporadas", callback_data=f"list_temporadas_{serie_id}")])

        await update.message.reply_text(
            f"📺 *{escape_markdown_v2(serie['title'])}*\n\n{escape_markdown_v2(serie['caption'])}\n\nCapítulos de la Temporada {first_temporada_key[1:]}:",
            reply_markup=InlineKeyboardMarkup(botones),
            parse_mode="MarkdownV2",
            disable_web_page_preview=True,
        )
        logger.info(f"Enviada lista de capítulos para serie {serie_id} (Temporada {first_temporada_key}) a {user_id}.")
        return

    # Si no es un deep link, verificar suscripción
    if user_verified.get(user_id):
        await update.message.reply_text("📋 Menú principal:", reply_markup=get_main_menu())
        logger.info(f"Usuario {user_id} ya verificado, mostrando menú principal.")
        return

    # Si el usuario no está verificado, pedir que se una a los canales
    await update.message.reply_text(
        "👋 Hola! Para usar nuestro bot, debes unirte a todos nuestros canales.\n"
        "Una vez te hayas unido, haz clic en *Verificar suscripción* para continuar.",
        reply_markup=InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("🔗 Unirse a Supertv", url=f"https://t.me/{CHANNELS['supertvw2'][1:]}"),
                    InlineKeyboardButton("🔗 Unirse a fullvvd", url=f"https://t.me/{CHANNELS['fullvvd'][1:]}"),
                ],
                [InlineKeyboardButton("✅ Verificar suscripción", callback_data="verify")],
            ]
        ),
        parse_mode="MarkdownV2"
    )
    logger.info(f"Usuario {user_id} no verificado, pidiendo verificación.")


async def verify(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Maneja el callback de verificación de suscripción a canales."""
    query = update.callback_query
    await query.answer() # Siempre responde a la callback query
    user_id = query.from_user.id
    logger.info(f"Verificando suscripción para el usuario {user_id}.")

    not_joined = []
    for name, username in CHANNELS.items():
        try:
            member = await context.bot.get_chat_member(chat_id=username, user_id=user_id)
            if member.status not in ["member", "administrator", "creator"]:
                not_joined.append(username)
        except Exception as e:
            logger.error(f"Error verificando canal '{username}' para {user_id}: {e}")
            not_joined.append(username) # Si hay un error, asumimos que no está unido para ser seguros

    if not not_joined:
        user_verified[user_id] = True
        save_data()
        await query.edit_message_text("✅ Verificación completada. Menú disponible:")
        await query.message.reply_text("📋 Menú principal:", reply_markup=get_main_menu())
        logger.info(f"Usuario {user_id} verificado exitosamente.")
    else:
        channels_str = "\n".join([escape_markdown_v2(ch) for ch in not_joined])
        await query.edit_message_text(f"❌ Aún no estás suscrito a:\n{channels_str}", parse_mode="MarkdownV2")
        logger.warning(f"Usuario {user_id} no pudo verificar suscripción a canales: {not_joined}")


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Maneja todas las callback queries del bot."""
    query = update.callback_query
    await query.answer() # Responde a la callback query, puede ser con un mensaje emergente o silencioso
    user = query.from_user
    user_id = user.id
    data = query.data
    logger.info(f"Callback query recibida de {user_id}: {data}")

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
        await query.message.reply_text(texto_planes, parse_mode="MarkdownV2", reply_markup=botones_planes)

    elif data in ["comprar_pro", "comprar_ultra"]:
        item_to_buy = None
        if data == "comprar_pro":
            item_to_buy = PLAN_PRO_ITEM
        elif data == "comprar_ultra":
            item_to_buy = PLAN_ULTRA_ITEM
        
        if not item_to_buy:
            await query.message.reply_text("❌ Plan no reconocido.")
            logger.warning(f"Intento de compra de plan no reconocido: {data} por {user_id}.")
            return

        if is_premium(user_id):
            exp_data = user_premium.get(user_id, {})
            # Asegura que expire_at es aware antes de formatear
            exp_dt = exp_data.get("expire_at", datetime.min.replace(tzinfo=timezone.utc))
            exp_str = exp_dt.strftime("%Y-%m-%d %H:%M:%S UTC")
            plan_name = get_user_plan_name(user_id)
            await query.message.reply_text(f"✅ Ya tienes el plan *{escape_markdown_v2(plan_name)}* activo hasta {escape_markdown_v2(exp_str)}.", parse_mode="MarkdownV2")
            logger.info(f"Usuario {user_id} intentó comprar plan, pero ya tiene {plan_name} activo.")
            return
        
        if not PROVIDER_TOKEN:
            await query.message.reply_text("❌ Lo siento, los pagos no están configurados en este momento. Intenta más tarde o contacta al soporte.")
            logger.error("PROVIDER_TOKEN no está configurado. No se pueden procesar pagos.")
            return

        try:
            await context.bot.send_invoice(
                chat_id=query.message.chat_id,
                title=item_to_buy["title"],
                description=item_to_buy["description"],
                payload=item_to_buy["payload"],
                provider_token=PROVIDER_TOKEN,
                currency=item_to_buy["currency"],
                prices=item_to_buy["prices"],
                start_parameter=f"buy-{item_to_buy['payload']}",
            )
            logger.info(f"Factura de pago enviada para plan {item_to_buy['payload']} a {user_id}.")
        except Exception as e:
            await query.message.reply_text("❌ Error al procesar el pago. Por favor, inténtalo de nuevo más tarde.")
            logger.error(f"Error al enviar factura para {user_id} ({item_to_buy['payload']}): {e}")

    elif data == "perfil":
        plan_name = get_user_plan_name(user_id)
        exp_data = user_premium.get(user_id, {})
        exp = exp_data.get("expire_at")
        
        escaped_plan_name = escape_markdown_v2(plan_name)
        exp_display = exp.strftime('%Y-%m-%d %H:%M:%S UTC') if exp else 'N/A'
        
        await query.message.reply_text(
            f"🧑 Perfil:\n"
            f"• Nombre: {escape_markdown_v2(user.full_name)}\n"
            f"• Usuario: @{escape_markdown_v2(user.username or 'Sin usuario')}\n"
            f"• ID: `{user_id}`\n" # ID en code block
            f"• Plan: *{escaped_plan_name}*\n"
            f"• Expira: {escape_markdown_v2(exp_display)}",
            parse_mode="MarkdownV2",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Volver", callback_data="planes")]]),
        )
        logger.info(f"Perfil mostrado para {user_id}.")

    elif data == "menu_principal":
        await query.message.reply_text("📋 Menú principal:", reply_markup=get_main_menu())
        logger.info(f"Volviendo al menú principal para {user_id}.")

    elif data == "peliculas_menu":
        # Este es un callback para un menú interno de películas.
        await query.message.reply_text("🎬 Aquí podrás explorar nuestro catálogo de películas. ¡Próximamente más contenido!",
                                       reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Volver", callback_data="menu_principal")]])
                                      )
        logger.info(f"Menú de películas mostrado a {user_id}.")
        # Aquí podrías añadir botones para diferentes géneros, listar películas, etc.
    
    elif data == "list_series":
        # Construye un menú con las series existentes
        if not series_data:
            await query.message.reply_text("📺 Actualmente no hay series disponibles. ¡Vuelve pronto!",
                                           reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Volver", callback_data="menu_principal")]])
                                          )
            logger.info(f"No hay series disponibles para {user_id}.")
            return
        
        botones_series = []
        for serie_id, serie in series_data.items():
            button_title = serie["title"]
            if len(button_title) > 30: 
                button_title = button_title[:27] + "..."
            botones_series.append(
                [InlineKeyboardButton(f"📺 {escape_markdown_v2(button_title)}", callback_data=f"serie_{serie_id}")]
            )
        botones_series.append([InlineKeyboardButton("🔙 Volver al Menú", callback_data="menu_principal")])
        await query.message.reply_text("📺 Explora nuestras series:", reply_markup=InlineKeyboardMarkup(botones_series))
        logger.info(f"Lista de series mostrada a {user_id}.")

    elif data == "info_hades":
        await query.message.reply_text("ℹ️ Este bot fue creado por *Hades*.\n\nContáctalo para soporte o desarrollo de bots personalizados.", 
                                       parse_mode="MarkdownV2",
                                       reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Volver", callback_data="menu_principal")]])
                                      )
        logger.info(f"Info Hades mostrada a {user_id}.")

    elif data.startswith("show_video_"):
        prefix, pkg_id = data.rsplit('_', 1) # Asegura que solo se divide por el último '_'
        logger.info(f"Solicitud para mostrar video individual {pkg_id} de {user_id}.")
        
        pkg = content_packages.get(pkg_id)
        if not pkg:
            await query.message.reply_text("❌ Video no disponible o eliminado.")
            logger.warning(f"Video individual {pkg_id} no encontrado para {user_id}.")
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
                        parse_mode="MarkdownV2"
                    )
                    logger.warning(f"Usuario {user_id} no unido al canal {username} para ver video {pkg_id}.")
                    return
            except Exception as e:
                logger.error(f"Error verificando canal '{username}' para video {pkg_id} de {user_id}: {e}")
                await query.answer("❌ Error al verificar canales. Intenta más tarde.", show_alert=True)
                return

        if can_view_video(user_id):
            await register_view(user_id)
            try:
                await query.message.reply_video(
                    video=pkg["video_id"],
                    caption=f"🎬 *{escape_markdown_v2(pkg['caption'].splitlines()[0])}*", # Solo la primera línea del caption como título
                    parse_mode="MarkdownV2",
                    protect_content=not is_premium(user_id) # Protege si no es premium
                )
                logger.info(f"Video {pkg_id} enviado a {user_id}. Protegido: {not is_premium(user_id)}.")
            except Exception as e:
                logger.error(f"Error al enviar video {pkg_id} a {user_id}: {e}")
                await query.message.reply_text("❌ No se pudo enviar el video. Puede que esté dañado o el ID sea incorrecto.")

            # Intenta eliminar el mensaje de sinopsis si es posible
            try:
                await query.delete_message()
                logger.debug(f"Mensaje de sinopsis para {pkg_id} eliminado.")
            except Exception as e:
                logger.warning(f"No se pudo eliminar el mensaje de sinopsis intermedia para {user_id}: {e}")
        else:
            await query.answer("🚫 Has alcanzado tu límite diario de videos. Compra un plan para más acceso.", show_alert=True)
            await query.message.reply_text(
                f"🚫 Has alcanzado tu límite diario de {FREE_LIMIT_VIDEOS} videos\\.\n"
                "💎 Compra un plan para más acceso y reenvíos ilimitados\\.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("💎 Comprar Planes", callback_data="planes")]]),
                parse_mode="MarkdownV2"
            )
            logger.info(f"Usuario {user_id} superó el límite de vistas diarias para video {pkg_id}.")

    elif data.startswith("serie_"): # Este callback se usa para mostrar los capítulos de la primera temporada o para ir al menú de temporadas
        _, serie_id = data.split("_", 1) # Asegura que solo se divide por el primer '_'
        serie = series_data.get(serie_id)
        if not serie:
            await query.message.reply_text("❌ Serie no encontrada.")
            logger.warning(f"Serie {serie_id} no encontrada en callback de lista de temporadas.")
            return
        
        temporada_keys = sorted(serie.get("temporadas", {}).keys())
        if not temporada_keys:
            await query.message.reply_text("❌ Esta serie no tiene capítulos disponibles.")
            logger.info(f"Serie {serie_id} sin capítulos.")
            return

        # Si solo hay una temporada, la muestra directamente. Si hay varias, ofrece el menú de temporadas.
        if len(temporada_keys) == 1:
            first_temporada_key = temporada_keys[0]
            capitulos = serie["temporadas"][first_temporada_key]
            
            botones = []
            row = []
            for i, _ in enumerate(capitulos):
                row.append(InlineKeyboardButton(f"{i+1}", callback_data=f"cap_{serie_id}_{first_temporada_key}_{i}"))
                if len(row) == 5:
                    botones.append(row)
                    row = []
            if row:
                botones.append(row)
            
            botones.append([InlineKeyboardButton("🔙 Volver a Series", callback_data="list_series")])

            await query.message.reply_text(
                f"📺 *{escape_markdown_v2(serie['title'])}*\n\n{escape_markdown_v2(serie['caption'])}\n\nCapítulos de la Temporada {first_temporada_key[1:]}:",
                reply_markup=InlineKeyboardMarkup(botones),
                parse_mode="MarkdownV2",
                disable_web_page_preview=True,
            )
            logger.info(f"Mostrando capítulos de T{first_temporada_key[1:]} para serie {serie_id} a {user_id}.")

        else: # Múltiples temporadas, muestra el menú de temporadas
            await query.message.reply_text(f"📺 *{escape_markdown_v2(serie['title'])}*\n\n{escape_markdown_v2(serie['caption'])}", 
                                           parse_mode="MarkdownV2", disable_web_page_preview=True)
            
            botones = []
            for temporada_key in sorted(serie.get("temporadas", {}).keys()):
                botones.append(
                    [InlineKeyboardButton(f"Temporada {temporada_key[1:]}", callback_data=f"ver_{serie_id}_{temporada_key}")]
                )
            
            botones.append([InlineKeyboardButton("🔙 Volver a Series", callback_data="list_series")])
            await query.message.reply_text(
                f"📺 Temporadas de *{escape_markdown_v2(serie['title'])}*:",
                reply_markup=InlineKeyboardMarkup(botones),
                parse_mode="MarkdownV2"
            )
            logger.info(f"Mostrando menú de temporadas para serie {serie_id} a {user_id}.")
        
        try:
            await query.delete_message()
        except Exception as e:
            logger.warning(f"No se pudo eliminar el mensaje anterior en 'serie_': {e}")


    elif data.startswith("list_temporadas_"): # Este se usa cuando el usuario explícitamente pide "Ver Temporadas"
        _, serie_id = data.split("_", 2) # Asegura que solo se divide por el segundo '_'
        serie = series_data.get(serie_id)
        if not serie:
            await query.message.reply_text("❌ Serie no encontrada.")
            logger.warning(f"Serie {serie_id} no encontrada en callback list_temporadas_.")
            return

        botones = []
        for temporada_key in sorted(serie.get("temporadas", {}).keys()):
            botones.append(
                [InlineKeyboardButton(f"Temporada {temporada_key[1:]}", callback_data=f"ver_{serie_id}_{temporada_key}")]
            )
        
        botones.append([InlineKeyboardButton("🔙 Volver a Series", callback_data="list_series")])

        await query.message.reply_text(
            f"📺 Temporadas de *{escape_markdown_v2(serie['title'])}*:",
            reply_markup=InlineKeyboardMarkup(botones),
            parse_mode="MarkdownV2"
        )
        try:
            await query.delete_message()
        except Exception as e:
            logger.warning(f"No se pudo eliminar el mensaje anterior en 'list_temporadas_': {e}")


    elif data.startswith("ver_"): # Este se usa para ver los capítulos de una temporada específica
        _, serie_id, temporada = data.split("_", 2)
        serie = series_data.get(serie_id)
        if not serie or temporada not in serie.get("temporadas", {}):
            await query.message.reply_text("❌ Temporada no disponible.")
            logger.warning(f"Temporada {temporada} de serie {serie_id} no disponible en callback ver_.")
            return

        capitulos = serie["temporadas"][temporada]
        if not capitulos:
            await query.message.reply_text(f"❌ La Temporada {temporada[1:]} no tiene capítulos.")
            logger.info(f"Temporada {temporada} de serie {serie_id} sin capítulos.")
            return

        botones = []
        row = []
        for i, _ in enumerate(capitulos):
            row.append(InlineKeyboardButton(f"{i+1}", callback_data=f"cap_{serie_id}_{temporada}_{i}"))
            if len(row) == 5:
                botones.append(row)
                row = []
        if row:
            botones.append(row)
        
        botones.append([InlineKeyboardButton("🔙 Volver a Temporadas", callback_data=f"list_temporadas_{serie_id}")])

        await query.message.reply_text(
            f"📺 Capítulos de Temporada {temporada[1:]} de *{escape_markdown_v2(serie['title'])}*:",
            reply_markup=InlineKeyboardMarkup(botones),
            parse_mode="MarkdownV2"
        )
        try:
            await query.delete_message()
        except Exception as e:
            logger.warning(f"No se pudo eliminar el mensaje anterior en 'ver_': {e}")


    elif data.startswith("cap_"): # Este se usa para ver un capítulo específico y navegar
        _, serie_id, temporada, index_str = data.split("_")
        index = int(index_str)
        serie = series_data.get(serie_id)
        if not serie or temporada not in serie.get("temporadas", {}):
            await query.message.reply_text("❌ Capítulo no disponible (Serie o Temporada no encontrada).")
            logger.warning(f"Capítulo no disponible: Serie {serie_id} o Temporada {temporada} no encontrada para {user_id}.")
            return

        capitulos = serie["temporadas"][temporada]
        total_capitulos = len(capitulos)
        if index < 0 or index >= total_capitulos:
            await query.message.reply_text("❌ Capítulo fuera de rango.")
            logger.warning(f"Capítulo fuera de rango: index {index} para serie {serie_id}, temporada {temporada} (total {total_capitulos}).")
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
                        parse_mode="MarkdownV2"
                    )
                    logger.warning(f"Usuario {user_id} no unido al canal {username} para ver capítulo de serie {serie_id}.")
                    return
            except Exception as e:
                logger.error(f"Error verificando canal '{username}' para capítulo de serie {serie_id} de {user_id}: {e}")
                await query.answer("❌ Error al verificar canales. Intenta más tarde.", show_alert=True)
                return

        if can_view_video(user_id):
            await register_view(user_id)
            video_id = capitulos[index]

            botones_navegacion = []
            if index > 0:
                botones_navegacion.append(InlineKeyboardButton("⬅️ Anterior", callback_data=f"cap_{serie_id}_{temporada}_{index - 1}"))
            if index < total_capitulos - 1:
                botones_navegacion.append(InlineKeyboardButton("➡️ Siguiente", callback_data=f"cap_{serie_id}_{temporada}_{index + 1}"))
            
            markup_buttons = [botones_navegacion]
            
            # Botón para volver a la lista de capítulos de la temporada
            markup_buttons.append([InlineKeyboardButton("🔙 Ver Capítulos", callback_data=f"ver_{serie_id}_{temporada}")])
            markup = InlineKeyboardMarkup(markup_buttons)

            try:
                await query.message.reply_video(
                    video=video_id,
                    caption=f"📺 *{escape_markdown_v2(serie['title'])}*\n\nTemporada {temporada[1:]} Capítulo {index+1}/{total_capitulos}",
                    parse_mode="MarkdownV2",
                    reply_markup=markup,
                    protect_content=not is_premium(user_id)
                )
                logger.info(f"Capítulo {index+1} de Temporada {temporada[1:]} de serie {serie_id} enviado a {user_id}.")
            except Exception as e:
                logger.error(f"Error al enviar capítulo de serie {serie_id} (cap {index+1}) a {user_id}: {e}")
                await query.message.reply_text("❌ No se pudo enviar el capítulo. Puede que esté dañado o el ID sea incorrecto.")
            
            try:
                await query.delete_message()
            except Exception as e:
                logger.warning(f"No se pudo eliminar el mensaje anterior en 'cap_': {e}")

        else:
            await query.answer("🚫 Has alcanzado tu límite diario de videos. Compra un plan para más acceso.", show_alert=True)
            await query.message.reply_text(
                f"🚫 Has alcanzado tu límite diario de {FREE_LIMIT_VIDEOS} videos\\.\n"
                "💎 Compra un plan para más acceso y reenvíos ilimitados\\.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("💎 Comprar Planes", callback_data="planes")]]),
                parse_mode="MarkdownV2"
            )
            logger.info(f"Usuario {user_id} superó el límite de vistas diarias para capítulo de serie {serie_id}.")


# --- Pagos ---
async def precheckout_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Maneja las consultas de pre-checkout antes de un pago."""
    query = update.pre_checkout_query
    logger.info(f"Pre-checkout query de {query.from_user.id}: {query.invoice_payload}")
    await query.answer(ok=True) # Siempre responde OK si el pago es válido desde la perspectiva del bot

async def successful_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Maneja los pagos exitosos."""
    user_id = update.effective_user.id
    payload = update.message.successful_payment.invoice_payload
    currency = update.message.successful_payment.currency
    total_amount = update.message.successful_payment.total_amount
    
    expire_at = datetime.now(timezone.utc) + timedelta(days=30) # 30 días de suscripción
    user_premium[user_id] = {
        "expire_at": expire_at,
        "plan_type": payload
    }
    save_data() # Guarda el estado premium en Firestore
    
    plan_name = get_user_plan_name(user_id) # Usa la función para obtener el nombre real del plan

    await update.message.reply_text(
        f"🎉 ¡Gracias por tu compra! Tu plan *{escape_markdown_v2(plan_name)}* se activó por 30 días\\. "
        f"Monto: {total_amount / 100} {currency}\\.", # Dividir por 100 para mostrar el valor real
        parse_mode="MarkdownV2"
    )
    logger.info(f"Pago exitoso de {user_id} para plan '{payload}'.")

# --- Recepción contenido (sinopsis + video) ---
async def recibir_foto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Recibe una foto con caption (sinopsis). Almacena temporalmente la foto y el caption.
    Prepara para recibir un video individual o iniciar una serie.
    """
    msg = update.message
    user_id = msg.from_user.id
    username = msg.from_user.username or "N/A"
    ADMIN_IDS = [5603774849, 6505701831] # <--- ¡IMPORTANTE! Tus IDs de administrador

    if user_id not in ADMIN_IDS:
        await msg.reply_text("❌ No tienes permisos para añadir contenido.")
        logger.warning(f"Intento de añadir foto/sinopsis sin permisos de {user_id}.")
        return

    if msg.photo and msg.caption:
        photo_id = msg.photo[-1].file_id # Obtiene la ID de la foto de mayor resolución
        caption = msg.caption
        
        current_photo[user_id] = {
            "photo_id": photo_id,
            "caption": caption,
        }
        await msg.reply_text(
            "✅ Sinopsis con imagen recibida.\n"
            "Ahora:\n"
            "1. Envía el *video* para crear un contenido *individual* (película).\n"
            "2. Usa el comando `/crear_serie` para empezar a añadir capítulos de una *serie*.",
            parse_mode="MarkdownV2"
        )
        logger.info(f"Sinopsis recibida de {user_id}. Photo ID: {photo_id[:10]}..., Caption: {caption[:30]}...")
    else:
        await msg.reply_text("❌ Envía una *imagen con una descripción (caption)* para la sinopsis\\.", parse_mode="MarkdownV2")
        logger.warning(f"Mensaje no válido para sinopsis de {user_id}. No es foto con caption.")

async def recibir_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Recibe un video. Determina si es para un contenido individual o un capítulo de serie.
    """
    msg = update.message
    user_id = msg.from_user.id
    username = msg.from_user.username or "N/A"
    ADMIN_IDS = [5603774849, 6505701831] # <--- ¡IMPORTANT! Tus IDs de administrador

    if user_id not in ADMIN_IDS:
        await msg.reply_text("❌ No tienes permisos para añadir contenido.")
        logger.warning(f"Intento de añadir video sin permisos de {user_id}.")
        return

    if not msg.video:
        await msg.reply_text("❌ Esto no es un video. Por favor, envía un archivo de video.")
        logger.warning(f"Mensaje no válido para video de {user_id}. No es un video.")
        return

    video_id = msg.video.file_id
    logger.info(f"Video recibido de {user_id}. Video ID: {video_id[:10]}...")

    # 1. Si hay una serie en progreso para este usuario
    if user_id in current_series:
        serie_data = current_series[user_id]
        if "current_temporada_key" not in serie_data or serie_data["current_temporada_key"] not in serie_data["temporadas"]:
            await msg.reply_text("❌ No se ha seleccionado una temporada activa para añadir capítulos. Usa /agregar_temporada [número].")
            logger.warning(f"Usuario {user_id} intentó añadir capítulo sin temporada activa para serie {serie_data.get('serie_id')}.")
            return
        
        temporada_key = serie_data["current_temporada_key"]
        
        # Agrega el video al final de la lista de capítulos de la temporada actual
        serie_data["temporadas"][temporada_key].append(video_id)
        
        await msg.reply_text(
            f"✅ Capítulo {len(serie_data['temporadas'][temporada_key])} agregado a la Temporada {temporada_key[1:]} de la serie '{escape_markdown_v2(serie_data['title'])}'.\n"
            "Envía más videos para esta temporada o usa /finalizar_serie para guardar y publicar la serie.",
            parse_mode="MarkdownV2"
        )
        logger.info(f"Capítulo agregado a serie {serie_data['serie_id']}, Temporada {temporada_key} por {user_id}.")
        return # Salir, ya que el video fue manejado como parte de una serie

    # 2. Si no hay serie en progreso, se asume que es un video individual
    if user_id not in current_photo:
        await msg.reply_text("❌ Primero envía una *sinopsis con imagen* para crear contenido individual\\.", parse_mode="MarkdownV2")
        logger.warning(f"Usuario {user_id} intentó enviar video individual sin sinopsis previa.")
        return

    # Procesa como video individual
    pkg_id = str(int(datetime.now(timezone.utc).timestamp())) # ID único basado en timestamp
    photo_id = current_photo[user_id]["photo_id"]
    caption = current_photo[user_id]["caption"]
    
    content_packages[pkg_id] = {
        "photo_id": photo_id,
        "caption": caption,
        "video_id": video_id,
    }
    del current_photo[user_id] # Limpia el estado temporal de la foto

    save_data() # Guarda el nuevo video en Firestore

    # Crea el botón de deep link para el contenido
    boton = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "▶️ Ver Contenido", url=f"https://t.me/{(await context.bot.get_me()).username}?start=content_{pkg_id}"
                )
            ]
        ]
    )
    
    # Publica el nuevo contenido en los chats conocidos
    if not known_chats:
        await msg.reply_text(
            "✅ Contenido individual guardado, pero no hay grupos configurados para su publicación.\n"
            "Usa `/add_chat` en un grupo para añadirlo a la lista de difusión.",
            parse_mode="MarkdownV2"
        )
        logger.warning(f"Video individual {pkg_id} guardado, pero no hay chats conocidos para publicar.")
    else:
        for chat_id in known_chats:
            try:
                await context.bot.send_photo(
                    chat_id=chat_id,
                    photo=photo_id,
                    caption=escape_markdown_v2(caption),
                    reply_markup=boton,
                    protect_content=True, # Protege la sinopsis en grupos
                    parse_mode="MarkdownV2"
                )
                logger.info(f"Contenido {pkg_id} enviado a chat {chat_id}.")
            except Exception as e:
                logger.error(f"No se pudo enviar contenido {pkg_id} a chat {chat_id}: {e}")

        await msg.reply_text(f"✅ Contenido individual enviado a {len(known_chats)} grupos.")
        logger.info(f"Video individual {pkg_id} procesado y publicado por {user_id}.")

# --- Comandos para la administración de series ---

async def crear_serie(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Comando para iniciar la creación de una nueva serie (requiere sinopsis+foto previa)."""
    user_id = update.message.from_user.id
    ADMIN_IDS = [5603774849, 6505701831] # <--- ¡IMPORTANTE! Tus IDs de administrador

    if user_id not in ADMIN_IDS:
        await update.message.reply_text("❌ No tienes permisos para crear series.")
        return

    if user_id not in current_photo:
        await update.message.reply_text("❌ Primero envía la *sinopsis con imagen* para la serie\\.", parse_mode="MarkdownV2")
        return
    
    serie_id = str(int(datetime.now(timezone.utc).timestamp()))
    data = current_photo[user_id]
    
    current_series[user_id] = {
        "serie_id": serie_id,
        "title": data["caption"].split("\n")[0], # Asume que el título es la primera línea del caption
        "photo_id": data["photo_id"],
        "caption": data["caption"],
        "temporadas": {}, # Inicializa el diccionario de temporadas para esta nueva serie
        "current_temporada_key": None # No hay temporada activa inicialmente
    }
    del current_photo[user_id] # Limpia la foto actual después de usarla para la serie
    
    await update.message.reply_text(
        "✅ Serie iniciada. Ahora:\n"
        "1. Usa el comando `/agregar_temporada [número]` (ej. `/agregar_temporada 1`) para añadir una temporada\\.\n"
        "2. Envía los *videos* para cada capítulo de esa temporada\\.",
        parse_mode="MarkdownV2"
    )
    logger.info(f"Serie '{current_series[user_id]['title']}' iniciada por {user_id}.")

async def agregar_temporada(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Comando para añadir o seleccionar una temporada para la serie en creación."""
    user_id = update.message.from_user.id
    ADMIN_IDS = [5603774849, 6505701831] # <--- ¡IMPORTANTE! Tus IDs de administrador

    if user_id not in ADMIN_IDS:
        await update.message.reply_text("❌ No tienes permisos para agregar temporadas.")
        return

    if user_id not in current_series:
        await update.message.reply_text("❌ No hay serie en creación. Usa `/crear_serie` primero\\.", parse_mode="MarkdownV2")
        return
    
    args = context.args
    if not args or not args[0].isdigit():
        await update.message.reply_text("❌ Uso: `/agregar_temporada N`, donde `N` es el número de temporada (ej. `/agregar_temporada 1`).", parse_mode="MarkdownV2")
        return
    
    temporada_num = args[0]
    temporada_key = f"T{temporada_num}" # Ejemplo: "T1", "T2"

    serie_data_in_progress = current_series[user_id]
    
    if temporada_key not in serie_data_in_progress["temporadas"]:
        serie_data_in_progress["temporadas"][temporada_key] = [] # Inicializa la lista de capítulos
        await update.message.reply_text(f"✅ Temporada {temporada_num} creada para la serie '{escape_markdown_v2(serie_data_in_progress['title'])}'.")
    else:
        await update.message.reply_text(f"✅ Reanudando Temporada {temporada_num} para la serie '{escape_markdown_v2(serie_data_in_progress['title'])}'.")
    
    current_series[user_id]["current_temporada_key"] = temporada_key # Guarda la clave de la temporada actual
    await update.message.reply_text("Envía los *videos* para añadir los capítulos a esta temporada\\.", parse_mode="MarkdownV2")
    logger.info(f"Temporada {temporada_key} seleccionada/creada para serie '{serie_data_in_progress['title']}' por {user_id}.")


async def finalizar_serie(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Comando para finalizar la creación de la serie y guardarla permanentemente."""
    user_id = update.message.from_user.id
    ADMIN_IDS = [5603774849, 6505701831] # <--- ¡IMPORTANTE! Tus IDs de administrador

    if user_id not in ADMIN_IDS:
        await update.message.reply_text("❌ No tienes permisos para finalizar series.")
        return

    if user_id not in current_series:
        await update.message.reply_text("❌ No hay serie en creación para finalizar.")
        return

    serie_to_save = current_series[user_id]
    
    # Verificar si hay temporadas o capítulos agregados
    if not serie_to_save["temporadas"] or all(not caps for caps in serie_to_save["temporadas"].values()):
        await update.message.reply_text("❌ La serie no tiene ninguna temporada o capítulo agregado. No se guardará. Usa `/crear_serie` y `/agregar_temporada` para empezar de nuevo.", parse_mode="MarkdownV2")
        del current_series[user_id] # Limpiar datos incompletos
        logger.warning(f"Serie '{serie_to_save.get('title', 'N/A')}' de {user_id} no finalizada: sin temporadas o capítulos.")
        return

    # Guarda la serie en la base de datos de series
    series_data[serie_to_save["serie_id"]] = {
        "title": serie_to_save["title"],
        "photo_id": serie_to_save["photo_id"],
        "caption": serie_to_save["caption"],
        "temporadas": serie_to_save["temporadas"],
    }
    
    del current_series[user_id] # Limpia el estado de creación para el usuario
    save_data() # Guarda los datos actualizados de todas las colecciones

    # Botón de deep link para la serie recién creada
    boton = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "▶️ Ver Serie", url=f"https://t.me/{(await context.bot.get_me()).username}?start=serie_{serie_to_save['serie_id']}"
                )
            ]
        ]
    )

    # Envía la notificación de la nueva serie a los chats conocidos
    if not known_chats:
        await update.message.reply_text(
            "✅ Serie guardada, pero no hay grupos configurados para su publicación.\n"
            "Usa `/add_chat` en un grupo para añadirlo a la lista de difusión.",
            parse_mode="MarkdownV2"
        )
        logger.warning(f"Serie {serie_to_save['serie_id']} guardada, pero no hay chats conocidos para publicar.")
    else:
        for chat_id in known_chats:
            try:
                await context.bot.send_photo(
                    chat_id=chat_id,
                    photo=serie_to_save["photo_id"],
                    caption=f"✨ ¡Nueva Serie: *{escape_markdown_v2(serie_to_save['title'])}*!\n\n{escape_markdown_v2(serie_to_save['caption'])}",
                    reply_markup=boton,
                    protect_content=True, # Protege la sinopsis de la serie en grupos
                    parse_mode="MarkdownV2"
                )
                logger.info(f"Serie {serie_to_save['serie_id']} enviada a chat {chat_id}.")
            except Exception as e:
                logger.error(f"No se pudo enviar la notificación de la serie {serie_to_save['serie_id']} a {chat_id}: {e}")

        await update.message.reply_text(
            f"✅ Serie '{escape_markdown_v2(serie_to_save['title'])}' guardada y publicada en {len(known_chats)} grupos.",
            reply_markup=boton,
            parse_mode="MarkdownV2"
        )
        logger.info(f"Serie {serie_to_save['serie_id']} finalizada y publicada por {user_id}.")

# --- Comandos Admin (para añadir/eliminar chats) ---
async def add_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Añade el chat actual a la lista de chats conocidos (solo para administradores)."""
    user_id = update.effective_user.id
    ADMIN_IDS = [5603774849, 6505701831] # <--- ¡IMPORTANTE! Tus IDs de administrador

    if user_id not in ADMIN_IDS:
        await update.message.reply_text("❌ No tienes permisos para usar este comando.")
        return

    chat_id = update.effective_chat.id
    chat_title = update.effective_chat.title or f"Chat ID: {chat_id}"
    if chat_id < 0: # Es un grupo o canal
        if chat_id not in known_chats:
            known_chats.add(chat_id)
            save_data() # Guarda la lista actualizada de chats
            await update.message.reply_text(f"✅ Chat '{escape_markdown_v2(chat_title)}' (`{chat_id}`) añadido a la lista de difusión.", parse_mode="MarkdownV2")
            logger.info(f"Chat {chat_id} ('{chat_title}') añadido por {user_id}.")
        else:
            await update.message.reply_text(f"ℹ️ El chat '{escape_markdown_v2(chat_title)}' (`{chat_id}`) ya estaba en la lista de difusión.", parse_mode="MarkdownV2")
            logger.info(f"Intento de añadir chat {chat_id} por {user_id}, ya estaba en la lista.")
    else:
        await update.message.reply_text("❌ Este comando solo funciona en grupos o canales.")
        logger.warning(f"Comando /add_chat usado en chat privado por {user_id}.")

async def remove_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Elimina el chat actual de la lista de chats conocidos (solo para administradores)."""
    user_id = update.effective_user.id
    ADMIN_IDS = [5603774849, 6505701831] # <--- ¡IMPORTANTE! Tus IDs de administrador

    if user_id not in ADMIN_IDS:
        await update.message.reply_text("❌ No tienes permisos para usar este comando.")
        return

    chat_id = update.effective_chat.id
    chat_title = update.effective_chat.title or f"Chat ID: {chat_id}"
    if chat_id < 0: # Es un grupo o canal
        if chat_id in known_chats:
            known_chats.remove(chat_id)
            save_data() # Guarda la lista actualizada de chats
            await update.message.reply_text(f"✅ Chat '{escape_markdown_v2(chat_title)}' (`{chat_id}`) eliminado de la lista de difusión.", parse_mode="MarkdownV2")
            logger.info(f"Chat {chat_id} ('{chat_title}') eliminado por {user_id}.")
        else:
            await update.message.reply_text(f"❌ Este chat '{escape_markdown_v2(chat_title)}' (`{chat_id}`) no estaba en la lista de difusión.", parse_mode="MarkdownV2")
            logger.info(f"Intento de eliminar chat {chat_id} por {user_id}, no estaba en la lista.")
    else:
        await update.message.reply_text("❌ Este comando solo funciona en grupos o canales.")
        logger.warning(f"Comando /remove_chat usado en chat privado por {user_id}.")

# --- Funciones de Webhook (para Render.com y python-telegram-bot) ---
# Esta función es la que el servidor web de aiohttp usará para procesar las actualizaciones
async def handle_webhook(request):
    """Maneja las peticiones webhook entrantes de Telegram."""
    update_data = await request.json()
    # Pasa la actualización a la aplicación de python-telegram-bot
    update = Update.de_json(update_data, application.bot)
    await application.process_update(update)
    return web.Response(status=200) # Telegram espera un 200 OK para saber que la actualización fue recibida

async def set_webhook_func(app_instance: Application):
    """Establece el webhook del bot de Telegram a la URL de la aplicación."""
    webhook_url = APP_URL + "/webhook"
    current_webhook_info = await app_instance.bot.get_webhook_info()
    if current_webhook_info.url != webhook_url:
        await app_instance.bot.set_webhook(url=webhook_url)
        logger.info(f"✅ Webhook establecido en: {webhook_url}")
    else:
        logger.info(f"✅ Webhook ya configurado correctamente en: {webhook_url}")


# --- Función Principal ---
def main():
    """Inicia el bot y el servidor webhook."""
    # Crea la Application y pasa el token de tu bot.
    global application # Se declara global para que `handle_webhook` pueda acceder a ella
    application = Application.builder().token(TOKEN).build()

    # --- Registra todos los Handlers ---
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CallbackQueryHandler(handle_callback))
    application.add_handler(PreCheckoutQueryHandler(precheckout_handler))
    application.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment))

    # Manejadores para añadir contenido (películas/videos individuales)
    # filters.PHOTO & filters.CAPTION: Asegura que es una foto Y tiene un caption (texto).
    # filters.ChatType.PRIVATE: Importante, solo permite recibir fotos con sinopsis en chat privado con el bot.
    application.add_handler(MessageHandler(filters.PHOTO & filters.CAPTION & filters.ChatType.PRIVATE, recibir_foto))
    # filters.VIDEO: Asegura que es un video. Este handler maneja videos tanto para series como individuales.
    # filters.ChatType.PRIVATE: Para que solo responda a videos en el chat privado del bot.
    application.add_handler(MessageHandler(filters.VIDEO & filters.ChatType.PRIVATE, recibir_video))

    # Comandos para la administración de series
    application.add_handler(CommandHandler("crear_serie", crear_serie))
    application.add_handler(CommandHandler("agregar_temporada", agregar_temporada))
    application.add_handler(CommandHandler("finalizar_serie", finalizar_serie))

    # Comandos de administración de chats y eliminación de videos
    application.add_handler(CommandHandler("add_chat", add_chat))
    application.add_handler(CommandHandler("remove_chat", remove_chat))
    application.add_handler(CommandHandler("admin_delete_videos", delete_all_videos_firestore_command)) # Nuevo comando admin

    # --- INICIALIZACIÓN Y CONFIGURACIÓN DEL WEBHOOK ---
    # `application.initialize()` prepara el bot para operaciones de red.
    asyncio.run(application.initialize()) 
    logger.info("Aplicación de Telegram Bot inicializada.")

    # Establece el webhook en Telegram. Esto solo necesita hacerse una vez.
    asyncio.run(set_webhook_func(application))

    # Inicia el servidor webhook. Este es un método bloqueante que correrá indefinidamente.
    logger.info(f"Iniciando servidor webhook en http://0.0.0.0:{PORT}/webhook")
    application.run_webhook(
        listen="0.0.0.0", # Escucha en todas las interfaces de red
        port=PORT,         # Puerto de escucha, tomado de las variables de entorno
        url_path="/webhook", # Ruta URL que Telegram usará para enviar actualizaciones
        # Ya no se necesita 'on_startup' porque set_webhook_func ya se llamó
        # También, 'webhook_url' aquí es opcional si ya se estableció con set_webhook.
        # Lo mantengo para claridad, pero el que importa es el de set_webhook_func.
        webhook_url=APP_URL + "/webhook", 
    )

if __name__ == "__main__":
    load_data() # Carga todos los datos de Firestore al iniciar el script
    main() # Inicia el bot
