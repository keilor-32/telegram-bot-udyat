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
    # ¡IMPORTANTE! Aquí se mantienen tus URLs originales para los botones que lo tenían.
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
                InlineKeyboardButton("🎬 Peliculas", callback_data="peliculas_menu"), # Ahora usa callback_data
                InlineKeyboardButton("📺 Series", callback_data="list_series"),      # Ahora usa callback_data
            ],
            [
                InlineKeyboardButton("💎 Planes", callback_data="planes"),
            ],
            [
                InlineKeyboardButton("🧑 Perfil", callback_data="perfil"),
            ],
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

    # NUEVOS CALLBACKS DE MENÚ (Estos sí deben ser callbacks para que el bot los maneje)
    elif data == "peliculas_menu":
        await query.message.reply_text("🎬 Aquí podrás explorar nuestro catálogo de películas. ¡Próximamente más!",
                                       reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Volver", callback_data="menu_principal")]])
                                      )
        # Aquí podrías añadir una lista de botones para diferentes géneros o directamente una lista de películas.
    
    elif data == "list_series":
        # Construye un menú con las series existentes
        if not series_data:
            await query.message.reply_text("📺 Actualmente no hay series disponibles. ¡Vuelve pronto!",
                                           reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Volver", callback_data="menu_principal")]])
                                          )
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


    # Estos ya eran URLs y se mantienen así por tu petición original
    elif data == "audio_libros": # Este callback ya no será llamado si el botón es URL, pero lo dejo por si cambias de idea.
        await query.message.reply_text("🎧 Aquí estará el contenido de Audio Libros. ¡Pronto más!")
    elif data == "libro_pdf": # Este callback ya no será llamado si el botón es URL.
        await query.message.reply_text("📚 Aquí estará el contenido de Libro PDF. ¡Pronto más!")
    elif data == "chat_pedido": # Este callback ya no será llamado si el botón es URL.
        await query.message.reply_text("💬 Aquí puedes hacer tu pedido en el chat. ¡Pronto más!")
    elif data == "cursos": # Este callback ya no será llamado si el botón es URL.
        await query.message.reply_text("🎓 Aquí estarán los cursos disponibles. ¡Pronto más!")
    
    elif data == "info_hades": # Nuevo callback para el botón "Info"
        await query.message.reply_text("ℹ️ Este bot fue creado por *Hades*.\n\nContáctalo para soporte o desarrollo de bots personalizados.", 
                                       parse_mode="Markdown",
                                       reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Volver", callback_data="menu_principal")]])
                                      )

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
    
    expire_at = datetime.now(timezone.utc) + timedelta(days=30)
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
        await msg.reply_text("❌ Envía una imagen con sinopsis y un caption.")

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
        if "current_temporada_key" not in serie_data or serie_data["current_temporada_key"] not in serie_data["temporadas"]:
            await msg.reply_text("❌ No se ha seleccionado una temporada activa para añadir capítulos. Usa /agregar_temporada [número].")
            return
        
        temporada_key = serie_data["current_temporada_key"]
        
        # Agrega el video al final de la lista de capítulos de la temporada actual
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

    pkg_id = str(int(datetime.now(timezone.utc).timestamp()))
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
    
    serie_id = str(int(datetime.now(timezone.utc).timestamp()))
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
    ADMIN_IDS = [5603774849, 6505701831] # <-- ¡IMPORTANTE! Cambia esto por tus propios IDs de Telegram

    if user_id not in ADMIN_IDS:
        await update.message.reply_text("❌ No tienes permisos para usar este comando.")
        return

    chat_id = update.effective_chat.id
    if chat_id < 0: # Es un grupo o canal
        if chat_id not in known_chats:
            known_chats.add(chat_id)
            save_data()
            await update.message.reply_text(f"✅ Chat {chat_id} añadido a la lista de difusión.")
        else:
            await update.message.reply_text(f"ℹ️ El chat {chat_id} ya estaba en la lista de difusión.")
    else:
        await update.message.reply_text("❌ Este comando solo funciona en grupos o canales.")

