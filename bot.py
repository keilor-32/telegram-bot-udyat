import os
import json
import tempfile
import logging
import asyncio
from datetime import datetime, timedelta, timezone # <-- ¡IMPORTANTE! Se añadió timezone
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
import re

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
user_premium = {}             # {user_id: {"expire_at": datetime, "plan_type": "payload_del_plan"}}
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
COLLECTION_SERIES = "series_data"
COLLECTION_VERIFIED_USERS = "verified_users"

# --- Funciones Firestore (Síncronas) ---
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
        data = doc.to_dict()
        result[doc.id] = data # Las claves de fecha ya serán strings, no es necesario procesar
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


# --- Guardar y cargar todo ---
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
    "description": "Videos ilimitados y reenvíos ilimitados, sin restricciones.",
    "payload": "plan_ultra",
    "currency": "XTR",
    "prices": [LabeledPrice("Plan Ultra por 30 días", 100)],
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
        now_utc = datetime.now(timezone.utc) # Obtiene la hora actual en UTC y la hace aware
        return user_premium[user_id]["expire_at"] > now_utc # Compara datetimes aware
    return False

def get_user_plan_name(user_id):
    if is_premium(user_id):
        plan_data = user_premium.get(user_id, {})
        plan_type = plan_data.get("plan_type", "premium_plan")
        if plan_type == PLAN_PRO_ITEM["payload"]:
            return PLAN_PRO_ITEM["title"]
        elif plan_type == PLAN_ULTRA_ITEM["payload"]:
            return PLAN_ULTRA_ITEM["title"]
        else:
            return PREMIUM_ITEM["title"]
    return "Gratis"

def can_view_video(user_id):
    if is_premium(user_id):
        return True
    today = str(datetime.now(timezone.utc).date()) # Usa datetime.now(timezone.utc)
    return user_daily_views.get(str(user_id), {}).get(today, 0) < FREE_LIMIT_VIDEOS

async def register_view(user_id):
    today = str(datetime.now(timezone.utc).date()) # Usa datetime.now(timezone.utc)
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
            # Primera fila: Películas | Series
            [
                InlineKeyboardButton("🎬 Películas", callback_data="peliculas_menu"), # Nuevo botón para películas
                InlineKeyboardButton("📺 Series", callback_data="list_series"),      # Nuevo botón para series
            ],
            # Segunda fila: Audiolibros | Libro PDF
            [
                InlineKeyboardButton("🎧 Audiolibros", callback_data="audio_libros"),
                InlineKeyboardButton("📚 Libro PDF", callback_data="libro_pdf"),
            ],
            # Tercera fila: Chat Pedido | Cursos
            [
                InlineKeyboardButton("💬 Chat Pedido", callback_data="chat_pedido"),
                InlineKeyboardButton("🎓 Cursos", callback_data="cursos"),
            ],
            # Cuarta fila: Planes
            [
                InlineKeyboardButton("💎 Planes", callback_data="planes"),
            ],
            # Quinta fila: Perfil
            [
                InlineKeyboardButton("🧑 Perfil", callback_data="perfil"),
            ],
            # Sexta fila: Info | Soporte
            [
                InlineKeyboardButton("ℹ️ Info", callback_data="info_hades"), # Cambié el callback_data para que sea más específico
                InlineKeyboardButton("❓ Soporte", url="https://t.me/Hsito"),
            ],
        ]
    )

