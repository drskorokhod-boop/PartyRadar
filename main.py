# PartyRadar — Render-ready main.py
# Python 3.11 + python-telegram-bot v20+
# Features: CryptoCloud payments, push notifications, banners, JSON storage, lifetimes, Render-safe startup.

import os, json, asyncio, re, time, uuid
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass, asdict
from typing import Optional, List

from dotenv import load_dotenv
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
assert BOT_TOKEN, "BOT_TOKEN отсутствует в .env"

CRYPTOCLOUD_API_KEY = os.getenv("CRYPTOCLOUD_API_KEY", "").strip()
CRYPTOCLOUD_SHOP_ID = os.getenv("CRYPTOCLOUD_SHOP_ID", "").strip()
ADMIN_ID = int(os.getenv("ADMIN_ID", "0") or "0")

# File paths (same folder as main.py)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
EVENTS_FILE = os.path.join(BASE_DIR, "events.json")
USERS_FILE = os.path.join(BASE_DIR, "users.json")
BANNERS_FILE = os.path.join(BASE_DIR, "banners.json")

# Render-safe: create empty files if missing
def _ensure(path: str, default: dict):
    if not os.path.exists(path):
        with open(path, "w", encoding="utf-8") as f:
            json.dump(default, f, ensure_ascii=False, indent=2)

_ensure(EVENTS_FILE, {"events": {}})
_ensure(USERS_FILE, {"users": {}})
_ensure(BANNERS_FILE, {"banners": []})

def _read(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def _write(path: str, data: dict) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)

# Lifetimes (hours) per category
LIFETIMES = {
    "party": 36,        # Вечеринки
    "market": 36,       # Маркет
    "housing": 48,      # Жильё
    "dating": None,     # Знакомства — без срока жизни
    "rideshare": None,  # Попутчики — без срока
}

PUSH_LEAD_HOURS = 2

# CryptoCloud minimal client
import http.client, json as _json

def create_invoice(amount_usd: float, order_id: str, description: str) -> Optional[str]:
    """
    Create a CryptoCloud invoice and return pay_url.
    """
    if not (CRYPTOCLOUD_API_KEY and CRYPTOCLOUD_SHOP_ID):
        return None
    try:
        conn = http.client.HTTPSConnection("api.cryptocloud.plus", timeout=15)
        payload = _json.dumps({
            "shop_id": CRYPTOCLOUD_SHOP_ID,
            "amount": f"{amount_usd:.2f}",
            "currency": "USD",
            "order_id": order_id,
            "description": description[:250]
        })
        headers = {
            "Authorization": f"Token {CRYPTOCLOUD_API_KEY}",
            "Content-Type": "application/json"
        }
        conn.request("POST", "/v2/invoice/create", body=payload, headers=headers)
        resp = conn.getresponse()
        data = resp.read()
        conn.close()
        if resp.status in (200, 201):
            j = _json.loads(data.decode("utf-8"))
            return j.get("result", {}).get("url")
    except Exception:
        return None
    return None

# --- Telegram bot (PTB v20+) ---
from telegram import (
    Update, InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton, InputMediaPhoto
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ConversationHandler, ContextTypes, filters
)

# --- Data models ---
from dataclasses import dataclass

@dataclass
class Event:
    id: str
    user_id: int
    category: str
    description: str
    date: str         # ISO date 'YYYY-MM-DD'
    time: str         # 'HH:MM'
    contact: Optional[str] = ""
    lat: Optional[float] = None
    lon: Optional[float] = None
    address: Optional[str] = ""
    photo_file_id: Optional[str] = None
    created_at: str = ""
    expires_at: Optional[str] = None
    hidden: bool = False
    top_until: Optional[str] = None
    autorenew: bool = False

    def is_expired(self) -> bool:
        if self.expires_at is None:
            return False
        return datetime.now(timezone.utc) > datetime.fromisoformat(self.expires_at)

