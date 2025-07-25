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
    raise ValueError("âŒ La variable GOOGLE_APPLICATION_CREDENTIALS_JSON no estÃ¡ configurada.")

google_credentials_str = json.loads(google_credentials_raw)
google_credentials_dict = json.loads(google_credentials_str)

with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as temp:
    json.dump(google_credentials_dict, temp)
    temp_path = temp.name

cred = credentials.Certificate(temp_path)
firebase_admin.initialize_app(cred)
db = firestore.client()
print("âœ… Firestore inicializado correctamente.")

# --- ConfiguraciÃ³n ---
TOKEN = os.getenv("TOKEN")
PROVIDER_TOKEN = os.getenv("PROVIDER_TOKEN", "")
APP_URL = os.getenv("APP_URL")
PORT = int(os.getenv("PORT", "8080"))

if not TOKEN:
    raise ValueError("âŒ ERROR: La variable de entorno TOKEN no estÃ¡ configurada.")
if not APP_URL:
    raise ValueError("âŒ ERROR: La variable de entorno APP_URL no estÃ¡ configurada.")

# --- Logging ---
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- Variables en memoria ---
# MODIFICADO: Ahora user_premium guarda un diccionario {expire_at: datetime, plan_type: str}
user_premium = {}          # {user_id: {expire_at: datetime, plan_type: str}}
user_daily_views = {}      # {user_id: {date: count}}
content_packages = {}      # {pkg_id: {photo_id, caption, video_id}}
known_chats = set()
current_photo = {}
series_data = {}           # {serie_id: {"title", "photo_id", "caption", "capitulos": [video_id, ...], ...}}
current_series = {}        # {user_id: {"title", "photo_id", "caption", "serie_id", "capitulos": []}}

# --- Firestore colecciones ---
COLLECTION_USERS = "users_premium"
COLLECTION_VIDEOS = "videos"
COLLECTION_VIEWS = "user_daily_views"
COLLECTION_CHATS = "known_chats"
COLLECTION_SERIES = "series_data"

# --- Funciones Firestore (SÃ­ncronas) ---
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
    "description": "50 videos diarios, sin reenvÃ­os ni compartir.",
    "payload": "plan_pro", # Usado como plan_type
    "currency": "XTR",
    "prices": [LabeledPrice("Plan Pro por 30 dÃ­as", 25)],
}
PLAN_ULTRA_ITEM = {
    "title": "Plan Ultra",
    "description": "Videos y reenvÃ­os ilimitados, sin restricciones.",
    "payload": "plan_ultra", # Usado como plan_type
    "currency": "XTR",
    "prices": [LabeledPrice("Plan Ultra por 30 dÃ­as", 50)],
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
        return "plan_ultra" # Asumir Ultra para planes antiguos sin tipo explÃ­cito
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

# --- Canales para verificaciÃ³n ---
CHANNELS = {
    "canal_1": "@hsitotv",
    "canal_2": "@Jhonmaxs",
}

# --- MenÃº principal ---
def get_main_menu():
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("ðŸŽ§ Audio Libros", url="https://t.me/+3lDaURwlx-g4NWJk"),
                InlineKeyboardButton("ðŸ“š Libro PDF", url="https://t.me/+iJ5D1VLCAW5hYzhk"),
            ],
            [
                InlineKeyboardButton("ðŸ’¬ Chat Pedido", url="https://t.me/+6eA7AdRfgq81NzBh"),
                InlineKeyboardButton("ðŸ“½ï¸ doramas", url="https://t.me/+YIXdwQ9Sa-I3ODYx"),
            ],
            [
                InlineKeyboardButton("ðŸ“½ï¸ peliculas", url="https://t.me/+rvYUEq-c96kzODE0"),
                InlineKeyboardButton("ðŸŽ¬ series", url="https://t.me/+eYI6JZq72o4xNWFh"),
            ],
            [
                InlineKeyboardButton("ðŸ’Ž Planes", callback_data="planes"),
               ],
            [
                InlineKeyboardButton("ðŸ§‘ Perfil", callback_data="perfil"),
            ],
            [
                InlineKeyboardButton("â„¹ï¸ Info", callback_data="info"),
                InlineKeyboardButton("â“ soporte", url="https://t.me/Hsito"),
            ],
        ]
    )

