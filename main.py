# main.py
# PartyRadar — оптимизированная версия под aiogram 3.x

import asyncio
import json
import logging
import os
import re
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, List, Tuple

import aiohttp
from aiohttp import web
from geopy.distance import geodesic

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode, ContentType
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    Message, CallbackQuery,
    ReplyKeyboardMarkup, KeyboardButton,
    InlineKeyboardMarkup, InlineKeyboardButton,
    FSInputFile,
    InputMediaPhoto, InputMediaVideo,
)
from aiogram.webhook.aiohttp_server import SimpleRequestHandler
from dotenv import load_dotenv
from aiohttp import web

from contextlib import contextmanager

from sqlalchemy import create_engine, Column, Integer, String, Text
from sqlalchemy.dialects.sqlite import JSON as SA_JSON
from sqlalchemy.orm import sessionmaker, declarative_base, Session

# ===================== DATABASE (SQL) =====================

DB_URL = os.getenv("DATABASE_URL", "sqlite:///./partyradar.db")

engine = create_engine(DB_URL, echo=False, future=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)
Base = declarative_base()


class EventRow(Base):
    __tablename__ = "events_store"
    id = Column(Integer, primary_key=True, autoincrement=False)
    payload = Column(SA_JSON)


class BannerRow(Base):
    __tablename__ = "banners_store"
    id = Column(Integer, primary_key=True, autoincrement=False)
    payload = Column(SA_JSON)


class UserRow(Base):
    __tablename__ = "users_store"
    key = Column(String, primary_key=True)
    payload = Column(SA_JSON)


class PaymentRow(Base):
    __tablename__ = "payments_store"
    key = Column(String, primary_key=True)
    payload = Column(SA_JSON)


def init_db():
    Base.metadata.create_all(bind=engine)


@contextmanager
def get_session() -> Session:
    session: Session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()

# Инициализируем базу при старте модуля
init_db()

# ===================== CONFIG =====================

load_dotenv()
TOKEN = os.getenv("BOT_TOKEN")
assert TOKEN, "❌ BOT_TOKEN отсутствует в .env"

CRYPTOCLOUD_API_KEY = os.getenv("CRYPTOCLOUD_API_KEY", "").strip()
CRYPTOCLOUD_SHOP_ID = os.getenv("CRYPTOCLOUD_SHOP_ID", "").strip()
ADMIN_ID = int(os.getenv("ADMIN_ID", "0") or 0)
PUBLIC_URL = os.getenv("PUBLIC_URL", "").strip()

LOGO_URL = ""  # можно указать URL логотипа (если локального файла нет)

logging.basicConfig(level=logging.INFO)

bot = Bot(TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher(storage=MemoryStorage())

EVENTS_FILE = "events.json"
BANNERS_FILE = "banners.json"
USERS_FILE = "users.json"
PAYMENTS_FILE = "payments.json"

DEFAULT_RADIUS_KM = 30
PUSH_LEAD_HOURS = 2
MAX_ACTIVE_BANNERS = 3
ANYPAY_VERIFICATION_TEXT = "0298a93952ce16ab5114a95d874d"
BITPAPA_REF_LINK = "https://bitpapa.com/?ref=Y2RhNjc3MT"
# Тарифы (USD)
TARIFFS_USD = {
    24: 0.5,    # используется для платного 1 дня, когда бесплатный лимит исчерпан
    72: 0.8,    # 3 дня
    168: 1.5,   # 7 дней
    720: 3.0,   # 30 дней
}

TOP_PRICES = {
    1: 1.0,
    3: 2.0,
    7: 3.0,
    15: 5.0,
    30: 8.0,
}

PUSH_PRICE_USD = 1.0

BANNER_DURATIONS = {
    "📅 1 день — $3": (1, 3.0),
    "📅 3 дня — $7": (3, 7.0),
    "📅 7 дней — $12": (7, 12.0),
    "📅 15 дней — $18": (15, 18.0),
    "📅 30 дней — $30": (30, 30.0),
}

# Сроки жизни событий (варианты на клавиатуре)
LIFETIME_OPTIONS = {
    "🕐 1 день (бесплатно)": 24,
    "⏱ 3 дня — $0.8": 72,
    "⏱ 7 дней — $1.5": 168,
    "⏱ 30 дней — $3.0": 720,
}


# ===================== JSON HELPERS =====================

def _ensure_dir(path: str):
    d = os.path.dirname(path)
    if d and not os.path.exists(d):
        os.makedirs(d, exist_ok=True)


def _load_json(path: str, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _save_json(path: str, data):
    """
    Безопасная запись: временный файл + fsync + os.replace,
    чтобы Render не успел «убить» процесс посреди записи.
    """
    _ensure_dir(path)
    tmp_path = path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, path)



def _load_events() -> List[dict]:
    """
    Загрузка событий из SQL-базы.
    Возвращает список dict, совместимый с прежней структурой JSON.
    """
    with get_session() as session:
        rows = session.query(EventRow).order_by(EventRow.id).all()
        return [row.payload for row in rows]


def _save_events(data: List[dict]):
    """
    Полная синхронизация списка событий в SQL.
    Таблица events_store будет содержать ровно те события, что в data.
    """
    with get_session() as session:
        session.query(EventRow).delete()
        for ev in data:
            ev_id = ev.get("id")
            if ev_id is None:
                continue
            try:
                ev_id_int = int(ev_id)
            except Exception:
                continue
            session.merge(EventRow(id=ev_id_int, payload=ev))


def _load_banners() -> List[dict]:
    """
    Загрузка баннеров из SQL.
    """
    with get_session() as session:
        rows = session.query(BannerRow).order_by(BannerRow.id).all()
        return [row.payload for row in rows]


def _save_banners(data: List[dict]):
    """
    Полная синхронизация баннеров в SQL.
    """
    with get_session() as session:
        session.query(BannerRow).delete()
        for b in data:
            b_id = b.get("id")
            if b_id is None:
                continue
            try:
                b_id_int = int(b_id)
            except Exception:
                continue
            session.merge(BannerRow(id=b_id_int, payload=b))




def _load_users() -> Dict[str, dict]:
    """
    Загрузка пользователей из SQL.
    Возвращает dict[str, dict] как и раньше.
    """
    with get_session() as session:
        rows = session.query(UserRow).all()
        return {row.key: row.payload for row in rows}


def _save_users(data: Dict[str, dict]):
    """
    Полная синхронизация пользователей в SQL.
    """
    with get_session() as session:
        session.query(UserRow).delete()
        for key, payload in data.items():
            session.merge(UserRow(key=str(key), payload=payload))


def _load_payments() -> Dict[str, dict]:
    """
    Загрузка платежей из SQL.
    """
    with get_session() as session:
        rows = session.query(PaymentRow).all()
        return {row.key: row.payload for row in rows}


def _save_payments(data: Dict[str, dict]):
    """
    Полная синхронизация платежей в SQL.
    """
    with get_session() as session:
        session.query(PaymentRow).delete()
        for key, payload in data.items():
            session.merge(PaymentRow(key=str(key), payload=payload))


def _safe_dt(s: Optional[str]) -> Optional[datetime]:

    try:
        return datetime.fromisoformat(s) if s else None
    except Exception:
        return None


# ===================== CRYPTOCLOUD =====================

async def cc_create_invoice(amount_usd: float, order_id: str, description: str) -> Tuple[Optional[str], Optional[str]]:
    if not CRYPTOCLOUD_API_KEY or not CRYPTOCLOUD_SHOP_ID:
        logging.warning("⚠️ CryptoCloud ключи не заданы")
        return None, None

    url = "https://api.cryptocloud.plus/v2/invoice/create"
    headers = {
        "Authorization": f"Token {CRYPTOCLOUD_API_KEY}",
        "Content-Type": "application/json"
    }
    payload = {
        "shop_id": CRYPTOCLOUD_SHOP_ID,
        "amount": float(amount_usd),
        "currency": "USD",
        "order_id": order_id,
        "description": description,
        "locale": "ru"
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, headers=headers, json=payload, timeout=30) as resp:
                data = await resp.json()
                link = data.get("result", {}).get("link")
                uuid = data.get("result", {}).get("uuid")

        payments = _load_payments()
        payments[str(order_id)] = {
            "invoice_uuid": uuid,
            "user_id": order_id,
            "amount": amount_usd,
            "description": description,
            "timestamp": datetime.now().isoformat()
        }
        _save_payments(payments)
        logging.info(f"✅ Платёж сохранён: {order_id} → {uuid}")

        return link, uuid
    except Exception as e:
        logging.exception(f"CryptoCloud create error: {e}")
        return None, None


async def cc_is_paid(invoice_uuid: str) -> bool:
    if not (CRYPTOCLOUD_API_KEY and invoice_uuid):
        return False

    url = "https://api.cryptocloud.plus/v2/invoice/merchant/info"
    headers = {"Authorization": f"Token {CRYPTOCLOUD_API_KEY}"}
    payload = {"uuids": [invoice_uuid]}

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, headers=headers, json=payload, timeout=30) as resp:
                data = await resp.json()

        if data.get("status") != "success":
            return False

        result = data.get("result") or []
        if not result:
            return False

        invoice = result[0]
        status = (invoice.get("status") or "").lower()
        return status in ("paid", "overpaid")
    except Exception as e:
        logging.exception(f"CryptoCloud check error: {e}")
        return False


@dp.message(Command("testpay"))
async def test_payment_status(m: Message):
    await m.answer("🔍 Проверяю последний платёж...")
    payments = _load_payments()
    user_id = str(m.from_user.id)
    entry = payments.get(user_id)
    if not entry:
        await m.answer("❌ В payments.json нет записей о платеже.")
        return
    invoice_uuid = entry.get("invoice_uuid")
    paid = await cc_is_paid(invoice_uuid)
    await m.answer(f"🧾 Статус: {'✅ Оплачен' if paid else '❌ Не найден'}\nUUID: {invoice_uuid}")


@dp.message(Command("admin"))
async def admin_stats(m: Message):
    if ADMIN_ID and int(m.from_user.id) != int(ADMIN_ID):
        # Игнорируем или можно отправить сообщение "нет доступа"
        return

    users = _load_users()
    events = _load_events()
    banners = _load_banners()
    payments = _load_payments()

    total_users = len(users)
    now = datetime.now()
    active_users_24h = 0
    for u in users.values():
        last = _safe_dt(u.get("last_seen"))
        if last and (now - last).total_seconds() <= 24 * 3600:
            active_users_24h += 1

    total_events = len(events)
    active_events = 0
    paid_events = 0
    for ev in events:
        exp = _safe_dt(ev.get("expire"))
        if exp and exp > now:
            active_events += 1
        if not ev.get("is_free", True):
            paid_events += 1

    total_banners = len(banners)
    active_banners = 0
    for b in banners:
        exp = _safe_dt(b.get("expire"))
        if exp and exp > now:
            active_banners += 1

    total_payments = len(payments)

    text = (
        "<b>📊 Статистика бота</b>\n\n"
        f"👤 Пользователи всего: <b>{total_users}</b>\n"
        f"🟢 Активны за 24ч: <b>{active_users_24h}</b>\n\n"
        f"📌 Событий всего: <b>{total_events}</b>\n"
        f"🟡 Активных событий: <b>{active_events}</b>\n"
        f"💵 Платных событий: <b>{paid_events}</b>\n\n"
        f"🖼 Баннеров всего: <b>{total_banners}</b>\n"
        f"🟢 Активных баннеров: <b>{active_banners}</b>\n\n"
        f"🧾 Записей о платежах: <b>{total_payments}</b>"
    )
    await m.answer(text)