@dataclass
class User:
    id: int
    first_name: str = ""
    last_name: str = ""
    username: str = ""
    lang: str = "ru"
    lat: Optional[float] = None
    lon: Optional[float] = None

# --- Storage helpers ---
def save_event(ev: Event):
    db = _read(EVENTS_FILE) or {"events": {}}
    db["events"][ev.id] = asdict(ev)
    _write(EVENTS_FILE, db)

def get_event(eid: str) -> Optional[Event]:
    db = _read(EVENTS_FILE) or {}
    info = (db.get("events") or {}).get(eid)
    if not info:
        return None
    return Event(**info)

def list_events(include_hidden=False) -> List[Event]:
    db = _read(EVENTS_FILE) or {}
    res = []
    for v in (db.get("events") or {}).values():
        ev = Event(**v)
        if not include_hidden and ev.hidden:
            continue
        res.append(ev)
    # TOP first
    res.sort(key=lambda e: (0 if (e.top_until and datetime.fromisoformat(e.top_until) > datetime.now(timezone.utc)) else 1,
                            e.created_at), reverse=False)
    return res

def save_user(u: User):
    db = _read(USERS_FILE) or {"users": {}}
    db["users"][str(u.id)] = asdict(u)
    _write(USERS_FILE, db)

def get_user(uid: int) -> User:
    db = _read(USERS_FILE) or {}
    info = (db.get("users") or {}).get(str(uid)) or {}
    if not info:
        return User(id=uid)
    return User(**info)

# --- UI helpers ---
MAIN_KB = ReplyKeyboardMarkup(
    [
        ["➕ Создать событие", "🔎 Поиск рядом"],
        ["🏆 ТОП / Продвижение", "🪧 Баннеры"],
        ["🗂 Мои события", "ℹ️ Помощь"],
    ],
    resize_keyboard=True
)

CATS = {
    "🎉 Вечеринка": "party",
    "🏪 Маркет": "market",
    "🏠 Жильё": "housing",
    "🫂 Знакомства": "dating",
    "🚗 Попутчики": "rideshare",
}

# --- Conversations ---
(ASK_CAT, ASK_DESC, ASK_DATE, ASK_TIME, ASK_CONTACT, ASK_LOCATION, ASK_PHOTO) = range(7)

def _utc_now_iso():
    return datetime.now(timezone.utc).isoformat()

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    user = get_user(u.id)
    user.first_name = u.first_name or user.first_name
    user.last_name = u.last_name or user.last_name
    user.username = u.username or user.username
    save_user(user)

    await update.message.reply_text(
        "Привет! Я PartyRadar — помогу создать и найти события по геолокации.\n"
        "Выбери действие ниже 👇",
        reply_markup=MAIN_KB
    )

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Как пользоваться:\n"
        "• Нажми «➕ Создать событие» и заполни шаги.\n"
        "• Поиск: «🔎 Поиск рядом» — пришли локацию.\n"
        "• Продвижение/ТОП — платные опции через CryptoCloud.\n"
        "• Сроки жизни объявлений:\n"
        "  — Вечеринки/Маркет: 36 ч\n"
        "  — Жильё: 48 ч\n"
        "  — Знакомства и Попутчики — без ограничения"
    )

async def my_events(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    events = [e for e in list_events(True) if e.user_id == uid]
    if not events:
        await update.message.reply_text("У тебя пока нет событий.", reply_markup=MAIN_KB)
        return
    chunks = []
    for e in events:
        line = f"• [{e.id}] {e.category} — {e.date} {e.time} — {'скрыто' if e.hidden else 'активно'}"
        chunks.append(line)
    await update.message.reply_text("\n".join(chunks))

async def create_event_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = [[k] for k in CATS.keys()]
    await update.message.reply_text("Выбери категорию:", reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True, one_time_keyboard=True))
    return ASK_CAT