# --- FunciÃ³n auxiliar para generar botones de capÃ­tulos en cuadrÃ­cula ---
def generate_chapter_buttons(serie_id, num_chapters, chapters_per_row=5):
    buttons = []
    row = []
    for i in range(num_chapters):
        row.append(InlineKeyboardButton(str(i + 1), callback_data=f"cap_{serie_id}_{i}"))
        if len(row) == chapters_per_row:
            buttons.append(row)
            row = []
    if row: # AÃ±adir la Ãºltima fila si no estÃ¡ completa
        buttons.append(row)
    
    # AÃ±adir botÃ³n "Volver al menÃº principal" al final
    buttons.append([InlineKeyboardButton("ðŸ”™ Volver al menÃº principal", callback_data="menu_principal")])
    return InlineKeyboardMarkup(buttons)

# --- Handlers ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    user_id = update.effective_user.id
    bot_username = (await context.bot.get_me()).username

    # Manejo del start link para mostrar sinopsis + botÃ³n "Ver Video" (Videos individuales)
    if args and args[0].startswith("video_"):
        pkg_id = args[0].split("_")[1]
        pkg = content_packages.get(pkg_id)
        if not pkg:
            await update.message.reply_text("âŒ Contenido no disponible.")
            return

        # Verifica suscripciÃ³n a canales
        for name, username in CHANNELS.items():
            try:
                member = await context.bot.get_chat_member(chat_id=username, user_id=user_id)
                if member.status not in ["member", "administrator", "creator"]:
                    await update.message.reply_text(
                        "ðŸ”’ Para ver este contenido debes unirte a los canales.",
                        reply_markup=InlineKeyboardMarkup(
                            [
                                [
                                    InlineKeyboardButton(
                                        "ðŸ”— Unirse a canal 1", url=f"https://t.me/{CHANNELS['canal_1'][1:]}"
                                    )
                                ],
                                [
                                    InlineKeyboardButton(
                                        "ðŸ”— Unirse a canal 2", url=f"https://t.me/{CHANNELS['canal_2'][1:]}"
                                    )
                                ],
                                [InlineKeyboardButton("âœ… Verificar suscripciÃ³n", callback_data="verify")],
                            ]
                        ),
                    )
                    return
            except Exception as e:
                logger.warning(f"Error verificando canal: {e}")
                await update.message.reply_text("âŒ Error al verificar canales. Intenta mÃ¡s tarde.")
                return

        # Mostrar sinopsis y botÃ³n "Ver Video"
        ver_video_button = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "â–¶ï¸ Ver Video", callback_data=f"play_video_{pkg_id}" # Callback para cargar el video
                    )
                ]
            ]
        )
        await update.message.reply_text(
            f"ðŸŽ¬ **{pkg.get('caption', 'Contenido:')}**\n\nPresiona 'Ver Video' para iniciar la reproducciÃ³n.",
            reply_markup=ver_video_button,
            parse_mode="Markdown"
        )
        return

    # Manejo del start link para reproducir video (Videos individuales)
    elif args and args[0].startswith("play_video_"):
        pkg_id = args[0].split("_")[2]
        pkg = content_packages.get(pkg_id)
        if not pkg or "video_id" not in pkg:
            await update.message.reply_text("âŒ Video no disponible.")
            return

        # La verificaciÃ³n de canales ya se hizo en el paso 'video_' anterior,
        # pero para mayor seguridad o si el usuario llegÃ³ directamente aquÃ­, se puede repetir.
        for name, username in CHANNELS.items():
            try:
                member = await context.bot.get_chat_member(chat_id=username, user_id=user_id)
                if member.status not in ["member", "administrator", "creator"]:
                    await update.message.reply_text(
                        "ðŸ”’ saludos debes unirte a todos nuestros canales para asi poder usar este bot una ves te hayas unido debes dar click en verificar suscripcion para con tinuar.",
                        reply_markup=InlineKeyboardMarkup(
                            [
                                [
                                    InlineKeyboardButton(
                                        "ðŸ”— Unirse a canal 1", url=f"https://t.me/{CHANNELS['canal_1'][1:]}]"
                                    )
                                ],
                                [
                                    InlineKeyboardButton(
                                        "ðŸ”— Unirse a canal 2", url=f"https://t.me/{CHANNELS['canal_2'][1:]}"
                                    )
                                ],
                                [InlineKeyboardButton("âœ… Verificar suscripciÃ³n", callback_data="verify")],
                            ]
                        ),
                    )
                    return
            except Exception as e:
                logger.warning(f"Error verificando canal: {e}")
                await update.message.reply_text("âŒ Error al verificar canales. Intenta mÃ¡s tarde.")
                return

        if can_view_video(user_id):
            await register_view(user_id)
            title_caption = pkg.get("caption", "ðŸŽ¬ AquÃ­ tienes el video completo.")
            await update.message.reply_video(
                video=pkg["video_id"],
                caption=title_caption,
                protect_content=not can_resend_content(user_id)
            )
        else:
            await update.message.reply_text(
                f"ðŸš« Has alcanzado tu lÃ­mite diario de {FREE_LIMIT_VIDEOS} videos.\n"
                "ðŸ’Ž Por favor, considera comprar un plan para acceso ilimitado.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("ðŸ’Ž Comprar Planes", callback_data="planes")]]),
            )
            return

    # Modificado: Manejo de argumentos para series (directo a capÃ­tulos)
    elif args and args[0].startswith("serie_"):
        serie_id = args[0].split("_", 1)[1]
        serie = series_data.get(serie_id)
        if not serie:
            await update.message.reply_text("âŒ Serie no encontrada.")
            return

        # Verifica suscripciÃ³n a canales (se mantiene para series)
        for name, username in CHANNELS.items():
            try:
                member = await context.bot.get_chat_member(chat_id=username, user_id=user_id)
                if member.status not in ["member", "administrator", "creator"]:
                    await update.message.reply_text(
                        "ðŸ”’ Para ver este contenido debes unirte a los canales.",
                        reply_markup=InlineKeyboardMarkup(
                            [
                                [
                                    InlineKeyboardButton(
                                        "ðŸ”— Unirse a canal 1", url=f"https://t.me/{CHANNELS['canal_1'][1:]}"
                                    )
                                ],
                                [
                                    InlineKeyboardButton(
                                        "ðŸ”— Unirse a canal 2", url=f"https://t.me/{CHANNELS['canal_2'][1:]}"
                                    )
                                ],
                                [InlineKeyboardButton("âœ… Verificar suscripciÃ³n", callback_data="verify")],
                            ]
                        ),
                    )
                    return
            except Exception as e:
                logger.warning(f"Error verificando canal: {e}")
                await update.message.reply_text("âŒ Error al verificar canales. Intenta mÃ¡s tarde.")
                return

        # APLICACIÃ“N DE LA SEGURIDAD PARA SERIES AQUÃ
        if not can_view_video(user_id): # Verifica si tiene vistas disponibles
            await update.message.reply_text(
                f"ðŸš« Has alcanzado tu lÃ­mite diario de {FREE_LIMIT_VIDEOS} vistas para series/videos.\n"
                "ðŸ’Ž Por favor, considera comprar un plan para acceso ilimitado.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("ðŸ’Ž Comprar Planes", callback_data="planes")]]),
            )
            return

        # Si puede ver, mostrar capÃ­tulos
        capitulos = serie.get("capitulos", [])
        if not capitulos:
            await update.message.reply_text("âŒ Esta serie no tiene capÃ­tulos disponibles aÃºn.")
            return
        
        # Usar la nueva funciÃ³n para generar los botones de los capÃ­tulos
        markup = generate_chapter_buttons(serie_id, len(capitulos))

        await update.message.reply_photo(
            photo=serie["photo_id"],
            caption=f"ðŸ“º *{serie['title']}*\n\n{serie['caption']}\n\nSelecciona un capÃ­tulo:",
            reply_markup=markup,
            parse_mode="Markdown"
        )
    else:
        await update.message.reply_text(
            "ðŸ‘‹ Â¡Hola! primero debes unirte a todos nuestros canales para usar este bot una ves te hayas unido haz click en verificar suscripcion para continuar.",
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton("ðŸ”— Unirse a canal 1", url=f"https://t.me/{CHANNELS['canal_1'][1:]}"),
                        InlineKeyboardButton("ðŸ”— Unirse a canal 2", url=f"https://t.me/{CHANNELS['canal_2'][1:]}"),
                    ],
                    [InlineKeyboardButton("âœ… Verificar suscripciÃ³n", callback_data="verify")],
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
        await query.edit_message_text("âœ… VerificaciÃ³n completada. MenÃº disponible:")
        await query.message.reply_text("ðŸ“‹ MenÃº principal:", reply_markup=get_main_menu())
    else:
        await query.edit_message_text("âŒ AÃºn no estÃ¡s suscrito a:\n" + "\n".join(not_joined))

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = query.from_user
    user_id = user.id
    data = query.data

    if data == "planes":
        texto_planes = (
            f"ðŸ’Ž *Planes disponibles:*\n\n"
            f"ðŸ”¹ Free â€“ Hasta {FREE_LIMIT_VIDEOS} videos por dÃ­a.\n\n"
            "ðŸ”¸ *Plan Pro*\n"
            "Precio: 25 estrellas\n"
            "Beneficios: 50 videos diarios, sin reenvÃ­os ni compartir.\n\n"
            "ðŸ”¸ *Plan Ultra*\n"
            "Precio: 50 estrellas\n"
            "Beneficios: Videos y reenvÃ­os ilimitados, sin restricciones.\n"
        )
        botones_planes = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("ðŸ’¸ Comprar Plan Pro (25 â­)", callback_data="comprar_pro")],
                [InlineKeyboardButton("ðŸ’¸ Comprar Plan Ultra (50 â­)", callback_data="comprar_ultra")],
                [InlineKeyboardButton("ðŸ”™ Volver", callback_data="menu_principal")],
            ]
        )
        await query.message.reply_text(texto_planes, parse_mode="Markdown", reply_markup=botones_planes)

    elif data == "comprar_pro":
        if is_premium(user_id):
            exp_date = user_premium[user_id].get("expire_at", datetime.now(timezone.utc)).strftime("%Y-%m-%d") # MODIFICADO
            await query.message.reply_text(f"âœ… Ya tienes un plan activo hasta {exp_date}.")
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
            await query.message.reply_text(f"âœ… Ya tienes un plan activo hasta {exp_date}.")
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
            f"ðŸ§‘ Perfil:\nâ€¢ {user.full_name}\nâ€¢ @{user.username or 'Sin usuario'}\n"
            f"â€¢ ID: {user_id}\nâ€¢ Plan: {plan_type.replace('plan_', '').capitalize()}\nâ€¢ Expira: {exp_date_str}", # MODIFICADO: Mostrar tipo de plan
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("ðŸ”™ Volver", callback_data="planes")]]),
        )

    elif data == "menu_principal":
        await query.message.reply_text("ðŸ“‹ MenÃº principal:", reply_markup=get_main_menu())

    elif data == "audio_libros":
        await query.message.reply_text("ðŸŽ§ AquÃ­ estarÃ¡ el contenido de Audio Libros.")
    elif data == "libro_pdf":
        await query.message.reply_text("ðŸ“š AquÃ­ estarÃ¡ el contenido de Libro PDF.")
    elif data == "chat_pedido":
        await query.message.reply_text("ðŸ’¬ AquÃ­ puedes hacer tu pedido en el chat.")
    elif data == "cursos":
        await query.message.reply_text("ðŸŽ“ AquÃ­ estarÃ¡n los cursos disponibles.")

    # Manejo del callback para reproducir el video individual
    elif data.startswith("play_video_"):
        pkg_id = data.split("_")[2]
        pkg = content_packages.get(pkg_id)
        if not pkg or "video_id" not in pkg:
            await query.message.reply_text("âŒ Video no disponible.")
            return

        # VerificaciÃ³n de seguridad (similar a 'start' handler)
        for name, username in CHANNELS.items():
            try:
                member = await context.bot.get_chat_member(chat_id=username, user_id=user_id)
                if member.status not in ["member", "administrator", "creator"]:
                    await query.message.reply_text(
                        "ðŸ”’ Para ver este contenido debes unirte a los canales.",
                        reply_markup=InlineKeyboardMarkup(
                            [
                                [
                                    InlineKeyboardButton(
                                        "ðŸ”— Unirse a canal 1", url=f"https://t.me/{CHANNELS['canal_1'][1:]}"
                                    )
                                ],
                                [
                                    InlineKeyboardButton(
                                        "ðŸ”— Unirse a canal 2", url=f"https://t.me/{CHANNELS['canal_2'][1:]}"
                                    )
                                ],
                                [InlineKeyboardButton("âœ… Verificar suscripciÃ³n", callback_data="verify")],
                            ]
                        ),
                    )
                    return
            except Exception as e:
                logger.warning(f"Error verificando canal: {e}")
                await query.message.reply_text("âŒ Error al verificar canales. Intenta mÃ¡s tarde.")
                return

        if can_view_video(user_id):
            await register_view(user_id)
            title_caption = pkg.get("caption", "ðŸŽ¬ AquÃ­ tienes el video completo.")
            await query.message.reply_video(
                video=pkg["video_id"],
                caption=title_caption,
                protect_content=not can_resend_content(user_id)
            )
            await query.message.delete() # Eliminar el mensaje anterior
        else:
            await query.answer("ðŸš« Has alcanzado tu lÃ­mite diario de videos. Compra un plan para mÃ¡s acceso.", show_alert=True)
            await query.message.reply_text(
                f"ðŸš« Has alcanzado tu lÃ­mite diario de {FREE_LIMIT_VIDEOS} videos.\n"
                "ðŸ’Ž Por favor, considera comprar un plan para acceso ilimitado.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("ðŸ’Ž Comprar Planes", callback_data="planes")]]),
            )

    # Mostrar video capÃ­tulo con navegaciÃ³n (series)
    elif data.startswith("cap_"):
        _, serie_id, index = data.split("_")
        index = int(index)
        serie = series_data.get(serie_id)
        
        if not serie or "capitulos" not in serie:
            await query.message.reply_text("âŒ Serie o capÃ­tulos no disponibles.")
            return

        capitulos = serie["capitulos"]
        total = len(capitulos)
        if index < 0 or index >= total:
            await query.message.reply_text("âŒ CapÃ­tulo fuera de rango.")
            return

        # APLICACIÃ“N DE LA SEGURIDAD PARA CAPÃTULOS DE SERIES AQUÃ
        if can_view_video(user_id): # Verifica si tiene vistas disponibles
            await register_view(user_id) # Registra la vista
            video_id = capitulos[index]

            botones = []
            if index > 0:
                botones.append(InlineKeyboardButton("â¬…ï¸ Anterior", callback_data=f"cap_{serie_id}_{index - 1}"))
            if index < total - 1:
                botones.append(InlineKeyboardButton("âž¡ï¸ Siguiente", callback_data=f"cap_{serie_id}_{index + 1}"))
            
            # BotÃ³n "Volver a la Serie" que regresarÃ¡ a la lista de capÃ­tulos
            botones.append(InlineKeyboardButton("ðŸ”™ Volver a la Serie", callback_data=f"serie_list_{serie_id}")) # Nuevo callback para listar capÃ­tulos

            markup = InlineKeyboardMarkup([botones])

            await query.edit_message_media(
                media=InputMediaVideo(
                    media=video_id,
                    caption=f"{serie['title']} - CapÃ­tulo {index+1}",
                    parse_mode="Markdown"
                ),
                reply_markup=markup,
            )
        else:
            await query.answer("ðŸš« Has alcanzado tu lÃ­mite diario de videos. Compra un plan para mÃ¡s acceso.", show_alert=True)
            await query.message.reply_text(
                f"ðŸš« Has alcanzado tu lÃ­mite diario de {FREE_LIMIT_VIDEOS} videos.\n"
                "ðŸ’Ž Por favor, considera comprar un plan para acceso ilimitado.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("ðŸ’Ž Comprar Planes", callback_data="planes")]]),
            )
    
    # Nuevo callback para mostrar la lista de capÃ­tulos de una serie
    elif data.startswith("serie_list_"):
        serie_id = data.split("_")[2]
        serie = series_data.get(serie_id)
        if not serie:
            await query.message.reply_text("âŒ Serie no encontrada.")
            return
        
        # APLICACIÃ“N DE LA SEGURIDAD PARA SERIES AQUÃ (al volver a la lista)
        if not can_view_video(user_id): # Verifica si tiene vistas disponibles
            await query.message.reply_text(
                f"ðŸš« Has alcanzado tu lÃ­mite diario de {FREE_LIMIT_VIDEOS} vistas para series/videos.\n"
                "ðŸ’Ž Por favor, considera comprar un plan para acceso ilimitado.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("ðŸ’Ž Comprar Planes", callback_data="planes")]]),
            )
            return

        capitulos = serie.get("capitulos", [])
        if not capitulos:
            await query.message.reply_text("âŒ Esta serie no tiene capÃ­tulos disponibles aÃºn.")
            return
        
        # Reutilizar la funciÃ³n para generar los botones de los capÃ­tulos
        markup = generate_chapter_buttons(serie_id, len(capitulos))

        await query.edit_message_media(
            media=InputMediaPhoto(
                media=serie["photo_id"],
                caption=f"ðŸ“º *{serie['title']}*\n\n{serie['caption']}\n\nSelecciona un capÃ­tulo:",
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
    # MODIFICADO: Guardar el tipo de plan junto con la fecha de expiraciÃ³n
    if payload == PLAN_PRO_ITEM["payload"]:
        expire_at = datetime.now(timezone.utc) + timedelta(days=30)
        user_premium[user_id] = {"expire_at": expire_at, "plan_type": "plan_pro"}
        await update.message.reply_text("ðŸŽ‰ Â¡Gracias por tu compra! Tu *Plan Pro* se activÃ³ por 30 dÃ­as.")
    elif payload == PLAN_ULTRA_ITEM["payload"]:
        expire_at = datetime.now(timezone.utc) + timedelta(days=30)
        user_premium[user_id] = {"expire_at": expire_at, "plan_type": "plan_ultra"}
        await update.message.reply_text("ðŸŽ‰ Â¡Gracias por tu compra! Tu *Plan Ultra* se activÃ³ por 30 dÃ­as.")
    # Si tienes un 'PREMIUM_ITEM' original, asegÃºrate de manejarlo tambiÃ©n.
    # Ejemplo de manejo para el viejo "premium_plan" si aÃºn lo usas:
    # elif payload == PREMIUM_ITEM["payload"]:
    #     expire_at = datetime.now(timezone.utc) + timedelta(days=30)
    #     user_premium[user_id] = {"expire_at": expire_at, "plan_type": "premium_legacy"}
    #     await update.message.reply_text("ðŸŽ‰ Â¡Gracias por tu compra! Tu *Plan Premium* se activÃ³ por 30 dÃ­as.")
    
    save_data()


# --- RecepciÃ³n contenido (sinopsis + video) ---
async def recibir_foto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    user_id = msg.from_user.id
    if msg.photo and msg.caption:
        current_photo[user_id] = {
            "photo_id": msg.photo[-1].file_id,
            "caption": msg.caption,
        }
        await msg.reply_text("âœ… Sinopsis recibida. Ahora envÃ­a el video o usa /crear_serie para series.")
    else:
        await msg.reply_text("âŒ EnvÃ­a una imagen con sinopsis.")

async def recibir_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    user_id = msg.from_user.id
    bot_username = (await context.bot.get_me()).username

    if user_id not in current_photo:
        await msg.reply_text("âŒ Primero envÃ­a una sinopsis con imagen.")
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

    direct_url = f"https://t.me/{bot_username}?start=video_{pkg_id}"
    
    # Formato mejorado para clicable
    full_caption = (
        f"{caption}\n\n"
        f"ðŸŽ¬ *haga click aqui:ðŸ‘‡*\n"
        f"âž¡ï¸ [ver contenido ]({direct_url})\n" # Enlace clicable
    )

    for chat_id in known_chats:
        try:
            await context.bot.send_photo(
                chat_id=chat_id,
                photo=photo_id,
                caption=full_caption,
                parse_mode="Markdown",
                protect_content=False,
            )
        except Exception as e:
            logger.warning(f"No se pudo enviar a {chat_id}: {e}")

    await msg.reply_text("âœ… Contenido enviado a los grupos.")

# --- Comandos para series (simplificado) ---
async def crear_serie(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Comando para iniciar creaciÃ³n de serie (sinopsis + foto)."""
    user_id = update.message.from_user.id
    if user_id not in current_photo:
        await update.message.reply_text("âŒ Primero envÃ­a la sinopsis con imagen.")
        return
    serie_id = str(int(datetime.utcnow().timestamp()))
    data = current_photo[user_id]
    current_series[user_id] = {
        "serie_id": serie_id,
        "title": data["caption"].split("\n")[0],
        "photo_id": data["photo_id"],
        "caption": data["caption"],
        "capitulos": [],
    }
    del current_photo[user_id]
    await update.message.reply_text(
        "âœ… Serie creada temporalmente.\n"
        "Ahora envÃ­a el primer video para el capÃ­tulo 1 usando /agregar_capitulo."
    )

async def agregar_capitulo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Comando para agregar capÃ­tulo a la serie actual."""
    user_id = update.message.from_user.id
    if user_id not in current_series:
        await update.message.reply_text("âŒ No hay serie en creaciÃ³n. Usa /crear_serie primero.")
        return
    
    await update.message.reply_text(
        "ðŸ“½ï¸ Por favor envÃ­a ahora el video para el capÃ­tulo de la serie."
    )

async def recibir_video_serie(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Para recibir video y asignarlo como capÃ­tulo si el usuario estÃ¡ en proceso de agregar capÃ­tulo a serie."""
    msg = update.message
    user_id = msg.from_user.id
    if user_id not in current_series:
        # If not creating a series, treat it as a regular video
        await recibir_video(update, context)
        return

    if not msg.video:
        await msg.reply_text("âŒ EnvÃ­a un video vÃ¡lido para el capÃ­tulo.")
        return

    serie = current_series[user_id]
    video_id = msg.video.file_id
    serie["capitulos"].append(video_id)

    await msg.reply_text(f"âœ… CapÃ­tulo {len(serie['capitulos'])} agregado a la serie. Usa /finalizar_serie para guardar la serie o envÃ­a otro video para aÃ±adir el siguiente capÃ­tulo.")

async def finalizar_serie(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Finaliza y guarda la serie creada en Firestore y memoria."""
    user_id = update.message.from_user.id
    if user_id not in current_series:
        await update.message.reply_text("âŒ No hay serie en creaciÃ³n.")
        return
    serie = current_series[user_id]
    serie_id = serie["serie_id"]
    
    series_data[serie_id] = {
        "title": serie["title"],
        "photo_id": serie["photo_id"],
        "caption": serie["caption"],
        "capitulos": serie["capitulos"],
    }
    save_data()
    del current_series[user_id]

    bot_username = (await context.bot.get_me()).username
    direct_url = f"https://t.me/{bot_username}?start=serie_{serie_id}"
    
    # Formato mejorado para clicable
    full_caption = (
        f"{serie['caption']}\n\n"
        f"ðŸŽ¬ *haga click aqui:ðŸ‘‡*\n"
        f"âž¡ï¸ [ver contenido ]({direct_url})\n" # Enlace clicable
    )

    for chat_id in known_chats:
        try:
            await context.bot.send_photo(
                chat_id=chat_id,
                photo=serie["photo_id"],
                caption=full_caption,
                parse_mode="Markdown",
                protect_content=False,
            )
        except Exception as e:
            logger.warning(f"No se pudo enviar serie a {chat_id}: {e}")

    await update.message.reply_text("âœ… Serie guardada y enviada a los grupos.")

# MODIFICADO: FunciÃ³n para detectar grupos y canales
async def detectar_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    # Check for group/supergroup messages
    if chat.type in ["group", "supergroup"]:
        if chat.id not in known_chats:
            known_chats.add(chat.id)
            save_data()
            logger.info(f"Grupo registrado: {chat.id}")
            await update.message.reply_text(f"âœ… Â¡Este grupo ha sido registrado para envÃ­os! ID: `{chat.id}`", parse_mode="Markdown")
    # Check for forwarded channel posts or bot added to channel
    elif update.channel_post: # This catches messages directly from a channel where the bot is admin
        channel_id = update.channel_post.chat.id
        if channel_id not in known_chats:
            known_chats.add(channel_id)
            save_data()
            logger.info(f"Canal registrado: {channel_id}")
            await context.bot.send_message(
                chat_id=channel_id,
                text=f"âœ… Â¡Este canal ha sido registrado para envÃ­os! ID: `{channel_id}`\n\n"
                     "AsegÃºrate de que el bot tenga permisos de 'Publicar mensajes' y 'Editar mensajes' en este canal.",
                parse_mode="Markdown"
            )
    elif update.message and update.message.forward_from_chat and update.message.forward_from_chat.type == "channel":
        channel_id = update.message.forward_from_chat.id
        if channel_id not in known_chats:
            known_chats.add(channel_id)
            save_data()
            logger.info(f"Canal registrado via forward: {channel_id}")
            await update.message.reply_text(f"âœ… Â¡Canal registrado exitosamente! ID: `{channel_id}`\n\n"
                                             "AsegÃºrate de que el bot sea administrador con permisos de 'Publicar mensajes' y 'Editar mensajes' en tu canal para que pueda enviar contenido automÃ¡ticamente.",
                                             parse_mode="Markdown")
    else:
        # If it's a private chat and not a command, ignore, or provide info
        pass


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
app_telegram.add_handler(CallbackQueryHandler(handle_callback, pattern="^play_video_.*$"))
app_telegram.add_handler(CallbackQueryHandler(handle_callback))
app_telegram.add_handler(PreCheckoutQueryHandler(precheckout_handler))
app_telegram.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment))
app_telegram.add_handler(MessageHandler(filters.PHOTO & filters.ChatType.PRIVATE, recibir_foto))
app_telegram.add_handler(MessageHandler(filters.VIDEO & filters.ChatType.PRIVATE, recibir_video_serie))
# MODIFICADO: Usar la funciÃ³n detectar_chat para todos los tipos de chat
app_telegram.add_handler(MessageHandler(filters.ALL & (filters.ChatType.GROUPS | filters.ChatType.CHANNEL), detectar_chat)) # CORREGIDO: CHANNELS a CHANNEL
app_telegram.add_handler(MessageHandler(filters.FORWARDED & filters.ChatType.PRIVATE, detectar_chat)) # Handle forwarded messages in private chat

# Comandos para series
app_telegram.add_handler(CommandHandler("crear_serie", crear_serie))
app_telegram.add_handler(CommandHandler("agregar_capitulo", agregar_capitulo))
app_telegram.add_handler(CommandHandler("finalizar_serie", finalizar_serie))

# --- Servidor aiohttp ---
web_app = web.Application()
web_app.router.add_post("/webhook", webhook_handler)
web_app.router.add_get("/ping", lambda request: web.Response(text="âœ… Bot activo."))
web_app.on_startup.append(on_startup)
web_app.on_shutdown.append(on_shutdown)

async def main():
    load_data()
    logger.info("ðŸ¤– Bot iniciado con webhook")

    await app_telegram.initialize()
    await app_telegram.start()

    runner = web.AppRunner(web_app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    logger.info(f"ðŸŒ Webhook corriendo en puerto {PORT}")

    try:
        while True:
            await asyncio.sleep(3600)
    except (KeyboardInterrupt, SystemExit):
        logger.info("ðŸ›‘ Deteniendo bot...")
    finally:
        await app_telegram.stop()
        await app_telegram.shutdown()
        await runner.cleanup()

if __name__ == "__main__":
    asyncio.run(main())