# ===================== FSM =====================

class AddEvent(StatesGroup):
    title = State()
    description = State()
    category = State()
    dt = State()          # используется только как шаг "цена" для маркета
    media = State()
    contact = State()
    lifetime = State()
    payment = State()
    upsell = State()
    upsell_more = State()
    pay_option = State()


class AddBanner(StatesGroup):
    media = State()
    description = State()
    link = State()
    duration = State()
    payment = State()


class SearchEvents(StatesGroup):
    menu = State()
    all = State()
    market = State()
    work = State()
    selfpromo = State()
    findyou = State()


# ===================== KEYBOARDS =====================

def kb_main():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📍 Найти события рядом")],
            [KeyboardButton(text="➕ Создать событие")],
            [KeyboardButton(text="⭐ Избранное")],
            [KeyboardButton(text="📩 Связаться с нами")],
        ],
        resize_keyboard=True
    )


def kb_back():
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="⬅ Назад")]],
        resize_keyboard=True
    )


def kb_media_step():
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="⬅ Назад")]],
        resize_keyboard=True
    )


def kb_categories():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🎉 Вечеринка"), KeyboardButton(text="💬 Свидание")],
            [KeyboardButton(text="🧠 Встреча по интересам"), KeyboardButton(text="⚽ Активность/Спорт")],
            [KeyboardButton(text="🛒 Куплю"), KeyboardButton(text="💰 Продам")],
            [KeyboardButton(text="💼 Ищу работу"), KeyboardButton(text="🧑‍💼 Предлагаю работу")],
            [KeyboardButton(text="✨ Покажи себя"), KeyboardButton(text="🔍 Ищу тебя")],
            [KeyboardButton(text="🎊 Поздравления"), KeyboardButton(text="🧭 Другое")],
            [KeyboardButton(text="⬅ Назад")],
        ],
        resize_keyboard=True
    )


def kb_lifetime():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🕐 1 день (бесплатно)")],
            [KeyboardButton(text="⏱ 3 дня — $0.8"), KeyboardButton(text="⏱ 7 дней — $1.5")],
            [KeyboardButton(text="⏱ 30 дней — $3.0")],
            [KeyboardButton(text="⬅ Назад")],
        ],
        resize_keyboard=True
    )


def kb_payment():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="💳 Получить ссылку на оплату")],
            [KeyboardButton(text="✅ Я оплатил")],
            [KeyboardButton(text="⬅ Назад")],
        ],
        resize_keyboard=True
    )


def kb_payment_method():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="💳 Оплата картой (BitPapa)")],
            [KeyboardButton(text="🪙 Оплата криптовалютой (USDT)")],
            [KeyboardButton(text="⬅ Назад")],
        ],
        resize_keyboard=True
    )


def kb_upsell():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="⭐ Продвижение ТОП")],
            [KeyboardButton(text="📣 Push-рассылка (30 км)")],
            [KeyboardButton(text="🖼 Баннер (премиум)")],
            [KeyboardButton(text="🌍 Оставить без доп.опций")]
        ],
        resize_keyboard=True
    )
def kb_upsell_more():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="➕ Добавить ещё опцию")],
            [KeyboardButton(text="🏠 Главное меню")],
        ],
        resize_keyboard=True
    )


def kb_top_duration():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="⭐ 1 день — $1"), KeyboardButton(text="⭐ 3 дня — $2")],
            [KeyboardButton(text="⭐ 7 дней — $3"), KeyboardButton(text="⭐ 15 дней — $5")],
            [KeyboardButton(text="⭐ 30 дней — $8")],
            [KeyboardButton(text="⬅ Назад")],
        ],
        resize_keyboard=True
    )


def kb_banner_duration():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📅 1 день — $3"), KeyboardButton(text="📅 3 дня — $7")],
            [KeyboardButton(text="📅 7 дней — $12"), KeyboardButton(text="📅 15 дней — $18")],
            [KeyboardButton(text="📅 30 дней — $30")],
            [KeyboardButton(text="⬅ Назад")],
        ],
        resize_keyboard=True
    )


# ===================== TEXT / FORMAT HELPERS =====================

def sanitize(text: str) -> str:
    return re.sub(r"[^\S\r\n]+", " ", (text or "")).strip()


def format_event_card(ev: dict, with_distance: Optional[float] = None) -> str:
    desc = f"\n📝 {sanitize(ev.get('description') or '')}" if ev.get("description") else ""
    contact = f"\n☎ <b>Контакт:</b> {sanitize(ev.get('contact') or '')}" if ev.get("contact") else ""
    top = " 🔥<b>ТОП</b>" if ev.get("is_top") else ""
    dist = f"\n📏 Расстояние: {with_distance:.1f} км" if with_distance is not None else ""
    price_part = f"\n💵 Цена: {sanitize(ev.get('price') or '')}" if ev.get("price") else ""
    return (
        f"📌 <b>{sanitize(ev['title'])}</b>{top}\n"
        f"📍 {sanitize(ev['category'])}{desc}"
        f"{price_part}{contact}{dist}"
    )


def format_banner_caption(b: dict) -> str:
    parts = []
    if b.get("text"):
        parts.append(sanitize(b["text"]))
    if b.get("link"):
        parts.append(f"🔗 {sanitize(b['link'])}")
    if b.get("lat") is not None and b.get("lon") is not None:
        g = f"https://www.google.com/maps?q={b['lat']},{b['lon']}"
        parts.append(f"🗺 <a href=\"{g}\">Показать на карте</a>")
    return "\n".join(parts) if parts else "Рекламный баннер"


async def send_event_media(chat_id: int, ev: dict, with_distance: Optional[float] = None):
    text = format_event_card(ev, with_distance=with_distance)
    buttons = []

    # Кнопка карты
    if ev.get("lat") is not None and ev.get("lon") is not None:
        gmap = f"https://www.google.com/maps?q={ev['lat']},{ev['lon']}"
        buttons.append([InlineKeyboardButton(text="🌐 Открыть в Google Maps", url=gmap)])

    # Кнопки избранного / удалить
    if ev.get("id") is not None:
        row = [InlineKeyboardButton(text="⭐ В избранное", callback_data=f"fav_add:{ev['id']}")]
        if ev.get("author") and int(ev["author"]) == int(chat_id):
            row.append(InlineKeyboardButton(text="🗑 Удалить", callback_data=f"ev_del:{ev['id']}"))
        buttons.append(row)

    ikb = InlineKeyboardMarkup(inline_keyboard=buttons) if buttons else None
    media = ev.get("media_files") or []

    # Локальные файлы (например, баннеры/лого) оборачиваем в FSInputFile
    for f in media:
        if f.get("is_local"):
            f["file_id"] = FSInputFile(f["file_id"])

    # Несколько медиа — отправляем альбом без подписи, потом текст + кнопки
    if len(media) > 1:
        group = []
        for f in media:
            caption = None
            if f["type"] == "photo":
                group.append(InputMediaPhoto(media=f["file_id"], caption=caption, parse_mode="HTML"))
            elif f["type"] == "video":
                group.append(InputMediaVideo(media=f["file_id"], caption=caption, parse_mode="HTML"))
        await bot.send_media_group(chat_id, group)
        if ikb or text:
            await bot.send_message(chat_id, text, reply_markup=ikb)

    # Одно медиа — прикрепляем подпись и кнопки
    elif len(media) == 1:
        f = media[0]
        if f["type"] == "photo":
            await bot.send_photo(chat_id, f["file_id"], caption=text, reply_markup=ikb)
        elif f["type"] == "video":
            await bot.send_video(chat_id, f["file_id"], caption=text, reply_markup=ikb)

    # Нет медиа — подставляем логотип, если он есть
    else:
        logo_path = None
        for ext in ("png", "jpg", "jpeg"):
            p = f"imgonline-com-ua-Resize-poVtNXt7aue6.{ext}"
            if os.path.exists(p):
                logo_path = p
                break

        if logo_path:
            await bot.send_photo(chat_id, FSInputFile(logo_path), caption=text, reply_markup=ikb)
        elif LOGO_URL:
            await bot.send_photo(chat_id, LOGO_URL, caption=text, reply_markup=ikb)
        else:
            await bot.send_message(chat_id, text, reply_markup=ikb)


    if media_type == "photo" and file_id:
        # Локальный файл (например, логотип) — отправляем через FSInputFile
        if b.get("is_local"):
            file_id = FSInputFile(file_id)
        await bot.send_photo(chat_id, file_id, caption=cap, parse_mode="HTML")
    elif media_type == "video" and file_id:
        await bot.send_video(chat_id, file_id, caption=cap, parse_mode="HTML")
    else:
        await bot.send_message(chat_id, cap, parse_mode="HTML")


def user_has_active_free_event(user_id: int, category: str) -> bool:
    """
    Проверяем, есть ли у пользователя уже активное БЕСПЛАТНОЕ объявление в категории.
    Смотрим события с is_free=True и не истёкшим expire.
    """
    events = _load_events()
    now = datetime.now()
    for ev in events:
        if int(ev.get("author", 0)) != int(user_id):
            continue
        if ev.get("category") != category:
            continue
        exp = _safe_dt(ev.get("expire"))
        if not exp or exp <= now:
            continue
        if ev.get("is_free"):
            return True
    return False


async def show_nearby_banner_for_user(m: Message):
    """
    Показать один баннер при старте.
    1) Сначала ищем баннеры в радиусе DEFAULT_RADIUS_KM от последней геопозиции пользователя.
    2) Если по гео ничего не нашли, но у пользователя есть свой активный баннер — показываем его.
    """
    user_id = m.from_user.id
    users = _load_users()
    u = users.get(str(user_id))

    banners = _load_banners()
    now = datetime.now()

    # --- 1. Баннеры по геолокации ---
    loc_banner_candidates = []
    if u:
        loc = u.get("last_location")
        if loc and loc.get("lat") is not None and loc.get("lon") is not None:
            lat = loc["lat"]
            lon = loc["lon"]
            for b in banners:
                exp = _safe_dt(b.get("expire"))
                if not exp or exp <= now:
                    continue
                b_lat = b.get("lat")
                b_lon = b.get("lon")
                if b_lat is None or b_lon is None:
                    continue
                try:
                    dist = geodesic((lat, lon), (b_lat, b_lon)).km
                except Exception:
                    continue
                if dist <= DEFAULT_RADIUS_KM:
                    loc_banner_candidates.append((b, dist))

    if loc_banner_candidates:
        # Берём самый свежий по id
        loc_banner_candidates.sort(key=lambda x: x[0].get("id", 0), reverse=True)
        banner, _ = loc_banner_candidates[0]
        try:
            await send_banner(m.chat.id, banner)
        except Exception as e:
            logging.exception(f"Ошибка отправки баннера по гео: {e}")
        return

    # --- 2. Если по гео не нашли — показываем ЛИЧНЫЙ баннер владельцу ---
    owner_banners = []
    for b in banners:
        exp = _safe_dt(b.get("expire"))
        if not exp or exp <= now:
            continue
        if int(b.get("owner", 0)) == int(user_id):
            owner_banners.append(b)

    if owner_banners:
        owner_banners.sort(key=lambda x: x.get("id", 0), reverse=True)
        banner = owner_banners[0]
        try:
            await send_banner(m.chat.id, banner)
        except Exception as e:
            logging.exception(f"Ошибка отправки личного баннера: {e}")