async def ask_desc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = update.message.text
    cat_code = CATS.get(txt)
    if not cat_code:
        await update.message.reply_text("Пожалуйста, выбери категорию из списка.")
        return ASK_CAT
    context.user_data["cat"] = cat_code
    await update.message.reply_text("Опиши событие (до 500 символов):", reply_markup=ReplyKeyboardMarkup([["⬅️ Отмена"]], resize_keyboard=True))
    return ASK_DESC

async def ask_date(update: Update, context: ContextTypes.DEFAULT_TYPE):
    desc = (update.message.text or "")[:500]
    context.user_data["desc"] = desc
    await update.message.reply_text("Дата события (формат YYYY-MM-DD):")
    return ASK_DATE

async def ask_time(update: Update, context: ContextTypes.DEFAULT_TYPE):
    date = update.message.text.strip()
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", date):
        await update.message.reply_text("Неверный формат даты. Пример: 2025-11-08")
        return ASK_DATE
    context.user_data["date"] = date
    await update.message.reply_text("Время (HH:MM):")
    return ASK_TIME

async def ask_contact(update: Update, context: ContextTypes.DEFAULT_TYPE):
    t = update.message.text.strip()
    if not re.match(r"^\d{2}:\d{2}$", t):
        await update.message.reply_text("Неверный формат времени. Пример: 19:30")
        return ASK_TIME
    context.user_data["time"] = t
    await update.message.reply_text("Контакт (телеграм @username или телефон). Можно пропустить — напиши «-».")
    return ASK_CONTACT

async def ask_location(update: Update, context: ContextTypes.DEFAULT_TYPE):
    contact = update.message.text.strip()
    if contact != "-":
        context.user_data["contact"] = contact
    else:
        context.user_data["contact"] = ""
    await update.message.reply_text(
        "Отправь локацию (📍 геопозицию), или напиши адрес текстом.\n"
        "Можно выбрать на карте в Telegram (скрепка → Location)."
    )
    return ASK_LOCATION

async def ask_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lat = None
    lon = None
    addr = ""
    if update.message.location:
        lat = update.message.location.latitude
        lon = update.message.location.longitude
    else:
        addr = (update.message.text or "").strip()[:200]
    context.user_data["lat"] = lat
    context.user_data["lon"] = lon
    context.user_data["address"] = addr
    await update.message.reply_text("Пришли фото для события (или напиши «-», чтобы пропустить).")
    return ASK_PHOTO

async def finalize_event(update: Update, context: ContextTypes.DEFAULT_TYPE):
    photo_id = None
    if update.message.photo:
        photo_id = update.message.photo[-1].file_id
    elif (update.message.text or "").strip() == "-":
        photo_id = None
    else:
        await update.message.reply_text("Пришли фото или напиши '-' для пропуска.")
        return ASK_PHOTO

    cat = context.user_data["cat"]
    desc = context.user_data["desc"]
    date = context.user_data["date"]
    time_str = context.user_data["time"]
    contact = context.user_data.get("contact", "")
    lat = context.user_data.get("lat")
    lon = context.user_data.get("lon")
    address = context.user_data.get("address", "")

    eid = uuid.uuid4().hex[:8]
    created = datetime.now(timezone.utc).isoformat()

    expires_at = None
    lifetime_hours = LIFETIMES.get(cat)
    if lifetime_hours:
        expires_at = (datetime.now(timezone.utc) + timedelta(hours=lifetime_hours)).isoformat()

    ev = Event(
        id=eid, user_id=update.effective_user.id, category=cat,
        description=desc, date=date, time=time_str, contact=contact,
        lat=lat, lon=lon, address=address, photo_file_id=photo_id,
        created_at=created, expires_at=expires_at, hidden=False
    )
    save_event(ev)

    await update.message.reply_text(f"Событие создано ✅\nID: {eid}\n"
                                    f"{date} {time_str}\n"
                                    f"{'📍Локация добавлена' if (lat and lon) or address else 'Без локации'}",
                                    reply_markup=MAIN_KB)
    return ConversationHandler.END

