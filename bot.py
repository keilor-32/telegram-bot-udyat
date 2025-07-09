import logging
import json
import os
import asyncio
from datetime import datetime, timedelta
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup, LabeledPrice
)
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, MessageHandler, ContextTypes,
    filters, PreCheckoutQueryHandler
)
from aiohttp import web

# --- CONFIGURACIÓN --- #
TOKEN = os.getenv("TOKEN")
PROVIDER_TOKEN = os.getenv("PROVIDER_TOKEN", "")
APP_URL = os.getenv("APP_URL")
PORT = int(os.getenv("PORT", "8080"))

if not TOKEN:
    raise ValueError("❌ ERROR: La variable de entorno TOKEN no está configurada.")
if not APP_URL:
    raise ValueError("❌ ERROR: La variable de entorno APP_URL no está configurada.")

# Canales de verificación (inicio)
CHANNELS = {
    'canal1': 'https://t.me/+rzFyi_cr_T1kNTAx',
    'canal2': 'https://t.me/Jhonmaxs'
}

FREE_LIMIT_VIDEOS = 3

PREMIUM_ITEM = {
    "title": "Plan Premium",
    "description": "Acceso y reenvíos ilimitados por 30 días.",
    "payload": "premium_plan",
    "currency": "XTR",
    "prices": [LabeledPrice("Premium por 30 días", 1)]
}

PLAN_PRO_ITEM = {
    "title": "Plan Pro",
    "description": "50 videos diarios, sin reenvíos ni compartir.",
    "payload": "plan_pro",
    "currency": "XTR",
    "prices": [LabeledPrice("Plan Pro por 30 días", 40)]
}

PLAN_ULTRA_ITEM = {
    "title": "Plan Ultra",
    "description": "Videos y reenvíos ilimitados, sin restricciones.",
    "payload": "plan_ultra",
    "currency": "XTR",
    "prices": [LabeledPrice("Plan Ultra por 30 días", 100)]
}

# --- ARCHIVOS --- #
USER_PREMIUM_FILE = "user_premium.json"
USER_VIEWS_FILE = "user_views.json"
CONTENT_PACKAGES_FILE = "content_packages.json"
KNOWN_CHATS_FILE = "known_chats.json"

# --- LOGGING --- #
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- VARIABLES --- #
user_premium = {}
user_daily_views = {}
content_packages = {}
known_chats = set()
current_photo = {}

# --- UTILIDADES --- #
def save_json(filename, data):
    with open(filename, "w", encoding="utf-8") as f:
        json.dump(data, f)

def load_json(filename):
    if not os.path.exists(filename):
        return {}
    with open(filename, "r", encoding="utf-8") as f:
        return json.load(f)

def save_data():
    save_json(USER_PREMIUM_FILE, {str(k): v.isoformat() for k, v in user_premium.items()})
    save_json(USER_VIEWS_FILE, user_daily_views)
    save_json(CONTENT_PACKAGES_FILE, content_packages)
    save_json(KNOWN_CHATS_FILE, list(known_chats))

def load_data():
    global user_premium, user_daily_views, content_packages, known_chats
    up = load_json(USER_PREMIUM_FILE)
    user_premium = {int(k): datetime.fromisoformat(v) for k, v in up.items()}
    user_daily_views = load_json(USER_VIEWS_FILE)
    content_packages = load_json(CONTENT_PACKAGES_FILE)
    known_chats = set(load_json(KNOWN_CHATS_FILE))

def is_premium(user_id):
    return user_id in user_premium and user_premium[user_id] > datetime.utcnow()

def can_view_video(user_id):
    if is_premium(user_id):
        return True
    today = str(datetime.utcnow().date())
    return user_daily_views.get(str(user_id), {}).get(today, 0) < FREE_LIMIT_VIDEOS

def register_view(user_id):
    today = str(datetime.utcnow().date())
    uid = str(user_id)
    if uid not in user_daily_views:
        user_daily_views[uid] = {}
    user_daily_views[uid][today] = user_daily_views[uid].get(today, 0) + 1
    save_data()

