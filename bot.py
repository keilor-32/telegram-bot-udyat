import os
import json
import tempfile
import logging
import asyncio
# Importar timezone junto con datetime y timedelta
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

# Asegurarse de que el JSON se parsea correctamente
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
user_premium = {}             # {user_id: expire_at datetime}
user_daily_views = {}         # {user_id: {date: count}}
content_packages = {}         # {pkg_id: {photo_id, caption, video_id}}
known_chats = set()
current_photo = {}
user_verified = {}            # {user_id: True} si el usuario ya se verificó

# NUEVO: series con temporadas y capítulos
series_data = {}              # {serie_id: {"title", "photo_id", "caption", "temporadas": {T1: [video_id, ...], ...}}}
current_series = {}           # {user_id: {"title", "photo_id", "caption", "serie_id", "temporada", "capitulos": []}}

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
    for uid, exp in user_premium.items():
        doc_ref = db.collection(COLLECTION_USERS).document(str(uid))
        # Almacenar en formato ISO con información de zona horaria (Z para UTC)
        batch.set(doc_ref, {"expire_at": exp.isoformat()})
    batch.commit()

def load_user_premium_firestore():
    docs = db.collection(COLLECTION_USERS).stream()
    result = {}
    for doc in docs:
        data = doc.to_dict()
        try:
            # datetime.fromisoformat() puede parsear fechas conscientes si la cadena las incluye
            expire_at = datetime.fromisoformat(data.get("expire_at"))
            result[int(doc.id)] = expire_at
        except Exception:
            # En caso de error de parseo o si la fecha no tiene tzinfo, se ignora o se maneja
            # una buena práctica sería loggear el error para investigar
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
    "prices": [LabeledPrice("Plan Ultra por 30 días", 1)],
}

# --- Control acceso ---
def is_premium(user_id):
    # Comparar una fecha consciente con una fecha consciente
    # datetime.now(timezone.utc) crea una fecha consciente de la zona horaria (UTC)
    return user_id in user_premium and user_premium[user_id] > datetime.now(timezone.utc)

def can_view_video(user_id):
    if is_premium(user_id):
        return True
    # Almacenar y comparar la fecha como una cadena simple para daily views
    today = str(datetime.now(timezone.utc).date()) # Usar date() para solo la fecha
    return user_daily_views.get(str(user_id), {}).get(today, 0) < FREE_LIMIT_VIDEOS

async def register_view(user_id):
    today = str(datetime.now(timezone.utc).date()) # Usar date() para solo la fecha
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
            f"📺 *{serie['title']}*\n\n{serie['caption']}\n\nCapítulos de la Temporada {first_temporada_key[1:]}:",
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
            f"• ID: {user_id}\n• Plan: {plan}\n• Expira: {exp.strftime('%Y-%m-%d %H:%M:%S %Z') if exp else 'N/A'}", # Modificado para mostrar la zona horaria
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
                caption=f"🎬 *{pkg['caption'].splitlines()[0]}*",
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
            f"📺 Temporadas de *{serie['title']}*:",
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
                caption=f"📺 *{serie['title']}*\n\nTemporada {temporada[1:]} Capítulo {index+1}",
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
    if payload in [PREMIUM_ITEM["payload"], PLAN_PRO_ITEM["payload"], PLAN_ULTRA_ITEM["payload"]]:
        # Asegurarse de que el expire_at también sea consciente de la zona horaria (UTC)
        expire_at = datetime.now(timezone.utc) + timedelta(days=30)
        user_premium[user_id] = expire_at
        save_data()
        await update.message.reply_text("🎉 ¡Gracias por tu compra! Tu plan se activó por 30 días.")

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

    pkg_id = str(int(datetime.now(timezone.utc).timestamp())) # Usar datetime.now(timezone.utc)
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
                caption=caption,
                reply_markup=boton,
                protect_content=True,
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
    serie_id = str(int(datetime.now(timezone.utc).timestamp())) # Usar datetime.now(timezone.utc)
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
    # Guardamos temporada actual para agregar los videos que vengan después
    current_series[user_id]["current_season_for_chapters"] = temporada_key