async def cancel_conv(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Отмена.", reply_markup=MAIN_KB)
    return ConversationHandler.END

# --- Search nearby ---
def _distance_km(lat1, lon1, lat2, lon2):
    from math import radians, sin, cos, sqrt, atan2
    R = 6371.0
    dlat = radians(lat2 - lat1)
    dlon = radians(lon2 - lon1)
    a = sin(dlat/2)**2 + cos(radians(lat1))*cos(radians(lat2))*sin(dlon/2)**2
    c = 2 * atan2(sqrt(1-a), sqrt(a))
    return R * c

async def search_nearby(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Пришли свою локацию (📍) — я покажу события в радиусе 10 км.",
                                    reply_markup=ReplyKeyboardMarkup([[KeyboardButton("📍 Отправить геопозицию", request_location=True)]], resize_keyboard=True))

async def handle_location(update: Update, context: ContextTypes.DEFAULT_TYPE):
    loc = update.message.location
    if not loc:
        await update.message.reply_text("Не получил геопозицию.")
        return
    user = get_user(update.effective_user.id)
    user.lat, user.lon = loc.latitude, loc.longitude
    save_user(user)

    events = [e for e in list_events() if (e.lat and e.lon)]
    nearby = []
    for e in events:
        d = _distance_km(user.lat, user.lon, e.lat, e.lon)
        if d <= 10:
            nearby.append((d, e))
    if not nearby:
        await update.message.reply_text("Рядом событий не найдено.")
        return
    nearby.sort(key=lambda x: x[0])
    lines = []
    for d, e in nearby[:20]:
        lines.append(f"• {e.category} — {e.date} {e.time} ({d:.1f} км)\n{e.description[:120]}")
    await update.message.reply_text("\n\n".join(lines))

# --- Promote / TOP ---
TOP_PRICE_USD = 3.00
AUTO_RENEW_PRICE_USD = 2.00

async def promote(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Продвижение / ТОП:\n"
        f"• Поднять в ТОП на 48 ч — ${TOP_PRICE_USD:.2f}\n"
        f"• Автопродление (ещё +36/48 ч по категории) — ${AUTO_RENEW_PRICE_USD:.2f}\n\n"
        "Пришли ID события для продвижения (или напиши «отмена»)."
    )
    context.user_data["await_promote_id"] = True

async def handle_promote_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get("await_promote_id"):
        return
    msg = update.message.text.strip().lower()
    if msg == "отмена":
        context.user_data.pop("await_promote_id", None)
        await update.message.reply_text("Отменено.", reply_markup=MAIN_KB)
        return
    ev = get_event(msg)
    if not ev or ev.user_id != update.effective_user.id:
        await update.message.reply_text("Событие не найдено или не твоё. Пришли корректный ID или «отмена».")
        return
    order_id = f"top_{ev.id}_{int(time.time())}"
    url = create_invoice(TOP_PRICE_USD, order_id, f"TOP for event {ev.id}")
    if not url:
        await update.message.reply_text("Платёж временно недоступен. Попробуй позже.")
        return
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("Оплатить через CryptoCloud", url=url)]])
    await update.message.reply_text("Ссылка на оплату готова. После оплаты статус обновится автоматически (обычно в течение нескольких минут).", reply_markup=kb)
    context.user_data.pop("await_promote_id", None)