# --- MENÚ PRINCIPAL --- #
def get_main_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🎬 Películas", url="https://t.me/+dVTzx8dMGf81NTcx"),
         InlineKeyboardButton("📺 Series", url="https://t.me/+qiFtv2EmV-xmNWFh")],
        [InlineKeyboardButton("🎧 Audiolibros", url="https://t.me/+3lDaURwlx-g4NWJk"),
         InlineKeyboardButton("📚 Libro PDF", url="https://t.me/+iJ5D1VLCAW5hYzhk")],
        [InlineKeyboardButton("💬 Chat Pedido", callback_data="chat_pedido"),
         InlineKeyboardButton("🎓 Cursos", url="https://t.me/clasesdigitales")],
        [InlineKeyboardButton("💎 Planes", callback_data="planes")],
        [InlineKeyboardButton("🧑 Perfil", callback_data="perfil")],
        [InlineKeyboardButton("ℹ️ Info", callback_data="info"),
         InlineKeyboardButton("🆘 Soporte", url="https://t.me/Hsito")]
    ])

# --- HANDLERS --- #
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📋 Menú principal:", reply_markup=get_main_menu())

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
        botones_planes = InlineKeyboardMarkup([
            [InlineKeyboardButton("💸 Comprar Plan Pro (40 ⭐)", callback_data="comprar_pro")],
            [InlineKeyboardButton("💸 Comprar Plan Ultra (100 ⭐)", callback_data="comprar_ultra")],
            [InlineKeyboardButton("🔙 Volver al menú", callback_data="menu_principal")]
        ])
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
            start_parameter="buy-plan-pro"
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
            start_parameter="buy-plan-ultra"
        )

    elif data == "perfil":
        plan = "Premium" if is_premium(user_id) else "Free"
        exp = user_premium.get(user_id)
        await query.message.reply_text(
            f"🧑 Perfil:\n• {user.full_name}\n• @{user.username or 'Sin usuario'}\n"
            f"• ID: {user_id}\n• Plan: {plan}\n• Expira: {exp.strftime('%Y-%m-%d') if exp else 'N/A'}",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Volver al menú", callback_data="menu_principal")]])
        )

    elif data == "chat_pedido":
        await query.message.reply_text(
            "💬 Aquí puedes hacer tu pedido en el chat.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Volver al menú", callback_data="menu_principal")]])
        )

    elif data == "info":
        await query.message.reply_text(
            "ℹ️ Este bot te permite acceder a películas, series, libros y más.\n"
            "Para soporte técnico pulsa el botón correspondiente.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Volver al menú", callback_data="menu_principal")]])
        )

    elif data == "menu_principal":
        await query.message.reply_text("📋 Menú principal:", reply_markup=get_main_menu())

async def precheckout_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.pre_checkout_query.answer(ok=True)

async def successful_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    payload = update.message.successful_payment.invoice_payload
    if payload in [PREMIUM_ITEM["payload"], PLAN_PRO_ITEM["payload"], PLAN_ULTRA_ITEM["payload"]]:
        user_premium[user_id] = datetime.utcnow() + timedelta(days=30)
        save_data()
        await update.message.reply_text("🎉 ¡Gracias por tu compra! Tu plan se activó por 30 días.")

# --- WEBHOOK SETUP --- #
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

# --- APP CONFIG --- #
app_telegram = Application.builder().token(TOKEN).build()
app_telegram.add_handler(CommandHandler("start", start))
app_telegram.add_handler(CallbackQueryHandler(handle_callback))
app_telegram.add_handler(PreCheckoutQueryHandler(precheckout_handler))
app_telegram.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment))

web_app = web.Application()
web_app.router.add_post("/webhook", webhook_handler)
web_app.on_startup.append(on_startup)
web_app.on_shutdown.append(on_shutdown)

async def main():
    load_data()
    logger.info("🤖 Bot iniciado con webhook")
    await app_telegram.initialize()
    await app_telegram.start()

    runner = web.AppRunner(web_app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    logger.info(f"Servidor webhook corriendo en puerto {PORT}")

    try:
        while True:
            await asyncio.sleep(3600)
    except (KeyboardInterrupt, SystemExit):
        logger.info("Deteniendo bot...")
    finally:
        await app_telegram.stop()
        await app_telegram.shutdown()
        await runner.cleanup()

if __name__ == "__main__":
    asyncio.run(main())