# ===================== START / WELCOME =====================

# ===================== START / WELCOME =====================

async def send_logo_then_welcome(m: Message):
    logo_path = None
    for ext in ("png", "jpg", "jpeg"):
        p = f"imgonline-com-ua-Resize-poVtNXt7aue6.{ext}"
        if os.path.exists(p):
            logo_path = p
            break
    try:
        if logo_path:
            await m.answer_photo(FSInputFile(logo_path))
        elif LOGO_URL:
            await m.answer_photo(LOGO_URL)
    except Exception:
        pass

    await asyncio.sleep(0.5)

    await m.answer(
        "⚡️ <b>PartyRadar</b>\n"
        "\n"
        "Здесь ты находишь события и создаёшь свои — быстро и удобно.\n\n"
        "Функционал:\n"
        "• 🔎 Поиск событий рядом\n"
        "• ➕ Создание своих объявлений\n"
        "• ⭐ ТОП-продвижение\n"
        "• 📣 Push по радиусу\n"
        "• 🖼 Баннеры над лентой\n\n"
        "Выбирай, что делаем 👇",
        reply_markup=kb_main()
    )
    
# ================== UNITPAY VERIFICATION FILE ==================

async def handle_unitpay_verification(request):
    return web.Response(
        text="25a558d08ef4438fbefddd2aae7fe5",
        content_type="text/plain"
    )

app = web.Application()
app.router.add_get('/verification-25a55.txt', handle_unitpay_verification)

# ================== TELEGRAM WEBHOOK ==================
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application

SimpleRequestHandler(dispatcher=dp, bot=bot).register(app, path="/webhook")
setup_application(app, dp)

@dp.message(Command("start"))
async def start_cmd(m: Message, state: FSMContext):
    await state.clear()
    await send_logo_then_welcome(m)
    # Попробуем показать баннер рядом (если есть сохранённая геолокация)
    await show_nearby_banner_for_user(m)


# ===================== SUPPORT =====================

@dp.message(F.text == "📩 Связаться с нами")
async def support(m: Message):
    await m.answer(
        "💬 Идеи, баги, реклама, коллаборации:\n"
        "👉 <b>@drscorohod</b>\n\n"
        "Пишешь — читаем. Не всегда мгновенно, но живые люди 🙂",
        reply_markup=kb_main()
    )


# ===================== СОЗДАНИЕ СОБЫТИЙ =====================

@dp.message(F.text == "➕ Создать событие")
async def create_event_start(m: Message, state: FSMContext):
    await state.set_state(AddEvent.title)
    await m.answer(
        "📝 Давай создадим событие.\n\n"
        "Сначала напиши <b>краткий заголовок</b> — так, чтобы сразу было понятно, о чём движ.",
        reply_markup=kb_back()
    )


@dp.message(AddEvent.title)
async def ev_title(m: Message, state: FSMContext):
    if m.text == "⬅ Назад":
        await state.clear()
        return await m.answer("Окей, вернулись в главное меню 👋", reply_markup=kb_main())

    await state.update_data(title=sanitize(m.text))
    await state.set_state(AddEvent.description)
    await m.answer(
        "✏️ Теперь введи <b>описание</b> события.\n\n"
        "Укажи обязательно:\n"
        "- дату и время (например, 25.10 в 19:30)\n"
        "- место\n"
        "- цену / стоимость участия (если есть)\n"
        "- формат и важные условия (дресс-код, ограничения и т.п.)\n\n"
        "⚠️ Дата и время больше нигде не спрашиваются — укажи их прямо в тексте.\n\n"
        "Если что-то пошло не так — открой меню слева от строки ввода и нажми «Перезапустить бота».",
        reply_markup=kb_back(),
    )


@dp.message(AddEvent.description)
async def ev_desc(m: Message, state: FSMContext):
    if m.text == "⬅ Назад":
        await state.set_state(AddEvent.title)
        return await m.answer("✏️ Напиши заголовок события:", reply_markup=kb_back())

    await state.update_data(description=sanitize(m.text))
    await state.set_state(AddEvent.category)

    await m.answer(
        "📌 Выбери категорию события.\n\n"
        "📝 Если что-то пошло не так — нажми кнопку меню слева от строки ввода и выбери «Перезапустить бота».",
        reply_markup=kb_categories()
    )


MARKET_CATS = ("🛒 Куплю", "💰 Продам")


@dp.message(AddEvent.category)
async def ev_cat(m: Message, state: FSMContext):
    if m.text == "⬅ Назад":
        await state.set_state(AddEvent.description)
        return await m.answer("🧾 Введи описание события:", reply_markup=kb_back())

    cat = sanitize(m.text)
    await state.update_data(category=cat)

    # Маркет — отдельный шаг для цены
    if cat in MARKET_CATS:
        await state.set_state(AddEvent.dt)
        return await m.answer(
            "💵 Укажи цену (можно с символом валюты):\n"
            "Например: <b>150€</b>, <b>200$</b>, <b>5000₽</b>, <b>договорная</b>.\n\n"
            "❗ Срок актуальности и детали сделки укажи в описании, не забудь.",
            reply_markup=kb_back()
        )

    # Работа
    if cat in ("💼 Ищу работу", "🧑‍💼 Предлагаю работу"):
        await state.update_data(price=None, media_files=[])
        await state.set_state(AddEvent.media)
        return await m.answer(
            "💼 Рубрика «Работа».\n\n"
            "В описании уже должно быть:\n"
            "• формат (офис/удалёнка)\n"
            "• график\n"
            "• зарплата / вилка\n"
            "• требования и дата старта.\n\n"
            "📎 Сейчас шаг медиа. Прикрепи до 3 фото/видео или сразу отправь геолокацию.\n"
            "❗ Если планируешь делать баннер — <b>обязательно загрузи медиа на этом шаге</b>.",
            reply_markup=kb_media_step()
        )

    # Покажи себя
    if cat == "✨ Покажи себя":
        await state.update_data(price=None, media_files=[])
        await state.set_state(AddEvent.media)
        return await m.answer(
            "✨ Здесь можно рассказать о себе или своём проекте.\n\n"
            "В описании уже должны быть:\n"
            "• город/район\n"
            "• возраст (если актуально)\n"
            "• кто ты и что ищешь\n"
            "• цена за услуги (если есть).\n\n"
            "📎 Сейчас прикрепи до 3 фото/видео или отправь геолокацию.\n"
            "❗ Если хочешь потом баннер — медиа нужно загрузить <b>именно сейчас</b>.",
            reply_markup=kb_media_step()
        )

    # Ищу тебя
    if cat == "🐾 Ищу тебя":
        await state.update_data(price=None, media_files=[])
        await state.set_state(AddEvent.media)
        return await m.answer(
            "👀 Поиск людей или питомцев.\n\n"
            "В описании уже должно быть:\n"
            "• кого ищешь\n"
            "• где и когда видел(а) в последний раз\n"
            "• как с тобой связаться\n\n"
            "📎 Прикрепи до 3 фото/видео (если есть) или отправь геолокацию места.\n\n"
            "ℹ️ Если что-то пошло не так — открой меню слева от строки ввода и выбери «Перезапустить бота».",
            reply_markup=kb_media_step(),
        )

    # Поздравления
    elif cat == "🎉 Поздравления":
        await state.update_data(price=None, media_files=[])
        await state.set_state(AddEvent.media)
        return await m.answer(
            "🎉 Рубрика поздравлений и приятных новостей.\n\n"
            "В описании можно указать повод, дату и кому адресовано.\n\n"
            "📎 Прикрепи до 3 фото/видео или отправь геолокацию (если уместно).\n\n"
            "ℹ️ Если что-то пошло не так — открой меню слева от строки ввода и выбери «Перезапустить бота».",
            reply_markup=kb_media_step(),
        )

    # Остальные — просто события
    else:
        await state.update_data(price=None, media_files=[])
        await state.set_state(AddEvent.media)
        return await m.answer(
            "📸 Шаг медиа!\n\n"
            "Прикрепи до 3 фото/видео (атмосфера, место, афиша)\n"
            "или отправь геолокацию.\n\n"
            "Если хочешь потом запускать <b>баннер</b> — для этого события медиа нужно загрузить <b>именно сейчас</b>.\n"
            "⚠️ Аудио и кружки не поддерживаются.\n\n"
            "ℹ️ Если что-то пошло не так — открой меню слева от строки ввода и выбери «Перезапустить бота».",
            reply_markup=kb_media_step(),
        )


@dp.message(AddEvent.dt)
async def ev_dt(m: Message, state: FSMContext):
    # Используется только как шаг "цена" для маркета
    if m.text == "⬅ Назад":
        await state.set_state(AddEvent.category)
        return await m.answer("📂 Вернулись к выбору категории:", reply_markup=kb_categories())

    await state.update_data(price=sanitize(m.text), media_files=[])
    await state.set_state(AddEvent.media)
    await m.answer(
        "📎 Шаг медиа: прикрепи до 3 фото/видео или отправь геолокацию.\n\n"
        "❗ Если хочешь в будущем сделать баннер — загрузи медиа сейчас.\n"
        "⚠ Аудио и кружки не поддерживаются.",
        reply_markup=kb_media_step()
    )


MAX_MEDIA = 3


@dp.message(AddEvent.media, F.content_type.in_({ContentType.PHOTO, ContentType.VIDEO}))
async def ev_media(m: Message, state: FSMContext):
    data = await state.get_data()
    files = data.get("media_files", [])
    if len(files) >= MAX_MEDIA:
        return await m.answer("⚠ Уже 3 файла. Теперь отправь геолокацию.", reply_markup=kb_media_step())

    if m.photo:
        files.append({"type": "photo", "file_id": m.photo[-1].file_id})
    elif m.video:
        files.append({"type": "video", "file_id": m.video.file_id})

    await state.update_data(media_files=files)
    left = MAX_MEDIA - len(files)
    await m.answer(
        f"✅ Файл добавлен ({len(files)}/{MAX_MEDIA}).\n"
        + ("Можешь добавить ещё или " if left else "")
        + "отправь геолокацию для следующего шага.",
        reply_markup=kb_media_step()
    )


@dp.message(AddEvent.media, F.content_type.in_({ContentType.AUDIO, ContentType.VOICE}))
async def ev_media_unsupported(m: Message, state: FSMContext):
    await m.answer(
        "⚠ Аудиосообщения и кружки не поддерживаются.\n"
        "Прикрепи, пожалуйста, <b>фото или видео</b>, либо сразу отправь геолокацию.",
        reply_markup=kb_media_step()
    )