# --- Banners (simple view) ---
async def banners(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db = _read(BANNERS_FILE) or {}
    banners = db.get("banners") or []
    if not banners:
        await update.message.reply_text("Баннеров пока нет.", reply_markup=MAIN_KB)
        return
    media = []
    for b in banners[:10]:
        if b.get("photo"):
            media.append(InputMediaPhoto(b["photo"], caption=b.get("title", "")))
    if media:
        await update.message.reply_media_group(media)
    else:
        await update.message.reply_text("\n".join([b.get("title","") for b in banners]))

# --- Expiry monitor + push ---
async def expiry_worker(app):
    while True:
        try:
            db = _read(EVENTS_FILE) or {"events": {}}
            changed = False
            now = datetime.now(timezone.utc)
            to_notify = []  # (chat_id, text)
            for eid, ed in list((db.get("events") or {}).items()):
                ev = Event(**ed)
                # notify before expiry
                if ev.expires_at and not ev.hidden:
                    exp_dt = datetime.fromisoformat(ev.expires_at)
                    lead = exp_dt - now
                    # send once near lead window
                    if timedelta(hours=PUSH_LEAD_HOURS) >= lead > timedelta(hours=PUSH_LEAD_HOURS-0.05):
                        to_notify.append((ev.user_id, f"⏰ Событие {ev.id} истекает через {PUSH_LEAD_HOURS} ч. "
                                                       f"Продлить? /top"))
                # hide after expiry
                if ev.expires_at and now >= datetime.fromisoformat(ev.expires_at) and not ev.hidden:
                    ev.hidden = True
                    db["events"][eid] = asdict(ev)
                    changed = True
            if changed:
                _write(EVENTS_FILE, db)
            # send notifications
            for chat_id, text in to_notify:
                try:
                    await app.bot.send_message(chat_id=chat_id, text=text)
                except Exception:
                    pass
        except Exception:
            pass
        await asyncio.sleep(60)

# --- Handlers wiring ---
def build_app() -> Application:
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("myevents", my_events))
    app.add_handler(CommandHandler("top", promote))
    app.add_handler(CommandHandler("banners", banners))

    # Main menu text buttons
    app.add_handler(MessageHandler(filters.Regex("^ℹ️ Помощь$"), help_cmd))
    app.add_handler(MessageHandler(filters.Regex("^🗂 Мои события$"), my_events))
    app.add_handler(MessageHandler(filters.Regex("^🪧 Баннеры$"), banners))
    app.add_handler(MessageHandler(filters.Regex("^🏆 ТОП / Продвижение$"), promote))
    app.add_handler(MessageHandler(filters.Regex("^🔎 Поиск рядом$"), search_nearby))

    # Location
    app.add_handler(MessageHandler(filters.LOCATION, handle_location))

    # Promotion flow (enter event id)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_promote_id))

    # Create event conversation
    from telegram.ext import ConversationHandler
    conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^➕ Создать событие$"), create_event_entry)],
        states={
            0: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_desc)],      # ASK_CAT
            1: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_date)],      # ASK_DESC
            2: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_time)],      # ASK_DATE
            3: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_contact)],   # ASK_TIME
            4: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_location)],  # ASK_CONTACT
            5: [
                MessageHandler(filters.LOCATION, ask_photo),
                MessageHandler(filters.TEXT & ~filters.COMMAND, ask_photo),
            ],                                                                   # ASK_LOCATION
            6: [
                MessageHandler(filters.PHOTO, finalize_event),
                MessageHandler(filters.TEXT & ~filters.COMMAND, finalize_event),
            ],                                                                   # ASK_PHOTO
        },
        fallbacks=[MessageHandler(filters.Regex("^⬅️ Отмена$"), cancel_conv)],
        allow_reentry=True,
    )
    app.add_handler(conv)

    return app

async def delayed_run(app: Application):
    # Graceful delay for Render cold start
    await asyncio.sleep(3)
    asyncio.create_task(expiry_worker(app))
    await app.initialize()
    try:
        await app.start()
        await app.updater.start_polling(drop_pending_updates=True)
        await app.updater.idle()
    finally:
        await app.stop()
        await app.shutdown()

def main():
    app = build_app()
    asyncio.run(delayed_run(app))

if __name__ == "__main__":
    main()
