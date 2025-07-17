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

from telegram.ext.filters import BaseFilter

import firebase_admin
from firebase_admin import credentials, firestore

# --- Inicializar Firestore con variable de entorno JSON doblemente serializada ---
google_credentials_raw = os.getenv("GOOGLE_APPLICATION_CREDENTIALS_JSON")
if not google_credentials_raw:
    raise ValueError("❌ La variable GOOGLE_APPLICATION_CREDENTIALS_JSON no está configurada.")

try:
    google_credentials_str = json.loads(google_credentials_raw)
    google_credentials_dict = json.loads(google_credentials_str)
except json.JSONDecodeError:
    google_credentials_dict = json.loads(google_credentials_raw)

with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as temp:
    json.dump(google_credentials_dict, temp)
    temp_path = temp.name

cred = credentials.Certificate(temp_path)
firebase_admin.initialize_app(cred)
db = firestore.client()

print("✅ Firestore inicializado correctamente.")

# --- Configuración del Bot ---
TOKEN = os.getenv("TOKEN")
PROVIDER_TOKEN = os.getenv("PROVIDER_TOKEN", "")
APP_URL = os.getenv("APP_URL")
PORT = int(os.getenv("PORT", "8080"))

if not TOKEN:
    raise ValueError("❌ ERROR: La variable de entorno TOKEN no está configurada.")
if not APP_URL:
    raise ValueError("❌ ERROR: La variable de entorno APP_URL no está configurada.")

# --- Configuración de Logging ---
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- Variables en memoria (se cargarán desde Firestore) ---
user_premium = {}
user_daily_views = {}
content_packages = {}
known_chats = set()
current_photo = {}
user_verified = {}

series_data = {}
current_series = {}

# --- Nombres de Colecciones de Firestore ---
COLLECTION_USERS = "users_premium"
COLLECTION_VIDEOS = "videos"
COLLECTION_VIEWS = "user_daily_views"
COLLECTION_CHATS = "known_chats"
COLLECTION_SERIES = "series_data"
COLLECTION_VERIFIED_USERS = "verified_users"

# --- Funciones de guardado y carga de datos de Firestore ---
def save_user_premium_firestore():
    batch = db.batch()
    for uid, data in user_premium.items():
        doc_ref = db.collection(COLLECTION_USERS).document(str(uid))
        exp = data["expire_at"]
        plan_type = data["plan_type"]
        if exp.tzinfo is None:
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
            plan_type = data.get("plan_type", "premium_plan")
            if expire_at_str:
                expire_at = datetime.fromisoformat(expire_at_str)
                if expire_at.tzinfo is None:
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
FREE_LIMIT_VIDEOS = 3

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
    """Escapa los caracteres especiales de Markdown para Telegram (parse_mode='Markdown')."""
    text = text.replace("_", "\\_")
    text = text.replace("*", "\\*")
    text = text.replace("`", "\\`")
    text = text.replace("[", "\\[")
    return text