@dp.message(AddEvent.media, F.text == "⬅ Назад")
async def ev_media_back(m: Message, state: FSMContext):
    data = await state.get_data()
    files = data.get("media_files", [])
    category = data.get("category")

    if files:
        await m.answer(
            "ℹ️ Уже есть прикреплённые медиа.\n"
            "Если хочешь полностью изменить медиа — проще пересоздать объявление заново.",
            reply_markup=kb_media_step()
        )
        return

    if category in MARKET_CATS:
        await state.set_state(AddEvent.dt)
        return await m.answer("💵 Укажи цену:", reply_markup=kb_back())

    await state.set_state(AddEvent.category)
    await m.answer("🧭 Вернулись к выбору категории:", reply_markup=kb_categories())


@dp.message(AddEvent.media, F.location)
async def ev_media_location(m: Message, state: FSMContext):
    await state.update_data(lat=m.location.latitude, lon=m.location.longitude)

    users = _load_users()
    u = users.get(str(m.from_user.id)) or {}
    u["last_location"] = {"lat": m.location.latitude, "lon": m.location.longitude}
    u["last_seen"] = datetime.now().isoformat()
    users[str(m.from_user.id)] = u
    _save_users(users)

    await state.set_state(AddEvent.contact)
    await m.answer(
        "📞 Последний шаг по содержанию.\n"
        "Укажи контакт: @username, телефон или ссылку.\n"
        "Или напиши «Пропустить».",
        reply_markup=kb_back()
    )

@dp.message(AddEvent.contact)
async def ev_contact(m: Message, state: FSMContext):
    if m.text == "⬅ Назад":
        await state.set_state(AddEvent.media)
        return await m.answer(
            "Вернулись к шагу медиа.\n"
            "Отправь фото/видео или геолокацию.",
            reply_markup=kb_media_step()
        )

    if m.text.lower().strip() != "пропустить":
        await state.update_data(contact=sanitize(m.text))

    await state.set_state(AddEvent.lifetime)
    await m.answer(
        "⏳ Выбери срок жизни объявления. Это влияет на то, как долго событие будет видно другим пользователям.\n\n"
        "Если что-то пошло не так — нажми меню слева от строки ввода и выбери «Перезапуск бота».\n\n"
        "Напоминание: дату/время, цену и детали ты уже указал в описании.",
        reply_markup=kb_lifetime()
    )



@dp.message(AddEvent.media)
async def ev_media_other(m: Message, state: FSMContext):
    # Любой другой ввод на шаге медиа – не сбрасываем FSM,
    # а ещё раз объясняем, что нужно сделать.
    await m.answer(
        "Сейчас мы на шаге медиа. Этот шаг нужен, чтобы прикрепить фото/видео "
        "или геолокацию к событию.\n\n"
        "Если что-то пошло не так — нажми меню слева от строки ввода и выбери "
        "«Перезапуск бота».\n\n"
        "Отправь, пожалуйста, <b>фото/видео</b> или <b>геолокацию</b> места события.\n\n"
        "Если планируешь делать баннер — медиа нужно загрузить именно здесь.",
        reply_markup=kb_media_step()
    )


@dp.message(AddEvent.contact)
async def ev_contact(m: Message, state: FSMContext):
    if m.text == "⏪ Назад":
        await state.set_state(AddEvent.media)
        return await m.answer(
            "Вернулись к шагу медиа.\n"
            "Отправь фото/видео или геолокацию.",
            reply_markup=kb_media_step()
        )

    if m.text.lower().strip() != "пропустить":
        await state.update_data(contact=sanitize(m.text))

    await state.set_state(AddEvent.lifetime)
    await m.answer(
        "⏳ Выбери срок жизни объявления. Это влияет на то, как долго событие "
        "будет видно другим пользователям.\n\n"
        "Если что-то пошло не так — нажми меню слева от строки ввода и выбери "
        "«Перезапуск бота».\n\n"
        "Напоминание: дату/время, цену и детали ты уже указал в описании.",
        reply_markup=kb_lifetime()
    )


@dp.message(AddEvent.contact)
async def ev_contact(m: Message, state: FSMContext):
    if m.text == "⬅ Назад":
        await state.set_state(AddEvent.media)
        return await m.answer(
            "Вернулись к шагу медиа.\n"
            "Отправь фото/видео или геолокацию.",
            reply_markup=kb_media_step()
        )

    if m.text.lower().strip() != "пропустить":
        await state.update_data(contact=sanitize(m.text))

    await state.set_state(AddEvent.lifetime)
    await m.answer(
    "⏳ Выбери срок жизни объявления.\n\n"
    "Если что-то пошло не так — нажми меню слева от строки ввода и выбери «Перезапуск бота».\n\n"
    "Напоминание: дату/время, цену и детали ты уже указал в описании.",
    reply_markup=kb_lifetime()
)


# ======== АВТОМАТИЧЕСКАЯ МОДЕРАЦИЯ =========

FORBIDDEN_KEYWORDS_GROUPS = {
    "adult": ["интим", "эскорт", "секс услуги", "sex услуги", "минет", "onlyfans", "онлифанс", "порн", "pornhub"],
    "drugs": ["закладка", "закладки", "наркотик", "наркота", "метамфетамин", "амфетамин",
              "героин", "кокаин", "марихуана", "шишки", "спайс"],
    "weapons": ["оружие", "пистолет", "автомат калашникова", "ak-47", "ak47",
                "нож-бабочка", "куплю гранату", "продам гранату", "продам оружие"],
    "gambling": ["ставки на спорт", "казино", "1xbet", "1хбет", "букмекерская контора",
                 "игровые автоматы", "слоты", "рулетка"],
    "fraud": ["легкий заработок без вложений", "быстрый заработок", "доход 1000$ в день",
              "пирамида", "финансовая пирамида", "инвестиции без риска"],
}

FORBIDDEN_DOMAINS = [
    "onlyfans.com", "pornhub.com", "xvideos.com", "xhamster.com",
    "1xbet.com", "ggbet", "mostbet", "casino", "pin-up"
]

SUSPICIOUS_SHORTLINKS = ["bit.ly", "tinyurl.com", "cutt.ly", "t.me/joinchat", "t.me/+"]


def _normalize_text(text: str) -> str:
    return (text or "").lower()


def _check_text_moderation(text: str) -> Tuple[bool, Optional[str]]:
    t = _normalize_text(text)

    for dom in FORBIDDEN_DOMAINS:
        if dom in t:
            return False, "Объявление содержит запрещённые ссылки или ресурсы."

    for dom in SUSPICIOUS_SHORTLINKS:
        if dom in t:
            return False, "Объявление содержит подозрительные сокращённые ссылки."

    for group, words in FORBIDDEN_KEYWORDS_GROUPS.items():
        for w in words:
            if w in t:
                if group == "adult":
                    return False, "Объявление похоже на 18+ контент. Такое мы не публикуем."
                if group == "drugs":
                    return False, "Объявление похоже на рекламу запрещённых веществ."
                if group == "weapons":
                    return False, "Объявление похоже на продажу оружия."
                if group == "gambling":
                    return False, "Объявление похоже на рекламу азартных игр."
                if group == "fraud":
                    return False, "Объявление похоже на сомнительную финансовую схему."
                return False, "Объявление не прошло автоматическую модерацию."

    return True, None


def check_event_moderation(data: dict) -> Tuple[bool, Optional[str]]:
    parts = []
    for key in ("title", "description", "contact", "category"):
        val = data.get(key)
        if val:
            parts.append(str(val))
    full_text = "\n".join(parts)
    return _check_text_moderation(full_text)


async def publish_event(m: Message, data: dict, hours: int, is_free: bool):
    media_files = data.get("media_files", [])
    if not media_files:
        # подставим логотип как заглушку
        for ext in ("png", "jpg", "jpeg"):
            p = f"imgonline-com-ua-Resize-poVtNXt7aue6.{ext}"
            if os.path.exists(p):
                media_files = [{"type": "photo", "file_id": p, "is_local": True}]
                break

    events = _load_events()
    now = datetime.now()
    expires = now + timedelta(hours=hours)
    new_id = (events[-1]["id"] + 1) if events else 1

    ev = {
        "id": new_id,
        "author": m.from_user.id,
        "title": data["title"],
        "description": data.get("description"),
        "category": data["category"],
        "datetime": None,  # даты больше нет, всё в описании
        "price": data.get("price"),
        "lat": data.get("lat"),
        "lon": data.get("lon"),
        "media_files": media_files,
        "contact": data.get("contact"),
        "expire": expires.isoformat(),
        "created": now.isoformat(),
        "notified": False,
        "is_top": False,
        "top_expire": None,
        "top_paid_at": None,
        "is_free": bool(is_free),
    }

    _save_events(events + [ev])
    return ev


@dp.message(AddEvent.lifetime)
async def ev_lifetime(m: Message, state: FSMContext):
    if m.text == "⬅ Назад":
        await state.set_state(AddEvent.contact)
        return await m.answer("☎ Укажи контакт или напиши «Пропустить».", reply_markup=kb_back())

    if m.text not in LIFETIME_OPTIONS:
        return await m.answer("Выбери срок из списка:", reply_markup=kb_lifetime())

    hours = LIFETIME_OPTIONS[m.text]
    data = await state.get_data()

    # Модерация
    ok, reason = check_event_moderation(data)
    if not ok:
        await state.clear()
        return await m.answer(
            reason + "\n\nПопробуй переписать текст более нейтрально 🙏",
            reply_markup=kb_main()
        )

    user_id = m.from_user.id
    category = data.get("category")

    # Бесплатный 1 день с лимитом 1 объявление на категорию
    if hours == 24:
        if user_has_active_free_event(user_id, category):
            amount = TARIFFS_USD[24]
            await state.update_data(paid_lifetime=24, _pay_uuid=None, free_limit_exceeded=True)
            await state.set_state(AddEvent.payment)
            return await m.answer(
                "⚠ В этой категории у тебя уже есть активное <b>бесплатное</b> объявление.\n\n"
                "Можно разместить ещё одно объявление на <b>1 день</b> как платное.\n"
                f"💵 Стоимость: <b>${amount}</b>\n\n"
                "Напоминание: дату/время и цену указывай в описании.",
                reply_markup=kb_payment_method()
            )

        # бесплатное размещение
        ev = await publish_event(m, data, hours, is_free=True)

        await state.set_state(AddEvent.upsell)
        return await m.answer(
            "✅ Событие опубликовано <b>бесплатно</b> на 1 день.\n\n"
            "Сейчас можно усилить охват: ТОП, Push или баннер.",
            reply_markup=kb_upsell()
        )

    # Платные сроки показа (3/7/30 дней)
    amount = TARIFFS_USD[hours]
    await state.update_data(paid_lifetime=hours, _pay_uuid=None, free_limit_exceeded=False)
    await state.set_state(AddEvent.payment)
    await m.answer(
        f"⏳ <b>Платный срок показа</b>\n"
        f"Ты выбрал: <b>{m.text}</b>\n"
        f"Стоимость: <b>${amount}</b>\n\n"
        "После оплаты мы опубликуем событие и предложим доп.опции (ТОП, Push, баннер).\n\n"
        "Выбери способ оплаты:",
        reply_markup=kb_payment_method()
    )