# --- Handlers ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    user_id = update.effective_user.id

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
        
        if len(temporada_keys) > 1:
            botones.append([InlineKeyboardButton("🔙 Ver Temporadas", callback_data=f"list_temporadas_{serie_id}")])

        await update.message.reply_text(
            f"📺 *{escape_markdown_v2(serie['title'])}*\n\n{escape_markdown_v2(serie['caption'])}\n\nCapítulos de la Temporada {first_temporada_key[1:]}:",
            reply_markup=InlineKeyboardMarkup(botones),
            parse_mode="Markdown",
            disable_web_page_preview=True,
        )
        return

    if user_verified.get(user_id):
        await update.message.reply_text("📋 Menú principal:", reply_markup=get_main_menu())
        return

    await update.message.reply_text(
        "👋 hola debes unirte a todos nuestros canales para usar nuestro bot, una ves te hayas unido has click  en verificar suscripcion para continuar.",
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
            not_joined.append(username)

    if not not_joined:
        user_verified[user_id] = True
        save_data()
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
            exp = exp_data.get("expire_at", datetime.min.replace(tzinfo=timezone.utc)).strftime("%Y-%m-%d") # Asegura datetime aware para format
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
            exp = exp_data.get("expire_at", datetime.min.replace(tzinfo=timezone.utc)).strftime("%Y-%m-%d") # Asegura datetime aware para format
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
        
        escaped_plan_name = escape_markdown_v2(plan_name)
        
        await query.message.reply_text(
            f"🧑 Perfil:\n• {escape_markdown_v2(user.full_name)}\n• @{escape_markdown_v2(user.username or 'Sin usuario')}\n"
            f"• ID: {user_id}\n• Plan: *{escaped_plan_name}*\n• Expira: {exp.strftime('%Y-%m-%d') if exp else 'N/A'}",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Volver", callback_data="planes")]]),
        )

    elif data == "menu_principal":
        await query.message.reply_text("📋 Menú principal:", reply_markup=get_main_menu())

    # NUEVOS CALLBACKS DE MENÚ
    elif data == "peliculas_menu":
        await query.message.reply_text("🎬 Aquí podrás explorar nuestro catálogo de películas. ¡Próximamente más!")
        # Aquí podrías añadir una lista de botones para diferentes géneros o directamente una lista de películas.
    
    elif data == "list_series":
        # Construye un menú con las series existentes
        if not series_data:
            await query.message.reply_text("📺 Actualmente no hay series disponibles. ¡Vuelve pronto!")
            return
        
        botones_series = []
        for serie_id, serie in series_data.items():
            # Limita el título del botón si es muy largo
            button_title = serie["title"]
            if len(button_title) > 30: # Ajusta el límite si es necesario
                button_title = button_title[:27] + "..."
            botones_series.append(
                [InlineKeyboardButton(f"📺 {escape_markdown_v2(button_title)}", callback_data=f"serie_{serie_id}")]
            )
        botones_series.append([InlineKeyboardButton("🔙 Volver al Menú", callback_data="menu_principal")])
        await query.message.reply_text("📺 Explora nuestras series:", reply_markup=InlineKeyboardMarkup(botones_series))


    elif data == "audio_libros":
        await query.message.reply_text("🎧 Aquí estará el contenido de Audio Libros. ¡Pronto más!")
    elif data == "libro_pdf":
        await query.message.reply_text("📚 Aquí estará el contenido de Libro PDF. ¡Pronto más!")
    elif data == "chat_pedido":
        await query.message.reply_text("💬 Aquí puedes hacer tu pedido en el chat. ¡Pronto más!")
    elif data == "cursos":
        await query.message.reply_text("🎓 Aquí estarán los cursos disponibles. ¡Pronto más!")
    elif data == "info_hades": # Nuevo callback para el botón "Info"
        await query.message.reply_text("ℹ️ Este bot fue creado por *Hades*.\n\nContáctalo para soporte o desarrollo de bots personalizados.", parse_mode="Markdown")

    elif data.startswith("show_video_"):
        prefix, pkg_id = data.rsplit('_', 1)
        
        pkg = content_packages.get(pkg_id)
        if not pkg:
            await query.message.reply_text("❌ Video no disponible o eliminado.")
            return

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
                caption=f"🎬 *{escape_markdown_v2(pkg['caption'].splitlines()[0])}*",
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
        
        botones.append([InlineKeyboardButton("🔙 Volver a Series", callback_data="list_series")]) # Volver al listado de series

        await query.message.reply_text(
            f"📺 Temporadas de *{escape_markdown_v2(serie['title'])}*:",
            reply_markup=InlineKeyboardMarkup(botones),
            parse_mode="Markdown"
        )
        try:
            await query.delete_message()
        except Exception as e:
            logger.warning(f"No se pudo eliminar el mensaje anterior en 'list_temporadas_': {e}")


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
            if len(row) == 5:
                botones.append(row)
                row = []
        if row:
            botones.append(row)
        
        # Siempre añadir un botón para volver a las temporadas de esta serie
        botones.append([InlineKeyboardButton("🔙 Volver a Temporadas", callback_data=f"list_temporadas_{serie_id}")])

        await query.message.reply_text(
            f"📺 Capítulos de Temporada {temporada[1:]} de *{escape_markdown_v2(serie['title'])}*:",
            reply_markup=InlineKeyboardMarkup(botones),
            parse_mode="Markdown"
        )
        
        try:
            await query.delete_message()
        except Exception as e:
            logger.warning(f"No se pudo eliminar el mensaje anterior en 'ver_': {e}")


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
            
            # Botón para volver a la lista de capítulos de la temporada
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
    
    expire_at = datetime.now(timezone.utc) + timedelta(days=30) # <-- ¡IMPORTANTE! Se usa timezone.utc
    user_premium[user_id] = {
        "expire_at": expire_at,
        "plan_type": payload
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
        await msg.reply_text("✅ Sinopsis recibida. Ahora envía el video para contenido individual o usa /crear_serie para series.")
    else:
        await msg.reply_text("❌ Envía una imagen con sinopsis.")

async def recibir_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    user_id = msg.from_user.id
    
    if not msg.video:
        # Esto no debería ocurrir si el filtro es correcto, pero es un fallback
        await msg.reply_text("❌ Esto no es un video.")
        return

    # Si hay una serie en progreso para este usuario
    if user_id in current_series:
        serie_data = current_series[user_id]
        if "current_temporada_key" not in serie_data:
            await msg.reply_text("❌ No se ha seleccionado una temporada activa para añadir capítulos. Usa /agregar_temporada [número].")
            return
        
        temporada_key = serie_data["current_temporada_key"]
        
        # Inicializa la lista de capítulos si la temporada es nueva
        if temporada_key not in serie_data["temporadas"]:
            serie_data["temporadas"][temporada_key] = []

        serie_data["temporadas"][temporada_key].append(msg.video.file_id)
        
        await msg.reply_text(
            f"✅ Capítulo {len(serie_data['temporadas'][temporada_key])} agregado a la Temporada {temporada_key[1:]} de la serie '{serie_data['title']}'.\n"
            "Envía más videos o usa /finalizar_serie para guardar."
        )
        return # Salir, ya que el video fue manejado como parte de una serie

    # Si no hay serie en progreso, se asume que es un video individual
    if user_id not in current_photo:
        await msg.reply_text("❌ Primero envía una sinopsis con imagen para crear contenido individual.")
        return

    pkg_id = str(int(datetime.now(timezone.utc).timestamp())) # Usa datetime.now(timezone.utc)
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
                parse_mode="Markdown"
            )
        except Exception as e:
            logger.warning(f"No se pudo enviar a {chat_id}: {e}")

    await msg.reply_text("✅ Contenido individual enviado a los grupos.")

# --- NUEVO: Comandos para series ---

async def crear_serie(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Comando para iniciar creación de serie (sinopsis + foto)."""
    user_id = update.message.from_user.id
    if user_id not in current_photo:
        await update.message.reply_text("❌ Primero envía la sinopsis con imagen.")
        return
    
    serie_id = str(int(datetime.now(timezone.utc).timestamp())) # Usa datetime.now(timezone.utc)
    data = current_photo[user_id]
    
    current_series[user_id] = {
        "serie_id": serie_id,
        "title": data["caption"].split("\n")[0], # Asume que el título es la primera línea del caption
        "photo_id": data["photo_id"],
        "caption": data["caption"],
        "temporadas": {}, # Inicializa el diccionario de temporadas
    }
    del current_photo[user_id] # Limpia la foto actual después de usarla
    
    await update.message.reply_text(
        "✅ Serie creada temporalmente.\n"
        "Ahora usa el comando /agregar_temporada [numero de temporada] para añadir capítulos."
    )

async def agregar_temporada(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Comando para añadir temporada."""
    user_id = update.message.from_user.id
    if user_id not in current_series:
        await update.message.reply_text("❌ No hay serie en creación. Usa /crear_serie primero.")
        return
    
    args = context.args
    if not args or not args[0].isdigit():
        await update.message.reply_text("❌ Usa /agregar_temporada N, donde N es el número de temporada (ej. /agregar_temporada 1).")
        return
    
    temporada_num = args[0]
    temporada_key = f"T{temporada_num}" # Ejemplo: "T1", "T2"

    serie_data_in_progress = current_series[user_id]
    
    if temporada_key in serie_data_in_progress["temporadas"]:
        # Si la temporada ya existe, permite continuar añadiendo capítulos a ella
        current_series[user_id]["current_temporada_key"] = temporada_key
        await update.message.reply_text(f"✅ Reanudando Temporada {temporada_num}. Envía los videos para añadir los capítulos.")
    else:
        # Si la temporada no existe, la crea
        serie_data_in_progress["temporadas"][temporada_key] = []
        current_series[user_id]["current_temporada_key"] = temporada_key # Guarda la clave de la temporada actual
        await update.message.reply_text(f"✅ Temporada {temporada_num} agregada. Ahora envía los videos para añadir los capítulos.")


async def finalizar_serie(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Comando para finalizar la creación de la serie y guardarla."""
    user_id = update.message.from_user.id
    if user_id not in current_series:
        await update.message.reply_text("❌ No hay serie en creación para finalizar.")
        return

    serie_to_save = current_series[user_id]
    
    # Verificar si hay temporadas o capítulos agregados
    if not serie_to_save["temporadas"] or all(not caps for caps in serie_to_save["temporadas"].values()):
        await update.message.reply_text("❌ La serie no tiene ninguna temporada o capítulo agregado. No se guardará. Usa /crear_serie y /agregar_temporada para empezar de nuevo.")
        del current_series[user_id] # Limpiar datos incompletos
        return

    # Guarda la serie en la base de datos de series
    series_data[serie_to_save["serie_id"]] = {
        "title": serie_to_save["title"],
        "photo_id": serie_to_save["photo_id"],
        "caption": serie_to_save["caption"],
        "temporadas": serie_to_save["temporadas"],
    }
    
    del current_series[user_id] # Limpia el estado de creación para el usuario
    save_data() # Guarda los datos actualizados

    # Botón para la serie recién creada
    boton = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "Ver Serie", url=f"https://t.me/{(await context.bot.get_me()).username}?start=serie_{serie_to_save['serie_id']}"
                )
            ]
        ]
    )

    # Envía la notificación de la nueva serie a los chats conocidos
    for chat_id in known_chats:
        try:
            await context.bot.send_photo(
                chat_id=chat_id,
                photo=serie_to_save["photo_id"],
                caption=f"✨ ¡Nueva Serie: *{escape_markdown_v2(serie_to_save['title'])}*!\n\n{escape_markdown_v2(serie_to_save['caption'])}",
                reply_markup=boton,
                protect_content=True, # Protege la sinopsis de la serie
                parse_mode="Markdown"
            )
        except Exception as e:
            logger.warning(f"No se pudo enviar la notificación de la serie a {chat_id}: {e}")

    await update.message.reply_text(
        f"✅ Serie '{serie_to_save['title']}' guardada y publicada en los grupos.",
        reply_markup=boton
    )

# --- Comandos Admin (para añadir chats) ---
async def add_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Añade el chat actual a la lista de chats conocidos (solo para administradores)."""
    user_id = update.effective_user.id
    # Reemplaza con tus IDs de administrador
    ADMIN_IDS = [123456789, 987654321] # <-- ¡IMPORTANTE! Cambia esto por tus propios IDs de Telegram

    if user_id not in ADMIN_IDS:
        await update.message.reply_text("❌ No tienes permisos para usar este comando.")
        return

    chat_id = update.effective_chat.id
    if chat_id < 0: # Es un grupo o canal
        known_chats.add(chat_id)
        save_data()
        await update.message.reply_text(f"✅ Chat {chat_id} añadido a la lista de difusión.")
    else:
        await update.message.reply_text("❌ Este comando solo funciona en grupos o canales.")

async def remove_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Elimina el chat actual de la lista de chats conocidos (solo para administradores)."""
    user_id = update.effective_user.id
    # Reemplaza con tus IDs de administrador
    ADMIN_IDS = [123456789, 987654321] # <-- ¡IMPORTANTE! Cambia esto por tus propios IDs de Telegram

    if user_id not in ADMIN_IDS:
        await update.message.reply_text("❌ No tienes permisos para usar este comando.")
        return

    chat_id = update.effective_chat.id
    if chat_id < 0: # Es un grupo o canal
        if chat_id in known_chats:
            known_chats.remove(chat_id)
            save_data()
            await update.message.reply_text(f"✅ Chat {chat_id} eliminado de la lista de difusión.")
        else:
            await update.message.reply_text("❌ Este chat no estaba en la lista.")
    else:
        await update.message.reply_text("❌ Este comando solo funciona en grupos o canales.")

# --- Funciones de Webhook (para Render.com) ---
async def handle_webhook(request):
    update_data = await request.json()
    update = Update.de_json(update_data, application.bot)
    await application.process_update(update)
    return web.Response(status=200)

async def set_webhook():
    await application.bot.set_webhook(url=APP_URL + "/webhook")
    logger.info(f"✅ Webhook establecido en: {APP_URL}/webhook")

# --- Función Principal ---
def main():
    """Start the bot."""
    # Crea la Application y pasa el token de tu bot.
    application = Application.builder().token(TOKEN).build()

    # Handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CallbackQueryHandler(handle_callback))
    application.add_handler(PreCheckoutQueryHandler(precheckout_handler))
    application.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment))

    # Manejadores para añadir contenido (películas/videos individuales)
    # Revisa estos filtros, son la fuente común del error "TypeError: argument of type 'bool' is not iterable"
    # filters.PHOTO & filters.CAPTION: Asegura que es una foto Y tiene un caption (texto).
    application.add_handler(MessageHandler(filters.PHOTO & filters.CAPTION, recibir_foto))
    # filters.VIDEO: Asegura que es un video. Este handler ahora maneja videos tanto para series como individuales.
    application.add_handler(MessageHandler(filters.VIDEO, recibir_video))

    # Comandos para la administración de series
    application.add_handler(CommandHandler("crear_serie", crear_serie))
    application.add_handler(CommandHandler("agregar_temporada", agregar_temporada))
    application.add_handler(CommandHandler("finalizar_serie", finalizar_serie))


    # Comandos de administración de chats
    application.add_handler(CommandHandler("add_chat", add_chat))
    application.add_handler(CommandHandler("remove_chat", remove_chat))

    # Inicia el servidor web para el webhook
    app = web.Application()
    app.router.add_post("/webhook", handle_webhook)
    
    # Inicia la aplicación de Telegram y configura el webhook
    async def on_startup(app_obj):
        await set_webhook()

    application.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        url_path="/webhook",
        webhook_url=APP_URL + "/webhook",
        on_startup=on_startup # Llama a set_webhook cuando la aplicación web se inicie
    )

if __name__ == "__main__":
    load_data() # Carga los datos al iniciar el bot
    # Crear y ejecutar la aplicación aiohttp manualmente
    # Esto es necesario cuando usas `application.run_webhook` con aiohttp fuera de un script directamente ejecutable
    # y quieres controlar el loop de eventos.
    # El `application.run_webhook` internamente ya lo hace, pero si lo tuvieras separado así:
    # app_web = web.Application()
    # app_web.router.add_post("/webhook", handle_webhook)
    # web.run_app(app_web, host="0.0.0.0", port=PORT)

    # Simplificando la ejecución para Render:
    main()