# --- Handlers del Bot ---

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
            caption=escape_for_telegram_markdown(pkg["caption"]),
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
            f"📺 *{escape_for_telegram_markdown(serie['title'])}*\n\n{escape_for_telegram_markdown(serie['caption'])}\n\nCapítulos de la Temporada {first_temporada_key[1:]}:",
            reply_markup=InlineKeyboardMarkup(botones),
            parse_mode="Markdown",
            disable_web_page_preview=True,
        )
        return

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
        except Exception as e:
            logger.warning(f"Error verificando canal ({username}): {e}")
            not_joined.append(username)

    if not not_joined:
        user_verified[user_id] = True
        save_data()
        await query.edit_message_text("✅ Verificación completada. Menú disponible:")
        await query.message.reply_text("📋 Menú principal:", reply_markup=get_main_menu())
    else:
        await query.edit_message_text("❌ Aún no estás suscrito a los siguientes canales:\n" + "\n".join(not_joined))


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
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
        
        vistas_hoy = 0
        if not is_premium(user_id):
            today = str(datetime.utcnow().date())
            vistas_hoy = user_daily_views.get(str(user_id), {}).get(today, 0)
        
        vistas_info = f"• Vistas hoy: {vistas_hoy}/{FREE_LIMIT_VIDEOS}" if not is_premium(user_id) else "• Vistas: Ilimitadas"

        escaped_full_name = escape_for_telegram_markdown(user.full_name)
        user_username_display = user.username or 'Sin usuario'
        escaped_username_display = escape_for_telegram_markdown(user_username_display)
        escaped_plan_actual = escape_for_telegram_markdown(plan_actual)

        await query.message.reply_text(
            f"🧑 Perfil:\n"
            f"• Nombre: {escaped_full_name}\n"
            f"• Usuario: @{escaped_username_display}\n"
            f"• ID: `{user_id}`\n"
            f"• Plan: **{escaped_plan_actual}**\n"
            f"• Expira: {expiracion}\n"
            f"{vistas_info}",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Volver", callback_data="menu_principal")]]),
            parse_mode="Markdown"
        )

    elif data == "menu_principal":
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

    elif data.startswith("show_video_"):
        _, pkg_id = data.rsplit('_', 1)
        
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
                caption=f"🎬 *{escape_for_telegram_markdown(pkg['caption'].splitlines()[0])}*",
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
                "💎 Compra un plan para acceso ilimitado y reenvíos.",
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
        
        await query.message.reply_text(
            f"📺 Temporadas de *{escape_for_telegram_markdown(serie['title'])}*:",
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
        
        if len(serie.get("temporadas", {})) > 1:
            botones.append([InlineKeyboardButton("🔙 Volver a Temporadas", callback_data=f"list_temporadas_{serie_id}")])
        else:
            botones.append([InlineKeyboardButton("🔙 Volver a Serie", callback_data=f"serie_{serie_id}")])

        await query.message.reply_text(
            f"📺 Capítulos de Temporada {temporada[1:]}:",
            reply_markup=InlineKeyboardMarkup(botones)
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
                "💎 Compra un plan para acceso ilimitado y reenvíos.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("💎 Comprar Planes", callback_data="planes")]]),
            )


# --- Manejo de Pagos ---
async def precheckout_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.pre_checkout_query
    if query.invoice_payload in [PREMIUM_ITEM["payload"], PLAN_PRO_ITEM["payload"], PLAN_ULTRA_ITEM["payload"]]:
        await query.answer(ok=True)
    else:
        await query.answer(ok=False, error_message="Algo salió mal con tu compra. Por favor, intenta de nuevo más tarde.")

async def successful_payment_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.message.from_user.id
    payload = update.message.successful_payment.invoice_payload
    currency = update.message.successful_payment.currency
    total_amount = update.message.successful_payment.total_amount / 100

    expire_at = datetime.now(timezone.utc) + timedelta(days=30)

    user_premium[user_id] = {"expire_at": expire_at, "plan_type": payload}
    save_data()

    plan_title = PLAN_PAYLOAD_TO_TITLE.get(payload, "Plan Premium")

    await update.message.reply_text(
        f"🎉 ¡Pago de **{escape_for_telegram_markdown(plan_title)}** recibido exitosamente!\n"
        f"Monto: {total_amount} {currency}\n"
        f"Tu suscripción es válida hasta: `{expire_at.strftime('%Y-%m-%d %H:%M UTC')}`\n"
        "¡Gracias por tu compra!",
        parse_mode="Markdown"
    )

# --- Comandos de Administración ---
ADMIN_IDS = [int(admin_id) for admin_id in os.getenv("ADMIN_IDS", "").split(',') if admin_id]

def is_admin(user_id):
    return user_id in ADMIN_IDS

async def admin_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_text("🚫 No tienes permiso para usar este comando.")
        return

    markup = InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Añadir Película", callback_data="admin_add_movie")],
        [InlineKeyboardButton("➕ Añadir Serie", callback_data="admin_add_serie")],
        [InlineKeyboardButton("🗑️ Eliminar Contenido", callback_data="admin_delete_content")],
        [InlineKeyboardButton("✉️ Difundir Mensaje", callback_data="admin_broadcast")],
        [InlineKeyboardButton("📊 Estadísticas", callback_data="admin_stats")],
    ])
    await update.message.reply_text("⚙️ Panel de Administrador:", reply_markup=markup)