@dp.message(AddEvent.payment, F.text == "💳 Оплата картой (BitPapa)")
async def ev_pay_method_card(m: Message, state: FSMContext):
    # Инструкция по оплате через BitPapa
    txt = (
        "💳 <b>Оплата картой через BitPapa</b>\n\n"
        "1️⃣ Открой BitPapa по ссылке:\n"
        f"{BITPAPA_REF_LINK}\n\n"
        "2️⃣ Купи USDT удобным способом. Рекомендуем продавцов со статусом 🟢 <b>Online</b>, "
        "рейтингом от 99% и с 100+ завершёнными сделками.\n"
        "3️⃣ После покупки вернись в бота, нажми «💳 Получить ссылку на оплату» и оплати счёт USDT.\n"
        "4️⃣ После отправки USDT по ссылке нажми «✅ Я оплатил»."
    )
    await m.answer(txt, reply_markup=kb_payment())

@dp.message(AddEvent.payment, F.text == "🪙 Оплата криптовалютой (USDT)")
async def ev_pay_method_crypto(m: Message, state: FSMContext):
    await m.answer(
        "🪙 Ты выбрал оплату уже имеющейся криптовалютой (USDT).\n\n"
        "Сейчас можно получить ссылку на оплату, оплатить её из своего кошелька, "
        "а затем нажать «✅ Я оплатил».",
        reply_markup=kb_payment()
    )

@dp.message(AddEvent.payment, F.text == "💳 Получить ссылку на оплату")
async def ev_pay_get(m: Message, state: FSMContext):
    data = await state.get_data()
    hours = data.get("paid_lifetime")
    if not hours:
        return await m.answer("❌ Нет активного платного тарифа.", reply_markup=kb_payment())

    # Если уже есть активный счёт и он создан менее 24 часов назад — просто повторно отправляем ссылку
    invoice_uuid = data.get("_pay_uuid")
    existing_link = data.get("_pay_link")
    created_at_str = data.get("_pay_created")
    created_at = _safe_dt(created_at_str) if created_at_str else None

    if invoice_uuid and existing_link and created_at:
        if datetime.now() - created_at < timedelta(hours=24):
            return await m.answer(
                f"У тебя уже есть активный счёт (действителен 24 часа):\n{existing_link}\n\n"
                "После оплаты нажми «✅ Я оплатил».",
                reply_markup=kb_payment()
            )

    amount = TARIFFS_USD[hours]
    order_id = str(m.from_user.id)
    link, invoice_id = await cc_create_invoice(amount, order_id, f"PartyRadar: event lifetime {hours}h")

    if not link or not invoice_id:
        return await m.answer(
            "⚠️ Не удалось получить ссылку на счёт. Проверь API ключ.",
            reply_markup=kb_payment()
        )

    pay = _load_payments()
    pay[str(m.from_user.id)] = {
        "type": "event_lifetime",
        "user_id": m.from_user.id,
        "invoice_uuid": invoice_id,
        "payload": {"hours": hours, "data": data},
    }
    _save_payments(pay)

    await state.update_data(
        _pay_uuid=invoice_id,
        _pay_link=link,
        _pay_created=datetime.now().isoformat()
    )

    await m.answer(
        f"💳 Ссылка на оплату:\n{link}\n\n"
        "После оплаты нажми «✅ Я оплатил».\n\n"
        "⚠ Платёжная система может брать свою комиссию.",
        reply_markup=kb_payment()
    )

@dp.message(AddEvent.payment, F.text == "✅ Я оплатил")
async def ev_pay_check(m: Message, state: FSMContext):
    data = await state.get_data()
    invoice_uuid = data.get("_pay_uuid")
    hours = data.get("paid_lifetime")
    already_published = data.get("already_published")

    # Если платёж уже был успешно обработан ранее — не публикуем событие повторно
    if already_published:
        await state.set_state(AddEvent.upsell)
        return await m.answer(
            "✅ Оплата уже подтверждена, событие опубликовано.\n"
            "Можешь выбрать дополнительные опции:",
            reply_markup=kb_upsell()
        )

    if not invoice_uuid or not hours:
        return await m.answer("⚠️ Ошибка: не найден счёт или тариф.", reply_markup=kb_payment())

    await m.answer("🔍 Проверяю оплату...")
    paid = await cc_is_paid(invoice_uuid)
    if not paid:
        return await m.answer(
            "❌ Оплата пока не найдена.\n"
            "Если ты только что оплатил — подожди минуту и нажми ещё раз.",
            reply_markup=kb_payment()
        )

    await m.answer("☑️ Оплата подтверждена! Публикую событие...")

    # публикуем событие как платное (только один раз)
    ev = await publish_event(m, data, hours, is_free=False)
    try:
        await send_event_media(m.chat.id, ev)
    except Exception:
        await m.answer(format_event_card(ev))

    # помечаем в состоянии, что это событие уже опубликовано по этому платежу
    await state.update_data(already_published=True)

    await state.set_state(AddEvent.upsell)
    await m.answer(
        "✅ Событие опубликовано!\n"
        "Теперь можешь включить доп.опции для большего охвата:",
        reply_markup=kb_upsell()
    )


@dp.message(AddEvent.payment, F.text == "⬅ Назад")
async def ev_pay_back(m: Message, state: FSMContext):
    await state.set_state(AddEvent.lifetime)
    await m.answer("🔙 Вернулись к выбору срока:", reply_markup=kb_lifetime())


# ===================== UPSELL: TOP / PUSH / BANNER =====================

async def send_push_for_event(ev: dict) -> int:
    """Рассылка события всем пользователям в радиусе DEFAULT_RADIUS_KM."""
    lat = ev.get("lat")
    lon = ev.get("lon")
    if lat is None or lon is None:
        return 0

    users = _load_users()
    event_loc = (lat, lon)
    sent = 0

    for uid, info in users.items():
        loc = info.get("last_location") or {}
        u_lat = loc.get("lat")
        u_lon = loc.get("lon")
        if u_lat is None or u_lon is None:
            continue
        dist = geodesic((u_lat, u_lon), event_loc).km
        if dist > DEFAULT_RADIUS_KM:
            continue
        try:
            await send_event_media(int(uid), ev)
            sent += 1
        except Exception as e:
            logging.exception(f"Ошибка PUSH пользователю {uid}: {e}")

    return sent


@dp.message(AddEvent.upsell)
async def ev_upsell(m: Message, state: FSMContext):
    txt = m.text or ""

    if txt == "⬅ Назад":
        await state.clear()
        return await m.answer("Главное меню:", reply_markup=kb_main())

    if txt == "🌍 Оставить без доп.опций":
        await state.clear()
        return await m.answer(
            "✔️ Готово! Событие опубликовано.\n"
            "Если захочешь — всегда можно вернуться и докупить ТОП, Push или баннер.",
            reply_markup=kb_main()
        )

    # Выбор ТОПа – показываем сроки ТОП
    if txt == "⭐ Продвижение ТОП":
        await m.answer(
            "<b>⭐ТОП-продвижение</b> — поднимает твое событие в начало списка, делая его заметным для всех пользователей.\n"
            "Это помогает быстрее собрать просмотры и отклики!\n",
            reply_markup=kb_top_duration()
        )

        await state.update_data(
            opt_type="top",
            opt_event_id=None,
            opt_days=None,
            _pay_uuid=None
        )

        await state.set_state(AddEvent.pay_option)
        return await m.answer("Выбери срок ТОП-продвижения:", reply_markup=kb_top_duration())

    # Push
    if txt == "📣 Push-рассылка (30 км)":
        events = _load_events()
        user_events = [e for e in events if int(e.get("author", 0)) == int(m.from_user.id)]
        if not user_events:
            await state.clear()
            return await m.answer("❌ У тебя пока нет опубликованных событий.", reply_markup=kb_main())

        current = user_events[-1]
        await state.update_data(
            opt_type="push",
            opt_event_id=current["id"],
            opt_days=None,
            _pay_uuid=None,
        )

        await state.set_state(AddEvent.pay_option)
        return await m.answer(
            "📣 Push-рассылка — отправим твоё объявление всем активным пользователям в радиусе 30 км.\n\n"
            f"💵 Стоимость одного запуска: <b>${PUSH_PRICE_USD}</b>.\n\n"
            "Выбери способ оплаты:",
            reply_markup=kb_payment_method()
        )

   # Баннер
    if txt == "🖼 Баннер (премиум)":
        events = _load_events()
        user_events = [e for e in events if int(e.get("author", 0)) == int(m.from_user.id)]
        if not user_events:
            await state.clear()
            return await m.answer("❌ У тебя пока нет событий для баннера.", reply_markup=kb_main())

        current = user_events[-1]
        await m.answer(
            "🖼 <b>Баннер (премиум)</b> — крупный баннер твоего события, который показывается наверху экрана после приветствия у пользователей рядом.\n"
            "Отлично подходит для вечеринок, концертов, открытий и любых крупных мероприятий, когда нужно максимальное внимание.\n"
        )

        media_files = current.get("media_files") or []
        b_media = None

        # Если есть своё медиа — используем его
        if media_files:
            f = media_files[0]
            b_media = {
                "type": f.get("type"),
                "file_id": f.get("file_id"),
                "is_local": f.get("is_local", False),
            }
        else:
            # Если медиа нет — используем логотип по умолчанию
            logo_path = None
            for ext in ("png", "jpg", "jpeg"):
                p = f"imgonline-com-ua-Resize-poVtNXt7aue6.{ext}"
                if os.path.exists(p):
                    logo_path = p
                    break
            if logo_path:
                b_media = {
                    "type": "photo",
                    "file_id": logo_path,
                    "is_local": True,
                }

        parts = []
        if current.get("title"):
            parts.append(sanitize(current["title"]))
        if current.get("description"):
            parts.append(sanitize(current["description"]))

        b_text = "\n\n".join(parts) if parts else None

        await state.update_data(
            b_media=b_media,
            b_text=b_text,
            b_link=current.get("contact"),
            b_lat=current.get("lat"),
            b_lon=current.get("lon"),
        )

        await state.set_state(AddBanner.duration)
        return await m.answer("Выбери срок показа баннера:", reply_markup=kb_banner_duration())



@dp.message(StateFilter(AddEvent.pay_option), F.text == "⬅ Назад")
async def ev_opt_back(m: Message, state: FSMContext):
    await state.set_state(AddEvent.upsell)
    await m.answer("Выбери дополнительную опцию:\n\n"
        "ℹ️ Если что-то пошло не так — нажми меню слева от строки ввода и выбери «Перезапустить бота».", reply_markup=kb_upsell())


@dp.message(StateFilter(AddEvent.upsell_more))
async def ev_upsell_more(m: Message, state: FSMContext):
    txt = m.text or ""
    if txt == "➕ Добавить ещё опцию":
        await state.set_state(AddEvent.upsell)
        return await m.answer("Выберите дополнительную опцию для этого объявления:", reply_markup=kb_upsell())

    if txt in ("🏠 Главное меню", "Главное меню"):
        await state.clear()
        return await m.answer("Возврат в главное меню 👇", reply_markup=kb_main())

    await m.answer("Выбери вариант из меню ниже 👇", reply_markup=kb_upsell_more())