async def remove_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Elimina el chat actual de la lista de chats conocidos (solo para administradores)."""
    user_id = update.effective_user.id
    # Reemplaza con tus IDs de administrador
    ADMIN_IDS = [5603774849, 6505701831] # <-- ¡IMPORTANTE! Cambia esto por tus propios IDs de Telegram

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

async def set_webhook_func(app_instance: Application):
    """Esta función ahora recibe la instancia de la aplicación."""
    await app_instance.bot.set_webhook(url=APP_URL + "/webhook")
    logger.info(f"✅ Webhook establecido en: {APP_URL}/webhook")

# --- Función Principal ---
def main():
    """Start the bot."""
    # Crea la Application y pasa el token de tu bot.
    global application # Declara application como global para que handle_webhook pueda acceder a ella
    application = Application.builder().token(TOKEN).build()

    # Handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CallbackQueryHandler(handle_callback))
    application.add_handler(PreCheckoutQueryHandler(precheckout_handler))
    application.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment))

    # Manejadores para añadir contenido (películas/videos individuales)
    # filters.PHOTO & filters.CAPTION: Asegura que es una foto Y tiene un caption (texto).
    application.add_handler(MessageHandler(filters.PHOTO & filters.CAPTION & filters.ChatType.PRIVATE, recibir_foto))
    # filters.VIDEO: Asegura que es un video. Este handler ahora maneja videos tanto para series como individuales.
    # filters.ChatType.PRIVATE: Para que solo responda a videos en el chat privado del bot
    application.add_handler(MessageHandler(filters.VIDEO & filters.ChatType.PRIVATE, recibir_video))


    # Comandos para la administración de series
    application.add_handler(CommandHandler("crear_serie", crear_serie))
    application.add_handler(CommandHandler("agregar_temporada", agregar_temporada))
    application.add_handler(CommandHandler("finalizar_serie", finalizar_serie))


    # Comandos de administración de chats
    application.add_handler(CommandHandler("add_chat", add_chat))
    application.add_handler(CommandHandler("remove_chat", remove_chat))

    # ---- CAMBIO AQUÍ ----
    # 1. Llamar a set_webhook directamente (una sola vez)
    #    Para hacer esto de forma asíncrona en un entorno síncrono (main), necesitamos un loop de asyncio.
    #    Como run_webhook ya maneja su propio loop, la forma más sencilla en este contexto es:
    #    Si estás en PTB 20.x, puedes usar application.initialize() y luego await application.bot.set_webhook().
    #    Si estás en una versión anterior de PTB o para compatibilidad más amplia con el setup de Render,
    #    es común que el `set_webhook` se realice en un proceso separado o en el propio `main`
    #    antes de que el servidor `aiohttp` se inicie si `run_webhook` no tiene `on_startup`.

    # Para asegurar que set_webhook se ejecuta antes de que el servidor empiece a escuchar:
    # Si tu versión de PTB es 20.0+, puedes hacer esto:
    # asyncio.run(application.initialize()) # Necesario para iniciar el bot si no usas run_polling
    # asyncio.run(application.bot.set_webhook(url=APP_URL + "/webhook"))
    # logger.info(f"✅ Webhook establecido en: {APP_URL}/webhook")

    # Sin embargo, dado el error "on_startup", es más probable que debas confiar en el inicio del servidor de aiohttp
    # para ejecutar el set_webhook. Si run_webhook no tiene on_startup, el patrón es:
    # aiohttp.web.run_app() y pasar el startup_hook.

    # REVISIÓN: La manera más sencilla con `run_webhook` sin `on_startup` es:
    # Simplemente ejecutar el set_webhook al inicio del script, una vez.
    # Como `application.run_webhook` es un método síncrono que inicia un loop asíncrono,
    # el `set_webhook` debe ser `await`eado. Esto implica que `main` también debe ser `async`.

    # Vamos a refactorizar `main` a `async def` para permitir el `await set_webhook_func`.

    # Crea la Application y pasa el token de tu bot.
    # `application` ya es global y creada arriba.

    # Inicia la aplicación de Telegram y configura el webhook
    # Aquí el cambio crucial: Llama a set_webhook_func antes de run_webhook
    # como main ahora es async, puedes await directamente.
    # No, run_webhook es síncrono y bloqueante. La forma correcta en un entorno como Render
    # es que la función que inicia el servidor aiohttp tenga un hook de inicio.
    # La versión de python-telegram-bot 20.x maneja esto internamente.
    # Si te da este error, **probablemente estás en PTB 13.x o una versión muy temprana de 20.x**
    # que usa una forma diferente de iniciar el servidor web subyacente (aiohttp).

    # Para Render, el patrón más simple y robusto (que asume que run_webhook no tiene on_startup)
    # es que el webhook se configure una vez, idealmente en un script de deploy o manualmente.
    # Sin embargo, si quieres que el bot lo haga al iniciar, la única forma es:

    # 1. Ejecutar Application.run_webhook() en modo polling (para un solo uso)
    #    para establecer el webhook, y luego relanzar en modo webhook. (Complicado)
    # 2. Asumir que set_webhook puede ser llamado *fuera* del loop del bot, lo cual es cierto.

    # La solución más limpia que se ajusta a lo que estás intentando hacer
    # (ejecutar set_webhook cuando el servidor arranca) es modificar cómo
    # `aiohttp` se integra. `run_webhook` de `python-telegram-bot` es una abstracción.
    # Si esa abstracción no tiene `on_startup`, debemos ir al nivel de `aiohttp`.

    # Option A (Si tu PTB realmente es viejo y no tiene on_startup):
    # Tendrías que iniciar el servidor aiohttp manualmente y añadir el hook ahí.
    # Esto implica no usar `application.run_webhook` directamente, sino:
    # `application.updater.start_webhook(...)` y luego `web.run_app(...)`.

    # Dado que `application.run_webhook` te permite especificar `listen` y `port`,
    # es la forma preferida. El error de `on_startup` es el problema.

    # Solución propuesta: Actualiza `python-telegram-bot`
    # Es la forma más fácil de obtener la funcionalidad `on_startup` si estás en PTB 20+.
    # Si ya estás en PTB 20.0 y el problema persiste, es posible que haya un problema
    # con las dependencias de aiohttp o que la firma del método haya cambiado ligeramente.

    # Si NO PUEDES ACTUALIZAR `python-telegram-bot` a una versión que soporte `on_startup`:
    # La solución es establecer el webhook *una vez* antes de iniciar el bot en Render.
    # Podrías tener un script `set_webhook.py` que solo haga esto:
    #
    # # set_webhook.py
    # import asyncio
    # import os
    # from telegram.ext import Application
    #
    # TOKEN = os.getenv("TOKEN")
    # APP_URL = os.getenv("APP_URL")
    #
    # async def main():
    #     app = Application.builder().token(TOKEN).build()
    #     await app.bot.set_webhook(url=APP_URL + "/webhook")
    #     print(f"Webhook establecido en: {APP_URL}/webhook")
    #
    # if __name__ == "__main__":
    #     asyncio.run(main())
    #
    # Y luego en Render, tu comando de inicio sería:
    # `python set_webhook.py && python bot.py`
    # O ejecutar `python set_webhook.py` como un "Pre-Build Command" en Render.

    # PERO, si la intención es que el bot mismo lo haga al iniciar siempre:
    # Es probable que tu versión de `python-telegram-bot` o `aiohttp` no sean compatibles
    # con la forma en que `on_startup` fue diseñado para ser usado en `run_webhook`.
    #
    # La alternativa es usar el método `start_webhook` del `Updater` y luego iniciar
    # la aplicación web de `aiohttp` manualmente.

    # **Vamos a intentar la solución más limpia que se ajusta a lo que querías,
    # asumiendo que el error es por una versión que no tiene `on_startup` en `run_webhook`.**
    #
    # **La forma más estándar si `on_startup` no funciona es llamar a `set_webhook` ANTES
    # de `run_webhook` si el `Application` ya está inicializado.**

    # Modificación para `main`:
    async def run_bot():
        # Dentro de esta función async, podemos await el set_webhook.
        await set_webhook_func(application) # Llama a la función asíncrona para establecer el webhook
        
        # Ahora, inicia el webhook del bot.
        # run_webhook es un método bloqueante, no necesitas await aquí.
        application.run_webhook(
            listen="0.0.0.0",
            port=PORT,
            url_path="/webhook",
            webhook_url=APP_URL + "/webhook",
            # on_startup=on_startup # <--- ESTO ES LO QUE ESTABA CAUSANDO EL ERROR Y SE ELIMINA
        )

    # Inicia el servidor web para el webhook (esto es la parte de aiohttp)
    # Esto ya no es necesario si usas application.run_webhook directamente,
    # ya que application.run_webhook ya inicia su propio servidor aiohttp.
    # Solo necesitas el `handle_webhook` para que el servidor que inicia PTB lo use.
    # app = web.Application()
    # app.router.add_post("/webhook", handle_webhook) # Esta ruta ya la maneja PTB internamente
    # Esto se vuelve redundante con `application.run_webhook`.

    # El problema es que `application.run_webhook` por sí mismo ya inicia el servidor web.
    # No necesitas `web.Application()` si usas `run_webhook`.
    # Lo que necesitamos es que `set_webhook` se ejecute ANTES de que `run_webhook`
    # inicie su bucle y escuche por peticiones.

    # La función main no puede ser async directamente si se llama con `main()`.
    # Debe ser llamada con `asyncio.run(main())`.
    # Refactoricemos `main` para que sea un wrapper y `start_bot` sea la corrutina.

    # --- Función Principal (REFACTORIZADA) ---
def main():
    """Start the bot."""
    # Crea la Application y pasa el token de tu bot.
    global application # Declara application como global para que handle_webhook pueda acceder a ella
    application = Application.builder().token(TOKEN).build()

    # Handlers (igual que antes)
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CallbackQueryHandler(handle_callback))
    application.add_handler(PreCheckoutQueryHandler(precheckout_handler))
    application.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment))

    application.add_handler(MessageHandler(filters.PHOTO & filters.CAPTION & filters.ChatType.PRIVATE, recibir_foto))
    application.add_handler(MessageHandler(filters.VIDEO & filters.ChatType.PRIVATE, recibir_video))

    application.add_handler(CommandHandler("crear_serie", crear_serie))
    application.add_handler(CommandHandler("agregar_temporada", agregar_temporada))
    application.add_handler(CommandHandler("finalizar_serie", finalizar_serie))

    application.add_handler(CommandHandler("add_chat", add_chat))
    application.add_handler(CommandHandler("remove_chat", remove_chat))

    # --- MODIFICACIÓN CLAVE AQUÍ ---
    # 1. Initialize the Application
    #    (This must be called before interacting with the bot object's network methods)
    asyncio.run(application.initialize())

    # 2. Set the webhook before starting the webhook server
    asyncio.run(set_webhook_func(application))

    # 3. Run the webhook server (this is a blocking call)
    application.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        url_path="/webhook",
        webhook_url=APP_URL + "/webhook",
        # on_startup=on_startup # Eliminar este argumento
    )


if __name__ == "__main__":
    load_data() # Carga los datos al iniciar el bot
    main()