# --- Flujo para añadir película ---
async def admin_add_movie_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_admin(user_id): return
    await context.bot.send_message(user_id, "Envía la **foto de portada** para la película. Luego te pediré la sinopsis y el video.", parse_mode="Markdown")
    context.user_data["state"] = "waiting_for_movie_photo"

async def admin_receive_movie_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_admin(user_id) or context.user_data.get("state") != "waiting_for_movie_photo": return

    photo_id = update.message.photo[-1].file_id
    current_photo[user_id] = {"photo_id": photo_id}
    await context.bot.send_message(user_id, "Ahora, envía la **sinopsis** de la película.", parse_mode="Markdown")
    context.user_data["state"] = "waiting_for_movie_caption"

async def admin_receive_movie_caption(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_admin(user_id) or context.user_data.get("state") != "waiting_for_movie_caption": return

    caption = update.message.text
    if user_id not in current_photo:
        await context.bot.send_message(user_id, "❌ Error: Primero envía la foto de portada.")
        context.user_data["state"] = None
        return

    current_photo[user_id]["caption"] = caption
    await context.bot.send_message(user_id, "Finalmente, envía el **video** de la película.", parse_mode="Markdown")
    context.user_data["state"] = "waiting_for_movie_video"

async def admin_receive_movie_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_admin(user_id) or context.user_data.get("state") != "waiting_for_movie_video": return

    if not update.message.video:
        await context.bot.send_message(user_id, "❌ Eso no parece ser un video. Por favor, envía un video válido.")
        return

    video_id = update.message.video.file_id
    pkg_id = f"movie_{datetime.now().strftime('%Y%m%d%H%M%S')}"

    if user_id not in current_photo or "caption" not in current_photo[user_id]:
        await context.bot.send_message(user_id, "❌ Error: La información de la película está incompleta. Por favor, reinicia el proceso.")
        context.user_data["state"] = None
        current_photo.pop(user_id, None)
        return

    content_packages[pkg_id] = {
        "photo_id": current_photo[user_id]["photo_id"],
        "caption": current_photo[user_id]["caption"],
        "video_id": video_id
    }
    save_data()

    share_link = f"https://t.me/{context.bot.username}?start=content_{pkg_id}"
    markup = InlineKeyboardMarkup([[InlineKeyboardButton("🔗 Compartir película", url=share_link)]])

    await context.bot.send_message(
        user_id,
        f"✅ Película '{escape_for_telegram_markdown(current_photo[user_id]['caption'].splitlines()[0])}' añadida y guardada. "
        "Aquí está el enlace para compartir:",
        reply_markup=markup,
        parse_mode="Markdown"
    )
    current_photo.pop(user_id)
    context.user_data["state"] = None

# --- Flujo para añadir serie ---
async def admin_add_serie_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_admin(user_id): return
    await context.bot.send_message(user_id, "Envía la **foto de portada** para la nueva serie.", parse_mode="Markdown")
    context.user_data["state"] = "waiting_for_serie_photo"
    current_series[user_id] = {}

async def admin_receive_serie_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_admin(user_id) or context.user_data.get("state") != "waiting_for_serie_photo": return

    photo_id = update.message.photo[-1].file_id
    current_series[user_id]["photo_id"] = photo_id
    await context.bot.send_message(user_id, "Ahora, envía el **título** de la serie (ej: 'Stranger Things').", parse_mode="Markdown")
    context.user_data["state"] = "waiting_for_serie_title"

async def admin_receive_serie_title(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_admin(user_id) or context.user_data.get("state") != "waiting_for_serie_title": return

    title = update.message.text
    current_series[user_id]["title"] = title
    await context.bot.send_message(user_id, "Ahora, envía la **sinopsis** de la serie.", parse_mode="Markdown")
    context.user_data["state"] = "waiting_for_serie_caption"

async def admin_receive_serie_caption(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_admin(user_id) or context.user_data.get("state") != "waiting_for_serie_caption": return

    caption = update.message.text
    current_series[user_id]["caption"] = caption
    current_series[user_id]["temporadas"] = {}
    await context.bot.send_message(
        user_id,
        "Ahora envía el **número de la primera temporada** que deseas añadir (ej: 'T1', 'T2', etc.). "
        "O puedes enviar `/finalizar_serie` si ya has terminado.",
        parse_mode="Markdown"
    )
    context.user_data["state"] = "waiting_for_temporada_number"

async def admin_receive_temporada_number(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_admin(user_id) or context.user_data.get("state") != "waiting_for_temporada_number": return

    temporada_key = update.message.text.upper()
    if not temporada_key.startswith("T") or not temporada_key[1:].isdigit():
        await context.bot.send_message(user_id, "❌ Formato de temporada inválido. Usa 'T' seguido del número (ej: 'T1').")
        return

    current_series[user_id]["current_temporada"] = temporada_key
    current_series[user_id]["temporadas"][temporada_key] = []
    await context.bot.send_message(
        user_id,
        f"Enviando capítulos para la **Temporada {temporada_key[1:]}**.\n"
        "Envía los videos de los capítulos uno por uno. Cuando termines con esta temporada, "
        "envía `/siguiente_temporada` para añadir otra, o `/finalizar_serie` para guardar la serie completa.",
        parse_mode="Markdown"
    )
    context.user_data["state"] = "waiting_for_capitulo_video"

async def admin_receive_capitulo_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_admin(user_id) or context.user_data.get("state") != "waiting_for_capitulo_video": return

    if not update.message.video:
        await context.bot.send_message(user_id, "❌ Eso no parece ser un video. Por favor, envía un video válido para el capítulo.")
        return

    video_id = update.message.video.file_id
    current_temporada = current_series[user_id].get("current_temporada")

    if not current_temporada or current_temporada not in current_series[user_id]["temporadas"]:
        await context.bot.send_message(user_id, "❌ Error: No se ha seleccionado una temporada actual. Por favor, reinicia el proceso de añadir serie.")
        context.user_data["state"] = None
        current_series.pop(user_id, None)
        return

    current_series[user_id]["temporadas"][current_temporada].append(video_id)
    num_capitulos = len(current_series[user_id]["temporadas"][current_temporada])
    await context.bot.send_message(
        user_id,
        f"✅ Capítulo {num_capitulos} añadido a la Temporada {current_temporada[1:]}. "
        "Envía el siguiente capítulo, o `/siguiente_temporada` para la siguiente temporada, "
        "o `/finalizar_serie` para guardar la serie.",
        parse_mode="Markdown"
    )

async def admin_next_temporada(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_admin(user_id): return

    if context.user_data.get("state") not in ["waiting_for_capitulo_video", "waiting_for_temporada_number"]:
        await update.message.reply_text("❌ Comando inválido en este momento.")
        return
    
    if user_id not in current_series or not current_series[user_id].get("photo_id"):
        await update.message.reply_text("❌ No hay una serie en proceso de creación. Por favor, inicia con /admin_add_serie.")
        context.user_data["state"] = None
        current_series.pop(user_id, None)
        return

    await context.bot.send_message(user_id, "Envía el **número de la siguiente temporada** a añadir (ej: 'T2').", parse_mode="Markdown")
    context.user_data["state"] = "waiting_for_temporada_number"

async def admin_finalizar_serie(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_admin(user_id): return

    if user_id not in current_series or not current_series[user_id].get("photo_id"):
        await update.message.reply_text("❌ No hay una serie en proceso de creación para finalizar.")
        context.user_data["state"] = None
        current_series.pop(user_id, None)
        return

    serie_data_to_save = current_series[user_id].copy()
    serie_id = f"serie_{datetime.now().strftime('%Y%m%d%H%M%S')}"

    series_data[serie_id] = serie_data_to_save
    save_data()

    share_link = f"https://t.me/{context.bot.username}?start=serie_{serie_id}"
    markup = InlineKeyboardMarkup([[InlineKeyboardButton("🔗 Compartir Serie", url=share_link)]])

    await context.bot.send_message(
        user_id,
        f"✅ Serie '*{escape_for_telegram_markdown(serie_data_to_save['title'])}*' añadida y guardada. "
        "Aquí está el enlace para compartir:",
        reply_markup=markup,
        parse_mode="Markdown"
    )
    current_series.pop(user_id)
    context.user_data["state"] = None

# --- Flujo para eliminar contenido ---
async def admin_delete_content_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_admin(user_id): return

    markup_buttons = []
    for pkg_id, pkg_data in content_packages.items():
        title = pkg_data.get("caption", "Sin título").splitlines()[0]
        markup_buttons.append([InlineKeyboardButton(f"🎬 Película: {title}", callback_data=f"delete_pkg_{pkg_id}")])
    
    for serie_id, serie_data in series_data.items():
        title = serie_data.get("title", "Sin título")
        markup_buttons.append([InlineKeyboardButton(f"📺 Serie: {title}", callback_data=f"delete_serie_{serie_id}")])

    if not markup_buttons:
        await update.message.reply_text("No hay contenido para eliminar.")
        return

    markup = InlineKeyboardMarkup(markup_buttons)
    await update.message.reply_text("Selecciona el contenido a eliminar:", reply_markup=markup)

async def admin_delete_content_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_admin(user_id): return

    query = update.callback_query
    await query.answer()

    data = query.data
    if data.startswith("delete_pkg_"):
        pkg_id_to_delete = data.split("delete_pkg_")[1]
        if pkg_id_to_delete in content_packages:
            del content_packages[pkg_id_to_delete]
            save_data()
            await query.edit_message_text(f"✅ Película '{pkg_id_to_delete}' eliminada.")
        else:
            await query.edit_message_text("❌ Película no encontrada.")
    elif data.startswith("delete_serie_"):
        serie_id_to_delete = data.split("delete_serie_")[1]
        if serie_id_to_delete in series_data:
            del series_data[serie_id_to_delete]
            save_data()
            await query.edit_message_text(f"✅ Serie '{serie_id_to_delete}' eliminada.")
        else:
            await query.edit_message_text("❌ Serie no encontrada.")

# --- Flujo de Difusión de Mensajes ---
async def admin_broadcast_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_admin(user_id): return
    await update.message.reply_text("Envía el mensaje que deseas difundir a todos los usuarios del bot. "
                                    "Puedes usar Markdown. Envía /cancelar_difusion para cancelar.", parse_mode="Markdown")
    context.user_data["state"] = "waiting_for_broadcast_message"

async def admin_receive_broadcast_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_admin(user_id) or context.user_data.get("state") != "waiting_for_broadcast_message": return

    if update.message.text == "/cancelar_difusion":
        await update.message.reply_text("❌ Difusión cancelada.")
        context.user_data["state"] = None
        return

    message_text = update.message.text
    
    all_user_ids = set(user_premium.keys()).union(set(int(uid) for uid in user_daily_views.keys()))
    all_user_ids.update(known_chats)

    success_count = 0
    fail_count = 0
    for target_user_id in all_user_ids:
        try:
            await context.bot.send_message(
                chat_id=target_user_id,
                text=message_text,
                parse_mode="Markdown"
            )
            success_count += 1
            if target_user_id not in known_chats:
                known_chats.add(target_user_id)
        except Exception as e:
            logger.warning(f"No se pudo enviar mensaje a {target_user_id}: {e}")
            fail_count += 1
    
    save_data()

    await update.message.reply_text(f"✅ Difusión completada. Mensajes enviados a {success_count} usuarios. Fallaron: {fail_count}.")
    context.user_data["state"] = None

async def admin_cancel_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_admin(user_id): return
    if context.user_data.get("state") == "waiting_for_broadcast_message":
        await update.message.reply_text("❌ Difusión cancelada.")
        context.user_data["state"] = None
    else:
        await update.message.reply_text("No hay ninguna difusión activa para cancelar.")

# --- Estadísticas de Administración ---
async def admin_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_admin(user_id): return

    total_users = len(set(user_premium.keys()).union(set(int(uid) for uid in user_daily_views.keys())).union(known_chats))
    premium_users = sum(1 for uid in user_premium if user_premium[uid]["expire_at"] > datetime.now(timezone.utc))
    free_users = total_users - premium_users
    total_movies = len(content_packages)
    total_series = len(series_data)
    
    total_chapters = 0
    for serie in series_data.values():
        for temporada in serie.get("temporadas", {}).values():
            total_chapters += len(temporada)

    stats_text = (
        f"📊 *Estadísticas del Bot:*\n"
        f"• Usuarios Totales: {total_users}\n"
        f"• Usuarios Premium: {premium_users}\n"
        f"• Usuarios Gratuitos: {free_users}\n"
        f"• Películas Publicadas: {total_movies}\n"
        f"• Series Publicadas: {total_series}\n"
        f"• Capítulos de Series: {total_chapters}\n"
    )
    await update.message.reply_text(stats_text, parse_mode="Markdown")


# --- Manejo de mensajes de texto genéricos (para agregar el chat a known_chats) ---
async def handle_text_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id not in known_chats:
        known_chats.add(chat_id)
        save_data()

    user_id = update.effective_user.id
    if not is_admin(user_id) and not user_verified.get(user_id):
        pass


# --- Manejo de fotos genéricas (para añadir contenido) ---
async def handle_photo_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_admin(user_id): return

    if context.user_data.get("state") == "waiting_for_movie_photo":
        await admin_receive_movie_photo(update, context)
    elif context.user_data.get("state") == "waiting_for_serie_photo":
        await admin_receive_serie_photo(update, context)
    else:
        await update.message.reply_text("📸 Recibí tu foto, pero no estoy esperando una foto en este momento.")


# --- Manejo de videos genéricos (para añadir contenido) ---
async def handle_video_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_admin(user_id): return

    if context.user_data.get("state") == "waiting_for_movie_video":
        await admin_receive_movie_video(update, context)
    elif context.user_data.get("state") == "waiting_for_capitulo_video":
        await admin_receive_capitulo_video(update, context)
    else:
        await update.message.reply_text("🎥 Recibí tu video, pero no estoy esperando un video en este momento.")


# --- Función para iniciar el servidor web para Render ---
async def health_check(request):
    return web.Response(text="Bot is running")

async def webhook_handler(request):
    # Asegúrate de que 'application' se ha inicializado y está en estado RUNNING
    # El método _check_initialized() verifica que application._running_future esté establecido
    # y que _is_started sea True.
    if application is None or not application.updater.is_running: # Utiliza application.updater.is_running
        # Si el bot no está completamente iniciado, devuelve un error o espera
        # Para evitar el RuntimeError, puedes devolver una respuesta de error 503 o 400
        # hasta que el bot esté listo.
        logger.warning("Webhook received before Application is fully initialized/started.")
        return web.Response(status=503, text="Bot not ready yet.")

    update = Update.de_json(await request.json(), application.bot)
    await application.process_update(update)
    return web.Response(text="ok")

# Declarar 'application' globalmente para que 'create_state_filter' pueda acceder a ella
application = None

# --- Clase de Filtro Personalizado para Estados de Usuario ---
class StateFilter(BaseFilter):
    def __init__(self, state_name):
        super().__init__()
        self.state_name = state_name

    def filter(self, message):
        global application
        if application is None or not application.updater.is_running:
            return False

        user_id = message.effective_user.id
        return (
            user_id in application.user_data
            and application.user_data[user_id].get("state") == self.state_name
        )

def create_state_filter_instance(state_name):
    return StateFilter(state_name)

# --- Función Principal (main) ---
async def main(): # Make main an async function
    global application
    load_data()

    application = Application.builder().token(TOKEN).build()

    # --- Handlers de Comandos ---
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("admin", admin_menu, filters=filters.User(ADMIN_IDS)))
    application.add_handler(CommandHandler("finalizar_serie", admin_finalizar_serie, filters=filters.User(ADMIN_IDS)))
    application.add_handler(CommandHandler("siguiente_temporada", admin_next_temporada, filters=filters.User(ADMIN_IDS)))
    application.add_handler(CommandHandler("cancelar_difusion", admin_cancel_broadcast, filters=filters.User(ADMIN_IDS)))

    # --- Handlers de Callbacks ---
    application.add_handler(CallbackQueryHandler(verify, pattern="^verify$"))
    application.add_handler(CallbackQueryHandler(handle_callback))

    # --- Handlers de Mensajes de Administrador (Refactorizados) ---
    application.add_handler(MessageHandler(filters.PHOTO & filters.User(ADMIN_IDS), handle_photo_message))
    application.add_handler(MessageHandler(filters.VIDEO & filters.User(ADMIN_IDS), handle_video_message))
    
    # Manejadores de texto para ADMINS, filtrando por estado de user_data
    application.add_handler(MessageHandler(
        filters.TEXT & filters.User(ADMIN_IDS) & create_state_filter_instance("waiting_for_movie_caption"),
        admin_receive_movie_caption
    ))
    application.add_handler(MessageHandler(
        filters.TEXT & filters.User(ADMIN_IDS) & create_state_filter_instance("waiting_for_serie_title"),
        admin_receive_serie_title
    ))
    application.add_handler(MessageHandler(
        filters.TEXT & filters.User(ADMIN_IDS) & create_state_filter_instance("waiting_for_serie_caption"),
        admin_receive_serie_caption
    ))
    application.add_handler(MessageHandler(
        filters.TEXT & filters.User(ADMIN_IDS) & create_state_filter_instance("waiting_for_temporada_number"),
        admin_receive_temporada_number
    ))
    application.add_handler(MessageHandler(
        filters.TEXT & filters.User(ADMIN_IDS) & create_state_filter_instance("waiting_for_broadcast_message"),
        admin_receive_broadcast_message
    ))

    # --- Handlers de Pagos ---
    application.add_handler(PreCheckoutQueryHandler(precheckout_handler))
    application.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment_callback))

    # --- Handlers de Callbacks de Admin ---
    application.add_handler(CallbackQueryHandler(admin_add_movie_start, pattern="^admin_add_movie$"))
    application.add_handler(CallbackQueryHandler(admin_add_serie_start, pattern="^admin_add_serie$"))
    application.add_handler(CallbackQueryHandler(admin_delete_content_start, pattern="^admin_delete_content$"))
    application.add_handler(CallbackQueryHandler(admin_delete_content_confirm, pattern="^delete_pkg_|^delete_serie_"))
    application.add_handler(CallbackQueryHandler(admin_broadcast_start, pattern="^admin_broadcast$"))
    application.add_handler(CallbackQueryHandler(admin_stats, pattern="^admin_stats$"))

    # --- Handler para mensajes de texto genéricos ---
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_message))

    # --- Explicitly initialize the Application ---
    # This prepares the internal structures for running
    await application.initialize()

    # --- Set up the webhook ---
    webhook_url = f"{APP_URL}/{TOKEN}"
    print(f"🌐 Configurando webhook en: {webhook_url}")
    await application.bot.set_webhook(url=webhook_url)

    # --- Start the Application (but not the polling updater) ---
    # This sets _running_future and _is_started
    await application.start()

    # --- Create and start the aiohttp web server ---
    app = web.Application()
    app.router.add_post(f'/{TOKEN}', webhook_handler)
    app.router.add_get('/health', health_check)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', PORT)
    print(f"🚀 Servidor web escuchando en el puerto {PORT}")
    await site.start()

    # Keep the aiohttp server running
    try:
        # Instead of loop.run_forever(), we need to keep the aiohttp server alive
        # A simple way for webhooks is to run a Future that never completes
        # Or, ideally, use aiohttp's own run_app for simplicity if it fits.
        # For a manually controlled loop like this, we'll just wait for the runner.
        await asyncio.Event().wait() # This will block forever
    except asyncio.CancelledError:
        pass # Expected on shutdown
    finally:
        # Stop the PTB application gracefully
        await application.stop()
        await application.shutdown() # Clean up application resources
        await runner.cleanup()

if __name__ == "__main__":
    asyncio.run(main()) # Run the async main function