@dp.message(StateFilter(AddEvent.pay_option))
async def ev_opt_router(m: Message, state: FSMContext):
    txt = m.text or ""
    data = await state.get_data()

    # выбор срока ТОП
    if txt.startswith("⭐ "):
        try:
            days = int(txt.split()[1])
        except Exception:
            return await m.answer("❌ Не понял срок. Выбери из меню.", reply_markup=kb_top_duration())

        if days not in TOP_PRICES:
            return await m.answer("❌ Такого срока нет.", reply_markup=kb_top_duration())

        events = _load_events()
        user_events = [e for e in events if int(e.get("author", 0)) == int(m.from_user.id)]
        if not user_events:
            await state.clear()
            return await m.answer("❌ У тебя нет событий для ТОП.", reply_markup=kb_main())

        current = user_events[-1]
        await state.update_data(opt_type="top", opt_event_id=current["id"], opt_days=days, _pay_uuid=None)

        price = TOP_PRICES[days]
        return await m.answer(
            f"⭐ ТОП-продвижение на {days} дней. Стоимость: ${price}.\n\n"
            "Выбери способ оплаты:",
            reply_markup=kb_payment_method()
        )

    # Если это запрос на оплату опции

    if txt == "💳 Оплата картой (BitPapa)":
        txt_help = (
            "💳 <b>Оплата картой через BitPapa</b>\n\n"
            "1️⃣ Открой BitPapa по ссылке:\n"
            f"{BITPAPA_REF_LINK}\n\n"
            "2️⃣ Купи USDT удобным способом. Рекомендуем продавцов со статусом 🟢 <b>Online</b>, "
            "рейтингом от 99% и с 100+ завершёнными сделками.\n"
            "3️⃣ После покупки вернись в бота, нажми «💳 Получить ссылку на оплату» и оплати счёт USDT.\n"
            "4️⃣ После отправки USDT по ссылке нажми «✅ Я оплатил»."
        )
        return await m.answer(txt_help, reply_markup=kb_payment())

    if txt == "🪙 Оплата криптовалютой (USDT)":
        return await m.answer(
            "🪙 Ты выбрал оплату уже имеющейся криптовалютой (USDT).\n\n"
            "Сейчас можно получить ссылку на оплату, оплатить её из своего кошелька, "
            "а затем нажать «✅ Я оплатил».",
            reply_markup=kb_payment()
        )

    if txt == "💳 Получить ссылку на оплату":
        data = await state.get_data()
        opt_type = data.get("opt_type")
        ev_id = data.get("opt_event_id")
        days = data.get("opt_days")

        if opt_type not in ("top", "push") or not ev_id:
            return await m.answer("❌ Не выбрана опция.", reply_markup=kb_upsell())

        # Проверяем, есть ли уже активный счёт по опции и он моложе 24 часов
        invoice_uuid = data.get("_pay_uuid")
        existing_link = data.get("_pay_link")
        created_at_str = data.get("_pay_created")
        created_at = _safe_dt(created_at_str) if created_at_str else None
        if invoice_uuid and existing_link and created_at:
            if datetime.now() - created_at < timedelta(hours=24):
                return await m.answer(
                    f"У тебя уже есть активный счёт (действителен 24 часа):\n{existing_link}\n\n"
                    "После оплаты нажми «✅ Я оплатил».",
                    reply_markup=kb_payment()
                )

        if opt_type == "top":
            amount = TOP_PRICES.get(days)
            if not amount:
                return await m.answer("❌ Неверный срок ТОП.", reply_markup=kb_top_duration())
            desc = f"PartyRadar: ТОП на {days} дн. для события #{ev_id}"
        else:
            amount = PUSH_PRICE_USD
            desc = f"PartyRadar: PUSH для события #{ev_id}"

        order_id = str(m.from_user.id)
        link, invoice_id = await cc_create_invoice(amount, order_id, desc)
        if not link or not invoice_id:
            return await m.answer("⚠️ Не удалось создать счёт.", reply_markup=kb_payment())

        pay = _load_payments()
        pay[str(m.from_user.id)] = {
            "type": opt_type,
            "user_id": m.from_user.id,
            "invoice_uuid": invoice_id,
            "payload": {"event_id": ev_id, "days": days},
        }
        _save_payments(pay)
        await state.update_data(
            _pay_uuid=invoice_id,
            _pay_link=link,
            _pay_created=datetime.now().isoformat()
        )

        return await m.answer(
            f"💳 Ссылка на оплату:\n{link}\n\nПосле оплаты нажми «✅ Я оплатил».",
            reply_markup=kb_payment()
        )


    
    if txt == "✅ Я оплатил":
        data = await state.get_data()
        invoice_uuid = data.get("_pay_uuid")
        opt_type = data.get("opt_type")
        ev_id = data.get("opt_event_id")
        days = data.get("opt_days")
        already_done = data.get("opt_done")

        # Если этот платёж по опции уже был успешно обработан — не выполняем действие повторно
        if already_done:
            await state.set_state(AddEvent.upsell_more)
            return await m.answer(
                "✅ Эта опция уже активирована для объявления.\n"
                "Можешь выбрать ещё одну опцию:",
                reply_markup=kb_upsell_more()
            )

        if not invoice_uuid:
            return await m.answer("❌ Счёт не найден.", reply_markup=kb_payment())

        paid = await cc_is_paid(invoice_uuid)
        if not paid:
            return await m.answer("❌ Оплата не найдена. Подожди и попробуй снова.", reply_markup=kb_payment())

        events = _load_events()
        target = next((e for e in events if e["id"] == ev_id), None)
        if not target:
            await state.clear()
            return await m.answer("❌ Событие не найдено.", reply_markup=kb_main())

        if opt_type == "top":
            if target.get("is_top"):
                return await m.answer("❌ Это объявление уже в ТОПе.", reply_markup=kb_upsell_more())
            target["is_top"] = True
            target["top_expire"] = (datetime.now() + timedelta(days=days)).isoformat()
            target["top_paid_at"] = datetime.now().isoformat()
            _save_events(events)
            await state.update_data(opt_done=True)
            await state.set_state(AddEvent.upsell_more)
            return await m.answer(
                f"🎉 ТОП активирован на {days} дней!\n"
                "Добавить ещё одну опцию к этому объявлению?",
                reply_markup=kb_upsell_more()
            )

        if opt_type == "push":
            sent = await send_push_for_event(target)
            await state.update_data(opt_done=True)
            await state.set_state(AddEvent.upsell_more)
            return await m.answer(
                f"📣 PUSH-рассылка отправлена. Получателей: {sent}.\n"
                "Добавить ещё одну опцию к этому объявлению?",
                reply_markup=kb_upsell_more()
            )
    await m.answer("Выбери пункт из меню.", reply_markup=kb_upsell())


# ===================== БАННЕРЫ (АПСЕЛ) =====================

@dp.message(AddBanner.duration, F.text == "⬅ Назад")
async def banner_duration(m: Message, state: FSMContext):
    if m.text == "⬅ Назад":
        await state.set_state(AddEvent.upsell)
        return await m.answer("Выберите дополнительную опцию:", reply_markup=kb_upsell())

@dp.message(StateFilter(AddBanner.duration))
async def banner_choose_duration(m: Message, state: FSMContext):
    if m.text not in BANNER_DURATIONS:
        return await m.answer("Выбери один из вариантов:", reply_markup=kb_banner_duration())

    days, amount = BANNER_DURATIONS[m.text]
    await state.update_data(b_days=days, _pay_uuid=None)
    await state.set_state(AddBanner.payment)

    await m.answer(
        f"📢 Баннер на {days} дн.\n"
        f"Стоимость: ${amount}.\n\n"
        "Выбери способ оплаты:",
        reply_markup=kb_payment_method()
    )



@dp.message(AddBanner.payment, F.text == "💳 Оплата картой (BitPapa)")
async def banner_pay_method_card(m: Message, state: FSMContext):
    txt = (
        "💳 <b>Оплата картой через BitPapa</b>\n\n"
        "1️⃣ Открой BitPapa по ссылке:\n"
        f"{BITPAPA_REF_LINK}\n\n"
        "2️⃣ Купи USDT удобным способом. Рекомендуем продавцов со статусом 🟢 <b>Online</b>, "
        "рейтингом от 99% и с 100+ завершёнными сделками.\n"
        "3️⃣ После покупки вернись в бота, нажми «💳 Получить ссылку на оплату» и оплати счёт USDT.\n"
        "4️⃣ После отправки USDT по ссылке нажми «✅ Я оплатил»."
    )
    await m.answer(txt, reply_markup=kb_payment())


@dp.message(AddBanner.payment, F.text == "🪙 Оплата криптовалютой (USDT)")
async def banner_pay_method_crypto(m: Message, state: FSMContext):
    await m.answer(
        "🪙 Ты выбрал оплату уже имеющейся криптовалютой (USDT).\n\n"
        "Сейчас можно получить ссылку на оплату, оплатить её из своего кошелька, "
        "а затем нажать «✅ Я оплатил».",
        reply_markup=kb_payment()
    )


@dp.message(AddBanner.payment, F.text == "💳 Получить ссылку на оплату")
async def banner_pay_link(m: Message, state: FSMContext):
    data = await state.get_data()
    days = data.get("b_days")
    if not days:
        return await m.answer("❌ Срок не выбран.", reply_markup=kb_banner_duration())

    # Если уже есть активный счёт и он моложе 24 часов — просто повторно отправляем ссылку
    existing_uuid = data.get("_pay_uuid")
    existing_link = data.get("_pay_link")
    created_at_str = data.get("_pay_created")
    created_at = _safe_dt(created_at_str) if created_at_str else None
    if existing_uuid and existing_link and created_at:
        if datetime.now() - created_at < timedelta(hours=24):
            return await m.answer(
                f"У тебя уже есть активный счёт на баннер (действителен 24 часа):\n{existing_link}\n\n"
                "После оплаты нажми «✅ Я оплатил».",
                reply_markup=kb_payment()
            )

    # проверка на уже существующий баннер в этом районе
    lat = data.get("b_lat")
    lon = data.get("b_lon")
    if lat is not None and lon is not None:
        banners = _load_banners()
        now = datetime.now()
        for b in banners:
            exp = _safe_dt(b.get("expire"))
            if not exp or exp <= now:
                continue
            b_lat = b.get("lat")
            b_lon = b.get("lon")
            if b_lat is None or b_lon is None:
                continue
            try:
                dist = geodesic((lat, lon), (b_lat, b_lon)).km
            except Exception:
                continue
            if dist <= DEFAULT_RADIUS_KM:
                return await m.answer(
                    "❌ В этом районе уже есть активный баннер.\n"
                    "Можно разместить новый, когда текущий истечёт.",
                    reply_markup=kb_main()
                )

    amount = None
    for _, (d, a) in BANNER_DURATIONS.items():
        if d == days:
            amount = a
            break
    if amount is None:
        return await m.answer("❌ Тариф не найден.", reply_markup=kb_banner_duration())

    order_id = f"banner_{m.from_user.id}_{int(datetime.now().timestamp())}_{days}"
    link, uuid = await cc_create_invoice(amount, order_id, f"PartyRadar banner {days}d")
    if not link or not uuid:
        return await m.answer("⚠ Не удалось получить ссылку.", reply_markup=kb_payment())

    pay = _load_payments()
    pay[uuid] = {"type": "banner_buy", "user_id": m.from_user.id, "payload": data}
    _save_payments(pay)

    await state.update_data(
        _pay_uuid=uuid,
        _pay_link=link,
        _pay_created=datetime.now().isoformat()
    )
    await m.answer(
        f"💳 Ссылка на оплату баннера:\n{link}\n\nПосле оплаты нажми «✅ Я оплатил».",
        reply_markup=kb_payment()
    )