# --- Manejar álbumes de video para capítulos de series ---
async def handle_video_album(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    user_id = msg.from_user.id

    if user_id not in current_series or "current_season_for_chapters" not in current_series[user_id]:
        # Si no hay una serie en edición o no se espera un capítulo, ignorar o responder apropiadamente
        await msg.reply_text("❌ No se está esperando un capítulo de serie en este momento. Usa /crear_serie para empezar o /agregar_temporada para añadir una temporada.")
        return

    temporada_key = current_series[user_id]["current_season_for_chapters"]
    serie = current_series[user_id]

    if not msg.video:
        await msg.reply_text("❌ Por favor, envía videos.")
        return

    # Si es parte de un álbum, msg.media_group_id estará presente
    # Pero aquí solo recibimos un video a la vez del álbum.
    # El `telegram.ext.MessageHandler(filters.VIDEO & filters.AS_ALBUM, handle_video_album)`
    # ya se encargaría de procesar cada video del álbum.
    
    video_id = msg.video.file_id
    serie["temporadas"][temporada_key].append(video_id)

    # Si el álbum ha terminado (lo sabríamos por telegram.ext, o si no hay más media_group_id)
    # y si no estamos en un grupo de medios, es decir, el último video del álbum fue enviado o es un solo video
    if not msg.media_group_id:
        # Guardar la serie en Firestore solo después de que todos los capítulos de la temporada han sido enviados o si es un solo video
        series_data[serie["serie_id"]] = serie
        save_data()
        await msg.reply_text(
            f"✅ Capítulo(s) añadido(s) a la Temporada {temporada_key[1:]}. "
            f"Capítulos totales en esta temporada: {len(serie['temporadas'][temporada_key])}.\n"
            "Usa /finalizar_serie para publicar o /agregar_temporada para otra temporada."
        )
        # Eliminar la clave temporal para evitar añadir videos incorrectamente
        if "current_season_for_chapters" in current_series[user_id]:
            del current_series[user_id]["current_season_for_chapters"]

# --- Finalizar creación de serie y publicarla ---
async def finalizar_serie(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    if user_id not in current_series:
        await update.message.reply_text("❌ No hay serie en creación para finalizar.")
        return

    serie = current_series[user_id]
    serie_id = serie["serie_id"]

    if not serie["temporadas"]:
        await update.message.reply_text("❌ La serie no tiene temporadas. Añade al menos una con /agregar_temporada.")
        return
    
    # Verificar que al menos una temporada tenga capítulos
    has_chapters = any(len(capitulos) > 0 for capitulos in serie["temporadas"].values())
    if not has_chapters:
        await update.message.reply_text("❌ La serie no tiene capítulos. Asegúrate de agregar videos a tus temporadas.")
        return

    # Mover la serie de `current_series` a `series_data` y guardarla
    series_data[serie_id] = serie
    del current_series[user_id]
    save_data()

    # Botón para la serie
    boton_serie = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "Ver Serie", url=f"https://t.me/{(await context.bot.get_me()).username}?start=serie_{serie_id}"
                )
            ]
        ]
    )

    # Publicar la sinopsis de la serie en todos los chats conocidos
    for chat_id in known_chats:
        try:
            await context.bot.send_photo(
                chat_id=chat_id,
                photo=serie["photo_id"],
                caption=f"🎬 ¡Nueva Serie Disponible!\n\n*{serie['title']}*\n\n{serie['caption']}",
                reply_markup=boton_serie,
                parse_mode="Markdown",
                protect_content=True,
            )
        except Exception as e:
            logger.warning(f"No se pudo enviar la serie a {chat_id}: {e}")

    await update.message.reply_text(f"✅ Serie '{serie['title']}' publicada y guardada.")


# --- Comandos administrativos ---
async def añadir_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    known_chats.add(chat_id)
    save_data()
    await update.message.reply_text(f"✅ Este chat ({chat_id}) ha sido añadido para envíos de contenido.")

async def eliminar_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    if chat_id in known_chats:
        known_chats.remove(chat_id)
        save_data()
        await update.message.reply_text(f"❌ Este chat ({chat_id}) ha sido eliminado de los envíos de contenido.")
    else:
        await update.message.reply_text("Este chat no estaba en la lista de chats conocidos.")

async def mostrar_chats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if known_chats:
        chat_list = "\n".join(known_chats)
        await update.message.reply_text(f"Chats conocidos:\n{chat_list}")
    else:
        await update.message.reply_text("No hay chats conocidos.")


# --- Main ---
async def main():
    # Cargar datos al inicio
    load_data()
    logger.info(f"Datos cargados. Usuarios premium: {len(user_premium)}, Contenido: {len(content_packages)}")
    logger.info(f"Series cargadas: {len(series_data)}")
    logger.info(f"Chats conocidos: {known_chats}")
    logger.info(f"Usuarios verificados: {len(user_verified)}")

    application = Application.builder().token(TOKEN).build()

    # Handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CallbackQueryHandler(handle_callback))
    application.add_handler(PreCheckoutQueryHandler(precheckout_handler))
    application.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment))
    application.add_handler(CallbackQueryHandler(verify, pattern="^verify$"))

    # Handlers de administración de contenido (solo para el admin)
    # Considera añadir un filtro por ID de usuario para estos comandos
    application.add_handler(CommandHandler("anadir_chat", añadir_chat))
    application.add_handler(CommandHandler("eliminar_chat", eliminar_chat))
    application.add_handler(CommandHandler("mostrar_chats", mostrar_chats))
    application.add_handler(MessageHandler(filters.PHOTO & filters.CaptionPresent, recibir_foto))
    application.add_handler(MessageHandler(filters.VIDEO, recibir_video))

    # NUEVOS: Handlers para series
    application.add_handler(CommandHandler("crear_serie", crear_serie))
    application.add_handler(CommandHandler("agregar_temporada", agregar_temporada))
    application.add_handler(CommandHandler("agregar_capitulo", agregar_capitulo))
    # Manejar videos enviados después de /agregar_capitulo
    # Si quieres manejar álbumes, la lógica de `handle_video_album` necesitaría más refinamiento
    # para coleccionar todos los videos de un media_group_id antes de procesar.
    # Por ahora, con filters.VIDEO, se procesa cada video individualmente.
    application.add_handler(MessageHandler(filters.VIDEO & ~filters.FORWARDED, handle_video_album))
    application.add_handler(CommandHandler("finalizar_serie", finalizar_serie))


    # Start the bot
    if APP_URL:
        # Modo webhook
        await application.bot.set_webhook(url=APP_URL)
        app = web.Application()
        app.router.add_post("/", application.webhook_handler)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", PORT)
        await site.start()
        logger.info(f"Webhook escuchando en puerto {PORT}")
        # Mantener el servidor en ejecución
        while True:
            await asyncio.sleep(3600) # Dormir por una hora
    else:
        # Modo polling (para desarrollo local)
        logger.info("Iniciando bot en modo polling...")
        await application.run_polling()


if __name__ == "__main__":
    asyncio.run(main())