@dp.message(AddBanner.payment, F.text == "✅ Я оплатил")
async def banner_paid(m: Message, state: FSMContext):
    data = await state.get_data()
    uuid = data.get("_pay_uuid")
    already_done = data.get("banner_done")

    # Если этот баннер уже был успешно активирован — не создаём его повторно
    if already_done:
        await state.set_state(AddEvent.upsell_more)
        return await m.answer(
            "✅ Оплата этого баннера уже подтверждена.\n"
            "Добавить ещё одну опцию к этому объявлению?",
            reply_markup=kb_upsell_more()
        )

    if not uuid:
        return await m.answer("❌ Счёт не найден. Получи ссылку ещё раз.", reply_markup=kb_payment())

    paid = await cc_is_paid(uuid)
    if not paid:
        return await m.answer("❌ Оплата не найдена. Подожди и попробуй снова.", reply_markup=kb_payment())

    d = await state.get_data()
    media = d.get("b_media")
    if not media:
        return await m.answer("❌ Медиа не найдено. Начни заново.", reply_markup=kb_main())

    text = d.get("b_text")
    link = d.get("b_link")
    lat = d.get("b_lat")
    lon = d.get("b_lon")
    days = d.get("b_days", 1)

    banners = _load_banners()
    new_id = (max([b["id"] for b in banners]) + 1) if banners else 1

    now = datetime.now()
    expire = now + timedelta(days=days)

    banners.append({
        "id": new_id,
        "user_id": m.from_user.id,
        "text": text,
        "link": link,
        "lat": lat,
        "lon": lon,
        "media": media,
        "created": now.isoformat(),
        "expire": expire.isoformat(),
        "notified": False,
    })
    _save_banners(banners)

    # помечаем, что баннер уже активирован по этому платежу
    await state.update_data(banner_done=True)

    await state.set_state(AddEvent.upsell_more)
    await m.answer(
        "✅ Баннер активирован и будет показываться пользователям в твоём районе.\n"
        "Добавить ещё одну опцию к этому объявлению?",
        reply_markup=kb_upsell_more()
    )
@dp.message(AddBanner.payment, F.text == "⬅ Назад")
async def banner_pay_back(m: Message, state: FSMContext):
    await state.set_state(AddBanner.duration)
    await m.answer("🔙 Вернулись к выбору срока размещения баннера:", reply_markup=kb_banner_duration())


# ===================== ПОИСК СОБЫТИЙ =====================

@dp.message(F.text == "📍 Найти события рядом")
async def search_start(m: Message, state: FSMContext):
    await state.set_state(SearchEvents.menu)
    kb = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🔎 Все события рядом")],
            [KeyboardButton(text="🛒 Маркет"), KeyboardButton(text="💼 Работа")],
            [KeyboardButton(text="✨ Покажи себя"), KeyboardButton(text="🔍 Ищу тебя")],
            [KeyboardButton(text="⬅ Назад")],
        ],
        resize_keyboard=True
    )
    await m.answer(
        "Что ищем?\n\n"
        "🔎 Все события — живые встречи, тусовки, спорт и движ.\n"
        "🛒 Маркет — куплю/продам.\n"
        "💼 Работа — вакансии и соискатели.\n"
        "✨ Покажи себя — анкеты и самопрезентации.\n"
        "🔍 Ищу тебя — поиск людей и питомцев.",
        reply_markup=kb
    )


@dp.message(SearchEvents.menu)
async def search_menu_router(m: Message, state: FSMContext):
    text = m.text or ""
    if text == "⬅ Назад":
        await state.clear()
        return await m.answer("Главное меню:", reply_markup=kb_main())

    mapping = {
        "🔎 Все события рядом": "all",
        "🛒 Маркет": "market",
        "💼 Работа": "work",
        "✨ Покажи себя": "selfpromo",
        "🔍 Ищу тебя": "findyou",
    }
    category_filter = mapping.get(text)
    if not category_filter:
        return await m.answer("Выбери один из вариантов:", reply_markup=kb_main())

    await state.set_state(getattr(SearchEvents, category_filter))
    kb = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📍 Отправить геолокацию", request_location=True)],
            [KeyboardButton(text="⬅ Назад")],
        ],
        resize_keyboard=True
    )
    await m.answer(
        "📍 Отправь геолокацию (скрепка → Геопозиция → точка на карте).\n"
        f"Покажу объявления в радиусе ~{DEFAULT_RADIUS_KM} км.",
        reply_markup=kb
    )


async def _search_and_show(m: Message, user_loc, category_filter, state: FSMContext):
    users = _load_users()
    u = users.get(str(m.from_user.id)) or {}
    u["last_location"] = {"lat": user_loc[0], "lon": user_loc[1]}
    u["last_seen"] = datetime.now().isoformat()
    users[str(m.from_user.id)] = u
    _save_users(users)

    events = _load_events()
    now = datetime.now()
    found = []

    for ev in events:
        exp = _safe_dt(ev.get("expire"))
        if not exp or exp <= now:
            continue
        if ev.get("lat") is None or ev.get("lon") is None:
            continue

        cat = ev.get("category")
        if category_filter == "market" and cat not in ("🛒 Куплю", "💰 Продам"):
            continue
        if category_filter == "work" and cat not in ("💼 Ищу работу", "🧑‍💼 Предлагаю работу"):
            continue
        if category_filter == "selfpromo" and cat != "✨ Покажи себя":
            continue
        if category_filter == "findyou" and cat != "🔍 Ищу тебя":
            continue

        dist = geodesic(user_loc, (ev["lat"], ev["lon"])).km
        if dist <= DEFAULT_RADIUS_KM:
            found.append((ev, dist))

    def _sort_key(item):
        ev, dist = item
        is_top = ev.get("is_top")
        if is_top:
            paid_dt = _safe_dt(ev.get("top_paid_at")) or _safe_dt(ev.get("created")) or datetime.min
            return (0, -paid_dt.timestamp(), dist)
        return (1, dist, 0)

    found.sort(key=_sort_key)
    await state.clear()

    if not found:
        kb = ReplyKeyboardMarkup(
            keyboard=[
                [KeyboardButton(text="➕ Создать событие")],
                [KeyboardButton(text="⬅ Назад")],
            ],
            resize_keyboard=True
        )
        return await m.answer("Ничего рядом не найдено. Можно создать своё событие 🤟", reply_markup=kb)

    # Чтобы ТОП-публикации были «внизу» чата и бросались в глаза первыми,
    # делим результаты на обычные и ТОП и управляем порядком вручную.
    top_events = [(ev, dist) for ev, dist in found if ev.get("is_top")]
    regular_events = [(ev, dist) for ev, dist in found if not ev.get("is_top")]

    # Сначала обычные события
    for ev, dist in regular_events:
        try:
            await send_event_media(m.chat.id, ev, with_distance=dist)
        except Exception:
            await m.answer(format_event_card(ev, with_distance=dist))

    # Затем ТОП-события в ОБРАТНОМ порядке,
    # чтобы последним отправленным (и самым заметным) был последний оплаченный ТОП.
    for ev, dist in reversed(top_events):
        try:
            await send_event_media(m.chat.id, ev, with_distance=dist)
        except Exception:
            await m.answer(format_event_card(ev, with_distance=dist))



@dp.message(SearchEvents.all, F.location)
async def search_all_with_location(m: Message, state: FSMContext):
    await _search_and_show(m, (m.location.latitude, m.location.longitude), "all", state)


@dp.message(SearchEvents.market, F.location)
async def search_market_with_location(m: Message, state: FSMContext):
    await _search_and_show(m, (m.location.latitude, m.location.longitude), "market", state)


@dp.message(SearchEvents.work, F.location)
async def search_work_with_location(m: Message, state: FSMContext):
    await _search_and_show(m, (m.location.latitude, m.location.longitude), "work", state)


@dp.message(SearchEvents.selfpromo, F.location)
async def search_selfpromo_with_location(m: Message, state: FSMContext):
    await _search_and_show(m, (m.location.latitude, m.location.longitude), "selfpromo", state)


@dp.message(SearchEvents.findyou, F.location)
async def search_findyou_with_location(m: Message, state: FSMContext):
    await _search_and_show(m, (m.location.latitude, m.location.longitude), "findyou", state)


@dp.message(StateFilter(
    SearchEvents.all,
    SearchEvents.market,
    SearchEvents.work,
    SearchEvents.selfpromo,
    SearchEvents.findyou
), F.text == "⬅ Назад")
async def search_location_back(m: Message, state: FSMContext):
    # Возвращаем к меню выбора типа поиска
    await state.set_state(SearchEvents.menu)
    kb = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🔎 Все события рядом")],
            [KeyboardButton(text="🛒 Маркет"), KeyboardButton(text="💼 Работа")],
            [KeyboardButton(text="✨ Покажи себя"), KeyboardButton(text="🔍 Ищу тебя")],
            [KeyboardButton(text="⬅ Назад")],
        ],
        resize_keyboard=True
    )
    await m.answer(
        "Окей, вернулись к выбору режима поиска.\n\n"
        "Что ищем?",
        reply_markup=kb
    )


@dp.message(StateFilter(
    SearchEvents.all,
    SearchEvents.market,
    SearchEvents.work,
    SearchEvents.selfpromo,
    SearchEvents.findyou
))
async def search_location_wrong_input(m: Message, state: FSMContext):
    # Любой другой текст на шаге локации — не сбрасываем FSM, а объясняем, что нужно
    kb = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📍 Отправить геолокацию", request_location=True)],
            [KeyboardButton(text="⬅ Назад")],
        ],
        resize_keyboard=True
    )
    await m.answer(
        "Сейчас нужно отправить <b>геолокацию</b> (скрепка → Геопозиция → точка на карте).\n\n"
        "Или нажми «⬅ Назад», чтобы поменять тип поиска.",
        reply_markup=kb
    )


# ===================== ИЗБРАННОЕ =====================

@dp.callback_query(F.data.startswith("fav_add:"))
async def cb_fav_add(cq: CallbackQuery):
    _, ev_id_str = cq.data.split(":", 1)
    ev_id = int(ev_id_str)

    events = _load_events()
    ev = next((e for e in events if e.get("id") == ev_id), None)
    if not ev:
        return await cq.answer("Событие не найдено.", show_alert=True)

    users = _load_users()
    u = users.get(str(cq.from_user.id)) or {}
    fav = u.get("favorites") or []
    if ev_id in fav:
        return await cq.answer("Уже в избранном.", show_alert=True)
    fav.append(ev_id)
    u["favorites"] = fav
    users[str(cq.from_user.id)] = u
    _save_users(users)

    await cq.answer("Добавлено в избранное ⭐", show_alert=False)


@dp.message(F.text == "⭐ Избранное")
async def show_favorites(m: Message):
    users = _load_users()
    u = users.get(str(m.from_user.id)) or {}
    fav_ids = u.get("favorites") or []
    if not fav_ids:
        return await m.answer("У тебя пока нет избранных событий ⭐", reply_markup=kb_main())

    events = _load_events()
    now = datetime.now()
    fav_events = []
    for ev in events:
        if ev.get("id") in fav_ids:
            exp = _safe_dt(ev.get("expire"))
            if exp and exp > now:
                fav_events.append(ev)

    if not fav_events:
        u["favorites"] = []
        users[str(m.from_user.id)] = u
        _save_users(users)
        return await m.answer(
            "Раньше здесь были события, но их срок уже истёк 🕒\n"
            "Добавь новые в избранное ⭐",
            reply_markup=kb_main()
        )

    await m.answer("Твои избранные события 👇")
    for ev in fav_events:
        try:
            await send_event_media(m.chat.id, ev)
        except Exception:
            await m.answer(format_event_card(ev))

    await m.answer(
        "Готово 🙌\nЕсли событие истекает — оно исчезает и из избранного автоматически.",
        reply_markup=kb_main()
    )


# ===================== УДАЛЕНИЕ СОБЫТИЙ =====================

@dp.callback_query(F.data.startswith("ev_del:"))
async def cb_delete_event(cq: CallbackQuery):
    try:
        _, ev_id_str = cq.data.split(":", 1)
        ev_id = int(ev_id_str)
    except Exception:
        return await cq.answer("Ошибка идентификатора.", show_alert=True)

    events = _load_events()
    target = next((e for e in events if e.get("id") == ev_id), None)
    if not target:
        return await cq.answer("Событие уже удалено.", show_alert=True)

    if int(target.get("author", 0)) != int(cq.from_user.id):
        return await cq.answer("Это не твоё объявление.", show_alert=True)

    target["expire"] = datetime.now().isoformat()
    _save_events(events)

    await cq.answer("Событие удалено.")
    try:
        await cq.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass


# ===================== PUSH-ДЕЙМОН + ПРОДЛЕНИЕ =====================

async def push_daemon():
    """Пуш за 2 часа до окончания событий и баннеров + снятие истёкшего ТОПа."""
    while True:
        try:
            now = datetime.now()

            # События
            events = _load_events()
            changed = False
            for ev in events:
                # снять ТОП по истечении
                if ev.get("is_top") and ev.get("top_expire"):
                    te = _safe_dt(ev["top_expire"])
                    if te and te <= now:
                        ev["is_top"] = False
                        ev["top_expire"] = None
                        changed = True

                exp = _safe_dt(ev.get("expire"))
                if not exp or ev.get("notified"):
                    continue
                if timedelta(0) < (exp - now) <= timedelta(hours=PUSH_LEAD_HOURS):
                    ev["notified"] = True
                    changed = True
                    kb = InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(text="📅 +1 день", callback_data=f"extend_ev:{ev['id']}:24")],
                        [InlineKeyboardButton(text="⏱ +3 дня", callback_data=f"extend_ev:{ev['id']}:72")],
                        [InlineKeyboardButton(text="⏱ +7 дней", callback_data=f"extend_ev:{ev['id']}:168")],
                        [InlineKeyboardButton(text="⏱ +30 дней", callback_data=f"extend_ev:{ev['id']}:720")],
                    ])
                    try:
                        await bot.send_message(
                            ev["author"],
                            f"⏳ Событие «{ev['title']}» скоро завершится. Продлить?",
                            reply_markup=kb
                        )
                    except Exception:
                        pass
            if changed:
                _save_events(events)

            # Баннеры
            banners = _load_banners()
            b_changed = False
            for b in banners:
                exp = _safe_dt(b.get("expire"))
                if not exp or b.get("notified"):
                    continue
                if timedelta(0) < (exp - now) <= timedelta(hours=PUSH_LEAD_HOURS):
                    b["notified"] = True
                    b_changed = True
                    kb = InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(text="📆 +1 день", callback_data=f"extend_bn:{b['id']}:1")],
                        [InlineKeyboardButton(text="📆 +3 дня", callback_data=f"extend_bn:{b['id']}:3")],
                        [InlineKeyboardButton(text="📆 +7 дней", callback_data=f"extend_bn:{b['id']}:7")],
                        [InlineKeyboardButton(text="📆 +14 дней", callback_data=f"extend_bn:{b['id']}:14")],
                        [InlineKeyboardButton(text="📆 +30 дней", callback_data=f"extend_bn:{b['id']}:30")],
                    ])
                    try:
                        await bot.send_message(
                            b["owner"],
                            "⏳ Срок показа баннера заканчивается. Продлить?",
                            reply_markup=kb
                        )
                    except Exception:
                        pass
            if b_changed:
                _save_banners(banners)

        except Exception as e:
            logging.exception(f"push_daemon error: {e}")

        await asyncio.sleep(300)


@dp.callback_query(F.data.startswith("extend_ev:"))
async def cb_extend_event(cq: CallbackQuery):
    try:
        _, ev_id, hours = cq.data.split(":")
        ev_id = int(ev_id)
        hours = int(hours)
    except Exception:
        return await cq.answer("Ошибка", show_alert=True)

    amount = TARIFFS_USD.get(hours)
    if not amount:
        return await cq.answer("Тариф не найден", show_alert=True)

    order_id = f"extend_event_{ev_id}_{cq.from_user.id}_{hours}_{int(datetime.now().timestamp())}"
    link, uuid = await cc_create_invoice(amount, order_id, f"PartyRadar event extend {hours}h")
    if not link or not uuid:
        return await cq.answer("Не удалось создать счёт", show_alert=True)

    pay = _load_payments()
    pay[uuid] = {"type": "event_extend", "user_id": cq.from_user.id, "payload": {"event_id": ev_id, "hours": hours}}
    _save_payments(pay)

    await cq.message.answer(
        f"💳 <b>Оплата продления</b>\n\n"
        f"1️⃣ Если хочешь оплатить <b>картой</b>, открой BitPapa по ссылке:\n{BITPAPA_REF_LINK}\n\n"
        f"2️⃣ Купи USDT удобным способом, затем оплати продление по ссылке:\n{link}\n\n"
        "3️⃣ Если у тебя уже есть USDT — просто оплати по ссылке выше из своего кошелька.\n\n"
        "Продление активируется автоматически в течение нескольких минут."
    )
    await cq.answer()


@dp.callback_query(F.data.startswith("extend_bn:"))
async def cb_extend_banner(cq: CallbackQuery):
    try:
        _, b_id, days = cq.data.split(":")
        b_id = int(b_id)
        days = int(days)
    except Exception:
        return await cq.answer("Ошибка", show_alert=True)

    amount = None
    for _, (d, a) in BANNER_DURATIONS.items():
        if d == days:
            amount = a
            break
    if amount is None:
        return await cq.answer("Тариф не найден", show_alert=True)

    order_id = f"extend_banner_{b_id}_{cq.from_user.id}_{days}_{int(datetime.now().timestamp())}"
    link, uuid = await cc_create_invoice(amount, order_id, f"PartyRadar banner extend {days}d")
    if not link or not uuid:
        return await cq.answer("Не удалось создать счёт", show_alert=True)

    pay = _load_payments()
    pay[uuid] = {"type": "banner_extend", "user_id": cq.from_user.id, "payload": {"banner_id": b_id, "days": days}}
    _save_payments(pay)

    await cq.message.answer(
        f"💳 <b>Оплата продления баннера</b>\n\n"
        f"1️⃣ Если хочешь оплатить <b>картой</b>, открой BitPapa по ссылке:\n{BITPAPA_REF_LINK}\n\n"
        f"2️⃣ Купи USDT удобным способом, затем оплати продление по ссылке:\n{link}\n\n"
        "3️⃣ Если у тебя уже есть USDT — просто оплати по ссылке выше из своего кошелька.\n\n"
        "Продление активируется автоматически в течение нескольких минут."
    )
    await cq.answer()


# ===================== ВЕБХУК ДЛЯ CRYPTOCLOUD =====================

async def handle_payment_callback(request: web.Request):
    try:
        body = await request.json()
    except Exception:
        body = await request.text()
        logging.info(f"callback non-json: {body}")
        return web.Response(text="ok")

    uuid = None
    status = None
    try:
        uuid = body.get("result", {}).get("uuid")
        status = body.get("result", {}).get("status")
    except Exception:
        pass

    if not uuid:
        return web.Response(text="ok")

    pay = _load_payments()
    entry = pay.get(uuid)
    if not entry:
        return web.Response(text="ok")

    if str(status).lower() != "paid":
        return web.Response(text="ok")

    p_type = entry.get("type")
    payload = entry.get("payload") or {}
    user_id = entry.get("user_id")

    # обработка продления событий/баннеров
    if p_type == "event_extend":
        events = _load_events()
        ev = next((e for e in events if e.get("id") == payload.get("event_id")), None)
        if ev:
            exp = _safe_dt(ev.get("expire")) or datetime.now()
            ev["expire"] = (exp + timedelta(hours=payload.get("hours", 24))).isoformat()
            _save_events(events)
            try:
                asyncio.create_task(
                    bot.send_message(user_id, "✅ Продление события оплачено и активировано.")
                )
            except Exception:
                pass

    if p_type == "banner_extend":
        banners = _load_banners()
        b = next((x for x in banners if x.get("id") == payload.get("banner_id")), None)
        if b:
            exp = _safe_dt(b.get("expire")) or datetime.now()
            b["expire"] = (exp + timedelta(days=payload.get("days", 1))).isoformat()
            _save_banners(banners)
            try:
                asyncio.create_task(
                    bot.send_message(user_id, "✅ Продление баннера оплачено и активировано.")
                )
            except Exception:
                pass

    return web.Response(text="ok")


# ===================== FALLBACK =====================

@dp.message(StateFilter(None))
async def fallback(m: Message):
    if not m.text:
        return
    await m.answer(
        "Я тебя не понял 🤔\n"
        "Пользуйся, пожалуйста, кнопками ниже — так всё работает стабильнее.",
        reply_markup=kb_main()
    )


# ===================== WEBHOOK / RUN =====================

async def make_web_app():
    try:
        app = web.Application()
        app.router.add_get("/verification-25a55.txt", handle_unitpay_verification)

        # Платёжные маршруты
        app.router.add_post("/payment_callback", handle_payment_callback)
        app.router.add_get("/payment_callback", handle_payment_callback)

        # Вебхук Telegram
        SimpleRequestHandler(dispatcher=dp, bot=bot).register(
            app,
            path="/webhook"
        )

        return app

    except Exception as e:
        logging.exception(f"❌ Ошибка make_web_app(): {e}")

        # Всегда возвращаем пустой Application, чтобы AppRunner не падал
        return web.Application()


async def on_startup():
    if not PUBLIC_URL:
        logging.warning("⚠ PUBLIC_URL не задан, webhook не будет установлен")
        return
    webhook_url = f"{PUBLIC_URL}/webhook"
    await bot.set_webhook(webhook_url)
    logging.info(f"🚀 Webhook set to {webhook_url}")


async def main():
    app = await make_web_app()
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("PORT", "10000"))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()

    await on_startup()
    logging.info("✅ Webhook server running")

    asyncio.create_task(push_daemon())

    while True:
        await asyncio.sleep(3600)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logging.info("🛑 Server stopped manually")
